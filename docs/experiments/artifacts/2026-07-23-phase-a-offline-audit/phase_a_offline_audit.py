"""Reproducible, read-only Phase A audit over existing public run artifacts.

This script never imports a provider client or a Vitis backend.  It reads the
already-materialized public corpus and historical run artifacts, verifies
their hashes, and writes the Phase A JSON/JSONL/Markdown evidence set.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
import tomllib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from llm4hls_agent.v3_phase_router import PhaseRouter


REPO = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
EXPERIMENTS = REPO / "docs" / "experiments"
STATUS = REPO / "docs" / "status"
CORPUS = REPO / "llm4hls_harness" / "task_corpus" / "v3d-fast"
ABC_BASE = REPO / "llm4hls_harness" / "experiments" / "token_policy_abc"
TARGET = ABC_BASE / "pilot-real-deepseek-v4-pro-20260722-01"
HISTORICAL_COMMIT = "431f7a65627ab6911bd11d1a4a8ae6f57094ab08"
INITIAL_HEAD = "65ab82c13f28921356b77ea454306d6dc1ca8e17"
OLD_REPORT = EXPERIMENTS / "2026-07-22-value-gated-pa-continuation-report.md"
CONDITION_SHORT = {
    "A_FIXED": "A",
    "B_DYNAMIC_HARD": "B",
    "C_DYNAMIC_VISIBLE": "C",
}
VALID_MODES = {"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{number}")
        rows.append(value)
    return rows


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(value: object) -> str:
    return sha256_bytes(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def git(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def status_of(value: object) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status")
        return str(status) if status is not None else None
    return None


def validation_statuses(candidate: Mapping[str, object] | None) -> dict[str, str | None]:
    validation = (
        candidate.get("final_validation")
        if isinstance(candidate, Mapping)
        and isinstance(candidate.get("final_validation"), Mapping)
        else candidate.get("validation")
        if isinstance(candidate, Mapping)
        and isinstance(candidate.get("validation"), Mapping)
        else {}
    )
    assert isinstance(validation, Mapping)
    return {
        stage: status_of(validation.get(stage))
        for stage in ("csim", "synth", "cosim")
    }


def final_validation_from_result(row: Mapping[str, object]) -> dict[str, str | None]:
    final = row.get("final_validation")
    final = final if isinstance(final, Mapping) else {}
    return {
        stage: status_of(final.get(stage))
        for stage in ("csim", "synth", "cosim")
    }


def ledger_summary(run_root: Path) -> dict[str, object]:
    path = run_root / "budget_ledger.jsonl"
    if not path.is_file():
        return {
            "available": False,
            "tokens": None,
            "credits": None,
            "calls": {},
            "initialized_at": None,
            "llm_started_at": [],
        }
    calls: Counter[str] = Counter()
    tokens = 0
    credits = 0
    initialized_at: str | None = None
    llm_started_at: list[str] = []
    for row in read_jsonl(path):
        if row.get("state") == "INITIALIZED":
            initialized_at = str(row.get("timestamp") or "") or None
        if row.get("state") == "STARTED" and row.get("kind") == "llm":
            timestamp = str(row.get("timestamp") or "")
            if timestamp:
                llm_started_at.append(timestamp)
        if row.get("state") != "COMPLETED":
            continue
        kind = str(row.get("kind") or "UNKNOWN")
        calls[kind] += 1
        raw_cost = row.get("actual_cost")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            credits += int(raw_cost)
        if kind == "llm":
            for field in ("input_tokens", "output_tokens"):
                raw_tokens = row.get(field)
                if isinstance(raw_tokens, int) and not isinstance(raw_tokens, bool):
                    tokens += raw_tokens
    return {
        "available": True,
        "tokens": tokens,
        "credits": credits,
        "calls": dict(sorted(calls.items())),
        "initialized_at": initialized_at,
        "llm_started_at": llm_started_at,
    }


def verify_artifact_manifest(
    run_root: Path, row: Mapping[str, object]
) -> dict[str, object]:
    reference = row.get("artifact_manifest_ref")
    expected_digest = row.get("artifact_manifest_sha256")
    if not isinstance(reference, str) or not reference:
        return {
            "valid": False,
            "manifest_present": False,
            "files_declared": 0,
            "files_verified": 0,
            "errors": ["MISSING_ARTIFACT_MANIFEST_REF"],
        }
    manifest_path = run_root / reference
    if not manifest_path.is_file():
        return {
            "valid": False,
            "manifest_present": False,
            "files_declared": 0,
            "files_verified": 0,
            "errors": ["MISSING_ARTIFACT_MANIFEST"],
        }
    errors: list[str] = []
    actual_digest = sha256_file(manifest_path)
    if expected_digest != actual_digest:
        errors.append("ARTIFACT_MANIFEST_SHA256_MISMATCH")
    manifest = read_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return {
            "valid": False,
            "manifest_present": True,
            "files_declared": 0,
            "files_verified": 0,
            "errors": errors + ["INVALID_ARTIFACT_FILE_MAP"],
        }
    verified = 0
    for relative, metadata in files.items():
        if not isinstance(relative, str) or not isinstance(metadata, Mapping):
            errors.append("INVALID_ARTIFACT_ENTRY")
            continue
        path = run_root / relative
        try:
            path.resolve().relative_to(run_root.resolve())
        except ValueError:
            errors.append(f"OUT_OF_ROOT:{relative}")
            continue
        if not path.is_file():
            errors.append(f"MISSING:{relative}")
            continue
        size = metadata.get("size_bytes")
        digest = metadata.get("sha256")
        if size != path.stat().st_size:
            errors.append(f"SIZE_MISMATCH:{relative}")
            continue
        if digest != sha256_file(path):
            errors.append(f"SHA256_MISMATCH:{relative}")
            continue
        verified += 1
    return {
        "valid": not errors,
        "manifest_present": True,
        "manifest_sha256": actual_digest,
        "files_declared": len(files),
        "files_verified": verified,
        "errors": errors,
    }


def failure_class(row: Mapping[str, object]) -> str:
    status = str(row.get("status") or "UNKNOWN")
    stop = str(row.get("stop_reason") or "UNKNOWN")
    final_success = row.get("final_success")
    if status == "DONE" and final_success is True:
        return "NONE"
    if status == "ERROR" and final_success is True:
        return "POST_VALIDATION_METRIC_OR_RESULT_WRITER_ERROR"
    if "NO_IMPROVEMENT" in stop or "FINAL_RESERVE" in stop:
        return "UNRESOLVED_STRUCTURAL_FIX"
    if status == "ERROR":
        return "UNRESOLVED_POST_RUN_RECONCILIATION_ERROR"
    if status == "FAILED":
        return "UNRESOLVED_TASK_FAILURE"
    return "UNKNOWN"


def candidate_map(run_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = read_json(run_root / "candidate_registry.json")
    raw = registry.get("candidates")
    candidates = dict(raw) if isinstance(raw, Mapping) else {}
    return registry, candidates


def final_candidate(
    row: Mapping[str, object],
    registry: Mapping[str, object],
    candidates: Mapping[str, object],
) -> tuple[str | None, Mapping[str, object] | None]:
    identifier = registry.get("final_candidate_id")
    if not isinstance(identifier, str) and row.get("final_success") is True:
        identifier = registry.get("best_candidate_id")
    if not isinstance(identifier, str):
        return None, None
    value = candidates.get(identifier)
    return identifier, value if isinstance(value, Mapping) else None


def task_digest_from_spec(spec: Mapping[str, object]) -> str:
    public_hashes = spec.get("public_file_hashes")
    if isinstance(public_hashes, Mapping):
        return canonical_sha256(public_hashes)
    return canonical_sha256(spec)


def reconcile_ledger(
    row: Mapping[str, object], ledger: Mapping[str, object]
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not ledger.get("available"):
        return False, ["MISSING_BUDGET_LEDGER"]
    if ledger.get("tokens") != row.get("actual_total_tokens"):
        reasons.append("TOKEN_LEDGER_MISMATCH")
    if ledger.get("credits") != row.get("credits_used"):
        reasons.append("CREDIT_LEDGER_MISMATCH")
    calls = ledger.get("calls")
    calls = calls if isinstance(calls, Mapping) else {}
    if calls.get("llm", 0) != row.get("planner_calls"):
        reasons.append("PLANNER_CALL_LEDGER_MISMATCH")
    recorded_tools = row.get("tool_calls")
    recorded_tools = recorded_tools if isinstance(recorded_tools, Mapping) else {}
    for stage in ("csim", "synth", "cosim"):
        if calls.get(stage, 0) != recorded_tools.get(stage, 0):
            reasons.append(f"{stage.upper()}_CALL_LEDGER_MISMATCH")
    return not reasons, reasons


def observed_strategy_atoms(patch: str) -> list[str]:
    lower = patch.lower()
    atoms: set[str] = set()
    checks = (
        ("hls pipeline", "PIPELINE"),
        ("hls unroll", "UNROLL"),
        ("hls dataflow", "DATAFLOW"),
        ("array_partition", "ARRAY_PARTITION"),
        ("bind_storage", "BIND_STORAGE"),
        ("stream depth", "STREAM_DEPTH"),
        ("#pragma hls stream", "STREAM_DEPTH"),
        ("interface", "INTERFACE_PRAGMA"),
        ("std::", "CPP_LIBRARY_CHANGE"),
        ("for (", "LOOP_STRUCTURE"),
        ("while (", "LOOP_STRUCTURE"),
    )
    for needle, atom in checks:
        if needle in lower:
            atoms.add(atom)
    if ".write(" in lower or ".read(" in lower:
        atoms.add("STREAM_ACCESS_ORDER")
    if any(op in patch for op in ("+", "-", "*", "/", "%")):
        atoms.add("ARITHMETIC_OR_EXPRESSION")
    if not atoms:
        atoms.add("SOURCE_PATCH_OTHER")
    return sorted(atoms)


def safe_report(run_root: Path, reference: object) -> Mapping[str, object]:
    if not isinstance(reference, str) or not reference:
        return {}
    path = run_root / reference
    try:
        path.resolve().relative_to(run_root.resolve())
    except ValueError:
        return {}
    if not path.is_file():
        return {}
    value = read_json(path)
    report = value.get("report")
    return report if isinstance(report, Mapping) else {}


def latency_worst(report: Mapping[str, object]) -> float | None:
    latency = report.get("latency")
    latency = latency if isinstance(latency, Mapping) else {}
    value = latency.get("worst")
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def area_total(report: Mapping[str, object]) -> float | None:
    resources = report.get("resources")
    resources = resources if isinstance(resources, Mapping) else {}
    values: list[float] = []
    for key in ("bram", "dsp", "ff", "lut", "uram"):
        value = resources.get(key)
        if isinstance(value, Mapping):
            value = value.get("percent")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return sum(values) if values else None


def performance_area(report: Mapping[str, object]) -> dict[str, float | None]:
    return {
        "latency_worst": latency_worst(report),
        "area_percent_sum": area_total(report),
    }


def seconds_between(start: object, end: object, limit: float) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        elapsed = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return None
    return max(0.0, limit - elapsed)


def outcome_for_followup(
    *,
    mode: str,
    incumbent: Mapping[str, object],
    candidate: Mapping[str, object],
    run_root: Path,
) -> dict[str, object]:
    parent_report = safe_report(
        run_root,
        (
            incumbent.get("metrics", {}).get("ref")
            if isinstance(incumbent.get("metrics"), Mapping)
            else None
        ),
    )
    candidate_report = safe_report(run_root, candidate.get("metrics_ref"))
    parent_latency = latency_worst(parent_report)
    candidate_latency = latency_worst(candidate_report)
    parent_area = area_total(parent_report)
    candidate_area = area_total(candidate_report)
    latency_delta = (
        candidate_latency - parent_latency
        if candidate_latency is not None and parent_latency is not None
        else None
    )
    area_delta = (
        candidate_area - parent_area
        if candidate_area is not None and parent_area is not None
        else None
    )
    candidate_status = str(candidate.get("status") or "UNKNOWN")
    validation = validation_statuses(candidate)
    incumbent_validation = (
        incumbent.get("validation")
        if isinstance(incumbent.get("validation"), Mapping)
        else {}
    )
    correctness_delta = {
        stage: {
            "before": status_of(incumbent_validation.get(stage)),
            "after": validation[stage],
        }
        for stage in ("csim", "synth", "cosim")
    }
    promoted = candidate_status in {
        "PROMOTED",
        "FINAL_VERIFIED",
        "CORRECTNESS_VERIFIED",
    }
    if mode == "STRUCTURAL_FIX":
        before_cosim = status_of(incumbent_validation.get("cosim"))
        if validation["cosim"] == "PASS" and before_cosim != "PASS":
            label = "ESSENTIAL_STRUCTURAL"
        elif candidate_status.startswith("REJECTED"):
            label = "HARMFUL"
        else:
            label = "UNRESOLVED"
    elif promoted and latency_delta is not None and latency_delta < 0:
        label = "BENEFICIAL_PERFORMANCE"
    elif promoted and area_delta is not None and area_delta < 0:
        label = "BENEFICIAL_AREA"
    elif promoted:
        label = "BENEFICIAL_CORRECTNESS"
    elif candidate_status.startswith("REJECTED"):
        label = "HARMFUL"
    else:
        label = "UNRESOLVED"
    return {
        "class": label,
        "candidate_promoted": promoted,
        "correctness_delta": correctness_delta,
        "latency_delta": latency_delta,
        "area_delta": area_delta,
    }


def relative_delta(left: object, right: object) -> float | None:
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (left, right)
    ):
        return None
    left_value, right_value = float(left), float(right)
    denominator = (abs(left_value) + abs(right_value)) / 2.0
    if denominator == 0:
        return 0.0
    return abs(left_value - right_value) / denominator


def median(values: Iterable[object]) -> float | None:
    clean = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return statistics.median(clean) if clean else None


def fmt(value: object, digits: int = 4) -> str:
    if value is None:
        return "NOT_COMPARABLE"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def rate(agreed: int, denominator: int) -> float | None:
    return agreed / denominator if denominator else None


def build_historical_inventory() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]
]:
    manifest = read_json(TARGET / "manifest.json")
    schedule = read_jsonl(TARGET / "schedule.jsonl")
    results = read_jsonl(TARGET / "token_policy_abc_reconciled_results.jsonl")
    if len(schedule) != 72 or len(results) != 72:
        raise RuntimeError("target batch does not contain exactly 72 schedule/results rows")
    by_run = {str(row["run_id"]): row for row in results}
    if len(by_run) != 72:
        raise RuntimeError("target results contain duplicate run_id values")
    slot_counts = Counter(
        (
            str(row.get("task_id")),
            str(row.get("condition")),
            int(row.get("repeat", 0)),
        )
        for row in schedule
    )
    duplicates = sorted(
        {
            f"{task}/{condition}/r{repeat}"
            for (task, condition, repeat), count in slot_counts.items()
            if count != 1
        }
    )
    tasks = sorted({str(row.get("task_id")) for row in schedule})
    conditions = sorted({str(row.get("condition")) for row in schedule})
    repeats = sorted({int(row.get("repeat", 0)) for row in schedule})
    if len(tasks) != 12 or len(conditions) != 3 or repeats != [1, 2] or duplicates:
        raise RuntimeError("target batch shape is not 12 tasks x 3 conditions x 2 repeats")

    inventory: list[dict[str, Any]] = []
    reuse_rows: list[dict[str, Any]] = []
    integrity_by_run: list[dict[str, Any]] = []
    for slot in schedule:
        run_id = str(slot["run_id"])
        row = by_run[run_id]
        run_root = TARGET / str(row["run_ref"])
        if run_root.name != run_id or not run_root.is_dir():
            raise RuntimeError(f"missing or mismatched run directory: {run_id}")
        spec = read_json(run_root / "v3_task_spec.json")
        registry, candidates = candidate_map(run_root)
        candidate_id, candidate = final_candidate(row, registry, candidates)
        final_statuses = final_validation_from_result(row)
        ledger = ledger_summary(run_root)
        ledger_ok, ledger_reasons = reconcile_ledger(row, ledger)
        artifact = verify_artifact_manifest(run_root, row)
        failure = failure_class(row)

        stability_reasons: list[str] = []
        if not artifact["valid"]:
            stability_reasons.append("HASH_MISMATCH")
        if not ledger_ok:
            stability_reasons.extend(ledger_reasons)
        if row.get("status") not in {"DONE", "FAILED", "ERROR"}:
            stability_reasons.append("UNKNOWN_TERMINAL_STATUS")
        if not isinstance(row.get("actual_total_tokens"), int):
            stability_reasons.append("MISSING_TOKENS")
        if not isinstance(row.get("planner_calls"), int):
            stability_reasons.append("MISSING_PLANNER_CALLS")
        if not isinstance(row.get("final_success"), bool):
            stability_reasons.append("MISSING_FINAL_STATE")
        if failure == "UNKNOWN":
            stability_reasons.append("UNKNOWN_FAILURE_CLASS")

        continuation_reasons: list[str] = []
        planner_calls = row.get("planner_calls")
        if not isinstance(planner_calls, int) or planner_calls <= 1:
            continuation_reasons.append("NO_FOLLOW_UP")
        else:
            pre_state_path = run_root / "planner" / "inputs" / "round_002.json"
            followup = candidates.get("candidate_002")
            if not pre_state_path.is_file():
                continuation_reasons.append("MISSING_PRE_STATE")
            if not isinstance(followup, Mapping):
                continuation_reasons.append("OUTCOME_NOT_BINDABLE")
            elif not any(
                isinstance(value, Mapping) and value.get("result_ref")
                for value in (
                    followup.get("validation", {}).values()
                    if isinstance(followup.get("validation"), Mapping)
                    else ()
                )
            ):
                continuation_reasons.append("OUTCOME_NOT_BINDABLE")

        experience_reasons: list[str] = []
        if row.get("evidence_level") not in {
            "REAL_VITIS_VALIDATED",
            "REAL_VITIS_ATTEMPT_FAILED",
        }:
            experience_reasons.append("NON_REAL_VITIS")
        if row.get("public_scope") != "PUBLIC_TRAIN_DEV_ONLY" or row.get(
            "hidden_like"
        ) is not False:
            experience_reasons.append("NON_PUBLIC_OR_HIDDEN_SCOPE")
        non_baseline = [
            (cid, value)
            for cid, value in candidates.items()
            if isinstance(value, Mapping) and value.get("kind") != "baseline"
        ]
        if not non_baseline:
            experience_reasons.append("NO_CANDIDATE")
        for cid, value in non_baseline:
            for field in ("patch_ref", "source_ref"):
                reference = value.get(field)
                if not isinstance(reference, str) or not (run_root / reference).is_file():
                    experience_reasons.append(f"MISSING_{field.upper()}:{cid}")

        reusable_stability = not stability_reasons
        reusable_continuation = not continuation_reasons
        reusable_experience = not experience_reasons
        item = {
            "run_id": run_id,
            "run_path": str(run_root),
            "task_name": str(row.get("task_id")),
            "task_digest": task_digest_from_spec(spec),
            "condition": CONDITION_SHORT.get(str(row.get("condition")), "UNKNOWN"),
            "condition_full": row.get("condition"),
            "repeat": row.get("repeat"),
            "branch": None,
            "commit_sha": manifest.get("git_commit"),
            "model_alias": row.get("model"),
            "vitis_version": manifest.get("vitis_version"),
            "validation_profile": row.get("validation_profile"),
            "final_validation_policy": None,
            "continuation_policy": None,
            "experience_mode": row.get("experience_mode"),
            "ranker_mode": "NOT_RECORDED",
            "terminal_status": row.get("status"),
            "stop_reason": row.get("stop_reason"),
            "final_candidate_id": candidate_id,
            "final_source_digest": (
                candidate.get("code_hash") if isinstance(candidate, Mapping) else None
            ),
            "final_csim": final_statuses["csim"],
            "final_synth": final_statuses["synth"],
            "final_cosim": final_statuses["cosim"],
            "final_success": row.get("final_success"),
            "tokens": row.get("actual_total_tokens"),
            "planner_calls": row.get("planner_calls"),
            "tool_credits": row.get("credits_used"),
            "csim_calls": (
                row.get("tool_calls", {}).get("csim")
                if isinstance(row.get("tool_calls"), Mapping)
                else None
            ),
            "synth_calls": (
                row.get("tool_calls", {}).get("synth")
                if isinstance(row.get("tool_calls"), Mapping)
                else None
            ),
            "cosim_calls": (
                row.get("tool_calls", {}).get("cosim")
                if isinstance(row.get("tool_calls"), Mapping)
                else None
            ),
            "wall_time_seconds": row.get("wall_time_seconds"),
            "baseline_latency": row.get("baseline_latency"),
            "final_latency": row.get("final_latency"),
            "acceleration": row.get("acceleration"),
            "failure_class": failure,
            "ledger_reconciled": ledger_ok,
            "manifest_valid": artifact["valid"],
            "reconciled_from_durable_artifacts": bool(
                row.get("reconciled_from_durable_artifacts", False)
            ),
            "reusable_for_stability": reusable_stability,
            "reusable_for_continuation": reusable_continuation,
            "reusable_for_experience": reusable_experience,
            "reuse_rejection_reasons": sorted(
                set(stability_reasons + continuation_reasons + experience_reasons)
            ),
        }
        inventory.append(item)
        reuse_rows.append(
            {
                "run_id": run_id,
                "slot": {
                    "task_name": item["task_name"],
                    "condition": item["condition"],
                    "repeat": item["repeat"],
                },
                "stability": {
                    "eligible": reusable_stability,
                    "reasons": stability_reasons,
                },
                "continuation": {
                    "eligible": reusable_continuation,
                    "reasons": continuation_reasons,
                },
                "experience_and_ranker": {
                    "eligible": reusable_experience,
                    "reasons": experience_reasons,
                    "training_ready": False,
                    "admission_status": "NOT_READY",
                },
                "artifact_manifest": artifact,
                "ledger_reconciled": ledger_ok,
            }
        )
        integrity_by_run.append(
            {
                "run_id": run_id,
                "artifact_manifest": artifact,
                "ledger_reconciled": ledger_ok,
                "ledger_reconciliation_reasons": ledger_reasons,
            }
        )

    inventory.sort(key=lambda item: int(str(item["run_id"]).split("--", 1)[0]))
    reuse_rows.sort(key=lambda item: int(str(item["run_id"]).split("--", 1)[0]))
    summary = {
        "schema_version": "phase-a.historical-72-integrity.v1",
        "target_batch": str(TARGET),
        "intended_slots": 72,
        "discovered_slots": len(inventory),
        "unique_tasks": len(tasks),
        "conditions": conditions,
        "repeats": repeats,
        "duplicate_slots": len(duplicates),
        "missing_slots": 72 - len(inventory),
        "artifact_manifests_valid": sum(
            bool(item["manifest_valid"]) for item in inventory
        ),
        "artifact_files_verified": sum(
            int(item["artifact_manifest"]["files_verified"])
            for item in integrity_by_run
        ),
        "ledger_reconciled_runs": sum(
            bool(item["ledger_reconciled"]) for item in inventory
        ),
        "stability_eligible_runs": sum(
            bool(item["reusable_for_stability"]) for item in inventory
        ),
        "continuation_eligible_runs": sum(
            bool(item["reusable_for_continuation"]) for item in inventory
        ),
        "experience_eligible_runs": sum(
            bool(item["reusable_for_experience"]) for item in inventory
        ),
        "reconciled_from_durable_artifacts": sum(
            bool(item["reconciled_from_durable_artifacts"]) for item in inventory
        ),
        "integrity_errors": [
            item for item in integrity_by_run if not item["artifact_manifest"]["valid"]
        ],
        "per_run": integrity_by_run,
    }
    return inventory, reuse_rows, summary


def build_discovery_summary(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    target_manifest = read_json(TARGET / "manifest.json")
    hybrid = (
        REPO
        / "llm4hls_harness"
        / "experiments"
        / "token_policy_hybrid_v2"
        / "pilot-real-deepseek-v4-pro-20260722-01"
    )
    hybrid_manifest = read_json(hybrid / "manifest.json")
    preflight = ABC_BASE / "preflight-real-20260722-01"
    preflight_manifest = read_json(preflight / "manifest.json")
    return {
        "schema_version": "phase-a.historical-72-discovery.v1",
        "discovery_date": "2026-07-23",
        "status": "UNIQUE_MATCH",
        "selection": {
            "path": str(TARGET),
            "reason": (
                "Only candidate with content-confirmed 12 unique tasks x "
                "3 Token Policy conditions x 2 repeats, 72 schedule rows, "
                "72 reconciled result rows, COMPLETE real-run manifest, and "
                "72 matching run directories."
            ),
            "created_at": target_manifest.get("created_at"),
            "finished_at": target_manifest.get("finished_at"),
            "git_commit": target_manifest.get("git_commit"),
            "model": target_manifest.get("model"),
            "vitis_version": target_manifest.get("vitis_version"),
            "tasks": sorted({item["task_name"] for item in inventory}),
            "conditions": sorted(
                {item["condition_full"] for item in inventory}
            ),
            "repeats": sorted({item["repeat"] for item in inventory}),
            "run_count": len(inventory),
            "real_evidence_levels": sorted(
                set(
                    str(row.get("evidence_level"))
                    for row in read_jsonl(
                        TARGET / "token_policy_abc_reconciled_results.jsonl"
                    )
                )
            ),
        },
        "candidates": [
            {
                "path": str(TARGET),
                "shape": "12 tasks x 3 conditions x 2 repeats",
                "planned_runs": target_manifest.get("planned_runs"),
                "actual_runs": target_manifest.get("actual_runs"),
                "status": target_manifest.get("status"),
                "match": True,
            },
            {
                "path": str(preflight),
                "shape": "1 task x 1 condition x 1 repeat",
                "planned_runs": preflight_manifest.get("planned_runs"),
                "actual_runs": preflight_manifest.get("actual_runs"),
                "status": preflight_manifest.get("status"),
                "match": False,
                "rejection_reason": "PREFLIGHT_ONLY_NOT_72",
            },
            {
                "path": str(hybrid),
                "shape": "12 tasks x 2 conditions x 2 repeats",
                "planned_runs": hybrid_manifest.get("planned_runs"),
                "actual_runs": hybrid_manifest.get("actual_runs"),
                "status": hybrid_manifest.get("status"),
                "match": False,
                "rejection_reason": "48_RUN_TWO_CONDITION_HYBRID_NOT_ABC",
            },
        ],
        "directory_count_not_used_as_identity": True,
    }


def build_pairs(inventory: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in inventory:
        if row["reusable_for_stability"]:
            grouped[(str(row["task_name"]), str(row["condition"]))].append(row)
    pairs: list[dict[str, Any]] = []
    agreement_fields = {
        "terminal_status": "final",
        "final_csim": "csim",
        "final_synth": "synth",
        "final_cosim": "cosim",
        "final_source_digest": "source_digest",
        "stop_reason": "stop_reason",
        "failure_class": "failure_class",
    }
    aggregate_agreed: Counter[str] = Counter()
    aggregate_comparable: Counter[str] = Counter()
    for (task, condition), rows in sorted(grouped.items()):
        rows.sort(key=lambda item: int(item["repeat"]))
        if len(rows) != 2 or {row["repeat"] for row in rows} != {1, 2}:
            pairs.append(
                {
                    "task_name": task,
                    "condition": condition,
                    "comparison_status": "NOT_COMPARABLE",
                    "reason": "PAIR_DOES_NOT_HAVE_EXACTLY_R1_AND_R2",
                }
            )
            continue
        left, right = rows
        agreements: dict[str, object] = {}
        for field, label in agreement_fields.items():
            left_value, right_value = left.get(field), right.get(field)
            comparable = left_value is not None and right_value is not None
            agreed = bool(comparable and left_value == right_value)
            agreements[label] = {
                "comparable": comparable,
                "agreed": agreed if comparable else None,
                "repeat_1": left_value,
                "repeat_2": right_value,
            }
            if comparable:
                aggregate_comparable[label] += 1
                aggregate_agreed[label] += int(agreed)
        token_abs = abs(int(left["tokens"]) - int(right["tokens"]))
        planner_delta = abs(int(left["planner_calls"]) - int(right["planner_calls"]))
        credit_delta = abs(int(left["tool_credits"]) - int(right["tool_credits"]))
        latency_abs = (
            abs(float(left["final_latency"]) - float(right["final_latency"]))
            if isinstance(left.get("final_latency"), (int, float))
            and isinstance(right.get("final_latency"), (int, float))
            else None
        )
        acceleration_abs = (
            abs(float(left["acceleration"]) - float(right["acceleration"]))
            if isinstance(left.get("acceleration"), (int, float))
            and isinstance(right.get("acceleration"), (int, float))
            else None
        )
        pairs.append(
            {
                "task_name": task,
                "condition": condition,
                "comparison_status": "COMPARABLE",
                "agreements": agreements,
                "token_absolute_delta": token_abs,
                "token_relative_delta": relative_delta(left["tokens"], right["tokens"]),
                "planner_call_delta": planner_delta,
                "tool_credit_delta": credit_delta,
                "latency_absolute_delta": latency_abs,
                "acceleration_absolute_delta": acceleration_abs,
                "acceleration_relative_delta": relative_delta(
                    left.get("acceleration"), right.get("acceleration")
                ),
            }
        )
    comparable_pairs = [row for row in pairs if row["comparison_status"] == "COMPARABLE"]
    summary = {
        "intended_slots": 72,
        "discovered_slots": len(inventory),
        "valid_reusable_slots": sum(
            bool(row["reusable_for_stability"]) for row in inventory
        ),
        "missing_slots": 72 - len(inventory),
        "duplicate_slots": 0,
        "intended_pairs": 36,
        "valid_comparable_pairs": len(comparable_pairs),
        "not_comparable_pairs": len(pairs) - len(comparable_pairs),
        "agreement": {
            label: {
                "agreed": aggregate_agreed[label],
                "comparable": aggregate_comparable[label],
                "rate": rate(
                    aggregate_agreed[label], aggregate_comparable[label]
                ),
            }
            for label in agreement_fields.values()
        },
        "median_token_absolute_delta": median(
            row.get("token_absolute_delta") for row in comparable_pairs
        ),
        "median_token_relative_delta": median(
            row.get("token_relative_delta") for row in comparable_pairs
        ),
        "maximum_token_relative_delta": max(
            (
                float(row["token_relative_delta"])
                for row in comparable_pairs
                if row.get("token_relative_delta") is not None
            ),
            default=None,
        ),
        "planner_call_agreement": {
            "agreed": sum(
                int(row.get("planner_call_delta") == 0) for row in comparable_pairs
            ),
            "comparable": len(comparable_pairs),
            "rate": rate(
                sum(int(row.get("planner_call_delta") == 0) for row in comparable_pairs),
                len(comparable_pairs),
            ),
        },
        "median_planner_call_delta": median(
            row.get("planner_call_delta") for row in comparable_pairs
        ),
        "median_tool_credit_delta": median(
            row.get("tool_credit_delta") for row in comparable_pairs
        ),
        "median_latency_absolute_delta": median(
            row.get("latency_absolute_delta") for row in comparable_pairs
        ),
        "median_acceleration_absolute_delta": median(
            row.get("acceleration_absolute_delta") for row in comparable_pairs
        ),
        "median_acceleration_relative_delta": median(
            row.get("acceleration_relative_delta") for row in comparable_pairs
        ),
        "relative_delta_definition": (
            "absolute repeat delta divided by mean absolute magnitude of the pair"
        ),
        "claim_scope": "REPEATED_PAIR_CONSISTENCY_ONLY",
        "statistical_significance_claimed": False,
    }
    return pairs, summary


def build_continuation(
    inventory: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    inventory_by_run = {str(row["run_id"]): row for row in inventory}
    result_rows = read_jsonl(TARGET / "token_policy_abc_reconciled_results.jsonl")
    for result in result_rows:
        planner_calls = result.get("planner_calls")
        if not isinstance(planner_calls, int) or planner_calls <= 1:
            continue
        run_id = str(result["run_id"])
        run_root = TARGET / str(result["run_ref"])
        decision_id = f"{run_id}:round:2"
        input_path = run_root / "planner" / "inputs" / "round_002.json"
        registry, candidates = candidate_map(run_root)
        candidate = candidates.get("candidate_002")
        reasons: list[str] = []
        if not input_path.is_file():
            reasons.append("MISSING_PRE_STATE")
        if not isinstance(candidate, Mapping):
            reasons.append("OUTCOME_NOT_BINDABLE")
        if reasons:
            excluded.append(
                {
                    "decision_id": decision_id,
                    "run_id": run_id,
                    "binding_status": "EXCLUDED",
                    "reason_codes": reasons,
                    "source_path": str(run_root),
                    "purpose": "offline_shadow_evaluation_or_candidate_pool",
                    "training_ready": False,
                    "admission_status": "NOT_READY",
                }
            )
            continue
        planner_input = read_json(input_path)
        round_state = planner_input.get("round")
        round_state = round_state if isinstance(round_state, Mapping) else {}
        budget = planner_input.get("budget")
        budget = budget if isinstance(budget, Mapping) else {}
        incumbent = planner_input.get("incumbent")
        incumbent = incumbent if isinstance(incumbent, Mapping) else {}
        history = planner_input.get("history")
        history = history if isinstance(history, list) else []
        observed_history: list[dict[str, object]] = []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            cid = item.get("candidate_id")
            source_candidate = candidates.get(cid)
            if not isinstance(source_candidate, Mapping):
                continue
            patch_ref = source_candidate.get("patch_ref")
            patch_path = run_root / str(patch_ref) if isinstance(patch_ref, str) else None
            patch = (
                patch_path.read_text(encoding="utf-8", errors="replace")
                if patch_path is not None and patch_path.is_file()
                else ""
            )
            observed_history.append(
                {
                    "candidate_id": cid,
                    "patch_sha256": source_candidate.get("patch_sha256"),
                    "observed_strategy_atoms": observed_strategy_atoms(patch),
                    "status": item.get("status"),
                }
            )
        failure_evidence = round_state.get("failure_evidence")
        failure_evidence = (
            failure_evidence if isinstance(failure_evidence, Mapping) else {}
        )
        synth_evidence = incumbent.get("synth_evidence")
        synth_evidence = synth_evidence if isinstance(synth_evidence, Mapping) else {}
        evidence_material = {
            "failure_evidence": failure_evidence,
            "incumbent_synth_evidence_sha256": synth_evidence.get("sha256"),
            "history": observed_history,
        }
        ledger = ledger_summary(run_root)
        llm_started = ledger.get("llm_started_at")
        second_started = (
            llm_started[1]
            if isinstance(llm_started, list) and len(llm_started) >= 2
            else None
        )
        incumbent_report = safe_report(
            run_root,
            (
                incumbent.get("metrics", {}).get("ref")
                if isinstance(incumbent.get("metrics"), Mapping)
                else None
            ),
        )
        outcome = outcome_for_followup(
            mode=str(round_state.get("mode") or "UNKNOWN"),
            incumbent=incumbent,
            candidate=candidate,
            run_root=run_root,
        )
        sample = {
            "decision_id": decision_id,
            "run_id": run_id,
            "binding_status": "BOUND",
            "pre_state": {
                "mode": round_state.get("mode"),
                "round": round_state.get("round_index"),
                "remaining_tokens": budget.get("tokens_remaining"),
                "remaining_credits": budget.get("credits_remaining"),
                "remaining_time_seconds": seconds_between(
                    ledger.get("initialized_at"), second_started, 3600.0
                ),
                "incumbent_digest": incumbent.get("code_hash"),
                "evidence_fingerprint": canonical_sha256(evidence_material),
                "evidence_delta": {
                    "failure_kind": failure_evidence.get("failure_kind"),
                    "incumbent_synth_evidence_sha256": synth_evidence.get("sha256"),
                    "history_count": len(observed_history),
                },
                "observed_strategy_history": observed_history,
                "current_performance_area": performance_area(incumbent_report),
            },
            "policy_decision": {
                "policy_version": None,
                "decision": "UNKNOWN",
                "reason_codes": ["POLICY_NOT_ENABLED_AT_SOURCE_COMMIT"],
            },
            "outcome_label": outcome,
            "leakage_audit": {
                "future_fields_excluded_from_policy_input": True,
                "policy_evaluator_input_path": "pre_state",
                "violations": [],
            },
            "source_binding": {
                "followup_planner_input_ref": str(input_path.relative_to(run_root)),
                "followup_candidate_id": "candidate_002",
                "followup_candidate_digest": candidate.get("code_hash"),
                "followup_patch_sha256": candidate.get("patch_sha256"),
                "source_manifest_valid": inventory_by_run[run_id]["manifest_valid"],
            },
            "purpose": "offline_shadow_evaluation_or_candidate_pool",
            "training_ready": False,
            "admission_status": "NOT_READY",
        }
        bound.append(sample)
    outcomes = Counter(
        str(row["outcome_label"]["class"])
        for row in bound
        if isinstance(row.get("outcome_label"), Mapping)
    )
    summary = {
        "runs_with_follow_up": sum(
            int(isinstance(row.get("planner_calls"), int) and row["planner_calls"] > 1)
            for row in inventory
        ),
        "follow_up_decision_points": len(bound) + len(excluded),
        "completely_bound_samples": len(bound),
        "excluded_samples": len(excluded),
        "outcome_distribution": dict(sorted(outcomes.items())),
        "exclusion_distribution": dict(
            sorted(
                Counter(
                    reason
                    for row in excluded
                    for reason in row.get("reason_codes", [])
                ).items()
            )
        ),
        "old_r02_admission": "NOT_READY",
        "current_admission": "INSUFFICIENT_EVIDENCE",
        "current_authority": "SHADOW",
        "policy_replay_executed": False,
        "policy_replay_reason": (
            "Source runs predate persisted Continuation decisions; one follow-up "
            "has no bindable Candidate, and no threshold tuning or invented "
            "readiness formula is permitted in Phase A."
        ),
        "future_fields_separated": True,
        "policy_input_boundary": "pre_state_only",
    }
    return bound, excluded, summary


def candidate_outcome(candidate: Mapping[str, object]) -> str:
    status = str(candidate.get("status") or "UNKNOWN")
    validation = validation_statuses(candidate)
    if status in {"PROMOTED", "FINAL_VERIFIED", "CORRECTNESS_VERIFIED"}:
        return "SUCCESS"
    if validation["csim"] == "FAIL":
        return "CSIM_FAILURE"
    if validation["synth"] == "FAIL":
        return "SYNTH_FAILURE"
    if validation["cosim"] == "FAIL":
        return "COSIM_FAILURE"
    if status.startswith("REJECTED"):
        return "REJECTED_NO_STRICT_IMPROVEMENT"
    return "UNRESOLVED"


def build_candidate_pools(
    inventory: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inventory_by_run = {str(row["run_id"]): row for row in inventory}
    experience: list[dict[str, Any]] = []
    ranker: list[dict[str, Any]] = []
    results = read_jsonl(TARGET / "token_policy_abc_reconciled_results.jsonl")
    for result in results:
        run_id = str(result["run_id"])
        run_root = TARGET / str(result["run_ref"])
        _, candidates = candidate_map(run_root)
        for candidate_id, candidate in sorted(candidates.items()):
            if not isinstance(candidate, Mapping) or candidate.get("kind") == "baseline":
                continue
            patch_ref = candidate.get("patch_ref")
            patch_path = run_root / str(patch_ref) if isinstance(patch_ref, str) else None
            patch = (
                patch_path.read_text(encoding="utf-8", errors="replace")
                if patch_path is not None and patch_path.is_file()
                else ""
            )
            atoms = observed_strategy_atoms(patch)
            common = {
                "record_id": f"{run_id}:{candidate_id}",
                "run_id": run_id,
                "task_name": result.get("task_id"),
                "task_family": result.get("family"),
                "mode": result.get("mode"),
                "condition": CONDITION_SHORT.get(
                    str(result.get("condition")), "UNKNOWN"
                ),
                "repeat": result.get("repeat"),
                "candidate_id": candidate_id,
                "round": candidate.get("round_index"),
                "parent_candidate_id": candidate.get("parent_id"),
                "candidate_source_digest": candidate.get("code_hash"),
                "patch_digest": candidate.get("patch_sha256"),
                "observed_strategy_atoms": atoms,
                "declared_change_class": candidate.get("change_class"),
                "outcome": candidate_outcome(candidate),
                "candidate_status": candidate.get("status"),
                "validation": validation_statuses(candidate),
                "source_kind": "REAL_LLM_VITIS",
                "scope": "PUBLIC_TRAIN_DEV_ONLY",
                "hidden_reference_golden_used": False,
                "manifest_valid": inventory_by_run[run_id]["manifest_valid"],
                "feature_boundary": (
                    "task/mode/family plus observed pre-outcome patch atoms; "
                    "future outcome excluded from retrieval features"
                ),
                "leave_out_audit_keys": {
                    "run": run_id,
                    "task": result.get("task_id"),
                    "family": result.get("family"),
                },
                "purpose": "offline_shadow_evaluation_or_candidate_pool",
                "training_ready": False,
                "admission_status": "NOT_READY",
            }
            experience.append(
                {
                    **common,
                    "record_type": "EXPERIENCE_CANDIDATE",
                    "candidate_and_outcome_bindable": True,
                }
            )
            ranker.append(
                {
                    **common,
                    "record_type": "STRATEGY_RANKER_CANDIDATE",
                    "ranker_authority": "BAYESIAN_SHADOW",
                    "learned_model_trained": False,
                }
            )
    return experience, ranker


def build_failure_taxonomy(
    inventory: list[dict[str, Any]], experience: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": "phase-a.historical-failure-taxonomy.v1",
        "scope": "72 historical public real LLM+Vitis runs",
        "terminal_status": dict(
            sorted(Counter(str(row["terminal_status"]) for row in inventory).items())
        ),
        "stop_reason": dict(
            sorted(Counter(str(row["stop_reason"]) for row in inventory).items())
        ),
        "failure_class": dict(
            sorted(Counter(str(row["failure_class"]) for row in inventory).items())
        ),
        "by_mode_and_failure_class": {
            mode: dict(
                sorted(
                    Counter(
                        str(row["failure_class"])
                        for row in inventory
                        if read_json(
                            Path(str(row["run_path"])) / "abc_run_record.json"
                        ).get("mode")
                        == mode
                    ).items()
                )
            )
            for mode in sorted(VALID_MODES)
        },
        "candidate_outcomes": dict(
            sorted(Counter(str(row["outcome"]) for row in experience).items())
        ),
        "post_validation_reconciled_runs": sum(
            int(row["reconciled_from_durable_artifacts"]) for row in inventory
        ),
        "interpretation": [
            "ERROR with final_success=true is retained as terminal ERROR and separately classified as a post-validation metric/result-writer error.",
            "No failed run is rewritten as DONE.",
            "No missing field is imputed from task names.",
        ],
    }


def current_task_records(
    inventory: list[dict[str, Any]], reuse_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = read_json(CORPUS / "corpus_manifest.json")
    tasks_raw = manifest.get("tasks")
    if not isinstance(tasks_raw, list) or len(tasks_raw) != 28:
        raise RuntimeError("current V3-D corpus is not a 28-task manifest")
    historical: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inventory:
        historical[str(row["task_name"])].append(row)
    stability_by_run = {
        str(row["run_id"]): bool(row["stability"]["eligible"])
        for row in reuse_rows
        if isinstance(row.get("stability"), Mapping)
    }
    current_head = git("rev-parse", "HEAD")
    router = PhaseRouter()
    records: list[dict[str, Any]] = []
    for manifest_task in tasks_raw:
        if not isinstance(manifest_task, Mapping):
            raise ValueError("invalid corpus task entry")
        task_id = str(manifest_task["task_id"])
        task_root = CORPUS / str(manifest_task["path"])
        task_toml = tomllib.loads(
            (task_root / "task.toml").read_text(encoding="utf-8")
        )
        acceptance = read_json(task_root / "acceptance.json")
        baseline = acceptance.get("baseline_validation")
        if not isinstance(baseline, Mapping):
            raise ValueError(f"missing development baseline facts: {task_id}")

        def record(stage: str) -> dict[str, str] | None:
            value = str(baseline.get(stage) or "NOT_RUN")
            return None if value == "NOT_RUN" else {"status": value}

        decision = router.route(
            baseline_csim=record("csim"),
            baseline_synth=record("synth"),
            baseline_cosim=record("cosim"),
            task_metadata={
                "task_type": task_toml.get("task_type", "generate"),
                "requires_cosim": bool(acceptance.get("requires_cosim")),
            },
        )
        history = historical.get(task_id, [])
        conditions = sorted({str(row["condition"]) for row in history})
        repeat_counts = {
            condition: sum(int(row["condition"] == condition) for row in history)
            for condition in conditions
        }
        reusable = bool(history) and all(
            stability_by_run.get(str(row["run_id"]), False) for row in history
        )
        public_hashes = {
            name: sha256_file(task_root / name)
            for name in (
                "description.md",
                "kernel.cpp",
                "kernel.h",
                "kernel_tb.cpp",
                "task.toml",
            )
            if (task_root / name).is_file()
        }
        records.append(
            {
                "task_name": task_id,
                "task_digest": canonical_sha256(public_hashes),
                "task_type": task_toml.get("task_type", "generate"),
                "generation_required": task_toml.get(
                    "generation_required",
                    task_toml.get("task_type", "generate") == "generate",
                ),
                "mode": decision.mode.value,
                "mode_source": (
                    "CURRENT_ROUTER_REPLAY_FROM_DEVELOPMENT_ACCEPTANCE_BASELINE_FACTS; "
                    "development-only audit oracle, never Planner-visible"
                ),
                "mode_reason": decision.reason,
                "baseline_facts": {
                    stage: baseline.get(stage)
                    for stage in ("csim", "synth", "cosim")
                },
                "requires_cosim": bool(acceptance.get("requires_cosim")),
                "in_historical_12": bool(history),
                "historical_conditions": conditions,
                "historical_repeat_count_by_condition": repeat_counts,
                "historical_artifacts_reusable": reusable,
                "historical_commit_shas": sorted(
                    {
                        str(row["commit_sha"])
                        for row in history
                        if row.get("commit_sha")
                    }
                ),
                "current_head": current_head,
                "effective_worktree_note": (
                    "Phase A router P0 fix is uncommitted and included in this audit state"
                ),
                "current_head_change_impact": "GLOBAL_BEHAVIORAL",
                "impact_reasons": [
                    "GLOBAL_EXECUTION_PATH_CHANGED_SINCE_HISTORICAL_COMMIT",
                    "TASK_CONTRACT_FINAL_POLICY_ADDED",
                    "PLANNER_AND_TOKEN_ENVELOPE_CHANGED",
                    "BUDGET_AND_VITIS_EXECUTION_PATH_CHANGED",
                    "PHASE_A_ROUTER_FIX_RESTORES_BASELINE_FACT_AUTHORITY",
                ],
                "needs_current_head_fresh_run": True,
                "fresh_run_reason_codes": [
                    "GLOBAL_BEHAVIORAL_CHANGE",
                    "FORMAL_SAME_VERSION_MATRIX_REQUIRED",
                ],
            }
        )
    records.sort(key=lambda row: str(row["task_name"]))
    changed_files = git(
        "diff", "--name-only", f"{HISTORICAL_COMMIT}..HEAD"
    ).splitlines()
    worktree_files = git("diff", "--name-only").splitlines()
    impact = {
        "schema_version": "phase-a.current-head-impact.v1",
        "historical_commit": HISTORICAL_COMMIT,
        "current_head": current_head,
        "effective_state": "CURRENT_HEAD_PLUS_PHASE_A_UNCOMMITTED_ROUTER_FIX",
        "changed_files_since_historical_commit": changed_files,
        "phase_a_worktree_files": worktree_files,
        "capabilities": [
            {
                "capability": "task_loader_and_task_contract",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": ["llm4hls_harness/llm4hls_agent/task.py"],
            },
            {
                "capability": "PhaseRouter",
                "classification": "NONE",
                "evidence": [
                    "Effective Phase A router source is byte-identical to historical commit source.",
                    "Committed HEAD had a generation override P0; Phase A removes it before acceptance.",
                ],
            },
            {
                "capability": "Planner_prompt_schema_and_token_envelope",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": [
                    "llm4hls_harness/llm4hls_agent/openai_provider.py",
                    "llm4hls_harness/llm4hls_agent/v3_openai_planner.py",
                ],
            },
            {
                "capability": "Patch_validator",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": ["llm4hls_harness/llm4hls_agent/repair.py"],
            },
            {
                "capability": "Candidate_creation_comparator_promotion_and_final",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": ["llm4hls_harness/llm4hls_agent/v3_prototype.py"],
            },
            {
                "capability": "BudgetLedger_and_reserves",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": ["llm4hls_harness/llm4hls_agent/budget.py"],
            },
            {
                "capability": "Vitis_command_and_receipt",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": ["llm4hls_harness/llm4hls_agent/vitis.py"],
            },
            {
                "capability": "validation_profile_and_final_policy",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": [
                    "task_contract/full_internal_audit behavior was added after historical commit"
                ],
            },
            {
                "capability": "Continuation",
                "classification": "GLOBAL_BEHAVIORAL",
                "evidence": [
                    "New shadow-capable continuation implementation; authority remains SHADOW"
                ],
            },
            {
                "capability": "Experience_and_Bayesian_Ranker",
                "classification": "NONE",
                "evidence": [
                    "No guided/trained authority enabled by Phase A; historical batch used experience=off"
                ],
            },
            {
                "capability": "V3D_28_public_task_files_metadata_testbenches",
                "classification": "NONE",
                "evidence": [
                    "No v3d-fast task file differs between historical commit and current HEAD"
                ],
            },
        ],
        "task_impact_counts": dict(
            sorted(
                Counter(
                    str(row["current_head_change_impact"]) for row in records
                ).items()
            )
        ),
        "formal_comparability": "NOT_SAME_VERSION",
        "conclusion": (
            "Historical artifacts are reusable for descriptive engineering "
            "coverage, but global execution-path changes require a current-version "
            "formal 28-task matrix for submission-grade same-version claims."
        ),
    }
    return records, impact


def estimate_for_mode(
    mode: str, *, full_audit: bool, requires_cosim: bool = False
) -> dict[str, object]:
    if mode == "REPAIR":
        calls = {"csim": 4, "synth": 3, "cosim": 1 if full_audit else 0}
    elif mode == "SYNTH_FIX":
        calls = {"csim": 4, "synth": 4, "cosim": 1 if full_audit else 0}
    elif mode == "STRUCTURAL_FIX":
        calls = {"csim": 4, "synth": 2, "cosim": 4}
    else:
        calls = {
            "csim": 4,
            "synth": 4,
            # A requires_cosim task runs a baseline CoSim and a fresh-final
            # CoSim.  A non-required full audit adds only the final CoSim.
            "cosim": 2 if requires_cosim else 1 if full_audit else 0,
        }
    credits = calls["csim"] + 4 * calls["synth"] + 20 * calls["cosim"]
    return {
        "planner_call_cap": 2,
        "token_cap": 8000,
        "estimated_tool_call_caps": calls,
        "estimated_credit_cap": credits,
    }


def build_paid_proposal(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(row["task_name"]): row for row in tasks}
    historical = sorted(
        task_id for task_id, row in by_id.items() if row["in_historical_12"]
    )
    missing = sorted(
        task_id for task_id, row in by_id.items() if not row["in_historical_12"]
    )
    anchors = [
        ("repair", "v3d_fast_002", "REPAIR"),
        ("synth_fix", "v3d_fast_010", "SYNTH_FIX"),
        ("structural_fix", "v3d_fast_018", "STRUCTURAL_FIX"),
        ("optimize", "v3d_fast_022", "OPTIMIZE"),
    ]
    anchor_rows: list[dict[str, Any]] = []
    for name, task_id, mode in anchors:
        if by_id[task_id]["mode"] != mode or task_id not in missing:
            raise RuntimeError(f"anchor selection does not match replayed mode: {task_id}")
        anchor_rows.append(
            {
                "anchor": name,
                "task_name": task_id,
                "mode": mode,
                "selected_from_missing_16": True,
                "must_run_separately": False,
                **estimate_for_mode(
                    mode,
                    full_audit=True,
                    requires_cosim=bool(by_id[task_id]["requires_cosim"]),
                ),
                "run_directory_template": (
                    "llm4hls_harness/runs/phase-b-current-head/"
                    "{timestamp}/anchor-" + name + "-" + task_id
                ),
            }
        )
    anchor_rows.append(
        {
            "anchor": "true_empty_stub_generation",
            "task_name": "track_a_empty_stub_generation",
            "mode": "REPAIR_FROM_REAL_BASELINE_CSIM_FAILURE",
            "selected_from_missing_16": False,
            "must_run_separately": True,
            **estimate_for_mode("REPAIR", full_audit=True),
            "run_directory_template": (
                "llm4hls_harness/runs/phase-b-current-head/"
                "{timestamp}/anchor-empty-stub-generation"
            ),
        }
    )
    gap_rows = [
        {
            "task_name": task_id,
            "mode": by_id[task_id]["mode"],
            "requires_cosim": by_id[task_id]["requires_cosim"],
            **estimate_for_mode(
                str(by_id[task_id]["mode"]),
                full_audit=False,
                requires_cosim=bool(by_id[task_id]["requires_cosim"]),
            ),
            "run_directory_template": (
                "llm4hls_harness/runs/phase-b-gap/{timestamp}/"
                + task_id
                + "--r01"
            ),
        }
        for task_id in missing
    ]
    return {
        "schema_version": "phase-a.current-head-paid-run-proposal.v1",
        "proposal_only": True,
        "executed": False,
        "common_runtime": {
            "provider": "real DeepSeek via openai-compatible adapter",
            "vitis": "real Vitis 2025.2",
            "continuation": "shadow",
            "experience": "shadow",
            "ranker": "bayesian_shadow",
            "fresh_run_directory_required": True,
            "selective_automatic_rerun": False,
        },
        "five_current_head_anchors": {
            "final_validation_policy": "full_internal_audit",
            "count": len(anchor_rows),
            "runs": anchor_rows,
        },
        "engineering_gap_coverage": {
            "final_validation_policy": "task_contract",
            "historical_12_reused_descriptively": historical,
            "historical_12_count": len(historical),
            "missing_16_count": len(missing),
            "planned_fresh_runs": len(gap_rows),
            "one_repeat_max_per_missing_task": True,
            "runs": gap_rows,
        },
        "formal_28_task_matrix": {
            "recommendation": "REQUIRED",
            "execute_in_phase_a": False,
            "task_count": 28,
            "repeat_policy": "at least one frozen-config run per task; repeats require separate approval",
            "reason_codes": [
                "GLOBAL_BEHAVIORAL_CHANGE",
                "NOT_SAME_VERSION_FORMAL_DATA",
                "TASK_CONTRACT_AND_FINAL_POLICY_CHANGED",
                "PLANNER_BUDGET_VITIS_PATH_CHANGED",
                "SUBMISSION_FORMAL_TABLE_REQUIRES_SINGLE_COMMIT_AND_CONFIG",
            ],
        },
        "separation_of_claims": {
            "historical_plus_gap": "engineering coverage only, not same-version formal matrix",
            "formal_matrix": "submission-grade same-commit/same-config evidence",
        },
        "phase_a_budget": {
            "real_llm_calls": 0,
            "real_llm_tokens": 0,
            "csim_calls": 0,
            "synth_calls": 0,
            "cosim_calls": 0,
            "tool_credits": 0,
        },
    }


def render_stability(
    pairs: list[dict[str, Any]],
    summary: dict[str, Any],
    continuation: dict[str, Any],
) -> str:
    agreement = summary["agreement"]
    lines = [
        "# Historical 12-task repeated-pair stability audit",
        "",
        "## Scope and evidence boundary",
        "",
        "- Source: the content-confirmed 2026-07-22 Token Policy A/B/C batch.",
        "- Design: 12 tasks × 3 conditions × 2 repeats = 72 intended slots.",
        "- This is repeated-pair consistency and pairwise variability only. It is not a significance, convergence, or population-reliability claim.",
        "- Relative deltas use `|r1-r2| / mean(|r1|, |r2|)`.",
        "",
        "## Summary",
        "",
        "| Metric | Value | Denominator |",
        "|---|---:|---|",
        f"| Intended/discovered/eligible slots | {summary['intended_slots']} / {summary['discovered_slots']} / {summary['valid_reusable_slots']} | 72 slots |",
        f"| Missing/duplicate slots | {summary['missing_slots']} / {summary['duplicate_slots']} | 72 slots |",
        f"| Valid comparable pairs | {summary['valid_comparable_pairs']} | 36 intended pairs |",
        f"| Final terminal-status agreement | {fmt(agreement['final']['rate'])} | {agreement['final']['agreed']} / {agreement['final']['comparable']} comparable pairs |",
        f"| Final CSim agreement | {fmt(agreement['csim']['rate'])} | {agreement['csim']['agreed']} / {agreement['csim']['comparable']} comparable pairs |",
        f"| Final Synth agreement | {fmt(agreement['synth']['rate'])} | {agreement['synth']['agreed']} / {agreement['synth']['comparable']} comparable pairs |",
        f"| Final CoSim agreement | {fmt(agreement['cosim']['rate'])} | {agreement['cosim']['agreed']} / {agreement['cosim']['comparable']} comparable pairs |",
        f"| Stop-reason agreement | {fmt(agreement['stop_reason']['rate'])} | {agreement['stop_reason']['agreed']} / {agreement['stop_reason']['comparable']} comparable pairs |",
        f"| Failure-class agreement | {fmt(agreement['failure_class']['rate'])} | {agreement['failure_class']['agreed']} / {agreement['failure_class']['comparable']} comparable pairs |",
        f"| Planner-call agreement | {fmt(summary['planner_call_agreement']['rate'])} | {summary['planner_call_agreement']['agreed']} / {summary['planner_call_agreement']['comparable']} pairs |",
        f"| Median token absolute/relative delta | {fmt(summary['median_token_absolute_delta'])} / {fmt(summary['median_token_relative_delta'])} | 36 pairs |",
        f"| Maximum token relative delta | {fmt(summary['maximum_token_relative_delta'])} | 36 pairs |",
        f"| Median Planner-call delta | {fmt(summary['median_planner_call_delta'])} | 36 pairs |",
        f"| Median Tool Credit delta | {fmt(summary['median_tool_credit_delta'])} | 36 pairs |",
        f"| Median acceleration absolute/relative delta | {fmt(summary['median_acceleration_absolute_delta'])} / {fmt(summary['median_acceleration_relative_delta'])} | only pairs with two recorded accelerations |",
        "",
        "A pair with missing final-stage evidence is excluded from that field's denominator rather than forced to agree or disagree.",
        "",
        "## Pair detail",
        "",
        "| Task | Cond. | Terminal | CSim | Synth | CoSim | Stop | Failure | ΔToken | rel ΔToken | ΔPlanner | ΔCredit | ΔAccel | rel ΔAccel |",
        "|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for pair in pairs:
        if pair["comparison_status"] != "COMPARABLE":
            lines.append(
                f"| {pair['task_name']} | {pair['condition']} | NOT_COMPARABLE | - | - | - | - | - | - | - | - | - | - | - |"
            )
            continue
        agreements = pair["agreements"]

        def mark(name: str) -> str:
            value = agreements[name]
            return (
                "NOT_COMPARABLE"
                if not value["comparable"]
                else "AGREE"
                if value["agreed"]
                else "DIFFER"
            )

        lines.append(
            "| {task} | {condition} | {final} | {csim} | {synth} | {cosim} | "
            "{stop} | {failure} | {token_abs} | {token_rel} | {planner} | "
            "{credit} | {accel_abs} | {accel_rel} |".format(
                task=pair["task_name"],
                condition=pair["condition"],
                final=mark("final"),
                csim=mark("csim"),
                synth=mark("synth"),
                cosim=mark("cosim"),
                stop=mark("stop_reason"),
                failure=mark("failure_class"),
                token_abs=fmt(pair["token_absolute_delta"]),
                token_rel=fmt(pair["token_relative_delta"]),
                planner=fmt(pair["planner_call_delta"]),
                credit=fmt(pair["tool_credit_delta"]),
                accel_abs=fmt(pair["acceleration_absolute_delta"]),
                accel_rel=fmt(pair["acceleration_relative_delta"]),
            )
        )
    lines += [
        "",
        "## Continuation comparison with old V3-F R02",
        "",
        "| Metric | Old V3-F R02 | This 72-run offline audit |",
        "|---|---:|---:|",
        "| scanned runs | 83 | 72 |",
        "| runs with follow-up | UNKNOWN | {runs} |".format(
            runs=continuation["runs_with_follow_up"]
        ),
        "| follow-up decision points | at least 11 bindable | {points} |".format(
            points=continuation["follow_up_decision_points"]
        ),
        "| completely bound samples | 11 | {bound} |".format(
            bound=continuation["completely_bound_samples"]
        ),
        "| beneficial/essential | 5 | {value} |".format(
            value=sum(
                count
                for label, count in continuation["outcome_distribution"].items()
                if label.startswith("BENEFICIAL") or label == "ESSENTIAL_STRUCTURAL"
            )
        ),
        "| harmful/waste | 6 | {value} |".format(
            value=continuation["outcome_distribution"].get("HARMFUL", 0)
            + continuation["outcome_distribution"].get("NEUTRAL", 0)
        ),
        "| excluded | 6 known old exclusions | {value} |".format(
            value=continuation["excluded_samples"]
        ),
        "| beneficial retention | 60% | NOT_EVALUATED |",
        "| waste block rate | 16.7% | NOT_EVALUATED |",
        "| admission | NOT_READY | INSUFFICIENT_EVIDENCE |",
        "",
        "The old report is preserved unchanged. More rows do not enable `enforce`; current authority remains `SHADOW`.",
        "",
    ]
    return "\n".join(lines)


def render_current_inventory(tasks: list[dict[str, Any]]) -> str:
    mode_counts = Counter(str(row["mode"]) for row in tasks)
    historical = sum(int(row["in_historical_12"]) for row in tasks)
    lines = [
        "# Current 28-task inventory",
        "",
        "Mode is replayed from development-side baseline facts through the current deterministic router. Acceptance/oracle facts are used only for offline audit and are never Planner-visible.",
        "",
        f"- Tasks: **{len(tasks)}**",
        f"- Historical 12 coverage: **{historical}**",
        f"- Missing historical coverage: **{len(tasks) - historical}**",
        "- Mode counts: "
        + ", ".join(f"`{mode}`={mode_counts[mode]}" for mode in sorted(mode_counts)),
        "- Effective audit state: current HEAD plus the uncommitted Phase A router P0 fix.",
        "",
        "| Task | Type | Generation | Mode | Mode reason | CoSim required | Hist. conditions/repeats | Reusable | Impact | Fresh |",
        "|---|---|---:|---|---|---:|---|---:|---|---:|",
    ]
    for row in tasks:
        repeats = ", ".join(
            f"{key}:{value}"
            for key, value in row["historical_repeat_count_by_condition"].items()
        ) or "-"
        lines.append(
            f"| {row['task_name']} | {row['task_type']} | {row['generation_required']} | "
            f"{row['mode']} | {row['mode_reason']} | {row['requires_cosim']} | "
            f"{repeats} | {row['historical_artifacts_reusable']} | "
            f"{row['current_head_change_impact']} | {row['needs_current_head_fresh_run']} |"
        )
    lines += [
        "",
        "Fresh-run flags express submission-grade current-version comparability. The historical 12 remain usable for descriptive engineering coverage, but not as a same-version formal matrix.",
        "",
    ]
    return "\n".join(lines)


def render_impact(impact: Mapping[str, object]) -> str:
    lines = [
        "# Current-HEAD impact analysis",
        "",
        f"- Historical run commit: `{impact['historical_commit']}`",
        f"- Current HEAD: `{impact['current_head']}`",
        f"- Effective audited state: `{impact['effective_state']}`",
        f"- Formal comparability: `{impact['formal_comparability']}`",
        "",
        "| Capability | Classification | Evidence |",
        "|---|---|---|",
    ]
    for item in impact["capabilities"]:
        assert isinstance(item, Mapping)
        lines.append(
            f"| {item['capability']} | {item['classification']} | "
            + "; ".join(str(value) for value in item["evidence"])
            + " |"
        )
    lines += [
        "",
        "## Per-task classification",
        "",
        "- `DIRECT`: 0",
        "- `GLOBAL_BEHAVIORAL`: 28",
        "- `NON_BEHAVIORAL`: 0",
        "- `NONE`: 0",
        "- `UNKNOWN`: 0",
        "",
        str(impact["conclusion"]),
        "",
        "The router fix is not treated as a direct change to all 28 tasks: it restores the historical baseline-fact routing semantics. The global classification comes from independent Planner, task-contract/final, Budget and Vitis execution-path changes.",
        "",
    ]
    return "\n".join(lines)


def render_proposal(proposal: Mapping[str, object]) -> str:
    anchors = proposal["five_current_head_anchors"]
    gap = proposal["engineering_gap_coverage"]
    assert isinstance(anchors, Mapping) and isinstance(gap, Mapping)
    lines = [
        "# Current-HEAD paid-run proposal",
        "",
        "> Proposal only. Phase A executed none of these LLM or Vitis runs.",
        "",
        "## Common frozen controls",
        "",
        "- Real DeepSeek through the OpenAI-compatible adapter; real Vitis 2025.2.",
        "- `continuation=shadow`, `experience=shadow`, `ranker=bayesian_shadow`.",
        "- Fresh run directory per task; retain all failure evidence; no selective automatic rerun.",
        "",
        "## Five current-HEAD anchors",
        "",
        "- Final policy: `full_internal_audit`.",
        "",
        "| Anchor | Task | Mode | Missing-16 reuse | Separate | Planner cap | Token cap | CSim/Synth/CoSim cap | Credit cap |",
        "|---|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for row in anchors["runs"]:
        calls = row["estimated_tool_call_caps"]
        lines.append(
            f"| {row['anchor']} | {row['task_name']} | {row['mode']} | "
            f"{row['selected_from_missing_16']} | {row['must_run_separately']} | "
            f"{row['planner_call_cap']} | {row['token_cap']} | "
            f"{calls['csim']}/{calls['synth']}/{calls['cosim']} | "
            f"{row['estimated_credit_cap']} |"
        )
    lines += [
        "",
        "The first four anchors are selected from the uncovered 16 tasks and can also count toward engineering gap coverage. The true empty-stub fixture is outside the V3-D 28 and must run separately.",
        "",
        "## 28-task engineering gap coverage",
        "",
        f"- Historical tasks reused descriptively: **{gap['historical_12_count']}**.",
        f"- Missing tasks planned once each: **{gap['planned_fresh_runs']}**.",
        "- Final policy: `task_contract`.",
        "",
        "| Task | Mode | CoSim required | Planner cap | Token cap | CSim/Synth/CoSim cap | Credit cap |",
        "|---|---|---:|---:|---:|---|---:|",
    ]
    for row in gap["runs"]:
        calls = row["estimated_tool_call_caps"]
        lines.append(
            f"| {row['task_name']} | {row['mode']} | {row['requires_cosim']} | "
            f"{row['planner_call_cap']} | {row['token_cap']} | "
            f"{calls['csim']}/{calls['synth']}/{calls['cosim']} | "
            f"{row['estimated_credit_cap']} |"
        )
    matrix = proposal["formal_28_task_matrix"]
    assert isinstance(matrix, Mapping)
    lines += [
        "",
        "## Formal 28-task matrix recommendation",
        "",
        f"Recommendation: **{matrix['recommendation']}**.",
        "",
        "Reason: historical artifacts are valid for descriptive reuse, but the current task-contract/final policy, Planner/token envelope, Budget and Vitis execution paths are globally behavior-changing. Submission-grade formal tables therefore need a single current commit and frozen configuration. This recommendation is not executed in Phase A.",
        "",
    ]
    return "\n".join(lines)


def render_status(
    *,
    inventory: list[dict[str, Any]],
    integrity: Mapping[str, object],
    stability: Mapping[str, object],
    continuation: Mapping[str, object],
    experience_count: int,
    ranker_count: int,
    tasks: list[dict[str, Any]],
    impact: Mapping[str, object],
    proposal: Mapping[str, object],
) -> str:
    initial = (EVIDENCE / "initial-git-state.txt").read_text(encoding="utf-8").strip()
    return f"""# Phase A baseline recovery and offline audit status

