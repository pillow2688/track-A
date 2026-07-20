from __future__ import annotations

import json
import unittest

from llm4hls_agent.tools import ToolResult
from llm4hls_agent.v3_failure_evidence import (
    MAX_LOG_LINE_CHARS,
    MAX_LOG_LINES,
    COSIM_FAILURE_EVIDENCE_SCHEMA,
    CSIM_FAILURE_EVIDENCE_SCHEMA,
    SYNTH_FAILURE_EVIDENCE_SCHEMA,
    extract_cosim_failure_evidence,
    extract_csim_failure_evidence,
    extract_synth_failure_evidence,
)


def tool_result(
    kind: str,
    *,
    phase: str,
    ok: bool = False,
    evidence: list[str] | None = None,
    report: dict[str, object] | None = None,
    cosim: dict[str, object] | None = None,
) -> ToolResult:
    return ToolResult(
        kind=kind,
        ok=ok,
        phase=phase,
        return_code=0 if ok else 1,
        elapsed_s=0.1,
        effective_timeout_seconds=10.0,
        action_id=f"{kind}-action",
        candidate_id="candidate_007",
        code_hash="code-hash",
        tool_config_hash="config-hash",
        backend_fingerprint="backend",
        task_fingerprint="task",
        result_ref=f"actions/{kind}-action/result.json",
        cached=False,
        evidence=evidence or [],
        artifacts={},
        artifact_hashes={},
        report=report,
        cosim=cosim,
    )


class CSimFailureEvidenceTests(unittest.TestCase):
    def test_extracts_compile_location_and_expected_actual_without_full_log(self) -> None:
        result = tool_result(
            "csim",
            phase="compile_error",
            evidence=[
                "unrelated compiler banner " + ("x" * 5000),
                (
                    "ERROR: /home/runner/private/task/projection.cpp:17:5: "
                    "output mismatch; expected=42, actual=41"
                ),
            ],
        )

        evidence = extract_csim_failure_evidence(result)
        payload = evidence.to_dict()

        self.assertEqual(payload["schema_version"], CSIM_FAILURE_EVIDENCE_SCHEMA)
        self.assertEqual(payload["failure_kind"], "COMPILE_ERROR")
        self.assertEqual(payload["source_locations"], ["projection.cpp:17:5"])
        self.assertEqual(payload["expected"], "42")
        self.assertEqual(payload["actual"], "41")
        self.assertNotIn("/home/", json.dumps(payload))
        self.assertNotIn("compiler banner", payload["relevant_log_lines"])
        self.assertLessEqual(len(payload["relevant_log_lines"]), MAX_LOG_LINES)
        self.assertTrue(
            all(len(line) <= MAX_LOG_LINE_CHARS for line in payload["relevant_log_lines"])
        )

    def test_runtime_failure_does_not_invent_a_mismatch(self) -> None:
        evidence = extract_csim_failure_evidence(
            tool_result(
                "csim",
                phase="runtime_fail",
                evidence=["public test returned non-zero without diagnostics"],
            )
        )

        self.assertEqual(evidence.failure_kind, "RUNTIME_FAIL")
        self.assertIsNone(evidence.expected)
        self.assertIsNone(evidence.actual)
        self.assertEqual(evidence.source_locations, ())


class SynthFailureEvidenceTests(unittest.TestCase):
    def test_extracts_unsupported_construct_clock_and_resource_facts(self) -> None:
        result = tool_result(
            "synth",
            phase="synth_error",
            evidence=[
                "ERROR: unsupported dynamic memory allocation at /work/kernel.cpp:22",
                "WARNING: failed to meet clock constraint",
            ],
            report={
                "estimated_clock_period_ns": 11.5,
                "resources": {"LUT": 101, "DSP": 3},
                "available_resources": {"LUT": 100, "DSP": 10},
            },
        )

        evidence = extract_synth_failure_evidence(
            result,
            validation_evidence={"target_clock_period_ns": 10.0},
        )
        payload = evidence.to_dict()

        self.assertEqual(payload["schema_version"], SYNTH_FAILURE_EVIDENCE_SCHEMA)
        self.assertEqual(payload["failure_kind"], "SYNTH_ERROR")
        self.assertEqual(payload["source_locations"], ["kernel.cpp:22"])
        self.assertEqual(len(payload["unsupported_constructs"]), 1)
        self.assertEqual(
            payload["clock_violation"],
            {
                "estimated_clock_period_ns": 11.5,
                "target_clock_period_ns": 10.0,
            },
        )
        self.assertEqual(
            payload["resource_violations"],
            [{"resource": "LUT", "used": 101.0, "available": 100.0}],
        )

    def test_generic_synth_error_does_not_invent_specific_causes(self) -> None:
        evidence = extract_synth_failure_evidence(
            tool_result("synth", phase="synth_error", evidence=["return_code=1"])
        )

        self.assertEqual(evidence.failure_kind, "SYNTH_ERROR")
        self.assertEqual(evidence.unsupported_constructs, ())
        self.assertIsNone(evidence.clock_violation)
        self.assertEqual(evidence.resource_violations, ())


