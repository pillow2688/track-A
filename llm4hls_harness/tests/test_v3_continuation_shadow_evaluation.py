from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_continuation_shadow_evaluation import (
    collect_online_shadow_samples,
    evaluate_online_shadow_runs,
    evaluate_shadow_samples,
)
from llm4hls_agent.v3_continuation_v2 import continuation_decision_v2


def sample(
    mode: str,
    outcome: str,
    *,
    v1: str,
    v2: str,
) -> dict[str, object]:
    return {
        "sample_id": f"{mode}:{outcome}",
        "mode": mode,
        "v1_decision": {"decision": v1},
        "v2_decision": {"decision": v2},
        "outcome": {"label": outcome},
        "leakage_violations": 0,
    }


class ContinuationShadowEvaluationTests(unittest.TestCase):
    def test_fixed_gate_passes_only_with_four_modes_and_safe_retention(self) -> None:
        value = evaluate_shadow_samples(
            [
                sample("REPAIR", "NEUTRAL", v1="ALLOW", v2="BLOCK"),
                sample(
                    "SYNTH_FIX",
                    "BENEFICIAL_PERFORMANCE",
                    v1="ALLOW",
                    v2="ALLOW",
                ),
                sample(
                    "STRUCTURAL_FIX",
                    "ESSENTIAL_FOR_CORRECTNESS",
                    v1="ALLOW",
                    v2="ALLOW",
                ),
                sample("OPTIMIZE", "HARMFUL", v1="BLOCK", v2="BLOCK"),
            ]
        )
        self.assertEqual(value["decision"], "PASS")
        self.assertEqual(value["metrics"]["false_blocks"], 0)
        self.assertEqual(
            value["metrics"]["structural_essential_retention"], 1.0
        )
        self.assertGreater(
            value["metrics"]["waste_block_rate"],
            value["metrics"]["v1_waste_block_rate"],
        )

    def test_missing_mode_and_beneficial_false_block_fail(self) -> None:
        value = evaluate_shadow_samples(
            [
                sample(
                    "OPTIMIZE",
                    "BENEFICIAL_PERFORMANCE",
                    v1="ALLOW",
                    v2="BLOCK",
                )
            ]
        )
        self.assertEqual(value["decision"], "FAIL")
        self.assertIn("PER_MODE_SAMPLE_GATE", value["gate_failures"])
        self.assertIn("FALSE_BLOCK_GATE", value["gate_failures"])

    def test_collector_recomputes_hash_bound_v2_before_joining_outcome(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate_dir = root / "planner" / "call_gates"
            gate_dir.mkdir(parents=True)
            pre_state = {
                "previous_latency": 120,
                "current_latency": 100,
                "final_reserve_available": True,
                "remaining_tokens": 1000,
                "remaining_credits": 50,
                "remaining_rounds": 1,
                "has_verified_incumbent": True,
            }
            gate = continuation_decision_v2(
                mode="OPTIMIZE",
                pre_state=pre_state,
            )
            gate.update(
                {
                    "run_id": root.name,
                    "round_index": 2,
                    "policy_mode": "shadow",
                    "policy_version": "v3.continuation-policy.v2",
                    "pre_state": pre_state,
                    "decision_hash": "f" * 64,
                }
            )
            gate_path = gate_dir / "round_002.json"
            gate_path.write_text(
                json.dumps(gate, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result = {
                "backend": {
                    "class": "llm4hls_agent.vitis.VitisBackend",
                    "evidence_level": "REAL_VITIS_VALIDATED",
                },
                "task_id": "public_opt",
                "mode": "OPTIMIZE",
                "continuation_policy_mode": "shadow",
                "continuation_policy_version": "v2",
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
                        "decision": "PROMOTED",
                        "latency_worst": 80,
                    },
                ],
            }
            (root / "v3_prototype_result.json").write_text(
                json.dumps(result, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            samples, excluded = collect_online_shadow_samples([root])
            self.assertEqual(excluded, {})
            self.assertEqual(len(samples), 1)
            self.assertEqual(
                samples[0]["outcome"]["label"], "BENEFICIAL_PERFORMANCE"
            )
            self.assertNotIn("outcome", samples[0]["pre_state"])

            output = root / "evaluation.json"
            admission = root / "admission.json"
            value = evaluate_online_shadow_runs(
                [root],
                output_path=output,
            )
            self.assertEqual(value["decision"], "FAIL")
            self.assertTrue(output.is_file())
            with self.assertRaisesRegex(ValueError, "Gate failed"):
                evaluate_online_shadow_runs(
                    [root],
                    admission_path=admission,
                )
            self.assertFalse(admission.exists())


if __name__ == "__main__":
    unittest.main()
