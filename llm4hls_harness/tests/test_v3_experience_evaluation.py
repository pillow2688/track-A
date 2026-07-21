from __future__ import annotations

import copy
import json
import unittest

from llm4hls_agent.v3_experience_evaluation import evaluate_generalization
from llm4hls_agent.v3_experience_v2 import seal_experience_v2

from .test_v3_experience_v2_schema import sample_body


def record(index: int, *, success: bool = True) -> dict[str, object]:
    body = copy.deepcopy(sample_body())
    algorithm = "DOT_PRODUCT" if index <= 4 else "REDUCTION"
    body["source"].update(
        {
            "run_id": f"run-{((index - 1) % 4) + 1}",
            "candidate_id": f"candidate-{index}",
            "round_index": index,
            "task_family_hash": f"{index + 10:064x}",
            "algorithm_family": algorithm,
        }
    )
    body["strategy"].update(
        {
            "patch_digest": f"{index + 100:064x}",
            "declared_strategy_bundle": ["LOOP_UNROLL"],
            "observed_strategy_atoms": ["LOOP_UNROLL"],
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


class GeneralizedEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [record(index, success=index != 8) for index in range(1, 9)]
        self.audit = {
            item["record_id"]: {"task_audit_hash": f"{index + 300:064x}"}
            for index, item in enumerate(self.records, start=1)
        }

    def test_all_four_holdouts_are_deterministic_and_leak_free(self) -> None:
        first = evaluate_generalization(self.records, audit_groups=self.audit)
        second = evaluate_generalization(
            list(reversed(self.records)), audit_groups=self.audit
        )
        self.assertEqual(first, second)
        self.assertEqual(
            set(first["policies"]),
            {
                "leave_one_run_out",
                "leave_one_task_out",
                "leave_one_task_family_out",
                "leave_one_algorithm_family_out",
            },
        )
        self.assertTrue(first["leakage_checks"]["all_policy_checks_pass"])
        for policy in first["policies"].values():
            self.assertTrue(policy["leakage_check_pass"])
            self.assertEqual(policy["overall"]["family_leakage_rate"], 0.0)

    def test_task_group_is_audit_only_and_never_rendered_in_rows(self) -> None:
        report = evaluate_generalization(self.records, audit_groups=self.audit)
        rows = json.dumps(
            report["policies"]["leave_one_task_out"]["rows"], sort_keys=True
        )
        self.assertNotIn("task_audit_hash", rows)
        self.assertNotIn(next(iter(self.audit.values()))["task_audit_hash"], rows)
        self.assertTrue(report["leakage_checks"]["task_identity_not_a_feature"])

    def test_algorithm_holdout_abstains_without_same_algorithm_support(self) -> None:
        report = evaluate_generalization(self.records, audit_groups=self.audit)
        metrics = report["policies"]["leave_one_algorithm_family_out"]["overall"]
        self.assertEqual(metrics["coverage"], 0.0)
        self.assertEqual(metrics["inject_count"], 0)

    def test_missing_task_audit_groups_are_reported_not_guessed(self) -> None:
        report = evaluate_generalization(self.records, audit_groups={})
        task = report["policies"]["leave_one_task_out"]
        self.assertEqual(task["skipped_missing_group"], len(self.records))
        self.assertEqual(task["overall"]["queries"], 0)

    def test_report_contains_required_quality_metrics_and_breakdowns(self) -> None:
        report = evaluate_generalization(self.records, audit_groups=self.audit)
        for policy in report["policies"].values():
            for key in (
                "coverage",
                "success_strategy_hit_rate",
                "harmful_recommendation_rate",
                "duplicate_failure_suppression_rate",
                "family_leakage_rate",
                "average_guidance_tokens",
                "confidence_brier_score",
            ):
                self.assertIn(key, policy["overall"])
            self.assertIn("OPTIMIZE", policy["by_mode"])
            self.assertIn("SERIAL_REDUCTION", policy["by_subtype"])
            self.assertIn("DOT_PRODUCT", policy["by_algorithm_family"])


if __name__ == "__main__":
    unittest.main()
