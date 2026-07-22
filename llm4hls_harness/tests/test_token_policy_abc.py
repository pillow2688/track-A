from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.token_policy_abc_aggregate import aggregate
from llm4hls_agent.token_policy_abc_runner import (
    CONDITIONS,
    MODES,
    RUN_SCHEMA,
    _condition_args,
    load_matrix,
    reconcile_record_from_artifacts,
    run_matrix,
)


def matrix_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "token_policy_abc"
        / "token_policy_abc_matrix.json"
    )


class TokenPolicyABCMatrixTests(unittest.TestCase):
    def test_public_pilot_schedule_is_balanced_and_content_bound(self) -> None:
        matrix = load_matrix(matrix_path())
        self.assertEqual(len(matrix.jobs), 72)
        self.assertEqual(
            {condition: sum(job.condition == condition for job in matrix.jobs) for condition in CONDITIONS},
            {condition: 24 for condition in CONDITIONS},
        )
        self.assertEqual(
            {mode: len({job.task_id for job in matrix.jobs if job.mode == mode}) for mode in MODES},
            {mode: 3 for mode in MODES},
        )
        self.assertTrue(all(job.task_id.startswith("v3d_fast_") for job in matrix.jobs))
        self.assertEqual(len({job.run_id for job in matrix.jobs}), 72)
        self.assertEqual(len({job.run_fingerprint for job in matrix.jobs}), 72)
        self.assertEqual(len(matrix.implementation_sha256), 64)

        for offset in range(0, len(matrix.jobs), 3):
            self.assertEqual(
                {job.condition for job in matrix.jobs[offset : offset + 3]},
                set(CONDITIONS),
            )

    def test_conditions_change_only_token_policy_visibility(self) -> None:
        matrix = load_matrix(matrix_path())
        args = {condition: _condition_args(matrix, condition) for condition in CONDITIONS}
        joined = {condition: " ".join(value) for condition, value in args.items()}
        self.assertIn("--token-budget-policy fixed", joined["A_FIXED"])
        self.assertNotIn("--token-budget-visibility", joined["A_FIXED"])
        self.assertIn("--token-budget-policy dynamic", joined["B_DYNAMIC_HARD"])
        self.assertIn("--token-budget-visibility hidden", joined["B_DYNAMIC_HARD"])
        self.assertIn("--token-budget-policy dynamic", joined["C_DYNAMIC_VISIBLE"])
        self.assertIn("--token-budget-visibility visible", joined["C_DYNAMIC_VISIBLE"])
        for flag in (
            "--credit-limit 100",
            "--run-token-limit 12000",
            "--llm-temperature 0.0",
            "--llm-top-p 1.0",
            "--max-planner-rounds 4",
        ):
            self.assertTrue(all(flag in value for value in joined.values()))

    def test_dry_run_writes_manifest_without_external_execution(self) -> None:
        matrix = load_matrix(matrix_path())
        with tempfile.TemporaryDirectory() as directory:
            outcome = run_matrix(matrix, directory, dry_run=True)
            self.assertEqual(outcome["status"], "DRY_RUN")
            self.assertEqual(outcome["planned_runs"], 72)
            self.assertEqual(outcome["hidden_like_runs"], 0)
            self.assertFalse(outcome["baseline_reuse"])
            self.assertFalse(outcome["candidate_reuse"])
            self.assertFalse(outcome["final_reuse"])
            self.assertEqual(
                len((Path(directory) / "schedule.jsonl").read_text().splitlines()),
                72,
            )


