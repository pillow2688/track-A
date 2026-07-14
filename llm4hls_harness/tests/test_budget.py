from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig, BudgetExceeded, BudgetLedger


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


if __name__ == "__main__":
    unittest.main()
