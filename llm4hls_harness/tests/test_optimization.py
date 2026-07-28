from __future__ import annotations

import json
import hashlib
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent.artifacts import verify_artifact_manifest
from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.optimization import (
    ALLOWED_OPTIMIZATIONS,
    OptimizationConfig,
    OptimizationContext,
    describe_optimization_selection,
    evaluate_exploration_cosim_gate,
    run_v2,
    run_v2_rejection,
    select_optimization,
)
from llm4hls_agent.repair import PatchProposal, RepairProviderError
from llm4hls_agent.scoring import CandidateScore, ScoringConfig
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

    def test_top_interval_is_not_treated_as_loop_ii_when_loop_is_already_ii_one(self) -> None:
        metrics = self.metrics(interval=1025, latency=1027)
        metrics["loop_evidence"] = {
            "loops": [
                {
                    "name": "VITIS_LOOP_7_1",
                    "trip_count": 1024,
                    "latency_cycles": 1025,
                    "pipeline_ii": 1,
                }
            ]
        }

        decision = select_optimization(
            metrics,
            source="for (int i = 0; i < 1024; ++i) sum += a[i] * b[i];",
            attempted=(),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_UNROLL")
        self.assertNotEqual(decision.optimization_class, "LOOP_PIPELINE")
        self.assertIn("already PipelineII=1", decision.bottleneck)

    def test_reported_loop_ii_above_one_selects_pipeline(self) -> None:
        metrics = self.metrics(interval=512, latency=513)
        metrics["loop_evidence"] = {
            "loops": [
                {
                    "name": "VITIS_LOOP_9_1",
                    "trip_count": 256,
                    "latency_cycles": 513,
                    "pipeline_ii": 2,
                }
            ]
        }

        decision = select_optimization(
            metrics,
            source="for (int i = 0; i < 256; ++i) c[i] = a[i] + b[i];",
            attempted=(),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_PIPELINE")
        self.assertIn("reported PipelineII 2", decision.bottleneck)

    def test_inner_loop_ii_bottleneck_precedes_outer_loop_unroll(self) -> None:
        metrics = self.metrics(interval=1000, latency=10000)
        metrics["loop_evidence"] = {
            "loops": [
                {
                    "name": "outer_loop",
                    "trip_count": 1000,
                    "latency_cycles": 10000,
                    "pipeline_ii": 1,
                },
                {
                    "name": "inner_loop",
                    "trip_count": 25,
                    "latency_cycles": 100,
                    "pipeline_ii": 4,
                },
            ]
        }

        decision = select_optimization(
            metrics,
            source="nested loops",
            attempted=(),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_PIPELINE")
        self.assertIn("inner_loop", decision.bottleneck)
        self.assertIn("reported PipelineII 4", decision.bottleneck)

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

    def test_improved_metrics_can_retry_an_optimization_class(self) -> None:
        previous = self.metrics(interval=16, latency=4098)
        first = select_optimization(
            previous,
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=(),
            failures=(),
        )

        second = select_optimization(
            self.metrics(interval=8, latency=2049),
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=(("LOOP_PIPELINE", first.metrics_digest),),
            failures=(),
        )

        self.assertEqual(second.optimization_class, "LOOP_PIPELINE")

    def test_exhausted_whitelist_stops_explicitly(self) -> None:
        decision = select_optimization(
            self.metrics(interval=1, latency=1),
            source="for (int i = 0; i < 1; ++i) {}",
            attempted=ALLOWED_OPTIMIZATIONS,
            failures=(),
        )

        self.assertIsNone(decision.optimization_class)
        self.assertEqual(decision.stop_reason, "NO_DISTINCT_OPTIMIZATION")

    def test_official_score_tie_uses_internal_ppa_for_cosim_gate(self) -> None:
        common = {
            "verification_tier": 3,
            "hard_constraints_passed": True,
            "hard_failures": (),
            "components": {},
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "credits_used": 5,
            "official_score": 3.0,
            "official_score_source": "PUBLIC_VALIDATION_PROXY_V1",
        }
        incumbent = CandidateScore(
            candidate_id="candidate_001", ppa_cost=0.5, **common
        )
        candidate = CandidateScore(
            candidate_id="candidate_002", ppa_cost=0.4, **common
        )

        gate = evaluate_exploration_cosim_gate(
            candidate,
            incumbent,
            policy="official_score_gate",
        )

        self.assertTrue(gate.eligible)
        self.assertEqual(gate.reason, "OFFICIAL_SCORE_TIE_PPA_IMPROVEMENT")

    def test_selection_context_explains_same_metrics_branch_set(self) -> None:
        metrics = self.metrics(interval=16, latency=4098)
        digest = select_optimization(
            metrics,
            source="for (;;) {}",
            attempted=(),
            failures=(),
        ).metrics_digest

        context = describe_optimization_selection(
            metrics,
            attempted=(("LOOP_PIPELINE", digest),),
            failures=(("MEMORY_LAYOUT", digest),),
        )

        self.assertEqual(context.metrics_digest, digest)
        self.assertEqual(context.attempted_same_metrics, ("LOOP_PIPELINE",))
        self.assertEqual(context.failed_same_metrics, ("MEMORY_LAYOUT",))
        self.assertEqual(
            context.available_classes,
            ("LOOP_UNROLL", "LOOP_RESTRUCTURE"),
        )


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

    def describe_optimization_request(
        self, context: OptimizationContext
    ) -> dict[str, object]:
        return {
            "provider": "openai-compatible",
            "model": "deepseek-v4-pro",
            "http_body": {"prompt": context.to_dict(), "max_tokens": 1000},
        }

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


class ExcerptFailureProvider:
    def fingerprint(self) -> str:
        return "excerpt-failure-provider-v1"

    def describe_optimization_request(
        self, context: OptimizationContext
    ) -> dict[str, object]:
        return {
            "provider": "openai-compatible",
            "model": "deepseek-v4-pro",
            "http_body": {"prompt": context.to_dict(), "max_tokens": 1000},
        }

    def propose_optimization(self, _context: OptimizationContext) -> PatchProposal:
        raise RepairProviderError(
            "provider response field required_validation has the wrong type",
            input_tokens=101,
            output_tokens=23,
            cached_input_tokens=7,
            duration_seconds=0.5,
            request_id="request-failed-schema",
            response_excerpt='{"required_validation":{"csim":true}}',
        )


class RejectOnceThenSucceedProvider(SequenceOptimizationProvider):
    def __init__(self) -> None:
        self.calls = 0

    def fingerprint(self) -> str:
        return "reject-once-then-succeed-provider-v1"

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal:
        self.calls += 1
        if self.calls == 1:
            raise RepairProviderError("injected first-round provider rejection")
        return super().propose_optimization(replace(context, round_index=1))


class InvalidPatchOptimizationProvider(SequenceOptimizationProvider):
    def fingerprint(self) -> str:
        return "invalid-patch-optimization-provider-v1"

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal:
        proposal = super().propose_optimization(context)
        return replace(
            proposal,
            patch=proposal.patch.replace("kernel.cpp", "kernel_tb.cpp"),
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


class BaselineCsimFailureBackend(PPASequenceBackend):
    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object) -> BackendResult:
        if kind == "csim":
            self.calls.append((kind, self.factor(kernel_bytes)))
            return BackendResult(
                False,
                "runtime_fail",
                1,
                0.1,
                ["injected baseline public mismatch"],
            )
        return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)


class OfficialScoreConflictBackend(PPASequenceBackend):
    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object) -> BackendResult:
        factor = self.factor(kernel_bytes)
        if kind != "synth":
            return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)
        self.calls.append((kind, factor))
        latency, interval, lut = {
            0: (1000, 1, 100),
            1: (400, 1, 100),
            2: (399, 16, 900),
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
                    "LUT": lut,
                    "FF": 200,
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


class RejectPromoteRetryBackend(PPASequenceBackend):
    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object) -> BackendResult:
        factor = self.factor(kernel_bytes)
        if kind != "synth":
            return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)
        self.calls.append((kind, factor))
        latency, interval = {
            0: (1000, 16),
            1: (1200, 16),
            2: (400, 16),
            4: (200, 4),
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
                    "LUT": 100,
                    "FF": 200,
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


class RejectPromoteRetryProvider(SequenceOptimizationProvider):
    def fingerprint(self) -> str:
        return "reject-promote-retry-provider-v1"

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal:
        old, new = {1: (0, 1), 2: (0, 2), 3: (2, 4)}[context.round_index]
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
            hypothesis="exercise reject, promote, and retry recovery",
            change_class=context.allowed_optimization_class,
            expected_effect="improve the configured fake PPA",
            risk="low",
            required_validation=("csim", "synth", "cosim"),
        )


