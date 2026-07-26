from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import minimal_flow
import v1_planner_io
import benchmark_public_tasks_v1 as benchmark
from llm4hls_agent.budget import BudgetConfig, BudgetExceeded, BudgetLedger


class V1BudgetConsistencyTests(unittest.TestCase):
    def test_unknown_usage_final_budget_matches_state_and_ledger_is_nullable(self) -> None:
        patch = (
            minimal_flow.HARNESS_ROOT / "examples" / "u55c_repair.diff"
        ).read_text(encoding="utf-8")
        content = json.dumps(
            {
                "hypothesis": "Fix the public arithmetic mismatch.",
                "patch": patch,
            }
        )
        envelope = {
            "id": "req-unknown-usage",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
        }
        exchange = v1_planner_io.HttpExchange(
            response_received=True,
            status_code=200,
            request_id_header=None,
            raw_body=json.dumps(envelope).encode(),
            elapsed_s=0.2,
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with mock.patch.object(
                minimal_flow,
                "perform_chat_completion",
                return_value=exchange,
            ) as dispatch:
                exit_code = minimal_flow.main(
                    [
                        "--task-dir",
                        str(
                            minimal_flow.HARNESS_ROOT
                            / "examples"
                            / "u55c_repair_task"
                        ),
                        "--live-openai",
                        "--base-url",
                        "https://example.invalid/v1",
                        "--api-key",
                        "do-not-persist",
                        "--model",
                        "test-model",
                        "--run-dir",
                        str(run_dir),
                        "--backend",
                        "demo",
                    ]
                )
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(exit_code, 0)
            result = json.loads(
                (run_dir / "minimal_result.json").read_text(encoding="utf-8")
            )
            state = json.loads(
                (run_dir / "budget_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["budget"], state)
            self.assertEqual(state["llm_requests_total"], 1)
            self.assertEqual(state["usage_known_count"], 0)
            self.assertEqual(state["usage_unknown_count"], 1)
            self.assertIsNone(state["tokens_used"])
            self.assertEqual(state["recorded_token_lower_bound"], 0)
            self.assertEqual(state["pending_credits_reserved"], 0)
            self.assertEqual(state["pending_tokens_reserved"], 0)
            ledger_events = [
                json.loads(line)
                for line in (run_dir / "budget_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            llm_terminal = next(
                event
                for event in ledger_events
                if event.get("kind") == "llm"
                and event.get("state") == "COMPLETED"
            )
            self.assertIsNone(llm_terminal["tokens_used"])
            self.assertIsNone(llm_terminal["input_tokens"])
            self.assertEqual(
                llm_terminal["result_ref"],
                "planner/response_receipt.json",
            )
            self.assertEqual(result["selected_candidate_id"], "candidate_001")
            self.assertTrue(benchmark._audit_budget(run_dir, result)["pass"])

    def test_ambiguous_llm_usage_is_null_and_blocks_later_token_reservation(self) -> None:
        config = BudgetConfig(
            credit_limit=10,
            costs={"llm": 0},
            tool_limits={"llm": 2},
            token_limit=100,
            runtime_limit_seconds=60,
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(Path(directory) / "ledger.jsonl", config)
            ledger.reserve(
                action_id="a" * 64,
                kind="llm",
                candidate_id="candidate",
                code_hash="b" * 64,
                tool_config_hash="c" * 64,
                estimated_tokens=80,
            )
            ledger.mark_ambiguous("a" * 64)
            terminal = ledger.action_events("a" * 64)[-1]
            self.assertIsNone(terminal["tokens_used"])
            self.assertFalse(ledger.snapshot()["token_usage_complete"])
            with self.assertRaises(BudgetExceeded):
                ledger.reserve(
                    action_id="d" * 64,
                    kind="llm",
                    candidate_id="candidate",
                    code_hash="e" * 64,
                    tool_config_hash="f" * 64,
                    estimated_tokens=1,
                )

    def test_read_only_ledger_audit_refuses_torn_tail_without_repair(self) -> None:
        config = BudgetConfig(
            credit_limit=10,
            costs={"llm": 0},
            tool_limits={"llm": 1},
            token_limit=100,
            runtime_limit_seconds=60,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            BudgetLedger(path, config)
            with path.open("ab") as stream:
                stream.write(b'{"sequence":1')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                benchmark._read_ledger_events_strict(path)
            self.assertEqual(path.read_bytes(), before)

    def test_action_audit_rejects_orphan_terminal_and_token_overrun(self) -> None:
        config = BudgetConfig(
            credit_limit=10,
            costs={"llm": 0},
            tool_limits={"llm": 1},
            token_limit=10,
            runtime_limit_seconds=60,
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            result_path = run_dir / "receipt.json"
            result_path.write_text("{}\n", encoding="utf-8")
            digest = minimal_flow._sha256(result_path.read_bytes())
            initialized = {
                "sequence": 0,
                "state": "INITIALIZED",
                "config_hash": config.config_hash,
            }
            orphan = {
                "sequence": 1,
                "state": "COMPLETED",
                "action_id": "a" * 64,
                "kind": "llm",
                "actual_cost": 0,
                "result_ref": "receipt.json",
                "result_sha256": digest,
            }
            _paired, errors = benchmark._validate_ledger_actions(
                events=[initialized, orphan],
                config=config,
                run_dir=run_dir,
            )
            self.assertTrue(
                any("ORPHAN_OR_DUPLICATE_TERMINAL" in error for error in errors)
            )

            started = {
                "sequence": 1,
                "state": "STARTED",
                "action_id": "b" * 64,
                "kind": "llm",
                "estimated_cost": 0,
            }
            overrun = {
                "sequence": 2,
                "state": "COMPLETED",
                "action_id": "b" * 64,
                "kind": "llm",
                "actual_cost": 0,
                "token_reservation_overrun": True,
                "result_ref": "receipt.json",
                "result_sha256": digest,
            }
            _paired, errors = benchmark._validate_ledger_actions(
                events=[initialized, started, overrun],
                config=config,
                run_dir=run_dir,
            )
            self.assertTrue(
                any("TOKEN_RESERVATION_OVERRUN" in error for error in errors)
            )


if __name__ == "__main__":
    unittest.main()
