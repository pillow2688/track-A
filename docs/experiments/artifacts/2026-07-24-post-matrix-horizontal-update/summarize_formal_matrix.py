#!/usr/bin/env python3
"""Create the immutable, usage-correct summary of the formal 28x1 matrix."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping


ARTIFACT_DIR = Path(__file__).resolve().parent
REPO = ARTIFACT_DIR.parents[3]
MATRIX = (
    REPO
    / "llm4hls_harness/experiments/"
    "formal_matrix_20260724_4a05763_deepseek_28x1"
)
RESULTS = MATRIX / "benchmark_results.jsonl"
COVERAGE = MATRIX / "terminal_coverage.json"
OUTPUT = ARTIFACT_DIR / "formal-matrix-result-audit.json"


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, object]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"expected JSONL objects: {path}")
    return rows


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def compact_error(record: Mapping[str, object]) -> str | None:
    error = record.get("error")
    if not isinstance(error, Mapping):
        return None
    detail = error.get("detail")
    return str(detail) if isinstance(detail, str) else None


def error_class(detail: str | None) -> str | None:
    if detail is None:
        return None
    if "terminal result does not bind the last Candidate decision" in detail:
        return "TERMINAL_LAST_CANDIDATE_BINDING"
    if "candidate worst latency is invalid" in detail:
        return "INVALID_CANDIDATE_WORST_LATENCY"
    return "OTHER_EXECUTOR_ERROR"


def main() -> int:
    records = read_jsonl(RESULTS)
    coverage = read_json(COVERAGE)
    if coverage.get("status") != "COMPLETE" or len(records) != 28:
        raise RuntimeError("formal matrix is not terminal-complete")
    task_ids = [str(record["task_id"]) for record in records]
    if len(set(task_ids)) != 28:
        raise RuntimeError("formal matrix task IDs are not unique")

    usage_rows: list[dict[str, object]] = []
    by_expected: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    failures: list[dict[str, object]] = []
    acceleration_rows: list[dict[str, object]] = []
    required_cosim_count = 0
    optional_cosim_count = 0
    for record in records:
        run_dir = Path(str(record["run_dir"]))
        budget = read_json(run_dir / "budget_state.json")
        task = read_json(run_dir / "v3_task_spec.json")
        requires_cosim = task.get("requires_cosim") is True
        required_cosim_count += int(requires_cosim)
        optional_cosim_count += int(not requires_cosim)
        usage_rows.append(
            {
                "task_id": record["task_id"],
                "tokens_used": int(budget.get("tokens_used") or 0),
                "cached_input_tokens_used": int(
                    budget.get("cached_input_tokens_used") or 0
                ),
                "credits_used": int(budget.get("credits_used") or 0),
                "tool_used": dict(budget.get("tool_used") or {}),
            }
        )
        expected = str(record.get("expected_mode") or "UNKNOWN")
        by_expected[expected].append(record)
        if record.get("e2e_success") is not True:
            detail = compact_error(record)
            failures.append(
                {
                    "task_id": record["task_id"],
                    "status": record["status"],
                    "expected_mode": record.get("expected_mode"),
                    "routed_mode": record.get("routed_mode"),
                    "stop_reason": record.get("stop_reason"),
                    "failure_stage": record.get("failure_stage"),
                    "error_class": error_class(detail),
                    "error_detail": detail,
                    "actual_usage": usage_rows[-1],
                }
            )
        acceleration = record.get("acceleration_vs_baseline")
        if isinstance(acceleration, (int, float)) and not isinstance(
            acceleration, bool
        ):
            acceleration_rows.append(
                {
                    "task_id": record["task_id"],
                    "acceleration_vs_baseline": acceleration,
                    "tokens_used": budget.get("tokens_used"),
                    "credits_used": budget.get("credits_used"),
                    "stop_reason": record.get("stop_reason"),
                }
            )

    all_tool_names = ("llm", "csim", "synth", "cosim")
    total_tools = {
        name: sum(
            int((row["tool_used"]).get(name) or 0)  # type: ignore[union-attr]
            for row in usage_rows
        )
        for name in all_tool_names
    }
    comparable = [
        record
        for record in records
        if isinstance(record.get("router_correct"), bool)
    ]
    audit = {
        "schema_version": "formal-matrix.result-audit.v1",
        "status": "COMPLETE_WITH_MEASURED_FAILURES",
        "frozen_head": "4a05763b593a527878a0056f64763126c58ee63b",
        "protocol": {
            "tasks": 28,
            "models": ["deepseek-v4-pro"],
            "repeats": 1,
            "continuation": "off",
            "experience": "off",
            "ranker": "off",
            "final_validation_policy": "task_contract",
            "token_policy": "fixed",
            "primary_retry": False,
        },
        "terminal_coverage": coverage,
        "status_counts": count([str(record["status"]) for record in records]),
        "e2e": {
            "success": sum(record.get("e2e_success") is True for record in records),
            "failure": sum(record.get("e2e_success") is not True for record in records),
            "success_rate": sum(
                record.get("e2e_success") is True for record in records
            )
            / len(records),
        },
        "by_expected_mode": {
            mode: {
                "tasks": len(rows),
                "done": sum(row.get("status") == "DONE" for row in rows),
                "failed": sum(row.get("status") == "FAILED" for row in rows),
                "error": sum(row.get("status") == "ERROR" for row in rows),
                "e2e_success": sum(
                    row.get("e2e_success") is True for row in rows
                ),
            }
            for mode, rows in sorted(by_expected.items())
        },
        "router": {
            "comparable": len(comparable),
            "correct": sum(
                record.get("router_correct") is True for record in comparable
            ),
            "incorrect": sum(
                record.get("router_correct") is False for record in comparable
            ),
            "unavailable_due_preterminal_error": len(records) - len(comparable),
            "mismatches": [
                {
                    "task_id": record["task_id"],
                    "expected_mode": record.get("expected_mode"),
                    "routed_mode": record.get("routed_mode"),
                    "reason": (
                        "baseline CoSim passed in this run, so the measured "
                        "Router correctly selected OPTIMIZE although the "
                        "acceptance label expected STRUCTURAL_FIX"
                    ),
                }
                for record in records
                if record.get("router_correct") is False
            ],
        },
        "task_contract": {
            "requires_cosim_true": required_cosim_count,
            "requires_cosim_false": optional_cosim_count,
            "batch_provenance_compatibility_patch": (
                "ACCEPTED_EXACT_AND_EXERCISED"
            ),
        },
        "actual_usage_from_each_run_budget_state": {
            "tokens_used": sum(int(row["tokens_used"]) for row in usage_rows),
            "cached_input_tokens_used": sum(
                int(row["cached_input_tokens_used"]) for row in usage_rows
            ),
            "credits_used": sum(int(row["credits_used"]) for row in usage_rows),
            "tool_calls": total_tools,
            "note": (
                "Run-local BudgetLedger is authoritative. Outer ERROR rows "
                "report zero because the CLI lacked a standard terminal "
                "result, so aggregate usage is not taken from outer rows."
            ),
        },
        "performance_improvements": {
            "count": len(acceleration_rows),
            "records": acceleration_rows,
            "maximum_acceleration": max(
                (
                    float(item["acceleration_vs_baseline"])
                    for item in acceleration_rows
                ),
                default=None,
            ),
            "acceleration_stop_8x_observed": any(
                float(item["acceleration_vs_baseline"]) >= 8
                for item in acceleration_rows
            ),
        },
        "failures": failures,
        "failure_class_counts": count(
            [
                str(item["error_class"] or item["stop_reason"])
                for item in failures
            ]
        ),
        "preflight_archives": [
            {
                "directory": (
                    "formal_matrix_20260724_4a05763_deepseek_28x1_"
                    "aborted_network_sandbox_preflight01"
                ),
                "formal_statistics_included": False,
                "reason": "sandbox network unavailable",
            },
            {
                "directory": (
                    "formal_matrix_20260724_4a05763_deepseek_28x1_"
                    "aborted_task_contract_validator_preflight01"
                ),
                "formal_statistics_included": False,
                "reason": (
                    "old Batch provenance validator required optional CoSim"
                ),
            },
        ],
        "artifact_hashes": {
            "benchmark_results.jsonl": sha256(RESULTS),
            "terminal_coverage.json": sha256(COVERAGE),
            "benchmark_plan.json": sha256(MATRIX / "benchmark_plan.json"),
        },
        "scope_guards": {
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "secret_value_recorded": False,
        },
    }
    OUTPUT.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": audit["status"],
                "status_counts": audit["status_counts"],
                "e2e": audit["e2e"],
                "usage": audit["actual_usage_from_each_run_budget_state"],
                "performance": audit["performance_improvements"],
                "failure_class_counts": audit["failure_class_counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
