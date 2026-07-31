#!/usr/bin/env python3
"""Create a secret-free, public-input reproducibility snapshot for RC2 FULL46.

The program does not invoke a provider or an HLS tool.  It intentionally
excludes every golden/hidden/reference-like path from the task-file manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any


PUBLIC_TASK_FILES = {
    "task.toml",
    "description.md",
    "kernel.cpp",
    "kernel.h",
    "kernel_tb.cpp",
    "acceptance.json",
    "mutation_manifest.json",
}
FORBIDDEN_PATH_COMPONENTS = {"answer", "golden", "hidden", "hidden_like", "reference"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=repo, text=True).strip()


def public_corpus_files(corpus: Path) -> list[Path]:
    selected: list[Path] = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(corpus)
        if any(part in FORBIDDEN_PATH_COMPONENTS for part in relative.parts):
            continue
        if relative.parts[0] == "schema" and path.suffix == ".json":
            selected.append(path)
        elif len(relative.parts) == 1 and path.name in {"README.md", "corpus_manifest.json"}:
            selected.append(path)
        elif len(relative.parts) >= 3 and relative.parts[0] == "tasks" and path.name in PUBLIC_TASK_FILES:
            selected.append(path)
    return selected


def copy_exact(source: Path, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    source_hash = digest(source)
    target_hash = digest(target)
    if source_hash != target_hash:
        raise RuntimeError(f"copy hash mismatch: {source}")
    return {"source": str(source), "snapshot": str(target), "sha256": source_hash, "size_bytes": source.stat().st_size}


def task_contract(corpus: Path) -> tuple[list[dict[str, Any]], Counter[str], set[str], set[float]]:
    tasks: list[dict[str, Any]] = []
    modes: Counter[str] = Counter()
    parts: set[str] = set()
    clocks: set[float] = set()
    for task_toml in sorted((corpus / "tasks").glob("*/task.toml")):
        data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
        mode = str(data.get("mode") or data.get("task_type") or "UNSPECIFIED")
        # The corpus manifests are the authority for mode labels; task.toml is
        # used here only for target and budget facts.
        target = data.get("target") or {}
        parts.add(str(target.get("part", "UNKNOWN")))
        clock = target.get("clock_ns")
        if isinstance(clock, (int, float)):
            clocks.add(float(clock))
        tasks.append({
            "task_id": data["task_id"],
            "task_toml": str(task_toml),
            "max_credits": data.get("max_credits", data.get("budget")),
            "max_tokens": data.get("max_tokens"),
            "requires_cosim": bool(data.get("requires_cosim", False)),
        })
        modes[mode] += 1
    return tasks, modes, parts, clocks


def manifest_tasks(corpus: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    data = json.loads((corpus / "corpus_manifest.json").read_text(encoding="utf-8"))
    tasks = list(data.get("tasks", []))
    if not tasks:
        raise RuntimeError(f"missing task list: {corpus}")
    modes = Counter(str(task["mode"]) for task in tasks)
    return tasks, modes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--official-scoring", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--vitis-root", required=True)
    parser.add_argument("--vitis-version", required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    harness = args.harness.resolve()
    run_dir = args.run_dir.resolve()
    snapshot = args.snapshot_dir.resolve()
    manifest = harness / "llm4hls_agent/config/track_a_rc2_structural_search_continuation_020.json"
    plan_path = run_dir / "benchmark_plan.json"
    if not manifest.is_file() or not plan_path.is_file() or not args.official_scoring.is_file():
        raise RuntimeError("required frozen input is missing")

    corpus_roots = [harness / "task_corpus/v3d-fast", harness / "task_corpus/v3d-expanded"]
    if any(not root.is_dir() for root in corpus_roots):
        raise RuntimeError("one or more corpus roots are missing")

    copied = {
        "official_scoring": copy_exact(args.official_scoring, snapshot / "official_scoring.py"),
        "full_agent_manifest": copy_exact(manifest, snapshot / "runtime_full_agent_manifest.json"),
        "benchmark_plan": copy_exact(plan_path, snapshot / "benchmark_plan.json"),
    }
    runtime_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    artifact_root = repo
    artifact_copies: dict[str, dict[str, Any]] = {}
    for name, entry in sorted((runtime_manifest.get("artifacts") or {}).items()):
        source = artifact_root / entry["path"]
        if not source.is_file():
            raise RuntimeError(f"runtime artifact missing: {name}: {source}")
        copied_entry = copy_exact(source, snapshot / "runtime_artifacts" / source.name)
        if copied_entry["sha256"] != entry["sha256"]:
            raise RuntimeError(f"runtime artifact hash mismatch: {name}")
        artifact_copies[name] = copied_entry

    public_files: list[dict[str, Any]] = []
    manifest_task_rows: list[dict[str, Any]] = []
    mode_counts: Counter[str] = Counter()
    parts: set[str] = set()
    clocks: set[float] = set()
    task_budget_rows: list[dict[str, Any]] = []
    for root in corpus_roots:
        corpus_tasks, corpus_modes = manifest_tasks(root)
        task_rows, _, corpus_parts, corpus_clocks = task_contract(root)
        task_index = {row["task_id"]: row for row in task_rows}
        for entry in corpus_tasks:
            task_id = entry["task_id"]
            if task_id not in task_index:
                raise RuntimeError(f"manifest task lacks public task.toml: {task_id}")
            manifest_task_rows.append({
                "corpus": root.name,
                "task_id": task_id,
                "mode": entry["mode"],
                "difficulty": entry["difficulty"],
                "requires_cosim": bool(entry.get("requires_cosim", task_index[task_id]["requires_cosim"])),
            })
            task_budget_rows.append({"corpus": root.name, **task_index[task_id]})
        mode_counts.update(corpus_modes)
        parts.update(corpus_parts)
        clocks.update(corpus_clocks)
        for path in public_corpus_files(root):
            public_files.append({
                "corpus": root.name,
                "path": str(path.relative_to(harness)),
                "sha256": digest(path),
                "size_bytes": path.stat().st_size,
            })

    if len(manifest_task_rows) != 46 or len({row["task_id"] for row in manifest_task_rows}) != 46:
        raise RuntimeError("expected exactly 46 unique manifest tasks")
    if any(component in FORBIDDEN_PATH_COMPONENTS for entry in public_files for component in Path(entry["path"]).parts):
        raise RuntimeError("forbidden task content entered snapshot manifest")

    report_root = repo / "docs/experiments/artifacts/2026-07-31-experiment-framework-enhancement"
    reports = [
        "experiment_freeze_46.md", "full_46_results.jsonl", "full_46_summary.csv",
        "full_46_scorecards.csv", "full_46_benchmark_report.md",
        "full_46_failure_inventory.csv", "full_46_integrity_audit.md",
    ]
    report_hashes = []
    for name in reports:
        path = report_root / name
        if not path.is_file():
            raise RuntimeError(f"frozen report missing: {name}")
        report_hashes.append({"path": str(path.relative_to(repo)), "sha256": digest(path), "size_bytes": path.stat().st_size})

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    config = plan["configuration"]
    config_snapshot = {
        "schema_version": "full46.reproducibility-config.v1",
        "evidence_commit": git(repo, "rev-parse", "HEAD"),
        "branch_at_snapshot": git(repo, "branch", "--show-current"),
        "provider": "openai-compatible",
        "model": config["models"],
        "backend": config["backend"],
        "components": {
            "A1": config["evidence_memory_mode"],
            "A2": config["continuation_policy_mode"],
            "A3": config["experience_mode"],
            "token_policy": "fixed",
            "B1": "on",
            "B2": "on",
        },
        "planner": {"max_rounds": config["max_planner_rounds"], "retry_failures": config["retry_failures"], "repeats": config["repeats"]},
        "budget": {"source": "each task.toml; no upward override", "task_entries": task_budget_rows},
        "timeouts": {"max_runtime_seconds": config["max_runtime_seconds"], "source": "per-tool runtime configuration captured in the benchmark plan and run artifacts"},
        "toolchain": {"vitis_root": args.vitis_root, "vitis_version": args.vitis_version, "parts": sorted(parts), "clock_ns": sorted(clocks), "minimum_frequency_mhz": 100},
        "full_agent_manifest_sha256": digest(manifest),
        "execution_policy_fingerprint": plan["execution_policy_fingerprint"],
        "executor_fingerprint": plan["executor_fingerprint"],
        "implementation_fingerprint": plan["implementation_fingerprint"],
    }
    (snapshot / "experiment_config_snapshot.json").write_text(json.dumps(config_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    snapshot_manifest = {
        "schema_version": "full46.reproducibility-snapshot.v1",
        "purpose": "recover frozen public inputs and independently verify the FULL46 evidence contract",
        "evidence_commit": git(repo, "rev-parse", "HEAD"),
        "branch_at_snapshot": git(repo, "branch", "--show-current"),
        "task_count": len(manifest_task_rows),
        "mode_counts": dict(sorted(mode_counts.items())),
        "tasks": sorted(manifest_task_rows, key=lambda row: row["task_id"]),
        "public_task_files": public_files,
        "excluded_path_components": sorted(FORBIDDEN_PATH_COMPONENTS),
        "copied_inputs": copied,
        "runtime_artifact_copies": artifact_copies,
        "frozen_report_hashes": report_hashes,
        "experiment_config_snapshot_sha256": digest(snapshot / "experiment_config_snapshot.json"),
    }
    (snapshot / "snapshot_manifest.json").write_text(json.dumps(snapshot_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "snapshot": str(snapshot), "tasks": len(manifest_task_rows), "public_files": len(public_files), "excluded_components": sorted(FORBIDDEN_PATH_COMPONENTS)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
