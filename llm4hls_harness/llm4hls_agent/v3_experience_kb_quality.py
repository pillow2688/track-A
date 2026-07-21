"""Bayesian atom ranking and conservative guidance gating for v2 experience.

The components in this module are advisory only.  They cannot mutate a
Candidate, approve a tool call, change budget state, or select a final result.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

from .v3_experience_kb import (
    ExplainableRetrievalResult,
    validate_kb_query,
)
from .v3_experience_v2 import (
    BOTTLENECK_TAXONOMY_SCHEMA,
    FAILURE_TAXONOMY_SCHEMA,
    STRATEGY_TAXONOMY_SCHEMA,
    STRATEGIES_BY_MODE,
    validate_experience_v2,
)


KB_RANKING_SCHEMA = "v3e.strategy-atom-ranking.v1"
KB_GUIDANCE_DECISION_SCHEMA = "v3e.guidance-quality-decision.v2"
KB_PROMPT_GUIDANCE_SCHEMA = "v3e.prompt-guidance.v2"


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _atom_success(record: Mapping[str, object]) -> tuple[bool, bool]:
    """Return primary success and higher-grade/final success."""

    problem = record["problem"]
    validation = record["validation"]
    performance = record["performance"]
    assert isinstance(problem, Mapping)
    assert isinstance(validation, Mapping)
    assert isinstance(performance, Mapping)
    mode = problem["mode"]
    final = validation["fresh_final_status"] == "PASS"
    if mode == "REPAIR":
        return final, final
    if mode == "SYNTH_FIX":
        primary = (
            validation["csim_status"] == "PASS"
            and validation["synth_status"] == "PASS"
            and final
        )
        return primary, final
    if mode == "STRUCTURAL_FIX":
        primary = (
            validation["csim_status"] == "PASS"
            and validation["cosim_status"] == "PASS"
            and final
        )
        return primary, final
    primary = (
        validation["csim_status"] == "PASS"
        and validation["synth_status"] == "PASS"
        and validation["cosim_status"] != "FAIL"
        and performance["strict_improvement"] is True
    )
    return primary, primary and final


def _subtype(query: Mapping[str, object]) -> str:
    return str(
        query["bottleneck_subtype"]
        if query["mode"] == "OPTIMIZE"
        else query["failure_subtype"]
    )


def _record_subtype(record: Mapping[str, object]) -> str:
    problem = record["problem"]
    assert isinstance(problem, Mapping)
    return str(
        problem["bottleneck_subtype"]
        if problem["mode"] == "OPTIMIZE"
        else problem["failure_subtype"]
    )


@dataclass(frozen=True)
class BayesianAtomRankerConfig:
    alpha: float = 1.0
    beta: float = 1.0
    recommend_posterior: float = 0.55
    discourage_posterior: float = 0.40
    minimum_algorithm_support: int = 5

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("Beta prior parameters must be positive")
        if not 0 <= self.discourage_posterior <= self.recommend_posterior <= 1:
            raise ValueError("posterior thresholds are invalid")
        if self.minimum_algorithm_support < 2:
            raise ValueError("minimum_algorithm_support must be at least two")


class BayesianAtomRanker:
    """Rank observed strategy atoms with deterministic Beta smoothing."""

    def __init__(self, config: BayesianAtomRankerConfig | None = None) -> None:
        self.config = config or BayesianAtomRankerConfig()

    def rank(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        query = validate_kb_query(query)
        mode = str(query["mode"])
        wanted_subtype = _subtype(query)
        eligible: list[dict[str, object]] = []
        for raw in records:
            record = validate_experience_v2(raw)
            source = record["source"]
            problem = record["problem"]
            provenance = record["provenance"]
            if (
                source["evidence_level"] != "REAL_LLM_VITIS"
                or provenance["eligible_for_ranking"] is not True
                or problem["mode"] != mode
                or _record_subtype(record) != wanted_subtype
            ):
                continue
            eligible.append(record)

        algorithm_records = [
            record
            for record in eligible
            if record["source"]["algorithm_family"] == query["algorithm_family"]
        ]
        use_algorithm_family = len(algorithm_records) >= self.config.minimum_algorithm_support
        ranking_records = algorithm_records if use_algorithm_family else eligible
        by_atom: dict[str, list[dict[str, object]]] = {}
        for record in ranking_records:
            strategy = record["strategy"]
            assert isinstance(strategy, Mapping)
            for atom in strategy["observed_strategy_atoms"]:
                if atom in STRATEGIES_BY_MODE[mode]:
                    by_atom.setdefault(str(atom), []).append(record)

        entries: list[dict[str, object]] = []
        for atom, samples in sorted(by_atom.items()):
            successes = 0
            higher_grade = 0
            accelerations: list[float] = []
            tokens: list[float] = []
            credits: list[float] = []
            walls: list[float] = []
            stats = {
                "attempts": len(samples),
                "patch_valid": 0,
                "candidate_created": 0,
                "csim_pass": 0,
                "synth_pass": 0,
                "strict_improvement": 0,
                "cosim_pass": 0,
                "fresh_final_pass": 0,
                "promoted": 0,
                "no_improvement": 0,
            }
            evidence_ids: list[str] = []
            for record in samples:
                validation = record["validation"]
                performance = record["performance"]
                cost = record["cost"]
                assert isinstance(validation, Mapping)
                assert isinstance(performance, Mapping)
                assert isinstance(cost, Mapping)
                primary, final = _atom_success(record)
                successes += int(primary)
                higher_grade += int(final)
                stats["patch_valid"] += int(validation["patch_valid"] is True)
                stats["candidate_created"] += int(validation["candidate_created"] is True)
                stats["csim_pass"] += int(validation["csim_status"] == "PASS")
                stats["synth_pass"] += int(validation["synth_status"] == "PASS")
                stats["strict_improvement"] += int(performance["strict_improvement"] is True)
                stats["cosim_pass"] += int(validation["cosim_status"] == "PASS")
                stats["fresh_final_pass"] += int(validation["fresh_final_status"] == "PASS")
                stats["promoted"] += int(validation["promoted"] is True)
                stats["no_improvement"] += int(
                    validation["synth_status"] == "PASS"
                    and performance["strict_improvement"] is False
                )
                acceleration = _number(performance["acceleration"])
                if acceleration is not None:
                    accelerations.append(acceleration)
                tokens.append(float(cost["total_tokens"]))
                credits.append(float(cost["credits"]))
                walls.append(float(cost["wall_time_seconds"]))
                evidence_ids.append(str(record["record_id"]))
            attempts = len(samples)
            posterior = (successes + self.config.alpha) / (
                attempts + self.config.alpha + self.config.beta
            )
            variance = (
                (successes + self.config.alpha)
                * (attempts - successes + self.config.beta)
                / (
                    (attempts + self.config.alpha + self.config.beta) ** 2
                    * (attempts + self.config.alpha + self.config.beta + 1)
                )
            )
            uncertainty = math.sqrt(variance)
            confidence = min(1.0, attempts / 8.0) * max(0.0, 1.0 - 2.0 * uncertainty)
            entry: dict[str, object] = {
                "strategy_atom": atom,
                "key": [mode, wanted_subtype, atom],
                "algorithm_family_conditioned": use_algorithm_family,
                **stats,
                "primary_successes": successes,
                "higher_grade_successes": higher_grade,
                "posterior_success": round(posterior, 8),
                "confidence": round(confidence, 8),
                "uncertainty": round(uncertainty, 8),
                "average_acceleration": round(statistics.fmean(accelerations), 8)
                if accelerations
                else None,
                "median_acceleration": round(statistics.median(accelerations), 8)
                if accelerations
                else None,
                "expected_gain": round(
                    statistics.fmean(max(0.0, item - 1.0) for item in accelerations), 8
                )
                if accelerations
                else 0.0,
                "expected_cost": {
                    "tokens": round(statistics.fmean(tokens), 4) if tokens else 0.0,
                    "credits": round(statistics.fmean(credits), 4) if credits else 0.0,
                    "wall_time_seconds": round(statistics.fmean(walls), 4) if walls else 0.0,
                },
                "evidence_ids": sorted(set(evidence_ids)),
            }
            entries.append(entry)

        entries.sort(
            key=lambda item: (
                -float(item["posterior_success"]),
                -float(item["expected_gain"]),
                str(item["strategy_atom"]),
            )
        )
        recommended = [
            item
            for item in entries
            if float(item["posterior_success"]) >= self.config.recommend_posterior
        ]
        discouraged = [
            item
            for item in entries
            if float(item["posterior_success"]) <= self.config.discourage_posterior
            or (
                int(item["no_improvement"]) > 0
                and int(item["no_improvement"]) >= int(item["primary_successes"])
            )
        ]
        support_count = sum(int(item["attempts"]) for item in entries)
        output = {
            "schema_version": KB_RANKING_SCHEMA,
            "taxonomy_versions": {
                "failure": FAILURE_TAXONOMY_SCHEMA,
                "bottleneck": BOTTLENECK_TAXONOMY_SCHEMA,
                "strategy": STRATEGY_TAXONOMY_SCHEMA,
            },
            "query_id": query["query_id"],
            "mode": mode,
            "subtype": wanted_subtype,
            "algorithm_family_conditioned": use_algorithm_family,
            "support_count": support_count,
            "recommended": recommended,
            "discouraged": discouraged,
            "all_atoms": entries,
            "abstain_reason": None if recommended else "NO_POSITIVE_POSTERIOR_SUPPORT",
        }
        return json.loads(json.dumps(output, sort_keys=True))


def _guidance_tokens(value: Mapping[str, object]) -> int:
    compact = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return max(1, math.ceil(len(compact) / 4))


def _evidence_conflict(query: Mapping[str, object], atoms: Sequence[str]) -> str | None:
    features = query["structure_features"]
    assert isinstance(features, Mapping)
    selected = set(atoms)
    if "LOOP_PIPELINE" in selected and (
        features.get("critical_loop_ii") == 1 or features.get("has_pipeline") is True
    ):
        return "PIPELINE_ALREADY_ACHIEVED"
    if selected.intersection({"ARRAY_PARTITION", "MEMORY_PARTITION", "MEMORY_BANKING"}):
        bottleneck = str(query.get("bottleneck_subtype") or "UNKNOWN")
        if features.get("memory_bottleneck") is False and "MEMORY" not in bottleneck:
            return "MEMORY_STRATEGY_CONFLICTS_WITH_EVIDENCE"
    if selected.intersection({"DATAFLOW", "STREAMING"}) and not any(
        features.get(name) is True for name in ("has_dataflow", "has_stream", "has_fifo")
    ):
        return "STRUCTURAL_STRATEGY_WITHOUT_STRUCTURAL_EVIDENCE"
    return None


@dataclass(frozen=True)
class HardenedGuidanceConfig:
    minimum_support: int = 2
    minimum_runs: int = 2
    require_two_task_families: bool = True
    minimum_similarity: float = 0.50
    minimum_similarity_mean: float = 0.45
    minimum_normalization_confidence: float = 0.65
    maximum_family_concentration: float = 0.50
    maximum_run_concentration: float = 0.50
    maximum_prompt_tokens: int = 600

    def __post_init__(self) -> None:
        if self.minimum_support < 2 or self.minimum_runs < 2:
            raise ValueError("guidance requires at least two independent supports")
        for name in (
            "minimum_similarity",
            "minimum_similarity_mean",
            "minimum_normalization_confidence",
            "maximum_family_concentration",
            "maximum_run_concentration",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.maximum_prompt_tokens <= 0 or self.maximum_prompt_tokens > 600:
            raise ValueError("guidance token limit must be in [1,600]")


@dataclass(frozen=True)
class HardenedGuidanceResult:
    decision: dict[str, object]
    prompt_guidance: dict[str, object] | None

    @property
    def injectable(self) -> bool:
        return self.decision["decision"] == "INJECT"


class HardenedGuidanceQualityGate:
    """Inject only independent, compatible, observed real-world support."""

    def __init__(self, config: HardenedGuidanceConfig | None = None) -> None:
        self.config = config or HardenedGuidanceConfig()

    def _result(
        self,
        *,
        decision: str,
        reason: str,
        recommended: Sequence[str] = (),
        discouraged: Sequence[str] = (),
        support: Sequence[Mapping[str, object]] = (),
        similarities: Sequence[float] = (),
        rank_entry: Mapping[str, object] | None = None,
        token_limit: int,
    ) -> HardenedGuidanceResult:
        evidence_ids = sorted(str(item["record_id"]) for item in support)
        prompt: dict[str, object] | None = None
        if decision == "INJECT":
            prompt = {
                "schema_version": KB_PROMPT_GUIDANCE_SCHEMA,
                "recommended_strategy_atoms": list(recommended),
                "discouraged_strategy_atoms": list(discouraged),
                "historical_summary": {
                    "support_count": len(support),
                    "posterior_success": rank_entry.get("posterior_success") if rank_entry else None,
                    "expected_gain": rank_entry.get("expected_gain") if rank_entry else None,
                    "expected_cost": rank_entry.get("expected_cost") if rank_entry else {},
                },
                "supporting_record_ids": evidence_ids,
                "notice": "Historical correlation only; current evidence and all hard gates remain authoritative.",
            }
            while _guidance_tokens(prompt) > token_limit and prompt["supporting_record_ids"]:
                prompt["supporting_record_ids"].pop()
            if _guidance_tokens(prompt) > token_limit:
                return self._result(
                    decision="ABSTAIN",
                    reason="GUIDANCE_TOKEN_LIMIT",
                    token_limit=token_limit,
                )
        tokens = _guidance_tokens(prompt) if prompt is not None else 0
        confidence = float(rank_entry.get("confidence", 0.0)) if rank_entry else 0.0
        quality = {
            "schema_version": KB_GUIDANCE_DECISION_SCHEMA,
            "decision": decision,
            "confidence": round(confidence, 8),
            "support_count": len(support),
            "similarity_max": round(max(similarities), 8) if similarities else None,
            "similarity_mean": round(statistics.fmean(similarities), 8) if similarities else None,
            "recommended_strategy_atoms": list(recommended),
            "discouraged_strategy_atoms": list(discouraged),
            "estimated_success": rank_entry.get("posterior_success") if rank_entry else None,
            "estimated_gain": rank_entry.get("expected_gain") if rank_entry else None,
            "estimated_cost": rank_entry.get("expected_cost") if rank_entry else {},
            "supporting_record_ids": evidence_ids,
            "reason": "INDEPENDENT_COMPATIBLE_SUPPORT" if decision == "INJECT" else "FAIL_OPEN_ORIGINAL_PLANNER",
            "abstain_reason": None if decision == "INJECT" else reason,
            "guidance_tokens": tokens,
            "prompt_token_limit": token_limit,
        }
        return HardenedGuidanceResult(quality, prompt)

    def evaluate(
        self,
        query: Mapping[str, object],
        retrieval: ExplainableRetrievalResult,
        ranking: Mapping[str, object],
        *,
        attempted_strategies: Sequence[str] = (),
        prompt_token_limit: int | None = None,
    ) -> HardenedGuidanceResult:
        query = validate_kb_query(query)
        token_limit = min(
            self.config.maximum_prompt_tokens,
            int(prompt_token_limit or self.config.maximum_prompt_tokens),
        )
        expected_taxonomy = {
            "failure": FAILURE_TAXONOMY_SCHEMA,
            "bottleneck": BOTTLENECK_TAXONOMY_SCHEMA,
            "strategy": STRATEGY_TAXONOMY_SCHEMA,
        }
        if ranking.get("taxonomy_versions") != expected_taxonomy:
            return self._result(decision="ABSTAIN", reason="TAXONOMY_VERSION_MISMATCH", token_limit=token_limit)
        if ranking.get("query_id") != query["query_id"]:
            return self._result(decision="ABSTAIN", reason="RANKING_QUERY_MISMATCH", token_limit=token_limit)
        recommended_entries = ranking.get("recommended")
        if not isinstance(recommended_entries, list) or not recommended_entries:
            return self._result(decision="ABSTAIN", reason="NO_RECOMMENDED_STRATEGY", token_limit=token_limit)
        entry = recommended_entries[0]
        if not isinstance(entry, Mapping):
            return self._result(decision="ABSTAIN", reason="INVALID_RANKING_ENTRY", token_limit=token_limit)
        atom = str(entry.get("strategy_atom") or "")
        attempted = set(attempted_strategies).union(str(item) for item in query["strategy_context"])
        if atom in attempted:
            return self._result(decision="ABSTAIN", reason="STRATEGY_ALREADY_FAILED_OR_ATTEMPTED", token_limit=token_limit)
        conflict = _evidence_conflict(query, [atom])
        if conflict:
            return self._result(decision="ABSTAIN", reason=conflict, token_limit=token_limit)

        supports: list[dict[str, object]] = []
        similarities: list[float] = []
        for case in retrieval.considered:
            record = case.record
            source = record["source"]
            strategy = record["strategy"]
            validation = record["validation"]
            primary_success, _ = _atom_success(record)
            if not primary_success or atom not in strategy["observed_strategy_atoms"]:
                continue
            if _record_subtype(record) != _subtype(query):
                continue
            if source["algorithm_family"] != query["algorithm_family"]:
                continue
            if source["toolchain"] != query["toolchain"]:
                continue
            if query["backend_fingerprint"] != "UNKNOWN" and source["backend_fingerprint"] != query["backend_fingerprint"]:
                continue
            if query["prompt_version"] != "UNKNOWN" and source["prompt_version"] != query["prompt_version"]:
                continue
            if float(strategy["strategy_normalization_confidence"]) < self.config.minimum_normalization_confidence:
                continue
            if validation["fresh_final_status"] != "PASS" and query["mode"] != "OPTIMIZE":
                continue
            similarity = float(case.explanation["similarity"])
            if similarity < self.config.minimum_similarity:
                continue
            supports.append(record)
            similarities.append(similarity)
        if len(supports) < self.config.minimum_support:
            return self._result(decision="ABSTAIN", reason="INSUFFICIENT_SUPPORT", support=supports, similarities=similarities, token_limit=token_limit)
        if statistics.fmean(similarities) < self.config.minimum_similarity_mean:
            return self._result(decision="ABSTAIN", reason="LOW_MEAN_SIMILARITY", support=supports, similarities=similarities, token_limit=token_limit)
        runs = [str(record["source"]["run_id"]) for record in supports]
        families = [str(record["source"]["task_family_hash"]) for record in supports]
        patches = {record["strategy"]["patch_digest"] for record in supports}
        unique_runs = set(runs)
        unique_families = set(families)
        if len(unique_runs) < self.config.minimum_runs:
            return self._result(decision="ABSTAIN", reason="INSUFFICIENT_INDEPENDENT_RUNS", support=supports, similarities=similarities, token_limit=token_limit)
        if max(runs.count(run) for run in unique_runs) / len(runs) > self.config.maximum_run_concentration:
            return self._result(decision="ABSTAIN", reason="RUN_CONCENTRATION_TOO_HIGH", support=supports, similarities=similarities, token_limit=token_limit)
        if self.config.require_two_task_families and len(unique_families) < 2:
            return self._result(decision="ABSTAIN", reason="INSUFFICIENT_TASK_FAMILIES", support=supports, similarities=similarities, token_limit=token_limit)
        if max(families.count(family) for family in unique_families) / len(families) > self.config.maximum_family_concentration:
            return self._result(decision="ABSTAIN", reason="TASK_FAMILY_CONCENTRATION_TOO_HIGH", support=supports, similarities=similarities, token_limit=token_limit)
        if len(patches) < self.config.minimum_support:
            return self._result(decision="ABSTAIN", reason="NEAR_DUPLICATE_PATCH_SUPPORT", support=supports, similarities=similarities, token_limit=token_limit)
        failures = int(entry.get("attempts", 0)) - int(entry.get("primary_successes", 0))
        if failures >= int(entry.get("primary_successes", 0)) and float(entry.get("posterior_success", 0.0)) <= 0.5:
            return self._result(decision="ABSTAIN", reason="SUCCESS_FAILURE_EVIDENCE_CONFLICT", support=supports, similarities=similarities, token_limit=token_limit)
        discouraged = [
            str(item["strategy_atom"])
            for item in ranking.get("discouraged", [])
            if isinstance(item, Mapping) and item.get("strategy_atom") != atom
        ][:3]
        return self._result(
            decision="INJECT",
            reason="INDEPENDENT_COMPATIBLE_SUPPORT",
            recommended=[atom],
            discouraged=discouraged,
            support=supports,
            similarities=similarities,
            rank_entry=entry,
            token_limit=token_limit,
        )


__all__ = [
    "BayesianAtomRanker",
    "BayesianAtomRankerConfig",
    "HardenedGuidanceConfig",
    "HardenedGuidanceQualityGate",
    "HardenedGuidanceResult",
]