## Phase goal

Restore the deterministic Router contract, audit the existing 72 real public runs offline, inventory the current 28 tasks, and prepare—but do not execute—the next paid-run plan.

## Repository baseline

- Branch: `{git("branch", "--show-current")}`
- Start HEAD: `{INITIAL_HEAD}`
- End HEAD: `{git("rev-parse", "HEAD")}`
- Start worktree: preserved verbatim in `initial-git-state.txt` and `preexisting-worktree-files.txt`.
- End worktree: pre-existing user changes preserved; Phase A product/test/docs evidence is uncommitted.

<details><summary>Initial snapshot</summary>

```text
{initial}
```

</details>

## Gate results

- Router P0 existed: **yes**—generation metadata overrode passing baseline facts for eight optimize corpus cases.
- Router P0 fixed: **yes**—generation fields no longer select mode.
- Full test before: **555 run, 8 failures, 0 errors**.
- Focused Router/corpus tests: **25/25 pass**.
- Full test after: **556/556 pass, 0 failures, 0 errors**.
- `compileall`: **PASS**.
- `git diff --check`: **PASS**.
- Offline parser wall time: recorded in `offline-audit-generator.log`.

## Offline audit

- Historical run target/discovered: **72/72**.
- Stability reuse admitted: **{integrity["stability_eligible_runs"]}/72**.
- Artifact manifests: **{integrity["artifact_manifests_valid"]}/72 valid**; **{integrity["artifact_files_verified"]} files verified**.
- Ledger reconciliation: **{integrity["ledger_reconciled_runs"]}/72**.
- Repeated pairs: **{stability["valid_comparable_pairs"]}/36 comparable**.
- Terminal agreement: **{fmt(stability["agreement"]["final"]["rate"])}** over **{stability["agreement"]["final"]["comparable"]}** comparable pairs.
- Continuation funnel: **{continuation["runs_with_follow_up"]} runs / {continuation["follow_up_decision_points"]} points / {continuation["completely_bound_samples"]} bound / {continuation["excluded_samples"]} excluded**.
- Continuation admission/authority: **{continuation["current_admission"]} / {continuation["current_authority"]}**.
- Experience candidate records: **{experience_count}**, offline shadow, not training-ready.
- Strategy Ranker candidate records: **{ranker_count}**, Bayesian shadow, no learned training.
- Current 28-task inventory: **{len(tasks)}/28**.
- Current-HEAD task impact counts: `{json.dumps(impact["task_impact_counts"], ensure_ascii=False, sort_keys=True)}`.
- Paid-run proposal generated: **yes**; executed: **{proposal["executed"]}**.
- Formal matrix recommendation: **{proposal["formal_28_task_matrix"]["recommendation"]}**.

