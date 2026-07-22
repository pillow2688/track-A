"""Leakage-safe offline replay utilities for V3-F continuation decisions.

The builder keeps decision-time fields under ``pre_state`` and writes the
subsequent Candidate outcome separately.  The evaluator accepts only
``pre_state`` when calling the policy; tests assert this boundary explicitly.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from .v3_continuation import (
    continuation_cost,
    continuation_decision,
    evidence_delta,
    performance_area_delta,
    strategy_novelty,
)


REPLAY_SAMPLE_SCHEMA = "v3.continuation-replay-sample.v1"
REPLAY_MANIFEST_SCHEMA = "v3.continuation-replay-manifest.v1"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _report(run_root: Path, reference: object) -> Mapping[str, object]:
    if not isinstance(reference, str) or not reference:
        return {}
    path = (run_root / reference).resolve()
    try:
        path.relative_to(run_root.resolve())
    except ValueError:
        return {}
    if not path.is_file():
        return {}
    value = _read(path)
    report = value.get("report")
    return report if isinstance(report, Mapping) else {}


def _evidence(run_root: Path, reference: object) -> Mapping[str, object]:
    if not isinstance(reference, str) or not reference:
        return {}
    path = (run_root / reference).resolve()
    try:
        path.relative_to(run_root.resolve())
    except ValueError:
        return {}
    return _read(path) if path.is_file() else {}


def _outcome(parent: Mapping[str, object], candidate: Mapping[str, object], parent_metrics: Mapping[str, object], candidate_metrics: Mapping[str, object]) -> str:
    status = str(candidate.get("status", ""))
    if status.startswith("REJECTED"):
        return "HARMFUL"
    candidate_latency = ((candidate_metrics.get("latency") or {}) if isinstance(candidate_metrics.get("latency"), Mapping) else {}).get("worst")
    parent_latency = ((parent_metrics.get("latency") or {}) if isinstance(parent_metrics.get("latency"), Mapping) else {}).get("worst")
    if isinstance(candidate_latency, (int, float)) and isinstance(parent_latency, (int, float)) and candidate_latency < parent_latency:
        return "BENEFICIAL_PERFORMANCE"
    if status in {"PROMOTED", "FINAL_VERIFIED", "CORRECTNESS_VERIFIED"}:
        return "ESSENTIAL_FOR_CORRECTNESS"
    return "NEUTRAL"


def build_replay_dataset(run_roots: Iterable[str | Path], output_dir: str | Path) -> dict[str, object]:
    """Import only real, public V3 runs with sufficient bound artifacts."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    samples: list[dict[str, object]] = []
    excluded: Counter[str] = Counter()
    source_count = 0
    seen: set[tuple[str, int]] = set()
    for root_value in run_roots:
        run_root = Path(root_value)
        result_path = run_root / "v3_prototype_result.json"
        registry_path = run_root / "candidate_registry.json"
        if not result_path.is_file() or not registry_path.is_file():
            excluded["MISSING_V3_ARTIFACTS"] += 1
            continue
        result, registry = _read(result_path), _read(registry_path)
        if result.get("backend", {}).get("evidence_level") not in {"REAL_VITIS_VALIDATED", "REAL_VITIS_ATTEMPT_FAILED"} if isinstance(result.get("backend"), Mapping) else True:
            excluded["NOT_REAL_VITIS"] += 1
            continue
        if result.get("task_id", "").__class__ is not str or "hidden" in str(result.get("task_id", "")).lower():
            excluded["HIDDEN_OR_INVALID_TASK"] += 1
            continue
        candidates_raw = registry.get("candidates")
        if not isinstance(candidates_raw, Mapping):
            excluded["INVALID_REGISTRY"] += 1
            continue
        candidates = [(str(key), value) for key, value in candidates_raw.items() if isinstance(value, Mapping) and value.get("kind") != "baseline"]
        candidates.sort(key=lambda item: int(item[1].get("round_index", 0) or 0))
        by_id = {key: value for key, value in candidates_raw.items() if isinstance(value, Mapping)}
        for candidate_id, candidate in candidates:
            round_index = candidate.get("round_index")
            if not isinstance(round_index, int) or round_index <= 1:
                continue
            parent_id = candidate.get("parent_id")
            parent = by_id.get(parent_id)
            if not isinstance(parent, Mapping):
                excluded["MISSING_PARENT"] += 1
                continue
            parent_metrics, candidate_metrics = _report(run_root, parent.get("metrics_ref")), _report(run_root, candidate.get("metrics_ref"))
            if not parent_metrics or not candidate_metrics:
                excluded["MISSING_SYNTH_METRICS"] += 1
                continue
            mode = str(result.get("mode") or "OPTIMIZE")
            if mode not in {"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"}:
                excluded["UNKNOWN_MODE"] += 1
                continue
            sample_key = (str(result.get("run_dir") or run_root.name), round_index)
            if sample_key in seen:
                excluded["DUPLICATE"] += 1
                continue
            seen.add(sample_key)
            parent_parent_id = parent.get("parent_id")
            parent_parent = by_id.get(parent_parent_id)
            before_metrics = _report(run_root, parent_parent.get("metrics_ref")) if isinstance(parent_parent, Mapping) else parent_metrics
            before_evidence = _evidence(run_root, parent_parent.get("synth_evidence_ref")) if isinstance(parent_parent, Mapping) else {}
            after_evidence = _evidence(run_root, parent.get("synth_evidence_ref"))
            pre_state = {
                "mode": mode,
                "round_index": round_index,
                "has_correct_candidate": str(parent.get("status", "")) in {"PROMOTED", "FINAL_VERIFIED", "CORRECTNESS_VERIFIED"},
                "parent_metrics": parent_metrics,
                "previous_metrics": before_metrics,
                "previous_evidence": before_evidence,
                "current_evidence": after_evidence,
                "attempted_strategy_atoms": [str(item.get("change_class", "")) for _, item in candidates if int(item.get("round_index", 0) or 0) < round_index],
                "budget": result.get("budget", {}),
            }
            samples.append({
                "schema_version": REPLAY_SAMPLE_SCHEMA,
                "sample_id": f"{run_root.name}:round:{round_index}",
                "source_kind": "REAL_LLM_VITIS",
                "pre_state": pre_state,
                "outcome": {
                    "label": _outcome(parent, candidate, parent_metrics, candidate_metrics),
                    "actual_tokens": candidate.get("input_tokens", 0) + candidate.get("output_tokens", 0),
                    "actual_credits": candidate.get("credits_used", 0),
                    "candidate_status": candidate.get("status"),
                },
            })
            source_count += 1
    dataset_path = output / "continuation_replay_dataset.jsonl"
    dataset_path.write_text("".join(json.dumps(sample, sort_keys=True) + "\n" for sample in samples), encoding="utf-8")
    manifest = {"schema_version": REPLAY_MANIFEST_SCHEMA, "sample_count": len(samples), "source_count": source_count, "excluded": dict(sorted(excluded.items())), "future_fields_separated": True, "status": "READY" if samples else "DESCRIPTIVE_ONLY"}
    (output / "continuation_replay_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "continuation_replay_data_quality.md").write_text("# Continuation replay data quality\n\n- Future Candidate and final values are stored only under `outcome`.\n- The policy evaluator reads only `pre_state`.\n- Status: `" + str(manifest["status"]) + "`.\n", encoding="utf-8")
    return manifest


def evaluate_replay(dataset_path: str | Path, output_path: str | Path | None = None) -> dict[str, object]:
    rows = [json.loads(line) for line in Path(dataset_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    counts: Counter[str] = Counter()
    by_mode: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        pre = row["pre_state"]
        if not isinstance(pre, Mapping):
            raise ValueError("replay row lacks pre_state")
        mode = str(pre["mode"])
        metrics = pre.get("parent_metrics") if isinstance(pre.get("parent_metrics"), Mapping) else {}
        # No future Candidate, Patch, response, final metric, or outcome is passed here.
        before_metrics = pre.get("previous_metrics") if isinstance(pre.get("previous_metrics"), Mapping) else metrics
        before_evidence = pre.get("previous_evidence") if isinstance(pre.get("previous_evidence"), Mapping) else {}
        current_evidence = pre.get("current_evidence") if isinstance(pre.get("current_evidence"), Mapping) else {}
        delta = evidence_delta(before_evidence, current_evidence, mode=mode, before_metrics=before_metrics, after_metrics=metrics)
        strategies = strategy_novelty(attempted=[(item,) for item in pre.get("attempted_strategy_atoms", []) if item])
        decision = continuation_decision(
            run_id="replay", round_index=int(pre["round_index"]), mode=mode, policy_mode="enforce",
            has_correct_candidate=bool(pre.get("has_correct_candidate")), has_strict_latency_improvement=False,
            performance_area=performance_area_delta(before_metrics, metrics), delta=delta, strategies=strategies,
            cost=continuation_cost(ledger=pre.get("budget") if isinstance(pre.get("budget"), Mapping) else {}, estimated_input_tokens=0, estimated_output_tokens=0, estimated_credits=0, estimated_wall_time_seconds=0, final_reserve_safe=True),
            remaining_rounds=1,
        )
        outcome = row.get("outcome") if isinstance(row.get("outcome"), Mapping) else {}
        label = str(outcome.get("label", "UNKNOWN"))
        key = "ALLOW" if decision["decision"] == "ALLOW" else "BLOCK"
        counts["followups"] += 1; counts[key] += 1; counts[f"outcome_{label}"] += 1
        by_mode[mode][key] += 1; by_mode[mode][f"outcome_{label}"] += 1
        if label in {"NEUTRAL", "HARMFUL"} and key == "BLOCK": counts["waste_blocked"] += 1
        if label.startswith("BENEFICIAL") and key == "ALLOW": counts["beneficial_retained"] += 1
        if label == "ESSENTIAL_FOR_CORRECTNESS" and key == "ALLOW": counts["essential_retained"] += 1
    denominator_waste = counts["outcome_NEUTRAL"] + counts["outcome_HARMFUL"]
    denominator_beneficial = sum(counts[f"outcome_{label}"] for label in ("BENEFICIAL_PERFORMANCE", "BENEFICIAL_AREA", "BENEFICIAL_BALANCED_PA"))
    result = {"schema_version": "v3.continuation-replay-evaluation.v1", "followup_total": counts["followups"], "decisions": {"allow": counts["ALLOW"], "block": counts["BLOCK"]}, "essential_retention": None if not counts["outcome_ESSENTIAL_FOR_CORRECTNESS"] else counts["essential_retained"] / counts["outcome_ESSENTIAL_FOR_CORRECTNESS"], "beneficial_retention": None if not denominator_beneficial else counts["beneficial_retained"] / denominator_beneficial, "waste_block_rate": None if not denominator_waste else counts["waste_blocked"] / denominator_waste, "outcomes": dict(counts), "by_mode": {mode: dict(values) for mode, values in by_mode.items()}, "status": "DESCRIPTIVE_ONLY" if len(rows) < 10 else "EVALUATED"}
    if output_path is not None:
        Path(output_path).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
