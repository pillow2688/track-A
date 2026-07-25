"""Fail-closed contracts for C1.1 Shadow isolation and offline replay.

This module has no Graph, Planner, provider, Candidate, BudgetLedger or HLS
tool dependency.  It records the boundary that an offline Experience/Ranker
experiment must satisfy before it can be present in a formal matrix run.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from .v3_experience_kb import QUERY_SAFE_FIELDS, QUERY_STRUCTURE_SAFE_FIELDS


SHADOW_BOUNDARY_SCHEMA = "v3e.shadow-boundary.c1.1.v1"

RECORD_ONLY_LABEL_SECTIONS = frozenset(
    {"validation", "performance", "cost", "token_policy"}
)
FORBIDDEN_QUERY_LABELS = frozenset(
    {
        "outcome",
        "promoted",
        "rejected",
        "fresh_final_status",
        "strict_improvement",
        "acceleration",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "credits",
        "wall_time_seconds",
    }
)

LABEL_LAYERS: Mapping[str, tuple[str, ...]] = {
    "L0_PRE_DECISION_PUBLIC_FEATURES": (
        "mode",
        "failure_subtype",
        "bottleneck_subtype",
        "algorithm_family",
        "structure_features",
        "strategy_context",
        "frozen_runtime_identity",
    ),
    "L1_HISTORICAL_SUPPORT_LABELS": (
        "validation",
        "performance",
        "cost",
        "token_policy",
    ),
    "L2_HELD_OUT_EVALUATION_LABELS": (
        "fresh_final_status",
        "strict_improvement",
        "harmful_recommendation",
        "cost",
    ),
    "L3_POST_TERMINAL_ATTRIBUTION": (
        "candidate_outcome",
        "terminal_budget",
        "artifact_provenance",
    ),
}

COST_SCOPE: Mapping[str, tuple[str, ...]] = {
    "QUERY_ALLOWED": (),
    "HISTORICAL_SUPPORT_ONLY": (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "credits",
        "csim_calls",
        "synth_calls",
        "cosim_calls",
        "wall_time_seconds",
    ),
    "HELD_OUT_EVALUATION_ONLY": (
        "held_out_cost",
        "held_out_utility",
    ),
    "CURRENT_RUN_AUTHORITY": (
        "BudgetLedger",
        "TokenBudgetPolicy",
        "tool_limits",
    ),
}


class ProvenancePool(str, Enum):
    TRAIN_SUPPORT = "TRAIN_SUPPORT"
    TRAIN_OBSERVATION_ONLY = "TRAIN_OBSERVATION_ONLY"
    DEV_EVALUATION_ONLY = "DEV_EVALUATION_ONLY"
    HIDDEN_LIKE_EVALUATION_ONLY = "HIDDEN_LIKE_EVALUATION_ONLY"
    CURRENT_MATRIX_POST_TERMINAL_ONLY = "CURRENT_MATRIX_POST_TERMINAL_ONLY"
    FIXTURE_EXCLUDED = "FIXTURE_EXCLUDED"
    QUARANTINE = "QUARANTINE"


def classify_provenance(
    *,
    evidence_level: str,
    task_split: str,
    eligible_for_ranking: bool,
    current_matrix_record: bool = False,
) -> ProvenancePool:
    """Assign one record to exactly one non-overlapping protocol pool."""

    if current_matrix_record:
        return ProvenancePool.CURRENT_MATRIX_POST_TERMINAL_ONLY
    if evidence_level != "REAL_LLM_VITIS":
        return ProvenancePool.FIXTURE_EXCLUDED
    if task_split == "hidden_like":
        return ProvenancePool.HIDDEN_LIKE_EVALUATION_ONLY
    if task_split == "dev":
        return ProvenancePool.DEV_EVALUATION_ONLY
    if task_split == "train" and eligible_for_ranking:
        return ProvenancePool.TRAIN_SUPPORT
    if task_split == "train":
        return ProvenancePool.TRAIN_OBSERVATION_ONLY
    return ProvenancePool.QUARANTINE


def query_boundary_report(
    *,
    top_level_fields: set[str] | frozenset[str],
    structure_fields: set[str] | frozenset[str],
) -> dict[str, object]:
    top = set(top_level_fields)
    structure = set(structure_fields)
    top_extra = sorted(top.difference(QUERY_SAFE_FIELDS))
    structure_extra = sorted(structure.difference(QUERY_STRUCTURE_SAFE_FIELDS))
    forbidden = sorted(
        (top | structure).intersection(
            RECORD_ONLY_LABEL_SECTIONS | FORBIDDEN_QUERY_LABELS
        )
    )
    return {
        "schema_version": SHADOW_BOUNDARY_SCHEMA,
        "top_level_whitelist_exact": not top_extra,
        "structure_whitelist_exact": not structure_extra,
        "top_level_extra": top_extra,
        "structure_extra": structure_extra,
        "forbidden_labels_present": forbidden,
        "safe": not top_extra and not structure_extra and not forbidden,
    }


def formal_matrix_shadow_decision(
    *,
    fixed_provider_request_equal: bool,
    dynamic_provider_request_equal: bool,
    dispatch_context_equal: bool,
    planner_fingerprint_equal: bool,
    graph_route_equal: bool,
    ranker_runtime_imported: bool,
) -> dict[str, object]:
    """Return the fail-closed formal-matrix configuration."""

    experience_equivalent = all(
        (
            fixed_provider_request_equal,
            dynamic_provider_request_equal,
            dispatch_context_equal,
            planner_fingerprint_equal,
            graph_route_equal,
        )
    )
    ranker_non_interference = not ranker_runtime_imported
    admitted = experience_equivalent and ranker_non_interference
    return {
        "schema_version": SHADOW_BOUNDARY_SCHEMA,
        "experience_full_equivalence": experience_equivalent,
        "ranker_v2_non_interference_by_non_integration": ranker_non_interference,
        "runtime_shadow_admitted": admitted,
        "formal_matrix": {
            "continuation": "off",
            "experience": "shadow" if admitted else "off",
            "ranker": "shadow" if admitted else "off",
        },
        "post_matrix": {
            "experience": "offline_replay",
            "ranker": "fixed_protocol_offline_reevaluation",
        },
        "reason": (
            "FULL_EQUIVALENCE_PROVED"
            if admitted
            else "FAIL_CLOSED_SHADOW_NOT_FULLY_EQUIVALENT"
        ),
    }


__all__ = [
    "COST_SCOPE",
    "FORBIDDEN_QUERY_LABELS",
    "LABEL_LAYERS",
    "ProvenancePool",
    "RECORD_ONLY_LABEL_SECTIONS",
    "SHADOW_BOUNDARY_SCHEMA",
    "classify_provenance",
    "formal_matrix_shadow_decision",
    "query_boundary_report",
]
