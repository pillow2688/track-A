"""Structured tool results and the only budget-charged Vitis-facing API."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

from .budget import BudgetLedger
from .runtime_control import (
    COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME,
    TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME,
    RuntimeDeadline,
    RuntimePermit,
    RuntimeUnavailable,
)
from .task import PublicTask


_SAFE_TCL_ATOM = re.compile(r"\A[A-Za-z0-9_.+-]+\Z")
_VALIDATION_SCOPES = {"exploration", "search_closeout"}


class ToolArtifactError(RuntimeError):
    """Raised when an audited action has missing or inconsistent artifacts."""


class AmbiguousActionError(RuntimeError):
    """Raised when recovery cannot prove whether a started action completed."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return encoded


def _append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _backend_fingerprint(backend: object) -> str:
    explicit = getattr(backend, "fingerprint", None)
    if callable(explicit):
        value = str(explicit())
    else:
        cls = type(backend)
        value = f"{cls.__module__}.{cls.__qualname__}"
    if not value:
        raise ValueError("backend fingerprint must not be empty")
    return value


@dataclass(frozen=True)
class ToolConfig:
    vitis_root: str
    part: str
    clock_ns: float
    timeouts: Mapping[str, float]
    flow_target: str = "vivado"
    toolchain_id: str = "Vitis 2025.2"

    def __post_init__(self) -> None:
        clock_ns = float(self.clock_ns)
        timeouts = {str(kind): float(value) for kind, value in self.timeouts.items()}
        if set(timeouts) != {"csim", "synth", "cosim"}:
            raise ValueError("timeouts must configure exactly csim, synth, and cosim")
        if any(not math.isfinite(value) or value <= 0 for value in timeouts.values()):
            raise ValueError("tool timeouts must be finite and positive")
        if not math.isfinite(clock_ns) or clock_ns <= 0:
            raise ValueError("clock_ns must be finite and positive")
        if not self.vitis_root or not self.part or not self.flow_target:
            raise ValueError("Vitis root, part, and flow target must not be empty")
        if _SAFE_TCL_ATOM.fullmatch(self.part) is None:
            raise ValueError("part contains Tcl metacharacters")
        if _SAFE_TCL_ATOM.fullmatch(self.flow_target) is None:
            raise ValueError("flow_target contains Tcl metacharacters")
        if not self.toolchain_id:
            raise ValueError("toolchain_id must not be empty")
        object.__setattr__(self, "clock_ns", clock_ns)
        object.__setattr__(self, "timeouts", MappingProxyType(timeouts))

    def timeout_for(self, kind: str) -> float:
        try:
            return float(self.timeouts[kind])
        except KeyError as exc:
            raise ValueError(f"no timeout configured for {kind}") from exc

    def with_timeout(self, kind: str, timeout_seconds: float) -> "ToolConfig":
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("effective timeout must be finite and positive")
        values = dict(self.timeouts)
        if kind not in values:
            raise ValueError(f"no timeout configured for {kind}")
        values[kind] = timeout
        return replace(self, timeouts=values)

    def hash_for(self, kind: str, *, backend_fingerprint: str = "unspecified") -> str:
        payload = {
            "kind": kind,
            "vitis_root": self.vitis_root,
            "part": self.part,
            "clock_ns": self.clock_ns,
            "timeout_seconds": self.timeout_for(kind),
            "flow_target": self.flow_target,
            "toolchain_id": self.toolchain_id,
            "backend_fingerprint": backend_fingerprint,
        }
        return _sha256(_canonical_json(payload).encode())

    def to_dict(self) -> dict[str, object]:
        return {
            "vitis_root": self.vitis_root,
            "part": self.part,
            "clock_ns": self.clock_ns,
            "timeouts": dict(self.timeouts),
            "flow_target": self.flow_target,
            "toolchain_id": self.toolchain_id,
        }


@dataclass(frozen=True)
class BackendResult:
    """Backend outcome with artifact refs relative to ``work_dir.parent``."""

    ok: bool
    phase: str
    return_code: int
    elapsed_s: float
    evidence: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    report: dict[str, object] | None = None
    cosim: dict[str, object] | None = None


