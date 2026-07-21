"""Versioned, public-only contracts for the V3-E experience layer.

The module deliberately contains no LangGraph, provider, or tool imports.  It
defines small immutable values and Protocols so the experience implementation
can advise the existing V3-D workflow without acquiring authority over it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, Sequence


EXPERIENCE_SCHEMA = "v3e.experience.v1"
EXPERIENCE_QUERY_SCHEMA = "v3e.experience-query.v1"
EXPERIENCE_GUIDANCE_SCHEMA = "v3e.experience-guidance.v1"
EXPERIENCE_SNAPSHOT_SCHEMA = "v3e.experience-snapshot.v1"
RISK_ADVISORY_SCHEMA = "v3e.risk-advisory.v1"
CONTINUE_ADVISORY_SCHEMA = "v3e.continue-advisory.v1"

PHASE_MODES = frozenset({"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"})
TASK_SPLITS = frozenset(
    {
        "train",
        "dev",
        "validation",
        "hidden_like",
        "test",
        "holdout",
        "unknown",
        "unspecified",
    }
)
EXECUTION_CLASSES = frozenset(
    {
        "REAL_LLM_VITIS",
        "DETERMINISTIC_FIXTURE",
        "DEMO_FIXTURE",
        "ORACLE_FIXTURE",
    }
)
RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH", "UNKNOWN"})
COSIM_STATUSES = frozenset({"PASS", "FAIL", "SKIPPED", "NOT_RUN", "UNKNOWN"})
RESOURCE_PRESSURES = frozenset({"LOW", "MEDIUM", "HIGH", "UNKNOWN"})

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"\A[A-Za-z0-9_.:@+-]{1,160}\Z")
_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9._-]{8,}|\bbearer\s+\S+|"
    r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_FORBIDDEN_LABEL_TOKENS = frozenset(
    {"answer", "golden", "hidden", "mutation", "reference", "secret"}
)


class ExperienceValidationError(ValueError):
    """A record or advisory violates the V3-E public bounded contract."""


class ExperienceMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    GUIDED = "guided"

    @classmethod
    def parse(cls, value: object) -> "ExperienceMode":
        try:
            return cls(str(value).strip().casefold())
        except ValueError as exc:
            raise ExperienceValidationError(
                "experience mode must be off, shadow, or guided"
            ) from exc


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def opaque_hash(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExperienceValidationError("value to hash must be non-empty text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def numeric_bucket(value: object) -> str:
    """Return a coarse scale bucket shared by latency, interval and trip count."""

    if value is None or isinstance(value, bool):
        return "UNKNOWN"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "UNKNOWN"
    if not math.isfinite(number) or number < 0:
        return "UNKNOWN"
    if number == 0:
        return "0"
    if number <= 1:
        return "1"
    if number <= 4:
        return "2-4"
    if number <= 16:
        return "5-16"
    if number <= 64:
        return "17-64"
    if number <= 256:
        return "65-256"
    if number <= 1024:
        return "257-1024"
    return "1024+"


def normalize_strategy_bundle(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split("+")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw = list(value)
    else:
        raise ExperienceValidationError("strategy bundle must be text or a list")
    normalized: list[str] = []
    for item in raw:
        strategy = re.sub(r"[^A-Z0-9_]+", "_", str(item).strip().upper()).strip("_")
        if len(strategy) > 64:
            raise ExperienceValidationError("strategy identifier is too long")
        if strategy and strategy not in normalized:
            normalized.append(strategy)
    if len(normalized) > 3:
        raise ExperienceValidationError("strategy bundle contains more than 3 items")
    return normalized


def normalized_patch_hash(patch: str) -> str:
    """Hash a whitespace-stable diff without retaining the Patch itself."""

    if not isinstance(patch, str):
        raise ExperienceValidationError("patch must be text")
    lines = [line.rstrip() for line in patch.replace("\r\n", "\n").split("\n")]
    return hashlib.sha256(("\n".join(lines).strip() + "\n").encode("utf-8")).hexdigest()


def changed_patch_lines(patch: str) -> int:
    return sum(
        1
        for line in str(patch).splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _plain_copy(value: object) -> object:
    return json.loads(canonical_json(value).decode("utf-8"))


def _required_fields(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        missing = sorted(expected.difference(value))
        extra = sorted(set(value).difference(expected))
        raise ExperienceValidationError(
            f"{name} fields mismatch; missing={missing}, extra={extra}"
        )


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperienceValidationError(f"{name} must be a non-negative integer")
    return value


def _optional_number(value: object, name: str, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperienceValidationError(f"{name} must be numeric or null")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0) or number < 0:
        raise ExperienceValidationError(f"{name} has an invalid numeric value")
    return number


def _optional_bool(value: object, name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ExperienceValidationError(f"{name} must be boolean or null")
    return value


def _safe_short_text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 320:
        raise ExperienceValidationError(f"{name} must be bounded non-empty text")
    if _SECRET.search(value) or _POSIX_ABSOLUTE_PATH.search(value) or _WINDOWS_ABSOLUTE_PATH.search(value):
        raise ExperienceValidationError(f"{name} contains secret or local-path data")
    return value


def _label_tokens(value: object) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", str(value).casefold())
        if token
    }


def _contains_forbidden_label(value: object) -> bool:
    return bool(_label_tokens(value).intersection(_FORBIDDEN_LABEL_TOKENS))


def _validate_bounded_public_value(value: object, name: str) -> None:
    """Reject hidden/golden/secret/path-bearing values at plugin boundaries."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if _contains_forbidden_label(key):
                raise ExperienceValidationError(f"{name} contains a forbidden field")
            _validate_bounded_public_value(item, name)
        return
    if isinstance(value, list):
        for item in value:
            _validate_bounded_public_value(item, name)
        return
    if isinstance(value, str) and (
        _SECRET.search(value)
        or _POSIX_ABSOLUTE_PATH.search(value)
        or _WINDOWS_ABSOLUTE_PATH.search(value)
        or _contains_forbidden_label(value)
    ):
        raise ExperienceValidationError(f"{name} contains non-public data")


