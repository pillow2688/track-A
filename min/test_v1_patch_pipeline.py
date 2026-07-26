from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import minimal_flow
import v1_patch_pipeline as patch_pipeline
from llm4hls_agent.repair import PatchLimits, PatchValidationError, apply_unified_diff


class V1PatchPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.task = minimal_flow.load_public_task(
            minimal_flow.HARNESS_ROOT / "examples" / "u55c_repair_task"
        )
        cls.raw_patch = (
            minimal_flow.HARNESS_ROOT / "examples" / "u55c_repair.diff"
        ).read_text(encoding="utf-8")

    def test_valid_raw_patch_is_not_rewritten(self) -> None:
        with mock.patch.object(
            patch_pipeline,
            "normalize_patch",
            side_effect=AssertionError("normalization must not run after raw success"),
        ):
            resolution = patch_pipeline.resolve_and_apply_patch(
                task=self.task,
                raw_patch=self.raw_patch,
                max_changed_lines=80,
            )
        self.assertEqual(resolution.selected_stage, patch_pipeline.RAW_STRICT)
        self.assertEqual(resolution.applied_patch, self.raw_patch)
        self.assertEqual(resolution.receipt["attempts"][0]["status"], "APPLIED")

    def test_bare_empty_hunk_line_and_bad_counts_are_normalized(self) -> None:
        patch = """\
--- a/kernel.cpp
+++ b/kernel.cpp
@@ -1,99 +1,77 @@
 #include "kernel.h"

 void vector_add(const int a[16], const int b[16], int c[16]) {
     for (int i = 0; i < 16; ++i) {
-        c[i] = a[i] - b[i];
+        c[i] = a[i] + b[i];
"""
        resolution = patch_pipeline.resolve_and_apply_patch(
            task=self.task,
            raw_patch=patch,
            max_changed_lines=80,
        )
        self.assertEqual(
            resolution.selected_stage,
            patch_pipeline.NORMALIZED_STRICT,
        )
        self.assertIn("\n \n", resolution.normalized_patch)
        self.assertIn("@@ -1,5 +1,5 @@", resolution.normalized_patch)

    def test_wrong_header_location_uses_unique_complete_old_side(self) -> None:
        patch = self.raw_patch.replace("@@ -3,5 +3,5 @@", "@@ -99,5 +99,5 @@")
        resolution = patch_pipeline.resolve_and_apply_patch(
            task=self.task,
            raw_patch=patch,
            max_changed_lines=80,
        )
        self.assertEqual(
            resolution.selected_stage,
            patch_pipeline.EXACT_CONTEXT_RECOVERY,
        )
        self.assertIn("@@ -3,5 +3,5 @@", resolution.applied_patch)
        self.assertEqual(resolution.receipt["attempts"][2]["match_counts"], [1])

    def test_unique_delete_block_rebuilds_only_after_context_zero_match(self) -> None:
        patch = """\
--- a/kernel.cpp
+++ b/kernel.cpp
@@ -3,2 +3,2 @@
 void vector_add(const int a[16], const int b[16], int c[16]) {
-        c[i] = a[i] - b[i];
+        c[i] = a[i] + b[i];
"""
        resolution = patch_pipeline.resolve_and_apply_patch(
            task=self.task,
            raw_patch=patch,
            max_changed_lines=80,
        )
        self.assertEqual(
            resolution.selected_stage,
            patch_pipeline.UNIQUE_DELETE_BLOCK_RECOVERY,
        )
        self.assertEqual(
            resolution.receipt["attempts"][2]["error_code"],
            "EXACT_CONTEXT_ZERO_MATCH",
        )
        self.assertEqual(
            resolution.receipt["attempts"][3]["match_counts"],
            [1],
        )
        self.assertIn("@@ -5,1 +5,1 @@", resolution.applied_patch)

    def test_zero_ambiguous_and_fuzzy_matches_are_distinct_and_rejected(self) -> None:
        source = b"same\nmiddle\nsame\n"
        ambiguous = """\
--- kernel.cpp
+++ kernel.cpp
@@ -1,2 +1,2 @@
 missing
-same
+changed
"""
        normalized = patch_pipeline.normalize_patch(ambiguous)
        with self.assertRaises(patch_pipeline.RecoveryMatchError) as context_error:
            patch_pipeline.relocate_by_full_old_side(source, normalized)
        self.assertEqual(
            context_error.exception.code,
            "EXACT_CONTEXT_ZERO_MATCH",
        )
        with self.assertRaises(patch_pipeline.RecoveryMatchError) as delete_error:
            patch_pipeline.rebuild_by_unique_delete_block(source, normalized)
        self.assertEqual(
            delete_error.exception.code,
            "DELETE_BLOCK_AMBIGUOUS_MATCH",
        )

        fuzzy = ambiguous.replace("-same", "- same")
        with self.assertRaises(patch_pipeline.RecoveryMatchError) as fuzzy_error:
            patch_pipeline.rebuild_by_unique_delete_block(
                source,
                patch_pipeline.normalize_patch(fuzzy),
            )
        self.assertEqual(fuzzy_error.exception.code, "DELETE_BLOCK_ZERO_MATCH")

    def test_malformed_second_header_is_rejected_by_strict_validator(self) -> None:
        patch = self.raw_patch.replace("+++ b/kernel.cpp", "not b/kernel.cpp")
        with self.assertRaises(PatchValidationError):
            apply_unified_diff(
                self.task.kernel_bytes,
                patch,
                kernel_name=self.task.kernel_name,
                limits=PatchLimits(),
                task=self.task,
            )

    def test_extra_file_header_pair_and_unanchored_addition_are_rejected(self) -> None:
        extra_pair = (
            self.raw_patch
            + "--- a/other.cpp\n"
            + "+++ b/other.cpp\n"
            + "@@ -1,1 +1,1 @@\n"
            + "-old\n"
            + "+new\n"
        )
        addition_only = """\
--- a/kernel.cpp
+++ b/kernel.cpp
@@ -1,0 +2,1 @@
+// unanchored insertion
"""
        for patch in (extra_pair, addition_only):
            with self.subTest(patch=patch), self.assertRaises(
                patch_pipeline.PatchResolutionError
            ):
                patch_pipeline.resolve_and_apply_patch(
                    task=self.task,
                    raw_patch=patch,
                    max_changed_lines=80,
                )

    def test_literal_and_operator_interface_directives_are_hard_rejected(self) -> None:
        for directive in (
            "#pragma HLS INTERFACE mode=ap_none port=c",
            '_Pragma("HLS INTERFACE mode=ap_none port=c")',
        ):
            patch = f"""\
--- a/kernel.cpp
+++ b/kernel.cpp
@@ -3,5 +3,6 @@
 void vector_add(const int a[16], const int b[16], int c[16]) {{
     for (int i = 0; i < 16; ++i) {{
-        c[i] = a[i] - b[i];
+        {directive}
+        c[i] = a[i] + b[i];
     }}
 }}
"""
            with self.subTest(directive=directive), self.assertRaises(
                patch_pipeline.PatchResolutionError
            ) as caught:
                patch_pipeline.resolve_and_apply_patch(
                    task=self.task,
                    raw_patch=patch,
                    max_changed_lines=80,
                )
            self.assertIn(
                "top-interface directive changes",
                caught.exception.receipt["attempts"][-1]["error_detail"],
            )
            self.assertIsNotNone(
                caught.exception.receipt["attempts"][-1]["interface_guard"]
            )

    def test_recovery_cannot_bypass_top_interface_guard(self) -> None:
        patch = """\
--- a/kernel.cpp
+++ b/kernel.cpp
@@ -1,2 +1,2 @@
 missing context
-void vector_add(const int a[16], const int b[16], int c[16]) {
+void vector_add(const int a[16], const int b[16], int c[16], int extra) {
"""
        with self.assertRaises(patch_pipeline.PatchResolutionError) as caught:
            patch_pipeline.resolve_and_apply_patch(
                task=self.task,
                raw_patch=patch,
                max_changed_lines=80,
            )
        attempts = caught.exception.receipt["attempts"]
        self.assertEqual(attempts[-1]["stage"], patch_pipeline.UNIQUE_DELETE_BLOCK_RECOVERY)
        self.assertIn("top interface guard rejected", attempts[-1]["error_detail"])

    def test_recovered_candidate_still_runs_csim(self) -> None:
        patch = """\
--- a/kernel.cpp
+++ b/kernel.cpp
@@ -3,2 +3,2 @@
 void vector_add(const int a[16], const int b[16], int c[16]) {
-        c[i] = a[i] - b[i];
+        c[i] = a[i] + b[i];
"""
        with tempfile.TemporaryDirectory() as directory:
            patch_path = Path(directory) / "input.diff"
            patch_path.write_text(patch, encoding="utf-8")
            run_dir = Path(directory) / "run"
            exit_code = minimal_flow.main(
                [
                    "--task-dir",
                    str(minimal_flow.HARNESS_ROOT / "examples" / "u55c_repair_task"),
                    "--patch-file",
                    str(patch_path),
                    "--run-dir",
                    str(run_dir),
                    "--backend",
                    "demo",
                ]
            )
            self.assertEqual(exit_code, 0)
            receipt = (run_dir / "patch" / "patch_resolution.json").read_text(
                encoding="utf-8"
            )
            self.assertIn(patch_pipeline.UNIQUE_DELETE_BLOCK_RECOVERY, receipt)
            trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
            self.assertIn('"kind":"csim"', trace)
            self.assertTrue((run_dir / "candidate_001" / "kernel.cpp").is_file())

    def test_no_public_task_identifier_is_hardcoded_in_v1_implementation(self) -> None:
        implementation = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path(minimal_flow.__file__),
                Path(patch_pipeline.__file__),
            )
        )
        for forbidden in (
            "projection_bugfix",
            "dotProduct_optimize",
            "residual_stream_deadlock",
        ):
            self.assertNotIn(forbidden, implementation)


if __name__ == "__main__":
    unittest.main()
