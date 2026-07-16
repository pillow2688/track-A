"""Read-only V1 evidence aggregation and flat human-review report rendering."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

from .acceptance import AcceptanceError, compute_acceptance
from .artifacts import manifest_digest, verify_artifact_manifest


REPORT_EN = "V1_ACCEPTANCE_REPORT.md"
REPORT_CN = "V1_ACCEPTANCE_REPORT_CN.md"
DASHBOARD = "V1_ACCEPTANCE_DASHBOARD.html"
_CASE_ORDER = ("compile_error", "functional_mismatch", "synthesis_error", "patch_invalid")
_TERMINAL_LEDGER_STATES = {"COMPLETED", "AMBIGUOUS"}


class ReviewError(RuntimeError):
    """Raised when a human-review report cannot be generated safely."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"cannot read JSON evidence {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"JSON evidence is not an object: {path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReviewError(f"cannot read JSONL evidence {path.name}: {exc}") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewError(f"invalid {path.name} line {index}: {exc}") from exc
        if not isinstance(value, dict):
            raise ReviewError(f"invalid {path.name} line {index}: not an object")
        values.append(value)
    return values


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _relative_link(target: Path, runs_root: Path) -> str:
    target_resolved = target.resolve()
    root_resolved = runs_root.resolve()
    try:
        relative = target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ReviewError("evidence link escapes the runs directory") from exc
    value = relative.as_posix()
    if not value or value.startswith("/") or "\\" in value or ".." in Path(value).parts:
        raise ReviewError("invalid evidence link")
    return value


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ReviewError(f"cannot write human-review file {path.name}: {exc}") from exc


def _machine_snapshot(
    run_dirs: Mapping[str, Path], acceptance_result: Path
) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    paths: set[Path] = {acceptance_result.resolve()}
    for run_root in run_dirs.values():
        manifest = verify_artifact_manifest(run_root)
        paths.add((run_root / "artifact_manifest.json").resolve())
        entries = manifest.get("artifacts")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("path"), str):
                paths.add((run_root / str(entry["path"])).resolve())
    for path in sorted(paths):
        try:
            stat = path.stat()
        except OSError as exc:
            raise ReviewError(f"machine evidence disappeared: {path.name}") from exc
        snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns, _sha256_file(path))
    return snapshot


def _ledger_summary(
    run_root: Path, result: Mapping[str, object]
) -> tuple[dict[str, object], list[dict[str, object]], bool]:
    events = _read_jsonl(run_root / "budget_ledger.jsonl")
    terminal = [event for event in events if event.get("state") in _TERMINAL_LEDGER_STATES]
    counts = Counter(str(event.get("kind")) for event in terminal)
    summary = {
        "tool_used": {name: counts.get(name, 0) for name in ("csim", "synth", "cosim", "llm")},
        "credits_used": sum(_as_int(event.get("actual_cost")) for event in terminal),
        "input_tokens": sum(_as_int(event.get("input_tokens")) for event in terminal),
        "output_tokens": sum(_as_int(event.get("output_tokens")) for event in terminal),
        "cached_input_tokens": sum(
            _as_int(event.get("cached_input_tokens")) for event in terminal
        ),
        "tokens_used": sum(_as_int(event.get("tokens_used")) for event in terminal),
    }
    budget = _as_mapping(result.get("budget"))
    budget_tools = _as_mapping(budget.get("tool_used"))
    consistent = (
        summary["credits_used"] == _as_int(budget.get("credits_used"))
        and summary["input_tokens"] == _as_int(budget.get("input_tokens_used"))
        and summary["output_tokens"] == _as_int(budget.get("output_tokens_used"))
        and summary["cached_input_tokens"] == _as_int(budget.get("cached_input_tokens_used"))
        and summary["tokens_used"] == _as_int(budget.get("tokens_used"))
        and all(
            _as_mapping(summary["tool_used"]).get(name) == _as_int(budget_tools.get(name))
            for name in ("csim", "synth", "cosim", "llm")
        )
    )
    for event in terminal:
        reference = event.get("result_ref")
        expected_hash = event.get("result_sha256")
        if not isinstance(reference, str) or not isinstance(expected_hash, str):
            consistent = False
            continue
        path = run_root / reference
        if not path.is_file() or _sha256_file(path) != expected_hash:
            consistent = False
    return summary, events, consistent


def _trace_summary(
    run_root: Path, ledger_events: Iterable[Mapping[str, object]]
) -> tuple[list[str], bool]:
    events = _read_jsonl(run_root / "trace.jsonl")
    summary: list[str] = []
    tool_ids: set[str] = set()
    llm_completed = 0
    for event in events:
        name = str(event.get("event", ""))
        if name == "TOOL_COMPLETED":
            kind = str(event.get("kind", "tool")).upper()
            candidate = str(event.get("candidate_id") or "none")
            phase = str(event.get("phase", "unknown")).upper()
            summary.append(f"{candidate} {kind} {phase}")
            if isinstance(event.get("action_id"), str):
                tool_ids.add(str(event["action_id"]))
        elif name == "LLM_COMPLETED":
            llm_completed += 1
            provider = str(event.get("provider", "unknown")).split(":", 1)[0]
            summary.append(f"PROPOSAL COMPLETED ({provider})")
        elif name in {
            "RUN_STARTED",
            "BASELINE_READY",
            "V1_DIAGNOSTIC_READY",
            "CANDIDATE_MATERIALIZED",
            "PATCH_REJECTED",
            "V1_COMPLETED",
        }:
            suffix = ""
            if name == "V1_COMPLETED":
                suffix = f" ({event.get('status')}/{event.get('stop_reason')})"
            summary.append(name + suffix)
    ledger_terminal = [
        event for event in ledger_events if event.get("state") in _TERMINAL_LEDGER_STATES
    ]
    expected_tool_ids = {
        Path(str(event.get("result_ref"))).parent.name
        for event in ledger_terminal
        if event.get("kind") in {"csim", "synth", "cosim"}
        and isinstance(event.get("result_ref"), str)
    }
    expected_llm = sum(1 for event in ledger_terminal if event.get("kind") == "llm")
    consistent = tool_ids == expected_tool_ids and llm_completed == expected_llm
    return summary, consistent


def _patch_files(patch: str) -> list[str]:
    files: list[str] = []
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        if value not in files:
            files.append(value)
    return files


def _changed_line(patch: str) -> int | None:
    old_line = 0
    for line in patch.splitlines():
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            old_line = int(match.group(1))
            continue
        if not old_line or line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            return old_line
        if line.startswith(" "):
            old_line += 1
    return None


def _signature(source: str, top: str) -> str | None:
    pattern = re.compile(rf"^[^\n]*\b{re.escape(top)}\s*\([^\n]*", re.MULTILINE)
    match = pattern.search(source)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(0).split("{", 1)[0].strip())


def _patch_display(patch: str, limit: int = 30) -> dict[str, object]:
    lines = patch.splitlines()
    truncated = len(lines) > limit
    displayed = lines[:limit]
    if truncated:
        displayed.append(f"... [truncated: showing {limit} of {len(lines)} lines]")
    return {
        "line_count": len(lines),
        "truncated": truncated,
        "display": "\n".join(displayed),
    }


def _diff_stats(patch: str) -> tuple[int, int, int]:
    additions = 0
    deletions = 0
    hunks = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions, hunks


