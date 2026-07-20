from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .qa import validate_real_vitis_evidence, validate_run_provenance


PASS = "PASS"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def create_benchmark_snapshot(*, source_path: Path, output_path: Path) -> Path:
    """Write a path-free, evidence-preserving benchmark summary snapshot."""
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, dict):
        raise ValueError("benchmark summary must be a JSON object")
    configuration = source.get("configuration", {})
    if not isinstance(configuration, dict):
        configuration = {}
    snapshot = {
        "schema_version": "v3d.submission-benchmark-snapshot.v1",
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "benchmark_fingerprint": source.get("benchmark_fingerprint"),
        "generated_at": source.get("generated_at"),
        "configuration": {
            key: configuration.get(key)
            for key in (
                "backend", "models", "repeats", "splits", "task_filters",
                "mode_filters", "difficulty_filters", "max_tasks",
            )
        },
        "selection": source.get("selection", {}),
        "execution": source.get("execution", {}),
        "evidence_policy": source.get("evidence_policy", {}),
        "real_evidence_headline": source.get("real_evidence_headline", {}),
        "populations": source.get("populations", {}),
        "all_attempt_population": source.get("all_attempt_population", {}),
        "latest_slot_population": source.get("latest_slot_population", {}),
        "by_evidence_class": source.get("by_evidence_class", {}),
        "by_expected_mode": source.get("by_expected_mode", {}),
        "by_mode": source.get("by_mode", {}),
        "by_model": source.get("by_model", {}),
        "implementation_fingerprint": source.get("implementation_fingerprint"),
        "execution_policy_fingerprint": source.get("execution_policy_fingerprint"),
        "run_ids": source.get("run_ids", []),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def create_oracle_snapshot(*, source_path: Path, output_path: Path) -> Path:
    """Write a path-free Oracle summary while preserving evidence authority."""
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, dict):
        raise ValueError("Oracle summary must be a JSON object")
    backend = source.get("backend", {})
    corpus = source.get("corpus", {})
    execution = source.get("execution", {})
    snapshot = {
        "schema_version": "v3d.submission-oracle-snapshot.v1",
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "generated_at": source.get("generated_at"),
        "runner_version": source.get("runner_version"),
        "status": source.get("status"),
        "oracle_pass": source.get("oracle_pass"),
        "backend": {
            key: backend.get(key)
            for key in ("name", "fingerprint", "evidence_class", "real_anchor_authorized", "disclaimer")
        } if isinstance(backend, dict) else {},
        "corpus": {
            key: corpus.get(key)
            for key in ("manifest_sha256", "schema_version", "selected_tasks")
        } if isinstance(corpus, dict) else {},
        "counts": source.get("counts", {}),
        "execution": {
            key: execution.get(key)
            for key in (
                "attempted_this_run", "elapsed_seconds", "resumed_tasks", "serial",
                "state_counts", "stopped_reason",
            )
        } if isinstance(execution, dict) else {},
        "real_anchor": source.get("real_anchor", {}),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _read_ref(root: Path, ref: object) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref:
        return {}
    path = root / ref
    return _load_json(path) if path.is_file() else {}


def _latency_worst(root: Path, result: dict[str, Any], key: str) -> float | None:
    report = _read_ref(root, result.get(key)).get("report", {})
    latency = report.get("latency", {}) if isinstance(report, dict) else {}
    value = latency.get("worst") if isinstance(latency, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _all_final_pass(result: dict[str, Any]) -> bool:
    validation = result.get("final_validation", {})
    if not isinstance(validation, dict):
        return False
    return all(
        isinstance(validation.get(tool), dict)
        and validation[tool].get("status") == PASS
        and validation[tool].get("validation_scope") == "final"
        and validation[tool].get("cached") is False
        for tool in ("csim", "synth", "cosim")
    )


def _proposal_objects(root: Path) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for subdir in ("planner/live_outcomes", "planner/outputs"):
        for path in sorted((root / subdir).glob("*.json")):
            try:
                value = _load_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            proposal = value.get("proposal")
            if isinstance(proposal, dict):
                objects.append(proposal)
    # Outputs and live_outcomes may contain the same proposal.  De-duplicate by
    # stable fields without exposing Patch contents in the submission tables.
    unique: dict[tuple[object, ...], dict[str, Any]] = {}
    for proposal in objects:
        key = (
            proposal.get("model"),
            proposal.get("hypothesis"),
            proposal.get("input_tokens"),
            proposal.get("output_tokens"),
        )
        unique[key] = proposal
    return list(unique.values())


def _failure_stage(result: dict[str, Any]) -> str:
    if result.get("status") == "DONE" and _all_final_pass(result):
        return "NONE"
    gate = result.get("cosim_gate", {})
    if isinstance(gate, dict) and gate.get("reason"):
        return str(gate["reason"])
    phase = result.get("phase_decision", {})
    if isinstance(phase, dict) and phase.get("reason"):
        return str(phase["reason"])
    return str(result.get("stop_reason") or result.get("last_tool_phase") or "UNKNOWN")


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task: str
    mode: str
    provider_class: str
    evidence_role: str
    model: str
    status: str
    final_pass: bool
    evidence_level: str
    validation_profile: str
    baseline_latency: float | None
    final_latency: float | None
    acceleration: float | None
    tokens: int
    credits: int
    llm_calls: int
    csim_calls: int
    synth_calls: int
    cosim_calls: int
    wall_time_s: float
    failure_stage: str
    stop_reason: str
    hypothesis: str
    note: str

    @property
    def real_llm_vitis_e2e(self) -> bool:
        return (
            self.provider_class == "REAL_LLM"
            and self.evidence_level == "REAL_VITIS_VALIDATED"
            and self.final_pass
        )

    @property
    def replay_vitis_e2e(self) -> bool:
        return (
            self.provider_class == "SCRIPTED_PATCH_REPLAY"
            and self.evidence_level == "REAL_VITIS_VALIDATED"
            and self.final_pass
        )


def _record(runs_root: Path, spec: dict[str, Any]) -> RunRecord:
    run_id = str(spec["run_id"])
    root = runs_root / run_id
    result_path = root / "v3_prototype_result.json"
    if not result_path.is_file():
        raise FileNotFoundError(f"curated run is missing result: {result_path}")
    result = _load_json(result_path)
    budget = result.get("budget", {})
    tools = budget.get("tool_used", {}) if isinstance(budget, dict) else {}
    proposals = _proposal_objects(root)
    models = sorted({str(item.get("model")) for item in proposals if item.get("model")})
    llm_calls = int(tools.get("llm", 0) or 0)
    provider_class = str(spec["provider_class"])
    provenance_findings = validate_run_provenance(
        run_root=root,
        result=result,
        provider_class=provider_class,
    )
    if provenance_findings:
        details = "; ".join(
            f"{finding.code}: {finding.detail}" for finding in provenance_findings
        )
        raise ValueError(f"{run_id}: REAL_LLM requires valid live provenance: {details}")
    if provider_class in {"SCRIPTED_PATCH_REPLAY", "DETERMINISTIC"} and llm_calls:
        raise ValueError(f"{run_id}: {provider_class} cannot contain recorded LLM calls")
    final_pass = _all_final_pass(result)
    evidence_level = str((result.get("backend") or {}).get("evidence_level") or "UNKNOWN")
    if provider_class == "REAL_LLM" and evidence_level == "REAL_VITIS_VALIDATED" and final_pass:
        vitis_findings = validate_real_vitis_evidence(
            run_root=root,
            result=result,
            require_live_planner_candidate=True,
        )
        if vitis_findings:
            details = "; ".join(
                f"{finding.code}: {finding.detail}" for finding in vitis_findings
            )
            raise ValueError(f"{run_id}: REAL_LLM + REAL_VITIS evidence is invalid: {details}")
    baseline = _latency_worst(root, result, "baseline_metrics_ref")
    final = _latency_worst(root, result, "final_metrics_ref")
    acceleration = baseline / final if baseline is not None and final not in (None, 0) else None
    hypothesis = ""
    if proposals:
        hypothesis = str(proposals[0].get("hypothesis") or "")
    return RunRecord(
        run_id=run_id,
        task=str(result.get("task_id") or "UNKNOWN"),
        mode=str(result.get("mode") or spec.get("mode") or "OPTIMIZE"),
        provider_class=provider_class,
        evidence_role=str(spec.get("evidence_role") or "supporting"),
        model=", ".join(models) if models else "N/A",
        status=str(result.get("status") or "UNKNOWN"),
        final_pass=final_pass,
        evidence_level=evidence_level,
        validation_profile=str(result.get("validation_profile") or "UNKNOWN"),
        baseline_latency=baseline,
        final_latency=final,
        acceleration=acceleration,
        tokens=int(budget.get("tokens_used", 0) or 0),
        credits=int(budget.get("credits_used", 0) or 0),
        llm_calls=llm_calls,
        csim_calls=int(tools.get("csim", 0) or 0),
        synth_calls=int(tools.get("synth", 0) or 0),
        cosim_calls=int(tools.get("cosim", 0) or 0),
        wall_time_s=float(budget.get("runtime_used_seconds", 0.0) or 0.0),
        failure_stage=_failure_stage(result),
        stop_reason=str(result.get("stop_reason") or "UNKNOWN"),
        hypothesis=hypothesis,
        note=str(spec.get("note") or ""),
    )


def _fmt_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.{digits}f}"


def _fmt_rate(value: object) -> str:
    return f"{100.0 * float(value):.1f}%" if isinstance(value, (int, float)) else "—"


def _table(headers: Iterable[str], rows: Iterable[Iterable[object]]) -> str:
    head = [str(value) for value in headers]
    output = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    for row in rows:
        cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        output.append("| " + " | ".join(cells) + " |")
    return "\n".join(output)


def _front_matter(manifest_name: str) -> str:
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return (
        f"> 自动生成于 {generated}，事实来源：`{manifest_name}` 指定的 run JSON。\n"
        "> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。\n"
    )


def _benchmark_rows(
    benchmarks: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]], list[tuple[object, ...]]]:
    headline_rows: list[tuple[object, ...]] = []
    mode_rows: list[tuple[object, ...]] = []
    deterministic_rows: list[tuple[object, ...]] = []
    for source, summary in benchmarks:
        selection = summary.get("selection", {})
        execution = summary.get("execution", {})
        real = summary.get("real_evidence_headline", {})
        if not all(isinstance(item, dict) for item in (selection, execution, real)):
            continue
        headline_rows.append(
            (
                source,
                str(summary.get("benchmark_fingerprint") or "UNKNOWN")[:16],
                selection.get("tasks_selected", 0),
                execution.get("records", 0),
                real.get("runs", 0),
                _fmt_rate(real.get("e2e_success_rate")),
                _fmt_rate(real.get("fresh_final_success_rate")),
            )
        )
        by_evidence = summary.get("by_evidence_class", {})
        deterministic = by_evidence.get("DETERMINISTIC", {}) if isinstance(by_evidence, dict) else {}
        execution = summary.get("execution", {})
        if isinstance(deterministic, dict) and deterministic.get("runs"):
            usage = deterministic.get("usage", {})
            calls = usage.get("tool_calls", {}) if isinstance(usage, dict) else {}
            router = deterministic.get("router", {})
            optimize = (summary.get("by_mode") or {}).get("OPTIMIZE", {})
            acceleration = optimize.get("acceleration_vs_baseline", {}) if isinstance(optimize, dict) else {}
            deterministic_rows.append(
                (
                    source,
                    deterministic.get("runs", 0),
                    _fmt_rate((router or {}).get("accuracy")),
                    _fmt_rate(deterministic.get("e2e_success_rate")),
                    _fmt_rate(deterministic.get("fresh_final_success_rate")),
                    f"{calls.get('csim', 0)}/{calls.get('synth', 0)}/{calls.get('cosim', 0)}/{calls.get('llm', 0)}",
                    acceleration.get("count", 0) if isinstance(acceleration, dict) else 0,
                    _fmt_number(acceleration.get("mean") if isinstance(acceleration, dict) else None, 6),
                    execution.get("resumed_runs", 0) if isinstance(execution, dict) else 0,
                )
            )
        by_mode = summary.get("by_mode", {})
        if isinstance(by_mode, dict):
            for mode, block in sorted(by_mode.items()):
                if not isinstance(block, dict) or not block.get("runs"):
                    continue
                mode_rows.append(
                    (
                        source,
                        mode,
                        block.get("runs", 0),
                        block.get("e2e_successes", 0),
                        _fmt_rate(block.get("e2e_success_rate")),
                        json.dumps((block.get("failures") or {}).get("by_stage", {}), sort_keys=True),
                    )
                )
    return headline_rows, mode_rows, deterministic_rows


