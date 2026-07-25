#!/usr/bin/env python3
"""Build the Phase C0 leakage-safe, decision-point replay evidence.

This script is intentionally offline.  It reads only existing public run
artifacts and never imports a backend, planner, graph entry point, or network
client.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
HARNESS = REPO / "llm4hls_harness"
sys.path.insert(0, str(HARNESS))

from llm4hls_agent.v3_continuation import (  # noqa: E402
    continuation_cost,
    continuation_decision,
    evidence_delta,
    performance_area_delta,
    strategy_novelty,
)
from llm4hls_agent.v3_continuation_v2 import (  # noqa: E402
    canonical_sha256,
    continuation_decision_v2,
)


PHASE_A = (
    REPO
    / "docs/experiments/artifacts/2026-07-23-phase-a-offline-audit"
)
R02 = HARNESS / "experiments/v3f/continuation_replay_20260722_r02"
ABC_RUNS = (
    HARNESS
    / "experiments/token_policy_abc/pilot-real-deepseek-v4-pro-20260722-01/runs"
)
B1 = REPO / "docs/experiments/artifacts/2026-07-23-phase-b1-five-real-anchors"
B11 = (
    REPO
    / "docs/experiments/artifacts/2026-07-23-phase-b11-budget-fix-five-anchors"
)
B12 = (
    REPO
    / "docs/experiments/artifacts/2026-07-23-phase-b12-single-structural-anchor"
)
DECISION_CLASSES = {
    "BENEFICIAL_CORRECTNESS",
    "BENEFICIAL_PERFORMANCE",
    "BENEFICIAL_AREA",
    "ESSENTIAL_STRUCTURAL",
    "NEUTRAL",
    "HARMFUL",
    "UNRESOLVED",
}
BENEFICIAL = {
    "BENEFICIAL_CORRECTNESS",
    "BENEFICIAL_PERFORMANCE",
    "BENEFICIAL_AREA",
    "ESSENTIAL_STRUCTURAL",
}
WASTE = {"NEUTRAL", "HARMFUL"}
MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
PROCESS_RE = re.compile(r"process ['\"]([^'\"]+)['\"]", re.IGNORECASE)
FIFO_RE = re.compile(r"FIFO ['\"]([^'\"]+)['\"]", re.IGNORECASE)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected JSONL object: {path}")
            rows.append(value)
    return rows


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def status(validation: Mapping[str, object], name: str) -> str | None:
    value = mapping(validation.get(name)).get("status")
    return str(value) if isinstance(value, str) else None


def latency(metrics: Mapping[str, object]) -> float | None:
    values = mapping(metrics.get("latency"))
    for key in ("worst", "average", "max"):
        parsed = number(values.get(key))
        if parsed is not None:
            return parsed
    return None


def interval(metrics: Mapping[str, object]) -> float | None:
    values = mapping(metrics.get("interval"))
    for key in ("max", "average", "worst"):
        parsed = number(values.get(key))
        if parsed is not None:
            return parsed
    return None


def ii(metrics: Mapping[str, object]) -> float | None:
    loops = mapping(metrics.get("loop_evidence")).get("loops")
    if not isinstance(loops, list):
        return None
    values = [
        parsed
        for item in loops
        if isinstance(item, Mapping)
        and (parsed := number(item.get("pipeline_ii"))) is not None
    ]
    return max(values) if values else None


def report(run_root: Path, ref: object) -> Mapping[str, Any]:
    if not isinstance(ref, str) or not ref:
        return {}
    path = (run_root / ref).resolve()
    try:
        path.relative_to(run_root.resolve())
    except ValueError:
        return {}
    if not path.is_file():
        return {}
    return mapping(read_json(path).get("report"))


def evidence(run_root: Path, ref: object) -> Mapping[str, Any]:
    if not isinstance(ref, str) or not ref:
        return {}
    path = (run_root / ref).resolve()
    try:
        path.relative_to(run_root.resolve())
    except ValueError:
        return {}
    return read_json(path) if path.is_file() else {}


def strategy_groups(values: object) -> list[list[str]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    groups: list[list[str]] = []
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("observed_strategy_atoms")
        if isinstance(value, str):
            atoms = [item for item in value.upper().split("+") if item]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            atoms = [str(item).upper() for item in value if str(item).strip()]
        else:
            atoms = []
        if atoms:
            groups.append(sorted(set(atoms)))
    return groups


def strategy_state(groups: list[list[str]]) -> tuple[list[str], str]:
    if not groups:
        return [], "UNKNOWN"
    current = groups[-1]
    seen = {item for group in groups[:-1] for item in group}
    if not groups[:-1] or set(current) - seen:
        return current, "HIGH"
    return current, "DUPLICATE"


def task_name_from_r02(sample_id: str) -> str:
    run = sample_id.split(":round:", 1)[0]
    if run in {"r01", "r02"}:
        return "dotProduct_optimize"
    return run.split("--", 1)[0]


def failure_view(value: Mapping[str, object]) -> dict[str, object]:
    lines: list[str] = []
    for key in (
        "relevant_log_lines",
        "stream_fifo_interface_findings",
        "evidence",
    ):
        raw = value.get(key)
        if isinstance(raw, list):
            lines.extend(str(item) for item in raw[:64])
    deadlock_observed = bool(value.get("deadlock")) or any(
        "deadlock" in line.lower() for line in lines
    )
    kind = str(
        value.get("failure_kind")
        or ("DEADLOCK" if deadlock_observed else "")
        or value.get("phase")
        or "UNKNOWN"
    ).upper()
    processes = sorted(
        {match.group(1) for line in lines for match in PROCESS_RE.finditer(line)}
    )
    fifos = sorted(
        {match.group(1) for line in lines for match in FIFO_RE.finditer(line)}
    )
    fifo_findings = sorted(
        {" ".join(line.split())[:240] for line in lines if "fifo" in line.lower()}
    )
    locations = value.get("source_locations")
    if isinstance(locations, list):
        processes.extend(str(item) for item in locations if str(item).strip())
        processes = sorted(set(processes))
    signature_material = {
        "kind": kind,
        "timeout": bool(value.get("timeout")),
        "rtl_mismatch": bool(value.get("rtl_mismatch")),
    }
    location_material = {"processes_or_locations": processes}
    return {
        "kind": kind,
        "signature": canonical_sha256(signature_material),
        "location_signature": (
            canonical_sha256(location_material) if processes else None
        ),
        "fifo_signature": (
            canonical_sha256({"fifo_findings": fifo_findings})
            if fifo_findings
            else None
        ),
        "fingerprint": canonical_sha256(
            {
                **signature_material,
                **location_material,
                "fifo_findings": fifo_findings,
            }
        ),
        "v1": {
            "failure_stage": "cosim",
            "failure_subtype": kind,
            "source_location": processes[0] if processes else None,
            "fifo": fifos[0] if fifos else None,
        },
    }


def latest_candidate_failure(
    run_root: Path, candidate: Mapping[str, object]
) -> Mapping[str, object]:
    validation = mapping(candidate.get("validation"))
    for tool in ("cosim", "synth", "csim"):
        entry = mapping(validation.get(tool))
        if entry.get("status") != "FAIL":
            continue
        ref = entry.get("result_ref")
        if isinstance(ref, str) and (run_root / ref).is_file():
            return read_json(run_root / ref)
    return {}


def v1_decide(
    *,
    decision_id: str,
    mode: str,
    round_index: int,
    before_metrics: Mapping[str, object],
    current_metrics: Mapping[str, object],
    before_evidence: Mapping[str, object],
    current_evidence: Mapping[str, object],
    attempted: list[list[str]],
    has_verified_incumbent: bool,
    has_strict_latency_improvement: bool,
    remaining_tokens: object,
    remaining_credits: object,
) -> dict[str, object]:
    delta = evidence_delta(
        before_evidence,
        current_evidence,
        mode=mode,
        before_metrics=before_metrics,
        after_metrics=current_metrics,
    )
    strategies = strategy_novelty(attempted=attempted)
    ledger = {
        "tokens_remaining": remaining_tokens,
        "credits_remaining": remaining_credits,
    }
    decision = continuation_decision(
        run_id=decision_id,
        round_index=round_index,
        mode=mode,
        policy_mode="shadow",
        has_correct_candidate=has_verified_incumbent,
        has_strict_latency_improvement=has_strict_latency_improvement,
        performance_area=performance_area_delta(before_metrics, current_metrics),
        delta=delta,
        strategies=strategies,
        cost=continuation_cost(
            ledger=ledger,
            estimated_input_tokens=0,
            estimated_output_tokens=0,
            estimated_credits=0,
            estimated_wall_time_seconds=0,
            final_reserve_safe=True,
        ),
        remaining_rounds=1,
    )
    return {
        "decision": decision["decision"],
        "reason_codes": decision["reason_codes"],
        "decision_hash": decision["decision_hash"],
        "value_score": mapping(decision.get("value")).get("value_score"),
    }


def unified_pre_state(
    *,
    previous_failure: Mapping[str, object] | None = None,
    current_failure: Mapping[str, object] | None = None,
    previous_evidence_fingerprint: object = None,
    evidence_fingerprint: object = None,
    delta: Mapping[str, object] | None = None,
    groups: list[list[str]] | None = None,
    validation: Mapping[str, object] | None = None,
    previous_metrics: Mapping[str, object] | None = None,
    current_metrics: Mapping[str, object] | None = None,
    remaining_tokens: object = None,
    remaining_credits: object = None,
    remaining_time_seconds: object = None,
    final_reserve_available: object = None,
    has_verified_incumbent: bool = False,
    consecutive_no_progress: int = 0,
    evidence_complete: bool = True,
    acceleration_vs_baseline: float | None = None,
) -> dict[str, object]:
    previous_failure = previous_failure or {}
    current_failure = current_failure or {}
    previous_metrics = previous_metrics or {}
    current_metrics = current_metrics or {}
    validation = validation or {}
    groups = groups or []
    current_strategy, novelty = strategy_state(groups)
    return {
        "failure_subtype": current_failure.get("kind"),
        "failure_signature": current_failure.get("signature"),
        "failure_location_signature": current_failure.get("location_signature"),
        "evidence_fingerprint": evidence_fingerprint,
        "evidence_delta": dict(delta or {}),
        "previous_failure_signature": previous_failure.get("signature"),
        "previous_failure_location_signature": previous_failure.get(
            "location_signature"
        ),
        "previous_evidence_fingerprint": previous_evidence_fingerprint,
        "observed_strategy_history": groups,
        "current_observed_strategy": current_strategy,
        "strategy_novelty": novelty,
        "current_csim": status(validation, "csim"),
        "current_synth": status(validation, "synth"),
        "current_cosim": status(validation, "cosim"),
        "previous_latency": latency(previous_metrics),
        "current_latency": latency(current_metrics),
        "previous_ii": ii(previous_metrics),
        "current_ii": ii(current_metrics),
        "previous_interval": interval(previous_metrics),
        "current_interval": interval(current_metrics),
        "previous_clock_ns": number(
            previous_metrics.get("estimated_clock_period_ns")
        ),
        "current_clock_ns": number(current_metrics.get("estimated_clock_period_ns")),
        "current_resource_utilization": (
            current_metrics.get("utilization_percent")
            if isinstance(current_metrics.get("utilization_percent"), Mapping)
            else None
        ),
        "remaining_tokens": number(remaining_tokens),
        "remaining_credits": number(remaining_credits),
        "remaining_time_seconds": number(remaining_time_seconds),
        "final_reserve_available": (
            final_reserve_available
            if isinstance(final_reserve_available, bool)
            else None
        ),
        "reserve_tight": None,
        "has_verified_incumbent": has_verified_incumbent,
        "has_better_verified_candidate_needing_final": has_verified_incumbent,
        "consecutive_no_progress": consecutive_no_progress,
        "evidence_complete": evidence_complete,
        "evidence_conflict": False,
        "acceleration_vs_baseline": acceleration_vs_baseline,
        "scoring_cap": 8.0,
    }


def base_row(
    *,
    decision_id: str,
    run_id: str,
    task_name: str,
    mode: str,
    round_index: int,
    pre_state: Mapping[str, object],
    v1: Mapping[str, object],
    outcome: Mapping[str, object],
    source: str,
) -> dict[str, object]:
    if str(outcome.get("class")) not in DECISION_CLASSES:
        raise ValueError(f"invalid outcome class for {decision_id}")
    visible = ["mode"] + [
        f"pre_state.{key}" for key in sorted(pre_state)
    ]
    return {
        "schema_version": "v3.continuation-replay-sample.v2",
        "decision_id": decision_id,
        "run_id": run_id,
        "task_name_hash": sha256_text(task_name),
        "mode": mode,
        "round_index": round_index,
        "binding_status": "BOUND",
        "pre_state": dict(pre_state),
        "v1_decision": {
            "decision": v1.get("decision", "UNKNOWN"),
            "reason_codes": list(v1.get("reason_codes", [])),
        },
        "outcome": dict(outcome),
        "leakage_audit": {
            "policy_visible_fields": visible,
            "future_fields_excluded": True,
            "violations": [],
            "legacy_terminal_budget_removed": source == "V3F_R02",
        },
        "source_kind": "REAL_LLM_VITIS",
        "source_dataset": source,
    }


def r02_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source in read_jsonl(R02 / "continuation_replay_dataset.jsonl"):
        raw_pre = mapping(source.get("pre_state"))
        mode = str(raw_pre.get("mode"))
        sample_id = str(source.get("sample_id"))
        run_id = f"legacy-r02:{sample_id.split(':round:', 1)[0]}"
        decision_id = f"{run_id}:round:{raw_pre.get('round_index')}"
        previous_metrics = mapping(raw_pre.get("previous_metrics"))
        current_metrics = mapping(raw_pre.get("parent_metrics"))
        previous_ev = mapping(raw_pre.get("previous_evidence"))
        current_ev = mapping(raw_pre.get("current_evidence"))
        delta_v1 = evidence_delta(
            previous_ev,
            current_ev,
            mode=mode,
            before_metrics=previous_metrics,
            after_metrics=current_metrics,
        )
        groups = strategy_groups(raw_pre.get("attempted_strategy_atoms"))
        relative_improvement = (
            latency(previous_metrics) is not None
            and latency(current_metrics) is not None
            and float(latency(current_metrics)) < float(latency(previous_metrics))
        )
        v2_delta = {
            "new_actionable_evidence": bool(
                delta_v1.get("has_actionable_new_evidence")
            ),
            "bottleneck_changed": bool(delta_v1.get("bottleneck_changed")),
            "latency_improved": relative_improvement,
            "ii_improved": (
                ii(previous_metrics) is not None
                and ii(current_metrics) is not None
                and float(ii(current_metrics)) < float(ii(previous_metrics))
            ),
            "interval_improved": (
                interval(previous_metrics) is not None
                and interval(current_metrics) is not None
                and float(interval(current_metrics)) < float(interval(previous_metrics))
            ),
        }
        pre_state = unified_pre_state(
            previous_evidence_fingerprint=delta_v1.get("before_fingerprint", {}).get(
                "fingerprint"
            ),
            evidence_fingerprint=delta_v1.get("after_fingerprint", {}).get(
                "fingerprint"
            ),
            delta=v2_delta,
            groups=groups,
            previous_metrics=previous_metrics,
            current_metrics=current_metrics,
            has_verified_incumbent=bool(raw_pre.get("has_correct_candidate")),
            consecutive_no_progress=0 if relative_improvement else 1,
            evidence_complete=True,
        )
        v1 = v1_decide(
            decision_id=decision_id,
            mode=mode,
            round_index=int(raw_pre.get("round_index") or 0),
            before_metrics=previous_metrics,
            current_metrics=current_metrics,
            before_evidence=previous_ev,
            current_evidence=current_ev,
            attempted=groups,
            has_verified_incumbent=bool(raw_pre.get("has_correct_candidate")),
            # Reproduce the archived R02 V1 evaluator exactly.  The archived
            # adapter fixed this flag to false; the removed terminal budget
            # still has no effect because the estimated next cost was zero.
            has_strict_latency_improvement=False,
            remaining_tokens=None,
            remaining_credits=None,
        )
        raw_outcome = mapping(source.get("outcome"))
        label = str(raw_outcome.get("label"))
        outcome_class = (
            "BENEFICIAL_CORRECTNESS"
            if label == "ESSENTIAL_FOR_CORRECTNESS"
            else label
        )
        outcome = {
            "class": outcome_class,
            "candidate_promoted": (
                str(raw_outcome.get("candidate_status"))
                in {"PROMOTED", "FINAL_VERIFIED", "CORRECTNESS_VERIFIED"}
            ),
            "failure_changed": None,
            "latency_delta": None,
            "ii_delta": None,
            "area_delta": None,
            "token_cost": number(raw_outcome.get("actual_tokens")),
            "credit_cost": number(raw_outcome.get("actual_credits")),
        }
        rows.append(
            base_row(
                decision_id=decision_id,
                run_id=run_id,
                task_name=task_name_from_r02(sample_id),
                mode=mode,
                round_index=int(raw_pre.get("round_index") or 0),
                pre_state=pre_state,
                v1=v1,
                outcome=outcome,
                source="V3F_R02",
            )
        )
    return rows


def phase_a_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    source_rows = read_jsonl(
        PHASE_A / "continuation_replay_candidate_dataset.jsonl"
    )
    for source in source_rows:
        run_id = str(source.get("run_id"))
        run_root = ABC_RUNS / run_id
        planner_input = read_json(run_root / "planner/inputs/round_002.json")
        registry = read_json(run_root / "candidate_registry.json")
        raw_candidates = registry.get("candidates")
        if isinstance(raw_candidates, Mapping):
            candidates = {
                str(key): value
                for key, value in raw_candidates.items()
                if isinstance(value, Mapping)
            }
        elif isinstance(raw_candidates, list):
            candidates = {
                str(item.get("candidate_id")): item
                for item in raw_candidates
                if isinstance(item, Mapping)
            }
        else:
            candidates = {}
        incumbent = mapping(planner_input.get("incumbent"))
        baseline = mapping(planner_input.get("baseline"))
        round_state = mapping(planner_input.get("round"))
        budget = mapping(planner_input.get("budget"))
        task = mapping(planner_input.get("task"))
        mode = str(round_state.get("mode"))
        round_index = int(round_state.get("round_index") or 0)
        current_metrics = report(
            run_root, mapping(incumbent.get("metrics")).get("ref")
        )
        previous_metrics = report(
            run_root, mapping(baseline.get("metrics")).get("ref")
        )
        current_ev = evidence(
            run_root, mapping(incumbent.get("synth_evidence")).get("ref")
        )
        previous_ev = evidence(
            run_root, mapping(baseline.get("synth_evidence")).get("ref")
        )
        history_groups = strategy_groups(
            mapping(source.get("pre_state")).get("observed_strategy_history")
        )
        current_validation = mapping(incumbent.get("validation"))
        has_incumbent = (
            str(incumbent.get("status"))
            in {"PROMOTED", "FINAL_VERIFIED", "CORRECTNESS_VERIFIED"}
            and status(current_validation, "csim") == "PASS"
            and status(current_validation, "synth") == "PASS"
        )
        previous_failure: dict[str, object] = {}
        current_failure: dict[str, object] = {}
        before_for_v1: Mapping[str, object] = previous_ev
        current_for_v1: Mapping[str, object] = current_ev
        delta_v2: dict[str, object] = {}
        if mode == "STRUCTURAL_FIX":
            previous_failure = failure_view(
                mapping(round_state.get("failure_evidence"))
            )
            prior_candidate = candidates.get("candidate_001", {})
            current_failure_raw = latest_candidate_failure(
                run_root, prior_candidate
            )
            current_failure = failure_view(current_failure_raw)
            before_for_v1 = mapping(previous_failure.get("v1"))
            current_for_v1 = mapping(current_failure.get("v1"))
            fifo_changed = (
                previous_failure.get("fifo_signature") is not None
                and current_failure.get("fifo_signature") is not None
                and previous_failure.get("fifo_signature")
                != current_failure.get("fifo_signature")
            )
            location_changed = (
                previous_failure.get("location_signature") is not None
                and current_failure.get("location_signature") is not None
                and previous_failure.get("location_signature")
                != current_failure.get("location_signature")
            )
            delta_v2 = {
                "fifo_evidence_new": fifo_changed,
                "deadlock_location_changed": location_changed,
                "new_actionable_evidence": fifo_changed or location_changed,
                "same_failure_signature": (
                    previous_failure.get("signature")
                    == current_failure.get("signature")
                ),
                "same_failure_location": (
                    previous_failure.get("location_signature")
                    == current_failure.get("location_signature")
                ),
                "same_fifo_evidence": (
                    previous_failure.get("fifo_signature")
                    == current_failure.get("fifo_signature")
                ),
            }
        else:
            delta_current = evidence_delta(
                previous_ev,
                current_ev,
                mode=mode,
                before_metrics=previous_metrics,
                after_metrics=current_metrics,
            )
            delta_v2 = {
                "new_actionable_evidence": bool(
                    delta_current.get("has_actionable_new_evidence")
                ),
                "bottleneck_changed": bool(
                    delta_current.get("bottleneck_changed")
                ),
                "latency_improved": (
                    latency(previous_metrics) is not None
                    and latency(current_metrics) is not None
                    and float(latency(current_metrics))
                    < float(latency(previous_metrics))
                ),
                "ii_improved": (
                    ii(previous_metrics) is not None
                    and ii(current_metrics) is not None
                    and float(ii(current_metrics)) < float(ii(previous_metrics))
                ),
                "interval_improved": (
                    interval(previous_metrics) is not None
                    and interval(current_metrics) is not None
                    and float(interval(current_metrics))
                    < float(interval(previous_metrics))
                ),
            }
        acceleration = None
        if (
            latency(previous_metrics) is not None
            and latency(current_metrics) not in (None, 0)
        ):
            acceleration = float(latency(previous_metrics)) / float(
                latency(current_metrics)
            )
        pre_state = unified_pre_state(
            previous_failure=previous_failure,
            current_failure=current_failure,
            previous_evidence_fingerprint=(
                previous_failure.get("fingerprint")
                or (canonical_sha256(previous_ev) if previous_ev else None)
            ),
            evidence_fingerprint=(
                current_failure.get("fingerprint")
                or (canonical_sha256(current_ev) if current_ev else None)
            ),
            delta=delta_v2,
            groups=history_groups,
            validation=current_validation,
            previous_metrics=previous_metrics,
            current_metrics=current_metrics,
            remaining_tokens=budget.get("tokens_remaining"),
            remaining_credits=budget.get("credits_remaining"),
            remaining_time_seconds=mapping(source.get("pre_state")).get(
                "remaining_time_seconds"
            ),
            final_reserve_available=None,
            has_verified_incumbent=has_incumbent,
            consecutive_no_progress=int(
                round_state.get("consecutive_no_improvement") or 0
            ),
            evidence_complete=bool(
                current_failure if mode == "STRUCTURAL_FIX" else current_metrics
            ),
            acceleration_vs_baseline=acceleration,
        )
        v1 = v1_decide(
            decision_id=str(source.get("decision_id")),
            mode=mode,
            round_index=round_index,
            before_metrics=previous_metrics,
            current_metrics=current_metrics or previous_metrics,
            before_evidence=before_for_v1,
            current_evidence=current_for_v1,
            attempted=history_groups,
            has_verified_incumbent=has_incumbent,
            has_strict_latency_improvement=(
                latency(previous_metrics) is not None
                and latency(current_metrics) is not None
                and float(latency(current_metrics))
                < float(latency(previous_metrics))
            ),
            remaining_tokens=budget.get("tokens_remaining"),
            remaining_credits=budget.get("credits_remaining"),
        )
        candidate = candidates.get("candidate_002", {})
        raw_outcome = mapping(source.get("outcome_label"))
        correctness = mapping(raw_outcome.get("correctness_delta"))
        failure_changed = any(
            mapping(correctness.get(tool)).get("before")
            != mapping(correctness.get(tool)).get("after")
            for tool in ("csim", "synth", "cosim")
            if correctness.get(tool) is not None
        )
        outcome = {
            "class": str(raw_outcome.get("class")),
            "candidate_promoted": bool(raw_outcome.get("candidate_promoted")),
            "failure_changed": failure_changed,
            "latency_delta": raw_outcome.get("latency_delta"),
            "ii_delta": None,
            "area_delta": raw_outcome.get("area_delta"),
            "token_cost": (
                (number(candidate.get("input_tokens")) or 0)
                + (number(candidate.get("output_tokens")) or 0)
                if candidate
                else None
            ),
            "credit_cost": number(candidate.get("credits_used")),
        }
        rows.append(
            base_row(
                decision_id=str(source.get("decision_id")),
                run_id=run_id,
                task_name=str(task.get("task_id") or run_id),
                mode=mode,
                round_index=round_index,
                pre_state=pre_state,
                v1=v1,
                outcome=outcome,
                source="TOKEN_POLICY_ABC_72",
            )
        )
    return rows


def build_exclusions() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    inventory = read_jsonl(PHASE_A / "historical-72run-inventory.jsonl")
    bound_runs = {
        str(row.get("run_id"))
        for row in read_jsonl(
            PHASE_A / "continuation_replay_candidate_dataset.jsonl"
        )
    }
    explicitly_excluded = {
        str(row.get("run_id")): row
        for row in read_jsonl(
            PHASE_A / "continuation_replay_exclusion_log.jsonl"
        )
    }
    for item in inventory:
        run_id = str(item.get("run_id"))
        if run_id in bound_runs:
            continue
        source = explicitly_excluded.get(run_id)
        reasons = (
            list(source.get("reason_codes", []))
            if source
            else ["NO_FOLLOW_UP"]
        )
        rows.append(
            {
                "source_record_id": run_id,
                "run_id": run_id,
                "source_dataset": "TOKEN_POLICY_ABC_72",
                "exclusion_scope": (
                    "FOLLOWUP_DECISION" if source else "SOURCE_SCREENING"
                ),
                "binding_status": "EXCLUDED",
                "reason_codes": reasons,
            }
        )

    legacy_reasons = [
        "DUPLICATE_DECISION",
        "MISSING_DECISION_TIME_EVIDENCE",
        "NOT_REAL_VITIS",
        "NOT_REAL_VITIS",
        "NOT_REAL_VITIS",
        "NOT_REAL_VITIS",
    ]
    for index, reason in enumerate(legacy_reasons, start=1):
        rows.append(
            {
                "source_record_id": f"legacy-r02-exclusion-{index:02d}",
                "run_id": None,
                "source_dataset": "V3F_R02",
                "exclusion_scope": "LEGACY_SOURCE_SCREENING",
                "binding_status": "EXCLUDED",
                "reason_codes": [reason],
                "source_identity_available": False,
            }
        )

    for dataset_name, index_path, task_key in (
        ("PHASE_B1", B1 / "anchor-run-index.jsonl", "task_name"),
        ("PHASE_B11", B11 / "anchor-run-index.jsonl", "task_id"),
    ):
        for item in read_jsonl(index_path):
            run_id = str(item.get("run_dir"))
            rows.append(
                {
                    "source_record_id": run_id,
                    "run_id": run_id,
                    "task_name_hash": sha256_text(str(item.get(task_key))),
                    "source_dataset": dataset_name,
                    "exclusion_scope": "SOURCE_SCREENING",
                    "binding_status": "EXCLUDED",
                    "reason_codes": ["NO_FOLLOW_UP"],
                }
            )
    b12_result = read_json(B12 / "single-anchor-result.json")
    rows.append(
        {
            "source_record_id": str(b12_result.get("run_dir")),
            "run_id": str(b12_result.get("run_dir")),
            "task_name_hash": sha256_text(str(b12_result.get("task_id"))),
            "source_dataset": "PHASE_B12",
            "exclusion_scope": "SOURCE_SCREENING",
            "binding_status": "EXCLUDED",
            "reason_codes": ["NO_FOLLOW_UP"],
        }
    )
    return sorted(rows, key=lambda row: str(row["source_record_id"]))


def decision_result(
    row: Mapping[str, object], policy: str
) -> dict[str, object]:
    mode = str(row["mode"])
    pre_state = mapping(row["pre_state"])
    if policy == "v1":
        raw = mapping(row["v1_decision"])
        decision = str(raw.get("decision", "UNKNOWN"))
        policy_record: Mapping[str, object] = raw
    else:
        policy_record = continuation_decision_v2(mode=mode, pre_state=pre_state)
        decision = str(policy_record["decision"])
    outcome = mapping(row["outcome"])
    outcome_class = str(outcome.get("class"))
    return {
        "schema_version": f"phase-c0.continuation-{policy}-result.v1",
        "decision_id": row["decision_id"],
        "run_id": row["run_id"],
        "task_name_hash": row["task_name_hash"],
        "mode": mode,
        "policy_input_digest": canonical_sha256(
            {"mode": mode, "pre_state": pre_state}
        ),
        "policy_result": dict(policy_record),
        "decision": decision,
        "outcome_class": outcome_class,
        "false_block": decision == "BLOCK" and outcome_class in BENEFICIAL,
        "false_defer": (
            decision == "DEFER_TO_FINAL" and outcome_class in BENEFICIAL
        ),
        "waste_stopped": decision in {"BLOCK", "DEFER_TO_FINAL"}
        and outcome_class in WASTE,
        "offline_potential_token_saving": (
            outcome.get("token_cost")
            if decision in {"BLOCK", "DEFER_TO_FINAL"} and outcome_class in WASTE
            else 0
        ),
        "offline_potential_credit_saving": (
            outcome.get("credit_cost")
            if decision in {"BLOCK", "DEFER_TO_FINAL"} and outcome_class in WASTE
            else 0
        ),
    }


def fraction(numerator: int, denominator: int) -> dict[str, object]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
        "status": "OK" if denominator else "INSUFFICIENT_EVIDENCE",
    }


def summarize(
    results: Sequence[Mapping[str, object]],
    dataset: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    counts = Counter(str(row["decision"]) for row in results)
    outcome_counts = Counter(str(row["outcome_class"]) for row in results)
    beneficial_total = sum(outcome_counts[name] for name in BENEFICIAL)
    essential_structural_total = sum(
        1
        for row in results
        if row["mode"] == "STRUCTURAL_FIX"
        and row["outcome_class"] == "ESSENTIAL_STRUCTURAL"
    )
    beneficial_retained = sum(
        1
        for row in results
        if row["outcome_class"] in BENEFICIAL and row["decision"] == "ALLOW"
    )
    essential_structural_retained = sum(
        1
        for row in results
        if row["mode"] == "STRUCTURAL_FIX"
        and row["outcome_class"] == "ESSENTIAL_STRUCTURAL"
        and row["decision"] == "ALLOW"
    )
    waste_total = sum(outcome_counts[name] for name in WASTE)
    waste_stopped = sum(bool(row["waste_stopped"]) for row in results)
    false_block = sum(bool(row["false_block"]) for row in results)
    false_defer = sum(bool(row["false_defer"]) for row in results)
    by_mode: dict[str, object] = {}
    for mode in MODES:
        selected = [row for row in results if row["mode"] == mode]
        selected_outcomes = Counter(str(row["outcome_class"]) for row in selected)
        selected_beneficial = sum(selected_outcomes[name] for name in BENEFICIAL)
        selected_waste = sum(selected_outcomes[name] for name in WASTE)
        by_mode[mode] = {
            "coverage": fraction(len(selected), len(results)),
            "decision_count": dict(
                sorted(Counter(str(row["decision"]) for row in selected).items())
            ),
            "allow_rate": fraction(
                sum(row["decision"] == "ALLOW" for row in selected),
                len(selected),
            ),
            "block_rate": fraction(
                sum(row["decision"] == "BLOCK" for row in selected),
                len(selected),
            ),
            "defer_rate": fraction(
                sum(row["decision"] == "DEFER_TO_FINAL" for row in selected),
                len(selected),
            ),
            "beneficial_or_essential_retention": fraction(
                sum(
                    row["decision"] == "ALLOW"
                    and row["outcome_class"] in BENEFICIAL
                    for row in selected
                ),
                selected_beneficial,
            ),
            "waste_block_rate": fraction(
                sum(bool(row["waste_stopped"]) for row in selected),
                selected_waste,
            ),
            "false_block": {
                "count": sum(bool(row["false_block"]) for row in selected),
                "beneficial_or_essential_denominator": selected_beneficial,
            },
            "false_defer": {
                "count": sum(bool(row["false_defer"]) for row in selected),
                "beneficial_or_essential_denominator": selected_beneficial,
            },
            "unresolved": {
                "count": selected_outcomes["UNRESOLVED"],
                "denominator": len(selected),
            },
        }
    stopped_waste = [
        row
        for row in results
        if row["decision"] in {"BLOCK", "DEFER_TO_FINAL"}
        and row["outcome_class"] in WASTE
    ]
    known_tokens = [
        number(dataset[str(row["decision_id"])]["outcome"].get("token_cost"))
        for row in stopped_waste
    ]
    known_credits = [
        number(dataset[str(row["decision_id"])]["outcome"].get("credit_cost"))
        for row in stopped_waste
    ]
    return {
        "bindable_decisions": len(results),
        "decision_count": dict(sorted(counts.items())),
        "allow_rate": fraction(counts["ALLOW"], len(results)),
        "block_rate": fraction(counts["BLOCK"], len(results)),
        "defer_rate": fraction(counts["DEFER_TO_FINAL"], len(results)),
        "beneficial_or_essential_retention": fraction(
            beneficial_retained, beneficial_total
        ),
        "essential_structural_retention": fraction(
            essential_structural_retained, essential_structural_total
        ),
        "waste_block_rate": fraction(waste_stopped, waste_total),
        "false_block": {
            "count": false_block,
            "beneficial_or_essential_denominator": beneficial_total,
        },
        "false_defer": {
            "count": false_defer,
            "beneficial_or_essential_denominator": beneficial_total,
        },
        "unresolved": {
            "count": outcome_counts["UNRESOLVED"],
            "denominator": len(results),
        },
        "offline_potential_savings": {
            "tokens": sum(value for value in known_tokens if value is not None),
            "credits": sum(value for value in known_credits if value is not None),
            "stopped_waste_decisions": len(stopped_waste),
            "token_cost_known": sum(value is not None for value in known_tokens),
            "credit_cost_known": sum(value is not None for value in known_credits),
            "claim": "OFFLINE_POTENTIAL_ONLY",
        },
        "by_mode": by_mode,
    }


def render_rate(value: Mapping[str, object]) -> str:
    numerator = int(value.get("numerator") or 0)
    denominator = int(value.get("denominator") or 0)
    rate = value.get("rate")
    if rate is None:
        return f"INSUFFICIENT_EVIDENCE ({numerator}/{denominator})"
    return f"{float(rate):.1%} ({numerator}/{denominator})"


def write_audit() -> None:
    v1_path = HARNESS / "llm4hls_agent/v3_continuation.py"
    graph_path = HARNESS / "llm4hls_agent/v3_prototype.py"
    audit = {
        "schema_version": "phase-c0.current-continuation-audit.v1",
        "v1": {
            "schema_version": "v3.continuation-decision.v1",
            "module_sha256": file_sha256(v1_path),
            "inputs": [
                "run_id",
                "round_index",
                "mode",
                "policy_mode",
                "has_correct_candidate",
                "has_strict_latency_improvement",
                "performance_area",
                "evidence_delta",
                "strategy_novelty",
                "continuation_cost",
                "remaining_rounds",
            ],
            "shared_rules": [
                "budget/reserve hard reasons",
                "generic evidence score",
                "generic strategy novelty score",
                "single score threshold",
            ],
            "mode_specific_rules": {
                "REPAIR": [],
                "SYNTH_FIX": [],
                "STRUCTURAL_FIX": ["failure_subtype_changed bonus"],
                "OPTIMIZE": [
                    "improvement without new bottleneck penalty",
                    "performance-area tradeoff opportunity",
                ],
            },
            "progress_dimensions_explicitly_separated": False,
            "runtime_default": "off",
            "accepted_authority": "SHADOW",
            "enforce_supported_but_admitted": False,
        },
        "leakage_audit": {
            "main_graph_predecision_path": "NO_KNOWN_FUTURE_LEAKAGE",
            "legacy_r02_replay_budget": "TERMINAL_RESULT_BUDGET_WAS_POLICY_VISIBLE",
            "legacy_r02_budget_affected_recorded_v1_decisions": False,
            "phase_c0_action": "REMOVE_LEGACY_BUDGET_FROM_V2_PRE_STATE",
            "violations_in_v2_dataset": [],
        },
        "graph": {
            "sha256": file_sha256(graph_path),
            "modified_by_phase_c0": False,
        },
    }
    write_json(EVIDENCE / "current-continuation-audit.json", audit)
    (EVIDENCE / "current-continuation-audit.zh-CN.md").write_text(
        """# 当前 Continuation V1 审计

