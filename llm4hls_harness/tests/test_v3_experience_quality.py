from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import estimated_guidance_tokens
from llm4hls_agent.v3_experience_attribution import (
    _adherence,
    build_recommendation_attributions,
    persist_recommendation_attributions,
)
from llm4hls_agent.v3_experience_guidance import (
    BayesianStrategyRanker,
    ExperienceCoordinator,
    ExperienceFeatureExtractor,
    WeightedKNNRetriever,
)
from llm4hls_agent.v3_experience_loro import evaluate_leave_one_run_out
from llm4hls_agent.v3_experience_quality import (
    GuidanceQualityConfig,
    GuidanceQualityGate,
)
from llm4hls_agent.v3_experience_store import JsonlExperienceRepository


DOT_SOURCE = """
void top(float a[1024], float b[1024], float *out) {
  float acc = 0;
  for (int i=0; i<1024; ++i) acc += a[i] * b[i];
  *out = acc;
}
"""


def outcome(*, success: bool) -> dict[str, object]:
    return {
        "patch_valid": True,
        "candidate_created": True,
        "csim_pass": True if success else False,
        "synth_pass": True if success else None,
        "cosim_status": "SKIPPED" if success else "NOT_RUN",
        "final_pass": True if success else False,
        "promoted": success,
        "failure_stage": None if success else "CSIM",
        "latency_before": 100,
        "latency_after": 50 if success else None,
        "acceleration": 2.0 if success else None,
        "tokens": 300,
        "credits": 5,
        "wall_time_seconds": 3.0,
    }


