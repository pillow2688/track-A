from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_DECISION_SCHEMA,
    CONTINUATION_FIXED_THRESHOLDS,
    CONTINUATION_GATE_SCHEMA,
    CONTINUATION_MODES,
    CONTINUATION_POLICY_VERSION,
    CONTINUATION_PROTOCOL_VERSION,
    current_repository_commit,
    load_continuation_admission,
    validate_continuation_admission,
    validate_continuation_gate,
)


def passing_gate() -> dict[str, object]:
    commit = current_repository_commit()
    samples = [
        {
            "mode": mode,
            "outcome": (
                "BENEFICIAL_PERFORMANCE"
                if mode == "OPTIMIZE"
                else "ESSENTIAL_FOR_CORRECTNESS"
            ),
            "current_decision": {
                "schema_version": CONTINUATION_DECISION_SCHEMA,
                "policy_version": CONTINUATION_POLICY_VERSION,
                "decision": "ALLOW",
            },
            "leakage_violations": 0,
        }
        for mode in CONTINUATION_MODES
    ]
    return {
        "schema_version": CONTINUATION_GATE_SCHEMA,
        "gate_status": "PASS",
        "policy_version": CONTINUATION_POLICY_VERSION,
        "decision_schema": CONTINUATION_DECISION_SCHEMA,
        "current_commit": commit,
        "protocol_version": CONTINUATION_PROTOCOL_VERSION,
        "thresholds": CONTINUATION_FIXED_THRESHOLDS,
        "mode_coverage": {mode: 1 for mode in CONTINUATION_MODES},
        "metrics": {
            "sample_count": 4,
            "false_blocks": 0,
            "leakage_violations": 0,
        },
        "source_manifest_sha256": "a" * 64,
        "audited_samples": samples,
    }


def write_passing_artifacts(root: Path) -> tuple[Path, Path]:
    gate_path = root / "continuation-v3-gate.json"
    gate_path.write_text(
        json.dumps(passing_gate(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    admission = {
        "schema_version": CONTINUATION_ADMISSION_SCHEMA,
        "policy_version": CONTINUATION_POLICY_VERSION,
        "decision_schema": CONTINUATION_DECISION_SCHEMA,
        "current_commit": current_repository_commit(),
        "gate_json_path": gate_path.name,
        "gate_json_sha256": hashlib.sha256(
            gate_path.read_bytes()
        ).hexdigest(),
        "gate_status": gate["gate_status"],
        "protocol_version": CONTINUATION_PROTOCOL_VERSION,
        "mode_coverage": gate["mode_coverage"],
    }
    admission_path = root / "continuation-v3-admission.json"
    admission_path.write_text(
        json.dumps(admission, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return gate_path, admission_path


class ContinuationAdmissionTests(unittest.TestCase):
    def test_current_v3_pass_gate_admission_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _gate, admission = write_passing_artifacts(Path(directory))
            loaded = load_continuation_admission(admission)
        self.assertEqual(loaded["policy_version"], CONTINUATION_POLICY_VERSION)
        self.assertEqual(
            loaded["mode_coverage"],
            {mode: 1 for mode in CONTINUATION_MODES},
        )

    def test_previous_policy_admission_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _gate, admission_path = write_passing_artifacts(Path(directory))
            value = json.loads(admission_path.read_text(encoding="utf-8"))
            value["policy_version"] = "v3.continuation-policy.v2"
            with self.assertRaisesRegex(ValueError, "policy version mismatch"):
                validate_continuation_admission(
                    value,
                    admission_path=admission_path,
                )

    def test_gate_hash_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate_path, admission = write_passing_artifacts(Path(directory))
            gate_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Gate hash mismatch"):
                load_continuation_admission(admission)

    def test_historical_v2_gate_cannot_authorize_current_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate_path, admission_path = write_passing_artifacts(Path(directory))
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["policy_version"] = "v3.continuation-policy.v2"
            gate_path.write_text(
                json.dumps(gate, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            admission = json.loads(admission_path.read_text(encoding="utf-8"))
            admission["gate_json_sha256"] = hashlib.sha256(
                gate_path.read_bytes()
            ).hexdigest()
            admission_path.write_text(
                json.dumps(admission, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Gate policy version mismatch"):
                load_continuation_admission(admission_path)

    def test_gate_status_and_recomputed_false_block_fail_closed(self) -> None:
        gate = passing_gate()
        gate["audited_samples"][0]["current_decision"]["decision"] = "BLOCK"
        gate["metrics"]["false_blocks"] = 1
        gate["gate_status"] = "FAIL"
        with self.assertRaisesRegex(ValueError, "Gate is not PASS"):
            validate_continuation_gate(gate)

    def test_missing_mode_fails_closed(self) -> None:
        gate = copy.deepcopy(passing_gate())
        gate["audited_samples"] = gate["audited_samples"][:-1]
        gate["metrics"]["sample_count"] = 3
        gate["mode_coverage"]["OPTIMIZE"] = 0
        with self.assertRaisesRegex(ValueError, "per-mode sample Gate"):
            validate_continuation_gate(gate)

    def test_pathless_mapping_cannot_bypass_gate_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _gate, admission_path = write_passing_artifacts(Path(directory))
            value = json.loads(admission_path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "path is required"):
                validate_continuation_admission(value)

    def test_commit_and_protocol_mismatch_fail_closed(self) -> None:
        gate = passing_gate()
        gate["current_commit"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "current commit mismatch"):
            validate_continuation_gate(gate)
        gate = passing_gate()
        gate["protocol_version"] = "legacy"
        with self.assertRaisesRegex(ValueError, "protocol version mismatch"):
            validate_continuation_gate(gate)


if __name__ == "__main__":
    unittest.main()
