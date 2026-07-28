from __future__ import annotations

import contextlib
import io
import json
import inspect
import unittest

from llm4hls_agent.budget import (
    TOKEN_ENVELOPE_SCHEMA,
    TokenBudgetLimits,
    TokenBudgetPolicy,
    TokenEnvelope,
    TokenEstimator,
)
from llm4hls_agent.v3_prototype_cli import _parser


def snapshot(*, limit: int = 12000, used: int = 1000) -> dict[str, object]:
    return {
        "run_token_limit": limit,
        "token_limit": limit,
        "tokens_used": used,
        "tokens_remaining": limit - used,
    }


class TokenEstimatorTests(unittest.TestCase):
    def test_empty_and_component_fallback_are_deterministic_and_secret_free(self) -> None:
        estimator = TokenEstimator()
        self.assertEqual(estimator.estimate_text(""), 0)
        first = estimator.estimate_components(
            {
                "kernel": "int top(int x) { return x; }",
                "header": "#pragma once",
                "description": "public task",
                "evidence": {"latency": 10},
                "history": [],
                "guidance": {"recommend": "PIPELINE"},
            }
        )
        second = estimator.estimate_components(
            {
                "guidance": {"recommend": "PIPELINE"},
                "history": [],
                "evidence": {"latency": 10},
                "description": "public task",
                "header": "#pragma once",
                "kernel": "int top(int x) { return x; }",
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(first.total_tokens, sum(first.component_tokens.values()))
        serialized = json.dumps(first.to_dict(), sort_keys=True)
        self.assertNotIn("int top", serialized)
        self.assertNotIn("PIPELINE", serialized)

    def test_injected_tokenizer_path_is_used(self) -> None:
        estimator = TokenEstimator(
            tokenizer=lambda text: text.split(),
            estimator_name="fixture-tokenizer",
            estimator_version="test-v1",
        )
        estimate = estimator.estimate_components({"kernel": "a b c", "header": "d"})
        self.assertEqual(estimate.total_tokens, 4)
        self.assertEqual(estimate.estimator_name, "fixture-tokenizer")


class TokenBudgetPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = TokenBudgetPolicy(
            TokenBudgetLimits(
                provider_hard_output_cap=3000,
                context_window_tokens=8000,
                context_safety_margin_tokens=200,
                token_budget_safety_margin=100,
                future_round_token_reserve=1000,
                search_closeout_token_reserve=300,
                configured_guidance_cap=600,
                guidance_ratio=0.15,
            )
        )

    def allocate(self, **overrides: object):
        values: dict[str, object] = {
            "budget_snapshot": snapshot(),
            "mode": "OPTIMIZE",
            "estimated_base_prompt_tokens": 1000,
            "estimated_guidance_tokens": 0,
            "estimated_input_tokens": 1000,
            "rounds_remaining": 2,
        }
        values.update(overrides)
        return self.policy.allocate(**values)  # type: ignore[arg-type]

    def test_envelope_is_versioned_canonical_and_hash_stable(self) -> None:
        first = self.allocate()
        second = self.allocate()
        self.assertEqual(first.to_dict()["schema_version"], TOKEN_ENVELOPE_SCHEMA)
        self.assertEqual(first.canonical_json, second.canonical_json)
        self.assertEqual(first.stable_hash, second.stable_hash)
        self.assertEqual(first.effective_max_output_tokens, 2400)
        self.assertEqual(TokenEnvelope.from_dict(first.to_dict()), first)

    def test_caps_apply_independently(self) -> None:
        configured = TokenBudgetPolicy(
            TokenBudgetLimits(configured_max_output_tokens=900)
        ).allocate(
            budget_snapshot=snapshot(limit=20000, used=0),
            mode="OPTIMIZE",
            estimated_base_prompt_tokens=100,
            estimated_guidance_tokens=0,
            estimated_input_tokens=100,
            rounds_remaining=1,
        )
        self.assertEqual(configured.effective_max_output_tokens, 900)

        provider = TokenBudgetPolicy(
            TokenBudgetLimits(provider_hard_output_cap=1100)
        ).allocate(
            budget_snapshot=snapshot(limit=20000, used=0),
            mode="OPTIMIZE",
            estimated_base_prompt_tokens=100,
            estimated_guidance_tokens=0,
            estimated_input_tokens=100,
            rounds_remaining=1,
        )
        self.assertEqual(provider.effective_max_output_tokens, 1100)

        context = self.allocate(estimated_input_tokens=6900, rounds_remaining=1)
        self.assertEqual(context.effective_max_output_tokens, 900)

        run = self.allocate(
            budget_snapshot=snapshot(limit=5000, used=2800),
            estimated_input_tokens=1000,
            rounds_remaining=1,
        )
        self.assertEqual(run.effective_max_output_tokens, 800)

    def test_input_future_and_search_closeout_reserves_are_deducted(self) -> None:
        envelope = self.allocate(
            budget_snapshot=snapshot(limit=6000, used=1000),
            estimated_input_tokens=1800,
            rounds_remaining=2,
        )
        # 5000 remaining - 1800 input - 1000 future - 300 closeout - 100 safety.
        self.assertEqual(envelope.effective_max_output_tokens, 1800)
        self.assertEqual(envelope.future_round_token_reserve, 1000)

    def test_pressure_and_minimum_viable_gate(self) -> None:
        low = self.allocate()
        medium = self.allocate(
            budget_snapshot=snapshot(limit=4600, used=1000),
            rounds_remaining=1,
        )
        high = self.allocate(
            budget_snapshot=snapshot(limit=3400, used=1000),
            rounds_remaining=1,
        )
        critical = self.allocate(
            budget_snapshot=snapshot(limit=3300, used=1000),
            rounds_remaining=1,
        )
        self.assertEqual(low.token_pressure, "LOW")
        self.assertEqual(medium.token_pressure, "MEDIUM")
        self.assertEqual(high.token_pressure, "HIGH")
        self.assertEqual(critical.token_pressure, "CRITICAL")
        self.assertFalse(critical.planner_call_allowed)
        self.assertIn("BELOW_MINIMUM_VIABLE_OUTPUT", critical.reason_codes)

    def test_existing_gate_and_context_overflow_block_planner(self) -> None:
        gated = self.allocate(existing_budget_gate_allowed=False)
        self.assertFalse(gated.planner_call_allowed)
        self.assertEqual(gated.token_pressure, "CRITICAL")
        overflow = self.allocate(estimated_input_tokens=7900, rounds_remaining=1)
        self.assertFalse(overflow.planner_call_allowed)
        self.assertIn("INPUT_EXCEEDS_CONTEXT_WINDOW", overflow.reason_codes)

    def test_dynamic_guidance_cap_shrinks_and_can_be_zero(self) -> None:
        low = self.allocate()
        medium = self.allocate(
            budget_snapshot=snapshot(limit=4600, used=1000),
            rounds_remaining=1,
        )
        high = self.allocate(
            budget_snapshot=snapshot(limit=3400, used=1000),
            rounds_remaining=1,
        )
        critical = self.allocate(
            budget_snapshot=snapshot(limit=3300, used=1000),
            rounds_remaining=1,
        )
        abstain = self.allocate(guidance_allowed=False)
        self.assertLessEqual(low.guidance_token_cap, 600)
        self.assertGreater(low.guidance_token_cap, medium.guidance_token_cap)
        self.assertGreaterEqual(medium.guidance_token_cap, high.guidance_token_cap)
        self.assertEqual(critical.guidance_token_cap, 0)
        self.assertEqual(abstain.guidance_token_cap, 0)

    def test_guidance_growth_reduces_effective_output(self) -> None:
        before = self.allocate(
            budget_snapshot=snapshot(limit=5000, used=1000),
            estimated_input_tokens=1000,
            rounds_remaining=1,
        )
        after = self.allocate(
            budget_snapshot=snapshot(limit=5000, used=1000),
            estimated_guidance_tokens=500,
            estimated_input_tokens=1500,
            rounds_remaining=1,
        )
        self.assertLess(after.effective_max_output_tokens, before.effective_max_output_tokens)

    def test_mode_defaults_and_search_closeout_reserve_are_explicit(self) -> None:
        expected = {
            "REPAIR": (1400, 700),
            "SYNTH_FIX": (1800, 800),
            "STRUCTURAL_FIX": (2200, 1000),
            "OPTIMIZE": (2400, 1000),
        }
        limits = TokenBudgetLimits()
        for mode, (cap, minimum) in expected.items():
            self.assertEqual(limits.mode_output_caps[mode], cap)
            self.assertEqual(limits.mode_minimum_viable_output[mode], minimum)

        closeout_only = TokenBudgetPolicy(
            TokenBudgetLimits(search_closeout_token_reserve=900)
        ).allocate(
            budget_snapshot=snapshot(limit=2000, used=1100),
            mode="REPAIR",
            estimated_base_prompt_tokens=0,
            estimated_guidance_tokens=0,
            estimated_input_tokens=0,
            rounds_remaining=1,
        )
        self.assertFalse(closeout_only.planner_call_allowed)
        self.assertIn(
            "ONLY_SEARCH_CLOSEOUT_TOKEN_RESERVE_REMAINS",
            closeout_only.reason_codes,
        )


class TokenPolicyIntegrationContractTests(unittest.TestCase):
    def test_product_cli_is_fixed_only_and_experiment_cli_retains_replay(self) -> None:
        args = _parser(experimental_token_policy=True).parse_args(
            [
                "--task-dir",
                "public-task",
                "--patch-file",
                "candidate.diff",
                "--run-dir",
                "run",
                "--token-budget-policy",
                "dynamic",
                "--run-token-limit",
                "12000",
                "--max-output-tokens",
                "1700",
                "--repair-max-output-tokens",
                "1300",
                "--synth-fix-max-output-tokens",
                "1600",
                "--structural-fix-max-output-tokens",
                "1900",
                "--optimize-max-output-tokens",
                "2100",
                "--minimum-viable-output-tokens",
                "650",
                "--context-window-tokens",
                "16000",
                "--token-safety-margin",
                "96",
                "--future-round-token-reserve",
                "1400",
                "--guidance-token-cap",
                "500",
            ]
        )
        self.assertEqual(args.token_budget_policy, "dynamic")
        self.assertEqual(args.run_token_limit, 12000)
        self.assertEqual(args.max_output_tokens, 1700)
        self.assertEqual(args.minimum_viable_output_tokens, 650)
        self.assertEqual(args.experience_max_guidance_tokens, 500)

        defaults = _parser().parse_args(
            [
                "--task-dir",
                "public-task",
                "--patch-file",
                "candidate.diff",
                "--run-dir",
                "run",
            ]
        )
        self.assertEqual(defaults.token_budget_policy, "fixed")
        self.assertEqual(defaults.evidence_memory, "on")
        self.assertFalse(hasattr(defaults, "token_policy_config"))

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _parser().parse_args(
                    [
                        "--task-dir",
                        "public-task",
                        "--patch-file",
                        "candidate.diff",
                        "--run-dir",
                        "run",
                        "--token-budget-policy",
                        "dynamic",
                    ]
                )
            with self.assertRaises(SystemExit):
                _parser().parse_args(
                    [
                        "--task-dir",
                        "public-task",
                        "--patch-file",
                        "candidate.diff",
                        "--run-dir",
                        "run",
                        "--max-output-tokens",
                        "1700",
                    ]
                )
            with self.assertRaises(SystemExit):
                _parser(experimental_token_policy=True).parse_args(
                    [
                        "--task-dir",
                        "public-task",
                        "--patch-file",
                        "candidate.diff",
                        "--run-dir",
                        "run",
                        "--token-budget-policy",
                        "hybrid",
                    ]
                )
            with self.assertRaises(SystemExit):
                _parser().parse_args(
                    [
                        "--task-dir",
                        "public-task",
                        "--patch-file",
                        "candidate.diff",
                        "--run-dir",
                        "run",
                        "--mod",
                        "untrusted-model",
                    ]
                )

    def test_token_policy_adds_no_graph_node(self) -> None:
        from llm4hls_agent import v3_prototype

        graph_source = inspect.getsource(v3_prototype.build_v3_prototype_graph)
        added_nodes = [
            line.strip()
            for line in graph_source.splitlines()
            if "graph.add_node" in line
            and any(name in line.casefold() for name in ("token", "envelope"))
        ]
        self.assertEqual(added_nodes, [])


if __name__ == "__main__":
    unittest.main()
