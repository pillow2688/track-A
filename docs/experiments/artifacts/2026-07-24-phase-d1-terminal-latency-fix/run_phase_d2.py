#!/usr/bin/env python3
"""Run exactly the four Phase D2 P0 regression slots on a frozen patch snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path


FROZEN_HEAD = "4a05763b593a527878a0056f64763126c58ee63b"
EXPECTED_MODEL = "deepseek-v4-pro"
EXPECTED_BASE_URL = "https://api.deepseek.com"
EXPECTED_VITIS_ROOT = Path(
    "/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis"
)
TASK_IDS = (
    "v3d_fast_016",
    "v3d_fast_020",
    "v3d_fast_021",
    "v3d_fast_022",
)
OUTPUT_RELATIVE = Path(
    "llm4hls_harness/runs/"
    "phase-d2-4a05763-d1cad2cd88-20260724T124625Z"
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def git_head(root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


def environment_gate() -> dict[str, object]:
    base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    model = os.environ.get("LLM4HLS_MODEL", "")
    key_present = bool(os.environ.get("OPENAI_API_KEY", ""))
    vitis_root = Path(
        os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "")
    ).expanduser()
    toolchain = os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2")
    checks = {
        "base_url_exact": base_url == EXPECTED_BASE_URL,
        "api_key_present_not_recorded": key_present,
        "model_exact": model == EXPECTED_MODEL,
        "vitis_root_exact": (
            bool(str(vitis_root))
            and vitis_root.resolve() == EXPECTED_VITIS_ROOT.resolve()
        ),
        "vitis_run_present": (vitis_root / "bin/vitis-run").is_file(),
        "toolchain_exact": toolchain == "Vitis 2025.2",
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
    }


def frozen_files_gate(
    root: Path, frozen_files: dict[str, str]
) -> dict[str, object]:
    mismatches: list[str] = []
    for relative, expected in sorted(frozen_files.items()):
        path = root / relative
        if not path.is_file() or sha256_file(path) != expected:
            mismatches.append(relative)
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "checked_file_count": len(frozen_files),
        "mismatches": mismatches,
    }


def terminal_coverage(
    records: list[dict[str, object]],
) -> dict[str, object]:
    by_task: Counter[str] = Counter(
        str(record.get("task_id")) for record in records
    )
    explicit = [
        record
        for record in records
        if str(record.get("task_id", "")) in TASK_IDS
        and str(record.get("status", "")).upper()
        in {"DONE", "FAILED", "ERROR"}
        and isinstance(record.get("run_id"), str)
        and bool(str(record["run_id"]).strip())
    ]
    missing = sorted(set(TASK_IDS).difference(by_task))
    unexpected = sorted(set(by_task).difference(TASK_IDS))
    duplicates = sorted(task for task, count in by_task.items() if count != 1)
    complete = bool(
        len(records) == len(TASK_IDS)
        and len(explicit) == len(TASK_IDS)
        and not missing
        and not unexpected
        and not duplicates
    )
    return {
        "schema_version": "phase-d2.terminal-coverage.v1",
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "expected_task_ids": list(TASK_IDS),
        "expected_slots": len(TASK_IDS),
        "terminal_slots": len(explicit),
        "missing_task_ids": missing,
        "unexpected_task_ids": unexpected,
        "non_unit_task_counts": duplicates,
        "status_counts": dict(
            sorted(
                Counter(str(record.get("status")) for record in records).items()
            )
        ),
        "primary_retry_enabled": False,
    }


def main() -> int:
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "llm4hls_harness"))
    from llm4hls_agent.v3_batch_benchmark import (
        BatchBenchmarkRunner,
        BenchmarkConfig,
        V3PrototypeCLIExecutor,
    )

    artifact_dir = Path(__file__).resolve().parent
    freeze_path = artifact_dir / "phase-d2-frozen-snapshot.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    environment = environment_gate()
    frozen_files = {
        str(key): str(value)
        for key, value in dict(freeze["frozen_files"]).items()
    }
    files_before = frozen_files_gate(root, frozen_files)
    output_dir = root / OUTPUT_RELATIVE
    preflight_path = (
        root
        / "docs/experiments/artifacts/2026-07-24-formal-matrix/"
        "per-task-token-preflight.json"
    )
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight_by_task = {
        str(record["task_id"]): record for record in preflight["records"]
    }
    task_preflight = {
        task_id: preflight_by_task.get(task_id) for task_id in TASK_IDS
    }
    invariant_checks = {
        "snapshot_frozen": freeze.get("status")
        == "FROZEN_UNCOMMITTED_FIX_SNAPSHOT",
        "head_exact": git_head(root) == FROZEN_HEAD,
        "runner_hash_exact": (
            sha256_file(Path(__file__).resolve())
            == freeze["runner"]["sha256"]
        ),
        "formal_preflight_hash_exact": (
            sha256_file(preflight_path)
            == freeze["formal_preflight"]["sha256"]
        ),
        "four_task_preflight_present": all(
            isinstance(record, dict) for record in task_preflight.values()
        ),
        "four_task_preflight_pass": all(
            isinstance(record, dict) and record.get("status") == "PASS"
            for record in task_preflight.values()
        ),
        "frozen_files_exact": files_before["status"] == "PASS",
        "environment_ready": environment["status"] == "PASS",
        "fresh_output_directory": not output_dir.exists(),
    }
    prelaunch = {
        "schema_version": "phase-d2.prelaunch.v1",
        "status": (
            "PASS" if all(invariant_checks.values()) else "PRELAUNCH_BLOCKED"
        ),
        "checks": invariant_checks,
        "environment": environment,
        "frozen_files": files_before,
        "frozen_head": FROZEN_HEAD,
        "output_dir": str(output_dir),
        "task_ids": list(TASK_IDS),
        "secret_value_recorded": False,
    }
    write_json(artifact_dir / "phase-d2-prelaunch.json", prelaunch)
    if prelaunch["status"] != "PASS":
        print(
            json.dumps(prelaunch, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 3

    extra_args = (
        "--run-token-limit",
        "32768",
        "--token-budget-policy",
        "fixed",
        "--llm-timeout",
        "180",
        "--llm-max-output-tokens",
        "4096",
        "--llm-temperature",
        "0.0",
        "--llm-top-p",
        "1.0",
        "--max-planner-rounds",
        "2",
        "--final-reserve-credits",
        "25",
        "--max-no-improvement-rounds",
        "2",
        "--continuation-policy",
        "off",
        "--final-validation-policy",
        "task_contract",
        "--toolchain-id",
        "Vitis 2025.2",
        "--cost-csim",
        "1",
        "--cost-synth",
        "4",
        "--cost-cosim",
        "20",
        "--runtime-limit",
        "3600",
        "--csim-timeout",
        "300",
        "--synth-timeout",
        "900",
        "--cosim-timeout",
        "600",
        "--minimum-frequency-mhz",
        "100",
    )
    config = BenchmarkConfig(
        corpus=root / "llm4hls_harness/task_corpus/v3d-fast/tasks",
        output_dir=output_dir,
        models=(EXPECTED_MODEL,),
        repeats=1,
        backend="vitis",
        splits=("all",),
        task_filters=TASK_IDS,
        resume=False,
        retry_failures=False,
        max_runtime_seconds=None,
        validation_profile="fast-experiment",
        experience_mode="off",
        experience_store=None,
        experience_task_split=None,
    )
    executor = V3PrototypeCLIExecutor(
        extra_args=extra_args,
        validation_profile="fast-experiment",
        experience_mode="off",
        experience_store=None,
        experience_task_split=None,
    )
    outcome = BatchBenchmarkRunner(config, executor=executor).run()
    coverage = terminal_coverage(outcome.records)
    files_after = frozen_files_gate(root, frozen_files)
    postrun_checks = {
        "head_unchanged": git_head(root) == FROZEN_HEAD,
        "frozen_files_unchanged": files_after["status"] == "PASS",
        "terminal_coverage_complete": coverage["status"] == "COMPLETE",
    }
    postrun = {
        "schema_version": "phase-d2.postrun.v1",
        "status": "PASS" if all(postrun_checks.values()) else "FAIL",
        "checks": postrun_checks,
        "coverage": coverage,
        "frozen_files": files_after,
        "secret_value_recorded": False,
    }
    write_json(output_dir / "terminal_coverage.json", coverage)
    write_json(artifact_dir / "phase-d2-postrun.json", postrun)
    print(json.dumps(postrun, ensure_ascii=False, sort_keys=True))
    return 0 if postrun["status"] == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
