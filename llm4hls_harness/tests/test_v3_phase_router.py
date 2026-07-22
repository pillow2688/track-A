from __future__ import annotations

import unittest
from types import SimpleNamespace

from llm4hls_agent.tools import ToolResult
from llm4hls_agent.v3_phase_router import (
    PhaseMode,
    PhaseRouter,
    PhaseRoutingError,
    route_phase,
)


def result(*, kind: str, ok: bool, phase: str) -> ToolResult:
    return ToolResult(
        kind=kind,
        ok=ok,
        phase=phase,
        return_code=0 if ok else 1,
        elapsed_s=0.1,
        effective_timeout_seconds=30.0,
        action_id=f"{kind}-action",
        candidate_id="candidate_000",
        code_hash="a" * 64,
        tool_config_hash="b" * 64,
        backend_fingerprint="fixture-backend",
        task_fingerprint="c" * 64,
        result_ref=f"actions/{kind}-action/result.json",
        cached=False,
        evidence=[],
        artifacts={},
        artifact_hashes={},
    )


class V3PhaseRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = PhaseRouter()

    def test_projection_bugfix_csim_failure_routes_to_repair(self) -> None:
        decision = self.router.route(
            baseline_csim={"status": "FAIL", "phase": "csim", "ok": False},
            baseline_synth={"status": "NOT_RUN"},
            baseline_cosim={"status": "NOT_RUN"},
            task_metadata={"task_type": "repair", "requires_cosim": False},
        )

        self.assertEqual(decision.mode, PhaseMode.REPAIR)
        self.assertEqual(decision.reason, "BASELINE_CSIM_FAILED")
        self.assertEqual(decision.validation_status["synth"], "NOT_RUN")

    def test_synth_failure_after_csim_pass_routes_to_synth_fix(self) -> None:
        decision = self.router.route(
            baseline_csim=result(kind="csim", ok=True, phase="pass"),
            baseline_synth=result(kind="synth", ok=False, phase="tool_error"),
            baseline_cosim=None,
            requires_cosim=False,
        )

        self.assertEqual(decision.mode, PhaseMode.SYNTH_FIX)
        self.assertEqual(decision.reason, "BASELINE_SYNTH_FAILED")

    def test_required_cosim_deadlock_routes_to_structural_fix(self) -> None:
        decision = route_phase(
            baseline_csim={"status": "PASS"},
            baseline_synth={"status": "PASS"},
            baseline_cosim={"status": "FAIL", "phase": "deadlock"},
            task_metadata=SimpleNamespace(requires_cosim=True),
        )

        self.assertEqual(decision.mode, PhaseMode.STRUCTURAL_FIX)
        self.assertEqual(decision.reason, "REQUIRED_BASELINE_COSIM_FAILED")
        self.assertTrue(decision.requires_cosim)

    def test_required_cosim_timeout_routes_to_structural_fix(self) -> None:
        decision = self.router.route(
            baseline_csim={"status": "PASS"},
            baseline_synth={"status": "PASS"},
            baseline_cosim={"status": "TIMEOUT", "phase": "timeout"},
            requires_cosim=True,
        )

        self.assertEqual(decision.mode, PhaseMode.STRUCTURAL_FIX)

    def test_dot_product_skipped_optional_cosim_routes_to_optimize(self) -> None:
        decision = self.router.route(
            baseline_csim={"status": "PASS"},
            baseline_synth={"status": "PASS"},
            baseline_cosim={"status": "NOT_RUN"},
            task_metadata={"task_type": "optimize", "requires_cosim": False},
        )

        self.assertEqual(decision.mode, PhaseMode.OPTIMIZE)
        self.assertEqual(
            decision.reason,
            "BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED",
        )
        self.assertEqual(decision.to_dict()["mode"], "OPTIMIZE")

    def test_public_generate_task_routes_to_repair_after_baseline_passes(self) -> None:
        decision = self.router.route(
            baseline_csim={"status": "PASS"},
            baseline_synth={"status": "PASS"},
            baseline_cosim={"status": "NOT_RUN"},
            task_metadata={"task_type": "generate", "requires_cosim": False},
        )

        self.assertEqual(decision.mode, PhaseMode.REPAIR)
        self.assertEqual(decision.reason, "GENERATE_TASK_REQUIRES_IMPLEMENTATION")

    def test_optional_cosim_pass_also_routes_to_optimize(self) -> None:
        decision = self.router.route(
            baseline_csim={"status": "PASS"},
            baseline_synth={"status": "PASS"},
            baseline_cosim={"status": "PASS"},
            requires_cosim=False,
        )

        self.assertEqual(decision.mode, PhaseMode.OPTIMIZE)
        self.assertEqual(decision.reason, "BASELINE_VALIDATION_PASSED")

    def test_observed_optional_cosim_failure_routes_structural_fail_safe(self) -> None:
        decision = self.router.route(
            baseline_csim={"status": "PASS"},
            baseline_synth={"status": "PASS"},
            baseline_cosim={"status": "FAIL", "phase": "rtl_mismatch"},
            requires_cosim=False,
        )

        self.assertEqual(decision.mode, PhaseMode.STRUCTURAL_FIX)
        self.assertEqual(
            decision.reason, "OBSERVED_OPTIONAL_BASELINE_COSIM_FAILED"
        )
        self.assertFalse(decision.requires_cosim)

    def test_required_cosim_not_run_is_incomplete_not_structural_failure(self) -> None:
        with self.assertRaisesRegex(PhaseRoutingError, "CoSim must run"):
            self.router.route(
                baseline_csim={"status": "PASS"},
                baseline_synth={"status": "PASS"},
                baseline_cosim={"status": "NOT_RUN"},
                requires_cosim=True,
            )

    def test_missing_synth_is_incomplete_not_synth_failure(self) -> None:
        with self.assertRaisesRegex(PhaseRoutingError, "Synth must run"):
            self.router.route(
                baseline_csim={"status": "PASS"},
                baseline_synth=None,
                baseline_cosim=None,
                requires_cosim=False,
            )

    def test_conflicting_requires_cosim_metadata_is_rejected(self) -> None:
        with self.assertRaisesRegex(PhaseRoutingError, "conflicts"):
            self.router.route(
                baseline_csim={"status": "PASS"},
                baseline_synth={"status": "PASS"},
                baseline_cosim={"status": "NOT_RUN"},
                task_metadata={"requires_cosim": True},
                requires_cosim=False,
            )

    def test_contradictory_validation_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(PhaseRoutingError, "contradictory"):
            self.router.route(
                baseline_csim={"status": "PASS", "ok": False},
                baseline_synth={"status": "NOT_RUN"},
                baseline_cosim={"status": "NOT_RUN"},
                requires_cosim=False,
            )


if __name__ == "__main__":
    unittest.main()
