from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_continuation_v2 import (
    canonical_sha256,
    continuation_decision_v2,
    legal_eight_x_stop_status,
)


def legal_eight_x_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "baseline_latency": 800.0,
        "previous_latency": 100.0,
        "current_latency": 100.0,
        "acceleration_vs_baseline": 8.0,
        "scoring_cap": 8.0,
        "has_verified_incumbent": True,
        "incumbent_eligible": True,
        "current_csim": "PASS",
        "current_synth": "PASS",
        "current_cosim": "NOT_RUN",
        "tool_config_comparable": True,
        "clock_gate_passed": True,
        "resource_gate_passed": True,
        "cosim_required": False,
        "evidence_complete": True,
        "search_closeout_reserve_available": True,
    }
    state.update(overrides)
    return state


class ContinuationV2CommonTests(unittest.TestCase):
    def test_same_input_and_digest_are_stable(self) -> None:
        state = {
            "failure_signature": "compile:error",
            "evidence_delta": {"failure_subtype_changed": True},
            "search_closeout_reserve_available": True,
        }
        first = continuation_decision_v2(mode="REPAIR", pre_state=state)
        second = continuation_decision_v2(mode="REPAIR", pre_state=copy.deepcopy(state))
        self.assertEqual(first, second)
        payload = {key: value for key, value in first.items() if key != "decision_digest"}
        self.assertEqual(first["decision_digest"], canonical_sha256(payload))

    def test_future_fields_cannot_change_decision_or_digest(self) -> None:
        state = {
            "failure_signature": "compile:error",
            "evidence_complete": False,
            "search_closeout_reserve_available": True,
        }
        expected = continuation_decision_v2(mode="REPAIR", pre_state=state)
        leaked = dict(state)
        leaked.update(
            {
                "outcome": {"class": "BENEFICIAL_CORRECTNESS"},
                "future_latency": 1,
                "candidate_promoted": True,
                "followup_patch": "secret future patch",
                "final_result": "PASS",
            }
        )
        self.assertEqual(
            expected, continuation_decision_v2(mode="REPAIR", pre_state=leaked)
        )

    def test_search_closeout_reserve_unavailable_never_allows(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "search_closeout_reserve_available": False,
                "previous_latency": 100,
                "current_latency": 1,
            },
        )
        self.assertEqual(value["decision"], "DEFER_TO_FINAL")

    def test_missing_evidence_is_not_high_confidence_block(self) -> None:
        for mode in ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"):
            value = continuation_decision_v2(
                mode=mode,
                pre_state={
                    "evidence_complete": False,
                    "search_closeout_reserve_available": True,
                },
            )
            self.assertFalse(
                value["decision"] == "BLOCK" and value["confidence"] == "HIGH"
            )


