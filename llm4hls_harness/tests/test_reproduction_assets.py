from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ReproductionAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = Path(__file__).resolve().parents[1]
        cls.entrypoint = cls.project / "scripts" / "v3d-reproduce.sh"

    def test_demo_smoke_persists_only_demo_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update(
                {
                    "LLM4HLS_OUTPUT_DIR": directory,
                    "LLM4HLS_DEMO_BATCH_TIMEOUT_S": "10",
                    "LLM4HLS_DEMO_SMOKE_TIMEOUT_S": "20",
                    "PYTHON_BIN": sys.executable,
                }
            )
            completed = subprocess.run(
                [str(self.entrypoint), "demo-smoke"],
                cwd=self.project,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            output = Path(directory) / "v3d-demo-smoke"
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["by_evidence_class"]["DEMO"]["runs"], 1)
            self.assertEqual(summary["real_evidence_headline"]["runs"], 0)
            self.assertEqual(summary["configuration"]["backend"], "demo")
            for name in (
                "benchmark_results.jsonl",
                "summary.json",
                "summary.csv",
                "report.md",
            ):
                self.assertTrue((output / name).is_file(), name)

    def test_official_smoke_runs_exactly_three_fixture_tasks(self) -> None:
        official = (
            self.project
            / "task_corpus"
            / "official"
            / "fpt26-harness-public"
        )
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update(
                {
                    "LLM4HLS_OUTPUT_DIR": directory,
                    "LLM4HLS_OFFICIAL_CORPUS_ROOT": str(official),
                    "LLM4HLS_OFFICIAL_BATCH_TIMEOUT_S": "20",
                    "LLM4HLS_OFFICIAL_SMOKE_TIMEOUT_S": "30",
                    "PYTHON_BIN": sys.executable,
                }
            )
            completed = subprocess.run(
                [str(self.entrypoint), "official-smoke"],
                cwd=self.project,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=40,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            output = Path(directory) / "v3d-official-three-smoke"
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            receipt = json.loads(
                (output / "official_smoke_receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["selection"]["tasks_selected"], 3)
            self.assertEqual(summary["selection"]["planned_runs"], 3)
            self.assertEqual(summary["by_evidence_class"]["DETERMINISTIC"]["runs"], 3)
            self.assertEqual(summary["real_evidence_headline"]["runs"], 0)
            self.assertEqual(receipt["evidence"], "DETERMINISTIC_FIXTURE_ONLY")
            self.assertEqual(receipt["hls_actions_started"], 0)
            self.assertEqual(receipt["llm_calls_started"], 0)
            self.assertEqual(
                receipt["synthetic_tool_call_records"],
                {"csim": 0, "synth": 0, "cosim": 0},
            )
            self.assertEqual(
                set(receipt["tasks"]),
                {
                    "projection_bugfix",
                    "dotProduct_optimize",
                    "residual_stream_deadlock",
                },
            )
            for name in (
                "benchmark_results.jsonl",
                "summary.json",
                "summary.csv",
                "report.md",
                "benchmark_summary.json",
                "benchmark_summary.csv",
                "benchmark_report.md",
                "official_smoke_receipt.json",
            ):
                self.assertTrue((output / name).is_file(), name)

    def test_official_smoke_requires_external_corpus_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update(
                {
                    "LLM4HLS_OUTPUT_DIR": directory,
                    "LLM4HLS_OFFICIAL_CORPUS_ROOT": str(Path(directory) / "missing"),
                    "PYTHON_BIN": sys.executable,
                }
            )
            completed = subprocess.run(
                [str(self.entrypoint), "official-smoke"],
                cwd=self.project,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 3)
            self.assertIn("Official public corpus mount is missing", completed.stderr)

    def test_quick_tests_run_from_explicit_source_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_clean_room_smoke.py").write_text(
                "import unittest\n\n"
                "class CleanRoomSmoke(unittest.TestCase):\n"
                "    def test_clean_dependency_runtime(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.update(
                {
                    "LLM4HLS_SOURCE_ROOT": str(source),
                    "LLM4HLS_QUICK_TEST_TIMEOUT_S": "20",
                    "PYTHON_BIN": sys.executable,
                }
            )
            completed = subprocess.run(
                [str(self.entrypoint), "quick-tests"],
                cwd=self.project,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Ran 1 test", completed.stderr)

    def test_real_preflight_fails_closed_before_hls_when_vitis_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update(
                {
                    "LLM4HLS_OUTPUT_DIR": directory,
                    "LLM4HLS_VITIS_HLS_ROOT": str(Path(directory) / "missing-vitis"),
                    "PYTHON_BIN": sys.executable,
                }
            )
            completed = subprocess.run(
                [str(self.entrypoint), "real-preflight"],
                cwd=self.project,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 3)
            payload = json.loads(completed.stderr.strip().splitlines()[-1])
            self.assertEqual(payload["error_type"], "VITIS_SETTINGS_MISSING")
            self.assertEqual(payload["evidence"], "VITIS_PREFLIGHT_ONLY")
            self.assertEqual(payload["hls_actions_started"], 0)

    def test_container_context_excludes_secrets_and_private_oracles(self) -> None:
        dockerignore = (self.project / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (
            ".env.*",
            "*.key",
            "*.pem",
            "task_corpus/official",
            "task_corpus/v3d-fast/tasks/*/golden",
            "task_corpus/v3d-fast/tasks/*/hidden_like",
        ):
            self.assertIn(pattern, dockerignore)

        template = (self.project / ".env.example").read_text(encoding="utf-8")
        self.assertIn("provider.example.invalid", template)
        self.assertIn("OPENAI_API_KEY=replace-with-provider-api-key", template)
        self.assertNotIn("sk-", template)

        dockerfile = (self.project / "Dockerfile").read_text(encoding="utf-8")
        copy_lines = [
            line.strip() for line in dockerfile.splitlines() if line.startswith("COPY ")
        ]
        self.assertNotIn("COPY . .", copy_lines)
        self.assertFalse(any(".env" in line for line in copy_lines))
        self.assertFalse(any("Vitis" in line for line in copy_lines))
        self.assertFalse(any("task_corpus/official" in line for line in copy_lines))

        entrypoint = self.entrypoint.read_text(encoding="utf-8")
        self.assertIn("official-smoke", entrypoint)
        self.assertIn("quick-tests", entrypoint)
        self.assertIn("DETERMINISTIC_FIXTURE_ONLY", entrypoint)


if __name__ == "__main__":
    unittest.main()
