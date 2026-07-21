"""Append-only, idempotent and process-safe accounting for charged actions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Sequence


class BudgetError(RuntimeError):
    """Base class for budget and ledger failures."""


class BudgetExceeded(BudgetError):
    """Raised before execution when a configured budget is insufficient."""


class BudgetLedgerError(BudgetError):
    """Raised when an existing ledger is corrupt or incompatible."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class BudgetConfig:
    credit_limit: int | None
    costs: Mapping[str, int]
    tool_limits: Mapping[str, int | None]
    token_limit: int
    runtime_limit_seconds: float

    def __post_init__(self) -> None:
        credit_limit = None if self.credit_limit is None else int(self.credit_limit)
        token_limit = int(self.token_limit)
        runtime_limit = float(self.runtime_limit_seconds)
        costs = {str(kind): int(cost) for kind, cost in self.costs.items()}
        limits = {
            str(kind): (None if limit is None else int(limit))
            for kind, limit in self.tool_limits.items()
        }
        if credit_limit is not None and credit_limit < 0:
            raise ValueError("credit_limit must be non-negative or None")
        if any(cost < 0 for cost in costs.values()):
            raise ValueError("tool costs must be non-negative")
        if any(limit is not None and limit < 0 for limit in limits.values()):
            raise ValueError("tool limits must be non-negative or None")
        if set(costs) != set(limits):
            raise ValueError("costs and tool_limits must contain the same tools")
        if token_limit < 0:
            raise ValueError("token_limit must be non-negative")
        if not math.isfinite(runtime_limit) or runtime_limit <= 0:
            raise ValueError("runtime_limit_seconds must be finite and positive")
        object.__setattr__(self, "credit_limit", credit_limit)
        object.__setattr__(self, "token_limit", token_limit)
        object.__setattr__(self, "runtime_limit_seconds", runtime_limit)
        object.__setattr__(self, "costs", MappingProxyType(costs))
        object.__setattr__(self, "tool_limits", MappingProxyType(limits))

    def to_dict(self) -> dict[str, object]:
        return {
            "credit_limit": self.credit_limit,
            "costs": dict(self.costs),
            "tool_limits": dict(self.tool_limits),
            "token_limit": self.token_limit,
            "runtime_limit_seconds": self.runtime_limit_seconds,
        }

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode()).hexdigest()

    @property
    def run_token_limit(self) -> int:
        """Unambiguous name used by the V3 token policy.

        ``token_limit`` remains the serialized V0--V3-D compatibility field;
        its accounting meaning is unchanged.
        """

        return self.token_limit


TOKEN_ENVELOPE_SCHEMA = "v3.token-envelope.v1"
TOKEN_POLICY_VERSION = "v3.token-policy.v1"
TOKEN_PRESSURES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
TOKEN_POLICY_MODES = frozenset(
    {"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"}
)


@dataclass(frozen=True)
class TokenEstimate:
    """Secret-free, component-level estimate for one bounded request."""

    total_tokens: int
    component_tokens: Mapping[str, int]
    estimator_name: str
    estimator_version: str
    error_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        components = {str(name): int(value) for name, value in self.component_tokens.items()}
        if self.total_tokens < 0 or any(value < 0 for value in components.values()):
            raise ValueError("token estimates must be non-negative")
        if sum(components.values()) != self.total_tokens:
            raise ValueError("component token estimates must sum to total_tokens")
        if not self.estimator_name or not self.estimator_version:
            raise ValueError("token estimator identity must not be empty")
        object.__setattr__(self, "component_tokens", MappingProxyType(components))
        object.__setattr__(
            self,
            "error_sources",
            tuple(dict.fromkeys(str(item) for item in self.error_sources if str(item))),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "total_tokens": self.total_tokens,
            "component_tokens": dict(self.component_tokens),
            "estimator_name": self.estimator_name,
            "estimator_version": self.estimator_version,
            "error_sources": list(self.error_sources),
        }


