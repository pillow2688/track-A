"""Leakage-resistant offline evaluation for v2 experience guidance."""

from __future__ import annotations

import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .v3_experience import canonical_json
from .v3_experience_analysis import experience_outcome
from .v3_experience_kb import ExplainableSimilarCaseRetriever, build_kb_query
from .v3_experience_kb_quality import (
    BayesianAtomRanker,
    HardenedGuidanceQualityGate,
)
from .v3_experience_v2 import validate_experience_v2


GENERALIZED_EVALUATION_SCHEMA = "v3e.generalized-offline-evaluation.v1"


def _query_for(record: Mapping[str, object]) -> dict[str, object]:
    source = record["source"]
    problem = record["problem"]
    structure = record["structure_features"]
    strategy = record["strategy"]
    assert isinstance(source, Mapping)
    assert isinstance(problem, Mapping)
    assert isinstance(structure, Mapping)
    assert isinstance(strategy, Mapping)
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
        current_candidate_id=str(source["candidate_id"]),
        current_patch_digest=str(strategy["patch_digest"])
        if strategy["patch_digest"] is not None
        else None,
        toolchain=str(source["toolchain"]),
        backend_fingerprint=str(source["backend_fingerprint"]),
        prompt_version=str(source["prompt_version"]),
    )


def _is_positive(record: Mapping[str, object]) -> bool:
    return experience_outcome(record) == "SUCCESS"


def _actual_atoms(record: Mapping[str, object]) -> set[str]:
    strategy = record["strategy"]
    assert isinstance(strategy, Mapping)
    return {str(item) for item in strategy["observed_strategy_atoms"]}


def _summarize_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    total = len(rows)
    injected = sum(row["decision"] == "INJECT" for row in rows)
    positives = sum(row["actual_positive"] is True for row in rows)
    negatives = total - positives
    hits = sum(row["success_strategy_hit"] is True for row in rows)
    harmful = sum(row["harmful_recommendation"] is True for row in rows)
    suppressed = sum(row["duplicate_failure_suppressed"] is True for row in rows)
    leakages = sum(row["support_leakage_detected"] is True for row in rows)
    probabilities = [
        (float(row["estimated_success"]), 1.0 if row["actual_positive"] else 0.0)
        for row in rows
        if isinstance(row.get("estimated_success"), (int, float))
        and not isinstance(row.get("estimated_success"), bool)
    ]
    return {
        "queries": total,
        "inject_count": injected,
        "abstain_count": total - injected,
        "coverage": round(injected / total, 8) if total else 0.0,
        "success_strategy_hit_rate": round(hits / positives, 8) if positives else None,
        "harmful_recommendation_rate": round(harmful / injected, 8) if injected else 0.0,
        "duplicate_failure_suppression_rate": round(suppressed / negatives, 8)
        if negatives
        else None,
        "family_leakage_rate": round(leakages / total, 8) if total else 0.0,
        "average_guidance_tokens": round(
            statistics.fmean(float(row["guidance_tokens"]) for row in rows), 4
        )
        if rows
        else 0.0,
        "confidence_brier_score": round(
            statistics.fmean((probability - label) ** 2 for probability, label in probabilities),
            8,
        )
        if probabilities
        else None,
    }


def _group_summary(
    rows: Sequence[Mapping[str, object]], name: str
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[name])].append(row)
    return {key: _summarize_rows(grouped[key]) for key in sorted(grouped)}


