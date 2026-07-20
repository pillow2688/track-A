"""Runnable V3-A1 vertical prototype built from action-level LangGraph nodes.

This module is intentionally parallel to V2.  It proves a deterministic,
checkpointed optimization closure (baseline -> scripted Candidate rounds ->
gated CoSim -> final validation -> sealed report package) without changing the
production V2 entry point.
"""

from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Annotated, Mapping, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .budget import BudgetLedger
from .candidate import CandidateManager
from .optimization import evaluate_exploration_cosim_gate
from .repair import (
    PatchLimits,
    PatchProposal,
    PatchValidationError,
    apply_unified_diff,
)
from .scoring import (
    OFFICIAL_SCORE_SOURCE,
    CandidateScore,
    ScoringConfig,
    estimate_official_score_proxy,
    compare_scores,
    load_scoring_config,
    score_candidate,
)
from .task import PublicTask
from .tools import BackendResult, ToolBackend, ToolResult, ToolServer
from .v3_evidence import build_synth_evidence
from .v3_planner import (
    PLANNER_ACTION_SCHEMA,
    PLANNER_INPUT_SCHEMA,
    PLANNER_OUTPUT_SCHEMA,
    ScriptedPlanner,
    build_planner_input,
    build_planner_output,
    canonical_sha256,
    load_planner_output,
    planner_action_id,
    planner_action_request,
    proposal_payload,
    stable_validation,
    validate_planner_input,
)
from .v3_planner_action import (
    LIVE_PLANNER_ACTION_SCHEMA,
    LIVE_PLANNER_OUTCOME_SCHEMA,
    LivePlanner,
    PlannerActionJournal,
    PlannerActionResult,
    PreparedPlannerCall,
)
from .vitis import VitisBackend
from .workflow import (
    RunConfig,
    _RunLock,
    _append_trace,
    _atomic_json,
    _create_or_load_registry,
    _fsync_directory,
    _initial_validation,
    _invoke_stage,
    _snapshot_baseline,
    _task_spec,
    _validation_record,
    _write_once_or_verify,
)


WORKFLOW_NAME = "V3A0_LANGGRAPH_PROTOTYPE"
STATE_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 3
TERMINAL_RESULT_SCHEMA = "v3a.terminal-result.v1"
_FULL_CLOSURE_CALLS = {"csim": 1, "synth": 1, "cosim": 1}


class V3PrototypeState(TypedDict, total=False):
    task_id: str
    run_dir: str
    phase: str
    status: str
    stop_reason: str
    baseline_candidate_id: str
    active_candidate_id: str | None
    best_candidate_id: str
    final_attempt_candidate_id: str | None
    final_candidate_id: str | None
    planner_ref: str
    planner_action_id: str
    planner_input_ref: str
    planner_input_sha256: str
    planner_output_ref: str
    planner_output_sha256: str
    live_planner_action_id: str
    live_planner_request_ref: str
    live_planner_request_sha256: str
    live_planner_output_ref: str
    live_planner_output_sha256: str
    live_planner_started_ref: str
    live_planner_completed_ref: str
    planner_selection_metrics_digest: str
    baseline_metrics_ref: str
    best_metrics_ref: str
    candidate_metrics_ref: str
    final_metrics_ref: str
    baseline_synth_evidence_ref: str
    baseline_synth_evidence_sha256: str
    best_synth_evidence_ref: str
    best_synth_evidence_sha256: str
    candidate_synth_evidence_ref: str
    candidate_synth_evidence_sha256: str
    final_synth_evidence_ref: str
    final_synth_evidence_sha256: str
    baseline_clock: dict[str, object]
    best_clock: dict[str, object]
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
    round_index: int
    rounds_completed: int
    no_improvement_rounds: int
    last_round_improved: bool
    exploration_stop_reason: str
    decision_ref: str
    registry_revision: int
    final_attempt_count: int
    final_attempted_candidate_ids: list[str]


@dataclass(frozen=True)
class _Runtime:
    task: PublicTask
    run_root: Path
    config: RunConfig
    proposals: tuple[PatchProposal, ...]
    backend: ToolBackend
    scoring: ScoringConfig
    patch_limits: PatchLimits
    thread_id: str
    max_no_improvement_rounds: int
    max_final_attempts: int
    live_planner: LivePlanner | None = None
    max_planner_rounds: int = 1

    @property
    def proposal(self) -> PatchProposal:
        """Legacy single-proposal view used by old run identities."""

        return self.proposals[0]

    @property
    def planner_mode(self) -> str:
        return (
            "live_non_replayable_adapter"
            if self.live_planner is not None
            else "scripted_deterministic_adapter"
        )


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
    _fsync_directory(path.parent)


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


def _checkpoint_schema_snapshot() -> dict[str, object]:
    return {
        "schema_version": "v3a.graph-checkpoint.v3",
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "compatible_terminal_result_schema": 1,
    }


def _run_config_snapshot(runtime: _Runtime) -> dict[str, object]:
    value = runtime.config.to_dict()
    identity: dict[str, object] = {
        "workflow": WORKFLOW_NAME,
        "prototype": True,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "thread_id": runtime.thread_id,
        "backend_fingerprint": _backend_fingerprint(runtime.backend),
        "scoring": runtime.scoring.to_dict(),
        "patch_limits": {
            "max_changed_lines": runtime.patch_limits.max_changed_lines,
            "max_hunks": runtime.patch_limits.max_hunks,
            "allow_full_file_replacement": (
                runtime.patch_limits.allow_full_file_replacement
            ),
        },
    }
    if runtime.live_planner is None:
        identity["proposal_sha256"] = _sha256_json(runtime.proposal.to_dict())
    else:
        identity.update(
            {
                "live_planner_fingerprint": runtime.live_planner.fingerprint(),
                "live_planner_replay_policy": runtime.live_planner.replay_policy,
                "planner_mode": runtime.planner_mode,
                "max_planner_rounds": runtime.max_planner_rounds,
                "live_planner_action_schema": LIVE_PLANNER_ACTION_SCHEMA,
                "live_planner_outcome_schema": LIVE_PLANNER_OUTCOME_SCHEMA,
            }
        )
    value.update(identity)
    # Preserve the exact legacy identity for the already-published one-patch
    # prototype runs.  Multi-round runs add an explicit planner policy and the
    # complete ordered proposal digest list.
    if runtime.live_planner is None and len(runtime.proposals) > 1:
        value["proposal_sha256_list"] = [
            _sha256_json(proposal.to_dict()) for proposal in runtime.proposals
        ]
        value["max_no_improvement_rounds"] = runtime.max_no_improvement_rounds
    if runtime.max_final_attempts != 1:
        value["max_final_attempts"] = runtime.max_final_attempts
    return value


def _safe_run_ref(runtime: _Runtime, reference: str) -> Path:
    if not reference:
        raise RuntimeError("artifact reference must not be empty")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"artifact reference escapes the run: {reference}")
    unresolved = runtime.run_root / relative
    cursor = runtime.run_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RuntimeError(
                f"artifact reference uses a symbolic link: {reference}"
            )
    path = unresolved.resolve()
    run_root = runtime.run_root.resolve()
    try:
        path.relative_to(run_root)
    except ValueError as exc:
        raise RuntimeError(f"artifact reference escapes the run: {reference}") from exc
    if not path.is_file():
        raise RuntimeError(f"artifact reference is missing: {reference}")
    return path


def _artifact_binding(runtime: _Runtime, reference: object) -> dict[str, object]:
    if not isinstance(reference, str) or not reference:
        return {"ref": None, "sha256": None}
    path = _safe_run_ref(runtime, reference)
    return {"ref": reference, "sha256": _sha256_file(path)}


def _verify_synth_evidence_bindings(
    runtime: _Runtime, value: object
) -> None:
    if isinstance(value, Mapping):
        for key, reference in value.items():
            name = str(key)
            if name.endswith("synth_evidence_ref"):
                digest_key = name[: -len("ref")] + "sha256"
                digest = value.get(digest_key)
                if (reference is None or reference == "") and (
                    digest is None or digest == ""
                ):
                    continue
                if not isinstance(reference, str) or not isinstance(digest, str):
                    raise RuntimeError("Synth evidence binding is incomplete")
                if _sha256_file(_safe_run_ref(runtime, reference)) != digest:
                    raise RuntimeError("Synth evidence artifact hash mismatch")
            _verify_synth_evidence_bindings(runtime, reference)
        return
    if isinstance(value, list):
        for child in value:
            _verify_synth_evidence_bindings(runtime, child)


