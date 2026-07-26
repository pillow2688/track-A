from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import minimal_flow
import v1_planner_io as planner_io
from llm4hls_agent.budget import BudgetConfig, BudgetLedger


class V1PlannerIoTests(unittest.TestCase):
    def _receipt(
        self,
        directory: Path,
        content: object,
        *,
        finish_reason: str = "stop",
        usage: object | None = None,
    ) -> planner_io.PlannerResponseReceipt:
        envelope = {
            "id": "req-test",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": content},
                }
            ],
            "usage": (
                usage
                if usage is not None
                else {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 2},
                }
            ),
        }
        return planner_io.persist_response(
            planner_dir=directory,
            exchange=planner_io.HttpExchange(
                response_received=True,
                status_code=200,
                request_id_header="header-id",
                raw_body=json.dumps(envelope).encode(),
                elapsed_s=0.25,
            ),
            model="test-model",
        )

    def test_string_text_array_and_single_markdown_fence(self) -> None:
        payload = {"hypothesis": "h", "patch": "--- k.cpp\n+++ k.cpp\n"}
        encoded = json.dumps(payload)
        cases: list[object] = [
            encoded,
            [encoded[:8], {"type": "text", "text": encoded[8:]}],
            f"```json\n{encoded}\n```",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, content in enumerate(cases):
                receipt = self._receipt(root / str(index), content)
                parsed = planner_io.parse_planner_response(receipt)
                self.assertEqual(parsed.hypothesis, "h")
                self.assertEqual(parsed.patch, payload["patch"])

    def test_error_categories_are_stable(self) -> None:
        cases = [
            ("", "stop", planner_io.ERROR_EMPTY_RESPONSE),
            ('{"hypothesis": "h", "patch": ', "stop", planner_io.ERROR_TRUNCATED_JSON),
            ("not json", "stop", planner_io.ERROR_INVALID_JSON),
            (json.dumps({"hypothesis": "h"}), "stop", planner_io.ERROR_INVALID_SCHEMA),
            (
                json.dumps({"hypothesis": "h", "patch": "p"}),
                "length",
                planner_io.ERROR_TRUNCATED_JSON,
            ),
            (json.dumps("json string"), "stop", planner_io.ERROR_INVALID_SCHEMA),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (content, finish, expected) in enumerate(cases):
                receipt = self._receipt(
                    root / str(index),
                    content,
                    finish_reason=finish,
                )
                with self.assertRaises(planner_io.PlannerAdaptationError) as caught:
                    planner_io.parse_planner_response(receipt)
                self.assertEqual(caught.exception.category, expected)

    def test_non_text_array_item_is_invalid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = self._receipt(
                Path(directory),
                [{"type": "image", "image_url": "x"}],
            )
            with self.assertRaises(planner_io.PlannerAdaptationError) as caught:
                planner_io.parse_planner_response(receipt)
            self.assertEqual(
                caught.exception.category,
                planner_io.ERROR_INVALID_SCHEMA,
            )

    def test_response_and_usage_are_durable_and_accounted_before_parse(self) -> None:
        invalid_content = '{"hypothesis": "h", "patch": '
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            config = BudgetConfig(
                credit_limit=10,
                costs={"llm": 0},
                tool_limits={"llm": 1},
                token_limit=100,
                runtime_limit_seconds=60,
            )
            ledger = BudgetLedger(run_root / "budget_ledger.jsonl", config)
            exchange = self._receipt_exchange(invalid_content)
            original_parse = minimal_flow.parse_planner_response

            def parse_after_accounting(receipt: planner_io.PlannerResponseReceipt):
                self.assertTrue((run_root / "planner" / "response_envelope.json").is_file())
                self.assertTrue((run_root / "planner" / "raw_content.txt").is_file())
                snapshot = ledger.snapshot()
                self.assertEqual(snapshot["tool_used"]["llm"], 1)
                self.assertEqual(snapshot["tokens_used"], 15)
                return original_parse(receipt)

            baseline = minimal_flow.ValidationOutcome(
                candidate_id="candidate_000",
                csim=None,
                synth=None,
                cosim=None,
                requires_cosim=False,
            )
            task = minimal_flow.load_public_task(
                minimal_flow.HARNESS_ROOT / "examples" / "u55c_repair_task"
            )
            with (
                mock.patch.object(
                    minimal_flow,
                    "perform_chat_completion",
                    return_value=exchange,
                ) as dispatch,
                mock.patch.object(
                    minimal_flow,
                    "parse_planner_response",
                    side_effect=parse_after_accounting,
                ),
            ):
                with self.assertRaises(planner_io.PlannerAdaptationError):
                    minimal_flow._call_live_planner(
                        task=task,
                        mode="REPAIR",
                        baseline=baseline,
                        ledger=ledger,
                        run_root=run_root,
                        base_url="https://example.invalid/v1",
                        api_key="never-persist-this-key",
                        model="test-model",
                        max_output_tokens=20,
                        timeout_s=1,
                    )
            self.assertEqual(dispatch.call_count, 1)
            result = json.loads(
                (run_root / "planner" / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["error_type"], planner_io.ERROR_TRUNCATED_JSON)
            self.assertEqual(result["usage"]["tokens_used"], 15)
            self.assertNotIn(
                "never-persist-this-key",
                "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in run_root.rglob("*")
                    if path.is_file()
                ),
            )

    def test_unknown_usage_is_null_not_zero_and_still_counts_call(self) -> None:
        content = json.dumps({"hypothesis": "h", "patch": "p"})
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            receipt = self._receipt(run_root / "planner", content, usage={})
            self.assertFalse(receipt.usage.usage_complete)
            self.assertIsNone(receipt.usage.tokens_used)
            config = BudgetConfig(
                credit_limit=10,
                costs={"llm": 0},
                tool_limits={"llm": 1},
                token_limit=100,
                runtime_limit_seconds=60,
            )
            ledger = BudgetLedger(run_root / "ledger.jsonl", config)
            ledger.reserve(
                action_id="a" * 64,
                kind="llm",
                candidate_id="candidate",
                code_hash="b" * 64,
                tool_config_hash="c" * 64,
                estimated_tokens=100,
            )
            ledger.complete_unknown_usage(
                action_id="a" * 64,
                result_ref="planner/response_receipt.json",
                result_sha256=receipt.receipt_sha256,
                elapsed_s=receipt.elapsed_s,
            )
            event = ledger.completed_event("a" * 64)
            self.assertIsNotNone(event)
            self.assertIsNone(event["tokens_used"])
            self.assertIsNone(event["input_tokens"])
            snapshot = ledger.snapshot()
            self.assertEqual(snapshot["tool_used"]["llm"], 1)
            self.assertEqual(snapshot["usage_unknown_count"], 1)
            self.assertFalse(snapshot["token_usage_complete"])

    def test_missing_cached_usage_is_null_while_total_usage_remains_known(self) -> None:
        content = json.dumps({"hypothesis": "h", "patch": "p"})
        with tempfile.TemporaryDirectory() as directory:
            receipt = self._receipt(
                Path(directory),
                content,
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            )
            self.assertTrue(receipt.usage.usage_complete)
            self.assertEqual(receipt.usage.tokens_used, 15)
            self.assertIsNone(receipt.usage.cached_input_tokens)

    def test_http_transport_and_invalid_envelope_are_rejected_before_payload(self) -> None:
        valid_content = json.dumps({"hypothesis": "h", "patch": "p"})
        valid_envelope = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": valid_content},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode()
        cases = [
            (
                planner_io.HttpExchange(
                    response_received=True,
                    status_code=500,
                    request_id_header="req-http",
                    raw_body=valid_envelope,
                    elapsed_s=0.1,
                    transport_error_type="HTTPError",
                    transport_error_detail="HTTP 500",
                ),
                planner_io.ERROR_HTTP_RESPONSE,
            ),
            (
                planner_io.HttpExchange(
                    response_received=False,
                    status_code=None,
                    request_id_header=None,
                    raw_body=b"",
                    elapsed_s=0.1,
                    transport_error_type="TimeoutError",
                    transport_error_detail="timed out",
                ),
                planner_io.ERROR_TRANSPORT,
            ),
            (
                planner_io.HttpExchange(
                    response_received=True,
                    status_code=200,
                    request_id_header="req-envelope",
                    raw_body=b"{not-json",
                    elapsed_s=0.1,
                ),
                planner_io.ERROR_INVALID_ENVELOPE,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (exchange, expected) in enumerate(cases):
                receipt = planner_io.persist_response(
                    planner_dir=root / str(index),
                    exchange=exchange,
                    model="test-model",
                )
                with self.assertRaises(planner_io.PlannerAdaptationError) as caught:
                    planner_io.parse_planner_response(receipt)
                self.assertEqual(caught.exception.category, expected)

    def test_echoed_api_key_is_redacted_from_all_response_artifacts(self) -> None:
        secret = "sk-test-exact-secret"
        content = json.dumps(
            {
                "hypothesis": f"echo {secret}",
                "patch": "p",
            }
        )
        envelope = {
            "id": "req-secret",
            "echo": secret,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = planner_io.persist_response(
                planner_dir=root,
                exchange=planner_io.HttpExchange(
                    response_received=True,
                    status_code=200,
                    request_id_header=None,
                    raw_body=json.dumps(envelope).encode(),
                    elapsed_s=0.1,
                ),
                model="test-model",
                api_key=secret,
            )
            parsed = planner_io.parse_planner_response(receipt)
            self.assertIn("<REDACTED_SECRET>", parsed.hypothesis)
            for path in root.iterdir():
                if path.is_file():
                    self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def test_trailing_comma_is_invalid_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = self._receipt(
                Path(directory),
                '{"hypothesis":"h","patch":"p",}',
            )
            with self.assertRaises(planner_io.PlannerAdaptationError) as caught:
                planner_io.parse_planner_response(receipt)
            self.assertEqual(
                caught.exception.category,
                planner_io.ERROR_INVALID_JSON,
            )

    @staticmethod
    def _receipt_exchange(content: str) -> planner_io.HttpExchange:
        envelope = {
            "id": "req-sequencing",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        return planner_io.HttpExchange(
            response_received=True,
            status_code=200,
            request_id_header=None,
            raw_body=json.dumps(envelope).encode(),
            elapsed_s=0.1,
        )


if __name__ == "__main__":
    unittest.main()
