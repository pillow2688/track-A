from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm4hls_agent.v3_planner_action as planner_action_module
from llm4hls_agent.budget import BudgetConfig, BudgetExceeded, BudgetLedger
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.v3_planner import (
    build_planner_input,
    canonical_json,
    canonical_sha256,
)
from llm4hls_agent.v3_planner_action import (
    PlannerActionAmbiguous,
    PlannerActionJournal,
    PreparedPlannerCall,
)


def live_proposal() -> PatchProposal:
    return PatchProposal(
        patch=(
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,2 +1,3 @@\n"
            " void kernel() {\n"
            "+#pragma HLS PIPELINE II=1\n"
            " }\n"
        ),
        provider="fixture-provider",
        model="fixture-live-model",
        revision="fixture-revision-1",
        input_tokens=17,
        output_tokens=11,
        cached_input_tokens=3,
        request_id="fixture-request-1",
        duration_seconds=0.25,
        hypothesis="Pipeline the dominant loop.",
        change_class="LOOP_PIPELINE",
        expected_effect="Reduce loop II to one.",
        risk="low",
        required_validation=("csim", "synth"),
    )


def planner_input() -> dict[str, object]:
    return build_planner_input(
        task={"task_id": "dot_product", "task_type": "optimize"},
        round_state={
            "round_index": 1,
            "max_rounds": 2,
            "parent_candidate_id": "candidate_000",
        },
        incumbent={"candidate_id": "candidate_000", "latency": 1027},
        baseline={"candidate_id": "candidate_000", "latency": 1027},
        history=[{"round_index": 0, "decision": "BASELINE"}],
        policy={"objective": "official_score", "requires_cosim": False},
        budget={"remaining_credits": 70, "remaining_tokens": 100},
    )


class FakeLivePlanner:
    replay_policy = "NON_REPLAYABLE"

    def __init__(self) -> None:
        self.prepare_calls = 0
        self.invoke_calls = 0
        self.proposal = live_proposal()

    def fingerprint(self) -> str:
        return "fixture-live-planner-v1"

    def prepare(self, value) -> PreparedPlannerCall:
        self.prepare_calls += 1
        self.last_input = value
        return PreparedPlannerCall(
            request={
                "model": "fixture-live-model",
                "messages": [
                    {"role": "user", "content": canonical_sha256(value)}
                ],
                "temperature": 0,
            },
            estimated_input_tokens=20,
            max_output_tokens=30,
        )

    def invoke(self, prepared: PreparedPlannerCall) -> PatchProposal:
        self.invoke_calls += 1
        self.last_prepared = prepared
        return self.proposal