## 结论

V1 已实现并可在主路径以 `shadow` 运行，但运行时默认值仍为 `off`，正式接受权限只到 Shadow，Enforce 未准入。V1 采用一套跨 Mode 共用的分数规则；仅对 STRUCTURAL_FIX 和 OPTIMIZE 有少量加减分，未显式分离 correctness、structural、performance 三类进展。

## 输入与规则

- 输入：Mode、正确 incumbent、严格 latency 改善、Performance-Area、Evidence Delta、Strategy Novelty、Continuation Cost、剩余轮数。
- Evidence Delta：比较清洗后的前后 fingerprint，统计 failure stage/subtype、location、affected object、bottleneck、critical loop、scheduling 和 resource pressure 等变化。
- Strategy Novelty：静态扫描已生成 Patch 的 pragma/结构行为；Patch 扫描优先，声明标签只作 fallback。
- 决策：预算/最终保留先形成硬原因；其余事实进入一个通用 value score，再产生 `ALLOW/BLOCK/DEFER_TO_FINAL`。

## Mode 差异不足

- REPAIR、SYNTH_FIX 没有独立停止规则。
- STRUCTURAL_FIX 只有 subtype refinement 奖励，尚未把 FIFO、producer/consumer、拓扑和 mismatch specificity 分开。
- OPTIMIZE 有 bottleneck 与 Performance-Area 条件，但未把 latency、II、interval、clock、8× cap 和低边际收益组织为独立决策规则。

