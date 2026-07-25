from __future__ import annotations

import unittest

from llm4hls_agent.v3_continuation import (
    canonical_sha256,
    continuation_cost,
    continuation_decision,
    evidence_delta,
    evidence_fingerprint,
    load_performance_area_policy,
    observed_strategy_atoms,
    performance_area_delta,
    strategy_novelty,
)


def metrics(*, latency: int, lut: int = 100, ff: int = 100, dsp: int = 1, bram: int = 1, ii: int = 1) -> dict[str, object]:
    return {
        "latency": {"worst": latency},
        "interval": {"max": latency},
        "estimated_clock_period_ns": 5.0,
        "resources": {"LUT": lut, "FF": ff, "DSP": dsp, "BRAM_18K": bram, "URAM": 0},
        "available_resources": {"LUT": 1000, "FF": 1000, "DSP": 10, "BRAM_18K": 10, "URAM": 10},
        "loop_evidence": {"loops": [{"loop_id": "compute/L0", "pipeline_ii": ii, "trip_count": 128}]},
    }


class PerformanceAreaTests(unittest.TestCase):
    def test_dominates_and_preserves_raw_resources(self) -> None:
        value = performance_area_delta(metrics(latency=100, lut=200), metrics(latency=80, lut=100))
        self.assertEqual(value["pareto_relation"], "DOMINATES")
        self.assertEqual(value["delta_class"], "BALANCED_PERFORMANCE_AREA_IMPROVEMENT")
        self.assertEqual(value["after"]["resources"]["lut"], 100.0)
        self.assertEqual(value["power"], "NOT_COLLECTED")

    def test_tradeoff_and_unknown_are_not_hidden(self) -> None:
        tradeoff = performance_area_delta(metrics(latency=100, lut=100), metrics(latency=80, lut=400))
        self.assertEqual(tradeoff["pareto_relation"], "NON_DOMINATED")
        unknown = performance_area_delta({"latency": {"worst": 10}}, metrics(latency=8))
        self.assertEqual(unknown["pareto_relation"], "UNKNOWN")
        self.assertIsNone(unknown["before"]["area_proxy"])

    def test_policy_is_versioned(self) -> None:
        self.assertEqual(load_performance_area_policy().policy_version, "v3.performance-area-policy.v1")


class EvidenceDeltaTests(unittest.TestCase):
    def test_latency_only_is_not_actionable(self) -> None:
        before = {"observations": [{"kind": "TOP_LEVEL_INTERVAL_GT_1"}]}
        after = {"observations": [{"kind": "TOP_LEVEL_INTERVAL_GT_1"}]}
        delta = evidence_delta(before, after, mode="OPTIMIZE", before_metrics=metrics(latency=100), after_metrics=metrics(latency=90))
        self.assertFalse(delta["has_actionable_new_evidence"])

    def test_memory_bottleneck_change_is_actionable(self) -> None:
        before = {"observations": [{"kind": "TOP_LEVEL_INTERVAL_GT_1"}]}
        after = {"observations": [{"kind": "MEMORY_SCHEDULING_CONSTRAINT"}]}
        delta = evidence_delta(before, after, mode="OPTIMIZE", before_metrics=metrics(latency=100), after_metrics=metrics(latency=90))
        self.assertTrue(delta["has_actionable_new_evidence"])
        self.assertTrue(delta["bottleneck_changed"])

    def test_path_and_timestamp_are_removed_from_fingerprint(self) -> None:
        first = evidence_fingerprint("REPAIR", {"stage": "csim", "subtype": "mismatch", "source_location": "/tmp/a/kernel.cpp:10", "affected_symbol": "sum"})
        second = evidence_fingerprint("REPAIR", {"stage": "csim", "subtype": "mismatch", "source_location": "/another/run/kernel.cpp:10", "affected_symbol": "sum"})
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_extractor_schema_stage_and_subtype_changes_are_actionable(self) -> None:
        before = {
            "schema_version": "v3c.synth-failure-evidence.v1",
            "phase": "synth_error",
            "failure_kind": "SYNTH_ERROR",
            "source_locations": [],
        }
        after = {
            "schema_version": "v3c.csim-failure-evidence.v1",
            "phase": "compile_error",
            "failure_kind": "COMPILE_ERROR",
            "source_locations": ["kernel.cpp:9:10"],
        }

        delta = evidence_delta(before, after, mode="SYNTH_FIX")

        self.assertTrue(delta["failure_stage_changed"])
        self.assertTrue(delta["failure_subtype_changed"])
        self.assertTrue(delta["source_location_changed"])
        self.assertTrue(delta["has_actionable_new_evidence"])


