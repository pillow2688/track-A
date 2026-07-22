from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.token_policy_hybrid_aggregate import aggregate
from llm4hls_agent.token_policy_hybrid_runner import (
    CONDITIONS,
    MODES,
    RUN_SCHEMA,
    _condition_args,
    load_matrix,
    run_matrix,
)


def matrix_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "token_policy_hybrid_v2"
        / "token_policy_hybrid_matrix.json"
    )


class TokenPolicyHybridMatrixTests(unittest.TestCase):
    def test_public_schedule_is_balanced_and_fresh(self) -> None:
        matrix = load_matrix(matrix_path())
        self.assertEqual(len(matrix.jobs), 48)
        self.assertEqual(
            {
                condition: sum(job.condition == condition for job in matrix.jobs)
                for condition in CONDITIONS
            },
            {"A_FIXED": 24, "D_HYBRID": 24},
        )
        self.assertEqual(
            {
                mode: len({job.task_id for job in matrix.jobs if job.mode == mode})
                for mode in MODES
            },
            {mode: 3 for mode in MODES},
        )
        self.assertEqual(len({job.run_id for job in matrix.jobs}), 48)
        self.assertEqual(len({job.run_fingerprint for job in matrix.jobs}), 48)
        for offset in range(0, 48, 2):
            self.assertEqual(
                {job.condition for job in matrix.jobs[offset : offset + 2]},
                set(CONDITIONS),
            )

    def test_conditions_only_select_fixed_or_hybrid_policy(self) -> None:
        matrix = load_matrix(matrix_path())
        args = {
            condition: " ".join(_condition_args(matrix, condition))
            for condition in CONDITIONS
        }
        self.assertIn("--token-budget-policy fixed", args["A_FIXED"])
        self.assertNotIn("--token-policy-config", args["A_FIXED"])
        self.assertIn("--token-budget-policy hybrid", args["D_HYBRID"])
        self.assertIn("--token-policy-config", args["D_HYBRID"])
        for flag in (
            "--credit-limit 100",
            "--run-token-limit 12000",
            "--llm-temperature 0.0",
            "--llm-top-p 1.0",
            "--max-planner-rounds 4",
        ):
            self.assertTrue(all(flag in value for value in args.values()))

    def test_dry_run_has_no_hidden_or_artifact_reuse(self) -> None:
        matrix = load_matrix(matrix_path())
        with tempfile.TemporaryDirectory() as directory:
            outcome = run_matrix(matrix, directory, dry_run=True)
            self.assertEqual(outcome["status"], "DRY_RUN")
            self.assertEqual(outcome["planned_runs"], 48)
            self.assertEqual(outcome["hidden_like_runs"], 0)
            self.assertFalse(outcome["baseline_reuse"])
            self.assertFalse(outcome["candidate_reuse"])
            self.assertFalse(outcome["final_reuse"])

    def test_aggregate_applies_admission_to_real_task_pairs(self) -> None:
        matrix = load_matrix(matrix_path())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETE",
                        "experiment_id": "hybrid-fixture",
                        "planned_runs": 48,
                        "matrix_config_ref": str(matrix.path),
                    }
                ),
                encoding="utf-8",
            )
            records = []
            for job in matrix.jobs:
                run_dir = root / "runs" / job.run_id
                run_dir.mkdir(parents=True)
                receipt = run_dir / "hybrid_artifact_hashes.json"
                receipt.write_text(json.dumps({"files": {}}), encoding="utf-8")
                total_tokens = 1000 if job.condition == "A_FIXED" else 800
                record = {
                    "schema_version": RUN_SCHEMA,
                    **job.to_dict(),
                    "run_ref": f"runs/{job.run_id}",
                    "artifact_manifest_ref": receipt.name,
                    "artifact_manifest_sha256": hashlib.sha256(
                        receipt.read_bytes()
                    ).hexdigest(),
                    "hidden_like": False,
                    "final_success": True,
                    "actual_total_tokens": total_tokens,
                    "actual_input_tokens": total_tokens * 0.75,
                    "actual_output_tokens": total_tokens * 0.25,
                    "planner_calls": 1,
                    "second_call_attempted": False,
                    "second_call_blocked": False,
                    "second_call_improved": False,
                    "patch_invalid_count": 0,
                    "output_truncated": False,
                    "budget_compliant": True,
                    "candidate_promotion_rate": 1.0,
                    "acceleration": 2.0,
                    "local_score_proxy": 0.9,
                    "credits_used": 35,
                    "wall_time_seconds": 10,
                }
                records.append(record)
            (root / "token_policy_hybrid_results.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            result = aggregate(root)

        self.assertTrue(result["real_complete_matrix"])
        self.assertEqual(result["actual_runs"], 48)
        self.assertEqual(len(result["task_level_results"]), 12)
        self.assertEqual(result["engineering_admission"]["status"], "PASS")
        success_comparison = result["task_level_paired_comparisons"][
            "final_success"
        ]
        self.assertEqual(success_comparison["pairs"], 12)
        self.assertEqual(success_comparison["absolute_difference"], 0.0)
        self.assertFalse(result["supports_statistical_conclusion"])
        self.assertFalse(result["supports_competition_score_improvement_claim"])
        self.assertTrue(result["artifact_receipts"]["all_valid"])


if __name__ == "__main__":
    unittest.main()
