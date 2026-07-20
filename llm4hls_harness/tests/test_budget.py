from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import (
    BudgetConfig,
    BudgetExceeded,
    BudgetLedger,
    BudgetLedgerError,
)


def config(*, runtime: float = 10.0, tokens: int = 0) -> BudgetConfig:
    return BudgetConfig(
        credit_limit=10,
        costs={"csim": 1, "synth": 4, "cosim": 5},
        tool_limits={"csim": 1, "synth": 1, "cosim": 1},
        token_limit=tokens,
        runtime_limit_seconds=runtime,
    )


class BudgetLedgerTests(unittest.TestCase):
    def test_non_finite_runtime_is_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config(runtime=value)

    def test_token_usage_cannot_be_negative_or_exceed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = BudgetLedger(Path(tmp) / "ledger.jsonl", config(tokens=1))
            ledger.reserve(
                action_id="a",
                kind="csim",
                candidate_id="candidate_000",
                code_hash="code",
                tool_config_hash="tool",
            )
            for value in (-1, 2):
                with self.subTest(value=value), self.assertRaises(BudgetExceeded):
                    ledger.complete(
                        action_id="a",
                        result_ref="result.json",
                        result_sha256="0" * 64,
                        elapsed_s=0.0,
                        tokens_used=value,
                    )

    def test_incomplete_trailing_json_is_repaired_to_last_durable_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = BudgetLedger(path, config())
            original = path.read_bytes()
            with path.open("ab") as stream:
                stream.write(b'{"sequence":1,"state":"STAR')

            recovered = BudgetLedger(path, config())

            self.assertEqual(recovered.events()[0]["state"], "INITIALIZED")
            self.assertEqual(path.read_bytes(), original)

    def test_snapshot_separates_input_and_output_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = BudgetLedger(Path(tmp) / "ledger.jsonl", config(tokens=10))
            ledger.reserve(
                action_id="token-action", kind="csim",
                candidate_id="candidate_000", code_hash="code", tool_config_hash="tool",
            )
            ledger.complete(
                action_id="token-action", result_ref="result.json",
                result_sha256="0" * 64, elapsed_s=0.1,
                tokens_used=3, input_tokens=2, output_tokens=1, cached_input_tokens=1,
            )
            snapshot = ledger.snapshot()
            self.assertEqual(snapshot["tokens_used"], 3)
            self.assertEqual(snapshot["input_tokens_used"], 2)
            self.assertEqual(snapshot["output_tokens_used"], 1)
            self.assertEqual(snapshot["cached_input_tokens_used"], 1)

    def test_started_action_durably_reserves_tokens_and_reconciles_actual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = BudgetLedger(Path(tmp) / "ledger.jsonl", config(tokens=10))
            ledger.reserve(
                action_id="reserved",
                kind="csim",
                candidate_id="candidate_000",
                code_hash="code",
                tool_config_hash="tool",
                estimated_tokens=8,
            )

            pending = ledger.snapshot()
            self.assertEqual(pending["pending_tokens_reserved"], 8)
            self.assertEqual(pending["tokens_remaining"], 2)
            with self.assertRaises(BudgetExceeded):
                ledger.reserve(
                    action_id="too-large",
                    kind="synth",
                    candidate_id="candidate_000",
                    code_hash="code",
                    tool_config_hash="tool",
                    estimated_tokens=3,
                )

            ledger.complete(
                action_id="reserved",
                result_ref="result.json",
                result_sha256="0" * 64,
                elapsed_s=0.1,
                tokens_used=3,
                input_tokens=2,
                output_tokens=1,
            )
            completed = ledger.snapshot()
            self.assertEqual(completed["pending_tokens_reserved"], 0)
            self.assertEqual(completed["tokens_used"], 3)
            self.assertEqual(completed["tokens_remaining"], 7)

    def test_non_replayable_ambiguous_action_charges_reserved_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            budget = BudgetConfig(
                credit_limit=10,
                costs={"llm": 2},
                tool_limits={"llm": 1},
                token_limit=100,
                runtime_limit_seconds=10.0,
            )
            ledger = BudgetLedger(Path(tmp) / "ledger.jsonl", budget)
            ledger.reserve(
                action_id="live-planner",
                kind="llm",
                candidate_id="candidate_000",
                code_hash="code",
                tool_config_hash="request",
                estimated_tokens=80,
            )

            ledger.mark_ambiguous_conservative(
                "live-planner", reason="provider dispatch state is unknown"
            )

            snapshot = ledger.snapshot()
            self.assertEqual(snapshot["credits_used"], 2)
            self.assertEqual(snapshot["tokens_used"], 80)
            self.assertEqual(snapshot["tokens_remaining"], 20)
            self.assertFalse(snapshot["token_usage_complete"])
            self.assertEqual(snapshot["tool_used"]["llm"], 1)
            self.assertEqual(snapshot["tool_pending"]["llm"], 0)

    def test_snapshot_rejects_tampered_terminal_credit_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = BudgetLedger(path, config(tokens=10))
            ledger.reserve(
                action_id="cost-bound",
                kind="csim",
                candidate_id="candidate_000",
                code_hash="code",
                tool_config_hash="tool",
            )
            ledger.complete(
                action_id="cost-bound",
                result_ref="result.json",
                result_sha256="0" * 64,
                elapsed_s=0.1,
            )
            events = [json.loads(line) for line in path.read_text().splitlines()]
            events[-1]["actual_cost"] = 0
            path.write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BudgetLedgerError, "terminal cost"):
                BudgetLedger(path, config(tokens=10)).snapshot()


if __name__ == "__main__":
    unittest.main()
