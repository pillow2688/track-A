"""Independent CLI for the scripted or live V3 vertical prototype."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .budget import (
    BudgetConfig,
    TokenBudgetLimits,
    TokenBudgetPolicy,
    TokenEstimator,
)
from .repair import PatchProposal
from .task import load_public_task
from .tools import ToolConfig
from .workflow import RunConfig


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive integer without making malformed shell state silent."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


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
    parser.add_argument(
        "--cost-csim",
        type=int,
        default=_env_positive_int("LLM4HLS_COST_CSIM", 1),
        help="Configured CSim Credit cost (default: LLM4HLS_COST_CSIM or 1).",
    )
    parser.add_argument(
        "--cost-synth",
        type=int,
        default=_env_positive_int("LLM4HLS_COST_SYNTH", 4),
        help="Configured Synth Credit cost (default: LLM4HLS_COST_SYNTH or 4).",
    )
    parser.add_argument(
        "--cost-cosim",
        type=int,
        default=_env_positive_int("LLM4HLS_COST_COSIM", 20),
        help="Configured CoSim Credit cost (default: LLM4HLS_COST_COSIM or 20).",
    )
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
        "--run-token-limit",
        type=int,
        help="Unambiguous alias for the total run Token limit; overrides --token-budget.",
    )
    parser.add_argument(
        "--token-budget-policy",
        choices=("fixed", "dynamic", "hybrid"),
        default="fixed",
        help=(
            "fixed preserves V3-D; dynamic enables v3.token-policy.v1; "
            "hybrid enables empirical stable caps and the Planner-call gate."
        ),
    )
    parser.add_argument(
        "--token-policy-config",
        help=(
            "Hybrid policy JSON override. By default the versioned package "
            "configuration generated from the 72-run historical analysis is used."
        ),
    )
    parser.add_argument(
        "--token-budget-visibility",
        choices=("hidden", "visible"),
        default="visible",
        help=(
            "For dynamic policy, hidden enforces the Provider cap without adding "
            "TokenEnvelope text; visible also shows the matching envelope and "
            "pressure guidance to the Planner."
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
        "--max-output-tokens",
        type=int,
        help="Optional global configured output cap below mode/provider limits.",
    )
    parser.add_argument("--repair-max-output-tokens", type=int, default=1400)
    parser.add_argument("--synth-fix-max-output-tokens", type=int, default=1800)
    parser.add_argument("--structural-fix-max-output-tokens", type=int, default=2200)
    parser.add_argument("--optimize-max-output-tokens", type=int, default=2400)
    parser.add_argument("--minimum-viable-output-tokens", type=int)
    parser.add_argument("--context-window-tokens", type=int, default=32768)
    parser.add_argument("--context-safety-margin-tokens", type=int, default=256)
    parser.add_argument("--token-safety-margin", type=int, default=128)
    parser.add_argument("--future-round-token-reserve", type=int, default=1800)
    parser.add_argument("--final-token-reserve", type=int, default=0)
    parser.add_argument("--guidance-ratio", type=float, default=0.15)
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=0.0,
        help="OpenAI-compatible sampling temperature.",
    )
    parser.add_argument(
        "--llm-top-p",
        type=float,
        help=(
            "Optional explicit OpenAI-compatible top_p. Omitted preserves the "
            "Provider default; controlled matrices should set it explicitly."
        ),
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
        "--continuation-policy",
        choices=("off", "shadow", "enforce"),
        default="shadow",
        help=(
            "Value-gated follow-up policy. shadow persists a decision without "
            "changing routing; enforce uses the existing stop/final edges."
        ),
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
    parser.add_argument(
        "--experience-mode",
        choices=("off", "shadow", "guided"),
        default="shadow",
        help=(
            "off preserves V3-D exactly; shadow records bounded advice without "
            "changing the model request; guided adds that advice to Planner context."
        ),
    )
    parser.add_argument(
        "--experience-store",
        help=(
            "Read-only JSONL seed store used for retrieval. New Candidate "
            "experience is written under the run directory, never back here."
        ),
    )
    parser.add_argument(
        "--experience-task-split",
        choices=("train", "dev", "hidden_like", "unknown"),
        default="unknown",
        help="Public split label used by experience leakage controls.",
    )
    parser.add_argument(
        "--experience-max-guidance-tokens",
        "--guidance-token-cap",
        type=int,
        default=600,
        help="Conservative upper bound for the serialized advisory summary.",
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
    experience_ingest: dict[str, object] = {
        "planner_status": "NOT_REQUESTED",
        "postprocess_status": "NOT_REQUESTED",
    }

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
            live_token_limit = (
                args.run_token_limit
                if args.run_token_limit is not None
                else 32768
                if args.token_budget is None
                else args.token_budget
            )
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
            from .v3_experience_guidance import ExperienceCoordinator
            from .v3_experience_store import JsonlExperienceRepository

            run_root = Path(args.run_dir).resolve()
            seed_store = (
                Path(args.experience_store).expanduser().resolve()
                if args.experience_store
                else run_root / "experience" / "empty_seed.jsonl"
            )
            if args.experience_store and not seed_store.is_file():
                raise ValueError(
                    "--experience-store must name an existing JSONL file"
                )
            experience_coordinator = None
            if args.experience_mode != "off":
                try:
                    experience_coordinator = ExperienceCoordinator(
                        JsonlExperienceRepository(seed_store),
                        recommendation_path=(
                            run_root
                            / "experience"
                            / "experience_recommendations.jsonl"
                        ),
                        max_guidance_tokens=args.experience_max_guidance_tokens,
                    )
                except Exception as exc:
                    if args.experience_mode == "guided":
                        raise
                    # Shadow is observational and must never prevent the
                    # pre-V3-E Planner from running.
                    experience_ingest.update(
                        {
                            "planner_status": "SHADOW_DISABLED",
                            "planner_error_type": type(exc).__name__,
                        }
                    )
                else:
                    experience_ingest["planner_status"] = "ACTIVE"

            provider = OpenAICompatibleOptimizationProvider(
                OpenAICompatibleConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=str(args.model),
                    timeout_seconds=args.llm_timeout,
                    max_output_tokens=args.llm_max_output_tokens,
                    temperature=args.llm_temperature,
                    top_p=args.llm_top_p,
                )
            )
            token_policy = None
            token_estimator = None
            if args.token_budget_policy in {"dynamic", "hybrid"}:
                hybrid_config: dict[str, object] = {}
                if args.token_budget_policy == "hybrid":
                    hybrid_path = (
                        Path(args.token_policy_config).expanduser().resolve()
                        if args.token_policy_config
                        else Path(__file__).with_name("config")
                        / "token_policy_hybrid_v2.json"
                    )
                    hybrid_config = json.loads(
                        hybrid_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(hybrid_config, dict)
                        or hybrid_config.get("schema_version")
                        != "v3e.token-policy-hybrid-config.v1"
                    ):
                        raise ValueError("invalid Hybrid Token Policy config")
                configured_caps = (
                    hybrid_config.get("mode_output_caps")
                    if hybrid_config
                    else {
                        "REPAIR": args.repair_max_output_tokens,
                        "SYNTH_FIX": args.synth_fix_max_output_tokens,
                        "STRUCTURAL_FIX": args.structural_fix_max_output_tokens,
                        "OPTIMIZE": args.optimize_max_output_tokens,
                    }
                )
                configured_minimums = (
                    hybrid_config.get("mode_minimum_viable_output")
                    if hybrid_config
                    else None
                )
                if not isinstance(configured_caps, dict):
                    raise ValueError("Hybrid mode_output_caps must be an object")
                if configured_minimums is not None and not isinstance(
                    configured_minimums, dict
                ):
                    raise ValueError(
                        "Hybrid mode_minimum_viable_output must be an object"
                    )
                limit_kwargs: dict[str, object] = {
                    "mode_output_caps": configured_caps,
                    "configured_max_output_tokens": args.max_output_tokens,
                    "minimum_viable_output_tokens": (
                        args.minimum_viable_output_tokens
                    ),
                    "provider_hard_output_cap": args.llm_max_output_tokens,
                    "context_window_tokens": args.context_window_tokens,
                    "context_safety_margin_tokens": (
                        args.context_safety_margin_tokens
                    ),
                    "token_budget_safety_margin": args.token_safety_margin,
                    "future_round_token_reserve": (
                        args.future_round_token_reserve
                    ),
                    "final_token_reserve": args.final_token_reserve,
                    "configured_guidance_cap": (
                        args.experience_max_guidance_tokens
                    ),
                    "guidance_ratio": args.guidance_ratio,
                }
                if configured_minimums is not None:
                    limit_kwargs["mode_minimum_viable_output"] = (
                        configured_minimums
                    )
                token_policy = TokenBudgetPolicy(
                    TokenBudgetLimits(**limit_kwargs),
                    profile=args.token_budget_policy,
                )
                token_estimator = TokenEstimator.for_model(str(args.model))
            token_policy_kwargs = (
                {
                    "token_budget_policy": token_policy,
                    "token_estimator": token_estimator,
                    "token_budget_visible": (
                        args.token_budget_policy == "hybrid"
                        or args.token_budget_visibility == "visible"
                    ),
                }
                if token_policy is not None
                else {}
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
                    experience_mode=args.experience_mode,
                    experience_coordinator=experience_coordinator,
                    experience_task_split=args.experience_task_split,
                    **token_policy_kwargs,
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
                    experience_mode=args.experience_mode,
                    experience_coordinator=experience_coordinator,
                    experience_task_split=args.experience_task_split,
                    **token_policy_kwargs,
                )
            proposals: tuple[PatchProposal, ...] = ()
            token_limit = live_token_limit
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
            token_limit = (
                args.run_token_limit
                if args.run_token_limit is not None
                else 4096
                if args.token_budget is None
                else args.token_budget
            )
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
                costs={
                    "csim": args.cost_csim,
                    "synth": args.cost_synth,
                    "cosim": args.cost_cosim,
                    "llm": 0,
                },
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
                continuation_policy_mode=args.continuation_policy,
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
                continuation_policy_mode=args.continuation_policy,
            )
        if args.experience_mode != "off":
            # Rebuild Candidate-level records from terminal, hash-bound run
            # artifacts.  This is idempotent across CLI retries/checkpoint
            # resumes and intentionally writes to a run-local store so a
            # frozen pilot seed cannot learn from earlier tasks in the batch.
            experience_dir = Path(args.run_dir).resolve() / "experience"
            try:
                from .v3_experience_importer import (
                    ImportPolicy,
                    import_historical_runs,
                    write_import_artifacts,
                )
                from .v3_experience_store import JsonlExperienceRepository

                local_repository = JsonlExperienceRepository(
                    experience_dir / "experience_records.jsonl"
                )
                imported = import_historical_runs(
                    [Path(args.run_dir).resolve() / "v3_prototype_result.json"],
                    local_repository,
                    policy=ImportPolicy(task_split=args.experience_task_split),
                )
                write_import_artifacts(imported, experience_dir)
                experience_ingest.update(
                    {
                        "postprocess_status": "COMPLETE",
                        "records": len(imported.records),
                        "inserted": len(imported.inserted_record_ids),
                        "duplicates": len(imported.duplicate_record_ids),
                    }
                )
            except Exception as exc:
                # Experience export is post-terminal advisory work.  Its
                # failure cannot turn a successful HLS run into CLI failure.
                experience_ingest.update(
                    {
                        "postprocess_status": "FAILED_OPEN",
                        "postprocess_error_type": type(exc).__name__,
                    }
                )
            # Recommendation attribution has an independent fail-open boundary:
            # a Candidate import problem must not hide whether the Planner
            # followed the frozen recommendation, and vice versa.
            try:
                from .v3_experience_attribution import (
                    persist_recommendation_attributions,
                )

                attribution = persist_recommendation_attributions(
                    Path(args.run_dir).resolve(), result
                )
                experience_ingest.update(
                    {
                        "attribution_status": "COMPLETE",
                        "attribution_records": attribution["record_count"],
                        "attribution_inserted": attribution["inserted"],
                        "attribution_duplicates": attribution["duplicates"],
                    }
                )
            except Exception as exc:
                experience_ingest.update(
                    {
                        "attribution_status": "FAILED_OPEN",
                        "attribution_error_type": type(exc).__name__,
                    }
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
        "experience_mode": args.experience_mode,
        "experience_ingest": experience_ingest,
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
