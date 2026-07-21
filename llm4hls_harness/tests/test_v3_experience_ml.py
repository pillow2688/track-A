from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_ml import (
    build_learning_readiness,
    build_ml_datasets,
    write_ml_artifacts,
)
from llm4hls_agent.v3_experience_v2 import seal_experience_v2

from .test_v3_experience_v2_schema import sample_body


def record(
    index: int,
    *,
    family: str,
    run: str,
    cosim_status: str = "PASS",
    success: bool = True,
) -> dict[str, object]:
    body = copy.deepcopy(sample_body())
    body["source"].update(
        {
            "run_id": run,
            "candidate_id": f"candidate-{index}",
            "round_index": index,
            "task_family_hash": family,
        }
    )
    body["strategy"].update(
        {
            "patch_digest": f"{index + 100:064x}",
            "declared_strategy_bundle": ["LOOP_UNROLL"],
            "observed_strategy_atoms": ["LOOP_UNROLL"],
        }
    )
    body["validation"]["cosim_status"] = cosim_status
    body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
    if not success:
        body["validation"].update(
            {"fresh_final_status": "FAIL", "promoted": False, "rejected": True}
        )
        body["performance"].update(
            {"strict_improvement": False, "acceleration": 1.0, "latency_after": 1027.0}
        )
    return seal_experience_v2(body)


class ExperienceMLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            record(1, family="1" * 64, run="run-a"),
            record(2, family="1" * 64, run="run-a", success=False),
            record(3, family="2" * 64, run="run-b", cosim_status="FAIL", success=False),
            record(4, family="3" * 64, run="run-c"),
            record(5, family="4" * 64, run="run-d"),
        ]

    def test_family_group_split_is_disjoint_and_deterministic(self) -> None:
        first = build_ml_datasets(self.records)
        second = build_ml_datasets(list(reversed(self.records)))
        self.assertEqual(first, second)
        families_by_split = {}
        for family, split in first["family_splits"].items():
            families_by_split.setdefault(split, set()).add(family)
        values = list(families_by_split.values())
        for index, left in enumerate(values):
            for right in values[index + 1 :]:
                self.assertTrue(left.isdisjoint(right))
        self.assertIn("train", families_by_split)
        self.assertIn("validation", families_by_split)
        self.assertIn("test", families_by_split)

    def test_export_labels_are_derived_from_real_validation(self) -> None:
        export = build_ml_datasets(self.records)
        strategy = export["datasets"]["strategy_ranking"]
        self.assertEqual(len(strategy), len(self.records))
        self.assertTrue(any(row["labels"]["success"] for row in strategy))
        self.assertTrue(any(not row["labels"]["success"] for row in strategy))
        cosim = export["datasets"]["cosim_risk"]
        self.assertEqual(
            {row["labels"]["cosim_status"] for row in cosim}, {"PASS", "FAIL"}
        )
        continues = export["datasets"]["continue_value"]
        self.assertEqual(len(continues), 1)
        self.assertFalse(continues[0]["labels"]["next_round_improved"])

    def test_token_policy_is_exported_as_cost_and_feasibility_context(self) -> None:
        export = build_ml_datasets(self.records)
        strategy = export["datasets"]["strategy_ranking"][0]["features"]
        self.assertEqual(strategy["patch_complexity"], "SMALL")
        self.assertEqual(strategy["token_cost_context"]["token_pressure"], "MEDIUM")
        self.assertEqual(
            strategy["token_cost_context"]["effective_max_output_tokens"], 1400
        )
        cosim = export["datasets"]["cosim_risk"][0]["features"]
        self.assertEqual(cosim["token_context"]["token_pressure"], "MEDIUM")
        evidence = export["datasets"]["evidence_selection"][0]["features"]
        self.assertEqual(
            evidence["context_token_profile"]["guidance_actual_tokens"], 200
        )
        continuation = export["datasets"]["continue_value"][0]["features"]
        self.assertIn("future_round_token_reserve", continuation["token_policy"])

    def test_dataset_rows_have_no_task_identity_or_forbidden_content(self) -> None:
        export = build_ml_datasets(self.records)
        rendered = json.dumps(export["datasets"], sort_keys=True).casefold()
        self.assertNotIn("task_id", rendered)
        self.assertNotIn("source_code", rendered)
        self.assertNotIn("patch_text", rendered)
        self.assertNotIn("golden", rendered)
        self.assertNotIn("hidden", rendered)

    def test_insufficient_data_is_not_ready(self) -> None:
        export = build_ml_datasets(self.records)
        readiness = build_learning_readiness(self.records, export)
        self.assertEqual(readiness["strategy_ranker"]["status"], "NOT_READY")
        self.assertEqual(readiness["cosim_risk"]["status"], "NOT_READY")
        self.assertEqual(readiness["continue_predictor"]["status"], "NOT_READY")
        self.assertEqual(readiness["evidence_selector"]["status"], "NOT_READY")
        self.assertFalse(readiness["complex_model_training_allowed"])

    def test_writer_emits_dataset_card_schema_splits_and_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records_path = root / "records.jsonl"
            records_path.write_bytes(
                b"".join(canonical_json(item) + b"\n" for item in self.records)
            )
            output = root / "datasets"
            write_ml_artifacts(records_path=records_path, output_root=output)
            for name in (
                "strategy_ranking_train.jsonl",
                "cosim_risk_train.jsonl",
                "continue_value_train.jsonl",
                "evidence_selection_train.jsonl",
                "dataset_card.md",
                "feature_schema.json",
                "split_manifest.json",
                "experience_learning_readiness.json",
                "experience_learning_readiness.md",
            ):
                self.assertTrue((output / name).is_file(), name)
            manifest = json.loads((output / "split_manifest.json").read_text())
            self.assertTrue(manifest["family_split_disjoint"])


if __name__ == "__main__":
    unittest.main()