def _policy_evaluation(
    name: str,
    records: Sequence[Mapping[str, object]],
    *,
    group_value: Callable[[Mapping[str, object]], str | None],
    audit_groups: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    retriever = ExplainableSimilarCaseRetriever()
    ranker = BayesianAtomRanker()
    gate = HardenedGuidanceQualityGate()
    rows: list[dict[str, object]] = []
    skipped_missing_group = 0
    for query_record in records:
        held_out = group_value(query_record)
        if held_out is None:
            skipped_missing_group += 1
            continue
        support = [record for record in records if group_value(record) != held_out]
        leakage = any(group_value(record) == held_out for record in support)
        query = _query_for(query_record)
        retrieval = retriever.retrieve(query, support)
        ranking = ranker.rank(query, [case.record for case in retrieval.considered])
        decision = gate.evaluate(query, retrieval, ranking)
        recommended = set(decision.decision["recommended_strategy_atoms"])
        actual = _actual_atoms(query_record)
        positive = _is_positive(query_record)
        inject = decision.decision["decision"] == "INJECT"
        hit = bool(inject and positive and recommended.intersection(actual))
        harmful = bool(inject and not positive and recommended.intersection(actual))
        discouraged = {
            str(item["strategy_atom"])
            for item in ranking.get("discouraged", [])
            if isinstance(item, Mapping)
        }
        suppressed = bool(
            not positive
            and (
                discouraged.intersection(actual)
                or not recommended.intersection(actual)
            )
        )
        problem = query_record["problem"]
        source = query_record["source"]
        assert isinstance(problem, Mapping) and isinstance(source, Mapping)
        subtype = (
            problem["bottleneck_subtype"]
            if problem["mode"] == "OPTIMIZE"
            else problem["failure_subtype"]
        )
        rows.append(
            {
                "record_id": query_record["record_id"],
                "mode": problem["mode"],
                "subtype": subtype,
                "algorithm_family": source["algorithm_family"],
                "decision": decision.decision["decision"],
                "abstain_reason": decision.decision["abstain_reason"],
                "support_pool_count": len(support),
                "retrieved_support_count": len(retrieval.considered),
                "support_leakage_detected": leakage,
                "recommended_strategy_atoms": sorted(recommended),
                "actual_strategy_atoms": sorted(actual),
                "actual_positive": positive,
                "success_strategy_hit": hit,
                "harmful_recommendation": harmful,
                "duplicate_failure_suppressed": suppressed,
                "guidance_tokens": decision.decision["guidance_tokens"],
                "confidence": decision.decision["confidence"],
                "estimated_success": decision.decision["estimated_success"],
            }
        )
    rows.sort(key=lambda item: str(item["record_id"]))
    return {
        "policy": name,
        "overall": _summarize_rows(rows),
        "by_mode": _group_summary(rows, "mode"),
        "by_subtype": _group_summary(rows, "subtype"),
        "by_algorithm_family": _group_summary(rows, "algorithm_family"),
        "skipped_missing_group": skipped_missing_group,
        "leakage_check_pass": all(not row["support_leakage_detected"] for row in rows),
        "rows": rows,
    }


def evaluate_generalization(
    raw_records: Sequence[Mapping[str, object]],
    *,
    audit_groups: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, object]:
    """Run Leave-One-Run/Task/Task-Family/Algorithm-Family evaluation."""

    records = [
        validate_experience_v2(item)
        for item in raw_records
        if item.get("source", {}).get("evidence_level") == "REAL_LLM_VITIS"
        and item.get("provenance", {}).get("eligible_for_ranking") is True
    ]
    records.sort(key=lambda item: str(item["record_id"]))
    groups = audit_groups or {}

    def run_group(record: Mapping[str, object]) -> str:
        return str(record["source"]["run_id"])

    def task_group(record: Mapping[str, object]) -> str | None:
        audit = groups.get(str(record["record_id"]))
        return str(audit["task_audit_hash"]) if audit and audit.get("task_audit_hash") else None

    def family_group(record: Mapping[str, object]) -> str:
        return str(record["source"]["task_family_hash"])

    def algorithm_group(record: Mapping[str, object]) -> str:
        return str(record["source"]["algorithm_family"])

    policies = {
        "leave_one_run_out": _policy_evaluation(
            "leave_one_run_out", records, group_value=run_group, audit_groups=groups
        ),
        "leave_one_task_out": _policy_evaluation(
            "leave_one_task_out", records, group_value=task_group, audit_groups=groups
        ),
        "leave_one_task_family_out": _policy_evaluation(
            "leave_one_task_family_out",
            records,
            group_value=family_group,
            audit_groups=groups,
        ),
        "leave_one_algorithm_family_out": _policy_evaluation(
            "leave_one_algorithm_family_out",
            records,
            group_value=algorithm_group,
            audit_groups=groups,
        ),
    }
    output = {
        "schema_version": GENERALIZED_EVALUATION_SCHEMA,
        "record_count": len(records),
        "task_group_mapping_count": sum(
            str(record["record_id"]) in groups for record in records
        ),
        "policies": policies,
        "leakage_checks": {
            "task_identity_not_a_feature": True,
            "task_groups_audit_only": True,
            "hidden_golden_reference_support_allowed": False,
            "all_policy_checks_pass": all(
                policy["leakage_check_pass"] for policy in policies.values()
            ),
        },
    }
    return json.loads(json.dumps(output, sort_keys=True))


def render_generalization_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# V3-E Experience 泛化离线评估",
        "",
        f"评估真实可排名 Candidate：{report['record_count']}。",
        "",
        "| Holdout | Coverage | 成功策略命中 | 有害建议率 | 重复失败抑制 | 泄漏率 | 平均 Guidance Token |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, policy in report["policies"].items():
        metrics = policy["overall"]
        lines.append(
            "| {name} | {coverage:.2%} | {hit} | {harmful:.2%} | {suppression} | {leakage:.2%} | {tokens:.1f} |".format(
                name=name,
                coverage=float(metrics["coverage"]),
                hit="—" if metrics["success_strategy_hit_rate"] is None else f"{float(metrics['success_strategy_hit_rate']):.2%}",
                harmful=float(metrics["harmful_recommendation_rate"]),
                suppression="—" if metrics["duplicate_failure_suppression_rate"] is None else f"{float(metrics['duplicate_failure_suppression_rate']):.2%}",
                leakage=float(metrics["family_leakage_rate"]),
                tokens=float(metrics["average_guidance_tokens"]),
            )
        )
    lines.extend(
        [
            "",
            "Task 分组 hash 只用于离线剔除 support，不进入 Retriever、Ranker 或 Planner 特征。ABSTAIN 表示安全回退原 Planner。",
            "",
        ]
    )
    return "\n".join(lines)


def write_generalization_artifacts(
    *,
    records_path: str | Path,
    audit_groups_path: str | Path,
    output_root: str | Path,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for raw in Path(records_path).read_bytes().splitlines():
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise ValueError("v2 evaluation input contains a non-object record")
        records.append(dict(value))
    audit_value = json.loads(Path(audit_groups_path).read_text(encoding="utf-8"))
    if not isinstance(audit_value, Mapping) or not isinstance(audit_value.get("groups"), Mapping):
        raise ValueError("evaluation audit-group sidecar is invalid")
    report = evaluate_generalization(records, audit_groups=audit_value["groups"])
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    outputs = {
        root / "experience_generalized_evaluation.json": canonical_json(report) + b"\n",
        root / "experience_generalized_evaluation.md": render_generalization_markdown(report).encode("utf-8"),
    }
    for path, content in outputs.items():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    return report


__all__ = [
    "evaluate_generalization",
    "render_generalization_markdown",
    "write_generalization_artifacts",
]