def _localize_error(
    diagnostic: Mapping[str, object], patch: str, task: Mapping[str, object], safety: bool
) -> dict[str, object]:
    evidence = [str(value) for value in diagnostic.get("evidence", [])] if isinstance(
        diagnostic.get("evidence"), list
    ) else []
    joined = "\n".join(evidence)
    code = "PATCH_INVALID" if safety else str(diagnostic.get("code", "UNKNOWN"))
    top = str(task.get("top", "unknown"))
    file_name = str(task.get("kernel_file", "kernel.cpp"))
    line: int | None = None
    column: int | None = None
    symbol = top
    key_log = str(diagnostic.get("summary", "unknown error"))
    subtype = "unknown"
    compiler = re.search(
        r"(?P<file>[\w./-]+\.cpp):(?P<line>\d+):(?P<column>\d+): error: (?P<message>[^\n]+)",
        joined,
    )
    hls = re.search(
        r"ERROR: \[HLS [^]]+\].*?: (?P<message>[^\n(]+).*?\((?P<file>[^():]+\.cpp):(?P<line>\d+):(?P<column>\d+)\)",
        joined,
    )
    if safety:
        files = _patch_files(patch)
        file_name = files[0] if files else "unknown"
        line = _changed_line(patch)
        symbol = "forbidden testbench change"
        subtype = "forbidden_file_modification"
        key_log = "Patch policy rejected modification outside the kernel source"
    elif compiler:
        file_name = Path(compiler.group("file")).name
        line = int(compiler.group("line"))
        column = int(compiler.group("column"))
        message = compiler.group("message").strip()
        identifier = re.search(r"undeclared identifier ['\"]([^'\"]+)", message)
        symbol = identifier.group(1) if identifier else top
        subtype = "undeclared_identifier" if identifier else "compiler_error"
        key_log = f"{file_name}:{line}:{column}: {message}"
    elif hls:
        file_name = Path(hls.group("file")).name
        line = int(hls.group("line"))
        column = int(hls.group("column"))
        message = hls.group("message").strip()
        symbol_match = re.search(r"Undefined function (operator [^ ]+)", message)
        symbol = symbol_match.group(1) if symbol_match else top
        subtype = (
            "unsupported_dynamic_allocation"
            if "operator new[]" in joined or "operator delete[]" in joined
            else "hls_synthesis_error"
        )
        key_log = f"{file_name}:{line}:{column}: {message}"
    elif code == "FUNCTIONAL_MISMATCH":
        mismatch = next((value for value in evidence if "mismatch" in value.lower()), key_log)
        line = _changed_line(patch)
        subtype = "output_mismatch"
        key_log = mismatch
    return {
        "category": code,
        "subtype": subtype,
        "file": file_name,
        "line": line,
        "column": column,
        "symbol": symbol,
        "top": top,
        "key_log": key_log,
    }


def _check(
    check_id: str,
    condition: bool,
    observed: object,
    expected: object,
    evidence_refs: Iterable[str] = (),
) -> dict[str, object]:
    return {
        "check_id": check_id,
        "status": "PASS" if condition else "FAIL",
        "observed": observed,
        "expected": expected,
        "evidence_refs": list(evidence_refs),
    }


def _existing_links(run_root: Path, runs_root: Path, references: Iterable[object]) -> list[str]:
    links: list[str] = []
    for reference in references:
        if not isinstance(reference, str) or not reference:
            continue
        path = run_root / reference
        if path.is_file():
            value = _relative_link(path, runs_root)
            if value not in links:
                links.append(value)
    return links


def _action_binding_ok(
    run_root: Path,
    validation: Mapping[str, object],
    candidate_id: str,
    code_hash: str,
) -> bool:
    for stage in ("csim", "synth", "cosim"):
        item = _as_mapping(validation.get(stage))
        reference = item.get("result_ref")
        if not isinstance(reference, str) or not (run_root / reference).is_file():
            return False
        action = _read_json(run_root / reference)
        if (
            action.get("candidate_id") != candidate_id
            or action.get("code_hash") != code_hash
            or action.get("kind") != stage
        ):
            return False
    return True


def _acceptance_consistent(
    recorded: Mapping[str, object], recomputed: Mapping[str, object]
) -> bool:
    if recorded.get("overall_status") != recomputed.get("overall_status"):
        return False
    if recorded.get("policy_reason_codes") != recomputed.get("policy_reason_codes"):
        return False
    recorded_cases = {
        str(case.get("case_id")): case
        for case in recorded.get("cases", [])
        if isinstance(case, Mapping)
    }
    recomputed_cases = {
        str(case.get("case_id")): case
        for case in recomputed.get("cases", [])
        if isinstance(case, Mapping)
    }
    if set(recorded_cases) != set(recomputed_cases):
        return False
    for case_id, case in recomputed_cases.items():
        previous = recorded_cases[case_id]
        for field in ("status", "reason_codes", "manifest_digest"):
            if previous.get(field) != case.get(field):
                return False
    return True


