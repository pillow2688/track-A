"""Mode-specific, deterministic Continuation V2 shadow advice.

The policy deliberately has no graph, Candidate, BudgetLedger, tool, or LLM
authority.  It accepts only the mode and a bounded pre-decision state.  Unknown
or unrecognised fields are ignored so future outcome material cannot affect the
decision or its digest.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

from .scoring import OFFICIAL_ACCELERATION_CAP


CONTINUATION_DECISION_SCHEMA_V2 = "v3.continuation-decision.v2"
MODES = frozenset({"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"})
DECISIONS = frozenset({"ALLOW", "BLOCK", "DEFER_TO_FINAL"})
CONFIDENCE_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})
FALLBACK = "FOLLOW_EXISTING_MAIN_POLICY"

_TOP_LEVEL_FIELDS = frozenset(
    {
        "previous_failure_signature",
        "failure_signature",
        "previous_failure_location_signature",
        "failure_location_signature",
        "previous_evidence_fingerprint",
        "evidence_fingerprint",
        "evidence_delta",
        "observed_strategy_history",
        "current_observed_strategy",
        "strategy_novelty",
        "current_csim",
        "current_synth",
        "current_cosim",
        "baseline_latency",
        "previous_latency",
        "current_latency",
        "previous_ii",
        "current_ii",
        "previous_interval",
        "current_interval",
        "previous_clock_ns",
        "current_clock_ns",
        "current_resource_utilization",
        "remaining_tokens",
        "remaining_credits",
        "remaining_time_seconds",
        "estimated_next_tokens",
        "estimated_next_credits",
        "remaining_rounds",
        "search_closeout_reserve_available",
        "reserve_tight",
        "has_verified_incumbent",
        "has_better_verified_candidate_needing_final",
        "consecutive_no_progress",
        "evidence_complete",
        "evidence_conflict",
        "acceleration_vs_baseline",
        "scoring_cap",
        "incumbent_eligible",
        "tool_config_comparable",
        "clock_gate_passed",
        "resource_gate_passed",
        "cosim_required",
    }
)
_DELTA_FIELDS = frozenset(
    {
        "failure_subtype_changed",
        "failure_location_changed",
        "error_count_reduced",
        "new_actionable_evidence",
        "correctness_progress",
        "synth_stage_advanced",
        "unsupported_construct_removed",
        "scheduling_evidence_new",
        "memory_evidence_new",
        "fifo_evidence_new",
        "producer_consumer_imbalance_new",
        "deadlock_location_changed",
        "topology_understanding_improved",
        "transaction_mismatch_more_specific",
        "structural_strategy_new",
        "bottleneck_changed",
        "latency_improved",
        "ii_improved",
        "interval_improved",
        "clock_improved",
        "resource_pressure_changed",
        "same_failure_signature",
        "same_failure_location",
        "same_fifo_evidence",
        "same_observed_strategy",
    }
)


def canonical_json(value: object) -> str:
    """Return stable JSON used for all V2 decision digests."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:160] or None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _positive_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def legal_eight_x_stop_status(
    *,
    mode: str,
    incumbent_eligible: bool,
    csim_passed: bool,
    synth_passed: bool,
    baseline_latency: object,
    candidate_latency: object,
    tool_config_comparable: bool,
    clock_passed: bool,
    resource_passed: bool,
    cosim_required: bool,
    cosim_passed: bool,
) -> dict[str, object]:
    """Return the shared, fail-closed legality check for an 8x stop.

    This helper has no Graph or tool authority.  Callers must supply facts
    already bound to the current Candidate and its completed search actions.
    """

    normalized_mode = str(mode).upper()
    baseline = _positive_number(baseline_latency)
    candidate = _positive_number(candidate_latency)
    latency_comparable = bool(
        baseline is not None
        and candidate is not None
        and tool_config_comparable is True
    )
    acceleration = (
        baseline / candidate
        if latency_comparable and baseline is not None and candidate is not None
        else None
    )
    blockers: list[str] = []
    if normalized_mode != "OPTIMIZE":
        blockers.append("MODE_NOT_OPTIMIZE")
    if incumbent_eligible is not True:
        blockers.append("INCUMBENT_NOT_ELIGIBLE")
    if csim_passed is not True:
        blockers.append("CSIM_NOT_PASS")
    if synth_passed is not True:
        blockers.append("SYNTH_NOT_PASS")
    if not latency_comparable:
        blockers.append("LATENCY_NOT_COMPARABLE")
    if clock_passed is not True:
        blockers.append("CLOCK_GATE_NOT_PASS")
    if resource_passed is not True:
        blockers.append("RESOURCE_GATE_NOT_PASS")
    if cosim_required is True and cosim_passed is not True:
        blockers.append("REQUIRED_COSIM_NOT_PASS")
    if acceleration is None or acceleration < OFFICIAL_ACCELERATION_CAP:
        blockers.append("ACCELERATION_BELOW_8X")
    return {
        "checked": True,
        "reached": not blockers,
        "mode": normalized_mode,
        "incumbent_eligible": bool(incumbent_eligible),
        "csim_passed": bool(csim_passed),
        "synth_passed": bool(synth_passed),
        "cosim_required": bool(cosim_required),
        "cosim_passed": bool(cosim_passed),
        "baseline_latency": baseline,
        "candidate_latency": candidate,
        "tool_config_comparable": bool(tool_config_comparable),
        "latency_comparable": latency_comparable,
        "clock_passed": bool(clock_passed),
        "resource_passed": bool(resource_passed),
        "acceleration": acceleration,
        "scoring_cap": OFFICIAL_ACCELERATION_CAP,
        "blockers": blockers,
    }