## Safety and budget

- Public train/dev artifacts only.
- No hidden/reference/golden content was opened or passed to a Planner.
- No real LLM, CSim, Synth, CoSim, enforce, guided mode, or training was invoked.
- Phase A budget: **0 LLM calls, 0 tokens, 0 CSim, 0 Synth, 0 CoSim, 0 Tool Credits**.
- Old V3-F report remains tracked and unmodified.

## Incomplete work

- Five fresh current-HEAD anchors: TODO.
- Missing-16 gap coverage: TODO.
- Same-version formal 28-task matrix: TODO/decision scheduling.
- Multi-model matrix, staging/reproduction, paper and video: TODO.

## Phase conclusion

**DONE**
"""


def render_acceptance(
    *,
    discovery: Mapping[str, object],
    integrity: Mapping[str, object],
    stability: Mapping[str, object],
    continuation: Mapping[str, object],
    experience_count: int,
    ranker_count: int,
    taxonomy: Mapping[str, object],
    tasks: list[dict[str, Any]],
    impact: Mapping[str, object],
    proposal: Mapping[str, object],
) -> str:
    return f"""# Phase A baseline/offline-audit acceptance

## Acceptance Scope

Router P0 recovery plus an offline audit of the already-existing public 72-run Token Policy A/B/C batch, current 28-task inventory, current-state impact analysis and a non-executed paid-run proposal.