def _validate_artifact_ref(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, {"role", "ref", "sha256"}, "artifact reference")
    role = _safe_short_text(value.get("role"), "artifact role")
    reference = _safe_short_text(value.get("ref"), "artifact ref")
    digest = value.get("sha256")
    assert isinstance(role, str) and isinstance(reference, str)
    relative = Path(reference)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or _contains_forbidden_label(role)
        or any(_contains_forbidden_label(part) for part in relative.parts)
    ):
        raise ExperienceValidationError("artifact ref is not a safe public run reference")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ExperienceValidationError("artifact sha256 is invalid")
    return {"role": role, "ref": reference, "sha256": digest}


EVIDENCE_FEATURE_FIELDS = {
    "failure_type",
    "primary_bottleneck",
    "loop_ii",
    "trip_count_bucket",
    "transaction_interval_bucket",
    "latency_bucket",
    "pipeline_status",
    "has_dataflow",
    "has_stream",
    "has_fifo",
    "has_interface",
    "has_bitwidth",
    "memory_bottleneck",
    "resource_pressure",
}


def validate_evidence_features(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, EVIDENCE_FEATURE_FIELDS, "evidence features")
    result = dict(value)
    for name in ("failure_type", "primary_bottleneck"):
        result[name] = _safe_short_text(result.get(name), name, nullable=True)
    loop_ii = result.get("loop_ii")
    if loop_ii is not None:
        result["loop_ii"] = _nonnegative_int(loop_ii, "loop_ii")
    for name in (
        "trip_count_bucket",
        "transaction_interval_bucket",
        "latency_bucket",
        "pipeline_status",
    ):
        result[name] = _safe_short_text(result.get(name), name)
    for name in (
        "has_dataflow",
        "has_stream",
        "has_fifo",
        "has_interface",
        "has_bitwidth",
        "memory_bottleneck",
    ):
        if not isinstance(result.get(name), bool):
            raise ExperienceValidationError(f"{name} must be boolean")
    if result.get("resource_pressure") not in RESOURCE_PRESSURES:
        raise ExperienceValidationError("resource_pressure is invalid")
    copied = _plain_copy(result)
    assert isinstance(copied, dict)
    return copied


PROPOSAL_FEATURE_FIELDS = {
    "strategy_bundle",
    "patch_lines",
    "planner_risk",
    "normalized_patch_hash",
}


