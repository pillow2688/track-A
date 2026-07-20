from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleOptimizationProvider,
    RepairProviderError,
    build_fast_experiment_prompt,
)
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.task import load_public_task
from llm4hls_agent.v3_openai_planner import (
    OPENAI_V3_FAST_REQUEST_SCHEMA,
    OpenAICompatibleV3PlannerAdapter,
)
from llm4hls_agent.v3_prototype import run_v3_prototype

from .test_openai_provider import envelope
from .test_v3_prototype import (
    PrototypeBackend,
    non_improving_proposal,
    prototype_config,
    prototype_proposal,
)


def fast_context() -> dict[str, object]:
    return {
        "objective": "strictly reduce dotProduct worst-case latency",
        "task": {
            "task_id": "dotProduct_optimize",
            "top": "dotProduct",
            "kernel_file": "dotProduct.cpp",
        },
        "current_kernel": "FeatureType dotProduct(...) { /* serial sum */ }",
        "description": "Optimize the public dot product kernel.",
        "read_only_headers": {
            "dotProduct.h": "#define PAR_FACTOR 32\nconst int NUM_FEATURES = 1024;"
        },
        "synth_evidence": {
            "top_latency": {"worst": 1027},
            "top_transaction_interval": {"max": 1025},
            "loops": {
                "loops": [
                    {
                        "name": "dot_product_loop",
                        "trip_count": 1024,
                        "pipeline_ii": 1,
                        "latency_cycles": 1025,
                    }
                ]
            },
            "scheduling_or_memory_evidence": ["serial accumulation dependency"],
            "resources": {
                "LUT": 156,
                "FF": 93,
                "DSP": 2,
                "BRAM_18K": 0,
                "URAM": 0,
            },
            "available_resources": {},
        },
        "recent_failures": [
            {
                "strategy_bundle": ["LOOP_PIPELINE"],
                "reason": "LATENCY_NOT_STRICTLY_IMPROVED",
            }
        ],
        "attempted_strategies": [
            {
                "strategy_bundle": ["LOOP_PIPELINE"],
                "patch_sha256": "a" * 64,
            }
        ],
        "budget": {
            "remaining_tokens": 12000,
            "remaining_credits": 35,
            "round_index": 2,
            "rounds_completed": 1,
            "max_optimization_rounds": 4,
            "max_no_improvement_rounds": 2,
            "final_reserve_credits": 25,
        },
        "constraints": {
            "allowed_files": ["dotProduct.cpp"],
            "read_only_files": ["dotProduct.h", "dotProduct_tb.cpp"],
            "preserve_top": "dotProduct",
            "preserve_interface": True,
            "preserve_numerical_semantics": True,
            "planner_cannot_choose_tools_or_final": True,
        },
    }


def fast_response(*, risk: str = "LOW") -> str:
    return json.dumps(
        {
            "hypothesis": "bank inputs and reduce 32 products in parallel",
            "primary_bottleneck": "serial accumulation across 1024 products",
            "evidence_used": [
                "top transaction interval=1025",
                "loop achieved II=1 and TripCount=1024",
                "PAR_FACTOR=32",
            ],
            "strategy_bundle": [
                "ARRAY_PARTITION",
                "LOOP_UNROLL",
                "PARALLEL_REDUCTION",
            ],
            "expected_effect": "reduce the reduction to roughly 32 groups",
            "risk": {"level": risk, "dimensions": ["resource growth"]},
            "patch": (
                "--- a/dotProduct.cpp\n"
                "+++ b/dotProduct.cpp\n"
                "@@ -5,6 +5,9 @@\n"
                " dotProduct(FeatureType param[NUM_FEATURES], DataType feature[NUM_FEATURES]) {\n"
                "     FeatureType result = 0;\n"
                "+#pragma HLS ARRAY_PARTITION variable=param cyclic factor=32 dim=1\n"
                "+#pragma HLS ARRAY_PARTITION variable=feature cyclic factor=32 dim=1\n"
                "     for (int i = 0; i < NUM_FEATURES; i++) {\n"
                "+#pragma HLS PIPELINE II=1\n"
                "         result += param[i] * feature[i];\n"
                "     }\n"
                "     return result;\n"
            ),
        },
        sort_keys=True,
    )


def dataflow_proposal() -> PatchProposal:
    proposal = dot_improving_proposal()
    return PatchProposal(
        patch=proposal.patch.replace("+5,9", "+5,10").replace(
            "     FeatureType result = 0;\n",
            "     FeatureType result = 0;\n+#pragma HLS DATAFLOW\n",
        ),
        provider=proposal.provider,
        model=proposal.model,
        hypothesis="exercise the high-risk CoSim gate",
        change_class="DATAFLOW+LOOP_PIPELINE",
        expected_effect=proposal.expected_effect,
        risk=json.dumps({"level": "HIGH", "dimensions": ["dataflow"]}),
        required_validation=("csim", "synth"),
    )


