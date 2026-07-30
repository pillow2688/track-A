from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.repair import RepairProviderError
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.v3_planner_action import PreparedPlannerCall
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


class VersionedPrototypeBackend(PrototypeBackend):
    """Same deterministic semantics with a configurable implementation ID."""

    def __init__(self, fingerprint: str) -> None:
        super().__init__()
        self._fingerprint = fingerprint

    def fingerprint(self) -> str:
        return self._fingerprint


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


def wrong_new_start_proposal() -> PatchProposal:
    return PatchProposal(
        patch=(
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,2 +1,1 @@\n"
            ' #include "kernel.h"\n'
            "-\n"
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
        hypothesis="Exercise strict multi-hunk coordinate validation.",
        change_class="pipeline",
        expected_effect="The malformed metadata must be rejected.",
        risk="low",
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
    def test_unknown_live_planner_usage_seals_a_formal_terminal_failure(self) -> None:
        class UnknownUsagePlanner:
            replay_policy = "NON_REPLAYABLE"

            def __init__(self) -> None:
                self.invoke_calls = 0

            def fingerprint(self) -> str:
                return "unknown-usage-live-planner-v1"

            def prepare(self, value):
                return PreparedPlannerCall(
                    request={"model": "fixture", "messages": []},
                    estimated_input_tokens=8,
                    max_output_tokens=16,
                )

            def invoke(self, _prepared):
                self.invoke_calls += 1
                raise RepairProviderError(
                    "transport unavailable",
                    usage_complete=False,
                )

        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        base = prototype_config(task, credit_limit=100)
        config = replace(
            base,
            budget=BudgetConfig(
                credit_limit=100,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 1},
                tool_limits={"csim": 5, "synth": 5, "cosim": 4, "llm": 2},
                token_limit=1024,
                runtime_limit_seconds=300.0,
            ),
        )
        planner = UnknownUsagePlanner()
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "unknown-provider-usage"
            result = run_v3_prototype(
                task,
                run_root,
                config,
                backend=PrototypeBackend(),
                planner=planner,
                thread_id="unknown-provider-usage",
                validation_profile="fast-experiment",
                final_validation_policy="task_contract",
            )
            persisted = json.loads(
                (run_root / "v3_prototype_result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(planner.invoke_calls, 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(
            result["stop_reason"], "PLANNER_USAGE_UNKNOWN_AFTER_DISPATCH"
        )
        self.assertEqual(persisted["status"], "FAILED")
        self.assertEqual(
            persisted["stop_reason"], "PLANNER_USAGE_UNKNOWN_AFTER_DISPATCH"
        )
        self.assertEqual(result["budget"]["tool_used"]["llm"], 1)
        self.assertFalse(result["budget"]["token_usage_complete"])
        self.assertIsNone(result["final_candidate_id"])

    def test_planner_token_summary_rejects_ledger_mismatch(self) -> None:
        self.assertIsNotNone(v3_prototype_module)

        with self.assertRaisesRegex(
            RuntimeError,
            "Planner token audit count does not match the Agent Ledger",
        ):
            v3_prototype_module._planner_token_summary(
                [],
                {"tool_used": {"llm": 1}},
            )

    def test_task_aware_request_audit_counts_planner_call(self) -> None:
        self.assertIsNotNone(v3_prototype_module)
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            action_id = "task-aware-action"
            input_ref = "planner/inputs/round_001.json"
            request_ref = "planner/requests/task-aware-request.json"
            input_path = run_root / input_ref
            request_path = run_root / request_ref
            started_path = (
                run_root
                / "control"
                / "live_planner_actions"
                / f"{action_id}.started.json"
            )
            outcome_path = (
                run_root / "planner" / "live_outcomes" / f"{action_id}.json"
            )
            for path in (input_path, request_path, started_path, outcome_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            input_path.write_text(
                json.dumps(
                    {"round": {"round_index": 1, "mode": "REPAIR"}}
                ),
                encoding="utf-8",
            )
            request_path.write_text(
                json.dumps(
                    {
                        "estimated_input_tokens": 1200,
                        "configured_max_output_tokens": 4096,
                        "effective_max_output_tokens": 4096,
                        "request": {
                            "schema_version": "v3c.openai-task-aware-request.v1"
                        },
                    }
                ),
                encoding="utf-8",
            )
            started_path.write_text(
                json.dumps(
                    {
                        "action_id": action_id,
                        "request": {
                            "input_ref": input_ref,
                            "request_ref": request_ref,
                        },
                    }
                ),
                encoding="utf-8",
            )
            outcome_path.write_text(
                json.dumps(
                    {
                        "proposal": {
                            "input_tokens": 300,
                            "output_tokens": 100,
                            "finish_reason": "stop",
                            "output_truncated": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            runtime = type("Runtime", (), {"run_root": run_root})()

            rows = v3_prototype_module._planner_token_rounds(runtime)
            summary = v3_prototype_module._planner_token_summary(
                rows,
                {
                    "run_token_limit": 32768,
                    "tokens_used": 400,
                    "tool_used": {"llm": 1},
                },
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_audit_source"], "planner_request_audit")
        self.assertEqual(rows[0]["final_input_estimate"], 1200)
        self.assertEqual(rows[0]["actual_input_tokens"], 300)
        self.assertEqual(rows[0]["actual_output_tokens"], 100)
        self.assertEqual(summary["planner_calls"], 1)

    def test_task_contract_final_skips_cosim_only_when_not_required(self) -> None:
        project = Path(__file__).resolve().parents[1]
        structural_task = load_public_task(
            project / "examples" / "u55c_v2_optimize_task"
        )
        optional_cosim_task = replace(
            structural_task, task_type="optimize", requires_cosim=False
        )
        backend = PrototypeBackend()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                optional_cosim_task,
                Path(directory) / "task-contract-final",
                prototype_config(optional_cosim_task, credit_limit=100),
                prototype_proposal(),
                backend=backend,
                thread_id="task-contract-final",
                validation_profile="fast-experiment",
                final_validation_policy="task_contract",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["final_validation"]["csim"]["status"], "PASS")
        self.assertEqual(result["final_validation"]["synth"]["status"], "PASS")
        self.assertEqual(result["final_validation"]["cosim"]["status"], "NOT_RUN")
        accounting = result["track_a_budget_accounting"]
        self.assertEqual(accounting["search_closeout_cost"], 5)
        self.assertEqual(
            accounting["final_certification_agent_credits_charged"], 0
        )
        self.assertTrue(accounting["reconciled"])

    def test_task_contract_final_keeps_required_cosim(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "required-task-contract-final",
                prototype_config(task, credit_limit=100),
                prototype_proposal(),
                backend=PrototypeBackend(),
                thread_id="required-task-contract-final",
                validation_profile="fast-experiment",
                final_validation_policy="task_contract",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["final_validation"]["cosim"]["status"], "PASS")
        self.assertEqual(
            result["track_a_budget_accounting"]["search_closeout_cost"],
            25,
        )

    def test_eight_x_acceleration_stops_only_further_latency_follow_up(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "track-a-8x-cap",
                prototype_config(task, credit_limit=100),
                (prototype_proposal(), another_non_improving_proposal()),
                backend=PrototypeBackend(candidate_latency=256),
                thread_id="track-a-8x-cap",
                continuation_policy_mode="shadow",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds_completed"], 1)
        self.assertEqual(result["exploration_stop_reason"], "ACCELERATION_CAP_REACHED")
        cap_events = [
            event
            for event in result["node_events"]
            if event["node"] == "evaluate_round_budget"
            and event["outcome"] == "ACCELERATION_CAP_REACHED"
        ]
        self.assertEqual(len(cap_events), 1)
        cap = cap_events[0]["details"]["acceleration_cap"]
        self.assertTrue(cap["reached"])
        self.assertGreaterEqual(cap["acceleration"], 8.0)
        accounting = result["track_a_budget_accounting"]
        self.assertEqual(
            accounting["final_certification_agent_credits_charged"], 0
        )
        self.assertTrue(accounting["reconciled"])
        self.assertEqual(
            accounting["agent_search_cost"],
            result["budget"]["credits_used"],
        )

    def test_continuation_off_disables_eight_x_stop_for_a2_ablation(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "track-a-8x-cap-a2-off",
                prototype_config(task, credit_limit=100),
                (prototype_proposal(), another_non_improving_proposal()),
                backend=PrototypeBackend(candidate_latency=256),
                thread_id="track-a-8x-cap-a2-off",
                continuation_policy_mode="off",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds_completed"], 2)
        self.assertNotEqual(
            result["exploration_stop_reason"],
            "ACCELERATION_CAP_REACHED",
        )
        self.assertFalse(
            any(
                event["node"] == "evaluate_round_budget"
                and event["outcome"] == "ACCELERATION_CAP_REACHED"
                for event in result["node_events"]
            )
        )

    def test_shadow_continuation_persists_decision_without_changing_first_route(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v3f-shadow"
            result = run_v3_prototype(
                task, root, prototype_config(task), prototype_proposal(),
                backend=PrototypeBackend(), thread_id="v3f-shadow-test",
                continuation_policy_mode="shadow",
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["continuation_policy_mode"], "shadow")
            reference = result["continuation_decision_ref"]
            self.assertIsInstance(reference, str)
            decision = json.loads((root / reference).read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "ALLOW")
            self.assertEqual(decision["round_index"], 1)
            self.assertTrue((root / result["performance_area_ref"]).is_file())

    def _invoke_continuation_route(
        self,
        *,
        policy_mode: str,
        policy_decision: str,
        budget_allowed: bool,
        round_index: int,
    ) -> tuple[bool, dict[str, object]]:
        self.assertIsNotNone(v3_prototype_module)
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(
                continuation_policy_mode=policy_mode,
                run_root=Path(directory),
                max_planner_rounds=2,
                live_planner=None,
                proposals=(prototype_proposal(), prototype_proposal()),
                config=SimpleNamespace(budget=object()),
            )
            state = {
                "round_index": round_index,
                "mode": "OPTIMIZE",
                "best_candidate_id": "candidate_000",
                "baseline_candidate_id": "candidate_000",
                "no_improvement_rounds": 0,
            }

            def decision(*_args: object, **_kwargs: object) -> dict[str, object]:
                return {
                    "schema_version": "v3.continuation-decision.v2",
                    "mode": "OPTIMIZE",
                    "decision": policy_decision,
                    "confidence": "HIGH",
                    "reason_codes": ["FIXTURE_DECISION"],
                    "supporting_evidence": [],
                    "fallback": "FOLLOW_EXISTING_MAIN_POLICY",
                    "decision_digest": "d" * 64,
                }

            with (
                patch.object(
                    v3_prototype_module,
                    "BudgetLedger",
                    return_value=SimpleNamespace(snapshot=lambda: {}),
                ),
                patch.object(
                    v3_prototype_module,
                    "_continuation_history",
                    return_value=((), (), (), None),
                ),
                patch.object(
                    v3_prototype_module,
                    "_continuation_evidence",
                    return_value=({}, {}, {}, {}),
                ),
                patch.object(
                    v3_prototype_module,
                    "evidence_delta",
                    return_value={},
                ),
                patch.object(
                    v3_prototype_module,
                    "strategy_novelty",
                    return_value={},
                ),
                patch.object(
                    v3_prototype_module,
                    "performance_area_delta",
                    return_value={},
                ),
                patch.object(
                    v3_prototype_module,
                    "_acceleration_cap_status",
                    return_value={
                        "baseline_latency": None,
                        "incumbent_eligible": False,
                        "csim_passed": False,
                        "synth_passed": False,
                        "cosim_required": False,
                        "cosim_passed": False,
                    },
                ),
                patch.object(
                    v3_prototype_module,
                    "continuation_cost",
                    return_value={},
                ),
                patch.object(
                    v3_prototype_module,
                    "_continuation_v2_pre_state",
                    return_value={},
                ),
                patch.object(
                    v3_prototype_module,
                    "continuation_decision_v2",
                    side_effect=decision,
                ),
            ):
                allowed, artifact, *_rest = (
                    v3_prototype_module._apply_continuation_policy(
                        runtime,
                        state,
                        budget_gate={"allowed": budget_allowed},
                        estimated_tokens=0,
                        estimated_credits=0,
                    )
                )
        return allowed, artifact

    def test_continuation_enforce_changes_only_allowed_followup_route(self) -> None:
        shadow_allowed, shadow = self._invoke_continuation_route(
            policy_mode="shadow",
            policy_decision="BLOCK",
            budget_allowed=True,
            round_index=2,
        )
        enforce_allowed, enforce = self._invoke_continuation_route(
            policy_mode="enforce",
            policy_decision="BLOCK",
            budget_allowed=True,
            round_index=2,
        )

        self.assertTrue(shadow_allowed)
        self.assertFalse(enforce_allowed)
        self.assertEqual(shadow["decision"], "BLOCK")
        self.assertEqual(enforce["decision"], "BLOCK")

    def test_continuation_enforce_cannot_overturn_b1_budget_rejection(self) -> None:
        allowed, artifact = self._invoke_continuation_route(
            policy_mode="enforce",
            policy_decision="ALLOW",
            budget_allowed=False,
            round_index=2,
        )

        self.assertFalse(allowed)
        self.assertEqual(artifact["decision"], "ALLOW")

    def test_continuation_enforce_always_allows_first_planner_call(self) -> None:
        allowed, artifact = self._invoke_continuation_route(
            policy_mode="enforce",
            policy_decision="BLOCK",
            budget_allowed=True,
            round_index=1,
        )

        self.assertTrue(allowed)
        self.assertEqual(artifact["decision"], "ALLOW")
        self.assertIn("INITIAL_PLANNER_CALL", artifact["reason_codes"])

    def test_evidence_memory_off_removes_cross_candidate_history_only(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "b0-evidence-off"
            result = run_v3_prototype(
                task,
                root,
                prototype_config(task, credit_limit=100),
                (
                    non_improving_proposal(),
                    another_non_improving_proposal(),
                ),
                backend=PrototypeBackend(),
                thread_id="b0-evidence-off",
                max_no_improvement_rounds=3,
                evidence_memory_mode="off",
                continuation_policy_mode="off",
            )
            second_input = json.loads(
                (root / "planner" / "inputs" / "round_002.json").read_text(
                    encoding="utf-8"
                )
            )
            run_config = json.loads(
                (root / "v3_run_config.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["evidence_memory_mode"], "off")
        self.assertEqual(second_input["history"], [])
        self.assertEqual(
            second_input["policy"]["evidence_memory_mode"], "off"
        )
        self.assertEqual(run_config["evidence_memory_mode"], "off")
        self.assertGreater(result["budget"]["tool_used"]["csim"], 0)
        self.assertGreater(result["budget"]["tool_used"]["synth"], 0)
        self.assertNotIn("continuation_decision_ref", second_input)

    def test_evidence_memory_off_rejects_hidden_continuation_dependency(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError,
                "Continuation requires evidence memory on",
            ):
                run_v3_prototype(
                    task,
                    Path(directory) / "invalid-a1-a2",
                    prototype_config(task),
                    prototype_proposal(),
                    backend=PrototypeBackend(),
                    evidence_memory_mode="off",
                    continuation_policy_mode="shadow",
                )

    def test_mode_specific_continuation_v2_is_bound_into_terminal_result(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v3f-shadow-v2"
            result = run_v3_prototype(
                task,
                root,
                prototype_config(task),
                prototype_proposal(),
                backend=PrototypeBackend(),
                thread_id="v3f-shadow-v2-test",
                continuation_policy_mode="shadow",
                continuation_policy_version="v2",
            )
            decision = json.loads(
                (root / result["continuation_decision_ref"]).read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result["continuation_policy_version"], "v2")
        self.assertEqual(
            decision["schema_version"], "v3.continuation-decision.v2"
        )
        self.assertEqual(
            decision["policy_version"], "v3.continuation-policy.v3"
        )
        self.assertIn("pre_state", decision)
        self.assertNotIn("outcome", decision["pre_state"])
        self.assertNotIn("final_result", decision["pre_state"])

    def test_removed_v1_is_rejected_and_enforce_requires_admission(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError,
                "only v2 is available",
            ):
                run_v3_prototype(
                    task,
                    Path(directory) / "v1-enforce",
                    prototype_config(task),
                    prototype_proposal(),
                    backend=PrototypeBackend(),
                    continuation_policy_mode="enforce",
                    continuation_policy_version="v1",
                )
            with self.assertRaisesRegex(
                ValueError,
                "requires a passing admission manifest",
            ):
                run_v3_prototype(
                    task,
                    Path(directory) / "v2-enforce-no-gate",
                    prototype_config(task),
                    prototype_proposal(),
                    backend=PrototypeBackend(),
                    continuation_policy_mode="enforce",
                    continuation_policy_version="v2",
                )

    def test_historical_v3_gate_admission_fails_closed_after_commit_change(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        admission = (
            project.parent
            / "docs"
            / "experiments"
            / "artifacts"
            / "2026-07-28-track-a-rc1-head-bound"
            / "a2"
            / "continuation-v3-admission.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError, "Continuation admission current commit mismatch"
            ):
                run_v3_prototype(
                    task,
                    Path(directory) / "historical-v3-enforce",
                    prototype_config(task),
                    prototype_proposal(),
                    backend=PrototypeBackend(),
                    continuation_policy_mode="enforce",
                    continuation_policy_version="v2",
                    continuation_admission_manifest=admission,
                )

    def test_legacy_checkpoint_without_mode_keeps_optimize_cosim_route(self) -> None:
        self.assertIsNotNone(v3_prototype_module)
        for legacy_mode in (None, "", "UNROUTED"):
            state = {"last_tool_ok": True}
            if legacy_mode is not None:
                state["mode"] = legacy_mode
            with self.subTest(mode=legacy_mode):
                self.assertEqual(
                    v3_prototype_module._candidate_cosim_route(state),
                    "promote",
                )

    def test_a1_happy_path_seals_planner_and_synth_evidence_contracts(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-a1-contracts"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-a1-contracts-test",
            )

            self.assertEqual(result["status"], "DONE")
            action_id = result["planner_action_id"]
            input_ref = result["planner_input_ref"]
            output_ref = result["planner_output_ref"]
            self.assertIsInstance(action_id, str)
            self.assertIsInstance(input_ref, str)
            self.assertIsInstance(output_ref, str)

            input_value = json.loads(
                (run_root / input_ref).read_text(encoding="utf-8")
            )
            output_value = json.loads(
                (run_root / output_ref).read_text(encoding="utf-8")
            )
            self.assertEqual(input_value["schema_version"], "v3a.planner-input.v1")
            self.assertEqual(output_value["schema_version"], "v3a.planner-output.v1")
            self.assertEqual(output_value["action_id"], action_id)
            self.assertEqual(
                output_value["input_sha256"], result["planner_input_sha256"]
            )
            self.assertEqual(
                result["planner_input_sha256"],
                v3_prototype_module.canonical_sha256(input_value),
            )
            self.assertEqual(
                result["planner_output_sha256"],
                v3_prototype_module.canonical_sha256(output_value),
            )

            journal_root = run_root / "control" / "planner_actions"
            started_ref = f"control/planner_actions/{action_id}.started.json"
            completed_ref = f"control/planner_actions/{action_id}.completed.json"
            started = json.loads(
                (journal_root / f"{action_id}.started.json").read_text(
                    encoding="utf-8"
                )
            )
            completed = json.loads(
                (journal_root / f"{action_id}.completed.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(started["status"], "STARTED")
            self.assertEqual(completed["status"], "COMPLETED")
            self.assertEqual(started["input_ref"], input_ref)
            self.assertEqual(completed["result_ref"], output_ref)
            self.assertEqual(
                completed["result_sha256"], result["planner_output_sha256"]
            )

            evidence_refs = {
                "candidate_000": result["baseline_synth_evidence_ref"],
                "candidate_001": result["candidate_synth_evidence_ref"],
                "final_candidate_001": result["final_synth_evidence_ref"],
            }
            self.assertEqual(len(set(evidence_refs.values())), 3)
            for expected_candidate_id, reference in evidence_refs.items():
                evidence = json.loads(
                    (run_root / reference).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    evidence["schema_version"], "v3a.synth-evidence.v1"
                )
                self.assertEqual(
                    evidence["candidate_id"],
                    expected_candidate_id.removeprefix("final_"),
                )
                self.assertEqual(
                    evidence["completeness"]["loop_evidence"], "UNAVAILABLE"
                )

            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            candidate = registry["candidates"]["candidate_001"]
            immutable_metadata = json.loads(
                (run_root / "candidates" / "candidate_001" / "candidate.json")
                .read_text(encoding="utf-8")
            )
            planner_bindings = (
                "planner_action_id",
                "planner_input_ref",
                "planner_input_sha256",
                "planner_output_ref",
                "planner_output_sha256",
            )
            for name in planner_bindings:
                self.assertEqual(candidate[name], result[name])
                self.assertEqual(immutable_metadata[name], result[name])

            manifest = json.loads(
                (run_root / "control" / "package_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest_paths = {item["path"] for item in manifest["artifacts"]}
            self.assertTrue(
                {
                    input_ref,
                    output_ref,
                    started_ref,
                    completed_ref,
                    *evidence_refs.values(),
                }.issubset(manifest_paths)
            )

    def test_a1_round_two_planner_input_uses_promoted_incumbent_evidence(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend(candidate_latency=1024)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-a1-round-two-input"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task, credit_limit=100),
                (prototype_proposal(), regression_after_promotion_proposal()),
                backend=backend,
                thread_id="prototype-a1-round-two-input-test",
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            round_two = json.loads(
                (run_root / "planner" / "inputs" / "round_002.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(round_two["round"]["parent_candidate_id"], "candidate_001")
        self.assertEqual(round_two["incumbent"]["candidate_id"], "candidate_001")
        promoted = registry["candidates"]["candidate_001"]
        self.assertEqual(
            round_two["incumbent"]["synth_evidence"],
            {
                "ref": promoted["synth_evidence_ref"],
                "sha256": promoted["synth_evidence_sha256"],
            },
        )
        self.assertEqual(round_two["baseline"]["candidate_id"], "candidate_000")
        self.assertNotEqual(
            round_two["incumbent"]["synth_evidence"],
            round_two["baseline"]["synth_evidence"],
        )
        self.assertEqual(
            registry["candidates"]["candidate_002"]["parent_id"], "candidate_001"
        )

    def test_a1_tampered_planner_output_fails_closed_before_materialization(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-a1-planner-output-tamper"
            with patch.object(
                v3_prototype_module,
                "_materialize_candidate",
                side_effect=RuntimeError("injected before materialization"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before materialization"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-a1-planner-output-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            registry_before_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(registry_before_resume["candidates"]), {"candidate_000"}
            )
            self.assertEqual(
                [kind for kind, _optimized, _work in calls_before_resume],
                ["csim", "synth", "cosim"],
            )

            output_paths = list((run_root / "planner" / "outputs").glob("*.json"))
            self.assertEqual(len(output_paths), 1)
            output = json.loads(output_paths[0].read_text(encoding="utf-8"))
            output["proposal"]["hypothesis"] = "tampered after durable planning"
            output_paths[0].write_text(json.dumps(output), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "Planner output artifact hash mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-a1-planner-output-tamper-test",
                )

            registry_after_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(backend.calls, calls_before_resume)
        self.assertEqual(registry_after_resume, registry_before_resume)

    def test_a1_tampered_planner_input_fails_closed_before_materialization(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-a1-planner-input-tamper"
            with patch.object(
                v3_prototype_module,
                "_materialize_candidate",
                side_effect=RuntimeError("injected before materialization"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before materialization"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-a1-planner-input-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            registry_before_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            candidate_root = run_root / "candidates"
            candidate_dirs_before_resume = (
                sorted(path.name for path in candidate_root.iterdir() if path.is_dir())
                if candidate_root.is_dir()
                else []
            )
            self.assertEqual(
                set(registry_before_resume["candidates"]), {"candidate_000"}
            )

            input_paths = list((run_root / "planner" / "inputs").glob("*.json"))
            self.assertEqual(len(input_paths), 1)
            planner_input = json.loads(input_paths[0].read_text(encoding="utf-8"))
            planner_input["round"]["round_index"] = 999
            input_paths[0].write_text(json.dumps(planner_input), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "Planner input artifact hash mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-a1-planner-input-tamper-test",
                )

            registry_after_resume = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            candidate_dirs_after_resume = (
                sorted(path.name for path in candidate_root.iterdir() if path.is_dir())
                if candidate_root.is_dir()
                else []
            )

        self.assertEqual(backend.calls, calls_before_resume)
        self.assertEqual(registry_after_resume, registry_before_resume)
        self.assertEqual(candidate_dirs_after_resume, candidate_dirs_before_resume)

    def test_a1_missing_planner_journal_fails_closed_before_materialization(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        self.assertIsNotNone(v3_prototype_module)

        for journal_status in ("started", "completed"):
            with self.subTest(journal_status=journal_status):
                backend = PrototypeBackend()
                with tempfile.TemporaryDirectory() as directory:
                    run_root = Path(directory) / f"v3-a1-missing-{journal_status}"
                    with patch.object(
                        v3_prototype_module,
                        "_materialize_candidate",
                        side_effect=RuntimeError("injected before materialization"),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "injected before materialization"
                        ):
                            run_v3_prototype(
                                task,
                                run_root,
                                prototype_config(task),
                                prototype_proposal(),
                                backend=backend,
                                thread_id=(
                                    "prototype-a1-missing-planner-"
                                    f"{journal_status}-test"
                                ),
                            )

                    calls_before_resume = list(backend.calls)
                    registry_before_resume = json.loads(
                        (run_root / "candidate_registry.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    candidate_root = run_root / "candidates"
                    candidate_dirs_before_resume = (
                        sorted(
                            path.name
                            for path in candidate_root.iterdir()
                            if path.is_dir()
                        )
                        if candidate_root.is_dir()
                        else []
                    )
                    output_paths = list(
                        (run_root / "planner" / "outputs").glob("*.json")
                    )
                    self.assertEqual(len(output_paths), 1)
                    action_id = output_paths[0].stem
                    journal_path = (
                        run_root
                        / "control"
                        / "planner_actions"
                        / f"{action_id}.{journal_status}.json"
                    )
                    self.assertTrue(journal_path.is_file())
                    journal_path.unlink()

                    with self.assertRaisesRegex(
                        RuntimeError, "artifact reference is missing"
                    ):
                        run_v3_prototype(
                            task,
                            run_root,
                            prototype_config(task),
                            prototype_proposal(),
                            backend=backend,
                            thread_id=(
                                "prototype-a1-missing-planner-"
                                f"{journal_status}-test"
                            ),
                        )

                    registry_after_resume = json.loads(
                        (run_root / "candidate_registry.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    candidate_dirs_after_resume = (
                        sorted(
                            path.name
                            for path in candidate_root.iterdir()
                            if path.is_dir()
                        )
                        if candidate_root.is_dir()
                        else []
                    )

                self.assertEqual(backend.calls, calls_before_resume)
                self.assertEqual(registry_after_resume, registry_before_resume)
                self.assertEqual(
                    candidate_dirs_after_resume, candidate_dirs_before_resume
                )

    def test_a1_planner_started_without_output_replays_deterministically(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_write = v3_prototype_module._write_once_or_verify
        interrupted = False

        def interrupt_after_started(path, value):
            nonlocal interrupted
            result = original_write(path, value)
            target = Path(path)
            if (
                target.parent.name == "planner_actions"
                and target.name.endswith(".started.json")
                and not interrupted
            ):
                interrupted = True
                raise RuntimeError("injected after Planner STARTED")
            return result

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-a1-started-replay"
            with patch.object(
                v3_prototype_module,
                "_write_once_or_verify",
                side_effect=interrupt_after_started,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected after Planner STARTED"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-a1-started-replay-test",
                    )

            calls_before_resume = list(backend.calls)
            started_before = list(
                (run_root / "control" / "planner_actions").glob(
                    "*.started.json"
                )
            )
            self.assertEqual(len(started_before), 1)
            self.assertEqual(
                list((run_root / "planner" / "outputs").glob("*.json")), []
            )
            self.assertEqual(
                list(
                    (run_root / "control" / "planner_actions").glob(
                        "*.completed.json"
                    )
                ),
                [],
            )
            self.assertEqual(
                [kind for kind, _optimized, _work in calls_before_resume],
                ["csim", "synth", "cosim"],
            )

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-a1-started-replay-test",
            )

            action_id = result["planner_action_id"]
            self.assertTrue(
                (
                    run_root
                    / "control"
                    / "planner_actions"
                    / f"{action_id}.started.json"
                ).is_file()
            )
            self.assertTrue((run_root / result["planner_output_ref"]).is_file())
            self.assertTrue(
                (
                    run_root
                    / "control"
                    / "planner_actions"
                    / f"{action_id}.completed.json"
                ).is_file()
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

    def test_a1_planner_output_without_completed_journal_is_reconciled(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)
        original_write = v3_prototype_module._write_once_or_verify
        interrupted = False

        def interrupt_after_output(path, value):
            nonlocal interrupted
            result = original_write(path, value)
            target = Path(path)
            if (
                target.parent.name == "outputs"
                and target.parent.parent.name == "planner"
                and not interrupted
            ):
                interrupted = True
                raise RuntimeError("injected after Planner output")
            return result

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-a1-output-reconcile"
            with patch.object(
                v3_prototype_module,
                "_write_once_or_verify",
                side_effect=interrupt_after_output,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected after Planner output"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-a1-output-reconcile-test",
                    )

            calls_before_resume = list(backend.calls)
            output_before = list(
                (run_root / "planner" / "outputs").glob("*.json")
            )
            self.assertEqual(len(output_before), 1)
            output_bytes = output_before[0].read_bytes()
            journal_root = run_root / "control" / "planner_actions"
            self.assertEqual(len(list(journal_root.glob("*.started.json"))), 1)
            self.assertEqual(list(journal_root.glob("*.completed.json")), [])
            self.assertEqual(
                [kind for kind, _optimized, _work in calls_before_resume],
                ["csim", "synth", "cosim"],
            )

            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-a1-output-reconcile-test",
            )

            output_after = list(
                (run_root / "planner" / "outputs").glob("*.json")
            )
            self.assertEqual(len(output_after), 1)
            self.assertEqual(output_after[0].read_bytes(), output_bytes)
            action_id = result["planner_action_id"]
            self.assertTrue(
                (journal_root / f"{action_id}.completed.json").is_file()
            )
            self.assertEqual(len(list(journal_root.glob("*.completed.json"))), 1)

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
                    "phase_router",
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
        backend = PrototypeBackend(candidate_latency=1024)

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

    def test_hunk_coordinate_failure_is_precise_and_preserves_registry(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-hunk-coordinate-then-promote"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task, credit_limit=75),
                (wrong_new_start_proposal(), prototype_proposal()),
                backend=backend,
                thread_id="prototype-hunk-coordinate-then-promote-test",
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            rejection = json.loads(
                (
                    run_root
                    / "control"
                    / "proposal_rejections"
                    / "round_001.json"
                ).read_text(encoding="utf-8")
            )
            evidence_ref = rejection["patch_failure_evidence_ref"]
            evidence_path = run_root / evidence_ref
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence_file_sha256 = hashlib.sha256(
                evidence_path.read_bytes()
            ).hexdigest()
            round_two = json.loads(
                (
                    run_root / "planner" / "inputs" / "round_002.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["best_candidate_id"], "candidate_001")
        self.assertEqual(result["final_candidate_id"], "candidate_001")
        self.assertEqual(
            set(registry["candidates"]), {"candidate_000", "candidate_001"}
        )
        self.assertEqual(registry["candidates"]["candidate_000"]["kind"], "baseline")
        self.assertEqual(rejection["reason"], "PATCH_POLICY_REJECTED")
        self.assertEqual(
            rejection["patch_error_type"],
            "PATCH_HUNK_NEW_START_MISMATCH",
        )
        self.assertEqual(
            rejection["patch_failure_evidence_sha256"],
            evidence_file_sha256,
        )
        self.assertEqual(evidence["file"], "kernel.cpp")
        self.assertEqual(
            evidence["schema_version"],
            "v3.patch-hunk-failure-evidence.v1",
        )
        self.assertEqual(evidence["hunk_index"], 2)
        self.assertEqual(evidence["hunk_header"], "@@ -10,5 +10,5 @@")
        self.assertEqual(evidence["declared_new_start"], 10)
        self.assertEqual(evidence["expected_new_start"], 9)
        self.assertEqual(evidence["declared_old_count"], 5)
        self.assertEqual(evidence["actual_old_count"], 5)
        self.assertEqual(evidence["declared_new_count"], 5)
        self.assertEqual(evidence["actual_new_count"], 5)
        self.assertEqual(
            evidence["guidance"],
            "regenerate the unified diff with corrected hunk coordinates",
        )
        self.assertIn("PATCH_HUNK_NEW_START_MISMATCH", evidence["message"])
        self.assertIn("declared_new_start=10", evidence["message"])
        self.assertIn("expected_new_start=9", evidence["message"])
        self.assertEqual(
            round_two["round"]["parent_candidate_id"], "candidate_000"
        )
        self.assertEqual(
            round_two["incumbent"]["candidate_id"], "candidate_000"
        )
        self.assertEqual(
            registry["candidates"]["candidate_001"]["parent_id"],
            "candidate_000",
        )
        proposal_rejections = [
            item
            for item in round_two["history"]
            if item.get("kind") == "proposal_rejection"
        ]
        self.assertEqual(len(proposal_rejections), 1)
        self.assertEqual(
            proposal_rejections[0]["patch_failure_evidence"],
            {
                "ref": evidence_ref,
                "sha256": rejection["patch_failure_evidence_sha256"],
            },
        )
        search_control = round_two["round"]["search_control"]
        self.assertTrue(search_control["failure"]["mechanical_failure"])
        self.assertFalse(search_control["failure"]["terminal_failure"])
        self.assertEqual(
            search_control["recommended_continuation_action"],
            "CONTINUE_WITHOUT_LLM",
        )
        self.assertEqual(
            search_control["semantic_no_improvement_rounds"], 0
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
        backend = PrototypeBackend(candidate_latency=1024)

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
                RuntimeError, "Candidate decision chain does not bind the Registry"
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

    def test_terminal_package_rejects_symlinked_planner_artifact(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-planner-symlink"
            result = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-planner-symlink-test",
            )
            calls_before_resume = list(backend.calls)
            planner_input = run_root / str(result["planner_input_ref"])
            backup = planner_input.with_suffix(".original.json")
            planner_input.rename(backup)
            planner_input.symlink_to(backup.name)

            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-planner-symlink-test",
                )

        self.assertEqual(backend.calls, calls_before_resume)

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

    def test_completed_terminal_is_readable_after_backend_fingerprint_upgrade(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        historical_backend = VersionedPrototypeBackend(
            "v3-prototype-test-backend-v1"
        )

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-backend-fingerprint-upgrade"
            first = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=historical_backend,
                thread_id="prototype-backend-fingerprint-upgrade-test",
            )
            calls_after_first = list(historical_backend.calls)
            credits_after_first = first["budget"]["credits_used"]
            ledger_after_first = (run_root / "budget_ledger.jsonl").read_bytes()
            report_after_first = (run_root / "v3_team_report.md").read_bytes()
            result_after_first = (run_root / "v3_prototype_result.json").read_bytes()

            upgraded_backend = VersionedPrototypeBackend(
                "v3-prototype-test-backend-v2"
            )
            second = run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=upgraded_backend,
                thread_id="prototype-backend-fingerprint-upgrade-test",
            )

            self.assertEqual(second, first)
            self.assertEqual(
                second["backend"]["fingerprint"],
                "v3-prototype-test-backend-v1",
            )
            self.assertEqual(second["budget"]["credits_used"], credits_after_first)
            self.assertEqual(historical_backend.calls, calls_after_first)
            self.assertEqual(upgraded_backend.calls, [])
            self.assertEqual(
                (run_root / "budget_ledger.jsonl").read_bytes(),
                ledger_after_first,
            )
            self.assertEqual(
                (run_root / "v3_team_report.md").read_bytes(),
                report_after_first,
            )
            self.assertEqual(
                (run_root / "v3_prototype_result.json").read_bytes(),
                result_after_first,
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
                Path(directory) / "v3-search-closeout-reserve",
                prototype_config(task, credit_limit=55),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-search-closeout-reserve-test",
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        self.assertEqual(result["budget"]["credits_used"], 50)
        self.assertEqual(
            result["cosim_gate"]["reason"], "ROUND_SKIPPED_SEARCH_CLOSEOUT_RESERVE"
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

    def test_tampered_materialized_source_fails_closed_before_candidate_csim(
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
            run_root = Path(directory) / "v3-materialized-source-tamper"
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
                        thread_id="prototype-materialized-source-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            registry_path = run_root / "candidate_registry.json"
            registry_before_resume = json.loads(
                registry_path.read_text(encoding="utf-8")
            )
            candidate = registry_before_resume["candidates"]["candidate_001"]
            source_path = run_root / str(candidate["source_ref"])
            source_path.chmod(0o600)
            source_path.write_bytes(source_path.read_bytes() + b"\n// tampered\n")

            with self.assertRaises(RuntimeError):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-materialized-source-tamper-test",
                )

            registry_after_resume = json.loads(
                registry_path.read_text(encoding="utf-8")
            )

        self.assertEqual(
            [kind for kind, _optimized, _work in calls_before_resume],
            ["csim", "synth", "cosim"],
        )
        self.assertEqual(backend.calls, calls_before_resume)
        self.assertEqual(registry_after_resume, registry_before_resume)

    def test_round_two_legacy_projection_tamper_cannot_rebind_completed_journal(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend(candidate_latency=1024)
        self.assertIsNotNone(v3_prototype_module)
        original_materialize = v3_prototype_module._materialize_candidate

        def interrupt_round_two_materialization(runtime, state):
            if int(state.get("round_index", 1)) == 2:
                raise RuntimeError("injected before round-two materialization")
            return original_materialize(runtime, state)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-round-two-legacy-tamper"
            with patch.object(
                v3_prototype_module,
                "_materialize_candidate",
                side_effect=interrupt_round_two_materialization,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before round-two materialization"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task, credit_limit=100),
                        (
                            prototype_proposal(),
                            regression_after_promotion_proposal(),
                        ),
                        backend=backend,
                        thread_id="prototype-round-two-legacy-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            registry_path = run_root / "candidate_registry.json"
            registry_before_resume = json.loads(
                registry_path.read_text(encoding="utf-8")
            )
            legacy_path = run_root / "planner" / "proposal_002.json"
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            legacy["parent_candidate_id"] = "candidate_000"
            legacy["round_index"] = 999
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

            completed_paths = list(
                (run_root / "control" / "planner_actions").glob(
                    "*.completed.json"
                )
            )
            round_two_completed = None
            for completed_path in completed_paths:
                completed = json.loads(completed_path.read_text(encoding="utf-8"))
                if completed.get("legacy_projection_ref") == (
                    "planner/proposal_002.json"
                ):
                    round_two_completed = completed_path
                    completed["legacy_projection_sha256"] = (
                        v3_prototype_module.canonical_sha256(legacy)
                    )
                    completed_path.write_text(
                        json.dumps(completed), encoding="utf-8"
                    )
                    break
            self.assertIsNotNone(round_two_completed)

            with self.assertRaisesRegex(
                RuntimeError,
                "legacy Planner projection diverges from versioned output",
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task, credit_limit=100),
                    (
                        prototype_proposal(),
                        regression_after_promotion_proposal(),
                    ),
                    backend=backend,
                    thread_id="prototype-round-two-legacy-tamper-test",
                )

            registry_after_resume = json.loads(
                registry_path.read_text(encoding="utf-8")
            )

        self.assertEqual(
            [kind for kind, _optimized, _work in calls_before_resume],
            ["csim", "synth", "cosim", "csim", "synth", "cosim"],
        )
        self.assertEqual(backend.calls, calls_before_resume)
        self.assertEqual(registry_after_resume, registry_before_resume)

    def test_tampered_final_synth_result_fails_before_final_cosim(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-final-synth-result-tamper"
            with patch.object(
                v3_prototype_module,
                "_final_cosim",
                side_effect=RuntimeError("injected before final cosim"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before final cosim"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-final-synth-result-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            final_synth_paths: list[Path] = []
            for result_path in (run_root / "actions").glob("*/result.json"):
                value = json.loads(result_path.read_text(encoding="utf-8"))
                if (
                    value.get("kind") == "synth"
                    and value.get("validation_scope") == "search_closeout"
                ):
                    final_synth_paths.append(result_path)
            self.assertEqual(len(final_synth_paths), 1)
            final_synth = json.loads(
                final_synth_paths[0].read_text(encoding="utf-8")
            )
            final_synth["report"]["latency"]["best"] = 1
            final_synth_paths[0].write_text(
                json.dumps(final_synth), encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "result digest mismatch"):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-final-synth-result-tamper-test",
                )

        self.assertEqual(
            [kind for kind, _optimized, _work in calls_before_resume],
            [
                "csim",
                "synth",
                "cosim",
                "csim",
                "synth",
                "cosim",
                "csim",
                "synth",
            ],
        )
        self.assertEqual(backend.calls, calls_before_resume)

    def test_a1_terminal_cannot_downgrade_by_removing_package_metadata(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-terminal-package-downgrade"
            run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-terminal-package-downgrade-test",
            )
            calls_before_reentry = list(backend.calls)
            result_path = run_root / "v3_prototype_result.json"
            stored = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(stored.pop("package", None))
            stored["final_candidate_id"] = "candidate_000"
            result_path.write_text(json.dumps(stored), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "terminal V3 sealed package is required"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-terminal-package-downgrade-test",
                )

        self.assertEqual(backend.calls, calls_before_reentry)

    def test_tampered_final_synth_evidence_fails_before_package_commit(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-final-evidence-tamper"
            with patch.object(
                v3_prototype_module,
                "_write_report",
                side_effect=RuntimeError("injected before package commit"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before package commit"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-final-evidence-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            final_evidence_paths: list[Path] = []
            for evidence_path in (run_root / "evidence" / "synth").glob(
                "*.json"
            ):
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                result = json.loads(
                    (run_root / str(evidence["result_ref"])).read_text(
                        encoding="utf-8"
                    )
                )
                if result.get("validation_scope") == "search_closeout":
                    final_evidence_paths.append(evidence_path)
            self.assertEqual(len(final_evidence_paths), 1)
            evidence = json.loads(
                final_evidence_paths[0].read_text(encoding="utf-8")
            )
            evidence["top_level"]["latency"]["best"] = 1
            final_evidence_paths[0].write_text(
                json.dumps(evidence), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                RuntimeError, "Synth evidence artifact hash mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-final-evidence-tamper-test",
                )

        self.assertEqual(backend.calls, calls_before_resume)

    def test_registry_synth_evidence_digest_must_match_artifact(self) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-registry-evidence-digest"
            run_v3_prototype(
                task,
                run_root,
                prototype_config(task),
                prototype_proposal(),
                backend=backend,
                thread_id="prototype-registry-evidence-digest-test",
            )
            calls_before_reentry = list(backend.calls)
            registry_path = run_root / "candidate_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["candidates"]["candidate_001"][
                "synth_evidence_sha256"
            ] = "0" * 64
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "Synth evidence artifact hash mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-registry-evidence-digest-test",
                )

        self.assertEqual(backend.calls, calls_before_reentry)

    def test_tampered_final_score_is_recomputed_before_package_commit(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-final-score-tamper"
            with patch.object(
                v3_prototype_module,
                "_write_report",
                side_effect=RuntimeError("injected before package commit"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before package commit"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-final-score-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            score_path = run_root / "scores" / "candidate_001.final.json"
            score = json.loads(score_path.read_text(encoding="utf-8"))
            score["tokens_used"] = 999999
            score["forged_extra"] = "must not survive semantic sealing"
            score_path.write_text(json.dumps(score), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "score artifact semantic mismatch"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-final-score-tamper-test",
                )

        self.assertEqual(backend.calls, calls_before_resume)

    def test_tampered_candidate_decision_commit_fails_before_package_commit(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        task = load_public_task(project / "examples" / "u55c_v2_optimize_task")
        backend = PrototypeBackend()
        self.assertIsNotNone(v3_prototype_module)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "v3-candidate-decision-tamper"
            with patch.object(
                v3_prototype_module,
                "_write_report",
                side_effect=RuntimeError("injected before package commit"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected before package commit"
                ):
                    run_v3_prototype(
                        task,
                        run_root,
                        prototype_config(task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="prototype-candidate-decision-tamper-test",
                    )

            calls_before_resume = list(backend.calls)
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            operation_id = registry["v3_last_operation_id"]
            committed_path = (
                run_root
                / "control"
                / "candidate_operations"
                / f"{operation_id}.committed.json"
            )
            committed = json.loads(committed_path.read_text(encoding="utf-8"))
            committed["request"]["reason"] = "tampered decision reason"
            committed_path.write_text(json.dumps(committed), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "Candidate decision committed record is malformed"
            ):
                run_v3_prototype(
                    task,
                    run_root,
                    prototype_config(task),
                    prototype_proposal(),
                    backend=backend,
                    thread_id="prototype-candidate-decision-tamper-test",
                )

        self.assertEqual(backend.calls, calls_before_resume)


if __name__ == "__main__":
    unittest.main()