def _build_case(
    case_id: str,
    run_root: Path,
    runs_root: Path,
    recomputed_case: Mapping[str, object],
    recorded_case: Mapping[str, object],
) -> dict[str, object]:
    manifest = verify_artifact_manifest(run_root)
    result = _read_json(run_root / "v1_result.json")
    registry = _read_json(run_root / "candidate_registry.json")
    task = _read_json(run_root / "task_spec.json")
    diagnostic = _read_json(run_root / "diagnostics" / "candidate_000.json")
    ledger, ledger_events, ledger_consistent = _ledger_summary(run_root, result)
    trace, trace_consistent = _trace_summary(run_root, ledger_events)
    patch_value = _as_mapping(result.get("patch"))
    raw_patch = str(patch_value.get("applied_patch") or patch_value.get("patch") or "")
    patch_files = _patch_files(raw_patch)
    parsed_additions, parsed_deletions, parsed_hunks = _diff_stats(raw_patch)
    safety = case_id == "patch_invalid"
    localization = _localize_error(diagnostic, raw_patch, task, safety)
    proposal_refs = [
        str(event.get("result_ref"))
        for event in ledger_events
        if event.get("state") in _TERMINAL_LEDGER_STATES
        and event.get("kind") == "llm"
        and isinstance(event.get("result_ref"), str)
    ]
    proposals = [_read_json(run_root / ref) for ref in proposal_refs]
    real_proposals = [value for value in proposals if value.get("provider") == "openai-compatible"]
    proposal = proposals[-1] if proposals else patch_value
    provider = str(proposal.get("provider", patch_value.get("provider", "unknown")))
    model = str(proposal.get("model", patch_value.get("model", "unknown")))
    budget = _as_mapping(result.get("budget"))
    tool_used = _as_mapping(ledger.get("tool_used"))
    candidate_id = result.get("candidate_id")
    candidate_map = _as_mapping(registry.get("candidates"))
    candidate = _as_mapping(candidate_map.get(candidate_id))
    baseline = _as_mapping(result.get("baseline"))
    baseline_validation = _as_mapping(baseline.get("validation"))
    final_validation = _as_mapping(result.get("validation"))
    baseline_stage = str(diagnostic.get("stage", "unknown"))
    baseline_phase = str(diagnostic.get("phase", "unknown"))
    baseline_item = _as_mapping(baseline_validation.get(baseline_stage))
    baseline_ref = baseline_item.get("result_ref")
    baseline_real = (
        baseline_item.get("status") == "FAIL"
        and baseline_item.get("phase") == baseline_phase
        and isinstance(baseline_ref, str)
        and (run_root / baseline_ref).is_file()
    )
    manifest_entries = manifest.get("artifacts")
    entries = manifest_entries if isinstance(manifest_entries, list) else []
    manifest_paths = {
        str(entry.get("path")) for entry in entries if isinstance(entry, Mapping)
    }
    fallback_absent = not any(path.startswith("v0_fallback/") for path in manifest_paths)
    kernel_file = str(task.get("kernel_file", "kernel.cpp"))
    additions = _as_int(patch_value.get("additions"), parsed_additions)
    deletions = _as_int(patch_value.get("deletions"), parsed_deletions)
    hunks = _as_int(patch_value.get("hunks"), parsed_hunks)
    patch_changed = additions + deletions
    patch_minimal = (
        not safety
        and patch_files == [kernel_file]
        and 0 < patch_changed <= 30
        and 1 <= hunks <= 30
    )
    baseline_source = (run_root / "baseline" / "source" / kernel_file).read_text(
        encoding="utf-8"
    )
    source_ref = candidate.get("source_ref")
    final_source = (
        (run_root / str(source_ref)).read_text(encoding="utf-8")
        if isinstance(source_ref, str) and (run_root / source_ref).is_file()
        else ""
    )
    top = str(task.get("top", ""))
    interface_unchanged = (
        not safety
        and _signature(baseline_source, top) is not None
        and _signature(baseline_source, top) == _signature(final_source, top)
    )
    validation_comparison: dict[str, object] = {}
    final_gates = not safety
    for stage in ("csim", "synth", "cosim"):
        before = _as_mapping(baseline_validation.get(stage))
        after = _as_mapping(final_validation.get(stage))
        passed = after.get("status") == "PASS"
        final_gates = final_gates and passed
        validation_comparison[stage] = {
            "baseline_status": before.get("status", "NOT_RUN"),
            "baseline_phase": before.get("phase", ""),
            "final_status": after.get("status", "NOT_RUN"),
            "final_phase": after.get("phase", ""),
            "accepted": passed if not safety else None,
            "evidence_ref": after.get("result_ref"),
        }
    clock = _as_mapping(result.get("clock_constraint"))
    if not safety:
        final_gates = final_gates and clock.get("passed") is True
    validation_comparison["clock"] = {
        "baseline_status": "NOT_AVAILABLE",
        "final_status": "PASS" if clock.get("passed") is True else "FAIL",
        "target_period_ns": clock.get("maximum_period_ns"),
        "estimated_period_ns": clock.get("estimated_period_ns"),
        "accepted": clock.get("passed") if not safety else None,
    }
    metrics: Mapping[str, object] = {}
    metrics_ref = candidate.get("metrics_ref")
    if isinstance(metrics_ref, str) and (run_root / metrics_ref).is_file():
        metrics = _as_mapping(_read_json(run_root / metrics_ref).get("report"))
    candidate_promoted = (
        not safety
        and isinstance(candidate_id, str)
        and candidate.get("parent_id") == "candidate_000"
        and candidate.get("status") == "VERIFIED"
        and candidate.get("immutable") is True
        and registry.get("best_candidate_id") == candidate_id
        and registry.get("final_candidate_id") == candidate_id
    )
    binding_ok = (
        _action_binding_ok(
            run_root,
            final_validation,
            str(candidate_id),
            str(candidate.get("code_hash", "")),
        )
        if not safety
        else True
    )
    token_complete = (
        budget.get("token_usage_complete") is True
        and _as_int(ledger.get("input_tokens")) == _as_int(budget.get("input_tokens_used"))
        and _as_int(ledger.get("output_tokens")) == _as_int(budget.get("output_tokens_used"))
        and _as_int(ledger.get("cached_input_tokens"))
        == _as_int(budget.get("cached_input_tokens_used"))
    )
    core_refs = _existing_links(
        run_root,
        runs_root,
        [
            "artifact_manifest.json",
            "v1_result.json",
            "candidate_registry.json",
            "budget_ledger.jsonl",
            "trace.jsonl",
            "diagnostics/candidate_000.json",
            baseline_ref,
            *proposal_refs,
            *(item.get("result_ref") for item in final_validation.values() if isinstance(item, Mapping)),
            source_ref,
            candidate.get("patch_ref"),
        ],
    )
    recorded_matches = all(
        recorded_case.get(field) == recomputed_case.get(field)
        for field in ("status", "reason_codes", "manifest_digest")
    )
    checks: list[dict[str, object]] = [
        _check("manifest_integrity", True, manifest_digest(run_root), "valid manifest", core_refs[:1]),
        _check(
            "recorded_acceptance_consistent",
            recorded_matches,
            recorded_case.get("status"),
            recomputed_case.get("status"),
            [_relative_link(run_root.parent / "v1-acceptance" / "acceptance_result.json", runs_root)],
        ),
        _check(
            "deterministic_acceptance",
            recomputed_case.get("status") == "PASS",
            recomputed_case.get("reason_codes"),
            [],
            core_refs,
        ),
        _check(
            "baseline_error_real",
            baseline_real,
            f"{baseline_stage}/{baseline_item.get('status')}/{baseline_item.get('phase')}",
            f"{baseline_stage}/FAIL/{baseline_phase}",
            _existing_links(run_root, runs_root, [baseline_ref]),
        ),
        _check(
            "ledger_trace_action_consistent",
            ledger_consistent and trace_consistent,
            {"ledger": ledger_consistent, "trace": trace_consistent},
            {"ledger": True, "trace": True},
            _existing_links(run_root, runs_root, ["budget_ledger.jsonl", "trace.jsonl"]),
        ),
        _check(
            "token_usage_complete",
            token_complete,
            {
                "input": ledger.get("input_tokens"),
                "output": ledger.get("output_tokens"),
                "cached": ledger.get("cached_input_tokens"),
            },
            "ledger equals budget/provider usage",
            _existing_links(run_root, runs_root, ["budget_ledger.jsonl", *proposal_refs]),
        ),
    ]
    if safety:
        candidates_root = run_root / "candidates"
        candidate_dirs = (
            [path.name for path in candidates_root.iterdir() if path.name.startswith("candidate_")]
            if candidates_root.is_dir()
            else []
        )
        started_candidate_tools = [
            event
            for event in ledger_events
            if event.get("state") == "STARTED"
            and event.get("kind") in {"csim", "synth", "cosim"}
            and event.get("candidate_id") not in {None, "candidate_000"}
        ]
        rollback = _as_mapping(result.get("rollback"))
        safety_checks = [
            ("safety_workflow_failed", result.get("status") == "FAILED", result.get("status"), "FAILED"),
            ("safety_reason_patch_invalid", result.get("stop_reason") == "PATCH_INVALID", result.get("stop_reason"), "PATCH_INVALID"),
            ("safety_forbidden_file_detected", any(path != kernel_file for path in patch_files), patch_files, "contains forbidden non-kernel path"),
            ("safety_policy_rejected", "may modify only" in str(result.get("patch_error", "")), result.get("patch_error"), "policy rejection"),
            ("safety_candidate_id_unallocated", result.get("candidate_id") is None, result.get("candidate_id"), None),
            ("safety_candidate_not_materialized", not candidate_dirs, candidate_dirs, []),
            ("safety_registry_clean", set(candidate_map) == {"candidate_000"}, sorted(candidate_map), ["candidate_000"]),
            ("safety_best_final_clean", registry.get("best_candidate_id") in {None, "candidate_000"} and registry.get("final_candidate_id") in {None, "candidate_000"}, {"best": registry.get("best_candidate_id"), "final": registry.get("final_candidate_id")}, "none or candidate_000"),
            ("safety_baseline_unchanged", baseline.get("baseline_unchanged") is True, baseline.get("baseline_unchanged"), True),
            ("safety_manifest_unpolluted", not any(isinstance(entry, Mapping) and entry.get("artifact_type") in {"candidate_source", "candidate_patch", "candidate_metadata"} for entry in entries), "candidate artifacts absent", "candidate artifacts absent"),
            ("safety_no_candidate_tool_charge", not started_candidate_tools, len(started_candidate_tools), 0),
            ("safety_rollback_to_baseline", rollback.get("from") == "candidate_000" and rollback.get("to") == "candidate_000", rollback, {"from": "candidate_000", "to": "candidate_000"}),
        ]
        checks.extend(_check(*value, core_refs) for value in safety_checks)
    else:
        model_ok = (
            len(real_proposals) == 1
            and provider == "openai-compatible"
            and model == "deepseek-v4-pro"
            and fallback_absent
        )
        checks.extend(
            [
                _check(
                    "real_model_no_fallback",
                    model_ok,
                    {"provider": provider, "model": model, "real_calls": len(real_proposals), "fallback": not fallback_absent},
                    {"provider": "openai-compatible", "model": "deepseek-v4-pro", "real_calls": 1, "fallback": False},
                    _existing_links(run_root, runs_root, proposal_refs),
                ),
                _check(
                    "patch_scope_minimal",
                    patch_minimal,
                    {"files": patch_files, "changed_lines": patch_changed, "hunks": hunks},
                    {"files": [kernel_file], "changed_lines": "1..30", "hunks": "1..30"},
                    _existing_links(run_root, runs_root, [candidate.get("patch_ref"), *proposal_refs]),
                ),
                _check(
                    "interface_unchanged",
                    interface_unchanged,
                    _signature(final_source, top),
                    _signature(baseline_source, top),
                    _existing_links(run_root, runs_root, [source_ref, "baseline/source/" + kernel_file]),
                ),
                _check(
                    "final_gates_pass",
                    final_gates,
                    {stage: _as_mapping(final_validation.get(stage)).get("status") for stage in ("csim", "synth", "cosim")} | {"clock": clock.get("passed")},
                    {"csim": "PASS", "synth": "PASS", "cosim": "PASS", "clock": True},
                    _existing_links(run_root, runs_root, [item.get("result_ref") for item in final_validation.values() if isinstance(item, Mapping)]),
                ),
                _check(
                    "candidate_promoted",
                    candidate_promoted,
                    {"candidate": candidate_id, "parent": candidate.get("parent_id"), "status": candidate.get("status"), "best": registry.get("best_candidate_id"), "final": registry.get("final_candidate_id")},
                    "verified candidate_001 promoted to best/final",
                    _existing_links(run_root, runs_root, ["candidate_registry.json"]),
                ),
                _check(
                    "candidate_action_binding",
                    binding_ok,
                    {"candidate_id": candidate_id, "code_hash": candidate.get("code_hash")},
                    "all final actions bind the same candidate/code hash",
                    _existing_links(run_root, runs_root, [item.get("result_ref") for item in final_validation.values() if isinstance(item, Mapping)]),
                ),
            ]
        )
    case_status = "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL"
    return {
        "case_id": case_id,
        "kind": "safety" if safety else "hls",
        "status": case_status,
        "run_dir": run_root.name,
        "error": localization,
        "baseline": {
            "stage": baseline_stage,
            "phase": baseline_phase,
            "real": baseline_real,
        },
        "provider": {
            "provider": provider,
            "model": model,
            "revision": proposal.get("revision"),
            "real_llm_calls": len(real_proposals),
            "fallback_status": "NOT_APPLICABLE" if safety else ("NOT_USED" if fallback_absent else "USED"),
            "input_tokens": _as_int(ledger.get("input_tokens")),
            "output_tokens": _as_int(ledger.get("output_tokens")),
            "cached_input_tokens": _as_int(ledger.get("cached_input_tokens")),
            "total_tokens": _as_int(ledger.get("tokens_used")),
        },
        "patch": {
            "files": patch_files,
            "additions": additions,
            "deletions": deletions,
            "hunks": hunks,
            "change_class": patch_value.get("change_class"),
            "hypothesis": patch_value.get("hypothesis"),
            "expected_effect": patch_value.get("expected_effect"),
            "risk": patch_value.get("risk"),
            "normalization_applied": patch_value.get("normalization_applied") is True,
            **_patch_display(raw_patch),
        },
        "validation": validation_comparison,
        "metrics": dict(metrics),
        "candidate": {
            "baseline_id": registry.get("baseline_candidate_id"),
            "candidate_id": candidate_id,
            "parent_id": candidate.get("parent_id"),
            "status": candidate.get("status"),
            "immutable": candidate.get("immutable"),
            "best_id": registry.get("best_candidate_id"),
            "final_id": registry.get("final_candidate_id"),
            "promoted": candidate_promoted,
            "rollback": result.get("rollback"),
        },
        "budget": {
            "llm_calls": len(real_proposals),
            "csim_calls": _as_int(tool_used.get("csim")),
            "synth_calls": _as_int(tool_used.get("synth")),
            "cosim_calls": _as_int(tool_used.get("cosim")),
            "tool_calls": sum(_as_int(tool_used.get(name)) for name in ("csim", "synth", "cosim")),
            "credits_used": _as_int(budget.get("credits_used")),
            "credit_limit": _as_int(budget.get("credit_limit")),
            "credits_remaining": _as_int(budget.get("credits_remaining")),
            "token_limit": _as_int(budget.get("token_limit")),
            "tokens_remaining": _as_int(budget.get("tokens_remaining")),
        },
        "checks": checks,
        "trace_summary": trace,
        "raw_evidence_links": core_refs,
        "manifest_digest": manifest_digest(run_root),
    }


