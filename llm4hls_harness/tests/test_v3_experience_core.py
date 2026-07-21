from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import (
    EXPERIENCE_SCHEMA,
    ExperienceValidationError,
    estimated_guidance_tokens,
    numeric_bucket,
    validate_experience_record,
    validate_guidance,
)
from llm4hls_agent.v3_experience_guidance import (
    BayesianStrategyRanker,
    EmpiricalRiskAdvisor,
    ExperienceCoordinator,
    ExperienceFeatureExtractor,
    HeuristicContinueAdvisor,
    WeightedKNNRetriever,
)
from llm4hls_agent.v3_experience_store import JsonlExperienceRepository


def _outcome(*, success: bool, before: int = 100, after: int = 50) -> dict[str, object]:
    return {
        "patch_valid": True,
        "candidate_created": True,
        "csim_pass": success,
        "synth_pass": success,
        "cosim_status": "SKIPPED" if success else "NOT_RUN",
        "final_pass": None,
        "promoted": success,
        "failure_stage": None if success else "CSIM",
        "latency_before": before,
        "latency_after": after if success else None,
        "acceleration": before / after if success else None,
        "tokens": 240,
        "credits": 5,
        "wall_time_seconds": 4.0,
    }


class ExperienceCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = ExperienceFeatureExtractor()
        self.dot_source = """
        void top(float a[1024], float b[1024], float *out) {
          float acc = 0;
          for (int i=0; i<1024; ++i) acc += a[i] * b[i];
          *out = acc;
        }
        """

    def record(
        self,
        index: int,
        *,
        mode: str = "OPTIMIZE",
        split: str = "train",
        strategy: tuple[str, ...] = ("LOOP_UNROLL",),
        success: bool = True,
        task_id: str | None = None,
    ) -> dict[str, object]:
        return self.extractor.build_record(
            run_id=f"run-{index}",
            candidate_id=f"cand-{index}",
            task_id=task_id or f"task-{index}",
            task_split=split,
            mode=mode,
            source=self.dot_source,
            description="Compute a dot product reduction.",
            patch=f"--- a/kernel.cpp\n+++ b/kernel.cpp\n+// strategy {index}\n",
            execution_class="REAL_LLM_VITIS",
            eligible_for_ranking=True,
            artifact_refs=[
                {
                    "role": "tool-result",
                    "ref": f"artifacts/candidate-{index}/result.json",
                    "sha256": f"{index:064x}",
                }
            ],
            strategy_bundle=list(strategy),
            outcome=_outcome(success=success),
        )

    def query(
        self,
        *,
        split: str = "train",
        task_id: str = "query-task",
        run_id: str = "query-run",
    ) -> dict[str, object]:
        return self.extractor.build_query(
            mode="OPTIMIZE",
            source=self.dot_source,
            description="Compute a dot product reduction.",
            task_id=task_id,
            task_split=split,
            run_id=run_id,
            candidate_id="query-candidate",
            remaining_credits=30,
            remaining_tokens=4000,
        )

    def test_schema_feature_buckets_and_structural_extraction(self) -> None:
        self.assertEqual(EXPERIENCE_SCHEMA, "v3e.experience.v1")
        self.assertEqual(numeric_bucket(1024), "257-1024")
        self.assertEqual(numeric_bucket(1025), "1024+")
        features = self.extractor.evidence_features(
            source="#pragma HLS DATAFLOW\nhls::stream<int> fifo; ap_uint<16> x;",
            synth_evidence={
                "top_level": {
                    "latency": {"max": 1027},
                    "transaction_interval": {"max": 1025},
                    "utilization_percent": {"LUT": 83.0},
                },
                "loops": [
                    {
                        "latency_cycles": 1024,
                        "pipeline_ii": 1,
                        "trip_count": 1024,
                    }
                ],
                "observations": [{"kind": "MEMORY_SCHEDULING_CONSTRAINT"}],
            },
        )
        self.assertEqual(features["loop_ii"], 1)
        self.assertEqual(features["transaction_interval_bucket"], "1024+")
        self.assertEqual(features["resource_pressure"], "HIGH")
        self.assertTrue(features["has_dataflow"])
        self.assertTrue(features["has_stream"])
        self.assertTrue(features["has_fifo"])
        self.assertTrue(features["has_bitwidth"])
        self.assertTrue(features["memory_bottleneck"])

    def test_schema_rejects_fixture_as_ranking_and_unsafe_artifact(self) -> None:
        value = self.record(1)
        value["execution_class"] = "DETERMINISTIC_FIXTURE"
        with self.assertRaises(ExperienceValidationError):
            validate_experience_record(value)
        for reference in (
            "hidden_tb/result.json",
            "golden_kernel.cpp",
            "reference_solution/result.json",
            "hidden-like/test.json",
        ):
            value = self.record(3)
            value["artifact_refs"] = [
                {"role": "tool-result", "ref": reference, "sha256": "b" * 64}
            ]
            with self.assertRaises(ExperienceValidationError):
                validate_experience_record(value)
        value = self.record(2)
        value["artifact_refs"] = [
            {"role": "result", "ref": "/tmp/private/result.json", "sha256": "a" * 64}
        ]
        with self.assertRaises(ExperienceValidationError):
            validate_experience_record(value)

    def test_task_id_never_changes_similarity_or_ranking(self) -> None:
        records = [
            self.record(1, strategy=("LOOP_UNROLL",)),
            self.record(2, strategy=("ARRAY_PARTITION",), success=False),
        ]
        first = self.query(task_id="public-alpha", run_id="query-a")
        second = self.query(task_id="completely-different", run_id="query-b")
        retriever = WeightedKNNRetriever()
        first_result = retriever.retrieve(first, records)
        second_result = retriever.retrieve(second, records)
        self.assertEqual(
            [item["record_id"] for item in first_result.considered],
            [item["record_id"] for item in second_result.considered],
        )
        self.assertEqual(
            [item["_similarity"] for item in first_result.considered],
            [item["_similarity"] for item in second_result.considered],
        )

    def test_hidden_like_retrieves_train_only_and_same_mode(self) -> None:
        records = [
            self.record(1, split="train"),
            self.record(2, split="dev"),
            self.record(3, split="hidden_like"),
            self.record(4, mode="REPAIR"),
        ]
        result = WeightedKNNRetriever().retrieve(
            self.query(split="hidden_like"), records
        )
        self.assertEqual([item["run_id"] for item in result.considered], ["run-1"])

    def test_unknown_split_also_fails_closed_to_train_only(self) -> None:
        records = [
            self.record(1, split="train"),
            self.record(2, split="dev"),
            self.record(3, split="hidden_like"),
        ]
        result = WeightedKNNRetriever().retrieve(
            self.query(split="unknown"), records
        )
        self.assertEqual([item["run_id"] for item in result.considered], ["run-1"])

    def test_guidance_rejects_extra_answer_bearing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonlExperienceRepository(Path(directory) / "experience.jsonl")
            repository.put_if_absent(self.record(1))
            guidance = ExperienceCoordinator(repository).build_guidance(
                mode="OPTIMIZE",
                source=self.dot_source,
                description="Compute a dot product reduction.",
                current_run_id="query-run",
                remaining_tokens=100,
            )
        guidance["recommended_strategy_bundles"][0]["golden_patch"] = (
            "/home/user/private.cpp"
        )
        with self.assertRaises(ExperienceValidationError):
            validate_guidance(guidance)

    def test_knn_order_is_deterministic_and_deduplicates_runs(self) -> None:
        duplicate_run = self.record(2)
        duplicate_run["run_id"] = "run-1"
        # Rebuild IDs because the schema requires content-bound IDs only by convention,
        # while retrieval must still deduplicate opaque run identities.
        records = [self.record(3), duplicate_run, self.record(1)]
        retriever = WeightedKNNRetriever()
        left = retriever.retrieve(self.query(), records)
        right = retriever.retrieve(self.query(), list(reversed(records)))
        self.assertEqual(
            [item["record_id"] for item in left.considered],
            [item["record_id"] for item in right.considered],
        )
        self.assertEqual(sum(item["run_id"] == "run-1" for item in left.considered), 1)

    def test_beta_smoothing_and_mode_specific_rank(self) -> None:
        ranker = BayesianStrategyRanker()
        self.assertEqual(ranker.posterior_success(0, 0), 0.5)
        records = [
            self.record(1, strategy=("LOOP_UNROLL",), success=True),
            self.record(2, strategy=("LOOP_UNROLL",), success=False),
            self.record(3, strategy=("ARRAY_PARTITION",), success=True),
            self.record(4, mode="REPAIR", strategy=("OTHER",), success=True),
        ]
        ranked = ranker.rank(self.query(), records)
        bundles = [
            item["strategy_bundle"]
            for item in ranked["recommended_strategy_bundles"]
        ]
        self.assertIn(["ARRAY_PARTITION"], bundles)
        self.assertNotIn(["OTHER"], bundles)
        self.assertLessEqual(len(bundles), 3)

    def test_risk_and_continue_are_advisory(self) -> None:
        records = [self.record(1), self.record(2, success=False)]
        query = self.query()
        risk = EmpiricalRiskAdvisor().advise(query, records)
        self.assertIn("Advisory only", risk["notice"])
        ranking = BayesianStrategyRanker().rank(query, records)
        decision = HeuristicContinueAdvisor().advise(query, records, ranking)
        self.assertIn(decision["decision"], {"CONTINUE", "STOP_ADVISORY"})
        self.assertIn("Advisory only", decision["notice"])

    def test_empty_coordinator_falls_back_and_persistence_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonlExperienceRepository(Path(directory) / "experience.jsonl")
            recommendations = Path(directory) / "recommendations.jsonl"
            coordinator = ExperienceCoordinator(
                repository, recommendation_path=recommendations
            )
            guidance = coordinator.build_guidance(
                mode="OPTIMIZE",
                source=self.dot_source,
                current_run_id="query-run",
                remaining_tokens=100,
            )
            self.assertEqual(guidance["fallback_reason"], "NO_ELIGIBLE_EXPERIENCE")
            self.assertFalse(coordinator._last_result.actionable)  # bounded result API
            self.assertLessEqual(estimated_guidance_tokens(guidance), 1100)
            first = coordinator.persist_recommendation(0, guidance)
            second = coordinator.persist_recommendation(0, guidance)
            self.assertTrue(first["persisted"])
            self.assertFalse(second["persisted"])
            self.assertEqual(len(recommendations.read_text().splitlines()), 1)

    def test_guidance_is_bounded_and_contains_no_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonlExperienceRepository(Path(directory) / "experience.jsonl")
            for index in range(1, 8):
                repository.put_if_absent(
                    self.record(
                        index,
                        strategy=("LOOP_UNROLL",) if index % 2 else ("ARRAY_PARTITION",),
                        success=index % 3 != 0,
                    )
                )
            coordinator = ExperienceCoordinator(repository, max_guidance_tokens=1100)
            guidance = coordinator.build_guidance(
                mode="OPTIMIZE",
                source=self.dot_source,
                description="Compute a dot product reduction.",
                task_id="sensitive-public-task-name",
                current_run_id="query-run",
                remaining_tokens=100,
            )
            rendered = json.dumps(guidance, sort_keys=True)
            self.assertLessEqual(estimated_guidance_tokens(guidance), 1100)
            self.assertNotIn("sensitive-public-task-name", rendered)
            self.assertIn("recommended_strategy_bundles", guidance)

    def test_coordinator_freezes_seed_snapshot_at_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonlExperienceRepository(Path(directory) / "experience.jsonl")
            repository.put_if_absent(self.record(1))
            coordinator = ExperienceCoordinator(repository)
            frozen = coordinator.fingerprint()
            repository.put_if_absent(self.record(2))
            guidance = coordinator.build_guidance(
                mode="OPTIMIZE",
                source=self.dot_source,
                description="Compute a dot product reduction.",
                current_run_id="query-run",
                remaining_tokens=100,
            )
            self.assertEqual(coordinator.fingerprint(), frozen)
            self.assertEqual(coordinator.snapshot_metadata()["record_count"], 1)
            supporting = {
                item["record_id"] for item in guidance["similar_successes"]
            }
            self.assertNotIn(self.record(2)["record_id"], supporting)


if __name__ == "__main__":
    unittest.main()
