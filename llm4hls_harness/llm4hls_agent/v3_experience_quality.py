"""Deterministic quality gate for bounded V3-E Planner guidance.

The gate owns no Planner, Graph, Candidate, budget or tool authority.  It only
decides whether already retrieved and ranked historical advice is strong enough
to be included in a Planner prompt.  An abstention is deliberately fail-open:
the caller uses the original Planner prompt without experience guidance.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from .v3_experience import (
    EXPERIENCE_GUIDANCE_SCHEMA,
    RetrievalResult,
    estimated_guidance_tokens,
    normalize_strategy_bundle,
    validate_experience_query,
    validate_experience_record,
    validate_guidance,
)


GUIDANCE_QUALITY_SCHEMA = "v3e.guidance-quality-decision.v1"
QUALITY_DECISIONS = frozenset({"INJECT", "ABSTAIN"})


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bundle(value: object) -> tuple[str, ...]:
    return tuple(normalize_strategy_bundle(value))


def _known(value: object) -> str | None:
    text = str(value or "").strip().upper()
    return None if not text or text == "UNKNOWN" else text


def _record_similarity(record: Mapping[str, object]) -> float:
    value = _number(record.get("_similarity"))
    return max(0.0, min(1.0, value if value is not None else 0.0))


def _context_matches(
    query_evidence: Mapping[str, object], record_evidence: Mapping[str, object]
) -> bool:
    """Require an exact known failure type or primary bottleneck match."""

    query_failure = _known(query_evidence.get("failure_type"))
    record_failure = _known(record_evidence.get("failure_type"))
    query_bottleneck = _known(query_evidence.get("primary_bottleneck"))
    record_bottleneck = _known(record_evidence.get("primary_bottleneck"))
    return bool(
        query_failure
        and record_failure
        and query_failure == record_failure
        or query_bottleneck
        and record_bottleneck
        and query_bottleneck == record_bottleneck
    )


def _evidence_conflict(
    evidence: Mapping[str, object], bundle: Sequence[str]
) -> str | None:
    strategies = set(bundle)
    loop_ii = evidence.get("loop_ii")
    pipeline = str(evidence.get("pipeline_status") or "").upper()
    if "LOOP_PIPELINE" in strategies and (
        (isinstance(loop_ii, int) and not isinstance(loop_ii, bool) and loop_ii <= 1)
        or pipeline == "PIPELINED_II_1"
    ):
        return "PIPELINE_ALREADY_ACHIEVED_II_1"
    if strategies.intersection(
        {"ARRAY_PARTITION", "MEMORY_PARTITION", "MEMORY_BANKING"}
    ):
        bottleneck = _known(evidence.get("primary_bottleneck"))
        if (
            evidence.get("memory_bottleneck") is False
            and bottleneck is not None
            and "MEMORY" not in bottleneck
        ):
            return "MEMORY_STRATEGY_CONFLICTS_WITH_CURRENT_BOTTLENECK"
    if strategies.intersection({"DATAFLOW", "STREAMING"}) and (
        evidence.get("has_dataflow") is False
        and evidence.get("has_stream") is False
        and evidence.get("has_fifo") is False
        and _known(evidence.get("failure_type")) not in {
            "DEADLOCK",
            "TIMEOUT",
            "RTL_MISMATCH",
        }
    ):
        return "STRUCTURAL_STRATEGY_HAS_NO_CURRENT_STRUCTURAL_EVIDENCE"
    return None


def _historical_success(record: Mapping[str, object], mode: str) -> bool:
    outcome = _mapping(record.get("outcome"))
    if mode == "REPAIR":
        return outcome.get("final_pass") is True
    if mode == "SYNTH_FIX":
        return outcome.get("csim_pass") is True and outcome.get("synth_pass") is True
    if mode == "STRUCTURAL_FIX":
        return (
            outcome.get("csim_pass") is True
            and outcome.get("cosim_status") == "PASS"
            and outcome.get("final_pass") is not False
        )
    acceleration = _number(outcome.get("acceleration"))
    return bool(
        outcome.get("csim_pass") is True
        and outcome.get("synth_pass") is True
        and outcome.get("cosim_status") != "FAIL"
        and acceleration is not None
        and acceleration > 1.0
    )


@dataclass(frozen=True)
class GuidanceQualityConfig:
    min_support_count: int = 2
    min_similarity: float = 0.50
    min_similarity_mean: float = 0.45
    # The existing ranker maps two semantic samples to confidence 0.25
    # (sample_count / 8).  The default therefore permits the required minimum
    # of two strong supports while still abstaining on a single record.
    min_confidence: float = 0.25
    max_prompt_tokens: int = 600

    def __post_init__(self) -> None:
        if self.min_support_count < 2:
            raise ValueError("min_support_count must be at least 2")
        for name in ("min_similarity", "min_similarity_mean", "min_confidence"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.max_prompt_tokens < 200:
            raise ValueError("max_prompt_tokens is too small for safe guidance")


@dataclass(frozen=True)
class GuidanceQualityResult:
    decision: dict[str, object]
    prompt_guidance: dict[str, object]

    @property
    def injectable(self) -> bool:
        return self.decision.get("decision") == "INJECT"


def validate_quality_decision(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version",
        "decision",
        "confidence",
        "support_count",
        "similarity_max",
        "similarity_mean",
        "recommended_strategies",
        "discouraged_strategies",
        "estimated_success",
        "estimated_gain",
        "estimated_cost",
        "reason",
        "abstain_reason",
        "supporting_record_ids",
        "guidance_tokens",
        "prompt_token_limit",
    }
    if set(value) != required:
        raise ValueError("guidance quality decision fields mismatch")
    if value.get("schema_version") != GUIDANCE_QUALITY_SCHEMA:
        raise ValueError("unsupported guidance quality schema")
    decision = value.get("decision")
    if decision not in QUALITY_DECISIONS:
        raise ValueError("guidance quality decision must be INJECT or ABSTAIN")
    confidence = _number(value.get("confidence"))
    similarity_max = _number(value.get("similarity_max"))
    similarity_mean = _number(value.get("similarity_mean"))
    if confidence is None or not 0 <= confidence <= 1:
        raise ValueError("quality confidence must be in [0,1]")
    for name, number in (
        ("similarity_max", similarity_max),
        ("similarity_mean", similarity_mean),
    ):
        if number is not None and not 0 <= number <= 1:
            raise ValueError(f"{name} must be null or in [0,1]")
    for name in ("support_count", "guidance_tokens", "prompt_token_limit"):
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if not isinstance(value.get("estimated_cost"), Mapping):
        raise ValueError("estimated_cost must be an object")
    recommended = value.get("recommended_strategies")
    discouraged = value.get("discouraged_strategies")
    if not isinstance(recommended, list) or not isinstance(discouraged, list):
        raise ValueError("quality strategies must be lists")
    normalized_recommended = [normalize_strategy_bundle(item) for item in recommended]
    normalized_discouraged = [normalize_strategy_bundle(item) for item in discouraged]
    supporting = value.get("supporting_record_ids")
    if not isinstance(supporting, list) or any(
        not isinstance(item, str) or len(item) != 64 for item in supporting
    ):
        raise ValueError("quality supporting record IDs are invalid")
    reason = value.get("reason")
    abstain = value.get("abstain_reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError("quality reason must be non-empty")
    if abstain is not None and (not isinstance(abstain, str) or not abstain):
        raise ValueError("abstain_reason must be null or non-empty text")
    if decision == "INJECT" and abstain is not None:
        raise ValueError("INJECT cannot have an abstain_reason")
    copied = json.loads(json.dumps(dict(value), sort_keys=True))
    copied["recommended_strategies"] = normalized_recommended
    copied["discouraged_strategies"] = normalized_discouraged
    return copied


class GuidanceQualityGate:
    """Choose INJECT or ABSTAIN from frozen retrieval/ranking evidence."""

    def __init__(self, config: GuidanceQualityConfig | None = None) -> None:
        self.config = config or GuidanceQualityConfig()

    @staticmethod
    def _compact_ranked_bundle(value: Mapping[str, object]) -> dict[str, object]:
        compact: dict[str, object] = {
            "strategy_bundle": normalize_strategy_bundle(
                value.get("strategy_bundle")
            )
        }
        for name in (
            "posterior_success",
            "normalized_expected_gain",
            "average_acceleration",
            "average_tokens",
            "average_credits",
            "average_wall_time_seconds",
        ):
            if value.get(name) is not None:
                compact[name] = value[name]
        return compact

    @staticmethod
    def _empty_guidance(reason: str, confidence: float) -> dict[str, object]:
        return validate_guidance(
            {
                "schema_version": EXPERIENCE_GUIDANCE_SCHEMA,
                "similar_successes": [],
                "similar_failures": [],
                "recommended_strategy_bundles": [],
                "discouraged_strategy_bundles": [],
                "confidence": round(confidence, 6),
                "supporting_record_ids": [],
                "fallback_reason": reason,
                "notice": "Experience abstained; use current evidence and the original Planner.",
            }
        )

    def _prompt_guidance(
        self,
        recommended: Sequence[Mapping[str, object]],
        discouraged: Sequence[Mapping[str, object]],
        *,
        confidence: float,
        token_limit: int,
    ) -> dict[str, object] | None:
        compact_recommended = [
            self._compact_ranked_bundle(item) for item in recommended[:1]
        ]
        compact_discouraged = [
            {"strategy_bundle": normalize_strategy_bundle(item.get("strategy_bundle"))}
            for item in discouraged[:1]
        ]
        guidance: dict[str, object] = {
            "schema_version": EXPERIENCE_GUIDANCE_SCHEMA,
            "similar_successes": [],
            "similar_failures": [],
            "recommended_strategy_bundles": compact_recommended,
            "discouraged_strategy_bundles": compact_discouraged,
            "confidence": round(confidence, 6),
            "supporting_record_ids": [],
            "fallback_reason": None,
            "notice": "Advisory only; current evidence and harness gates are authoritative.",
        }
        # Trim cost details before dropping a useful recommendation.
        optional = (
            "average_wall_time_seconds",
            "average_tokens",
            "average_credits",
            "average_acceleration",
            "normalized_expected_gain",
        )
        for field in (None, *optional):
            candidate = validate_guidance(guidance)
            if estimated_guidance_tokens(candidate) <= token_limit:
                return candidate
            if field is not None and compact_recommended:
                compact_recommended[0].pop(field, None)
        if compact_discouraged:
            compact_discouraged.clear()
            candidate = validate_guidance(guidance)
            if estimated_guidance_tokens(candidate) <= token_limit:
                return candidate
        return None

    def evaluate(
        self,
        query: Mapping[str, object],
        retrieval: RetrievalResult,
        ranking: Mapping[str, object],
        *,
        prompt_token_limit: int | None = None,
    ) -> GuidanceQualityResult:
        validated_query = validate_experience_query(query)
        token_limit = int(
            self.config.max_prompt_tokens
            if prompt_token_limit is None
            else prompt_token_limit
        )
        if token_limit < 200:
            raise ValueError("prompt_token_limit is too small for safe guidance")
        query_mode = str(validated_query["mode"])
        query_evidence = _mapping(validated_query["evidence_features"])

        eligible: list[dict[str, object]] = []
        for raw in retrieval.considered:
            clean = {
                key: item
                for key, item in raw.items()
                if not str(key).startswith("_")
            }
            record = validate_experience_record(clean)
            if (
                record["execution_class"] != "REAL_LLM_VITIS"
                or record["eligible_for_ranking"] is not True
                or record["task_split"] != "train"
                or record["mode"] != query_mode
            ):
                continue
            similarity = _record_similarity(raw)
            if similarity < self.config.min_similarity:
                continue
            if not _context_matches(
                query_evidence, _mapping(record["evidence_features"])
            ):
                continue
            annotated = dict(record)
            annotated["_similarity"] = similarity
            eligible.append(annotated)

        attempted = {
            _bundle(item)
            for item in validated_query["attempted_strategy_bundles"]
            if _bundle(item)
        }
        raw_recommended = [
            _mapping(item)
            for item in _sequence(ranking.get("recommended_strategy_bundles"))
            if _bundle(_mapping(item).get("strategy_bundle"))
        ]
        raw_discouraged = [
            _mapping(item)
            for item in _sequence(ranking.get("discouraged_strategy_bundles"))
            if _bundle(_mapping(item).get("strategy_bundle"))
        ]
        bundle_history: dict[tuple[str, ...], list[dict[str, object]]] = {}
        for item in eligible:
            bundle = _bundle(
                _mapping(item["proposal_features"]).get("strategy_bundle")
            )
            if bundle:
                bundle_history.setdefault(bundle, []).append(item)
        # Historical failure discourages a bundle only when no matching
        # semantic attempt succeeded.  A mixed history is uncertainty, not a
        # contradiction.  Current-run attempted bundles remain hard-suppressed.
        failed_bundles = {
            bundle
            for bundle, items in bundle_history.items()
            if items and not any(_historical_success(item, query_mode) for item in items)
        }
        recommended = [
            item
            for item in raw_recommended
            if _bundle(item.get("strategy_bundle")) not in attempted
            and not (
                _bundle(item.get("strategy_bundle")) in failed_bundles
                and int(item.get("successes") or 0) == 0
            )
        ]
        discouraged_keys = {
            _bundle(item.get("strategy_bundle")) for item in raw_discouraged
        } | failed_bundles | attempted
        discouraged_map: dict[tuple[str, ...], Mapping[str, object]] = {
            _bundle(item.get("strategy_bundle")): item for item in raw_discouraged
        }
        for bundle in sorted(discouraged_keys):
            discouraged_map.setdefault(bundle, {"strategy_bundle": list(bundle)})
        discouraged = list(discouraged_map.values())[:3]

        abstain_reason: str | None = None
        top: Mapping[str, object] | None = recommended[0] if recommended else None
        support: list[dict[str, object]] = []
        if top is None:
            abstain_reason = (
                "CURRENT_STRATEGY_ALREADY_FAILED_OR_ATTEMPTED"
                if raw_recommended
                else "NO_RECOMMENDED_STRATEGY"
            )
        else:
            top_bundle = _bundle(top.get("strategy_bundle"))
            top_ids = set(str(item) for item in _sequence(top.get("supporting_record_ids")))
            support = [
                item
                for item in eligible
                if str(item["record_id"]) in top_ids
                and _bundle(_mapping(item["proposal_features"]).get("strategy_bundle"))
                == top_bundle
            ]
            conflict = _evidence_conflict(query_evidence, top_bundle)
            if conflict is not None:
                abstain_reason = conflict
            elif top_bundle in discouraged_keys:
                abstain_reason = "RECOMMENDATION_CONFLICTS_WITH_FAILURE_HISTORY"
            elif len(support) < self.config.min_support_count:
                abstain_reason = "INSUFFICIENT_SUPPORT"
            elif int(top.get("successes") or 0) <= 0:
                abstain_reason = "NO_SUCCESSFUL_STRATEGY_SUPPORT"

        similarities = [_record_similarity(item) for item in support]
        similarity_max = max(similarities) if similarities else None
        similarity_mean = (
            sum(similarities) / len(similarities) if similarities else None
        )
        rank_confidence = _number(ranking.get("confidence")) or 0.0
        confidence = min(
            rank_confidence,
            similarity_mean if similarity_mean is not None else 0.0,
            min(1.0, len(support) / max(1, self.config.min_support_count)),
        )
        if abstain_reason is None and (
            similarity_max is None
            or similarity_max < self.config.min_similarity
            or similarity_mean is None
            or similarity_mean < self.config.min_similarity_mean
        ):
            abstain_reason = "SIMILARITY_BELOW_QUALITY_THRESHOLD"
        if abstain_reason is None and confidence < self.config.min_confidence:
            abstain_reason = "CONFIDENCE_BELOW_QUALITY_THRESHOLD"

        prompt_guidance: dict[str, object]
        if abstain_reason is None:
            built = self._prompt_guidance(
                recommended,
                discouraged,
                confidence=confidence,
                token_limit=token_limit,
            )
            if built is None:
                abstain_reason = "PROMPT_TOKEN_LIMIT"
                prompt_guidance = self._empty_guidance(
                    abstain_reason, confidence
                )
            else:
                prompt_guidance = built
        else:
            prompt_guidance = self._empty_guidance(abstain_reason, confidence)

        decision_name = "ABSTAIN" if abstain_reason else "INJECT"
        top = recommended[0] if recommended else {}
        decision = validate_quality_decision(
            {
                "schema_version": GUIDANCE_QUALITY_SCHEMA,
                "decision": decision_name,
                "confidence": round(confidence, 6),
                "support_count": len(support),
                "similarity_max": (
                    round(similarity_max, 6) if similarity_max is not None else None
                ),
                "similarity_mean": (
                    round(similarity_mean, 6) if similarity_mean is not None else None
                ),
                "recommended_strategies": [
                    list(_bundle(item.get("strategy_bundle")))
                    for item in recommended[:3]
                ],
                "discouraged_strategies": [
                    list(_bundle(item.get("strategy_bundle")))
                    for item in discouraged[:3]
                    if _bundle(item.get("strategy_bundle"))
                ],
                "estimated_success": _number(top.get("posterior_success")),
                "estimated_gain": _number(top.get("normalized_expected_gain")),
                "estimated_cost": {
                    "tokens": _number(top.get("average_tokens")),
                    "credits": _number(top.get("average_credits")),
                    "wall_time_seconds": _number(
                        top.get("average_wall_time_seconds")
                    ),
                },
                "reason": (
                    "HIGH_QUALITY_MATCHED_REAL_EXPERIENCE"
                    if decision_name == "INJECT"
                    else "EXPERIENCE_NOT_SAFE_OR_STRONG_ENOUGH_TO_INJECT"
                ),
                "abstain_reason": abstain_reason,
                "supporting_record_ids": sorted(
                    str(item["record_id"]) for item in support
                )[:8],
                # ABSTAIN guidance is retained only as a run-local audit
                # envelope.  The Planner adapter treats it as non-actionable,
                # so it contributes zero tokens to the actual model prompt.
                "guidance_tokens": (
                    estimated_guidance_tokens(prompt_guidance)
                    if decision_name == "INJECT"
                    else 0
                ),
                "prompt_token_limit": token_limit,
            }
        )
        return GuidanceQualityResult(decision, prompt_guidance)


__all__ = [
    "GUIDANCE_QUALITY_SCHEMA",
    "GuidanceQualityConfig",
    "GuidanceQualityGate",
    "GuidanceQualityResult",
    "validate_quality_decision",
]
