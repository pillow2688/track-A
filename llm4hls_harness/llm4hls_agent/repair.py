"""V1 minimal repair loop with constrained patches and isolated candidates.

The module deliberately keeps the model boundary small: a provider may return
one unified diff, but it cannot write files, run tools, or select the final
candidate.  All materialization and validation remains deterministic and is
audited through the V0 ToolServer and BudgetLedger.
"""

from __future__ import annotations

import hashlib
import difflib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from .budget import BudgetError, BudgetExceeded, BudgetLedger, BudgetLedgerError
from .candidate import CandidateManager
from .task import PublicTask
from .top_interface_guard import TopInterfaceGuard, TopInterfaceGuardResult
from .tools import ToolBackend
from .validation import validate_candidate
from .workflow import (
    RunArtifactError,
    RunConfig,
    _RunLock,
    _append_trace,
    _atomic_json,
    _sha256,
    run_v0,
)


class V1Error(RuntimeError):
    """Base class for V1 repair errors."""


class PatchValidationError(V1Error, ValueError):
    """Raised when a model patch violates the constrained patch contract."""

    def __init__(
        self,
        message: str,
        *,
        interface_guard: TopInterfaceGuardResult | None = None,
        failure_evidence: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.interface_guard = interface_guard
        self.failure_evidence = (
            dict(failure_evidence) if failure_evidence is not None else None
        )


class RepairProviderError(V1Error):
    """Raised when a repair provider returns unusable output."""

    def __init__(
        self,
        message: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        duration_seconds: float = 0.0,
        request_id: str | None = None,
        response_excerpt: str | None = None,
        finish_reason: str | None = None,
        output_truncated: bool = False,
        truncation_reason: str | None = None,
        usage_complete: bool = True,
    ) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_input_tokens = cached_input_tokens
        self.duration_seconds = duration_seconds
        self.request_id = request_id
        self.response_excerpt = response_excerpt
        self.finish_reason = finish_reason
        self.output_truncated = bool(output_truncated)
        self.truncation_reason = truncation_reason
        self.usage_complete = bool(usage_complete)


_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)
_LINE_REFERENCE = re.compile(r"(?:^|[/\\])([^/\\:]+\.cpp):(\d+)")


def _hunk_validation_error(
    *,
    error_type: str,
    kernel_name: str,
    hunk_index: int,
    hunk_header: str,
    declared_old_count: int,
    actual_old_count: int,
    declared_new_count: int,
    actual_new_count: int,
    declared_old_start: int,
    declared_new_start: int,
    expected_new_start: int | None = None,
) -> PatchValidationError:
    """Build one bounded, actionable unified-diff hunk diagnostic."""

    coordinate_header, separator, _suffix = hunk_header.partition(" @@")
    bounded_header = (
        f"{coordinate_header}{separator}" if separator else hunk_header[:512]
    )
    evidence: dict[str, object] = {
        "schema_version": "v3.patch-hunk-failure-evidence.v1",
        "error_type": error_type,
        "file": kernel_name,
        "hunk_index": hunk_index,
        "hunk_header": bounded_header,
        "declared_old_start": declared_old_start,
        "declared_new_start": declared_new_start,
        "declared_old_count": declared_old_count,
        "actual_old_count": actual_old_count,
        "declared_new_count": declared_new_count,
        "actual_new_count": actual_new_count,
        "guidance": (
            "regenerate the unified diff with corrected hunk coordinates"
        ),
    }
    if expected_new_start is not None:
        evidence["expected_new_start"] = expected_new_start
    fields = [
        f"file={kernel_name}",
        f"hunk={hunk_index}",
        f"header={bounded_header}",
        f"declared_old_count={declared_old_count}",
        f"actual_old_count={actual_old_count}",
        f"declared_new_count={declared_new_count}",
        f"actual_new_count={actual_new_count}",
    ]
    if expected_new_start is not None:
        fields.extend(
            [
                f"declared_new_start={declared_new_start}",
                f"expected_new_start={expected_new_start}",
            ]
        )
    message = (
        f"{error_type}: "
        + " ".join(fields)
        + "; regenerate the unified diff with corrected hunk coordinates"
    )
    evidence["message"] = message
    return PatchValidationError(message, failure_evidence=evidence)


@dataclass(frozen=True)
class PatchLimits:
    """Safety limits applied before a patch can create a candidate."""

    max_changed_lines: int = 80
    max_hunks: int = 8
    allow_full_file_replacement: bool = False

    def __post_init__(self) -> None:
        if self.max_changed_lines <= 0 or self.max_hunks <= 0:
            raise ValueError("patch limits must be positive")


def task_patch_limits(task: PublicTask, base: PatchLimits) -> PatchLimits:
    """Return the smallest safe patch policy for the public task capability.

    Ordinary repair/optimization tasks retain V3's intentionally small patch
    budget.  A declared public generation/stub task is different: its kernel
    body may legitimately need to be replaced.  The exception is deliberately
    narrow: the existing kernel-only path checks and TopInterfaceGuard still
    run for every patch.
    """

    if not task.generation_required:
        return base
    return PatchLimits(
        max_changed_lines=max(base.max_changed_lines, 1600),
        max_hunks=max(base.max_hunks, 48),
        allow_full_file_replacement=True,
    )


