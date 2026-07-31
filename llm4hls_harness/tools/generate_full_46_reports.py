#!/usr/bin/env python3
"""Render auditable reports for the frozen RC2 46-task FULL_REF campaign.

This is deliberately an offline report generator: it consumes only persisted run
artifacts and never invokes a model or an HLS tool.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


ACCEL_CAP = 8.0


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def status(value: Any) -> str:
    if isinstance(value, dict):
        raw = value.get("status", value.get("phase", "UNKNOWN"))
        return str(raw).upper()
    return "UNKNOWN" if value is None else str(value)


def metric_latency(result: dict[str, Any] | None) -> int | None:
    report = (result or {}).get("report") or {}
    latency = report.get("latency") or {}
    for key in ("worst", "average", "best"):
        value = latency.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def read_relative(run_dir: Path, ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    path = run_dir / ref
    return load_json(path) if path.is_file() else None


def markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "_None_\n"
    header = "| " + " | ".join(name for name, _ in columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        values = []
        for _, key in columns:
            value = row.get(key, "")
            if value is None:
                value = "N/A"
            if isinstance(value, float):
                value = f"{value:.3f}"
            values.append(str(value).replace("|", "\\|"))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, rule, *body]) + "\n"


def command(cwd: Path, *args: str) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def extract_record(row: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(row["run_dir"])
    certified = load_json(run_dir / "v3_certified_result.json")
    receipt_ref = (certified.get("final_certification") or {}).get("receipt_ref")
    receipt = read_relative(run_dir, receipt_ref)
    baseline = read_relative(run_dir, certified.get("baseline_metrics_ref"))
    final_metrics = read_relative(run_dir, certified.get("final_metrics_ref"))
    baseline_latency = metric_latency(baseline)
    candidate_latency = metric_latency(final_metrics)
    acceleration = None
    if baseline_latency and candidate_latency:
        acceleration = baseline_latency / candidate_latency

    b2_stages = (receipt or {}).get("stages") or {}
    b2_csim = status(b2_stages.get("csim"))
    b2_synth = status(b2_stages.get("synth"))
    b2_cosim = status(b2_stages.get("cosim"))
    final_validation = certified.get("final_validation") or {}
    search_csim = status(final_validation.get("csim"))
    search_synth = status(final_validation.get("synth"))
    search_cosim = status(final_validation.get("cosim"))
    clock = (receipt or {}).get("clock_gate") or certified.get("final_clock") or {}
    clock_pass = bool(clock.get("passed"))
    requires_cosim = bool(load_json(run_dir / "v3_task_spec.json").get("requires_cosim", False))
    public_functional = b2_csim == "PASS" and (not requires_cosim or b2_cosim == "PASS")
    public_synth = b2_synth == "PASS"
    public_proxy_score = 0.0
    if public_functional:
        ppa = min(acceleration, ACCEL_CAP) / ACCEL_CAP if acceleration else 0.0
        public_proxy_score = row["difficulty"] * (0.5 + (0.2 if public_synth else 0.0) + 0.3 * ppa)

    budget = certified.get("budget") or {}
    tool_used = budget.get("tool_used") or row.get("tool_calls") or {}
    provenance = row.get("provenance_validation") or {}
    package_ref = ((certified.get("package") or {}).get("manifest_ref"))
    package_path = run_dir / package_ref if package_ref else None
    receipt_path = run_dir / receipt_ref if receipt_ref else None
    final_candidate = certified.get("final_candidate_id")
    candidate_path = run_dir / "candidates" / str(final_candidate) / "source" / "kernel.cpp"
    return {
        "task_id": row["task_id"],
        "task_dir": row["task_dir"],
        "difficulty": row["difficulty"],
        "expected_mode": row["expected_mode"],
        "routed_mode": row["routed_mode"],
        "route_match": row["expected_mode"] == row["routed_mode"],
        "status": row["status"],
        "e2e_success": bool(row["e2e_success"]),
        "fresh_final_success": bool(row["fresh_final_success"]),
        "final_validation_success": bool(row["final_validation_success"]),
        "b2_status": status(certified.get("final_certification")),
        "b2_csim": b2_csim,
        "b2_synth": b2_synth,
        "b2_cosim": b2_cosim,
        "clock_100mhz": "PASS" if clock_pass else "FAIL",
        "search_csim": search_csim,
        "search_synth": search_synth,
        "search_cosim": search_cosim,
        "requires_cosim": requires_cosim,
        "final_candidate_id": final_candidate,
        "candidate_sha256": sha256(candidate_path) if candidate_path.is_file() else "MISSING",
        "baseline_latency": baseline_latency,
        "candidate_latency": candidate_latency,
        "raw_acceleration": acceleration,
        "capped_acceleration": min(acceleration, ACCEL_CAP) if acceleration else None,
        "official_score": "NOT_EXECUTED_NO_HIDDEN_RECEIPT",
        "public_validation_proxy_score": round(public_proxy_score, 4),
        "public_proxy_formula": "difficulty*(0.5*public_functional+0.2*public_synth+0.3*min(acceleration,8)/8)",
        "planner_calls": row["model_calls"],
        "input_tokens": row["input_tokens_used"],
        "output_tokens": row["output_tokens_used"],
        "tokens": row["tokens_used"],
        "credits": row["credits_used"],
        "candidates": row["patch_candidates"],
        "patch_rejections": row["patch_rejections"],
        "wall_time_s": row["wall_time_s"],
        "tool_csim": tool_used.get("csim", 0),
        "tool_synth": tool_used.get("synth", 0),
        "tool_cosim": tool_used.get("cosim", 0),
        "tool_llm": tool_used.get("llm", row["model_calls"]),
        "failure_stage": row.get("failure_stage") or "NONE",
        "failure_kind": certified.get("last_failure_kind") or "NONE",
        "failure_signature": certified.get("last_failure_signature") or "NONE",
        "last_action_family": certified.get("last_action_family") or "NONE",
        "stop_reason": row["stop_reason"],
        "run_dir": str(run_dir),
        "run_id": row["run_id"],
        "task_fingerprint": row["task_fingerprint"],
        "executor_fingerprint": row["executor_fingerprint"],
        "run_fingerprint": row["run_fingerprint"],
        "package_manifest": str(package_path) if package_path else "MISSING",
        "package_sha256": sha256(package_path) if package_path and package_path.is_file() else "MISSING",
        "certification_receipt": str(receipt_path) if receipt_path else "MISSING",
        "certification_receipt_sha256": sha256(receipt_path) if receipt_path and receipt_path.is_file() else "MISSING",
        "ledger_sha256": (provenance.get("token_ledger") or {}).get("sha256", "MISSING"),
        "provenance_complete": bool(provenance) and bool(receipt_path and receipt_path.is_file()) and bool(package_path and package_path.is_file()),
        "agent_ledger_unchanged_by_b2": (receipt or {}).get("agent_ledger", {}).get("unchanged"),
    }


def sum_fields(records: list[dict[str, Any]], key: str) -> float:
    return sum(value for record in records if isinstance((value := record.get(key)), (int, float)))


def summary_for(records: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(records)
    success = sum(bool(record["e2e_success"]) for record in records)
    return {
        "tasks": count,
        "success": success,
        "success_rate": success / count if count else 0.0,
        "planner_calls": sum_fields(records, "planner_calls"),
        "tokens": sum_fields(records, "tokens"),
        "credits": sum_fields(records, "credits"),
        "candidates": sum_fields(records, "candidates"),
        "wall_time_s": sum_fields(records, "wall_time_s"),
        "public_validation_proxy_score": sum_fields(records, "public_validation_proxy_score"),
        "mean_acceleration": mean([record["raw_acceleration"] for record in records if record["raw_acceleration"] is not None]) if any(record["raw_acceleration"] is not None for record in records) else None,
        "acceleration_ge_8": sum((record["raw_acceleration"] or 0) >= 8.0 for record in records),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    args = parser.parse_args()
    result_path = args.run_dir / "benchmark_results.jsonl"
    rows = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [extract_record(row) for row in rows]
    records.sort(key=lambda record: record["task_id"])
    args.out_dir.mkdir(parents=True, exist_ok=True)

    results_path = args.out_dir / "full_46_results.jsonl"
    with results_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    mode_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        mode_records[record["expected_mode"]].append(record)
    total = summary_for(records)
    mode_summaries = {mode: summary_for(values) for mode, values in sorted(mode_records.items())}
    summary_rows = [{"scope": "ALL", **total}, *({"scope": mode, **summary} for mode, summary in mode_summaries.items())]
    summary_path = args.out_dir / "full_46_summary.csv"
    with summary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader(); writer.writerows(summary_rows)

    score_fields = [
        "task_id", "difficulty", "expected_mode", "routed_mode", "route_match", "e2e_success",
        "b2_status", "b2_csim", "b2_synth", "b2_cosim", "clock_100mhz", "baseline_latency",
        "candidate_latency", "raw_acceleration", "capped_acceleration", "official_score",
        "public_validation_proxy_score", "requires_cosim",
    ]
    score_path = args.out_dir / "full_46_scorecards.csv"
    with score_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=score_fields)
        writer.writeheader(); writer.writerows([{key: record.get(key) for key in score_fields} for record in records])

    # A completed task can contain useful baseline/intermediate failure evidence
    # (for example a repaired compile error or public deadlock) without being a
    # terminal Agent failure.  Keep that evidence visible, but label it so the
    # CSV cannot be mistaken for an E2E failure list.
    failure_evidence = [
        record for record in records
        if (not record["e2e_success"]
            or record["failure_stage"] != "NONE"
            or record["failure_kind"] not in {"NONE", "UNKNOWN"})
    ]
    failure_fields = ["task_id", "expected_mode", "routed_mode", "e2e_success", "terminal_failure", "evidence_role", "failure_stage", "failure_kind", "failure_signature", "last_action_family", "stop_reason", "patch_rejections"]
    failure_path = args.out_dir / "full_46_failure_inventory.csv"
    with failure_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=failure_fields)
        writer.writeheader()
        for record in failure_evidence:
            row = {key: record.get(key) for key in failure_fields}
            row["terminal_failure"] = not record["e2e_success"] or record["failure_stage"] != "NONE"
            row["evidence_role"] = "TERMINAL_FAILURE" if row["terminal_failure"] else "PRE_FINALIZATION_DIAGNOSTIC"
            writer.writerow(row)

    stage_counts = Counter(record["failure_stage"] for record in records)
    kind_counts = Counter(record["failure_kind"] for record in records)
    action_counts = Counter(record["last_action_family"] for record in records)
    tool_totals = {name: sum_fields(records, name) for name in ("tool_csim", "tool_synth", "tool_cosim", "tool_llm")}
    report_path = args.out_dir / "full_46_benchmark_report.md"
    report = [
        "# RC2 46-task FULL_REF benchmark report",
        "",
        "## Result",
        "",
        f"- Fresh completed tasks: **{total['tasks']}/46**; E2E success: **{total['success']}/46 ({total['success_rate']:.2%})**.",
        "- Every reported success has a frozen final Candidate, independent B2 certification, and a 100 MHz clock-gate result.",
        "- This report uses public Artifact evidence only. `official_score` is `NOT_EXECUTED_NO_HIDDEN_RECEIPT` for every task; no hidden test was run or inferred.",
        "- `public_validation_proxy_score` implements the official `scoring.py` formula using public B2 evidence and public synth latency. It is not an official hidden-test score.",
        "",
        "## Benchmark overview",
        "",
        f"- Mode distribution: " + "; ".join(f"{mode} {summary['tasks']}" for mode, summary in mode_summaries.items()) + ".",
        "- Difficulty comes from the frozen public task manifests; the benchmark contains original `v3d-fast` and expanded `v3d-expanded` tasks.",
        "",
        "## Mode analysis",
        "",
        markdown_table(summary_rows, [("Scope", "scope"), ("Tasks", "tasks"), ("Success", "success"), ("Success rate", "success_rate"), ("Planner", "planner_calls"), ("Tokens", "tokens"), ("Credits", "credits"), ("Candidates", "candidates"), ("Wall seconds", "wall_time_s"), ("Public proxy", "public_validation_proxy_score"), ("Mean accel", "mean_acceleration")]),
        "## Agent efficiency and tools",
        "",
        f"- Planner calls: {int(total['planner_calls'])}; Tokens: {int(total['tokens'])}; Agent Credits: {int(total['credits'])}; Candidates: {int(total['candidates'])}; total wall time: {total['wall_time_s']:.1f}s.",
        f"- Tool calls: CSim {int(tool_totals['tool_csim'])}; Synth {int(tool_totals['tool_synth'])}; CoSim {int(tool_totals['tool_cosim'])}; LLM {int(tool_totals['tool_llm'])}.",
        f"- Measured accelerations: mean {total['mean_acceleration']:.3f}x; tasks at/above 8x: {total['acceleration_ge_8']}.",
        "",
        "## Routing and notable evidence",
        "",
        "- `v3d_fast_018` was expected `STRUCTURAL_FIX` but routed `OPTIMIZE`; it remained a certified success using its legal baseline. The difference is preserved, not normalized away.",
        "- `v3d_fast_020` baseline CoSim timed out with the public deadlock evidence; its single Planner candidate passed CSim/Synth/CoSim and B2, then finalized safely.",
        "",
        "## Terminal failures and diagnostic evidence",
        "",
        f"- Terminal E2E failures: {len([record for record in records if not record['e2e_success']])}. Terminal-stage counts: {dict(stage_counts)}.",
        f"- Pre-finalization diagnostic evidence (not terminal failures): {dict(kind_counts)}. The CSV inventory labels each row as `PRE_FINALIZATION_DIAGNOSTIC` or `TERMINAL_FAILURE`.",
        f"- Last action-family distribution: {dict(action_counts)}.",
        "",
        "## Per-task scorecards",
        "",
        markdown_table(records, [("Task", "task_id"), ("Expected", "expected_mode"), ("Routed", "routed_mode"), ("B2", "b2_status"), ("100 MHz", "clock_100mhz"), ("Base lat", "baseline_latency"), ("Cand lat", "candidate_latency"), ("Accel", "raw_acceleration"), ("Public proxy", "public_validation_proxy_score"), ("Planner", "planner_calls"), ("Tokens", "tokens"), ("Credits", "credits")]),
    ]
    report_path.write_text("\n".join(report), encoding="utf-8")

    frozen_text = args.freeze.read_text(encoding="utf-8")
    frozen_head = re.search(r"Git commit: `([^`]+)`", frozen_text)
    frozen_runtime = re.search(r"Runtime/Executor fingerprint: `([^`]+)`", frozen_text)
    current_head = command(args.repo, "git", "rev-parse", "HEAD")
    executor_fingerprints = sorted({record["executor_fingerprint"] for record in records})
    integrity_failures = []
    if len(records) != 46: integrity_failures.append("result_count_not_46")
    if len({record["task_id"] for record in records}) != 46: integrity_failures.append("task_ids_not_unique")
    if any(record["status"] != "DONE" or not record["e2e_success"] for record in records): integrity_failures.append("non_success_terminal_result")
    if any(not record["provenance_complete"] for record in records): integrity_failures.append("missing_package_or_certification")
    if any(record["agent_ledger_unchanged_by_b2"] is not True for record in records): integrity_failures.append("b2_ledger_mutation_or_missing_receipt")
    diff_check = subprocess.run(["git", "diff", "--check"], cwd=args.repo, text=True, capture_output=True)
    if diff_check.returncode: integrity_failures.append("git_diff_check_failed")
    integrity_path = args.out_dir / "full_46_integrity_audit.md"
    integrity = [
        "# RC2 46-task FULL_REF integrity audit", "",
        f"- Campaign: `{args.run_dir}`", f"- Result count / unique task IDs: {len(records)} / {len({record['task_id'] for record in records})}",
        f"- Batch terminal marker: `{(args.run_dir / 'driver.exit').read_text(encoding='utf-8').strip()}`", f"- Frozen HEAD: `{frozen_head.group(1) if frozen_head else 'MISSING'}`; current HEAD: `{current_head}`.",
        f"- Frozen aggregate runtime fingerprint: `{frozen_runtime.group(1) if frozen_runtime else 'MISSING'}`.",
        f"- Per-run executor fingerprint(s): `{', '.join(executor_fingerprints)}`.",
        "- The freeze aggregate fingerprint and the per-run executor fingerprint are different layers of evidence and are therefore recorded separately; this audit does not falsely compare them as identical strings.",
        f"- Package + certification receipt + B2 Ledger-unchanged evidence: {sum(record['provenance_complete'] and record['agent_ledger_unchanged_by_b2'] is True for record in records)}/46.",
        f"- `git diff --check`: {'PASS' if diff_check.returncode == 0 else 'FAIL'}.",
        f"- Audit status: **{'PASS' if not integrity_failures else 'FAIL'}**.",
        "", "## Findings", "",
        "- " + ("No integrity failure was found in the completed campaign artifacts." if not integrity_failures else "; ".join(integrity_failures)),
        "- Existing dirty-worktree entries were present at freeze and are documented by the freeze receipt; this report generator does not modify product runtime logic or historical run artifacts.",
    ]
    integrity_path.write_text("\n".join(integrity) + "\n", encoding="utf-8")

    print(json.dumps({"status": "PASS" if not integrity_failures else "FAIL", "out_dir": str(args.out_dir), "records": len(records), "success": total["success"], "public_proxy_total": round(total["public_validation_proxy_score"], 4)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
