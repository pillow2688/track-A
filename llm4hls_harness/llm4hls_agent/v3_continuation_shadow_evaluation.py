"""Fixed-protocol online Shadow evaluation for Continuation V2.

Only real Vitis runs with the V2 policy in ``shadow`` mode are accepted.  The
policy sees the hash-bound decision-time ``pre_state``; subsequent Candidate
outcomes are joined only after both V1 and V2 decisions have been computed.
An enforce admission manifest is emitted only when every frozen Gate passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .v3_continuation import continuation_cost, continuation_decision
from .v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_FIXED_THRESHOLDS,
    CONTINUATION_POLICY_VERSION,
    validate_continuation_admission,
)
from .v3_continuation_v2 import (
    CONTINUATION_DECISION_SCHEMA_V2,
    canonical_sha256,
    continuation_decision_v2,
)


SHADOW_EVALUATION_SCHEMA = "v3.continuation-shadow-evaluation.v1"
SHADOW_SAMPLE_SCHEMA = "v3.continuation-shadow-sample.v1"
MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
BENEFICIAL = {
    "BENEFICIAL_PERFORMANCE",
    "BENEFICIAL_AREA",
    "BENEFICIAL_BALANCED_PA",
}
WASTE = {"NEUTRAL", "HARMFUL"}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _read_object(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"expected JSON object: {path}")
    return decoded


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _is_allow(value: Mapping[str, object]) -> bool:
    return value.get("decision") == "ALLOW"


def _v1_from_pre_state(
    *, mode: str, round_index: int, pre_state: Mapping[str, object]
) -> dict[str, object]:
    """Project the common frozen pre-state into the legacy V1 rule."""

    delta_raw = _mapping(pre_state.get("evidence_delta"))
    actionable = bool(delta_raw.get("new_actionable_evidence"))
    semantic_count = sum(
        bool(delta_raw.get(name))
        for name in (
            "failure_subtype_changed",
            "failure_location_changed",
            "bottleneck_changed",
            "topology_understanding_improved",
            "latency_improved",
            "interval_improved",
            "clock_improved",
            "resource_pressure_changed",
        )
    )
    delta = {
        "has_new_evidence": semantic_count > 0,
        "has_actionable_new_evidence": actionable,
        "failure_subtype_changed": bool(
            delta_raw.get("failure_subtype_changed")
        ),
        "bottleneck_changed": bool(delta_raw.get("bottleneck_changed")),
        "evidence_strength": (
            "HIGH"
            if actionable and semantic_count >= 2
            else "MEDIUM"
            if actionable
            else "LOW"
            if semantic_count
            else "NONE"
        ),
        "reason_codes": [],
    }
    history = pre_state.get("observed_strategy_history")
    history = history if isinstance(history, Sequence) else ()
    current = pre_state.get("current_observed_strategy")
    current = current if isinstance(current, Sequence) else ()
    normalized_current = tuple(sorted(str(item).upper() for item in current))
    normalized_history = {
        tuple(sorted(str(item).upper() for item in group))
        for group in history
        if isinstance(group, Sequence) and not isinstance(group, (str, bytes))
    }
    duplicate = bool(normalized_current and normalized_current in normalized_history)
    novelty = str(pre_state.get("strategy_novelty") or "UNKNOWN").upper()
    strategies = {
        "untried_matched_strategy_atoms": (
            list(normalized_current)
            if normalized_current and not duplicate and novelty != "NONE"
            else []
        ),
        "duplicate_strategy": duplicate
        or bool(delta_raw.get("same_observed_strategy")),
        "duplicate_patch": False,
        "reason_codes": [],
    }
    performance_area = {
        "latency_improved": bool(delta_raw.get("latency_improved")),
        "transaction_interval_improved": bool(
            delta_raw.get("interval_improved")
        ),
        "clock_improved": bool(delta_raw.get("clock_improved")),
        "area_proxy_improved": False,
        "performance_area_proxy_improved": False,
        "resource_pressure_changed": bool(
            delta_raw.get("resource_pressure_changed")
        ),
        "tradeoff_detected": False,
        "pareto_relation": "UNKNOWN",
    }
    cost = continuation_cost(
        ledger={
            "tokens_remaining": pre_state.get("remaining_tokens"),
            "credits_remaining": pre_state.get("remaining_credits"),
        },
        estimated_input_tokens=int(
            pre_state.get("estimated_next_tokens") or 0
        ),
        estimated_output_tokens=0,
        estimated_credits=int(
            pre_state.get("estimated_next_credits") or 0
        ),
        estimated_wall_time_seconds=0.0,
        final_reserve_safe=bool(
            pre_state.get("final_reserve_available")
        ),
    )
    return continuation_decision(
        run_id="fixed-shadow-evaluation",
        round_index=round_index,
        mode=mode,
        policy_mode="shadow",
        has_correct_candidate=bool(
            pre_state.get("has_verified_incumbent")
        ),
        has_strict_latency_improvement=bool(
            delta_raw.get("latency_improved")
        ),
        performance_area=performance_area,
        delta=delta,
        strategies=strategies,
        cost=cost,
        remaining_rounds=int(pre_state.get("remaining_rounds") or 1),
    )


def _outcome_label(
    *,
    mode: str,
    pre_state: Mapping[str, object],
    candidate: Mapping[str, object],
    previous_latency: float | None,
) -> str:
    decision = str(candidate.get("decision") or "")
    latency = candidate.get("latency_worst")
    current_latency = (
        float(latency)
        if isinstance(latency, (int, float)) and not isinstance(latency, bool)
        else None
    )
    if (
        decision in {"FINAL_VERIFIED", "CORRECTNESS_VERIFIED", "PROMOTED"}
        and not bool(pre_state.get("has_verified_incumbent"))
    ):
        return "ESSENTIAL_FOR_CORRECTNESS"
    if (
        current_latency is not None
        and previous_latency is not None
        and current_latency < previous_latency
    ):
        return "BENEFICIAL_PERFORMANCE"
    risk = _mapping(candidate.get("risk_decision"))
    if decision.startswith("REJECTED") or str(risk.get("level", "")).upper() == "HIGH":
        return "HARMFUL"
    if mode == "STRUCTURAL_FIX" and decision == "FINAL_VERIFIED":
        return "ESSENTIAL_FOR_CORRECTNESS"
    return "NEUTRAL"


def collect_online_shadow_samples(
    run_roots: Iterable[str | Path],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Collect hash-checked follow-up samples from explicit public run roots."""

    samples: list[dict[str, object]] = []
    excluded: Counter[str] = Counter()
    seen: set[tuple[str, int]] = set()
    for root_value in run_roots:
        root = Path(root_value).expanduser().resolve()
        result_path = root / "v3_prototype_result.json"
        if not result_path.is_file():
            excluded["MISSING_TERMINAL_RESULT"] += 1
            continue
        result = _read_object(result_path)
        backend = _mapping(result.get("backend"))
        if (
            backend.get("evidence_level")
            not in {"REAL_VITIS_VALIDATED", "REAL_VITIS_ATTEMPT_FAILED"}
            or not str(backend.get("class", "")).endswith(".VitisBackend")
        ):
            excluded["NOT_REAL_VITIS"] += 1
            continue
        if (
            result.get("continuation_policy_mode") != "shadow"
            or result.get("continuation_policy_version") != "v2"
        ):
            excluded["NOT_V2_SHADOW"] += 1
            continue
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id or "hidden" in task_id.lower():
            excluded["HIDDEN_OR_INVALID_TASK"] += 1
            continue
        mode = str(result.get("mode") or "")
        if mode not in MODES:
            excluded["UNKNOWN_MODE"] += 1
            continue
        candidates = result.get("candidate_rounds")
        candidates = candidates if isinstance(candidates, list) else []
        candidate_by_round = {
            int(item["round"]): item
            for item in candidates
            if isinstance(item, Mapping)
            and isinstance(item.get("round"), int)
        }
        previous_latency: float | None = None
        for round_index in sorted(candidate_by_round):
            if round_index <= 1:
                latency = candidate_by_round[round_index].get("latency_worst")
                if isinstance(latency, (int, float)) and not isinstance(
                    latency, bool
                ):
                    previous_latency = float(latency)
                continue
            key = (str(root), round_index)
            if key in seen:
                excluded["DUPLICATE"] += 1
                continue
            gate_ref = None
            gate_sha = None
            for row in result.get("planner_call_gates", []):
                if isinstance(row, Mapping) and row.get("round") == round_index:
                    gate_ref, gate_sha = row.get("ref"), row.get("sha256")
                    break
            if not isinstance(gate_ref, str) or not isinstance(gate_sha, str):
                excluded["MISSING_GATE_BINDING"] += 1
                continue
            gate_path = (root / gate_ref).resolve()
            try:
                gate_path.relative_to(root)
            except ValueError:
                excluded["UNSAFE_GATE_REF"] += 1
                continue
            if (
                not gate_path.is_file()
                or hashlib.sha256(gate_path.read_bytes()).hexdigest() != gate_sha
            ):
                excluded["GATE_HASH_MISMATCH"] += 1
                continue
            gate = _read_object(gate_path)
            pre_state = _mapping(gate.get("pre_state"))
            if (
                gate.get("schema_version") != CONTINUATION_DECISION_SCHEMA_V2
                or gate.get("policy_version") != CONTINUATION_POLICY_VERSION
                or not pre_state
            ):
                excluded["INVALID_V2_GATE"] += 1
                continue
            leakage = int(
                any(
                    name in pre_state
                    for name in (
                        "outcome",
                        "final_result",
                        "future_candidate",
                        "actual_followup",
                    )
                )
            )
            recomputed_v2 = continuation_decision_v2(
                mode=mode,
                pre_state=pre_state,
            )
            stored_projection = {
                key: gate.get(key)
                for key in (
                    "schema_version",
                    "mode",
                    "decision",
                    "confidence",
                    "reason_codes",
                    "supporting_evidence",
                    "fallback",
                    "decision_digest",
                )
            }
            if stored_projection != recomputed_v2:
                excluded["V2_RECOMPUTE_MISMATCH"] += 1
                continue
            v1 = _v1_from_pre_state(
                mode=mode,
                round_index=round_index,
                pre_state=pre_state,
            )
            candidate = candidate_by_round[round_index]
            label = _outcome_label(
                mode=mode,
                pre_state=pre_state,
                candidate=candidate,
                previous_latency=previous_latency,
            )
            samples.append(
                {
                    "schema_version": SHADOW_SAMPLE_SCHEMA,
                    "sample_id": canonical_sha256(
                        {
                            "run": str(root),
                            "round": round_index,
                            "gate_sha256": gate_sha,
                        }
                    ),
                    "task_id": task_id,
                    "mode": mode,
                    "round_index": round_index,
                    "source": {
                        "kind": "REAL_DEEPSEEK_VITIS_SHADOW",
                        "run_root": str(root),
                        "gate_ref": gate_ref,
                        "gate_sha256": gate_sha,
                    },
                    "pre_state": dict(pre_state),
                    "v1_decision": v1,
                    "v2_decision": recomputed_v2,
                    "outcome": {
                        "label": label,
                        "candidate_id": candidate.get("candidate_id"),
                        "candidate_decision": candidate.get("decision"),
                        "latency_worst": candidate.get("latency_worst"),
                    },
                    "leakage_violations": leakage,
                }
            )
            seen.add(key)
            latency = candidate.get("latency_worst")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                previous_latency = float(latency)
    return samples, dict(sorted(excluded.items()))


