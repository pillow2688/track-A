"""Runnable V3-A0 vertical prototype built from action-level LangGraph nodes.

This module is intentionally parallel to V2.  It proves the smallest useful
graph closure (baseline -> one scripted Candidate -> gated CoSim -> final
validation -> team report) without changing the production V2 entry point.
"""

from __future__ import annotations

import hashlib
import json
import math
import operator
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Mapping, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .budget import BudgetLedger
from .candidate import CandidateManager
from .optimization import evaluate_exploration_cosim_gate, _read_report
from .repair import PatchLimits, PatchProposal, apply_unified_diff
from .scoring import (
    OFFICIAL_SCORE_SOURCE,
    CandidateScore,
    ScoringConfig,
    estimate_official_score_proxy,
    load_scoring_config,
    score_candidate,
)
from .task import PublicTask
from .tools import BackendResult, ToolBackend, ToolResult, ToolServer
from .vitis import VitisBackend
from .workflow import (
    RunConfig,
    _RunLock,
    _append_trace,
    _atomic_json,
    _create_or_load_registry,
    _initial_validation,
    _invoke_stage,
    _snapshot_baseline,
    _task_spec,
    _validation_record,
    _write_once_or_verify,
)


WORKFLOW_NAME = "V3A0_LANGGRAPH_PROTOTYPE"
STATE_SCHEMA_VERSION = 1
_FULL_CLOSURE_CALLS = {"csim": 1, "synth": 1, "cosim": 1}


class V3PrototypeState(TypedDict, total=False):
    task_id: str
    run_dir: str
    phase: str
    status: str
    stop_reason: str
    baseline_candidate_id: str
    active_candidate_id: str
    best_candidate_id: str
    final_attempt_candidate_id: str | None
    final_candidate_id: str | None
    planner_ref: str
    baseline_metrics_ref: str
    candidate_metrics_ref: str
    final_metrics_ref: str
    baseline_clock: dict[str, object]
    candidate_clock: dict[str, object]
    final_clock: dict[str, object]
    final_validation: dict[str, dict[str, object]]
    baseline_score_ref: str
    candidate_score_ref: str
    final_score_ref: str
    cosim_gate: dict[str, object]
    budget_gate: dict[str, object]
    last_tool_ok: bool
    last_tool_phase: str
    last_tool_reason: str
    node_events: Annotated[list[dict[str, object]], operator.add]
    result_ref: str
    report_ref: str


@dataclass(frozen=True)
class _Runtime:
    task: PublicTask
    run_root: Path
    config: RunConfig
    proposal: PatchProposal
    backend: ToolBackend
    scoring: ScoringConfig
    patch_limits: PatchLimits
    thread_id: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _backend_fingerprint(backend: ToolBackend) -> str:
    explicit = getattr(backend, "fingerprint", None)
    if callable(explicit):
        value = str(explicit())
    else:
        value = f"{type(backend).__module__}.{type(backend).__qualname__}"
    if not value:
        raise ValueError("backend fingerprint must not be empty")
    return value


def _run_config_snapshot(runtime: _Runtime) -> dict[str, object]:
    value = runtime.config.to_dict()
    value.update(
        {
            "workflow": WORKFLOW_NAME,
            "prototype": True,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "thread_id": runtime.thread_id,
            "backend_fingerprint": _backend_fingerprint(runtime.backend),
            "proposal_sha256": _sha256_json(runtime.proposal.to_dict()),
            "scoring": runtime.scoring.to_dict(),
            "patch_limits": {
                "max_changed_lines": runtime.patch_limits.max_changed_lines,
                "max_hunks": runtime.patch_limits.max_hunks,
                "allow_full_file_replacement": (
                    runtime.patch_limits.allow_full_file_replacement
                ),
            },
        }
    )
    return value


def _proposal_snapshot(runtime: _Runtime) -> dict[str, object]:
    value = runtime.proposal.to_dict()
    value["required_validation"] = list(runtime.proposal.required_validation)
    return value | {
        "parent_candidate_id": "candidate_000",
        "planner_mode": "scripted_prototype",
    }


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read durable {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"durable {path.name} is not a JSON object")
    return value


def _verify_run_identity(runtime: _Runtime) -> None:
    task_path = runtime.run_root / "v3_task_spec.json"
    config_path = runtime.run_root / "v3_run_config.json"
    if not task_path.exists() and not config_path.exists():
        return
    expected = {
        task_path: _task_spec(runtime.task),
        config_path: _run_config_snapshot(runtime),
    }
    proposal_path = runtime.run_root / "planner" / "proposal_001.json"
    if proposal_path.exists():
        expected[proposal_path] = _proposal_snapshot(runtime)
    for path, wanted in expected.items():
        if not path.is_file() or _read_json_object(path) != wanted:
            raise RuntimeError(
                f"existing run identity mismatch: "
                f"{path.relative_to(runtime.run_root)}"
            )


def _load_terminal_result(runtime: _Runtime) -> dict[str, object] | None:
    result_path = runtime.run_root / "v3_prototype_result.json"
    if not result_path.exists():
        return None
    _verify_run_identity(runtime)
    result = _read_json_object(result_path)
    backend = result.get("backend")
    if (
        result.get("workflow") != WORKFLOW_NAME
        or result.get("task_id") != runtime.task.id
        or result.get("status") not in {"DONE", "FAILED"}
        or not isinstance(backend, Mapping)
        or backend.get("fingerprint") != _backend_fingerprint(runtime.backend)
    ):
        raise RuntimeError("terminal V3 prototype result has an invalid identity")
    report_path = runtime.run_root / "v3_team_report.md"
    expected_report = _render_team_report(runtime, result)
    try:
        stored_report = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        stored_report = ""
    if stored_report != expected_report:
        _atomic_text(report_path, expected_report)
    return result


