"""Aggregate controlled Token Policy A/B/C runs with paired task statistics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .token_policy_abc_runner import (
    CONDITIONS,
    MODES,
    RUN_SCHEMA,
    TokenPolicyABCError,
    _canonical_json,
    reconcile_record_from_artifacts,
)


AGGREGATE_SCHEMA = "v3e.token-policy-abc-aggregate.v1"
PAIRINGS = (
    ("A_FIXED", "B_DYNAMIC_HARD", "A_vs_B"),
    ("B_DYNAMIC_HARD", "C_DYNAMIC_VISIBLE", "B_vs_C"),
    ("A_FIXED", "C_DYNAMIC_VISIBLE", "A_vs_C"),
)
CONTINUOUS_METRICS = (
    "final_success",
    "actual_total_tokens",
    "actual_input_tokens",
    "actual_output_tokens",
    "max_output_utilization",
    "output_truncated",
    "invalid_output",
    "patch_valid_rate",
    "planner_calls",
    "budget_compliant",
    "candidate_promotion_rate",
    "latency_improved",
    "acceleration",
    "local_score_proxy",
    "credits_used",
    "wall_time_seconds",
)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenPolicyABCError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise TokenPolicyABCError(f"JSON is not an object: {path}")
    return value


def _read_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenPolicyABCError(f"cannot read matrix results: {path}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TokenPolicyABCError(f"invalid result line {number}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != RUN_SCHEMA:
            raise TokenPolicyABCError(f"invalid result schema at line {number}")
        records.append(value)
    return records


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {name: None for name in ("min", "p25", "p50", "p75", "p90", "max")}
    ordered = sorted(values)

    def quantile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "min": ordered[0],
        "p25": quantile(0.25),
        "p50": quantile(0.50),
        "p75": quantile(0.75),
        "p90": quantile(0.90),
        "max": ordered[-1],
    }


def _rate(records: Sequence[Mapping[str, object]], predicate: Callable[[Mapping[str, object]], bool]) -> float | None:
    return (
        sum(predicate(record) for record in records) / len(records)
        if records
        else None
    )


def _metric(record: Mapping[str, object], name: str) -> float | None:
    if name == "invalid_output":
        return float(
            sum(
                int(record.get(field) or 0)
                for field in (
                    "json_incomplete_count",
                    "patch_incomplete_count",
                    "patch_invalid_count",
                )
            )
            > 0
        )
    if name == "patch_valid_rate":
        calls = int(record.get("planner_calls") or 0)
        return (
            float(record.get("patch_valid_count") or 0) / calls
            if calls > 0
            else None
        )
    return _number(record.get(name))


def _condition_summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    successful = [record for record in records if record.get("final_success") is True]
    total_tokens = [
        value
        for record in records
        for value in [_number(record.get("actual_total_tokens"))]
        if value is not None
    ]
    input_tokens = [
        value
        for record in records
        for value in [_number(record.get("actual_input_tokens"))]
        if value is not None
    ]
    output_tokens = [
        value
        for record in records
        for value in [_number(record.get("actual_output_tokens"))]
        if value is not None
    ]
    all_attempt_tokens = sum(
        float(record.get("actual_total_tokens") or 0) for record in records
    )
    effective = [
        float(item)
        for record in records
        for item in (
            record.get("effective_max_outputs")
            if isinstance(record.get("effective_max_outputs"), list)
            else []
        )
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]
    return {
        "runs": len(records),
        "final_successes": len(successful),
        "final_success_rate": len(successful) / len(records) if records else None,
        "average_total_tokens": _mean(total_tokens),
        "tokens_per_successful_run": (
            all_attempt_tokens / len(successful) if successful else None
        ),
        "average_input_tokens": _mean(input_tokens),
        "average_output_tokens": _mean(output_tokens),
        "input_token_distribution": _distribution(input_tokens),
        "output_token_distribution": _distribution(output_tokens),
        "average_effective_max_output": _mean(effective),
        "max_output_utilization": _mean(
            [
                value
                for record in records
                for value in [_metric(record, "max_output_utilization")]
                if value is not None
            ]
        ),
        "output_truncation_rate": _rate(
            records, lambda record: record.get("output_truncated") is True
        ),
        "json_incomplete_rate": _rate(
            records, lambda record: int(record.get("json_incomplete_count") or 0) > 0
        ),
        "patch_incomplete_rate": _rate(
            records, lambda record: int(record.get("patch_incomplete_count") or 0) > 0
        ),
        "patch_invalid_rate": _rate(
            records, lambda record: int(record.get("patch_invalid_count") or 0) > 0
        ),
        "patch_valid_rate": _mean(
            [
                value
                for record in records
                for value in [_metric(record, "patch_valid_rate")]
                if value is not None
            ]
        ),
        "average_planner_calls": _mean(
            [float(record.get("planner_calls") or 0) for record in records]
        ),
        "budget_compliance_rate": _rate(
            records, lambda record: record.get("budget_compliant") is True
        ),
        "candidate_promotion_rate": _mean(
            [float(record.get("candidate_promotion_rate") or 0) for record in records]
        ),
        "latency_improvement_rate": _rate(
            records, lambda record: record.get("latency_improved") is True
        ),
        "average_acceleration": _mean(
            [
                value
                for record in records
                for value in [_metric(record, "acceleration")]
                if value is not None
            ]
        ),
        "average_local_score_proxy": _mean(
            [
                value
                for record in records
                for value in [_metric(record, "local_score_proxy")]
                if value is not None
            ]
        ),
        "average_credits": _mean(
            [float(record.get("credits_used") or 0) for record in records]
        ),
        "average_wall_time_seconds": _mean(
            [float(record.get("wall_time_seconds") or 0) for record in records]
        ),
    }


def _task_condition_metrics(
    records: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], dict[str, float]]:
    grouped: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        grouped[(str(record["task_id"]), str(record["condition"]))].append(record)
    projected: dict[tuple[str, str], dict[str, float]] = {}
    for key, rows in grouped.items():
        values: dict[str, float] = {}
        for metric in CONTINUOUS_METRICS:
            observed = [
                value
                for row in rows
                for value in [_metric(row, metric)]
                if value is not None
            ]
            if observed:
                values[metric] = statistics.fmean(observed)
        successful = [row for row in rows if row.get("final_success") is True]
        if successful:
            values["tokens_per_successful_task"] = sum(
                float(row.get("actual_total_tokens") or 0) for row in rows
            ) / len(successful)
        projected[key] = values
    return projected


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _paired_bootstrap(
    pairs: Sequence[tuple[float, float]],
    *,
    samples: int,
    seed: int,
) -> dict[str, object]:
    if not pairs:
        return {
            "pairs": 0,
            "baseline_mean": None,
            "comparison_mean": None,
            "absolute_difference": None,
            "relative_difference": None,
            "paired_bootstrap_95_ci": [None, None],
        }
    differences = [right - left for left, right in pairs]
    baseline = statistics.fmean(left for left, _ in pairs)
    comparison = statistics.fmean(right for _, right in pairs)
    randomizer = random.Random(seed)
    bootstrapped: list[float] = []
    for _ in range(samples):
        indices = [randomizer.randrange(len(pairs)) for _ in pairs]
        bootstrapped.append(statistics.fmean(differences[index] for index in indices))
    absolute = comparison - baseline
    return {
        "pairs": len(pairs),
        "baseline_mean": baseline,
        "comparison_mean": comparison,
        "absolute_difference": absolute,
        "relative_difference": absolute / baseline if baseline != 0 else None,
        "paired_bootstrap_95_ci": [
            _percentile(bootstrapped, 0.025),
            _percentile(bootstrapped, 0.975),
        ],
    }


def _mcnemar_exact(
    records: Sequence[Mapping[str, object]], left: str, right: str
) -> dict[str, object]:
    slots: dict[tuple[str, int], dict[str, bool]] = defaultdict(dict)
    for record in records:
        condition = str(record.get("condition"))
        if condition not in {left, right}:
            continue
        slots[(str(record.get("task_id")), int(record.get("repeat") or 0))][
            condition
        ] = record.get("final_success") is True
    paired = [value for value in slots.values() if left in value and right in value]
    left_only = sum(value[left] and not value[right] for value in paired)
    right_only = sum(value[right] and not value[left] for value in paired)
    discordant = left_only + right_only
    if discordant:
        tail = sum(math.comb(discordant, index) for index in range(min(left_only, right_only) + 1))
        p_value = min(1.0, 2.0 * tail / (2**discordant))
    else:
        p_value = 1.0
    return {
        "paired_run_slots": len(paired),
        "left_success_right_failure": left_only,
        "left_failure_right_success": right_only,
        "discordant_pairs": discordant,
        "mcnemar_exact_two_sided_p": p_value,
    }


def _comparisons(
    records: Sequence[Mapping[str, object]], config: Mapping[str, object]
) -> dict[str, object]:
    task_metrics = _task_condition_metrics(records)
    statistics_config = config.get("statistics")
    stats = statistics_config if isinstance(statistics_config, Mapping) else {}
    samples = int(stats.get("bootstrap_samples") or 10000)
    seed = int(stats.get("bootstrap_seed") or 0)
    minimum = int(stats.get("minimum_tasks_for_inferential_claim") or 20)
    result: dict[str, object] = {}
    for pair_index, (left, right, label) in enumerate(PAIRINGS):
        tasks = sorted(
            {
                task_id
                for task_id, condition in task_metrics
                if condition == left and (task_id, right) in task_metrics
            }
        )
        metrics: dict[str, object] = {}
        for metric in (*CONTINUOUS_METRICS, "tokens_per_successful_task"):
            pairs = [
                (task_metrics[(task_id, left)][metric], task_metrics[(task_id, right)][metric])
                for task_id in tasks
                if metric in task_metrics[(task_id, left)]
                and metric in task_metrics[(task_id, right)]
            ]
            metrics[metric] = _paired_bootstrap(
                pairs,
                samples=samples,
                seed=seed + pair_index * 1009 + sum(map(ord, metric)),
            )
        result[label] = {
            "left": left,
            "right": right,
            "paired_tasks": len(tasks),
            "inference_status": (
                "PAIRED_ESTIMATE" if len(tasks) >= minimum else "DESCRIPTIVE_ONLY"
            ),
            "metrics": metrics,
            "binary_final_success": _mcnemar_exact(records, left, right),
        }
    return result


def _validate_artifact_receipts(root: Path, records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    valid = 0
    invalid: list[str] = []
    for record in records:
        run_ref = record.get("run_ref")
        manifest_ref = record.get("artifact_manifest_ref")
        expected = record.get("artifact_manifest_sha256")
        if not all(isinstance(value, str) and value for value in (run_ref, manifest_ref, expected)):
            invalid.append(str(record.get("run_id")))
            continue
        path = root / str(run_ref) / str(manifest_ref)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            invalid.append(str(record.get("run_id")))
            continue
        if digest != expected:
            invalid.append(str(record.get("run_id")))
        else:
            valid += 1
    return {"valid": valid, "invalid": invalid, "all_valid": valid == len(records)}


def _admission(
    summaries: Mapping[str, Mapping[str, object]],
    by_mode: Mapping[str, Mapping[str, Mapping[str, object]]],
    comparisons: Mapping[str, object],
    config: Mapping[str, object],
    *,
    complete: bool,
) -> dict[str, object]:
    admission = config.get("admission")
    rules = admission if isinstance(admission, Mapping) else {}
    left = summaries.get("A_FIXED", {})
    right = summaries.get("C_DYNAMIC_VISIBLE", {})
    a_success = _number(left.get("final_success_rate"))
    c_success = _number(right.get("final_success_rate"))
    a_tokens = _number(left.get("average_total_tokens"))
    c_tokens = _number(right.get("average_total_tokens"))
    a_tps = _number(left.get("tokens_per_successful_run"))
    c_tps = _number(right.get("tokens_per_successful_run"))
    success_drop = (
        (c_success - a_success) * 100
        if a_success is not None and c_success is not None
        else None
    )
    token_reduction = (
        (a_tokens - c_tokens) / a_tokens
        if a_tokens not in {None, 0} and c_tokens is not None
        else None
    )
    success_token_reduction = (
        (a_tps - c_tps) / a_tps
        if a_tps not in {None, 0} and c_tps is not None
        else None
    )
    optimize = by_mode.get("OPTIMIZE", {})
    optimize_a = optimize.get("A_FIXED", {})
    optimize_c = optimize.get("C_DYNAMIC_VISIBLE", {})
    tolerance = float(rules.get("quality_regression_tolerance") or 0.05)
    quality_observations: dict[str, dict[str, object]] = {}
    quality_checks: list[bool] = []
    for metric in ("average_acceleration", "average_local_score_proxy"):
        baseline_value = _number(optimize_a.get(metric))
        dynamic_value = _number(optimize_c.get(metric))
        available = baseline_value is not None and dynamic_value is not None
        passed_metric = bool(
            available
            and (
                baseline_value == 0
                or dynamic_value >= baseline_value * (1.0 - tolerance)
            )
        )
        quality_observations[metric] = {
            "available": available,
            "A_FIXED": baseline_value,
            "C_DYNAMIC_VISIBLE": dynamic_value,
            "relative_tolerance": tolerance,
            "passed": passed_metric if available else None,
        }
        if available:
            quality_checks.append(passed_metric)
    optimization_quality_preserved = bool(quality_checks and all(quality_checks))
    checks = {
        "final_success_drop_within_limit": (
            success_drop is not None
            and success_drop >= -float(rules.get("max_final_success_drop_points") or 5.0)
        ),
        "average_total_token_reduction_target": (
            token_reduction is not None
            and token_reduction >= float(rules.get("target_average_total_token_reduction") or 0.1)
        ),
        "tokens_per_success_reduction_target": (
            success_token_reduction is not None
            and success_token_reduction >= float(rules.get("target_tokens_per_success_reduction") or 0.1)
        ),
        "budget_compliance": right.get("budget_compliance_rate")
        == float(rules.get("required_budget_compliance") or 1.0),
        "truncation_not_increased": (
            _number(right.get("output_truncation_rate")) is not None
            and _number(left.get("output_truncation_rate")) is not None
            and float(right["output_truncation_rate"])
            <= float(left["output_truncation_rate"])
        ),
        "invalid_output_not_increased": (
            sum(
                float(right.get(name) or 0)
                for name in ("json_incomplete_rate", "patch_incomplete_rate", "patch_invalid_rate")
            )
            <= sum(
                float(left.get(name) or 0)
                for name in ("json_incomplete_rate", "patch_incomplete_rate", "patch_invalid_rate")
            )
        ),
        "optimization_quality_not_degraded": optimization_quality_preserved,
    }
    passed = complete and all(checks.values())
    return {
        "status": "PASS" if passed else "NOT_PASSED",
        "complete_matrix_required": complete,
        "final_success_difference_points": success_drop,
        "average_total_token_reduction": token_reduction,
        "tokens_per_success_reduction": success_token_reduction,
        "optimization_quality": quality_observations,
        "checks": checks,
        "comparison_ref": "A_vs_C",
        "paired_comparison": comparisons.get("A_vs_C"),
    }


def aggregate(root: Path | str) -> dict[str, object]:
    output = Path(root).expanduser().resolve()
    manifest = _read_json(output / "manifest.json")
    matrix_path = Path(str(manifest.get("matrix_config_ref") or ""))
    config = _read_json(matrix_path)
    raw_records = _read_records(output / "token_policy_abc_results.jsonl")
    records: list[dict[str, object]] = []
    reconciled_runs: list[dict[str, object]] = []
    for raw_record in raw_records:
        run_ref = raw_record.get("run_ref")
        run_dir = output / str(run_ref) if isinstance(run_ref, str) else output
        record, changed = reconcile_record_from_artifacts(
            raw_record, run_dir, config
        )
        records.append(record)
        if changed:
            reconciled_runs.append(
                {"run_id": record.get("run_id"), "fields": changed}
            )
    reconciled_ref = output / "token_policy_abc_reconciled_results.jsonl"
    reconciled_ref.write_text(
        "".join(_canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )
    planned = int(manifest.get("planned_runs") or 0)
    complete = bool(
        manifest.get("status") == "COMPLETE"
        and len(records) == planned
        and planned > 0
        and all(
            record.get("evidence_level")
            in {"REAL_VITIS_VALIDATED", "REAL_VITIS_ATTEMPT_FAILED"}
            for record in records
        )
        and all(record.get("hidden_like") is False for record in records)
    )
    by_condition = {
        condition: _condition_summary(
            [record for record in records if record.get("condition") == condition]
        )
        for condition in CONDITIONS
    }
    by_mode = {
        mode: {
            condition: _condition_summary(
                [
                    record
                    for record in records
                    if record.get("mode") == mode and record.get("condition") == condition
                ]
            )
            for condition in CONDITIONS
        }
        for mode in MODES
    }
    comparisons = _comparisons(records, config)
    admission = _admission(
        by_condition, by_mode, comparisons, config, complete=complete
    )
    inferential = bool(
        complete
        and all(
            isinstance(value, Mapping)
            and value.get("inference_status") == "PAIRED_ESTIMATE"
            for value in comparisons.values()
        )
    )
    if admission["status"] == "PASS" and inferential:
        claim = (
            "Under controlled paired real-provider evaluation, the dynamic "
            "Token Policy reduced token cost while preserving final correctness."
        )
    elif complete:
        claim = (
            "Controlled real-provider A/B/C Pilot runs completed, but engineering "
            "admission was not met. No claim is made about token-efficiency or "
            "competition-score improvement."
        )
    else:
        claim = (
            "Token Policy engineering implementation and deterministic validation passed. "
            "No claim is made about success-rate, token-efficiency, or competition-score "
            "improvement because controlled real-provider A/B/C runs are not yet complete."
        )
    return {
        "schema_version": AGGREGATE_SCHEMA,
        "experiment_id": manifest.get("experiment_id"),
        "manifest_status": manifest.get("status"),
        "planned_runs": planned,
        "actual_runs": len(records),
        "missing_runs": max(0, planned - len(records)),
        "failed_runs": sum(record.get("final_success") is not True for record in records),
        "real_complete_matrix": complete,
        "artifact_receipts": _validate_artifact_receipts(output, records),
        "reconciliation": {
            "source": "durable BudgetLedger/CandidateRegistry/action receipts",
            "reconciled_runs": len(reconciled_runs),
            "details": reconciled_runs,
            "results_ref": reconciled_ref.name,
        },
        "by_condition": by_condition,
        "by_mode": by_mode,
        "paired_comparisons": comparisons,
        "engineering_admission": admission,
        "supports_statistical_conclusion": inferential,
        "supports_competition_score_improvement_claim": bool(
            inferential
            and admission["status"] == "PASS"
            and _number(by_condition["C_DYNAMIC_VISIBLE"].get("average_local_score_proxy"))
            is not None
        ),
        "claim": claim,
        "adjustment_parameters": [
            "mode output cap",
            "minimum viable output",
            "future reserve",
            "safety margin",
            "pressure threshold",
            "prompt compression order",
        ],
    }


def _write_outputs(root: Path, value: Mapping[str, object]) -> None:
    json_path = root / "token_policy_abc_summary.json"
    json_path.write_text(_canonical_json(value) + "\n", encoding="utf-8")
    csv_path = root / "token_policy_abc_summary.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("condition", "runs", "final_success_rate", "average_total_tokens", "tokens_per_successful_run", "truncation_rate", "patch_valid_rate", "budget_compliance"))
        conditions = value.get("by_condition")
        for condition in CONDITIONS:
            row = conditions.get(condition, {}) if isinstance(conditions, Mapping) else {}
            writer.writerow(
                (
                    condition,
                    row.get("runs"),
                    row.get("final_success_rate"),
                    row.get("average_total_tokens"),
                    row.get("tokens_per_successful_run"),
                    row.get("output_truncation_rate"),
                    row.get("patch_valid_rate"),
                    row.get("budget_compliance_rate"),
                )
            )
    lines = [
        "# Token Policy Dynamic A/B/C Real Evaluation",
        "",
        str(value.get("claim")),
        "",
        f"- Planned runs: {value.get('planned_runs')}",
        f"- Actual runs: {value.get('actual_runs')}",
        f"- Missing runs: {value.get('missing_runs')}",
        f"- Engineering admission: {value.get('engineering_admission', {}).get('status') if isinstance(value.get('engineering_admission'), Mapping) else None}",
        f"- Statistical conclusion supported: {value.get('supports_statistical_conclusion')}",
        "",
        "| Condition | Runs | Final success | Avg tokens | Tokens/success | Truncation | Patch valid | Budget compliant |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    conditions = value.get("by_condition")
    for condition in CONDITIONS:
        row = conditions.get(condition, {}) if isinstance(conditions, Mapping) else {}
        lines.append(
            "| " + " | ".join(
                str(item)
                for item in (
                    condition,
                    row.get("runs"),
                    row.get("final_success_rate"),
                    row.get("average_total_tokens"),
                    row.get("tokens_per_successful_run"),
                    row.get("output_truncation_rate"),
                    row.get("patch_valid_rate"),
                    row.get("budget_compliance_rate"),
                )
            ) + " |"
        )
    lines.extend(("", "Full paired statistics and per-mode results are in `token_policy_abc_summary.json`.", ""))
    (root / "token_policy_abc_report.md").write_text("\n".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m llm4hls_agent.token_policy_abc_aggregate"
    )
    parser.add_argument("--experiment-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.experiment_dir).expanduser().resolve()
    try:
        value = aggregate(root)
        _write_outputs(root, value)
    except Exception as exc:
        print(
            _canonical_json(
                {"status": "ERROR", "error_type": type(exc).__name__, "detail": str(exc)}
            ),
            file=sys.stderr,
        )
        return 3
    print(_canonical_json(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
