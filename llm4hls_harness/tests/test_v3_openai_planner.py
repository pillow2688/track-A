from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.optimization import select_optimization
from llm4hls_agent.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleOptimizationProvider,
)
from llm4hls_agent.task import load_public_task
from llm4hls_agent.v3_openai_planner import (
    OPENAI_V3_ADAPTER_REQUEST_SCHEMA,
    OpenAICompatibleV3PlannerAdapter,
    V3OpenAIPlannerError,
    _history_state,
    _metrics_with_evidence,
)
import llm4hls_agent.v3_openai_planner as openai_planner_module
from llm4hls_agent.v3_prototype import run_v3_prototype

from .test_v3_prototype import PrototypeBackend, prototype_config


def _envelope(content: str) -> bytes:
    return json.dumps(
        {
            "id": "req-v3-openai-fixture",
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 100,
                "prompt_cache_hit_tokens": 120,
            },
        }
    ).encode("utf-8")


def _optimization_response() -> str:
    return json.dumps(
        {
            "hypothesis": "the reported transaction interval is conservative",
            "optimization_class": "LOOP_PIPELINE",
            "expected_effect": "reduce interval and worst-case latency",
            "risk": "low",
            "required_validation": ["csim", "synth", "cosim"],
            "patch": (
                "--- a/kernel.cpp\n"
                "+++ b/kernel.cpp\n"
                "@@ -10,5 +10,5 @@\n"
                " vector_add_loop:\n"
                "     for (int i = 0; i < VECTOR_SIZE; ++i) {\n"
                "-#pragma HLS PIPELINE II=16\n"
                "+#pragma HLS PIPELINE II=1\n"
                "         c[i] = a[i] + b[i];\n"
                "     }\n"
            ),
        },
        sort_keys=True,
    )