def _oracle_tables(
    oracle: tuple[str, dict[str, Any]] | None,
    anchors: tuple[str, dict[str, Any]] | None,
) -> tuple[str, str, str]:
    if oracle is None:
        oracle_summary = "TODO：缺少 Oracle summary。"
    else:
        source, payload = oracle
        counts = payload.get("counts", {})
        execution = payload.get("execution", {})
        backend = payload.get("backend", {})
        oracle_summary = (
            f"`{source}`：accepted={counts.get('accepted', 0)}，rejected={counts.get('rejected', 0)}，"
            f"pending={counts.get('pending', 0)}，real anchors={counts.get('real_vitis_anchors', 0)}，"
            f"resume={execution.get('resumed_tasks', 0)}；backend={backend.get('evidence_class', 'UNKNOWN')}（fixture only）。"
        )
    anchor_rows: list[tuple[object, ...]] = []
    invalidated_rows: list[tuple[object, ...]] = []
    if anchors is not None:
        source, payload = anchors
        corpus = payload.get("corpus", {})
        deterministic = payload.get("deterministic_oracle", {})
        if not isinstance(deterministic, dict):
            deterministic = {}
        deterministic_checks = deterministic.get(
            "checks_total", corpus.get("deterministic_checks", "—")
        )
        deterministic_accepted = deterministic.get(
            "accepted", corpus.get("deterministic_accepted", "—")
        )
        deterministic_rejected = deterministic.get(
            "rejected", corpus.get("deterministic_rejected", "—")
        )
        oracle_summary += (
            f" Release `{source}` 记录 deterministic checks={deterministic_checks}，"
            f"accepted/rejected={deterministic_accepted}/{deterministic_rejected}。"
        )
        receipt_bundle = payload.get("receipt_bundle", {})
        if isinstance(receipt_bundle, dict) and receipt_bundle:
            oracle_summary += (
                " 可提交 receipt bundle 绑定 "
                f"{receipt_bundle.get('receipts', 0)} 题、"
                f"{receipt_bundle.get('artifact_hashes', 0)} 个 artifact hashes；"
                "它是 REAL_VITIS_NO_LLM 证据，不是 Agent 成绩。"
            )
        for item in payload.get("valid_real_vitis_anchors", []):
            if not isinstance(item, dict):
                continue
            checks_value = item.get("checks", [])
            checks_count = (
                checks_value
                if isinstance(checks_value, int) and not isinstance(checks_value, bool)
                else len(checks_value) if isinstance(checks_value, list) else 0
            )
            anchor_rows.append(
                (
                    item.get("mode", "UNKNOWN"),
                    item.get("task_id", "UNKNOWN"),
                    item.get("run_id", "UNKNOWN"),
                    item.get("status", "ACCEPTED"),
                    checks_count,
                    _fmt_number(item.get("baseline_latency_cycles")),
                    _fmt_number(item.get("golden_latency_cycles")),
                    _fmt_number(item.get("acceleration"), 3),
                    _fmt_number(item.get("wall_time_seconds"), 3),
                )
            )
        historical = payload.get(
            "historical_rejections_preserved",
            payload.get("invalidated_historical_rows", []),
        )
        for item in historical:
            if isinstance(item, dict):
                invalidated_rows.append((item.get("run_id"), item.get("task_id"), item.get("reason")))
    anchor_table = _table(
        ("Mode", "Task", "Run ID", "Status", "Checks", "Baseline", "Golden", "Acceleration", "Wall (s)"),
        anchor_rows or [("TODO", "—", "—", "—", "—", "—", "—", "—", "—")],
    )
    invalidated_table = _table(
        ("Run ID", "Task", "Reason"),
        invalidated_rows or [("—", "—", "无已知降级记录")],
    )
    return oracle_summary, anchor_table, invalidated_table