class CoSimFailureEvidenceTests(unittest.TestCase):
    def test_extracts_explicit_deadlock_and_stream_fifo_information(self) -> None:
        result = tool_result(
            "cosim",
            phase="cosim_fail",
            evidence=[
                "ERROR: RTL deadlock detected in /tmp/private/residual.cpp:31",
                "FIFO channel residual_fifo is full; hls::stream producer is blocked",
                *[f"FIFO diagnostic {index}: " + ("z" * 500) for index in range(20)],
            ],
            cosim={"status": "Fail"},
        )

        evidence = extract_cosim_failure_evidence(result)
        payload = evidence.to_dict()

        self.assertEqual(payload["schema_version"], COSIM_FAILURE_EVIDENCE_SCHEMA)
        self.assertEqual(payload["failure_kind"], "DEADLOCK")
        self.assertTrue(payload["deadlock"])
        self.assertFalse(payload["timeout"])
        self.assertFalse(payload["rtl_mismatch"])
        self.assertEqual(payload["source_locations"], ["residual.cpp:31"])
        self.assertLessEqual(len(payload["stream_fifo_interface_findings"]), MAX_LOG_LINES)
        self.assertLessEqual(len(payload["relevant_log_lines"]), MAX_LOG_LINES)
        self.assertTrue(
            all(len(line) <= MAX_LOG_LINE_CHARS for line in payload["relevant_log_lines"])
        )
        self.assertNotIn("/tmp/", json.dumps(payload))

    def test_timeout_is_not_automatically_called_a_deadlock(self) -> None:
        evidence = extract_cosim_failure_evidence(
            tool_result(
                "cosim",
                phase="timeout",
                evidence=["subprocess timeout expired"],
            )
        )

        self.assertEqual(evidence.failure_kind, "TIMEOUT")
        self.assertTrue(evidence.timeout)
        self.assertFalse(evidence.deadlock)
        self.assertFalse(evidence.rtl_mismatch)

    def test_negated_symptoms_are_not_reported_as_observed(self) -> None:
        evidence = extract_cosim_failure_evidence(
            tool_result(
                "cosim",
                phase="cosim_fail",
                evidence=["No deadlock detected; no output mismatch found"],
            )
        )

        self.assertEqual(evidence.failure_kind, "COSIM_FAILURE")
        self.assertFalse(evidence.deadlock)
        self.assertFalse(evidence.rtl_mismatch)

    def test_explicit_expected_actual_is_an_rtl_mismatch(self) -> None:
        evidence = extract_cosim_failure_evidence(
            tool_result(
                "cosim",
                phase="cosim_fail",
                evidence=["RTL mismatch: expected 0x12 but got 0x10"],
                cosim={"status": "Fail"},
            )
        )

        self.assertEqual(evidence.failure_kind, "RTL_MISMATCH")
        self.assertTrue(evidence.rtl_mismatch)
        self.assertEqual(evidence.expected, "0x12")
        self.assertEqual(evidence.actual, "0x10")


class FailureEvidenceInputSafetyTests(unittest.TestCase):
    def test_rejects_a_result_from_the_wrong_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected csim"):
            extract_csim_failure_evidence(tool_result("synth", phase="synth_error"))

    def test_validation_mapping_only_exposes_allowlisted_diagnostics(self) -> None:
        evidence = extract_csim_failure_evidence(
            tool_result("csim", phase="runtime_fail"),
            validation_evidence={
                "error_summary": (
                    "assert failed at kernel.cpp:9; "
                    "OPENAI_API_KEY=sk-super-secret-token"
                ),
                "hidden_testbench": "DO NOT EXPOSE THIS",
                "full_log": "DO NOT EXPOSE THIS EITHER",
                "message": "details saved in /home/runner/private/full.log",
            },
        )
        encoded = json.dumps(evidence.to_dict())

        self.assertIn("assert failed", encoded)
        self.assertNotIn("DO NOT EXPOSE", encoded)
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("sk-super-secret-token", encoded)


if __name__ == "__main__":
    unittest.main()
