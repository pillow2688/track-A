"""Deterministic feature, retrieval and advisory components for V3-E.

Nothing in this module can approve a Patch, tool call, Candidate promotion or
final result.  Its output is bounded historical guidance for the existing
Planner and trace/report path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import (
    CONTINUE_ADVISORY_SCHEMA,
    EXPERIENCE_QUERY_SCHEMA,
    EXPERIENCE_SCHEMA,
    RISK_ADVISORY_SCHEMA,
    ExperienceSnapshot,
    GuidanceResult,
    RetrievalResult,
    canonical_json,
    canonical_sha256,
    changed_patch_lines,
    normalize_strategy_bundle,
    normalized_patch_hash,
    numeric_bucket,
    opaque_hash,
    validate_experience_query,
    validate_experience_record,
    validate_guidance,
)
from .v3_experience_quality import GuidanceQualityGate, validate_quality_decision


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return converted
    return {}


def _sequence(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _integer(value: object) -> int | None:
    result = _number(value)
    return None if result is None else int(result)


def _nested_number(value: object, *preferred: str) -> float | None:
    direct = _number(value)
    if direct is not None:
        return direct
    data = _mapping(value)
    for name in preferred:
        result = _number(data.get(name))
        if result is not None:
            return result
    for child in data.values():
        result = _number(child)
        if result is not None:
            return result
    return None


def _bounded_words(value: object, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    text = " ".join(str(value).strip().split())
    if not text:
        return default
    return text[:160]


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _category(value: object, *, default: str) -> str:
    text = re.sub(r"[^A-Z0-9_]+", "_", _enum_text(value).strip().upper()).strip("_")
    return text[:160] or default


def _safe_failure_type(evidence: Mapping[str, object]) -> str | None:
    for name in ("failure_kind", "failure_type", "failure_stage", "kind"):
        value = _bounded_words(evidence.get(name))
        if value:
            return re.sub(r"[^A-Z0-9_]+", "_", value.upper()).strip("_")[:80]
    if evidence.get("deadlock") is True:
        return "DEADLOCK"
    if evidence.get("timeout") is True:
        return "TIMEOUT"
    if evidence.get("rtl_mismatch") is True:
        return "RTL_MISMATCH"
    return None


def _flatten_public_text(value: object, *, limit: int = 5000) -> str:
    """Flatten bounded structured public evidence; never retain the result."""

    pieces: list[str] = []

    def visit(item: object) -> None:
        if sum(len(piece) for piece in pieces) >= limit:
            return
        if isinstance(item, Mapping):
            for key in sorted(item, key=str):
                visit(item[key])
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for child in item[:24]:
                visit(child)
        elif item is not None:
            pieces.append(str(item)[:320])

    visit(value)
    return " ".join(pieces)[:limit].casefold()


def _algorithm_family(description: str, source: str) -> str:
    """Classify from public semantics only; task identity is intentionally absent."""

    text = f"{description}\n{source}".casefold()
    rules = (
        ("CONVOLUTION_STENCIL", ("convolution", "stencil", "kernel window")),
        ("MATRIX_ALGEBRA", ("matrix", "matmul", "gemm")),
        ("DOT_REDUCTION", ("dot product", "dotproduct", "accumulate", "reduction")),
        ("STREAM_PIPELINE", ("hls::stream", "dataflow", "fifo")),
        ("SORT_SEARCH", ("sort", "search", "compare-and-swap")),
        ("CRYPTO_HASH", ("aes", "sha", "cipher", "encrypt")),
        ("VECTOR_ELEMENTWISE", ("vector", "elementwise", "element-wise")),
    )
    for family, tokens in rules:
        if any(token in text for token in tokens):
            return family
    # Source-shape fallback is deterministic and still independent of task ID.
    if re.search(r"\b(?:sum|acc|total)\s*[+]=", source):
        return "DOT_REDUCTION"
    return "GENERIC_HLS"


def _resource_pressure(report: Mapping[str, object], evidence: Mapping[str, object]) -> str:
    top = _mapping(evidence.get("top_level"))
    utilization = _mapping(top.get("utilization_percent")) or _mapping(
        evidence.get("utilization_percent")
    ) or _mapping(report.get("utilization_percent"))
    percentages = [number for number in (_number(item) for item in utilization.values()) if number is not None]
    if not percentages:
        resources = _mapping(top.get("resources")) or _mapping(report.get("resources"))
        available = _mapping(report.get("available_resources"))
        for name, used in resources.items():
            numerator = _number(used)
            denominator = _number(available.get(name))
            if numerator is not None and denominator and denominator > 0:
                percentages.append(100.0 * numerator / denominator)
    if not percentages:
        return "UNKNOWN"
    maximum = max(percentages)
    if maximum >= 80:
        return "HIGH"
    if maximum >= 50:
        return "MEDIUM"
    return "LOW"


def _critical_loop(evidence: Mapping[str, object], report: Mapping[str, object]) -> Mapping[str, object]:
    loops = _sequence(evidence.get("loops")) or _sequence(report.get("loop_evidence"))
    candidates = [_mapping(item) for item in loops if _mapping(item)]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda item: (
            _integer(item.get("latency_cycles")) or 0,
            _integer(item.get("trip_count")) or 0,
            str(item.get("loop_id") or item.get("name") or ""),
        ),
    )


class ExperienceFeatureExtractor:
    """Fixed schema-v1 feature extraction from public run artifacts."""

    def evidence_features(
        self,
        *,
        source: str,
        failure_evidence: object = None,
        synth_report: object = None,
        synth_evidence: object = None,
    ) -> dict[str, object]:
        failure = _mapping(failure_evidence)
        report = _mapping(synth_report)
        evidence = _mapping(synth_evidence)
        top = _mapping(evidence.get("top_level"))
        loop = _critical_loop(evidence, report)
        loop_ii = _integer(loop.get("pipeline_ii"))
        trip_count = _integer(loop.get("trip_count"))
        latency = _nested_number(
            top.get("latency") or report.get("latency"), "max", "worst", "value"
        )
        interval = _nested_number(
            top.get("transaction_interval")
            or evidence.get("top_transaction_interval")
            or report.get("interval"),
            "max",
            "worst",
            "value",
        )
        public_text = " ".join(
            (
                source.casefold(),
                _flatten_public_text(failure),
                _flatten_public_text(evidence),
                _flatten_public_text(report),
            )
        )
        memory_tokens = (
            "memory port",
            "memory scheduling",
            "memory_scheduling",
            "limited memory",
            "unable to schedule load",
            "unable to schedule store",
            "bank conflict",
        )
        if loop_ii is not None and loop_ii <= 1:
            pipeline = "PIPELINED_II_1"
        elif loop_ii is not None:
            pipeline = "PIPELINED_II_GT_1"
        elif "pipeline" in public_text:
            pipeline = "PIPELINE_PRESENT_II_UNKNOWN"
        else:
            pipeline = "NOT_EVIDENCED"

        primary = _bounded_words(
            failure.get("primary_bottleneck")
            or evidence.get("primary_bottleneck")
            or report.get("primary_bottleneck")
        )
        if primary is None:
            if failure.get("deadlock") is True or "deadlock" in public_text:
                primary = "STREAM_OR_DATAFLOW_DEADLOCK"
            elif any(token in public_text for token in memory_tokens):
                primary = "MEMORY_SCHEDULING"
            elif loop_ii is not None and loop_ii > 1:
                primary = "LOOP_PIPELINE_II"
        elif primary:
            primary = _category(primary, default="UNKNOWN")

        return {
            "failure_type": _safe_failure_type(failure),
            "primary_bottleneck": primary,
            "loop_ii": loop_ii,
            "trip_count_bucket": numeric_bucket(trip_count),
            "transaction_interval_bucket": numeric_bucket(interval),
            "latency_bucket": numeric_bucket(latency),
            "pipeline_status": pipeline,
            "has_dataflow": bool(re.search(r"(?:#\s*pragma\s+hls\s+dataflow|\bdataflow\b)", public_text)),
            "has_stream": "hls::stream" in public_text or "hls stream" in public_text,
            "has_fifo": bool(
                "fifo" in public_text
                or "stream depth" in public_text
                or re.search(r"#\s*pragma\s+hls\s+stream\b", public_text)
            ),
            "has_interface": bool(re.search(r"#\s*pragma\s+hls\s+interface|\bap_(?:axis|fifo|memory)\b", public_text)),
            "has_bitwidth": bool(re.search(r"\b(?:ap_u?int|ap_u?fixed|u?int\d+_t)\b", public_text)),
            "memory_bottleneck": any(token in public_text for token in memory_tokens),
            "resource_pressure": _resource_pressure(report, evidence),
        }

    def build_query(self, **facts: object) -> dict[str, object]:
        source = str(facts.get("source") or "")
        description = str(facts.get("description") or "")
        task_id = str(facts.get("task_id") or "unknown-task")
        run_id = str(facts.get("run_id") or "experience-query")
        candidate_raw = facts.get("candidate_id")
        candidate_id = str(candidate_raw) if candidate_raw else None
        family = _category(
            facts.get("algorithm_family") or _algorithm_family(description, source),
            default="GENERIC_HLS",
        )
        evidence = self.evidence_features(
            source=source,
            failure_evidence=facts.get("failure_evidence"),
            synth_report=facts.get("synth_report"),
            synth_evidence=facts.get("synth_evidence"),
        )
        query: dict[str, object] = {
            "schema_version": EXPERIENCE_QUERY_SCHEMA,
            "query_id": "",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "task_id_hash": opaque_hash(task_id),
            "task_split": _enum_text(facts.get("task_split") or "unknown").lower(),
            "mode": _enum_text(facts.get("mode") or "OPTIMIZE").upper(),
            "difficulty": int(facts.get("difficulty") or 1),
            "algorithm_family": family,
            "evidence_features": evidence,
            "attempted_strategy_bundles": list(
                facts.get("attempted_strategy_bundles") or []
            ),
            "attempted_patch_hashes": list(facts.get("attempted_patch_hashes") or []),
            "remaining_credits": facts.get("remaining_credits"),
            "remaining_tokens": int(facts.get("remaining_tokens") or 0),
            "no_improvement_rounds": int(facts.get("no_improvement_rounds") or 0),
        }
        identity = dict(query)
        identity.pop("query_id")
        query["query_id"] = canonical_sha256(identity)
        return validate_experience_query(query)

    def build_record(self, **facts: object) -> dict[str, object]:
        source = str(facts.get("source") or "")
        patch = str(facts.get("patch") or "")
        run_id = str(facts.get("run_id") or "")
        candidate_id = str(facts.get("candidate_id") or "")
        revision = int(facts.get("revision") or 1)
        task_id = str(facts.get("task_id") or "unknown-task")
        execution_class = _enum_text(
            facts.get("execution_class") or "DETERMINISTIC_FIXTURE"
        ).upper()
        eligible = bool(facts.get("eligible_for_ranking", False))
        family = _category(
            facts.get("algorithm_family")
            or _algorithm_family(str(facts.get("description") or ""), source),
            default="GENERIC_HLS",
        )
        proposal = _mapping(facts.get("proposal"))
        bundle = proposal.get("strategy_bundle") or facts.get("strategy_bundle") or []
        patch_hash = normalized_patch_hash(patch) if patch else facts.get("patch_hash")
        outcome = dict(_mapping(facts.get("outcome")))
        before = _number(outcome.get("latency_before"))
        after = _number(outcome.get("latency_after"))
        if outcome.get("acceleration") is None and before is not None and after and after > 0:
            outcome["acceleration"] = before / after
        defaults: dict[str, object] = {
            "patch_valid": False,
            "candidate_created": False,
            "csim_pass": None,
            "synth_pass": None,
            "cosim_status": "NOT_RUN",
            "final_pass": None,
            "promoted": False,
            "failure_stage": None,
            "latency_before": None,
            "latency_after": None,
            "acceleration": None,
            "tokens": 0,
            "credits": 0,
            "wall_time_seconds": 0.0,
        }
        defaults.update(outcome)
        trajectory_id = canonical_sha256(
            {"run_id": run_id, "candidate_id": candidate_id}
        )
        body: dict[str, object] = {
            "schema_version": EXPERIENCE_SCHEMA,
            "record_id": "",
            "trajectory_id": trajectory_id,
            "revision": revision,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "task_id_hash": opaque_hash(task_id),
            "task_split": _enum_text(facts.get("task_split") or "unknown").lower(),
            "mode": _enum_text(facts.get("mode") or "OPTIMIZE").upper(),
            "difficulty": int(facts.get("difficulty") or 1),
            "algorithm_family": family,
            "execution_class": execution_class,
            "eligible_for_ranking": eligible,
            "evidence_features": self.evidence_features(
                source=f"{source}\n{patch}" if patch else source,
                failure_evidence=facts.get("failure_evidence"),
                synth_report=facts.get("synth_report"),
                synth_evidence=facts.get("synth_evidence"),
            ),
            "proposal_features": {
                "strategy_bundle": normalize_strategy_bundle(bundle),
                "patch_lines": changed_patch_lines(patch)
                if patch
                else int(proposal.get("patch_lines") or 0),
                "planner_risk": _enum_text(
                    _mapping(proposal.get("risk")).get("level")
                    or proposal.get("planner_risk")
                    or facts.get("planner_risk")
                    or "UNKNOWN"
                ).upper(),
                "normalized_patch_hash": patch_hash,
            },
            "outcome": defaults,
            "artifact_refs": list(facts.get("artifact_refs") or []),
        }
        identity = dict(body)
        identity.pop("record_id")
        body["record_id"] = canonical_sha256(identity)
        return validate_experience_record(body)


_BUCKET_ORDER = {
    "UNKNOWN": -1,
    "0": 0,
    "1": 1,
    "2-4": 2,
    "5-16": 3,
    "17-64": 4,
    "65-256": 5,
    "257-1024": 6,
    "1024+": 7,
}


def _bucket_similarity(left: object, right: object) -> float | None:
    a = _BUCKET_ORDER.get(str(left), -1)
    b = _BUCKET_ORDER.get(str(right), -1)
    if a < 0 or b < 0:
        return None
    return max(0.0, 1.0 - abs(a - b) / 4.0)


def _is_success(record: Mapping[str, object]) -> bool:
    mode = record.get("mode")
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
    before = _number(outcome.get("latency_before"))
    after = _number(outcome.get("latency_after"))
    return bool(
        outcome.get("csim_pass") is True
        and outcome.get("synth_pass") is True
        and outcome.get("cosim_status") != "FAIL"
        and before is not None
        and after is not None
        and after < before
    )


class WeightedKNNRetriever:
    def __init__(
        self,
        *,
        allow_cross_mode: bool = False,
        min_similarity: float = 0.30,
        max_successes: int = 3,
        max_failures: int = 2,
    ) -> None:
        self.allow_cross_mode = allow_cross_mode
        self.min_similarity = float(min_similarity)
        self.max_successes = max_successes
        self.max_failures = max_failures

    def similarity(
        self, query: Mapping[str, object], record: Mapping[str, object]
    ) -> float:
        if not self.allow_cross_mode and query.get("mode") != record.get("mode"):
            return 0.0
        q = _mapping(query.get("evidence_features"))
        r = _mapping(record.get("evidence_features"))
        weighted = 0.0
        total = 0.0

        def equality(name: str, weight: float, *, ignore_empty: bool = True) -> None:
            nonlocal weighted, total
            left, right = q.get(name), r.get(name)
            if ignore_empty and (left in (None, "", "UNKNOWN") or right in (None, "", "UNKNOWN")):
                return
            total += weight
            if left == right:
                weighted += weight

        equality("failure_type", 4.0)
        equality("primary_bottleneck", 4.0)
        query_family = query.get("algorithm_family")
        record_family = record.get("algorithm_family")
        if query_family != "GENERIC_HLS" and record_family != "GENERIC_HLS":
            total += 2.0
            if query_family == record_family:
                weighted += 2.0
        equality("loop_ii", 2.0)
        equality("pipeline_status", 1.0)
        equality("resource_pressure", 1.0)
        for name, weight in (
            ("trip_count_bucket", 1.5),
            ("transaction_interval_bucket", 1.5),
            ("latency_bucket", 0.5),
        ):
            score = _bucket_similarity(q.get(name), r.get(name))
            if score is not None:
                total += weight
                weighted += weight * score
        for name in (
            "has_dataflow",
            "has_stream",
            "has_fifo",
            "has_interface",
            "has_bitwidth",
            "memory_bottleneck",
        ):
            # Matching absence is weak evidence; structural presence is useful.
            if q.get(name) is True or r.get(name) is True:
                total += 0.75
                if q.get(name) == r.get(name):
                    weighted += 0.75
        return 0.0 if total <= 0 else round(weighted / total, 8)

    def retrieve(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> RetrievalResult:
        validated_query = validate_experience_query(query)
        attempted_hashes = set(validated_query["attempted_patch_hashes"])
        candidates: list[dict[str, object]] = []
        for raw in sorted(records, key=lambda item: str(item.get("record_id", ""))):
            record = validate_experience_record(raw)
            if not record["eligible_for_ranking"] or record["execution_class"] != "REAL_LLM_VITIS":
                continue
            # Experience consumption is train-only for every evaluation split.
            # This fails closed for unknown/unspecified callers and prevents a
            # caller from relabelling dev/hidden-like trajectories as priors.
            if record["task_split"] != "train":
                continue
            if record["run_id"] == validated_query["run_id"]:
                continue
            if (
                validated_query["candidate_id"]
                and record["run_id"] == validated_query["run_id"]
                and record["candidate_id"] == validated_query["candidate_id"]
            ):
                continue
            patch_hash = _mapping(record["proposal_features"]).get("normalized_patch_hash")
            if patch_hash in attempted_hashes:
                continue
            score = self.similarity(validated_query, record)
            if score < self.min_similarity:
                continue
            annotated = dict(record)
            annotated["_similarity"] = score
            candidates.append(annotated)

        # Select the most similar representative before deduplicating.  This
        # prevents record-id ordering from choosing a weaker Candidate in a run.
        candidates.sort(
            key=lambda item: (-float(item["_similarity"]), str(item["record_id"]))
        )
        scored: list[dict[str, object]] = []
        seen_run: set[str] = set()
        seen_candidate: set[tuple[str, str]] = set()
        seen_patch: set[str] = set()
        for annotated in candidates:
            patch_hash = _mapping(annotated["proposal_features"]).get(
                "normalized_patch_hash"
            )
            candidate_key = (
                str(annotated["run_id"]),
                str(annotated["candidate_id"]),
            )
            if (
                str(annotated["run_id"]) in seen_run
                or candidate_key in seen_candidate
                or (patch_hash is not None and str(patch_hash) in seen_patch)
            ):
                continue
            scored.append(annotated)
            seen_run.add(str(annotated["run_id"]))
            seen_candidate.add(candidate_key)
            if patch_hash is not None:
                seen_patch.add(str(patch_hash))
        successes = tuple(item for item in scored if _is_success(item))[: self.max_successes]
        failures = tuple(item for item in scored if not _is_success(item))[: self.max_failures]
        return RetrievalResult(successes, failures, tuple(scored))


class BayesianStrategyRanker:
    def __init__(
        self,
        *,
        alpha: float = 1.0,
        beta: float = 1.0,
        lambda_credit: float = 0.08,
        lambda_token: float = 0.04,
        lambda_failure: float = 0.10,
    ) -> None:
        if alpha <= 0 or beta <= 0:
            raise ValueError("Beta prior parameters must be positive")
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.lambda_credit = float(lambda_credit)
        self.lambda_token = float(lambda_token)
        self.lambda_failure = float(lambda_failure)

    def posterior_success(self, successes: int, attempts: int) -> float:
        if attempts < successes or successes < 0:
            raise ValueError("invalid Bayesian attempt counts")
        return (successes + self.alpha) / (attempts + self.alpha + self.beta)

    def rank(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        validated_query = validate_experience_query(query)
        groups: dict[
            tuple[str, tuple[str, ...]], list[Mapping[str, object]]
        ] = {}
        for raw in records:
            record = validate_experience_record(
                {key: value for key, value in raw.items() if not str(key).startswith("_")}
            )
            if not record["eligible_for_ranking"] or record["execution_class"] != "REAL_LLM_VITIS":
                continue
            if record["task_split"] != "train":
                continue
            if record["mode"] != validated_query["mode"]:
                continue
            bundle = tuple(_mapping(record["proposal_features"])["strategy_bundle"])
            if bundle:
                evidence = _mapping(record["evidence_features"])
                context = str(
                    evidence.get("primary_bottleneck")
                    or evidence.get("failure_type")
                    or "UNKNOWN"
                )
                groups.setdefault((context, bundle), []).append(record)
        ranked: list[dict[str, object]] = []
        for (context, bundle), items in groups.items():
            semantic_items = [
                item
                for item in items
                if _mapping(item["outcome"]).get("patch_valid") is True
                and _mapping(item["outcome"]).get("candidate_created") is True
            ]
            successes = sum(1 for item in semantic_items if _is_success(item))
            semantic_attempts = len(semantic_items)
            proposal_attempts = len(items)
            probability = self.posterior_success(successes, semantic_attempts)
            outcomes = [_mapping(item["outcome"]) for item in items]
            accelerations = [value for value in (_number(item.get("acceleration")) for item in outcomes) if value is not None]
            tokens = [_number(item.get("tokens")) or 0.0 for item in outcomes]
            credits = [_number(item.get("credits")) or 0.0 for item in outcomes]
            walls = [_number(item.get("wall_time_seconds")) or 0.0 for item in outcomes]
            if validated_query["mode"] == "OPTIMIZE":
                expected_gain = (
                    sum(min(value, 8.0) / 8.0 for value in accelerations)
                    / len(accelerations)
                    if accelerations
                    else 0.0
                )
            else:
                expected_gain = probability
            avg_credit = sum(credits) / proposal_attempts
            avg_token = sum(tokens) / proposal_attempts
            utility = (
                probability * expected_gain
                - self.lambda_credit * min(avg_credit / 25.0, 1.0)
                - self.lambda_token * min(avg_token / 8000.0, 1.0)
                - self.lambda_failure * (1.0 - probability)
            )
            ranked.append(
                {
                    "strategy_bundle": list(bundle),
                    "context": context,
                    "attempts": proposal_attempts,
                    "semantic_attempts": semantic_attempts,
                    "patch_valid": sum(
                        _mapping(item["outcome"]).get("patch_valid") is True
                        for item in items
                    ),
                    "candidate_created": sum(
                        _mapping(item["outcome"]).get("candidate_created") is True
                        for item in items
                    ),
                    "successes": successes,
                    "posterior_success": round(probability, 6),
                    "normalized_expected_gain": round(expected_gain, 6),
                    "average_acceleration": round(sum(accelerations) / len(accelerations), 6) if accelerations else None,
                    "average_tokens": round(avg_token, 2),
                    "average_credits": round(avg_credit, 2),
                    "average_wall_time_seconds": round(
                        sum(walls) / proposal_attempts, 2
                    ),
                    "utility": round(utility, 6),
                    "supporting_record_ids": sorted(str(item["record_id"]) for item in items)[:8],
                }
            )
        ranked.sort(key=lambda item: (-float(item["utility"]), tuple(item["strategy_bundle"])))
        recommended = ranked[:3]
        recommended_keys = {
            (str(item["context"]), tuple(item["strategy_bundle"]))
            for item in recommended
        }
        discouraged = [
            item
            for item in reversed(ranked)
            if (str(item["context"]), tuple(item["strategy_bundle"]))
            not in recommended_keys
        ][:3]
        sample_count = sum(
            _mapping(item["outcome"]).get("patch_valid") is True
            and _mapping(item["outcome"]).get("candidate_created") is True
            for items in groups.values()
            for item in items
        )
        confidence = min(1.0, sample_count / 8.0) if sample_count else 0.0
        supporting = sorted(
            {
                digest
                for item in [*recommended, *discouraged]
                for digest in item["supporting_record_ids"]
            }
        )
        return {
            "recommended_strategy_bundles": recommended,
            "discouraged_strategy_bundles": discouraged,
            "confidence": round(confidence, 6),
            "supporting_record_ids": supporting,
            "fallback_reason": None if ranked else "NO_MATCHING_STRATEGY_HISTORY",
        }


class EmpiricalRiskAdvisor:
    def __init__(self, *, alpha: float = 1.0, beta: float = 1.0) -> None:
        self.alpha = float(alpha)
        self.beta = float(beta)

    def _failure_probability(self, failures: int, attempts: int) -> float:
        return (failures + self.alpha) / (attempts + self.alpha + self.beta)

    def advise(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        validate_experience_query(query)
        statuses = {
            "csim": [],
            "synth": [],
            "cosim": [],
            "final": [],
        }
        supporting: list[str] = []
        for record in records:
            outcome = _mapping(record.get("outcome"))
            if outcome.get("csim_pass") is not None:
                statuses["csim"].append(outcome.get("csim_pass") is False)
            if outcome.get("synth_pass") is not None:
                statuses["synth"].append(outcome.get("synth_pass") is False)
            if outcome.get("cosim_status") in {"PASS", "FAIL"}:
                statuses["cosim"].append(outcome.get("cosim_status") == "FAIL")
            if outcome.get("final_pass") is not None:
                statuses["final"].append(outcome.get("final_pass") is False)
            supporting.append(str(record.get("record_id")))
        probabilities = {
            f"p_{stage}_fail": round(
                self._failure_probability(sum(values), len(values)), 6
            )
            for stage, values in statuses.items()
        }
        return {
            "schema_version": RISK_ADVISORY_SCHEMA,
            **probabilities,
            "sample_counts": {stage: len(values) for stage, values in statuses.items()},
            "supporting_record_ids": sorted(set(supporting))[:8],
            "notice": "Advisory only; hard validation and CoSim policy remain authoritative.",
        }


class HeuristicContinueAdvisor:
    def advise(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
        ranking: Mapping[str, object],
    ) -> dict[str, object]:
        validated = validate_experience_query(query)
        attempted = {
            tuple(bundle) for bundle in validated["attempted_strategy_bundles"]
        }
        untried = [
            item
            for item in _sequence(ranking.get("recommended_strategy_bundles"))
            if tuple(_mapping(item).get("strategy_bundle") or []) not in attempted
        ]
        successes = sum(1 for item in records if _is_success(item))
        historical_rate = (successes + 1.0) / (len(records) + 2.0)
        credits = validated.get("remaining_credits")
        if isinstance(credits, int) and credits < 5:
            decision, reason = "STOP_ADVISORY", "INSUFFICIENT_CANDIDATE_VALIDATION_CREDIT"
        elif validated["remaining_tokens"] <= 0:
            decision, reason = "STOP_ADVISORY", "NO_REMAINING_TOKEN_BUDGET"
        elif validated["no_improvement_rounds"] >= 2 and not untried:
            decision, reason = "STOP_ADVISORY", "NO_UNTRIED_HIGH_VALUE_STRATEGY"
        else:
            decision = "CONTINUE"
            reason = "UNTRIED_RECOMMENDATION_AVAILABLE" if untried else "BUDGET_AND_PROGRESS_ALLOW_EXPLORATION"
        return {
            "schema_version": CONTINUE_ADVISORY_SCHEMA,
            "decision": decision,
            "expected_value": round(historical_rate, 6),
            "reason": reason,
            "untried_recommended_bundles": [
                list(_mapping(item).get("strategy_bundle") or []) for item in untried[:3]
            ],
            "notice": "Advisory only; existing Stop Policy remains authoritative.",
        }


class ExperienceCoordinator:
    """Freeze, retrieve, rank and emit bounded Planner guidance."""

    def __init__(
        self,
        repository: object,
        *,
        feature_extractor: ExperienceFeatureExtractor | None = None,
        retriever: WeightedKNNRetriever | None = None,
        ranker: BayesianStrategyRanker | None = None,
        risk_advisor: EmpiricalRiskAdvisor | None = None,
        continue_advisor: HeuristicContinueAdvisor | None = None,
        quality_gate: GuidanceQualityGate | None = None,
        max_guidance_tokens: int = 600,
        recommendation_path: str | Path | None = None,
    ) -> None:
        if max_guidance_tokens < 200:
            raise ValueError("max_guidance_tokens is too small for guidance schema")
        self.repository = repository
        self.feature_extractor = feature_extractor or ExperienceFeatureExtractor()
        self.retriever = retriever or WeightedKNNRetriever()
        self.ranker = ranker or BayesianStrategyRanker()
        self.risk_advisor = risk_advisor or EmpiricalRiskAdvisor()
        self.continue_advisor = continue_advisor or HeuristicContinueAdvisor()
        self.quality_gate = quality_gate or GuidanceQualityGate()
        self.max_guidance_tokens = int(max_guidance_tokens)
        self.recommendation_path = Path(recommendation_path) if recommendation_path else None
        # One coordinator corresponds to one run-local seed view.  Freezing here
        # keeps repeated Planner prepare()/resume calls action-identical even if
        # another process grows the shared append-only store.
        self._snapshot = self.repository.snapshot()
        self._last_result: GuidanceResult | None = None
        self._last_query: dict[str, object] | None = None
        self._last_quality_decision: dict[str, object] | None = None

    def snapshot_metadata(self) -> dict[str, object]:
        return self._snapshot.to_dict()

    def fingerprint(self) -> dict[str, object]:
        method = getattr(self.repository, "fingerprint", None)
        return (
            dict(method(self._snapshot))
            if callable(method)
            else self._snapshot.to_dict()
        )

    def recommend(
        self,
        query: Mapping[str, object],
        *,
        snapshot: ExperienceSnapshot | None = None,
        prompt_token_limit: int | None = None,
    ) -> GuidanceResult:
        validated = validate_experience_query(query)
        frozen = snapshot or self._snapshot
        records = self.repository.latest_trajectories(frozen, ranking_only=True)
        retrieval = self.retriever.retrieve(validated, records)
        ranking = self.ranker.rank(validated, retrieval.considered)
        risk = self.risk_advisor.advise(validated, retrieval.considered)
        continuation = self.continue_advisor.advise(
            validated, retrieval.considered, ranking
        )
        quality = self.quality_gate.evaluate(
            validated,
            retrieval,
            ranking,
            prompt_token_limit=(
                self.max_guidance_tokens
                if prompt_token_limit is None
                else min(self.max_guidance_tokens, max(0, int(prompt_token_limit)))
            ),
        )
        guidance = quality.prompt_guidance
        if not records and quality.decision.get("decision") == "ABSTAIN":
            # Preserve the established empty-store reason for old reports while
            # the quality decision records the more detailed gate outcome.
            guidance = dict(guidance)
            guidance["fallback_reason"] = "NO_ELIGIBLE_EXPERIENCE"
            guidance = validate_guidance(guidance)
        result = GuidanceResult(validate_guidance(guidance), risk, continuation, frozen)
        self._last_result = result
        self._last_query = validated
        self._last_quality_decision = quality.decision
        return result

    def build_guidance(
        self,
        *,
        mode: str,
        source: str,
        failure_evidence: object = None,
        synth_report: object = None,
        synth_evidence: object = None,
        task_split: str = "unknown",
        algorithm_family: str | None = None,
        current_run_id: str | None = None,
        current_candidate_id: str | None = None,
        description: str = "",
        task_id: str = "unknown-task",
        difficulty: int = 1,
        attempted_strategy_bundles: Sequence[Sequence[str]] = (),
        attempted_patch_hashes: Sequence[str] = (),
        remaining_credits: int | None = None,
        remaining_tokens: int = 1,
        no_improvement_rounds: int = 0,
        prompt_token_limit: int | None = None,
    ) -> dict[str, object]:
        """Stable Planner-adapter API returning only bounded guidance."""

        query = self.feature_extractor.build_query(
            mode=mode,
            source=source,
            description=description,
            task_id=task_id,
            task_split=task_split,
            algorithm_family=algorithm_family,
            run_id=current_run_id or "experience-query",
            candidate_id=current_candidate_id,
            difficulty=difficulty,
            failure_evidence=failure_evidence,
            synth_report=synth_report,
            synth_evidence=synth_evidence,
            attempted_strategy_bundles=attempted_strategy_bundles,
            attempted_patch_hashes=attempted_patch_hashes,
            remaining_credits=remaining_credits,
            remaining_tokens=remaining_tokens,
            no_improvement_rounds=no_improvement_rounds,
        )
        return self.recommend(
            query,
            prompt_token_limit=prompt_token_limit,
        ).guidance

    def persist_recommendation(
        self, round_index: int, guidance: Mapping[str, object]
    ) -> dict[str, object]:
        """Idempotently append the last frozen recommendation audit record."""

        if int(round_index) < 0:
            raise ValueError("round_index must be non-negative")
        if self.recommendation_path is None:
            return {"persisted": False, "reason": "RECOMMENDATION_STORE_DISABLED"}
        if (
            self._last_result is None
            or self._last_query is None
            or self._last_quality_decision is None
        ):
            raise ValueError("build_guidance/recommend must run before persistence")
        validated = validate_guidance(guidance)
        if canonical_json(validated) != canonical_json(self._last_result.guidance):
            raise ValueError("guidance does not match the coordinator's frozen result")
        payload: dict[str, object] = {
            "schema_version": "v3e.recommendation.v2",
            "recommendation_id": canonical_sha256(
                {
                    "query_id": self._last_query["query_id"],
                    "round_index": int(round_index),
                    "snapshot": self._last_result.snapshot.to_dict(),
                    "guidance": validated,
                    "quality_decision": self._last_quality_decision,
                }
            ),
            "round_index": int(round_index),
            "query_id": self._last_query["query_id"],
            "query_features": {
                "mode": self._last_query["mode"],
                "difficulty": self._last_query["difficulty"],
                "evidence_features": self._last_query["evidence_features"],
                "attempted_strategy_bundles": self._last_query[
                    "attempted_strategy_bundles"
                ],
                "remaining_credits": self._last_query["remaining_credits"],
                "remaining_tokens": self._last_query["remaining_tokens"],
                "no_improvement_rounds": self._last_query[
                    "no_improvement_rounds"
                ],
            },
            "snapshot": self._last_result.snapshot.to_dict(),
            "guidance": validated,
            "quality_decision": validate_quality_decision(
                self._last_quality_decision
            ),
            "risk_advisory": self._last_result.risk_advisory,
            "continue_advisory": self._last_result.continue_advisory,
        }
        encoded = canonical_json(payload)
        self.recommendation_path.parent.mkdir(parents=True, exist_ok=True)
        with self.recommendation_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                committed = handle.read()
                if committed and not committed.endswith(b"\n"):
                    newline = committed.rfind(b"\n")
                    handle.seek(0 if newline < 0 else newline + 1)
                    handle.truncate()
                    handle.seek(0)
                    committed = handle.read()
                for raw in committed.splitlines():
                    try:
                        existing = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ValueError("corrupt recommendation JSONL") from exc
                    if existing.get("recommendation_id") == payload["recommendation_id"]:
                        if canonical_json(existing) != encoded:
                            raise ValueError("recommendation ID collision")
                        return {
                            "persisted": False,
                            "reason": "ALREADY_PRESENT",
                            "recommendation_id": payload["recommendation_id"],
                        }
                handle.seek(0, os.SEEK_END)
                handle.write(encoded + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "persisted": True,
            "reason": "APPENDED",
            "recommendation_id": payload["recommendation_id"],
        }


__all__ = [
    "BayesianStrategyRanker",
    "EmpiricalRiskAdvisor",
    "ExperienceCoordinator",
    "ExperienceFeatureExtractor",
    "HeuristicContinueAdvisor",
    "WeightedKNNRetriever",
]
