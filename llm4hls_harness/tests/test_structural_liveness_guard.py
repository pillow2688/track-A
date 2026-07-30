"""Focused contracts for evidence-bound DATAFLOW/FIFO liveness admission."""

from __future__ import annotations

import difflib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.openai_provider import build_task_aware_prompt
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.v3_search_control import (
    continuation_action,
    rtl_liveness_obligation,
    semantic_progress_assessment,
    structural_action_family_frontier,
    structural_liveness_guard,
    structural_facts,
)
from llm4hls_agent.workflow import RunConfig

try:
    from llm4hls_agent.v3_prototype import run_v3_prototype
except ModuleNotFoundError:
    run_v3_prototype = None  # type: ignore[assignment]


BASELINE_CYCLE = """
void seed(hls::stream<int>& feedback) { feedback.write(0); }
void forward(hls::stream<int>& forward_fifo, hls::stream<int>& feedback) {
  forward_fifo.write(feedback.read());
}
void consume(hls::stream<int>& forward_fifo, hls::stream<int>& feedback) {
  feedback.write(forward_fifo.read());
}
void kernel() {
  hls::stream<int> forward_fifo, feedback;
#pragma HLS DATAFLOW
  seed(feedback); forward(forward_fifo, feedback); consume(forward_fifo, feedback);
}
"""

DEPTH_ONLY = BASELINE_CYCLE.replace(
    "hls::stream<int> forward_fifo, feedback;",
    "hls::stream<int> forward_fifo, feedback;\n#pragma HLS STREAM variable=feedback depth=2",
)

UNINITIALIZED_FEEDBACK = """
void forward(hls::stream<int>& forward_fifo, hls::stream<int>& feedback) {
  forward_fifo.write(feedback.read());
}
void consume(hls::stream<int>& forward_fifo, hls::stream<int>& feedback) {
  feedback.write(forward_fifo.read());
}
void kernel() {
  hls::stream<int> forward_fifo, feedback;
#pragma HLS DATAFLOW
  forward(forward_fifo, feedback); consume(forward_fifo, feedback);
}
"""

SPSC = """
void forward(hls::stream<int>& forward_fifo) { forward_fifo.write(1); }
void consume(hls::stream<int>& forward_fifo) { int value = forward_fifo.read(); }
void kernel() {
  hls::stream<int> forward_fifo;
#pragma HLS DATAFLOW
  forward(forward_fifo); consume(forward_fifo);
}
"""

INITIALIZED_FEEDBACK = """
void loop(hls::stream<int>& feedback) {
  feedback.write(0);
  int value = feedback.read();
  feedback.write(value + 1);
}
void kernel() {
  hls::stream<int> feedback;
#pragma HLS DATAFLOW
  loop(feedback);
}
"""


def _obligation() -> dict[str, object]:
    value = rtl_liveness_obligation(
        requires_cosim=True,
        evidence={
            "schema_version": "v3c.cosim-failure-evidence.v1",
            "phase": "timeout",
            "failure_kind": "TIMEOUT",
            "error_summary": "RTL no progress while draining DATAFLOW",
        },
        source=BASELINE_CYCLE,
    )
    assert value is not None
    return value