@dataclass(frozen=True)
class ToolResult:
    kind: str
    ok: bool
    phase: str
    return_code: int
    elapsed_s: float
    effective_timeout_seconds: float
    action_id: str
    candidate_id: str
    code_hash: str
    tool_config_hash: str
    backend_fingerprint: str
    task_fingerprint: str
    result_ref: str
    cached: bool
    evidence: list[str]
    artifacts: dict[str, str]
    artifact_hashes: dict[str, str]
    validation_scope: str = "exploration"
    report: dict[str, object] | None = None
    cosim: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        if self.validation_scope == "exploration":
            value.pop("validation_scope", None)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ToolResult":
        if not isinstance(value.get("ok"), bool):
            raise TypeError("result ok must be boolean")
        return cls(
            kind=str(value["kind"]),
            ok=value["ok"],  # type: ignore[arg-type]
            phase=str(value["phase"]),
            return_code=int(value["return_code"]),
            elapsed_s=float(value["elapsed_s"]),
            effective_timeout_seconds=float(value["effective_timeout_seconds"]),
            action_id=str(value["action_id"]),
            candidate_id=str(value["candidate_id"]),
            code_hash=str(value["code_hash"]),
            tool_config_hash=str(value["tool_config_hash"]),
            backend_fingerprint=str(value["backend_fingerprint"]),
            task_fingerprint=str(value["task_fingerprint"]),
            result_ref=str(value["result_ref"]),
            cached=bool(value.get("cached", False)),
            evidence=[str(item) for item in value.get("evidence", [])],  # type: ignore[arg-type]
            artifacts={
                str(key): str(item)
                for key, item in dict(value.get("artifacts", {})).items()
            },
            artifact_hashes={
                str(key): str(item)
                for key, item in dict(value.get("artifact_hashes", {})).items()
            },
            validation_scope=str(value.get("validation_scope", "exploration")),
            report=value.get("report") if isinstance(value.get("report"), dict) else None,
            cosim=value.get("cosim") if isinstance(value.get("cosim"), dict) else None,
        )


class ToolBackend(Protocol):
    def run(
        self,
        kind: str,
        *,
        task: PublicTask,
        kernel_bytes: bytes,
        work_dir: Path,
        config: ToolConfig,
    ) -> BackendResult: ...


