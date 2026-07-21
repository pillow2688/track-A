"""Leave-one-run-out evaluator for V3-E guidance quality.

The evaluator is offline and public-feature-only.  For every eligible real
Candidate it removes the complete originating run, rebuilds retrieval/ranking,
and measures whether the quality gate recommends the held-out success or the
held-out failure.  No model, Vitis tool or LangGraph node is invoked.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from .v3_experience import (
    EXPERIENCE_QUERY_SCHEMA,
    canonical_json,
    canonical_sha256,
    normalize_strategy_bundle,
    validate_experience_query,
)
from .v3_experience_guidance import BayesianStrategyRanker, WeightedKNNRetriever
from .v3_experience_quality import GuidanceQualityConfig, GuidanceQualityGate
from .v3_experience_store import JsonlExperienceRepository


LORO_ROW_SCHEMA = "v3e.guidance-loro-row.v1"
LORO_REPORT_SCHEMA = "v3e.guidance-loro-report.v1"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bundle(value: object) -> tuple[str, ...]:
    return tuple(normalize_strategy_bundle(value))


def _success(record: Mapping[str, object]) -> bool:
    outcome = _mapping(record.get("outcome"))
    mode = record.get("mode")
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


def _query(
    heldout: Mapping[str, object], *, attempted: Sequence[Sequence[str]] = ()
) -> dict[str, object]:
    identity = {
        "heldout_record_id": heldout["record_id"],
        "attempted": [list(item) for item in attempted],
    }
    value: dict[str, object] = {
        "schema_version": EXPERIENCE_QUERY_SCHEMA,
        "query_id": canonical_sha256(identity),
        "run_id": f"loro-{str(heldout['record_id'])[:24]}",
        "candidate_id": None,
        # The hash is a schema requirement only.  Retrieval/ranking never use it.
        "task_id_hash": "0" * 64,
        "task_split": "hidden_like",
        "mode": heldout["mode"],
        "difficulty": heldout["difficulty"],
        "algorithm_family": heldout["algorithm_family"],
        "evidence_features": heldout["evidence_features"],
        "attempted_strategy_bundles": [list(item) for item in attempted],
        "attempted_patch_hashes": [],
        "remaining_credits": 100,
        "remaining_tokens": 32768,
        "no_improvement_rounds": 0,
    }
    return validate_experience_query(value)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _metrics(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    total = len(rows)
    success_rows = [item for item in rows if item.get("heldout_success") is True]
    failed_rows = [item for item in rows if item.get("heldout_success") is False]
    suppression_rows = [
        item for item in failed_rows if item.get("suppression_evaluable") is True
    ]
    tokens = [int(item.get("guidance_tokens") or 0) for item in rows]
    return {
        "evaluated": total,
        "coverage": _rate(
            sum(item.get("quality_decision") == "INJECT" for item in rows), total
        ),
        "success_strategy_hit_rate": _rate(
            sum(item.get("success_strategy_hit") is True for item in success_rows),
            len(success_rows),
        ),
        "harmful_recommendation_rate": _rate(
            sum(item.get("harmful_recommendation") is True for item in failed_rows),
            len(failed_rows),
        ),
        "abstain_rate": _rate(
            sum(item.get("quality_decision") == "ABSTAIN" for item in rows), total
        ),
        "duplicate_failure_suppression_rate": _rate(
            sum(
                item.get("duplicate_failure_suppressed") is True
                for item in suppression_rows
            ),
            len(suppression_rows),
        ),
        "average_guidance_tokens": (
            round(sum(tokens) / len(tokens), 6) if tokens else None
        ),
        "high_confidence_injections": sum(
            item.get("quality_decision") == "INJECT"
            and (_number(item.get("confidence")) or 0) >= 0.5
            for item in rows
        ),
        "success_cases": len(success_rows),
        "failure_cases": len(failed_rows),
    }


def evaluate_leave_one_run_out(
    records: Sequence[Mapping[str, object]],
    *,
    quality_gate: GuidanceQualityGate | None = None,
    retriever: WeightedKNNRetriever | None = None,
    ranker: BayesianStrategyRanker | None = None,
    harmful_threshold: float = 0.10,
    minimum_high_confidence: int = 2,
    guidance_token_limit: int = 600,
) -> dict[str, object]:
    if not 0 <= harmful_threshold <= 1:
        raise ValueError("harmful_threshold must be in [0,1]")
    gate = quality_gate or GuidanceQualityGate(
        GuidanceQualityConfig(max_prompt_tokens=guidance_token_limit)
    )
    case_retriever = retriever or WeightedKNNRetriever()
    strategy_ranker = ranker or BayesianStrategyRanker()
    eligible = [
        dict(item)
        for item in records
        if item.get("execution_class") == "REAL_LLM_VITIS"
        and item.get("eligible_for_ranking") is True
        and item.get("task_split") == "train"
    ]
    eligible.sort(key=lambda item: str(item.get("record_id")))
    rows: list[dict[str, object]] = []
    for heldout in eligible:
        training = [
            item for item in eligible if item.get("run_id") != heldout.get("run_id")
        ]
        query = _query(heldout)
        retrieval = case_retriever.retrieve(query, training)
        ranking = strategy_ranker.rank(query, retrieval.considered)
        quality = gate.evaluate(
            query,
            retrieval,
            ranking,
            prompt_token_limit=guidance_token_limit,
        )
        heldout_bundle = _bundle(
            _mapping(heldout.get("proposal_features")).get("strategy_bundle")
        )
        recommended = {
            _bundle(item)
            for item in quality.decision["recommended_strategies"]
            if _bundle(item)
        }
        heldout_success = _success(heldout)
        suppression_evaluable = bool(not heldout_success and heldout_bundle)
        suppressed = False
        if suppression_evaluable:
            suppression_query = _query(heldout, attempted=[heldout_bundle])
            suppression_retrieval = case_retriever.retrieve(
                suppression_query, training
            )
            suppression_ranking = strategy_ranker.rank(
                suppression_query, suppression_retrieval.considered
            )
            suppression_quality = gate.evaluate(
                suppression_query,
                suppression_retrieval,
                suppression_ranking,
                prompt_token_limit=guidance_token_limit,
            )
            suppression_recommended = {
                _bundle(item)
                for item in suppression_quality.decision["recommended_strategies"]
                if _bundle(item)
            }
            suppressed = heldout_bundle not in suppression_recommended
        injected = quality.decision["decision"] == "INJECT"
        rows.append(
            {
                "schema_version": LORO_ROW_SCHEMA,
                "heldout_record_id": heldout["record_id"],
                "mode": heldout["mode"],
                "heldout_success": heldout_success,
                "heldout_strategy_bundle": list(heldout_bundle),
                "quality_decision": quality.decision["decision"],
                "confidence": quality.decision["confidence"],
                "support_count": quality.decision["support_count"],
                "similarity_max": quality.decision["similarity_max"],
                "similarity_mean": quality.decision["similarity_mean"],
                "recommended_strategies": quality.decision[
                    "recommended_strategies"
                ],
                "abstain_reason": quality.decision["abstain_reason"],
                "guidance_tokens": quality.decision["guidance_tokens"],
                "success_strategy_hit": bool(
                    injected and heldout_success and heldout_bundle in recommended
                ),
                "harmful_recommendation": bool(
                    injected and not heldout_success and heldout_bundle in recommended
                ),
                "suppression_evaluable": suppression_evaluable,
                "duplicate_failure_suppressed": suppressed,
            }
        )

    overall = _metrics(rows)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["mode"])].append(row)
    by_mode = {mode: _metrics(grouped.get(mode, [])) for mode in (
        "REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"
    )}
    blockers: list[str] = []
    harmful = overall["harmful_recommendation_rate"]
    if harmful is None or harmful > harmful_threshold:
        blockers.append("HARMFUL_RECOMMENDATION_RATE_TOO_HIGH_OR_UNKNOWN")
    if int(overall["high_confidence_injections"]) < minimum_high_confidence:
        blockers.append("INSUFFICIENT_HIGH_CONFIDENCE_INJECTIONS")
    average_tokens = overall["average_guidance_tokens"]
    if average_tokens is None or average_tokens > guidance_token_limit:
        blockers.append("GUIDANCE_TOKEN_LIMIT_EXCEEDED_OR_UNKNOWN")
    # The evaluator schema never emits task IDs, paths, source, Patch or logs.
    leakage_checks = {
        "task_id_excluded": True,
        "hidden_golden_fields_excluded": True,
        "train_only_consumption": True,
    }
    report: dict[str, object] = {
        "schema_version": LORO_REPORT_SCHEMA,
        "experience_records": len(records),
        "eligible_real_train_candidates": len(eligible),
        "overall": overall,
        "by_mode": by_mode,
        "guided_decision": "RUN_GUIDED" if not blockers else "SKIP_GUIDED",
        "guided_blockers": blockers,
        "thresholds": {
            "harmful_recommendation_rate_max": harmful_threshold,
            "minimum_high_confidence_injections": minimum_high_confidence,
            "guidance_token_limit": guidance_token_limit,
        },
        "leakage_checks": leakage_checks,
        "rows": rows,
    }
    return report


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _render_markdown(report: Mapping[str, object]) -> str:
    overall = _mapping(report.get("overall"))
    lines = [
        "# V3-E Guidance Leave-One-Run-Out 评估",
        "",
        f"- Experience records: {report.get('experience_records')}",
        f"- Eligible real train Candidates: {report.get('eligible_real_train_candidates')}",
        f"- Guided decision: `{report.get('guided_decision')}`",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
    ]
    for key in (
        "coverage",
        "success_strategy_hit_rate",
        "harmful_recommendation_rate",
        "abstain_rate",
        "duplicate_failure_suppression_rate",
        "average_guidance_tokens",
    ):
        lines.append(f"| `{key}` | {overall.get(key)} |")
    lines.extend(["", "## 按 mode", "", "| Mode | Evaluated | Coverage | Hit | Harmful | Abstain |", "|---|---:|---:|---:|---:|---:|"])
    for mode, raw in _mapping(report.get("by_mode")).items():
        item = _mapping(raw)
        lines.append(
            f"| {mode} | {item.get('evaluated')} | {item.get('coverage')} | "
            f"{item.get('success_strategy_hit_rate')} | "
            f"{item.get('harmful_recommendation_rate')} | {item.get('abstain_rate')} |"
        )
    blockers = list(report.get("guided_blockers") or [])
    lines.extend(["", "## Guided gate", ""])
    lines.append("- 无阻塞项。" if not blockers else "- " + "\n- ".join(blockers))
    return "\n".join(lines) + "\n"


def write_loro_report(report: Mapping[str, object], output_dir: str | Path) -> None:
    output = Path(output_dir).resolve()
    rows = list(report.get("rows") or [])
    summary = dict(report)
    summary.pop("rows", None)
    _atomic_bytes(output / "loro_results.jsonl", b"".join(canonical_json(item) + b"\n" for item in rows))
    _atomic_bytes(output / "loro_report.json", canonical_json(summary) + b"\n")
    _atomic_bytes(output / "loro_report.md", _render_markdown(summary).encode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm4hls-v3e-loro")
    parser.add_argument("--experience-store", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--guidance-token-limit", type=int, default=600)
    parser.add_argument("--harmful-threshold", type=float, default=0.10)
    parser.add_argument("--minimum-high-confidence", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    repository = JsonlExperienceRepository(Path(args.experience_store))
    records = repository.latest_trajectories(ranking_only=True)
    report = evaluate_leave_one_run_out(
        records,
        harmful_threshold=args.harmful_threshold,
        minimum_high_confidence=args.minimum_high_confidence,
        guidance_token_limit=args.guidance_token_limit,
    )
    write_loro_report(report, args.output_dir)
    payload = dict(report)
    payload.pop("rows", None)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stdout)
    return 0


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()


__all__ = ["evaluate_leave_one_run_out", "write_loro_report"]
