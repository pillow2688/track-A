"""Deterministic, evidence-bound team retrospective for one V2 optimize run.

The report deliberately separates Provider claims from Harness decisions and Vitis
measurements.  It has no dependency on :mod:`optimization`, so the optimizer can
call it from its terminal artifact finalizer without creating an import cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from .artifacts import ArtifactManifestError, verify_artifact_manifest


REPORT_SCHEMA_VERSION = 1
_MODES = {"automatic", "offline"}
_STAGES = ("csim", "synth", "cosim")
_OPTIMIZATION_CLASSES = (
    "LOOP_PIPELINE",
    "LOOP_UNROLL",
    "MEMORY_LAYOUT",
    "LOOP_RESTRUCTURE",
)
_OUTPUT_REFS = {"artifact_manifest.json", "experimental_report.md"}
_CORE_FILES = (
    "task_spec.json",
    "run_config.json",
    "optimization_config.json",
    "workflow_result.json",
    "v2_result.json",
    "candidate_registry.json",
    "budget_ledger.jsonl",
    "trace.jsonl",
)
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "private_key",
}
_ABSOLUTE_POSIX_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:home|root|tmp|var|opt|mnt|workspace|Users|usr|etc|data|srv|run|proc|sys|dev)"
    r"(?:/[^\s\"'`<>|]+)+"
)
_ABSOLUTE_WINDOWS_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\(?:[^\s\"'`<>|]+\\?)+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s?\"'`<>]+)\?[^\s\"'`<>]+")


class V2TeamReportError(RuntimeError):
    """Raised when a trustworthy V2 retrospective cannot be reconstructed."""


@dataclass(frozen=True)
class LedgerAction:
    """One STARTED/terminal Ledger pair."""

    action_id: str
    kind: str
    candidate_id: str | None
    state: str
    actual_cost: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    elapsed_s: float
    started_timestamp: str | None
    terminal_timestamp: str | None
    result_ref: str | None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class RoundReview:
    """Normalized, immutable shell for one durable optimization attempt."""

    round_index: int
    parent_candidate_id: str
    candidate_id: str | None
    best_before: str
    best_after: str
    branch: Mapping[str, object]
    selector: Mapping[str, object]
    model_claim: Mapping[str, object]
    patch: Mapping[str, object]
    tools: tuple[Mapping[str, object], ...]
    cosim_gate: Mapping[str, object]
    score_and_comparison: Mapping[str, object]
    decision: str
    rejection_reason: str | None
    no_improvement_after: int
    tokens: Mapping[str, object]
    cost: Mapping[str, object]
    timing: Mapping[str, object]
    lesson: str
    next_action: str
    evidence_refs: tuple[str, ...]
    prompt: Mapping[str, object]


@dataclass(frozen=True)
class V2TeamReportData:
    """Single source of truth consumed by every report section."""

    schema_version: int
    task: Mapping[str, object]
    policy: Mapping[str, object]
    run_outcome: Mapping[str, object]
    baseline: Mapping[str, object]
    rounds: tuple[RoundReview, ...]
    candidate_tree: Mapping[str, object]
    final_validation: Mapping[str, object]
    fallback: Mapping[str, object] | None
    accounting: Mapping[str, object]
    cosim_summary: Mapping[str, object]
    findings: tuple[Mapping[str, object], ...]
    next_actions: tuple[Mapping[str, object], ...]
    evidence_index: tuple[str, ...]
    integrity: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible copy, useful for deterministic tests."""

        value = _plain(self)
        if not isinstance(value, dict):  # pragma: no cover - dataclass invariant
            raise AssertionError("report data did not serialize to an object")
        return value


def _plain(value: object) -> object:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise V2TeamReportError(f"{label} must be a non-negative integer")
    return value


def _sanitize_text(value: str) -> str:
    sanitized = _ABSOLUTE_POSIX_RE.sub("<ABSOLUTE_PATH>", value)
    sanitized = _ABSOLUTE_WINDOWS_RE.sub("<ABSOLUTE_PATH>", sanitized)
    sanitized = _BEARER_RE.sub("Bearer <REDACTED>", sanitized)
    return _URL_QUERY_RE.sub(r"\1?<REDACTED_QUERY>", sanitized)


def _is_secret_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        normalized in _SECRET_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
    )


def _redact(value: object, *, key: str = "") -> object:
    if key and _is_secret_key(key):
        return "<REDACTED>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, key=str(item_key))
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


