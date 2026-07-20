"""Independent CLI for the scripted or live V3 vertical prototype."""

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
            "Run the isolated multi-round V3-B0 LangGraph in scripted or live "
            "OpenAI-compatible Planner mode. "
            "The default demo backend proves orchestration only; use --backend vitis "
            "for real HLS evidence."
        ),
    )
    parser.add_argument(
        "--task-dir",
        required=True,
        help="Path to the public task package.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--patch-file",
        action="append",
        help=(
            "Path to a scripted unified diff. Repeat this option to exercise "
            "multiple optimization rounds in order."
        ),
    )
    mode.add_argument(
        "--live-openai",
        action="store_true",
        help=(
            "Use the non-replayable OpenAI-compatible V3 Planner adapter. "
            "OPENAI_BASE_URL and OPENAI_API_KEY must be set."
        ),
    )
    mode.add_argument(
        "--planner",
        choices=("openai-compatible",),
        help=(
            "Select the real Planner backend. This is the explicit alias for "
            "--live-openai used by V3-B experiments."
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
        "--model",
        default=os.environ.get("LLM4HLS_MODEL", "deepseek-v4-pro"),
        help="OpenAI-compatible model name used by --live-openai.",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        help=(
            "Total LLM token budget. The legacy scripted default remains 4096; "
            "the live default is 32768."
        ),
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=float(os.environ.get("LLM4HLS_LLM_TIMEOUT_S", "120")),
        help="OpenAI-compatible request timeout in seconds.",
    )
    parser.add_argument(
        "--llm-max-output-tokens",
        type=int,
        default=int(os.environ.get("LLM4HLS_LLM_MAX_OUTPUT_TOKENS", "1000")),
        help="Maximum output tokens reserved for each live Planner call.",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=0.0,
        help="OpenAI-compatible sampling temperature.",
    )
    parser.add_argument(
        "--max-planner-rounds",
        type=int,
        help=(
            "Maximum number of live Planner calls. Defaults to 4 for "
            "fast-experiment and 1 for strict compatibility."
        ),
    )
    parser.add_argument(
        "--final-reserve-credits",
        type=int,
        default=25,
        help="Credits the live Planner must leave for final CSim/Synth/CoSim.",
    )
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
    parser.add_argument(
        "--validation-profile",
        choices=("strict", "fast-experiment"),
        default="strict",
        help=(
            "strict preserves V3-A1 validation; fast-experiment skips ordinary "
            "baseline/exploration CoSim while retaining a fresh final closure."
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
        planner = None
        live_openai = bool(
            args.live_openai or args.planner == "openai-compatible"
        )
        planned_rounds = (
            args.max_planner_rounds
            if args.max_planner_rounds is not None
            else 4
            if args.validation_profile == "fast-experiment"
            else 1
        )
        if live_openai:
            base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not base_url:
                raise ValueError("OPENAI_BASE_URL is required with --live-openai")
            if not api_key:
                raise ValueError("OPENAI_API_KEY is required with --live-openai")
            from .openai_provider import (
                OpenAICompatibleConfig,
                OpenAICompatibleOptimizationProvider,
            )
            from .v3_openai_planner import OpenAICompatibleV3PlannerAdapter

            provider = OpenAICompatibleOptimizationProvider(
                OpenAICompatibleConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=str(args.model),
                    timeout_seconds=args.llm_timeout,
                    max_output_tokens=args.llm_max_output_tokens,
                    temperature=args.llm_temperature,
                )
            )
            if args.validation_profile == "fast-experiment":
                planner = OpenAICompatibleV3PlannerAdapter(
                    Path(args.run_dir).resolve(),
                    provider,
                    final_reserve_credits=args.final_reserve_credits,
                    max_output_tokens=args.llm_max_output_tokens,
                    fast_experiment=True,
                    read_only_headers={
                        name: content.decode("utf-8")
                        for name, content in task.headers.items()
                    },
                )
            else:
                planner = OpenAICompatibleV3PlannerAdapter(
                    Path(args.run_dir).resolve(),
                    provider,
                    final_reserve_credits=args.final_reserve_credits,
                    max_output_tokens=args.llm_max_output_tokens,
                    read_only_headers={
                        name: content.decode("utf-8")
                        for name, content in task.headers.items()
                    },
                )
            proposals: tuple[PatchProposal, ...] = ()
            token_limit = 32768 if args.token_budget is None else args.token_budget
        else:
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
            planned_rounds = len(proposals)
            token_limit = 4096 if args.token_budget is None else args.token_budget
        max_final_attempts = (
            2
            if args.enable_final_fallback
            or args.validation_profile == "fast-experiment"
            else 1
        )
        validation_call_limit = planned_rounds + 1 + max_final_attempts
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
                    "llm": planned_rounds,
                },
                token_limit=token_limit,
                runtime_limit_seconds=args.runtime_limit,
            ),
            minimum_frequency_mhz=args.minimum_frequency_mhz,
        )
        backend = DeterministicPrototypeBackend() if args.backend == "demo" else None
        if planner is None:
            result = run_v3_prototype(
                task,
                args.run_dir,
                config,
                proposals,
                backend=backend,
                thread_id=args.thread_id,
                max_no_improvement_rounds=args.max_no_improvement_rounds,
                max_final_attempts=max_final_attempts,
                validation_profile=args.validation_profile,
            )
        else:
            result = run_v3_prototype(
                task,
                args.run_dir,
                config,
                backend=backend,
                planner=planner,
                max_planner_rounds=planned_rounds,
                thread_id=args.thread_id,
                max_no_improvement_rounds=args.max_no_improvement_rounds,
                max_final_attempts=max_final_attempts,
                validation_profile=args.validation_profile,
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
        "validation_profile": result.get("validation_profile"),
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "exploration_stop_reason": result.get("exploration_stop_reason"),
        "rounds_completed": result.get("rounds_completed"),
        "best_candidate_id": result.get("best_candidate_id"),
        "final_candidate_id": result.get("final_candidate_id"),
        "credits_used": result.get("budget", {}).get("credits_used"),
        "tokens_used": result.get("budget", {}).get("tokens_used"),
        "tool_calls": result.get("budget", {}).get("tool_used"),
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
