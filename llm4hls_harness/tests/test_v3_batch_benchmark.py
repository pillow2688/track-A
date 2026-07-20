from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_batch_benchmark import (
    RUN_SCHEMA,
    SUMMARY_SCHEMA,
    BatchBenchmarkRunner,
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkExecutionError,
    BenchmarkRunSpec,
    EvidenceClass,
    SyntheticBenchmarkExecutor,
    V3PrototypeCLIExecutor,
    build_summary,
    discover_tasks,
    main,
    select_models,
    select_tasks,
)


def write_task(
    root: Path,
    task_id: str,
    *,
    task_type: str = "optimize",
    difficulty: int = 1,
    split: str | None = None,
    expected_mode: str | None = None,
) -> Path:
    directory = root / task_id
    directory.mkdir(parents=True)
    metadata = [
        f'task_id = "{task_id}"',
        f'task_type = "{task_type}"',
        f"difficulty = {difficulty}",
        'top = "kernel"',
        'kernel_file = "kernel.cpp"',
        'public_tb = "kernel_tb.cpp"',
        "header_files = []",
        "budget = 80",
        "",
    ]
    if split is not None or expected_mode is not None:
        metadata.extend(["[benchmark]"])
        if split is not None:
            metadata.append(f'split = "{split}"')
        if expected_mode is not None:
            metadata.append(f'expected_mode = "{expected_mode}"')
        metadata.append("")
    (directory / "task.toml").write_text("\n".join(metadata), encoding="utf-8")
    (directory / "kernel.cpp").write_text(
        "void kernel(int *x) { x[0] += 1; }\n", encoding="utf-8"
    )
    (directory / "kernel_tb.cpp").write_text(
        "int main() { return 0; }\n", encoding="utf-8"
    )
    return directory


