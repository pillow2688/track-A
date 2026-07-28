from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.validation import (
    complete_candidate_cosim,
    validate_candidate,
)
from llm4hls_agent.workflow import RunConfig


def make_task(root: Path) -> None:
    root.mkdir()
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "validation_fixture"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 80",
                "requires_cosim = true",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 10.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text(
        '#include "kernel.h"\nvoid kernel(int *out) { *out = 1; }\n',
        encoding="utf-8",
    )
    (root / "kernel.h").write_text("void kernel(int *out);\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class PassingBackend:
    def run(self, kind: str, **_kwargs: object) -> BackendResult:
        if kind == "synth":
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                report={
                    "estimated_clock_period_ns": 4.0,
                    "latency": {"best": 8, "average": 8, "worst": 8},
                    "interval": {"min": 1, "max": 1},
                    "resources": {"LUT": 10},
                    "available_resources": {"LUT": 1000},
                },
            )
        if kind == "cosim":
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                cosim={"status": "Pass"},
            )
        return BackendResult(True, "pass", 0, 0.1)


class CandidateValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_task(task_dir)
        self.task = load_public_task(task_dir)
        self.config = RunConfig(
            tool=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=self.task.part,
                clock_ns=10.0,
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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requires_cosim_runs_all_stages_and_returns_metrics(self) -> None:
        result = validate_candidate(
            self.task,
            self.task.kernel_bytes,
            "candidate_001",
            self.root / "run",
            self.config,
            backend=PassingBackend(),
            validation_scope="exploration",
        )

        self.assertEqual(result.status, "DONE")
        self.assertEqual(result.validation["cosim"]["status"], "PASS")
        self.assertIsNotNone(result.metrics_ref)

    def test_search_closeout_scope_is_propagated_to_every_stage(self) -> None:
        result = validate_candidate(
            self.task,
            self.task.kernel_bytes,
            "candidate_001",
            self.root / "final-run",
            self.config,
            backend=PassingBackend(),
            validation_scope="search_closeout",
        )

        self.assertEqual(
            [
                result.validation[stage]["validation_scope"]
                for stage in ("csim", "synth", "cosim")
            ],
            ["search_closeout", "search_closeout", "search_closeout"],
        )

    def test_exploration_can_stop_after_synth_then_complete_cosim(self) -> None:
        run_root = self.root / "gated-run"

        preliminary = validate_candidate(
            self.task,
            self.task.kernel_bytes,
            "candidate_001",
            run_root,
            self.config,
            backend=PassingBackend(),
            validation_scope="exploration",
            run_cosim=False,
        )

        self.assertEqual(preliminary.status, "DONE")
        self.assertEqual(preliminary.stop_reason, "CANDIDATE_SYNTH_VERIFIED")
        self.assertEqual(preliminary.validation["csim"]["status"], "PASS")
        self.assertEqual(preliminary.validation["synth"]["status"], "PASS")
        self.assertEqual(preliminary.validation["cosim"]["status"], "NOT_RUN")
        self.assertEqual(preliminary.budget["tool_used"]["cosim"], 0)

        completed = complete_candidate_cosim(
            self.task,
            self.task.kernel_bytes,
            "candidate_001",
            run_root,
            self.config,
            preliminary,
            backend=PassingBackend(),
            validation_scope="exploration",
        )

        self.assertEqual(completed.status, "DONE")
        self.assertEqual(completed.stop_reason, "CANDIDATE_VERIFIED")
        self.assertEqual(completed.validation["cosim"]["status"], "PASS")
        self.assertEqual(completed.budget["tool_used"]["cosim"], 1)

    def test_unsupported_scope_is_rejected_before_charging(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope"):
            validate_candidate(
                self.task,
                self.task.kernel_bytes,
                "candidate_001",
                self.root / "invalid-run",
                self.config,
                backend=PassingBackend(),
                validation_scope="other",
            )


if __name__ == "__main__":
    unittest.main()