class EvidenceReader:
    """Boundary-checked reader for automatic or sealed offline evidence.

    Offline mode verifies the Manifest first and thereafter refuses to read any
    run-local path not present in its artifact allowlist.  Automatic mode runs
    before the Manifest exists and instead checks every declared reference
    directly under the resolved run directory.
    """

    def __init__(self, run_dir: str | Path, *, mode: str = "automatic") -> None:
        if mode not in _MODES:
            raise V2TeamReportError(f"unsupported report mode: {mode}")
        self.mode = mode
        self.root = Path(run_dir).resolve()
        if not self.root.is_dir():
            raise V2TeamReportError("run directory does not exist or is not a directory")
        self._allowlist: frozenset[str] | None = None
        self.manifest: Mapping[str, object] | None = None
        self.manifest_sha256: str | None = None
        self.read_refs: set[str] = set()
        if mode == "offline":
            manifest_path = self.root / "artifact_manifest.json"
            if not manifest_path.is_file():
                raise V2TeamReportError(
                    "UNSEALED_LEGACY_RUN: artifact_manifest.json is missing"
                )
            try:
                manifest = verify_artifact_manifest(self.root)
                manifest_bytes = manifest_path.read_bytes()
            except (ArtifactManifestError, OSError) as exc:
                raise V2TeamReportError(f"MANIFEST_VERIFICATION_FAILED: {exc}") from exc
            paths: list[str] = []
            for raw in _sequence(manifest.get("artifacts")):
                entry = _mapping(raw)
                relative = entry.get("path")
                if not isinstance(relative, str):
                    raise V2TeamReportError("manifest contains a non-string artifact path")
                paths.append(relative)
            self._allowlist = frozenset(paths)
            self.manifest = manifest
            self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    @property
    def allowlist(self) -> frozenset[str] | None:
        return self._allowlist

    def _relative(self, reference: str) -> tuple[str, Path]:
        if not isinstance(reference, str) or not reference or "\x00" in reference:
            raise V2TeamReportError("evidence reference is empty or invalid")
        if "\\" in reference:
            raise V2TeamReportError(f"evidence reference uses a non-portable separator: {reference}")
        pure = PurePosixPath(reference)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise V2TeamReportError(f"evidence reference escapes the run: {reference}")
        normalized = pure.as_posix()
        path = self.root.joinpath(*pure.parts)
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise V2TeamReportError(
                f"evidence reference resolves outside the run: {reference}"
            ) from exc
        if path.is_symlink() or resolved != path.absolute():
            raise V2TeamReportError(f"evidence symlink is not allowed: {reference}")
        return normalized, path

    def validate_ref(self, reference: str, *, must_exist: bool = True) -> str:
        relative, path = self._relative(reference)
        if self._allowlist is not None and relative not in self._allowlist:
            raise V2TeamReportError(
                f"MANIFEST_REF_CLOSURE_FAILED: artifact is not covered: {relative}"
            )
        if must_exist and (not path.is_file() or path.is_symlink()):
            raise V2TeamReportError(f"declared evidence is missing: {relative}")
        return relative

    def read_bytes(self, reference: str) -> bytes:
        relative = self.validate_ref(reference)
        path = self.root.joinpath(*PurePosixPath(relative).parts)
        try:
            value = path.read_bytes()
        except OSError as exc:
            raise V2TeamReportError(f"cannot read evidence {relative}: {exc}") from exc
        self.read_refs.add(relative)
        return value

    def read_text(self, reference: str) -> str:
        try:
            return self.read_bytes(reference).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise V2TeamReportError(f"evidence is not UTF-8: {reference}") from exc

    def read_json(self, reference: str) -> dict[str, object]:
        try:
            value = json.loads(self.read_text(reference))
        except json.JSONDecodeError as exc:
            raise V2TeamReportError(f"evidence is invalid JSON: {reference}: {exc}") from exc
        if not isinstance(value, dict):
            raise V2TeamReportError(f"evidence is not a JSON object: {reference}")
        return value

    def read_jsonl(self, reference: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(self.read_text(reference).splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise V2TeamReportError(
                    f"evidence is invalid JSONL: {reference}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise V2TeamReportError(
                    f"evidence JSONL record is not an object: {reference}:{line_number}"
                )
            records.append(value)
        return records


def _declared_refs(value: object, *, in_artifacts: bool = False) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            artifact_map = key == "artifacts"
            if isinstance(item, str) and (
                key.endswith("_ref")
                or key in {"llm_ref", "patch_ref", "source_ref", "result_ref"}
                or in_artifacts
            ):
                if "/" in item or item.endswith((".json", ".jsonl", ".md", ".diff", ".cpp", ".h")):
                    refs.add(item)
            refs.update(_declared_refs(item, in_artifacts=artifact_map))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.update(_declared_refs(item, in_artifacts=in_artifacts))
    return refs


def _validate_declared_refs(reader: EvidenceReader, values: Sequence[object]) -> set[str]:
    refs: set[str] = set()
    for value in values:
        refs.update(_declared_refs(value))
    for reference in sorted(refs.difference(_OUTPUT_REFS)):
        reader.validate_ref(reference)
    return refs


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_ledger(reader: EvidenceReader) -> tuple[dict[str, LedgerAction], Mapping[str, object]]:
    events = reader.read_jsonl("budget_ledger.jsonl")
    if not events or events[0].get("state") != "INITIALIZED":
        raise V2TeamReportError("Ledger does not start with INITIALIZED")
    for index, event in enumerate(events):
        if event.get("sequence") != index:
            raise V2TeamReportError("Ledger sequence is not contiguous")
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for event in events[1:]:
        action_id = event.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise V2TeamReportError("Ledger action event lacks action_id")
        grouped.setdefault(action_id, []).append(event)
    actions: dict[str, LedgerAction] = {}
    for action_id in sorted(grouped):
        records = grouped[action_id]
        started = [item for item in records if item.get("state") == "STARTED"]
        terminal = [
            item for item in records if item.get("state") in {"COMPLETED", "AMBIGUOUS"}
        ]
        if len(started) != 1 or len(terminal) != 1 or len(records) != 2:
            raise V2TeamReportError(
                f"Ledger action {action_id} does not have exactly one STARTED/terminal pair"
            )
        start = started[0]
        end = terminal[0]
        kind = start.get("kind")
        if not isinstance(kind, str) or end.get("kind") != kind:
            raise V2TeamReportError(f"Ledger action {action_id} has inconsistent kind")
        actual_cost = _nonnegative_int(end.get("actual_cost"), label="Ledger actual_cost")
        input_tokens = _nonnegative_int(
            end.get("input_tokens", 0), label="Ledger input_tokens"
        )
        output_tokens = _nonnegative_int(
            end.get("output_tokens", 0), label="Ledger output_tokens"
        )
        cached_tokens = _nonnegative_int(
            end.get("cached_input_tokens", 0), label="Ledger cached_input_tokens"
        )
        tokens_used = _nonnegative_int(
            end.get("tokens_used", 0), label="Ledger tokens_used"
        )
        if input_tokens + output_tokens != tokens_used:
            raise V2TeamReportError(
                f"Ledger action {action_id} violates input + output = total Token"
            )
        if cached_tokens > input_tokens:
            raise V2TeamReportError(
                f"Ledger action {action_id} has cached Token larger than input Token"
            )
        elapsed = _finite_number(end.get("elapsed_s"))
        if elapsed is None or elapsed < 0:
            raise V2TeamReportError(f"Ledger action {action_id} has invalid elapsed_s")
        result_ref = end.get("result_ref")
        if end.get("state") == "COMPLETED":
            if not isinstance(result_ref, str):
                raise V2TeamReportError(f"completed Ledger action {action_id} lacks result_ref")
            encoded = reader.read_bytes(result_ref)
            declared_sha = end.get("result_sha256")
            if declared_sha != hashlib.sha256(encoded).hexdigest():
                raise V2TeamReportError(
                    f"Ledger result digest mismatch for action {action_id}"
                )
        elif result_ref is not None:
            raise V2TeamReportError(f"ambiguous Ledger action {action_id} declares result_ref")
        candidate_id = start.get("candidate_id")
        actions[action_id] = LedgerAction(
            action_id=action_id,
            kind=kind,
            candidate_id=candidate_id if isinstance(candidate_id, str) else None,
            state=str(end.get("state")),
            actual_cost=actual_cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            elapsed_s=elapsed,
            started_timestamp=(
                str(start["timestamp"]) if isinstance(start.get("timestamp"), str) else None
            ),
            terminal_timestamp=(
                str(end["timestamp"]) if isinstance(end.get("timestamp"), str) else None
            ),
            result_ref=result_ref if isinstance(result_ref, str) else None,
        )
    starts = [
        parsed
        for action in actions.values()
        if (parsed := _parse_timestamp(action.started_timestamp)) is not None
    ]
    ends = [
        parsed
        for action in actions.values()
        if (parsed := _parse_timestamp(action.terminal_timestamp)) is not None
    ]
    wall_s: float | None = None
    if starts and ends:
        wall_s = max(0.0, (max(ends) - min(starts)).total_seconds())
    totals = {
        "credits": sum(action.actual_cost for action in actions.values()),
        "input_tokens": sum(action.input_tokens for action in actions.values()),
        "output_tokens": sum(action.output_tokens for action in actions.values()),
        "cached_input_tokens": sum(
            action.cached_input_tokens for action in actions.values()
        ),
        "total_tokens": sum(action.total_tokens for action in actions.values()),
        "action_elapsed_sum_s": sum(action.elapsed_s for action in actions.values()),
        "wall_clock_s": wall_s,
        "tool_calls": {
            kind: sum(1 for action in actions.values() if action.kind == kind)
            for kind in ("llm", "csim", "synth", "cosim")
        },
    }
    return actions, totals


def _action_from_ref(reference: object) -> str | None:
    if not isinstance(reference, str):
        return None
    parts = PurePosixPath(reference).parts
    if len(parts) >= 3 and parts[0] in {"actions", "llm_actions"}:
        return parts[1]
    return None


def _action_ids_from_validation(value: object) -> tuple[str, ...]:
    result: list[str] = []
    validation = _mapping(value)
    for stage in _STAGES:
        record = _mapping(validation.get(stage))
        action_id = record.get("action_id")
        if isinstance(action_id, str) and action_id not in result:
            result.append(action_id)
    return tuple(result)


def _read_action_result(
    reader: EvidenceReader,
    record: Mapping[str, object],
    actions: Mapping[str, LedgerAction],
) -> tuple[Mapping[str, object], LedgerAction | None]:
    action_id = record.get("action_id")
    reference = record.get("result_ref")
    if not isinstance(action_id, str):
        action_id = _action_from_ref(reference)
    action = actions.get(action_id) if isinstance(action_id, str) else None
    if isinstance(action_id, str) and action is None:
        raise V2TeamReportError(f"validation declares unknown Ledger action {action_id}")
    if action is not None and isinstance(reference, str) and action.result_ref != reference:
        raise V2TeamReportError(
            f"validation result_ref disagrees with Ledger action {action.action_id}"
        )
    result: Mapping[str, object] = {}
    effective_ref = reference if isinstance(reference, str) else (
        action.result_ref if action is not None else None
    )
    if isinstance(effective_ref, str):
        result = reader.read_json(effective_ref)
        result_action = result.get("action_id")
        if action is not None and result_action not in {None, action.action_id}:
            raise V2TeamReportError(
                f"tool result action_id disagrees for {action.action_id}"
            )
    return result, action


def _metrics_from_result(value: Mapping[str, object]) -> Mapping[str, object]:
    report = value.get("report")
    if not isinstance(report, Mapping):
        return {}
    return {
        key: _redact(report.get(key))
        for key in (
            "estimated_clock_period_ns",
            "latency",
            "interval",
            "resources",
            "available_resources",
            "utilization_percent",
        )
        if key in report
    }


def _candidate_metrics(
    reader: EvidenceReader,
    candidate: Mapping[str, object],
) -> Mapping[str, object]:
    reference = candidate.get("metrics_ref")
    if not isinstance(reference, str):
        return {}
    synth_ref = _mapping(_mapping(candidate.get("validation")).get("synth")).get(
        "result_ref"
    )
    if isinstance(synth_ref, str) and reference != synth_ref:
        raise V2TeamReportError(
            f"Candidate {candidate.get('candidate_id')} metrics_ref is not its Synth result_ref"
        )
    return _metrics_from_result(reader.read_json(reference))


def _candidate_score(
    reader: EvidenceReader,
    candidate: Mapping[str, object],
    *,
    pre_cosim: bool = False,
) -> Mapping[str, object]:
    key = "pre_cosim_score_ref" if pre_cosim else "score_ref"
    reference = candidate.get(key)
    if not isinstance(reference, str):
        return {}
    value = reader.read_json(reference)
    candidate_id = candidate.get("candidate_id")
    if isinstance(candidate_id, str) and value.get("candidate_id") != candidate_id:
        raise V2TeamReportError(
            f"Candidate {candidate_id} {key} binds Candidate {value.get('candidate_id')}"
        )
    return _redact(value)  # type: ignore[return-value]


def _detect_gate_capability(
    optimization: Mapping[str, object],
    raw_rounds: Sequence[Mapping[str, object]],
) -> str:
    policy = optimization.get("exploration_cosim_policy")
    if policy == "official_score_gate":
        return "official_proxy_gate_v1"
    if policy == "ppa_gate":
        return "ppa_gate_v1"
    saw_gate = False
    for round_record in raw_rounds:
        gate = _mapping(round_record.get("cosim_gate"))
        if isinstance(round_record.get("cosim_gate_ref"), str) or gate:
            saw_gate = True
        if (
            gate.get("policy") == "official_score_gate"
            or "candidate_official_score" in gate
            or "incumbent_official_score" in gate
        ):
            return "official_proxy_gate_v1"
    return "ppa_gate_v1" if saw_gate else "legacy_ungated"


def _patch_observation(
    text: str,
    *,
    declared_class: object,
    applied: bool,
    reference: str | None,
) -> Mapping[str, object]:
    files: list[str] = []
    added_pragmas: list[str] = []
    removed_pragmas: list[str] = []
    added_lines = 0
    removed_lines = 0
    hunks = 0
    for line in text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip().split("\t", 1)[0]
            if name != "/dev/null":
                files.append(PurePosixPath(name).name)
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            hunks += 1
            continue
        if line.startswith("+"):
            added_lines += 1
            if "#pragma HLS" in line:
                added_pragmas.append(line[1:].strip())
        elif line.startswith("-"):
            removed_lines += 1
            if "#pragma HLS" in line:
                removed_pragmas.append(line[1:].strip())
    observed_classes: set[str] = set()
    for pragma in added_pragmas:
        upper = pragma.upper()
        if re.search(r"\bPIPELINE\b", upper):
            observed_classes.add("LOOP_PIPELINE")
        if re.search(r"\bUNROLL\b", upper):
            observed_classes.add("LOOP_UNROLL")
        if any(
            token in upper
            for token in ("ARRAY_PARTITION", "ARRAY_RESHAPE", "BIND_STORAGE")
        ):
            observed_classes.add("MEMORY_LAYOUT")
    declared = declared_class if isinstance(declared_class, str) else None
    if observed_classes and declared not in observed_classes:
        semantic = "DECLARED_CLASS_MAY_NOT_MATCH_PATCH"
    elif observed_classes and declared in observed_classes:
        semantic = "DECLARED_CLASS_MATCHES_OBSERVED_DIRECTIVE"
    else:
        semantic = "SEMANTIC_MATCH_UNKNOWN"
    observations = [
        f"{len(set(files))} file(s), {hunks} hunk(s), "
        f"{added_lines + removed_lines} changed diff line(s)"
    ]
    if semantic == "DECLARED_CLASS_MAY_NOT_MATCH_PATCH":
        observations.append(
            "declared class differs from an obvious added HLS directive"
        )
    return {
        "status": "APPLIED" if applied else "MODEL_PROPOSED_NOT_APPLIED",
        "source": "APPLIED_DIFF" if applied else "MODEL_PROPOSAL",
        "reference": reference,
        "changed_files": sorted(set(files)),
        "hunks": hunks,
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "changed_lines": added_lines + removed_lines,
        "non_pragma_changed_lines": max(
            0,
            added_lines + removed_lines - len(added_pragmas) - len(removed_pragmas),
        ),
        "added_pragmas": added_pragmas,
        "removed_pragmas": removed_pragmas,
        "observed_classes": sorted(observed_classes),
        "semantic_check": semantic,
        "observations": observations,
        "full_diff": _sanitize_text(text),
    }


def _empty_patch() -> Mapping[str, object]:
    return {
        "status": "NOT_AVAILABLE",
        "source": "NONE",
        "reference": None,
        "changed_files": [],
        "hunks": 0,
        "added_lines": 0,
        "removed_lines": 0,
        "changed_lines": 0,
        "non_pragma_changed_lines": 0,
        "added_pragmas": [],
        "removed_pragmas": [],
        "observed_classes": [],
        "semantic_check": "SEMANTIC_MATCH_UNKNOWN",
        "observations": [],
        "full_diff": "",
    }


_INVOCATION_REASONS = {
    "baseline": {
        "csim": "建立 public 功能基线",
        "synth": "建立 latency、II、clock 和 resource 基线",
        "cosim": "执行本地严格 baseline RTL 验证",
    },
    "exploration": {
        "csim": "合法 Patch 后执行成本最低的 public 功能回归门",
        "synth": "CSim PASS 后检查可综合性、时钟和性能指标",
        "cosim": "CoSim gate 判定 eligible 后执行 RTL 实测，确认晋升资格",
    },
    "final": {
        "csim": "探索缓存之外执行独立 final CSim closure",
        "synth": "final CSim PASS 后执行独立综合和时钟 closure",
        "cosim": "final Synth/clock PASS 后执行独立 RTL closure",
    },
    "fallback": {
        "csim": "当前 best final 失败后验证下一名已验证 Candidate",
        "synth": "fallback CSim PASS 后重新验证综合和时钟",
        "cosim": "fallback Synth/clock PASS 后重新验证 RTL",
    },
}


def _execution_state(status: str, phase: object) -> str:
    normalized = status.upper()
    phase_value = str(phase or "").upper()
    if "TIMEOUT" in normalized or "TIMEOUT" in phase_value:
        return "EXECUTED_TIMEOUT"
    if normalized == "PASS":
        return "EXECUTED_PASS"
    if normalized in {"FAIL", "FAILED", "TOOL_ERROR", "ERROR"}:
        return "EXECUTED_FAIL"
    if normalized == "AMBIGUOUS":
        return "EXECUTED_AMBIGUOUS"
    return normalized


def _status_from_tool_result(result: Mapping[str, object]) -> str:
    if result.get("ok") is True:
        return "PASS"
    phase = str(result.get("phase", ""))
    if phase == "timeout":
        return "TIMEOUT"
    if phase == "tool_error":
        return "TOOL_ERROR"
    return "FAIL"


def _cosim_state(
    validation: Mapping[str, object],
    clock: Mapping[str, object],
    gate: Mapping[str, object],
    *,
    candidate_exists: bool,
    gate_capability: str,
) -> str:
    if not candidate_exists:
        return "NOT_APPLICABLE_NO_CANDIDATE"
    csim = _mapping(validation.get("csim"))
    synth = _mapping(validation.get("synth"))
    cosim = _mapping(validation.get("cosim"))
    status = str(cosim.get("status", "NOT_RUN")).upper()
    if status not in {"NOT_RUN", "N/A", "NONE", ""}:
        return _execution_state(status, cosim.get("phase"))
    if str(csim.get("status", "NOT_RUN")).upper() != "PASS":
        return "NOT_REACHED_CSIM_FAILED"
    if str(synth.get("status", "NOT_RUN")).upper() != "PASS":
        return "NOT_REACHED_SYNTH_FAILED"
    if clock and clock.get("passed") is not True:
        return "NOT_REACHED_CLOCK_FAILED"
    if gate_capability != "legacy_ungated" and gate.get("eligible") is False:
        return "SKIPPED_BY_GATE"
    return "LEGACY_EVIDENCE_LIMITATION"


def _invocation_reason(
    scope: str,
    stage: str,
    state: str,
    gate: Mapping[str, object],
) -> str:
    """Explain both executed tools and deliberately unexecuted branches."""

    if state.startswith("EXECUTED_"):
        return _INVOCATION_REASONS[scope][stage]
    if state == "SKIPPED_BY_GATE":
        reason = gate.get("reason", "INELIGIBLE")
        return f"未调用：CoSim gate 判定 ineligible（{reason}）"
    if state == "NOT_REACHED_CSIM_FAILED":
        return f"未调用 {stage}：前置 CSim 未通过"
    if state == "NOT_REACHED_SYNTH_FAILED":
        return "未调用 CoSim：前置 Synth 未通过"
    if state == "NOT_REACHED_CLOCK_FAILED":
        return "未调用 CoSim：Synth 时钟约束未通过"
    if state == "NOT_APPLICABLE_NO_CANDIDATE":
        return f"未调用 {stage}：Provider/Patch 阶段未形成 Candidate"
    if state == "LEGACY_EVIDENCE_LIMITATION":
        return "历史证据未记录 CoSim gate 结论，无法可靠还原调用原因"
    return f"未调用 {stage}：{state}"


def _normalize_tools(
    reader: EvidenceReader,
    actions: Mapping[str, LedgerAction],
    validation: Mapping[str, object],
    clock: Mapping[str, object],
    gate: Mapping[str, object],
    *,
    scope: str,
    candidate_id: str | None,
    gate_capability: str,
) -> tuple[Mapping[str, object], ...]:
    tools: list[Mapping[str, object]] = []
    expected_validation_scope = (
        "search_closeout" if scope in {"final", "fallback"} else "exploration"
    )
    cosim_state = _cosim_state(
        validation,
        clock,
        gate,
        candidate_exists=candidate_id is not None,
        gate_capability=gate_capability,
    )
    for stage in _STAGES:
        record = _mapping(validation.get(stage))
        status = str(record.get("status", "NOT_RUN"))
        result: Mapping[str, object] = {}
        action: LedgerAction | None = None
        if isinstance(record.get("action_id"), str) or isinstance(
            record.get("result_ref"), str
        ):
            result, action = _read_action_result(reader, record, actions)
        if action is not None:
            if action.kind != stage:
                raise V2TeamReportError(
                    f"{scope} {stage} evidence binds Ledger kind={action.kind}"
                )
            if candidate_id is not None and action.candidate_id != candidate_id:
                raise V2TeamReportError(
                    f"{scope} {stage} action binds Candidate {action.candidate_id}, "
                    f"expected {candidate_id}"
                )
            record_scope = record.get("validation_scope") or "exploration"
            if record_scope != expected_validation_scope:
                raise V2TeamReportError(
                    f"{scope} {stage} validation_scope={record_scope}, "
                    f"expected {expected_validation_scope}"
                )
        if result:
            expected_result_ref = action.result_ref if action is not None else None
            for field, expected in (
                ("action_id", action.action_id if action is not None else None),
                ("kind", stage),
                ("candidate_id", candidate_id),
                ("result_ref", expected_result_ref),
            ):
                if result.get(field) != expected:
                    raise V2TeamReportError(
                        f"{scope} {stage} result {field}={result.get(field)}, "
                        f"expected {expected}"
                    )
            result_scope = result.get("validation_scope") or "exploration"
            if result_scope != expected_validation_scope:
                raise V2TeamReportError(
                    f"{scope} {stage} result validation_scope={result_scope}, "
                    f"expected {expected_validation_scope}"
                )
            expected_record_fields = {
                "status": _status_from_tool_result(result),
                "phase": result.get("phase"),
                "ok": result.get("ok"),
                "action_id": result.get("action_id"),
                "result_ref": result.get("result_ref"),
                "code_hash": result.get("code_hash"),
                "tool_config_hash": result.get("tool_config_hash"),
                "backend_fingerprint": result.get("backend_fingerprint"),
                "task_fingerprint": result.get("task_fingerprint"),
                "effective_timeout_seconds": result.get(
                    "effective_timeout_seconds"
                ),
            }
            for field, expected in expected_record_fields.items():
                if record.get(field) != expected:
                    raise V2TeamReportError(
                        f"{scope} {stage} validation {field}={record.get(field)}, "
                        f"expected tool result value {expected}"
                    )
        if stage == "cosim" and status.upper() in {"NOT_RUN", "N/A", "NONE", ""}:
            state = cosim_state
        elif status.upper() == "NOT_RUN":
            if stage == "synth":
                state = "NOT_REACHED_CSIM_FAILED"
            else:
                state = "NOT_REACHED"
        else:
            state = _execution_state(status, record.get("phase", result.get("phase")))
        diagnostic = (
            result.get("phase")
            or record.get("phase")
            or result.get("error")
            or record.get("error")
            or status
        )
        tools.append(
            {
                "stage": stage,
                "invocation_reason": _invocation_reason(
                    scope, stage, state, gate
                ),
                "execution_state": state,
                "status": status,
                "phase": result.get("phase", record.get("phase")),
                "diagnostic": _sanitize_text(str(diagnostic)),
                "elapsed_s": action.elapsed_s if action is not None else 0.0,
                "credit_cost": action.actual_cost if action is not None else 0,
                "action_id": action.action_id if action is not None else None,
                "result_ref": action.result_ref if action is not None else None,
                "metrics": _metrics_from_result(result) if stage == "synth" else {},
                "measurement": (
                    _redact(result.get("cosim", {})) if stage == "cosim" else {}
                ),
            }
        )
    if gate_capability != "legacy_ungated" and gate:
        eligible = gate.get("eligible")
        cosim_tool = tools[2]
        executed = str(cosim_tool.get("execution_state", "")).startswith("EXECUTED_")
        if eligible is True and not executed:
            raise V2TeamReportError("eligible CoSim gate has no CoSim action")
        if eligible is False and executed:
            raise V2TeamReportError("ineligible CoSim gate nevertheless has a CoSim action")
    return tuple(tools)


def _tool_by_stage(
    tools: Sequence[Mapping[str, object]], stage: str
) -> Mapping[str, object]:
    return next((item for item in tools if item.get("stage") == stage), {})


def _cosim_latency_max(tools: Sequence[Mapping[str, object]]) -> float | None:
    measurement = _mapping(_tool_by_stage(tools, "cosim").get("measurement"))
    latency = _mapping(measurement.get("latency"))
    return _finite_number(latency.get("max"))


def _score_ref_value(
    reader: EvidenceReader,
    record: Mapping[str, object],
    key: str,
    *,
    expected_candidate_id: str | None = None,
) -> Mapping[str, object]:
    reference = record.get(key)
    if not isinstance(reference, str):
        return {}
    value = reader.read_json(reference)
    if (
        expected_candidate_id is not None
        and value.get("candidate_id") != expected_candidate_id
    ):
        raise V2TeamReportError(
            f"{key} binds Candidate {value.get('candidate_id')}, "
            f"expected {expected_candidate_id}"
        )
    return _redact(value)  # type: ignore[return-value]


def _gate_value(
    reader: EvidenceReader,
    record: Mapping[str, object],
    *,
    expected_candidate_id: str | None = None,
    expected_incumbent_id: str | None = None,
) -> Mapping[str, object]:
    inline = _mapping(record.get("cosim_gate"))
    reference = record.get("cosim_gate_ref")
    if isinstance(reference, str):
        stored = reader.read_json(reference)
        if inline and dict(inline) != stored:
            raise V2TeamReportError("inline CoSim gate disagrees with its evidence ref")
    else:
        stored = dict(inline)
    if (
        stored
        and expected_candidate_id is not None
        and stored.get("candidate_id") != expected_candidate_id
    ):
        raise V2TeamReportError("CoSim gate Candidate binding mismatch")
    if (
        stored
        and expected_incumbent_id is not None
        and stored.get("incumbent_id") != expected_incumbent_id
    ):
        raise V2TeamReportError("CoSim gate incumbent binding mismatch")
    return _redact(stored)  # type: ignore[return-value]


def _group_values(
    actions: Mapping[str, LedgerAction], action_ids: Sequence[str]
) -> Mapping[str, object]:
    unique = tuple(dict.fromkeys(action_ids))
    selected: list[LedgerAction] = []
    for action_id in unique:
        action = actions.get(action_id)
        if action is None:
            raise V2TeamReportError(f"declared action is absent from Ledger: {action_id}")
        selected.append(action)
    return {
        "action_ids": unique,
        "credits": sum(item.actual_cost for item in selected),
        "input_tokens": sum(item.input_tokens for item in selected),
        "output_tokens": sum(item.output_tokens for item in selected),
        "cached_input_tokens": sum(item.cached_input_tokens for item in selected),
        "total_tokens": sum(item.total_tokens for item in selected),
        "elapsed_s": sum(item.elapsed_s for item in selected),
        "llm_credits": sum(
            item.actual_cost for item in selected if item.kind == "llm"
        ),
        "tool_credits": sum(
            item.actual_cost for item in selected if item.kind != "llm"
        ),
    }


def _round_exit(decision: str, gate: Mapping[str, object], stop_reason: object) -> str:
    if decision == "PROVIDER_REJECTED":
        return "PROVIDER_REJECTED"
    if decision == "PATCH_REJECTED":
        return "PATCH_REJECTED"
    if decision == "PROMOTED":
        return "PROMOTED"
    reason = str(stop_reason or "")
    if reason.startswith("COSIM_"):
        return "COSIM_REJECTED"
    if decision == "REJECTED_NOT_BETTER" and gate.get("eligible") is False:
        return "SKIPPED_COSIM_NOT_BETTER"
    if decision == "REJECTED_NOT_BETTER":
        return "REJECTED_NOT_BETTER"
    return "REJECTED_VALIDATION" if decision == "REJECTED_VALIDATION" else decision


def _metric_delta(
    before: Mapping[str, object], after: Mapping[str, object]
) -> Mapping[str, object]:
    def nested(group: str, name: str) -> float | None:
        return _finite_number(_mapping(after.get(group)).get(name))

    def previous(group: str, name: str) -> float | None:
        return _finite_number(_mapping(before.get(group)).get(name))

    values: dict[str, object] = {}
    for label, group, name in (
        ("latency_worst", "latency", "worst"),
        ("interval_max", "interval", "max"),
    ):
        old = previous(group, name)
        new = nested(group, name)
        values[label] = {
            "before": old,
            "after": new,
            "delta": new - old if old is not None and new is not None else None,
        }
    old_clock = _finite_number(before.get("estimated_clock_period_ns"))
    new_clock = _finite_number(after.get("estimated_clock_period_ns"))
    values["clock_period_ns"] = {
        "before": old_clock,
        "after": new_clock,
        "delta": (
            new_clock - old_clock
            if old_clock is not None and new_clock is not None
            else None
        ),
    }
    return values


def _candidate_tree(
    registry: Mapping[str, object], rounds: Sequence[RoundReview]
) -> Mapping[str, object]:
    candidates = _mapping(registry.get("candidates"))
    nodes: list[Mapping[str, object]] = []
    for candidate_id in sorted(candidates):
        record = _mapping(candidates[candidate_id])
        nodes.append(
            {
                "id": candidate_id,
                "parent": record.get("parent_id"),
                "kind": record.get("kind"),
                "round": record.get("round"),
                "status": record.get("status"),
                "selection_status": record.get("selection_status"),
                "rejection_reason": record.get("rejection_reason"),
            }
        )
    attempts = tuple(
        {
            "id": f"attempt_round_{item.round_index:03d}",
            "parent": item.parent_candidate_id,
            "round": item.round_index,
            "status": item.decision,
            "rejection_reason": item.rejection_reason,
        }
        for item in rounds
        if item.candidate_id is None
    )
    return {
        "baseline_candidate_id": registry.get("baseline_candidate_id", "candidate_000"),
        "best_candidate_id": registry.get("best_candidate_id"),
        "final_candidate_id": registry.get("final_candidate_id"),
        "nodes": tuple(nodes),
        "attempts": attempts,
    }


def collect_v2_team_report_data(
    run_dir: str | Path, *, mode: str = "automatic"
) -> V2TeamReportData:
    """Collect and normalize one durable V2 optimize run without side effects."""

    reader = EvidenceReader(run_dir, mode=mode)
    core: dict[str, object] = {}
    for reference in _CORE_FILES:
        core[reference] = (
            reader.read_jsonl(reference)
            if reference.endswith(".jsonl")
            else reader.read_json(reference)
        )
    task = _mapping(core["task_spec.json"])
    run_config = _mapping(core["run_config.json"])
    optimization = _mapping(core["optimization_config.json"])
    workflow = _mapping(core["workflow_result.json"])
    result = _mapping(core["v2_result.json"])
    registry = _mapping(core["candidate_registry.json"])
    trace = _sequence(core["trace.jsonl"])
    if result.get("workflow") != "V2_CANDIDATE_PPA":
        raise V2TeamReportError("team report only supports V2_CANDIDATE_PPA runs")
    if task.get("task_id") != result.get("task_id"):
        raise V2TeamReportError("task_spec and v2_result task_id disagree")
    if registry.get("task_id") not in {None, result.get("task_id")}:
        raise V2TeamReportError("candidate Registry and v2_result task_id disagree")

    actions, ledger_totals = _read_ledger(reader)
    embedded_rounds = [
        _mapping(item) for item in _sequence(result.get("rounds"))
    ]
    raw_rounds: list[Mapping[str, object]] = []
    for expected_index, embedded in enumerate(embedded_rounds, 1):
        index = embedded.get("round_index")
        if index != expected_index:
            raise V2TeamReportError("V2 round indexes are not contiguous")
        reference = embedded.get("result_ref")
        if not isinstance(reference, str):
            reference = f"optimization_rounds/round_{expected_index:03d}.json"
        stored = reader.read_json(reference)
        comparable = dict(embedded)
        comparable.pop("result_ref", None)
        if comparable != stored:
            raise V2TeamReportError(
                f"embedded round {expected_index} disagrees with {reference}"
            )
        raw_rounds.append({**stored, "result_ref": reference})
    manifest_rounds = (
        sorted(
            reference
            for reference in reader.allowlist
            if reference.startswith("optimization_rounds/")
            and reference.endswith(".json")
        )
        if reader.allowlist is not None
        else []
    )
    if manifest_rounds and len(manifest_rounds) != len(raw_rounds):
        raise V2TeamReportError("Manifest round count disagrees with v2_result")
    gate_capability = _detect_gate_capability(optimization, raw_rounds)
    candidates = _mapping(registry.get("candidates"))
    baseline_id = str(result.get("baseline_candidate_id", "candidate_000"))
    baseline_candidate = _mapping(candidates.get(baseline_id))
    if not baseline_candidate:
        raise V2TeamReportError("baseline Candidate is absent from Registry")
    metrics_cache: dict[str, Mapping[str, object]] = {
        baseline_id: _candidate_metrics(reader, baseline_candidate)
    }

    reviews: list[RoundReview] = []
    round_action_ids: list[tuple[str, ...]] = []
    current_best = baseline_id
    no_improvement = 0
    legacy_history: list[tuple[str, str, str]] = []
    for position, record in enumerate(raw_rounds):
        round_index = int(record["round_index"])
        parent_id = record.get("parent_candidate_id")
        if not isinstance(parent_id, str) or parent_id != current_best:
            raise V2TeamReportError(
                f"round {round_index} parent does not match reconstructed best"
            )
        candidate_id = record.get("candidate_id")
        if candidate_id is not None and not isinstance(candidate_id, str):
            raise V2TeamReportError(f"round {round_index} candidate_id is invalid")
        candidate = _mapping(candidates.get(candidate_id)) if candidate_id else {}
        if candidate_id and not candidate:
            raise V2TeamReportError(
                f"round {round_index} Candidate is absent from Registry"
            )
        if candidate and candidate.get("parent_id") != parent_id:
            raise V2TeamReportError(f"round {round_index} Candidate parent mismatch")
        provider_ref = record.get("provider_ref")
        request_ref = record.get("request_ref")
        if not isinstance(provider_ref, str) or not isinstance(request_ref, str):
            raise V2TeamReportError(f"round {round_index} lacks Provider evidence refs")
        provider_result = reader.read_json(provider_ref)
        raw_prompt = reader.read_json(request_ref)
        prompt = _redact(raw_prompt)
        if not isinstance(prompt, Mapping):  # pragma: no cover - redactor invariant
            prompt = {}
        llm_action_id = _action_from_ref(provider_ref)
        if llm_action_id is None or llm_action_id not in actions:
            raise V2TeamReportError(f"round {round_index} Provider action is absent from Ledger")
        llm_action = actions[llm_action_id]
        if llm_action.kind != "llm":
            raise V2TeamReportError(f"round {round_index} Provider action is not kind=llm")
        if llm_action.result_ref != provider_ref:
            raise V2TeamReportError(
                f"round {round_index} Provider ref is not the Ledger result_ref"
            )
        if llm_action.candidate_id != parent_id:
            raise V2TeamReportError(
                f"round {round_index} Provider action parent Candidate mismatch"
            )
        provider_parts = PurePosixPath(provider_ref).parts
        request_parts = PurePosixPath(request_ref).parts
        if provider_parts != ("llm_actions", llm_action_id, "result.json"):
            raise V2TeamReportError(f"round {round_index} Provider result path is invalid")
        if request_parts != ("llm_actions", llm_action_id, "request.json"):
            raise V2TeamReportError(f"round {round_index} Provider request binding mismatch")
        if raw_prompt.get("action_id") != llm_action_id:
            raise V2TeamReportError(f"round {round_index} Provider request action_id mismatch")
        selector = _mapping(record.get("selector"))
        selected_class = record.get("optimization_class")
        if selector.get("optimization_class") not in {None, selected_class}:
            raise V2TeamReportError(f"round {round_index} Selector class mismatch")
        prompt_context = _mapping(raw_prompt.get("context"))
        for field, expected in (
            ("round_index", round_index),
            ("parent_candidate_id", parent_id),
            ("allowed_optimization_class", selected_class),
        ):
            if field in prompt_context and prompt_context.get(field) != expected:
                raise V2TeamReportError(
                    f"round {round_index} Provider request {field} mismatch"
                )
        selection = _mapping(record.get("selection_context"))
        if selection:
            selection_context: Mapping[str, object] = {
                **selection,
                "evidence_state": "PERSISTED",
            }
        else:
            digest = str(selector.get("metrics_digest", ""))
            prior = [item for item in legacy_history if item[1] == digest]
            selection_context = {
                "metrics_digest": digest,
                "attempted_same_metrics": [item[0] for item in prior],
                "failed_same_metrics": [
                    item[0] for item in prior if item[2] != "PROMOTED"
                ],
                "available_classes": "N/A",
                "evidence_state": "LEGACY_EVIDENCE_LIMITATION",
            }
        gate = _gate_value(
            reader,
            record,
            expected_candidate_id=candidate_id,
            expected_incumbent_id=parent_id,
        )
        validation = _mapping(candidate.get("validation"))
        clock = _mapping(candidate.get("clock_constraint"))
        tools = _normalize_tools(
            reader,
            actions,
            validation,
            clock,
            gate,
            scope="exploration",
            candidate_id=candidate_id,
            gate_capability=gate_capability,
        )
        if candidate_id:
            metrics_cache[candidate_id] = _candidate_metrics(reader, candidate)
        patch: Mapping[str, object] = _empty_patch()
        patch_ref = candidate.get("patch_ref")
        if isinstance(patch_ref, str):
            patch = _patch_observation(
                reader.read_text(patch_ref),
                declared_class=provider_result.get("change_class", selected_class),
                applied=True,
                reference=patch_ref,
            )
        elif provider_result.get("ok") is True and isinstance(
            provider_result.get("patch"), str
        ):
            patch = _patch_observation(
                str(provider_result["patch"]),
                declared_class=provider_result.get("change_class", selected_class),
                applied=False,
                reference=provider_ref,
            )
        for ref_key in (
            "pre_cosim_score_ref",
            "cosim_gate_ref",
            "score_ref",
            "comparison_ref",
        ):
            round_ref = record.get(ref_key)
            candidate_ref = candidate.get(ref_key)
            if isinstance(round_ref, str) and candidate_ref != round_ref:
                raise V2TeamReportError(
                    f"round {round_index} {ref_key} disagrees with Candidate Registry"
                )
            if isinstance(candidate_ref, str) and round_ref != candidate_ref:
                raise V2TeamReportError(
                    f"round {round_index} omits Candidate Registry {ref_key}"
                )
        score = _score_ref_value(
            reader, record, "score_ref", expected_candidate_id=candidate_id
        )
        pre_score = _score_ref_value(
            reader,
            record,
            "pre_cosim_score_ref",
            expected_candidate_id=candidate_id,
        )
        comparison = _score_ref_value(reader, record, "comparison_ref")
        inline_comparison = _mapping(record.get("comparison"))
        if comparison and inline_comparison and comparison != inline_comparison:
            raise V2TeamReportError(f"round {round_index} comparison ref mismatch")
        if comparison:
            candidate_key = _sequence(comparison.get("candidate_key"))
            incumbent_key = _sequence(comparison.get("incumbent_key"))
            if not candidate_key or candidate_key[-1] != candidate_id:
                raise V2TeamReportError(
                    f"round {round_index} comparison Candidate binding mismatch"
                )
            if not incumbent_key or incumbent_key[-1] != parent_id:
                raise V2TeamReportError(
                    f"round {round_index} comparison incumbent binding mismatch"
                )
            if comparison.get("winner") not in {candidate_id, parent_id}:
                raise V2TeamReportError(
                    f"round {round_index} comparison winner binding mismatch"
                )
        decision = str(record.get("decision", "UNKNOWN"))
        best_before = current_best
        if decision == "PROMOTED":
            if not candidate_id:
                raise V2TeamReportError(f"round {round_index} promotes no Candidate")
            current_best = candidate_id
            no_improvement = 0
        else:
            no_improvement += 1
        stop_reason = record.get("stop_reason", record.get("error"))
        if stop_reason is None and decision != "PROMOTED":
            stop_reason = candidate.get("rejection_reason") or gate.get("reason")
        exit_branch = _round_exit(decision, gate, stop_reason)
        next_parent = (
            raw_rounds[position + 1].get("parent_candidate_id")
            if position + 1 < len(raw_rounds)
            else current_best
        )
        entry_reason = (
            "verified baseline starts exploration"
            if round_index == 1
            else f"previous round ended as {reviews[-1].decision}; continue from current best"
        )
        tool_ids = tuple(
            str(item["action_id"])
            for item in tools
            if isinstance(item.get("action_id"), str)
        )
        ids = (llm_action_id, *tool_ids)
        round_action_ids.append(ids)
        group = _group_values(actions, ids)
        before_metrics = metrics_cache.get(parent_id, {})
        after_metrics = metrics_cache.get(candidate_id, {}) if candidate_id else {}
        evidence_refs = {
            str(record["result_ref"]),
            provider_ref,
            request_ref,
            *(
                str(value)
                for key in (
                    "pre_cosim_score_ref",
                    "cosim_gate_ref",
                    "score_ref",
                    "comparison_ref",
                )
                if isinstance((value := record.get(key)), str)
            ),
            *(
                str(item["result_ref"])
                for item in tools
                if isinstance(item.get("result_ref"), str)
            ),
        }
        if isinstance(patch.get("reference"), str):
            evidence_refs.add(str(patch["reference"]))
        model_claim = {
            "fact_level": "PROVIDER_CLAIM",
            "ok": provider_result.get("ok"),
            "provider": provider_result.get("provider"),
            "model": provider_result.get("model"),
            "revision": provider_result.get("revision"),
            "hypothesis": _redact(provider_result.get("hypothesis")),
            "expected_effect": _redact(provider_result.get("expected_effect")),
            "risk": _redact(provider_result.get("risk")),
            "declared_change_class": provider_result.get("change_class"),
            "required_validation": _redact(provider_result.get("required_validation", [])),
            "error": _redact(provider_result.get("error")),
            "request_id": provider_result.get("request_id"),
        }
        reviews.append(
            RoundReview(
                round_index=round_index,
                parent_candidate_id=parent_id,
                candidate_id=candidate_id,
                best_before=best_before,
                best_after=current_best,
                branch={
                    "fact_level": "RULE_DECISION",
                    "entry_reason": entry_reason,
                    **selection_context,
                    "selected_class": selected_class,
                    "selector_reason": selector.get("bottleneck"),
                    "exit_branch": exit_branch,
                    "next_parent": next_parent,
                    "next_round_reason": (
                        "continue exploration from reconstructed best"
                        if position + 1 < len(raw_rounds)
                        else f"exploration stops: {result.get('exploration_stop_reason')}"
                    ),
                },
                selector={"fact_level": "RULE_DECISION", **selector},
                model_claim=model_claim,
                patch={"fact_level": "PATCH_FACT", **patch},
                tools=tools,
                cosim_gate={
                    "fact_level": "MACHINE_DECISION",
                    "state": _tool_by_stage(tools, "cosim").get("execution_state"),
                    "capability": gate_capability,
                    "policy": gate.get("policy", optimization.get("exploration_cosim_policy")),
                    "reason": gate.get("reason"),
                    "eligible": gate.get("eligible"),
                    "candidate_proxy": gate.get("candidate_official_score"),
                    "incumbent_proxy": gate.get("incumbent_official_score"),
                    "candidate_ppa": gate.get("candidate_ppa_cost"),
                    "incumbent_ppa": gate.get("incumbent_ppa_cost"),
                    "provisional_note": (
                        "provisional gate-only estimate; for requires_cosim tasks it "
                        "assumes CoSim PASS solely to decide whether to run CoSim"
                        if pre_score
                        else None
                    ),
                },
                score_and_comparison={
                    "pre_cosim_score": pre_score,
                    "score": score,
                    "comparison": comparison or inline_comparison,
                    "metrics_before": before_metrics,
                    "metrics_after": after_metrics,
                    "measured_delta": _metric_delta(before_metrics, after_metrics),
                },
                decision=decision,
                rejection_reason=str(stop_reason) if stop_reason is not None else None,
                no_improvement_after=no_improvement,
                tokens={
                    "input": group["input_tokens"],
                    "output": group["output_tokens"],
                    "cached_input_subset": group["cached_input_tokens"],
                    "total": group["total_tokens"],
                },
                cost={
                    "llm_credits": group["llm_credits"],
                    "tool_credits": group["tool_credits"],
                    "total_credits": group["credits"],
                },
                timing={
                    "model_elapsed_s": llm_action.elapsed_s,
                    "tool_elapsed_s": sum(
                        float(item.get("elapsed_s", 0.0)) for item in tools
                    ),
                    "action_elapsed_sum_s": group["elapsed_s"],
                },
                lesson="",
                next_action="",
                evidence_refs=tuple(sorted(evidence_refs)),
                prompt=prompt,
            )
        )
        legacy_history.append((str(selected_class), str(selector.get("metrics_digest", "")), decision))

    if result.get("stop_reason") == "BASELINE_NOT_VERIFIED" and not raw_rounds:
        if result.get("best_candidate_id") is not None or registry.get(
            "best_candidate_id"
        ) is not None:
            raise V2TeamReportError(
                "failed baseline unexpectedly declares a best Candidate"
            )
    elif result.get("best_candidate_id") != current_best or registry.get(
        "best_candidate_id"
    ) != current_best:
        raise V2TeamReportError("reconstructed best disagrees with terminal evidence")
    if isinstance(result.get("no_improvement_rounds"), int) and result.get(
        "no_improvement_rounds"
    ) != no_improvement:
        raise V2TeamReportError("reconstructed no-improvement counter disagrees")

    baseline_validation = _mapping(workflow.get("validation")) or _mapping(
        baseline_candidate.get("validation")
    )
    baseline_clock = _mapping(workflow.get("clock_constraint"))
    baseline_tools = _normalize_tools(
        reader,
        actions,
        baseline_validation,
        baseline_clock,
        {},
        scope="baseline",
        candidate_id=baseline_id,
        gate_capability="legacy_ungated",
    )
    baseline_ids = tuple(
        str(item["action_id"])
        for item in baseline_tools
        if isinstance(item.get("action_id"), str)
    )
    baseline_group = _group_values(actions, baseline_ids)

    final_attempt_values: list[Mapping[str, object]] = []
    final_groups: list[tuple[str, Mapping[str, object]]] = []
    for attempt_index, raw_attempt in enumerate(_sequence(result.get("final_attempts"))):
        attempt = _mapping(raw_attempt)
        validation = _mapping(attempt.get("validation"))
        clock = _mapping(attempt.get("clock_constraint"))
        attempt_candidate_id = attempt.get("candidate_id")
        if not isinstance(attempt_candidate_id, str):
            raise V2TeamReportError("final attempt lacks candidate_id")
        scope = "final" if attempt_index == 0 else "fallback"
        tools = _normalize_tools(
            reader,
            actions,
            validation,
            clock,
            {},
            scope=scope,
            candidate_id=attempt_candidate_id,
            gate_capability="legacy_ungated",
        )
        ids = tuple(
            str(item["action_id"])
            for item in tools
            if isinstance(item.get("action_id"), str)
        )
        group = _group_values(actions, ids)
        final_groups.append((scope, group))
        final_attempt_values.append(
            {
                "attempt_index": attempt_index + 1,
                "scope": scope,
                "candidate_id": attempt.get("candidate_id"),
                "status": attempt.get("status"),
                "stop_reason": attempt.get("stop_reason"),
                "clock_constraint": clock,
                "tools": tools,
                "cost": group,
            }
        )

    assigned: set[str] = set(baseline_ids)
    for ids in round_action_ids:
        assigned.update(ids)
    for _, group in final_groups:
        assigned.update(str(item) for item in _sequence(group.get("action_ids")))
    unassigned = sorted(set(actions).difference(assigned))
    baseline_credits = int(baseline_group["credits"])
    rounds_credits = sum(int(item.cost["total_credits"]) for item in reviews)
    final_credits = sum(
        int(group["credits"]) for scope, group in final_groups if scope == "final"
    )
    fallback_credits = sum(
        int(group["credits"]) for scope, group in final_groups if scope == "fallback"
    )
    decomposed = baseline_credits + rounds_credits + final_credits + fallback_credits
    result_budget = _mapping(result.get("budget"))
    declared_credits = result_budget.get("credits_used")
    mismatches: list[str] = []
    if decomposed != ledger_totals["credits"]:
        mismatches.append("phase credit decomposition does not equal Ledger total")
    if isinstance(declared_credits, int) and declared_credits != ledger_totals["credits"]:
        mismatches.append("v2_result budget credits do not equal Ledger total")
    for key, ledger_key in (
        ("input_tokens_used", "input_tokens"),
        ("output_tokens_used", "output_tokens"),
        ("cached_input_tokens_used", "cached_input_tokens"),
        ("tokens_used", "total_tokens"),
    ):
        if isinstance(result_budget.get(key), int) and result_budget[key] != ledger_totals[ledger_key]:
            mismatches.append(f"v2_result {key} does not equal Ledger total")
    if unassigned:
        mismatches.append("Ledger contains unassigned actions: " + ", ".join(unassigned))

    final_id = result.get("final_candidate_id")
    final_candidate = _mapping(candidates.get(final_id)) if isinstance(final_id, str) else {}
    selected_metrics = _candidate_metrics(reader, final_candidate) if final_candidate else {}
    successful_final_attempt = next(
        (
            attempt
            for attempt in final_attempt_values
            if attempt.get("candidate_id") == final_id
            and attempt.get("status") == "DONE"
        ),
        {},
    )
    successful_final_tools = tuple(
        _mapping(item)
        for item in _sequence(_mapping(successful_final_attempt).get("tools"))
    )
    final_metrics = _mapping(
        _tool_by_stage(successful_final_tools, "synth").get("metrics")
    )
    if result.get("status") == "DONE":
        if not successful_final_attempt:
            raise V2TeamReportError(
                "DONE V2 run lacks a successful final validation attempt"
            )
        if not final_metrics:
            raise V2TeamReportError(
                "DONE V2 run lacks final closure Synth metrics"
            )
    elif successful_final_attempt:
        raise V2TeamReportError(
            "failed V2 run unexpectedly contains a successful final validation attempt"
        )
    baseline_score = _candidate_score(reader, baseline_candidate)
    final_score = _candidate_score(reader, final_candidate) if final_candidate else {}
    baseline_latency = _finite_number(
        _mapping(metrics_cache[baseline_id].get("latency")).get("worst")
    )
    final_latency = _finite_number(_mapping(final_metrics.get("latency")).get("worst"))
    speedup = (
        baseline_latency / final_latency
        if baseline_latency is not None and final_latency not in {None, 0.0}
        else None
    )
    baseline_cosim_latency = _cosim_latency_max(baseline_tools)
    final_cosim_latency = _cosim_latency_max(successful_final_tools)
    cosim_speedup = (
        baseline_cosim_latency / final_cosim_latency
        if baseline_cosim_latency is not None
        and final_cosim_latency not in {None, 0.0}
        else None
    )
    round_cosim_states = [
        str(item.cosim_gate.get("state")) for item in reviews
    ]
    cosim_cost = _mapping(_mapping(run_config.get("budget")).get("costs")).get(
        "cosim", 0
    )
    skipped = round_cosim_states.count("SKIPPED_BY_GATE")

    _validate_declared_refs(
        reader,
        [task, run_config, optimization, workflow, result, registry, trace, *raw_rounds],
    )
    task_budget = task.get("task_budget")
    local_limit = _mapping(run_config.get("budget")).get("credit_limit")
    override = (
        isinstance(task_budget, int)
        and isinstance(local_limit, int)
        and task_budget != local_limit
    )
    integrity = {
        "mode": mode,
        "state": "GENERATED_PRE_MANIFEST" if mode == "automatic" else "MANIFEST_VERIFIED",
        "manifest_sha256": reader.manifest_sha256,
        "manifest_note": (
            "Manifest 将在本报告之后封存；本报告不声称已验证尚未生成的 Manifest"
            if mode == "automatic"
            else "既有 Manifest 已在读取任何 run-local 证据前验证"
        ),
        "legacy_limitations": gate_capability == "legacy_ungated"
        or any(not _mapping(item.get("selection_context")) for item in raw_rounds),
    }
    evidence = set(reader.read_refs)
    evidence.update(_CORE_FILES)
    return V2TeamReportData(
        schema_version=REPORT_SCHEMA_VERSION,
        task={
            "task_id": task.get("task_id"),
            "task_type": task.get("task_type"),
            "difficulty": task.get("difficulty"),
            "requires_cosim": task.get("requires_cosim"),
            "part": task.get("part", _mapping(run_config.get("tool")).get("part")),
            "clock_ns": task.get("clock_ns", _mapping(run_config.get("tool")).get("clock_ns")),
            "task_budget": task_budget,
            "local_credit_limit": local_limit,
            "budget_label": "LOCAL_STRICT_BUDGET_OVERRIDE" if override else "TASK_BUDGET",
            "provenance": task.get("provenance", "NOT_PERSISTED_IN_RUN"),
        },
        policy={
            "gate_capability": gate_capability,
            "exploration_cosim_policy": optimization.get("exploration_cosim_policy"),
            "scoring": _redact(optimization.get("scoring", {})),
            "max_rounds": optimization.get("max_rounds"),
            "max_no_improvement_rounds": optimization.get("max_no_improvement_rounds"),
            "search_closeout_reserve_credits": optimization.get("search_closeout_reserve_credits"),
            "official_proxy_is_final_score": False,
            "result_workflow": result.get("workflow"),
            "run_config_workflow": run_config.get("workflow"),
        },
        run_outcome={
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "exploration_stop_reason": result.get("exploration_stop_reason"),
            "baseline_candidate_id": baseline_id,
            "best_candidate_id": current_best,
            "final_candidate_id": final_id,
            "round_count": len(reviews),
            "no_improvement_rounds": no_improvement,
            "baseline_official_proxy": baseline_score.get("official_score"),
            "final_official_proxy": final_score.get("official_score"),
            "baseline_ppa_cost": baseline_score.get("ppa_cost"),
            "final_ppa_cost": final_score.get("ppa_cost"),
            "baseline_latency_worst": baseline_latency,
            "final_latency_worst": final_latency,
            "latency_speedup": speedup,
            "baseline_cosim_latency_max": baseline_cosim_latency,
            "final_cosim_latency_max": final_cosim_latency,
            "cosim_latency_speedup": cosim_speedup,
        },
        baseline={
            "candidate_id": baseline_id,
            "status": workflow.get("status", baseline_candidate.get("status")),
            "stop_reason": workflow.get("stop_reason"),
            "tools": baseline_tools,
            "clock_constraint": baseline_clock,
            "metrics": metrics_cache[baseline_id],
            "score": baseline_score,
            "cost": baseline_group,
        },
        rounds=tuple(reviews),
        candidate_tree=_candidate_tree(registry, reviews),
        final_validation={
            "status": result.get("status"),
            "final_candidate_id": final_id,
            "attempts": tuple(final_attempt_values),
            "clock_constraint": _mapping(result.get("final_clock_constraint")),
            "metrics": final_metrics,
            "selected_exploration_metrics": selected_metrics,
            "score": final_score,
        },
        fallback=_mapping(result.get("fallback")) or None,
        accounting={
            "ledger": ledger_totals,
            "baseline_credits": baseline_credits,
            "rounds_credits": rounds_credits,
            "final_credits": final_credits,
            "fallback_credits": fallback_credits,
            "decomposed_credits": decomposed,
            "declared_credits": declared_credits,
            "conserved": not mismatches,
            "mismatches": tuple(mismatches),
            "unassigned_action_ids": tuple(unassigned),
        },
        cosim_summary={
            "gate_capability": gate_capability,
            "exploration_states": tuple(round_cosim_states),
            "exploration_executed": sum(
                1 for state in round_cosim_states if state.startswith("EXECUTED_")
            ),
            "gate_skipped": skipped,
            "gate_saved_credits": skipped * int(cosim_cost or 0),
            "ledger_cosim_calls": _mapping(ledger_totals.get("tool_calls")).get("cosim", 0),
            "not_reached": sum(
                1 for state in round_cosim_states if state.startswith("NOT_REACHED_")
            ),
        },
        findings=(),
        next_actions=(),
        evidence_index=tuple(sorted(evidence.difference({"experimental_report.md"}))),
        integrity=integrity,
    )


def _round_advice(round_review: RoundReview) -> tuple[str, str]:
    state = str(round_review.cosim_gate.get("state", ""))
    decision = round_review.decision
    if decision == "PROMOTED":
        return (
            "Candidate 通过验证且严格改善，best 已更新。",
            "用新 best 的实测 metrics 重新识别瓶颈，不沿用旧 metrics 假设。",
        )
    if decision == "PROVIDER_REJECTED":
        return (
            "Provider 调用产生了成本和 Token，但没有形成合法提案。",
            "检查响应 schema；预算允许时改用其他优化类或重试 Provider。",
        )
    if decision == "PATCH_REJECTED":
        return (
            "Provider 返回了提案，但 Patch policy/应用阶段拒绝，未产生 Candidate。",
            "根据 policy error 缩小 Patch 或修正目标文件与 hunk。",
        )
    if state == "NOT_REACHED_CSIM_FAILED":
        return (
            "Patch 未通过 public 功能回归，不能得出性能结论。",
            "回滚并先修正功能语义；不要运行 Synth/CoSim。",
        )
    if state == "NOT_REACHED_SYNTH_FAILED":
        return (
            "CSim 通过但综合失败，性能预测没有得到硬件证据支持。",
            "根据结构化 HLS diagnostic 修正 pragma 或结构冲突。",
        )
    if state == "NOT_REACHED_CLOCK_FAILED":
        return (
            "综合完成但时钟约束失败，因此没有进入 CoSim。",
            "优先处理组合路径或流水结构，再考虑 RTL 验证。",
        )
    if state == "SKIPPED_BY_GATE":
        return (
            "代理分/PPA gate 未改善，Harness 主动跳过了一次 CoSim。",
            "保留 incumbent，并选择尚未尝试的优化类。",
        )
    if state in {"EXECUTED_FAIL", "EXECUTED_TIMEOUT"}:
        return (
            "CoSim 未通过，pre-CoSim 临时分不能作为 Candidate 最终成绩。",
            "优先调查 RTL、stream/FIFO 或 interface 行为。",
        )
    if decision == "REJECTED_NOT_BETTER":
        return (
            "Candidate 完成验证但未严格优于 incumbent。",
            "保留 incumbent，并改用同一 metrics 下尚未尝试的优化类。",
        )
    return (
        "该尝试未晋升，best 保持不变。",
        "依据失败阶段和剩余优化类继续；不要把 Provider 假设当成实测根因。",
    )


def analyze_v2_team_report_data(data: V2TeamReportData) -> V2TeamReportData:
    """Add deterministic findings and recommendations; never call a Provider."""

    findings: list[Mapping[str, object]] = [
        {
            "level": "RULE_DECISION",
            "code": "V2_SCOPE",
            "message": "本 run 是 V2 optimize：baseline 必须先验证，V2 不负责自动修复。",
        }
    ]
    next_actions: list[Mapping[str, object]] = []
    rounds: list[RoundReview] = []
    promoted = 0
    for item in data.rounds:
        lesson, next_action = _round_advice(item)
        rounds.append(replace(item, lesson=lesson, next_action=next_action))
        if item.decision == "PROMOTED":
            promoted += 1
        semantic = item.patch.get("semantic_check")
        if semantic == "DECLARED_CLASS_MAY_NOT_MATCH_PATCH":
            findings.append(
                {
                    "level": "PATCH_FACT",
                    "code": str(semantic),
                    "round_index": item.round_index,
                    "message": (
                        f"Round {item.round_index}: Provider 声明的优化类与实际新增 HLS directive 可能不一致。"
                    ),
                }
            )
        expected = item.model_claim.get("expected_effect")
        after = _mapping(item.score_and_comparison.get("metrics_after"))
        findings.append(
            {
                "level": "TOOL_MEASUREMENT" if after else "PROVIDER_CLAIM",
                "code": "PREDICTION_VS_MEASUREMENT",
                "round_index": item.round_index,
                "message": (
                    f"Round {item.round_index}: Provider 预期已与 Vitis 实测并列；"
                    + ("存在 Synth metrics。" if after else "无完整 Synth metrics，不能验证预测。")
                ),
                "provider_expected_effect": expected,
                "measured_delta": item.score_and_comparison.get("measured_delta"),
            }
        )
    findings.append(
        {
            "level": "MACHINE_DECISION",
            "code": "BEST_TRANSITIONS",
            "message": f"{len(data.rounds)} 轮探索中 {promoted} 轮晋升；其余分支保留 incumbent。",
        }
    )
    if data.cosim_summary.get("gate_skipped"):
        findings.append(
            {
                "level": "MACHINE_DECISION",
                "code": "COSIM_GATE_SAVING",
                "message": (
                    f"CoSim gate 跳过 {data.cosim_summary.get('gate_skipped')} 次，"
                    f"按配置节省 {data.cosim_summary.get('gate_saved_credits')} credits。"
                ),
            }
        )
    if data.accounting.get("conserved") is not True:
        findings.append(
            {
                "level": "REPORT_INFERENCE",
                "code": "ACCOUNTING_MISMATCH",
                "message": "Ledger 与阶段分解不守恒；报告没有用预期成本填补差额。",
            }
        )
        next_actions.append(
            {
                "level": "REPORT_INFERENCE",
                "priority": 1,
                "action": "先修复 Ledger/阶段归属不一致，再比较 Provider 或工具效率。",
            }
        )
    stop = data.run_outcome.get("exploration_stop_reason")
    stop_advice = {
        "NO_IMPROVEMENT_LIMIT": "停止是连续无改善保护；复盘 Selector 和评分粒度。",
        "SEARCH_CLOSEOUT_RESERVE_REACHED": (
            "停止是主动保护 Agent 搜索收尾储备，不代表 Provider 失败。"
        ),
        "TOKEN_RESERVE_REACHED": "缩短 Prompt 或增加明确的 Token 预算后再探索。",
        "MAX_ROUNDS": "检查是否仍有未尝试分支，再决定是否提高轮数。",
        "MAX_OPTIMIZATION_ROUNDS": "已达到本次配置的探索轮数；根据剩余分支与预算决定是否继续。",
    }.get(str(stop), "依据最终 stop reason 复盘探索边界。")
    next_actions.append(
        {"level": "REPORT_INFERENCE", "priority": 2, "action": stop_advice}
    )
    if data.run_outcome.get("stop_reason") == "BASELINE_NOT_VERIFIED":
        next_actions.append(
            {
                "level": "REPORT_INFERENCE",
                "priority": 1,
                "action": "先定位 baseline 的首个失败阶段；功能/综合问题回到 V1 修复，尚未进入 V2 final/fallback。",
            }
        )
    elif data.run_outcome.get("status") != "DONE":
        next_actions.append(
            {
                "level": "REPORT_INFERENCE",
                "priority": 1,
                "action": "检查 final/fallback 验证失败；无可用 fallback 时进行人工审查。",
            }
        )
    elif promoted:
        next_actions.append(
            {
                "level": "REPORT_INFERENCE",
                "priority": 3,
                "action": "从 final best 的新 metrics 重新识别下一瓶颈。",
            }
        )
    return replace(
        data,
        rounds=tuple(rounds),
        findings=tuple(findings),
        next_actions=tuple(next_actions),
    )


def _fmt(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (dict, list, tuple)):
        return _sanitize_text(
            json.dumps(_plain(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        )
    return _sanitize_text(str(value))


def _cell(value: object) -> str:
    return _fmt(value).replace("|", "\\|").replace("\n", " ")


def _code(value: object) -> str:
    return "`" + _fmt(value).replace("`", "\\`") + "`"


def _fenced(language: str, value: str) -> list[str]:
    longest = max((len(item) for item in re.findall(r"`+", value)), default=0)
    fence = "`" * max(4, longest + 1)
    return [f"{fence}{language}", value, fence]


def _evidence_link(reference: str, prefix: str) -> str:
    target = f"{prefix.rstrip('/')}/{reference}" if prefix else reference
    return f"[{reference}]({target})"


def _render_candidate_tree(tree: Mapping[str, object]) -> list[str]:
    nodes = [_mapping(item) for item in _sequence(tree.get("nodes"))]
    attempts = [_mapping(item) for item in _sequence(tree.get("attempts"))]
    children: dict[str | None, list[Mapping[str, object]]] = {}
    for node in nodes:
        parent = node.get("parent") if isinstance(node.get("parent"), str) else None
        children.setdefault(parent, []).append(node)
    attempts_by_parent: dict[str, list[Mapping[str, object]]] = {}
    for attempt in attempts:
        parent = attempt.get("parent")
        if isinstance(parent, str):
            attempts_by_parent.setdefault(parent, []).append(attempt)
    rendered: list[str] = []

    def visit(node: Mapping[str, object], prefix: str, last: bool) -> None:
        candidate_id = str(node.get("id"))
        branch = "└─" if last else "├─"
        rendered.append(
            f"{prefix}{branch} {candidate_id} [{_fmt(node.get('status'))}]"
            + (f" reason={_fmt(node.get('rejection_reason'))}" if node.get("rejection_reason") else "")
        )
        descendants = list(children.get(candidate_id, [])) + list(
            attempts_by_parent.get(candidate_id, [])
        )
        child_prefix = prefix + ("   " if last else "│  ")
        for index, child in enumerate(descendants):
            if child in attempts:
                is_last = index == len(descendants) - 1
                rendered.append(
                    f"{child_prefix}{'└─' if is_last else '├─'} "
                    f"{child.get('id')} [{child.get('status')}] "
                    f"reason={_fmt(child.get('rejection_reason'))}"
                )
            else:
                visit(child, child_prefix, index == len(descendants) - 1)

    roots = children.get(None, [])
    for index, root in enumerate(roots):
        visit(root, "", index == len(roots) - 1)
    return rendered or ["(no Candidate tree evidence)"]


def render_v2_team_report(
    data: V2TeamReportData, *, evidence_prefix: str = ""
) -> str:
    """Render deterministic Chinese-first Markdown from normalized evidence."""

    if not data.findings and not data.next_actions:
        data = analyze_v2_team_report_data(data)
    outcome = data.run_outcome
    ledger = _mapping(data.accounting.get("ledger"))
    scoring = _mapping(data.policy.get("scoring"))
    official_score = _mapping(scoring.get("official_score"))
    scoring_role = (
        "official public proxy 为第一排序键，`PPA tie-break` 仅在代理分相同"
        if official_score.get("enabled") is True
        else "本 run 未启用 official public proxy；PPA 是主要比较信号，`PPA tie-break` 不适用"
    )
    final_candidate_id = outcome.get("final_candidate_id")
    successful_final_attempt = next(
        (
            _mapping(item)
            for item in _sequence(data.final_validation.get("attempts"))
            if _mapping(item).get("candidate_id") == final_candidate_id
            and _mapping(item).get("status") == "DONE"
        ),
        {},
    )
    successful_final_tools = tuple(
        _mapping(item)
        for item in _sequence(successful_final_attempt.get("tools"))
    )
    final_cosim_measurement = _mapping(
        _tool_by_stage(successful_final_tools, "cosim").get("measurement")
    )
    report_trust_note = (
        "本自动报告随后由 run Manifest 封存。"
        if data.integrity.get("mode") == "automatic"
        else "本文件是 run 外部复盘产物，不在源 run Manifest 内；源证据已先验验签，报告自身需单独保存哈希。"
    )
    lines = [
        "# V2 逐轮团队复盘报告",
        "",
        "> 本报告由持久化证据确定性生成；Proposal Provider（LLM 或明确标注的本地规则）只提出 Patch，CSim/Synth/CoSim 均由 Harness 编排。",
        f"> 信任边界：{report_trust_note}",
        "",
        "## 30 秒结论",
        "",
        f"- 状态 / 停止原因：{_code(outcome.get('status'))} / {_code(outcome.get('stop_reason'))}",
        f"- Baseline / Best / Final：{_code(outcome.get('baseline_candidate_id'))} → {_code(outcome.get('best_candidate_id'))} → {_code(outcome.get('final_candidate_id'))}",
        f"- 探索轮数 / 连续无改善：{_code(outcome.get('round_count'))} / {_code(outcome.get('no_improvement_rounds'))}",
        f"- Synth worst-case latency（cycles）baseline → final closure / speedup：{_code(outcome.get('baseline_latency_worst'))} → {_code(outcome.get('final_latency_worst'))} / {_code(outcome.get('latency_speedup'))}",
        f"- CoSim max latency（cycles）baseline → final closure / speedup：{_code(outcome.get('baseline_cosim_latency_max'))} → {_code(outcome.get('final_cosim_latency_max'))} / {_code(outcome.get('cosim_latency_speedup'))}",
        f"- official public proxy baseline → final：{_code(outcome.get('baseline_official_proxy'))} → {_code(outcome.get('final_official_proxy'))}（公开评分代理仅是搜索信号，不是官方最终分）",
        f"- 排序角色：{scoring_role}；本地信号都不替代官方 hidden scorer。",
        f"- Token input / output / cached-subset / total：{_code(ledger.get('input_tokens'))} / {_code(ledger.get('output_tokens'))} / {_code(ledger.get('cached_input_tokens'))} / {_code(ledger.get('total_tokens'))}",
        f"- Credits：{_code(ledger.get('credits'))}；阶段守恒：{_code(data.accounting.get('conserved'))}",
        f"- CoSim：Ledger 调用 {_code(data.cosim_summary.get('ledger_cosim_calls'))} 次；gate 跳过 {_code(data.cosim_summary.get('gate_skipped'))} 次；节省 {_code(data.cosim_summary.get('gate_saved_credits'))} credits",
        f"- Gate 能力：{_code(data.policy.get('gate_capability'))}；证据完整性：{_code(data.integrity.get('state'))}",
        "",
        "### 三个关键发现",
        "",
    ]
    for finding in data.findings[:3]:
        lines.append(
            f"- [{_fmt(finding.get('level'))}] {_fmt(finding.get('message'))}"
        )
    lines.extend(["", "### 下一步", ""])
    for action in data.next_actions[:3]:
        lines.append(f"- P{_fmt(action.get('priority'))}：{_fmt(action.get('action'))}")

    lines.extend(
        [
            "",
            "## 职责与任务预判",
            "",
            "- 当前阶段：`V2 optimize`；进入条件是 baseline 已通过本地严格验证。",
            f"- Task：{_code(data.task.get('task_id'))}；type={_code(data.task.get('task_type'))}；difficulty={_code(data.task.get('difficulty'))}；requires_cosim={_code(data.task.get('requires_cosim'))}",
            f"- Task provenance：{_code(data.task.get('provenance'))}。若为 `NOT_PERSISTED_IN_RUN`，仅凭 task_id 不能证明它是官方题，需在 run 外核对题库哈希。",
            f"- Target：part={_code(data.task.get('part'))}；clock={_code(data.task.get('clock_ns'))} ns。",
            f"- 预算：task={_code(data.task.get('task_budget'))}；local={_code(data.task.get('local_credit_limit'))}；label={_code(data.task.get('budget_label'))}。",
            f"- Workflow identity：v2_result={_code(data.policy.get('result_workflow'))}；run_config={_code(data.policy.get('run_config_workflow'))}。`V0_DETERMINISTIC` 在这里表示复用的 baseline executor，不是整体 V2 终态身份。",
            f"- 完整性：{_fmt(data.integrity.get('manifest_note'))}",
            "",
            "## V2 主控制流",
            "",
            "```mermaid",
            "flowchart TD",
            '    A["Verified baseline"] --> B["Selector chooses branch"]',
            '    B --> C["Provider proposes one Patch"]',
            '    C --> D{"Patch policy pass?"}',
            '    D -- "No" --> I["Record result and keep or update best"]',
            '    D -- "Yes" --> E["CSim, Synth and clock gates"]',
            '    E -- "Fail" --> I',
            '    E -- "Pass" --> F{"CoSim gate"}',
            '    F -- "Skip" --> I',
            '    F -- "Run" --> G["CoSim"]',
            '    G --> H["Score and compare"]',
            '    H --> I',
            '    I --> J{"Continue exploration?"}',
            '    J -- "Yes" --> B',
            '    J -- "No" --> K["Independent final validation"]',
            "```",
            "",
            "纯文本：`baseline -> selector -> Provider -> Patch -> CSim -> Synth/clock -> CoSim gate -> optional CoSim -> compare -> promote/reject -> repeat -> final closure`",
            "",
            "## Baseline 数据流",
            "",
            "| 工具 | Harness 调用原因 | 状态 | 耗时(s) | Credits | 指标/诊断 |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for tool in _sequence(data.baseline.get("tools")):
        value = _mapping(tool)
        measurement = value.get("measurement") or value.get("diagnostic")
        lines.append(
            f"| {_cell(value.get('stage'))} | {_cell(value.get('invocation_reason'))} | {_cell(value.get('execution_state'))} | {_cell(value.get('elapsed_s'))} | {_cell(value.get('credit_cost'))} | {_cell(measurement)} |"
        )
    lines.extend(
        [
            "",
            f"Baseline metrics：{_code(data.baseline.get('metrics'))}",
            "",
            "说明：这里的 `latency` 与 `interval.max` 来自 Synth；`interval.max` 是 top-level transaction interval，不等于某个循环的 `PipelineII`。CoSim latency 单独来自 RTL 仿真结果。",
            "",
            "## 全局轮次时间线",
            "",
            "| Round | Parent → Candidate | Why/Class | CSim / Synth / CoSim | Gate | Decision | Token | Credits | Best after |",
            "|---:|---|---|---|---|---|---:|---:|---|",
        ]
    )
    for item in data.rounds:
        states = " / ".join(
            str(tool.get("execution_state")) for tool in item.tools
        )
        lines.append(
            f"| {item.round_index} | {_cell(item.parent_candidate_id)} → {_cell(item.candidate_id)} | {_cell(item.branch.get('selector_reason'))} / {_cell(item.branch.get('selected_class'))} | {_cell(states)} | {_cell(item.cosim_gate.get('reason'))} | {_cell(item.decision)} | {_cell(item.tokens.get('total'))} | {_cell(item.cost.get('total_credits'))} | {_cell(item.best_after)} |"
        )

    lines.extend(["", "## 逐轮详细卡片", ""])
    for item in data.rounds:
        lines.extend(
            [
                f"### Round {item.round_index} — {item.decision} — best {_code(item.best_before)} → {_code(item.best_after)}",
                "",
                "#### Why / branch",
                "",
                f"- 进入原因：{_fmt(item.branch.get('entry_reason'))}",
                f"- Selector：class={_code(item.branch.get('selected_class'))}；bottleneck={_code(item.branch.get('selector_reason'))}。这是规则判断，不是已证明的物理根因。",
                f"- 同 metrics 已尝试 / 失败 / 可选：{_code(item.branch.get('attempted_same_metrics'))} / {_code(item.branch.get('failed_same_metrics'))} / {_code(item.branch.get('available_classes'))}",
                f"- 分支出口 / 下一 parent：{_code(item.branch.get('exit_branch'))} / {_code(item.branch.get('next_parent'))}",
                "",
                "#### Provider claim（若 Provider 是 LLM，则为模型声明；不是 Vitis 实测）",
                "",
                f"- Provider / model / revision：{_code(item.model_claim.get('provider'))} / {_code(item.model_claim.get('model'))} / {_code(item.model_claim.get('revision'))}",
                f"- Hypothesis：{_fmt(item.model_claim.get('hypothesis'))}",
                f"- Expected effect：{_fmt(item.model_claim.get('expected_effect'))}",
                f"- Risk / declared class：{_fmt(item.model_claim.get('risk'))} / {_code(item.model_claim.get('declared_change_class'))}",
                f"- Token input/output/cached-subset/total：{_code(item.tokens.get('input'))}/{_code(item.tokens.get('output'))}/{_code(item.tokens.get('cached_input_subset'))}/{_code(item.tokens.get('total'))}；Provider 耗时 {_code(item.timing.get('model_elapsed_s'))} s。",
                "",
                "#### Actual Patch",
                "",
                f"- 状态 / 文件 / hunks / changed lines：{_code(item.patch.get('status'))} / {_code(item.patch.get('changed_files'))} / {_code(item.patch.get('hunks'))} / {_code(item.patch.get('changed_lines'))}",
                f"- 新增/删除 pragma：{_code(item.patch.get('added_pragmas'))} / {_code(item.patch.get('removed_pragmas'))}",
                f"- 声明与观察：{_code(item.patch.get('semantic_check'))}",
                "",
                "#### Harness tools（由 Harness 编排，不是 Provider 直接调用）",
                "",
                "| Stage | 调用/跳过原因 | 状态 | Diagnostic | 耗时(s) | Credits |",
                "|---|---|---|---|---:|---:|",
            ]
        )
        for tool in item.tools:
            diagnostic = tool.get("measurement") or tool.get("diagnostic")
            lines.append(
                f"| {_cell(tool.get('stage'))} | {_cell(tool.get('invocation_reason'))} | {_cell(tool.get('execution_state'))} | {_cell(diagnostic)} | {_cell(tool.get('elapsed_s'))} | {_cell(tool.get('credit_cost'))} |"
            )
        lines.extend(
            [
                "",
                "#### Gate、实测、比较与转移",
                "",
                f"- CoSim：state={_code(item.cosim_gate.get('state'))}；capability={_code(item.cosim_gate.get('capability'))}；eligible={_code(item.cosim_gate.get('eligible'))}；reason={_code(item.cosim_gate.get('reason'))}。",
                f"- Gate proxy Candidate/Best：{_code(item.cosim_gate.get('candidate_proxy'))} / {_code(item.cosim_gate.get('incumbent_proxy'))}；PPA Candidate/Best：{_code(item.cosim_gate.get('candidate_ppa'))} / {_code(item.cosim_gate.get('incumbent_ppa'))}。",
                f"- Gate 注记：{_fmt(item.cosim_gate.get('provisional_note'))}",
                f"- Vitis Synth measured delta（其中 interval_max 是 top-level transaction interval，不是 loop PipelineII）：{_code(item.score_and_comparison.get('measured_delta'))}",
                f"- Score / comparison：{_code(item.score_and_comparison.get('score'))} / {_code(item.score_and_comparison.get('comparison'))}",
                f"- Decision / rejection / no-improvement：{_code(item.decision)} / {_code(item.rejection_reason)} / {_code(item.no_improvement_after)}",
                f"- 本轮 Credits / action elapsed sum：{_code(item.cost.get('total_credits'))} / {_code(item.timing.get('action_elapsed_sum_s'))} s（action sum 不是 wall time）。",
                f"- Lesson：{_fmt(item.lesson)}",
                f"- Next：{_fmt(item.next_action)}",
                "",
                "Evidence：" + ", ".join(
                    _evidence_link(ref, evidence_prefix) for ref in item.evidence_refs
                ),
                "",
            ]
        )

    lines.extend(["## Candidate / attempt tree", "", "```text"])
    lines.extend(_render_candidate_tree(data.candidate_tree))
    lines.extend(
        [
            "```",
            "",
            "Registry 是 Candidate 最终动态状态的权威来源；`candidate.json` 仅是物化快照。没有 Candidate 的 Provider/Patch failure 仍显示为 attempt stub。",
            "",
            "## CoSim 专项复盘",
            "",
            f"- 能力：{_code(data.cosim_summary.get('gate_capability'))}",
            f"- 每轮状态：{_code(data.cosim_summary.get('exploration_states'))}",
            f"- 探索执行 / gate 跳过 / 未到达：{_code(data.cosim_summary.get('exploration_executed'))} / {_code(data.cosim_summary.get('gate_skipped'))} / {_code(data.cosim_summary.get('not_reached'))}",
            f"- 只有 `SKIPPED_BY_GATE` 计 gate savings：{_code(data.cosim_summary.get('gate_saved_credits'))} credits。",
            "",
            "## Final closure 与 fallback",
            "",
            "| Attempt | Scope | Candidate | Status | Stop | CSim / Synth / CoSim | CoSim measurement | Credits |",
            "|---:|---|---|---|---|---|---|---:|",
        ]
    )
    for attempt in _sequence(data.final_validation.get("attempts")):
        value = _mapping(attempt)
        states = " / ".join(
            str(_mapping(tool).get("execution_state"))
            for tool in _sequence(value.get("tools"))
        )
        cosim_measurement = _mapping(
            _tool_by_stage(
                tuple(
                    _mapping(tool) for tool in _sequence(value.get("tools"))
                ),
                "cosim",
            ).get("measurement")
        )
        lines.append(
            f"| {_cell(value.get('attempt_index'))} | {_cell(value.get('scope'))} | {_cell(value.get('candidate_id'))} | {_cell(value.get('status'))} | {_cell(value.get('stop_reason'))} | {_cell(states)} | {_cell(cosim_measurement)} | {_cell(_mapping(value.get('cost')).get('credits'))} |"
        )
    lines.extend(
        [
            "",
            f"- Final closure Synth metrics：{_code(data.final_validation.get('metrics'))}",
            f"- Final closure CoSim measurement：{_code(final_cosim_measurement)}",
            f"- Selected Candidate exploration metrics（用于当时选择）：{_code(data.final_validation.get('selected_exploration_metrics'))}",
            f"- Selected Candidate score（探索阶段持久化证据）：{_code(data.final_validation.get('score'))}",
            "- Final closure 是独立重验；上面的 final latency 取 closure Synth，不能用探索期缓存指标冒充。",
            "",
            f"Fallback：{_code(data.fallback)}",
            "",
            "## 预算与数据流守恒",
            "",
            "| Phase | Credits |",
            "|---|---:|",
            f"| Baseline | {_cell(data.accounting.get('baseline_credits'))} |",
            f"| Rounds | {_cell(data.accounting.get('rounds_credits'))} |",
            f"| Final | {_cell(data.accounting.get('final_credits'))} |",
            f"| Fallback | {_cell(data.accounting.get('fallback_credits'))} |",
            f"| **分解合计** | **{_cell(data.accounting.get('decomposed_credits'))}** |",
            f"| **Ledger** | **{_cell(ledger.get('credits'))}** |",
            "",
            f"- 守恒：{_code(data.accounting.get('conserved'))}；mismatches={_code(data.accounting.get('mismatches'))}。",
            f"- Action elapsed sum / run wall-clock range：{_code(ledger.get('action_elapsed_sum_s'))} / {_code(ledger.get('wall_clock_s'))} s。",
            "",
            "## 预测与实测偏差",
            "",
            "| Round | Provider expected effect | Vitis Synth measured delta | 是否有实测 |",
            "|---:|---|---|---|",
        ]
    )
    for item in data.rounds:
        measured = item.score_and_comparison.get("measured_delta")
        has_measurement = bool(_mapping(item.score_and_comparison.get("metrics_after")))
        lines.append(
            f"| {item.round_index} | {_cell(item.model_claim.get('expected_effect'))} | {_cell(measured)} | {_cell(has_measurement)} |"
        )
    lines.extend(["", "## 团队下一步建议", ""])
    for action in data.next_actions:
        lines.append(
            f"- [{_fmt(action.get('level'))}] P{_fmt(action.get('priority'))}：{_fmt(action.get('action'))}"
        )
    lines.extend(
        [
            "",
            "## 当前 V2 与官方参考的简短差异",
            "",
            "- Agent 只看 public 输入；官方 hidden grader 在 Agent 运行后独立执行。",
            "- `public_proxy_v1` 只是搜索信号，不是官方最终分。",
            "- 官方 grader 仅在 `requires_cosim=true` 时执行 hidden CoSim；本地 V2 对所有 final 执行 public CoSim。",
            "- 本地 baseline/final 完整验证计入 Ledger；官方 hidden grading 在 Agent 预算外。",
            "- 历史 `ppa_gate_v1` 或 `legacy_ungated` run 按持久化能力显示，不用当前默认配置改写历史。",
            "- 当前 run 未持久化题目来源标签；official/local 身份需在 run 外以题库文件哈希核验。",
            "- `run_config.workflow=V0_DETERMINISTIC` 是共享 baseline executor 的历史命名；终态以 `v2_result.workflow=V2_CANDIDATE_PPA` 为准。",
            "- Manifest 顶层 provider/model 可能跟随 final Candidate；逐轮 Provider 身份以绑定到 Ledger 的 request/result 为准。",
            "- run 外离线报告不属于源 Manifest；源证据完整性与报告文件自身哈希是两条独立信任链。",
            "",
            "## 审计附录：Prompt、Patch 与证据",
            "",
        ]
    )
    for item in data.rounds:
        lines.extend([f"### Round {item.round_index} Prompt（已脱敏）", ""])
        prompt_text = json.dumps(
            _plain(item.prompt), indent=2, sort_keys=True, ensure_ascii=False
        )
        lines.extend(_fenced("json", _sanitize_text(prompt_text)))
        lines.extend(["", f"### Round {item.round_index} Patch", ""])
        diff = str(item.patch.get("full_diff", ""))
        lines.extend(_fenced("diff", diff if diff else "# N/A — no Patch was available"))
        lines.append("")
    lines.extend(["### Evidence index", ""])
    for reference in data.evidence_index:
        lines.append(f"- {_evidence_link(reference, evidence_prefix)}")
    lines.extend(
        [
            "",
            f"Integrity：{_code(data.integrity)}",
            "",
        ]
    )
    rendered = "\n".join(lines)
    return _sanitize_text(rendered)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise V2TeamReportError(f"cannot atomically write report: {exc}") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def write_v2_team_report(
    run_dir: str | Path,
    *,
    mode: str = "automatic",
    output_path: str | Path | None = None,
) -> Path:
    """Collect, analyze, render and atomically write a V2 team report."""

    if mode not in _MODES:
        raise V2TeamReportError(f"unsupported report mode: {mode}")
    run_root = Path(run_dir).resolve()
    if mode == "automatic":
        destination = (
            run_root / "experimental_report.md"
            if output_path is None
            else Path(output_path).resolve()
        )
        if destination != run_root / "experimental_report.md":
            raise V2TeamReportError(
                "automatic report output must be run_dir/experimental_report.md"
            )
        prefix = ""
    else:
        if output_path is None:
            raise V2TeamReportError("offline report requires an explicit output_path")
        destination = Path(output_path).resolve()
        try:
            destination.relative_to(run_root)
        except ValueError:
            pass
        else:
            raise V2TeamReportError("offline report output must be outside run_dir")
        prefix = Path(os.path.relpath(run_root, destination.parent)).as_posix()
    data = analyze_v2_team_report_data(
        collect_v2_team_report_data(run_root, mode=mode)
    )
    rendered = render_v2_team_report(data, evidence_prefix=prefix)
    _atomic_write_text(destination, rendered)
    return destination


__all__ = [
    "EvidenceReader",
    "RoundReview",
    "V2TeamReportData",
    "V2TeamReportError",
    "analyze_v2_team_report_data",
    "collect_v2_team_report_data",
    "render_v2_team_report",
    "write_v2_team_report",
]
