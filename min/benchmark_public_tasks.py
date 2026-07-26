#!/usr/bin/env python3
"""Run and summarize repeated live-model/Vitis public-task experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


MIN_DIR = Path(__file__).resolve().parent
HARNESS_DIR = MIN_DIR.parent / "llm4hls_harness"
TASK_ROOT = (
    HARNESS_DIR
    / "task_corpus"
    / "official"
    / "fpt26-harness-public"
)
FLOW_SCRIPT = MIN_DIR / "minimal_flow.py"
TASK_TYPES = {
    "projection_bugfix": "repair",
    "dotProduct_optimize": "optimize",
    "residual_stream_deadlock": "structural",
}
SCHEMA_VERSION = "track-a.minimal-public-benchmark.v1"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _selected_outcome(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    selected_id = result.get("selected_candidate_id")
    key = "candidate" if selected_id == "candidate_001" else "baseline"
    value = result.get(key)
    return value if isinstance(value, Mapping) else None


def _objective_success(
    *,
    task_type: str,
    result: Mapping[str, Any],
) -> bool:
    selected = _selected_outcome(result)
    verified = bool(
        result.get("status") == "DONE"
        and selected is not None
        and selected.get("functional_pass") is True
        and selected.get("synth_pass") is True
    )
    if not verified or result.get("selected_candidate_id") != "candidate_001":
        return False
    if task_type == "optimize":
        return result.get("selection_reason") == (
            "STRICT_WORST_LATENCY_IMPROVEMENT"
        )
    return result.get("selection_reason") == (
        "FUNCTIONAL_AND_SYNTHESIZABLE_REPAIR"
    )


def _validation_stage(
    outcome: Mapping[str, Any] | None,
    stage: str,
) -> Mapping[str, Any]:
    if outcome is None:
        return {}
    validation = outcome.get("validation")
    if not isinstance(validation, Mapping):
        return {}
    value = validation.get(stage)
    return value if isinstance(value, Mapping) else {}


def _action_result(
    run_dir: Path,
    stage: Mapping[str, Any],
) -> Mapping[str, Any]:
    result_ref = stage.get("result_ref")
    if not isinstance(result_ref, str):
        return {}
    path = run_dir / result_ref
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _raw_worst_latency(action: Mapping[str, Any]) -> float | None:
    report = action.get("report")
    if not isinstance(report, Mapping):
        return None
    latency = report.get("latency")
    if not isinstance(latency, Mapping):
        return None
    value = latency.get("worst")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) > 0
    ):
        return float(value)
    return None


def _synth_qualified(action: Mapping[str, Any], target_clock_ns: float) -> bool:
    if action.get("ok") is not True:
        return False
    report = action.get("report")
    if not isinstance(report, Mapping):
        return False
    clock = report.get("estimated_clock_period_ns")
    if (
        not isinstance(clock, (int, float))
        or isinstance(clock, bool)
        or not math.isfinite(float(clock))
        or float(clock) > target_clock_ns
    ):
        return False
    resources = report.get("resources")
    available = report.get("available_resources")
    if not isinstance(resources, Mapping) or not isinstance(available, Mapping):
        return False
    for name in ("LUT", "FF", "DSP", "BRAM_18K", "URAM"):
        used = resources.get(name)
        limit = available.get(name)
        if (
            not isinstance(used, int)
            or isinstance(used, bool)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or used < 0
            or limit <= 0
            or used > limit
        ):
            return False
    return True


def _final_hash_matches(run_dir: Path, expected: object) -> bool:
    if not isinstance(expected, str):
        return False
    path = run_dir / "final_kernel.cpp"
    return path.is_file() and _sha256(path) == expected


def _strict_attempt_facts(
    *,
    task_id: str,
    run_dir: Path,
    result: Mapping[str, Any],
    driver_exit_code: int | None,
) -> dict[str, object]:
    baseline = result.get("baseline")
    candidate = result.get("candidate")
    baseline = baseline if isinstance(baseline, Mapping) else None
    candidate = candidate if isinstance(candidate, Mapping) else None
    baseline_csim = _validation_stage(baseline, "csim")
    baseline_synth = _validation_stage(baseline, "synth")
    baseline_cosim = _validation_stage(baseline, "cosim")
    candidate_csim = _validation_stage(candidate, "csim")
    candidate_synth = _validation_stage(candidate, "synth")
    candidate_cosim = _validation_stage(candidate, "cosim")
    baseline_synth_action = _action_result(run_dir, baseline_synth)
    candidate_synth_action = _action_result(run_dir, candidate_synth)
    baseline_worst = _raw_worst_latency(baseline_synth_action)
    candidate_worst = _raw_worst_latency(candidate_synth_action)
    planner = result.get("planner")
    planner = planner if isinstance(planner, Mapping) else {}
    budget = result.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}
    tool_used = budget.get("tool_used")
    tool_used = tool_used if isinstance(tool_used, Mapping) else {}
    common = bool(
        driver_exit_code in {0, None}
        and result.get("status") == "DONE"
        and result.get("selected_candidate_id") == "candidate_001"
        and result.get("planner_error") is None
        and result.get("patch_error") is None
        and planner.get("provider") == "openai-compatible"
        and planner.get("model") not in {None, "", "none"}
        and tool_used.get("llm") == 1
        and candidate_csim.get("status") == "PASS"
        and candidate_synth.get("status") == "PASS"
        and _final_hash_matches(run_dir, result.get("final_kernel_sha256"))
    )
    if task_id == "projection_bugfix":
        task_specific = bool(
            result.get("mode") == "REPAIR"
            and baseline_csim.get("status") == "FAIL"
            and baseline_synth.get("status") == "NOT_RUN"
            and candidate_cosim.get("status") == "NOT_RUN"
            and result.get("selection_reason")
            == "FUNCTIONAL_AND_SYNTHESIZABLE_REPAIR"
        )
    elif task_id == "dotProduct_optimize":
        task_specific = bool(
            result.get("mode") == "OPTIMIZE"
            and baseline_csim.get("status") == "PASS"
            and baseline_synth.get("status") == "PASS"
            and baseline_worst is not None
            and candidate_worst is not None
            and candidate_worst < baseline_worst
            and candidate_cosim.get("status") == "NOT_RUN"
            and result.get("selection_reason")
            == "STRICT_WORST_LATENCY_IMPROVEMENT"
        )
    else:
        baseline_evidence = baseline_cosim.get("evidence")
        evidence_text = " ".join(
            str(item)
            for item in (
                baseline_evidence
                if isinstance(baseline_evidence, list)
                else []
            )
        ).casefold()
        task_specific = bool(
            result.get("mode") == "STRUCTURAL_FIX"
            and baseline_csim.get("status") == "PASS"
            and baseline_synth.get("status") == "PASS"
            and baseline_cosim.get("status") == "FAIL"
            and "deadlock" in evidence_text
            and candidate_cosim.get("status") == "PASS"
            and result.get("selection_reason")
            == "FUNCTIONAL_AND_SYNTHESIZABLE_REPAIR"
        )
    objective_success = common and task_specific
    patch_path = run_dir / "candidate_001" / "patch.diff"
    patch_text = (
        patch_path.read_text(encoding="utf-8", errors="replace")
        if patch_path.is_file()
        else ""
    )
    interface_change = any(
        line.startswith("+") and "HLS INTERFACE" in line
        for line in patch_text.splitlines()
    )
    qualified_success = bool(
        objective_success
        and not interface_change
        and _synth_qualified(candidate_synth_action, 5.0)
    )
    return {
        "objective_success": objective_success,
        "qualified_success": qualified_success,
        "baseline_raw_worst_latency": baseline_worst,
        "candidate_raw_worst_latency": candidate_worst,
        "high_risk_interface_change": interface_change,
    }


def _record(
    *,
    task_id: str,
    attempt: int,
    run_dir: Path,
    driver_exit_code: int | None,
) -> dict[str, object]:
    result_path = run_dir / "minimal_result.json"
    if not result_path.is_file():
        return {
            "task_id": task_id,
            "task_type": TASK_TYPES[task_id],
            "attempt": attempt,
            "run_dir": str(run_dir),
            "result_present": False,
            "driver_exit_code": driver_exit_code,
            "flow_success": False,
            "objective_success": False,
            "qualified_success": False,
            "tokens": 0,
            "credits": 0,
            "runtime_seconds": 0.0,
            "ledger_runtime_seconds": 0.0,
            "score_proxy": 0.0,
            "failure": "minimal_result.json missing",
        }
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "task_id": task_id,
            "task_type": TASK_TYPES[task_id],
            "attempt": attempt,
            "run_dir": str(run_dir),
            "result_present": False,
            "driver_exit_code": driver_exit_code,
            "flow_success": False,
            "objective_success": False,
            "qualified_success": False,
            "tokens": 0,
            "credits": 0,
            "runtime_seconds": 0.0,
            "ledger_runtime_seconds": 0.0,
            "score_proxy": 0.0,
            "failure": f"invalid minimal_result.json: {exc}",
        }
    budget = result.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}
    selected = _selected_outcome(result)
    flow_success = bool(
        result.get("status") == "DONE"
        and selected is not None
        and selected.get("functional_pass") is True
        and selected.get("synth_pass") is True
    )
    task_type = TASK_TYPES[task_id]
    strict_facts = _strict_attempt_facts(
        task_id=task_id,
        run_dir=run_dir,
        result=result,
        driver_exit_code=driver_exit_code,
    )
    driver_metadata_path = run_dir / "driver_metadata.json"
    try:
        driver_metadata = json.loads(
            driver_metadata_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        driver_metadata = {}
    driver_wall = _number(driver_metadata.get("wall_seconds"), default=-1.0)
    ledger_runtime = _number(budget.get("runtime_used_seconds"))
    runtime = driver_wall if driver_wall >= 0 else ledger_runtime
    return {
        "task_id": task_id,
        "task_type": task_type,
        "attempt": attempt,
        "run_dir": str(run_dir),
        "result_present": True,
        "driver_exit_code": driver_exit_code,
        "status": result.get("status"),
        "mode": result.get("mode"),
        "selected_candidate_id": result.get("selected_candidate_id"),
        "selection_reason": result.get("selection_reason"),
        "flow_success": flow_success,
        **strict_facts,
        "tokens": int(_number(budget.get("tokens_used"))),
        "credits": int(_number(budget.get("credits_used"))),
        "runtime_seconds": runtime,
        "ledger_runtime_seconds": ledger_runtime,
        "score_proxy": _number(result.get("selected_public_score_proxy")),
        "planner_error": result.get("planner_error"),
        "patch_error": result.get("patch_error"),
        "final_kernel_sha256": result.get("final_kernel_sha256"),
    }


def _mean(records: list[dict[str, object]], field: str) -> float:
    return round(
        statistics.fmean(_number(record.get(field)) for record in records),
        4,
    )


def _wilson_95(successes: int, attempts: int) -> list[float]:
    if attempts == 0:
        return [0.0, 0.0]
    z = 1.959963984540054
    proportion = successes / attempts
    denominator = 1.0 + z * z / attempts
    center = (proportion + z * z / (2.0 * attempts)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / attempts
            + z * z / (4.0 * attempts * attempts)
        )
        / denominator
    )
    return [round(center - margin, 4), round(center + margin, 4)]


def summarize(records: list[dict[str, object]]) -> dict[str, object]:
    groups: dict[str, list[dict[str, object]]] = {
        task_id: [] for task_id in TASK_TYPES
    }
    for record in records:
        task_id = str(record["task_id"])
        groups.setdefault(task_id, []).append(record)

    def group_summary(items: list[dict[str, object]]) -> dict[str, object]:
        attempts = len(items)
        flow_successes = sum(record["flow_success"] is True for record in items)
        objective_successes = sum(
            record["objective_success"] is True for record in items
        )
        qualified_successes = sum(
            record["qualified_success"] is True for record in items
        )
        return {
            "attempts": attempts,
            "flow_successes": flow_successes,
            "flow_success_rate": (
                round(flow_successes / attempts, 4) if attempts else 0.0
            ),
            "objective_successes": objective_successes,
            "objective_success_rate": (
                round(objective_successes / attempts, 4) if attempts else 0.0
            ),
            "objective_success_wilson_95": _wilson_95(
                objective_successes,
                attempts,
            ),
            "qualified_successes": qualified_successes,
            "qualified_success_rate": (
                round(qualified_successes / attempts, 4) if attempts else 0.0
            ),
            "averages_over_all_attempts": {
                "tokens": _mean(items, "tokens") if items else 0.0,
                "credits": _mean(items, "credits") if items else 0.0,
                "runtime_seconds": (
                    _mean(items, "runtime_seconds") if items else 0.0
                ),
                "score_proxy": _mean(items, "score_proxy") if items else 0.0,
            },
        }

    ordered = sorted(
        records,
        key=lambda item: (str(item["task_id"]), int(item["attempt"])),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "success_definition": {
            "flow_success": (
                "selected final is functionally valid and synthesizable"
            ),
            "objective_success": (
                "repair/structural selects a verified candidate; optimize "
                "selects a verified candidate with strict worst-latency improvement"
            ),
            "primary_rate": "objective_success_rate",
            "qualified_rate": (
                "objective success plus target clock, resource-fit and no "
                "added HLS INTERFACE pragma"
            ),
            "averages": "computed over all attempts, including failures",
            "score": (
                "public score proxy; not the official hidden-test score"
            ),
        },
        "tasks": {
            task_id: group_summary(groups.get(task_id, []))
            for task_id in TASK_TYPES
        },
        "overall": group_summary(ordered),
        "records": ordered,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument(
        "--tasks",
        nargs="+",
        choices=tuple(TASK_TYPES),
        default=list(TASK_TYPES),
    )
    parser.add_argument("--vitis-root", default=os.environ.get("LLM4HLS_VITIS_HLS_ROOT"))
    parser.add_argument("--model", default=os.environ.get("LLM4HLS_MODEL"))
    parser.add_argument("--planner-timeout", type=float, default=180.0)
    parser.add_argument("--csim-timeout", type=float, default=180.0)
    parser.add_argument("--synth-timeout", type=float, default=900.0)
    parser.add_argument("--cosim-timeout", type=float, default=120.0)
    parser.add_argument("--token-budget", type=int, default=32768)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repeats < 1:
        raise ValueError("--repeats must be positive")
    missing = [
        name
        for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "required environment variables are missing: " + ", ".join(missing)
        )
    if not args.model:
        raise RuntimeError("LLM4HLS_MODEL or --model is required")
    if not args.vitis_root:
        raise RuntimeError("LLM4HLS_VITIS_HLS_ROOT or --vitis-root is required")

    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_unix_seconds": time.time(),
        "repeats": args.repeats,
        "task_order": list(args.tasks),
        "execution_order": "attempt-major interleaved, sequential",
        "model": args.model,
        "endpoint_host": urllib.parse.urlsplit(
            os.environ["OPENAI_BASE_URL"]
        ).hostname,
        "vitis_root": str(Path(args.vitis_root).resolve()),
        "minimal_flow_sha256": _sha256(FLOW_SCRIPT),
        "task_file_sha256": {
            task_id: {
                path.name: _sha256(path)
                for path in sorted((TASK_ROOT / task_id).iterdir())
                if path.is_file()
            }
            for task_id in args.tasks
        },
        "timeouts": {
            "planner": args.planner_timeout,
            "csim": args.csim_timeout,
            "synth": args.synth_timeout,
            "cosim": args.cosim_timeout,
        },
        "token_budget": args.token_budget,
        "max_output_tokens": args.max_output_tokens,
    }
    manifest_path = output_root / "benchmark_manifest.json"
    if not manifest_path.exists():
        _write_json(manifest_path, manifest)

    driver_codes: dict[tuple[str, int], int | None] = {}
    total = args.repeats * len(args.tasks)
    completed = 0
    for attempt in range(1, args.repeats + 1):
        for task_id in args.tasks:
            completed += 1
            run_dir = output_root / task_id / f"run_{attempt:02d}"
            result_path = run_dir / "minimal_result.json"
            if result_path.is_file():
                print(
                    f"[{completed}/{total}] reuse {task_id} run_{attempt:02d}",
                    flush=True,
                )
                driver_codes[(task_id, attempt)] = None
                continue
            run_dir.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable,
                str(FLOW_SCRIPT),
                "--task-dir",
                str(TASK_ROOT / task_id),
                "--live-openai",
                "--run-dir",
                str(run_dir),
                "--backend",
                "vitis",
                "--vitis-root",
                str(args.vitis_root),
                "--model",
                str(args.model),
                "--token-budget",
                str(args.token_budget),
                "--max-output-tokens",
                str(args.max_output_tokens),
                "--planner-timeout",
                str(args.planner_timeout),
                "--csim-timeout",
                str(args.csim_timeout),
                "--synth-timeout",
                str(args.synth_timeout),
                "--cosim-timeout",
                str(args.cosim_timeout),
            ]
            print(
                f"[{completed}/{total}] start {task_id} run_{attempt:02d}",
                flush=True,
            )
            started = time.monotonic()
            started_unix = time.time()
            process = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            driver_codes[(task_id, attempt)] = process.returncode
            (run_dir / "driver.stdout.log").write_text(
                process.stdout,
                encoding="utf-8",
            )
            (run_dir / "driver.stderr.log").write_text(
                process.stderr,
                encoding="utf-8",
            )
            _write_json(
                run_dir / "driver_metadata.json",
                {
                    "started_unix_seconds": started_unix,
                    "finished_unix_seconds": time.time(),
                    "wall_seconds": time.monotonic() - started,
                    "exit_code": process.returncode,
                    "command": command,
                },
            )
            print(
                f"[{completed}/{total}] end {task_id} run_{attempt:02d} "
                f"exit={process.returncode} wall={time.monotonic() - started:.1f}s",
                flush=True,
            )
            records = [
                _record(
                    task_id=current_task,
                    attempt=current_attempt,
                    run_dir=(
                        output_root
                        / current_task
                        / f"run_{current_attempt:02d}"
                    ),
                    driver_exit_code=driver_codes.get(
                        (current_task, current_attempt)
                    ),
                )
                for current_attempt in range(1, args.repeats + 1)
                for current_task in args.tasks
                if (
                    output_root
                    / current_task
                    / f"run_{current_attempt:02d}"
                ).exists()
            ]
            _write_json(output_root / "benchmark_summary.json", summarize(records))

    records = [
        _record(
            task_id=task_id,
            attempt=attempt,
            run_dir=output_root / task_id / f"run_{attempt:02d}",
            driver_exit_code=driver_codes.get((task_id, attempt)),
        )
        for attempt in range(1, args.repeats + 1)
        for task_id in args.tasks
    ]
    summary = summarize(records)
    _write_json(output_root / "benchmark_summary.json", summary)
    print(json.dumps(summary["tasks"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