@dataclass(frozen=True)
class PatchProposal:
    """A provider response; the provider cannot perform side effects."""

    patch: str
    provider: str = "scripted"
    model: str = "offline"
    revision: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    request_id: str | None = None
    duration_seconds: float = 0.0
    hypothesis: str | None = None
    change_class: str | None = None
    expected_effect: str | None = None
    risk: str | None = None
    required_validation: tuple[str, ...] = ("csim", "synth", "cosim")
    finish_reason: str | None = None
    output_truncated: bool = False
    truncation_reason: str | None = None
    provider_parameter_name: str | None = None
    requested_max_output_tokens: int | None = None
    effective_max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.patch, str) or not self.patch.strip():
            raise ValueError("patch must be a non-empty string")
        if not self.provider or not self.model:
            raise ValueError("provider and model must not be empty")
        if self.input_tokens < 0 or self.output_tokens < 0 or self.cached_input_tokens < 0:
            raise ValueError("token counts must be non-negative")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("provider duration must be finite and non-negative")
        if any(stage not in {"csim", "synth", "cosim"} for stage in self.required_validation):
            raise ValueError("required validation contains an unsupported stage")
        if not isinstance(self.output_truncated, bool):
            raise ValueError("output_truncated must be boolean")
        for name in ("requested_max_output_tokens", "effective_max_output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or null")
        if self.output_truncated and not self.truncation_reason:
            raise ValueError("truncated provider output requires a reason")

    @property
    def tokens_used(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PatchProposal":
        """Parse the pure proposal object used by versioned Planner outputs."""

        expected = {
            "patch",
            "provider",
            "model",
            "revision",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "request_id",
            "duration_seconds",
            "hypothesis",
            "change_class",
            "expected_effect",
            "risk",
            "required_validation",
            "finish_reason",
            "output_truncated",
            "truncation_reason",
            "provider_parameter_name",
            "requested_max_output_tokens",
            "effective_max_output_tokens",
        }
        legacy = expected.difference(
            {
                "finish_reason",
                "output_truncated",
                "truncation_reason",
                "provider_parameter_name",
                "requested_max_output_tokens",
                "effective_max_output_tokens",
            }
        )
        if frozenset(value) not in {frozenset(expected), frozenset(legacy)}:
            missing = sorted(expected.difference(value))
            extra = sorted(set(value).difference(expected))
            raise ValueError(
                f"proposal fields mismatch; missing={missing}, extra={extra}"
            )

        def required_text(name: str) -> str:
            item = value[name]
            if not isinstance(item, str) or not item:
                raise ValueError(f"proposal {name} must be a non-empty string")
            return item

        def optional_text(name: str) -> str | None:
            item = value[name]
            if item is not None and not isinstance(item, str):
                raise ValueError(f"proposal {name} must be a string or null")
            return item

        def token_count(name: str) -> int:
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"proposal {name} must be a non-negative integer")
            return item

        duration = value["duration_seconds"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
        ):
            raise ValueError(
                "proposal duration_seconds must be a finite non-negative number"
            )
        validations = value["required_validation"]
        if not isinstance(validations, (list, tuple)) or any(
            not isinstance(item, str) for item in validations
        ):
            raise ValueError("proposal required_validation must be a string list")
        if "output_truncated" in value and not isinstance(
            value["output_truncated"], bool
        ):
            raise ValueError("proposal output_truncated must be boolean")
        return cls(
            patch=required_text("patch"),
            provider=required_text("provider"),
            model=required_text("model"),
            revision=optional_text("revision"),
            input_tokens=token_count("input_tokens"),
            output_tokens=token_count("output_tokens"),
            cached_input_tokens=token_count("cached_input_tokens"),
            request_id=optional_text("request_id"),
            duration_seconds=float(duration),
            hypothesis=optional_text("hypothesis"),
            change_class=optional_text("change_class"),
            expected_effect=optional_text("expected_effect"),
            risk=optional_text("risk"),
            required_validation=tuple(validations),
            finish_reason=(
                optional_text("finish_reason") if "finish_reason" in value else None
            ),
            output_truncated=(
                value["output_truncated"]
                if isinstance(value.get("output_truncated"), bool)
                else False
            ),
            truncation_reason=(
                optional_text("truncation_reason")
                if "truncation_reason" in value
                else None
            ),
            provider_parameter_name=(
                optional_text("provider_parameter_name")
                if "provider_parameter_name" in value
                else None
            ),
            requested_max_output_tokens=(
                token_count("requested_max_output_tokens")
                if value.get("requested_max_output_tokens") is not None
                else None
            ),
            effective_max_output_tokens=(
                token_count("effective_max_output_tokens")
                if value.get("effective_max_output_tokens") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class RepairContext:
    """Small, localized context supplied to a repair provider."""

    task_id: str
    candidate_id: str
    stage: str
    phase: str
    diagnostic_code: str
    summary: str
    evidence: tuple[str, ...]
    source_excerpt: str
    remaining_tokens: int
    remaining_credits: int | None
    top: str
    kernel_name: str
    part: str
    clock_ns: float
    initial_condition: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"evidence": list(self.evidence)}


class RepairProvider(Protocol):
    """Provider boundary for one constrained repair proposal."""

    def propose_patch(self, context: RepairContext) -> PatchProposal: ...


class StaticPatchProvider:
    """Offline provider used for deterministic tests and reproducible demos."""

    def __init__(self, proposal: PatchProposal) -> None:
        self.proposal = proposal

    def fingerprint(self) -> str:
        return "static:" + _sha256(self.proposal.patch.encode("utf-8"))

    def propose_patch(self, _context: RepairContext) -> PatchProposal:
        return self.proposal


def deterministic_fallback_proposal(task: PublicTask, diagnostic: FailureDiagnostic) -> PatchProposal | None:
    """V0-compatible minimal repair for the public vector-add fixture."""
    if diagnostic.code != "FUNCTIONAL_MISMATCH" or task.top != "vector_add":
        return None
    old = task.kernel_bytes.decode("utf-8")
    new = old.replace("c[i] = a[i] - b[i];", "c[i] = a[i] + b[i];", 1)
    if new == old:
        return None
    patch = "".join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile="a/kernel.cpp", tofile="b/kernel.cpp",
    ))
    return PatchProposal(
        patch=patch, provider="v0-deterministic-fallback", model="v0",
        hypothesis="The public vector-add kernel uses subtraction instead of addition.",
        change_class="functional-correction", expected_effect="Match the public addition testbench.",
        risk="Localized one-line arithmetic change.", required_validation=("csim", "synth", "cosim"),
    )


@dataclass(frozen=True)
class PatchApplication:
    """Result of applying one validated patch to one source snapshot."""

    kernel_name: str
    original_sha256: str
    patched_sha256: str
    patched_bytes: bytes
    additions: int
    deletions: int
    hunks: int
    interface_guard: TopInterfaceGuardResult | None = None


