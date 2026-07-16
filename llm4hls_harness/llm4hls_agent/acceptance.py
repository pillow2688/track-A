"""Unified deterministic V1 acceptance evaluation over immutable evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections import Counter
from pathlib import Path
from typing import Mapping

from .artifacts import (
    ArtifactManifestError,
    manifest_digest,
    verify_artifact_manifest,
)


EVALUATOR_VERSION = "v1.0"
_HLS_CLASSES = {"FUNCTIONAL_MISMATCH", "COMPILE_ERROR", "SYNTHESIS_ERROR"}
_BASELINE_PHASE = {
    "FUNCTIONAL_MISMATCH": ("csim", "runtime_fail"),
    "COMPILE_ERROR": ("csim", "compile_error"),
    "SYNTHESIS_ERROR": ("synth", "synth_error"),
}
_REAL_ARTIFACTS = {
    "csim": {"tcl", "vitis_stdout", "vitis_stderr", "csim_stdout", "csim_stderr"},
    "synth": {"tcl", "vitis_stdout", "vitis_stderr", "csynth_xml"},
    "cosim": {"tcl", "vitis_stdout", "vitis_stderr", "csynth_xml", "cosim_report"},
}


class AcceptanceError(RuntimeError):
    """Raised for an invalid evaluator invocation or acceptance specification."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{path} is not a JSON object")
    return value


def _evidence_path(run_root: Path, reference: object) -> Path:
    if not isinstance(reference, str) or not reference:
        raise AcceptanceError("evidence reference is missing")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or "\\" in reference:
        raise AcceptanceError("evidence reference escapes its run directory")
    path = (run_root / relative).resolve()
    try:
        path.relative_to(run_root.resolve())
    except ValueError as exc:
        raise AcceptanceError("evidence reference resolves outside its run directory") from exc
    return path


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_json(value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise AcceptanceError(f"cannot write {path}: {exc}") from exc


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise AcceptanceError(f"cannot write {path}: {exc}") from exc


def _write_acceptance_report(
    output_root: Path,
    result: Mapping[str, object],
    resolved_runs: Mapping[str, Path],
    spec_path: Path,
) -> None:
    result_path = (output_root / "acceptance_result.json").as_posix()
    lines = [
        "# V1 Unified Acceptance Report",
        "",
        f"- Evidence tier: `{result['evidence_tier']}`",
        f"- Overall status: `{result['overall_status']}`",
        f"- Evaluator: `{result['evaluator_version']}`",
        f"- Acceptance result: [acceptance_result.json]({result_path})",
        "",
        "## Acceptance matrix",
        "",
        "| Case | Kind | Error class | Status | Experimental report | Artifact Manifest |",
        "|---|---|---|---|---|---|",
    ]
    for case in result["cases"]:
        case_id = str(case["case_id"])
        run_root = resolved_runs.get(case_id)
        if run_root is None:
            report_link = "missing"
            manifest_link = "missing"
        else:
            report = (run_root / "experimental_report.md").as_posix()
            manifest = (run_root / "artifact_manifest.json").as_posix()
            report_link = f"[report]({report})"
            manifest_link = f"[manifest]({manifest})"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{case_id}`",
                    f"`{case['kind']}`",
                    f"`{case['error_class']}`",
                    f"`{case['status']}`",
                    report_link,
                    manifest_link,
                ]
            )
            + " |"
        )
    command = [
        "python3",
        "-m",
        "llm4hls_agent",
        "accept-v1",
        "--functional-run",
        str(resolved_runs.get("functional_mismatch", "")),
        "--compile-run",
        str(resolved_runs.get("compile_error", "")),
        "--synthesis-run",
        str(resolved_runs.get("synthesis_error", "")),
        "--patch-invalid-run",
        str(resolved_runs.get("patch_invalid", "")),
        "--spec",
        str(spec_path),
        "--output-dir",
        str(output_root),
    ]
    lines.extend(
        [
            "",
            "## Reproduce the deterministic evaluation",
            "",
            "```bash",
            " \\\n  ".join(shlex.quote(part) for part in command),
            "```",
            "",
            "The evaluator is read-only with respect to all four evidence run directories.",
        ]
    )
    _write_text(output_root / "acceptance_report.md", "\n".join(lines) + "\n")

    cn_lines = [
        "# V1 统一验收报告",
        "",
        f"- 证据等级：`{result['evidence_tier']}`",
        f"- 总体状态：`{result['overall_status']}`",
        f"- 验收器版本：`{result['evaluator_version']}`",
        f"- 机器可读结果：[acceptance_result.json]({result_path})",
        "",
        "## 验收矩阵",
        "",
        "| 场景 | 类别 | 错误类别 | 状态 | 实验报告 | 产物清单 |",
        "|---|---|---|---|---|---|",
    ]
    for case in result["cases"]:
        case_id = str(case["case_id"])
        run_root = resolved_runs.get(case_id)
        if run_root is None:
            report_link = "缺失"
            manifest_link = "缺失"
        else:
            report = (run_root / "experimental_report.md").as_posix()
            manifest = (run_root / "artifact_manifest.json").as_posix()
            report_link = f"[报告]({report})"
            manifest_link = f"[清单]({manifest})"
        cn_lines.append(
            "| "
            + " | ".join(
                [
                    f"`{case_id}`",
                    f"`{case['kind']}`",
                    f"`{case['error_class']}`",
                    f"`{case['status']}`",
                    report_link,
                    manifest_link,
                ]
            )
            + " |"
        )
    cn_lines.extend(
        [
            "",
            "## 复现确定性验收",
            "",
            "```bash",
            " \\\n  ".join(shlex.quote(part) for part in command),
            "```",
            "",
            "验收器只读上述四个证据运行目录，不会修改其中的文件。",
        ]
    )
    _write_text(output_root / "acceptance_report_CN.md", "\n".join(cn_lines) + "\n")


