from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_kb import ExperienceKnowledgeBase, build_kb_query
from llm4hls_agent.v3_experience_kb_cli import main
from llm4hls_agent.v3_experience_v2 import seal_experience_v2

from .test_v3_experience_v2_schema import sample_body


class ExperienceKBCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kb = ExperienceKnowledgeBase(self.root / "kb")
        for index in (1, 2):
            body = copy.deepcopy(sample_body())
            body["source"].update(
                {
                    "run_id": f"run-{index}",
                    "candidate_id": f"candidate-{index}",
                    "round_index": index,
                    "task_family_hash": f"{index + 10:064x}",
                }
            )
            body["strategy"]["patch_digest"] = f"{index + 100:064x}"
            body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
            self.kb.put_if_absent(seal_experience_v2(body))
        self.snapshot = self.kb.snapshot(persist=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *args: str) -> tuple[int, dict[str, object]]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = main(list(args))
        return status, json.loads(stream.getvalue())

    def test_stats_and_snapshot_emit_machine_json(self) -> None:
        status, stats = self.invoke("stats", "--kb-root", str(self.kb.root))
        self.assertEqual(status, 0)
        self.assertEqual(stats["migrated_record_count"], 2)
        status, snapshot = self.invoke("snapshot", "--kb-root", str(self.kb.root))
        self.assertEqual(status, 0)
        self.assertEqual(snapshot["record_count"], 2)

    def test_retrieve_and_rank_accept_frozen_snapshot(self) -> None:
        query = build_kb_query(
            mode="OPTIMIZE",
            task_split="hidden_like",
            bottleneck_subtype="SERIAL_REDUCTION",
            algorithm_family="DOT_PRODUCT",
            task_family_hash="f" * 64,
            toolchain="Vitis 2025.2",
            backend_fingerprint="vitis-backend-v0.7",
            prompt_version="v3c.task-aware.v1",
        )
        query_path = self.root / "query.json"
        query_path.write_bytes(canonical_json(query) + b"\n")
        snapshot_path = next((self.kb.root / "snapshots").glob("*.json"))
        status, retrieval = self.invoke(
            "retrieve",
            "--kb-root",
            str(self.kb.root),
            "--snapshot",
            str(snapshot_path),
            "--query-json",
            str(query_path),
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(retrieval["successes"]), 2)
        status, ranking = self.invoke(
            "rank",
            "--kb-root",
            str(self.kb.root),
            "--snapshot",
            str(snapshot_path),
            "--query-json",
            str(query_path),
        )
        self.assertEqual(status, 0)
        self.assertEqual(ranking["support_count"], 2)

    def test_validate_quarantines_only_digest_of_bad_record(self) -> None:
        bad = self.root / "bad.jsonl"
        bad.write_text('{"not":"v2"}\n', encoding="utf-8")
        status, result = self.invoke(
            "validate",
            "--kb-root",
            str(self.kb.root),
            "--input",
            str(bad),
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["valid"], 0)
        self.assertEqual(result["quarantined"], 1)
        quarantine = (self.kb.root / "quarantine" / "quarantine.jsonl").read_text()
        self.assertNotIn('{"not":"v2"}', quarantine)

    def test_readiness_does_not_run_external_tools(self) -> None:
        records = self.root / "records.jsonl"
        records.write_bytes(
            b"".join(
                canonical_json(item) + b"\n" for item in self.kb.records(self.snapshot)
            )
        )
        status, result = self.invoke("readiness", "--records", str(records))
        self.assertEqual(status, 0)
        self.assertEqual(result["strategy_ranker"]["status"], "NOT_READY")
        self.assertFalse(result["complex_model_training_allowed"])


if __name__ == "__main__":
    unittest.main()
