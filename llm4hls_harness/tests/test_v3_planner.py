from __future__ import annotations

import copy
import unittest

from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.v3_planner import (
    ScriptedPlanner,
    build_planner_input,
    build_planner_output,
    canonical_sha256,
    load_planner_output,
    planner_action_id,
    planner_action_request,
    stable_validation,
)


def proposal(*, interval: int = 1, hypothesis: str = "pipeline the hot loop") -> PatchProposal:
    return PatchProposal(
        patch=(
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,2 +1,3 @@\n"
            " void kernel() {\n"
            f"+#pragma HLS PIPELINE II={interval}\n"
            " }\n"
        ),
        provider="scripted-test",
        model="fixture-v1",
        revision="revision-1",
        input_tokens=17,
        output_tokens=11,
        cached_input_tokens=3,
        request_id="request-1",
        duration_seconds=0.25,
        hypothesis=hypothesis,
        change_class="LOOP_PIPELINE",
        expected_effect=f"Reduce the loop initiation interval to {interval}.",
        risk="low",
        required_validation=("csim", "synth"),
    )


def planner_input(*, round_index: int = 1, incumbent_id: str = "candidate_000") -> dict[str, object]:
    return build_planner_input(
        task={"task_id": "dot_product", "task_type": "optimize"},
        round_state={"round_index": round_index, "max_rounds": 2},
        incumbent={
            "candidate_id": incumbent_id,
            "latency": 1027,
            "evidence_ref": "evidence/candidate_000.synth.json",
        },
        baseline={"candidate_id": "candidate_000", "latency": 1027},
        history=[{"round_index": 0, "decision": "BASELINE"}],
        policy={"objective": "official_score", "requires_cosim": False},
        budget={"remaining_credits": 70, "remaining_tokens": 32000},
    )


class V3PlannerContractTests(unittest.TestCase):
    def test_scripted_planner_uses_round_order_and_has_stable_fingerprint(self) -> None:
        first = proposal(interval=1, hypothesis="first")
        second = proposal(interval=2, hypothesis="second")
        planner = ScriptedPlanner((first, second))

        self.assertEqual(planner.plan(planner_input(round_index=1)), first)
        self.assertEqual(planner.plan(planner_input(round_index=2)), second)
        self.assertEqual(
            planner.fingerprint(),
            ScriptedPlanner((first, second)).fingerprint(),
        )
        self.assertNotEqual(
            planner.fingerprint(),
            ScriptedPlanner((second, first)).fingerprint(),
        )

    def test_action_id_is_stable_and_bound_to_input_and_planner(self) -> None:
        value = planner_input()
        input_hash = canonical_sha256(value)
        fingerprint = ScriptedPlanner((proposal(),)).fingerprint()

        request = planner_action_request(
            planner_fingerprint=fingerprint,
            planner_input_ref="planner/round_001.input.json",
            planner_input_sha256=input_hash,
            replay_policy="DETERMINISTIC",
        )
        self.assertEqual(planner_action_id(request), planner_action_id(dict(request)))

        changed_input = planner_input(incumbent_id="candidate_001")
        request_for_changed_input = planner_action_request(
            planner_fingerprint=fingerprint,
            planner_input_ref="planner/round_001.input.json",
            planner_input_sha256=canonical_sha256(changed_input),
            replay_policy="DETERMINISTIC",
        )
        request_for_changed_planner = planner_action_request(
            planner_fingerprint=fingerprint + ":changed",
            planner_input_ref="planner/round_001.input.json",
            planner_input_sha256=input_hash,
            replay_policy="DETERMINISTIC",
        )
        self.assertNotEqual(
            planner_action_id(request), planner_action_id(request_for_changed_input)
        )
        self.assertNotEqual(
            planner_action_id(request), planner_action_id(request_for_changed_planner)
        )

    def test_planner_output_round_trip_and_tampering_is_rejected(self) -> None:
        original = proposal()
        input_hash = canonical_sha256(planner_input())
        action_id = "planner-action-123"
        output = build_planner_output(
            action_id=action_id,
            input_sha256=input_hash,
            proposal=original,
        )

        restored = load_planner_output(
            output,
            expected_action_id=action_id,
            expected_input_sha256=input_hash,
        )
        self.assertEqual(restored, original)

        with self.subTest("action binding"):
            with self.assertRaisesRegex(ValueError, "action binding mismatch"):
                load_planner_output(output, expected_action_id="different-action")

        with self.subTest("input binding"):
            with self.assertRaisesRegex(ValueError, "input binding mismatch"):
                load_planner_output(output, expected_input_sha256="different-input")

        with self.subTest("proposal payload"):
            tampered_payload = copy.deepcopy(output)
            tampered_payload["proposal"]["patch"] += "// tampered\n"  # type: ignore[index]
            with self.assertRaisesRegex(ValueError, "proposal hash mismatch"):
                load_planner_output(tampered_payload)

        with self.subTest("proposal hash"):
            tampered_hash = dict(output)
            tampered_hash["proposal_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "proposal hash mismatch"):
                load_planner_output(tampered_hash)

    def test_stable_validation_recursively_drops_only_replay_metadata(self) -> None:
        first = {
            "ok": True,
            "cached": False,
            "timestamp": "2026-07-20T01:02:03Z",
            "nested": {
                "cached": True,
                "timestamp": "old",
                "result": "PASS",
                "items": [
                    {"cached": False, "timestamp": "old", "value": 7},
                    "unchanged",
                ],
            },
        }
        second = copy.deepcopy(first)
        second["cached"] = True
        second["timestamp"] = "later"
        second["nested"]["cached"] = False  # type: ignore[index]
        second["nested"]["timestamp"] = "later"  # type: ignore[index]
        second["nested"]["items"][0]["timestamp"] = "later"  # type: ignore[index]

        expected = {
            "ok": True,
            "nested": {
                "result": "PASS",
                "items": [{"value": 7}, "unchanged"],
            },
        }
        self.assertEqual(stable_validation(first), expected)
        self.assertEqual(stable_validation(first), stable_validation(second))

    def test_patch_proposal_from_dict_round_trip_and_exact_fields(self) -> None:
        original = proposal()
        payload = original.to_dict()
        restored = PatchProposal.from_dict(payload)

        self.assertEqual(restored, original)
        self.assertIsInstance(restored.required_validation, tuple)

        missing = dict(payload)
        missing.pop("model")
        with self.assertRaisesRegex(ValueError, "missing=.*model"):
            PatchProposal.from_dict(missing)

        extra = dict(payload)
        extra["untrusted"] = "value"
        with self.assertRaisesRegex(ValueError, "extra=.*untrusted"):
            PatchProposal.from_dict(extra)


if __name__ == "__main__":
    unittest.main()
