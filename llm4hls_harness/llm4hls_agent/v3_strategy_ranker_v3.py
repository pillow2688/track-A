"""Family-calibrated Bayesian strategy ranking with conservative abstention.

V3 fixes two unsafe properties of the offline V2 prototype:

* only records with a real final PASS/FAIL label are evidence;
* repeated Candidates from one task family contribute one Bernoulli sample.

The ranker remains a pure advisory function.  Prompt injection is authorized
only by the separately verified runtime admission manifest.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

from .v3_experience_kb import validate_kb_query
from .v3_experience_v2 import STRATEGIES_BY_MODE, validate_experience_v2


STRATEGY_RANKER_V3_SCHEMA = "v3e.bayesian-strategy-ranker.v3"

_LOW_CONFIDENCE_REASONS = frozenset(
    {
        "DECLARED_STRATEGY_FALLBACK",
        "UNCLASSIFIED_PATCH_FALLBACK",
        "PATCH_TEXT_UNAVAILABLE",
    }
)
_GENERIC_OTHER_BY_CELL = {
    ("REPAIR", "FUNCTIONAL_MISMATCH_OTHER"): "OTHER_FUNCTIONAL_REPAIR",
    ("SYNTH_FIX", "SYNTHESIS_ERROR_OTHER"): "OTHER_SYNTHESIS_REPAIR",
    ("STRUCTURAL_FIX", "STRUCTURAL_ERROR_OTHER"): "OTHER_STRUCTURAL_REPAIR",
}


def _subtype_from_query(query: Mapping[str, object]) -> str:
    return str(
        query["bottleneck_subtype"]
        if query["mode"] == "OPTIMIZE"
        else query["failure_subtype"]
    )


def _subtype_from_record(record: Mapping[str, object]) -> str:
    problem = record["problem"]
    assert isinstance(problem, Mapping)
    return str(
        problem["bottleneck_subtype"]
        if problem["mode"] == "OPTIMIZE"
        else problem["failure_subtype"]
    )


def verified_success(record: Mapping[str, object]) -> bool | None:
    """Return the final binary label, or None when final audit is absent."""

    problem = record["problem"]
    validation = record["validation"]
    performance = record["performance"]
    assert isinstance(problem, Mapping)
    assert isinstance(validation, Mapping)
    assert isinstance(performance, Mapping)
    final_status = validation["fresh_final_status"]
    if final_status not in {"PASS", "FAIL"}:
        return None
    if final_status == "FAIL":
        return False
    mode = problem["mode"]
    if mode == "SYNTH_FIX":
        return bool(
            validation["csim_status"] == "PASS"
            and validation["synth_status"] == "PASS"
        )
    if mode == "STRUCTURAL_FIX":
        return bool(
            validation["csim_status"] == "PASS"
            and validation["cosim_status"] == "PASS"
        )
    if mode == "OPTIMIZE":
        return bool(
            validation["csim_status"] == "PASS"
            and validation["synth_status"] == "PASS"
            and validation["cosim_status"] != "FAIL"
            and performance["strict_improvement"] is True
        )
    return True


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _semantic_profile_from_query(query: Mapping[str, object]) -> dict[str, object]:
    structure = query["structure_features"]
    assert isinstance(structure, Mapping)
    return {
        "failure_stage": structure.get("failure_stage"),
        "primary_bottleneck": structure.get("primary_bottleneck"),
        "has_dataflow": structure.get("has_dataflow"),
        "has_stream": structure.get("has_stream"),
        "has_fifo": structure.get("has_fifo"),
        "has_reduction": structure.get("has_reduction"),
        "memory_access_pattern": structure.get("memory_access_pattern"),
        "resource_pressure": structure.get("resource_pressure"),
    }


def _semantic_profile_from_record(record: Mapping[str, object]) -> dict[str, object]:
    problem = record["problem"]
    structure = record["structure_features"]
    assert isinstance(problem, Mapping)
    assert isinstance(structure, Mapping)
    return {
        "failure_stage": problem.get("failure_stage"),
        "primary_bottleneck": problem.get("primary_bottleneck"),
        "has_dataflow": structure.get("has_dataflow"),
        "has_stream": structure.get("has_stream"),
        "has_fifo": structure.get("has_fifo"),
        "has_reduction": structure.get("has_reduction"),
        "memory_access_pattern": structure.get("memory_access_pattern"),
        "resource_pressure": structure.get("resource_pressure"),
    }


def _known(value: object) -> bool:
    return value not in (None, "", "UNKNOWN", {})


def _profile_compatible(
    query_profile: Mapping[str, object], record_profile: Mapping[str, object]
) -> bool:
    """Require all known high-signal query facts to agree.

    Structural booleans are used only when both sides provide them.  This is a
    conservative hierarchical backoff, not an unconditional mode/subtype
    fallback.
    """

    compared = 0
    for name in ("failure_stage", "primary_bottleneck"):
        left = query_profile.get(name)
        right = record_profile.get(name)
        if _known(left) and _known(right):
            compared += 1
            if left != right:
                return False
    structural_matches = 0
    for name in (
        "has_dataflow",
        "has_stream",
        "has_fifo",
        "has_reduction",
        "memory_access_pattern",
    ):
        left = query_profile.get(name)
        right = record_profile.get(name)
        if _known(left) and _known(right):
            if left == right:
                structural_matches += 1
    # Boolean structure is supporting context, not an exact identity.  A
    # successful strategy may intentionally change pipeline/dataflow/stream
    # flags, so treating every mismatch as a hard exclusion would condition on
    # the post-Patch shape and collapse each family into a singleton.
    return compared >= 1 or structural_matches >= 2


def _normalization_is_safe(
    record: Mapping[str, object], atom: str
) -> bool:
    strategy = record["strategy"]
    assert isinstance(strategy, Mapping)
    confidence = _finite(strategy.get("strategy_normalization_confidence"))
    reasons = set(strategy.get("normalization_reason_codes") or [])
    return bool(
        confidence is not None
        and confidence >= 0.65
        and not atom.startswith("OTHER_")
        and not reasons.intersection(_LOW_CONFIDENCE_REASONS)
    )


@dataclass(frozen=True)
class BayesianStrategyRankerV3Config:
    alpha: float = 1.0
    beta: float = 1.0
    minimum_task_families: int = 3
    minimum_success_families: int = 2
    minimum_patch_digests: int = 2
    minimum_posterior: float = 0.65
    minimum_lower_bound: float = 0.45
    minimum_margin: float = 0.05
    lower_bound_z: float = 1.2815515655446004

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("Beta prior parameters must be positive")
        for name in (
            "minimum_task_families",
            "minimum_success_families",
            "minimum_patch_digests",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        for name in (
            "minimum_posterior",
            "minimum_lower_bound",
            "minimum_margin",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.lower_bound_z <= 0:
            raise ValueError("lower_bound_z must be positive")


class BayesianStrategyRankerV3:
    """Rank high-confidence atoms using independent task-family evidence."""

    def __init__(self, config: BayesianStrategyRankerV3Config | None = None) -> None:
        self.config = config or BayesianStrategyRankerV3Config()

    def _eligible(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict[str, object]], int]:
        mode = str(query["mode"])
        subtype = _subtype_from_query(query)
        output: list[dict[str, object]] = []
        unverified = 0
        for raw in records:
            record = validate_experience_v2(raw)
            source = record["source"]
            problem = record["problem"]
            strategy = record["strategy"]
            provenance = record["provenance"]
            if (
                source["evidence_level"] != "REAL_LLM_VITIS"
                or source["task_split"] not in {"train", "dev"}
                or provenance["eligible_for_ranking"] is not True
                or problem["mode"] != mode
                or _subtype_from_record(record) != subtype
            ):
                continue
            if source["run_id"] == query["current_run_id"]:
                continue
            if (
                query["current_candidate_id"] is not None
                and source["candidate_id"] == query["current_candidate_id"]
            ):
                continue
            if (
                query["current_patch_digest"] is not None
                and strategy["patch_digest"] == query["current_patch_digest"]
            ):
                continue
            if (
                query["exclude_same_task_family"] is True
                and source["task_family_hash"] == query["task_family_hash"]
            ):
                continue
            if verified_success(record) is None:
                unverified += 1
                continue
            output.append(record)
        output.sort(key=lambda item: str(item["record_id"]))
        return output, unverified

    def _samples(
        self,
        *,
        atom: str,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> tuple[list[Mapping[str, object]], str]:
        generic_atom = _GENERIC_OTHER_BY_CELL.get(
            (str(query["mode"]), _subtype_from_query(query))
        )
        if atom == generic_atom:
            # A generic atom may be used only inside its exact generic subtype.
            # It is deliberately not conditioned on noisy post-Patch shape,
            # and _entry additionally requires zero failed families.  The
            # ordinary independent-family, patch-diversity, posterior and
            # lower-bound requirements remain unchanged.
            return (
                [
                    record
                    for record in records
                    if atom in record["strategy"]["observed_strategy_atoms"]
                ],
                "MODE_SUBTYPE_GENERIC_OTHER",
            )
        profile = _semantic_profile_from_query(query)
        safe = [
            record
            for record in records
            if atom in record["strategy"]["observed_strategy_atoms"]
            and _normalization_is_safe(record, atom)
            and _profile_compatible(
                profile, _semantic_profile_from_record(record)
            )
        ]
        exact = [
            record
            for record in safe
            if record["source"]["algorithm_family"] == query["algorithm_family"]
        ]
        exact_families = {
            str(record["source"]["task_family_hash"]) for record in exact
        }
        if len(exact_families) >= self.config.minimum_task_families:
            return exact, "MODE_SUBTYPE_ALGORITHM_PROFILE"
        return safe, "MODE_SUBTYPE_PROFILE_BACKOFF"

    def _entry(
        self,
        *,
        atom: str,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        samples, conditioning = self._samples(
            atom=atom, query=query, records=records
        )
        by_family: dict[str, list[Mapping[str, object]]] = {}
        for record in samples:
            by_family.setdefault(
                str(record["source"]["task_family_hash"]), []
            ).append(record)
        # Any verified failure makes the family a failure.  Repeated successes
        # within one family never multiply confidence.
        family_success = {
            family: all(verified_success(record) is True for record in group)
            for family, group in by_family.items()
        }
        attempts = len(family_success)
        successes = sum(family_success.values())
        failures = attempts - successes
        posterior_alpha = successes + self.config.alpha
        posterior_beta = failures + self.config.beta
        posterior = posterior_alpha / (posterior_alpha + posterior_beta)
        variance = (
            posterior_alpha
            * posterior_beta
            / (
                (posterior_alpha + posterior_beta) ** 2
                * (posterior_alpha + posterior_beta + 1)
            )
        )
        uncertainty = math.sqrt(variance)
        lower_bound = max(
            0.0, posterior - self.config.lower_bound_z * uncertainty
        )
        patches = {
            str(record["strategy"]["patch_digest"])
            for record in samples
            if record["strategy"]["patch_digest"] is not None
        }
        accelerations = [
            number
            for record in samples
            if (number := _finite(record["performance"]["acceleration"]))
            is not None
        ]
        reasons: list[str] = []
        if attempts < self.config.minimum_task_families:
            reasons.append("INSUFFICIENT_INDEPENDENT_FAMILY_SUPPORT")
        if successes < self.config.minimum_success_families:
            reasons.append("INSUFFICIENT_SUCCESS_FAMILY_SUPPORT")
        if len(patches) < self.config.minimum_patch_digests:
            reasons.append("INSUFFICIENT_PATCH_DIVERSITY")
        if conditioning == "MODE_SUBTYPE_GENERIC_OTHER" and failures:
            reasons.append("GENERIC_SUPPORT_HAS_FAILED_FAMILY")
        if posterior < self.config.minimum_posterior:
            reasons.append("POSTERIOR_BELOW_THRESHOLD")
        if lower_bound < self.config.minimum_lower_bound:
            reasons.append("LOWER_BOUND_BELOW_THRESHOLD")
        return {
            "strategy_atom": atom,
            "key": [
                query["mode"],
                _subtype_from_query(query),
                query["algorithm_family"],
                atom,
            ],
            "conditioning": conditioning,
            "attempts": attempts,
            "successes": successes,
            "failures": failures,
            "task_family_count": attempts,
            "success_task_family_count": successes,
            "patch_digest_count": len(patches),
            "posterior_success": round(posterior, 8),
            "uncertainty": round(uncertainty, 8),
            "lower_bound": round(lower_bound, 8),
            "average_acceleration": (
                round(statistics.fmean(accelerations), 8)
                if accelerations
                else None
            ),
            "eligible_for_recommendation": not reasons,
            "ineligibility_reasons": reasons,
            "evidence_ids": sorted(
                str(record["record_id"]) for record in samples
            ),
            "independent_family_labels": {
                family: "PASS" if success else "FAIL"
                for family, success in sorted(family_success.items())
            },
        }

    def rank(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        validated_query = validate_kb_query(query)
        mode = str(validated_query["mode"])
        attempted = {str(item) for item in validated_query["strategy_context"]}
        eligible, unverified = self._eligible(validated_query, records)
        atoms = sorted(
            {
                str(atom)
                for record in eligible
                for atom in record["strategy"]["observed_strategy_atoms"]
                if atom in STRATEGIES_BY_MODE[mode] and atom not in attempted
            }
        )
        entries = [
            self._entry(atom=atom, query=validated_query, records=eligible)
            for atom in atoms
        ]
        entries.sort(
            key=lambda item: (
                -float(item["lower_bound"]),
                -float(item["posterior_success"]),
                -int(item["attempts"]),
                str(item["strategy_atom"]),
            )
        )
        candidates = [
            item
            for item in entries
            if item["eligible_for_recommendation"] is True
        ]
        recommended = candidates[0] if candidates else None
        abstain_reason: str | None = None
        if recommended is None:
            abstain_reason = (
                "NO_SAFE_VERIFIED_STRATEGY"
                if entries
                else "NO_MATCHING_VERIFIED_SUPPORT"
            )
        elif len(candidates) > 1:
            margin = float(recommended["lower_bound"]) - float(
                candidates[1]["lower_bound"]
            )
            if margin < self.config.minimum_margin:
                recommended = None
                abstain_reason = "INSUFFICIENT_TOP_STRATEGY_MARGIN"
        discouraged = [
            item
            for item in entries
            if (
                int(item["attempts"]) >= self.config.minimum_task_families
                and (
                    float(item["posterior_success"]) <= 0.40
                    or int(item["failures"]) >= int(item["successes"])
                )
            )
        ]
        output = {
            "schema_version": STRATEGY_RANKER_V3_SCHEMA,
            "authority": "ADVISORY_ONLY",
            "query_id": validated_query["query_id"],
            "mode": mode,
            "subtype": _subtype_from_query(validated_query),
            "verified_support_record_count": len(eligible),
            "excluded_unverified_record_count": unverified,
            "decision": (
                "RECOMMEND" if recommended is not None else "ABSTAIN"
            ),
            "recommended": recommended,
            "discouraged": discouraged,
            "all_atoms": entries,
            "abstain_reason": abstain_reason,
            "input_contract": {
                "query_contains_outcome": False,
                "support_outcomes_are_historical_only": True,
                "family_level_sampling": True,
                "unverified_labels_excluded": True,
                "prompt_injection_authorized": False,
            },
        }
        return json.loads(json.dumps(output, sort_keys=True))


__all__ = [
    "BayesianStrategyRankerV3",
    "BayesianStrategyRankerV3Config",
    "STRATEGY_RANKER_V3_SCHEMA",
    "verified_success",
]
