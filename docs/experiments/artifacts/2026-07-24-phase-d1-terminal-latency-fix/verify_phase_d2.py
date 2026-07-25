#!/usr/bin/env python3
"""Verify the four Phase D2 runs without modifying their artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Mapping


FROZEN_HEAD = "4a05763b593a527878a0056f64763126c58ee63b"
TASK_IDS = (
    "v3d_fast_016",
    "v3d_fast_020",
    "v3d_fast_021",
    "v3d_fast_022",
)
P0_MARKERS = (
    "TERMINAL_LAST_CANDIDATE_BINDING",
    "INVALID_CANDIDATE_WORST_LATENCY",
)
RESTRICTED_PATH_PARTS = {"hidden", "reference", "golden"}


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def safe_ref(root: Path, reference: object) -> Path | None:
    if not isinstance(reference, str) or not reference:
        return None
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def manifest_check(
    run_dir: Path,
    result: Mapping[str, object],
    row: Mapping[str, object],
) -> dict[str, object]:
    package = result.get("package")
    package = package if isinstance(package, Mapping) else {}
    manifest_ref = package.get("manifest_ref")
    manifest_path = safe_ref(run_dir, manifest_ref)
    manifest = (
        load_json(manifest_path)
        if manifest_path is not None and manifest_path.is_file()
        else {}
    )
    receipt = row.get("provenance_validation")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    receipt_manifest = receipt.get("package_manifest")
    receipt_manifest = (
        receipt_manifest if isinstance(receipt_manifest, Mapping) else {}
    )
    artifact_checks: list[dict[str, object]] = []
    for item in manifest.get("artifacts", []):
        if not isinstance(item, Mapping):
            artifact_checks.append({"status": "FAIL", "reason": "NOT_OBJECT"})
            continue
        path = safe_ref(run_dir, item.get("path"))
        exists = path is not None and path.is_file()
        size_matches = bool(
            exists and path.stat().st_size == item.get("size_bytes")
        )
        hash_matches = bool(
            exists and sha256_file(path) == item.get("sha256")
        )
        artifact_checks.append(
            {
                "path": item.get("path"),
                "exists": exists,
                "size_matches": size_matches,
                "sha256_matches": hash_matches,
                "status": (
                    "PASS"
                    if exists and size_matches and hash_matches
                    else "FAIL"
                ),
            }
        )
    manifest_paths = {
        str(item.get("path"))
        for item in manifest.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    decision_ref = result.get("decision_ref")
    binding = result.get("terminal_candidate_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    source_ref = binding.get("source_ref")
    restricted_paths = sorted(
        path
        for path in manifest_paths
        if RESTRICTED_PATH_PARTS.intersection(Path(path).parts)
    )
    checks = {
        "manifest_exists": manifest_path is not None and manifest_path.is_file(),
        "manifest_file_sha256_matches_batch_receipt": bool(
            manifest_path is not None
            and manifest_path.is_file()
            and sha256_file(manifest_path)
            == receipt_manifest.get("file_sha256")
        ),
        "logical_manifest_sha256_matches_result_and_receipt": bool(
            package.get("manifest_sha256")
            and package.get("manifest_sha256")
            == receipt_manifest.get("sha256")
        ),
        "all_manifest_artifacts_match": bool(artifact_checks)
        and all(item["status"] == "PASS" for item in artifact_checks),
        "decision_ref_covered": decision_ref in manifest_paths,
        "bound_source_covered": source_ref in manifest_paths,
        "restricted_artifact_paths_absent": not restricted_paths,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "artifact_count": len(artifact_checks),
        "failed_artifacts": [
            item for item in artifact_checks if item["status"] != "PASS"
        ],
        "restricted_artifact_paths": restricted_paths,
        "manifest_ref": manifest_ref,
        "manifest_file_sha256": (
            sha256_file(manifest_path)
            if manifest_path is not None and manifest_path.is_file()
            else None
        ),
        "manifest_logical_sha256": package.get("manifest_sha256"),
    }


def ledger_check(
    run_dir: Path, result: Mapping[str, object]
) -> dict[str, object]:
    budget = result.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}
    state = load_json(run_dir / "budget_state.json")
    events = [
        json.loads(line)
        for line in (run_dir / "budget_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    terminal = [
        event
        for event in events
        if event.get("state") in {"COMPLETED", "AMBIGUOUS"}
    ]
    completed = [
        event for event in terminal if event.get("state") == "COMPLETED"
    ]
    llm_terminal = [
        event for event in terminal if event.get("kind") == "llm"
    ]
    counts = Counter(str(event.get("kind")) for event in terminal)
    tool_used = budget.get("tool_used")
    tool_used = tool_used if isinstance(tool_used, Mapping) else {}
    tool_costs = budget.get("tool_costs")
    tool_costs = tool_costs if isinstance(tool_costs, Mapping) else {}
    calculated_credits = sum(
        int(tool_used.get(kind, 0)) * int(tool_costs.get(kind, 0))
        for kind in ("csim", "synth", "cosim", "llm")
    )
    runtime_keys = {"runtime_used_seconds", "runtime_remaining_seconds"}
    result_non_runtime = {
        key: value for key, value in budget.items() if key not in runtime_keys
    }
    state_non_runtime = {
        key: value for key, value in state.items() if key not in runtime_keys
    }
    result_runtime = float(budget.get("runtime_used_seconds", 0.0))
    state_runtime = float(state.get("runtime_used_seconds", 0.0))
    result_remaining = float(
        budget.get("runtime_remaining_seconds", 0.0)
    )
    state_remaining = float(state.get("runtime_remaining_seconds", 0.0))
    checks = {
        "budget_state_non_runtime_fields_match_result": (
            state_non_runtime == result_non_runtime
        ),
        "budget_state_runtime_is_monotonic_packaging_snapshot": (
            state_runtime >= result_runtime
            and state_remaining <= result_remaining
            and abs(
                (state_runtime + state_remaining)
                - (result_runtime + result_remaining)
            )
            < 1e-6
            and state_runtime - result_runtime < 5.0
        ),
        "all_actions_completed": len(terminal) == len(completed),
        "tool_counts_match": all(
            counts.get(kind, 0) == int(tool_used.get(kind, 0))
            for kind in ("csim", "synth", "cosim", "llm")
        ),
        "llm_tokens_match": sum(
            int(event.get("tokens_used", 0)) for event in llm_terminal
        )
        == budget.get("tokens_used"),
        "llm_input_tokens_match": sum(
            int(event.get("input_tokens", 0)) for event in llm_terminal
        )
        == budget.get("input_tokens_used"),
        "llm_output_tokens_match": sum(
            int(event.get("output_tokens", 0)) for event in llm_terminal
        )
        == budget.get("output_tokens_used"),
        "llm_cached_tokens_match": sum(
            int(event.get("cached_input_tokens", 0))
            for event in llm_terminal
        )
        == budget.get("cached_input_tokens_used"),
        "credits_match_cost_formula": calculated_credits
        == budget.get("credits_used"),
        "no_pending_reservations": (
            budget.get("pending_credits_reserved") == 0
            and budget.get("pending_tokens_reserved") == 0
            and all(
                int(value) == 0
                for value in dict(budget.get("tool_pending", {})).values()
            )
        ),
        "token_usage_complete": budget.get("token_usage_complete") is True,
        "within_token_limit": (
            isinstance(budget.get("tokens_used"), int)
            and isinstance(budget.get("token_limit"), int)
            and int(budget["tokens_used"]) <= int(budget["token_limit"])
        ),
        "within_credit_limit": (
            isinstance(budget.get("credits_used"), int)
            and isinstance(budget.get("credit_limit"), int)
            and int(budget["credits_used"]) <= int(budget["credit_limit"])
        ),
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "tool_used": dict(tool_used),
        "tokens_used": budget.get("tokens_used"),
        "credits_used": budget.get("credits_used"),
        "runtime_used_seconds": budget.get("runtime_used_seconds"),
    }


def binding_check(
    run_dir: Path, result: Mapping[str, object]
) -> dict[str, object]:
    binding = result.get("terminal_candidate_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    registry = load_json(run_dir / "candidate_registry.json")
    candidates = registry.get("candidates")
    candidates = candidates if isinstance(candidates, Mapping) else {}
    candidate_id = binding.get("candidate_id")
    candidate = (
        candidates.get(candidate_id)
        if isinstance(candidate_id, str)
        else None
    )
    candidate = candidate if isinstance(candidate, Mapping) else {}
    source_path = safe_ref(run_dir, binding.get("source_ref"))
    decision_path = safe_ref(run_dir, binding.get("candidate_decision_ref"))
    decision = (
        load_json(decision_path)
        if decision_path is not None and decision_path.is_file()
        else {}
    )
    digest_payload = dict(binding)
    recorded_binding_sha256 = digest_payload.pop("binding_sha256", None)
    final_candidate_id = result.get("final_candidate_id")
    final_binding_consistent = (
        not isinstance(final_candidate_id, str)
        or (
            final_candidate_id == candidate_id
            and binding.get("binding_source")
            == "FINAL_VERIFIED_CANDIDATE"
        )
    )
    checks = {
        "candidate_id_real": isinstance(candidate_id, str)
        and bool(candidate_id),
        "candidate_present_in_registry": bool(candidate),
        "source_ref_safe_and_present": source_path is not None
        and source_path.is_file(),
        "source_sha256_matches": bool(
            source_path is not None
            and source_path.is_file()
            and sha256_file(source_path) == binding.get("source_sha256")
        ),
        "registry_code_hash_matches": bool(
            candidate
            and candidate.get("code_hash") == binding.get("source_sha256")
        ),
        "decision_ref_safe_and_present": decision_path is not None
        and decision_path.is_file(),
        "decision_registry_revision_matches": (
            decision.get("registry_revision")
            == binding.get("registry_revision")
            == registry.get("v3_revision")
            == result.get("registry_revision")
        ),
        "result_decision_ref_matches": (
            result.get("decision_ref")
            == binding.get("candidate_decision_ref")
        ),
        "binding_sha256_matches": (
            recorded_binding_sha256 == canonical_sha256(digest_payload)
        ),
        "final_candidate_consistent": final_binding_consistent,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "candidate_id": candidate_id,
        "source_sha256": binding.get("source_sha256"),
        "binding_source": binding.get("binding_source"),
        "binding_sha256": recorded_binding_sha256,
        "validation_status": binding.get("validation_status"),
        "terminal_reconciliation": result.get("terminal_reconciliation"),
    }


def latency_check(result: Mapping[str, object]) -> dict[str, object]:
    rounds = result.get("candidate_rounds")
    rounds = rounds if isinstance(rounds, list) else []
    valid_statuses = {"VALID", "MISSING", "INVALID", "NOT_REPORTED"}
    rows: list[dict[str, object]] = []
    for item in rounds:
        if not isinstance(item, Mapping):
            rows.append({"status": "FAIL", "reason": "NOT_OBJECT"})
            continue
        latency_status = item.get("latency_status")
        comparability = item.get("performance_comparability")
        acceleration = item.get("acceleration_vs_baseline")
        safe_noncomparable = (
            comparability != "NOT_COMPARABLE"
            or (
                acceleration is None
                and item.get("latency_worst") is None
                and bool(item.get("performance_reason_codes"))
            )
        )
        finite_acceleration = (
            acceleration is None
            or (
                isinstance(acceleration, (int, float))
                and not isinstance(acceleration, bool)
                and math.isfinite(float(acceleration))
                and float(acceleration) >= 0
            )
        )
        row_checks = {
            "explicit_latency_status": latency_status in valid_statuses,
            "explicit_comparability": comparability
            in {"COMPARABLE", "NOT_COMPARABLE"},
            "safe_noncomparable_degradation": safe_noncomparable,
            "finite_or_null_acceleration": finite_acceleration,
        }
        rows.append(
            {
                "candidate_id": item.get("candidate_id"),
                "latency_status": latency_status,
                "latency_worst": item.get("latency_worst"),
                "performance_comparability": comparability,
                "performance_reason_codes": item.get(
                    "performance_reason_codes"
                ),
                "acceleration_vs_baseline": acceleration,
                "status": (
                    "PASS" if all(row_checks.values()) else "FAIL"
                ),
                "checks": row_checks,
            }
        )
    checks = {
        "all_rounds_explicit_and_safe": all(
            row["status"] == "PASS" for row in rows
        ),
        "top_level_terminal_not_error": result.get("status") != "ERROR",
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "rounds": rows,
        "invalid_latency_exercised_in_new_run": any(
            row.get("latency_status") in {"MISSING", "INVALID"}
            for row in rows
        ),
    }


def main() -> int:
    root = Path(__file__).resolve().parents[4]
    artifact_dir = Path(__file__).resolve().parent
    run_root = (
        root
        / "llm4hls_harness/runs/"
        "phase-d2-4a05763-d1cad2cd88-20260724T124625Z"
    )
    result_rows = [
        json.loads(line)
        for line in (run_root / "benchmark_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    by_task = {str(row.get("task_id")): row for row in result_rows}
    task_results: dict[str, object] = {}
    for task_id in TASK_IDS:
        row = by_task.get(task_id, {})
        run_dir_value = row.get("run_dir")
        run_dir = (
            Path(run_dir_value).resolve()
            if isinstance(run_dir_value, str)
            else Path("/nonexistent")
        )
        result = (
            load_json(run_dir / "v3_prototype_result.json")
            if (run_dir / "v3_prototype_result.json").is_file()
            else {}
        )
        binding = binding_check(run_dir, result)
        latency = latency_check(result)
        ledger = ledger_check(run_dir, result)
        manifest = manifest_check(run_dir, result, row)
        correctness = binding.get("validation_status")
        task_checks = {
            "unique_terminal_row": list(by_task).count(task_id) == 1,
            "terminal_status_explicit": result.get("status")
            in {"DONE", "FAILED"},
            "not_executor_error": result.get("status") != "ERROR",
            "terminal_binding_pass": binding["status"] == "PASS",
            "latency_handling_pass": latency["status"] == "PASS",
            "ledger_pass": ledger["status"] == "PASS",
            "manifest_pass": manifest["status"] == "PASS",
            "correctness_fact_preserved": (
                isinstance(correctness, Mapping)
                and correctness.get("csim") == "PASS"
                and correctness.get("synth") == "PASS"
            ),
            "public_interface_contract_pass": (
                isinstance(correctness, Mapping)
                and correctness.get("csim") == "PASS"
            ),
        }
        task_results[task_id] = {
            "status": "PASS" if all(task_checks.values()) else "FAIL",
            "checks": task_checks,
            "terminal_status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "run_id": row.get("run_id"),
            "run_dir": str(run_dir),
            "binding": binding,
            "latency": latency,
            "ledger": ledger,
            "manifest": manifest,
            "batch_provenance_validation": row.get(
                "provenance_validation"
            ),
            "e2e_success": row.get("e2e_success"),
        }

    marker_files: dict[str, list[str]] = {marker: [] for marker in P0_MARKERS}
    for path in run_root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {
            ".json",
            ".jsonl",
            ".log",
            ".md",
            ".txt",
        }:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in P0_MARKERS:
            if marker in text:
                marker_files[marker].append(
                    str(path.relative_to(run_root))
                )

    summary = load_json(run_root / "benchmark_summary.json")
    real = dict(summary["by_evidence_class"]["REAL"])
    usage = dict(real["usage"])
    aggregate = {
        "llm_calls": int(usage["model_calls"]["total"]),
        "tokens": int(usage["tokens"]["total"]),
        "csim": int(usage["tool_calls"]["csim"]),
        "synth": int(usage["tool_calls"]["synth"]),
        "cosim": int(usage["tool_calls"]["cosim"]),
        "credits": int(usage["credits"]["total"]),
        "wall_time_seconds": float(usage["wall_time_s"]["total"]),
    }
    postrun = load_json(artifact_dir / "phase-d2-postrun.json")
    frozen = load_json(artifact_dir / "phase-d2-frozen-snapshot.json")
    current_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    global_checks = {
        "four_unique_rows": len(result_rows) == len(TASK_IDS)
        and set(by_task) == set(TASK_IDS),
        "four_task_checks_pass": all(
            item["status"] == "PASS" for item in task_results.values()
        ),
        "p0_markers_absent": all(not paths for paths in marker_files.values()),
        "postrun_freeze_gate_pass": postrun.get("status") == "PASS",
        "head_unchanged": current_head == FROZEN_HEAD,
        "task_and_runtime_hashes_unchanged": (
            postrun.get("checks", {}).get("frozen_files_unchanged") is True
        ),
        "primary_retry_disabled": (
            postrun.get("coverage", {}).get("primary_retry_enabled") is False
        ),
        "all_slots_explicit_terminal": (
            postrun.get("coverage", {}).get("terminal_slots")
            == len(TASK_IDS)
        ),
        "no_hidden_reference_golden_artifact_paths": all(
            not item["manifest"]["restricted_artifact_paths"]
            for item in task_results.values()
        ),
        "scope_guards_preserved": all(
            frozen.get("scope_guards", {}).get(key) is False
            for key in (
                "c0_c1_c2_runtime_enabled",
                "golden_accessed",
                "hidden_accessed",
                "reference_accessed",
                "task_code_changed_by_d1",
            )
        ),
    }
    accepted = all(global_checks.values())
    output = {
        "schema_version": "phase-d2.real-run-acceptance.v1",
        "phase": "D1/D2 terminal binding and invalid latency P0 fix",
        "status": "ACCEPTED" if accepted else "REJECTED",
        "frozen_head": FROZEN_HEAD,
        "current_head": current_head,
        "frozen_patch_snapshot_id": frozen["patch_binding"]["snapshot_id"],
        "global_checks": global_checks,
        "p0_marker_files": marker_files,
        "terminal_status_counts": dict(
            sorted(
                Counter(
                    str(item["terminal_status"])
                    for item in task_results.values()
                ).items()
            )
        ),
        "aggregate_usage": aggregate,
        "tasks": task_results,
        "notes": {
            "invalid_latency_new_run_observation": (
                "The stochastic rerun produced valid latency for 021/022; "
                "the invalid-latency branch is accepted from deterministic "
                "old-artifact replay plus focused/full tests, without "
                "fabricating invalid live evidence."
            ),
            "success_rate_is_not_the_p0_gate": True,
            "secret_value_recorded": False,
        },
    }
    output_path = artifact_dir / "phase-d2-real-run-acceptance.json"
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": output["status"],
                "global_checks": global_checks,
                "terminal_status_counts": output["terminal_status_counts"],
                "aggregate_usage": aggregate,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
