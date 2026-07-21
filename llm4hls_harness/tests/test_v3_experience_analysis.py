from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import TokenBudgetPolicy
from llm4hls_agent.v3_experience_analysis import (
    PublicRunArtifactResolver,
    build_coverage_matrix,
    build_data_quality,
    coverage_csv,
    derive_v2_backfill,
    load_public_corpus,
    plan_gap_driven_collection,
)
from llm4hls_agent.v3_experience_guidance import ExperienceFeatureExtractor
from llm4hls_agent.v3_experience_v2 import seal_experience_v2

from .test_v3_experience_v2_schema import sample_body


def v1_record(index: int = 1) -> dict[str, object]:
    return ExperienceFeatureExtractor().build_record(
        run_id=f"migration-run-{index}",
        candidate_id=f"candidate_{index:03d}",
        task_id=f"public-task-{index}",
        task_split="train",
        mode="OPTIMIZE",
        source="void kernel(float a[32], float *o){float acc=0; for(int i=0;i<32;i++)acc+=a[i];}",
        patch="--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS UNROLL factor=4\n",
        execution_class="REAL_LLM_VITIS",
        eligible_for_ranking=True,
        artifact_refs=[
            {
                "role": "candidate_patch",
                "ref": f"candidates/candidate_{index:03d}/patch.diff",
                "sha256": f"{index + 500:064x}",
            }
        ],
        strategy_bundle=["LOOP_UNROLL"],
        outcome={
            "patch_valid": True,
            "candidate_created": True,
            "csim_pass": True,
            "synth_pass": True,
            "cosim_status": "PASS",
            "final_pass": True,
            "promoted": True,
            "failure_stage": None,
            "latency_before": 100,
            "latency_after": 25,
            "acceleration": 4.0,
            "tokens": 1200,
            "credits": 30,
            "wall_time_seconds": 10.0,
        },
    )


def v2_record(index: int, *, success: bool = True, family: str | None = None):
    body = copy.deepcopy(sample_body())
    body["source"].update(
        {
            "run_id": f"coverage-run-{index}",
            "candidate_id": f"coverage-candidate-{index}",
            "round_index": index,
            "task_family_hash": family or f"{index + 10:064x}",
        }
    )
    body["strategy"].update(
        {
            "patch_digest": f"{index + 100:064x}",
            "observed_strategy_atoms": ["LOOP_UNROLL"],
            "declared_strategy_bundle": ["LOOP_UNROLL"],
        }
    )
    body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
    if not success:
        body["validation"].update(
            {"fresh_final_status": "FAIL", "promoted": False, "rejected": True}
        )
        body["performance"].update(
            {"strict_improvement": False, "acceleration": 1.0, "latency_after": 1027.0}
        )
    return seal_experience_v2(body)


