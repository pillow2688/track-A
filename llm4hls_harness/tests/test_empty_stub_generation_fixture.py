from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleOptimizationProvider,
)
from llm4hls_agent.repair import (
    PatchLimits,
    PatchValidationError,
    apply_unified_diff,
    task_patch_limits,
)
from llm4hls_agent.task import TaskPackageError, load_public_task
from llm4hls_agent.v3_openai_planner import OpenAICompatibleV3PlannerAdapter
from llm4hls_agent.v3_planner import build_planner_input


FIXTURE = (
    Path(__file__).parents[1]
    / "task_corpus"
    / "development"
    / "track_a_empty_stub_generation"
)


def _large_legal_source() -> str:
    return """#include \"matrix_transform.h\"

static int clamp_value(int value) {
    if (value < MATRIX_CLAMP_MIN) {
        return MATRIX_CLAMP_MIN;
    }
    if (value > MATRIX_CLAMP_MAX) {
        return MATRIX_CLAMP_MAX;
    }
    return value;
}

static int compute_raw_cell(
    const int lhs[MATRIX_DIM][MATRIX_DIM],
    const int rhs[MATRIX_DIM][MATRIX_DIM],
    const int bias[MATRIX_DIM],
    int row,
    int column) {
    int accumulator = bias[column];
    for (int k = 0; k < MATRIX_DIM; ++k) {
        accumulator += lhs[row][k] * rhs[k][column];
    }
    return accumulator;
}

void matrix_transform(
    const int lhs[MATRIX_DIM][MATRIX_DIM],
    const int rhs[MATRIX_DIM][MATRIX_DIM],
    const int bias[MATRIX_DIM],
    int output[MATRIX_DIM][MATRIX_DIM],
    int row_sums[MATRIX_DIM]) {
    for (int i = 0; i < MATRIX_DIM; ++i) {
        int row_sum = 0;
        for (int j = 0; j < MATRIX_DIM; ++j) {
            int raw_cell = compute_raw_cell(lhs, rhs, bias, i, j);
            int bounded = clamp_value(raw_cell);
            output[i][j] = bounded;
            row_sum += bounded;
        }
        row_sums[i] = row_sum;
    }
}
"""


def _patch(before: str, after: str, *, target: str = "kernel.cpp") -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{target}",
            tofile=f"b/{target}",
            n=0,
        )
    )


class EmptyStubGenerationFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = load_public_task(FIXTURE)

    def test_public_fixture_is_a_real_empty_stub_generation_task(self) -> None:
        self.assertEqual(self.task.task_type, "generate")
        self.assertTrue(self.task.generation_required)
        self.assertFalse(self.task.requires_cosim)
        self.assertFalse(self.task.difficulty_declared)
        self.assertIn("TODO", self.task.kernel_code)
        self.assertNotIn("for (int", self.task.kernel_code)
        self.assertIn("乘加归约", self.task.description)
        self.assertEqual(
            set(self.task.public_file_hashes),
            {
                "task.toml",
                "description.md",
                "kernel.cpp",
                "matrix_transform.h",
                "matrix_transform_tb.cpp",
            },
        )
        for name in self.task.public_file_hashes:
            self.assertFalse(
                {"hidden", "reference", "golden"}.intersection(Path(name).parts)
            )

    def test_generation_large_kernel_body_is_allowed_but_normal_repair_is_not(self) -> None:
        patch = _patch(self.task.kernel_code, _large_legal_source())
        changed_lines = sum(
            1
            for line in patch.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        normal_task_dir = Path(tempfile.mkdtemp()) / "repair"
        try:
            shutil.copytree(FIXTURE, normal_task_dir)
            task_toml = normal_task_dir / "task.toml"
            task_toml.write_text(
                task_toml.read_text(encoding="utf-8")
                .replace('task_type = "generate"', 'task_type = "repair"')
                .replace("generation_required = true\n", "generation_required = false\n"),
                encoding="utf-8",
            )
            normal_task = load_public_task(normal_task_dir)
            standard = PatchLimits(max_changed_lines=30, max_hunks=4)
            self.assertGreater(changed_lines, standard.max_changed_lines)
            with self.assertRaises(PatchValidationError):
                apply_unified_diff(
                    normal_task.kernel_bytes,
                    patch,
                    kernel_name=normal_task.kernel_name,
                    limits=task_patch_limits(normal_task, standard),
                    task=normal_task,
                )
        finally:
            shutil.rmtree(normal_task_dir.parent, ignore_errors=True)

        application = apply_unified_diff(
            self.task.kernel_bytes,
            patch,
            kernel_name=self.task.kernel_name,
            limits=task_patch_limits(self.task, PatchLimits(max_changed_lines=30, max_hunks=4)),
            task=self.task,
        )
        self.assertTrue(task_patch_limits(self.task, PatchLimits()).allow_full_file_replacement)
        self.assertGreater(application.additions + application.deletions, 30)
        self.assertIn(b"static int clamp_value", application.patched_bytes)

    def test_generation_cannot_modify_header_testbench_or_top_signature(self) -> None:
        legal = _large_legal_source()
        limits = task_patch_limits(self.task, PatchLimits(max_changed_lines=30, max_hunks=4))
        for target in ("matrix_transform.h", "matrix_transform_tb.cpp"):
            with self.subTest(target=target), self.assertRaises(PatchValidationError):
                apply_unified_diff(
                    self.task.kernel_bytes,
                    _patch(self.task.kernel_code, legal, target=target),
                    kernel_name=self.task.kernel_name,
                    limits=limits,
                    task=self.task,
                )
        signature_changed = legal.replace("void matrix_transform(", "int matrix_transform(")
        with self.assertRaises(PatchValidationError):
            apply_unified_diff(
                self.task.kernel_bytes,
                _patch(self.task.kernel_code, signature_changed),
                kernel_name=self.task.kernel_name,
                limits=limits,
                task=self.task,
            )

    def test_unknown_task_type_fails_closed(self) -> None:
        root = Path(tempfile.mkdtemp()) / "unsupported"
        try:
            shutil.copytree(FIXTURE, root)
            task_toml = root / "task.toml"
            task_toml.write_text(
                task_toml.read_text(encoding="utf-8").replace(
                    'task_type = "generate"', 'task_type = "unknown_generation"'
                ),
                encoding="utf-8",
            )
            with self.assertRaises(TaskPackageError):
                load_public_task(root)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_planner_context_uses_only_public_kernel_description_and_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copied_fixture = root / "public_task"
            shutil.copytree(FIXTURE, copied_fixture)
            forbidden_marker = "FORBIDDEN_PRIVATE_MARKER_MUST_NOT_REACH_PLANNER"
            (copied_fixture / "hidden").mkdir()
            (copied_fixture / "hidden" / "private.txt").write_text(
                forbidden_marker, encoding="utf-8"
            )
            task = load_public_task(copied_fixture)
            run_root = root / "run"
            source_path = run_root / "candidates" / "candidate_000" / task.kernel_name
            source_path.parent.mkdir(parents=True)
            source_path.write_bytes(task.kernel_bytes)
            source_sha = hashlib.sha256(task.kernel_bytes).hexdigest()
            candidate = {
                "candidate_id": "candidate_000",
                "parent_id": None,
                "kind": "baseline",
                "status": "BASELINE",
                "source": {"ref": str(source_path.relative_to(run_root)), "sha256": source_sha},
                "code_hash": source_sha,
                "metrics": {"ref": None, "sha256": None},
                "synth_evidence": {"ref": None, "sha256": None},
                "validation": {},
            }
            planner_input = build_planner_input(
                task={
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "top": task.top,
                    "kernel_file": task.kernel_name,
                    "public_tb": task.public_tb_name,
                    "description": task.description,
                    "generation_required": task.generation_required,
                    "requires_cosim": task.requires_cosim,
                    "part": task.part,
                    "clock_ns": task.clock_ns,
                    "initial_condition": task.initial_condition,
                },
                round_state={
                    "round_index": 1,
                    "rounds_completed": 0,
                    "parent_candidate_id": "candidate_000",
                    "mode": "REPAIR",
                    "failure_evidence": {
                        "schema_version": "v3c.csim-failure-evidence.v1",
                        "failure_kind": "runtime_fail",
                        "error_summary": "public testbench reports output mismatch",
                        "source_locations": ["kernel.cpp:8"],
                        "relevant_log_lines": ["expected public transform output"],
                    },
                },
                incumbent=candidate,
                baseline=candidate,
                history=[],
                policy={},
                budget={"tokens_remaining": 6000, "credits_remaining": 80},
            )
            provider = OpenAICompatibleOptimizationProvider(
                OpenAICompatibleConfig(
                    base_url="https://llm.example/v1",
                    api_key="fixture-secret",
                    model="fixture-model",
                    max_output_tokens=1200,
                )
            )
            planner = OpenAICompatibleV3PlannerAdapter(
                run_root,
                provider,
                fast_experiment=True,
                read_only_headers={
                    name: content.decode("utf-8") for name, content in task.headers.items()
                },
            )
            prepared = planner.prepare(planner_input)
            context = prepared.dispatch_context
            self.assertIsInstance(context, dict)
            serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
            self.assertEqual(context["current_kernel"], task.kernel_code)
            self.assertEqual(context["description"], task.description)
            self.assertEqual(
                context["read_only_headers"]["matrix_transform.h"],
                task.headers["matrix_transform.h"].decode("utf-8"),
            )
            self.assertNotIn(task.public_tb_bytes.decode("utf-8"), serialized)
            self.assertNotIn(forbidden_marker, serialized)
            self.assertNotIn(forbidden_marker, json.dumps(prepared.request))


if __name__ == "__main__":
    unittest.main()