def fake_result(
    mode: str,
    *,
    status: str = "DONE",
    cached_final: bool = False,
    evidence_level: str = "DETERMINISTIC_TEST_ONLY",
    rejection: bool = False,
) -> dict[str, object]:
    candidate_rounds: list[dict[str, object]] = [
        {
            "round": 0,
            "candidate_id": "candidate_000",
            "latency_worst": 100,
            "decision": "BASELINE",
        }
    ]
    final_id = "candidate_001"
    if rejection:
        candidate_rounds.append(
            {
                "round": 1,
                "candidate_id": "candidate_001",
                "latency_worst": 120,
                "decision": "REJECTED",
                "decision_reason": "PATCH_POLICY_REJECTED",
            }
        )
        candidate_rounds.append(
            {
                "round": 2,
                "candidate_id": "candidate_002",
                "latency_worst": 40,
                "decision": "FINAL_VERIFIED",
            }
        )
        final_id = "candidate_002"
    else:
        candidate_rounds.append(
            {
                "round": 1,
                "candidate_id": "candidate_001",
                "latency_worst": 40,
                "decision": "FINAL_VERIFIED",
            }
        )
    validation_status = "PASS" if status == "DONE" else "FAIL"
    return {
        "backend": {"evidence_level": evidence_level},
        "mode": mode,
        "status": status,
        "stop_reason": "COMPLETED" if status == "DONE" else "FINAL_SYNTH_FAILED",
        "baseline_candidate_id": "candidate_000",
        "final_candidate_id": final_id if status == "DONE" else None,
        "final_validation": {
            stage: {"status": validation_status, "cached": cached_final}
            for stage in ("csim", "synth", "cosim")
        },
        "candidate_rounds": candidate_rounds,
        "budget": {
            "credits_used": 75,
            "tokens_used": 900,
            "input_tokens_used": 700,
            "output_tokens_used": 200,
            "runtime_used_seconds": 3.5,
            "tool_used": {"csim": 3, "synth": 3, "cosim": 2, "llm": 2},
        },
    }


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def write_canonical_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (canonical_json(value) + "\n").encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_real_fixture(
    executor: V3PrototypeCLIExecutor,
    spec: BenchmarkRunSpec,
    *,
    outcome_model: str | None = None,
    ledger_tokens: int | None = None,
) -> dict[str, object]:
    """Write a minimal internally hash-bound REAL terminal package."""

    run_dir = spec.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    write_canonical_json(
        run_dir / "benchmark_executor_command.json", executor._command(spec)
    )
    write_canonical_json(
        run_dir / "benchmark_execution_binding.json",
        {
            "schema_version": "v3d.execution-binding.v1",
            "run_id": spec.run_id,
            "run_fingerprint": spec.run_fingerprint,
            "task_id": spec.task.id,
            "task_fingerprint": spec.descriptor.task_fingerprint,
            "model": spec.model,
        },
    )
    budget_config = {
        "credit_limit": 100,
        "costs": {"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
        "tool_limits": {"csim": 4, "synth": 4, "cosim": 4, "llm": 1},
        "token_limit": 1000,
        "runtime_limit_seconds": 100.0,
    }
    backend_fingerprint = "llm4hls_agent.vitis.VitisBackend:v0.7"
    write_canonical_json(
        run_dir / "v3_run_config.json",
        {
            "thread_id": spec.run_id,
            "backend_fingerprint": backend_fingerprint,
            "budget": budget_config,
        },
    )
    write_canonical_json(
        run_dir / "v3_task_spec.json",
        {
            "task_id": spec.task.id,
            "public_file_hashes": dict(spec.task.public_file_hashes),
        },
    )

    source_ref = "candidates/candidate_001/kernel.cpp"
    source = b"void kernel(int *x) { x[0] += 2; }\n"
    source_path = run_dir / source_ref
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source)
    source_sha = hashlib.sha256(source).hexdigest()
    write_canonical_json(
        run_dir / "candidate_registry.json",
        {
            "candidates": {
                "candidate_001": {
                    "source_ref": source_ref,
                    "code_hash": source_sha,
                }
            }
        },
    )

    planner_input = {"schema_version": "fixture.input.v1", "round": 1}
    planner_input_ref = "planner/inputs/round_001.json"
    write_canonical_json(run_dir / planner_input_ref, planner_input)
    planner_output = {"schema_version": "fixture.output.v1", "proposal": "p"}
    planner_output_ref = "planner/outputs/projection.json"
    write_canonical_json(run_dir / planner_output_ref, planner_output)

    model = spec.model if outcome_model is None else outcome_model
    request_audit = {
        "schema_version": "v3b.planner-provider-request.v1",
        "logical_operation_id": "fixture-operation",
        "planner_fingerprint": "fixture-planner",
        "input_sha256": json_sha256(planner_input),
        "estimated_input_tokens": 10,
        "max_output_tokens": 10,
        "request": {
            "provider_request": {
                "provider": "openai-compatible",
                "model": model,
                "http_body": {"model": model},
            }
        },
    }
    request_ref = "planner/requests/fixture.json"
    write_canonical_json(run_dir / request_ref, request_audit)
    request_sha = json_sha256(request_audit)
    action_request = {
        "schema_version": "v3b.planner-action.v1",
        "logical_operation_id": "fixture-operation",
        "attempt_index": 0,
        "retry_of": None,
        "planner_fingerprint": "fixture-planner",
        "input_ref": planner_input_ref,
        "input_sha256": json_sha256(planner_input),
        "request_ref": request_ref,
        "request_sha256": request_sha,
        "replay_policy": "NON_REPLAYABLE",
    }
    action_id = json_sha256(action_request)
    proposal = {
        "patch": "--- a/kernel.cpp\n+++ b/kernel.cpp\n@@ -1 +1 @@\n-a\n+b\n",
        "provider": "openai-compatible",
        "model": model,
        "revision": None,
        "input_tokens": 7,
        "output_tokens": 3,
        "cached_input_tokens": 1,
        "request_id": "request-1",
        "duration_seconds": 0.5,
        "hypothesis": "fixture",
        "change_class": "fixture",
        "expected_effect": "fixture",
        "risk": "fixture",
        "required_validation": ["csim", "synth", "cosim"],
    }
    usage = {
        "input_tokens": 7,
        "output_tokens": 3,
        "cached_input_tokens": 1,
        "tokens_used": 10,
        "duration_seconds": 0.5,
        "request_id": "request-1",
        "usage_complete": True,
    }
    outcome = {
        "schema_version": "v3b.planner-outcome.v1",
        "action_id": action_id,
        "input_sha256": json_sha256(planner_input),
        "outcome": "PROPOSAL",
        "proposal": proposal,
        "semantic_proposal_sha256": "0" * 64,
        "provider_binding": {
            "planner_fingerprint": "fixture-planner",
            "provider": "openai-compatible",
            "model": model,
            "revision": None,
        },
        "usage": usage,
    }
    live_output_ref = f"planner/live_outcomes/{action_id}.json"
    live_output_sha = write_canonical_json(run_dir / live_output_ref, outcome)
    live_started_ref = f"control/live_planner_actions/{action_id}.started.json"
    write_canonical_json(
        run_dir / live_started_ref,
        {
            "schema_version": "v3b.planner-action-started.v1",
            "action_id": action_id,
            "status": "STARTED",
            "request": action_request,
        },
    )
    live_completed_ref = f"control/live_planner_actions/{action_id}.completed.json"
    write_canonical_json(
        run_dir / live_completed_ref,
        {
            "schema_version": "v3b.planner-action-completed.v1",
            "action_id": action_id,
            "status": "COMPLETED",
            "request": action_request,
            "outcome": "PROPOSAL",
            "result_ref": live_output_ref,
            "result_sha256": live_output_sha,
            "semantic_proposal_sha256": "0" * 64,
            "tokens_used": 10,
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 1,
        },
    )

    charged_tokens = 10 if ledger_tokens is None else ledger_tokens
    ledger_events = [
        {
            "state": "INITIALIZED",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "epoch_seconds": 0.0,
            "config_hash": "fixture",
            "config": budget_config,
        },
        {
            "state": "STARTED",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "action_id": action_id,
            "kind": "llm",
            "candidate_id": "candidate_000",
            "code_hash": "0" * 64,
            "tool_config_hash": request_sha,
            "estimated_cost": 0,
            "estimated_tokens": 20,
        },
        {
            "state": "COMPLETED",
            "timestamp": "2026-01-01T00:00:02+00:00",
            "action_id": action_id,
            "kind": "llm",
            "actual_cost": 0,
            "tokens_used": charged_tokens,
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 1,
            "elapsed_s": 0.5,
            "result_ref": live_output_ref,
            "result_sha256": live_output_sha,
        },
    ]
    ledger_path = run_dir / "budget_ledger.jsonl"
    ledger_path.write_text(
        "".join(canonical_json(event) + "\n" for event in ledger_events),
        encoding="utf-8",
    )

    final_validation: dict[str, object] = {}
    for stage in ("csim", "synth", "cosim"):
        final_ref = f"actions/final-{stage}/result.json"
        write_canonical_json(
            run_dir / final_ref,
            {
                "action_id": f"final-{stage}",
                "kind": stage,
                "ok": True,
                "candidate_id": "candidate_001",
                "code_hash": source_sha,
                "validation_scope": "final",
            },
        )
        final_validation[stage] = {
            "status": "PASS",
            "cached": False,
            "validation_scope": "final",
            "action_id": f"final-{stage}",
            "result_ref": final_ref,
        }

    result: dict[str, object] = {
        "schema_version": 1,
        "result_schema": "v3a.terminal-result.v1",
        "workflow": "V3_A1_VERTICAL_PROTOTYPE",
        "backend": {
            "class": "llm4hls_agent.vitis.VitisBackend",
            "fingerprint": backend_fingerprint,
            "evidence_level": "REAL_VITIS_VALIDATED",
        },
        "task_id": spec.task.id,
        "mode": "OPTIMIZE",
        "status": "DONE",
        "stop_reason": "COMPLETED",
        "baseline_candidate_id": "candidate_000",
        "final_candidate_id": "candidate_001",
        "planner_input_ref": planner_input_ref,
        "planner_input_sha256": json_sha256(planner_input),
        "planner_output_ref": planner_output_ref,
        "planner_output_sha256": json_sha256(planner_output),
        "live_planner_action_id": action_id,
        "live_planner_request_ref": request_ref,
        "live_planner_request_sha256": request_sha,
        "live_planner_output_ref": live_output_ref,
        "live_planner_output_sha256": live_output_sha,
        "live_planner_started_ref": live_started_ref,
        "live_planner_completed_ref": live_completed_ref,
        "final_validation": final_validation,
        "candidate_rounds": [],
        "node_events": [],
        "budget": {
            "credits_used": 0,
            "tokens_used": charged_tokens,
            "input_tokens_used": 7,
            "output_tokens_used": 3,
            "cached_input_tokens_used": 1,
            "token_usage_complete": True,
            "runtime_used_seconds": 1.0,
            "tool_used": {"llm": 1},
        },
    }
    artifact_paths = sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file()
        and path.name not in {
            "benchmark_executor_command.json",
            "benchmark_execution_binding.json",
        }
    )
    artifacts = []
    for path in artifact_paths:
        data = path.read_bytes()
        artifacts.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    manifest = {
        "schema_version": "v3a.package-manifest.v1",
        "workflow": result["workflow"],
        "task_id": spec.task.id,
        "status": "DONE",
        "final_candidate_id": "candidate_001",
        "node_event_count": 0,
        "terminal_payload_sha256": json_sha256(result),
        "artifacts": artifacts,
    }
    manifest_ref = "control/package_manifest.json"
    write_canonical_json(run_dir / manifest_ref, manifest)
    result["package"] = {
        "schema_version": "v3a.package-commit.v1",
        "manifest_ref": manifest_ref,
        "manifest_sha256": json_sha256(manifest),
    }
    write_canonical_json(run_dir / "v3_prototype_result.json", result)
    return result


