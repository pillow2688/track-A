"""Migration, quality, coverage and gap-driven collection for V3-E experience.

All analysis consumes immutable derived metadata.  The collection queue may
name public train/dev tasks for operations, but task identity is never copied
into retrieval or ranking features.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import canonical_json, canonical_sha256, validate_experience_record
from .v3_experience_normalizer import MigrationContext, migrate_v1_to_v2
from .v3_experience_kb import ExperienceKnowledgeBase
from .v3_experience_store import JsonlExperienceRepository
from .v3_experience_v2 import (
    MODES,
    STRATEGIES_BY_MODE,
    validate_experience_v2,
)


DATA_QUALITY_SCHEMA = "v3e.experience-data-quality.v2"
COVERAGE_SCHEMA = "v3e.experience-coverage.v2"
COLLECTION_QUEUE_SCHEMA = "v3e.experience-collection-queue.v1"
BACKFILL_SCHEMA = "v3e.experience-backfill.v1"

MODE_TARGETS = {
    "REPAIR": 15,
    "SYNTH_FIX": 15,
    "STRUCTURAL_FIX": 15,
    "OPTIMIZE": 25,
}

_FORBIDDEN_PATH_PARTS = {"hidden", "hidden_like", "golden", "reference", "answer"}


def _safe_public_path(path: Path) -> bool:
    return not any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in path.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class ArtifactResolution:
    context: MigrationContext
    matched_artifact_hashes: tuple[str, ...]


class PublicRunArtifactResolver:
    """Resolve v1 hashes to public run artifacts without storing their content."""

    def __init__(self, roots: Sequence[str | Path]) -> None:
        self._by_digest: dict[str, Path] = {}
        patterns = (
            "**/candidates/*/patch.diff",
            "**/planner/proposal_*.json",
            "**/planner/requests/*.json",
            "**/planner/live_outcomes/*.json",
            "**/control/package_manifest.json",
        )
        for root_value in roots:
            root = Path(root_value)
            if not root.exists() or not _safe_public_path(root):
                continue
            for pattern in patterns:
                for path in sorted(root.glob(pattern)):
                    if path.is_file() and _safe_public_path(path):
                        self._by_digest.setdefault(_sha256(path), path)

    @staticmethod
    def _run_root(path: Path) -> Path | None:
        current = path
        for parent in (path, *path.parents):
            if (parent / "v3_run_config.json").is_file():
                return parent
            current = parent
        return None

    def context_for(self, record: Mapping[str, object]) -> ArtifactResolution:
        refs = record.get("artifact_refs")
        refs = refs if isinstance(refs, list) else []
        matched = [
            self._by_digest[str(item.get("sha256"))]
            for item in refs
            if isinstance(item, Mapping) and str(item.get("sha256")) in self._by_digest
        ]
        matched_hashes = tuple(sorted({_sha256(path) for path in matched}))
        patch = ""
        proposal: dict[str, object] = {}
        run_root: Path | None = None
        candidate_source = ""
        parent_source = ""
        token_envelope: dict[str, object] = {}
        proposal_round_index: int | None = None
        for path in matched:
            run_root = run_root or self._run_root(path)
            if path.name == "patch.diff":
                patch = path.read_text(encoding="utf-8", errors="replace")
                source_dir = path.parent / "source"
                sources = sorted(
                    item
                    for item in source_dir.glob("*.cpp")
                    if item.is_file() and _safe_public_path(item)
                )
                if sources:
                    candidate_source = sources[0].read_text(
                        encoding="utf-8", errors="replace"
                    )
            elif path.parent.name == "planner" and path.suffix == ".json":
                proposal = _read_json(path)
                round_match = re.fullmatch(r"proposal_(\d+)\.json", path.name)
                if round_match:
                    proposal_round_index = int(round_match.group(1))
                if not patch and isinstance(proposal.get("patch"), str):
                    patch = str(proposal["patch"])
            elif path.parent.name == "requests" and path.suffix == ".json":
                request_audit = _read_json(path)
                adapter_request = request_audit.get("request")
                raw_envelope = (
                    adapter_request.get("token_envelope")
                    if isinstance(adapter_request, Mapping)
                    else None
                )
                if isinstance(raw_envelope, Mapping):
                    token_envelope = dict(raw_envelope)
            elif path.parent.name == "live_outcomes" and path.suffix == ".json":
                live_outcome = _read_json(path)
                raw_proposal = live_outcome.get("proposal")
                if isinstance(raw_proposal, Mapping):
                    proposal = dict(raw_proposal)
        config: dict[str, object] = {}
        task_spec: dict[str, object] = {}
        if run_root is not None:
            config_path = run_root / "v3_run_config.json"
            task_path = run_root / "v3_task_spec.json"
            config = _read_json(config_path) if config_path.is_file() else {}
            task_spec = _read_json(task_path) if task_path.is_file() else {}
            baseline_dir = run_root / "baseline" / "source"
            baseline_sources = sorted(
                item
                for item in baseline_dir.glob("*.cpp")
                if item.is_file() and _safe_public_path(item)
            )
            if baseline_sources:
                parent_source = baseline_sources[0].read_text(
                    encoding="utf-8", errors="replace"
                )
            # Candidate v1 records normally bind the round proposal, not every
            # Planner journal artifact.  Resolve the corresponding action from
            # the terminal token table so new real runs carry their exact
            # TokenEnvelope into derived Experience v2 without expanding v1.
            if not token_envelope and proposal_round_index is not None:
                result_path = run_root / "v3_prototype_result.json"
                terminal = _read_json(result_path) if result_path.is_file() else {}
                token_rows = terminal.get("token_policy_rounds")
                token_rows = token_rows if isinstance(token_rows, list) else []
                token_row = next(
                    (
                        item
                        for item in token_rows
                        if isinstance(item, Mapping)
                        and item.get("round") == proposal_round_index
                        and isinstance(item.get("action_id"), str)
                    ),
                    None,
                )
                if isinstance(token_row, Mapping):
                    action_id = str(token_row["action_id"])
                    started_path = (
                        run_root
                        / "control"
                        / "live_planner_actions"
                        / f"{action_id}.started.json"
                    )
                    started = _read_json(started_path) if started_path.is_file() else {}
                    action_request = started.get("request")
                    request_ref = (
                        action_request.get("request_ref")
                        if isinstance(action_request, Mapping)
                        else None
                    )
                    request_path: Path | None = None
                    if (
                        isinstance(request_ref, str)
                        and request_ref
                        and not Path(request_ref).is_absolute()
                        and ".." not in Path(request_ref).parts
                    ):
                        candidate_request_path = (run_root / request_ref).resolve()
                        try:
                            candidate_request_path.relative_to(run_root.resolve())
                        except ValueError:
                            pass
                        else:
                            if _safe_public_path(candidate_request_path):
                                request_path = candidate_request_path
                    request_audit = (
                        _read_json(request_path)
                        if request_path is not None and request_path.is_file()
                        else {}
                    )
                    adapter_request = request_audit.get("request")
                    raw_envelope = (
                        adapter_request.get("token_envelope")
                        if isinstance(adapter_request, Mapping)
                        else None
                    )
                    if isinstance(raw_envelope, Mapping):
                        token_envelope = dict(raw_envelope)
                    outcome_path = (
                        run_root / "planner" / "live_outcomes" / f"{action_id}.json"
                    )
                    outcome = _read_json(outcome_path) if outcome_path.is_file() else {}
                    raw_proposal = outcome.get("proposal")
                    if isinstance(raw_proposal, Mapping):
                        proposal = dict(raw_proposal)
        tool = config.get("tool") if isinstance(config.get("tool"), Mapping) else {}
        token_policy = None
        if token_envelope:
            actual_input = (
                int(proposal["input_tokens"])
                if isinstance(proposal.get("input_tokens"), int)
                else None
            )
            actual_output = (
                int(proposal["output_tokens"])
                if isinstance(proposal.get("output_tokens"), int)
                else None
            )
            token_policy = {
                "run_token_limit": int(token_envelope.get("run_token_limit") or 0),
                "tokens_remaining_before_call": int(
                    token_envelope.get("tokens_remaining") or 0
                ),
                "estimated_base_prompt_tokens": int(
                    token_envelope.get("estimated_base_prompt_tokens") or 0
                ),
                "estimated_guidance_tokens": int(
                    token_envelope.get("estimated_guidance_tokens") or 0
                ),
                "estimated_input_tokens": int(
                    token_envelope.get("estimated_input_tokens") or 0
                ),
                "configured_max_output_tokens": int(
                    token_envelope.get("configured_max_output_tokens") or 0
                ),
                "effective_max_output_tokens": int(
                    token_envelope.get("effective_max_output_tokens") or 0
                ),
                "actual_input_tokens": actual_input,
                "actual_output_tokens": actual_output,
                "actual_total_tokens": (
                    actual_input + actual_output
                    if actual_input is not None and actual_output is not None
                    else None
                ),
                "context_window_tokens": int(
                    token_envelope.get("context_window_tokens") or 0
                ),
                "future_round_token_reserve": int(
                    token_envelope.get("future_round_token_reserve") or 0
                ),
                "guidance_token_cap": int(
                    token_envelope.get("guidance_token_cap") or 0
                ),
                "guidance_actual_tokens": int(
                    token_envelope.get("estimated_guidance_tokens") or 0
                ),
                "rounds_remaining": int(
                    token_envelope.get("rounds_remaining") or 0
                ),
                "token_pressure": str(
                    token_envelope.get("token_pressure") or "LOW"
                ),
                "finish_reason": proposal.get("finish_reason"),
                "output_truncated": bool(proposal.get("output_truncated", False)),
                "truncation_reason": proposal.get("truncation_reason"),
                "estimator_name": str(
                    token_envelope.get("estimator_name") or "UNKNOWN"
                ),
                "estimator_version": str(
                    token_envelope.get("estimator_version") or "UNKNOWN"
                ),
                "token_policy_version": str(
                    token_envelope.get("policy_version") or "v3.token-policy.v1"
                ),
            }
        return ArtifactResolution(
            MigrationContext(
                parent_source=parent_source,
                candidate_source=candidate_source,
                patch=patch,
                provider=str(proposal.get("provider") or "UNKNOWN"),
                model=str(proposal.get("model") or "UNKNOWN"),
                prompt_version=str(
                    config.get("live_planner_fingerprint")
                    or config.get("planner_mode")
                    or "UNKNOWN"
                )[:160],
                toolchain=str(tool.get("toolchain_id") or "Vitis 2025.2"),
                backend_fingerprint=str(
                    config.get("backend_fingerprint") or "UNKNOWN"
                )[:160],
                requires_cosim=bool(task_spec.get("requires_cosim"))
                if "requires_cosim" in task_spec
                else None,
                input_tokens=int(proposal["input_tokens"])
                if isinstance(proposal.get("input_tokens"), int)
                else None,
                output_tokens=int(proposal["output_tokens"])
                if isinstance(proposal.get("output_tokens"), int)
                else None,
                round_index=int(proposal["round_index"])
                if isinstance(proposal.get("round_index"), int)
                else None,
                token_policy=token_policy,
            ),
            matched_hashes,
        )


def derive_v2_backfill(
    v1_records: Sequence[Mapping[str, object]],
    *,
    resolver: PublicRunArtifactResolver | None = None,
) -> dict[str, object]:
    """Derive v2 records and an audit-only task grouping sidecar."""

    records: list[dict[str, object]] = []
    quarantine: list[dict[str, object]] = []
    audit_groups: dict[str, dict[str, str]] = {}
    matched_artifacts = 0
    for index, raw in enumerate(v1_records, start=1):
        try:
            v1 = validate_experience_record(raw)
            resolution = resolver.context_for(v1) if resolver else ArtifactResolution(MigrationContext(), ())
            v2 = migrate_v1_to_v2(v1, resolution.context)
            records.append(v2)
            matched_artifacts += int(bool(resolution.matched_artifact_hashes))
            audit_groups[str(v2["record_id"])] = {
                "task_audit_hash": str(v1["task_id_hash"]),
                "source_v1_record_id": str(v1["record_id"]),
            }
        except (OSError, UnicodeError, ValueError) as exc:
            quarantine.append(
                {
                    "line_number": index,
                    "source_digest": canonical_sha256(raw),
                    "reason": str(exc)[:160] or type(exc).__name__,
                }
            )
    records.sort(key=lambda item: str(item["record_id"]))
    return {
        "schema_version": BACKFILL_SCHEMA,
        "source_count": len(v1_records),
        "migrated_count": len(records),
        "quarantine_count": len(quarantine),
        "artifact_context_matches": matched_artifacts,
        "records": records,
        "quarantine": quarantine,
        "audit_groups": audit_groups,
    }


def _distribution(values: Sequence[object]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def experience_outcome(record: Mapping[str, object]) -> str:
    validation = record["validation"]
    performance = record["performance"]
    assert isinstance(validation, Mapping) and isinstance(performance, Mapping)
    if validation["fresh_final_status"] == "PASS":
        return "SUCCESS"
    if validation["synth_status"] == "PASS" and performance["strict_improvement"] is False:
        return "NO_IMPROVEMENT"
    return "FAILURE"


def build_data_quality(
    records: Sequence[Mapping[str, object]],
    *,
    source_count: int | None = None,
    quarantine_count: int = 0,
    artifact_context_matches: int = 0,
) -> dict[str, object]:
    validated = [validate_experience_v2(item) for item in records]
    declared_observed = []
    atoms: list[str] = []
    missing: Counter[str] = Counter()
    exclusions: list[str] = []
    for record in validated:
        source = record["source"]
        problem = record["problem"]
        strategy = record["strategy"]
        provenance = record["provenance"]
        declared = set(strategy["declared_strategy_bundle"])
        observed = set(strategy["observed_strategy_atoms"])
        declared_observed.append(declared == observed and bool(observed))
        atoms.extend(str(item) for item in observed)
        exclusions.extend(str(item) for item in provenance["exclusion_reasons"])
        for path, value in (
            ("source.provider", source["provider"]),
            ("source.model", source["model"]),
            ("source.prompt_version", source["prompt_version"]),
            ("problem.failure_subtype", problem["failure_subtype"]),
            ("problem.bottleneck_subtype", problem["bottleneck_subtype"]),
        ):
            if value in (None, "", "UNKNOWN"):
                missing[path] += 1
    other_count = sum(atom.startswith("OTHER_") for atom in atoms)
    report = {
        "schema_version": DATA_QUALITY_SCHEMA,
        "source_record_count": len(validated) if source_count is None else source_count,
        "migrated_record_count": len(validated),
        "quarantine_count": quarantine_count,
        "ranking_eligible_count": sum(
            record["provenance"]["eligible_for_ranking"] is True for record in validated
        ),
        "retrieval_eligible_count": sum(
            record["provenance"]["eligible_for_retrieval"] is True for record in validated
        ),
        "artifact_context_matches": artifact_context_matches,
        "declared_observed_exact_count": sum(declared_observed),
        "declared_observed_exact_rate": round(
            sum(declared_observed) / len(declared_observed), 8
        )
        if declared_observed
        else 0.0,
        "other_strategy_count": other_count,
        "other_strategy_rate": round(other_count / len(atoms), 8) if atoms else 0.0,
        "mode_distribution": _distribution([item["problem"]["mode"] for item in validated]),
        "failure_subtype_distribution": _distribution(
            [item["problem"]["failure_subtype"] for item in validated]
        ),
        "bottleneck_subtype_distribution": _distribution(
            [item["problem"]["bottleneck_subtype"] for item in validated]
        ),
        "strategy_atom_distribution": _distribution(atoms),
        "algorithm_family_distribution": _distribution(
            [item["source"]["algorithm_family"] for item in validated]
        ),
        "task_family_distribution": _distribution(
            [item["source"]["task_family_hash"] for item in validated]
        ),
        "outcome_distribution": _distribution([experience_outcome(item) for item in validated]),
        "provider_distribution": _distribution([item["source"]["provider"] for item in validated]),
        "toolchain_distribution": _distribution([item["source"]["toolchain"] for item in validated]),
        "backend_fingerprint_distribution": _distribution(
            [item["source"]["backend_fingerprint"] for item in validated]
        ),
        "missing_or_unknown_fields": dict(sorted(missing.items())),
        "exclusion_reasons": _distribution(exclusions),
    }
    return json.loads(json.dumps(report, sort_keys=True))


def render_data_quality_markdown(report: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# V3-E Experience v2 数据质量报告",
            "",
            f"- 原始记录：{report['source_record_count']}",
            f"- 成功迁移：{report['migrated_record_count']}",
            f"- 隔离记录：{report['quarantine_count']}",
            f"- 可排名真实 Candidate：{report['ranking_eligible_count']}",
            f"- 命中实际 Patch/运行 Artifact：{report['artifact_context_matches']}",
            f"- Planner 声明与 Patch 观测完全一致率：{float(report['declared_observed_exact_rate']):.2%}",
            f"- OTHER_* 策略比例：{float(report['other_strategy_rate']):.2%}",
            "",
            "## 分布",
            "",
            "```json",
            json.dumps(
                {
                    "mode": report["mode_distribution"],
                    "failure_subtype": report["failure_subtype_distribution"],
                    "bottleneck_subtype": report["bottleneck_subtype_distribution"],
                    "strategy": report["strategy_atom_distribution"],
                    "algorithm_family": report["algorithm_family_distribution"],
                    "outcome": report["outcome_distribution"],
                    "provider": report["provider_distribution"],
                    "toolchain": report["toolchain_distribution"],
                    "missing": report["missing_or_unknown_fields"],
                    "excluded": report["exclusion_reasons"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
            "本报告只描述相关性与数据质量，不把 Oracle、golden 或 fixture 记作 Agent 成功。",
            "",
        ]
    )


def build_coverage_matrix(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    validated = [
        validate_experience_v2(item)
        for item in records
        if item.get("provenance", {}).get("eligible_for_ranking") is True
        and item.get("source", {}).get("evidence_level") == "REAL_LLM_VITIS"
    ]
    rows: list[dict[str, object]] = []
    strategies: dict[str, dict[str, object]] = {}
    for record in validated:
        source = record["source"]
        problem = record["problem"]
        validation = record["validation"]
        strategy = record["strategy"]
        outcome = experience_outcome(record)
        subtype = (
            problem["bottleneck_subtype"]
            if problem["mode"] == "OPTIMIZE"
            else problem["failure_subtype"]
        )
        for atom in strategy["observed_strategy_atoms"]:
            key = f"{problem['mode']}::{subtype}::{atom}"
            item = strategies.setdefault(
                key,
                {
                    "mode": problem["mode"],
                    "subtype": subtype,
                    "strategy_atom": atom,
                    "total": 0,
                    "success": 0,
                    "failure": 0,
                    "no_improvement": 0,
                    "task_families": {},
                },
            )
            item["total"] += 1
            item[outcome.casefold()] += 1
            families = item["task_families"]
            families[source["task_family_hash"]] = families.get(source["task_family_hash"], 0) + 1
        rows.append(
            {
                "record_id": record["record_id"],
                "mode": problem["mode"],
                "subtype": subtype,
                "strategies": list(strategy["observed_strategy_atoms"]),
                "algorithm_family": source["algorithm_family"],
                "task_family_hash": source["task_family_hash"],
                "difficulty": source["difficulty"],
                "outcome": outcome,
                "provider": source["provider"],
                "toolchain": source["toolchain"],
                "prompt_version": source["prompt_version"],
                "cosim_status": validation["cosim_status"],
            }
        )
    for item in strategies.values():
        counts = list(item["task_families"].values())
        item["maximum_family_share"] = round(max(counts) / item["total"], 8) if counts else 0.0
        item["has_positive_and_negative"] = bool(
            item["success"] and (item["failure"] or item["no_improvement"])
        )
    mode_distribution = _distribution([row["mode"] for row in rows])
    gaps: list[dict[str, object]] = []
    for mode in sorted(MODES):
        count = mode_distribution.get(mode, 0)
        target = MODE_TARGETS[mode]
        if count < target:
            gaps.append(
                {
                    "dimension": "mode",
                    "key": mode,
                    "current": count,
                    "target": target,
                    "shortfall": target - count,
                }
            )
    subtype_distribution = _distribution([row["subtype"] for row in rows])
    for subtype, count in subtype_distribution.items():
        if subtype != "UNKNOWN" and count < 3:
            gaps.append(
                {
                    "dimension": "subtype",
                    "key": subtype,
                    "current": count,
                    "target": 3,
                    "shortfall": 3 - count,
                }
            )
    for key, item in sorted(strategies.items()):
        if not item["has_positive_and_negative"]:
            gaps.append(
                {
                    "dimension": "strategy_outcome_balance",
                    "key": key,
                    "current": {
                        "success": item["success"],
                        "failure": item["failure"],
                        "no_improvement": item["no_improvement"],
                    },
                    "target": "success_and_failure_or_no_improvement",
                    "shortfall": 1,
                }
            )
        if item["maximum_family_share"] > 0.5:
            gaps.append(
                {
                    "dimension": "strategy_family_concentration",
                    "key": key,
                    "current": item["maximum_family_share"],
                    "target": 0.5,
                    "shortfall": round(item["maximum_family_share"] - 0.5, 8),
                }
            )
    cosim = Counter(row["cosim_status"] for row in rows)
    report = {
        "schema_version": COVERAGE_SCHEMA,
        "ranking_eligible_count": len(rows),
        "targets": {
            "mode_minimum": MODE_TARGETS,
            "important_subtype_minimum": 3,
            "maximum_strategy_family_share": 0.5,
            "minimum_algorithm_families_for_cross_task_claim": 2,
        },
        "mode_distribution": mode_distribution,
        "subtype_distribution": subtype_distribution,
        "strategy_distribution": _distribution(
            [atom for row in rows for atom in row["strategies"]]
        ),
        "algorithm_family_distribution": _distribution(
            [row["algorithm_family"] for row in rows]
        ),
        "task_family_distribution": _distribution([row["task_family_hash"] for row in rows]),
        "difficulty_distribution": _distribution([row["difficulty"] for row in rows]),
        "outcome_distribution": _distribution([row["outcome"] for row in rows]),
        "provider_distribution": _distribution([row["provider"] for row in rows]),
        "toolchain_distribution": _distribution([row["toolchain"] for row in rows]),
        "prompt_version_distribution": _distribution([row["prompt_version"] for row in rows]),
        "cosim_distribution": dict(sorted(cosim.items())),
        "cosim_risk_has_pass_and_fail": bool(cosim["PASS"] and (cosim["FAIL"] or cosim["TIMEOUT"])),
        "strategy_cells": [strategies[key] for key in sorted(strategies)],
        "rows": sorted(rows, key=lambda item: str(item["record_id"])),
        "gaps": sorted(
            gaps,
            key=lambda item: (-float(item["shortfall"]) if isinstance(item["shortfall"], (int, float)) else 0.0, str(item["dimension"]), str(item["key"])),
        ),
    }
    return json.loads(json.dumps(report, sort_keys=True))


def coverage_csv(report: Mapping[str, object]) -> str:
    fields = [
        "record_id",
        "mode",
        "subtype",
        "strategies",
        "algorithm_family",
        "task_family_hash",
        "difficulty",
        "outcome",
        "provider",
        "toolchain",
        "prompt_version",
        "cosim_status",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for raw in report.get("rows", []):
        row = dict(raw)
        row["strategies"] = "+".join(row["strategies"])
        writer.writerow({field: row.get(field) for field in fields})
    return output.getvalue()


def render_coverage_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# V3-E Experience 覆盖矩阵",
        "",
        f"当前可排名真实 Candidate：{report['ranking_eligible_count']}。",
        "",
        "## Mode 覆盖",
        "",
        "| Mode | 当前 | 目标 | 缺口 |",
        "|---|---:|---:|---:|",
    ]
    for mode, target in MODE_TARGETS.items():
        current = int(report["mode_distribution"].get(mode, 0))
        lines.append(f"| {mode} | {current} | {target} | {max(0, target-current)} |")
    lines.extend(
        [
            "",
            f"算法族数量：{len(report['algorithm_family_distribution'])}；覆盖缺口总数：{len(report['gaps'])}。",
            "",
            "Quality Gate 只有在跨 run、跨 task family 的可信支持满足阈值时才允许注入；本矩阵不等同于模型已具备泛化能力。",
            "",
        ]
    )
    return "\n".join(lines)


_FAMILY_TO_ALGORITHM = {
    "map": "VECTOR_ELEMENTWISE",
    "coordinate_projection": "COORDINATE_TRANSFORM",
    "prefix_sum": "PREFIX_SUM",
    "histogram": "HISTOGRAM",
    "vector_add": "VECTOR_ELEMENTWISE",
    "fir": "FIR_CONVOLUTION",
    "matrix_multiplication": "MATRIX_MULTIPLICATION",
    "stencil": "STENCIL",
    "dot_product": "DOT_PRODUCT",
    "reduction": "REDUCTION",
    "dataflow": "STREAM_PIPELINE",
}


def _algorithm_from_family(family: str) -> str:
    folded = family.casefold()
    for token, algorithm in _FAMILY_TO_ALGORITHM.items():
        if token in folded:
            return algorithm
    if any(token in folded for token in ("stream", "fifo", "producer", "consumer", "dependency_cycle")):
        return "STREAM_PIPELINE"
    return "OTHER"


def _subtype_hint(mode: str, family: str) -> str:
    """Collection-only public metadata hint; never a Planner feature."""

    text = family.casefold()
    mappings = {
        "REPAIR": (
            ("off_by_one", "OFF_BY_ONE"),
            ("wrong_index", "WRONG_ARRAY_INDEX"),
            ("omitted", "OMITTED_TERM"),
            ("sign", "WRONG_SIGN"),
            ("multiplier", "WRONG_COEFFICIENT"),
        ),
        "SYNTH_FIX": (
            ("dynamic", "DYNAMIC_ALLOCATION"),
            ("stl", "UNSUPPORTED_STL"),
            ("recurs", "RECURSION"),
            ("loop_bound", "NON_STATIC_LOOP_BOUND"),
        ),
        "STRUCTURAL_FIX": (
            ("stream_count", "STREAM_COUNT_MISMATCH"),
            ("fifo", "FIFO_DEPTH_INSUFFICIENT"),
            ("dependency_cycle", "DATAFLOW_DEPENDENCY_CYCLE"),
            ("rate_mismatch", "PRODUCER_CONSUMER_RATE_MISMATCH"),
            ("producer_burst", "PRODUCER_BURST_DEADLOCK"),
            ("stream_order", "STREAM_ORDER_MISMATCH"),
        ),
        "OPTIMIZE": (
            ("reduction", "SERIAL_REDUCTION"),
            ("memory_port", "MEMORY_PORT_LIMIT"),
            ("transaction", "HIGH_TRANSACTION_LATENCY_WITH_II_ONE"),
            ("dataflow", "SEQUENTIAL_LOAD_COMPUTE_STORE"),
        ),
    }
    for token, subtype in mappings.get(mode, ()):
        if token in text:
            return subtype
    return "UNKNOWN"


def load_public_corpus(corpus_manifest: str | Path) -> list[dict[str, object]]:
    path = Path(corpus_manifest)
    if not _safe_public_path(path):
        raise ValueError("collection corpus must be public train/dev")
    value = _read_json(path)
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("public corpus manifest has no task list")
    inventory: list[dict[str, object]] = []
    for index, raw in enumerate(tasks):
        if not isinstance(raw, Mapping):
            continue
        relative = Path(str(raw.get("path") or ""))
        task_path = (path.parent / relative).resolve()
        if not task_path.is_relative_to(path.parent.resolve()) or not _safe_public_path(task_path):
            raise ValueError("corpus task path escapes public corpus")
        mode = str(raw.get("mode") or "")
        if mode not in MODES:
            continue
        family = str(raw.get("family") or "UNKNOWN")
        inventory.append(
            {
                "task": str(raw.get("task_id") or relative.name),
                "relative_path": relative.as_posix(),
                "mode": mode,
                "subtype": _subtype_hint(mode, family),
                "algorithm_family": _algorithm_from_family(family),
                "public_family": family,
                "difficulty": int(raw.get("difficulty") or 1),
                "task_split": "dev" if (index + 1) % 5 == 0 else "train",
                "requires_cosim": mode == "STRUCTURAL_FIX",
            }
        )
    return inventory


def plan_gap_driven_collection(
    coverage: Mapping[str, object],
    corpus: Sequence[Mapping[str, object]],
    *,
    available_credit: int = 2400,
    provider_available: bool = True,
    vitis_available: bool = True,
) -> dict[str, object]:
    mode_counts = Counter(
        {key: int(value) for key, value in coverage.get("mode_distribution", {}).items()}
    )
    subtype_counts = Counter(
        {key: int(value) for key, value in coverage.get("subtype_distribution", {}).items()}
    )
    algorithms = set(coverage.get("algorithm_family_distribution", {}))
    outcomes = Counter(coverage.get("outcome_distribution", {}))
    mode_shortfalls = {
        mode: max(0, target - mode_counts[mode])
        for mode, target in MODE_TARGETS.items()
    }
    needed_total = max(
        0,
        60 - int(coverage.get("ranking_eligible_count", 0)),
        sum(mode_shortfalls.values()),
    )
    family_budget = max(1, math.ceil(max(needed_total, 1) * 0.5))
    candidates: list[tuple[float, dict[str, object]]] = []
    for raw in corpus:
        task = dict(raw)
        if task.get("task_split") not in {"train", "dev"}:
            continue
        mode = str(task["mode"])
        subtype = str(task["subtype"])
        mode_gap = max(0, MODE_TARGETS[mode] - mode_counts[mode])
        subtype_gap = max(0, 3 - subtype_counts[subtype]) if subtype != "UNKNOWN" else 1
        new_algorithm = task["algorithm_family"] not in algorithms
        needs_negative = outcomes["FAILURE"] + outcomes["NO_IMPROVEMENT"] < outcomes["SUCCESS"]
        priority = (
            10.0 * mode_gap
            + 4.0 * subtype_gap
            + (8.0 if new_algorithm else 0.0)
            + (3.0 if needs_negative else 0.0)
            + float(task["difficulty"]) / 10.0
        )
        candidates.append((priority, task))
    candidates.sort(key=lambda item: (-item[0], str(item[1]["task"])))
    family_attempts: Counter[str] = Counter()
    remaining_credit = max(0, available_credit)
    queue: list[dict[str, object]] = []
    planned_candidates = 0
    planned_by_mode: Counter[str] = Counter()
    for priority, task in candidates:
        if not provider_available or not vitis_available:
            attempts = 0
        else:
            mode = str(task["mode"])
            shortfall = max(0, mode_shortfalls[mode] - planned_by_mode[mode])
            attempts = min(3, shortfall)
            attempts = min(attempts, max(0, family_budget - family_attempts[str(task["public_family"])]))
        credit_each = 60 if task["requires_cosim"] else 40
        attempts = min(attempts, remaining_credit // credit_each)
        if attempts == 0 and provider_available and vitis_available:
            continue
        needed_examples = ["POSITIVE"]
        if coverage.get("outcome_distribution", {}).get("FAILURE", 0) < coverage.get("outcome_distribution", {}).get("SUCCESS", 0):
            needed_examples.append("NEGATIVE_OR_NO_IMPROVEMENT_NATURALLY_OBSERVED")
        item = {
            "queue_id": "",
            "task": task["task"],
            "relative_path": task["relative_path"],
            "task_split": task["task_split"],
            "mode": task["mode"],
            "subtype": task["subtype"],
            "algorithm_family": task["algorithm_family"],
            "public_family": task["public_family"],
            "difficulty": task["difficulty"],
            "current_gap": {
                "mode_shortfall": max(
                    0,
                    MODE_TARGETS[str(task["mode"])]
                    - mode_counts[str(task["mode"])],
                ),
                "subtype_shortfall": max(0, 3 - subtype_counts[str(task["subtype"])]),
                "new_algorithm_family": task["algorithm_family"] not in algorithms,
            },
            "needed_examples": needed_examples,
            "recommended_attempts": attempts,
            "requires_cosim": task["requires_cosim"],
            "estimated_cost": {
                "tokens": attempts * 3500,
                "credits": attempts * credit_each,
                "wall_time_seconds": attempts * (900 if task["requires_cosim"] else 600),
            },
            "priority": round(priority, 4),
            "reason": "MODE_SUBTYPE_FAMILY_GAP; metadata hint is queue-only and never Planner input",
            "status": "PENDING" if attempts else "BLOCKED_EXTERNAL_AVAILABILITY",
            "attempts_completed": 0,
        }
        item["queue_id"] = canonical_sha256({**item, "queue_id": ""})
        queue.append(item)
        planned_candidates += attempts
        planned_by_mode[str(task["mode"])] += attempts
        family_attempts[str(task["public_family"])] += attempts
        remaining_credit -= attempts * credit_each
        if planned_candidates >= needed_total and all(
            planned_by_mode[mode] >= mode_shortfalls[mode] for mode in MODE_TARGETS
        ):
            break
    output = {
        "schema_version": COLLECTION_QUEUE_SCHEMA,
        "coverage_schema": coverage.get("schema_version"),
        "provider_available": provider_available,
        "vitis_available": vitis_available,
        "available_credit": available_credit,
        "minimum_total_target": 60,
        "planned_candidate_count": planned_candidates,
        "planned_by_mode": dict(sorted(planned_by_mode.items())),
        "remaining_credit": remaining_credit,
        "family_attempt_quota": family_budget,
        "hidden_like_external_collection_allowed": False,
        "guidance_mode": "off_or_shadow",
        "items": queue,
    }
    output["queue_sha256"] = canonical_sha256(output)
    return json.loads(json.dumps(output, sort_keys=True))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return canonical_json(value) + b"\n"


def write_analysis_artifacts(
    *,
    v1_store: str | Path,
    output_root: str | Path,
    public_run_roots: Sequence[str | Path] = (),
    corpus_manifest: str | Path | None = None,
    provider_available: bool = True,
    vitis_available: bool = True,
    available_credit: int = 2400,
) -> dict[str, object]:
    """Build a deterministic v2 snapshot and all quality/coverage artifacts."""

    output = Path(output_root)
    repository = JsonlExperienceRepository(v1_store)
    v1_snapshot = repository.snapshot()
    v1_records = repository.records(v1_snapshot)
    resolver = PublicRunArtifactResolver(public_run_roots) if public_run_roots else None
    backfill = derive_v2_backfill(v1_records, resolver=resolver)
    records = backfill["records"]
    assert isinstance(records, list)

    backfill_path = output / "experience_v2_backfill.jsonl"
    _atomic_write(
        backfill_path,
        b"".join(canonical_json(record) + b"\n" for record in records),
    )
    quarantine_path = output / "experience_v2_quarantine.jsonl"
    _atomic_write(
        quarantine_path,
        b"".join(canonical_json(item) + b"\n" for item in backfill["quarantine"]),
    )
    _atomic_write(
        output / "experience_v2_audit_groups.json",
        _json_bytes(
            {
                "schema_version": "v3e.experience-audit-groups.v1",
                "groups": backfill["audit_groups"],
                "retrieval_feature": False,
            }
        ),
    )

    knowledge_base = ExperienceKnowledgeBase(output / "experience")
    for record in records:
        knowledge_base.put_if_absent(record)
    snapshot = knowledge_base.snapshot(persist=True)
    quality = build_data_quality(
        records,
        source_count=int(backfill["source_count"]),
        quarantine_count=int(backfill["quarantine_count"]),
        artifact_context_matches=int(backfill["artifact_context_matches"]),
    )
    stats = {
        "schema_version": "v3e.experience-v2-stats.v1",
        "v1_snapshot": v1_snapshot.to_dict(),
        "v2_snapshot": snapshot.to_dict(),
        "source_count": backfill["source_count"],
        "migrated_count": backfill["migrated_count"],
        "quarantine_count": backfill["quarantine_count"],
        "v2_store_record_count": snapshot.record_count,
        "ranking_eligible_count": quality["ranking_eligible_count"],
        "retrieval_eligible_count": quality["retrieval_eligible_count"],
        "mode_distribution": quality["mode_distribution"],
        "outcome_distribution": quality["outcome_distribution"],
        "backfill_sha256": _sha256(backfill_path),
    }
    _atomic_write(output / "experience_v2_stats.json", _json_bytes(stats))
    _atomic_write(output / "experience_v2_data_quality.json", _json_bytes(quality))
    _atomic_write(
        output / "experience_v2_quality_report.md",
        render_data_quality_markdown(quality).encode("utf-8"),
    )

    coverage = build_coverage_matrix(records)
    _atomic_write(output / "experience_coverage_matrix.json", _json_bytes(coverage))
    _atomic_write(
        output / "experience_coverage_matrix.csv", coverage_csv(coverage).encode("utf-8")
    )
    _atomic_write(
        output / "experience_coverage_report.md",
        render_coverage_markdown(coverage).encode("utf-8"),
    )
    queue: dict[str, object] = {
        "schema_version": COLLECTION_QUEUE_SCHEMA,
        "items": [],
        "blocked_reason": "PUBLIC_CORPUS_NOT_CONFIGURED",
    }
    if corpus_manifest is not None:
        queue = plan_gap_driven_collection(
            coverage,
            load_public_corpus(corpus_manifest),
            available_credit=available_credit,
            provider_available=provider_available,
            vitis_available=vitis_available,
        )
    _atomic_write(output / "experience_collection_queue.json", _json_bytes(queue))
    manifest = {
        "schema_version": "v3e.experience-analysis-artifacts.v1",
        "v2_snapshot": snapshot.to_dict(),
        "files": {
            path.name: _sha256(path)
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "artifact_manifest.json"
        },
    }
    _atomic_write(output / "artifact_manifest.json", _json_bytes(manifest))
    return {
        "output_root": output.as_posix(),
        "stats": stats,
        "quality": quality,
        "coverage": coverage,
        "collection_queue": queue,
        "artifact_manifest": manifest,
    }


__all__ = [
    "PublicRunArtifactResolver",
    "build_coverage_matrix",
    "build_data_quality",
    "coverage_csv",
    "derive_v2_backfill",
    "experience_outcome",
    "load_public_corpus",
    "plan_gap_driven_collection",
    "render_coverage_markdown",
    "render_data_quality_markdown",
    "write_analysis_artifacts",
]