class GuidanceQualityTests(unittest.TestCase):
    def test_attribution_covers_all_four_adherence_relations(self) -> None:
        recommended = {("ARRAY_PARTITION", "LOOP_UNROLL")}
        discouraged = {("DATAFLOW",)}
        self.assertEqual(
            _adherence(
                ("ARRAY_PARTITION", "LOOP_UNROLL"), recommended, discouraged
            ),
            "FOLLOWED",
        )
        self.assertEqual(
            _adherence(("ARRAY_PARTITION",), recommended, discouraged),
            "PARTIALLY_FOLLOWED",
        )
        self.assertEqual(
            _adherence(("LOOP_PIPELINE",), recommended, discouraged),
            "IGNORED",
        )
        self.assertEqual(
            _adherence(("DATAFLOW",), recommended, discouraged),
            "CONTRADICTED",
        )

    def setUp(self) -> None:
        self.extractor = ExperienceFeatureExtractor()

    def record(
        self,
        index: int,
        *,
        strategy: tuple[str, ...] = ("ARRAY_PARTITION",),
        success: bool = True,
        split: str = "train",
        mode: str = "OPTIMIZE",
        bottleneck: str = "MEMORY_SCHEDULING",
    ) -> dict[str, object]:
        return self.extractor.build_record(
            run_id=f"quality-run-{index}",
            candidate_id=f"candidate-{index}",
            task_id=f"task-{index}",
            task_split=split,
            mode=mode,
            source=DOT_SOURCE,
            description="A public reduction kernel.",
            patch=f"--- a/kernel.cpp\n+++ b/kernel.cpp\n+// {index}\n",
            failure_evidence={"primary_bottleneck": bottleneck},
            execution_class="REAL_LLM_VITIS",
            eligible_for_ranking=True,
            artifact_refs=[
                {
                    "role": "tool-result",
                    "ref": f"artifacts/{index}/result.json",
                    "sha256": f"{index:064x}",
                }
            ],
            strategy_bundle=list(strategy),
            outcome=outcome(success=success),
        )

    def query(
        self,
        *,
        task_id: str = "query-task",
        split: str = "hidden_like",
        attempted: tuple[tuple[str, ...], ...] = (),
        bottleneck: str = "MEMORY_SCHEDULING",
    ) -> dict[str, object]:
        return self.extractor.build_query(
            mode="OPTIMIZE",
            source=DOT_SOURCE,
            description="A public reduction kernel.",
            failure_evidence={"primary_bottleneck": bottleneck},
            task_id=task_id,
            task_split=split,
            run_id="quality-query",
            candidate_id="incumbent",
            attempted_strategy_bundles=attempted,
            remaining_credits=30,
            remaining_tokens=4000,
        )

    def quality(self, query, records):
        retrieval = WeightedKNNRetriever().retrieve(query, records)
        ranking = BayesianStrategyRanker().rank(query, retrieval.considered)
        return GuidanceQualityGate().evaluate(query, retrieval, ranking)

    def test_inject_requires_two_matching_real_supports(self) -> None:
        result = self.quality(self.query(), [self.record(1), self.record(2)])
        self.assertEqual(result.decision["decision"], "INJECT")
        self.assertGreaterEqual(result.decision["support_count"], 2)
        self.assertEqual(
            result.decision["recommended_strategies"][0], ["ARRAY_PARTITION"]
        )
        self.assertLessEqual(estimated_guidance_tokens(result.prompt_guidance), 600)

    def test_insufficient_data_abstains_and_fails_open(self) -> None:
        result = self.quality(self.query(), [self.record(1)])
        self.assertEqual(result.decision["decision"], "ABSTAIN")
        self.assertEqual(result.decision["abstain_reason"], "INSUFFICIENT_SUPPORT")
        self.assertEqual(result.decision["guidance_tokens"], 0)
        self.assertEqual(result.prompt_guidance["recommended_strategy_bundles"], [])

    def test_evidence_conflict_abstains(self) -> None:
        records = [
            self.record(1, strategy=("LOOP_PIPELINE",), bottleneck="LOOP_PIPELINE_II"),
            self.record(2, strategy=("LOOP_PIPELINE",), bottleneck="LOOP_PIPELINE_II"),
        ]
        query = self.extractor.build_query(
            mode="OPTIMIZE",
            source=DOT_SOURCE,
            task_id="query",
            task_split="hidden_like",
            run_id="quality-query",
            synth_evidence={
                "primary_bottleneck": "LOOP_PIPELINE_II",
                "loops": [{"pipeline_ii": 1, "trip_count": 1024}],
            },
            remaining_tokens=4000,
        )
        result = self.quality(query, records)
        self.assertEqual(result.decision["decision"], "ABSTAIN")
        self.assertEqual(
            result.decision["abstain_reason"], "PIPELINE_ALREADY_ACHIEVED_II_1"
        )

    def test_attempted_failed_strategy_is_not_recommended(self) -> None:
        result = self.quality(
            self.query(attempted=(("ARRAY_PARTITION",),)),
            [self.record(1), self.record(2)],
        )
        self.assertEqual(result.decision["decision"], "ABSTAIN")
        self.assertNotIn(
            ["ARRAY_PARTITION"], result.decision["recommended_strategies"]
        )

    def test_hidden_split_reads_train_only(self) -> None:
        query = self.query(split="hidden_like")
        records = [
            self.record(1, split="train"),
            self.record(2, split="train"),
            self.record(3, split="hidden_like"),
        ]
        retrieval = WeightedKNNRetriever().retrieve(query, records)
        self.assertEqual(
            {item["task_split"] for item in retrieval.considered}, {"train"}
        )

    def test_fixture_and_oracle_records_cannot_support_injection(self) -> None:
        records = [self.record(1), self.record(2)]
        for index, execution_class in enumerate(
            ("DETERMINISTIC_FIXTURE", "DEMO_FIXTURE", "ORACLE_FIXTURE"), start=3
        ):
            records.append(
                self.extractor.build_record(
                    run_id=f"fixture-run-{index}",
                    candidate_id=f"fixture-candidate-{index}",
                    task_id=f"fixture-task-{index}",
                    task_split="train",
                    mode="OPTIMIZE",
                    source=DOT_SOURCE,
                    patch=f"--- a/kernel.cpp\n+++ b/kernel.cpp\n+// fixture {index}\n",
                    failure_evidence={"primary_bottleneck": "MEMORY_SCHEDULING"},
                    execution_class=execution_class,
                    eligible_for_ranking=False,
                    strategy_bundle=["ARRAY_PARTITION"],
                    outcome=outcome(success=True),
                )
            )
        result = self.quality(self.query(), records)
        self.assertEqual(result.decision["decision"], "INJECT")
        self.assertEqual(result.decision["support_count"], 2)

    def test_task_id_does_not_change_quality_decision(self) -> None:
        records = [self.record(1), self.record(2)]
        left = self.quality(self.query(task_id="alpha"), records)
        right = self.quality(self.query(task_id="unrelated-beta"), records)
        left_value = dict(left.decision)
        right_value = dict(right.decision)
        self.assertEqual(left_value, right_value)

    def test_coordinator_uses_600_token_gate_and_persists_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = JsonlExperienceRepository(root / "store.jsonl")
            repository.put_if_absent(self.record(1))
            repository.put_if_absent(self.record(2))
            coordinator = ExperienceCoordinator(
                repository,
                recommendation_path=root / "recommendations.jsonl",
            )
            guidance = coordinator.build_guidance(
                mode="OPTIMIZE",
                source=DOT_SOURCE,
                failure_evidence={"primary_bottleneck": "MEMORY_SCHEDULING"},
                task_split="hidden_like",
                current_run_id="quality-query",
                remaining_tokens=4000,
            )
            self.assertLessEqual(estimated_guidance_tokens(guidance), 600)
            coordinator.persist_recommendation(1, guidance)
            saved = json.loads((root / "recommendations.jsonl").read_text())
            self.assertEqual(saved["quality_decision"]["decision"], "INJECT")
            self.assertLessEqual(saved["quality_decision"]["guidance_tokens"], 600)

    def test_dynamic_cap_below_minimum_abstains_instead_of_blocking_planner(self) -> None:
        query = self.query()
        records = [self.record(1), self.record(2)]
        retrieval = WeightedKNNRetriever().retrieve(query, records)
        ranking = BayesianStrategyRanker().rank(query, retrieval.considered)
        result = GuidanceQualityGate().evaluate(
            query,
            retrieval,
            ranking,
            prompt_token_limit=120,
        )
        self.assertEqual(result.decision["decision"], "ABSTAIN")
        self.assertEqual(result.decision["abstain_reason"], "PROMPT_TOKEN_LIMIT")
        self.assertEqual(result.decision["guidance_tokens"], 0)

    def test_loro_reports_required_metrics_without_task_identity(self) -> None:
        records = [
            self.record(1),
            self.record(2),
            self.record(3),
            self.record(4, strategy=("LOOP_UNROLL",), success=False),
        ]
        report = evaluate_leave_one_run_out(records)
        for key in (
            "coverage",
            "success_strategy_hit_rate",
            "harmful_recommendation_rate",
            "abstain_rate",
            "duplicate_failure_suppression_rate",
            "average_guidance_tokens",
        ):
            self.assertIn(key, report["overall"])
        rendered = json.dumps(report, sort_keys=True)
        self.assertTrue(report["leakage_checks"]["task_id_excluded"])
        self.assertTrue(
            all("task_id_hash" not in row for row in report["rows"])
        )
        self.assertNotIn("golden", json.dumps(report["rows"]).casefold())

    def test_attribution_is_checkpoint_replay_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            experience = root / "experience"
            planner = root / "planner"
            experience.mkdir()
            planner.mkdir()
            recommendation = {
                "schema_version": "v3e.recommendation.v2",
                "recommendation_id": "a" * 64,
                "round_index": 1,
                "quality_decision": {
                    "decision": "INJECT",
                    "recommended_strategies": [["ARRAY_PARTITION"]],
                    "discouraged_strategies": [["LOOP_PIPELINE"]],
                },
                "guidance": {},
            }
            (experience / "experience_recommendations.jsonl").write_text(
                json.dumps(recommendation) + "\n", encoding="utf-8"
            )
            (planner / "proposal_001.json").write_text(
                json.dumps(
                    {
                        "change_class": "ARRAY_PARTITION+LOOP_UNROLL",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "duration_seconds": 1.5,
                    }
                ),
                encoding="utf-8",
            )
            result = {
                "run_id": "attribution-run",
                "final_candidate_id": "candidate_001",
                "candidate_rounds": [
                    {
                        "round": 1,
                        "candidate_id": "candidate_001",
                        "strategy_bundle": ["ARRAY_PARTITION", "LOOP_UNROLL"],
                        "decision": "FINAL_VERIFIED",
                        "decision_reason": "FINAL_CLOSURE_PASS",
                        "credits": 5,
                        "acceleration_vs_baseline": 2.0,
                    }
                ],
                "final_validation": {
                    stage: {"status": "PASS"}
                    for stage in ("csim", "synth", "cosim")
                },
            }
            (root / "candidate_registry.json").write_text(
                json.dumps(
                    {
                        "candidates": {
                            "candidate_001": {
                                "status": "FINAL_VERIFIED",
                                "validation": {
                                    stage: {"status": "PASS"}
                                    for stage in ("csim", "synth", "cosim")
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            first = persist_recommendation_attributions(root, result)
            second = persist_recommendation_attributions(root, result)
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(
                len((experience / "recommendation_attributions.jsonl").read_text().splitlines()),
                1,
            )
            attribution = build_recommendation_attributions(root, result)[0]
            self.assertEqual(attribution["adherence"], "PARTIALLY_FOLLOWED")
            self.assertEqual(
                attribution["declared_strategy_relation"],
                "PARTIALLY_FOLLOWED",
            )
            self.assertEqual(
                attribution["observed_strategy_relation"],
                "PARTIALLY_FOLLOWED",
            )
            self.assertFalse(attribution["causal_claim"])
            self.assertIn("guidance_tokens", attribution["usage"])
            self.assertEqual(attribution["validation"]["final"], "PASS")

            recommendation["recommendation_id"] = "b" * 64
            recommendation["quality_decision"]["decision"] = "ABSTAIN"
            (experience / "experience_recommendations.jsonl").write_text(
                json.dumps(recommendation) + "\n", encoding="utf-8"
            )
            abstained = build_recommendation_attributions(root, result)[0]
            self.assertEqual(abstained["adherence"], "IGNORED")


if __name__ == "__main__":
    unittest.main()