def _load_planner_output_only(
    runtime: _Runtime,
    *,
    output_ref: object,
    output_sha256: object,
    action_id: object,
    input_sha256: object,
) -> PatchProposal:
    if not all(
        isinstance(item, str) and item
        for item in (output_ref, output_sha256, action_id, input_sha256)
    ):
        raise RuntimeError("Planner output binding is incomplete")
    path = _safe_run_ref(runtime, output_ref)
    value = _read_json_object(path)
    if canonical_sha256(value) != output_sha256:
        raise RuntimeError("Planner output artifact hash mismatch")
    try:
        return load_planner_output(
            value,
            expected_action_id=action_id,
            expected_input_sha256=input_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Planner output contract violation: {exc}") from exc


def _validate_planner_binding(
    runtime: _Runtime,
    *,
    action_id: object,
    input_ref: object,
    input_sha256: object,
    output_ref: object,
    output_sha256: object,
    legacy_projection_ref: object,
) -> PatchProposal:
    if not all(
        isinstance(item, str) and item
        for item in (
            action_id,
            input_ref,
            input_sha256,
            output_ref,
            output_sha256,
            legacy_projection_ref,
        )
    ):
        raise RuntimeError("Planner provenance binding is incomplete")

    planner_input = _read_json_object(_safe_run_ref(runtime, input_ref))
    if canonical_sha256(planner_input) != input_sha256:
        raise RuntimeError("Planner input artifact hash mismatch")
    try:
        validate_planner_input(planner_input)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Planner input contract violation: {exc}") from exc

    action_root = runtime.run_root / "control" / "planner_actions"
    started_ref = str(
        (action_root / f"{action_id}.started.json").relative_to(runtime.run_root)
    ).replace("\\", "/")
    completed_ref = str(
        (action_root / f"{action_id}.completed.json").relative_to(runtime.run_root)
    ).replace("\\", "/")
    started = _read_json_object(_safe_run_ref(runtime, started_ref))
    request_fields = {
        "schema_version",
        "planner_fingerprint",
        "input_ref",
        "input_sha256",
        "replay_policy",
    }
    request = {name: started.get(name) for name in request_fields}
    if (
        set(started) != request_fields | {"action_id", "status"}
        or started.get("action_id") != action_id
        or started.get("status") != "STARTED"
        or request.get("input_ref") != input_ref
        or request.get("input_sha256") != input_sha256
    ):
        raise RuntimeError("Planner STARTED journal binding mismatch")
    try:
        if planner_action_id(request) != action_id:
            raise RuntimeError("Planner STARTED action identity mismatch")
    except ValueError as exc:
        raise RuntimeError(f"Planner STARTED journal contract violation: {exc}") from exc

    proposal = _load_planner_output_only(
        runtime,
        output_ref=output_ref,
        output_sha256=output_sha256,
        action_id=action_id,
        input_sha256=input_sha256,
    )
    legacy_projection = _read_json_object(
        _safe_run_ref(runtime, legacy_projection_ref)
    )
    round_state = planner_input.get("round")
    if not isinstance(round_state, Mapping):
        raise RuntimeError("Planner input round state is missing")
    round_index = round_state.get("round_index")
    parent_candidate_id = round_state.get("parent_candidate_id")
    if (
        isinstance(round_index, bool)
        or not isinstance(round_index, int)
        or round_index <= 0
        or not isinstance(parent_candidate_id, str)
        or not parent_candidate_id
    ):
        raise RuntimeError("Planner input round identity is invalid")
    expected_projection = _proposal_snapshot(
        runtime,
        proposal=proposal,
        parent_candidate_id=parent_candidate_id,
        round_index=round_index,
    )
    if legacy_projection != expected_projection:
        raise RuntimeError("legacy Planner projection diverges from versioned output")
    completed = _read_json_object(_safe_run_ref(runtime, completed_ref))
    expected_completed = request | {
        "action_id": action_id,
        "status": "COMPLETED",
        "outcome": "PROPOSAL",
        "result_ref": output_ref,
        "result_sha256": output_sha256,
        "legacy_projection_ref": legacy_projection_ref,
        "legacy_projection_sha256": _sha256_json(legacy_projection),
    }
    if completed != expected_completed:
        raise RuntimeError("Planner COMPLETED journal binding mismatch")
    return proposal


def _current_proposal(
    runtime: _Runtime, state: Mapping[str, object]
) -> PatchProposal:
    return _validate_planner_binding(
        runtime,
        input_ref=state.get("planner_input_ref"),
        output_ref=state.get("planner_output_ref"),
        output_sha256=state.get("planner_output_sha256"),
        action_id=state.get("planner_action_id"),
        input_sha256=state.get("planner_input_sha256"),
        legacy_projection_ref=state.get("planner_ref"),
    )


def _proposal_for_candidate(
    runtime: _Runtime, candidate_id: str
) -> PatchProposal | None:
    registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
    candidates = registry.get("candidates")
    candidate = (
        candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    )
    if not isinstance(candidate, Mapping) or candidate.get("kind") == "baseline":
        return None
    proposal = _validate_planner_binding(
        runtime,
        input_ref=candidate.get("planner_input_ref"),
        output_ref=candidate.get("planner_output_ref"),
        output_sha256=candidate.get("planner_output_sha256"),
        action_id=candidate.get("planner_action_id"),
        input_sha256=candidate.get("planner_input_sha256"),
        legacy_projection_ref=candidate.get("planner_ref"),
    )
    expected_proposal_sha256 = candidate.get("proposal_sha256")
    if (
        not isinstance(expected_proposal_sha256, str)
        or canonical_sha256(proposal_payload(proposal)) != expected_proposal_sha256
    ):
        raise RuntimeError("Candidate Planner proposal binding mismatch")
    return proposal


def _validate_live_planner_candidate_binding(
    runtime: _Runtime,
    registry: Mapping[str, object],
    candidate: Mapping[str, object],
    proposal: PatchProposal,
) -> None:
    live_action_id = candidate.get("live_planner_action_id")
    if live_action_id is None:
        return
    if not isinstance(live_action_id, str) or not live_action_id:
        raise RuntimeError("live Planner Candidate action binding is invalid")
    if runtime.live_planner is None:
        raise RuntimeError(
            "live Planner Candidate requires its original Planner adapter"
        )
    input_ref = candidate.get("planner_input_ref")
    input_sha256 = candidate.get("planner_input_sha256")
    parent_id = candidate.get("parent_id")
    candidates = registry.get("candidates")
    parent = (
        candidates.get(parent_id)
        if isinstance(candidates, Mapping) and isinstance(parent_id, str)
        else None
    )
    if (
        not isinstance(input_ref, str)
        or not isinstance(input_sha256, str)
        or not isinstance(parent_id, str)
        or not isinstance(parent, Mapping)
        or not isinstance(parent.get("code_hash"), str)
    ):
        raise RuntimeError("live Planner Candidate provenance is incomplete")
    planner_input = _read_json_object(_safe_run_ref(runtime, input_ref))
    result = PlannerActionJournal(
        runtime.run_root,
        BudgetLedger(
            runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
        ),
    ).execute_or_recover(
        runtime.live_planner,
        planner_input,
        input_ref=input_ref,
        input_sha256=input_sha256,
        candidate_id=parent_id,
        code_hash=str(parent["code_hash"]),
    )
    expected = {
        "live_planner_action_id": result.action_id,
        "live_planner_request_ref": result.request_ref,
        "live_planner_request_sha256": result.request_sha256,
        "live_planner_output_ref": result.output_ref,
        "live_planner_output_sha256": result.output_sha256,
        "live_planner_started_ref": result.started_ref,
        "live_planner_completed_ref": result.completed_ref,
    }
    if any(candidate.get(key) != value for key, value in expected.items()):
        raise RuntimeError("live Planner Candidate artifact binding mismatch")
    if proposal_payload(result.proposal) != proposal_payload(proposal):
        raise RuntimeError("live Planner outcome diverges from Candidate proposal")


def _proposal_snapshot(
    runtime: _Runtime,
    *,
    proposal: PatchProposal | None = None,
    parent_candidate_id: str = "candidate_000",
    round_index: int = 1,
) -> dict[str, object]:
    if proposal is None and runtime.live_planner is not None:
        raise RuntimeError("live Planner projection requires a durable proposal")
    selected = proposal or runtime.proposal
    value = selected.to_dict()
    value["required_validation"] = list(selected.required_validation)
    return value | {
        "parent_candidate_id": parent_candidate_id,
        "planner_mode": (
            runtime.planner_mode
            if runtime.live_planner is not None
            else "scripted_prototype"
        ),
        **(
            {"round_index": round_index}
            if runtime.max_planner_rounds > 1 or len(runtime.proposals) > 1
            else {}
        ),
    }


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read durable {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"durable {path.name} is not a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _planner_candidate_facts(
    runtime: _Runtime,
    registry: Mapping[str, object],
    candidate_id: str,
) -> dict[str, object]:
    candidates = registry.get("candidates")
    candidate = (
        candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    )
    if not isinstance(candidate, Mapping):
        raise RuntimeError(f"Planner incumbent is absent: {candidate_id}")
    validation = candidate.get("validation")
    metrics_ref = candidate.get("metrics_ref")
    if isinstance(metrics_ref, str) and metrics_ref:
        _completed_synth_report(
            runtime,
            metrics_ref,
            candidate_id=candidate_id,
            validation_scope="exploration",
        )
    synth_evidence = _artifact_binding(
        runtime, candidate.get("synth_evidence_ref")
    )
    stored_evidence_hash = candidate.get("synth_evidence_sha256")
    if (
        isinstance(stored_evidence_hash, str)
        and stored_evidence_hash
        and synth_evidence.get("sha256") != stored_evidence_hash
    ):
        raise RuntimeError("Candidate synth evidence binding mismatch")
    return {
        "candidate_id": candidate_id,
        "parent_id": candidate.get("parent_id"),
        "kind": candidate.get("kind"),
        "status": candidate.get("status"),
        "source": _artifact_binding(runtime, candidate.get("source_ref")),
        "code_hash": candidate.get("code_hash"),
        "metrics": _artifact_binding(runtime, metrics_ref),
        "synth_evidence": synth_evidence,
        "validation": stable_validation(validation)
        if isinstance(validation, Mapping)
        else {},
    }


def _build_round_planner_input(
    runtime: _Runtime, state: Mapping[str, object]
) -> dict[str, object]:
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    round_index = int(state.get("round_index", 1))
    baseline_id = str(state["baseline_candidate_id"])
    incumbent_id = str(state["best_candidate_id"])
    candidates = registry.get("candidates")
    history: list[dict[str, object]] = []
    if isinstance(candidates, Mapping):
        ordered = sorted(
            (
                (str(candidate_id), candidate)
                for candidate_id, candidate in candidates.items()
                if isinstance(candidate, Mapping)
                and candidate.get("kind") != "baseline"
            ),
            key=lambda item: (
                int(item[1].get("round_index", 0)),
                item[0],
            ),
        )
        for candidate_id, candidate in ordered:
            history.append(
                {
                    "kind": "candidate",
                    "candidate_id": candidate_id,
                    "round_index": candidate.get("round_index"),
                    "parent_id": candidate.get("parent_id"),
                    "status": candidate.get("status"),
                    "change_class": candidate.get("change_class"),
                    "selection_metrics_digest": candidate.get(
                        "selection_metrics_digest"
                    ),
                    "patch_sha256": candidate.get("patch_sha256"),
                    "metrics": _artifact_binding(
                        runtime, candidate.get("metrics_ref")
                    ),
                    "synth_evidence": _artifact_binding(
                        runtime, candidate.get("synth_evidence_ref")
                    ),
                    "score": _artifact_binding(
                        runtime,
                        candidate.get("score_ref")
                        or candidate.get("rejection_score_ref"),
                    ),
                    "rejection_reason": candidate.get("rejection_reason"),
                }
            )
    for rejection_path in sorted(
        (runtime.run_root / "control" / "proposal_rejections").glob(
            "round_*.json"
        )
    ):
        rejection = _read_json_object(rejection_path)
        rejected_round = rejection.get("round_index")
        if (
            isinstance(rejected_round, bool)
            or not isinstance(rejected_round, int)
            or rejected_round >= round_index
        ):
            continue
        history.append(
            {
                "kind": "proposal_rejection",
                "round_index": rejected_round,
                "parent_id": rejection.get("parent_candidate_id"),
                "reason": rejection.get("reason"),
                "change_class": rejection.get("change_class"),
                "selection_metrics_digest": rejection.get(
                    "selection_metrics_digest"
                ),
                "planner_action_id": rejection.get("planner_action_id"),
                "planner_output": _artifact_binding(
                    runtime, rejection.get("planner_output_ref")
                ),
            }
        )
    history.sort(
        key=lambda item: (
            int(item.get("round_index", 0)),
            str(item.get("kind", "")),
            str(item.get("candidate_id", "")),
        )
    )
    budget_snapshot = BudgetLedger(
        runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
    ).snapshot()
    return build_planner_input(
        task=_task_spec(runtime.task),
        round_state={
            "round_index": round_index,
            "rounds_completed": int(state.get("rounds_completed", 0)),
            "consecutive_no_improvement": int(
                state.get("no_improvement_rounds", 0)
            ),
            "parent_candidate_id": incumbent_id,
        },
        incumbent=_planner_candidate_facts(runtime, registry, incumbent_id),
        baseline=_planner_candidate_facts(runtime, registry, baseline_id),
        history=history,
        policy={
            "minimum_frequency_mhz": runtime.config.minimum_frequency_mhz,
            "requires_cosim": runtime.task.requires_cosim,
            "candidate_gate": "strict_score_improvement_before_cosim",
            "final_validation": ["csim", "synth", "cosim"],
            "max_no_improvement_rounds": runtime.max_no_improvement_rounds,
            "scoring": runtime.scoring.to_dict(),
        },
        budget={
            "credit_limit": budget_snapshot.get("credit_limit"),
            "credits_used": budget_snapshot.get("credits_used"),
            "credits_remaining": budget_snapshot.get("credits_remaining"),
            "tool_used": budget_snapshot.get("tool_used"),
            "tool_pending": budget_snapshot.get("tool_pending"),
            "token_limit": budget_snapshot.get("token_limit"),
            "tokens_used": budget_snapshot.get("tokens_used"),
            "tokens_remaining": budget_snapshot.get("tokens_remaining"),
        },
    )


def _planner_recovery_projection(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Compare stable round facts while allowing metering counters to advance."""

    validated = validate_planner_input(value)
    budget = validated.get("budget")
    if not isinstance(budget, Mapping):
        raise RuntimeError("Planner recovery budget is invalid")
    validated["budget"] = {
        "credit_limit": budget.get("credit_limit"),
        "token_limit": budget.get("token_limit"),
    }
    return validated


def _write_synth_evidence(
    runtime: _Runtime,
    result: ToolResult | None,
    *,
    candidate_id: str,
) -> tuple[str, str]:
    if result is None or result.report is None:
        return "", ""
    result_path = _safe_run_ref(runtime, result.result_ref)
    action_root = result_path.parent
    xml_path: Path | None = None
    xml_ref: str | None = None
    xml_sha256: str | None = None
    relative = result.artifacts.get("csynth_xml")
    expected_hash = result.artifact_hashes.get("csynth_xml")
    if relative is not None or expected_hash is not None:
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise RuntimeError("Synth result has an incomplete csynth.xml binding")
        candidate_path = (action_root / relative).resolve()
        try:
            candidate_path.relative_to(action_root)
        except ValueError as exc:
            raise RuntimeError("csynth.xml artifact escapes its tool action") from exc
        if not candidate_path.is_file() or _sha256_file(candidate_path) != expected_hash:
            raise RuntimeError("csynth.xml artifact hash mismatch")
        xml_path = candidate_path
        xml_ref = str(candidate_path.relative_to(runtime.run_root)).replace("\\", "/")
        xml_sha256 = expected_hash
    evidence = build_synth_evidence(
        candidate_id=candidate_id,
        action_id=result.action_id,
        result_ref=result.result_ref,
        report=result.report,
        tool_evidence=result.evidence,
        csynth_xml_path=xml_path,
        csynth_xml_ref=xml_ref,
        csynth_xml_sha256=xml_sha256,
    )
    reference = f"evidence/synth/{result.action_id}.json"
    path = runtime.run_root / reference
    _write_once_or_verify(path, evidence)
    return reference, _sha256_file(path)


def _build_package_manifest(
    runtime: _Runtime, result: Mapping[str, object]
) -> dict[str, object]:
    registry_path = runtime.run_root / "candidate_registry.json"
    ledger_path = runtime.run_root / "budget_ledger.jsonl"
    report_path = runtime.run_root / "v3_team_report.md"
    graph_schema_path = runtime.run_root / "v3_graph_schema.json"
    task_spec_path = runtime.run_root / "v3_task_spec.json"
    run_config_path = runtime.run_root / "v3_run_config.json"
    trace_path = runtime.run_root / "trace.jsonl"
    registry = _read_json_object(registry_path)
    score_artifacts: list[Path] = []
    candidate_operation_artifacts: list[Path] = []
    if isinstance(result.get("planner_contract"), Mapping):
        _validate_candidate_registry_sources(runtime, registry)
        _verify_synth_evidence_bindings(runtime, registry)
        _verify_synth_evidence_bindings(runtime, result)
        score_artifacts = _validate_score_artifacts(runtime, registry, result)
        candidate_operation_artifacts = _validate_candidate_operation_journals(
            runtime, registry, result
        )
    artifact_paths = [
        registry_path,
        ledger_path,
        report_path,
        graph_schema_path,
        task_spec_path,
        run_config_path,
        trace_path,
        *score_artifacts,
        *candidate_operation_artifacts,
    ]

    def collect_references(value: object, *, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                collect_references(child, key=str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                collect_references(child, key=key)
            return
        if key.endswith("_ref") and isinstance(value, str) and value:
            if value == "v3_prototype_result.json":
                # The result is the outer commit marker and is written only
                # after this Manifest; its canonical payload is hashed below.
                return
            path = (runtime.run_root / value).resolve()
            try:
                path.relative_to(runtime.run_root)
            except ValueError as exc:
                raise RuntimeError("Registry artifact reference escapes the run") from exc
            if not path.is_file():
                raise RuntimeError(f"Registry artifact reference is missing: {value}")
            artifact_paths.append(path)

    collect_references(registry)
    collect_references(result)

    # A Planner result is usable only as one complete provenance chain.  Do
    # not let optional glob collection make a missing STARTED/COMPLETED journal
    # disappear from the sealed package.
    planner_action_root = runtime.run_root / "control" / "planner_actions"
    planner_output_root = runtime.run_root / "planner" / "outputs"
    started_paths = sorted(planner_action_root.glob("*.started.json"))
    completed_paths = sorted(planner_action_root.glob("*.completed.json"))
    output_paths = sorted(planner_output_root.glob("*.json"))

    def action_ids(paths: Sequence[Path], suffix: str) -> set[str]:
        identities: set[str] = set()
        for path in paths:
            if not path.name.endswith(suffix):
                raise RuntimeError("Planner journal filename is invalid")
            action_id = path.name[: -len(suffix)]
            if not action_id or action_id in identities:
                raise RuntimeError("Planner journal action identity is invalid")
            identities.add(action_id)
        return identities

    started_ids = action_ids(started_paths, ".started.json")
    completed_ids = action_ids(completed_paths, ".completed.json")
    output_ids = action_ids(output_paths, ".json")
    if started_ids != completed_ids or started_ids != output_ids:
        raise RuntimeError("Planner provenance chain is incomplete")

    for output_path in output_paths:
        action_id = output_path.stem
        started_path = planner_action_root / f"{action_id}.started.json"
        completed_path = planner_action_root / f"{action_id}.completed.json"
        started = _read_json_object(started_path)
        completed = _read_json_object(completed_path)
        input_ref = started.get("input_ref")
        input_sha256 = started.get("input_sha256")
        output_ref = str(output_path.relative_to(runtime.run_root)).replace(
            "\\", "/"
        )
        output_sha256 = canonical_sha256(_read_json_object(output_path))
        legacy_projection_ref = completed.get("legacy_projection_ref")
        _validate_planner_binding(
            runtime,
            action_id=action_id,
            input_ref=input_ref,
            input_sha256=input_sha256,
            output_ref=output_ref,
            output_sha256=output_sha256,
            legacy_projection_ref=legacy_projection_ref,
        )
        for reference in (input_ref, legacy_projection_ref):
            if not isinstance(reference, str):
                raise RuntimeError("Planner provenance reference is incomplete")
            artifact_paths.append(_safe_run_ref(runtime, reference))
        artifact_paths.extend(
            [started_path, completed_path, output_path]
        )

    for pattern in ("evidence/synth/*.json",):
        artifact_paths.extend(sorted(runtime.run_root.glob(pattern)))
    completed_tool_results = {
        item.result_ref: item for item in _validate_completed_tool_actions(runtime)
    }
    _budget, tool_server = _server(runtime)
    for result_ref in completed_tool_results:
        artifact_paths.append(_safe_run_ref(runtime, result_ref))
    for result_path in list(artifact_paths):
        if result_path.name != "result.json" or result_path.parent.parent.name != "actions":
            continue
        result_ref = str(result_path.relative_to(runtime.run_root)).replace(
            "\\", "/"
        )
        tool_result = completed_tool_results.get(result_ref)
        if tool_result is None:
            tool_result = tool_server.load_completed_result(result_ref)
        tool_artifacts = tool_result.artifacts
        artifact_hashes = tool_result.artifact_hashes
        for name, relative in tool_artifacts.items():
            expected_hash = artifact_hashes.get(name)
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise RuntimeError("Tool result artifact binding is incomplete")
            path = (result_path.parent / relative).resolve()
            try:
                path.relative_to(result_path.parent)
            except ValueError as exc:
                raise RuntimeError("Tool result artifact escapes its action") from exc
            if not path.is_file() or _sha256_file(path) != expected_hash:
                raise RuntimeError(f"Tool result artifact hash mismatch: {name}")
            artifact_paths.append(path)
    final_candidate_id = result.get("final_candidate_id")
    if isinstance(final_candidate_id, str):
        candidates = registry.get("candidates")
        candidate = (
            candidates.get(final_candidate_id)
            if isinstance(candidates, Mapping)
            else None
        )
        if not isinstance(candidate, Mapping):
            raise RuntimeError("final Candidate is absent from the Registry")
        source = runtime.run_root / str(candidate.get("source_ref", ""))
        if not source.is_file():
            raise RuntimeError("final Candidate source artifact is missing")
        artifact_paths.append(source)
    artifacts = []
    for path in sorted(set(artifact_paths)):
        if path.is_symlink():
            raise RuntimeError("package artifact must not be a symbolic link")
        resolved = path.resolve()
        try:
            resolved.relative_to(runtime.run_root)
        except ValueError as exc:
            raise RuntimeError("package artifact escapes the run") from exc
        if not resolved.is_file():
            raise RuntimeError("package artifact is missing")
        relative = str(resolved.relative_to(runtime.run_root)).replace("\\", "/")
        artifacts.append(
            {
                "path": relative,
                "sha256": _sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
            }
        )
    return {
        "schema_version": "v3a.package-manifest.v1",
        "workflow": WORKFLOW_NAME,
        "task_id": runtime.task.id,
        "status": result.get("status"),
        "final_candidate_id": final_candidate_id,
        "registry_revision": int(registry.get("v3_revision", 0)),
        "registry_last_operation_id": registry.get("v3_last_operation_id"),
        "node_event_count": len(result.get("node_events", [])),
        "terminal_payload_sha256": _sha256_json(dict(result)),
        "artifacts": artifacts,
    }


def _verify_package_manifest(
    runtime: _Runtime,
    result: Mapping[str, object],
    *,
    verify_report: bool,
) -> None:
    package = result.get("package")
    if package is None:
        graph_schema_path = runtime.run_root / "v3_graph_schema.json"
        graph_schema = (
            _read_json_object(graph_schema_path)
            if graph_schema_path.is_file()
            else {}
        )
        checkpoint_version = graph_schema.get("checkpoint_schema_version")
        registry_path = runtime.run_root / "candidate_registry.json"
        registry = (
            _read_json_object(registry_path) if registry_path.is_file() else {}
        )
        candidates = registry.get("candidates")
        registry_has_a1 = bool(
            isinstance(candidates, Mapping)
            and any(
                isinstance(candidate, Mapping)
                and isinstance(candidate.get("planner_action_id"), str)
                for candidate in candidates.values()
            )
        )
        planner_has_a1 = bool(
            list((runtime.run_root / "planner" / "inputs").glob("*.json"))
            or list((runtime.run_root / "planner" / "outputs").glob("*.json"))
            or list(
                (runtime.run_root / "control" / "planner_actions").glob(
                    "*.json"
                )
            )
        )
        requires_package = bool(
            result.get("result_schema") == TERMINAL_RESULT_SCHEMA
            or isinstance(result.get("planner_contract"), Mapping)
            or registry_has_a1
            or planner_has_a1
            or (
                isinstance(checkpoint_version, int)
                and not isinstance(checkpoint_version, bool)
                and checkpoint_version >= 3
            )
        )
        if requires_package:
            raise RuntimeError("terminal V3 sealed package is required")
        # Backward compatibility is restricted to historical A0 terminals.
        return
    if not isinstance(package, Mapping):
        raise RuntimeError("terminal V3 package metadata is invalid")
    if isinstance(result.get("planner_contract"), Mapping):
        registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
        _validate_candidate_registry_sources(runtime, registry)
        _verify_synth_evidence_bindings(runtime, registry)
        _verify_synth_evidence_bindings(runtime, result)
        _validate_completed_tool_actions(runtime)
        _validate_score_artifacts(runtime, registry, result)
        _validate_candidate_operation_journals(runtime, registry, result)
    manifest_ref = package.get("manifest_ref")
    manifest_hash = package.get("manifest_sha256")
    if not isinstance(manifest_ref, str) or not isinstance(manifest_hash, str):
        raise RuntimeError("terminal V3 package metadata is incomplete")
    manifest_path = _safe_run_ref(runtime, manifest_ref)
    manifest = _read_json_object(manifest_path)
    if _sha256_json(manifest) != manifest_hash:
        raise RuntimeError("terminal V3 package manifest hash mismatch")
    if (
        manifest.get("workflow") != WORKFLOW_NAME
        or manifest.get("task_id") != runtime.task.id
        or manifest.get("status") != result.get("status")
        or manifest.get("final_candidate_id") != result.get("final_candidate_id")
    ):
        raise RuntimeError("terminal V3 package identity mismatch")
    terminal_payload = dict(result)
    terminal_payload.pop("package", None)
    if manifest.get("terminal_payload_sha256") != _sha256_json(terminal_payload):
        raise RuntimeError("terminal V3 result payload hash mismatch")
    node_events = result.get("node_events")
    if (
        not isinstance(node_events, list)
        or manifest.get("node_event_count") != len(node_events)
    ):
        raise RuntimeError("terminal V3 node-event cutoff mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("terminal V3 package artifact list is invalid")
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise RuntimeError("terminal V3 package artifact record is invalid")
        reference = item.get("path")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if (
            not isinstance(reference, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
        ):
            raise RuntimeError("terminal V3 package artifact record is incomplete")
        if reference == "v3_team_report.md" and not verify_report:
            continue
        path = _safe_run_ref(runtime, reference)
        if (
            not path.is_file()
            or path.stat().st_size != size
            or _sha256_file(path) != digest
        ):
            raise RuntimeError(f"terminal V3 package artifact mismatch: {reference}")


def _commit_registry_operation(
    runtime: _Runtime,
    *,
    operation_type: str,
    candidate_id: str,
    expected_best_id: str,
    expected_registry_revision: int,
    round_index: int,
    reason: str,
    registry_updates: Mapping[str, object],
    candidate_updates: Mapping[str, object],
) -> str:
    """Apply an idempotent compare-and-set Candidate decision.

    The prepared record is durable before the Registry mutation.  If the
    process dies after the Registry write but before the committed record, a
    replay recognizes ``v3_last_operation_id`` and only seals the operation.
    A different incumbent fails closed instead of overwriting newer state.
    """

    def stable_value(value: object) -> object:
        if isinstance(value, Mapping):
            return {
                str(key): stable_value(child)
                for key, child in value.items()
                if str(key) not in {"cached", "timestamp"}
            }
        if isinstance(value, list):
            return [stable_value(child) for child in value]
        return value

    stable_registry_updates = stable_value(dict(registry_updates))
    stable_candidate_updates = stable_value(dict(candidate_updates))
    if not isinstance(stable_registry_updates, dict) or not isinstance(
        stable_candidate_updates, dict
    ):
        raise TypeError("Candidate decision updates must be objects")
    request: dict[str, object] = {
        "schema_version": "v3a.candidate-operation.v1",
        "operation_type": operation_type,
        "candidate_id": candidate_id,
        "expected_best_candidate_id": expected_best_id,
        "expected_registry_revision": expected_registry_revision,
        "round_index": round_index,
        "reason": reason,
        "registry_updates": stable_registry_updates,
        "candidate_updates": stable_candidate_updates,
    }
    operation_id = _sha256_json(request)
    request["operation_id"] = operation_id
    root = runtime.run_root / "control" / "candidate_operations"
    prepared = root / f"{operation_id}.prepared.json"
    committed = root / f"{operation_id}.committed.json"
    _write_once_or_verify(prepared, request)

    if committed.exists():
        durable = _read_json_object(committed)
        if durable.get("request") != request:
            raise RuntimeError("Candidate decision journal identity mismatch")
        registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
        candidates = registry.get("candidates")
        candidate = (
            candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
        )
        if (
            registry.get("v3_last_operation_id") != operation_id
            or int(registry.get("v3_revision", 0))
            != expected_registry_revision + 1
            or not isinstance(candidate, Mapping)
            or any(
                registry.get(key) != value
                for key, value in stable_registry_updates.items()
            )
            or any(
                candidate.get(key) != value
                for key, value in stable_candidate_updates.items()
            )
        ):
            raise RuntimeError("REGISTRY_CAS_CONFLICT: committed decision diverged")
        return str(committed.relative_to(runtime.run_root)).replace("\\", "/")

    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    last_operation = registry.get("v3_last_operation_id")
    if last_operation != operation_id:
        current_best = registry.get("best_candidate_id")
        current_revision = int(registry.get("v3_revision", 0))
        if (
            current_best != expected_best_id
            or current_revision != expected_registry_revision
        ):
            raise RuntimeError(
                "REGISTRY_CAS_CONFLICT: expected best/revision "
                f"{expected_best_id}/{expected_registry_revision}, found "
                f"{current_best}/{current_revision}"
            )
        candidates = registry.get("candidates")
        candidate = (
            candidates.get(candidate_id) if isinstance(candidates, dict) else None
        )
        if not isinstance(candidate, dict):
            raise RuntimeError(
                f"REGISTRY_OPERATION_ERROR: missing Candidate {candidate_id}"
            )
        candidate.update(stable_candidate_updates)
        registry.update(stable_registry_updates)
        registry["v3_revision"] = expected_registry_revision + 1
        registry["v3_last_operation_id"] = operation_id
        manager.save_registry(registry)
    candidates = registry.get("candidates")
    candidate = (
        candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    )
    if (
        registry.get("v3_last_operation_id") != operation_id
        or int(registry.get("v3_revision", 0)) != expected_registry_revision + 1
        or not isinstance(candidate, Mapping)
        or any(
            registry.get(key) != value
            for key, value in stable_registry_updates.items()
        )
        or any(
            candidate.get(key) != value
            for key, value in stable_candidate_updates.items()
        )
    ):
        raise RuntimeError("REGISTRY_CAS_CONFLICT: replayed decision diverged")
    durable = {
        "schema_version": "v3a.candidate-operation-commit.v1",
        "request": request,
        "registry_revision": int(registry.get("v3_revision", 0)),
        "registry_sha256": _sha256_json(registry),
    }
    _write_once_or_verify(committed, durable)
    return str(committed.relative_to(runtime.run_root)).replace("\\", "/")


def _validate_candidate_operation_request(
    request: Mapping[str, object],
) -> None:
    """Validate the semantic shape of one durable Candidate decision."""

    operation_type = request.get("operation_type")
    reason = request.get("reason")
    registry_updates = request.get("registry_updates")
    candidate_updates = request.get("candidate_updates")
    if not isinstance(reason, str) or not reason:
        raise RuntimeError("Candidate decision reason is invalid")
    if not isinstance(registry_updates, Mapping) or not isinstance(
        candidate_updates, Mapping
    ):
        raise RuntimeError("Candidate decision updates are invalid")

    if operation_type == "PROMOTE":
        if (
            set(registry_updates) != {"best_candidate_id", "active_candidate_id"}
            or registry_updates.get("best_candidate_id")
            != request.get("candidate_id")
            or registry_updates.get("active_candidate_id") is not None
            or set(candidate_updates) != {"status", "score_ref"}
            or candidate_updates.get("status") != "PROMOTED"
            or not isinstance(candidate_updates.get("score_ref"), str)
        ):
            raise RuntimeError("PROMOTE Candidate decision is malformed")
        return
    if operation_type == "REJECT":
        if (
            dict(registry_updates) != {"active_candidate_id": None}
            or set(candidate_updates)
            != {"status", "rejection_reason", "rejection_score_ref"}
            or candidate_updates.get("status") != "REJECTED"
            or candidate_updates.get("rejection_reason") != reason
            or (
                candidate_updates.get("rejection_score_ref") is not None
                and not isinstance(
                    candidate_updates.get("rejection_score_ref"), str
                )
            )
        ):
            raise RuntimeError("REJECT Candidate decision is malformed")
        return
    if operation_type == "SELECT_FINAL_ATTEMPT":
        if (
            set(registry_updates)
            != {"active_candidate_id", "final_attempt_candidate_id"}
            or registry_updates.get("active_candidate_id") is not None
            or registry_updates.get("final_attempt_candidate_id")
            != request.get("candidate_id")
            or candidate_updates
        ):
            raise RuntimeError(
                "SELECT_FINAL_ATTEMPT Candidate decision is malformed"
            )
        return
    if operation_type == "SELECT_FINAL_FALLBACK":
        if (
            dict(registry_updates)
            != {"final_attempt_candidate_id": request.get("candidate_id")}
            or candidate_updates
        ):
            raise RuntimeError(
                "SELECT_FINAL_FALLBACK Candidate decision is malformed"
            )
        return
    if operation_type == "COMMIT_FINAL":
        required_candidate_updates = {
            "status",
            "final_validation",
            "final_metrics_ref",
            "final_synth_evidence_ref",
            "final_synth_evidence_sha256",
            "final_score_ref",
        }
        if (
            set(registry_updates)
            != {"final_candidate_id", "final_attempt_candidate_id"}
            or any(
                registry_updates.get(key) != request.get("candidate_id")
                for key in registry_updates
            )
            or set(candidate_updates) != required_candidate_updates
            or candidate_updates.get("status") != "FINAL_VERIFIED"
            or not isinstance(candidate_updates.get("final_validation"), Mapping)
            or any(
                not isinstance(candidate_updates.get(key), str)
                or not candidate_updates.get(key)
                for key in (
                    "final_metrics_ref",
                    "final_synth_evidence_ref",
                    "final_synth_evidence_sha256",
                    "final_score_ref",
                )
            )
        ):
            raise RuntimeError("COMMIT_FINAL Candidate decision is malformed")
        return
    raise RuntimeError(f"unknown Candidate decision type: {operation_type}")


def _validate_candidate_operation_journals(
    runtime: _Runtime,
    registry: Mapping[str, object],
    result: Mapping[str, object] | None = None,
) -> list[Path]:
    """Verify the prepared/committed Candidate decision chain.

    Candidate operation files are control-plane evidence, not arbitrary report
    attachments.  The package may seal them only after their identities,
    semantic shapes, revision order, and terminal Registry binding agree.
    """

    root = runtime.run_root / "control" / "candidate_operations"
    registry_revision = registry.get("v3_revision", 0)
    if (
        not isinstance(registry_revision, int)
        or isinstance(registry_revision, bool)
        or registry_revision < 0
    ):
        raise RuntimeError("Candidate Registry revision is invalid")
    if not root.exists():
        if registry_revision != 0 or registry.get("v3_last_operation_id") not in {
            None,
            "",
        }:
            raise RuntimeError("Candidate decision journals are missing")
        return []
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Candidate decision journal root is invalid")

    prepared_by_id: dict[str, Path] = {}
    committed_by_id: dict[str, Path] = {}
    name_pattern = re.compile(
        r"(?P<operation>[0-9a-f]{64})\.(?P<kind>prepared|committed)\.json\Z"
    )
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Candidate decision journal entry is invalid")
        match = name_pattern.fullmatch(path.name)
        if match is None:
            raise RuntimeError(
                f"Candidate decision journal filename is invalid: {path.name}"
            )
        operation_id = match.group("operation")
        collection = (
            prepared_by_id
            if match.group("kind") == "prepared"
            else committed_by_id
        )
        if operation_id in collection:
            raise RuntimeError("duplicate Candidate decision journal identity")
        collection[operation_id] = path
    if set(prepared_by_id) != set(committed_by_id):
        raise RuntimeError("Candidate decision journal chain is incomplete")

    candidates = registry.get("candidates")
    if not isinstance(candidates, Mapping):
        raise RuntimeError("Candidate Registry candidates are invalid")
    by_revision: dict[int, tuple[str, dict[str, object], dict[str, object]]] = {}
    request_keys = {
        "schema_version",
        "operation_type",
        "candidate_id",
        "expected_best_candidate_id",
        "expected_registry_revision",
        "round_index",
        "reason",
        "registry_updates",
        "candidate_updates",
        "operation_id",
    }
    commit_keys = {
        "schema_version",
        "request",
        "registry_revision",
        "registry_sha256",
    }
    for operation_id in sorted(prepared_by_id):
        prepared = _read_json_object(prepared_by_id[operation_id])
        committed = _read_json_object(committed_by_id[operation_id])
        if set(prepared) != request_keys:
            raise RuntimeError("Candidate decision prepared record is malformed")
        unsigned = dict(prepared)
        stored_operation_id = unsigned.pop("operation_id", None)
        if (
            prepared.get("schema_version") != "v3a.candidate-operation.v1"
            or stored_operation_id != operation_id
            or _sha256_json(unsigned) != operation_id
        ):
            raise RuntimeError("Candidate decision prepared identity mismatch")
        candidate_id = prepared.get("candidate_id")
        expected_best_id = prepared.get("expected_best_candidate_id")
        expected_revision = prepared.get("expected_registry_revision")
        round_index = prepared.get("round_index")
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in candidates
            or not isinstance(expected_best_id, str)
            or expected_best_id not in candidates
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or not isinstance(round_index, int)
            or isinstance(round_index, bool)
            or round_index <= 0
        ):
            raise RuntimeError("Candidate decision prepared state is invalid")
        _validate_candidate_operation_request(prepared)
        committed_revision = committed.get("registry_revision")
        registry_sha256 = committed.get("registry_sha256")
        if (
            set(committed) != commit_keys
            or committed.get("schema_version")
            != "v3a.candidate-operation-commit.v1"
            or committed.get("request") != prepared
            or not isinstance(committed_revision, int)
            or isinstance(committed_revision, bool)
            or committed_revision != expected_revision + 1
            or not isinstance(registry_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", registry_sha256) is None
        ):
            raise RuntimeError("Candidate decision committed record is malformed")
        if committed_revision in by_revision:
            raise RuntimeError("duplicate Candidate decision Registry revision")
        by_revision[committed_revision] = (
            operation_id,
            prepared,
            committed,
        )

    expected_revisions = list(range(1, registry_revision + 1))
    if sorted(by_revision) != expected_revisions:
        raise RuntimeError("Candidate decision Registry revision chain is broken")
    if not by_revision:
        if registry.get("v3_last_operation_id") not in {None, ""}:
            raise RuntimeError("Candidate Registry last decision is unjournaled")
        if result is not None and int(result.get("registry_revision", 0)) != 0:
            raise RuntimeError("terminal Candidate decision revision mismatch")
        return []

    for revision in expected_revisions:
        _operation_id, request, _commit = by_revision[revision]
        if request.get("expected_registry_revision") != revision - 1:
            raise RuntimeError("Candidate decision expected revision is not continuous")
    last_operation_id, _last_request, last_commit = by_revision[registry_revision]
    last_ref = str(
        committed_by_id[last_operation_id].relative_to(runtime.run_root)
    ).replace("\\", "/")
    if (
        registry.get("v3_last_operation_id") != last_operation_id
        or last_commit.get("registry_sha256") != _sha256_json(registry)
    ):
        raise RuntimeError("Candidate decision chain does not bind the Registry")
    if result is not None and (
        result.get("registry_revision") != registry_revision
        or result.get("decision_ref") != last_ref
    ):
        raise RuntimeError("terminal result does not bind the last Candidate decision")
    return sorted([*prepared_by_id.values(), *committed_by_id.values()])


def _verify_run_identity(
    runtime: _Runtime, *, require_complete: bool = False
) -> None:
    task_path = runtime.run_root / "v3_task_spec.json"
    config_path = runtime.run_root / "v3_run_config.json"
    if not task_path.exists() and not config_path.exists():
        return
    expected = {
        task_path: _task_spec(runtime.task),
        config_path: _run_config_snapshot(runtime),
    }
    proposal_path = runtime.run_root / "planner" / "proposal_001.json"
    if len(runtime.proposals) == 1 and proposal_path.exists():
        expected[proposal_path] = _proposal_snapshot(runtime)
    for path, wanted in expected.items():
        if not path.exists():
            if require_complete:
                raise RuntimeError(
                    f"existing run identity mismatch: "
                    f"{path.relative_to(runtime.run_root)}"
                )
            continue
        existing = _read_json_object(path) if path.is_file() else None
        historical_backend_only = False
        if (
            path == config_path
            and isinstance(existing, dict)
            and (runtime.run_root / "v3_prototype_result.json").is_file()
        ):
            stored_without_backend = dict(existing)
            wanted_without_backend = dict(wanted)
            stored_backend = stored_without_backend.pop("backend_fingerprint", None)
            wanted_without_backend.pop("backend_fingerprint", None)
            historical_backend_only = bool(
                isinstance(stored_backend, str)
                and stored_backend
                and stored_without_backend == wanted_without_backend
            )
        if existing != wanted and not historical_backend_only:
            raise RuntimeError(
                f"existing run identity mismatch: "
                f"{path.relative_to(runtime.run_root)}"
            )


def _load_terminal_result(runtime: _Runtime) -> dict[str, object] | None:
    result_path = runtime.run_root / "v3_prototype_result.json"
    if not result_path.exists():
        return None
    _verify_run_identity(runtime, require_complete=True)
    result = _read_json_object(result_path)
    backend = result.get("backend")
    stored_config = _read_json_object(runtime.run_root / "v3_run_config.json")
    stored_backend_fingerprint = stored_config.get("backend_fingerprint")
    allowed_backend_fingerprints = {_backend_fingerprint(runtime.backend)}
    if isinstance(stored_backend_fingerprint, str):
        allowed_backend_fingerprints.add(stored_backend_fingerprint)
    if (
        result.get("workflow") != WORKFLOW_NAME
        or result.get("task_id") != runtime.task.id
        or result.get("status") not in {"DONE", "FAILED"}
        or not isinstance(backend, Mapping)
        or backend.get("fingerprint") not in allowed_backend_fingerprints
    ):
        raise RuntimeError("terminal V3 prototype result has an invalid identity")
    _verify_package_manifest(runtime, result, verify_report=False)
    if result.get("package") is None:
        # Legacy A0 terminals predate the sealed package.  They are readable,
        # but must remain byte-for-byte archival evidence rather than being
        # silently rewritten by a newer report renderer.
        return result
    report_path = runtime.run_root / "v3_team_report.md"
    expected_report = _render_team_report(runtime, result)
    try:
        stored_report = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        stored_report = ""
    if stored_report != expected_report:
        _atomic_text(report_path, expected_report)
    _verify_package_manifest(runtime, result, verify_report=True)
    return result


def _budget_affordability(
    runtime: _Runtime,
    *,
    required_calls: Mapping[str, int],
    policy: str,
    required_tokens: int = 0,
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
    if (
        isinstance(required_tokens, bool)
        or not isinstance(required_tokens, int)
        or required_tokens < 0
    ):
        raise ValueError("required_tokens must be a non-negative integer")
    if int(snapshot["tokens_remaining"]) < required_tokens:
        blockers.append(
            f"tokens:{snapshot['tokens_remaining']}<{required_tokens}"
        )
    return {
        "policy": policy,
        "allowed": not blockers,
        "required_calls": dict(required_calls),
        "required_credits": required_credits,
        "credits_remaining": remaining,
        "required_tokens": required_tokens,
        "tokens_remaining": snapshot["tokens_remaining"],
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


def _completed_tool_result(
    runtime: _Runtime,
    result_ref: object,
    *,
    expected_kind: str | None = None,
    expected_candidate_id: str | None = None,
    expected_scope: str | None = None,
) -> ToolResult:
    if not isinstance(result_ref, str) or not result_ref:
        raise RuntimeError("tool result reference is incomplete")
    _budget, server = _server(runtime)
    allowed_backend_fingerprints = {_backend_fingerprint(runtime.backend)}
    stored_config_path = runtime.run_root / "v3_run_config.json"
    if (
        (runtime.run_root / "v3_prototype_result.json").is_file()
        and stored_config_path.is_file()
    ):
        stored_fingerprint = _read_json_object(stored_config_path).get(
            "backend_fingerprint"
        )
        if isinstance(stored_fingerprint, str) and stored_fingerprint:
            allowed_backend_fingerprints.add(stored_fingerprint)
    result = server.load_completed_result(
        result_ref,
        allowed_backend_fingerprints=allowed_backend_fingerprints,
    )
    if expected_kind is not None and result.kind != expected_kind:
        raise RuntimeError("tool result kind binding mismatch")
    if (
        expected_candidate_id is not None
        and result.candidate_id != expected_candidate_id
    ):
        raise RuntimeError("tool result Candidate binding mismatch")
    if expected_scope is not None and result.validation_scope != expected_scope:
        raise RuntimeError("tool result validation-scope binding mismatch")
    return result


def _completed_synth_report(
    runtime: _Runtime,
    result_ref: object,
    *,
    candidate_id: str,
    validation_scope: str,
) -> dict[str, object]:
    result = _completed_tool_result(
        runtime,
        result_ref,
        expected_kind="synth",
        expected_candidate_id=candidate_id,
        expected_scope=validation_scope,
    )
    if not isinstance(result.report, dict):
        raise RuntimeError("completed Synth result has no structured report")
    return dict(result.report)


def _validate_completed_tool_actions(runtime: _Runtime) -> list[ToolResult]:
    ledger, server = _server(runtime)
    allowed_backend_fingerprints = {_backend_fingerprint(runtime.backend)}
    stored_config_path = runtime.run_root / "v3_run_config.json"
    if (
        (runtime.run_root / "v3_prototype_result.json").is_file()
        and stored_config_path.is_file()
    ):
        stored_fingerprint = _read_json_object(stored_config_path).get(
            "backend_fingerprint"
        )
        if isinstance(stored_fingerprint, str) and stored_fingerprint:
            allowed_backend_fingerprints.add(stored_fingerprint)
    results: list[ToolResult] = []
    for event in ledger.events():
        if (
            event.get("state") != "COMPLETED"
            or event.get("kind") not in {"csim", "synth", "cosim"}
        ):
            continue
        result_ref = event.get("result_ref")
        if not isinstance(result_ref, str):
            raise RuntimeError("completed tool action has no result reference")
        results.append(
            server.load_completed_result(
                result_ref,
                allowed_backend_fingerprints=allowed_backend_fingerprints,
            )
        )
    return results


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
    round_index: int | None = None,
    details: Mapping[str, object] | None = None,
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
    if round_index is not None:
        event["round_index"] = round_index
    if details is not None:
        event["details"] = dict(details)
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


def _candidate_source(
    runtime: _Runtime,
    candidate_id: str,
    *,
    registry: Mapping[str, object] | None = None,
    visiting: tuple[str, ...] = (),
) -> bytes:
    if candidate_id in visiting:
        raise RuntimeError("Candidate lineage contains a cycle")
    if registry is None:
        registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
    candidates = registry.get("candidates")
    record = candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    if not isinstance(record, Mapping):
        raise RuntimeError(f"Candidate is missing: {candidate_id}")
    source_ref = record.get("source_ref")
    code_hash = record.get("code_hash")
    if not isinstance(source_ref, str) or not isinstance(code_hash, str):
        raise RuntimeError("Candidate source binding is incomplete")
    source = _safe_run_ref(runtime, source_ref).read_bytes()
    if hashlib.sha256(source).hexdigest() != code_hash:
        raise RuntimeError(f"Candidate source hash mismatch: {candidate_id}")

    parent_id = record.get("parent_id")
    if record.get("kind") == "baseline":
        if parent_id is not None or source != runtime.task.kernel_bytes:
            raise RuntimeError("baseline Candidate diverges from the public task")
        return source
    if not isinstance(parent_id, str) or not parent_id:
        raise RuntimeError("derived Candidate parent binding is incomplete")
    patch_ref = record.get("patch_ref")
    patch_sha256 = record.get("patch_sha256")
    if not isinstance(patch_ref, str) or not isinstance(patch_sha256, str):
        raise RuntimeError("derived Candidate Patch binding is incomplete")
    patch_path = _safe_run_ref(runtime, patch_ref)
    try:
        patch_text = patch_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"cannot read Candidate Patch: {candidate_id}") from exc
    if hashlib.sha256(patch_text.encode("utf-8")).hexdigest() != patch_sha256:
        raise RuntimeError(f"Candidate Patch hash mismatch: {candidate_id}")

    parent_source = _candidate_source(
        runtime,
        parent_id,
        registry=registry,
        visiting=(*visiting, candidate_id),
    )
    try:
        application = apply_unified_diff(
            parent_source,
            patch_text,
            kernel_name=runtime.task.kernel_name,
            limits=runtime.patch_limits,
        )
    except PatchValidationError as exc:
        raise RuntimeError(
            f"Candidate Patch no longer applies to its parent: {candidate_id}"
        ) from exc
    if application.patched_bytes != source or application.patched_sha256 != code_hash:
        raise RuntimeError(
            f"Candidate source diverges from parent plus Patch: {candidate_id}"
        )

    proposal = _proposal_for_candidate(runtime, candidate_id)
    if proposal is None or proposal.patch != patch_text:
        raise RuntimeError(
            f"Candidate Patch diverges from Planner output: {candidate_id}"
        )
    _validate_live_planner_candidate_binding(
        runtime, registry, record, proposal
    )
    metadata_ref = f"candidates/{candidate_id}/candidate.json"
    immutable_metadata = _read_json_object(_safe_run_ref(runtime, metadata_ref))
    mutable_fields = {"status", "validation", "metrics_ref", "credits_used"}
    for name, value in immutable_metadata.items():
        if name not in mutable_fields and record.get(name) != value:
            raise RuntimeError(
                f"Candidate immutable metadata mismatch: {candidate_id}.{name}"
            )
    return source


def _validate_candidate_registry_sources(
    runtime: _Runtime, registry: Mapping[str, object]
) -> None:
    candidates = registry.get("candidates")
    if not isinstance(candidates, Mapping):
        raise RuntimeError("Candidate Registry is missing candidates")
    for candidate_id in sorted(str(item) for item in candidates):
        _candidate_source(runtime, candidate_id, registry=registry)


def _save_validation(
    runtime: _Runtime,
    candidate_id: str,
    stage: str,
    record: Mapping[str, object],
    *,
    metrics_ref: str | None = None,
    synth_evidence_ref: str | None = None,
    synth_evidence_sha256: str | None = None,
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
    if synth_evidence_ref is not None:
        candidate["synth_evidence_ref"] = synth_evidence_ref
    if synth_evidence_sha256 is not None:
        candidate["synth_evidence_sha256"] = synth_evidence_sha256
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
    round_index: int | None = None,
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
            round_index=round_index,
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
        round_index=round_index,
    )
    return result, record, event


def _validated_validation(
    runtime: _Runtime,
    candidate_id: str,
    validation: object,
) -> dict[str, dict[str, object]]:
    if not isinstance(validation, Mapping):
        raise ValueError(f"Candidate {candidate_id} has no validation state")
    records = {
        stage: dict(record) if isinstance(record, Mapping) else {"status": "NOT_RUN"}
        for stage, record in validation.items()
    }
    for stage, record in records.items():
        action_id = record.get("action_id")
        result_ref = record.get("result_ref")
        if not isinstance(action_id, str):
            continue
        scope = str(record.get("validation_scope", "exploration"))
        result = _completed_tool_result(
            runtime,
            result_ref,
            expected_kind=stage,
            expected_candidate_id=candidate_id,
            expected_scope=scope,
        )
        expected = _validation_record(result) | {"validation_scope": scope}
        if result.action_id != action_id or stable_validation(record) != stable_validation(
            expected
        ):
            raise RuntimeError(
                f"Candidate validation diverges from completed action: "
                f"{candidate_id}.{stage}"
            )
    return records


def _registry_validation(
    runtime: _Runtime, candidate_id: str
) -> dict[str, dict[str, object]]:
    registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
    candidate = registry["candidates"][candidate_id]
    return _validated_validation(runtime, candidate_id, candidate.get("validation"))


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


def _recompute_exploration_score(
    runtime: _Runtime,
    registry: Mapping[str, object],
    *,
    candidate_id: str,
    baseline_metrics: Mapping[str, object],
    provisional_cosim: bool,
    include_proposal: bool,
) -> CandidateScore:
    candidates = registry.get("candidates")
    candidate = (
        candidates.get(candidate_id) if isinstance(candidates, Mapping) else None
    )
    if not isinstance(candidate, Mapping):
        raise RuntimeError(f"score Candidate is missing: {candidate_id}")
    metrics = _completed_synth_report(
        runtime,
        candidate.get("metrics_ref"),
        candidate_id=candidate_id,
        validation_scope="exploration",
    )
    validation = _registry_validation(runtime, candidate_id)
    if provisional_cosim:
        validation = dict(validation)
        validation["cosim"] = {"status": "NOT_RUN"}
    return _score(
        runtime,
        candidate_id=candidate_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=metrics,
        validation=validation,
        clock=_clock_constraint(metrics, runtime.config.minimum_frequency_mhz),
        proposal=(
            _proposal_for_candidate(runtime, candidate_id)
            if include_proposal
            else None
        ),
        provisional_cosim=provisional_cosim,
    )


def _validate_score_artifacts(
    runtime: _Runtime,
    registry: Mapping[str, object],
    result: Mapping[str, object],
) -> list[Path]:
    score_paths = sorted((runtime.run_root / "scores").glob("*.json"))
    if not score_paths:
        return []
    baseline_id = result.get("baseline_candidate_id")
    if not isinstance(baseline_id, str):
        raise RuntimeError("score validation has no baseline Candidate")
    baseline_metrics = _completed_synth_report(
        runtime,
        result.get("baseline_metrics_ref"),
        candidate_id=baseline_id,
        validation_scope="exploration",
    )
    validated: list[Path] = []
    pattern = re.compile(
        r"(?P<candidate>candidate_\d+)(?:"
        r"(?P<incumbent>\.round_\d+\.incumbent)|"
        r"(?P<pre>\.pre_cosim)|"
        r"(?P<verified>\.verified)|"
        r"(?P<final>\.final))\.json\Z"
    )
    completed_final_results: dict[tuple[str, str], ToolResult] = {
        (item.candidate_id, item.kind): item
        for item in _validate_completed_tool_actions(runtime)
        if item.validation_scope == "final"
    }
    attempted_final_ids = {
        str(item) for item in result.get("final_attempted_candidate_ids", [])
    }
    for score_path in score_paths:
        if score_path.is_symlink():
            raise RuntimeError("score artifact must not be a symbolic link")
        match = pattern.fullmatch(score_path.name)
        if match is None:
            raise RuntimeError(f"unrecognized score artifact: {score_path.name}")
        candidate_id = match.group("candidate")
        raw_score = _read_json_object(score_path)
        CandidateScore.from_dict(raw_score)
        if match.group("final") is not None:
            if candidate_id not in attempted_final_ids:
                raise RuntimeError("final score Candidate was never attempted")
            stage_results = {
                stage: completed_final_results.get((candidate_id, stage))
                for stage in ("csim", "synth", "cosim")
            }
            if any(item is None for item in stage_results.values()):
                raise RuntimeError("final score lacks a complete tool closure")
            synth_result = stage_results["synth"]
            assert synth_result is not None
            if not isinstance(synth_result.report, dict):
                raise RuntimeError("final score Synth report is missing")
            final_metrics = dict(synth_result.report)
            final_validation = {
                stage: _validation_record(tool_result) | {
                    "validation_scope": "final"
                }
                for stage, tool_result in stage_results.items()
                if tool_result is not None
            }
            expected = _score(
                runtime,
                candidate_id=candidate_id,
                baseline_metrics=baseline_metrics,
                candidate_metrics=final_metrics,
                validation=final_validation,
                clock=_clock_constraint(
                    final_metrics, runtime.config.minimum_frequency_mhz
                ),
                proposal=_proposal_for_candidate(runtime, candidate_id),
                provisional_cosim=False,
            )
        else:
            expected = _recompute_exploration_score(
                runtime,
                registry,
                candidate_id=candidate_id,
                baseline_metrics=baseline_metrics,
                provisional_cosim=match.group("pre") is not None,
                include_proposal=(
                    match.group("pre") is not None
                    or match.group("verified") is not None
                ),
            )
        if raw_score != expected.to_dict():
            raise RuntimeError(f"score artifact semantic mismatch: {score_path.name}")
        validated.append(score_path)
    return validated


def _initialize(runtime: _Runtime, _state: V3PrototypeState) -> V3PrototypeState:
    runtime.run_root.mkdir(parents=True, exist_ok=True)
    _write_once_or_verify(
        runtime.run_root / "v3_graph_schema.json", _checkpoint_schema_snapshot()
    )
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
    if any(
        proposal.input_tokens
        or proposal.output_tokens
        or proposal.cached_input_tokens
        for proposal in runtime.proposals
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
        "final_attempt_count": 0,
        "final_attempted_candidate_ids": [],
        "final_validation": _initial_validation(),
        "cosim_gate": {
            "eligible": False,
            "reason": "NOT_EVALUATED",
        },
        "budget_gate": budget_gate,
        "last_tool_ok": allowed,
        "round_index": 1,
        "rounds_completed": 0,
        "no_improvement_rounds": 0,
        "last_round_improved": False,
        "exploration_stop_reason": "RUNNING",
        "registry_revision": int(registry.get("v3_revision", 0)),
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
    synth_evidence_ref, synth_evidence_sha256 = _write_synth_evidence(
        runtime, result, candidate_id=candidate_id
    )
    _save_validation(
        runtime,
        candidate_id,
        "synth",
        record,
        metrics_ref=metrics_ref,
        synth_evidence_ref=synth_evidence_ref or None,
        synth_evidence_sha256=synth_evidence_sha256 or None,
    )
    clock = _clock_constraint(
        result.report if result is not None else None,
        runtime.config.minimum_frequency_mhz,
    )
    ok = bool(result is not None and result.ok and metrics_ref and clock["passed"])
    if synth_evidence_ref:
        event["details"] = {
            "synth_evidence_ref": synth_evidence_ref,
            "synth_evidence_sha256": synth_evidence_sha256,
        }
    return {
        "baseline_metrics_ref": metrics_ref or "",
        "best_metrics_ref": metrics_ref or "",
        "baseline_synth_evidence_ref": synth_evidence_ref,
        "baseline_synth_evidence_sha256": synth_evidence_sha256,
        "best_synth_evidence_ref": synth_evidence_ref,
        "best_synth_evidence_sha256": synth_evidence_sha256,
        "baseline_clock": clock,
        "best_clock": clock,
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
    round_index = int(state.get("round_index", 1))
    no_improvement = int(state.get("no_improvement_rounds", 0))
    planner_round_limit = (
        runtime.max_planner_rounds
        if runtime.live_planner is not None
        else len(runtime.proposals)
    )
    if round_index > planner_round_limit:
        gate: dict[str, object] = {
            "policy": "scripted_proposals",
            "allowed": False,
            "required_calls": {},
            "required_credits": 0,
            "blockers": ["no_more_distinct_proposals"],
        }
        reason = "MAX_OPTIMIZATION_ROUNDS"
    elif no_improvement >= runtime.max_no_improvement_rounds:
        gate = {
            "policy": "no_improvement_limit",
            "allowed": False,
            "required_calls": {},
            "required_credits": 0,
            "blockers": [
                f"no_improvement:{no_improvement}>="
                f"{runtime.max_no_improvement_rounds}"
            ],
        }
        reason = "NO_IMPROVEMENT_LIMIT"
    else:
        required = {
            kind: count * 2 for kind, count in _FULL_CLOSURE_CALLS.items()
        }
        required_tokens = 0
        if runtime.live_planner is not None:
            prepared = runtime.live_planner.prepare(
                _build_round_planner_input(runtime, state)
            )
            if not isinstance(prepared, PreparedPlannerCall):
                raise RuntimeError(
                    "live Planner prepare() returned an invalid request"
                )
            required["llm"] = 1
            required_tokens = prepared.estimated_tokens
        gate = _budget_affordability(
            runtime,
            required_calls=required,
            policy="candidate_exploration_plus_final_closure",
            required_tokens=required_tokens,
        )
        reason = (
            "ROUND_BUDGET_AVAILABLE"
            if gate["allowed"] is True
            else "ROUND_SKIPPED_FINAL_RESERVE"
        )
    allowed = gate["allowed"] is True
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
        round_index=round_index,
    )
    update: V3PrototypeState = {
        "budget_gate": gate,
        "last_tool_ok": allowed,
        "node_events": [event],
    }
    if not allowed and int(state.get("rounds_completed", 0)) == 0:
        update["cosim_gate"] = {"eligible": False, "reason": reason}
    if not allowed:
        update["exploration_stop_reason"] = reason
    return update


def _plan_candidate(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    round_index = int(state.get("round_index", 1))
    input_ref = f"planner/inputs/round_{round_index:03d}.json"
    input_path = runtime.run_root / input_ref
    current_input = _build_round_planner_input(runtime, state)
    if input_path.exists() and runtime.live_planner is not None:
        planner_input = _read_json_object(_safe_run_ref(runtime, input_ref))
        try:
            validate_planner_input(planner_input)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"durable live Planner input is invalid: {exc}"
            ) from exc
        bindings: list[Mapping[str, object]] = []
        for started_path in sorted(
            (runtime.run_root / "control" / "live_planner_actions").glob(
                "*.started.json"
            )
        ):
            started_ref = str(
                started_path.relative_to(runtime.run_root)
            ).replace("\\", "/")
            started = _read_json_object(_safe_run_ref(runtime, started_ref))
            request = started.get("request")
            if isinstance(request, Mapping) and request.get("input_ref") == input_ref:
                bindings.append(request)
        if not bindings:
            if planner_input != current_input:
                raise RuntimeError(
                    "unbound live Planner input diverges from current state"
                )
        else:
            if (
                len(bindings) != 1
                or bindings[0].get("input_sha256")
                != canonical_sha256(planner_input)
            ):
                raise RuntimeError("live Planner input action binding mismatch")
            if _planner_recovery_projection(
                planner_input
            ) != _planner_recovery_projection(current_input):
                raise RuntimeError(
                    "bound live Planner input diverges from current stable state"
                )
    else:
        planner_input = current_input
    input_sha256 = canonical_sha256(planner_input)
    _write_once_or_verify(input_path, planner_input)
    live_result: PlannerActionResult | None = None
    selection_metrics_digest: str | None = None
    if runtime.live_planner is not None:
        registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
        candidates = registry.get("candidates")
        incumbent = (
            candidates.get(state["best_candidate_id"])
            if isinstance(candidates, Mapping)
            else None
        )
        if not isinstance(incumbent, Mapping) or not isinstance(
            incumbent.get("code_hash"), str
        ):
            raise RuntimeError("live Planner incumbent code binding is missing")
        budget = BudgetLedger(
            runtime.run_root / "budget_ledger.jsonl", runtime.config.budget
        )
        live_result = PlannerActionJournal(
            runtime.run_root, budget
        ).execute_or_recover(
            runtime.live_planner,
            planner_input,
            input_ref=input_ref,
            input_sha256=input_sha256,
            candidate_id=state["best_candidate_id"],
            code_hash=str(incumbent["code_hash"]),
        )
        proposal = live_result.proposal
        request_audit = _read_json_object(
            _safe_run_ref(runtime, live_result.request_ref)
        )
        if canonical_sha256(request_audit) != live_result.request_sha256:
            raise RuntimeError("live Planner request audit hash mismatch")
        adapter_request = request_audit.get("request")
        selection = (
            adapter_request.get("selection")
            if isinstance(adapter_request, Mapping)
            else None
        )
        raw_metrics_digest = (
            selection.get("metrics_digest")
            if isinstance(selection, Mapping)
            else None
        )
        if raw_metrics_digest is not None:
            if (
                not isinstance(raw_metrics_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", raw_metrics_digest) is None
            ):
                raise RuntimeError(
                    "live Planner selection metrics digest is invalid"
                )
            selection_metrics_digest = raw_metrics_digest
        planner_fingerprint = (
            "live-planner-durable-projection-v1:" + live_result.action_id
        )
        replay_policy = "DURABLE_RESULT"
    else:
        planner = ScriptedPlanner(runtime.proposals)
        proposal = planner.plan(planner_input)
        planner_fingerprint = planner.fingerprint()
        replay_policy = planner.replay_policy
    request = planner_action_request(
        planner_fingerprint=planner_fingerprint,
        planner_input_ref=input_ref,
        planner_input_sha256=input_sha256,
        replay_policy=replay_policy,
    )
    action_id = planner_action_id(request)
    action_root = runtime.run_root / "control" / "planner_actions"
    started = request | {"action_id": action_id, "status": "STARTED"}
    _write_once_or_verify(action_root / f"{action_id}.started.json", started)

    # The scripted path is deterministic.  The live path projects only a
    # previously persisted, Ledger-bound outcome and never repeats the remote
    # provider call at this compatibility boundary.
    output = build_planner_output(
        action_id=action_id,
        input_sha256=input_sha256,
        proposal=proposal,
    )
    output_ref = f"planner/outputs/{action_id}.json"
    output_sha256 = canonical_sha256(output)
    _write_once_or_verify(runtime.run_root / output_ref, output)
    output_proposal = _load_planner_output_only(
        runtime,
        output_ref=output_ref,
        output_sha256=output_sha256,
        action_id=action_id,
        input_sha256=input_sha256,
    )
    if proposal_payload(output_proposal) != proposal_payload(proposal):
        raise RuntimeError("deterministic Planner replay diverged")

    # Keep the A0 projection for old reports and completed-run identity checks;
    # all executable downstream decisions consume the versioned output above.
    proposal_ref = f"planner/proposal_{round_index:03d}.json"
    value = _proposal_snapshot(
        runtime,
        proposal=output_proposal,
        parent_candidate_id=state["best_candidate_id"],
        round_index=round_index,
    )
    _write_once_or_verify(runtime.run_root / proposal_ref, value)
    _write_once_or_verify(
        action_root / f"{action_id}.completed.json",
        request
        | {
            "action_id": action_id,
            "status": "COMPLETED",
            "outcome": "PROPOSAL",
            "result_ref": output_ref,
            "result_sha256": output_sha256,
            "legacy_projection_ref": proposal_ref,
            "legacy_projection_sha256": _sha256_json(value),
        },
    )
    durable_proposal = _validate_planner_binding(
        runtime,
        action_id=action_id,
        input_ref=input_ref,
        input_sha256=input_sha256,
        output_ref=output_ref,
        output_sha256=output_sha256,
        legacy_projection_ref=proposal_ref,
    )
    if proposal_payload(durable_proposal) != proposal_payload(proposal):
        raise RuntimeError("durable Planner provenance diverged")
    event = _event(
        runtime,
        node="plan_candidate",
        phase="OPTIMIZE",
        candidate_id=state["best_candidate_id"],
        action=(
            "live_planner_proposal"
            if live_result is not None
            else "scripted_planner_proposal"
        ),
        why=durable_proposal.hypothesis or "Apply the configured prototype Patch.",
        outcome="PROPOSAL_READY",
        result_ref=output_ref,
        round_index=round_index,
        details={
            "planner_action_id": action_id,
            "planner_input_ref": input_ref,
            "planner_input_sha256": input_sha256,
            "planner_output_ref": output_ref,
            "planner_output_sha256": output_sha256,
            "legacy_projection_ref": proposal_ref,
            "proposal_decision": {
                "provider": durable_proposal.provider,
                "model": durable_proposal.model,
                "change_class": durable_proposal.change_class,
                "hypothesis": durable_proposal.hypothesis,
                "expected_effect": durable_proposal.expected_effect,
                "risk": durable_proposal.risk,
                "required_validation": list(
                    durable_proposal.required_validation
                ),
                "patch_sha256": hashlib.sha256(
                    durable_proposal.patch.encode("utf-8")
                ).hexdigest(),
                "input_tokens": durable_proposal.input_tokens,
                "output_tokens": durable_proposal.output_tokens,
                "cached_input_tokens": durable_proposal.cached_input_tokens,
                "request_id": durable_proposal.request_id,
                "duration_seconds": durable_proposal.duration_seconds,
                "selection_metrics_digest": selection_metrics_digest,
            },
            **(
                {
                    "live_planner_action_id": live_result.action_id,
                    "live_planner_request_ref": live_result.request_ref,
                    "live_planner_request_sha256": live_result.request_sha256,
                    "live_planner_output_ref": live_result.output_ref,
                    "live_planner_output_sha256": live_result.output_sha256,
                    "live_planner_started_ref": live_result.started_ref,
                    "live_planner_completed_ref": live_result.completed_ref,
                    "live_planner_cached": live_result.cached,
                }
                if live_result is not None
                else {}
            ),
        },
    )
    update: V3PrototypeState = {
        "phase": "OPTIMIZE",
        "planner_ref": proposal_ref,
        "planner_action_id": action_id,
        "planner_input_ref": input_ref,
        "planner_input_sha256": input_sha256,
        "planner_output_ref": output_ref,
        "planner_output_sha256": output_sha256,
        "planner_selection_metrics_digest": selection_metrics_digest or "",
        "node_events": [event],
    }
    if live_result is not None:
        update.update(
            {
                "live_planner_action_id": live_result.action_id,
                "live_planner_request_ref": live_result.request_ref,
                "live_planner_request_sha256": live_result.request_sha256,
                "live_planner_output_ref": live_result.output_ref,
                "live_planner_output_sha256": live_result.output_sha256,
                "live_planner_started_ref": live_result.started_ref,
                "live_planner_completed_ref": live_result.completed_ref,
            }
        )
    return update


def _materialize_candidate(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    parent_id = state["best_candidate_id"]
    round_index = int(state.get("round_index", 1))
    proposal = _current_proposal(runtime, state)
    manager = CandidateManager(runtime.run_root, runtime.task)
    registry = manager.load_registry()
    patch_sha256 = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
    candidates = registry.get("candidates")
    duplicate_id = None
    duplicate_record: Mapping[str, object] | None = None
    if isinstance(candidates, Mapping):
        for candidate_id, candidate in candidates.items():
            if (
                isinstance(candidate, Mapping)
                and candidate.get("parent_id") == parent_id
                and candidate.get("patch_sha256") == patch_sha256
            ):
                duplicate_id = str(candidate_id)
                duplicate_record = candidate
                break
    replaying_materialization = bool(
        duplicate_id is not None
        and duplicate_record is not None
        and duplicate_record.get("round_index") == round_index
        and duplicate_record.get("planner_ref") == state.get("planner_ref")
        and duplicate_record.get("planner_output_ref")
        == state.get("planner_output_ref")
        and duplicate_record.get("planner_output_sha256")
        == state.get("planner_output_sha256")
        and duplicate_record.get("status") == "MATERIALIZED"
        and registry.get("active_candidate_id") == duplicate_id
    )
    if duplicate_id is not None and not replaying_materialization:
        reason = "DUPLICATE_PROPOSAL"
        event = _event(
            runtime,
            node="materialize_candidate",
            phase="OPTIMIZE",
            candidate_id=None,
            action="deduplicate_patch_before_candidate_allocation",
            why=f"The same parent/Patch was already evaluated as {duplicate_id}.",
            outcome=reason,
            round_index=round_index,
        )
        return {
            "active_candidate_id": None,
            "last_tool_ok": False,
            "last_tool_reason": reason,
            "cosim_gate": {"eligible": False, "reason": reason},
            "node_events": [event],
        }
    source = _candidate_source(runtime, parent_id)
    try:
        application = apply_unified_diff(
            source,
            proposal.patch,
            kernel_name=runtime.task.kernel_name,
            limits=runtime.patch_limits,
        )
    except PatchValidationError as exc:
        reason = "PATCH_POLICY_REJECTED"
        event = _event(
            runtime,
            node="materialize_candidate",
            phase="OPTIMIZE",
            candidate_id=None,
            action="validate_patch_before_candidate_allocation",
            why=str(exc),
            outcome=reason,
            round_index=round_index,
        )
        return {
            "active_candidate_id": None,
            "last_tool_ok": False,
            "last_tool_reason": reason,
            "cosim_gate": {"eligible": False, "reason": reason},
            "node_events": [event],
        }
    materialized = manager.materialize(
        registry,
        parent_id=parent_id,
        patch_text=proposal.patch,
        application=application,
        kind="optimization",
        metadata={
            "planner_ref": state["planner_ref"],
            "planner_action_id": state["planner_action_id"],
            "planner_input_ref": state["planner_input_ref"],
            "planner_input_sha256": state["planner_input_sha256"],
            "planner_output_ref": state["planner_output_ref"],
            "planner_output_sha256": state["planner_output_sha256"],
            "proposal_sha256": canonical_sha256(proposal_payload(proposal)),
            "round_index": round_index,
            "provider": proposal.provider,
            "model": proposal.model,
            "change_class": proposal.change_class,
            "selection_metrics_digest": state.get(
                "planner_selection_metrics_digest"
            ),
            "hypothesis": proposal.hypothesis,
            "expected_effect": proposal.expected_effect,
            "risk": proposal.risk,
            "required_validation": list(proposal.required_validation),
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
            **(
                {
                    "live_planner_action_id": state["live_planner_action_id"],
                    "live_planner_request_ref": state[
                        "live_planner_request_ref"
                    ],
                    "live_planner_request_sha256": state[
                        "live_planner_request_sha256"
                    ],
                    "live_planner_output_ref": state[
                        "live_planner_output_ref"
                    ],
                    "live_planner_output_sha256": state[
                        "live_planner_output_sha256"
                    ],
                    "live_planner_started_ref": state[
                        "live_planner_started_ref"
                    ],
                    "live_planner_completed_ref": state[
                        "live_planner_completed_ref"
                    ],
                }
                if runtime.live_planner is not None
                else {}
            ),
        },
    )
    event = _event(
        runtime,
        node="materialize_candidate",
        phase="OPTIMIZE",
        candidate_id=materialized.candidate_id,
        action="validate_patch_and_create_immutable_candidate",
        why=(
            "Recover the Candidate atomically materialized by this same Graph round."
            if replaying_materialization
            else "The planner may propose code, but only the Candidate manager may materialize it."
        ),
        outcome=("MATERIALIZATION_REPLAYED" if replaying_materialization else "MATERIALIZED"),
        result_ref=f"candidates/{materialized.candidate_id}/candidate.json",
        round_index=round_index,
    )
    return {
        "active_candidate_id": materialized.candidate_id,
        "last_tool_ok": True,
        "candidate_metrics_ref": "",
        "candidate_synth_evidence_ref": "",
        "candidate_synth_evidence_sha256": "",
        "candidate_score_ref": "",
        "candidate_clock": {},
        "cosim_gate": {"eligible": False, "reason": "NOT_EVALUATED"},
        "node_events": [event],
    }


def _record_rejected_proposal(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    round_index = int(state.get("round_index", 1))
    reason = str(state.get("last_tool_reason", "PATCH_POLICY_REJECTED"))
    rejection_ref = f"control/proposal_rejections/round_{round_index:03d}.json"
    record = {
        "schema_version": "v3a.proposal-rejection.v1",
        "round_index": round_index,
        "parent_candidate_id": state["best_candidate_id"],
        "planner_ref": state.get("planner_ref"),
        "planner_action_id": state.get("planner_action_id"),
        "planner_input_ref": state.get("planner_input_ref"),
        "planner_input_sha256": state.get("planner_input_sha256"),
        "planner_output_ref": state.get("planner_output_ref"),
        "planner_output_sha256": state.get("planner_output_sha256"),
        "change_class": _current_proposal(runtime, state).change_class,
        "selection_metrics_digest": state.get(
            "planner_selection_metrics_digest"
        ),
        "reason": reason,
    }
    _write_once_or_verify(runtime.run_root / rejection_ref, record)
    event = _event(
        runtime,
        node="record_rejected_proposal",
        phase="DECIDE",
        candidate_id=None,
        action="record_unmaterialized_proposal_rejection",
        why="An invalid proposal consumes no Candidate ID or Vitis credits.",
        outcome=reason,
        result_ref=rejection_ref,
        round_index=round_index,
    )
    return {
        "phase": "DECIDE",
        "active_candidate_id": None,
        "candidate_metrics_ref": "",
        "candidate_synth_evidence_ref": "",
        "candidate_synth_evidence_sha256": "",
        "candidate_score_ref": "",
        "candidate_clock": {},
        "last_round_improved": False,
        "decision_ref": rejection_ref,
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
        round_index=int(state.get("round_index", 1)),
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
        round_index=int(state.get("round_index", 1)),
    )
    metrics_ref = result.result_ref if result is not None and result.report is not None else None
    synth_evidence_ref, synth_evidence_sha256 = _write_synth_evidence(
        runtime, result, candidate_id=candidate_id
    )
    _save_validation(
        runtime,
        candidate_id,
        "synth",
        record,
        metrics_ref=metrics_ref,
        synth_evidence_ref=synth_evidence_ref or None,
        synth_evidence_sha256=synth_evidence_sha256 or None,
    )
    clock = _clock_constraint(
        result.report if result is not None else None,
        runtime.config.minimum_frequency_mhz,
    )
    ok = bool(result is not None and result.ok and metrics_ref and clock["passed"])
    if synth_evidence_ref:
        event["details"] = {
            "synth_evidence_ref": synth_evidence_ref,
            "synth_evidence_sha256": synth_evidence_sha256,
        }
    update: V3PrototypeState = {
        "candidate_metrics_ref": metrics_ref or "",
        "candidate_synth_evidence_ref": synth_evidence_ref,
        "candidate_synth_evidence_sha256": synth_evidence_sha256,
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
    incumbent_id = state["best_candidate_id"]
    candidate_id = state["active_candidate_id"]
    proposal = _current_proposal(runtime, state)
    baseline_metrics = _completed_synth_report(
        runtime,
        state["baseline_metrics_ref"],
        candidate_id=baseline_id,
        validation_scope="exploration",
    )
    incumbent_metrics = _completed_synth_report(
        runtime,
        state["best_metrics_ref"],
        candidate_id=incumbent_id,
        validation_scope="exploration",
    )
    candidate_metrics = _completed_synth_report(
        runtime,
        state["candidate_metrics_ref"],
        candidate_id=candidate_id,
        validation_scope="exploration",
    )
    incumbent_score = _score(
        runtime,
        candidate_id=incumbent_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=incumbent_metrics,
        validation=_registry_validation(runtime, incumbent_id),
        clock=state["best_clock"],
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
        proposal=proposal,
        provisional_cosim=True,
    )
    round_index = int(state.get("round_index", 1))
    baseline_score_ref = (
        f"scores/{incumbent_id}.round_{round_index:03d}.incumbent.json"
    )
    candidate_score_ref = f"scores/{candidate_id}.pre_cosim.json"
    _atomic_json(runtime.run_root / baseline_score_ref, incumbent_score.to_dict())
    _atomic_json(runtime.run_root / candidate_score_ref, candidate_score.to_dict())
    policy = (
        "official_score_gate"
        if runtime.scoring.official_score_enabled
        else "ppa_gate"
    )
    gate = evaluate_exploration_cosim_gate(
        candidate_score,
        incumbent_score,
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
        round_index=round_index,
        details={
            "incumbent_id": incumbent_id,
            "candidate_id": candidate_id,
            "incumbent_latency": incumbent_metrics.get("latency"),
            "candidate_latency": candidate_metrics.get("latency"),
            "incumbent_official_score": incumbent_score.official_score,
            "candidate_official_score": candidate_score.official_score,
            "incumbent_ppa_cost": incumbent_score.ppa_cost,
            "candidate_ppa_cost": candidate_score.ppa_cost,
            "decision": gate.reason,
        },
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
        round_index=int(state.get("round_index", 1)),
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
        round_index=int(state.get("round_index", 1)),
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
    proposal = _current_proposal(runtime, state)
    baseline_metrics = _completed_synth_report(
        runtime,
        state["baseline_metrics_ref"],
        candidate_id=state["baseline_candidate_id"],
        validation_scope="exploration",
    )
    candidate_metrics = _completed_synth_report(
        runtime,
        state["candidate_metrics_ref"],
        candidate_id=candidate_id,
        validation_scope="exploration",
    )
    score = _score(
        runtime,
        candidate_id=candidate_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        validation=_registry_validation(runtime, candidate_id),
        clock=state["candidate_clock"],
        proposal=proposal,
        provisional_cosim=False,
    )
    score_ref = f"scores/{candidate_id}.verified.json"
    _atomic_json(runtime.run_root / score_ref, score.to_dict())
    decision_ref = _commit_registry_operation(
        runtime,
        operation_type="PROMOTE",
        candidate_id=candidate_id,
        expected_best_id=state["best_candidate_id"],
        expected_registry_revision=int(state.get("registry_revision", 0)),
        round_index=int(state.get("round_index", 1)),
        reason=str(state["cosim_gate"].get("reason", "STRICT_IMPROVEMENT")),
        registry_updates={
            "best_candidate_id": candidate_id,
            "active_candidate_id": None,
        },
        candidate_updates={"status": "PROMOTED", "score_ref": score_ref},
    )
    event = _event(
        runtime,
        node="promote_candidate",
        phase="DECIDE",
        candidate_id=candidate_id,
        action="promote_verified_candidate",
        why=str(state["cosim_gate"].get("reason", "STRICT_IMPROVEMENT")),
        outcome="PROMOTED",
        result_ref=decision_ref,
        round_index=int(state.get("round_index", 1)),
    )
    return {
        "phase": "DECIDE",
        "active_candidate_id": None,
        "best_candidate_id": candidate_id,
        "best_metrics_ref": state["candidate_metrics_ref"],
        "best_synth_evidence_ref": state["candidate_synth_evidence_ref"],
        "best_synth_evidence_sha256": state[
            "candidate_synth_evidence_sha256"
        ],
        "best_clock": state["candidate_clock"],
        "candidate_score_ref": score_ref,
        "last_round_improved": True,
        "decision_ref": decision_ref,
        "registry_revision": int(state.get("registry_revision", 0)) + 1,
        "node_events": [event],
    }


def _reject_candidate(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    incumbent_id = state["best_candidate_id"]
    candidate_id = state["active_candidate_id"]
    reason = str(state.get("cosim_gate", {}).get("reason", "CANDIDATE_VALIDATION_FAILED"))
    decision_ref = _commit_registry_operation(
        runtime,
        operation_type="REJECT",
        candidate_id=candidate_id,
        expected_best_id=incumbent_id,
        expected_registry_revision=int(state.get("registry_revision", 0)),
        round_index=int(state.get("round_index", 1)),
        reason=reason,
        registry_updates={"active_candidate_id": None},
        candidate_updates={
            "status": "REJECTED",
            "rejection_reason": reason,
            "rejection_score_ref": state.get("candidate_score_ref"),
        },
    )
    event = _event(
        runtime,
        node="reject_candidate",
        phase="DECIDE",
        candidate_id=candidate_id,
        action="reject_candidate_and_preserve_incumbent",
        why=reason,
        outcome="REJECTED",
        result_ref=decision_ref,
        round_index=int(state.get("round_index", 1)),
    )
    return {
        "phase": "DECIDE",
        "active_candidate_id": None,
        "best_candidate_id": incumbent_id,
        "candidate_metrics_ref": "",
        "candidate_synth_evidence_ref": "",
        "candidate_synth_evidence_sha256": "",
        "candidate_score_ref": "",
        "candidate_clock": {},
        "last_round_improved": False,
        "decision_ref": decision_ref,
        "registry_revision": int(state.get("registry_revision", 0)) + 1,
        "node_events": [event],
    }


def _advance_round(runtime: _Runtime, state: V3PrototypeState) -> V3PrototypeState:
    improved = state.get("last_round_improved") is True
    round_index = int(state.get("round_index", 1))
    no_improvement = 0 if improved else int(state.get("no_improvement_rounds", 0)) + 1
    event = _event(
        runtime,
        node="advance_round",
        phase="OPTIMIZE",
        candidate_id=state["best_candidate_id"],
        action="advance_optimization_round",
        why=(
            "A promoted Candidate becomes the next incumbent."
            if improved
            else "A rejected Candidate does not terminate exploration by itself."
        ),
        outcome="IMPROVED" if improved else "NO_IMPROVEMENT",
        round_index=round_index,
    )
    return {
        "phase": "OPTIMIZE",
        "round_index": round_index + 1,
        "rounds_completed": int(state.get("rounds_completed", 0)) + 1,
        "no_improvement_rounds": no_improvement,
        "node_events": [event],
    }


def _select_final_attempt(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    candidate_id = state["best_candidate_id"]
    reason = str(state.get("exploration_stop_reason", "EXPLORATION_COMPLETE"))
    decision_ref = _commit_registry_operation(
        runtime,
        operation_type="SELECT_FINAL_ATTEMPT",
        candidate_id=candidate_id,
        expected_best_id=candidate_id,
        expected_registry_revision=int(state.get("registry_revision", 0)),
        round_index=int(state.get("round_index", 1)),
        reason=reason,
        registry_updates={
            "active_candidate_id": None,
            "final_attempt_candidate_id": candidate_id,
        },
        candidate_updates={},
    )
    event = _event(
        runtime,
        node="select_final_attempt",
        phase="FINAL",
        candidate_id=candidate_id,
        action="select_current_incumbent_for_fresh_final_validation",
        why=reason,
        outcome="FINAL_ATTEMPT_SELECTED",
        result_ref=decision_ref,
    )
    return {
        "phase": "FINAL",
        "final_attempt_candidate_id": candidate_id,
        "final_candidate_id": None,
        "final_attempt_count": 1,
        "final_attempted_candidate_ids": [candidate_id],
        "final_validation": _initial_validation(),
        "final_synth_evidence_ref": "",
        "final_synth_evidence_sha256": "",
        "decision_ref": decision_ref,
        "registry_revision": int(state.get("registry_revision", 0)) + 1,
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


def _fallback_verified(candidate: Mapping[str, object]) -> bool:
    validation = candidate.get("validation")
    if not isinstance(validation, Mapping):
        return False
    for stage in ("csim", "synth", "cosim"):
        record = validation.get(stage)
        if not isinstance(record, Mapping) or not (
            record.get("status") == "PASS" or record.get("ok") is True
        ):
            return False
    return True


def _evaluate_final_fallback(
    runtime: _Runtime, state: V3PrototypeState
) -> V3PrototypeState:
    attempted = list(state.get("final_attempted_candidate_ids", []))
    registry = CandidateManager(runtime.run_root, runtime.task).load_registry()
    candidates = registry.get("candidates")
    eligible: list[str] = []
    if isinstance(candidates, Mapping):
        baseline_metrics = _completed_synth_report(
            runtime,
            state["baseline_metrics_ref"],
            candidate_id=state["baseline_candidate_id"],
            validation_scope="exploration",
        )
        ranked: list[CandidateScore] = []
        for candidate_id, candidate in candidates.items():
            identifier = str(candidate_id)
            if (
                identifier in attempted
                or not isinstance(candidate, Mapping)
                or not _fallback_verified(candidate)
            ):
                continue
            score = _recompute_exploration_score(
                runtime,
                registry,
                candidate_id=identifier,
                baseline_metrics=baseline_metrics,
                provisional_cosim=False,
                include_proposal=(identifier != state["baseline_candidate_id"]),
            )
            if score.candidate_id != identifier:
                raise RuntimeError("fallback Candidate score identity mismatch")
            if (
                not score.hard_constraints_passed
                or score.verification_tier
                < runtime.scoring.required_verification_tier
            ):
                continue
            ranked.append(score)

        def compare(left: CandidateScore, right: CandidateScore) -> int:
            decision = compare_scores(left, right)
            return -1 if decision.winner == left.candidate_id else 1

        eligible = [
            score.candidate_id
            for score in sorted(ranked, key=cmp_to_key(compare))
        ]
    budget_gate = _budget_affordability(
        runtime,
        required_calls=_FULL_CLOSURE_CALLS,
        policy="final_fallback_closure",
    )
    limit_reached = int(state.get("final_attempt_count", 1)) >= runtime.max_final_attempts
    if limit_reached or not eligible or budget_gate["allowed"] is not True:
        reason = (
            "FINAL_ATTEMPT_LIMIT_REACHED"
            if limit_reached
            else "NO_VERIFIED_FINAL_FALLBACK"
            if not eligible
            else "FINAL_FALLBACK_UNAFFORDABLE"
        )
        event = _event(
            runtime,
            node="evaluate_final_fallback",
            phase="FINAL",
            candidate_id=state.get("final_attempt_candidate_id"),
            action="find_affordable_verified_fallback",
            why=str(state.get("stop_reason", "FINAL_VALIDATION_FAILED")),
            outcome=reason,
        )
        return {
            "budget_gate": budget_gate,
            "last_tool_ok": False,
            "node_events": [event],
        }

    fallback_id = eligible[0]
    failure_reason = str(state.get("stop_reason", "FINAL_VALIDATION_FAILED"))
    decision_ref = _commit_registry_operation(
        runtime,
        operation_type="SELECT_FINAL_FALLBACK",
        candidate_id=fallback_id,
        expected_best_id=state["best_candidate_id"],
        expected_registry_revision=int(state.get("registry_revision", 0)),
        round_index=int(state.get("round_index", 1)),
        reason=failure_reason,
        registry_updates={"final_attempt_candidate_id": fallback_id},
        candidate_updates={},
    )
    event = _event(
        runtime,
        node="evaluate_final_fallback",
        phase="FINAL",
        candidate_id=fallback_id,
        action="select_verified_fallback_for_fresh_final_validation",
        why=failure_reason,
        outcome="FALLBACK_ATTEMPT_SELECTED",
        result_ref=decision_ref,
    )
    return {
        "status": "RUNNING",
        "stop_reason": "RUNNING",
        "budget_gate": budget_gate,
        "last_tool_ok": True,
        "final_attempt_candidate_id": fallback_id,
        "final_candidate_id": None,
        "final_attempt_count": int(state.get("final_attempt_count", 1)) + 1,
        "final_attempted_candidate_ids": [*attempted, fallback_id],
        "final_validation": _initial_validation(),
        "final_metrics_ref": "",
        "final_synth_evidence_ref": "",
        "final_synth_evidence_sha256": "",
        "final_score_ref": "",
        "final_clock": {},
        "decision_ref": decision_ref,
        "registry_revision": int(state.get("registry_revision", 0)) + 1,
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
        synth_evidence_ref, synth_evidence_sha256 = _write_synth_evidence(
            runtime, result, candidate_id=candidate_id
        )
        clock = _clock_constraint(
            result.report if result is not None else None,
            runtime.config.minimum_frequency_mhz,
        )
        update["final_metrics_ref"] = metrics_ref
        update["final_synth_evidence_ref"] = synth_evidence_ref
        update["final_synth_evidence_sha256"] = synth_evidence_sha256
        if synth_evidence_ref:
            event["details"] = {
                "synth_evidence_ref": synth_evidence_ref,
                "synth_evidence_sha256": synth_evidence_sha256,
            }
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
    baseline_metrics = _completed_synth_report(
        runtime,
        state["baseline_metrics_ref"],
        candidate_id=state["baseline_candidate_id"],
        validation_scope="exploration",
    )
    final_metrics = _completed_synth_report(
        runtime,
        state["final_metrics_ref"],
        candidate_id=candidate_id,
        validation_scope="final",
    )
    score = _score(
        runtime,
        candidate_id=candidate_id,
        baseline_metrics=baseline_metrics,
        candidate_metrics=final_metrics,
        validation=validation,
        clock=state["final_clock"],
        proposal=_proposal_for_candidate(runtime, candidate_id),
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
    decision_ref = _commit_registry_operation(
        runtime,
        operation_type="COMMIT_FINAL",
        candidate_id=candidate_id,
        expected_best_id=state["best_candidate_id"],
        expected_registry_revision=int(state.get("registry_revision", 0)),
        round_index=int(state.get("round_index", 1)),
        reason="FINAL_FULL_CLOSURE_PASS",
        registry_updates={
            "final_candidate_id": candidate_id,
            "final_attempt_candidate_id": candidate_id,
        },
        candidate_updates={
            "status": "FINAL_VERIFIED",
            "final_validation": validation,
            "final_metrics_ref": state["final_metrics_ref"],
            "final_synth_evidence_ref": state.get("final_synth_evidence_ref"),
            "final_synth_evidence_sha256": state.get(
                "final_synth_evidence_sha256"
            ),
            "final_score_ref": score_ref,
        },
    )
    promoted = candidate_id != state["baseline_candidate_id"]
    used_fallback = int(state.get("final_attempt_count", 1)) > 1
    return update | {
        "final_score_ref": score_ref,
        "decision_ref": decision_ref,
        "registry_revision": int(state.get("registry_revision", 0)) + 1,
        "final_candidate_id": candidate_id,
        "status": "DONE",
        "stop_reason": (
            "FALLBACK_VERIFIED"
            if used_fallback
            else "CANDIDATE_PROMOTED_AND_FINALIZED"
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

    def report_cell(value: object) -> str:
        if value is None or value == "":
            return "-"
        if isinstance(value, (Mapping, list)):
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            rendered = str(value)
        return rendered.replace("\n", "<br>").replace("|", "\\|")

    rows = [
        "| # | Round | Node | Phase | Candidate | Action | Why | Outcome | Result ref | Cached | Credits | Tokens | Decision data |",
        "|---:|---:|---|---|---|---|---|---|---|---|---:|---:|---|",
    ]
    for index, raw_event in enumerate(node_events, 1):
        if not isinstance(raw_event, Mapping):
            raise RuntimeError("terminal result contains an invalid node event")
        cells = [
            str(index),
            str(raw_event.get("round_index", "-")),
            str(raw_event.get("node", "")),
            str(raw_event.get("phase", "")),
            str(raw_event.get("candidate_id") or "-"),
            report_cell(raw_event.get("action", "")),
            report_cell(raw_event.get("why", "")),
            report_cell(raw_event.get("outcome", "")),
            report_cell(raw_event.get("result_ref", "-")),
            str(raw_event.get("cached", "-")),
            str(raw_event.get("credits_used", "")),
            str(raw_event.get("tokens_used", "")),
            report_cell(raw_event.get("details", {})),
        ]
        rows.append("| " + " | ".join(cells) + " |")

    gate = result.get("cosim_gate")
    gate = gate if isinstance(gate, Mapping) else {}
    gate_rows = [
        "| 项目 | 当前值 |",
        "|---|---|",
        f"| 决策 | {report_cell(gate.get('reason'))} |",
        f"| 是否进入 CoSim | {report_cell(gate.get('eligible'))} |",
        f"| 策略 | {report_cell(gate.get('policy'))} |",
        f"| Incumbent | {report_cell(gate.get('incumbent_id'))} |",
        f"| Candidate | {report_cell(gate.get('candidate_id'))} |",
        "| Official score（incumbent → candidate） | "
        + report_cell(gate.get("incumbent_official_score"))
        + " → "
        + report_cell(gate.get("candidate_official_score"))
        + " |",
        "| PPA cost（incumbent → candidate） | "
        + report_cell(gate.get("incumbent_ppa_cost"))
        + " → "
        + report_cell(gate.get("candidate_ppa_cost"))
        + " |",
    ]

    final_validation = result.get("final_validation")
    final_validation = (
        final_validation if isinstance(final_validation, Mapping) else {}
    )
    final_rows = [
        "| Stage | Status | Phase | Scope | Cached | Failure reason | Result ref |",
        "|---|---|---|---|---|---|---|",
    ]
    for stage in ("csim", "synth", "cosim"):
        record = final_validation.get(stage)
        record = record if isinstance(record, Mapping) else {}
        final_rows.append(
            "| "
            + " | ".join(
                [
                    stage.upper(),
                    report_cell(record.get("status", "NOT_RUN")),
                    report_cell(record.get("phase")),
                    report_cell(record.get("validation_scope")),
                    report_cell(record.get("cached")),
                    report_cell(record.get("failure_reason")),
                    report_cell(record.get("result_ref")),
                ]
            )
            + " |"
        )
    planner_contract = result.get("planner_contract")
    a1_lines: list[str] = []
    if isinstance(planner_contract, Mapping):
        a1_lines = [
            "## Planner 与综合证据链",
            "",
            "- Planner mode: `"
            + str(planner_contract.get("mode", "-"))
            + "`",
            "- Planner contract: `"
            + str(planner_contract.get("action_schema", "-"))
            + "`",
            f"- Last Planner action: `{result.get('planner_action_id') or '-'}`",
            f"- Planner input: `{result.get('planner_input_ref') or '-'}`",
            f"- Planner output: `{result.get('planner_output_ref') or '-'}`",
            *(
                [
                    "- Live Planner action: `"
                    + str(result.get("live_planner_action_id") or "-")
                    + "`",
                    "- Live Planner fingerprint: `"
                    + str(planner_contract.get("live_planner_fingerprint") or "-")
                    + "`",
                    "- Live replay policy: `"
                    + str(planner_contract.get("live_replay_policy") or "-")
                    + "`",
                    "- Live provider request audit: `"
                    + str(result.get("live_planner_request_ref") or "-")
                    + "`",
                    "- Live provider outcome: `"
                    + str(result.get("live_planner_output_ref") or "-")
                    + "`",
                ]
                if planner_contract.get("mode")
                == "live_non_replayable_adapter"
                else []
            ),
            "- Baseline synth evidence: `"
            + str(result.get("baseline_synth_evidence_ref") or "-")
            + "`",
            "- Best synth evidence: `"
            + str(result.get("best_synth_evidence_ref") or "-")
            + "`",
            "- Final synth evidence: `"
            + str(result.get("final_synth_evidence_ref") or "-")
            + "`",
            "- 解释口径：top-level transaction interval 是整次调用间隔，"
            "不能当作 loop PipelineII。",
            "",
        ]
    return "\n".join(
        [
            (
                "# V3-B0 Live Planner 原型团队复盘报告"
                if isinstance(planner_contract, Mapping)
                and planner_contract.get("mode")
                == "live_non_replayable_adapter"
                else (
                    "# V3-A1 原型团队复盘报告"
                    if isinstance(planner_contract, Mapping)
                    else "# V3-A0 原型团队复盘报告"
                )
            ),
            "",
            f"- Task: `{runtime.task.id}`",
            f"- Status: `{result.get('status', 'FAILED')}`",
            f"- Stop reason: `{result.get('stop_reason', 'UNKNOWN')}`",
            f"- Exploration stop: `{result.get('exploration_stop_reason', 'UNKNOWN')}`",
            "- Rounds / consecutive no-improvement: `"
            + str(result.get("rounds_completed", 0))
            + " / "
            + str(result.get("no_improvement_rounds", 0))
            + "`",
            "- Best / Final: `"
            + str(result.get("best_candidate_id"))
            + " / "
            + str(result.get("final_candidate_id"))
            + "`",
            f"- Final attempt: `{result.get('final_attempt_candidate_id')}`",
            f"- Final attempt count: `{result.get('final_attempt_count', 0)}`",
            f"- Final attempt limit: `{result.get('max_final_attempts', 1)}`",
            f"- Credits / Tokens: `{budget.get('credits_used')} / {budget.get('tokens_used')}`",
            "",
            "## CoSim 晋升门控",
            "",
            *gate_rows,
            "",
            "## 最终三阶段验证",
            "",
            *final_rows,
            "",
            f"- Final metrics: `{result.get('final_metrics_ref') or '-'}`",
            f"- Final score: `{result.get('final_score_ref') or '-'}`",
            "",
            *a1_lines,
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
        "result_schema": TERMINAL_RESULT_SCHEMA,
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
        "exploration_stop_reason": state.get(
            "exploration_stop_reason", "UNKNOWN"
        ),
        "rounds_completed": int(state.get("rounds_completed", 0)),
        "no_improvement_rounds": int(state.get("no_improvement_rounds", 0)),
        "baseline_candidate_id": state.get("baseline_candidate_id"),
        "best_candidate_id": state.get("best_candidate_id"),
        "final_attempt_candidate_id": state.get("final_attempt_candidate_id"),
        "final_candidate_id": state.get("final_candidate_id"),
        "final_attempt_count": int(state.get("final_attempt_count", 0)),
        "max_final_attempts": runtime.max_final_attempts,
        "final_attempted_candidate_ids": list(
            state.get("final_attempted_candidate_ids", [])
        ),
        "registry_revision": int(state.get("registry_revision", 0)),
        "decision_ref": state.get("decision_ref"),
        "planner_contract": {
            "input_schema": PLANNER_INPUT_SCHEMA,
            "output_schema": PLANNER_OUTPUT_SCHEMA,
            "action_schema": PLANNER_ACTION_SCHEMA,
            "mode": runtime.planner_mode,
            **(
                {
                    "live_action_schema": LIVE_PLANNER_ACTION_SCHEMA,
                    "live_outcome_schema": LIVE_PLANNER_OUTCOME_SCHEMA,
                    "live_planner_fingerprint": (
                        runtime.live_planner.fingerprint()
                    ),
                    "live_replay_policy": runtime.live_planner.replay_policy,
                }
                if runtime.live_planner is not None
                else {}
            ),
        },
        "planner_action_id": state.get("planner_action_id"),
        "planner_input_ref": state.get("planner_input_ref"),
        "planner_input_sha256": state.get("planner_input_sha256"),
        "planner_output_ref": state.get("planner_output_ref"),
        "planner_output_sha256": state.get("planner_output_sha256"),
        "baseline_metrics_ref": state.get("baseline_metrics_ref"),
        "candidate_metrics_ref": state.get("candidate_metrics_ref"),
        "final_metrics_ref": state.get("final_metrics_ref"),
        "baseline_synth_evidence_ref": state.get(
            "baseline_synth_evidence_ref"
        ),
        "baseline_synth_evidence_sha256": state.get(
            "baseline_synth_evidence_sha256"
        ),
        "best_synth_evidence_ref": state.get("best_synth_evidence_ref"),
        "best_synth_evidence_sha256": state.get(
            "best_synth_evidence_sha256"
        ),
        "candidate_synth_evidence_ref": state.get(
            "candidate_synth_evidence_ref"
        ),
        "candidate_synth_evidence_sha256": state.get(
            "candidate_synth_evidence_sha256"
        ),
        "final_synth_evidence_ref": state.get("final_synth_evidence_ref"),
        "final_synth_evidence_sha256": state.get(
            "final_synth_evidence_sha256"
        ),
        "baseline_score_ref": state.get("baseline_score_ref"),
        "candidate_score_ref": state.get("candidate_score_ref"),
        "final_score_ref": state.get("final_score_ref"),
        "final_validation": state.get("final_validation", _initial_validation()),
        "final_clock": state.get("final_clock", {}),
        "last_tool_phase": state.get("last_tool_phase"),
        "last_tool_reason": state.get("last_tool_reason"),
        "cosim_gate": state.get("cosim_gate", {}),
        "budget": budget,
        "node_events": node_events,
        "prototype_limits": [
            (
                "live Planner is transactionally bounded; native multi-strategy planning is not enabled"
                if runtime.live_planner is not None
                else "versioned deterministic Planner boundary; no autonomous LLM planner yet"
            ),
            "loop-level synth evidence is explicit; unavailable evidence is never fabricated",
            "Candidate decisions use recoverable compare-and-set operation journals",
            "final fallback is best-effort and runs only when one fresh closure remains affordable",
            "A1 risk-gated provisional Candidates are not enabled",
        ],
        "artifacts": {
            "checkpoints": "graph_checkpoints.sqlite",
            "candidate_registry": "candidate_registry.json",
            "budget_ledger": "budget_ledger.jsonl",
            "trace": "trace.jsonl",
            "candidate_operations": "control/candidate_operations",
            "planner_inputs": "planner/inputs",
            "planner_outputs": "planner/outputs",
            "synth_evidence": "evidence/synth",
            "package_manifest": "control/package_manifest.json",
            "team_report": "v3_team_report.md",
            "result": "v3_prototype_result.json",
        },
    }
    if runtime.live_planner is not None:
        result.update(
            {
                "live_planner_action_id": state.get(
                    "live_planner_action_id"
                ),
                "live_planner_request_ref": state.get(
                    "live_planner_request_ref"
                ),
                "live_planner_request_sha256": state.get(
                    "live_planner_request_sha256"
                ),
                "live_planner_output_ref": state.get(
                    "live_planner_output_ref"
                ),
                "live_planner_output_sha256": state.get(
                    "live_planner_output_sha256"
                ),
                "live_planner_started_ref": state.get(
                    "live_planner_started_ref"
                ),
                "live_planner_completed_ref": state.get(
                    "live_planner_completed_ref"
                ),
            }
        )
    _atomic_text(
        runtime.run_root / "v3_team_report.md",
        _render_team_report(runtime, result),
    )
    manifest_ref = "control/package_manifest.json"
    manifest = _build_package_manifest(runtime, result)
    _atomic_json(runtime.run_root / manifest_ref, manifest)
    result["package"] = {
        "schema_version": "v3a.package-commit.v1",
        "manifest_ref": manifest_ref,
        "manifest_sha256": _sha256_json(manifest),
    }
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


def _pass_or_finalize(state: V3PrototypeState) -> str:
    return "pass" if state.get("last_tool_ok") is True else "finalize"


def _gate_route(state: V3PrototypeState) -> str:
    return "cosim" if state.get("cosim_gate", {}).get("eligible") is True else "select"


def _final_done_or_fallback(state: V3PrototypeState) -> str:
    return "done" if state.get("status") == "DONE" else "fallback"


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
    graph.add_node(
        "record_rejected_proposal",
        lambda state: _record_rejected_proposal(runtime, state),
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
    graph.add_node("reject_candidate", lambda state: _reject_candidate(runtime, state))
    graph.add_node("advance_round", lambda state: _advance_round(runtime, state))
    graph.add_node(
        "select_final_attempt", lambda state: _select_final_attempt(runtime, state)
    )
    graph.add_node(
        "evaluate_final_budget", lambda state: _evaluate_final_budget(runtime, state)
    )
    graph.add_node("final_csim", lambda state: _final_csim(runtime, state))
    graph.add_node("final_synth", lambda state: _final_synth(runtime, state))
    graph.add_node("final_cosim", lambda state: _final_cosim(runtime, state))
    graph.add_node(
        "evaluate_final_fallback",
        lambda state: _evaluate_final_fallback(runtime, state),
    )
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
        _pass_or_finalize,
        {"pass": "plan_candidate", "finalize": "select_final_attempt"},
    )
    graph.add_edge("plan_candidate", "materialize_candidate")
    graph.add_conditional_edges(
        "materialize_candidate",
        _pass_or_select,
        {"pass": "candidate_csim", "select": "record_rejected_proposal"},
    )
    graph.add_edge("record_rejected_proposal", "advance_round")
    graph.add_conditional_edges(
        "candidate_csim",
        _pass_or_select,
        {"pass": "candidate_synth", "select": "reject_candidate"},
    )
    graph.add_conditional_edges(
        "candidate_synth",
        _pass_or_select,
        {"pass": "candidate_score_gate", "select": "reject_candidate"},
    )
    graph.add_conditional_edges(
        "candidate_score_gate",
        _gate_route,
        {"cosim": "candidate_cosim_budget_gate", "select": "reject_candidate"},
    )
    graph.add_conditional_edges(
        "candidate_cosim_budget_gate",
        _gate_route,
        {"cosim": "candidate_cosim", "select": "reject_candidate"},
    )
    graph.add_conditional_edges(
        "candidate_cosim",
        _pass_or_select,
        {"pass": "promote_candidate", "select": "reject_candidate"},
    )
    graph.add_edge("promote_candidate", "advance_round")
    graph.add_edge("reject_candidate", "advance_round")
    graph.add_edge("advance_round", "evaluate_round_budget")
    graph.add_edge("select_final_attempt", "evaluate_final_budget")
    graph.add_conditional_edges(
        "evaluate_final_budget",
        _pass_or_report,
        {"pass": "final_csim", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "final_csim",
        _pass_or_report,
        {"pass": "final_synth", "report": "evaluate_final_fallback"},
    )
    graph.add_conditional_edges(
        "final_synth",
        _pass_or_report,
        {"pass": "final_cosim", "report": "evaluate_final_fallback"},
    )
    graph.add_conditional_edges(
        "final_cosim",
        _final_done_or_fallback,
        {"done": "write_report", "fallback": "evaluate_final_fallback"},
    )
    graph.add_conditional_edges(
        "evaluate_final_fallback",
        _pass_or_report,
        {"pass": "final_csim", "report": "write_report"},
    )
    graph.add_edge("write_report", END)
    return graph.compile(checkpointer=checkpointer)


def run_v3_prototype(
    task: PublicTask,
    run_dir: str | Path,
    config: RunConfig,
    proposal: PatchProposal | Sequence[PatchProposal] | None = None,
    *,
    backend: ToolBackend | None = None,
    planner: LivePlanner | None = None,
    max_planner_rounds: int = 1,
    scoring_config: ScoringConfig | None = None,
    patch_limits: PatchLimits | None = None,
    thread_id: str = "v3a0-prototype",
    max_no_improvement_rounds: int = 2,
    max_final_attempts: int = 1,
) -> dict[str, object]:
    """Run the checkpointed V3-A1 graph and return its durable result.

    A single ``PatchProposal`` preserves the published prototype behavior.
    Passing an ordered sequence enables deterministic multi-round hardening.
    The optional live ``planner`` uses a separately charged, non-replayable
    transaction boundary and is mutually exclusive with scripted proposals.
    """

    if not thread_id.strip():
        raise ValueError("thread_id must not be empty")
    if max_no_improvement_rounds <= 0:
        raise ValueError("max_no_improvement_rounds must be positive")
    if max_final_attempts <= 0:
        raise ValueError("max_final_attempts must be positive")
    if max_planner_rounds <= 0:
        raise ValueError("max_planner_rounds must be positive")
    if planner is not None and proposal is not None:
        raise ValueError("live planner and scripted proposals are mutually exclusive")
    if planner is None:
        proposals = (
            (proposal,)
            if isinstance(proposal, PatchProposal)
            else tuple(proposal or ())
        )
        if not proposals or not all(
            isinstance(item, PatchProposal) for item in proposals
        ):
            raise ValueError("at least one valid PatchProposal is required")
    else:
        proposals = ()
        if planner.replay_policy != "NON_REPLAYABLE":
            raise ValueError("live planner must declare NON_REPLAYABLE")
        if "llm" not in config.budget.costs:
            raise ValueError("live planner requires an llm budget entry")
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    scoring = scoring_config or load_scoring_config(
        Path(__file__).with_name("config") / "v2_scoring.yaml"
    )
    runtime = _Runtime(
        task=task,
        run_root=root,
        config=config,
        proposals=proposals,
        backend=backend or VitisBackend(),
        scoring=scoring,
        patch_limits=patch_limits or PatchLimits(max_changed_lines=30, max_hunks=4),
        thread_id=thread_id,
        max_no_improvement_rounds=max_no_improvement_rounds,
        max_final_attempts=max_final_attempts,
        live_planner=planner,
        max_planner_rounds=max_planner_rounds,
    )
    checkpoint_path = root / "graph_checkpoints.sqlite"
    graph_schema_path = root / "v3_graph_schema.json"
    with _RunLock(root):
        _verify_run_identity(runtime)
        terminal = _load_terminal_result(runtime)
        if terminal is not None:
            return terminal
        existing_initialized_run = (
            (root / "v3_task_spec.json").exists()
            or (root / "v3_run_config.json").exists()
        )
        if (
            checkpoint_path.exists()
            and existing_initialized_run
            and not graph_schema_path.exists()
        ):
            raise RuntimeError(
                "V3_CHECKPOINT_MIGRATION_REQUIRED: an unfinished pre-hardening "
                "checkpoint cannot be resumed by checkpoint schema v3"
            )
        if graph_schema_path.exists():
            existing_graph_schema = _read_json_object(graph_schema_path)
            if existing_graph_schema != _checkpoint_schema_snapshot():
                raise RuntimeError(
                    "V3_CHECKPOINT_MIGRATION_REQUIRED: an unfinished checkpoint "
                    "does not use checkpoint schema v3"
                )
            registry_path = root / "candidate_registry.json"
            if registry_path.is_file():
                registry = CandidateManager(root, task).load_registry()
                _validate_candidate_registry_sources(runtime, registry)
                _verify_synth_evidence_bindings(runtime, registry)
            if (root / "budget_ledger.jsonl").is_file():
                _validate_completed_tool_actions(runtime)
        _write_once_or_verify(graph_schema_path, _checkpoint_schema_snapshot())
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            graph = build_v3_prototype_graph(runtime, checkpointer)
            graph_config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": max(
                    64,
                    16
                    * (
                        max_planner_rounds
                        if planner is not None
                        else len(proposals)
                    )
                    + 32,
                ),
            }
            snapshot = graph.get_state(graph_config)
            missing_identity = [
                path.name
                for path in (
                    root / "v3_task_spec.json",
                    root / "v3_run_config.json",
                )
                if not path.is_file()
            ]
            if (
                snapshot.values
                and missing_identity
                and "initialize" not in snapshot.next
            ):
                raise RuntimeError(
                    "existing run identity is incomplete after initialization: "
                    + ", ".join(missing_identity)
                )
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
