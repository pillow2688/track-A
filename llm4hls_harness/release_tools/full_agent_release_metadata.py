"""Patch combined coverage metadata and export a reproducible release snapshot.

This module is deliberately outside :mod:`llm4hls_agent`.  It reads completed
run evidence and emits reports/metadata only; it must never enter the Agent
runtime, call a model provider, or launch an HLS tool.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit


CLI_POLICY_INTERFACE_VERSION = "v2"
EFFECTIVE_POLICY_VERSION = "v3.continuation-policy.v3"
DECISION_SCHEMA = "v3.continuation-decision.v2"
COMBINED_MARKER = "COMBINED_COVERAGE_NOT_SINGLE_BATCH_28X1"
COMBINED_STATUS = "COMPLETE_LATEST_VALIDATED_COMBINED_COVERAGE"
TARGETED_RERUN_TASK_IDS = (
    "v3d_fast_012",
    "v3d_fast_018",
    "v3d_fast_020",
)
FINAL_CERTIFICATION_STAGES = ("csim", "synth", "cosim")
FIRST_ATTEMPT_DEFINITION = (
    "FIRST_QUALIFYING_AGENT_ATTEMPT_PER_TASK_EXCLUDING_EXECUTOR_PREFLIGHT"
)
SNAPSHOT_SCHEMA = "v3d.full-agent-release-snapshot.v1"

RUNTIME_SUFFIXES = {".py", ".json", ".jsonl", ".toml", ".yaml", ".yml"}
PUBLIC_TASK_NAMES = {"task.toml", "acceptance.json", "description.md"}
PUBLIC_TASK_SUFFIXES = {".cpp", ".h"}
DEPENDENCY_LOCK_NAMES = {
    "requirements-v3.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "pyproject.toml",
}
SECRET_ENV_NAMES = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "API_KEY",
    "PASSWORD",
    "SECRET",
    "AUTH_TOKEN",
    "ACCESS_TOKEN",
    "LICENSE_SERVER",
    "XILINXD_LICENSE_FILE",
    "LM_LICENSE_FILE",
}
RELEVANT_ENV_NAMES = (
    "LLM4HLS_VITIS_HLS_ROOT",
    "LLM4HLS_TOOLCHAIN_ID",
    "LLM4HLS_PART",
    "LLM4HLS_CLOCK_NS",
    "LLM4HLS_LLM_TIMEOUT_S",
    "LLM4HLS_LLM_MAX_OUTPUT_TOKENS",
    "LLM4HLS_MODEL",
    "LLM4HLS_CREDIT_BUDGET",
    "LLM4HLS_TOKEN_BUDGET",
    "LLM4HLS_COST_CSIM",
    "LLM4HLS_COST_SYNTH",
    "LLM4HLS_COST_COSIM",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
)
OVERLAY_EXACT_PATHS = {
    "llm4hls_harness/llm4hls_agent/config/track_a_full_agent_v1.json",
    "llm4hls_harness/llm4hls_agent/final_certification.py",
    "llm4hls_harness/llm4hls_agent/final_certification_cli.py",
    "llm4hls_harness/llm4hls_agent/full_agent_manifest.py",
    "llm4hls_harness/llm4hls_agent/runtime_control.py",
    "llm4hls_harness/llm4hls_agent/token_policy_experiment_cli.py",
    "llm4hls_harness/llm4hls_agent/v3_continuation_offline_gate.py",
    "llm4hls_harness/tests/test_executor_runtime.py",
    "llm4hls_harness/tests/test_final_certification.py",
    "llm4hls_harness/tests/test_track_a_full_agent.py",
    "llm4hls_harness/tests/test_v3_continuation_offline_gate.py",
    "llm4hls_harness/tests/test_release_snapshot_reporting.py",
    (
        "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
        "a2/continuation-v3-gate.json"
    ),
    (
        "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
        "a2/continuation-v3-admission.json"
    ),
    (
        "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
        "a3/train-only-experience-store.jsonl"
    ),
    (
        "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
        "a3/ranker-v3-gate.json"
    ),
    (
        "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
        "a3/admission.json"
    ),
}
OVERLAY_PREFIXES = (
    "llm4hls_harness/release_tools/",
)
EXCLUSION_RULES = (
    {
        "rule": "EXCLUDE_RUN_EVIDENCE_CONTENT",
        "patterns": ["llm4hls_harness/runs/**", "**/actions/**/work/**"],
        "reason": "Run evidence is content-addressed by run_evidence_manifest.json.",
    },
    {
        "rule": "EXCLUDE_SECRETS",
        "patterns": [".env", ".env.*", "**/*.pem", "**/*.key", "**/*.p12"],
        "reason": "Credentials and license material never enter a release snapshot.",
    },
    {
        "rule": "EXCLUDE_TOOL_AND_MODEL_STATE",
        "patterns": [
            ".venv/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "**/.cache/**",
            "**/vitis/**",
            "**/xsim.dir/**",
        ],
        "reason": "Generated environments, caches and HLS work trees are not source.",
    },
    {
        "rule": "EXCLUDE_UNRELATED_PROJECT_TREES",
        "patterns": ["min/**", "**/hidden/**", "**/reference/**", "**/golden/**"],
        "reason": (
            "These trees are excluded from the runtime-critical allow-list and "
            "untracked overlay. Any tracked edits remain in the required full "
            "working_tree.patch."
        ),
    },
)


class ReleaseMetadataError(RuntimeError):
    """Raised when report or snapshot integrity fails closed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseMetadataError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseMetadataError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def content_record(path: Path, *, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ReleaseMetadataError(f"path is outside repository root: {path}") from exc
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_symlink() or not path.is_file():
        raise ReleaseMetadataError(f"snapshot source must be a regular file: {path}")
    return {
        "path": relative,
        "size_bytes": path.stat().st_size,
        "mode": f"{mode:04o}",
        "sha256": sha256_file(path),
    }


def aggregate_content_records(records: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    normalized = sorted(
        (
            str(record["path"]),
            int(record["size_bytes"]),
            str(record["sha256"]),
        )
        for record in records
    )
    for path, size_bytes, file_hash in normalized:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseMetadataError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def git_text(root: Path, *args: str) -> str:
    return git_bytes(root, *args).decode("utf-8", errors="strict")


def repository_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ReleaseMetadataError(f"path is outside repository: {path}") from exc


def absolute_preserving_symlinks(path: Path) -> Path:
    """Make a CLI path absolute without resolving a virtualenv interpreter."""

    return Path(os.path.abspath(path))


def _history_guard(report: Mapping[str, Any]) -> str:
    projection = []
    for task in report.get("tasks", []):
        projection.append(
            {
                "task_id": task.get("task_id"),
                "first_attempt_result": task.get("first_attempt_result"),
                "attempt_history": task.get("attempt_history"),
                "targeted_rerun_reason": task.get("targeted_rerun_reason"),
                "prior_2026_07_27_coverage_state": task.get(
                    "prior_2026_07_27_coverage_state"
                ),
                "original_2026_07_27_coverage_record": task.get(
                    "original_2026_07_27_coverage_record"
                ),
            }
        )
    return sha256_bytes(canonical_json_bytes(projection))


def _normalize_a2(a2: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(a2))
    result.pop("configured_api_version", None)
    result["cli_policy_interface_version"] = CLI_POLICY_INTERFACE_VERSION
    result["effective_policy_version"] = EFFECTIVE_POLICY_VERSION
    result["decision_schema"] = DECISION_SCHEMA
    decisions = []
    for raw in result.get("decisions", []):
        if not isinstance(raw, Mapping):
            raise ReleaseMetadataError("A2 decision must be an object")
        decision = copy.deepcopy(dict(raw))
        decision.pop("configured_api_version", None)
        decision["cli_policy_interface_version"] = CLI_POLICY_INTERFACE_VERSION
        decision["effective_policy_version"] = EFFECTIVE_POLICY_VERSION
        decision["decision_schema"] = DECISION_SCHEMA
        decision.setdefault("planner_called", decision.get("decision") == "ALLOW")
        decisions.append(decision)
    result["decisions"] = decisions
    if "finalized_by_policy" in result and "finalized_by_learning_policy" not in result:
        result["finalized_by_learning_policy"] = result.pop("finalized_by_policy")
    result.setdefault("finalized_by_learning_policy", False)
    return result


def patch_combined_report(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized report without mutating historical attempt records."""

    report = copy.deepcopy(dict(source))
    tasks = report.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 28:
        raise ReleaseMetadataError("combined report must contain exactly 28 tasks")
    if len({task.get("task_id") for task in tasks if isinstance(task, Mapping)}) != 28:
        raise ReleaseMetadataError("combined report task IDs must be unique")
    history_before = _history_guard(report)

    first_successes = 0
    latest_successes = 0
    first_failed: list[str] = []
    latest_failed: list[str] = []
    route_mismatches: list[dict[str, Any]] = []
    a2_decision_count = 0
    a2_false_blocks = 0
    a2_unsafe_finalizes = 0
    a3_injection_mismatches = 0

    for task in tasks:
        if not isinstance(task, dict):
            raise ReleaseMetadataError("combined task record must be an object")
        task_id = str(task.get("task_id"))
        first = task.get("first_attempt_result")
        latest = task.get("latest_validated_result")
        if not isinstance(first, Mapping) or not isinstance(latest, dict):
            raise ReleaseMetadataError(f"attempt records missing for {task_id}")
        if first.get("strict_success") is True:
            first_successes += 1
        else:
            first_failed.append(task_id)
        if latest.get("strict_success") is True:
            latest_successes += 1
        else:
            latest_failed.append(task_id)

        a2 = latest.get("a2")
        if not isinstance(a2, Mapping):
            raise ReleaseMetadataError(f"A2 record missing for {task_id}")
        latest["a2"] = _normalize_a2(a2)
        task["a2_version_contract"] = {
            "cli_policy_interface_version": CLI_POLICY_INTERFACE_VERSION,
            "effective_policy_version": EFFECTIVE_POLICY_VERSION,
            "decision_schema": DECISION_SCHEMA,
        }
        a2_decision_count += len(latest["a2"]["decisions"])
        a2_false_blocks += int(
            bool(latest["a2"].get("false_block_suspected"))
        )
        a2_unsafe_finalizes += int(
            bool(latest["a2"].get("unsafe_finalize_suspected"))
        )

        a3 = latest.get("a3")
        if not isinstance(a3, Mapping):
            raise ReleaseMetadataError(f"A3 record missing for {task_id}")
        a3_injection_mismatches += int(a3.get("injection_mismatches", 0))

        routing = latest.get("routing")
        if not isinstance(routing, dict):
            raise ReleaseMetadataError(f"routing record missing for {task_id}")
        expected_mode = routing.get("expected_mode") or task.get("expected_mode")
        routed_mode = routing.get("routed_mode")
        route_match = expected_mode == routed_mode
        routing["expected_mode"] = expected_mode
        routing["routed_mode"] = routed_mode
        routing["route_match"] = route_match
        routing["expected_mode_match"] = route_match
        routing["runtime_gate_consistent"] = True
        routing["router_correct"] = route_match
        if not route_match:
            mismatch = {
                "task_id": task_id,
                "expected_mode": expected_mode,
                "routed_mode": routed_mode,
                "route_match": False,
                "runtime_gate_consistent": True,
                "runtime_route_basis": "BASELINE_VALIDATION_PASSED",
                "baseline_real_results": {
                    "csim": "PASS",
                    "synth": "PASS",
                    "cosim": "PASS",
                },
                "explanation": (
                    "The task fixture expects STRUCTURAL_FIX, while this run's "
                    "real baseline CSim, Synth and required CoSim all passed; "
                    "the runtime therefore routed to OPTIMIZE. The Router is "
                    "not modified and the mismatch remains visible."
                ),
            }
            route_mismatches.append(mismatch)
            routing.update(mismatch)

    if first_successes != 25 or first_failed != list(TARGETED_RERUN_TASK_IDS):
        raise ReleaseMetadataError(
            "first-attempt strict coverage must be 25/28 with failures "
            "012, 018 and 020"
        )
    if latest_successes != 28 or latest_failed:
        raise ReleaseMetadataError("latest validated strict coverage must be 28/28")
    if a2_decision_count != 48:
        raise ReleaseMetadataError(f"expected 48 A2 decisions, got {a2_decision_count}")
    if a2_false_blocks or a2_unsafe_finalizes or a3_injection_mismatches:
        raise ReleaseMetadataError(
            "no-harm counters changed: "
            f"A2 false block={a2_false_blocks}, "
            f"A2 unsafe finalize={a2_unsafe_finalizes}, "
            f"A3 injection mismatch={a3_injection_mismatches}"
        )
    if [item["task_id"] for item in route_mismatches] != ["v3d_fast_018"]:
        raise ReleaseMetadataError("018 must be the only expected/runtime mode mismatch")

    report["schema_version"] = "v3d.fast-28-combined.v3"
    report["status"] = COMBINED_STATUS
    report["coverage_complete"] = True
    report["all_tasks_pass"] = True
    markers = list(report.get("markers", []))
    if COMBINED_MARKER not in markers:
        markers.append(COMBINED_MARKER)
    report["markers"] = markers
    report["targeted_rerun_task_ids"] = list(TARGETED_RERUN_TASK_IDS)
    report["coverage_metrics"] = {
        "FIRST_ATTEMPT_STRICT_SUCCESS": {
            "successes": 25,
            "denominator": 28,
            "rate": 25 / 28,
        },
        "LATEST_VALIDATED_COMBINED_COVERAGE": {
            "successes": 28,
            "denominator": 28,
            "rate": 1.0,
        },
    }

    configuration = report.setdefault("configuration", {})
    configuration["a2"] = _normalize_a2(configuration.get("a2", {}))
    configuration["a2"].pop("decisions", None)
    coverage_identity = report.setdefault("coverage_identity", {})
    coverage_identity.update(
        {
            "classification": COMBINED_MARKER,
            "single_batch_28x1": False,
            "single_fingerprint_causal_ablation": False,
            "first_attempts_preserved": True,
            "targeted_rerun_history_preserved": True,
            "targeted_rerun_task_ids": list(TARGETED_RERUN_TASK_IDS),
        }
    )

    summary = report.setdefault("summary", {})
    summary["coverage"] = {
        "expected_tasks": 28,
        "first_attempt": {
            "label": "FIRST_ATTEMPT_STRICT_SUCCESS",
            "definition": FIRST_ATTEMPT_DEFINITION,
            "attempted": 28,
            "strict_successes": 25,
            "failed": 3,
            "failed_task_ids": list(TARGETED_RERUN_TASK_IDS),
            "strict_success_rate": 25 / 28,
            "executor_preflight_attempts_excluded": 3,
        },
        "latest_validated_combined": {
            "label": "LATEST_VALIDATED_COMBINED_COVERAGE",
            "attempted": 28,
            "strict_successes": 28,
            "failed": 0,
            "failed_task_ids": [],
            "strict_success_rate": 1.0,
        },
        "targeted_rerun_task_ids": list(TARGETED_RERUN_TASK_IDS),
        "single_batch_28x1": False,
    }
    summary_a2 = summary.setdefault("a2", {})
    summary_a2.update(
        {
            "cli_policy_interface_version": CLI_POLICY_INTERFACE_VERSION,
            "effective_policy_version": EFFECTIVE_POLICY_VERSION,
            "decision_schema": DECISION_SCHEMA,
            "decision_count": a2_decision_count,
            "false_block_suspected": a2_false_blocks,
            "unsafe_finalize_suspected": a2_unsafe_finalizes,
        }
    )
    summary_a3 = summary.setdefault("a3", {})
    summary_a3["injection_mismatches"] = a3_injection_mismatches
    summary["routing"] = {
        "expected_mode_matches": 27,
        "total": 28,
        "rate": 27 / 28,
        "mismatches": route_mismatches,
    }

    report["evidence_boundaries"] = {
        "no_harm_evidence": {
            "latest_validated_combined_coverage": {
                "strict_successes": 28,
                "denominator": 28,
                "rate": 1.0,
            },
            "a2_false_block": 0,
            "a2_unsafe_finalize": 0,
            "a3_injection_mismatch": 0,
        },
        "causal_benefit_evidence": {
            "same_implementation_and_executor_fingerprint_paired_ablation": False,
            "off_shadow_pairing_available": False,
            "supported_claims": [],
            "unsupported_claims": [
                "A2 or A3 caused a success-rate increase",
                "A2 or A3 caused Token usage to decrease",
                "A2 or A3 caused Planner calls to decrease",
            ],
            "reason": (
                "No off/shadow paired ablation exists under one identical "
                "runtime implementation and Executor fingerprint."
            ),
        },
    }
    release_decision = report.setdefault("release_decision", {})
    release_decision.update(
        {
            "threshold_rule": "LATEST_VALIDATED_STRICT_SUCCESS_GTE_25_OF_28",
            "evaluation_basis": "LATEST_VALIDATED_COMBINED_COVERAGE",
            "observed": 28,
            "denominator": 28,
            "result": "ENTER_RELEASE_FREEZE",
        }
    )
    report.setdefault("outputs", {})["release_snapshot"] = (
        "docs/experiments/artifacts/2026-07-28-full-agent-release-snapshot"
    )
    report["report_normalization"] = {
        "schema_version": "v3d.report-normalization.v1",
        "legacy_original_coverage_records_preserved_verbatim": True,
        "attempt_history_guard_sha256": history_before,
        "a2_version_source": (
            "CLI interface comes from the configured v2 compatibility option; "
            "effective policy and decision schema come from call-gate/admission "
            "evidence."
        ),
    }
    if _history_guard(report) != history_before:
        raise ReleaseMetadataError("attempt or original coverage history changed")
    return report


def _format_percent(numerator: int, denominator: int) -> str:
    return f"{100.0 * numerator / denominator:.2f}%"


def render_combined_markdown(report: Mapping[str, Any]) -> str:
    tasks = report["tasks"]
    usage = report["summary"]["usage"]
    by_mode = report["summary"]["by_expected_mode"]
    a2 = report["summary"]["a2"]
    a3 = report["summary"]["a3"]
    performance = report["summary"]["performance"]
    proxy = report["summary"]["official_score_proxy"]
    rows = []
    for task in tasks:
        latest = task["latest_validated_result"]
        routing = latest["routing"]
        task_a2 = latest["a2"]
        task_a3 = latest["a3"]
        perf = latest["performance"]
        raw_acceleration = (
            f"{perf['raw_acceleration']:.4f}×"
            if task["expected_mode"] == "OPTIMIZE"
            else "—"
        )
        rows.append(
            "| {task} | {expected} | {routed} | {match} | {agent} | "
            "{candidate} | {planner} | {tokens} | {credits} | {a2} | "
            "{a3} | {b2} | {clock:.3f} ns | {accel} |".format(
                task=task["task_id"],
                expected=routing["expected_mode"],
                routed=routing["routed_mode"],
                match=str(routing["route_match"]).lower(),
                agent=latest["agent"]["status"],
                candidate=latest["agent"]["final_candidate"],
                planner=latest["usage"]["planner_calls"],
                tokens=latest["usage"]["tokens"],
                credits=latest["usage"]["agent_credits"],
                a2="/".join(
                    item["decision"] for item in task_a2["decisions"]
                ),
                a3="/".join(
                    item["decision"] for item in task_a3["decisions"]
                ),
                b2=latest["b2"]["status"],
                clock=latest["b2"]["observed_period_ns"],
                accel=raw_acceleration,
            )
        )
    history_rows = []
    for task in tasks:
        first = task["first_attempt_result"]
        if first.get("failure_class"):
            history_rows.append(
                f"| {task['task_id']} | {first.get('failure_class')} | "
                f"{task.get('targeted_rerun_reason')} | "
                f"{task['latest_validated_result']['agent']['status']} / "
                f"{task['latest_validated_result']['b2']['status']} |"
            )
    acceleration_rows = []
    for item in performance["optimize_normalized_acceleration"]["tasks"]:
        acceleration_rows.append(
            f"| {item['task_id']} | {item['baseline_latency']:.0f} | "
            f"{item['candidate_latency']:.0f} | "
            f"{item['raw_acceleration']:.6f}× | "
            f"{item['capped_acceleration']:.6f}× |"
        )
    return f"""# Full Agent 公开 Corpus 28 题合并覆盖结果

`{COMBINED_MARKER}`

```text
First attempt: 25/28
Latest validated combined coverage: 28/28
Targeted reruns: 012, 018, 020
Single batch 28×1: No
Single-fingerprint causal ablation: No
```

## 覆盖结论

- 首次严格成功：**25/28（{_format_percent(25, 28)}）**。
- latest validated 合并覆盖：**28/28（100%）**。
- REPAIR `{by_mode['REPAIR']['strict_successes']}/8`；SYNTH_FIX `{by_mode['SYNTH_FIX']['strict_successes']}/6`；STRUCTURAL_FIX `{by_mode['STRUCTURAL_FIX']['strict_successes']}/6`；OPTIMIZE `{by_mode['OPTIMIZE']['strict_successes']}/8`。
- B2 CSim/Synth/CoSim 和 100 MHz Gate 均为 `28/28 PASS`。
- 这是跨批次 latest-validated 合并覆盖，不是统一单批次 28×1。

## A2 版本合同

```text
cli_policy_interface_version = {CLI_POLICY_INTERFACE_VERSION}
effective_policy_version = {EFFECTIVE_POLICY_VERSION}
decision_schema = {DECISION_SCHEMA}
```

外部 `v2` 是 CLI 兼容接口，不是实际执行策略版本。48 个 A2 决策均按上述合同归一化。

## 28 题主表

| Task | Expected Mode | Routed Mode | Route Match | Agent | Final Candidate | Planner | Tokens | Agent Credits | A2 | A3 | B2 | Clock | Raw acceleration |
|---|---|---|---|---|---|---:|---:|---:|---|---|---|---:|---:|
{chr(10).join(rows)}

`v3d_fast_018` 的 Expected Mode 为 `STRUCTURAL_FIX`，Routed Mode 为 `OPTIMIZE`，Route Match 为 `false`。本次真实 baseline CSim、Synth 和 required CoSim 均 PASS，因此运行时进入 OPTIMIZE；此处不修改 Router，也不隐藏 fixture 预期与真实运行路由的差异。

## A2/A3 证据边界

No-harm evidence：

- latest validated `28/28`；
- A2 false block `0`；
- A2 unsafe finalize `0`；
- A3 injection mismatch `0`。

Causal-benefit evidence：

- 当前没有同一运行实现和 Executor fingerprint 下的 off/shadow 配对消融；
- 不能声称 A2/A3 导致成功率提升；
- 不能声称 A2/A3 导致 Token 下降；
- 不能声称 A2/A3 导致 Planner 调用减少。

## 用量与策略统计

- Planner `{usage['planner_calls_total']}` 次，均值 `{usage['planner_calls_mean']:.4f}`。
- Tokens `{usage['tokens_total']}`，均值/每成功题 `{usage['tokens_mean']:.4f}`。
- Agent Credits 独立 Ledger 统计和 `{usage['agent_credits_statistical_sum']}`，不是共享预算。
- A2：ALLOW `{a2['decision_counts'].get('ALLOW', 0)}`、BLOCK `{a2['decision_counts'].get('BLOCK', 0)}`、学习型 FINALIZE `{a2['decision_counts'].get('FINALIZE', 0)}`。
- A3：RECOMMEND `{a3['decision_counts'].get('RECOMMEND', 0)}`、ABSTAIN `{a3['decision_counts'].get('ABSTAIN', 0)}`，注入 mismatch `0`。
- PUBLIC_VALIDATION_PROXY_V1 总和 `{proxy['total']:.4f}`，不是 hidden official score。

## OPTIMIZE acceleration

| Task | Baseline | B2 Candidate | Raw | Capped |
|---|---:|---:|---:|---:|
{chr(10).join(acceleration_rows)}

达到官方 8× 封顶的任务为 `v3d_fast_026`；023、025 合法回退 baseline，归一 acceleration 为 `1.0×`。

## 首次失败与定向重跑

| Task | First-attempt failure | Targeted rerun reason | Latest |
|---|---|---|---|
{chr(10).join(history_rows)}

首次失败、020 的三个 Executor preflight，以及全部 attempt history 均保留；preflight 不进入 28 题首次 Agent 尝试分母。

## Release 判断

- 现有 no-harm 覆盖满足进入代码冻结的条件。
- 尚缺统一单批次 28×1，以及同实现/同 Executor fingerprint 的 A2/A3 配对消融。
- 本轮仅生成报告与快照，不调用模型或 Vitis，不自动 commit、merge 或 push。

## 输出

- 机器结果：`llm4hls_harness/runs/full-agent-v3d-fast-combined-28-coverage-20260728-a01/combined_full_agent_28_task_coverage.json`
- 状态记录：`docs/status/2026-07-28-full-agent-combined-28-coverage.md`
- Release snapshot：`docs/experiments/artifacts/2026-07-28-full-agent-release-snapshot/`
"""


def render_status_markdown(report: Mapping[str, Any]) -> str:
    usage = report["summary"]["usage"]
    a2 = report["summary"]["a2"]
    a3 = report["summary"]["a3"]
    return f"""# 2026-07-28 Full Agent combined 28-task coverage

`{COMBINED_MARKER}`

```text
First attempt: 25/28
Latest validated combined coverage: 28/28
Targeted reruns: 012, 018, 020
Single batch 28×1: No
Single-fingerprint causal ablation: No
```

## 状态

- 状态码：`{COMBINED_STATUS}`。
- 首次严格成功：`25/28`（`{_format_percent(25, 28)}`）。
- latest validated 合并覆盖：`28/28`（`100%`）。
- B2 和 100 MHz Gate：全部 `28/28 PASS`。
- 018 路由差异保持可见：Expected `STRUCTURAL_FIX`，Routed `OPTIMIZE`，Route Match `false`；真实 baseline 三阶段均 PASS。
- 当前判断：进入代码冻结；停止 A1/A2/A3 和 Executor 开发。

## A2 版本

- CLI interface：`{CLI_POLICY_INTERFACE_VERSION}`。
- Effective policy：`{EFFECTIVE_POLICY_VERSION}`。
- Decision schema：`{DECISION_SCHEMA}`。

## 证据边界

No-harm：latest `28/28`、A2 false block `0`、A2 unsafe finalize `0`、A3 injection mismatch `0`。

Causal benefit：没有同实现、同 Executor fingerprint 的 off/shadow 配对消融；不能把成功率、Token 或 Planner 调用变化归因于 A2/A3。

## 统计

- Planner `{usage['planner_calls_total']}`；Tokens `{usage['tokens_total']}`；Agent Credits 统计和 `{usage['agent_credits_statistical_sum']}`。
- A2 ALLOW `{a2['decision_counts'].get('ALLOW', 0)}`；A3 RECOMMEND `{a3['decision_counts'].get('RECOMMEND', 0)}`、ABSTAIN `{a3['decision_counts'].get('ABSTAIN', 0)}`。

## 边界与输出

本结果不是单批次 28×1；每题 Agent Ledger 独立。仍需统一正式 28×1 和同 fingerprint 配对消融，才能形成对应的统一批次与因果收益证据。

- 机器结果：`llm4hls_harness/runs/full-agent-v3d-fast-combined-28-coverage-20260728-a01/combined_full_agent_28_task_coverage.json`
- 详细报告：`llm4hls_harness/runs/full-agent-v3d-fast-combined-28-coverage-20260728-a01/combined_full_agent_28_task_report.md`
- Release snapshot：`docs/experiments/artifacts/2026-07-28-full-agent-release-snapshot/`
"""


def patch_report_files(
    *,
    combined_json: Path,
    combined_markdown: Path,
    status_markdown: Path,
) -> dict[str, Any]:
    source = load_json(combined_json)
    report = patch_combined_report(source)
    write_json(combined_json, report)
    combined_markdown.write_text(
        render_combined_markdown(report), encoding="utf-8"
    )
    status_markdown.write_text(
        render_status_markdown(report), encoding="utf-8"
    )
    return report


def runtime_critical_paths(root: Path) -> list[Path]:
    harness = root / "llm4hls_harness"
    paths: set[Path] = set()
    package_root = harness / "llm4hls_agent"
    for path in package_root.rglob("*"):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix in RUNTIME_SUFFIXES
        ):
            paths.add(path)
    paths.add(harness / "pyproject.toml")
    paths.update(
        {
            harness / "llm4hls_agent/config/track_a_full_agent_v1.json",
            root
            / (
                "docs/experiments/artifacts/"
                "2026-07-27-a2-a3-full-agent-closure/"
                "a2/continuation-v3-gate.json"
            ),
            root
            / (
                "docs/experiments/artifacts/"
                "2026-07-27-a2-a3-full-agent-closure/"
                "a2/continuation-v3-admission.json"
            ),
            root
            / (
                "docs/experiments/artifacts/"
                "2026-07-27-a2-a3-full-agent-closure/"
                "a3/train-only-experience-store.jsonl"
            ),
            root
            / (
                "docs/experiments/artifacts/"
                "2026-07-27-a2-a3-full-agent-closure/"
                "a3/ranker-v3-gate.json"
            ),
            root
            / (
                "docs/experiments/artifacts/"
                "2026-07-27-a2-a3-full-agent-closure/"
                "a3/admission.json"
            ),
            harness / "task_corpus/v3d-fast/corpus_manifest.json",
        }
    )
    tasks_root = harness / "task_corpus/v3d-fast/tasks"
    for task_dir in sorted(tasks_root.glob("v3d_fast_*")):
        for path in task_dir.iterdir():
            if path.is_file() and (
                path.name in PUBLIC_TASK_NAMES
                or path.suffix in PUBLIC_TASK_SUFFIXES
            ):
                paths.add(path)
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ReleaseMetadataError(
            "runtime-critical files are missing: "
            + ", ".join(str(path) for path in missing)
        )
    return sorted(paths)


def runtime_source_snapshot(root: Path) -> dict[str, Any]:
    records = [
        content_record(path, root=root) for path in runtime_critical_paths(root)
    ]
    executor = root / "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py"
    return {
        "schema_version": "v3d.runtime-source-snapshot.v1",
        "file_count": len(records),
        "aggregate_sha256": aggregate_content_records(records),
        "executor": content_record(executor, root=root),
        "files": records,
    }


def _tracked_file_manifest(root: Path) -> list[dict[str, Any]]:
    paths = [
        PurePosixPath(item.decode("utf-8"))
        for item in git_bytes(root, "ls-files", "-z").split(b"\0")
        if item
    ]
    head_rows: dict[str, tuple[str, str]] = {}
    raw_tree = git_bytes(root, "ls-tree", "-r", "-z", "HEAD")
    for item in raw_tree.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, _kind, blob = metadata.decode("ascii").split()
        head_rows[raw_path.decode("utf-8")] = (mode, blob)
    records = []
    for pure_path in sorted(paths, key=lambda item: item.as_posix()):
        relative = pure_path.as_posix()
        path = root / relative
        head_mode, head_blob = head_rows.get(relative, (None, None))
        record: dict[str, Any] = {
            "path": relative,
            "head_mode": head_mode,
            "head_blob_oid": head_blob,
            "exists_in_worktree": path.is_file(),
        }
        if path.is_file():
            record.update(
                {
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "worktree_mode": f"{stat.S_IMODE(path.lstat().st_mode):04o}",
                }
            )
        else:
            record.update(
                {
                    "size_bytes": None,
                    "sha256": None,
                    "worktree_mode": None,
                }
            )
        records.append(record)
    return records


def _untracked_paths(root: Path) -> list[str]:
    return sorted(
        item.decode("utf-8")
        for item in git_bytes(
            root, "ls-files", "--others", "--exclude-standard", "-z"
        ).split(b"\0")
        if item
    )


def is_critical_untracked(relative: str) -> bool:
    if relative in OVERLAY_EXACT_PATHS:
        return True
    if relative.startswith("llm4hls_harness/llm4hls_agent/"):
        path = PurePosixPath(relative)
        return (
            path.suffix in RUNTIME_SUFFIXES
            and "__pycache__" not in path.parts
            and ".cache" not in path.parts
        )
    return any(relative.startswith(prefix) for prefix in OVERLAY_PREFIXES)


def _overlay_source_class(relative: str) -> tuple[str, list[str]]:
    if relative.startswith("llm4hls_harness/release_tools/"):
        return "REPORT_ONLY_TOOL", ["release metadata reproduction"]
    if relative.startswith("llm4hls_harness/tests/"):
        return "REPRODUCTION_TEST", ["focused/full test reproduction"]
    if relative.startswith("llm4hls_harness/llm4hls_agent/"):
        return "RUNTIME_SOURCE", ["Full Agent runtime import closure"]
    if "/a2/" in relative:
        return "FROZEN_A2_ARTIFACT", ["Full Agent Manifest", "A2 Admission"]
    if "/a3/" in relative:
        return "FROZEN_A3_ARTIFACT", ["Full Agent Manifest", "A3 Admission"]
    raise ReleaseMetadataError(f"unclassified overlay path: {relative}")


def build_critical_overlay(
    *, root: Path, snapshot_dir: Path
) -> dict[str, Any]:
    snapshot_prefix = repository_relative(snapshot_dir, root).rstrip("/") + "/"
    untracked = [
        path
        for path in _untracked_paths(root)
        if not path.startswith(snapshot_prefix)
    ]
    selected = [path for path in untracked if is_critical_untracked(path)]
    overlay_root = snapshot_dir / "source_overlay"
    records = []
    for relative in selected:
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise ReleaseMetadataError(
                f"critical untracked input must be a regular file: {relative}"
            )
        target = overlay_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, stat.S_IMODE(source.lstat().st_mode))
        source_class, required_by = _overlay_source_class(relative)
        record = content_record(source, root=root)
        record.update(
            {
                "operation": "add",
                "base_git_blob_oid": None,
                "source_class": source_class,
                "required_by": required_by,
                "overlay_ref": (
                    Path("source_overlay") / relative
                ).as_posix(),
            }
        )
        if sha256_file(target) != record["sha256"]:
            raise ReleaseMetadataError(f"overlay copy hash mismatch: {relative}")
        records.append(record)
    missing_inputs = sorted(
        relative
        for relative in OVERLAY_EXACT_PATHS
        if not (root / relative).is_file()
    )
    if missing_inputs:
        raise ReleaseMetadataError(
            "required runtime closure is missing: "
            + ", ".join(missing_inputs)
        )
    tracked = set(
        item.decode("utf-8")
        for item in git_bytes(root, "ls-files", "-z").split(b"\0")
        if item
    )
    uncovered = sorted(
        relative
        for relative in OVERLAY_EXACT_PATHS
        if relative not in selected and relative not in tracked
    )
    if uncovered:
        raise ReleaseMetadataError(
            "required runtime inputs are neither tracked nor overlaid: "
            + ", ".join(uncovered)
        )
    excluded = [path for path in untracked if path not in set(selected)]
    return {
        "schema_version": "v3d.critical-untracked-manifest.v1",
        "base": "repository_root",
        "file_count": len(records),
        "aggregate_sha256": aggregate_content_records(records),
        "files": records,
        "excluded_untracked": {
            "count": len(excluded),
            "paths": excluded,
            "rules": list(EXCLUSION_RULES),
        },
        "restore_contract": {
            "base": "exact base HEAD checkout",
            "step_1": "apply working_tree.patch with git apply --binary",
            "step_2": (
                "copy source_overlay contents to repository root and verify "
                "critical_untracked_manifest.json hashes"
            ),
            "complete_for_runtime_critical_untracked_inputs": True,
        },
        "required_input_coverage": {
            "paths": sorted(OVERLAY_EXACT_PATHS),
            "tracked_count": sum(
                relative in tracked for relative in OVERLAY_EXACT_PATHS
            ),
            "overlay_count": sum(
                relative in selected for relative in OVERLAY_EXACT_PATHS
            ),
            "uncovered_count": 0,
        },
    }


def _sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return "<REDACTED_INVALID_URL>"
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[name] = value
    return values


def sanitized_environment(root: Path) -> dict[str, Any]:
    file_values = parse_env_file(root / ".env")
    result = {}
    for name in RELEVANT_ENV_NAMES:
        if name in os.environ:
            value = os.environ[name]
            source = "process_environment"
        elif name in file_values:
            value = file_values[name]
            source = ".env"
        else:
            value = None
            source = "unset"
        redacted = name in SECRET_ENV_NAMES or any(
            token in name
            for token in ("API_KEY", "PASSWORD", "SECRET", "AUTH_TOKEN")
        )
        if redacted and value is not None:
            rendered: str | None = "<REDACTED>"
        elif name == "OPENAI_BASE_URL" and value:
            rendered = _sanitize_url(value)
        else:
            rendered = value
        result[name] = {
            "value": rendered,
            "source": source,
            "redacted": redacted and value is not None,
        }
    return result


def command_output(command: Sequence[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"UNAVAILABLE: {type(exc).__name__}"
    output = result.stdout.strip()
    return output if output else f"exit_code={result.returncode}"


def python_environment_text(python: Path, harness: Path) -> str:
    version = command_output([str(python), "--version"], cwd=harness)
    freeze = command_output(
        [str(python), "-m", "pip", "freeze", "--all"], cwd=harness
    )
    return (
        f"generated_at={utc_now()}\n"
        f"python_executable={python}\n"
        f"python_version={version}\n\n"
        "[pip freeze --all]\n"
        f"{freeze}\n"
    )


def _find_toolchain_receipt(
    report: Mapping[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    task = next(
        item for item in report["tasks"] if item["task_id"] == "v3d_fast_020"
    )
    latest = task["latest_validated_result"]
    run_dir = Path(latest["source"]["run_dir"])
    cert = load_json(Path(latest["source"]["certified_result_ref"]))
    receipt_path = run_dir / cert["final_certification"]["receipt_ref"]
    receipt = load_json(receipt_path)
    stage = receipt.get("stages", {}).get("csim")
    if not isinstance(stage, Mapping):
        raise ReleaseMetadataError("validated CSim certification stage missing")
    artifact_ref = stage.get("artifacts", {}).get("vitis_toolchain")
    declared_hash = stage.get("artifact_hashes", {}).get("vitis_toolchain")
    if not isinstance(artifact_ref, str) or not isinstance(declared_hash, str):
        raise ReleaseMetadataError("certification toolchain binding missing")
    toolchain_path = receipt_path.parent / "actions" / "csim" / artifact_ref
    if not toolchain_path.is_file():
        raise ReleaseMetadataError("validated Vitis toolchain receipt not found")
    if sha256_file(toolchain_path) != declared_hash:
        raise ReleaseMetadataError("validated Vitis toolchain hash mismatch")
    return toolchain_path, load_json(toolchain_path), receipt


def sanitize_toolchain_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Remove machine-local paths while preserving version and content binding."""

    sanitized = copy.deepcopy(dict(raw))
    vitis_root = sanitized.get("vitis_root")
    selected_executable = sanitized.get("selected_executable")
    if isinstance(vitis_root, str):
        sanitized["vitis_root"] = "<VITIS_ROOT>"
    if isinstance(selected_executable, str):
        executable_name = Path(selected_executable).name
        sanitized["selected_executable"] = (
            f"<VITIS_ROOT>/bin/{executable_name}"
        )
    return sanitized


def toolchain_environment(
    *,
    root: Path,
    report: Mapping[str, Any],
    runtime_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    toolchain_path, raw, cert_receipt = _find_toolchain_receipt(report)
    sanitized = sanitize_toolchain_receipt(raw)
    certification_tool = copy.deepcopy(
        cert_receipt.get("config", {}).get("tool", {})
    )
    if isinstance(certification_tool.get("vitis_root"), str):
        certification_tool["vitis_root"] = "<VITIS_ROOT>"
    dependency_locks = []
    for path in sorted((root / "llm4hls_harness").iterdir()):
        if path.is_file() and path.name in DEPENDENCY_LOCK_NAMES:
            dependency_locks.append(content_record(path, root=root))
    os_release = {}
    os_release_path = Path("/etc/os-release")
    if os_release_path.is_file():
        for raw_line in os_release_path.read_text(encoding="utf-8").splitlines():
            if "=" not in raw_line:
                continue
            name, value = raw_line.split("=", 1)
            os_release[name] = value.strip().strip('"')
    return {
        "schema_version": "v3d.toolchain-environment.v1",
        "captured_at": utc_now(),
        "os": {
            "platform": platform.platform(),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "os_release": os_release,
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "compilers": {
            "gcc": command_output(["gcc", "--version"]).splitlines()[0],
            "g++": command_output(["g++", "--version"]).splitlines()[0],
            "clang": command_output(["clang", "--version"]).splitlines()[0],
        },
        "dependencies": {
            "lock_files": dependency_locks,
            "lock_regeneration_verified": False,
            "lock_regeneration_note": (
                "The existing lock is content-bound but was not regenerated "
                "or downloaded during this report-only patch."
            ),
        },
        "vitis": {
            "source": "existing independently certified run receipt",
            "source_receipt_ref": repository_relative(toolchain_path, root),
            "source_receipt_sha256": sha256_file(toolchain_path),
            "sanitized_receipt": sanitized,
            "sanitized_receipt_sha256": sha256_bytes(
                canonical_json_bytes(sanitized)
            ),
            "backend_fingerprint": (
                cert_receipt["stages"]["csim"].get("backend_fingerprint")
            ),
            "version": raw.get("version"),
            "selected_executable_sha256": raw.get(
                "selected_executable_sha256"
            ),
            "certification_tool_config": certification_tool,
            "invoked_during_snapshot": False,
        },
        "runtime_source_aggregate_sha256": runtime_snapshot[
            "aggregate_sha256"
        ],
        "environment": sanitized_environment(root),
        "commands": {
            "single_task_command_template": (
                "LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/"
                "vitis/AMD/2025.2/Vitis "
                "../.venv/bin/llm4hls-v3-prototype "
                "--task-dir task_corpus/v3d-fast/tasks/<TASK_ID> "
                "--planner openai-compatible --run-dir runs/<FRESH_RUN_DIR> "
                "--thread-id <UNIQUE_THREAD_ID> --backend vitis "
                "--vitis-root /home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis "
                "--toolchain-id 'Vitis 2025.2' --credit-limit <TASK_MAX_CREDITS> "
                "--runtime-limit 2400 --cleanup-reserve-seconds 30 "
                "--csim-timeout 300 --synth-timeout 1800 --cosim-timeout 1800 "
                "--minimum-frequency-mhz 100 --model deepseek-v4-pro "
                "--run-token-limit <TASK_MAX_TOKENS_OR_32768_FALLBACK> "
                "--token-budget-policy fixed --max-planner-rounds 4 "
                "--evidence-memory on --continuation-policy enforce "
                "--continuation-policy-version v2 "
                "--continuation-admission-manifest "
                "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
                "a2/continuation-v3-admission.json "
                "--experience-mode guided --experience-ranker-version v3 "
                "--experience-store "
                "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
                "a3/train-only-experience-store.jsonl "
                "--experience-admission-manifest "
                "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
                "a3/admission.json --validation-profile fast-experiment "
                "--final-validation-policy task_contract"
            ),
            "batch_command_template": (
                "LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/"
                "vitis/AMD/2025.2/Vitis "
                "../.venv/bin/python3 -m llm4hls_agent.v3_batch_benchmark "
                "--corpus task_corpus/v3d-fast --output-dir runs/<FRESH_OUTPUT> "
                "--split all --task <TASK_ID_OR_GLOB> "
                "--models deepseek-v4-pro --repeats 1 --backend vitis "
                "--validation-profile fast-experiment "
                "--final-validation-policy task_contract "
                "--evidence-memory on --max-planner-rounds 4 "
                "--continuation-policy enforce "
                "--continuation-policy-version v2 "
                "--continuation-admission-manifest "
                "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
                "a2/continuation-v3-admission.json "
                "--full-agent-manifest "
                "llm4hls_agent/config/track_a_full_agent_v1.json "
                "--experience-mode guided --experience-ranker-version v3 "
                "--experience-store "
                "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
                "a3/train-only-experience-store.jsonl "
                "--experience-admission-manifest "
                "docs/experiments/artifacts/2026-07-27-a2-a3-full-agent-closure/"
                "a3/admission.json "
                "--max-runtime 2400"
            ),
        },
        "limits": {
            "outer_runtime_seconds": 2400,
            "cleanup_reserve_seconds": 30,
            "max_planner_rounds": 4,
            "minimum_frequency_mhz": 100,
            "token_policy": "fixed",
            "undeclared_max_tokens_fallback": 32768,
            "credits": "task-declared max_credits takes precedence",
            "independent_final_certification_outside_agent_ledger": True,
        },
    }


def _evidence_paths_for_task(
    task: Mapping[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Path]]:
    latest = task["latest_validated_result"]
    run_dir = Path(latest["source"]["run_dir"])
    cert_path = Path(latest["source"]["certified_result_ref"])
    cert = load_json(cert_path)
    paths = {
        "certified_result": cert_path,
        "certification_receipt": run_dir
        / cert["final_certification"]["receipt_ref"],
        "package_manifest": run_dir / cert["package"]["manifest_ref"],
        "final_candidate_source": run_dir
        / cert["terminal_candidate_binding"]["source_ref"],
        "agent_ledger": run_dir / "budget_ledger.jsonl",
        "benchmark_run": run_dir / "benchmark_run.json",
        "candidate_registry": run_dir / "candidate_registry.json",
    }
    execution_binding = run_dir / "benchmark_execution_binding.json"
    if execution_binding.is_file():
        paths["benchmark_execution_binding"] = execution_binding
    return run_dir, cert, paths


def build_run_evidence_manifest(
    *, root: Path, report: Mapping[str, Any]
) -> dict[str, Any]:
    records = []
    evidence_files = []
    core_evidence_files = []
    certification_toolchain_files = []
    executor_fingerprints: set[str] = set()
    toolchain_versions: set[str] = set()
    toolchain_executable_hashes: set[str] = set()
    toolchain_backend_fingerprints: set[str] = set()
    run_ids: set[str] = set()
    run_dirs: set[str] = set()
    for task in report["tasks"]:
        run_dir, cert, paths = _evidence_paths_for_task(task)
        files = {}
        for role, path in paths.items():
            if not path.is_file():
                raise ReleaseMetadataError(
                    f"run evidence is missing for {task['task_id']}: {path}"
                )
            record = content_record(path, root=root)
            files[role] = record
            evidence_files.append(record)
            core_evidence_files.append(record)
        benchmark = load_json(paths["benchmark_run"])
        certification_receipt = load_json(paths["certification_receipt"])
        package_manifest = load_json(paths["package_manifest"])
        candidate_registry = load_json(paths["candidate_registry"])
        latest = task["latest_validated_result"]
        task_id = str(task["task_id"])
        run_id = str(latest["source"]["run_id"])
        if benchmark.get("task_id") != task_id:
            raise ReleaseMetadataError(
                f"benchmark task identity mismatch for {task_id}"
            )
        if benchmark.get("run_id") != run_id:
            raise ReleaseMetadataError(
                f"benchmark run identity mismatch for {task_id}"
            )
        benchmark_run_dir = benchmark.get("run_dir")
        if (
            not isinstance(benchmark_run_dir, str)
            or Path(benchmark_run_dir).resolve() != run_dir.resolve()
        ):
            raise ReleaseMetadataError(
                f"benchmark run directory mismatch for {task_id}"
            )
        if run_id in run_ids or str(run_dir.resolve()) in run_dirs:
            raise ReleaseMetadataError(
                f"latest validated run reused by multiple tasks: {task_id}"
            )
        run_ids.add(run_id)
        run_dirs.add(str(run_dir.resolve()))
        if benchmark.get("source_result_sha256") != files[
            "certified_result"
        ]["sha256"]:
            raise ReleaseMetadataError(
                f"certified result hash mismatch for {task_id}"
            )
        if cert.get("task_id") != task_id or cert.get("status") != "DONE":
            raise ReleaseMetadataError(
                f"certified result identity/status mismatch for {task_id}"
            )
        final_candidate = latest["agent"]["final_candidate"]
        if any(
            value != final_candidate
            for value in (
                cert.get("final_candidate_id"),
                cert.get("terminal_candidate_binding", {}).get(
                    "candidate_id"
                ),
                certification_receipt.get("candidate_id"),
                package_manifest.get("final_candidate_id"),
                candidate_registry.get("final_candidate_id"),
            )
        ):
            raise ReleaseMetadataError(
                f"final Candidate identity mismatch for {task_id}"
            )
        if (
            package_manifest.get("task_id") != task_id
            or package_manifest.get("status") != "DONE"
            or candidate_registry.get("task_id") != task_id
            or certification_receipt.get("status") != "PASS"
        ):
            raise ReleaseMetadataError(
                f"package/registry/certification status mismatch for {task_id}"
            )
        terminal_binding = cert.get("terminal_candidate_binding", {})
        if (
            terminal_binding.get("source_sha256")
            != files["final_candidate_source"]["sha256"]
        ):
            raise ReleaseMetadataError(
                f"final Candidate source hash mismatch for {task_id}"
            )
        provenance = benchmark.get("provenance_validation", {})
        if (
            provenance.get("token_ledger", {}).get("sha256")
            != files["agent_ledger"]["sha256"]
            or provenance.get("package_manifest", {}).get("file_sha256")
            != files["package_manifest"]["sha256"]
            or provenance.get("independent_certification", {})
            .get("receipt", {})
            .get("file_sha256")
            != files["certification_receipt"]["sha256"]
        ):
            raise ReleaseMetadataError(
                f"provenance file binding mismatch for {task_id}"
            )
        if (
            latest["usage"]["planner_calls"] != benchmark.get("model_calls")
            or latest["usage"]["tokens"] != benchmark.get("tokens_used")
            or latest["usage"]["agent_credits"] != benchmark.get(
                "credits_used"
            )
        ):
            raise ReleaseMetadataError(
                f"reported usage mismatch for {task_id}"
            )
        certification_root = paths["certification_receipt"].parent
        for stage_name in FINAL_CERTIFICATION_STAGES:
            stage = certification_receipt.get("stages", {}).get(stage_name)
            if not isinstance(stage, Mapping):
                raise ReleaseMetadataError(
                    f"B2 {stage_name} stage missing for {task['task_id']}"
                )
            artifact_ref = stage.get("artifacts", {}).get("vitis_toolchain")
            declared_hash = stage.get("artifact_hashes", {}).get(
                "vitis_toolchain"
            )
            if not isinstance(artifact_ref, str) or not isinstance(
                declared_hash, str
            ):
                raise ReleaseMetadataError(
                    f"B2 {stage_name} toolchain binding missing for "
                    f"{task['task_id']}"
                )
            path = (
                certification_root
                / "actions"
                / stage_name
                / artifact_ref
            )
            if not path.is_file() or sha256_file(path) != declared_hash:
                raise ReleaseMetadataError(
                    f"B2 {stage_name} toolchain hash mismatch for "
                    f"{task['task_id']}"
                )
            raw_toolchain = load_json(path)
            if (
                raw_toolchain.get("schema_version")
                != "v3.vitis-toolchain-receipt.v1"
                or raw_toolchain.get("preflight_result") != "READY"
                or raw_toolchain.get("invocation_mode") != "vitis-run"
                or raw_toolchain.get("version") != "2025.2"
                or not isinstance(
                    raw_toolchain.get("selected_executable_sha256"), str
                )
                or not isinstance(stage.get("backend_fingerprint"), str)
            ):
                raise ReleaseMetadataError(
                    f"B2 {stage_name} toolchain contract mismatch for "
                    f"{task['task_id']}"
                )
            toolchain_versions.add(str(raw_toolchain["version"]))
            toolchain_executable_hashes.add(
                str(raw_toolchain["selected_executable_sha256"])
            )
            toolchain_backend_fingerprints.add(
                str(stage["backend_fingerprint"])
            )
            toolchain_record = content_record(path, root=root)
            toolchain_record.update(
                {
                    "receipt_declared_sha256": declared_hash,
                    "schema_version": raw_toolchain.get("schema_version"),
                    "version": raw_toolchain.get("version"),
                    "preflight_result": raw_toolchain.get(
                        "preflight_result"
                    ),
                    "invocation_mode": raw_toolchain.get("invocation_mode"),
                    "selection_source": raw_toolchain.get("selection_source"),
                    "selected_executable_sha256": raw_toolchain.get(
                        "selected_executable_sha256"
                    ),
                    "backend_fingerprint": stage.get(
                        "backend_fingerprint"
                    ),
                    "tool_config_hash": stage.get("tool_config_hash"),
                }
            )
            role = f"b2_{stage_name}_toolchain"
            files[role] = toolchain_record
            evidence_files.append(toolchain_record)
            certification_toolchain_files.append(toolchain_record)
        executor_fingerprints.add(str(benchmark["executor_fingerprint"]))
        first_attempt = copy.deepcopy(task["first_attempt_result"])
        first_run_dir = first_attempt.get("run_dir")
        if isinstance(first_run_dir, str):
            try:
                first_attempt["run_dir"] = repository_relative(
                    Path(first_run_dir), root
                )
            except ReleaseMetadataError:
                first_attempt["run_dir"] = "<OUTSIDE_REPOSITORY>"
        records.append(
            {
                "task_id": task["task_id"],
                "source_campaign": latest["source"]["campaign"],
                "run_id": latest["source"]["run_id"],
                "run_dir": repository_relative(run_dir, root),
                "evidence": files,
                "final_candidate_id": latest["agent"]["final_candidate"],
                "final_candidate_source_sha256": files[
                    "final_candidate_source"
                ]["sha256"],
                "agent_ledger_sha256": files["agent_ledger"]["sha256"],
                "planner_calls": latest["usage"]["planner_calls"],
                "tokens": latest["usage"]["tokens"],
                "agent_credits": latest["usage"]["agent_credits"],
                "first_attempt_result": first_attempt,
                "latest_validated": {
                    "strict_success": latest["strict_success"],
                    "agent_status": latest["agent"]["status"],
                    "b2_status": latest["b2"]["status"],
                    "clock_100mhz": latest["b2"]["clock_100mhz"],
                },
                "targeted_rerun": task["task_id"]
                in TARGETED_RERUN_TASK_IDS,
                "targeted_rerun_reason": task.get("targeted_rerun_reason"),
                "executor_fingerprint": benchmark["executor_fingerprint"],
                "backend_fingerprint": certification_receipt.get("stages", {})
                .get("csim", {})
                .get("backend_fingerprint"),
                "certified_result_status": cert.get("status"),
            }
        )
    if (
        toolchain_versions != {"2025.2"}
        or len(toolchain_executable_hashes) != 1
        or len(toolchain_backend_fingerprints) != 1
    ):
        raise ReleaseMetadataError(
            "B2 toolchain identity is not uniform across 28 tasks"
        )
    return {
        "schema_version": "v3d.run-evidence-manifest.v1",
        "task_count": len(records),
        "latest_validated_strict_successes": sum(
            record["latest_validated"]["strict_success"] is True
            for record in records
        ),
        "first_attempt_strict_successes": sum(
            record["first_attempt_result"].get("strict_success") is True
            for record in records
        ),
        "targeted_rerun_task_ids": list(TARGETED_RERUN_TASK_IDS),
        "unique_run_ids": len(run_ids),
        "unique_run_directories": len(run_dirs),
        "executor_fingerprints": sorted(executor_fingerprints),
        "evidence_file_count": len(evidence_files),
        "evidence_file_aggregate_sha256": aggregate_content_records(
            evidence_files
        ),
        "certification_toolchain_file_count": len(
            certification_toolchain_files
        ),
        "certification_toolchain_file_aggregate_sha256": (
            aggregate_content_records(certification_toolchain_files)
        ),
        "certification_toolchain_identity": {
            "versions": sorted(toolchain_versions),
            "selected_executable_sha256": sorted(
                toolchain_executable_hashes
            ),
            "backend_fingerprints": sorted(
                toolchain_backend_fingerprints
            ),
        },
        "prestate_compatible_evidence_binding_aggregate_sha256": (
            aggregate_content_records(
                [
                    {
                        "path": str(root / record["path"]),
                        "size_bytes": record["size_bytes"],
                        "sha256": record["sha256"],
                    }
                    for record in core_evidence_files
                ]
            )
        ),
        "tasks": records,
    }


def run_critical_tree_snapshot(
    *, root: Path, report: Mapping[str, Any]
) -> dict[str, Any]:
    records = []
    for task in report["tasks"]:
        run_dir = Path(task["latest_validated_result"]["source"]["run_dir"])
        for path in run_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(run_dir)
            if any(part in {"work", "__pycache__", ".cache"} for part in relative.parts):
                continue
            records.append(
                {
                    "path": f"{task['task_id']}/{relative.as_posix()}",
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "file_count": len(records),
        "aggregate_sha256": aggregate_content_records(records),
    }


def test_command(
    command: Sequence[str], *, cwd: Path
) -> dict[str, Any]:
    started = time.monotonic()
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    elapsed = time.monotonic() - started
    output = result.stdout
    match = re.search(r"Ran (\d+) tests? in", output)
    passed = (
        result.returncode == 0
        and re.search(r"\nOK(?: \([^)]*\))?(?:\n|$)", output) is not None
    )
    return {
        "command": list(command),
        "cwd": str(cwd),
        "exit_code": result.returncode,
        "passed": passed,
        "test_count": int(match.group(1)) if match else None,
        "duration_seconds": elapsed,
        "output_sha256": sha256_bytes(output.encode("utf-8")),
        "output": output,
    }


def execute_local_tests(
    *, root: Path, python: Path, output_path: Path
) -> dict[str, Any]:
    harness = root / "llm4hls_harness"
    focused = test_command(
        [
            str(python),
            "-m",
            "unittest",
            "tests.test_release_snapshot_reporting",
            "tests.test_executor_runtime",
            "tests.test_v3_batch_benchmark",
            "tests.test_v3_prototype",
            "tests.test_v3_planner_action",
            "tests.test_final_certification",
        ],
        cwd=harness,
    )
    full = test_command(
        [
            str(python),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
        ],
        cwd=harness,
    )
    diff_check = subprocess.run(
        ["git", "diff", "--check"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    result = {
        "schema_version": "v3d.release-test-results.v1",
        "captured_at": utc_now(),
        "focused": focused,
        "full": full,
        "git_diff_check": {
            "command": ["git", "diff", "--check"],
            "exit_code": diff_check.returncode,
            "passed": diff_check.returncode == 0,
            "output": diff_check.stdout,
            "output_sha256": sha256_bytes(
                diff_check.stdout.encode("utf-8")
            ),
        },
        "deepseek_calls": 0,
        "vitis_actions": 0,
        "reporting_sources": {
            "generator": content_record(Path(__file__), root=root),
            "tests": content_record(
                harness / "tests/test_release_snapshot_reporting.py",
                root=root,
            ),
        },
    }
    result["passed"] = (
        focused["passed"]
        and full["passed"]
        and result["git_diff_check"]["passed"]
    )
    write_json(output_path, result)
    if not result["passed"]:
        raise ReleaseMetadataError("local release tests failed")
    return result


def _check_snapshot_target(snapshot_dir: Path) -> None:
    if snapshot_dir.exists() and any(snapshot_dir.iterdir()):
        raise ReleaseMetadataError(
            f"snapshot directory must be absent or empty: {snapshot_dir}"
        )
    snapshot_dir.mkdir(parents=True, exist_ok=True)


def _write_worktree_patch(root: Path, snapshot_dir: Path) -> dict[str, Any]:
    patch = git_bytes(root, "diff", "--binary", "HEAD", "--")
    path = snapshot_dir / "working_tree.patch"
    path.write_bytes(patch)
    return {
        "ref": path.name,
        "size_bytes": len(patch),
        "sha256": sha256_bytes(patch),
        "includes_tracked_changes_relative_to_head": True,
        "includes_untracked_files": False,
    }


def _manifest_entry(path: Path, snapshot_dir: Path) -> dict[str, Any]:
    return {
        "ref": path.relative_to(snapshot_dir).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def render_snapshot_report(
    *,
    release_metadata: Mapping[str, Any],
    report_hash_inputs: Mapping[str, Mapping[str, Any]],
    tests: Mapping[str, Any],
) -> str:
    pre = release_metadata["fingerprints"]["before"]
    post = release_metadata["fingerprints"]["after"]
    return f"""# Full Agent Release Metadata Snapshot

## 结论

- Snapshot 类型：report-only metadata patch。
- Base HEAD：`{release_metadata['source']['base_head']}`。
- Branch：`{release_metadata['source']['branch']}`。
- Runtime aggregate before/after：`{pre['runtime_source_aggregate_sha256']}` / `{post['runtime_source_aggregate_sha256']}`。
- Executor source before/after：`{pre['executor_implementation_sha256']}` / `{post['executor_implementation_sha256']}`。
- Historical run critical tree before/after：`{pre['run_critical_tree_aggregate_sha256']}` / `{post['run_critical_tree_aggregate_sha256']}`。
- Runtime、Executor 与历史 run 关键证据均未变化。

## 覆盖口径

```text
First attempt: 25/28
Latest validated combined coverage: 28/28
Targeted reruns: 012, 018, 020
Single batch 28×1: No
Single-fingerprint causal ablation: No
```

No-harm evidence 为 latest `28/28`、A2 false block `0`、A2 unsafe finalize `0`、A3 injection mismatch `0`。当前没有同实现/同 Executor fingerprint 的 off/shadow 配对消融，因此不作 A2/A3 因果收益声明。

## 可恢复源码状态

- `working_tree.patch` 保存相对 HEAD 的完整 tracked binary diff。
- `critical_untracked_manifest.json` 与 `source_overlay/` 保存运行关键未跟踪源码、配置、冻结 A2/A3 Artifact、report-only 工具和复现测试。
- `runtime_source_manifest.json` 包含 tracked 工作树清单、当前内容 hash、运行时 allow-list 及 aggregate fingerprint。
    - `runs/`、Vitis work、cache、`.env`、`min/`、hidden/reference/golden 未跟踪内容未复制进 overlay；tracked diff 仍完整保存在 `working_tree.patch`，28 题证据由 `run_evidence_manifest.json` 内容寻址。

## 环境与测试

- Focused：`{tests['focused']['test_count']}` tests，PASS。
- Full：`{tests['full']['test_count']}` tests，PASS。
- `git diff --check`：PASS。
- DeepSeek calls：0；Vitis actions：0。

## 报告哈希

- Combined JSON：`{report_hash_inputs['combined_json']['sha256']}`。
- Combined Markdown：`{report_hash_inputs['combined_markdown']['sha256']}`。
- Status Markdown：`{report_hash_inputs['status_markdown']['sha256']}`。

`report_hashes.json` 是无环的外部索引：它在本报告生成后记录本报告自身 hash；被索引报告不声明自己的 hash。

## 尚缺证据

1. 同一实现/Executor fingerprint 的统一单批次 28×1；
2. 同一实现/Executor fingerprint 的 A2/A3 off/shadow 配对消融。
"""


def export_release_snapshot(
    *,
    root: Path,
    combined_json: Path,
    combined_markdown: Path,
    status_markdown: Path,
    snapshot_dir: Path,
    prestate_path: Path,
    test_results_path: Path,
    python: Path,
) -> dict[str, Any]:
    _check_snapshot_target(snapshot_dir)
    report = load_json(combined_json)
    prestate = load_json(prestate_path)
    tests = load_json(test_results_path)
    if tests.get("passed") is not True:
        raise ReleaseMetadataError("snapshot requires passing local tests")
    for record in tests.get("reporting_sources", {}).values():
        source = root / record["path"]
        if (
            not source.is_file()
            or source.stat().st_size != record["size_bytes"]
            or sha256_file(source) != record["sha256"]
        ):
            raise ReleaseMetadataError(
                "release reporting source changed after tests"
            )
    if report.get("status") != COMBINED_STATUS:
        raise ReleaseMetadataError("combined report status is not normalized")
    head_before = git_text(root, "rev-parse", "HEAD").strip()
    branch_before = git_text(root, "branch", "--show-current").strip()
    if head_before != prestate.get("head") or branch_before != prestate.get(
        "branch"
    ):
        raise ReleaseMetadataError(
            "repository HEAD or branch changed since prestate capture"
        )

    before_runtime = prestate["runtime_source_aggregate_sha256"]
    runtime_snapshot = runtime_source_snapshot(root)
    if runtime_snapshot["aggregate_sha256"] != before_runtime:
        raise ReleaseMetadataError(
            "runtime source changed before snapshot export; stop immediately"
        )
    executor_before = prestate["executor_implementation_sha256"]
    executor_after = runtime_snapshot["executor"]["sha256"]
    if executor_after != executor_before:
        raise ReleaseMetadataError(
            "Executor implementation changed during report-only patch"
        )
    run_tree_after = run_critical_tree_snapshot(root=root, report=report)
    if (
        run_tree_after["file_count"] != prestate["run_critical_tree_file_count"]
        or run_tree_after["aggregate_sha256"]
        != prestate["run_critical_tree_aggregate_sha256"]
    ):
        raise ReleaseMetadataError(
            "historical run Artifact changed during report-only patch"
        )

    patch_record = _write_worktree_patch(root, snapshot_dir)
    overlay = build_critical_overlay(root=root, snapshot_dir=snapshot_dir)
    critical_path = snapshot_dir / "critical_untracked_manifest.json"
    write_json(critical_path, overlay)

    tracked_files = _tracked_file_manifest(root)
    runtime_manifest = {
        "schema_version": "v3d.runtime-source-manifest.v1",
        "source": {
            "base_head": git_text(root, "rev-parse", "HEAD").strip(),
            "branch": git_text(root, "branch", "--show-current").strip(),
            "repository_layout_root": "track-A",
            "tracked_file_count": len(tracked_files),
            "tracked_files": tracked_files,
            "working_tree_patch": patch_record,
            "critical_untracked_manifest": _manifest_entry(
                critical_path, snapshot_dir
            ),
            "exclusion_rules": list(EXCLUSION_RULES),
        },
        "runtime_critical": runtime_snapshot,
        "aggregate_fingerprint_contract": (
            "SHA-256 over sorted path\\0size\\0sha256\\n records"
        ),
    }
    runtime_path = snapshot_dir / "runtime_source_manifest.json"
    write_json(runtime_path, runtime_manifest)

    run_evidence = build_run_evidence_manifest(root=root, report=report)
    run_evidence_path = snapshot_dir / "run_evidence_manifest.json"
    write_json(run_evidence_path, run_evidence)
    if run_evidence["latest_validated_strict_successes"] != 28:
        raise ReleaseMetadataError("run evidence latest strict count changed")
    if run_evidence["first_attempt_strict_successes"] != 25:
        raise ReleaseMetadataError("run evidence first-attempt count changed")
    if (
        run_evidence[
            "prestate_compatible_evidence_binding_aggregate_sha256"
        ]
        != prestate["run_evidence_binding_aggregate_sha256"]
    ):
        raise ReleaseMetadataError(
            "historical run evidence binding changed during report-only patch"
        )

    python_path = snapshot_dir / "python_environment.txt"
    python_path.write_text(
        python_environment_text(python, root / "llm4hls_harness"),
        encoding="utf-8",
    )
    toolchain = toolchain_environment(
        root=root,
        report=report,
        runtime_snapshot=runtime_snapshot,
    )
    toolchain_path = snapshot_dir / "toolchain_environment.json"
    write_json(toolchain_path, toolchain)

    test_path = snapshot_dir / "test_results.json"
    shutil.copyfile(test_results_path, test_path)
    if sha256_file(test_path) != sha256_file(test_results_path):
        raise ReleaseMetadataError("test result copy hash mismatch")

    report_hash_inputs = {
        "combined_json": {
            "ref": repository_relative(combined_json, root),
            "size_bytes": combined_json.stat().st_size,
            "sha256": sha256_file(combined_json),
        },
        "combined_markdown": {
            "ref": repository_relative(combined_markdown, root),
            "size_bytes": combined_markdown.stat().st_size,
            "sha256": sha256_file(combined_markdown),
        },
        "status_markdown": {
            "ref": repository_relative(status_markdown, root),
            "size_bytes": status_markdown.stat().st_size,
            "sha256": sha256_file(status_markdown),
        },
    }
    runtime_end = runtime_source_snapshot(root)
    run_tree_end = run_critical_tree_snapshot(root=root, report=report)
    evidence_end = build_run_evidence_manifest(root=root, report=report)
    current_patch = git_bytes(root, "diff", "--binary", "HEAD", "--")
    if runtime_end["aggregate_sha256"] != runtime_snapshot[
        "aggregate_sha256"
    ]:
        raise ReleaseMetadataError("runtime changed during snapshot export")
    if runtime_end["executor"]["sha256"] != executor_after:
        raise ReleaseMetadataError("Executor changed during snapshot export")
    if run_tree_end != run_tree_after:
        raise ReleaseMetadataError(
            "historical run Artifact changed during snapshot export"
        )
    if evidence_end[
        "evidence_file_aggregate_sha256"
    ] != run_evidence["evidence_file_aggregate_sha256"]:
        raise ReleaseMetadataError(
            "run evidence changed during snapshot export"
        )
    if sha256_bytes(current_patch) != patch_record["sha256"]:
        raise ReleaseMetadataError(
            "tracked working tree changed during snapshot export"
        )
    if git_text(root, "rev-parse", "HEAD").strip() != head_before:
        raise ReleaseMetadataError("HEAD changed during snapshot export")
    if git_text(root, "branch", "--show-current").strip() != branch_before:
        raise ReleaseMetadataError("branch changed during snapshot export")
    for record in overlay["files"]:
        source = root / record["path"]
        overlay_copy = snapshot_dir / record["overlay_ref"]
        if (
            not source.is_file()
            or sha256_file(source) != record["sha256"]
            or sha256_file(overlay_copy) != record["sha256"]
        ):
            raise ReleaseMetadataError(
                f"critical untracked source changed during export: "
                f"{record['path']}"
            )

    release_metadata: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "generated_at": utc_now(),
        "scope": "REPORT_ONLY_RELEASE_METADATA_PATCH",
        "status": "READY_FOR_CODE_FREEZE",
        "source": {
            "base_head": git_text(root, "rev-parse", "HEAD").strip(),
            "branch": git_text(root, "branch", "--show-current").strip(),
            "dirty_worktree": bool(git_text(root, "status", "--short").strip()),
            "working_tree_patch": patch_record,
            "critical_untracked_overlay": _manifest_entry(
                critical_path, snapshot_dir
            ),
        },
        "coverage": {
            "first_attempt_strict_success": {
                "successes": 25,
                "denominator": 28,
                "rate": 25 / 28,
            },
            "latest_validated_combined_coverage": {
                "successes": 28,
                "denominator": 28,
                "rate": 1.0,
            },
            "targeted_rerun_task_ids": list(TARGETED_RERUN_TASK_IDS),
            "single_batch_28x1": False,
            "single_fingerprint_causal_ablation": False,
        },
        "a2_version_contract": {
            "cli_policy_interface_version": CLI_POLICY_INTERFACE_VERSION,
            "effective_policy_version": EFFECTIVE_POLICY_VERSION,
            "decision_schema": DECISION_SCHEMA,
        },
        "fingerprints": {
            "before": {
                "runtime_source_aggregate_sha256": before_runtime,
                "executor_implementation_sha256": executor_before,
                "evidence_executor_fingerprints": prestate[
                    "evidence_executor_fingerprints"
                ],
                "run_evidence_binding_aggregate_sha256": prestate[
                    "run_evidence_binding_aggregate_sha256"
                ],
                "run_critical_tree_aggregate_sha256": prestate[
                    "run_critical_tree_aggregate_sha256"
                ],
            },
            "after": {
                "runtime_source_aggregate_sha256": runtime_end[
                    "aggregate_sha256"
                ],
                "executor_implementation_sha256": runtime_end["executor"][
                    "sha256"
                ],
                "evidence_executor_fingerprints": evidence_end[
                    "executor_fingerprints"
                ],
                "run_evidence_binding_aggregate_sha256": evidence_end[
                    "prestate_compatible_evidence_binding_aggregate_sha256"
                ],
                "run_critical_tree_aggregate_sha256": run_tree_end[
                    "aggregate_sha256"
                ],
            },
            "runtime_unchanged": before_runtime
            == runtime_snapshot["aggregate_sha256"],
            "executor_unchanged": executor_before == executor_after,
            "historical_run_critical_tree_unchanged": True,
            "report_only_patch_created_new_runtime_fingerprint": False,
            "run_evidence_binding_unchanged": True,
        },
        "evidence_boundaries": report["evidence_boundaries"],
        "tests": {
            "focused_passed": tests["focused"]["passed"],
            "focused_test_count": tests["focused"]["test_count"],
            "full_passed": tests["full"]["passed"],
            "full_test_count": tests["full"]["test_count"],
            "git_diff_check_passed": tests["git_diff_check"]["passed"],
            "deepseek_calls": 0,
            "vitis_actions": 0,
        },
        "snapshot_files": {},
        "missing_evidence": [
            "UNIFIED_SINGLE_BATCH_28X1",
            "SAME_FINGERPRINT_PAIRED_A2_A3_ABLATION",
        ],
    }
    snapshot_report_path = snapshot_dir / "release_snapshot_report.md"
    snapshot_report_path.write_text(
        render_snapshot_report(
            release_metadata=release_metadata,
            report_hash_inputs=report_hash_inputs,
            tests=tests,
        ),
        encoding="utf-8",
    )
    report_hashes = {
        "schema_version": "v3d.report-hashes.v1",
        "hash_graph": "ACYCLIC_EXTERNAL_INDEX",
        "reports": {
            **report_hash_inputs,
            "release_snapshot_report": _manifest_entry(
                snapshot_report_path, snapshot_dir
            ),
        },
    }
    report_hashes_path = snapshot_dir / "report_hashes.json"
    write_json(report_hashes_path, report_hashes)

    required_files = [
        runtime_path,
        critical_path,
        snapshot_dir / "working_tree.patch",
        python_path,
        toolchain_path,
        run_evidence_path,
        test_path,
        report_hashes_path,
        snapshot_report_path,
    ]
    release_metadata["snapshot_files"] = {
        path.name: _manifest_entry(path, snapshot_dir)
        for path in required_files
    }
    release_metadata_path = snapshot_dir / "release_metadata.json"
    write_json(release_metadata_path, release_metadata)
    return release_metadata


def validate_report_hashes(
    *, snapshot_dir: Path, root: Path
) -> dict[str, Any]:
    index = load_json(snapshot_dir / "report_hashes.json")
    reports = index.get("reports", {})
    expected_labels = {
        "combined_json",
        "combined_markdown",
        "status_markdown",
        "release_snapshot_report",
    }
    if not isinstance(reports, Mapping) or set(reports) != expected_labels:
        raise ReleaseMetadataError(
            "report hash index must bind exactly the four release reports"
        )
    for label, record in reports.items():
        ref = record.get("ref")
        if not isinstance(ref, str):
            raise ReleaseMetadataError(f"report hash ref missing for {label}")
        base = snapshot_dir if label == "release_snapshot_report" else root
        path = (base / ref).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError as exc:
            raise ReleaseMetadataError(
                f"report hash path escapes its root: {label}"
            ) from exc
        if not path.is_file():
            raise ReleaseMetadataError(f"hashed report missing: {path}")
        if path.stat().st_size != record.get("size_bytes"):
            raise ReleaseMetadataError(f"report size mismatch: {label}")
        if sha256_file(path) != record.get("sha256"):
            raise ReleaseMetadataError(f"report hash mismatch: {label}")
    return index


def validate_release_snapshot(
    *, snapshot_dir: Path, root: Path
) -> dict[str, Any]:
    required = {
        "release_metadata.json",
        "runtime_source_manifest.json",
        "working_tree.patch",
        "critical_untracked_manifest.json",
        "python_environment.txt",
        "toolchain_environment.json",
        "run_evidence_manifest.json",
        "test_results.json",
        "report_hashes.json",
        "release_snapshot_report.md",
    }
    missing = sorted(
        name for name in required if not (snapshot_dir / name).is_file()
    )
    if missing:
        raise ReleaseMetadataError(
            "release snapshot files missing: " + ", ".join(missing)
        )
    report_hashes = validate_report_hashes(
        snapshot_dir=snapshot_dir, root=root
    )
    metadata = load_json(snapshot_dir / "release_metadata.json")
    runtime_manifest = load_json(
        snapshot_dir / "runtime_source_manifest.json"
    )
    overlay = load_json(snapshot_dir / "critical_untracked_manifest.json")
    run_evidence = load_json(snapshot_dir / "run_evidence_manifest.json")
    tests = load_json(snapshot_dir / "test_results.json")
    toolchain = load_json(snapshot_dir / "toolchain_environment.json")
    if metadata.get("status") != "READY_FOR_CODE_FREEZE":
        raise ReleaseMetadataError("release metadata is not freeze-ready")
    fingerprints = metadata.get("fingerprints", {})
    if not all(
        fingerprints.get(name) is expected
        for name, expected in (
            ("runtime_unchanged", True),
            ("executor_unchanged", True),
            ("historical_run_critical_tree_unchanged", True),
            ("report_only_patch_created_new_runtime_fingerprint", False),
            ("run_evidence_binding_unchanged", True),
        )
    ):
        raise ReleaseMetadataError("release fingerprint assertions failed")
    if (
        tests.get("passed") is not True
        or tests.get("deepseek_calls") != 0
        or tests.get("vitis_actions") != 0
    ):
        raise ReleaseMetadataError("release test record is not clean")
    if (
        run_evidence.get("task_count") != 28
        or run_evidence.get("unique_run_ids") != 28
        or run_evidence.get("unique_run_directories") != 28
        or run_evidence.get("latest_validated_strict_successes") != 28
        or run_evidence.get("first_attempt_strict_successes") != 25
        or run_evidence.get("certification_toolchain_file_count") != 84
    ):
        raise ReleaseMetadataError("run evidence coverage contract failed")
    for task in run_evidence.get("tasks", []):
        for record in task.get("evidence", {}).values():
            path = root / record["path"]
            if (
                not path.is_file()
                or path.stat().st_size != record["size_bytes"]
                or sha256_file(path) != record["sha256"]
            ):
                raise ReleaseMetadataError(
                    f"run evidence hash mismatch: {record['path']}"
                )
    for record in overlay.get("files", []):
        path = snapshot_dir / record["overlay_ref"]
        if (
            not path.is_file()
            or path.stat().st_size != record["size_bytes"]
            or sha256_file(path) != record["sha256"]
        ):
            raise ReleaseMetadataError(
                f"source overlay hash mismatch: {record['path']}"
            )
    snapshot_files = metadata.get("snapshot_files", {})
    for name in required - {"release_metadata.json"}:
        record = snapshot_files.get(name)
        path = snapshot_dir / name
        if (
            not isinstance(record, Mapping)
            or record.get("size_bytes") != path.stat().st_size
            or record.get("sha256") != sha256_file(path)
        ):
            raise ReleaseMetadataError(
                f"snapshot file binding mismatch: {name}"
            )
    patch_record = runtime_manifest.get("source", {}).get(
        "working_tree_patch", {}
    )
    patch_path = snapshot_dir / "working_tree.patch"
    if patch_record.get("sha256") != sha256_file(patch_path):
        raise ReleaseMetadataError("working tree patch hash mismatch")
    runtime_current = runtime_source_snapshot(root)
    if runtime_current["aggregate_sha256"] != runtime_manifest.get(
        "runtime_critical", {}
    ).get("aggregate_sha256"):
        raise ReleaseMetadataError(
            "current runtime no longer matches release snapshot"
        )
    vitis = toolchain.get("vitis", {})
    if (
        vitis.get("version") != "2025.2"
        or not isinstance(vitis.get("selected_executable_sha256"), str)
        or vitis.get("invoked_during_snapshot") is not False
        or "/home/" in json.dumps(
            vitis.get("sanitized_receipt", {}), ensure_ascii=False
        )
    ):
        raise ReleaseMetadataError("toolchain environment contract failed")
    return {
        "status": "PASS",
        "reports": len(report_hashes.get("reports", {})),
        "tasks": run_evidence["task_count"],
        "certification_toolchain_files": run_evidence[
            "certification_toolchain_file_count"
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report-only Full Agent release metadata exporter"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    patch = subparsers.add_parser("patch-reports")
    patch.add_argument("--combined-json", type=Path, required=True)
    patch.add_argument("--combined-markdown", type=Path, required=True)
    patch.add_argument("--status-markdown", type=Path, required=True)

    tests = subparsers.add_parser("run-tests")
    tests.add_argument("--root", type=Path, required=True)
    tests.add_argument("--python", type=Path, required=True)
    tests.add_argument("--output", type=Path, required=True)

    snapshot = subparsers.add_parser("export-snapshot")
    snapshot.add_argument("--root", type=Path, required=True)
    snapshot.add_argument("--combined-json", type=Path, required=True)
    snapshot.add_argument("--combined-markdown", type=Path, required=True)
    snapshot.add_argument("--status-markdown", type=Path, required=True)
    snapshot.add_argument("--snapshot-dir", type=Path, required=True)
    snapshot.add_argument("--prestate", type=Path, required=True)
    snapshot.add_argument("--test-results", type=Path, required=True)
    snapshot.add_argument("--python", type=Path, required=True)

    validate = subparsers.add_parser("validate-snapshot")
    validate.add_argument("--root", type=Path, required=True)
    validate.add_argument("--snapshot-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "patch-reports":
        report = patch_report_files(
            combined_json=args.combined_json,
            combined_markdown=args.combined_markdown,
            status_markdown=args.status_markdown,
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "first_attempt": "25/28",
                    "latest_validated": "28/28",
                }
            )
        )
        return 0
    if args.command == "run-tests":
        result = execute_local_tests(
            root=args.root.resolve(),
            python=absolute_preserving_symlinks(args.python),
            output_path=args.output.resolve(),
        )
        print(
            json.dumps(
                {
                    "focused": result["focused"]["test_count"],
                    "full": result["full"]["test_count"],
                    "passed": result["passed"],
                }
            )
        )
        return 0
    if args.command == "export-snapshot":
        metadata = export_release_snapshot(
            root=args.root.resolve(),
            combined_json=args.combined_json.resolve(),
            combined_markdown=args.combined_markdown.resolve(),
            status_markdown=args.status_markdown.resolve(),
            snapshot_dir=args.snapshot_dir.resolve(),
            prestate_path=args.prestate.resolve(),
            test_results_path=args.test_results.resolve(),
            python=absolute_preserving_symlinks(args.python),
        )
        print(
            json.dumps(
                {
                    "status": metadata["status"],
                    "runtime_unchanged": metadata["fingerprints"][
                        "runtime_unchanged"
                    ],
                    "executor_unchanged": metadata["fingerprints"][
                        "executor_unchanged"
                    ],
                }
            )
        )
        return 0
    if args.command == "validate-snapshot":
        validation = validate_release_snapshot(
            snapshot_dir=args.snapshot_dir.resolve(),
            root=args.root.resolve(),
        )
        print(json.dumps(validation))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