def _artifact_hashes(action_dir: Path, artifacts: Mapping[str, str]) -> dict[str, str]:
    root = action_dir.resolve()
    hashes: dict[str, str] = {}
    for name, reference in artifacts.items():
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolArtifactError(f"artifact {name} escapes its action directory")
        path = (action_dir / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ToolArtifactError(
                f"artifact {name} resolves outside its action directory"
            ) from exc
        if not path.is_file():
            raise ToolArtifactError(f"artifact {name} is missing: {reference}")
        try:
            hashes[str(name)] = _sha256(path.read_bytes())
        except OSError as exc:
            raise ToolArtifactError(f"cannot hash artifact {name}: {exc}") from exc
    return hashes


class ToolServer:
    """Metered and audited csim/synth/cosim interface used by the workflow."""

    def __init__(
        self,
        *,
        task: PublicTask,
        budget: BudgetLedger,
        run_root: str | Path,
        config: ToolConfig,
        backend: ToolBackend,
        runtime_deadline: RuntimeDeadline | None = None,
    ) -> None:
        self.task = task
        self.budget = budget
        self.run_root = Path(run_root)
        self.config = config
        self._backend = backend
        self.runtime_deadline = runtime_deadline
        self.backend_fingerprint = _backend_fingerprint(backend)
        self.task_fingerprint = _sha256(
            _canonical_json(
                {
                    "task_id": task.id,
                    "top": task.top,
                    "kernel_file": task.kernel_name,
                    "public_tb": task.public_tb_name,
                    "public_file_hashes": dict(task.public_file_hashes),
                }
            ).encode()
        )
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.run_root / "trace.jsonl"
        self.budget_state_path = self.run_root / "budget_state.json"
        self.budget.write_snapshot(self.budget_state_path)

    def csim(
        self,
        kernel_code: str | bytes,
        *,
        candidate_id: str = "candidate_000",
        validation_scope: str = "exploration",
    ) -> ToolResult:
        return self._invoke("csim", kernel_code, candidate_id, validation_scope)

    def synth(
        self,
        kernel_code: str | bytes,
        *,
        candidate_id: str = "candidate_000",
        validation_scope: str = "exploration",
    ) -> ToolResult:
        return self._invoke("synth", kernel_code, candidate_id, validation_scope)

    def cosim(
        self,
        kernel_code: str | bytes,
        *,
        candidate_id: str = "candidate_000",
        validation_scope: str = "exploration",
    ) -> ToolResult:
        return self._invoke("cosim", kernel_code, candidate_id, validation_scope)

    def _trace(self, event: str, **fields: object) -> None:
        _append_jsonl(
            self.trace_path,
            {"timestamp": _utc_now(), "event": event, **fields},
        )

    def _load_result(
        self,
        path: Path,
        *,
        expected_sha256: str | None,
        action_id: str,
        kind: str,
        candidate_id: str,
        code_hash: str,
        tool_config_hash: str,
        validation_scope: str,
        result_ref: str,
        expected_backend_fingerprint: str | None = None,
    ) -> ToolResult:
        try:
            encoded = path.read_bytes()
            if expected_sha256 is not None and _sha256(encoded) != expected_sha256:
                raise ToolArtifactError(f"result digest mismatch for {action_id}")
            value = json.loads(encoded.decode("utf-8"))
            if not isinstance(value, dict):
                raise TypeError("result is not a JSON object")
            result = ToolResult.from_dict(value)
        except ToolArtifactError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ToolArtifactError(f"cannot load result for {action_id}: {exc}") from exc
        if (
            result.action_id != action_id
            or result.kind != kind
            or result.candidate_id != candidate_id
            or result.code_hash != code_hash
            or result.tool_config_hash != tool_config_hash
            or result.validation_scope != validation_scope
            or result.backend_fingerprint
            != (expected_backend_fingerprint or self.backend_fingerprint)
            or result.task_fingerprint != self.task_fingerprint
            or result.result_ref != result_ref
        ):
            raise ToolArtifactError(f"result binding mismatch for {action_id}")
        if not math.isfinite(result.elapsed_s) or result.elapsed_s < 0:
            raise ToolArtifactError(f"invalid elapsed time for {action_id}")
        if (
            not math.isfinite(result.effective_timeout_seconds)
            or result.effective_timeout_seconds <= 0
        ):
            raise ToolArtifactError(f"invalid effective timeout for {action_id}")
        actual_hashes = _artifact_hashes(path.parent, result.artifacts)
        if actual_hashes != result.artifact_hashes:
            raise ToolArtifactError(f"artifact digest mismatch for {action_id}")
        return result

    def load_completed_result(
        self,
        result_ref: str,
        *,
        allowed_backend_fingerprints: set[str] | None = None,
    ) -> ToolResult:
        """Load a result only when its action identity and Ledger digest agree."""

        relative = Path(result_ref)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 3
            or relative.parts[0] != "actions"
            or relative.parts[2] != "result.json"
            or re.fullmatch(r"[0-9a-f]{64}", relative.parts[1]) is None
        ):
            raise ToolArtifactError("completed result reference is invalid")
        action_id = relative.parts[1]
        unresolved = self.run_root / relative
        cursor = self.run_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ToolArtifactError(
                    f"completed result uses a symbolic link for {action_id}"
                )
        result_path = unresolved.resolve()
        run_root = self.run_root.resolve()
        try:
            result_path.relative_to(run_root)
        except ValueError as exc:
            raise ToolArtifactError(
                f"completed result escapes the run for {action_id}"
            ) from exc

        completed = self.budget.completed_event(action_id)
        action_events = self.budget.action_events(action_id)
        started = next(
            (event for event in action_events if event.get("state") == "STARTED"),
            None,
        )
        if completed is None or started is None:
            raise ToolArtifactError(f"action is not durably completed: {action_id}")
        expected_digest = completed.get("result_sha256")
        if (
            completed.get("result_ref") != result_ref
            or not isinstance(expected_digest, str)
            or not result_path.is_file()
        ):
            raise ToolArtifactError(
                f"completed action {action_id} has an invalid result binding"
            )
        try:
            encoded = result_path.read_bytes()
            if _sha256(encoded) != expected_digest:
                raise ToolArtifactError(f"result digest mismatch for {action_id}")
            value = json.loads(encoded.decode("utf-8"))
            if not isinstance(value, dict):
                raise TypeError("result is not a JSON object")
        except ToolArtifactError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
        ) as exc:
            raise ToolArtifactError(f"cannot load result for {action_id}: {exc}") from exc

        validation_scope = str(value.get("validation_scope", "exploration"))
        if validation_scope not in _VALIDATION_SCOPES:
            raise ToolArtifactError(
                f"invalid validation scope for completed action {action_id}"
            )
        result_backend_fingerprint = value.get("backend_fingerprint")
        allowed_fingerprints = allowed_backend_fingerprints or {
            self.backend_fingerprint
        }
        if (
            not isinstance(result_backend_fingerprint, str)
            or result_backend_fingerprint not in allowed_fingerprints
        ):
            raise ToolArtifactError(
                f"backend fingerprint mismatch for completed action {action_id}"
            )
        action_payload = {
            "kind": started.get("kind"),
            "candidate_id": started.get("candidate_id"),
            "code_hash": started.get("code_hash"),
            "tool_config_hash": started.get("tool_config_hash"),
            "backend_fingerprint": result_backend_fingerprint,
            "task_fingerprint": self.task_fingerprint,
        }
        if validation_scope != "exploration":
            action_payload["validation_scope"] = validation_scope
        if _sha256(_canonical_json(action_payload).encode()) != action_id:
            raise ToolArtifactError(f"action identity mismatch for {action_id}")
        return self._load_result(
            result_path,
            expected_sha256=expected_digest,
            action_id=action_id,
            kind=str(started.get("kind", "")),
            candidate_id=str(started.get("candidate_id", "")),
            code_hash=str(started.get("code_hash", "")),
            tool_config_hash=str(started.get("tool_config_hash", "")),
            validation_scope=validation_scope,
            result_ref=result_ref,
            expected_backend_fingerprint=result_backend_fingerprint,
        )

    def _invoke(
        self,
        kind: str,
        kernel_code: str | bytes,
        candidate_id: str,
        validation_scope: str,
    ) -> ToolResult:
        if validation_scope not in _VALIDATION_SCOPES:
            raise ValueError(f"unsupported validation scope: {validation_scope}")
        kernel_bytes = (
            kernel_code.encode("utf-8")
            if isinstance(kernel_code, str)
            else bytes(kernel_code)
        )
        code_hash = _sha256(kernel_bytes)
        tool_config_hash = self.config.hash_for(
            kind, backend_fingerprint=self.backend_fingerprint
        )
        action_payload = {
            "kind": kind,
            "candidate_id": candidate_id,
            "code_hash": code_hash,
            "tool_config_hash": tool_config_hash,
            "backend_fingerprint": self.backend_fingerprint,
            "task_fingerprint": self.task_fingerprint,
        }
        if validation_scope != "exploration":
            action_payload["validation_scope"] = validation_scope
        action_id = _sha256(_canonical_json(action_payload).encode())
        result_ref = f"actions/{action_id}/result.json"
        result_path = self.run_root / result_ref

        completed = self.budget.completed_event(action_id)
        if completed is not None:
            completed_ref = completed.get("result_ref")
            expected_digest = completed.get("result_sha256")
            if completed_ref != result_ref or not isinstance(expected_digest, str):
                raise ToolArtifactError(
                    f"completed action {action_id} has an invalid result binding"
                )
            if not result_path.is_file():
                raise ToolArtifactError(
                    f"completed action {action_id} is missing {result_ref}"
                )
            cached = replace(
                self._load_result(
                    result_path,
                    expected_sha256=expected_digest,
                    action_id=action_id,
                    kind=kind,
                    candidate_id=candidate_id,
                    code_hash=code_hash,
                    tool_config_hash=tool_config_hash,
                    validation_scope=validation_scope,
                    result_ref=result_ref,
                ),
                cached=True,
            )
            # budget_state.json is derived and may be missing if the previous
            # process crashed after committing COMPLETED to the ledger.
            self.budget.write_snapshot(self.budget_state_path)
            self._trace(
                "TOOL_CACHE_HIT",
                action_id=action_id,
                candidate_id=candidate_id,
                kind=kind,
                result_ref=result_ref,
                result_sha256=expected_digest,
            )
            return cached

        if self.budget.is_ambiguous(action_id):
            raise AmbiguousActionError(
                f"action {action_id} remains ambiguous; charge is conserved"
            )

        if self.budget.has_pending(action_id):
            if result_path.is_file():
                recovered = self._load_result(
                    result_path,
                    expected_sha256=None,
                    action_id=action_id,
                    kind=kind,
                    candidate_id=candidate_id,
                    code_hash=code_hash,
                    tool_config_hash=tool_config_hash,
                    validation_scope=validation_scope,
                    result_ref=result_ref,
                )
                result_digest = _sha256(result_path.read_bytes())
                self.budget.complete(
                    action_id=action_id,
                    result_ref=result_ref,
                    result_sha256=result_digest,
                    elapsed_s=recovered.elapsed_s,
                )
                self.budget.write_snapshot(self.budget_state_path)
                self._trace(
                    "TOOL_RECOVERED",
                    action_id=action_id,
                    kind=kind,
                    result_sha256=result_digest,
                )
                return replace(recovered, cached=True)
            self.budget.mark_ambiguous(action_id)
            self.budget.write_snapshot(self.budget_state_path)
            self._trace("TOOL_AMBIGUOUS", action_id=action_id, kind=kind)
            raise AmbiguousActionError(
                f"started action {action_id} had no durable result; charge was conserved"
            )

        configured_timeout = self.config.timeout_for(kind)
        if self.runtime_deadline is not None:
            permit = self.runtime_deadline.permit(
                configured_timeout,
                operation=kind,
                final_closure=validation_scope == "search_closeout",
            )
            if not permit.allowed:
                self._trace(
                    "TOOL_NOT_STARTED",
                    action_id=action_id,
                    candidate_id=candidate_id,
                    kind=kind,
                    validation_scope=validation_scope,
                    reason_code=permit.reason_code,
                    remaining_runtime_seconds=(
                        permit.remaining_runtime_seconds
                    ),
                    cleanup_reserve_seconds=(
                        permit.cleanup_reserve_seconds
                    ),
                    configured_timeout_seconds=(
                        permit.configured_timeout_seconds
                    ),
                    effective_timeout_seconds=(
                        permit.effective_timeout_seconds
                    ),
                    minimum_runtime_seconds=(
                        permit.minimum_runtime_seconds
                    ),
                )
                raise RuntimeUnavailable(permit)
            effective_timeout = permit.effective_timeout_seconds
            remaining_runtime = permit.remaining_runtime_seconds
        else:
            remaining_runtime = self.budget.remaining_runtime_seconds()
            effective_timeout = min(configured_timeout, remaining_runtime)
            if effective_timeout <= 0:
                permit = RuntimePermit(
                    allowed=False,
                    operation=kind,
                    remaining_runtime_seconds=max(0.0, remaining_runtime),
                    cleanup_reserve_seconds=0.0,
                    configured_timeout_seconds=configured_timeout,
                    effective_timeout_seconds=0.0,
                    minimum_runtime_seconds=0.0,
                    reason_code=(
                        COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME
                        if kind == "cosim"
                        else TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME
                    ),
                )
                self._trace(
                    "TOOL_NOT_STARTED",
                    action_id=action_id,
                    candidate_id=candidate_id,
                    kind=kind,
                    validation_scope=validation_scope,
                    reason_code=permit.reason_code,
                    remaining_runtime_seconds=max(
                        0.0, remaining_runtime
                    ),
                    cleanup_reserve_seconds=0.0,
                    configured_timeout_seconds=configured_timeout,
                    effective_timeout_seconds=0.0,
                    minimum_runtime_seconds=0.0,
                )
                raise RuntimeUnavailable(permit)

        self.budget.reserve(
            action_id=action_id,
            kind=kind,
            candidate_id=candidate_id,
            code_hash=code_hash,
            tool_config_hash=tool_config_hash,
        )
        self.budget.write_snapshot(self.budget_state_path)
        self._trace(
            "TOOL_STARTED",
            action_id=action_id,
                candidate_id=candidate_id,
                kind=kind,
                validation_scope=validation_scope,
            code_hash=code_hash,
            tool_config_hash=tool_config_hash,
            backend_fingerprint=self.backend_fingerprint,
            task_fingerprint=self.task_fingerprint,
            configured_timeout_seconds=configured_timeout,
            effective_timeout_seconds=effective_timeout,
            cost=self.budget.cost(kind),
        )

        effective_config = self.config.with_timeout(kind, effective_timeout)
        try:
            backend_result = self._backend.run(
                kind,
                task=self.task,
                kernel_bytes=kernel_bytes,
                work_dir=result_path.parent / "work",
                config=effective_config,
            )
        except Exception as exc:  # charged action still needs durable evidence
            backend_result = BackendResult(
                ok=False,
                phase="tool_error",
                return_code=-1,
                elapsed_s=0.0,
                evidence=[f"{type(exc).__name__}: {exc}"],
            )

        elapsed = float(backend_result.elapsed_s)
        if not math.isfinite(elapsed) or elapsed < 0:
            backend_result = BackendResult(
                ok=False,
                phase="tool_error",
                return_code=-1,
                elapsed_s=0.0,
                evidence=["backend returned invalid elapsed_s"],
                artifacts=dict(backend_result.artifacts),
            )
        artifact_hashes = _artifact_hashes(
            result_path.parent, backend_result.artifacts
        )
        result = ToolResult(
            kind=kind,
            ok=backend_result.ok,
            phase=backend_result.phase,
            return_code=backend_result.return_code,
            elapsed_s=backend_result.elapsed_s,
            effective_timeout_seconds=effective_timeout,
            action_id=action_id,
            candidate_id=candidate_id,
            code_hash=code_hash,
            tool_config_hash=tool_config_hash,
            backend_fingerprint=self.backend_fingerprint,
            task_fingerprint=self.task_fingerprint,
            result_ref=result_ref,
            cached=False,
            evidence=list(backend_result.evidence),
            artifacts=dict(backend_result.artifacts),
            artifact_hashes=artifact_hashes,
            validation_scope=validation_scope,
            report=backend_result.report,
            cosim=backend_result.cosim,
        )
        try:
            encoded = _atomic_json(result_path, result.to_dict())
        except (TypeError, ValueError) as exc:
            fallback = replace(
                result,
                ok=False,
                phase="tool_error",
                return_code=-1,
                evidence=[f"backend returned non-JSON evidence: {exc}"],
                report=None,
                cosim=None,
            )
            result = fallback
            encoded = _atomic_json(result_path, result.to_dict())
        result_digest = _sha256(encoded)
        self.budget.complete(
            action_id=action_id,
            result_ref=result_ref,
            result_sha256=result_digest,
            elapsed_s=result.elapsed_s,
        )
        self.budget.write_snapshot(self.budget_state_path)
        self._trace(
            "TOOL_COMPLETED",
            action_id=action_id,
            candidate_id=candidate_id,
            kind=kind,
            validation_scope=validation_scope,
            phase=result.phase,
            ok=result.ok,
            result_ref=result_ref,
            result_sha256=result_digest,
            credits_used=self.budget.snapshot()["credits_used"],
        )
        return result
