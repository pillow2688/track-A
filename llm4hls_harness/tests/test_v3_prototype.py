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


def non_improving_proposal() -> PatchProposal:
    return PatchProposal(
        patch=(
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -10,5 +10,5 @@\n"
            " vector_add_loop:\n"
            "     for (int i = 0; i < VECTOR_SIZE; ++i) {\n"
            "-#pragma HLS PIPELINE II=16\n"
            "+#pragma HLS PIPELINE II=8\n"
            "         c[i] = a[i] + b[i];\n"
            "     }\n"
        ),
        provider="scripted-prototype",
        model="fixture-v1",
        hypothesis="Try a smaller but still conservative pipeline interval.",
        change_class="pipeline",
        expected_effect="This fixture intentionally has no measured improvement.",
        risk="low",
        required_validation=("csim", "synth", "cosim"),
    )


def another_non_improving_proposal() -> PatchProposal:
    proposal = non_improving_proposal()
    return PatchProposal(
        patch=proposal.patch.replace("II=8", "II=4"),
        provider=proposal.provider,
        model=proposal.model,
        hypothesis="Try a second distinct interval with no measured gain.",
        change_class=proposal.change_class,
        expected_effect=proposal.expected_effect,
        risk=proposal.risk,
        required_validation=proposal.required_validation,
    )


def regression_after_promotion_proposal() -> PatchProposal:
    return PatchProposal(
        patch=(
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -10,5 +10,5 @@\n"
            " vector_add_loop:\n"
            "     for (int i = 0; i < VECTOR_SIZE; ++i) {\n"
            "-#pragma HLS PIPELINE II=1\n"
            "+#pragma HLS PIPELINE II=2\n"
            "         c[i] = a[i] + b[i];\n"
            "     }\n"
        ),
        provider="scripted-prototype",
        model="fixture-v1",
        hypothesis="Probe a follow-up that intentionally regresses the incumbent.",
        change_class="pipeline",
        expected_effect="The incumbent must remain selected.",
        risk="low",
        required_validation=("csim", "synth", "cosim"),
    )


def forbidden_path_proposal() -> PatchProposal:
    return PatchProposal(
        patch=(
            "--- a/kernel_tb.cpp\n"
            "+++ b/kernel_tb.cpp\n"
            "@@ -1 +1 @@\n"
            "-// public testbench\n"
            "+// forbidden edit\n"
        ),
        provider="scripted-prototype",
        model="fixture-v1",
        hypothesis="This proposal intentionally targets a forbidden file.",
        change_class="invalid",
        expected_effect="It must be rejected before Candidate allocation.",
        risk="forbidden",
        required_validation=("csim", "synth", "cosim"),
    )


