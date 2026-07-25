#!/usr/bin/env python3
"""Combine the immutable audited pool with the new public Gate increment."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_v2 import validate_experience_v2


ROOT = Path(__file__).resolve().parents[4]
ARTIFACT_DIR = Path(__file__).resolve().parent
BASE_DIR = (
    ROOT
    / "docs/experiments/artifacts/2026-07-25-horizontal-online-shadow"
)
INCREMENT_DIR = ARTIFACT_DIR / "gate-experience-increment"
OUTPUT_RECORDS = ARTIFACT_DIR / "gate-combined-experience-v2.jsonl"
OUTPUT_GROUPS = ARTIFACT_DIR / "gate-combined-experience-v2-audit-groups.json"
OUTPUT_MANIFEST = ARTIFACT_DIR / "gate-combined-experience-manifest.json"


def _records(path: Path) -> list[dict[str, object]]:
    return [
        validate_experience_v2(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _groups(path: Path) -> dict[str, dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    groups = value.get("groups")
    if not isinstance(groups, dict):
        raise ValueError(f"invalid audit groups: {path}")
    return {
        str(record_id): dict(group)
        for record_id, group in groups.items()
        if isinstance(group, dict)
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    base = _records(BASE_DIR / "combined-experience-v2.jsonl")
    increment = _records(INCREMENT_DIR / "experience-v2-reconciled.jsonl")
    base_ids = {str(item["record_id"]) for item in base}
    increment_ids = {str(item["record_id"]) for item in increment}
    if base_ids & increment_ids:
        raise RuntimeError("base and increment record IDs overlap")

    records = sorted(base + increment, key=lambda item: str(item["record_id"]))
    groups = {
        **_groups(BASE_DIR / "combined-experience-v2-audit-groups.json"),
        **_groups(
            INCREMENT_DIR / "experience-v2-reconciled-audit-groups.json"
        ),
    }
    record_ids = {str(item["record_id"]) for item in records}
    if len(record_ids) != len(records) or set(groups) != record_ids:
        raise RuntimeError("combined records/audit groups are not one-to-one")

    OUTPUT_RECORDS.write_bytes(
        b"".join(canonical_json(item) + b"\n" for item in records)
    )
    OUTPUT_GROUPS.write_bytes(
        canonical_json(
            {
                "schema_version": "v3e.online-admission-audit-groups.v1",
                "groups": dict(sorted(groups.items())),
                "retrieval_feature": False,
            }
        )
        + b"\n"
    )
    verified = [
        item
        for item in records
        if item["provenance"]["eligible_for_ranking"] is True
        and item["validation"]["fresh_final_status"] in {"PASS", "FAIL"}
    ]
    manifest = {
        "schema_version": "v3e.online-admission-combined-experience.v1",
        "status": "PASS",
        "base_record_count": len(base),
        "increment_record_count": len(increment),
        "combined_record_count": len(records),
        "verified_record_count": len(verified),
        "verified_by_mode": dict(
            sorted(
                Counter(
                    str(item["problem"]["mode"]) for item in verified
                ).items()
            )
        ),
        "pool_policy": {
            "base_pool_mutated": False,
            "increment_source": "SEVEN_EXPLICIT_PUBLIC_TERMINAL_RUNS",
            "failed_debug_runs_included": False,
            "same_data_threshold_adjustment": False,
            "guided_mode_enabled_during_collection": False,
        },
        "scope_guards": {
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "secret_value_recorded": False,
        },
        "files": {
            OUTPUT_RECORDS.name: _sha256(OUTPUT_RECORDS),
            OUTPUT_GROUPS.name: _sha256(OUTPUT_GROUPS),
        },
    }
    OUTPUT_MANIFEST.write_bytes(canonical_json(manifest) + b"\n")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
