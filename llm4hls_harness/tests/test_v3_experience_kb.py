from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience_kb import (
    ExperienceKnowledgeBase,
    ExperienceKnowledgeBaseError,
    ExplainableSimilarCaseRetriever,
    build_kb_query,
)
from llm4hls_agent.v3_experience_v2 import seal_experience_v2
from .test_v3_experience_v2_schema import sample_body


def record(
    index: int,
    *,
    mode: str = "OPTIMIZE",
    split: str = "train",
    family: str | None = None,
    success: bool = True,
) -> dict[str, object]:
    body = sample_body()
    body["source"]["run_id"] = f"run-{index}"
    body["source"]["candidate_id"] = f"candidate-{index}"
    body["source"]["round_index"] = index
    body["source"]["task_split"] = split
    body["source"]["task_family_hash"] = family or f"{index:064x}"
    body["strategy"]["patch_digest"] = f"{index + 100:064x}"
    body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
    body["problem"]["mode"] = mode
    if mode == "REPAIR":
        body["problem"]["failure_subtype"] = "WRONG_LOOP_BOUND"
        body["problem"]["bottleneck_subtype"] = "UNKNOWN"
        body["strategy"]["declared_strategy_bundle"] = ["FIX_LOOP_BOUND"]
        body["strategy"]["observed_strategy_atoms"] = ["FIX_LOOP_BOUND"]
    if not success:
        body["validation"]["fresh_final_status"] = "FAIL"
        body["validation"]["promoted"] = False
        body["validation"]["rejected"] = True
        body["performance"]["strict_improvement"] = False
        body["performance"]["latency_after"] = body["performance"]["latency_before"]
        body["performance"]["acceleration"] = 1.0
    return seal_experience_v2(body)


class KnowledgeBaseRepositoryTests(unittest.TestCase):
    def test_append_snapshot_index_and_duplicate_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kb = ExperienceKnowledgeBase(directory)
            first = record(1)
            self.assertTrue(kb.put_if_absent(first).inserted)
            self.assertFalse(kb.put_if_absent(first).inserted)
            snapshot = kb.snapshot()
            kb.put_if_absent(record(2))
            self.assertEqual(len(kb.records(snapshot)), 1)
            self.assertEqual(len(kb.records()), 2)
            index = kb.rebuild_index()
            self.assertIn(first["record_id"], index["dimensions"]["mode"]["OPTIMIZE"])
            self.assertTrue((Path(directory) / "snapshots" / f"{snapshot.prefix_sha256}.json").is_file())

    def test_identity_conflict_fails_without_rewriting_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kb = ExperienceKnowledgeBase(directory)
            first = record(1)
            kb.put_if_absent(first)
            body = sample_body()
            body["source"]["run_id"] = first["source"]["run_id"]
            body["source"]["candidate_id"] = first["source"]["candidate_id"]
            body["source"]["round_index"] = first["source"]["round_index"]
            conflicting = seal_experience_v2(body)
            with self.assertRaisesRegex(ExperienceKnowledgeBaseError, "already has"):
                kb.put_if_absent(conflicting)
            self.assertEqual(kb.records(), (first,))

    def test_corrupt_external_record_is_quarantined_by_digest_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "external.jsonl"
            source.write_text(json.dumps(record(1)) + "\n{bad-json}\n", encoding="utf-8")
            kb = ExperienceKnowledgeBase(root / "kb")
            result = kb.validate_external_jsonl(source)
            self.assertEqual(result["valid"], 1)
            self.assertEqual(result["quarantined"], 1)
            quarantine = (root / "kb/quarantine/quarantine.jsonl").read_text()
            self.assertNotIn("bad-json", quarantine)
            self.assertIn("source_digest", quarantine)


class ExplainableRetrieverTests(unittest.TestCase):
    def query(self, **overrides):
        values = {
            "mode": "OPTIMIZE",
            "task_split": "hidden_like",
            "failure_subtype": "UNKNOWN",
            "bottleneck_subtype": "SERIAL_REDUCTION",
            "algorithm_family": "DOT_PRODUCT",
            "task_family_hash": "f" * 64,
            "structure_features": {
                "has_reduction": True,
                "has_stream": False,
                "has_dataflow": False,
                "critical_loop_ii": 1,
                "critical_loop_trip_count_bucket": "257-1024",
                "transaction_interval_bucket": "1024+",
                "latency_bucket": "1024+",
                "memory_access_pattern": "SEQUENTIAL",
                "requires_cosim": False,
                "patch_complexity": "SMALL",
            },
        }
        values.update(overrides)
        return build_kb_query(**values)

    def test_mode_hidden_split_current_and_family_filters(self) -> None:
        same_family = "f" * 64
        records = [
            record(1, family=same_family),
            record(2, split="dev"),
            record(3, mode="REPAIR"),
            record(4),
        ]
        result = ExplainableSimilarCaseRetriever(min_similarity=0).retrieve(
            self.query(current_run_id="run-4", exclude_same_task_family=True), records
        )
        ids = {item.record["record_id"] for item in result.considered}
        self.assertNotIn(records[0]["record_id"], ids)
        self.assertNotIn(records[1]["record_id"], ids)
        self.assertNotIn(records[2]["record_id"], ids)
        self.assertNotIn(records[3]["record_id"], ids)
        self.assertEqual(result.filtered_counts["HIDDEN_QUERY_TRAIN_ONLY"], 1)

    def test_similarity_is_deterministic_explainable_and_task_id_is_rejected(self) -> None:
        records = [record(1), record(2, success=False)]
        retriever = ExplainableSimilarCaseRetriever(min_similarity=0)
        first = retriever.retrieve(self.query(), records)
        second = retriever.retrieve(self.query(), list(reversed(records)))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertIn("algorithm_family", first.considered[0].explanation["matched_features"])
        invalid = copy.deepcopy(self.query())
        invalid["task_id"] = "forbidden"
        with self.assertRaises(ValueError):
            retriever.retrieve(invalid, records)

    def test_empty_store_is_a_bounded_fallback(self) -> None:
        result = ExplainableSimilarCaseRetriever().retrieve(self.query(), [])
        self.assertEqual(result.successes, ())
        self.assertEqual(result.failures, ())
        self.assertEqual(result.to_dict()["considered"], [])


if __name__ == "__main__":
    unittest.main()
