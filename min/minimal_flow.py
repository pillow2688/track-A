#!/usr/bin/env python3
"""One-round Track A baseline with no horizontal decision components.

The flow is intentionally linear:

    task -> baseline validation -> mode routing -> one patch
         -> candidate validation -> selection -> final_kernel.cpp

It does not import or execute Continuation, Experience/Ranker, dynamic token
allocation, cross-round failure memory, Evidence Delta, or Performance-Area
advisory/ranking code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MIN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MIN_DIR.parent
HARNESS_ROOT = PROJECT_ROOT / "llm4hls_harness"
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from llm4hls_agent.budget import (  # noqa: E402
    BudgetConfig,
    BudgetLedger,
)
from llm4hls_agent.repair import (  # noqa: E402
    PatchValidationError,
)
from llm4hls_agent.task import PublicTask, load_public_task  # noqa: E402
from llm4hls_agent.tools import (  # noqa: E402
    BackendResult,
    ToolBackend,
    ToolConfig,
    ToolResult,
    ToolServer,
)
from llm4hls_agent.vitis import VitisBackend  # noqa: E402
from llm4hls_agent.workflow import _append_trace  # noqa: E402

from v1_patch_pipeline import (  # noqa: E402
    PatchResolution,
    PatchResolutionError,
    persist_patch_resolution,
    resolve_and_apply_patch,
)
from v1_planner_io import (  # noqa: E402
    PlannerAdaptationError,
    ParsedPlannerPayload,
    perform_chat_completion,
    persist_parse_result,
    persist_response,
    parse_planner_response,
)


FLOW_SCHEMA = "track-a.minimal-flow.v1.1"
ACTIVE_COMPONENTS = (
    "public_task_loader",
    "fixed_budget_ledger",
    "single_call_openai_compatible_planner",
    "kernel_patch_validation_and_interface_guard",
    "metered_tool_server",
    "minimal_vitis_executor_and_synth_report_parser",
    "task_contract_validator",
    "correctness_first_selector",
    "final_kernel_and_result_writer",
)
REMOVED_COMPONENTS = (
    "continuation",
    "experience_store",
    "experience_guidance",
    "strategy_ranker",
    "dynamic_or_hybrid_token_policy",
    "cross_round_failure_memory",
    "evidence_delta",
    "performance_area_advisory",
    "pareto_or_internal_ppa_ranking",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _canonical_budget_view(
    source_snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Convert the internal lower-bound reducer into the public nullable view."""

    snapshot = dict(source_snapshot)
    tool_used = snapshot.get("tool_used")
    tool_used = tool_used if isinstance(tool_used, Mapping) else {}
    tool_pending = snapshot.get("tool_pending")
    tool_pending = tool_pending if isinstance(tool_pending, Mapping) else {}
    pending_llm = int(tool_pending.get("llm", 0))
    snapshot["llm_requests_total"] = int(tool_used.get("llm", 0)) + pending_llm
    snapshot["usage_pending_count"] = pending_llm
    if pending_llm:
        snapshot["usage_unknown_count"] = int(
            snapshot.get("usage_unknown_count", 0)
        ) + pending_llm
        snapshot["token_usage_complete"] = False
        snapshot["cached_input_usage_complete"] = False
    snapshot["known_tokens_total"] = int(
        snapshot.get("recorded_token_lower_bound", 0)
    )
    if snapshot.get("token_usage_complete") is not True:
        snapshot["tokens_used"] = None
        snapshot["input_tokens_used"] = None
        snapshot["output_tokens_used"] = None
        snapshot["cached_input_tokens_used"] = None
        snapshot["tokens_remaining"] = None
    elif snapshot.get("cached_input_usage_complete") is not True:
        snapshot["cached_input_tokens_used"] = None
    return snapshot


def _final_budget_snapshot(ledger: BudgetLedger) -> dict[str, object]:
    """Return the canonical public budget view derived only from the Ledger."""

    return _canonical_budget_view(ledger.snapshot())


class MinimalBudgetLedger(BudgetLedger):
    """Budget Ledger whose every derived state uses nullable V1 semantics."""

    def write_snapshot(self, path: str | Path) -> dict[str, object]:
        snapshot = _final_budget_snapshot(self)
        _write_json(Path(path), snapshot)
        return snapshot


