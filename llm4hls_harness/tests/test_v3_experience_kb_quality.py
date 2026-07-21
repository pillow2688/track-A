from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_experience_kb import (
    ExplainableSimilarCaseRetriever,
    build_kb_query,
)
from llm4hls_agent.v3_experience_kb_quality import (
    BayesianAtomRanker,
    HardenedGuidanceQualityGate,
)
from llm4hls_agent.v3_experience_v2 import seal_experience_v2

from .test_v3_experience_v2_schema import sample_body


class ExperienceKBQualityTests(unittest.TestCase):
    def record(
        self,
        index: int,
        *,
        atom: str = "LOOP_UNROLL",
        success: bool = True,
        family: str | None = None,
        run: str | None = None,
        evidence_level: str = "REAL_LLM_VITIS",
        normalization_confidence: float = 1.0,
        prompt_version: str = "v3c.task-aware.v1",
    ) -> dict[str, object]:
        body = copy.deepcopy(sample_body())
        body["source"].update(
            {
                "run_id": run or f"run-{index}",
                "candidate_id": f"candidate-{index}",
                "round_index": index,
                "task_family_hash": family or f"{index + 20:064x}",
                "evidence_level": evidence_level,
                "prompt_version": prompt_version,
            }
        )
        body["strategy"].update(
            {
                "declared_strategy_bundle": [atom],
                "observed_strategy_atoms": [atom],
                "strategy_normalization_confidence": normalization_confidence,
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

    def query(
        self,
        *,
        atom_context: tuple[str, ...] = (),
        structure: dict[str, object] | None = None,
        prompt_version: str = "v3c.task-aware.v1",
    ) -> dict[str, object]:
        return build_kb_query(
            mode="OPTIMIZE",
            task_split="hidden_like",
            bottleneck_subtype="SERIAL_REDUCTION",
            algorithm_family="DOT_PRODUCT",
            task_family_hash="f" * 64,
            structure_features=structure or {},
            strategy_context=atom_context,
            current_run_id="current-run",
            toolchain="Vitis 2025.2",
            backend_fingerprint="vitis-backend-v0.7",
            prompt_version=prompt_version,
        )

    def retrieve_and_rank(self, query, records):
        retrieval = ExplainableSimilarCaseRetriever().retrieve(query, records)
        ranking = BayesianAtomRanker().rank(
            query, [case.record for case in retrieval.considered]
        )
        return retrieval, ranking

    def test_beta_smoothing_and_full_statistics(self) -> None:
        query = self.query()
        _, ranking = self.retrieve_and_rank(
            query, [self.record(1), self.record(2, success=False)]
        )
        entry = ranking["all_atoms"][0]
        self.assertEqual(entry["attempts"], 2)
        self.assertEqual(entry["primary_successes"], 1)
        self.assertEqual(entry["posterior_success"], 0.5)
        self.assertEqual(entry["no_improvement"], 1)
        self.assertIn("median_acceleration", entry)
        self.assertIn("expected_cost", entry)

    def test_small_sample_retains_uncertainty(self) -> None:
        query = self.query()
        _, ranking = self.retrieve_and_rank(query, [self.record(1)])
        entry = ranking["all_atoms"][0]
        self.assertGreater(entry["uncertainty"], 0.2)
        self.assertLess(entry["confidence"], 0.2)

    def test_no_improvement_becomes_discouraged(self) -> None:
        query = self.query()
        _, ranking = self.retrieve_and_rank(
            query, [self.record(1, success=False), self.record(2, success=False)]
        )
        self.assertEqual(ranking["recommended"], [])
        self.assertEqual(ranking["discouraged"][0]["strategy_atom"], "LOOP_UNROLL")

    def test_oracle_or_fixture_evidence_never_enters_ranker(self) -> None:
        query = self.query()
        fixture = self.record(1, evidence_level="DETERMINISTIC_FIXTURE")
        ranking = BayesianAtomRanker().rank(query, [fixture])
        self.assertEqual(ranking["support_count"], 0)
        self.assertEqual(ranking["recommended"], [])

    def test_gate_injects_two_independent_families_and_runs(self) -> None:
        query = self.query()
        retrieval, ranking = self.retrieve_and_rank(
            query, [self.record(1), self.record(2)]
        )
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertTrue(result.injectable)
        self.assertEqual(result.decision["support_count"], 2)
        self.assertEqual(result.decision["recommended_strategy_atoms"], ["LOOP_UNROLL"])
        self.assertLessEqual(result.decision["guidance_tokens"], 600)

    def test_gate_abstains_on_family_or_run_concentration(self) -> None:
        query = self.query()
        same_family = "a" * 64
        retrieval, ranking = self.retrieve_and_rank(
            query,
            [self.record(1, family=same_family), self.record(2, family=same_family)],
        )
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertEqual(result.decision["abstain_reason"], "INSUFFICIENT_TASK_FAMILIES")

        retrieval, ranking = self.retrieve_and_rank(
            query, [self.record(3, run="same-run"), self.record(4, run="same-run")]
        )
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertEqual(result.decision["abstain_reason"], "INSUFFICIENT_INDEPENDENT_RUNS")

    def test_gate_abstains_on_evidence_conflict_and_attempted_atom(self) -> None:
        records = [
            self.record(1, atom="LOOP_PIPELINE"),
            self.record(2, atom="LOOP_PIPELINE"),
        ]
        query = self.query(structure={"critical_loop_ii": 1, "has_pipeline": True})
        retrieval, ranking = self.retrieve_and_rank(query, records)
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertEqual(result.decision["abstain_reason"], "PIPELINE_ALREADY_ACHIEVED")

        query = self.query(atom_context=("LOOP_UNROLL",))
        retrieval, ranking = self.retrieve_and_rank(
            query, [self.record(3), self.record(4)]
        )
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertEqual(
            result.decision["abstain_reason"], "STRATEGY_ALREADY_FAILED_OR_ATTEMPTED"
        )

    def test_gate_rejects_low_observation_confidence_and_prompt_mismatch(self) -> None:
        query = self.query()
        records = [
            self.record(1, normalization_confidence=0.35),
            self.record(2, normalization_confidence=0.35),
        ]
        retrieval, ranking = self.retrieve_and_rank(query, records)
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertEqual(result.decision["abstain_reason"], "INSUFFICIENT_SUPPORT")

        records = [
            self.record(3, prompt_version="old-prompt"),
            self.record(4, prompt_version="old-prompt"),
        ]
        retrieval, ranking = self.retrieve_and_rank(query, records)
        result = HardenedGuidanceQualityGate().evaluate(query, retrieval, ranking)
        self.assertEqual(result.decision["abstain_reason"], "INSUFFICIENT_SUPPORT")

    def test_tiny_prompt_limit_fails_open_without_guidance(self) -> None:
        query = self.query()
        retrieval, ranking = self.retrieve_and_rank(
            query, [self.record(1), self.record(2)]
        )
        result = HardenedGuidanceQualityGate().evaluate(
            query, retrieval, ranking, prompt_token_limit=20
        )
        self.assertEqual(result.decision["decision"], "ABSTAIN")
        self.assertEqual(result.decision["abstain_reason"], "GUIDANCE_TOKEN_LIMIT")
        self.assertIsNone(result.prompt_guidance)


if __name__ == "__main__":
    unittest.main()
