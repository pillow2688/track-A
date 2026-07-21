"""Idempotent post-terminal attribution for V3-E recommendations.

Attribution is derived from immutable recommendation, Planner, Candidate and
tool artifacts after the existing Graph reaches a terminal state.  It therefore
adds no Graph node and cannot affect Candidate promotion or final validation.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import canonical_json, canonical_sha256, normalize_strategy_bundle
from .v3_experience_normalizer import StrategyNormalizer
from .v3_experience_v2 import MODES


ATTRIBUTION_SCHEMA = "v3e.recommendation-attribution.v2"
ATTRIBUTION_SUMMARY_SCHEMA = "v3e.recommendation-attribution-summary.v1"
ADHERENCE = frozenset(
    {"FOLLOWED", "PARTIALLY_FOLLOWED", "IGNORED", "CONTRADICTED"}
)
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _bundle(value: object) -> tuple[str, ...]:
    try:
        return tuple(normalize_strategy_bundle(value))
    except ValueError:
        return ()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _read_jsonl(path: Path, *, limit: int = 128) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    result: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    if len(lines) > limit:
        raise ValueError("recommendation attribution input exceeds bounded limit")
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid recommendation JSONL") from exc
        if isinstance(value, Mapping):
            result.append(dict(value))
    return result


def _status(value: object) -> str:
    record = _mapping(value)
    if record.get("status") == "PASS" or record.get("ok") is True:
        return "PASS"
    if record.get("status") in {"FAIL", "FAILED"} or record.get("ok") is False:
        return "FAIL"
    return "NOT_RUN"


def _final_status(result: Mapping[str, object], candidate_id: str | None) -> str:
    if not candidate_id or candidate_id != result.get("final_candidate_id"):
        return "NOT_RUN"
    stages = _mapping(result.get("final_validation"))
    statuses = [_status(stages.get(name)) for name in ("csim", "synth", "cosim")]
    if all(item == "PASS" for item in statuses):
        return "PASS"
    if any(item == "FAIL" for item in statuses):
        return "FAIL"
    return "NOT_RUN"


def _adherence(
    selected: tuple[str, ...],
    recommended: set[tuple[str, ...]],
    discouraged: set[tuple[str, ...]],
) -> str:
    if not selected:
        return "IGNORED"
    selected_set = set(selected)
    if selected in discouraged or any(
        selected_set.intersection(bundle) for bundle in discouraged
    ):
        return "CONTRADICTED"
    if selected in recommended:
        return "FOLLOWED"
    if any(selected_set.intersection(bundle) for bundle in recommended):
        return "PARTIALLY_FOLLOWED"
    return "IGNORED"


def _recommendation_strategies(
    recommendation: Mapping[str, object], name: str
) -> set[tuple[str, ...]]:
    quality = _mapping(recommendation.get("quality_decision"))
    # An abstention never reached the Planner prompt.  Candidate overlap in
    # that case is coincidence, not recommendation adherence.
    if quality and quality.get("decision") != "INJECT":
        return set()
    key = f"{name}_strategies"
    values = _sequence(quality.get(key))
    if values:
        return {bundle for item in values for bundle in [_bundle(item)] if bundle}
    guidance = _mapping(recommendation.get("guidance"))
    fallback_key = f"{name}_strategy_bundles"
    return {
        bundle
        for item in _sequence(guidance.get(fallback_key))
        for bundle in [_bundle(_mapping(item).get("strategy_bundle"))]
        if bundle
    }


def _proposal(run_root: Path, round_index: int) -> dict[str, object]:
    return _read_json(run_root / "planner" / f"proposal_{round_index:03d}.json")


def _actions(run_root: Path, candidate_id: str | None) -> list[dict[str, object]]:
    if not candidate_id:
        return []
    root = run_root / "actions"
    if not root.is_dir():
        return []
    values: list[dict[str, object]] = []
    for path in sorted(root.glob("*/result.json"))[:256]:
        value = _read_json(path)
        if value.get("candidate_id") == candidate_id:
            values.append(value)
    return values


def _candidate_record(
    registry: Mapping[str, object], candidate_id: str | None
) -> Mapping[str, object]:
    if not candidate_id:
        return {}
    return _mapping(_mapping(registry.get("candidates")).get(candidate_id))


def _relative_text(root: Path, reference: object) -> str:
    if not isinstance(reference, str) or not reference:
        return ""
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        return ""
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _candidate_observed_strategy(
    root: Path,
    terminal: Mapping[str, object],
    candidate: Mapping[str, object],
    declared: tuple[str, ...],
) -> tuple[tuple[str, ...], float, tuple[str, ...]]:
    mode = str(terminal.get("mode") or "")
    if mode not in MODES:
        mode = "OPTIMIZE"
    patch = _relative_text(root, candidate.get("patch_ref"))
    candidate_source = _relative_text(root, candidate.get("source_ref"))
    parent_id = candidate.get("parent_id")
    parent_source = ""
    if isinstance(parent_id, str) and parent_id != "candidate_000":
        parent = _candidate_record(_read_json(root / "candidate_registry.json"), parent_id)
        parent_source = _relative_text(root, parent.get("source_ref"))
    if not parent_source:
        baseline_sources = sorted((root / "baseline" / "source").glob("*.cpp"))
        if baseline_sources:
            try:
                parent_source = baseline_sources[0].read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                parent_source = ""
    normalized = StrategyNormalizer().normalize(
        mode=mode,
        declared_strategy=declared,
        parent_source=parent_source,
        candidate_source=candidate_source,
        patch=patch,
        validation={"patch_valid": bool(candidate)},
    )
    return (
        normalized.observed_strategy_atoms,
        normalized.confidence,
        normalized.reason_codes,
    )


def _same_family_support(recommendation: Mapping[str, object]) -> bool:
    guidance = _mapping(recommendation.get("guidance"))
    cases = _sequence(guidance.get("similar_successes")) + _sequence(
        guidance.get("similar_failures")
    )
    return any(
        str(_mapping(item).get("family_relation") or "").upper() == "SAME"
        for item in cases
    )


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(canonical_json(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _append_idempotent(path: Path, records: Sequence[Mapping[str, object]]) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    inserted = 0
    duplicates = 0
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            data = handle.read()
            if data and not data.endswith(b"\n"):
                newline = data.rfind(b"\n")
                handle.seek(0 if newline < 0 else newline + 1)
                handle.truncate()
                handle.seek(0)
                data = handle.read()
            existing: dict[str, bytes] = {}
            for raw in data.splitlines():
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError("corrupt attribution JSONL") from exc
                identity = str(_mapping(value).get("attribution_id") or "")
                if identity:
                    existing[identity] = canonical_json(value)
            handle.seek(0, os.SEEK_END)
            for record in records:
                encoded = canonical_json(record)
                identity = str(record["attribution_id"])
                if identity in existing:
                    if existing[identity] != encoded:
                        raise ValueError("attribution ID collision")
                    duplicates += 1
                    continue
                handle.write(encoded + b"\n")
                existing[identity] = encoded
                inserted += 1
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return inserted, duplicates


def build_recommendation_attributions(
    run_root: str | Path,
    result: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], ...]:
    root = Path(run_root).resolve()
    terminal = dict(result) if isinstance(result, Mapping) else _read_json(
        root / "v3_prototype_result.json"
    )
    recommendations = _read_jsonl(
        root / "experience" / "experience_recommendations.jsonl"
    )
    if not recommendations:
        return ()
    registry = _read_json(root / "candidate_registry.json")
    rounds = {
        _nonnegative_int(_mapping(item).get("round")): _mapping(item)
        for item in _sequence(terminal.get("candidate_rounds"))
        if _nonnegative_int(_mapping(item).get("round")) > 0
    }
    records: list[dict[str, object]] = []
    run_identity = str(terminal.get("run_id") or root.name)
    previous_acceleration = 1.0
    for recommendation in recommendations:
        recommendation_id = str(recommendation.get("recommendation_id") or "")
        if _SHA256.fullmatch(recommendation_id) is None:
            continue
        round_index = _nonnegative_int(recommendation.get("round_index"))
        if round_index <= 0:
            continue
        row = rounds.get(round_index, {})
        candidate_id_raw = row.get("candidate_id")
        candidate_id = str(candidate_id_raw) if candidate_id_raw else None
        proposal = _proposal(root, round_index)
        selected = _bundle(
            row.get("strategy_bundle")
            or row.get("change_class")
            or proposal.get("change_class")
        )
        recommended = _recommendation_strategies(recommendation, "recommended")
        discouraged = _recommendation_strategies(recommendation, "discouraged")
        candidate = _candidate_record(registry, candidate_id)
        observed, observed_confidence, observed_reasons = (
            _candidate_observed_strategy(root, terminal, candidate, selected)
        )
        validation = _mapping(candidate.get("validation"))
        actions = _actions(root, candidate_id)
        proposal_tokens = _nonnegative_int(proposal.get("input_tokens")) + _nonnegative_int(
            proposal.get("output_tokens")
        )
        tool_wall = sum(_number(item.get("elapsed_s")) or 0.0 for item in actions)
        planner_wall = _number(proposal.get("duration_seconds")) or 0.0
        acceleration = _number(row.get("acceleration_vs_baseline"))
        if acceleration is None:
            acceleration = _number(_mapping(candidate.get("outcome")).get("acceleration"))
        decision = str(row.get("decision") or "NOT_MATERIALIZED")
        promoted = decision in {"PROMOTED", "FINAL_VERIFIED", "ACCEPTED"} or str(
            candidate.get("status") or ""
        ) in {"PROMOTED", "FINAL_VERIFIED", "CORRECTNESS_VERIFIED"}
        final_status = _final_status(terminal, candidate_id)
        attribution_id = canonical_sha256(
            {
                "run_id": run_identity,
                "recommendation_id": recommendation_id,
                "round_index": round_index,
                "candidate_id": candidate_id,
                "selected_strategy_bundle": list(selected),
            }
        )
        quality = _mapping(recommendation.get("quality_decision"))
        declared_relation = _adherence(selected, recommended, discouraged)
        observed_relation = _adherence(observed, recommended, discouraged)
        recommended_atoms = {atom for bundle in recommended for atom in bundle}
        declared_hits = sorted(set(selected).intersection(recommended_atoms))
        observed_hits = sorted(set(observed).intersection(recommended_atoms))
        marginal_gain = (
            round(acceleration - previous_acceleration, 8)
            if acceleration is not None
            else None
        )
        if acceleration is not None:
            previous_acceleration = max(previous_acceleration, acceleration)
        query_features = _mapping(recommendation.get("query_features"))
        attempted = {
            bundle
            for item in _sequence(query_features.get("attempted_strategy_bundles"))
            for bundle in [_bundle(item)]
            if bundle
        }
        quality_confidence = _number(quality.get("confidence")) or 0.0
        records.append(
            {
                "schema_version": ATTRIBUTION_SCHEMA,
                "attribution_id": attribution_id,
                "recommendation_id": recommendation_id,
                "round_index": round_index,
                "quality_decision": str(quality.get("decision") or "UNKNOWN"),
                "planner_strategy_bundle": list(selected),
                "observed_strategy_atoms": list(observed),
                "declared_strategy_relation": declared_relation,
                "observed_strategy_relation": observed_relation,
                "adherence": observed_relation,
                "recommended_atom_hits": {
                    "declared": declared_hits,
                    "observed": observed_hits,
                },
                "recommendation_same_task_family_support": _same_family_support(
                    recommendation
                ),
                "patch_valid": bool(candidate_id),
                "validation": {
                    "csim": _status(validation.get("csim")),
                    "synth": _status(validation.get("synth")),
                    "cosim": _status(validation.get("cosim")),
                    "final": final_status,
                },
                "promoted": promoted,
                "acceleration": acceleration,
                "marginal_acceleration_gain": marginal_gain,
                "attribution_confidence": round(
                    min(observed_confidence, quality_confidence), 6
                ),
                "planner_may_have_chosen_without_guidance": bool(
                    quality.get("decision") != "INJECT"
                ),
                "avoided_repeated_failed_strategy": bool(
                    selected and selected not in attempted
                ),
                "causal_claim": False,
                "usage": {
                    "tokens": proposal_tokens,
                    "guidance_tokens": _nonnegative_int(
                        quality.get("guidance_tokens")
                    ),
                    "credits": _nonnegative_int(row.get("credits")),
                    "wall_time_seconds": round(planner_wall + tool_wall, 6),
                },
                "normalization": {
                    "confidence": observed_confidence,
                    "reason_codes": list(observed_reasons),
                },
                "candidate_id": candidate_id,
                "decision": decision,
                "decision_reason": str(row.get("decision_reason") or "UNKNOWN"),
            }
        )
    return tuple(records)


def persist_recommendation_attributions(
    run_root: str | Path,
    result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    root = Path(run_root).resolve()
    records = build_recommendation_attributions(root, result)
    path = root / "experience" / "recommendation_attributions.jsonl"
    inserted, duplicates = _append_idempotent(path, records) if records else (0, 0)
    counts = Counter(str(item["adherence"]) for item in records)
    summary: dict[str, object] = {
        "schema_version": ATTRIBUTION_SUMMARY_SCHEMA,
        "record_count": len(records),
        "inserted": inserted,
        "duplicates": duplicates,
        "adherence_counts": dict(sorted(counts.items())),
        "promoted": sum(item.get("promoted") is True for item in records),
        "final_pass": sum(
            _mapping(item.get("validation")).get("final") == "PASS"
            for item in records
        ),
        "attributions_ref": "experience/recommendation_attributions.jsonl",
    }
    _atomic_json(root / "experience" / "recommendation_attribution_summary.json", summary)
    return summary


__all__ = [
    "ADHERENCE",
    "ATTRIBUTION_SCHEMA",
    "build_recommendation_attributions",
    "persist_recommendation_attributions",
]
