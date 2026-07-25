"""Conservative conditional Bayesian strategy ranking for offline Shadow use.

The ranker is advisory only.  It cannot mutate a Candidate, authorize a tool
call, change a budget, inject a prompt, or select a final result.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

from .v3_experience_kb import validate_kb_query
from .v3_experience_v2 import STRATEGIES_BY_MODE, validate_experience_v2


STRATEGY_RANKER_V2_SCHEMA = "v3e.bayesian-strategy-ranker.v2"


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


def _success(record: Mapping[str, object]) -> bool:
    problem = record["problem"]
    validation = record["validation"]
    performance = record["performance"]
    assert isinstance(problem, Mapping)
    assert isinstance(validation, Mapping)
    assert isinstance(performance, Mapping)
    mode = problem["mode"]
    if mode == "REPAIR":
        return validation["fresh_final_status"] == "PASS"
    if mode == "SYNTH_FIX":
        return bool(
            validation["csim_status"] == "PASS"
            and validation["synth_status"] == "PASS"
            and validation["fresh_final_status"] == "PASS"
        )
    if mode == "STRUCTURAL_FIX":
        return bool(
            validation["csim_status"] == "PASS"
            and validation["cosim_status"] == "PASS"
            and validation["fresh_final_status"] == "PASS"
        )
    return bool(
        validation["csim_status"] == "PASS"
        and validation["synth_status"] == "PASS"
        and validation["cosim_status"] != "FAIL"
        and performance["strict_improvement"] is True
        and validation["fresh_final_status"] == "PASS"
    )


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class BayesianStrategyRankerV2Config:
    """Fixed pre-evaluation thresholds for the conservative Shadow policy."""

    alpha: float = 1.0
    beta: float = 1.0
    minimum_attempts: int = 3
    minimum_runs: int = 3
    minimum_task_families: int = 2
    minimum_patch_digests: int = 2
    algorithm_condition_minimum_attempts: int = 4
    algorithm_condition_minimum_families: int = 2
    minimum_posterior: float = 0.65
    minimum_lower_bound: float = 0.45
    minimum_margin: float = 0.05
    lower_bound_z: float = 1.2815515655446004

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("Beta prior parameters must be positive")
        for name in (
            "minimum_attempts",
            "minimum_runs",
            "minimum_task_families",
            "minimum_patch_digests",
            "algorithm_condition_minimum_attempts",
            "algorithm_condition_minimum_families",
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


class BayesianStrategyRankerV2:
    """Rank strategy atoms with conditional support and safe abstention."""

    def __init__(self, config: BayesianStrategyRankerV2Config | None = None) -> None:
        self.config = config or BayesianStrategyRankerV2Config()

    def _eligible(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        mode = str(query["mode"])
        subtype = _subtype_from_query(query)
        output: list[dict[str, object]] = []
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
            output.append(record)
        output.sort(key=lambda item: str(item["record_id"]))
        return output

    def _entry(
        self,
        *,
        atom: str,
        mode: str,
        subtype: str,
        query_algorithm_family: str,
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        exact = [
            record
            for record in records
            if record["source"]["algorithm_family"] == query_algorithm_family
            and atom in record["strategy"]["observed_strategy_atoms"]
        ]
        exact_families = {
            str(record["source"]["task_family_hash"]) for record in exact
        }
        algorithm_conditioned = bool(
            len(exact) >= self.config.algorithm_condition_minimum_attempts
            and len(exact_families)
            >= self.config.algorithm_condition_minimum_families
        )
        samples = (
            exact
            if algorithm_conditioned
            else [
                record
                for record in records
                if atom in record["strategy"]["observed_strategy_atoms"]
            ]
        )
        successes = sum(_success(record) for record in samples)
        attempts = len(samples)
        posterior_alpha = successes + self.config.alpha
        posterior_beta = attempts - successes + self.config.beta
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
        lower_bound = max(0.0, posterior - self.config.lower_bound_z * uncertainty)
        run_ids = {str(record["source"]["run_id"]) for record in samples}
        families = {
            str(record["source"]["task_family_hash"]) for record in samples
        }
        patches = {
            str(record["strategy"]["patch_digest"])
            for record in samples
            if record["strategy"]["patch_digest"] is not None
        }
        success_families = {
            str(record["source"]["task_family_hash"])
            for record in samples
            if _success(record)
        }
        accelerations = [
            number
            for record in samples
            if (number := _finite(record["performance"]["acceleration"])) is not None
        ]
        reasons: list[str] = []
        if attempts < self.config.minimum_attempts:
            reasons.append("INSUFFICIENT_ATTEMPTS")
        if len(run_ids) < self.config.minimum_runs:
            reasons.append("INSUFFICIENT_RUN_DIVERSITY")
        if len(families) < self.config.minimum_task_families:
            reasons.append("INSUFFICIENT_FAMILY_DIVERSITY")
        if len(patches) < self.config.minimum_patch_digests:
            reasons.append("INSUFFICIENT_PATCH_DIVERSITY")
        if len(success_families) < self.config.minimum_task_families:
            reasons.append("SUCCESS_CONCENTRATED_BY_FAMILY")
        if posterior < self.config.minimum_posterior:
            reasons.append("POSTERIOR_BELOW_THRESHOLD")
        if lower_bound < self.config.minimum_lower_bound:
            reasons.append("LOWER_BOUND_BELOW_THRESHOLD")
        return {
            "strategy_atom": atom,
            "key": [mode, subtype, query_algorithm_family, atom],
            "conditioning": (
                "MODE_SUBTYPE_ALGORITHM_FAMILY"
                if algorithm_conditioned
                else "MODE_SUBTYPE_BACKOFF"
            ),
            "attempts": attempts,
            "successes": successes,
            "failures": attempts - successes,
            "run_count": len(run_ids),
            "task_family_count": len(families),
            "success_task_family_count": len(success_families),
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
            "evidence_ids": sorted(str(record["record_id"]) for record in samples),
        }

    def rank(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        validated_query = validate_kb_query(query)
        mode = str(validated_query["mode"])
        subtype = _subtype_from_query(validated_query)
        attempted = {str(item) for item in validated_query["strategy_context"]}
        eligible = self._eligible(validated_query, records)
        atoms = sorted(
            {
                str(atom)
                for record in eligible
                for atom in record["strategy"]["observed_strategy_atoms"]
                if atom in STRATEGIES_BY_MODE[mode] and atom not in attempted
            }
        )
        entries = [
            self._entry(
                atom=atom,
                mode=mode,
                subtype=subtype,
                query_algorithm_family=str(validated_query["algorithm_family"]),
                records=eligible,
            )
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
            item for item in entries if item["eligible_for_recommendation"] is True
        ]
        recommended: dict[str, object] | None = candidates[0] if candidates else None
        abstain_reason: str | None = None
        if recommended is None:
            abstain_reason = (
                "NO_ELIGIBLE_STRATEGY" if entries else "NO_MATCHING_SUPPORT"
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
                float(item["posterior_success"]) <= 0.40
                or int(item["failures"]) >= int(item["successes"])
            )
        ]
        output = {
            "schema_version": STRATEGY_RANKER_V2_SCHEMA,
            "authority": "SHADOW",
            "query_id": validated_query["query_id"],
            "mode": mode,
            "subtype": subtype,
            "support_record_count": len(eligible),
            "decision": "RECOMMEND" if recommended is not None else "ABSTAIN",
            "recommended": recommended,
            "discouraged": discouraged,
            "all_atoms": entries,
            "abstain_reason": abstain_reason,
            "input_contract": {
                "query_contains_outcome": False,
                "support_outcomes_are_historical_only": True,
                "prompt_injection_authorized": False,
            },
        }
        return json.loads(json.dumps(output, sort_keys=True))


__all__ = [
    "BayesianStrategyRankerV2",
    "BayesianStrategyRankerV2Config",
    "STRATEGY_RANKER_V2_SCHEMA",
]
