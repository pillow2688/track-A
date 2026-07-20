"""Versioned, bounded synthesis evidence for V3 planner decisions."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping, Sequence


SYNTH_EVIDENCE_SCHEMA = "v3a.synth-evidence.v1"
_MEMORY_TOKENS = (
    "memory port",
    "limited memory",
    "load operation",
    "store operation",
    "unable to schedule",
)


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(element: ET.Element, name: str) -> str | None:
    value = element.findtext(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped and stripped != "-" else None


def parse_csynth_loop_evidence(path: str | Path) -> dict[str, object]:
    """Read only loop/violation facts from a Vitis ``csynth.xml`` report."""

    root = ET.parse(path).getroot()
    report_version = root.findtext("./ReportVersion/Version")
    loops: list[dict[str, object]] = []

    def read_performance(
        performance: ET.Element | None, *, module: str | None
    ) -> list[dict[str, object]]:
        if performance is None:
            return []
        violation_summary = performance.find(
            "SummaryOfViolations/SummaryOfLoopViolations"
        )
        violations: dict[str, dict[str, object]] = {}
        if violation_summary is not None:
            for item in violation_summary:
                name = _text(item, "Name") or item.tag
                violations[name] = {
                    "issue_type": _text(item, "IssueType"),
                    "violation_type": _text(item, "ViolationType"),
                    "source_location": _text(item, "SourceLocation"),
                }
        loop_summary = performance.find("SummaryOfLoopLatency")
        parsed: list[dict[str, object]] = []
        if loop_summary is None:
            return parsed
        for item in loop_summary:
            name = _text(item, "Name") or item.tag
            violation = violations.get(name, {})
            parsed.append(
                {
                    "loop_id": f"{module}/{name}" if module else name,
                    "module": module,
                    "name": name,
                    "trip_count": _integer(item.findtext("TripCount")),
                    "latency_cycles": _integer(item.findtext("Latency")),
                    "pipeline_ii": _integer(item.findtext("PipelineII")),
                    "pipeline_depth": _integer(item.findtext("PipelineDepth")),
                    "slack_ns": _number(item.findtext("Slack")),
                    "perfect_nested": (
                        _integer(item.findtext("isPerfectNested")) == 1
                        if item.findtext("isPerfectNested") is not None
                        else None
                    ),
                    "performance_pragma": _text(item, "PerformancePragma"),
                    "issue_type": violation.get("issue_type"),
                    "violation_type": violation.get("violation_type"),
                    "source_location": violation.get("source_location"),
                }
            )
        return parsed

    module_loop_names: set[str] = set()
    for module_element in root.findall("./ModuleInformation/Module"):
        module_name = _text(module_element, "Name")
        parsed = read_performance(
            module_element.find("PerformanceEstimates"), module=module_name
        )
        loops.extend(parsed)
        module_loop_names.update(str(item["name"]) for item in parsed)

    # Older/smaller reports expose only the top-level summary.  When module
    # details exist, skip the duplicated top-level copy of the same loop.
    for item in read_performance(root.find("./PerformanceEstimates"), module=None):
        if str(item["name"]) not in module_loop_names:
            loops.append(item)
    loops.sort(
        key=lambda item: (
            -int(item.get("latency_cycles") or 0),
            str(item.get("module") or ""),
            str(item.get("name", "")),
        )
    )
    return {
        "report_version": report_version.strip() if report_version else None,
        "loops": loops,
    }


def _mapping_copy(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    copied: dict[str, object] = {}
    for key, child in value.items():
        if isinstance(child, Mapping):
            copied[str(key)] = _mapping_copy(child)
        elif isinstance(child, (str, int, float, bool)) or child is None:
            copied[str(key)] = child
    return copied


def _relevant_log_lines(values: Sequence[str]) -> list[str]:
    selected: list[str] = []
    tokens = (*_MEMORY_TOKENS, "pipeline", "violation", "loop")
    for value in values:
        for raw_line in str(value).splitlines():
            line = " ".join(raw_line.strip().split())
            lowered = line.casefold()
            if line and any(token in lowered for token in tokens):
                selected.append(line[:320])
                if len(selected) >= 12:
                    return selected
    return selected


def build_synth_evidence(
    *,
    candidate_id: str,
    action_id: str,
    result_ref: str,
    report: Mapping[str, object],
    tool_evidence: Sequence[str] = (),
    csynth_xml_path: str | Path | None = None,
    csynth_xml_ref: str | None = None,
    csynth_xml_sha256: str | None = None,
) -> dict[str, object]:
    """Create facts for a Planner without pretending they prove a root cause."""

    if not candidate_id or not action_id or not result_ref:
        raise ValueError("synthesis evidence requires Candidate/action/result identity")
    parsed: dict[str, object] = {"report_version": None, "loops": []}
    if csynth_xml_path is not None:
        parsed = parse_csynth_loop_evidence(csynth_xml_path)
    loops = parsed.get("loops")
    if not isinstance(loops, list):
        raise ValueError("parsed loop evidence must be a list")

    top_latency = _mapping_copy(report.get("latency"))
    top_interval = _mapping_copy(report.get("interval"))
    relevant_logs = _relevant_log_lines(tool_evidence)
    observations: list[dict[str, object]] = []
    for loop in loops:
        if not isinstance(loop, Mapping):
            continue
        ii = _integer(loop.get("pipeline_ii"))
        latency = _integer(loop.get("latency_cycles"))
        if ii is not None and ii > 1:
            observations.append(
                {
                    "kind": "LOOP_PIPELINE_II_GT_1",
                    "loop": loop.get("name"),
                    "source_location": loop.get("source_location"),
                    "facts": {
                        "pipeline_ii": ii,
                        "latency_cycles": latency,
                        "trip_count": loop.get("trip_count"),
                    },
                    "inference": (
                        "This loop is a candidate bottleneck; the report does not "
                        "by itself prove which code change is safe."
                    ),
                }
            )
        combined = " ".join(
            str(loop.get(key) or "")
            for key in ("issue_type", "violation_type")
        ).casefold()
        if any(token in combined for token in _MEMORY_TOKENS):
            observations.append(
                {
                    "kind": "MEMORY_SCHEDULING_CONSTRAINT",
                    "loop": loop.get("name"),
                    "source_location": loop.get("source_location"),
                    "facts": {
                        "issue_type": loop.get("issue_type"),
                        "violation_type": loop.get("violation_type"),
                    },
                    "inference": "Memory access structure may limit scheduling.",
                }
            )
    if not loops and (_integer(top_interval.get("max")) or 0) > 1:
        observations.append(
            {
                "kind": "TOP_LEVEL_INTERVAL_GT_1",
                "loop": None,
                "source_location": None,
                "facts": {"transaction_interval": top_interval.get("max")},
                "inference": (
                    "Only top-level transaction interval is available; it must "
                    "not be reported as loop PipelineII."
                ),
            }
        )
    if relevant_logs and any(
        any(token in line.casefold() for token in _MEMORY_TOKENS)
        for line in relevant_logs
    ):
        observations.append(
            {
                "kind": "MEMORY_WARNING_IN_TOOL_LOG",
                "loop": None,
                "source_location": None,
                "facts": {"matching_log_lines": relevant_logs},
                "inference": "The warning should be localized before changing memory layout.",
            }
        )

    return {
        "schema_version": SYNTH_EVIDENCE_SCHEMA,
        "candidate_id": candidate_id,
        "action_id": action_id,
        "result_ref": result_ref,
        "source": {
            "kind": "CSYNTH_XML" if csynth_xml_path is not None else "TOOL_RESULT_ONLY",
            "csynth_xml_ref": csynth_xml_ref,
            "csynth_xml_sha256": csynth_xml_sha256,
            "report_version": parsed.get("report_version"),
        },
        "top_level": {
            "estimated_clock_period_ns": report.get("estimated_clock_period_ns"),
            "latency": top_latency,
            "transaction_interval": top_interval,
            "resources": _mapping_copy(report.get("resources")),
            "utilization_percent": _mapping_copy(
                report.get("utilization_percent")
            ),
            "interval_note": (
                "transaction_interval is top-level and is not loop PipelineII"
            ),
        },
        "loops": loops,
        "observations": observations,
        "relevant_tool_log_lines": relevant_logs,
        "completeness": {
            "loop_evidence": "AVAILABLE" if loops else "UNAVAILABLE",
            "raw_csynth_xml": csynth_xml_path is not None,
        },
    }
