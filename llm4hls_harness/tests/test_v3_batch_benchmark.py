from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm4hls_agent import v3_batch_benchmark as benchmark_module
from llm4hls_agent.v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_DECISION_SCHEMA,
    CONTINUATION_FIXED_THRESHOLDS,
    CONTINUATION_GATE_SCHEMA,
    CONTINUATION_POLICY_VERSION,
    CONTINUATION_PROTOCOL_VERSION,
    current_repository_commit,
)
from llm4hls_agent.v3_strategy_ranker_v3 import STRATEGY_RANKER_V3_SCHEMA
from llm4hls_agent.v3_experience_v2_runtime import (
    RANKER_ADMISSION_SCHEMA,
    RANKER_GATE_PROTOCOL,
    RANKER_GATE_SCHEMA,
    current_repository_commit as current_experience_repository_commit,
)
from llm4hls_agent.v3_batch_benchmark import (
    RUN_SCHEMA,
    SUMMARY_SCHEMA,
    BatchBenchmarkRunner,
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkExecutionError,
    BenchmarkRunSpec,
    EvidenceClass,
    SyntheticBenchmarkExecutor,
    V3PrototypeCLIExecutor,
    build_summary,
    discover_tasks,
    main,
    select_models,
    select_tasks,
)


def write_task(
    root: Path,
    task_id: str,
    *,
    task_type: str = "optimize",
    difficulty: int = 1,
    split: str | None = None,
    expected_mode: str | None = None,
    requires_cosim: bool = False,
) -> Path:
    directory = root / task_id
    directory.mkdir(parents=True)
    metadata = [
        f'task_id = "{task_id}"',
        f'task_type = "{task_type}"',
        f"difficulty = {difficulty}",
        'top = "kernel"',
        'kernel_file = "kernel.cpp"',
        'public_tb = "kernel_tb.cpp"',
        "header_files = []",
        "budget = 80",
        f"requires_cosim = {str(requires_cosim).lower()}",
        "",
    ]
    if split is not None or expected_mode is not None:
        metadata.extend(["[benchmark]"])
        if split is not None:
            metadata.append(f'split = "{split}"')
        if expected_mode is not None:
            metadata.append(f'expected_mode = "{expected_mode}"')
        metadata.append("")
    (directory / "task.toml").write_text("\n".join(metadata), encoding="utf-8")
    (directory / "kernel.cpp").write_text(
        "void kernel(int *x) { x[0] += 1; }\n", encoding="utf-8"
    )
    (directory / "kernel_tb.cpp").write_text(
        "int main() { return 0; }\n", encoding="utf-8"
    )
    return directory