class V3OpenAIPlannerTests(unittest.TestCase):
    def test_mocked_openai_provider_completes_one_live_v3_round(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        base_config = prototype_config(task)
        config = replace(
            base_config,
            budget=BudgetConfig(
                credit_limit=base_config.budget.credit_limit,
                costs=base_config.budget.costs,
                tool_limits=base_config.budget.tool_limits,
                token_limit=20_000,
                runtime_limit_seconds=base_config.budget.runtime_limit_seconds,
            ),
        )
        captured: dict[str, object] = {"calls": 0}

        def transport(request, timeout):
            captured["calls"] = int(captured["calls"]) + 1
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return 200, {}, _envelope(_optimization_response())

        provider = OpenAICompatibleOptimizationProvider(
            OpenAICompatibleConfig(
                base_url="https://llm.example/v1",
                api_key="v3-secret-fixture-key",
                model="fixture-v3-model",
                timeout_seconds=15.0,
                max_output_tokens=512,
                temperature=0.0,
            ),
            transport=transport,
        )

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-openai-adapter"
            planner = OpenAICompatibleV3PlannerAdapter(run_root, provider)
            backend = PrototypeBackend()

            first = run_v3_prototype(
                task,
                run_root,
                config,
                backend=backend,
                planner=planner,
                max_planner_rounds=1,
                thread_id="v3-openai-adapter-test",
            )
            calls_after_first = list(backend.calls)
            second = run_v3_prototype(
                task,
                run_root,
                config,
                backend=backend,
                planner=planner,
                max_planner_rounds=1,
                thread_id="v3-openai-adapter-test",
            )

            self.assertEqual(first["status"], "DONE")
            self.assertEqual(first["final_candidate_id"], "candidate_001")
            self.assertEqual(first["budget"]["tokens_used"], 600)
            self.assertEqual(first["budget"]["tool_used"]["llm"], 1)
            self.assertEqual(captured["calls"], 1)
            self.assertEqual(second, first)
            self.assertEqual(backend.calls, calls_after_first)
            self.assertEqual(
                captured["url"], "https://llm.example/v1/chat/completions"
            )
            self.assertEqual(
                captured["authorization"], "Bearer v3-secret-fixture-key"
            )

            request_audit = json.loads(
                (run_root / first["live_planner_request_ref"]).read_text(
                    encoding="utf-8"
                )
            )
            adapter_request = request_audit["request"]
            self.assertEqual(
                adapter_request["schema_version"],
                OPENAI_V3_ADAPTER_REQUEST_SCHEMA,
            )
            self.assertEqual(
                adapter_request["selection"]["optimization_class"],
                "LOOP_PIPELINE",
            )
            selection_digest = adapter_request["selection"]["metrics_digest"]
            encoded_audit = json.dumps(request_audit, sort_keys=True)
            self.assertNotIn("v3-secret-fixture-key", encoded_audit)
            self.assertNotIn(str(run_root), encoded_audit)
            self.assertNotIn(task.public_tb_name, encoded_audit)

            plan_event = next(
                event
                for event in first["node_events"]
                if event.get("node") == "plan_candidate"
            )
            decision = plan_event["details"]["proposal_decision"]
            self.assertEqual(decision["change_class"], "LOOP_PIPELINE")
            self.assertEqual(decision["input_tokens"], 500)
            self.assertEqual(decision["output_tokens"], 100)
            report = (run_root / "v3_team_report.md").read_text(encoding="utf-8")
            self.assertIn("V3-B0 Live Planner", report)
            self.assertIn('change_class":"LOOP_PIPELINE', report)

            prompt = captured["body"]["messages"][-1]["content"]
            self.assertIn('"allowed_optimization_class": "LOOP_PIPELINE"', prompt)
            self.assertIn("maximum transaction interval is 16", prompt)
            self.assertNotIn(task.public_tb_name, prompt)
            planner_input = json.loads(
                (run_root / first["planner_input_ref"]).read_text(encoding="utf-8")
            )

            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                registry["candidates"]["candidate_001"][
                    "selection_metrics_digest"
                ],
                selection_digest,
            )

            # The adapter consumes the bytes it hashed. Replacing the source
            # after resolution cannot change the prompt for that preparation.
            original_source_ref = planner_input["incumbent"]["source"]["ref"]
            original_source_path = run_root / original_source_ref
            original_source = original_source_path.read_bytes()
            original_source_path.chmod(0o600)
            baseline_copy = run_root / "planner" / "baseline-source-copy.cpp"
            baseline_copy.write_bytes(original_source)
            planner_input["baseline"]["source"] = {
                "ref": str(baseline_copy.relative_to(run_root)),
                "sha256": hashlib.sha256(original_source).hexdigest(),
            }
            resolve_binding = openai_planner_module._resolve_binding

            def replace_after_resolve(root, value, *, name, optional=False):
                artifact = resolve_binding(root, value, name=name, optional=optional)
                if name == "incumbent.source":
                    original_source_path.write_text(
                        "void unbound_replacement() {}\n", encoding="utf-8"
                    )
                return artifact

            with patch.object(
                openai_planner_module,
                "_resolve_binding",
                side_effect=replace_after_resolve,
            ):
                prepared = planner.prepare(planner_input)
            context = prepared.dispatch_context
            self.assertIn("vector_add", context.source_excerpt)
            self.assertNotIn("unbound_replacement", context.source_excerpt)
            original_source_path.write_bytes(original_source)

            metrics_ref = planner_input["incumbent"]["metrics"]["ref"]
            metrics_path = run_root / metrics_ref
            forged_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            forged_metrics["code_hash"] = "b" * 64
            encoded_metrics = (
                json.dumps(forged_metrics, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            metrics_path.write_bytes(encoded_metrics)
            planner_input["incumbent"]["metrics"]["sha256"] = hashlib.sha256(
                encoded_metrics
            ).hexdigest()

            with self.assertRaisesRegex(V3OpenAIPlannerError, "bound Synth result"):
                planner.prepare(planner_input)

    def test_history_filters_attempts_by_the_metrics_digest(self) -> None:
        metrics = {
            "latency": {"worst": 4096},
            "interval": {"max": 16},
            "loop_evidence": {"loops": []},
            "evidence": [],
        }
        first = select_optimization(
            metrics, source="", attempted=(), failures=()
        )
        self.assertEqual(first.optimization_class, "LOOP_PIPELINE")
        old_digest = "0" * 64
        attempted, _failed = _history_state(
            [
                {
                    "kind": "candidate",
                    "change_class": "LOOP_PIPELINE",
                    "selection_metrics_digest": old_digest,
                    "status": "PROMOTED",
                }
            ]
        )

        changed_metrics_decision = select_optimization(
            metrics, source="", attempted=attempted, failures=()
        )
        self.assertEqual(
            changed_metrics_decision.optimization_class, "LOOP_PIPELINE"
        )

        attempted_same, _failed = _history_state(
            [
                {
                    "kind": "candidate",
                    "change_class": "LOOP_PIPELINE",
                    "selection_metrics_digest": first.metrics_digest,
                    "status": "REJECTED",
                }
            ]
        )
        same_metrics_decision = select_optimization(
            metrics, source="", attempted=attempted_same, failures=()
        )
        self.assertNotEqual(
            same_metrics_decision.optimization_class, "LOOP_PIPELINE"
        )

    def test_empty_external_evidence_preserves_real_loop_ii_and_redacts_paths(
        self,
    ) -> None:
        report = {
            "latency": {"worst": 1024},
            "interval": {"max": 16},
            "loop_evidence": {
                "loops": [
                    {
                        "name": "dot_loop",
                        "pipeline_ii": 1,
                        "trip_count": 64,
                        "latency_cycles": 1024,
                        "source_location": "/home/runner/task/kernel.cpp:9",
                    }
                ]
            },
            "evidence": ["warning at /tmp/vitis/report.log"],
        }
        merged = _metrics_with_evidence(
            report,
            {
                "loops": [],
                "observations": [],
                "relevant_tool_log_lines": [
                    "Bearer private-value in /workspace/run/tool.log"
                ],
            },
        )
        decision = select_optimization(
            merged, source="", attempted=(), failures=()
        )

        self.assertEqual(
            merged["loop_evidence"]["loops"][0]["pipeline_ii"], 1
        )
        self.assertEqual(decision.optimization_class, "LOOP_UNROLL")
        encoded = json.dumps(merged, sort_keys=True)
        self.assertNotIn("/home/runner", encoded)
        self.assertNotIn("/tmp/vitis", encoded)
        self.assertNotIn("/workspace/run", encoded)
        self.assertNotIn("private-value", encoded)


if __name__ == "__main__":
    unittest.main()
