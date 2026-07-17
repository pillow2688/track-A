"""Shared deterministic CSim -> Synth -> Clock -> CoSim Candidate validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .budget import BudgetLedger
from .task import PublicTask
from .tools import ToolBackend, ToolResult, ToolServer
from .vitis import VitisBackend
from .workflow import RunConfig, _invoke_stage, _validation_record


@dataclass(frozen=True)
class CandidateValidation:
    status: str
    stop_reason: str
    validation: dict[str, dict[str, object]]
    clock_constraint: dict[str, object]
    metrics_ref: str | None
    budget: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "validation": self.validation,
            "clock_constraint": self.clock_constraint,
            "metrics_ref": self.metrics_ref,
            "budget": self.budget,
        }


def _record(result: ToolResult) -> dict[str, object]:
    return _validation_record(result) | {
        "validation_scope": result.validation_scope,
    }


def validate_csim_only(
    task: PublicTask,
    kernel_bytes: bytes,
    candidate_id: str,
    run_root: str | Path,
    config: RunConfig,
    *,
    backend: ToolBackend | None,
) -> CandidateValidation:
    """Run only CSim for a safety Candidate and preserve NOT_RUN downstream."""

    root = Path(run_root).resolve()
    budget = BudgetLedger(root / "budget_ledger.jsonl", config.budget)
    server = ToolServer(
        task=task,
        budget=budget,
        run_root=root,
        config=config.tool,
        backend=backend or VitisBackend(),
    )
    validation: dict[str, dict[str, object]] = {
        "csim": {"status": "NOT_RUN"},
        "synth": {"status": "NOT_RUN"},
        "cosim": {"status": "NOT_RUN"},
    }
    csim, error, reason = _invoke_stage(
        server,
        "csim",
        kernel_bytes,
        candidate_id=candidate_id,
    )
    if csim is None:
        validation["csim"] = error or {"status": "TOOL_ERROR"}
        stop_reason = reason or "CSIM_ERROR"
    else:
        validation["csim"] = _record(csim)
        stop_reason = (
            "CSIM_PASSED"
            if csim.ok
            else f"CSIM_{csim.phase.upper()}"
        )
    return CandidateValidation(
        status="DONE" if csim is not None and not csim.ok else "FAILED",
        stop_reason=stop_reason,
        validation=validation,
        clock_constraint={
            "minimum_frequency_mhz": config.minimum_frequency_mhz,
            "maximum_period_ns": 1000.0 / config.minimum_frequency_mhz,
            "estimated_period_ns": None,
            "passed": False,
            "status": "NOT_RUN",
        },
        metrics_ref=None,
        budget=budget.snapshot(),
    )


def validate_candidate(
    task: PublicTask,
    kernel_bytes: bytes,
    candidate_id: str,
    run_root: str | Path,
    config: RunConfig,
    *,
    backend: ToolBackend | None,
    validation_scope: str = "exploration",
    run_cosim: bool = True,
) -> CandidateValidation:
    if validation_scope not in {"exploration", "final"}:
        raise ValueError(f"unsupported validation scope: {validation_scope}")
    if validation_scope == "final" and not run_cosim:
        raise ValueError("final validation cannot skip cosim")
    root = Path(run_root).resolve()
    budget = BudgetLedger(root / "budget_ledger.jsonl", config.budget)
    server = ToolServer(
        task=task,
        budget=budget,
        run_root=root,
        config=config.tool,
        backend=backend or VitisBackend(),
    )
    validation: dict[str, dict[str, object]] = {
        "csim": {"status": "NOT_RUN"},
        "synth": {"status": "NOT_RUN"},
        "cosim": {"status": "NOT_RUN"},
    }
    stop_reason: str | None = None
    clock: dict[str, object] = {
        "minimum_frequency_mhz": config.minimum_frequency_mhz,
        "maximum_period_ns": 1000.0 / config.minimum_frequency_mhz,
        "estimated_period_ns": None,
        "passed": False,
    }

    csim, error, reason = _invoke_stage(
        server,
        "csim",
        kernel_bytes,
        candidate_id=candidate_id,
        validation_scope=validation_scope,
    )
    if csim is None:
        validation["csim"] = error or {"status": "TOOL_ERROR"}
        stop_reason = reason or "CSIM_ERROR"
    else:
        validation["csim"] = _record(csim)
        if not csim.ok:
            stop_reason = f"CSIM_{csim.phase.upper()}"

    synth = None
    if csim is not None and csim.ok:
        synth, error, reason = _invoke_stage(
            server,
            "synth",
            kernel_bytes,
            candidate_id=candidate_id,
            validation_scope=validation_scope,
        )
        if synth is None:
            validation["synth"] = error or {"status": "TOOL_ERROR"}
            stop_reason = reason or "SYNTH_ERROR"
        else:
            validation["synth"] = _record(synth)
            if not synth.ok:
                stop_reason = f"SYNTH_{synth.phase.upper()}"

    cosim = None
    if synth is not None and synth.ok:
        estimated = (
            synth.report.get("estimated_clock_period_ns")
            if synth.report is not None
            else None
        )
        clock["estimated_period_ns"] = estimated
        if (
            isinstance(estimated, bool)
            or not isinstance(estimated, (int, float))
            or not math.isfinite(float(estimated))
            or float(estimated) <= 0
        ):
            stop_reason = "SYNTH_INVALID_CLOCK_METRIC"
        elif float(estimated) > float(clock["maximum_period_ns"]):
            stop_reason = "CLOCK_CONSTRAINT_FAILED"
        else:
            clock["passed"] = True
            if run_cosim:
                cosim, error, reason = _invoke_stage(
                    server,
                    "cosim",
                    kernel_bytes,
                    candidate_id=candidate_id,
                    validation_scope=validation_scope,
                )
                if cosim is None:
                    validation["cosim"] = error or {"status": "TOOL_ERROR"}
                    stop_reason = reason or "COSIM_ERROR"
                else:
                    validation["cosim"] = _record(cosim)
                    if not cosim.ok:
                        stop_reason = f"COSIM_{cosim.phase.upper()}"

    if not run_cosim and synth is not None and synth.ok and stop_reason is None:
        status = "DONE"
        stop_reason = "CANDIDATE_SYNTH_VERIFIED"
    elif cosim is not None and cosim.ok and stop_reason is None:
        status = "DONE"
        stop_reason = "CANDIDATE_VERIFIED"
    else:
        status = "FAILED"
    metrics_ref = (
        synth.result_ref
        if synth is not None and synth.report is not None
        else None
    )
    return CandidateValidation(
        status=status,
        stop_reason=stop_reason,
        validation=validation,
        clock_constraint=clock,
        metrics_ref=metrics_ref,
        budget=budget.snapshot(),
    )


def complete_candidate_cosim(
    task: PublicTask,
    kernel_bytes: bytes,
    candidate_id: str,
    run_root: str | Path,
    config: RunConfig,
    preliminary: CandidateValidation,
    *,
    backend: ToolBackend | None,
    validation_scope: str = "exploration",
) -> CandidateValidation:
    """Complete a synth-verified Candidate with exactly one gated CoSim action."""

    if validation_scope not in {"exploration", "final"}:
        raise ValueError(f"unsupported validation scope: {validation_scope}")
    if (
        preliminary.status != "DONE"
        or preliminary.stop_reason != "CANDIDATE_SYNTH_VERIFIED"
        or preliminary.validation.get("csim", {}).get("status") != "PASS"
        or preliminary.validation.get("synth", {}).get("status") != "PASS"
        or preliminary.validation.get("cosim", {}).get("status") != "NOT_RUN"
        or preliminary.clock_constraint.get("passed") is not True
        or preliminary.metrics_ref is None
    ):
        raise ValueError("gated cosim requires a synth-verified Candidate")

    root = Path(run_root).resolve()
    budget = BudgetLedger(root / "budget_ledger.jsonl", config.budget)
    server = ToolServer(
        task=task,
        budget=budget,
        run_root=root,
        config=config.tool,
        backend=backend or VitisBackend(),
    )
    validation = {
        stage: dict(record)
        for stage, record in preliminary.validation.items()
    }
    cosim, error, reason = _invoke_stage(
        server,
        "cosim",
        kernel_bytes,
        candidate_id=candidate_id,
        validation_scope=validation_scope,
    )
    if cosim is None:
        validation["cosim"] = error or {"status": "TOOL_ERROR"}
        status = "FAILED"
        stop_reason = reason or "COSIM_ERROR"
    else:
        validation["cosim"] = _record(cosim)
        status = "DONE" if cosim.ok else "FAILED"
        stop_reason = (
            "CANDIDATE_VERIFIED"
            if cosim.ok
            else f"COSIM_{cosim.phase.upper()}"
        )
    return CandidateValidation(
        status=status,
        stop_reason=stop_reason,
        validation=validation,
        clock_constraint=dict(preliminary.clock_constraint),
        metrics_ref=preliminary.metrics_ref,
        budget=budget.snapshot(),
    )