def _budget_affordability(
    runtime: _Runtime,
    *,
    required_calls: Mapping[str, int],
    policy: str,
) -> dict[str, object]:
    snapshot = BudgetLedger(
        runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
    ).snapshot()
    required_credits = sum(
        int(runtime.config.budget.costs[kind]) * int(count)
        for kind, count in required_calls.items()
    )
    blockers: list[str] = []
    remaining = snapshot["credits_remaining"]
    if remaining is not None and int(remaining) < required_credits:
        blockers.append(
            f"credits:{remaining}<{required_credits}"
        )
    used = snapshot["tool_used"]
    pending = snapshot["tool_pending"]
    for kind, count in required_calls.items():
        limit = runtime.config.budget.tool_limits[kind]
        if limit is None:
            continue
        available = int(limit) - int(used[kind]) - int(pending[kind])  # type: ignore[index]
        if available < int(count):
            blockers.append(f"{kind}_calls:{available}<{count}")
    if float(snapshot["runtime_remaining_seconds"]) <= 0:
        blockers.append("runtime_exhausted")
    return {
        "policy": policy,
        "allowed": not blockers,
        "required_calls": dict(required_calls),
        "required_credits": required_credits,
        "credits_remaining": remaining,
        "blockers": blockers,
    }


def _server(runtime: _Runtime) -> tuple[BudgetLedger, ToolServer]:
    budget = BudgetLedger(
        runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
    )
    server = ToolServer(
        task=runtime.task,
        budget=budget,
        run_root=runtime.run_root,
        config=runtime.config.tool,
        backend=runtime.backend,
    )
    return budget, server


def _event(
    runtime: _Runtime,
    *,
    node: str,
    phase: str,
    candidate_id: str | None,
    action: str,
    why: str,
    outcome: str,
    result_ref: str | None = None,
    cached: bool | None = None,
) -> dict[str, object]:
    budget = BudgetLedger(
        runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
    ).snapshot()
    event: dict[str, object] = {
        "timestamp": _utc_now(),
        "node": node,
        "phase": phase,
        "candidate_id": candidate_id,
        "action": action,
        "why": why,
        "outcome": outcome,
        "credits_used": budget["credits_used"],
        "tokens_used": budget["tokens_used"],
    }
    if result_ref is not None:
        event["result_ref"] = result_ref
    if cached is not None:
        event["cached"] = cached
    _append_trace(
        runtime.run_root / "trace.jsonl",
        "V3_NODE_COMPLETED",
        **event,
    )
    return event


def _clock_constraint(
    report: Mapping[str, object] | None, minimum_frequency_mhz: float
) -> dict[str, object]:
    maximum = 1000.0 / minimum_frequency_mhz
    estimated = (
        report.get("estimated_clock_period_ns") if isinstance(report, Mapping) else None
    )
    valid = (
        not isinstance(estimated, bool)
        and isinstance(estimated, (int, float))
        and math.isfinite(float(estimated))
        and float(estimated) > 0
    )
    return {
        "minimum_frequency_mhz": minimum_frequency_mhz,
        "maximum_period_ns": maximum,
        "estimated_period_ns": estimated,
        "passed": bool(valid and float(estimated) <= maximum),
    }


def _candidate_source(runtime: _Runtime, candidate_id: str) -> bytes:
    registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
    candidates = registry.get("candidates")
    record = candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    if not isinstance(record, Mapping):
        raise ValueError(f"Candidate is missing: {candidate_id}")
    path = runtime.run_root / str(record.get("source_ref", ""))
    return path.read_bytes()


def _save_validation(
    runtime: _Runtime,
    candidate_id: str,
    stage: str,
    record: Mapping[str, object],
    *,
    metrics_ref: str | None = None,
) -> None:
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    candidates = registry["candidates"]
    candidate = candidates[candidate_id]
    validation = candidate.get("validation")
    if not isinstance(validation, dict):
        validation = _initial_validation()
        candidate["validation"] = validation
    validation[stage] = dict(record)
    candidate["status"] = "EVALUATING"
    if metrics_ref is not None:
        candidate["metrics_ref"] = metrics_ref
    candidate["credits_used"] = sum(
        int(runtime.config.budget.costs[name])
        for name in ("csim", "synth", "cosim")
        if isinstance(validation.get(name), Mapping)
        and isinstance(validation[name].get("action_id"), str)
    )
    manager.save_registry(registry)


def _run_tool(
    runtime: _Runtime,
    *,
    node: str,
    phase: str,
    candidate_id: str,
    stage: str,
    validation_scope: str,
) -> tuple[ToolResult | None, dict[str, object], dict[str, object]]:
    _budget, server = _server(runtime)
    result, error, reason = _invoke_stage(
        server,
        stage,
        _candidate_source(runtime, candidate_id),
        candidate_id=candidate_id,
        validation_scope=validation_scope,
    )
    if result is None:
        record = dict(error or {
            "status": "TOOL_ERROR",
            "phase": "tool_error",
            "ok": False,
        })
        record["failure_reason"] = reason or f"{stage.upper()}_ERROR"
        event = _event(
            runtime,
            node=node,
            phase=phase,
            candidate_id=candidate_id,
            action=stage,
            why=f"{stage.upper()} is the next correctness/performance gate.",
            outcome=reason or f"{stage.upper()}_ERROR",
        )
        return None, record, event
    record = _validation_record(result) | {
        "validation_scope": validation_scope,
    }
    event = _event(
        runtime,
        node=node,
        phase=phase,
        candidate_id=candidate_id,
        action=stage,
        why=f"{stage.upper()} is the next correctness/performance gate.",
        outcome="PASS" if result.ok else result.phase.upper(),
        result_ref=result.result_ref,
        cached=result.cached,
    )
    return result, record, event


def _registry_validation(
    runtime: _Runtime, candidate_id: str
) -> dict[str, dict[str, object]]:
    registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
    candidate = registry["candidates"][candidate_id]
    validation = candidate.get("validation")
    if not isinstance(validation, Mapping):
        raise ValueError(f"Candidate {candidate_id} has no validation state")
    return {
        stage: dict(record) if isinstance(record, Mapping) else {"status": "NOT_RUN"}
        for stage, record in validation.items()
    }