## Repository Baseline

- Branch: `{git("branch", "--show-current")}`
- Start/End HEAD: `{INITIAL_HEAD}` / `{git("rev-parse", "HEAD")}`

## Pre-existing Worktree State

Recorded before Phase A in `preexisting-worktree-files.txt`; all five pre-existing modified/untracked documentation paths were preserved.

## Unit Test Baseline

555 tests; 8 failures; 0 errors. All failures were the same generation-metadata Router override affecting `v3d_fast_021` through `v3d_fast_028`.

## Router Contract Analysis

Baseline facts are authoritative: CSim fail → REPAIR; CSim pass/Synth fail → SYNTH_FIX; required or observed CoSim fail → STRUCTURAL_FIX; all required gates pass → OPTIMIZE. Generation capability metadata may enlarge patch/context permissions but may not select the mode.

## Router Fix or No-op Verification

The generation override was removed without adding a node, mode, task-ID exception or policy change. Regression tests cover passing-baseline generation metadata and failing-CSim generation metadata.

## Full Test Result

Focused 25/25 and full 556/556 pass; 0 FAIL; 0 ERROR. `compileall` and `git diff --check` pass.
Offline parser wall time is recorded in `offline-audit-generator.log`.

## Historical 72-run Discovery

`{discovery["status"]}`: `{discovery["selection"]["path"]}` is the only candidate with content-confirmed 12 tasks × 3 conditions × 2 repeats and 72 matching schedule/result/run records. The one-run preflight and 48-run hybrid batch were rejected as non-matches.