def validate_proposal_features(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, PROPOSAL_FEATURE_FIELDS, "proposal features")
    bundle = normalize_strategy_bundle(value.get("strategy_bundle"))
    patch_hash = value.get("normalized_patch_hash")
    if patch_hash is not None and (
        not isinstance(patch_hash, str) or _SHA256.fullmatch(patch_hash) is None
    ):
        raise ExperienceValidationError("normalized_patch_hash is invalid")
    risk = str(value.get("planner_risk", "UNKNOWN")).upper()
    if risk not in RISK_LEVELS:
        raise ExperienceValidationError("planner_risk is invalid")
    return {
        "strategy_bundle": bundle,
        "patch_lines": _nonnegative_int(value.get("patch_lines"), "patch_lines"),
        "planner_risk": risk,
        "normalized_patch_hash": patch_hash,
    }


OUTCOME_FIELDS = {
    "patch_valid",
    "candidate_created",
    "csim_pass",
    "synth_pass",
    "cosim_status",
    "final_pass",
    "promoted",
    "failure_stage",
    "latency_before",
    "latency_after",
    "acceleration",
    "tokens",
    "credits",
    "wall_time_seconds",
}


def validate_outcome(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, OUTCOME_FIELDS, "experience outcome")
    for name in ("patch_valid", "candidate_created", "promoted"):
        if not isinstance(value.get(name), bool):
            raise ExperienceValidationError(f"outcome.{name} must be boolean")
    cosim = value.get("cosim_status")
    if cosim not in COSIM_STATUSES:
        raise ExperienceValidationError("outcome.cosim_status is invalid")
    return {
        "patch_valid": value["patch_valid"],
        "candidate_created": value["candidate_created"],
        "csim_pass": _optional_bool(value.get("csim_pass"), "csim_pass"),
        "synth_pass": _optional_bool(value.get("synth_pass"), "synth_pass"),
        "cosim_status": cosim,
        "final_pass": _optional_bool(value.get("final_pass"), "final_pass"),
        "promoted": value["promoted"],
        "failure_stage": _safe_short_text(
            value.get("failure_stage"), "failure_stage", nullable=True
        ),
        "latency_before": _optional_number(value.get("latency_before"), "latency_before"),
        "latency_after": _optional_number(value.get("latency_after"), "latency_after"),
        "acceleration": _optional_number(
            value.get("acceleration"), "acceleration", positive=True
        ),
        "tokens": _nonnegative_int(value.get("tokens"), "tokens"),
        "credits": _nonnegative_int(value.get("credits"), "credits"),
        "wall_time_seconds": _optional_number(
            value.get("wall_time_seconds"), "wall_time_seconds"
        ),
    }


EXPERIENCE_RECORD_FIELDS = {
    "schema_version",
    "record_id",
    "trajectory_id",
    "revision",
    "run_id",
    "candidate_id",
    "task_id_hash",
    "task_split",
    "mode",
    "difficulty",
    "algorithm_family",
    "execution_class",
    "eligible_for_ranking",
    "evidence_features",
    "proposal_features",
    "outcome",
    "artifact_refs",
}