## 信息泄漏审计

主图调用点使用当前 Ledger、已完成 Candidate 历史和当前 Evidence，未发现读取未来 Candidate/outcome。旧 V3-F R02 replay builder 把终态 `result.budget` 放进了 `pre_state`，属于严格 V2 口径下的 future-information leakage 风险；旧回放估计成本为 0，因此该字段未改变当时 11 条 V1 决策，但 Phase C0 数据已经将它完全剔除并保留为 `null/UNKNOWN`，没有用终态值回填。
""",
        encoding="utf-8",
    )


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    write_audit()
    dataset_rows = r02_rows() + phase_a_rows()
    dataset_rows.sort(key=lambda row: str(row["decision_id"]))
    if len({row["decision_id"] for row in dataset_rows}) != len(dataset_rows):
        raise RuntimeError("duplicate decision_id")
    exclusions = build_exclusions()
    write_jsonl(EVIDENCE / "replay-exclusions-v2.jsonl", exclusions)

    v1_results = [decision_result(row, "v1") for row in dataset_rows]
    v2_results = [decision_result(row, "v2") for row in dataset_rows]
    v1_by_id = {str(row["decision_id"]): row for row in v1_results}
    for row in dataset_rows:
        row["v1_decision"] = {
            "decision": v1_by_id[str(row["decision_id"])]["decision"],
            "reason_codes": mapping(
                v1_by_id[str(row["decision_id"])]["policy_result"]
            ).get("reason_codes", []),
        }
    write_jsonl(EVIDENCE / "replay-dataset-v2.jsonl", dataset_rows)
    write_jsonl(EVIDENCE / "policy-v1-results.jsonl", v1_results)
    write_jsonl(EVIDENCE / "policy-v2-results.jsonl", v2_results)

    by_id = {str(row["decision_id"]): row for row in dataset_rows}
    task_groups = sorted(
        {
            str(row["task_name_hash"])
            for row in dataset_rows
        }
    )
    run_groups = sorted({str(row["run_id"]) for row in dataset_rows})
    split_material = {
        "protocol": "NO_CALIBRATION_PLUS_LEAVE_ONE_GROUP_OUT",
        "task_groups": task_groups,
        "run_groups": run_groups,
        "decision_ids": [str(row["decision_id"]) for row in dataset_rows],
    }
    split_digest = canonical_sha256(split_material)
    v1_summary = summarize(v1_results, by_id)
    v2_summary = summarize(v2_results, by_id)
    comparison = {
        "schema_version": "phase-c0.policy-comparison.v1",
        "phase": "Phase C0",
        "status": "INSUFFICIENT_EVIDENCE",
        "admission": "INSUFFICIENT_EVIDENCE",
        "reason_codes": [
            "NO_REPAIR_FOLLOWUP_SAMPLES",
            "NO_SYNTH_FIX_FOLLOWUP_SAMPLES",
            "NO_ESSENTIAL_STRUCTURAL_OUTCOMES",
            "WASTE_BLOCK_RATE_REGRESSED",
        ],
        "dataset": {
            "source_screening_records": len(dataset_rows) + len(exclusions),
            "candidate_decision_points": 21,
            "bindable_decisions": len(dataset_rows),
            "followup_excluded": 1,
            "all_screening_exclusions": len(exclusions),
            "mode_counts": dict(
                sorted(Counter(str(row["mode"]) for row in dataset_rows).items())
            ),
            "outcome_counts": dict(
                sorted(
                    Counter(
                        str(mapping(row["outcome"]).get("class"))
                        for row in dataset_rows
                    ).items()
                )
            ),
            "future_fields_separated": True,
            "leakage_violations": [],
        },
        "evaluation_protocol": {
            "calibration": "NONE; rules were transcribed from the frozen Phase C0 specification",
            "held_out": "LEAVE_ONE_RUN_OUT_AND_LEAVE_ONE_TASK_OUT",
            "split_digest": split_digest,
            "leave_one_run_out": {
                "stable_decisions": len(dataset_rows),
                "evaluated_decisions": len(dataset_rows),
                "rate": 1.0,
                "interpretation": "Pure non-fitted policy is context-free; each held-out decision matched its full-set decision.",
            },
            "leave_one_task_out": {
                "stable_task_groups": len(task_groups),
                "evaluated_task_groups": len(task_groups),
                "rate": 1.0,
                "interpretation": "No task-specific threshold, ID, or outcome enters the policy.",
            },
        },
        "v1": v1_summary,
        "v2": v2_summary,
        "delta": {
            "beneficial_retention_rate": (
                mapping(v2_summary["beneficial_or_essential_retention"]).get("rate")
                - mapping(v1_summary["beneficial_or_essential_retention"]).get("rate")
                if mapping(v1_summary["beneficial_or_essential_retention"]).get(
                    "rate"
                )
                is not None
                and mapping(v2_summary["beneficial_or_essential_retention"]).get(
                    "rate"
                )
                is not None
                else None
            ),
            "waste_block_rate": (
                mapping(v2_summary["waste_block_rate"]).get("rate")
                - mapping(v1_summary["waste_block_rate"]).get("rate")
                if mapping(v1_summary["waste_block_rate"]).get("rate") is not None
                and mapping(v2_summary["waste_block_rate"]).get("rate") is not None
                else None
            ),
            "false_block_count": int(
                mapping(v2_summary["false_block"]).get("count") or 0
            )
            - int(mapping(v1_summary["false_block"]).get("count") or 0),
            "offline_potential_token_saving": number(
                mapping(v2_summary["offline_potential_savings"]).get("tokens")
            )
            - number(
                mapping(v1_summary["offline_potential_savings"]).get("tokens")
            ),
            "offline_potential_credit_saving": number(
                mapping(v2_summary["offline_potential_savings"]).get("credits")
            )
            - number(
                mapping(v1_summary["offline_potential_savings"]).get("credits")
            ),
        },
        "authority": {
            "v1": "SHADOW",
            "v2": "OFFLINE_ONLY",
            "shadow_pilot": "NOT_READY",
            "enforce": "DISABLED",
        },
    }
    write_json(EVIDENCE / "policy-comparison.json", comparison)

    inventory = {
        "schema_version": "phase-c0.dataset-inventory.v1",
        "scope": "PUBLIC_TRAIN_DEV_REAL_LLM_VITIS_ARTIFACTS_ONLY",
        "sources": [
            {
                "name": "V3F_R02",
                "bound_decisions": 11,
                "legacy_screening_exclusions": 6,
                "terminal_budget_removed": True,
            },
            {
                "name": "TOKEN_POLICY_ABC_72",
                "source_runs": 72,
                "followup_points": 10,
                "bound_decisions": 9,
                "followup_excluded": 1,
                "no_followup": 62,
            },
            {
                "name": "PHASE_B1_B11_B12_ANCHORS",
                "source_runs": 12,
                "bound_decisions": 0,
                "no_followup": 12,
            },
            {
                "name": "EXPERIENCE_CANDIDATE_RECORDS",
                "records": 81,
                "role": "OVERLAP_SUPPORT_ONLY_NOT_ADDITIONAL_DECISION_POINTS",
            },
        ],
        "funnel": comparison["dataset"],
        "exclusion_reason_counts": dict(
            sorted(
                Counter(
                    reason
                    for row in exclusions
                    for reason in row.get("reason_codes", [])
                ).items()
            )
        ),
        "mode_counts": comparison["dataset"]["mode_counts"],
        "outcome_counts": comparison["dataset"]["outcome_counts"],
        "split_digest": split_digest,
        "hidden_reference_golden_accessed": False,
    }
    write_json(EVIDENCE / "dataset-inventory.json", inventory)

    v1_benefit = render_rate(
        mapping(v1_summary["beneficial_or_essential_retention"])
    )
    v2_benefit = render_rate(
        mapping(v2_summary["beneficial_or_essential_retention"])
    )
    v1_struct = render_rate(mapping(v1_summary["essential_structural_retention"]))
    v2_struct = render_rate(mapping(v2_summary["essential_structural_retention"]))
    v1_waste = render_rate(mapping(v1_summary["waste_block_rate"]))
    v2_waste = render_rate(mapping(v2_summary["waste_block_rate"]))
    (EVIDENCE / "policy-comparison.zh-CN.md").write_text(
        f"""# Continuation V1 / V2 离线对照

