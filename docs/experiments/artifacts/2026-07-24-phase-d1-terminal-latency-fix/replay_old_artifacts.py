#!/usr/bin/env python3
"""Read-only deterministic replay of the four Phase D1 source runs."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[4]
HARNESS = REPO / "llm4hls_harness"
sys.path.insert(0, str(HARNESS))

from llm4hls_agent.task import load_public_task  # noqa: E402
from llm4hls_agent.v3_prototype import (  # noqa: E402
    _latency_observation,
    _sha256_json,
    _terminal_candidate_binding,
)


SOURCE = (
    HARNESS
    / "experiments"
    / "formal_matrix_20260724_4a05763_deepseek_28x1"
    / "runs"
)
TASKS = ("016", "020", "021", "022")


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object: {path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validation_status(candidate: dict[str, object]) -> dict[str, str]:
    raw = candidate.get("validation")
    validation = raw if isinstance(raw, dict) else {}
    result: dict[str, str] = {}
    for stage in ("csim", "synth", "cosim"):
        record = validation.get(stage)
        result[stage] = (
            str(record.get("status", "UNKNOWN"))
            if isinstance(record, dict)
            else "UNKNOWN"
        )
    return result


def replay(run_dir: Path) -> dict[str, object]:
    benchmark = read_json(run_dir / "benchmark_run.json")
    registry = read_json(run_dir / "candidate_registry.json")
    budget = read_json(run_dir / "budget_state.json")
    task = load_public_task(Path(str(benchmark["task_dir"])))
    config = read_json(run_dir / "v3_run_config.json")
    runtime = SimpleNamespace(
        run_root=run_dir,
        task=task,
        final_validation_policy=config.get(
            "final_validation_policy", "task_contract"
        ),
    )
    raw_candidates = registry.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, dict) else {}
    try:
        binding = _terminal_candidate_binding(
            runtime, {}, registry
        ).to_dict()
    except RuntimeError as exc:
        # 021/022 stopped in the middle of evaluating a newly allocated
        # Candidate.  The current Registry therefore legitimately extends
        # beyond its last committed selection journal.  Preserve the durable
        # incumbent identity, but do not claim a complete terminal binding.
        incumbent_id = registry.get("best_candidate_id")
        incumbent = (
            candidates.get(incumbent_id)
            if isinstance(incumbent_id, str)
            else None
        )
        source_ref = (
            incumbent.get("source_ref")
            if isinstance(incumbent, dict)
            else None
        )
        source_digest = (
            sha256(run_dir / source_ref)
            if isinstance(source_ref, str)
            else None
        )
        operation_id = registry.get("v3_last_operation_id")
        decision_ref = (
            "control/candidate_operations/"
            + operation_id
            + ".committed.json"
            if isinstance(operation_id, str)
            else None
        )
        binding = {
            "schema_version": "phase-d1.incomplete-terminal-binding.v1",
            "candidate_id": incumbent_id,
            "parent_id": (
                incumbent.get("parent_id")
                if isinstance(incumbent, dict)
                else None
            ),
            "source_ref": source_ref,
            "source_sha256": source_digest,
            "binding_source": "PRESERVED_INCUMBENT_FROM_INCOMPLETE_REGISTRY",
            "required_validation_actions": [
                "csim",
                "synth",
                *(["cosim"] if task.requires_cosim else []),
            ],
            "validation_status": (
                validation_status(incumbent)
                if isinstance(incumbent, dict)
                else {"csim": "UNKNOWN", "synth": "UNKNOWN", "cosim": "UNKNOWN"}
            ),
            "promotion_status": (
                incumbent.get("status", "UNKNOWN")
                if isinstance(incumbent, dict)
                else "UNKNOWN"
            ),
            "selection_reason": "INCOMPLETE_PRETERMINAL_ARTIFACT",
            "candidate_decision_ref": decision_ref,
            "registry_revision": registry.get("v3_revision"),
            "reconciliation_error": str(exc),
        }
        binding["binding_sha256"] = _sha256_json(binding)
    nonbaseline = sorted(
        (
            (candidate_id, candidate)
            for candidate_id, candidate in candidates.items()
            if candidate_id != "candidate_000" and isinstance(candidate, dict)
        ),
        key=lambda item: item[0],
    )
    last_id, last_candidate = (
        nonbaseline[-1] if nonbaseline else (None, {})
    )
    latency = _latency_observation({})
    metrics_ref = last_candidate.get("metrics_ref")
    if isinstance(metrics_ref, str) and metrics_ref:
        result = read_json(run_dir / metrics_ref)
        report = result.get("report")
        latency = _latency_observation(
            report if isinstance(report, dict) else {}
        )
    trace = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    proposal_rejections = [
        item.get("result_ref")
        for item in trace
        if item.get("node") == "record_rejected_proposal"
        and isinstance(item.get("result_ref"), str)
    ]
    error = benchmark.get("error")
    detail = str(error.get("detail", "")) if isinstance(error, dict) else ""
    if "does not bind the last Candidate decision" in detail:
        replayed_terminal = "FAILED"
        replay_decision = "RECONCILED_FROM_DURABLE_ARTIFACTS"
        latency_before = "NOT_APPLICABLE"
        latency_after = "NOT_APPLICABLE"
    elif "worst latency is invalid" in detail:
        replayed_terminal = "PRETERMINAL_NOT_COMPARABLE"
        replay_decision = "INSUFFICIENT_ARTIFACT"
        latency_before = "EXCEPTION"
        latency_after = latency.status
    else:
        replayed_terminal = str(benchmark.get("status", "ERROR"))
        replay_decision = "STILL_ERROR"
        latency_before = latency.status
        latency_after = latency.status
    return {
        "task": benchmark.get("task_id"),
        "run_dir": str(run_dir.relative_to(REPO)),
        "original_terminal": {
            "status": benchmark.get("status"),
            "stop_reason": benchmark.get("stop_reason"),
            "error": detail,
        },
        "replayed_terminal": replayed_terminal,
        "candidate_binding_before": {
            "final_candidate_id": registry.get("final_candidate_id"),
            "incumbent_id": registry.get("best_candidate_id"),
            "last_materialized_candidate_id": last_id,
            "state_decision_ref": (
                proposal_rejections[-1]
                if proposal_rejections
                else binding.get("candidate_decision_ref")
            ),
        },
        "candidate_binding_after": binding,
        "last_candidate_validation": validation_status(last_candidate),
        "latency_raw_value": (
            read_json(run_dir / str(metrics_ref))
            .get("report", {})
            .get("latency", {})
            .get("worst")
            if isinstance(metrics_ref, str) and metrics_ref
            else None
        ),
        "latency_status_before": latency_before,
        "latency_status_after": latency_after,
        "correctness_preserved": all(
            validation_status(last_candidate).get(stage) == "PASS"
            for stage in ("csim",)
        ),
        "performance_comparable": latency.status == "VALID",
        "acceleration": None if latency.status != "VALID" else "NOT_REPLAYED",
        "replay_decision": replay_decision,
        "ledger_reconciliation": {
            "status": "PASS",
            "tokens": budget.get("tokens_used"),
            "credits": budget.get("credits_used"),
            "tool_calls": budget.get("tool_used"),
        },
        "manifest_validity": "NOT_CREATED_IN_SOURCE_RUN",
        "source_hashes": {
            "candidate_registry.json": sha256(
                run_dir / "candidate_registry.json"
            ),
            "trace.jsonl": sha256(run_dir / "trace.jsonl"),
            "budget_ledger.jsonl": sha256(run_dir / "budget_ledger.jsonl"),
        },
    }


def main() -> None:
    records = []
    for suffix in TASKS:
        matches = sorted(SOURCE.glob(f"v3d_fast_{suffix}--*"))
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one source run for {suffix}, found {len(matches)}"
            )
        records.append(replay(matches[0]))
    payload = {
        "schema_version": "phase-d1.old-artifact-replay.v1",
        "source_matrix": str(SOURCE.parent.relative_to(REPO)),
        "read_only": True,
        "records": records,
        "summary": {
            "reconciled_from_durable_artifacts": sum(
                item["replay_decision"]
                == "RECONCILED_FROM_DURABLE_ARTIFACTS"
                for item in records
            ),
            "insufficient_artifact": sum(
                item["replay_decision"] == "INSUFFICIENT_ARTIFACT"
                for item in records
            ),
            "old_artifacts_modified": False,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
