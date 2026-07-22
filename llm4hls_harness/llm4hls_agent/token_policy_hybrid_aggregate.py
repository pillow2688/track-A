"""Aggregate the controlled A_FIXED versus D_HYBRID real Pilot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .token_policy_abc_aggregate import _paired_bootstrap
from .token_policy_abc_runner import (
    _canonical_json,
    reconcile_record_from_artifacts,
)
from .token_policy_hybrid_runner import CONDITIONS, MODES, RUN_SCHEMA


SCHEMA = "v3e.token-policy-hybrid-aggregate.v1"


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object expected: {path}")
    return value


def _records(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("schema_version") != RUN_SCHEMA:
            raise ValueError(f"invalid Hybrid record at line {number}")
        rows.append(value)
    return rows


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _geomean(values: Sequence[float]) -> float | None:
    positive = [value for value in values if value > 0]
    return (
        math.exp(statistics.fmean(math.log(value) for value in positive))
        if positive
        else None
    )


def _rate(
    rows: Sequence[Mapping[str, object]],
    predicate: Callable[[Mapping[str, object]], bool],
) -> float | None:
    return sum(predicate(row) for row in rows) / len(rows) if rows else None


def _summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    successes = [row for row in rows if row.get("final_success") is True]
    tokens = [
        value
        for row in rows
        for value in [_number(row.get("actual_total_tokens"))]
        if value is not None
    ]
    inputs = [
        value
        for row in rows
        for value in [_number(row.get("actual_input_tokens"))]
        if value is not None
    ]
    outputs = [
        value
        for row in rows
        for value in [_number(row.get("actual_output_tokens"))]
        if value is not None
    ]
    accelerations = [
        value
        for row in rows
        for value in [_number(row.get("acceleration"))]
        if value is not None and value > 0
    ]
    calls = [float(row.get("planner_calls") or 0) for row in rows]
    return {
        "runs": len(rows),
        "final_successes": len(successes),
        "final_success_rate": len(successes) / len(rows) if rows else None,
        "average_total_tokens": _mean(tokens),
        "tokens_per_successful_run": (
            sum(tokens) / len(successes) if successes else None
        ),
        "average_input_tokens": _mean(inputs),
        "average_output_tokens": _mean(outputs),
        "average_planner_calls": _mean(calls),
        "second_call_rate": _rate(
            rows, lambda row: row.get("second_call_attempted") is True
        ),
        "second_call_block_rate": _rate(
            rows, lambda row: row.get("second_call_blocked") is True
        ),
        "second_call_improvement_rate": (
            sum(row.get("second_call_improved") is True for row in rows)
            / sum(row.get("second_call_attempted") is True for row in rows)
            if any(row.get("second_call_attempted") is True for row in rows)
            else None
        ),
        "patch_invalid_rate": _rate(
            rows, lambda row: int(row.get("patch_invalid_count") or 0) > 0
        ),
        "truncation_rate": _rate(
            rows, lambda row: row.get("output_truncated") is True
        ),
        "budget_compliance_rate": _rate(
            rows, lambda row: row.get("budget_compliant") is True
        ),
        "candidate_promotion_rate": _mean(
            [float(row.get("candidate_promotion_rate") or 0) for row in rows]
        ),
        "acceleration_count": len(accelerations),
        "acceleration_median": (
            statistics.median(accelerations) if accelerations else None
        ),
        "acceleration_geometric_mean": _geomean(accelerations),
        "average_local_score_proxy": _mean(
            [
                value
                for row in rows
                for value in [_number(row.get("local_score_proxy"))]
                if value is not None
            ]
        ),
        "average_credits": _mean(
            [float(row.get("credits_used") or 0) for row in rows]
        ),
        "average_wall_time_seconds": _mean(
            [float(row.get("wall_time_seconds") or 0) for row in rows]
        ),
    }


def _task_metric_pairs(
    rows: Sequence[Mapping[str, object]], metric: str
) -> list[tuple[float, float]]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        raw_value = row.get(metric)
        value = (
            float(raw_value)
            if metric == "final_success" and isinstance(raw_value, bool)
            else _number(raw_value)
        )
        if value is not None:
            grouped[(str(row.get("task_id")), str(row.get("condition")))].append(
                value
            )
    pairs: list[tuple[float, float]] = []
    tasks = sorted({task for task, condition in grouped if condition == "A_FIXED"})
    for task in tasks:
        left = grouped.get((task, "A_FIXED"), [])
        right = grouped.get((task, "D_HYBRID"), [])
        if left and right:
            pairs.append((statistics.fmean(left), statistics.fmean(right)))
    return pairs


def _task_level_results(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for task_id in sorted({str(row.get("task_id")) for row in rows}):
        task_rows = [row for row in rows if str(row.get("task_id")) == task_id]
        by_condition = {
            condition: _summary(
                [row for row in task_rows if row.get("condition") == condition]
            )
            for condition in CONDITIONS
        }
        left = by_condition["A_FIXED"]
        right = by_condition["D_HYBRID"]

        def difference(name: str) -> float | None:
            a_value = _number(left.get(name))
            d_value = _number(right.get(name))
            return d_value - a_value if a_value is not None and d_value is not None else None

        results.append(
            {
                "task_id": task_id,
                "mode": str(task_rows[0].get("mode")) if task_rows else None,
                "family": str(task_rows[0].get("family")) if task_rows else None,
                "by_condition": by_condition,
                "D_minus_A": {
                    "final_success_rate_points": (
                        difference("final_success_rate") * 100
                        if difference("final_success_rate") is not None
                        else None
                    ),
                    "average_total_tokens": difference("average_total_tokens"),
                    "average_planner_calls": difference("average_planner_calls"),
                    "acceleration_median": difference("acceleration_median"),
                    "acceleration_geometric_mean": difference(
                        "acceleration_geometric_mean"
                    ),
                    "average_credits": difference("average_credits"),
                    "average_wall_time_seconds": difference(
                        "average_wall_time_seconds"
                    ),
                },
            }
        )
    return results


def _artifact_receipts(root: Path, rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    invalid: list[str] = []
    for row in rows:
        path = root / str(row.get("run_ref")) / str(row.get("artifact_manifest_ref"))
        expected = row.get("artifact_manifest_sha256")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != expected:
            invalid.append(str(row.get("run_id")))
    return {
        "valid": len(rows) - len(invalid),
        "invalid": invalid,
        "all_valid": not invalid,
    }


def aggregate(root: Path | str) -> dict[str, object]:
    output = Path(root).expanduser().resolve()
    manifest = _read_object(output / "manifest.json")
    config = _read_object(Path(str(manifest["matrix_config_ref"])))
    raw = _records(output / "token_policy_hybrid_results.jsonl")
    rows: list[dict[str, object]] = []
    reconciled: list[dict[str, object]] = []
    for record in raw:
        value, changed = reconcile_record_from_artifacts(
            record, output / str(record["run_ref"]), config
        )
        rows.append(value)
        if changed:
            reconciled.append({"run_id": value.get("run_id"), "fields": changed})
    reconciled_path = output / "token_policy_hybrid_reconciled_results.jsonl"
    reconciled_path.write_text(
        "".join(_canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    complete = bool(
        manifest.get("status") == "COMPLETE"
        and len(rows) == int(manifest.get("planned_runs") or 0) == 48
        and all(row.get("hidden_like") is False for row in rows)
    )
    by_condition = {
        condition: _summary(
            [row for row in rows if row.get("condition") == condition]
        )
        for condition in CONDITIONS
    }
    by_mode = {
        mode: {
            condition: _summary(
                [
                    row
                    for row in rows
                    if row.get("mode") == mode
                    and row.get("condition") == condition
                ]
            )
            for condition in CONDITIONS
        }
        for mode in MODES
    }
    metrics = (
        "final_success", "actual_total_tokens", "actual_input_tokens",
        "actual_output_tokens", "planner_calls", "acceleration",
        "local_score_proxy", "credits_used", "wall_time_seconds",
    )
    comparisons = {
        metric: _paired_bootstrap(
            _task_metric_pairs(rows, metric),
            samples=int(config["statistics"]["bootstrap_samples"]),
            seed=int(config["statistics"]["bootstrap_seed"])
            + sum(map(ord, metric)),
        )
        for metric in metrics
    }
    left = by_condition["A_FIXED"]
    right = by_condition["D_HYBRID"]
    optimize_left = by_mode["OPTIMIZE"]["A_FIXED"]
    optimize_right = by_mode["OPTIMIZE"]["D_HYBRID"]

    def ratio_reduction(left_value: object, right_value: object) -> float | None:
        left_number = _number(left_value)
        right_number = _number(right_value)
        if left_number in {None, 0} or right_number is None:
            return None
        return (left_number - right_number) / left_number

    success_difference = (
        float(right["final_success_rate"])
        - float(left["final_success_rate"])
    ) * 100
    token_reduction = ratio_reduction(
        left["average_total_tokens"], right["average_total_tokens"]
    )
    tokens_per_success_reduction = ratio_reduction(
        left["tokens_per_successful_run"], right["tokens_per_successful_run"]
    )
    quality_checks: dict[str, bool | None] = {}
    for metric in ("acceleration_median", "acceleration_geometric_mean"):
        a_value = _number(optimize_left.get(metric))
        d_value = _number(optimize_right.get(metric))
        quality_checks[metric] = (
            d_value >= a_value * 0.95
            if a_value is not None and d_value is not None
            else None
        )
    checks = {
        "final_success_drop_within_5pp": success_difference >= -5.0,
        "average_tokens_not_increased": (
            token_reduction is not None and token_reduction >= 0
        ),
        "average_token_reduction_at_least_10pct": (
            token_reduction is not None and token_reduction >= 0.10
        ),
        "tokens_per_success_not_increased": (
            tokens_per_success_reduction is not None
            and tokens_per_success_reduction >= 0
        ),
        "planner_calls_not_increased": (
            float(right["average_planner_calls"])
            <= float(left["average_planner_calls"])
        ),
        "patch_invalid_zero": right["patch_invalid_rate"] == 0.0,
        "truncation_zero": right["truncation_rate"] == 0.0,
        "budget_compliance_100pct": right["budget_compliance_rate"] == 1.0,
        "optimize_acceleration_median_preserved": quality_checks[
            "acceleration_median"
        ] is True,
        "optimize_acceleration_geomean_preserved": quality_checks[
            "acceleration_geometric_mean"
        ] is True,
    }
    admission_passed = complete and all(checks.values())
    supports_statistics = bool(
        complete
        and len({str(row.get("task_id")) for row in rows})
        >= int(config["statistics"]["minimum_tasks_for_inferential_claim"])
    )
    return {
        "schema_version": SCHEMA,
        "experiment_id": manifest.get("experiment_id"),
        "planned_runs": manifest.get("planned_runs"),
        "actual_runs": len(rows),
        "missing_runs": max(0, 48 - len(rows)),
        "failed_runs": sum(row.get("final_success") is not True for row in rows),
        "real_complete_matrix": complete,
        "by_condition": by_condition,
        "by_mode": by_mode,
        "task_level_results": _task_level_results(rows),
        "task_level_paired_comparisons": comparisons,
        "engineering_admission": {
            "status": "PASS" if admission_passed else "NOT_PASSED",
            "checks": checks,
            "final_success_difference_points": success_difference,
            "average_total_token_reduction": token_reduction,
            "tokens_per_success_reduction": tokens_per_success_reduction,
            "optimize_quality": quality_checks,
            "enter_28_task_stage": admission_passed,
        },
        "artifact_receipts": _artifact_receipts(output, rows),
        "reconciliation": {
            "new_runs_should_not_require_reconciliation": True,
            "reconciled_runs": len(reconciled),
            "details": reconciled,
            "results_ref": reconciled_path.name,
        },
        "supports_statistical_conclusion": supports_statistics,
        "supports_competition_score_improvement_claim": False,
        "claim": (
            "Hybrid Token Policy passed the engineering admission for a larger public evaluation."
            if admission_passed
            else "Hybrid Token Policy did not pass engineering admission; the 28-task stage must not run."
        ),
    }


def write_outputs(root: Path | str, value: Mapping[str, object]) -> None:
    output = Path(root).expanduser().resolve()
    (output / "token_policy_hybrid_summary.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    by_condition = value.get("by_condition")
    by_condition = by_condition if isinstance(by_condition, Mapping) else {}
    with (output / "token_policy_hybrid_summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            (
                "condition", "runs", "success_rate", "avg_total_tokens",
                "tokens_per_success", "avg_input", "avg_output", "planner_calls",
                "second_call_rate", "patch_invalid", "truncation", "budget_compliance",
                "acceleration_median", "acceleration_geomean", "credits", "wall_time",
            )
        )
        for condition in CONDITIONS:
            row = by_condition.get(condition, {})
            row = row if isinstance(row, Mapping) else {}
            writer.writerow(
                (
                    condition, row.get("runs"), row.get("final_success_rate"),
                    row.get("average_total_tokens"), row.get("tokens_per_successful_run"),
                    row.get("average_input_tokens"), row.get("average_output_tokens"),
                    row.get("average_planner_calls"), row.get("second_call_rate"),
                    row.get("patch_invalid_rate"), row.get("truncation_rate"),
                    row.get("budget_compliance_rate"), row.get("acceleration_median"),
                    row.get("acceleration_geometric_mean"), row.get("average_credits"),
                    row.get("average_wall_time_seconds"),
                )
            )
    admission = value.get("engineering_admission")
    admission = admission if isinstance(admission, Mapping) else {}
    lines = [
        "# Token Policy Hybrid V2 A/D Real Evaluation", "",
        str(value.get("claim")), "",
        f"- Runs: {value.get('actual_runs')}/{value.get('planned_runs')}",
        f"- Engineering admission: {admission.get('status')}",
        f"- Supports statistical conclusion: {value.get('supports_statistical_conclusion')}",
        "", "| Condition | Success | Avg tokens | Tokens/success | Input | Output | Planner calls | Acceleration median/geomean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in CONDITIONS:
        row = by_condition.get(condition, {})
        row = row if isinstance(row, Mapping) else {}
        lines.append(
            f"| {condition} | {row.get('final_success_rate')} | "
            f"{row.get('average_total_tokens')} | {row.get('tokens_per_successful_run')} | "
            f"{row.get('average_input_tokens')} | {row.get('average_output_tokens')} | "
            f"{row.get('average_planner_calls')} | {row.get('acceleration_median')}/"
            f"{row.get('acceleration_geometric_mean')} |"
        )
    lines.extend(("", "## Admission checks", ""))
    checks = admission.get("checks")
    if isinstance(checks, Mapping):
        lines.extend(f"- {name}: `{passed}`" for name, passed in checks.items())
    (output / "token_policy_hybrid_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", required=True)
    args = parser.parse_args(argv)
    value = aggregate(args.experiment_root)
    write_outputs(args.experiment_root, value)
    print(_canonical_json(value["engineering_admission"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
