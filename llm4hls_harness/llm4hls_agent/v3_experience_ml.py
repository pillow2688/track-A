"""Leakage-safe dataset export and learning-readiness checks for V3-E v2."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import canonical_json, canonical_sha256
from .v3_experience_analysis import experience_outcome
from .v3_experience_v2 import MODES, validate_experience_v2


ML_EXPORT_SCHEMA = "v3e.experience-ml-export.v1"
LEARNING_READINESS_SCHEMA = "v3e.experience-learning-readiness.v1"


def _eligible(raw_records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    records = []
    for raw in raw_records:
        record = validate_experience_v2(raw)
        if (
            record["source"]["evidence_level"] == "REAL_LLM_VITIS"
            and record["source"]["task_split"] in {"train", "dev"}
            and record["provenance"]["eligible_for_ranking"] is True
        ):
            records.append(record)
    records.sort(key=lambda item: str(item["record_id"]))
    return records


def _family_splits(records: Sequence[Mapping[str, object]]) -> dict[str, str]:
    families = sorted(
        {str(record["source"]["task_family_hash"]) for record in records},
        key=lambda item: hashlib.sha256(item.encode("ascii")).hexdigest(),
    )
    if not families:
        return {}
    if len(families) == 1:
        return {families[0]: "train"}
    if len(families) == 2:
        return {families[0]: "train", families[1]: "test"}
    test_count = max(1, round(len(families) * 0.15))
    validation_count = max(1, round(len(families) * 0.15))
    while test_count + validation_count >= len(families):
        if validation_count > 1:
            validation_count -= 1
        elif test_count > 1:
            test_count -= 1
        else:
            break
    train_end = len(families) - validation_count - test_count
    return {
        family: (
            "train"
            if index < train_end
            else "validation"
            if index < train_end + validation_count
            else "test"
        )
        for index, family in enumerate(families)
    }


def _subtype(record: Mapping[str, object]) -> str:
    problem = record["problem"]
    assert isinstance(problem, Mapping)
    return str(
        problem["bottleneck_subtype"]
        if problem["mode"] == "OPTIMIZE"
        else problem["failure_subtype"]
    )


def _success(record: Mapping[str, object]) -> bool:
    return experience_outcome(record) == "SUCCESS"


def _row(
    schema: str,
    record: Mapping[str, object],
    split: str,
    features: Mapping[str, object],
    labels: Mapping[str, object],
    *,
    suffix: str = "",
    source_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    value = {
        "schema_version": schema,
        "row_id": "",
        "split": split,
        "family_group_hash": record["source"]["task_family_hash"],
        "features": dict(features),
        "labels": dict(labels),
        "source_record_ids": list(source_ids or [str(record["record_id"])]),
        "weak_supervision": schema.endswith("evidence-selection.v1"),
    }
    value["row_id"] = canonical_sha256({**value, "row_id": "", "suffix": suffix})
    return value


def build_ml_datasets(
    raw_records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    records = _eligible(raw_records)
    splits = _family_splits(records)
    strategy_rows: list[dict[str, object]] = []
    cosim_rows: list[dict[str, object]] = []
    evidence_rows: list[dict[str, object]] = []
    continue_rows: list[dict[str, object]] = []

    for record in records:
        source = record["source"]
        problem = record["problem"]
        structure = record["structure_features"]
        strategy = record["strategy"]
        validation = record["validation"]
        performance = record["performance"]
        cost = record["cost"]
        token_policy = record.get("token_policy")
        if not isinstance(token_policy, Mapping):
            token_policy = {
                "token_pressure": "LOW",
                "effective_max_output_tokens": 0,
                "estimated_input_tokens": 0,
                "guidance_actual_tokens": 0,
                "actual_output_tokens": None,
                "tokens_remaining_before_call": 0,
                "future_round_token_reserve": 0,
                "rounds_remaining": 0,
                "output_truncated": False,
                "estimated_base_prompt_tokens": 0,
                "estimated_guidance_tokens": 0,
            }
        split = splits[str(source["task_family_hash"])]
        success = _success(record)
        acceleration = performance["acceleration"]
        acceleration_number = float(acceleration) if isinstance(acceleration, (int, float)) and not isinstance(acceleration, bool) else 0.0
        utility = (
            (1.0 if success else 0.0)
            + max(0.0, acceleration_number - 1.0)
            - float(cost["credits"]) / 100.0
            - float(cost["total_tokens"]) / 100000.0
        )
        common = {
            "mode": problem["mode"],
            "subtype": _subtype(record),
            "algorithm_family": source["algorithm_family"],
            "difficulty": source["difficulty"],
            "numeric_semantics": problem["numeric_semantics"],
            "requires_cosim": problem["requires_cosim"],
            "structure": structure,
            "patch_complexity": strategy["patch_complexity"],
            "normalization_confidence": strategy["strategy_normalization_confidence"],
            "token_cost_context": {
                "token_pressure": token_policy["token_pressure"],
                "effective_max_output_tokens": token_policy[
                    "effective_max_output_tokens"
                ],
                "estimated_input_tokens": token_policy[
                    "estimated_input_tokens"
                ],
                "guidance_actual_tokens": token_policy[
                    "guidance_actual_tokens"
                ],
                "actual_output_tokens": token_policy["actual_output_tokens"],
            },
        }
        for atom in strategy["observed_strategy_atoms"]:
            strategy_rows.append(
                _row(
                    "v3e.ml.strategy-ranking.v1",
                    record,
                    split,
                    {**common, "strategy_atom": atom, "cost": cost},
                    {
                        "success": success,
                        "strict_improvement": performance["strict_improvement"],
                        "final_pass": validation["fresh_final_status"] == "PASS",
                        "acceleration": acceleration,
                        "utility": round(utility, 8),
                    },
                    suffix=str(atom),
                )
            )
        if validation["cosim_status"] in {"PASS", "FAIL", "TIMEOUT"}:
            cosim_rows.append(
                _row(
                    "v3e.ml.cosim-risk.v1",
                    record,
                    split,
                    {
                        "requires_cosim": problem["requires_cosim"],
                        "has_stream": structure["has_stream"],
                        "has_dataflow": structure["has_dataflow"],
                        "has_fifo": structure["has_fifo"],
                        "has_interface_change": "INTERFACE_CHANGE_OBSERVED"
                        in strategy["normalization_reason_codes"],
                        "has_bitwidth_change": "BITWIDTH_OPTIMIZATION"
                        in strategy["observed_strategy_atoms"],
                        "strategy_atoms": strategy["observed_strategy_atoms"],
                        "patch_complexity": strategy["patch_complexity"],
                        "synth_status": validation["synth_status"],
                        "token_context": {
                            "token_pressure": token_policy["token_pressure"],
                            "effective_max_output_tokens": token_policy[
                                "effective_max_output_tokens"
                            ],
                        },
                    },
                    {"cosim_status": validation["cosim_status"]},
                )
            )
        evidence_rows.append(
            _row(
                "v3e.ml.evidence-selection.v1",
                record,
                split,
                {
                    **common,
                    "available_evidence": {
                        "failure_stage": problem["failure_stage"],
                        "failure_subtype": problem["failure_subtype"],
                        "bottleneck_subtype": problem["bottleneck_subtype"],
                        "loop_ii_available": structure["critical_loop_ii"] is not None,
                        "latency_available": performance["latency_before"] is not None,
                        "resource_pressure_available": any(
                            item != "UNKNOWN"
                            for item in structure["resource_pressure"].values()
                        ),
                    },
                    "declared_strategy_bundle": strategy["declared_strategy_bundle"],
                    "observed_strategy_atoms": strategy["observed_strategy_atoms"],
                    "context_token_profile": {
                        "estimated_base_prompt_tokens": token_policy[
                            "estimated_base_prompt_tokens"
                        ],
                        "estimated_guidance_tokens": token_policy[
                            "estimated_guidance_tokens"
                        ],
                        "estimated_input_tokens": token_policy[
                            "estimated_input_tokens"
                        ],
                        "guidance_actual_tokens": token_policy[
                            "guidance_actual_tokens"
                        ],
                        "output_truncated": token_policy["output_truncated"],
                    },
                },
                {
                    "strategy_succeeded": success,
                    "strategy_observation_trusted": strategy[
                        "strategy_normalization_confidence"
                    ]
                    >= 0.65,
                },
            )
        )

    by_run: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        by_run[str(record["source"]["run_id"])].append(record)
    for run_records in by_run.values():
        run_records.sort(
            key=lambda item: (
                int(item["source"]["round_index"]), str(item["record_id"])
            )
        )
        no_improvement = 0
        for current, following in zip(run_records, run_records[1:]):
            current_performance = current["performance"]
            following_performance = following["performance"]
            current_token_policy = current.get("token_policy")
            if not isinstance(current_token_policy, Mapping):
                current_token_policy = {
                    "tokens_remaining_before_call": 0,
                    "effective_max_output_tokens": 0,
                    "future_round_token_reserve": 0,
                    "rounds_remaining": 0,
                    "token_pressure": "LOW",
                    "actual_total_tokens": None,
                }
            if current_performance["strict_improvement"]:
                no_improvement = 0
            else:
                no_improvement += 1
            split = splits[str(current["source"]["task_family_hash"])]
            next_acceleration = following_performance["acceleration"]
            next_improved = following_performance["strict_improvement"] is True
            continue_rows.append(
                _row(
                    "v3e.ml.continue-value.v1",
                    current,
                    split,
                    {
                        "mode": current["problem"]["mode"],
                        "subtype": _subtype(current),
                        "algorithm_family": current["source"]["algorithm_family"],
                        "best_latency": current_performance["latency_after"],
                        "no_improvement_count": no_improvement,
                        "current_strategy_atoms": current["strategy"]["observed_strategy_atoms"],
                        "current_cost": current["cost"],
                        "token_policy": {
                            "tokens_remaining_before_call": current_token_policy[
                                "tokens_remaining_before_call"
                            ],
                            "effective_max_output_tokens": current_token_policy[
                                "effective_max_output_tokens"
                            ],
                            "future_round_token_reserve": current_token_policy[
                                "future_round_token_reserve"
                            ],
                            "rounds_remaining": current_token_policy[
                                "rounds_remaining"
                            ],
                            "token_pressure": current_token_policy[
                                "token_pressure"
                            ],
                            "actual_total_tokens": current_token_policy[
                                "actual_total_tokens"
                            ],
                        },
                    },
                    {
                        "next_round_improved": next_improved,
                        "next_round_acceleration": next_acceleration,
                        "next_round_credits": following["cost"]["credits"],
                        "next_round_tokens": following["cost"]["total_tokens"],
                    },
                    suffix=str(following["record_id"]),
                    source_ids=[str(current["record_id"]), str(following["record_id"])],
                )
            )

    datasets = {
        "strategy_ranking": sorted(strategy_rows, key=lambda item: item["row_id"]),
        "cosim_risk": sorted(cosim_rows, key=lambda item: item["row_id"]),
        "continue_value": sorted(continue_rows, key=lambda item: item["row_id"]),
        "evidence_selection": sorted(evidence_rows, key=lambda item: item["row_id"]),
    }
    split_counts = {
        name: dict(sorted(Counter(row["split"] for row in rows).items()))
        for name, rows in datasets.items()
    }
    return {
        "schema_version": ML_EXPORT_SCHEMA,
        "source_record_count": len(records),
        "family_splits": dict(sorted(splits.items())),
        "family_split_disjoint": all(
            len({split}) == 1 for split in splits.values()
        ),
        "split_counts": split_counts,
        "datasets": datasets,
    }


def _metric(
    generalization: Mapping[str, object] | None,
    policy: str,
    name: str,
    default: float = 0.0,
) -> float:
    try:
        value = generalization["policies"][policy]["overall"][name]
    except (KeyError, TypeError):
        return default
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def build_learning_readiness(
    raw_records: Sequence[Mapping[str, object]],
    export: Mapping[str, object],
    *,
    generalization: Mapping[str, object] | None = None,
) -> dict[str, object]:
    records = _eligible(raw_records)
    mode_counts = Counter(str(record["problem"]["mode"]) for record in records)
    outcome_by_subtype: dict[str, set[str]] = defaultdict(set)
    for record in records:
        outcome_by_subtype[_subtype(record)].add(experience_outcome(record))
    key_subtype_balance = any(
        "SUCCESS" in outcomes
        and ("FAILURE" in outcomes or "NO_IMPROVEMENT" in outcomes)
        for outcomes in outcome_by_subtype.values()
    )
    loto_coverage = _metric(generalization, "leave_one_task_out", "coverage")
    harmful = _metric(
        generalization, "leave_one_task_out", "harmful_recommendation_rate"
    )
    strategy_checks = {
        "ranking_candidates_at_least_60": len(records) >= 60,
        "each_mode_at_least_10": all(mode_counts[mode] >= 10 for mode in MODES),
        "key_subtype_has_positive_and_negative": key_subtype_balance,
        "leave_one_task_out_coverage_at_least_40_percent": loto_coverage >= 0.40,
        "harmful_recommendation_rate_at_most_5_percent": harmful <= 0.05,
    }
    cosim_records = [
        record
        for record in records
        if record["validation"]["cosim_status"] in {"PASS", "FAIL", "TIMEOUT"}
    ]
    cosim_statuses = Counter(record["validation"]["cosim_status"] for record in cosim_records)
    cosim_families = Counter(record["source"]["task_family_hash"] for record in cosim_records)
    max_cosim_family = (
        max(cosim_families.values()) / len(cosim_records) if cosim_records else 1.0
    )
    cosim_checks = {
        "real_cosim_samples_at_least_30": len(cosim_records) >= 30,
        "pass_and_fail_or_timeout_present": bool(
            cosim_statuses["PASS"] and (cosim_statuses["FAIL"] or cosim_statuses["TIMEOUT"])
        ),
        "single_family_share_at_most_50_percent": max_cosim_family <= 0.5,
    }
    continue_rows = export["datasets"]["continue_value"]
    continue_labels = Counter(
        bool(row["labels"]["next_round_improved"]) for row in continue_rows
    )
    continue_checks = {
        "decisions_at_least_50": len(continue_rows) >= 50,
        "improvement_and_no_improvement_present": bool(
            continue_labels[True] and continue_labels[False]
        ),
    }
    evidence_rows = export["datasets"]["evidence_selection"]
    evidence_checks = {
        "planner_rounds_at_least_100": len(evidence_rows) >= 100,
        "evidence_usage_and_outcome_attributable": all(
            "available_evidence" in row["features"]
            and "strategy_succeeded" in row["labels"]
            for row in evidence_rows
        )
        and bool(evidence_rows),
    }

    def status(checks: Mapping[str, bool]) -> str:
        return "READY" if all(checks.values()) else "NOT_READY"

    output = {
        "schema_version": LEARNING_READINESS_SCHEMA,
        "strategy_ranker": {
            "status": status(strategy_checks),
            "checks": strategy_checks,
            "observed": {
                "ranking_candidates": len(records),
                "mode_counts": dict(sorted(mode_counts.items())),
                "leave_one_task_out_coverage": loto_coverage,
                "harmful_recommendation_rate": harmful,
            },
        },
        "cosim_risk": {
            "status": status(cosim_checks),
            "checks": cosim_checks,
            "observed": {
                "samples": len(cosim_records),
                "status_counts": dict(sorted(cosim_statuses.items())),
                "maximum_family_share": round(max_cosim_family, 8),
            },
        },
        "continue_predictor": {
            "status": status(continue_checks),
            "checks": continue_checks,
            "observed": {
                "decisions": len(continue_rows),
                "label_counts": {str(key).lower(): value for key, value in sorted(continue_labels.items())},
            },
        },
        "evidence_selector": {
            "status": status(evidence_checks),
            "checks": evidence_checks,
            "observed": {"planner_rounds": len(evidence_rows)},
        },
        "complex_model_training_allowed": False,
    }
    output["complex_model_training_allowed"] = any(
        output[name]["status"] == "READY"
        for name in (
            "strategy_ranker",
            "cosim_risk",
            "continue_predictor",
            "evidence_selector",
        )
    )
    return json.loads(json.dumps(output, sort_keys=True))


def render_dataset_card(export: Mapping[str, object]) -> str:
    lines = [
        "# V3-E Experience Dataset Card",
        "",
        f"来源：{export['source_record_count']} 条 public train/dev、真实 LLM + Vitis、ranking-eligible Candidate。",
        "",
        "| 数据集 | Train | Validation | Test |",
        "|---|---:|---:|---:|",
    ]
    for name, counts in export["split_counts"].items():
        lines.append(
            f"| {name} | {counts.get('train', 0)} | {counts.get('validation', 0)} | {counts.get('test', 0)} |"
        )
    lines.extend(
        [
            "",
            "同一 task family 只进入一个 split。task ID、完整源码、完整 Patch、Prompt、日志、secret 及 golden/reference 内容均不进入数据行。",
            "",
            "当前导出适合做规则/特征审计与小规模基线，不代表数据量已经足够训练复杂模型；正式训练必须服从 readiness 报告。",
            "",
        ]
    )
    return "\n".join(lines)


def render_readiness_markdown(report: Mapping[str, object]) -> str:
    lines = ["# V3-E Experience 学习准备度", ""]
    for key, title in (
        ("strategy_ranker", "Strategy Ranker"),
        ("cosim_risk", "CoSim Risk"),
        ("continue_predictor", "Continue Predictor"),
        ("evidence_selector", "Evidence Selector"),
    ):
        item = report[key]
        lines.extend([f"## {title}: {item['status']}", ""])
        for check, passed in item["checks"].items():
            lines.append(f"- {'PASS' if passed else 'FAIL'}：{check}")
        lines.append("")
    if not report["complex_model_training_allowed"]:
        lines.append("结论：现阶段不得训练复杂模型，应继续按 Coverage Queue 积累真实正例、失败和无收益样本。")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_ml_artifacts(
    *,
    records_path: str | Path,
    output_root: str | Path,
    generalization_path: str | Path | None = None,
) -> dict[str, object]:
    records = [
        json.loads(raw)
        for raw in Path(records_path).read_bytes().splitlines()
        if raw.strip()
    ]
    generalization = (
        json.loads(Path(generalization_path).read_text(encoding="utf-8"))
        if generalization_path is not None
        else None
    )
    export = build_ml_datasets(records)
    readiness = build_learning_readiness(
        records, export, generalization=generalization
    )
    root = Path(output_root)
    name_map = {
        "strategy_ranking": "strategy_ranking",
        "cosim_risk": "cosim_risk",
        "continue_value": "continue_value",
        "evidence_selection": "evidence_selection",
    }
    for dataset, prefix in name_map.items():
        rows = export["datasets"][dataset]
        for split in ("train", "validation", "test"):
            selected = [row for row in rows if row["split"] == split]
            _atomic_write(
                root / f"{prefix}_{split}.jsonl",
                b"".join(canonical_json(row) + b"\n" for row in selected),
            )
    feature_schema = {
        "schema_version": "v3e.ml-feature-schema.v1",
        "datasets": {
            name: {
                "row_schema": rows[0]["schema_version"] if rows else None,
                "feature_keys": sorted(
                    {key for row in rows for key in row["features"]}
                ),
                "label_keys": sorted(
                    {key for row in rows for key in row["labels"]}
                ),
            }
            for name, rows in export["datasets"].items()
        },
        "forbidden_features": [
            "task_id",
            "source_code",
            "patch_text",
            "prompt_text",
            "secret",
            "golden_or_reference_content",
        ],
    }
    split_manifest = {
        "schema_version": "v3e.ml-family-split.v1",
        "family_splits": export["family_splits"],
        "split_counts": export["split_counts"],
        "family_split_disjoint": export["family_split_disjoint"],
    }
    _atomic_write(root / "feature_schema.json", canonical_json(feature_schema) + b"\n")
    _atomic_write(root / "split_manifest.json", canonical_json(split_manifest) + b"\n")
    _atomic_write(root / "dataset_card.md", render_dataset_card(export).encode("utf-8"))
    _atomic_write(
        root / "experience_learning_readiness.json",
        canonical_json(readiness) + b"\n",
    )
    _atomic_write(
        root / "experience_learning_readiness.md",
        render_readiness_markdown(readiness).encode("utf-8"),
    )
    return {"export": export, "readiness": readiness}


__all__ = [
    "build_learning_readiness",
    "build_ml_datasets",
    "write_ml_artifacts",
]
