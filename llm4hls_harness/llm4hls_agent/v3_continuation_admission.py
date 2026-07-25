"""Fail-closed admission contract for Continuation V2 enforce authority."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from .v3_experience import canonical_json


CONTINUATION_ADMISSION_SCHEMA = "v3.continuation-admission.v1"
CONTINUATION_POLICY_VERSION = "v3.continuation-policy.v2"
CONTINUATION_FIXED_THRESHOLDS = {
    "minimum_samples_per_mode": 1,
    "maximum_false_blocks": 0,
    "minimum_structural_essential_samples": 1,
    "minimum_structural_essential_retention": 1.0,
    "maximum_leakage_violations": 0,
    "beneficial_retention_not_below_v1": True,
    "waste_block_rate_not_below_v1": True,
}
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def _rate(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"{name} must be in [0,1]")
    return result


def validate_continuation_admission(
    value: Mapping[str, object],
) -> dict[str, object]:
    expected = {
        "schema_version",
        "decision",
        "policy_version",
        "protocol",
        "thresholds",
        "metrics",
        "evidence_sha256",
    }
    if set(value) != expected:
        raise ValueError("Continuation admission fields mismatch")
    if value.get("schema_version") != CONTINUATION_ADMISSION_SCHEMA:
        raise ValueError("unsupported Continuation admission schema")
    if value.get("decision") != "PASS":
        raise ValueError("Continuation admission decision is not PASS")
    if value.get("policy_version") != CONTINUATION_POLICY_VERSION:
        raise ValueError("Continuation admission policy version mismatch")
    if value.get("protocol") != "MODE_SPECIFIC_PRE_STATE_ONLINE_SHADOW":
        raise ValueError("Continuation admission protocol mismatch")
    if value.get("thresholds") != CONTINUATION_FIXED_THRESHOLDS:
        raise ValueError("Continuation admission thresholds were changed")
    digest = value.get("evidence_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("Continuation admission evidence digest is invalid")
    metrics = value.get("metrics")
    expected_metrics = {
        "mode_counts",
        "beneficial_retention",
        "v1_beneficial_retention",
        "waste_block_rate",
        "v1_waste_block_rate",
        "false_blocks",
        "structural_essential_samples",
        "structural_essential_retention",
        "leakage_violations",
    }
    if not isinstance(metrics, Mapping) or set(metrics) != expected_metrics:
        raise ValueError("Continuation admission metrics mismatch")
    mode_counts = metrics.get("mode_counts")
    modes = {"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"}
    if not isinstance(mode_counts, Mapping) or set(mode_counts) != modes:
        raise ValueError("Continuation admission must cover all four modes")
    if any(
        isinstance(mode_counts[mode], bool)
        or not isinstance(mode_counts[mode], int)
        or mode_counts[mode]
        < CONTINUATION_FIXED_THRESHOLDS["minimum_samples_per_mode"]
        for mode in modes
    ):
        raise ValueError("Continuation per-mode sample Gate failed")
    retention = _rate(
        metrics.get("beneficial_retention"), "beneficial_retention"
    )
    v1_retention = _rate(
        metrics.get("v1_beneficial_retention"),
        "v1_beneficial_retention",
    )
    waste = _rate(metrics.get("waste_block_rate"), "waste_block_rate")
    v1_waste = _rate(
        metrics.get("v1_waste_block_rate"), "v1_waste_block_rate"
    )
    if retention < v1_retention:
        raise ValueError("Continuation beneficial retention regressed")
    if waste < v1_waste:
        raise ValueError("Continuation waste block rate regressed")
    for name in (
        "false_blocks",
        "structural_essential_samples",
        "leakage_violations",
    ):
        item = metrics.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"Continuation {name} is invalid")
    if (
        metrics["false_blocks"]
        > CONTINUATION_FIXED_THRESHOLDS["maximum_false_blocks"]
    ):
        raise ValueError("Continuation false-block Gate failed")
    if (
        metrics["structural_essential_samples"]
        < CONTINUATION_FIXED_THRESHOLDS[
            "minimum_structural_essential_samples"
        ]
    ):
        raise ValueError("Continuation structural sample Gate failed")
    if (
        _rate(
            metrics.get("structural_essential_retention"),
            "structural_essential_retention",
        )
        < CONTINUATION_FIXED_THRESHOLDS[
            "minimum_structural_essential_retention"
        ]
    ):
        raise ValueError("Continuation structural retention Gate failed")
    if (
        metrics["leakage_violations"]
        > CONTINUATION_FIXED_THRESHOLDS["maximum_leakage_violations"]
    ):
        raise ValueError("Continuation leakage Gate failed")
    copied = json.loads(canonical_json(value).decode("utf-8"))
    assert isinstance(copied, dict)
    return copied


def load_continuation_admission(path: str | Path) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("Continuation admission must be a JSON object")
    return validate_continuation_admission(value)


__all__ = [
    "CONTINUATION_ADMISSION_SCHEMA",
    "CONTINUATION_FIXED_THRESHOLDS",
    "CONTINUATION_POLICY_VERSION",
    "load_continuation_admission",
    "validate_continuation_admission",
]