def distinct_improving_proposal() -> PatchProposal:
    proposal = dot_improving_proposal()
    return PatchProposal(
        patch=proposal.patch,
        provider=proposal.provider,
        model=proposal.model,
        hypothesis=proposal.hypothesis,
        change_class="ARRAY_PARTITION+LOOP_UNROLL",
        expected_effect=proposal.expected_effect,
        risk=json.dumps({"level": "LOW", "dimensions": []}),
        required_validation=("csim", "synth"),
    )


def dot_improving_proposal() -> PatchProposal:
    payload = json.loads(fast_response())
    return PatchProposal(
        patch=payload["patch"],
        provider="scripted-fast",
        model="fixture-v1",
        hypothesis=payload["hypothesis"],
        change_class="ARRAY_PARTITION+LOOP_UNROLL+PARALLEL_REDUCTION",
        expected_effect=payload["expected_effect"],
        risk=json.dumps(payload["risk"], sort_keys=True),
        required_validation=("csim", "synth"),
    )


def dot_non_improving_proposal() -> PatchProposal:
    proposal = dot_improving_proposal()
    return PatchProposal(
        patch=proposal.patch.replace("II=1", "II=2"),
        provider=proposal.provider,
        model=proposal.model,
        hypothesis="pipeline-only probe that does not improve measured latency",
        change_class="LOOP_PIPELINE",
        expected_effect="fixture intentionally keeps baseline latency",
        risk=json.dumps({"level": "LOW", "dimensions": []}),
        required_validation=("csim", "synth"),
    )


def official_dot_task() -> object:
    project = Path(__file__).resolve().parents[1]
    return load_public_task(
        project
        / "task_corpus"
        / "official"
        / "fpt26-harness-public"
        / "dotProduct_optimize"
    )


