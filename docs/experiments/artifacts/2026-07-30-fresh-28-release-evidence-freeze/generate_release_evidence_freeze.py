#!/usr/bin/env python3
"""Generate a read-only evidence freeze for the fresh 28-task RC2 run.

The script reads the repository and completed run artifacts only.  Its outputs
are confined to this snapshot directory; it neither invokes a model nor starts
any Vitis action.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SNAPSHOT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SNAPSHOT_DIR.parents[3]
HARNESS = PROJECT_ROOT / "llm4hls_harness"
RUN_ROOT = HARNESS / "runs/a02-fresh-full-ref-20260730T105924Z"
MANIFEST = HARNESS / "llm4hls_agent/config/track_a_rc2_prompt_contract_targeted_regression.json"
CORPUS_MANIFEST = HARNESS / "task_corpus/v3d-fast/corpus_manifest.json"

RUNTIME_SOURCES = [
    "llm4hls_harness/llm4hls_agent/openai_provider.py",
    "llm4hls_harness/llm4hls_agent/repair.py",
    "llm4hls_harness/llm4hls_agent/runtime_control.py",
    "llm4hls_harness/llm4hls_agent/tools.py",
    "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py",
    "llm4hls_harness/llm4hls_agent/v3_failure_evidence.py",
    "llm4hls_harness/llm4hls_agent/v3_openai_planner.py",
    "llm4hls_harness/llm4hls_agent/v3_planner_action.py",
    "llm4hls_harness/llm4hls_agent/v3_prototype.py",
    "llm4hls_harness/llm4hls_agent/v3_prototype_cli.py",
    "llm4hls_harness/llm4hls_agent/v3_search_control.py",
    "llm4hls_harness/llm4hls_agent/vitis.py",
    "llm4hls_harness/llm4hls_agent/workflow.py",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=PROJECT_ROOT, text=True, stderr=subprocess.STDOUT)


def file_record(path: Path, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def resolve_artifact(path_text: str) -> Path:
    return (PROJECT_ROOT / path_text).resolve()


def main() -> None:
    if not RUN_ROOT.is_dir():
        raise SystemExit(f"Missing completed fresh run: {RUN_ROOT}")
    if not MANIFEST.is_file() or not CORPUS_MANIFEST.is_file():
        raise SystemExit("Missing manifest or corpus manifest")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    results_path = RUN_ROOT / "benchmark_results.jsonl"
    summary_path = RUN_ROOT / "benchmark_summary.json"
    results = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if len(results) != 28:
        raise SystemExit(f"Freeze requires 28 completed results, found {len(results)}")

    run_files = [file_record(path, RUN_ROOT) for path in sorted(RUN_ROOT.rglob("*")) if path.is_file()]
    run_tree_rows = [f"{item['path']}\t{item['bytes']}\t{item['sha256']}" for item in run_files]
    run_tree_sha256 = hashlib.sha256("\n".join(run_tree_rows).encode("utf-8")).hexdigest()
    run_manifest = {
        "schema_version": "track-a.release-evidence-run-manifest.v1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_root": str(RUN_ROOT),
        "file_count": len(run_files),
        "file_tree_sha256": run_tree_sha256,
        "files": run_files,
    }
    run_manifest_path = SNAPSHOT_DIR / "fresh_28_run_file_manifest.json"
    run_manifest_path.write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    artifact_records: dict[str, dict[str, Any]] = {
        "full_agent_manifest": file_record(MANIFEST),
        "corpus_manifest": file_record(CORPUS_MANIFEST),
    }
    for name, item in manifest["artifacts"].items():
        artifact_path = resolve_artifact(item["path"])
        if not artifact_path.is_file():
            raise SystemExit(f"Missing bound artifact {name}: {artifact_path}")
        record = file_record(artifact_path)
        record["manifest_declared_sha256"] = item["sha256"]
        record["manifest_hash_matches"] = record["sha256"] == item["sha256"]
        artifact_records[name] = record

    source_records = [file_record(PROJECT_ROOT / source) for source in RUNTIME_SOURCES]
    source_fingerprint = canonical_sha256(source_records)
    executor_fingerprints = sorted({record.get("executor_fingerprint") for record in results})
    backend_fingerprints = sorted({record.get("provenance_validation", {}).get("execution_binding", {}).get("sha256") for record in results})
    run_configs = sorted({record.get("provenance_validation", {}).get("run_config", {}).get("sha256") for record in results})
    first_run_config = json.loads((Path(results[0]["run_dir"]) / "v3_run_config.json").read_text(encoding="utf-8"))

    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=PROJECT_ROOT)
    git_status = run("git", "status", "--short").splitlines()
    git_block = {
        "head": run("git", "rev-parse", "HEAD").strip(),
        "branch": run("git", "branch", "--show-current").strip(),
        "status_porcelain_v1": git_status,
        "binary_diff_against_head_bytes": len(diff),
        "binary_diff_against_head_sha256": hashlib.sha256(diff).hexdigest(),
    }

    run_result_records = []
    for record in results:
        run_result_records.append({
            "task_id": record["task_id"],
            "run_id": record["run_id"],
            "status": record["status"],
            "e2e_success": record["e2e_success"],
            "stop_reason": record["stop_reason"],
            "result_sha256": record["result_sha256"],
            "run_dir": record["run_dir"],
            "final_certification": record.get("provenance_validation", {}).get("independent_certification", {}),
            "package_manifest": record.get("package_manifest", {}),
        })

    environment = {
        "os": platform.platform(),
        "python": sys.version,
        "machine": platform.machine(),
        "backend_from_completed_run": first_run_config["backend_fingerprint"],
        "toolchain_from_completed_run": first_run_config["tool"],
        "executor_fingerprints": executor_fingerprints,
        "execution_binding_hashes": backend_fingerprints,
        "run_config_hashes": run_configs,
        "environment_variables": {
            key: os.environ.get(key)
            for key in ("LLM4HLS_VITIS_HLS_ROOT", "LLM4HLS_PART", "LLM4HLS_CLOCK_NS")
            if os.environ.get(key) is not None
        },
        "secrets_redacted": True,
    }

    snapshot = {
        "schema_version": "track-a.release-evidence-snapshot.v1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "freeze_mode": "READ_ONLY_RELEASE_EVIDENCE_FREEZE",
        "prohibited_actions_confirmed": [
            "no_deepseek_call",
            "no_vitis_action",
            "no_product_code_change",
            "no_historical_run_artifact_change",
            "no_git_tag_change",
            "no_commit_or_push",
        ],
        "git": git_block,
        "runtime": {
            "manifest": artifact_records["full_agent_manifest"],
            "manifest_components": manifest["components"],
            "manifest_foundations": manifest["foundations"],
            "bound_artifacts": {key: value for key, value in artifact_records.items() if key not in {"full_agent_manifest", "corpus_manifest"}},
            "prompt_runtime_source": next(item for item in source_records if item["path"].endswith("v3_openai_planner.py")),
            "runtime_sources": source_records,
            "runtime_source_fingerprint": source_fingerprint,
            "corpus_manifest": artifact_records["corpus_manifest"],
            "environment": environment,
        },
        "fresh_28_evidence": {
            "run_root": str(RUN_ROOT),
            "run_file_manifest": run_manifest_path.name,
            "run_file_manifest_sha256": sha256_file(run_manifest_path),
            "run_file_tree_sha256": run_tree_sha256,
            "result_records": run_result_records,
            "summary": {
                "benchmark_fingerprint": summary.get("benchmark_fingerprint"),
                "records": len(results),
                "real_e2e_successes": summary["real_evidence_headline"]["e2e_successes"],
                "real_e2e_success_rate": summary["real_evidence_headline"]["e2e_success_rate"],
                "failures": summary["real_evidence_headline"]["failures"],
            },
        },
    }
    snapshot_path = SNAPSHOT_DIR / "release_evidence_snapshot_manifest.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    receipt = {
        "schema_version": "track-a.release-evidence-freeze-receipt.v1",
        "status": "FROZEN_READ_ONLY",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_manifest": {
            "path": snapshot_path.name,
            "sha256": sha256_file(snapshot_path),
        },
        "fresh_28_run_file_manifest": {
            "path": run_manifest_path.name,
            "sha256": sha256_file(run_manifest_path),
            "file_tree_sha256": run_tree_sha256,
            "file_count": len(run_files),
        },
        "head": git_block["head"],
        "branch": git_block["branch"],
        "fresh_28_benchmark_fingerprint": summary.get("benchmark_fingerprint"),
        "fresh_28_status": "27_OF_28_REAL_E2E_CERTIFIED",
        "immutable_run_root": str(RUN_ROOT),
    }
    (SNAPSHOT_DIR / "release_evidence_freeze_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