def validate_experience_record(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, EXPERIENCE_RECORD_FIELDS, "experience record")
    if value.get("schema_version") != EXPERIENCE_SCHEMA:
        raise ExperienceValidationError("unsupported experience schema")
    for name in ("record_id", "trajectory_id", "task_id_hash"):
        item = value.get(name)
        if not isinstance(item, str) or _SHA256.fullmatch(item) is None:
            raise ExperienceValidationError(f"{name} must be a SHA-256 digest")
    for name in ("run_id", "candidate_id"):
        item = value.get(name)
        if (
            not isinstance(item, str)
            or _SAFE_ID.fullmatch(item) is None
            or _SECRET.search(item) is not None
        ):
            raise ExperienceValidationError(f"{name} is not a safe opaque identifier")
    mode = value.get("mode")
    split = value.get("task_split")
    execution_class = value.get("execution_class")
    if mode not in PHASE_MODES:
        raise ExperienceValidationError("experience mode is invalid")
    if split not in TASK_SPLITS:
        raise ExperienceValidationError("task_split is invalid")
    if execution_class not in EXECUTION_CLASSES:
        raise ExperienceValidationError("execution_class is invalid")
    eligible = value.get("eligible_for_ranking")
    if not isinstance(eligible, bool):
        raise ExperienceValidationError("eligible_for_ranking must be boolean")
    if eligible and execution_class != "REAL_LLM_VITIS":
        raise ExperienceValidationError(
            "only REAL_LLM_VITIS records may be ranking-eligible"
        )
    difficulty = _nonnegative_int(value.get("difficulty"), "difficulty")
    if difficulty <= 0:
        raise ExperienceValidationError("difficulty must be positive")
    algorithm_family = _safe_short_text(
        value.get("algorithm_family"), "algorithm_family"
    )
    evidence = value.get("evidence_features")
    proposal = value.get("proposal_features")
    outcome = value.get("outcome")
    refs = value.get("artifact_refs")
    if not isinstance(evidence, Mapping) or not isinstance(proposal, Mapping):
        raise ExperienceValidationError("experience feature sections must be objects")
    if not isinstance(outcome, Mapping) or not isinstance(refs, list):
        raise ExperienceValidationError("experience outcome/artifacts are invalid")
    artifacts = []
    seen_refs: set[tuple[str, str]] = set()
    for raw in refs:
        if not isinstance(raw, Mapping):
            raise ExperienceValidationError("artifact reference must be an object")
        item = _validate_artifact_ref(raw)
        identity = (str(item["role"]), str(item["ref"]))
        if identity not in seen_refs:
            artifacts.append(item)
            seen_refs.add(identity)
    if eligible and not artifacts:
        raise ExperienceValidationError(
            "ranking-eligible real experience must bind at least one hashed artifact"
        )
    result = {
        "schema_version": EXPERIENCE_SCHEMA,
        "record_id": value["record_id"],
        "trajectory_id": value["trajectory_id"],
        "revision": _nonnegative_int(value.get("revision"), "revision"),
        "run_id": value["run_id"],
        "candidate_id": value["candidate_id"],
        "task_id_hash": value["task_id_hash"],
        "task_split": split,
        "mode": mode,
        "difficulty": difficulty,
        "algorithm_family": algorithm_family,
        "execution_class": execution_class,
        "eligible_for_ranking": eligible,
        "evidence_features": validate_evidence_features(evidence),
        "proposal_features": validate_proposal_features(proposal),
        "outcome": validate_outcome(outcome),
        "artifact_refs": artifacts,
    }
    if result["revision"] <= 0:
        raise ExperienceValidationError("revision must be positive")
    copied = _plain_copy(result)
    assert isinstance(copied, dict)
    return copied


QUERY_FIELDS = {
    "schema_version",
    "query_id",
    "run_id",
    "candidate_id",
    "task_id_hash",
    "task_split",
    "mode",
    "difficulty",
    "algorithm_family",
    "evidence_features",
    "attempted_strategy_bundles",
    "attempted_patch_hashes",
    "remaining_credits",
    "remaining_tokens",
    "no_improvement_rounds",
}


def validate_experience_query(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, QUERY_FIELDS, "experience query")
    if value.get("schema_version") != EXPERIENCE_QUERY_SCHEMA:
        raise ExperienceValidationError("unsupported experience query schema")
    for name in ("query_id", "task_id_hash"):
        item = value.get(name)
        if not isinstance(item, str) or _SHA256.fullmatch(item) is None:
            raise ExperienceValidationError(f"query {name} must be SHA-256")
    run_id = value.get("run_id")
    candidate_id = value.get("candidate_id")
    if not isinstance(run_id, str) or _SAFE_ID.fullmatch(run_id) is None:
        raise ExperienceValidationError("query run_id is invalid")
    if candidate_id is not None and (
        not isinstance(candidate_id, str) or _SAFE_ID.fullmatch(candidate_id) is None
    ):
        raise ExperienceValidationError("query candidate_id is invalid")
    if value.get("task_split") not in TASK_SPLITS or value.get("mode") not in PHASE_MODES:
        raise ExperienceValidationError("query split or mode is invalid")
    difficulty = _nonnegative_int(value.get("difficulty"), "query difficulty")
    if difficulty <= 0:
        raise ExperienceValidationError("query difficulty must be positive")
    family = _safe_short_text(value.get("algorithm_family"), "algorithm_family")
    evidence = value.get("evidence_features")
    if not isinstance(evidence, Mapping):
        raise ExperienceValidationError("query evidence_features must be an object")
    attempts = value.get("attempted_strategy_bundles")
    patch_hashes = value.get("attempted_patch_hashes")
    if not isinstance(attempts, list) or not isinstance(patch_hashes, list):
        raise ExperienceValidationError("query attempted values must be lists")
    normalized_attempts = [normalize_strategy_bundle(item) for item in attempts]
    normalized_hashes: list[str] = []
    for digest in patch_hashes:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ExperienceValidationError("query attempted patch hash is invalid")
        if digest not in normalized_hashes:
            normalized_hashes.append(digest)
    result = {
        "schema_version": EXPERIENCE_QUERY_SCHEMA,
        "query_id": value["query_id"],
        "run_id": run_id,
        "candidate_id": candidate_id,
        "task_id_hash": value["task_id_hash"],
        "task_split": value["task_split"],
        "mode": value["mode"],
        "difficulty": difficulty,
        "algorithm_family": family,
        "evidence_features": validate_evidence_features(evidence),
        "attempted_strategy_bundles": normalized_attempts,
        "attempted_patch_hashes": normalized_hashes,
        "remaining_credits": (
            None
            if value.get("remaining_credits") is None
            else _nonnegative_int(value.get("remaining_credits"), "remaining_credits")
        ),
        "remaining_tokens": _nonnegative_int(
            value.get("remaining_tokens"), "remaining_tokens"
        ),
        "no_improvement_rounds": _nonnegative_int(
            value.get("no_improvement_rounds"), "no_improvement_rounds"
        ),
    }
    copied = _plain_copy(result)
    assert isinstance(copied, dict)
    return copied


