"""V0 deterministic immutable-baseline -> csim -> synth -> cosim workflow."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from .budget import BudgetConfig, BudgetError, BudgetExceeded, BudgetLedger, BudgetLedgerError
from .task import PublicTask, TaskPackageError, current_public_file_hashes
from .tools import (
    AmbiguousActionError,
    ToolArtifactError,
    ToolBackend,
    ToolConfig,
    ToolResult,
    ToolServer,
)
from .vitis import VitisBackend


class RunArtifactError(RuntimeError):
    """Raised when an existing run is incompatible or has been mutated."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_trace(path: Path, event: str, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"timestamp": _utc_now(), "event": event, **fields}
    encoded = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


class _RunLock(AbstractContextManager[None]):
    """Non-blocking cross-process lock for registry/result writers."""

    def __init__(self, run_root: Path) -> None:
        self.path = run_root / ".run.lock"
        self._stream: IO[bytes] | None = None

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise RunArtifactError(
                f"another process is already writing run directory: {self.path.parent}"
            ) from exc
        self._stream = stream
        return None

    def __exit__(self, *_args: object) -> None:
        stream = self._stream
        if stream is None:
            return None
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None
        return None


@dataclass(frozen=True)
class RunConfig:
    tool: ToolConfig
    budget: BudgetConfig
    minimum_frequency_mhz: float = 100.0

    def __post_init__(self) -> None:
        frequency = float(self.minimum_frequency_mhz)
        if not math.isfinite(frequency) or frequency <= 0:
            raise ValueError("minimum_frequency_mhz must be finite and positive")
        object.__setattr__(self, "minimum_frequency_mhz", frequency)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool.to_dict(),
            "budget": self.budget.to_dict(),
            "minimum_frequency_mhz": self.minimum_frequency_mhz,
            "workflow": "V0_DETERMINISTIC",
        }


def _task_spec(task: PublicTask) -> dict[str, object]:
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "difficulty": task.difficulty,
        "top": task.top,
        "task_budget": task.budget,
        "part": task.part,
        "clock_ns": task.clock_ns,
        "requires_cosim": task.requires_cosim,
        "initial_condition": task.initial_condition,
        "description": task.description,
        "kernel_file": task.kernel_name,
        "header_files": sorted(task.headers),
        "public_tb": task.public_tb_name,
        "public_file_hashes": dict(task.public_file_hashes),
    }


def _write_once_or_verify(path: Path, value: object) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunArtifactError(f"cannot read existing {path.name}: {exc}") from exc
        if existing != value:
            raise RunArtifactError(f"existing {path.name} does not match this run")
    else:
        _atomic_json(path, value)


def _initial_validation() -> dict[str, dict[str, object]]:
    return {
        "csim": {"status": "NOT_RUN"},
        "synth": {"status": "NOT_RUN"},
        "cosim": {"status": "NOT_RUN"},
    }


