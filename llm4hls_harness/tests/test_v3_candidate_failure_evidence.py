from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llm4hls_agent.tools import ToolResult
from llm4hls_agent.v3_prototype import (
    _candidate_cosim,
    _candidate_failure_evidence_update,
    _continuation_evidence,
)


def failed_result(kind: str, phase: str, diagnostic: str) -> ToolResult:
    return ToolResult(
        kind=kind,
        ok=False,
        phase=phase,
        return_code=1,
        elapsed_s=0.1,
        effective_timeout_seconds=10.0,
        action_id=f"{kind}-candidate-action",
        candidate_id="candidate_002",
        code_hash="code-hash",
        tool_config_hash="config-hash",
        backend_fingerprint="backend",
        task_fingerprint="task",
        result_ref=f"actions/{kind}-candidate-action/result.json",
        cached=False,
        evidence=[diagnostic],
        artifacts={},
        artifact_hashes={},
        report=None,
        cosim=None,
    )


class CandidateFailureEvidenceTests(unittest.TestCase):
    def test_candidate_cosim_failure_is_persisted_for_next_round(self) -> None:
        result = failed_result(
            "cosim",
            "timeout",
            "subprocess timeout expired",
        )
        record = {
            **result.to_dict(),
            "status": "TIMEOUT",
            "ok": False,
        }
        event: dict[str, object] = {"details": {"validation_scope": "exploration"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = SimpleNamespace(run_root=root)
            state = {
                "active_candidate_id": "candidate_002",
                "mode": "STRUCTURAL_FIX",
                "round_index": 2,
                "cosim_gate": {"eligible": True, "reason": "READY"},
            }
            with (
                patch(
                    "llm4hls_agent.v3_prototype._run_tool",
                    return_value=(result, record, event),
                ),
                patch("llm4hls_agent.v3_prototype._save_validation"),
            ):
                update = _candidate_cosim(runtime, state)

            evidence_path = root / str(update["failure_evidence_ref"])
            stored = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(update["last_tool_ok"], False)
        self.assertEqual(
            update["cosim_gate"]["reason"],
            "CANDIDATE_COSIM_FAILED",
        )
        self.assertEqual(
            update["failure_evidence_ref"],
            "evidence/failures/candidate_002_cosim.json",
        )
        self.assertEqual(stored["candidate_id"], "candidate_002")
        self.assertEqual(stored["failure_kind"], "TIMEOUT")
        self.assertEqual(
            event["details"]["failure_evidence_ref"],
            update["failure_evidence_ref"],
        )

    def test_latest_synth_failure_is_persisted_and_bound_for_next_round(self) -> None:
        result = failed_result(
            "synth",
            "synth_error",
            (
                "ERROR: Unsupported std function at "
                "/home/private/kernel.cpp:19"
            ),
        )
        event: dict[str, object] = {"details": {"existing": True}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            update = _candidate_failure_evidence_update(
                SimpleNamespace(run_root=root),
                candidate_id="candidate_002",
                stage="synth",
                result=result,
                validation_evidence=result.to_dict(),
                event=event,
            )
            evidence_path = root / str(update["failure_evidence_ref"])
            stored_bytes = evidence_path.read_bytes()
            stored = json.loads(stored_bytes)

        self.assertEqual(
            update["failure_evidence_ref"],
            "evidence/failures/candidate_002_synth.json",
        )
        self.assertEqual(stored["candidate_id"], "candidate_002")
        self.assertEqual(stored["failure_kind"], "SYNTH_ERROR")
        self.assertIn("Unsupported std function", stored["synthesis_error"])
        self.assertNotIn("/home/private", json.dumps(stored))
        self.assertEqual(
            update["failure_evidence_sha256"],
            hashlib.sha256(stored_bytes).hexdigest(),
        )
        self.assertEqual(event["details"]["existing"], True)
        self.assertEqual(
            event["details"]["failure_evidence_ref"],
            update["failure_evidence_ref"],
        )

    def test_candidate_identity_mismatch_fails_closed(self) -> None:
        result = failed_result(
            "csim",
            "compile_error",
            "ERROR: kernel.cpp:3: missing declaration",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                RuntimeError, "failure evidence identity mismatch"
            ):
                _candidate_failure_evidence_update(
                    SimpleNamespace(run_root=Path(directory)),
                    candidate_id="candidate_003",
                    stage="csim",
                    result=result,
                    validation_evidence=result.to_dict(),
                )

    def test_continuation_compares_previous_and_latest_candidate_failures(self) -> None:
        baseline = {
            "schema_version": "v3c.synth-failure-evidence.v1",
            "candidate_id": "candidate_000",
            "failure_kind": "SYNTH_ERROR",
            "synthesis_error": "recursive call",
        }
        first = {
            "schema_version": "v3c.csim-failure-evidence.v1",
            "candidate_id": "candidate_001",
            "failure_kind": "COMPILE_ERROR",
            "error_summary": "missing include",
        }
        second = {
            "schema_version": "v3c.synth-failure-evidence.v1",
            "candidate_id": "candidate_002",
            "failure_kind": "SYNTH_ERROR",
            "synthesis_error": "unsupported std function",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure_dir = root / "evidence" / "failures"
            failure_dir.mkdir(parents=True)
            (failure_dir / "baseline_synth.json").write_text(
                json.dumps(baseline),
                encoding="utf-8",
            )
            (failure_dir / "candidate_001_csim.json").write_text(
                json.dumps(first),
                encoding="utf-8",
            )
            (failure_dir / "candidate_002_synth.json").write_text(
                json.dumps(second),
                encoding="utf-8",
            )
            registry = {
                "candidates": {
                    "candidate_000": {
                        "candidate_id": "candidate_000",
                        "kind": "baseline",
                    },
                    "candidate_001": {
                        "candidate_id": "candidate_001",
                        "kind": "synth_fix",
                        "round_index": 1,
                        "failure_evidence_ref": (
                            "evidence/failures/candidate_001_csim.json"
                        ),
                    },
                    "candidate_002": {
                        "candidate_id": "candidate_002",
                        "kind": "synth_fix",
                        "round_index": 2,
                        "failure_evidence_ref": (
                            "evidence/failures/candidate_002_synth.json"
                        ),
                    },
                }
            }
            (root / "candidate_registry.json").write_text(
                json.dumps(registry),
                encoding="utf-8",
            )
            runtime = SimpleNamespace(run_root=root, task=SimpleNamespace())
            before, after, _, _ = _continuation_evidence(
                runtime,
                {
                    "mode": "SYNTH_FIX",
                    "best_metrics_ref": "",
                    "best_candidate_id": "candidate_000",
                    "failure_evidence": second,
                },
                registry["candidates"]["candidate_002"],
            )

        self.assertEqual(before, first)
        self.assertEqual(after, second)


if __name__ == "__main__":
    unittest.main()