def _experiment_tables(
    records: list[RunRecord],
    manifest_name: str,
    blockers: dict[str, str],
    benchmarks: list[tuple[str, dict[str, Any]]],
    oracle: tuple[str, dict[str, Any]] | None,
    anchors: tuple[str, dict[str, Any]] | None,
) -> str:
    capability_rows = []
    for mode in ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"):
        subset = [item for item in records if item.mode == mode]
        real_e2e = any(item.real_llm_vitis_e2e for item in subset)
        replay = any(item.replay_vitis_e2e for item in subset)
        state = "真实 LLM + Vitis 闭环" if real_e2e else ("真实 Vitis replay 闭环；真实 LLM 待补" if replay else "仅失败证据/待补")
        capability_rows.append((mode, state, ", ".join(item.run_id for item in subset) or "TODO"))

    run_rows = [
        (
            item.model,
            item.task,
            item.mode,
            item.provider_class,
            item.status,
            "PASS" if item.final_pass else "FAIL/NOT RUN",
            item.run_id,
        )
        for item in records
    ]
    cost_rows = [
        (
            item.run_id,
            item.tokens,
            item.credits,
            item.llm_calls,
            item.csim_calls,
            item.synth_calls,
            item.cosim_calls,
            f"{item.wall_time_s:.2f}",
        )
        for item in records
    ]
    perf_rows = [
        (
            item.run_id,
            _fmt_number(item.baseline_latency),
            _fmt_number(item.final_latency),
            _fmt_number(item.acceleration, 3),
            item.validation_profile,
            item.evidence_level,
        )
        for item in records
        if item.baseline_latency is not None or item.final_latency is not None
    ]
    success_rows = []
    for provider in sorted({item.provider_class for item in records}):
        for mode in sorted({item.mode for item in records if item.provider_class == provider}):
            group = [item for item in records if item.provider_class == provider and item.mode == mode]
            if provider == "REAL_LLM":
                passed = sum(item.real_llm_vitis_e2e for item in group)
            elif provider == "SCRIPTED_PATCH_REPLAY":
                passed = sum(item.replay_vitis_e2e for item in group)
            else:
                passed = sum(item.final_pass for item in group)
            success_rows.append((provider, mode, len(group), passed, len(group) - passed, f"{passed / len(group):.1%}"))
    benchmark_rows, benchmark_mode_rows, deterministic_rows = _benchmark_rows(benchmarks)
    oracle_summary, anchor_table, invalidated_table = _oracle_tables(oracle, anchors)
    evidence_notes = "\n".join(
        f"- `{item.run_id}` [{item.evidence_role}]：{item.note}"
        for item in records
        if item.note
    ) or "- TODO：没有 evidence note。"

    return f"""# V3-D 实验表（事实快照）

{_front_matter(manifest_name)}
## 当前能力矩阵

{_table(("Mode", "当前证据", "Run IDs"), capability_rows)}

## 模型 × 任务运行

{_table(("Model", "Task", "Mode", "Patch provider", "Run status", "Fresh final", "Run ID"), run_rows)}

说明：`SCRIPTED_PATCH_REPLAY` 只证明图、Patch 应用和真实 Vitis 闭环，不证明模型能够自主修复。

### Evidence notes

{evidence_notes}

## 分组成功率

{_table(("Patch provider", "Mode", "Runs", "Success", "Failure", "Success rate"), success_rows)}

## Token、Credit 与工具次数

{_table(("Run ID", "Tokens", "Credits", "LLM", "CSim", "Synth", "CoSim", "Wall time (s)"), cost_rows)}

## Latency 与 acceleration

{_table(("Run ID", "Baseline cycles", "Final cycles", "Acceleration", "Profile", "Evidence"), perf_rows or [("TODO", "—", "—", "—", "—", "—")])}

## strict / fast-experiment

- fast-experiment：本表已有事实见上表。
- strict：**TODO**。需要在相同任务、模型、预算和 Prompt 下新增 strict run，当前不能比较。

## 消融入口

- 无结构化 Evidence：**TODO**。需要固定模型、任务、随机性和预算，关闭 Evidence 后重复运行。
- 无 CoSim risk gate：**TODO**。需要固定其他配置并记录 CoSim 次数、Credit 与最终通过率。

## Batch benchmark（真实与 deterministic 不混算）

{_table(("Summary", "Fingerprint", "Tasks", "Records", "Real runs", "Real E2E rate", "Real fresh-final rate"), benchmark_rows or [("TODO", "—", "—", "—", "—", "—", "—")])}

{_table(("Summary", "Mode", "Runs", "Success", "Success rate", "Failure stages"), benchmark_mode_rows or [("TODO", "—", "—", "—", "—", "需要显式把 benchmark_summary.json 加入 evidence manifest")])}

### Deterministic full-corpus 明细（只验证编排）

{_table(("Summary", "Runs", "Router", "E2E", "Fresh final", "C/S/Co/L", "Optimize N", "Optimize accel mean", "Resumed"), deterministic_rows or [("TODO", "—", "—", "—", "—", "—", "—", "—", "—")])}

## Corpus Oracle 与真实 Vitis anchors

{oracle_summary}

{anchor_table}

### 已降级的历史 anchor

{invalidated_table}

## 模型矩阵缺口

- {blockers.get("model_matrix", "TODO：运行 DeepSeek/Qwen 相同配置的重复实验并生成 benchmark_summary.json。")}
"""


