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
    TokenPolicyABCError,
    _artifact_manifest,
    _condition_args,
    _condition_executor,
    _execution_identity,
    _load_resumed_records,
    _validate_common_config,
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
        ):
            self.assertTrue(all(flag in value for value in joined.values()))
        self.assertTrue(
            all(
                "--max-planner-rounds" not in value
                and "--max-no-improvement-rounds" not in value
                for value in joined.values()
            )
        )

    def test_condition_executors_use_owned_round_controls(self) -> None:
        matrix = load_matrix(matrix_path())
        for condition in CONDITIONS:
            executor = _condition_executor(matrix, condition)
            self.assertTrue(executor.experimental_token_policy)
            self.assertEqual(executor.max_planner_rounds, 4)
            self.assertEqual(executor.max_no_improvement_rounds, 2)
            self.assertNotIn("--max-planner-rounds", executor.extra_args)
            self.assertNotIn(
                "--max-no-improvement-rounds", executor.extra_args
            )

    def test_execution_identity_binds_model_and_executor(self) -> None:
        matrix = load_matrix(matrix_path())
        executors = {
            condition: _condition_executor(matrix, condition)
            for condition in CONDITIONS
        }
        first = _execution_identity(
            matrix,
            model="model-a",
            vitis_root=Path("/opt/xilinx/2025.2/Vitis"),
            executors=executors,
        )
        second = _execution_identity(
            matrix,
            model="model-b",
            vitis_root=Path("/opt/xilinx/2025.2/Vitis"),
            executors=executors,
        )
        self.assertNotEqual(
            first["execution_identity_sha256"],
            second["execution_identity_sha256"],
        )

    def test_declared_mode_minimums_must_match_runtime_defaults(self) -> None:
        config = json.loads(matrix_path().read_text(encoding="utf-8"))
        config["mode_minimum_viable_output"]["REPAIR"] += 1
        with self.assertRaisesRegex(
            TokenPolicyABCError, "versioned runtime defaults"
        ):
            _validate_common_config(config)

    def test_resume_validates_record_and_artifact_identity(self) -> None:
        matrix = load_matrix(matrix_path())
        job = matrix.jobs[0]
        execution_identity = {
            "execution_identity_sha256": "e" * 64,
            "model": "bound-model",
        }
        condition_config = matrix.config["conditions"][job.condition]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run_dir = output / "runs" / job.run_id
            run_dir.mkdir(parents=True)
            payload_path = run_dir / "durable.json"
            payload_path.write_text('{"status":"DONE"}\n', encoding="utf-8")
            artifact_manifest = _artifact_manifest(run_dir)
            artifact_path = run_dir / "abc_artifact_hashes.json"
            artifact_path.write_text(
                json.dumps(artifact_manifest, sort_keys=True),
                encoding="utf-8",
            )
            record = {
                **job.to_dict(),
                "schema_version": RUN_SCHEMA,
                "matrix_config_sha256": matrix.config_sha256,
                "implementation_sha256": matrix.implementation_sha256,
                "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
                "execution_identity_sha256": "e" * 64,
                "run_ref": f"runs/{job.run_id}",
                "provider": matrix.config["provider"],
                "model": "bound-model",
                "token_budget_policy": condition_config[
                    "token_budget_policy"
                ],
                "token_budget_visibility": condition_config[
                    "token_budget_visibility"
                ],
                "artifact_manifest_ref": "abc_artifact_hashes.json",
                "artifact_manifest_sha256": hashlib.sha256(
                    artifact_path.read_bytes()
                ).hexdigest(),
            }
            (run_dir / "abc_run_record.json").write_text(
                json.dumps(record, sort_keys=True),
                encoding="utf-8",
            )
            results_path = output / "token_policy_abc_results.jsonl"
            results_path.write_text(
                json.dumps(record, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path = output / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "v3e.token-policy-abc-manifest.v1",
                        "matrix_config_sha256": matrix.config_sha256,
                        "implementation_sha256": matrix.implementation_sha256,
                        "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
                        "execution_identity_sha256": "e" * 64,
                        "planned_runs": 1,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            resumed = _load_resumed_records(
                matrix=matrix,
                jobs=(job,),
                output=output,
                manifest_path=manifest_path,
                results_path=results_path,
                execution_identity=execution_identity,
            )
            self.assertEqual(set(resumed), {job.run_id})

            with self.assertRaisesRegex(
                TokenPolicyABCError,
                "resume manifest identity mismatch",
            ):
                _load_resumed_records(
                    matrix=matrix,
                    jobs=(job,),
                    output=output,
                    manifest_path=manifest_path,
                    results_path=results_path,
                    execution_identity={
                        "execution_identity_sha256": "f" * 64,
                        "model": "other-model",
                    },
                )

            payload_path.write_text('{"status":"CHANGED"}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                TokenPolicyABCError, "resume run artifacts changed"
            ):
                _load_resumed_records(
                    matrix=matrix,
                    jobs=(job,),
                    output=output,
                    manifest_path=manifest_path,
                    results_path=results_path,
                    execution_identity=execution_identity,
                )

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
                            "validation_scope": "search_closeout",
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