@dataclass(frozen=True)
class FailureDiagnostic:
    """Deterministic diagnosis passed to the provider and persisted on disk."""

    candidate_id: str
    stage: str
    phase: str
    code: str
    summary: str
    evidence: tuple[str, ...]
    source_excerpt: str
    source_lines: tuple[int, ...]
    repairable: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {
            "evidence": list(self.evidence),
            "source_lines": list(self.source_lines),
        }


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _patch_path(raw: str, *, prefix: str, expected: str) -> str:
    if not raw.startswith(prefix):
        raise PatchValidationError(
            f"patch header must start with {prefix.strip()!r}"
        )
    value = raw[len(prefix) :].split("\t", 1)[0].split(" ", 1)[0]
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise PatchValidationError("patch path traversal is not allowed")
    if value != expected:
        raise PatchValidationError(
            f"patch may modify only {expected!r}, received {value!r}"
        )
    if Path(value).suffix != ".cpp":
        raise PatchValidationError("patch target must be a .cpp kernel")
    return value


def unified_diff_targets_kernel(patch: str, *, kernel_name: str) -> bool:
    """Return whether a unified diff's only file headers target the kernel.

    Git-prefixed (``a/`` and ``b/``) and bare unified-diff paths are equivalent
    under the strict Patch validator.  This helper gives evidence evaluators the
    exact same path semantics without applying a candidate Patch again.
    """

    headers = [
        line for line in patch.splitlines() if line.startswith(("--- ", "+++ "))
    ]
    if (
        len(headers) != 2
        or not headers[0].startswith("--- ")
        or not headers[1].startswith("+++ ")
    ):
        return False
    try:
        _patch_path(headers[0], prefix="--- ", expected=kernel_name)
        _patch_path(headers[1], prefix="+++ ", expected=kernel_name)
    except PatchValidationError:
        return False
    return True


def canonicalize_unified_diff_paths(patch: str, *, kernel_name: str) -> str:
    """Canonicalize only validated Git/bare file headers for comparison."""

    if not unified_diff_targets_kernel(patch, kernel_name=kernel_name):
        raise PatchValidationError("unified diff does not target only the kernel")
    lines = patch.splitlines()
    old_index = next(index for index, line in enumerate(lines) if line.startswith("--- "))
    new_index = next(index for index, line in enumerate(lines) if line.startswith("+++ "))
    lines[old_index] = f"--- {kernel_name}"
    lines[new_index] = f"+++ {kernel_name}"
    canonical = "\n".join(lines)
    return canonical + ("\n" if patch.endswith("\n") else "")


def normalize_unified_diff_headers(patch: str) -> str:
    """Recompute hunk counts without changing paths or hunk body content.

    OpenAI-compatible models occasionally emit the intended minimal diff with
    stale line counts after deleting a line. Count normalization belongs to
    the deterministic parse/normalize step; the normalized diff must still
    pass the complete path, policy, context, and dry-run validator.
    """

    lines = patch.splitlines()
    changed = False
    index = 0
    while index < len(lines):
        match = _HUNK.match(lines[index])
        if match is None:
            index += 1
            continue
        end = index + 1
        valid_body = True
        old_count = 0
        new_count = 0
        while end < len(lines) and _HUNK.match(lines[end]) is None:
            body = lines[end]
            if not body or body[0] not in " +-":
                valid_body = False
                break
            if body[0] in " -":
                old_count += 1
            if body[0] in " +":
                new_count += 1
            end += 1
        if valid_body:
            closing = lines[index].find("@@", 2)
            suffix = lines[index][closing + 2 :] if closing >= 0 else ""
            normalized = (
                f"@@ -{int(match.group(1))},{old_count} "
                f"+{int(match.group(3))},{new_count} @@{suffix}"
            )
            if normalized != lines[index]:
                lines[index] = normalized
                changed = True
        index = max(index + 1, end)
    if not changed:
        return patch
    normalized_patch = "\n".join(lines)
    return normalized_patch + ("\n" if patch.endswith("\n") else "")


def relocate_unified_diff_hunks(
    source: bytes | str,
    patch: str,
    *,
    kernel_name: str,
    max_offset_lines: int | None = None,
) -> str:
    """Repair a hunk start only when its old body matches uniquely.

    This changes unified-diff location metadata only. Paths and every context,
    addition, and deletion line remain byte-for-byte identical, and the result
    must still pass :func:`apply_unified_diff`.
    """

    if max_offset_lines is not None and max_offset_lines < 0:
        raise ValueError("max_offset_lines must be non-negative")
    source_bytes = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    try:
        source_lines = source_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PatchValidationError("kernel source is not UTF-8") from exc
    lines = patch.splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("--- ")),
        None,
    )
    if header_index is None or header_index + 1 >= len(lines):
        return patch
    _patch_path(lines[header_index], prefix="--- ", expected=kernel_name)
    _patch_path(lines[header_index + 1], prefix="+++ ", expected=kernel_name)

    changed = False
    cumulative_delta = 0
    index = header_index + 2
    while index < len(lines):
        match = _HUNK.match(lines[index])
        if match is None:
            return patch
        end = index + 1
        old_body: list[str] = []
        additions = deletions = 0
        while end < len(lines) and _HUNK.match(lines[end]) is None:
            body = lines[end]
            if not body or body[0] not in " +-":
                return patch
            if body[0] in " -":
                old_body.append(body[1:])
            if body[0] == "+":
                additions += 1
            elif body[0] == "-":
                deletions += 1
            end += 1
        if not old_body:
            return patch
        declared_start = int(match.group(1)) - 1
        declared_slice = source_lines[
            declared_start : declared_start + len(old_body)
        ] if declared_start >= 0 else []
        if declared_slice != old_body:
            matches = [
                start
                for start in range(0, len(source_lines) - len(old_body) + 1)
                if source_lines[start : start + len(old_body)] == old_body
            ]
            if len(matches) != 1:
                raise PatchValidationError(
                    "patch hunk context is not a unique source match"
                )
            actual_start = matches[0]
            if (
                max_offset_lines is not None
                and abs(actual_start - declared_start) > max_offset_lines
            ):
                raise PatchValidationError("patch hunk start offset exceeds safety limit")
            closing = lines[index].find("@@", 2)
            suffix = lines[index][closing + 2 :] if closing >= 0 else ""
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
            new_start = actual_start + 1 + cumulative_delta
            lines[index] = (
                f"@@ -{actual_start + 1},{old_count} "
                f"+{new_start},{new_count} @@{suffix}"
            )
            changed = True
            declared_start = actual_start
        cumulative_delta += additions - deletions
        index = end
    if not changed:
        return patch
    relocated = "\n".join(lines)
    return relocated + ("\n" if patch.endswith("\n") else "")


