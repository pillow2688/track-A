from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.repair import PatchProposal
from llm4hls_agent.task import PublicTask, load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.v3_failure_evidence import (
    COSIM_FAILURE_EVIDENCE_SCHEMA,
    CSIM_FAILURE_EVIDENCE_SCHEMA,
)
from llm4hls_agent.workflow import RunConfig

try:
    from llm4hls_agent.v3_prototype import run_v3_prototype
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("langgraph"):
        run_v3_prototype = None  # type: ignore[assignment]
    else:
        raise


OFFICIAL_TASKS = (
    Path(__file__).resolve().parents[1]
    / "task_corpus"
    / "official"
    / "fpt26-harness-public"
)


class TaskAwareSmokeBackend:
    """Deterministic baseline failures and repaired outcomes for graph smoke tests."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.calls: list[tuple[str, bool]] = []

    def fingerprint(self) -> str:
        return f"v3-task-aware-smoke-{self.task_id}-v1"

    def _is_candidate(self, kernel_bytes: bytes) -> bool:
        markers = {
            "projection_bugfix": b"triangle_3d.z2 / 3;",
            "dotProduct_optimize": b"#pragma HLS PIPELINE II=1",
            "residual_stream_deadlock": (
                b"s_main.write(in[i]);\n        s_skip.write(in[i]);"
            ),
        }
        return markers[self.task_id] in kernel_bytes

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        work_dir: Path,
        **_kwargs: object,
    ) -> BackendResult:
        del work_dir
        candidate = self._is_candidate(kernel_bytes)
        self.calls.append((kind, candidate))

        if (
            self.task_id == "projection_bugfix"
            and kind == "csim"
            and not candidate
        ):
            return BackendResult(
                False,
                "runtime_fail",
                1,
                0.01,
                evidence=[
                    "projection.cpp:17: public CSim mismatch",
                    "expected z=60, actual z=30 for angle=0",
                ],
            )

        if (
            self.task_id == "residual_stream_deadlock"
            and kind == "cosim"
            and not candidate
        ):
            return BackendResult(
                False,
                "cosim_fail",
                1,
                0.01,
                evidence=[
                    "residual.cpp:10: RTL deadlock while draining DATAFLOW region",
                    "hls::stream s_main/s_skip FIFO depth=2 timed out",
                ],
                cosim={"status": "Fail", "reason": "deadlock"},
            )

        if kind == "synth":
            latency = (
                64
                if self.task_id == "dotProduct_optimize" and candidate
                else 1027
                if self.task_id == "dotProduct_optimize"
                else 32
            )
            return BackendResult(
                True,
                "pass",
                0,
                0.01,
                evidence=["deterministic task-aware smoke synthesis"],
                report={
                    "estimated_clock_period_ns": 5.0,
                    "latency": {
                        "best": latency,
                        "average": latency,
                        "worst": latency,
                    },
                    "interval": {"min": latency, "max": latency},
                    "resources": {
                        "LUT": 128,
                        "FF": 256,
                        "DSP": 2,
                        "BRAM_18K": 0,
                        "URAM": 0,
                    },
                    "available_resources": {
                        "LUT": 100_000,
                        "FF": 200_000,
                        "DSP": 1_000,
                        "BRAM_18K": 1_000,
                        "URAM": 100,
                    },
                },
            )

        if kind == "cosim":
            return BackendResult(
                True,
                "pass",
                0,
                0.01,
                cosim={"status": "Pass"},
            )

        return BackendResult(True, "pass", 0, 0.01)


class ResourceViolationSmokeBackend(TaskAwareSmokeBackend):
    def __init__(self, task_id: str, *, violation_on_synth_call: int) -> None:
        super().__init__(task_id)
        self.violation_on_synth_call = violation_on_synth_call
        self.synth_calls = 0

    def fingerprint(self) -> str:
        return (
            f"v3-task-aware-resource-smoke-{self.task_id}-"
            f"{self.violation_on_synth_call}-v1"
        )

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        work_dir: Path,
        **kwargs: object,
    ) -> BackendResult:
        result = super().run(
            kind,
            kernel_bytes=kernel_bytes,
            work_dir=work_dir,
            **kwargs,
        )
        if kind != "synth":
            return result
        self.synth_calls += 1
        if self.synth_calls != self.violation_on_synth_call:
            return result
        report = dict(result.report or {})
        resources = dict(report.get("resources", {}))
        resources["LUT"] = 200_000
        report["resources"] = resources
        return replace(result, report=report)


class CombinationalRepairSmokeBackend(TaskAwareSmokeBackend):
    """Model the zero-cycle latency Vitis reports for combinational kernels."""

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        work_dir: Path,
        **kwargs: object,
    ) -> BackendResult:
        result = super().run(
            kind,
            kernel_bytes=kernel_bytes,
            work_dir=work_dir,
            **kwargs,
        )
        if kind != "synth":
            return result
        report = dict(result.report or {})
        report["latency"] = {"best": 0, "average": 0, "worst": 0}
        report["interval"] = {"min": 0, "max": 0}
        return replace(result, report=report)


class MissingLatencyRepairSmokeBackend(TaskAwareSmokeBackend):
    """Model valid correctness synthesis reports with no PPA latency table."""

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        work_dir: Path,
        **kwargs: object,
    ) -> BackendResult:
        result = super().run(
            kind,
            kernel_bytes=kernel_bytes,
            work_dir=work_dir,
            **kwargs,
        )
        if kind != "synth":
            return result
        report = dict(result.report or {})
        report.pop("latency", None)
        report.pop("interval", None)
        return replace(result, report=report)


def task_config(task: PublicTask) -> RunConfig:
    # The official projection task advertises 20 credits, while the unchanged
    # mandatory fresh final closure alone costs 25.  These graph smoke tests
    # deliberately provide enough credit to exercise the complete route.
    return RunConfig(
        tool=ToolConfig(
            vitis_root="/opt/xilinx/2025.2/Vitis",
            part=task.part,
            clock_ns=task.clock_ns,
            timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
        ),
        budget=BudgetConfig(
            credit_limit=100,
            costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
            tool_limits={"csim": 6, "synth": 6, "cosim": 4, "llm": 2},
            token_limit=4096,
            runtime_limit_seconds=300.0,
        ),
        minimum_frequency_mhz=100.0,
    )


def proposal_for(task_id: str) -> PatchProposal:
    if task_id == "projection_bugfix":
        return PatchProposal(
            patch=(
                "--- a/projection.cpp\n"
                "+++ b/projection.cpp\n"
                "@@ -14,5 +14,5 @@\n"
                "         triangle_2d->x2 = triangle_3d.x2;\n"
                "         triangle_2d->y2 = triangle_3d.y2;\n"
                "         // BUG: drops the third vertex term; should be z0/3 + z1/3 + z2/3\n"
                "-        triangle_2d->z = triangle_3d.z0 / 3 + triangle_3d.z1 / 3;\n"
                "+        triangle_2d->z = triangle_3d.z0 / 3 + triangle_3d.z1 / 3 + triangle_3d.z2 / 3;\n"
                "     }\n"
            ),
            provider="scripted-task-aware-smoke",
            model="fixture-v1",
            hypothesis="The angle-zero branch omits the third z coordinate.",
            change_class="FUNCTIONAL_REPAIR",
            expected_effect="Restore the public functional invariant.",
            risk=json.dumps({"level": "LOW", "dimensions": ["arithmetic"]}),
            required_validation=("csim", "synth"),
        )
    if task_id == "residual_stream_deadlock":
        return PatchProposal(
            patch=(
                "--- a/residual.cpp\n"
                "+++ b/residual.cpp\n"
                "@@ -7,6 +7,8 @@\n"
                "     // skip stream. Unbounded C-sim FIFOs tolerate this, but bounded RTL FIFOs\n"
                "     // (depth 2) cannot buffer the burst -> the dataflow region deadlocks in\n"
                "     // co-simulation.\n"
                "-    for (int i = 0; i < N; i++) s_main.write(in[i]);\n"
                "-    for (int i = 0; i < N; i++) s_skip.write(in[i]);\n"
                "+    for (int i = 0; i < N; i++) {\n"
                "+        s_main.write(in[i]);\n"
                "+        s_skip.write(in[i]);\n"
                "+    }\n"
                " }\n"
            ),
            provider="scripted-task-aware-smoke",
            model="fixture-v1",
            hypothesis="The producer bursts one FIFO before servicing the skip FIFO.",
            change_class="STRUCTURAL_REPAIR",
            expected_effect="Interleave both stream writes and remove RTL backpressure deadlock.",
            risk=json.dumps(
                {"level": "HIGH", "dimensions": ["dataflow", "stream", "fifo"]}
            ),
            required_validation=("csim", "cosim"),
        )
    if task_id == "dotProduct_optimize":
        return PatchProposal(
            patch=(
                "--- a/dotProduct.cpp\n"
                "+++ b/dotProduct.cpp\n"
                "@@ -5,6 +5,7 @@\n"
                " dotProduct(FeatureType param[NUM_FEATURES], DataType feature[NUM_FEATURES]) {\n"
                "     FeatureType result = 0;\n"
                "     for (int i = 0; i < NUM_FEATURES; i++) {\n"
                "+#pragma HLS PIPELINE II=1\n"
                "         result += param[i] * feature[i];\n"
                "     }\n"
                "     return result;\n"
            ),
            provider="scripted-task-aware-smoke",
            model="fixture-v1",
            hypothesis="Pipeline the accumulation loop for the deterministic fixture.",
            change_class="LOOP_PIPELINE",
            expected_effect="Reduce fixture latency from 1027 to 64 cycles.",
            risk=json.dumps({"level": "LOW", "dimensions": []}),
            required_validation=("csim", "synth"),
        )
    raise AssertionError(f"unsupported fixture task: {task_id}")


def load_official(task_id: str) -> PublicTask:
    return load_public_task(OFFICIAL_TASKS / task_id)


def _assert_fresh_final_closure(test: unittest.TestCase, result: dict[str, object]) -> None:
    final_validation = result["final_validation"]
    test.assertIsInstance(final_validation, dict)
    for stage in ("csim", "synth", "cosim"):
        record = final_validation[stage]  # type: ignore[index]
        test.assertEqual(record["status"], "PASS")
        test.assertEqual(record["validation_scope"], "final")


@unittest.skipIf(run_v3_prototype is None, "V3 optional dependencies are not installed")
class V3TaskAwareOfficialSmokeTests(unittest.TestCase):
    def _run(
        self,
        task_id: str,
        proposal: PatchProposal | None = None,
        *,
        backend: TaskAwareSmokeBackend | None = None,
    ) -> tuple[
        dict[str, object], dict[str, object], dict[str, object], list[str]
    ]:
        task = load_official(task_id)
        backend = backend or TaskAwareSmokeBackend(task_id)
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / task_id
            result = run_v3_prototype(
                task,
                run_root,
                task_config(task),
                proposal or proposal_for(task_id),
                backend=backend,
                validation_profile="fast-experiment",
                max_no_improvement_rounds=2,
                thread_id=f"v3-task-aware-{task_id}",
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )
            planner_input = json.loads(
                (run_root / str(result["planner_input_ref"])).read_text(
                    encoding="utf-8"
                )
            )

            failure_ref = result.get("failure_evidence_ref")
            if isinstance(failure_ref, str) and failure_ref:
                failure_evidence = json.loads(
                    (run_root / failure_ref).read_text(encoding="utf-8")
                )
                self.assertLess(len(json.dumps(failure_evidence)), 5000)

            _assert_fresh_final_closure(self, result)
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["final_candidate_id"], "candidate_001")
            self.assertEqual(
                planner_input["round"]["mode"],
                result["mode"],
            )
            return (
                result,
                registry,
                planner_input,
                [kind for kind, _candidate in backend.calls],
            )

    def test_projection_bugfix_routes_to_repair_and_closes_fresh(self) -> None:
        result, registry, planner_input, calls = self._run("projection_bugfix")

        self.assertEqual(result["mode"], "REPAIR")
        self.assertEqual(result["phase_decision"]["mode"], "REPAIR")
        self.assertEqual(
            result["phase_decision"]["reason"], "BASELINE_CSIM_FAILED"
        )
        self.assertEqual(registry["candidates"]["candidate_001"]["kind"], "repair")
        self.assertTrue(result["failure_evidence_ref"])
        self.assertEqual(
            planner_input["round"]["failure_evidence"]["schema_version"],
            CSIM_FAILURE_EVIDENCE_SCHEMA,
        )
        self.assertEqual(
            calls,
            ["csim", "csim", "synth", "csim", "synth", "cosim"],
        )
        self.assertEqual(
            result["budget"]["tool_used"],
            {"csim": 3, "synth": 2, "cosim": 1, "llm": 0},
        )

    def test_projection_repair_relocates_unique_model_hunk_start(self) -> None:
        proposal = proposal_for("projection_bugfix")
        misplaced = replace(
            proposal,
            patch=proposal.patch.replace(
                "@@ -14,5 +14,5 @@", "@@ -12,5 +12,5 @@"
            ),
        )

        result, registry, _planner_input, _calls = self._run(
            "projection_bugfix", misplaced
        )

        candidate = registry["candidates"]["candidate_001"]
        self.assertEqual(result["status"], "DONE")
        self.assertTrue(candidate["patch_metadata_normalized"])
        materialized = next(
            event
            for event in result["node_events"]
            if event["node"] == "materialize_candidate"
        )
        self.assertTrue(materialized["details"]["patch_metadata_normalized"])

    def test_projection_combinational_zero_latency_still_writes_report(self) -> None:
        result, _registry, _planner_input, _calls = self._run(
            "projection_bugfix",
            backend=CombinationalRepairSmokeBackend("projection_bugfix"),
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["candidate_rounds"][0]["latency_worst"], 0.0)

    def test_projection_missing_latency_keeps_fresh_final_pass_authoritative(self) -> None:
        result, _registry, _planner_input, _calls = self._run(
            "projection_bugfix",
            backend=MissingLatencyRepairSmokeBackend("projection_bugfix"),
        )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["stop_reason"], "REPAIR_FINALIZED")
        self.assertIsNone(result["candidate_rounds"][0]["latency_worst"])
        self.assertIsNone(
            result["candidate_rounds"][0]["acceleration_vs_baseline"]
        )

    def test_dot_product_keeps_existing_optimize_route(self) -> None:
        result, registry, planner_input, calls = self._run("dotProduct_optimize")

        self.assertEqual(result["mode"], "OPTIMIZE")
        self.assertEqual(result["phase_decision"]["mode"], "OPTIMIZE")
        self.assertEqual(planner_input["round"]["failure_evidence"], {})
        self.assertEqual(
            registry["candidates"]["candidate_001"]["kind"], "optimization"
        )
        self.assertEqual(
            calls,
            ["csim", "synth", "csim", "synth", "csim", "synth", "cosim"],
        )
        self.assertEqual(result["candidate_rounds"][0]["latency_worst"], 64.0)
        self.assertEqual(result["budget"]["credits_used"], 35)

    def test_residual_deadlock_routes_to_structural_fix(self) -> None:
        result, registry, planner_input, calls = self._run(
            "residual_stream_deadlock"
        )

        self.assertEqual(result["mode"], "STRUCTURAL_FIX")
        self.assertEqual(
            result["phase_decision"]["mode"], "STRUCTURAL_FIX"
        )
        self.assertEqual(
            result["phase_decision"]["reason"],
            "REQUIRED_BASELINE_COSIM_FAILED",
        )
        self.assertEqual(
            registry["candidates"]["candidate_001"]["kind"], "structural_fix"
        )
        self.assertTrue(result["failure_evidence_ref"])
        self.assertEqual(
            planner_input["round"]["failure_evidence"]["schema_version"],
            COSIM_FAILURE_EVIDENCE_SCHEMA,
        )
        self.assertEqual(
            calls,
            [
                "csim",
                "synth",
                "cosim",
                "csim",
                "cosim",
                "csim",
                "synth",
                "cosim",
            ],
        )
        self.assertEqual(result["budget"]["credits_used"], 71)

    def test_synth_pass_with_resource_violation_routes_to_synth_fix(self) -> None:
        task = load_official("dotProduct_optimize")
        backend = ResourceViolationSmokeBackend(
            task.id, violation_on_synth_call=1
        )
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "synth_fix"
            result = run_v3_prototype(
                task,
                run_root,
                task_config(task),
                proposal_for(task.id),
                backend=backend,
                validation_profile="fast-experiment",
                thread_id="v3-task-aware-synth-fix",
            )
            planner_input = json.loads(
                (run_root / str(result["planner_input_ref"])).read_text(
                    encoding="utf-8"
                )
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["mode"], "SYNTH_FIX")
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(
            registry["candidates"]["candidate_001"]["kind"], "synth_fix"
        )
        failure = planner_input["round"]["failure_evidence"]
        self.assertEqual(failure["failure_kind"], "CONSTRAINT_VIOLATION")
        self.assertTrue(failure["resource_violations"])

    def test_repair_final_resource_violation_fails_closed(self) -> None:
        task = load_official("projection_bugfix")
        backend = ResourceViolationSmokeBackend(
            task.id, violation_on_synth_call=2
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_v3_prototype(
                task,
                Path(directory) / "final_resource_failure",
                task_config(task),
                proposal_for(task.id),
                backend=backend,
                validation_profile="fast-experiment",
                thread_id="v3-task-aware-final-resource-failure",
            )

        self.assertEqual(result["mode"], "REPAIR")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(
            result["stop_reason"], "FINAL_CORRECTNESS_CONSTRAINT_FAILED"
        )
        self.assertIsNone(result["final_candidate_id"])


if __name__ == "__main__":
    unittest.main()