class FinalBestFailureBackend(PPASequenceBackend):
    def __init__(self) -> None:
        super().__init__()
        self.factor_two_cosim_calls = 0

    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object) -> BackendResult:
        factor = self.factor(kernel_bytes)
        if kind == "cosim" and factor == 2:
            self.factor_two_cosim_calls += 1
            if self.factor_two_cosim_calls == 2:
                self.calls.append((kind, factor))
                return BackendResult(
                    False,
                    "cosim_fail",
                    1,
                    0.1,
                    ["final-only injected CoSim failure"],
                )
        return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)


class EveryFinalCosimFailureBackend(PPASequenceBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cosim_calls: dict[int, int] = {}

    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object) -> BackendResult:
        factor = self.factor(kernel_bytes)
        if kind == "cosim":
            count = self.cosim_calls.get(factor, 0) + 1
            self.cosim_calls[factor] = count
            if count >= 2:
                self.calls.append((kind, factor))
                return BackendResult(
                    False,
                    "cosim_fail",
                    1,
                    0.1,
                    ["injected final CoSim failure"],
                )
        return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)


class VectorAddRegressionBackend:
    def run(self, kind: str, *, kernel_bytes: bytes, **_kwargs: object) -> BackendResult:
        if kind == "csim" and b"a[i] - b[i]" in kernel_bytes:
            return BackendResult(
                False,
                "runtime_fail",
                1,
                0.1,
                ["case 0 mismatch at index 1"],
            )
        if kind == "synth":
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                report={
                    "estimated_clock_period_ns": 5.0,
                    "latency": {"best": 4098, "average": 4098, "worst": 4098},
                    "interval": {"min": 16, "max": 16},
                    "resources": {
                        "LUT": 100,
                        "FF": 200,
                        "DSP": 0,
                        "BRAM_18K": 0,
                        "URAM": 0,
                    },
                    "available_resources": {
                        "LUT": 100000,
                        "FF": 200000,
                        "DSP": 1000,
                        "BRAM_18K": 1000,
                        "URAM": 100,
                    },
                },
            )
        if kind == "cosim":
            return BackendResult(True, "pass", 0, 0.1, cosim={"status": "Pass"})
        return BackendResult(True, "pass", 0, 0.1)


