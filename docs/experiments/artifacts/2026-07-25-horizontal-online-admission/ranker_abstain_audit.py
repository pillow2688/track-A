#!/usr/bin/env python3
"""Audit fixed-protocol Ranker V3 abstentions without changing thresholds."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_v2 import validate_experience_v2
from llm4hls_agent.v3_strategy_ranker_evaluation import _query
from llm4hls_agent.v3_strategy_ranker_v3 import (
    BayesianStrategyRankerV3,
    verified_success,
)


ARTIFACT_DIR = Path(__file__).resolve().parent
RECORDS = ARTIFACT_DIR / "gate-combined-experience-v2.jsonl"
OUTPUT = ARTIFACT_DIR / "ranker-v3-abstain-audit.json"


def main() -> int:
    records = [
        validate_experience_v2(json.loads(line))
        for line in RECORDS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    verified = [
        record
        for record in records
        if record["source"]["evidence_level"] == "REAL_LLM_VITIS"
        and record["provenance"]["eligible_for_ranking"] is True
        and verified_success(record) is not None
    ]
    ranker = BayesianStrategyRankerV3()
    rows = []
    for held in verified:
        held_group = str(held["source"]["task_family_hash"])
        support = [
            record
            for record in verified
            if record["source"]["task_family_hash"] != held_group
        ]
        decision = ranker.rank(_query(held), support)
        if decision["decision"] != "ABSTAIN":
            continue
        entries = [
            {
                "strategy_atom": entry["strategy_atom"],
                "conditioning": entry["conditioning"],
                "eligible": entry["eligible_for_recommendation"],
                "attempts": entry["attempts"],
                "successes": entry["successes"],
                "failures": entry["failures"],
                "patch_digest_count": entry["patch_digest_count"],
                "posterior_success": entry["posterior_success"],
                "lower_bound": entry["lower_bound"],
                "ineligibility_reasons": entry["ineligibility_reasons"],
            }
            for entry in decision["all_atoms"][:4]
        ]
        rows.append(
            {
                "record_id": held["record_id"],
                "mode": held["problem"]["mode"],
                "subtype": (
                    held["problem"]["bottleneck_subtype"]
                    if held["problem"]["mode"] == "OPTIMIZE"
                    else held["problem"]["failure_subtype"]
                ),
                "algorithm_family": held["source"]["algorithm_family"],
                "abstain_reason": decision["abstain_reason"],
                "top_entries": entries,
                # Held-out labels are appended only after rank() returns.
                "actual_positive": verified_success(held) is True,
                "actual_atoms": held["strategy"]["observed_strategy_atoms"],
            }
        )
    output = {
        "schema_version": "v3e.strategy-ranker-v3-abstain-audit.v1",
        "protocol": "LEAVE_ONE_TASK_FAMILY_OUT",
        "thresholds_changed": False,
        "abstain_count": len(rows),
        "abstain_reasons": dict(
            sorted(Counter(row["abstain_reason"] for row in rows).items())
        ),
        "rows": sorted(rows, key=lambda row: str(row["record_id"])),
    }
    OUTPUT.write_bytes(canonical_json(output) + b"\n")
    print(
        json.dumps(
            {
                key: output[key]
                for key in (
                    "schema_version",
                    "protocol",
                    "thresholds_changed",
                    "abstain_count",
                    "abstain_reasons",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