def _latency(result: ToolResult | None) -> float | None:
    if result is None or result.report is None:
        return None
    latency = result.report.get("latency")
    if not isinstance(latency, Mapping):
        return None
    for key in ("worst", "average"):
        value = latency.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) > 0
        ):
            return float(value)
    return None


def _tool_record(result: ToolResult | None) -> dict[str, object]:
    if result is None:
        return {"status": "NOT_RUN"}
    return {
        "status": "PASS" if result.ok else "FAIL",
        "phase": result.phase,
        "result_ref": result.result_ref,
        "evidence": list(result.evidence[-8:]),
    }


@dataclass(frozen=True)
class ValidationOutcome:
    candidate_id: str
    csim: ToolResult | None
    synth: ToolResult | None
    cosim: ToolResult | None
    requires_cosim: bool

    @property
    def functional_pass(self) -> bool:
        return bool(
            self.csim is not None
            and self.csim.ok
            and (
                not self.requires_cosim
                or (self.cosim is not None and self.cosim.ok)
            )
        )

    @property
    def synth_pass(self) -> bool:
        return bool(self.synth is not None and self.synth.ok)

    @property
    def latency(self) -> float | None:
        return _latency(self.synth)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "functional_pass": self.functional_pass,
            "synth_pass": self.synth_pass,
            "latency_worst_or_average": self.latency,
            "validation": {
                "csim": _tool_record(self.csim),
                "synth": _tool_record(self.synth),
                "cosim": _tool_record(self.cosim),
            },
        }


def _validate(
    *,
    task: PublicTask,
    server: ToolServer,
    kernel: bytes,
    candidate_id: str,
) -> ValidationOutcome:
    """Run exactly the task-contract validation stages, once."""

    csim = server.csim(
        kernel,
        candidate_id=candidate_id,
        validation_scope="final",
    )
    synth: ToolResult | None = None
    cosim: ToolResult | None = None
    if csim.ok:
        synth = server.synth(
            kernel,
            candidate_id=candidate_id,
            validation_scope="final",
        )
    if synth is not None and synth.ok and task.requires_cosim:
        cosim = server.cosim(
            kernel,
            candidate_id=candidate_id,
            validation_scope="final",
        )
    return ValidationOutcome(
        candidate_id=candidate_id,
        csim=csim,
        synth=synth,
        cosim=cosim,
        requires_cosim=task.requires_cosim,
    )


def _route_mode(baseline: ValidationOutcome) -> str:
    if baseline.csim is None or not baseline.csim.ok:
        return "REPAIR"
    if baseline.synth is None or not baseline.synth.ok:
        return "SYNTH_FIX"
    if baseline.requires_cosim and (
        baseline.cosim is None or not baseline.cosim.ok
    ):
        return "STRUCTURAL_FIX"
    return "OPTIMIZE"


def _public_score(
    *,
    difficulty: int,
    baseline_latency: float | None,
    outcome: ValidationOutcome,
) -> tuple[float, float | None]:
    if not outcome.functional_pass:
        return 0.0, None
    acceleration: float | None = None
    if (
        outcome.synth_pass
        and baseline_latency is not None
        and outcome.latency is not None
    ):
        acceleration = baseline_latency / outcome.latency
    ppa = min(acceleration, 8.0) / 8.0 if acceleration is not None else 0.0
    quality = 0.5 + 0.2 * (1.0 if outcome.synth_pass else 0.0) + 0.3 * ppa
    return round(float(difficulty) * quality, 4), acceleration


