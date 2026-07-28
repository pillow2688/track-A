from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult
from llm4hls_agent.v3_prototype import (
    FAST_EXPERIMENT_PROFILE,
    TASK_CONTRACT_FINAL_POLICY,
    _latency_observation,
    _terminal_candidate_binding,
    run_v3_prototype,
)
import llm4hls_agent.v3_prototype as v3_prototype_module

from .test_v3_prototype import (
    PrototypeBackend,
    prototype_config,
    prototype_proposal,
)


class StructuralFailureBackend(PrototypeBackend):
    """Route through STRUCTURAL_FIX and keep every CoSim fact negative."""

    def run(
        self,
        kind: str,
        *,
        kernel_bytes: bytes,
        work_dir: Path,
        **kwargs: object,
    ) -> BackendResult:
        if kind == "cosim":
            self.calls.append((kind, False, work_dir.name))
            return BackendResult(
                False,
                "cosim_fail",
                1,
                0.01,
                evidence=["deterministic structural failure"],
                cosim={"status": "Fail"},
            )
        return super().run(
            kind,
            kernel_bytes=kernel_bytes,
            work_dir=work_dir,
            **kwargs,
        )


class LatencyObservationTests(unittest.TestCase):
    def test_absent_latency_is_not_reported(self) -> None:
        observed = _latency_observation({})
        self.assertEqual(observed.status, "NOT_REPORTED")
        self.assertIsNone(observed.value)

    def test_null_latency_is_missing(self) -> None:
        observed = _latency_observation({"latency": None})
        self.assertEqual(observed.status, "MISSING")

    def test_missing_worst_latency_is_missing(self) -> None:
        observed = _latency_observation({"latency": {"best": 1}})
        self.assertEqual(observed.status, "MISSING")

    def test_string_worst_latency_is_invalid(self) -> None:
        observed = _latency_observation({"latency": {"worst": "1"}})
        self.assertEqual(observed.status, "INVALID")
        self.assertIsNone(observed.value)

    def test_nan_worst_latency_is_invalid_and_json_safe(self) -> None:
        observed = _latency_observation({"latency": {"worst": float("nan")}})
        self.assertEqual(observed.status, "INVALID")
        self.assertIsNone(observed.to_dict()["value"])

    def test_infinite_worst_latency_is_invalid(self) -> None:
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(
                    _latency_observation(
                        {"latency": {"worst": value}}
                    ).status,
                    "INVALID",
                )

    def test_negative_worst_latency_is_invalid(self) -> None:
        observed = _latency_observation({"latency": {"worst": -1}})
        self.assertEqual(observed.status, "INVALID")

    def test_zero_worst_latency_is_invalid_for_optimize(self) -> None:
        observed = _latency_observation({"latency": {"worst": 0}})
        self.assertEqual(observed.status, "INVALID")
        self.assertIn("WORST_LATENCY_ZERO", observed.reason_codes)

    def test_zero_worst_latency_can_be_reported_for_correctness_mode(self) -> None:
        observed = _latency_observation(
            {"latency": {"worst": 0}}, allow_zero=True
        )
        self.assertEqual(observed.status, "VALID")
        self.assertEqual(observed.value, 0.0)

    def test_positive_finite_worst_latency_is_valid(self) -> None:
        observed = _latency_observation({"latency": {"worst": 12}})
        self.assertEqual(observed.status, "VALID")
        self.assertEqual(observed.value, 12.0)


class TerminalLatencyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        project = Path(__file__).resolve().parents[1]
        cls.task = load_public_task(
            project / "examples" / "u55c_v2_optimize_task"
        )

    def _run_invalid_latency(self, run_root: Path) -> dict[str, object]:
        return run_v3_prototype(
            self.task,
            run_root,
            prototype_config(self.task),
            prototype_proposal(),
            backend=PrototypeBackend(candidate_latency=0),
            thread_id="terminal-latency-invalid",
            validation_profile=FAST_EXPERIMENT_PROFILE,
            final_validation_policy=TASK_CONTRACT_FINAL_POLICY,
        )

    def test_invalid_latency_preserves_correctness_and_returns_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "invalid-latency"
            result = self._run_invalid_latency(run_root)
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
        candidate = registry["candidates"]["candidate_001"]
        self.assertEqual(candidate["validation"]["csim"]["status"], "PASS")
        self.assertEqual(candidate["validation"]["synth"]["status"], "PASS")
        self.assertEqual(candidate["status"], "REJECTED")
        self.assertEqual(candidate["rejection_reason"], "LATENCY_NOT_COMPARABLE")
        self.assertEqual(result["status"], "DONE")

    def test_invalid_latency_keeps_incumbent_and_never_calculates_acceleration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_invalid_latency(Path(directory) / "incumbent")
        self.assertEqual(result["final_candidate_id"], "candidate_000")
        row = result["candidate_rounds"][0]
        self.assertEqual(row["performance_comparability"], "NOT_COMPARABLE")
        self.assertEqual(row["latency_status"], "INVALID")
        self.assertIsNone(row["latency_worst"])
        self.assertIsNone(row["acceleration_vs_baseline"])

    def test_selected_final_candidate_has_durable_terminal_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "selected-final"
            result = run_v3_prototype(
                self.task,
                run_root,
                prototype_config(self.task),
                prototype_proposal(),
                backend=PrototypeBackend(),
                thread_id="terminal-binding-selected",
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
        binding = result["terminal_candidate_binding"]
        self.assertEqual(binding["candidate_id"], result["final_candidate_id"])
        self.assertEqual(
            binding["candidate_id"], registry["final_candidate_id"]
        )
        self.assertEqual(binding["promotion_status"], "FINAL_VERIFIED")
        self.assertEqual(binding["binding_source"], "FINAL_VERIFIED_CANDIDATE")

    def test_rejected_last_candidate_reconciles_to_preserved_incumbent(
        self,
    ) -> None:
        backend = StructuralFailureBackend()
        proposals = [prototype_proposal(), prototype_proposal()]
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "structural-reconcile"
            result = run_v3_prototype(
                self.task,
                run_root,
                prototype_config(self.task, credit_limit=100),
                proposals,
                backend=backend,
                thread_id="terminal-binding-reconcile",
                max_planner_rounds=2,
                validation_profile=FAST_EXPERIMENT_PROFILE,
                final_validation_policy=TASK_CONTRACT_FINAL_POLICY,
            )
            registry = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            trace = [
                json.loads(line)
                for line in (run_root / "trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "MAX_TASK_REPAIR_ROUNDS")
        self.assertIsNone(result["final_candidate_id"])
        self.assertEqual(
            result["terminal_candidate_binding"]["candidate_id"],
            registry["best_candidate_id"],
        )
        self.assertTrue(result["terminal_reconciliation"]["reconciled"])
        self.assertEqual(
            result["decision_ref"],
            result["terminal_candidate_binding"]["candidate_decision_ref"],
        )
        self.assertEqual(trace[-1]["node"], "write_report")

    def test_report_interruption_resume_preserves_candidate_identity(
        self,
    ) -> None:
        backend = PrototypeBackend()
        original_atomic_json = v3_prototype_module._atomic_json
        interrupted = False

        def interrupt_terminal_marker(path: Path, value: object) -> None:
            nonlocal interrupted
            if Path(path).name == "v3_prototype_result.json" and not interrupted:
                interrupted = True
                raise RuntimeError("injected terminal marker interruption")
            original_atomic_json(path, value)

        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "resume-binding"
            with patch.object(
                v3_prototype_module,
                "_atomic_json",
                side_effect=interrupt_terminal_marker,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "terminal marker interruption"
                ):
                    run_v3_prototype(
                        self.task,
                        run_root,
                        prototype_config(self.task),
                        prototype_proposal(),
                        backend=backend,
                        thread_id="terminal-binding-resume",
                    )
            before = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            calls_before_resume = list(backend.calls)
            result = run_v3_prototype(
                self.task,
                run_root,
                prototype_config(self.task),
                prototype_proposal(),
                backend=backend,
                thread_id="terminal-binding-resume",
            )
            after = json.loads(
                (run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(before["final_candidate_id"], after["final_candidate_id"])
        self.assertEqual(
            result["terminal_candidate_binding"]["candidate_id"],
            after["final_candidate_id"],
        )
        self.assertEqual(backend.calls, calls_before_resume)

    def test_no_legal_candidate_returns_explicit_unknown_without_fabrication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(
                run_root=Path(directory),
                task=self.task,
                final_validation_policy=TASK_CONTRACT_FINAL_POLICY,
            )
            binding = _terminal_candidate_binding(
                runtime,
                {},
                {
                    "v3_revision": 0,
                    "v3_last_operation_id": None,
                    "candidates": {},
                    "baseline_candidate_id": None,
                    "best_candidate_id": None,
                    "final_candidate_id": None,
                },
            )
        self.assertIsNone(binding.candidate_id)
        self.assertEqual(binding.binding_source, "NO_LEGAL_CANDIDATE")
        self.assertEqual(binding.selection_reason, "NO_LEGAL_CANDIDATE")

    def test_terminal_binding_digest_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_invalid_latency(Path(directory) / "digest")
        binding = dict(result["terminal_candidate_binding"])
        digest = binding.pop("binding_sha256")
        self.assertEqual(
            digest, v3_prototype_module._sha256_json(binding)
        )

    def test_product_code_has_no_target_task_hardcoding(self) -> None:
        source = Path(v3_prototype_module.__file__).read_text(encoding="utf-8")
        for task_id in (
            "v3d_fast_016",
            "v3d_fast_020",
            "v3d_fast_021",
            "v3d_fast_022",
        ):
            self.assertNotIn(task_id, source)


if __name__ == "__main__":
    unittest.main()
