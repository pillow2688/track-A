from __future__ import annotations

import unittest

import benchmark_public_tasks as benchmark


class BenchmarkPublicTasksTests(unittest.TestCase):
    def test_optimize_baseline_fallback_is_not_objective_success(self) -> None:
        result = {
            "status": "DONE",
            "selected_candidate_id": "candidate_000",
            "selection_reason": "NO_SAFE_STRICT_LATENCY_IMPROVEMENT",
            "baseline": {
                "functional_pass": True,
                "synth_pass": True,
            },
            "candidate": {
                "functional_pass": True,
                "synth_pass": True,
            },
        }
        self.assertFalse(
            benchmark._objective_success(
                task_type="optimize",
                result=result,
            )
        )

    def test_strict_optimization_and_verified_repair_are_successes(self) -> None:
        optimized = {
            "status": "DONE",
            "selected_candidate_id": "candidate_001",
            "selection_reason": "STRICT_WORST_LATENCY_IMPROVEMENT",
            "baseline": {
                "functional_pass": True,
                "synth_pass": True,
            },
            "candidate": {
                "functional_pass": True,
                "synth_pass": True,
            },
        }
        repaired = optimized | {
            "selection_reason": "FUNCTIONAL_AND_SYNTHESIZABLE_REPAIR"
        }
        self.assertTrue(
            benchmark._objective_success(
                task_type="optimize",
                result=optimized,
            )
        )
        self.assertTrue(
            benchmark._objective_success(
                task_type="repair",
                result=repaired,
            )
        )
        self.assertTrue(
            benchmark._objective_success(
                task_type="structural",
                result=repaired,
            )
        )

    def test_summary_averages_include_failures(self) -> None:
        records = [
            {
                "task_id": "projection_bugfix",
                "attempt": 1,
                "flow_success": True,
                "objective_success": True,
                "qualified_success": True,
                "tokens": 100,
                "credits": 6,
                "runtime_seconds": 10.0,
                "score_proxy": 1.4,
            },
            {
                "task_id": "projection_bugfix",
                "attempt": 2,
                "flow_success": False,
                "objective_success": False,
                "qualified_success": False,
                "tokens": 0,
                "credits": 1,
                "runtime_seconds": 2.0,
                "score_proxy": 0.0,
            },
        ]
        summary = benchmark.summarize(records)
        task = summary["tasks"]["projection_bugfix"]
        self.assertEqual(task["objective_success_rate"], 0.5)
        self.assertEqual(
            task["averages_over_all_attempts"],
            {
                "tokens": 50.0,
                "credits": 3.5,
                "runtime_seconds": 6.0,
                "score_proxy": 0.7,
            },
        )


if __name__ == "__main__":
    unittest.main()