def _create_or_load_registry(
    task: PublicTask, run_root: Path, baseline_ref: str
) -> dict[str, object]:
    path = run_root / "candidate_registry.json"
    if path.exists():
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
            candidate = registry["candidates"]["candidate_000"]
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise RunArtifactError(f"invalid candidate registry: {exc}") from exc
        if (
            registry.get("task_id") != task.id
            or candidate.get("code_hash") != task.kernel_sha256
            or candidate.get("source_ref") != baseline_ref
            or candidate.get("immutable") is not True
        ):
            raise RunArtifactError("candidate registry baseline does not match the task")
        return registry

    registry = {
        "schema_version": 1,
        "task_id": task.id,
        "baseline_candidate_id": "candidate_000",
        "best_candidate_id": None,
        "final_candidate_id": None,
        "candidates": {
            "candidate_000": {
                "candidate_id": "candidate_000",
                "parent_id": None,
                "kind": "baseline",
                "immutable": True,
                "source_ref": baseline_ref,
                "code_hash": task.kernel_sha256,
                "status": "BASELINE",
                "validation": _initial_validation(),
                "metrics_ref": None,
                "credits_used": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        },
    }
    _atomic_json(path, registry)
    return registry


def _snapshot_baseline(task: PublicTask, run_root: Path) -> tuple[Path, str]:
    source_root = run_root / "baseline" / "source"
    path = source_root / task.kernel_name
    try:
        path.resolve().relative_to(source_root.resolve())
    except ValueError as exc:
        raise RunArtifactError("baseline path escapes its source directory") from exc
    reference = str(path.relative_to(run_root)).replace("\\", "/")
    if path.is_symlink():
        raise RunArtifactError("immutable baseline snapshot must not be a symlink")
    if path.exists():
        if path.read_bytes() != task.kernel_bytes:
            raise RunArtifactError("immutable baseline snapshot was modified")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(task.kernel_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return path, reference


def _public_inputs_unchanged(task: PublicTask) -> bool:
    try:
        current = current_public_file_hashes(task)
    except (OSError, TaskPackageError):
        return False
    return dict(current) == dict(task.public_file_hashes)


def _validation_record(result: ToolResult) -> dict[str, object]:
    if result.ok:
        status = "PASS"
    elif result.phase == "timeout":
        status = "TIMEOUT"
    elif result.phase == "tool_error":
        status = "TOOL_ERROR"
    else:
        status = "FAIL"
    return {
        "status": status,
        "phase": result.phase,
        "ok": result.ok,
        "action_id": result.action_id,
        "result_ref": result.result_ref,
        "code_hash": result.code_hash,
        "tool_config_hash": result.tool_config_hash,
        "backend_fingerprint": result.backend_fingerprint,
        "task_fingerprint": result.task_fingerprint,
        "effective_timeout_seconds": result.effective_timeout_seconds,
        "cached": result.cached,
    }


def _stop_reason(stage: str, result: ToolResult) -> str:
    return f"{stage.upper()}_{result.phase.upper()}"


def _tool_exception(stage: str, exc: Exception) -> tuple[dict[str, object], str]:
    if isinstance(exc, BudgetExceeded):
        status, phase, suffix = "BUDGET_EXCEEDED", "budget_exceeded", "BUDGET_EXCEEDED"
    elif isinstance(exc, AmbiguousActionError):
        status, phase, suffix = "AMBIGUOUS", "ambiguous_action", "AMBIGUOUS_ACTION"
    elif isinstance(exc, ToolArtifactError):
        status, phase, suffix = "ARTIFACT_ERROR", "artifact_error", "ARTIFACT_ERROR"
    elif isinstance(exc, BudgetLedgerError):
        status, phase, suffix = "LEDGER_ERROR", "ledger_error", "LEDGER_ERROR"
    else:
        status, phase, suffix = "BUDGET_ERROR", "budget_error", "BUDGET_ERROR"
    return (
        {
            "status": status,
            "phase": phase,
            "ok": False,
            "error_type": type(exc).__name__,
            "detail": str(exc),
        },
        f"{stage.upper()}_{suffix}",
    )


def _invoke_stage(
    server: ToolServer, stage: str, kernel_bytes: bytes
) -> tuple[ToolResult | None, dict[str, object] | None, str | None]:
    try:
        result = getattr(server, stage)(kernel_bytes, candidate_id="candidate_000")
    except (
        BudgetError,
        AmbiguousActionError,
        ToolArtifactError,
    ) as exc:
        record, reason = _tool_exception(stage, exc)
        return None, record, reason
    return result, None, None


def run_v0(
    task: PublicTask,
    run_dir: str | Path,
    config: RunConfig,
    *,
    backend: ToolBackend | None = None,
) -> dict[str, object]:
    """Execute exactly one immutable baseline through all metered V0 stages."""

    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    with _RunLock(run_root):
        return _run_v0_locked(task, run_root, config, backend=backend)


def _run_v0_locked(
    task: PublicTask,
    run_root: Path,
    config: RunConfig,
    *,
    backend: ToolBackend | None,
) -> dict[str, object]:
    trace_path = run_root / "trace.jsonl"
    _write_once_or_verify(run_root / "task_spec.json", _task_spec(task))
    _write_once_or_verify(run_root / "run_config.json", config.to_dict())
    baseline_path, baseline_ref = _snapshot_baseline(task, run_root)
    registry = _create_or_load_registry(task, run_root, baseline_ref)
    candidate = registry["candidates"]["candidate_000"]  # type: ignore[index]

    # Open and validate accounting before replacing any previously verified
    # registry state.  A corrupt ledger therefore cannot erase good evidence.
    budget = BudgetLedger(run_root / "budget_ledger.jsonl", config.budget)
    server = ToolServer(
        task=task,
        budget=budget,
        run_root=run_root,
        config=config.tool,
        backend=backend or VitisBackend(),
    )

    candidate["validation"] = _initial_validation()
    candidate["status"] = "EVALUATING"
    candidate["metrics_ref"] = None
    registry["best_candidate_id"] = None
    registry["final_candidate_id"] = None
    # Keep the last durable registry intact until this attempt reaches a
    # terminal state.  Per-stage durability lives in ledger/action/trace files.
    _append_trace(
        trace_path,
        "RUN_STARTED",
        run_id=run_root.name,
        task_id=task.id,
        workflow="V0_DETERMINISTIC",
    )
    _append_trace(
        trace_path,
        "BASELINE_READY",
        candidate_id="candidate_000",
        source_ref=baseline_ref,
        code_hash=task.kernel_sha256,
        immutable=True,
    )

    validation = candidate["validation"]
    cache_hits = 0
    status = "FAILED"
    stop_reason: str | None = None
    clock_constraint: dict[str, object] = {
        "minimum_frequency_mhz": config.minimum_frequency_mhz,
        "maximum_period_ns": 1000.0 / config.minimum_frequency_mhz,
        "estimated_period_ns": None,
        "passed": False,
    }

    csim, error_record, error_reason = _invoke_stage(
        server, "csim", task.kernel_bytes
    )
    if csim is None:
        validation["csim"] = error_record
        stop_reason = error_reason
    else:
        cache_hits += int(csim.cached)
        validation["csim"] = _validation_record(csim)
        if not csim.ok:
            stop_reason = _stop_reason("csim", csim)
    if csim is not None and csim.ok:
        synth, error_record, error_reason = _invoke_stage(
            server, "synth", task.kernel_bytes
        )
        if synth is None:
            validation["synth"] = error_record
            stop_reason = error_reason
        else:
            cache_hits += int(synth.cached)
            validation["synth"] = _validation_record(synth)
            candidate["metrics_ref"] = (
                synth.result_ref if synth.report is not None else None
            )
            if not synth.ok:
                stop_reason = _stop_reason("synth", synth)
        if synth is not None and synth.ok:
            estimated_clock = (
                synth.report.get("estimated_clock_period_ns")
                if synth.report is not None
                else None
            )
            clock_constraint["estimated_period_ns"] = estimated_clock
            if isinstance(estimated_clock, bool) or not isinstance(
                estimated_clock, (int, float)
            ):
                stop_reason = "SYNTH_MISSING_CLOCK_METRIC"
            elif not math.isfinite(float(estimated_clock)) or float(estimated_clock) <= 0:
                stop_reason = "SYNTH_INVALID_CLOCK_METRIC"
            elif float(estimated_clock) > float(clock_constraint["maximum_period_ns"]):
                stop_reason = "CLOCK_CONSTRAINT_FAILED"
            else:
                clock_constraint["passed"] = True
                cosim, error_record, error_reason = _invoke_stage(
                    server, "cosim", task.kernel_bytes
                )
                if cosim is None:
                    validation["cosim"] = error_record
                    stop_reason = error_reason
                else:
                    cache_hits += int(cosim.cached)
                    validation["cosim"] = _validation_record(cosim)
                    if not cosim.ok:
                        stop_reason = _stop_reason("cosim", cosim)
                    else:
                        status = "DONE"
                        stop_reason = "BASELINE_VERIFIED"

    try:
        baseline_bytes = baseline_path.read_bytes()
        write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        baseline_read_only = baseline_path.stat().st_mode & write_bits == 0
    except OSError:
        baseline_bytes = b""
        baseline_read_only = False
    baseline_unchanged = (
        _public_inputs_unchanged(task)
        and baseline_bytes == task.kernel_bytes
        and _sha256(baseline_bytes) == task.kernel_sha256
        and baseline_read_only
    )
    if not baseline_unchanged:
        status = "FAILED"
        stop_reason = "BASELINE_MUTATED"

    if status == "DONE":
        candidate["status"] = "VERIFIED"
        registry["best_candidate_id"] = "candidate_000"
        registry["final_candidate_id"] = "candidate_000"
    else:
        candidate["status"] = "FAILED"
    snapshot = budget.snapshot()
    candidate["credits_used"] = snapshot["credits_used"]
    _atomic_json(run_root / "candidate_registry.json", registry)
    result: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_root.name,
        "task_id": task.id,
        "workflow": "V0_DETERMINISTIC",
        "status": status,
        "stop_reason": stop_reason,
        "baseline_candidate_id": "candidate_000",
        "best_candidate_id": registry["best_candidate_id"],
        "final_candidate_id": registry["final_candidate_id"],
        "baseline_unchanged": baseline_unchanged,
        "validation": validation,
        "clock_constraint": clock_constraint,
        "budget": snapshot,
        "cache_hits": cache_hits,
        "artifacts": {
            "task_spec": "task_spec.json",
            "run_config": "run_config.json",
            "workflow_result": "workflow_result.json",
            "candidate_registry": "candidate_registry.json",
            "budget_ledger": "budget_ledger.jsonl",
            "budget_state": "budget_state.json",
            "trace": "trace.jsonl",
            "baseline_source": baseline_ref,
        },
    }
    _atomic_json(run_root / "workflow_result.json", result)
    _append_trace(
        trace_path,
        "RUN_COMPLETED",
        run_id=run_root.name,
        task_id=task.id,
        status=status,
        stop_reason=stop_reason,
        baseline_unchanged=baseline_unchanged,
        credits_used=snapshot["credits_used"],
        cache_hits=cache_hits,
        result_ref="workflow_result.json",
    )
    return result
