"""Reproducible command-line entry point for the deterministic V0 harness."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .acceptance import AcceptanceError, evaluate_acceptance
from .artifacts import (
    ArtifactManifestError,
    build_artifact_manifest,
    manifest_digest,
)
from .budget import BudgetConfig, BudgetError
from .openai_provider import (
    DEFAULT_MODEL,
    OpenAICompatibleConfig,
    OpenAICompatibleOptimizationProvider,
    OpenAICompatibleRepairProvider,
)
from .optimization import OptimizationConfig, run_v2
from .repair import PatchLimits, PatchProposal, StaticPatchProvider, V1Error, run_v1
from .review import ReviewError, generate_review_reports
from .scoring import load_scoring_config
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

    repair = subparsers.add_parser(
        "repair", help="run one constrained V1 repair proposal and validate it"
    )
    repair.add_argument("task_dir", type=Path, help="reference-compatible public task package")
    repair.add_argument("--run-dir", type=Path, required=True, help="durable artifact directory")
    repair.add_argument(
        "--provider",
        choices=("api", "static"),
        default="api",
        help="OpenAI-compatible API (default) or deterministic patch file",
    )
    repair.add_argument("--patch-file", type=Path, help="unified diff required by --provider static")
    repair.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    repair.add_argument("--model", default=os.environ.get("LLM4HLS_MODEL", DEFAULT_MODEL))
    repair.add_argument(
        "--llm-timeout",
        type=float,
        default=_env_float("LLM4HLS_LLM_TIMEOUT_S", 120.0),
    )
    repair.add_argument(
        "--llm-max-output-tokens",
        type=int,
        default=_env_int("LLM4HLS_LLM_MAX_OUTPUT_TOKENS", 1000),
    )
    repair.add_argument(
        "--llm-temperature",
        type=float,
        default=_env_float("LLM4HLS_LLM_TEMPERATURE", 0.0),
    )
    repair.add_argument(
        "--vitis-root",
        default=os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "/opt/xilinx/2025.2/Vitis"),
    )
    repair.add_argument("--part", default=None)
    repair.add_argument("--clock-ns", type=float, default=None)
    repair.add_argument("--toolchain-id", default=os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"))
    repair.add_argument("--credit-limit", type=int, default=None)
    repair.add_argument("--cost-csim", type=int, default=_env_int("LLM4HLS_COST_CSIM", 1))
    repair.add_argument("--cost-synth", type=int, default=_env_int("LLM4HLS_COST_SYNTH", 4))
    repair.add_argument("--cost-cosim", type=int, default=_env_int("LLM4HLS_COST_COSIM", 20))
    repair.add_argument("--cost-llm", type=int, default=_env_int("LLM4HLS_COST_LLM", 0))
    repair.add_argument("--max-llm-calls", type=int, default=1)
    repair.add_argument("--csim-timeout", type=float, default=_env_float("LLM4HLS_CSIM_TIMEOUT_S", 180.0))
    repair.add_argument("--synth-timeout", type=float, default=_env_float("LLM4HLS_SYNTH_TIMEOUT_S", 600.0))
    repair.add_argument("--cosim-timeout", type=float, default=_env_float("LLM4HLS_COSIM_TIMEOUT_S", 900.0))
    repair.add_argument("--token-budget", type=int, default=_env_int("LLM4HLS_TOKEN_BUDGET", 32768))
    repair.add_argument("--runtime-limit", type=float, default=_env_float("LLM4HLS_RUNTIME_LIMIT_S", 3600.0))
    repair.add_argument("--minimum-frequency-mhz", type=float, default=_env_float("LLM4HLS_MIN_FREQUENCY_MHZ", 100.0))
    repair.add_argument("--max-changed-lines", type=int, default=80)
    repair.add_argument(
        "--allow-deterministic-fallback",
        action="store_true",
        help="debug-only: use the narrow V0 vector-add fallback after provider failure",
    )
    optimize = subparsers.add_parser(
        "optimize", help="run the V2 Candidate tree and PPA optimization loop"
    )
    optimize.add_argument("task_dir", type=Path)
    optimize.add_argument("--run-dir", type=Path, required=True)
    optimize.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    optimize.add_argument("--model", default=os.environ.get("LLM4HLS_MODEL", DEFAULT_MODEL))
    optimize.add_argument("--llm-timeout", type=float, default=_env_float("LLM4HLS_LLM_TIMEOUT_S", 120.0))
    optimize.add_argument("--llm-max-output-tokens", type=int, default=_env_int("LLM4HLS_LLM_MAX_OUTPUT_TOKENS", 1000))
    optimize.add_argument("--llm-temperature", type=float, default=_env_float("LLM4HLS_LLM_TEMPERATURE", 0.0))
    optimize.add_argument("--vitis-root", default=os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "/opt/xilinx/2025.2/Vitis"))
    optimize.add_argument("--part", default=None)
    optimize.add_argument("--clock-ns", type=float, default=None)
    optimize.add_argument("--toolchain-id", default=os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"))
    optimize.add_argument("--credit-limit", type=int, default=None)
    optimize.add_argument("--cost-csim", type=int, default=_env_int("LLM4HLS_COST_CSIM", 1))
    optimize.add_argument("--cost-synth", type=int, default=_env_int("LLM4HLS_COST_SYNTH", 4))
    optimize.add_argument("--cost-cosim", type=int, default=_env_int("LLM4HLS_COST_COSIM", 20))
    optimize.add_argument("--cost-llm", type=int, default=_env_int("LLM4HLS_COST_LLM", 0))
    optimize.add_argument("--max-csim-calls", type=int, default=6)
    optimize.add_argument("--max-synth-calls", type=int, default=6)
    optimize.add_argument("--max-cosim-calls", type=int, default=6)
    optimize.add_argument("--max-llm-calls", type=int, default=6)
    optimize.add_argument("--csim-timeout", type=float, default=_env_float("LLM4HLS_CSIM_TIMEOUT_S", 180.0))
    optimize.add_argument("--synth-timeout", type=float, default=_env_float("LLM4HLS_SYNTH_TIMEOUT_S", 900.0))
    optimize.add_argument("--cosim-timeout", type=float, default=_env_float("LLM4HLS_COSIM_TIMEOUT_S", 900.0))
    optimize.add_argument("--token-budget", type=int, default=_env_int("LLM4HLS_TOKEN_BUDGET", 32768))
    optimize.add_argument("--runtime-limit", type=float, default=_env_float("LLM4HLS_RUNTIME_LIMIT_S", 7200.0))
    optimize.add_argument("--minimum-frequency-mhz", type=float, default=_env_float("LLM4HLS_MIN_FREQUENCY_MHZ", 100.0))
    optimize.add_argument(
        "--scoring-config",
        type=Path,
        default=Path(__file__).resolve().parent / "config" / "v2_scoring.yaml",
    )
    optimize.add_argument("--max-optimization-rounds", type=int, default=4)
    optimize.add_argument("--max-no-improvement-rounds", type=int, default=2)
    optimize.add_argument("--final-reserve-credits", type=int, default=25)
    optimize.add_argument("--max-changed-lines", type=int, default=30)
    manifest = subparsers.add_parser(
        "manifest", help="build or rebuild a deterministic artifact manifest"
    )
    manifest.add_argument("--run-dir", type=Path, required=True)
    accept = subparsers.add_parser(
        "accept-v1", help="evaluate the complete V1 evidence matrix"
    )
    accept.add_argument(
        "--spec",
        type=Path,
        default=Path(__file__).resolve().parent / "config" / "v1_acceptance.json",
    )
    accept.add_argument("--functional-run", type=Path, required=True)
    accept.add_argument("--compile-run", type=Path, required=True)
    accept.add_argument("--synthesis-run", type=Path, required=True)
    accept.add_argument("--patch-invalid-run", type=Path, required=True)
    accept.add_argument("--output-dir", type=Path, required=True)
    review = subparsers.add_parser(
        "review-v1",
        help="offline: aggregate existing V1 evidence into flat human-review reports",
    )
    review.add_argument("--runs-root", type=Path, default=Path("runs"))
    review.add_argument(
        "--spec",
        type=Path,
        default=Path(__file__).resolve().parent / "config" / "v1_acceptance.json",
    )
    review.add_argument("--functional-run", type=Path)
    review.add_argument("--compile-run", type=Path)
    review.add_argument("--synthesis-run", type=Path)
    review.add_argument("--patch-invalid-run", type=Path)
    review.add_argument("--acceptance-result", type=Path)
    return parser


def _print_error(exc: Exception) -> None:
    print(
        json.dumps(
            {"status": "ERROR", "error_type": type(exc).__name__, "detail": str(exc)},
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _write_experimental_report(run_dir: Path, result: dict[str, object]) -> None:
    validation = result.get("validation", {})
    budget = result.get("budget", {}) if isinstance(result.get("budget", {}), dict) else {}
    candidate = result.get("candidate", {}) if isinstance(result.get("candidate", {}), dict) else {}
    patch = result.get("patch", {}) if isinstance(result.get("patch", {}), dict) else {}
    diagnostic = result.get("diagnostic", {}) if isinstance(result.get("diagnostic", {}), dict) else {}
    candidate_provider = str(candidate.get("provider", patch.get("provider", "N/A")))
    llm_candidate_used = candidate_provider == "openai-compatible"
    acceptance = result.get("v1_acceptance", {}) if isinstance(result.get("v1_acceptance", {}), dict) else {}
    llm_input_tokens = 0
    llm_output_tokens = 0
    llm_cached_input_tokens = 0
    llm_usage_complete = True
    for action_result in (run_dir / "llm_actions").glob("*/result.json"):
        try:
            llm_data = json.loads(action_result.read_text())
            if "input_tokens" not in llm_data or "output_tokens" not in llm_data:
                llm_usage_complete = False
            llm_input_tokens += int(llm_data.get("input_tokens", 0))
            llm_output_tokens += int(llm_data.get("output_tokens", 0))
            llm_cached_input_tokens += int(llm_data.get("cached_input_tokens", 0))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
    calls = budget.get("tool_used", {}) if isinstance(budget.get("tool_used", {}), dict) else {}
    costs = budget.get("tool_costs", {}) if isinstance(budget.get("tool_costs", {}), dict) else {}
    lines = ["# V1 Experimental Report", "", f"- Status: `{result.get('status')}`", f"- Stop reason: `{result.get('stop_reason')}`", f"- Error class: `{diagnostic.get('code', 'N/A')}`", f"- Baseline failure stage / phase: `{diagnostic.get('stage', 'N/A')} / {diagnostic.get('phase', 'N/A')}`", f"- Candidate ID: `{result.get('candidate_id')}`", f"- Candidate materialized: `{'yes' if result.get('candidate_id') else 'no'}`", f"- Candidate provider: `{candidate_provider}`", f"- LLM-generated candidate used: `{'yes' if llm_candidate_used else 'no'}`", f"- LLM-based V1 accepted: `{'yes' if acceptance.get('accepted') is True else 'no'}`", "", "## Budget accounting", "", f"- Credits used / limit / remaining: `{budget.get('credits_used', 'N/A')} / {budget.get('credit_limit', 'N/A')} / {budget.get('credits_remaining', 'N/A')}`", f"- Tokens used / limit / remaining: `{budget.get('tokens_used', 'N/A')} / {budget.get('token_limit', 'N/A')} / {budget.get('tokens_remaining', 'N/A')}`", f"- Provider input / output / cached-input tokens: `{llm_input_tokens} / {llm_output_tokens} / {llm_cached_input_tokens}`", f"- Token accounting complete: `{'yes' if llm_usage_complete else 'no (legacy failed provider result omitted usage)'}`", "", "| Tool | Calls | Unit cost | Credits |", "|---|---:|---:|---:|"]
    total_calls = 0
    total_credits = 0
    for tool_name in ("csim", "synth", "cosim", "llm"):
        count = int(calls.get(tool_name, 0))
        unit = int(costs.get(tool_name, 0))
        total_calls += count
        total_credits += count * unit
        lines.append(f"| {tool_name} | {count} | {unit} | {count * unit} |")
    lines += [f"| **合计** | **{total_calls}** | — | **{total_credits}** |", "", "## Vitis validation", "", "| Stage | Status | Phase | Evidence |", "|---|---|---|---|"]
    synth_report: dict[str, object] = {}
    for stage in ("csim", "synth", "cosim"):
        item = validation.get(stage, {}) if isinstance(validation, dict) else {}
        ref = item.get("result_ref", "") if isinstance(item, dict) else ""
        evidence = ""
        if ref and (run_dir / ref).is_file():
            try:
                raw = json.loads((run_dir / ref).read_text())
                evidence = "; ".join(raw.get("evidence", [])[:1])
                if stage == "synth": synth_report = raw.get("report", {})
            except (OSError, UnicodeError, json.JSONDecodeError):
                evidence = "unreadable result"
        status = item.get("status", "NOT_RUN") if isinstance(item, dict) else "NOT_RUN"
        phase = item.get("phase", "") if isinstance(item, dict) else ""
        lines.append(f"| {stage} | `{status}` | `{phase}` | {evidence} |")
    lines += ["", "## Synthesis metrics", "", f"- Estimated clock period (ns): `{synth_report.get('estimated_clock_period_ns', 'N/A')}`", f"- Latency: `{synth_report.get('latency', 'N/A')}`", f"- II: `{synth_report.get('interval', 'N/A')}`", f"- Resources: `{synth_report.get('resources', 'N/A')}`", "", "## Artifact integrity", "", "- Manifest: `artifact_manifest.json`", "- Final manifest SHA-256 is recorded by `acceptance_result.json`.", "", "Raw evidence: `v1_result.json`, `artifact_manifest.json` and `actions/*/result.json`."]
    (run_dir / "experimental_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    argv: Sequence[str] | None = None, *, backend: ToolBackend | None = None
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except ValueError as exc:
        _print_error(exc)
        return 3
    if args.command == "repair":
        return _main_repair(args, backend=backend)
    if args.command == "optimize":
        return _main_optimize(args, backend=backend)
    if args.command == "manifest":
        try:
            manifest = build_artifact_manifest(args.run_dir)
            summary = {
                "status": "DONE",
                "run_dir": str(Path(args.run_dir).resolve()),
                "artifact_count": len(manifest["artifacts"]),
                "manifest_ref": "artifact_manifest.json",
                "manifest_sha256": manifest_digest(args.run_dir),
            }
        except ArtifactManifestError as exc:
            _print_error(exc)
            return 3
        print(json.dumps(summary, sort_keys=True))
        return 0
    if args.command == "accept-v1":
        try:
            result = evaluate_acceptance(
                args.spec,
                {
                    "functional_mismatch": args.functional_run,
                    "compile_error": args.compile_run,
                    "synthesis_error": args.synthesis_run,
                    "patch_invalid": args.patch_invalid_run,
                },
                args.output_dir,
            )
        except AcceptanceError as exc:
            _print_error(exc)
            return 3
        print(
            json.dumps(
                {
                    "status": result["overall_status"],
                    "result_ref": "acceptance_result.json",
                    "report_ref": "acceptance_report.md",
                    "report_cn_ref": "acceptance_report_CN.md",
                    "output_dir": str(Path(args.output_dir).resolve()),
                },
                sort_keys=True,
            )
        )
        return 0 if result["overall_status"] == "PASS" else 2
    if args.command == "review-v1":
        runs_root = Path(args.runs_root)
        try:
            summary = generate_review_reports(
                args.spec,
                {
                    "functional_mismatch": args.functional_run
                    or runs_root / "v1-functional-final-2",
                    "compile_error": args.compile_run or runs_root / "v1-compile-final",
                    "synthesis_error": args.synthesis_run
                    or runs_root / "v1-synthesis-final-2",
                    "patch_invalid": args.patch_invalid_run
                    or runs_root / "v1-patch-invalid",
                },
                args.acceptance_result
                or runs_root / "v1-acceptance" / "acceptance_result.json",
                runs_root,
            )
        except ReviewError as exc:
            _print_error(exc)
            return 3
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary["status"] == "PASS" else 2
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
        manifest = build_artifact_manifest(args.run_dir)
    except (
        TaskPackageError,
        BudgetError,
        RunArtifactError,
        ArtifactManifestError,
        ValueError,
    ) as exc:
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
        "manifest_ref": "artifact_manifest.json",
        "artifact_count": len(manifest["artifacts"]),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result["status"] == "DONE" else 2


def _main_repair(args: argparse.Namespace, *, backend: ToolBackend | None) -> int:
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
                "llm": args.cost_llm,
            },
            tool_limits={
                "csim": None,
                "synth": None,
                "cosim": None,
                "llm": args.max_llm_calls,
            },
            token_limit=args.token_budget,
            runtime_limit_seconds=args.runtime_limit,
        )
        if args.provider == "static":
            if args.patch_file is None:
                raise ValueError("--patch-file is required with --provider static")
            patch = args.patch_file.read_text(encoding="utf-8")
            provider = StaticPatchProvider(
                PatchProposal(
                    patch=patch,
                    provider="static-patch-file",
                    model="offline",
                    input_tokens=0,
                    output_tokens=0,
                )
            )
        else:
            if args.patch_file is not None:
                raise ValueError("--patch-file is only valid with --provider static")
            if not args.base_url:
                raise ValueError("OPENAI_BASE_URL or --base-url is required")
            provider = OpenAICompatibleRepairProvider(
                OpenAICompatibleConfig(
                    base_url=str(args.base_url),
                    api_key=os.environ.get("OPENAI_API_KEY", ""),
                    model=str(args.model),
                    timeout_seconds=args.llm_timeout,
                    max_output_tokens=args.llm_max_output_tokens,
                    temperature=args.llm_temperature,
                )
            )
        result = run_v1(
            task,
            args.run_dir,
            RunConfig(
                tool=tool,
                budget=budget,
                minimum_frequency_mhz=args.minimum_frequency_mhz,
            ),
            provider,
            backend=backend,
            patch_limits=PatchLimits(max_changed_lines=args.max_changed_lines),
            allow_deterministic_fallback=args.allow_deterministic_fallback,
        )
    except (OSError, UnicodeError, TaskPackageError, BudgetError, RunArtifactError, V1Error, ValueError) as exc:
        _print_error(exc)
        return 3
    try:
        _write_experimental_report(Path(args.run_dir).resolve(), result)
        manifest = build_artifact_manifest(args.run_dir)
    except (OSError, UnicodeError, ArtifactManifestError) as exc:
        _print_error(exc)
        return 3
    summary = {
        "task_id": result["baseline"]["task_id"],  # type: ignore[index]
        "run_dir": str(Path(args.run_dir).resolve()),
        "status": result["status"],
        "stop_reason": result["stop_reason"],
        "candidate_id": result.get("candidate_id"),
        "rollback": result.get("rollback"),
        "credits_used": result["budget"]["credits_used"],  # type: ignore[index]
        "tokens_used": result["budget"]["tokens_used"],  # type: ignore[index]
        "result_ref": "v1_result.json",
        "manifest_ref": "artifact_manifest.json",
        "artifact_count": len(manifest["artifacts"]),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result["status"] == "DONE" else 2


def _main_optimize(args: argparse.Namespace, *, backend: ToolBackend | None) -> int:
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
                "llm": args.cost_llm,
            },
            tool_limits={
                "csim": args.max_csim_calls,
                "synth": args.max_synth_calls,
                "cosim": args.max_cosim_calls,
                "llm": args.max_llm_calls,
            },
            token_limit=args.token_budget,
            runtime_limit_seconds=args.runtime_limit,
        )
        if not args.base_url:
            raise ValueError("OPENAI_BASE_URL or --base-url is required")
        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url=str(args.base_url),
                api_key=os.environ.get("OPENAI_API_KEY", ""),
                model=str(args.model),
                timeout_seconds=args.llm_timeout,
                max_output_tokens=args.llm_max_output_tokens,
                temperature=args.llm_temperature,
            )
        )
        result = run_v2(
            task,
            args.run_dir,
            RunConfig(
                tool=tool,
                budget=budget,
                minimum_frequency_mhz=args.minimum_frequency_mhz,
            ),
            OptimizationConfig(
                scoring=load_scoring_config(args.scoring_config),
                max_rounds=args.max_optimization_rounds,
                max_no_improvement_rounds=args.max_no_improvement_rounds,
                max_llm_calls=args.max_llm_calls,
                final_reserve_credits=args.final_reserve_credits,
                patch_limits=PatchLimits(max_changed_lines=args.max_changed_lines),
            ),
            provider,
            backend=backend,
        )
    except (
        OSError,
        UnicodeError,
        TaskPackageError,
        BudgetError,
        RunArtifactError,
        V1Error,
        ValueError,
    ) as exc:
        _print_error(exc)
        return 3
    summary = {
        "task_id": result.get("task_id", task.id),
        "run_dir": str(Path(args.run_dir).resolve()),
        "status": result["status"],
        "stop_reason": result["stop_reason"],
        "best_candidate_id": result.get("best_candidate_id"),
        "final_candidate_id": result.get("final_candidate_id"),
        "rounds_completed": len(result.get("rounds", [])),
        "credits_used": result.get("budget", {}).get("credits_used", 0),
        "tokens_used": result.get("budget", {}).get("tokens_used", 0),
        "result_ref": "v2_result.json",
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result["status"] == "DONE" else 2


def main_entry() -> None:
    raise SystemExit(main())