def build_review_evidence(
    spec_path: str | Path,
    run_dirs: Mapping[str, str | Path],
    acceptance_result: str | Path,
    runs_root: str | Path,
) -> dict[str, object]:
    """Build one canonical review model without writing or invoking tools."""

    root = Path(runs_root).resolve()
    resolved_runs = {key: Path(value).resolve() for key, value in run_dirs.items()}
    for run_root in resolved_runs.values():
        try:
            run_root.relative_to(root)
        except ValueError as exc:
            raise ReviewError("all evidence runs must be inside runs_root") from exc
    acceptance_path = Path(acceptance_result).resolve()
    try:
        acceptance_path.relative_to(root)
    except ValueError as exc:
        raise ReviewError("acceptance_result must be inside runs_root") from exc
    recorded = _read_json(acceptance_path)
    try:
        recomputed = compute_acceptance(spec_path, resolved_runs)
    except AcceptanceError as exc:
        raise ReviewError(str(exc)) from exc
    recorded_cases = {
        str(case.get("case_id")): case
        for case in recorded.get("cases", [])
        if isinstance(case, Mapping)
    }
    recomputed_cases = {
        str(case.get("case_id")): case
        for case in recomputed.get("cases", [])
        if isinstance(case, Mapping)
    }
    cases: list[dict[str, object]] = []
    for case_id in _CASE_ORDER:
        if case_id not in resolved_runs:
            raise ReviewError(f"missing evidence run for {case_id}")
        cases.append(
            _build_case(
                case_id,
                resolved_runs[case_id],
                root,
                _as_mapping(recomputed_cases.get(case_id)),
                _as_mapping(recorded_cases.get(case_id)),
            )
        )
    recorded_consistent = _acceptance_consistent(recorded, recomputed)
    hls_cases = [case for case in cases if case["kind"] == "hls"]
    safety_cases = [case for case in cases if case["kind"] == "safety"]
    summary = {
        "acceptance_cases": len(cases),
        "hls_repairs_passed": sum(case["status"] == "PASS" for case in hls_cases),
        "safety_rejections_passed": sum(case["status"] == "PASS" for case in safety_cases),
        "llm_calls": sum(_as_int(_as_mapping(case["budget"]).get("llm_calls")) for case in cases),
        "input_tokens": sum(_as_int(_as_mapping(case["provider"]).get("input_tokens")) for case in cases),
        "output_tokens": sum(_as_int(_as_mapping(case["provider"]).get("output_tokens")) for case in cases),
        "cached_input_tokens": sum(_as_int(_as_mapping(case["provider"]).get("cached_input_tokens")) for case in cases),
        "total_tokens": sum(_as_int(_as_mapping(case["provider"]).get("total_tokens")) for case in cases),
        "csim_calls": sum(_as_int(_as_mapping(case["budget"]).get("csim_calls")) for case in cases),
        "synth_calls": sum(_as_int(_as_mapping(case["budget"]).get("synth_calls")) for case in cases),
        "cosim_calls": sum(_as_int(_as_mapping(case["budget"]).get("cosim_calls")) for case in cases),
        "tool_calls": sum(_as_int(_as_mapping(case["budget"]).get("tool_calls")) for case in cases),
        "credits_used": sum(_as_int(_as_mapping(case["budget"]).get("credits_used")) for case in cases),
        "credit_limit": sum(_as_int(_as_mapping(case["budget"]).get("credit_limit")) for case in cases),
        "credits_remaining": sum(_as_int(_as_mapping(case["budget"]).get("credits_remaining")) for case in cases),
        "token_limit": sum(_as_int(_as_mapping(case["budget"]).get("token_limit")) for case in cases),
        "tokens_remaining": sum(_as_int(_as_mapping(case["budget"]).get("tokens_remaining")) for case in cases),
        "final_csim_pass": sum(_as_mapping(_as_mapping(case["validation"]).get("csim")).get("accepted") is True for case in hls_cases),
        "final_synth_pass": sum(_as_mapping(_as_mapping(case["validation"]).get("synth")).get("accepted") is True for case in hls_cases),
        "final_cosim_pass": sum(_as_mapping(_as_mapping(case["validation"]).get("cosim")).get("accepted") is True for case in hls_cases),
        "clock_pass": sum(_as_mapping(_as_mapping(case["validation"]).get("clock")).get("accepted") is True for case in hls_cases),
        "hls_case_count": len(hls_cases),
    }
    audit_items = {
        "overall_consistency": recorded_consistent,
        "three_hls_repairs": summary["hls_repairs_passed"] == 3,
        "one_safety_rejection": summary["safety_rejections_passed"] == 1,
        "baseline_errors_real": all(any(check["check_id"] == "baseline_error_real" and check["status"] == "PASS" for check in case["checks"]) for case in cases),
        "real_model_no_fallback": all(any(check["check_id"] == "real_model_no_fallback" and check["status"] == "PASS" for check in case["checks"]) for case in hls_cases),
        "patch_minimal_interface_safe": all(all(any(check["check_id"] == check_id and check["status"] == "PASS" for check in case["checks"]) for check_id in ("patch_scope_minimal", "interface_unchanged")) for case in hls_cases),
        "final_gates_pass": all(any(check["check_id"] == "final_gates_pass" and check["status"] == "PASS" for check in case["checks"]) for case in hls_cases),
        "tokens_tools_complete": all(all(any(check["check_id"] == check_id and check["status"] == "PASS" for check in case["checks"]) for check_id in ("token_usage_complete", "ledger_trace_action_consistent")) for case in cases),
        "candidate_promoted_or_safe_rollback": all(case["status"] == "PASS" for case in cases),
        "raw_evidence_consistent": all(case["status"] == "PASS" for case in cases),
    }
    overall = (
        "PASS"
        if recorded_consistent
        and recomputed.get("overall_status") == "PASS"
        and all(case["status"] == "PASS" for case in cases)
        and all(audit_items.values())
        else "FAIL"
    )
    review: dict[str, object] = {
        "schema_version": 1,
        "overall_status": overall,
        "recorded_overall_status": recorded.get("overall_status"),
        "recomputed_overall_status": recomputed.get("overall_status"),
        "acceptance_consistent": recorded_consistent,
        "evidence_tier": recorded.get("evidence_tier"),
        "acceptance_id": recorded.get("acceptance_id"),
        "evaluator_version": recorded.get("evaluator_version"),
        "acceptance_result_ref": _relative_link(acceptance_path, root),
        "acceptance_result_sha256": _sha256_file(acceptance_path),
        "summary": summary,
        "audit_items": audit_items,
        "cases": cases,
    }
    review["review_data_digest"] = _sha256_bytes(_canonical_json(review))
    return review


