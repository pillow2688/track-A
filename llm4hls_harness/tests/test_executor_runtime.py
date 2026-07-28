from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig, BudgetLedger
from llm4hls_agent.runtime_control import (
    COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME,
    FINAL_CLOSURE_UNAFFORDABLE_RUNTIME,
    RuntimeDeadline,
    RuntimeUnavailable,
)
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig, ToolServer
from llm4hls_agent.v3_batch_benchmark import (
    BenchmarkExecutionTimeout,
    BenchmarkRunSpec,
    EvidenceClass,
    TaskDescriptor,
    V3PrototypeCLIExecutor,
    _failure_record,
    _enable_child_subreaper,
    _process_group_members,
    _terminate_executor_process_group,
    recover_partial_run_report,
)


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _CaptureBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    def run(self, kind: str, **kwargs: object) -> BackendResult:
        config = kwargs["config"]
        assert isinstance(config, ToolConfig)
        self.calls.append(kind)
        self.timeouts.append(config.timeout_for(kind))
        return BackendResult(
            ok=True,
            phase="pass",
            return_code=0,
            elapsed_s=0.01,
        )


def _make_task(root: Path) -> None:
    (root / "task.toml").write_text(
        "\n".join(
            (
                'task_id = "executor_runtime_fixture"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'public_tb = "kernel_tb.cpp"',
                "budget = 80",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            )
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text(
        "void kernel(int *value) { *value += 1; }\n",
        encoding="utf-8",
    )
    (root / "kernel_tb.cpp").write_text(
        "int main() { return 0; }\n",
        encoding="utf-8",
    )


def _write_json(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    path.write_bytes(data)
    return data


def _write_partial_tool_fixture(root: Path, *, include_result: bool = True) -> None:
    completed_id = "a" * 64
    pending_id = "b" * 64
    result_ref = f"actions/{completed_id}/result.json"
    result = {
        "action_id": completed_id,
        "kind": "csim",
        "candidate_id": "candidate_000",
        "code_hash": "c" * 64,
        "tool_config_hash": "d" * 64,
        "result_ref": result_ref,
        "artifacts": {},
        "artifact_hashes": {},
    }
    result_data = (
        _write_json(root / result_ref, result) if include_result else b"missing"
    )
    events = [
        {
            "sequence": 0,
            "state": "INITIALIZED",
            "config": {"credit_limit": 80},
        },
        {
            "sequence": 1,
            "state": "STARTED",
            "action_id": completed_id,
            "kind": "csim",
            "candidate_id": "candidate_000",
            "code_hash": "c" * 64,
            "tool_config_hash": "d" * 64,
            "estimated_cost": 1,
            "estimated_tokens": 0,
        },
        {
            "sequence": 2,
            "state": "COMPLETED",
            "action_id": completed_id,
            "kind": "csim",
            "actual_cost": 1,
            "tokens_used": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "result_ref": result_ref,
            "result_sha256": hashlib.sha256(result_data).hexdigest(),
        },
        {
            "sequence": 3,
            "state": "STARTED",
            "action_id": pending_id,
            "kind": "cosim",
            "candidate_id": "candidate_001",
            "code_hash": "e" * 64,
            "tool_config_hash": "f" * 64,
            "estimated_cost": 20,
            "estimated_tokens": 0,
        },
    ]
    root.mkdir(parents=True, exist_ok=True)
    (root / "budget_ledger.jsonl").write_text(
        "".join(
            json.dumps(event, sort_keys=True) + "\n" for event in events
        ),
        encoding="utf-8",
    )
    (root / "trace.jsonl").write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "event": "V3_NODE_COMPLETED",
                        "node": "candidate_csim",
                    }
                ),
                json.dumps(
                    {
                        "event": "TOOL_STARTED",
                        "kind": "cosim",
                        "action_id": pending_id,
                        "candidate_id": "candidate_001",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


@unittest.skipUnless(os.name == "posix", "process-group tests require POSIX")
class ExecutorProcessGroupTests(unittest.TestCase):
    def _start_ignoring_tree(self) -> subprocess.Popen[str]:
        child_code = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "time.sleep(60)"
        )
        parent_code = (
            "import signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"subprocess.Popen([sys.executable,'-c',{child_code!r},"
            "'vitis','xsim']);"
            "print('started',flush=True);"
            "time.sleep(60)"
        )
        _enable_child_subreaper()
        return subprocess.Popen(
            [sys.executable, "-c", parent_code, "executor-run"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def test_ignoring_process_tree_is_sigkilled_and_reaped(self) -> None:
        process = self._start_ignoring_tree()
        assert process.stdout is not None
        marker = process.stdout.readline()
        _stdout, _stderr, cleanup = _terminate_executor_process_group(
            process,
            pgid=process.pid,
            term_grace_seconds=0.1,
            kill_grace_seconds=1.0,
        )
        self.assertIn("started", marker)
        self.assertTrue(cleanup["term_sent"])
        self.assertTrue(cleanup["kill_sent"])
        self.assertTrue(cleanup["parent_reaped"])
        self.assertTrue(cleanup["process_tree_cleaned"])
        self.assertEqual(_process_group_members(process.pid), [])

    def test_graceful_process_group_needs_no_sigkill(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        _stdout, _stderr, cleanup = _terminate_executor_process_group(
            process,
            pgid=process.pid,
            term_grace_seconds=1.0,
            kill_grace_seconds=1.0,
        )
        self.assertTrue(cleanup["term_sent"])
        self.assertFalse(cleanup["kill_sent"])
        self.assertTrue(cleanup["process_tree_cleaned"])

    def test_cleanup_does_not_kill_an_unrelated_session(self) -> None:
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        target = self._start_ignoring_tree()
        try:
            assert target.stdout is not None
            self.assertIn("started", target.stdout.readline())
            _terminate_executor_process_group(
                target,
                pgid=target.pid,
                term_grace_seconds=0.1,
                kill_grace_seconds=1.0,
            )
            self.assertIsNone(sentinel.poll())
        finally:
            if sentinel.poll() is None:
                os.killpg(sentinel.pid, signal.SIGKILL)
                sentinel.wait(timeout=2.0)

    def test_real_executor_timeout_uses_group_cleanup_and_partial_report(
        self,
    ) -> None:
        child_code = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "time.sleep(60)"
        )
        parent_code = (
            "import signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            "print('executor-started',flush=True);"
            "time.sleep(60)"
        )

        class _DummyExecutor(V3PrototypeCLIExecutor):
            process_term_grace_seconds = 0.1
            process_kill_grace_seconds = 1.0

            def _command(self, _spec: BenchmarkRunSpec) -> list[str]:
                return [sys.executable, "-c", parent_code]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "task"
            task_root.mkdir()
            _make_task(task_root)
            task = load_public_task(task_root)
            run_root = root / "run"
            descriptor = TaskDescriptor(
                directory=task_root,
                relative_path="task",
                task_id=task.id,
                task_type=task.task_type,
                difficulty=None,
                split="public",
                expected_mode="OPTIMIZE",
                expected_mode_source="fixture",
                task_fingerprint="1" * 64,
                task=task,
            )
            spec = BenchmarkRunSpec(
                task=task,
                descriptor=descriptor,
                model="dummy",
                repeat_index=1,
                backend="vitis",
                run_id="executor-runtime-test",
                run_fingerprint="2" * 64,
                run_dir=run_root,
            )
            with self.assertRaises(BenchmarkExecutionTimeout) as caught:
                _DummyExecutor().execute(spec, timeout_seconds=0.2)
            cleanup = caught.exception.process_cleanup
            partial = caught.exception.partial_report
            assert cleanup is not None
            assert partial is not None
            self.assertTrue(cleanup["process_tree_cleaned"])
            self.assertEqual(partial["status"], "ABSENT")
            self.assertEqual(partial["credits_used"], "UNKNOWN")
            self.assertIn(
                "executor-started",
                (run_root / "benchmark_stdout.log").read_text(
                    encoding="utf-8"
                ),
            )


class RuntimeDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        task_root = root / "task"
        task_root.mkdir()
        _make_task(task_root)
        self.task = load_public_task(task_root)
        self.run_root = root / "run"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _server(
        self,
        deadline: RuntimeDeadline,
        backend: _CaptureBackend,
    ) -> tuple[BudgetLedger, ToolServer]:
        budget = BudgetLedger(
            self.run_root / "budget_ledger.jsonl",
            BudgetConfig(
                credit_limit=80,
                costs={"csim": 1, "synth": 4, "cosim": 20},
                tool_limits={"csim": 4, "synth": 4, "cosim": 4},
                token_limit=1000,
                runtime_limit_seconds=1000.0,
            ),
        )
        return budget, ToolServer(
            task=self.task,
            budget=budget,
            run_root=self.run_root,
            config=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=self.task.part,
                clock_ns=self.task.clock_ns,
                timeouts={"csim": 300, "synth": 300, "cosim": 1800},
            ),
            backend=backend,
            runtime_deadline=deadline,
        )

    def test_tool_timeout_is_clipped_to_deadline_minus_cleanup(self) -> None:
        clock = _Clock(100.0)
        backend = _CaptureBackend()
        _budget, server = self._server(
            RuntimeDeadline(
                200.0,
                cleanup_reserve_seconds=30.0,
                monotonic=clock,
            ),
            backend,
        )
        result = server.csim(self.task.kernel_bytes)
        self.assertEqual(backend.calls, ["csim"])
        self.assertEqual(backend.timeouts, [70.0])
        self.assertEqual(result.effective_timeout_seconds, 70.0)

    def test_cleanup_reserve_blocks_before_ledger_start(self) -> None:
        clock = _Clock(170.0)
        backend = _CaptureBackend()
        budget, server = self._server(
            RuntimeDeadline(
                200.0,
                cleanup_reserve_seconds=30.0,
                monotonic=clock,
            ),
            backend,
        )
        with self.assertRaises(RuntimeUnavailable):
            server.csim(self.task.kernel_bytes)
        self.assertEqual(backend.calls, [])
        self.assertEqual(budget.snapshot()["tool_used"]["csim"], 0)
        self.assertEqual(budget.snapshot()["tool_pending"]["csim"], 0)

    def test_cosim_below_minimum_window_is_not_started(self) -> None:
        clock = _Clock(120.0)
        backend = _CaptureBackend()
        budget, server = self._server(
            RuntimeDeadline(
                200.0,
                cleanup_reserve_seconds=30.0,
                cosim_minimum_runtime_seconds=60.0,
                monotonic=clock,
            ),
            backend,
        )
        with self.assertRaises(RuntimeUnavailable) as caught:
            server.cosim(self.task.kernel_bytes)
        self.assertEqual(
            caught.exception.reason_code,
            COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME,
        )
        self.assertEqual(backend.calls, [])
        self.assertEqual(budget.snapshot()["tool_pending"]["cosim"], 0)

    def test_final_closure_has_distinct_runtime_reason(self) -> None:
        clock = _Clock(130.0)
        permit = RuntimeDeadline(
            200.0,
            cleanup_reserve_seconds=30.0,
            monotonic=clock,
        ).permit(
            300.0,
            operation="cosim",
            final_closure=True,
        )
        self.assertFalse(permit.allowed)
        self.assertEqual(
            permit.reason_code,
            FINAL_CLOSURE_UNAFFORDABLE_RUNTIME,
        )


class PartialArtifactRecoveryTests(unittest.TestCase):
    def test_completed_and_pending_usage_are_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_partial_tool_fixture(root)
            before = (root / "budget_ledger.jsonl").read_bytes()
            report = recover_partial_run_report(root)
            self.assertEqual(report["status"], "VALID")
            self.assertEqual(report["credits_used"], 1)
            self.assertEqual(report["pending_credits_reserved"], 20)
            self.assertEqual(report["credits_accounted_conservative"], 21)
            self.assertEqual(report["tool_calls"], {"csim": 1})
            self.assertEqual(report["pending_tool_calls"], {"cosim": 1})
            self.assertEqual(
                report["unfinished_action_ids"],
                ["b" * 64],
            )
            self.assertEqual(
                (root / "budget_ledger.jsonl").read_bytes(),
                before,
            )

    def test_missing_completed_result_fails_closed_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_partial_tool_fixture(root, include_result=False)
            report = recover_partial_run_report(root)
            self.assertEqual(report["status"], "INVALID")
            self.assertFalse(
                report["usage_recovered_from_partial_artifacts"]
            )
            self.assertEqual(report["credits_used"], "UNKNOWN")

    def test_absent_ledger_uses_unknown_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = recover_partial_run_report(directory)
            self.assertEqual(report["status"], "ABSENT")
            self.assertEqual(report["credits_used"], "UNKNOWN")
            self.assertEqual(report["tokens_used"], "UNKNOWN")
            self.assertEqual(report["planner_calls"], "UNKNOWN")

    def test_timeout_failure_row_uses_executor_status_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "task"
            task_root.mkdir()
            _make_task(task_root)
            task = load_public_task(task_root)
            descriptor = TaskDescriptor(
                directory=task_root,
                relative_path="task",
                task_id=task.id,
                task_type=task.task_type,
                difficulty=None,
                split="public",
                expected_mode="OPTIMIZE",
                expected_mode_source="fixture",
                task_fingerprint="3" * 64,
                task=task,
            )
            partial = recover_partial_run_report(root / "run")
            record = _failure_record(
                descriptor,
                model="dummy",
                repeat_index=1,
                backend="vitis",
                evidence_class=EvidenceClass.REAL,
                executor_fingerprint="executor",
                run_id="run",
                run_fingerprint="4" * 64,
                run_dir=root / "run",
                started_at="start",
                finished_at="finish",
                wall_time_s=1.0,
                error_type="BenchmarkExecutionTimeout",
                detail="timeout",
                execution_started=True,
                partial_report=partial,
            )
            self.assertEqual(record["status"], "EXECUTOR_TIMEOUT")
            self.assertEqual(
                record["stop_reason"],
                "EXECUTOR_TIMEOUT_AND_INCOMPLETE_ARTIFACT",
            )
            self.assertEqual(record["credits_used"], "UNKNOWN")
            self.assertEqual(record["tokens_used"], "UNKNOWN")
            self.assertEqual(record["model_calls"], "UNKNOWN")

    def test_existing_020_incomplete_run_is_recovered_read_only(self) -> None:
        project = Path(__file__).resolve().parents[1]
        run_root = (
            project
            / "runs"
            / "full-agent-v3d-fast-targeted-rerun-20260728-a01"
            / "v3d_fast_020"
            / "runs"
            / "v3d_fast_020--deepseek-v4-pro--r001--3cd15b547f31"
        )
        if not run_root.is_dir():
            self.skipTest("the historical incomplete 020 run is unavailable")
        ledger_before = (run_root / "budget_ledger.jsonl").read_bytes()
        report = recover_partial_run_report(run_root)
        self.assertEqual(report["status"], "VALID")
        self.assertEqual(report["credits_used"], 26)
        self.assertEqual(report["pending_credits_reserved"], 20)
        self.assertEqual(report["planner_calls"], 1)
        self.assertEqual(report["tokens_used"], 2594)
        self.assertEqual(report["input_tokens_used"], 2120)
        self.assertEqual(report["output_tokens_used"], 474)
        self.assertEqual(report["cached_input_tokens_used"], 256)
        self.assertEqual(
            report["last_completed_graph_node"],
            "candidate_csim",
        )
        self.assertEqual(
            report["last_started_tool"]["kind"],  # type: ignore[index]
            "cosim",
        )
        self.assertFalse(report["candidate_frozen"])
        self.assertFalse(report["agent_terminal_present"])
        self.assertEqual(
            (run_root / "budget_ledger.jsonl").read_bytes(),
            ledger_before,
        )


if __name__ == "__main__":
    unittest.main()