GUIDANCE_FIELDS = {
    "schema_version",
    "similar_successes",
    "similar_failures",
    "recommended_strategy_bundles",
    "discouraged_strategy_bundles",
    "confidence",
    "supporting_record_ids",
    "fallback_reason",
    "notice",
}

GUIDANCE_CASE_FIELDS = {
    "record_id",
    "similarity",
    "features",
    "strategy_bundle",
    "outcome",
}
GUIDANCE_CASE_FEATURE_FIELDS = {
    "failure_type",
    "primary_bottleneck",
    "algorithm_family",
    "loop_ii",
    "trip_count_bucket",
}
RANKED_BUNDLE_FIELDS = {
    "strategy_bundle",
    "context",
    "attempts",
    "semantic_attempts",
    "patch_valid",
    "candidate_created",
    "successes",
    "posterior_success",
    "normalized_expected_gain",
    "average_acceleration",
    "average_tokens",
    "average_credits",
    "average_wall_time_seconds",
    "utility",
    "supporting_record_ids",
}


def validate_guidance(value: Mapping[str, object]) -> dict[str, object]:
    _required_fields(value, GUIDANCE_FIELDS, "experience guidance")
    if value.get("schema_version") != EXPERIENCE_GUIDANCE_SCHEMA:
        raise ExperienceValidationError("unsupported guidance schema")
    successes = value.get("similar_successes")
    failures = value.get("similar_failures")
    recommended = value.get("recommended_strategy_bundles")
    discouraged = value.get("discouraged_strategy_bundles")
    supporting = value.get("supporting_record_ids")
    if not isinstance(successes, list) or len(successes) > 3:
        raise ExperienceValidationError("guidance successes must contain at most 3 cases")
    if not isinstance(failures, list) or len(failures) > 2:
        raise ExperienceValidationError("guidance failures must contain at most 2 cases")
    if not isinstance(recommended, list) or len(recommended) > 3:
        raise ExperienceValidationError("recommended bundles must contain at most 3 items")
    if not isinstance(discouraged, list) or len(discouraged) > 3:
        raise ExperienceValidationError("discouraged bundles must contain at most 3 items")
    if not isinstance(supporting, list):
        raise ExperienceValidationError("supporting_record_ids must be a list")
    for case in [*successes, *failures]:
        if not isinstance(case, Mapping):
            raise ExperienceValidationError("guidance case must be an object")
        _required_fields(case, GUIDANCE_CASE_FIELDS, "guidance case")
        features = case.get("features")
        if not isinstance(features, Mapping):
            raise ExperienceValidationError("guidance case features must be an object")
        _required_fields(
            features, GUIDANCE_CASE_FEATURE_FIELDS, "guidance case features"
        )
        record_id = case.get("record_id")
        if not isinstance(record_id, str) or _SHA256.fullmatch(record_id) is None:
            raise ExperienceValidationError("guidance case record ID is invalid")
        normalize_strategy_bundle(case.get("strategy_bundle"))
        _validate_bounded_public_value(case, "guidance case")
        encoded = canonical_json(case)
        if len(encoded) > 1600:
            raise ExperienceValidationError("guidance case is not bounded or safe")
    for bundle in [*recommended, *discouraged]:
        if not isinstance(bundle, Mapping):
            raise ExperienceValidationError("ranked bundle must be an object")
        extra = set(bundle).difference(RANKED_BUNDLE_FIELDS)
        if extra:
            raise ExperienceValidationError(
                f"ranked bundle contains unsupported fields: {sorted(extra)}"
            )
        if "strategy_bundle" not in bundle:
            raise ExperienceValidationError("ranked bundle is missing strategy_bundle")
        normalize_strategy_bundle(bundle.get("strategy_bundle"))
        _validate_bounded_public_value(bundle, "ranked bundle")
    for digest in supporting:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ExperienceValidationError("supporting record ID is invalid")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ExperienceValidationError("guidance confidence must be numeric")
    if not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise ExperienceValidationError("guidance confidence must be in [0,1]")
    _safe_short_text(value.get("fallback_reason"), "fallback_reason", nullable=True)
    _safe_short_text(value.get("notice"), "notice")
    copied = _plain_copy(value)
    assert isinstance(copied, dict)
    return copied


