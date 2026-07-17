"""Flat bilingual, single-file-first human review for existing V2 evidence."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Mapping

from .v2_acceptance import compute_v2_acceptance


class V2ReviewError(RuntimeError):
    """Raised when human-review evidence is incomplete or inconsistent."""


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2ReviewError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise V2ReviewError(f"{path} is not a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2ReviewError(f"cannot read {path}: {exc}") from exc
    if not all(isinstance(value, dict) for value in values):
        raise V2ReviewError(f"{path} contains a non-object record")
    return values


def _file_snapshot(roots: tuple[Path, ...]) -> dict[str, tuple[int, str]]:
    state: dict[str, tuple[int, str]] = {}
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.is_file():
                data = path.read_bytes()
                state[str(path.resolve())] = (len(data), hashlib.sha256(data).hexdigest())
    return state


def _relative_link(report_root: Path, path: Path, label: str) -> str:
    relative = Path(os.path.relpath(path.resolve(), report_root.resolve())).as_posix()
    return f"[{label}]({relative})"


def _action_report(root: Path, reference: object) -> dict[str, object]:
    if not isinstance(reference, str):
        return {}
    value = _read_json(root / reference)
    report = value.get("report")
    return report if isinstance(report, dict) else {}


def _stage_rows(validation: object) -> list[dict[str, object]]:
    values = validation if isinstance(validation, Mapping) else {}
    rows = []
    for stage in ("csim", "synth", "cosim"):
        item = values.get(stage)
        value = item if isinstance(item, Mapping) else {}
        rows.append(
            {
                "stage": stage,
                "status": value.get("status", "NOT_RUN"),
                "phase": value.get("phase", ""),
                "scope": value.get("validation_scope", ""),
                "result_ref": value.get("result_ref"),
            }
        )
    return rows


def _patch_block(patch: str) -> tuple[int, str, bool]:
    changed = sum(
        1
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    lines = patch.splitlines()
    complete = changed <= 30
    displayed = lines if complete else lines[:30]
    return changed, "\n".join(displayed), complete


def _collect_review_data(
    optimization_root: Path,
    rejection_root: Path,
    acceptance: Mapping[str, object],
) -> dict[str, object]:
    opt_result = _read_json(optimization_root / "v2_result.json")
    opt_registry = _read_json(optimization_root / "candidate_registry.json")
    reject_result = _read_json(rejection_root / "v2_rejection_result.json")
    reject_registry = _read_json(rejection_root / "candidate_registry.json")
    workflow = _read_json(optimization_root / "workflow_result.json")
    run_config = _read_json(optimization_root / "run_config.json")
    candidates_raw = opt_registry.get("candidates")
    candidates = candidates_raw if isinstance(candidates_raw, Mapping) else {}
    baseline = candidates.get("candidate_000")
    baseline_value = baseline if isinstance(baseline, Mapping) else {}
    baseline_metrics = _action_report(
        optimization_root, baseline_value.get("metrics_ref")
    )
    final_id = opt_result.get("final_candidate_id")
    final_raw = candidates.get(final_id)
    final_candidate = final_raw if isinstance(final_raw, Mapping) else {}
    final_metrics = _action_report(
        optimization_root, final_candidate.get("metrics_ref")
    )

    candidate_rows: list[dict[str, object]] = []
    for candidate_id in sorted(candidates):
        raw = candidates[candidate_id]
        candidate = raw if isinstance(raw, Mapping) else {}
        score = (
            _read_json(optimization_root / str(candidate["score_ref"]))
            if isinstance(candidate.get("score_ref"), str)
            else {}
        )
        pre_cosim_score = (
            _read_json(optimization_root / str(candidate["pre_cosim_score_ref"]))
            if isinstance(candidate.get("pre_cosim_score_ref"), str)
            else {}
        )
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "parent_id": candidate.get("parent_id"),
                "kind": candidate.get("kind"),
                "optimization_class": candidate.get("optimization_class"),
                "status": candidate.get("status"),
                "provider": candidate.get("provider"),
                "model": candidate.get("model"),
                "input_tokens": candidate.get("input_tokens", 0),
                "output_tokens": candidate.get("output_tokens", 0),
                "cached_input_tokens": candidate.get("cached_input_tokens", 0),
                "credits_used": candidate.get("credits_used", 0),
                "ppa_cost": (
                    score.get("ppa_cost")
                    if score.get("ppa_cost") is not None
                    else pre_cosim_score.get("ppa_cost")
                ),
                "hard_constraints_passed": (
                    score.get("hard_constraints_passed")
                    if score.get("ppa_cost") is not None
                    else pre_cosim_score.get("hard_constraints_passed")
                ),
            }
        )

    round_rows: list[dict[str, object]] = []
    rounds = opt_result.get("rounds")
    for raw in rounds if isinstance(rounds, list) else []:
        record = raw if isinstance(raw, Mapping) else {}
        provider = (
            _read_json(optimization_root / str(record["provider_ref"]))
            if isinstance(record.get("provider_ref"), str)
            else {}
        )
        request = (
            _read_json(optimization_root / str(record["request_ref"]))
            if isinstance(record.get("request_ref"), str)
            else {}
        )
        provider_request = request.get("provider_request")
        provider_request_value = (
            provider_request if isinstance(provider_request, Mapping) else {}
        )
        http_body = provider_request_value.get("http_body")
        http_body_value = http_body if isinstance(http_body, Mapping) else {}
        messages = http_body_value.get("messages")
        prompt = ""
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, Mapping) and message.get("role") == "user":
                    prompt = str(message.get("content", ""))
                    break
        elif http_body_value:
            prompt = json.dumps(http_body_value, indent=2, sort_keys=True)
        candidate_id = record.get("candidate_id")
        candidate_raw = candidates.get(candidate_id)
        candidate = candidate_raw if isinstance(candidate_raw, Mapping) else {}
        patch = ""
        if isinstance(candidate.get("patch_ref"), str):
            patch = (optimization_root / str(candidate["patch_ref"])).read_text(
                encoding="utf-8"
            )
        changed, patch_display, patch_complete = _patch_block(patch)
        score = (
            _read_json(optimization_root / str(record["score_ref"]))
            if isinstance(record.get("score_ref"), str)
            else {}
        )
        pre_cosim_score = (
            _read_json(optimization_root / str(record["pre_cosim_score_ref"]))
            if isinstance(record.get("pre_cosim_score_ref"), str)
            else {}
        )
        comparison = (
            _read_json(optimization_root / str(record["comparison_ref"]))
            if isinstance(record.get("comparison_ref"), str)
            else {}
        )
        metrics = _action_report(optimization_root, candidate.get("metrics_ref"))
        round_rows.append(
            {
                "round_index": record.get("round_index"),
                "parent_candidate_id": record.get("parent_candidate_id"),
                "optimization_class": record.get("optimization_class"),
                "bottleneck": (
                    record.get("selector", {}).get("bottleneck")
                    if isinstance(record.get("selector"), Mapping)
                    else None
                ),
                "candidate_id": candidate_id,
                "decision": record.get("decision"),
                "provider": provider.get("provider"),
                "model": provider.get("model"),
                "input_tokens": provider.get("input_tokens", 0),
                "output_tokens": provider.get("output_tokens", 0),
                "cached_input_tokens": provider.get("cached_input_tokens", 0),
                "fallback": candidate.get("fallback", "NOT_USED"),
                "sent_files": request.get("sent_files", []),
                "code_ranges": request.get("code_ranges", []),
                "context_mode": request.get("context_mode"),
                "hls_rules": request.get("hls_rules", []),
                "request_prompt": prompt,
                "request_ref": record.get("request_ref"),
                "changed_lines": changed,
                "patch": patch_display,
                "patch_complete": patch_complete,
                "validation": _stage_rows(candidate.get("validation")),
                "clock": candidate.get("clock_constraint", {}),
                "metrics": metrics,
                "score": score,
                "pre_cosim_score": pre_cosim_score,
                "comparison": comparison,
                "cosim_gate": record.get("cosim_gate", {}),
                "result_ref": record.get("result_ref"),
            }
        )

    reject_candidates = reject_registry.get("candidates")
    reject_map = reject_candidates if isinstance(reject_candidates, Mapping) else {}
    rejected = reject_map.get(reject_result.get("rejected_candidate_id"))
    rejected_value = rejected if isinstance(rejected, Mapping) else {}
    safety_patch = reject_result.get("patch")
    safety_patch_value = safety_patch if isinstance(safety_patch, Mapping) else {}
    safety_text = str(safety_patch_value.get("applied_patch", ""))
    safety_changed, safety_display, safety_complete = _patch_block(safety_text)
    trace_counts = Counter(
        str(value.get("event"))
        for root in (optimization_root, rejection_root)
        for value in _read_jsonl(root / "trace.jsonl")
    )
    tool = run_config.get("tool")
    tool_value = tool if isinstance(tool, Mapping) else {}
    return {
        "acceptance": dict(acceptance),
        "task_id": opt_result.get("task_id"),
        "toolchain": tool_value.get("toolchain_id"),
        "part": tool_value.get("part"),
        "clock_ns": tool_value.get("clock_ns"),
        "baseline_validation": _stage_rows(workflow.get("validation")),
        "baseline_clock": workflow.get("clock_constraint", {}),
        "baseline_metrics": baseline_metrics,
        "final_metrics": final_metrics,
        "candidates": candidate_rows,
        "rounds": round_rows,
        "best_candidate_id": opt_result.get("best_candidate_id"),
        "final_candidate_id": opt_result.get("final_candidate_id"),
        "exploration_stop_reason": opt_result.get("exploration_stop_reason"),
        "stop_reason": opt_result.get("stop_reason"),
        "fallback": opt_result.get("fallback"),
        "final_validation": _stage_rows(opt_result.get("final_validation")),
        "final_clock": opt_result.get("final_clock_constraint", {}),
        "safety": {
            "status": reject_result.get("status"),
            "stop_reason": reject_result.get("stop_reason"),
            "rejected_candidate_id": reject_result.get("rejected_candidate_id"),
            "candidate_status": rejected_value.get("status"),
            "best_candidate_id": reject_registry.get("best_candidate_id"),
            "final_candidate_id": reject_registry.get("final_candidate_id"),
            "active_candidate_id": reject_registry.get("active_candidate_id"),
            "validation": _stage_rows(rejected_value.get("validation")),
            "invariants": reject_result.get("safety_invariants", {}),
            "rollback": reject_result.get("rollback"),
            "changed_lines": safety_changed,
            "patch": safety_display,
            "patch_complete": safety_complete,
        },
        "trace_counts": dict(sorted(trace_counts.items())),
    }


def _fmt(value: object) -> str:
    return "N/A" if value is None else str(value)


def _render_report(
    data: Mapping[str, object],
    *,
    chinese: bool,
    report_root: Path,
    optimization_root: Path,
    rejection_root: Path,
    acceptance_path: Path,
) -> str:
    acceptance = data["acceptance"]
    acceptance_value = acceptance if isinstance(acceptance, Mapping) else {}
    summary = acceptance_value.get("summary")
    summary_value = summary if isinstance(summary, Mapping) else {}
    calls = summary_value.get("tool_calls")
    call_value = calls if isinstance(calls, Mapping) else {}
    checks = acceptance_value.get("checks")
    check_value = checks if isinstance(checks, Mapping) else {}
    title = "# V2 人工验收报告" if chinese else "# V2 Human Acceptance Report"
    lines = [
        title,
        "",
        f"- {'总体状态' if chinese else 'Overall status'}: `{acceptance_value.get('overall_status')}`",
        f"- {'证据等级' if chinese else 'Evidence tier'}: `{acceptance_value.get('evidence_tier')}`",
        f"- {'任务' if chinese else 'Task'}: `{data.get('task_id')}`",
        f"- {'工具链' if chinese else 'Toolchain'}: `{data.get('toolchain')}`",
        f"- {'器件 / 目标周期' if chinese else 'Part / target clock'}: `{data.get('part')} / {data.get('clock_ns')} ns`",
        "",
        "## 核心统计" if chinese else "## Core statistics",
        "",
        "| 指标 | 数值 |" if chinese else "| Metric | Value |",
        "|---|---:|",
    ]
    statistics = [
        ("LLM 调用次数" if chinese else "LLM calls", call_value.get("llm", 0)),
        ("输入 Token" if chinese else "Input Tokens", summary_value.get("input_tokens", 0)),
        ("输出 Token" if chinese else "Output Tokens", summary_value.get("output_tokens", 0)),
        ("Cached Token" if chinese else "Cached-input Tokens", summary_value.get("cached_input_tokens", 0)),
        ("总 Token" if chinese else "Total Tokens", summary_value.get("tokens_used", 0)),
        ("CSim 调用" if chinese else "CSim calls", call_value.get("csim", 0)),
        ("Synth 调用" if chinese else "Synth calls", call_value.get("synth", 0)),
        ("CoSim 调用" if chinese else "CoSim calls", call_value.get("cosim", 0)),
        ("工具调用总数" if chinese else "Total tool calls", summary_value.get("tool_call_total", 0)),
        ("Credits 消耗" if chinese else "Credits used", summary_value.get("credits_used", 0)),
        ("剩余预算" if chinese else "Credits remaining", summary_value.get("credits_remaining", 0)),
    ]
    lines.extend(f"| {label} | `{value}` |" for label, value in statistics)
    lines.extend(
        [
            "",
            "### 分场景预算与调用" if chinese else "### Per-scenario accounting",
            "",
            "| Scenario | Input / Output / Cached Tokens | CSim / Synth / CoSim / LLM | Credits used / remaining |",
            "|---|---:|---:|---:|",
        ]
    )
    for name, key in (
        (("优化闭环" if chinese else "Optimization"), "optimization"),
        (("安全拒绝" if chinese else "Safety rejection"), "rejection"),
    ):
        raw_scenario = summary_value.get(key)
        scenario = raw_scenario if isinstance(raw_scenario, Mapping) else {}
        raw_scenario_calls = scenario.get("tool_used")
        scenario_calls = raw_scenario_calls if isinstance(raw_scenario_calls, Mapping) else {}
        lines.append(
            f"| {name} | `{scenario.get('input_tokens_used', 0)} / {scenario.get('output_tokens_used', 0)} / {scenario.get('cached_input_tokens_used', 0)}` | "
            f"`{scenario_calls.get('csim', 0)} / {scenario_calls.get('synth', 0)} / {scenario_calls.get('cosim', 0)} / {scenario_calls.get('llm', 0)}` | "
            f"`{scenario.get('credits_used', 0)} / {scenario.get('credits_remaining', 0)}` |"
        )

    lines.extend(
        [
            "",
            "## 验收条件逐项结果" if chinese else "## Acceptance checks",
            "",
            "| 条件 | 结果 |" if chinese else "| Check | Result |",
            "|---|---|",
        ]
    )
    for name in sorted(check_value):
        lines.append(f"| `{name}` | `{'PASS' if check_value[name] is True else 'FAIL'}` |")

    baseline_metrics = data.get("baseline_metrics")
    baseline_value = baseline_metrics if isinstance(baseline_metrics, Mapping) else {}
    final_metrics = data.get("final_metrics")
    final_metric_value = final_metrics if isinstance(final_metrics, Mapping) else {}
    lines.extend(
        [
            "",
            "## 基线 PPA" if chinese else "## Baseline PPA",
            "",
            f"- {'验证' if chinese else 'Validation'}: `CSim/Synth/CoSim PASS`",
            f"- {'时钟约束' if chinese else 'Clock constraint'}: `{data.get('baseline_clock')}`",
            f"- Latency: `{baseline_value.get('latency')}`",
            f"- II: `{baseline_value.get('interval')}`",
            f"- Resources: `{baseline_value.get('resources')}`",
            f"- Estimated clock: `{baseline_value.get('estimated_clock_period_ns')}` ns",
            "",
            "### 基线与最终 PPA 对比" if chinese else "### Baseline vs Final PPA",
            "",
            "| Metric | Baseline | Final |",
            "|---|---:|---:|",
            f"| Latency worst | `{(baseline_value.get('latency') or {}).get('worst') if isinstance(baseline_value.get('latency'), Mapping) else None}` | `{(final_metric_value.get('latency') or {}).get('worst') if isinstance(final_metric_value.get('latency'), Mapping) else None}` |",
            f"| II max | `{(baseline_value.get('interval') or {}).get('max') if isinstance(baseline_value.get('interval'), Mapping) else None}` | `{(final_metric_value.get('interval') or {}).get('max') if isinstance(final_metric_value.get('interval'), Mapping) else None}` |",
            f"| Estimated clock ns | `{baseline_value.get('estimated_clock_period_ns')}` | `{final_metric_value.get('estimated_clock_period_ns')}` |",
            f"| Resources | `{baseline_value.get('resources')}` | `{final_metric_value.get('resources')}` |",
            "",
            "## 候选树" if chinese else "## Candidate tree",
            "",
            "| Candidate | Parent | Kind | Class | Status | Provider / Model | Tokens in/out/cached | Credits | PPA cost |",
            "|---|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    candidates = data.get("candidates")
    for raw in candidates if isinstance(candidates, list) else []:
        value = raw if isinstance(raw, Mapping) else {}
        lines.append(
            f"| `{value.get('candidate_id')}` | `{_fmt(value.get('parent_id'))}` | "
            f"`{value.get('kind')}` | `{_fmt(value.get('optimization_class'))}` | "
            f"`{value.get('status')}` | `{_fmt(value.get('provider'))} / {_fmt(value.get('model'))}` | "
            f"`{value.get('input_tokens')}/{value.get('output_tokens')}/{value.get('cached_input_tokens')}` | "
            f"`{value.get('credits_used')}` | `{_fmt(value.get('ppa_cost'))}` |"
        )

    rounds = data.get("rounds")
    for raw in rounds if isinstance(rounds, list) else []:
        value = raw if isinstance(raw, Mapping) else {}
        lines.extend(
            [
                "",
                f"### {'第' if chinese else 'Round '} {value.get('round_index')}{'轮' if chinese else ''}: `{value.get('optimization_class')}`",
                "",
                f"- {'父候选' if chinese else 'Parent Candidate'}: `{value.get('parent_candidate_id')}`",
                f"- {'瓶颈' if chinese else 'Bottleneck'}: `{value.get('bottleneck')}`",
                f"- {'Provider / 模型' if chinese else 'Provider / Model'}: `{value.get('provider')} / {value.get('model')}`",
                f"- Tokens input/output/cached: `{value.get('input_tokens')}/{value.get('output_tokens')}/{value.get('cached_input_tokens')}`",
                f"- Fallback: `{value.get('fallback')}`",
                f"- {'发送文件 / 代码范围' if chinese else 'Sent files / code ranges'}: `{value.get('sent_files')} / {value.get('code_ranges')}`",
                f"- {'上下文模式' if chinese else 'Context mode'}: `{value.get('context_mode')}`",
                f"- {'相关 HLS 规则' if chinese else 'Relevant HLS rules'}: `{value.get('hls_rules')}`",
                f"- {'请求证据' if chinese else 'Request evidence'}: `{value.get('request_ref')}`",
                "",
                f"**{'发送给 Provider 的完整用户 Prompt' if chinese else 'Complete user prompt sent to Provider'}**",
                "",
                "```text",
                str(value.get("request_prompt", "")),
                "```",
                f"- {'候选 / 决策' if chinese else 'Candidate / decision'}: `{value.get('candidate_id')} / {value.get('decision')}`",
                f"- {'改动行数' if chinese else 'Changed lines'}: `{value.get('changed_lines')}`",
                f"- {'完整 unified diff' if chinese else 'Full unified diff'}: `{'yes' if value.get('patch_complete') else 'first 30 lines'}`",
                "",
                "```diff",
                str(value.get("patch", "")),
                "```",
                "",
                "| Stage | Status | Phase | Scope |",
                "|---|---|---|---|",
            ]
        )
        validation = value.get("validation")
        for stage in validation if isinstance(validation, list) else []:
            item = stage if isinstance(stage, Mapping) else {}
            lines.append(
                f"| {item.get('stage')} | `{item.get('status')}` | `{item.get('phase')}` | `{item.get('scope')}` |"
            )
        metrics = value.get("metrics")
        metric_value = metrics if isinstance(metrics, Mapping) else {}
        score = value.get("score")
        score_value = score if isinstance(score, Mapping) else {}
        pre_cosim_score = value.get("pre_cosim_score")
        pre_cosim_score_value = (
            pre_cosim_score if isinstance(pre_cosim_score, Mapping) else {}
        )
        comparison = value.get("comparison")
        comparison_value = comparison if isinstance(comparison, Mapping) else {}
        cosim_gate = value.get("cosim_gate")
        cosim_gate_value = cosim_gate if isinstance(cosim_gate, Mapping) else {}
        lines.extend(
            [
                "",
                f"- Latency / II: `{metric_value.get('latency')} / {metric_value.get('interval')}`",
                f"- Resources / clock: `{metric_value.get('resources')} / {metric_value.get('estimated_clock_period_ns')} ns`",
                f"- PPA cost / hard constraints: `{score_value.get('ppa_cost')} / {score_value.get('hard_constraints_passed')}`",
                f"- {'探索 CoSim 门控' if chinese else 'Exploration CoSim gate'}: eligible `{cosim_gate_value.get('eligible')}`, reason `{cosim_gate_value.get('reason')}`, Candidate/Best PPA `{pre_cosim_score_value.get('ppa_cost')} / {cosim_gate_value.get('incumbent_ppa_cost')}`",
                f"- {'比较结果' if chinese else 'Comparison'}: winner `{comparison_value.get('winner')}`, reason `{comparison_value.get('reason')}`",
            ]
        )

    lines.extend(
        [
            "",
            "## 最终 Vitis 验证" if chinese else "## Final Vitis validation",
            "",
            f"- Best / Final: `{data.get('best_candidate_id')} / {data.get('final_candidate_id')}`",
            f"- Exploration / final stop: `{data.get('exploration_stop_reason')} / {data.get('stop_reason')}`",
            f"- Fallback: `{data.get('fallback')}`",
            "",
            "| Stage | Status | Phase | Scope |",
            "|---|---|---|---|",
        ]
    )
    final_validation = data.get("final_validation")
    for raw in final_validation if isinstance(final_validation, list) else []:
        value = raw if isinstance(raw, Mapping) else {}
        lines.append(
            f"| {value.get('stage')} | `{value.get('status')}` | `{value.get('phase')}` | `{value.get('scope')}` |"
        )
    lines.append(f"- Clock: `{data.get('final_clock')}`")

    safety = data.get("safety")
    safety_value = safety if isinstance(safety, Mapping) else {}
    lines.extend(
        [
            "",
            "## 安全拒绝" if chinese else "## Safety rejection",
            "",
            f"- Status / stop: `{safety_value.get('status')} / {safety_value.get('stop_reason')}`",
            f"- Rejected Candidate / status: `{safety_value.get('rejected_candidate_id')} / {safety_value.get('candidate_status')}`",
            f"- Best / Final / Active: `{safety_value.get('best_candidate_id')} / {safety_value.get('final_candidate_id')} / {safety_value.get('active_candidate_id')}`",
            f"- Rollback: `{safety_value.get('rollback')}`",
            "",
            "| Safety invariant | Result |",
            "|---|---|",
        ]
    )
    invariants = safety_value.get("invariants")
    for name, passed in sorted(invariants.items()) if isinstance(invariants, Mapping) else []:
        lines.append(f"| `{name}` | `{'PASS' if passed is True else 'FAIL'}` |")
    lines.extend(
        [
            "",
            f"- {'完整安全 Patch' if chinese else 'Full safety Patch'}: `{'yes' if safety_value.get('patch_complete') else 'first 30 lines'}`",
            "",
            "```diff",
            str(safety_value.get("patch", "")),
            "```",
            "",
            "## Ledger / Trace / action 一致性" if chinese else "## Ledger / Trace / action consistency",
            "",
            f"- Acceptance check: `{'PASS' if check_value.get('ledger_trace_actions_consistent') is True else 'FAIL'}`",
            f"- Trace transitions: `{data.get('trace_counts')}`",
            "",
            "## 原始证据索引" if chinese else "## Raw evidence index",
            "",
        ]
    )
    raw_links = [
        _relative_link(report_root, acceptance_path, "acceptance_result.json"),
        _relative_link(report_root, optimization_root / "v2_result.json", "optimization result"),
        _relative_link(report_root, optimization_root / "artifact_manifest.json", "optimization Manifest"),
        _relative_link(report_root, optimization_root / "candidate_registry.json", "Candidate Registry"),
        _relative_link(report_root, optimization_root / "budget_ledger.jsonl", "optimization Ledger"),
        _relative_link(report_root, optimization_root / "trace.jsonl", "optimization Trace"),
        _relative_link(report_root, rejection_root / "v2_rejection_result.json", "safety result"),
        _relative_link(report_root, rejection_root / "artifact_manifest.json", "safety Manifest"),
        _relative_link(report_root, rejection_root / "budget_ledger.jsonl", "safety Ledger"),
        _relative_link(report_root, rejection_root / "trace.jsonl", "safety Trace"),
    ]
    lines.extend(f"- {link}" for link in raw_links)
    lines.extend(
        [
            "",
            (
                "本报告由机器证据自动聚合；大型 stdout、stderr、XML、Trace 和 Ledger 未原样嵌入。"
                if chinese
                else "This report is generated from machine evidence; large stdout, stderr, XML, Trace, and Ledger files are linked rather than embedded."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def generate_v2_review_reports(
    spec_path: str | Path,
    optimization_run: str | Path,
    rejection_run: str | Path,
    acceptance_result: str | Path,
    runs_root: str | Path,
) -> dict[str, object]:
    """Generate two flat Markdown reports without invoking LLM or Vitis."""

    spec = Path(spec_path).resolve()
    optimization_root = Path(optimization_run).resolve()
    rejection_root = Path(rejection_run).resolve()
    acceptance_path = Path(acceptance_result).resolve()
    output_root = Path(runs_root).resolve()
    evidence_roots = (optimization_root, rejection_root, acceptance_path, spec)
    before = _file_snapshot(evidence_roots)
    recorded = _read_json(acceptance_path)
    recomputed = compute_v2_acceptance(spec, optimization_root, rejection_root)
    if recorded != recomputed:
        raise V2ReviewError(
            "recorded acceptance_result.json disagrees with recomputed V2 acceptance"
        )
    data = _collect_review_data(optimization_root, rejection_root, recomputed)
    english = _render_report(
        data,
        chinese=False,
        report_root=output_root,
        optimization_root=optimization_root,
        rejection_root=rejection_root,
        acceptance_path=acceptance_path,
    )
    chinese = _render_report(
        data,
        chinese=True,
        report_root=output_root,
        optimization_root=optimization_root,
        rejection_root=rejection_root,
        acceptance_path=acceptance_path,
    )
    english_path = output_root / "V2_ACCEPTANCE_REPORT.md"
    chinese_path = output_root / "V2_ACCEPTANCE_REPORT_CN.md"
    _atomic_text(english_path, english)
    _atomic_text(chinese_path, chinese)
    if _file_snapshot(evidence_roots) != before:
        raise V2ReviewError("review generation mutated machine evidence")
    digest = hashlib.sha256((english + chinese).encode("utf-8")).hexdigest()
    return {
        "status": recomputed.get("overall_status"),
        "report_ref": english_path.name,
        "report_cn_ref": chinese_path.name,
        "review_data_digest": digest,
        "summary": recomputed.get("summary", {}),
    }