## Artifact Integrity

{integrity["artifact_manifests_valid"]}/72 run manifests valid; {integrity["artifact_files_verified"]} declared files hash/size verified; {integrity["ledger_reconciled_runs"]}/72 ledgers reconcile. Thirteen result rows are explicitly reconciled from durable artifacts rather than rewritten as ordinary terminal runs.

## Reuse Admission

- Stability: {integrity["stability_eligible_runs"]}/72 eligible.
- Continuation: {integrity["continuation_eligible_runs"]}/72 runs contain a bindable follow-up point.
- Experience/Ranker: {integrity["experience_eligible_runs"]}/72 source runs are public real-artifact candidates; every exported record remains `training_ready=false`.

## Repeated-pair Consistency

{stability["valid_comparable_pairs"]}/36 pairs are valid. Terminal agreement is {fmt(stability["agreement"]["final"]["rate"])} ({stability["agreement"]["final"]["agreed"]}/{stability["agreement"]["final"]["comparable"]}). This is pairwise consistency only, with no statistical-significance claim.

## Continuation Binding Funnel

{continuation["runs_with_follow_up"]} runs contain {continuation["follow_up_decision_points"]} follow-up points; {continuation["completely_bound_samples"]} are completely bound and {continuation["excluded_samples"]} are excluded. Current admission is `{continuation["current_admission"]}` and authority remains `{continuation["current_authority"]}`.