class ExperienceAnalysisTests(unittest.TestCase):
    def test_backfill_is_immutable_and_keeps_task_only_in_audit_sidecar(self) -> None:
        source = [v1_record(1), v1_record(2)]
        before = copy.deepcopy(source)
        result = derive_v2_backfill(source)
        self.assertEqual(source, before)
        self.assertEqual(result["migrated_count"], 2)
        self.assertEqual(result["quarantine_count"], 0)
        self.assertEqual(len(result["audit_groups"]), 2)
        rendered_records = json.dumps(result["records"], sort_keys=True)
        self.assertNotIn("task_id", rendered_records)
        self.assertNotIn("public-task", rendered_records)

    def test_public_artifact_resolver_uses_patch_hash_without_exporting_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_path = root / "run" / "candidates" / "candidate_001" / "patch.diff"
            patch_path.parent.mkdir(parents=True)
            patch = "--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS UNROLL factor=4\n"
            patch_path.write_text(patch, encoding="utf-8")
            digest = __import__("hashlib").sha256(patch.encode()).hexdigest()
            source = v1_record()
            source["artifact_refs"] = [
                {
                    "role": "candidate_patch",
                    "ref": "candidates/candidate_001/patch.diff",
                    "sha256": digest,
                }
            ]
            # Re-seal through the canonical builder because v1 record IDs bind content.
            source = ExperienceFeatureExtractor().build_record(
                run_id="artifact-run",
                candidate_id="candidate_001",
                task_id="public-task",
                task_split="train",
                mode="OPTIMIZE",
                source="void kernel(float a[32]){}",
                patch=patch,
                execution_class="REAL_LLM_VITIS",
                eligible_for_ranking=True,
                artifact_refs=source["artifact_refs"],
                strategy_bundle=["LOOP_UNROLL"],
                outcome=source["outcome"],
            )
            result = derive_v2_backfill(
                [source], resolver=PublicRunArtifactResolver([root])
            )
            self.assertEqual(result["artifact_context_matches"], 1)
            rendered = json.dumps(result["records"])
            self.assertNotIn("#pragma", rendered)
            self.assertIn("LOOP_UNROLL", rendered)

    def test_public_artifact_resolver_carries_real_token_envelope_into_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            proposal_path = run / "planner" / "proposal_001.json"
            request_path = run / "planner" / "requests" / "request.json"
            outcome_path = run / "planner" / "live_outcomes" / "action-001.json"
            started_path = (
                run
                / "control"
                / "live_planner_actions"
                / "action-001.started.json"
            )
            for path in (proposal_path, request_path, outcome_path, started_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            (run / "v3_run_config.json").write_text(
                json.dumps({"tool": {"toolchain_id": "Vitis 2025.2"}}),
                encoding="utf-8",
            )
            patch = "--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS UNROLL factor=4\n"
            proposal = {
                "round_index": 1,
                "patch": patch,
                "provider": "openai-compatible",
                "model": "public-model",
                "input_tokens": 900,
                "output_tokens": 180,
                "finish_reason": "stop",
                "output_truncated": False,
                "truncation_reason": None,
            }
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            envelope = TokenBudgetPolicy().allocate(
                budget_snapshot={
                    "run_token_limit": 12000,
                    "tokens_used": 2000,
                    "tokens_remaining": 10000,
                },
                mode="OPTIMIZE",
                estimated_base_prompt_tokens=1600,
                estimated_guidance_tokens=120,
                estimated_input_tokens=1720,
                rounds_remaining=2,
            ).to_dict()
            request_path.write_text(
                json.dumps({"request": {"token_envelope": envelope}}),
                encoding="utf-8",
            )
            outcome_path.write_text(
                json.dumps({"proposal": proposal}), encoding="utf-8"
            )
            started_path.write_text(
                json.dumps(
                    {"request": {"request_ref": "planner/requests/request.json"}}
                ),
                encoding="utf-8",
            )
            (run / "v3_prototype_result.json").write_text(
                json.dumps(
                    {
                        "token_policy_rounds": [
                            {"round": 1, "action_id": "action-001"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            digest = __import__("hashlib").sha256(proposal_path.read_bytes()).hexdigest()
            source = ExperienceFeatureExtractor().build_record(
                run_id="token-run",
                candidate_id="candidate_001",
                task_id="public-task",
                task_split="train",
                mode="OPTIMIZE",
                source="void kernel(float a[32]){}",
                patch=patch,
                execution_class="REAL_LLM_VITIS",
                eligible_for_ranking=True,
                artifact_refs=[
                    {
                        "role": "planner_proposal",
                        "ref": "planner/proposal_001.json",
                        "sha256": digest,
                    }
                ],
                strategy_bundle=["LOOP_UNROLL"],
                outcome=v1_record()["outcome"],
            )
            result = derive_v2_backfill(
                [source], resolver=PublicRunArtifactResolver([root])
            )
            token_policy = result["records"][0]["token_policy"]
            self.assertEqual(token_policy["estimated_input_tokens"], 1720)
            self.assertEqual(token_policy["actual_total_tokens"], 1080)
            self.assertEqual(token_policy["guidance_actual_tokens"], 120)

    def test_data_quality_and_coverage_are_deterministic(self) -> None:
        records = [v2_record(1), v2_record(2, success=False)]
        quality = build_data_quality(records, source_count=3, quarantine_count=1)
        self.assertEqual(quality["source_record_count"], 3)
        self.assertEqual(quality["ranking_eligible_count"], 2)
        self.assertEqual(
            quality["outcome_distribution"],
            {"NO_IMPROVEMENT": 1, "SUCCESS": 1},
        )
        first = build_coverage_matrix(records)
        second = build_coverage_matrix(list(reversed(records)))
        self.assertEqual(first, second)
        self.assertTrue(first["strategy_cells"][0]["has_positive_and_negative"])
        self.assertIn("mode,subtype,strategies", coverage_csv(first))
        self.assertTrue(any(gap["key"] == "REPAIR" for gap in first["gaps"]))

    def test_gap_queue_prioritizes_missing_modes_and_respects_family_quota(self) -> None:
        coverage = build_coverage_matrix([v2_record(1), v2_record(2)])
        corpus = [
            {
                "task": "public-a",
                "relative_path": "tasks/a",
                "task_split": "train",
                "mode": "REPAIR",
                "subtype": "OFF_BY_ONE",
                "algorithm_family": "VECTOR_ELEMENTWISE",
                "public_family": "map",
                "difficulty": 1,
                "requires_cosim": False,
            },
            {
                "task": "public-b",
                "relative_path": "tasks/b",
                "task_split": "dev",
                "mode": "STRUCTURAL_FIX",
                "subtype": "FIFO_DEPTH_INSUFFICIENT",
                "algorithm_family": "STREAM_PIPELINE",
                "public_family": "stream",
                "difficulty": 4,
                "requires_cosim": True,
            },
            {
                "task": "hidden-c",
                "relative_path": "tasks/c",
                "task_split": "hidden_like",
                "mode": "REPAIR",
                "subtype": "WRONG_SIGN",
                "algorithm_family": "VECTOR_ELEMENTWISE",
                "public_family": "map",
                "difficulty": 1,
                "requires_cosim": False,
            },
        ]
        first = plan_gap_driven_collection(coverage, corpus, available_credit=400)
        second = plan_gap_driven_collection(coverage, corpus, available_credit=400)
        self.assertEqual(first, second)
        self.assertFalse(first["hidden_like_external_collection_allowed"])
        self.assertNotIn("hidden-c", {item["task"] for item in first["items"]})
        family_counts = {}
        for item in first["items"]:
            family_counts[item["public_family"]] = family_counts.get(item["public_family"], 0) + item["recommended_attempts"]
        self.assertLessEqual(max(family_counts.values()), first["family_attempt_quota"])
        self.assertTrue(all(item["attempts_completed"] == 0 for item in first["items"]))

    def test_unavailable_collection_produces_explicit_blocked_queue(self) -> None:
        coverage = build_coverage_matrix([])
        corpus = [
            {
                "task": "public-a",
                "relative_path": "tasks/a",
                "task_split": "train",
                "mode": "REPAIR",
                "subtype": "OFF_BY_ONE",
                "algorithm_family": "VECTOR_ELEMENTWISE",
                "public_family": "map",
                "difficulty": 1,
                "requires_cosim": False,
            }
        ]
        queue = plan_gap_driven_collection(
            coverage, corpus, provider_available=False, vitis_available=True
        )
        self.assertEqual(queue["planned_candidate_count"], 0)
        self.assertEqual(queue["items"][0]["status"], "BLOCKED_EXTERNAL_AVAILABILITY")

    def test_public_corpus_loader_refuses_hidden_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks" / "public"
            tasks.mkdir(parents=True)
            manifest = root / "corpus_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "public-1",
                                "path": "tasks/public",
                                "mode": "REPAIR",
                                "family": "map",
                                "difficulty": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            inventory = load_public_corpus(manifest)
            self.assertEqual(inventory[0]["mode"], "REPAIR")
            self.assertEqual(inventory[0]["algorithm_family"], "VECTOR_ELEMENTWISE")
            hidden_root = root / "hidden_like"
            hidden_root.mkdir()
            hidden_manifest = hidden_root / "corpus_manifest.json"
            hidden_manifest.write_text('{"tasks": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "public"):
                load_public_corpus(hidden_manifest)


if __name__ == "__main__":
    unittest.main()
