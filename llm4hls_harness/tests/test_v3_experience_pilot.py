from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience_pilot import (
    PILOT_RESULT_SCHEMA,
    PILOT_SUMMARY_SCHEMA,
    aggregate_pilot,
    main,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def recommendation(round_index: int) -> dict[str, object]:
    return {
        "schema_version": "v3e.recommendation.v1",
        "recommendation_id": f"{round_index:064x}",
        "round_index": round_index,
        "query_id": f"{round_index + 20:064x}",
        "snapshot": {
            "schema_version": "v3e.experience-snapshot.v1",
            "byte_offset": 10,
            "prefix_sha256": "a" * 64,
            "record_count": 1,
        },
        "guidance": {
            "schema_version": "v3e.experience-guidance.v1",
            "similar_successes": [],
            "similar_failures": [
                {
                    "record_id": "b" * 64,
                    "similarity": 0.8,
                    "features": {},
                    "strategy_bundle": ["PIPELINE"],
                    "outcome": "FAIL:NO_SYNTH_IMPROVEMENT",
                }
            ],
            "recommended_strategy_bundles": [
                {
                    "strategy_bundle": ["ARRAY_PARTITION"],
                    "attempts": 2,
                    "utility": 0.5,
                }
            ],
            "discouraged_strategy_bundles": [
                {
                    "strategy_bundle": ["PIPELINE"],
                    "attempts": 2,
                    "utility": -0.2,
                }
            ],
            "confidence": 0.5,
            "supporting_record_ids": ["b" * 64],
            "fallback_reason": None,
            "notice": "Historical advice only.",
        },
        "risk_advisory": {},
        "continue_advisory": {},
    }


def write_run(batch: Path, arm: str, task_id: str, *, tokens: int) -> dict[str, object]:
    run_id = f"{arm}-{task_id}-r001"
    run_root = batch / "runs" / run_id
    candidates = [
        (1, "candidate_001", ["ARRAY_PARTITION"], "FINAL_VERIFIED", "PASS"),
        (2, "candidate_002", ["ARRAY_PARTITION"], "REJECTED", "FAIL"),
        (3, "candidate_003", ["PIPELINE"], "REJECTED", "PASS"),
    ]
    for index, (_round, candidate, _strategy, _decision, csim) in enumerate(
        candidates, 1
    ):
        write_json(
            run_root / "actions" / f"csim-{index}" / "result.json",
            {
                "candidate_id": candidate,
                "kind": "csim",
                "ok": csim == "PASS",
                "phase": "pass" if csim == "PASS" else "runtime_fail",
            },
        )
        if csim == "PASS":
            write_json(
                run_root / "actions" / f"synth-{index}" / "result.json",
                {
                    "candidate_id": candidate,
                    "kind": "synth",
                    "ok": True,
                    "phase": "pass",
                },
            )
    recommendations = run_root / "experience" / "experience_recommendations.jsonl"
    recommendations.parent.mkdir(parents=True, exist_ok=True)
    recommendations.write_text(
        "".join(json.dumps(recommendation(index), sort_keys=True) + "\n" for index in range(1, 4)),
        encoding="utf-8",
    )
    result = {
        "status": "DONE",
        "baseline_candidate_id": "candidate_000",
        "final_candidate_id": "candidate_001",
        "candidate_rounds": [
            {
                "round": round_index,
                "candidate_id": candidate,
                "strategy_bundle": strategy,
                "decision": decision,
                "decision_reason": (
                    "OPTIMIZATION_FINAL_CLOSURE_PASS"
                    if decision == "FINAL_VERIFIED"
                    else "NO_SYNTH_IMPROVEMENT"
                ),
                "cosim": "NOT_RUN",
                "latency_worst": 40 + round_index,
            }
            for round_index, candidate, strategy, decision, _csim in candidates
        ],
        "node_events": [],
        "experience": {
            "recommendations_ref": "experience/experience_recommendations.jsonl"
        },
    }
    write_json(run_root / "v3_prototype_result.json", result)
    return {
        "task_id": task_id,
        "expected_mode": "OPTIMIZE",
        "routed_mode": "OPTIMIZE",
        "model": "org/deepseek-v4-pro",
        "repeat_index": 1,
        "run_id": run_id,
        "run_fingerprint": ("c" if arm == "shadow" else "d") * 64,
        "status": "DONE",
        "execution_started": True,
        "e2e_success": True,
        "fresh_final_success": True,
        "final_validation_success": True,
        "failure_stage": None,
        "stop_reason": "COMPLETED",
        "acceleration_vs_baseline": 2.5,
        "tokens_used": tokens,
        "input_tokens_used": tokens - 20,
        "output_tokens_used": 20,
        "credits_used": 31,
        "wall_time_s": 10.0,
        "tool_calls": {"csim": 3, "synth": 2, "cosim": 1, "llm": 3},
        "source_result_ref": "v3_prototype_result.json",
    }


def write_batch(
    root: Path,
    arm: str,
    task_ids: tuple[str, ...],
    *,
    seed: str = "a" * 64,
    omit_last: bool = False,
) -> None:
    summary = {
        "configuration": {
            "backend": "vitis",
            "models": ["org/deepseek-v4-pro"],
            "repeats": 1,
            "validation_profile": "strict",
            "experience_mode": arm,
            "experience_store_snapshot": {
                "present": True,
                "sha256": seed,
                "size_bytes": 100,
            },
            # Deliberately absolute: the pilot output must not copy it.
            "output_dir": str(root.resolve()),
            "experience_store": str((root / "secret-seed.jsonl").resolve()),
        },
        "selection": {"task_ids": list(task_ids), "tasks_selected": len(task_ids)},
    }
    write_json(root / "summary.json", summary)
    observed = task_ids[:-1] if omit_last else task_ids
    rows = [
        write_run(root, arm, task_id, tokens=100 if arm == "shadow" else 120)
        for task_id in observed
    ]
    (root / "benchmark_results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class ExperiencePilotTests(unittest.TestCase):
    def test_complete_comparison_aggregates_candidate_and_advice_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = ("task_a", "task_b")
            write_batch(root / "shadow", "shadow", tasks)
            write_batch(root / "guided", "guided", tasks)

            summary = aggregate_pilot(
                root / "shadow", root / "guided", root / "out", expected_task_ids=tasks
            )

            self.assertEqual(summary["schema_version"], PILOT_SUMMARY_SCHEMA)
            self.assertEqual(summary["status"], "COMPLETE")
            self.assertTrue(summary["comparison_valid"])
            guided = summary["arms"]["guided"]["metrics"]
            gates = guided["candidate_gates"]
            self.assertEqual(guided["scheduled_attempts"], 2)
            self.assertEqual(guided["final_successes"], 2)
            self.assertEqual(guided["average_tokens"], 120.0)
            self.assertEqual(gates["duplicate_strategy_attempts"], 2)
            self.assertEqual(gates["recommendation_agreements"], 4)
            self.assertEqual(gates["repeated_known_failure_strategies"], 2)
            self.assertEqual(gates["advice_helpful"], 2)
            self.assertEqual(gates["advice_misleading"], 2)
            self.assertEqual(gates["csim_failures"], 2)
            self.assertEqual(
                summary["comparison"]["guided_minus_shadow"]["average_tokens"],
                20.0,
            )

            rows = [
                json.loads(line)
                for line in (root / "out" / "pilot_results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["schema_version"] == PILOT_RESULT_SCHEMA for row in rows))
            self.assertTrue(all(row["seed_sha256"] == "a" * 64 for row in rows))
            self.assertEqual(rows[0]["model"], "org/deepseek-v4-pro")
            with (root / "out" / "pilot_summary.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), 4)

            serialized = (root / "out" / "pilot_summary.json").read_text() + (
                root / "out" / "pilot_results.jsonl"
            ).read_text()
            self.assertNotIn(str(root.resolve()), serialized)
            self.assertNotIn("secret-seed", serialized)

    def test_missing_batches_emit_blocked_not_run_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = aggregate_pilot(
                root / "missing-shadow",
                root / "missing-guided",
                root / "out",
                expected_task_ids=("task_a", "task_b"),
            )
            self.assertEqual(summary["status"], "BLOCKED")
            self.assertFalse(summary["comparison_valid"])
            self.assertEqual(summary["arms"]["shadow"]["status"], "NOT_RUN")
            self.assertEqual(summary["arms"]["guided"]["status"], "NOT_RUN")
            rows = [
                json.loads(line)
                for line in (root / "out" / "pilot_results.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["attempt_status"] == "NOT_RUN" for row in rows))
            self.assertTrue((root / "out" / "pilot_summary.csv").is_file())

    def test_missing_slot_remains_in_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = ("task_a", "task_b")
            write_batch(root / "shadow", "shadow", tasks, omit_last=True)
            write_batch(root / "guided", "guided", tasks)
            summary = aggregate_pilot(
                root / "shadow", root / "guided", root / "out", expected_task_ids=tasks
            )
            self.assertEqual(summary["status"], "BLOCKED")
            shadow = summary["arms"]["shadow"]["metrics"]
            self.assertEqual(shadow["scheduled_attempts"], 2)
            self.assertEqual(shadow["not_run"], 1)
            self.assertEqual(shadow["final_success_rate"], 0.5)

    def test_seed_mismatch_blocks_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = ("task_a",)
            write_batch(root / "shadow", "shadow", tasks, seed="a" * 64)
            write_batch(root / "guided", "guided", tasks, seed="b" * 64)
            summary = aggregate_pilot(
                root / "shadow", root / "guided", root / "out", expected_task_ids=tasks
            )
            self.assertEqual(summary["status"], "BLOCKED")
            self.assertIn("ARM_SEED_SHA256_MISMATCH", summary["blockers"])

    def test_cli_returns_blocked_status_without_throwing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            import io

            stdout = io.StringIO()
            code = main(
                [
                    "--shadow-dir",
                    str(root / "shadow"),
                    "--guided-dir",
                    str(root / "guided"),
                    "--output-dir",
                    str(root / "out"),
                    "--task-id",
                    "task_a",
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
