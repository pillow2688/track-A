"""Deterministic task-phase routing from baseline validation outcomes.

The router is intentionally independent from LangGraph and the LLM planner.  It
only classifies an already-observed baseline into the next task mode; graph
nodes remain responsible for executing tools and planners remain responsible
for proposing source patches.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class PhaseRoutingError(ValueError):
    """Raised when baseline evidence is missing, contradictory, or malformed."""


class PhaseMode(str, Enum):
    """Supported task-aware planner modes."""

    REPAIR = "REPAIR"
    SYNTH_FIX = "SYNTH_FIX"
    STRUCTURAL_FIX = "STRUCTURAL_FIX"
    OPTIMIZE = "OPTIMIZE"


@dataclass(frozen=True)
class PhaseDecision:
    """Auditable result of a deterministic phase-routing decision."""

    mode: PhaseMode
    reason: str
    requires_cosim: bool
    validation_status: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "requires_cosim": self.requires_cosim,
            "validation_status": dict(self.validation_status),
        }


_PASS = "PASS"
_NOT_RUN = "NOT_RUN"
_FAIL = "FAIL"


def _field(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _normalise_status(stage: str, record: object | None) -> str:
    """Reduce ToolResult/validation dictionaries to PASS, FAIL, or NOT_RUN."""

    if record is None:
        return _NOT_RUN

    raw_status = _field(record, "status")
    raw_ok = _field(record, "ok")
    raw_phase = _field(record, "phase")

    status = str(raw_status).strip().upper() if raw_status is not None else ""
    phase = str(raw_phase).strip().lower() if raw_phase is not None else ""

    if raw_ok is not None and not isinstance(raw_ok, bool):
        raise PhaseRoutingError(f"{stage} validation ok must be boolean")

    status_pass = status in {"PASS", "PASSED", "SUCCESS", "SUCCEEDED"}
    status_not_run = status in {"", "NOT_RUN", "SKIPPED", "PENDING"}
    phase_pass = phase in {"pass", "passed", "success", "succeeded"}
    phase_not_run = phase in {"", "not_run", "skipped", "pending"}

    if raw_ok is True:
        if status and not status_pass:
            raise PhaseRoutingError(
                f"{stage} validation is contradictory: ok=true, status={status}"
            )
        return _PASS

    if raw_ok is False:
        if status_pass:
            raise PhaseRoutingError(
                f"{stage} validation is contradictory: ok=false, status={status}"
            )
        return _FAIL

    if status_pass or (status_not_run and phase_pass):
        return _PASS
    if status_not_run and phase_not_run:
        return _NOT_RUN

    # FAIL, TIMEOUT, TOOL_ERROR, DEADLOCK, RTL_MISMATCH, and other explicit
    # non-success terminal states all route to the corresponding repair mode.
    return _FAIL


def _metadata_requires_cosim(task_metadata: object | None) -> bool | None:
    if task_metadata is None:
        return None
    if isinstance(task_metadata, Mapping):
        value = task_metadata.get("requires_cosim")
    else:
        value = getattr(task_metadata, "requires_cosim", None)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise PhaseRoutingError("task metadata requires_cosim must be boolean")
    return value


def _resolve_requires_cosim(
    *, task_metadata: object | None, requires_cosim: bool | None
) -> bool:
    metadata_value = _metadata_requires_cosim(task_metadata)
    if requires_cosim is not None and not isinstance(requires_cosim, bool):
        raise PhaseRoutingError("requires_cosim must be boolean")
    if (
        requires_cosim is not None
        and metadata_value is not None
        and requires_cosim != metadata_value
    ):
        raise PhaseRoutingError(
            "requires_cosim conflicts with task metadata requires_cosim"
        )
    resolved = requires_cosim if requires_cosim is not None else metadata_value
    if resolved is None:
        raise PhaseRoutingError("requires_cosim is required for phase routing")
    return resolved


class PhaseRouter:
    """Pure-Python decision node for selecting the task repair/optimize mode."""

    def route(
        self,
        *,
        baseline_csim: object | None,
        baseline_synth: object | None,
        baseline_cosim: object | None,
        task_metadata: object | None = None,
        requires_cosim: bool | None = None,
    ) -> PhaseDecision:
        required_cosim = _resolve_requires_cosim(
            task_metadata=task_metadata,
            requires_cosim=requires_cosim,
        )
        statuses = {
            "csim": _normalise_status("csim", baseline_csim),
            "synth": _normalise_status("synth", baseline_synth),
            "cosim": _normalise_status("cosim", baseline_cosim),
        }

        if statuses["csim"] == _FAIL:
            return PhaseDecision(
                mode=PhaseMode.REPAIR,
                reason="BASELINE_CSIM_FAILED",
                requires_cosim=required_cosim,
                validation_status=statuses,
            )
        if statuses["csim"] != _PASS:
            raise PhaseRoutingError("baseline CSim must run before phase routing")

        if statuses["synth"] == _FAIL:
            return PhaseDecision(
                mode=PhaseMode.SYNTH_FIX,
                reason="BASELINE_SYNTH_FAILED",
                requires_cosim=required_cosim,
                validation_status=statuses,
            )
        if statuses["synth"] != _PASS:
            raise PhaseRoutingError("baseline Synth must run after CSim passes")

        if required_cosim:
            if statuses["cosim"] == _FAIL:
                return PhaseDecision(
                    mode=PhaseMode.STRUCTURAL_FIX,
                    reason="REQUIRED_BASELINE_COSIM_FAILED",
                    requires_cosim=True,
                    validation_status=statuses,
                )
            if statuses["cosim"] != _PASS:
                raise PhaseRoutingError(
                    "baseline CoSim must run when requires_cosim=true"
                )

        if not required_cosim and statuses["cosim"] == _FAIL:
            return PhaseDecision(
                mode=PhaseMode.STRUCTURAL_FIX,
                reason="OBSERVED_OPTIONAL_BASELINE_COSIM_FAILED",
                requires_cosim=False,
                validation_status=statuses,
            )

        # A non-structural task deliberately skips baseline CoSim.  A PASS is
        # also accepted when a strict validation profile elected to run it.
        return PhaseDecision(
            mode=PhaseMode.OPTIMIZE,
            reason=(
                "BASELINE_CSIM_SYNTH_PASSED_COSIM_NOT_REQUIRED"
                if statuses["cosim"] == _NOT_RUN
                else "BASELINE_VALIDATION_PASSED"
            ),
            requires_cosim=required_cosim,
            validation_status=statuses,
        )


def route_phase(
    *,
    baseline_csim: object | None,
    baseline_synth: object | None,
    baseline_cosim: object | None,
    task_metadata: object | None = None,
    requires_cosim: bool | None = None,
) -> PhaseDecision:
    """Functional convenience wrapper suitable for a LangGraph decision node."""

    return PhaseRouter().route(
        baseline_csim=baseline_csim,
        baseline_synth=baseline_synth,
        baseline_cosim=baseline_cosim,
        task_metadata=task_metadata,
        requires_cosim=requires_cosim,
    )