def apply_unified_diff(
    source: bytes | str,
    patch: str,
    *,
    kernel_name: str,
    limits: PatchLimits | None = None,
    task: PublicTask | None = None,
) -> PatchApplication:
    """Strictly parse and apply a one-file unified diff.

    Only the task kernel may be changed.  Headers, testbenches, metadata,
    hidden/reference paths, fenced markdown, and whole-file replacements are
    rejected before any candidate directory is created.
    """

    limits = limits or PatchLimits()
    source_bytes = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchValidationError("kernel source is not UTF-8") from exc
    if "```" in patch:
        raise PatchValidationError("markdown fences are not valid patch syntax")
    lines = patch.splitlines()
    if not lines:
        raise PatchValidationError("patch is empty")
    old_headers = [
        index for index, line in enumerate(lines) if line.startswith("--- ")
    ]
    new_headers = [
        index for index, line in enumerate(lines) if line.startswith("+++ ")
    ]
    if len(old_headers) != 1 or len(new_headers) != 1:
        raise PatchValidationError(
            "unified diff must contain exactly one ---/+++ header pair"
        )
    header_index = old_headers[0]
    if new_headers[0] != header_index + 1:
        raise PatchValidationError("unified diff headers must be adjacent and ordered")
    if header_index > 0 and any(line.strip() for line in lines[:header_index]):
        raise PatchValidationError("unexpected text before unified diff headers")
    _patch_path(lines[header_index], prefix="--- ", expected=kernel_name)
    _patch_path(lines[header_index + 1], prefix="+++ ", expected=kernel_name)
    cursor = header_index + 2
    hunks: list[tuple[int, int, int, int, list[tuple[str, str]]]] = []
    cumulative_delta = 0
    while cursor < len(lines):
        hunk_index = len(hunks) + 1
        hunk_header = lines[cursor]
        match = _HUNK.match(lines[cursor])
        if match is None:
            raise PatchValidationError(f"unexpected patch line: {lines[cursor]!r}")
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        if old_start < 0 or new_start < 0:
            raise PatchValidationError("negative unified-diff locations are not allowed")
        if (old_count > 0 and old_start == 0) or (
            new_count > 0 and new_start == 0
        ):
            raise PatchValidationError(
                "non-empty unified-diff ranges must start at line one or later"
            )
        cursor += 1
        hunk_lines: list[tuple[str, str]] = []
        old_seen = new_seen = 0
        while cursor < len(lines) and not lines[cursor].startswith("@@ "):
            line = lines[cursor]
            if line.startswith("\\"):
                raise PatchValidationError("no-newline markers are not supported")
            if not line or line[0] not in " +-":
                raise PatchValidationError(f"invalid hunk line: {line!r}")
            kind, text = line[0], line[1:]
            hunk_lines.append((kind, text))
            if kind in " -":
                old_seen += 1
            if kind in " +":
                new_seen += 1
            cursor += 1
        if old_seen != old_count:
            raise _hunk_validation_error(
                error_type="PATCH_HUNK_OLD_COUNT_MISMATCH",
                kernel_name=kernel_name,
                hunk_index=hunk_index,
                hunk_header=hunk_header,
                declared_old_count=old_count,
                actual_old_count=old_seen,
                declared_new_count=new_count,
                actual_new_count=new_seen,
                declared_old_start=old_start,
                declared_new_start=new_start,
            )
        if new_seen != new_count:
            raise _hunk_validation_error(
                error_type="PATCH_HUNK_NEW_COUNT_MISMATCH",
                kernel_name=kernel_name,
                hunk_index=hunk_index,
                hunk_header=hunk_header,
                declared_old_count=old_count,
                actual_old_count=old_seen,
                declared_new_count=new_count,
                actual_new_count=new_seen,
                declared_old_start=old_start,
                declared_new_start=new_start,
            )
        if old_count == 0:
            expected_new_start = old_start + cumulative_delta + 1
        elif new_count == 0:
            expected_new_start = old_start + cumulative_delta - 1
        else:
            expected_new_start = old_start + cumulative_delta
        if new_start != expected_new_start:
            raise _hunk_validation_error(
                error_type="PATCH_HUNK_NEW_START_MISMATCH",
                kernel_name=kernel_name,
                hunk_index=hunk_index,
                hunk_header=hunk_header,
                declared_old_count=old_count,
                actual_old_count=old_seen,
                declared_new_count=new_count,
                actual_new_count=new_seen,
                declared_old_start=old_start,
                declared_new_start=new_start,
                expected_new_start=expected_new_start,
            )
        hunks.append((old_start, old_count, new_start, new_count, hunk_lines))
        cumulative_delta += new_count - old_count
    if not hunks:
        raise PatchValidationError("patch contains no hunks")
    if len(hunks) > limits.max_hunks:
        raise PatchValidationError("patch hunk limit exceeded")

    additions = sum(1 for hunk in hunks for kind, _ in hunk[4] if kind == "+")
    deletions = sum(1 for hunk in hunks for kind, _ in hunk[4] if kind == "-")
    if additions + deletions > limits.max_changed_lines:
        raise PatchValidationError("patch changed-line limit exceeded")
    source_lines = source_text.splitlines(keepends=True)
    if (
        not limits.allow_full_file_replacement
        and deletions >= len(source_lines)
        and additions > 0
    ):
        raise PatchValidationError("whole-file replacement is not allowed")
    newline = "\r\n" if "\r\n" in source_text else "\n"
    output: list[str] = []
    source_cursor = 0
    for old_start, _old_count, _new_start, _new_count, hunk_lines in hunks:
        start = old_start if _old_count == 0 else old_start - 1
        if start < source_cursor or start > len(source_lines):
            raise PatchValidationError("hunk source range is out of bounds")
        output.extend(source_lines[source_cursor:start])
        source_cursor = start
        for kind, text in hunk_lines:
            if kind == " ":
                if source_cursor >= len(source_lines) or source_lines[source_cursor].rstrip("\r\n") != text:
                    raise PatchValidationError("patch context does not match source")
                output.append(source_lines[source_cursor])
                source_cursor += 1
            elif kind == "-":
                if source_cursor >= len(source_lines) or source_lines[source_cursor].rstrip("\r\n") != text:
                    raise PatchValidationError("patch deletion does not match source")
                source_cursor += 1
            else:
                output.append(text + newline)
    output.extend(source_lines[source_cursor:])
    patched_bytes = "".join(output).encode("utf-8")
    if patched_bytes == source_bytes:
        raise PatchValidationError("patch does not change the kernel")
    if not patched_bytes.strip():
        raise PatchValidationError("patch would produce an empty kernel")
    guard_result = None
    if task is not None:
        if task.kernel_name != kernel_name:
            raise PatchValidationError(
                "top interface guard task/kernel binding does not match"
            )
        guard_result = TopInterfaceGuard.from_task(task).check(
            patched_bytes,
            changed_files=(kernel_name,),
        )
        if not guard_result.allowed:
            raise PatchValidationError(
                "top interface guard rejected patch: "
                + guard_result.failure_summary(),
                interface_guard=guard_result,
            )
    return PatchApplication(
        kernel_name=kernel_name,
        original_sha256=_sha256(source_bytes),
        patched_sha256=_sha256(patched_bytes),
        patched_bytes=patched_bytes,
        additions=additions,
        deletions=deletions,
        hunks=len(hunks),
        interface_guard=guard_result,
    )


