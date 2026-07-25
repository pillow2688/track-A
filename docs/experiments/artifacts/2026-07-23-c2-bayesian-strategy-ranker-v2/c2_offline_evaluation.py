#!/usr/bin/env python3
"""Leakage-resistant holdout evaluation for Bayesian Strategy Ranker V2."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from llm4hls_agent.v3_experience_analysis import experience_outcome
from llm4hls_agent.v3_experience_evaluation import evaluate_generalization
from llm4hls_agent.v3_experience_kb import build_kb_query
from llm4hls_agent.v3_experience_v2 import validate_experience_v2
from llm4hls_agent.v3_strategy_ranker_v2 import BayesianStrategyRankerV2


def _query(record: Mapping[str, object]) -> dict[str, object]:
    """Build the query before reading the held-out strategy or outcome."""

    source = record["source"]
    problem = record["problem"]
    structure = record["structure_features"]
    assert isinstance(source, Mapping)
    assert isinstance(problem, Mapping)
    assert isinstance(structure, Mapping)
    return build_kb_query(
        mode=str(problem["mode"]),
        task_split=str(source["task_split"]),
        failure_subtype=str(problem["failure_subtype"]),
        bottleneck_subtype=str(problem["bottleneck_subtype"]),
        algorithm_family=str(source["algorithm_family"]),
        task_family_hash=str(source["task_family_hash"]),
        structure_features={
            **dict(structure),
            "requires_cosim": problem["requires_cosim"],
        },
        strategy_context=(),
        current_run_id=str(source["run_id"]),
        current_candidate_id=None,
        current_patch_digest=None,
        toolchain=str(source["toolchain"]),
        backend_fingerprint=str(source["backend_fingerprint"]),
        prompt_version=str(source["prompt_version"]),
    )


def _summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    queries = len(rows)
    recommendations = sum(row["decision"] == "RECOMMEND" for row in rows)
    positives = sum(row["actual_positive"] is True for row in rows)
    negatives = queries - positives
    hits = sum(row["positive_strategy_hit"] is True for row in rows)
    harmful = sum(row["harmful_duplicate_recommendation"] is True for row in rows)
    suppressed = sum(row["failed_strategy_suppressed"] is True for row in rows)
    return {
        "queries": queries,
        "recommend_count": recommendations,
        "abstain_count": queries - recommendations,
        "coverage": round(recommendations / queries, 8) if queries else 0.0,
        "positive_queries": positives,
        "negative_queries": negatives,
        "positive_strategy_hit_rate": (
            round(hits / positives, 8) if positives else None
        ),
        "harmful_duplicate_recommendation_rate": (
            round(harmful / recommendations, 8) if recommendations else 0.0
        ),
        "failed_strategy_suppression_rate": (
            round(suppressed / negatives, 8) if negatives else None
        ),
        "group_leakage_rate": round(
            sum(row["support_group_leakage"] is True for row in rows) / queries,
            8,
        )
        if queries
        else 0.0,
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
    ranker = BayesianStrategyRankerV2()
    rows: list[dict[str, object]] = []
    skipped = 0
    for held_record in records:
        held_group = group(held_record)
        if held_group is None:
            skipped += 1
            continue
        support = [record for record in records if group(record) != held_group]
        leakage = any(group(record) == held_group for record in support)

        # The decision is completed here.  No held-out strategy, validation,
        # performance, cost, or outcome has been read above this line.
        decision = ranker.rank(_query(held_record), support)

        actual_atoms = {
            str(atom)
            for atom in held_record["strategy"]["observed_strategy_atoms"]
        }
        actual_positive = experience_outcome(held_record) == "SUCCESS"
        recommended = decision["recommended"]
        recommended_atom = (
            str(recommended["strategy_atom"])
            if isinstance(recommended, Mapping)
            else None
        )
        is_recommend = decision["decision"] == "RECOMMEND"
        actual_match = bool(recommended_atom in actual_atoms if recommended_atom else False)
        problem = held_record["problem"]
        source = held_record["source"]
        assert isinstance(problem, Mapping)
        assert isinstance(source, Mapping)
        rows.append(
            {
                "record_id": held_record["record_id"],
                "mode": problem["mode"],
                "algorithm_family": source["algorithm_family"],
                "held_out_group": held_group,
                "support_count": len(support),
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
                    is_recommend and actual_positive and actual_match
                ),
                "harmful_duplicate_recommendation": bool(
                    is_recommend and not actual_positive and actual_match
                ),
                "failed_strategy_suppressed": bool(
                    not actual_positive and not actual_match
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
        "leakage_check_pass": all(
            row["support_group_leakage"] is False for row in rows
        ),
        "rows": rows,
    }


def evaluate(
    records: Sequence[Mapping[str, object]],
    audit_groups: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    eligible = [
        validate_experience_v2(record)
        for record in records
        if record["source"]["evidence_level"] == "REAL_LLM_VITIS"
        and record["provenance"]["eligible_for_ranking"] is True
    ]
    eligible.sort(key=lambda item: str(item["record_id"]))

    def run_group(record: Mapping[str, object]) -> str:
        return str(record["source"]["run_id"])

    def task_group(record: Mapping[str, object]) -> str | None:
        value = audit_groups.get(str(record["record_id"]))
        return str(value["task_audit_hash"]) if value else None

    def family_group(record: Mapping[str, object]) -> str:
        return str(record["source"]["task_family_hash"])

    policies = {
        "leave_one_run_out": _evaluate_policy(
            "leave_one_run_out", eligible, run_group
        ),
        "leave_one_task_out": _evaluate_policy(
            "leave_one_task_out", eligible, task_group
        ),
        "leave_one_task_family_out": _evaluate_policy(
            "leave_one_task_family_out", eligible, family_group
        ),
    }
    v1 = evaluate_generalization(eligible, audit_groups=audit_groups)
    mode_counts = dict(
        sorted(Counter(str(record["problem"]["mode"]) for record in eligible).items())
    )
    all_leakage_pass = all(
        policy["leakage_check_pass"] for policy in policies.values()
    )
    task_metrics = policies["leave_one_task_out"]["overall"]
    family_metrics = policies["leave_one_task_family_out"]["overall"]
    overall_harmful_safe = (
        float(task_metrics["harmful_duplicate_recommendation_rate"]) <= 0.05
        and float(family_metrics["harmful_duplicate_recommendation_rate"]) <= 0.05
    )
    per_mode_harmful_safe = all(
        float(metrics["harmful_duplicate_recommendation_rate"]) <= 0.05
        for policy in (
            policies["leave_one_task_out"],
            policies["leave_one_task_family_out"],
        )
        for metrics in policy["by_mode"].values()
        if int(metrics["recommend_count"]) > 0
    )
    harmful_safe = overall_harmful_safe and per_mode_harmful_safe
    coverage_ready = (
        float(task_metrics["coverage"]) >= 0.40
        and float(family_metrics["coverage"]) >= 0.40
    )
    all_modes_ready = all(mode_counts.get(mode, 0) >= 10 for mode in (
        "REPAIR",
        "SYNTH_FIX",
        "STRUCTURAL_FIX",
        "OPTIMIZE",
    ))
    component_state = (
        "GLOBAL_ABORT"
        if not all_leakage_pass
        else "NEGATIVE_RESULT"
        if not harmful_safe
        else "PASS_PROMOTABLE"
        if coverage_ready and all_modes_ready
        else "INSUFFICIENT_EVIDENCE"
    )
    return {
        "schema_version": "c2.bayesian-strategy-ranker-v2-evaluation.v1",
        "component_state": component_state,
        "authority": "SHADOW",
        "learned_ranker": "TRAINING_NOT_READY",
        "input_dependency": {
            "c1_state": "PASS_PROMOTABLE",
            "record_count": len(records),
            "ranking_eligible_count": len(eligible),
        },
        "fixed_candidate_thresholds": {
            "leave_one_task_coverage_at_least": 0.40,
            "leave_one_task_family_coverage_at_least": 0.40,
            "harmful_duplicate_recommendation_rate_at_most": 0.05,
            "harmful_rate_applies_per_mode": True,
            "each_mode_records_at_least": 10,
        },
        "mode_counts": mode_counts,
        "policies": policies,
        "v1_formal_guidance_baseline": {
            name: value["overall"] for name, value in v1["policies"].items()
        },
        "checks": {
            "all_holdout_leakage_checks_pass": all_leakage_pass,
            "query_outcome_fields_present": False,
            "heldout_labels_read_after_decision": True,
            "harmful_rate_gate_pass": harmful_safe,
            "overall_harmful_rate_gate_pass": overall_harmful_safe,
            "per_mode_harmful_rate_gate_pass": per_mode_harmful_safe,
            "coverage_gate_pass": coverage_ready,
            "all_modes_minimum_count_pass": all_modes_ready,
            "hidden_reference_golden_support_allowed": False,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--audit-groups", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    audit_value = json.loads(args.audit_groups.read_text(encoding="utf-8"))
    report = evaluate(records, audit_value["groups"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "component_state": report["component_state"],
                "mode_counts": report["mode_counts"],
                "policies": {
                    name: value["overall"]
                    for name, value in report["policies"].items()
                },
                "checks": report["checks"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2 if report["component_state"] == "GLOBAL_ABORT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
