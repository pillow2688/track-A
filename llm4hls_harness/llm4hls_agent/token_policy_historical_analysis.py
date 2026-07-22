"""Offline Token Policy analysis over durable, reconciled real-run receipts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence


SCHEMA = "v3e.token-policy-historical-distribution.v1"
MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object expected: {path}")
    return value


def _read_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"record {number} is not an object")
        records.append(value)
    return records


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _quantile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _quantile(values, 0.50),
        "p75": _quantile(values, 0.75),
        "p90": _quantile(values, 0.90),
        "p95": _quantile(values, 0.95),
        "max": max(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
    }


def _geomean(values: Sequence[float]) -> float | None:
    positive = [value for value in values if value > 0]
    if not positive:
        return None
    return math.exp(statistics.fmean(math.log(value) for value in positive))


def _call_rows(run_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted((run_dir / "planner").glob("proposal_*.json")):
        try:
            proposal = _read_object(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        rows.append(
            {
                "round": int(proposal.get("round_index") or len(rows) + 1),
                "input_tokens": _number(proposal.get("input_tokens")),
                "output_tokens": _number(proposal.get("output_tokens")),
                "finish_reason": proposal.get("finish_reason"),
                "truncated": proposal.get("output_truncated") is True,
                "strategy_bundle": [
                    item
                    for item in str(proposal.get("change_class") or "").split("+")
                    if item
                ],
            }
        )
    rows.sort(key=lambda row: int(row["round"]))
    return rows


def _second_call_has_new_evidence(run_dir: Path) -> bool | None:
    second = run_dir / "planner" / "inputs" / "round_002.json"
    if not second.is_file():
        return None
    value = _read_object(second)
    round_state = value.get("round")
    round_state = round_state if isinstance(round_state, Mapping) else {}
    failure = round_state.get("failure_evidence")
    history = value.get("history")
    if isinstance(failure, Mapping) and bool(failure):
        return True
    if not isinstance(history, list):
        return False
    for item in history:
        if not isinstance(item, Mapping):
            continue
        if any(
            item.get(field)
            for field in (
                "selection_metrics_digest",
                "metrics",
                "synth_evidence",
                "rejection_reason",
            )
        ):
            return True
    return False


def _second_call_improved(run_dir: Path) -> bool | None:
    registry_path = run_dir / "candidate_registry.json"
    if not registry_path.is_file():
        return None
    candidates = _read_object(registry_path).get("candidates")
    if not isinstance(candidates, Mapping):
        return None
    second_or_later = [
        value
        for value in candidates.values()
        if isinstance(value, Mapping) and int(value.get("round_index") or 0) >= 2
    ]
    if not second_or_later:
        return None
    return any(
        value.get("status") in {"PROMOTED", "FINAL_VERIFIED"}
        for value in second_or_later
    )


def _stable_cap(successful_call_outputs: Sequence[float], margin: float) -> int | None:
    p95 = _quantile(successful_call_outputs, 0.95)
    if p95 is None:
        return None
    # A 16-token quantum keeps the generated cap stable without hiding its
    # empirical origin behind a coarse, hand-picked round number.
    return int(math.ceil((p95 * (1.0 + margin)) / 16.0) * 16)


def analyze(
    experiment_root: Path | str,
    *,
    safety_margin: float = 0.15,
) -> dict[str, object]:
    root = Path(experiment_root).expanduser().resolve()
    records_path = root / "token_policy_abc_reconciled_results.jsonl"
    records = _read_records(records_path)
    if len(records) != 72:
        raise ValueError(f"expected 72 reconciled records, found {len(records)}")
    enriched: list[dict[str, object]] = []
    for record in records:
        run_ref = record.get("run_ref")
        if not isinstance(run_ref, str) or not run_ref:
            raise ValueError("reconciled record has no run_ref")
        run_dir = root / run_ref
        enriched.append(
            {
                **record,
                "call_rows": _call_rows(run_dir),
                "second_call_new_evidence": _second_call_has_new_evidence(run_dir),
                "second_call_improved": _second_call_improved(run_dir),
            }
        )

    by_mode: dict[str, object] = {}
    generated_caps: dict[str, int | None] = {}
    for mode in MODES:
        rows = [row for row in enriched if row.get("mode") == mode]
        successful = [row for row in rows if row.get("final_success") is True]
        call_outputs = [
            value
            for row in rows
            for call in row["call_rows"]  # type: ignore[index]
            for value in [_number(call.get("output_tokens"))]
            if value is not None
        ]
        successful_call_outputs = [
            value
            for row in successful
            for call in row["call_rows"]  # type: ignore[index]
            for value in [_number(call.get("output_tokens"))]
            if value is not None
        ]
        run_outputs = [
            value
            for row in rows
            for value in [_number(row.get("actual_output_tokens"))]
            if value is not None
        ]
        successful_run_outputs = [
            value
            for row in successful
            for value in [_number(row.get("actual_output_tokens"))]
            if value is not None
        ]
        inputs = [
            value
            for row in rows
            for value in [_number(row.get("actual_input_tokens"))]
            if value is not None
        ]
        accelerations = [
            value
            for row in successful
            for value in [_number(row.get("acceleration"))]
            if value is not None and value > 0
        ]
        second = [row for row in rows if int(row.get("planner_calls") or 0) >= 2]
        cap = _stable_cap(successful_call_outputs, safety_margin)
        generated_caps[mode] = cap
        by_mode[mode] = {
            "runs": len(rows),
            "successful_runs": len(successful),
            "final_success_rate": len(successful) / len(rows) if rows else None,
            "run_output_tokens": _distribution(run_outputs),
            "successful_run_output_tokens": _distribution(successful_run_outputs),
            "per_call_output_tokens": _distribution(call_outputs),
            "successful_per_call_output_tokens": _distribution(
                successful_call_outputs
            ),
            "run_input_tokens": _distribution(inputs),
            "planner_calls": _distribution(
                [float(row.get("planner_calls") or 0) for row in rows]
            ),
            "second_call_runs": len(second),
            "second_call_rate": len(second) / len(rows) if rows else None,
            "second_call_new_evidence_rate": (
                sum(row["second_call_new_evidence"] is True for row in second)
                / len(second)
                if second
                else None
            ),
            "second_call_improvement_rate": (
                sum(row["second_call_improved"] is True for row in second)
                / len(second)
                if second
                else None
            ),
            "patch_invalid_runs": sum(
                int(row.get("patch_invalid_count") or 0) > 0 for row in rows
            ),
            "acceleration_median": statistics.median(accelerations)
            if accelerations
            else None,
            "acceleration_geometric_mean": _geomean(accelerations),
            "stable_cap": cap,
        }
    return {
        "schema_version": SCHEMA,
        "source": {
            "experiment_root": root.as_posix(),
            "records_ref": records_path.name,
            "real_reconciled_runs": len(records),
            "external_model_calls": 0,
            "vitis_calls": 0,
        },
        "stable_cap_rule": {
            "population": "successful real Planner calls grouped by routed mode",
            "percentile": 0.95,
            "safety_margin": safety_margin,
            "rounding_quantum_tokens": 16,
        },
        "generated_mode_stable_caps": generated_caps,
        "by_mode": by_mode,
    }


def write_outputs(value: Mapping[str, object], output_dir: Path | str) -> None:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "token_policy_historical_distribution.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    by_mode = value.get("by_mode")
    by_mode = by_mode if isinstance(by_mode, Mapping) else {}
    with (output / "token_policy_historical_distribution.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            (
                "mode", "runs", "success_rate", "call_output_p50",
                "call_output_p75", "call_output_p90", "call_output_p95",
                "call_output_max", "successful_call_output_p95", "stable_cap",
                "planner_calls_mean", "second_call_rate",
                "second_call_new_evidence_rate", "second_call_improvement_rate",
                "patch_invalid_runs", "acceleration_median",
                "acceleration_geometric_mean",
            )
        )
        for mode in MODES:
            row = by_mode.get(mode, {})
            row = row if isinstance(row, Mapping) else {}
            calls = row.get("per_call_output_tokens")
            calls = calls if isinstance(calls, Mapping) else {}
            success_calls = row.get("successful_per_call_output_tokens")
            success_calls = success_calls if isinstance(success_calls, Mapping) else {}
            planner = row.get("planner_calls")
            planner = planner if isinstance(planner, Mapping) else {}
            writer.writerow(
                (
                    mode, row.get("runs"), row.get("final_success_rate"),
                    calls.get("p50"), calls.get("p75"), calls.get("p90"),
                    calls.get("p95"), calls.get("max"), success_calls.get("p95"),
                    row.get("stable_cap"), planner.get("mean"),
                    row.get("second_call_rate"),
                    row.get("second_call_new_evidence_rate"),
                    row.get("second_call_improvement_rate"),
                    row.get("patch_invalid_runs"), row.get("acceleration_median"),
                    row.get("acceleration_geometric_mean"),
                )
            )
    lines = [
        "# Token Policy Historical Analysis (72 Real Runs)", "",
        "This is an offline analysis of reconciled DeepSeek + Vitis receipts; it made no external model or Vitis calls.",
        "", "| Mode | Runs | Success | Successful call P95 | Stable cap | Planner calls mean | Second-call rate | Second-call improvement | Acceleration median/geomean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        row = by_mode.get(mode, {})
        row = row if isinstance(row, Mapping) else {}
        success_calls = row.get("successful_per_call_output_tokens")
        success_calls = success_calls if isinstance(success_calls, Mapping) else {}
        planner = row.get("planner_calls")
        planner = planner if isinstance(planner, Mapping) else {}
        lines.append(
            f"| {mode} | {row.get('runs')} | {row.get('final_success_rate')} | "
            f"{success_calls.get('p95')} | {row.get('stable_cap')} | "
            f"{planner.get('mean')} | {row.get('second_call_rate')} | "
            f"{row.get('second_call_improvement_rate')} | "
            f"{row.get('acceleration_median')}/{row.get('acceleration_geometric_mean')} |"
        )
    lines.extend(
        (
            "", "## Cap rule", "",
            "Each mode cap is the successful real Planner-call output P95 plus a 15% safety margin, rounded upward to 16 tokens. It is a configurable starting point, not a claim of optimality.",
            "", "## Second-call interpretation", "",
            "A second call has new evidence only when its structured round input contains a new failure record or a Candidate history entry bound to metrics/synthesis evidence. Improvement requires a round-2-or-later Candidate to be promoted or final-verified.",
        )
    )
    (output / "token_policy_historical_analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--safety-margin", type=float, default=0.15)
    args = parser.parse_args(argv)
    value = analyze(args.experiment_root, safety_margin=args.safety_margin)
    write_outputs(value, args.output_dir)
    print(json.dumps(value["generated_mode_stable_caps"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