def _result_evidence(run_root: Path, record: Mapping[str, object]) -> list[str]:
    reference = record.get("result_ref")
    if not isinstance(reference, str):
        return []
    path = (run_root / reference).resolve()
    try:
        path.relative_to(run_root.resolve())
    except ValueError:
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    evidence = value.get("evidence", []) if isinstance(value, dict) else []
    return [str(item) for item in evidence] if isinstance(evidence, list) else []


def _classify(stage: str, phase: str, evidence: list[str]) -> tuple[str, str, bool]:
    text = " ".join(evidence).casefold()
    if phase == "compile_error":
        return "COMPILE_ERROR", "kernel compilation failed", True
    if phase == "runtime_fail":
        if "mismatch" in text or "assert" in text or "expected" in text:
            return "FUNCTIONAL_MISMATCH", "public testbench reported a mismatch", True
        return "RUNTIME_FAILURE", "public testbench execution failed", True
    if phase == "synth_error":
        return "SYNTHESIS_ERROR", "HLS synthesis failed", True
    if phase == "cosim_fail":
        return "COSIM_FAILURE", "C/RTL co-simulation failed", True
    if phase == "timeout":
        return "TOOL_TIMEOUT", f"{stage} exceeded its bounded timeout", False
    return "INFRASTRUCTURE_ERROR", "tool infrastructure did not produce a repairable code failure", False


def diagnose_failure(
    task: PublicTask,
    baseline_result: Mapping[str, object],
    run_root: str | Path,
    *,
    candidate_id: str = "candidate_000",
    max_excerpt_lines: int = 80,
) -> FailureDiagnostic:
    """Create a deterministic, localized diagnostic from a V0 result."""

    root = Path(run_root).resolve()
    validation = baseline_result.get("validation", {})
    if not isinstance(validation, Mapping):
        raise V1Error("baseline result has no structured validation")
    stage = phase = "unknown"
    record: Mapping[str, object] = {}
    for name in ("csim", "synth", "cosim"):
        value = validation.get(name)
        if isinstance(value, Mapping) and value.get("status") not in {"PASS", "NOT_RUN"}:
            stage = name
            phase = str(value.get("phase", "unknown"))
            record = value
            break
    if stage == "unknown":
        raise V1Error("baseline result does not contain a repairable failure")
    evidence = _result_evidence(root, record)
    code, summary, repairable = _classify(stage, phase, evidence)
    source = task.kernel_code.splitlines()
    locations: set[int] = set()
    for item in evidence:
        for match in _LINE_REFERENCE.finditer(item):
            if Path(match.group(1)).name == Path(task.kernel_name).name:
                locations.add(int(match.group(2)))
    if locations:
        selected: set[int] = set()
        for line in locations:
            selected.update(range(max(1, line - 4), min(len(source), line + 4) + 1))
        line_numbers = sorted(selected)[:max_excerpt_lines]
    else:
        line_numbers = list(range(1, min(len(source), max_excerpt_lines) + 1))
    excerpt = "\n".join(f"{line}: {source[line - 1]}" for line in line_numbers)
    return FailureDiagnostic(
        candidate_id=candidate_id,
        stage=stage,
        phase=phase,
        code=code,
        summary=summary,
        evidence=tuple(evidence[-20:]),
        source_excerpt=excerpt,
        source_lines=tuple(line_numbers),
        repairable=repairable,
    )


def _provider_fingerprint(provider: RepairProvider) -> str:
    explicit = getattr(provider, "fingerprint", None)
    value = str(explicit()) if callable(explicit) else f"{type(provider).__module__}.{type(provider).__qualname__}"
    if not value:
        raise RepairProviderError("repair provider fingerprint is empty")
    return value


