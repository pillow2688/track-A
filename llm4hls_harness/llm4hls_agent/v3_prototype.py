"""Runnable V3-A0 vertical prototype built from action-level LangGraph nodes.

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
from .optimization import evaluate_exploration_cosim_gate, _read_report
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
CHECKPOINT_SCHEMA_VERSION = 2
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
    baseline_metrics_ref: str
    best_metrics_ref: str
    candidate_metrics_ref: str
    final_metrics_ref: str
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

    @property
    def proposal(self) -> PatchProposal:
        """Legacy single-proposal view used by old run identities."""

        return self.proposals[0]


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
        "schema_version": "v3a.graph-checkpoint.v2",
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "compatible_terminal_result_schema": 1,
    }


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
    # Preserve the exact legacy identity for the already-published one-patch
    # prototype runs.  Multi-round runs add an explicit planner policy and the
    # complete ordered proposal digest list.
    if len(runtime.proposals) > 1:
        value["proposal_sha256_list"] = [
            _sha256_json(proposal.to_dict()) for proposal in runtime.proposals
        ]
        value["max_no_improvement_rounds"] = runtime.max_no_improvement_rounds
    if runtime.max_final_attempts != 1:
        value["max_final_attempts"] = runtime.max_final_attempts
    return value


def _proposal_for_round(runtime: _Runtime, round_index: int) -> PatchProposal:
    if round_index <= 0 or round_index > len(runtime.proposals):
        raise ValueError(f"no scripted proposal for round {round_index}")
    return runtime.proposals[round_index - 1]


def _current_proposal(
    runtime: _Runtime, state: Mapping[str, object]
) -> PatchProposal:
    return _proposal_for_round(runtime, int(state.get("round_index", 1)))


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
    round_index = candidate.get("round_index")
    if isinstance(round_index, int):
        return _proposal_for_round(runtime, round_index)
    # Legacy one-Candidate artifacts predate explicit round metadata.
    return runtime.proposal if len(runtime.proposals) == 1 else None


def _proposal_snapshot(
    runtime: _Runtime,
    *,
    proposal: PatchProposal | None = None,
    parent_candidate_id: str = "candidate_000",
    round_index: int = 1,
) -> dict[str, object]:
    selected = proposal or runtime.proposal
    value = selected.to_dict()
    value["required_validation"] = list(selected.required_validation)
    return value | {
        "parent_candidate_id": parent_candidate_id,
        "planner_mode": "scripted_prototype",
        **({"round_index": round_index} if len(runtime.proposals) > 1 else {}),
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
    artifact_paths = [
        registry_path,
        ledger_path,
        report_path,
        graph_schema_path,
        task_spec_path,
        run_config_path,
        trace_path,
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
    for pattern in (
        "control/candidate_operations/*.committed.json",
        "control/planner_actions/*.completed.json",
    ):
        artifact_paths.extend(sorted(runtime.run_root.glob(pattern)))
    for result_path in list(artifact_paths):
        if result_path.name != "result.json" or result_path.parent.parent.name != "actions":
            continue
        tool_result = _read_json_object(result_path)
        tool_artifacts = tool_result.get("artifacts")
        artifact_hashes = tool_result.get("artifact_hashes")
        if not isinstance(tool_artifacts, Mapping) or not isinstance(
            artifact_hashes, Mapping
        ):
            continue
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
        relative = str(path.relative_to(runtime.run_root)).replace("\\", "/")
        artifacts.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
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
        # Backward compatibility for already-completed A0 prototype runs.
        return
    if not isinstance(package, Mapping):
        raise RuntimeError("terminal V3 package metadata is invalid")
    manifest_ref = package.get("manifest_ref")
    manifest_hash = package.get("manifest_sha256")
    if not isinstance(manifest_ref, str) or not isinstance(manifest_hash, str):
        raise RuntimeError("terminal V3 package metadata is incomplete")
    manifest_path = (runtime.run_root / manifest_ref).resolve()
    try:
        manifest_path.relative_to(runtime.run_root)
    except ValueError as exc:
        raise RuntimeError("terminal V3 package manifest escapes the run") from exc
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
        path = (runtime.run_root / reference).resolve()
        try:
            path.relative_to(runtime.run_root)
        except ValueError as exc:
            raise RuntimeError("terminal V3 package artifact escapes the run") from exc
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
        if not path.is_file() or _read_json_object(path) != wanted:
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
    if (
        result.get("workflow") != WORKFLOW_NAME
        or result.get("task_id") != runtime.task.id
        or result.get("status") not in {"DONE", "FAILED"}
        or not isinstance(backend, Mapping)
        or backend.get("fingerprint") != _backend_fingerprint(runtime.backend)
    ):
        raise RuntimeError("terminal V3 prototype result has an invalid identity")
    _verify_package_manifest(runtime, result, verify_report=False)
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
    _save_validation(runtime, candidate_id, "synth", record, metrics_ref=metrics_ref)
    clock = _clock_constraint(
        result.report if result is not None else None,
        runtime.config.minimum_frequency_mhz,
    )
    ok = bool(result is not None and result.ok and metrics_ref and clock["passed"])
    return {
        "baseline_metrics_ref": metrics_ref or "",
        "best_metrics_ref": metrics_ref or "",
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
    if round_index > len(runtime.proposals):
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
        gate = _budget_affordability(
            runtime,
            required_calls=required,
            policy="candidate_exploration_plus_final_closure",
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
    proposal = _proposal_for_round(runtime, round_index)
    proposal_ref = f"planner/proposal_{round_index:03d}.json"
    value = _proposal_snapshot(
        runtime,
        proposal=proposal,
        parent_candidate_id=state["best_candidate_id"],
        round_index=round_index,
    )
    request = {
        "schema_version": "v3a.planner-action.v1",
        "planner_mode": "scripted_prototype",
        "round_index": round_index,
        "parent_candidate_id": state["best_candidate_id"],
        "proposal_sha256": _sha256_json(proposal.to_dict()),
    }
    action_id = _sha256_json(request)
    action_root = runtime.run_root / "control" / "planner_actions"
    _write_once_or_verify(
        action_root / f"{action_id}.started.json",
        request | {"action_id": action_id, "status": "STARTED"},
    )
    _write_once_or_verify(runtime.run_root / proposal_ref, value)
    _write_once_or_verify(
        action_root / f"{action_id}.completed.json",
        request
        | {
            "action_id": action_id,
            "status": "COMPLETED",
            "outcome": "PROPOSAL",
            "result_ref": proposal_ref,
            "result_sha256": _sha256_json(value),
        },
    )
    event = _event(
        runtime,
        node="plan_candidate",
        phase="OPTIMIZE",
        candidate_id=state["best_candidate_id"],
        action="scripted_planner_proposal",
        why=proposal.hypothesis or "Apply the configured prototype Patch.",
        outcome="PROPOSAL_READY",
        result_ref=proposal_ref,
        round_index=round_index,
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
    round_index = int(state.get("round_index", 1))
    proposal = _proposal_for_round(runtime, round_index)
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
            "round_index": round_index,
            "provider": proposal.provider,
            "model": proposal.model,
            "hypothesis": proposal.hypothesis,
            "expected_effect": proposal.expected_effect,
            "risk": proposal.risk,
            "required_validation": list(proposal.required_validation),
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
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
    incumbent_id = state["best_candidate_id"]
    candidate_id = state["active_candidate_id"]
    proposal = _current_proposal(runtime, state)
    baseline_metrics = _read_report(runtime.run_root, state["baseline_metrics_ref"])
    incumbent_metrics = _read_report(runtime.run_root, state["best_metrics_ref"])
    candidate_metrics = _read_report(runtime.run_root, state["candidate_metrics_ref"])
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
    baseline_metrics = _read_report(runtime.run_root, state["baseline_metrics_ref"])
    candidate_metrics = _read_report(runtime.run_root, state["candidate_metrics_ref"])
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
        baseline_metrics = _read_report(
            runtime.run_root, state["baseline_metrics_ref"]
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
            score_ref = candidate.get("score_ref")
            if isinstance(score_ref, str) and score_ref:
                score = CandidateScore.from_dict(
                    _read_json_object(runtime.run_root / score_ref)
                )
            elif identifier == state["baseline_candidate_id"]:
                score = _score(
                    runtime,
                    candidate_id=identifier,
                    baseline_metrics=baseline_metrics,
                    candidate_metrics=baseline_metrics,
                    validation=_registry_validation(runtime, identifier),
                    clock=state["baseline_clock"],
                    proposal=None,
                    provisional_cosim=False,
                )
            else:
                continue
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
    return "\n".join(
        [
            "# V3-A0 原型团队复盘报告",
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
        "baseline_metrics_ref": state.get("baseline_metrics_ref"),
        "candidate_metrics_ref": state.get("candidate_metrics_ref"),
        "final_metrics_ref": state.get("final_metrics_ref"),
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
            "deterministic scripted proposal sequence; no autonomous LLM planner yet",
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
            "package_manifest": "control/package_manifest.json",
            "team_report": "v3_team_report.md",
            "result": "v3_prototype_result.json",
        },
    }
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
    proposal: PatchProposal | Sequence[PatchProposal],
    *,
    backend: ToolBackend | None = None,
    scoring_config: ScoringConfig | None = None,
    patch_limits: PatchLimits | None = None,
    thread_id: str = "v3a0-prototype",
    max_no_improvement_rounds: int = 2,
    max_final_attempts: int = 1,
) -> dict[str, object]:
    """Run the checkpointed V3-A0 graph and return its durable result.

    A single ``PatchProposal`` preserves the published prototype behavior.
    Passing an ordered sequence enables deterministic multi-round hardening.
    """

    if not thread_id.strip():
        raise ValueError("thread_id must not be empty")
    if max_no_improvement_rounds <= 0:
        raise ValueError("max_no_improvement_rounds must be positive")
    if max_final_attempts <= 0:
        raise ValueError("max_final_attempts must be positive")
    proposals = (
        (proposal,)
        if isinstance(proposal, PatchProposal)
        else tuple(proposal)
    )
    if not proposals or not all(isinstance(item, PatchProposal) for item in proposals):
        raise ValueError("at least one valid PatchProposal is required")
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
                "checkpoint cannot be resumed by checkpoint schema v2"
            )
        _write_once_or_verify(graph_schema_path, _checkpoint_schema_snapshot())
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            graph = build_v3_prototype_graph(runtime, checkpointer)
            graph_config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": max(64, 16 * len(proposals) + 32),
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
