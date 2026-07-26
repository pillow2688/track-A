from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import minimal_flow


class MinimalFlowTests(unittest.TestCase):
    def test_demo_repair_selects_candidate_without_horizontal_artifacts(self) -> None:
        task = (
            minimal_flow.HARNESS_ROOT
            / "examples"
            / "u55c_repair_task"
        )
        patch = minimal_flow.HARNESS_ROOT / "examples" / "u55c_repair.diff"
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            exit_code = minimal_flow.main(
                [
                    "--task-dir",
                    str(task),
                    "--patch-file",
                    str(patch),
                    "--run-dir",
                    str(run_dir),
                    "--backend",
                    "demo",
                ]
            )
            self.assertEqual(exit_code, 0)
            result = json.loads(
                (run_dir / "minimal_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["mode"], "REPAIR")
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["selected_candidate_id"], "candidate_001")
            self.assertEqual(result["horizontal_decision_modules"], [])
            self.assertEqual(
                result["active_components"],
                list(minimal_flow.ACTIVE_COMPONENTS),
            )
            self.assertIn(
                "c[i] = a[i] + b[i];",
                (run_dir / "final_kernel.cpp").read_text(encoding="utf-8"),
            )
            forbidden = {
                "experience",
                "performance_area",
                "call_gates",
                "evidence_delta",
                "failure_memory",
            }
            names = {path.name for path in run_dir.rglob("*")}
            self.assertTrue(forbidden.isdisjoint(names))

    def test_official_proxy_boundaries(self) -> None:
        passing = minimal_flow.ValidationOutcome(
            candidate_id="candidate_000",
            csim=self._tool("csim", True),
            synth=self._tool("synth", True, latency=10),
            cosim=None,
            requires_cosim=False,
        )
        score, acceleration = minimal_flow._public_score(
            difficulty=2,
            baseline_latency=10,
            outcome=passing,
        )
        self.assertEqual(score, 1.475)
        self.assertEqual(acceleration, 1.0)

        functional_only = minimal_flow.ValidationOutcome(
            candidate_id="candidate_001",
            csim=self._tool("csim", True),
            synth=self._tool("synth", False),
            cosim=None,
            requires_cosim=False,
        )
        score, acceleration = minimal_flow._public_score(
            difficulty=2,
            baseline_latency=10,
            outcome=functional_only,
        )
        self.assertEqual(score, 1.0)
        self.assertIsNone(acceleration)

    def test_all_four_routes_are_derived_from_current_validation(self) -> None:
        repair = self._validation(csim_ok=False)
        synth_fix = self._validation(csim_ok=True, synth_ok=False)
        structural_fix = self._validation(
            csim_ok=True,
            synth_ok=True,
            cosim_ok=False,
            requires_cosim=True,
        )
        optimize = self._validation(csim_ok=True, synth_ok=True, latency=10)

        self.assertEqual(minimal_flow._route_mode(repair), "REPAIR")
        self.assertEqual(minimal_flow._route_mode(synth_fix), "SYNTH_FIX")
        self.assertEqual(
            minimal_flow._route_mode(structural_fix),
            "STRUCTURAL_FIX",
        )
        self.assertEqual(minimal_flow._route_mode(optimize), "OPTIMIZE")

    def test_optimize_selects_only_a_safe_strict_latency_improvement(self) -> None:
        baseline = self._validation(
            candidate_id="candidate_000",
            csim_ok=True,
            synth_ok=True,
            latency=10,
        )
        faster = self._validation(
            candidate_id="candidate_001",
            csim_ok=True,
            synth_ok=True,
            latency=9,
        )
        equal = self._validation(
            candidate_id="candidate_001",
            csim_ok=True,
            synth_ok=True,
            latency=10,
        )
        unsafe = self._validation(
            candidate_id="candidate_001",
            csim_ok=False,
        )

        self.assertEqual(
            minimal_flow._select(
                mode="OPTIMIZE",
                baseline=baseline,
                candidate=faster,
            ),
            ("candidate_001", "STRICT_WORST_LATENCY_IMPROVEMENT"),
        )
        for candidate in (equal, unsafe):
            self.assertEqual(
                minimal_flow._select(
                    mode="OPTIMIZE",
                    baseline=baseline,
                    candidate=candidate,
                ),
                ("candidate_000", "NO_SAFE_STRICT_LATENCY_IMPROVEMENT"),
            )

    def test_minimal_synth_parser_has_no_loop_evidence_dependency(self) -> None:
        xml = """\
<Report>
  <PerformanceEstimates>
    <SummaryOfTimingAnalysis>
      <EstimatedClockPeriod>2.5</EstimatedClockPeriod>
    </SummaryOfTimingAnalysis>
    <SummaryOfOverallLatency>
      <Best-caseLatency>9</Best-caseLatency>
      <Average-caseLatency>10</Average-caseLatency>
      <Worst-caseLatency>11</Worst-caseLatency>
      <Interval-min>1</Interval-min>
      <Interval-max>2</Interval-max>
    </SummaryOfOverallLatency>
  </PerformanceEstimates>
  <AreaEstimates>
    <Resources><LUT>10</LUT><FF>20</FF><DSP>1</DSP></Resources>
    <AvailableResources>
      <LUT>100</LUT><FF>200</FF><DSP>10</DSP>
    </AvailableResources>
  </AreaEstimates>
</Report>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "csynth.xml"
            path.write_text(xml, encoding="utf-8")
            report = minimal_flow._minimal_synth_report(path)

        self.assertEqual(report["estimated_clock_period_ns"], 2.5)
        self.assertEqual(report["latency"]["worst"], 11)
        self.assertEqual(report["resources"]["LUT"], 10)
        self.assertNotIn("loop_evidence", report)
        self.assertNotIn("bottlenecks", report)

    def _validation(
        self,
        *,
        candidate_id: str = "candidate",
        csim_ok: bool,
        synth_ok: bool | None = None,
        cosim_ok: bool | None = None,
        requires_cosim: bool = False,
        latency: int | None = None,
    ) -> minimal_flow.ValidationOutcome:
        return minimal_flow.ValidationOutcome(
            candidate_id=candidate_id,
            csim=self._tool("csim", csim_ok),
            synth=(
                self._tool("synth", synth_ok, latency=latency)
                if synth_ok is not None
                else None
            ),
            cosim=(
                self._tool("cosim", cosim_ok)
                if cosim_ok is not None
                else None
            ),
            requires_cosim=requires_cosim,
        )

    @staticmethod
    def _tool(
        kind: str,
        ok: bool,
        *,
        latency: int | None = None,
    ) -> minimal_flow.ToolResult:
        report = (
            {
                "latency": {
                    "best": latency,
                    "average": latency,
                    "worst": latency,
                }
            }
            if latency is not None
            else None
        )
        return minimal_flow.ToolResult(
            kind=kind,
            ok=ok,
            phase="pass" if ok else "error",
            return_code=0 if ok else 1,
            elapsed_s=0.0,
            effective_timeout_seconds=1.0,
            action_id="a" * 64,
            candidate_id="candidate",
            code_hash="b" * 64,
            tool_config_hash="c" * 64,
            backend_fingerprint="test",
            task_fingerprint="d" * 64,
            result_ref="result.json",
            cached=False,
            evidence=[],
            artifacts={},
            artifact_hashes={},
            report=report,
        )


if __name__ == "__main__":
    unittest.main()