def _proposal_from_dict(value: Mapping[str, object]) -> PatchProposal:
    if value.get("ok") is False:
        raise RepairProviderError(str(value.get("error", "cached repair failed")))
    return PatchProposal(
        patch=str(value["patch"]),
        provider=str(value.get("provider", "unknown")),
        model=str(value.get("model", "unknown")),
        revision=str(value["revision"]) if value.get("revision") is not None else None,
        input_tokens=int(value.get("input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        cached_input_tokens=int(value.get("cached_input_tokens", 0)),
        request_id=str(value["request_id"]) if value.get("request_id") is not None else None,
        duration_seconds=float(value.get("duration_seconds", 0.0)),
        hypothesis=str(value["hypothesis"]) if value.get("hypothesis") is not None else None,
        change_class=str(value["change_class"]) if value.get("change_class") is not None else None,
        expected_effect=str(value["expected_effect"]) if value.get("expected_effect") is not None else None,
        risk=str(value["risk"]) if value.get("risk") is not None else None,
        required_validation=tuple(str(item) for item in value.get("required_validation", ("csim", "synth", "cosim"))),
    )


def _call_provider(
    provider: RepairProvider,
    context: RepairContext,
    *,
    run_root: Path,
    budget: BudgetLedger,
    candidate_id: str,
    code_hash: str,
    tool_config_hash: str,
) -> tuple[PatchProposal | None, str | None]:
    """Call one provider under the same durable budget ledger as Vitis."""

    provider_id = _provider_fingerprint(provider)
    stable_context = context.to_dict()
    # Remaining budget is prompt metadata, not the identity of the repair
    # request.  Excluding it makes a rerun reuse the completed LLM action
    # instead of consuming the bounded repair-call allowance again.
    stable_context.pop("remaining_tokens", None)
    stable_context.pop("remaining_credits", None)
    payload = {
        "kind": "llm",
        "candidate_id": candidate_id,
        "code_hash": code_hash,
        "tool_config_hash": tool_config_hash,
        "provider": provider_id,
        "context": stable_context,
    }
    action_id = _sha256(_canonical_json(payload).encode("utf-8"))
    result_ref = f"llm_actions/{action_id}/result.json"
    result_path = run_root / result_ref
    completed = budget.completed_event(action_id)
    if completed is not None:
        try:
            encoded = result_path.read_bytes()
            if _sha256(encoded) != completed.get("result_sha256"):
                raise RepairProviderError("cached provider result digest does not match ledger")
            value = json.loads(encoded.decode("utf-8"))
            return _proposal_from_dict(value), result_ref
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RepairProviderError(f"cached provider result is invalid: {exc}") from exc
    if budget.is_ambiguous(action_id):
        raise RepairProviderError(f"provider action {action_id} is ambiguous")
    if budget.has_pending(action_id):
        budget.mark_ambiguous(action_id)
        raise RepairProviderError(f"provider action {action_id} had no durable result")
    budget.reserve(
        action_id=action_id,
        kind="llm",
        candidate_id=candidate_id,
        code_hash=code_hash,
        tool_config_hash=tool_config_hash,
    )
    try:
        proposal = provider.propose_patch(context)
        if not isinstance(proposal, PatchProposal):
            raise TypeError("provider must return PatchProposal")
        value: dict[str, object] = {"ok": True, **proposal.to_dict()}
        input_tokens = proposal.input_tokens
        output_tokens = proposal.output_tokens
        cached_input_tokens = proposal.cached_input_tokens
        tokens = proposal.tokens_used
    except Exception as exc:
        input_tokens = int(getattr(exc, "input_tokens", 0))
        output_tokens = int(getattr(exc, "output_tokens", 0))
        cached_input_tokens = int(getattr(exc, "cached_input_tokens", 0))
        duration_seconds = float(getattr(exc, "duration_seconds", 0.0))
        value = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "request_id": getattr(exc, "request_id", None),
            "duration_seconds": duration_seconds,
            "response_excerpt": getattr(exc, "response_excerpt", None),
        }
        tokens = input_tokens + output_tokens
        proposal = None
    encoded = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(result_path)
    budget.complete(
        action_id=action_id,
        result_ref=result_ref,
        result_sha256=_sha256(encoded),
        elapsed_s=proposal.duration_seconds if proposal is not None else duration_seconds,
        tokens_used=tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )
    _append_trace(
        run_root / "trace.jsonl",
        "LLM_COMPLETED",
        action_id=action_id,
        candidate_id=candidate_id,
        provider=provider_id,
        result_ref=result_ref,
        tokens_used=tokens,
    )
    if proposal is None:
        return None, str(value["error"])
    return proposal, result_ref


def _ensure_repair_closure_budget(budget: BudgetLedger) -> None:
    """Require one LLM call and a complete final validation closure."""

    snapshot = budget.snapshot()
    required_kinds = ("llm", "csim", "synth", "cosim")
    required_credits = sum(budget.cost(kind) for kind in required_kinds)
    remaining = snapshot["credits_remaining"]
    if remaining is not None and int(remaining) < required_credits:
        raise BudgetExceeded(
            f"repair closure costs {required_credits} credits but only {remaining} remain"
        )
    # The LLM action may already be a durable cache hit. Its own reserve call
    # enforces the configured limit when a new request is actually needed.
    for kind in ("csim", "synth", "cosim"):
        limit = budget.config.tool_limits[kind]
        used = int(snapshot["tool_used"][kind]) + int(snapshot["tool_pending"][kind])  # type: ignore[index]
        if limit is not None and used >= limit:
            raise BudgetExceeded(f"repair closure requires another {kind} call")
    if int(snapshot["tokens_remaining"]) <= 0:
        raise BudgetExceeded("repair closure has no remaining token budget")


def _load_registry(run_root: Path) -> dict[str, object]:
    try:
        value = json.loads((run_root / "candidate_registry.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("candidates"), dict):
            raise TypeError("candidate registry is not an object")
        return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RunArtifactError(f"cannot load candidate registry: {exc}") from exc


def _materialize_candidate(
    task: PublicTask,
    run_root: Path,
    registry: dict[str, object],
    *,
    patch_text: str,
    application: PatchApplication,
    diagnostic: FailureDiagnostic,
    proposal: PatchProposal,
    proposal_ref: str,
) -> tuple[str, dict[str, object]]:
    materialized = CandidateManager(run_root, task).materialize(
        registry,
        parent_id="candidate_000",
        patch_text=patch_text,
        application=application,
        kind="repair",
        metadata={
            "diagnostic_ref": f"diagnostics/{diagnostic.candidate_id}.json",
            "llm_ref": proposal_ref,
            "provider": proposal.provider,
            "model": proposal.model,
            "revision": proposal.revision,
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
            "interface_guard": (
                application.interface_guard.to_dict()
                if application.interface_guard is not None
                else None
            ),
        },
    )
    return materialized.candidate_id, materialized.record


def _validate_candidate(
    task: PublicTask,
    kernel_bytes: bytes,
    candidate_id: str,
    run_root: Path,
    config: RunConfig,
    *,
    backend: ToolBackend | None,
) -> dict[str, object]:
    shared = validate_candidate(
        task,
        kernel_bytes,
        candidate_id,
        run_root,
        config,
        backend=backend,
    ).to_dict()
    records = shared.get("validation")
    if isinstance(records, dict):
        for record in records.values():
            if isinstance(record, dict):
                record.pop("validation_scope", None)
    return shared


def run_v1(
    task: PublicTask,
    run_dir: str | Path,
    config: RunConfig,
    provider: RepairProvider,
    *,
    backend: ToolBackend | None = None,
    patch_limits: PatchLimits | None = None,
    allow_deterministic_fallback: bool = False,
) -> dict[str, object]:
    """Run baseline -> one constrained repair -> isolated candidate validation."""

    if "llm" not in config.budget.costs or "llm" not in config.budget.tool_limits:
        raise V1Error("V1 budget must include an llm cost and tool limit")
    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    baseline = run_v0(task, run_root, config, backend=backend)
    if baseline.get("status") == "DONE":
        result = {
            "schema_version": 1,
            "workflow": "V1_MINIMAL_REPAIR",
            "status": "DONE",
            "stop_reason": "BASELINE_ALREADY_VERIFIED",
            "baseline": baseline,
            "candidate_id": "candidate_000",
            "rollback": None,
            "budget": baseline["budget"],
        }
        _atomic_json(run_root / "v1_result.json", result)
        return result

    diagnostic = diagnose_failure(task, baseline, run_root)
    diagnostic_ref = f"diagnostics/{diagnostic.candidate_id}.json"
    _atomic_json(run_root / diagnostic_ref, diagnostic.to_dict())
    _append_trace(
        run_root / "trace.jsonl",
        "V1_DIAGNOSTIC_READY",
        candidate_id=diagnostic.candidate_id,
        diagnostic_code=diagnostic.code,
        repairable=diagnostic.repairable,
        result_ref=diagnostic_ref,
    )
    if not diagnostic.repairable:
        result = {
            "schema_version": 1,
            "workflow": "V1_MINIMAL_REPAIR",
            "status": "FAILED",
            "stop_reason": "NO_REPAIRABLE_CODE_FAILURE",
            "baseline": baseline,
            "diagnostic": diagnostic.to_dict(),
            "diagnostic_ref": diagnostic_ref,
            "candidate_id": None,
            "rollback": {"from": "candidate_000", "to": "candidate_000", "reason": "non-repairable failure"},
            "budget": BudgetLedger(run_root / "budget_ledger.jsonl", config.budget).snapshot(),
        }
        _atomic_json(run_root / "v1_result.json", result)
        return result

    budget = BudgetLedger(run_root / "budget_ledger.jsonl", config.budget)
    snapshot = budget.snapshot()
    context = RepairContext(
        task_id=task.id,
        candidate_id="candidate_000",
        stage=diagnostic.stage,
        phase=diagnostic.phase,
        diagnostic_code=diagnostic.code,
        summary=diagnostic.summary,
        evidence=diagnostic.evidence,
        source_excerpt=diagnostic.source_excerpt,
        remaining_tokens=int(snapshot["tokens_remaining"]),
        remaining_credits=(
            int(snapshot["credits_remaining"])
            if snapshot["credits_remaining"] is not None
            else None
        ),
        top=task.top,
        kernel_name=task.kernel_name,
        part=config.tool.part,
        clock_ns=config.tool.clock_ns,
        initial_condition=task.initial_condition,
    )
    provider_stop_reason = "LLM_REPAIR_FAILED"
    try:
        _ensure_repair_closure_budget(budget)
        proposal, provider_ref_or_error = _call_provider(
            provider,
            context,
            run_root=run_root,
            budget=budget,
            candidate_id="candidate_000",
            code_hash=task.kernel_sha256,
            tool_config_hash=config.tool.hash_for("csim", backend_fingerprint="repair-context"),
        )
    except BudgetExceeded as exc:
        provider_stop_reason = "REPAIR_CLOSURE_BUDGET_DENIED"
        proposal, provider_ref_or_error = None, f"{type(exc).__name__}: {exc}"
    except (BudgetError, BudgetLedgerError, RepairProviderError) as exc:
        proposal, provider_ref_or_error = None, f"{type(exc).__name__}: {exc}"
    if proposal is None and allow_deterministic_fallback:
        fallback = deterministic_fallback_proposal(task, diagnostic)
        if fallback is not None:
            proposal = fallback
            provider_ref_or_error = "v0_fallback/deterministic_vector_add.json"
            _atomic_json(
                run_root / provider_ref_or_error,
                {"ok": True, "provider": fallback.provider, "proposal": fallback.to_dict()},
            )
            _append_trace(
                run_root / "trace.jsonl", "V1_PROVIDER_FALLBACK",
                candidate_id="candidate_000", provider=fallback.provider,
                reason=provider_ref_or_error,
            )
    if proposal is None:
        result = {
            "schema_version": 1,
            "workflow": "V1_MINIMAL_REPAIR",
            "status": "FAILED",
            "stop_reason": provider_stop_reason,
            "baseline": baseline,
            "diagnostic": diagnostic.to_dict(),
            "diagnostic_ref": diagnostic_ref,
            "provider_error": provider_ref_or_error,
            "candidate_id": None,
            "rollback": {"from": "candidate_000", "to": "candidate_000", "reason": "provider failure"},
            "budget": budget.snapshot(),
        }
        _atomic_json(run_root / "v1_result.json", result)
        return result

    normalized_patch = normalize_unified_diff_headers(proposal.patch)
    try:
        normalized_patch = relocate_unified_diff_hunks(
            task.kernel_bytes,
            normalized_patch,
            kernel_name=task.kernel_name,
        )
        application = apply_unified_diff(
            task.kernel_bytes,
            normalized_patch,
            kernel_name=task.kernel_name,
            limits=patch_limits,
            task=task,
        )
    except PatchValidationError as exc:
        result = {
            "schema_version": 1,
            "workflow": "V1_MINIMAL_REPAIR",
            "status": "FAILED",
            "stop_reason": "PATCH_INVALID",
            "baseline": baseline,
            "diagnostic": diagnostic.to_dict(),
            "diagnostic_ref": diagnostic_ref,
            "patch_error": str(exc),
            "interface_guard": (
                exc.interface_guard.to_dict()
                if exc.interface_guard is not None
                else None
            ),
            "patch": proposal.to_dict()
            | {
                "normalization_applied": normalized_patch != proposal.patch,
                "applied_patch": normalized_patch,
            },
            "candidate_id": None,
            "rollback": {"from": "candidate_000", "to": "candidate_000", "reason": "patch rejected before materialization"},
            "budget": budget.snapshot(),
        }
        _atomic_json(run_root / "v1_result.json", result)
        _append_trace(run_root / "trace.jsonl", "PATCH_REJECTED", reason=str(exc))
        return result

    proposal_ref = provider_ref_or_error or ""
    if not (proposal_ref.startswith("llm_actions/") or proposal_ref.startswith("v0_fallback/")):
        raise RepairProviderError("provider result reference is missing")
    with _RunLock(run_root):
        registry = _load_registry(run_root)
        candidate_id, candidate_record = _materialize_candidate(
            task,
            run_root,
            registry,
            patch_text=normalized_patch,
            application=application,
            diagnostic=diagnostic,
            proposal=proposal,
            proposal_ref=proposal_ref,
        )
    _append_trace(
        run_root / "trace.jsonl",
        "CANDIDATE_MATERIALIZED",
        candidate_id=candidate_id,
        parent_id="candidate_000",
        patch_sha256=_sha256(normalized_patch.encode("utf-8")),
    )
    validation = _validate_candidate(
        task,
        application.patched_bytes,
        candidate_id,
        run_root,
        config,
        backend=backend,
    )
    with _RunLock(run_root):
        registry = _load_registry(run_root)
        candidates = registry["candidates"]
        if not isinstance(candidates, dict) or candidate_id not in candidates:
            raise RunArtifactError("materialized candidate disappeared")
        candidate = candidates[candidate_id]
        if not isinstance(candidate, dict):
            raise RunArtifactError("candidate record is not an object")
        candidate["validation"] = validation["validation"]
        candidate["metrics_ref"] = validation["metrics_ref"]
        candidate["credits_used"] = validation["budget"]["credits_used"]  # type: ignore[index]
        if validation["status"] == "DONE":
            candidate["status"] = "VERIFIED"
            registry["best_candidate_id"] = candidate_id
            registry["final_candidate_id"] = candidate_id
            registry["active_candidate_id"] = candidate_id
            rollback = None
        else:
            candidate["status"] = "REJECTED"
            registry["active_candidate_id"] = "candidate_000"
            rollback = {"from": candidate_id, "to": "candidate_000", "reason": validation["stop_reason"]}
        _atomic_json(run_root / "candidate_registry.json", registry)
        candidate_record = dict(candidate)
    result = {
        "schema_version": 1,
        "workflow": "V1_MINIMAL_REPAIR",
        "status": validation["status"],
        "stop_reason": validation["stop_reason"],
        "baseline": baseline,
        "diagnostic": diagnostic.to_dict(),
        "diagnostic_ref": diagnostic_ref,
        "patch": proposal.to_dict() | {
            "normalization_applied": normalized_patch != proposal.patch,
            "applied_patch": normalized_patch,
            "additions": application.additions,
            "deletions": application.deletions,
            "hunks": application.hunks,
            "original_sha256": application.original_sha256,
            "patched_sha256": application.patched_sha256,
            "interface_guard": (
                application.interface_guard.to_dict()
                if application.interface_guard is not None
                else None
            ),
        },
        "candidate_id": candidate_id,
        "candidate": candidate_record,
        "validation": validation["validation"],
        "clock_constraint": validation["clock_constraint"],
        "rollback": rollback,
        "budget": validation["budget"],
        "v1_acceptance": {
            "llm_based": proposal.provider == "openai-compatible",
            "provider": proposal.provider,
            "token_usage_complete": bool(validation["budget"].get("token_usage_complete", False)),  # type: ignore[union-attr]
            "accepted": bool(
                validation["status"] == "DONE"
                and proposal.provider == "openai-compatible"
                and validation["budget"].get("token_usage_complete", False)  # type: ignore[union-attr]
                and proposal.tokens_used > 0
            ),
        },
        "artifacts": {
            "candidate_registry": "candidate_registry.json",
            "diagnostic": diagnostic_ref,
            "workflow_result": "v1_result.json",
        },
    }
    _atomic_json(run_root / "v1_result.json", result)
    _append_trace(
        run_root / "trace.jsonl",
        "V1_COMPLETED",
        candidate_id=candidate_id,
        status=validation["status"],
        stop_reason=validation["stop_reason"],
        rollback=rollback,
    )
    return result