class DemoBackend:
    """Deterministic smoke backend; it is not HLS performance evidence."""

    def fingerprint(self) -> str:
        return "track-a-minimal-demo-v1"

    def run(
        self,
        kind: str,
        *,
        task: PublicTask,
        kernel_bytes: bytes,
        work_dir: Path,
        config: ToolConfig,
    ) -> BackendResult:
        del task, work_dir, config
        source = kernel_bytes.decode("utf-8", errors="replace")
        known_bad = "c[i] = a[i] - b[i];" in source
        if kind == "csim" and known_bad:
            return BackendResult(
                ok=False,
                phase="runtime_fail",
                return_code=1,
                elapsed_s=0.001,
                evidence=["demo public mismatch: subtraction used instead of addition"],
            )
        if kind == "synth":
            latency = 8 if "#pragma HLS PIPELINE" in source else 16
            return BackendResult(
                ok=True,
                phase="pass",
                return_code=0,
                elapsed_s=0.001,
                report={
                    "estimated_clock_period_ns": 4.0,
                    "latency": {
                        "best": latency,
                        "average": latency,
                        "worst": latency,
                    },
                    "interval": {"min": 1, "max": 1},
                    "resources": {
                        "LUT": 16,
                        "FF": 16,
                        "DSP": 0,
                        "BRAM_18K": 0,
                        "URAM": 0,
                    },
                },
            )
        if kind == "cosim":
            return BackendResult(
                ok=True,
                phase="pass",
                return_code=0,
                elapsed_s=0.001,
                cosim={"status": "Pass"},
            )
        return BackendResult(
            ok=True,
            phase="pass",
            return_code=0,
            elapsed_s=0.001,
        )


_RESOURCE_NAMES = ("LUT", "FF", "DSP", "BRAM_18K", "URAM")


def _report_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _minimal_synth_report(path: Path) -> dict[str, object]:
    """Parse only standard HLS metrics needed by the vertical flow."""

    root = ET.parse(path).getroot()
    performance = root.find("PerformanceEstimates")
    timing = (
        performance.find("SummaryOfTimingAnalysis")
        if performance is not None
        else None
    )
    latency = (
        performance.find("SummaryOfOverallLatency")
        if performance is not None
        else None
    )
    clock_text = (
        timing.findtext("EstimatedClockPeriod")
        if timing is not None
        else None
    )
    estimated_clock = float(clock_text) if clock_text else None
    if estimated_clock is not None and (
        not math.isfinite(estimated_clock) or estimated_clock <= 0
    ):
        raise ValueError("EstimatedClockPeriod must be finite and positive")

    def latency_value(tag: str) -> int | None:
        return _report_int(latency.findtext(tag)) if latency is not None else None

    area = root.find("AreaEstimates")
    resources_element = area.find("Resources") if area is not None else None
    available_element = (
        area.find("AvailableResources") if area is not None else None
    )
    resources = {
        name: (
            _report_int(resources_element.findtext(name))
            if resources_element is not None
            else None
        )
        for name in _RESOURCE_NAMES
    }
    available = {
        name: (
            _report_int(available_element.findtext(name))
            if available_element is not None
            else None
        )
        for name in _RESOURCE_NAMES
    }
    utilization = {
        name: (
            round(100.0 * resources[name] / available[name], 3)
            if resources[name] is not None and available[name]
            else None
        )
        for name in _RESOURCE_NAMES
    }
    return {
        "estimated_clock_period_ns": estimated_clock,
        "latency": {
            "best": latency_value("Best-caseLatency"),
            "average": latency_value("Average-caseLatency"),
            "worst": latency_value("Worst-caseLatency"),
        },
        "interval": {
            "min": latency_value("Interval-min"),
            "max": latency_value("Interval-max"),
        },
        "resources": resources,
        "available_resources": available,
        "utilization_percent": utilization,
    }


def _minimal_process_evidence(process: object) -> list[str]:
    return_code = int(getattr(process, "return_code"))
    evidence = [f"return_code={return_code}"]
    if bool(getattr(process, "timed_out")):
        evidence.append("subprocess timeout expired")
    combined = (
        str(getattr(process, "stdout"))
        + "\n"
        + str(getattr(process, "stderr"))
    ).strip()
    if not combined:
        return evidence
    lines = [
        " ".join(line.strip().split())[:512]
        for line in combined.splitlines()
        if line.strip()
    ]
    diagnostic_tokens = (
        "error",
        "fatal",
        "deadlock",
        "timeout",
        "mismatch",
        "failed",
    )
    relevant = [
        line
        for line in lines
        if any(token in line.casefold() for token in diagnostic_tokens)
    ]
    evidence.extend((relevant or lines)[-12:])
    return evidence


