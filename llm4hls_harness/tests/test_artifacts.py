from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.artifacts import (
    ArtifactManifestError,
    build_artifact_manifest,
    manifest_digest,
    verify_artifact_manifest,
)


class ArtifactManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        values = {
            "task_spec.json": {"task_id": "task_a"},
            "run_config.json": {
                "tool": {"toolchain_id": "Vitis 2025.2", "part": "u55c"}
            },
            "candidate_registry.json": {
                "baseline_candidate_id": "candidate_000",
                "best_candidate_id": "candidate_001",
                "final_candidate_id": "candidate_001",
                "candidates": {
                    "candidate_000": {"code_hash": "0" * 64},
                    "candidate_001": {
                        "code_hash": "1" * 64,
                        "provider": "openai-compatible",
                        "model": "deepseek-v4-pro",
                    },
                },
            },
            "v1_result.json": {
                "baseline": {"run_id": "run", "task_id": "task_a"},
                "candidate_id": "candidate_001",
                "candidate": {
                    "provider": "openai-compatible",
                    "model": "deepseek-v4-pro",
                },
            },
        }
        for name, value in values.items():
            (self.run_dir / name).write_text(
                json.dumps(value, sort_keys=True), encoding="utf-8"
            )
        (self.run_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        (self.run_dir / "experimental_report.md").write_text(
            "# report\n", encoding="utf-8"
        )
        action = self.run_dir / "actions" / "action_a"
        action.mkdir(parents=True)
        (action / "result.json").write_text(
            json.dumps(
                {
                    "action_id": "action_a",
                    "candidate_id": "candidate_001",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_manifest_is_stable_sorted_and_self_excluding(self) -> None:
        first = build_artifact_manifest(self.run_dir)
        first_bytes = (self.run_dir / "artifact_manifest.json").read_bytes()
        second = build_artifact_manifest(self.run_dir)

        self.assertEqual(first, second)
        self.assertEqual(
            (self.run_dir / "artifact_manifest.json").read_bytes(), first_bytes
        )
        paths = [entry["path"] for entry in first["artifacts"]]
        self.assertEqual(paths, sorted(paths))
        self.assertNotIn("artifact_manifest.json", paths)
        verified = verify_artifact_manifest(self.run_dir)
        self.assertEqual(verified, first)
        self.assertEqual(len(manifest_digest(self.run_dir)), 64)

    def test_manifest_verification_fails_closed_after_tampering(self) -> None:
        build_artifact_manifest(self.run_dir)
        (self.run_dir / "trace.jsonl").write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(ArtifactManifestError, "digest|size"):
            verify_artifact_manifest(self.run_dir)

    def test_sensitive_file_name_is_rejected(self) -> None:
        (self.run_dir / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")

        with self.assertRaisesRegex(ArtifactManifestError, "sensitive"):
            build_artifact_manifest(self.run_dir)


if __name__ == "__main__":
    unittest.main()
