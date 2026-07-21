"""Versioned, public-only schema and taxonomies for the V3-E knowledge base.

The v2 record is a derived metadata record.  It never stores source code,
Patch text, logs or prompts and cannot authorize any HLS orchestration action.
"""

from __future__ import annotations

import json
import math
import re
from typing import Mapping, Sequence

from .v3_experience import (
    ExperienceValidationError,
    canonical_json,
    canonical_sha256,
)


EXPERIENCE_V2_SCHEMA = "v3e.experience.v2"
FAILURE_TAXONOMY_SCHEMA = "v3e.failure-taxonomy.v1"
BOTTLENECK_TAXONOMY_SCHEMA = "v3e.bottleneck-taxonomy.v1"
STRATEGY_TAXONOMY_SCHEMA = "v3e.strategy-taxonomy.v1"

MODES = frozenset({"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"})
TASK_SPLITS_V2 = frozenset({"train", "dev", "hidden_like"})
EVIDENCE_LEVELS = frozenset(
    {"REAL_LLM_VITIS", "DETERMINISTIC_FIXTURE", "DEMO_FIXTURE", "ORACLE_FIXTURE"}
)
STATUS_VALUES = frozenset({"PASS", "FAIL", "NOT_RUN", "SKIPPED", "UNKNOWN"})
PATCH_COMPLEXITIES = frozenset({"SMALL", "MEDIUM", "LARGE"})
PRESSURE_VALUES = frozenset({"LOW", "MEDIUM", "HIGH", "UNKNOWN"})
NUMERIC_SEMANTICS = frozenset(
    {"INTEGER_EXACT", "FLOAT_TOLERANT", "FIXED_POINT", "MIXED", "UNKNOWN"}
)
MEMORY_PATTERNS = frozenset(
    {
        "SEQUENTIAL",
        "STRIDED",
        "RANDOM",
        "BANKED",
        "STREAMING",
        "MIXED",
        "UNKNOWN",
    }
)

REPAIR_FAILURE_SUBTYPES = frozenset(
    {
        "COMPILE_ERROR",
        "OFF_BY_ONE",
        "WRONG_LOOP_BOUND",
        "WRONG_ARRAY_INDEX",
        "OMITTED_TERM",
        "WRONG_BRANCH_CONDITION",
        "WRONG_SIGN",
        "WRONG_COEFFICIENT",
        "BAD_INITIALIZATION",
        "BAD_ACCUMULATION",
        "NUMERIC_CAST_OR_TRUNCATION",
        "OUT_OF_BOUNDS",
        "FUNCTIONAL_MISMATCH_OTHER",
        "UNKNOWN",
    }
)
SYNTH_FAILURE_SUBTYPES = frozenset(
    {
        "DYNAMIC_ALLOCATION",
        "UNSUPPORTED_STL",
        "UNSUPPORTED_CALL",
        "RECURSION",
        "NON_STATIC_LOOP_BOUND",
        "UNSYNTHESIZABLE_TYPE",
        "RESOURCE_LIMIT",
        "CLOCK_CONSTRAINT",
        "DEPENDENCY_PREVENTS_SYNTHESIS",
        "SYNTHESIS_ERROR_OTHER",
        "UNKNOWN",
    }
)
STRUCTURAL_FAILURE_SUBTYPES = frozenset(
    {
        "STREAM_COUNT_MISMATCH",
        "PRODUCER_BURST_DEADLOCK",
        "CONSUMER_STARVATION",
        "FIFO_DEPTH_INSUFFICIENT",
        "DATAFLOW_DEPENDENCY_CYCLE",
        "PRODUCER_CONSUMER_RATE_MISMATCH",
        "STREAM_ORDER_MISMATCH",
        "INTERFACE_PROTOCOL_MISMATCH",
        "RTL_C_MISMATCH",
        "COSIM_TIMEOUT_UNKNOWN",
        "STRUCTURAL_ERROR_OTHER",
        "UNKNOWN",
    }
)
OPTIMIZE_BOTTLENECK_SUBTYPES = frozenset(
    {
        "SERIAL_REDUCTION",
        "MISSING_PARALLELISM",
        "MEMORY_PORT_LIMIT",
        "MEMORY_BANKING_CONFLICT",
        "LOW_PARALLEL_FACTOR",
        "LOOP_NOT_PIPELINED",
        "LOOP_II_LIMITED",
        "HIGH_TRANSACTION_LATENCY_WITH_II_ONE",
        "SEQUENTIAL_LOAD_COMPUTE_STORE",
        "DATA_DEPENDENCY",
        "RESOURCE_OVERPROVISION",
        "RESOURCE_UNDERUTILIZATION",
        "SUBOPTIMAL_LOOP_STRUCTURE",
        "OPTIMIZATION_BOTTLENECK_OTHER",
        "UNKNOWN",
    }
)

