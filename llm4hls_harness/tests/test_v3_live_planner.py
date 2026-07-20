from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import llm4hls_agent.v3_planner_action as planner_action_module
from llm4hls_agent.task import load_public_task
from llm4hls_agent.v3_planner_action import PreparedPlannerCall
from llm4hls_agent.v3_prototype import run_v3_prototype

from .test_v3_prototype import (
    PrototypeBackend,
    prototype_config,
    prototype_proposal,
)


class FakeLiveGraphPlanner:
    replay_policy = "NON_REPLAYABLE"

    def __init__(self) -> None:
        self.prepare_calls = 0
        self.invoke_calls = 0
        self.proposal = replace(
            prototype_proposal(),
            provider="fixture-live-provider",
            model="fixture-live-model",
            revision="fixture-live-revision-1",
            input_tokens=18,
            output_tokens=9,
            cached_input_tokens=4,
            request_id="fixture-live-request-1",
            duration_seconds=0.2,
        )

    def fingerprint(self) -> str:
        return "fixture-live-graph-planner-v1"

    def prepare(self, planner_input) -> PreparedPlannerCall:
        self.prepare_calls += 1
        return PreparedPlannerCall(
            request={
                "wire_schema": "fixture-live-plan.v1",
                "model": "fixture-live-model",
                "round": planner_input["round"]["round_index"],
            },
            estimated_input_tokens=128,
            max_output_tokens=128,
        )

    def invoke(self, prepared: PreparedPlannerCall):
        self.invoke_calls += 1
        self.last_prepared = prepared
        return self.proposal


class V3LivePlannerGraphTests(unittest.TestCase):
    def test_live_planner_runs_once_is_charged_and_terminal_reentry_is_cached(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        planner = FakeLiveGraphPlanner()
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-live-planner"
            first = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                backend=backend,
                planner=planner,
                max_planner_rounds=1,
                thread_id="prototype-live-planner-test",
            )
            calls_after_first = list(backend.calls)
            ledger_after_first = (run_root / "budget_ledger.jsonl").read_bytes()
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )

            second = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                backend=backend,
                planner=planner,
                max_planner_rounds=1,
                thread_id="prototype-live-planner-test",
            )

            self.assertEqual(first["status"], "DONE")
            self.assertEqual(first["final_candidate_id"], "candidate_001")
            self.assertEqual(first["planner_contract"]["mode"], "live_non_replayable_adapter")
            self.assertEqual(first["budget"]["tokens_used"], 27)
            self.assertEqual(first["budget"]["input_tokens_used"], 18)
            self.assertEqual(first["budget"]["output_tokens_used"], 9)
            self.assertEqual(first["budget"]["cached_input_tokens_used"], 4)
            self.assertEqual(first["budget"]["tool_used"]["llm"], 1)
            self.assertTrue(first["budget"]["token_usage_complete"])
            self.assertEqual(planner.invoke_calls, 1)
            self.assertEqual(second, first)
            self.assertEqual(backend.calls, calls_after_first)
            self.assertEqual(
                (run_root / "budget_ledger.jsonl").read_bytes(),
                ledger_after_first,
            )
            candidate = registry["candidates"]["candidate_001"]
            for key in (
                "live_planner_action_id",
                "live_planner_request_ref",
                "live_planner_request_sha256",
                "live_planner_output_ref",
                "live_planner_output_sha256",
                "live_planner_started_ref",
                "live_planner_completed_ref",
            ):
                self.assertEqual(candidate[key], first[key])
            self.assertTrue(
                (run_root / first["live_planner_completed_ref"]).is_file()
            )

    def test_graph_resume_after_live_outcome_does_not_call_model_twice(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        planner = FakeLiveGraphPlanner()
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-live-planner-recovery"
            with patch.object(
                planner_action_module,
                "_after_output_persisted",
                side_effect=RuntimeError("injected after live outcome"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected after live outcome"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        backend=backend,
                        planner=planner,
                        max_planner_rounds=1,
                        thread_id="prototype-live-planner-recovery-test",
                    )

            self.assertEqual(planner.invoke_calls, 1)
            self.assertEqual(
                [kind for kind, _optimized, _work in backend.calls],
                ["csim", "synth", "cosim"],
            )

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                backend=backend,
                planner=planner,
                max_planner_rounds=1,
                thread_id="prototype-live-planner-recovery-test",
            )

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["final_candidate_id"], "candidate_001")
            self.assertEqual(result["budget"]["tokens_used"], 27)
            self.assertEqual(result["budget"]["tool_used"]["llm"], 1)
            self.assertEqual(planner.invoke_calls, 1)

    def test_graph_resume_rejects_stable_input_drift_without_reinvocation(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        planner = FakeLiveGraphPlanner()
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-live-planner-input-drift"
            with patch.object(
                planner_action_module,
                "_after_output_persisted",
                side_effect=RuntimeError("injected after live outcome"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected after live outcome"):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        backend=backend,
                        planner=planner,
                        max_planner_rounds=1,
                        thread_id="prototype-live-planner-input-drift-test",
                    )

            registry_path = run_root / "candidate_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["candidates"]["candidate_000"]["status"] = "EXTERNAL_DRIFT"
            registry_path.write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "current stable state"):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    backend=backend,
                    planner=planner,
                    max_planner_rounds=1,
                    thread_id="prototype-live-planner-input-drift-test",
                )

            self.assertEqual(planner.invoke_calls, 1)


if __name__ == "__main__":
    unittest.main()
