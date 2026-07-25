#!/usr/bin/env python3
"""Freeze the post-P0-fix D3 28x1 plan without executing any slot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping


FROZEN_HEAD = "76698b02942369a937a9d6baf77c862cb06f49f2"
OLD_HEAD = "4a05763b593a527878a0056f64763126c58ee63b"
MODEL = "deepseek-v4-pro"
CORPUS_DIGEST = (
    "6343d3071b8c44a5e4bfc57e3812e59e6f553c587c9eb3707bc81034474f6cc6"
)
COMPATIBILITY_PATCH_SHA256 = (
    "22bd25029ba7a7d3e15b511685234fe32717873a5b746c9108e24154da75c4e9"
)
FORMAL_PREFLIGHT_SHA256 = (
    "e0640a379e9630d7065c203c0d1068f438cb166dca3131a3aa2e562a952a3d16"
)
OUTPUT_RELATIVE = Path(
    "llm4hls_harness/experiments/"
    "formal_matrix_20260724_76698b0_deepseek_28x1"
)
EXTRA_ARGS = (
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


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def git_output(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True
    ).strip()


def task_corpus_digest(root: Path, corpus: Path) -> str:
    lines: list[str] = []
    for path in sorted(item for item in corpus.rglob("*") if item.is_file()):
        lines.append(f"{sha256_file(path)}  {path.relative_to(root)}\n")
    return sha256_bytes("".join(lines).encode("utf-8"))


def option(command: list[str], name: str) -> str | None:
    try:
        index = command.index(name)
    except ValueError:
        return None
    if index + 1 >= len(command):
        return None
    return command[index + 1]


def main() -> int:
    root = Path(__file__).resolve().parents[4]
    artifact_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "llm4hls_harness"))
    from llm4hls_agent.v3_batch_benchmark import (
        BatchBenchmarkRunner,
        BenchmarkConfig,
        V3PrototypeCLIExecutor,
        _implementation_facts,
        _implementation_fingerprint,
        discover_tasks,
        select_models,
        select_tasks,
    )

    corpus = root / "llm4hls_harness/task_corpus/v3d-fast/tasks"
    old_matrix = (
        root
        / "llm4hls_harness/experiments/"
        "formal_matrix_20260724_4a05763_deepseek_28x1"
    )
    old_plan_path = old_matrix / "benchmark_plan.json"
    old_plan = json.loads(old_plan_path.read_text(encoding="utf-8"))
    old_by_task = {
        str(slot["task_id"]): slot for slot in old_plan["runs"]
    }
    preflight_path = (
        root
        / "docs/experiments/artifacts/2026-07-24-formal-matrix/"
        "per-task-token-preflight.json"
    )
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight_by_task = {
        str(record["task_id"]): record for record in preflight["records"]
    }

    old_commands = []
    for path in sorted((old_matrix / "runs").glob("*/benchmark_executor_command.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError(f"invalid first-round executor command: {path}")
        old_commands.append(value)
    expected_options = {
        "--backend": "vitis",
        "--planner": "openai-compatible",
        "--model": MODEL,
        "--validation-profile": "fast-experiment",
        "--experience-mode": "off",
        **{
            EXTRA_ARGS[index]: EXTRA_ARGS[index + 1]
            for index in range(0, len(EXTRA_ARGS), 2)
        },
    }
    command_protocol_exact = bool(
        len(old_commands) == 28
        and all(
            all(option(command, name) == value for name, value in expected_options.items())
            and "--experience-store" not in command
            for command in old_commands
        )
    )

    config = BenchmarkConfig(
        corpus=corpus,
        output_dir=root / OUTPUT_RELATIVE,
        models=(MODEL,),
        repeats=1,
        backend="vitis",
        splits=("all",),
        resume=False,
        retry_failures=False,
        max_runtime_seconds=None,
        validation_profile="fast-experiment",
        experience_mode="off",
        experience_store=None,
        experience_task_split=None,
    )
    executor = V3PrototypeCLIExecutor(
        extra_args=EXTRA_ARGS,
        validation_profile="fast-experiment",
        experience_mode="off",
        experience_store=None,
        experience_task_split=None,
    )
    runner = BatchBenchmarkRunner(config, executor=executor)
    descriptors = select_tasks(discover_tasks(config.corpus), config)
    models = select_models(config)
    planned = runner._plan(descriptors, models)
    slots = []
    for descriptor, model, repeat_index, fingerprint, run_id in planned:
        old_slot = old_by_task.get(descriptor.task_id)
        token_preflight = preflight_by_task.get(descriptor.task_id)
        if not isinstance(old_slot, Mapping) or not isinstance(
            token_preflight, Mapping
        ):
            raise RuntimeError(
                f"missing first-round binding for {descriptor.task_id}"
            )
        slots.append(
            {
                "slot_id": f"{descriptor.task_id}/repeat_{repeat_index:02d}",
                "task_id": descriptor.task_id,
                "repeat": repeat_index,
                "model": model,
                "expected_mode": descriptor.expected_mode,
                "requires_cosim": token_preflight.get("requires_cosim"),
                "task_credit_limit": token_preflight.get("task_credit_limit"),
                "task_fingerprint": descriptor.task_fingerprint,
                "first_round_task_fingerprint": old_slot.get(
                    "task_fingerprint"
                ),
                "run_fingerprint": fingerprint,
                "run_id": run_id,
                "terminal_required": True,
            }
        )

    vitis_run = Path(
        os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "")
    ).expanduser() / "bin/vitis-run"
    corpus_digest = task_corpus_digest(root, corpus)
    checks = {
        "head_exact": git_output(root, "rev-parse", "HEAD") == FROZEN_HEAD,
        "branch_is_not_main": git_output(root, "branch", "--show-current")
        not in {"", "main", "master"},
        "d1_product_committed_clean": subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                "HEAD",
                "--",
                "llm4hls_harness/llm4hls_agent/v3_prototype.py",
                "llm4hls_harness/tests/test_v3_terminal_latency_fix.py",
            ],
            cwd=root,
            check=False,
        ).returncode
        == 0,
        "corpus_committed_clean": subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                "HEAD",
                "--",
                "llm4hls_harness/task_corpus/v3d-fast/tasks",
            ],
            cwd=root,
            check=False,
        ).returncode
        == 0,
        "corpus_digest_exact": corpus_digest == CORPUS_DIGEST,
        "first_round_task_fingerprints_exact": all(
            slot["task_fingerprint"]
            == slot["first_round_task_fingerprint"]
            for slot in slots
        ),
        "first_round_executor_protocol_exact": command_protocol_exact,
        "compatibility_patch_exact": sha256_file(
            root
            / "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py"
        )
        == COMPATIBILITY_PATCH_SHA256,
        "formal_preflight_exact": sha256_file(preflight_path)
        == FORMAL_PREFLIGHT_SHA256,
        "slot_count_28": len(slots) == 28,
        "unique_slots_28": len({slot["slot_id"] for slot in slots}) == 28,
        "unique_tasks_28": len({slot["task_id"] for slot in slots}) == 28,
        "output_directory_fresh": not (root / OUTPUT_RELATIVE).exists(),
        "vitis_run_exact": (
            vitis_run.is_file()
            and sha256_file(vitis_run)
            == "4d1bf95564e127673e3fde65bdaa7f93dc35220d15af85c794d148517fd91fb3"
        ),
    }
    protocol = {
        "task_count": 28,
        "repeat": 1,
        "model": MODEL,
        "backend": "Vitis 2025.2",
        "continuation": "off",
        "experience": "off",
        "ranker": "off",
        "final_validation_policy": "task_contract",
        "token_policy": "fixed",
        "run_token_limit": 32768,
        "max_output_tokens": 4096,
        "max_planner_rounds": 2,
        "max_no_improvement_rounds": 2,
        "temperature": 0.0,
        "top_p": 1.0,
        "llm_timeout_seconds": 180,
        "tool_costs": {"csim": 1, "synth": 4, "cosim": 20},
        "tool_limits": {"csim": 5, "synth": 5, "cosim": 5, "llm": 2},
        "tool_timeouts_seconds": {
            "csim": 300,
            "synth": 900,
            "cosim": 600,
        },
        "run_timeout_seconds": 3600,
        "batch_timeout_seconds": None,
        "final_reserve_credits": 25,
        "minimum_frequency_mhz": 100,
        "validation_profile": "fast-experiment",
        "task_credit_limit_source": "task.toml budget",
        "primary_retry": False,
        "failure_isolating": True,
        "resumable": True,
        "max_parallel_runs": 1,
        "power": "UNSUPPORTED",
    }
    configuration_binding = {
        "commit_sha": FROZEN_HEAD,
        "corpus_digest": corpus_digest,
        "protocol": protocol,
        "implementation_fingerprint": _implementation_fingerprint(),
        "executor_fingerprint": runner.executor_fingerprint,
        "execution_policy_fingerprint": runner.execution_policy_fingerprint,
        "compatibility_patch_sha256": COMPATIBILITY_PATCH_SHA256,
        "vitis_run_sha256": sha256_file(vitis_run)
        if vitis_run.is_file()
        else None,
    }
    plan = {
        "schema_version": "phase-d3.formal-matrix-plan.v1",
        "phase": "D3",
        "status": "FROZEN_READY_FOR_KEY_ROTATION_GATE"
        if all(checks.values())
        else "FREEZE_BLOCKED",
        "commit_sha": FROZEN_HEAD,
        "parent_formal_head": OLD_HEAD,
        "corpus": str(corpus.relative_to(root)),
        "corpus_digest": corpus_digest,
        "corpus_digest_method": (
            "sha256 of sorted lines '<file_sha256>  <relative_path>\\n' "
            "for every file under the public v3d-fast task corpus"
        ),
        "task_count": 28,
        "repeat": 1,
        "model": MODEL,
        "backend": "Vitis 2025.2",
        "continuation": "off",
        "experience": "off",
        "ranker": "off",
        "final_validation_policy": "task_contract",
        "token_policy": "fixed",
        "primary_retry": False,
        "failure_isolating": True,
        "resumable": True,
        "temperature": 0.0,
        "top_p": 1.0,
        "max_planner_rounds": 2,
        "protocol": protocol,
        "configuration_fingerprint_sha256": sha256_json(
            configuration_binding
        ),
        "configuration_binding": configuration_binding,
        "implementation_facts": _implementation_facts(),
        "output_dir": str(OUTPUT_RELATIVE),
        "first_round_refs": {
            "head": OLD_HEAD,
            "plan": str(old_plan_path.relative_to(root)),
            "plan_sha256": sha256_file(old_plan_path),
            "formal_preflight": str(preflight_path.relative_to(root)),
            "formal_preflight_sha256": sha256_file(preflight_path),
            "executor_command_count": len(old_commands),
        },
        "compatibility_patch": {
            "file": "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py",
            "sha256": COMPATIBILITY_PATCH_SHA256,
            "committed_in_d1_fix": False,
            "scope": "BATCH_TERMINAL_PROVENANCE_VALIDATION_ONLY",
        },
        "vitis": {
            "version": "2025.2",
            "vitis_run_sha256": sha256_file(vitis_run)
            if vitis_run.is_file()
            else None,
            "backend_fingerprint": "llm4hls_agent.vitis.VitisBackend:v0.8",
        },
        "checks": checks,
        "slots": slots,
        "scope_guards": {
            "task_code_changed": False,
            "router_changed": False,
            "planner_prompt_changed": False,
            "token_policy_changed": False,
            "continuation_enabled": False,
            "experience_enabled": False,
            "ranker_enabled": False,
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "secret_value_recorded": False,
        },
    }
    plan_path = artifact_dir / "formal-matrix-plan.json"
    write_json(plan_path, plan)
    plan_sha256 = sha256_file(plan_path)
    (artifact_dir / "formal-matrix-plan.sha256").write_text(
        f"{plan_sha256}  formal-matrix-plan.json\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": plan["status"],
                "commit_sha": FROZEN_HEAD,
                "corpus_digest": corpus_digest,
                "configuration_fingerprint_sha256": plan[
                    "configuration_fingerprint_sha256"
                ],
                "plan_sha256": plan_sha256,
                "slots": len(slots),
                "checks": checks,
                "secret_value_recorded": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