def evaluate_shadow_samples(
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Evaluate frozen Gate metrics after joining post-decision outcomes."""

    mode_counts = {mode: 0 for mode in MODES}
    beneficial_total = beneficial_v1 = beneficial_v2 = 0
    waste_total = waste_v1 = waste_v2 = 0
    false_blocks = structural_total = structural_retained = leakage = 0
    audited: list[dict[str, object]] = []
    for sample in samples:
        mode = str(sample.get("mode") or "")
        if mode not in mode_counts:
            raise ValueError("shadow sample has unsupported mode")
        v1 = _mapping(sample.get("v1_decision"))
        v2 = _mapping(sample.get("v2_decision"))
        outcome = _mapping(sample.get("outcome"))
        label = str(outcome.get("label") or "")
        if label not in BENEFICIAL | WASTE | {"ESSENTIAL_FOR_CORRECTNESS"}:
            raise ValueError("shadow sample has unsupported outcome")
        mode_counts[mode] += 1
        v1_allow, v2_allow = _is_allow(v1), _is_allow(v2)
        if label in BENEFICIAL:
            beneficial_total += 1
            beneficial_v1 += int(v1_allow)
            beneficial_v2 += int(v2_allow)
            false_blocks += int(not v2_allow)
        elif label in WASTE:
            waste_total += 1
            waste_v1 += int(not v1_allow)
            waste_v2 += int(not v2_allow)
        else:
            false_blocks += int(not v2_allow)
            if mode == "STRUCTURAL_FIX":
                structural_total += 1
                structural_retained += int(v2_allow)
        leakage += int(sample.get("leakage_violations") or 0)
        audited.append(
            {
                "sample_id": sample.get("sample_id"),
                "mode": mode,
                "v1": v1.get("decision"),
                "v2": v2.get("decision"),
                "outcome": label,
            }
        )
    metrics = {
        "mode_counts": mode_counts,
        "beneficial_retention": _rate(beneficial_v2, beneficial_total),
        "v1_beneficial_retention": _rate(beneficial_v1, beneficial_total),
        "waste_block_rate": _rate(waste_v2, waste_total),
        "v1_waste_block_rate": _rate(waste_v1, waste_total),
        "false_blocks": false_blocks,
        "structural_essential_samples": structural_total,
        "structural_essential_retention": _rate(
            structural_retained, structural_total
        ),
        "leakage_violations": leakage,
    }
    failures: list[str] = []
    if any(
        count < CONTINUATION_FIXED_THRESHOLDS["minimum_samples_per_mode"]
        for count in mode_counts.values()
    ):
        failures.append("PER_MODE_SAMPLE_GATE")
    if false_blocks > CONTINUATION_FIXED_THRESHOLDS["maximum_false_blocks"]:
        failures.append("FALSE_BLOCK_GATE")
    if (
        structural_total
        < CONTINUATION_FIXED_THRESHOLDS[
            "minimum_structural_essential_samples"
        ]
    ):
        failures.append("STRUCTURAL_ESSENTIAL_SAMPLE_GATE")
    if (
        metrics["structural_essential_retention"]
        < CONTINUATION_FIXED_THRESHOLDS[
            "minimum_structural_essential_retention"
        ]
    ):
        failures.append("STRUCTURAL_ESSENTIAL_RETENTION_GATE")
    if metrics["beneficial_retention"] < metrics["v1_beneficial_retention"]:
        failures.append("BENEFICIAL_RETENTION_REGRESSION")
    if metrics["waste_block_rate"] < metrics["v1_waste_block_rate"]:
        failures.append("WASTE_BLOCK_RATE_REGRESSION")
    if leakage > CONTINUATION_FIXED_THRESHOLDS["maximum_leakage_violations"]:
        failures.append("LEAKAGE_GATE")
    evidence_sha256 = canonical_sha256(audited)
    return {
        "schema_version": SHADOW_EVALUATION_SCHEMA,
        "protocol": "MODE_SPECIFIC_PRE_STATE_ONLINE_SHADOW",
        "decision": "PASS" if not failures else "FAIL",
        "thresholds": CONTINUATION_FIXED_THRESHOLDS,
        "metrics": metrics,
        "gate_failures": failures,
        "evidence_sha256": evidence_sha256,
        "audited_samples": audited,
    }


def evaluate_online_shadow_runs(
    run_roots: Iterable[str | Path],
    *,
    output_path: str | Path | None = None,
    admission_path: str | Path | None = None,
) -> dict[str, object]:
    samples, excluded = collect_online_shadow_samples(run_roots)
    evaluation = evaluate_shadow_samples(samples)
    evaluation["sample_count"] = len(samples)
    evaluation["excluded"] = excluded
    evaluation["samples_sha256"] = canonical_sha256(samples)
    if output_path is not None:
        Path(output_path).write_text(
            json.dumps(evaluation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if admission_path is not None:
        if evaluation["decision"] != "PASS":
            raise ValueError(
                "Continuation admission is forbidden because the Shadow Gate failed"
            )
        admission = {
            "schema_version": CONTINUATION_ADMISSION_SCHEMA,
            "decision": "PASS",
            "policy_version": CONTINUATION_POLICY_VERSION,
            "protocol": "MODE_SPECIFIC_PRE_STATE_ONLINE_SHADOW",
            "thresholds": CONTINUATION_FIXED_THRESHOLDS,
            "metrics": evaluation["metrics"],
            "evidence_sha256": evaluation["evidence_sha256"],
        }
        validate_continuation_admission(admission)
        Path(admission_path).write_text(
            json.dumps(admission, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--admission")
    args = parser.parse_args(argv)
    result = evaluate_online_shadow_runs(
        args.run_root,
        output_path=args.output,
        admission_path=args.admission,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["decision"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