class MinimalVitisBackend(VitisBackend):
    """Vitis executor with a vertical-only synthesis report parser."""

    def fingerprint(self) -> str:
        return "track-a-minimal-vitis:v1"

    def _run_synth(
        self,
        task: PublicTask,
        work_dir: Path,
        config: ToolConfig,
    ) -> BackendResult:
        tcl = (
            "open_project synth_proj\n"
            f"add_files {{{task.kernel_name}}}\n"
            + self._common_tcl(task, config)
            + "config_compile -unsafe_math_optimizations\n"
            + "csynth_design\nexit\n"
        )
        process, artifacts = self._run_vitis(
            kind="synth",
            tcl=tcl,
            work_dir=work_dir,
            config=config,
        )
        evidence = _minimal_process_evidence(process)
        if process.timed_out:
            return BackendResult(
                False,
                "timeout",
                -1,
                process.elapsed_s,
                evidence,
                artifacts,
            )
        if process.return_code != 0:
            return BackendResult(
                False,
                "synth_error",
                process.return_code,
                process.elapsed_s,
                evidence,
                artifacts,
            )
        report_path = (
            work_dir
            / "synth_proj"
            / "sol"
            / "syn"
            / "report"
            / "csynth.xml"
        )
        if not report_path.is_file():
            return BackendResult(
                False,
                "synth_error",
                process.return_code,
                process.elapsed_s,
                ["csynth.xml is missing"],
                artifacts,
            )
        artifacts["csynth_xml"] = str(
            report_path.relative_to(work_dir.parent)
        ).replace("\\", "/")
        try:
            report = _minimal_synth_report(report_path)
        except (ET.ParseError, OSError, TypeError, ValueError) as exc:
            return BackendResult(
                False,
                "synth_error",
                process.return_code,
                process.elapsed_s,
                [f"cannot parse csynth.xml: {exc}"],
                artifacts,
            )
        estimated_clock = report.get("estimated_clock_period_ns")
        if not isinstance(estimated_clock, (int, float)) or isinstance(
            estimated_clock,
            bool,
        ):
            return BackendResult(
                False,
                "synth_error",
                process.return_code,
                process.elapsed_s,
                ["csynth.xml is missing a valid EstimatedClockPeriod"],
                artifacts,
                report=report,
            )
        return BackendResult(
            True,
            "pass",
            process.return_code,
            process.elapsed_s,
            evidence,
            artifacts,
            report=report,
        )


@dataclass(frozen=True)
class PlannerResult:
    patch: str
    hypothesis: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    elapsed_s: float = 0.0
    usage_complete: bool = False
    request_id: str | None = None
    finish_reason: str | None = None
    response_ref: str | None = None
    response_sha256: str | None = None

    @property
    def tokens_used(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "patch": self.patch,
            "hypothesis": self.hypothesis,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "tokens_used": self.tokens_used,
            "usage_complete": self.usage_complete,
            "elapsed_s": self.elapsed_s,
            "request_id": self.request_id,
            "finish_reason": self.finish_reason,
            "response_ref": self.response_ref,
            "response_sha256": self.response_sha256,
        }