def _failure_analysis(
    records: list[RunRecord],
    manifest_name: str,
    anchors: tuple[str, dict[str, Any]] | None,
) -> str:
    failed = [item for item in records if not item.final_pass]
    rows = [
        (item.run_id, item.task, item.mode, item.provider_class, item.failure_stage, item.stop_reason)
        for item in failed
    ]
    hypotheses = [item for item in records if item.hypothesis]
    hypothesis_rows = [(item.run_id, item.model, item.hypothesis[:240]) for item in hypotheses]
    _, _, invalidated_table = _oracle_tables(None, anchors)
    return f"""# V3-D 失败分析

{_front_matter(manifest_name)}
## 失败阶段分布

{_table(("Run ID", "Task", "Mode", "Patch provider", "Failure stage", "Stop reason"), rows or [("—", "—", "—", "—", "无", "—")])}

## 已观察到的关键问题

- projection 的真实模型 A01–A03 能定位缺失项，但 Patch 在旧的严格 hunk 行号策略处被拒绝；它们不是功能推理失败，也不是 Vitis 失败。
- projection post-fix、residual structural、synth-fix 的 replay 已通过真实 Vitis fresh final，但 replay 不构成新的真实模型成功证据。
- XSIM/CoSim 失败必须区分 RTL deadlock、仿真器内部异常与沙箱环境失败；不能把启动异常写成算法错误。

## Planner hypothesis（仅来自已保存输出）

{_table(("Run ID", "Model", "Hypothesis excerpt"), hypothesis_rows or [("TODO", "—", "缺少 Planner 输出")])}

## 下一轮所需证据

1. 在 post-fix 代码上完成 projection 新的真实模型 run（新 run ID）。
2. residual 与 synth-fix 各完成至少 3 次真实模型 run，保留全部失败。
3. 为每个失败统一记录 failure stage、Patch rejection、Token、Credit 和 final fresh closure。

## Oracle anchor 降级记录

{invalidated_table}
"""