class FakeExecutor:
    evidence_class = EvidenceClass.DETERMINISTIC
    requires_vitis_lock = False

    def __init__(
        self,
        *,
        fingerprint: str = "fake-deterministic:v1",
        fail_tasks: set[str] | None = None,
        wrong_modes: set[str] | None = None,
        cached_tasks: set[str] | None = None,
        rejected_tasks: set[str] | None = None,
    ) -> None:
        self._fingerprint = fingerprint
        self.fail_tasks = fail_tasks or set()
        self.wrong_modes = wrong_modes or set()
        self.cached_tasks = cached_tasks or set()
        self.rejected_tasks = rejected_tasks or set()
        self.calls: list[str] = []
        self.max_timeout: list[float | None] = []

    def fingerprint(self) -> str:
        return self._fingerprint

    def execute(self, spec, *, timeout_seconds=None):
        self.calls.append(spec.task.id)
        self.max_timeout.append(timeout_seconds)
        if spec.task.id in self.fail_tasks:
            raise BenchmarkExecutionError("injected executor failure")
        mode = spec.descriptor.expected_mode or "OPTIMIZE"
        if spec.task.id in self.wrong_modes:
            mode = "REPAIR" if mode != "REPAIR" else "OPTIMIZE"
        return fake_result(
            mode,
            cached_final=spec.task.id in self.cached_tasks,
            rejection=spec.task.id in self.rejected_tasks,
        )


class DemoExecutor(FakeExecutor):
    evidence_class = EvidenceClass.DEMO

    def execute(self, spec, *, timeout_seconds=None):
        result = super().execute(spec, timeout_seconds=timeout_seconds)
        result["backend"] = {"evidence_level": "ORCHESTRATION_SMOKE_ONLY"}
        return result


