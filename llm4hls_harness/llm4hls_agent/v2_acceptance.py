"""Fail-closed V2 acceptance over Manifest-covered immutable evidence."""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Mapping

from .artifacts import ArtifactManifestError, manifest_digest, verify_artifact_manifest
from .scoring import CandidateScore, ScoringConfig, compare_scores, score_candidate


EVALUATOR_VERSION = "v2.0"


class V2AcceptanceError(RuntimeError):
    """Raised when the evaluator invocation or specification is invalid."""


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2AcceptanceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise V2AcceptanceError(f"{path} is not a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        values = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2AcceptanceError(f"cannot read {path}: {exc}") from exc
    if not all(isinstance(value, dict) for value in values):
        raise V2AcceptanceError(f"{path} contains a non-object record")
    return values


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_paths(manifest: Mapping[str, object]) -> set[str]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise V2AcceptanceError("Manifest artifacts are invalid")
    return {
        str(item["path"])
        for item in artifacts
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }


def _covered_path(root: Path, paths: set[str], reference: object) -> Path:
    if not isinstance(reference, str) or reference not in paths:
        raise V2AcceptanceError(f"evidence reference is not Manifest-covered: {reference}")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or "\\" in reference:
        raise V2AcceptanceError("evidence reference escapes its run directory")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise V2AcceptanceError("evidence path escapes its run directory") from exc
    return path


def _covered_json(
    root: Path, paths: set[str], reference: object
) -> dict[str, object]:
    return _read_json(_covered_path(root, paths, reference))


def _scoring_config(snapshot: Mapping[str, object]) -> ScoringConfig:
    weights = snapshot.get("ppa_weights")
    caps = snapshot.get("max_resource_percent")
    official = snapshot.get("official_score")
    if not isinstance(weights, Mapping) or not isinstance(caps, Mapping):
        raise V2AcceptanceError("scoring configuration is invalid")
    if not isinstance(official, Mapping):
        raise V2AcceptanceError("official score configuration is invalid")
    return ScoringConfig(
        weights={str(key): float(value) for key, value in weights.items()},
        max_resource_percent={str(key): float(value) for key, value in caps.items()},
        required_verification_tier=int(snapshot["required_verification_tier"]),
        official_score_enabled=official.get("enabled") is True,
    )


def _report_from_action(
    root: Path, paths: set[str], reference: object
) -> dict[str, object]:
    action = _covered_json(root, paths, reference)
    report = action.get("report")
    if not isinstance(report, dict):
        raise V2AcceptanceError("synthesis action has no structured report")
    return report


def _candidate_tree(
    root: Path,
    paths: set[str],
    registry: Mapping[str, object],
) -> tuple[bool, dict[str, Mapping[str, object]]]:
    raw = registry.get("candidates")
    if not isinstance(raw, Mapping) or "candidate_000" not in raw:
        return False, {}
    candidates = {
        str(key): value for key, value in raw.items() if isinstance(value, Mapping)
    }
    if len(candidates) != len(raw):
        return False, candidates
    for candidate_id, candidate in candidates.items():
        parent = candidate.get("parent_id")
        if candidate_id == "candidate_000":
            if parent is not None:
                return False, candidates
        elif not isinstance(parent, str) or parent not in candidates:
            return False, candidates
        source_ref = candidate.get("source_ref")
        try:
            source = _covered_path(root, paths, source_ref).read_bytes()
        except (V2AcceptanceError, OSError):
            return False, candidates
        if _sha256(source) != candidate.get("code_hash"):
            return False, candidates
        if candidate_id != "candidate_000":
            patch_ref = candidate.get("patch_ref")
            try:
                patch = _covered_path(root, paths, patch_ref).read_bytes()
            except (V2AcceptanceError, OSError):
                return False, candidates
            if _sha256(patch) != candidate.get("patch_sha256"):
                return False, candidates
        visited: set[str] = set()
        current = candidate_id
        while current != "candidate_000":
            if current in visited:
                return False, candidates
            visited.add(current)
            parent_value = candidates[current].get("parent_id")
            if not isinstance(parent_value, str) or parent_value not in candidates:
                return False, candidates
            current = parent_value
    return True, candidates


def _public_inputs_bound(
    root: Path,
    paths: set[str],
    candidates: Mapping[str, Mapping[str, object]],
) -> bool:
    task_spec = _covered_json(root, paths, "task_spec.json")
    hashes = task_spec.get("public_file_hashes")
    kernel_name = task_spec.get("kernel_file")
    if not isinstance(hashes, Mapping) or not isinstance(kernel_name, str):
        return False
    baseline = candidates.get("candidate_000")
    if not isinstance(baseline, Mapping):
        return False
    try:
        baseline_source = _covered_path(
            root, paths, baseline.get("source_ref")
        ).read_bytes()
    except (V2AcceptanceError, OSError):
        return False
    baseline_hash = _sha256(baseline_source)
    if hashes.get(kernel_name) != baseline_hash or baseline.get("code_hash") != baseline_hash:
        return False
    expected_old = f"--- a/{kernel_name}"
    expected_new = f"+++ b/{kernel_name}"
    for candidate_id, candidate in candidates.items():
        if candidate_id == "candidate_000":
            continue
        try:
            patch = _covered_path(
                root, paths, candidate.get("patch_ref")
            ).read_text(encoding="utf-8")
        except (V2AcceptanceError, OSError, UnicodeDecodeError):
            return False
        headers = [
            line for line in patch.splitlines() if line.startswith(("--- ", "+++ "))
        ]
        if headers != [expected_old, expected_new]:
            return False
    return True


def _score_evidence(
    root: Path,
    paths: set[str],
    result: Mapping[str, object],
    registry: Mapping[str, object],
    candidates: Mapping[str, Mapping[str, object]],
    scoring: ScoringConfig,
) -> tuple[bool, bool]:
    workflow = _covered_json(root, paths, "workflow_result.json")
    baseline = candidates["candidate_000"]
    baseline_metrics = _report_from_action(root, paths, baseline.get("metrics_ref"))
    scores: dict[str, CandidateScore] = {}
    for candidate_id, candidate in candidates.items():
        score_ref = candidate.get("score_ref")
        if not isinstance(score_ref, str):
            continue
        stored = _covered_json(root, paths, score_ref)
        if candidate_id == "candidate_000":
            validation = workflow.get("validation")
            clock = workflow.get("clock_constraint")
            input_tokens = output_tokens = cached_tokens = 0
            credits = int(workflow.get("budget", {}).get("credits_used", 0)) if isinstance(workflow.get("budget"), Mapping) else 0
        else:
            validation = candidate.get("validation")
            clock = candidate.get("clock_constraint")
            input_tokens = int(candidate.get("input_tokens", 0))
            output_tokens = int(candidate.get("output_tokens", 0))
            cached_tokens = int(candidate.get("cached_input_tokens", 0))
            credits = int(candidate.get("credits_used", 0))
        if not isinstance(validation, Mapping) or not isinstance(clock, Mapping):
            return False, False
        metrics = _report_from_action(root, paths, candidate.get("metrics_ref"))
        recomputed = score_candidate(
            candidate_id=candidate_id,
            baseline=baseline_metrics,
            candidate=metrics,
            validation=validation,
            clock=clock,
            config=scoring,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            credits_used=credits,
        )
        if recomputed.to_dict() != stored:
            return False, False
        scores[candidate_id] = recomputed

    if "candidate_000" not in scores:
        return False, False
    current_best = "candidate_000"
    rounds = result.get("rounds")
    if not isinstance(rounds, list):
        return False, False
    for expected_index, raw_round in enumerate(rounds, start=1):
        if not isinstance(raw_round, Mapping):
            return False, False
        round_record = _covered_json(root, paths, raw_round.get("result_ref"))
        if round_record.get("round_index") != expected_index:
            return False, False
        if round_record.get("parent_candidate_id") != current_best:
            return False, False
        candidate_id = round_record.get("candidate_id")
        decision = round_record.get("decision")
        if not isinstance(candidate_id, str) or candidate_id not in scores:
            if decision == "PROMOTED":
                return False, False
            continue
        comparison = compare_scores(scores[candidate_id], scores[current_best])
        comparison_ref = round_record.get("comparison_ref")
        if not isinstance(comparison_ref, str):
            return False, False
        normalized_comparison = json.loads(json.dumps(comparison.to_dict()))
        if normalized_comparison != _covered_json(root, paths, comparison_ref):
            return False, False
        if comparison.strictly_better:
            if decision != "PROMOTED":
                return False, False
            current_best = candidate_id
        elif decision != "REJECTED_NOT_BETTER":
            return False, False
    recorded_best = result.get("best_candidate_id")
    best_valid = (
        current_best == recorded_best == registry.get("best_candidate_id")
    )
    return True, best_valid


def _ledger_accounting(
    root: Path,
    paths: set[str],
    result: Mapping[str, object],
) -> tuple[bool, dict[str, object]]:
    events = _read_jsonl(_covered_path(root, paths, "budget_ledger.jsonl"))
    trace = _read_jsonl(_covered_path(root, paths, "trace.jsonl"))
    if not events or events[0].get("state") != "INITIALIZED":
        return False, {}
    if any(event.get("sequence") != index for index, event in enumerate(events)):
        return False, {}
    config = events[0].get("config")
    if not isinstance(config, Mapping) or not isinstance(config.get("costs"), Mapping):
        return False, {}
    costs = config["costs"]
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for event in events[1:]:
        action_id = event.get("action_id")
        if isinstance(action_id, str):
            grouped[action_id].append(event)
    calls = {str(kind): 0 for kind in costs}
    credits = input_tokens = output_tokens = cached_tokens = tokens = 0
    completed_ids: set[str] = set()
    for action_id, action_events in grouped.items():
        started = next((item for item in action_events if item.get("state") == "STARTED"), None)
        terminal = action_events[-1]
        if started is None or terminal.get("state") != "COMPLETED":
            return False, {}
        kind = str(started.get("kind"))
        if kind not in costs or int(terminal.get("actual_cost", -1)) != int(costs[kind]):
            return False, {}
        try:
            encoded = _covered_path(root, paths, terminal.get("result_ref")).read_bytes()
        except (V2AcceptanceError, OSError):
            return False, {}
        if terminal.get("result_sha256") != _sha256(encoded):
            return False, {}
        completed_ids.add(action_id)
        calls[kind] += 1
        credits += int(terminal.get("actual_cost", 0))
        tokens += int(terminal.get("tokens_used", 0))
        input_tokens += int(terminal.get("input_tokens", 0))
        output_tokens += int(terminal.get("output_tokens", 0))
        cached_tokens += int(terminal.get("cached_input_tokens", 0))
    trace_ids = {
        str(item["action_id"])
        for item in trace
        if item.get("event") in {"TOOL_COMPLETED", "V2_LLM_COMPLETED"}
        and isinstance(item.get("action_id"), str)
    }
    summary = {
        "credits_used": credits,
        "tokens_used": tokens,
        "input_tokens_used": input_tokens,
        "output_tokens_used": output_tokens,
        "cached_input_tokens_used": cached_tokens,
        "tool_used": calls,
        "credit_limit": config.get("credit_limit"),
        "credits_remaining": (
            int(config["credit_limit"]) - credits
            if config.get("credit_limit") is not None
            else None
        ),
    }
    budget = result.get("budget")
    budget_value = budget if isinstance(budget, Mapping) else {}
    state = _covered_json(root, paths, "budget_state.json")
    matches = all(budget_value.get(key) == summary[key] for key in (
        "credits_used",
        "tokens_used",
        "input_tokens_used",
        "output_tokens_used",
        "cached_input_tokens_used",
        "tool_used",
    )) and all(state.get(key) == summary[key] for key in (
        "credits_used",
        "tokens_used",
        "input_tokens_used",
        "output_tokens_used",
        "cached_input_tokens_used",
        "tool_used",
    ))
    return matches and completed_ids.issubset(trace_ids), summary


def _backend_and_toolchain(
    root: Path,
    paths: set[str],
    spec: Mapping[str, object],
) -> tuple[bool, bool]:
    run_config = _covered_json(root, paths, "run_config.json")
    tool = run_config.get("tool")
    tool_value = tool if isinstance(tool, Mapping) else {}
    toolchain_ok = tool_value.get("toolchain_id") == spec.get("required_toolchain")
    backends = []
    for relative in sorted(path for path in paths if path.startswith("actions/") and path.endswith("/result.json")):
        action = _covered_json(root, paths, relative)
        backends.append(action.get("backend_fingerprint"))
    return toolchain_ok, bool(backends) and all(
        value == spec.get("required_backend_fingerprint") for value in backends
    )


def _real_llm_candidate_count(
    root: Path,
    paths: set[str],
    candidates: Mapping[str, Mapping[str, object]],
    spec: Mapping[str, object],
) -> int:
    count = 0
    for candidate_id, candidate in candidates.items():
        if candidate_id == "candidate_000" or candidate.get("kind") != "optimization":
            continue
        if (
            candidate.get("provider") != spec.get("required_provider")
            or candidate.get("model") != spec.get("required_model")
        ):
            continue
        try:
            action = _covered_json(root, paths, candidate.get("llm_ref"))
        except V2AcceptanceError:
            continue
        if (
            action.get("ok") is True
            and int(action.get("input_tokens", 0)) > 0
            and int(action.get("output_tokens", 0)) > 0
        ):
            count += 1
    return count


def _final_validation(result: Mapping[str, object]) -> bool:
    validation = result.get("final_validation")
    if not isinstance(validation, Mapping):
        return False
    return (
        result.get("status") == "DONE"
        and isinstance(result.get("final_candidate_id"), str)
        and all(
            isinstance(validation.get(stage), Mapping)
            and validation[stage].get("status") == "PASS"
            and validation[stage].get("validation_scope") == "final"
            for stage in ("csim", "synth", "cosim")
        )
        and isinstance(result.get("final_clock_constraint"), Mapping)
        and result["final_clock_constraint"].get("passed") is True
    )


def _rejection_checks(
    result: Mapping[str, object], registry: Mapping[str, object]
) -> bool:
    candidates = registry.get("candidates")
    candidate_map = candidates if isinstance(candidates, Mapping) else {}
    rejected_id = result.get("rejected_candidate_id")
    rejected = candidate_map.get(rejected_id)
    value = rejected if isinstance(rejected, Mapping) else {}
    validation = value.get("validation")
    stages = validation if isinstance(validation, Mapping) else {}
    invariants = result.get("safety_invariants")
    invariant_value = invariants if isinstance(invariants, Mapping) else {}
    budget = result.get("budget")
    budget_value = budget if isinstance(budget, Mapping) else {}
    calls = budget_value.get("tool_used")
    call_value = calls if isinstance(calls, Mapping) else {}
    return (
        result.get("status") == "DONE"
        and result.get("stop_reason") == "SAFETY_REGRESSION_REJECTED"
        and bool(invariant_value)
        and all(item is True for item in invariant_value.values())
        and value.get("kind") == "safety_regression"
        and value.get("status") == "REJECTED_VALIDATION"
        and isinstance(stages.get("csim"), Mapping)
        and stages["csim"].get("status") == "FAIL"
        and isinstance(stages.get("synth"), Mapping)
        and stages["synth"].get("status") == "NOT_RUN"
        and isinstance(stages.get("cosim"), Mapping)
        and stages["cosim"].get("status") == "NOT_RUN"
        and registry.get("best_candidate_id") == "candidate_000"
        and registry.get("final_candidate_id") == "candidate_000"
        and registry.get("active_candidate_id") == "candidate_000"
        and int(call_value.get("llm", -1)) == 0
    )


def compute_v2_acceptance(
    spec_path: str | Path,
    optimization_run: str | Path,
    rejection_run: str | Path,
) -> dict[str, object]:
    """Compute V2 acceptance without writing or mutating any run evidence."""

    spec = _read_json(Path(spec_path).resolve())
    if spec.get("schema_version") != 1 or spec.get("evidence_tier") not in {"REAL", "TEST"}:
        raise V2AcceptanceError("invalid V2 acceptance specification")
    opt_root = Path(optimization_run).resolve()
    reject_root = Path(rejection_run).resolve()
    reasons: list[str] = []
    try:
        opt_manifest = verify_artifact_manifest(opt_root)
        reject_manifest = verify_artifact_manifest(reject_root)
        opt_paths = _manifest_paths(opt_manifest)
        reject_paths = _manifest_paths(reject_manifest)
    except (ArtifactManifestError, V2AcceptanceError):
        reasons.append("MANIFEST_INVALID")
        return {
            "schema_version": 1,
            "evaluator_version": EVALUATOR_VERSION,
            "acceptance_id": spec.get("acceptance_id"),
            "evidence_tier": spec.get("evidence_tier"),
            "overall_status": "FAIL",
            "checks": {"manifest_valid": False},
            "reason_codes": reasons,
        }

    try:
        opt_result = _covered_json(opt_root, opt_paths, "v2_result.json")
        reject_result = _covered_json(
            reject_root, reject_paths, "v2_rejection_result.json"
        )
        opt_registry = _covered_json(opt_root, opt_paths, "candidate_registry.json")
        reject_registry = _covered_json(
            reject_root, reject_paths, "candidate_registry.json"
        )
        optimization_snapshot = _covered_json(
            opt_root, opt_paths, "optimization_config.json"
        )
        scoring_snapshot = optimization_snapshot.get("scoring")
        if not isinstance(scoring_snapshot, Mapping):
            raise V2AcceptanceError("V2 scoring snapshot is missing")
        scoring = _scoring_config(scoring_snapshot)
        tree_ok, candidates = _candidate_tree(opt_root, opt_paths, opt_registry)
        rejection_tree_ok, rejection_candidates = _candidate_tree(
            reject_root, reject_paths, reject_registry
        )
        public_inputs_ok = _public_inputs_bound(
            opt_root, opt_paths, candidates
        ) and _public_inputs_bound(
            reject_root, reject_paths, rejection_candidates
        )
        scores_ok, best_ok = _score_evidence(
            opt_root,
            opt_paths,
            opt_result,
            opt_registry,
            candidates,
            scoring,
        ) if tree_ok else (False, False)
        opt_accounting, opt_summary = _ledger_accounting(
            opt_root, opt_paths, opt_result
        )
        rejection_accounting, rejection_summary = _ledger_accounting(
            reject_root, reject_paths, reject_result
        )
        opt_toolchain, opt_backend = _backend_and_toolchain(
            opt_root, opt_paths, spec
        )
        reject_toolchain, reject_backend = _backend_and_toolchain(
            reject_root, reject_paths, spec
        )
        llm_count = _real_llm_candidate_count(
            opt_root, opt_paths, candidates, spec
        )
    except (V2AcceptanceError, ValueError, TypeError, KeyError, OSError):
        reasons.append("EVIDENCE_INVALID")
        checks = {"manifest_valid": True, "evidence_parse_valid": False}
        return {
            "schema_version": 1,
            "evaluator_version": EVALUATOR_VERSION,
            "acceptance_id": spec.get("acceptance_id"),
            "evidence_tier": spec.get("evidence_tier"),
            "overall_status": "FAIL",
            "checks": checks,
            "reason_codes": reasons,
        }

    minimum_candidates = int(spec.get("minimum_real_llm_candidates", 2))
    checks = {
        "manifest_valid": True,
        "task_ids_match": (
            opt_result.get("task_id")
            == reject_result.get("task_id")
            == spec.get("task_id")
        ),
        "workflows_done": (
            opt_result.get("workflow") == "V2_CANDIDATE_PPA"
            and opt_result.get("status") == "DONE"
            and reject_result.get("workflow") == "V2_SAFETY_REJECTION"
            and reject_result.get("status") == "DONE"
        ),
        "toolchain_valid": opt_toolchain and reject_toolchain,
        "backend_valid": opt_backend and reject_backend,
        "candidate_tree_valid": tree_ok and rejection_tree_ok,
        "public_inputs_and_kernel_only_patches_bound": public_inputs_ok,
        "scores_recomputed": scores_ok,
        "two_real_llm_candidates": llm_count >= minimum_candidates,
        "best_matches_recomputed_comparator": best_ok,
        "final_candidate_verified": _final_validation(opt_result),
        "fallback_not_used": (
            opt_result.get("fallback") is None
            if spec.get("require_no_fallback") is True
            else True
        ),
        "rejected_candidate_did_not_pollute_best": _rejection_checks(
            reject_result, reject_registry
        ),
        "ledger_trace_actions_consistent": opt_accounting and rejection_accounting,
    }
    reason_map = {
        "task_ids_match": "TASK_ID_MISMATCH",
        "workflows_done": "WORKFLOW_NOT_DONE",
        "toolchain_valid": "TOOLCHAIN_INVALID",
        "backend_valid": "BACKEND_NOT_ALLOWED",
        "candidate_tree_valid": "CANDIDATE_TREE_INVALID",
        "public_inputs_and_kernel_only_patches_bound": "PUBLIC_INPUT_BINDING_INVALID",
        "scores_recomputed": "SCORE_RECOMPUTE_MISMATCH",
        "two_real_llm_candidates": "INSUFFICIENT_REAL_LLM_CANDIDATES",
        "best_matches_recomputed_comparator": "BEST_CANDIDATE_MISMATCH",
        "final_candidate_verified": "FINAL_VALIDATION_INVALID",
        "fallback_not_used": "FALLBACK_USED",
        "rejected_candidate_did_not_pollute_best": "SAFETY_REJECTION_INVALID",
        "ledger_trace_actions_consistent": "ACCOUNTING_OR_TRACE_MISMATCH",
    }
    reasons.extend(
        code for name, code in reason_map.items() if checks.get(name) is not True
    )
    combined_calls = {
        tool: int(opt_summary.get("tool_used", {}).get(tool, 0))
        + int(rejection_summary.get("tool_used", {}).get(tool, 0))
        for tool in ("csim", "synth", "cosim", "llm")
    }
    summary = {
        "llm_candidate_count": llm_count,
        "input_tokens": int(opt_summary.get("input_tokens_used", 0)),
        "output_tokens": int(opt_summary.get("output_tokens_used", 0)),
        "cached_input_tokens": int(opt_summary.get("cached_input_tokens_used", 0)),
        "tokens_used": int(opt_summary.get("tokens_used", 0)),
        "tool_calls": combined_calls,
        "tool_call_total": sum(combined_calls.values()),
        "credits_used": int(opt_summary.get("credits_used", 0))
        + int(rejection_summary.get("credits_used", 0)),
        "credits_remaining": int(opt_summary.get("credits_remaining", 0))
        + int(rejection_summary.get("credits_remaining", 0)),
        "optimization": opt_summary,
        "rejection": rejection_summary,
    }
    passed = not reasons
    status = "PASS" if passed and spec.get("evidence_tier") == "REAL" else (
        "TEST_PASS" if passed else "FAIL"
    )
    return {
        "schema_version": 1,
        "evaluator_version": EVALUATOR_VERSION,
        "acceptance_id": spec.get("acceptance_id"),
        "evidence_tier": spec.get("evidence_tier"),
        "overall_status": status,
        "checks": checks,
        "reason_codes": sorted(set(reasons)),
        "summary": summary,
        "runs": {
            "optimization_manifest_sha256": manifest_digest(opt_root),
            "rejection_manifest_sha256": manifest_digest(reject_root),
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_base_report(path: Path, result: Mapping[str, object], *, chinese: bool) -> None:
    title = "# V2 机器验收报告" if chinese else "# V2 Machine Acceptance Report"
    check_title = "## 验收条件" if chinese else "## Acceptance checks"
    summary_title = "## 核心统计" if chinese else "## Core statistics"
    lines = [
        title,
        "",
        f"- {'总体状态' if chinese else 'Overall status'}: `{result.get('overall_status')}`",
        f"- {'证据等级' if chinese else 'Evidence tier'}: `{result.get('evidence_tier')}`",
        "",
        summary_title,
        "",
        "```json",
        json.dumps(result.get("summary", {}), indent=2, sort_keys=True, ensure_ascii=False),
        "```",
        "",
        check_title,
        "",
    ]
    checks = result.get("checks")
    for name, passed in sorted(checks.items()) if isinstance(checks, Mapping) else []:
        lines.append(f"- `{name}`: `{'PASS' if passed is True else 'FAIL'}`")
    lines.extend(["", "Machine evidence: `acceptance_result.json`.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def evaluate_v2_acceptance(
    spec_path: str | Path,
    optimization_run: str | Path,
    rejection_run: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    result = compute_v2_acceptance(spec_path, optimization_run, rejection_run)
    output = Path(output_dir).resolve()
    _write_json(output / "acceptance_result.json", result)
    _write_base_report(output / "acceptance_report.md", result, chinese=False)
    _write_base_report(output / "acceptance_report_CN.md", result, chinese=True)
    return result