def _reproducibility(
    records: list[RunRecord],
    manifest_name: str,
    blockers: dict[str, str],
    replay_release: tuple[str, dict[str, Any]] | None,
) -> str:
    run_ids = "\n".join(f"- `{item.run_id}`：{item.provider_class} / {item.evidence_level}" for item in records)
    replay_boundary = "TODO：缺少 replay release。"
    if replay_release is not None:
        source, payload = replay_release
        boundary = payload.get("evidence_boundary", {})
        replay_boundary = (
            f"`{source}`：Vitis={boundary.get('vitis', 'UNKNOWN')}；"
            f"successful replay planner={boundary.get('planner_in_successful_replays', 'UNKNOWN')}；"
            f"atomic real LLM acceptance={boundary.get('atomic_real_llm_acceptance', False)}。"
        )
    return f"""# V3-D 可复现性说明

{_front_matter(manifest_name)}
## 证据分级

- `REAL_LLM + REAL_VITIS_VALIDATED`：模型真实生成 Patch，fresh CSim/Synth/CoSim 全通过。
- `SCRIPTED_PATCH_REPLAY + REAL_VITIS_VALIDATED`：真实 Vitis 验证已知 Patch，只验证执行闭环。
- `DETERMINISTIC/DEMO`：只验证编排和报告，不作为 HLS 成绩。
- `REAL_VITIS_ATTEMPT_FAILED`：保留失败事实，不能计入成功率分子。

## 当前审计 run

{run_ids}

Replay release 的证据边界：{replay_boundary}

## 生成报告

```bash
cd "$PROJECT_ROOT/llm4hls_harness"
python -m submission_tools.cli generate \\
  --repo-root "$PROJECT_ROOT" \\
  --manifest "$PROJECT_ROOT/docs/submission/evidence_manifest.json" \\
  --output-dir "$PROJECT_ROOT/docs/submission"
```

Batch 原始 summary 可能含本机输出路径，先生成只保留指标与 SHA-256 的脱敏快照，再把快照相对路径加入 `evidence_manifest.json`：

```bash
python -m submission_tools.cli snapshot-benchmark \\
  --input "$BENCHMARK_DIR/summary.json" \\
  --output "$PROJECT_ROOT/docs/submission/benchmark_snapshots/my-benchmark.json"

python -m submission_tools.cli snapshot-oracle \\
  --input "$ORACLE_RUN_DIR/summary.json" \\
  --output "$PROJECT_ROOT/docs/submission/oracle_snapshots/my-oracle.json"
```

## 从零运行确定性 Corpus 与 Batch

下面两条命令会创建全新的输出目录；它们只验证数据集门控和批量编排，
不会被报告成真实 LLM 或真实 Vitis 成绩：

```bash
cd "$PROJECT_ROOT"
PYTHONPATH=llm4hls_harness .venv/bin/python \\
  -m llm4hls_agent.v3d_oracle_validator \\
  --corpus llm4hls_harness/task_corpus/v3d-fast \\
  --output-dir /tmp/v3d-oracle-deterministic-fresh \\
  --backend deterministic

PYTHONPATH=llm4hls_harness .venv/bin/python \\
  -m llm4hls_agent.v3_batch_benchmark \\
  --corpus llm4hls_harness/task_corpus/v3d-fast \\
  --output-dir /tmp/v3d-benchmark-deterministic-fresh \\
  --models deterministic-fixture-v1 \\
  --backend deterministic
```

在相同命令末尾增加 `--resume` 可验证断点复用；不要把 `/tmp` 的原始
run 目录直接放入提交包，应先使用上面的 snapshot 命令脱敏。

## 快速回归与真实环境检查

```bash
cd "$PROJECT_ROOT/llm4hls_harness"
python -m unittest discover -s tests -p 'test_*.py'
scripts/v3d-reproduce.sh demo-smoke
LLM4HLS_VITIS_HLS_ROOT="$VITIS_ROOT" scripts/v3d-reproduce.sh real-preflight
```

`real-preflight` 只探测外部 Vitis 2025.2，不声称运行了 CSim/Synth/CoSim。真实模型还必须在运行时提供 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `LLM4HLS_MODEL`，密钥不得写入文件。

## 生成并检查非最终 staging

```bash
cd "$PROJECT_ROOT"
PYTHONPATH=llm4hls_harness .venv/bin/python -m submission_tools.cli stage \\
  --source-root "$PROJECT_ROOT" \\
  --output-root "$PROJECT_ROOT/build/submission-staging-NOT-FINAL" \\
  --spec "$PROJECT_ROOT/docs/submission/staging_spec.json"

PYTHONPATH=llm4hls_harness .venv/bin/python -m submission_tools.cli scan \\
  --root "$PROJECT_ROOT/build/submission-staging-NOT-FINAL"
```

Planner 输入可以独立专项检查：`python -m submission_tools.cli scan --root "$RUN_DIR/planner/inputs"`。
staging 工具直接读取当前 Git HEAD 和工作树状态；环境变量不能把脏工作树伪装成 clean。每个 staging 文件仍有独立 SHA-256。

## 当前外部阻塞

- {blockers.get("real_llm", "TODO：配置 OpenAI-compatible endpoint、key 和 model 后运行真实模型矩阵。")}

## 可复现性边界

- 本文不声称 hidden grader 通过。
- 本文不声称 Vitis 可以合法打包进容器；使用主机外部 runtime。
- Run 目录是本地证据，不进入提交 staging；提交前只保留脱敏汇总和必要源码。
"""