def _atoms(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        sorted(
            {
                normalized
                for item in value[:32]
                if (normalized := (_text(item) or "").upper())
            }
        )
    )


def _history(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    rows: list[tuple[str, ...]] = []
    for item in value[-16:]:
        if isinstance(item, Mapping):
            item = item.get("observed_strategy_atoms")
        atoms = _atoms(item)
        if atoms:
            rows.append(atoms)
    return tuple(rows)


def _resource_utilization(value: object) -> dict[str, float] | float | None:
    number = _number(value)
    if number is not None:
        return number
    mapping = _mapping(value)
    bounded = {
        str(key)[:32]: number
        for key, item in sorted(mapping.items(), key=lambda row: str(row[0]))[:16]
        if (number := _number(item)) is not None
    }
    return bounded or None


def _bounded_pre_state(pre_state: Mapping[str, object]) -> dict[str, object]:
    """Copy only the documented decision-time feature allow-list."""

    state: dict[str, object] = {}
    for key in _TOP_LEVEL_FIELDS:
        value = pre_state.get(key)
        if key == "evidence_delta":
            delta = _mapping(value)
            state[key] = {
                name: bool(delta[name])
                for name in sorted(_DELTA_FIELDS)
                if isinstance(delta.get(name), bool)
            }
        elif key == "observed_strategy_history":
            state[key] = _history(value)
        elif key == "current_observed_strategy":
            state[key] = _atoms(value)
        elif key in {
            "previous_failure_signature",
            "failure_signature",
            "previous_failure_location_signature",
            "failure_location_signature",
            "previous_evidence_fingerprint",
            "evidence_fingerprint",
            "strategy_novelty",
            "current_csim",
            "current_synth",
            "current_cosim",
        }:
            state[key] = _text(value)
        elif key in {
            "search_closeout_reserve_available",
            "reserve_tight",
            "has_verified_incumbent",
            "has_better_verified_candidate_needing_final",
            "evidence_complete",
            "evidence_conflict",
            "incumbent_eligible",
            "tool_config_comparable",
            "clock_gate_passed",
            "resource_gate_passed",
            "cosim_required",
        }:
            state[key] = _optional_bool(value)
        elif key == "current_resource_utilization":
            state[key] = _resource_utilization(value)
        else:
            state[key] = _number(value)
    return state


def _same(left: object, right: object) -> bool:
    return left not in (None, "") and left == right


def _strategy_state(state: Mapping[str, object]) -> tuple[bool, bool, bool]:
    novelty = str(state.get("strategy_novelty") or "UNKNOWN").upper()
    current = tuple(state.get("current_observed_strategy") or ())
    history = tuple(state.get("observed_strategy_history") or ())
    same = novelty in {"NONE", "DUPLICATE"} or bool(current and current in history[:-1])
    novel = novelty in {"HIGH", "NOVEL"} or bool(current and current not in history[:-1])
    known = novelty != "UNKNOWN" or bool(current)
    return same, novel, known


def _no_progress_count(state: Mapping[str, object]) -> int:
    value = state.get("consecutive_no_progress")
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def _decision(
    *,
    mode: str,
    decision: str,
    confidence: str,
    reason_codes: Sequence[str],
    supporting_evidence: Sequence[str],
) -> dict[str, object]:
    if decision not in DECISIONS or confidence not in CONFIDENCE_LEVELS:
        raise ValueError("invalid V2 decision")
    record: dict[str, object] = {
        "schema_version": CONTINUATION_DECISION_SCHEMA_V2,
        "mode": mode,
        "decision": decision,
        "confidence": confidence,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "supporting_evidence": list(dict.fromkeys(supporting_evidence)),
        "fallback": FALLBACK,
    }
    record["decision_digest"] = canonical_sha256(record)
    return record


def _generic_guard(mode: str, state: Mapping[str, object]) -> dict[str, object] | None:
    reserve = state.get("search_closeout_reserve_available")
    incumbent = state.get("has_verified_incumbent") is True
    if reserve is False:
        return _decision(
            mode=mode,
            decision="DEFER_TO_FINAL",
            confidence="HIGH",
            reason_codes=("SEARCH_CLOSEOUT_RESERVE_UNAVAILABLE",),
            supporting_evidence=("search_closeout_reserve_available=false",),
        )
    if state.get("evidence_conflict") is True:
        return _decision(
            mode=mode,
            decision="DEFER_TO_FINAL" if incumbent else "ALLOW",
            confidence="LOW",
            reason_codes=("CONFLICTING_PRE_STATE_EVIDENCE",),
            supporting_evidence=("evidence_conflict=true",),
        )
    return None


def _repair(mode: str, state: Mapping[str, object]) -> dict[str, object]:
    delta = _mapping(state.get("evidence_delta"))
    same_strategy, novel_strategy, strategy_known = _strategy_state(state)
    progress = [
        ("FAILURE_SUBTYPE_CHANGED", delta.get("failure_subtype_changed")),
        ("FAILURE_LOCATION_CHANGED", delta.get("failure_location_changed")),
        ("ERROR_COUNT_REDUCED", delta.get("error_count_reduced")),
        ("NEW_ACTIONABLE_EVIDENCE", delta.get("new_actionable_evidence")),
        ("CORRECTNESS_PROGRESS", delta.get("correctness_progress")),
        ("NOVEL_REPAIR_STRATEGY", novel_strategy),
    ]
    positive = [reason for reason, present in progress if present]
    if positive:
        return _decision(
            mode=mode,
            decision="ALLOW",
            confidence="HIGH" if len(positive) >= 2 else "MEDIUM",
            reason_codes=positive,
            supporting_evidence=tuple(reason.lower() for reason in positive),
        )

    same_failure = bool(delta.get("same_failure_signature")) or _same(
        state.get("previous_failure_signature"), state.get("failure_signature")
    )
    same_location = bool(delta.get("same_failure_location")) or _same(
        state.get("previous_failure_location_signature"),
        state.get("failure_location_signature"),
    )
    repeated_no_progress = _no_progress_count(state) >= 2
    if same_failure and same_location and same_strategy and repeated_no_progress:
        incumbent = state.get("has_verified_incumbent") is True
        reserve_tight = state.get("reserve_tight") is True
        return _decision(
            mode=mode,
            decision="DEFER_TO_FINAL" if incumbent and reserve_tight else "BLOCK",
            confidence="MEDIUM",
            reason_codes=("REPEATED_IDENTICAL_REPAIR_WITHOUT_PROGRESS",),
            supporting_evidence=(
                "same_failure_signature",
                "same_failure_location",
                "same_observed_strategy",
                "consecutive_no_progress>=2",
            ),
        )

    incomplete = state.get("evidence_complete") is False or not (
        state.get("failure_signature") or delta or strategy_known
    )
    return _decision(
        mode=mode,
        decision="ALLOW",
        confidence="LOW",
        reason_codes=(
            "INCOMPLETE_EVIDENCE_CONTINUE_CONSERVATIVELY"
            if incomplete
            else "SINGLE_OR_UNCONFIRMED_FAILURE_NOT_ENOUGH_TO_STOP",
        ),
        supporting_evidence=("one_failure_is_not_a_stop_condition",),
    )


def _synth_fix(mode: str, state: Mapping[str, object]) -> dict[str, object]:
    delta = _mapping(state.get("evidence_delta"))
    same_strategy, novel_strategy, strategy_known = _strategy_state(state)
    progress = [
        ("SYNTH_STAGE_ADVANCED", delta.get("synth_stage_advanced")),
        ("UNSUPPORTED_CONSTRUCT_REMOVED", delta.get("unsupported_construct_removed")),
        ("NEW_SCHEDULING_EVIDENCE", delta.get("scheduling_evidence_new")),
        ("NEW_MEMORY_EVIDENCE", delta.get("memory_evidence_new")),
        ("NEW_ACTIONABLE_EVIDENCE", delta.get("new_actionable_evidence")),
        ("NOVEL_SYNTH_STRATEGY", novel_strategy),
    ]
    positive = [reason for reason, present in progress if present]
    if positive:
        return _decision(
            mode=mode,
            decision="ALLOW",
            confidence="HIGH" if len(positive) >= 2 else "MEDIUM",
            reason_codes=positive,
            supporting_evidence=tuple(reason.lower() for reason in positive),
        )

    same_failure = bool(delta.get("same_failure_signature")) or _same(
        state.get("previous_failure_signature"), state.get("failure_signature")
    )
    same_location = bool(delta.get("same_failure_location")) or _same(
        state.get("previous_failure_location_signature"),
        state.get("failure_location_signature"),
    )
    if (
        same_failure
        and same_location
        and same_strategy
        and _no_progress_count(state) >= 2
    ):
        incumbent = state.get("has_verified_incumbent") is True
        return _decision(
            mode=mode,
            decision="DEFER_TO_FINAL" if incumbent else "BLOCK",
            confidence="MEDIUM",
            reason_codes=("REPEATED_IDENTICAL_SYNTH_FAILURE_WITHOUT_PROGRESS",),
            supporting_evidence=(
                "same_unsupported_construct",
                "same_failure_location",
                "same_observed_strategy",
                "consecutive_no_progress>=2",
            ),
        )

    incomplete = state.get("evidence_complete") is False or not (
        state.get("failure_signature") or delta or strategy_known
    )
    return _decision(
        mode=mode,
        decision="ALLOW",
        confidence="LOW",
        reason_codes=(
            "INCOMPLETE_EVIDENCE_CONTINUE_CONSERVATIVELY"
            if incomplete
            else "SYNTH_FAILURE_ALONE_NOT_ENOUGH_TO_STOP",
        ),
        supporting_evidence=("synth_failure_is_not_a_stop_condition",),
    )


def _structural_fix(mode: str, state: Mapping[str, object]) -> dict[str, object]:
    delta = _mapping(state.get("evidence_delta"))
    same_strategy, novel_strategy, strategy_known = _strategy_state(state)
    progress = [
        ("NEW_FIFO_EVIDENCE", delta.get("fifo_evidence_new")),
        (
            "NEW_PRODUCER_CONSUMER_IMBALANCE",
            delta.get("producer_consumer_imbalance_new"),
        ),
        ("DEADLOCK_LOCATION_CHANGED", delta.get("deadlock_location_changed")),
        (
            "TOPOLOGY_UNDERSTANDING_IMPROVED",
            delta.get("topology_understanding_improved"),
        ),
        (
            "TRANSACTION_MISMATCH_MORE_SPECIFIC",
            delta.get("transaction_mismatch_more_specific"),
        ),
        ("NEW_ACTIONABLE_EVIDENCE", delta.get("new_actionable_evidence")),
        ("NOVEL_STRUCTURAL_STRATEGY", delta.get("structural_strategy_new") or novel_strategy),
    ]
    positive = [reason for reason, present in progress if present]
    if positive:
        return _decision(
            mode=mode,
            decision="ALLOW",
            confidence="MEDIUM",
            reason_codes=positive,
            supporting_evidence=tuple(reason.lower() for reason in positive),
        )

    same_failure = bool(delta.get("same_failure_signature")) or _same(
        state.get("previous_failure_signature"), state.get("failure_signature")
    )
    same_location = bool(delta.get("same_failure_location")) or _same(
        state.get("previous_failure_location_signature"),
        state.get("failure_location_signature"),
    )
    same_fifo = delta.get("same_fifo_evidence") is True
    no_actionable = delta.get("new_actionable_evidence") is not True
    reserve_safe = state.get("search_closeout_reserve_available") is True
    if (
        same_failure
        and same_location
        and same_fifo
        and same_strategy
        and no_actionable
        and reserve_safe
        and _no_progress_count(state) >= 2
    ):
        return _decision(
            mode=mode,
            decision="BLOCK",
            confidence="MEDIUM",
            reason_codes=("REPEATED_IDENTICAL_STRUCTURAL_FAILURE_WITHOUT_PROGRESS",),
            supporting_evidence=(
                "same_structural_failure_signature",
                "same_deadlock_location",
                "same_stream_fifo_evidence",
                "same_observed_structural_strategy",
                "consecutive_no_progress>=2",
            ),
        )

    incomplete = state.get("evidence_complete") is False or not (
        state.get("failure_signature") or delta or strategy_known
    )
    return _decision(
        mode=mode,
        decision="ALLOW",
        confidence="LOW",
        reason_codes=(
            "INCOMPLETE_STRUCTURAL_EVIDENCE_CONTINUE_CONSERVATIVELY"
            if incomplete
            else "STRUCTURAL_STOP_CONDITIONS_NOT_ALL_MET",
        ),
        supporting_evidence=("structural_policy_is_false_block_averse",),
    )


def _improved(previous: object, current: object) -> bool | None:
    before, after = _number(previous), _number(current)
    if before is None or after is None:
        return None
    return after < before


def _optimize(mode: str, state: Mapping[str, object]) -> dict[str, object]:
    delta = _mapping(state.get("evidence_delta"))
    same_strategy, novel_strategy, strategy_known = _strategy_state(state)
    previous_latency = _number(state.get("previous_latency"))
    current_latency = _number(state.get("current_latency"))
    if previous_latency is None or current_latency is None or previous_latency == 0:
        relative_gain = None
    else:
        relative_gain = (previous_latency - current_latency) / previous_latency

    incumbent = state.get("has_verified_incumbent") is True
    low_gain = relative_gain is not None and relative_gain <= 0.01
    no_gain = relative_gain is not None and relative_gain <= 0

    if incumbent and low_gain and same_strategy:
        return _decision(
            mode=mode,
            decision="DEFER_TO_FINAL",
            confidence="MEDIUM",
            reason_codes=("LOW_MARGINAL_GAIN_WITH_REPEATED_STRATEGY",),
            supporting_evidence=(
                "relative_latency_gain<=0.01",
                "has_verified_incumbent",
                "same_observed_strategy",
            ),
        )

    progress = [
        (
            "SIGNIFICANT_LATENCY_IMPROVEMENT",
            relative_gain is not None and relative_gain > 0.01,
        ),
        ("II_IMPROVED", delta.get("ii_improved") or _improved(state.get("previous_ii"), state.get("current_ii"))),
        (
            "TRANSACTION_INTERVAL_IMPROVED",
            delta.get("interval_improved")
            or _improved(state.get("previous_interval"), state.get("current_interval")),
        ),
        (
            "CLOCK_IMPROVED",
            delta.get("clock_improved")
            or _improved(state.get("previous_clock_ns"), state.get("current_clock_ns")),
        ),
        ("BOTTLENECK_CHANGED", delta.get("bottleneck_changed")),
        ("NEW_OPTIMIZATION_STRATEGY", novel_strategy),
    ]
    positive = [reason for reason, present in progress if present]
    if positive:
        return _decision(
            mode=mode,
            decision="ALLOW",
            confidence="HIGH" if len(positive) >= 2 else "MEDIUM",
            reason_codes=positive,
            supporting_evidence=tuple(reason.lower() for reason in positive),
        )

    repeated_no_progress = (
        no_gain
        and same_strategy
        and delta.get("new_actionable_evidence") is not True
        and _no_progress_count(state) >= 2
    )
    if repeated_no_progress:
        needs_final = (
            state.get("has_better_verified_candidate_needing_final") is True or incumbent
        )
        return _decision(
            mode=mode,
            decision="DEFER_TO_FINAL" if needs_final else "BLOCK",
            confidence="MEDIUM",
            reason_codes=("REPEATED_OPTIMIZATION_WITHOUT_GAIN_OR_NEW_EVIDENCE",),
            supporting_evidence=(
                "no_latency_gain",
                "same_observed_strategy",
                "no_new_actionable_evidence",
                "consecutive_no_progress>=2",
            ),
        )

    incomplete = state.get("evidence_complete") is False or not (
        relative_gain is not None or delta or strategy_known
    )
    resource_evidence = (
        "resource_utilization=UNKNOWN"
        if state.get("current_resource_utilization") is None
        else "resource_utilization=KNOWN"
    )
    return _decision(
        mode=mode,
        decision="DEFER_TO_FINAL" if incumbent and incomplete else "ALLOW",
        confidence="LOW",
        reason_codes=(
            "INCOMPLETE_OPTIMIZATION_EVIDENCE"
            if incomplete
            else "OPTIMIZATION_STOP_CONDITIONS_NOT_MET",
        ),
        supporting_evidence=(
            "unknown_metrics_are_not_zero",
            "existing_incumbent_available" if incumbent else "no_verified_incumbent",
            resource_evidence,
        ),
    )


def continuation_decision_v2(
    *, mode: str, pre_state: Mapping[str, object]
) -> dict[str, object]:
    """Return deterministic offline/shadow advice from pre-decision facts only."""

    normalized_mode = str(mode).upper()
    if normalized_mode not in MODES:
        raise ValueError("unsupported continuation mode")
    if not isinstance(pre_state, Mapping):
        raise TypeError("pre_state must be a mapping")
    state = _bounded_pre_state(pre_state)
    guarded = _generic_guard(normalized_mode, state)
    if guarded is not None:
        return guarded
    if normalized_mode == "REPAIR":
        return _repair(normalized_mode, state)
    if normalized_mode == "SYNTH_FIX":
        return _synth_fix(normalized_mode, state)
    if normalized_mode == "STRUCTURAL_FIX":
        return _structural_fix(normalized_mode, state)
    return _optimize(normalized_mode, state)
