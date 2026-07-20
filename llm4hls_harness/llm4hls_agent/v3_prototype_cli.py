"""Independent CLI for the V3-A0 vertical prototype."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .budget import BudgetConfig
from .repair import PatchProposal
from .task import load_public_task
from .tools import ToolConfig
from .workflow import RunConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm4hls-v3-prototype",
        description=(
            "Run the isolated deterministic multi-round V3-A0 LangGraph. "
            "The default demo backend proves orchestration only; use --backend vitis "
            "for real HLS evidence."
        ),
    )
    parser.add_argument(
        "--task-dir",
        required=True,
        help="Path to the public task package.",
    )
    parser.add_argument(
        "--patch-file",
        required=True,
        action="append",
        help=(
            "Path to a scripted unified diff. Repeat this option to exercise "
            "multiple optimization rounds in order."
        ),
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--thread-id", default="v3a0-prototype")
    parser.add_argument(
        "--backend",
        choices=("demo", "vitis"),
        default="demo",
    )
    parser.add_argument(
        "--vitis-root",
        default=os.environ.get(
            "LLM4HLS_VITIS_HLS_ROOT", "/opt/xilinx/2025.2/Vitis"
        ),
    )
    parser.add_argument(
        "--toolchain-id",
        default=os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"),
    )
    parser.add_argument("--credit-limit", type=int)
    parser.add_argument("--runtime-limit", type=float, default=7200.0)
    parser.add_argument("--csim-timeout", type=float, default=300.0)
    parser.add_argument("--synth-timeout", type=float, default=1800.0)
    parser.add_argument("--cosim-timeout", type=float, default=1800.0)
    parser.add_argument("--minimum-frequency-mhz", type=float, default=100.0)
    parser.add_argument(
        "--max-no-improvement-rounds",
        type=int,
        default=2,
        help="Stop after this many consecutive rejected/non-improving rounds.",
    )
    parser.add_argument(
        "--enable-final-fallback",
        action="store_true",
        help=(
            "Allow one additional fresh final CSim/Synth/CoSim attempt when "
            "credits remain. Disabled by default to preserve legacy run identity."
        ),
    )
    return parser


def _print_error(error_type: str, detail: str, **extra: object) -> None:
    payload: dict[str, object] = {
        "status": "ERROR",
        "error_type": error_type,
        "detail": detail,
    }
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    vitis_root = Path(args.vitis_root).expanduser()

    if args.backend == "vitis":
        settings_path = vitis_root / "settings64.sh"
        if not settings_path.is_file():
            _print_error(
                "VITIS_PREFLIGHT_FAILED",
                "The Vitis root must contain settings64.sh; the Graph was not started.",
                backend="vitis",
                expected_settings64=str(settings_path),
                credits_used=0,
            )
            return 3

    try:
        # Keep the optional LangGraph dependency out of module import so that
        # ``llm4hls-v3-prototype --help`` works with the base installation.
        from .v3_prototype import DeterministicPrototypeBackend, run_v3_prototype
    except ModuleNotFoundError as exc:
        _print_error(
            "V3_OPTIONAL_DEPENDENCY_MISSING",
            "Install the V3 optional dependencies before running this command.",
            missing_module=exc.name,
            install="python -m pip install -e 'llm4hls_harness[v3]'",
        )
        return 3

    try:
        task = load_public_task(args.task_dir)
        proposals = tuple(
            PatchProposal(
                patch=Path(patch_file).read_text(encoding="utf-8"),
                provider="scripted-v3a0-prototype",
                model="operator-supplied-patch-v1",
                hypothesis=(
                    "Evaluate the operator-supplied scripted prototype patch."
                    if len(args.patch_file) == 1
                    else f"Evaluate operator-supplied scripted patch {index}."
                ),
                change_class="scripted",
                expected_effect=(
                    "Measure the supplied patch against the verified baseline."
                    if len(args.patch_file) == 1
                    else "Measure the supplied patch against the current verified incumbent."
                ),
                risk="operator-supplied: full validation remains mandatory",
                required_validation=("csim", "synth", "cosim"),
            )
            for index, patch_file in enumerate(args.patch_file, 1)
        )
        validation_call_limit = len(proposals) + (
            3 if args.enable_final_fallback else 2
        )
        config = RunConfig(
            tool=ToolConfig(
                vitis_root=str(vitis_root),
                part=task.part,
                clock_ns=task.clock_ns,
                timeouts={
                    "csim": args.csim_timeout,
                    "synth": args.synth_timeout,
                    "cosim": args.cosim_timeout,
                },
                toolchain_id=str(args.toolchain_id),
            ),
            budget=BudgetConfig(
                credit_limit=(
                    int(args.credit_limit)
                    if args.credit_limit is not None
                    else task.budget
                ),
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={
                    "csim": validation_call_limit,
                    "synth": validation_call_limit,
                    "cosim": validation_call_limit,
                    "llm": len(proposals),
                },
                token_limit=4096,
                runtime_limit_seconds=args.runtime_limit,
            ),
            minimum_frequency_mhz=args.minimum_frequency_mhz,
        )
        backend = DeterministicPrototypeBackend() if args.backend == "demo" else None
        result = run_v3_prototype(
            task,
            args.run_dir,
            config,
            proposals,
            backend=backend,
            thread_id=args.thread_id,
            max_no_improvement_rounds=args.max_no_improvement_rounds,
            max_final_attempts=2 if args.enable_final_fallback else 1,
        )
    except Exception as exc:
        _print_error(type(exc).__name__, str(exc))
        return 3
    done = result.get("status") == "DONE"
    summary = {
        "workflow": result.get("workflow"),
        "evidence": (
            "ORCHESTRATION_SMOKE_ONLY"
            if args.backend == "demo"
            else ("REAL_VITIS_VALIDATED" if done else "REAL_VITIS_ATTEMPT_FAILED")
        ),
        "task_id": result.get("task_id"),
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "exploration_stop_reason": result.get("exploration_stop_reason"),
        "rounds_completed": result.get("rounds_completed"),
        "best_candidate_id": result.get("best_candidate_id"),
        "final_candidate_id": result.get("final_candidate_id"),
        "credits_used": result.get("budget", {}).get("credits_used"),
        "run_dir": str(Path(args.run_dir).resolve()),
        "result_ref": "v3_prototype_result.json",
        "report_ref": "v3_team_report.md",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if done else 2


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
