from __future__ import annotations

import json
import unittest

from llm4hls_agent.openai_provider import (
    DEFAULT_MODEL,
    OpenAICompatibleConfig,
    OpenAICompatibleRepairProvider,
    build_repair_prompt,
)
from llm4hls_agent.repair import RepairContext, RepairProviderError


def context() -> RepairContext:
    return RepairContext(
        task_id="fixture",
        candidate_id="candidate_000",
        stage="csim",
        phase="runtime_fail",
        diagnostic_code="FUNCTIONAL_MISMATCH",
        summary="public output mismatch",
        evidence=("kernel.cpp:5 mismatch",),
        source_excerpt='4: for (...) {\n5:   c[i] = a[i] - b[i];\n6: }',
        remaining_tokens=4096,
        remaining_credits=55,
        top="vector_add",
        kernel_name="kernel.cpp",
        part="xcu55c-fsvh2892-2L-e",
        clock_ns=10.0,
        initial_condition="vector addition must pass",
    )


def envelope(content: object, *, usage: object | None = None) -> bytes:
    return json.dumps(
        {
            "id": "req_fixture",
            "choices": [{"message": {"content": content}}],
            "usage": usage
            if usage is not None
            else {"prompt_tokens": 321, "completion_tokens": 87},
        }
    ).encode()


class OpenAICompatibleProviderTests(unittest.TestCase):
    def config(self, **overrides: object) -> OpenAICompatibleConfig:
        values = {
            "base_url": "https://llm.example/v1",
            "api_key": "secret-test-key",
            "model": DEFAULT_MODEL,
            "timeout_seconds": 30.0,
            "max_output_tokens": 900,
            "temperature": 0.0,
        }
        values.update(overrides)
        return OpenAICompatibleConfig(**values)  # type: ignore[arg-type]

    def response_content(self) -> str:
        return json.dumps(
            {
                "hypothesis": "wrong arithmetic operator",
                "change_class": "FUNCTIONAL_REPAIR",
                "expected_effect": "restore addition",
                "risk": "low",
                "required_validation": ["csim", "synth", "cosim"],
                "patch": "--- a/kernel.cpp\n+++ b/kernel.cpp\n@@ -1 +1 @@\n-a-b\n+a+b\n",
            }
        )

    def test_request_and_strict_response_are_auditable(self) -> None:
        captured: dict[str, object] = {}

        def transport(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return 200, {}, envelope(self.response_content())

        provider = OpenAICompatibleRepairProvider(self.config(), transport=transport)
        proposal = provider.propose_patch(context())

        self.assertEqual(captured["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer secret-test-key")
        self.assertEqual(captured["body"]["model"], DEFAULT_MODEL)  # type: ignore[index]
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})  # type: ignore[index]
        self.assertEqual(proposal.input_tokens, 321)
        self.assertEqual(proposal.output_tokens, 87)
        self.assertEqual(proposal.cached_input_tokens, 0)
        self.assertEqual(proposal.request_id, "req_fixture")
        self.assertEqual(proposal.change_class, "FUNCTIONAL_REPAIR")
        self.assertNotIn("secret-test-key", provider.fingerprint())

    def test_deepseek_style_base_url_uses_chat_completions_without_v1(self) -> None:
        config = self.config(base_url="https://api.deepseek.com")
        self.assertEqual(
            config.chat_completions_url,
            "https://api.deepseek.com/chat/completions",
        )

    def test_deepseek_v4_disables_thinking_for_bounded_json_patch(self) -> None:
        captured: dict[str, object] = {}

        def transport(request, _timeout):
            captured.update(json.loads(request.data.decode()))
            return 200, {}, envelope(self.response_content())

        provider = OpenAICompatibleRepairProvider(
            self.config(base_url="https://api.deepseek.com", model="deepseek-v4-pro"),
            transport=transport,
        )
        provider.propose_patch(context())
        self.assertEqual(captured["thinking"], {"type": "disabled"})

    def test_prompt_contains_local_evidence_and_constraints_only(self) -> None:
        prompt = build_repair_prompt(context())
        self.assertIn("FUNCTIONAL_MISMATCH", prompt)
        self.assertIn("kernel.cpp", prompt)
        self.assertIn("xcu55c-fsvh2892-2L-e", prompt)
        self.assertNotIn("secret", prompt)
        self.assertNotIn("hidden/", prompt)

    def test_invalid_content_and_missing_usage_are_rejected(self) -> None:
        cases = [
            envelope("not json"),
            envelope(json.dumps({"patch": "diff"})),
            envelope(self.response_content(), usage={}),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                provider = OpenAICompatibleRepairProvider(
                    self.config(), transport=lambda _request, _timeout, raw=raw: (200, {}, raw)
                )
                with self.assertRaises(RepairProviderError):
                    provider.propose_patch(context())

    def test_invalid_model_output_preserves_reported_token_usage(self) -> None:
        provider = OpenAICompatibleRepairProvider(
            self.config(),
            transport=lambda _request, _timeout: (200, {}, envelope("not json")),
        )
        with self.assertRaises(RepairProviderError) as raised:
            provider.propose_patch(context())
        self.assertEqual(raised.exception.input_tokens, 321)
        self.assertEqual(raised.exception.output_tokens, 87)
        self.assertEqual(raised.exception.request_id, "req_fixture")
        self.assertEqual(raised.exception.response_excerpt, "not json")

    def test_http_failure_does_not_expose_api_key(self) -> None:
        provider = OpenAICompatibleRepairProvider(
            self.config(), transport=lambda _request, _timeout: (401, {}, b"denied")
        )
        with self.assertRaises(RepairProviderError) as caught:
            provider.propose_patch(context())
        self.assertNotIn("secret-test-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
