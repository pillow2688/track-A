"""Deterministic semantic normalization and v1-to-v2 derivation.

This module reads public kernel/Patch artifacts only when the caller supplies
them.  It stores only taxonomy atoms, counts and hashes in the returned record.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from .v3_experience import (
    canonical_sha256,
    normalize_strategy_bundle,
    numeric_bucket,
    validate_experience_record,
)
from .v3_experience_v2 import (
    ALGORITHM_FAMILIES,
    BOTTLENECK_TAXONOMY_SCHEMA,
    EXPERIENCE_V2_SCHEMA,
    FAILURE_TAXONOMY_SCHEMA,
    STRATEGIES_BY_MODE,
    STRATEGY_TAXONOMY_SCHEMA,
    seal_experience_v2,
)


@dataclass(frozen=True)
class StrategyNormalization:
    declared_strategy_bundle: tuple[str, ...]
    observed_strategy_atoms: tuple[str, ...]
    confidence: float
    reason_codes: tuple[str, ...]
    unclassified_changes: tuple[str, ...]
    patch_lines_added: int
    patch_lines_deleted: int
    patch_complexity: str
    patch_digest: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "declared_strategy_bundle": list(self.declared_strategy_bundle),
            "observed_strategy_atoms": list(self.observed_strategy_atoms),
            "strategy_normalization_confidence": self.confidence,
            "normalization_reason_codes": list(self.reason_codes),
            "unclassified_changes": list(self.unclassified_changes),
            "patch_lines_added": self.patch_lines_added,
            "patch_lines_deleted": self.patch_lines_deleted,
            "patch_complexity": self.patch_complexity,
            "patch_digest": self.patch_digest,
        }


@dataclass(frozen=True)
class MigrationContext:
    parent_source: str = ""
    candidate_source: str = ""
    patch: str = ""
    description: str = ""
    provider: str = "UNKNOWN"
    model: str = "UNKNOWN"
    prompt_version: str = "UNKNOWN"
    toolchain: str = "Vitis 2025.2"
    backend_fingerprint: str = "UNKNOWN"
    requires_cosim: bool | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    round_index: int | None = None
    token_policy: Mapping[str, object] | None = None


_DECLARED_ALIASES = {
    "FUNCTIONAL_REPAIR": "OTHER_FUNCTIONAL_REPAIR",
    "SYNTHESIS_REPAIR": "OTHER_SYNTHESIS_REPAIR",
    "STRUCTURAL_REPAIR": "OTHER_STRUCTURAL_REPAIR",
    "PIPELINE": "LOOP_PIPELINE",
    "UNROLL": "LOOP_UNROLL",
    "PARALLEL_FACTOR": "PAR_FACTOR_TUNING",
    "MEMORY_LAYOUT": "MEMORY_PARTITION",
    "BITWIDTH": "BITWIDTH_OPTIMIZATION",
}
_OTHER_BY_MODE = {
    "REPAIR": "OTHER_FUNCTIONAL_REPAIR",
    "SYNTH_FIX": "OTHER_SYNTHESIS_REPAIR",
    "STRUCTURAL_FIX": "OTHER_STRUCTURAL_REPAIR",
    "OPTIMIZE": "OTHER_OPTIMIZATION",
}
_FAILURE_TO_STRATEGY = {
    "OFF_BY_ONE": "FIX_LOOP_BOUND",
    "WRONG_LOOP_BOUND": "FIX_LOOP_BOUND",
    "WRONG_ARRAY_INDEX": "FIX_ARRAY_INDEX",
    "OMITTED_TERM": "RESTORE_OMITTED_TERM",
    "WRONG_BRANCH_CONDITION": "FIX_BRANCH_CONDITION",
    "WRONG_SIGN": "FIX_SIGN",
    "WRONG_COEFFICIENT": "FIX_COEFFICIENT",
    "BAD_INITIALIZATION": "FIX_INITIALIZATION",
    "BAD_ACCUMULATION": "FIX_ACCUMULATION",
    "NUMERIC_CAST_OR_TRUNCATION": "FIX_NUMERIC_CAST",
    "OUT_OF_BOUNDS": "FIX_OUT_OF_BOUNDS",
    "DYNAMIC_ALLOCATION": "REMOVE_DYNAMIC_ALLOCATION",
    "UNSUPPORTED_STL": "REPLACE_UNSUPPORTED_STL",
    "UNSUPPORTED_CALL": "REWRITE_UNSUPPORTED_CALL",
    "RECURSION": "REMOVE_RECURSION",
    "NON_STATIC_LOOP_BOUND": "STATICIZE_LOOP_BOUND",
    "UNSYNTHESIZABLE_TYPE": "REPLACE_UNSYNTHESIZABLE_TYPE",
    "RESOURCE_LIMIT": "REDUCE_RESOURCE_PRESSURE",
    "CLOCK_CONSTRAINT": "FIX_CLOCK_CONSTRAINT",
    "DEPENDENCY_PREVENTS_SYNTHESIS": "REWRITE_DEPENDENCY",
    "STREAM_COUNT_MISMATCH": "BALANCE_STREAM_COUNTS",
    "PRODUCER_BURST_DEADLOCK": "INTERLEAVE_STREAM_WRITES",
    "CONSUMER_STARVATION": "REBALANCE_PRODUCER_CONSUMER",
    "FIFO_DEPTH_INSUFFICIENT": "INCREASE_FIFO_DEPTH",
    "DATAFLOW_DEPENDENCY_CYCLE": "BREAK_DATAFLOW_CYCLE",
    "PRODUCER_CONSUMER_RATE_MISMATCH": "REBALANCE_PRODUCER_CONSUMER",
    "STREAM_ORDER_MISMATCH": "FIX_STREAM_ORDER",
    "INTERFACE_PROTOCOL_MISMATCH": "FIX_INTERFACE_PROTOCOL",
    "RTL_C_MISMATCH": "FIX_RTL_C_SEMANTICS",
}

_OBSERVED_STRATEGY_TO_FAILURE = {
    "FIX_LOOP_BOUND": "WRONG_LOOP_BOUND",
    "FIX_ARRAY_INDEX": "WRONG_ARRAY_INDEX",
    "RESTORE_OMITTED_TERM": "OMITTED_TERM",
    "FIX_BRANCH_CONDITION": "WRONG_BRANCH_CONDITION",
    "FIX_SIGN": "WRONG_SIGN",
    "FIX_COEFFICIENT": "WRONG_COEFFICIENT",
    "FIX_INITIALIZATION": "BAD_INITIALIZATION",
    "FIX_ACCUMULATION": "BAD_ACCUMULATION",
    "FIX_NUMERIC_CAST": "NUMERIC_CAST_OR_TRUNCATION",
    "FIX_OUT_OF_BOUNDS": "OUT_OF_BOUNDS",
    "REMOVE_DYNAMIC_ALLOCATION": "DYNAMIC_ALLOCATION",
    "REPLACE_UNSUPPORTED_STL": "UNSUPPORTED_STL",
    "REWRITE_UNSUPPORTED_CALL": "UNSUPPORTED_CALL",
    "REMOVE_RECURSION": "RECURSION",
    "STATICIZE_LOOP_BOUND": "NON_STATIC_LOOP_BOUND",
    "REPLACE_UNSYNTHESIZABLE_TYPE": "UNSYNTHESIZABLE_TYPE",
    "REDUCE_RESOURCE_PRESSURE": "RESOURCE_LIMIT",
    "FIX_CLOCK_CONSTRAINT": "CLOCK_CONSTRAINT",
    "REWRITE_DEPENDENCY": "DEPENDENCY_PREVENTS_SYNTHESIS",
    "BALANCE_STREAM_COUNTS": "STREAM_COUNT_MISMATCH",
    "INTERLEAVE_STREAM_WRITES": "PRODUCER_BURST_DEADLOCK",
    "REORDER_STREAM_OPERATIONS": "STREAM_ORDER_MISMATCH",
    "INCREASE_FIFO_DEPTH": "FIFO_DEPTH_INSUFFICIENT",
    "BREAK_DATAFLOW_CYCLE": "DATAFLOW_DEPENDENCY_CYCLE",
    "REBALANCE_PRODUCER_CONSUMER": "PRODUCER_CONSUMER_RATE_MISMATCH",
    "FIX_STREAM_ORDER": "STREAM_ORDER_MISMATCH",
    "FIX_INTERFACE_PROTOCOL": "INTERFACE_PROTOCOL_MISMATCH",
    "FIX_RTL_C_SEMANTICS": "RTL_C_MISMATCH",
}


def _changed_lines(patch: str) -> tuple[list[str], list[str]]:
    added: list[str] = []
    deleted: list[str] = []
    for line in str(patch).replace("\r\n", "\n").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            deleted.append(line[1:])
    return added, deleted


def _compact(lines: Sequence[str]) -> str:
    return "\n".join(re.sub(r"\s+", " ", item.strip()) for item in lines if item.strip())


def _conditions(text: str, keyword: str) -> tuple[str, ...]:
    return tuple(re.findall(rf"\b{keyword}\s*\(([^)]*)\)", text))


def _array_indices(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\b[A-Za-z_]\w*\s*\[([^\]]+)\]", text))


def _function_signature(text: str) -> str:
    matches = re.findall(
        r"(?:^|\n)\s*(?:extern\s+\"C\"\s+)?([A-Za-z_][\w:<>,\s*&]+\s+[A-Za-z_]\w*\s*\([^;{}]*\))\s*\{",
        text,
    )
    if not matches:
        return ""
    return re.sub(r"\b[A-Za-z_]\w*\s*(?=[,)=\[])|\s+", "", matches[-1])


def _function_calls(text: str) -> tuple[str, ...]:
    excluded = {"if", "for", "while", "switch", "return", "sizeof"}
    return tuple(
        item
        for item in re.findall(r"\b([A-Za-z_]\w*)\s*\(", text)
        if item not in excluded
    )


def _defined_function_name(text: str) -> str | None:
    matches = re.findall(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", text)
    return matches[-1] if matches else None


def _canonical_declared(mode: str, declared: object) -> tuple[str, ...]:
    values = normalize_strategy_bundle(declared)
    allowed = STRATEGIES_BY_MODE[mode]
    result: list[str] = []
    for item in values:
        normalized = _DECLARED_ALIASES.get(item, item)
        if normalized not in allowed:
            normalized = _OTHER_BY_MODE[mode]
        if normalized not in result:
            result.append(normalized)
    return tuple(result[:3])


def _detect_common(added: str, deleted: str, parent: str, candidate: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    tests = (
        ("LOOP_PIPELINE", r"#\s*pragma\s+HLS\s+PIPELINE", "OBSERVED_PRAGMA_PIPELINE"),
        ("LOOP_UNROLL", r"#\s*pragma\s+HLS\s+UNROLL", "OBSERVED_PRAGMA_UNROLL"),
        ("DATAFLOW", r"#\s*pragma\s+HLS\s+DATAFLOW", "OBSERVED_PRAGMA_DATAFLOW"),
        ("ARRAY_PARTITION", r"#\s*pragma\s+HLS\s+ARRAY_PARTITION", "OBSERVED_ARRAY_PARTITION"),
        ("STREAMING", r"\bhls::stream\b", "OBSERVED_HLS_STREAM"),
        ("BITWIDTH_OPTIMIZATION", r"\bap_(?:u?int|u?fixed)\s*<", "OBSERVED_BITWIDTH_TYPE"),
    )
    for atom, pattern, reason in tests:
        if re.search(pattern, added, re.I) and not re.search(pattern, deleted, re.I):
            found.append((atom, reason))
    if re.search(r"\b(?:PAR_FACTOR|parallel_factor|par_factor)\b", added, re.I):
        found.append(("PAR_FACTOR_TUNING", "OBSERVED_PARALLEL_FACTOR_CHANGE"))
    if re.search(r"\b(?:partial|lane|local)_?(?:sum|acc)\b", candidate, re.I) and not re.search(
        r"\b(?:partial|lane|local)_?(?:sum|acc)\b", parent, re.I
    ):
        found.append(("MULTI_PARTIAL_SUM", "OBSERVED_PARTIAL_SUMS"))
    if (
        ("MULTI_PARTIAL_SUM", "OBSERVED_PARTIAL_SUMS") in found
        and re.search(r"\bfor\b", candidate)
    ):
        found.append(("PARALLEL_REDUCTION", "OBSERVED_PARALLEL_REDUCTION"))
    return found


class StrategyNormalizer:
    """Classify actual public Patch semantics before trusting declarations."""

    def normalize(
        self,
        *,
        mode: str,
        declared_strategy: object = None,
        parent_source: str = "",
        candidate_source: str = "",
        patch: str = "",
        evidence: Mapping[str, object] | None = None,
        validation: Mapping[str, object] | None = None,
    ) -> StrategyNormalization:
        if mode not in STRATEGIES_BY_MODE:
            raise ValueError("unsupported strategy normalization mode")
        added_lines, deleted_lines = _changed_lines(patch)
        added = _compact(added_lines)
        deleted = _compact(deleted_lines)
        declared = _canonical_declared(mode, declared_strategy)
        observed: list[str] = []
        reasons: list[str] = []
        unclassified: list[str] = []

        def add(atom: str, reason: str) -> None:
            if atom in STRATEGIES_BY_MODE[mode] and atom not in observed:
                observed.append(atom)
                reasons.append(reason)

        for atom, reason in _detect_common(added, deleted, parent_source, candidate_source):
            add(atom, reason)

        if mode == "REPAIR":
            if _conditions(parent_source, "for") != _conditions(candidate_source, "for"):
                add("FIX_LOOP_BOUND", "OBSERVED_LOOP_CONDITION_CHANGE")
            if _array_indices(parent_source) != _array_indices(candidate_source):
                add("FIX_ARRAY_INDEX", "OBSERVED_ARRAY_INDEX_CHANGE")
            if _conditions(parent_source, "if") != _conditions(candidate_source, "if"):
                add("FIX_BRANCH_CONDITION", "OBSERVED_BRANCH_CONDITION_CHANGE")
            if re.search(r"(?:\+=|-=|\*=|/=)", added) and re.search(r"(?:\+=|-=|\*=|/=)", deleted):
                add("FIX_ACCUMULATION", "OBSERVED_ACCUMULATION_CHANGE")
            if re.search(r"\b(?:sum|acc|total)\s*=", added, re.I) and re.search(
                r"\b(?:sum|acc|total)\s*=", deleted, re.I
            ):
                add("FIX_INITIALIZATION", "OBSERVED_INITIALIZATION_CHANGE")
            if re.search(r"(?:static_cast\s*<|\([A-Za-z_]\w*\s*\))", added):
                add("FIX_NUMERIC_CAST", "OBSERVED_CAST_CHANGE")
            if re.search(r"[+\-]\s*\d+(?:\.\d+)?", added) and re.search(
                r"[+\-]\s*\d+(?:\.\d+)?", deleted
            ):
                add("FIX_COEFFICIENT", "OBSERVED_COEFFICIENT_CHANGE")
            if re.search(r"\+=.+", added) and not re.search(r"\+=.+", deleted):
                add("RESTORE_OMITTED_TERM", "OBSERVED_ARITHMETIC_TERM_ADDED")
            if re.search(r"\+=\s*-|-=\s*", added) != re.search(r"\+=\s*-|-=\s*", deleted):
                add("FIX_SIGN", "OBSERVED_SIGN_CHANGE")
            if "FIX_LOOP_BOUND" in observed and re.search(r"<=|\+\s*1|-\s*1", added + deleted):
                reasons.append("POSSIBLE_OFF_BY_ONE")

        elif mode == "SYNTH_FIX":
            if re.search(r"\b(?:new|delete|malloc|calloc|realloc|free)\b", deleted) and not re.search(
                r"\b(?:new|delete|malloc|calloc|realloc|free)\b", added
            ):
                add("REMOVE_DYNAMIC_ALLOCATION", "OBSERVED_DYNAMIC_ALLOCATION_REMOVAL")
            if re.search(r"\b(?:std::(?:vector|map|list|deque|function)|#\s*include\s*<vector>)", deleted) and not re.search(
                r"\b(?:std::(?:vector|map|list|deque|function)|#\s*include\s*<vector>)", added
            ):
                add("REPLACE_UNSUPPORTED_STL", "OBSERVED_STL_REMOVAL")
            function_name = _defined_function_name(parent_source)
            if function_name and _function_calls(deleted).count(function_name) > _function_calls(added).count(function_name):
                    add("REMOVE_RECURSION", "OBSERVED_RECURSION_REMOVAL")
            if _conditions(parent_source, "for") != _conditions(candidate_source, "for") and re.search(
                r"\b(?:constexpr|const)\b", added
            ):
                add("STATICIZE_LOOP_BOUND", "OBSERVED_STATIC_LOOP_BOUND")
            if re.search(r"\b(?:ap_(?:u?int|u?fixed)|int\d+_t)\b", added):
                add("REPLACE_UNSYNTHESIZABLE_TYPE", "OBSERVED_SYNTHESIZABLE_TYPE_REWRITE")

        elif mode == "STRUCTURAL_FIX":
            if re.search(r"#\s*pragma\s+HLS\s+STREAM.*depth", added, re.I):
                add("INCREASE_FIFO_DEPTH", "OBSERVED_FIFO_DEPTH_CHANGE")
            if re.search(r"#\s*pragma\s+HLS\s+DATAFLOW", deleted, re.I) and not re.search(
                r"#\s*pragma\s+HLS\s+DATAFLOW", added, re.I
            ):
                add("REMOVE_UNSAFE_DATAFLOW", "OBSERVED_DATAFLOW_REMOVAL")
            parent_calls = _function_calls(parent_source)
            candidate_calls = _function_calls(candidate_source)
            if set(parent_calls) == set(candidate_calls) and parent_calls != candidate_calls:
                add("REORDER_STREAM_OPERATIONS", "OBSERVED_CALL_ORDER_CHANGE")
            if re.search(r"\.(?:read|write)\s*\(", added) and re.search(
                r"\.(?:read|write)\s*\(", deleted
            ):
                add("FIX_STREAM_ORDER", "OBSERVED_STREAM_OPERATION_CHANGE")
            if re.search(r"#\s*pragma\s+HLS\s+INTERFACE", added + deleted, re.I):
                add("FIX_INTERFACE_PROTOCOL", "OBSERVED_INTERFACE_PRAGMA_CHANGE")

        elif mode == "OPTIMIZE":
            if re.search(r"ARRAY_PARTITION.*(?:cyclic|block)", added, re.I):
                add("MEMORY_BANKING", "OBSERVED_MEMORY_BANKING")
            if re.search(r"ARRAY_RESHAPE|bind_storage", added, re.I):
                add("MEMORY_PARTITION", "OBSERVED_MEMORY_LAYOUT_CHANGE")
            if len(_conditions(parent_source, "for")) != len(_conditions(candidate_source, "for")):
                add("LOCAL_LOOP_RESTRUCTURE", "OBSERVED_LOOP_STRUCTURE_CHANGE")
            if re.search(r"\b(?:load|read).*(?:compute).*(?:store|write)\b", candidate_source, re.I | re.S):
                if re.search(r"#\s*pragma\s+HLS\s+DATAFLOW", candidate_source, re.I):
                    add("LOAD_COMPUTE_STORE_OVERLAP", "OBSERVED_LOAD_COMPUTE_STORE_OVERLAP")

        parent_signature = _function_signature(parent_source)
        candidate_signature = _function_signature(candidate_source)
        if parent_signature and candidate_signature and parent_signature != candidate_signature:
            unclassified.append("FUNCTION_SIGNATURE_CHANGED")
            reasons.append("INTERFACE_CHANGE_OBSERVED")

        evidence = evidence or {}
        subtype = str(
            evidence.get("failure_subtype")
            or evidence.get("bottleneck_subtype")
            or "UNKNOWN"
        ).upper()
        if not observed and subtype in _FAILURE_TO_STRATEGY:
            add(_FAILURE_TO_STRATEGY[subtype], "EVIDENCE_SUBTYPE_FALLBACK")
            confidence = 0.65
        elif observed:
            confidence = 0.95
        elif declared:
            observed.extend(declared)
            reasons.append("DECLARED_STRATEGY_FALLBACK")
            confidence = 0.35
        else:
            observed.append(_OTHER_BY_MODE[mode])
            reasons.append("UNCLASSIFIED_PATCH_FALLBACK")
            confidence = 0.20

        if declared and set(declared) != set(observed):
            reasons.append("DECLARED_OBSERVED_CONFLICT")
            confidence = min(confidence, 0.75 if any(item not in declared for item in observed) else confidence)
        if unclassified:
            confidence = min(confidence, 0.40)
        if validation and validation.get("patch_valid") is False:
            reasons.append("PATCH_INVALID_LIMITS_OBSERVATION")
            confidence = min(confidence, 0.50)

        added_count = len(added_lines)
        deleted_count = len(deleted_lines)
        changed = added_count + deleted_count
        complexity = "SMALL" if changed <= 10 else "MEDIUM" if changed <= 40 else "LARGE"
        digest = (
            hashlib.sha256(
                ("\n".join(line.rstrip() for line in patch.splitlines()).strip() + "\n").encode("utf-8")
            ).hexdigest()
            if patch
            else None
        )
        return StrategyNormalization(
            declared,
            tuple(observed[:3]),
            round(confidence, 6),
            tuple(dict.fromkeys(reasons)),
            tuple(unclassified[:8]),
            added_count,
            deleted_count,
            complexity,
            digest,
        )


def classify_algorithm_family(source: str, description: str = "") -> str:
    text = f"{description}\n{source}".casefold()
    rules = (
        ("MATRIX_MULTIPLICATION", ("matrix multiplication", "matmul", "gemm")),
        ("FIR_CONVOLUTION", ("fir", "finite impulse", "convolution")),
        ("STENCIL", ("stencil", "neighbor", "window")),
        ("HISTOGRAM", ("histogram", "bin count")),
        ("PREFIX_SUM", ("prefix sum", "scan")),
        ("COORDINATE_TRANSFORM", ("coordinate", "projection", "transform")),
        ("RESIDUAL_DATAFLOW", ("residual", "skip connection")),
        ("STREAM_PIPELINE", ("hls::stream", "dataflow", "fifo")),
        ("DOT_PRODUCT", ("dot product", "dotproduct", "a[i] * b[i]", "a[i]*b[i]")),
        ("REDUCTION", ("reduction", "sum +=", "acc +=", "total +=")),
        ("VECTOR_ELEMENTWISE", ("elementwise", "element-wise", "output[i]", "out[i]")),
    )
    for family, tokens in rules:
        if any(token in text for token in tokens):
            return family
    if re.search(r"\b[A-Za-z_]\w*\s*\[[^\]]+\]\s*=", source) and re.search(
        r"\bfor\s*\(", source
    ):
        return "VECTOR_ELEMENTWISE"
    return "OTHER"


def _top_signature_hash(source: str) -> str:
    signature = _function_signature(source)
    return hashlib.sha256((signature or "UNKNOWN_TOP_SIGNATURE").encode("utf-8")).hexdigest()


def task_family_hash(source: str, algorithm_family: str, structure: Mapping[str, object]) -> str:
    """Hash public structural shape, excluding task ID and mutation constants."""

    public_shape = {
        "algorithm_family": algorithm_family,
        "top_signature_hash": _top_signature_hash(source),
        "loop_count_bucket": structure.get("loop_count_bucket", "UNKNOWN"),
        "has_dataflow": bool(structure.get("has_dataflow")),
        "has_stream": bool(structure.get("has_stream")),
        "has_fifo": bool(structure.get("has_fifo")),
        "has_reduction": bool(structure.get("has_reduction")),
        "memory_access_pattern": structure.get("memory_access_pattern", "UNKNOWN"),
    }
    return canonical_sha256(public_shape)


def classify_failure_subtype(
    mode: str, evidence: Mapping[str, object], patch: str = ""
) -> str:
    text = " ".join(
        str(evidence.get(name) or "")
        for name in ("failure_type", "failure_kind", "error_summary", "failure_stage")
    ).casefold()
    patch_text = patch.casefold()
    if mode == "REPAIR":
        rules = (
            ("COMPILE_ERROR", ("compile", "compiler", "undeclared", "syntax error")),
            ("OUT_OF_BOUNDS", ("out of bounds", "out-of-bounds", "overflow index")),
            ("WRONG_LOOP_BOUND", ("loop bound", "last element", "off by one", "off-by-one")),
            ("WRONG_ARRAY_INDEX", ("array index", "wrong index")),
            ("OMITTED_TERM", ("omitted", "missing term")),
            ("WRONG_BRANCH_CONDITION", ("branch", "condition")),
            ("WRONG_SIGN", ("wrong sign", "sign error")),
            ("WRONG_COEFFICIENT", ("coefficient", "multiplier")),
            ("BAD_INITIALIZATION", ("initialization", "uninitialized")),
            ("BAD_ACCUMULATION", ("accumulation", "accumulator")),
            ("NUMERIC_CAST_OR_TRUNCATION", ("truncation", "numeric cast", "precision")),
        )
        for subtype, tokens in rules:
            if any(item in text for item in tokens):
                return subtype
        if re.search(r"for\s*\([^;]+;[^;]+(?:-\s*1|<=)", patch_text):
            return "OFF_BY_ONE"
        return "FUNCTIONAL_MISMATCH_OTHER" if text else "UNKNOWN"
    if mode == "SYNTH_FIX":
        rules = (
            ("DYNAMIC_ALLOCATION", ("dynamic allocation", "malloc", "new ", "free")),
            ("UNSUPPORTED_STL", ("std::vector", "unsupported stl", "std::function")),
            ("RECURSION", ("recursion", "recursive")),
            ("NON_STATIC_LOOP_BOUND", ("non-static loop", "loop bound")),
            ("UNSYNTHESIZABLE_TYPE", ("unsynthesizable type", "unsupported type")),
            ("RESOURCE_LIMIT", ("resource limit", "exceeds available")),
            ("CLOCK_CONSTRAINT", ("clock constraint", "timing violation")),
            ("DEPENDENCY_PREVENTS_SYNTHESIS", ("dependency", "dependence")),
            ("UNSUPPORTED_CALL", ("unsupported call", "cannot synthesize call")),
        )
        for subtype, tokens in rules:
            if any(item in text for item in tokens):
                return subtype
        return "SYNTHESIS_ERROR_OTHER" if text else "UNKNOWN"
    if mode == "STRUCTURAL_FIX":
        rules = (
            ("STREAM_COUNT_MISMATCH", ("stream count", "read/write count")),
            ("PRODUCER_BURST_DEADLOCK", ("producer burst", "burst deadlock")),
            ("CONSUMER_STARVATION", ("consumer starvation", "starvation")),
            ("FIFO_DEPTH_INSUFFICIENT", ("fifo depth", "fifo full")),
            ("DATAFLOW_DEPENDENCY_CYCLE", ("dependency cycle", "dataflow cycle")),
            ("PRODUCER_CONSUMER_RATE_MISMATCH", ("rate mismatch", "producer consumer")),
            ("STREAM_ORDER_MISMATCH", ("stream order", "read order", "write order")),
            ("INTERFACE_PROTOCOL_MISMATCH", ("interface protocol", "axi mismatch")),
            ("RTL_C_MISMATCH", ("rtl mismatch", "c/rtl mismatch", "expected")),
            ("COSIM_TIMEOUT_UNKNOWN", ("timeout", "timed out")),
        )
        for subtype, tokens in rules:
            if any(item in text for item in tokens):
                return subtype
        return "STRUCTURAL_ERROR_OTHER" if text else "UNKNOWN"
    return "UNKNOWN"


def infer_failure_subtype_from_observed(
    current: str, observed_strategy_atoms: Sequence[str]
) -> str:
    """Use an unambiguous observed fix to refine an OTHER/UNKNOWN subtype."""

    if current not in {
        "UNKNOWN",
        "FUNCTIONAL_MISMATCH_OTHER",
        "SYNTHESIS_ERROR_OTHER",
        "STRUCTURAL_ERROR_OTHER",
    }:
        return current
    candidates = {
        _OBSERVED_STRATEGY_TO_FAILURE[atom]
        for atom in observed_strategy_atoms
        if atom in _OBSERVED_STRATEGY_TO_FAILURE
    }
    return next(iter(candidates)) if len(candidates) == 1 else current


def classify_bottleneck_subtype(evidence: Mapping[str, object], source: str = "") -> str:
    text = " ".join(
        str(evidence.get(name) or "")
        for name in ("primary_bottleneck", "failure_type", "pipeline_status")
    ).casefold()
    if evidence.get("loop_ii") == 1 and any(item in text for item in ("latency", "interval", "loop")):
        return "HIGH_TRANSACTION_LATENCY_WITH_II_ONE"
    rules = (
        ("SERIAL_REDUCTION", ("serial reduction", "accumulation", "reduction")),
        ("MEMORY_PORT_LIMIT", ("memory port", "port limit")),
        ("MEMORY_BANKING_CONFLICT", ("bank conflict", "banking conflict")),
        ("LOW_PARALLEL_FACTOR", ("parallel factor", "par_factor")),
        ("LOOP_NOT_PIPELINED", ("not pipelined", "missing pipeline")),
        ("LOOP_II_LIMITED", ("loop ii", "initiation interval")),
        ("SEQUENTIAL_LOAD_COMPUTE_STORE", ("load compute store", "sequential stages")),
        ("DATA_DEPENDENCY", ("dependency", "dependence")),
        ("RESOURCE_OVERPROVISION", ("overprovision", "resource high")),
        ("RESOURCE_UNDERUTILIZATION", ("underutil", "resource low")),
        ("SUBOPTIMAL_LOOP_STRUCTURE", ("loop latency", "loop structure")),
    )
    for subtype, tokens in rules:
        if any(item in text for item in tokens):
            return subtype
    if re.search(r"\b(?:sum|acc|total)\s*\+=", source):
        return "SERIAL_REDUCTION"
    return "OPTIMIZATION_BOTTLENECK_OTHER" if text else "UNKNOWN"


def _structure_features(source: str, evidence: Mapping[str, object]) -> dict[str, object]:
    text = source.casefold()
    loop_count = len(re.findall(r"\bfor\s*\(", source)) + len(re.findall(r"\bwhile\s*\(", source))
    pressure = str(evidence.get("resource_pressure") or "UNKNOWN").upper()
    pressure = pressure if pressure in {"LOW", "MEDIUM", "HIGH", "UNKNOWN"} else "UNKNOWN"
    if "hls::stream" in text:
        memory_pattern = "STREAMING"
    elif re.search(r"\[[^\]]*\bi\b[^\]]*\]", source):
        memory_pattern = "SEQUENTIAL"
    else:
        memory_pattern = "UNKNOWN"
    return {
        "top_signature_hash": _top_signature_hash(source),
        "loop_count_bucket": numeric_bucket(loop_count),
        "critical_loop_ii": evidence.get("loop_ii") if isinstance(evidence.get("loop_ii"), int) else None,
        "critical_loop_trip_count_bucket": str(evidence.get("trip_count_bucket") or "UNKNOWN"),
        "transaction_interval_bucket": str(evidence.get("transaction_interval_bucket") or "UNKNOWN"),
        "latency_bucket": str(evidence.get("latency_bucket") or "UNKNOWN"),
        "has_pipeline": bool("#pragma hls pipeline" in text or evidence.get("pipeline_status") not in (None, "UNKNOWN", "NOT_PIPELINED")),
        "has_dataflow": bool("#pragma hls dataflow" in text or evidence.get("has_dataflow")),
        "has_stream": bool("hls::stream" in text or evidence.get("has_stream")),
        "has_fifo": bool("fifo" in text or "#pragma hls stream" in text or evidence.get("has_fifo")),
        "has_reduction": bool(re.search(r"\b(?:sum|acc|total)\s*(?:\+=|=.*\+)", text) or "reduction" in text),
        "has_dynamic_allocation": bool(re.search(r"\b(?:new|delete|malloc|calloc|realloc|free)\b", text)),
        "has_recursion": False,
        "has_unsupported_stl": bool(re.search(r"\bstd::(?:vector|map|list|deque|function)\b", text)),
        "memory_access_pattern": memory_pattern,
        "resource_pressure": {name: pressure for name in ("lut", "ff", "dsp", "bram")},
    }


def _status(value: object) -> str:
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL"
    return "NOT_RUN"


def _round_index(candidate_id: str, context: MigrationContext) -> int:
    if context.round_index is not None:
        return max(0, int(context.round_index))
    match = re.search(r"(?:candidate|proposal)_(\d+)", candidate_id)
    return int(match.group(1)) if match else 0


def migrate_v1_to_v2(
    record: Mapping[str, object],
    context: MigrationContext | None = None,
    *,
    normalizer: StrategyNormalizer | None = None,
) -> dict[str, object]:
    """Derive one immutable v2 record without mutating the v1 input."""

    v1 = validate_experience_record(record)
    ctx = context or MigrationContext()
    mode = str(v1["mode"])
    evidence = dict(v1["evidence_features"])
    proposal = dict(v1["proposal_features"])
    outcome = dict(v1["outcome"])
    source = ctx.candidate_source or ctx.parent_source
    algorithm = classify_algorithm_family(source, ctx.description)
    if algorithm == "OTHER":
        old_family = str(v1.get("algorithm_family") or "").upper()
        aliases = {
            "DOT_REDUCTION": "DOT_PRODUCT",
            "VECTOR_ELEMENTWISE": "VECTOR_ELEMENTWISE",
            "STREAM_PIPELINE": "STREAM_PIPELINE",
            "CONVOLUTION_STENCIL": "STENCIL",
            "MATRIX_ALGEBRA": "MATRIX_MULTIPLICATION",
        }
        algorithm = aliases.get(old_family, "OTHER")
    if algorithm not in ALGORITHM_FAMILIES:
        algorithm = "OTHER"
    failure_subtype = classify_failure_subtype(mode, evidence, ctx.patch)
    bottleneck_subtype = (
        classify_bottleneck_subtype(evidence, source) if mode == "OPTIMIZE" else "UNKNOWN"
    )
    normalization = (normalizer or StrategyNormalizer()).normalize(
        mode=mode,
        declared_strategy=proposal.get("strategy_bundle"),
        parent_source=ctx.parent_source,
        candidate_source=ctx.candidate_source,
        patch=ctx.patch,
        evidence={
            **evidence,
            "failure_subtype": failure_subtype,
            "bottleneck_subtype": bottleneck_subtype,
        },
        validation={"patch_valid": outcome.get("patch_valid")},
    )
    failure_subtype = infer_failure_subtype_from_observed(
        failure_subtype, normalization.observed_strategy_atoms
    )
    if not ctx.patch:
        total = int(proposal.get("patch_lines") or 0)
        normalization = StrategyNormalization(
            normalization.declared_strategy_bundle,
            normalization.observed_strategy_atoms,
            min(normalization.confidence, 0.35),
            tuple(dict.fromkeys((*normalization.reason_codes, "PATCH_TEXT_UNAVAILABLE"))),
            normalization.unclassified_changes,
            total,
            0,
            "SMALL" if total <= 10 else "MEDIUM" if total <= 40 else "LARGE",
            proposal.get("normalized_patch_hash") if isinstance(proposal.get("normalized_patch_hash"), str) else None,
        )
    structure = _structure_features(source, evidence)
    task_family = task_family_hash(source, algorithm, structure)
    total_tokens = int(outcome.get("tokens") or 0)
    input_tokens = total_tokens if ctx.input_tokens is None else max(0, ctx.input_tokens)
    output_tokens = 0 if ctx.output_tokens is None else max(0, ctx.output_tokens)
    if input_tokens + output_tokens != total_tokens:
        total_tokens = input_tokens + output_tokens
    final_pass = outcome.get("final_pass")
    candidate_created = outcome.get("candidate_created") is True
    patch_valid = outcome.get("patch_valid") is True
    failure_stage = str(outcome.get("failure_stage") or "NONE")
    real = v1.get("execution_class") == "REAL_LLM_VITIS"
    allowed_split = v1.get("task_split") in {"train", "dev", "hidden_like"}
    eligible_retrieval = bool(real and allowed_split and candidate_created and patch_valid)
    eligible_ranking = bool(eligible_retrieval and v1.get("eligible_for_ranking") is True)
    exclusions: list[str] = []
    if not real:
        exclusions.append("NON_REAL_EVIDENCE")
    if not allowed_split:
        exclusions.append("UNSUPPORTED_SPLIT")
    if not candidate_created:
        exclusions.append("CANDIDATE_NOT_CREATED")
    if not patch_valid:
        exclusions.append("PATCH_INVALID")
    if not v1.get("eligible_for_ranking"):
        exclusions.append("V1_NOT_RANKING_ELIGIBLE")
    refs = [dict(item) for item in v1.get("artifact_refs", [])]
    body: dict[str, object] = {
        "schema_version": EXPERIENCE_V2_SCHEMA,
        "record_id": "",
        "taxonomy_versions": {
            "failure": FAILURE_TAXONOMY_SCHEMA,
            "bottleneck": BOTTLENECK_TAXONOMY_SCHEMA,
            "strategy": STRATEGY_TAXONOMY_SCHEMA,
        },
        "source": {
            "run_id": v1["run_id"],
            "candidate_id": v1["candidate_id"],
            "round_index": _round_index(str(v1["candidate_id"]), ctx),
            "provider": ctx.provider,
            "model": ctx.model,
            "prompt_version": ctx.prompt_version,
            "toolchain": ctx.toolchain,
            "backend_fingerprint": ctx.backend_fingerprint,
            "evidence_level": v1["execution_class"],
            "task_split": v1["task_split"] if allowed_split else "train",
            "task_family_hash": task_family,
            "algorithm_family": algorithm,
            "difficulty": int(v1["difficulty"]),
        },
        "problem": {
            "mode": mode,
            "failure_stage": failure_stage,
            "failure_subtype": failure_subtype,
            "primary_bottleneck": str(evidence.get("primary_bottleneck") or "UNKNOWN"),
            "bottleneck_subtype": bottleneck_subtype,
            "numeric_semantics": "UNKNOWN",
            "requires_cosim": bool(
                ctx.requires_cosim if ctx.requires_cosim is not None else mode == "STRUCTURAL_FIX"
            ),
        },
        "structure_features": structure,
        "strategy": normalization.to_dict(),
        "validation": {
            "patch_valid": patch_valid,
            "interface_guard_pass": bool(patch_valid and candidate_created),
            "candidate_created": candidate_created,
            "csim_status": _status(outcome.get("csim_pass")),
            "synth_status": _status(outcome.get("synth_pass")),
            "cosim_status": str(outcome.get("cosim_status") or "NOT_RUN"),
            "fresh_final_status": _status(final_pass),
            "promoted": outcome.get("promoted") is True,
            "rejected": bool(candidate_created and outcome.get("promoted") is not True),
            "failure_reason": None if failure_stage == "NONE" else failure_stage,
        },
        "performance": {
            "latency_before": outcome.get("latency_before"),
            "latency_after": outcome.get("latency_after"),
            "transaction_interval_before": None,
            "transaction_interval_after": None,
            "acceleration": outcome.get("acceleration"),
            "resource_delta": {},
            "strict_improvement": bool(
                outcome.get("latency_before") is not None
                and outcome.get("latency_after") is not None
                and float(outcome["latency_after"]) < float(outcome["latency_before"])
            ),
        },
        "cost": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "credits": int(outcome.get("credits") or 0),
            "csim_calls": int(outcome.get("csim_pass") is not None) + int(final_pass is True),
            "synth_calls": int(outcome.get("synth_pass") is not None) + int(final_pass is True),
            "cosim_calls": int(outcome.get("cosim_status") in {"PASS", "FAIL"}),
            "wall_time_seconds": float(outcome.get("wall_time_seconds") or 0.0),
        },
        "token_policy": {
            **{
                "run_token_limit": 0,
                "tokens_remaining_before_call": 0,
                "estimated_base_prompt_tokens": 0,
                "estimated_guidance_tokens": 0,
                "estimated_input_tokens": 0,
                "configured_max_output_tokens": 0,
                "effective_max_output_tokens": 0,
                "actual_input_tokens": ctx.input_tokens,
                "actual_output_tokens": ctx.output_tokens,
                "actual_total_tokens": (
                    ctx.input_tokens + ctx.output_tokens
                    if ctx.input_tokens is not None
                    and ctx.output_tokens is not None
                    else None
                ),
                "context_window_tokens": 0,
                "future_round_token_reserve": 0,
                "guidance_token_cap": 0,
                "guidance_actual_tokens": 0,
                "rounds_remaining": 0,
                "token_pressure": "LOW",
                "finish_reason": None,
                "output_truncated": False,
                "truncation_reason": None,
                "estimator_name": "legacy-artifact-unavailable",
                "estimator_version": "v1",
                "token_policy_version": "v3.token-policy.v1",
            },
            **dict(ctx.token_policy or {}),
        },
        "provenance": {
            "artifact_refs": refs,
            "artifact_hashes": [str(item["sha256"]) for item in refs],
            "source_record_hash": str(v1["record_id"]),
            "eligible_for_retrieval": eligible_retrieval,
            "eligible_for_ranking": eligible_ranking,
            "exclusion_reasons": exclusions,
        },
    }
    return seal_experience_v2(body)


__all__ = [
    "MigrationContext",
    "StrategyNormalization",
    "StrategyNormalizer",
    "classify_algorithm_family",
    "classify_bottleneck_subtype",
    "classify_failure_subtype",
    "infer_failure_subtype_from_observed",
    "migrate_v1_to_v2",
    "task_family_hash",
]
