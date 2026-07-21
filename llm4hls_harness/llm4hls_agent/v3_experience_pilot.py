"""Bounded, path-free comparison of V3-E shadow and guided pilot batches.

The comparator is intentionally offline.  It does not execute a model or
Vitis, and it does not alter either input batch.  Every planned slot is kept in
the denominator: an absent result becomes an explicit ``NOT_RUN`` row instead
of disappearing from the success rate.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO


PILOT_RESULT_SCHEMA = "v3e.pilot-result.v1"
PILOT_SUMMARY_SCHEMA = "v3e.pilot-summary.v1"
DEFAULT_V3E_PILOT_TASK_IDS = (
    "v3d_fast_001",
    "v3d_fast_002",
    "v3d_fast_003",
    "v3d_fast_009",
    "v3d_fast_010",
    "v3d_fast_011",
    "v3d_fast_015",
    "v3d_fast_016",
    "v3d_fast_017",
    "v3d_fast_021",
    "v3d_fast_022",
    "v3d_fast_028",
)

_ARMS = ("shadow", "guided")
_PASS = {"PASS", "PASSED", "SUCCESS", "SUCCEEDED"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,159}")
_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+/-]{0,159}")
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer\s+|sk-[A-Za-z0-9_-]{8,})"
)


class PilotAggregationError(ValueError):
    """Raised for malformed input which cannot safely be summarized."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _safe_id(value: object, *, fallback: str = "UNKNOWN") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if _SECRET.search(text) or Path(text).is_absolute():
        return fallback
    return text if _SAFE_ID.fullmatch(text) else fallback


def _safe_model(value: object) -> str:
    text = str(value or "").strip()
    if (
        not text
        or _SECRET.search(text)
        or Path(text).is_absolute()
        or ".." in Path(text).parts
        or _SAFE_MODEL.fullmatch(text) is None
    ):
        return "UNKNOWN"
    return text


