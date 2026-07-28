"""Shared monotonic runtime deadline and deterministic pre-start gates."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable


DEFAULT_CLEANUP_RESERVE_SECONDS = 30.0
DEFAULT_COSIM_MINIMUM_RUNTIME_SECONDS = 60.0

TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME = (
    "TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME"
)
COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME = (
    "COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME"
)
FINAL_CLOSURE_UNAFFORDABLE_RUNTIME = "FINAL_CLOSURE_UNAFFORDABLE_RUNTIME"


@dataclass(frozen=True)
class RuntimePermit:
    """One pre-start decision derived from the run's absolute deadline."""

    allowed: bool
    operation: str
    remaining_runtime_seconds: float
    cleanup_reserve_seconds: float
    configured_timeout_seconds: float
    effective_timeout_seconds: float
    minimum_runtime_seconds: float
    reason_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "operation": self.operation,
            "remaining_runtime_seconds": self.remaining_runtime_seconds,
            "cleanup_reserve_seconds": self.cleanup_reserve_seconds,
            "configured_timeout_seconds": self.configured_timeout_seconds,
            "effective_timeout_seconds": self.effective_timeout_seconds,
            "minimum_runtime_seconds": self.minimum_runtime_seconds,
            "reason_code": self.reason_code,
        }


class RuntimeUnavailable(RuntimeError):
    """Raised before an action is reserved when no safe runtime window remains."""

    def __init__(self, permit: RuntimePermit) -> None:
        self.permit = permit
        reason = permit.reason_code or TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME
        super().__init__(
            f"{reason}: operation={permit.operation}, "
            f"remaining_runtime_seconds={permit.remaining_runtime_seconds:.6f}, "
            f"cleanup_reserve_seconds={permit.cleanup_reserve_seconds:.6f}, "
            f"effective_timeout_seconds={permit.effective_timeout_seconds:.6f}"
        )

    @property
    def reason_code(self) -> str:
        return (
            self.permit.reason_code
            or TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME
        )


@dataclass(frozen=True)
class RuntimeDeadline:
    """One process-shared absolute deadline using the monotonic clock."""

    run_deadline_monotonic: float
    cleanup_reserve_seconds: float = DEFAULT_CLEANUP_RESERVE_SECONDS
    cosim_minimum_runtime_seconds: float = (
        DEFAULT_COSIM_MINIMUM_RUNTIME_SECONDS
    )
    monotonic: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        deadline = float(self.run_deadline_monotonic)
        cleanup = float(self.cleanup_reserve_seconds)
        cosim_minimum = float(self.cosim_minimum_runtime_seconds)
        if not math.isfinite(deadline):
            raise ValueError("run_deadline_monotonic must be finite")
        if not math.isfinite(cleanup) or cleanup < 0:
            raise ValueError(
                "cleanup_reserve_seconds must be finite and non-negative"
            )
        if not math.isfinite(cosim_minimum) or cosim_minimum < 0:
            raise ValueError(
                "cosim_minimum_runtime_seconds must be finite and non-negative"
            )
        object.__setattr__(self, "run_deadline_monotonic", deadline)
        object.__setattr__(self, "cleanup_reserve_seconds", cleanup)
        object.__setattr__(
            self,
            "cosim_minimum_runtime_seconds",
            cosim_minimum,
        )

    @classmethod
    def from_duration(
        cls,
        duration_seconds: float,
        *,
        cleanup_reserve_seconds: float = DEFAULT_CLEANUP_RESERVE_SECONDS,
        cosim_minimum_runtime_seconds: float = (
            DEFAULT_COSIM_MINIMUM_RUNTIME_SECONDS
        ),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "RuntimeDeadline":
        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration_seconds must be finite and positive")
        return cls(
            run_deadline_monotonic=float(monotonic()) + duration,
            cleanup_reserve_seconds=cleanup_reserve_seconds,
            cosim_minimum_runtime_seconds=cosim_minimum_runtime_seconds,
            monotonic=monotonic,
        )

    def remaining_runtime_seconds(self) -> float:
        return max(
            0.0,
            self.run_deadline_monotonic - float(self.monotonic()),
        )

    def permit(
        self,
        configured_timeout_seconds: float,
        *,
        operation: str,
        final_closure: bool = False,
        minimum_runtime_seconds: float | None = None,
    ) -> RuntimePermit:
        configured = float(configured_timeout_seconds)
        if not math.isfinite(configured) or configured <= 0:
            raise ValueError(
                "configured_timeout_seconds must be finite and positive"
            )
        normalized_operation = str(operation).strip().casefold()
        if not normalized_operation:
            raise ValueError("operation must not be empty")
        remaining = self.remaining_runtime_seconds()
        available = max(0.0, remaining - self.cleanup_reserve_seconds)
        effective = min(configured, available)
        minimum = (
            min(configured, self.cosim_minimum_runtime_seconds)
            if minimum_runtime_seconds is None
            and normalized_operation == "cosim"
            else 0.0
            if minimum_runtime_seconds is None
            else float(minimum_runtime_seconds)
        )
        if not math.isfinite(minimum) or minimum < 0:
            raise ValueError(
                "minimum_runtime_seconds must be finite and non-negative"
            )
        allowed = (
            remaining > self.cleanup_reserve_seconds
            and effective > 0
            and effective >= minimum
        )
        reason: str | None = None
        if not allowed:
            if final_closure:
                reason = FINAL_CLOSURE_UNAFFORDABLE_RUNTIME
            elif normalized_operation == "cosim":
                reason = COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME
            else:
                reason = TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME
        return RuntimePermit(
            allowed=allowed,
            operation=normalized_operation,
            remaining_runtime_seconds=remaining,
            cleanup_reserve_seconds=self.cleanup_reserve_seconds,
            configured_timeout_seconds=configured,
            effective_timeout_seconds=effective,
            minimum_runtime_seconds=minimum,
            reason_code=reason,
        )

    def require(
        self,
        configured_timeout_seconds: float,
        *,
        operation: str,
        final_closure: bool = False,
        minimum_runtime_seconds: float | None = None,
    ) -> RuntimePermit:
        permit = self.permit(
            configured_timeout_seconds,
            operation=operation,
            final_closure=final_closure,
            minimum_runtime_seconds=minimum_runtime_seconds,
        )
        if not permit.allowed:
            raise RuntimeUnavailable(permit)
        return permit