class StructuralLivenessGuardTests(unittest.TestCase):
    def test_requires_cosim_deadlock_topology_creates_high_priority_obligation(self) -> None:
        obligation = _obligation()
        self.assertEqual(obligation["id"], "RTL_LIVENESS_STREAM_TOPOLOGY")
        self.assertEqual(obligation["priority"], 100)
        self.assertIn("feedback", obligation["implicated_streams"])
        self.assertTrue(obligation["dependency_cycles"])
        self.assertIn("FIFO depth-only changes do not satisfy this obligation.", obligation["planner_requirements"])

    def test_depth_only_candidate_is_rejected_before_expensive_tools(self) -> None:
        guard = structural_liveness_guard(
            parent_source=BASELINE_CYCLE,
            candidate_source=DEPTH_ONLY,
            obligation=_obligation(),
        )
        self.assertFalse(guard["eligible"])
        self.assertEqual(guard["reason"], "STRUCTURAL_OBLIGATION_UNSATISFIED")
        self.assertIn("IMPLICATED_MULTI_PRODUCER_REMAINS", guard["violations"])
        self.assertIn("DEPTH_ONLY_OR_TOPOLOGY_NEUTRAL_PATCH", guard["violations"])

    def test_removing_seed_but_retaining_feedback_cycle_is_rejected(self) -> None:
        guard = structural_liveness_guard(
            parent_source=BASELINE_CYCLE,
            candidate_source=UNINITIALIZED_FEEDBACK,
            obligation=_obligation(),
        )
        self.assertFalse(guard["eligible"])
        self.assertIn("UNINITIALIZED_STREAM_DEPENDENCY_CYCLE_REMAINS", guard["violations"])

    def test_unidirectional_spsc_and_ordinary_acyclic_dataflow_are_allowed(self) -> None:
        guard = structural_liveness_guard(
            parent_source=BASELINE_CYCLE,
            candidate_source=SPSC,
            obligation=_obligation(),
        )
        self.assertTrue(guard["eligible"])
        ordinary = structural_liveness_guard(
            parent_source=SPSC,
            candidate_source=SPSC,
            obligation=None,
        )
        self.assertTrue(ordinary["eligible"])
        self.assertEqual(ordinary["reason"], "NO_RTL_LIVENESS_OBLIGATION")

    def test_explicit_write_before_read_initialization_is_not_blanket_rejected(self) -> None:
        guard = structural_liveness_guard(
            parent_source=BASELINE_CYCLE,
            candidate_source=INITIALIZED_FEEDBACK,
            obligation=_obligation(),
        )
        self.assertTrue(guard["eligible"])
        self.assertTrue(guard["candidate_has_explicit_initial_token"])

    def test_frontier_and_progress_distinguish_new_structural_information(self) -> None:
        frontier = structural_action_family_frontier(
            facts={"structural_facts": structural_facts(BASELINE_CYCLE)},
            history=[
                {
                    "status": "REJECTED",
                    "proposal_experiment": {
                        "action_family": "capacity_adjustment",
                        "input_failure_signature": "a" * 64,
                    },
                }
            ],
        )
        self.assertIn("capacity_adjustment", frontier["attempted_action_families"])
        self.assertIn("topology_elimination", frontier["untried_action_families"])
        progress = semantic_progress_assessment(
            mode="STRUCTURAL_FIX",
            facts={"last_failure_signature": "b" * 64, "terminal_failure": True},
            experiment={
                "action_family": "capacity_adjustment",
                "input_failure_signature": "a" * 64,
                "frontier_was_untried": True,
            },
            frontier=frontier,
        )
        self.assertTrue(progress["is_semantic_progress"])
        self.assertIn("NEW_FAILURE_SIGNATURE", progress["reasons"])
        self.assertIn("ACTION_FAMILY_EXCLUDED_OR_TESTED", progress["reasons"])

    def test_guard_rejection_continues_only_when_an_untried_family_remains(self) -> None:
        facts = {"terminal_failure": True}
        self.assertEqual(
            continuation_action(
                facts=facts,
                attempted=[],
                baseline_id="candidate_000",
                incumbent_id="candidate_000",
                structural_guard_rejected=True,
                action_family_frontier={"has_high_value_untried_family": True},
            ),
            "CONTINUE_WITH_LLM",
        )
        self.assertEqual(
            continuation_action(
                facts=facts,
                attempted=[],
                baseline_id="candidate_000",
                incumbent_id="candidate_000",
                structural_guard_rejected=True,
                action_family_frontier={"has_high_value_untried_family": False},
            ),
            "STOP",
        )

    def test_non_structural_modes_do_not_receive_structural_semantic_progress(self) -> None:
        for mode in ("REPAIR", "SYNTH_FIX", "OPTIMIZE"):
            with self.subTest(mode=mode):
                progress = semantic_progress_assessment(
                    mode=mode,
                    facts={"last_failure_signature": "b" * 64, "terminal_failure": True},
                    experiment={"frontier_was_untried": True},
                )
                self.assertFalse(progress["is_semantic_progress"])

    def test_guard_is_independent_of_a2_a3_switches(self) -> None:
        # The guard takes only public source/evidence obligation inputs.  These
        # four labels represent the A2/A3 2x2 modes and must not affect it.
        expected = None
        for switches in (("off", "off"), ("off", "guided"), ("enforce", "off"), ("enforce", "guided")):
            del switches
            result = structural_liveness_guard(
                parent_source=BASELINE_CYCLE,
                candidate_source=DEPTH_ONLY,
                obligation=_obligation(),
            )
            if expected is None:
                expected = result
            self.assertEqual(result, expected)

    def test_non_structural_prompt_is_unchanged_by_absent_obligation(self) -> None:
        prompt = build_task_aware_prompt(
            {
                "mode": "REPAIR",
                "task": {"task_id": "fixture", "requires_cosim": False},
                "current_kernel": "void kernel() {}\n",
                "description": "fixture",
                "read_only_headers": {},
                "failure_evidence": {"failure_kind": "RUNTIME_FAIL"},
                "budget": {"remaining_tokens": 1000},
                "constraints": {"allowed_files": ["kernel.cpp"]},
                "search_control": {"primary_obligation": "FUNCTIONAL_CORRECTNESS"},
            }
        )
        self.assertNotIn("RTL LIVENESS OBLIGATION (MANDATORY)", prompt)

    @unittest.skipIf(run_v3_prototype is None, "V3 optional dependencies are not installed")
    def test_020_depth_only_candidate_is_materialized_then_rejected_before_candidate_tools(self) -> None:
        class BaselineDeadlockBackend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, bool]] = []

            def fingerprint(self) -> str:
                return "structural-liveness-guard-test-v1"

            def run(self, kind: str, *, kernel_bytes: bytes, **_kwargs: object) -> BackendResult:
                candidate = b"variable=feedback_stream depth=2" in kernel_bytes
                self.calls.append((kind, candidate))
                if kind == "synth":
                    return BackendResult(
                        True,
                        "pass",
                        0,
                        0.01,
                        report={
                            "estimated_clock_period_ns": 5.0,
                            "latency": {"best": 32, "average": 32, "worst": 32},
                            "interval": {"min": 1, "max": 1},
                            "resources": {"LUT": 1, "FF": 1, "DSP": 0, "BRAM_18K": 0, "URAM": 0},
                            "available_resources": {"LUT": 100, "FF": 100, "DSP": 10, "BRAM_18K": 10, "URAM": 10},
                        },
                    )
                if kind == "cosim" and not candidate:
                    return BackendResult(
                        False,
                        "timeout",
                        1,
                        0.01,
                        evidence=["RTL no progress DATAFLOW deadlock"],
                        cosim={"status": "Timeout"},
                    )
                return BackendResult(True, "pass", 0, 0.01, cosim={"status": "Pass"})

        root = Path(__file__).resolve().parents[1]
        task = load_public_task(root / "task_corpus" / "v3d-fast" / "tasks" / "v3d_fast_020")
        source = task.kernel_bytes.decode("utf-8")
        depth_only = source.replace(
            "variable=feedback_stream depth=1",
            "variable=feedback_stream depth=2",
        )
        patch = "".join(
            difflib.unified_diff(
                source.splitlines(True),
                depth_only.splitlines(True),
                fromfile="a/kernel.cpp",
                tofile="b/kernel.cpp",
            )
        )
        config = RunConfig(
            tool=ToolConfig(vitis_root="/tmp", part=task.part, clock_ns=task.clock_ns, timeouts={"csim": 10.0, "synth": 10.0, "cosim": 10.0}),
            budget=BudgetConfig(
                credit_limit=100,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 5, "synth": 5, "cosim": 5, "llm": 2},
                token_limit=1024,
                runtime_limit_seconds=300.0,
            ),
            minimum_frequency_mhz=100.0,
        )
        backend = BaselineDeadlockBackend()
        proposal = PatchProposal(
            patch=patch,
            provider="fixture",
            model="fixture",
            target_obligation="RTL_LIVENESS",
            hypothesis="increase the FIFO depth only",
            change_class="STRUCTURAL_REPAIR",
            risk="high",
        )
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3d-fast-020"
            result = run_v3_prototype(
                task,
                run_root,
                config,
                proposal,
                backend=backend,
                max_planner_rounds=1,
                thread_id="structural-liveness-guard-test",
            )
            registry = json.loads((run_root / "candidate_registry.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (run_root / "evidence" / "failures" / "candidate_001_structural_obligation.json").read_text(encoding="utf-8")
            )

        self.assertEqual(registry["candidates"]["candidate_001"]["rejection_reason"], "STRUCTURAL_OBLIGATION_UNSATISFIED")
        self.assertEqual(evidence["error_type"], "STRUCTURAL_OBLIGATION_UNSATISFIED")
        self.assertFalse(any(candidate for _kind, candidate in backend.calls))
        self.assertEqual(result["budget"]["credits_used"], 25)
        self.assertEqual(result["semantic_no_improvement_rounds"], 0)


if __name__ == "__main__":
    unittest.main()
