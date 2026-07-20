from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.workflow import RunConfig

try:
    import llm4hls_agent.v3_prototype as v3_prototype_module
    from llm4hls_agent.v3_prototype import run_v3_prototype
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("langgraph"):
        v3_prototype_module = None  # type: ignore[assignment]
        run_v3_prototype = None  # type: ignore[assignment]
    else:
        raise


class PrototypeBackend:
    """Deterministic backend with PPA tied to the submitted source."""

    def __init__(
        self,
        *,
        candidate_latency: int = 256,
        fail_final_cosim: bool = False,
    ) -> None:
        self.calls: list[tuple[str, bool, str]] = []
        self.candidate_latency = candidate_latency
        self.fail_final_cosim = fail_final_cosim
        self.cosim_calls = 0

    def fingerprint(self) -> str:
        return "v3-prototype-test-backend-v1"

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        work_dir: Path,
        **_kwargs: object,
    ) -> BackendResult:
        optimized = b"PIPELINE II=1\n" in kernel_bytes
        self.calls.append((kind, optimized, work_dir.name))
        if kind == "synth":
            latency = self.candidate_latency if optimized else 4096
            interval = 1 if optimized and latency < 4096 else 32 if optimized else 16
            return BackendResult(
                True,
                "pass",
                0,
                0.01,
                report={
                    "estimated_clock_period_ns": 5.0,
                    "latency": {
                        "best": latency,
                        "average": latency,
                        "worst": latency,
                    },
                    "interval": {"min": interval, "max": interval},
                    "resources": {
                        "LUT": 120 if optimized else 100,
                        "FF": 220 if optimized else 200,
                        "DSP": 0,
                        "BRAM_18K": 0,
                        "URAM": 0,
                    },
                    "available_resources": {
                        "LUT": 1000,
                        "FF": 2000,
                        "DSP": 100,
                        "BRAM_18K": 100,
                        "URAM": 50,
                    },
                },
            )
        if kind == "cosim":
            self.cosim_calls += 1
            if self.fail_final_cosim and self.cosim_calls == 3:
                return BackendResult(
                    False,
                    "cosim_fail",
                    1,
                    0.01,
                    evidence=["injected final C/RTL mismatch"],
                    cosim={"status": "Fail"},
                )
            return BackendResult(
                True,
                "pass",
                0,
                0.01,
                cosim={"status": "Pass"},
            )
        return BackendResult(True, "pass", 0, 0.01)


