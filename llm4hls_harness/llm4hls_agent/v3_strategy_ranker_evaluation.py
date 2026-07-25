"""Fixed-protocol verified-only evaluation and admission for Ranker V3."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .v3_experience import canonical_json
from .v3_experience_kb import build_kb_query
from .v3_experience_v2 import STRATEGIES_BY_MODE, validate_experience_v2
from .v3_experience_v2_runtime import RANKER_ADMISSION_SCHEMA
from .v3_strategy_ranker_v3 import (
    STRATEGY_RANKER_V3_SCHEMA,
    BayesianStrategyRankerV3,
    verified_success,
)


RANKER_V3_EVALUATION_SCHEMA = "v3e.strategy-ranker-v3-evaluation.v1"
FIXED_THRESHOLDS = {
    "minimum_coverage": 0.40,
    "maximum_harmful_rate": 0.05,
    "minimum_records_per_mode": 10,
    "minimum_global_positive_hit_rate": 0.2727,
    "maximum_leakage_violations": 0,
}


def _query(record: Mapping[str, object]) -> dict[str, object]:
    """Build the decision input before reading held-out strategy/outcome fields."""

    source = record["source"]
    problem = record["problem"]
    structure = record["structure_features"]
    assert isinstance(source, Mapping)
    assert isinstance(problem, Mapping)
    assert isinstance(structure, Mapping)
    # Only pre-decision semantic shape is projected.  Candidate validation,
    # performance, costs, observed strategy and Patch identity are excluded.
    safe_structure = {
        name: structure.get(name)
        for name in (
            "has_dataflow",
            "has_stream",
            "has_fifo",
            "has_reduction",
            "memory_access_pattern",
            "resource_pressure",
        )
    }
    safe_structure.update(
        {
            "failure_stage": problem.get("failure_stage"),
            "primary_bottleneck": problem.get("primary_bottleneck"),
        }
    )
    return build_kb_query(
        mode=str(problem["mode"]),
        task_split=str(source["task_split"]),
        failure_subtype=str(problem["failure_subtype"]),
        bottleneck_subtype=str(problem["bottleneck_subtype"]),
        algorithm_family=str(source["algorithm_family"]),
        task_family_hash=str(source["task_family_hash"]),
        structure_features=safe_structure,
        strategy_context=(),
        current_run_id=str(source["run_id"]),
        current_candidate_id=None,
        current_patch_digest=None,
        toolchain=str(source["toolchain"]),
        backend_fingerprint=str(source["backend_fingerprint"]),
        prompt_version=str(source["prompt_version"]),
        exclude_same_task_family=False,
    )


def _summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    total = len(rows)
    recommendations = sum(row["decision"] == "RECOMMEND" for row in rows)
    positives = sum(row["actual_positive"] is True for row in rows)
    negatives = total - positives
    hits = sum(row["positive_strategy_hit"] is True for row in rows)
    harmful = sum(row["harmful_recommendation"] is True for row in rows)
    leakages = sum(row["support_group_leakage"] is True for row in rows)
    return {
        "queries": total,
        "recommend_count": recommendations,
        "abstain_count": total - recommendations,
        "coverage": round(recommendations / total, 8) if total else 0.0,
        "positive_queries": positives,
        "negative_queries": negatives,
        "positive_strategy_hit_rate": (
            round(hits / positives, 8) if positives else 0.0
        ),
        "harmful_recommendation_rate": (
            round(harmful / recommendations, 8)
            if recommendations
            else 0.0
        ),
        "leakage_violations": leakages,
        "decision_distribution": dict(
            sorted(Counter(str(row["decision"]) for row in rows).items())
        ),
        "abstain_reasons": dict(
            sorted(
                Counter(
                    str(row["abstain_reason"])
                    for row in rows
                    if row["abstain_reason"] is not None
                ).items()
            )
        ),
    }


def _evaluate_policy(
    name: str,
    records: Sequence[Mapping[str, object]],
    group: Callable[[Mapping[str, object]], str | None],
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    skipped = 0
    ranker = BayesianStrategyRankerV3()
    for held_record in records:
        held_group = group(held_record)
        if held_group is None:
            skipped += 1
            continue
        support = [record for record in records if group(record) != held_group]
        leakage = any(group(record) == held_group for record in support)

        # Decision boundary: held-out strategy and final result are first read
        # only after rank() has returned.
        decision = ranker.rank(_query(held_record), support)

        strategy = held_record["strategy"]
        problem = held_record["problem"]
        source = held_record["source"]
        assert isinstance(strategy, Mapping)
        assert isinstance(problem, Mapping)
        assert isinstance(source, Mapping)
        actual_atoms = {
            str(atom) for atom in strategy["observed_strategy_atoms"]
        }
        actual_positive = verified_success(held_record) is True
        recommended = decision["recommended"]
        recommended_atom = (
            str(recommended["strategy_atom"])
            if isinstance(recommended, Mapping)
            else None
        )
        recommend = decision["decision"] == "RECOMMEND"
        match = bool(
            recommended_atom in actual_atoms if recommended_atom else False
        )
        rows.append(
            {
                "record_id": held_record["record_id"],
                "mode": problem["mode"],
                "algorithm_family": source["algorithm_family"],
                "held_out_group": held_group,
                "support_record_count": len(support),
                "support_group_leakage": leakage,
                "decision": decision["decision"],
                "abstain_reason": decision["abstain_reason"],
                "recommended_strategy_atom": recommended_atom,
                "recommended_posterior": (
                    recommended["posterior_success"]
                    if isinstance(recommended, Mapping)
                    else None
                ),
                "recommended_lower_bound": (
                    recommended["lower_bound"]
                    if isinstance(recommended, Mapping)
                    else None
                ),
                "actual_strategy_atoms": sorted(actual_atoms),
                "actual_positive": actual_positive,
                "positive_strategy_hit": bool(
                    recommend and actual_positive and match
                ),
                "harmful_recommendation": bool(
                    recommend and not actual_positive and match
                ),
            }
        )
    rows.sort(key=lambda item: str(item["record_id"]))
    by_mode: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        by_mode[str(row["mode"])].append(row)
    return {
        "policy": name,
        "overall": _summary(rows),
        "by_mode": {
            mode: _summary(mode_rows)
            for mode, mode_rows in sorted(by_mode.items())
        },
        "skipped_missing_group": skipped,
        "rows": rows,
    }


def evaluate_ranker_v3(
    raw_records: Sequence[Mapping[str, object]],
    audit_groups: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    eligible = [
        validate_experience_v2(record)
        for record in raw_records
        if record.get("source", {}).get("evidence_level")
        == "REAL_LLM_VITIS"
        and record.get("provenance", {}).get("eligible_for_ranking") is True
    ]
    verified = [
        record for record in eligible if verified_success(record) is not None
    ]
    verified.sort(key=lambda item: str(item["record_id"]))

    def task_group(record: Mapping[str, object]) -> str | None:
        value = audit_groups.get(str(record["record_id"]))
        return (
            str(value["task_audit_hash"])
            if value and value.get("task_audit_hash")
            else None
        )

    def family_group(record: Mapping[str, object]) -> str:
        return str(record["source"]["task_family_hash"])

    policies = {
        "leave_one_task_out": _evaluate_policy(
            "leave_one_task_out", verified, task_group
        ),
        "leave_one_task_family_out": _evaluate_policy(
            "leave_one_task_family_out", verified, family_group
        ),
    }
    mode_counts = Counter(
        str(record["problem"]["mode"]) for record in verified
    )
    policy_values = list(policies.values())
    coverage_pass = all(
        float(policy["overall"]["coverage"])
        >= FIXED_THRESHOLDS["minimum_coverage"]
        for policy in policy_values
    )
    harmful_pass = all(
        float(policy["overall"]["harmful_recommendation_rate"])
        <= FIXED_THRESHOLDS["maximum_harmful_rate"]
        for policy in policy_values
    ) and all(
        float(metrics["harmful_recommendation_rate"])
        <= FIXED_THRESHOLDS["maximum_harmful_rate"]
        for policy in policy_values
        for metrics in policy["by_mode"].values()
    )
    mode_count_pass = all(
        mode_counts.get(mode, 0)
        >= FIXED_THRESHOLDS["minimum_records_per_mode"]
        for mode in STRATEGIES_BY_MODE
    )
    positive_hit_pass = all(
        float(policy["overall"]["positive_strategy_hit_rate"])
        >= FIXED_THRESHOLDS["minimum_global_positive_hit_rate"]
        for policy in policy_values
    )
    leakage_violations = sum(
        int(policy["overall"]["leakage_violations"])
        for policy in policy_values
    )
    leakage_pass = (
        leakage_violations
        <= FIXED_THRESHOLDS["maximum_leakage_violations"]
    )
    decision = (
        "PASS"
        if all(
            (
                coverage_pass,
                harmful_pass,
                mode_count_pass,
                positive_hit_pass,
                leakage_pass,
            )
        )
        else "FAIL"
    )
    return {
        "schema_version": RANKER_V3_EVALUATION_SCHEMA,
        "decision": decision,
        "authority": (
            "ELIGIBLE_FOR_ADMISSION" if decision == "PASS" else "SHADOW_ONLY"
        ),
        "ranker_version": STRATEGY_RANKER_V3_SCHEMA,
        "fixed_protocol": "LOTO_AND_LEAVE_ONE_TASK_FAMILY_OUT",
        "thresholds": FIXED_THRESHOLDS,
        "input": {
            "total_records": len(raw_records),
            "ranking_eligible_records": len(eligible),
            "verified_records": len(verified),
            "excluded_unverified_records": len(eligible) - len(verified),
            "verified_by_mode": dict(sorted(mode_counts.items())),
        },
        "policies": policies,
        "checks": {
            "coverage_gate_pass": coverage_pass,
            "harmful_rate_gate_pass": harmful_pass,
            "mode_count_gate_pass": mode_count_pass,
            "positive_hit_gate_pass": positive_hit_pass,
            "leakage_gate_pass": leakage_pass,
            "unverified_labels_excluded": True,
            "family_level_sampling": True,
            "same_data_threshold_tuning": False,
            "query_outcome_fields_present": False,
            "heldout_labels_read_after_decision": True,
        },
    }


def admission_manifest(
    report: Mapping[str, object],
    *,
    seed_sha256: str,
    evidence_sha256: str,
) -> dict[str, object]:
    if report.get("decision") != "PASS":
        raise ValueError("cannot admit a Ranker V3 evaluation that did not PASS")
    policies = report["policies"]
    assert isinstance(policies, Mapping)
    task = policies["leave_one_task_out"]
    family = policies["leave_one_task_family_out"]
    assert isinstance(task, Mapping) and isinstance(family, Mapping)
    by_mode: dict[str, object] = {}
    counts = report["input"]["verified_by_mode"]
    for mode in sorted(STRATEGIES_BY_MODE):
        task_row = task["by_mode"][mode]
        family_row = family["by_mode"][mode]
        by_mode[mode] = {
            "records": int(counts[mode]),
            "coverage": min(
                float(task_row["coverage"]), float(family_row["coverage"])
            ),
            "harmful_rate": max(
                float(task_row["harmful_recommendation_rate"]),
                float(family_row["harmful_recommendation_rate"]),
            ),
            "positive_hit_rate": min(
                float(task_row["positive_strategy_hit_rate"]),
                float(family_row["positive_strategy_hit_rate"]),
            ),
        }
    return {
        "schema_version": RANKER_ADMISSION_SCHEMA,
        "decision": "PASS",
        "ranker_version": STRATEGY_RANKER_V3_SCHEMA,
        "seed_sha256": seed_sha256,
        "protocol": "LOTO_AND_LEAVE_ONE_TASK_FAMILY_OUT",
        "thresholds": FIXED_THRESHOLDS,
        "metrics": {
            "coverage": min(
                float(task["overall"]["coverage"]),
                float(family["overall"]["coverage"]),
            ),
            "harmful_rate": max(
                float(task["overall"]["harmful_recommendation_rate"]),
                float(family["overall"]["harmful_recommendation_rate"]),
            ),
            "global_positive_hit_rate": min(
                float(task["overall"]["positive_strategy_hit_rate"]),
                float(family["overall"]["positive_strategy_hit_rate"]),
            ),
            "leakage_violations": sum(
                int(policy["overall"]["leakage_violations"])
                for policy in (task, family)
            ),
            "by_mode": by_mode,
        },
        "evidence_sha256": evidence_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--audit-groups", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admission-output", type=Path)
    args = parser.parse_args(argv)
    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    groups_value = json.loads(
        args.audit_groups.read_text(encoding="utf-8")
    )
    groups = groups_value.get("groups")
    if not isinstance(groups, Mapping):
        raise ValueError("audit groups file is malformed")
    report = evaluate_ranker_v3(records, groups)
    encoded = canonical_json(report) + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    if args.admission_output is not None and report["decision"] == "PASS":
        manifest = admission_manifest(
            report,
            seed_sha256=hashlib.sha256(args.records.read_bytes()).hexdigest(),
            evidence_sha256=hashlib.sha256(encoded).hexdigest(),
        )
        args.admission_output.parent.mkdir(parents=True, exist_ok=True)
        args.admission_output.write_bytes(canonical_json(manifest) + b"\n")
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "input": report["input"],
                "checks": report["checks"],
                "policies": {
                    name: policy["overall"]
                    for name, policy in report["policies"].items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["decision"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FIXED_THRESHOLDS",
    "RANKER_V3_EVALUATION_SCHEMA",
    "admission_manifest",
    "evaluate_ranker_v3",
]