## Leakage Audit

Every replay record physically separates `pre_state`, `policy_decision`, `outcome_label`, and `leakage_audit`. The declared evaluator boundary is `pre_state` only. Future Planner response, Patch, Candidate, promotion, final and outcome values are not policy inputs. No violations were found.

## Old V3-F R02 Comparison

The old fixed figures remain: 83 scanned, 11 bound, 5 beneficial/essential, 6 harmful/waste, 6 known exclusions, 60% beneficial retention, 16.7% waste block, `NOT_READY`. The new audit does not overwrite the report or enable enforce.

## Experience Candidate Records

{experience_count} bindable public real-run Candidate records were exported with observed strategies derived from diffs. They are offline shadow candidates, not training-ready.

## Strategy Ranker Candidate Pool

{ranker_count} records were exported for Bayesian shadow analysis. No learned router/ranker was trained or admitted.

## Historical Failure Taxonomy

Terminal distribution: `{json.dumps(taxonomy["terminal_status"], ensure_ascii=False, sort_keys=True)}`. Reconciled post-validation rows remain visibly classified rather than relabeled.

## Current 28-task Inventory

{len(tasks)}/28 tasks are inventoried from corpus specs, metadata and deterministic Router replay. Historical coverage is {sum(int(row["in_historical_12"]) for row in tasks)}/28.

