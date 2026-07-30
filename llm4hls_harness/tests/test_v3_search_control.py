"""Focused contracts for the bounded V3 search-control layer."""

from __future__ import annotations

import unittest

from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.v3_search_control import (
    candidate_state,
    canonical_action_family,
    continuation_action,
    failure_facts,
    is_repeated_terminal_experiment,
    obligation_state,
    primary_obligation,
    proposal_experiment,
    search_control_state,
    structural_facts,
)


def _proposal(*, hypothesis: str, patch: str) -> PatchProposal:
    return PatchProposal(
        patch=patch,
        hypothesis=hypothesis,
        change_class="STRUCTURAL_REPAIR",
        expected_effect="remove the observed blocker",
    )


class SearchControlTests(unittest.TestCase):
    def test_patch_coordinate_failure_is_mechanical_not_terminal(self) -> None:
        state = search_control_state(
            mode="SYNTH_FIX",
            evidence={"phase": "synth_error", "failure_kind": "SYNTH_ERROR"},
            patch_failure={"error_type": "PATCH_HUNK_NEW_COUNT_MISMATCH"},
            history=[],
            semantic_no_improvement=0,
            baseline_id="candidate_000",
            incumbent_id="candidate_000",
        )
        self.assertEqual(state["primary_obligation"], "PATCH_APPLICABILITY")
        self.assertTrue(state["failure"]["mechanical_failure"])
        self.assertFalse(state["failure"]["terminal_failure"])
        self.assertEqual(state["required_next_change"], "REGENERATE_APPLICABLE_DIFF")

    def test_cosim_timeout_schema_never_becomes_unknown_failure_stage(self) -> None:
        facts = failure_facts(
            {
                "schema_version": "v3c.cosim-failure-evidence.v1",
                "phase": "timeout",
                "failure_kind": "TIMEOUT",
                "error_summary": "no RTL test progress",
            }
        )
        self.assertEqual(facts["last_failure_stage"], "COSIM")
        self.assertEqual(facts["last_failure_kind"], "TIMEOUT")

    def test_structural_fifo_and_topology_proposals_are_distinct_families(self) -> None:
        topology = proposal_experiment(
            _proposal(
                hypothesis="reorder stream writes before reads",
                patch="--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS DATAFLOW\n+s.write(x);\n",
            ),
            obligation="RTL_LIVENESS",
        )
        fifo = proposal_experiment(
            _proposal(
                hypothesis="increase the FIFO depth",
                patch="--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS STREAM variable=s depth=4\n",
            ),
            obligation="RTL_LIVENESS",
        )
        self.assertEqual(topology["action_family"], "DATAFLOW_TOPOLOGY_OR_ORDER")
        self.assertEqual(fifo["action_family"], "FIFO_CAPACITY_OR_PROTOCOL")
        self.assertTrue(fifo["changes_stream_topology"])

    def test_only_terminal_repeat_is_rejected(self) -> None:
        experiment = proposal_experiment(
            _proposal(
                hypothesis="reorder stream writes before reads",
                patch="--- a/kernel.cpp\n+++ b/kernel.cpp\n+s.write(x);\n",
            ),
            obligation="RTL_LIVENESS",
        )
        history = [{"proposal_experiment": experiment}]
        self.assertTrue(
            is_repeated_terminal_experiment(
                experiment, history, terminal_failure=True
            )
        )
        self.assertFalse(
            is_repeated_terminal_experiment(
                experiment, history, terminal_failure=False
            )
        )
        distinct_family = dict(experiment)
        distinct_family["action_family"] = "FIFO_CAPACITY_OR_PROTOCOL"
        self.assertFalse(
            is_repeated_terminal_experiment(
                distinct_family, history, terminal_failure=True
            )
        )
        distinct_hypothesis = dict(experiment)
        distinct_hypothesis["hypothesis"] = "introduce an initial token"
        self.assertFalse(
            is_repeated_terminal_experiment(
                distinct_hypothesis, history, terminal_failure=True
            )
        )
        self.assertTrue(
            is_repeated_terminal_experiment(
                distinct_hypothesis,
                history,
                terminal_failure=True,
                forbidden_action_families=[experiment["action_family"]],
            )
        )

    def test_stream_depth_alias_cannot_bypass_terminal_family_boundary(self) -> None:
        self.assertEqual(
            canonical_action_family("STREAM_DEPTH_INCREASE"),
            "FIFO_CAPACITY_OR_PROTOCOL",
        )
        history = [
            {
                "proposal_experiment": {
                    "action_family": "STREAM_DEPTH_INCREASE",
                    "hypothesis": "increase FIFO depth to two",
                    "input_failure_signature": "a" * 64,
                }
            }
        ]
        source = """
void first(hls::stream<int>& forward, hls::stream<int>& feedback) { forward.write(feedback.read()); }
void second(hls::stream<int>& forward, hls::stream<int>& feedback) { feedback.write(forward.read()); }
void top() { hls::stream<int> forward, feedback; first(forward, feedback); second(forward, feedback); }
"""
        base = dict(
            mode="STRUCTURAL_FIX",
            evidence={"phase": "cosim_fail", "failure_kind": "DEADLOCK"},
            patch_failure=None,
            semantic_no_improvement=1,
            baseline_id="candidate_000",
            incumbent_id="candidate_000",
            source=source,
        )
        state = search_control_state(history=history, **base)
        history[0]["proposal_experiment"]["input_failure_signature"] = state["failure"]["last_failure_signature"]
        state = search_control_state(history=history, **base)
        self.assertFalse(state["new_evidence_since_last_planner"])
        self.assertEqual(
            state["required_next_change"],
            "REQUIRE_STREAM_TOPOLOGY_OR_VERIFIED_FALLBACK",
        )
        self.assertEqual(
            state["required_action_family"], "DATAFLOW_TOPOLOGY_OR_ORDER"
        )
        self.assertEqual(state["forbidden_action_families"], ["FIFO_CAPACITY_OR_PROTOCOL"])
        self.assertTrue(
            is_repeated_terminal_experiment(
                {"action_family": "STREAM_DEPTH_INCREASE", "hypothesis": "depth four"},
                history,
                terminal_failure=True,
                forbidden_action_families=state["forbidden_action_families"],
                required_action_family=state["required_action_family"],
            )
        )
        self.assertFalse(
            is_repeated_terminal_experiment(
                {"action_family": "DATAFLOW_TOPOLOGY_OR_ORDER", "hypothesis": "break feedback edge"},
                history,
                terminal_failure=True,
                forbidden_action_families=state["forbidden_action_families"],
                required_action_family=state["required_action_family"],
            )
        )

    def test_synth_language_obligation_is_generic_not_task_specific(self) -> None:
        self.assertEqual(
            primary_obligation(
                "SYNTH_FIX",
                {
                    "phase": "synth_error",
                    "failure_kind": "SYNTH_ERROR",
                    "synthesis_error": "std::vector requires unsupported operator new",
                },
            ),
            "SYNTHESIS_LEGALITY",
        )

    def test_structural_facts_report_stream_counts_without_claiming_liveness(self) -> None:
        facts = structural_facts(
            """
void producer(hls::stream<int>& s) { s.write(1); }
void consumer(hls::stream<int>& s) { int x = s.read(); }
void top() {
  hls::stream<int> s;
#pragma HLS STREAM variable=s depth=2
#pragma HLS DATAFLOW
  producer(s); consumer(s);
}
"""
        )
        self.assertTrue(facts["dataflow_present"])
        self.assertEqual(facts["streams"][0]["producer_count"], 1)
        self.assertEqual(facts["streams"][0]["consumer_count"], 1)
        self.assertEqual(facts["feedback_initial_token"], "UNKNOWN")

    def test_structural_facts_find_a_generic_stream_dependency_cycle(self) -> None:
        facts = structural_facts(
            """
void first(hls::stream<int>& forward, hls::stream<int>& feedback) {
  forward.write(feedback.read());
}
void second(hls::stream<int>& forward, hls::stream<int>& feedback) {
  feedback.write(forward.read());
}
void top() {
  hls::stream<int> forward, feedback;
#pragma HLS DATAFLOW
  first(forward, feedback); second(forward, feedback);
}
"""
        )
        self.assertTrue(facts["has_stream_dependency_cycle"])
        self.assertIn("second", facts["process_call_graph"]["first"])
        state = search_control_state(
            mode="STRUCTURAL_FIX",
            evidence={"phase": "cosim_fail", "failure_kind": "DEADLOCK"},
            patch_failure=None,
            history=[],
            semantic_no_improvement=0,
            baseline_id="candidate_000",
            incumbent_id="candidate_000",
            source="""
void first(hls::stream<int>& forward, hls::stream<int>& feedback) { forward.write(feedback.read()); }
void second(hls::stream<int>& forward, hls::stream<int>& feedback) { feedback.write(forward.read()); }
void top() { hls::stream<int> forward, feedback; first(forward, feedback); second(forward, feedback); }
""",
        )
        self.assertIn(
            "stream_topology", [item["kind"] for item in state["obligations"]]
        )
        self.assertIn(
            "hidden_regression_risk",
            [item["kind"] for item in state["obligations"]],
        )

    def test_obligation_and_portfolio_keep_specific_constraint_separate_from_mode(self) -> None:
        obligation = obligation_state(
            "SYNTH_FIX",
            {
                "phase": "synth_error",
                "failure_kind": "SYNTH_ERROR",
                "file": "kernel.cpp",
                "result_ref": "actions/synth/result.json",
            },
        )
        self.assertEqual(obligation["id"], "SYNTHESIS_LEGALITY")
        self.assertEqual(obligation["kind"], "synthesis_legality")
        self.assertEqual(obligation["affected_regions"], ["kernel.cpp"])
        state = search_control_state(
            mode="SYNTH_FIX",
            evidence={"phase": "synth_error", "failure_kind": "SYNTH_ERROR"},
            patch_failure=None,
            history=[],
            semantic_no_improvement=0,
            baseline_id="candidate_000",
            incumbent_id="candidate_001",
            candidate_records={
                "candidate_000": {"status": "BASELINE", "code_hash": "a"},
                "candidate_001": {
                    "parent_id": "candidate_000",
                    "status": "PROMOTED",
                    "code_hash": "b",
                    "validation": {"csim": {"ok": True}, "synth": {"ok": True}},
                },
            },
        )
        portfolio = state["candidate_portfolio"]
        self.assertEqual(portfolio["fallback_parent_id"], "candidate_001")
        self.assertEqual(
            portfolio["candidate_states"]["candidate_001"]["synth_status"], "PASS"
        )

    def test_candidate_state_and_five_way_continuation_are_deterministic(self) -> None:
        self.assertEqual(
            candidate_state("candidate_001", {"validation": {"cosim": {"ok": False}}})[
                "cosim_status"
            ],
            "FAIL",
        )
        self.assertEqual(
            continuation_action(
                facts={"mechanical_failure": True, "safe_local_recovery_available": True},
                attempted=[], baseline_id="candidate_000", incumbent_id="candidate_000",
            ),
            "CONTINUE_WITHOUT_LLM",
        )
        self.assertEqual(
            continuation_action(
                facts={"terminal_failure": True},
                attempted=[{"action_family": "FIFO"}],
                baseline_id="candidate_000", incumbent_id="candidate_001",
            ),
            "SWITCH_PARENT",
        )
        self.assertEqual(
            continuation_action(
                facts={"terminal_failure": True},
                attempted=[], baseline_id="candidate_000", incumbent_id="candidate_000",
            ),
            "CONTINUE_WITH_LLM",
        )
        self.assertEqual(
            continuation_action(
                facts={}, attempted=[], baseline_id="candidate_000", incumbent_id="candidate_000",
                active_probe_id="candidate_001",
            ),
            "STOP",
        )
        self.assertEqual(
            continuation_action(
                facts={}, attempted=[], baseline_id="candidate_000", incumbent_id="candidate_000",
                final_reserve_only=True,
            ),
            "FINALIZE",
        )

    def test_new_evidence_flag_compares_the_previous_planner_input_signature(self) -> None:
        same_signature = "a" * 64
        history = [
            {
                "proposal_experiment": {
                    "action_family": "LOCAL_FUNCTIONAL_REPAIR",
                    "hypothesis": "repair arithmetic",
                    "input_failure_signature": same_signature,
                }
            }
        ]
        base = dict(
            mode="REPAIR",
            evidence={"phase": "runtime_fail", "failure_kind": "RUNTIME_FAIL"},
            patch_failure=None,
            semantic_no_improvement=0,
            baseline_id="candidate_000",
            incumbent_id="candidate_000",
        )
        state = search_control_state(history=history, **base)
        self.assertTrue(state["new_evidence_since_last_planner"])
        current_signature = state["failure"]["last_failure_signature"]
        history[0]["proposal_experiment"]["input_failure_signature"] = current_signature
        state = search_control_state(history=history, **base)
        self.assertFalse(state["new_evidence_since_last_planner"])


if __name__ == "__main__":
    unittest.main()
