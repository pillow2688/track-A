from __future__ import annotations

import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.optimization import (
    ALLOWED_OPTIMIZATIONS,
    OptimizationConfig,
    OptimizationContext,
    run_v2,
    select_optimization,
)
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.scoring import ScoringConfig
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.workflow import RunConfig


class OptimizationSelectorTests(unittest.TestCase):
    @staticmethod
    def metrics(
        *, interval: int, latency: int, evidence: list[str] | None = None
    ) -> dict[str, object]:
        return {
            "latency": {"best": latency, "average": latency, "worst": latency},
            "interval": {"min": interval, "max": interval},
            "evidence": evidence or [],
        }

    def test_high_interval_selects_pipeline_even_with_conservative_pragma(self) -> None:
        source = (
            "for (int i = 0; i < 256; ++i) {\n"
            "#pragma HLS PIPELINE II=16\n"
            "c[i] = a[i] + b[i];\n}"
        )

        decision = select_optimization(
            self.metrics(interval=16, latency=4098),
            source=source,
            attempted=(),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_PIPELINE")
        self.assertEqual(decision.stop_reason, None)
        self.assertTrue(decision.metrics_digest)

    def test_high_latency_after_pipeline_selects_unroll(self) -> None:
        source = (
            "for (int i = 0; i < 256; ++i) {\n"
            "#pragma HLS PIPELINE II=1\n"
            "c[i] = a[i] + b[i];\n}"
        )

        decision = select_optimization(
            self.metrics(interval=1, latency=260),
            source=source,
            attempted=("LOOP_PIPELINE",),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_UNROLL")

    def test_memory_port_evidence_selects_memory_layout(self) -> None:
        decision = select_optimization(
            self.metrics(
                interval=4,
                latency=300,
                evidence=["Unable to schedule load operation due to limited memory ports"],
            ),
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=("LOOP_PIPELINE", "LOOP_UNROLL"),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "MEMORY_LAYOUT")

    def test_no_new_evidence_does_not_repeat_failed_class(self) -> None:
        metrics = self.metrics(interval=16, latency=4098)
        first = select_optimization(
            metrics,
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=(),
            failures=(),
        )
        second = select_optimization(
            metrics,
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=("LOOP_PIPELINE",),
            failures=(("LOOP_PIPELINE", first.metrics_digest),),
        )

        self.assertNotEqual(second.optimization_class, "LOOP_PIPELINE")

    def test_exhausted_whitelist_stops_explicitly(self) -> None:
        decision = select_optimization(
            self.metrics(interval=1, latency=1),
            source="for (int i = 0; i < 1; ++i) {}",
            attempted=ALLOWED_OPTIMIZATIONS,
            failures=(),
        )

        self.assertIsNone(decision.optimization_class)
        self.assertEqual(decision.stop_reason, "NO_DISTINCT_OPTIMIZATION")


def make_workflow_task(root: Path) -> None:
    root.mkdir()
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "v2_workflow_fixture"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 160",
                "requires_cosim = true",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 10.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text(
        '#include "kernel.h"\nvoid kernel(int *out) {\n    int factor = 0;\n    *out = factor;\n}\n',
        encoding="utf-8",
    )
    (root / "kernel.h").write_text("void kernel(int *out);\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class SequenceOptimizationProvider:
    def fingerprint(self) -> str:
        return "sequence-optimization-provider-v1"

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal:
        replacements = {1: (0, 1), 2: (1, 2), 3: (2, 3), 4: (2, 4)}
        old, new = replacements[context.round_index]
        patch = (
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,5 +1,5 @@\n"
            ' #include "kernel.h"\n'
            " void kernel(int *out) {\n"
            f"-    int factor = {old};\n"
            f"+    int factor = {new};\n"
            "     *out = factor;\n"
            " }\n"
        )
        return PatchProposal(
            patch=patch,
            provider="openai-compatible",
            model="deepseek-v4-pro",
            input_tokens=100 + context.round_index,
            output_tokens=20,
            cached_input_tokens=10,
            hypothesis="test optimization proposal",
            change_class=context.allowed_optimization_class,
            expected_effect="improve the configured fake PPA",
            risk="low",
            required_validation=("csim", "synth", "cosim"),
        )


class PPASequenceBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    @staticmethod
    def factor(kernel_bytes: bytes) -> int:
        match = re.search(rb"int factor = (\d+);", kernel_bytes)
        if match is None:
            raise AssertionError("factor marker is missing")
        return int(match.group(1))

    def run(self, kind: str, *, kernel_bytes: bytes, **_kwargs: object) -> BackendResult:
        factor = self.factor(kernel_bytes)
        self.calls.append((kind, factor))
        if factor == 3 and kind == "csim":
            return BackendResult(False, "runtime_fail", 1, 0.1, ["semantic regression"])
        if kind == "synth":
            latency, interval = {
                0: (1000, 16),
                1: (400, 4),
                2: (100, 1),
                4: (200, 2),
            }[factor]
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                report={
                    "estimated_clock_period_ns": 5.0,
                    "latency": {"best": latency, "average": latency, "worst": latency},
                    "interval": {"min": interval, "max": interval},
                    "resources": {
                        "LUT": 100 + factor,
                        "FF": 200 + factor,
                        "DSP": 0,
                        "BRAM_18K": 0,
                        "URAM": 0,
                    },
                    "available_resources": {
                        "LUT": 1000,
                        "FF": 2000,
                        "DSP": 100,
                        "BRAM_18K": 100,
                        "URAM": 50,
                    },
                },
            )
        if kind == "cosim":
            return BackendResult(True, "pass", 0, 0.1, cosim={"status": "Pass"})
        return BackendResult(True, "pass", 0, 0.1)


class V2WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_workflow_task(task_dir)
        self.task = load_public_task(task_dir)
        self.run_root = self.root / "run"
        self.backend = PPASequenceBackend()
        self.run_config = RunConfig(
            tool=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=self.task.part,
                clock_ns=10.0,
                timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
            ),
            budget=BudgetConfig(
                credit_limit=160,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 6, "synth": 6, "cosim": 6, "llm": 6},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
            minimum_frequency_mhz=100.0,
        )
        scoring = ScoringConfig(
            weights={
                "latency": 0.45,
                "ii": 0.35,
                "lut": 0.06,
                "ff": 0.04,
                "dsp": 0.04,
                "bram": 0.04,
                "uram": 0.02,
            },
            max_resource_percent={
                "lut": 100.0,
                "ff": 100.0,
                "dsp": 100.0,
                "bram": 100.0,
                "uram": 100.0,
            },
        )
        self.optimization_config = OptimizationConfig(scoring=scoring)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_four_round_loop_preserves_best_and_finally_revalidates(self) -> None:
        result = run_v2(
            self.task,
            self.run_root,
            self.run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=self.backend,
        )
        registry = json.loads(
            (self.run_root / "candidate_registry.json").read_text(encoding="utf-8")
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["stop_reason"], "NO_IMPROVEMENT_LIMIT")
        self.assertEqual(registry["best_candidate_id"], "candidate_002")
        self.assertEqual(registry["final_candidate_id"], "candidate_002")
        self.assertEqual(
            registry["candidates"]["candidate_003"]["status"],
            "REJECTED_VALIDATION",
        )
        self.assertEqual(
            registry["candidates"]["candidate_004"]["status"],
            "REJECTED_NOT_BETTER",
        )
        self.assertEqual(
            registry["candidates"]["candidate_003"]["parent_id"],
            "candidate_002",
        )
        self.assertEqual(len(result["rounds"]), 4)
        for stage in ("csim", "synth", "cosim"):
            ref = result["final_validation"][stage]["result_ref"]
            action = json.loads((self.run_root / ref).read_text(encoding="utf-8"))
            self.assertEqual(action["validation_scope"], "final")

    def test_exploration_stops_before_spending_final_reserve(self) -> None:
        limited = replace(
            self.run_config,
            budget=BudgetConfig(
                credit_limit=50,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 2, "synth": 2, "cosim": 2, "llm": 1},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )

        result = run_v2(
            self.task,
            self.root / "reserve-run",
            limited,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=PPASequenceBackend(),
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(
            result["exploration_stop_reason"], "FINAL_RESERVE_REACHED"
        )
        self.assertEqual(result["rounds"], [])
        self.assertEqual(result["budget"]["credits_used"], 50)

    def test_completed_run_is_reused_without_duplicate_charges(self) -> None:
        first = run_v2(
            self.task,
            self.run_root,
            self.run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=self.backend,
        )
        ledger_before = (self.run_root / "budget_ledger.jsonl").read_bytes()

        second = run_v2(
            self.task,
            self.run_root,
            self.run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=self.backend,
        )

        self.assertEqual(second, first)
        self.assertEqual(
            (self.run_root / "budget_ledger.jsonl").read_bytes(), ledger_before
        )


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