FAILURE_SUBTYPES_BY_MODE = {
    "REPAIR": REPAIR_FAILURE_SUBTYPES,
    "SYNTH_FIX": SYNTH_FAILURE_SUBTYPES,
    "STRUCTURAL_FIX": STRUCTURAL_FAILURE_SUBTYPES,
    "OPTIMIZE": frozenset({"UNKNOWN"}),
}
BOTTLENECK_SUBTYPES_BY_MODE = {
    "REPAIR": frozenset({"UNKNOWN"}),
    "SYNTH_FIX": frozenset({"UNKNOWN"}),
    "STRUCTURAL_FIX": frozenset({"UNKNOWN"}),
    "OPTIMIZE": OPTIMIZE_BOTTLENECK_SUBTYPES,
}

REPAIR_STRATEGIES = frozenset(
    {
        "FIX_LOOP_BOUND",
        "FIX_ARRAY_INDEX",
        "RESTORE_OMITTED_TERM",
        "FIX_BRANCH_CONDITION",
        "FIX_SIGN",
        "FIX_COEFFICIENT",
        "FIX_INITIALIZATION",
        "FIX_ACCUMULATION",
        "FIX_NUMERIC_CAST",
        "FIX_OUT_OF_BOUNDS",
        "OTHER_FUNCTIONAL_REPAIR",
    }
)
SYNTH_STRATEGIES = frozenset(
    {
        "REMOVE_DYNAMIC_ALLOCATION",
        "REPLACE_UNSUPPORTED_STL",
        "REWRITE_UNSUPPORTED_CALL",
        "REMOVE_RECURSION",
        "STATICIZE_LOOP_BOUND",
        "REPLACE_UNSYNTHESIZABLE_TYPE",
        "REDUCE_RESOURCE_PRESSURE",
        "FIX_CLOCK_CONSTRAINT",
        "REWRITE_DEPENDENCY",
        "OTHER_SYNTHESIS_REPAIR",
    }
)
STRUCTURAL_STRATEGIES = frozenset(
    {
        "BALANCE_STREAM_COUNTS",
        "INTERLEAVE_STREAM_WRITES",
        "REORDER_STREAM_OPERATIONS",
        "INCREASE_FIFO_DEPTH",
        "BREAK_DATAFLOW_CYCLE",
        "REBALANCE_PRODUCER_CONSUMER",
        "REMOVE_UNSAFE_DATAFLOW",
        "FIX_STREAM_ORDER",
        "FIX_INTERFACE_PROTOCOL",
        "FIX_RTL_C_SEMANTICS",
        "OTHER_STRUCTURAL_REPAIR",
    }
)
OPTIMIZE_STRATEGIES = frozenset(
    {
        "ARRAY_PARTITION",
        "MEMORY_PARTITION",
        "LOOP_UNROLL",
        "PAR_FACTOR_TUNING",
        "MULTI_PARTIAL_SUM",
        "PARALLEL_REDUCTION",
        "MEMORY_BANKING",
        "LOCAL_LOOP_RESTRUCTURE",
        "LOOP_PIPELINE",
        "DATAFLOW",
        "STREAMING",
        "BITWIDTH_OPTIMIZATION",
        "LOAD_COMPUTE_STORE_OVERLAP",
        "RESOURCE_REBALANCING",
        "OTHER_OPTIMIZATION",
    }
)
STRATEGIES_BY_MODE = {
    "REPAIR": REPAIR_STRATEGIES,
    "SYNTH_FIX": SYNTH_STRATEGIES,
    "STRUCTURAL_FIX": STRUCTURAL_STRATEGIES,
    "OPTIMIZE": OPTIMIZE_STRATEGIES,
}