class V2SafetyRejectionTests(unittest.TestCase):
    def test_policy_valid_semantic_regression_is_rejected_without_polluting_best(self) -> None:
        examples = Path(__file__).parents[1] / "examples"
        task = load_public_task(examples / "u55c_v2_optimize_task")
        patch_path = examples / "u55c_v2_regression.diff"
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "rejection"
            config = RunConfig(
                tool=ToolConfig(
                    vitis_root="/opt/xilinx/2025.2/Vitis",
                    part=task.part,
                    clock_ns=task.clock_ns,
                    timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
                ),
                budget=BudgetConfig(
                    credit_limit=160,
                    costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                    tool_limits={"csim": 3, "synth": 2, "cosim": 2, "llm": 0},
                    token_limit=0,
                    runtime_limit_seconds=3600.0,
                ),
                minimum_frequency_mhz=100.0,
            )

            result = run_v2_rejection(
                task,
                run_root,
                config,
                patch_path,
                backend=VectorAddRegressionBackend(),
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                run_v2_rejection(
                    task,
                    run_root,
                    replace(config, tool=replace(config.tool, clock_ns=5.0)),
                    patch_path,
                    backend=VectorAddRegressionBackend(),
                )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["stop_reason"], "SAFETY_REGRESSION_REJECTED")
        self.assertEqual(registry["best_candidate_id"], "candidate_000")
        self.assertEqual(registry["final_candidate_id"], "candidate_000")
        rejected = registry["candidates"]["candidate_001"]
        self.assertEqual(rejected["kind"], "safety_regression")
        self.assertEqual(rejected["parent_id"], "candidate_000")
        self.assertEqual(rejected["status"], "REJECTED_VALIDATION")
        self.assertEqual(rejected["validation"]["csim"]["status"], "FAIL")
        self.assertEqual(rejected["validation"]["synth"]["status"], "NOT_RUN")
        self.assertEqual(rejected["validation"]["cosim"]["status"], "NOT_RUN")
        self.assertEqual(result["budget"]["tool_used"], {
            "cosim": 1,
            "csim": 2,
            "llm": 0,
            "synth": 1,
        })
        self.assertEqual(result["budget"]["credits_used"], 26)


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
            registry["candidates"]["candidate_004"]["validation"]["cosim"]["status"],
            "NOT_RUN",
        )
        self.assertNotIn(("cosim", 4), self.backend.calls)
        self.assertEqual(
            result["budget"]["tool_used"],
            {"cosim": 4, "csim": 6, "llm": 4, "synth": 5},
        )
        self.assertEqual(result["budget"]["credits_used"], 106)
        self.assertEqual(
            registry["candidates"]["candidate_003"]["parent_id"],
            "candidate_002",
        )
        self.assertEqual(len(result["rounds"]), 4)
        self.assertTrue((self.run_root / "experimental_report.md").is_file())
        manifest = verify_artifact_manifest(self.run_root)
        self.assertEqual(manifest["workflow"], "V2_CANDIDATE_PPA")
        for round_record in result["rounds"]:
            self.assertIn("selection_context", round_record)
            self.assertIn("request_ref", round_record)
            request_ref = round_record["request_ref"]
            request = json.loads(
                (self.run_root / request_ref).read_text(encoding="utf-8")
            )
            self.assertEqual(request["sent_files"], ["kernel.cpp"])
            self.assertEqual(
                request["code_ranges"],
                [{"file": "kernel.cpp", "start_line": 1, "end_line": 5}],
            )
            self.assertIn("current_validation", request["context"])
            self.assertIn("current_clock_constraint", request["context"])
            self.assertGreaterEqual(len(request["hls_rules"]), 1)
            self.assertLessEqual(len(request["hls_rules"]), 3)
            sent_payload = json.dumps(
                {
                    "context": request["context"],
                    "provider_request": request["provider_request"],
                },
                sort_keys=True,
            )
            self.assertNotIn("kernel_tb.cpp", sent_payload)
            self.assertNotIn("hidden/", sent_payload)
            self.assertNotIn("reference/", sent_payload)
            self.assertNotIn("/home/", sent_payload)
        for stage in ("csim", "synth", "cosim"):
            ref = result["final_validation"][stage]["result_ref"]
            action = json.loads((self.run_root / ref).read_text(encoding="utf-8"))
            self.assertEqual(action["validation_scope"], "search_closeout")

    def test_default_candidate_and_no_improvement_limits_match_policy(self) -> None:
        self.assertEqual(self.optimization_config.max_rounds, 6)
        self.assertEqual(self.optimization_config.max_no_improvement_rounds, 2)

    def test_baseline_failure_is_a_complete_sealed_v2_terminal_run(self) -> None:
        run_root = self.root / "baseline-failure"

        result = run_v2(
            self.task,
            run_root,
            self.run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=BaselineCsimFailureBackend(),
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "BASELINE_NOT_VERIFIED")
        self.assertEqual(result["task_id"], self.task.id)
        self.assertEqual(result["rounds"], [])
        self.assertEqual(result["budget"]["tool_used"]["csim"], 1)
        self.assertTrue((run_root / "optimization_config.json").is_file())
        self.assertTrue((run_root / "experimental_report.md").is_file())
        manifest = verify_artifact_manifest(run_root)
        covered = {item["path"] for item in manifest["artifacts"]}
        self.assertIn("v2_result.json", covered)
        self.assertIn("experimental_report.md", covered)

    def test_provider_rejection_continues_and_next_round_can_promote(self) -> None:
        provider = RejectOnceThenSucceedProvider()
        config = replace(self.optimization_config, max_rounds=2)

        result = run_v2(
            self.task,
            self.root / "reject-then-promote",
            self.run_config,
            config,
            provider,
            backend=PPASequenceBackend(),
        )

        self.assertEqual(
            [round_record["decision"] for round_record in result["rounds"]],
            ["PROVIDER_REJECTED", "PROMOTED"],
        )
        self.assertEqual(
            [round_record["parent_candidate_id"] for round_record in result["rounds"]],
            ["candidate_000", "candidate_000"],
        )
        self.assertIsNone(result["rounds"][0]["candidate_id"])
        self.assertEqual(result["rounds"][1]["candidate_id"], "candidate_001")
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(result["no_improvement_rounds"], 0)
        self.assertEqual(result["exploration_stop_reason"], "MAX_OPTIMIZATION_ROUNDS")
        self.assertEqual(provider.calls, 2)

    def test_official_score_gate_checks_latency_improvement_before_ppa(self) -> None:
        scoring = replace(
            self.optimization_config.scoring,
            official_score_enabled=True,
        )
        config = replace(
            self.optimization_config,
            scoring=scoring,
            max_rounds=2,
            exploration_cosim_policy="official_score_gate",
        )
        run_root = self.root / "official-score-gate"
        backend = OfficialScoreConflictBackend()

        result = run_v2(
            self.task,
            run_root,
            self.run_config,
            config,
            SequenceOptimizationProvider(),
            backend=backend,
        )

        first = json.loads(
            (run_root / "scores/candidate_001.pre_cosim.json").read_text()
        )
        second = json.loads(
            (run_root / "scores/candidate_002.pre_cosim.json").read_text()
        )
        second_gate = json.loads(
            (run_root / "cosim_gates/candidate_002.json").read_text()
        )
        self.assertGreater(second["ppa_cost"], first["ppa_cost"])
        self.assertGreater(second["official_score"], first["official_score"])
        self.assertTrue(second_gate["eligible"])
        self.assertEqual(second_gate["policy"], "official_score_gate")
        self.assertEqual(
            second_gate["reason"], "STRICT_OFFICIAL_SCORE_IMPROVEMENT"
        )
        self.assertIn(("cosim", 2), backend.calls)
        self.assertEqual(result["best_candidate_id"], "candidate_002")
        self.assertEqual(result["final_candidate_id"], "candidate_002")
        report = (run_root / "experimental_report.md").read_text(encoding="utf-8")
        self.assertIn("official public proxy", report)
        self.assertIn("public_proxy_v1", report)
        self.assertIn("PPA tie-break", report)

    def test_search_closeout_reserve_cannot_be_lower_than_configured_final_tool_cost(self) -> None:
        unsafe = replace(self.optimization_config, search_closeout_reserve_credits=24)

        with self.assertRaisesRegex(ValueError, "search closeout reserve"):
            run_v2(
                self.task,
                self.root / "unsafe-reserve",
                self.run_config,
                unsafe,
                SequenceOptimizationProvider(),
                backend=PPASequenceBackend(),
            )

    def test_exploration_stops_before_spending_search_closeout_reserve(self) -> None:
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
            result["exploration_stop_reason"], "SEARCH_CLOSEOUT_RESERVE_REACHED"
        )
        self.assertEqual(result["rounds"], [])
        self.assertEqual(result["budget"]["credits_used"], 50)

    def test_llm_call_is_denied_before_provider_when_token_reserve_is_too_low(self) -> None:
        limited = replace(
            self.run_config,
            budget=replace(self.run_config.budget, token_limit=1000),
        )
        provider = SequenceOptimizationProvider()

        with patch.object(
            provider,
            "propose_optimization",
            wraps=provider.propose_optimization,
        ) as propose:
            result = run_v2(
                self.task,
                self.root / "token-reserve-run",
                limited,
                self.optimization_config,
                provider,
                backend=PPASequenceBackend(),
            )

        self.assertEqual(propose.call_count, 0)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["exploration_stop_reason"], "TOKEN_RESERVE_REACHED")
        self.assertEqual(result["budget"]["tool_used"]["llm"], 0)
        self.assertEqual(result["rounds"], [])

    def test_provider_failure_persists_response_and_auditable_refs(self) -> None:
        run_root = self.root / "provider-failure"

        result = run_v2(
            self.task,
            run_root,
            self.run_config,
            self.optimization_config,
            ExcerptFailureProvider(),
            backend=PPASequenceBackend(),
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(len(result["rounds"]), 2)
        for round_record in result["rounds"]:
            provider_result = json.loads(
                (run_root / round_record["provider_ref"]).read_text(encoding="utf-8")
            )
            self.assertFalse(provider_result["ok"])
            self.assertEqual(
                provider_result["response_excerpt"],
                '{"required_validation":{"csim":true}}',
            )
            self.assertEqual(provider_result["request_id"], "request-failed-schema")
            self.assertEqual(provider_result["input_tokens"], 101)
            self.assertEqual(provider_result["output_tokens"], 23)
            self.assertTrue((run_root / round_record["request_ref"]).is_file())

    def test_recovery_honors_durable_no_improvement_limit(self) -> None:
        from llm4hls_agent import optimization

        run_root = self.root / "durable-no-improvement"
        original_append_trace = optimization._append_trace

        def interrupt_after_second_round(path, event, **payload):
            original_append_trace(path, event, **payload)
            if event == "V2_ROUND_COMPLETED" and payload.get("round_index") == 2:
                raise KeyboardInterrupt("injected after durable rejection round")

        with patch(
            "llm4hls_agent.optimization._append_trace",
            side_effect=interrupt_after_second_round,
        ), self.assertRaises(KeyboardInterrupt):
            run_v2(
                self.task,
                run_root,
                self.run_config,
                self.optimization_config,
                ExcerptFailureProvider(),
                backend=PPASequenceBackend(),
            )

        result = run_v2(
            self.task,
            run_root,
            self.run_config,
            self.optimization_config,
            ExcerptFailureProvider(),
            backend=PPASequenceBackend(),
        )

        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["exploration_stop_reason"], "NO_IMPROVEMENT_LIMIT")
        self.assertEqual(result["budget"]["tool_used"]["llm"], 2)

    def test_recovery_preserves_not_better_failure_history_after_promotion(self) -> None:
        from llm4hls_agent import optimization

        run_root = self.root / "reject-promote-retry-recovery"
        config = replace(self.optimization_config, max_rounds=3)
        original_append_trace = optimization._append_trace

        def interrupt_after_third_round(path, event, **payload):
            original_append_trace(path, event, **payload)
            if event == "V2_ROUND_COMPLETED" and payload.get("round_index") == 3:
                raise KeyboardInterrupt("injected after retry round")

        with patch(
            "llm4hls_agent.optimization._append_trace",
            side_effect=interrupt_after_third_round,
        ), self.assertRaises(KeyboardInterrupt):
            run_v2(
                self.task,
                run_root,
                self.run_config,
                config,
                RejectPromoteRetryProvider(),
                backend=RejectPromoteRetryBackend(),
            )

        result = run_v2(
            self.task,
            run_root,
            self.run_config,
            config,
            RejectPromoteRetryProvider(),
            backend=RejectPromoteRetryBackend(),
        )
        third_request = json.loads(
            (
                run_root
                / result["rounds"][2]["request_ref"]
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            [record["decision"] for record in result["rounds"]],
            ["REJECTED_NOT_BETTER", "PROMOTED", "PROMOTED"],
        )
        self.assertEqual(result["best_candidate_id"], "candidate_003")
        self.assertEqual(
            third_request["context"]["failed_actions"][0]["optimization_class"],
            "LOOP_PIPELINE",
        )

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

    def test_final_failure_uses_ranked_verified_fallback(self) -> None:
        fallback_run_config = replace(
            self.run_config,
            budget=BudgetConfig(
                credit_limit=200,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 7, "synth": 7, "cosim": 7, "llm": 6},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )

        result = run_v2(
            self.task,
            self.root / "fallback-run",
            fallback_run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=FinalBestFailureBackend(),
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["stop_reason"], "FALLBACK_VERIFIED")
        self.assertEqual(result["best_candidate_id"], "candidate_002")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(
            result["fallback"],
            {
                "from": "candidate_002",
                "to": "candidate_001",
                "reason": "COSIM_COSIM_FAIL",
            },
        )

    def test_final_attempt_limit_prevents_unbounded_fallback(self) -> None:
        limited_attempts = replace(
            self.optimization_config,
            max_final_attempts=1,
        )
        fallback_run_config = replace(
            self.run_config,
            budget=BudgetConfig(
                credit_limit=200,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 7, "synth": 7, "cosim": 7, "llm": 6},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )

        result = run_v2(
            self.task,
            self.root / "fallback-limit-run",
            fallback_run_config,
            limited_attempts,
            SequenceOptimizationProvider(),
            backend=FinalBestFailureBackend(),
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "FINAL_VALIDATION_FAILED")
        self.assertEqual(len(result["final_attempts"]), 1)
        self.assertIsNone(result["final_candidate_id"])

    def test_all_failed_final_attempts_still_report_complete_ledger_budget(self) -> None:
        run_config = replace(
            self.run_config,
            budget=BudgetConfig(
                credit_limit=200,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 7, "synth": 7, "cosim": 7, "llm": 6},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )
        run_root = self.root / "all-final-attempts-fail"

        result = run_v2(
            self.task,
            run_root,
            run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=EveryFinalCosimFailureBackend(),
        )
        budget_state = json.loads(
            (run_root / "budget_state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "FINAL_VALIDATION_FAILED")
        self.assertEqual(len(result["final_attempts"]), 2)
        self.assertEqual(result["budget"]["credits_used"], 131)
        self.assertEqual(
            result["budget"]["credits_used"], budget_state["credits_used"]
        )
        self.assertEqual(result["budget"]["tool_used"], budget_state["tool_used"])

    def test_restart_after_completed_round_does_not_repeat_first_round(self) -> None:
        from llm4hls_agent import optimization

        original_gate = optimization._ensure_round_affordable
        calls = 0

        def interrupt_before_second_round(budget, config):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("injected after durable round one")
            return original_gate(budget, config)

        with patch(
            "llm4hls_agent.optimization._ensure_round_affordable",
            side_effect=interrupt_before_second_round,
        ), self.assertRaises(KeyboardInterrupt):
            run_v2(
                self.task,
                self.run_root,
                self.run_config,
                self.optimization_config,
                SequenceOptimizationProvider(),
                backend=self.backend,
            )
        ledger_after_round_one = json.loads(
            (self.run_root / "budget_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger_after_round_one["tool_used"]["llm"], 1)
        self.assertEqual(ledger_after_round_one["credits_used"], 50)

        result = run_v2(
            self.task,
            self.run_root,
            self.run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=self.backend,
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds"][0]["candidate_id"], "candidate_001")
        self.assertEqual(result["budget"]["tool_used"]["llm"], 4)
        self.assertEqual(result["budget"]["credits_used"], 106)

    def test_recovery_rejects_tampered_durable_round_binding(self) -> None:
        from llm4hls_agent import optimization

        original_gate = optimization._ensure_round_affordable
        calls = 0

        def interrupt_before_second_round(budget, config):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("injected after durable round one")
            return original_gate(budget, config)

        with patch(
            "llm4hls_agent.optimization._ensure_round_affordable",
            side_effect=interrupt_before_second_round,
        ), self.assertRaises(KeyboardInterrupt):
            run_v2(
                self.task,
                self.run_root,
                self.run_config,
                self.optimization_config,
                SequenceOptimizationProvider(),
                backend=self.backend,
            )
        ledger_before = (self.run_root / "budget_ledger.jsonl").read_bytes()
        round_path = self.run_root / "optimization_rounds/round_001.json"
        round_record = json.loads(round_path.read_text(encoding="utf-8"))
        round_record["parent_candidate_id"] = "candidate_999"
        round_path.write_text(json.dumps(round_record), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "durable round binding"):
            run_v2(
                self.task,
                self.run_root,
                self.run_config,
                self.optimization_config,
                SequenceOptimizationProvider(),
                backend=self.backend,
            )

        self.assertEqual(
            (self.run_root / "budget_ledger.jsonl").read_bytes(), ledger_before
        )

    def test_recovery_rejects_tampered_comparison_evidence(self) -> None:
        from llm4hls_agent import optimization

        run_root = self.root / "comparison-recovery"
        original_gate = optimization._ensure_round_affordable
        calls = 0

        def interrupt_before_second_round(budget, config):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("injected after durable round one")
            return original_gate(budget, config)

        with patch(
            "llm4hls_agent.optimization._ensure_round_affordable",
            side_effect=interrupt_before_second_round,
        ), self.assertRaises(KeyboardInterrupt):
            run_v2(
                self.task,
                run_root,
                self.run_config,
                self.optimization_config,
                SequenceOptimizationProvider(),
                backend=PPASequenceBackend(),
            )
        comparison_path = run_root / "comparisons/candidate_001.json"
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        comparison["winner"] = "candidate_000"
        comparison_path.write_text(json.dumps(comparison), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "comparison binding"):
            run_v2(
                self.task,
                run_root,
                self.run_config,
                self.optimization_config,
                SequenceOptimizationProvider(),
                backend=PPASequenceBackend(),
            )

    def test_recovery_reproves_patch_rejected_semantics(self) -> None:
        from llm4hls_agent import optimization

        run_root = self.root / "patch-rejected-recovery"
        provider = InvalidPatchOptimizationProvider()
        original_gate = optimization._ensure_round_affordable
        calls = 0

        def interrupt_before_second_round(budget, config):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("injected after durable round one")
            return original_gate(budget, config)

        with patch(
            "llm4hls_agent.optimization._ensure_round_affordable",
            side_effect=interrupt_before_second_round,
        ), self.assertRaises(KeyboardInterrupt):
            run_v2(
                self.task,
                run_root,
                self.run_config,
                self.optimization_config,
                provider,
                backend=PPASequenceBackend(),
            )
        round_record = json.loads(
            (run_root / "optimization_rounds/round_001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(round_record["decision"], "PATCH_REJECTED")
        result_path = run_root / round_record["provider_ref"]
        provider_result = json.loads(result_path.read_text(encoding="utf-8"))
        provider_result["patch"] = provider_result["patch"].replace(
            "kernel_tb.cpp", "kernel.cpp"
        )
        result_path.write_text(json.dumps(provider_result), encoding="utf-8")
        result_sha256 = hashlib.sha256(result_path.read_bytes()).hexdigest()
        ledger_path = run_root / "budget_ledger.jsonl"
        events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        for event in events:
            if (
                event.get("state") == "COMPLETED"
                and event.get("result_ref") == round_record["provider_ref"]
            ):
                event["result_sha256"] = result_sha256
        ledger_path.write_text(
            "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Patch rejection binding"):
            run_v2(
                self.task,
                run_root,
                self.run_config,
                self.optimization_config,
                provider,
                backend=PPASequenceBackend(),
            )


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