def write_ranker_admission(path: Path, seed: Path) -> Path:
    thresholds = {
        "minimum_coverage": 0.40,
        "maximum_harmful_rate": 0.05,
        "minimum_records_per_mode": 10,
        "minimum_global_positive_hit_rate": 0.2727,
        "maximum_leakage_violations": 0,
    }
    verified_by_mode = {
        mode: 10
        for mode in ("OPTIMIZE", "REPAIR", "STRUCTURAL_FIX", "SYNTH_FIX")
    }
    gate_path = path.with_name(f"{path.stem}-gate.json")
    gate = {
        "schema_version": RANKER_GATE_SCHEMA,
        "decision": "PASS",
        "authority": "ELIGIBLE_FOR_ADMISSION",
        "ranker_version": STRATEGY_RANKER_V3_SCHEMA,
        "fixed_protocol": RANKER_GATE_PROTOCOL,
        "thresholds": thresholds,
        "input": {
            "store_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
            "verified_by_mode": verified_by_mode,
        },
        "checks": {
            "coverage_gate_pass": True,
            "harmful_rate_gate_pass": True,
            "mode_count_gate_pass": True,
            "positive_hit_gate_pass": True,
            "leakage_gate_pass": True,
            "unverified_labels_excluded": True,
            "family_level_sampling": True,
            "query_outcome_fields_present": False,
            "heldout_labels_read_after_decision": True,
            "train_only_support": True,
            "public_only_support": True,
        },
        "policies": {
            policy: {
                "overall": {
                    "coverage": 0.5,
                    "harmful_recommendation_rate": 0.0,
                    "leakage_violations": 0,
                    "positive_strategy_hit_rate": 0.5,
                },
                "by_mode": {
                    mode: {"harmful_recommendation_rate": 0.0}
                    for mode in verified_by_mode
                },
            }
            for policy in (
                "leave_one_task_out",
                "leave_one_task_family_out",
            )
        },
    }
    gate_path.write_text(
        json.dumps(gate, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": RANKER_ADMISSION_SCHEMA,
                "decision": "PASS",
                "ranker_schema": STRATEGY_RANKER_V3_SCHEMA,
                "store_path": seed.name,
                "store_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
                "gate_path": gate_path.name,
                "gate_sha256": hashlib.sha256(
                    gate_path.read_bytes()
                ).hexdigest(),
                "thresholds": thresholds,
                "current_commit": current_experience_repository_commit(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_continuation_admission(path: Path) -> Path:
    commit = current_repository_commit()
    mode_coverage = {
        mode: 1
        for mode in ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
    }
    gate_path = path.with_name(f"{path.stem}-gate.json")
    gate = {
        "schema_version": CONTINUATION_GATE_SCHEMA,
        "gate_status": "PASS",
        "policy_version": CONTINUATION_POLICY_VERSION,
        "decision_schema": CONTINUATION_DECISION_SCHEMA,
        "current_commit": commit,
        "protocol_version": CONTINUATION_PROTOCOL_VERSION,
        "thresholds": CONTINUATION_FIXED_THRESHOLDS,
        "mode_coverage": mode_coverage,
        "metrics": {
            "sample_count": 4,
            "false_blocks": 0,
            "leakage_violations": 0,
        },
        "source_manifest_sha256": "b" * 64,
        "audited_samples": [
            {
                "mode": mode,
                "outcome": "BENEFICIAL_PERFORMANCE",
                "current_decision": {
                    "schema_version": CONTINUATION_DECISION_SCHEMA,
                    "policy_version": CONTINUATION_POLICY_VERSION,
                    "decision": "ALLOW",
                },
                "leakage_violations": 0,
            }
            for mode in mode_coverage
        ],
    }
    gate_path.write_text(
        json.dumps(gate, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": CONTINUATION_ADMISSION_SCHEMA,
                "policy_version": CONTINUATION_POLICY_VERSION,
                "decision_schema": CONTINUATION_DECISION_SCHEMA,
                "current_commit": commit,
                "gate_json_path": gate_path.name,
                "gate_json_sha256": hashlib.sha256(
                    gate_path.read_bytes()
                ).hexdigest(),
                "gate_status": "PASS",
                "protocol_version": CONTINUATION_PROTOCOL_VERSION,
                "mode_coverage": mode_coverage,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def fake_result(
    mode: str,
    *,
    status: str = "DONE",
    cached_final: bool = False,
    evidence_level: str = "DETERMINISTIC_TEST_ONLY",
    rejection: bool = False,
) -> dict[str, object]:
    candidate_rounds: list[dict[str, object]] = [
        {
            "round": 0,
            "candidate_id": "candidate_000",
            "latency_worst": 100,
            "decision": "BASELINE",
        }
    ]
    final_id = "candidate_001"
    if rejection:
        candidate_rounds.append(
            {
                "round": 1,
                "candidate_id": "candidate_001",
                "latency_worst": 120,
                "decision": "REJECTED",
                "decision_reason": "PATCH_POLICY_REJECTED",
            }
        )
        candidate_rounds.append(
            {
                "round": 2,
                "candidate_id": "candidate_002",
                "latency_worst": 40,
                "decision": "FINAL_VERIFIED",
            }
        )
        final_id = "candidate_002"
    else:
        candidate_rounds.append(
            {
                "round": 1,
                "candidate_id": "candidate_001",
                "latency_worst": 40,
                "decision": "FINAL_VERIFIED",
            }
        )
    validation_status = "PASS" if status == "DONE" else "FAIL"
    return {
        "backend": {"evidence_level": evidence_level},
        "mode": mode,
        "status": status,
        "stop_reason": "COMPLETED" if status == "DONE" else "FINAL_SYNTH_FAILED",
        "baseline_candidate_id": "candidate_000",
        "final_candidate_id": final_id if status == "DONE" else None,
        "final_validation": {
            stage: {"status": validation_status, "cached": cached_final}
            for stage in ("csim", "synth", "cosim")
        },
        "candidate_rounds": candidate_rounds,
        "budget": {
            "credits_used": 75,
            "tokens_used": 900,
            "input_tokens_used": 700,
            "output_tokens_used": 200,
            "runtime_used_seconds": 3.5,
            "tool_used": {"csim": 3, "synth": 3, "cosim": 2, "llm": 2},
        },
    }


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def write_canonical_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (canonical_json(value) + "\n").encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_real_fixture(
    executor: V3PrototypeCLIExecutor,
    spec: BenchmarkRunSpec,
    *,
    outcome_model: str | None = None,
    ledger_tokens: int | None = None,
    final_validation_policy: str = "full_internal_audit",
    include_provider_rejection: bool = False,
    provider_rejection_package_omissions: tuple[str, ...] = (),
    tamper_provider_rejection_hash: bool = False,
    tamper_provider_rejection_identity: bool = False,
    provider_rejection_token_overrun: bool = False,
) -> dict[str, object]:
    """Write a minimal internally hash-bound REAL terminal package."""

    run_dir = spec.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    write_canonical_json(
        run_dir / "benchmark_executor_command.json", executor._command(spec)
    )
    write_canonical_json(
        run_dir / "benchmark_execution_binding.json",
        {
            "schema_version": "v3d.execution-binding.v1",
            "run_id": spec.run_id,
            "run_fingerprint": spec.run_fingerprint,
            "task_id": spec.task.id,
            "task_fingerprint": spec.descriptor.task_fingerprint,
            "model": spec.model,
        },
    )
    budget_config = {
        "credit_limit": 100,
        "costs": {"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
        "tool_limits": {
            "csim": 4,
            "synth": 4,
            "cosim": 4,
            "llm": 2 if include_provider_rejection else 1,
        },
        "token_limit": 1000,
        "runtime_limit_seconds": 100.0,
    }
    backend_fingerprint = "llm4hls_agent.vitis.VitisBackend:v0.7"
    write_canonical_json(
        run_dir / "v3_run_config.json",
        {
            "thread_id": spec.run_id,
            "backend_fingerprint": backend_fingerprint,
            "budget": budget_config,
            "final_validation_policy": final_validation_policy,
        },
    )
    write_canonical_json(
        run_dir / "v3_task_spec.json",
        {
            "task_id": spec.task.id,
            "public_file_hashes": dict(spec.task.public_file_hashes),
            "requires_cosim": spec.task.requires_cosim,
        },
    )

    source_ref = "candidates/candidate_001/kernel.cpp"
    source = b"void kernel(int *x) { x[0] += 2; }\n"
    source_path = run_dir / source_ref
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source)
    source_sha = hashlib.sha256(source).hexdigest()
    write_canonical_json(
        run_dir / "candidate_registry.json",
        {
            "candidates": {
                "candidate_001": {
                    "source_ref": source_ref,
                    "code_hash": source_sha,
                }
            }
        },
    )

    planner_input = {"schema_version": "fixture.input.v1", "round": 1}
    planner_input_ref = "planner/inputs/round_001.json"
    write_canonical_json(run_dir / planner_input_ref, planner_input)
    planner_input_sha = json_sha256(planner_input)
    planner_output = {"schema_version": "fixture.output.v1", "proposal": "p"}
    planner_output_ref = "planner/outputs/projection.json"
    write_canonical_json(run_dir / planner_output_ref, planner_output)

    model = spec.model if outcome_model is None else outcome_model
    planner_logical_id = json_sha256(
        {
            "schema_version": "v3b.planner-logical-operation.v1",
            "planner_fingerprint": "fixture-planner",
            "input_sha256": planner_input_sha,
        }
    )
    request_audit = {
        "schema_version": "v3b.planner-provider-request.v1",
        "logical_operation_id": planner_logical_id,
        "planner_fingerprint": "fixture-planner",
        "input_sha256": planner_input_sha,
        "estimated_input_tokens": 10,
        "max_output_tokens": 10,
        "request": {
            "provider_request": {
                "provider": "openai-compatible",
                "model": model,
                "http_body": {"model": model},
            }
        },
    }
    request_ref = f"planner/requests/{planner_logical_id}.json"
    write_canonical_json(run_dir / request_ref, request_audit)
    request_sha = json_sha256(request_audit)
    action_request = {
        "schema_version": "v3b.planner-action.v1",
        "logical_operation_id": planner_logical_id,
        "attempt_index": 0,
        "retry_of": None,
        "planner_fingerprint": "fixture-planner",
        "input_ref": planner_input_ref,
        "input_sha256": planner_input_sha,
        "request_ref": request_ref,
        "request_sha256": request_sha,
        "replay_policy": "NON_REPLAYABLE",
    }
    action_id = json_sha256(action_request)
    proposal = {
        "patch": "--- a/kernel.cpp\n+++ b/kernel.cpp\n@@ -1 +1 @@\n-a\n+b\n",
        "provider": "openai-compatible",
        "model": model,
        "revision": None,
        "input_tokens": 7,
        "output_tokens": 3,
        "cached_input_tokens": 1,
        "request_id": "request-1",
        "duration_seconds": 0.5,
        "hypothesis": "fixture",
        "change_class": "fixture",
        "expected_effect": "fixture",
        "risk": "fixture",
        "required_validation": ["csim", "synth", "cosim"],
    }
    usage = {
        "input_tokens": 7,
        "output_tokens": 3,
        "cached_input_tokens": 1,
        "tokens_used": 10,
        "duration_seconds": 0.5,
        "request_id": "request-1",
        "usage_complete": True,
    }
    outcome = {
        "schema_version": "v3b.planner-outcome.v1",
        "action_id": action_id,
        "input_sha256": planner_input_sha,
        "outcome": "PROPOSAL",
        "proposal": proposal,
        "semantic_proposal_sha256": "0" * 64,
        "provider_binding": {
            "planner_fingerprint": "fixture-planner",
            "provider": "openai-compatible",
            "model": model,
            "revision": None,
        },
        "usage": usage,
    }
    live_output_ref = f"planner/live_outcomes/{action_id}.json"
    live_output_sha = write_canonical_json(run_dir / live_output_ref, outcome)
    live_started_ref = f"control/live_planner_actions/{action_id}.started.json"
    write_canonical_json(
        run_dir / live_started_ref,
        {
            "schema_version": "v3b.planner-started.v1",
            "action_id": action_id,
            "status": "STARTED",
            "request": action_request,
        },
    )
    live_completed_ref = f"control/live_planner_actions/{action_id}.completed.json"
    write_canonical_json(
        run_dir / live_completed_ref,
        {
            "schema_version": "v3b.planner-completed.v1",
            "action_id": action_id,
            "status": "COMPLETED",
            "request": action_request,
            "outcome": "PROPOSAL",
            "result_ref": live_output_ref,
            "result_sha256": live_output_sha,
            "semantic_proposal_sha256": "0" * 64,
            "tokens_used": 10,
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 1,
        },
    )

    provider_rejection: dict[str, object] | None = None
    if include_provider_rejection:
        failure_planner_input = {
            "schema_version": "fixture.input.v1",
            "round": 0,
        }
        failure_planner_input_ref = "planner/inputs/round_000.json"
        write_canonical_json(
            run_dir / failure_planner_input_ref, failure_planner_input
        )
        failure_planner_input_sha = json_sha256(failure_planner_input)
        failure_logical_id = json_sha256(
            {
                "schema_version": "v3b.planner-logical-operation.v1",
                "planner_fingerprint": "fixture-planner",
                "input_sha256": failure_planner_input_sha,
            }
        )
        if tamper_provider_rejection_identity:
            failure_logical_id = "f" * 64
        failure_request_audit = {
            "schema_version": "v3b.planner-provider-request.v1",
            "logical_operation_id": failure_logical_id,
            "planner_fingerprint": "fixture-planner",
            "input_sha256": failure_planner_input_sha,
            "estimated_input_tokens": 8,
            "max_output_tokens": 10,
            "request": {
                "provider_request": {
                    "provider": "openai-compatible",
                    "model": model,
                    "http_body": {"model": model},
                }
            },
        }
        failure_request_ref = (
            f"planner/requests/{failure_logical_id}.json"
        )
        write_canonical_json(
            run_dir / failure_request_ref, failure_request_audit
        )
        failure_request_sha = json_sha256(failure_request_audit)
        failure_action_request = {
            "schema_version": "v3b.planner-action.v1",
            "logical_operation_id": failure_logical_id,
            "attempt_index": 0,
            "retry_of": None,
            "planner_fingerprint": "fixture-planner",
            "input_ref": failure_planner_input_ref,
            "input_sha256": failure_planner_input_sha,
            "request_ref": failure_request_ref,
            "request_sha256": failure_request_sha,
            "replay_policy": "NON_REPLAYABLE",
        }
        failure_action_id = json_sha256(failure_action_request)
        failure_ref = (
            f"planner/provider_failures/{failure_action_id}.json"
        )
        failure = {
            "schema_version": "v3.token-provider-failure.v1",
            "action_id": failure_action_id,
            "input_sha256": failure_planner_input_sha,
            "planner_fingerprint": "fixture-planner",
            "outcome": "PROVIDER_OUTPUT_REJECTED",
            "error_type": "RepairProviderError",
            "error_message": (
                "provider output is incomplete and cannot create a Candidate"
            ),
            "response_excerpt": '{"patch":"--- kernel.cpp\\n',
            "requested_max_output_tokens": 10,
            "effective_max_output_tokens": 10,
            "provider_parameter_name": "max_tokens",
            "finish_reason": "stop",
            "output_truncated": True,
            "truncation_reason": "PATCH_INCOMPLETE",
            "json_incomplete": False,
            "patch_incomplete": True,
            "usage": {
                "actual_input_tokens": 8,
                "actual_output_tokens": 3,
                "actual_total_tokens": 11,
                "cached_input_tokens": 0,
                "duration_seconds": 0.25,
                "request_id": "request-failure",
                "usage_complete": True,
            },
        }
        failure_sha = write_canonical_json(run_dir / failure_ref, failure)
        failure_started_ref = (
            "control/live_planner_actions/"
            f"{failure_action_id}.started.json"
        )
        write_canonical_json(
            run_dir / failure_started_ref,
            {
                "schema_version": "v3b.planner-started.v1",
                "action_id": failure_action_id,
                "status": "STARTED",
                "request": failure_action_request,
            },
        )
        failure_completed_ref = (
            "control/live_planner_actions/"
            f"{failure_action_id}.completed.json"
        )
        write_canonical_json(
            run_dir / failure_completed_ref,
            {
                "schema_version": "v3b.planner-completed.v1",
                "action_id": failure_action_id,
                "status": "COMPLETED",
                "request": failure_action_request,
                "outcome": "PROVIDER_OUTPUT_REJECTED",
                "result_ref": failure_ref,
                "result_sha256": failure_sha,
                "truncation_reason": "PATCH_INCOMPLETE",
                "finish_reason": "stop",
                "tokens_used": 11,
                "input_tokens": 8,
                "output_tokens": 3,
                "cached_input_tokens": 0,
            },
        )
        rejection_ref = "control/proposal_rejections/round_000.json"
        write_canonical_json(
            run_dir / rejection_ref,
            {
                "schema_version": "v3a.proposal-rejection.v1",
                "round_index": 0,
                "parent_candidate_id": "candidate_000",
                "planner_ref": None,
                "planner_action_id": failure_action_id,
                "planner_input_ref": failure_planner_input_ref,
                "planner_input_sha256": failure_planner_input_sha,
                "planner_output_ref": failure_ref,
                "planner_output_sha256": (
                    "f" * 64
                    if tamper_provider_rejection_hash
                    else failure_sha
                ),
                "change_class": None,
                "selection_metrics_digest": None,
                "reason": "PROVIDER_OUTPUT_REJECTED:PATCH_INCOMPLETE",
            },
        )
        provider_rejection = {
            "action_id": failure_action_id,
            "input_ref": failure_planner_input_ref,
            "request_ref": failure_request_ref,
            "request_sha256": failure_request_sha,
            "started_ref": failure_started_ref,
            "completed_ref": failure_completed_ref,
            "result_ref": failure_ref,
            "result_sha256": failure_sha,
            "rejection_ref": rejection_ref,
        }

    charged_tokens = 10 if ledger_tokens is None else ledger_tokens
    ledger_events = [
        {
            "state": "INITIALIZED",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "epoch_seconds": 0.0,
            "config_hash": "fixture",
            "config": budget_config,
        },
        *(
            [
                {
                    "state": "STARTED",
                    "timestamp": "2026-01-01T00:00:00.100000+00:00",
                    "action_id": provider_rejection["action_id"],
                    "kind": "llm",
                    "candidate_id": "candidate_000",
                    "code_hash": "0" * 64,
                    "tool_config_hash": provider_rejection[
                        "request_sha256"
                    ],
                    "estimated_cost": 0,
                    "estimated_tokens": (
                        10 if provider_rejection_token_overrun else 18
                    ),
                },
                {
                    "state": "COMPLETED",
                    "timestamp": "2026-01-01T00:00:00.200000+00:00",
                    "action_id": provider_rejection["action_id"],
                    "kind": "llm",
                    "actual_cost": 0,
                    "tokens_used": 11,
                    "input_tokens": 8,
                    "output_tokens": 3,
                    "cached_input_tokens": 0,
                    "elapsed_s": 0.25,
                    "result_ref": provider_rejection["result_ref"],
                    "result_sha256": provider_rejection[
                        "result_sha256"
                    ],
                    **(
                        {"token_reservation_overrun": True}
                        if provider_rejection_token_overrun
                        else {}
                    ),
                },
            ]
            if provider_rejection is not None
            else []
        ),
        {
            "state": "STARTED",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "action_id": action_id,
            "kind": "llm",
            "candidate_id": "candidate_000",
            "code_hash": "0" * 64,
            "tool_config_hash": request_sha,
            "estimated_cost": 0,
            "estimated_tokens": 20,
        },
        {
            "state": "COMPLETED",
            "timestamp": "2026-01-01T00:00:02+00:00",
            "action_id": action_id,
            "kind": "llm",
            "actual_cost": 0,
            "tokens_used": charged_tokens,
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 1,
            "elapsed_s": 0.5,
            "result_ref": live_output_ref,
            "result_sha256": live_output_sha,
        },
    ]
    ledger_path = run_dir / "budget_ledger.jsonl"
    ledger_path.write_text(
        "".join(canonical_json(event) + "\n" for event in ledger_events),
        encoding="utf-8",
    )

    final_validation: dict[str, object] = {}
    required_final_stages = {"csim", "synth"}
    if final_validation_policy == "full_internal_audit" or spec.task.requires_cosim:
        required_final_stages.add("cosim")
    for stage in ("csim", "synth", "cosim"):
        if stage not in required_final_stages:
            final_validation[stage] = {"status": "NOT_RUN"}
            continue
        final_ref = f"actions/final-{stage}/result.json"
        write_canonical_json(
            run_dir / final_ref,
            {
                "action_id": f"final-{stage}",
                "kind": stage,
                "ok": True,
                "candidate_id": "candidate_001",
                "code_hash": source_sha,
                "validation_scope": "search_closeout",
            },
        )
        final_validation[stage] = {
            "status": "PASS",
            "cached": False,
            "validation_scope": "search_closeout",
            "action_id": f"final-{stage}",
            "result_ref": final_ref,
        }

    result: dict[str, object] = {
        "schema_version": 1,
        "result_schema": "v3a.terminal-result.v1",
        "workflow": "V3_A1_VERTICAL_PROTOTYPE",
        "backend": {
            "class": "llm4hls_agent.vitis.VitisBackend",
            "fingerprint": backend_fingerprint,
            "evidence_level": "REAL_VITIS_VALIDATED",
        },
        "task_id": spec.task.id,
        "mode": "OPTIMIZE",
        "status": "DONE",
        "stop_reason": "COMPLETED",
        "baseline_candidate_id": "candidate_000",
        "final_candidate_id": "candidate_001",
        "terminal_candidate_binding": {
            "candidate_id": "candidate_001",
            "source_ref": source_ref,
            "source_sha256": source_sha,
        },
        "planner_input_ref": planner_input_ref,
        "planner_input_sha256": json_sha256(planner_input),
        "planner_output_ref": planner_output_ref,
        "planner_output_sha256": json_sha256(planner_output),
        "live_planner_action_id": action_id,
        "live_planner_request_ref": request_ref,
        "live_planner_request_sha256": request_sha,
        "live_planner_output_ref": live_output_ref,
        "live_planner_output_sha256": live_output_sha,
        "live_planner_started_ref": live_started_ref,
        "live_planner_completed_ref": live_completed_ref,
        "final_validation": final_validation,
        "candidate_rounds": [],
        "node_events": [],
        "budget": {
            "credits_used": 0,
            "tokens_used": charged_tokens + (
                11 if include_provider_rejection else 0
            ),
            "input_tokens_used": 7 + (
                8 if include_provider_rejection else 0
            ),
            "output_tokens_used": 3 + (
                3 if include_provider_rejection else 0
            ),
            "cached_input_tokens_used": 1,
            "token_usage_complete": True,
            "runtime_used_seconds": 1.0,
            "tool_used": {
                "llm": 2 if include_provider_rejection else 1
            },
        },
    }
    omitted_artifacts = set(provider_rejection_package_omissions)
    if provider_rejection is not None:
        omission_roles = {
            "INPUT": "input_ref",
            "REQUEST": "request_ref",
            "STARTED": "started_ref",
            "COMPLETED": "completed_ref",
            "OUTCOME": "result_ref",
            "REJECTION": "rejection_ref",
        }
        omitted_artifacts.update(
            str(provider_rejection[field])
            for role, field in omission_roles.items()
            if role in omitted_artifacts
        )
    artifact_paths = sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file()
        and path.relative_to(run_dir).as_posix()
        not in omitted_artifacts
        and path.name not in {
            "benchmark_executor_command.json",
            "benchmark_execution_binding.json",
        }
    )
    artifacts = []
    for path in artifact_paths:
        data = path.read_bytes()
        artifacts.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    manifest = {
        "schema_version": "v3a.package-manifest.v1",
        "workflow": result["workflow"],
        "task_id": spec.task.id,
        "status": "DONE",
        "final_candidate_id": "candidate_001",
        "node_event_count": 0,
        "terminal_payload_sha256": json_sha256(result),
        "artifacts": artifacts,
    }
    manifest_ref = "control/package_manifest.json"
    write_canonical_json(run_dir / manifest_ref, manifest)
    result["package"] = {
        "schema_version": "v3a.package-commit.v1",
        "manifest_ref": manifest_ref,
        "manifest_sha256": json_sha256(manifest),
    }
    search_result_sha256 = write_canonical_json(
        run_dir / "v3_prototype_result.json", result
    )
    ledger_sha256 = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    freeze = {
        "schema_version": "v3.frozen-search-candidate.v1",
        "candidate_id": "candidate_001",
        "source_ref": source_ref,
        "source_sha256": source_sha,
        "search_result_sha256": search_result_sha256,
        "agent_ledger_sha256": ledger_sha256,
    }
    freeze_ref = "control/frozen_candidate.json"
    write_canonical_json(run_dir / freeze_ref, freeze)
    certification_id = "c" * 64
    receipt_ref = f"certification/{certification_id}/receipt.json"
    receipt = {
        "schema_version": "v3.final-certification-receipt.v1",
        "certification_id": certification_id,
        "status": "PASS",
        "budget_domain": "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET",
        "agent_credits_charged": 0,
        "feedback_policy": "NO_SAME_RUN_AGENT_FEEDBACK",
        "frozen_candidate_ref": freeze_ref,
        "stages": {
            stage: {"kind": stage, "ok": True}
            for stage in ("csim", "synth", "cosim")
        },
        "clock_gate": {
            "maximum_period_ns": 10.0,
            "observed_period_ns": 5.0,
            "passed": True,
        },
        "agent_ledger": {
            "before_sha256": ledger_sha256,
            "after_sha256": ledger_sha256,
            "unchanged": True,
        },
    }
    receipt["receipt_sha256"] = json_sha256(receipt)
    write_canonical_json(run_dir / receipt_ref, receipt)
    certified = dict(result)
    certified["result_schema"] = "v3.certified-result.v1"
    certified["agent_search_status"] = result["status"]
    certified["agent_search_stop_reason"] = result["stop_reason"]
    certified["final_certification"] = {
        "status": "PASS",
        "certification_id": certification_id,
        "budget_domain": "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET",
        "agent_credits_charged": 0,
        "feedback_policy": "NO_SAME_RUN_AGENT_FEEDBACK",
        "receipt_ref": receipt_ref,
        "receipt_sha256": receipt["receipt_sha256"],
    }
    write_canonical_json(run_dir / "v3_certified_result.json", certified)
    return certified


class FakeExecutor:
    evidence_class = EvidenceClass.DETERMINISTIC
    requires_vitis_lock = False

    def __init__(
        self,
        *,
        fingerprint: str = "fake-deterministic:v1",
        fail_tasks: set[str] | None = None,
        wrong_modes: set[str] | None = None,
        cached_tasks: set[str] | None = None,
        rejected_tasks: set[str] | None = None,
    ) -> None:
        self._fingerprint = fingerprint
        self.fail_tasks = fail_tasks or set()
        self.wrong_modes = wrong_modes or set()
        self.cached_tasks = cached_tasks or set()
        self.rejected_tasks = rejected_tasks or set()
        self.calls: list[str] = []
        self.max_timeout: list[float | None] = []

    def fingerprint(self) -> str:
        return self._fingerprint

    def execute(self, spec, *, timeout_seconds=None):
        self.calls.append(spec.task.id)
        self.max_timeout.append(timeout_seconds)
        if spec.task.id in self.fail_tasks:
            raise BenchmarkExecutionError("injected executor failure")
        mode = spec.descriptor.expected_mode or "OPTIMIZE"
        if spec.task.id in self.wrong_modes:
            mode = "REPAIR" if mode != "REPAIR" else "OPTIMIZE"
        return fake_result(
            mode,
            cached_final=spec.task.id in self.cached_tasks,
            rejection=spec.task.id in self.rejected_tasks,
        )


class DemoExecutor(FakeExecutor):
    evidence_class = EvidenceClass.DEMO

    def execute(self, spec, *, timeout_seconds=None):
        result = super().execute(spec, timeout_seconds=timeout_seconds)
        result["backend"] = {"evidence_level": "ORCHESTRATION_SMOKE_ONLY"}
        return result


class RealFixtureExecutor(FakeExecutor):
    """A unit-test stub; it never invokes Vitis despite exercising policy code."""

    evidence_class = EvidenceClass.REAL
    requires_vitis_lock = True
    real_evidence_authority = "V3_PROTOTYPE_CLI_VITIS_V1"

    def execute(self, spec, *, timeout_seconds=None):
        result = super().execute(spec, timeout_seconds=timeout_seconds)
        result["backend"] = {"evidence_level": "REAL_VITIS_VALIDATED"}
        return result


class V3BatchBenchmarkTests(unittest.TestCase):
    def test_continuation_module_change_changes_implementation_fingerprint(
        self,
    ) -> None:
        benchmark_module._implementation_facts.cache_clear()
        baseline = benchmark_module._implementation_fingerprint()
        real_sha256_file = benchmark_module._sha256_file

        def altered_sha256_file(path: Path) -> str:
            observed = real_sha256_file(path)
            if path.name != "v3_continuation.py":
                return observed
            return "f" * 64 if observed != "f" * 64 else "e" * 64

        try:
            with mock.patch.object(
                benchmark_module,
                "_sha256_file",
                side_effect=altered_sha256_file,
            ):
                benchmark_module._implementation_facts.cache_clear()
                changed = benchmark_module._implementation_fingerprint()
        finally:
            benchmark_module._implementation_facts.cache_clear()

        self.assertNotEqual(baseline, changed)

    def test_discovery_and_filters_need_no_corpus_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(
                root / "train",
                "repair_easy",
                task_type="repair",
                difficulty=1,
                expected_mode="REPAIR",
            )
            write_task(
                root / "validation",
                "opt_hard",
                task_type="repair",
                difficulty=4,
                split="validation",
            )
            (root / "validation" / "opt_hard" / "acceptance.json").write_text(
                json.dumps(
                    {
                        "task_id": "opt_hard",
                        "expected_mode": "SYNTH_FIX",
                    }
                ),
                encoding="utf-8",
            )
            broken = root / "train" / "broken"
            broken.mkdir(parents=True)
            (broken / "task.toml").write_text(
                'task_id = "broken"\ntop = "kernel"\n'
                'kernel_file = "missing.cpp"\npublic_tb = "missing_tb.cpp"\n',
                encoding="utf-8",
            )

            descriptors = discover_tasks(root)
            self.assertEqual([item.task_id for item in descriptors], [
                "broken",
                "opt_hard",
                "repair_easy",
            ])
            broken_descriptor = descriptors[0]
            self.assertFalse(broken_descriptor.loadable)
            self.assertEqual(broken_descriptor.split, "train")
            repair = next(item for item in descriptors if item.task_id == "repair_easy")
            self.assertEqual(repair.expected_mode, "REPAIR")
            self.assertEqual(repair.expected_mode_source, "benchmark.expected_mode")
            synth_fix = next(item for item in descriptors if item.task_id == "opt_hard")
            self.assertEqual(synth_fix.expected_mode, "SYNTH_FIX")
            self.assertEqual(synth_fix.expected_mode_source, "acceptance.expected_mode")

            config = BenchmarkConfig(
                corpus=root,
                output_dir=Path(directory) / "out",
                models=("model-a", "model-b"),
                splits=("train",),
                mode_filters=("repair",),
                task_filters=("repair_*",),
                difficulty_filters=("1-2",),
                model_filters=("*-b",),
            )
            selected = select_tasks(descriptors, config)
            self.assertEqual([item.task_id for item in selected], ["repair_easy"])
            self.assertEqual(select_models(config), ("model-b",))

    def test_manifest_does_not_expose_family_mutation_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "pilot")
            (root / "corpus_manifest.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "path": "pilot",
                                "family": "stencil_2d",
                                "split": "dev",
                                "mode": "OPTIMIZE",
                                "operator": "answer_bearing_operator",
                                "seed": 719,
                                "mutation_summary": "private answer",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            descriptor = discover_tasks(root)[0]
            self.assertIsNone(descriptor.algorithm_family)
            self.assertIsNone(descriptor.algorithm_family_source)
            self.assertEqual(descriptor.split, "dev")
            self.assertEqual(descriptor.expected_mode, "OPTIMIZE")
            self.assertNotIn("operator", descriptor.__dict__)
            self.assertNotIn("seed", descriptor.__dict__)
            assert descriptor.task is not None
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="safe-labels",
                run_fingerprint="a" * 64,
                run_dir=Path(directory) / "run",
            )
            command = V3PrototypeCLIExecutor()._command(spec)
            self.assertEqual(
                command[command.index("--experience-task-split") + 1], "dev"
            )
            self.assertNotIn("answer_bearing_operator", command)
            self.assertNotIn("private answer", command)
            self.assertNotIn("--operator", command)
            self.assertNotIn("--seed", command)

    def test_experience_policy_is_forwarded_and_content_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "pilot")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            store = Path(directory) / "experience.jsonl"
            store.write_text('{"experience_id":"exp-1"}\n', encoding="utf-8")
            admission = write_ranker_admission(
                Path(directory) / "ranker-admission.json",
                store,
            )
            copied_bundle = Path(directory) / "copied-bundle"
            copied_bundle.mkdir()
            copied_store = copied_bundle / store.name
            copied_store.write_bytes(store.read_bytes())
            copied_admission = copied_bundle / admission.name
            copied_admission.write_bytes(admission.read_bytes())
            copied_gate = copied_bundle / f"{admission.stem}-gate.json"
            copied_gate.write_bytes(
                admission.with_name(
                    f"{admission.stem}-gate.json"
                ).read_bytes()
            )
            executor = V3PrototypeCLIExecutor(
                validation_profile="fast-experiment",
                experience_mode="guided",
                experience_store=store,
                experience_ranker_version="v3",
                experience_admission_manifest=admission,
                experience_task_split="dev",
            )
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="experience-policy",
                run_fingerprint="b" * 64,
                run_dir=Path(directory) / "run",
            )

            command = executor._command(spec)
            expected_values = {
                "--validation-profile": "fast-experiment",
                "--evidence-memory": "on",
                "--experience-mode": "guided",
                "--experience-store": str(store.resolve()),
                "--experience-ranker-version": "v3",
                "--experience-admission-manifest": str(admission.resolve()),
                "--experience-task-split": "dev",
            }
            for option, expected in expected_values.items():
                with self.subTest(option=option):
                    self.assertEqual(command[command.index(option) + 1], expected)

            self.assertEqual(
                executor.fingerprint(),
                V3PrototypeCLIExecutor(
                    validation_profile="fast-experiment",
                    experience_mode="guided",
                    experience_store=copied_store,
                    experience_ranker_version="v3",
                    experience_admission_manifest=copied_admission,
                    experience_task_split="dev",
                ).fingerprint(),
            )
            self.assertNotEqual(
                executor.fingerprint(),
                V3PrototypeCLIExecutor(
                    validation_profile="fast-experiment",
                    experience_mode="shadow",
                    experience_store=copied_store,
                    experience_ranker_version="v3",
                    experience_admission_manifest=copied_admission,
                    experience_task_split="dev",
                ).fingerprint(),
            )
            self.assertNotEqual(
                executor.fingerprint(),
                V3PrototypeCLIExecutor(
                    validation_profile="strict",
                    experience_mode="guided",
                    experience_store=copied_store,
                    experience_ranker_version="v3",
                    experience_admission_manifest=copied_admission,
                    experience_task_split="dev",
                ).fingerprint(),
            )

            store.write_text('{"experience_id":"exp-2"}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                BenchmarkExecutionError, "snapshot was frozen"
            ):
                executor._command(spec)

            config = BenchmarkConfig(
                corpus=root,
                output_dir=Path(directory) / "out",
                backend="vitis",
                experience_store=store,
            )
            store.write_text('{"experience_id":"exp-3"}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                BenchmarkError, "BenchmarkConfig snapshot was frozen"
            ):
                BatchBenchmarkRunner(config)

    def test_continuation_v2_enforce_is_gate_bound_and_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "pilot")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            admission = write_continuation_admission(
                Path(directory) / "continuation-admission.json"
            )
            executor = V3PrototypeCLIExecutor(
                final_validation_policy="full_internal_audit",
                max_planner_rounds=3,
                max_no_improvement_rounds=2,
                enable_final_fallback=True,
                continuation_policy_mode="enforce",
                continuation_policy_version="v2",
                continuation_admission_manifest=admission,
            )
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="continuation-policy",
                run_fingerprint="c" * 64,
                run_dir=Path(directory) / "run",
            )
            command = executor._command(spec)
            self.assertEqual(
                command[command.index("--continuation-policy") + 1],
                "enforce",
            )
            self.assertEqual(
                command[command.index("--continuation-policy-version") + 1],
                "v2",
            )
            self.assertEqual(
                command[
                    command.index("--continuation-admission-manifest") + 1
                ],
                str(admission.resolve()),
            )
            self.assertEqual(
                command[command.index("--final-validation-policy") + 1],
                "full_internal_audit",
            )
            self.assertEqual(
                command[command.index("--max-planner-rounds") + 1],
                "3",
            )
            self.assertEqual(
                command[command.index("--max-no-improvement-rounds") + 1],
                "2",
            )
            self.assertIn("--enable-final-fallback", command)
            with self.assertRaisesRegex(
                BenchmarkError, "requires an admission manifest"
            ):
                V3PrototypeCLIExecutor(
                    continuation_policy_mode="enforce",
                    continuation_policy_version="v2",
                )
            admission.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                BenchmarkExecutionError,
                "changed after the batch snapshot",
            ):
                executor._command(spec)

    def test_restricted_descriptor_cannot_be_relabelled_for_experience(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "pilot")
            (root / "corpus_manifest.json").write_text(
                json.dumps({"tasks": [{"path": "pilot", "split": "hidden_like"}]}),
                encoding="utf-8",
            )
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="restricted-split",
                run_fingerprint="c" * 64,
                run_dir=Path(directory) / "run",
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError, "cannot be relabelled"
            ):
                V3PrototypeCLIExecutor(
                    experience_task_split="dev"
                )._command(spec)

    def test_run_fingerprint_binds_experience_policy_and_store_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "pilot")
            descriptor = discover_tasks(root)[0]
            store_a = Path(directory) / "store-a.jsonl"
            store_b = Path(directory) / "store-b.jsonl"
            store_a.write_text('{"experience_id":"exp-a"}\n', encoding="utf-8")
            store_b.write_text('{"experience_id":"exp-b"}\n', encoding="utf-8")

            def planned_fingerprint(
                *,
                output_name: str,
                mode: str = "shadow",
                profile: str = "strict",
                store: Path | None = None,
            ) -> str:
                admission = None
                ranker_version = "v1"
                if mode == "guided":
                    assert store is not None
                    ranker_version = "v3"
                    admission = write_ranker_admission(
                        Path(directory) / f"{output_name}-admission.json",
                        store,
                    )
                config = BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / output_name,
                    backend="deterministic",
                    validation_profile=profile,
                    experience_mode=mode,
                    experience_store=store,
                    experience_ranker_version=ranker_version,
                    experience_admission_manifest=admission,
                    experience_task_split="dev",
                )
                runner = BatchBenchmarkRunner(config, FakeExecutor())
                return runner._plan([descriptor], select_models(config))[0][3]

            baseline = planned_fingerprint(output_name="baseline", store=store_a)
            self.assertNotEqual(
                baseline,
                planned_fingerprint(
                    output_name="guided", mode="guided", store=store_a
                ),
            )
            self.assertNotEqual(
                baseline,
                planned_fingerprint(
                    output_name="fast",
                    profile="fast-experiment",
                    store=store_a,
                ),
            )
            self.assertNotEqual(
                baseline,
                planned_fingerprint(output_name="other-store", store=store_b),
            )
            defaults = BenchmarkConfig(root, Path(directory) / "defaults")
            self.assertEqual(defaults.experience_mode, "shadow")
            self.assertEqual(defaults.evidence_memory_mode, "on")

            a3_off = BenchmarkConfig(
                root,
                Path(directory) / "a3-off",
                evidence_memory_mode="off",
                continuation_policy_mode="off",
                experience_mode="off",
                experience_ranker_version="v3",
            )
            self.assertIsNone(a3_off.experience_store)

            with self.assertRaisesRegex(
                ValueError, "continuation requires evidence memory on"
            ):
                BenchmarkConfig(
                    root,
                    Path(directory) / "invalid-a1-a2",
                    evidence_memory_mode="off",
                    continuation_policy_mode="shadow",
                )

    def test_batch_continues_after_failure_and_aggregates_every_required_metric(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "a_opt", task_type="optimize", difficulty=3)
            write_task(root, "b_repair", task_type="repair", difficulty=2)
            write_task(root, "c_struct", task_type="structural", difficulty=4)
            output = Path(directory) / "out"
            executor = FakeExecutor(
                fail_tasks={"b_repair"},
                wrong_modes={"c_struct"},
                cached_tasks={"c_struct"},
                rejected_tasks={"a_opt"},
            )
            outcome = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("fixture-model",),
                    backend="deterministic",
                ),
                executor,
            ).run()

            self.assertEqual(executor.calls, ["a_opt", "b_repair", "c_struct"])
            self.assertEqual(len(outcome.records), 3)
            self.assertEqual(
                [record["status"] for record in outcome.records],
                ["DONE", "ERROR", "DONE"],
            )
            self.assertEqual(len({record["run_id"] for record in outcome.records}), 3)
            for record in outcome.records:
                run_dir = Path(str(record["run_dir"]))
                self.assertTrue((run_dir / "benchmark_run.json").is_file())

            overall = outcome.summary["overall_all_evidence"]
            self.assertEqual(overall["runs"], 3)
            self.assertEqual(overall["e2e_successes"], 2)
            self.assertEqual(overall["fresh_final_successes"], 1)
            self.assertEqual(overall["router"], {
                "eligible": 2,
                "correct": 1,
                "accuracy": 0.5,
                "unknown": 1,
            })
            self.assertEqual(overall["usage"]["credits"]["total"], 150.0)
            self.assertEqual(overall["usage"]["tokens"]["total"], 1800.0)
            self.assertEqual(overall["usage"]["model_calls"]["total"], 4.0)
            self.assertEqual(overall["usage"]["tool_calls"]["synth"], 6)
            self.assertEqual(overall["acceleration_vs_baseline"]["count"], 1)
            self.assertEqual(overall["patch"]["rejections"], 1)
            self.assertEqual(
                overall["patch"]["reasons"], {"PATCH_POLICY_REJECTED": 1}
            )
            self.assertEqual(overall["failures"]["by_stage"], {"EXECUTOR": 1})
            self.assertEqual(
                outcome.summary["by_mode"]["OPTIMIZE"]["e2e_successes"], 1
            )
            self.assertEqual(
                outcome.summary["by_mode"]["REPAIR"]["e2e_successes"], 0
            )
            self.assertEqual(
                outcome.summary["by_mode"]["STRUCTURAL_FIX"]["e2e_successes"], 1
            )
            self.assertEqual(
                outcome.summary["by_routed_mode"]["REPAIR"]["e2e_successes"], 1
            )
            self.assertEqual(
                outcome.summary["by_expected_mode_by_evidence"]
                ["DETERMINISTIC"]["OPTIMIZE"]["runs"],
                1,
            )
            self.assertEqual(outcome.summary["real_evidence_headline"]["runs"], 0)

            lines = (output / "benchmark_results.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "summary.csv").is_file())
            self.assertEqual(
                (output / "benchmark_summary.json").read_bytes(),
                (output / "summary.json").read_bytes(),
            )
            self.assertEqual(
                (output / "benchmark_summary.csv").read_bytes(),
                (output / "summary.csv").read_bytes(),
            )
            report = (output / "report.md").read_text(encoding="utf-8")
            self.assertEqual(
                (output / "benchmark_report.md").read_text(encoding="utf-8"),
                report,
            )
            self.assertIn(
                "DEMO and DETERMINISTIC",
                report,
            )
            self.assertIn("6/6/4/4", report)
            with (output / "summary.csv").open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["run_id"] for row in rows}, {
                record["run_id"] for record in outcome.records
            })

    def test_resume_uses_fingerprint_and_does_not_reinvoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            task_dir = write_task(root, "only_task")
            output = Path(directory) / "out"
            config = BenchmarkConfig(
                corpus=root,
                output_dir=output,
                models=("same-model",),
                repeats=2,
                backend="deterministic",
            )
            first_executor = FakeExecutor()
            first = BatchBenchmarkRunner(config, first_executor).run()
            self.assertEqual(len(first_executor.calls), 2)

            resume_executor = FakeExecutor()
            resumed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("same-model",),
                    repeats=2,
                    backend="deterministic",
                    resume=True,
                ),
                resume_executor,
            ).run()
            self.assertEqual(resume_executor.calls, [])
            self.assertEqual(resumed.summary["execution"]["resumed_runs"], 2)
            self.assertTrue(all(record["resumed"] for record in resumed.records))
            self.assertEqual(
                [record["run_id"] for record in first.records],
                [record["run_id"] for record in resumed.records],
            )
            self.assertEqual(
                len((output / "benchmark_results.jsonl").read_text().splitlines()), 2
            )

            (task_dir / "kernel.cpp").write_text(
                "void kernel(int *x) { x[0] += 2; }\n", encoding="utf-8"
            )
            changed_executor = FakeExecutor()
            changed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("same-model",),
                    repeats=2,
                    backend="deterministic",
                    resume=True,
                ),
                changed_executor,
            ).run()
            self.assertEqual(len(changed_executor.calls), 2)
            self.assertEqual(changed.summary["execution"]["new_runs"], 2)
            self.assertNotEqual(
                first.records[0]["run_fingerprint"],
                changed.records[0]["run_fingerprint"],
            )

    def test_task_load_failure_is_a_record_and_later_task_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            broken = root / "a_broken"
            broken.mkdir(parents=True)
            (broken / "task.toml").write_text(
                'task_id="a_broken"\ntop="kernel"\n'
                'kernel_file="missing.cpp"\npublic_tb="missing_tb.cpp"\n',
                encoding="utf-8",
            )
            write_task(root, "b_good")
            executor = FakeExecutor()
            outcome = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "out",
                    backend="deterministic",
                ),
                executor,
            ).run()

            self.assertEqual(len(outcome.records), 2)
            self.assertEqual(outcome.records[0]["failure_stage"], "TASK_LOAD")
            self.assertFalse(outcome.records[0]["execution_started"])
            self.assertEqual(executor.calls, ["b_good"])
            self.assertTrue(outcome.records[1]["e2e_success"])

    def test_retry_failures_uses_a_fresh_independent_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "retry_task")
            output = Path(directory) / "out"
            failed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                ),
                FakeExecutor(fail_tasks={"retry_task"}),
            ).run()
            self.assertEqual(failed.records[0]["status"], "ERROR")
            failed_run_dir = failed.records[0]["run_dir"]

            retry_executor = FakeExecutor()
            retried = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                    resume=True,
                    retry_failures=True,
                ),
                retry_executor,
            ).run()
            self.assertEqual(retry_executor.calls, ["retry_task"])
            self.assertTrue(retried.records[0]["e2e_success"])
            self.assertNotEqual(retried.records[0]["run_dir"], failed_run_dir)
            self.assertIn("--retry001", retried.records[0]["run_id"])
            self.assertEqual(
                len((output / "benchmark_results.jsonl").read_text().splitlines()),
                2,
            )
            self.assertEqual(
                retried.summary["populations"]["all_attempts"]
                ["overall_all_evidence"]["runs"],
                2,
            )
            self.assertEqual(
                retried.summary["populations"]["all_attempts"]
                ["overall_all_evidence"]["e2e_success_rate"],
                0.5,
            )
            self.assertEqual(
                retried.summary["populations"]["latest_slots"]
                ["overall_all_evidence"]["runs"],
                1,
            )
            self.assertEqual(
                retried.summary["populations"]["latest_slots"]
                ["overall_all_evidence"]["e2e_success_rate"],
                1.0,
            )
            with (output / "summary.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_resume_reexecutes_when_durable_record_disagrees_with_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "tamper_task")
            output = Path(directory) / "out"
            first = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                ),
                FakeExecutor(),
            ).run()
            durable_path = Path(str(first.records[0]["run_dir"])) / "benchmark_run.json"
            durable = json.loads(durable_path.read_text(encoding="utf-8"))
            durable["credits_used"] = 999
            durable_path.write_text(json.dumps(durable), encoding="utf-8")

            executor = FakeExecutor()
            resumed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                    resume=True,
                ),
                executor,
            ).run()
            self.assertEqual(executor.calls, ["tamper_task"])
            self.assertEqual(resumed.summary["execution"]["new_runs"], 1)
            self.assertFalse(resumed.records[0]["resumed"])

    def test_demo_is_excluded_from_real_headline_and_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "demo_task")
            demo = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "demo",
                    backend="demo",
                ),
                DemoExecutor(),
            ).run()
            self.assertTrue(demo.records[0]["e2e_success"])
            self.assertEqual(demo.summary["by_evidence_class"]["DEMO"]["runs"], 1)
            self.assertEqual(demo.summary["real_evidence_headline"]["runs"], 0)

            class LyingDemo(DemoExecutor):
                def execute(self, spec, *, timeout_seconds=None):
                    result = super().execute(spec, timeout_seconds=timeout_seconds)
                    result["backend"] = {"evidence_level": "REAL_VITIS_VALIDATED"}
                    return result

            lying = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "lying",
                    backend="demo",
                ),
                LyingDemo(fingerprint="lying-demo:v1"),
            ).run()
            self.assertFalse(lying.records[0]["e2e_success"])
            self.assertEqual(lying.records[0]["failure_stage"], "EVIDENCE_POLICY")
            self.assertEqual(lying.summary["real_evidence_headline"]["runs"], 0)

    def test_vitis_backend_is_serial_and_fake_cannot_claim_real_headline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "a")
            write_task(root, "b")
            executor = RealFixtureExecutor(fingerprint="fake-real-policy-test:v1")
            outcome = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "out",
                    backend="vitis",
                ),
                executor,
            ).run()

            self.assertEqual(executor.calls, ["a", "b"])
            self.assertEqual(outcome.summary["execution"]["max_parallel_runs"], 1)
            self.assertTrue(outcome.summary["execution"]["vitis_serialized"])
            self.assertEqual(outcome.summary["by_evidence_class"]["REAL"]["runs"], 2)
            self.assertEqual(outcome.summary["real_evidence_headline"]["runs"], 0)
            self.assertTrue(
                all(record["failure_stage"] == "EVIDENCE_POLICY" for record in outcome.records)
            )

    def test_nonempty_preexisting_run_directory_is_never_used_as_a_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "stale_task")
            output = Path(directory) / "out"
            config = BenchmarkConfig(
                corpus=root,
                output_dir=output,
                backend="deterministic",
            )
            executor = FakeExecutor()
            runner = BatchBenchmarkRunner(config, executor)
            descriptor = discover_tasks(root)[0]
            planned = runner._plan([descriptor], select_models(config))
            run_id = planned[0][4]
            stale = output / "runs" / run_id
            stale.mkdir(parents=True)
            (stale / "v3_prototype_result.json").write_text(
                json.dumps(fake_result("OPTIMIZE")), encoding="utf-8"
            )

            outcome = runner.run()

            self.assertEqual(executor.calls, [])
            self.assertEqual(outcome.records[0]["status"], "ERROR")
            self.assertEqual(outcome.records[0]["failure_stage"], "EXECUTOR")
            self.assertIn("--blocked001", outcome.records[0]["run_id"])
            self.assertTrue((stale / "v3_prototype_result.json").is_file())

    def test_real_executor_rejects_identity_and_authority_overrides(self) -> None:
        forbidden = (
            "--task-dir",
            "--run-dir",
            "--thread-id",
            "--backend",
            "--planner",
            "--live-openai",
            "--patch-file",
            "--model",
            "--validation-profile",
            "--final-validation-policy",
            "--evidence-memory",
            "--max-planner-rounds",
            "--max-no-improvement-rounds",
            "--enable-final-fallback",
            "--continuation-policy",
            "--continuation-policy-version",
            "--continuation-admission-manifest",
            "--experience-mode",
            "--experience-store",
            "--experience-ranker-version",
            "--experience-admission-manifest",
            "--experience-task-split",
            "--token-budget-policy",
            "--token-budget-visibility",
            "--max-output-tokens",
            "--repair-max-output-tokens",
            "--synth-fix-max-output-tokens",
            "--structural-fix-max-output-tokens",
            "--optimize-max-output-tokens",
            "--minimum-viable-output-tokens",
            "--context-window-tokens",
            "--context-safety-margin-tokens",
            "--token-safety-margin",
            "--future-round-token-reserve",
            "--search-closeout-token-reserve",
            "--guidance-ratio",
        )
        for option in forbidden:
            with self.subTest(option=option, spelling="separate"):
                with self.assertRaisesRegex(BenchmarkError, option):
                    V3PrototypeCLIExecutor((option, "untrusted-value"))
            with self.subTest(option=option, spelling="equals"):
                with self.assertRaisesRegex(BenchmarkError, option):
                    V3PrototypeCLIExecutor((f"{option}=untrusted-value",))

    def test_experimental_token_executor_is_separate_from_product_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "token_policy")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="token-policy-experiment",
                run_fingerprint="f" * 64,
                run_dir=Path(directory) / "run",
            )
            executor = V3PrototypeCLIExecutor(
                ("--token-budget-policy", "dynamic"),
                experimental_token_policy=True,
            )
            command = executor._command(spec)

        self.assertIn(
            "llm4hls_agent.token_policy_experiment_cli", command
        )
        self.assertNotIn("llm4hls_agent.v3_prototype_cli", command)
        self.assertEqual(
            command[command.index("--token-budget-policy") + 1],
            "dynamic",
        )

    def test_experimental_token_executor_cannot_authorize_formal_real_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "token_policy_authority")
            config = BenchmarkConfig(
                corpus=root,
                output_dir=Path(directory) / "output",
                backend="vitis",
            )
            executor = V3PrototypeCLIExecutor(
                ("--token-budget-policy", "dynamic"),
                experimental_token_policy=True,
            )
            runner = BatchBenchmarkRunner(config, executor)

        self.assertFalse(runner.real_evidence_authorized)

    def test_formal_real_executor_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "token_policy_mutation")
            config = BenchmarkConfig(
                corpus=root,
                output_dir=Path(directory) / "output",
                backend="vitis",
            )
            executor = V3PrototypeCLIExecutor()
            runner = BatchBenchmarkRunner(config, executor)
            self.assertTrue(runner.real_evidence_authorized)
            executor.experimental_token_policy = True
            executor.extra_args = (
                "--token-budget-policy",
                "dynamic",
            )

            with self.assertRaisesRegex(
                BenchmarkError, "executor changed"
            ):
                runner.run()

    def test_real_executor_refuses_preexisting_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "stale_real_task")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            stale_result = run_dir / "v3_prototype_result.json"
            stale_payload = fake_result("OPTIMIZE")
            stale_result.write_text(json.dumps(stale_payload), encoding="utf-8")
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="real-model",
                repeat_index=1,
                backend="vitis",
                run_id="stale-real-run",
                run_fingerprint="f" * 64,
                run_dir=run_dir,
            )

            with self.assertRaisesRegex(
                BenchmarkExecutionError, "pre-existing V3 terminal result"
            ):
                V3PrototypeCLIExecutor().execute(spec)
            self.assertEqual(
                json.loads(stale_result.read_text(encoding="utf-8")), stale_payload
            )

    def test_max_tasks_and_runtime_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            for task_id in ("a", "b", "c"):
                write_task(root, task_id)
            limited = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "max-tasks",
                    backend="deterministic",
                    max_tasks=2,
                    repeats=2,
                ),
                FakeExecutor(),
            ).run()
            self.assertEqual(limited.summary["selection"]["tasks_selected"], 2)
            self.assertEqual(limited.summary["selection"]["planned_runs"], 4)

            class StepClock:
                def __init__(self) -> None:
                    self.value = 0.0

                def __call__(self) -> float:
                    self.value += 0.4
                    return self.value

            clock = StepClock()
            runtime_limited = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "runtime",
                    backend="deterministic",
                    max_runtime_seconds=1.0,
                ),
                FakeExecutor(),
                monotonic=clock,
            ).run()
            self.assertEqual(
                runtime_limited.summary["execution"]["stopped_reason"],
                "MAX_RUNTIME_REACHED",
            )
            self.assertLess(len(runtime_limited.records), 3)

    def test_output_directory_requires_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "task")
            output = Path(directory) / "out"
            config = BenchmarkConfig(
                corpus=root, output_dir=output, backend="deterministic"
            )
            BatchBenchmarkRunner(config, FakeExecutor()).run()
            with self.assertRaisesRegex(BenchmarkError, "--resume"):
                BatchBenchmarkRunner(config, FakeExecutor()).run()

    def test_cli_runs_small_deterministic_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "repair", task_type="repair", difficulty=2)
            write_task(root, "optimize", task_type="optimize", difficulty=3)
            experience_store = Path(directory) / "experience.jsonl"
            experience_store.write_text(
                '{"experience_id":"exp-cli"}\n', encoding="utf-8"
            )
            admission = write_ranker_admission(
                Path(directory) / "experience-admission.json",
                experience_store,
            )
            output = Path(directory) / "out"
            stdout = io.StringIO()
            code = main(
                [
                    "--corpus",
                    str(root),
                    "--output-dir",
                    str(output),
                    "--backend",
                    "deterministic",
                    "--validation-profile",
                    "fast-experiment",
                    "--experience-mode",
                    "guided",
                    "--experience-store",
                    str(experience_store),
                    "--experience-ranker-version",
                    "v3",
                    "--experience-admission-manifest",
                    str(admission),
                    "--experience-task-split",
                    "dev",
                    "--models",
                    "fixture-a,fixture-b",
                    "--model",
                    "fixture-a",
                    "--repeats",
                    "2",
                    "--max-tasks",
                    "1",
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(code, 0)
            self.assertEqual(payload["schema_version"], SUMMARY_SCHEMA)
            self.assertEqual(payload["records"], 2)
            self.assertEqual(
                payload["benchmark_summary_ref"], "benchmark_summary.json"
            )
            self.assertEqual(payload["benchmark_csv_ref"], "benchmark_summary.csv")
            self.assertEqual(payload["benchmark_report_ref"], "benchmark_report.md")
            self.assertEqual(summary["real_evidence_headline"]["runs"], 0)
            self.assertEqual(
                summary["configuration"]["validation_profile"],
                "fast-experiment",
            )
            self.assertEqual(
                summary["configuration"]["experience_mode"], "guided"
            )
            self.assertEqual(
                summary["configuration"]["experience_task_split"], "dev"
            )
            self.assertEqual(
                summary["configuration"]["experience_store_snapshot"]["sha256"],
                hashlib.sha256(experience_store.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                summary["configuration"]["experience_store_snapshot"]["role"],
                "READ_ONLY_SEED",
            )
            self.assertEqual(
                summary["by_evidence_class"]["DETERMINISTIC"]["runs"], 2
            )
            records = [
                json.loads(line)
                for line in (output / "benchmark_results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(all(record["schema_version"] == RUN_SCHEMA for record in records))
            self.assertTrue(
                all(record["status"] == "ORCHESTRATION_ONLY" for record in records)
            )
            self.assertTrue(all(record["e2e_success"] is None for record in records))
            self.assertTrue(
                all(record["fresh_final_success"] is None for record in records)
            )
            self.assertTrue(all(record["router_correct"] is None for record in records))
            self.assertTrue(
                all(record["acceleration_vs_baseline"] is None for record in records)
            )
            self.assertTrue(
                all(
                    record["orchestration_receipt_ref"] == "executor_result.json"
                    for record in records
                )
            )
            for record in records:
                receipt = json.loads(
                    (Path(str(record["run_dir"])) / "executor_result.json").read_text()
                )
                self.assertEqual(
                    receipt["result_schema"], "v3d.orchestration-receipt.v1"
                )
                for fabricated_field in (
                    "mode",
                    "phase_decision",
                    "final_validation",
                    "candidate_rounds",
                    "acceleration_vs_baseline",
                ):
                    self.assertNotIn(fabricated_field, receipt)
            deterministic = summary["by_evidence_class"]["DETERMINISTIC"]
            self.assertEqual(deterministic["runs"], 2)
            self.assertEqual(deterministic["e2e_eligible"], 0)
            self.assertIsNone(deterministic["e2e_success_rate"])
            facts = summary["implementation_facts"]
            self.assertEqual(
                set(facts["category_sha256"]),
                {
                    "batch_and_task_parsing",
                    "phase_router",
                    "planner_and_prompts",
                    "experience_layer",
                    "continuation_layer",
                    "backend_and_accounting",
                },
            )
            self.assertIn("openai_provider.py", facts["modules"])
            self.assertIn("vitis.py", facts["modules"])
            self.assertIn("final_certification.py", facts["modules"])
            self.assertIn("full_agent_manifest.py", facts["modules"])
            self.assertIn("v3_continuation.py", facts["modules"])
            self.assertNotIn("v3_experience_importer.py", facts["modules"])
            self.assertEqual(
                facts["category_sha256"]["continuation_layer"],
                json_sha256(
                    [
                        facts["modules"]["v3_continuation.py"],
                        facts["modules"]["v3_continuation_v2.py"],
                        facts["modules"]["v3_continuation_admission.py"],
                    ]
                ),
            )
            self.assertEqual(
                facts["category_sha256"]["backend_and_accounting"],
                json_sha256(
                    [
                        facts["modules"]["vitis.py"],
                        facts["modules"]["tools.py"],
                        facts["modules"]["budget.py"],
                        facts["modules"]["v3_prototype.py"],
                        facts["modules"]["final_certification.py"],
                    ]
                ),
            )
            plan = json.loads((output / "benchmark_plan.json").read_text())
            self.assertEqual(
                plan["implementation_fingerprint"],
                summary["implementation_fingerprint"],
            )

    def test_real_validator_binds_model_provider_usage_and_all_decisive_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "real_task")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            executor = V3PrototypeCLIExecutor()
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="real-fixture-run",
                run_fingerprint="f" * 64,
                run_dir=Path(directory) / "run",
            )
            result = write_real_fixture(executor, spec)
            receipt = executor._validate_terminal_provenance(spec, result)
            self.assertEqual(
                receipt["source_result"]["sha256"],
                hashlib.sha256(
                    (spec.run_dir / "v3_certified_result.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                receipt["independent_certification"]["agent_credits_charged"],
                0,
            )
            self.assertEqual(
                receipt["model_outcomes"][0]["model"], "scheduled-model"
            )
            self.assertEqual(receipt["token_ledger"]["tokens_used"], 10)
            self.assertEqual(
                set(receipt["final_validation_artifacts"]),
                {"csim", "synth", "cosim"},
            )

            bad_spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="bad-model-run",
                run_fingerprint="e" * 64,
                run_dir=Path(directory) / "bad-model",
            )
            bad_result = write_real_fixture(
                executor, bad_spec, outcome_model="different-model"
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError, "provider/model binding"
            ):
                executor._validate_terminal_provenance(bad_spec, bad_result)

            bad_ledger_spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="bad-ledger-run",
                run_fingerprint="d" * 64,
                run_dir=Path(directory) / "bad-ledger",
            )
            bad_ledger_result = write_real_fixture(
                executor, bad_ledger_spec, ledger_tokens=11
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError, "token ledger"
            ):
                executor._validate_terminal_provenance(
                    bad_ledger_spec, bad_ledger_result
                )

    def test_real_validator_accepts_only_the_bounded_runtime_command_suffix(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "real_task")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            executor = V3PrototypeCLIExecutor()
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="bounded-command-run",
                run_fingerprint="f" * 64,
                run_dir=Path(directory) / "run",
            )
            result = write_real_fixture(executor, spec)
            scheduled = executor._command(spec)
            command_path = spec.run_dir / "benchmark_executor_command.json"
            write_canonical_json(
                command_path,
                scheduled
                + [
                    "--run-deadline-monotonic",
                    "12345.5",
                    "--cleanup-reserve-seconds",
                    "30",
                ],
            )
            executor._validate_terminal_provenance(spec, result)

            invalid_suffixes = (
                [
                    "--run-deadline-monotonic",
                    "nan",
                    "--cleanup-reserve-seconds",
                    "30",
                ],
                [
                    "--run-deadline-monotonic",
                    "12345.5",
                    "--cleanup-reserve-seconds",
                    "29",
                ],
                ["--unexpected-runtime-override", "1"],
            )
            for invalid_suffix in invalid_suffixes:
                with self.subTest(invalid_suffix=invalid_suffix):
                    write_canonical_json(
                        command_path, scheduled + invalid_suffix
                    )
                    with self.assertRaisesRegex(
                        BenchmarkExecutionError,
                        "command does not match the scheduled run",
                    ):
                        executor._validate_terminal_provenance(spec, result)

    def test_real_validator_covers_success_and_provider_rejection_actions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "mixed_provider_outcomes")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            executor = V3PrototypeCLIExecutor()

            def fixture(
                name: str,
                *,
                omissions: tuple[str, ...] = (),
                tamper_hash: bool = False,
                tamper_identity: bool = False,
                token_overrun: bool = False,
            ) -> tuple[BenchmarkRunSpec, dict[str, object]]:
                spec = BenchmarkRunSpec(
                    task=descriptor.task,
                    descriptor=descriptor,
                    model="scheduled-model",
                    repeat_index=1,
                    backend="vitis",
                    run_id=name,
                    run_fingerprint=hashlib.sha256(
                        name.encode("utf-8")
                    ).hexdigest(),
                    run_dir=Path(directory) / name,
                )
                result = write_real_fixture(
                    executor,
                    spec,
                    include_provider_rejection=True,
                    provider_rejection_package_omissions=omissions,
                    tamper_provider_rejection_hash=tamper_hash,
                    tamper_provider_rejection_identity=tamper_identity,
                    provider_rejection_token_overrun=token_overrun,
                )
                return spec, result

            spec, result = fixture("mixed-valid")
            receipt = executor._validate_terminal_provenance(spec, result)
            self.assertEqual(receipt["token_ledger"]["llm_calls"], 2)
            self.assertEqual(receipt["token_ledger"]["tokens_used"], 21)
            self.assertEqual(len(receipt["model_outcomes"]), 2)
            self.assertEqual(
                {
                    item["outcome"]
                    for item in receipt["model_outcomes"]
                },
                {"PROPOSAL", "PROVIDER_OUTPUT_REJECTED"},
            )
            rejected = next(
                item
                for item in receipt["model_outcomes"]
                if item["outcome"] == "PROVIDER_OUTPUT_REJECTED"
            )
            self.assertEqual(
                rejected["rejection_reason"], "PATCH_INCOMPLETE"
            )
            self.assertTrue(str(rejected["rejection_ref"]).endswith(".json"))
            manifest = json.loads(
                (
                    spec.run_dir / "control" / "package_manifest.json"
                ).read_text(encoding="utf-8")
            )
            manifest_paths = {
                item["path"] for item in manifest["artifacts"]
            }
            rejected_action_id = rejected["action_id"]
            self.assertTrue(
                {
                    rejected["request_ref"],
                    rejected["outcome_ref"],
                    rejected["rejection_ref"],
                    (
                        "control/live_planner_actions/"
                        f"{rejected_action_id}.started.json"
                    ),
                    (
                        "control/live_planner_actions/"
                        f"{rejected_action_id}.completed.json"
                    ),
                }.issubset(manifest_paths)
            )

            for name, omissions, message in (
                (
                    "missing-failure-input",
                    ("INPUT",),
                    "absent from package",
                ),
                (
                    "missing-failure-request",
                    ("REQUEST",),
                    "absent from package",
                ),
                (
                    "missing-failure-outcome",
                    ("OUTCOME",),
                    "absent from package",
                ),
                (
                    "missing-failure-rejection",
                    ("REJECTION",),
                    "lacks one packaged decision",
                ),
                (
                    "ignored-failure-action",
                    (
                        "REQUEST",
                        "STARTED",
                        "COMPLETED",
                        "OUTCOME",
                        "REJECTION",
                    ),
                    "do not cover all completed ledger calls",
                ),
            ):
                with self.subTest(name=name):
                    bad_spec, bad_result = fixture(
                        name, omissions=omissions
                    )
                    with self.assertRaisesRegex(
                        BenchmarkExecutionError, message
                    ):
                        executor._validate_terminal_provenance(
                            bad_spec, bad_result
                        )

            tampered_spec, tampered_result = fixture(
                "tampered-rejection-hash", tamper_hash=True
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError,
                "provider rejection decision binding mismatch",
            ):
                executor._validate_terminal_provenance(
                    tampered_spec, tampered_result
                )

            identity_spec, identity_result = fixture(
                "tampered-rejection-identity", tamper_identity=True
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError,
                "deterministic identity",
            ):
                executor._validate_terminal_provenance(
                    identity_spec, identity_result
                )

            overrun_spec, overrun_result = fixture(
                "provider-rejection-token-overrun", token_overrun=True
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError,
                "token ledger",
            ):
                executor._validate_terminal_provenance(
                    overrun_spec, overrun_result
                )

    def test_real_validator_accepts_task_contract_without_optional_cosim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "task_contract_real", requires_cosim=False)
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            executor = V3PrototypeCLIExecutor(
                final_validation_policy="task_contract"
            )
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="task-contract-real-run",
                run_fingerprint="a" * 64,
                run_dir=Path(directory) / "run",
            )
            result = write_real_fixture(
                executor,
                spec,
                final_validation_policy="task_contract",
            )
            receipt = executor._validate_terminal_provenance(spec, result)
            self.assertEqual(
                set(receipt["final_validation_artifacts"]),
                {"csim", "synth"},
            )
            self.assertEqual(
                result["final_validation"]["cosim"],
                {"status": "NOT_RUN"},
            )

    def test_real_validator_task_contract_still_requires_contract_cosim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "task_contract_cosim", requires_cosim=True)
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            executor = V3PrototypeCLIExecutor(
                final_validation_policy="task_contract"
            )
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="task-contract-cosim-run",
                run_fingerprint="b" * 64,
                run_dir=Path(directory) / "run",
            )
            result = write_real_fixture(
                executor,
                spec,
                final_validation_policy="task_contract",
            )
            receipt = executor._validate_terminal_provenance(spec, result)
            self.assertEqual(
                set(receipt["final_validation_artifacts"]),
                {"csim", "synth", "cosim"},
            )

    def test_real_resume_revalidates_package_and_recovers_in_a_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "real_resume")
            output = Path(directory) / "out"

            first_executor = V3PrototypeCLIExecutor()
            first_calls: list[str] = []

            def first_execute(spec, *, timeout_seconds=None):
                del timeout_seconds
                first_calls.append(spec.run_id)
                return write_real_fixture(first_executor, spec)

            first_executor.execute = first_execute  # type: ignore[method-assign]
            first = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("scheduled-model",),
                    backend="vitis",
                ),
                first_executor,
            ).run()
            self.assertEqual(len(first_calls), 1)
            self.assertTrue(first.records[0]["e2e_success"])

            first_run_dir = Path(str(first.records[0]["run_dir"]))
            final_csim = first_run_dir / "actions/final-csim/result.json"
            tampered = json.loads(final_csim.read_text(encoding="utf-8"))
            tampered["ok"] = False
            final_csim.write_text(json.dumps(tampered), encoding="utf-8")

            recovery_executor = V3PrototypeCLIExecutor()
            recovery_calls: list[str] = []

            def recovery_execute(spec, *, timeout_seconds=None):
                del timeout_seconds
                recovery_calls.append(spec.run_id)
                return write_real_fixture(recovery_executor, spec)

            recovery_executor.execute = recovery_execute  # type: ignore[method-assign]
            resumed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("scheduled-model",),
                    backend="vitis",
                    resume=True,
                ),
                recovery_executor,
            ).run()
            self.assertEqual(len(recovery_calls), 1)
            self.assertIn("--recovery001", recovery_calls[0])
            self.assertFalse(resumed.records[0]["resumed"])
            self.assertEqual(
                resumed.summary["latest_slot_population"]["e2e_success_rate"],
                1.0,
            )
            self.assertEqual(
                resumed.summary["all_attempt_population"]["runs"], 2
            )
            self.assertEqual(
                resumed.summary["all_attempt_population"]["e2e_success_rate"],
                0.5,
            )
            self.assertEqual(
                resumed.summary["real_evidence_headline"]["e2e_success_rate"],
                0.5,
            )
            self.assertEqual(
                resumed.summary["populations"]["all_attempts"]
                ["overall_all_evidence"]["failures"]["by_stage"],
                {"PROVENANCE_RESUME": 1},
            )

    def test_summary_builder_keeps_demo_and_real_populations_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkConfig(
                corpus=Path(directory),
                output_dir=Path(directory) / "out",
                backend="deterministic",
            )
            base = {
                "run_id": "demo",
                "run_fingerprint": "a" * 64,
                "e2e_success": True,
                "fresh_final_success": True,
                "final_validation_success": True,
                "router_correct": True,
                "routed_mode": "OPTIMIZE",
                "model": "fixture",
                "credits_used": 1,
                "tokens_used": 2,
                "tool_calls": {"llm": 1},
                "wall_time_s": 0.1,
                "patch_candidates": 0,
                "patch_rejections": 0,
            }
            demo = {**base, "evidence_class": "DEMO", "real_evidence_eligible": False}
            deterministic = {
                **base,
                "run_id": "det",
                "run_fingerprint": "b" * 64,
                "evidence_class": "DETERMINISTIC",
                "real_evidence_eligible": False,
            }
            real = {
                **base,
                "run_id": "real",
                "run_fingerprint": "c" * 64,
                "evidence_class": "REAL",
                "real_evidence_eligible": True,
                "e2e_success": False,
                "fresh_final_success": False,
                "failure_stage": "FINAL_COSIM",
                "stop_reason": "FINAL_COSIM_FAILED",
            }
            summary = build_summary(
                config=config,
                executor_fingerprint="fixture",
                descriptors_found=0,
                selected_tasks=[],
                selected_models=("fixture",),
                planned_runs=3,
                records=[demo, deterministic, real],
                new_runs=3,
                resumed_runs=0,
                stopped_reason=None,
                batch_elapsed_s=0.3,
            )
            self.assertEqual(summary["schema_version"], SUMMARY_SCHEMA)
            self.assertEqual(summary["overall_all_evidence"]["runs"], 3)
            self.assertEqual(summary["real_evidence_headline"]["runs"], 1)
            self.assertEqual(summary["real_evidence_headline"]["e2e_successes"], 0)
            self.assertEqual(summary["by_evidence_class"]["DEMO"]["runs"], 1)
            self.assertEqual(
                summary["by_evidence_class"]["DETERMINISTIC"]["runs"], 1
            )


if __name__ == "__main__":
    unittest.main()
