from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_experience_kb import build_kb_query
from llm4hls_agent.v3_experience_v2 import seal_experience_v2
from llm4hls_agent.v3_strategy_ranker_v3 import BayesianStrategyRankerV3

from .test_v3_experience_v2_schema import sample_body


class BayesianStrategyRankerV3Tests(unittest.TestCase):
    def record(
        self,
        index: int,
        *,
        family: str | None = None,
        final_status: str = "PASS",
        confidence: float = 1.0,
        reasons: list[str] | None = None,
        mode: str = "OPTIMIZE",
        failure_subtype: str = "UNKNOWN",
        bottleneck_subtype: str = "SERIAL_REDUCTION",
        atom: str = "LOOP_UNROLL",
    ) -> dict[str, object]:
        body = copy.deepcopy(sample_body())
        body["source"].update(
            {
                "run_id": f"run-{index}",
                "candidate_id": f"candidate-{index}",
                "round_index": index,
                "task_family_hash": family or f"{index + 10:064x}",
            }
        )
        body["strategy"].update(
            {
                "declared_strategy_bundle": [atom],
                "observed_strategy_atoms": [atom],
                "patch_digest": f"{index + 100:064x}",
                "strategy_normalization_confidence": confidence,
                "normalization_reason_codes": (
                    reasons
                    if reasons is not None
                    else ["OBSERVED_PRAGMA_UNROLL"]
                ),
            }
        )
        body["problem"].update(
            {
                "mode": mode,
                "failure_subtype": failure_subtype,
                "bottleneck_subtype": bottleneck_subtype,
            }
        )
        body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
        body["validation"]["fresh_final_status"] = final_status
        if final_status == "FAIL":
            body["validation"].update(
                {"promoted": False, "rejected": True}
            )
            body["performance"].update(
                {
                    "strict_improvement": False,
                    "acceleration": 1.0,
                    "latency_after": body["performance"]["latency_before"],
                }
            )
        return seal_experience_v2(body)

    def query(
        self,
        *,
        mode: str = "OPTIMIZE",
        failure_subtype: str = "UNKNOWN",
        bottleneck_subtype: str = "SERIAL_REDUCTION",
    ) -> dict[str, object]:
        return build_kb_query(
            mode=mode,
            task_split="hidden_like",
            failure_subtype=failure_subtype,
            bottleneck_subtype=bottleneck_subtype,
            algorithm_family="DOT_PRODUCT",
            task_family_hash="f" * 64,
            structure_features={
                "failure_stage": "NONE",
                "primary_bottleneck": "LOOP_LATENCY",
                "has_reduction": True,
            },
            current_run_id="current-run",
        )

    def test_repeated_candidates_from_one_family_are_one_sample(self) -> None:
        shared = "a" * 64
        result = BayesianStrategyRankerV3().rank(
            self.query(),
            [
                self.record(1, family=shared),
                self.record(2, family=shared),
                self.record(3, family=shared),
                self.record(4, family=shared),
            ],
        )
        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertEqual(result["all_atoms"][0]["attempts"], 1)

    def test_any_failure_makes_family_label_fail(self) -> None:
        shared = "a" * 64
        records = [
            self.record(1, family=shared),
            self.record(2, family=shared, final_status="FAIL"),
            self.record(3),
            self.record(4),
        ]
        result = BayesianStrategyRankerV3().rank(self.query(), records)
        entry = result["all_atoms"][0]
        self.assertEqual(entry["attempts"], 3)
        self.assertEqual(entry["successes"], 2)
        self.assertEqual(entry["failures"], 1)

    def test_not_run_is_excluded_not_counted_as_failure(self) -> None:
        result = BayesianStrategyRankerV3().rank(
            self.query(),
            [
                self.record(1),
                self.record(2),
                self.record(3),
                self.record(4, final_status="NOT_RUN"),
            ],
        )
        self.assertEqual(result["excluded_unverified_record_count"], 1)
        self.assertEqual(result["all_atoms"][0]["attempts"], 3)
        self.assertEqual(result["all_atoms"][0]["failures"], 0)

    def test_low_confidence_declared_fallback_cannot_recommend(self) -> None:
        records = [
            self.record(
                index,
                confidence=0.35,
                reasons=["DECLARED_STRATEGY_FALLBACK"],
            )
            for index in range(1, 5)
        ]
        result = BayesianStrategyRankerV3().rank(self.query(), records)
        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertEqual(result["all_atoms"][0]["attempts"], 0)

    def test_generic_other_requires_diverse_failure_free_exact_cell(self) -> None:
        records = [
            self.record(
                index,
                mode="REPAIR",
                failure_subtype="FUNCTIONAL_MISMATCH_OTHER",
                bottleneck_subtype="UNKNOWN",
                atom="OTHER_FUNCTIONAL_REPAIR",
                confidence=0.35,
                reasons=["DECLARED_STRATEGY_FALLBACK"],
            )
            for index in range(1, 5)
        ]
        query = self.query(
            mode="REPAIR",
            failure_subtype="FUNCTIONAL_MISMATCH_OTHER",
            bottleneck_subtype="UNKNOWN",
        )
        result = BayesianStrategyRankerV3().rank(query, records)
        self.assertEqual(result["decision"], "RECOMMEND")
        self.assertEqual(
            result["recommended"]["conditioning"],
            "MODE_SUBTYPE_GENERIC_OTHER",
        )

        failed = copy.deepcopy(records)
        failed[0] = self.record(
            1,
            mode="REPAIR",
            failure_subtype="FUNCTIONAL_MISMATCH_OTHER",
            bottleneck_subtype="UNKNOWN",
            atom="OTHER_FUNCTIONAL_REPAIR",
            confidence=0.35,
            reasons=["DECLARED_STRATEGY_FALLBACK"],
            final_status="FAIL",
        )
        self.assertEqual(
            BayesianStrategyRankerV3().rank(query, failed)["decision"],
            "ABSTAIN",
        )

    def test_generic_other_does_not_cross_subtype(self) -> None:
        records = [
            self.record(
                index,
                mode="REPAIR",
                failure_subtype="WRONG_COEFFICIENT",
                bottleneck_subtype="UNKNOWN",
                atom="OTHER_FUNCTIONAL_REPAIR",
                confidence=0.35,
                reasons=["DECLARED_STRATEGY_FALLBACK"],
            )
            for index in range(1, 5)
        ]
        result = BayesianStrategyRankerV3().rank(
            self.query(
                mode="REPAIR",
                failure_subtype="WRONG_COEFFICIENT",
                bottleneck_subtype="UNKNOWN",
            ),
            records,
        )
        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertEqual(result["all_atoms"][0]["attempts"], 0)

    def test_diverse_verified_families_can_recommend(self) -> None:
        result = BayesianStrategyRankerV3().rank(
            self.query(),
            [self.record(index) for index in range(1, 6)],
        )
        self.assertEqual(result["decision"], "RECOMMEND")
        self.assertEqual(
            result["recommended"]["strategy_atom"], "LOOP_UNROLL"
        )
        self.assertTrue(result["input_contract"]["family_level_sampling"])
        self.assertFalse(
            result["input_contract"]["prompt_injection_authorized"]
        )

    def test_zero_failure_safe_tie_uses_stable_order(self) -> None:
        records = [
            self.record(index, atom=atom)
            for index, atom in (
                (1, "ARRAY_PARTITION"),
                (2, "ARRAY_PARTITION"),
                (3, "ARRAY_PARTITION"),
                (4, "LOOP_PIPELINE"),
                (5, "LOOP_PIPELINE"),
                (6, "LOOP_PIPELINE"),
            )
        ]

        result = BayesianStrategyRankerV3().rank(self.query(), records)

        self.assertEqual(result["decision"], "RECOMMEND")
        self.assertEqual(
            result["recommended"]["strategy_atom"],
            "ARRAY_PARTITION",
        )
        self.assertEqual(
            result["tie_resolution"],
            "ZERO_FAILURE_SAFE_TIE_STABLE_ORDER",
        )

    def test_tie_with_verified_failed_families_still_abstains(self) -> None:
        records = []
        index = 1
        for atom in ("ARRAY_PARTITION", "LOOP_PIPELINE"):
            for offset in range(5):
                records.append(
                    self.record(
                        index,
                        atom=atom,
                        final_status="FAIL" if offset == 0 else "PASS",
                    )
                )
                index += 1

        result = BayesianStrategyRankerV3().rank(self.query(), records)

        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertEqual(
            result["abstain_reason"],
            "INSUFFICIENT_TOP_STRATEGY_MARGIN",
        )
        self.assertIsNone(result["tie_resolution"])


if __name__ == "__main__":
    unittest.main()