class TokenPolicyABCAggregateTests(unittest.TestCase):
    def test_reconcile_recovers_post_final_exception_from_durable_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "actions" / "csim").mkdir(parents=True)
            (run_dir / "actions" / "synth").mkdir(parents=True)
            (run_dir / "actions" / "cosim").mkdir(parents=True)
            final_rows = {}
            for stage in ("csim", "synth", "cosim"):
                result_ref = f"actions/{stage}/result.json"
                (run_dir / result_ref).write_text(
                    json.dumps(
                        {
                            "backend_fingerprint": "llm4hls_agent.vitis.VitisBackend:v0.7",
                            "cached": False,
                            "validation_scope": "final",
                        }
                    ),
                    encoding="utf-8",
                )
                final_rows[stage] = {
                    "status": "PASS",
                    "ok": True,
                    "result_ref": result_ref,
                }
            (run_dir / "budget_state.json").write_text(
                json.dumps(
                    {
                        "input_tokens_used": 1200,
                        "output_tokens_used": 300,
                        "tokens_used": 1500,
                        "credits_used": 35,
                        "tool_used": {"llm": 1, "csim": 3, "synth": 3, "cosim": 1},
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "candidate_registry.json").write_text(
                json.dumps(
                    {
                        "final_candidate_id": "candidate_001",
                        "candidates": {
                            "candidate_000": {"kind": "baseline"},
                            "candidate_001": {
                                "kind": "synth_fix",
                                "status": "FINAL_VERIFIED",
                                "final_validation": final_rows,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            recovered, fields = reconcile_record_from_artifacts(
                {
                    "status": "ERROR",
                    "error_type": "BenchmarkExecutionError",
                    "final_success": False,
                    "actual_total_tokens": 0,
                    "credits_used": 0,
                    "evidence_level": None,
                },
                run_dir,
                {"run_token_limit": 12000, "credit_limit": 100},
            )
            self.assertTrue(recovered["final_success"])
            self.assertEqual(recovered["actual_total_tokens"], 1500)
            self.assertEqual(recovered["credits_used"], 35)
            self.assertEqual(recovered["evidence_level"], "REAL_VITIS_VALIDATED")
            self.assertIn("final_success", fields)

    def test_paired_bootstrap_and_admission_use_real_task_pairs(self) -> None:
        matrix = load_matrix(matrix_path())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = matrix.path
            manifest = {
                "status": "COMPLETE",
                "experiment_id": "fixture",
                "planned_runs": 72,
                "matrix_config_ref": str(config),
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            records = []
            for job in matrix.jobs:
                total = {
                    "A_FIXED": 1000,
                    "B_DYNAMIC_HARD": 900,
                    "C_DYNAMIC_VISIBLE": 800,
                }[job.condition]
                run_dir = root / "runs" / job.run_id
                run_dir.mkdir(parents=True)
                artifact = run_dir / "abc_artifact_hashes.json"
                artifact.write_text(
                    json.dumps({"schema_version": "fixture", "files": {}}),
                    encoding="utf-8",
                )
                record = {
                    "schema_version": RUN_SCHEMA,
                    **job.to_dict(),
                    "run_ref": f"runs/{job.run_id}",
                    "artifact_manifest_ref": "abc_artifact_hashes.json",
                    "artifact_manifest_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "evidence_level": "REAL_VITIS_VALIDATED",
                    "hidden_like": False,
                    "final_success": True,
                    "actual_total_tokens": total,
                    "actual_input_tokens": total * 0.75,
                    "actual_output_tokens": total * 0.25,
                    "max_output_utilization": 0.5,
                    "output_truncated": False,
                    "json_incomplete_count": 0,
                    "patch_incomplete_count": 0,
                    "patch_invalid_count": 0,
                    "patch_valid_count": 1,
                    "planner_calls": 1,
                    "budget_compliant": True,
                    "candidate_promotion_rate": 1.0,
                    "latency_improved": True,
                    "acceleration": 2.0,
                    "local_score_proxy": 0.9,
                    "credits_used": 35,
                    "wall_time_seconds": 10,
                }
                records.append(record)
            (root / "token_policy_abc_results.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            result = aggregate(root)

            self.assertTrue(result["real_complete_matrix"])
            self.assertEqual(result["actual_runs"], 72)
            self.assertEqual(result["engineering_admission"]["status"], "PASS")
            a_to_c = result["paired_comparisons"]["A_vs_C"]
            self.assertEqual(a_to_c["paired_tasks"], 12)
            self.assertEqual(a_to_c["inference_status"], "DESCRIPTIVE_ONLY")
            token_comparison = a_to_c["metrics"]["actual_total_tokens"]
            self.assertEqual(token_comparison["absolute_difference"], -200)
            self.assertEqual(token_comparison["paired_bootstrap_95_ci"], [-200, -200])
            self.assertTrue(
                result["engineering_admission"]["checks"][
                    "optimization_quality_not_degraded"
                ]
            )
            self.assertFalse(result["supports_statistical_conclusion"])
            self.assertTrue(result["artifact_receipts"]["all_valid"])


if __name__ == "__main__":
    unittest.main()
