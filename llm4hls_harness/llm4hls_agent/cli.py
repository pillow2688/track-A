"""Reproducible command-line entry point for the deterministic V0 harness."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .budget import BudgetConfig, BudgetError
from .task import TaskPackageError, load_public_task
from .tools import ToolBackend, ToolConfig
from .workflow import RunArtifactError, RunConfig, run_v0


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm4hls-v0",
        description="Run an immutable baseline through metered csim/synth/cosim.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="execute the deterministic V0 workflow")
    run.add_argument("task_dir", type=Path, help="reference-compatible public task package")
    run.add_argument("--run-dir", type=Path, required=True, help="durable artifact directory")
    run.add_argument(
        "--vitis-root",
        default=os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "/opt/xilinx/2025.2/Vitis"),
    )
    run.add_argument("--part", default=None, help="override task target part")
    run.add_argument("--clock-ns", type=float, default=None, help="override task clock period")
    run.add_argument(
        "--toolchain-id",
        default=os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"),
        help="declared toolchain identity included in action cache keys",
    )
    run.add_argument("--credit-limit", type=int, default=None)
    run.add_argument("--cost-csim", type=int, default=_env_int("LLM4HLS_COST_CSIM", 1))
    run.add_argument("--cost-synth", type=int, default=_env_int("LLM4HLS_COST_SYNTH", 4))
    run.add_argument("--cost-cosim", type=int, default=_env_int("LLM4HLS_COST_COSIM", 20))
    run.add_argument("--max-csim-calls", type=int, default=None)
    run.add_argument("--max-synth-calls", type=int, default=None)
    run.add_argument("--max-cosim-calls", type=int, default=None)
    run.add_argument(
        "--csim-timeout",
        type=float,
        default=_env_float("LLM4HLS_CSIM_TIMEOUT_S", 180.0),
    )
    run.add_argument(
        "--synth-timeout",
        type=float,
        default=_env_float("LLM4HLS_SYNTH_TIMEOUT_S", 600.0),
    )
    run.add_argument(
        "--cosim-timeout",
        type=float,
        default=_env_float("LLM4HLS_COSIM_TIMEOUT_S", 900.0),
    )
    run.add_argument(
        "--token-budget",
        type=int,
        default=_env_int("LLM4HLS_TOKEN_BUDGET", 32768),
    )
    run.add_argument(
        "--runtime-limit",
        type=float,
        default=_env_float("LLM4HLS_RUNTIME_LIMIT_S", 3600.0),
    )
    run.add_argument(
        "--minimum-frequency-mhz",
        type=float,
        default=_env_float("LLM4HLS_MIN_FREQUENCY_MHZ", 100.0),
    )
    return parser


def _print_error(exc: Exception) -> None:
    print(
        json.dumps(
            {"status": "ERROR", "error_type": type(exc).__name__, "detail": str(exc)},
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def main(
    argv: Sequence[str] | None = None, *, backend: ToolBackend | None = None
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except ValueError as exc:
        _print_error(exc)
        return 3
    if args.command != "run":
        raise AssertionError(f"unsupported command: {args.command}")
    try:
        task = load_public_task(args.task_dir)
        credit_limit = args.credit_limit
        if credit_limit is None:
            credit_limit = int(os.environ.get("LLM4HLS_CREDIT_BUDGET", task.budget))
        tool = ToolConfig(
            vitis_root=str(args.vitis_root),
            part=str(args.part or task.part),
            clock_ns=float(args.clock_ns if args.clock_ns is not None else task.clock_ns),
            timeouts={
                "csim": args.csim_timeout,
                "synth": args.synth_timeout,
                "cosim": args.cosim_timeout,
            },
            toolchain_id=str(args.toolchain_id),
        )
        budget = BudgetConfig(
            credit_limit=credit_limit,
            costs={
                "csim": args.cost_csim,
                "synth": args.cost_synth,
                "cosim": args.cost_cosim,
            },
            tool_limits={
                "csim": args.max_csim_calls,
                "synth": args.max_synth_calls,
                "cosim": args.max_cosim_calls,
            },
            token_limit=args.token_budget,
            runtime_limit_seconds=args.runtime_limit,
        )
        result = run_v0(
            task,
            args.run_dir,
            RunConfig(
                tool=tool,
                budget=budget,
                minimum_frequency_mhz=args.minimum_frequency_mhz,
            ),
            backend=backend,
        )
    except (TaskPackageError, BudgetError, RunArtifactError, ValueError) as exc:
        _print_error(exc)
        return 3

    summary = {
        "task_id": result["task_id"],
        "run_id": result["run_id"],
        "run_dir": str(Path(args.run_dir).resolve()),
        "status": result["status"],
        "stop_reason": result["stop_reason"],
        "credits_used": result["budget"]["credits_used"],  # type: ignore[index]
        "cache_hits": result["cache_hits"],
        "baseline_unchanged": result["baseline_unchanged"],
        "result_ref": "workflow_result.json",
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result["status"] == "DONE" else 2


def main_entry() -> None:
    raise SystemExit(main())