class TokenEstimator:
    """Estimate bounded Planner input without retaining its contents.

    A caller may inject a model tokenizer.  When no tokenizer is available the
    deterministic fallback charges one Unicode code point per token.  That is
    intentionally conservative for the OpenAI-compatible models used by this
    project and makes its error direction explicit rather than pretending that
    byte or character counts are actual provider usage.
    """

    FALLBACK_NAME = "unicode-codepoint-upper-bound"
    FALLBACK_VERSION = "v1"

    def __init__(
        self,
        *,
        tokenizer: Callable[[str], object] | None = None,
        estimator_name: str | None = None,
        estimator_version: str = "v1",
        error_sources: Sequence[str] = (),
    ) -> None:
        self._tokenizer = tokenizer
        self.estimator_name = (
            str(estimator_name)
            if estimator_name
            else self.FALLBACK_NAME if tokenizer is None else "injected-tokenizer"
        )
        self.estimator_version = str(estimator_version)
        self.error_sources = tuple(str(item) for item in error_sources)

    @classmethod
    def for_model(cls, model: str) -> "TokenEstimator":
        """Use tiktoken when installed and known; otherwise fail open safely."""

        try:
            import tiktoken  # type: ignore[import-not-found]

            try:
                encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                encoding = tiktoken.get_encoding("cl100k_base")
                return cls(
                    tokenizer=encoding.encode,
                    estimator_name="tiktoken-cl100k-base-fallback",
                    estimator_version=str(getattr(tiktoken, "__version__", "unknown")),
                    error_sources=("MODEL_ENCODING_UNKNOWN",),
                )
            return cls(
                tokenizer=encoding.encode,
                estimator_name=f"tiktoken:{getattr(encoding, 'name', 'model')}",
                estimator_version=str(getattr(tiktoken, "__version__", "unknown")),
            )
        except (ImportError, ModuleNotFoundError):
            return cls(error_sources=(f"TOKENIZER_UNAVAILABLE_FOR:{model}",))

    @staticmethod
    def _render(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return _canonical_json(value)

    def estimate_text(self, value: object) -> int:
        rendered = self._render(value)
        if not rendered:
            return 0
        if self._tokenizer is None:
            return len(rendered)
        encoded = self._tokenizer(rendered)
        if isinstance(encoded, bool):
            raise ValueError("tokenizer returned an invalid boolean")
        if isinstance(encoded, int):
            if encoded < 0:
                raise ValueError("tokenizer returned a negative count")
            return encoded
        try:
            return len(encoded)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("tokenizer result has no length") from exc

    def estimate_components(self, components: Mapping[str, object]) -> TokenEstimate:
        counts = {
            str(name): self.estimate_text(value)
            for name, value in sorted(components.items(), key=lambda item: str(item[0]))
        }
        return TokenEstimate(
            total_tokens=sum(counts.values()),
            component_tokens=counts,
            estimator_name=self.estimator_name,
            estimator_version=self.estimator_version,
            error_sources=self.error_sources,
        )


def _default_mode_output_caps() -> dict[str, int]:
    return {
        "REPAIR": 1400,
        "SYNTH_FIX": 1800,
        "STRUCTURAL_FIX": 2200,
        "OPTIMIZE": 2400,
    }


def _default_mode_minimums() -> dict[str, int]:
    return {
        "REPAIR": 700,
        "SYNTH_FIX": 800,
        "STRUCTURAL_FIX": 1000,
        "OPTIMIZE": 1000,
    }


@dataclass(frozen=True)
class TokenBudgetLimits:
    """Configurable soft allocation limits; hard run limits stay in Ledger."""

    mode_output_caps: Mapping[str, int] = field(default_factory=_default_mode_output_caps)
    mode_minimum_viable_output: Mapping[str, int] = field(
        default_factory=_default_mode_minimums
    )
    configured_max_output_tokens: int | None = None
    minimum_viable_output_tokens: int | None = None
    provider_hard_output_cap: int = 4096
    context_window_tokens: int = 32768
    context_safety_margin_tokens: int = 256
    token_budget_safety_margin: int = 128
    future_round_token_reserve: int = 1800
    final_token_reserve: int = 0
    configured_guidance_cap: int = 600
    guidance_ratio: float = 0.15

    def __post_init__(self) -> None:
        caps = {str(mode): int(value) for mode, value in self.mode_output_caps.items()}
        minimums = {
            str(mode): int(value)
            for mode, value in self.mode_minimum_viable_output.items()
        }
        if set(caps) != TOKEN_POLICY_MODES or set(minimums) != TOKEN_POLICY_MODES:
            raise ValueError("mode token limits must cover all Planner modes")
        integers = {
            "provider_hard_output_cap": self.provider_hard_output_cap,
            "context_window_tokens": self.context_window_tokens,
            "context_safety_margin_tokens": self.context_safety_margin_tokens,
            "token_budget_safety_margin": self.token_budget_safety_margin,
            "future_round_token_reserve": self.future_round_token_reserve,
            "final_token_reserve": self.final_token_reserve,
            "configured_guidance_cap": self.configured_guidance_cap,
        }
        if any(isinstance(value, bool) or int(value) < 0 for value in integers.values()):
            raise ValueError("token policy limits must be non-negative integers")
        if self.provider_hard_output_cap <= 0 or self.context_window_tokens <= 0:
            raise ValueError("provider and context limits must be positive")
        if any(value <= 0 for value in (*caps.values(), *minimums.values())):
            raise ValueError("mode output limits must be positive")
        if self.configured_max_output_tokens is not None and (
            isinstance(self.configured_max_output_tokens, bool)
            or self.configured_max_output_tokens <= 0
        ):
            raise ValueError("configured_max_output_tokens must be positive")
        if self.minimum_viable_output_tokens is not None and (
            isinstance(self.minimum_viable_output_tokens, bool)
            or self.minimum_viable_output_tokens <= 0
        ):
            raise ValueError("minimum_viable_output_tokens must be positive")
        ratio = float(self.guidance_ratio)
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            raise ValueError("guidance_ratio must be in [0,1]")
        object.__setattr__(self, "mode_output_caps", MappingProxyType(caps))
        object.__setattr__(
            self, "mode_minimum_viable_output", MappingProxyType(minimums)
        )


@dataclass(frozen=True)
class TokenEnvelope:
    """Immutable, hash-stable quota for exactly one Planner request."""

    run_token_limit: int
    tokens_used: int
    tokens_remaining: int
    estimated_base_prompt_tokens: int
    estimated_guidance_tokens: int
    estimated_input_tokens: int
    configured_max_output_tokens: int
    effective_max_output_tokens: int
    context_window_tokens: int
    context_safety_margin_tokens: int
    future_round_token_reserve: int
    final_token_reserve: int
    guidance_token_cap: int
    rounds_remaining: int
    minimum_viable_output_tokens: int
    provider_hard_output_cap: int
    token_budget_safety_margin: int
    token_pressure: str
    planner_call_allowed: bool
    policy_version: str = TOKEN_POLICY_VERSION
    reason_codes: tuple[str, ...] = ()
    estimator_name: str = TokenEstimator.FALLBACK_NAME
    estimator_version: str = TokenEstimator.FALLBACK_VERSION
    estimator_error_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in self.to_dict(include_schema=False).items():
            if name in {
                "token_pressure",
                "planner_call_allowed",
                "policy_version",
                "reason_codes",
                "estimator_name",
                "estimator_version",
                "estimator_error_sources",
            }:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"TokenEnvelope {name} must be non-negative")
        if self.token_pressure not in TOKEN_PRESSURES:
            raise ValueError("TokenEnvelope token_pressure is invalid")
        if not isinstance(self.planner_call_allowed, bool):
            raise ValueError("planner_call_allowed must be boolean")
        object.__setattr__(
            self,
            "reason_codes",
            tuple(dict.fromkeys(str(item) for item in self.reason_codes if str(item))),
        )
        object.__setattr__(
            self,
            "estimator_error_sources",
            tuple(
                dict.fromkeys(
                    str(item) for item in self.estimator_error_sources if str(item)
                )
            ),
        )

    def to_dict(self, *, include_schema: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "run_token_limit": self.run_token_limit,
            "tokens_used": self.tokens_used,
            "tokens_remaining": self.tokens_remaining,
            "estimated_base_prompt_tokens": self.estimated_base_prompt_tokens,
            "estimated_guidance_tokens": self.estimated_guidance_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "configured_max_output_tokens": self.configured_max_output_tokens,
            "effective_max_output_tokens": self.effective_max_output_tokens,
            "context_window_tokens": self.context_window_tokens,
            "context_safety_margin_tokens": self.context_safety_margin_tokens,
            "future_round_token_reserve": self.future_round_token_reserve,
            "final_token_reserve": self.final_token_reserve,
            "guidance_token_cap": self.guidance_token_cap,
            "rounds_remaining": self.rounds_remaining,
            "minimum_viable_output_tokens": self.minimum_viable_output_tokens,
            "provider_hard_output_cap": self.provider_hard_output_cap,
            "token_budget_safety_margin": self.token_budget_safety_margin,
            "token_pressure": self.token_pressure,
            "planner_call_allowed": self.planner_call_allowed,
            "policy_version": self.policy_version,
            "reason_codes": list(self.reason_codes),
            "estimator_name": self.estimator_name,
            "estimator_version": self.estimator_version,
            "estimator_error_sources": list(self.estimator_error_sources),
        }
        return {"schema_version": TOKEN_ENVELOPE_SCHEMA, **value} if include_schema else value

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def stable_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TokenEnvelope":
        if value.get("schema_version") != TOKEN_ENVELOPE_SCHEMA:
            raise ValueError("unsupported TokenEnvelope schema")
        expected = set(cls.__dataclass_fields__) | {"schema_version"}
        if set(value) != expected:
            raise ValueError("TokenEnvelope fields mismatch")
        payload = {name: value[name] for name in cls.__dataclass_fields__}
        payload["reason_codes"] = tuple(payload["reason_codes"])
        payload["estimator_error_sources"] = tuple(
            payload["estimator_error_sources"]
        )
        return cls(**payload)  # type: ignore[arg-type]


