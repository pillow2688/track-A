from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

try:
    from llm4hls_agent.task import load_public_task
    from llm4hls_agent.tools import ToolConfig
    from llm4hls_agent.vitis import (
        ProcessResult,
        VitisBackend,
        parse_cosim_report,
        parse_synth_report,
    )
except ModuleNotFoundError:
    def _missing(*_args: object, **_kwargs: object):
        raise AssertionError("Vitis backend is not implemented")

    load_public_task = ToolConfig = ProcessResult = VitisBackend = _missing  # type: ignore[misc,assignment]
    parse_cosim_report = parse_synth_report = _missing


def make_task(root: Path) -> None:
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "fixture"',
                'task_type = "structural"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 80",
                "requires_cosim = true",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text('#include "kernel.h"\nvoid kernel() {}\n', encoding="utf-8")
    (root / "kernel.h").write_text("void kernel();\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class PlannedRunner:
    def __init__(self, results: list[object], *, create_csim_exe: bool = False) -> None:
        self.results = list(results)
        self.create_csim_exe = create_csim_exe
        self.calls: list[tuple[list[str], Path, float]] = []

    def run(self, command: list[str], *, cwd: Path, timeout_s: float):
        self.calls.append((command, cwd, timeout_s))
        if self.create_csim_exe and len(self.calls) == 1:
            exe = cwd / "csim_proj" / "sol" / "csim" / "build" / "csim.exe"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"fixture")
        return self.results.pop(0)


class DelayedRunner(PlannedRunner):
    def run(self, command: list[str], *, cwd: Path, timeout_s: float):
        time.sleep(0.03)
        return super().run(command, cwd=cwd, timeout_s=timeout_s)


class PassingCosimReportRunner(PlannedRunner):
    def run(self, command: list[str], *, cwd: Path, timeout_s: float):
        synth = cwd / "cosim_proj" / "sol" / "syn" / "report" / "csynth.xml"
        synth.parent.mkdir(parents=True, exist_ok=True)
        synth.write_text("<Report />", encoding="utf-8")
        report = cwd / "cosim_proj" / "sol" / "sim" / "report" / "kernel_cosim.rpt"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            "| RTL | Status | Latency | Latency | Latency | Interval |\n"
            "| Verilog | Pass | 20 | 21 | 22 | 2 |\n",
            encoding="utf-8",
        )
        return super().run(command, cwd=cwd, timeout_s=timeout_s)


class MissingCosimReportRunner(PlannedRunner):
    def run(self, command: list[str], *, cwd: Path, timeout_s: float):
        synth = cwd / "cosim_proj" / "sol" / "syn" / "report" / "csynth.xml"
        synth.parent.mkdir(parents=True, exist_ok=True)
        synth.write_text("<Report />", encoding="utf-8")
        return super().run(command, cwd=cwd, timeout_s=timeout_s)


class VitisBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        task_dir.mkdir()
        make_task(task_dir)
        self.task = load_public_task(task_dir)
        self.config = ToolConfig(
            vitis_root="/opt/xilinx/2025.2/Vitis",
            part=self.task.part,
            clock_ns=self.task.clock_ns,
            timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_csim_separates_successful_compile_from_runtime_failure(self) -> None:
        runner = PlannedRunner(
            [
                ProcessResult(0, "compiled", "", 1.0, False),
                ProcessResult(1, "test failed", "mismatch", 0.5, False),
            ],
            create_csim_exe=True,
        )
        work = self.root / "csim-work"

        result = VitisBackend(runner).run(
            "csim",
            task=self.task,
            kernel_bytes=self.task.kernel_bytes,
            work_dir=work,
            config=self.config,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "runtime_fail")
        self.assertEqual(result.return_code, 1)
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("csim_design -setup", (work / "run_hls.tcl").read_text(encoding="utf-8"))
        self.assertEqual((work / "csim.stderr.log").read_text(encoding="utf-8"), "mismatch")

    def test_synth_does_not_fabricate_pass_when_report_is_missing(self) -> None:
        runner = PlannedRunner([ProcessResult(0, "done", "", 2.0, False)])
        work = self.root / "synth-work"

        result = VitisBackend(runner).run(
            "synth",
            task=self.task,
            kernel_bytes=self.task.kernel_bytes,
            work_dir=work,
            config=self.config,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "synth_error")
        self.assertIn("csynth.xml is missing", result.evidence)
        self.assertTrue((work / "vitis.stdout.log").is_file())
        self.assertTrue((work / "run_hls.tcl").is_file())

    def test_cosim_timeout_is_structured(self) -> None:
        runner = PlannedRunner([ProcessResult(-1, "", "hung", 30.0, True)])

        result = VitisBackend(runner).run(
            "cosim",
            task=self.task,
            kernel_bytes=self.task.kernel_bytes,
            work_dir=self.root / "cosim-work",
            config=self.config,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "timeout")
        self.assertEqual(result.return_code, -1)

    def test_cosim_nonzero_exit_cannot_be_overridden_by_pass_report(self) -> None:
        runner = PassingCosimReportRunner(
            [ProcessResult(1, "cosim reported an error", "failed", 2.0, False)]
        )

        result = VitisBackend(runner).run(
            "cosim",
            task=self.task,
            kernel_bytes=self.task.kernel_bytes,
            work_dir=self.root / "cosim-nonzero",
            config=self.config,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "cosim_fail")
        self.assertEqual(result.return_code, 1)
        self.assertIn("return_code=1", result.evidence)

    def test_cosim_missing_report_preserves_bounded_failure_log(self) -> None:
        runner = MissingCosimReportRunner(
            [
                ProcessResult(
                    1,
                    "ERROR!!! DEADLOCK DETECTED\n"
                    "Process stageB blocked by full output FIFO s_main\n",
                    "Deadlock detected in co-simulation",
                    2.0,
                    False,
                )
            ]
        )

        result = VitisBackend(runner).run(
            "cosim",
            task=self.task,
            kernel_bytes=self.task.kernel_bytes,
            work_dir=self.root / "cosim-missing-report",
            config=self.config,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "cosim_fail")
        self.assertIn("cosim report is missing", result.evidence)
        self.assertTrue(any("DEADLOCK DETECTED" in line for line in result.evidence))

    def test_report_parsers_return_structured_metrics(self) -> None:
        xml = self.root / "csynth.xml"
        xml.write_text(
            """<Report>
<PerformanceEstimates>
  <SummaryOfTimingAnalysis><EstimatedClockPeriod>4.125</EstimatedClockPeriod></SummaryOfTimingAnalysis>
  <SummaryOfOverallLatency>
    <Best-caseLatency>10</Best-caseLatency><Average-caseLatency>11</Average-caseLatency>
    <Worst-caseLatency>12</Worst-caseLatency><Interval-min>2</Interval-min><Interval-max>3</Interval-max>
  </SummaryOfOverallLatency>
  <SummaryOfLoopLatency>
    <Loop>
      <Name>VITIS_LOOP_7_1</Name><TripCount>1024</TripCount>
      <Latency>1025</Latency><PipelineII>1</PipelineII><PipelineDepth>3</PipelineDepth>
    </Loop>
  </SummaryOfLoopLatency>
</PerformanceEstimates>
<AreaEstimates>
  <Resources><LUT>100</LUT><FF>200</FF><DSP>3</DSP><BRAM_18K>4</BRAM_18K><URAM>5</URAM></Resources>
  <AvailableResources><LUT>1000</LUT><FF>2000</FF><DSP>30</DSP><BRAM_18K>40</BRAM_18K><URAM>50</URAM></AvailableResources>
</AreaEstimates>
</Report>""",
            encoding="utf-8",
        )
        cosim = self.root / "kernel_cosim.rpt"
        cosim.write_text(
            "| RTL | Status | Latency | Latency | Latency | Interval |\n"
            "| Verilog | Pass | 20 | 21 | 22 | 2 |\n",
            encoding="utf-8",
        )

        synth_metrics = parse_synth_report(xml)
        cosim_metrics = parse_cosim_report(cosim)

        self.assertEqual(synth_metrics["estimated_clock_period_ns"], 4.125)
        self.assertEqual(synth_metrics["latency"]["worst"], 12)
        self.assertEqual(synth_metrics["interval"]["max"], 3)
        self.assertEqual(
            synth_metrics["loop_evidence"]["loops"][0]["pipeline_ii"], 1
        )
        self.assertEqual(
            synth_metrics["loop_evidence"]["loops"][0]["trip_count"], 1024
        )
        self.assertEqual(synth_metrics["resources"]["BRAM_18K"], 4)
        self.assertEqual(synth_metrics["utilization_percent"]["URAM"], 10.0)
        self.assertEqual(cosim_metrics["status"], "Pass")
        self.assertEqual(cosim_metrics["latency"]["max"], 22)

    def test_csim_compile_and_execution_share_one_timeout_budget(self) -> None:
        runner = DelayedRunner(
            [
                ProcessResult(0, "compiled", "", 0.03, False),
                ProcessResult(0, "passed", "", 0.03, False),
            ],
            create_csim_exe=True,
        )
        config = ToolConfig(
            vitis_root=self.config.vitis_root,
            part=self.config.part,
            clock_ns=self.config.clock_ns,
            timeouts={"csim": 0.2, "synth": 1.0, "cosim": 1.0},
        )

        result = VitisBackend(runner).run(
            "csim",
            task=self.task,
            kernel_bytes=self.task.kernel_bytes,
            work_dir=self.root / "shared-deadline",
            config=config,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(runner.calls), 2)
        self.assertLessEqual(runner.calls[0][2], 0.2)
        self.assertLess(runner.calls[1][2], runner.calls[0][2])

    def test_invalid_synth_clock_values_are_rejected(self) -> None:
        for value in ("NaN", "Inf", "0", "-1"):
            with self.subTest(value=value):
                path = self.root / f"invalid-{value}.xml"
                path.write_text(
                    "<Report><PerformanceEstimates><SummaryOfTimingAnalysis>"
                    f"<EstimatedClockPeriod>{value}</EstimatedClockPeriod>"
                    "</SummaryOfTimingAnalysis></PerformanceEstimates></Report>",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    parse_synth_report(path)

    def test_backend_exposes_stable_cache_fingerprint(self) -> None:
        first = VitisBackend(PlannedRunner([])).fingerprint()
        second = VitisBackend(PlannedRunner([])).fingerprint()
        self.assertEqual(first, second)
        self.assertIn("VitisBackend", first)


if __name__ == "__main__":
    unittest.main()
