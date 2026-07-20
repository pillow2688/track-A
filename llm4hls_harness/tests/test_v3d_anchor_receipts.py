from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3d_anchor_receipts import (
    AnchorReceiptError,
    EVIDENCE_CLASS,
    _compact_observation,
    verify_anchor_receipts,
)


class V3DAnchorReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = Path(__file__).resolve().parents[1]
        cls.corpus = cls.project / "task_corpus" / "v3d-fast"
        cls.receipts = (
            cls.project
            / "releases"
            / "v3d-real-vitis-anchor-receipts-2026-07-20"
        )
        cls.release = (
            cls.project
            / "releases"
            / "v3d-corpus-oracle-anchors-2026-07-20.json"
        )

    def test_checked_in_receipts_verify_without_ignored_source_runs(self) -> None:
        result = verify_anchor_receipts(
            corpus_root=self.corpus,
            receipts_dir=self.receipts,
            release_path=self.release,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["evidence_class"], EVIDENCE_CLASS)
        self.assertEqual(result["receipts"], 12)
        self.assertEqual(result["checks"], 59)
        self.assertEqual(result["artifact_hashes"], 238)
        self.assertEqual(result["task_tree_matches"], 12)
        self.assertTrue(result["release_bound"])

    def test_receipts_are_small_sanitized_hash_bindings_not_raw_logs(self) -> None:
        files = sorted((self.receipts / "receipts").glob("*.json"))
        self.assertEqual(len(files), 12)
        self.assertLess(sum(path.stat().st_size for path in files), 256 * 1024)
        forbidden = ("/home/", "OPENAI_API_KEY", "Bearer ", '"evidence":', '"artifacts":')
        for path in files + [self.receipts / "manifest.json"]:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for marker in forbidden:
                    self.assertNotIn(marker, text)
        sample = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertFalse(sample["evidence_boundary"]["agent_or_llm_evaluation"])
        self.assertFalse(sample["evidence_boundary"]["raw_artifacts_committed"])
        self.assertEqual(sample["backend"]["name"], "vitis")
        self.assertEqual(len(sample["source"]["source_record_sha256"]), 64)
        self.assertGreater(sample["artifact_binding"]["count"], 0)

    def test_receipt_byte_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "receipts"
            shutil.copytree(self.receipts, copied)
            target = copied / "receipts" / "v3d_fast_001.json"
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["result"]["status"] = "REJECTED"
            target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(AnchorReceiptError, "receipt file hash mismatch"):
                verify_anchor_receipts(
                    corpus_root=self.corpus,
                    receipts_dir=copied,
                )

    def test_current_task_tree_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied_corpus = Path(directory) / "v3d-fast"
            shutil.copytree(self.corpus, copied_corpus)
            kernel = copied_corpus / "tasks" / "v3d_fast_001" / "kernel.cpp"
            kernel.write_text(kernel.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AnchorReceiptError, "task tree has drifted"):
                verify_anchor_receipts(
                    corpus_root=copied_corpus,
                    receipts_dir=self.receipts,
                )

    def test_release_binding_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release.json"
            payload = json.loads(self.release.read_text(encoding="utf-8"))
            payload["valid_real_vitis_anchors"][0]["source_record_sha256"] = "0" * 64
            release.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(AnchorReceiptError, "release binding mismatch"):
                verify_anchor_receipts(
                    corpus_root=self.corpus,
                    receipts_dir=self.receipts,
                    release_path=release,
                )

    def test_extractor_rehashes_every_declared_raw_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt = Path(directory)
            artifact = attempt / "baseline" / "csim" / "stdout.log"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("real tool bytes\n", encoding="utf-8")
            observation = {
                "status": "PASS",
                "phase": "pass",
                "return_code": 0,
                "elapsed_seconds": 1.0,
                "artifact_hashes": {"stdout": "0" * 64},
                "artifacts": {"stdout": "baseline/csim/stdout.log"},
            }
            with self.assertRaisesRegex(AnchorReceiptError, "raw artifact drift"):
                _compact_observation(
                    observation,
                    attempt_root=attempt,
                    subject="baseline",
                    gate="csim",
                )


if __name__ == "__main__":
    unittest.main()
