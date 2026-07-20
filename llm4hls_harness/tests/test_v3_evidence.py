from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_evidence import (
    SYNTH_EVIDENCE_SCHEMA,
    build_synth_evidence,
    parse_csynth_loop_evidence,
)


def csynth_xml(*, pipeline_ii: int, loop_latency: int = 1025) -> str:
    return f"""\
<profile>
  <ReportVersion><Version>2025.2</Version></ReportVersion>
  <PerformanceEstimates>
    <SummaryOfLoopLatency>
      <dot_product_loop>
        <Slack>7.10</Slack>
        <TripCount>1024</TripCount>
        <Latency>{loop_latency}</Latency>
        <PipelineII>{pipeline_ii}</PipelineII>
        <PipelineDepth>2</PipelineDepth>
        <isPerfectNested>0</isPerfectNested>
        <PerformancePragma>-</PerformancePragma>
      </dot_product_loop>
    </SummaryOfLoopLatency>
    <SummaryOfViolations>
      <SummaryOfLoopViolations>
        <dot_product_loop>
          <Name>dot_product_loop</Name>
          <IssueType>-</IssueType>
          <ViolationType>-</ViolationType>
          <SourceLocation>dotProduct.cpp:17</SourceLocation>
        </dot_product_loop>
      </SummaryOfLoopViolations>
    </SummaryOfViolations>
  </PerformanceEstimates>
</profile>
"""


def synth_report(*, transaction_interval: int = 1025) -> dict[str, object]:
    return {
        "estimated_clock_period_ns": 2.1,
        "latency": {"best": 1027, "average": 1027, "worst": 1027},
        "interval": {
            "min": transaction_interval,
            "max": transaction_interval,
        },
        "resources": {"LUT": 100, "FF": 200, "DSP": 1},
        "utilization_percent": {"LUT": 0.1, "FF": 0.1, "DSP": 0.1},
    }


class V3SynthEvidenceTests(unittest.TestCase):
    def test_module_loop_details_replace_the_duplicated_top_level_copy(self) -> None:
        xml = csynth_xml(pipeline_ii=1).replace(
            "</profile>",
            """
  <ModuleInformation>
    <Module><Name>dotProduct</Name><PerformanceEstimates>
      <SummaryOfLoopLatency><dot_product_loop>
        <Name>dot_product_loop</Name><TripCount>1024</TripCount>
        <Latency>1025</Latency><PipelineII>1</PipelineII><PipelineDepth>3</PipelineDepth>
      </dot_product_loop></SummaryOfLoopLatency>
      <SummaryOfViolations><SummaryOfLoopViolations><dot_product_loop>
        <Name>dot_product_loop</Name><SourceLocation>dotProduct.cpp:17</SourceLocation>
      </dot_product_loop></SummaryOfLoopViolations></SummaryOfViolations>
    </PerformanceEstimates></Module>
  </ModuleInformation>
</profile>""",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "csynth.xml"
            path.write_text(xml, encoding="utf-8")
            parsed = parse_csynth_loop_evidence(path)

        self.assertEqual(len(parsed["loops"]), 1)
        self.assertEqual(parsed["loops"][0]["module"], "dotProduct")
        self.assertEqual(
            parsed["loops"][0]["loop_id"], "dotProduct/dot_product_loop"
        )

    def test_loop_ii_is_not_confused_with_top_level_transaction_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "csynth.xml"
            path.write_text(csynth_xml(pipeline_ii=1), encoding="utf-8")
            evidence = build_synth_evidence(
                candidate_id="candidate_000",
                action_id="synth-action",
                result_ref="actions/synth-action/result.json",
                report=synth_report(transaction_interval=1025),
                csynth_xml_path=path,
                csynth_xml_ref="actions/synth-action/work/csynth.xml",
                csynth_xml_sha256="fixture-hash",
            )

        self.assertEqual(evidence["schema_version"], SYNTH_EVIDENCE_SCHEMA)
        self.assertEqual(
            evidence["top_level"]["transaction_interval"]["max"], 1025
        )
        self.assertEqual(evidence["loops"][0]["pipeline_ii"], 1)
        self.assertEqual(evidence["loops"][0]["trip_count"], 1024)
        self.assertEqual(evidence["loops"][0]["source_location"], "dotProduct.cpp:17")
        self.assertNotIn(
            "LOOP_PIPELINE_II_GT_1",
            {item["kind"] for item in evidence["observations"]},
        )

    def test_real_loop_ii_above_one_is_exposed_as_a_candidate_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "csynth.xml"
            path.write_text(
                csynth_xml(pipeline_ii=2, loop_latency=2047),
                encoding="utf-8",
            )
            parsed = parse_csynth_loop_evidence(path)
            evidence = build_synth_evidence(
                candidate_id="candidate_000",
                action_id="synth-action",
                result_ref="actions/synth-action/result.json",
                report=synth_report(transaction_interval=2048),
                csynth_xml_path=path,
            )

        self.assertEqual(parsed["report_version"], "2025.2")
        self.assertEqual(parsed["loops"][0]["pipeline_ii"], 2)
        observation = evidence["observations"][0]
        self.assertEqual(observation["kind"], "LOOP_PIPELINE_II_GT_1")
        self.assertEqual(observation["facts"]["pipeline_ii"], 2)

    def test_tool_result_only_evidence_marks_loop_data_unavailable(self) -> None:
        evidence = build_synth_evidence(
            candidate_id="candidate_000",
            action_id="demo-action",
            result_ref="actions/demo-action/result.json",
            report=synth_report(transaction_interval=16),
        )

        self.assertEqual(evidence["source"]["kind"], "TOOL_RESULT_ONLY")
        self.assertEqual(evidence["completeness"]["loop_evidence"], "UNAVAILABLE")
        self.assertEqual(evidence["observations"][0]["kind"], "TOP_LEVEL_INTERVAL_GT_1")
        self.assertIn("loop PipelineII", evidence["observations"][0]["inference"])


if __name__ == "__main__":
    unittest.main()
