"""Deterministic V2 optimization selection and workflow boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Mapping, Protocol

from .artifacts import build_artifact_manifest, verify_artifact_manifest
from .budget import BudgetExceeded, BudgetLedger
from .candidate import CandidateManager
from .repair import (
    PatchLimits,
    PatchProposal,
    PatchValidationError,
    RepairProviderError,
    apply_unified_diff,
    normalize_unified_diff_headers,
)
from .scoring import CandidateScore, ScoringConfig, compare_scores, score_candidate
from .task import PublicTask
from .tools import ToolBackend
from .validation import CandidateValidation, validate_candidate, validate_csim_only
from .workflow import RunConfig, _RunLock, _append_trace, _atomic_json, run_v0


ALLOWED_OPTIMIZATIONS = (
    "LOOP_PIPELINE",
    "LOOP_UNROLL",
    "MEMORY_LAYOUT",
    "LOOP_RESTRUCTURE",
)


@dataclass(frozen=True)
class OptimizationDecision:
    optimization_class: str | None
    bottleneck: str
    evidence: tuple[str, ...]
    metrics_digest: str
    stop_reason: str | None = None


@dataclass(frozen=True)
class OptimizationContext:
    task_id: str
    parent_candidate_id: str
    round_index: int
    allowed_optimization_class: str
    bottleneck: str
    evidence: tuple[str, ...]
    baseline_metrics: Mapping[str, object]
    current_metrics: Mapping[str, object]
    source_excerpt: str
    failed_actions: tuple[Mapping[str, object], ...]
    remaining_tokens: int
    remaining_credits: int | None
    final_reserve_credits: int
    top: str
    kernel_name: str
    part: str
    clock_ns: float

    def __post_init__(self) -> None:
        if self.allowed_optimization_class not in ALLOWED_OPTIMIZATIONS:
            raise ValueError("optimization context has an unsupported class")
        if self.round_index <= 0:
            raise ValueError("optimization round index must be positive")
        if self.remaining_tokens < 0 or self.final_reserve_credits < 0:
            raise ValueError("optimization budget values must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "parent_candidate_id": self.parent_candidate_id,
            "round_index": self.round_index,
            "allowed_optimization_class": self.allowed_optimization_class,
            "bottleneck": self.bottleneck,
            "evidence": list(self.evidence),
            "baseline_metrics": dict(self.baseline_metrics),
            "current_metrics": dict(self.current_metrics),
            "source_excerpt": self.source_excerpt,
            "failed_actions": [dict(item) for item in self.failed_actions],
            "remaining_tokens": self.remaining_tokens,
            "remaining_credits": self.remaining_credits,
            "final_reserve_credits": self.final_reserve_credits,
            "top": self.top,
            "kernel_name": self.kernel_name,
            "part": self.part,
            "clock_ns": self.clock_ns,
        }


class OptimizationProvider(Protocol):
    def propose_optimization(self, context: OptimizationContext) -> PatchProposal: ...


@dataclass(frozen=True)
class OptimizationConfig:
    scoring: ScoringConfig
    max_rounds: int = 4
    max_no_improvement_rounds: int = 2
    max_llm_calls: int = 6
    final_reserve_credits: int = 25
    patch_limits: PatchLimits = field(
        default_factory=lambda: PatchLimits(max_changed_lines=30, max_hunks=4)
    )

    def __post_init__(self) -> None:
        if self.max_rounds <= 0 or self.max_no_improvement_rounds <= 0:
            raise ValueError("optimization round limits must be positive")
        if self.max_llm_calls < self.max_rounds:
            raise ValueError("LLM call limit cannot be lower than the round limit")
        if self.final_reserve_credits < 0:
            raise ValueError("final reserve credits must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "scoring": self.scoring.to_dict(),
            "max_rounds": self.max_rounds,
            "max_no_improvement_rounds": self.max_no_improvement_rounds,
            "max_llm_calls": self.max_llm_calls,
            "final_reserve_credits": self.final_reserve_credits,
            "patch_limits": asdict(self.patch_limits),
        }


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_v2_experimental_report(
    run_root: Path,
    result: Mapping[str, object],
    registry: Mapping[str, object],
) -> None:
    budget = result.get("budget")
    budget_value = budget if isinstance(budget, Mapping) else {}
    calls = budget_value.get("tool_used")
    call_value = calls if isinstance(calls, Mapping) else {}
    workflow = str(result.get("workflow", ""))
    lines = [
        "# V2 Experimental Report",
        "",
        f"- Workflow: `{workflow}`",
        f"- Status: `{result.get('status')}`",
        f"- Stop reason: `{result.get('stop_reason')}`",
        f"- Baseline Candidate: `{result.get('baseline_candidate_id', 'candidate_000')}`",
        f"- Best Candidate: `{result.get('best_candidate_id')}`",
        f"- Final Candidate: `{result.get('final_candidate_id')}`",
        "",
        "## Budget and calls",
        "",
        f"- Credits used / remaining: `{budget_value.get('credits_used')} / {budget_value.get('credits_remaining')}`",
        f"- Input / output / cached-input Tokens: `{budget_value.get('input_tokens_used')} / {budget_value.get('output_tokens_used')} / {budget_value.get('cached_input_tokens_used')}`",
        f"- Total Tokens: `{budget_value.get('tokens_used')}`",
        "",
        "| Tool | Calls |",
        "|---|---:|",
    ]
    for tool in ("csim", "synth", "cosim", "llm"):
        lines.append(f"| {tool} | {call_value.get(tool, 0)} |")

    if workflow == "V2_CANDIDATE_PPA":
        lines.extend(
            [
                "",
                "## Candidate rounds",
                "",
                "| Round | Parent | Class | Candidate | Decision |",
                "|---:|---|---|---|---|",
            ]
        )
        rounds = result.get("rounds")
        for item in rounds if isinstance(rounds, list) else []:
            value = item if isinstance(item, Mapping) else {}
            lines.append(
                "| "
                + " | ".join(
                    str(value.get(key, ""))
                    for key in (
                        "round_index",
                        "parent_candidate_id",
                        "optimization_class",
                        "candidate_id",
                        "decision",
                    )
                )
                + " |"
            )
        final_validation = result.get("final_validation")
        final_value = final_validation if isinstance(final_validation, Mapping) else {}
        lines.extend(
            [
                "",
                "## Final Vitis validation",
                "",
                "| Stage | Status | Scope | Result |",
                "|---|---|---|---|",
            ]
        )
        for stage in ("csim", "synth", "cosim"):
            item = final_value.get(stage)
            value = item if isinstance(item, Mapping) else {}
            ref = str(value.get("result_ref", ""))
            link = f"[{ref}]({ref})" if ref else ""
            lines.append(
                f"| {stage} | `{value.get('status', 'NOT_RUN')}` | "
                f"`{value.get('validation_scope', '')}` | {link} |"
            )
        final_id = result.get("final_candidate_id")
        candidates = registry.get("candidates")
        candidate_map = candidates if isinstance(candidates, Mapping) else {}
        final_candidate = candidate_map.get(final_id)
        final_record = final_candidate if isinstance(final_candidate, Mapping) else {}
        metrics_ref = final_record.get("metrics_ref")
        metrics: Mapping[str, object] = {}
        if isinstance(metrics_ref, str):
            try:
                metrics = _read_report(run_root, metrics_ref)
            except ValueError:
                metrics = {}
        lines.extend(
            [
                "",
                "## Final PPA metrics",
                "",
                f"- Estimated clock period: `{metrics.get('estimated_clock_period_ns')}` ns",
                f"- Latency: `{metrics.get('latency')}`",
                f"- II: `{metrics.get('interval')}`",
                f"- Resources: `{metrics.get('resources')}`",
                f"- Fallback: `{result.get('fallback')}`",
            ]
        )
    else:
        invariants = result.get("safety_invariants")
        invariant_value = invariants if isinstance(invariants, Mapping) else {}
        lines.extend(
            [
                "",
                "## Safety invariants",
                "",
                "| Invariant | Passed |",
                "|---|---|",
            ]
        )
        for name in sorted(invariant_value):
            lines.append(f"| `{name}` | `{invariant_value[name]}` |")
        validation = result.get("validation")
        validation_value = validation if isinstance(validation, Mapping) else {}
        lines.extend(["", "## Rejected Candidate validation", ""])
        for stage in ("csim", "synth", "cosim"):
            item = validation_value.get(stage)
            value = item if isinstance(item, Mapping) else {}
            lines.append(f"- {stage}: `{value.get('status', 'NOT_RUN')}`")

    lines.extend(
        [
            "",
            "## Raw evidence",
            "",
            "- `candidate_registry.json`",
            "- `budget_ledger.jsonl`",
            "- `trace.jsonl`",
            "- `artifact_manifest.json`",
            "",
        ]
    )
    _atomic_text(run_root / "experimental_report.md", "\n".join(lines))


def _finalize_v2_artifacts(
    run_root: Path,
    result: Mapping[str, object],
    registry: Mapping[str, object],
) -> None:
    _write_v2_experimental_report(run_root, result, registry)
    build_artifact_manifest(run_root)


def _positive_metric(metrics: Mapping[str, object], group: str, name: str) -> float:
    values = metrics.get(group)
    value = values.get(name) if isinstance(values, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"optimization metric {group}.{name} is missing")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"optimization metric {group}.{name} is invalid")
    return parsed


def select_optimization(
    metrics: Mapping[str, object],
    *,
    source: str,
    attempted: tuple[str, ...],
    failures: tuple[tuple[str, str], ...],
) -> OptimizationDecision:
    """Select one untried class from structured evidence without side effects."""

    if any(item not in ALLOWED_OPTIMIZATIONS for item in attempted):
        raise ValueError("attempted optimization contains an unsupported class")
    digest = _canonical_digest(metrics)
    failed_same = {
        optimization_class
        for optimization_class, metrics_digest in failures
        if metrics_digest == digest
    }
    available = [
        item
        for item in ALLOWED_OPTIMIZATIONS
        if item not in attempted and item not in failed_same
    ]
    if not available:
        return OptimizationDecision(
            optimization_class=None,
            bottleneck="no distinct optimization remains",
            evidence=(),
            metrics_digest=digest,
            stop_reason="NO_DISTINCT_OPTIMIZATION",
        )

    interval = _positive_metric(metrics, "interval", "max")
    latency = _positive_metric(metrics, "latency", "worst")
    raw_evidence = metrics.get("evidence", [])
    evidence = tuple(
        str(item) for item in raw_evidence
    ) if isinstance(raw_evidence, list) else ()
    joined = " ".join(evidence).lower()

    if (
        "MEMORY_LAYOUT" in available
        and any(token in joined for token in ("memory port", "limited memory", "load operation"))
    ):
        selected = "MEMORY_LAYOUT"
        bottleneck = "memory port or array access bottleneck"
    elif interval > 1 and "LOOP_PIPELINE" in available:
        selected = "LOOP_PIPELINE"
        bottleneck = f"maximum interval is {interval:g}"
    elif "LOOP_UNROLL" in available and latency > max(4.0, 4.0 * interval):
        selected = "LOOP_UNROLL"
        bottleneck = f"worst latency is {latency:g} with interval {interval:g}"
    elif "MEMORY_LAYOUT" in available:
        selected = "MEMORY_LAYOUT"
        bottleneck = "pipeline and unroll opportunities were exhausted"
    else:
        selected = available[0]
        bottleneck = "remaining loop structure bottleneck"

    return OptimizationDecision(
        optimization_class=selected,
        bottleneck=bottleneck,
        evidence=evidence,
        metrics_digest=digest,
    )


def _provider_fingerprint(provider: OptimizationProvider) -> str:
    explicit = getattr(provider, "fingerprint", None)
    if callable(explicit):
        value = str(explicit())
    else:
        cls = type(provider)
        value = f"{cls.__module__}.{cls.__qualname__}"
    if not value:
        raise ValueError("optimization provider fingerprint must not be empty")
    return value


def _proposal_from_dict(value: Mapping[str, object]) -> PatchProposal:
    if value.get("ok") is not True:
        raise RepairProviderError(str(value.get("error", "provider action failed")))
    validations = value.get("required_validation", [])
    if not isinstance(validations, list):
        raise RepairProviderError("cached optimization validation list is invalid")
    return PatchProposal(
        patch=str(value["patch"]),
        provider=str(value["provider"]),
        model=str(value["model"]),
        revision=str(value["revision"]) if value.get("revision") is not None else None,
        input_tokens=int(value.get("input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        cached_input_tokens=int(value.get("cached_input_tokens", 0)),
        request_id=str(value["request_id"]) if value.get("request_id") else None,
        duration_seconds=float(value.get("duration_seconds", 0.0)),
        hypothesis=str(value["hypothesis"]) if value.get("hypothesis") else None,
        change_class=str(value["change_class"]) if value.get("change_class") else None,
        expected_effect=str(value["expected_effect"]) if value.get("expected_effect") else None,
        risk=str(value["risk"]) if value.get("risk") else None,
        required_validation=tuple(str(item) for item in validations),
    )


def _call_optimization_provider(
    provider: OptimizationProvider,
    context: OptimizationContext,
    *,
    run_root: Path,
    budget: BudgetLedger,
    parent_id: str,
    code_hash: str,
    tool_config_hash: str,
) -> tuple[PatchProposal | None, str]:
    stable_context = context.to_dict()
    stable_context.pop("remaining_tokens", None)
    stable_context.pop("remaining_credits", None)
    payload = {
        "kind": "llm",
        "purpose": "ppa_optimization",
        "candidate_id": parent_id,
        "code_hash": code_hash,
        "tool_config_hash": tool_config_hash,
        "provider": _provider_fingerprint(provider),
        "context": stable_context,
    }
    action_id = _canonical_digest(payload)
    result_ref = f"llm_actions/{action_id}/result.json"
    result_path = run_root / result_ref
    completed = budget.completed_event(action_id)
    if completed is not None:
        encoded = result_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != completed.get("result_sha256"):
            raise RepairProviderError("cached optimization result digest mismatch")
        value = json.loads(encoded.decode("utf-8"))
        if not isinstance(value, dict):
            raise RepairProviderError("cached optimization result is not an object")
        return _proposal_from_dict(value), result_ref
    if budget.has_pending(action_id) or budget.is_ambiguous(action_id):
        raise RepairProviderError(f"optimization action {action_id} is not recoverable")
    budget.reserve(
        action_id=action_id,
        kind="llm",
        candidate_id=parent_id,
        code_hash=code_hash,
        tool_config_hash=tool_config_hash,
    )
    proposal: PatchProposal | None = None
    try:
        proposal = provider.propose_optimization(context)
        if proposal.change_class != context.allowed_optimization_class:
            raise RepairProviderError("optimization proposal class does not match Selector")
        value: dict[str, object] = {"ok": True, **proposal.to_dict()}
        input_tokens = proposal.input_tokens
        output_tokens = proposal.output_tokens
        cached_tokens = proposal.cached_input_tokens
        duration = proposal.duration_seconds
    except Exception as exc:
        input_tokens = int(getattr(exc, "input_tokens", 0))
        output_tokens = int(getattr(exc, "output_tokens", 0))
        cached_tokens = int(getattr(exc, "cached_input_tokens", 0))
        duration = float(getattr(exc, "duration_seconds", 0.0))
        value = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_tokens,
            "duration_seconds": duration,
        }
    _atomic_json(result_path, value)
    encoded = result_path.read_bytes()
    budget.complete(
        action_id=action_id,
        result_ref=result_ref,
        result_sha256=hashlib.sha256(encoded).hexdigest(),
        elapsed_s=duration,
        tokens_used=input_tokens + output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
    )
    _append_trace(
        run_root / "trace.jsonl",
        "V2_LLM_COMPLETED",
        action_id=action_id,
        candidate_id=parent_id,
        round_index=context.round_index,
        optimization_class=context.allowed_optimization_class,
        ok=proposal is not None,
        result_ref=result_ref,
        tokens_used=input_tokens + output_tokens,
    )
    if proposal is None:
        return None, str(value["error"])
    return proposal, result_ref


def _read_report(run_root: Path, result_ref: str) -> dict[str, object]:
    try:
        value = json.loads((run_root / result_ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Vitis result {result_ref}: {exc}") from exc
    report = value.get("report") if isinstance(value, dict) else None
    if not isinstance(report, dict):
        raise ValueError(f"Vitis result {result_ref} has no synthesis report")
    return report


def _read_score(run_root: Path, score_ref: str) -> CandidateScore:
    try:
        value = json.loads((run_root / score_ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Candidate score {score_ref}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Candidate score {score_ref} is not an object")
    return CandidateScore.from_dict(value)


def _candidate_source(
    run_root: Path, registry: Mapping[str, object], candidate_id: str
) -> bytes:
    candidates = registry.get("candidates")
    candidate = candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    if not isinstance(candidate, Mapping):
        raise ValueError(f"Candidate is missing: {candidate_id}")
    path = run_root / str(candidate.get("source_ref", ""))
    return path.read_bytes()


def _ensure_round_affordable(
    budget: BudgetLedger, config: OptimizationConfig
) -> None:
    snapshot = budget.snapshot()
    stage_cost = sum(budget.cost(stage) for stage in ("csim", "synth", "cosim"))
    remaining = snapshot["credits_remaining"]
    required = stage_cost + config.final_reserve_credits
    if remaining is not None and int(remaining) < required:
        raise BudgetExceeded(
            f"optimization closure requires {required} credits but {remaining} remain"
        )
    for stage in ("csim", "synth", "cosim"):
        limit = budget.config.tool_limits[stage]
        used = int(snapshot["tool_used"][stage]) + int(snapshot["tool_pending"][stage])
        if limit is not None and used + 2 > limit:
            raise BudgetExceeded(
                f"optimization and final validation require two {stage} calls"
            )
    llm_limit = budget.config.tool_limits["llm"]
    llm_used = int(snapshot["tool_used"]["llm"]) + int(snapshot["tool_pending"]["llm"])
    if llm_limit is not None and llm_used >= min(llm_limit, config.max_llm_calls):
        raise BudgetExceeded("optimization LLM call limit is exhausted")
    if int(snapshot["tokens_remaining"]) <= 0:
        raise BudgetExceeded("optimization token budget is exhausted")


def _ensure_final_affordable(budget: BudgetLedger) -> None:
    snapshot = budget.snapshot()
    required = sum(budget.cost(stage) for stage in ("csim", "synth", "cosim"))
    remaining = snapshot["credits_remaining"]
    if remaining is not None and int(remaining) < required:
        raise BudgetExceeded(
            f"final validation requires {required} credits but {remaining} remain"
        )
    for stage in ("csim", "synth", "cosim"):
        limit = budget.config.tool_limits[stage]
        used = int(snapshot["tool_used"][stage]) + int(snapshot["tool_pending"][stage])
        if limit is not None and used >= limit:
            raise BudgetExceeded(f"final validation requires another {stage} call")


def _rank_scores(values: list[CandidateScore]) -> list[CandidateScore]:
    def compare(left: CandidateScore, right: CandidateScore) -> int:
        winner = compare_scores(left, right).winner
        return -1 if winner == left.candidate_id else 1

    return sorted(values, key=cmp_to_key(compare))


def _score_from_validation(
    *,
    candidate_id: str,
    baseline_metrics: Mapping[str, object],
    candidate_metrics: Mapping[str, object],
    validation: CandidateValidation,
    config: ScoringConfig,
    proposal: PatchProposal | None,
    credits_used: int,
) -> CandidateScore:
    return score_candidate(
        candidate_id=candidate_id,
        baseline=baseline_metrics,
        candidate=candidate_metrics,
        validation=validation.validation,
        clock=validation.clock_constraint,
        config=config,
        input_tokens=proposal.input_tokens if proposal is not None else 0,
        output_tokens=proposal.output_tokens if proposal is not None else 0,
        cached_input_tokens=proposal.cached_input_tokens if proposal is not None else 0,
        credits_used=credits_used,
    )


def run_v2_rejection(
    task: PublicTask,
    run_dir: str | Path,
    run_config: RunConfig,
    patch_file: str | Path,
    *,
    backend: ToolBackend | None = None,
    patch_limits: PatchLimits = PatchLimits(max_changed_lines=30, max_hunks=4),
) -> dict[str, object]:
    """Prove that a policy-valid semantic regression is safely rejected."""

    if task.task_type != "optimize":
        raise ValueError("V2 rejection requires task_type=optimize")
    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        patch_text = Path(patch_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read V2 rejection Patch: {exc}") from exc
    normalized = normalize_unified_diff_headers(patch_text)
    patch_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    completed_path = run_root / "v2_rejection_result.json"
    if completed_path.is_file():
        try:
            completed = json.loads(completed_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"completed V2 rejection run is unreadable: {exc}") from exc
        current_budget = BudgetLedger(
            run_root / "budget_ledger.jsonl", run_config.budget
        ).snapshot()
        recorded_budget = completed.get("budget") if isinstance(completed, dict) else None
        if (
            not isinstance(completed, dict)
            or completed.get("workflow") != "V2_SAFETY_REJECTION"
            or completed.get("status") != "DONE"
            or completed.get("task_id") != task.id
            or completed.get("patch_sha256") != patch_sha256
            or not isinstance(recorded_budget, dict)
            or any(
                recorded_budget.get(key) != current_budget.get(key)
                for key in ("credits_used", "tokens_used", "tool_used")
            )
        ):
            raise ValueError("completed V2 rejection run does not match this invocation")
        verify_artifact_manifest(run_root)
        return completed

    baseline = run_v0(task, run_root, run_config, backend=backend)
    if baseline.get("status") != "DONE":
        result = {
            "schema_version": 1,
            "workflow": "V2_SAFETY_REJECTION",
            "task_id": task.id,
            "status": "FAILED",
            "stop_reason": "BASELINE_NOT_VERIFIED",
            "baseline": baseline,
            "patch_sha256": patch_sha256,
        }
        _atomic_json(completed_path, result)
        return result

    application = apply_unified_diff(
        task.kernel_bytes,
        normalized,
        kernel_name=task.kernel_name,
        limits=patch_limits,
    )
    manager = CandidateManager(run_root, task)
    registry = manager.load_registry()
    materialized = manager.materialize(
        registry,
        parent_id="candidate_000",
        patch_text=normalized,
        application=application,
        kind="safety_regression",
        metadata={
            "provider": "deterministic-fixture",
            "model": None,
            "fallback": "NOT_APPLICABLE",
            "optimization_class": "SAFETY_REGRESSION",
        },
    )
    candidate_id = materialized.candidate_id
    before_credits = int(BudgetLedger(
        run_root / "budget_ledger.jsonl", run_config.budget
    ).snapshot()["credits_used"])
    validation = validate_csim_only(
        task,
        application.patched_bytes,
        candidate_id,
        run_root,
        run_config,
        backend=backend,
    )
    registry = manager.load_registry()
    candidate = registry["candidates"][candidate_id]
    candidate["validation"] = validation.validation
    candidate["credits_used"] = int(validation.budget["credits_used"]) - before_credits
    candidate["status"] = (
        "REJECTED_VALIDATION" if validation.status == "DONE" else "UNSAFE_ACCEPTED"
    )
    registry["active_candidate_id"] = "candidate_000"
    registry["best_candidate_id"] = "candidate_000"
    registry["final_candidate_id"] = "candidate_000"
    manager.save_registry(registry)

    invariants = {
        "baseline_verified": baseline.get("status") == "DONE",
        "baseline_unchanged": baseline.get("baseline_unchanged") is True,
        "patch_policy_valid": True,
        "candidate_materialized_after_patch_validation": candidate_id == "candidate_001",
        "candidate_csim_failed": validation.validation["csim"].get("status") == "FAIL",
        "candidate_synth_not_run": validation.validation["synth"].get("status") == "NOT_RUN",
        "candidate_cosim_not_run": validation.validation["cosim"].get("status") == "NOT_RUN",
        "candidate_rejected": candidate["status"] == "REJECTED_VALIDATION",
        "best_preserved": registry.get("best_candidate_id") == "candidate_000",
        "final_preserved": registry.get("final_candidate_id") == "candidate_000",
        "active_rolled_back": registry.get("active_candidate_id") == "candidate_000",
        "llm_not_called": validation.budget["tool_used"].get("llm") == 0,
    }
    passed = all(invariants.values())
    result = {
        "schema_version": 1,
        "workflow": "V2_SAFETY_REJECTION",
        "task_id": task.id,
        "status": "DONE" if passed else "FAILED",
        "stop_reason": (
            "SAFETY_REGRESSION_REJECTED" if passed else "SAFETY_INVARIANT_FAILED"
        ),
        "baseline_candidate_id": "candidate_000",
        "rejected_candidate_id": candidate_id,
        "best_candidate_id": registry.get("best_candidate_id"),
        "final_candidate_id": registry.get("final_candidate_id"),
        "patch_sha256": patch_sha256,
        "patch": {
            "kernel_name": application.kernel_name,
            "original_sha256": application.original_sha256,
            "patched_sha256": application.patched_sha256,
            "additions": application.additions,
            "deletions": application.deletions,
            "hunks": application.hunks,
            "applied_patch": normalized,
        },
        "validation": validation.validation,
        "safety_invariants": invariants,
        "rollback": {
            "from": candidate_id,
            "to": "candidate_000",
            "reason": validation.stop_reason,
        },
        "budget": validation.budget,
        "artifacts": {
            "workflow_result": "workflow_result.json",
            "rejection_result": "v2_rejection_result.json",
            "candidate_registry": "candidate_registry.json",
            "budget_ledger": "budget_ledger.jsonl",
            "trace": "trace.jsonl",
            "experimental_report": "experimental_report.md",
            "artifact_manifest": "artifact_manifest.json",
        },
    }
    _atomic_json(completed_path, result)
    _append_trace(
        run_root / "trace.jsonl",
        "V2_SAFETY_REJECTION_COMPLETED",
        status=result["status"],
        stop_reason=result["stop_reason"],
        rejected_candidate_id=candidate_id,
        rollback_to="candidate_000",
        result_ref="v2_rejection_result.json",
    )
    _finalize_v2_artifacts(run_root, result, registry)
    return result


def run_v2(
    task: PublicTask,
    run_dir: str | Path,
    run_config: RunConfig,
    optimization_config: OptimizationConfig,
    provider: OptimizationProvider,
    *,
    backend: ToolBackend | None = None,
) -> dict[str, object]:
    if task.task_type != "optimize":
        raise ValueError("V2 requires task_type=optimize")
    if "llm" not in run_config.budget.costs:
        raise ValueError("V2 budget must configure llm")
    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    optimization_snapshot = optimization_config.to_dict() | {
        "provider_fingerprint": _provider_fingerprint(provider)
    }
    completed_path = run_root / "v2_result.json"
    if completed_path.is_file():
        try:
            completed = json.loads(completed_path.read_text(encoding="utf-8"))
            stored_optimization = json.loads(
                (run_root / "optimization_config.json").read_text(encoding="utf-8")
            )
            stored_run = json.loads(
                (run_root / "run_config.json").read_text(encoding="utf-8")
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"completed V2 run is unreadable: {exc}") from exc
        if (
            not isinstance(completed, dict)
            or completed.get("workflow") != "V2_CANDIDATE_PPA"
            or completed.get("status") != "DONE"
            or completed.get("task_id") != task.id
            or stored_optimization != optimization_snapshot
            or stored_run != run_config.to_dict()
            or not isinstance(registry, dict)
            or registry.get("best_candidate_id") != completed.get("best_candidate_id")
            or registry.get("final_candidate_id") != completed.get("final_candidate_id")
        ):
            raise ValueError("completed V2 run does not match this invocation")
        current_budget = BudgetLedger(
            run_root / "budget_ledger.jsonl", run_config.budget
        ).snapshot()
        recorded_budget = completed.get("budget")
        if not isinstance(recorded_budget, dict) or any(
            current_budget.get(key) != recorded_budget.get(key)
            for key in (
                "credits_used",
                "tokens_used",
                "input_tokens_used",
                "output_tokens_used",
                "cached_input_tokens_used",
                "tool_used",
            )
        ):
            raise ValueError("completed V2 budget does not match the ledger")
        verify_artifact_manifest(run_root)
        return completed
    baseline = run_v0(task, run_root, run_config, backend=backend)
    if baseline.get("status") != "DONE":
        result = {
            "schema_version": 1,
            "workflow": "V2_CANDIDATE_PPA",
            "status": "FAILED",
            "stop_reason": "BASELINE_NOT_VERIFIED",
            "baseline": baseline,
        }
        _atomic_json(run_root / "v2_result.json", result)
        return result
    _atomic_json(run_root / "optimization_config.json", optimization_snapshot)
    manager = CandidateManager(run_root, task)
    registry = manager.load_registry()
    baseline_ref = str(baseline["validation"]["synth"]["result_ref"])
    baseline_metrics = _read_report(run_root, baseline_ref)
    baseline_validation = CandidateValidation(
        status="DONE",
        stop_reason="BASELINE_VERIFIED",
        validation=baseline["validation"],
        clock_constraint=baseline["clock_constraint"],
        metrics_ref=baseline_ref,
        budget=baseline["budget"],
    )
    baseline_score = _score_from_validation(
        candidate_id="candidate_000",
        baseline_metrics=baseline_metrics,
        candidate_metrics=baseline_metrics,
        validation=baseline_validation,
        config=optimization_config.scoring,
        proposal=None,
        credits_used=int(baseline["budget"]["credits_used"]),
    )
    scores: dict[str, CandidateScore] = {"candidate_000": baseline_score}
    metrics_by_candidate: dict[str, dict[str, object]] = {
        "candidate_000": baseline_metrics
    }
    _atomic_json(run_root / "scores/candidate_000.json", baseline_score.to_dict())
    candidates = registry["candidates"]
    baseline_record = candidates["candidate_000"]
    baseline_record["score_ref"] = "scores/candidate_000.json"
    baseline_record["metrics_ref"] = baseline_ref
    manager.save_registry(registry)

    budget = BudgetLedger(run_root / "budget_ledger.jsonl", run_config.budget)
    rounds: list[dict[str, object]] = []
    attempted: list[str] = []
    failures: list[tuple[str, str]] = []
    no_improvement = 0
    best_id = "candidate_000"
    exploration_stop_reason = "MAX_OPTIMIZATION_ROUNDS"

    round_paths = sorted((run_root / "optimization_rounds").glob("round_*.json"))
    for expected_index, round_path in enumerate(round_paths, start=1):
        try:
            round_record = json.loads(round_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot recover {round_path.name}: {exc}") from exc
        if (
            not isinstance(round_record, dict)
            or round_record.get("round_index") != expected_index
            or round_record.get("optimization_class") not in ALLOWED_OPTIMIZATIONS
            or not isinstance(round_record.get("decision"), str)
        ):
            raise ValueError(f"invalid durable optimization round: {round_path.name}")
        optimization_class = str(round_record["optimization_class"])
        attempted.append(optimization_class)
        selector = round_record.get("selector")
        metrics_digest = (
            str(selector.get("metrics_digest"))
            if isinstance(selector, Mapping)
            else ""
        )
        candidate_id = round_record.get("candidate_id")
        if isinstance(candidate_id, str):
            candidate_record = candidates.get(candidate_id)
            if not isinstance(candidate_record, Mapping):
                raise ValueError(
                    f"durable round Candidate is missing: {candidate_id}"
                )
            score_ref = candidate_record.get("score_ref")
            metrics_ref = candidate_record.get("metrics_ref")
            if isinstance(score_ref, str) and isinstance(metrics_ref, str):
                scores[candidate_id] = _read_score(run_root, score_ref)
                metrics_by_candidate[candidate_id] = _read_report(
                    run_root, metrics_ref
                )
        if round_record["decision"] == "PROMOTED":
            if not isinstance(candidate_id, str) or candidate_id not in scores:
                raise ValueError("promoted durable round has no scored Candidate")
            best_id = candidate_id
            no_improvement = 0
        else:
            failures.append((optimization_class, metrics_digest))
            no_improvement += 1
        recovered = dict(round_record)
        recovered["result_ref"] = str(round_path.relative_to(run_root)).replace(
            "\\", "/"
        )
        rounds.append(recovered)
    if registry.get("best_candidate_id") != best_id:
        raise ValueError("Candidate Registry best disagrees with durable rounds")

    for round_index in range(len(rounds) + 1, optimization_config.max_rounds + 1):
        try:
            _ensure_round_affordable(budget, optimization_config)
        except BudgetExceeded:
            exploration_stop_reason = "FINAL_RESERVE_REACHED"
            break
        current_metrics = metrics_by_candidate[best_id]
        current_source = _candidate_source(run_root, registry, best_id)
        decision = select_optimization(
            current_metrics,
            source=current_source.decode("utf-8"),
            attempted=tuple(attempted),
            failures=tuple(failures),
        )
        if decision.optimization_class is None:
            exploration_stop_reason = decision.stop_reason or "NO_DISTINCT_OPTIMIZATION"
            break
        attempted.append(decision.optimization_class)
        snapshot = budget.snapshot()
        context = OptimizationContext(
            task_id=task.id,
            parent_candidate_id=best_id,
            round_index=round_index,
            allowed_optimization_class=decision.optimization_class,
            bottleneck=decision.bottleneck,
            evidence=decision.evidence,
            baseline_metrics=baseline_metrics,
            current_metrics=current_metrics,
            source_excerpt=current_source.decode("utf-8"),
            failed_actions=tuple(
                {"optimization_class": item, "metrics_digest": digest}
                for item, digest in failures
            ),
            remaining_tokens=int(snapshot["tokens_remaining"]),
            remaining_credits=(
                int(snapshot["credits_remaining"])
                if snapshot["credits_remaining"] is not None
                else None
            ),
            final_reserve_credits=optimization_config.final_reserve_credits,
            top=task.top,
            kernel_name=task.kernel_name,
            part=run_config.tool.part,
            clock_ns=run_config.tool.clock_ns,
        )
        proposal, provider_ref = _call_optimization_provider(
            provider,
            context,
            run_root=run_root,
            budget=budget,
            parent_id=best_id,
            code_hash=str(candidates[best_id]["code_hash"]),
            tool_config_hash=run_config.tool.hash_for(
                "csim", backend_fingerprint="optimization-context"
            ),
        )
        round_record: dict[str, object] = {
            "round_index": round_index,
            "parent_candidate_id": best_id,
            "optimization_class": decision.optimization_class,
            "selector": asdict(decision),
            "provider_ref": provider_ref,
            "candidate_id": None,
            "decision": "PROVIDER_REJECTED",
        }
        if proposal is None:
            failures.append((decision.optimization_class, decision.metrics_digest))
            no_improvement += 1
        else:
            normalized = normalize_unified_diff_headers(proposal.patch)
            try:
                application = apply_unified_diff(
                    current_source,
                    normalized,
                    kernel_name=task.kernel_name,
                    limits=optimization_config.patch_limits,
                )
            except PatchValidationError as exc:
                round_record["decision"] = "PATCH_REJECTED"
                round_record["error"] = str(exc)
                failures.append((decision.optimization_class, decision.metrics_digest))
                no_improvement += 1
            else:
                before_credits = int(budget.snapshot()["credits_used"])
                with _RunLock(run_root):
                    registry = manager.load_registry()
                    materialized = manager.materialize(
                        registry,
                        parent_id=best_id,
                        patch_text=normalized,
                        application=application,
                        kind="optimization",
                        metadata={
                            "round": round_index,
                            "optimization_class": decision.optimization_class,
                            "llm_ref": provider_ref,
                            "provider": proposal.provider,
                            "model": proposal.model,
                            "revision": proposal.revision,
                            "input_tokens": proposal.input_tokens,
                            "output_tokens": proposal.output_tokens,
                            "cached_input_tokens": proposal.cached_input_tokens,
                        },
                    )
                candidate_id = materialized.candidate_id
                round_record["candidate_id"] = candidate_id
                validation = validate_candidate(
                    task,
                    application.patched_bytes,
                    candidate_id,
                    run_root,
                    run_config,
                    backend=backend,
                    validation_scope="exploration",
                )
                candidate_credits = int(validation.budget["credits_used"]) - before_credits
                with _RunLock(run_root):
                    registry = manager.load_registry()
                    candidates = registry["candidates"]
                    candidate = candidates[candidate_id]
                    candidate["validation"] = validation.validation
                    candidate["clock_constraint"] = validation.clock_constraint
                    candidate["metrics_ref"] = validation.metrics_ref
                    candidate["credits_used"] = candidate_credits
                    if validation.status != "DONE" or validation.metrics_ref is None:
                        candidate["status"] = "REJECTED_VALIDATION"
                        candidate["rejection_reason"] = validation.stop_reason
                        registry["active_candidate_id"] = best_id
                        round_record["decision"] = "REJECTED_VALIDATION"
                        round_record["stop_reason"] = validation.stop_reason
                        failures.append((decision.optimization_class, decision.metrics_digest))
                        no_improvement += 1
                    else:
                        candidate_metrics = _read_report(
                            run_root, validation.metrics_ref
                        )
                        score = _score_from_validation(
                            candidate_id=candidate_id,
                            baseline_metrics=baseline_metrics,
                            candidate_metrics=candidate_metrics,
                            validation=validation,
                            config=optimization_config.scoring,
                            proposal=proposal,
                            credits_used=candidate_credits,
                        )
                        score_ref = f"scores/{candidate_id}.json"
                        _atomic_json(run_root / score_ref, score.to_dict())
                        comparison = compare_scores(score, scores[best_id])
                        comparison_ref = f"comparisons/{candidate_id}.json"
                        _atomic_json(run_root / comparison_ref, comparison.to_dict())
                        candidate["score_ref"] = score_ref
                        candidate["comparison_ref"] = comparison_ref
                        scores[candidate_id] = score
                        metrics_by_candidate[candidate_id] = candidate_metrics
                        round_record["score_ref"] = score_ref
                        round_record["comparison_ref"] = comparison_ref
                        round_record["comparison"] = comparison.to_dict()
                        if comparison.strictly_better:
                            previous_best = best_id
                            candidate["status"] = "PROMOTED"
                            candidate["selection_status"] = "PROMOTED"
                            candidates[previous_best]["superseded_by"] = candidate_id
                            registry["best_candidate_id"] = candidate_id
                            registry["active_candidate_id"] = candidate_id
                            best_id = candidate_id
                            round_record["decision"] = "PROMOTED"
                            no_improvement = 0
                        else:
                            candidate["status"] = "REJECTED_NOT_BETTER"
                            candidate["selection_status"] = "REJECTED"
                            candidate["rejection_reason"] = comparison.reason
                            registry["active_candidate_id"] = best_id
                            round_record["decision"] = "REJECTED_NOT_BETTER"
                            no_improvement += 1
                    manager.save_registry(registry)
        round_ref = f"optimization_rounds/round_{round_index:03d}.json"
        _atomic_json(run_root / round_ref, round_record)
        round_record["result_ref"] = round_ref
        rounds.append(round_record)
        _append_trace(
            run_root / "trace.jsonl",
            "V2_ROUND_COMPLETED",
            round_index=round_index,
            parent_candidate_id=round_record["parent_candidate_id"],
            candidate_id=round_record.get("candidate_id"),
            optimization_class=decision.optimization_class,
            decision=round_record["decision"],
            best_candidate_id=best_id,
            no_improvement_rounds=no_improvement,
            result_ref=round_ref,
        )
        if no_improvement >= optimization_config.max_no_improvement_rounds:
            exploration_stop_reason = "NO_IMPROVEMENT_LIMIT"
            break

    registry = manager.load_registry()
    final_source = _candidate_source(run_root, registry, best_id)
    final_validation = validate_candidate(
        task,
        final_source,
        best_id,
        run_root,
        run_config,
        backend=backend,
        validation_scope="final",
    )
    final_attempts: list[dict[str, object]] = [
        {
            "candidate_id": best_id,
            "status": final_validation.status,
            "stop_reason": final_validation.stop_reason,
            "validation": final_validation.validation,
            "clock_constraint": final_validation.clock_constraint,
        }
    ]
    fallback: dict[str, object] | None = None
    final_id: str | None = best_id if final_validation.status == "DONE" else None
    overall_stop_reason = exploration_stop_reason
    if final_validation.status != "DONE":
        initial_failure = final_validation.stop_reason
        eligible = _rank_scores(
            [
                score
                for candidate_id, score in scores.items()
                if candidate_id != best_id
                and score.verification_tier >= optimization_config.scoring.required_verification_tier
                and score.hard_constraints_passed
            ]
        )
        for fallback_score in eligible:
            try:
                _ensure_final_affordable(budget)
            except BudgetExceeded:
                break
            fallback_id = fallback_score.candidate_id
            fallback_source = _candidate_source(run_root, registry, fallback_id)
            attempt = validate_candidate(
                task,
                fallback_source,
                fallback_id,
                run_root,
                run_config,
                backend=backend,
                validation_scope="final",
            )
            final_attempts.append(
                {
                    "candidate_id": fallback_id,
                    "status": attempt.status,
                    "stop_reason": attempt.stop_reason,
                    "validation": attempt.validation,
                    "clock_constraint": attempt.clock_constraint,
                }
            )
            if attempt.status == "DONE":
                final_validation = attempt
                final_id = fallback_id
                fallback = {
                    "from": best_id,
                    "to": fallback_id,
                    "reason": initial_failure,
                }
                overall_stop_reason = "FALLBACK_VERIFIED"
                break
    with _RunLock(run_root):
        registry = manager.load_registry()
        candidates = registry["candidates"]
        candidate = candidates[final_id or best_id]
        candidate["final_validation"] = final_validation.validation
        if final_id is not None and final_validation.status == "DONE":
            candidate["status"] = "FINAL" if final_id == best_id else "FINAL_FALLBACK"
            registry["final_candidate_id"] = final_id
            status = "DONE"
        else:
            status = "FAILED"
            overall_stop_reason = "FINAL_VALIDATION_FAILED"
        manager.save_registry(registry)
    result = {
        "schema_version": 1,
        "workflow": "V2_CANDIDATE_PPA",
        "task_id": task.id,
        "status": status,
        "stop_reason": overall_stop_reason,
        "exploration_stop_reason": exploration_stop_reason,
        "baseline_candidate_id": "candidate_000",
        "best_candidate_id": registry.get("best_candidate_id"),
        "final_candidate_id": registry.get("final_candidate_id"),
        "rounds": rounds,
        "no_improvement_rounds": no_improvement,
        "final_validation": final_validation.validation,
        "final_attempts": final_attempts,
        "final_clock_constraint": final_validation.clock_constraint,
        "budget": final_validation.budget,
        "fallback": fallback,
        "artifacts": {
            "candidate_registry": "candidate_registry.json",
            "budget_ledger": "budget_ledger.jsonl",
            "trace": "trace.jsonl",
            "result": "v2_result.json",
            "experimental_report": "experimental_report.md",
            "artifact_manifest": "artifact_manifest.json",
        },
    }
    _atomic_json(run_root / "v2_result.json", result)
    _append_trace(
        run_root / "trace.jsonl",
        "V2_RUN_COMPLETED",
        status=status,
        stop_reason=overall_stop_reason,
        best_candidate_id=registry.get("best_candidate_id"),
        final_candidate_id=registry.get("final_candidate_id"),
        credits_used=final_validation.budget["credits_used"],
        tokens_used=final_validation.budget["tokens_used"],
        result_ref="v2_result.json",
    )
    stored_result = json.loads(
        (run_root / "v2_result.json").read_text(encoding="utf-8")
    )
    if not isinstance(stored_result, dict):
        raise ValueError("stored V2 result is not an object")
    _finalize_v2_artifacts(run_root, stored_result, registry)
    return stored_result
