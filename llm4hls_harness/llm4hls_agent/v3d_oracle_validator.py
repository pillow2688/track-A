"""Resumable offline oracle validation for the V3-D fast corpus.

The validator has two deliberately separate jobs:

* fixture backends (``demo`` and ``deterministic``) check corpus plumbing and
  deterministic expectations without claiming hardware-tool evidence;
* the optional ``vitis`` backend executes the same mode-specific gate plan and
  is the only backend eligible to produce a real corpus anchor.

Rejected task packages are copied to an append-only ``rejected_corpus`` area
with a machine-readable reason.  The public task loader remains public-only;
private golden material is opened only inside this offline validator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, TextIO

from .task import PublicTask, load_public_task
from .tools import ToolConfig
from .v3d_corpus import (
    MutationEngine,
    PHASE_MODES,
    validate_acceptance,
    validate_corpus_manifest,
    validate_mutation_manifest,
)
from .vitis import VitisBackend


ORACLE_TASK_SCHEMA_VERSION = "v3d.oracle-task.v1"
ORACLE_QUEUE_SCHEMA_VERSION = "v3d.oracle-queue.v1"
ORACLE_SUMMARY_SCHEMA_VERSION = "v3d.oracle-summary.v1"
REJECTED_CORPUS_SCHEMA_VERSION = "v3d.rejected-corpus.v1"
ORACLE_RUNNER_VERSION = "v3d.corpus-oracle-runner.v2"
REAL_VITIS_ANCHOR_AUTHORITY = "VITIS_STRUCTURED_CORPUS_ORACLE_V1"

GATES = ("csim", "synth", "cosim", "ppa")
GATE_STATUSES = ("PASS", "FAIL", "NOT_RUN", "ERROR")


class OracleError(RuntimeError):
    """Base error for corpus oracle validation."""


class OracleConfigurationError(OracleError):
    """Raised before execution for an invalid or incompatible run."""


class OracleTaskError(OracleError):
    """Raised when one corpus task is not a valid hash-bound package."""


class OracleExecutionError(OracleError):
    """Raised when an oracle backend cannot produce an observation."""


class EvidenceClass(str, Enum):
    DEMO = "demo"
    DETERMINISTIC = "deterministic"
    REAL_VITIS = "real_vitis"


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


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = _json_bytes(value)
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OracleTaskError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise OracleTaskError(f"{label} must be a JSON object")
    return value


def _safe_relative(path: Path, root: Path, *, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise OracleTaskError(f"{label} escapes the corpus root") from exc


def _tree_fingerprint(directory: Path) -> str:
    """Hash a task tree without following symbolic links."""

    if not directory.is_dir():
        return _sha256(f"missing:{directory}".encode("utf-8"))
    rows: list[dict[str, str]] = []
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError as exc:
                target = f"unreadable:{type(exc).__name__}:{exc}"
            rows.append({"path": relative, "kind": "symlink", "value": target})
        elif path.is_file():
            try:
                digest = _sha256(path.read_bytes())
            except OSError as exc:
                digest = _sha256(f"unreadable:{type(exc).__name__}:{exc}".encode())
            rows.append({"path": relative, "kind": "file", "value": digest})
        elif path.is_dir():
            rows.append({"path": relative, "kind": "directory", "value": ""})
    return _sha256(_canonical_json(rows).encode("utf-8"))


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":  # pragma: no cover - Linux is used in CI
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class OracleConfig:
    corpus: Path | str
    output_dir: Path | str
    backend: str = "deterministic"
    resume: bool = False
    retry_rejected: bool = False
    max_tasks: int | None = None
    max_runtime_seconds: float | None = None
    rejected_corpus_dir: Path | str | None = None
    task_filters: tuple[str, ...] = ()
    mode_filters: tuple[str, ...] = ()
    compiler: str = "g++"
    vitis_root: str = "/opt/xilinx/2025.2/Vitis"
    csim_timeout_seconds: float = 60.0
    synth_timeout_seconds: float = 900.0
    cosim_timeout_seconds: float = 1200.0
    toolchain_id: str = "Vitis 2025.2"
    prepare_only: bool = False

    def __post_init__(self) -> None:
        corpus = Path(self.corpus).resolve()
        output = Path(self.output_dir).resolve()
        rejected = (
            Path(self.rejected_corpus_dir).resolve()
            if self.rejected_corpus_dir is not None
            else output / "rejected_corpus"
        )
        backend = str(self.backend).casefold()
        if backend not in {"demo", "deterministic", "vitis"}:
            raise OracleConfigurationError(
                "backend must be demo, deterministic, or vitis"
            )
        if not corpus.is_dir():
            raise OracleConfigurationError(f"corpus directory does not exist: {corpus}")
        try:
            output.relative_to(corpus)
        except ValueError:
            pass
        else:
            raise OracleConfigurationError(
                "output_dir must be outside the source corpus"
            )
        try:
            rejected.relative_to(corpus)
        except ValueError:
            pass
        else:
            raise OracleConfigurationError(
                "rejected_corpus_dir must be outside the source corpus"
            )
        if self.max_tasks is not None and (
            isinstance(self.max_tasks, bool)
            or not isinstance(self.max_tasks, int)
            or self.max_tasks <= 0
        ):
            raise OracleConfigurationError("max_tasks must be a positive integer")
        if self.max_runtime_seconds is not None:
            runtime = float(self.max_runtime_seconds)
            if not math.isfinite(runtime) or runtime <= 0:
                raise OracleConfigurationError(
                    "max_runtime_seconds must be finite and positive"
                )
            object.__setattr__(self, "max_runtime_seconds", runtime)
        for field_name in (
            "csim_timeout_seconds",
            "synth_timeout_seconds",
            "cosim_timeout_seconds",
        ):
            timeout = float(getattr(self, field_name))
            if not math.isfinite(timeout) or timeout <= 0:
                raise OracleConfigurationError(
                    f"{field_name} must be finite and positive"
                )
            object.__setattr__(self, field_name, timeout)
        modes = tuple(str(mode).upper() for mode in self.mode_filters)
        if any(mode not in PHASE_MODES for mode in modes):
            raise OracleConfigurationError(
                f"mode filters must be selected from {', '.join(PHASE_MODES)}"
            )
        object.__setattr__(self, "corpus", corpus)
        object.__setattr__(self, "output_dir", output)
        object.__setattr__(self, "rejected_corpus_dir", rejected)
        object.__setattr__(self, "backend", backend)
        object.__setattr__(
            self, "task_filters", tuple(str(item) for item in self.task_filters)
        )
        object.__setattr__(self, "mode_filters", modes)

    def plan_dict(self) -> dict[str, object]:
        """Configuration fields that must not drift across resume."""

        return {
            "runner_version": ORACLE_RUNNER_VERSION,
            "corpus": str(self.corpus),
            "backend": self.backend,
            "task_filters": list(self.task_filters),
            "mode_filters": list(self.mode_filters),
            "compiler": self.compiler,
            "vitis_root": self.vitis_root,
            "csim_timeout_seconds": self.csim_timeout_seconds,
            "synth_timeout_seconds": self.synth_timeout_seconds,
            "cosim_timeout_seconds": self.cosim_timeout_seconds,
            "toolchain_id": self.toolchain_id,
        }

    def public_dict(self) -> dict[str, object]:
        return {
            **self.plan_dict(),
            "output_dir": str(self.output_dir),
            "rejected_corpus_dir": str(self.rejected_corpus_dir),
            "resume": self.resume,
            "retry_rejected": self.retry_rejected,
            "max_tasks": self.max_tasks,
            "max_runtime_seconds": self.max_runtime_seconds,
            "prepare_only": self.prepare_only,
        }


@dataclass(frozen=True)
class OracleTask:
    directory: Path
    task_id: str
    mode: str
    operator: str
    seed: int
    task_fingerprint: str
    public_task: PublicTask
    golden_kernel_bytes: bytes
    acceptance: Mapping[str, object]
    mutation: Mapping[str, object]


@dataclass(frozen=True)
class OracleOutcome:
    summary: Mapping[str, object]
    records: tuple[Mapping[str, object], ...]


class OracleBackend(Protocol):
    evidence_class: EvidenceClass
    real_anchor_authority: str | None
    requires_vitis_lock: bool

    def fingerprint(self) -> str: ...

    def validate(
        self,
        task: OracleTask,
        *,
        run_dir: Path,
        timeout_seconds: float | None,
    ) -> Mapping[str, object]: ...


def mode_gate_scope(
    mode: str, *, requires_cosim: bool = False
) -> tuple[str, ...]:
    """Return the ordered gates needed to prove one V3-C mode."""

    try:
        scope = {
            "REPAIR": ("csim", "synth"),
            "SYNTH_FIX": ("csim", "synth"),
            "STRUCTURAL_FIX": ("csim", "synth", "cosim"),
            "OPTIMIZE": ("csim", "synth", "ppa"),
        }[mode]
    except KeyError as exc:
        raise OracleTaskError(f"unsupported oracle mode: {mode}") from exc
    if mode == "OPTIMIZE" and requires_cosim:
        return ("csim", "synth", "cosim", "ppa")
    return scope


def expected_gate_statuses(
    acceptance: Mapping[str, object],
) -> dict[str, dict[str, str]]:
    """Build the mode-scoped baseline/golden expectation matrix.

    ``ppa`` is the comparison between the baseline and golden synth reports.
    The acceptance v1 schema predates a PPA field, so the OPTIMIZE expectation
    is derived explicitly from its declared ``expected_failing_gate``.
    """

    validated = validate_acceptance(acceptance)
    mode = str(validated["expected_mode"])
    scope = mode_gate_scope(
        mode, requires_cosim=bool(validated.get("requires_cosim", False))
    )
    baseline_declared = validated["baseline_validation"]
    golden_declared = validated["golden_validation"]
    assert isinstance(baseline_declared, Mapping)
    assert isinstance(golden_declared, Mapping)
    result = {
        subject: {gate: "NOT_RUN" for gate in GATES}
        for subject in ("baseline", "golden")
    }
    for gate in scope:
        if gate == "ppa":
            result["baseline"][gate] = "FAIL"
            result["golden"][gate] = "PASS"
        else:
            result["baseline"][gate] = str(baseline_declared[gate])
            result["golden"][gate] = str(golden_declared[gate])
    return result


def _projected_observations(task: OracleTask, *, source: str) -> dict[str, object]:
    expected = expected_gate_statuses(task.acceptance)
    return {
        subject: {
            gate: {
                "status": expected[subject][gate],
                "phase": "not_run" if expected[subject][gate] == "NOT_RUN" else source,
                "evidence": [
                    "fixture projection only; this is not real Vitis evidence"
                ]
                if expected[subject][gate] != "NOT_RUN"
                else [],
                "artifacts": {},
            }
            for gate in GATES
        }
        for subject in ("baseline", "golden")
    }


class DemoOracleBackend:
    """Manifest projection used for CLI demonstrations and queue tests."""

    evidence_class = EvidenceClass.DEMO
    real_anchor_authority = None
    requires_vitis_lock = False

    def fingerprint(self) -> str:
        return "llm4hls_agent.v3d_oracle_validator.DemoOracleBackend:v1"

    def validate(
        self,
        task: OracleTask,
        *,
        run_dir: Path,
        timeout_seconds: float | None,
    ) -> Mapping[str, object]:
        del run_dir, timeout_seconds
        return _projected_observations(task, source="demo_projection")


class DeterministicOracleBackend:
    """Native public CSim plus hash-bound fixture projections for other gates."""

    evidence_class = EvidenceClass.DETERMINISTIC
    real_anchor_authority = None
    requires_vitis_lock = False

    def __init__(
        self,
        compiler: str = "g++",
        *,
        subprocess_timeout_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.compiler = str(compiler)
        self.subprocess_timeout_seconds = float(subprocess_timeout_seconds)
        self.monotonic = monotonic

    def fingerprint(self) -> str:
        resolved = shutil.which(self.compiler) or self.compiler
        resolved_path = Path(resolved).resolve()
        try:
            compiler_sha256 = (
                _sha256(resolved_path.read_bytes())
                if resolved_path.is_file()
                else None
            )
        except OSError:
            compiler_sha256 = None
        return _sha256(
            _canonical_json(
                {
                    "backend": "deterministic-native-csim:v2",
                    "compiler": str(resolved_path),
                    "compiler_sha256": compiler_sha256,
                    "subprocess_timeout_seconds": self.subprocess_timeout_seconds,
                }
            ).encode("utf-8")
        )

    def _remaining_timeout(self, deadline: float | None) -> float:
        remaining = self.subprocess_timeout_seconds
        if deadline is not None:
            remaining = min(remaining, deadline - self.monotonic())
        if remaining <= 0:
            raise OracleExecutionError("deterministic oracle runtime expired")
        return max(0.001, remaining)

    def _native_csim(
        self,
        task: OracleTask,
        *,
        subject: str,
        kernel_bytes: bytes,
        run_dir: Path,
        deadline: float | None,
    ) -> dict[str, object]:
        compiler = shutil.which(self.compiler)
        if compiler is None:
            raise OracleExecutionError(
                f"deterministic CSim compiler is unavailable: {self.compiler}"
            )
        work = run_dir / "native_csim" / subject
        work.mkdir(parents=True, exist_ok=True)
        package = work / "package"
        kernel_path = package / task.public_task.kernel_name
        testbench_path = package / task.public_task.public_tb_name
        _atomic_bytes(kernel_path, kernel_bytes)
        _atomic_bytes(testbench_path, task.public_task.public_tb_bytes)
        for name, content in task.public_task.headers.items():
            _atomic_bytes(package / name, content)
        executable = work / "oracle_csim"
        compile_command = [
            compiler,
            "-std=c++17",
            "-Wno-unknown-pragmas",
            f"-I{package}",
            str(kernel_path),
            str(testbench_path),
            "-o",
            str(executable),
        ]
        started = self.monotonic()
        try:
            compiled = subprocess.run(
                compile_command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._remaining_timeout(deadline),
            )
        except subprocess.TimeoutExpired as exc:
            _atomic_text(work / "compile.stdout.log", _process_text(exc.stdout))
            _atomic_text(work / "compile.stderr.log", _process_text(exc.stderr))
            return {
                "status": "ERROR",
                "phase": "compile_timeout",
                "elapsed_seconds": max(0.0, self.monotonic() - started),
                "evidence": ["native CSim compilation timed out"],
                "artifacts": {
                    "compile_stdout": str(
                        (work / "compile.stdout.log").relative_to(run_dir)
                    ),
                    "compile_stderr": str(
                        (work / "compile.stderr.log").relative_to(run_dir)
                    ),
                },
            }
        _atomic_text(work / "compile.stdout.log", compiled.stdout)
        _atomic_text(work / "compile.stderr.log", compiled.stderr)
        artifacts = {
            "compile_stdout": str(
                (work / "compile.stdout.log").relative_to(run_dir)
            ),
            "compile_stderr": str(
                (work / "compile.stderr.log").relative_to(run_dir)
            ),
        }
        if compiled.returncode != 0:
            detail = compiled.stderr.strip().splitlines()[-1:]
            return {
                "status": "ERROR",
                "phase": "compile_fail",
                "return_code": compiled.returncode,
                "elapsed_seconds": max(0.0, self.monotonic() - started),
                "evidence": detail or ["native CSim compilation failed"],
                "artifacts": artifacts,
            }
        try:
            executed = subprocess.run(
                [str(executable)],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._remaining_timeout(deadline),
            )
        except subprocess.TimeoutExpired as exc:
            _atomic_text(work / "run.stdout.log", _process_text(exc.stdout))
            _atomic_text(work / "run.stderr.log", _process_text(exc.stderr))
            artifacts.update(
                {
                    "run_stdout": str(
                        (work / "run.stdout.log").relative_to(run_dir)
                    ),
                    "run_stderr": str(
                        (work / "run.stderr.log").relative_to(run_dir)
                    ),
                }
            )
            return {
                "status": "ERROR",
                "phase": "runtime_timeout",
                "elapsed_seconds": max(0.0, self.monotonic() - started),
                "evidence": ["native CSim execution timed out"],
                "artifacts": artifacts,
            }
        _atomic_text(work / "run.stdout.log", executed.stdout)
        _atomic_text(work / "run.stderr.log", executed.stderr)
        artifacts.update(
            {
                "run_stdout": str((work / "run.stdout.log").relative_to(run_dir)),
                "run_stderr": str((work / "run.stderr.log").relative_to(run_dir)),
            }
        )
        detail = (executed.stderr or executed.stdout).strip().splitlines()[-3:]
        return {
            "status": "PASS" if executed.returncode == 0 else "FAIL",
            "phase": "pass" if executed.returncode == 0 else "runtime_fail",
            "return_code": executed.returncode,
            "elapsed_seconds": max(0.0, self.monotonic() - started),
            "evidence": detail or [f"native CSim return_code={executed.returncode}"],
            "artifacts": artifacts,
        }

    def validate(
        self,
        task: OracleTask,
        *,
        run_dir: Path,
        timeout_seconds: float | None,
    ) -> Mapping[str, object]:
        observations = _projected_observations(
            task, source="hash_bound_manifest_projection"
        )
        deadline = (
            None
            if timeout_seconds is None
            else self.monotonic() + max(0.001, timeout_seconds)
        )
        observations["baseline"]["csim"] = self._native_csim(  # type: ignore[index]
            task,
            subject="baseline",
            kernel_bytes=task.public_task.kernel_bytes,
            run_dir=run_dir,
            deadline=deadline,
        )
        observations["golden"]["csim"] = self._native_csim(  # type: ignore[index]
            task,
            subject="golden",
            kernel_bytes=task.golden_kernel_bytes,
            run_dir=run_dir,
            deadline=deadline,
        )
        return observations


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _synth_metric_vector(report: object) -> dict[str, float] | None:
    if not isinstance(report, Mapping):
        return None
    latency = report.get("latency")
    interval = report.get("interval")
    resources = report.get("resources")
    available = report.get("available_resources")
    if not all(isinstance(item, Mapping) for item in (latency, interval, resources)):
        return None
    assert isinstance(latency, Mapping)
    assert isinstance(interval, Mapping)
    assert isinstance(resources, Mapping)
    worst = _number(latency.get("worst"))
    ii = _number(interval.get("max"))
    if worst is None or worst <= 0 or ii is None or ii <= 0:
        return None
    utilization = 0.0
    for name in ("LUT", "FF", "DSP", "BRAM_18K", "URAM"):
        used = _number(resources.get(name))
        capacity = (
            _number(available.get(name)) if isinstance(available, Mapping) else None
        )
        if used is not None and capacity is not None and capacity > 0:
            utilization += used / capacity
    return {"latency_worst": worst, "interval_max": ii, "resource_sum": utilization}


def _ppa_observations(
    baseline_report: object, golden_report: object
) -> tuple[dict[str, object], dict[str, object]]:
    baseline = _synth_metric_vector(baseline_report)
    golden = _synth_metric_vector(golden_report)
    policy = "golden worst-case latency must be strictly lower than baseline"
    if baseline is None or golden is None:
        record = {
            "status": "ERROR",
            "phase": "ppa_metrics_missing",
            "evidence": ["structured synth metrics are incomplete"],
            "artifacts": {},
            "comparison_policy": policy,
        }
        return dict(record), dict(record)
    preferred = golden["latency_worst"] < baseline["latency_worst"]
    common = {
        "phase": "ppa_comparison",
        "evidence": [
            "golden is preferred" if preferred else "golden is not preferred"
        ],
        "artifacts": {},
        "comparison_policy": policy,
        "baseline_metrics": baseline,
        "golden_metrics": golden,
    }
    return (
        {**common, "status": "FAIL" if preferred else "PASS"},
        {**common, "status": "PASS" if preferred else "FAIL"},
    )


def _vitis_gate_status(gate: str, result: object) -> str:
    """Classify only mode-relevant semantic failures as ``FAIL``.

    An expected-fail corpus gate is an oracle assertion, not merely a request
    for a non-zero process return code.  Unknown failures therefore fail
    closed as ``ERROR`` so simulator crashes, missing reports, license errors,
    and packaging problems cannot accidentally certify a mutation.
    """

    if bool(getattr(result, "ok", False)):
        return "PASS"
    phase = str(getattr(result, "phase", "")).strip().casefold()
    return_code = getattr(result, "return_code", None)
    evidence = "\n".join(
        str(item) for item in getattr(result, "evidence", [])
    ).casefold()
    infrastructure_tokens = (
        "command not found",
        "no such file or directory",
        "license checkout",
        "license manager",
        "cannot connect to license",
        "failed to open display",
        "tool_error",
        "internal exception",
        "internal error",
        "exceptional condition",
        "segmentation fault",
        "core dumped",
        "stack trace",
        "failed to launch",
        "cannot execute",
    )
    if (
        return_code in {126, 127}
        or any(token in evidence for token in infrastructure_tokens)
        or phase == "tool_error"
    ):
        return "ERROR"

    if gate == "csim":
        # The current REPAIR corpus defines a broken baseline as a public-TB
        # runtime/assertion/mismatch failure.  Compilation and timeouts are
        # infrastructure/source-package errors, not evidence of that defect.
        return (
            "FAIL"
            if phase in {"runtime_fail", "assert_fail", "mismatch"}
            else "ERROR"
        )

    if gate == "synth":
        if phase != "synth_error":
            return "ERROR"
        non_semantic_report_errors = (
            "csynth.xml is missing",
            "synthesis report is missing",
            "cannot parse csynth",
            "cannot parse synthesis report",
            "missing a valid estimatedclockperiod",
        )
        if any(token in evidence for token in non_semantic_report_errors):
            return "ERROR"
        source_or_constraint_tokens = (
            "source synthesis",
            "pre-synthesis failed",
            "syn check fail",
            "undefined function",
            "unsupported construct",
            "not supported for synthesis",
            "cannot be synthesized",
            "cannot synthesize",
            "synthesis constraint",
            "clock constraint",
            "timing constraint",
            "resource constraint",
            "resource limit",
            "exceeds available",
            "over-utilization",
            "over utilization",
            "[hls 214-",
        )
        return (
            "FAIL"
            if any(token in evidence for token in source_or_constraint_tokens)
            else "ERROR"
        )

    if gate == "cosim":
        cosim = getattr(result, "cosim", None)
        structured_mismatch = False
        if isinstance(cosim, Mapping):
            status = str(cosim.get("status", "")).strip().casefold()
            structured_mismatch = bool(status and status != "pass")
        semantic_tokens = (
            "deadlock detected",
            "blocked by empty",
            "blocked by full",
            "rtl mismatch",
            "c/rtl mismatch",
            "output mismatch",
            "post check failed",
        )
        semantic_failure = structured_mismatch or any(
            token in evidence for token in semantic_tokens
        )
        if phase == "timeout":
            # A timeout is semantic only after the evidence proves RTL
            # simulation started.  A generic Vitis process timeout is ERROR.
            simulation_started = (
                "# xsim" in evidence or "rtl simulation" in evidence
            )
            return "FAIL" if simulation_started else "ERROR"
        if phase != "cosim_fail":
            return "ERROR"
        # A deadlock commonly prevents Vitis from writing the cosim report;
        # explicit semantic evidence still preserves that valid anchor.  A
        # missing/unparseable report on its own remains an infrastructure error.
        return "FAIL" if semantic_failure else "ERROR"

    return "ERROR"


def _bound_vitis_artifacts(
    gate_root: Path, run_dir: Path, artifacts: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    references: dict[str, str] = {}
    hashes: dict[str, str] = {}
    resolved_gate_root = gate_root.resolve()
    for name, raw_reference in artifacts.items():
        relative = Path(raw_reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise OracleExecutionError(
                f"Vitis artifact {name} has an unsafe reference"
            )
        artifact = (gate_root / relative).resolve()
        try:
            artifact.relative_to(resolved_gate_root)
        except ValueError as exc:
            raise OracleExecutionError(
                f"Vitis artifact {name} escapes its gate directory"
            ) from exc
        if not artifact.is_file():
            raise OracleExecutionError(f"Vitis artifact {name} is missing")
        references[str(name)] = str(artifact.relative_to(run_dir.resolve()))
        hashes[str(name)] = _sha256(artifact.read_bytes())
    return references, hashes


class VitisOracleBackend:
    """Real, serial Vitis validation for baseline/golden gate anchors."""

    requires_vitis_lock = True

    def __init__(
        self,
        *,
        vitis_root: str,
        csim_timeout_seconds: float,
        synth_timeout_seconds: float,
        cosim_timeout_seconds: float,
        toolchain_id: str,
        runner: object | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vitis_root = str(vitis_root)
        self.timeouts = {
            "csim": float(csim_timeout_seconds),
            "synth": float(synth_timeout_seconds),
            "cosim": float(cosim_timeout_seconds),
        }
        self.toolchain_id = str(toolchain_id)
        self.monotonic = monotonic
        self.uses_default_runner = runner is None
        self.evidence_class = (
            EvidenceClass.REAL_VITIS
            if self.uses_default_runner
            else EvidenceClass.DETERMINISTIC
        )
        self.real_anchor_authority = (
            REAL_VITIS_ANCHOR_AUTHORITY if self.uses_default_runner else None
        )
        if self.uses_default_runner:
            self._probe_real_toolchain()
        self.backend = VitisBackend(runner=runner)  # type: ignore[arg-type]

    def _probe_real_toolchain(self) -> None:
        root = Path(self.vitis_root)
        settings = root / "settings64.sh"
        executable = root / "bin" / "vitis-run"
        if not settings.is_file():
            raise OracleConfigurationError(
                f"Vitis settings file is missing: {settings}"
            )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise OracleConfigurationError(
                f"Vitis executable is missing or not executable: {executable}"
            )

    def fingerprint(self) -> str:
        return _sha256(
            _canonical_json(
                {
                    "backend": "vitis-corpus-oracle:v3",
                    "raw_backend": self.backend.fingerprint(),
                    "vitis_root": self.vitis_root,
                    "timeouts": self.timeouts,
                    "toolchain_id": self.toolchain_id,
                    "evidence_class": self.evidence_class.value,
                    "runner_mode": (
                        "default_subprocess"
                        if self.uses_default_runner
                        else "injected_fixture"
                    ),
                    "ppa_policy": "strict-worst-latency:v2",
                }
            ).encode("utf-8")
        )

    def _remaining(
        self, gate: str, deadline: float | None
    ) -> float:
        timeout = self.timeouts[gate]
        if deadline is not None:
            timeout = min(timeout, deadline - self.monotonic())
        if timeout <= 0:
            raise OracleExecutionError("Vitis oracle runtime expired")
        return max(0.001, timeout)

    def validate(
        self,
        task: OracleTask,
        *,
        run_dir: Path,
        timeout_seconds: float | None,
    ) -> Mapping[str, object]:
        settings = Path(self.vitis_root) / "settings64.sh"
        if not settings.is_file():
            raise OracleExecutionError(f"Vitis settings file is missing: {settings}")
        observations = {
            subject: {
                gate: {
                    "status": "NOT_RUN",
                    "phase": "not_run",
                    "evidence": [],
                    "artifacts": {},
                }
                for gate in GATES
            }
            for subject in ("baseline", "golden")
        }
        deadline = (
            None
            if timeout_seconds is None
            else self.monotonic() + max(0.001, timeout_seconds)
        )
        expected = expected_gate_statuses(task.acceptance)
        scope = tuple(
            gate
            for gate in mode_gate_scope(
                task.mode,
                requires_cosim=bool(task.acceptance.get("requires_cosim", False)),
            )
            if gate != "ppa"
        )
        kernels = {
            "baseline": task.public_task.kernel_bytes,
            "golden": task.golden_kernel_bytes,
        }
        for subject in ("baseline", "golden"):
            for gate in scope:
                if expected[subject][gate] == "NOT_RUN":
                    continue
                timeout = self._remaining(gate, deadline)
                config = ToolConfig(
                    vitis_root=self.vitis_root,
                    part=task.public_task.part,
                    clock_ns=task.public_task.clock_ns,
                    timeouts={**self.timeouts, gate: timeout},
                    toolchain_id=self.toolchain_id,
                )
                gate_root = run_dir / subject / gate
                gate_root.mkdir(parents=True, exist_ok=True)
                result = self.backend.run(
                    gate,
                    task=task.public_task,
                    kernel_bytes=kernels[subject],
                    work_dir=gate_root / "work",
                    config=config,
                )
                artifact_refs, artifact_hashes = _bound_vitis_artifacts(
                    gate_root, run_dir, result.artifacts
                )
                observations[subject][gate] = {
                    "status": _vitis_gate_status(gate, result),
                    "phase": result.phase,
                    "return_code": result.return_code,
                    "elapsed_seconds": result.elapsed_s,
                    "evidence": list(result.evidence),
                    "artifacts": artifact_refs,
                    "artifact_hashes": artifact_hashes,
                    "report": result.report,
                    "cosim": result.cosim,
                }
        if task.mode == "OPTIMIZE":
            baseline_synth = observations["baseline"]["synth"]
            golden_synth = observations["golden"]["synth"]
            baseline_ppa, golden_ppa = _ppa_observations(
                baseline_synth.get("report"), golden_synth.get("report")
            )
            observations["baseline"]["ppa"] = baseline_ppa
            observations["golden"]["ppa"] = golden_ppa
        return observations


def default_backend(config: OracleConfig) -> OracleBackend:
    if config.backend == "demo":
        return DemoOracleBackend()
    if config.backend == "deterministic":
        return DeterministicOracleBackend(config.compiler)
    return VitisOracleBackend(
        vitis_root=config.vitis_root,
        csim_timeout_seconds=config.csim_timeout_seconds,
        synth_timeout_seconds=config.synth_timeout_seconds,
        cosim_timeout_seconds=config.cosim_timeout_seconds,
        toolchain_id=config.toolchain_id,
    )


def _backend_evidence_class(backend: object) -> EvidenceClass:
    value = getattr(backend, "evidence_class", None)
    if isinstance(value, EvidenceClass):
        return value
    try:
        return EvidenceClass(str(value))
    except ValueError as exc:
        raise OracleConfigurationError(
            "oracle backend must declare a valid evidence_class"
        ) from exc


def _prepare_task(
    corpus: Path,
    entry: Mapping[str, object],
    task_fingerprint: str,
) -> OracleTask:
    task_id = str(entry.get("task_id", ""))
    relative = Path(str(entry.get("path", "")))
    task_dir = corpus / relative
    if _safe_relative(task_dir, corpus, label=f"task {task_id}") != relative:
        raise OracleTaskError(f"task {task_id} path is not canonical")
    if task_dir.is_symlink():
        raise OracleTaskError(f"task {task_id} directory is a symbolic link")
    for path in task_dir.rglob("*"):
        if path.is_symlink():
            raise OracleTaskError(
                f"task {task_id} contains symbolic link {path.relative_to(task_dir)}"
            )
    if _tree_fingerprint(task_dir) != task_fingerprint:
        raise OracleTaskError(f"task {task_id} changed after queue creation")
    acceptance_path = task_dir / "acceptance.json"
    mutation_path = task_dir / "mutation_manifest.json"
    try:
        acceptance_bytes = acceptance_path.read_bytes()
        mutation_bytes = mutation_path.read_bytes()
    except OSError as exc:
        raise OracleTaskError(f"task {task_id} manifest is unreadable: {exc}") from exc
    if _sha256(acceptance_bytes) != entry.get("acceptance_sha256"):
        raise OracleTaskError(f"task {task_id} acceptance digest mismatch")
    if _sha256(mutation_bytes) != entry.get("mutation_manifest_sha256"):
        raise OracleTaskError(f"task {task_id} mutation digest mismatch")
    try:
        acceptance_raw = json.loads(acceptance_bytes.decode("utf-8"))
        mutation_raw = json.loads(mutation_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OracleTaskError(f"task {task_id} manifest is invalid JSON: {exc}") from exc
    if not isinstance(acceptance_raw, Mapping) or not isinstance(
        mutation_raw, Mapping
    ):
        raise OracleTaskError(f"task {task_id} manifests must be JSON objects")
    try:
        acceptance = validate_acceptance(acceptance_raw)
        mutation = validate_mutation_manifest(mutation_raw)
    except Exception as exc:
        raise OracleTaskError(f"task {task_id} manifest schema failed: {exc}") from exc
    mode = str(entry.get("mode", ""))
    if (
        acceptance["task_id"] != task_id
        or mutation["task_id"] != task_id
        or acceptance["expected_mode"] != mode
        or mutation["expected_mode"] != mode
        or mutation["operator"] != entry.get("operator")
        or mutation["seed"] != entry.get("seed")
    ):
        raise OracleTaskError(f"task {task_id} corpus bindings disagree")
    try:
        public_task = load_public_task(task_dir)
    except Exception as exc:
        raise OracleTaskError(
            f"task {task_id} public package failed to load: {exc}"
        ) from exc
    if public_task.id != task_id:
        raise OracleTaskError(f"task {task_id} public task id disagrees")
    golden_path = task_dir / "golden" / "kernel.cpp"
    try:
        golden = golden_path.read_bytes()
    except OSError as exc:
        raise OracleTaskError(f"task {task_id} golden kernel is unreadable: {exc}") from exc
    if public_task.kernel_sha256 != acceptance["mutated_kernel_sha256"]:
        raise OracleTaskError(f"task {task_id} baseline kernel digest mismatch")
    if _sha256(golden) != acceptance["golden_kernel_sha256"]:
        raise OracleTaskError(f"task {task_id} golden kernel digest mismatch")
    if (
        mutation["mutated_sha256"] != acceptance["mutated_kernel_sha256"]
        or mutation["base_sha256"] != acceptance["golden_kernel_sha256"]
        or public_task.requires_cosim != bool(acceptance["requires_cosim"])
    ):
        raise OracleTaskError(f"task {task_id} acceptance bindings disagree")
    try:
        replay = MutationEngine().mutate(
            golden.decode("utf-8"),
            operator=str(mutation["operator"]),
            seed=int(mutation["seed"]),
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise OracleTaskError(f"task {task_id} mutation replay failed: {exc}") from exc
    source_range = mutation["source_range"]
    assert isinstance(source_range, Mapping)
    if (
        replay.source.encode("utf-8") != public_task.kernel_bytes
        or replay.base_sha256 != mutation["base_sha256"]
        or replay.mutated_sha256 != mutation["mutated_sha256"]
        or replay.base_range_sha256 != source_range["base_range_sha256"]
        or replay.base_start_line != source_range["base_start_line"]
        or replay.base_end_line != source_range["base_end_line"]
        or replay.mutated_start_line != source_range["mutated_start_line"]
        or replay.mutated_end_line != source_range["mutated_end_line"]
    ):
        raise OracleTaskError(f"task {task_id} deterministic mutation replay disagrees")
    return OracleTask(
        directory=task_dir,
        task_id=task_id,
        mode=mode,
        operator=str(entry["operator"]),
        seed=int(entry["seed"]),
        task_fingerprint=task_fingerprint,
        public_task=public_task,
        golden_kernel_bytes=golden,
        acceptance=acceptance,
        mutation=mutation,
    )


def _normalise_observations(value: Mapping[str, object]) -> dict[str, object]:
    normalised: dict[str, object] = {}
    for subject in ("baseline", "golden"):
        subject_value = value.get(subject)
        if not isinstance(subject_value, Mapping):
            raise OracleExecutionError(f"oracle result is missing {subject}")
        gates: dict[str, object] = {}
        for gate in GATES:
            raw = subject_value.get(gate)
            if isinstance(raw, str):
                raw = {"status": raw}
            if not isinstance(raw, Mapping):
                raise OracleExecutionError(
                    f"oracle result is missing {subject}.{gate}"
                )
            status = raw.get("status")
            if status not in GATE_STATUSES:
                raise OracleExecutionError(
                    f"oracle result has invalid {subject}.{gate} status"
                )
            encoded = json.loads(json.dumps(dict(raw), allow_nan=False))
            encoded["status"] = str(status)
            gates[gate] = encoded
        normalised[subject] = gates
    return normalised


def _evaluate_observations(
    expected: Mapping[str, Mapping[str, str]],
    observations: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    checks: list[dict[str, object]] = []
    reason_codes: list[str] = []
    details: list[str] = []
    for subject in ("baseline", "golden"):
        actual_subject = observations[subject]
        assert isinstance(actual_subject, Mapping)
        for gate in GATES:
            expected_status = expected[subject][gate]
            if expected_status == "NOT_RUN":
                continue
            actual_gate = actual_subject[gate]
            assert isinstance(actual_gate, Mapping)
            actual_status = str(actual_gate["status"])
            matches = actual_status == expected_status
            checks.append(
                {
                    "subject": subject,
                    "gate": gate,
                    "expected": expected_status,
                    "actual": actual_status,
                    "matches": matches,
                }
            )
            if not matches:
                code = f"{subject}_{gate}_status_mismatch".upper()
                reason_codes.append(code)
                if actual_status == "ERROR":
                    reason_codes.append(
                        f"{subject}_{gate}_oracle_error".upper()
                    )
                details.append(
                    f"{subject}.{gate}: expected {expected_status}, observed {actual_status}"
                )
    return checks, list(dict.fromkeys(reason_codes)), details


def _record_ref(output_dir: Path, task_id: str, attempt: int) -> str:
    return f"runs/{task_id}/attempt_{attempt:04d}/oracle_result.json"


def _load_record(output_dir: Path, reference: object) -> dict[str, object] | None:
    if not isinstance(reference, str):
        return None
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = (output_dir / relative).resolve()
    try:
        path.relative_to(output_dir.resolve())
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class CorpusOracleValidator:
    """Serial, resumable corpus validator with an explicit durable queue."""

    def __init__(
        self,
        config: OracleConfig,
        *,
        backend: OracleBackend | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] = _utc_now,
    ) -> None:
        self.config = config
        self.backend = backend or default_backend(config)
        self.monotonic = monotonic
        self.utc_now = utc_now
        self.evidence_class = _backend_evidence_class(self.backend)
        expected_class = {
            "demo": EvidenceClass.DEMO,
            "deterministic": EvidenceClass.DETERMINISTIC,
            "vitis": EvidenceClass.REAL_VITIS,
        }[config.backend]
        if self.evidence_class is not expected_class:
            raise OracleConfigurationError(
                f"backend {config.backend} cannot claim evidence class "
                f"{self.evidence_class.value}"
            )
        fingerprint = self.backend.fingerprint()
        if not isinstance(fingerprint, str) or not fingerprint:
            raise OracleConfigurationError(
                "oracle backend fingerprint must be non-empty"
            )
        self.backend_fingerprint = fingerprint
        self.real_anchor_authorized = (
            config.backend == "vitis"
            and self.evidence_class is EvidenceClass.REAL_VITIS
            and type(self.backend) is VitisOracleBackend
            and bool(getattr(self.backend, "uses_default_runner", False))
            and getattr(self.backend, "real_anchor_authority", None)
            == REAL_VITIS_ANCHOR_AUTHORITY
        )

    def _load_manifest(self) -> tuple[dict[str, object], str]:
        path = self.config.corpus / "corpus_manifest.json"
        try:
            encoded = path.read_bytes()
            raw = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OracleConfigurationError(
                f"cannot read corpus manifest: {exc}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise OracleConfigurationError("corpus manifest must be an object")
        try:
            manifest = validate_corpus_manifest(raw)
        except Exception as exc:
            raise OracleConfigurationError(
                f"corpus manifest is invalid: {exc}"
            ) from exc
        return manifest, _sha256(encoded)

    def _selected_entries(
        self, manifest: Mapping[str, object]
    ) -> list[dict[str, object]]:
        entries = manifest["tasks"]
        assert isinstance(entries, list)
        selected: list[dict[str, object]] = []
        task_filters = set(self.config.task_filters)
        mode_filters = set(self.config.mode_filters)
        known_task_ids = {
            str(raw["task_id"]) for raw in entries if isinstance(raw, Mapping)
        }
        unknown = task_filters - known_task_ids
        if unknown:
            raise OracleConfigurationError(
                f"unknown task filters: {', '.join(sorted(unknown))}"
            )
        for raw in entries:
            assert isinstance(raw, dict)
            if task_filters and str(raw["task_id"]) not in task_filters:
                continue
            if mode_filters and str(raw["mode"]) not in mode_filters:
                continue
            selected.append(dict(raw))
        if not selected:
            raise OracleConfigurationError("oracle selection is empty")
        return selected

    def _new_queue(
        self,
        entries: Sequence[Mapping[str, object]],
        *,
        corpus_manifest_sha256: str,
    ) -> dict[str, object]:
        plan_payload = {
            "configuration": self.config.plan_dict(),
            "backend_fingerprint": self.backend_fingerprint,
            "corpus_manifest_sha256": corpus_manifest_sha256,
            "tasks": [
                {
                    "task_id": entry["task_id"],
                    "path": entry["path"],
                    "mode": entry["mode"],
                    "task_fingerprint": _tree_fingerprint(
                        self.config.corpus / str(entry["path"])
                    ),
                }
                for entry in entries
            ],
        }
        plan_fingerprint = _sha256(
            _canonical_json(plan_payload).encode("utf-8")
        )
        return {
            "schema_version": ORACLE_QUEUE_SCHEMA_VERSION,
            "runner_version": ORACLE_RUNNER_VERSION,
            "created_at": self.utc_now(),
            "updated_at": self.utc_now(),
            "plan_fingerprint": plan_fingerprint,
            **plan_payload,
            "entries": [
                {
                    **task,
                    "state": "PENDING",
                    "attempts": 0,
                    "latest_result_ref": None,
                    "reason_codes": [],
                    "rejected_copy_ref": None,
                }
                for task in plan_payload["tasks"]
            ],
        }

    def _load_or_create_queue(
        self,
        entries: Sequence[Mapping[str, object]],
        *,
        corpus_manifest_sha256: str,
    ) -> dict[str, object]:
        queue_path = self.config.output_dir / "oracle_queue.json"
        results_path = self.config.output_dir / "oracle_results.jsonl"
        proposed = self._new_queue(
            entries, corpus_manifest_sha256=corpus_manifest_sha256
        )
        if not queue_path.exists():
            if results_path.exists() and results_path.stat().st_size:
                raise OracleConfigurationError(
                    "oracle results exist without their durable queue"
                )
            _atomic_json(queue_path, proposed)
            return proposed
        if not self.config.resume:
            raise OracleConfigurationError(
                "output directory already contains an oracle queue; use --resume"
            )
        try:
            existing = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OracleConfigurationError(f"cannot resume oracle queue: {exc}") from exc
        if not isinstance(existing, dict):
            raise OracleConfigurationError("oracle queue must be a JSON object")
        if (
            existing.get("schema_version") != ORACLE_QUEUE_SCHEMA_VERSION
            or existing.get("plan_fingerprint") != proposed["plan_fingerprint"]
        ):
            raise OracleConfigurationError(
                "oracle queue plan differs from the current corpus/backend"
            )
        queue_entries = existing.get("entries")
        if not isinstance(queue_entries, list) or len(queue_entries) != len(entries):
            raise OracleConfigurationError("oracle queue entries are invalid")
        for entry in queue_entries:
            if not isinstance(entry, dict):
                raise OracleConfigurationError("oracle queue entry is invalid")
            state = entry.get("state")
            if state not in {"PENDING", "RUNNING", "ACCEPTED", "REJECTED"}:
                raise OracleConfigurationError("oracle queue state is invalid")
            if state == "RUNNING" or (
                state == "REJECTED" and self.config.retry_rejected
            ):
                entry["state"] = "PENDING"
                entry["reason_codes"] = []
                entry["rejected_copy_ref"] = None
            elif state in {"ACCEPTED", "REJECTED"}:
                record = _load_record(
                    self.config.output_dir, entry.get("latest_result_ref")
                )
                if (
                    record is None
                    or record.get("status") != state
                    or record.get("task_id") != entry.get("task_id")
                    or record.get("task_fingerprint")
                    != entry.get("task_fingerprint")
                    or record.get("backend_fingerprint")
                    != self.backend_fingerprint
                ):
                    entry["state"] = "PENDING"
                    entry["latest_result_ref"] = None
                    entry["reason_codes"] = []
                    entry["rejected_copy_ref"] = None
        existing["updated_at"] = self.utc_now()
        _atomic_json(queue_path, existing)
        return existing

    def _write_queue(self, queue: dict[str, object]) -> None:
        queue["updated_at"] = self.utc_now()
        _atomic_json(self.config.output_dir / "oracle_queue.json", queue)

    def _rejection_copy(
        self,
        *,
        task_entry: Mapping[str, object],
        task_fingerprint: str,
        record: Mapping[str, object],
        record_ref: str,
    ) -> str:
        source = self.config.corpus / str(task_entry["path"])
        rejected_root = Path(self.config.rejected_corpus_dir)
        tasks_root = rejected_root / "tasks"
        tasks_root.mkdir(parents=True, exist_ok=True)
        base_name = str(task_entry["task_id"])
        destination = tasks_root / base_name
        if destination.exists():
            suffix = task_fingerprint[:12]
            destination = tasks_root / f"{base_name}--{suffix}"
            serial = 1
            while destination.exists():
                destination = tasks_root / f"{base_name}--{suffix}-{serial:03d}"
                serial += 1
        temporary = tasks_root / (
            f".{destination.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        if source.is_dir():
            shutil.copytree(source, temporary, symlinks=True)
        else:
            temporary.mkdir(parents=True)
            _atomic_text(temporary / "SOURCE_MISSING.txt", f"{source}\n")
        rejection = {
            "schema_version": REJECTED_CORPUS_SCHEMA_VERSION,
            "task_id": task_entry["task_id"],
            "mode": task_entry["mode"],
            "task_fingerprint": task_fingerprint,
            "source_task": str(source),
            "backend": self.config.backend,
            "evidence_class": self.evidence_class.value,
            "real_anchor": False,
            "record_ref": record_ref,
            "reason_codes": list(record.get("reason_codes", [])),
            "reason_details": list(record.get("reason_details", [])),
            "rejected_at": record.get("finished_at", self.utc_now()),
        }
        _atomic_json(temporary / "rejection.json", rejection)
        os.replace(temporary, destination)
        try:
            copy_ref = str(destination.relative_to(self.config.output_dir))
        except ValueError:
            copy_ref = str(destination)
        registry_path = rejected_root / "rejected_manifest.json"
        if registry_path.exists():
            try:
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                registry = None
        else:
            registry = None
        if not isinstance(registry, dict) or not isinstance(
            registry.get("rejections"), list
        ):
            registry = {
                "schema_version": REJECTED_CORPUS_SCHEMA_VERSION,
                "source_corpus": str(self.config.corpus),
                "rejections": [],
            }
        registry["updated_at"] = self.utc_now()
        registry["rejections"].append({**rejection, "copy_ref": copy_ref})
        registry["rejection_count"] = len(registry["rejections"])
        _atomic_json(registry_path, registry)
        return copy_ref

    def _failure_record(
        self,
        *,
        entry: Mapping[str, object],
        task_fingerprint: str,
        attempt: int,
        started_at: str,
        elapsed: float,
        error: Exception,
    ) -> dict[str, object]:
        if isinstance(error, OracleTaskError):
            reason_code = "TASK_PACKAGE_INVALID"
        elif isinstance(error, OracleExecutionError):
            reason_code = "ORACLE_EXECUTION_ERROR"
        else:
            reason_code = "ORACLE_BACKEND_EXCEPTION"
        return {
            "schema_version": ORACLE_TASK_SCHEMA_VERSION,
            "task_id": entry["task_id"],
            "mode": entry["mode"],
            "task_fingerprint": task_fingerprint,
            "attempt": attempt,
            "backend": self.config.backend,
            "backend_fingerprint": self.backend_fingerprint,
            "evidence_class": self.evidence_class.value,
            "real_anchor": False,
            "status": "REJECTED",
            "started_at": started_at,
            "finished_at": self.utc_now(),
            "wall_time_seconds": max(0.0, elapsed),
            "expected": None,
            "observations": None,
            "checks": [],
            "reason_codes": [reason_code],
            "reason_details": [f"{type(error).__name__}: {error}"],
            "error": {
                "type": type(error).__name__,
                "detail": str(error),
            },
        }

    def _validate_one(
        self,
        entry: Mapping[str, object],
        queue_entry: dict[str, object],
        *,
        timeout_seconds: float | None,
    ) -> tuple[dict[str, object], str]:
        attempt = int(queue_entry.get("attempts", 0)) + 1
        queue_entry["attempts"] = attempt
        task_id = str(entry["task_id"])
        reference = _record_ref(self.config.output_dir, task_id, attempt)
        run_dir = self.config.output_dir / Path(reference).parent
        run_dir.mkdir(parents=True, exist_ok=True)
        started_at = self.utc_now()
        start = self.monotonic()
        task_fingerprint = str(queue_entry["task_fingerprint"])
        try:
            task = _prepare_task(
                self.config.corpus, entry, task_fingerprint
            )
            expected = expected_gate_statuses(task.acceptance)
            raw = self.backend.validate(
                task,
                run_dir=run_dir,
                timeout_seconds=timeout_seconds,
            )
            if not isinstance(raw, Mapping):
                raise OracleExecutionError("oracle backend must return a mapping")
            observations = _normalise_observations(raw)
            checks, reason_codes, reason_details = _evaluate_observations(
                expected, observations
            )
            accepted = not reason_codes
            record = {
                "schema_version": ORACLE_TASK_SCHEMA_VERSION,
                "task_id": task.task_id,
                "mode": task.mode,
                "operator": task.operator,
                "seed": task.seed,
                "task_fingerprint": task.task_fingerprint,
                "attempt": attempt,
                "backend": self.config.backend,
                "backend_fingerprint": self.backend_fingerprint,
                "evidence_class": self.evidence_class.value,
                "real_anchor": bool(accepted and self.real_anchor_authorized),
                "status": "ACCEPTED" if accepted else "REJECTED",
                "started_at": started_at,
                "finished_at": self.utc_now(),
                "wall_time_seconds": max(0.0, self.monotonic() - start),
                "gate_scope": list(
                    mode_gate_scope(
                        task.mode,
                        requires_cosim=bool(
                            task.acceptance.get("requires_cosim", False)
                        ),
                    )
                ),
                "expected": expected,
                "observations": observations,
                "checks": checks,
                "reason_codes": reason_codes,
                "reason_details": reason_details,
            }
        except Exception as exc:
            record = self._failure_record(
                entry=entry,
                task_fingerprint=task_fingerprint,
                attempt=attempt,
                started_at=started_at,
                elapsed=self.monotonic() - start,
                error=exc,
            )
        if record["status"] == "REJECTED":
            try:
                copy_ref = self._rejection_copy(
                    task_entry=entry,
                    task_fingerprint=task_fingerprint,
                    record=record,
                    record_ref=reference,
                )
            except Exception as exc:
                record["reason_codes"] = [
                    *record.get("reason_codes", []),
                    "REJECTED_CORPUS_COPY_FAILED",
                ]
                record["reason_details"] = [
                    *record.get("reason_details", []),
                    f"rejected corpus copy failed: {type(exc).__name__}: {exc}",
                ]
                copy_ref = None
            record["rejected_copy_ref"] = copy_ref
        _atomic_json(self.config.output_dir / reference, record)
        _append_jsonl(self.config.output_dir / "oracle_results.jsonl", record)
        return record, reference

    def _summary(
        self,
        queue: Mapping[str, object],
        *,
        manifest: Mapping[str, object],
        corpus_manifest_sha256: str,
        attempted_this_run: int,
        resumed_tasks: int,
        stopped_reason: str | None,
        elapsed: float,
    ) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
        entries = queue["entries"]
        assert isinstance(entries, list)
        accepted: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        pending: list[str] = []
        records: list[dict[str, object]] = []
        state_counts = {state: 0 for state in ("PENDING", "RUNNING", "ACCEPTED", "REJECTED")}
        for entry in entries:
            assert isinstance(entry, Mapping)
            state = str(entry.get("state"))
            state_counts[state] = state_counts.get(state, 0) + 1
            if state in {"PENDING", "RUNNING"}:
                pending.append(str(entry["task_id"]))
                continue
            record = _load_record(
                self.config.output_dir, entry.get("latest_result_ref")
            )
            if record is None:
                pending.append(str(entry["task_id"]))
                continue
            records.append(record)
            row = {
                "task_id": entry["task_id"],
                "mode": entry["mode"],
                "task_fingerprint": entry["task_fingerprint"],
                "record_ref": entry["latest_result_ref"],
                "reason_codes": list(record.get("reason_codes", [])),
                "reason_details": list(record.get("reason_details", [])),
                "real_anchor": bool(record.get("real_anchor", False)),
            }
            if state == "ACCEPTED":
                accepted.append(row)
            else:
                row["rejected_copy_ref"] = entry.get("rejected_copy_ref")
                rejected.append(row)
        accepted.sort(key=lambda item: str(item["task_id"]))
        rejected.sort(key=lambda item: str(item["task_id"]))
        records.sort(key=lambda item: str(item["task_id"]))
        real_anchors = [item for item in accepted if item["real_anchor"]]
        complete = not pending
        summary = {
            "schema_version": ORACLE_SUMMARY_SCHEMA_VERSION,
            "runner_version": ORACLE_RUNNER_VERSION,
            "status": "COMPLETE" if complete else "PARTIAL",
            "oracle_pass": bool(complete and not rejected),
            "generated_at": self.utc_now(),
            "configuration": self.config.public_dict(),
            "corpus": {
                "root": str(self.config.corpus),
                "schema_version": manifest.get("schema_version"),
                "manifest_sha256": corpus_manifest_sha256,
                "selected_tasks": len(entries),
            },
            "backend": {
                "name": self.config.backend,
                "fingerprint": self.backend_fingerprint,
                "evidence_class": self.evidence_class.value,
                "real_anchor_authorized": self.real_anchor_authorized,
                "disclaimer": (
                    "structured real Vitis evidence"
                    if self.real_anchor_authorized
                    else "fixture evidence only; never a real Vitis anchor"
                ),
            },
            "execution": {
                "serial": True,
                "queue_ref": "oracle_queue.json",
                "results_ref": "oracle_results.jsonl",
                "attempted_this_run": attempted_this_run,
                "resumed_tasks": resumed_tasks,
                "elapsed_seconds": max(0.0, elapsed),
                "stopped_reason": stopped_reason,
                "state_counts": state_counts,
            },
            "counts": {
                "accepted": len(accepted),
                "rejected": len(rejected),
                "pending": len(pending),
                "real_vitis_anchors": len(real_anchors),
                "fixture_acceptances": (
                    len(accepted) if not self.real_anchor_authorized else 0
                ),
            },
            "accepted": accepted,
            "rejected": rejected,
            "pending": sorted(pending),
            "real_anchor": {
                "authorized": self.real_anchor_authorized,
                "count": len(real_anchors),
                "task_ids": [str(item["task_id"]) for item in real_anchors],
            },
        }
        _atomic_json(self.config.output_dir / "summary.json", summary)
        _atomic_json(
            self.config.output_dir / "accepted.json",
            {
                "schema_version": ORACLE_SUMMARY_SCHEMA_VERSION,
                "evidence_class": self.evidence_class.value,
                "real_anchor_authorized": self.real_anchor_authorized,
                "count": len(accepted),
                "tasks": accepted,
            },
        )
        _atomic_json(
            self.config.output_dir / "rejected.json",
            {
                "schema_version": ORACLE_SUMMARY_SCHEMA_VERSION,
                "count": len(rejected),
                "tasks": rejected,
            },
        )
        return summary, tuple(records)

    def run(self) -> OracleOutcome:
        output = self.config.output_dir
        output.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(output / ".oracle.lock"):
            return self._run_locked()

    def _run_locked(self) -> OracleOutcome:
        start = self.monotonic()
        manifest, manifest_sha = self._load_manifest()
        selected = self._selected_entries(manifest)
        queue = self._load_or_create_queue(
            selected, corpus_manifest_sha256=manifest_sha
        )
        queue_entries = queue["entries"]
        assert isinstance(queue_entries, list)
        entry_by_id = {str(entry["task_id"]): entry for entry in selected}
        resumed_tasks = sum(
            isinstance(entry, Mapping)
            and entry.get("state") in {"ACCEPTED", "REJECTED"}
            for entry in queue_entries
        )
        attempted = 0
        stopped_reason: str | None = None
        if not self.config.prepare_only:
            for queue_entry in queue_entries:
                assert isinstance(queue_entry, dict)
                if queue_entry.get("state") != "PENDING":
                    continue
                elapsed = self.monotonic() - start
                if (
                    self.config.max_runtime_seconds is not None
                    and elapsed >= self.config.max_runtime_seconds
                ):
                    stopped_reason = "MAX_RUNTIME_REACHED"
                    break
                if (
                    self.config.max_tasks is not None
                    and attempted >= self.config.max_tasks
                ):
                    stopped_reason = "MAX_TASKS_REACHED"
                    break
                task_id = str(queue_entry["task_id"])
                task_entry = entry_by_id[task_id]
                queue_entry["state"] = "RUNNING"
                self._write_queue(queue)
                if (
                    self.config.backend == "vitis"
                    or getattr(self.backend, "requires_vitis_lock", False)
                ):
                    with _exclusive_lock(
                        Path("/tmp/llm4hls-v3d-vitis-serial.lock")
                    ):
                        if (
                            self.config.max_runtime_seconds is not None
                            and self.monotonic() - start
                            >= self.config.max_runtime_seconds
                        ):
                            queue_entry["state"] = "PENDING"
                            stopped_reason = "MAX_RUNTIME_REACHED"
                            self._write_queue(queue)
                            break
                        remaining = (
                            None
                            if self.config.max_runtime_seconds is None
                            else max(
                                0.001,
                                self.config.max_runtime_seconds
                                - (self.monotonic() - start),
                            )
                        )
                        record, reference = self._validate_one(
                            task_entry,
                            queue_entry,
                            timeout_seconds=remaining,
                        )
                else:
                    remaining = (
                        None
                        if self.config.max_runtime_seconds is None
                        else max(
                            0.001,
                            self.config.max_runtime_seconds
                            - (self.monotonic() - start),
                        )
                    )
                    record, reference = self._validate_one(
                        task_entry,
                        queue_entry,
                        timeout_seconds=remaining,
                    )
                queue_entry["state"] = str(record["status"])
                queue_entry["latest_result_ref"] = reference
                queue_entry["reason_codes"] = list(record["reason_codes"])
                queue_entry["rejected_copy_ref"] = record.get("rejected_copy_ref")
                attempted += 1
                self._write_queue(queue)
        elif any(
            isinstance(entry, Mapping) and entry.get("state") == "PENDING"
            for entry in queue_entries
        ):
            stopped_reason = "PREPARE_ONLY"
        if stopped_reason is None and any(
            isinstance(entry, Mapping) and entry.get("state") == "PENDING"
            for entry in queue_entries
        ):
            stopped_reason = "QUEUE_PENDING"
        summary, records = self._summary(
            queue,
            manifest=manifest,
            corpus_manifest_sha256=manifest_sha,
            attempted_this_run=attempted,
            resumed_tasks=resumed_tasks,
            stopped_reason=stopped_reason,
            elapsed=self.monotonic() - start,
        )
        return OracleOutcome(summary=summary, records=records)


def validate_corpus(
    config: OracleConfig, *, backend: OracleBackend | None = None
) -> OracleOutcome:
    """Convenience API for one serial corpus-oracle run."""

    return CorpusOracleValidator(config, backend=backend).run()


def _split_values(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(
        token.strip()
        for value in values
        for token in str(value).split(",")
        if token.strip()
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m llm4hls_agent.v3d_oracle_validator",
        description=(
            "Validate V3-D baseline/golden mode gates through a durable serial "
            "queue. Demo/deterministic results are fixtures, never real anchors."
        ),
    )
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--backend", choices=("demo", "deterministic", "vitis"), default="deterministic"
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-rejected", action="store_true")
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument(
        "--max-runtime",
        "--max-runtime-seconds",
        dest="max_runtime_seconds",
        type=float,
    )
    parser.add_argument("--rejected-corpus-dir")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--mode", action="append", default=[])
    parser.add_argument("--compiler", default="g++")
    parser.add_argument(
        "--vitis-root",
        default=os.environ.get("XILINX_VITIS", "/opt/xilinx/2025.2/Vitis"),
    )
    parser.add_argument("--csim-timeout", type=float, default=60.0)
    parser.add_argument("--synth-timeout", type=float, default=900.0)
    parser.add_argument("--cosim-timeout", type=float, default=1200.0)
    parser.add_argument("--toolchain-id", default="Vitis 2025.2")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="write the durable queue without executing any oracle task",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stream = stdout or sys.stdout
    args = _parser().parse_args(argv)
    try:
        config = OracleConfig(
            corpus=args.corpus,
            output_dir=args.output_dir,
            backend=args.backend,
            resume=args.resume,
            retry_rejected=args.retry_rejected,
            max_tasks=args.max_tasks,
            max_runtime_seconds=args.max_runtime_seconds,
            rejected_corpus_dir=args.rejected_corpus_dir,
            task_filters=_split_values(args.task),
            mode_filters=_split_values(args.mode),
            compiler=args.compiler,
            vitis_root=args.vitis_root,
            csim_timeout_seconds=args.csim_timeout,
            synth_timeout_seconds=args.synth_timeout,
            cosim_timeout_seconds=args.cosim_timeout,
            toolchain_id=args.toolchain_id,
            prepare_only=args.prepare_only,
        )
        outcome = validate_corpus(config)
    except Exception as exc:
        print(
            _canonical_json(
                {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 3
    counts = outcome.summary["counts"]
    assert isinstance(counts, Mapping)
    print(
        _canonical_json(
            {
                "status": outcome.summary["status"],
                "oracle_pass": outcome.summary["oracle_pass"],
                "accepted": counts["accepted"],
                "rejected": counts["rejected"],
                "pending": counts["pending"],
                "real_vitis_anchors": counts["real_vitis_anchors"],
                "output_dir": str(config.output_dir),
                "summary_ref": "summary.json",
                "queue_ref": "oracle_queue.json",
            }
        ),
        file=stream,
    )
    return 0 if int(counts["rejected"]) == 0 else 2


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - covered through main()
    main_entry()
