#!/usr/bin/env python3
"""Combine the frozen C1 V2 pool with the isolated formal-matrix V2 pool."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_analysis import experience_outcome
from llm4hls_agent.v3_experience_v2 import validate_experience_v2


ARTIFACT_DIR = Path(__file__).resolve().parent
REPO = ARTIFACT_DIR.parents[3]
OLD_RECORDS = (
    REPO
    / "docs/experiments/artifacts/2026-07-23-c1-experience-record-v2/"
    "experience-record-v2-freeze.jsonl"
)
OLD_GROUPS = (
    REPO
    / "llm4hls_harness/experiments/v3e/kb_v2/"
    "experience_v2_audit_groups.json"
)
NEW_RECORDS = (
    ARTIFACT_DIR / "experience-v2-formal/experience_v2_backfill.jsonl"
)
NEW_GROUPS = (
    ARTIFACT_DIR / "experience-v2-formal/experience_v2_audit_groups.json"
)
COMBINED_RECORDS = ARTIFACT_DIR / "combined-experience-v2.jsonl"
COMBINED_GROUPS = ARTIFACT_DIR / "combined-experience-v2-audit-groups.json"
MANIFEST = ARTIFACT_DIR / "combined-experience-manifest.json"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        validate_experience_v2(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_groups(path: Path) -> dict[str, dict[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    groups = value.get("groups")
    if not isinstance(groups, dict):
        raise ValueError(f"invalid audit groups: {path}")
    return {
        str(key): dict(item)
        for key, item in groups.items()
        if isinstance(item, dict)
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def distribution(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def main() -> int:
    old = read_jsonl(OLD_RECORDS)
    new = read_jsonl(NEW_RECORDS)
    old_ids = {str(record["record_id"]) for record in old}
    new_ids = {str(record["record_id"]) for record in new}
    overlap = sorted(old_ids.intersection(new_ids))
    if overlap:
        raise RuntimeError("old/new record_id pools overlap")

    records = sorted(old + new, key=lambda item: str(item["record_id"]))
    if len({str(item["record_id"]) for item in records}) != len(records):
        raise RuntimeError("combined record_id values are not unique")
    source_keys = {
        (
            str(item["source"]["run_id"]),
            str(item["source"]["candidate_id"]),
            str(item["source"]["round_index"]),
        )
        for item in records
    }
    if len(source_keys) != len(records):
        raise RuntimeError("combined source identities are not unique")

    old_groups = read_groups(OLD_GROUPS)
    new_groups = read_groups(NEW_GROUPS)
    group_overlap = sorted(set(old_groups).intersection(new_groups))
    if group_overlap:
        raise RuntimeError("old/new audit-group record IDs overlap")
    groups = {**old_groups, **new_groups}
    if set(groups) != {str(item["record_id"]) for item in records}:
        raise RuntimeError("combined audit groups do not cover every record")

    COMBINED_RECORDS.write_bytes(
        b"".join(canonical_json(record) + b"\n" for record in records)
    )
    COMBINED_GROUPS.write_text(
        json.dumps(
            {
                "schema_version": "post-matrix.experience-audit-groups.v1",
                "groups": dict(sorted(groups.items())),
                "retrieval_feature": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    eligible = [
        item
        for item in records
        if item["provenance"]["eligible_for_ranking"] is True
    ]
    manifest = {
        "schema_version": "post-matrix.combined-experience.v1",
        "status": "PASS",
        "pool_policy": {
            "old_pool": "C1_FROZEN_103_UNCHANGED",
            "new_pool": "FORMAL_MATRIX_PUBLIC_TRAIN_ISOLATED",
            "record_id_overlap": 0,
            "source_identity_overlap": 0,
            "main_graph_ingestion": False,
            "planner_ingestion": False,
            "guided_mode_enabled": False,
        },
        "old_record_count": len(old),
        "new_record_count": len(new),
        "combined_record_count": len(records),
        "ranking_eligible_count": len(eligible),
        "mode_distribution": distribution(
            [str(item["problem"]["mode"]) for item in records]
        ),
        "ranking_eligible_by_mode": distribution(
            [str(item["problem"]["mode"]) for item in eligible]
        ),
        "outcome_distribution": distribution(
            [experience_outcome(item) for item in records]
        ),
        "ranking_eligible_outcome_distribution": distribution(
            [experience_outcome(item) for item in eligible]
        ),
        "files": {
            COMBINED_RECORDS.name: sha256(COMBINED_RECORDS),
            COMBINED_GROUPS.name: sha256(COMBINED_GROUPS),
        },
        "fixed_protocol": {
            "c2_threshold_tuning": False,
            "c2_threshold_source": (
                "2026-07-23-c2-bayesian-strategy-ranker-v2/"
                "c2_offline_evaluation.py"
            ),
            "same_data_threshold_adjustment": False,
        },
        "scope_guards": {
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "secret_value_recorded": False,
        },
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
