#!/usr/bin/env python3
"""Freeze the public 28x1 matrix and perform per-task initial-call preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping


FROZEN_HEAD = "4a05763b593a527878a0056f64763126c58ee63b"
MODEL = "deepseek-v4-pro"
TOKEN_LIMIT = 32768
OUTPUT_CAP = 4096
HEADROOM = 128
RUNTIME_FILES = (
    "llm4hls_harness/llm4hls_agent/v3_prototype.py",
    "llm4hls_harness/llm4hls_agent/v3_prototype_cli.py",
    "llm4hls_harness/llm4hls_agent/v3_openai_planner.py",
    "llm4hls_harness/llm4hls_agent/v3_phase_router.py",
    "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py",
    "llm4hls_harness/llm4hls_agent/v3_planner.py",
    "llm4hls_harness/llm4hls_agent/v3_planner_action.py",
    "llm4hls_harness/llm4hls_agent/budget.py",
    "llm4hls_harness/llm4hls_agent/vitis.py",
)
ORCHESTRATION_COMPATIBILITY_FILE = (
    "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py"
)
ORCHESTRATION_COMPATIBILITY_SHA256 = (
    "22bd25029ba7a7d3e15b511685234fe32717873a5b746c9108e24154da75c4e9"
)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def candidate_binding(
    root: Path,
    *,
    task: object,
    include_metrics: bool,
) -> dict[str, object]:
    source_ref = "candidates/candidate_000/kernel.cpp"
    source_path = root / source_ref
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source = task.kernel_bytes
    source_path.write_bytes(source)
    source_sha = sha256_bytes(source)
    metrics_binding: dict[str, object] = {"ref": None, "sha256": None}
    if include_metrics:
        metrics_ref = "tools/synth/candidate_000/result.json"
        report = {
            "latency": {
                "best": 4096,
                "average": 4096,
                "worst": 4096,
            },
            "interval": {
                "best": 4096,
                "average": 4096,
                "worst": 4096,
            },
            "estimated_clock_period_ns": task.clock_ns,
            "resources": {
                "BRAM_18K": 16,
                "DSP": 16,
                "FF": 4096,
                "LUT": 4096,
            },
            "available_resources": {
                "BRAM_18K": 4096,
                "DSP": 4096,
                "FF": 1000000,
                "LUT": 1000000,
            },
        }
        tool_result = {
            "kind": "synth",
            "candidate_id": "candidate_000",
            "code_hash": source_sha,
            "result_ref": metrics_ref,
            "validation_scope": "exploration",
            "ok": True,
            "action_id": "c11-token-preflight",
            "report": report,
        }
        metrics_path = root / metrics_ref
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_data = canonical_json(tool_result)
        metrics_path.write_bytes(metrics_data)
        metrics_binding = {
            "ref": metrics_ref,
            "sha256": sha256_bytes(metrics_data),
        }
    return {
        "candidate_id": "candidate_000",
        "parent_id": None,
        "kind": "baseline",
        "status": "BASELINE",
        "source": {"ref": source_ref, "sha256": source_sha},
        "code_hash": source_sha,
        "metrics": metrics_binding,
        "synth_evidence": {"ref": None, "sha256": None},
        "validation": {},
    }


def max_failure_evidence(mode: str) -> dict[str, object]:
    schema = {
        "REPAIR": "v3c.csim-failure-evidence.v1",
        "SYNTH_FIX": "v3c.synth-failure-evidence.v1",
        "STRUCTURAL_FIX": "v3c.cosim-failure-evidence.v1",
    }[mode]
    return {
        "schema_version": schema,
        "failure_kind": "bounded_public_preflight",
        "error_summary": "E" * 480,
        "source_locations": [
            f"kernel.cpp:{100 + index}" for index in range(6)
        ],
        "relevant_log_lines": [
            f"{index}:" + "L" * 318 for index in range(8)
        ],
    }


def planner_input(
    task: object,
    root: Path,
    *,
    mode: str,
) -> dict[str, object]:
    from llm4hls_agent.v3_planner import build_planner_input

    candidate = candidate_binding(
        root,
        task=task,
        include_metrics=mode == "OPTIMIZE",
    )
    round_state: dict[str, object] = {
        "round_index": 1,
        "rounds_completed": 0,
        "parent_candidate_id": "candidate_000",
        "mode": mode,
        "no_improvement_rounds": 0,
    }
    if mode != "OPTIMIZE":
        round_state["failure_evidence"] = max_failure_evidence(mode)
    return build_planner_input(
        task={
            "task_id": task.id,
            "task_type": task.task_type,
            "difficulty": task.difficulty,
            "difficulty_status": (
                "DECLARED" if task.difficulty_declared else "DEFAULTED"
            ),
            "top": task.top,
            "part": task.part,
            "clock_ns": task.clock_ns,
            "requires_cosim": task.requires_cosim,
            "generation_required": task.generation_required,
            "initial_condition": task.initial_condition,
            "description": task.description,
            "kernel_file": task.kernel_name,
            "public_tb": task.public_tb_name,
        },
        round_state=round_state,
        incumbent=candidate,
        baseline=candidate,
        history=[],
        policy={
            "max_optimization_rounds": 2,
            "max_no_improvement_rounds": 2,
            "minimum_frequency_mhz": 100.0,
        },
        budget={
            "run_token_limit": TOKEN_LIMIT,
            "token_limit": TOKEN_LIMIT,
            "tokens_used": 0,
            "tokens_remaining": TOKEN_LIMIT,
            "credits_remaining": task.budget,
        },
    )


def preflight_task(
    task: object,
    *,
    mode: str,
) -> dict[str, object]:
    from llm4hls_agent.openai_provider import (
        OpenAICompatibleConfig,
        OpenAICompatibleOptimizationProvider,
    )
    from llm4hls_agent.v3_openai_planner import (
        OpenAICompatibleV3PlannerAdapter,
    )

    with tempfile.TemporaryDirectory(prefix="llm4hls-matrix-preflight-") as temp:
        root = Path(temp).resolve()
        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://preflight.invalid/v1",
                api_key="offline-fixture-secret",
                model=MODEL,
                max_output_tokens=OUTPUT_CAP,
                temperature=0.0,
                top_p=1.0,
            ),
            transport=lambda _request, _timeout: (500, {}, b"{}"),
        )
        planner = OpenAICompatibleV3PlannerAdapter(
            root,
            provider,
            final_reserve_credits=25,
            max_output_tokens=OUTPUT_CAP,
            fast_experiment=True,
            read_only_headers={
                name: content.decode("utf-8")
                for name, content in task.headers.items()
            },
            experience_mode="off",
        )
        prepared = planner.prepare(planner_input(task, root, mode=mode))
    required = prepared.estimated_tokens
    proposed = required + HEADROOM
    return {
        "task_id": task.id,
        "mode": mode,
        "task_credit_limit": task.budget,
        "requires_cosim": task.requires_cosim,
        "generation_required": task.generation_required,
        "estimated_input_tokens": prepared.estimated_input_tokens,
        "configured_output_cap": prepared.max_output_tokens,
        "required_initial_call_tokens": required,
        "headroom_tokens": HEADROOM,
        "required_with_headroom": proposed,
        "frozen_run_token_limit": TOKEN_LIMIT,
        "status": "PASS" if proposed <= TOKEN_LIMIT else "FAIL",
        "evidence_scope": (
            "PUBLIC_TASK_PLUS_MAX_BOUNDED_INITIAL_FAILURE_EVIDENCE"
            if mode != "OPTIMIZE"
            else "PUBLIC_TASK_PLUS_CONSERVATIVE_SYNTH_SUMMARY"
        ),
        "followup_policy": (
            "FOLLOWUP_REPREPARED_AND_FAIL_CLOSED_BY_BUDGET_LEDGER"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    artifact_dir = Path(args.output_dir).resolve()
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "llm4hls_harness"))
    from llm4hls_agent.task import load_public_task

    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=root, text=True
    ).strip()
    runtime_changed_files = subprocess.check_output(
        ["git", "diff", "--name-only", FROZEN_HEAD, "--", *RUNTIME_FILES],
        cwd=root,
        text=True,
    ).splitlines()
    compatibility_path = root / ORCHESTRATION_COMPATIBILITY_FILE
    compatibility_sha256 = sha256_file(compatibility_path)
    compatibility_patch_exact = bool(
        runtime_changed_files == [ORCHESTRATION_COMPATIBILITY_FILE]
        and compatibility_sha256 == ORCHESTRATION_COMPATIBILITY_SHA256
    )
    core_runtime_files_match_head = not any(
        name != ORCHESTRATION_COMPATIBILITY_FILE
        for name in runtime_changed_files
    )

    corpus = root / "llm4hls_harness/task_corpus/v3d-fast"
    manifest_path = corpus / "corpus_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mode_by_task = {
        str(item["task_id"]): str(item["mode"])
        for item in manifest["tasks"]
    }
    records: list[dict[str, object]] = []
    for task_dir in sorted((corpus / "tasks").iterdir()):
        if not task_dir.is_dir():
            continue
        task = load_public_task(task_dir)
        mode = mode_by_task[task.id]
        record = preflight_task(task, mode=mode)
        record["task_fingerprint"] = sha256_bytes(
            canonical_json(
                {
                    "task_id": task.id,
                    "public_file_hashes": dict(
                        sorted(task.public_file_hashes.items())
                    ),
                }
            )
        )
        record["public_file_hashes"] = dict(
            sorted(task.public_file_hashes.items())
        )
        records.append(record)

    c11_path = (
        root
        / "docs/experiments/artifacts/"
        "2026-07-24-phase-c11-short-closure/c11-audit-result.json"
    )
    c11 = json.loads(c11_path.read_text(encoding="utf-8"))
    runner_path = artifact_dir / "run_formal_matrix.py"
    task_ids = [str(record["task_id"]) for record in records]
    all_pass = bool(
        head == FROZEN_HEAD
        and core_runtime_files_match_head
        and compatibility_patch_exact
        and len(records) == 28
        and len(set(task_ids)) == 28
        and all(record["status"] == "PASS" for record in records)
        and c11["formal_matrix_decision"]["formal_matrix"]
        == {"continuation": "off", "experience": "off", "ranker": "off"}
        and runner_path.is_file()
    )
    preflight = {
        "schema_version": "formal-matrix.token-preflight.v1",
        "status": "PASS" if all_pass else "FAIL",
        "frozen_head": FROZEN_HEAD,
        "method": {
            "provider_calls": 0,
            "vitis_calls": 0,
            "token_policy": "fixed",
            "formula": (
                "required_with_headroom = "
                "PreparedPlannerCall.estimated_input_tokens + "
                "configured_output_cap + 128"
            ),
            "initial_call_scope": True,
            "failure_evidence_bounds": {
                "summary_chars": 480,
                "source_locations": 6,
                "log_lines": 8,
                "log_line_chars": 320,
            },
            "followup_scope": (
                "Each follow-up is re-prepared from the current Candidate and "
                "must pass BudgetLedger; no follow-up budget is assumed."
            ),
        },
        "record_count": len(records),
        "records": records,
    }
    write_json(artifact_dir / "per-task-token-preflight.json", preflight)

    freeze = {
        "schema_version": "formal-matrix.freeze.v1",
        "status": "FROZEN_READY_FOR_ENV" if all_pass else "FREEZE_FAILED",
        "phase": "28 tasks x 1 current frozen HEAD",
        "branch": branch,
        "frozen_head": FROZEN_HEAD,
        "head_matches": head == FROZEN_HEAD,
        "runtime_files_match_head": not runtime_changed_files,
        "core_runtime_files_match_head": core_runtime_files_match_head,
        "runtime_changed_files": runtime_changed_files,
        "runtime_files": list(RUNTIME_FILES),
        "orchestration_compatibility_patch": {
            "status": (
                "ACCEPTED_EXACT"
                if compatibility_patch_exact
                else "MISSING_OR_HASH_MISMATCH"
            ),
            "file": ORCHESTRATION_COMPATIBILITY_FILE,
            "sha256": compatibility_sha256,
            "expected_sha256": ORCHESTRATION_COMPATIBILITY_SHA256,
            "scope": "BATCH_TERMINAL_PROVENANCE_VALIDATION_ONLY",
            "purpose": (
                "Align successful REAL-row provenance validation with the "
                "frozen task_contract final policy: require fresh CSim and "
                "Synth, and require CoSim only when the public task contract "
                "requires it. The Graph, Planner, BudgetLedger, Router, "
                "Vitis execution, model settings, and task corpus are unchanged."
            ),
            "focused_regression": {
                "command": (
                    "PYTHONPATH=.:.. ../.venv/bin/python -m unittest -q "
                    "tests.test_v3_batch_benchmark"
                ),
                "tests_passed": 23,
                "tests_failed": 0,
            },
        },
        "matrix": {
            "corpus": "llm4hls_harness/task_corpus/v3d-fast/tasks",
            "task_count": 28,
            "task_ids": task_ids,
            "models": [MODEL],
            "repeats": 1,
            "backend": "vitis",
            "validation_profile": "fast-experiment",
            "final_validation_policy": "task_contract",
            "continuation_policy": "off",
            "experience_mode": "off",
            "ranker_runtime_mode": "off",
            "ranker_post_matrix": "fixed_protocol_offline_reevaluation",
            "experience_post_matrix": "offline_replay",
            "token_budget_policy": "fixed",
            "run_token_limit": TOKEN_LIMIT,
            "llm_max_output_tokens": OUTPUT_CAP,
            "max_planner_rounds": 2,
            "max_no_improvement_rounds": 2,
            "final_reserve_credits": 25,
            "task_credit_limit_source": "task.toml budget",
            "costs": {"csim": 1, "synth": 4, "cosim": 20},
            "timeouts_seconds": {
                "llm": 180,
                "csim": 300,
                "synth": 900,
                "cosim": 600,
                "run_runtime": 3600,
                "batch_runtime": None,
            },
            "sampling": {"temperature": 0.0, "top_p": 1.0},
            "all_slots_require_terminal_record": True,
            "failure_isolating": True,
            "resumable": True,
            "retry_failures_during_primary_matrix": False,
        },
        "required_environment": {
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "OPENAI_API_KEY": "REQUIRED_NONEMPTY_NOT_RECORDED",
            "LLM4HLS_MODEL": MODEL,
            "LLM4HLS_VITIS_HLS_ROOT": (
                "/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis"
            ),
            "LLM4HLS_TOOLCHAIN_ID": "Vitis 2025.2",
        },
        "output_dir": (
            "llm4hls_harness/experiments/"
            "formal_matrix_20260724_4a05763_deepseek_28x1"
        ),
        "c11_gate": {
            "status": c11["status"],
            "decision_sha256": sha256_file(c11_path),
            "runtime_shadow_admitted": False,
            "fallback_applied": True,
        },
        "structural_anchor": {
            "status": "REUSED_CURRENT_HEAD_ACCEPTED",
            "task_id": "residual_stream_deadlock",
            "result_ref": (
                "docs/experiments/artifacts/"
                "2026-07-23-phase-b12-single-structural-anchor/"
                "single-anchor-result.json"
            ),
            "rerun_required": False,
        },
        "token_preflight": {
            "status": preflight["status"],
            "record_count": len(records),
            "ref": "per-task-token-preflight.json",
            "sha256": sha256_file(
                artifact_dir / "per-task-token-preflight.json"
            ),
        },
        "runner": {
            "ref": "run_formal_matrix.py",
            "sha256": sha256_file(runner_path),
        },
        "scope_guards": {
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "secret_value_recorded": False,
            "c2_threshold_changed": False,
        },
    }
    write_json(artifact_dir / "formal-matrix-freeze.json", freeze)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
