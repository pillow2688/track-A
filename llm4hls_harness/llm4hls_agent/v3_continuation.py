"""Deterministic, leakage-safe continuation and performance-area advice.

This module deliberately has no tool, Candidate, LLM, or graph authority.  It
only turns facts already available *before* a prospective Planner call into a
hash-bound recommendation.  The V3 graph consumes the result at its existing
budget/stop gates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence


CONTINUATION_DECISION_SCHEMA = "v3.continuation-decision.v1"
CONTINUATION_POLICY_VERSION = "v3.continuation-policy.v1"
PERFORMANCE_AREA_DELTA_SCHEMA = "v3.performance-area-delta.v1"
FOLLOWUP_OUTCOME_SCHEMA = "v3.followup-outcome.v1"
POLICY_MODES = frozenset({"off", "shadow", "enforce"})
DECISIONS = frozenset({"ALLOW", "BLOCK", "DEFER_TO_FINAL"})
MODES = frozenset({"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"})
_RESOURCE_ALIASES = {
    "lut": ("LUT", "lut"),
    "ff": ("FF", "ff"),
    "dsp": ("DSP", "dsp"),
    "bram": ("BRAM_18K", "BRAM", "bram", "bram_18k"),
    "uram": ("URAM", "uram"),
}
_DYNAMIC_TEXT = re.compile(
    r"(?:[A-Za-z]:)?(?:/[^\s:]+)+|\b(?:20\d{2}|1\d{9,})\b|"
    r"(?:candidate|run|action)[_-]?[0-9a-f]{6,}",
    re.IGNORECASE,
)
_STRATEGY_PATTERNS = (
    ("PIPELINE_ONLY", re.compile(r"#pragma\s+HLS\s+PIPELINE", re.I)),
    ("LOOP_UNROLL", re.compile(r"#pragma\s+HLS\s+UNROLL", re.I)),
    ("MEMORY_PARTITION", re.compile(r"#pragma\s+HLS\s+ARRAY_(?:PARTITION|RESHAPE)", re.I)),
    ("DATAFLOW", re.compile(r"#pragma\s+HLS\s+DATAFLOW", re.I)),
    ("FIFO_DEPTH", re.compile(r"#pragma\s+HLS\s+STREAM|depth\s*=", re.I)),
    ("PARALLEL_REDUCTION", re.compile(r"partial|reduce|accum|sum", re.I)),
    ("BITWIDTH", re.compile(r"ap_(?:u?int|fixed)|(?:int|uint)\d+_t", re.I)),
)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(_DYNAMIC_TEXT.sub("<dynamic>", value).split())
    return normalized[:240] or None


def _bucket(value: object, *, scale: float = 1.0) -> str | None:
    number = _number(value)
    if number is None:
        return None
    return str(int(number / scale))


def _first(mapping: Mapping[str, object], aliases: Sequence[str]) -> float | None:
    for key in aliases:
        if key in mapping:
            return _number(mapping[key])
    return None


def _latency(metrics: Mapping[str, object]) -> float | None:
    return _first(_mapping(metrics.get("latency")), ("worst", "average", "max"))


def _interval(metrics: Mapping[str, object]) -> float | None:
    return _first(_mapping(metrics.get("interval")), ("max", "average", "worst"))


def _clock(metrics: Mapping[str, object]) -> float | None:
    return _number(metrics.get("estimated_clock_period_ns"))


def _resource_values(metrics: Mapping[str, object]) -> tuple[dict[str, float | None], dict[str, float | None]]:
    used_raw, available_raw = _mapping(metrics.get("resources")), _mapping(metrics.get("available_resources"))
    used = {key: _first(used_raw, aliases) for key, aliases in _RESOURCE_ALIASES.items()}
    available = {key: _first(available_raw, aliases) for key, aliases in _RESOURCE_ALIASES.items()}
    return used, available


@dataclass(frozen=True)
class PerformanceAreaPolicy:
    policy_version: str
    weights: Mapping[str, float]
    thresholds: Mapping[str, float]

    def __post_init__(self) -> None:
        weights = {key: float(self.weights[key]) for key in _RESOURCE_ALIASES}
        thresholds = {key: float(self.thresholds[key]) for key in ("medium", "high", "critical")}
        if not self.policy_version or any(value < 0 for value in weights.values()):
            raise ValueError("invalid performance-area policy weights")
        if not (0 < sum(weights.values()) <= 1.000001):
            raise ValueError("performance-area weights must sum to a positive value no larger than one")
        if not (0 < thresholds["medium"] < thresholds["high"] < thresholds["critical"] <= 1):
            raise ValueError("invalid resource pressure thresholds")
        object.__setattr__(self, "weights", MappingProxyType(weights))
        object.__setattr__(self, "thresholds", MappingProxyType(thresholds))

    def to_dict(self) -> dict[str, object]:
        return {"policy_version": self.policy_version, "weights": dict(self.weights), "resource_pressure_thresholds": dict(self.thresholds)}


def load_performance_area_policy(path: str | Path | None = None) -> PerformanceAreaPolicy:
    policy_path = Path(path) if path else Path(__file__).with_name("config") / "performance_area_policy_v1.json"
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("performance-area policy must be an object")
    return PerformanceAreaPolicy(
        policy_version=str(raw.get("policy_version", "")),
        weights=_mapping(raw.get("weights")),
        thresholds=_mapping(raw.get("resource_pressure_thresholds")),
    )


def _pressure(utilizations: Mapping[str, float | None], policy: PerformanceAreaPolicy) -> str:
    values = [value for value in utilizations.values() if value is not None]
    if not values:
        return "UNKNOWN"
    maximum = max(values)
    if maximum >= policy.thresholds["critical"]:
        return "CRITICAL"
    if maximum >= policy.thresholds["high"]:
        return "HIGH"
    if maximum >= policy.thresholds["medium"]:
        return "MEDIUM"
    return "LOW"


def performance_area_delta(
    before: Mapping[str, object], after: Mapping[str, object], *, policy: PerformanceAreaPolicy | None = None, reference: str = "baseline"
) -> dict[str, object]:
    """Describe resource-aware change without re-labelling it as physical area or power."""

    policy = policy or load_performance_area_policy()
    before_used, before_available = _resource_values(before)
    after_used, after_available = _resource_values(after)
    before_util = {key: (before_used[key] / before_available[key] if before_used[key] is not None and before_available[key] not in (None, 0) else None) for key in _RESOURCE_ALIASES}
    after_util = {key: (after_used[key] / after_available[key] if after_used[key] is not None and after_available[key] not in (None, 0) else None) for key in _RESOURCE_ALIASES}
    complete_before = all(value is not None for value in before_util.values())
    complete_after = all(value is not None for value in after_util.values())
    area_before = sum(policy.weights[key] * float(before_util[key]) for key in _RESOURCE_ALIASES) if complete_before else None
    area_after = sum(policy.weights[key] * float(after_util[key]) for key in _RESOURCE_ALIASES) if complete_after else None
    latency_before, latency_after = _latency(before), _latency(after)
    pa_before = latency_before * area_before if latency_before is not None and area_before is not None else None
    pa_after = latency_after * area_after if latency_after is not None and area_after is not None else None
    latency_improved = latency_before is not None and latency_after is not None and latency_after < latency_before
    area_improved = area_before is not None and area_after is not None and area_after < area_before
    resource_regressed = any(
        before_util[key] is not None and after_util[key] is not None and after_util[key] > before_util[key]
        for key in _RESOURCE_ALIASES
    )
    if latency_before is None or latency_after is None or area_before is None or area_after is None:
        relation, delta_class = "UNKNOWN", "UNKNOWN"
    elif latency_after <= latency_before and area_after <= area_before and (latency_after < latency_before or area_after < area_before):
        relation = "DOMINATES"
        delta_class = "BALANCED_PERFORMANCE_AREA_IMPROVEMENT" if latency_improved and area_improved else "PERFORMANCE_IMPROVEMENT" if latency_improved else "AREA_IMPROVEMENT"
    elif latency_after >= latency_before and area_after >= area_before and (latency_after > latency_before or area_after > area_before):
        relation, delta_class = "DOMINATED", "PERFORMANCE_AREA_REGRESSION"
    elif latency_improved != area_improved:
        relation, delta_class = "NON_DOMINATED", "PERFORMANCE_AREA_TRADEOFF"
    else:
        relation, delta_class = "EQUAL", "NO_PERFORMANCE_AREA_IMPROVEMENT"
    return {
        "schema_version": PERFORMANCE_AREA_DELTA_SCHEMA,
        "policy": policy.to_dict(), "reference": reference,
        "before": {"latency": latency_before, "transaction_interval": _interval(before), "estimated_clock_period_ns": _clock(before), "resources": before_used, "available_resources": before_available, "utilization": before_util, "area_proxy": area_before, "performance_area_proxy": pa_before},
        "after": {"latency": latency_after, "transaction_interval": _interval(after), "estimated_clock_period_ns": _clock(after), "resources": after_used, "available_resources": after_available, "utilization": after_util, "area_proxy": area_after, "performance_area_proxy": pa_after},
        "absolute_resource_delta": {key: (None if before_used[key] is None or after_used[key] is None else after_used[key] - before_used[key]) for key in _RESOURCE_ALIASES},
        "relative_resource_delta": {key: (None if before_util[key] is None or after_util[key] is None else after_util[key] - before_util[key]) for key in _RESOURCE_ALIASES},
        "performance_area_proxy_delta": None if pa_before is None or pa_after is None else pa_after - pa_before,
        "pareto_relation": relation, "delta_class": delta_class,
        "latency_improved": latency_improved, "transaction_interval_improved": (_interval(before) is not None and _interval(after) is not None and _interval(after) < _interval(before)),
        "clock_improved": (_clock(before) is not None and _clock(after) is not None and _clock(after) < _clock(before)),
        "area_proxy_improved": area_improved, "performance_area_proxy_improved": pa_before is not None and pa_after is not None and pa_after < pa_before,
        "resource_pressure_before": _pressure(before_util, policy), "resource_pressure_after": _pressure(after_util, policy),
        "resource_pressure_changed": _pressure(before_util, policy) != _pressure(after_util, policy),
        "tradeoff_detected": relation == "NON_DOMINATED" or resource_regressed,
        "data_complete": complete_before and complete_after and latency_before is not None and latency_after is not None,
        "power": "NOT_COLLECTED",
    }


def evidence_fingerprint(mode: str, evidence: Mapping[str, object], metrics: Mapping[str, object] | None = None) -> dict[str, object]:
    """Return stable, task-ID-free facts used by the delta checker."""

    if mode not in MODES:
        raise ValueError("unsupported continuation mode")
    facts: dict[str, object] = {"mode": mode}
    if mode == "OPTIMIZE":
        metrics = metrics or evidence
        loops = _mapping(metrics.get("loop_evidence")).get("loops")
        critical = _mapping(loops[0]) if isinstance(loops, list) and loops and isinstance(loops[0], Mapping) else {}
        observations = evidence.get("observations")
        observations = observations if isinstance(observations, list) else []
        kinds = sorted({str(_mapping(item).get("kind")) for item in observations if _mapping(item).get("kind")})
        facts.update({"bottleneck": kinds[0] if kinds else _text(evidence.get("primary_bottleneck")), "critical_loop": _text(critical.get("loop_id") or critical.get("name")), "achieved_ii": _bucket(critical.get("pipeline_ii")), "trip_count_bucket": _bucket(critical.get("trip_count"), scale=16), "transaction_interval_bucket": _bucket(_interval(metrics), scale=4), "scheduling": tuple(kinds), "resource_pressure": _text(evidence.get("resource_pressure"))})
    else:
        facts.update({"failure_stage": _text(evidence.get("failure_stage") or evidence.get("stage")), "failure_subtype": _text(evidence.get("failure_subtype") or evidence.get("subtype") or evidence.get("category")), "source_location": _text(evidence.get("source_location")), "affected_object": _text(evidence.get("affected_symbol") or evidence.get("stream") or evidence.get("fifo") or evidence.get("interface")), "expected_actual_category": _text(evidence.get("expected_actual_category") or evidence.get("mismatch_category"))})
    facts["fingerprint"] = canonical_sha256(facts)
    return facts


def evidence_delta(before: Mapping[str, object] | None, after: Mapping[str, object] | None, *, mode: str, before_metrics: Mapping[str, object] | None = None, after_metrics: Mapping[str, object] | None = None) -> dict[str, object]:
    before_fingerprint = evidence_fingerprint(mode, before or {}, before_metrics)
    after_fingerprint = evidence_fingerprint(mode, after or {}, after_metrics)
    changed = {key: before_fingerprint.get(key) != after_fingerprint.get(key) for key in before_fingerprint if key not in {"fingerprint", "mode"}}
    semantic = [key for key, value in changed.items() if value]
    meaningful = {"failure_stage", "failure_subtype", "bottleneck", "critical_loop", "scheduling", "affected_object", "source_location", "resource_pressure"}
    actionable = [key for key in semantic if key in meaningful and after_fingerprint.get(key) not in (None, "", (), "0")]
    strength = "HIGH" if len(actionable) >= 2 else "MEDIUM" if actionable else "LOW" if semantic else "NONE"
    reasons = ["EVIDENCE_" + key.upper() + "_CHANGED" for key in actionable]
    return {"before_fingerprint": before_fingerprint, "after_fingerprint": after_fingerprint, "has_new_evidence": bool(semantic), "has_actionable_new_evidence": bool(actionable), "failure_stage_changed": changed.get("failure_stage", False), "failure_subtype_changed": changed.get("failure_subtype", False), "bottleneck_changed": changed.get("bottleneck", False), "critical_loop_changed": changed.get("critical_loop", False), "affected_object_changed": changed.get("affected_object", False), "source_location_changed": changed.get("source_location", False), "resource_pressure_changed": changed.get("resource_pressure", False), "evidence_strength": strength, "reason_codes": reasons}


def observed_strategy_atoms(*, declared: Sequence[object] = (), patch: str = "") -> tuple[str, ...]:
    observed: set[str] = set()
    for name, pattern in _STRATEGY_PATTERNS:
        if pattern.search(patch):
            observed.add(name)
    # Concrete Patch behavior is authoritative.  Self-reported labels are a
    # fallback only when a bounded static scan sees no known action at all.
    if observed:
        return tuple(sorted(observed))
    return tuple(sorted({str(item).strip().upper() for item in declared if str(item).strip()}))


def strategy_novelty(*, declared: Sequence[object] = (), patch: str = "", attempted: Sequence[Sequence[object]] = (), failed: Sequence[Sequence[object]] = (), patch_digests: Sequence[str] = ()) -> dict[str, object]:
    atoms = observed_strategy_atoms(declared=declared, patch=patch)
    attempted_sets = {tuple(sorted(str(item).upper() for item in group)) for group in attempted}
    failed_atoms = {str(item).upper() for group in failed for item in group}
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest() if patch else ""
    duplicate_strategy = atoms in attempted_sets if atoms else False
    duplicate_patch = bool(digest and digest in set(patch_digests))
    novelty = "NONE" if duplicate_strategy or duplicate_patch else "HIGH" if atoms and not set(atoms).issubset(failed_atoms) else "LOW" if atoms else "NONE"
    reasons = (["DUPLICATE_STRATEGY"] if duplicate_strategy else []) + (["DUPLICATE_PATCH"] if duplicate_patch else [])
    return {"previous_strategy_atoms": sorted({item for group in attempted for item in (str(value).upper() for value in group)}), "failed_strategy_atoms": sorted(failed_atoms), "untried_matched_strategy_atoms": [atom for atom in atoms if atom not in failed_atoms], "observed_strategy_atoms": list(atoms), "duplicate_strategy": duplicate_strategy, "duplicate_patch": duplicate_patch, "strategy_novelty": novelty, "reason_codes": reasons}


def continuation_cost(*, ledger: Mapping[str, object], estimated_input_tokens: int, estimated_output_tokens: int, estimated_credits: int, estimated_wall_time_seconds: float, final_reserve_safe: bool) -> dict[str, object]:
    return {"estimated_next_input_tokens": max(0, int(estimated_input_tokens)), "estimated_next_output_tokens": max(0, int(estimated_output_tokens)), "estimated_next_total_tokens": max(0, int(estimated_input_tokens)) + max(0, int(estimated_output_tokens)), "estimated_next_credits": max(0, int(estimated_credits)), "estimated_next_wall_time_seconds": max(0.0, float(estimated_wall_time_seconds)), "remaining_tokens": max(0, int(ledger.get("tokens_remaining") or 0)), "remaining_credits": ledger.get("credits_remaining"), "final_reserve_safe": bool(final_reserve_safe)}


def continuation_decision(*, run_id: str, round_index: int, mode: str, policy_mode: str, has_correct_candidate: bool, has_strict_latency_improvement: bool, performance_area: Mapping[str, object], delta: Mapping[str, object], strategies: Mapping[str, object], cost: Mapping[str, object], remaining_rounds: int) -> dict[str, object]:
    """Compute the transparent rule-based decision from pre-follow-up facts only."""
    if mode not in MODES or policy_mode not in POLICY_MODES:
        raise ValueError("invalid continuation mode")
    reason_codes: list[str] = []
    benefit: list[str] = []
    penalties: list[str] = []
    score = 0
    if remaining_rounds <= 0:
        reason_codes.append("MAX_ROUNDS_REACHED")
    if not cost.get("final_reserve_safe", False):
        reason_codes.append("FINAL_RESERVE_UNSAFE")
    if int(cost.get("remaining_tokens") or 0) < int(cost.get("estimated_next_total_tokens") or 0):
        reason_codes.append("TOKEN_INSUFFICIENT")
    remaining_credits = cost.get("remaining_credits")
    if isinstance(remaining_credits, (int, float)) and remaining_credits < int(cost.get("estimated_next_credits") or 0):
        reason_codes.append("CREDIT_INSUFFICIENT")
    if delta.get("has_actionable_new_evidence"):
        score += 3; benefit.append("ACTIONABLE_EVIDENCE")
    else:
        score -= 5; penalties.append("NO_ACTIONABLE_EVIDENCE")
    if delta.get("evidence_strength") == "HIGH": score += 4
    elif delta.get("evidence_strength") == "LOW": score -= 2
    if strategies.get("untried_matched_strategy_atoms"):
        score += 2; benefit.append("UNTRIED_MATCHED_STRATEGY")
    if strategies.get("duplicate_strategy") or strategies.get("duplicate_patch"):
        score -= 4; reason_codes.append("DUPLICATE_STRATEGY_OR_PATCH")
    if has_correct_candidate and has_strict_latency_improvement and mode == "OPTIMIZE" and not delta.get("bottleneck_changed"):
        score -= 3; penalties.append("IMPROVED_WITHOUT_NEW_BOTTLENECK")
    if mode == "STRUCTURAL_FIX" and delta.get("failure_subtype_changed"):
        score += 2; benefit.append("STRUCTURAL_SUBTYPE_REFINED")
    if performance_area.get("tradeoff_detected") and strategies.get("untried_matched_strategy_atoms"):
        score += 3; benefit.append("PERFORMANCE_AREA_REBALANCING_OPPORTUNITY")
    if performance_area.get("pareto_relation") == "DOMINATES" and not delta.get("has_actionable_new_evidence"):
        score -= 2; penalties.append("CURRENT_CANDIDATE_PARETO_DOMINATES")
    if cost.get("final_reserve_safe"):
        score += 1
    hard_block = bool(reason_codes)
    if hard_block:
        decision = "DEFER_TO_FINAL" if has_correct_candidate and "FINAL_RESERVE_UNSAFE" in reason_codes else "BLOCK"
    elif score >= 4:
        decision = "ALLOW"
    elif score >= 1 and not has_correct_candidate:
        decision = "ALLOW"
    else:
        decision = "BLOCK"
    expected = "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW" if score >= 1 else "NONE"
    record = {"schema_version": CONTINUATION_DECISION_SCHEMA, "run_id": run_id, "round_index": int(round_index), "mode": mode, "policy_mode": policy_mode, "decision": decision, "current_state": {"has_correct_candidate": bool(has_correct_candidate), "has_strict_latency_improvement": bool(has_strict_latency_improvement), "has_performance_area_improvement": bool(performance_area.get("performance_area_proxy_improved")), "best_candidate_id": None, "remaining_rounds": int(remaining_rounds)}, "evidence_delta": dict(delta), "strategy_state": dict(strategies), "cost": dict(cost), "performance_area_state": {key: performance_area.get(key) for key in ("latency_improved", "transaction_interval_improved", "clock_improved", "area_proxy_improved", "performance_area_proxy_improved", "resource_pressure_changed", "tradeoff_detected", "pareto_relation") } | {"reason_codes": []}, "value": {"expected_value": expected, "value_score": score, "benefit_reason_codes": benefit, "cost_reason_codes": penalties}, "reason_codes": reason_codes + list(delta.get("reason_codes", [])) + list(strategies.get("reason_codes", [])), "policy_version": CONTINUATION_POLICY_VERSION}
    record["decision_hash"] = canonical_sha256(record)
    return record