ALGORITHM_FAMILIES = frozenset(
    {
        "VECTOR_ELEMENTWISE",
        "DOT_PRODUCT",
        "REDUCTION",
        "MATRIX_MULTIPLICATION",
        "FIR_CONVOLUTION",
        "STENCIL",
        "HISTOGRAM",
        "PREFIX_SUM",
        "COORDINATE_TRANSFORM",
        "STREAM_PIPELINE",
        "RESIDUAL_DATAFLOW",
        "OTHER",
    }
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_TEXT = re.compile(r"\A[A-Za-z0-9_.:@+ /-]{1,160}\Z")
_ABSOLUTE_PATH = re.compile(r"(?:\A/|\b[A-Za-z]:\\)")
_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9._-]{8,}|\bbearer\s+\S+|"
    r"\b(?:api[_-]?key|authorization|access[_-]?token)\s*[:=])",
    re.IGNORECASE,
)
_FORBIDDEN = re.compile(r"(?:^|[._/\\-])(golden|hidden|reference|answer)(?:$|[._/\\-])", re.I)

_TOP_FIELDS = {
    "schema_version",
    "record_id",
    "taxonomy_versions",
    "source",
    "problem",
    "structure_features",
    "strategy",
    "validation",
    "performance",
    "cost",
    "provenance",
}
_SOURCE_FIELDS = {
    "run_id",
    "candidate_id",
    "round_index",
    "provider",
    "model",
    "prompt_version",
    "toolchain",
    "backend_fingerprint",
    "evidence_level",
    "task_split",
    "task_family_hash",
    "algorithm_family",
    "difficulty",
}
_PROBLEM_FIELDS = {
    "mode",
    "failure_stage",
    "failure_subtype",
    "primary_bottleneck",
    "bottleneck_subtype",
    "numeric_semantics",
    "requires_cosim",
}
_STRUCTURE_FIELDS = {
    "top_signature_hash",
    "loop_count_bucket",
    "critical_loop_ii",
    "critical_loop_trip_count_bucket",
    "transaction_interval_bucket",
    "latency_bucket",
    "has_pipeline",
    "has_dataflow",
    "has_stream",
    "has_fifo",
    "has_reduction",
    "has_dynamic_allocation",
    "has_recursion",
    "has_unsupported_stl",
    "memory_access_pattern",
    "resource_pressure",
}
_STRATEGY_FIELDS = {
    "declared_strategy_bundle",
    "observed_strategy_atoms",
    "strategy_normalization_confidence",
    "normalization_reason_codes",
    "unclassified_changes",
    "patch_lines_added",
    "patch_lines_deleted",
    "patch_complexity",
    "patch_digest",
}
_VALIDATION_FIELDS = {
    "patch_valid",
    "interface_guard_pass",
    "candidate_created",
    "csim_status",
    "synth_status",
    "cosim_status",
    "fresh_final_status",
    "promoted",
    "rejected",
    "failure_reason",
}
_PERFORMANCE_FIELDS = {
    "latency_before",
    "latency_after",
    "transaction_interval_before",
    "transaction_interval_after",
    "acceleration",
    "resource_delta",
    "strict_improvement",
}
_COST_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "credits",
    "csim_calls",
    "synth_calls",
    "cosim_calls",
    "wall_time_seconds",
}
_PROVENANCE_FIELDS = {
    "artifact_refs",
    "artifact_hashes",
    "source_record_hash",
    "eligible_for_retrieval",
    "eligible_for_ranking",
    "exclusion_reasons",
}


def taxonomy_manifest() -> dict[str, object]:
    """Return a stable, machine-readable taxonomy manifest."""

    return {
        "failure": {
            "schema_version": FAILURE_TAXONOMY_SCHEMA,
            "by_mode": {
                mode: sorted(values) for mode, values in FAILURE_SUBTYPES_BY_MODE.items()
            },
        },
        "bottleneck": {
            "schema_version": BOTTLENECK_TAXONOMY_SCHEMA,
            "by_mode": {
                mode: sorted(values) for mode, values in BOTTLENECK_SUBTYPES_BY_MODE.items()
            },
        },
        "strategy": {
            "schema_version": STRATEGY_TAXONOMY_SCHEMA,
            "by_mode": {
                mode: sorted(values) for mode, values in STRATEGIES_BY_MODE.items()
            },
        },
        "algorithm_families": sorted(ALGORITHM_FAMILIES),
    }