def _demo_script(records: list[RunRecord], manifest_name: str) -> str:
    dot = next(
        (
            item
            for item in records
            if item.task == "dotProduct_optimize" and item.real_llm_vitis_e2e
        ),
        None,
    )
    dot_claim = (
        f"真实 DeepSeek + Vitis：{_fmt_number(dot.baseline_latency)} → {_fmt_number(dot.final_latency)} cycles，"
        f"{_fmt_number(dot.acceleration, 2)}×，Tokens={dot.tokens}，Credits={dot.credits}。"
        if dot
        else "TODO：补录 dotProduct 真实结果。"
    )
    return f"""# 5 分钟 Demo 讲稿草案

{_front_matter(manifest_name)}
## 0:00–0:40 问题与限制

Track A 不是一次生成代码，而是在 Token、Credit 和时间受限条件下，用 CSim、Synth、CoSim 搜索正确且更快的 HLS kernel。工具成本分别是 1、4、20 Credits，因此验证顺序直接影响成绩。

## 0:40–1:20 PhaseRouter 与 Evidence

展示 baseline 验证如何路由到 `REPAIR / SYNTH_FIX / STRUCTURAL_FIX / OPTIMIZE`。展示结构化 Evidence 只保留错误类型、源码位置、loop II/TripCount、资源和有界日志，不把完整日志、hidden 或 golden 交给模型。

## 1:20–2:10 LLM Patch、Candidate 与 Budget

模型只输出 hypothesis、strategy bundle 和 unified diff；Patch Validator 与 TopInterfaceGuard 保护文件路径和顶层接口。CandidateManager 保存父子关系，BudgetLedger 是硬约束，模型不能批准工具、晋升 Candidate 或透支预算。

## 2:10–3:00 CoSim gate 与 fresh final closure

Candidate 先 CSim，再 Synth。没有严格性能提升就拒绝且不花 CoSim。stream/DATAFLOW/interface 或高风险 bitwidth 修改必须 CoSim。最终答案无条件 fresh 运行 CSim、Synth、CoSim，不能复用探索缓存。

## 3:00–3:50 真实 dotProduct

{dot_claim}
重点解释 baseline transaction interval 约 1025，但 loop achieved II=1；瓶颈是 1024 次串行 transaction/accumulation，不是“缺 PIPELINE”。模型采用 array partition、unroll 和多部分和并行归约。

## 3:50–4:30 Task-aware 三模式

- projection：真实模型 A01–A03 找到功能错误，但旧 Patch policy 拒绝；post-fix 历史 Patch replay 已完成真实 Vitis fresh closure。不得称为新的真实 LLM 成功。
- structural：residual deadlock 的脚本 Patch replay 已完成真实 Vitis closure；真实 LLM 仍为 TODO。
- synth-fix：dynamic allocation 的脚本 Patch replay 已完成真实 Vitis closure；真实 LLM 仍为 TODO。

## 4:30–5:00 总结

收束到三点：任务阶段路由、预算感知验证、可审计证据分级。最后明确当前缺口是重复真实模型矩阵和 hidden grader，而不是用 demo/replay 冒充结果。
"""


