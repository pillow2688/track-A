from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from llm4hls_agent.budget import (
    BudgetConfig,
    BudgetExceeded,
    TOKEN_POLICY_HYBRID_VERSION,
    TokenBudgetLimits,
    TokenBudgetPolicy,
    TokenEstimator,
)
from llm4hls_agent.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleOptimizationProvider,
    _token_budget_prompt,
)
from llm4hls_agent.task import load_public_task
from llm4hls_agent.v3_openai_planner import OpenAICompatibleV3PlannerAdapter
from llm4hls_agent.v3_planner import build_planner_input
from llm4hls_agent.v3_prototype import run_v3_prototype

from .test_openai_provider import envelope
from .test_v3_fast_experiment import fast_response
from .test_v3_prototype import PrototypeBackend, prototype_config


def _limits() -> TokenBudgetLimits:
    return TokenBudgetLimits(
        mode_output_caps={mode: 500 for mode in (
            "REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"
        )},
        mode_minimum_viable_output={mode: 200 for mode in (
            "REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"
        )},
        provider_hard_output_cap=1000,
        context_window_tokens=8192,
        token_budget_safety_margin=128,
        future_round_token_reserve=100,
    )


def _planner_input(
    run_root: Path,
    *,
    round_index: int = 1,
    history: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_path = run_root / "candidates" / "candidate_000" / "kernel.cpp"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source = b"void top(int *a) { a[0] = 0; }\n"
    source_path.write_bytes(source)
    source_sha = hashlib.sha256(source).hexdigest()
    candidate = {
        "candidate_id": "candidate_000",
        "parent_id": None,
        "kind": "baseline",
        "status": "BASELINE",
        "source": {
            "ref": source_path.relative_to(run_root).as_posix(),
            "sha256": source_sha,
        },
        "code_hash": source_sha,
        "metrics": {"ref": None, "sha256": None},
        "synth_evidence": {"ref": None, "sha256": None},
        "validation": {},
    }
    return build_planner_input(
        task={
            "task_id": "hybrid-fixture",
            "task_type": "repair",
            "difficulty": 1,
            "top": "top",
            "part": "xcu55c-fsvh2892-2L-e",
            "clock_ns": 10.0,
            "requires_cosim": False,
            "initial_condition": "repair public behavior",
            "description": "A public Hybrid fixture.",
            "kernel_file": "kernel.cpp",
            "public_tb": "kernel_tb.cpp",
        },
        round_state={
            "round_index": round_index,
            "rounds_completed": round_index - 1,
            "parent_candidate_id": "candidate_000",
            "mode": "REPAIR",
            "failure_evidence": {
                "schema_version": "v3c.csim-failure-evidence.v1",
                "failure_kind": "runtime_fail",
                "error_summary": "public mismatch",
                "source_locations": ["kernel.cpp:1"],
                "relevant_log_lines": ["expected 1 actual 0"],
            },
        },
        incumbent=candidate,
        baseline=candidate,
        history=history or [],
        policy={"max_optimization_rounds": 4},
        budget={
            "run_token_limit": 6000,
            "token_limit": 6000,
            "tokens_used": 0,
            "tokens_remaining": 6000,
            "credits_remaining": 80,
        },
    )


def _planner(run_root: Path) -> OpenAICompatibleV3PlannerAdapter:
    provider = OpenAICompatibleOptimizationProvider(
        OpenAICompatibleConfig(
            base_url="https://llm.example/v1",
            api_key="hybrid-secret",
            model="fixture-hybrid",
            max_output_tokens=1000,
        )
    )
    return OpenAICompatibleV3PlannerAdapter(
        run_root,
        provider,
        fast_experiment=True,
        read_only_headers={"kernel.h": "void top(int *a);\n"},
        token_budget_policy=TokenBudgetPolicy(_limits(), profile="hybrid"),
        token_estimator=TokenEstimator(
            tokenizer=lambda text: text.split(), estimator_name="fixture-tokenizer"
        ),
    )


class HybridTokenPolicyTests(unittest.TestCase):
    def test_low_medium_high_and_critical_allocation(self) -> None:
        policy = TokenBudgetPolicy(_limits(), profile="hybrid")

        def allocate(remaining: int):
            return policy.allocate(
                budget_snapshot={
                    "run_token_limit": remaining,
                    "tokens_used": 0,
                    "tokens_remaining": remaining,
                },
                mode="REPAIR",
                estimated_base_prompt_tokens=200,
                estimated_guidance_tokens=0,
                estimated_input_tokens=200,
                rounds_remaining=2,
                guidance_allowed=False,
            )

        low = allocate(2000)
        medium = allocate(1000)
        high = allocate(850)
        critical = allocate(400)
        self.assertEqual(low.token_pressure, "LOW")
        self.assertEqual(medium.token_pressure, "MEDIUM")
        self.assertEqual(high.token_pressure, "HIGH")
        self.assertEqual(critical.token_pressure, "CRITICAL")
        self.assertEqual(low.effective_max_output_tokens, 500)
        self.assertEqual(medium.effective_max_output_tokens, 500)
        self.assertLess(high.effective_max_output_tokens, 500)
        self.assertFalse(critical.planner_call_allowed)
        self.assertEqual(low.policy_version, TOKEN_POLICY_HYBRID_VERSION)

    def test_low_prompt_hides_envelope_and_uses_stable_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = _planner(root).prepare(_planner_input(root))
            envelope = prepared.request["token_envelope"]
            prompt = prepared.request["provider_request"]["http_body"]["messages"][1]["content"]
            self.assertEqual(envelope["policy_version"], TOKEN_POLICY_HYBRID_VERSION)
            self.assertEqual(envelope["token_pressure"], "LOW")
            self.assertEqual(prepared.max_output_tokens, 500)
            self.assertNotIn("TOKEN BUDGET", prompt)
            self.assertEqual(
                prepared.request["planner_call_gate"]["decision"], "ALLOW"
            )

    def test_medium_prompt_uses_only_minimal_budget_line(self) -> None:
        limits = _limits()
        policy = TokenBudgetPolicy(limits, profile="hybrid")
        allocation = policy.allocate(
            budget_snapshot={
                "run_token_limit": 1000,
                "tokens_used": 0,
                "tokens_remaining": 1000,
            },
            mode="REPAIR",
            estimated_base_prompt_tokens=200,
            estimated_guidance_tokens=0,
            estimated_input_tokens=200,
            rounds_remaining=2,
            guidance_allowed=False,
        )
        self.assertEqual(allocation.token_pressure, "MEDIUM")
        prompt = _token_budget_prompt(allocation.to_dict())
        self.assertIn("TOKEN BUDGET: pressure=MEDIUM", prompt)
        self.assertIn("one concise hypothesis", prompt)
        self.assertNotIn("Remaining run tokens", prompt)
        self.assertEqual(prompt.count("TOKEN BUDGET"), 1)

    def test_second_call_without_new_structured_evidence_is_blocked(self) -> None:
        history = [
            {
                "kind": "candidate",
                "candidate_id": "candidate_001",
                "round_index": 1,
                "change_class": "FUNCTIONAL_REPAIR",
                "patch_sha256": "a" * 64,
                "status": "REJECTED",
                "rejection_reason": "CSIM_FAIL",
                "metrics": {"ref": None, "sha256": None},
                "synth_evidence": {"ref": None, "sha256": None},
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(BudgetExceeded, "NO_NEW_STRUCTURED_EVIDENCE"):
                _planner(root).prepare(
                    _planner_input(root, round_index=2, history=history)
                )
            gate = json.loads(
                (root / "planner/call_gates/round_002.json").read_text()
            )
            self.assertEqual(gate["decision"], "BLOCK")

    def test_second_call_with_new_synth_evidence_is_allowed(self) -> None:
        history = [
            {
                "kind": "candidate",
                "candidate_id": "candidate_001",
                "round_index": 1,
                "change_class": "FUNCTIONAL_REPAIR",
                "patch_sha256": "a" * 64,
                "status": "REJECTED",
                "rejection_reason": "SYNTH_FAIL",
                "metrics": {"ref": "actions/x/result.json", "sha256": "b" * 64},
                "synth_evidence": {"ref": "evidence/synth/x.json", "sha256": "c" * 64},
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = _planner(root).prepare(
                _planner_input(root, round_index=2, history=history)
            )
            self.assertEqual(
                prepared.request["planner_call_gate"]["decision"], "ALLOW"
            )

    def test_hybrid_live_planner_reaches_fresh_final_and_reports_gate(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(
            project
            / "task_corpus"
            / "official"
            / "fpt26-harness-public"
            / "dotProduct_optimize"
        )
        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://llm.example/v1",
                api_key="hybrid-secret",
                model="fixture-hybrid",
                max_output_tokens=1000,
            ),
            transport=lambda _request, _timeout: (
                200,
                {},
                envelope(
                    fast_response(),
                    usage={"prompt_tokens": 800, "completion_tokens": 200},
                ),
            ),
        )
        base = prototype_config(task, credit_limit=40)
        config = replace(
            base,
            budget=BudgetConfig(
                credit_limit=40,
                costs=base.budget.costs,
                tool_limits=base.budget.tool_limits,
                token_limit=100_000,
                runtime_limit_seconds=300.0,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "hybrid-live-graph"
            planner = OpenAICompatibleV3PlannerAdapter(
                run_root,
                provider,
                fast_experiment=True,
                read_only_headers={
                    name: content.decode("utf-8")
                    for name, content in task.headers.items()
                },
                token_budget_policy=TokenBudgetPolicy(_limits(), profile="hybrid"),
                token_estimator=TokenEstimator(
                    tokenizer=lambda text: text.split(),
                    estimator_name="fixture-tokenizer",
                ),
            )
            result = run_v3_prototype(
                task,
                run_root,
                config,
                backend=PrototypeBackend(),
                planner=planner,
                max_planner_rounds=1,
                validation_profile="fast-experiment",
                thread_id="hybrid-live-graph",
            )
            report = (run_root / "v3_team_report.md").read_text(encoding="utf-8")

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(
            {
                stage: result["final_validation"][stage]["status"]
                for stage in ("csim", "synth", "cosim")
            },
            {"csim": "PASS", "synth": "PASS", "cosim": "PASS"},
        )
        self.assertEqual(result["planner_call_gates"][0]["decision"], "ALLOW")
        self.assertIn("Planner Call Gate", report)


if __name__ == "__main__":
    unittest.main()
