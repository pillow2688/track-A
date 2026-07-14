from __future__ import annotations

import json
import math
import stat
import tempfile
import unittest
from pathlib import Path

try:
    from llm4hls_agent.budget import BudgetConfig, BudgetLedgerError
    from llm4hls_agent.task import load_public_task
    from llm4hls_agent.tools import BackendResult, ToolConfig
    from llm4hls_agent.workflow import RunConfig, run_v0
except ModuleNotFoundError:
    def _missing(*_args: object, **_kwargs: object):
        raise AssertionError("deterministic V0 workflow is not implemented")

    BudgetConfig = ToolConfig = RunConfig = BackendResult = _missing  # type: ignore[misc,assignment]
    load_public_task = run_v0 = _missing


def make_task(root: Path, *, task_id: str, requires_cosim: bool = False) -> None:
    (root / "task.toml").write_text(
        "\n".join(
            [
                f'task_id = "{task_id}"',
                'task_type = "structural"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 80",
                f"requires_cosim = {'true' if requires_cosim else 'false'}",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "description.md").write_text("Public description.\n", encoding="utf-8")
    (root / "kernel.cpp").write_bytes(b'#include "kernel.h"\nvoid kernel() {}\n')
    (root / "kernel.h").write_bytes(b"void kernel();\n")
    (root / "kernel_tb.cpp").write_bytes(b"int main() { return 0; }\n")


def synth_report() -> dict[str, object]:
    return {
        "estimated_clock_period_ns": 4.25,
        "latency": {"best": 10, "average": 10, "worst": 10},
        "interval": {"min": 1, "max": 1},
        "resources": {"LUT": 1, "FF": 2, "DSP": 0, "BRAM_18K": 0, "URAM": 0},
        "available_resources": {},
        "utilization_percent": {},
    }


class ScenarioBackend:
    def __init__(self, outcomes: dict[str, BackendResult]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def run(self, kind: str, **_kwargs: object) -> BackendResult:
        self.calls.append(kind)
        return self.outcomes[kind]


class InterruptingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, kind: str, **_kwargs: object) -> BackendResult:
        self.calls.append(kind)
        raise KeyboardInterrupt("simulated hard interruption")


class DeterministicWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def config(self, task) -> RunConfig:
        return RunConfig(
            tool=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=task.part,
                clock_ns=task.clock_ns,
                timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
            ),
            budget=BudgetConfig(
                credit_limit=80,
                costs={"csim": 1, "synth": 4, "cosim": 20},
                tool_limits={"csim": None, "synth": None, "cosim": None},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
            minimum_frequency_mhz=100.0,
        )

    def test_all_pass_creates_immutable_baseline_and_reuses_all_actions(self) -> None:
        task_dir = self.root / "dotProduct_optimize"
        task_dir.mkdir()
        make_task(task_dir, task_id="dotProduct_optimize")
        original = {path.name: path.read_bytes() for path in task_dir.iterdir() if path.is_file()}
        task = load_public_task(task_dir)
        backend = ScenarioBackend(
            {
                "csim": BackendResult(True, "pass", 0, 0.1),
                "synth": BackendResult(True, "pass", 0, 0.2, report=synth_report()),
                "cosim": BackendResult(
                    True,
                    "pass",
                    0,
                    0.3,
                    cosim={"status": "Pass", "latency": {"min": 10, "average": 10, "max": 10}},
                ),
            }
        )
        run_dir = self.root / "run"

        first = run_v0(task, run_dir, self.config(task), backend=backend)
        ledger_after_first = (run_dir / "budget_ledger.jsonl").read_bytes()
        second = run_v0(task, run_dir, self.config(task), backend=backend)

        self.assertEqual(first["status"], "DONE")
        self.assertEqual(second["status"], "DONE")
        self.assertEqual(second["cache_hits"], 3)
        self.assertEqual(backend.calls, ["csim", "synth", "cosim"])
        self.assertEqual((run_dir / "budget_ledger.jsonl").read_bytes(), ledger_after_first)
        self.assertEqual(second["budget"]["credits_used"], 25)
        self.assertTrue(second["baseline_unchanged"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in task_dir.iterdir() if path.is_file()},
            original,
        )

        baseline = run_dir / "baseline" / "source" / "kernel.cpp"
        self.assertEqual(baseline.read_bytes(), task.kernel_bytes)
        self.assertEqual(baseline.stat().st_mode & stat.S_IWUSR, 0)
        registry = json.loads((run_dir / "candidate_registry.json").read_text(encoding="utf-8"))
        candidate = registry["candidates"]["candidate_000"]
        self.assertTrue(candidate["immutable"])
        self.assertEqual(candidate["code_hash"], task.kernel_sha256)
        self.assertEqual(candidate["validation"]["cosim"]["status"], "PASS")
        self.assertEqual(registry["best_candidate_id"], "candidate_000")
        self.assertEqual(registry["final_candidate_id"], "candidate_000")

    def test_csim_failure_is_structured_and_gates_later_tools(self) -> None:
        task_dir = self.root / "projection_bugfix"
        task_dir.mkdir()
        make_task(task_dir, task_id="projection_bugfix")
        task = load_public_task(task_dir)
        backend = ScenarioBackend(
            {"csim": BackendResult(False, "runtime_fail", 1, 0.1, ["public mismatch"])}
        )
        run_dir = self.root / "run-projection"

        result = run_v0(task, run_dir, self.config(task), backend=backend)

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "CSIM_RUNTIME_FAIL")
        self.assertEqual(backend.calls, ["csim"])
        registry = json.loads((run_dir / "candidate_registry.json").read_text(encoding="utf-8"))
        validation = registry["candidates"]["candidate_000"]["validation"]
        self.assertEqual(validation["csim"]["status"], "FAIL")
        self.assertEqual(validation["csim"]["phase"], "runtime_fail")
        self.assertEqual(validation["synth"]["status"], "NOT_RUN")
        self.assertEqual(validation["cosim"]["status"], "NOT_RUN")
        self.assertEqual(result["budget"]["credits_used"], 1)

    def test_structural_task_records_cosim_timeout_and_terminates(self) -> None:
        task_dir = self.root / "residual_stream_deadlock"
        task_dir.mkdir()
        make_task(task_dir, task_id="residual_stream_deadlock", requires_cosim=True)
        task = load_public_task(task_dir)
        backend = ScenarioBackend(
            {
                "csim": BackendResult(True, "pass", 0, 0.1),
                "synth": BackendResult(True, "pass", 0, 0.2, report=synth_report()),
                "cosim": BackendResult(False, "timeout", -1, 30.0, ["bounded timeout"]),
            }
        )
        run_dir = self.root / "run-residual"

        result = run_v0(task, run_dir, self.config(task), backend=backend)

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "COSIM_TIMEOUT")
        self.assertEqual(backend.calls, ["csim", "synth", "cosim"])
        self.assertEqual(result["validation"]["csim"]["status"], "PASS")
        self.assertEqual(result["validation"]["cosim"]["status"], "TIMEOUT")
        self.assertEqual(result["budget"]["credits_used"], 25)

    def test_budget_exhaustion_is_a_structured_terminal_result(self) -> None:
        task_dir = self.root / "budgeted"
        task_dir.mkdir()
        make_task(task_dir, task_id="budgeted")
        task = load_public_task(task_dir)
        backend = ScenarioBackend({})
        base = self.config(task)
        config = RunConfig(
            tool=base.tool,
            budget=BudgetConfig(
                credit_limit=0,
                costs=base.budget.costs,
                tool_limits=base.budget.tool_limits,
                token_limit=base.budget.token_limit,
                runtime_limit_seconds=base.budget.runtime_limit_seconds,
            ),
            minimum_frequency_mhz=base.minimum_frequency_mhz,
        )
        run_dir = self.root / "run-budgeted"

        result = run_v0(task, run_dir, config, backend=backend)

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "CSIM_BUDGET_EXCEEDED")
        self.assertEqual(result["validation"]["csim"]["status"], "BUDGET_EXCEEDED")
        self.assertEqual(backend.calls, [])
        self.assertTrue((run_dir / "workflow_result.json").is_file())
        trace = [
            json.loads(line)["event"]
            for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(trace[-1], "RUN_COMPLETED")

    def test_interrupted_action_recovers_as_stable_structured_ambiguity(self) -> None:
        task_dir = self.root / "interrupted"
        task_dir.mkdir()
        make_task(task_dir, task_id="interrupted")
        task = load_public_task(task_dir)
        backend = InterruptingBackend()
        run_dir = self.root / "run-interrupted"

        with self.assertRaises(KeyboardInterrupt):
            run_v0(task, run_dir, self.config(task), backend=backend)
        second = run_v0(task, run_dir, self.config(task), backend=backend)
        ledger_after_second = (run_dir / "budget_ledger.jsonl").read_bytes()
        third = run_v0(task, run_dir, self.config(task), backend=backend)

        for result in (second, third):
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["stop_reason"], "CSIM_AMBIGUOUS_ACTION")
            self.assertEqual(result["validation"]["csim"]["status"], "AMBIGUOUS")
            self.assertEqual(result["budget"]["credits_used"], 1)
        self.assertEqual(backend.calls, ["csim"])
        self.assertEqual(
            (run_dir / "budget_ledger.jsonl").read_bytes(), ledger_after_second
        )

    def test_non_finite_clock_configuration_is_rejected(self) -> None:
        task_dir = self.root / "finite"
        task_dir.mkdir()
        make_task(task_dir, task_id="finite")
        task = load_public_task(task_dir)
        base = self.config(task)
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunConfig(
                    tool=base.tool,
                    budget=base.budget,
                    minimum_frequency_mhz=value,
                )

    def test_non_finite_synth_metric_never_reaches_cosim_or_done(self) -> None:
        task_dir = self.root / "invalid-metric"
        task_dir.mkdir()
        make_task(task_dir, task_id="invalid-metric")
        task = load_public_task(task_dir)
        report = synth_report()
        report["estimated_clock_period_ns"] = math.nan
        backend = ScenarioBackend(
            {
                "csim": BackendResult(True, "pass", 0, 0.1),
                "synth": BackendResult(True, "pass", 0, 0.1, report=report),
            }
        )

        result = run_v0(
            task,
            self.root / "run-invalid-metric",
            self.config(task),
            backend=backend,
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertNotEqual(result["stop_reason"], "BASELINE_VERIFIED")
        self.assertEqual(backend.calls, ["csim", "synth"])
        self.assertEqual(result["validation"]["cosim"]["status"], "NOT_RUN")

    def test_existing_baseline_snapshot_is_made_read_only_again(self) -> None:
        task_dir = self.root / "permissions"
        task_dir.mkdir()
        make_task(task_dir, task_id="permissions")
        task = load_public_task(task_dir)
        backend = ScenarioBackend(
            {
                "csim": BackendResult(False, "runtime_fail", 1, 0.1),
            }
        )
        run_dir = self.root / "run-permissions"
        run_v0(task, run_dir, self.config(task), backend=backend)
        baseline = run_dir / "baseline" / "source" / "kernel.cpp"
        baseline.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertNotEqual(baseline.stat().st_mode & stat.S_IWUSR, 0)

        run_v0(task, run_dir, self.config(task), backend=backend)

        self.assertEqual(baseline.stat().st_mode & stat.S_IWUSR, 0)

    def test_bad_ledger_does_not_erase_previously_verified_registry(self) -> None:
        task_dir = self.root / "registry-preservation"
        task_dir.mkdir()
        make_task(task_dir, task_id="registry-preservation")
        task = load_public_task(task_dir)
        backend = ScenarioBackend(
            {
                "csim": BackendResult(True, "pass", 0, 0.1),
                "synth": BackendResult(True, "pass", 0, 0.1, report=synth_report()),
                "cosim": BackendResult(
                    True,
                    "pass",
                    0,
                    0.1,
                    cosim={"status": "Pass"},
                ),
            }
        )
        run_dir = self.root / "run-registry-preservation"
        result = run_v0(task, run_dir, self.config(task), backend=backend)
        self.assertEqual(result["status"], "DONE")
        registry_before = (run_dir / "candidate_registry.json").read_bytes()
        with (run_dir / "budget_ledger.jsonl").open("ab") as stream:
            stream.write(b"not-json\n")

        with self.assertRaises(BudgetLedgerError):
            run_v0(task, run_dir, self.config(task), backend=backend)

        self.assertEqual(
            (run_dir / "candidate_registry.json").read_bytes(), registry_before
        )

    def test_interruption_during_rerun_preserves_verified_registry(self) -> None:
        task_dir = self.root / "rerun-preservation"
        task_dir.mkdir()
        make_task(task_dir, task_id="rerun-preservation")
        task = load_public_task(task_dir)
        passing = ScenarioBackend(
            {
                "csim": BackendResult(True, "pass", 0, 0.1),
                "synth": BackendResult(True, "pass", 0, 0.1, report=synth_report()),
                "cosim": BackendResult(True, "pass", 0, 0.1, cosim={"status": "Pass"}),
            }
        )
        run_dir = self.root / "run-rerun-preservation"
        self.assertEqual(
            run_v0(task, run_dir, self.config(task), backend=passing)["status"],
            "DONE",
        )
        registry_before = (run_dir / "candidate_registry.json").read_bytes()

        with self.assertRaises(KeyboardInterrupt):
            run_v0(
                task,
                run_dir,
                self.config(task),
                backend=InterruptingBackend(),
            )

        self.assertEqual(
            (run_dir / "candidate_registry.json").read_bytes(), registry_before
        )


if __name__ == "__main__":
    unittest.main()