## Current-HEAD Change Impact

Task impact: `{json.dumps(impact["task_impact_counts"], ensure_ascii=False, sort_keys=True)}`. The effective Router restores historical semantics, but other global execution paths changed, so historical data is not a same-version formal matrix.

## Paid-run Proposal

Five full-audit anchors plus {proposal["engineering_gap_coverage"]["planned_fresh_runs"]} missing-task engineering gap runs are proposed and not executed.

## Formal Matrix Recommendation

`{proposal["formal_28_task_matrix"]["recommendation"]}` for submission-grade same-commit/same-config claims.

## Safety and Non-leakage

No real LLM or Vitis call; no continuation enforce; no experience guided mode; no model training; no hidden/reference/golden content access; old V3-F report unchanged.

## Budget Usage

Real LLM calls/tokens: 0/0. CSim/Synth/CoSim: 0/0/0. Tool Credits: 0.

## Evidence Index

Raw evidence is under `docs/experiments/artifacts/2026-07-23-phase-a-offline-audit/`; analysis documents are linked from the status file and completion board.

## Acceptance Decision

**ACCEPTED**
"""


def render_board() -> str:
    return """# Track A completion board — 2026-07-23

| Item | Status | Evidence / next gate |
|---|---|---|
| P0 Router | DONE | Baseline-fact authority restored; 556/556 fast tests pass |
| Historical 72-run audit | DONE | 72/72 discovered; manifests, ledger and reuse audited offline |
| Current 28-task inventory | DONE | 28/28 corpus tasks inventoried |
| Current-HEAD impact analysis | DONE | Global behavioral change documented |
| Five real anchors | TODO | Proposal exists; no Phase A paid run executed |
| 16-task gap coverage | TODO | One-run-per-missing-task proposal exists |
| Formal 28-task matrix | DECISION_PENDING | Audit recommendation is REQUIRED; schedule/budget approval pending |
| Model matrix | TODO | No multi-model run in Phase A |
| Lightweight tools | SHADOW / NOT_READY | Continuation SHADOW; Experience SHADOW; Bayesian Ranker SHADOW; learned training NOT_READY |
| Reproduction/staging | TODO | Clean-environment reproduction and staging package pending |
| Paper/video | TODO | Formal same-version evidence and presentation assets pending |