def _planner_prompt(
    task: PublicTask,
    mode: str,
    baseline: ValidationOutcome,
) -> str:
    failure: dict[str, object] | None = None
    for result in (baseline.csim, baseline.synth, baseline.cosim):
        if result is not None and not result.ok:
            failure = {
                "stage": result.kind,
                "phase": result.phase,
                "evidence": list(result.evidence[-8:]),
            }
            break
    headers = {
        name: value.decode("utf-8", errors="replace")
        for name, value in task.headers.items()
    }
    objective = (
        "Reduce worst-case synthesis latency with one minimal semantics-preserving patch."
        if mode == "OPTIMIZE"
        else "Repair the observed public validation failure with one minimal patch."
    )
    payload = {
        "mode": mode,
        "task_id": task.id,
        "top": task.top,
        "kernel_name": task.kernel_name,
        "description": task.description,
        "initial_condition": task.initial_condition,
        "objective": objective,
        "failure": failure,
        "baseline_latency": baseline.latency,
        "kernel": task.kernel_code,
        "headers": headers,
    }
    return (
        "You are a minimal AMD Vitis HLS patch planner. Use only the supplied public "
        "inputs. Preserve the top function signature, interfaces and required semantics. "
        "Return one strict JSON object with exactly two string fields: hypothesis and "
        "patch. patch must be a directly applicable unified diff that changes only the "
        "kernel source. Do not claim tool results.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _static_planner(path: Path) -> PlannerResult:
    return PlannerResult(
        patch=path.read_text(encoding="utf-8"),
        hypothesis="Operator-supplied deterministic smoke patch.",
        provider="static",
        model="none",
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        usage_complete=True,
    )


def _call_live_planner(
    *,
    task: PublicTask,
    mode: str,
    baseline: ValidationOutcome,
    ledger: BudgetLedger,
    run_root: Path,
    base_url: str,
    api_key: str,
    model: str,
    max_output_tokens: int,
    timeout_s: float,
) -> PlannerResult:
    prompt = _planner_prompt(task, mode, baseline)
    payload = {
        "provider": "openai-compatible",
        "model": model,
        "prompt_sha256": _sha256(prompt.encode("utf-8")),
        "max_output_tokens": max_output_tokens,
    }
    action_id = _sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    planner_dir = run_root / "planner"
    result_path = planner_dir / "result.json"
    trace_path = run_root / "trace.jsonl"
    ledger.reserve(
        action_id=action_id,
        kind="llm",
        candidate_id="candidate_000",
        code_hash=task.kernel_sha256,
        tool_config_hash=_sha256(model.encode("utf-8")),
        estimated_tokens=int(ledger.snapshot()["tokens_remaining"]),
    )
    ledger.write_snapshot(run_root / "budget_state.json")
    _append_trace(
        trace_path,
        "LLM_STARTED",
        action_id=action_id,
        candidate_id="candidate_000",
        kind="llm",
        model=model,
        prompt_sha256=payload["prompt_sha256"],
        max_output_tokens=max_output_tokens,
    )
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    exchange = perform_chat_completion(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        prompt=prompt,
        max_output_tokens=max_output_tokens,
        timeout_s=timeout_s,
    )
    receipt = persist_response(
        planner_dir=planner_dir,
        exchange=exchange,
        model=model,
        api_key=api_key,
        action_id=action_id,
        prompt_sha256=str(payload["prompt_sha256"]),
        endpoint=endpoint,
    )
    receipt_ref = f"planner/{receipt.receipt_ref}"
    if receipt.usage.usage_complete:
        assert receipt.usage.tokens_used is not None
        assert receipt.usage.input_tokens is not None
        assert receipt.usage.output_tokens is not None
        ledger.complete(
            action_id=action_id,
            result_ref=receipt_ref,
            result_sha256=receipt.receipt_sha256,
            elapsed_s=receipt.elapsed_s,
            tokens_used=receipt.usage.tokens_used,
            input_tokens=receipt.usage.input_tokens,
            output_tokens=receipt.usage.output_tokens,
            cached_input_tokens=receipt.usage.cached_input_tokens,
        )
    else:
        ledger.complete_unknown_usage(
            action_id=action_id,
            result_ref=receipt_ref,
            result_sha256=receipt.receipt_sha256,
            elapsed_s=receipt.elapsed_s,
        )
    ledger.write_snapshot(run_root / "budget_state.json")
    _append_trace(
        trace_path,
        "LLM_RESPONSE_ACCOUNTED",
        action_id=action_id,
        kind="llm",
        response_ref=receipt_ref,
        response_sha256=receipt.receipt_sha256,
        request_id=receipt.request_id,
        finish_reason=receipt.finish_reason,
        usage_complete=receipt.usage.usage_complete,
        tokens_used=receipt.usage.tokens_used,
    )

    try:
        parsed: ParsedPlannerPayload = parse_planner_response(receipt)
    except PlannerAdaptationError as exc:
        persist_parse_result(planner_dir=planner_dir, payload=None, error=exc)
        record: dict[str, object] = {
            "ok": False,
            "error_type": exc.category,
            "error": exc.detail,
            "provider": "openai-compatible",
            "model": model,
            "request_id": receipt.request_id,
            "finish_reason": receipt.finish_reason,
            "usage": receipt.usage.to_dict(),
            "response_ref": receipt_ref,
            "response_sha256": receipt.receipt_sha256,
        }
        _write_json(result_path, record)
        _append_trace(
            trace_path,
            "LLM_PARSE_REJECTED",
            action_id=action_id,
            kind="llm",
            error_type=exc.category,
            response_ref=receipt_ref,
        )
        raise

    persist_parse_result(planner_dir=planner_dir, payload=parsed, error=None)
    result = PlannerResult(
        patch=parsed.patch,
        hypothesis=parsed.hypothesis,
        provider="openai-compatible",
        model=model,
        input_tokens=receipt.usage.input_tokens,
        output_tokens=receipt.usage.output_tokens,
        cached_input_tokens=receipt.usage.cached_input_tokens,
        elapsed_s=receipt.elapsed_s,
        usage_complete=receipt.usage.usage_complete,
        request_id=receipt.request_id,
        finish_reason=receipt.finish_reason,
        response_ref=receipt_ref,
        response_sha256=receipt.receipt_sha256,
    )
    _write_json(result_path, {"ok": True, **result.to_dict()})
    _append_trace(
        trace_path,
        "LLM_PARSED",
        action_id=action_id,
        kind="llm",
        response_ref=receipt_ref,
        patch_sha256=_sha256(parsed.patch.encode("utf-8")),
    )
    return result


def _apply_patch(
    *,
    task: PublicTask,
    patch: str,
    max_changed_lines: int,
) -> PatchResolution:
    return resolve_and_apply_patch(
        task=task,
        raw_patch=patch,
        max_changed_lines=max_changed_lines,
    )


def _select(
    *,
    mode: str,
    baseline: ValidationOutcome,
    candidate: ValidationOutcome,
) -> tuple[str, str]:
    if mode == "OPTIMIZE":
        if (
            candidate.functional_pass
            and candidate.synth_pass
            and baseline.latency is not None
            and candidate.latency is not None
            and candidate.latency < baseline.latency
        ):
            return candidate.candidate_id, "STRICT_WORST_LATENCY_IMPROVEMENT"
        return baseline.candidate_id, "NO_SAFE_STRICT_LATENCY_IMPROVEMENT"
    if candidate.functional_pass and candidate.synth_pass:
        return candidate.candidate_id, "FUNCTIONAL_AND_SYNTHESIZABLE_REPAIR"
    if candidate.functional_pass and not baseline.functional_pass:
        return candidate.candidate_id, "FUNCTIONAL_PARTIAL_CREDIT_RESCUE"
    return baseline.candidate_id, "CANDIDATE_REJECTED_BASELINE_PRESERVED"


def _default_vitis_root() -> str:
    configured = os.environ.get("LLM4HLS_VITIS_HLS_ROOT")
    if configured:
        return configured
    local = Path("/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis")
    return str(local if local.is_dir() else Path("/opt/xilinx/2025.2/Vitis"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the one-round Track A flow without horizontal components."
    )
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    planner = parser.add_mutually_exclusive_group(required=True)
    planner.add_argument("--patch-file", type=Path)
    planner.add_argument("--live-openai", action="store_true")
    parser.add_argument("--backend", choices=("demo", "vitis"), default="demo")
    parser.add_argument("--vitis-root", default=_default_vitis_root())
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument(
        "--model",
        default=os.environ.get("LLM4HLS_MODEL", "deepseek-v4-pro"),
    )
    parser.add_argument("--credit-limit", type=int)
    parser.add_argument("--token-budget", type=int, default=32768)
    parser.add_argument("--max-output-tokens", type=int, default=2000)
    parser.add_argument("--runtime-limit", type=float, default=3600.0)
    parser.add_argument("--planner-timeout", type=float, default=120.0)
    parser.add_argument("--csim-timeout", type=float, default=180.0)
    parser.add_argument("--synth-timeout", type=float, default=900.0)
    parser.add_argument("--cosim-timeout", type=float, default=900.0)
    parser.add_argument("--cost-csim", type=int, default=1)
    parser.add_argument("--cost-synth", type=int, default=4)
    parser.add_argument("--cost-cosim", type=int, default=20)
    parser.add_argument("--max-changed-lines", type=int, default=80)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    task = load_public_task(args.task_dir)
    run_root = args.run_dir.resolve()
    if (run_root / "minimal_result.json").exists():
        raise RuntimeError(
            f"run directory already contains a completed result: {run_root}"
        )
    run_root.mkdir(parents=True, exist_ok=True)
    credit_limit = task.budget if args.credit_limit is None else args.credit_limit
    budget_config = BudgetConfig(
        credit_limit=credit_limit,
        costs={
            "csim": args.cost_csim,
            "synth": args.cost_synth,
            "cosim": args.cost_cosim,
            "llm": 0,
        },
        tool_limits={"csim": 2, "synth": 2, "cosim": 2, "llm": 1},
        token_limit=args.token_budget,
        runtime_limit_seconds=args.runtime_limit,
    )
    tool_config = ToolConfig(
        vitis_root=args.vitis_root,
        part=task.part,
        clock_ns=task.clock_ns,
        timeouts={
            "csim": args.csim_timeout,
            "synth": args.synth_timeout,
            "cosim": args.cosim_timeout,
        },
    )
    _write_json(
        run_root / "minimal_config.json",
        {
            "schema_version": FLOW_SCHEMA,
            "task_id": task.id,
            "backend": args.backend,
            "budget": budget_config.to_dict(),
            "tool": tool_config.to_dict(),
            "planner": "openai-compatible" if args.live_openai else "static",
            "active_components": list(ACTIVE_COMPONENTS),
            "horizontal_components_removed": list(REMOVED_COMPONENTS),
        },
    )
    ledger = MinimalBudgetLedger(
        run_root / "budget_ledger.jsonl",
        budget_config,
    )
    backend: ToolBackend = (
        DemoBackend() if args.backend == "demo" else MinimalVitisBackend()
    )
    server = ToolServer(
        task=task,
        budget=ledger,
        run_root=run_root,
        config=tool_config,
        backend=backend,
    )

    baseline = _validate(
        task=task,
        server=server,
        kernel=task.kernel_bytes,
        candidate_id="candidate_000",
    )
    mode = _route_mode(baseline)
    planner_error: str | None = None
    patch_error: str | None = None
    planner_result: PlannerResult | None = None
    candidate: ValidationOutcome | None = None
    candidate_kernel: bytes | None = None
    patch_evidence: dict[str, object] | None = None

    try:
        if args.patch_file is not None:
            planner_result = _static_planner(args.patch_file)
        else:
            if not args.base_url or not args.api_key:
                raise RuntimeError(
                    "--live-openai requires OPENAI_BASE_URL and OPENAI_API_KEY"
                )
            planner_result = _call_live_planner(
                task=task,
                mode=mode,
                baseline=baseline,
                ledger=ledger,
                run_root=run_root,
                base_url=args.base_url,
                api_key=args.api_key,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                timeout_s=args.planner_timeout,
            )
    except Exception as exc:
        planner_error = f"{type(exc).__name__}: {exc}"

    if planner_result is not None:
        try:
            resolution = _apply_patch(
                task=task,
                patch=planner_result.patch,
                max_changed_lines=args.max_changed_lines,
            )
            candidate_kernel = resolution.application.patched_bytes
            persisted = persist_patch_resolution(
                evidence_dir=run_root / "patch",
                raw_patch=resolution.raw_patch,
                normalized_patch=resolution.normalized_patch,
                applied_patch=resolution.applied_patch,
                receipt=resolution.receipt,
            )
            patch_evidence = {
                key: (
                    f"patch/{value}"
                    if key.endswith("_ref") and isinstance(value, str)
                    else value
                )
                for key, value in persisted.items()
            }
            _append_trace(
                run_root / "trace.jsonl",
                "PATCH_RESOLVED",
                selected_stage=resolution.selected_stage,
                patch_resolution_ref=patch_evidence["patch_resolution_ref"],
                patch_resolution_sha256=patch_evidence[
                    "patch_resolution_sha256"
                ],
                fuzzy_matching_used=False,
            )
            candidate_root = run_root / "candidate_001"
            candidate_root.mkdir(parents=True, exist_ok=True)
            (candidate_root / task.kernel_name).write_bytes(candidate_kernel)
            (candidate_root / "patch.diff").write_text(
                resolution.applied_patch,
                encoding="utf-8",
            )
            candidate = _validate(
                task=task,
                server=server,
                kernel=candidate_kernel,
                candidate_id="candidate_001",
            )
        except PatchResolutionError as exc:
            persisted = persist_patch_resolution(
                evidence_dir=run_root / "patch",
                raw_patch=exc.raw_patch,
                normalized_patch=exc.normalized_patch,
                applied_patch=None,
                receipt=exc.receipt,
            )
            patch_evidence = {
                key: (
                    f"patch/{value}"
                    if key.endswith("_ref") and isinstance(value, str)
                    else value
                )
                for key, value in persisted.items()
            }
            patch_error = f"{type(exc).__name__}: {exc}"
            _append_trace(
                run_root / "trace.jsonl",
                "PATCH_REJECTED",
                patch_resolution_ref=patch_evidence["patch_resolution_ref"],
                patch_resolution_sha256=patch_evidence[
                    "patch_resolution_sha256"
                ],
                fuzzy_matching_used=False,
            )
        except (PatchValidationError, OSError, ValueError) as exc:
            patch_error = f"{type(exc).__name__}: {exc}"

    if candidate is None:
        selected_id = baseline.candidate_id
        selection_reason = (
            "PLANNER_FAILED_BASELINE_PRESERVED"
            if planner_error
            else "PATCH_FAILED_BASELINE_PRESERVED"
        )
    else:
        selected_id, selection_reason = _select(
            mode=mode,
            baseline=baseline,
            candidate=candidate,
        )
    selected_kernel = (
        candidate_kernel
        if selected_id == "candidate_001" and candidate_kernel is not None
        else task.kernel_bytes
    )
    selected_outcome = candidate if selected_id == "candidate_001" else baseline
    assert selected_outcome is not None
    baseline_score, baseline_acceleration = _public_score(
        difficulty=task.difficulty,
        baseline_latency=baseline.latency,
        outcome=baseline,
    )
    candidate_score: float | None = None
    candidate_acceleration: float | None = None
    if candidate is not None:
        candidate_score, candidate_acceleration = _public_score(
            difficulty=task.difficulty,
            baseline_latency=baseline.latency,
            outcome=candidate,
        )
    selected_score = (
        candidate_score if selected_id == "candidate_001" else baseline_score
    )
    (run_root / "final_kernel.cpp").write_bytes(selected_kernel)
    status = (
        "DONE"
        if selected_outcome.functional_pass and selected_outcome.synth_pass
        else "PARTIAL"
        if selected_outcome.functional_pass
        else "FAILED"
    )
    final_budget = _final_budget_snapshot(ledger)
    _write_json(run_root / "budget_state.json", final_budget)
    planner_receipt_path = run_root / "planner" / "response_receipt.json"
    planner_response: dict[str, object] | None = None
    if planner_receipt_path.is_file():
        try:
            loaded_receipt = json.loads(
                planner_receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            loaded_receipt = None
        if isinstance(loaded_receipt, dict):
            planner_response = loaded_receipt
    result: dict[str, object] = {
        "schema_version": FLOW_SCHEMA,
        "status": status,
        "task_id": task.id,
        "mode": mode,
        "baseline": baseline.to_dict()
        | {
            "public_score_proxy": baseline_score,
            "acceleration": baseline_acceleration,
        },
        "candidate": (
            candidate.to_dict()
            | {
                "public_score_proxy": candidate_score,
                "acceleration": candidate_acceleration,
            }
            if candidate is not None
            else None
        ),
        "planner": planner_result.to_dict() if planner_result is not None else None,
        "planner_response": planner_response,
        "planner_error": planner_error,
        "patch_error": patch_error,
        "patch_resolution": patch_evidence,
        "selected_candidate_id": selected_id,
        "selection_reason": selection_reason,
        "selected_public_score_proxy": selected_score,
        "final_kernel_ref": "final_kernel.cpp",
        "final_kernel_sha256": _sha256(selected_kernel),
        "budget": final_budget,
        "active_components": list(ACTIVE_COMPONENTS),
        "horizontal_decision_modules": [],
        "horizontal_components_removed": list(REMOVED_COMPONENTS),
    }
    _write_json(run_root / "minimal_result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    print(
        json.dumps(
            {
                "status": result["status"],
                "task_id": result["task_id"],
                "mode": result["mode"],
                "selected_candidate_id": result["selected_candidate_id"],
                "selected_public_score_proxy": result[
                    "selected_public_score_proxy"
                ],
                "run_dir": str(args.run_dir.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["status"] in {"DONE", "PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
