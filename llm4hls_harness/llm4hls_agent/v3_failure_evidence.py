"""Bounded, factual failure evidence for task-aware V3 planning.

The extractors deliberately consume only an already materialized ``ToolResult``
(or its JSON mapping) plus an optional, allow-listed validation summary.  They
never open artifact references or forward a complete Vitis log to a Planner.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence


CSIM_FAILURE_EVIDENCE_SCHEMA = "v3c.csim-failure-evidence.v1"
SYNTH_FAILURE_EVIDENCE_SCHEMA = "v3c.synth-failure-evidence.v1"
COSIM_FAILURE_EVIDENCE_SCHEMA = "v3c.cosim-failure-evidence.v1"

MAX_LOG_LINES = 8
MAX_LOG_LINE_CHARS = 320
MAX_SUMMARY_CHARS = 480
MAX_SOURCE_LOCATIONS = 6
_MAX_SCANNED_LINES = 256
_MAX_VALIDATION_VALUES = 48

_SOURCE_SUFFIXES = r"c|cc|cpp|cxx|h|hh|hpp|hxx"
_SOURCE_LOCATION_RE = re.compile(
    rf"(?P<path>(?:(?:[A-Za-z]:)?[/\\])?(?:[A-Za-z0-9_.-]+[/\\])*"
    rf"[A-Za-z0-9_.-]+\.(?:{_SOURCE_SUFFIXES}))"
    r":(?P<line>\d+)(?::(?P<column>\d+))?",
    re.IGNORECASE,
)
_ABSOLUTE_SOURCE_RE = re.compile(
    rf"(?:(?:[A-Za-z]:)?[/\\])(?:[^/\\\s:()\[\]{{}}]+[/\\])+"
    rf"(?P<name>[^/\\\s:()\[\]{{}}]+\.(?:{_SOURCE_SUFFIXES}))",
    re.IGNORECASE,
)
_UNIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:[^/\s:]+/)+[^\s,;]+"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"\b[A-Za-z]:\\(?:[^\\\s:]+\\)+[^\s,;]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:\b(?:[a-z0-9_]*api[_-]?key|authorization)\s*[:=]\s*"
    r"(?:bearer\s+)?\S+|\bbearer\s+\S+)",
    re.IGNORECASE,
)
_SECRET_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b")
_EXPECTED_MARKER_RE = re.compile(
    r"\bexpected(?:\s+(?:value|result|output))?\s*(?:[:=]\s*|\s+)",
    re.IGNORECASE,
)
_ACTUAL_SEPARATOR_RE = re.compile(
    r"(?:\s*[,;]\s*|\s+)(?:but\s+)?"
    r"(?:actual|got|received|observed)(?:\s+(?:value|result|output))?"
    r"\s*(?:[:=]\s*|\s+)",
    re.IGNORECASE,
)
_ACTUAL_MARKER_RE = re.compile(
    r"\b(?:actual|got|received|observed)"
    r"(?:\s+(?:value|result|output))?\s*(?:[:=]\s*|\s+)",
    re.IGNORECASE,
)

_VALIDATION_TEXT_KEYS = {
    "actual",
    "clock_violation",
    "clock_violations",
    "deadlock",
    "diagnostic",
    "diagnostics",
    "error",
    "error_summary",
    "errors",
    "expected",
    "failure",
    "failure_summary",
    "failures",
    "fifo",
    "interface",
    "message",
    "messages",
    "mismatch",
    "observed",
    "reason",
    "resource_violation",
    "resource_violations",
    "rtl_mismatch",
    "source_location",
    "source_locations",
    "status",
    "stream",
    "summary",
    "synthesis_error",
    "timeout",
    "unsupported_construct",
    "unsupported_constructs",
}

_CSIM_RELEVANT_TOKENS = (
    "error",
    "fatal",
    "assert",
    "mismatch",
    "expected",
    "actual",
    "got ",
    "failed",
    "failure",
    "undefined reference",
    "not declared",
    "no matching function",
    "segmentation",
    "out of range",
    "timeout",
)
_SYNTH_RELEVANT_TOKENS = (
    "error",
    "fatal",
    "unsupported",
    "not synthesizable",
    "cannot be synthesized",
    "cannot synthesize",
    "clock violation",
    "timing violation",
    "failed to meet clock",
    "negative slack",
    "resource violation",
    "resource limit",
    "over-util",
    "exceeds available",
    "timeout",
)
_SYNTH_SOURCE_ERROR_TOKENS = (
    "undefined function",
    "syn check fail",
    "source synthesis",
    "synthesis failed",
    "failed during synthesis",
)
_UNSUPPORTED_TOKENS = (
    "unsupported",
    "not synthesizable",
    "cannot be synthesized",
    "cannot synthesize",
)
_CLOCK_TOKENS = (
    "clock violation",
    "timing violation",
    "failed to meet clock",
    "negative slack",
)
_RESOURCE_TOKENS = (
    "resource violation",
    "resource limit",
    "over-util",
    "exceeds available",
    "resource exhausted",
)
_COSIM_RELEVANT_TOKENS = (
    "error",
    "fatal",
    "deadlock",
    "dead lock",
    "timeout",
    "timed out",
    "mismatch",
    "expected",
    "actual",
    "fifo",
    "stream",
    "interface",
    "handshake",
    "blocked",
    "stall",
    "failed",
    "failure",
)
_STREAM_TOKENS = ("fifo", "stream", "interface", "handshake", "channel")
_COSIM_SUMMARY_PRIORITY_TOKENS = (
    "deadlock",
    "dead lock",
    "rtl mismatch",
    "mismatch",
    "timed out",
    "timeout expired",
    "fatal",
    "error",
    "failed",
)


@dataclass(frozen=True)
class ResourceViolation:
    resource: str | None = None
    used: float | None = None
    available: float | None = None
    summary: str | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {}
        if self.resource is not None:
            value["resource"] = self.resource
        if self.used is not None:
            value["used"] = self.used
        if self.available is not None:
            value["available"] = self.available
        if self.summary is not None:
            value["summary"] = self.summary
        return value


@dataclass(frozen=True)
class CSimEvidence:
    schema_version: str
    candidate_id: str
    action_id: str
    result_ref: str
    phase: str
    return_code: int | None
    failure_kind: str
    error_summary: str
    source_locations: tuple[str, ...]
    expected: str | None
    actual: str | None
    relevant_log_lines: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "action_id": self.action_id,
            "result_ref": self.result_ref,
            "phase": self.phase,
            "return_code": self.return_code,
            "failure_kind": self.failure_kind,
            "error_summary": self.error_summary,
            "source_locations": list(self.source_locations),
            "expected": self.expected,
            "actual": self.actual,
            "relevant_log_lines": list(self.relevant_log_lines),
        }


@dataclass(frozen=True)
class SynthFailureEvidence:
    schema_version: str
    candidate_id: str
    action_id: str
    result_ref: str
    phase: str
    return_code: int | None
    failure_kind: str
    synthesis_error: str | None
    source_locations: tuple[str, ...]
    unsupported_constructs: tuple[str, ...]
    clock_violation: Mapping[str, object] | None
    resource_violations: tuple[ResourceViolation, ...]
    relevant_log_lines: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "action_id": self.action_id,
            "result_ref": self.result_ref,
            "phase": self.phase,
            "return_code": self.return_code,
            "failure_kind": self.failure_kind,
            "synthesis_error": self.synthesis_error,
            "source_locations": list(self.source_locations),
            "unsupported_constructs": list(self.unsupported_constructs),
            "clock_violation": (
                dict(self.clock_violation)
                if self.clock_violation is not None
                else None
            ),
            "resource_violations": [item.to_dict() for item in self.resource_violations],
            "relevant_log_lines": list(self.relevant_log_lines),
        }


@dataclass(frozen=True)
class CoSimFailureEvidence:
    schema_version: str
    candidate_id: str
    action_id: str
    result_ref: str
    phase: str
    return_code: int | None
    failure_kind: str
    error_summary: str
    source_locations: tuple[str, ...]
    deadlock: bool
    timeout: bool
    rtl_mismatch: bool
    expected: str | None
    actual: str | None
    stream_fifo_interface_findings: tuple[str, ...]
    relevant_log_lines: tuple[str, ...]
    cosim_progress: str
    no_progress_seconds: float | None
    xsim_started: bool
    runtime_stage: str
    transaction_progress: str | None
    log_growth: str | None
    output_growth: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "action_id": self.action_id,
            "result_ref": self.result_ref,
            "phase": self.phase,
            "return_code": self.return_code,
            "failure_kind": self.failure_kind,
            "error_summary": self.error_summary,
            "source_locations": list(self.source_locations),
            "deadlock": self.deadlock,
            "timeout": self.timeout,
            "rtl_mismatch": self.rtl_mismatch,
            "expected": self.expected,
            "actual": self.actual,
            "stream_fifo_interface_findings": list(
                self.stream_fifo_interface_findings
            ),
            "relevant_log_lines": list(self.relevant_log_lines),
            "cosim_progress": self.cosim_progress,
            "no_progress_seconds": self.no_progress_seconds,
            "xsim_started": self.xsim_started,
            "runtime_stage": self.runtime_stage,
            "transaction_progress": self.transaction_progress,
            "log_growth": self.log_growth,
            "output_growth": self.output_growth,
        }


def _value(result: object, name: str, default: object = None) -> object:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _text_value(result: object, name: str) -> str:
    value = _value(result, name, "")
    return str(value) if value is not None else ""


def _integer_value(result: object, name: str) -> int | None:
    value = _value(result, name)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _basename(path: str) -> str:
    return re.split(r"[/\\]", path)[-1]


def _sanitize_line(value: str) -> str:
    compact = " ".join(str(value).strip().split())
    # Tool launch commands can contain thousands of path characters.  Bound
    # regex input before path redaction so an adversarial or generated log
    # line cannot trigger excessive backtracking in the sanitizer.
    compact = compact[: MAX_LOG_LINE_CHARS * 2]
    compact = _ABSOLUTE_SOURCE_RE.sub(lambda match: match.group("name"), compact)
    compact = _UNIX_ABSOLUTE_PATH_RE.sub("<local-path>", compact)
    compact = _WINDOWS_ABSOLUTE_PATH_RE.sub("<local-path>", compact)
    compact = _SECRET_ASSIGNMENT_RE.sub("<redacted-secret>", compact)
    compact = _SECRET_TOKEN_RE.sub("<redacted-secret>", compact)
    return compact[:MAX_LOG_LINE_CHARS]


def _validation_lines(
    value: object,
    *,
    output: list[str],
    depth: int = 0,
) -> None:
    if depth > 4 or len(output) >= _MAX_VALIDATION_VALUES:
        return
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if len(output) >= _MAX_VALIDATION_VALUES:
                return
            key = str(raw_key).casefold()
            if key not in _VALIDATION_TEXT_KEYS:
                # A container may have a neutral name, but its leaves still
                # have to be explicitly allow-listed.
                if isinstance(child, Mapping):
                    _validation_lines(child, output=output, depth=depth + 1)
                elif isinstance(child, Sequence) and not isinstance(
                    child, (str, bytes, bytearray)
                ):
                    for item in child:
                        if isinstance(item, Mapping):
                            _validation_lines(
                                item, output=output, depth=depth + 1
                            )
                continue
            if isinstance(child, Mapping):
                _validation_lines(child, output=output, depth=depth + 1)
            elif isinstance(child, Sequence) and not isinstance(
                child, (str, bytes, bytearray)
            ):
                for item in child:
                    if isinstance(item, Mapping):
                        _validation_lines(item, output=output, depth=depth + 1)
                    elif isinstance(item, (str, int, float)) and not isinstance(
                        item, bool
                    ):
                        output.append(f"{key}: {item}")
                        if len(output) >= _MAX_VALIDATION_VALUES:
                            return
            elif isinstance(child, str):
                output.append(f"{key}: {child}")
            elif child is True:
                output.append(f"{key}: true")


def _raw_lines(
    result: object, validation_evidence: Mapping[str, object] | None
) -> list[str]:
    values: list[str] = []
    raw_evidence = _value(result, "evidence", ())
    if isinstance(raw_evidence, str):
        raw_evidence = (raw_evidence,)
    tool_line_limit = _MAX_SCANNED_LINES - _MAX_VALIDATION_VALUES
    for item in _sequence(raw_evidence):
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            values.extend(str(item).splitlines())
            if len(values) >= tool_line_limit:
                break
    values = values[:tool_line_limit]
    validation_values: list[str] = []
    if validation_evidence is not None:
        _validation_lines(validation_evidence, output=validation_values)
    values.extend(validation_values)
    return [_sanitize_line(line) for line in values[:_MAX_SCANNED_LINES] if line.strip()]


def _unique_bounded(values: Sequence[str], *, limit: int = MAX_LOG_LINES) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _sanitize_line(raw)
        key = value.casefold()
        if value and key not in seen:
            unique.append(value)
            seen.add(key)
        if len(unique) >= limit:
            break
    return tuple(unique)


def _matching_lines(lines: Sequence[str], tokens: Sequence[str]) -> tuple[str, ...]:
    return _unique_bounded(
        [line for line in lines if any(token in line.casefold() for token in tokens)]
    )


def _cosim_diagnostic_lines(lines: Sequence[str]) -> tuple[str, ...]:
    """Remove simulator launch/configuration noise from CoSim symptoms.

    XSIM command lines commonly contain an ``UVM_TIMEOUT=<value>`` plusarg.
    That is a configured watchdog, not evidence that the simulation timed out.
    Module-compilation chatter containing ``fifo`` is likewise not a FIFO
    finding unless the line also reports an actual diagnostic.
    """

    diagnostics: list[str] = []
    for line in lines:
        lowered = line.casefold()
        if "uvm_timeout" in lowered:
            continue
        if "compiling module" in lowered and not any(
            token in lowered
            for token in ("error", "fatal", "failed", "deadlock", "mismatch")
        ):
            continue
        diagnostics.append(line)
    return tuple(diagnostics)


def _resource_report_unavailable(line: str) -> bool:
    lowered = line.casefold()
    return any(
        marker in lowered
        for marker in (
            "resource_report_unavailable",
            "resource report unavailable",
            "resources unavailable",
        )
    )


def _has_positive_marker(
    lines: Sequence[str],
    marker: str,
    *,
    aliases: Sequence[str] = (),
) -> bool:
    markers = (marker, *aliases)
    for line in lines:
        lowered = line.casefold()
        for value in markers:
            if value not in lowered:
                continue
            escaped = re.escape(value)
            negative_patterns = (
                rf"\bno\s+(?:\w+\s+){{0,2}}{escaped}\b",
                rf"\bwithout\s+(?:\w+\s+){{0,2}}{escaped}\b",
                rf"\b{escaped}\s*(?::|=)?\s*false\b",
                rf"\b{escaped}\s+not\s+(?:found|detected|observed)\b",
            )
            if not any(re.search(pattern, lowered) for pattern in negative_patterns):
                return True
    return False


def _source_locations(lines: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for line in lines:
        for match in _SOURCE_LOCATION_RE.finditer(line):
            location = f"{_basename(match.group('path'))}:{match.group('line')}"
            if match.group("column"):
                location += f":{match.group('column')}"
            found.append(location)
    return _unique_bounded(found, limit=MAX_SOURCE_LOCATIONS)


def _mismatch_values(lines: Sequence[str]) -> tuple[str | None, str | None]:
    expected: str | None = None
    actual: str | None = None
    for line in lines:
        expected_match = _EXPECTED_MARKER_RE.search(line)
        if expected_match is not None:
            tail = line[expected_match.end() :]
            separator = _ACTUAL_SEPARATOR_RE.search(tail)
            expected_text = tail[: separator.start()] if separator else tail
            expected_text = expected_text.strip(" \t,;:.")[:160]
            if expected_text and expected is None:
                expected = expected_text
            if separator is not None and actual is None:
                actual_text = tail[separator.end() :].strip(" \t,;:.")[:160]
                actual = actual_text or None
        if actual is None:
            actual_match = _ACTUAL_MARKER_RE.search(line)
            if actual_match is not None:
                actual_text = line[actual_match.end() :].strip(" \t,;:.")[:160]
                actual = actual_text or None
        if expected is not None and actual is not None:
            break
    return expected, actual


def _identity(result: object, expected_kind: str) -> tuple[str, str, str, str, int | None, bool]:
    kind = _text_value(result, "kind")
    if kind != expected_kind:
        raise ValueError(f"expected {expected_kind} ToolResult, received {kind or 'unknown'}")
    phase = _text_value(result, "phase") or "unknown"
    return (
        _text_value(result, "candidate_id"),
        _text_value(result, "action_id"),
        _text_value(result, "result_ref"),
        phase,
        _integer_value(result, "return_code"),
        bool(_value(result, "ok", False)),
    )


def _summary(
    *,
    tool_name: str,
    phase: str,
    return_code: int | None,
    ok: bool,
    relevant_lines: Sequence[str],
) -> str:
    if relevant_lines:
        return relevant_lines[0][:MAX_SUMMARY_CHARS]
    if ok:
        return f"{tool_name} reported pass."
    suffix = f" (return_code={return_code})" if return_code is not None else ""
    return f"{tool_name} failed during phase {phase}{suffix}."[:MAX_SUMMARY_CHARS]


def _recursive_number(value: object, key: str, *, depth: int = 0) -> float | None:
    if depth > 4 or not isinstance(value, Mapping):
        return None
    for raw_key, child in value.items():
        if str(raw_key).casefold() == key.casefold():
            parsed = _number(child)
            if parsed is not None:
                return parsed
        parsed = _recursive_number(child, key, depth=depth + 1)
        if parsed is not None:
            return parsed
    return None


def _compact_runtime_value(value: object) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set, bytes)):
        return None
    rendered = _sanitize_line(str(value))
    return rendered or None


def _cosim_runtime_facts(
    *, lines: Sequence[str], cosim: Mapping[str, object]
) -> tuple[bool, str, str | None, str | None, str | None]:
    """Extract only explicit simulator-progress facts from bounded evidence."""

    lowered = "\n".join(lines).casefold()
    xsim_started = bool(
        re.search(r"\bxsim\b|rtl simulation (?:started|running)|simulation started", lowered)
        or cosim.get("xsim_started") is True
    )
    if any(token in lowered for token in ("runtime", "simulation running", "rtl test progress")):
        stage = "RUNTIME"
    elif "elaborat" in lowered:
        stage = "ELABORATE"
    elif "compil" in lowered:
        stage = "COMPILE"
    elif xsim_started:
        stage = "XSIM_STARTED"
    else:
        stage = "UNKNOWN"
    progress = _compact_runtime_value(cosim.get("transaction_progress"))
    if progress is None:
        for line in lines:
            if "transaction" not in line.casefold() and "progress" not in line.casefold():
                continue
            match = re.search(r"\b\d+\s*/\s*\d+\b", line)
            if match is not None:
                progress = match.group(0)
                break
    log_growth = _compact_runtime_value(
        cosim.get("log_growth") or cosim.get("log_bytes")
    )
    output_growth = _compact_runtime_value(
        cosim.get("output_growth") or cosim.get("output_bytes")
    )
    return xsim_started, stage, progress, log_growth, output_growth


def extract_csim_failure_evidence(
    result: object,
    *,
    validation_evidence: Mapping[str, object] | None = None,
) -> CSimEvidence:
    """Extract a bounded public CSim failure summary without reading artifacts."""

    candidate_id, action_id, result_ref, phase, return_code, ok = _identity(
        result, "csim"
    )
    lines = _raw_lines(result, validation_evidence)
    relevant = _matching_lines(lines, _CSIM_RELEVANT_TOKENS)
    expected, actual = _mismatch_values(lines)
    if ok:
        failure_kind = "NONE"
    elif phase == "compile_error":
        failure_kind = "COMPILE_ERROR"
    elif phase == "runtime_fail":
        failure_kind = "RUNTIME_FAIL"
    elif phase == "timeout":
        failure_kind = "TIMEOUT"
    elif phase == "tool_error":
        failure_kind = "TOOL_ERROR"
    else:
        failure_kind = "UNKNOWN"
    return CSimEvidence(
        schema_version=CSIM_FAILURE_EVIDENCE_SCHEMA,
        candidate_id=candidate_id,
        action_id=action_id,
        result_ref=result_ref,
        phase=phase,
        return_code=return_code,
        failure_kind=failure_kind,
        error_summary=_summary(
            tool_name="CSim",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=relevant,
        ),
        source_locations=_source_locations(lines),
        expected=expected,
        actual=actual,
        relevant_log_lines=relevant,
    )


def _resource_violations(
    report: Mapping[str, object], lines: Sequence[str]
) -> tuple[ResourceViolation, ...]:
    violations: list[ResourceViolation] = []
    used = _mapping(report.get("resources"))
    available = _mapping(report.get("available_resources"))
    for raw_name in sorted(set(used).intersection(available), key=str):
        used_value = _number(used.get(raw_name))
        available_value = _number(available.get(raw_name))
        if (
            used_value is not None
            and available_value is not None
            and used_value > available_value
        ):
            violations.append(
                ResourceViolation(
                    resource=str(raw_name),
                    used=used_value,
                    available=available_value,
                )
            )
    for line in _matching_lines(lines, _RESOURCE_TOKENS):
        if _resource_report_unavailable(line):
            continue
        violations.append(ResourceViolation(summary=line))
    # Keep the structured result bounded even if a malformed report contains
    # hundreds of resource keys.
    return tuple(violations[:MAX_LOG_LINES])


def extract_synth_failure_evidence(
    result: object,
    *,
    validation_evidence: Mapping[str, object] | None = None,
) -> SynthFailureEvidence:
    """Extract synthesis failure facts and only explicitly supported causes."""

    candidate_id, action_id, result_ref, phase, return_code, ok = _identity(
        result, "synth"
    )
    lines = _raw_lines(result, validation_evidence)
    diagnostic_lines = tuple(
        line for line in lines if not _resource_report_unavailable(line)
    )
    relevant = _matching_lines(diagnostic_lines, _SYNTH_RELEVANT_TOKENS)
    source_errors = _matching_lines(diagnostic_lines, _SYNTH_SOURCE_ERROR_TOKENS)
    unsupported = _matching_lines(diagnostic_lines, _UNSUPPORTED_TOKENS)
    report = _mapping(_value(result, "report", {}))
    estimated = _number(report.get("estimated_clock_period_ns"))
    target = _recursive_number(validation_evidence, "target_clock_period_ns")
    clock_violation: Mapping[str, object] | None = None
    if estimated is not None and target is not None and estimated > target:
        clock_violation = {
            "estimated_clock_period_ns": estimated,
            "target_clock_period_ns": target,
        }
    else:
        clock_lines = _matching_lines(diagnostic_lines, _CLOCK_TOKENS)
        if clock_lines:
            clock_violation = {"summary": clock_lines[0]}

    if ok:
        failure_kind = "NONE"
        synthesis_error = None
    elif phase == "timeout":
        failure_kind = "TIMEOUT"
        synthesis_error = _summary(
            tool_name="Synth",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=relevant,
        )
    elif phase == "tool_error":
        failure_kind = "TOOL_ERROR"
        synthesis_error = _summary(
            tool_name="Synth",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=relevant,
        )
    elif phase in {"synth_error", "compile_error"} or source_errors:
        failure_kind = "SYNTH_ERROR"
        synthesis_error = _summary(
            tool_name="Synth",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=relevant,
        )
    elif phase in {"clock_violation", "resource_violation", "constraint_violation"}:
        failure_kind = "CONSTRAINT_VIOLATION"
        synthesis_error = _summary(
            tool_name="Synth",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=relevant,
        )
    else:
        failure_kind = "UNKNOWN"
        synthesis_error = _summary(
            tool_name="Synth",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=relevant,
        )
    return SynthFailureEvidence(
        schema_version=SYNTH_FAILURE_EVIDENCE_SCHEMA,
        candidate_id=candidate_id,
        action_id=action_id,
        result_ref=result_ref,
        phase=phase,
        return_code=return_code,
        failure_kind=failure_kind,
        synthesis_error=synthesis_error,
        source_locations=_source_locations(diagnostic_lines),
        unsupported_constructs=unsupported,
        clock_violation=clock_violation,
        resource_violations=_resource_violations(report, diagnostic_lines),
        relevant_log_lines=relevant,
    )


def extract_cosim_failure_evidence(
    result: object,
    *,
    validation_evidence: Mapping[str, object] | None = None,
) -> CoSimFailureEvidence:
    """Extract explicit CoSim symptoms without treating every timeout as deadlock."""

    candidate_id, action_id, result_ref, phase, return_code, ok = _identity(
        result, "cosim"
    )
    lines = _raw_lines(result, validation_evidence)
    cosim = _mapping(_value(result, "cosim", {}))
    status = cosim.get("status")
    if isinstance(status, str) and status.strip():
        lines.append(_sanitize_line(f"cosim status: {status}"))
    diagnostic_lines = _cosim_diagnostic_lines(lines)
    relevant = _matching_lines(diagnostic_lines, _COSIM_RELEVANT_TOKENS)
    summary_lines = _matching_lines(
        diagnostic_lines, _COSIM_SUMMARY_PRIORITY_TOKENS
    ) or relevant
    deadlock = _has_positive_marker(
        diagnostic_lines, "deadlock", aliases=("dead lock",)
    )
    timeout = phase == "timeout" or _has_positive_marker(
        diagnostic_lines, "timeout", aliases=("timed out",)
    )
    expected, actual = _mismatch_values(diagnostic_lines)
    rtl_mismatch = (
        _has_positive_marker(diagnostic_lines, "mismatch")
        or (
            expected is not None
            and actual is not None
            and expected.casefold() != actual.casefold()
        )
    )
    if ok:
        failure_kind = "NONE"
    elif deadlock:
        failure_kind = "DEADLOCK"
    elif rtl_mismatch:
        failure_kind = "RTL_MISMATCH"
    elif timeout:
        failure_kind = "TIMEOUT"
    elif phase == "tool_error":
        failure_kind = "TOOL_ERROR"
    elif phase in {"cosim_fail", "synth_error", "compile_error"}:
        failure_kind = "COSIM_FAILURE"
    else:
        failure_kind = "UNKNOWN"
    explicit_no_progress = any(
        "no rtl test progress" in line.casefold()
        or "no progress" in line.casefold()
        for line in diagnostic_lines
    )
    elapsed = _value(result, "elapsed_s")
    no_progress_seconds = (
        float(elapsed)
        if explicit_no_progress
        and isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(float(elapsed))
        and float(elapsed) >= 0
        else None
    )
    progress = (
        "NO_RTL_TEST_PROGRESS"
        if explicit_no_progress
        else "TIMEOUT_WITHOUT_PROGRESS_CLASSIFICATION"
        if timeout
        else "COMPLETED_OR_OBSERVED"
    )
    (
        xsim_started,
        runtime_stage,
        transaction_progress,
        log_growth,
        output_growth,
    ) = _cosim_runtime_facts(lines=diagnostic_lines, cosim=cosim)
    return CoSimFailureEvidence(
        schema_version=COSIM_FAILURE_EVIDENCE_SCHEMA,
        candidate_id=candidate_id,
        action_id=action_id,
        result_ref=result_ref,
        phase=phase,
        return_code=return_code,
        failure_kind=failure_kind,
        error_summary=_summary(
            tool_name="CoSim",
            phase=phase,
            return_code=return_code,
            ok=ok,
            relevant_lines=summary_lines,
        ),
        source_locations=_source_locations(lines),
        deadlock=deadlock,
        timeout=timeout,
        rtl_mismatch=rtl_mismatch,
        expected=expected,
        actual=actual,
        stream_fifo_interface_findings=_matching_lines(
            diagnostic_lines, _STREAM_TOKENS
        ),
        relevant_log_lines=relevant,
        cosim_progress=progress,
        no_progress_seconds=no_progress_seconds,
        xsim_started=xsim_started,
        runtime_stage=runtime_stage,
        transaction_progress=transaction_progress,
        log_growth=log_growth,
        output_growth=output_growth,
    )


__all__ = [
    "COSIM_FAILURE_EVIDENCE_SCHEMA",
    "CSIM_FAILURE_EVIDENCE_SCHEMA",
    "SYNTH_FAILURE_EVIDENCE_SCHEMA",
    "MAX_LOG_LINE_CHARS",
    "MAX_LOG_LINES",
    "CSimEvidence",
    "CoSimFailureEvidence",
    "ResourceViolation",
    "SynthFailureEvidence",
    "extract_cosim_failure_evidence",
    "extract_csim_failure_evidence",
    "extract_synth_failure_evidence",
]
