from __future__ import annotations

import difflib
import unittest
from pathlib import Path

from llm4hls_agent.repair import PatchValidationError, apply_unified_diff
from llm4hls_agent.task import load_public_task
from llm4hls_agent.top_interface_guard import TopInterfaceGuard


BASELINE = '''#include "kernel.h"
extern "C" int public_scale(int value) { return value * 2; }

void kernel(const int input[16], unsigned short scale, int output[16]) {
#pragma HLS INTERFACE ap_memory port=input
    for (int i = 0; i < 16; ++i) {
        output[i] = public_scale(input[i]) * scale;
    }
}
'''

HEADER = '''extern "C" int public_scale(int value);
void kernel(const int input[16], unsigned short scale, int output[16]);
'''


def guard() -> TopInterfaceGuard:
    return TopInterfaceGuard(
        top="kernel",
        kernel_name="kernel.cpp",
        baseline_source=BASELINE,
        headers={"kernel.h": HEADER},
        public_tb_name="kernel_tb.cpp",
    )


def reason_codes(result) -> set[str]:
    return {reason.code for reason in result.reasons}


class TopInterfaceGuardTests(unittest.TestCase):
    def test_local_loop_change_preserves_normalized_interface(self) -> None:
        candidate = BASELINE.replace("++i", "i += 1").replace(
            "const int input[16]", "int const * input", 1
        ).replace("int output[16]", "int *output", 1)

        result = guard().check(candidate)

        self.assertTrue(result.allowed)
        self.assertTrue(result.parse_reliable)
        self.assertEqual(result.risk_level, "LOW")
        self.assertEqual(result.baseline_top.parameters[0].type, "const int*")
        self.assertEqual(result.candidate_top.parameters[0].type, "const int*")

    def test_top_parameter_count_type_order_and_return_changes_are_rejected(self) -> None:
        cases = {
            "count": (
                BASELINE.replace(
                    "unsigned short scale, int output[16]", "int output[16]"
                ),
                "TOP_PARAMETER_COUNT_CHANGED",
            ),
            "type": (
                BASELINE.replace("unsigned short scale", "unsigned int scale"),
                "TOP_PARAMETER_TYPE_CHANGED",
            ),
            "order": (
                BASELINE.replace(
                    "const int input[16], unsigned short scale, int output[16]",
                    "unsigned short scale, const int input[16], int output[16]",
                ),
                "TOP_PARAMETER_TYPE_CHANGED",
            ),
            "return": (
                BASELINE.replace("void kernel", "int kernel"),
                "TOP_RETURN_TYPE_CHANGED",
            ),
        }
        for name, (candidate, expected_code) in cases.items():
            with self.subTest(name=name):
                result = guard().check(candidate)
                self.assertFalse(result.allowed)
                self.assertIn(expected_code, reason_codes(result))

    def test_parameter_name_only_renames_preserve_the_top_abi(self) -> None:
        candidate = BASELINE.replace(
            "const int input[16], unsigned short scale, int output[16]",
            "const int samples[16], unsigned short gain, int results[16]",
        ).replace(
            "output[i] = public_scale(input[i]) * scale;",
            "results[i] = public_scale(samples[i]) * gain;",
        )

        result = guard().check(candidate)

        self.assertTrue(result.allowed)
        self.assertTrue(result.parse_reliable)
        self.assertEqual(
            ["samples", "gain", "results"],
            [parameter.name for parameter in result.candidate_top.parameters],
        )
        self.assertEqual(
            [parameter.type for parameter in result.baseline_top.parameters],
            [parameter.type for parameter in result.candidate_top.parameters],
        )

    def test_renamed_or_deleted_top_is_rejected(self) -> None:
        renamed = guard().check(BASELINE.replace("void kernel", "void renamed"))
        deleted = guard().check(BASELINE[: BASELINE.index("void kernel")])

        self.assertFalse(renamed.allowed)
        self.assertIn("TOP_FUNCTION_MISSING", reason_codes(renamed))
        self.assertFalse(deleted.allowed)
        self.assertIn("TOP_FUNCTION_MISSING", reason_codes(deleted))

    def test_required_extern_public_symbol_is_preserved(self) -> None:
        candidate = BASELINE.replace(
            'extern "C" int public_scale(int value) { return value * 2; }\n',
            "",
        )

        result = guard().check(candidate)

        self.assertFalse(result.allowed)
        self.assertIn("REQUIRED_PUBLIC_SYMBOL_MISSING", reason_codes(result))
        self.assertTrue(any("public_scale" in item for item in result.required_public_symbols))

    def test_header_and_testbench_changes_are_rejected(self) -> None:
        for changed_file in ("kernel.h", "kernel_tb.cpp", "other.cpp", "../kernel.cpp"):
            with self.subTest(changed_file=changed_file):
                result = guard().check(BASELINE, changed_files=(changed_file,))
                self.assertFalse(result.allowed)
                self.assertIn("PROTECTED_FILE_CHANGED", reason_codes(result))

    def test_interface_pragma_change_is_allowed_but_high_risk(self) -> None:
        candidate = BASELINE.replace("ap_memory port=input", "m_axi port=input")

        result = guard().check(candidate)

        self.assertTrue(result.allowed)
        self.assertTrue(result.high_risk)
        self.assertTrue(result.requires_cosim)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertIn("INTERFACE_PRAGMA_CHANGED", reason_codes(result))
        pragma_reason = next(
            reason
            for reason in result.reasons
            if reason.code == "INTERFACE_PRAGMA_CHANGED"
        )
        self.assertFalse(pragma_reason.blocking)

    def test_unreliable_candidate_parse_fails_closed_with_structured_reason(self) -> None:
        result = guard().check(BASELINE + "\n/* unterminated")

        self.assertFalse(result.allowed)
        self.assertFalse(result.parse_reliable)
        reason = next(reason for reason in result.reasons if reason.code == "PARSE_UNCERTAIN")
        self.assertIsInstance(reason.actual, dict)
        self.assertEqual(reason.actual["side"], "candidate")

    def test_shared_patch_path_blocks_top_change_before_materialization(self) -> None:
        task = load_public_task(
            Path(__file__).parents[1] / "examples" / "u55c_repair_task"
        )
        original = task.kernel_code
        candidate = original.replace("void vector_add", "int vector_add", 1)
        patch = "".join(
            difflib.unified_diff(
                original.splitlines(True),
                candidate.splitlines(True),
                fromfile="a/kernel.cpp",
                tofile="b/kernel.cpp",
            )
        )

        with self.assertRaisesRegex(
            PatchValidationError, "TOP_RETURN_TYPE_CHANGED"
        ) as caught:
            apply_unified_diff(
                task.kernel_bytes,
                patch,
                kernel_name=task.kernel_name,
                task=task,
            )
        self.assertIsNotNone(caught.exception.interface_guard)
        self.assertIn(
            "TOP_RETURN_TYPE_CHANGED",
            reason_codes(caught.exception.interface_guard),
        )


if __name__ == "__main__":
    unittest.main()