class StrategyAndDecisionTests(unittest.TestCase):
    def test_observed_patch_strategy_beats_declared_order(self) -> None:
        atoms = observed_strategy_atoms(declared=("loop_unroll", "memory_partition"), patch="#pragma HLS UNROLL factor=4\n#pragma HLS ARRAY_PARTITION variable=a cyclic factor=4")
        self.assertEqual(atoms, ("LOOP_UNROLL", "MEMORY_PARTITION"))
        novelty = strategy_novelty(declared=("x",), patch="#pragma HLS UNROLL", attempted=(("LOOP_UNROLL",),))
        self.assertTrue(novelty["duplicate_strategy"])

    def test_optimize_improvement_without_new_bottleneck_blocks(self) -> None:
        decision = continuation_decision(
            run_id="safe", round_index=2, mode="OPTIMIZE", policy_mode="enforce",
            has_correct_candidate=True, has_strict_latency_improvement=True,
            performance_area={"pareto_relation": "DOMINATES", "tradeoff_detected": False},
            delta={"has_actionable_new_evidence": False, "evidence_strength": "NONE", "bottleneck_changed": False, "reason_codes": []},
            strategies={"untried_matched_strategy_atoms": [], "duplicate_strategy": False, "duplicate_patch": False, "reason_codes": []},
            cost=continuation_cost(ledger={"tokens_remaining": 10000, "credits_remaining": 50}, estimated_input_tokens=100, estimated_output_tokens=200, estimated_credits=5, estimated_wall_time_seconds=1, final_reserve_safe=True),
            remaining_rounds=2,
        )
        self.assertEqual(decision["decision"], "BLOCK")
        self.assertEqual(decision["decision_hash"], canonical_sha256({key: value for key, value in decision.items() if key != "decision_hash"}))

    def test_structural_new_subtype_is_allowed(self) -> None:
        decision = continuation_decision(
            run_id="safe", round_index=2, mode="STRUCTURAL_FIX", policy_mode="enforce",
            has_correct_candidate=False, has_strict_latency_improvement=False,
            performance_area={},
            delta={"has_actionable_new_evidence": True, "evidence_strength": "MEDIUM", "failure_subtype_changed": True, "reason_codes": ["EVIDENCE_FAILURE_SUBTYPE_CHANGED"]},
            strategies={"untried_matched_strategy_atoms": ["FIFO_DEPTH"], "duplicate_strategy": False, "duplicate_patch": False, "reason_codes": []},
            cost=continuation_cost(ledger={"tokens_remaining": 10000, "credits_remaining": 50}, estimated_input_tokens=100, estimated_output_tokens=200, estimated_credits=21, estimated_wall_time_seconds=1, final_reserve_safe=True),
            remaining_rounds=2,
        )
        self.assertEqual(decision["decision"], "ALLOW")

    def test_hard_reserve_blocks(self) -> None:
        decision = continuation_decision(
            run_id="safe", round_index=2, mode="REPAIR", policy_mode="enforce",
            has_correct_candidate=True, has_strict_latency_improvement=False, performance_area={},
            delta={"has_actionable_new_evidence": True, "evidence_strength": "HIGH", "reason_codes": []},
            strategies={"untried_matched_strategy_atoms": ["REPAIR"], "duplicate_strategy": False, "duplicate_patch": False, "reason_codes": []},
            cost=continuation_cost(ledger={"tokens_remaining": 10000, "credits_remaining": 4}, estimated_input_tokens=100, estimated_output_tokens=200, estimated_credits=5, estimated_wall_time_seconds=1, final_reserve_safe=False),
            remaining_rounds=1,
        )
        self.assertEqual(decision["decision"], "DEFER_TO_FINAL")