def _score(
    runtime: _Runtime,
    *,
    candidate_id: str,
    baseline_metrics: Mapping[str, object],
    candidate_metrics: Mapping[str, object],
    validation: Mapping[str, object],
    clock: Mapping[str, object],
    proposal: PatchProposal | None,
    provisional_cosim: bool,
) -> CandidateScore:
    scoring = (
        replace(runtime.scoring, required_verification_tier=3)
        if provisional_cosim
        else runtime.scoring
    )
    official_score = (
        estimate_official_score_proxy(
            difficulty=runtime.task.difficulty,
            baseline=baseline_metrics,
            candidate=candidate_metrics,
            validation=validation,
            requires_cosim=runtime.task.requires_cosim,
            provisional_cosim=provisional_cosim,
        )
        if scoring.official_score_enabled
        else None
    )
    credits = sum(
        int(runtime.config.budget.costs[stage])
        for stage in ("csim", "synth", "cosim")
        if isinstance(validation.get(stage), Mapping)
        and isinstance(validation[stage].get("action_id"), str)
    )
    return score_candidate(
        candidate_id=candidate_id,
        baseline=baseline_metrics,
        candidate=candidate_metrics,
        validation=validation,
        clock=clock,
        config=scoring,
        input_tokens=proposal.input_tokens if proposal is not None else 0,
        output_tokens=proposal.output_tokens if proposal is not None else 0,
        cached_input_tokens=(
            proposal.cached_input_tokens if proposal is not None else 0
        ),
        credits_used=credits,
        official_score=official_score,
        official_score_source=(
            OFFICIAL_SCORE_SOURCE if official_score is not None else None
        ),
    )


def _initialize(runtime: _Runtime, _state: V3PrototypeState) -> V3PrototypeState:
    runtime.run_root.mkdir(parents=True, exist_ok=True)
    _write_once_or_verify(runtime.run_root / "v3_task_spec.json", _task_spec(runtime.task))
    _write_once_or_verify(
        runtime.run_root / "v3_run_config.json", _run_config_snapshot(runtime)
    )
    _baseline_path, baseline_ref = _snapshot_baseline(runtime.task, runtime.run_root)
    registry = _create_or_load_registry(runtime.task, runtime.run_root, baseline_ref)
    BudgetLedger(runtime.run_root / "budget_ledger.jsonl", runtime.config.budget)
    budget_gate = _budget_affordability(
        runtime,
        required_calls={kind: count * 2 for kind, count in _FULL_CLOSURE_CALLS.items()},
        policy="baseline_plus_final_closure",
    )
    if (
        runtime.proposal.input_tokens
        or runtime.proposal.output_tokens
        or runtime.proposal.cached_input_tokens
    ):
        budget_gate["allowed"] = False
        budget_gate["blockers"] = [
            *budget_gate["blockers"],  # type: ignore[index]
            "scripted_proposal_tokens_must_be_zero",
        ]
    allowed = budget_gate["allowed"] is True
    event = _event(
        runtime,
        node="initialize",
        phase="INIT",
        candidate_id="candidate_000",
        action="load_task_and_initialize_run",
        why="Create immutable public-task, budget, Candidate and checkpoint boundaries.",
        outcome="READY" if allowed else "BASELINE_AND_FINAL_CLOSURE_UNAFFORDABLE",
    )
    return {
        "task_id": runtime.task.id,
        "run_dir": str(runtime.run_root),
        "phase": "BASELINE",
        "status": "RUNNING" if allowed else "FAILED",
        "stop_reason": (
            "RUNNING" if allowed else "BASELINE_AND_FINAL_CLOSURE_UNAFFORDABLE"
        ),
        "baseline_candidate_id": "candidate_000",
        "best_candidate_id": str(registry.get("best_candidate_id") or "candidate_000"),
        "final_attempt_candidate_id": None,
        "final_candidate_id": None,
        "final_validation": _initial_validation(),
        "cosim_gate": {
            "eligible": False,
            "reason": "NOT_EVALUATED",
        },
        "budget_gate": budget_gate,
        "last_tool_ok": allowed,
        "node_events": [event],
    }


