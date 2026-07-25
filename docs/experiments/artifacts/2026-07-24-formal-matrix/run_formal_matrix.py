#!/usr/bin/env python3
"""Run or resume the frozen public 28x1 DeepSeek+Vitis formal matrix."""

from __future__ import annotations

import argparse
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
MATRIX_RELATIVE_OUTPUT = Path(
    "llm4hls_harness/experiments/"
    "formal_matrix_20260724_4a05763_deepseek_28x1"
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


def terminal_coverage(
    records: list[dict[str, object]],
    *,
    expected_task_ids: list[str],
) -> dict[str, object]:
    by_task: Counter[str] = Counter(
        str(record.get("task_id")) for record in records
    )
    explicit = [
        record
        for record in records
        if isinstance(record.get("status"), str)
        and bool(str(record["status"]).strip())
        and isinstance(record.get("run_id"), str)
        and bool(str(record["run_id"]).strip())
    ]
    missing = sorted(set(expected_task_ids).difference(by_task))
    duplicates = sorted(task for task, count in by_task.items() if count != 1)
    complete = bool(
        len(records) == len(expected_task_ids)
        and len(explicit) == len(expected_task_ids)
        and not missing
        and not duplicates
    )
    return {
        "schema_version": "formal-matrix.terminal-coverage.v1",
        "status": "COMPLETE" if complete else "INCOMPLETE_RESUMABLE",
        "expected_slots": len(expected_task_ids),
        "terminal_slots": len(explicit),
        "missing_task_ids": missing,
        "non_unit_task_counts": duplicates,
        "status_counts": dict(
            sorted(
                Counter(str(record.get("status")) for record in records).items()
            )
        ),
        "e2e_success_count": sum(
            record.get("e2e_success") is True for record in records
        ),
        "e2e_failure_count": sum(
            record.get("e2e_success") is not True for record in records
        ),
        "retry_failures_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "llm4hls_harness"))
    from llm4hls_agent.v3_batch_benchmark import (
        BatchBenchmarkRunner,
        BenchmarkConfig,
        V3PrototypeCLIExecutor,
    )

    artifact_dir = Path(__file__).resolve().parent
    freeze_path = artifact_dir / "formal-matrix-freeze.json"
    preflight_path = artifact_dir / "per-task-token-preflight.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    gate = environment_gate()
    compatibility_patch = freeze.get(
        "orchestration_compatibility_patch", {}
    )
    compatibility_path = root / str(
        compatibility_patch.get("file", "")
    )
    invariant_checks = {
        "head_exact": head == FROZEN_HEAD,
        "freeze_ready": freeze.get("status") == "FROZEN_READY_FOR_ENV",
        "orchestration_compatibility_patch_exact": (
            compatibility_patch.get("status") == "ACCEPTED_EXACT"
            and compatibility_path.is_file()
            and sha256_file(compatibility_path)
            == compatibility_patch.get("expected_sha256")
        ),
        "preflight_pass": preflight.get("status") == "PASS",
        "preflight_hash_exact": (
            sha256_file(preflight_path)
            == freeze["token_preflight"]["sha256"]
        ),
        "runner_hash_exact": (
            sha256_file(Path(__file__).resolve())
            == freeze["runner"]["sha256"]
        ),
        "environment_ready": gate["status"] == "PASS",
    }
    if not all(invariant_checks.values()):
        print(
            json.dumps(
                {
                    "status": "PRELAUNCH_BLOCKED",
                    "checks": invariant_checks,
                    "environment": gate,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    output_dir = root / MATRIX_RELATIVE_OUTPUT
    results_exist = (
        output_dir / "benchmark_results.jsonl"
    ).is_file()
    resume = args.resume or results_exist
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
        resume=resume,
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
    expected = [str(item["task_id"]) for item in preflight["records"]]
    coverage = terminal_coverage(outcome.records, expected_task_ids=expected)
    write_json(output_dir / "terminal_coverage.json", coverage)
    print(json.dumps(coverage, ensure_ascii=False, sort_keys=True))
    return 0 if coverage["status"] == "COMPLETE" else 4


if __name__ == "__main__":
    raise SystemExit(main())
