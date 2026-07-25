#!/usr/bin/env python3
"""Replay C0 V1/V2 on follow-up decisions from the formal 28x1 matrix."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Mapping


ARTIFACT_DIR = Path(__file__).resolve().parent
REPO = ARTIFACT_DIR.parents[3]
HARNESS = REPO / "llm4hls_harness"
MATRIX = (
    HARNESS
    / "experiments/formal_matrix_20260724_4a05763_deepseek_28x1"
)
C0_DIR = (
    REPO
    / "docs/experiments/artifacts/2026-07-23-phase-c0-continuation-v2"
)


def load_c0() -> ModuleType:
    path = C0_DIR / "phase_c0_offline_replay.py"
    spec = importlib.util.spec_from_file_location("phase_c0_frozen", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen C0 evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def candidates(registry: Mapping[str, object]) -> list[dict[str, object]]:
    raw = registry.get("candidates")
    if isinstance(raw, dict):
        return [
            dict(value)
            for _, value in sorted(raw.items())
            if isinstance(value, dict)
        ]
    if isinstance(raw, list):
        return [dict(value) for value in raw if isinstance(value, dict)]
    return []


def candidate_for_round(
    registry: Mapping[str, object], round_index: int
) -> dict[str, object] | None:
    found = [
        item
        for item in candidates(registry)
        if item.get("round_index") == round_index
    ]
    if len(found) > 1:
        raise RuntimeError("multiple candidates bind one Planner round")
    return found[0] if found else None


def candidate_metrics(
    c0: ModuleType,
    run_dir: Path,
    candidate: Mapping[str, object] | None,
) -> Mapping[str, object]:
    if candidate is None:
        return {}
    return c0.report(
        run_dir,
        candidate.get("final_metrics_ref") or candidate.get("metrics_ref"),
    )


def outcome_for_followup(
    c0: ModuleType,
    run_dir: Path,
    *,
    mode: str,
    before_metrics: Mapping[str, object],
    candidate: Mapping[str, object] | None,
    proposal: Mapping[str, object],
) -> dict[str, object]:
    after_metrics = candidate_metrics(c0, run_dir, candidate)
    status = str(candidate.get("status", "")) if candidate else "NOT_MATERIALIZED"
    promoted = status in {"PROMOTED", "FINAL_VERIFIED"}
    before_latency = c0.latency(before_metrics)
    after_latency = c0.latency(after_metrics)
    strict_improvement = bool(
        before_latency is not None
        and after_latency is not None
        and float(after_latency) < float(before_latency)
    )
    if promoted and mode == "OPTIMIZE" and strict_improvement:
        outcome_class = "BENEFICIAL_PERFORMANCE"
    elif promoted and mode == "STRUCTURAL_FIX":
        outcome_class = "ESSENTIAL_STRUCTURAL"
    elif promoted and mode in {"REPAIR", "SYNTH_FIX"}:
        outcome_class = "BENEFICIAL_CORRECTNESS"
    else:
        outcome_class = "HARMFUL"
    token_cost = int(proposal.get("input_tokens") or 0) + int(
        proposal.get("output_tokens") or 0
    )
    credit_cost = int(candidate.get("credits_used") or 0) if candidate else 0
    return {
        "class": outcome_class,
        "candidate_promoted": promoted,
        "failure_changed": None,
        "latency_delta": (
            float(after_latency) - float(before_latency)
            if before_latency is not None and after_latency is not None
            else None
        ),
        "ii_delta": None,
        "area_delta": None,
        "token_cost": token_cost,
        "credit_cost": credit_cost,
        "binding": {
            "candidate_status": status,
            "candidate_materialized": candidate is not None,
            "strict_latency_improvement": strict_improvement,
        },
    }


def matrix_rows(
    c0: ModuleType,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    for run_dir in sorted((MATRIX / "runs").iterdir()):
        if not run_dir.is_dir():
            continue
        task_spec = read_json(run_dir / "v3_task_spec.json")
        task_id = str(task_spec["task_id"])
        round_two = run_dir / "planner/inputs/round_002.json"
        terminal = run_dir / "v3_prototype_result.json"
        if not round_two.is_file():
            exclusions.append(
                {
                    "task_name_hash": c0.sha256_text(task_id),
                    "run_id": run_dir.name,
                    "binding_status": "EXCLUDED",
                    "reason_codes": ["NO_FOLLOW_UP"],
                }
            )
            continue
        if not terminal.is_file():
            exclusions.append(
                {
                    "task_name_hash": c0.sha256_text(task_id),
                    "run_id": run_dir.name,
                    "binding_status": "EXCLUDED",
                    "reason_codes": ["NO_TERMINAL_AGENT_RESULT"],
                }
            )
            continue

        result = read_json(terminal)
        if result.get("status") not in {"DONE", "FAILED"}:
            raise RuntimeError("unexpected terminal result status")
        planner_input = read_json(round_two)
        proposal = read_json(run_dir / "planner/proposal_002.json")
        registry = read_json(run_dir / "candidate_registry.json")
        task = c0.mapping(planner_input.get("task"))
        round_state = c0.mapping(planner_input.get("round"))
        budget = c0.mapping(planner_input.get("budget"))
        incumbent = c0.mapping(planner_input.get("incumbent"))
        baseline = c0.mapping(planner_input.get("baseline"))
        mode = str(round_state.get("mode"))
        round_index = int(round_state.get("round_index") or 0)
        if round_index != 2:
            raise RuntimeError("formal replay only accepts round two")

        current_metrics = c0.report(
            run_dir, c0.mapping(incumbent.get("metrics")).get("ref")
        )
        previous_metrics = c0.report(
            run_dir, c0.mapping(baseline.get("metrics")).get("ref")
        )
        current_evidence = c0.evidence(
            run_dir, c0.mapping(incumbent.get("synth_evidence")).get("ref")
        )
        previous_evidence = c0.evidence(
            run_dir, c0.mapping(baseline.get("synth_evidence")).get("ref")
        )
        validation = c0.mapping(incumbent.get("validation"))
        history = planner_input.get("history")
        history = history if isinstance(history, list) else []
        groups = c0.strategy_groups(
            [
                item.get("change_class")
                for item in history
                if isinstance(item, dict)
            ]
        )
        delta = c0.evidence_delta(
            previous_evidence,
            current_evidence,
            mode=mode,
            before_metrics=previous_metrics,
            after_metrics=current_metrics,
        )
        v2_delta = {
            "new_actionable_evidence": bool(
                delta.get("has_actionable_new_evidence")
            ),
            "bottleneck_changed": bool(delta.get("bottleneck_changed")),
            "latency_improved": bool(
                c0.latency(previous_metrics) is not None
                and c0.latency(current_metrics) is not None
                and float(c0.latency(current_metrics))
                < float(c0.latency(previous_metrics))
            ),
            "ii_improved": bool(
                c0.ii(previous_metrics) is not None
                and c0.ii(current_metrics) is not None
                and float(c0.ii(current_metrics)) < float(c0.ii(previous_metrics))
            ),
            "interval_improved": bool(
                c0.interval(previous_metrics) is not None
                and c0.interval(current_metrics) is not None
                and float(c0.interval(current_metrics))
                < float(c0.interval(previous_metrics))
            ),
        }
        failure = c0.failure_view(c0.mapping(round_state.get("failure_evidence")))
        has_verified_incumbent = str(incumbent.get("status")) in {
            "PROMOTED",
            "FINAL_VERIFIED",
            "CORRECTNESS_VERIFIED",
        }
        acceleration = None
        if (
            c0.latency(previous_metrics) is not None
            and c0.latency(current_metrics) not in (None, 0)
        ):
            acceleration = float(c0.latency(previous_metrics)) / float(
                c0.latency(current_metrics)
            )
        pre_state = c0.unified_pre_state(
            previous_failure=failure if mode != "OPTIMIZE" else {},
            current_failure=failure if mode != "OPTIMIZE" else {},
            previous_evidence_fingerprint=(
                failure.get("fingerprint")
                if mode != "OPTIMIZE"
                else (
                    c0.canonical_sha256(previous_evidence)
                    if previous_evidence
                    else None
                )
            ),
            evidence_fingerprint=(
                failure.get("fingerprint")
                if mode != "OPTIMIZE"
                else (
                    c0.canonical_sha256(current_evidence)
                    if current_evidence
                    else None
                )
            ),
            delta=v2_delta,
            groups=groups,
            validation=validation,
            previous_metrics=previous_metrics,
            current_metrics=current_metrics,
            remaining_tokens=budget.get("tokens_remaining"),
            remaining_credits=budget.get("credits_remaining"),
            remaining_time_seconds=None,
            final_reserve_available=None,
            has_verified_incumbent=has_verified_incumbent,
            consecutive_no_progress=int(
                round_state.get("consecutive_no_improvement") or 0
            ),
            evidence_complete=bool(
                failure if mode != "OPTIMIZE" else current_metrics
            ),
            acceleration_vs_baseline=acceleration,
        )
        decision_id = f"{run_dir.name}:round:2"
        v1 = c0.v1_decide(
            decision_id=decision_id,
            mode=mode,
            round_index=round_index,
            before_metrics=previous_metrics,
            current_metrics=current_metrics or previous_metrics,
            before_evidence=(
                c0.mapping(failure.get("v1"))
                if mode != "OPTIMIZE"
                else previous_evidence
            ),
            current_evidence=(
                c0.mapping(failure.get("v1"))
                if mode != "OPTIMIZE"
                else current_evidence
            ),
            attempted=groups,
            has_verified_incumbent=has_verified_incumbent,
            has_strict_latency_improvement=bool(v2_delta["latency_improved"]),
            remaining_tokens=budget.get("tokens_remaining"),
            remaining_credits=budget.get("credits_remaining"),
        )
        candidate = candidate_for_round(registry, 2)
        outcome = outcome_for_followup(
            c0,
            run_dir,
            mode=mode,
            before_metrics=current_metrics,
            candidate=candidate,
            proposal=proposal,
        )
        row = c0.base_row(
            decision_id=decision_id,
            run_id=run_dir.name,
            task_name=str(task.get("task_id") or task_id),
            mode=mode,
            round_index=round_index,
            pre_state=pre_state,
            v1=v1,
            outcome=outcome,
            source="FORMAL_MATRIX_28X1",
        )
        rows.append(row)
    return rows, exclusions


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    c0 = load_c0()
    new_rows, exclusions = matrix_rows(c0)
    old_rows = c0.read_jsonl(C0_DIR / "replay-dataset-v2.jsonl")
    combined = sorted(
        old_rows + new_rows, key=lambda item: str(item["decision_id"])
    )
    if len({str(item["decision_id"]) for item in combined}) != len(combined):
        raise RuntimeError("duplicate combined continuation decision_id")

    by_id = {str(item["decision_id"]): item for item in combined}
    v1_results = [c0.decision_result(item, "v1") for item in combined]
    v2_results = [c0.decision_result(item, "v2") for item in combined]
    v1 = c0.summarize(v1_results, by_id)
    v2 = c0.summarize(v2_results, by_id)
    c0.write_jsonl(ARTIFACT_DIR / "continuation-formal-new.jsonl", new_rows)
    c0.write_jsonl(
        ARTIFACT_DIR / "continuation-formal-exclusions.jsonl", exclusions
    )
    c0.write_jsonl(
        ARTIFACT_DIR / "continuation-combined-dataset.jsonl", combined
    )
    c0.write_jsonl(
        ARTIFACT_DIR / "continuation-combined-v1-results.jsonl", v1_results
    )
    c0.write_jsonl(
        ARTIFACT_DIR / "continuation-combined-v2-results.jsonl", v2_results
    )

    mode_counts = dict(
        sorted(Counter(str(item["mode"]) for item in combined).items())
    )
    outcome_counts = dict(
        sorted(
            Counter(
                str(c0.mapping(item["outcome"]).get("class"))
                for item in combined
            ).items()
        )
    )
    sufficient = bool(
        all(mode_counts.get(mode, 0) > 0 for mode in c0.MODES)
        and int(
            c0.mapping(v2["essential_structural_retention"]).get(
                "denominator"
            )
            or 0
        )
        > 0
    )
    comparison = {
        "schema_version": "post-matrix.continuation-fixed-replay.v1",
        "status": "INSUFFICIENT_EVIDENCE" if not sufficient else "EVALUATED",
        "admission": "NOT_ADMITTED",
        "authority": {
            "v1": "SHADOW",
            "v2": "OFFLINE_ONLY",
            "enforce": "DISABLED",
            "main_graph_modified": False,
        },
        "fixed_protocol": {
            "policy_module": "llm4hls_agent/v3_continuation_v2.py",
            "policy_module_sha256": sha256(
                HARNESS / "llm4hls_agent/v3_continuation_v2.py"
            ),
            "threshold_tuning": False,
            "same_data_calibration": False,
            "outcomes_read_after_decision": True,
        },
        "dataset": {
            "prior_bindable_decisions": len(old_rows),
            "formal_followup_candidates": len(new_rows)
            + sum(
                "NO_TERMINAL_AGENT_RESULT" in item["reason_codes"]
                for item in exclusions
            ),
            "formal_bindable_decisions": len(new_rows),
            "formal_excluded_no_terminal": sum(
                "NO_TERMINAL_AGENT_RESULT" in item["reason_codes"]
                for item in exclusions
            ),
            "formal_no_followup": sum(
                "NO_FOLLOW_UP" in item["reason_codes"]
                for item in exclusions
            ),
            "combined_bindable_decisions": len(combined),
            "mode_counts": mode_counts,
            "outcome_counts": outcome_counts,
            "future_fields_separated": True,
            "leakage_violations": [],
        },
        "reason_codes": [
            "NO_SYNTH_FIX_FOLLOWUP_SAMPLES"
            if mode_counts.get("SYNTH_FIX", 0) == 0
            else "",
            "NO_ESSENTIAL_STRUCTURAL_OUTCOMES"
            if int(
                c0.mapping(v2["essential_structural_retention"]).get(
                    "denominator"
                )
                or 0
            )
            == 0
            else "",
            "MODE_COVERAGE_UNBALANCED",
        ],
        "v1": v1,
        "v2": v2,
        "real_budget": {
            "llm_calls": 0,
            "tokens": 0,
            "csim": 0,
            "synth": 0,
            "cosim": 0,
            "tool_credits": 0,
        },
        "scope_guards": {
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "secret_value_recorded": False,
        },
    }
    comparison["reason_codes"] = [
        item for item in comparison["reason_codes"] if item
    ]
    c0.write_json(
        ARTIFACT_DIR / "continuation-fixed-protocol-reevaluation.json",
        comparison,
    )
    print(
        json.dumps(
            {
                "status": comparison["status"],
                "dataset": comparison["dataset"],
                "v1": {
                    key: v1[key]
                    for key in (
                        "decision_count",
                        "beneficial_or_essential_retention",
                        "waste_block_rate",
                        "false_block",
                    )
                },
                "v2": {
                    key: v2[key]
                    for key in (
                        "decision_count",
                        "beneficial_or_essential_retention",
                        "waste_block_rate",
                        "false_block",
                    )
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
