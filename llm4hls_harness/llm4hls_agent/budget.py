"""Append-only, idempotent and process-safe accounting for charged actions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping


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
            if terminal is None:
                pending_credits += int(started["estimated_cost"])
                tool_pending[kind] += 1
            else:
                credits_used += int(terminal["actual_cost"])
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
            "token_limit": self.config.token_limit,
            "tokens_used": tokens_used,
            "input_tokens_used": input_tokens_used,
            "output_tokens_used": output_tokens_used,
            "cached_input_tokens_used": cached_input_tokens_used,
            "token_usage_complete": token_usage_complete,
            "tokens_remaining": self.config.token_limit - tokens_used,
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
    ) -> None:
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
            if int(snapshot["tokens_used"]) + token_count > self.config.token_limit:
                raise BudgetExceeded("token budget would be exceeded")
            started = action_events[-1]
            self._append_unlocked(
                {
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
