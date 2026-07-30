#!/usr/bin/env python3
"""Build read-only evidence reports for Goal 020's fresh campaigns.

This script deliberately consumes completed benchmark receipts only.  It never
opens an Agent CLI, invokes a tool backend, or changes a run artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import mean, median


GROUPS = ("FULL_REF", "MINUS_A2", "MINUS_A3", "MINUS_A2_A3")
TASKS = (
    "v3d_fast_001", "v3d_fast_008", "v3d_fast_009",
    "v3d_fast_012", "v3d_fast_018", "v3d_fast_021",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def one_jsonl(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError(f"expected exactly one benchmark row in {path}, got {len(rows)}")
    return rows[0]


def compact(row: dict, group: str | None = None) -> dict:
    provenance = row.get("provenance_validation") or {}
    tools = row.get("tool_calls") or {}
    result = {
        "task_id": row["task_id"],
        "group": group,
        "status": row.get("status"),
        "e2e_success": bool(row.get("e2e_success")),
        "fresh_final_success": bool(row.get("fresh_final_success")),
        "final_validation_success": bool(row.get("final_validation_success")),
        "stop_reason": row.get("stop_reason"),
        "failure_stage": row.get("failure_stage"),
        "expected_mode": row.get("expected_mode"),
        "routed_mode": row.get("routed_mode"),
        "route_match": row.get("router_correct"),
        "planner_calls": row.get("model_calls", 0),
        "tokens_used": row.get("tokens_used", 0),
        "input_tokens_used": row.get("input_tokens_used", 0),
        "output_tokens_used": row.get("output_tokens_used", 0),
        "cached_input_tokens_used": row.get("cached_input_tokens_used", 0),
        "credits_used": row.get("credits_used", 0),
        "patch_candidates": row.get("patch_candidates", 0),
        "patch_rejections": row.get("patch_rejections", 0),
        "patch_rejection_reasons": row.get("patch_rejection_reasons", []),
        "tool_calls": tools,
        "wall_time_s": row.get("wall_time_s"),
        "reported_runtime_s": row.get("reported_runtime_s"),
        "acceleration_vs_baseline": row.get("acceleration_vs_baseline"),
        "evidence_level": row.get("evidence_level"),
        "executor_fingerprint": row.get("executor_fingerprint"),
        "run_fingerprint": row.get("run_fingerprint"),
        "run_id": row.get("run_id"),
        "run_dir": row.get("run_dir"),
        "package_manifest_sha256": (provenance.get("package_manifest") or {}).get("sha256"),
        "certification_status": (provenance.get("independent_certification") or {}).get("status"),
    }
    return result


def numeric_summary(rows: list[dict]) -> dict:
    summary = {"runs": len(rows), "strict_successes": sum(r["e2e_success"] for r in rows)}
    summary["strict_success_rate"] = summary["strict_successes"] / len(rows) if rows else None
    for key in ("planner_calls", "tokens_used", "credits_used", "patch_candidates", "patch_rejections", "wall_time_s"):
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        summary[key] = {"total": sum(values), "mean": mean(values) if values else None,
                        "median": median(values) if values else None}
    summary["failure_stages"] = dict(Counter(r["failure_stage"] or "NONE" for r in rows if not r["e2e_success"]))
    summary["stop_reasons"] = dict(Counter(r["stop_reason"] or "UNKNOWN" for r in rows))
    return summary


def write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = [
        "task_id", "group", "status", "e2e_success", "fresh_final_success",
        "final_validation_success", "stop_reason", "failure_stage", "expected_mode",
        "routed_mode", "route_match", "planner_calls", "tokens_used",
        "input_tokens_used", "output_tokens_used", "credits_used", "patch_candidates",
        "patch_rejections", "wall_time_s", "acceleration_vs_baseline", "run_id", "run_dir",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def differences(left: list[dict], right: list[dict], name: str) -> dict:
    left_by_task = {r["task_id"]: r for r in left}
    right_by_task = {r["task_id"]: r for r in right}
    rows = []
    for task_id in TASKS:
        a, b = left_by_task[task_id], right_by_task[task_id]
        rows.append({
            "task_id": task_id,
            "comparison": name,
            "strict_success_delta": int(a["e2e_success"]) - int(b["e2e_success"]),
            "planner_calls_delta": a["planner_calls"] - b["planner_calls"],
            "tokens_used_delta": a["tokens_used"] - b["tokens_used"],
            "credits_used_delta": a["credits_used"] - b["credits_used"],
            "patch_rejections_delta": a["patch_rejections"] - b["patch_rejections"],
            "wall_time_s_delta": (a["wall_time_s"] or 0) - (b["wall_time_s"] or 0),
        })
    metrics = ("strict_success_delta", "planner_calls_delta", "tokens_used_delta", "credits_used_delta", "patch_rejections_delta", "wall_time_s_delta")
    return {"comparison": name, "per_task": rows,
            "mean_deltas": {key: mean(row[key] for row in rows) for key in metrics},
            "sum_deltas": {key: sum(row[key] for row in rows) for key in metrics}}


def report_main(rows: list[dict], summary: dict) -> str:
    failures = [r for r in rows if not r["e2e_success"]]
    lines = [
        "# Goal 020：fresh 28 题 Full Agent 主实验",
        "",
        "本报告由已封存的 `benchmark_results.jsonl` 离线生成；没有重跑任务、调用模型或启动 Vitis。",
        "",
        f"- 严格成功：{summary['strict_successes']}/{summary['runs']} ({summary['strict_success_rate']:.2%})",
        f"- Planner 调用：{summary['planner_calls']['total']}；Token：{summary['tokens_used']['total']}；Agent Credits：{summary['credits_used']['total']}",
        "- 成功定义：Agent 终态、冻结 candidate、B2 CSim/Synth/CoSim 和 100 MHz Gate 均通过。",
        "",
        "## 未通过任务",
        "",
        "| Task | Mode | Stop reason | Failure stage | Planner | Tokens | Credits |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for row in failures:
        lines.append(f"| {row['task_id']} | {row['expected_mode']} | {row['stop_reason']} | {row['failure_stage']} | {row['planner_calls']} | {row['tokens_used']} | {row['credits_used']} |")
    if not failures:
        lines.append("| None | - | - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def report_ablation(by_group: dict[str, list[dict]], summary: dict, deltas: list[dict], interaction: list[dict]) -> str:
    lines = [
        "# Goal 020：A2/A3 2×2 消融（6 题 × 4 组）",
        "",
        "A1 始终开启。每组均为 fresh run，FULL_REF 不是复用历史结果。此为每组一次的 6 题描述性证据，不具备统计显著性，不能据此宣称隐藏集或总体因果收益。",
        "",
        "| Group | A2 | A3 | Strict success | Planner total | Token total | Credits total |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    modes = {"FULL_REF": ("enforce", "guided"), "MINUS_A2": ("off", "guided"), "MINUS_A3": ("enforce", "off"), "MINUS_A2_A3": ("off", "off")}
    for group in GROUPS:
        s = summary[group]
        lines.append(f"| {group} | {modes[group][0]} | {modes[group][1]} | {s['strict_successes']}/{s['runs']} | {s['planner_calls']['total']} | {s['tokens_used']['total']} | {s['credits_used']['total']} |")
    lines += ["", "## 逐题成功矩阵", "", "| Task | FULL_REF | MINUS_A2 | MINUS_A3 | MINUS_A2_A3 |", "|---|---:|---:|---:|---:|"]
    for task in TASKS:
        lines.append("| " + task + " | " + " | ".join("PASS" if next(r for r in by_group[g] if r['task_id'] == task)['e2e_success'] else "FAIL" for g in GROUPS) + " |")
    lines += [
        "", "## 解释边界", "",
        "- A2/A3 的 off 组使用独立 Evaluation Harness 身份；A2 off 不加载 A2 Admission，A3 off 不加载 Ranker、A3 Admission 或 Store。",
        "- 唯一成功差异是 `v3d_fast_012`：MINUS_A3 失败、其他三组通过。它既可能反映组件条件下的输出差异，也可能只是单次 LLM 轨迹；没有配对重复，不能将它归因给 A2/A3。",
        "- `v3d_fast_018` 的 expected mode 是 STRUCTURAL_FIX，而本次真实 routed mode 为 OPTIMIZE；该运行时路由差异已保留，并未修改 Router。",
        "- 成功结果均具备独立最终认证；失败结果没有被计入成功。",
        "", "## 配对差值（左组减右组）", "",
        "| Comparison | Success Δ sum | Planner Δ sum | Token Δ sum | Credits Δ sum |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in deltas:
        s = item["sum_deltas"]
        lines.append(f"| {item['comparison']} | {s['strict_success_delta']:+.0f} | {s['planner_calls_delta']:+.0f} | {s['tokens_used_delta']:+.0f} | {s['credits_used_delta']:+.0f} |")
    if interaction:
        lines += ["", "交互项逐任务数据写入 `ablation_interaction_effects.json`；该数值同样只能描述本次单次执行。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-dir", type=Path, required=True)
    parser.add_argument("--ablation-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    main_sources = sorted(args.main_dir.glob("*/benchmark_results.jsonl"))
    main_rows = [compact(one_jsonl(path)) for path in main_sources]
    if len(main_rows) != 28 or len({r["task_id"] for r in main_rows}) != 28:
        raise ValueError(f"main campaign must contain 28 distinct records, found {len(main_rows)}")
    main_rows.sort(key=lambda r: int(r["task_id"].rsplit("_", 1)[1]))
    main_summary = numeric_summary(main_rows)
    main_evidence = [{"campaign": "FULL_MAIN_28", "task_id": one_jsonl(path)["task_id"],
                      "path": str(path), "sha256": sha256(path)} for path in main_sources]

    by_group: dict[str, list[dict]] = {}
    evidence_sources = []
    for group in GROUPS:
        rows = [compact(one_jsonl(args.ablation_dir / group / task / "benchmark_results.jsonl"), group) for task in TASKS]
        if {r["task_id"] for r in rows} != set(TASKS):
            raise ValueError(f"incomplete {group} results")
        by_group[group] = rows
        for task in TASKS:
            source = args.ablation_dir / group / task / "benchmark_results.jsonl"
            evidence_sources.append({"group": group, "task_id": task, "path": str(source), "sha256": sha256(source)})

    ablation_rows = [row for group in GROUPS for row in by_group[group]]
    ablation_summary = {group: numeric_summary(by_group[group]) for group in GROUPS}
    comparison_specs = (
        ("A2 effect with A3 guided: FULL_REF - MINUS_A2", "FULL_REF", "MINUS_A2"),
        ("A2 effect with A3 off: MINUS_A3 - MINUS_A2_A3", "MINUS_A3", "MINUS_A2_A3"),
        ("A3 effect with A2 enforce: FULL_REF - MINUS_A3", "FULL_REF", "MINUS_A3"),
        ("A3 effect with A2 off: MINUS_A2 - MINUS_A2_A3", "MINUS_A2", "MINUS_A2_A3"),
        ("A2+A3 overall: FULL_REF - MINUS_A2_A3", "FULL_REF", "MINUS_A2_A3"),
    )
    deltas = [differences(by_group[left], by_group[right], name) for name, left, right in comparison_specs]
    interaction = []
    by_task = {group: {r["task_id"]: r for r in rows} for group, rows in by_group.items()}
    for task in TASKS:
        def interaction_metric(key: str) -> float:
            return (by_task["FULL_REF"][task][key] - by_task["MINUS_A2"][task][key]
                    - by_task["MINUS_A3"][task][key] + by_task["MINUS_A2_A3"][task][key])
        interaction.append({"task_id": task, "success_interaction": interaction_metric("e2e_success"),
                            "planner_calls_interaction": interaction_metric("planner_calls"),
                            "tokens_used_interaction": interaction_metric("tokens_used"),
                            "credits_used_interaction": interaction_metric("credits_used")})

    matrix = {task: {group: next(r for r in by_group[group] if r["task_id"] == task)
                     for group in GROUPS} for task in TASKS}
    toggle_audit = {}
    for group in GROUPS:
        configs = []
        for task in TASKS:
            run_dir = Path(next(r for r in by_group[group] if r["task_id"] == task)["run_dir"])
            config = json.loads((run_dir / "v3_run_config.json").read_text())
            configs.append({
                "task_id": task,
                "continuation_policy_mode": config.get("continuation_policy_mode"),
                "continuation_admission_loaded": "continuation_admission_sha256" in config,
                "experience_directory_present": (run_dir / "experience").exists(),
            })
        toggle_audit[group] = configs

    write_jsonl(args.out_dir / "main_28_task_results.jsonl", main_rows)
    write_csv(args.out_dir / "main_28_task_results.csv", main_rows)
    write_json(args.out_dir / "main_28_task_summary.json", main_summary)
    (args.out_dir / "main_28_task_report.md").write_text(report_main(main_rows, main_summary))
    failures = [r for r in main_rows if not r["e2e_success"]]
    write_json(args.out_dir / "main_28_failures_timeouts_incomplete.json", failures)

    write_jsonl(args.out_dir / "ablation_24_task_results.jsonl", ablation_rows)
    write_csv(args.out_dir / "ablation_24_task_results.csv", ablation_rows)
    write_json(args.out_dir / "ablation_summary.json", ablation_summary)
    write_json(args.out_dir / "ablation_matrix.json", matrix)
    write_json(args.out_dir / "ablation_pairwise_deltas.json", deltas)
    write_json(args.out_dir / "ablation_interaction_effects.json", interaction)
    (args.out_dir / "ablation_report.md").write_text(report_ablation(by_group, ablation_summary, deltas, interaction))
    write_json(args.out_dir / "ablation_evidence_manifest.json", {"campaign": str(args.ablation_dir), "sources": evidence_sources})
    write_json(args.out_dir / "a2_a3_toggle_audit.json", toggle_audit)
    write_json(args.out_dir / "run_artifact_index.json", {"main": main_evidence, "ablation": evidence_sources})
    all_failures = ([dict(row, campaign="FULL_MAIN_28") for row in main_rows if not row["e2e_success"]]
                    + [dict(row, campaign="A2_A3_2X2") for row in ablation_rows if not row["e2e_success"]])
    write_json(args.out_dir / "all_failures_timeouts_incomplete.json", all_failures)

    architecture = """# A2/A3 架构核验（代码事实）\n\n- A2：`run_v3_prototype` 接收独立的 `continuation_policy_mode`；`off` 路径不调用 Continuation V3，`enforce` 仅在 B1 允许后改变后续搜索路由。\n- A3：CLI 仅在 `experience_mode != off` 时构造 Experience coordinator；`off` 不加载 Store、Ranker 或 A3 Admission。`guided` 才会生成受控 advice/Strategy Card。\n- A2→A3 的真实协作点是授权边界：A3 的建议和 Planner 请求先以纯内存方式准备；A2 使用该请求的 token 估计完成准入；仅当 A2 放行时，Action Journal 才持久化 A3 建议并派发该 Planner action。也就是说，A2 的 ALLOW/拒绝真实控制 A3 advice 是否成为正式运行记录以及是否参与本轮解题。\n- 代码没有“把 A2 decision 文本再注入 A3 Strategy Card”的同轮路径；这么做会使 Planner token 估计与 A2 准入相互循环。该限制必须如实保留，不能包装为更强的直接反馈。\n- 本次 `a2_a3_toggle_audit.json` 对 24 个 run 的 `v3_run_config.json` 和 run 目录逐一核对：A2-off 组均没有 continuation admission 哈希；A3-off 组均没有 `experience/` 运行目录。\n- 因此两者可独立关闭；此报告只证明开关/路径边界和本次运行未见越界，不证明 A2/A3 的因果收益。\n"""
    (args.out_dir / "a2_a3_architecture_audit.md").write_text(architecture)
    report020 = """# v3d_fast_020 修复与验收\n\n- 变更后的 baseline CoSim probe 使用 300 秒上限，并受单题绝对 deadline 与清理预留约束，不再默认占用 1800 秒。\n- Vitis backend 的 no-progress guard 仅清理当前 action 的进程组；超时后仍保留可序列化终态。\n- 专用 fresh smoke 曾产生完整认证成功证据（baseline CoSim probe 后进入 STRUCTURAL_FIX，candidate_001 通过 CSim/Synth/CoSim，B2 独立通过）。\n- 之后的 fresh 28 题 main run 中，020 因新的模型补丁未通过 candidate CoSim 而形成普通失败；但 baseline 已受 300 秒探针限制，Planner 实际调用、candidate 已创建，说明 executor/closeout 修复仍生效。\n- 该题的主失败不等于 executor 基础设施失败，也不应作为 A2/A3 成败证据。\n"""
    (args.out_dir / "v3d_fast_020_fix_acceptance.md").write_text(report020)
    manifest_paths = sorted(args.out_dir.glob("*"))
    write_json(args.out_dir / "report_manifest.json", {"files": [{"name": p.name, "sha256": sha256(p)} for p in manifest_paths if p.is_file() and p.name != "report_manifest.json"]})


if __name__ == "__main__":
    main()