class V3FastExperimentTests(unittest.TestCase):
    def test_fast_schema_prompt_and_usage_are_strict_and_bounded(self) -> None:
        prompt = build_fast_experiment_prompt(fast_context())
        self.assertIn("transaction interval", prompt)
        self.assertIn("achieved II=1", prompt)
        self.assertIn('"trip_count": 1024', prompt)
        self.assertIn("PAR_FACTOR 32", prompt)
        self.assertIn("ARRAY_PARTITION", prompt)
        self.assertNotIn("hidden grader", prompt.casefold())
        self.assertNotIn("/home/", prompt)

        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://llm.example/v1",
                api_key="fast-secret-key",
                model="fixture-fast-model",
                max_output_tokens=800,
            ),
            transport=lambda _request, _timeout: (
                200,
                {},
                envelope(
                    fast_response(),
                    usage={"prompt_tokens": 700, "completion_tokens": 180},
                ),
            ),
        )
        proposal = provider.propose_fast_experiment(fast_context())
        self.assertEqual(
            proposal.change_class,
            "ARRAY_PARTITION+LOOP_UNROLL+PARALLEL_REDUCTION",
        )
        self.assertEqual(proposal.input_tokens, 700)
        self.assertEqual(proposal.output_tokens, 180)
        self.assertEqual(json.loads(proposal.risk)["level"], "LOW")
        request = provider.describe_fast_experiment_request(fast_context())
        system_prompt = request["http_body"]["messages"][0]["content"]
        self.assertIn("AMD Vitis HLS optimization Planner", system_prompt)
        self.assertIn("exact old/new line counts", system_prompt)
        self.assertNotIn("fast-secret-key", json.dumps(request))

    def test_fast_provider_repairs_only_unified_diff_hunk_counts(self) -> None:
        malformed = fast_response().replace(
            "@@ -5,6 +5,9 @@", "@@ -5,99 +5,77 @@"
        )
        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://llm.example/v1",
                api_key="fast-secret-key",
            ),
            transport=lambda _request, _timeout: (
                200,
                {},
                envelope(malformed),
            ),
        )

        proposal = provider.propose_fast_experiment(fast_context())

        self.assertIn("@@ -5,6 +5,9 @@", proposal.patch)
        self.assertNotIn("99", proposal.patch)

    def test_fast_schema_rejects_more_than_three_strategies(self) -> None:
        invalid = json.loads(fast_response())
        invalid["strategy_bundle"].append("MEMORY_BANKING")
        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://llm.example/v1",
                api_key="fast-secret-key",
            ),
            transport=lambda _request, _timeout: (
                200,
                {},
                envelope(json.dumps(invalid)),
            ),
        )
        with self.assertRaisesRegex(RepairProviderError, "1-3 unique"):
            provider.propose_fast_experiment(fast_context())

    def test_low_risk_fast_candidate_defers_cosim_until_final(self) -> None:
        task = official_dot_task()
        backend = PrototypeBackend()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "fast-low-risk",
                prototype_config(task, credit_limit=35),
                dot_improving_proposal(),
                backend=backend,
                validation_profile="fast-experiment",
                thread_id="fast-low-risk",
            )
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["budget"]["credits_used"], 35)
        self.assertEqual(result["budget"]["tool_used"]["cosim"], 1)
        self.assertEqual(result["candidate_rounds"][0]["cosim"], "NOT_RUN")
        self.assertEqual(result["candidate_rounds"][0]["latency_worst"], 256.0)
        self.assertEqual(
            [kind for kind, _optimized, _work in backend.calls],
            ["csim", "synth", "csim", "synth", "csim", "synth", "cosim"],
        )

    def test_non_improving_candidate_never_consumes_exploration_cosim(self) -> None:
        task = official_dot_task()
        backend = PrototypeBackend()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "fast-no-gain",
                prototype_config(task, credit_limit=35),
                dot_non_improving_proposal(),
                backend=backend,
                validation_profile="fast-experiment",
                thread_id="fast-no-gain",
            )
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        self.assertEqual(result["budget"]["tool_used"]["cosim"], 1)
        self.assertEqual(result["candidate_rounds"][0]["decision"], "REJECTED")
        self.assertEqual(
            result["candidate_rounds"][0]["decision_reason"],
            "LATENCY_NOT_STRICTLY_IMPROVED",
        )

    def test_high_risk_improvement_requires_exploration_cosim(self) -> None:
        task = official_dot_task()
        backend = PrototypeBackend()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "fast-high-risk",
                prototype_config(task, credit_limit=80),
                dataflow_proposal(),
                backend=backend,
                validation_profile="fast-experiment",
                thread_id="fast-high-risk",
            )
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["budget"]["tool_used"]["cosim"], 2)
        self.assertEqual(result["candidate_rounds"][0]["cosim"], "PASS")

    def test_rejection_continues_to_distinct_second_strategy(self) -> None:
        task = official_dot_task()
        backend = PrototypeBackend()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "fast-multi-round",
                prototype_config(task, credit_limit=40),
                (dot_non_improving_proposal(), distinct_improving_proposal()),
                backend=backend,
                validation_profile="fast-experiment",
                thread_id="fast-multi-round",
            )
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(result["final_candidate_id"], "candidate_002")
        self.assertEqual(result["budget"]["credits_used"], 40)
        self.assertEqual(result["budget"]["tool_used"]["cosim"], 1)

    def test_real_openai_adapter_schema_runs_through_fast_graph_with_mock_transport(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(
            project
            / "task_corpus"
            / "official"
            / "fpt26-harness-public"
            / "dotProduct_optimize"
        )
        captured: dict[str, object] = {"calls": 0}

        def transport(request, _timeout):
            captured["calls"] = int(captured["calls"]) + 1
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return 200, {}, envelope(
                fast_response(),
                usage={"prompt_tokens": 800, "completion_tokens": 200},
            )

        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://llm.example/v1",
                api_key="fast-secret-key",
                model="fixture-fast-model",
                max_output_tokens=800,
            ),
            transport=transport,
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
            run_root = Path(directory) / "fast-live-mock"
            planner = OpenAICompatibleV3PlannerAdapter(
                run_root,
                provider,
                fast_experiment=True,
                read_only_headers={
                    name: content.decode("utf-8")
                    for name, content in task.headers.items()
                },
            )
            result = run_v3_prototype(
                task,
                run_root,
                config,
                backend=PrototypeBackend(),
                planner=planner,
                max_planner_rounds=1,
                validation_profile="fast-experiment",
                thread_id="fast-live-mock",
            )
            request = json.loads(
                (run_root / result["live_planner_request_ref"]).read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["budget"]["tokens_used"], 1000)
        self.assertEqual(result["budget"]["credits_used"], 35)
        self.assertEqual(captured["calls"], 1)
        self.assertEqual(
            request["request"]["schema_version"],
            OPENAI_V3_FAST_REQUEST_SCHEMA,
        )
        prompt = captured["body"]["messages"][1]["content"]
        self.assertIn("PAR_FACTOR", prompt)
        self.assertNotIn("fast-secret-key", json.dumps(request))
        self.assertNotIn(str(task.directory), prompt)


if __name__ == "__main__":
    unittest.main()
