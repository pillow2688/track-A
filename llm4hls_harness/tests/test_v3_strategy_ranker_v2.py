from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_experience_kb import build_kb_query
from llm4hls_agent.v3_experience_v2 import seal_experience_v2
from llm4hls_agent.v3_strategy_ranker_v2 import BayesianStrategyRankerV2

from .test_v3_experience_v2_schema import sample_body


class BayesianStrategyRankerV2Tests(unittest.TestCase):
    def record(
        self,
        index: int,
        *,
        success: bool = True,
        family: str | None = None,
        run: str | None = None,
        atom: str = "LOOP_UNROLL",
        evidence_level: str = "REAL_LLM_VITIS",
    ) -> dict[str, object]:
        body = copy.deepcopy(sample_body())
        body["source"].update(
            {
                "run_id": run or f"run-{index}",
                "candidate_id": f"candidate-{index}",
                "round_index": index,
                "task_family_hash": family or f"{index + 10:064x}",
                "evidence_level": evidence_level,
            }
        )
        body["strategy"].update(
            {
                "declared_strategy_bundle": [atom],
                "observed_strategy_atoms": [atom],
                "patch_digest": f"{index + 100:064x}",
            }
        )
        body["provenance"].update(
            {
                "source_record_hash": f"{index + 200:064x}",
                "eligible_for_retrieval": evidence_level == "REAL_LLM_VITIS",
                "eligible_for_ranking": evidence_level == "REAL_LLM_VITIS",
            }
        )
        if not success:
            body["validation"].update(
                {
                    "fresh_final_status": "FAIL",
                    "promoted": False,
                    "rejected": True,
                }
            )
            body["performance"].update(
                {
                    "latency_after": 1027.0,
                    "transaction_interval_after": 1025.0,
                    "acceleration": 1.0,
                    "strict_improvement": False,
                }
            )
        return seal_experience_v2(body)

    def query(self, *, attempted: tuple[str, ...] = ()) -> dict[str, object]:
        return build_kb_query(
            mode="OPTIMIZE",
            task_split="hidden_like",
            bottleneck_subtype="SERIAL_REDUCTION",
            algorithm_family="DOT_PRODUCT",
            task_family_hash="f" * 64,
            strategy_context=attempted,
            current_run_id="current-run",
            toolchain="Vitis 2025.2",
        )

    def test_recommends_only_with_diverse_high_posterior_support(self) -> None:
        result = BayesianStrategyRankerV2().rank(
            self.query(),
            [self.record(1), self.record(2), self.record(3), self.record(4)],
        )
        self.assertEqual(result["decision"], "RECOMMEND")
        self.assertEqual(result["recommended"]["strategy_atom"], "LOOP_UNROLL")
        self.assertGreaterEqual(result["recommended"]["task_family_count"], 2)
        self.assertFalse(result["input_contract"]["prompt_injection_authorized"])

    def test_same_family_support_abstains(self) -> None:
        family = "a" * 64
        result = BayesianStrategyRankerV2().rank(
            self.query(),
            [
                self.record(1, family=family),
                self.record(2, family=family),
                self.record(3, family=family),
                self.record(4, family=family),
            ],
        )
        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertIn(
            "INSUFFICIENT_FAMILY_DIVERSITY",
            result["all_atoms"][0]["ineligibility_reasons"],
        )

    def test_current_run_fixture_and_attempted_strategy_are_excluded(self) -> None:
        records = [
            self.record(1, run="current-run"),
            self.record(2, evidence_level="DETERMINISTIC_FIXTURE"),
            self.record(3),
            self.record(4),
            self.record(5),
        ]
        result = BayesianStrategyRankerV2().rank(
            self.query(attempted=("LOOP_UNROLL",)),
            records,
        )
        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertEqual(result["support_record_count"], 3)
        self.assertEqual(result["all_atoms"], [])

    def test_failures_lower_posterior_and_create_discouragement(self) -> None:
        result = BayesianStrategyRankerV2().rank(
            self.query(),
            [
                self.record(1, success=False),
                self.record(2, success=False),
                self.record(3, success=True),
                self.record(4, success=False),
            ],
        )
        self.assertEqual(result["decision"], "ABSTAIN")
        self.assertEqual(result["discouraged"][0]["strategy_atom"], "LOOP_UNROLL")

    def test_output_is_deterministic_under_input_reordering(self) -> None:
        records = [self.record(1), self.record(2), self.record(3), self.record(4)]
        ranker = BayesianStrategyRankerV2()
        self.assertEqual(
            ranker.rank(self.query(), records),
            ranker.rank(self.query(), list(reversed(records))),
        )


if __name__ == "__main__":
    unittest.main()