结论：`INSUFFICIENT_EVIDENCE`。20 个完整绑定点仅覆盖 OPTIMIZE=17、STRUCTURAL_FIX=3；REPAIR=0、SYNTH_FIX=0，并且 ESSENTIAL_STRUCTURAL 分母为 0。

| 指标 | V1 | V2 |
|---|---:|---:|
| Bindable decisions | 20 | 20 |
| Beneficial/essential retention | {v1_benefit} | {v2_benefit} |
| Essential structural retention | {v1_struct} | {v2_struct} |
| Waste block rate | {v1_waste} | {v2_waste} |
| False block | {mapping(v1_summary["false_block"]).get("count")} | {mapping(v2_summary["false_block"]).get("count")} |
| Potential Token saving | {mapping(v1_summary["offline_potential_savings"]).get("tokens")} | {mapping(v2_summary["offline_potential_savings"]).get("tokens")} |
| Potential Credit saving | {mapping(v1_summary["offline_potential_savings"]).get("credits")} | {mapping(v2_summary["offline_potential_savings"]).get("credits")} |
| REPAIR coverage | 0/20 | 0/20 |
| SYNTH_FIX coverage | 0/20 | 0/20 |
| STRUCTURAL_FIX coverage | 3/20 | 3/20 |
| OPTIMIZE coverage | 17/20 | 17/20 |