def _object(value: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ExperienceValidationError(f"{name} fields mismatch")
    return dict(value)


def _digest(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ExperienceValidationError(f"{name} must be a SHA-256 digest")
    return value


def _text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _SAFE_TEXT.fullmatch(value) is None:
        raise ExperienceValidationError(f"{name} must be bounded safe text")
    if _SECRET.search(value) or _ABSOLUTE_PATH.search(value):
        raise ExperienceValidationError(f"{name} contains a secret or absolute path")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperienceValidationError(f"{name} must be a non-negative integer")
    return value


def _number(value: object, name: str, *, nullable: bool = True) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperienceValidationError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ExperienceValidationError(f"{name} must be finite")
    return result


def _status(value: object, name: str) -> str:
    if value not in STATUS_VALUES:
        raise ExperienceValidationError(f"{name} has an invalid status")
    return str(value)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ExperienceValidationError(f"{name} must be boolean")
    return value


def _string_list(value: object, name: str, *, maximum: int = 32) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ExperienceValidationError(f"{name} must be a bounded list")
    result: list[str] = []
    for index, item in enumerate(value):
        parsed = _text(item, f"{name}[{index}]")
        assert parsed is not None
        if parsed not in result:
            result.append(parsed)
    return result


def _validate_artifact_ref(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"role", "ref", "sha256"}:
        raise ExperienceValidationError("artifact ref fields mismatch")
    role = _text(value.get("role"), "artifact role")
    ref = value.get("ref")
    if (
        not isinstance(ref, str)
        or not ref
        or len(ref) > 320
        or ref.startswith(("/", "\\"))
        or ".." in ref.replace("\\", "/").split("/")
        or _SECRET.search(ref)
        or _FORBIDDEN.search(ref)
    ):
        raise ExperienceValidationError("artifact ref is unsafe")
    digest = _digest(value.get("sha256"), "artifact sha256")
    return {"role": role, "ref": ref.replace("\\", "/"), "sha256": digest}


def v2_identity_key(record: Mapping[str, object]) -> tuple[str, str, int]:
    source = record.get("source")
    if not isinstance(source, Mapping):
        raise ExperienceValidationError("source must be an object")
    run_id = _text(source.get("run_id"), "source.run_id")
    candidate_id = _text(source.get("candidate_id"), "source.candidate_id")
    round_index = _nonnegative_int(source.get("round_index"), "source.round_index")
    assert run_id is not None and candidate_id is not None
    return run_id, candidate_id, round_index


def validate_experience_v2(value: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonical-copy one immutable v2 derived record."""

    if set(value) != _TOP_FIELDS or value.get("schema_version") != EXPERIENCE_V2_SCHEMA:
        raise ExperienceValidationError("v2 experience top-level fields mismatch")
    source = _object(value.get("source"), _SOURCE_FIELDS, "source")
    problem = _object(value.get("problem"), _PROBLEM_FIELDS, "problem")
    structure = _object(value.get("structure_features"), _STRUCTURE_FIELDS, "structure")
    strategy = _object(value.get("strategy"), _STRATEGY_FIELDS, "strategy")
    validation = _object(value.get("validation"), _VALIDATION_FIELDS, "validation")
    performance = _object(value.get("performance"), _PERFORMANCE_FIELDS, "performance")
    cost = _object(value.get("cost"), _COST_FIELDS, "cost")
    provenance = _object(value.get("provenance"), _PROVENANCE_FIELDS, "provenance")

    taxonomy = value.get("taxonomy_versions")
    expected_taxonomy = {
        "failure": FAILURE_TAXONOMY_SCHEMA,
        "bottleneck": BOTTLENECK_TAXONOMY_SCHEMA,
        "strategy": STRATEGY_TAXONOMY_SCHEMA,
    }
    if taxonomy != expected_taxonomy:
        raise ExperienceValidationError("taxonomy versions mismatch")

    mode = problem.get("mode")
    if mode not in MODES:
        raise ExperienceValidationError("problem.mode is invalid")
    failure_subtype = problem.get("failure_subtype")
    bottleneck_subtype = problem.get("bottleneck_subtype")
    if failure_subtype not in FAILURE_SUBTYPES_BY_MODE[str(mode)]:
        raise ExperienceValidationError("failure subtype is invalid for mode")
    if bottleneck_subtype not in BOTTLENECK_SUBTYPES_BY_MODE[str(mode)]:
        raise ExperienceValidationError("bottleneck subtype is invalid for mode")
    if problem.get("numeric_semantics") not in NUMERIC_SEMANTICS:
        raise ExperienceValidationError("numeric semantics is invalid")
    _bool(problem.get("requires_cosim"), "problem.requires_cosim")
    _text(problem.get("failure_stage"), "problem.failure_stage")
    _text(problem.get("primary_bottleneck"), "problem.primary_bottleneck")

    for name in (
        "run_id",
        "candidate_id",
        "provider",
        "model",
        "prompt_version",
        "toolchain",
        "backend_fingerprint",
    ):
        _text(source.get(name), f"source.{name}")
    _nonnegative_int(source.get("round_index"), "source.round_index")
    _nonnegative_int(source.get("difficulty"), "source.difficulty")
    if source.get("task_split") not in TASK_SPLITS_V2:
        raise ExperienceValidationError("source.task_split is invalid")
    if source.get("evidence_level") not in EVIDENCE_LEVELS:
        raise ExperienceValidationError("source.evidence_level is invalid")
    if source.get("algorithm_family") not in ALGORITHM_FAMILIES:
        raise ExperienceValidationError("source.algorithm_family is invalid")
    _digest(source.get("task_family_hash"), "source.task_family_hash")

    _digest(structure.get("top_signature_hash"), "structure.top_signature_hash")
    for name in (
        "loop_count_bucket",
        "critical_loop_trip_count_bucket",
        "transaction_interval_bucket",
        "latency_bucket",
    ):
        _text(structure.get(name), f"structure.{name}")
    ii = structure.get("critical_loop_ii")
    if ii is not None:
        _nonnegative_int(ii, "structure.critical_loop_ii")
    for name in (
        "has_pipeline",
        "has_dataflow",
        "has_stream",
        "has_fifo",
        "has_reduction",
        "has_dynamic_allocation",
        "has_recursion",
        "has_unsupported_stl",
    ):
        _bool(structure.get(name), f"structure.{name}")
    if structure.get("memory_access_pattern") not in MEMORY_PATTERNS:
        raise ExperienceValidationError("memory access pattern is invalid")
    pressure = structure.get("resource_pressure")
    if not isinstance(pressure, Mapping) or set(pressure) != {"lut", "ff", "dsp", "bram"}:
        raise ExperienceValidationError("resource pressure fields mismatch")
    if any(item not in PRESSURE_VALUES for item in pressure.values()):
        raise ExperienceValidationError("resource pressure is invalid")

    declared = _string_list(strategy.get("declared_strategy_bundle"), "declared strategies")
    observed = _string_list(strategy.get("observed_strategy_atoms"), "observed strategies")
    allowed = STRATEGIES_BY_MODE[str(mode)]
    if any(item not in allowed for item in observed):
        raise ExperienceValidationError("observed strategy is invalid for mode")
    if len(declared) > 3:
        raise ExperienceValidationError("declared strategy bundle is too large")
    confidence = _number(
        strategy.get("strategy_normalization_confidence"),
        "strategy normalization confidence",
        nullable=False,
    )
    assert confidence is not None
    if not 0 <= confidence <= 1:
        raise ExperienceValidationError("strategy confidence must be in [0,1]")
    _string_list(strategy.get("normalization_reason_codes"), "normalization reasons")
    _string_list(strategy.get("unclassified_changes"), "unclassified changes", maximum=8)
    _nonnegative_int(strategy.get("patch_lines_added"), "patch lines added")
    _nonnegative_int(strategy.get("patch_lines_deleted"), "patch lines deleted")
    if strategy.get("patch_complexity") not in PATCH_COMPLEXITIES:
        raise ExperienceValidationError("patch complexity is invalid")
    _digest(strategy.get("patch_digest"), "strategy.patch_digest", nullable=True)

    for name in ("patch_valid", "interface_guard_pass", "candidate_created", "promoted", "rejected"):
        _bool(validation.get(name), f"validation.{name}")
    for name in ("csim_status", "synth_status", "cosim_status", "fresh_final_status"):
        _status(validation.get(name), f"validation.{name}")
    _text(validation.get("failure_reason"), "validation.failure_reason", nullable=True)

    for name in (
        "latency_before",
        "latency_after",
        "transaction_interval_before",
        "transaction_interval_after",
        "acceleration",
    ):
        _number(performance.get(name), f"performance.{name}")
    _bool(performance.get("strict_improvement"), "performance.strict_improvement")
    deltas = performance.get("resource_delta")
    if not isinstance(deltas, Mapping) or set(deltas).difference({"lut", "ff", "dsp", "bram", "uram"}):
        raise ExperienceValidationError("resource_delta is invalid")
    for name, item in deltas.items():
        _number(item, f"resource_delta.{name}", nullable=False)

    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "credits",
        "csim_calls",
        "synth_calls",
        "cosim_calls",
    ):
        _nonnegative_int(cost.get(name), f"cost.{name}")
    if cost.get("total_tokens") != cost.get("input_tokens") + cost.get("output_tokens"):
        raise ExperienceValidationError("cost token total mismatch")
    wall = _number(cost.get("wall_time_seconds"), "cost.wall_time_seconds", nullable=False)
    assert wall is not None
    if wall < 0:
        raise ExperienceValidationError("wall time cannot be negative")

    refs = provenance.get("artifact_refs")
    if not isinstance(refs, list) or len(refs) > 32:
        raise ExperienceValidationError("artifact refs are invalid")
    provenance["artifact_refs"] = [_validate_artifact_ref(item) for item in refs]
    hashes = provenance.get("artifact_hashes")
    if not isinstance(hashes, list) or len(hashes) > 32:
        raise ExperienceValidationError("artifact hashes are invalid")
    provenance["artifact_hashes"] = [
        _digest(item, f"artifact_hashes[{index}]") for index, item in enumerate(hashes)
    ]
    _digest(provenance.get("source_record_hash"), "source_record_hash")
    for name in ("eligible_for_retrieval", "eligible_for_ranking"):
        _bool(provenance.get(name), f"provenance.{name}")
    _string_list(provenance.get("exclusion_reasons"), "exclusion reasons")
    if provenance.get("eligible_for_ranking") and source.get("evidence_level") != "REAL_LLM_VITIS":
        raise ExperienceValidationError("only real LLM/Vitis records may be ranking eligible")

    copied = json.loads(canonical_json(value).decode("utf-8"))
    copied["provenance"]["artifact_refs"] = provenance["artifact_refs"]
    copied["provenance"]["artifact_hashes"] = provenance["artifact_hashes"]
    expected_id = canonical_sha256({**copied, "record_id": ""})
    if copied.get("record_id") != expected_id:
        raise ExperienceValidationError("v2 record_id does not bind canonical content")
    v2_identity_key(copied)
    return copied


def seal_experience_v2(body: Mapping[str, object]) -> dict[str, object]:
    """Set the canonical record ID and validate the completed v2 record."""

    candidate = json.loads(canonical_json(body).decode("utf-8"))
    candidate["record_id"] = ""
    candidate["record_id"] = canonical_sha256(candidate)
    return validate_experience_v2(candidate)


__all__ = [
    "ALGORITHM_FAMILIES",
    "BOTTLENECK_SUBTYPES_BY_MODE",
    "BOTTLENECK_TAXONOMY_SCHEMA",
    "EXPERIENCE_V2_SCHEMA",
    "FAILURE_SUBTYPES_BY_MODE",
    "FAILURE_TAXONOMY_SCHEMA",
    "MODES",
    "STRATEGIES_BY_MODE",
    "STRATEGY_TAXONOMY_SCHEMA",
    "seal_experience_v2",
    "taxonomy_manifest",
    "v2_identity_key",
    "validate_experience_v2",
]