def validate_token_envelope(value: Mapping[str, object]) -> dict[str, object]:
    return TokenEnvelope.from_dict(value).to_dict()


class TokenBudgetPolicy:
    """Compute one bounded Planner quota from an authoritative Ledger snapshot."""

    def __init__(self, limits: TokenBudgetLimits | None = None) -> None:
        self.limits = limits or TokenBudgetLimits()

    def allocate(
        self,
        *,
        budget_snapshot: Mapping[str, object],
        mode: str,
        estimated_base_prompt_tokens: int,
        estimated_guidance_tokens: int,
        estimated_input_tokens: int,
        rounds_remaining: int,
        estimator: TokenEstimate | None = None,
        existing_budget_gate_allowed: bool = True,
        guidance_allowed: bool = True,
    ) -> TokenEnvelope:
        if mode not in TOKEN_POLICY_MODES:
            raise ValueError("unsupported TokenBudgetPolicy mode")
        numbers = {
            "estimated_base_prompt_tokens": estimated_base_prompt_tokens,
            "estimated_guidance_tokens": estimated_guidance_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "rounds_remaining": rounds_remaining,
        }
        if any(isinstance(value, bool) or int(value) < 0 for value in numbers.values()):
            raise ValueError("token policy inputs must be non-negative integers")
        run_limit_raw = budget_snapshot.get(
            "run_token_limit", budget_snapshot.get("token_limit")
        )
        used_raw = budget_snapshot.get("tokens_used")
        remaining_raw = budget_snapshot.get("tokens_remaining")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (run_limit_raw, used_raw, remaining_raw)
        ):
            raise ValueError("budget snapshot has invalid token counters")
        run_limit = int(run_limit_raw)
        tokens_used = int(used_raw)
        tokens_remaining = min(int(remaining_raw), max(0, run_limit - tokens_used))
        configured = int(self.limits.mode_output_caps[mode])
        if self.limits.configured_max_output_tokens is not None:
            configured = min(configured, self.limits.configured_max_output_tokens)
        minimum = int(self.limits.mode_minimum_viable_output[mode])
        if self.limits.minimum_viable_output_tokens is not None:
            minimum = int(self.limits.minimum_viable_output_tokens)
        future_reserve = (
            self.limits.future_round_token_reserve
            if rounds_remaining > 1
            else 0
        )
        remaining_after_input_and_reserve = max(
            0,
            tokens_remaining
            - int(estimated_input_tokens)
            - future_reserve
            - self.limits.final_token_reserve
            - self.limits.token_budget_safety_margin,
        )
        context_available_for_output = max(
            0,
            self.limits.context_window_tokens
            - int(estimated_input_tokens)
            - self.limits.context_safety_margin_tokens,
        )
        effective = max(
            0,
            min(
                configured,
                self.limits.provider_hard_output_cap,
                remaining_after_input_and_reserve,
                context_available_for_output,
            ),
        )
        reasons: list[str] = []
        if not existing_budget_gate_allowed:
            reasons.append("EXISTING_BUDGET_GATE_BLOCKED")
        if estimated_input_tokens + self.limits.context_safety_margin_tokens >= self.limits.context_window_tokens:
            reasons.append("INPUT_EXCEEDS_CONTEXT_WINDOW")
        if effective < minimum:
            reasons.append("BELOW_MINIMUM_VIABLE_OUTPUT")
        if tokens_remaining <= self.limits.final_token_reserve:
            reasons.append("ONLY_FINAL_TOKEN_RESERVE_REMAINS")

        future_need = max(0, rounds_remaining - 1) * minimum
        available_after_fixed = max(
            0,
            tokens_remaining
            - estimated_input_tokens
            - self.limits.final_token_reserve
            - self.limits.token_budget_safety_margin,
        )
        planner_allowed = not reasons and effective >= minimum
        if not planner_allowed:
            pressure = "CRITICAL"
        elif effective < max(minimum + 1, configured // 2) or available_after_fixed < minimum + future_need:
            pressure = "HIGH"
            reasons.append("TIGHT_OUTPUT_OR_FUTURE_ROUND_HEADROOM")
        elif effective < configured or available_after_fixed < configured + future_need:
            pressure = "MEDIUM"
            reasons.append("OUTPUT_CAP_REDUCED_TO_PRESERVE_BUDGET")
        else:
            pressure = "LOW"

        available_context_budget = max(
            0,
            self.limits.context_window_tokens
            - estimated_base_prompt_tokens
            - self.limits.context_safety_margin_tokens
            - minimum,
        )
        available_run_budget = max(
            0,
            tokens_remaining
            - estimated_base_prompt_tokens
            - future_reserve
            - self.limits.final_token_reserve
            - self.limits.token_budget_safety_margin
            - minimum,
        )
        guidance_cap = min(
            self.limits.configured_guidance_cap,
            int(available_context_budget * self.limits.guidance_ratio),
            int(available_run_budget * self.limits.guidance_ratio),
        )
        if pressure == "MEDIUM":
            guidance_cap //= 2
        elif pressure == "HIGH":
            guidance_cap //= 4
        elif pressure == "CRITICAL":
            guidance_cap = 0
        if not guidance_allowed:
            guidance_cap = 0
            reasons.append("GUIDANCE_NOT_ACTIONABLE_OR_DISABLED")
        guidance_cap = max(0, guidance_cap)

        estimate = estimator or TokenEstimate(
            total_tokens=estimated_input_tokens,
            component_tokens={"request": estimated_input_tokens},
            estimator_name=TokenEstimator.FALLBACK_NAME,
            estimator_version=TokenEstimator.FALLBACK_VERSION,
        )
        return TokenEnvelope(
            run_token_limit=run_limit,
            tokens_used=tokens_used,
            tokens_remaining=tokens_remaining,
            estimated_base_prompt_tokens=int(estimated_base_prompt_tokens),
            estimated_guidance_tokens=int(estimated_guidance_tokens),
            estimated_input_tokens=int(estimated_input_tokens),
            configured_max_output_tokens=configured,
            effective_max_output_tokens=effective,
            context_window_tokens=self.limits.context_window_tokens,
            context_safety_margin_tokens=self.limits.context_safety_margin_tokens,
            future_round_token_reserve=future_reserve,
            final_token_reserve=self.limits.final_token_reserve,
            guidance_token_cap=guidance_cap,
            rounds_remaining=int(rounds_remaining),
            minimum_viable_output_tokens=minimum,
            provider_hard_output_cap=self.limits.provider_hard_output_cap,
            token_budget_safety_margin=self.limits.token_budget_safety_margin,
            token_pressure=pressure,
            planner_call_allowed=planner_allowed,
            reason_codes=tuple(reasons),
            estimator_name=estimate.estimator_name,
            estimator_version=estimate.estimator_version,
            estimator_error_sources=estimate.error_sources,
        )


class BudgetLedger:
    """Authoritative JSONL accounting for one run directory.

    The sidecar lock makes the check-and-append transition atomic across
    processes.  A crash may leave only an incomplete final JSON fragment; on
    the next open that fragment is discarded while the durable prefix remains.
    """

    def __init__(self, path: str | Path, config: BudgetConfig) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            events = self._read_events_unlocked()
            if not events:
                self._append_unlocked(
                    {
                        "state": "INITIALIZED",
                        "timestamp": _utc_now(),
                        "epoch_seconds": time.time(),
                        "config_hash": config.config_hash,
                        "config": config.to_dict(),
                    }
                )
            else:
                first = events[0]
                if first.get("state") != "INITIALIZED":
                    raise BudgetLedgerError("ledger does not start with INITIALIZED")
                if first.get("config_hash") != config.config_hash:
                    raise BudgetLedgerError(
                        "budget configuration changed for an existing run"
                    )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _repair_torn_tail_unlocked(self) -> None:
        if not self.path.exists():
            return
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise BudgetLedgerError(f"cannot read budget ledger: {exc}") from exc
        if not data or data.endswith(b"\n"):
            return
        start = data.rfind(b"\n") + 1
        tail = data[start:]
        try:
            value = json.loads(tail.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("ledger event is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            try:
                with self.path.open("r+b") as stream:
                    stream.truncate(start)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise BudgetLedgerError(f"cannot repair budget ledger: {exc}") from exc
        else:
            try:
                with self.path.open("ab") as stream:
                    stream.write(b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise BudgetLedgerError(f"cannot finish budget ledger line: {exc}") from exc

    def _read_events_unlocked(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        parsed: list[dict[str, object]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("ledger event is not an object")
                if value.get("sequence") != len(parsed):
                    raise ValueError("ledger sequence is not contiguous")
                parsed.append(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BudgetLedgerError(f"cannot read budget ledger: {exc}") from exc
        return parsed

    def _append_unlocked(self, event: dict[str, object]) -> None:
        sequence = len(self._read_events_unlocked())
        value = {"sequence": sequence, **event}
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical_json(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise BudgetLedgerError(f"cannot append budget ledger: {exc}") from exc

    def events(self) -> list[dict[str, object]]:
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            return self._read_events_unlocked()

    @staticmethod
    def _actions_from(
        events: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        actions: dict[str, list[dict[str, object]]] = {}
        for event in events[1:]:
            action_id = event.get("action_id")
            if isinstance(action_id, str):
                actions.setdefault(action_id, []).append(event)
        return actions

    def action_events(self, action_id: str) -> list[dict[str, object]]:
        return self._actions_from(self.events()).get(action_id, [])

    def completed_event(self, action_id: str) -> dict[str, object] | None:
        for event in reversed(self.action_events(action_id)):
            if event.get("state") == "COMPLETED":
                return event
        return None

    def completed_result_ref(self, action_id: str) -> str | None:
        event = self.completed_event(action_id)
        result_ref = event.get("result_ref") if event is not None else None
        return result_ref if isinstance(result_ref, str) else None

    def has_pending(self, action_id: str) -> bool:
        events = self.action_events(action_id)
        return bool(events) and events[-1].get("state") == "STARTED"

    def is_ambiguous(self, action_id: str) -> bool:
        events = self.action_events(action_id)
        return bool(events) and events[-1].get("state") == "AMBIGUOUS"

    def cost(self, kind: str) -> int:
        try:
            return self.config.costs[kind]
        except KeyError as exc:
            raise BudgetError(f"unknown charged tool: {kind}") from exc

    def _snapshot_from(self, events: list[dict[str, object]]) -> dict[str, object]:
        if not events:
            raise BudgetLedgerError("budget ledger is empty")
        initialized = events[0]
        actions = self._actions_from(events)
        credits_used = 0
        pending_credits = 0
        pending_tokens = 0
        tool_used = {kind: 0 for kind in self.config.costs}
        tool_pending = {kind: 0 for kind in self.config.costs}
        tokens_used = 0
        input_tokens_used = 0
        output_tokens_used = 0
        cached_input_tokens_used = 0
        token_usage_complete = True
        for action_events in actions.values():
            started = next(
                (event for event in action_events if event.get("state") == "STARTED"),
                None,
            )
            if started is None:
                continue
            terminal = next(
                (
                    event
                    for event in reversed(action_events)
                    if event.get("state") in {"COMPLETED", "AMBIGUOUS"}
                ),
                None,
            )
            kind = str(started["kind"])
            if kind not in tool_used:
                raise BudgetLedgerError(f"ledger contains unknown tool: {kind}")
            expected_cost = int(self.config.costs[kind])
            if started.get("estimated_cost") != expected_cost:
                raise BudgetLedgerError(
                    f"ledger STARTED cost does not match config for {kind}"
                )
            if terminal is None:
                pending_credits += expected_cost
                pending_tokens += int(started.get("estimated_tokens", 0))
                tool_pending[kind] += 1
            else:
                if terminal.get("actual_cost") != expected_cost:
                    raise BudgetLedgerError(
                        f"ledger terminal cost does not match config for {kind}"
                    )
                credits_used += expected_cost
                tool_used[kind] += 1
                tokens_used += int(terminal.get("tokens_used", 0))
                input_tokens_used += int(terminal.get("input_tokens", 0))
                output_tokens_used += int(terminal.get("output_tokens", 0))
                cached_input_tokens_used += int(terminal.get("cached_input_tokens", 0))
                if kind == "llm" and (
                    "input_tokens" not in terminal or "output_tokens" not in terminal
                ):
                    token_usage_complete = False
        start_epoch = float(initialized.get("epoch_seconds", time.time()))
        runtime_used = max(0.0, time.time() - start_epoch)
        return {
            "credit_limit": self.config.credit_limit,
            "credits_used": credits_used,
            "pending_credits_reserved": pending_credits,
            "credits_remaining": (
                None
                if self.config.credit_limit is None
                else self.config.credit_limit - credits_used - pending_credits
            ),
            "tool_costs": dict(self.config.costs),
            "tool_limits": dict(self.config.tool_limits),
            "tool_used": tool_used,
            "tool_pending": tool_pending,
            "run_token_limit": self.config.run_token_limit,
            "token_limit": self.config.token_limit,
            "pending_tokens_reserved": pending_tokens,
            "tokens_used": tokens_used,
            "input_tokens_used": input_tokens_used,
            "output_tokens_used": output_tokens_used,
            "cached_input_tokens_used": cached_input_tokens_used,
            "token_usage_complete": token_usage_complete,
            "tokens_remaining": self.config.token_limit - tokens_used - pending_tokens,
            "runtime_limit_seconds": self.config.runtime_limit_seconds,
            "runtime_used_seconds": runtime_used,
            "runtime_remaining_seconds": max(
                0.0, self.config.runtime_limit_seconds - runtime_used
            ),
            "config_hash": self.config.config_hash,
        }

    def snapshot(self) -> dict[str, object]:
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            return self._snapshot_from(self._read_events_unlocked())

    def remaining_runtime_seconds(self) -> float:
        return float(self.snapshot()["runtime_remaining_seconds"])

    def reserve(
        self,
        *,
        action_id: str,
        kind: str,
        candidate_id: str,
        code_hash: str,
        tool_config_hash: str,
        estimated_tokens: int = 0,
    ) -> None:
        token_reservation = int(estimated_tokens)
        if isinstance(estimated_tokens, bool) or token_reservation < 0:
            raise BudgetExceeded("estimated token reservation must be non-negative")
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            events = self._read_events_unlocked()
            actions = self._actions_from(events)
            if actions.get(action_id):
                raise BudgetLedgerError(f"action already exists in ledger: {action_id}")
            cost = self.cost(kind)
            snapshot = self._snapshot_from(events)
            remaining = snapshot["credits_remaining"]
            if remaining is not None and int(remaining) < cost:
                raise BudgetExceeded(
                    f"{kind} costs {cost} but only {remaining} credits remain"
                )
            if int(snapshot["tokens_remaining"]) < token_reservation:
                raise BudgetExceeded(
                    f"action reserves {token_reservation} tokens but only "
                    f"{snapshot['tokens_remaining']} remain"
                )
            limit = self.config.tool_limits[kind]
            used = int(snapshot["tool_used"][kind]) + int(  # type: ignore[index]
                snapshot["tool_pending"][kind]  # type: ignore[index]
            )
            if limit is not None and used >= limit:
                raise BudgetExceeded(f"{kind} call limit {limit} is exhausted")
            if float(snapshot["runtime_remaining_seconds"]) <= 0:
                raise BudgetExceeded("run time budget is exhausted")
            self._append_unlocked(
                {
                    "state": "STARTED",
                    "timestamp": _utc_now(),
                    "action_id": action_id,
                    "kind": kind,
                    "candidate_id": candidate_id,
                    "code_hash": code_hash,
                    "tool_config_hash": tool_config_hash,
                    "estimated_cost": cost,
                    "estimated_tokens": token_reservation,
                }
            )

    def complete(
        self,
        *,
        action_id: str,
        result_ref: str,
        result_sha256: str,
        elapsed_s: float,
        tokens_used: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
    ) -> None:
        token_count = int(tokens_used)
        input_count = int(input_tokens)
        output_count = int(output_tokens)
        cached_input_count = int(cached_input_tokens)
        elapsed = float(elapsed_s)
        if token_count < 0 or input_count < 0 or output_count < 0 or cached_input_count < 0:
            raise BudgetExceeded("token counts cannot be negative")
        if input_count + output_count not in {0, token_count}:
            raise BudgetLedgerError("input_tokens + output_tokens must equal tokens_used")
        if not math.isfinite(elapsed) or elapsed < 0:
            raise BudgetLedgerError("elapsed_s must be finite and non-negative")
        if (
            len(result_sha256) != 64
            or any(character not in "0123456789abcdef" for character in result_sha256)
        ):
            raise BudgetLedgerError("result_sha256 must be a lowercase SHA-256 digest")
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            events = self._read_events_unlocked()
            action_events = self._actions_from(events).get(action_id, [])
            if not action_events or action_events[-1].get("state") != "STARTED":
                raise BudgetLedgerError(f"action is not pending: {action_id}")
            snapshot = self._snapshot_from(events)
            started = action_events[-1]
            estimated_tokens = int(started.get("estimated_tokens", 0))
            other_pending = int(snapshot["pending_tokens_reserved"]) - estimated_tokens
            total_after = int(snapshot["tokens_used"]) + other_pending + token_count
            reservation_overrun = (
                estimated_tokens > 0 and token_count > estimated_tokens
            )
            total_overrun = total_after > self.config.token_limit
            if estimated_tokens == 0 and total_overrun:
                raise BudgetExceeded("token budget would be exceeded")
            completed_event = {
                    "state": "COMPLETED",
                    "timestamp": _utc_now(),
                    "action_id": action_id,
                    "kind": started["kind"],
                    "actual_cost": int(started["estimated_cost"]),
                    "tokens_used": token_count,
                    "input_tokens": input_count,
                    "output_tokens": output_count,
                    "cached_input_tokens": cached_input_count,
                    "elapsed_s": elapsed,
                    "result_ref": result_ref,
                    "result_sha256": result_sha256,
                }
            if reservation_overrun or total_overrun:
                completed_event["token_reservation_overrun"] = True
            self._append_unlocked(completed_event)
            if reservation_overrun or total_overrun:
                raise BudgetExceeded(
                    "actual provider token usage exceeded the durable reservation"
                )

    def mark_ambiguous(self, action_id: str) -> None:
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            events = self._read_events_unlocked()
            action_events = self._actions_from(events).get(action_id, [])
            if not action_events or action_events[-1].get("state") != "STARTED":
                raise BudgetLedgerError(f"action is not pending: {action_id}")
            started = action_events[-1]
            self._append_unlocked(
                {
                    "state": "AMBIGUOUS",
                    "timestamp": _utc_now(),
                    "action_id": action_id,
                    "kind": started["kind"],
                    "actual_cost": int(started["estimated_cost"]),
                    "tokens_used": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "reason": "STARTED action had no durable result",
                }
            )

    def mark_ambiguous_conservative(
        self,
        action_id: str,
        *,
        reason: str = "non-replayable action has no durable result",
    ) -> None:
        """Close an ambiguous action while conservatively charging its reserve.

        This is intended for external model calls for which dispatch may have
        happened but neither a durable response nor exact usage exists.  The
        reserved token upper bound remains consumed, and the intentionally
        absent input/output token fields make ``token_usage_complete`` false.
        Existing ``mark_ambiguous`` behavior is preserved for V0--V2 callers.
        """

        if not isinstance(reason, str) or not reason.strip():
            raise BudgetLedgerError("ambiguous action reason must not be empty")
        with self._exclusive():
            self._repair_torn_tail_unlocked()
            events = self._read_events_unlocked()
            action_events = self._actions_from(events).get(action_id, [])
            if not action_events or action_events[-1].get("state") != "STARTED":
                raise BudgetLedgerError(f"action is not pending: {action_id}")
            started = action_events[-1]
            reserved_tokens = int(started.get("estimated_tokens", 0))
            self._append_unlocked(
                {
                    "state": "AMBIGUOUS",
                    "timestamp": _utc_now(),
                    "action_id": action_id,
                    "kind": started["kind"],
                    "actual_cost": int(started["estimated_cost"]),
                    "tokens_used": reserved_tokens,
                    "reason": reason.strip(),
                    "token_accounting": "CONSERVATIVE_RESERVATION",
                }
            )

    def write_snapshot(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(self.snapshot(), indent=2, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            raise BudgetLedgerError(f"cannot write budget snapshot: {exc}") from exc