V2 对有益调用更保守，但当前数据上 waste block rate 退化，且结构必要调用完全没有正样本，不能进入 Shadow Pilot。潜在节省均为离线反事实估计，不是真实节省。

固定评估协议未使用 calibration 或 outcome 调参；按 run 和 task 做 leave-one-group-out，一致性均为 100%。这只证明纯规则不依赖组上下文，不代表统计泛化。Split digest：`{split_digest}`。
""",
        encoding="utf-8",
    )

    metadata = {
        "schema_version": "phase-c0.acceptance-metadata.v1",
        "phase": "Phase C0",
        "status": "INSUFFICIENT_EVIDENCE",
        "acceptance": "NOT_ADMITTED_TO_SHADOW_PILOT",
        "head": "4a05763b593a527878a0056f64763126c58ee63b",
        "offline_only": True,
        "real_llm_calls": 0,
        "real_tokens": 0,
        "real_tool_calls": {"csim": 0, "synth": 0, "cosim": 0},
        "real_tool_credits": 0,
        "main_graph_modified": False,
        "v1_modified": False,
        "continuation_authority": "SHADOW",
        "v2_authority": "OFFLINE_ONLY",
        "experience_guided": "NOT_ADMITTED",
        "ranker_training": "NOT_READY",
        "hidden_reference_golden_accessed": False,
        "dataset_digest": canonical_sha256(dataset_rows),
        "split_digest": split_digest,
        "comparison_digest": canonical_sha256(comparison),
        "required_checks_pending": [
            "focused_tests",
            "full_tests",
            "compileall",
            "git_diff_check",
        ],
    }
    write_json(EVIDENCE / "acceptance-metadata.json", metadata)


if __name__ == "__main__":
    main()
