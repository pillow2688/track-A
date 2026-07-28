from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_continuation_admission import (
    CONTINUATION_MODES,
    CONTINUATION_POLICY_VERSION,
    CONTINUATION_PROTOCOL_VERSION,
    current_repository_commit,
    load_continuation_admission,
)
from llm4hls_agent.v3_continuation_offline_gate import (
    REPLAY_MANIFEST_SCHEMA,
    build_offline_gate,
    write_gate_and_admission,
)


def pre_state(mode: str) -> dict[str, object]:
    delta = {
        "new_actionable_evidence": mode in {"SYNTH_FIX", "STRUCTURAL_FIX"},
        "failure_subtype_changed": mode in {"SYNTH_FIX", "STRUCTURAL_FIX"},
        "failure_location_changed": False,
        "bottleneck_changed": False,
        "latency_improved": mode == "OPTIMIZE",
        "interval_improved": mode == "OPTIMIZE",
        "structural_strategy_new": mode == "STRUCTURAL_FIX",
        "same_failure_signature": False,
        "same_observed_strategy": False,
    }
    strategy = {
        "REPAIR": ["FUNCTIONAL_REPAIR"],
        "SYNTH_FIX": ["SYNTHESIS_REPAIR"],
        "STRUCTURAL_FIX": ["FIFO_SIZING"],
        "OPTIMIZE": ["LOOP_UNROLL"],
    }[mode]
    return {
        "previous_latency": 100,
        "current_latency": 90 if mode == "OPTIMIZE" else None,
        "evidence_delta": delta,
        "current_observed_strategy": strategy,
        "observed_strategy_history": [],
        "strategy_novelty": "HIGH",
        "remaining_tokens": 10000,
        "remaining_credits": 100,
        "remaining_rounds": 2,
        "estimated_next_tokens": 1000,
        "estimated_next_credits": 5,
        "final_reserve_available": True,
        "has_verified_incumbent": mode == "OPTIMIZE",
        "evidence_complete": True,
    }


def write_run(root: Path, mode: str) -> str:
    run = root / "runs" / mode.lower()
    gate_dir = run / "planner" / "call_gates"
    gate_dir.mkdir(parents=True)
    gate_path = gate_dir / "round_002.json"
    gate_path.write_text(
        json.dumps(
            {
                "schema_version": "v3.continuation-decision.v2",
                "policy_version": "v3.continuation-policy.v2",
                "pre_state": pre_state(mode),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    result_path = run / "v3_prototype_result.json"
    result_path.write_text(
        json.dumps(
            {
                "backend": {
                    "class": "llm4hls_agent.vitis.VitisBackend",
                    "evidence_level": "REAL_VITIS_VALIDATED",
                },
                "task_id": f"public_{mode.lower()}",
                "mode": mode,
                "planner_call_gates": [
                    {
                        "round": 2,
                        "ref": "planner/call_gates/round_002.json",
                        "sha256": hashlib.sha256(
                            gate_path.read_bytes()
                        ).hexdigest(),
                    }
                ],
                "candidate_rounds": [
                    {
                        "round": 1,
                        "candidate_id": "candidate_001",
                        "decision": "PROMOTED",
                        "latency_worst": 100,
                    },
                    {
                        "round": 2,
                        "candidate_id": "candidate_002",
                        "decision": "FINAL_VERIFIED",
                        "latency_worst": 50,
                    },
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(result_path.relative_to(root))


def write_manifest(root: Path) -> Path:
    manifest = {
        "schema_version": REPLAY_MANIFEST_SCHEMA,
        "protocol_version": CONTINUATION_PROTOCOL_VERSION,
        "samples": [
            {"run_result_path": write_run(root, mode), "round_index": 2}
            for mode in CONTINUATION_MODES
        ],
    }
    path = root / "replay-manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


class ContinuationOfflineGateTests(unittest.TestCase):
    def test_four_real_modes_replay_to_pass_without_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = write_manifest(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            gate = build_offline_gate(
                manifest,
                harness_root=root,
                current_commit=current_repository_commit(),
            )

        self.assertEqual(gate["gate_status"], "PASS")
        self.assertEqual(
            gate["mode_coverage"],
            {mode: 1 for mode in CONTINUATION_MODES},
        )
        self.assertEqual(gate["metrics"]["false_blocks"], 0)
        self.assertEqual(gate["metrics"]["leakage_violations"], 0)
        self.assertEqual(gate["metrics"]["essential_samples"], 3)
        self.assertEqual(gate["metrics"]["beneficial_samples"], 1)
        self.assertTrue(
            all(
                sample["current_decision"]["decision"] == "ALLOW"
                for sample in gate["audited_samples"]
            )
        )
        self.assertTrue(
            all(
                sample["pre_state_migrations"]
                == [
                    "final_reserve_available"
                    "->search_closeout_reserve_available"
                ]
                for sample in gate["audited_samples"]
            )
        )

    def test_generation_writes_content_bound_current_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            artifact = root / "artifact"
            gate_path = artifact / "continuation-v3-gate.json"
            admission_path = artifact / "continuation-v3-admission.json"
            gate, admission = write_gate_and_admission(
                replay_manifest_path=manifest,
                gate_path=gate_path,
                admission_path=admission_path,
                harness_root=root,
            )
            loaded = load_continuation_admission(admission_path)

        self.assertEqual(gate["gate_status"], "PASS")
        self.assertEqual(
            admission["policy_version"], CONTINUATION_POLICY_VERSION
        )
        self.assertEqual(loaded, admission)

    def test_future_outcome_in_pre_state_causes_leakage_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = write_manifest(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result_path = root / manifest["samples"][0]["run_result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            gate_path = (
                result_path.parent
                / result["planner_call_gates"][0]["ref"]
            )
            source_gate = json.loads(gate_path.read_text(encoding="utf-8"))
            source_gate["pre_state"]["future_candidate"] = "forbidden"
            gate_path.write_text(
                json.dumps(source_gate, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result["planner_call_gates"][0]["sha256"] = hashlib.sha256(
                gate_path.read_bytes()
            ).hexdigest()
            result_path.write_text(
                json.dumps(result, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            gate = build_offline_gate(
                manifest,
                harness_root=root,
                current_commit=current_repository_commit(),
            )

        self.assertEqual(gate["gate_status"], "FAIL")
        self.assertEqual(gate["metrics"]["leakage_violations"], 1)


if __name__ == "__main__":
    unittest.main()