def estimated_guidance_tokens(value: Mapping[str, object]) -> int:
    """Conservative dependency-free bound: one Unicode character per token."""

    rendered = canonical_json(value).decode("utf-8")
    return max(1, len(rendered))


@dataclass(frozen=True)
class ExperienceSnapshot:
    byte_offset: int
    prefix_sha256: str
    record_count: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.byte_offset, "snapshot byte_offset")
        _nonnegative_int(self.record_count, "snapshot record_count")
        if _SHA256.fullmatch(self.prefix_sha256) is None:
            raise ExperienceValidationError("snapshot prefix_sha256 is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EXPERIENCE_SNAPSHOT_SCHEMA,
            "byte_offset": self.byte_offset,
            "prefix_sha256": self.prefix_sha256,
            "record_count": self.record_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ExperienceSnapshot":
        _required_fields(
            value,
            {"schema_version", "byte_offset", "prefix_sha256", "record_count"},
            "experience snapshot",
        )
        if value.get("schema_version") != EXPERIENCE_SNAPSHOT_SCHEMA:
            raise ExperienceValidationError("unsupported snapshot schema")
        return cls(
            byte_offset=_nonnegative_int(value.get("byte_offset"), "byte_offset"),
            prefix_sha256=str(value.get("prefix_sha256")),
            record_count=_nonnegative_int(value.get("record_count"), "record_count"),
        )


@dataclass(frozen=True)
class PutResult:
    inserted: bool
    record_id: str
    record_sha256: str
    byte_offset: int


@dataclass(frozen=True)
class RetrievalResult:
    successes: tuple[dict[str, object], ...]
    failures: tuple[dict[str, object], ...]
    considered: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class GuidanceResult:
    guidance: dict[str, object]
    risk_advisory: dict[str, object]
    continue_advisory: dict[str, object]
    snapshot: ExperienceSnapshot

    @property
    def actionable(self) -> bool:
        return bool(
            self.guidance.get("similar_successes")
            or self.guidance.get("similar_failures")
            or self.guidance.get("recommended_strategy_bundles")
            or self.guidance.get("discouraged_strategy_bundles")
        )


class ExperienceRepository(Protocol):
    def snapshot(self) -> ExperienceSnapshot: ...

    def put_if_absent(self, record: Mapping[str, object]) -> PutResult: ...

    def records(
        self, snapshot: ExperienceSnapshot | None = None
    ) -> tuple[dict[str, object], ...]: ...

    def latest_trajectories(
        self,
        snapshot: ExperienceSnapshot | None = None,
        *,
        ranking_only: bool = True,
    ) -> tuple[dict[str, object], ...]: ...


class FeatureExtractor(Protocol):
    def build_query(self, **facts: object) -> dict[str, object]: ...

    def build_record(self, **facts: object) -> dict[str, object]: ...


class CaseRetriever(Protocol):
    def retrieve(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> RetrievalResult: ...


class StrategyRanker(Protocol):
    def rank(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]: ...


class RiskAdvisor(Protocol):
    def advise(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> dict[str, object]: ...


class ContinueAdvisor(Protocol):
    def advise(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
        ranking: Mapping[str, object],
    ) -> dict[str, object]: ...