def prototype_proposal() -> PatchProposal:
    return PatchProposal(
        patch=(
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
        provider="scripted-prototype",
        model="fixture-v1",
        hypothesis="The conservative pipeline interval is the latency bottleneck.",
        change_class="pipeline",
        expected_effect="Reduce II from 16 to 1.",
        risk="low",
        required_validation=("csim", "synth", "cosim"),
    )


def prototype_config(task, *, credit_limit: int = 80) -> RunConfig:
    return RunConfig(
        tool=ToolConfig(
            vitis_root="/opt/xilinx/2025.2/Vitis",
            part=task.part,
            clock_ns=task.clock_ns,
            timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
        ),
        budget=BudgetConfig(
            credit_limit=credit_limit,
            costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
            tool_limits={"csim": 4, "synth": 4, "cosim": 3, "llm": 1},
            token_limit=1024,
            runtime_limit_seconds=300.0,
        ),
        minimum_frequency_mhz=100.0,
    )


@unittest.skipIf(run_v3_prototype is None, "V3 optional dependencies are not installed")
class V3PrototypeTests(unittest.TestCase):
    def test_action_graph_runs_baseline_candidate_gate_and_final_closure(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-prototype"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-test",
            )

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(
                result["best_candidate_id"],
                "candidate_001",
                json.dumps(result, ensure_ascii=False, indent=2),
            )
            self.assertEqual(result["final_candidate_id"], "candidate_001")
            self.assertEqual(result["stop_reason"], "CANDIDATE_PROMOTED_AND_FINALIZED")
            self.assertTrue(result["cosim_gate"]["eligible"])
            self.assertEqual(result["budget"]["credits_used"], 75)
            self.assertEqual(result["final_attempt_candidate_id"], "candidate_001")
            self.assertEqual(
                result["backend"]["evidence_level"], "TEST_OR_CUSTOM_BACKEND"
            )
            self.assertEqual(
                [kind for kind, _optimized, _work in backend.calls],
                [
                    "csim",
                    "synth",
                    "cosim",
                    "csim",
                    "synth",
                    "cosim",
                    "csim",
                    "synth",
                    "cosim",
                ],
            )
            self.assertTrue((run_root / "graph_checkpoints.sqlite").is_file())
            self.assertTrue((run_root / "v3_prototype_result.json").is_file())
            self.assertTrue((run_root / "v3_team_report.md").is_file())

            stored = json.loads(
                (run_root / "v3_prototype_result.json").read_text(encoding="utf-8")
            )
            nodes = [event["node"] for event in stored["node_events"]]
            self.assertEqual(
                nodes,
                [
                    "initialize",
                    "baseline_csim",
                    "baseline_synth",
                    "baseline_cosim",
                    "evaluate_round_budget",
                    "plan_candidate",
                    "materialize_candidate",
                    "candidate_csim",
                    "candidate_synth",
                    "candidate_score_gate",
                    "candidate_cosim_budget_gate",
                    "candidate_cosim",
                    "promote_candidate",
                    "evaluate_final_budget",
                    "final_csim",
                    "final_synth",
                    "final_cosim",
                    "write_report",
                ],
            )

    def test_non_improving_candidate_skips_cosim_and_finalizes_baseline(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend(candidate_latency=5000)

        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "v3-reject",
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-reject-test",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["best_candidate_id"], "candidate_000")
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        self.assertEqual(result["stop_reason"], "BASELINE_FINALIZED_NO_IMPROVEMENT")
        self.assertFalse(result["cosim_gate"]["eligible"])
        self.assertEqual(result["cosim_gate"]["reason"], "OFFICIAL_SCORE_NOT_BETTER")
        self.assertEqual(result["budget"]["credits_used"], 55)
        self.assertEqual(
            [kind for kind, _optimized, _work in backend.calls],
            [
                "csim",
                "synth",
                "cosim",
                "csim",
                "synth",
                "csim",
                "synth",
                "cosim",
            ],
        )
        nodes = [event["node"] for event in result["node_events"]]
        self.assertIn("select_baseline", nodes)
        self.assertNotIn("candidate_cosim", nodes)
        self.assertNotIn("promote_candidate", nodes)

    def test_final_cosim_failure_is_a_structured_terminal_report(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend(fail_final_cosim=True)

        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "v3-final-fail",
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-final-fail-test",
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "FINAL_COSIM_FAILED")
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertIsNone(result["final_candidate_id"])
        self.assertEqual(result["final_attempt_candidate_id"], "candidate_001")
        self.assertEqual(result["budget"]["credits_used"], 75)
        self.assertEqual(result["node_events"][-1]["node"], "write_report")

    def test_terminal_reentry_returns_durable_result_without_new_actions(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-reentry"
            first = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-reentry-test",
            )
            calls_after_first = list(backend.calls)
            ledger_after_first = (run_root / "budget_ledger.jsonl").read_bytes()
            report_after_first = (run_root / "v3_team_report.md").read_text(
                encoding="utf-8"
            )
            (run_root / "v3_team_report.md").write_text("", encoding="utf-8")
            second = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-reentry-test",
            )

            self.assertEqual(second, first)
            self.assertEqual(backend.calls, calls_after_first)
            self.assertEqual(
                (run_root / "budget_ledger.jsonl").read_bytes(), ledger_after_first
            )
            self.assertEqual(
                (run_root / "v3_team_report.md").read_text(encoding="utf-8"),
                report_after_first,
            )

    def test_incomplete_checkpoint_resumes_from_failed_graph_node(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-checkpoint-resume"
            with patch(
                "llm4hls_agent.v3_prototype._candidate_cosim",
                side_effect=RuntimeError("injected graph interruption"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected graph interruption"):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-checkpoint-resume-test",
                    )
            calls_before_resume = list(backend.calls)
            self.assertEqual(
                [kind for kind, _optimized, _work in calls_before_resume],
                ["csim", "synth", "cosim", "csim", "synth"],
            )

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-checkpoint-resume-test",
            )

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["budget"]["credits_used"], 75)
            self.assertEqual(
                [kind for kind, _optimized, _work in backend.calls],
                [
                    "csim",
                    "synth",
                    "cosim",
                    "csim",
                    "synth",
                    "cosim",
                    "csim",
                    "synth",
                    "cosim",
                ],
            )

    def test_report_then_result_terminal_commit_recovers_after_interruption(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_atomic_json = v3_prototype_module._atomic_json

        def interrupt_result_commit(path: Path, value: object) -> None:
            if path.name == "v3_prototype_result.json":
                raise RuntimeError("injected terminal commit interruption")
            original_atomic_json(path, value)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-terminal-commit-resume"
            with patch.object(
                v3_prototype_module,
                "_atomic_json",
                side_effect=interrupt_result_commit,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected terminal commit interruption"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-terminal-commit-resume-test",
                    )
            calls_before_resume = list(backend.calls)
            self.assertTrue((run_root / "v3_team_report.md").stat().st_size > 0)
            self.assertFalse((run_root / "v3_prototype_result.json").exists())

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-terminal-commit-resume-test",
            )

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(backend.calls, calls_before_resume)
            self.assertTrue((run_root / "v3_prototype_result.json").is_file())

    def test_round_is_skipped_when_only_final_closure_is_affordable(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "v3-final-reserve",
                prototype_config(task, credit_limit=55),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-final-reserve-test",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        self.assertEqual(result["budget"]["credits_used"], 50)
        self.assertEqual(
            result["cosim_gate"]["reason"], "ROUND_SKIPPED_FINAL_RESERVE"
        )
        self.assertEqual(
            [kind for kind, _optimized, _work in backend.calls],
            ["csim", "synth", "cosim", "csim", "synth", "cosim"],
        )
        nodes = [event["node"] for event in result["node_events"]]
        self.assertNotIn("plan_candidate", nodes)
        self.assertIn("evaluate_final_budget", nodes)

    def test_run_fails_before_tools_when_baseline_and_final_do_not_fit(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "v3-preflight-budget",
                prototype_config(task, credit_limit=40),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-preflight-budget-test",
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(
            result["stop_reason"], "BASELINE_AND_FINAL_CLOSURE_UNAFFORDABLE"
        )
        self.assertEqual(result["budget"]["credits_used"], 0)
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
