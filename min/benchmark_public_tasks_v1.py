#!/usr/bin/env python3
"""Fresh, serial Phase V1 preflight and public 3x10 benchmark driver."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

import benchmark_public_tasks as baseline
import minimal_flow
from llm4hls_agent.budget import BudgetConfig, BudgetLedger


MIN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MIN_DIR.parent
TASK_ROOT = baseline.TASK_ROOT
FLOW_SCRIPT = MIN_DIR / "minimal_flow.py"
ACCEPTANCE_ROOT = MIN_DIR / "benchmarks" / "public_3x10V1"
TASK_ORDER = tuple(baseline.TASK_TYPES)
SCHEMA_VERSION = "track-a.public-3x10-v1-benchmark.v1"
STABLE_BUDGET_KEYS = (
    "credit_limit",
    "credits_used",
    "pending_credits_reserved",
    "credits_remaining",
    "tool_costs",
    "tool_limits",
    "tool_used",
    "tool_pending",
    "run_token_limit",
    "token_limit",
    "pending_tokens_reserved",
    "tokens_used",
    "input_tokens_used",
    "output_tokens_used",
    "cached_input_tokens_used",
    "token_usage_complete",
    "cached_input_usage_complete",
    "usage_known_count",
    "usage_unknown_count",
    "usage_pending_count",
    "recorded_token_lower_bound",
    "known_tokens_total",
    "tokens_remaining",
    "config_hash",
    "llm_requests_total",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _task_hashes() -> dict[str, dict[str, str]]:
    return {
        task_id: {
            path.name: _sha256(path)
            for path in sorted((TASK_ROOT / task_id).iterdir())
            if path.is_file()
        }
        for task_id in TASK_ORDER
    }


def _schedule(phase: str) -> list[tuple[str, int]]:
    repeats = 10 if phase == "formal" else 3
    schedule: list[tuple[str, int]] = []
    for repetition in range(1, repeats + 1):
        for task_id in TASK_ORDER:
            if phase == "preflight" and task_id == "residual_stream_deadlock":
                if repetition > 1:
                    continue
            schedule.append((task_id, repetition))
    return schedule


def _budget_config(run_dir: Path) -> BudgetConfig | None:
    config = _load_json(run_dir / "minimal_config.json")
    budget = config.get("budget") if config is not None else None
    if not isinstance(budget, Mapping):
        return None
    try:
        return BudgetConfig(
            credit_limit=budget.get("credit_limit"),
            costs=budget["costs"],
            tool_limits=budget["tool_limits"],
            token_limit=int(budget["token_limit"]),
            runtime_limit_seconds=float(budget["runtime_limit_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _stable_budget(value: Mapping[str, Any] | None) -> dict[str, object]:
    if value is None:
        return {key: None for key in STABLE_BUDGET_KEYS}
    return {key: value.get(key) for key in STABLE_BUDGET_KEYS}


def _read_ledger_events_strict(path: Path) -> list[dict[str, object]]:
    """Read a complete ledger without repairing or otherwise mutating it."""

    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("ledger is empty or has a torn final line")
    text = data.decode("utf-8")
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            raise ValueError("ledger contains an empty line")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("ledger event is not an object")
        if value.get("sequence") != len(events):
            raise ValueError("ledger sequence is not contiguous")
        events.append(value)
    if not events or events[0].get("state") != "INITIALIZED":
        raise ValueError("ledger does not start with INITIALIZED")
    return events


def _validate_ledger_actions(
    *,
    events: list[dict[str, object]],
    config: BudgetConfig,
    run_dir: Path,
) -> tuple[dict[str, tuple[dict[str, object], dict[str, object]]], list[str]]:
    errors: list[str] = []
    if events[0].get("config_hash") != config.config_hash:
        errors.append("INITIALIZED_CONFIG_HASH_MISMATCH")
    actions: dict[str, list[dict[str, object]]] = {}
    for event in events[1:]:
        action_id = event.get("action_id")
        state = event.get("state")
        if not isinstance(action_id, str):
            errors.append("ACTION_ID_MISSING")
            continue
        current = actions.setdefault(action_id, [])
        if state == "STARTED":
            if current:
                errors.append(f"DUPLICATE_OR_LATE_STARTED:{action_id}")
            current.append(event)
            continue
        if state not in {"COMPLETED", "AMBIGUOUS"}:
            errors.append(f"UNKNOWN_ACTION_STATE:{action_id}:{state}")
            current.append(event)
            continue
        if len(current) != 1 or current[0].get("state") != "STARTED":
            errors.append(f"ORPHAN_OR_DUPLICATE_TERMINAL:{action_id}")
        current.append(event)
    paired: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    for action_id, action_events in actions.items():
        if (
            len(action_events) != 2
            or action_events[0].get("state") != "STARTED"
            or action_events[1].get("state") != "COMPLETED"
        ):
            errors.append(f"ACTION_NOT_EXACT_STARTED_COMPLETED:{action_id}")
            continue
        started, terminal = action_events
        if started.get("kind") != terminal.get("kind"):
            errors.append(f"ACTION_KIND_MISMATCH:{action_id}")
        kind = started.get("kind")
        if kind not in config.costs:
            errors.append(f"UNKNOWN_TOOL_KIND:{action_id}:{kind}")
        elif (
            started.get("estimated_cost") != config.costs[kind]
            or terminal.get("actual_cost") != config.costs[kind]
        ):
            errors.append(f"ACTION_COST_MISMATCH:{action_id}")
        if terminal.get("token_reservation_overrun") is True:
            errors.append(f"TOKEN_RESERVATION_OVERRUN:{action_id}")
        result_ref = terminal.get("result_ref")
        result_hash = terminal.get("result_sha256")
        if not isinstance(result_ref, str) or not isinstance(result_hash, str):
            errors.append(f"RESULT_BINDING_MISSING:{action_id}")
        else:
            result_path = run_dir / result_ref
            if not result_path.is_file() or _sha256(result_path) != result_hash:
                errors.append(f"RESULT_BINDING_INVALID:{action_id}")
        paired[action_id] = (started, terminal)
    return paired, errors


def _audit_budget(
    run_dir: Path,
    result: Mapping[str, Any],
) -> dict[str, object]:
    state = _load_json(run_dir / "budget_state.json")
    result_budget = result.get("budget")
    result_budget = result_budget if isinstance(result_budget, Mapping) else None
    config = _budget_config(run_dir)
    ledger_path = run_dir / "budget_ledger.jsonl"
    replayed: Mapping[str, Any] | None = None
    replay_error: str | None = None
    events: list[dict[str, object]] = []
    action_errors: list[str] = []
    paired: dict[
        str,
        tuple[dict[str, object], dict[str, object]],
    ] = {}
    if ledger_path.is_file() and config is not None:
        try:
            events = _read_ledger_events_strict(ledger_path)
            paired, action_errors = _validate_ledger_actions(
                events=events,
                config=config,
                run_dir=run_dir,
            )
            reducer = object.__new__(BudgetLedger)
            reducer.config = config
            internal = BudgetLedger._snapshot_from(reducer, events)
            replayed = minimal_flow._canonical_budget_view(internal)
        except Exception as exc:
            replay_error = f"{type(exc).__name__}: {exc}"
    llm_started = [
        event
        for event in events
        if event.get("kind") == "llm" and event.get("state") == "STARTED"
    ]
    llm_terminal = [
        event
        for event in events
        if event.get("kind") == "llm"
        and event.get("state") in {"COMPLETED", "AMBIGUOUS"}
    ]
    response_binding_ok = False
    receipt_usage_matches_ledger = False
    if len(llm_terminal) == 1:
        terminal = llm_terminal[0]
        result_ref = terminal.get("result_ref")
        result_hash = terminal.get("result_sha256")
        if isinstance(result_ref, str) and isinstance(result_hash, str):
            response_path = run_dir / result_ref
            response_binding_ok = bool(
                response_path.is_file() and _sha256(response_path) == result_hash
            )
        receipt = _load_json(run_dir / "planner" / "response_receipt.json")
        usage = receipt.get("usage") if receipt is not None else None
        if isinstance(usage, Mapping):
            if usage.get("usage_complete") is True:
                receipt_usage_matches_ledger = all(
                    terminal.get(event_key) == usage.get(receipt_key)
                    for event_key, receipt_key in (
                        ("tokens_used", "tokens_used"),
                        ("input_tokens", "input_tokens"),
                        ("output_tokens", "output_tokens"),
                        ("cached_input_tokens", "cached_input_tokens"),
                    )
                )
            else:
                receipt_usage_matches_ledger = all(
                    terminal.get(key) is None
                    for key in (
                        "tokens_used",
                        "input_tokens",
                        "output_tokens",
                        "cached_input_tokens",
                    )
                )
    stable_state = _stable_budget(state)
    stable_result = _stable_budget(result_budget)
    stable_replayed = _stable_budget(replayed)
    no_pending = bool(
        replayed is not None
        and replayed.get("pending_credits_reserved") == 0
        and replayed.get("pending_tokens_reserved") == 0
        and isinstance(replayed.get("tool_pending"), Mapping)
        and all(value == 0 for value in replayed["tool_pending"].values())
    )
    nonnegative_remaining = bool(
        replayed is not None
        and (
            replayed.get("credits_remaining") is None
            or (
                isinstance(replayed.get("credits_remaining"), int)
                and replayed["credits_remaining"] >= 0
            )
        )
        and (
            replayed.get("tokens_remaining") is None
            or (
                isinstance(replayed.get("tokens_remaining"), int)
                and replayed["tokens_remaining"] >= 0
            )
        )
    )
    return {
        "state_equals_result_exactly": state == result_budget,
        "ledger_equals_result_stable": stable_replayed == stable_result,
        "ledger_equals_state_stable": stable_replayed == stable_state,
        "mismatch_keys_ledger_result": [
            key
            for key in STABLE_BUDGET_KEYS
            if stable_replayed[key] != stable_result[key]
        ],
        "mismatch_keys_ledger_state": [
            key
            for key in STABLE_BUDGET_KEYS
            if stable_replayed[key] != stable_state[key]
        ],
        "llm_started": len(llm_started),
        "llm_terminal": len(llm_terminal),
        "response_binding_ok": response_binding_ok,
        "receipt_usage_matches_ledger": receipt_usage_matches_ledger,
        "action_state_errors": action_errors,
        "no_pending": no_pending,
        "nonnegative_remaining": nonnegative_remaining,
        "replay_error": replay_error,
        "pass": bool(
            state == result_budget
            and stable_replayed == stable_result
            and stable_replayed == stable_state
            and len(llm_started) == 1
            and len(llm_terminal) == 1
            and response_binding_ok
            and receipt_usage_matches_ledger
            and not action_errors
            and no_pending
            and nonnegative_remaining
            and replay_error is None
        ),
    }


def _audit_response(run_dir: Path) -> dict[str, object]:
    receipt_path = run_dir / "planner" / "response_receipt.json"
    receipt = _load_json(receipt_path)
    parse = _load_json(run_dir / "planner" / "parse_result.json")
    usage = receipt.get("usage") if receipt is not None else None
    usage = usage if isinstance(usage, Mapping) else {}
    parse_error = parse.get("error_type") if parse is not None else None
    return {
        "receipt_present": receipt is not None,
        "receipt_sha256": _sha256(receipt_path) if receipt_path.is_file() else None,
        "response_received": receipt.get("response_received") if receipt else None,
        "http_status": receipt.get("http_status") if receipt else None,
        "request_success": bool(
            receipt is not None
            and receipt.get("response_received") is True
            and isinstance(receipt.get("http_status"), int)
            and 200 <= receipt["http_status"] < 300
        ),
        "envelope_present": (
            (run_dir / "planner" / "response_envelope.json").is_file()
        ),
        "raw_content_present": (
            (run_dir / "planner" / "raw_content.txt").is_file()
        ),
        "request_id_recorded": bool(receipt is not None and "request_id" in receipt),
        "finish_reason_recorded": bool(
            receipt is not None and "finish_reason" in receipt
        ),
        "usage_complete": usage.get("usage_complete"),
        "tokens_used": usage.get("tokens_used"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "parse_ok": parse.get("ok") if parse else None,
        "parse_error_type": parse_error,
        "parse_error_classified": parse_error
        in {
            "EMPTY_RESPONSE",
            "TRUNCATED_JSON",
            "INVALID_JSON",
            "INVALID_SCHEMA",
        },
    }


def _audit_patch(run_dir: Path, result: Mapping[str, Any]) -> dict[str, object]:
    receipt = _load_json(run_dir / "patch" / "patch_resolution.json")
    planner_ok = _load_json(run_dir / "planner" / "result.json")
    planner_produced_patch = bool(
        planner_ok is not None and planner_ok.get("ok") is True
    )
    if receipt is None:
        return {
            "expected": planner_produced_patch,
            "receipt_present": False,
            "selected_stage": None,
            "strict_apply_successes": 0,
            "normalized_apply_successes": 0,
            "exact_context_recovery_successes": 0,
            "unique_delete_block_recovery_successes": 0,
            "zero_match_count": 0,
            "ambiguous_match_count": 0,
            "fuzzy_recovery_count": 0,
            "recovery_exact_unique": False,
            "pass": not planner_produced_patch,
        }
    attempts = receipt.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    selected = receipt.get("selected_stage")
    success_by_stage = {
        stage: sum(
            isinstance(attempt, Mapping)
            and attempt.get("stage") == stage
            and attempt.get("status") == "APPLIED"
            for attempt in attempts
        )
        for stage in (
            "RAW_STRICT",
            "NORMALIZED_STRICT",
            "EXACT_CONTEXT_RECOVERY",
            "UNIQUE_DELETE_BLOCK_RECOVERY",
        )
    }
    error_codes = [
        str(attempt.get("error_code"))
        for attempt in attempts
        if isinstance(attempt, Mapping) and attempt.get("error_code")
    ]
    recovery_attempt = next(
        (
            attempt
            for attempt in attempts
            if isinstance(attempt, Mapping)
            and attempt.get("stage") == selected
            and selected
            in {
                "EXACT_CONTEXT_RECOVERY",
                "UNIQUE_DELETE_BLOCK_RECOVERY",
            }
        ),
        None,
    )
    match_counts = (
        recovery_attempt.get("match_counts")
        if isinstance(recovery_attempt, Mapping)
        else None
    )
    recovery_exact_unique = bool(
        selected not in {
            "EXACT_CONTEXT_RECOVERY",
            "UNIQUE_DELETE_BLOCK_RECOVERY",
        }
        or (
            isinstance(match_counts, list)
            and match_counts
            and all(count == 1 for count in match_counts)
        )
    )
    fuzzy_count = int(receipt.get("fuzzy_recovery_count", -1))
    raw_ref = receipt.get("raw_patch_ref")
    normalized_ref = receipt.get("normalized_patch_ref")
    raw_hash = receipt.get("raw_patch_sha256")
    normalized_hash = receipt.get("normalized_patch_sha256")
    raw_ok = bool(
        isinstance(raw_ref, str)
        and (run_dir / "patch" / raw_ref).is_file()
        and _sha256(run_dir / "patch" / raw_ref) == raw_hash
    )
    normalized_ok = bool(
        isinstance(normalized_ref, str)
        and (run_dir / "patch" / normalized_ref).is_file()
        and _sha256(run_dir / "patch" / normalized_ref) == normalized_hash
    )
    return {
        "expected": planner_produced_patch,
        "receipt_present": True,
        "selected_stage": selected,
        "strict_apply_successes": success_by_stage["RAW_STRICT"],
        "normalized_apply_successes": success_by_stage["NORMALIZED_STRICT"],
        "exact_context_recovery_successes": success_by_stage[
            "EXACT_CONTEXT_RECOVERY"
        ],
        "unique_delete_block_recovery_successes": success_by_stage[
            "UNIQUE_DELETE_BLOCK_RECOVERY"
        ],
        "zero_match_count": sum("ZERO_MATCH" in code for code in error_codes),
        "ambiguous_match_count": sum(
            "AMBIGUOUS_MATCH" in code for code in error_codes
        ),
        "fuzzy_recovery_count": fuzzy_count,
        "recovery_exact_unique": recovery_exact_unique,
        "raw_patch_hash_ok": raw_ok,
        "normalized_patch_hash_ok": normalized_ok,
        "final_patch_failed": result.get("patch_error") is not None,
        "pass": bool(
            recovery_exact_unique
            and fuzzy_count == 0
            and raw_ok
            and normalized_ok
        ),
    }


def _record(
    *,
    phase: str,
    slot_index: int,
    task_id: str,
    repetition: int,
    run_dir: Path,
    driver_exit_code: int,
) -> dict[str, object]:
    result_path = run_dir / "minimal_result.json"
    result = _load_json(result_path)
    driver = _load_json(run_dir / "driver_metadata.json")
    if result is None:
        return {
            "phase": phase,
            "slot_index": slot_index,
            "task_id": task_id,
            "task_type": baseline.TASK_TYPES[task_id],
            "repetition": repetition,
            "run_dir": str(run_dir),
            "driver_exit_code": driver_exit_code,
            "unique_terminal": driver is not None,
            "result_present": False,
            "flow_success": False,
            "objective_success": False,
            "qualified_success": False,
            "failure": "minimal_result.json missing or invalid",
        }
    selected = baseline._selected_outcome(result)
    flow_success = bool(
        result.get("status") == "DONE"
        and selected is not None
        and selected.get("functional_pass") is True
        and selected.get("synth_pass") is True
    )
    strict = baseline._strict_attempt_facts(
        task_id=task_id,
        run_dir=run_dir,
        result=result,
        driver_exit_code=driver_exit_code,
    )
    budget = result.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}
    response = _audit_response(run_dir)
    patch = _audit_patch(run_dir, result)
    budget_audit = _audit_budget(run_dir, result)
    wall = driver.get("wall_seconds") if driver is not None else None
    runtime = (
        float(wall)
        if isinstance(wall, (int, float))
        and not isinstance(wall, bool)
        and math.isfinite(float(wall))
        else None
    )
    horizontal_ok = bool(
        result.get("horizontal_decision_modules") == []
        and result.get("active_components") == list(minimal_flow.ACTIVE_COMPONENTS)
    )
    return {
        "phase": phase,
        "slot_index": slot_index,
        "task_id": task_id,
        "task_type": baseline.TASK_TYPES[task_id],
        "repetition": repetition,
        "run_dir": str(run_dir),
        "driver_exit_code": driver_exit_code,
        "unique_terminal": driver is not None,
        "result_present": True,
        "minimal_result_sha256": _sha256(result_path),
        "status": result.get("status"),
        "flow_success": flow_success,
        **strict,
        "planner_error": result.get("planner_error"),
        "patch_error": result.get("patch_error"),
        "selected_candidate_id": result.get("selected_candidate_id"),
        "selection_reason": result.get("selection_reason"),
        "final_kernel_sha256": result.get("final_kernel_sha256"),
        "llm_calls": budget.get("llm_requests_total"),
        "usage_complete": budget.get("token_usage_complete"),
        "tokens": budget.get("tokens_used"),
        "known_tokens": budget.get("known_tokens_total"),
        "usage_known_count": budget.get("usage_known_count"),
        "usage_unknown_count": budget.get("usage_unknown_count"),
        "credits": budget.get("credits_used"),
        "pending_credits": budget.get("pending_credits_reserved"),
        "pending_tokens": budget.get("pending_tokens_reserved"),
        "tool_used": budget.get("tool_used"),
        "runtime_seconds": runtime,
        "score_proxy": result.get("selected_public_score_proxy"),
        "horizontal_components_off": horizontal_ok,
        "response_audit": response,
        "patch_audit": patch,
        "budget_audit": budget_audit,
    }


def _numeric(items: list[dict[str, object]], key: str) -> list[float]:
    return [
        float(item[key])
        for item in items
        if isinstance(item.get(key), (int, float))
        and not isinstance(item.get(key), bool)
        and math.isfinite(float(item[key]))
    ]


def _group_summary(items: list[dict[str, object]]) -> dict[str, object]:
    attempts = len(items)
    flow = sum(item.get("flow_success") is True for item in items)
    objective = sum(item.get("objective_success") is True for item in items)
    qualified = sum(item.get("qualified_success") is True for item in items)
    exact_tokens = _numeric(items, "tokens")
    known_tokens = _numeric(items, "known_tokens")
    credits = _numeric(items, "credits")
    runtimes = _numeric(items, "runtime_seconds")
    scores = _numeric(items, "score_proxy")
    unknown_calls = sum(int(item.get("usage_unknown_count") or 0) for item in items)
    known_calls = sum(int(item.get("usage_known_count") or 0) for item in items)
    known_token_total = int(sum(known_tokens))
    return {
        "attempts": attempts,
        "flow_successes": flow,
        "flow_success_rate": round(flow / attempts, 4) if attempts else 0.0,
        "objective_successes": objective,
        "objective_success_rate": (
            round(objective / attempts, 4) if attempts else 0.0
        ),
        "objective_success_wilson_95": baseline._wilson_95(objective, attempts),
        "qualified_successes": qualified,
        "qualified_success_rate": (
            round(qualified / attempts, 4) if attempts else 0.0
        ),
        "average_tokens_exact": (
            round(statistics.fmean(exact_tokens), 4)
            if attempts and len(exact_tokens) == attempts and not unknown_calls
            else None
        ),
        "known_tokens_total": known_token_total,
        "average_known_tokens_per_known_call": (
            round(known_token_total / known_calls, 4) if known_calls else None
        ),
        "recorded_token_lower_bound": known_token_total,
        "average_credits": (
            round(statistics.fmean(credits), 4) if credits else None
        ),
        "total_credits": int(sum(credits)),
        "average_runtime_seconds": (
            round(statistics.fmean(runtimes), 4) if runtimes else None
        ),
        "average_score_proxy": (
            round(statistics.fmean(scores), 4) if scores else None
        ),
    }


def summarize(records: list[dict[str, object]]) -> dict[str, object]:
    llm_requests = sum(int(item.get("llm_calls") or 0) for item in records)
    usage_known = sum(int(item.get("usage_known_count") or 0) for item in records)
    usage_unknown = sum(
        int(item.get("usage_unknown_count") or 0) for item in records
    )
    known_tokens = sum(int(item.get("known_tokens") or 0) for item in records)
    parse_counts = {
        category: sum(
            isinstance(item.get("response_audit"), Mapping)
            and item["response_audit"].get("parse_error_type") == category
            for item in records
        )
        for category in (
            "EMPTY_RESPONSE",
            "TRUNCATED_JSON",
            "INVALID_JSON",
            "INVALID_SCHEMA",
        )
    }
    patch_totals = {
        key: sum(
            int(item["patch_audit"].get(key) or 0)
            for item in records
            if isinstance(item.get("patch_audit"), Mapping)
        )
        for key in (
            "strict_apply_successes",
            "normalized_apply_successes",
            "exact_context_recovery_successes",
            "unique_delete_block_recovery_successes",
            "zero_match_count",
            "ambiguous_match_count",
            "fuzzy_recovery_count",
            "final_patch_failed",
        )
    }
    tool_totals = {
        kind: sum(
            int(item["tool_used"].get(kind) or 0)
            for item in records
            if isinstance(item.get("tool_used"), Mapping)
        )
        for kind in ("llm", "csim", "synth", "cosim")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "slots": len(records),
        "tasks": {
            task_id: _group_summary(
                [item for item in records if item.get("task_id") == task_id]
            )
            for task_id in TASK_ORDER
        },
        "overall": _group_summary(records),
        "llm_reliability": {
            "requests_total": llm_requests,
            "request_successes": sum(
                isinstance(item.get("response_audit"), Mapping)
                and item["response_audit"].get("request_success") is True
                for item in records
            ),
            "response_receipts": sum(
                isinstance(item.get("response_audit"), Mapping)
                and item["response_audit"].get("receipt_present") is True
                for item in records
            ),
            "usage_known_calls": usage_known,
            "usage_unknown_calls": usage_unknown,
            "known_tokens_total": known_tokens,
            "known_call_token_mean": (
                round(known_tokens / usage_known, 4) if usage_known else None
            ),
            "recorded_token_lower_bound": known_tokens,
            **parse_counts,
        },
        "patch_reliability": patch_totals,
        "budget_consistency": {
            "ledger_vs_final_result_passes": sum(
                isinstance(item.get("budget_audit"), Mapping)
                and item["budget_audit"].get("ledger_equals_result_stable") is True
                for item in records
            ),
            "ledger_vs_budget_state_passes": sum(
                isinstance(item.get("budget_audit"), Mapping)
                and item["budget_audit"].get("ledger_equals_state_stable") is True
                for item in records
            ),
            "state_equals_result_exactly": sum(
                isinstance(item.get("budget_audit"), Mapping)
                and item["budget_audit"].get("state_equals_result_exactly") is True
                for item in records
            ),
            "pending_credit_total": sum(
                int(item.get("pending_credits") or 0) for item in records
            ),
            "pending_token_total": sum(
                int(item.get("pending_tokens") or 0) for item in records
            ),
        },
        "tool_totals": tool_totals,
        "records": records,
    }


def _preflight_gate(
    *,
    records: list[dict[str, object]],
    before_hashes: Mapping[str, object],
    after_hashes: Mapping[str, object],
) -> dict[str, object]:
    expected = _schedule("preflight")
    checks = {
        "slot_count_7": len(records) == 7,
        "unique_terminal_per_slot": all(
            item.get("unique_terminal") is True for item in records
        ),
        "fresh_no_retry_no_replacement": [
            (str(item.get("task_id")), int(item.get("repetition", 0)))
            for item in records
        ]
        == expected,
        "response_envelope_every_request": all(
            isinstance(item.get("response_audit"), Mapping)
            and item["response_audit"].get("receipt_present") is True
            and item["response_audit"].get("request_success") is True
            and item["response_audit"].get("envelope_present") is True
            and item["response_audit"].get("raw_content_present") is True
            for item in records
        ),
        "json_failures_preserve_and_classify": all(
            not isinstance(item.get("response_audit"), Mapping)
            or item["response_audit"].get("parse_ok") is True
            or item["response_audit"].get("parse_error_classified") is True
            for item in records
        ),
        "unknown_tokens_are_nullable": all(
            not isinstance(item.get("response_audit"), Mapping)
            or item["response_audit"].get("usage_complete") is True
            or (
                item["response_audit"].get("tokens_used") is None
                and item.get("tokens") is None
            )
            for item in records
        ),
        "budget_ledger_result_state_consistent": all(
            isinstance(item.get("budget_audit"), Mapping)
            and item["budget_audit"].get("pass") is True
            for item in records
        ),
        "patch_recovery_exact_unique_only": all(
            isinstance(item.get("patch_audit"), Mapping)
            and item["patch_audit"].get("pass") is True
            for item in records
        ),
        "fuzzy_recovery_zero": sum(
            int(item["patch_audit"].get("fuzzy_recovery_count") or 0)
            for item in records
            if isinstance(item.get("patch_audit"), Mapping)
        )
        == 0,
        "residual_success_no_regression": all(
            item.get("objective_success") is True
            for item in records
            if item.get("task_id") == "residual_stream_deadlock"
        ),
        "horizontal_components_off": all(
            item.get("horizontal_components_off") is True for item in records
        ),
        "task_files_unchanged": before_hashes == after_hashes,
    }
    status = "PASS" if all(checks.values()) else "PREFLIGHT_BLOCKED"
    return {
        "schema_version": "track-a.public-3x10-v1-preflight-gate.v1",
        "status": status,
        "checks": checks,
        "task_hashes_before": before_hashes,
        "task_hashes_after": after_hashes,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def _formal_gate(
    *,
    records: list[dict[str, object]],
    before_hashes: Mapping[str, object],
    after_hashes: Mapping[str, object],
) -> dict[str, object]:
    expected = _schedule("formal")
    checks = {
        "slot_count_30": len(records) == 30,
        "all_results_present": all(
            item.get("result_present") is True for item in records
        ),
        "unique_terminal_per_slot": all(
            item.get("unique_terminal") is True for item in records
        ),
        "fresh_no_retry_no_replacement": [
            (str(item.get("task_id")), int(item.get("repetition", 0)))
            for item in records
        ]
        == expected,
        "response_evidence_every_request": all(
            isinstance(item.get("response_audit"), Mapping)
            and item["response_audit"].get("receipt_present") is True
            and item["response_audit"].get("request_success") is True
            and item["response_audit"].get("envelope_present") is True
            and item["response_audit"].get("raw_content_present") is True
            for item in records
        ),
        "parse_failures_are_classified": all(
            isinstance(item.get("response_audit"), Mapping)
            and (
                item["response_audit"].get("parse_ok") is True
                or item["response_audit"].get("parse_error_classified") is True
            )
            for item in records
        ),
        "unknown_tokens_are_nullable": all(
            isinstance(item.get("response_audit"), Mapping)
            and (
                item["response_audit"].get("usage_complete") is True
                or (
                    item["response_audit"].get("tokens_used") is None
                    and item.get("tokens") is None
                )
            )
            for item in records
        ),
        "budget_ledger_result_state_consistent": all(
            isinstance(item.get("budget_audit"), Mapping)
            and item["budget_audit"].get("pass") is True
            for item in records
        ),
        "patch_chain_evidence_valid": all(
            isinstance(item.get("patch_audit"), Mapping)
            and item["patch_audit"].get("pass") is True
            for item in records
        ),
        "fuzzy_recovery_zero": sum(
            int(item["patch_audit"].get("fuzzy_recovery_count") or 0)
            for item in records
            if isinstance(item.get("patch_audit"), Mapping)
        )
        == 0,
        "horizontal_components_off": all(
            item.get("horizontal_components_off") is True for item in records
        ),
        "task_files_unchanged": before_hashes == after_hashes,
    }
    status = "PASS" if all(checks.values()) else "FORMAL_BLOCKED"
    return {
        "schema_version": "track-a.public-3x10-v1-formal-gate.v1",
        "status": status,
        "checks": checks,
        "task_hashes_before": before_hashes,
        "task_hashes_after": after_hashes,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("preflight", "formal"), required=True)
    parser.add_argument("--acceptance-root", type=Path, default=ACCEPTANCE_ROOT)
    parser.add_argument("--vitis-root", required=True)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--planner-timeout", type=float, default=180.0)
    parser.add_argument("--csim-timeout", type=float, default=180.0)
    parser.add_argument("--synth-timeout", type=float, default=900.0)
    parser.add_argument("--cosim-timeout", type=float, default=180.0)
    parser.add_argument("--token-budget", type=int, default=32768)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for variable in ("OPENAI_BASE_URL", "OPENAI_API_KEY"):
        if not os.environ.get(variable):
            raise RuntimeError(f"required environment variable is missing: {variable}")
    acceptance_root = args.acceptance_root.resolve()
    phase_root = acceptance_root / args.phase
    if phase_root.exists():
        raise RuntimeError(
            f"phase directory already exists; refusing reuse/retry: {phase_root}"
        )
    if args.phase == "formal":
        gate = _load_json(acceptance_root / "preflight" / "preflight_gate.json")
        if gate is None or gate.get("status") != "PASS":
            raise RuntimeError("formal phase requires a PASS preflight gate")
    phase_root.mkdir(parents=True)
    before_hashes = _task_hashes()
    schedule = _schedule(args.phase)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "phase": args.phase,
        "created_unix_seconds": time.time(),
        "model": args.model,
        "endpoint_host": urllib.parse.urlsplit(
            os.environ["OPENAI_BASE_URL"]
        ).hostname,
        "vitis_root": str(Path(args.vitis_root).resolve()),
        "vitis_version": "2025.2",
        "target": "U55C",
        "target_clock_ns": 5.0,
        "execution_order": "repetition-major interleaved, strictly serial",
        "schedule": [
            {"slot": index, "task_id": task_id, "repetition": repetition}
            for index, (task_id, repetition) in enumerate(schedule, start=1)
        ],
        "automatic_retry": False,
        "failed_sample_replacement": False,
        "horizontal_components": [],
        "minimal_flow_sha256": _sha256(FLOW_SCRIPT),
        "task_file_sha256": before_hashes,
        "timeouts": {
            "planner": args.planner_timeout,
            "csim": args.csim_timeout,
            "synth": args.synth_timeout,
            "cosim": args.cosim_timeout,
        },
        "token_budget": args.token_budget,
        "max_output_tokens": args.max_output_tokens,
    }
    _write_json(phase_root / f"{args.phase}_manifest.json", manifest)
    records: list[dict[str, object]] = []
    for slot_index, (task_id, repetition) in enumerate(schedule, start=1):
        run_dir = phase_root / "runs" / task_id / f"run_{repetition:02d}"
        if run_dir.exists():
            raise RuntimeError(f"fresh run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)
        command = [
            sys.executable,
            str(FLOW_SCRIPT),
            "--task-dir",
            str(TASK_ROOT / task_id),
            "--live-openai",
            "--run-dir",
            str(run_dir),
            "--backend",
            "vitis",
            "--vitis-root",
            str(args.vitis_root),
            "--model",
            args.model,
            "--token-budget",
            str(args.token_budget),
            "--max-output-tokens",
            str(args.max_output_tokens),
            "--planner-timeout",
            str(args.planner_timeout),
            "--csim-timeout",
            str(args.csim_timeout),
            "--synth-timeout",
            str(args.synth_timeout),
            "--cosim-timeout",
            str(args.cosim_timeout),
        ]
        print(
            f"[{slot_index}/{len(schedule)}] {args.phase} start "
            f"{task_id} run_{repetition:02d}",
            flush=True,
        )
        started = time.monotonic()
        started_unix = time.time()
        process = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        wall = time.monotonic() - started
        (run_dir / "driver.stdout.log").write_text(
            process.stdout,
            encoding="utf-8",
        )
        (run_dir / "driver.stderr.log").write_text(
            process.stderr,
            encoding="utf-8",
        )
        _write_json(
            run_dir / "driver_metadata.json",
            {
                "phase": args.phase,
                "slot_index": slot_index,
                "task_id": task_id,
                "repetition": repetition,
                "started_unix_seconds": started_unix,
                "finished_unix_seconds": time.time(),
                "wall_seconds": wall,
                "exit_code": process.returncode,
                "retry_count": 0,
                "replacement_sample": False,
                "command": command,
            },
        )
        record = _record(
            phase=args.phase,
            slot_index=slot_index,
            task_id=task_id,
            repetition=repetition,
            run_dir=run_dir,
            driver_exit_code=process.returncode,
        )
        records.append(record)
        _write_json(phase_root / f"{args.phase}_records.json", records)
        _write_json(phase_root / f"{args.phase}_summary.json", summarize(records))
        print(
            f"[{slot_index}/{len(schedule)}] {args.phase} end "
            f"{task_id} run_{repetition:02d} exit={process.returncode} "
            f"wall={wall:.1f}s",
            flush=True,
        )
    after_hashes = _task_hashes()
    summary = summarize(records)
    _write_json(phase_root / f"{args.phase}_summary.json", summary)
    _write_json(
        phase_root / "task_diff_check.json",
        {
            "before": before_hashes,
            "after": after_hashes,
            "unchanged": before_hashes == after_hashes,
        },
    )
    if args.phase == "preflight":
        gate = _preflight_gate(
            records=records,
            before_hashes=before_hashes,
            after_hashes=after_hashes,
        )
        _write_json(phase_root / "preflight_gate.json", gate)
        print(json.dumps(gate, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if gate["status"] == "PASS" else 4
    formal_gate = _formal_gate(
        records=records,
        before_hashes=before_hashes,
        after_hashes=after_hashes,
    )
    _write_json(phase_root / "formal_gate.json", formal_gate)
    print(json.dumps(summary["tasks"], ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if formal_gate["status"] == "PASS" else 5


if __name__ == "__main__":
    raise SystemExit(main())
