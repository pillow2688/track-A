"""Runtime adapter for frozen Experience V2 and Strategy Ranker V3.

The adapter implements the narrow coordinator contract consumed by the
OpenAI-compatible Planner.  Shadow operation needs only a valid frozen seed.
Guided prompt injection additionally requires a hash-bound admission manifest
whose fixed-protocol metrics satisfy the non-negotiable Gate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import (
    EXPERIENCE_GUIDANCE_SCHEMA,
    canonical_json,
    canonical_sha256,
    estimated_guidance_tokens,
    validate_guidance,
)
from .v3_experience_guidance import ExperienceFeatureExtractor
from .v3_experience_kb import build_kb_query
from .v3_experience_normalizer import (
    classify_algorithm_family,
    classify_bottleneck_subtype,
    classify_failure_subtype,
    task_family_hash,
)
from .v3_experience_v2 import (
    ALGORITHM_FAMILIES,
    STRATEGIES_BY_MODE,
    validate_experience_v2,
)
from .v3_strategy_ranker_v3 import (
    STRATEGY_RANKER_V3_SCHEMA,
    BayesianStrategyRankerV3,
)


RANKER_ADMISSION_SCHEMA = "v3e.strategy-ranker-admission.v1"
RUNTIME_COORDINATOR_SCHEMA = "v3e.experience-v2-runtime.v1"
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_GATE_THRESHOLDS = {
    "minimum_coverage": 0.40,
    "maximum_harmful_rate": 0.05,
    "minimum_records_per_mode": 10,
    "minimum_global_positive_hit_rate": 0.2727,
    "maximum_leakage_violations": 0,
}


def _plain(value: object) -> object:
    return json.loads(canonical_json(value).decode("utf-8"))


def _safe_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _finite_rate(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be in [0,1]")
    return number


def validate_ranker_admission_manifest(
    value: Mapping[str, object], *, seed_sha256: str
) -> dict[str, object]:
    expected = {
        "schema_version",
        "decision",
        "ranker_version",
        "seed_sha256",
        "protocol",
        "thresholds",
        "metrics",
        "evidence_sha256",
    }
    if set(value) != expected:
        raise ValueError("ranker admission manifest fields mismatch")
    if value.get("schema_version") != RANKER_ADMISSION_SCHEMA:
        raise ValueError("unsupported ranker admission manifest")
    if value.get("decision") != "PASS":
        raise ValueError("ranker admission decision is not PASS")
    if value.get("ranker_version") != STRATEGY_RANKER_V3_SCHEMA:
        raise ValueError("ranker admission version mismatch")
    if value.get("seed_sha256") != seed_sha256:
        raise ValueError("ranker admission seed hash mismatch")
    if value.get("protocol") != "LOTO_AND_LEAVE_ONE_TASK_FAMILY_OUT":
        raise ValueError("ranker admission protocol mismatch")
    if value.get("thresholds") != _GATE_THRESHOLDS:
        raise ValueError("ranker admission thresholds were changed")
    _safe_digest(value.get("evidence_sha256"), "evidence_sha256")
    metrics = value.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("ranker admission metrics must be an object")
    required_metrics = {
        "coverage",
        "harmful_rate",
        "global_positive_hit_rate",
        "leakage_violations",
        "by_mode",
    }
    if set(metrics) != required_metrics:
        raise ValueError("ranker admission metrics fields mismatch")
    coverage = _finite_rate(metrics.get("coverage"), "coverage")
    harmful = _finite_rate(metrics.get("harmful_rate"), "harmful_rate")
    positive_hit = _finite_rate(
        metrics.get("global_positive_hit_rate"),
        "global_positive_hit_rate",
    )
    leakage = metrics.get("leakage_violations")
    if isinstance(leakage, bool) or not isinstance(leakage, int) or leakage < 0:
        raise ValueError("leakage_violations must be non-negative")
    by_mode = metrics.get("by_mode")
    if not isinstance(by_mode, Mapping) or set(by_mode) != set(
        STRATEGIES_BY_MODE
    ):
        raise ValueError("ranker admission must cover all four modes")
    if coverage < _GATE_THRESHOLDS["minimum_coverage"]:
        raise ValueError("ranker admission coverage is below Gate")
    if harmful > _GATE_THRESHOLDS["maximum_harmful_rate"]:
        raise ValueError("ranker admission harmful rate exceeds Gate")
    if positive_hit < _GATE_THRESHOLDS["minimum_global_positive_hit_rate"]:
        raise ValueError("ranker admission positive-hit rate is below Gate")
    if leakage > _GATE_THRESHOLDS["maximum_leakage_violations"]:
        raise ValueError("ranker admission leakage Gate failed")
    for mode in sorted(STRATEGIES_BY_MODE):
        row = by_mode.get(mode)
        if not isinstance(row, Mapping) or set(row) != {
            "records",
            "coverage",
            "harmful_rate",
            "positive_hit_rate",
        }:
            raise ValueError(f"ranker admission {mode} metrics mismatch")
        records = row.get("records")
        if (
            isinstance(records, bool)
            or not isinstance(records, int)
            or records < _GATE_THRESHOLDS["minimum_records_per_mode"]
        ):
            raise ValueError(f"ranker admission {mode} record Gate failed")
        _finite_rate(row.get("coverage"), f"{mode}.coverage")
        if (
            _finite_rate(row.get("harmful_rate"), f"{mode}.harmful_rate")
            > _GATE_THRESHOLDS["maximum_harmful_rate"]
        ):
            raise ValueError(f"ranker admission {mode} harmful Gate failed")
        _finite_rate(
            row.get("positive_hit_rate"), f"{mode}.positive_hit_rate"
        )
    copied = _plain(value)
    assert isinstance(copied, dict)
    return copied


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _safe_category(value: object, *, default: str = "UNKNOWN") -> str:
    text = re.sub(r"[^A-Za-z0-9_.:+-]+", "_", str(value or "")).strip("_")
    return text[:160].upper() if text else default


def _memory_pattern(source: str, has_stream: bool) -> str:
    if has_stream:
        return "STREAMING"
    if re.search(r"\[[^\]]*\bi\b[^\]]*\]", source):
        return "SEQUENTIAL"
    return "UNKNOWN"


def _resource_pressure(value: object) -> dict[str, str]:
    text = str(value or "UNKNOWN").upper()
    level = text if text in {"LOW", "MEDIUM", "HIGH", "UNKNOWN"} else "UNKNOWN"
    return {name: level for name in ("lut", "ff", "dsp", "bram")}


class ExperienceV2RuntimeCoordinator:
    """Frozen V2 retrieval/ranking exposed through the Planner coordinator API."""

    requires_admission_gate = True

    def __init__(
        self,
        seed_path: str | Path,
        *,
        recommendation_path: str | Path | None = None,
        admission_manifest_path: str | Path | None = None,
        injection_requested: bool = False,
        max_guidance_tokens: int = 600,
        ranker: BayesianStrategyRankerV3 | None = None,
    ) -> None:
        if max_guidance_tokens < 200:
            raise ValueError("max_guidance_tokens is too small")
        self.seed_path = Path(seed_path).resolve()
        if not self.seed_path.is_file():
            raise ValueError("Experience V2 seed must be an existing JSONL file")
        raw = self.seed_path.read_bytes()
        self._seed_sha256 = hashlib.sha256(raw).hexdigest()
        self._records: list[dict[str, object]] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
                if not isinstance(decoded, Mapping):
                    raise ValueError("record is not an object")
                self._records.append(validate_experience_v2(decoded))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise ValueError(
                    f"invalid Experience V2 seed line {line_number}: "
                    f"{type(exc).__name__}"
                ) from exc
        self.recommendation_path = (
            Path(recommendation_path)
            if recommendation_path is not None
            else None
        )
        self.max_guidance_tokens = int(max_guidance_tokens)
        self.ranker = ranker or BayesianStrategyRankerV3()
        self._feature_extractor = ExperienceFeatureExtractor()
        self._admission: dict[str, object] | None = None
        if admission_manifest_path is not None:
            path = Path(admission_manifest_path).resolve()
            decoded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError("ranker admission manifest must be an object")
            self._admission = validate_ranker_admission_manifest(
                decoded, seed_sha256=self._seed_sha256
            )
        self._injection_authorized = bool(
            injection_requested and self._admission is not None
        )
        if injection_requested and not self._injection_authorized:
            raise ValueError(
                "guided Experience V2 requires a passing admission manifest"
            )
        self._last_query: dict[str, object] | None = None
        self._last_ranking: dict[str, object] | None = None
        self._last_guidance: dict[str, object] | None = None

    @property
    def prompt_injection_authorized(self) -> bool:
        return self._injection_authorized

    def snapshot_metadata(self) -> dict[str, object]:
        return {
            "schema_version": "v3e.experience-snapshot.v1",
            "byte_offset": self.seed_path.stat().st_size,
            "prefix_sha256": self._seed_sha256,
            "record_count": len(self._records),
        }

    def fingerprint(self) -> dict[str, object]:
        return {
            "schema_version": RUNTIME_COORDINATOR_SCHEMA,
            "seed_sha256": self._seed_sha256,
            "record_count": len(self._records),
            "ranker_version": STRATEGY_RANKER_V3_SCHEMA,
            "admission_evidence_sha256": (
                self._admission["evidence_sha256"]
                if self._admission is not None
                else None
            ),
            "prompt_injection_authorized": self._injection_authorized,
        }

    def _query(
        self,
        *,
        mode: str,
        source: str,
        failure_evidence: object,
        synth_report: object,
        synth_evidence: object,
        task_split: str,
        algorithm_family: str | None,
        current_run_id: str | None,
        current_candidate_id: str | None,
        description: str,
        attempted_strategy_bundles: Sequence[Sequence[str]],
        attempted_patch_hashes: Sequence[str],
    ) -> dict[str, object]:
        if task_split not in {"train", "dev", "hidden_like"}:
            raise ValueError(
                "Experience V2 requires an explicit train/dev/hidden_like split"
            )
        if mode not in STRATEGIES_BY_MODE:
            raise ValueError("unsupported Experience V2 mode")
        features = self._feature_extractor.evidence_features(
            source=source,
            failure_evidence=failure_evidence,
            synth_report=synth_report,
            synth_evidence=synth_evidence,
        )
        family = (
            str(algorithm_family).upper()
            if algorithm_family is not None
            else classify_algorithm_family(source, description)
        )
        if family not in ALGORITHM_FAMILIES:
            family = classify_algorithm_family(source, description)
        failure_subtype = classify_failure_subtype(mode, features)
        bottleneck_subtype = (
            classify_bottleneck_subtype(features, source)
            if mode == "OPTIMIZE"
            else "UNKNOWN"
        )
        failure = _mapping(failure_evidence)
        failure_stage = _safe_category(
            failure.get("failure_stage")
            or failure.get("stage")
            or failure.get("failure_kind")
        )
        primary_bottleneck = _safe_category(
            features.get("primary_bottleneck")
        )
        has_stream = bool(features.get("has_stream"))
        structure = {
            "loop_count_bucket": "UNKNOWN",
            "critical_loop_ii": features.get("loop_ii"),
            "critical_loop_trip_count_bucket": features.get(
                "trip_count_bucket"
            ),
            "transaction_interval_bucket": features.get(
                "transaction_interval_bucket"
            ),
            "latency_bucket": features.get("latency_bucket"),
            "has_pipeline": features.get("pipeline_status")
            not in (None, "UNKNOWN", "NOT_EVIDENCED"),
            "has_dataflow": bool(features.get("has_dataflow")),
            "has_stream": has_stream,
            "has_fifo": bool(features.get("has_fifo")),
            "has_reduction": bool(
                re.search(r"\b(?:sum|acc|total)\s*(?:\+=|=.*\+)", source)
            ),
            "has_dynamic_allocation": bool(
                re.search(
                    r"\b(?:new|delete|malloc|calloc|realloc|free)\b",
                    source,
                )
            ),
            "has_recursion": False,
            "has_unsupported_stl": bool(
                re.search(r"\bstd::(?:vector|map|list|deque|function)\b", source)
            ),
            "memory_access_pattern": _memory_pattern(source, has_stream),
            "resource_pressure": _resource_pressure(
                features.get("resource_pressure")
            ),
            "memory_bottleneck": bool(features.get("memory_bottleneck")),
            "failure_stage": failure_stage,
            "primary_bottleneck": primary_bottleneck,
        }
        current_family = task_family_hash(source, family, structure)
        attempted = {
            str(atom)
            for bundle in attempted_strategy_bundles
            for atom in bundle
            if atom in STRATEGIES_BY_MODE[mode]
        }
        patch_digest = next(
            (
                digest
                for digest in reversed(list(attempted_patch_hashes))
                if isinstance(digest, str) and _SHA256.fullmatch(digest)
            ),
            None,
        )
        return build_kb_query(
            mode=mode,
            task_split=task_split,
            failure_subtype=failure_subtype,
            bottleneck_subtype=bottleneck_subtype,
            algorithm_family=family,
            task_family_hash=current_family,
            structure_features=structure,
            strategy_context=sorted(attempted),
            current_run_id=current_run_id or "experience-v2-runtime",
            current_candidate_id=current_candidate_id,
            current_patch_digest=patch_digest,
            toolchain="Vitis 2025.2",
            backend_fingerprint="vitis-runtime-public-evidence",
            prompt_version="openai-v2-compat-planner-adapter-v1",
            exclude_same_task_family=True,
        )

    @staticmethod
    def _ranked_bundle(entry: Mapping[str, object]) -> dict[str, object]:
        return {
            "strategy_bundle": [entry["strategy_atom"]],
            "context": "VERIFIED_INDEPENDENT_TASK_FAMILIES",
            "attempts": int(entry["attempts"]),
            "successes": int(entry["successes"]),
            "posterior_success": float(entry["posterior_success"]),
            "utility": float(entry["lower_bound"]),
        }

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
        del task_id, difficulty, remaining_credits, remaining_tokens
        del no_improvement_rounds
        query = self._query(
            mode=mode,
            source=source,
            failure_evidence=failure_evidence,
            synth_report=synth_report,
            synth_evidence=synth_evidence,
            task_split=task_split,
            algorithm_family=algorithm_family,
            current_run_id=current_run_id,
            current_candidate_id=current_candidate_id,
            description=description,
            attempted_strategy_bundles=attempted_strategy_bundles,
            attempted_patch_hashes=attempted_patch_hashes,
        )
        ranking = self.ranker.rank(query, self._records)
        recommended = ranking.get("recommended")
        discouraged = ranking.get("discouraged")
        recommended_bundles = (
            [self._ranked_bundle(recommended)]
            if isinstance(recommended, Mapping)
            else []
        )
        discouraged_bundles = [
            self._ranked_bundle(item)
            for item in (discouraged if isinstance(discouraged, list) else [])[:3]
            if isinstance(item, Mapping)
        ]
        supporting_ids = sorted(
            {
                str(record_id)
                for entry in (
                    [recommended] if isinstance(recommended, Mapping) else []
                )
                + [
                    item
                    for item in (
                        discouraged
                        if isinstance(discouraged, list)
                        else []
                    )[:3]
                    if isinstance(item, Mapping)
                ]
                for record_id in entry.get("evidence_ids", [])
            }
        )[:4]
        guidance: dict[str, object] = {
            "schema_version": EXPERIENCE_GUIDANCE_SCHEMA,
            "similar_successes": [],
            "similar_failures": [],
            "recommended_strategy_bundles": recommended_bundles,
            "discouraged_strategy_bundles": discouraged_bundles,
            "confidence": (
                float(recommended["lower_bound"])
                if isinstance(recommended, Mapping)
                else 0.0
            ),
            "supporting_record_ids": supporting_ids,
            "fallback_reason": ranking.get("abstain_reason"),
            "notice": (
                "Family-level verified historical guidance; validation and "
                "all existing Graph gates remain authoritative."
            ),
        }
        cap = (
            self.max_guidance_tokens
            if prompt_token_limit is None
            else min(self.max_guidance_tokens, max(0, int(prompt_token_limit)))
        )
        safe = validate_guidance(guidance)
        if estimated_guidance_tokens(safe) > cap:
            guidance["discouraged_strategy_bundles"] = []
            guidance["supporting_record_ids"] = []
            safe = validate_guidance(guidance)
        if estimated_guidance_tokens(safe) > cap:
            guidance.update(
                {
                    "recommended_strategy_bundles": [],
                    "discouraged_strategy_bundles": [],
                    "supporting_record_ids": [],
                    "confidence": 0.0,
                    "fallback_reason": "GUIDANCE_TOKEN_CAP",
                }
            )
            safe = validate_guidance(guidance)
        self._last_query = query
        self._last_ranking = ranking
        self._last_guidance = safe
        return safe

    def persist_recommendation(
        self, round_index: int, guidance: Mapping[str, object]
    ) -> dict[str, object]:
        if self.recommendation_path is None:
            return {"persisted": False, "reason": "RECOMMENDATION_STORE_DISABLED"}
        if (
            self._last_query is None
            or self._last_ranking is None
            or self._last_guidance is None
        ):
            raise ValueError("build_guidance must run before persistence")
        safe = validate_guidance(guidance)
        if canonical_json(safe) != canonical_json(self._last_guidance):
            raise ValueError("guidance does not match the frozen ranker result")
        payload: dict[str, object] = {
            "schema_version": "v3e.strategy-ranker-runtime-decision.v1",
            "decision_id": "",
            "round_index": int(round_index),
            "query": self._last_query,
            "ranking": self._last_ranking,
            "guidance": safe,
            "control": {
                "prompt_injection_authorized": self._injection_authorized,
                "admission_evidence_sha256": (
                    self._admission["evidence_sha256"]
                    if self._admission is not None
                    else None
                ),
            },
        }
        payload["decision_id"] = canonical_sha256(
            {**payload, "decision_id": ""}
        )
        encoded = canonical_json(payload)
        self.recommendation_path.parent.mkdir(parents=True, exist_ok=True)
        with self.recommendation_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = handle.read().splitlines()
                if encoded not in existing:
                    handle.seek(0, os.SEEK_END)
                    handle.write(encoded + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    persisted = True
                else:
                    persisted = False
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "persisted": persisted,
            "decision_id": payload["decision_id"],
        }


__all__ = [
    "ExperienceV2RuntimeCoordinator",
    "RANKER_ADMISSION_SCHEMA",
    "RUNTIME_COORDINATOR_SCHEMA",
    "validate_ranker_admission_manifest",
]