def prototype_config(task, *, credit_limit: int = 80) -> RunConfig:
    tool_limits = (
        {"csim": 5, "synth": 5, "cosim": 4, "llm": 2}
        if credit_limit >= 100
        else {"csim": 4, "synth": 4, "cosim": 3, "llm": 1}
    )
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
            tool_limits=tool_limits,
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
            report = (run_root / "v3_team_report.md").read_text(encoding="utf-8")
            self.assertIn("## CoSim 晋升门控", report)
            self.assertIn("## 最终三阶段验证", report)
            self.assertIn("| CSIM | PASS |", report)
            manifest = json.loads(
                (run_root / "control" / "package_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest_paths = {item["path"] for item in manifest["artifacts"]}
            self.assertTrue(
                {
                    "v3_task_spec.json",
                    "v3_run_config.json",
                    "trace.jsonl",
                }.issubset(manifest_paths)
            )

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
                    "advance_round",
                    "evaluate_round_budget",
                    "select_final_attempt",
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
        self.assertIn("reject_candidate", nodes)
        self.assertIn("advance_round", nodes)
        self.assertIn("select_final_attempt", nodes)
        self.assertNotIn("candidate_cosim", nodes)
        self.assertNotIn("promote_candidate", nodes)

    def test_rejected_candidate_continues_to_second_round_and_promotes(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-two-rounds"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                (non_improving_proposal(), prototype_proposal()),
                backend=backend,
                thread_id="prototype-two-round-test",
            )

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["rounds_completed"], 2)
            self.assertEqual(result["no_improvement_rounds"], 0)
            self.assertEqual(result["exploration_stop_reason"], "MAX_OPTIMIZATION_ROUNDS")
            self.assertEqual(result["best_candidate_id"], "candidate_002")
            self.assertEqual(result["final_candidate_id"], "candidate_002")
            self.assertEqual(result["budget"]["credits_used"], 80)
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
                    "csim",
                    "synth",
                    "cosim",
                ],
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                registry["candidates"]["candidate_001"]["status"], "REJECTED"
            )
            self.assertEqual(
                registry["candidates"]["candidate_002"]["status"],
                "FINAL_VERIFIED",
            )
            committed = list(
                (run_root / "control" / "candidate_operations").glob(
                    "*.committed.json"
                )
            )
            self.assertEqual(len(committed), 4)

    def test_invalid_patch_is_recorded_then_next_round_promotes(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-invalid-then-promote"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task, credit_limit=75),
                (forbidden_path_proposal(), prototype_proposal()),
                backend=backend,
                thread_id="prototype-invalid-then-promote-test",
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(set(registry["candidates"]), {"candidate_000", "candidate_001"})
        self.assertEqual(result["budget"]["credits_used"], 75)
        nodes = [event["node"] for event in result["node_events"]]
        self.assertIn("record_rejected_proposal", nodes)
        self.assertLess(nodes.index("record_rejected_proposal"), nodes.index("candidate_csim"))

    def test_duplicate_patch_is_rejected_without_candidate_or_tool_replay(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        duplicate = non_improving_proposal()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-duplicate-then-promote"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                (duplicate, duplicate, prototype_proposal()),
                backend=backend,
                thread_id="prototype-duplicate-then-promote-test",
                max_no_improvement_rounds=3,
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds_completed"], 3)
        self.assertEqual(result["best_candidate_id"], "candidate_002")
        self.assertEqual(result["final_candidate_id"], "candidate_002")
        self.assertEqual(set(registry["candidates"]), {
            "candidate_000",
            "candidate_001",
            "candidate_002",
        })
        self.assertEqual(result["budget"]["credits_used"], 80)
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
                "csim",
                "synth",
                "cosim",
            ],
        )
        duplicate_events = [
            event
            for event in result["node_events"]
            if event.get("outcome") == "DUPLICATE_PROPOSAL"
        ]
        self.assertEqual(len(duplicate_events), 2)
        self.assertEqual(
            {event.get("round_index") for event in duplicate_events},
            {2},
        )

    def test_materialized_candidate_is_replayed_after_checkpoint_interruption(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_event = v3_prototype_module._event
        interrupted = False

        def interrupt_after_materialization(*args, **kwargs):
            nonlocal interrupted
            if kwargs.get("node") == "materialize_candidate" and not interrupted:
                interrupted = True
                raise RuntimeError("injected post-materialization interruption")
            return original_event(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-materialization-replay"
            with patch.object(
                v3_prototype_module,
                "_event",
                side_effect=interrupt_after_materialization,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected post-materialization interruption"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-materialization-replay-test",
                    )

            registry_before_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(registry_before_resume["candidates"]), {
                "candidate_000",
                "candidate_001",
            })
            self.assertEqual(
                registry_before_resume["candidates"]["candidate_001"]["status"],
                "MATERIALIZED",
            )
            self.assertEqual(
                [kind for kind, _optimized, _work in backend.calls],
                ["csim", "synth", "cosim"],
            )

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-materialization-replay-test",
            )
            registry_after_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(set(registry_after_resume["candidates"]), {
            "candidate_000",
            "candidate_001",
        })
        replay_events = [
            event
            for event in result["node_events"]
            if event.get("outcome") == "MATERIALIZATION_REPLAYED"
        ]
        self.assertEqual(len(replay_events), 1)
        self.assertEqual(result["budget"]["credits_used"], 75)

    def test_consecutive_rejections_stop_at_no_improvement_limit(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "v3-no-improvement-stop",
                prototype_config(task),
                (
                    non_improving_proposal(),
                    another_non_improving_proposal(),
                    prototype_proposal(),
                ),
                backend=backend,
                thread_id="prototype-no-improvement-stop-test",
                max_no_improvement_rounds=2,
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["exploration_stop_reason"], "NO_IMPROVEMENT_LIMIT")
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(result["no_improvement_rounds"], 2)
        self.assertEqual(result["best_candidate_id"], "candidate_000")
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        self.assertEqual(result["budget"]["credits_used"], 60)
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
                "csim",
                "synth",
                "cosim",
            ],
        )

    def test_later_rejection_preserves_promoted_incumbent(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-preserve-incumbent"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task, credit_limit=100),
                (prototype_proposal(), regression_after_promotion_proposal()),
                backend=backend,
                thread_id="prototype-preserve-incumbent-test",
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(
            registry["candidates"]["candidate_002"]["parent_id"], "candidate_001"
        )
        self.assertEqual(
            registry["candidates"]["candidate_002"]["status"], "REJECTED"
        )
        self.assertEqual(result["budget"]["credits_used"], 80)

    def test_promote_decision_replay_does_not_repeat_tools_or_charge(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_event = v3_prototype_module._event
        interrupted = False

        def interrupt_after_promote_commit(*args, **kwargs):
            nonlocal interrupted
            if kwargs.get("node") == "promote_candidate" and not interrupted:
                interrupted = True
                raise RuntimeError("injected post-promote interruption")
            return original_event(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-promote-replay"
            with patch.object(
                v3_prototype_module,
                "_event",
                side_effect=interrupt_after_promote_commit,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected post-promote interruption"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-promote-replay-test",
                    )
            calls_before_resume = list(backend.calls)
            ledger_before_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )["v3_revision"]

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-promote-replay-test",
            )

            self.assertEqual(
                [kind for kind, _optimized, _work in calls_before_resume],
                ["csim", "synth", "cosim", "csim", "synth", "cosim"],
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["budget"]["credits_used"], 75)
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(registry["v3_revision"], ledger_before_resume + 2)

    def test_final_commit_replay_is_idempotent_after_registry_write(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_commit = v3_prototype_module._commit_registry_operation
        interrupted = False

        def interrupt_after_final_commit(*args, **kwargs):
            nonlocal interrupted
            reference = original_commit(*args, **kwargs)
            if kwargs.get("operation_type") == "COMMIT_FINAL" and not interrupted:
                interrupted = True
                raise RuntimeError("injected post-final-commit interruption")
            return reference

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-final-commit-replay"
            with patch.object(
                v3_prototype_module,
                "_commit_registry_operation",
                side_effect=interrupt_after_final_commit,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected post-final-commit interruption"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-final-commit-replay-test",
                    )
            calls_before_resume = list(backend.calls)
            registry_before_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(registry_before_resume["v3_revision"], 3)

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-final-commit-replay-test",
            )
            registry_after_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["budget"]["credits_used"], 75)
        self.assertEqual(backend.calls, calls_before_resume)
        self.assertEqual(registry_after_resume["v3_revision"], 3)

    def test_final_commit_replay_rejects_tampered_registry(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_write = v3_prototype_module._write_once_or_verify
        interrupted = False

        def interrupt_before_final_commit_journal(path, value):
            nonlocal interrupted
            request = value.get("request") if isinstance(value, dict) else None
            if (
                Path(path).name.endswith(".committed.json")
                and isinstance(request, dict)
                and request.get("operation_type") == "COMMIT_FINAL"
                and not interrupted
            ):
                interrupted = True
                raise RuntimeError("injected pre-final-journal interruption")
            return original_write(path, value)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-final-cas-tamper"
            with patch.object(
                v3_prototype_module,
                "_write_once_or_verify",
                side_effect=interrupt_before_final_commit_journal,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected pre-final-journal interruption"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-final-cas-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            registry_path = run_root / "candidate_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["final_candidate_id"], "candidate_001")
            registry["final_candidate_id"] = None
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "REGISTRY_CAS_CONFLICT"):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-final-cas-tamper-test",
                )

        self.assertEqual(backend.calls, calls_before_resume)

    def test_terminal_package_detects_registry_tampering(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-package-tamper"
            run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-package-tamper-test",
            )
            registry_path = run_root / "candidate_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["final_candidate_id"] = "candidate_000"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "package artifact mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-package-tamper-test",
                )

    def test_terminal_package_detects_result_payload_tampering(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-result-tamper"
            run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-result-tamper-test",
            )
            result_path = run_root / "v3_prototype_result.json"
            stored = json.loads(result_path.read_text(encoding="utf-8"))
            stored["backend"]["class"] = "tampered.Backend"
            result_path.write_text(json.dumps(stored), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "result payload hash mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-result-tamper-test",
                )

    def test_terminal_package_detects_trace_tampering(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-trace-tamper"
            run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-trace-tamper-test",
            )
            trace_path = run_root / "trace.jsonl"
            trace_path.write_text(
                trace_path.read_text(encoding="utf-8") + "tampered\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "trace.jsonl"):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-trace-tamper-test",
                )

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

    def test_final_failure_uses_verified_baseline_fallback_when_affordable(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend(fail_final_cosim=True)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-final-fallback"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task, credit_limit=100),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-final-fallback-test",
                max_final_attempts=2,
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["stop_reason"], "FALLBACK_VERIFIED")
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        self.assertEqual(registry["best_candidate_id"], "candidate_001")
        self.assertEqual(registry["final_candidate_id"], "candidate_000")
        self.assertEqual(result["final_attempt_count"], 2)
        self.assertEqual(result["budget"]["credits_used"], 100)
        nodes = [event["node"] for event in result["node_events"]]
        self.assertIn("evaluate_final_fallback", nodes)
        self.assertEqual(nodes.count("final_csim"), 2)
        self.assertEqual(nodes.count("final_synth"), 2)
        self.assertEqual(nodes.count("final_cosim"), 2)

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

    def test_partial_identity_initialization_resumes_and_fills_missing_config(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_write = v3_prototype_module._write_once_or_verify
        interrupted = False

        def interrupt_before_run_config(path, value):
            nonlocal interrupted
            if Path(path).name == "v3_run_config.json" and not interrupted:
                interrupted = True
                raise RuntimeError("injected identity initialization interruption")
            return original_write(path, value)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-partial-identity"
            with patch.object(
                v3_prototype_module,
                "_write_once_or_verify",
                side_effect=interrupt_before_run_config,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected identity initialization interruption"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-partial-identity-test",
                    )

            self.assertTrue((run_root / "v3_task_spec.json").is_file())
            self.assertFalse((run_root / "v3_run_config.json").exists())
            self.assertEqual(backend.calls, [])

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-partial-identity-test",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(result["budget"]["credits_used"], 75)

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