class ContinuationV2RepairTests(unittest.TestCase):
    def test_changed_error_allows(self) -> None:
        value = continuation_decision_v2(
            mode="REPAIR",
            pre_state={
                "evidence_delta": {"failure_subtype_changed": True},
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")

    def test_repeated_identical_repair_can_block_or_defer(self) -> None:
        state = {
            "previous_failure_signature": "mismatch",
            "failure_signature": "mismatch",
            "previous_failure_location_signature": "kernel.cpp:9",
            "failure_location_signature": "kernel.cpp:9",
            "current_observed_strategy": ["INDEX_FIX"],
            "observed_strategy_history": [["INDEX_FIX"], ["INDEX_FIX"]],
            "strategy_novelty": "DUPLICATE",
            "consecutive_no_progress": 2,
            "search_closeout_reserve_available": True,
        }
        blocked = continuation_decision_v2(mode="REPAIR", pre_state=state)
        self.assertEqual(blocked["decision"], "BLOCK")
        state.update({"has_verified_incumbent": True, "reserve_tight": True})
        deferred = continuation_decision_v2(mode="REPAIR", pre_state=state)
        self.assertEqual(deferred["decision"], "DEFER_TO_FINAL")

    def test_one_failure_does_not_block(self) -> None:
        value = continuation_decision_v2(
            mode="REPAIR",
            pre_state={
                "previous_failure_signature": "mismatch",
                "failure_signature": "mismatch",
                "previous_failure_location_signature": "kernel.cpp:9",
                "failure_location_signature": "kernel.cpp:9",
                "strategy_novelty": "DUPLICATE",
                "consecutive_no_progress": 1,
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")


class ContinuationV2SynthTests(unittest.TestCase):
    def test_stage_advance_allows(self) -> None:
        value = continuation_decision_v2(
            mode="SYNTH_FIX",
            pre_state={
                "evidence_delta": {"synth_stage_advanced": True},
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")

    def test_repeated_unsupported_construct_tends_to_stop(self) -> None:
        value = continuation_decision_v2(
            mode="SYNTH_FIX",
            pre_state={
                "previous_failure_signature": "unsupported:malloc",
                "failure_signature": "unsupported:malloc",
                "previous_failure_location_signature": "kernel.cpp:12",
                "failure_location_signature": "kernel.cpp:12",
                "strategy_novelty": "DUPLICATE",
                "consecutive_no_progress": 2,
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "BLOCK")


class ContinuationV2StructuralTests(unittest.TestCase):
    def test_new_fifo_evidence_allows(self) -> None:
        value = continuation_decision_v2(
            mode="STRUCTURAL_FIX",
            pre_state={
                "evidence_delta": {"fifo_evidence_new": True},
                "current_cosim": "FAIL",
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")

    def test_new_deadlock_location_allows_despite_cosim_failure(self) -> None:
        value = continuation_decision_v2(
            mode="STRUCTURAL_FIX",
            pre_state={
                "evidence_delta": {"deadlock_location_changed": True},
                "current_cosim": "FAIL",
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")

    def test_only_complete_repetition_can_block(self) -> None:
        base = {
            "evidence_delta": {
                "same_failure_signature": True,
                "same_failure_location": True,
                "same_fifo_evidence": True,
                "same_observed_strategy": True,
                "new_actionable_evidence": False,
            },
            "strategy_novelty": "DUPLICATE",
            "consecutive_no_progress": 2,
            "search_closeout_reserve_available": True,
        }
        value = continuation_decision_v2(mode="STRUCTURAL_FIX", pre_state=base)
        self.assertEqual(value["decision"], "BLOCK")
        self.assertNotEqual(value["confidence"], "HIGH")
        advanced = copy.deepcopy(base)
        advanced["evidence_delta"]["fifo_evidence_new"] = True
        self.assertEqual(
            continuation_decision_v2(
                mode="STRUCTURAL_FIX", pre_state=advanced
            )["decision"],
            "ALLOW",
        )


class ContinuationV2OptimizeTests(unittest.TestCase):
    def test_significant_latency_gain_allows(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "previous_latency": 100,
                "current_latency": 80,
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")

    def test_low_gain_with_incumbent_and_repeat_defers(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "previous_latency": 1000,
                "current_latency": 995,
                "has_verified_incumbent": True,
                "strategy_novelty": "DUPLICATE",
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "DEFER_TO_FINAL")

    def test_significant_parallel_reduction_bundle_gain_still_allows(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "previous_latency": 1027,
                "current_latency": 518,
                "has_verified_incumbent": True,
                "has_better_verified_candidate_needing_final": True,
                "current_observed_strategy": [
                    "LOOP_UNROLL",
                    "MEMORY_PARTITION",
                    "PARALLEL_REDUCTION",
                    "PIPELINE_ONLY",
                ],
                "strategy_novelty": "HIGH",
                "search_closeout_reserve_available": True,
            },
        )

        self.assertEqual(value["decision"], "ALLOW")
        self.assertIn(
            "SIGNIFICANT_LATENCY_IMPROVEMENT",
            value["reason_codes"],
        )
        self.assertNotIn(
            "SATURATED_PARALLEL_REDUCTION_BUNDLE_PENDING_FINAL",
            value["reason_codes"],
        )

    def test_parallel_reduction_without_unroll_still_allows(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "previous_latency": 1027,
                "current_latency": 553,
                "has_verified_incumbent": True,
                "has_better_verified_candidate_needing_final": True,
                "current_observed_strategy": [
                    "MEMORY_PARTITION",
                    "PARALLEL_REDUCTION",
                    "PIPELINE_ONLY",
                ],
                "strategy_novelty": "HIGH",
                "search_closeout_reserve_available": True,
            },
        )

        self.assertEqual(value["decision"], "ALLOW")

    def test_fixed_legal_eight_x_predicate_reaches_cap(self) -> None:
        value = legal_eight_x_stop_status(
            mode="OPTIMIZE",
            incumbent_eligible=True,
            csim_passed=True,
            synth_passed=True,
            baseline_latency=800.0,
            candidate_latency=100.0,
            tool_config_comparable=True,
            clock_passed=True,
            resource_passed=True,
            cosim_required=False,
            cosim_passed=False,
        )
        self.assertTrue(value["reached"])
        self.assertEqual(value["acceleration"], 8.0)

    def test_learning_policy_does_not_own_eight_x_stop(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_7_99x_candidate_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(
                baseline_latency=799.0,
                acceleration_vs_baseline=7.99,
            ),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_zero_latency_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(
                current_latency=0.0,
                acceleration_vs_baseline=None,
            ),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_csim_failure_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(current_csim="FAIL"),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_synth_failure_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(current_synth="FAIL"),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_clock_failure_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(clock_gate_passed=False),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_resource_overflow_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(resource_gate_passed=False),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_required_cosim_failure_does_not_trigger_cap(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(
                cosim_required=True,
                current_cosim="FAIL",
            ),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_low_risk_optimize_does_not_require_search_cosim(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state=legal_eight_x_state(
                cosim_required=False,
                current_cosim="NOT_RUN",
            ),
        )
        self.assertEqual(value["decision"], "ALLOW")
        self.assertNotIn(
            "SCORING_ACCELERATION_CAP_REACHED",
            value["reason_codes"],
        )

    def test_cap_requires_optimize_eligible_incumbent_and_comparable_tools(
        self,
    ) -> None:
        cases = (
            (
                "non_optimize",
                "STRUCTURAL_FIX",
                legal_eight_x_state(),
            ),
            (
                "ineligible_incumbent",
                "OPTIMIZE",
                legal_eight_x_state(incumbent_eligible=False),
            ),
            (
                "incomparable_tools",
                "OPTIMIZE",
                legal_eight_x_state(tool_config_comparable=False),
            ),
        )
        for name, mode, state in cases:
            with self.subTest(name=name):
                value = continuation_decision_v2(
                    mode=mode,
                    pre_state=state,
                )
                self.assertNotIn(
                    "SCORING_ACCELERATION_CAP_REACHED",
                    value["reason_codes"],
                )

    def test_missing_resources_remain_unknown_not_zero(self) -> None:
        missing = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "current_resource_utilization": None,
                "evidence_complete": False,
                "has_verified_incumbent": True,
                "search_closeout_reserve_available": True,
            },
        )
        zero = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "current_resource_utilization": 0,
                "evidence_complete": False,
                "has_verified_incumbent": True,
                "search_closeout_reserve_available": True,
            },
        )
        self.assertEqual(missing["decision"], "DEFER_TO_FINAL")
        self.assertEqual(zero["decision"], "DEFER_TO_FINAL")
        self.assertIn("unknown_metrics_are_not_zero", missing["supporting_evidence"])
        self.assertIn("resource_utilization=UNKNOWN", missing["supporting_evidence"])
        self.assertIn("resource_utilization=KNOWN", zero["supporting_evidence"])


if __name__ == "__main__":
    unittest.main()
