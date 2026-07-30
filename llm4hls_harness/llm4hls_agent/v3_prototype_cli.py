"""Independent CLI for the scripted or live V3 vertical prototype."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .budget import (
    REFERENCE_DEVELOPMENT_MAX_CREDITS,
    REFERENCE_DEVELOPMENT_MAX_TOKENS,
    BudgetConfig,
    TokenBudgetLimits,
    TokenBudgetPolicy,
    TokenEstimator,
    resolve_agent_budget_limit,
    resolve_reference_tool_cost,
)
from .repair import PatchProposal
from .runtime_control import (
    DEFAULT_CLEANUP_RESERVE_SECONDS,
    DEFAULT_COSIM_MINIMUM_RUNTIME_SECONDS,
    RuntimeDeadline,
)
from .task import load_public_task
from .tools import ToolConfig
from .vitis import detect_vitis_toolchain
from .workflow import RunConfig


def _parser(
    *, experimental_token_policy: bool = False
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm4hls-v3-prototype",
        allow_abbrev=False,
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
        help="Configured CSim Credit cost (default: LLM4HLS_COST_CSIM or 1).",
    )
    parser.add_argument(
        "--cost-synth",
        type=int,
        help="Configured Synth Credit cost (default: LLM4HLS_COST_SYNTH or 4).",
    )
    parser.add_argument(
        "--cost-cosim",
        type=int,
        help="Configured CoSim Credit cost (default: LLM4HLS_COST_COSIM or 20).",
    )
    parser.add_argument("--runtime-limit", type=float, default=7200.0)
    parser.add_argument(
        "--run-deadline-monotonic",
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--cleanup-reserve-seconds",
        type=float,
        default=DEFAULT_CLEANUP_RESERVE_SECONDS,
        help="Seconds reserved for process cleanup and durable terminal output.",
    )
    parser.add_argument(
        "--cosim-minimum-runtime-seconds",
        type=float,
        default=DEFAULT_COSIM_MINIMUM_RUNTIME_SECONDS,
        help="Minimum effective CoSim window required before CoSim may start.",
    )
    parser.add_argument("--csim-timeout", type=float, default=300.0)
    parser.add_argument("--synth-timeout", type=float, default=1800.0)
    parser.add_argument("--cosim-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--baseline-cosim-probe-timeout",
        type=float,
        default=float(
            os.environ.get("LLM4HLS_BASELINE_COSIM_PROBE_TIMEOUT_S", "300")
        ),
        help=(
            "Maximum seconds for baseline CoSim probing; the Agent retains "
            "time for Candidate validation and independent certification."
        ),
    )
    parser.add_argument(
        "--cosim-no-progress-timeout",
        type=float,
        default=float(
            os.environ.get("LLM4HLS_COSIM_NO_PROGRESS_TIMEOUT_S", "0")
        ),
        help=(
            "Optional RTL testcase no-progress timeout for CoSim; 0 disables "
            "the early timeout and preserves the configured CoSim maximum."
        ),
    )
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
        choices=(
            ("fixed", "dynamic")
            if experimental_token_policy
            else ("fixed",)
        ),
        default="fixed",
        help=(
            "The formal product CLI accepts fixed only."
            if not experimental_token_policy
            else "Experimental fixed/dynamic Token Policy selector."
        ),
    )
    if experimental_token_policy:
        parser.add_argument(
            "--token-budget-visibility",
            choices=("hidden", "visible"),
            default="visible",
            help=(
                "For dynamic policy, hidden enforces the Provider cap without "
                "adding TokenEnvelope text; visible also shows the matching "
                "envelope and pressure guidance to the Planner."
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
    if experimental_token_policy:
        parser.add_argument(
            "--max-output-tokens",
            type=int,
            help="Optional global configured output cap below mode/provider limits.",
        )
        parser.add_argument("--repair-max-output-tokens", type=int, default=1400)
        parser.add_argument("--synth-fix-max-output-tokens", type=int, default=1800)
        parser.add_argument(
            "--structural-fix-max-output-tokens", type=int, default=2200
        )
        parser.add_argument("--optimize-max-output-tokens", type=int, default=2400)
        parser.add_argument("--minimum-viable-output-tokens", type=int)
        parser.add_argument("--context-window-tokens", type=int, default=32768)
        parser.add_argument(
            "--context-safety-margin-tokens", type=int, default=256
        )
        parser.add_argument("--token-safety-margin", type=int, default=128)
        parser.add_argument("--future-round-token-reserve", type=int, default=1800)
        parser.add_argument(
            "--search-closeout-token-reserve", type=int, default=0
        )
        parser.add_argument("--guidance-ratio", type=float, default=0.15)
    else:
        # Keep one implementation path while removing Dynamic-only controls
        # from the formal product surface.
        parser.set_defaults(
            token_budget_visibility="visible",
            max_output_tokens=None,
            repair_max_output_tokens=1400,
            synth_fix_max_output_tokens=1800,
            structural_fix_max_output_tokens=2200,
            optimize_max_output_tokens=2400,
            minimum_viable_output_tokens=None,
            context_window_tokens=32768,
            context_safety_margin_tokens=256,
            token_safety_margin=128,
            future_round_token_reserve=1800,
            search_closeout_token_reserve=0,
            guidance_ratio=0.15,
        )
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
        "--search-closeout-reserve-credits",
        type=int,
        default=25,
        help=(
            "Agent-search Credits retained for candidate closeout. This is "
            "not a final-certification budget; 25 is only a configurable "
            "development reference."
        ),
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
        "--continuation-policy-version",
        choices=("v2",),
        default="v2",
        help=(
            "Active mode-specific, leakage-safe Continuation interface. "
            "The removed v1 policy is not a product runtime option."
        ),
    )
    parser.add_argument(
        "--continuation-admission-manifest",
        help=(
            "Hash-bound online Shadow Gate evidence. Required for "
            "--continuation-policy enforce and rejected in off/shadow mode."
        ),
    )
    parser.add_argument(
        "--enable-final-fallback",
        action="store_true",
        help=(
            "Allow one additional Agent-search closeout attempt when search "
            "Credits remain; this never reserves independent certification."
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
        "--final-validation-policy",
        choices=("task_contract", "full_internal_audit"),
        default="task_contract",
        help=(
            "Agent-search closeout contract: task_contract runs CoSim only when "
            "required; full_internal_audit always runs CSim/Synth/CoSim. The "
            "independent certification always runs all three stages separately."
        ),
    )
    parser.add_argument(
        "--evidence-memory",
        choices=("off", "on"),
        default="on",
        help=(
            "A1 Structured Evidence Memory. off removes cross-Candidate "
            "history/Delta from subsequent Planner rounds while retaining "
            "tool PASS/FAIL, validation, and final certification."
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
        "--experience-ranker-version",
        choices=("v1", "v3"),
        default="v1",
        help=(
            "v1 preserves the legacy advisory coordinator. v3 consumes the "
            "frozen Experience V2 store and uses independent-family verified "
            "labels; guided mode requires v3 plus a passing admission manifest."
        ),
    )
    parser.add_argument(
        "--experience-admission-manifest",
        help=(
            "Hash-bound fixed-protocol Ranker V3 Gate result. Required for "
            "--experience-mode guided; ignored by the legacy v1 coordinator."
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


def main(
    argv: list[str] | None = None,
    *,
    experimental_token_policy: bool = False,
) -> int:
    args = _parser(
        experimental_token_policy=experimental_token_policy
    ).parse_args(argv)
    run_deadline_monotonic = (
        float(args.run_deadline_monotonic)
        if args.run_deadline_monotonic is not None
        else time.monotonic() + float(args.runtime_limit)
    )
    vitis_root = Path(args.vitis_root).expanduser()
    experience_ingest: dict[str, object] = {
        "planner_status": "NOT_REQUESTED",
        "postprocess_status": "EXTERNAL_OFFLINE",
        "postprocess_entry": "llm4hls-experience postprocess-run",
    }

    if args.backend == "vitis":
        toolchain = detect_vitis_toolchain(vitis_root)
        if toolchain.preflight_result != "READY":
            _print_error(
                "VITIS_PREFLIGHT_FAILED",
                "No supported Vitis executable was found; the Graph was not started.",
                backend="vitis",
                accepted_executables=["vitis-run", "vitis_hls"],
                selection_order=[
                    "root/bin/vitis-run",
                    "PATH:vitis-run",
                    "root/bin/vitis_hls",
                    "PATH:vitis_hls",
                ],
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
        token_override = (
            args.run_token_limit
            if args.run_token_limit is not None
            else args.token_budget
        )
        token_override_source = (
            "cli:--run-token-limit"
            if args.run_token_limit is not None
            else "cli:--token-budget"
        )
        credit_limit = resolve_agent_budget_limit(
            name="max_credits",
            task_limit=task.max_credits,
            task_source=task.max_credits_source,
            run_override=args.credit_limit,
            run_override_source="cli:--credit-limit",
            environment_variable="LLM4HLS_CREDIT_BUDGET",
            development_fallback=REFERENCE_DEVELOPMENT_MAX_CREDITS,
        )
        token_limit = resolve_agent_budget_limit(
            name="max_tokens",
            task_limit=task.max_tokens,
            task_source=task.max_tokens_source,
            run_override=token_override,
            run_override_source=token_override_source,
            environment_variable="LLM4HLS_TOKEN_BUDGET",
            development_fallback=REFERENCE_DEVELOPMENT_MAX_TOKENS,
        )
        resolved_costs = {
            tool: resolve_reference_tool_cost(
                tool=tool,
                run_override=override,
                environment_variable=f"LLM4HLS_COST_{tool.upper()}",
                reference_fallback=reference,
            )
            for tool, override, reference in (
                ("csim", args.cost_csim, 1),
                ("synth", args.cost_synth, 4),
                ("cosim", args.cost_cosim, 20),
            )
        }
        fallback_assumptions = tuple(
            item
            for item in (
                credit_limit.fallback_assumption,
                token_limit.fallback_assumption,
            )
            if item is not None
        )
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
            if (
                args.experience_mode != "off"
                and args.experience_ranker_version == "v3"
                and not args.experience_store
            ):
                raise ValueError(
                    "--experience-ranker-version v3 requires --experience-store"
                )
            if args.experience_mode == "guided":
                if args.experience_ranker_version != "v3":
                    raise ValueError(
                        "guided experience requires the Gate-controlled v3 ranker"
                    )
                if not args.experience_admission_manifest:
                    raise ValueError(
                        "guided experience requires --experience-admission-manifest"
                    )
            experience_coordinator = None
            if args.experience_mode != "off":
                try:
                    if args.experience_ranker_version == "v3":
                        from .v3_experience_v2_runtime import (
                            ExperienceV2RuntimeCoordinator,
                        )

                        experience_coordinator = ExperienceV2RuntimeCoordinator(
                            seed_store,
                            recommendation_path=(
                                run_root
                                / "experience"
                                / "experience_recommendations.jsonl"
                            ),
                            admission_manifest_path=(
                                args.experience_admission_manifest
                            ),
                            injection_requested=args.experience_mode == "guided",
                            max_guidance_tokens=(
                                args.experience_max_guidance_tokens
                            ),
                        )
                    else:
                        from .v3_experience_guidance import ExperienceCoordinator
                        from .v3_experience_store import JsonlExperienceRepository

                        experience_coordinator = ExperienceCoordinator(
                            JsonlExperienceRepository(seed_store),
                            recommendation_path=(
                                run_root
                                / "experience"
                                / "experience_recommendations.jsonl"
                            ),
                            max_guidance_tokens=(
                                args.experience_max_guidance_tokens
                            ),
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
            if args.token_budget_policy == "dynamic":
                token_policy = TokenBudgetPolicy(
                    TokenBudgetLimits(
                        mode_output_caps={
                            "REPAIR": args.repair_max_output_tokens,
                            "SYNTH_FIX": args.synth_fix_max_output_tokens,
                            "STRUCTURAL_FIX": args.structural_fix_max_output_tokens,
                            "OPTIMIZE": args.optimize_max_output_tokens,
                        },
                        configured_max_output_tokens=args.max_output_tokens,
                        minimum_viable_output_tokens=(
                            args.minimum_viable_output_tokens
                        ),
                        provider_hard_output_cap=args.llm_max_output_tokens,
                        context_window_tokens=args.context_window_tokens,
                        context_safety_margin_tokens=(
                            args.context_safety_margin_tokens
                        ),
                        token_budget_safety_margin=args.token_safety_margin,
                        future_round_token_reserve=(
                            args.future_round_token_reserve
                        ),
                        search_closeout_token_reserve=(
                            args.search_closeout_token_reserve
                        ),
                        configured_guidance_cap=(
                            args.experience_max_guidance_tokens
                        ),
                        guidance_ratio=args.guidance_ratio,
                    )
                )
                token_estimator = TokenEstimator.for_model(str(args.model))
            token_policy_kwargs = (
                {
                    "token_budget_policy": token_policy,
                    "token_estimator": token_estimator,
                    "token_budget_visible": args.token_budget_visibility == "visible",
                }
                if token_policy is not None
                else {}
            )
            if args.validation_profile == "fast-experiment":
                planner = OpenAICompatibleV3PlannerAdapter(
                    Path(args.run_dir).resolve(),
                    provider,
                    search_closeout_reserve_credits=args.search_closeout_reserve_credits,
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
                    search_closeout_reserve_credits=args.search_closeout_reserve_credits,
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
                cosim_no_progress_timeout_seconds=(
                    args.cosim_no_progress_timeout
                ),
            ),
            budget=BudgetConfig(
                credit_limit=credit_limit.value,
                costs={
                    "csim": resolved_costs["csim"].value,
                    "synth": resolved_costs["synth"].value,
                    "cosim": resolved_costs["cosim"].value,
                    "llm": 0,
                },
                tool_limits={
                    "csim": validation_call_limit,
                    "synth": validation_call_limit,
                    "cosim": validation_call_limit,
                    "llm": planned_rounds,
                },
                token_limit=token_limit.value,
                runtime_limit_seconds=args.runtime_limit,
                credit_limit_source=credit_limit.source,
                token_limit_source=token_limit.source,
                tool_cost_sources={
                    "csim": resolved_costs["csim"].source,
                    "synth": resolved_costs["synth"].source,
                    "cosim": resolved_costs["cosim"].source,
                    "llm": "command:planner-credit-cost-zero",
                },
                fallback_assumptions=fallback_assumptions,
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
                final_validation_policy=args.final_validation_policy,
                evidence_memory_mode=args.evidence_memory,
                continuation_policy_mode=args.continuation_policy,
                continuation_policy_version=args.continuation_policy_version,
                continuation_admission_manifest=(
                    args.continuation_admission_manifest
                ),
                run_deadline_monotonic=run_deadline_monotonic,
                cleanup_reserve_seconds=args.cleanup_reserve_seconds,
                cosim_minimum_runtime_seconds=(
                    args.cosim_minimum_runtime_seconds
                ),
                baseline_cosim_probe_timeout_seconds=(
                    args.baseline_cosim_probe_timeout
                ),
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
                final_validation_policy=args.final_validation_policy,
                evidence_memory_mode=args.evidence_memory,
                continuation_policy_mode=args.continuation_policy,
                continuation_policy_version=args.continuation_policy_version,
                continuation_admission_manifest=(
                    args.continuation_admission_manifest
                ),
                run_deadline_monotonic=run_deadline_monotonic,
                cleanup_reserve_seconds=args.cleanup_reserve_seconds,
                cosim_minimum_runtime_seconds=(
                    args.cosim_minimum_runtime_seconds
                ),
                baseline_cosim_probe_timeout_seconds=(
                    args.baseline_cosim_probe_timeout
                ),
            )
        if result.get("status") == "DONE":
            # The Agent graph is terminal and its Candidate/ledger are durable
            # before this separate domain starts.  Certification never calls
            # the Planner or resumes the graph.
            from .final_certification import (
                CertificationConfig,
                certify_v3_search_result,
            )

            result = certify_v3_search_result(
                task,
                args.run_dir,
                CertificationConfig(
                    tool=config.tool,
                    maximum_clock_period_ns=10.0,
                    runtime_deadline=RuntimeDeadline(
                        run_deadline_monotonic=run_deadline_monotonic,
                        cleanup_reserve_seconds=(
                            args.cleanup_reserve_seconds
                        ),
                        cosim_minimum_runtime_seconds=(
                            args.cosim_minimum_runtime_seconds
                        ),
                    ),
                ),
                backend=backend,
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
        "evidence_memory_mode": result.get(
            "evidence_memory_mode", args.evidence_memory
        ),
        "experience_mode": args.experience_mode,
        "experience_ingest": experience_ingest,
        "status": result.get("status"),
        "agent_search_status": result.get(
            "agent_search_status", result.get("status")
        ),
        "final_certification": result.get("final_certification"),
        "stop_reason": result.get("stop_reason"),
        "exploration_stop_reason": result.get("exploration_stop_reason"),
        "rounds_completed": result.get("rounds_completed"),
        "best_candidate_id": result.get("best_candidate_id"),
        "final_candidate_id": result.get("final_candidate_id"),
        "credits_used": result.get("budget", {}).get("credits_used"),
        "tokens_used": result.get("budget", {}).get("tokens_used"),
        "tool_calls": result.get("budget", {}).get("tool_used"),
        "run_dir": str(Path(args.run_dir).resolve()),
        "result_ref": (
            "v3_certified_result.json"
            if result.get("final_certification") is not None
            else "v3_prototype_result.json"
        ),
        "report_ref": "v3_team_report.md",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if done else 2


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