class RealFixtureExecutor(FakeExecutor):
    """A unit-test stub; it never invokes Vitis despite exercising policy code."""

    evidence_class = EvidenceClass.REAL
    requires_vitis_lock = True
    real_evidence_authority = "V3_PROTOTYPE_CLI_VITIS_V1"

    def execute(self, spec, *, timeout_seconds=None):
        result = super().execute(spec, timeout_seconds=timeout_seconds)
        result["backend"] = {"evidence_level": "REAL_VITIS_VALIDATED"}
        return result


class V3BatchBenchmarkTests(unittest.TestCase):
    def test_discovery_and_filters_need_no_corpus_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(
                root / "train",
                "repair_easy",
                task_type="repair",
                difficulty=1,
                expected_mode="REPAIR",
            )
            write_task(
                root / "validation",
                "opt_hard",
                task_type="repair",
                difficulty=4,
                split="validation",
            )
            (root / "validation" / "opt_hard" / "acceptance.json").write_text(
                json.dumps(
                    {
                        "task_id": "opt_hard",
                        "expected_mode": "SYNTH_FIX",
                    }
                ),
                encoding="utf-8",
            )
            broken = root / "train" / "broken"
            broken.mkdir(parents=True)
            (broken / "task.toml").write_text(
                'task_id = "broken"\ntop = "kernel"\n'
                'kernel_file = "missing.cpp"\npublic_tb = "missing_tb.cpp"\n',
                encoding="utf-8",
            )

            descriptors = discover_tasks(root)
            self.assertEqual([item.task_id for item in descriptors], [
                "broken",
                "opt_hard",
                "repair_easy",
            ])
            broken_descriptor = descriptors[0]
            self.assertFalse(broken_descriptor.loadable)
            self.assertEqual(broken_descriptor.split, "train")
            repair = next(item for item in descriptors if item.task_id == "repair_easy")
            self.assertEqual(repair.expected_mode, "REPAIR")
            self.assertEqual(repair.expected_mode_source, "benchmark.expected_mode")
            synth_fix = next(item for item in descriptors if item.task_id == "opt_hard")
            self.assertEqual(synth_fix.expected_mode, "SYNTH_FIX")
            self.assertEqual(synth_fix.expected_mode_source, "acceptance.expected_mode")

            config = BenchmarkConfig(
                corpus=root,
                output_dir=Path(directory) / "out",
                models=("model-a", "model-b"),
                splits=("train",),
                mode_filters=("repair",),
                task_filters=("repair_*",),
                difficulty_filters=("1-2",),
                model_filters=("*-b",),
            )
            selected = select_tasks(descriptors, config)
            self.assertEqual([item.task_id for item in selected], ["repair_easy"])
            self.assertEqual(select_models(config), ("model-b",))

    def test_batch_continues_after_failure_and_aggregates_every_required_metric(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "a_opt", task_type="optimize", difficulty=3)
            write_task(root, "b_repair", task_type="repair", difficulty=2)
            write_task(root, "c_struct", task_type="structural", difficulty=4)
            output = Path(directory) / "out"
            executor = FakeExecutor(
                fail_tasks={"b_repair"},
                wrong_modes={"c_struct"},
                cached_tasks={"c_struct"},
                rejected_tasks={"a_opt"},
            )
            outcome = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("fixture-model",),
                    backend="deterministic",
                ),
                executor,
            ).run()

            self.assertEqual(executor.calls, ["a_opt", "b_repair", "c_struct"])
            self.assertEqual(len(outcome.records), 3)
            self.assertEqual(
                [record["status"] for record in outcome.records],
                ["DONE", "ERROR", "DONE"],
            )
            self.assertEqual(len({record["run_id"] for record in outcome.records}), 3)
            for record in outcome.records:
                run_dir = Path(str(record["run_dir"]))
                self.assertTrue((run_dir / "benchmark_run.json").is_file())

            overall = outcome.summary["overall_all_evidence"]
            self.assertEqual(overall["runs"], 3)
            self.assertEqual(overall["e2e_successes"], 2)
            self.assertEqual(overall["fresh_final_successes"], 1)
            self.assertEqual(overall["router"], {
                "eligible": 2,
                "correct": 1,
                "accuracy": 0.5,
                "unknown": 1,
            })
            self.assertEqual(overall["usage"]["credits"]["total"], 150.0)
            self.assertEqual(overall["usage"]["tokens"]["total"], 1800.0)
            self.assertEqual(overall["usage"]["model_calls"]["total"], 4.0)
            self.assertEqual(overall["usage"]["tool_calls"]["synth"], 6)
            self.assertEqual(overall["acceleration_vs_baseline"]["count"], 1)
            self.assertEqual(overall["patch"]["rejections"], 1)
            self.assertEqual(
                overall["patch"]["reasons"], {"PATCH_POLICY_REJECTED": 1}
            )
            self.assertEqual(overall["failures"]["by_stage"], {"EXECUTOR": 1})
            self.assertEqual(
                outcome.summary["by_mode"]["OPTIMIZE"]["e2e_successes"], 1
            )
            self.assertEqual(
                outcome.summary["by_mode"]["REPAIR"]["e2e_successes"], 0
            )
            self.assertEqual(
                outcome.summary["by_mode"]["STRUCTURAL_FIX"]["e2e_successes"], 1
            )
            self.assertEqual(
                outcome.summary["by_routed_mode"]["REPAIR"]["e2e_successes"], 1
            )
            self.assertEqual(
                outcome.summary["by_expected_mode_by_evidence"]
                ["DETERMINISTIC"]["OPTIMIZE"]["runs"],
                1,
            )
            self.assertEqual(outcome.summary["real_evidence_headline"]["runs"], 0)

            lines = (output / "benchmark_results.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "summary.csv").is_file())
            self.assertEqual(
                (output / "benchmark_summary.json").read_bytes(),
                (output / "summary.json").read_bytes(),
            )
            self.assertEqual(
                (output / "benchmark_summary.csv").read_bytes(),
                (output / "summary.csv").read_bytes(),
            )
            report = (output / "report.md").read_text(encoding="utf-8")
            self.assertEqual(
                (output / "benchmark_report.md").read_text(encoding="utf-8"),
                report,
            )
            self.assertIn(
                "DEMO and DETERMINISTIC",
                report,
            )
            self.assertIn("6/6/4/4", report)
            with (output / "summary.csv").open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["run_id"] for row in rows}, {
                record["run_id"] for record in outcome.records
            })

    def test_resume_uses_fingerprint_and_does_not_reinvoke_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            task_dir = write_task(root, "only_task")
            output = Path(directory) / "out"
            config = BenchmarkConfig(
                corpus=root,
                output_dir=output,
                models=("same-model",),
                repeats=2,
                backend="deterministic",
            )
            first_executor = FakeExecutor()
            first = BatchBenchmarkRunner(config, first_executor).run()
            self.assertEqual(len(first_executor.calls), 2)

            resume_executor = FakeExecutor()
            resumed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("same-model",),
                    repeats=2,
                    backend="deterministic",
                    resume=True,
                ),
                resume_executor,
            ).run()
            self.assertEqual(resume_executor.calls, [])
            self.assertEqual(resumed.summary["execution"]["resumed_runs"], 2)
            self.assertTrue(all(record["resumed"] for record in resumed.records))
            self.assertEqual(
                [record["run_id"] for record in first.records],
                [record["run_id"] for record in resumed.records],
            )
            self.assertEqual(
                len((output / "benchmark_results.jsonl").read_text().splitlines()), 2
            )

            (task_dir / "kernel.cpp").write_text(
                "void kernel(int *x) { x[0] += 2; }\n", encoding="utf-8"
            )
            changed_executor = FakeExecutor()
            changed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("same-model",),
                    repeats=2,
                    backend="deterministic",
                    resume=True,
                ),
                changed_executor,
            ).run()
            self.assertEqual(len(changed_executor.calls), 2)
            self.assertEqual(changed.summary["execution"]["new_runs"], 2)
            self.assertNotEqual(
                first.records[0]["run_fingerprint"],
                changed.records[0]["run_fingerprint"],
            )

    def test_task_load_failure_is_a_record_and_later_task_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            broken = root / "a_broken"
            broken.mkdir(parents=True)
            (broken / "task.toml").write_text(
                'task_id="a_broken"\ntop="kernel"\n'
                'kernel_file="missing.cpp"\npublic_tb="missing_tb.cpp"\n',
                encoding="utf-8",
            )
            write_task(root, "b_good")
            executor = FakeExecutor()
            outcome = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "out",
                    backend="deterministic",
                ),
                executor,
            ).run()

            self.assertEqual(len(outcome.records), 2)
            self.assertEqual(outcome.records[0]["failure_stage"], "TASK_LOAD")
            self.assertFalse(outcome.records[0]["execution_started"])
            self.assertEqual(executor.calls, ["b_good"])
            self.assertTrue(outcome.records[1]["e2e_success"])

    def test_retry_failures_uses_a_fresh_independent_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "retry_task")
            output = Path(directory) / "out"
            failed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                ),
                FakeExecutor(fail_tasks={"retry_task"}),
            ).run()
            self.assertEqual(failed.records[0]["status"], "ERROR")
            failed_run_dir = failed.records[0]["run_dir"]

            retry_executor = FakeExecutor()
            retried = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                    resume=True,
                    retry_failures=True,
                ),
                retry_executor,
            ).run()
            self.assertEqual(retry_executor.calls, ["retry_task"])
            self.assertTrue(retried.records[0]["e2e_success"])
            self.assertNotEqual(retried.records[0]["run_dir"], failed_run_dir)
            self.assertIn("--retry001", retried.records[0]["run_id"])
            self.assertEqual(
                len((output / "benchmark_results.jsonl").read_text().splitlines()),
                2,
            )
            self.assertEqual(
                retried.summary["populations"]["all_attempts"]
                ["overall_all_evidence"]["runs"],
                2,
            )
            self.assertEqual(
                retried.summary["populations"]["all_attempts"]
                ["overall_all_evidence"]["e2e_success_rate"],
                0.5,
            )
            self.assertEqual(
                retried.summary["populations"]["latest_slots"]
                ["overall_all_evidence"]["runs"],
                1,
            )
            self.assertEqual(
                retried.summary["populations"]["latest_slots"]
                ["overall_all_evidence"]["e2e_success_rate"],
                1.0,
            )
            with (output / "summary.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_resume_reexecutes_when_durable_record_disagrees_with_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "tamper_task")
            output = Path(directory) / "out"
            first = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                ),
                FakeExecutor(),
            ).run()
            durable_path = Path(str(first.records[0]["run_dir"])) / "benchmark_run.json"
            durable = json.loads(durable_path.read_text(encoding="utf-8"))
            durable["credits_used"] = 999
            durable_path.write_text(json.dumps(durable), encoding="utf-8")

            executor = FakeExecutor()
            resumed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    backend="deterministic",
                    resume=True,
                ),
                executor,
            ).run()
            self.assertEqual(executor.calls, ["tamper_task"])
            self.assertEqual(resumed.summary["execution"]["new_runs"], 1)
            self.assertFalse(resumed.records[0]["resumed"])

    def test_demo_is_excluded_from_real_headline_and_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "demo_task")
            demo = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "demo",
                    backend="demo",
                ),
                DemoExecutor(),
            ).run()
            self.assertTrue(demo.records[0]["e2e_success"])
            self.assertEqual(demo.summary["by_evidence_class"]["DEMO"]["runs"], 1)
            self.assertEqual(demo.summary["real_evidence_headline"]["runs"], 0)

            class LyingDemo(DemoExecutor):
                def execute(self, spec, *, timeout_seconds=None):
                    result = super().execute(spec, timeout_seconds=timeout_seconds)
                    result["backend"] = {"evidence_level": "REAL_VITIS_VALIDATED"}
                    return result

            lying = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "lying",
                    backend="demo",
                ),
                LyingDemo(fingerprint="lying-demo:v1"),
            ).run()
            self.assertFalse(lying.records[0]["e2e_success"])
            self.assertEqual(lying.records[0]["failure_stage"], "EVIDENCE_POLICY")
            self.assertEqual(lying.summary["real_evidence_headline"]["runs"], 0)

    def test_vitis_backend_is_serial_and_fake_cannot_claim_real_headline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "a")
            write_task(root, "b")
            executor = RealFixtureExecutor(fingerprint="fake-real-policy-test:v1")
            outcome = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "out",
                    backend="vitis",
                ),
                executor,
            ).run()

            self.assertEqual(executor.calls, ["a", "b"])
            self.assertEqual(outcome.summary["execution"]["max_parallel_runs"], 1)
            self.assertTrue(outcome.summary["execution"]["vitis_serialized"])
            self.assertEqual(outcome.summary["by_evidence_class"]["REAL"]["runs"], 2)
            self.assertEqual(outcome.summary["real_evidence_headline"]["runs"], 0)
            self.assertTrue(
                all(record["failure_stage"] == "EVIDENCE_POLICY" for record in outcome.records)
            )

    def test_nonempty_preexisting_run_directory_is_never_used_as_a_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "stale_task")
            output = Path(directory) / "out"
            config = BenchmarkConfig(
                corpus=root,
                output_dir=output,
                backend="deterministic",
            )
            executor = FakeExecutor()
            runner = BatchBenchmarkRunner(config, executor)
            descriptor = discover_tasks(root)[0]
            planned = runner._plan([descriptor], select_models(config))
            run_id = planned[0][4]
            stale = output / "runs" / run_id
            stale.mkdir(parents=True)
            (stale / "v3_prototype_result.json").write_text(
                json.dumps(fake_result("OPTIMIZE")), encoding="utf-8"
            )

            outcome = runner.run()

            self.assertEqual(executor.calls, [])
            self.assertEqual(outcome.records[0]["status"], "ERROR")
            self.assertEqual(outcome.records[0]["failure_stage"], "EXECUTOR")
            self.assertIn("--blocked001", outcome.records[0]["run_id"])
            self.assertTrue((stale / "v3_prototype_result.json").is_file())

    def test_real_executor_rejects_identity_and_authority_overrides(self) -> None:
        forbidden = (
            "--task-dir",
            "--run-dir",
            "--thread-id",
            "--backend",
            "--planner",
            "--live-openai",
            "--patch-file",
            "--model",
        )
        for option in forbidden:
            with self.subTest(option=option, spelling="separate"):
                with self.assertRaisesRegex(BenchmarkError, option):
                    V3PrototypeCLIExecutor((option, "untrusted-value"))
            with self.subTest(option=option, spelling="equals"):
                with self.assertRaisesRegex(BenchmarkError, option):
                    V3PrototypeCLIExecutor((f"{option}=untrusted-value",))

    def test_real_executor_refuses_preexisting_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "stale_real_task")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            stale_result = run_dir / "v3_prototype_result.json"
            stale_payload = fake_result("OPTIMIZE")
            stale_result.write_text(json.dumps(stale_payload), encoding="utf-8")
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="real-model",
                repeat_index=1,
                backend="vitis",
                run_id="stale-real-run",
                run_fingerprint="f" * 64,
                run_dir=run_dir,
            )

            with self.assertRaisesRegex(
                BenchmarkExecutionError, "pre-existing V3 terminal result"
            ):
                V3PrototypeCLIExecutor().execute(spec)
            self.assertEqual(
                json.loads(stale_result.read_text(encoding="utf-8")), stale_payload
            )

    def test_max_tasks_and_runtime_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            for task_id in ("a", "b", "c"):
                write_task(root, task_id)
            limited = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "max-tasks",
                    backend="deterministic",
                    max_tasks=2,
                    repeats=2,
                ),
                FakeExecutor(),
            ).run()
            self.assertEqual(limited.summary["selection"]["tasks_selected"], 2)
            self.assertEqual(limited.summary["selection"]["planned_runs"], 4)

            class StepClock:
                def __init__(self) -> None:
                    self.value = 0.0

                def __call__(self) -> float:
                    self.value += 0.4
                    return self.value

            clock = StepClock()
            runtime_limited = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=Path(directory) / "runtime",
                    backend="deterministic",
                    max_runtime_seconds=1.0,
                ),
                FakeExecutor(),
                monotonic=clock,
            ).run()
            self.assertEqual(
                runtime_limited.summary["execution"]["stopped_reason"],
                "MAX_RUNTIME_REACHED",
            )
            self.assertLess(len(runtime_limited.records), 3)

    def test_output_directory_requires_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "task")
            output = Path(directory) / "out"
            config = BenchmarkConfig(
                corpus=root, output_dir=output, backend="deterministic"
            )
            BatchBenchmarkRunner(config, FakeExecutor()).run()
            with self.assertRaisesRegex(BenchmarkError, "--resume"):
                BatchBenchmarkRunner(config, FakeExecutor()).run()

    def test_cli_runs_small_deterministic_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "repair", task_type="repair", difficulty=2)
            write_task(root, "optimize", task_type="optimize", difficulty=3)
            output = Path(directory) / "out"
            stdout = io.StringIO()
            code = main(
                [
                    "--corpus",
                    str(root),
                    "--output-dir",
                    str(output),
                    "--backend",
                    "deterministic",
                    "--models",
                    "fixture-a,fixture-b",
                    "--model",
                    "fixture-a",
                    "--repeats",
                    "2",
                    "--max-tasks",
                    "1",
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(code, 0)
            self.assertEqual(payload["schema_version"], SUMMARY_SCHEMA)
            self.assertEqual(payload["records"], 2)
            self.assertEqual(
                payload["benchmark_summary_ref"], "benchmark_summary.json"
            )
            self.assertEqual(payload["benchmark_csv_ref"], "benchmark_summary.csv")
            self.assertEqual(payload["benchmark_report_ref"], "benchmark_report.md")
            self.assertEqual(summary["real_evidence_headline"]["runs"], 0)
            self.assertEqual(
                summary["by_evidence_class"]["DETERMINISTIC"]["runs"], 2
            )
            records = [
                json.loads(line)
                for line in (output / "benchmark_results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(all(record["schema_version"] == RUN_SCHEMA for record in records))
            self.assertTrue(
                all(record["status"] == "ORCHESTRATION_ONLY" for record in records)
            )
            self.assertTrue(all(record["e2e_success"] is None for record in records))
            self.assertTrue(
                all(record["fresh_final_success"] is None for record in records)
            )
            self.assertTrue(all(record["router_correct"] is None for record in records))
            self.assertTrue(
                all(record["acceleration_vs_baseline"] is None for record in records)
            )
            self.assertTrue(
                all(
                    record["orchestration_receipt_ref"] == "executor_result.json"
                    for record in records
                )
            )
            for record in records:
                receipt = json.loads(
                    (Path(str(record["run_dir"])) / "executor_result.json").read_text()
                )
                self.assertEqual(
                    receipt["result_schema"], "v3d.orchestration-receipt.v1"
                )
                for fabricated_field in (
                    "mode",
                    "phase_decision",
                    "final_validation",
                    "candidate_rounds",
                    "acceleration_vs_baseline",
                ):
                    self.assertNotIn(fabricated_field, receipt)
            deterministic = summary["by_evidence_class"]["DETERMINISTIC"]
            self.assertEqual(deterministic["runs"], 2)
            self.assertEqual(deterministic["e2e_eligible"], 0)
            self.assertIsNone(deterministic["e2e_success_rate"])
            facts = summary["implementation_facts"]
            self.assertEqual(
                set(facts["category_sha256"]),
                {
                    "batch_and_task_parsing",
                    "phase_router",
                    "planner_and_prompts",
                    "backend_and_accounting",
                },
            )
            self.assertIn("openai_provider.py", facts["modules"])
            self.assertIn("vitis.py", facts["modules"])
            plan = json.loads((output / "benchmark_plan.json").read_text())
            self.assertEqual(
                plan["implementation_fingerprint"],
                summary["implementation_fingerprint"],
            )

    def test_real_validator_binds_model_provider_usage_and_all_decisive_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "real_task")
            descriptor = discover_tasks(root)[0]
            assert descriptor.task is not None
            executor = V3PrototypeCLIExecutor()
            spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="real-fixture-run",
                run_fingerprint="f" * 64,
                run_dir=Path(directory) / "run",
            )
            result = write_real_fixture(executor, spec)
            receipt = executor._validate_terminal_provenance(spec, result)
            self.assertEqual(
                receipt["source_result"]["sha256"],
                hashlib.sha256(
                    (spec.run_dir / "v3_prototype_result.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                receipt["model_outcomes"][0]["model"], "scheduled-model"
            )
            self.assertEqual(receipt["token_ledger"]["tokens_used"], 10)
            self.assertEqual(
                set(receipt["final_validation_artifacts"]),
                {"csim", "synth", "cosim"},
            )

            bad_spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="bad-model-run",
                run_fingerprint="e" * 64,
                run_dir=Path(directory) / "bad-model",
            )
            bad_result = write_real_fixture(
                executor, bad_spec, outcome_model="different-model"
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError, "provider/model binding"
            ):
                executor._validate_terminal_provenance(bad_spec, bad_result)

            bad_ledger_spec = BenchmarkRunSpec(
                task=descriptor.task,
                descriptor=descriptor,
                model="scheduled-model",
                repeat_index=1,
                backend="vitis",
                run_id="bad-ledger-run",
                run_fingerprint="d" * 64,
                run_dir=Path(directory) / "bad-ledger",
            )
            bad_ledger_result = write_real_fixture(
                executor, bad_ledger_spec, ledger_tokens=11
            )
            with self.assertRaisesRegex(
                BenchmarkExecutionError, "token ledger"
            ):
                executor._validate_terminal_provenance(
                    bad_ledger_spec, bad_ledger_result
                )

    def test_real_resume_revalidates_package_and_recovers_in_a_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            write_task(root, "real_resume")
            output = Path(directory) / "out"

            first_executor = V3PrototypeCLIExecutor()
            first_calls: list[str] = []

            def first_execute(spec, *, timeout_seconds=None):
                del timeout_seconds
                first_calls.append(spec.run_id)
                return write_real_fixture(first_executor, spec)

            first_executor.execute = first_execute  # type: ignore[method-assign]
            first = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("scheduled-model",),
                    backend="vitis",
                ),
                first_executor,
            ).run()
            self.assertEqual(len(first_calls), 1)
            self.assertTrue(first.records[0]["e2e_success"])

            first_run_dir = Path(str(first.records[0]["run_dir"]))
            final_csim = first_run_dir / "actions/final-csim/result.json"
            tampered = json.loads(final_csim.read_text(encoding="utf-8"))
            tampered["ok"] = False
            final_csim.write_text(json.dumps(tampered), encoding="utf-8")

            recovery_executor = V3PrototypeCLIExecutor()
            recovery_calls: list[str] = []

            def recovery_execute(spec, *, timeout_seconds=None):
                del timeout_seconds
                recovery_calls.append(spec.run_id)
                return write_real_fixture(recovery_executor, spec)

            recovery_executor.execute = recovery_execute  # type: ignore[method-assign]
            resumed = BatchBenchmarkRunner(
                BenchmarkConfig(
                    corpus=root,
                    output_dir=output,
                    models=("scheduled-model",),
                    backend="vitis",
                    resume=True,
                ),
                recovery_executor,
            ).run()
            self.assertEqual(len(recovery_calls), 1)
            self.assertIn("--recovery001", recovery_calls[0])
            self.assertFalse(resumed.records[0]["resumed"])
            self.assertEqual(
                resumed.summary["latest_slot_population"]["e2e_success_rate"],
                1.0,
            )
            self.assertEqual(
                resumed.summary["all_attempt_population"]["runs"], 2
            )
            self.assertEqual(
                resumed.summary["all_attempt_population"]["e2e_success_rate"],
                0.5,
            )
            self.assertEqual(
                resumed.summary["real_evidence_headline"]["e2e_success_rate"],
                0.5,
            )
            self.assertEqual(
                resumed.summary["populations"]["all_attempts"]
                ["overall_all_evidence"]["failures"]["by_stage"],
                {"PROVENANCE_RESUME": 1},
            )

    def test_summary_builder_keeps_demo_and_real_populations_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = BenchmarkConfig(
                corpus=Path(directory),
                output_dir=Path(directory) / "out",
                backend="deterministic",
            )
            base = {
                "run_id": "demo",
                "run_fingerprint": "a" * 64,
                "e2e_success": True,
                "fresh_final_success": True,
                "final_validation_success": True,
                "router_correct": True,
                "routed_mode": "OPTIMIZE",
                "model": "fixture",
                "credits_used": 1,
                "tokens_used": 2,
                "tool_calls": {"llm": 1},
                "wall_time_s": 0.1,
                "patch_candidates": 0,
                "patch_rejections": 0,
            }
            demo = {**base, "evidence_class": "DEMO", "real_evidence_eligible": False}
            deterministic = {
                **base,
                "run_id": "det",
                "run_fingerprint": "b" * 64,
                "evidence_class": "DETERMINISTIC",
                "real_evidence_eligible": False,
            }
            real = {
                **base,
                "run_id": "real",
                "run_fingerprint": "c" * 64,
                "evidence_class": "REAL",
                "real_evidence_eligible": True,
                "e2e_success": False,
                "fresh_final_success": False,
                "failure_stage": "FINAL_COSIM",
                "stop_reason": "FINAL_COSIM_FAILED",
            }
            summary = build_summary(
                config=config,
                executor_fingerprint="fixture",
                descriptors_found=0,
                selected_tasks=[],
                selected_models=("fixture",),
                planned_runs=3,
                records=[demo, deterministic, real],
                new_runs=3,
                resumed_runs=0,
                stopped_reason=None,
                batch_elapsed_s=0.3,
            )
            self.assertEqual(summary["schema_version"], SUMMARY_SCHEMA)
            self.assertEqual(summary["overall_all_evidence"]["runs"], 3)
            self.assertEqual(summary["real_evidence_headline"]["runs"], 1)
            self.assertEqual(summary["real_evidence_headline"]["e2e_successes"], 0)
            self.assertEqual(summary["by_evidence_class"]["DEMO"]["runs"], 1)
            self.assertEqual(
                summary["by_evidence_class"]["DETERMINISTIC"]["runs"], 1
            )


if __name__ == "__main__":
    unittest.main()