def _ledger(run_root: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    path = run_root / "budget_ledger.jsonl"
    events: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise AcceptanceError(f"cannot read budget ledger: {exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(f"invalid budget ledger: {exc}") from exc
        if not isinstance(value, dict) or value.get("sequence") != len(events):
            raise AcceptanceError("budget ledger sequence is invalid")
        events.append(value)
    if not events or events[0].get("state") != "INITIALIZED":
        raise AcceptanceError("budget ledger is not initialized")
    terminal = [
        event
        for event in events
        if event.get("state") in {"COMPLETED", "AMBIGUOUS"}
    ]
    counts = Counter(str(event.get("kind")) for event in terminal)
    return (
        {
            "credits_used": sum(int(event.get("actual_cost", 0)) for event in terminal),
            "tokens_used": sum(int(event.get("tokens_used", 0)) for event in terminal),
            "input_tokens_used": sum(
                int(event.get("input_tokens", 0)) for event in terminal
            ),
            "output_tokens_used": sum(
                int(event.get("output_tokens", 0)) for event in terminal
            ),
            "cached_input_tokens_used": sum(
                int(event.get("cached_input_tokens", 0)) for event in terminal
            ),
            "tool_used": dict(sorted(counts.items())),
        },
        events,
    )


def _add(reasons: list[str], condition: bool, code: str) -> None:
    if not condition and code not in reasons:
        reasons.append(code)


def _validation_action(
    run_root: Path,
    record: object,
    *,
    stage: str,
    candidate_id: str,
    code_hash: str,
    backend: str,
    evidence_tier: str,
    reasons: list[str],
) -> None:
    item = record if isinstance(record, Mapping) else {}
    _add(reasons, item.get("status") == "PASS", f"{stage.upper()}_NOT_PASS")
    _add(reasons, item.get("phase") == "pass", f"{stage.upper()}_PHASE_INVALID")
    reference = item.get("result_ref")
    if not isinstance(reference, str):
        _add(reasons, False, f"{stage.upper()}_RESULT_MISSING")
        return
    try:
        action = _read_json(_evidence_path(run_root, reference))
    except AcceptanceError:
        _add(reasons, False, f"{stage.upper()}_RESULT_INVALID")
        return
    _add(reasons, action.get("candidate_id") == candidate_id, "CANDIDATE_BINDING_INVALID")
    _add(reasons, action.get("code_hash") == code_hash, "CODE_HASH_BINDING_INVALID")
    _add(reasons, action.get("backend_fingerprint") == backend, "BACKEND_NOT_ALLOWED")
    if evidence_tier == "REAL":
        artifacts = action.get("artifacts")
        names = set(artifacts) if isinstance(artifacts, Mapping) else set()
        _add(
            reasons,
            _REAL_ARTIFACTS[stage].issubset(names),
            f"{stage.upper()}_REAL_ARTIFACTS_MISSING",
        )


def _baseline_action(
    run_root: Path,
    record: object,
    *,
    stage: str,
    phase: str,
    backend: str,
    evidence_tier: str,
    reasons: list[str],
) -> None:
    item = record if isinstance(record, Mapping) else {}
    reference = item.get("result_ref")
    if not isinstance(reference, str):
        _add(reasons, False, "BASELINE_RESULT_MISSING")
        return
    try:
        action = _read_json(_evidence_path(run_root, reference))
    except AcceptanceError:
        _add(reasons, False, "BASELINE_RESULT_INVALID")
        return
    _add(reasons, action.get("candidate_id") == "candidate_000", "BASELINE_BINDING_INVALID")
    _add(reasons, action.get("backend_fingerprint") == backend, "BACKEND_NOT_ALLOWED")
    if evidence_tier == "REAL":
        artifacts = action.get("artifacts")
        names = set(artifacts) if isinstance(artifacts, Mapping) else set()
        required = {"tcl", "vitis_stdout", "vitis_stderr"}
        if stage == "csim" and phase != "compile_error":
            required.update({"csim_stdout", "csim_stderr"})
        _add(reasons, required.issubset(names), "BASELINE_REAL_ARTIFACTS_MISSING")


def _accounting_checks(
    run_root: Path, result: Mapping[str, object], reasons: list[str]
) -> list[dict[str, object]]:
    try:
        calculated, events = _ledger(run_root)
    except AcceptanceError:
        _add(reasons, False, "LEDGER_INVALID")
        return []
    actions: dict[str, list[dict[str, object]]] = {}
    for event in events[1:]:
        action_id = event.get("action_id")
        if isinstance(action_id, str):
            actions.setdefault(action_id, []).append(event)
    for action_events in actions.values():
        terminal = action_events[-1]
        _add(
            reasons,
            terminal.get("state") == "COMPLETED",
            "LEDGER_ACTION_INCOMPLETE",
        )
        if terminal.get("state") != "COMPLETED":
            continue
        try:
            result_path = _evidence_path(run_root, terminal.get("result_ref"))
            encoded = result_path.read_bytes()
        except (AcceptanceError, OSError):
            _add(reasons, False, "LEDGER_RESULT_BINDING_INVALID")
            continue
        _add(
            reasons,
            terminal.get("result_sha256") == _sha256(encoded),
            "LEDGER_RESULT_BINDING_INVALID",
        )
    budget = result.get("budget")
    budget_value = budget if isinstance(budget, Mapping) else {}
    for field in (
        "credits_used",
        "tokens_used",
        "input_tokens_used",
        "output_tokens_used",
        "cached_input_tokens_used",
    ):
        _add(
            reasons,
            budget_value.get(field) == calculated[field],
            "BUDGET_ACCOUNTING_MISMATCH",
        )
    reported_calls = budget_value.get("tool_used")
    reported = reported_calls if isinstance(reported_calls, Mapping) else {}
    calculated_calls = calculated["tool_used"]
    for kind in set(reported) | set(calculated_calls):  # type: ignore[arg-type]
        _add(
            reasons,
            int(reported.get(kind, 0)) == int(calculated_calls.get(kind, 0)),  # type: ignore[union-attr]
            "TOOL_CALL_ACCOUNTING_MISMATCH",
        )
    return events


def _evaluate_hls(
    run_root: Path,
    case: Mapping[str, object],
    spec: Mapping[str, object],
    manifest: Mapping[str, object],
    result: Mapping[str, object],
    registry: Mapping[str, object],
    reasons: list[str],
) -> None:
    error_class = str(case.get("error_class", ""))
    _add(reasons, error_class in _HLS_CLASSES, "HLS_ERROR_CLASS_INVALID")
    _add(reasons, result.get("status") == "DONE", "WORKFLOW_NOT_DONE")
    _add(
        reasons,
        result.get("stop_reason") == "CANDIDATE_VERIFIED",
        "STOP_REASON_INVALID",
    )
    diagnostic = result.get("diagnostic")
    diagnostic_value = diagnostic if isinstance(diagnostic, Mapping) else {}
    _add(reasons, diagnostic_value.get("code") == error_class, "ERROR_CLASS_MISMATCH")
    baseline = result.get("baseline")
    baseline_value = baseline if isinstance(baseline, Mapping) else {}
    _add(reasons, baseline_value.get("baseline_unchanged") is True, "BASELINE_MUTATED")
    baseline_validation = baseline_value.get("validation")
    baseline_stages = baseline_validation if isinstance(baseline_validation, Mapping) else {}
    expected_stage, expected_phase = _BASELINE_PHASE.get(error_class, ("", ""))
    expected = baseline_stages.get(expected_stage)
    expected_value = expected if isinstance(expected, Mapping) else {}
    _add(reasons, expected_value.get("status") == "FAIL", "BASELINE_STAGE_NOT_FAILED")
    _add(reasons, expected_value.get("phase") == expected_phase, "BASELINE_PHASE_MISMATCH")
    if error_class == "SYNTHESIS_ERROR":
        csim = baseline_stages.get("csim")
        csim_value = csim if isinstance(csim, Mapping) else {}
        _add(reasons, csim_value.get("status") == "PASS", "BASELINE_CSIM_NOT_PASS")

    backend = str(spec.get("required_backend_fingerprint", ""))
    evidence_tier = str(spec.get("evidence_tier"))
    _baseline_action(
        run_root,
        expected,
        stage=expected_stage,
        phase=expected_phase,
        backend=backend,
        evidence_tier=evidence_tier,
        reasons=reasons,
    )
    if error_class == "SYNTHESIS_ERROR":
        _baseline_action(
            run_root,
            baseline_stages.get("csim"),
            stage="csim",
            phase="pass",
            backend=backend,
            evidence_tier=evidence_tier,
            reasons=reasons,
        )

    candidate_id = result.get("candidate_id")
    _add(
        reasons,
        isinstance(candidate_id, str) and candidate_id != "candidate_000",
        "CANDIDATE_MISSING",
    )
    candidates = registry.get("candidates")
    candidate_map = candidates if isinstance(candidates, Mapping) else {}
    candidate = candidate_map.get(candidate_id, {})
    candidate_value = candidate if isinstance(candidate, Mapping) else {}
    _add(reasons, candidate_value.get("status") == "VERIFIED", "CANDIDATE_NOT_VERIFIED")
    _add(reasons, registry.get("best_candidate_id") == candidate_id, "BEST_CANDIDATE_INVALID")
    _add(reasons, registry.get("final_candidate_id") == candidate_id, "FINAL_CANDIDATE_INVALID")
    provider = str(spec.get("required_provider", ""))
    model = str(spec.get("required_model", ""))
    _add(reasons, candidate_value.get("provider") == provider, "PROVIDER_NOT_ALLOWED")
    _add(reasons, candidate_value.get("model") == model, "MODEL_NOT_ALLOWED")
    acceptance = result.get("v1_acceptance")
    acceptance_value = acceptance if isinstance(acceptance, Mapping) else {}
    _add(reasons, acceptance_value.get("accepted") is True, "LLM_ACCEPTANCE_FALSE")
    _add(
        reasons,
        acceptance_value.get("token_usage_complete") is True,
        "TOKEN_USAGE_INCOMPLETE",
    )
    budget = result.get("budget")
    budget_value = budget if isinstance(budget, Mapping) else {}
    _add(reasons, int(budget_value.get("input_tokens_used", 0)) > 0, "INPUT_TOKENS_ZERO")
    _add(reasons, int(budget_value.get("output_tokens_used", 0)) > 0, "OUTPUT_TOKENS_ZERO")
    clock = result.get("clock_constraint")
    clock_value = clock if isinstance(clock, Mapping) else {}
    _add(reasons, clock_value.get("passed") is True, "CLOCK_NOT_PASS")
    _add(
        reasons,
        manifest.get("toolchain_version") == spec.get("required_toolchain"),
        "TOOLCHAIN_NOT_ALLOWED",
    )
    validation = result.get("validation")
    validation_value = validation if isinstance(validation, Mapping) else {}
    code_hash = str(candidate_value.get("code_hash", ""))
    for stage in ("csim", "synth", "cosim"):
        _validation_action(
            run_root,
            validation_value.get(stage),
            stage=stage,
            candidate_id=str(candidate_id),
            code_hash=code_hash,
            backend=backend,
            evidence_tier=str(spec.get("evidence_tier")),
            reasons=reasons,
        )
    _accounting_checks(run_root, result, reasons)


def _evaluate_safety(
    run_root: Path,
    result: Mapping[str, object],
    registry: Mapping[str, object],
    manifest: Mapping[str, object],
    reasons: list[str],
) -> None:
    _add(reasons, result.get("status") == "FAILED", "SAFETY_WORKFLOW_NOT_FAILED")
    _add(reasons, result.get("stop_reason") == "PATCH_INVALID", "SAFETY_REASON_INVALID")
    _add(reasons, result.get("candidate_id") is None, "SAFETY_CANDIDATE_ALLOCATED")
    baseline = result.get("baseline")
    baseline_value = baseline if isinstance(baseline, Mapping) else {}
    _add(reasons, baseline_value.get("baseline_unchanged") is True, "BASELINE_MUTATED")
    candidates = registry.get("candidates")
    candidate_map = candidates if isinstance(candidates, Mapping) else {}
    _add(reasons, set(candidate_map) == {"candidate_000"}, "SAFETY_REGISTRY_POLLUTED")
    _add(
        reasons,
        registry.get("best_candidate_id") in {None, "candidate_000"},
        "BEST_CANDIDATE_INVALID",
    )
    _add(
        reasons,
        registry.get("final_candidate_id") in {None, "candidate_000"},
        "FINAL_CANDIDATE_INVALID",
    )
    candidates_root = run_root / "candidates"
    materialized = (
        [path for path in candidates_root.iterdir() if path.name.startswith("candidate_")]
        if candidates_root.is_dir()
        else []
    )
    _add(reasons, not materialized, "SAFETY_CANDIDATE_MATERIALIZED")
    entries = manifest.get("artifacts")
    entry_values = entries if isinstance(entries, list) else []
    _add(
        reasons,
        not any(
            isinstance(entry, Mapping)
            and entry.get("artifact_type")
            in {"candidate_source", "candidate_patch", "candidate_metadata"}
            for entry in entry_values
        ),
        "SAFETY_MANIFEST_POLLUTED",
    )
    events = _accounting_checks(run_root, result, reasons)
    _add(
        reasons,
        not any(
            event.get("state") == "STARTED"
            and event.get("kind") in {"csim", "synth", "cosim"}
            and event.get("candidate_id") != "candidate_000"
            for event in events
        ),
        "SAFETY_CANDIDATE_TOOL_CHARGED",
    )


def _evaluate_case(
    case: Mapping[str, object],
    run_dir: Path | None,
    spec: Mapping[str, object],
) -> dict[str, object]:
    case_id = str(case.get("case_id", ""))
    reasons: list[str] = []
    evidence_refs = ["artifact_manifest.json", "candidate_registry.json", "v1_result.json"]
    digest: str | None = None
    if run_dir is None:
        reasons.append("RUN_DIRECTORY_MISSING")
    else:
        run_root = run_dir.resolve()
        try:
            manifest = verify_artifact_manifest(run_root)
            digest = manifest_digest(run_root)
        except (ArtifactManifestError, OSError):
            reasons.append("MANIFEST_INVALID")
        else:
            try:
                result = _read_json(run_root / "v1_result.json")
                registry = _read_json(run_root / "candidate_registry.json")
            except AcceptanceError:
                reasons.append("CORE_RESULT_INVALID")
            else:
                _add(reasons, manifest.get("task_id") == case.get("task_id"), "TASK_ID_MISMATCH")
                if case.get("kind") == "hls":
                    _evaluate_hls(run_root, case, spec, manifest, result, registry, reasons)
                elif case.get("kind") == "safety":
                    _evaluate_safety(run_root, result, registry, manifest, reasons)
                else:
                    reasons.append("CASE_KIND_INVALID")
    return {
        "case_id": case_id,
        "kind": case.get("kind"),
        "error_class": case.get("error_class"),
        "status": "PASS" if not reasons else "FAIL",
        "reason_codes": sorted(reasons),
        "evidence_refs": evidence_refs,
        "manifest_digest": digest,
    }


def evaluate_acceptance(
    spec_path: str | Path,
    run_dirs: Mapping[str, str | Path],
    output_dir: str | Path,
) -> dict[str, object]:
    """Evaluate all specified cases without mutating their evidence directories."""

    path = Path(spec_path).resolve()
    try:
        spec_bytes = path.read_bytes()
    except OSError as exc:
        raise AcceptanceError(f"cannot read acceptance spec: {exc}") from exc
    spec = _read_json(path)
    if spec.get("schema_version") != 1:
        raise AcceptanceError("unsupported acceptance specification schema")
    tier = spec.get("evidence_tier")
    if tier not in {"REAL", "TEST"}:
        raise AcceptanceError("evidence_tier must be REAL or TEST")
    raw_cases = spec.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise AcceptanceError("acceptance specification has no cases")
    cases: list[Mapping[str, object]] = []
    identifiers: set[str] = set()
    for value in raw_cases:
        if not isinstance(value, Mapping):
            raise AcceptanceError("acceptance case is not an object")
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in identifiers:
            raise AcceptanceError("acceptance case IDs must be unique non-empty strings")
        identifiers.add(case_id)
        cases.append(value)
    resolved_runs = {key: Path(value).resolve() for key, value in run_dirs.items()}
    output_root = Path(output_dir).resolve()
    for run_root in resolved_runs.values():
        try:
            output_root.relative_to(run_root)
        except ValueError:
            continue
        raise AcceptanceError("acceptance output directory must be outside evidence runs")
    evaluated = [
        _evaluate_case(case, resolved_runs.get(str(case["case_id"])), spec)
        for case in sorted(cases, key=lambda item: str(item["case_id"]))
    ]
    policy_reasons: list[str] = []
    if tier == "REAL":
        hls = {
            str(case.get("error_class"))
            for case in cases
            if case.get("kind") == "hls"
        }
        safety = {
            str(case.get("error_class"))
            for case in cases
            if case.get("kind") == "safety"
        }
        if hls != _HLS_CLASSES:
            policy_reasons.append("REAL_HLS_MATRIX_INCOMPLETE")
        if safety != {"PATCH_INVALID"}:
            policy_reasons.append("REAL_SAFETY_MATRIX_INCOMPLETE")
    all_pass = all(case["status"] == "PASS" for case in evaluated)
    if all_pass and not policy_reasons:
        overall = "PASS" if tier == "REAL" else "TEST_PASS"
    else:
        overall = "FAIL"
    result = {
        "schema_version": 1,
        "acceptance_id": spec.get("acceptance_id"),
        "acceptance_spec_version": spec.get("schema_version"),
        "acceptance_spec_digest": _sha256(spec_bytes),
        "evaluator_version": EVALUATOR_VERSION,
        "evidence_tier": tier,
        "overall_status": overall,
        "policy_reason_codes": sorted(policy_reasons),
        "cases": evaluated,
        "manifest_digests": {
            str(case["case_id"]): case["manifest_digest"] for case in evaluated
        },
    }
    _write_json(output_root / "acceptance_result.json", result)
    _write_acceptance_report(output_root, result, resolved_runs, path)
    return result
