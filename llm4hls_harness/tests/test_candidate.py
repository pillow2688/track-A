from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.candidate import CandidateManager
from llm4hls_agent.repair import apply_unified_diff
from llm4hls_agent.task import load_public_task


def make_task(root: Path) -> None:
    root.mkdir()
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "candidate_fixture"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 80",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 10.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text(
        '#include "kernel.h"\nvoid kernel(int *out) {\n    *out = 1;\n}\n',
        encoding="utf-8",
    )
    (root / "kernel.h").write_text("void kernel(int *out);\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class CandidateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_task(task_dir)
        self.task = load_public_task(task_dir)
        self.run_root = self.root / "run"
        self.run_root.mkdir()
        self.manager = CandidateManager(self.run_root, self.task)
        self.registry: dict[str, object] = {
            "schema_version": 2,
            "task_id": self.task.id,
            "baseline_candidate_id": "candidate_000",
            "best_candidate_id": "candidate_002",
            "final_candidate_id": None,
            "candidates": {
                "candidate_000": {
                    "candidate_id": "candidate_000",
                    "parent_id": None,
                    "code_hash": self.task.kernel_sha256,
                },
                "candidate_001": {
                    "candidate_id": "candidate_001",
                    "parent_id": "candidate_000",
                    "code_hash": "1" * 64,
                },
                "candidate_002": {
                    "candidate_id": "candidate_002",
                    "parent_id": "candidate_000",
                    "code_hash": "2" * 64,
                },
            },
        }
        self.patch = (
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,4 +1,4 @@\n"
            ' #include "kernel.h"\n'
            " void kernel(int *out) {\n"
            "-    *out = 1;\n"
            "+    *out = 2;\n"
            " }\n"
        )
        self.application = apply_unified_diff(
            self.task.kernel_bytes,
            self.patch,
            kernel_name=self.task.kernel_name,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def materialize(self, parent_id: str):
        return self.manager.materialize(
            self.registry,
            parent_id=parent_id,
            patch_text=self.patch,
            application=self.application,
            kind="optimization",
            metadata={"round": 3, "optimization_class": "LOOP_UNROLL"},
        )

    def test_child_uses_explicit_best_parent_and_allocates_monotonic_id(self) -> None:
        materialized = self.materialize("candidate_002")

        self.assertEqual(materialized.candidate_id, "candidate_003")
        self.assertEqual(materialized.record["parent_id"], "candidate_002")
        self.assertEqual(
            materialized.record["validation"]["csim"]["status"], "NOT_RUN"
        )

    def test_materialization_is_idempotent_by_patch_and_parent(self) -> None:
        first = self.materialize("candidate_000")
        second = self.materialize("candidate_000")

        self.assertEqual(second.candidate_id, first.candidate_id)
        self.assertEqual(len(self.registry["candidates"]), 4)

    def test_same_patch_on_different_parent_is_a_distinct_candidate(self) -> None:
        first = self.materialize("candidate_000")
        second = self.materialize("candidate_001")

        self.assertNotEqual(second.candidate_id, first.candidate_id)
        self.assertEqual(second.record["parent_id"], "candidate_001")

    def test_missing_parent_is_rejected_without_allocating_candidate(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent"):
            self.materialize("candidate_999")

        self.assertEqual(set(self.registry["candidates"]), {
            "candidate_000", "candidate_001", "candidate_002"
        })


if __name__ == "__main__":
    unittest.main()
