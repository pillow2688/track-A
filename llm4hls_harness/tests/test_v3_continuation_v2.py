from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_continuation_v2 import (
    canonical_sha256,
    continuation_decision_v2,
)


class ContinuationV2CommonTests(unittest.TestCase):
    def test_same_input_and_digest_are_stable(self) -> None:
        state = {
            "failure_signature": "compile:error",
            "evidence_delta": {"failure_subtype_changed": True},
            "final_reserve_available": True,
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
            "final_reserve_available": True,
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

    def test_final_reserve_unavailable_never_allows(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "final_reserve_available": False,
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
                    "final_reserve_available": True,
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
                "final_reserve_available": True,
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
            "final_reserve_available": True,
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
                "final_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")


class ContinuationV2SynthTests(unittest.TestCase):
    def test_stage_advance_allows(self) -> None:
        value = continuation_decision_v2(
            mode="SYNTH_FIX",
            pre_state={
                "evidence_delta": {"synth_stage_advanced": True},
                "final_reserve_available": True,
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
                "final_reserve_available": True,
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
                "final_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "ALLOW")

    def test_new_deadlock_location_allows_despite_cosim_failure(self) -> None:
        value = continuation_decision_v2(
            mode="STRUCTURAL_FIX",
            pre_state={
                "evidence_delta": {"deadlock_location_changed": True},
                "current_cosim": "FAIL",
                "final_reserve_available": True,
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
            "final_reserve_available": True,
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
                "final_reserve_available": True,
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
                "final_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "DEFER_TO_FINAL")

    def test_saturated_parallel_reduction_bundle_defers_to_final(self) -> None:
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
                "final_reserve_available": True,
            },
        )

        self.assertEqual(value["decision"], "DEFER_TO_FINAL")
        self.assertIn(
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
                "final_reserve_available": True,
            },
        )

        self.assertEqual(value["decision"], "ALLOW")

    def test_eight_x_cap_defers(self) -> None:
        value = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "acceleration_vs_baseline": 8,
                "scoring_cap": 8,
                "has_verified_incumbent": True,
                "final_reserve_available": True,
            },
        )
        self.assertEqual(value["decision"], "DEFER_TO_FINAL")

    def test_missing_resources_remain_unknown_not_zero(self) -> None:
        missing = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "current_resource_utilization": None,
                "evidence_complete": False,
                "has_verified_incumbent": True,
                "final_reserve_available": True,
            },
        )
        zero = continuation_decision_v2(
            mode="OPTIMIZE",
            pre_state={
                "current_resource_utilization": 0,
                "evidence_complete": False,
                "has_verified_incumbent": True,
                "final_reserve_available": True,
            },
        )
        self.assertEqual(missing["decision"], "DEFER_TO_FINAL")
        self.assertEqual(zero["decision"], "DEFER_TO_FINAL")
        self.assertIn("unknown_metrics_are_not_zero", missing["supporting_evidence"])
        self.assertIn("resource_utilization=UNKNOWN", missing["supporting_evidence"])
        self.assertIn("resource_utilization=KNOWN", zero["supporting_evidence"])


if __name__ == "__main__":
    unittest.main()
