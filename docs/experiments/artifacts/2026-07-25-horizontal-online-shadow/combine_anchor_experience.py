#!/usr/bin/env python3
"""Append the four-Anchor online Shadow records to the frozen audited pool."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_v2 import validate_experience_v2


ROOT = Path(__file__).resolve().parents[4]
ARTIFACT_DIR = Path(__file__).resolve().parent
FROZEN_DIR = (
    ROOT
    / "docs/experiments/artifacts/2026-07-24-horizontal-components-online-admission/"
    "fresh-candidate-audits-rerun01"
)
ANCHOR_DIR = ARTIFACT_DIR / "four-anchor-experience-v2"
OUTPUT_RECORDS = ARTIFACT_DIR / "combined-experience-v2.jsonl"
OUTPUT_GROUPS = ARTIFACT_DIR / "combined-experience-v2-audit-groups.json"
OUTPUT_MANIFEST = ARTIFACT_DIR / "combined-experience-manifest.json"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        validate_experience_v2(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_groups(path: Path) -> dict[str, dict[str, object]]:
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


def _distribution(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def main() -> int:
    frozen = _read_jsonl(FROZEN_DIR / "experience-v2-audited.jsonl")
    anchors = _read_jsonl(ANCHOR_DIR / "experience-v2-reconciled.jsonl")
    frozen_ids = {str(item["record_id"]) for item in frozen}
    anchor_ids = {str(item["record_id"]) for item in anchors}
    if frozen_ids.intersection(anchor_ids):
        raise RuntimeError("frozen and Anchor record IDs overlap")

    records = sorted(frozen + anchors, key=lambda item: str(item["record_id"]))
    if len({str(item["record_id"]) for item in records}) != len(records):
        raise RuntimeError("combined record IDs are not unique")

    frozen_groups = _read_groups(
        FROZEN_DIR / "experience-v2-audited-audit-groups.json"
    )
    anchor_groups = _read_groups(
        ANCHOR_DIR / "experience-v2-reconciled-audit-groups.json"
    )
    if set(frozen_groups).intersection(anchor_groups):
        raise RuntimeError("frozen and Anchor audit groups overlap")
    groups = {**frozen_groups, **anchor_groups}
    if set(groups) != {str(item["record_id"]) for item in records}:
        raise RuntimeError("audit groups do not cover the combined record pool")

    OUTPUT_RECORDS.write_bytes(
        b"".join(canonical_json(item) + b"\n" for item in records)
    )
    OUTPUT_GROUPS.write_bytes(
        canonical_json(
            {
                "schema_version": "v3e.online-shadow-audit-groups.v1",
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
        "schema_version": "v3e.online-shadow-combined-experience.v1",
        "status": "PASS",
        "frozen_record_count": len(frozen),
        "anchor_record_count": len(anchors),
        "combined_record_count": len(records),
        "verified_record_count": len(verified),
        "verified_by_mode": _distribution(
            [str(item["problem"]["mode"]) for item in verified]
        ),
        "pool_policy": {
            "frozen_pool_mutated": False,
            "anchor_source": "FOUR_PUBLIC_DEEPSEEK_VITIS_SHADOW_RUNS",
            "main_graph_ingestion": False,
            "planner_ingestion": False,
            "guided_mode_enabled": False,
            "same_data_threshold_adjustment": False,
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
