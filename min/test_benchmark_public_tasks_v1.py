from __future__ import annotations

import unittest

import benchmark_public_tasks_v1 as benchmark


class BenchmarkPublicTasksV1Tests(unittest.TestCase):
    def test_preflight_and_formal_schedules_are_interleaved_without_duplicates(self) -> None:
        preflight = benchmark._schedule("preflight")
        formal = benchmark._schedule("formal")
        self.assertEqual(len(preflight), 7)
        self.assertEqual(len(set(preflight)), 7)
        self.assertEqual(len(formal), 30)
        self.assertEqual(len(set(formal)), 30)
        self.assertEqual(
            preflight[:3],
            [
                ("projection_bugfix", 1),
                ("dotProduct_optimize", 1),
                ("residual_stream_deadlock", 1),
            ],
        )

    def test_unknown_tokens_are_excluded_not_converted_to_zero(self) -> None:
        records = [
            self._record(tokens=100, known_tokens=100, known=1, unknown=0),
            self._record(tokens=None, known_tokens=0, known=0, unknown=1),
        ]
        summary = benchmark.summarize(records)
        overall = summary["overall"]
        reliability = summary["llm_reliability"]
        self.assertIsNone(overall["average_tokens_exact"])
        self.assertEqual(overall["known_tokens_total"], 100)
        self.assertEqual(overall["recorded_token_lower_bound"], 100)
        self.assertEqual(reliability["usage_known_calls"], 1)
        self.assertEqual(reliability["usage_unknown_calls"], 1)
        self.assertEqual(reliability["known_call_token_mean"], 100.0)

    @staticmethod
    def _record(
        *,
        tokens: int | None,
        known_tokens: int,
        known: int,
        unknown: int,
    ) -> dict[str, object]:
        return {
            "task_id": "projection_bugfix",
            "flow_success": True,
            "objective_success": True,
            "qualified_success": True,
            "tokens": tokens,
            "known_tokens": known_tokens,
            "usage_known_count": known,
            "usage_unknown_count": unknown,
            "llm_calls": 1,
            "credits": 5,
            "runtime_seconds": 10.0,
            "score_proxy": 1.0,
            "pending_credits": 0,
            "pending_tokens": 0,
            "tool_used": {"llm": 1, "csim": 1, "synth": 1, "cosim": 0},
            "response_audit": {
                "receipt_present": True,
                "parse_error_type": None,
            },
            "patch_audit": {
                "strict_apply_successes": 1,
                "normalized_apply_successes": 0,
                "exact_context_recovery_successes": 0,
                "unique_delete_block_recovery_successes": 0,
                "zero_match_count": 0,
                "ambiguous_match_count": 0,
                "fuzzy_recovery_count": 0,
                "final_patch_failed": False,
            },
            "budget_audit": {
                "ledger_equals_result_stable": True,
                "ledger_equals_state_stable": True,
                "state_equals_result_exactly": True,
            },
        }


if __name__ == "__main__":
    unittest.main()