_TEXT = {
    "en": {
        "title": "V1 Human Review Acceptance Report",
        "other": "中文报告",
        "dashboard": "Static dashboard",
        "status": "Overall status",
        "recorded": "Recorded machine status",
        "recomputed": "Recomputed evidence status",
        "consistent": "Recorded/recomputed consistency",
        "digest": "Review data digest",
        "machine_hash": "Machine acceptance SHA-256",
        "summary": "Core statistics",
        "metric": "Metric",
        "value": "Value",
        "audit": "Primary audit checklist",
        "source": "Machine decision source",
        "matrix": "Acceptance matrix",
        "case": "Case",
        "kind": "Kind",
        "error": "Error category / subtype",
        "run": "Evidence run",
        "baseline": "Baseline failure and localization",
        "provider": "Provider, model, fallback and Tokens",
        "patch": "Patch scope and unified diff",
        "validation": "Baseline versus Final validation",
        "candidate": "Candidate lifecycle",
        "checks": "Acceptance conditions",
        "trace": "Key Trace transitions",
        "raw": "Raw evidence links",
        "budget": "Per-case Token, tool and budget accounting",
        "statement": "Offline generation statement",
        "statement_text": "This report was aggregated from existing machine evidence. No LLM or Vitis action was invoked, and no machine-readable evidence was modified.",
    },
    "zh": {
        "title": "V1 人工审核统一验收报告",
        "other": "English report",
        "dashboard": "静态 Dashboard",
        "status": "总体状态",
        "recorded": "机器记录状态",
        "recomputed": "证据重新计算状态",
        "consistent": "机器记录与重新计算一致",
        "digest": "人工审核数据摘要",
        "machine_hash": "机器验收 SHA-256",
        "summary": "核心统计",
        "metric": "指标",
        "value": "数值",
        "audit": "人工审核总表",
        "source": "机器判定来源",
        "matrix": "验收矩阵",
        "case": "场景",
        "kind": "类别",
        "error": "错误类别 / subtype",
        "run": "证据运行目录",
        "baseline": "Baseline 失败与错误定位",
        "provider": "Provider、Model、fallback 与 Token",
        "patch": "Patch 范围与完整 unified diff",
        "validation": "Baseline 与 Final 验证对比",
        "candidate": "Candidate 生命周期",
        "checks": "Acceptance 条件逐项结果",
        "trace": "关键 Trace 状态转移",
        "raw": "原始证据相对链接",
        "budget": "各场景 Token、工具与预算明细",
        "statement": "离线生成声明",
        "statement_text": "本报告完全从既有机器证据聚合，没有调用 LLM 或 Vitis，也没有修改任何机器可读证据。",
    },
}

_SUMMARY_NAMES = {
    "acceptance_cases": ("Acceptance cases", "验收场景数"),
    "hls_repairs_passed": ("Real HLS repairs passed", "真实 HLS 修复成功数"),
    "safety_rejections_passed": ("Safety rejections passed", "安全拒绝成功数"),
    "llm_calls": ("Real LLM calls", "LLM 调用总次数"),
    "input_tokens": ("Input Tokens", "输入 Token 总量"),
    "output_tokens": ("Output Tokens", "输出 Token 总量"),
    "cached_input_tokens": ("Cached-input Tokens", "Cached Token 总量"),
    "total_tokens": ("Total Tokens", "Token 总量"),
    "csim_calls": ("CSim calls", "CSim 调用次数"),
    "synth_calls": ("Synth calls", "Synth 调用次数"),
    "cosim_calls": ("CoSim calls", "CoSim 调用次数"),
    "tool_calls": ("HLS tool calls", "工具调用总次数"),
    "credits_used": ("Tool Credits used", "工具 Credits 总消耗"),
    "credit_limit": ("Credit budget", "Credits 总预算"),
    "credits_remaining": ("Credits remaining", "Credits 剩余预算"),
    "token_limit": ("Token budget", "Token 总预算"),
    "tokens_remaining": ("Tokens remaining", "Token 剩余预算"),
    "final_csim_pass": ("Final CSim PASS", "最终 CSim PASS"),
    "final_synth_pass": ("Final Synth PASS", "最终 Synth PASS"),
    "final_cosim_pass": ("Final CoSim PASS", "最终 CoSim PASS"),
    "clock_pass": ("Clock constraint PASS", "时钟约束 PASS"),
}

_AUDIT_NAMES = {
    "overall_consistency": ("Recorded acceptance agrees with recomputed evidence", "机器验收结果与重新计算证据一致"),
    "three_hls_repairs": ("compile / functional / synthesis repair", "compile / functional / synthesis 三类 HLS 修复"),
    "one_safety_rejection": ("patch_invalid safety rejection", "patch_invalid 安全拒绝"),
    "baseline_errors_real": ("Baseline failures are real Vitis evidence", "Baseline 错误具有真实 Vitis 证据"),
    "real_model_no_fallback": ("Real model calls with no fallback", "真实调用模型且无 fallback"),
    "patch_minimal_interface_safe": ("Minimal Patch; no testbench/interface modification", "Patch 最小且未修改 testbench/接口"),
    "final_gates_pass": ("Final CSim/Synth/CoSim/Clock", "修复后 CSim/Synth/CoSim/Clock"),
    "tokens_tools_complete": ("Token and tool accounting complete", "Token 和工具调用记录完整"),
    "candidate_promoted_or_safe_rollback": ("Candidate promoted or safely rolled back", "Candidate 正确提升或安全回滚"),
    "raw_evidence_consistent": ("Manifest/Trace/Ledger/action records agree", "Manifest、Trace、Ledger 与实际调用一致"),
}

_CHECK_NAMES = {
    "manifest_integrity": ("Manifest integrity", "Manifest 完整性"),
    "recorded_acceptance_consistent": ("Recorded acceptance consistency", "机器验收一致性"),
    "deterministic_acceptance": ("Deterministic acceptance", "确定性验收结果"),
    "baseline_error_real": ("Baseline error is real", "Baseline 错误真实"),
    "ledger_trace_action_consistent": ("Ledger/Trace/action consistency", "Ledger/Trace/实际调用一致"),
    "token_usage_complete": ("Token usage complete", "Token 记录完整"),
    "real_model_no_fallback": ("Real model and no fallback", "真实模型且无 fallback"),
    "patch_scope_minimal": ("Minimal allowed Patch scope", "最小且允许的 Patch 范围"),
    "interface_unchanged": ("Top interface unchanged", "顶层接口未修改"),
    "final_gates_pass": ("Final CSim/Synth/CoSim/Clock", "Final CSim/Synth/CoSim/Clock"),
    "candidate_promoted": ("Candidate promoted to best/final", "Candidate 已提升为 best/final"),
    "candidate_action_binding": ("Candidate/action hash binding", "Candidate/action/hash 绑定"),
    "safety_workflow_failed": ("Workflow failed safely", "workflow 安全失败"),
    "safety_reason_patch_invalid": ("Stop reason is PATCH_INVALID", "停止原因为 PATCH_INVALID"),
    "safety_forbidden_file_detected": ("Forbidden file detected", "检测到越权文件"),
    "safety_policy_rejected": ("Patch policy rejected the change", "Patch 策略已拒绝"),
    "safety_candidate_id_unallocated": ("Candidate ID not allocated", "未分配 Candidate ID"),
    "safety_candidate_not_materialized": ("Candidate not materialized", "Candidate 未物化"),
    "safety_registry_clean": ("Registry contains baseline only", "Registry 仅包含 baseline"),
    "safety_best_final_clean": ("Best/final not polluted", "best/final 未污染"),
    "safety_baseline_unchanged": ("Baseline unchanged", "Baseline 未修改"),
    "safety_manifest_unpolluted": ("Manifest has no Candidate artifacts", "Manifest 无 Candidate 产物"),
    "safety_no_candidate_tool_charge": ("No Candidate tool call/charge", "无 Candidate 工具调用或收费"),
    "safety_rollback_to_baseline": ("Safe rollback to baseline", "安全回滚至 baseline"),
}


