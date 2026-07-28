"""Fail-closed admission contract for current Continuation V3 enforce authority."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import canonical_json


CONTINUATION_ADMISSION_SCHEMA = "v3.continuation-admission.v2"
CONTINUATION_GATE_SCHEMA = "v3.continuation-offline-gate.v1"
CONTINUATION_POLICY_VERSION = "v3.continuation-policy.v3"
CONTINUATION_DECISION_SCHEMA = "v3.continuation-decision.v2"
CONTINUATION_PROTOCOL_VERSION = "v3.continuation-offline-replay.v1"
CONTINUATION_MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
CONTINUATION_FIXED_THRESHOLDS = {
    "minimum_samples_per_mode": 1,
    "maximum_false_blocks": 0,
    "maximum_leakage_violations": 0,
}
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_POSITIVE_OUTCOMES = {
    "ESSENTIAL_FOR_CORRECTNESS",
    "BENEFICIAL_PERFORMANCE",
}
_OUTCOMES = _POSITIVE_OUTCOMES | {"HARMFUL", "WASTEFUL"}


def current_repository_commit() -> str:
    """Return the checked-out commit used by the current product code."""

    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or _COMMIT.fullmatch(value) is None:
        raise ValueError("Continuation admission cannot resolve current commit")
    return value


def _mode_coverage(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(CONTINUATION_MODES):
        raise ValueError(f"{label} must cover all four modes")
    result: dict[str, int] = {}
    for mode in CONTINUATION_MODES:
        count = value.get(mode)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{label} per-mode sample Gate failed")
        result[mode] = count
    return result


def _gate_failures(gate: Mapping[str, object]) -> list[str]:
    """Recompute the three deliberately small offline Gate conditions."""

    samples = gate.get("audited_samples")
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise ValueError("Continuation Gate audited samples are invalid")
    modes: Counter[str] = Counter()
    false_blocks = 0
    leakage = 0
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ValueError("Continuation Gate sample is invalid")
        mode = sample.get("mode")
        if mode not in CONTINUATION_MODES:
            raise ValueError("Continuation Gate sample mode is invalid")
        outcome = sample.get("outcome")
        if outcome not in _OUTCOMES:
            raise ValueError("Continuation Gate sample outcome is invalid")
        decision = sample.get("current_decision")
        if not isinstance(decision, Mapping):
            raise ValueError("Continuation Gate sample decision is invalid")
        if decision.get("schema_version") != CONTINUATION_DECISION_SCHEMA:
            raise ValueError("Continuation Gate decision schema mismatch")
        if decision.get("policy_version") != CONTINUATION_POLICY_VERSION:
            raise ValueError("Continuation Gate decision policy mismatch")
        decision_value = decision.get("decision")
        if decision_value not in {"ALLOW", "BLOCK", "DEFER_TO_FINAL"}:
            raise ValueError("Continuation Gate decision value is invalid")
        sample_leakage = sample.get("leakage_violations")
        if (
            isinstance(sample_leakage, bool)
            or not isinstance(sample_leakage, int)
            or sample_leakage < 0
        ):
            raise ValueError("Continuation Gate sample leakage is invalid")
        modes[str(mode)] += 1
        false_blocks += int(
            outcome in _POSITIVE_OUTCOMES and decision_value != "ALLOW"
        )
        leakage += sample_leakage

    expected_coverage = _mode_coverage(
        gate.get("mode_coverage"),
        label="Continuation Gate mode coverage",
    )
    if dict(modes) != expected_coverage:
        raise ValueError("Continuation Gate mode coverage does not match samples")
    metrics = gate.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("Continuation Gate metrics are invalid")
    if metrics.get("sample_count") != len(samples):
        raise ValueError("Continuation Gate sample count mismatch")
    if metrics.get("false_blocks") != false_blocks:
        raise ValueError("Continuation Gate false-block metric mismatch")
    if metrics.get("leakage_violations") != leakage:
        raise ValueError("Continuation Gate leakage metric mismatch")

    failures: list[str] = []
    if any(
        count
        < CONTINUATION_FIXED_THRESHOLDS["minimum_samples_per_mode"]
        for count in expected_coverage.values()
    ):
        failures.append("PER_MODE_SAMPLE_GATE")
    if (
        false_blocks
        > CONTINUATION_FIXED_THRESHOLDS["maximum_false_blocks"]
    ):
        failures.append("FALSE_BLOCK_GATE")
    if (
        leakage
        > CONTINUATION_FIXED_THRESHOLDS["maximum_leakage_violations"]
    ):
        failures.append("LEAKAGE_GATE")
    return failures


def validate_continuation_gate(
    value: Mapping[str, object],
    *,
    expected_commit: str | None = None,
) -> dict[str, object]:
    """Validate the immutable Gate content independently of its Admission."""

    expected = {
        "schema_version",
        "gate_status",
        "policy_version",
        "decision_schema",
        "current_commit",
        "protocol_version",
        "thresholds",
        "mode_coverage",
        "metrics",
        "source_manifest_sha256",
        "audited_samples",
    }
    if set(value) != expected:
        raise ValueError("Continuation Gate fields mismatch")
    if value.get("schema_version") != CONTINUATION_GATE_SCHEMA:
        raise ValueError("unsupported Continuation Gate schema")
    if value.get("policy_version") != CONTINUATION_POLICY_VERSION:
        raise ValueError("Continuation Gate policy version mismatch")
    if value.get("decision_schema") != CONTINUATION_DECISION_SCHEMA:
        raise ValueError("Continuation Gate decision schema mismatch")
    if value.get("protocol_version") != CONTINUATION_PROTOCOL_VERSION:
        raise ValueError("Continuation Gate protocol version mismatch")
    commit = value.get("current_commit")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("Continuation Gate current commit is invalid")
    current_commit = expected_commit or current_repository_commit()
    if commit != current_commit:
        raise ValueError("Continuation Gate current commit mismatch")
    if value.get("thresholds") != CONTINUATION_FIXED_THRESHOLDS:
        raise ValueError("Continuation Gate thresholds were changed")
    source_digest = value.get("source_manifest_sha256")
    if (
        not isinstance(source_digest, str)
        or _SHA256.fullmatch(source_digest) is None
    ):
        raise ValueError("Continuation Gate source digest is invalid")
    failures = _gate_failures(value)
    expected_status = "PASS" if not failures else "FAIL"
    if value.get("gate_status") != expected_status:
        raise ValueError("Continuation Gate status does not match recomputation")
    if expected_status != "PASS":
        raise ValueError(
            "Continuation Gate is not PASS: " + ",".join(failures)
        )
    copied = json.loads(canonical_json(value).decode("utf-8"))
    assert isinstance(copied, dict)
    return copied


def _resolve_gate_path(
    gate_ref: object,
    *,
    admission_path: str | Path,
) -> Path:
    if not isinstance(gate_ref, str) or not gate_ref:
        raise ValueError("Continuation admission Gate path is invalid")
    relative = Path(gate_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Continuation admission Gate path must be local")
    admission = Path(admission_path).expanduser().resolve()
    parent = admission.parent
    gate_path = (parent / relative).resolve()
    try:
        gate_path.relative_to(parent)
    except ValueError as exc:
        raise ValueError(
            "Continuation admission Gate path escapes its artifact directory"
        ) from exc
    if not gate_path.is_file():
        raise ValueError("Continuation admission Gate JSON is missing")
    return gate_path


def validate_continuation_admission(
    value: Mapping[str, object],
    *,
    admission_path: str | Path | None = None,
    expected_commit: str | None = None,
) -> dict[str, object]:
    """Validate Admission and re-read the exact Gate it authorizes."""

    expected = {
        "schema_version",
        "policy_version",
        "decision_schema",
        "current_commit",
        "gate_json_path",
        "gate_json_sha256",
        "gate_status",
        "protocol_version",
        "mode_coverage",
    }
    if set(value) != expected:
        raise ValueError("Continuation admission fields mismatch")
    if value.get("schema_version") != CONTINUATION_ADMISSION_SCHEMA:
        raise ValueError("unsupported Continuation admission schema")
    if value.get("policy_version") != CONTINUATION_POLICY_VERSION:
        raise ValueError("Continuation admission policy version mismatch")
    if value.get("decision_schema") != CONTINUATION_DECISION_SCHEMA:
        raise ValueError("Continuation admission decision schema mismatch")
    if value.get("protocol_version") != CONTINUATION_PROTOCOL_VERSION:
        raise ValueError("Continuation admission protocol version mismatch")
    commit = value.get("current_commit")
    current_commit = expected_commit or current_repository_commit()
    if commit != current_commit:
        raise ValueError("Continuation admission current commit mismatch")
    if value.get("gate_status") != "PASS":
        raise ValueError("Continuation admission Gate status is not PASS")
    coverage = _mode_coverage(
        value.get("mode_coverage"),
        label="Continuation admission mode coverage",
    )
    digest = value.get("gate_json_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("Continuation admission Gate digest is invalid")
    if admission_path is None:
        raise ValueError(
            "Continuation admission path is required to validate its Gate"
        )
    gate_path = _resolve_gate_path(
        value.get("gate_json_path"),
        admission_path=admission_path,
    )
    gate_bytes = gate_path.read_bytes()
    if hashlib.sha256(gate_bytes).hexdigest() != digest:
        raise ValueError("Continuation admission Gate hash mismatch")
    decoded = json.loads(gate_bytes)
    if not isinstance(decoded, Mapping):
        raise ValueError("Continuation Gate must be a JSON object")
    gate = validate_continuation_gate(
        decoded,
        expected_commit=current_commit,
    )
    if gate.get("gate_status") != value.get("gate_status"):
        raise ValueError("Continuation admission Gate status mismatch")
    if gate.get("policy_version") != value.get("policy_version"):
        raise ValueError("Continuation admission Gate policy mismatch")
    if gate.get("decision_schema") != value.get("decision_schema"):
        raise ValueError("Continuation admission Gate decision schema mismatch")
    if gate.get("current_commit") != value.get("current_commit"):
        raise ValueError("Continuation admission Gate commit mismatch")
    if gate.get("protocol_version") != value.get("protocol_version"):
        raise ValueError("Continuation admission Gate protocol mismatch")
    if gate.get("mode_coverage") != coverage:
        raise ValueError("Continuation admission Gate mode coverage mismatch")
    copied = json.loads(canonical_json(value).decode("utf-8"))
    assert isinstance(copied, dict)
    return copied


def load_continuation_admission(path: str | Path) -> dict[str, object]:
    admission_path = Path(path).expanduser().resolve()
    value = json.loads(admission_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("Continuation admission must be a JSON object")
    return validate_continuation_admission(
        value,
        admission_path=admission_path,
    )


__all__ = [
    "CONTINUATION_ADMISSION_SCHEMA",
    "CONTINUATION_DECISION_SCHEMA",
    "CONTINUATION_FIXED_THRESHOLDS",
    "CONTINUATION_GATE_SCHEMA",
    "CONTINUATION_MODES",
    "CONTINUATION_POLICY_VERSION",
    "CONTINUATION_PROTOCOL_VERSION",
    "current_repository_commit",
    "load_continuation_admission",
    "validate_continuation_admission",
    "validate_continuation_gate",
]
