from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from llm4hls_agent.task import load_public_task


class OfficialTaskCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).parents[1]
        self.corpus = self.project / "task_corpus"
        self.official = self.corpus / "official" / "fpt26-harness-public"

    def test_manifest_prioritizes_official_public_tasks(self) -> None:
        manifest = json.loads((self.corpus / "manifest.json").read_text())
        collections = manifest["collections"]

        self.assertEqual(manifest["selection_policy"], "official_public_first")
        self.assertIn("source_checkout_only", manifest["distribution"])
        self.assertEqual(collections[0]["id"], "fpt26_official_public_poc")
        self.assertEqual(collections[0]["priority"], 0)
        self.assertFalse(collections[0]["contains_hidden_or_reference_files"])

    def test_official_public_tasks_are_complete_loadable_and_diverse(self) -> None:
        expected = {
            "dotProduct_optimize": ("optimize", 3, False),
            "projection_bugfix": ("repair", 2, False),
            "residual_stream_deadlock": ("structural", 4, True),
        }

        loaded = {
            task_id: load_public_task(self.official / task_id)
            for task_id in expected
        }

        self.assertEqual(set(loaded), set(expected))
        for task_id, task in loaded.items():
            task_type, difficulty, requires_cosim = expected[task_id]
            self.assertEqual(task.id, task_id)
            self.assertEqual(task.task_type, task_type)
            self.assertEqual(task.difficulty, difficulty)
            self.assertEqual(task.requires_cosim, requires_cosim)
            self.assertEqual(task.part, "xcu55c-fsvh2892-2L-e")
            self.assertEqual(task.clock_ns, 5.0)
            self.assertNotIn("hidden", task.public_file_hashes)
            self.assertNotIn("reference", task.public_file_hashes)

    def test_provenance_hashes_cover_every_vendored_public_task_file(self) -> None:
        provenance = json.loads((self.official / "provenance.json").read_text())
        recorded = provenance["files"]
        actual_paths = {
            str(path.relative_to(self.official))
            for path in self.official.rglob("*")
            if path.is_file() and path.name != "provenance.json"
        }

        self.assertEqual(set(recorded), actual_paths)
        for relative, expected_hash in recorded.items():
            digest = hashlib.sha256((self.official / relative).read_bytes()).hexdigest()
            self.assertEqual(digest, expected_hash, relative)


if __name__ == "__main__":
    unittest.main()