class V3PlannerActionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_root = Path(self.temporary.name)
        self.config = BudgetConfig(
            credit_limit=4,
            costs={"llm": 1},
            tool_limits={"llm": 4},
            token_limit=100,
            runtime_limit_seconds=60.0,
        )
        self.input = planner_input()
        self.input_ref = "planner/inputs/round_001.json"
        input_path = self.run_root / self.input_ref
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_bytes(canonical_json(self.input))
        self.input_sha256 = canonical_sha256(self.input)

    def ledger(self) -> BudgetLedger:
        return BudgetLedger(self.run_root / "budget_ledger.jsonl", self.config)

    def journal(self) -> PlannerActionJournal:
        return PlannerActionJournal(self.run_root, self.ledger())

    def execute(self, journal: PlannerActionJournal, planner: FakeLivePlanner):
        return journal.execute_or_recover(
            planner,
            self.input,
            input_ref=self.input_ref,
            input_sha256=self.input_sha256,
            candidate_id="candidate_000",
            code_hash="a" * 64,
        )

    def only_started_action_id(self) -> str:
        started = [
            event
            for event in self.ledger().events()
            if event.get("state") == "STARTED"
        ]
        self.assertEqual(len(started), 1)
        action_id = started[0].get("action_id")
        self.assertIsInstance(action_id, str)
        return str(action_id)

    def test_success_charges_exact_provider_tokens_and_invokes_once(self) -> None:
        planner = FakeLivePlanner()

        result = self.execute(self.journal(), planner)

        self.assertEqual(result.proposal, planner.proposal)
        self.assertFalse(result.cached)
        self.assertEqual(planner.invoke_calls, 1)
        self.assertEqual(result.action_id, self.only_started_action_id())
        output_path = self.run_root / result.output_ref
        self.assertTrue(output_path.is_file())
        self.assertEqual(
            hashlib.sha256(output_path.read_bytes()).hexdigest(),
            result.output_sha256,
        )
        snapshot = self.ledger().snapshot()
        self.assertEqual(snapshot["credits_used"], 1)
        self.assertEqual(snapshot["tool_used"]["llm"], 1)
        self.assertEqual(snapshot["tool_pending"]["llm"], 0)
        self.assertEqual(snapshot["tokens_used"], 28)
        self.assertEqual(snapshot["input_tokens_used"], 17)
        self.assertEqual(snapshot["output_tokens_used"], 11)
        self.assertEqual(snapshot["cached_input_tokens_used"], 3)
        self.assertTrue(snapshot["token_usage_complete"])

    def test_completed_action_is_reused_without_reinvocation_or_recharge(self) -> None:
        planner = FakeLivePlanner()
        first = self.execute(self.journal(), planner)
        first_snapshot = self.ledger().snapshot()
        first_ledger = (self.run_root / "budget_ledger.jsonl").read_bytes()

        second = self.execute(self.journal(), planner)

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.proposal, first.proposal)
        self.assertEqual(second.action_id, first.action_id)
        self.assertEqual(second.output_ref, first.output_ref)
        self.assertEqual(second.output_sha256, first.output_sha256)
        self.assertEqual(planner.invoke_calls, 1)
        second_snapshot = self.ledger().snapshot()
        for field in (
            "credits_used",
            "tokens_used",
            "input_tokens_used",
            "output_tokens_used",
            "cached_input_tokens_used",
            "tool_used",
            "tool_pending",
        ):
            self.assertEqual(second_snapshot[field], first_snapshot[field])
        self.assertEqual(
            (self.run_root / "budget_ledger.jsonl").read_bytes(), first_ledger
        )

    def test_durable_output_before_ledger_complete_recovers_without_reinvocation(
        self,
    ) -> None:
        planner = FakeLivePlanner()
        with patch.object(
            planner_action_module,
            "_after_output_persisted",
            side_effect=RuntimeError("injected after Planner output persistence"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "injected after Planner output persistence"
            ):
                self.execute(self.journal(), planner)

        pending = self.ledger().snapshot()
        self.assertEqual(planner.invoke_calls, 1)
        self.assertEqual(pending["tool_pending"]["llm"], 1)
        self.assertEqual(pending["pending_tokens_reserved"], 50)
        self.assertEqual(pending["tokens_used"], 0)

        recovered = self.execute(self.journal(), planner)

        self.assertTrue(recovered.cached)
        self.assertEqual(recovered.proposal, planner.proposal)
        self.assertEqual(planner.invoke_calls, 1)
        self.assertTrue((self.run_root / recovered.output_ref).is_file())
        snapshot = self.ledger().snapshot()
        self.assertEqual(snapshot["tool_pending"]["llm"], 0)
        self.assertEqual(snapshot["tool_used"]["llm"], 1)
        self.assertEqual(snapshot["tokens_used"], 28)
        self.assertEqual(snapshot["input_tokens_used"], 17)
        self.assertEqual(snapshot["output_tokens_used"], 11)
        self.assertEqual(snapshot["cached_input_tokens_used"], 3)

    def test_started_without_output_becomes_ambiguous_without_invocation(self) -> None:
        planner = FakeLivePlanner()
        with patch.object(
            planner_action_module,
            "_after_started",
            side_effect=RuntimeError("injected after Planner STARTED"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "injected after Planner STARTED"
            ):
                self.execute(self.journal(), planner)

        action_id = self.only_started_action_id()
        self.assertEqual(planner.invoke_calls, 0)
        pending = self.ledger().snapshot()
        self.assertEqual(pending["tool_pending"]["llm"], 1)
        self.assertEqual(pending["pending_tokens_reserved"], 50)

        with self.assertRaises(RuntimeError):
            self.execute(self.journal(), planner)

        self.assertEqual(planner.invoke_calls, 0)
        ledger = self.ledger()
        self.assertTrue(ledger.is_ambiguous(action_id))
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["tool_pending"]["llm"], 0)
        self.assertEqual(snapshot["tool_used"]["llm"], 1)
        self.assertEqual(snapshot["credits_used"], 1)
        self.assertEqual(snapshot["tokens_used"], 50)
        self.assertFalse(snapshot["token_usage_complete"])

    def test_ledger_complete_before_journal_complete_recovers_without_reinvoke(
        self,
    ) -> None:
        planner = FakeLivePlanner()
        with patch.object(
            planner_action_module,
            "_after_ledger_completed",
            side_effect=RuntimeError("injected after Ledger completion"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "injected after Ledger completion"
            ):
                self.execute(self.journal(), planner)

        self.assertEqual(planner.invoke_calls, 1)
        completed_before = self.ledger().snapshot()
        self.assertEqual(completed_before["tool_used"]["llm"], 1)
        self.assertEqual(completed_before["tokens_used"], 28)
        self.assertEqual(
            list(
                (self.run_root / "control" / "live_planner_actions").glob(
                    "*.completed.json"
                )
            ),
            [],
        )

        recovered = self.execute(self.journal(), planner)

        self.assertTrue(recovered.cached)
        self.assertEqual(planner.invoke_calls, 1)
        self.assertTrue((self.run_root / recovered.completed_ref).is_file())
        self.assertEqual(self.ledger().snapshot()["tokens_used"], 28)

    def test_secret_bearing_request_is_rejected_before_reservation(self) -> None:
        class SecretPlanner(FakeLivePlanner):
            def prepare(self, value) -> PreparedPlannerCall:
                return PreparedPlannerCall(
                    request={
                        "model": "fixture-live-model",
                        "headers": {"Authorization": "Bearer must-not-persist"},
                    },
                    estimated_input_tokens=20,
                    max_output_tokens=30,
                )

        planner = SecretPlanner()

        with self.assertRaisesRegex(ValueError, "must not contain secrets"):
            self.execute(self.journal(), planner)

        self.assertEqual(planner.invoke_calls, 0)
        self.assertEqual(
            [
                event
                for event in self.ledger().events()
                if event.get("state") == "STARTED"
            ],
            [],
        )

    def test_request_string_rejects_bearer_secret_and_absolute_local_path(
        self,
    ) -> None:
        for unsafe_value, message in (
            ("Bearer must-not-persist", "secrets"),
            (
                "https://user:password@example.invalid/v1",
                "must not contain",
            ),
            (
                "https://example.invalid/v1?token=private-value",
                "must not contain",
            ),
            ("failure at /home/runner/private/kernel.cpp", "absolute local paths"),
            ("failure at /etc", "absolute local paths"),
            (r"failure at C:\Users\runner\kernel.cpp", "absolute local paths"),
            (r"failure at C:\secret", "absolute local paths"),
        ):
            with self.subTest(unsafe_value=unsafe_value):
                class UnsafePlanner(FakeLivePlanner):
                    def prepare(self, value) -> PreparedPlannerCall:
                        return PreparedPlannerCall(
                            request={
                                "model": "fixture-live-model",
                                "diagnostic": unsafe_value,
                            },
                            estimated_input_tokens=20,
                            max_output_tokens=30,
                        )

                planner = UnsafePlanner()
                with self.assertRaisesRegex(ValueError, message):
                    self.execute(self.journal(), planner)
                self.assertEqual(planner.invoke_calls, 0)

        self.assertEqual(self.ledger().snapshot()["tool_used"]["llm"], 0)

    def test_live_action_requires_a_positive_token_reservation(self) -> None:
        class ZeroReservePlanner(FakeLivePlanner):
            def prepare(self, value) -> PreparedPlannerCall:
                return PreparedPlannerCall(
                    request={"model": "fixture-live-model"},
                    estimated_input_tokens=0,
                    max_output_tokens=0,
                )

        planner = ZeroReservePlanner()

        with self.assertRaisesRegex(ValueError, "positive token bound"):
            self.execute(self.journal(), planner)

        self.assertEqual(planner.invoke_calls, 0)
        self.assertEqual(self.ledger().snapshot()["tool_used"]["llm"], 0)

    def test_missing_live_usage_is_conservatively_closed_not_marked_complete(
        self,
    ) -> None:
        planner = FakeLivePlanner()
        planner.proposal = PatchProposal(
            patch=live_proposal().patch,
            provider="fixture-provider",
            model="fixture-live-model",
            hypothesis="usage intentionally omitted",
            change_class="LOOP_PIPELINE",
            expected_effect="must not be accepted",
            risk="unknown",
            required_validation=("csim", "synth"),
        )

        with self.assertRaisesRegex(PlannerActionAmbiguous, "usage is incomplete"):
            self.execute(self.journal(), planner)

        snapshot = self.ledger().snapshot()
        self.assertEqual(planner.invoke_calls, 1)
        self.assertEqual(snapshot["tokens_used"], 50)
        self.assertFalse(snapshot["token_usage_complete"])
        self.assertEqual(snapshot["tool_used"]["llm"], 1)
        self.assertEqual(
            list((self.run_root / "planner" / "live_outcomes").glob("*.json")),
            [],
        )

    def test_token_reservation_overrun_never_recovers_as_cached_success(self) -> None:
        class UnderReservedPlanner(FakeLivePlanner):
            def prepare(self, value) -> PreparedPlannerCall:
                self.prepare_calls += 1
                return PreparedPlannerCall(
                    request={"model": "fixture-live-model", "input": value["round"]},
                    estimated_input_tokens=5,
                    max_output_tokens=5,
                )

        planner = UnderReservedPlanner()

        with self.assertRaisesRegex(BudgetExceeded, "durable reservation"):
            self.execute(self.journal(), planner)
        with self.assertRaisesRegex(BudgetExceeded, "durable reservation"):
            self.execute(self.journal(), planner)

        self.assertEqual(planner.invoke_calls, 1)
        completed = self.ledger().completed_event(self.only_started_action_id())
        self.assertIsNotNone(completed)
        self.assertTrue(completed["token_reservation_overrun"])

    def test_reserve_before_started_crash_is_recovered_before_dispatch(self) -> None:
        planner = FakeLivePlanner()
        with patch.object(
            planner_action_module,
            "_after_reserved_before_started",
            side_effect=RuntimeError("injected before STARTED journal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before STARTED journal"):
                self.execute(self.journal(), planner)

        self.assertEqual(planner.invoke_calls, 0)
        self.assertEqual(self.ledger().snapshot()["tool_pending"]["llm"], 1)
        self.assertEqual(
            list(
                (self.run_root / "control" / "live_planner_actions").glob(
                    "*.started.json"
                )
            ),
            [],
        )

        recovered = self.execute(self.journal(), planner)

        self.assertFalse(recovered.cached)
        self.assertEqual(planner.invoke_calls, 1)
        self.assertEqual(self.ledger().snapshot()["tool_pending"]["llm"], 0)
        self.assertTrue((self.run_root / recovered.completed_ref).is_file())


if __name__ == "__main__":
    unittest.main()