def _baseline_csim(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["baseline_candidate_id"]
    result, record, event = _run_tool(
        runtime,
        node="baseline_csim",
        phase="BASELINE",
        candidate_id=candidate_id,
        stage="csim",
        validation_scope="exploration",
    )
    _save_validation(runtime, candidate_id, "csim", record)
    return {
        "last_tool_ok": bool(result is not None and result.ok),
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "status": "RUNNING" if result is not None and result.ok else "FAILED",
        "stop_reason": (
            "RUNNING"
            if result is not None and result.ok
            else f"BASELINE_CSIM_{str(record.get('phase', 'error')).upper()}"
        ),
        "node_events": [event],
    }


def _baseline_synth(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["baseline_candidate_id"]
    result, record, event = _run_tool(
        runtime,
        node="baseline_synth",
        phase="BASELINE",
        candidate_id=candidate_id,
        stage="synth",
        validation_scope="exploration",
    )
    metrics_ref = result.result_ref if result is not None and result.report is not None else None
    _save_validation(runtime, candidate_id, "synth", record, metrics_ref=metrics_ref)
    clock = _clock_constraint(
        result.report if result is not None else None,
        runtime.config.minimum_frequency_mhz,
    )
    ok = bool(result is not None and result.ok and metrics_ref and clock["passed"])
    return {
        "baseline_metrics_ref": metrics_ref or "",
        "baseline_clock": clock,
        "last_tool_ok": ok,
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "status": "RUNNING" if ok else "FAILED",
        "stop_reason": "RUNNING" if ok else "BASELINE_SYNTH_OR_CLOCK_FAILED",
        "best_candidate_id": candidate_id,
        "node_events": [event],
    }


def _baseline_cosim(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["baseline_candidate_id"]
    result, record, event = _run_tool(
        runtime,
        node="baseline_cosim",
        phase="BASELINE",
        candidate_id=candidate_id,
        stage="cosim",
        validation_scope="exploration",
    )
    _save_validation(runtime, candidate_id, "cosim", record)
    ok = bool(result is not None and result.ok)
    if ok:
        manager = CandidateManager(runtime.run_root, runtime.task)
        registry = manager.load_registry()
        registry["best_candidate_id"] = candidate_id
        registry["candidates"][candidate_id]["status"] = "BASELINE_VERIFIED"
        manager.save_registry(registry)
    return {
        "last_tool_ok": ok,
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "status": "RUNNING" if ok else "FAILED",
        "stop_reason": "RUNNING" if ok else "BASELINE_COSIM_FAILED",
        "best_candidate_id": candidate_id,
        "node_events": [event],
    }


def _evaluate_round_budget(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    required = {kind: count * 2 for kind, count in _FULL_CLOSURE_CALLS.items()}
    gate = _budget_affordability(
        runtime,
        required_calls=required,
        policy="candidate_exploration_plus_final_closure",
    )
    allowed = gate["allowed"] is True
    reason = "ROUND_BUDGET_AVAILABLE" if allowed else "ROUND_SKIPPED_FINAL_RESERVE"
    event = _event(
        runtime,
        node="evaluate_round_budget",
        phase="OPTIMIZE",
        candidate_id=state["best_candidate_id"],
        action="reserve_candidate_and_final_validation_budget",
        why=(
            "Start optimization only when one full Candidate check and final "
            "closure remain affordable."
        ),
        outcome=reason,
    )
    update: V3PrototypeState = {
        "budget_gate": gate,
        "last_tool_ok": allowed,
        "node_events": [event],
    }
    if not allowed:
        update["cosim_gate"] = {"eligible": False, "reason": reason}
    return update


def _plan_candidate(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    proposal_ref = "planner/proposal_001.json"
    if state["best_candidate_id"] != "candidate_000":
        raise RuntimeError("the one-Candidate prototype must plan from candidate_000")
    value = _proposal_snapshot(runtime)
    _write_once_or_verify(runtime.run_root / proposal_ref, value)
    event = _event(
        runtime,
        node="plan_candidate",
        phase="OPTIMIZE",
        candidate_id=state["best_candidate_id"],
        action="scripted_planner_proposal",
        why=runtime.proposal.hypothesis or "Apply the configured prototype Patch.",
        outcome="PROPOSAL_READY",
        result_ref=proposal_ref,
    )
    return {
        "phase": "OPTIMIZE",
        "planner_ref": proposal_ref,
        "node_events": [event],
    }


def _materialize_candidate(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    parent_id = state["best_candidate_id"]
    source = _candidate_source(runtime, parent_id)
    application = apply_unified_diff(
        source,
        runtime.proposal.patch,
        kernel_name=runtime.task.kernel_name,
        limits=runtime.patch_limits,
    )
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    materialized = manager.materialize(
        registry,
        parent_id=parent_id,
        patch_text=runtime.proposal.patch,
        application=application,
        kind="optimization",
        metadata={
            "planner_ref": state["planner_ref"],
            "provider": runtime.proposal.provider,
            "model": runtime.proposal.model,
            "hypothesis": runtime.proposal.hypothesis,
            "expected_effect": runtime.proposal.expected_effect,
            "risk": runtime.proposal.risk,
            "required_validation": list(runtime.proposal.required_validation),
            "input_tokens": runtime.proposal.input_tokens,
            "output_tokens": runtime.proposal.output_tokens,
        },
    )
    event = _event(
        runtime,
        node="materialize_candidate",
        phase="OPTIMIZE",
        candidate_id=materialized.candidate_id,
        action="validate_patch_and_create_immutable_candidate",
        why="The planner may propose code, but only the Candidate manager may materialize it.",
        outcome="MATERIALIZED",
        result_ref=f"candidates/{materialized.candidate_id}/candidate.json",
    )
    return {
        "active_candidate_id": materialized.candidate_id,
        "node_events": [event],
    }


def _candidate_csim(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["active_candidate_id"]
    result, record, event = _run_tool(
        runtime,
        node="candidate_csim",
        phase="OPTIMIZE",
        candidate_id=candidate_id,
        stage="csim",
        validation_scope="exploration",
    )
    _save_validation(runtime, candidate_id, "csim", record)
    ok = bool(result is not None and result.ok)
    update: V3PrototypeState = {
        "last_tool_ok": ok,
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "node_events": [event],
    }
    if not ok:
        update["cosim_gate"] = {
            "eligible": False,
            "reason": "CANDIDATE_CSIM_FAILED",
        }
    return update


def _candidate_synth(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["active_candidate_id"]
    result, record, event = _run_tool(
        runtime,
        node="candidate_synth",
        phase="OPTIMIZE",
        candidate_id=candidate_id,
        stage="synth",
        validation_scope="exploration",
    )
    metrics_ref = result.result_ref if result is not None and result.report is not None else None
    _save_validation(runtime, candidate_id, "synth", record, metrics_ref=metrics_ref)
    clock = _clock_constraint(
        result.report if result is not None else None,
        runtime.config.minimum_frequency_mhz,
    )
    ok = bool(result is not None and result.ok and metrics_ref and clock["passed"])
    update: V3PrototypeState = {
        "candidate_metrics_ref": metrics_ref or "",
        "candidate_clock": clock,
        "last_tool_ok": ok,
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "node_events": [event],
    }
    if not ok:
        update["cosim_gate"] = {
            "eligible": False,
            "reason": "CANDIDATE_SYNTH_OR_CLOCK_FAILED",
        }
    return update


def _candidate_score_gate(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    baseline_id = state["baseline_candidate_id"]
    candidate_id = state["active_candidate_id"]
    baseline_metrics = _read_report(runtime.run_root, state["baseline_metrics_ref"])
    candidate_metrics = _read_report(runtime.run_root, state["candidate_metrics_ref"])
    baseline_score = _score(
        runtime,
        candidate_id=baseline_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=baseline_metrics,
        validation=_registry_validation(runtime, baseline_id),
        clock=state["baseline_clock"],
        proposal=None,
        provisional_cosim=False,
    )
    candidate_score = _score(
        runtime,
        candidate_id=candidate_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        validation=_registry_validation(runtime, candidate_id),
        clock=state["candidate_clock"],
        proposal=runtime.proposal,
        provisional_cosim=True,
    )
    baseline_score_ref = f"scores/{baseline_id}.pre_cosim.json"
    candidate_score_ref = f"scores/{candidate_id}.pre_cosim.json"
    _atomic_json(runtime.run_root / baseline_score_ref, baseline_score.to_dict())
    _atomic_json(runtime.run_root / candidate_score_ref, candidate_score.to_dict())
    policy = (
        "official_score_gate"
        if runtime.scoring.official_score_enabled
        else "ppa_gate"
    )
    gate = evaluate_exploration_cosim_gate(
        candidate_score,
        baseline_score,
        policy=policy,
    )
    event = _event(
        runtime,
        node="candidate_score_gate",
        phase="OPTIMIZE",
        candidate_id=candidate_id,
        action="compare_pre_cosim_score",
        why="Spend 20 CoSim credits only when CSim+Synth evidence beats the incumbent.",
        outcome=gate.reason,
        result_ref=candidate_score_ref,
    )
    return {
        "baseline_score_ref": baseline_score_ref,
        "candidate_score_ref": candidate_score_ref,
        "cosim_gate": gate.to_dict(),
        "node_events": [event],
    }


def _candidate_cosim_budget_gate(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    required = dict(_FULL_CLOSURE_CALLS)
    required["cosim"] += 1
    gate = _budget_affordability(
        runtime,
        required_calls=required,
        policy="candidate_cosim_plus_final_closure",
    )
    allowed = gate["allowed"] is True
    reason = (
        "CANDIDATE_COSIM_BUDGET_AVAILABLE"
        if allowed
        else "CANDIDATE_COSIM_SKIPPED_FINAL_RESERVE"
    )
    event = _event(
        runtime,
        node="candidate_cosim_budget_gate",
        phase="OPTIMIZE",
        candidate_id=state["active_candidate_id"],
        action="protect_final_closure_before_candidate_cosim",
        why=(
            "The 20-credit exploration CoSim may run only after preserving a "
            "complete final validation."
        ),
        outcome=reason,
    )
    update: V3PrototypeState = {
        "budget_gate": gate,
        "last_tool_ok": allowed,
        "node_events": [event],
    }
    if not allowed:
        update["cosim_gate"] = {
            **state.get("cosim_gate", {}),
            "eligible": False,
            "reason": reason,
        }
    return update


def _candidate_cosim(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["active_candidate_id"]
    result, record, event = _run_tool(
        runtime,
        node="candidate_cosim",
        phase="OPTIMIZE",
        candidate_id=candidate_id,
        stage="cosim",
        validation_scope="exploration",
    )
    _save_validation(runtime, candidate_id, "cosim", record)
    ok = bool(result is not None and result.ok)
    update: V3PrototypeState = {
        "last_tool_ok": ok,
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "node_events": [event],
    }
    if not ok:
        update["cosim_gate"] = {
            **state.get("cosim_gate", {}),
            "eligible": False,
            "reason": "CANDIDATE_COSIM_FAILED",
        }
    return update


def _promote_candidate(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    candidate_id = state["active_candidate_id"]
    baseline_metrics = _read_report(runtime.run_root, state["baseline_metrics_ref"])
    candidate_metrics = _read_report(runtime.run_root, state["candidate_metrics_ref"])
    score = _score(
        runtime,
        candidate_id=candidate_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        validation=_registry_validation(runtime, candidate_id),
        clock=state["candidate_clock"],
        proposal=runtime.proposal,
        provisional_cosim=False,
    )
    score_ref = f"scores/{candidate_id}.verified.json"
    _atomic_json(runtime.run_root / score_ref, score.to_dict())
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    registry["best_candidate_id"] = candidate_id
    registry["active_candidate_id"] = None
    candidate = registry["candidates"][candidate_id]
    candidate["status"] = "PROMOTED"
    candidate["score_ref"] = score_ref
    manager.save_registry(registry)
    event = _event(
        runtime,
        node="promote_candidate",
        phase="DECIDE",
        candidate_id=candidate_id,
        action="promote_verified_candidate",
        why=str(state["cosim_gate"].get("reason", "STRICT_IMPROVEMENT")),
        outcome="PROMOTED",
        result_ref=score_ref,
    )
    return {
        "phase": "FINAL",
        "best_candidate_id": candidate_id,
        "final_attempt_candidate_id": candidate_id,
        "final_candidate_id": None,
        "candidate_score_ref": score_ref,
        "final_validation": _initial_validation(),
        "node_events": [event],
    }


def _select_baseline(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    baseline_id = state["baseline_candidate_id"]
    candidate_id = state.get("active_candidate_id")
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    registry["best_candidate_id"] = baseline_id
    registry["active_candidate_id"] = None
    if candidate_id and candidate_id in registry["candidates"]:
        registry["candidates"][candidate_id]["status"] = "REJECTED"
    manager.save_registry(registry)
    reason = str(state.get("cosim_gate", {}).get("reason", "CANDIDATE_VALIDATION_FAILED"))
    event = _event(
        runtime,
        node="select_baseline",
        phase="DECIDE",
        candidate_id=baseline_id,
        action="reject_candidate_and_select_incumbent",
        why=reason,
        outcome="BASELINE_SELECTED",
    )
    return {
        "phase": "FINAL",
        "best_candidate_id": baseline_id,
        "final_attempt_candidate_id": baseline_id,
        "final_candidate_id": None,
        "final_validation": _initial_validation(),
        "node_events": [event],
    }


def _evaluate_final_budget(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    gate = _budget_affordability(
        runtime,
        required_calls=_FULL_CLOSURE_CALLS,
        policy="final_closure",
    )
    allowed = gate["allowed"] is True
    event = _event(
        runtime,
        node="evaluate_final_budget",
        phase="FINAL",
        candidate_id=state.get("final_attempt_candidate_id"),
        action="authorize_full_final_validation",
        why="Finalization requires a fresh CSim, Synth and CoSim closure.",
        outcome="FINAL_BUDGET_AVAILABLE" if allowed else "FINAL_CLOSURE_UNAFFORDABLE",
    )
    return {
        "budget_gate": gate,
        "last_tool_ok": allowed,
        "status": "RUNNING" if allowed else "FAILED",
        "stop_reason": "RUNNING" if allowed else "FINAL_CLOSURE_UNAFFORDABLE",
        "node_events": [event],
    }


def _final_stage(
    runtime: _Runtime,
    state: V3PrototypeState,
    *,
    stage: str,
) -> V3PrototypeState:
    candidate_id = state["final_attempt_candidate_id"]
    if candidate_id is None:
        raise RuntimeError("final validation has no selected attempt")
    node = f"final_{stage}"
    result, record, event = _run_tool(
        runtime,
        node=node,
        phase="FINAL",
        candidate_id=candidate_id,
        stage=stage,
        validation_scope="final",
    )
    validation = {
        name: dict(value)
        for name, value in state.get("final_validation", _initial_validation()).items()
    }
    validation[stage] = record
    update: V3PrototypeState = {
        "final_validation": validation,
        "last_tool_ok": bool(result is not None and result.ok),
        "last_tool_phase": result.phase if result is not None else str(record.get("phase")),
        "last_tool_reason": str(record.get("failure_reason", "")),
        "node_events": [event],
    }
    if stage == "synth":
        metrics_ref = result.result_ref if result is not None and result.report is not None else ""
        clock = _clock_constraint(
            result.report if result is not None else None,
            runtime.config.minimum_frequency_mhz,
        )
        update["final_metrics_ref"] = metrics_ref
        update["final_clock"] = clock
        update["last_tool_ok"] = bool(
            result is not None and result.ok and metrics_ref and clock["passed"]
        )
    return update


def _final_failure_reason(
    update: V3PrototypeState, *, default: str
) -> str:
    tool_reason = str(update.get("last_tool_reason", ""))
    if tool_reason:
        return f"FINAL_{tool_reason}"
    return default


def _final_csim(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    update = _final_stage(runtime, state, stage="csim")
    if not update["last_tool_ok"]:
        update["status"] = "FAILED"
        update["stop_reason"] = _final_failure_reason(
            update, default="FINAL_CSIM_FAILED"
        )
        update["final_candidate_id"] = None
    return update


def _final_synth(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    update = _final_stage(runtime, state, stage="synth")
    if not update["last_tool_ok"]:
        update["status"] = "FAILED"
        update["stop_reason"] = _final_failure_reason(
            update, default="FINAL_SYNTH_OR_CLOCK_FAILED"
        )
        update["final_candidate_id"] = None
    return update


def _final_cosim(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    update = _final_stage(runtime, state, stage="cosim")
    if not update["last_tool_ok"]:
        return update | {
            "status": "FAILED",
            "stop_reason": _final_failure_reason(
                update, default="FINAL_COSIM_FAILED"
            ),
            "final_candidate_id": None,
        }
    candidate_id = state["final_attempt_candidate_id"]
    if candidate_id is None:
        raise RuntimeError("final validation has no selected attempt")
    validation = update["final_validation"]
    baseline_metrics = _read_report(runtime.run_root, state["baseline_metrics_ref"])
    final_metrics = _read_report(runtime.run_root, state["final_metrics_ref"])
    score = _score(
        runtime,
        candidate_id=candidate_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=final_metrics,
        validation=validation,
        clock=state["final_clock"],
        proposal=(
            runtime.proposal
            if candidate_id != state["baseline_candidate_id"]
            else None
        ),
        provisional_cosim=False,
    )
    score_ref = f"scores/{candidate_id}.final.json"
    _atomic_json(runtime.run_root / score_ref, score.to_dict())
    if not score.hard_constraints_passed:
        return update | {
            "final_score_ref": score_ref,
            "status": "FAILED",
            "stop_reason": "FINAL_SCORE_CONSTRAINT_FAILED",
            "final_candidate_id": None,
        }
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    registry["best_candidate_id"] = candidate_id
    registry["final_candidate_id"] = candidate_id
    candidate = registry["candidates"][candidate_id]
    candidate["status"] = "FINAL_VERIFIED"
    candidate["final_validation"] = validation
    candidate["final_metrics_ref"] = state["final_metrics_ref"]
    candidate["final_score_ref"] = score_ref
    manager.save_registry(registry)
    promoted = candidate_id != state["baseline_candidate_id"]
    return update | {
        "final_score_ref": score_ref,
        "final_candidate_id": candidate_id,
        "status": "DONE",
        "stop_reason": (
            "CANDIDATE_PROMOTED_AND_FINALIZED"
            if promoted
            else "BASELINE_FINALIZED_NO_IMPROVEMENT"
        ),
    }


def _render_team_report(
    runtime: _Runtime, result: Mapping[str, object]
) -> str:
    budget = result.get("budget")
    node_events = result.get("node_events")
    limits = result.get("prototype_limits")
    if (
        not isinstance(budget, Mapping)
        or not isinstance(node_events, list)
        or not isinstance(limits, list)
    ):
        raise RuntimeError("terminal result cannot render the team report")
    rows = [
        "| # | Node | Phase | Candidate | Action | Why | Outcome | Credits |",
        "|---:|---|---|---|---|---|---|---:|",
    ]
    for index, raw_event in enumerate(node_events, 1):
        if not isinstance(raw_event, Mapping):
            raise RuntimeError("terminal result contains an invalid node event")
        cells = [
            str(index),
            str(raw_event.get("node", "")),
            str(raw_event.get("phase", "")),
            str(raw_event.get("candidate_id") or "-"),
            str(raw_event.get("action", "")).replace("|", "\\|"),
            str(raw_event.get("why", "")).replace("|", "\\|"),
            str(raw_event.get("outcome", "")).replace("|", "\\|"),
            str(raw_event.get("credits_used", "")),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(
        [
            "# V3-A0 原型团队复盘报告",
            "",
            f"- Task: `{runtime.task.id}`",
            f"- Status: `{result.get('status', 'FAILED')}`",
            f"- Stop reason: `{result.get('stop_reason', 'UNKNOWN')}`",
            "- Best / Final: `"
            + str(result.get("best_candidate_id"))
            + " / "
            + str(result.get("final_candidate_id"))
            + "`",
            f"- Final attempt: `{result.get('final_attempt_candidate_id')}`",
            f"- Credits / Tokens: `{budget.get('credits_used')} / {budget.get('tokens_used')}`",
            "- CoSim gate: `"
            + json.dumps(
                result.get("cosim_gate", {}),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "`",
            "",
            "## 数据与控制流",
            "",
            *rows,
            "",
            "## 当前原型边界",
            "",
            *[f"- {item}" for item in limits],
            "",
        ]
    )


def _write_report(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    report_event = _event(
        runtime,
        node="write_report",
        phase="REPORT",
        candidate_id=(
            state.get("final_candidate_id")
            or state.get("final_attempt_candidate_id")
        ),
        action="write_machine_and_team_reports",
        why="Expose every graph decision, tool call, reason and budget transition.",
        outcome=state.get("status", "FAILED"),
        result_ref="v3_prototype_result.json",
    )
    node_events = [*state.get("node_events", []), report_event]
    budget = BudgetLedger(
        runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
    ).snapshot()
    result: dict[str, object] = {
        "schema_version": 1,
        "workflow": WORKFLOW_NAME,
        "prototype": True,
        "backend": {
            "class": f"{type(runtime.backend).__module__}.{type(runtime.backend).__qualname__}",
            "fingerprint": (
                str(runtime.backend.fingerprint())
                if callable(getattr(runtime.backend, "fingerprint", None))
                else f"{type(runtime.backend).__module__}.{type(runtime.backend).__qualname__}"
            ),
            "evidence_level": (
                "ORCHESTRATION_SMOKE_ONLY"
                if isinstance(runtime.backend, DeterministicPrototypeBackend)
                else (
                    "REAL_VITIS_VALIDATED"
                    if isinstance(runtime.backend, VitisBackend)
                    and state.get("status") == "DONE"
                    else (
                        "REAL_VITIS_ATTEMPT_FAILED"
                        if isinstance(runtime.backend, VitisBackend)
                        else "TEST_OR_CUSTOM_BACKEND"
                    )
                )
            ),
        },
        "task_id": runtime.task.id,
        "status": state.get("status", "FAILED"),
        "stop_reason": state.get("stop_reason", "UNKNOWN"),
        "baseline_candidate_id": state.get("baseline_candidate_id"),
        "best_candidate_id": state.get("best_candidate_id"),
        "final_attempt_candidate_id": state.get("final_attempt_candidate_id"),
        "final_candidate_id": state.get("final_candidate_id"),
        "cosim_gate": state.get("cosim_gate", {}),
        "budget": budget,
        "node_events": node_events,
        "prototype_limits": [
            "one scripted optimization Candidate",
            "no autonomous multi-round planner yet",
            "Graph checkpoints and terminal re-entry are supported; Candidate "
            "decision CAS journaling is not yet implemented",
        ],
        "artifacts": {
            "checkpoints": "graph_checkpoints.sqlite",
            "candidate_registry": "candidate_registry.json",
            "budget_ledger": "budget_ledger.jsonl",
            "trace": "trace.jsonl",
            "team_report": "v3_team_report.md",
            "result": "v3_prototype_result.json",
        },
    }
    _atomic_text(
        runtime.run_root / "v3_team_report.md",
        _render_team_report(runtime, result),
    )
    # This file is the terminal commit marker.  It is written only after every
    # artifact referenced by it has been durably materialized.
    _atomic_json(runtime.run_root / "v3_prototype_result.json", result)
    return {
        "phase": "DONE",
        "result_ref": "v3_prototype_result.json",
        "report_ref": "v3_team_report.md",
        "node_events": [report_event],
    }


def _pass_or_report(state: V3PrototypeState) -> str:
    return "pass" if state.get("last_tool_ok") is True else "report"


def _pass_or_select(state: V3PrototypeState) -> str:
    return "pass" if state.get("last_tool_ok") is True else "select"


def _gate_route(state: V3PrototypeState) -> str:
    return "cosim" if state.get("cosim_gate", {}).get("eligible") is True else "select"


def build_v3_prototype_graph(runtime: _Runtime, checkpointer: SqliteSaver):
    """Build the action-level graph; each tool node performs exactly one stage."""

    graph = StateGraph(V3PrototypeState)
    graph.add_node("initialize", lambda state: _initialize(runtime, state))
    graph.add_node("baseline_csim", lambda state: _baseline_csim(runtime, state))
    graph.add_node("baseline_synth", lambda state: _baseline_synth(runtime, state))
    graph.add_node("baseline_cosim", lambda state: _baseline_cosim(runtime, state))
    graph.add_node(
        "evaluate_round_budget", lambda state: _evaluate_round_budget(runtime, state)
    )
    graph.add_node("plan_candidate", lambda state: _plan_candidate(runtime, state))
    graph.add_node(
        "materialize_candidate", lambda state: _materialize_candidate(runtime, state)
    )
    graph.add_node("candidate_csim", lambda state: _candidate_csim(runtime, state))
    graph.add_node("candidate_synth", lambda state: _candidate_synth(runtime, state))
    graph.add_node(
        "candidate_score_gate", lambda state: _candidate_score_gate(runtime, state)
    )
    graph.add_node(
        "candidate_cosim_budget_gate",
        lambda state: _candidate_cosim_budget_gate(runtime, state),
    )
    graph.add_node("candidate_cosim", lambda state: _candidate_cosim(runtime, state))
    graph.add_node("promote_candidate", lambda state: _promote_candidate(runtime, state))
    graph.add_node("select_baseline", lambda state: _select_baseline(runtime, state))
    graph.add_node(
        "evaluate_final_budget", lambda state: _evaluate_final_budget(runtime, state)
    )
    graph.add_node("final_csim", lambda state: _final_csim(runtime, state))
    graph.add_node("final_synth", lambda state: _final_synth(runtime, state))
    graph.add_node("final_cosim", lambda state: _final_cosim(runtime, state))
    graph.add_node("write_report", lambda state: _write_report(runtime, state))

    graph.add_edge(START, "initialize")
    graph.add_conditional_edges(
        "initialize",
        _pass_or_report,
        {"pass": "baseline_csim", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "baseline_csim",
        _pass_or_report,
        {"pass": "baseline_synth", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "baseline_synth",
        _pass_or_report,
        {"pass": "baseline_cosim", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "baseline_cosim",
        _pass_or_report,
        {"pass": "evaluate_round_budget", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "evaluate_round_budget",
        _pass_or_select,
        {"pass": "plan_candidate", "select": "select_baseline"},
    )
    graph.add_edge("plan_candidate", "materialize_candidate")
    graph.add_edge("materialize_candidate", "candidate_csim")
    graph.add_conditional_edges(
        "candidate_csim",
        _pass_or_select,
        {"pass": "candidate_synth", "select": "select_baseline"},
    )
    graph.add_conditional_edges(
        "candidate_synth",
        _pass_or_select,
        {"pass": "candidate_score_gate", "select": "select_baseline"},
    )
    graph.add_conditional_edges(
        "candidate_score_gate",
        _gate_route,
        {"cosim": "candidate_cosim_budget_gate", "select": "select_baseline"},
    )
    graph.add_conditional_edges(
        "candidate_cosim_budget_gate",
        _gate_route,
        {"cosim": "candidate_cosim", "select": "select_baseline"},
    )
    graph.add_conditional_edges(
        "candidate_cosim",
        _pass_or_select,
        {"pass": "promote_candidate", "select": "select_baseline"},
    )
    graph.add_edge("promote_candidate", "evaluate_final_budget")
    graph.add_edge("select_baseline", "evaluate_final_budget")
    graph.add_conditional_edges(
        "evaluate_final_budget",
        _pass_or_report,
        {"pass": "final_csim", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "final_csim",
        _pass_or_report,
        {"pass": "final_synth", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "final_synth",
        _pass_or_report,
        {"pass": "final_cosim", "report": "write_report"},
    )
    graph.add_edge("final_cosim", "write_report")
    graph.add_edge("write_report", END)
    return graph.compile(checkpointer=checkpointer)


def run_v3_prototype(
    task: PublicTask,
    run_dir: str | Path,
    config: RunConfig,
    proposal: PatchProposal,
    *,
    backend: ToolBackend | None = None,
    scoring_config: ScoringConfig | None = None,
    patch_limits: PatchLimits | None = None,
    thread_id: str = "v3a0-prototype",
) -> dict[str, object]:
    """Run the single-Candidate V3-A0 graph and return its durable result."""

    if not thread_id.strip():
        raise ValueError("thread_id must not be empty")
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    scoring = scoring_config or load_scoring_config(
        Path(__file__).with_name("config") / "v2_scoring.yaml"
    )
    runtime = _Runtime(
        task=task,
        run_root=root,
        config=config,
        proposal=proposal,
        backend=backend or VitisBackend(),
        scoring=scoring,
        patch_limits=patch_limits or PatchLimits(max_changed_lines=30, max_hunks=4),
        thread_id=thread_id,
    )
    checkpoint_path = root / "graph_checkpoints.sqlite"
    with _RunLock(root):
        _verify_run_identity(runtime)
        terminal = _load_terminal_result(runtime)
        if terminal is not None:
            return terminal
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            graph = build_v3_prototype_graph(runtime, checkpointer)
            graph_config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 64,
            }
            snapshot = graph.get_state(graph_config)
            if snapshot.values:
                if not snapshot.next:
                    raise RuntimeError(
                        "checkpoint is terminal but the durable V3 result is missing"
                    )
                graph.invoke(None, graph_config)
            else:
                graph.invoke({"node_events": []}, graph_config)
        terminal = _load_terminal_result(runtime)
        if terminal is None:
            raise RuntimeError("V3 prototype did not produce a durable result")
        return terminal


class DeterministicPrototypeBackend:
    """Smoke-only backend for proving graph orchestration without Vitis."""

    def fingerprint(self) -> str:
        return "deterministic-v3-prototype-backend-v1"

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        **_kwargs: object,
    ) -> BackendResult:
        optimized = b"PIPELINE II=1\n" in kernel_bytes
        if kind == "synth":
            latency = 256 if optimized else 4096
            interval = 1 if optimized else 16
            return BackendResult(
                True,
                "pass",
                0,
                0.01,
                evidence=["deterministic prototype synthesis"],
                report={
                    "estimated_clock_period_ns": 5.0,
                    "latency": {
                        "best": latency,
                        "average": latency,
                        "worst": latency,
                    },
                    "interval": {"min": interval, "max": interval},
                    "resources": {
                        "LUT": 120 if optimized else 100,
                        "FF": 220 if optimized else 200,
                        "DSP": 0,
                        "BRAM_18K": 0,
                        "URAM": 0,
                    },
                    "available_resources": {
                        "LUT": 1000,
                        "FF": 2000,
                        "DSP": 100,
                        "BRAM_18K": 100,
                        "URAM": 50,
                    },
                },
            )
        if kind == "cosim":
            return BackendResult(
                True,
                "pass",
                0,
                0.01,
                evidence=["deterministic prototype C/RTL agreement"],
                cosim={"status": "Pass"},
            )
        return BackendResult(
            True,
            "pass",
            0,
            0.01,
            evidence=["deterministic prototype C simulation"],
        )