def _checklist(
    records: list[RunRecord],
    manifest_name: str,
    blockers: dict[str, str],
    oracle: tuple[str, dict[str, Any]] | None,
    anchors: tuple[str, dict[str, Any]] | None,
) -> str:
    oracle_counts = oracle[1].get("counts", {}) if oracle else {}
    anchor_items = anchors[1].get("valid_real_vitis_anchors", []) if anchors else []
    anchor_modes = {
        str(item.get("mode"))
        for item in anchor_items
        if isinstance(item, dict) and item.get("mode")
    }
    anchor_modes_complete = anchor_modes == {
        "REPAIR",
        "SYNTH_FIX",
        "STRUCTURAL_FIX",
        "OPTIMIZE",
    }
    dot = next(
        (
            item
            for item in records
            if item.task == "dotProduct_optimize" and item.real_llm_vitis_e2e
        ),
        None,
    )
    dot_check = "x" if dot is not None else " "
    dot_summary = (
        f"{_fmt_number(dot.baseline_latency)}→{_fmt_number(dot.final_latency)} cycles"
        if dot is not None
        else "TODO：缺少同时满足真实模型 provenance、REAL_VITIS_VALIDATED 与 fresh final 的 run"
    )
    return f"""# 提交前 Checklist（非最终规则）

{_front_matter(manifest_name)}
> 本清单和 staging 都是内部候选，不代表官方最终提交格式已确认。

## 代码与运行

- [ ] 完整 unittest、`py_compile`、`git diff --check` 通过。
- [ ] deterministic 官方三题 smoke 可复现。
- [ ] 外部 Vitis 2025.2 preflight 通过。
- [ ] 所有真实 run 使用独立目录，失败 run 未被覆盖。
- [ ] fresh final CSim/Synth/CoSim 证据可追溯到 run ID。

## 实验完整性

- [{dot_check}] dotProduct 真实模型 + Vitis：{dot_summary}。
- [ ] projection post-fix 新真实模型闭环。当前只有失败 A01–A03 与 Patch replay。
- [ ] residual STRUCTURAL_FIX 真实模型闭环。当前只有 scripted replay + real Vitis。
- [ ] SYNTH_FIX 真实模型闭环。当前只有 scripted replay + real Vitis。
- [ ] DeepSeek 三次重复及 Qwen 相同配置矩阵：{blockers.get("model_matrix", "TODO")}
- [ ] strict / fast 和两项消融已运行；缺失处保持 TODO。
- [{'x' if oracle_counts.get('accepted') == 28 and oracle_counts.get('rejected') == 0 else ' '}] deterministic Oracle 28 accepted / 0 rejected（fixture only）。
- [{'x' if anchor_modes_complete else ' '}] 四种 mode 均有有效真实 Vitis corpus anchor；不等同于真实 LLM Agent 成功。

## 脱敏与打包

- [ ] `python -m submission_tools.cli stage ...` 生成新的 `NOT FINAL` staging。
- [ ] staging 安全扫描为 0 findings。
- [ ] 不包含 `.env`、API key、Authorization token、license、用户名或本机绝对路径。
- [ ] 不包含 `runs/`、checkpoint、cache、临时文件或大文件。
- [ ] 不包含 `golden/`、`hidden_like/`、reference solution 或 mutation answer。
- [ ] Planner 输入专项扫描确认没有 golden/hidden-like 字段或路径。
- [ ] 官方最终目录、Docker/Vitis 部署和视频要求经最新规则人工确认。
"""


