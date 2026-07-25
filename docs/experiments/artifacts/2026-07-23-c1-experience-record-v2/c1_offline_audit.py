#!/usr/bin/env python3
"""Freeze and audit the existing public-only Experience Record V2 store."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_analysis import experience_outcome
from llm4hls_agent.v3_experience_v2 import validate_experience_v2


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def audit(store: Path) -> tuple[dict[str, object], bytes]:
    raw = store.read_bytes()
    records = [
        validate_experience_v2(json.loads(line))
        for line in raw.decode("utf-8").splitlines()
        if line.strip()
    ]
    canonical = b"".join(canonical_json(record) + b"\n" for record in records)

    record_ids = [str(record["record_id"]) for record in records]
    source_keys = [
        "|".join(
            (
                str(record["source"]["run_id"]),
                str(record["source"]["candidate_id"]),
                str(record["source"]["round_index"]),
            )
        )
        for record in records
    ]
    serialized = canonical.decode("utf-8").lower()
    forbidden_secret_hits = [
        marker
        for marker in (
            "/home/",
            "api_key",
            "api-key",
            "authorization",
            "bearer ",
        )
        if marker in serialized
    ]
    if re.search(r"(?<![a-z0-9])sk-[a-z0-9]{8,}", serialized):
        forbidden_secret_hits.append("secret-key-pattern")
    forbidden_artifact_refs: list[str] = []
    for record in records:
        for artifact in record["provenance"]["artifact_refs"]:
            ref = str(artifact["ref"])
            segments = set(ref.lower().split("/"))
            if not segments.isdisjoint({"hidden", "reference", "golden"}):
                forbidden_artifact_refs.append(ref)

    ranking = [
        record
        for record in records
        if record["provenance"]["eligible_for_ranking"] is True
    ]
    summary: dict[str, object] = {
        "schema_version": "c1.experience-record-v2-offline-audit.v1",
        "component_state": "PASS_PROMOTABLE",
        "authority": "SHADOW",
        "guided": "NOT_ADMITTED",
        "source_store": store.as_posix(),
        "record_count": len(records),
        "valid_record_count": len(records),
        "record_id_unique_count": len(set(record_ids)),
        "source_identity_unique_count": len(set(source_keys)),
        "canonical_store_match": raw == canonical,
        "store_sha256": _sha256(raw),
        "canonical_sha256": _sha256(canonical),
        "mode_distribution": _counter(
            [str(record["problem"]["mode"]) for record in records]
        ),
        "outcome_distribution": _counter(
            [experience_outcome(record) for record in records]
        ),
        "evidence_distribution": _counter(
            [str(record["source"]["evidence_level"]) for record in records]
        ),
        "task_split_distribution": _counter(
            [str(record["source"]["task_split"]) for record in records]
        ),
        "run_count": len({str(record["source"]["run_id"]) for record in records}),
        "task_family_count": len(
            {str(record["source"]["task_family_hash"]) for record in records}
        ),
        "ranking_eligible_count": len(ranking),
        "ranking_eligible_by_mode": _counter(
            [str(record["problem"]["mode"]) for record in ranking]
        ),
        "retrieval_eligible_count": sum(
            record["provenance"]["eligible_for_retrieval"] is True
            for record in records
        ),
        "forbidden_secret_hits": forbidden_secret_hits,
        "forbidden_artifact_refs": sorted(forbidden_artifact_refs),
        "future_outcome_input_status": "NOT_APPLICABLE_RECORD_AUDIT_ONLY",
        "downstream_contract": {
            "query_must_exclude": [
                "validation",
                "performance",
                "cost",
                "outcome",
                "promoted",
                "rejected",
            ],
            "labels_may_be_read_only_after_decision": True,
        },
        "real_budget": {
            "llm_calls": 0,
            "tokens": 0,
            "csim": 0,
            "synth": 0,
            "cosim": 0,
            "tool_credits": 0,
        },
    }
    checks = {
        "all_records_valid": summary["valid_record_count"] == summary["record_count"],
        "record_ids_unique": summary["record_id_unique_count"] == summary["record_count"],
        "source_identities_unique": (
            summary["source_identity_unique_count"] == summary["record_count"]
        ),
        "canonical_store_stable": summary["canonical_store_match"],
        "all_four_modes_present": set(summary["mode_distribution"])
        == {"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"},
        "only_real_evidence": summary["evidence_distribution"]
        == {"REAL_LLM_VITIS": len(records)},
        "no_secret_marker": not forbidden_secret_hits,
        "no_forbidden_artifact_ref": not forbidden_artifact_refs,
        "ranking_data_available": len(ranking) > 0,
    }
    summary["checks"] = checks
    summary["valid_for_offline_downstream"] = all(checks.values())
    if not summary["valid_for_offline_downstream"]:
        summary["component_state"] = "BLOCKED_ENGINEERING"
    return summary, canonical


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary, canonical = audit(args.store)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "experience-record-v2-freeze.jsonl").write_bytes(canonical)
    (args.output_dir / "c1-audit-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["valid_for_offline_downstream"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
