"""Deterministic V2 optimization selection and workflow boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field, replace
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
    relocate_unified_diff_hunks,
)
from .scoring import (
    OFFICIAL_ACCELERATION_CAP,
    OFFICIAL_SCORE_SOURCE,
    CandidateScore,
    ScoringConfig,
    compare_scores,
    estimate_official_score_proxy,
    score_candidate,
)
from .task import PublicTask
from .tools import ToolBackend
from .validation import (
    CandidateValidation,
    complete_candidate_cosim,
    validate_candidate,
    validate_csim_only,
)
from .v2_team_report import write_v2_team_report
from .workflow import RunConfig, _RunLock, _append_trace, _atomic_json, run_v0


ALLOWED_OPTIMIZATIONS = (
    "LOOP_PIPELINE",
    "LOOP_UNROLL",
    "MEMORY_LAYOUT",
    "LOOP_RESTRUCTURE",
)

_HLS_RULES = {
    "LOOP_PIPELINE": (
        "Apply PIPELINE only to the selected bottleneck loop.",
        "Preserve the loop bounds, interfaces, and arithmetic semantics.",
    ),
    "LOOP_UNROLL": (
        "Apply UNROLL only to the selected loop and use a bounded factor.",
        "Do not change the function interface or numerical result.",
    ),
    "MEMORY_LAYOUT": (
        "Change only kernel-local HLS memory layout pragmas.",
        "Preserve array element order, ports, and observable semantics.",
    ),
    "LOOP_RESTRUCTURE": (
        "Restructure only the selected loop without changing iteration coverage.",
        "Preserve interfaces, ordering dependencies, and numerical semantics.",
    ),
}


@dataclass(frozen=True)
class OptimizationDecision:
    optimization_class: str | None
    bottleneck: str
    evidence: tuple[str, ...]
    metrics_digest: str
    stop_reason: str | None = None


@dataclass(frozen=True)
class OptimizationSelectionContext:
    metrics_digest: str
    attempted_same_metrics: tuple[str, ...]
    failed_same_metrics: tuple[str, ...]
    available_classes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics_digest": self.metrics_digest,
            "attempted_same_metrics": list(self.attempted_same_metrics),
            "failed_same_metrics": list(self.failed_same_metrics),
            "available_classes": list(self.available_classes),
        }


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
    current_validation: Mapping[str, object]
    current_clock_constraint: Mapping[str, object]
    source_excerpt: str
    hls_rules: tuple[str, ...]
    failed_actions: tuple[Mapping[str, object], ...]
    remaining_tokens: int
    remaining_credits: int | None
    final_reserve_credits: int
    top: str
    kernel_name: str
    part: str
    clock_ns: float
    difficulty: int
    official_score_enabled: bool
    current_official_score: float | None
    official_acceleration_cap: float = OFFICIAL_ACCELERATION_CAP

    def __post_init__(self) -> None:
        if self.allowed_optimization_class not in ALLOWED_OPTIMIZATIONS:
            raise ValueError("optimization context has an unsupported class")
        if self.round_index <= 0:
            raise ValueError("optimization round index must be positive")
        if self.remaining_tokens < 0 or self.final_reserve_credits < 0:
            raise ValueError("optimization budget values must be non-negative")
        if (
            isinstance(self.difficulty, bool)
            or not isinstance(self.difficulty, int)
            or self.difficulty <= 0
        ):
            raise ValueError("optimization difficulty must be positive")
        if not isinstance(self.official_score_enabled, bool):
            raise ValueError("official score enabled flag must be boolean")
        if self.current_official_score is not None and (
            isinstance(self.current_official_score, bool)
            or not isinstance(self.current_official_score, (int, float))
            or not math.isfinite(float(self.current_official_score))
            or float(self.current_official_score) < 0
        ):
            raise ValueError("current official score must be finite and non-negative")
        if (
            isinstance(self.official_acceleration_cap, bool)
            or not isinstance(self.official_acceleration_cap, (int, float))
            or not math.isfinite(float(self.official_acceleration_cap))
            or self.official_acceleration_cap <= 0
        ):
            raise ValueError("official acceleration cap must be positive")
        if not 1 <= len(self.hls_rules) <= 3 or any(
            not isinstance(rule, str) or not rule.strip() for rule in self.hls_rules
        ):
            raise ValueError("optimization context requires one to three HLS rules")

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
            "current_validation": dict(self.current_validation),
            "current_clock_constraint": dict(self.current_clock_constraint),
            "source_excerpt": self.source_excerpt,
            "hls_rules": list(self.hls_rules),
            "failed_actions": [dict(item) for item in self.failed_actions],
            "remaining_tokens": self.remaining_tokens,
            "remaining_credits": self.remaining_credits,
            "final_reserve_credits": self.final_reserve_credits,
            "top": self.top,
            "kernel_name": self.kernel_name,
            "part": self.part,
            "clock_ns": self.clock_ns,
            "difficulty": self.difficulty,
            "official_score_enabled": self.official_score_enabled,
            "current_official_score": self.current_official_score,
            "official_acceleration_cap": self.official_acceleration_cap,
        }


class OptimizationProvider(Protocol):
    def propose_optimization(self, context: OptimizationContext) -> PatchProposal: ...


@dataclass(frozen=True)
class OptimizationConfig:
    scoring: ScoringConfig
    max_rounds: int = 6
    max_no_improvement_rounds: int = 2
    max_llm_calls: int = 6
    max_final_attempts: int = 2
    final_reserve_credits: int = 25
    exploration_cosim_policy: str = "auto"
    patch_limits: PatchLimits = field(
        default_factory=lambda: PatchLimits(max_changed_lines=30, max_hunks=4)
    )

    def __post_init__(self) -> None:
        if (
            self.max_rounds <= 0
            or self.max_no_improvement_rounds <= 0
            or self.max_final_attempts <= 0
        ):
            raise ValueError("optimization round limits must be positive")
        if self.max_llm_calls < self.max_rounds:
            raise ValueError("LLM call limit cannot be lower than the round limit")
        if self.final_reserve_credits < 0:
            raise ValueError("final reserve credits must be non-negative")
        if self.exploration_cosim_policy == "auto":
            object.__setattr__(
                self,
                "exploration_cosim_policy",
                (
                    "official_score_gate"
                    if self.scoring.official_score_enabled
                    else "ppa_gate"
                ),
            )
        if self.exploration_cosim_policy not in {"ppa_gate", "official_score_gate"}:
            raise ValueError("unsupported exploration cosim policy")
        if self.scoring.official_score_enabled != (
            self.exploration_cosim_policy == "official_score_gate"
        ):
            raise ValueError("official scoring and exploration gate policy disagree")

    def to_dict(self) -> dict[str, object]:
        return {
            "scoring": self.scoring.to_dict(),
            "max_rounds": self.max_rounds,
            "max_no_improvement_rounds": self.max_no_improvement_rounds,
            "max_llm_calls": self.max_llm_calls,
            "max_final_attempts": self.max_final_attempts,
            "final_reserve_credits": self.final_reserve_credits,
            "exploration_cosim_policy": self.exploration_cosim_policy,
            "patch_limits": asdict(self.patch_limits),
        }


@dataclass(frozen=True)
class ExplorationCosimGate:
    candidate_id: str
    incumbent_id: str
    eligible: bool
    reason: str
    candidate_ppa_cost: float | None
    incumbent_ppa_cost: float | None
    candidate_hard_constraints_passed: bool
    policy: str = "ppa_gate"
    candidate_official_score: float | None = None
    incumbent_official_score: float | None = None

    def to_dict(self) -> dict[str, object]:
        value = {
            "candidate_id": self.candidate_id,
            "incumbent_id": self.incumbent_id,
            "eligible": self.eligible,
            "reason": self.reason,
            "candidate_ppa_cost": self.candidate_ppa_cost,
            "incumbent_ppa_cost": self.incumbent_ppa_cost,
            "candidate_hard_constraints_passed": self.candidate_hard_constraints_passed,
        }
        if self.policy == "official_score_gate":
            value.update(
                {
                    "policy": self.policy,
                    "candidate_official_score": self.candidate_official_score,
                    "incumbent_official_score": self.incumbent_official_score,
                }
            )
        return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_context_value(value: Mapping[str, object]) -> dict[str, object]:
    stable = dict(value)
    stable.pop("remaining_tokens", None)
    stable.pop("remaining_credits", None)
    validation = stable.get("current_validation")
    if isinstance(validation, Mapping):
        stable["current_validation"] = {
            str(stage): ({
                str(key): item
                for key, item in record.items()
                if key != "cached"
            } if isinstance(record, Mapping) else record)
            for stage, record in validation.items()
        }
    return stable


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

    def candidate_score(candidate: Mapping[str, object]) -> dict[str, object]:
        for field in ("score_ref", "pre_cosim_score_ref"):
            reference = candidate.get(field)
            if isinstance(reference, str):
                try:
                    return _read_score(run_root, reference).to_dict()
                except ValueError:
                    continue
        return {}

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
        try:
            optimization_snapshot = json.loads(
                (run_root / "optimization_config.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            optimization_snapshot = {}
        scoring_snapshot = (
            optimization_snapshot.get("scoring", {})
            if isinstance(optimization_snapshot, Mapping)
            else {}
        )
        official_snapshot = (
            scoring_snapshot.get("official_score", {})
            if isinstance(scoring_snapshot, Mapping)
            else {}
        )
        candidates = registry.get("candidates")
        candidate_map = candidates if isinstance(candidates, Mapping) else {}
        baseline_record = candidate_map.get("candidate_000")
        baseline_record_value = (
            baseline_record if isinstance(baseline_record, Mapping) else {}
        )
        baseline_score = candidate_score(baseline_record_value)
        lines.extend(
            [
                "",
                "## Scoring policy",
                "",
                f"- Primary: `{'official public proxy' if official_snapshot.get('enabled') is True else 'internal PPA'}`",
                f"- Official proxy version / acceleration cap: `{official_snapshot.get('version')} / {official_snapshot.get('acceleration_cap')}x`",
                f"- Exploration CoSim policy: `{optimization_snapshot.get('exploration_cosim_policy') if isinstance(optimization_snapshot, Mapping) else None}`",
                f"- Baseline official proxy / source: `{baseline_score.get('official_score')} / {baseline_score.get('official_score_source')}`",
                f"- PPA role / baseline cost: `tie-break and hard constraints / {baseline_score.get('ppa_cost')}`",
                "",
                "## Candidate rounds",
                "",
                "| Round | Parent | Class | Candidate | CoSim gate | Candidate / Best official | Candidate / Best PPA tie-break | CoSim | Decision |",
                "|---:|---|---|---|---|---:|---:|---|---|",
            ]
        )
        rounds = result.get("rounds")
        for item in rounds if isinstance(rounds, list) else []:
            value = item if isinstance(item, Mapping) else {}
            gate = value.get("cosim_gate")
            gate_value = gate if isinstance(gate, Mapping) else {}
            candidate = candidate_map.get(value.get("candidate_id"))
            candidate_value = candidate if isinstance(candidate, Mapping) else {}
            validation = candidate_value.get("validation")
            validation_value = validation if isinstance(validation, Mapping) else {}
            cosim = validation_value.get("cosim")
            cosim_value = cosim if isinstance(cosim, Mapping) else {}
            lines.append(
                f"| {value.get('round_index', '')} | "
                f"{value.get('parent_candidate_id', '')} | "
                f"{value.get('optimization_class', '')} | "
                f"{value.get('candidate_id', '')} | "
                f"{gate_value.get('eligible')} / {gate_value.get('reason')} | "
                f"{gate_value.get('candidate_official_score')} / {gate_value.get('incumbent_official_score')} | "
                f"{gate_value.get('candidate_ppa_cost')} / {gate_value.get('incumbent_ppa_cost')} | "
                f"{cosim_value.get('status', 'NOT_RUN')} | "
                f"{value.get('decision', '')} |"
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
        final_candidate = candidate_map.get(final_id)
        final_record = final_candidate if isinstance(final_candidate, Mapping) else {}
        final_score = candidate_score(final_record)
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
                "## Final selected score and PPA metrics",
                "",
                f"- Official proxy / source: `{final_score.get('official_score')} / {final_score.get('official_score_source')}`",
                f"- PPA tie-break cost: `{final_score.get('ppa_cost')}`",
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
    if result.get("workflow") == "V2_CANDIDATE_PPA":
        # The optimize report is reconstructed from persisted evidence rather
        # than trusting the caller's in-memory view.
        write_v2_team_report(run_root, mode="automatic")
    else:
        # Keep the focused safety-rejection report for its distinct workflow.
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


def describe_optimization_selection(
    metrics: Mapping[str, object],
    *,
    attempted: tuple[str | tuple[str, str], ...],
    failures: tuple[tuple[str, str], ...],
) -> OptimizationSelectionContext:
    """Describe the exact same-metrics branch set used by the Selector."""
    digest = _canonical_digest(metrics)
    attempted_same_metrics: set[str] = set()
    for item in attempted:
        if isinstance(item, str):
            optimization_class = item
            metrics_digest = digest
        elif (
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(value, str) for value in item)
        ):
            optimization_class, metrics_digest = item
        else:
            raise ValueError("attempted optimization record is invalid")
        if optimization_class not in ALLOWED_OPTIMIZATIONS:
            raise ValueError("attempted optimization contains an unsupported class")
        if metrics_digest == digest:
            attempted_same_metrics.add(optimization_class)
    failed_same = {
        optimization_class
        for optimization_class, metrics_digest in failures
        if metrics_digest == digest
    }
    if any(item not in ALLOWED_OPTIMIZATIONS for item in failed_same):
        raise ValueError("failed optimization contains an unsupported class")
    available = tuple(
        item
        for item in ALLOWED_OPTIMIZATIONS
        if item not in attempted_same_metrics and item not in failed_same
    )
    return OptimizationSelectionContext(
        metrics_digest=digest,
        attempted_same_metrics=tuple(
            item for item in ALLOWED_OPTIMIZATIONS if item in attempted_same_metrics
        ),
        failed_same_metrics=tuple(
            item for item in ALLOWED_OPTIMIZATIONS if item in failed_same
        ),
        available_classes=available,
    )


def select_optimization(
    metrics: Mapping[str, object],
    *,
    source: str,
    attempted: tuple[str | tuple[str, str], ...],
    failures: tuple[tuple[str, str], ...],
) -> OptimizationDecision:
    """Select one untried class from structured evidence without side effects."""

    del source
    selection = describe_optimization_selection(
        metrics,
        attempted=attempted,
        failures=failures,
    )
    digest = selection.metrics_digest
    available = selection.available_classes
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
    loop_section = metrics.get("loop_evidence")
    raw_loops = (
        loop_section.get("loops") if isinstance(loop_section, Mapping) else None
    )
    loops = [item for item in raw_loops if isinstance(item, Mapping)] if isinstance(
        raw_loops, list
    ) else []

    def positive_loop_number(loop: Mapping[str, object], name: str) -> float | None:
        value = loop.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    dominant = max(
        loops,
        key=lambda item: (
            positive_loop_number(item, "latency_cycles") or 0.0,
            positive_loop_number(item, "trip_count") or 0.0,
            str(item.get("name", "")),
        ),
    ) if loops else None
    pipeline_targets = [
        loop
        for loop in loops
        if (positive_loop_number(loop, "pipeline_ii") or 0.0) > 1.0
    ]
    pipeline_target = max(
        pipeline_targets,
        key=lambda item: (
            positive_loop_number(item, "latency_cycles") or 0.0,
            positive_loop_number(item, "pipeline_ii") or 0.0,
            positive_loop_number(item, "trip_count") or 0.0,
            str(item.get("name", "")),
        ),
    ) if pipeline_targets else None
    pipeline_target_ii = (
        positive_loop_number(pipeline_target, "pipeline_ii")
        if pipeline_target is not None
        else None
    )
    pipeline_target_name = (
        str(pipeline_target.get("name", "unknown"))
        if pipeline_target is not None
        else "unknown"
    )
    loop_ii = (
        positive_loop_number(dominant, "pipeline_ii")
        if dominant is not None
        else None
    )
    loop_latency = (
        positive_loop_number(dominant, "latency_cycles")
        if dominant is not None
        else None
    )
    trip_count = (
        positive_loop_number(dominant, "trip_count")
        if dominant is not None
        else None
    )
    loop_name = str(dominant.get("name", "unknown")) if dominant else "unknown"
    loop_diagnostics = " ".join(
        str(loop.get(name) or "")
        for loop in loops
        for name in ("issue_type", "violation_type")
    ).lower()
    memory_signal = any(
        token in f"{joined} {loop_diagnostics}"
        for token in ("memory port", "limited memory", "load operation")
    )
    decision_evidence = list(evidence)
    if dominant is not None:
        decision_evidence.append(
            "direct loop evidence: "
            f"{loop_name} PipelineII={loop_ii}, TripCount={trip_count}, "
            f"Latency={loop_latency}; top-level transaction interval={interval:g}"
        )
    if pipeline_target is not None and pipeline_target is not dominant:
        decision_evidence.append(
            "actionable II bottleneck: "
            f"{pipeline_target_name} PipelineII={pipeline_target_ii}, "
            f"TripCount={positive_loop_number(pipeline_target, 'trip_count')}, "
            f"Latency={positive_loop_number(pipeline_target, 'latency_cycles')}"
        )

    if (
        "MEMORY_LAYOUT" in available
        and memory_signal
    ):
        selected = "MEMORY_LAYOUT"
        bottleneck = "direct scheduler evidence reports a memory access bottleneck"
    elif pipeline_target is not None and "LOOP_PIPELINE" in available:
        selected = "LOOP_PIPELINE"
        bottleneck = (
            f"loop {pipeline_target_name} has reported PipelineII "
            f"{pipeline_target_ii:g}"
        )
    elif (
        dominant is not None
        and loop_ii == 1
        and "LOOP_UNROLL" in available
        and (trip_count or 0.0) >= 4.0
    ):
        selected = "LOOP_UNROLL"
        bottleneck = (
            f"loop {loop_name} is already PipelineII=1 but has TripCount "
            f"{trip_count:g}; pipeline repetition would not reduce its II"
        )
    elif not loops and interval > 1 and "LOOP_PIPELINE" in available:
        selected = "LOOP_PIPELINE"
        bottleneck = (
            f"maximum transaction interval is {interval:g}; loop evidence is "
            "unavailable, so this is a legacy heuristic"
        )
        decision_evidence.append("LEGACY_TOP_LEVEL_HEURISTIC")
    elif "LOOP_UNROLL" in available and (
        (loop_latency is not None and loop_latency > 4.0)
        or (not loops and latency > max(4.0, 4.0 * interval))
    ):
        selected = "LOOP_UNROLL"
        bottleneck = (
            f"dominant loop latency is {loop_latency:g}"
            if loop_latency is not None
            else f"worst latency is {latency:g} with interval {interval:g}"
        )
    elif "MEMORY_LAYOUT" in available:
        selected = "MEMORY_LAYOUT"
        bottleneck = "pipeline and unroll opportunities were exhausted"
    else:
        selected = available[0]
        bottleneck = "remaining loop structure bottleneck"

    return OptimizationDecision(
        optimization_class=selected,
        bottleneck=bottleneck,
        evidence=tuple(decision_evidence),
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


def _request_audit(
    provider: OptimizationProvider,
    context: OptimizationContext,
    *,
    action_id: str,
) -> dict[str, object]:
    describe = getattr(provider, "describe_optimization_request", None)
    provider_request: Mapping[str, object] = {}
    if callable(describe):
        described = describe(context)
        if not isinstance(described, Mapping):
            raise ValueError("optimization request description must be an object")
        provider_request = described
    source_lines = context.source_excerpt.splitlines()
    return {
        "schema_version": 1,
        "action_id": action_id,
        "purpose": "ppa_optimization",
        "provider_fingerprint": _provider_fingerprint(provider),
        "sent_files": [context.kernel_name],
        "code_ranges": [
            {
                "file": context.kernel_name,
                "start_line": 1,
                "end_line": max(1, len(source_lines)),
            }
        ],
        "hls_rules": list(context.hls_rules),
        "context_mode": "MINIMAL",
        "context": context.to_dict(),
        "provider_request": dict(provider_request),
        "excluded_categories": [
            "complete repository",
            "complete logs",
            "testbench",
            "hidden/reference content",
            "unrelated source",
            "machine absolute paths",
            "API keys and sensitive configuration",
        ],
    }


def _persist_request_audit(path: Path, value: Mapping[str, object]) -> None:
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepairProviderError(f"optimization request audit is unreadable: {exc}") from exc
        if existing != dict(value):
            raise RepairProviderError("optimization request audit does not match invocation")
        return
    _atomic_json(path, value)


def _ensure_provider_tokens_affordable(
    budget: BudgetLedger,
    request_audit: Mapping[str, object],
) -> int:
    """Fail before an API call unless its bounded request can fit the token budget."""

    provider_request = request_audit.get("provider_request")
    request_value = provider_request if isinstance(provider_request, Mapping) else {}
    http_body = request_value.get("http_body")
    if not isinstance(http_body, Mapping):
        raise ValueError("optimization provider must describe its bounded HTTP body")
    max_output_tokens = http_body.get("max_tokens")
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens <= 0
    ):
        raise ValueError("optimization provider must declare a positive max_tokens")
    encoded = json.dumps(
        http_body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    input_token_upper_bound = len(encoded) + 512
    required = input_token_upper_bound + max_output_tokens
    remaining = int(budget.snapshot()["tokens_remaining"])
    if remaining < required:
        raise BudgetExceeded(
            "optimization provider request requires a conservative reserve of "
            f"{required} tokens but {remaining} remain"
        )
    return required


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
) -> tuple[PatchProposal | None, str, str, str | None]:
    stable_context = _stable_context_value(context.to_dict())
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
    request_ref = f"llm_actions/{action_id}/request.json"
    request_path = run_root / request_ref
    request_audit = _request_audit(provider, context, action_id=action_id)
    completed = budget.completed_event(action_id)
    if completed is not None:
        if completed.get("token_reservation_overrun") is True:
            raise BudgetExceeded(
                "cached optimization provider action exceeded its token reservation"
            )
        if not request_path.is_file():
            raise RepairProviderError("cached optimization request audit is missing")
        encoded = result_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != completed.get("result_sha256"):
            raise RepairProviderError("cached optimization result digest mismatch")
        value = json.loads(encoded.decode("utf-8"))
        if not isinstance(value, dict):
            raise RepairProviderError("cached optimization result is not an object")
        if value.get("ok") is not True:
            return None, result_ref, request_ref, str(
                value.get("error", "provider action failed")
            )
        return _proposal_from_dict(value), result_ref, request_ref, None
    if budget.has_pending(action_id) or budget.is_ambiguous(action_id):
        raise RepairProviderError(f"optimization action {action_id} is not recoverable")
    estimated_tokens = _ensure_provider_tokens_affordable(budget, request_audit)
    _persist_request_audit(request_path, request_audit)
    budget.reserve(
        action_id=action_id,
        kind="llm",
        candidate_id=parent_id,
        code_hash=code_hash,
        tool_config_hash=tool_config_hash,
        estimated_tokens=estimated_tokens,
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
        request_id = getattr(exc, "request_id", None)
        response_excerpt = getattr(exc, "response_excerpt", None)
        value = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_tokens,
            "duration_seconds": duration,
            "request_id": str(request_id) if request_id else None,
            "response_excerpt": (
                str(response_excerpt) if response_excerpt is not None else None
            ),
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
        return None, result_ref, request_ref, str(value["error"])
    return proposal, result_ref, request_ref, None


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


def _recovered_validation_is_bound(
    run_root: Path,
    validation: Mapping[str, object],
    *,
    candidate_id: str,
    code_hash: str,
) -> bool:
    for stage in ("csim", "synth", "cosim"):
        record = validation.get(stage)
        if not isinstance(record, Mapping):
            return False
        status = record.get("status")
        if status == "NOT_RUN":
            continue
        result_ref = record.get("result_ref")
        if not isinstance(result_ref, str):
            return False
        try:
            action = json.loads(
                (run_root / result_ref).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(action, Mapping):
            return False
        expected_status = "PASS" if action.get("ok") is True else "FAIL"
        if (
            status != expected_status
            or action.get("kind") != stage
            or action.get("candidate_id") != candidate_id
            or action.get("code_hash") != code_hash
            or action.get("action_id") != record.get("action_id")
            or action.get("result_ref") != result_ref
            or action.get("validation_scope", "exploration") != "exploration"
            or record.get("validation_scope", "exploration") != "exploration"
        ):
            return False
    return True


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
    difficulty: int,
    requires_cosim: bool,
) -> CandidateScore:
    official_score = (
        estimate_official_score_proxy(
            difficulty=difficulty,
            baseline=baseline_metrics,
            candidate=candidate_metrics,
            validation=validation.validation,
            requires_cosim=requires_cosim,
        )
        if config.official_score_enabled
        else None
    )
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
        official_score=official_score,
        official_score_source=(
            OFFICIAL_SCORE_SOURCE if official_score is not None else None
        ),
    )


def _score_for_cosim_gate(
    *,
    candidate_id: str,
    baseline_metrics: Mapping[str, object],
    candidate_metrics: Mapping[str, object],
    validation: CandidateValidation,
    config: ScoringConfig,
    proposal: PatchProposal,
    credits_used: int,
    difficulty: int,
    requires_cosim: bool,
) -> CandidateScore:
    """Score CSim+Synth evidence without pretending that CoSim has passed."""

    gate_validation = {
        stage: dict(record)
        for stage, record in validation.validation.items()
    }
    gate_validation["cosim"] = {"status": "NOT_RUN"}
    official_score = (
        estimate_official_score_proxy(
            difficulty=difficulty,
            baseline=baseline_metrics,
            candidate=candidate_metrics,
            validation=gate_validation,
            requires_cosim=requires_cosim,
            provisional_cosim=True,
        )
        if config.official_score_enabled
        else None
    )
    return score_candidate(
        candidate_id=candidate_id,
        baseline=baseline_metrics,
        candidate=candidate_metrics,
        validation=gate_validation,
        clock=validation.clock_constraint,
        config=replace(config, required_verification_tier=3),
        input_tokens=proposal.input_tokens,
        output_tokens=proposal.output_tokens,
        cached_input_tokens=proposal.cached_input_tokens,
        credits_used=credits_used,
        official_score=official_score,
        official_score_source=(
            OFFICIAL_SCORE_SOURCE if official_score is not None else None
        ),
    )


def evaluate_exploration_cosim_gate(
    candidate: CandidateScore,
    incumbent: CandidateScore,
    *,
    policy: str = "auto",
) -> ExplorationCosimGate:
    """Allow expensive CoSim only for a strict primary-score improvement."""

    if policy == "auto":
        policy = (
            "official_score_gate"
            if candidate.official_score is not None
            and incumbent.official_score is not None
            else "ppa_gate"
        )
    if policy not in {"ppa_gate", "official_score_gate"}:
        raise ValueError("unsupported exploration CoSim gate policy")

    if candidate.verification_tier < 3 or not candidate.hard_constraints_passed:
        eligible = False
        reason = "PRE_COSIM_HARD_CONSTRAINTS"
    elif policy == "official_score_gate":
        if candidate.official_score is None or incumbent.official_score is None:
            eligible = False
            reason = "OFFICIAL_SCORE_UNAVAILABLE"
        elif candidate.official_score > incumbent.official_score:
            eligible = True
            reason = "STRICT_OFFICIAL_SCORE_IMPROVEMENT"
        elif (
            candidate.official_score == incumbent.official_score
            and candidate.ppa_cost is not None
            and incumbent.ppa_cost is not None
            and candidate.ppa_cost < incumbent.ppa_cost
        ):
            eligible = True
            reason = "OFFICIAL_SCORE_TIE_PPA_IMPROVEMENT"
        else:
            eligible = False
            reason = "OFFICIAL_SCORE_NOT_BETTER"
    elif candidate.ppa_cost is None or incumbent.ppa_cost is None:
        eligible = False
        reason = "PPA_COST_UNAVAILABLE"
    elif candidate.ppa_cost < incumbent.ppa_cost:
        eligible = True
        reason = "STRICT_PPA_IMPROVEMENT"
    else:
        eligible = False
        reason = "PPA_NOT_BETTER"
    return ExplorationCosimGate(
        candidate_id=candidate.candidate_id,
        incumbent_id=incumbent.candidate_id,
        eligible=eligible,
        reason=reason,
        candidate_ppa_cost=candidate.ppa_cost,
        incumbent_ppa_cost=incumbent.ppa_cost,
        candidate_hard_constraints_passed=candidate.hard_constraints_passed,
        policy=policy,
        candidate_official_score=candidate.official_score,
        incumbent_official_score=incumbent.official_score,
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
    normalized = relocate_unified_diff_hunks(
        task.kernel_bytes,
        normalized,
        kernel_name=task.kernel_name,
    )
    patch_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    completed_path = run_root / "v2_rejection_result.json"
    if completed_path.is_file():
        try:
            completed = json.loads(completed_path.read_text(encoding="utf-8"))
            stored_run = json.loads(
                (run_root / "run_config.json").read_text(encoding="utf-8")
            )
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
            or stored_run != run_config.to_dict()
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
    existing_candidates = registry.get("candidates")
    if not isinstance(existing_candidates, Mapping):
        raise ValueError("V2 rejection Candidate Registry is invalid")
    unexpected = [
        candidate_id
        for candidate_id, raw in existing_candidates.items()
        if candidate_id != "candidate_000"
        and (
            not isinstance(raw, Mapping)
            or raw.get("kind") != "safety_regression"
            or raw.get("patch_sha256") != patch_sha256
        )
    ]
    if (
        unexpected
        or registry.get("best_candidate_id") != "candidate_000"
        or registry.get("final_candidate_id") != "candidate_000"
    ):
        raise ValueError("V2 rejection run directory contains non-safety Candidates")
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
    final_cost = sum(
        int(run_config.budget.costs[stage])
        for stage in ("csim", "synth", "cosim")
    )
    if optimization_config.final_reserve_credits < final_cost:
        raise ValueError(
            "final reserve credits cannot be lower than final validation cost"
        )
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
        stored_optimization_compatible = (
            dict(stored_optimization) if isinstance(stored_optimization, dict) else {}
        )
        stored_optimization_compatible.setdefault("max_final_attempts", 2)
        if (
            not isinstance(completed, dict)
            or completed.get("workflow") != "V2_CANDIDATE_PPA"
            or completed.get("status") != "DONE"
            or completed.get("task_id") != task.id
            or stored_optimization_compatible != optimization_snapshot
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
    _atomic_json(run_root / "optimization_config.json", optimization_snapshot)
    baseline = run_v0(task, run_root, run_config, backend=backend)
    if baseline.get("status") != "DONE":
        try:
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"failed V2 baseline Registry is unreadable: {exc}") from exc
        if not isinstance(registry, dict):
            raise ValueError("failed V2 baseline Registry is not an object")
        result = {
            "schema_version": 1,
            "workflow": "V2_CANDIDATE_PPA",
            "task_id": task.id,
            "status": "FAILED",
            "stop_reason": "BASELINE_NOT_VERIFIED",
            "exploration_stop_reason": "BASELINE_NOT_VERIFIED",
            "baseline_candidate_id": "candidate_000",
            "best_candidate_id": registry.get("best_candidate_id"),
            "final_candidate_id": registry.get("final_candidate_id"),
            "rounds": [],
            "no_improvement_rounds": 0,
            "final_validation": {},
            "final_attempts": [],
            "final_clock_constraint": baseline.get("clock_constraint", {}),
            "budget": baseline.get("budget", {}),
            "fallback": None,
            "baseline": baseline,
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
            status="FAILED",
            stop_reason="BASELINE_NOT_VERIFIED",
            best_candidate_id=registry.get("best_candidate_id"),
            final_candidate_id=registry.get("final_candidate_id"),
            credits_used=result["budget"].get("credits_used", 0),
            tokens_used=result["budget"].get("tokens_used", 0),
            result_ref="v2_result.json",
        )
        _finalize_v2_artifacts(run_root, result, registry)
        return result
    manager = CandidateManager(run_root, task)
    registry = manager.load_registry()
    candidates = registry["candidates"]
    baseline_record = candidates["candidate_000"]
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
    baseline_credits = sum(
        int(run_config.budget.costs[stage])
        for stage in ("csim", "synth", "cosim")
        if isinstance(baseline["validation"].get(stage), Mapping)
        and baseline["validation"][stage].get("status") == "PASS"
    )
    baseline_score = _score_from_validation(
        candidate_id="candidate_000",
        baseline_metrics=baseline_metrics,
        candidate_metrics=baseline_metrics,
        validation=baseline_validation,
        config=optimization_config.scoring,
        proposal=None,
        credits_used=baseline_credits,
        difficulty=task.difficulty,
        requires_cosim=task.requires_cosim,
    )
    scores: dict[str, CandidateScore] = {"candidate_000": baseline_score}
    metrics_by_candidate: dict[str, dict[str, object]] = {
        "candidate_000": baseline_metrics
    }
    validation_by_candidate: dict[str, Mapping[str, object]] = {
        "candidate_000": baseline["validation"]
    }
    clock_by_candidate: dict[str, Mapping[str, object]] = {
        "candidate_000": baseline["clock_constraint"]
    }
    _atomic_json(run_root / "scores/candidate_000.json", baseline_score.to_dict())
    baseline_record["score_ref"] = "scores/candidate_000.json"
    baseline_record["metrics_ref"] = baseline_ref
    baseline_record["credits_used"] = baseline_credits
    manager.save_registry(registry)

    budget = BudgetLedger(run_root / "budget_ledger.jsonl", run_config.budget)
    rounds: list[dict[str, object]] = []
    attempted: list[tuple[str, str]] = []
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
        selector = round_record.get("selector")
        current_source = _candidate_source(run_root, registry, best_id)
        expected_selector = select_optimization(
            metrics_by_candidate[best_id],
            source=current_source.decode("utf-8"),
            attempted=tuple(attempted),
            failures=tuple(failures),
        )
        expected_selection_context = describe_optimization_selection(
            metrics_by_candidate[best_id],
            attempted=tuple(attempted),
            failures=tuple(failures),
        ).to_dict()
        stored_selection_context = round_record.get("selection_context")
        if (
            round_record.get("parent_candidate_id") != best_id
            or not isinstance(selector, Mapping)
            or _canonical_digest(dict(selector))
            != _canonical_digest(asdict(expected_selector))
            or expected_selector.optimization_class != optimization_class
            or (
                stored_selection_context is not None
                and (
                    not isinstance(stored_selection_context, Mapping)
                    or _canonical_digest(dict(stored_selection_context))
                    != _canonical_digest(expected_selection_context)
                )
            )
        ):
            raise ValueError(
                f"durable round binding is invalid: {round_path.name}"
            )
        request_ref = round_record.get("request_ref")
        provider_ref = round_record.get("provider_ref")
        if not isinstance(request_ref, str) or not isinstance(provider_ref, str):
            raise ValueError(f"durable round evidence is missing: {round_path.name}")
        try:
            request_value = json.loads(
                (run_root / request_ref).read_text(encoding="utf-8")
            )
            provider_value = json.loads(
                (run_root / provider_ref).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"durable round evidence is unreadable: {round_path.name}: {exc}"
            ) from exc
        if not isinstance(request_value, dict) or not isinstance(provider_value, dict):
            raise ValueError(f"durable round evidence is invalid: {round_path.name}")
        request_context = request_value.get("context")
        if not isinstance(request_context, Mapping):
            raise ValueError(f"durable round context is missing: {round_path.name}")
        expected_context = OptimizationContext(
            task_id=task.id,
            parent_candidate_id=best_id,
            round_index=expected_index,
            allowed_optimization_class=optimization_class,
            bottleneck=expected_selector.bottleneck,
            evidence=expected_selector.evidence,
            baseline_metrics=baseline_metrics,
            current_metrics=metrics_by_candidate[best_id],
            current_validation=validation_by_candidate[best_id],
            current_clock_constraint=clock_by_candidate[best_id],
            source_excerpt=current_source.decode("utf-8"),
            hls_rules=_HLS_RULES[optimization_class],
            failed_actions=tuple(
                {"optimization_class": item, "metrics_digest": digest}
                for item, digest in failures
                if item == optimization_class
            ),
            remaining_tokens=0,
            remaining_credits=None,
            final_reserve_credits=optimization_config.final_reserve_credits,
            top=task.top,
            kernel_name=task.kernel_name,
            part=run_config.tool.part,
            clock_ns=run_config.tool.clock_ns,
            difficulty=task.difficulty,
            official_score_enabled=optimization_config.scoring.official_score_enabled,
            current_official_score=scores[best_id].official_score,
        )
        stable_context = _stable_context_value(expected_context.to_dict())
        context_digest = _canonical_digest(stable_context)
        action_id = _canonical_digest(
            {
                "kind": "llm",
                "purpose": "ppa_optimization",
                "candidate_id": best_id,
                "code_hash": str(candidates[best_id]["code_hash"]),
                "tool_config_hash": run_config.tool.hash_for(
                    "csim", backend_fingerprint="optimization-context"
                ),
                "provider": _provider_fingerprint(provider),
                "context": stable_context,
            }
        )
        if (
            _canonical_digest(_stable_context_value(request_context))
            != context_digest
        ):
            actual_context = _stable_context_value(request_context)
            differing = sorted(
                key
                for key in set(actual_context) | set(stable_context)
                if _canonical_digest(actual_context.get(key))
                != _canonical_digest(stable_context.get(key))
            )
            raise ValueError(
                "durable round context binding is invalid for fields "
                f"{differing}: {round_path.name}"
            )
        if (
            request_value.get("action_id") != action_id
            or request_ref != f"llm_actions/{action_id}/request.json"
            or provider_ref != f"llm_actions/{action_id}/result.json"
        ):
            raise ValueError(
                f"durable round action binding is invalid: {round_path.name}"
            )
        if round_record.get("context_digest", context_digest) != context_digest:
            raise ValueError(
                f"durable round context digest is invalid: {round_path.name}"
            )
        completed_provider = budget.completed_event(action_id)
        provider_bytes = (run_root / provider_ref).read_bytes()
        if (
            completed_provider is None
            or completed_provider.get("result_ref") != provider_ref
            or completed_provider.get("result_sha256")
            != hashlib.sha256(provider_bytes).hexdigest()
        ):
            raise ValueError(
                f"durable round Provider ledger binding is invalid: {round_path.name}"
            )
        metrics_digest = (
            str(selector.get("metrics_digest"))
            if isinstance(selector, Mapping)
            else ""
        )
        attempted.append((optimization_class, metrics_digest))
        candidate_id = round_record.get("candidate_id")
        if isinstance(candidate_id, str):
            candidate_record = candidates.get(candidate_id)
            if not isinstance(candidate_record, Mapping):
                raise ValueError(
                    f"durable round Candidate is missing: {candidate_id}"
                )
            if (
                candidate_record.get("parent_id") != best_id
                or candidate_record.get("round") != expected_index
                or candidate_record.get("optimization_class") != optimization_class
                or candidate_record.get("llm_ref") != provider_ref
                or candidate_record.get("llm_request_ref") != request_ref
                or provider_value.get("ok") is not True
            ):
                raise ValueError(
                    f"durable round Candidate binding is invalid: {candidate_id}"
                )
            proposal = _proposal_from_dict(provider_value)
            stored_patch_path = run_root / str(candidate_record.get("patch_ref", ""))
            stored_source_path = run_root / str(candidate_record.get("source_ref", ""))
            try:
                stored_patch = stored_patch_path.read_text(encoding="utf-8")
                normalized_patch = normalize_unified_diff_headers(proposal.patch)
                normalized_patch = relocate_unified_diff_hunks(
                    current_source,
                    normalized_patch,
                    kernel_name=task.kernel_name,
                )
                application = apply_unified_diff(
                    current_source,
                    normalized_patch,
                    kernel_name=task.kernel_name,
                    limits=optimization_config.patch_limits,
                )
                stored_source = stored_source_path.read_bytes()
            except (OSError, UnicodeDecodeError, PatchValidationError) as exc:
                raise ValueError(
                    f"durable round Patch binding is invalid: {candidate_id}: {exc}"
                ) from exc
            if (
                normalized_patch != stored_patch
                or application.patched_bytes != stored_source
                or application.patched_sha256 != candidate_record.get("code_hash")
                or proposal.change_class != optimization_class
                or proposal.input_tokens != candidate_record.get("input_tokens")
                or proposal.output_tokens != candidate_record.get("output_tokens")
                or proposal.cached_input_tokens
                != candidate_record.get("cached_input_tokens")
            ):
                raise ValueError(
                    f"durable round Provider/Patch binding is invalid: {candidate_id}"
                )
            candidate_validation = candidate_record.get("validation")
            candidate_clock = candidate_record.get("clock_constraint")
            if (
                not isinstance(candidate_validation, Mapping)
                or not isinstance(candidate_clock, Mapping)
                or not _recovered_validation_is_bound(
                    run_root,
                    candidate_validation,
                    candidate_id=candidate_id,
                    code_hash=str(candidate_record.get("code_hash")),
                )
            ):
                raise ValueError(
                    f"durable Candidate validation binding is invalid: {candidate_id}"
                )
            score_ref = candidate_record.get("score_ref")
            metrics_ref = candidate_record.get("metrics_ref")
            csim_record = candidate_validation.get("csim")
            synth_record = candidate_validation.get("synth")
            cosim_record = candidate_validation.get("cosim")
            reached_cosim_gate = (
                isinstance(csim_record, Mapping)
                and csim_record.get("status") == "PASS"
                and isinstance(synth_record, Mapping)
                and synth_record.get("status") == "PASS"
                and candidate_clock.get("passed") is True
            )
            pre_score_ref = candidate_record.get("pre_cosim_score_ref")
            gate_ref = candidate_record.get("cosim_gate_ref")
            if reached_cosim_gate:
                if (
                    not isinstance(metrics_ref, str)
                    or not isinstance(pre_score_ref, str)
                    or not isinstance(gate_ref, str)
                    or round_record.get("pre_cosim_score_ref") != pre_score_ref
                    or round_record.get("cosim_gate_ref") != gate_ref
                ):
                    raise ValueError(
                        f"durable Candidate CoSim gate evidence is missing: {candidate_id}"
                    )
                candidate_metrics = _read_report(run_root, metrics_ref)
                gate_validation = CandidateValidation(
                    status="DONE",
                    stop_reason="CANDIDATE_SYNTH_VERIFIED",
                    validation={
                        str(key): dict(value)
                        for key, value in candidate_validation.items()
                        if isinstance(value, Mapping)
                    },
                    clock_constraint=dict(candidate_clock),
                    metrics_ref=metrics_ref,
                    budget={},
                )
                recomputed_pre_score = _score_for_cosim_gate(
                    candidate_id=candidate_id,
                    baseline_metrics=baseline_metrics,
                    candidate_metrics=candidate_metrics,
                    validation=gate_validation,
                    config=optimization_config.scoring,
                    proposal=proposal,
                    credits_used=int(
                        candidate_record.get("pre_cosim_credits_used", -1)
                    ),
                    difficulty=task.difficulty,
                    requires_cosim=task.requires_cosim,
                )
                stored_pre_score = _read_score(run_root, pre_score_ref)
                expected_gate = evaluate_exploration_cosim_gate(
                    recomputed_pre_score,
                    scores[best_id],
                    policy=optimization_config.exploration_cosim_policy,
                )
                try:
                    stored_gate = json.loads(
                        (run_root / gate_ref).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"durable Candidate CoSim gate is unreadable: {candidate_id}"
                    ) from exc
                if (
                    _canonical_digest(stored_pre_score.to_dict())
                    != _canonical_digest(recomputed_pre_score.to_dict())
                    or not isinstance(stored_gate, dict)
                    or _canonical_digest(stored_gate)
                    != _canonical_digest(expected_gate.to_dict())
                    or _canonical_digest(round_record.get("cosim_gate"))
                    != _canonical_digest(expected_gate.to_dict())
                ):
                    raise ValueError(
                        f"durable Candidate CoSim gate binding is invalid: {candidate_id}"
                    )
                cosim_status = (
                    cosim_record.get("status")
                    if isinstance(cosim_record, Mapping)
                    else None
                )
                if (
                    expected_gate.eligible and cosim_status == "NOT_RUN"
                ) or (
                    not expected_gate.eligible and cosim_status != "NOT_RUN"
                ):
                    raise ValueError(
                        f"durable Candidate CoSim gate action is invalid: {candidate_id}"
                    )
                if (
                    not expected_gate.eligible
                    and round_record.get("decision") != "REJECTED_NOT_BETTER"
                ):
                    raise ValueError(
                        f"durable Candidate CoSim gate decision is invalid: {candidate_id}"
                    )
            elif any(
                value is not None
                for value in (
                    pre_score_ref,
                    gate_ref,
                    round_record.get("pre_cosim_score_ref"),
                    round_record.get("cosim_gate_ref"),
                )
            ):
                raise ValueError(
                    f"durable rejected Candidate has premature CoSim gate evidence: {candidate_id}"
                )
            if isinstance(score_ref, str) and isinstance(metrics_ref, str):
                candidate_metrics = _read_report(run_root, metrics_ref)
                recovered_validation = CandidateValidation(
                    status="DONE",
                    stop_reason="CANDIDATE_VERIFIED",
                    validation={str(k): dict(v) for k, v in candidate_validation.items() if isinstance(v, Mapping)},
                    clock_constraint=dict(candidate_clock),
                    metrics_ref=metrics_ref,
                    budget={},
                )
                recomputed_score = _score_from_validation(
                    candidate_id=candidate_id,
                    baseline_metrics=baseline_metrics,
                    candidate_metrics=candidate_metrics,
                    validation=recovered_validation,
                    config=optimization_config.scoring,
                    proposal=proposal,
                    credits_used=int(candidate_record.get("credits_used", -1)),
                    difficulty=task.difficulty,
                    requires_cosim=task.requires_cosim,
                )
                stored_score = _read_score(run_root, score_ref)
                if (
                    _canonical_digest(stored_score.to_dict())
                    != _canonical_digest(recomputed_score.to_dict())
                    or round_record.get("score_ref") != score_ref
                ):
                    raise ValueError(
                        f"durable Candidate score binding is invalid: {candidate_id}"
                    )
                comparison_ref = candidate_record.get("comparison_ref")
                if not isinstance(comparison_ref, str):
                    raise ValueError(
                        f"durable Candidate comparison is missing: {candidate_id}"
                    )
                try:
                    stored_comparison = json.loads(
                        (run_root / comparison_ref).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"durable Candidate comparison is unreadable: {candidate_id}"
                    ) from exc
                expected_comparison = compare_scores(
                    recomputed_score, scores[best_id]
                ).to_dict()
                expected_round_decision = (
                    "PROMOTED"
                    if expected_comparison["strictly_better"] is True
                    else "REJECTED_NOT_BETTER"
                )
                if not isinstance(stored_comparison, dict) or (
                    _canonical_digest(stored_comparison)
                    != _canonical_digest(expected_comparison)
                ):
                    stored_value = (
                        stored_comparison
                        if isinstance(stored_comparison, dict)
                        else {}
                    )
                    differing = sorted(
                        key
                        for key in set(stored_value) | set(expected_comparison)
                        if _canonical_digest(stored_value.get(key))
                        != _canonical_digest(expected_comparison.get(key))
                    )
                    raise ValueError(
                        "durable Candidate comparison binding is invalid for fields "
                        f"{differing}: {candidate_id}; "
                        f"stored={stored_value.get('incumbent_key')}; "
                        f"expected={expected_comparison.get('incumbent_key')}"
                    )
                if (
                    _canonical_digest(round_record.get("comparison"))
                    != _canonical_digest(expected_comparison)
                ):
                    raise ValueError(
                        f"durable round comparison binding is invalid: {candidate_id}"
                    )
                if (
                    round_record.get("comparison_ref") != comparison_ref
                    or round_record.get("decision") != expected_round_decision
                ):
                    raise ValueError(
                        f"durable comparison decision binding is invalid: {candidate_id}"
                    )
                scores[candidate_id] = recomputed_score
                metrics_by_candidate[candidate_id] = candidate_metrics
                validation_by_candidate[candidate_id] = candidate_validation
                clock_by_candidate[candidate_id] = candidate_clock
            elif round_record.get("decision") != "REJECTED_VALIDATION":
                raise ValueError(
                    f"durable rejected Candidate binding is invalid: {candidate_id}"
                )
        else:
            expected_rejection = (
                "PATCH_REJECTED"
                if provider_value.get("ok") is True
                else "PROVIDER_REJECTED"
            )
            if round_record.get("decision") != expected_rejection:
                raise ValueError(
                    f"durable rejected round binding is invalid: {round_path.name}"
                )
            if expected_rejection == "PATCH_REJECTED":
                rejected_proposal = _proposal_from_dict(provider_value)
                try:
                    rejected_patch = normalize_unified_diff_headers(
                        rejected_proposal.patch
                    )
                    rejected_patch = relocate_unified_diff_hunks(
                        current_source,
                        rejected_patch,
                        kernel_name=task.kernel_name,
                    )
                    apply_unified_diff(
                        current_source,
                        rejected_patch,
                        kernel_name=task.kernel_name,
                        limits=optimization_config.patch_limits,
                    )
                except PatchValidationError:
                    pass
                else:
                    raise ValueError(
                        "durable Patch rejection binding is invalid: "
                        f"{round_path.name}"
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

    next_round_index = len(rounds) + 1
    if no_improvement >= optimization_config.max_no_improvement_rounds:
        exploration_stop_reason = "NO_IMPROVEMENT_LIMIT"
        next_round_index = optimization_config.max_rounds + 1

    for round_index in range(next_round_index, optimization_config.max_rounds + 1):
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
        selection_context = describe_optimization_selection(
            current_metrics,
            attempted=tuple(attempted),
            failures=tuple(failures),
        )
        if decision.optimization_class is None:
            exploration_stop_reason = decision.stop_reason or "NO_DISTINCT_OPTIMIZATION"
            break
        attempted.append((decision.optimization_class, decision.metrics_digest))
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
            current_validation=validation_by_candidate[best_id],
            current_clock_constraint=clock_by_candidate[best_id],
            source_excerpt=current_source.decode("utf-8"),
            hls_rules=_HLS_RULES[decision.optimization_class],
            failed_actions=tuple(
                {"optimization_class": item, "metrics_digest": digest}
                for item, digest in failures
                if item == decision.optimization_class
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
            difficulty=task.difficulty,
            official_score_enabled=optimization_config.scoring.official_score_enabled,
            current_official_score=scores[best_id].official_score,
        )
        try:
            proposal, provider_ref, request_ref, provider_error = (
                _call_optimization_provider(
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
            )
        except BudgetExceeded:
            exploration_stop_reason = "TOKEN_RESERVE_REACHED"
            break
        round_record: dict[str, object] = {
            "round_index": round_index,
            "parent_candidate_id": best_id,
            "optimization_class": decision.optimization_class,
            "selector": asdict(decision),
            "selection_context": selection_context.to_dict(),
            "context_digest": _canonical_digest(
                _stable_context_value(context.to_dict())
            ),
            "provider_ref": provider_ref,
            "request_ref": request_ref,
            "candidate_id": None,
            "decision": "PROVIDER_REJECTED",
        }
        if proposal is None:
            round_record["error"] = provider_error
            failures.append((decision.optimization_class, decision.metrics_digest))
            no_improvement += 1
        else:
            normalized = normalize_unified_diff_headers(proposal.patch)
            try:
                normalized = relocate_unified_diff_hunks(
                    current_source,
                    normalized,
                    kernel_name=task.kernel_name,
                )
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
                            "llm_request_ref": request_ref,
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
                preliminary = validate_candidate(
                    task,
                    application.patched_bytes,
                    candidate_id,
                    run_root,
                    run_config,
                    backend=backend,
                    validation_scope="exploration",
                    run_cosim=False,
                )
                validation = preliminary
                pre_cosim_credits = (
                    int(preliminary.budget["credits_used"]) - before_credits
                )
                candidate_metrics: dict[str, object] | None = None
                pre_cosim_score: CandidateScore | None = None
                cosim_gate: ExplorationCosimGate | None = None
                score: CandidateScore | None = None
                comparison = None
                score_ref: str | None = None
                comparison_ref: str | None = None
                if preliminary.status == "DONE" and preliminary.metrics_ref is not None:
                    candidate_metrics = _read_report(
                        run_root, preliminary.metrics_ref
                    )
                    pre_cosim_score = _score_for_cosim_gate(
                        candidate_id=candidate_id,
                        baseline_metrics=baseline_metrics,
                        candidate_metrics=candidate_metrics,
                        validation=preliminary,
                        config=optimization_config.scoring,
                        proposal=proposal,
                        credits_used=pre_cosim_credits,
                        difficulty=task.difficulty,
                        requires_cosim=task.requires_cosim,
                    )
                    cosim_gate = evaluate_exploration_cosim_gate(
                        pre_cosim_score,
                        scores[best_id],
                        policy=optimization_config.exploration_cosim_policy,
                    )
                    pre_cosim_score_ref = f"scores/{candidate_id}.pre_cosim.json"
                    cosim_gate_ref = f"cosim_gates/{candidate_id}.json"
                    _atomic_json(
                        run_root / pre_cosim_score_ref,
                        pre_cosim_score.to_dict(),
                    )
                    _atomic_json(run_root / cosim_gate_ref, cosim_gate.to_dict())
                    round_record["pre_cosim_score_ref"] = pre_cosim_score_ref
                    round_record["cosim_gate_ref"] = cosim_gate_ref
                    round_record["cosim_gate"] = cosim_gate.to_dict()
                    _append_trace(
                        run_root / "trace.jsonl",
                        "V2_COSIM_GATE_EVALUATED",
                        round_index=round_index,
                        candidate_id=candidate_id,
                        incumbent_id=best_id,
                        eligible=cosim_gate.eligible,
                        reason=cosim_gate.reason,
                        result_ref=cosim_gate_ref,
                    )
                    if cosim_gate.eligible:
                        validation = complete_candidate_cosim(
                            task,
                            application.patched_bytes,
                            candidate_id,
                            run_root,
                            run_config,
                            preliminary,
                            backend=backend,
                            validation_scope="exploration",
                        )
                candidate_credits = int(validation.budget["credits_used"]) - before_credits
                if validation.status == "DONE" and candidate_metrics is not None:
                    score = _score_from_validation(
                        candidate_id=candidate_id,
                        baseline_metrics=baseline_metrics,
                        candidate_metrics=candidate_metrics,
                        validation=validation,
                        config=optimization_config.scoring,
                        proposal=proposal,
                        credits_used=candidate_credits,
                        difficulty=task.difficulty,
                        requires_cosim=task.requires_cosim,
                    )
                    score_ref = f"scores/{candidate_id}.json"
                    _atomic_json(run_root / score_ref, score.to_dict())
                    comparison = compare_scores(score, scores[best_id])
                    comparison_ref = f"comparisons/{candidate_id}.json"
                    _atomic_json(run_root / comparison_ref, comparison.to_dict())
                with _RunLock(run_root):
                    registry = manager.load_registry()
                    candidates = registry["candidates"]
                    candidate = candidates[candidate_id]
                    candidate["validation"] = validation.validation
                    candidate["clock_constraint"] = validation.clock_constraint
                    candidate["metrics_ref"] = validation.metrics_ref
                    candidate["credits_used"] = candidate_credits
                    if pre_cosim_score is not None and cosim_gate is not None:
                        candidate["pre_cosim_credits_used"] = pre_cosim_credits
                        candidate["pre_cosim_score_ref"] = round_record[
                            "pre_cosim_score_ref"
                        ]
                        candidate["cosim_gate_ref"] = round_record["cosim_gate_ref"]
                    if validation.status != "DONE" or validation.metrics_ref is None:
                        candidate["status"] = "REJECTED_VALIDATION"
                        candidate["rejection_reason"] = validation.stop_reason
                        registry["active_candidate_id"] = best_id
                        round_record["decision"] = "REJECTED_VALIDATION"
                        round_record["stop_reason"] = validation.stop_reason
                        failures.append((decision.optimization_class, decision.metrics_digest))
                        no_improvement += 1
                    else:
                        if (
                            candidate_metrics is None
                            or score is None
                            or comparison is None
                            or score_ref is None
                            or comparison_ref is None
                            or cosim_gate is None
                        ):
                            raise ValueError("completed exploration Candidate lacks gate evidence")
                        candidate["score_ref"] = score_ref
                        candidate["comparison_ref"] = comparison_ref
                        scores[candidate_id] = score
                        metrics_by_candidate[candidate_id] = candidate_metrics
                        validation_by_candidate[candidate_id] = validation.validation
                        clock_by_candidate[candidate_id] = validation.clock_constraint
                        round_record["score_ref"] = score_ref
                        round_record["comparison_ref"] = comparison_ref
                        round_record["comparison"] = comparison.to_dict()
                        if cosim_gate.eligible and comparison.strictly_better:
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
                            candidate["rejection_reason"] = (
                                comparison.reason
                                if cosim_gate.eligible
                                else cosim_gate.reason
                            )
                            registry["active_candidate_id"] = best_id
                            round_record["decision"] = "REJECTED_NOT_BETTER"
                            failures.append(
                                (
                                    decision.optimization_class,
                                    decision.metrics_digest,
                                )
                            )
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
        for fallback_score in eligible[: optimization_config.max_final_attempts - 1]:
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
            registry["final_candidate_id"] = None
        manager.save_registry(registry)
    final_budget = budget.snapshot()
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
        "budget": final_budget,
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
        credits_used=final_budget["credits_used"],
        tokens_used=final_budget["tokens_used"],
        result_ref="v2_result.json",
    )
    stored_result = json.loads(
        (run_root / "v2_result.json").read_text(encoding="utf-8")
    )
    if not isinstance(stored_result, dict):
        raise ValueError("stored V2 result is not an object")
    _finalize_v2_artifacts(run_root, stored_result, registry)
    return stored_result