def _digest(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if _HEX64.fullmatch(text) else None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(path, _canonical_json(value) + "\n")


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotAggregationError(f"unreadable {path.name}") from exc
    if not isinstance(value, dict):
        raise PilotAggregationError(f"{path.name} must contain an object")
    return value


def _read_jsonl(path: Path, *, limit: int = 4096) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise PilotAggregationError(f"unreadable {path.name}") from exc
    if len(lines) > limit:
        raise PilotAggregationError(f"{path.name} exceeds the bounded row limit")
    records: list[dict[str, object]] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PilotAggregationError(f"invalid JSONL in {path.name}") from exc
        if not isinstance(value, dict):
            raise PilotAggregationError(f"non-object row in {path.name}")
        records.append(value)
    return records


def _safe_child(root: Path, reference: object) -> Path | None:
    if not isinstance(reference, str) or not reference:
        return None
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _strategy_bundle(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[object] = value.split("+")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return ()
    result: list[str] = []
    for raw in values:
        item = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item) and item not in result:
            result.append(item)
    return tuple(result[:3])


def _ranked_bundles(value: object) -> set[tuple[str, ...]]:
    bundles: set[tuple[str, ...]] = set()
    for raw in _sequence(value):
        item = _mapping(raw)
        bundle = _strategy_bundle(item.get("strategy_bundle"))
        if bundle:
            bundles.add(bundle)
    return bundles


def _status_from_actions(
    actions: Sequence[Mapping[str, object]], candidate_id: str, kind: str
) -> str:
    matching = [
        action
        for action in actions
        if action.get("candidate_id") == candidate_id
        and str(action.get("kind", "")).casefold() == kind
        and action.get("validation_scope") != "final"
    ]
    if not matching:
        return "NOT_RUN"
    return "PASS" if all(item.get("ok") is True for item in matching) else "FAIL"


def _load_actions(run_root: Path) -> list[dict[str, object]]:
    actions_root = run_root / "actions"
    if not actions_root.is_dir():
        return []
    actions: list[dict[str, object]] = []
    directories = sorted(
        (item for item in actions_root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
    )
    if len(directories) > 256:
        raise PilotAggregationError("run exceeds the bounded action limit")
    for directory in directories:
        path = directory / "result.json"
        if path.is_file():
            actions.append(_read_json(path))
    return actions


def _load_recommendations(
    run_root: Path, result: Mapping[str, object]
) -> dict[int, dict[str, object]]:
    experience = _mapping(result.get("experience"))
    reference = experience.get("recommendations_ref")
    path = _safe_child(run_root, reference)
    if path is None:
        path = run_root / "experience" / "experience_recommendations.jsonl"
    if not path.is_file():
        return {}
    recommendations: dict[int, dict[str, object]] = {}
    for record in _read_jsonl(path, limit=64):
        round_index = record.get("round_index")
        if isinstance(round_index, bool) or not isinstance(round_index, int):
            continue
        guidance = _mapping(record.get("guidance"))
        recommendations[round_index] = {
            "recommendation_id": _digest(record.get("recommendation_id")),
            "recommended": _ranked_bundles(
                guidance.get("recommended_strategy_bundles")
            ),
            "discouraged": _ranked_bundles(
                guidance.get("discouraged_strategy_bundles")
            ),
            "known_failures": {
                bundle
                for raw in _sequence(guidance.get("similar_failures"))
                for bundle in [_strategy_bundle(_mapping(raw).get("strategy_bundle"))]
                if bundle
            },
            "confidence": _finite(guidance.get("confidence")),
            "fallback_reason": _safe_id(guidance.get("fallback_reason")),
        }
    return recommendations


def _proposal_attempts(
    run_root: Path,
    result: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    recommendations = _load_recommendations(run_root, result)
    attempts: list[dict[str, object]] = []
    rounds = result.get("candidate_rounds")
    if isinstance(rounds, list):
        for raw in rounds[:32]:
            row = _mapping(raw)
            candidate_id = _safe_id(row.get("candidate_id"), fallback="UNKNOWN")
            if candidate_id == _safe_id(result.get("baseline_candidate_id")):
                continue
            round_index = _nonnegative_int(row.get("round"))
            bundle = _strategy_bundle(
                row.get("strategy_bundle") or row.get("change_class")
            )
            decision = _safe_id(row.get("decision"))
            decision_reason = _safe_id(row.get("decision_reason"))
            advice = recommendations.get(round_index, {})
            recommended = advice.get("recommended", set())
            known_failures = set(advice.get("discouraged", set())) | set(
                advice.get("known_failures", set())
            )
            agreement = bool(bundle and bundle in recommended)
            repeated_failure = bool(bundle and bundle in known_failures)
            rejected = "REJECT" in decision or "FAIL" in decision
            success = decision in {"PROMOTED", "FINAL_VERIFIED", "ACCEPTED"}
            attempts.append(
                {
                    "round": round_index,
                    "candidate_id": candidate_id,
                    "materialized": True,
                    "strategy_bundle": list(bundle),
                    "decision": decision,
                    "decision_reason": decision_reason,
                    "gates": {
                        "csim": _status_from_actions(actions, candidate_id, "csim"),
                        "synth": _status_from_actions(actions, candidate_id, "synth"),
                        "cosim": (
                            _status_from_actions(actions, candidate_id, "cosim")
                            if _status_from_actions(actions, candidate_id, "cosim")
                            != "NOT_RUN"
                            else str(row.get("cosim", "NOT_RUN")).upper()
                            if str(row.get("cosim", "NOT_RUN")).upper()
                            in {"PASS", "FAIL", "SKIPPED", "NOT_RUN"}
                            else "NOT_RUN"
                        ),
                    },
                    "latency_worst": _finite(row.get("latency_worst")),
                    "advice_available": bool(recommended or known_failures),
                    "recommendation_id": advice.get("recommendation_id"),
                    "recommendation_agreement": agreement,
                    "repeated_known_failure_strategy": repeated_failure,
                    "advice_helpful": agreement and success,
                    "advice_misleading": agreement and rejected,
                }
            )
    for raw in _sequence(result.get("node_events"))[:256]:
        event = _mapping(raw)
        if event.get("node") != "record_rejected_proposal":
            continue
        round_index = _nonnegative_int(event.get("round_index"))
        reference = event.get("result_ref")
        payload_path = _safe_child(run_root, reference)
        payload = _read_json(payload_path) if payload_path and payload_path.is_file() else {}
        bundle = _strategy_bundle(payload.get("change_class"))
        advice = recommendations.get(round_index, {})
        recommended = set(advice.get("recommended", set()))
        known_failures = set(advice.get("discouraged", set())) | set(
            advice.get("known_failures", set())
        )
        attempts.append(
            {
                "round": round_index,
                "candidate_id": None,
                "materialized": False,
                "strategy_bundle": list(bundle),
                "decision": "REJECTED",
                "decision_reason": _safe_id(
                    payload.get("reason") or event.get("outcome")
                ),
                "gates": {"csim": "NOT_RUN", "synth": "NOT_RUN", "cosim": "NOT_RUN"},
                "latency_worst": None,
                "advice_available": bool(recommended or known_failures),
                "recommendation_id": advice.get("recommendation_id"),
                "recommendation_agreement": bool(bundle and bundle in recommended),
                "repeated_known_failure_strategy": bool(
                    bundle and bundle in known_failures
                ),
                "advice_helpful": False,
                "advice_misleading": bool(bundle and bundle in recommended),
            }
        )
    return attempts


def _candidate_metrics(attempts: Sequence[Mapping[str, object]]) -> dict[str, int]:
    bundles = [
        tuple(str(item) for item in _sequence(attempt.get("strategy_bundle")))
        for attempt in attempts
        if _sequence(attempt.get("strategy_bundle"))
    ]
    return {
        "attempts": len(attempts),
        "materialized": sum(attempt.get("materialized") is True for attempt in attempts),
        "patch_rejections": sum(
            "PATCH" in str(attempt.get("decision_reason", ""))
            or attempt.get("materialized") is False
            for attempt in attempts
        ),
        "csim_failures": sum(
            _mapping(attempt.get("gates")).get("csim") == "FAIL"
            for attempt in attempts
        ),
        "synth_failures": sum(
            _mapping(attempt.get("gates")).get("synth") == "FAIL"
            for attempt in attempts
        ),
        "cosim_failures": sum(
            _mapping(attempt.get("gates")).get("cosim") == "FAIL"
            for attempt in attempts
        ),
        "strategy_attempts": len(bundles),
        "unique_strategy_bundles": len(set(bundles)),
        "duplicate_strategy_attempts": len(bundles) - len(set(bundles)),
        "advice_eligible": sum(
            attempt.get("advice_available") is True for attempt in attempts
        ),
        "recommendation_agreements": sum(
            attempt.get("recommendation_agreement") is True for attempt in attempts
        ),
        "repeated_known_failure_strategies": sum(
            attempt.get("repeated_known_failure_strategy") is True
            for attempt in attempts
        ),
        "advice_helpful": sum(
            attempt.get("advice_helpful") is True for attempt in attempts
        ),
        "advice_misleading": sum(
            attempt.get("advice_misleading") is True for attempt in attempts
        ),
    }


@dataclass(frozen=True)
class _ArmInput:
    name: str
    root: Path
    status: str
    summary: dict[str, object]
    rows: tuple[dict[str, object], ...]
    blockers: tuple[str, ...]


def _load_arm(name: str, root: Path) -> _ArmInput:
    if not root.is_dir():
        return _ArmInput(name, root, "NOT_RUN", {}, (), (f"{name.upper()}_BATCH_MISSING",))
    summary_path = root / "summary.json"
    results_path = root / "benchmark_results.jsonl"
    missing = [path.name for path in (summary_path, results_path) if not path.is_file()]
    if missing:
        return _ArmInput(
            name,
            root,
            "BLOCKED",
            {},
            (),
            tuple(f"{name.upper()}_{item.upper()}_MISSING" for item in missing),
        )
    try:
        summary = _read_json(summary_path)
        rows = tuple(_read_jsonl(results_path))
    except PilotAggregationError:
        return _ArmInput(name, root, "BLOCKED", {}, (), (f"{name.upper()}_BATCH_UNREADABLE",))
    return _ArmInput(name, root, "AVAILABLE", summary, rows, ())


def _arm_contract(arm: _ArmInput) -> dict[str, object]:
    configuration = _mapping(arm.summary.get("configuration"))
    selection = _mapping(arm.summary.get("selection"))
    snapshot = _mapping(configuration.get("experience_store_snapshot"))
    return {
        "experience_mode": _safe_id(configuration.get("experience_mode")),
        "validation_profile": _safe_id(configuration.get("validation_profile")),
        "backend": _safe_id(configuration.get("backend")),
        "models": tuple(
            _safe_model(item) for item in _sequence(configuration.get("models"))
        ),
        "repeats": _nonnegative_int(configuration.get("repeats")),
        "seed_sha256": _digest(snapshot.get("sha256")),
        "seed_present": snapshot.get("present") is True,
        "selected_task_ids": tuple(
            _safe_id(item) for item in _sequence(selection.get("task_ids"))
        ),
    }


def _source_result(arm: _ArmInput, row: Mapping[str, object]) -> tuple[Path | None, dict[str, object]]:
    run_id = _safe_id(row.get("run_id"), fallback="")
    if not run_id:
        return None, {}
    run_root = arm.root / "runs" / run_id
    if not run_root.is_dir():
        return None, {}
    reference = row.get("source_result_ref") or "v3_prototype_result.json"
    path = _safe_child(run_root, reference)
    if path is None or not path.is_file():
        return run_root, {}
    try:
        return run_root, _read_json(path)
    except PilotAggregationError:
        return run_root, {}


def _sanitize_run_row(
    arm: _ArmInput,
    row: Mapping[str, object],
    *,
    contract: Mapping[str, object],
    in_expected_set: bool,
    attempt_index: int,
) -> dict[str, object]:
    run_root, result = _source_result(arm, row)
    attempts = (
        _proposal_attempts(run_root, result, _load_actions(run_root))
        if run_root is not None and result
        else []
    )
    candidate_metrics = _candidate_metrics(attempts)
    final_success = row.get("fresh_final_success") is True
    tool_calls = _mapping(row.get("tool_calls"))
    return {
        "schema_version": PILOT_RESULT_SCHEMA,
        "arm": arm.name,
        "experience_mode": contract.get("experience_mode"),
        "validation_profile": contract.get("validation_profile"),
        "seed_sha256": contract.get("seed_sha256"),
        "task_id": _safe_id(row.get("task_id")),
        "in_expected_set": in_expected_set,
        "expected_mode": _safe_id(row.get("expected_mode")),
        "routed_mode": _safe_id(row.get("routed_mode")),
        "model": _safe_model(row.get("model")),
        "repeat_index": _nonnegative_int(row.get("repeat_index")),
        "attempt_index": attempt_index,
        "run_id": _safe_id(row.get("run_id")),
        "run_fingerprint": _digest(row.get("run_fingerprint")),
        "attempt_status": _safe_id(row.get("status")),
        "execution_started": row.get("execution_started") is True,
        "final_success": final_success,
        "fresh_final_success": final_success,
        "failure_stage": _safe_id(row.get("failure_stage"), fallback="NONE"),
        "stop_reason": _safe_id(row.get("stop_reason")),
        "acceleration_vs_baseline": _finite(row.get("acceleration_vs_baseline")),
        "usage": {
            "tokens": _nonnegative_int(row.get("tokens_used")),
            "input_tokens": _nonnegative_int(row.get("input_tokens_used")),
            "output_tokens": _nonnegative_int(row.get("output_tokens_used")),
            "credits": _nonnegative_int(row.get("credits_used")),
            "wall_time_seconds": _finite(row.get("wall_time_s")) or 0.0,
            "csim_calls": _nonnegative_int(tool_calls.get("csim")),
            "synth_calls": _nonnegative_int(tool_calls.get("synth")),
            "cosim_calls": _nonnegative_int(tool_calls.get("cosim")),
            "llm_calls": _nonnegative_int(tool_calls.get("llm")),
        },
        "candidate_metrics": candidate_metrics,
        "candidate_attempts": attempts[:32],
    }


def _not_run_row(
    arm: str,
    task_id: str,
    model: str,
    repeat_index: int,
    contract: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": PILOT_RESULT_SCHEMA,
        "arm": arm,
        "experience_mode": contract.get("experience_mode", arm),
        "validation_profile": contract.get("validation_profile"),
        "seed_sha256": contract.get("seed_sha256"),
        "task_id": task_id,
        "in_expected_set": True,
        "expected_mode": "UNKNOWN",
        "routed_mode": "UNKNOWN",
        "model": model,
        "repeat_index": repeat_index,
        "attempt_index": 0,
        "run_id": "NOT_RUN",
        "run_fingerprint": None,
        "attempt_status": "NOT_RUN",
        "execution_started": False,
        "final_success": False,
        "fresh_final_success": False,
        "failure_stage": "NOT_RUN",
        "stop_reason": "BATCH_OR_SLOT_NOT_RUN",
        "acceleration_vs_baseline": None,
        "usage": {
            "tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "credits": 0,
            "wall_time_seconds": 0.0,
            "csim_calls": 0,
            "synth_calls": 0,
            "cosim_calls": 0,
            "llm_calls": 0,
        },
        "candidate_metrics": _candidate_metrics([]),
        "candidate_attempts": [],
    }


def _arm_rows(
    arm: _ArmInput,
    expected_task_ids: Sequence[str],
) -> tuple[list[dict[str, object]], dict[str, object], list[str]]:
    contract = _arm_contract(arm) if arm.summary else {
        "experience_mode": arm.name,
        "validation_profile": None,
        "backend": None,
        "models": (),
        "repeats": 1,
        "seed_sha256": None,
        "seed_present": False,
        "selected_task_ids": (),
    }
    blockers = list(arm.blockers)
    expected_set = set(expected_task_ids)
    selected = tuple(contract["selected_task_ids"])
    if arm.status == "AVAILABLE" and set(selected) != expected_set:
        blockers.append(f"{arm.name.upper()}_TASK_SET_MISMATCH")
    if arm.status == "AVAILABLE" and contract["experience_mode"] != arm.name:
        blockers.append(f"{arm.name.upper()}_EXPERIENCE_MODE_MISMATCH")
    if arm.status == "AVAILABLE" and contract["backend"] != "vitis":
        blockers.append(f"{arm.name.upper()}_BACKEND_NOT_VITIS")
    if arm.status == "AVAILABLE" and contract["validation_profile"] == "UNKNOWN":
        blockers.append(f"{arm.name.upper()}_VALIDATION_PROFILE_MISSING")
    models = tuple(contract["models"])
    repeats = int(contract["repeats"] or 1)
    if arm.status == "AVAILABLE" and len(models) != 1:
        blockers.append(f"{arm.name.upper()}_MODEL_COUNT_NOT_ONE")
    if arm.status == "AVAILABLE" and repeats != 1:
        blockers.append(f"{arm.name.upper()}_REPEAT_COUNT_NOT_ONE")
    if not models:
        models = ("UNKNOWN",)
    rows: list[dict[str, object]] = []
    slot_counts: Counter[tuple[str, str, int]] = Counter()
    for raw in arm.rows:
        task_id = _safe_id(raw.get("task_id"))
        model = _safe_model(raw.get("model"))
        repeat = _nonnegative_int(raw.get("repeat_index")) or 1
        key = (task_id, model, repeat)
        slot_counts[key] += 1
        rows.append(
            _sanitize_run_row(
                arm,
                raw,
                contract=contract,
                in_expected_set=task_id in expected_set,
                attempt_index=slot_counts[key],
            )
        )
    for task_id in expected_task_ids:
        for model in models:
            for repeat in range(1, repeats + 1):
                key = (task_id, model, repeat)
                if slot_counts[key] == 0:
                    rows.append(_not_run_row(arm.name, task_id, model, repeat, contract))
                    blockers.append(f"{arm.name.upper()}_SCHEDULED_SLOT_NOT_RUN")
                elif slot_counts[key] > 1:
                    blockers.append(f"{arm.name.upper()}_SLOT_HAS_MULTIPLE_ATTEMPTS")
    return rows, contract, sorted(set(blockers))


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _arm_metrics(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    expected = [row for row in rows if row.get("in_expected_set") is True]
    candidate_totals: Counter[str] = Counter()
    usage_totals: Counter[str] = Counter()
    walls: list[float] = []
    tokens: list[float] = []
    credits: list[float] = []
    final_successes = 0
    not_run = 0
    failure_stages: Counter[str] = Counter()
    for row in expected:
        if row.get("attempt_status") == "NOT_RUN":
            not_run += 1
        if row.get("final_success") is True:
            final_successes += 1
        else:
            failure_stages[str(row.get("failure_stage") or "UNKNOWN")] += 1
        for key, value in _mapping(row.get("candidate_metrics")).items():
            candidate_totals[str(key)] += _nonnegative_int(value)
        usage = _mapping(row.get("usage"))
        for key in ("csim_calls", "synth_calls", "cosim_calls", "llm_calls"):
            usage_totals[key] += _nonnegative_int(usage.get(key))
        tokens.append(float(_nonnegative_int(usage.get("tokens"))))
        credits.append(float(_nonnegative_int(usage.get("credits"))))
        wall = _finite(usage.get("wall_time_seconds"))
        if wall is not None:
            walls.append(wall)
    denominator = len(expected)
    strategy_attempts = candidate_totals["strategy_attempts"]
    advice_eligible = candidate_totals["advice_eligible"]
    return {
        "scheduled_attempts": denominator,
        "not_run": not_run,
        "final_successes": final_successes,
        "final_success_rate": final_successes / denominator if denominator else None,
        "failures_by_stage": dict(sorted(failure_stages.items())),
        "average_tokens": _mean(tokens),
        "average_credits": _mean(credits),
        "average_wall_time_seconds": _mean(walls),
        "tool_calls": dict(sorted(usage_totals.items())),
        "candidate_gates": dict(sorted(candidate_totals.items())),
        "strategy_duplication_rate": (
            candidate_totals["duplicate_strategy_attempts"] / strategy_attempts
            if strategy_attempts
            else None
        ),
        "recommendation_agreement_rate": (
            candidate_totals["recommendation_agreements"] / advice_eligible
            if advice_eligible
            else None
        ),
    }


def _delta(guided: object, shadow: object) -> float | None:
    left, right = _finite(guided), _finite(shadow)
    return round(left - right, 6) if left is not None and right is not None else None


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    fields = (
        "arm",
        "experience_mode",
        "validation_profile",
        "seed_sha256",
        "task_id",
        "model",
        "repeat_index",
        "attempt_index",
        "run_id",
        "attempt_status",
        "final_success",
        "failure_stage",
        "stop_reason",
        "tokens",
        "credits",
        "wall_time_seconds",
        "patch_rejections",
        "csim_failures",
        "synth_failures",
        "cosim_failures",
        "duplicate_strategy_attempts",
        "recommendation_agreements",
        "repeated_known_failure_strategies",
        "advice_helpful",
        "advice_misleading",
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            usage = _mapping(row.get("usage"))
            candidates = _mapping(row.get("candidate_metrics"))
            writer.writerow(
                {
                    **{key: row.get(key) for key in fields},
                    "tokens": usage.get("tokens", 0),
                    "credits": usage.get("credits", 0),
                    "wall_time_seconds": usage.get("wall_time_seconds", 0),
                    **{
                        key: candidates.get(key, 0)
                        for key in (
                            "patch_rejections",
                            "csim_failures",
                            "synth_failures",
                            "cosim_failures",
                            "duplicate_strategy_attempts",
                            "recommendation_agreements",
                            "repeated_known_failure_strategies",
                            "advice_helpful",
                            "advice_misleading",
                        )
                    },
                }
            )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def aggregate_pilot(
    shadow_dir: str | Path,
    guided_dir: str | Path,
    output_dir: str | Path,
    *,
    expected_task_ids: Sequence[str] = DEFAULT_V3E_PILOT_TASK_IDS,
) -> dict[str, object]:
    """Compare two immutable batch outputs and write bounded pilot artifacts."""

    expected = tuple(_safe_id(item, fallback="") for item in expected_task_ids)
    if not expected or any(not item for item in expected) or len(set(expected)) != len(expected):
        raise PilotAggregationError("expected_task_ids must be unique safe identifiers")
    output = Path(output_dir).expanduser().resolve()
    arms = {
        "shadow": _load_arm("shadow", Path(shadow_dir).expanduser().resolve()),
        "guided": _load_arm("guided", Path(guided_dir).expanduser().resolve()),
    }
    all_rows: list[dict[str, object]] = []
    contracts: dict[str, dict[str, object]] = {}
    blockers: list[str] = []
    arm_metrics: dict[str, dict[str, object]] = {}
    arm_status: dict[str, str] = {}
    for name in _ARMS:
        rows, contract, arm_blockers = _arm_rows(arms[name], expected)
        all_rows.extend(rows)
        contracts[name] = contract
        blockers.extend(arm_blockers)
        arm_metrics[name] = _arm_metrics(rows)
        arm_status[name] = (
            "COMPLETE"
            if not arm_blockers
            else "BLOCKED"
            if arms[name].status == "AVAILABLE"
            else arms[name].status
        )

    comparable_fields = ("models", "validation_profile", "backend", "seed_sha256")
    if all(arms[name].status == "AVAILABLE" for name in _ARMS):
        for field in comparable_fields:
            if contracts["shadow"].get(field) != contracts["guided"].get(field):
                blockers.append(f"ARM_{field.upper()}_MISMATCH")
        if not contracts["shadow"].get("seed_present"):
            blockers.append("FROZEN_SEED_MISSING")
    blockers = sorted(set(blockers))
    comparison_valid = not blockers
    shadow_metrics, guided_metrics = arm_metrics["shadow"], arm_metrics["guided"]
    shadow_gates = _mapping(shadow_metrics.get("candidate_gates"))
    guided_gates = _mapping(guided_metrics.get("candidate_gates"))
    summary: dict[str, object] = {
        "schema_version": PILOT_SUMMARY_SCHEMA,
        "generated_at": _utc_now(),
        "status": "COMPLETE" if comparison_valid else "BLOCKED",
        "comparison_valid": comparison_valid,
        "blockers": blockers,
        "expected_task_ids": list(expected),
        "expected_task_count": len(expected),
        "arms": {
            name: {
                "status": arm_status[name],
                "configuration": {
                    "experience_mode": contracts[name].get("experience_mode"),
                    "validation_profile": contracts[name].get("validation_profile"),
                    "backend": contracts[name].get("backend"),
                    "models": list(contracts[name].get("models", ())),
                    "repeats": contracts[name].get("repeats"),
                    "seed_sha256": contracts[name].get("seed_sha256"),
                },
                "metrics": arm_metrics[name],
            }
            for name in _ARMS
        },
        "comparison": {
            "status": "MEASURED" if comparison_valid else "NOT_RUN",
            "guided_minus_shadow": {
                "final_success_rate": _delta(
                    guided_metrics.get("final_success_rate"),
                    shadow_metrics.get("final_success_rate"),
                ) if comparison_valid else None,
                "average_tokens": _delta(
                    guided_metrics.get("average_tokens"),
                    shadow_metrics.get("average_tokens"),
                ) if comparison_valid else None,
                "average_credits": _delta(
                    guided_metrics.get("average_credits"),
                    shadow_metrics.get("average_credits"),
                ) if comparison_valid else None,
                "average_wall_time_seconds": _delta(
                    guided_metrics.get("average_wall_time_seconds"),
                    shadow_metrics.get("average_wall_time_seconds"),
                ) if comparison_valid else None,
                "patch_rejections": _delta(
                    guided_gates.get("patch_rejections"),
                    shadow_gates.get("patch_rejections"),
                ) if comparison_valid else None,
                "strategy_duplication_rate": _delta(
                    guided_metrics.get("strategy_duplication_rate"),
                    shadow_metrics.get("strategy_duplication_rate"),
                ) if comparison_valid else None,
                "repeated_known_failure_strategies": _delta(
                    guided_gates.get("repeated_known_failure_strategies"),
                    shadow_gates.get("repeated_known_failure_strategies"),
                ) if comparison_valid else None,
            }
        },
        "artifacts": {
            "results_ref": "pilot_results.jsonl",
            "summary_ref": "pilot_summary.json",
            "csv_ref": "pilot_summary.csv",
        },
    }
    all_rows.sort(
        key=lambda row: (
            _ARMS.index(str(row["arm"])),
            expected.index(str(row["task_id"]))
            if row.get("task_id") in expected
            else len(expected),
            str(row.get("model")),
            _nonnegative_int(row.get("repeat_index")),
            _nonnegative_int(row.get("attempt_index")),
        )
    )
    _atomic_text(
        output / "pilot_results.jsonl",
        "".join(_canonical_json(row) + "\n" for row in all_rows),
    )
    _atomic_json(output / "pilot_summary.json", summary)
    _write_csv(output / "pilot_summary.csv", all_rows)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m llm4hls_agent.v3_experience_pilot",
        description="Aggregate bounded V3-E shadow/guided pilot evidence.",
    )
    parser.add_argument("--shadow-dir", required=True)
    parser.add_argument("--guided-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="Expected task ID; repeat exactly for the fixed pilot set.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = aggregate_pilot(
            args.shadow_dir,
            args.guided_dir,
            args.output_dir,
            expected_task_ids=tuple(args.task_id) or DEFAULT_V3E_PILOT_TASK_IDS,
        )
    except Exception as exc:
        payload = {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "detail": _safe_id(str(exc), fallback="PILOT_AGGREGATION_FAILED"),
        }
        print(_canonical_json(payload), file=sys.stderr)
        return 3
    payload = {
        "status": summary["status"],
        "comparison_valid": summary["comparison_valid"],
        "blockers": summary["blockers"],
        "results_ref": "pilot_results.jsonl",
        "summary_ref": "pilot_summary.json",
        "csv_ref": "pilot_summary.csv",
    }
    print(_canonical_json(payload), file=stdout or sys.stdout)
    return 0 if summary["comparison_valid"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_V3E_PILOT_TASK_IDS",
    "PILOT_RESULT_SCHEMA",
    "PILOT_SUMMARY_SCHEMA",
    "PilotAggregationError",
    "aggregate_pilot",
    "main",
]