Planning is not counted as real-run completion.
"""


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if git("rev-parse", "HEAD") != INITIAL_HEAD:
        raise RuntimeError("unexpected HEAD; Phase A snapshot no longer matches")
    if git("diff", "--", str(OLD_REPORT.relative_to(REPO))):
        raise RuntimeError("old V3-F report has been modified")

    inventory, reuse_rows, integrity = build_historical_inventory()
    discovery = build_discovery_summary(inventory)
    pairs, stability = build_pairs(inventory)
    continuation_rows, continuation_exclusions, continuation = build_continuation(
        inventory
    )
    experience, ranker = build_candidate_pools(inventory)
    taxonomy = build_failure_taxonomy(inventory, experience)
    tasks, impact = current_task_records(inventory, reuse_rows)
    proposal = build_paid_proposal(tasks)

    write_json(EVIDENCE / "historical-72run-discovery-summary.json", discovery)
    write_jsonl(EVIDENCE / "historical-72run-inventory.jsonl", inventory)
    write_jsonl(EVIDENCE / "historical-72run-reuse-eligibility.jsonl", reuse_rows)
    write_json(EVIDENCE / "historical-72run-integrity-summary.json", integrity)
    write_jsonl(
        EVIDENCE / "continuation_replay_candidate_dataset.jsonl",
        continuation_rows,
    )
    write_jsonl(
        EVIDENCE / "continuation_replay_exclusion_log.jsonl",
        continuation_exclusions,
    )
    write_jsonl(EVIDENCE / "experience_candidate_records.jsonl", experience)
    write_jsonl(EVIDENCE / "strategy_ranker_candidate_pool.jsonl", ranker)
    write_json(EVIDENCE / "historical_failure_taxonomy.json", taxonomy)
    write_json(EVIDENCE / "current-28task-inventory.json", tasks)
    write_json(EVIDENCE / "current-head-impact-analysis.json", impact)
    write_json(EVIDENCE / "current-head-paid-run-proposal.json", proposal)
    write_json(
        EVIDENCE / "acceptance-metadata.json",
        {
            "schema_version": "phase-a.acceptance-metadata.v1",
            "phase": "Phase A",
            "status": "DONE",
            "acceptance_decision": "ACCEPTED",
            "branch": git("branch", "--show-current"),
            "start_head": INITIAL_HEAD,
            "end_head": git("rev-parse", "HEAD"),
            "tests_before": {"run": 555, "failures": 8, "errors": 0},
            "focused_tests": {"run": 25, "failures": 0, "errors": 0},
            "tests_after": {"run": 556, "failures": 0, "errors": 0},
            "compileall": "PASS",
            "git_diff_check": "PASS",
            "historical_discovery": discovery["status"],
            "historical_runs": len(inventory),
            "stability_eligible_runs": integrity["stability_eligible_runs"],
            "continuation": continuation,
            "experience_candidate_records": len(experience),
            "strategy_ranker_candidate_records": len(ranker),
            "current_tasks": len(tasks),
            "task_impact_counts": impact["task_impact_counts"],
            "formal_matrix_recommendation": proposal["formal_28_task_matrix"][
                "recommendation"
            ],
            "old_v3f_report_sha256": sha256_file(OLD_REPORT),
            "real_llm_calls": 0,
            "real_llm_tokens": 0,
            "csim_calls": 0,
            "synth_calls": 0,
            "cosim_calls": 0,
            "tool_credits": 0,
            "offline_parser_wall_time_seconds": (
                "RECORDED_IN_offline-audit-generator.log"
            ),
        },
    )
    (EVIDENCE / "git-diff-check.txt").write_text(
        (EVIDENCE / "diff-check-after.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (EXPERIMENTS / "historical_12task_stability_report.md").write_text(
        render_stability(pairs, stability, continuation), encoding="utf-8"
    )
    (EXPERIMENTS / "current-28task-inventory.md").write_text(
        render_current_inventory(tasks), encoding="utf-8"
    )
    (EXPERIMENTS / "current-head-impact-analysis.md").write_text(
        render_impact(impact), encoding="utf-8"
    )
    (EXPERIMENTS / "current-head-paid-run-proposal.md").write_text(
        render_proposal(proposal), encoding="utf-8"
    )
    (
        STATUS / "2026-07-23-phase-a-baseline-offline-audit.md"
    ).write_text(
        render_status(
            inventory=inventory,
            integrity=integrity,
            stability=stability,
            continuation=continuation,
            experience_count=len(experience),
            ranker_count=len(ranker),
            tasks=tasks,
            impact=impact,
            proposal=proposal,
        ),
        encoding="utf-8",
    )
    (
        EXPERIMENTS
        / "2026-07-23-phase-a-baseline-offline-audit-acceptance.md"
    ).write_text(
        render_acceptance(
            discovery=discovery,
            integrity=integrity,
            stability=stability,
            continuation=continuation,
            experience_count=len(experience),
            ranker_count=len(ranker),
            taxonomy=taxonomy,
            tasks=tasks,
            impact=impact,
            proposal=proposal,
        ),
        encoding="utf-8",
    )
    (STATUS / "2026-07-23-track-a-completion-board.md").write_text(
        render_board(), encoding="utf-8"
    )
    write_json(
        EVIDENCE / "offline-audit-summary.json",
        {
            "historical": integrity,
            "stability": stability,
            "continuation": continuation,
            "experience_candidate_records": len(experience),
            "strategy_ranker_candidate_records": len(ranker),
            "current_28_task_count": len(tasks),
            "impact_counts": impact["task_impact_counts"],
            "proposal": {
                "anchors": proposal["five_current_head_anchors"]["count"],
                "gap_runs": proposal["engineering_gap_coverage"][
                    "planned_fresh_runs"
                ],
                "formal_matrix": proposal["formal_28_task_matrix"][
                    "recommendation"
                ],
            },
        },
    )

    print(
        json.dumps(
            {
                "runs": len(inventory),
                "artifact_files_verified": integrity["artifact_files_verified"],
                "stability_pairs": stability["valid_comparable_pairs"],
                "continuation_bound": len(continuation_rows),
                "continuation_excluded": len(continuation_exclusions),
                "experience_records": len(experience),
                "ranker_records": len(ranker),
                "tasks": len(tasks),
                "formal_matrix": proposal["formal_28_task_matrix"]["recommendation"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