def _label(table: Mapping[str, tuple[str, str]], key: str, language: str) -> str:
    value = table.get(key, (key.replace("_", " "), key.replace("_", " ")))
    return value[0 if language == "en" else 1]


def _plain(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _md(value: object) -> str:
    return _plain(value).replace("|", "\\|").replace("\n", "<br>")


def _status(value: object) -> str:
    return "✅ PASS" if value is True or value == "PASS" else "❌ FAIL"


def _render_case_markdown(case: Mapping[str, object], language: str) -> list[str]:
    t = _TEXT[language]
    error = _as_mapping(case.get("error"))
    provider = _as_mapping(case.get("provider"))
    patch = _as_mapping(case.get("patch"))
    validation = _as_mapping(case.get("validation"))
    candidate = _as_mapping(case.get("candidate"))
    metrics = _as_mapping(case.get("metrics"))
    lines = [
        f'<a id="{case.get("case_id")}"></a>',
        f"## {case.get('case_id')} — {_status(case.get('status'))}",
        "",
        f"### {t['baseline']}",
        "",
        "| Field | Value |" if language == "en" else "| 字段 | 数值 |",
        "|---|---|",
        f"| Baseline stage / phase | `{_md(_as_mapping(case.get('baseline')).get('stage'))} / {_md(_as_mapping(case.get('baseline')).get('phase'))}` |",
        f"| Error category / subtype | `{_md(error.get('category'))} / {_md(error.get('subtype'))}` |",
        f"| File / line / column | `{_md(error.get('file'))}:{_md(error.get('line'))}:{_md(error.get('column'))}` |",
        f"| Symbol / top | `{_md(error.get('symbol'))}` / `{_md(error.get('top'))}` |",
        f"| Key log | `{_md(error.get('key_log'))}` |",
        "",
        f"### {t['provider']}",
        "",
        "| Provider | Model | Fallback | Real LLM calls | Input | Output | Cached | Total |",
        "|---|---|---|---:|---:|---:|---:|---:|",
        f"| `{_md(provider.get('provider'))}` | `{_md(provider.get('model'))}` | `{_md(provider.get('fallback_status'))}` | {_md(provider.get('real_llm_calls'))} | {_md(provider.get('input_tokens'))} | {_md(provider.get('output_tokens'))} | {_md(provider.get('cached_input_tokens'))} | {_md(provider.get('total_tokens'))} |",
        "",
        f"### {t['patch']}",
        "",
        f"- Files: `{_md(patch.get('files'))}`",
        f"- Additions / deletions / hunks: `{_md(patch.get('additions'))} / {_md(patch.get('deletions'))} / {_md(patch.get('hunks'))}`",
        f"- Change class / risk: `{_md(patch.get('change_class'))} / {_md(patch.get('risk'))}`",
        f"- Normalization applied: `{_md(patch.get('normalization_applied'))}`",
        f"- Hypothesis: {_md(patch.get('hypothesis'))}",
        f"- Expected effect: {_md(patch.get('expected_effect'))}",
        f"- Patch lines: `{_md(patch.get('line_count'))}`; truncated: `{_md(patch.get('truncated'))}`",
        "",
        "```diff",
        str(patch.get("display", "")),
        "```",
        "",
        f"### {t['validation']}",
        "",
        "| Gate | Baseline | Final | Acceptance |",
        "|---|---|---|---|",
    ]
    for stage in ("csim", "synth", "cosim"):
        item = _as_mapping(validation.get(stage))
        lines.append(
            f"| {stage.upper()} | `{_md(item.get('baseline_status'))} / {_md(item.get('baseline_phase'))}` | `{_md(item.get('final_status'))} / {_md(item.get('final_phase'))}` | {_status(item.get('accepted')) if item.get('accepted') is not None else 'N/A'} |"
        )
    clock = _as_mapping(validation.get("clock"))
    clock_final = (
        f"{_md(clock.get('estimated_period_ns'))} ns ≤ {_md(clock.get('target_period_ns'))} ns"
        if clock.get("accepted") is not None
        else "N/A"
    )
    lines.append(
        f"| Clock | `{_md(clock.get('baseline_status'))}` | `{clock_final}` | {_status(clock.get('accepted')) if clock.get('accepted') is not None else 'N/A'} |"
    )
    if metrics:
        lines.extend(
            [
                "",
                f"- Latency: `{_md(metrics.get('latency'))}`",
                f"- II: `{_md(metrics.get('interval'))}`",
                f"- Resources: `{_md(metrics.get('resources'))}`",
                f"- Estimated clock period: `{_md(metrics.get('estimated_clock_period_ns'))} ns`",
            ]
        )
    lines.extend(
        [
            "",
            f"### {t['candidate']}",
            "",
            "| Baseline | Candidate | Parent | Status | Immutable | Best | Final | Promoted | Rollback |",
            "|---|---|---|---|---|---|---|---|---|",
            f"| `{_md(candidate.get('baseline_id'))}` | `{_md(candidate.get('candidate_id'))}` | `{_md(candidate.get('parent_id'))}` | `{_md(candidate.get('status'))}` | `{_md(candidate.get('immutable'))}` | `{_md(candidate.get('best_id'))}` | `{_md(candidate.get('final_id'))}` | `{_md(candidate.get('promoted'))}` | `{_md(candidate.get('rollback'))}` |",
            "",
            f"### {t['checks']}",
            "",
            "| Condition | Status | Observed | Expected |",
            "|---|---|---|---|",
        ]
    )
    for check in case.get("checks", []):
        if not isinstance(check, Mapping):
            continue
        name = _label(_CHECK_NAMES, str(check.get("check_id")), language)
        lines.append(
            f"| {name} | {_status(check.get('status'))} | {_md(check.get('observed'))} | {_md(check.get('expected'))} |"
        )
    lines.extend(["", f"### {t['trace']}", ""])
    lines.append(" → ".join(f"`{_md(item)}`" for item in case.get("trace_summary", [])))
    lines.extend(["", f"### {t['raw']}", ""])
    for link in case.get("raw_evidence_links", []):
        lines.append(f"- [{link}]({link})")
    lines.append("")
    return lines


def render_markdown(review: Mapping[str, object], language: str) -> str:
    """Render one complete English or Chinese single-file review report."""

    if language not in _TEXT:
        raise ReviewError("unsupported report language")
    t = _TEXT[language]
    other = REPORT_CN if language == "en" else REPORT_EN
    summary = _as_mapping(review.get("summary"))
    lines = [
        f"# {t['title']}",
        "",
        f"[{t['other']}]({other}) · [{t['dashboard']}]({DASHBOARD})",
        "",
        f"- **{t['status']}: {_status(review.get('overall_status'))}**",
        f"- {t['recorded']}: `{_md(review.get('recorded_overall_status'))}`",
        f"- {t['recomputed']}: `{_md(review.get('recomputed_overall_status'))}`",
        f"- {t['consistent']}: {_status(review.get('acceptance_consistent'))}",
        f"- {t['digest']}: `{_md(review.get('review_data_digest'))}`",
        f"- {t['machine_hash']}: `{_md(review.get('acceptance_result_sha256'))}`",
        "",
        f"## {t['summary']}",
        "",
        f"| {t['metric']} | {t['value']} |",
        "|---|---:|",
    ]
    for key in _SUMMARY_NAMES:
        value: object = summary.get(key)
        if key in {"final_csim_pass", "final_synth_pass", "final_cosim_pass", "clock_pass"}:
            value = f"{value} / {summary.get('hls_case_count')}"
        lines.append(f"| {_label(_SUMMARY_NAMES, key, language)} | {_md(value)} |")
    lines.extend(["", f"## {t['audit']}", "", f"| {t['metric']} | Status | {t['source']} |", "|---|---|---|"])
    for key, value in _as_mapping(review.get("audit_items")).items():
        lines.append(
            f"| {_label(_AUDIT_NAMES, str(key), language)} | {_status(value)} | `{key}` |"
        )
    lines.extend(["", f"## {t['matrix']}", "", f"| {t['case']} | {t['kind']} | {t['error']} | Status | {t['run']} |", "|---|---|---|---|---|"])
    for case in review.get("cases", []):
        if not isinstance(case, Mapping):
            continue
        error = _as_mapping(case.get("error"))
        lines.append(
            f"| [{case.get('case_id')}](#{case.get('case_id')}) | `{case.get('kind')}` | `{_md(error.get('category'))} / {_md(error.get('subtype'))}` | {_status(case.get('status'))} | `{_md(case.get('run_dir'))}` |"
        )
    lines.extend(["", f"## {t['budget']}", "", "| Case | LLM | Input | Output | Cached | Total | CSim | Synth | CoSim | Tools | Credits | Credit remaining | Token remaining |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for case in review.get("cases", []):
        if not isinstance(case, Mapping):
            continue
        provider = _as_mapping(case.get("provider"))
        budget = _as_mapping(case.get("budget"))
        lines.append(
            f"| {case.get('case_id')} | {budget.get('llm_calls')} | {provider.get('input_tokens')} | {provider.get('output_tokens')} | {provider.get('cached_input_tokens')} | {provider.get('total_tokens')} | {budget.get('csim_calls')} | {budget.get('synth_calls')} | {budget.get('cosim_calls')} | {budget.get('tool_calls')} | {budget.get('credits_used')} | {budget.get('credits_remaining')} | {budget.get('tokens_remaining')} |"
        )
    lines.append(
        f"| **Total** | **{summary.get('llm_calls')}** | **{summary.get('input_tokens')}** | **{summary.get('output_tokens')}** | **{summary.get('cached_input_tokens')}** | **{summary.get('total_tokens')}** | **{summary.get('csim_calls')}** | **{summary.get('synth_calls')}** | **{summary.get('cosim_calls')}** | **{summary.get('tool_calls')}** | **{summary.get('credits_used')}** | **{summary.get('credits_remaining')}** | **{summary.get('tokens_remaining')}** |"
    )
    lines.append("")
    for case in review.get("cases", []):
        if isinstance(case, Mapping):
            lines.extend(_render_case_markdown(case, language))
    lines.extend(
        [
            f"## {t['statement']}",
            "",
            t["statement_text"],
            "",
            "```bash",
            "python3 -m llm4hls_agent review-v1 --runs-root runs",
            "```",
            "",
            f"Machine acceptance: [{review.get('acceptance_result_ref')}]({review.get('acceptance_result_ref')})",
            "",
        ]
    )
    return "\n".join(lines)


def _h(value: object) -> str:
    return html.escape(_plain(value), quote=True)


def _html_status(value: object) -> str:
    passed = value is True or value == "PASS"
    css = "pass" if passed else "fail"
    text = "✓ PASS" if passed else "✕ FAIL"
    return f'<span class="status {css}">{text}</span>'


def _render_html_language(review: Mapping[str, object], language: str) -> str:
    t = _TEXT[language]
    summary = _as_mapping(review.get("summary"))
    card_parts: list[str] = []
    for key in _SUMMARY_NAMES:
        value: object = summary.get(key)
        if key in {"final_csim_pass", "final_synth_pass", "final_cosim_pass", "clock_pass"}:
            value = f"{value} / {summary.get('hls_case_count')}"
        card_parts.append(
            f'<div class="stat"><span>{_h(_label(_SUMMARY_NAMES, key, language))}</span><strong>{_h(value)}</strong></div>'
        )
    cards = "".join(card_parts)
    audit_rows = "".join(
        f"<tr><td>{_h(_label(_AUDIT_NAMES, str(key), language))}</td><td>{_html_status(value)}</td><td><code>{_h(key)}</code></td></tr>"
        for key, value in _as_mapping(review.get("audit_items")).items()
    )
    nav = "".join(
        f'<a href="#{language}-{_h(_as_mapping(case).get("case_id"))}">{_h(_as_mapping(case).get("case_id"))}</a>'
        for case in review.get("cases", [])
    )
    sections: list[str] = []
    for raw_case in review.get("cases", []):
        case = _as_mapping(raw_case)
        error = _as_mapping(case.get("error"))
        provider = _as_mapping(case.get("provider"))
        patch = _as_mapping(case.get("patch"))
        validation = _as_mapping(case.get("validation"))
        candidate = _as_mapping(case.get("candidate"))
        budget = _as_mapping(case.get("budget"))
        checks = "".join(
            f"<tr><td>{_h(_label(_CHECK_NAMES, str(check.get('check_id')), language))}</td><td>{_html_status(check.get('status'))}</td><td><code>{_h(check.get('observed'))}</code></td><td><code>{_h(check.get('expected'))}</code></td></tr>"
            for check in case.get("checks", [])
            if isinstance(check, Mapping)
        )
        validation_rows = ""
        for stage in ("csim", "synth", "cosim"):
            item = _as_mapping(validation.get(stage))
            validation_rows += (
                f"<tr><td>{stage.upper()}</td><td>{_h(item.get('baseline_status'))} / {_h(item.get('baseline_phase'))}</td>"
                f"<td>{_h(item.get('final_status'))} / {_h(item.get('final_phase'))}</td><td>{_html_status(item.get('accepted')) if item.get('accepted') is not None else 'N/A'}</td></tr>"
            )
        clock = _as_mapping(validation.get("clock"))
        clock_final = (
            f"{_h(clock.get('estimated_period_ns'))} ns ≤ {_h(clock.get('target_period_ns'))} ns"
            if clock.get("accepted") is not None
            else "N/A"
        )
        validation_rows += (
            f"<tr><td>Clock</td><td>{_h(clock.get('baseline_status'))}</td>"
            f"<td>{clock_final}</td>"
            f"<td>{_html_status(clock.get('accepted')) if clock.get('accepted') is not None else 'N/A'}</td></tr>"
        )
        trace = "".join(f"<li><code>{_h(item)}</code></li>" for item in case.get("trace_summary", []))
        links = "".join(
            f'<li><a href="{_h(link)}">{_h(link)}</a></li>'
            for link in case.get("raw_evidence_links", [])
        )
        sections.append(
            f'''<section class="case" id="{language}-{_h(case.get('case_id'))}">
<h2>{_h(case.get('case_id'))} {_html_status(case.get('status'))}</h2>
<div class="grid two"><article><h3>{_h(t['baseline'])}</h3>
<dl><dt>Stage / phase</dt><dd>{_h(_as_mapping(case.get('baseline')).get('stage'))} / {_h(_as_mapping(case.get('baseline')).get('phase'))}</dd>
<dt>Error</dt><dd>{_h(error.get('category'))} / {_h(error.get('subtype'))}</dd>
<dt>Location</dt><dd>{_h(error.get('file'))}:{_h(error.get('line'))}:{_h(error.get('column'))}</dd>
<dt>Symbol / top</dt><dd>{_h(error.get('symbol'))} / {_h(error.get('top'))}</dd>
<dt>Key log</dt><dd><code>{_h(error.get('key_log'))}</code></dd></dl></article>
<article><h3>{_h(t['provider'])}</h3>
<dl><dt>Provider / Model</dt><dd>{_h(provider.get('provider'))} / {_h(provider.get('model'))}</dd>
<dt>Fallback</dt><dd>{_h(provider.get('fallback_status'))}</dd><dt>Real LLM calls</dt><dd>{_h(provider.get('real_llm_calls'))}</dd>
<dt>Input / Output / Cached / Total</dt><dd>{_h(provider.get('input_tokens'))} / {_h(provider.get('output_tokens'))} / {_h(provider.get('cached_input_tokens'))} / {_h(provider.get('total_tokens'))}</dd>
<dt>CSim / Synth / CoSim / Tools</dt><dd>{_h(budget.get('csim_calls'))} / {_h(budget.get('synth_calls'))} / {_h(budget.get('cosim_calls'))} / {_h(budget.get('tool_calls'))}</dd>
<dt>Credits used / remaining</dt><dd>{_h(budget.get('credits_used'))} / {_h(budget.get('credits_remaining'))}</dd></dl></article></div>
<h3>{_h(t['patch'])}</h3><p>Files: <code>{_h(patch.get('files'))}</code> · +{_h(patch.get('additions'))} / -{_h(patch.get('deletions'))} · hunks {_h(patch.get('hunks'))} · lines {_h(patch.get('line_count'))}</p>
<pre class="diff"><code>{_h(patch.get('display'))}</code></pre>
<h3>{_h(t['validation'])}</h3><table><thead><tr><th>Gate</th><th>Baseline</th><th>Final</th><th>Acceptance</th></tr></thead><tbody>{validation_rows}</tbody></table>
<h3>{_h(t['candidate'])}</h3><p><code>{_h(candidate.get('baseline_id'))} → {_h(candidate.get('candidate_id'))}</code> · parent {_h(candidate.get('parent_id'))} · status {_h(candidate.get('status'))} · best {_h(candidate.get('best_id'))} · final {_h(candidate.get('final_id'))} · rollback {_h(candidate.get('rollback'))}</p>
<h3>{_h(t['checks'])}</h3><table><thead><tr><th>Condition</th><th>Status</th><th>Observed</th><th>Expected</th></tr></thead><tbody>{checks}</tbody></table>
<h3>{_h(t['trace'])}</h3><ol class="trace">{trace}</ol>
<details><summary>{_h(t['raw'])}</summary><ul>{links}</ul></details></section>'''
        )
    return f'''<main data-lang="{language}"{' hidden' if language == 'zh' else ''}>
<header><p class="eyebrow">V1 · {_h(review.get('evidence_tier'))}</p><h1>{_h(t['title'])}</h1>
<div class="hero-status">{_html_status(review.get('overall_status'))}</div>
<p>{_h(t['recorded'])}: <code>{_h(review.get('recorded_overall_status'))}</code> · {_h(t['recomputed'])}: <code>{_h(review.get('recomputed_overall_status'))}</code></p>
<p class="digest">{_h(t['digest'])}: <code>{_h(review.get('review_data_digest'))}</code></p></header>
<nav class="case-nav">{nav}</nav><section><h2>{_h(t['summary'])}</h2><div class="stats">{cards}</div></section>
<section><h2>{_h(t['audit'])}</h2><table><thead><tr><th>{_h(t['metric'])}</th><th>Status</th><th>{_h(t['source'])}</th></tr></thead><tbody>{audit_rows}</tbody></table></section>
{''.join(sections)}<footer><h2>{_h(t['statement'])}</h2><p>{_h(t['statement_text'])}</p><p><a href="{_h(review.get('acceptance_result_ref'))}">acceptance_result.json</a></p></footer></main>'''


def render_html(review: Mapping[str, object]) -> str:
    """Render a self-contained bilingual static review dashboard."""

    body = _render_html_language(review, "en") + _render_html_language(review, "zh")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V1 Acceptance Dashboard</title><style>
:root{{--ink:#172033;--muted:#5b6475;--line:#d9dee8;--panel:#fff;--bg:#f4f6fa;--pass:#137a50;--pass-bg:#e8f7ef;--fail:#b42318;--fail-bg:#ffebe9;--accent:#3157d5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,sans-serif}}.toolbar{{position:sticky;top:0;z-index:3;display:flex;justify-content:flex-end;gap:.5rem;padding:.75rem 4vw;background:#172033}}button{{border:1px solid #77839b;border-radius:999px;background:transparent;color:#fff;padding:.45rem .9rem;cursor:pointer}}button.active{{background:#fff;color:#172033}}main{{max-width:1180px;margin:auto;padding:2rem 4vw 5rem}}header,.case,section,footer{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:1.4rem;margin:1rem 0;box-shadow:0 5px 18px rgba(23,32,51,.05)}}header{{background:linear-gradient(135deg,#182340,#304f9f);color:#fff}}h1{{font-size:clamp(2rem,5vw,3.8rem);margin:.2rem 0}}h2{{margin-top:0}}h3{{margin-top:1.4rem}}.eyebrow{{text-transform:uppercase;letter-spacing:.16em}}.digest{{overflow-wrap:anywhere}}.status{{display:inline-block;border-radius:999px;padding:.18rem .55rem;font-weight:750}}.status.pass{{color:var(--pass);background:var(--pass-bg)}}.status.fail{{color:var(--fail);background:var(--fail-bg)}}.hero-status .status{{font-size:1.25rem}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.7rem}}.stat{{border:1px solid var(--line);border-radius:12px;padding:.85rem;background:#fafbfe}}.stat span{{display:block;color:var(--muted);font-size:.82rem}}.stat strong{{font-size:1.45rem}}.grid.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1rem}}article{{border:1px solid var(--line);border-radius:12px;padding:1rem}}dt{{font-size:.8rem;color:var(--muted);font-weight:700}}dd{{margin:0 0 .65rem;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse;display:block;overflow-x:auto}}th,td{{border-bottom:1px solid var(--line);padding:.65rem;text-align:left;vertical-align:top}}th{{background:#f6f8fc}}code,pre{{font-family:ui-monospace,SFMono-Regular,monospace}}pre.diff{{background:#121827;color:#e9efff;border-radius:12px;padding:1rem;overflow:auto;max-height:500px}}.case-nav{{display:flex;gap:.5rem;flex-wrap:wrap;margin:1rem 0}}.case-nav a{{background:#fff;border:1px solid var(--line);border-radius:999px;padding:.45rem .8rem;text-decoration:none;color:var(--accent)}}.trace{{display:flex;gap:.5rem;flex-wrap:wrap;padding:0;list-style:none}}.trace li{{background:#eef2ff;border-radius:8px;padding:.4rem .6rem}}a{{color:var(--accent)}}
@media print{{.toolbar{{display:none}}body{{background:#fff}}main{{max-width:none;padding:0}}header,.case,section,footer{{box-shadow:none;break-inside:avoid}}details{{display:block}}}}
</style></head><body><div class="toolbar"><button id="en" class="active" type="button">English</button><button id="zh" type="button">中文</button></div>{body}
<script>for(const lang of ['en','zh']){{document.getElementById(lang).addEventListener('click',()=>{{for(const node of document.querySelectorAll('main[data-lang]'))node.hidden=node.dataset.lang!==lang;for(const button of document.querySelectorAll('button'))button.classList.toggle('active',button.id===lang);document.documentElement.lang=lang==='zh'?'zh-CN':'en';}});}}</script></body></html>'''


def _assert_no_absolute_paths(content: str, roots: Iterable[Path]) -> None:
    forbidden = [str(path.resolve()) for path in roots]
    forbidden.extend(["/home/", "file://"])
    for value in forbidden:
        if value and value in content:
            raise ReviewError(f"human-review output contains forbidden absolute path: {value}")


def generate_review_reports(
    spec_path: str | Path,
    run_dirs: Mapping[str, str | Path],
    acceptance_result: str | Path,
    runs_root: str | Path,
) -> dict[str, object]:
    """Write only the three flat human-review files and preserve machine evidence."""

    root = Path(runs_root).resolve()
    resolved_runs = {key: Path(value).resolve() for key, value in run_dirs.items()}
    acceptance_path = Path(acceptance_result).resolve()
    before = _machine_snapshot(resolved_runs, acceptance_path)
    review = build_review_evidence(spec_path, resolved_runs, acceptance_path, root)
    english = render_markdown(review, "en")
    chinese = render_markdown(review, "zh")
    dashboard = render_html(review)
    for content in (english, chinese, dashboard):
        _assert_no_absolute_paths(content, [root, *resolved_runs.values()])
    _atomic_write(root / REPORT_EN, english)
    _atomic_write(root / REPORT_CN, chinese)
    _atomic_write(root / DASHBOARD, dashboard)
    after = _machine_snapshot(resolved_runs, acceptance_path)
    if before != after:
        raise ReviewError("EVIDENCE_CHANGED_DURING_RENDER")
    return {
        "status": review["overall_status"],
        "acceptance_result_ref": review["acceptance_result_ref"],
        "report_ref": REPORT_EN,
        "report_cn_ref": REPORT_CN,
        "dashboard_ref": DASHBOARD,
        "review_data_digest": review["review_data_digest"],
        "summary": review["summary"],
    }
