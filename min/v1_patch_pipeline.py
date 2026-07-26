"""Deterministic, exact-only unified-diff resolution for Phase V1."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from llm4hls_agent.repair import (
    PatchApplication,
    PatchLimits,
    PatchValidationError,
    apply_unified_diff,
)
from llm4hls_agent.task import PublicTask


RAW_STRICT = "RAW_STRICT"
NORMALIZED_STRICT = "NORMALIZED_STRICT"
EXACT_CONTEXT_RECOVERY = "EXACT_CONTEXT_RECOVERY"
UNIQUE_DELETE_BLOCK_RECOVERY = "UNIQUE_DELETE_BLOCK_RECOVERY"
STAGE_ORDER = (
    RAW_STRICT,
    NORMALIZED_STRICT,
    EXACT_CONTEXT_RECOVERY,
    UNIQUE_DELETE_BLOCK_RECOVERY,
)
_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?P<suffix>.*)$"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    new_start: int
    suffix: str
    body: tuple[str, ...]

    @property
    def old_count(self) -> int:
        return sum(line[0] in " -" for line in self.body)

    @property
    def new_count(self) -> int:
        return sum(line[0] in " +" for line in self.body)

    @property
    def additions(self) -> int:
        return sum(line[0] == "+" for line in self.body)

    @property
    def deletions(self) -> int:
        return sum(line[0] == "-" for line in self.body)


@dataclass(frozen=True)
class _ParsedPatch:
    prefix: tuple[str, ...]
    old_header: str
    new_header: str
    hunks: tuple[_Hunk, ...]
    trailing_newline: bool


@dataclass(frozen=True)
class PatchResolution:
    application: PatchApplication
    raw_patch: str
    normalized_patch: str
    applied_patch: str
    selected_stage: str
    receipt: dict[str, object]


class RecoveryMatchError(PatchValidationError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        match_counts: list[int] | None = None,
    ) -> None:
        self.code = code
        self.match_counts = list(match_counts or [])
        super().__init__(message)


class PatchResolutionError(PatchValidationError):
    def __init__(
        self,
        message: str,
        *,
        raw_patch: str,
        normalized_patch: str,
        receipt: dict[str, object],
    ) -> None:
        self.raw_patch = raw_patch
        self.normalized_patch = normalized_patch
        self.receipt = receipt
        super().__init__(message)


def _parse_patch(patch: str) -> _ParsedPatch:
    if "```" in patch:
        raise PatchValidationError("markdown fences are not valid patch syntax")
    lines = patch.splitlines()
    old_indices = [
        index for index, line in enumerate(lines) if line.startswith("--- ")
    ]
    new_indices = [
        index for index, line in enumerate(lines) if line.startswith("+++ ")
    ]
    if len(old_indices) != 1 or len(new_indices) != 1:
        raise PatchValidationError(
            "unified diff must contain exactly one ---/+++ header pair"
        )
    header_index = old_indices[0]
    if new_indices[0] != header_index + 1:
        raise PatchValidationError("unified diff headers must be adjacent and ordered")
    if any(line.strip() for line in lines[:header_index]):
        raise PatchValidationError("unexpected text before unified diff headers")
    cursor = header_index + 2
    hunks: list[_Hunk] = []
    while cursor < len(lines):
        match = _HUNK.match(lines[cursor])
        if match is None:
            raise PatchValidationError(f"unexpected patch line: {lines[cursor]!r}")
        cursor += 1
        body: list[str] = []
        while cursor < len(lines) and _HUNK.match(lines[cursor]) is None:
            line = lines[cursor]
            if not line or line[0] not in " +-":
                raise PatchValidationError(f"invalid normalized hunk line: {line!r}")
            if line.startswith("\\"):
                raise PatchValidationError("no-newline markers are not supported")
            body.append(line)
            cursor += 1
        if not body:
            raise PatchValidationError("patch hunk body is empty")
        hunks.append(
            _Hunk(
                old_start=int(match.group(1)),
                new_start=int(match.group(3)),
                suffix=match.group("suffix"),
                body=tuple(body),
            )
        )
    if not hunks:
        raise PatchValidationError("patch contains no hunks")
    return _ParsedPatch(
        prefix=tuple(lines[:header_index]),
        old_header=lines[header_index],
        new_header=lines[header_index + 1],
        hunks=tuple(hunks),
        trailing_newline=patch.endswith("\n"),
    )


def _serialize(parsed: _ParsedPatch) -> str:
    lines = [
        *parsed.prefix,
        parsed.old_header,
        parsed.new_header,
    ]
    for hunk in parsed.hunks:
        lines.append(
            f"@@ -{hunk.old_start},{hunk.old_count} "
            f"+{hunk.new_start},{hunk.new_count} @@{hunk.suffix}"
        )
        lines.extend(hunk.body)
    value = "\n".join(lines)
    return value + ("\n" if parsed.trailing_newline else "")


def normalize_patch(patch: str) -> str:
    """Prefix bare empty hunk lines and recompute every hunk count."""

    lines = patch.splitlines()
    in_hunk = False
    normalized: list[str] = []
    for line in lines:
        if _HUNK.match(line):
            in_hunk = True
            normalized.append(line)
            continue
        if in_hunk and line == "":
            normalized.append(" ")
            continue
        normalized.append(line)
    candidate = "\n".join(normalized) + ("\n" if patch.endswith("\n") else "")
    parsed = _parse_patch(candidate)
    return _serialize(parsed)


def _preflight_raw_patch(patch: str) -> None:
    """Reject ambiguous envelopes and hunks with no exact old-side anchor."""

    if "```" in patch:
        raise PatchValidationError("markdown fences are not valid patch syntax")
    lines = patch.splitlines()
    old_headers = [
        index for index, line in enumerate(lines) if line.startswith("--- ")
    ]
    new_headers = [
        index for index, line in enumerate(lines) if line.startswith("+++ ")
    ]
    if len(old_headers) != 1 or len(new_headers) != 1:
        raise PatchValidationError(
            "V1 patch must contain exactly one unambiguous file header pair"
        )
    header_index = old_headers[0]
    if new_headers[0] != header_index + 1:
        raise PatchValidationError("V1 patch file headers must be adjacent and ordered")
    if any(line.strip() for line in lines[:header_index]):
        raise PatchValidationError("unexpected text before unified diff headers")
    cursor = header_index + 2
    hunk_count = 0
    while cursor < len(lines):
        if _HUNK.match(lines[cursor]) is None:
            raise PatchValidationError(f"unexpected patch line: {lines[cursor]!r}")
        hunk_count += 1
        cursor += 1
        old_side_count = 0
        body_count = 0
        while cursor < len(lines) and _HUNK.match(lines[cursor]) is None:
            line = lines[cursor]
            if line.startswith("\\"):
                raise PatchValidationError("no-newline markers are not supported")
            if line == "":
                old_side_count += 1
                body_count += 1
                cursor += 1
                continue
            if line[0] not in " +-":
                raise PatchValidationError(f"invalid hunk line: {line!r}")
            if line[0] in " -":
                old_side_count += 1
            body_count += 1
            cursor += 1
        if body_count == 0:
            raise PatchValidationError("patch hunk body is empty")
        if old_side_count == 0:
            raise PatchValidationError(
                "V1 rejects addition-only hunks without an exact old-side anchor"
            )
    if hunk_count == 0:
        raise PatchValidationError("patch contains no hunks")


def _exact_matches(source: list[str], needle: list[str]) -> list[int]:
    if not needle or len(needle) > len(source):
        return []
    return [
        start
        for start in range(len(source) - len(needle) + 1)
        if source[start : start + len(needle)] == needle
    ]


def _validate_ordered_nonoverlap(
    starts: list[int],
    lengths: list[int],
) -> None:
    previous_end = -1
    for start, length in zip(starts, lengths, strict=True):
        if start < previous_end:
            raise RecoveryMatchError(
                "recovered hunks are out of order or overlap",
                code="RECOVERY_HUNKS_OVERLAP",
            )
        previous_end = start + length


def relocate_by_full_old_side(
    source: bytes,
    normalized_patch: str,
) -> tuple[str, list[int]]:
    try:
        source_lines = source.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PatchValidationError("kernel source is not UTF-8") from exc
    parsed = _parse_patch(normalized_patch)
    match_counts: list[int] = []
    starts: list[int] = []
    old_lengths: list[int] = []
    for hunk in parsed.hunks:
        old_side = [line[1:] for line in hunk.body if line[0] in " -"]
        matches = _exact_matches(source_lines, old_side)
        match_counts.append(len(matches))
        if not matches:
            raise RecoveryMatchError(
                "complete old-side context has zero exact source matches",
                code="EXACT_CONTEXT_ZERO_MATCH",
                match_counts=match_counts,
            )
        if len(matches) > 1:
            raise RecoveryMatchError(
                "complete old-side context has multiple exact source matches",
                code="EXACT_CONTEXT_AMBIGUOUS_MATCH",
                match_counts=match_counts,
            )
        starts.append(matches[0])
        old_lengths.append(len(old_side))
    _validate_ordered_nonoverlap(starts, old_lengths)
    cumulative_delta = 0
    rebuilt: list[_Hunk] = []
    for hunk, start in zip(parsed.hunks, starts, strict=True):
        old_start = start + 1
        new_start = old_start + cumulative_delta
        rebuilt.append(
            _Hunk(
                old_start=old_start,
                new_start=new_start,
                suffix=hunk.suffix,
                body=hunk.body,
            )
        )
        cumulative_delta += hunk.additions - hunk.deletions
    return (
        _serialize(
            _ParsedPatch(
                prefix=parsed.prefix,
                old_header=parsed.old_header,
                new_header=parsed.new_header,
                hunks=tuple(rebuilt),
                trailing_newline=parsed.trailing_newline,
            )
        ),
        match_counts,
    )


def rebuild_by_unique_delete_block(
    source: bytes,
    normalized_patch: str,
) -> tuple[str, list[int]]:
    try:
        source_lines = source.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PatchValidationError("kernel source is not UTF-8") from exc
    parsed = _parse_patch(normalized_patch)
    rebuilt_bodies: list[tuple[str, ...]] = []
    starts: list[int] = []
    deleted_lengths: list[int] = []
    match_counts: list[int] = []
    for hunk in parsed.hunks:
        change_indices = [
            index for index, line in enumerate(hunk.body) if line[0] in "+-"
        ]
        if not change_indices:
            raise RecoveryMatchError(
                "hunk contains no deletion/change block",
                code="DELETE_BLOCK_ZERO_MATCH",
                match_counts=match_counts,
            )
        cluster = hunk.body[
            min(change_indices) : max(change_indices) + 1
        ]
        if any(line[0] == " " for line in cluster):
            raise RecoveryMatchError(
                "hunk contains multiple context-separated edit clusters",
                code="DELETE_BLOCK_MULTIPLE_CLUSTERS",
                match_counts=match_counts,
            )
        deleted = [line[1:] for line in cluster if line[0] == "-"]
        if not deleted:
            raise RecoveryMatchError(
                "addition-only hunk has no exact deletion anchor",
                code="DELETE_BLOCK_NO_DELETION",
                match_counts=match_counts,
            )
        matches = _exact_matches(source_lines, deleted)
        match_counts.append(len(matches))
        if not matches:
            raise RecoveryMatchError(
                "deletion block has zero exact source matches",
                code="DELETE_BLOCK_ZERO_MATCH",
                match_counts=match_counts,
            )
        if len(matches) > 1:
            raise RecoveryMatchError(
                "deletion block has multiple exact source matches",
                code="DELETE_BLOCK_AMBIGUOUS_MATCH",
                match_counts=match_counts,
            )
        rebuilt_bodies.append(tuple(cluster))
        starts.append(matches[0])
        deleted_lengths.append(len(deleted))
    _validate_ordered_nonoverlap(starts, deleted_lengths)
    cumulative_delta = 0
    rebuilt_hunks: list[_Hunk] = []
    for original, body, start in zip(
        parsed.hunks,
        rebuilt_bodies,
        starts,
        strict=True,
    ):
        old_start = start + 1
        new_start = old_start + cumulative_delta
        hunk = _Hunk(
            old_start=old_start,
            new_start=new_start,
            suffix=original.suffix,
            body=body,
        )
        rebuilt_hunks.append(hunk)
        cumulative_delta += hunk.additions - hunk.deletions
    return (
        _serialize(
            _ParsedPatch(
                prefix=parsed.prefix,
                old_header=parsed.old_header,
                new_header=parsed.new_header,
                hunks=tuple(rebuilt_hunks),
                trailing_newline=parsed.trailing_newline,
            )
        ),
        match_counts,
    )


def _attempt_record(
    *,
    order: int,
    stage: str,
    patch: str | None,
    status: str,
    error: Exception | None = None,
    match_counts: list[int] | None = None,
) -> dict[str, object]:
    error_code = getattr(error, "code", None)
    guard = getattr(error, "interface_guard", None)
    return {
        "order": order,
        "stage": stage,
        "status": status,
        "input_patch_sha256": (
            _sha256(patch.encode("utf-8")) if patch is not None else None
        ),
        "match_counts": match_counts,
        "error_code": error_code or (type(error).__name__ if error else None),
        "error_detail": str(error) if error else None,
        "interface_guard": (
            guard.to_dict() if guard is not None else None
        ),
        "fuzzy_matching_used": False,
    }


def _strict_apply(
    *,
    task: PublicTask,
    source: bytes,
    patch: str,
    limits: PatchLimits,
) -> PatchApplication:
    application = apply_unified_diff(
        source,
        patch,
        kernel_name=task.kernel_name,
        limits=limits,
        task=task,
    )
    guard = application.interface_guard
    if guard is not None and guard.high_risk:
        raise PatchValidationError(
            "V1 patch policy rejects top-interface directive changes",
            interface_guard=guard,
        )
    return application


def resolve_and_apply_patch(
    *,
    task: PublicTask,
    raw_patch: str,
    max_changed_lines: int,
) -> PatchResolution:
    """Apply the first successful exact stage, preserving full provenance."""

    source = task.kernel_bytes
    source_sha = _sha256(source)
    limits = PatchLimits(
        max_changed_lines=max_changed_lines,
        max_hunks=8,
        allow_full_file_replacement=False,
    )
    attempts: list[dict[str, object]] = []
    try:
        _preflight_raw_patch(raw_patch)
    except (PatchValidationError, ValueError) as exc:
        receipt = {
            "schema_version": "track-a.v1-patch-resolution.v1",
            "kernel_name": task.kernel_name,
            "source_sha256": source_sha,
            "raw_patch_sha256": _sha256(raw_patch.encode("utf-8")),
            "normalized_patch_sha256": _sha256(raw_patch.encode("utf-8")),
            "applied_patch_sha256": None,
            "patched_source_sha256": None,
            "selected_stage": None,
            "status": "REJECTED",
            "attempts": [
                _attempt_record(
                    order=1,
                    stage=RAW_STRICT,
                    patch=raw_patch,
                    status="REJECTED",
                    error=exc,
                )
            ],
            "stage_order": list(STAGE_ORDER),
            "fuzzy_recovery_count": 0,
            "limits": {
                "max_changed_lines": limits.max_changed_lines,
                "max_hunks": limits.max_hunks,
                "allow_full_file_replacement": False,
            },
        }
        raise PatchResolutionError(
            "raw Patch failed the V1 safety envelope",
            raw_patch=raw_patch,
            normalized_patch=raw_patch,
            receipt=receipt,
        ) from exc

    try:
        application = _strict_apply(
            task=task,
            source=source,
            patch=raw_patch,
            limits=limits,
        )
    except PatchValidationError as exc:
        attempts.append(
            _attempt_record(
                order=1,
                stage=RAW_STRICT,
                patch=raw_patch,
                status="REJECTED",
                error=exc,
            )
        )
    else:
        normalized = raw_patch
        attempts.append(
            _attempt_record(
                order=1,
                stage=RAW_STRICT,
                patch=raw_patch,
                status="APPLIED",
            )
        )
        receipt = _receipt(
            task=task,
            limits=limits,
            source_sha=source_sha,
            raw_patch=raw_patch,
            normalized_patch=normalized,
            applied_patch=raw_patch,
            selected_stage=RAW_STRICT,
            application=application,
            attempts=attempts,
        )
        return PatchResolution(
            application=application,
            raw_patch=raw_patch,
            normalized_patch=normalized,
            applied_patch=raw_patch,
            selected_stage=RAW_STRICT,
            receipt=receipt,
        )

    try:
        normalized = normalize_patch(raw_patch)
    except (PatchValidationError, ValueError) as exc:
        normalized = raw_patch
        attempts.append(
            _attempt_record(
                order=2,
                stage=NORMALIZED_STRICT,
                patch=raw_patch,
                status="REJECTED",
                error=exc,
            )
        )
        normalization_error: Exception | None = exc
    else:
        normalization_error = None
        try:
            application = _strict_apply(
                task=task,
                source=source,
                patch=normalized,
                limits=limits,
            )
        except PatchValidationError as exc:
            attempts.append(
                _attempt_record(
                    order=2,
                    stage=NORMALIZED_STRICT,
                    patch=normalized,
                    status="REJECTED",
                    error=exc,
                )
            )
        else:
            attempts.append(
                _attempt_record(
                    order=2,
                    stage=NORMALIZED_STRICT,
                    patch=normalized,
                    status="APPLIED",
                )
            )
            receipt = _receipt(
                task=task,
                limits=limits,
                source_sha=source_sha,
                raw_patch=raw_patch,
                normalized_patch=normalized,
                applied_patch=normalized,
                selected_stage=NORMALIZED_STRICT,
                application=application,
                attempts=attempts,
            )
            return PatchResolution(
                application=application,
                raw_patch=raw_patch,
                normalized_patch=normalized,
                applied_patch=normalized,
                selected_stage=NORMALIZED_STRICT,
                receipt=receipt,
            )

    recovery_builders = (
        (EXACT_CONTEXT_RECOVERY, relocate_by_full_old_side),
        (UNIQUE_DELETE_BLOCK_RECOVERY, rebuild_by_unique_delete_block),
    )
    for order, (stage, builder) in enumerate(recovery_builders, start=3):
        if normalization_error is not None:
            attempts.append(
                _attempt_record(
                    order=order,
                    stage=stage,
                    patch=normalized,
                    status="REJECTED",
                    error=normalization_error,
                )
            )
            continue
        try:
            candidate_patch, match_counts = builder(source, normalized)
        except (PatchValidationError, ValueError) as exc:
            attempts.append(
                _attempt_record(
                    order=order,
                    stage=stage,
                    patch=normalized,
                    status="REJECTED",
                    error=exc,
                    match_counts=getattr(exc, "match_counts", None),
                )
            )
            continue
        try:
            application = _strict_apply(
                task=task,
                source=source,
                patch=candidate_patch,
                limits=limits,
            )
        except PatchValidationError as exc:
            attempts.append(
                _attempt_record(
                    order=order,
                    stage=stage,
                    patch=candidate_patch,
                    status="REJECTED",
                    error=exc,
                    match_counts=match_counts,
                )
            )
            continue
        attempts.append(
            _attempt_record(
                order=order,
                stage=stage,
                patch=candidate_patch,
                status="APPLIED",
                match_counts=match_counts,
            )
        )
        receipt = _receipt(
            task=task,
            limits=limits,
            source_sha=source_sha,
            raw_patch=raw_patch,
            normalized_patch=normalized,
            applied_patch=candidate_patch,
            selected_stage=stage,
            application=application,
            attempts=attempts,
        )
        return PatchResolution(
            application=application,
            raw_patch=raw_patch,
            normalized_patch=normalized,
            applied_patch=candidate_patch,
            selected_stage=stage,
            receipt=receipt,
        )

    receipt = {
        "schema_version": "track-a.v1-patch-resolution.v1",
        "kernel_name": task.kernel_name,
        "source_sha256": source_sha,
        "raw_patch_sha256": _sha256(raw_patch.encode("utf-8")),
        "normalized_patch_sha256": _sha256(normalized.encode("utf-8")),
        "applied_patch_sha256": None,
        "patched_source_sha256": None,
        "selected_stage": None,
        "status": "REJECTED",
        "attempts": attempts,
        "stage_order": list(STAGE_ORDER),
        "fuzzy_recovery_count": 0,
        "limits": {
            "max_changed_lines": limits.max_changed_lines,
            "max_hunks": limits.max_hunks,
            "allow_full_file_replacement": False,
        },
    }
    raise PatchResolutionError(
        "all deterministic patch application stages rejected the patch",
        raw_patch=raw_patch,
        normalized_patch=normalized,
        receipt=receipt,
    )


def _receipt(
    *,
    task: PublicTask,
    limits: PatchLimits,
    source_sha: str,
    raw_patch: str,
    normalized_patch: str,
    applied_patch: str,
    selected_stage: str,
    application: PatchApplication,
    attempts: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": "track-a.v1-patch-resolution.v1",
        "kernel_name": task.kernel_name,
        "source_sha256": source_sha,
        "raw_patch_sha256": _sha256(raw_patch.encode("utf-8")),
        "normalized_patch_sha256": _sha256(normalized_patch.encode("utf-8")),
        "applied_patch_sha256": _sha256(applied_patch.encode("utf-8")),
        "patched_source_sha256": application.patched_sha256,
        "selected_stage": selected_stage,
        "status": "APPLIED",
        "attempts": attempts,
        "stage_order": list(STAGE_ORDER),
        "fuzzy_recovery_count": 0,
        "limits": {
            "max_changed_lines": limits.max_changed_lines,
            "max_hunks": limits.max_hunks,
            "allow_full_file_replacement": False,
        },
        "interface_guard": (
            application.interface_guard.to_dict()
            if application.interface_guard is not None
            else None
        ),
    }


def persist_patch_resolution(
    *,
    evidence_dir: Path,
    raw_patch: str,
    normalized_patch: str,
    applied_patch: str | None,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Persist raw, normalized, resolved Patch and the stage receipt."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw_path = evidence_dir / "patch.raw.diff"
    normalized_path = evidence_dir / "patch.normalized.diff"
    applied_path = evidence_dir / "patch.applied.diff"
    receipt_path = evidence_dir / "patch_resolution.json"
    existing = [
        path
        for path in (raw_path, normalized_path, applied_path, receipt_path)
        if path.exists()
    ]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing Patch evidence: "
            + ", ".join(path.name for path in existing)
        )
    _atomic_write(raw_path, raw_patch.encode("utf-8"))
    _atomic_write(normalized_path, normalized_patch.encode("utf-8"))
    if applied_patch is not None:
        _atomic_write(applied_path, applied_patch.encode("utf-8"))
    value = dict(receipt) | {
        "raw_patch_ref": raw_path.name,
        "normalized_patch_ref": normalized_path.name,
        "applied_patch_ref": applied_path.name if applied_patch is not None else None,
    }
    _atomic_json(receipt_path, value)
    return {
        "patch_resolution_ref": receipt_path.name,
        "patch_resolution_sha256": _sha256(receipt_path.read_bytes()),
        "raw_patch_ref": raw_path.name,
        "raw_patch_sha256": _sha256(raw_path.read_bytes()),
        "normalized_patch_ref": normalized_path.name,
        "normalized_patch_sha256": _sha256(normalized_path.read_bytes()),
        "applied_patch_ref": applied_path.name if applied_patch is not None else None,
        "applied_patch_sha256": (
            _sha256(applied_path.read_bytes()) if applied_patch is not None else None
        ),
        "selected_stage": receipt.get("selected_stage"),
    }