def generate_submission_docs(*, repo_root: Path, manifest_path: Path, output_dir: Path) -> list[Path]:
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    manifest = _load_json(manifest_path)
    runs_root = repo_root / str(manifest.get("runs_root", "llm4hls_harness/runs"))
    specs = manifest.get("curated_runs")
    if not isinstance(specs, list) or not specs:
        raise ValueError("evidence manifest must contain non-empty curated_runs")
    records = [_record(runs_root, item) for item in specs if isinstance(item, dict)]
    benchmark_specs = manifest.get("benchmark_summaries", [])
    if not isinstance(benchmark_specs, list):
        raise ValueError("benchmark_summaries must be a list")
    benchmarks: list[tuple[str, dict[str, Any]]] = []
    for relative in benchmark_specs:
        path = repo_root / str(relative)
        if not path.is_file():
            raise FileNotFoundError(f"curated benchmark summary is missing: {path}")
        benchmarks.append((str(relative), _load_json(path)))
    def optional_json(key: str) -> tuple[str, dict[str, Any]] | None:
        relative = manifest.get(key)
        if not relative:
            return None
        path = repo_root / str(relative)
        if not path.is_file():
            raise FileNotFoundError(f"curated {key} is missing: {path}")
        return str(relative), _load_json(path)

    oracle = optional_json("oracle_summary")
    anchors = optional_json("real_anchor_release")
    replay_release = optional_json("replay_release")
    blockers = manifest.get("blockers", {})
    if not isinstance(blockers, dict):
        blockers = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "experiment_tables.md": _experiment_tables(records, manifest_path.name, blockers, benchmarks, oracle, anchors),
        "failure_analysis.md": _failure_analysis(records, manifest_path.name, anchors),
        "reproducibility.md": _reproducibility(records, manifest_path.name, blockers, replay_release),
        "demo_script_5min.md": _demo_script(records, manifest_path.name),
        "submission_checklist.md": _checklist(records, manifest_path.name, blockers, oracle, anchors),
    }
    paths: list[Path] = []
    for name, content in documents.items():
        path = output_dir / name
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        paths.append(path)
    return paths
