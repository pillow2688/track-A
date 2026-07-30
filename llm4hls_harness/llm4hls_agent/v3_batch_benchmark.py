"""Resumable, evidence-aware batch benchmark runner for V3 task packages.

The module deliberately keeps corpus discovery and aggregation independent of
the optional corpus builder.  Any directory tree containing loadable
``task.toml`` packages can be benchmarked directly.

``demo`` and ``deterministic`` are offline fixtures.  They are useful for
proving orchestration and report generation, but their rows are never included
in the real-evidence headline.  Only a ``vitis`` execution with structured
terminal backend evidence is eligible for that headline.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import tomllib
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Protocol, Sequence, TextIO

from .full_agent_manifest import (
    FullAgentManifest,
    FullAgentManifestError,
    load_full_agent_manifest,
)
from .task import PublicTask, load_public_task
from .v3_continuation_admission import load_continuation_admission
from .v3_experience_v2_runtime import validate_ranker_admission_manifest
from .v3_planner_action import (
    LIVE_PLANNER_ACTION_SCHEMA,
    LIVE_PLANNER_COMPLETED_SCHEMA,
    LIVE_PLANNER_FAILURE_SCHEMA,
    LIVE_PLANNER_OUTCOME_SCHEMA,
    LIVE_PLANNER_STARTED_SCHEMA,
    LIVE_PROVIDER_REQUEST_SCHEMA,
    PlannerActionError,
    completed_llm_action_ids,
    validate_live_planner_action_identity,
)


RUN_SCHEMA = "v3d.benchmark-run.v1"
SUMMARY_SCHEMA = "v3d.benchmark-summary.v1"
PLAN_SCHEMA = "v3d.benchmark-plan.v1"
RUNNER_FINGERPRINT = "llm4hls-v3d-batch-benchmark:v6"
REAL_EVIDENCE_AUTHORITY = "V3_PROTOTYPE_CLI_VITIS_V1"

# These files contain the execution semantics which can change a benchmark
# outcome without changing a task package or model name.  Their content hashes
# are part of every run identity.  In particular this covers the PhaseRouter,
# prompt construction/response parsing, Planner journals and Vitis parsing.
_CRITICAL_IMPLEMENTATION_MODULES = (
    "v3_batch_benchmark.py",
    "full_agent_manifest.py",
    "v3_prototype_cli.py",
    "token_policy_experiment_cli.py",
    "v3_prototype.py",
    "v3_experience.py",
    "v3_experience_store.py",
    "v3_experience_guidance.py",
    "v3_experience_v2.py",
    "v3_experience_v2_runtime.py",
    "v3_strategy_ranker_v3.py",
    "v3_continuation.py",
    "v3_continuation_v2.py",
    "v3_continuation_admission.py",
    "v3_phase_router.py",
    "v3_planner.py",
    "v3_planner_action.py",
    "v3_openai_planner.py",
    "openai_provider.py",
    "repair.py",
    "task.py",
    "budget.py",
    "tools.py",
    "vitis.py",
    "final_certification.py",
    "runtime_control.py",
)

_MODES = {"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE"}
_VALIDATION_PROFILES = {"strict", "fast-experiment"}
_FINAL_VALIDATION_POLICIES = {"task_contract", "full_internal_audit"}
_EVIDENCE_MEMORY_MODES = {"off", "on"}
_EXPERIENCE_MODES = {"off", "shadow", "guided"}
_EXPERIENCE_TASK_SPLITS = {"train", "dev", "hidden_like"}
_EXPERIENCE_RANKER_VERSIONS = {"v1", "v3"}
_CONTINUATION_POLICY_MODES = {"off", "shadow", "enforce"}
_CONTINUATION_POLICY_VERSIONS = {"v2"}
_MODE_ALIASES = {
    "REPAIR": "REPAIR",
    "BUGFIX": "REPAIR",
    "SYNTH_FIX": "SYNTH_FIX",
    "SYNTH-FIX": "SYNTH_FIX",
    "SYNTHESIS_FIX": "SYNTH_FIX",
    "STRUCTURAL": "STRUCTURAL_FIX",
    "STRUCTURAL_FIX": "STRUCTURAL_FIX",
    "STRUCTURAL-FIX": "STRUCTURAL_FIX",
    "OPTIMIZE": "OPTIMIZE",
    "OPTIMIZATION": "OPTIMIZE",
}
_TASK_TYPE_MODES = {
    "repair": "REPAIR",
    "bugfix": "REPAIR",
    "synth_fix": "SYNTH_FIX",
    "synthesis_fix": "SYNTH_FIX",
    "structural": "STRUCTURAL_FIX",
    "structural_fix": "STRUCTURAL_FIX",
    "optimize": "OPTIMIZE",
    "optimization": "OPTIMIZE",
}
_TERMINAL_STATUSES = {"DONE", "FAILED", "ERROR"}
_PASS_STATUSES = {"PASS", "PASSED", "SUCCESS", "SUCCEEDED"}


class BenchmarkError(RuntimeError):
    """Base error for invalid batch configuration or durable state."""


class BenchmarkExecutionError(BenchmarkError):
    """Raised when one executor invocation has no structured result."""

    def __init__(
        self,
        message: str,
        *,
        partial_report: Mapping[str, object] | None = None,
        process_cleanup: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_report = (
            dict(partial_report) if partial_report is not None else None
        )
        self.process_cleanup = (
            dict(process_cleanup) if process_cleanup is not None else None
        )


class BenchmarkExecutionTimeout(BenchmarkExecutionError):
    """Raised after the executor deadline and scoped process-tree cleanup."""


class EvidenceClass(str, Enum):
    """Mutually exclusive evidence populations used by every aggregate."""

    DEMO = "DEMO"
    DETERMINISTIC = "DETERMINISTIC"
    REAL = "REAL"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise BenchmarkError(f"cannot hash benchmark implementation file: {path}") from exc


def _experience_store_snapshot(
    path: Path | str | None,
    *,
    error_type: type[Exception] = BenchmarkError,
) -> tuple[Path | None, dict[str, object]]:
    """Resolve and content-bind one immutable retrieval-store snapshot."""

    if path is None:
        return None, {
            "role": "READ_ONLY_SEED",
            "present": False,
            "size_bytes": 0,
            "sha256": None,
        }
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise error_type(
            f"experience_store must be an existing regular file: {resolved}"
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise error_type(f"cannot snapshot experience_store: {resolved}") from exc
    return resolved, {
        "role": "READ_ONLY_SEED",
        "present": True,
        "size_bytes": len(data),
        "sha256": _sha256_bytes(data),
    }


def _admission_manifest_snapshot(
    path: Path | str | None,
    *,
    role: str,
    label: str,
    error_type: type[Exception] = BenchmarkError,
) -> tuple[Path | None, dict[str, object]]:
    """Resolve and content-bind one immutable component admission decision."""

    if path is None:
        return None, {
            "role": role,
            "present": False,
            "size_bytes": 0,
            "sha256": None,
        }
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise error_type(f"{label} must be an existing regular file: {resolved}")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise error_type(f"cannot snapshot {label}: {resolved}") from exc
    return resolved, {
        "role": role,
        "present": True,
        "size_bytes": len(data),
        "sha256": _sha256_bytes(data),
    }


def _validate_component_admissions(
    *,
    continuation_path: Path | None,
    experience_path: Path | None,
    experience_seed_path: Path | None,
    error_type: type[Exception],
) -> None:
    """Fail before scheduling when an admission artifact is malformed."""

    try:
        if continuation_path is not None:
            load_continuation_admission(continuation_path)
        if experience_path is not None:
            if experience_seed_path is None:
                raise ValueError(
                    "experience admission requires a frozen experience store"
                )
            decoded = json.loads(experience_path.read_text(encoding="utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError("experience admission manifest must be an object")
            validate_ranker_admission_manifest(
                decoded,
                seed_path=experience_seed_path,
                manifest_path=experience_path,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise error_type(
            f"component admission preflight failed: {type(exc).__name__}: {exc}"
        ) from exc


def _load_matching_full_agent_manifest(
    path: Path | str | None,
    *,
    backend: str,
    evidence_memory_mode: str,
    continuation_policy_mode: str,
    continuation_policy_version: str,
    continuation_admission_path: Path | None,
    continuation_admission_sha256: object,
    experience_mode: str,
    experience_store_path: Path | None,
    experience_store_sha256: object,
    experience_ranker_version: str,
    experience_admission_path: Path | None,
    experience_admission_sha256: object,
) -> FullAgentManifest | None:
    """Load Full Agent authority only when every runtime input matches it."""

    if path is None:
        return None
    try:
        manifest = load_full_agent_manifest(path, require_ready=True)
    except FullAgentManifestError as exc:
        raise ValueError(f"Full Agent manifest preflight failed: {exc}") from exc
    expected_runtime = {
        "backend": (backend, "vitis"),
        "evidence_memory_mode": (evidence_memory_mode, "on"),
        "continuation_policy_mode": (continuation_policy_mode, "enforce"),
        "continuation_policy_version": (continuation_policy_version, "v2"),
        "experience_mode": (experience_mode, "guided"),
        "experience_ranker_version": (experience_ranker_version, "v3"),
    }
    mismatches = [
        name
        for name, (observed, expected) in expected_runtime.items()
        if observed != expected
    ]
    if mismatches:
        raise ValueError(
            "Full Agent manifest runtime arguments mismatch: "
            + ", ".join(mismatches)
        )
    runtime_artifacts = {
        "a2_admission": (
            continuation_admission_path,
            continuation_admission_sha256,
        ),
        "a3_store": (experience_store_path, experience_store_sha256),
        "a3_admission": (
            experience_admission_path,
            experience_admission_sha256,
        ),
    }
    for role, (runtime_path, runtime_sha256) in runtime_artifacts.items():
        expected = manifest.artifact(role)
        if runtime_path != expected.path or runtime_sha256 != expected.sha256:
            raise ValueError(
                f"Full Agent manifest artifact does not match runtime {role}"
            )
    return manifest


@lru_cache(maxsize=1)
def _implementation_facts() -> dict[str, object]:
    """Return secret-free, content-addressed facts for run identity.

    A static version string is insufficient for scientific resume: prompt,
    parser, Planner, or backend changes could otherwise reuse an old slot.
    File names and hashes are stable across installation locations.
    """

    module_root = Path(__file__).resolve().parent
    modules: dict[str, str] = {}
    for name in _CRITICAL_IMPLEMENTATION_MODULES:
        path = module_root / name
        modules[name] = _sha256_file(path) if path.is_file() else "MISSING"
    categories = {
        "batch_and_task_parsing": (
            modules["v3_batch_benchmark.py"],
            modules["full_agent_manifest.py"],
            modules["v3_prototype_cli.py"],
            modules["task.py"],
        ),
        "phase_router": (modules["v3_phase_router.py"],),
        "planner_and_prompts": (
            modules["v3_planner.py"],
            modules["v3_planner_action.py"],
            modules["v3_openai_planner.py"],
            modules["openai_provider.py"],
            modules["repair.py"],
        ),
        "experience_layer": (
            modules["v3_experience.py"],
            modules["v3_experience_store.py"],
            modules["v3_experience_guidance.py"],
            modules["v3_experience_v2.py"],
            modules["v3_experience_v2_runtime.py"],
            modules["v3_strategy_ranker_v3.py"],
        ),
        "continuation_layer": (
            modules["v3_continuation.py"],
            modules["v3_continuation_v2.py"],
            modules["v3_continuation_admission.py"],
        ),
        "backend_and_accounting": (
            modules["vitis.py"],
            modules["tools.py"],
            modules["budget.py"],
            modules["v3_prototype.py"],
            modules["final_certification.py"],
        ),
    }
    return {
        "schema_version": "v3d.implementation-facts.v1",
        "modules": modules,
        "category_sha256": {
            name: _sha256_json(list(digests))
            for name, digests in sorted(categories.items())
        },
    }


def _implementation_fingerprint() -> str:
    return _sha256_json(_implementation_facts())


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _subprocess_text(value: object) -> str:
    """Normalize partial ``TimeoutExpired`` streams without hiding timeout."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _enable_child_subreaper() -> bool:
    """Best-effort Linux subreaper for exact-PGID orphan reaping."""

    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        # PR_SET_CHILD_SUBREAPER from linux/prctl.h.
        return int(libc.prctl(36, 1, 0, 0, 0)) == 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _process_group_members(pgid: int) -> list[int]:
    """Return Linux process IDs in one exact process group."""

    if not sys.platform.startswith("linux") or pgid <= 1:
        return []
    members: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_value = (entry / "stat").read_text(encoding="utf-8")
            closing = stat_value.rfind(")")
            fields = stat_value[closing + 2 :].split()
            process_group = int(fields[2])
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            continue
        if process_group == pgid:
            members.append(int(entry.name))
    return sorted(members)


def _reap_process_group_children(pgid: int) -> list[int]:
    reaped: list[int] = []
    if os.name != "posix" or pgid <= 1:
        return reaped
    while True:
        try:
            pid, _status = os.waitpid(-pgid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            break
        if pid <= 0:
            break
        reaped.append(pid)
    return reaped


def _signal_process_group(pgid: int, selected_signal: int) -> bool:
    if pgid <= 1 or pgid == os.getpgrp():
        raise BenchmarkExecutionError(
            "refusing to signal an unsafe executor process group"
        )
    try:
        os.killpg(pgid, selected_signal)
    except ProcessLookupError:
        return False
    return True


def _terminate_executor_process_group(
    process: subprocess.Popen[str],
    *,
    pgid: int,
    term_grace_seconds: float = 5.0,
    kill_grace_seconds: float = 5.0,
) -> tuple[str, str, dict[str, object]]:
    """TERM, then KILL, one run-owned process group and reap what we own."""

    term_sent = False
    kill_sent = False
    stdout = ""
    stderr = ""
    members_before = _process_group_members(pgid)
    if os.name == "posix":
        term_sent = _signal_process_group(pgid, signal.SIGTERM)
    else:
        process.terminate()
        term_sent = True
    try:
        stdout, stderr = process.communicate(timeout=term_grace_seconds)
    except subprocess.TimeoutExpired as exc:
        stdout = _subprocess_text(exc.stdout)
        stderr = _subprocess_text(exc.stderr)

    members_after_term = _process_group_members(pgid)
    if process.poll() is None or members_after_term:
        if os.name == "posix":
            kill_sent = _signal_process_group(pgid, signal.SIGKILL)
        else:
            process.kill()
            kill_sent = True
        try:
            final_stdout, final_stderr = process.communicate(
                timeout=kill_grace_seconds
            )
            stdout = _subprocess_text(final_stdout) or stdout
            stderr = _subprocess_text(final_stderr) or stderr
        except subprocess.TimeoutExpired as exc:
            stdout = _subprocess_text(exc.stdout) or stdout
            stderr = _subprocess_text(exc.stderr) or stderr
            process.kill()
            try:
                final_stdout, final_stderr = process.communicate(timeout=1.0)
                stdout = _subprocess_text(final_stdout) or stdout
                stderr = _subprocess_text(final_stderr) or stderr
            except subprocess.TimeoutExpired:
                pass

    reaped_descendants = _reap_process_group_children(pgid)
    deadline = time.monotonic() + max(0.0, kill_grace_seconds)
    members_after = _process_group_members(pgid)
    while members_after and time.monotonic() < deadline:
        _reap_process_group_children(pgid)
        time.sleep(0.01)
        members_after = _process_group_members(pgid)
    cleanup = {
        "schema_version": "v3d.executor-process-cleanup.v1",
        "pid": process.pid,
        "pgid": pgid,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "term_grace_seconds": float(term_grace_seconds),
        "kill_grace_seconds": float(kill_grace_seconds),
        "parent_reaped": process.poll() is not None,
        "return_code": process.returncode,
        "members_before": members_before,
        "members_after_term": members_after_term,
        "members_after": members_after,
        "reaped_descendant_pids": reaped_descendants,
        "process_tree_cleaned": process.poll() is not None and not members_after,
    }
    return _subprocess_text(stdout), _subprocess_text(stderr), cleanup


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(path, _canonical_json(_plain_json(value)) + "\n")


def _append_jsonl(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical_json(_plain_json(value)) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _safe_slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return (slug or fallback)[:64]


def _normalise_mode(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _MODE_ALIASES.get(value.strip().upper().replace(" ", "_"))


def _normalise_split(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unspecified"
    split = value.strip().casefold()
    return {"val": "validation", "valid": "validation"}.get(split, split)


def _as_nonnegative_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def _as_finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


@dataclass(frozen=True)
class TaskDescriptor:
    """One discovered task plus benchmark-only metadata."""

    directory: Path
    relative_path: str
    task_id: str
    task_type: str
    difficulty: int | None
    split: str
    expected_mode: str | None
    expected_mode_source: str | None
    task_fingerprint: str
    task: PublicTask | None = field(compare=False, repr=False)
    algorithm_family: str | None = None
    algorithm_family_source: str | None = None
    load_error_type: str | None = None
    load_error_detail: str | None = None

    @property
    def loadable(self) -> bool:
        return self.task is not None and self.load_error_type is None


@dataclass(frozen=True)
class BenchmarkRunSpec:
    """Immutable identity passed to an injected executor."""

    task: PublicTask
    descriptor: TaskDescriptor
    model: str
    repeat_index: int
    backend: str
    run_id: str
    run_fingerprint: str
    run_dir: Path


class BenchmarkExecutor(Protocol):
    """Small injection boundary used by fake, deterministic and Vitis runs."""

    evidence_class: EvidenceClass
    requires_vitis_lock: bool

    def fingerprint(self) -> str: ...

    def execute(
        self,
        spec: BenchmarkRunSpec,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class BenchmarkConfig:
    """Selection, repetition, output and stopping policy for one batch."""

    corpus: Path | str | Sequence[Path | str]
    output_dir: Path | str
    models: Sequence[str] = ("deterministic-fixture-v1",)
    repeats: int = 1
    backend: str = "deterministic"
    splits: Sequence[str] = ("all",)
    mode_filters: Sequence[str] = ()
    task_filters: Sequence[str] = ()
    difficulty_filters: Sequence[str | int] = ()
    model_filters: Sequence[str] = ()
    resume: bool = False
    retry_failures: bool = False
    max_tasks: int | None = None
    max_runtime_seconds: float | None = None
    validation_profile: str = "strict"
    final_validation_policy: str = "task_contract"
    evidence_memory_mode: str = "on"
    max_planner_rounds: int | None = None
    max_no_improvement_rounds: int = 2
    enable_final_fallback: bool = False
    continuation_policy_mode: str = "shadow"
    continuation_policy_version: str = "v2"
    continuation_admission_manifest: Path | str | None = None
    continuation_admission_sha256: str | None = field(
        init=False, default=None, repr=False
    )
    continuation_admission_size_bytes: int = field(
        init=False, default=0, repr=False
    )
    full_agent_manifest: Path | str | None = None
    full_agent_manifest_sha256: str | None = field(
        init=False, default=None, repr=False
    )
    full_agent_artifact_sha256s: Mapping[str, str] = field(
        init=False, default_factory=dict, repr=False
    )
    _full_agent_manifest_snapshot: FullAgentManifest | None = field(
        init=False, default=None, repr=False, compare=False
    )
    experience_mode: str = "shadow"
    experience_store: Path | str | None = None
    experience_ranker_version: str = "v1"
    experience_admission_manifest: Path | str | None = None
    experience_task_split: str | None = None
    experience_store_sha256: str | None = field(
        init=False, default=None, repr=False
    )
    experience_store_size_bytes: int = field(
        init=False, default=0, repr=False
    )
    experience_admission_sha256: str | None = field(
        init=False, default=None, repr=False
    )
    experience_admission_size_bytes: int = field(
        init=False, default=0, repr=False
    )

    def __post_init__(self) -> None:
        raw_corpus = self.corpus
        corpus_items: Sequence[Path | str]
        if isinstance(raw_corpus, (str, Path)):
            corpus_items = (raw_corpus,)
        else:
            corpus_items = tuple(raw_corpus)
        corpora = tuple(Path(item).expanduser().resolve() for item in corpus_items)
        if not corpora:
            raise ValueError("at least one corpus path is required")
        models = tuple(dict.fromkeys(str(item).strip() for item in self.models))
        if not models or any(not item for item in models):
            raise ValueError("models must contain non-empty names")
        repeats = int(self.repeats)
        if repeats <= 0:
            raise ValueError("repeats must be positive")
        backend = str(self.backend).strip().casefold()
        if backend not in {"demo", "deterministic", "vitis"}:
            raise ValueError("backend must be demo, deterministic, or vitis")
        validation_profile = str(self.validation_profile).strip().casefold()
        if validation_profile not in _VALIDATION_PROFILES:
            raise ValueError(
                "validation_profile must be strict or fast-experiment"
            )
        final_validation_policy = (
            str(self.final_validation_policy).strip().casefold()
        )
        if final_validation_policy not in _FINAL_VALIDATION_POLICIES:
            raise ValueError(
                "final_validation_policy must be task_contract or "
                "full_internal_audit"
            )
        evidence_memory_mode = (
            str(self.evidence_memory_mode).strip().casefold()
        )
        if evidence_memory_mode not in _EVIDENCE_MEMORY_MODES:
            raise ValueError("evidence_memory_mode must be off or on")
        max_planner_rounds = self.max_planner_rounds
        if max_planner_rounds is not None:
            max_planner_rounds = int(max_planner_rounds)
            if max_planner_rounds <= 0:
                raise ValueError("max_planner_rounds must be positive")
        max_no_improvement_rounds = int(self.max_no_improvement_rounds)
        if max_no_improvement_rounds <= 0:
            raise ValueError(
                "max_no_improvement_rounds must be positive"
            )
        continuation_policy_mode = (
            str(self.continuation_policy_mode).strip().casefold()
        )
        if continuation_policy_mode not in _CONTINUATION_POLICY_MODES:
            raise ValueError(
                "continuation_policy_mode must be off, shadow, or enforce"
            )
        continuation_policy_version = (
            str(self.continuation_policy_version).strip().casefold()
        )
        if continuation_policy_version not in _CONTINUATION_POLICY_VERSIONS:
            raise ValueError("continuation_policy_version must be v2")
        if (
            evidence_memory_mode == "off"
            and continuation_policy_mode != "off"
        ):
            raise ValueError(
                "continuation requires evidence memory on"
            )
        (
            continuation_admission_manifest,
            continuation_admission_snapshot,
        ) = _admission_manifest_snapshot(
            self.continuation_admission_manifest,
            role="CONTINUATION_ENFORCE_ADMISSION",
            label="continuation_admission_manifest",
            error_type=ValueError,
        )
        if continuation_policy_mode == "enforce":
            if continuation_admission_manifest is None:
                raise ValueError(
                    "continuation enforce requires an admission manifest"
                )
        elif continuation_admission_manifest is not None:
            raise ValueError(
                "continuation admission manifest is valid only in enforce mode"
            )
        experience_mode = str(self.experience_mode).strip().casefold()
        if experience_mode not in _EXPERIENCE_MODES:
            raise ValueError("experience_mode must be off, shadow, or guided")
        experience_ranker_version = (
            str(self.experience_ranker_version).strip().casefold()
        )
        if experience_ranker_version not in _EXPERIENCE_RANKER_VERSIONS:
            raise ValueError("experience_ranker_version must be v1 or v3")
        experience_task_split = self.experience_task_split
        if experience_task_split is not None:
            experience_task_split = (
                str(experience_task_split)
                .strip()
                .casefold()
                .replace("-", "_")
            )
            if experience_task_split not in _EXPERIENCE_TASK_SPLITS:
                raise ValueError(
                    "experience_task_split must be train, dev, or hidden_like"
                )
        experience_store, experience_snapshot = _experience_store_snapshot(
            self.experience_store,
            error_type=ValueError,
        )
        (
            experience_admission_manifest,
            experience_admission_snapshot,
        ) = _admission_manifest_snapshot(
            self.experience_admission_manifest,
            role="EXPERIENCE_GUIDED_ADMISSION",
            label="experience_admission_manifest",
            error_type=ValueError,
        )
        if (
            experience_mode != "off"
            and experience_ranker_version == "v3"
            and experience_store is None
        ):
            raise ValueError("experience ranker v3 requires an experience store")
        if experience_mode == "guided":
            if experience_ranker_version != "v3":
                raise ValueError(
                    "guided experience requires the Gate-controlled v3 ranker"
                )
            if experience_admission_manifest is None:
                raise ValueError(
                    "guided experience requires an admission manifest"
                )
        _validate_component_admissions(
            continuation_path=continuation_admission_manifest,
            experience_path=experience_admission_manifest,
            experience_seed_path=experience_store,
            error_type=ValueError,
        )
        full_agent_manifest = _load_matching_full_agent_manifest(
            self.full_agent_manifest,
            backend=backend,
            evidence_memory_mode=evidence_memory_mode,
            continuation_policy_mode=continuation_policy_mode,
            continuation_policy_version=continuation_policy_version,
            continuation_admission_path=continuation_admission_manifest,
            continuation_admission_sha256=continuation_admission_snapshot[
                "sha256"
            ],
            experience_mode=experience_mode,
            experience_store_path=experience_store,
            experience_store_sha256=experience_snapshot["sha256"],
            experience_ranker_version=experience_ranker_version,
            experience_admission_path=experience_admission_manifest,
            experience_admission_sha256=experience_admission_snapshot[
                "sha256"
            ],
        )
        max_tasks = self.max_tasks
        if max_tasks is not None and int(max_tasks) <= 0:
            raise ValueError("max_tasks must be positive when provided")
        max_runtime = self.max_runtime_seconds
        if max_runtime is not None:
            max_runtime = float(max_runtime)
            if not math.isfinite(max_runtime) or max_runtime <= 0:
                raise ValueError("max_runtime_seconds must be finite and positive")
        normalised_modes: list[str] = []
        for raw_mode in self.mode_filters:
            mode = _normalise_mode(raw_mode)
            if mode is None:
                raise ValueError(f"unsupported mode filter: {raw_mode}")
            if mode not in normalised_modes:
                normalised_modes.append(mode)
        # Parse once during construction so a malformed filter cannot silently
        # broaden an overnight batch to the entire corpus.
        _difficulty_values(self.difficulty_filters)
        object.__setattr__(self, "corpus", corpora)
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser().resolve())
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "repeats", repeats)
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "validation_profile", validation_profile)
        object.__setattr__(
            self, "final_validation_policy", final_validation_policy
        )
        object.__setattr__(
            self, "evidence_memory_mode", evidence_memory_mode
        )
        object.__setattr__(self, "max_planner_rounds", max_planner_rounds)
        object.__setattr__(
            self,
            "max_no_improvement_rounds",
            max_no_improvement_rounds,
        )
        object.__setattr__(
            self, "enable_final_fallback", bool(self.enable_final_fallback)
        )
        object.__setattr__(
            self, "continuation_policy_mode", continuation_policy_mode
        )
        object.__setattr__(
            self, "continuation_policy_version", continuation_policy_version
        )
        object.__setattr__(
            self,
            "continuation_admission_manifest",
            continuation_admission_manifest,
        )
        object.__setattr__(
            self,
            "continuation_admission_sha256",
            continuation_admission_snapshot["sha256"],
        )
        object.__setattr__(
            self,
            "continuation_admission_size_bytes",
            continuation_admission_snapshot["size_bytes"],
        )
        object.__setattr__(
            self,
            "full_agent_manifest",
            (
                full_agent_manifest.path
                if full_agent_manifest is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "full_agent_manifest_sha256",
            (
                full_agent_manifest.sha256
                if full_agent_manifest is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "full_agent_artifact_sha256s",
            (
                MappingProxyType(
                    {
                        role: artifact.sha256
                        for role, artifact in (
                            full_agent_manifest.artifacts.items()
                        )
                    }
                )
                if full_agent_manifest is not None
                else MappingProxyType({})
            ),
        )
        object.__setattr__(
            self,
            "_full_agent_manifest_snapshot",
            full_agent_manifest,
        )
        object.__setattr__(self, "experience_mode", experience_mode)
        object.__setattr__(self, "experience_store", experience_store)
        object.__setattr__(
            self, "experience_ranker_version", experience_ranker_version
        )
        object.__setattr__(
            self,
            "experience_admission_manifest",
            experience_admission_manifest,
        )
        object.__setattr__(
            self, "experience_task_split", experience_task_split
        )
        object.__setattr__(
            self,
            "experience_store_sha256",
            experience_snapshot["sha256"],
        )
        object.__setattr__(
            self,
            "experience_store_size_bytes",
            experience_snapshot["size_bytes"],
        )
        object.__setattr__(
            self,
            "experience_admission_sha256",
            experience_admission_snapshot["sha256"],
        )
        object.__setattr__(
            self,
            "experience_admission_size_bytes",
            experience_admission_snapshot["size_bytes"],
        )
        object.__setattr__(
            self,
            "splits",
            tuple(dict.fromkeys(_normalise_split(item) for item in self.splits)) or ("all",),
        )
        object.__setattr__(self, "mode_filters", tuple(normalised_modes))
        object.__setattr__(self, "task_filters", tuple(self.task_filters))
        object.__setattr__(self, "difficulty_filters", tuple(self.difficulty_filters))
        object.__setattr__(self, "model_filters", tuple(self.model_filters))
        object.__setattr__(self, "max_tasks", None if max_tasks is None else int(max_tasks))
        object.__setattr__(self, "max_runtime_seconds", max_runtime)

    def verify_full_agent_manifest(self) -> None:
        if self._full_agent_manifest_snapshot is None:
            return
        try:
            self._full_agent_manifest_snapshot.verify_unchanged()
        except FullAgentManifestError as exc:
            raise BenchmarkError(str(exc)) from exc

    def public_dict(self) -> dict[str, object]:
        return {
            "corpus": [str(item) for item in self.corpus],
            "output_dir": str(self.output_dir),
            "models": list(self.models),
            "repeats": self.repeats,
            "backend": self.backend,
            "splits": list(self.splits),
            "mode_filters": list(self.mode_filters),
            "task_filters": list(self.task_filters),
            "difficulty_filters": [str(item) for item in self.difficulty_filters],
            "model_filters": list(self.model_filters),
            "resume": self.resume,
            "retry_failures": self.retry_failures,
            "max_tasks": self.max_tasks,
            "max_runtime_seconds": self.max_runtime_seconds,
            "validation_profile": self.validation_profile,
            "final_validation_policy": self.final_validation_policy,
            "evidence_memory_mode": self.evidence_memory_mode,
            "max_planner_rounds": self.max_planner_rounds,
            "max_no_improvement_rounds": self.max_no_improvement_rounds,
            "enable_final_fallback": self.enable_final_fallback,
            "continuation_policy_mode": self.continuation_policy_mode,
            "continuation_policy_version": self.continuation_policy_version,
            "continuation_admission_manifest": (
                str(self.continuation_admission_manifest)
                if self.continuation_admission_manifest is not None
                else None
            ),
            "continuation_admission_snapshot": {
                "role": "CONTINUATION_ENFORCE_ADMISSION",
                "present": self.continuation_admission_manifest is not None,
                "size_bytes": self.continuation_admission_size_bytes,
                "sha256": self.continuation_admission_sha256,
            },
            "full_agent_manifest": (
                str(self.full_agent_manifest)
                if self.full_agent_manifest is not None
                else None
            ),
            "full_agent_manifest_snapshot": (
                self._full_agent_manifest_snapshot.public_dict()
                if self._full_agent_manifest_snapshot is not None
                else None
            ),
            "experience_mode": self.experience_mode,
            "experience_store": (
                str(self.experience_store)
                if self.experience_store is not None
                else None
            ),
            "experience_ranker_version": self.experience_ranker_version,
            "experience_admission_manifest": (
                str(self.experience_admission_manifest)
                if self.experience_admission_manifest is not None
                else None
            ),
            "experience_task_split": self.experience_task_split,
            "experience_store_snapshot": {
                "role": "READ_ONLY_SEED",
                "present": self.experience_store is not None,
                "size_bytes": self.experience_store_size_bytes,
                "sha256": self.experience_store_sha256,
            },
            "experience_admission_snapshot": {
                "role": "EXPERIENCE_GUIDED_ADMISSION",
                "present": self.experience_admission_manifest is not None,
                "size_bytes": self.experience_admission_size_bytes,
                "sha256": self.experience_admission_sha256,
            },
        }


def _read_task_metadata(task_toml: Path) -> Mapping[str, object]:
    try:
        value = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _benchmark_metadata(spec: Mapping[str, object]) -> Mapping[str, object]:
    value = spec.get("benchmark")
    return value if isinstance(value, Mapping) else {}


def _inferred_split(
    relative: Path,
    spec: Mapping[str, object],
    external_labels: Sequence[tuple[str, Mapping[str, object]]] = (),
) -> str:
    benchmark = _benchmark_metadata(spec)
    explicit = benchmark.get("split", spec.get("split"))
    if isinstance(explicit, str) and explicit.strip():
        return _normalise_split(explicit)
    for _source, labels in external_labels:
        explicit = labels.get("split")
        if isinstance(explicit, str) and explicit.strip():
            return _normalise_split(explicit)
    for part in relative.parts[:-1]:
        candidate = _normalise_split(part)
        if candidate in {"train", "validation", "test", "dev", "holdout"}:
            return candidate
    return "unspecified"


def _expected_mode(
    spec: Mapping[str, object],
    task_type: str,
    external_labels: Sequence[tuple[str, Mapping[str, object]]] = (),
) -> tuple[str | None, str | None]:
    benchmark = _benchmark_metadata(spec)
    for source, raw in (
        ("benchmark.expected_mode", benchmark.get("expected_mode")),
        ("benchmark.expected_router_mode", benchmark.get("expected_router_mode")),
        ("task.expected_mode", spec.get("expected_mode")),
        ("task.expected_router_mode", spec.get("expected_router_mode")),
    ):
        if raw is not None:
            mode = _normalise_mode(raw)
            return mode, source if mode is not None else None
    for source, labels in external_labels:
        for field in ("expected_mode", "expected_router_mode", "mode"):
            if labels.get(field) is not None:
                mode = _normalise_mode(labels.get(field))
                if mode is not None:
                    return mode, f"{source}.{field}"
    inferred = _TASK_TYPE_MODES.get(task_type.casefold())
    return inferred, "task_type" if inferred is not None else None


def _algorithm_family(
    spec: Mapping[str, object],
    external_labels: Sequence[tuple[str, Mapping[str, object]]] = (),
) -> tuple[str | None, str | None]:
    """Read only an explicitly safe, high-level family label."""

    benchmark = _benchmark_metadata(spec)
    for source, raw in (
        ("benchmark.algorithm_family", benchmark.get("algorithm_family")),
        ("benchmark.family", benchmark.get("family")),
        ("task.algorithm_family", spec.get("algorithm_family")),
    ):
        if isinstance(raw, str) and re.fullmatch(
            r"[a-z][a-z0-9_]*", raw.strip()
        ):
            return raw.strip(), source
    # Evaluator/corpus-manifest family labels can encode the injected mutation
    # or expected solution.  They are intentionally never exposed to the
    # experience layer; only explicit public task metadata is accepted.
    return None, None


def _read_label_sidecar(path: Path, *, task_id: str | None = None) -> Mapping[str, object]:
    """Read only evaluator labels from an optional JSON sidecar.

    The returned mapping is never attached to ``PublicTask`` or passed to an
    executor.  This keeps golden hashes and hidden-like evaluator data outside
    the Planner boundary while still making router accuracy measurable.
    """

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(path.parent.resolve())
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    recorded_task_id = value.get("task_id")
    if (
        task_id is not None
        and recorded_task_id is not None
        and str(recorded_task_id) != task_id
    ):
        return {}
    return {
        field: value[field]
        for field in ("expected_mode", "expected_router_mode", "mode", "split")
        if field in value
    }


def _manifest_labels(corpus: Path) -> dict[Path, Mapping[str, object]]:
    root = corpus if corpus.is_dir() else corpus.parent
    manifest_path = root / "corpus_manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    tasks = value.get("tasks") if isinstance(value, Mapping) else None
    if not isinstance(tasks, list):
        return {}
    labels: dict[Path, Mapping[str, object]] = {}
    resolved_root = root.resolve()
    for raw in tasks:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("path"), str):
            continue
        candidate = (resolved_root / str(raw["path"])).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            continue
        # This is an intentional allowlist.  In particular, mutation operator,
        # seed, mutation summary and answer-bearing fields never cross into a
        # TaskDescriptor or executor command.
        labels[candidate] = {
            field: raw[field]
            for field in (
                "expected_mode",
                "expected_router_mode",
                "mode",
                "split",
            )
            if field in raw
        }
    return labels


def _task_fingerprint(task: PublicTask) -> str:
    return _sha256_json(
        {
            "task_id": task.id,
            "public_file_hashes": dict(sorted(task.public_file_hashes.items())),
        }
    )


def _candidate_task_tomls(corpus: Path) -> list[tuple[Path, Path]]:
    if corpus.is_file():
        if corpus.name != "task.toml":
            raise BenchmarkError(
                f"corpus file must be a task.toml or a directory: {corpus}"
            )
        return [(corpus.parent, Path(corpus.parent.name))]
    if not corpus.is_dir():
        raise BenchmarkError(f"corpus path does not exist: {corpus}")
    direct = corpus / "task.toml"
    if direct.is_file():
        return [(corpus, Path(corpus.name))]
    found: list[tuple[Path, Path]] = []
    for task_toml in sorted(corpus.rglob("task.toml")):
        relative = task_toml.parent.relative_to(corpus)
        lowered = {part.casefold() for part in relative.parts}
        if lowered.intersection(
            {"answer", "golden", "hidden", "hidden_like", "reference"}
        ):
            continue
        found.append((task_toml.parent, relative))
    return found


def discover_tasks(corpora: Path | str | Sequence[Path | str]) -> list[TaskDescriptor]:
    """Discover every compatible task package without a corpus-manifest dependency."""

    items: Sequence[Path | str]
    if isinstance(corpora, (Path, str)):
        items = (corpora,)
    else:
        items = corpora
    descriptors: list[TaskDescriptor] = []
    seen: set[Path] = set()
    for raw_root in items:
        root = Path(raw_root).expanduser().resolve()
        manifest_labels = _manifest_labels(root)
        for directory, relative in _candidate_task_tomls(root):
            resolved = directory.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            task_toml = resolved / "task.toml"
            spec = _read_task_metadata(task_toml)
            task_id = str(spec.get("task_id", resolved.name))
            task_type = str(spec.get("task_type", "generate"))
            external_labels = (
                ("corpus_manifest", manifest_labels.get(resolved, {})),
                (
                    "acceptance",
                    _read_label_sidecar(
                        resolved / "acceptance.json", task_id=task_id
                    ),
                ),
            )
            raw_difficulty = spec.get("difficulty")
            try:
                difficulty = int(raw_difficulty) if raw_difficulty is not None else None
            except (TypeError, ValueError, OverflowError):
                difficulty = None
            expected_mode, mode_source = _expected_mode(
                spec, task_type, external_labels
            )
            algorithm_family, family_source = _algorithm_family(
                spec, external_labels
            )
            split = _inferred_split(relative, spec, external_labels)
            try:
                task = load_public_task(resolved)
            except Exception as exc:
                try:
                    raw_hash = _sha256_bytes(task_toml.read_bytes())
                except OSError:
                    raw_hash = _sha256_json({"path": str(resolved)})
                descriptors.append(
                    TaskDescriptor(
                        directory=resolved,
                        relative_path=relative.as_posix(),
                        task_id=task_id,
                        task_type=task_type,
                        difficulty=difficulty,
                        split=split,
                        expected_mode=expected_mode,
                        expected_mode_source=mode_source,
                        task_fingerprint=raw_hash,
                        task=None,
                        algorithm_family=algorithm_family,
                        algorithm_family_source=family_source,
                        load_error_type=type(exc).__name__,
                        load_error_detail=str(exc),
                    )
                )
                continue
            descriptors.append(
                TaskDescriptor(
                    directory=resolved,
                    relative_path=relative.as_posix(),
                    task_id=task.id,
                    task_type=task.task_type,
                    difficulty=task.difficulty,
                    split=split,
                    expected_mode=expected_mode,
                    expected_mode_source=mode_source,
                    task_fingerprint=_task_fingerprint(task),
                    task=task,
                    algorithm_family=algorithm_family,
                    algorithm_family_source=family_source,
                )
            )
    return sorted(
        descriptors,
        key=lambda item: (item.task_id.casefold(), str(item.directory)),
    )


def _matches_pattern(value: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return True
    folded = value.casefold()
    for raw in patterns:
        pattern = str(raw).strip().casefold()
        if not pattern:
            continue
        if folded == pattern or fnmatch.fnmatchcase(folded, pattern):
            return True
    return False


def _difficulty_values(filters: Sequence[str | int]) -> set[int]:
    values: set[int] = set()
    for raw in filters:
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            match = re.fullmatch(r"(-?\d+)\s*[-:]\s*(-?\d+)", token)
            if match is not None:
                start, end = int(match.group(1)), int(match.group(2))
                step = 1 if end >= start else -1
                values.update(range(start, end + step, step))
            else:
                values.add(int(token))
    return values


def select_tasks(
    descriptors: Sequence[TaskDescriptor], config: BenchmarkConfig
) -> list[TaskDescriptor]:
    splits = set(config.splits)
    mode_filters = {
        mode
        for raw in config.mode_filters
        for mode in [_normalise_mode(raw)]
        if mode is not None
    }
    difficulties = _difficulty_values(config.difficulty_filters)
    selected: list[TaskDescriptor] = []
    for descriptor in descriptors:
        if "all" not in splits and descriptor.split not in splits:
            continue
        if config.task_filters and not (
            _matches_pattern(descriptor.task_id, config.task_filters)
            or _matches_pattern(descriptor.relative_path, config.task_filters)
        ):
            continue
        if mode_filters and descriptor.expected_mode not in mode_filters:
            continue
        if difficulties and descriptor.difficulty not in difficulties:
            continue
        selected.append(descriptor)
    if config.max_tasks is not None:
        selected = selected[: config.max_tasks]
    return selected


def select_models(config: BenchmarkConfig) -> tuple[str, ...]:
    return tuple(
        model
        for model in config.models
        if _matches_pattern(model, config.model_filters)
    )


class SyntheticBenchmarkExecutor:
    """Offline orchestration fixture which deliberately emits no HLS metrics.

    The receipt proves selection, scheduling, isolation and durable report
    generation only.  It does not invent baseline observations for the real
    ``PhaseRouter`` and it does not fabricate tool validation or acceleration.
    """

    requires_vitis_lock = False

    def __init__(self, evidence_class: EvidenceClass) -> None:
        if evidence_class not in {EvidenceClass.DEMO, EvidenceClass.DETERMINISTIC}:
            raise ValueError("synthetic executor cannot emit real evidence")
        self.evidence_class = evidence_class

    def fingerprint(self) -> str:
        return (
            f"synthetic-v3d-orchestration:v2:{self.evidence_class.value}:"
            f"{_implementation_fingerprint()}"
        )

    def execute(
        self,
        spec: BenchmarkRunSpec,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        del timeout_seconds
        evidence_level = (
            "ORCHESTRATION_SMOKE_ONLY"
            if self.evidence_class is EvidenceClass.DEMO
            else "DETERMINISTIC_TEST_ONLY"
        )
        return {
            "schema_version": 1,
            "result_schema": "v3d.orchestration-receipt.v1",
            "workflow": "v3d-synthetic-orchestration",
            "backend": {
                "class": f"{type(self).__module__}.{type(self).__qualname__}",
                "fingerprint": self.fingerprint(),
                "evidence_level": evidence_level,
            },
            "task_id": spec.task.id,
            "status": "ORCHESTRATION_ONLY",
            "stop_reason": "ORCHESTRATION_RECEIPT_COMPLETED",
            "orchestration": {
                "run_id": spec.run_id,
                "run_fingerprint": spec.run_fingerprint,
                "task_fingerprint": spec.descriptor.task_fingerprint,
                "scheduled_model": spec.model,
                "scheduled_backend": spec.backend,
                "repeat_index": spec.repeat_index,
            },
            "metric_applicability": {
                "phase_router": "N/A_NO_OBSERVED_BASELINE",
                "e2e_success": "N/A_NO_TOOL_EXECUTION",
                "fresh_final": "N/A_NO_TOOL_EXECUTION",
                "acceleration": "N/A_NO_SYNTHESIS_MEASUREMENTS",
            },
            "budget": {
                "credits_used": 0,
                "tokens_used": 0,
                "input_tokens_used": 0,
                "output_tokens_used": 0,
                "cached_input_tokens_used": 0,
                "runtime_used_seconds": 0.0,
                "tool_used": {},
            },
        }


class V3PrototypeCLIExecutor:
    """Serialized adapter around the existing real V3 single-task CLI."""

    evidence_class = EvidenceClass.REAL
    requires_vitis_lock = True
    real_evidence_authority = REAL_EVIDENCE_AUTHORITY
    process_term_grace_seconds = 5.0
    process_kill_grace_seconds = 5.0

    _FORBIDDEN_EXTRA_ARGUMENTS = {
        "--task-dir",
        "--run-dir",
        "--thread-id",
        "--backend",
        "--planner",
        "--live-openai",
        "--patch-file",
        "--model",
        "--validation-profile",
        "--final-validation-policy",
        "--evidence-memory",
        "--max-planner-rounds",
        "--max-no-improvement-rounds",
        "--enable-final-fallback",
        "--continuation-policy",
        "--continuation-policy-version",
        "--continuation-admission-manifest",
        "--full-agent-manifest",
        "--experience-mode",
        "--experience-store",
        "--experience-ranker-version",
        "--experience-admission-manifest",
        "--experience-task-split",
    }
    _TOKEN_POLICY_EXTRA_ARGUMENTS = {
        "--token-budget-policy",
        "--token-budget-visibility",
        "--max-output-tokens",
        "--repair-max-output-tokens",
        "--synth-fix-max-output-tokens",
        "--structural-fix-max-output-tokens",
        "--optimize-max-output-tokens",
        "--minimum-viable-output-tokens",
        "--context-window-tokens",
        "--context-safety-margin-tokens",
        "--token-safety-margin",
        "--future-round-token-reserve",
        "--search-closeout-token-reserve",
        "--guidance-ratio",
    }

    def __init__(
        self,
        extra_args: Sequence[str] = (),
        *,
        validation_profile: str = "strict",
        final_validation_policy: str = "task_contract",
        evidence_memory_mode: str = "on",
        max_planner_rounds: int | None = None,
        max_no_improvement_rounds: int = 2,
        enable_final_fallback: bool = False,
        continuation_policy_mode: str = "shadow",
        continuation_policy_version: str = "v2",
        continuation_admission_manifest: Path | str | None = None,
        experience_mode: str = "shadow",
        experience_store: Path | str | None = None,
        experience_ranker_version: str = "v1",
        experience_admission_manifest: Path | str | None = None,
        experience_task_split: str | None = None,
        experimental_token_policy: bool = False,
    ) -> None:
        self.experimental_token_policy = bool(experimental_token_policy)
        self.extra_args = tuple(str(item) for item in extra_args)
        for item in self.extra_args:
            option = item.split("=", 1)[0]
            if (
                option in self._FORBIDDEN_EXTRA_ARGUMENTS
                or (
                    not self.experimental_token_policy
                    and option in self._TOKEN_POLICY_EXTRA_ARGUMENTS
                )
            ):
                raise BenchmarkError(
                    f"real benchmark executor cannot override {option}"
                )
        self.validation_profile = str(validation_profile).strip().casefold()
        if self.validation_profile not in _VALIDATION_PROFILES:
            raise BenchmarkError(
                "validation_profile must be strict or fast-experiment"
            )
        self.final_validation_policy = (
            str(final_validation_policy).strip().casefold()
        )
        if self.final_validation_policy not in _FINAL_VALIDATION_POLICIES:
            raise BenchmarkError(
                "final_validation_policy must be task_contract or "
                "full_internal_audit"
            )
        self.evidence_memory_mode = (
            str(evidence_memory_mode).strip().casefold()
        )
        if self.evidence_memory_mode not in _EVIDENCE_MEMORY_MODES:
            raise BenchmarkError("evidence_memory_mode must be off or on")
        self.max_planner_rounds = (
            None if max_planner_rounds is None else int(max_planner_rounds)
        )
        if (
            self.max_planner_rounds is not None
            and self.max_planner_rounds <= 0
        ):
            raise BenchmarkError("max_planner_rounds must be positive")
        self.max_no_improvement_rounds = int(max_no_improvement_rounds)
        if self.max_no_improvement_rounds <= 0:
            raise BenchmarkError(
                "max_no_improvement_rounds must be positive"
            )
        self.enable_final_fallback = bool(enable_final_fallback)
        self.continuation_policy_mode = (
            str(continuation_policy_mode).strip().casefold()
        )
        if self.continuation_policy_mode not in _CONTINUATION_POLICY_MODES:
            raise BenchmarkError(
                "continuation_policy_mode must be off, shadow, or enforce"
            )
        self.continuation_policy_version = (
            str(continuation_policy_version).strip().casefold()
        )
        if self.continuation_policy_version not in _CONTINUATION_POLICY_VERSIONS:
            raise BenchmarkError("continuation_policy_version must be v2")
        if (
            self.evidence_memory_mode == "off"
            and self.continuation_policy_mode != "off"
        ):
            raise BenchmarkError(
                "continuation requires evidence memory on"
            )
        (
            self.continuation_admission_manifest,
            self.continuation_admission_snapshot,
        ) = _admission_manifest_snapshot(
            continuation_admission_manifest,
            role="CONTINUATION_ENFORCE_ADMISSION",
            label="continuation_admission_manifest",
        )
        if self.continuation_policy_mode == "enforce":
            if self.continuation_admission_manifest is None:
                raise BenchmarkError(
                    "continuation enforce requires an admission manifest"
                )
        elif self.continuation_admission_manifest is not None:
            raise BenchmarkError(
                "continuation admission manifest is valid only in enforce mode"
            )
        self.experience_mode = str(experience_mode).strip().casefold()
        if self.experience_mode not in _EXPERIENCE_MODES:
            raise BenchmarkError(
                "experience_mode must be off, shadow, or guided"
            )
        self.experience_ranker_version = (
            str(experience_ranker_version).strip().casefold()
        )
        if self.experience_ranker_version not in _EXPERIENCE_RANKER_VERSIONS:
            raise BenchmarkError("experience_ranker_version must be v1 or v3")
        if experience_task_split is None:
            self.experience_task_split = None
        else:
            normalised_split = (
                str(experience_task_split)
                .strip()
                .casefold()
                .replace("-", "_")
            )
            if normalised_split not in _EXPERIENCE_TASK_SPLITS:
                raise BenchmarkError(
                    "experience_task_split must be train, dev, or hidden_like"
                )
            self.experience_task_split = normalised_split
        self.experience_store, self.experience_store_snapshot = (
            _experience_store_snapshot(experience_store)
        )
        (
            self.experience_admission_manifest,
            self.experience_admission_snapshot,
        ) = _admission_manifest_snapshot(
            experience_admission_manifest,
            role="EXPERIENCE_GUIDED_ADMISSION",
            label="experience_admission_manifest",
        )
        if (
            self.experience_mode != "off"
            and self.experience_ranker_version == "v3"
            and self.experience_store is None
        ):
            raise BenchmarkError(
                "experience ranker v3 requires an experience store"
            )
        if self.experience_mode == "guided":
            if self.experience_ranker_version != "v3":
                raise BenchmarkError(
                    "guided experience requires the Gate-controlled v3 ranker"
                )
            if self.experience_admission_manifest is None:
                raise BenchmarkError(
                    "guided experience requires an admission manifest"
                )
        _validate_component_admissions(
            continuation_path=self.continuation_admission_manifest,
            experience_path=self.experience_admission_manifest,
            experience_seed_path=self.experience_store,
            error_type=BenchmarkError,
        )

    def _verify_frozen_component_inputs(self) -> None:
        if self.experience_store is None:
            pass
        else:
            try:
                _path, current = _experience_store_snapshot(self.experience_store)
            except BenchmarkError as exc:
                raise BenchmarkExecutionError(
                    "experience_store is unavailable after the batch snapshot "
                    "was frozen"
                ) from exc
            if current != self.experience_store_snapshot:
                raise BenchmarkExecutionError(
                    "experience_store changed after the batch snapshot was frozen"
                )
        for path, expected, role, label in (
            (
                self.continuation_admission_manifest,
                self.continuation_admission_snapshot,
                "CONTINUATION_ENFORCE_ADMISSION",
                "continuation_admission_manifest",
            ),
            (
                self.experience_admission_manifest,
                self.experience_admission_snapshot,
                "EXPERIENCE_GUIDED_ADMISSION",
                "experience_admission_manifest",
            ),
        ):
            if path is None:
                continue
            try:
                _path, current = _admission_manifest_snapshot(
                    path,
                    role=role,
                    label=label,
                )
            except BenchmarkError as exc:
                raise BenchmarkExecutionError(
                    f"{label} is unavailable after the batch snapshot was frozen"
                ) from exc
            if current != expected:
                raise BenchmarkExecutionError(
                    f"{label} changed after the batch snapshot was frozen"
                )

    def fingerprint(self) -> str:
        # Only hashes of environment-derived values are retained.  The API key
        # is intentionally absent; it neither changes execution semantics nor
        # belongs in durable benchmark metadata.
        effective_environment = {
            "openai_base_url": os.environ.get("OPENAI_BASE_URL", "").strip(),
            "vitis_root": os.environ.get(
                "LLM4HLS_VITIS_HLS_ROOT", "/opt/xilinx/2025.2/Vitis"
            ),
            "toolchain_id": os.environ.get(
                "LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"
            ),
            "llm_timeout_s": os.environ.get("LLM4HLS_LLM_TIMEOUT_S", "120"),
            "llm_max_output_tokens": os.environ.get(
                "LLM4HLS_LLM_MAX_OUTPUT_TOKENS", "1000"
            ),
            "agent_credit_budget": os.environ.get(
                "LLM4HLS_CREDIT_BUDGET", ""
            ),
            "agent_token_budget": os.environ.get(
                "LLM4HLS_TOKEN_BUDGET", ""
            ),
            "cost_csim": os.environ.get("LLM4HLS_COST_CSIM", ""),
            "cost_synth": os.environ.get("LLM4HLS_COST_SYNTH", ""),
            "cost_cosim": os.environ.get("LLM4HLS_COST_COSIM", ""),
            "cosim_no_progress_timeout_s": os.environ.get(
                "LLM4HLS_COSIM_NO_PROGRESS_TIMEOUT_S", "0"
            ),
        }
        return _sha256_json(
            {
                "executor": "v3-prototype-cli:v3",
                "python": sys.version.split()[0],
                "extra_args": self.extra_args,
                "validation_profile": self.validation_profile,
                "final_validation_policy": self.final_validation_policy,
                "evidence_memory_mode": self.evidence_memory_mode,
                "experimental_token_policy": self.experimental_token_policy,
                "max_planner_rounds": self.max_planner_rounds,
                "max_no_improvement_rounds": (
                    self.max_no_improvement_rounds
                ),
                "enable_final_fallback": self.enable_final_fallback,
                "continuation_policy_mode": self.continuation_policy_mode,
                "continuation_policy_version": self.continuation_policy_version,
                "continuation_admission_snapshot": (
                    self.continuation_admission_snapshot
                ),
                "experience_mode": self.experience_mode,
                "experience_ranker_version": self.experience_ranker_version,
                "experience_task_split": self.experience_task_split,
                "experience_store_snapshot": self.experience_store_snapshot,
                "experience_admission_snapshot": (
                    self.experience_admission_snapshot
                ),
                "implementation_fingerprint": _implementation_fingerprint(),
                "effective_environment_sha256": _sha256_json(
                    effective_environment
                ),
            }
        )

    def _command(self, spec: BenchmarkRunSpec) -> list[str]:
        self._verify_frozen_component_inputs()
        command = [
            sys.executable,
            "-m",
            (
                "llm4hls_agent.token_policy_experiment_cli"
                if self.experimental_token_policy
                else "llm4hls_agent.v3_prototype_cli"
            ),
            "--task-dir",
            str(spec.descriptor.directory),
            "--run-dir",
            str(spec.run_dir),
            "--thread-id",
            spec.run_id,
            "--backend",
            "vitis",
            "--planner",
            "openai-compatible",
            "--model",
            spec.model,
            "--validation-profile",
            self.validation_profile,
            "--final-validation-policy",
            self.final_validation_policy,
            "--evidence-memory",
            self.evidence_memory_mode,
            "--max-no-improvement-rounds",
            str(self.max_no_improvement_rounds),
            "--continuation-policy",
            self.continuation_policy_mode,
            "--continuation-policy-version",
            self.continuation_policy_version,
            "--experience-mode",
            self.experience_mode,
            "--experience-ranker-version",
            self.experience_ranker_version,
        ]
        if self.max_planner_rounds is not None:
            command.extend(
                ("--max-planner-rounds", str(self.max_planner_rounds))
            )
        if self.enable_final_fallback:
            command.append("--enable-final-fallback")
        if self.continuation_admission_manifest is not None:
            command.extend(
                (
                    "--continuation-admission-manifest",
                    str(self.continuation_admission_manifest),
                )
            )
        if self.experience_store is not None:
            command.extend(("--experience-store", str(self.experience_store)))
        if self.experience_admission_manifest is not None:
            command.extend(
                (
                    "--experience-admission-manifest",
                    str(self.experience_admission_manifest),
                )
            )
        task_split = self.experience_task_split
        descriptor_split = spec.descriptor.split.replace("-", "_")
        if descriptor_split in {"hidden_like", "test", "holdout"}:
            if task_split is not None and task_split != "hidden_like":
                raise BenchmarkExecutionError(
                    "restricted task split is authoritative and cannot be relabelled"
                )
            task_split = "hidden_like"
        elif task_split is None:
            if descriptor_split in _EXPERIENCE_TASK_SPLITS:
                task_split = descriptor_split
        if task_split is not None:
            command.extend(("--experience-task-split", task_split))
        command.extend(self.extra_args)
        return command

    def execute(
        self,
        spec: BenchmarkRunSpec,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        search_result_path = spec.run_dir / "v3_prototype_result.json"
        certified_result_path = spec.run_dir / "v3_certified_result.json"
        if search_result_path.exists() or certified_result_path.exists():
            raise BenchmarkExecutionError(
                "refusing to execute with a pre-existing V3 terminal result"
            )
        run_deadline_monotonic = (
            None
            if timeout_seconds is None
            else time.monotonic() + float(timeout_seconds)
        )
        command = self._command(spec)
        if run_deadline_monotonic is not None:
            command.extend(
                (
                    "--run-deadline-monotonic",
                    format(run_deadline_monotonic, ".17g"),
                    "--cleanup-reserve-seconds",
                    "30",
                )
            )
        _atomic_json(spec.run_dir / "benchmark_executor_command.json", command)
        _atomic_json(
            spec.run_dir / "benchmark_execution_binding.json",
            {
                "schema_version": "v3d.execution-binding.v1",
                "run_id": spec.run_id,
                "run_fingerprint": spec.run_fingerprint,
                "task_id": spec.task.id,
                "task_fingerprint": spec.descriptor.task_fingerprint,
                "model": spec.model,
            },
        )
        if timeout_seconds is not None and float(timeout_seconds) <= 0:
            raise BenchmarkExecutionTimeout(
                "V3 CLI was not started because the run deadline expired",
                partial_report=recover_partial_run_report(spec.run_dir),
                process_cleanup={
                    "schema_version": "v3d.executor-process-cleanup.v1",
                    "process_started": False,
                    "process_tree_cleaned": True,
                },
            )
        subreaper_enabled = _enable_child_subreaper()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
        )
        pgid = process.pid
        try:
            communicate_timeout = (
                None
                if run_deadline_monotonic is None
                else max(
                    0.001,
                    run_deadline_monotonic - time.monotonic(),
                )
            )
            stdout, stderr = process.communicate(
                timeout=communicate_timeout
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, cleanup = _terminate_executor_process_group(
                process,
                pgid=pgid,
                term_grace_seconds=self.process_term_grace_seconds,
                kill_grace_seconds=self.process_kill_grace_seconds,
            )
            cleanup["subreaper_enabled"] = subreaper_enabled
            cleanup["timeout_seconds"] = timeout_seconds
            cleanup["run_deadline_monotonic"] = run_deadline_monotonic
            _atomic_text(
                spec.run_dir / "benchmark_stdout.log",
                stdout or _subprocess_text(exc.stdout),
            )
            _atomic_text(
                spec.run_dir / "benchmark_stderr.log",
                stderr or _subprocess_text(exc.stderr),
            )
            partial_report = recover_partial_run_report(spec.run_dir)
            _atomic_json(
                spec.run_dir / "benchmark_executor_timeout.json",
                cleanup,
            )
            _atomic_json(
                spec.run_dir / "benchmark_partial_run_report.json",
                partial_report,
            )
            raise BenchmarkExecutionTimeout(
                "V3 CLI timed out after its absolute run deadline",
                partial_report=partial_report,
                process_cleanup=cleanup,
            ) from exc
        except BaseException:
            if process.poll() is None or _process_group_members(pgid):
                _terminate_executor_process_group(process, pgid=pgid)
            raise
        completed = subprocess.CompletedProcess(
            command,
            int(process.returncode or 0),
            _subprocess_text(stdout),
            _subprocess_text(stderr),
        )
        _atomic_text(
            spec.run_dir / "benchmark_stdout.log",
            _subprocess_text(completed.stdout),
        )
        _atomic_text(
            spec.run_dir / "benchmark_stderr.log",
            _subprocess_text(completed.stderr),
        )
        result_path = (
            certified_result_path
            if certified_result_path.is_file()
            else search_result_path
        )
        if result_path.is_file():
            try:
                value = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BenchmarkExecutionError(
                    "V3 CLI produced an unreadable terminal result"
                ) from exc
            if isinstance(value, Mapping):
                status = str(value.get("status", "")).upper()
                if completed.returncode != 0 and status == "DONE":
                    raise BenchmarkExecutionError(
                        "V3 CLI returned a successful terminal result with a "
                        "non-zero process exit"
                    )
                if completed.returncode == 0 and status != "DONE":
                    raise BenchmarkExecutionError(
                        "V3 CLI exited zero without a DONE terminal result"
                    )
                self._validate_terminal_provenance(spec, value)
                return value
        partial_report = recover_partial_run_report(spec.run_dir)
        detail = completed.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise BenchmarkExecutionError(
            f"V3 CLI exited {completed.returncode} without a terminal result: {detail[0]}",
            partial_report=partial_report,
        )

    @staticmethod
    def _run_path(run_dir: Path, reference: str) -> Path:
        if not isinstance(reference, str) or not reference:
            raise BenchmarkExecutionError("V3 result contains an empty artifact ref")
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise BenchmarkExecutionError("V3 result contains an unsafe artifact ref")
        root = run_dir.resolve()
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise BenchmarkExecutionError(
                    f"V3 provenance artifact uses a symbolic link: {reference}"
                )
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BenchmarkExecutionError(
                f"V3 provenance artifact escapes the run: {reference}"
            ) from exc
        if not path.is_file():
            raise BenchmarkExecutionError(
                f"V3 provenance artifact is missing: {reference}"
            )
        return path

    @classmethod
    def _run_json_with_hash(
        cls, run_dir: Path, reference: str
    ) -> tuple[Mapping[str, object], str, int]:
        path = cls._run_path(run_dir, reference)
        try:
            data = path.read_bytes()
            value = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BenchmarkExecutionError(
                f"V3 provenance artifact is unreadable: {reference}"
            ) from exc
        if not isinstance(value, Mapping):
            raise BenchmarkExecutionError(
                f"V3 provenance artifact is not an object: {reference}"
            )
        return value, _sha256_bytes(data), len(data)

    @classmethod
    def _run_json(cls, run_dir: Path, reference: str) -> Mapping[str, object]:
        return cls._run_json_with_hash(run_dir, reference)[0]

    @staticmethod
    def _ledger_events(run_dir: Path, reference: str) -> tuple[list[dict[str, object]], str]:
        path = V3PrototypeCLIExecutor._run_path(run_dir, reference)
        try:
            data = path.read_bytes()
            lines = data.decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise BenchmarkExecutionError("V3 budget ledger is unreadable") from exc
        events: list[dict[str, object]] = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkExecutionError(
                    f"V3 budget ledger line {number} is invalid"
                ) from exc
            if not isinstance(value, dict):
                raise BenchmarkExecutionError(
                    f"V3 budget ledger line {number} is not an object"
                )
            events.append(value)
        if not events or events[0].get("state") != "INITIALIZED":
            raise BenchmarkExecutionError("V3 budget ledger is not initialized")
        return events, _sha256_bytes(data)

    @staticmethod
    def _require_digest(value: object, *, label: str) -> str:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            raise BenchmarkExecutionError(f"{label} is not a SHA-256 digest")
        return value

    def _validate_terminal_provenance(
        self, spec: BenchmarkRunSpec, result: Mapping[str, object]
    ) -> dict[str, object]:
        """Validate and hash-bind one REAL terminal and every decisive ref.

        This is the single validator used immediately after execution and on
        ``--resume``.  Resume therefore cannot trust a once-valid summary row
        after its source, package, Planner, ledger, final action, or Candidate
        source has been changed.
        """

        certified_payload = (
            result.get("result_schema") == "v3.certified-result.v1"
        )
        source_ref = (
            "v3_certified_result.json"
            if certified_payload
            else "v3_prototype_result.json"
        )
        source_result, source_sha256, _source_size = self._run_json_with_hash(
            spec.run_dir, source_ref
        )
        if dict(source_result) != dict(result):
            raise BenchmarkExecutionError(
                "V3 source terminal result differs from the validated payload"
            )
        if (
            str(result.get("status", "")).upper() == "DONE"
            and not certified_payload
        ):
            raise BenchmarkExecutionError(
                "successful REAL row lacks independent final certification"
            )
        certification_provenance: dict[str, object] | None = None
        agent_search_source: dict[str, str] | None = None
        if certified_payload:
            search_result, search_result_sha256, _ = self._run_json_with_hash(
                spec.run_dir, "v3_prototype_result.json"
            )
            for key, value in search_result.items():
                if key in {"status", "stop_reason", "result_schema"}:
                    continue
                if result.get(key) != value:
                    raise BenchmarkExecutionError(
                        f"certified result changed Agent search field {key}"
                    )
            if result.get("agent_search_status") != search_result.get("status"):
                raise BenchmarkExecutionError(
                    "certified result does not preserve Agent search status"
                )
            certification = _mapping(result.get("final_certification"))
            receipt_ref = certification.get("receipt_ref")
            if not isinstance(receipt_ref, str):
                raise BenchmarkExecutionError(
                    "certified result lacks certification receipt_ref"
                )
            receipt, receipt_file_sha256, _ = self._run_json_with_hash(
                spec.run_dir, receipt_ref
            )
            receipt_payload = {
                key: value
                for key, value in receipt.items()
                if key != "receipt_sha256"
            }
            if (
                receipt.get("schema_version")
                != "v3.final-certification-receipt.v1"
                or receipt.get("receipt_sha256") != _sha256_json(receipt_payload)
                or certification.get("receipt_sha256")
                != receipt.get("receipt_sha256")
                or receipt.get("budget_domain")
                != "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET"
                or receipt.get("agent_credits_charged") != 0
                or receipt.get("feedback_policy")
                != "NO_SAME_RUN_AGENT_FEEDBACK"
            ):
                raise BenchmarkExecutionError(
                    "independent certification receipt contract is invalid"
                )
            freeze_ref = receipt.get("frozen_candidate_ref")
            if not isinstance(freeze_ref, str):
                raise BenchmarkExecutionError(
                    "certification receipt lacks frozen Candidate ref"
                )
            freeze, freeze_file_sha256, _ = self._run_json_with_hash(
                spec.run_dir, freeze_ref
            )
            terminal_binding = _mapping(
                search_result.get("terminal_candidate_binding")
            )
            if (
                freeze.get("schema_version") != "v3.frozen-search-candidate.v1"
                or freeze.get("candidate_id") != terminal_binding.get("candidate_id")
                or freeze.get("source_sha256")
                != terminal_binding.get("source_sha256")
                or freeze.get("search_result_sha256") != search_result_sha256
            ):
                raise BenchmarkExecutionError(
                    "certification freeze does not bind the Agent terminal"
                )
            ledger_path = self._run_path(spec.run_dir, "budget_ledger.jsonl")
            ledger_sha256_now = _sha256_file(ledger_path)
            certified_ledger = _mapping(receipt.get("agent_ledger"))
            if (
                freeze.get("agent_ledger_sha256") != ledger_sha256_now
                or certified_ledger.get("before_sha256") != ledger_sha256_now
                or certified_ledger.get("after_sha256") != ledger_sha256_now
                or certified_ledger.get("unchanged") is not True
            ):
                raise BenchmarkExecutionError(
                    "certification did not preserve the Agent Ledger"
                )
            stages = _mapping(receipt.get("stages"))
            clock_gate = _mapping(receipt.get("clock_gate"))
            receipt_passed = (
                receipt.get("status") == "PASS"
                and all(
                    _mapping(stages.get(stage)).get("ok") is True
                    for stage in ("csim", "synth", "cosim")
                )
                and clock_gate.get("passed") is True
                and clock_gate.get("maximum_period_ns") == 10.0
            )
            if (str(result.get("status", "")).upper() == "DONE") != receipt_passed:
                raise BenchmarkExecutionError(
                    "certified terminal status disagrees with certification receipt"
                )
            certification_provenance = {
                "receipt": {
                    "ref": receipt_ref,
                    "file_sha256": receipt_file_sha256,
                    "receipt_sha256": receipt.get("receipt_sha256"),
                },
                "frozen_candidate": {
                    "ref": freeze_ref,
                    "sha256": freeze_file_sha256,
                },
                "status": receipt.get("status"),
                "budget_domain": receipt.get("budget_domain"),
                "agent_credits_charged": 0,
            }
            agent_search_source = {
                "ref": "v3_prototype_result.json",
                "sha256": search_result_sha256,
            }
            result = search_result

        config_ref = "v3_run_config.json"
        task_spec_ref = "v3_task_spec.json"
        binding_ref = "benchmark_execution_binding.json"
        command_ref = "benchmark_executor_command.json"
        config, config_sha256, _ = self._run_json_with_hash(
            spec.run_dir, config_ref
        )
        task_spec, task_spec_sha256, _ = self._run_json_with_hash(
            spec.run_dir, task_spec_ref
        )
        binding, binding_sha256, _ = self._run_json_with_hash(
            spec.run_dir, binding_ref
        )
        command_path = self._run_path(spec.run_dir, command_ref)
        try:
            command_data = command_path.read_bytes()
            recorded_command = json.loads(command_data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BenchmarkExecutionError("V3 benchmark command is unreadable") from exc
        scheduled_command = self._command(spec)
        command_matches = recorded_command == scheduled_command
        if (
            not command_matches
            and isinstance(recorded_command, list)
            and recorded_command[: len(scheduled_command)] == scheduled_command
        ):
            runtime_suffix = recorded_command[len(scheduled_command) :]
            if (
                len(runtime_suffix) == 4
                and runtime_suffix[0] == "--run-deadline-monotonic"
                and runtime_suffix[2:] == ["--cleanup-reserve-seconds", "30"]
            ):
                try:
                    recorded_deadline = float(runtime_suffix[1])
                except (TypeError, ValueError):
                    recorded_deadline = math.nan
                command_matches = (
                    math.isfinite(recorded_deadline) and recorded_deadline > 0.0
                )
        if not command_matches:
            raise BenchmarkExecutionError(
                "V3 benchmark command does not match the scheduled run"
            )
        command_sha256 = _sha256_bytes(command_data)

        if config.get("thread_id") != spec.run_id:
            raise BenchmarkExecutionError("V3 run thread_id does not match batch run_id")
        if (
            task_spec.get("task_id") != spec.task.id
            or task_spec.get("public_file_hashes")
            != dict(spec.task.public_file_hashes)
        ):
            raise BenchmarkExecutionError("V3 task spec does not match scheduled task")
        expected_binding = {
            "schema_version": "v3d.execution-binding.v1",
            "run_id": spec.run_id,
            "run_fingerprint": spec.run_fingerprint,
            "task_id": spec.task.id,
            "task_fingerprint": spec.descriptor.task_fingerprint,
            "model": spec.model,
        }
        if dict(binding) != expected_binding:
            raise BenchmarkExecutionError(
                "V3 execution binding does not match the scheduled run"
            )
        if result.get("task_id") != spec.task.id:
            raise BenchmarkExecutionError("V3 result does not match scheduled task")
        backend = _mapping(result.get("backend"))
        if (
            backend.get("class") != "llm4hls_agent.vitis.VitisBackend"
            or backend.get("fingerprint") != config.get("backend_fingerprint")
        ):
            raise BenchmarkExecutionError("REAL row was not produced by VitisBackend")

        package = _mapping(result.get("package"))
        manifest_ref = package.get("manifest_ref")
        if not isinstance(manifest_ref, str) or not manifest_ref:
            raise BenchmarkExecutionError("REAL terminal lacks a package manifest ref")
        manifest, manifest_file_sha256, _ = self._run_json_with_hash(
            spec.run_dir, manifest_ref
        )
        manifest_sha256 = self._require_digest(
            package.get("manifest_sha256"), label="package manifest_sha256"
        )
        if _sha256_json(manifest) != manifest_sha256:
            raise BenchmarkExecutionError("V3 package manifest hash mismatch")
        terminal_payload = dict(result)
        terminal_payload.pop("package", None)
        if manifest.get("terminal_payload_sha256") != _sha256_json(
            terminal_payload
        ):
            raise BenchmarkExecutionError("V3 terminal payload hash mismatch")
        node_events = result.get("node_events")
        if (
            manifest.get("workflow") != result.get("workflow")
            or manifest.get("task_id") != spec.task.id
            or manifest.get("status") != result.get("status")
            or manifest.get("final_candidate_id")
            != result.get("final_candidate_id")
            or not isinstance(node_events, list)
            or manifest.get("node_event_count") != len(node_events)
        ):
            raise BenchmarkExecutionError("V3 package identity is inconsistent")

        raw_artifacts = manifest.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise BenchmarkExecutionError("V3 package artifact list is invalid")
        artifact_index: dict[str, str] = {}
        artifact_order: list[str] = []
        for raw in raw_artifacts:
            item = _mapping(raw)
            reference = item.get("path")
            digest = item.get("sha256")
            size = item.get("size_bytes")
            if (
                not isinstance(reference, str)
                or reference in artifact_index
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                raise BenchmarkExecutionError(
                    "V3 package artifact entry is invalid or duplicated"
                )
            expected_digest = self._require_digest(
                digest, label=f"package artifact {reference} sha256"
            )
            path = self._run_path(spec.run_dir, reference)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise BenchmarkExecutionError(
                    f"V3 package artifact is unreadable: {reference}"
                ) from exc
            if len(data) != size or _sha256_bytes(data) != expected_digest:
                raise BenchmarkExecutionError(
                    f"V3 package artifact hash mismatch: {reference}"
                )
            artifact_index[reference] = expected_digest
            artifact_order.append(reference)
        if artifact_order != sorted(artifact_order):
            raise BenchmarkExecutionError("V3 package artifacts are not sorted")

        def covered(reference: str) -> str:
            digest = artifact_index.get(reference)
            if digest is None:
                raise BenchmarkExecutionError(
                    f"V3 decisive artifact is absent from package: {reference}"
                )
            return digest

        for required in (config_ref, task_spec_ref, "budget_ledger.jsonl"):
            covered(required)

        hash_bound_refs: list[dict[str, str]] = []

        def validate_json_ref(
            *,
            role: str,
            reference_value: object,
            digest_value: object | None = None,
            canonical_digest: bool = False,
            required: bool = False,
        ) -> Mapping[str, object] | None:
            if reference_value is None or reference_value == "":
                if required:
                    raise BenchmarkExecutionError(
                        f"successful REAL model row lacks {role} ref"
                    )
                return None
            if not isinstance(reference_value, str):
                raise BenchmarkExecutionError(f"V3 {role} ref is invalid")
            value, raw_digest, _ = self._run_json_with_hash(
                spec.run_dir, reference_value
            )
            manifest_digest = covered(reference_value)
            if raw_digest != manifest_digest:
                raise BenchmarkExecutionError(
                    f"V3 {role} differs from its package digest"
                )
            if digest_value is not None:
                declared = self._require_digest(
                    digest_value, label=f"{role} declared sha256"
                )
                actual = _sha256_json(value) if canonical_digest else raw_digest
                if declared != actual:
                    raise BenchmarkExecutionError(f"V3 {role} hash mismatch")
            hash_bound_refs.append(
                {"role": role, "ref": reference_value, "sha256": raw_digest}
            )
            return value

        planner_input = validate_json_ref(
            role="planner_input",
            reference_value=result.get("planner_input_ref"),
            digest_value=result.get("planner_input_sha256"),
            canonical_digest=True,
        )
        planner_output = validate_json_ref(
            role="planner_output",
            reference_value=result.get("planner_output_ref"),
            digest_value=result.get("planner_output_sha256"),
            canonical_digest=True,
        )
        if (planner_input is None) != (planner_output is None):
            raise BenchmarkExecutionError("V3 Planner input/output chain is incomplete")

        for stem in (
            "failure_evidence",
            "baseline_synth_evidence",
            "best_synth_evidence",
            "candidate_synth_evidence",
            "final_synth_evidence",
        ):
            validate_json_ref(
                role=stem,
                reference_value=result.get(f"{stem}_ref"),
                digest_value=result.get(f"{stem}_sha256"),
            )

        registry, _registry_sha, _ = self._run_json_with_hash(
            spec.run_dir, "candidate_registry.json"
        )
        covered("candidate_registry.json")
        final_source: dict[str, str] | None = None
        final_candidate_id = result.get("final_candidate_id")
        if isinstance(final_candidate_id, str):
            candidates = _mapping(registry.get("candidates"))
            candidate = _mapping(candidates.get(final_candidate_id))
            source_candidate_ref = candidate.get("source_ref")
            if not isinstance(source_candidate_ref, str) or not source_candidate_ref:
                raise BenchmarkExecutionError("V3 final Candidate source ref is missing")
            source_path = self._run_path(spec.run_dir, source_candidate_ref)
            source_digest = _sha256_file(source_path)
            if (
                covered(source_candidate_ref) != source_digest
                or candidate.get("code_hash") != source_digest
            ):
                raise BenchmarkExecutionError(
                    "V3 final Candidate source hash is inconsistent"
                )
            final_source = {
                "candidate_id": final_candidate_id,
                "ref": source_candidate_ref,
                "sha256": source_digest,
            }

        ledger_events, ledger_sha256 = self._ledger_events(
            spec.run_dir, "budget_ledger.jsonl"
        )
        if covered("budget_ledger.jsonl") != ledger_sha256:
            raise BenchmarkExecutionError("V3 budget ledger package hash mismatch")
        initialized_config = _mapping(ledger_events[0].get("config"))
        if initialized_config != _mapping(config.get("budget")):
            raise BenchmarkExecutionError("V3 budget ledger config mismatch")
        events_by_action: dict[str, list[Mapping[str, object]]] = {}
        for event in ledger_events[1:]:
            action_id = event.get("action_id")
            if isinstance(action_id, str):
                events_by_action.setdefault(action_id, []).append(event)

        live_completed_refs = sorted(
            reference
            for reference in artifact_index
            if reference.startswith("control/live_planner_actions/")
            and reference.endswith(".completed.json")
        )
        rejection_records_by_action: dict[
            str, list[tuple[str, Mapping[str, object]]]
        ] = {}
        for rejection_ref in sorted(
            reference
            for reference in artifact_index
            if reference.startswith("control/proposal_rejections/")
            and reference.endswith(".json")
        ):
            rejection = validate_json_ref(
                role=f"proposal_rejection:{Path(rejection_ref).name}",
                reference_value=rejection_ref,
                required=True,
            )
            assert rejection is not None
            rejection_action_id = rejection.get("planner_action_id")
            if isinstance(rejection_action_id, str):
                rejection_records_by_action.setdefault(
                    rejection_action_id, []
                ).append((rejection_ref, rejection))
        model_outcomes: list[dict[str, object]] = []
        live_action_ids: set[str] = set()
        for completed_ref in live_completed_refs:
            action_id = Path(completed_ref).name[: -len(".completed.json")]
            started_ref = (
                f"control/live_planner_actions/{action_id}.started.json"
            )
            completed = validate_json_ref(
                role=f"live_planner_completed:{action_id}",
                reference_value=completed_ref,
                required=True,
            )
            started = validate_json_ref(
                role=f"live_planner_started:{action_id}",
                reference_value=started_ref,
                required=True,
            )
            assert completed is not None and started is not None
            action_request = _mapping(completed.get("request"))
            if (
                completed.get("schema_version")
                != LIVE_PLANNER_COMPLETED_SCHEMA
                or started.get("schema_version")
                != LIVE_PLANNER_STARTED_SCHEMA
                or completed.get("action_id") != action_id
                or completed.get("status") != "COMPLETED"
                or started.get("action_id") != action_id
                or started.get("status") != "STARTED"
                or _mapping(started.get("request")) != action_request
                or action_request.get("schema_version")
                != LIVE_PLANNER_ACTION_SCHEMA
                or _sha256_json(action_request) != action_id
            ):
                raise BenchmarkExecutionError(
                    "V3 live Planner STARTED/COMPLETED identity mismatch"
                )
            action_input = validate_json_ref(
                role=f"live_planner_input:{action_id}",
                reference_value=action_request.get("input_ref"),
                digest_value=action_request.get("input_sha256"),
                canonical_digest=True,
                required=True,
            )
            request_ref = action_request.get("request_ref")
            request_sha256 = action_request.get("request_sha256")
            request_audit = validate_json_ref(
                role=f"live_planner_request:{action_id}",
                reference_value=request_ref,
                digest_value=request_sha256,
                canonical_digest=True,
                required=True,
            )
            output_ref = completed.get("result_ref")
            output_sha256 = completed.get("result_sha256")
            outcome = validate_json_ref(
                role=f"live_planner_outcome:{action_id}",
                reference_value=output_ref,
                digest_value=output_sha256,
                required=True,
            )
            assert (
                action_input is not None
                and request_audit is not None
                and outcome is not None
            )
            try:
                validate_live_planner_action_identity(
                    action_request,
                    request_audit,
                    expected_action_id=action_id,
                )
            except PlannerActionError as exc:
                raise BenchmarkExecutionError(str(exc)) from exc
            provider_request = _mapping(
                _mapping(request_audit.get("request")).get("provider_request")
            )
            provider_body = _mapping(provider_request.get("http_body"))
            if (
                request_audit.get("planner_fingerprint")
                != action_request.get("planner_fingerprint")
                or request_audit.get("schema_version")
                != LIVE_PROVIDER_REQUEST_SCHEMA
                or request_audit.get("input_sha256")
                != action_request.get("input_sha256")
                or provider_request.get("provider") != "openai-compatible"
                or provider_request.get("model") != spec.model
                or provider_body.get("model") != spec.model
                or outcome.get("action_id") != action_id
                or outcome.get("input_sha256")
                != action_request.get("input_sha256")
            ):
                raise BenchmarkExecutionError(
                    "V3 live Planner provider/model binding mismatch"
                )
            completed_outcome = completed.get("outcome")
            rejection_reason: str | None = None
            rejection_ref: str | None = None
            if completed_outcome == "PROPOSAL":
                provider_binding = _mapping(outcome.get("provider_binding"))
                proposal = _mapping(outcome.get("proposal"))
                usage = _mapping(outcome.get("usage"))
                if (
                    outcome.get("schema_version")
                    != LIVE_PLANNER_OUTCOME_SCHEMA
                    or outcome.get("outcome") != "PROPOSAL"
                    or provider_binding.get("planner_fingerprint")
                    != action_request.get("planner_fingerprint")
                    or provider_binding.get("provider")
                    != proposal.get("provider")
                    or provider_binding.get("model") != spec.model
                    or proposal.get("model") != spec.model
                    or usage.get("usage_complete") is not True
                ):
                    raise BenchmarkExecutionError(
                        "V3 live Planner provider/model binding mismatch"
                    )
                input_tokens = _as_nonnegative_int(
                    usage.get("input_tokens"), -1
                )
                output_tokens = _as_nonnegative_int(
                    usage.get("output_tokens"), -1
                )
                cached_tokens = _as_nonnegative_int(
                    usage.get("cached_input_tokens"), -1
                )
                tokens_used = _as_nonnegative_int(
                    usage.get("tokens_used"), -1
                )
                proposal_usage_matches = (
                    proposal.get("input_tokens") == input_tokens
                    and proposal.get("output_tokens") == output_tokens
                    and proposal.get("cached_input_tokens") == cached_tokens
                )
            elif completed_outcome == "PROVIDER_OUTPUT_REJECTED":
                usage = _mapping(outcome.get("usage"))
                raw_reason = (
                    outcome.get("truncation_reason")
                    or outcome.get("error_type")
                )
                rejection_reason = (
                    str(raw_reason)
                    if isinstance(raw_reason, str) and raw_reason
                    else None
                )
                expected_failure_ref = (
                    f"planner/provider_failures/{action_id}.json"
                )
                if (
                    output_ref != expected_failure_ref
                    or outcome.get("schema_version")
                    != LIVE_PLANNER_FAILURE_SCHEMA
                    or outcome.get("outcome")
                    != "PROVIDER_OUTPUT_REJECTED"
                    or outcome.get("planner_fingerprint")
                    != action_request.get("planner_fingerprint")
                    or usage.get("usage_complete") is not True
                    or rejection_reason is None
                    or completed.get("truncation_reason")
                    != outcome.get("truncation_reason")
                    or completed.get("finish_reason")
                    != outcome.get("finish_reason")
                ):
                    raise BenchmarkExecutionError(
                        "V3 live Planner provider rejection binding mismatch"
                    )
                matching_rejections = rejection_records_by_action.get(
                    action_id, []
                )
                if len(matching_rejections) != 1:
                    raise BenchmarkExecutionError(
                        "V3 provider rejection lacks one packaged decision"
                    )
                rejection_ref, rejection = matching_rejections[0]
                if (
                    rejection.get("schema_version")
                    != "v3a.proposal-rejection.v1"
                    or rejection.get("planner_action_id") != action_id
                    or rejection.get("planner_input_ref")
                    != action_request.get("input_ref")
                    or rejection.get("planner_input_sha256")
                    != action_request.get("input_sha256")
                    or rejection.get("planner_output_ref") != output_ref
                    or rejection.get("planner_output_sha256")
                    != output_sha256
                    or rejection.get("reason")
                    != f"PROVIDER_OUTPUT_REJECTED:{rejection_reason}"
                ):
                    raise BenchmarkExecutionError(
                        "V3 provider rejection decision binding mismatch"
                    )
                input_tokens = _as_nonnegative_int(
                    usage.get("actual_input_tokens"), -1
                )
                output_tokens = _as_nonnegative_int(
                    usage.get("actual_output_tokens"), -1
                )
                cached_tokens = _as_nonnegative_int(
                    usage.get("cached_input_tokens"), -1
                )
                tokens_used = _as_nonnegative_int(
                    usage.get("actual_total_tokens"), -1
                )
                proposal_usage_matches = True
            else:
                raise BenchmarkExecutionError(
                    "V3 live Planner completed outcome is unsupported"
                )
            if (
                input_tokens <= 0
                or output_tokens < 0
                or (
                    completed_outcome == "PROPOSAL"
                    and output_tokens == 0
                )
                or cached_tokens < 0
                or tokens_used != input_tokens + output_tokens
                or not proposal_usage_matches
                or completed.get("input_tokens") != input_tokens
                or completed.get("output_tokens") != output_tokens
                or completed.get("cached_input_tokens") != cached_tokens
                or completed.get("tokens_used") != tokens_used
            ):
                raise BenchmarkExecutionError(
                    "V3 live Planner outcome usage is inconsistent"
                )
            ledger_action = events_by_action.get(action_id, [])
            ledger_started = [
                event for event in ledger_action if event.get("state") == "STARTED"
            ]
            ledger_completed = [
                event
                for event in ledger_action
                if event.get("state") == "COMPLETED"
            ]
            estimated_tokens = (
                ledger_started[0].get("estimated_tokens")
                if len(ledger_started) == 1
                else None
            )
            charged_tokens = (
                ledger_completed[0].get("tokens_used")
                if len(ledger_completed) == 1
                else None
            )
            if (
                len(ledger_started) != 1
                or len(ledger_completed) != 1
                or len(ledger_action) != 2
                or ledger_started[0].get("kind") != "llm"
                or ledger_started[0].get("tool_config_hash") != request_sha256
                or ledger_completed[0].get("token_reservation_overrun") is True
                or isinstance(estimated_tokens, bool)
                or not isinstance(estimated_tokens, int)
                or estimated_tokens <= 0
                or isinstance(charged_tokens, bool)
                or not isinstance(charged_tokens, int)
                or charged_tokens > estimated_tokens
                or ledger_completed[0].get("result_ref") != output_ref
                or ledger_completed[0].get("result_sha256") != output_sha256
                or ledger_completed[0].get("tokens_used") != tokens_used
                or ledger_completed[0].get("input_tokens") != input_tokens
                or ledger_completed[0].get("output_tokens") != output_tokens
                or ledger_completed[0].get("cached_input_tokens") != cached_tokens
            ):
                raise BenchmarkExecutionError(
                    "V3 live Planner outcome is not bound to the token ledger"
                )
            live_action_ids.add(action_id)
            model_outcomes.append(
                {
                    "action_id": action_id,
                    "outcome": completed_outcome,
                    "provider": provider_request.get("provider"),
                    "model": spec.model,
                    "request_ref": request_ref,
                    "request_sha256": request_sha256,
                    "outcome_ref": output_ref,
                    "outcome_sha256": output_sha256,
                    "rejection_ref": rejection_ref,
                    "rejection_reason": rejection_reason,
                    "tokens_used": tokens_used,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cached_input_tokens": cached_tokens,
                }
            )

        llm_terminal_events = [
            event
            for event in ledger_events
            if event.get("kind") == "llm"
            and event.get("state") in {"COMPLETED", "AMBIGUOUS"}
        ]
        try:
            completed_llm_ids = completed_llm_action_ids(
                llm_terminal_events
            )
        except PlannerActionError as exc:
            raise BenchmarkExecutionError(str(exc)) from exc
        if completed_llm_ids != live_action_ids:
            raise BenchmarkExecutionError(
                "V3 live Planner artifacts do not cover all completed ledger calls"
            )

        top_live_action_id = result.get("live_planner_action_id")
        if isinstance(top_live_action_id, str) and top_live_action_id:
            matching_outcomes = [
                outcome
                for outcome in model_outcomes
                if outcome.get("action_id") == top_live_action_id
            ]
            if (
                len(matching_outcomes) != 1
                or matching_outcomes[0].get("request_ref")
                != result.get("live_planner_request_ref")
                or matching_outcomes[0].get("request_sha256")
                != result.get("live_planner_request_sha256")
                or matching_outcomes[0].get("outcome_ref")
                != result.get("live_planner_output_ref")
                or matching_outcomes[0].get("outcome_sha256")
                != result.get("live_planner_output_sha256")
            ):
                raise BenchmarkExecutionError(
                    "V3 terminal live Planner refs do not match its model outcome"
                )

        budget = _mapping(result.get("budget"))
        tool_used = _mapping(budget.get("tool_used"))
        llm_calls = _as_nonnegative_int(tool_used.get("llm"))
        ledger_tokens = sum(
            _as_nonnegative_int(event.get("tokens_used"))
            for event in llm_terminal_events
        )
        ledger_input = sum(
            _as_nonnegative_int(event.get("input_tokens"))
            for event in llm_terminal_events
        )
        ledger_output = sum(
            _as_nonnegative_int(event.get("output_tokens"))
            for event in llm_terminal_events
        )
        ledger_cached = sum(
            _as_nonnegative_int(event.get("cached_input_tokens"))
            for event in llm_terminal_events
        )
        if (
            llm_calls != len(llm_terminal_events)
            or _as_nonnegative_int(budget.get("tokens_used")) != ledger_tokens
            or _as_nonnegative_int(budget.get("input_tokens_used")) != ledger_input
            or _as_nonnegative_int(budget.get("output_tokens_used")) != ledger_output
            or _as_nonnegative_int(budget.get("cached_input_tokens_used"))
            != ledger_cached
        ):
            raise BenchmarkExecutionError(
                "V3 terminal usage does not match its token ledger"
            )

        status = str(result.get("status", "")).upper()
        if status == "DONE" and (
            not model_outcomes
            or any(event.get("state") != "COMPLETED" for event in llm_terminal_events)
            or budget.get("token_usage_complete") is not True
        ):
            raise BenchmarkExecutionError(
                "successful REAL model row lacks complete live Planner usage"
            )

        for key in (
            "live_planner_request_ref",
            "live_planner_output_ref",
            "live_planner_started_ref",
            "live_planner_completed_ref",
        ):
            reference = result.get(key)
            if status == "DONE" and (not isinstance(reference, str) or not reference):
                raise BenchmarkExecutionError(
                    f"successful REAL model row lacks {key}"
                )
            if isinstance(reference, str) and reference:
                covered(reference)

        final_artifacts: dict[str, dict[str, str]] = {}
        validation = _mapping(result.get("final_validation"))
        final_policy = str(
            config.get("final_validation_policy", "full_internal_audit")
        )
        if final_policy not in {"task_contract", "full_internal_audit"}:
            raise BenchmarkExecutionError(
                "V3 run config has an unsupported final validation policy"
            )
        task_requires_cosim = task_spec.get("requires_cosim")
        if not isinstance(task_requires_cosim, bool):
            raise BenchmarkExecutionError(
                "V3 task spec lacks a boolean requires_cosim contract"
            )
        required_final_stages = {"csim", "synth"}
        if final_policy == "full_internal_audit" or task_requires_cosim:
            required_final_stages.add("cosim")
        for stage in ("csim", "synth", "cosim"):
            record = _mapping(validation.get(stage))
            stage_required = stage in required_final_stages
            if status == "DONE" and stage_required and (
                str(record.get("status", "")).upper() not in _PASS_STATUSES
                or record.get("cached") is not False
                or record.get("validation_scope") != "search_closeout"
            ):
                raise BenchmarkExecutionError(
                    f"successful REAL model row lacks fresh final {stage}"
                )
            if status == "DONE" and not stage_required:
                if (
                    str(record.get("status", "")).upper()
                    not in {"NOT_RUN", "SKIPPED"}
                    or any(
                        record.get(name) not in (None, "")
                        for name in (
                            "action_id",
                            "result_ref",
                            "validation_scope",
                        )
                    )
                ):
                    raise BenchmarkExecutionError(
                        "task_contract REAL row has unexpected final cosim"
                    )
                continue
            reference = record.get("result_ref")
            if reference is None or reference == "":
                if status == "DONE" and stage_required:
                    raise BenchmarkExecutionError(
                        f"successful REAL model row lacks final {stage} result_ref"
                    )
                continue
            action = validate_json_ref(
                role=f"final_{stage}",
                reference_value=reference,
                required=True,
            )
            assert action is not None and isinstance(reference, str)
            if (
                action.get("kind") != stage
                or action.get("validation_scope") != "search_closeout"
                or action.get("action_id") != record.get("action_id")
                or (
                    status == "DONE"
                    and action.get("ok") is not True
                )
            ):
                raise BenchmarkExecutionError(
                    f"final {stage} action does not match terminal provenance"
                )
            if final_source is not None and (
                action.get("candidate_id") != final_source["candidate_id"]
                or action.get("code_hash") != final_source["sha256"]
            ):
                raise BenchmarkExecutionError(
                    f"final {stage} action is not bound to the final Candidate"
                )
            final_artifacts[stage] = {
                "ref": reference,
                "sha256": covered(reference),
            }

        return {
            "schema_version": "v3d.real-provenance-receipt.v1",
            "source_result": {"ref": source_ref, "sha256": source_sha256},
            "agent_search_source": agent_search_source,
            "independent_certification": certification_provenance,
            "package_manifest": {
                "ref": manifest_ref,
                "sha256": manifest_sha256,
                "file_sha256": manifest_file_sha256,
            },
            "execution_binding": {
                "ref": binding_ref,
                "sha256": binding_sha256,
            },
            "executor_command": {
                "ref": command_ref,
                "sha256": command_sha256,
            },
            "run_config": {"ref": config_ref, "sha256": config_sha256},
            "task_spec": {"ref": task_spec_ref, "sha256": task_spec_sha256},
            "final_candidate_source": final_source,
            "planner_artifacts": sorted(
                hash_bound_refs, key=lambda item: (item["role"], item["ref"])
            ),
            "final_validation_artifacts": final_artifacts,
            "model_outcomes": model_outcomes,
            "token_ledger": {
                "ref": "budget_ledger.jsonl",
                "sha256": ledger_sha256,
                "llm_calls": llm_calls,
                "tokens_used": ledger_tokens,
                "input_tokens": ledger_input,
                "output_tokens": ledger_output,
                "cached_input_tokens": ledger_cached,
            },
        }


def default_executor(
    backend: str,
    *,
    validation_profile: str = "strict",
    final_validation_policy: str = "task_contract",
    evidence_memory_mode: str = "on",
    max_planner_rounds: int | None = None,
    max_no_improvement_rounds: int = 2,
    enable_final_fallback: bool = False,
    continuation_policy_mode: str = "shadow",
    continuation_policy_version: str = "v2",
    continuation_admission_manifest: Path | str | None = None,
    experience_mode: str = "shadow",
    experience_store: Path | str | None = None,
    experience_ranker_version: str = "v1",
    experience_admission_manifest: Path | str | None = None,
    experience_task_split: str | None = None,
) -> BenchmarkExecutor:
    if backend == "demo":
        return SyntheticBenchmarkExecutor(EvidenceClass.DEMO)
    if backend == "deterministic":
        return SyntheticBenchmarkExecutor(EvidenceClass.DETERMINISTIC)
    if backend == "vitis":
        return V3PrototypeCLIExecutor(
            validation_profile=validation_profile,
            final_validation_policy=final_validation_policy,
            evidence_memory_mode=evidence_memory_mode,
            max_planner_rounds=max_planner_rounds,
            max_no_improvement_rounds=max_no_improvement_rounds,
            enable_final_fallback=enable_final_fallback,
            continuation_policy_mode=continuation_policy_mode,
            continuation_policy_version=continuation_policy_version,
            continuation_admission_manifest=continuation_admission_manifest,
            experience_mode=experience_mode,
            experience_store=experience_store,
            experience_ranker_version=experience_ranker_version,
            experience_admission_manifest=experience_admission_manifest,
            experience_task_split=experience_task_split,
        )
    raise ValueError(f"unsupported backend: {backend}")


def _executor_fingerprint(executor: object) -> str:
    method = getattr(executor, "fingerprint", None)
    value = method() if callable(method) else None
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError("executor fingerprint must be a non-empty string")
    return value.strip()


def _executor_evidence_class(executor: object, backend: str) -> EvidenceClass:
    raw = getattr(executor, "evidence_class", None)
    if isinstance(raw, EvidenceClass):
        declared = raw
    else:
        try:
            declared = EvidenceClass(str(raw))
        except ValueError:
            declared = {
                "demo": EvidenceClass.DEMO,
                "deterministic": EvidenceClass.DETERMINISTIC,
                "vitis": EvidenceClass.REAL,
            }[backend]
    expected = {
        "demo": EvidenceClass.DEMO,
        "deterministic": EvidenceClass.DETERMINISTIC,
        "vitis": EvidenceClass.REAL,
    }[backend]
    if declared is not expected:
        raise BenchmarkError(
            f"executor evidence class {declared.value} conflicts with backend {backend}"
        )
    return declared


def _execution_policy_fingerprint(config: BenchmarkConfig) -> str:
    """Hash batch policy which can alter whether/how a slot is executed."""

    return _sha256_json(
        {
            "schema_version": "v3d.execution-policy.v1",
            "python_implementation": sys.implementation.name,
            "python_version": sys.version.split()[0],
            "max_runtime_seconds": config.max_runtime_seconds,
            "validation_profile": config.validation_profile,
            "final_validation_policy": config.final_validation_policy,
            "evidence_memory_mode": config.evidence_memory_mode,
            "max_planner_rounds": config.max_planner_rounds,
            "max_no_improvement_rounds": (
                config.max_no_improvement_rounds
            ),
            "enable_final_fallback": config.enable_final_fallback,
            "continuation_policy_mode": config.continuation_policy_mode,
            "continuation_policy_version": config.continuation_policy_version,
            "continuation_admission_snapshot": {
                "role": "CONTINUATION_ENFORCE_ADMISSION",
                "present": config.continuation_admission_manifest is not None,
                "size_bytes": config.continuation_admission_size_bytes,
                "sha256": config.continuation_admission_sha256,
            },
            "full_agent_manifest_snapshot": (
                {
                    "sha256": config.full_agent_manifest_sha256,
                    "artifact_sha256s": dict(
                        sorted(config.full_agent_artifact_sha256s.items())
                    ),
                }
                if config.full_agent_manifest is not None
                else None
            ),
            "experience_mode": config.experience_mode,
            "experience_ranker_version": config.experience_ranker_version,
            "experience_task_split": config.experience_task_split,
            "experience_store_snapshot": {
                "role": "READ_ONLY_SEED",
                "present": config.experience_store is not None,
                "size_bytes": config.experience_store_size_bytes,
                "sha256": config.experience_store_sha256,
            },
            "experience_admission_snapshot": {
                "role": "EXPERIENCE_GUIDED_ADMISSION",
                "present": config.experience_admission_manifest is not None,
                "size_bytes": config.experience_admission_size_bytes,
                "sha256": config.experience_admission_sha256,
            },
            "per_run_timeout_policy": "remaining_batch_runtime",
            "max_parallel_runs": 1,
            "vitis_serialized": True,
        }
    )


def _run_fingerprint(
    descriptor: TaskDescriptor,
    *,
    model: str,
    repeat_index: int,
    backend: str,
    executor_fingerprint: str,
    execution_policy_fingerprint: str | None = None,
) -> str:
    return _sha256_json(
        {
            "schema": RUN_SCHEMA,
            "runner": RUNNER_FINGERPRINT,
            "implementation_fingerprint": _implementation_fingerprint(),
            "task_fingerprint": descriptor.task_fingerprint,
            "task_id": descriptor.task_id,
            "split": descriptor.split,
            "algorithm_family": descriptor.algorithm_family,
            "expected_mode": descriptor.expected_mode,
            "model": model,
            "repeat_index": repeat_index,
            "backend": backend,
            "executor_fingerprint": executor_fingerprint,
            "execution_policy_fingerprint": execution_policy_fingerprint,
        }
    )


def _make_run_id(
    descriptor: TaskDescriptor, model: str, repeat_index: int, fingerprint: str
) -> str:
    return "--".join(
        (
            _safe_slug(descriptor.task_id, fallback="task"),
            _safe_slug(model, fallback="model"),
            f"r{repeat_index:03d}",
            fingerprint[:12],
        )
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _routed_mode(result: Mapping[str, object]) -> str | None:
    direct = _normalise_mode(result.get("mode"))
    if direct is not None:
        return direct
    return _normalise_mode(_mapping(result.get("phase_decision")).get("mode"))


def _fresh_final(
    result: Mapping[str, object],
    *,
    requires_cosim: bool,
) -> tuple[bool, bool]:
    certification = _mapping(result.get("final_certification"))
    if certification:
        passed = (
            certification.get("status") == "PASS"
            and certification.get("budget_domain")
            == "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET"
            and certification.get("agent_credits_charged") == 0
        )
        return passed, passed
    validation = _mapping(result.get("final_validation"))
    cosim = _mapping(validation.get("cosim"))
    cosim_ran = str(cosim.get("status", "")).upper() not in {
        "",
        "NOT_RUN",
        "SKIPPED",
    }
    stages = (
        ("csim", "synth", "cosim")
        if requires_cosim or cosim_ran
        else ("csim", "synth")
    )
    records = [_mapping(validation.get(stage)) for stage in stages]
    complete = all(
        str(record.get("status", "")).upper() in _PASS_STATUSES for record in records
    )
    fresh = complete and all(record.get("cached") is False for record in records)
    return complete, fresh


def _acceleration(result: Mapping[str, object]) -> float | None:
    for value in (
        result.get("acceleration_vs_baseline"),
        _mapping(result.get("metrics")).get("acceleration_vs_baseline"),
    ):
        parsed = _as_finite_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    rounds = result.get("candidate_rounds")
    if not isinstance(rounds, list):
        return None
    baseline_id = str(result.get("baseline_candidate_id") or "candidate_000")
    final_id = result.get("final_candidate_id")
    baseline_latency: float | None = None
    final_latency: float | None = None
    declared: float | None = None
    for raw in rounds:
        row = _mapping(raw)
        candidate_id = str(row.get("candidate_id", ""))
        latency = _as_finite_float(row.get("latency_worst"))
        if candidate_id == baseline_id and latency is not None and latency > 0:
            baseline_latency = latency
        if final_id is not None and candidate_id == str(final_id):
            if latency is not None and latency > 0:
                final_latency = latency
            candidate_acceleration = _as_finite_float(
                row.get("acceleration_vs_baseline")
            )
            if candidate_acceleration is not None and candidate_acceleration > 0:
                declared = candidate_acceleration
    if baseline_latency is not None and final_latency is not None:
        return baseline_latency / final_latency
    return declared


def _patch_rejections(result: Mapping[str, object]) -> tuple[int, int, list[str]]:
    rounds = result.get("candidate_rounds")
    rounds = rounds if isinstance(rounds, list) else []
    baseline_id = str(result.get("baseline_candidate_id") or "candidate_000")
    candidates = 0
    reasons: list[str] = []
    for raw in rounds:
        row = _mapping(raw)
        if str(row.get("candidate_id", "")) == baseline_id:
            continue
        candidates += 1
        decision = str(row.get("decision", row.get("status", ""))).upper()
        if decision == "REJECTED" or "REJECT" in decision:
            reasons.append(
                str(
                    row.get("decision_reason")
                    or row.get("rejection_reason")
                    or "UNSPECIFIED"
                )
            )
    events = result.get("node_events")
    if isinstance(events, list):
        for raw in events:
            event = _mapping(raw)
            if event.get("node") != "record_rejected_proposal":
                continue
            candidates += 1
            reasons.append(str(event.get("outcome") or "PATCH_POLICY_REJECTED"))
    return len(reasons), candidates, reasons


def _failure_stage(
    result: Mapping[str, object] | None,
    *,
    error_type: str | None = None,
    load_failure: bool = False,
) -> str | None:
    if load_failure:
        return "TASK_LOAD"
    if result is None:
        return "EXECUTOR"
    if str(result.get("status", "FAILED")).upper() == "DONE":
        return None
    validation = _mapping(result.get("final_validation"))
    for stage in ("csim", "synth", "cosim"):
        record = _mapping(validation.get(stage))
        status = str(record.get("status", "NOT_RUN")).upper()
        if status not in _PASS_STATUSES | {"", "NOT_RUN", "SKIPPED", "PENDING"}:
            return f"FINAL_{stage.upper()}"
    text = " ".join(
        str(item).upper()
        for item in (
            result.get("stop_reason"),
            result.get("last_tool_phase"),
            result.get("last_tool_reason"),
            error_type,
        )
        if item
    )
    for label, tokens in (
        ("FINAL_COSIM", ("FINAL_COSIM",)),
        ("FINAL_SYNTH", ("FINAL_SYNTH",)),
        ("FINAL_CSIM", ("FINAL_CSIM",)),
        ("PATCH", ("PATCH", "DIFF", "APPLY")),
        ("PLANNER", ("PLANNER", "LLM", "MODEL")),
        ("BASELINE_COSIM", ("BASELINE_COSIM",)),
        ("BASELINE_SYNTH", ("BASELINE_SYNTH",)),
        ("BASELINE_CSIM", ("BASELINE_CSIM",)),
        ("COSIM", ("COSIM",)),
        ("SYNTH", ("SYNTH",)),
        ("CSIM", ("CSIM",)),
        ("BUDGET", ("BUDGET", "CREDIT", "TOKEN", "RUNTIME")),
    ):
        if any(token in text for token in tokens):
            return label
    return "UNKNOWN"


def _evidence_level(result: Mapping[str, object]) -> str:
    return str(_mapping(result.get("backend")).get("evidence_level", "UNKNOWN"))


def _is_orchestration_receipt(result: Mapping[str, object]) -> bool:
    return result.get("result_schema") == "v3d.orchestration-receipt.v1"


def _evidence_policy_valid(
    evidence_class: EvidenceClass, evidence_level: str, raw_status: str
) -> bool:
    level = evidence_level.upper()
    if evidence_class is EvidenceClass.DEMO:
        return "DEMO" in level or "ORCHESTRATION_SMOKE" in level
    if evidence_class is EvidenceClass.DETERMINISTIC:
        return "DETERMINISTIC" in level or "TEST_OR_CUSTOM" in level
    if level not in {"REAL_VITIS_VALIDATED", "REAL_VITIS_ATTEMPT_FAILED"}:
        return False
    if raw_status == "DONE":
        return level == "REAL_VITIS_VALIDATED"
    return level == "REAL_VITIS_ATTEMPT_FAILED"


def _normalise_result(
    spec: BenchmarkRunSpec,
    result: Mapping[str, object],
    *,
    evidence_class: EvidenceClass,
    real_evidence_authorized: bool,
    executor_fingerprint: str,
    provenance_receipt: Mapping[str, object] | None,
    started_at: str,
    finished_at: str,
    wall_time_s: float,
) -> dict[str, object]:
    result_task_id = result.get("task_id")
    if result_task_id is not None and str(result_task_id) != spec.task.id:
        raise BenchmarkExecutionError(
            "executor result task_id does not match the scheduled task"
        )
    raw_status = str(result.get("status", "FAILED")).upper()
    status = raw_status
    orchestration_only = _is_orchestration_receipt(result)
    if orchestration_only:
        if evidence_class is EvidenceClass.REAL:
            raise BenchmarkExecutionError(
                "REAL executor cannot return an orchestration-only receipt"
            )
        status = "ORCHESTRATION_ONLY"
    elif status not in _TERMINAL_STATUSES:
        status = "FAILED"
    routed_mode = _routed_mode(result)
    final_complete, fresh_final = _fresh_final(
        result,
        requires_cosim=spec.task.requires_cosim,
    )
    level = _evidence_level(result)
    policy_valid = _evidence_policy_valid(evidence_class, level, raw_status) and (
        evidence_class is not EvidenceClass.REAL or real_evidence_authorized
    )
    e2e_success: bool | None = status == "DONE" and policy_valid
    final_success: bool | None = final_complete and policy_valid
    fresh_success: bool | None = fresh_final and policy_valid
    if orchestration_only:
        # A synthetic scheduling receipt is useful, but none of the benchmark
        # outcome metrics have been observed.
        routed_mode = None
        e2e_success = None
        final_success = None
        fresh_success = None
        final_complete = False
        fresh_final = False
    if not policy_valid:
        status = "FAILED"
        e2e_success = False
        final_success = False
        fresh_success = False
        final_complete = False
        fresh_final = False
    budget = _mapping(result.get("budget"))
    calls = {
        str(key): _as_nonnegative_int(value)
        for key, value in _mapping(budget.get("tool_used")).items()
    }
    rejection_count, patch_candidates, rejection_reasons = _patch_rejections(result)
    expected_mode = spec.descriptor.expected_mode
    routing_was_emitted = not orchestration_only and (
        result.get("mode") is not None
        or _mapping(result.get("phase_decision")).get("mode") is not None
        or raw_status == "DONE"
    )
    router_correct = (
        routed_mode == expected_mode
        if expected_mode is not None and routing_was_emitted
        else None
    )
    raw_result_path = spec.run_dir / "executor_result.json"
    _atomic_json(raw_result_path, result)
    executor_result_sha256 = _sha256_file(raw_result_path)
    if (
        evidence_class is EvidenceClass.REAL
        and real_evidence_authorized
        and provenance_receipt is None
    ):
        raise BenchmarkExecutionError(
            "authorized REAL row lacks a provenance validation receipt"
        )
    source_receipt = _mapping(
        _mapping(provenance_receipt).get("source_result")
    )
    record: dict[str, object] = {
        "schema_version": RUN_SCHEMA,
        "terminal": True,
        "run_id": spec.run_id,
        "run_fingerprint": spec.run_fingerprint,
        "task_fingerprint": spec.descriptor.task_fingerprint,
        "executor_fingerprint": executor_fingerprint,
        "task_id": spec.descriptor.task_id,
        "task_dir": str(spec.descriptor.directory),
        "task_relative_path": spec.descriptor.relative_path,
        "task_type": spec.descriptor.task_type,
        "difficulty": spec.descriptor.difficulty,
        "split": spec.descriptor.split,
        "algorithm_family": spec.descriptor.algorithm_family,
        "algorithm_family_source": spec.descriptor.algorithm_family_source,
        "expected_mode": spec.descriptor.expected_mode,
        "expected_mode_source": spec.descriptor.expected_mode_source,
        "routed_mode": routed_mode,
        "router_correct": router_correct,
        "model": spec.model,
        "repeat_index": spec.repeat_index,
        "backend": spec.backend,
        "evidence_class": evidence_class.value,
        "evidence_level": level,
        "evidence_policy_valid": policy_valid,
        "real_evidence_eligible": (
            not orchestration_only
            and evidence_class is EvidenceClass.REAL
            and real_evidence_authorized
        ),
        "execution_started": True,
        "status": status,
        "raw_status": raw_status,
        "e2e_success": e2e_success,
        "final_validation_success": final_success,
        "fresh_final_success": fresh_success,
        "stop_reason": (
            "EVIDENCE_CLASS_MISMATCH"
            if not policy_valid
            else str(result.get("stop_reason", "UNKNOWN"))
        ),
        "failure_stage": (
            None
            if orchestration_only and policy_valid
            else
            "EVIDENCE_POLICY"
            if not policy_valid
            else _failure_stage(result)
        ),
        "acceleration_vs_baseline": (
            None if orchestration_only else _acceleration(result)
        ),
        "credits_used": _as_nonnegative_int(budget.get("credits_used")),
        "tokens_used": _as_nonnegative_int(budget.get("tokens_used")),
        "input_tokens_used": _as_nonnegative_int(budget.get("input_tokens_used")),
        "output_tokens_used": _as_nonnegative_int(budget.get("output_tokens_used")),
        "cached_input_tokens_used": _as_nonnegative_int(
            budget.get("cached_input_tokens_used")
        ),
        "tool_calls": calls,
        "model_calls": calls.get("llm", 0),
        "reported_runtime_s": _as_finite_float(budget.get("runtime_used_seconds")),
        "wall_time_s": wall_time_s,
        "patch_candidates": patch_candidates,
        "patch_rejections": rejection_count,
        "patch_rejection_reasons": rejection_reasons,
        "started_at": started_at,
        "finished_at": finished_at,
        "run_dir": str(spec.run_dir),
        "result_ref": "executor_result.json",
        "result_sha256": executor_result_sha256,
        "source_result_ref": (
            "v3_certified_result.json"
            if (spec.run_dir / "v3_certified_result.json").is_file()
            else "v3_prototype_result.json"
            if (spec.run_dir / "v3_prototype_result.json").is_file()
            else None
        ),
        "source_result_sha256": source_receipt.get("sha256"),
        "provenance_validation": (
            dict(provenance_receipt) if provenance_receipt is not None else None
        ),
        "orchestration_receipt_ref": (
            "executor_result.json" if orchestration_only else None
        ),
        "metric_applicability": (
            dict(_mapping(result.get("metric_applicability")))
            if orchestration_only
            else {
                "phase_router": "MEASURED" if router_correct is not None else "N/A",
                "e2e_success": "MEASURED",
                "fresh_final": "MEASURED",
                "acceleration": (
                    "MEASURED"
                    if _acceleration(result) is not None
                    else "N/A"
                ),
            }
        ),
        "resumed": False,
    }
    return record


_UNKNOWN_USAGE = "UNKNOWN"


def _partial_safe_path(run_dir: Path, reference: object) -> Path:
    if not isinstance(reference, str) or not reference:
        raise BenchmarkExecutionError("partial artifact reference is empty")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise BenchmarkExecutionError("partial artifact reference is unsafe")
    root = run_dir.resolve()
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise BenchmarkExecutionError(
                "partial artifact reference uses a symbolic link"
            )
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BenchmarkExecutionError(
            "partial artifact reference escapes its run"
        ) from exc
    return resolved


def _partial_json(run_dir: Path, reference: object) -> dict[str, object]:
    path = _partial_safe_path(run_dir, reference)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkExecutionError(
            f"partial artifact is unreadable: {reference}"
        ) from exc
    if not isinstance(value, dict):
        raise BenchmarkExecutionError(
            f"partial artifact is not an object: {reference}"
        )
    return value


def _validate_partial_tool_result(
    run_dir: Path,
    *,
    started: Mapping[str, object],
    completed: Mapping[str, object],
) -> dict[str, object]:
    reference = completed.get("result_ref")
    result_path = _partial_safe_path(run_dir, reference)
    try:
        data = result_path.read_bytes()
        result = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkExecutionError(
            "completed partial tool action has no readable result"
        ) from exc
    if not isinstance(result, dict):
        raise BenchmarkExecutionError(
            "completed partial tool result is not an object"
        )
    action_id = completed.get("action_id")
    expected = {
        "action_id": action_id,
        "kind": completed.get("kind"),
        "candidate_id": started.get("candidate_id"),
        "code_hash": started.get("code_hash"),
        "tool_config_hash": started.get("tool_config_hash"),
        "result_ref": reference,
    }
    if any(result.get(name) != value for name, value in expected.items()):
        raise BenchmarkExecutionError(
            "completed partial tool result binding is inconsistent"
        )
    if completed.get("result_sha256") != _sha256_bytes(data):
        raise BenchmarkExecutionError(
            "completed partial tool result digest is inconsistent"
        )
    artifacts = result.get("artifacts")
    hashes = result.get("artifact_hashes")
    if not isinstance(artifacts, Mapping) or not isinstance(hashes, Mapping):
        raise BenchmarkExecutionError(
            "completed partial tool artifact bindings are missing"
        )
    action_root = result_path.parent
    actual_hashes: dict[str, str] = {}
    for name, artifact_ref in artifacts.items():
        artifact_path = _partial_safe_path(
            run_dir,
            str(
                (
                    action_root.relative_to(run_dir.resolve())
                    / str(artifact_ref)
                )
            ),
        )
        if not artifact_path.is_file():
            raise BenchmarkExecutionError(
                "completed partial tool artifact is missing"
            )
        actual_hashes[str(name)] = _sha256_bytes(artifact_path.read_bytes())
    if actual_hashes != {str(key): str(value) for key, value in hashes.items()}:
        raise BenchmarkExecutionError(
            "completed partial tool artifact digest is inconsistent"
        )
    return result


def _validate_partial_planner_action(
    run_dir: Path,
    *,
    started: Mapping[str, object],
    completed: Mapping[str, object],
) -> None:
    action_id = completed.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise BenchmarkExecutionError(
            "completed partial Planner action has no identity"
        )
    started_journal = _partial_json(
        run_dir,
        f"control/live_planner_actions/{action_id}.started.json",
    )
    completed_journal = _partial_json(
        run_dir,
        f"control/live_planner_actions/{action_id}.completed.json",
    )
    action_request = started_journal.get("request")
    if (
        started_journal.get("action_id") != action_id
        or started_journal.get("status") != "STARTED"
        or completed_journal.get("action_id") != action_id
        or completed_journal.get("status") != "COMPLETED"
        or not isinstance(action_request, Mapping)
        or completed_journal.get("request") != action_request
    ):
        raise BenchmarkExecutionError(
            "partial Planner STARTED/COMPLETED journal is inconsistent"
        )
    request_ref = action_request.get("request_ref")
    request_audit = _partial_json(run_dir, request_ref)
    try:
        validate_live_planner_action_identity(
            action_request,
            request_audit,
            expected_action_id=action_id,
        )
    except PlannerActionError as exc:
        raise BenchmarkExecutionError(
            "partial Planner deterministic identity is invalid"
        ) from exc
    if action_request.get("request_sha256") != _sha256_json(request_audit):
        raise BenchmarkExecutionError(
            "partial Planner request digest is inconsistent"
        )
    input_value = _partial_json(run_dir, action_request.get("input_ref"))
    if action_request.get("input_sha256") != _sha256_json(input_value):
        raise BenchmarkExecutionError(
            "partial Planner input digest is inconsistent"
        )
    result_ref = completed.get("result_ref")
    if (
        completed_journal.get("result_ref") != result_ref
        or completed_journal.get("result_sha256")
        != completed.get("result_sha256")
    ):
        raise BenchmarkExecutionError(
            "partial Planner journal and Ledger result binding differ"
        )
    result_path = _partial_safe_path(run_dir, result_ref)
    try:
        result_data = result_path.read_bytes()
        result_value = json.loads(result_data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkExecutionError(
            "partial Planner outcome is unreadable"
        ) from exc
    if (
        not isinstance(result_value, Mapping)
        or result_value.get("action_id") != action_id
        or completed.get("result_sha256") != _sha256_bytes(result_data)
    ):
        raise BenchmarkExecutionError(
            "partial Planner outcome binding is inconsistent"
        )
    usage = result_value.get("usage")
    if not isinstance(usage, Mapping) or usage.get("usage_complete") is not True:
        raise BenchmarkExecutionError(
            "partial Planner outcome has incomplete usage"
        )
    for field in (
        "tokens_used",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
    ):
        journal_value = completed_journal.get(field)
        ledger_value = completed.get(field)
        usage_value = usage.get(field)
        if field == "tokens_used":
            expected_value = usage_value
        else:
            expected_value = usage_value
        if journal_value != ledger_value or ledger_value != expected_value:
            raise BenchmarkExecutionError(
                "partial Planner usage differs across artifacts"
            )


def recover_partial_run_report(
    run_dir: str | Path,
) -> dict[str, object]:
    """Read-only, fail-closed recovery for an executor-interrupted run."""

    root = Path(run_dir).resolve()
    report: dict[str, object] = {
        "schema_version": "v3d.partial-run-recovery.v1",
        "status": "ABSENT",
        "usage_recovered_from_partial_artifacts": False,
        "credits_used": _UNKNOWN_USAGE,
        "pending_credits_reserved": _UNKNOWN_USAGE,
        "credits_accounted_conservative": _UNKNOWN_USAGE,
        "tokens_used": _UNKNOWN_USAGE,
        "input_tokens_used": _UNKNOWN_USAGE,
        "output_tokens_used": _UNKNOWN_USAGE,
        "cached_input_tokens_used": _UNKNOWN_USAGE,
        "planner_calls": _UNKNOWN_USAGE,
        "tool_calls": _UNKNOWN_USAGE,
        "pending_tool_calls": _UNKNOWN_USAGE,
        "last_completed_graph_node": _UNKNOWN_USAGE,
        "last_started_tool": _UNKNOWN_USAGE,
        "unfinished_action_ids": _UNKNOWN_USAGE,
        "ledger_planner_artifacts_consistent": _UNKNOWN_USAGE,
        "candidate_frozen": False,
        "agent_terminal_present": (
            (root / "v3_prototype_result.json").is_file()
            or (root / "v3_certified_result.json").is_file()
        ),
        "why_no_agent_terminal": (
            None
            if (root / "v3_prototype_result.json").is_file()
            or (root / "v3_certified_result.json").is_file()
            else "NO_DURABLE_AGENT_TERMINAL_COMMIT_MARKER"
        ),
        "run_dir": str(root),
        "recovery_errors": [],
    }
    ledger_path = root / "budget_ledger.jsonl"
    if not ledger_path.is_file():
        return report
    try:
        ledger_data = ledger_path.read_bytes()
        raw_lines = ledger_data.decode("utf-8").splitlines()
        events: list[dict[str, object]] = []
        for number, line in enumerate(raw_lines, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"ledger line {number} is not an object")
            events.append(value)
        if (
            not events
            or events[0].get("state") != "INITIALIZED"
            or [event.get("sequence") for event in events]
            != list(range(len(events)))
        ):
            raise BenchmarkExecutionError(
                "partial Ledger initialization or sequence is invalid"
            )
        initialized = events[0]
        config = initialized.get("config")
        if not isinstance(config, Mapping):
            raise BenchmarkExecutionError(
                "partial Ledger has no valid configuration"
            )
        by_action: dict[str, list[dict[str, object]]] = {}
        for event in events[1:]:
            action_id = event.get("action_id")
            if not isinstance(action_id, str) or not action_id:
                raise BenchmarkExecutionError(
                    "partial Ledger action has no identity"
                )
            by_action.setdefault(action_id, []).append(event)

        completed_events: list[dict[str, object]] = []
        pending_events: list[dict[str, object]] = []
        credits_used = 0
        pending_credits = 0
        tokens_used = 0
        input_tokens = 0
        output_tokens = 0
        cached_input_tokens = 0
        tool_calls: Counter[str] = Counter()
        pending_calls: Counter[str] = Counter()
        for action_id, action_events in by_action.items():
            started_events = [
                event
                for event in action_events
                if event.get("state") == "STARTED"
            ]
            terminal_events = [
                event
                for event in action_events
                if event.get("state") in {"COMPLETED", "AMBIGUOUS"}
            ]
            if len(started_events) != 1 or len(terminal_events) > 1:
                raise BenchmarkExecutionError(
                    "partial Ledger action lifecycle is invalid"
                )
            started = started_events[0]
            terminal = terminal_events[0] if terminal_events else None
            if terminal is None:
                pending_events.append(started)
                pending_cost = started.get("estimated_cost")
                if isinstance(pending_cost, bool) or not isinstance(
                    pending_cost, int
                ):
                    raise BenchmarkExecutionError(
                        "partial Ledger pending cost is invalid"
                    )
                pending_credits += pending_cost
                pending_calls[str(started.get("kind"))] += 1
                continue
            if terminal.get("state") == "AMBIGUOUS":
                pending_events.append(started)
                pending_cost = started.get("estimated_cost")
                if isinstance(pending_cost, bool) or not isinstance(
                    pending_cost, int
                ):
                    raise BenchmarkExecutionError(
                        "partial Ledger ambiguous cost is invalid"
                    )
                pending_credits += pending_cost
                pending_calls[str(started.get("kind"))] += 1
                continue
            if terminal.get("kind") != started.get("kind"):
                raise BenchmarkExecutionError(
                    "partial Ledger STARTED/COMPLETED kind differs"
                )
            completed_events.append(terminal)
            actual_cost = terminal.get("actual_cost")
            if isinstance(actual_cost, bool) or not isinstance(actual_cost, int):
                raise BenchmarkExecutionError(
                    "partial Ledger completed cost is invalid"
                )
            credits_used += actual_cost
            kind = str(terminal.get("kind"))
            tool_calls[kind] += 1
            for name, accumulator in (
                ("tokens_used", "tokens"),
                ("input_tokens", "input"),
                ("output_tokens", "output"),
                ("cached_input_tokens", "cached"),
            ):
                value = terminal.get(name)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise BenchmarkExecutionError(
                        f"partial Ledger {name} is invalid"
                    )
                if accumulator == "tokens":
                    tokens_used += value
                elif accumulator == "input":
                    input_tokens += value
                elif accumulator == "output":
                    output_tokens += value
                else:
                    cached_input_tokens += value
            if kind == "llm":
                _validate_partial_planner_action(
                    root,
                    started=started,
                    completed=terminal,
                )
            else:
                _validate_partial_tool_result(
                    root,
                    started=started,
                    completed=terminal,
                )

        pending_llm = int(pending_calls.get("llm", 0))
        report.update(
            {
                "status": "VALID",
                "usage_recovered_from_partial_artifacts": True,
                "ledger_sha256": _sha256_bytes(ledger_data),
                "credits_used": credits_used,
                "pending_credits_reserved": pending_credits,
                "credits_accounted_conservative": (
                    credits_used + pending_credits
                ),
                "tokens_used": (
                    tokens_used if pending_llm == 0 else _UNKNOWN_USAGE
                ),
                "input_tokens_used": (
                    input_tokens if pending_llm == 0 else _UNKNOWN_USAGE
                ),
                "output_tokens_used": (
                    output_tokens if pending_llm == 0 else _UNKNOWN_USAGE
                ),
                "cached_input_tokens_used": (
                    cached_input_tokens
                    if pending_llm == 0
                    else _UNKNOWN_USAGE
                ),
                "planner_calls": int(tool_calls.get("llm", 0)),
                "tool_calls": dict(sorted(tool_calls.items())),
                "pending_tool_calls": dict(sorted(pending_calls.items())),
                "unfinished_action_ids": sorted(
                    str(event["action_id"]) for event in pending_events
                ),
                "ledger_planner_artifacts_consistent": True,
            }
        )

        trace_path = root / "trace.jsonl"
        if trace_path.is_file():
            trace_events: list[Mapping[str, object]] = []
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, Mapping):
                        trace_events.append(value)
            completed_nodes = [
                event
                for event in trace_events
                if event.get("event") == "V3_NODE_COMPLETED"
            ]
            started_tools = [
                event
                for event in trace_events
                if event.get("event") == "TOOL_STARTED"
            ]
            if completed_nodes:
                report["last_completed_graph_node"] = completed_nodes[-1].get(
                    "node", _UNKNOWN_USAGE
                )
            if started_tools:
                last_started = started_tools[-1]
                report["last_started_tool"] = {
                    "kind": last_started.get("kind"),
                    "action_id": last_started.get("action_id"),
                    "candidate_id": last_started.get("candidate_id"),
                }

        registry_path = root / "candidate_registry.json"
        if registry_path.is_file():
            registry = _partial_json(root, "candidate_registry.json")
            candidates = registry.get("candidates")
            report["candidate_count"] = (
                len(candidates) if isinstance(candidates, Mapping) else 0
            )
            report["active_candidate_id"] = registry.get(
                "active_candidate_id"
            )
            report["best_candidate_id"] = registry.get("best_candidate_id")
            final_candidate_id = registry.get("final_candidate_id")
            report["final_candidate_id"] = final_candidate_id
            report["candidate_frozen"] = (
                isinstance(final_candidate_id, str)
                and bool(final_candidate_id)
                and report["agent_terminal_present"] is True
            )
            if isinstance(candidates, Mapping):
                validations: dict[str, object] = {}
                for candidate_id, candidate in candidates.items():
                    if isinstance(candidate, Mapping):
                        validations[str(candidate_id)] = candidate.get(
                            "validation"
                        )
                report["candidate_validation"] = validations
        return report
    except (
        BenchmarkExecutionError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        report["status"] = "INVALID"
        report["usage_recovered_from_partial_artifacts"] = False
        report["ledger_planner_artifacts_consistent"] = False
        report["recovery_errors"] = [f"{type(exc).__name__}: {exc}"]
        return report


def _failure_record(
    descriptor: TaskDescriptor,
    *,
    model: str,
    repeat_index: int,
    backend: str,
    evidence_class: EvidenceClass,
    executor_fingerprint: str,
    run_id: str,
    run_fingerprint: str,
    run_dir: Path,
    started_at: str,
    finished_at: str,
    wall_time_s: float,
    error_type: str,
    detail: str,
    execution_started: bool,
    partial_report: Mapping[str, object] | None = None,
    process_cleanup: Mapping[str, object] | None = None,
) -> dict[str, object]:
    load_failure = not descriptor.loadable
    timeout_failure = error_type in {
        "BenchmarkExecutionTimeout",
        "EXECUTOR_TIMEOUT",
    }
    recovered = (
        partial_report is not None
        and partial_report.get("status") == "VALID"
        and partial_report.get("usage_recovered_from_partial_artifacts")
        is True
    )
    unknown = _UNKNOWN_USAGE if timeout_failure else 0
    credits_used = (
        partial_report.get("credits_used", unknown)
        if recovered and partial_report is not None
        else unknown
    )
    tokens_used = (
        partial_report.get("tokens_used", unknown)
        if recovered and partial_report is not None
        else unknown
    )
    input_tokens_used = (
        partial_report.get("input_tokens_used", unknown)
        if recovered and partial_report is not None
        else unknown
    )
    output_tokens_used = (
        partial_report.get("output_tokens_used", unknown)
        if recovered and partial_report is not None
        else unknown
    )
    cached_input_tokens_used = (
        partial_report.get("cached_input_tokens_used", unknown)
        if recovered and partial_report is not None
        else unknown
    )
    tool_calls = (
        dict(partial_report.get("tool_calls", {}))
        if recovered
        and partial_report is not None
        and isinstance(partial_report.get("tool_calls"), Mapping)
        else {} if not timeout_failure else _UNKNOWN_USAGE
    )
    model_calls = (
        partial_report.get("planner_calls", unknown)
        if recovered and partial_report is not None
        else unknown
    )
    status = "EXECUTOR_TIMEOUT" if timeout_failure else "ERROR"
    return {
        "schema_version": RUN_SCHEMA,
        "terminal": True,
        "run_id": run_id,
        "run_fingerprint": run_fingerprint,
        "task_fingerprint": descriptor.task_fingerprint,
        "executor_fingerprint": executor_fingerprint,
        "task_id": descriptor.task_id,
        "task_dir": str(descriptor.directory),
        "task_relative_path": descriptor.relative_path,
        "task_type": descriptor.task_type,
        "difficulty": descriptor.difficulty,
        "split": descriptor.split,
        "algorithm_family": descriptor.algorithm_family,
        "algorithm_family_source": descriptor.algorithm_family_source,
        "expected_mode": descriptor.expected_mode,
        "expected_mode_source": descriptor.expected_mode_source,
        "routed_mode": None,
        "router_correct": None,
        "model": model,
        "repeat_index": repeat_index,
        "backend": backend,
        "evidence_class": evidence_class.value,
        "evidence_level": "NO_TERMINAL_RESULT",
        "evidence_policy_valid": True,
        # Without a structured terminal result there is no proof that Vitis
        # passed preflight or started.  Keep the configured REAL population for
        # diagnostics, but exclude this row from the real-evidence headline.
        "real_evidence_eligible": False,
        "execution_started": execution_started,
        "status": status,
        "raw_status": status,
        "e2e_success": False,
        "final_validation_success": False,
        "fresh_final_success": False,
        "stop_reason": (
            "EXECUTOR_TIMEOUT_AND_INCOMPLETE_ARTIFACT"
            if timeout_failure
            else error_type
        ),
        "failure_stage": (
            "EXECUTOR_TIMEOUT_AND_INCOMPLETE_ARTIFACT"
            if timeout_failure
            else _failure_stage(
                None, error_type=error_type, load_failure=load_failure
            )
        ),
        "acceleration_vs_baseline": None,
        "credits_used": credits_used,
        "tokens_used": tokens_used,
        "input_tokens_used": input_tokens_used,
        "output_tokens_used": output_tokens_used,
        "cached_input_tokens_used": cached_input_tokens_used,
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "usage_recovered_from_partial_artifacts": recovered,
        "partial_run_report": (
            dict(partial_report) if partial_report is not None else None
        ),
        "process_cleanup": (
            dict(process_cleanup) if process_cleanup is not None else None
        ),
        "pending_credits_reserved": (
            partial_report.get("pending_credits_reserved", unknown)
            if partial_report is not None
            else unknown
        ),
        "pending_tool_calls": (
            partial_report.get("pending_tool_calls", unknown)
            if partial_report is not None
            else unknown
        ),
        "unfinished_action_ids": (
            partial_report.get("unfinished_action_ids", unknown)
            if partial_report is not None
            else unknown
        ),
        "last_completed_graph_node": (
            partial_report.get("last_completed_graph_node", unknown)
            if partial_report is not None
            else unknown
        ),
        "last_started_tool": (
            partial_report.get("last_started_tool", unknown)
            if partial_report is not None
            else unknown
        ),
        "candidate_frozen": (
            partial_report.get("candidate_frozen", False)
            if partial_report is not None
            else False
        ),
        "why_no_agent_terminal": (
            partial_report.get("why_no_agent_terminal")
            if partial_report is not None
            else None
        ),
        "reported_runtime_s": None,
        "wall_time_s": wall_time_s,
        "patch_candidates": 0,
        "patch_rejections": 0,
        "patch_rejection_reasons": [],
        "error": {"type": error_type, "detail": detail},
        "started_at": started_at,
        "finished_at": finished_at,
        "run_dir": str(run_dir),
        "result_ref": None,
        "result_sha256": None,
        "source_result_ref": None,
        "source_result_sha256": None,
        "provenance_validation": None,
        "orchestration_receipt_ref": None,
        "metric_applicability": {
            "phase_router": "N/A_NO_TERMINAL_RESULT",
            "e2e_success": "MEASURED_FAILURE",
            "fresh_final": "MEASURED_FAILURE",
            "acceleration": "N/A_NO_SUCCESSFUL_SYNTHESIS",
        },
        "resumed": False,
    }


def _load_previous_records(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise BenchmarkError(f"cannot read benchmark_results.jsonl: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(
                f"invalid benchmark_results.jsonl line {number}: {exc}"
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != RUN_SCHEMA:
            raise BenchmarkError(
                f"benchmark_results.jsonl line {number} has an incompatible schema"
            )
        records.append(value)
    return records


def _resume_candidate(
    output_dir: Path,
    record: Mapping[str, object],
    *,
    spec: BenchmarkRunSpec | None = None,
    executor: BenchmarkExecutor | None = None,
) -> dict[str, object] | None:
    run_dir_raw = record.get("run_dir")
    if not isinstance(run_dir_raw, str):
        return None
    run_dir = Path(run_dir_raw).resolve()
    try:
        run_dir.relative_to(output_dir)
    except ValueError:
        return None
    durable_path = run_dir / "benchmark_run.json"
    if not durable_path.is_file():
        return None
    try:
        durable = json.loads(durable_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(durable, dict):
        return None
    if (
        durable.get("run_id") != record.get("run_id")
        or durable.get("run_fingerprint") != record.get("run_fingerprint")
        or durable.get("schema_version") != RUN_SCHEMA
        or durable != record
    ):
        return None
    result_ref = record.get("result_ref")
    result_sha256 = record.get("result_sha256")
    if isinstance(result_ref, str) and result_ref:
        try:
            result_path = V3PrototypeCLIExecutor._run_path(run_dir, result_ref)
            result_bytes = result_path.read_bytes()
            executor_result = json.loads(result_bytes.decode("utf-8"))
        except (
            BenchmarkExecutionError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return None
        if (
            not isinstance(executor_result, Mapping)
            or not isinstance(result_sha256, str)
            or _sha256_bytes(result_bytes) != result_sha256
        ):
            return None
    elif result_ref is not None or result_sha256 is not None:
        return None
    else:
        executor_result = None

    if record.get("evidence_class") == EvidenceClass.REAL.value:
        if (
            spec is None
            or type(executor) is not V3PrototypeCLIExecutor
            or str(record.get("run_id")) != spec.run_id
            or record.get("run_fingerprint") != spec.run_fingerprint
            or record.get("task_fingerprint") != spec.descriptor.task_fingerprint
            or record.get("task_id") != spec.task.id
            or record.get("model") != spec.model
            or record.get("repeat_index") != spec.repeat_index
            or record.get("backend") != spec.backend
        ):
            return None
        source_ref = record.get("source_result_ref")
        source_digest = record.get("source_result_sha256")
        if not isinstance(source_ref, str) or not isinstance(source_digest, str):
            return None
        try:
            source_result, actual_source_digest, _ = (
                V3PrototypeCLIExecutor._run_json_with_hash(run_dir, source_ref)
            )
            if (
                actual_source_digest != source_digest
                or executor_result is None
                or dict(executor_result) != dict(source_result)
            ):
                return None
            receipt = executor._validate_terminal_provenance(spec, source_result)
        except (BenchmarkError, OSError, ValueError, TypeError):
            return None
        if receipt != record.get("provenance_validation"):
            return None
    resumed = dict(record)
    resumed["resumed"] = True
    return resumed


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.seek(0, os.SEEK_END) == 0:
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


def _metric_stats(values: Sequence[float]) -> dict[str, object]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "count": 0,
            "total": 0.0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(finite),
        "total": sum(finite),
        "mean": statistics.fmean(finite),
        "median": statistics.median(finite),
        "min": min(finite),
        "max": max(finite),
    }


def _aggregate(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    runs = len(records)
    e2e_rows = [
        record
        for record in records
        if isinstance(record.get("e2e_success"), bool)
    ]
    final_rows = [
        record
        for record in records
        if isinstance(record.get("final_validation_success"), bool)
    ]
    fresh_rows = [
        record
        for record in records
        if isinstance(record.get("fresh_final_success"), bool)
    ]
    e2e = sum(record.get("e2e_success") is True for record in e2e_rows)
    final = sum(
        record.get("final_validation_success") is True for record in final_rows
    )
    fresh = sum(record.get("fresh_final_success") is True for record in fresh_rows)
    router_rows = [
        record for record in records if record.get("router_correct") is not None
    ]
    router_correct = sum(bool(record.get("router_correct")) for record in router_rows)
    accelerations = [
        value
        for record in records
        if record.get("e2e_success") is True
        and record.get("routed_mode") == "OPTIMIZE"
        for value in [_as_finite_float(record.get("acceleration_vs_baseline"))]
        if value is not None and value > 0
    ]
    tool_calls: Counter[str] = Counter()
    for record in records:
        for kind, count in _mapping(record.get("tool_calls")).items():
            tool_calls[str(kind)] += _as_nonnegative_int(count)
    rejection_count = sum(
        _as_nonnegative_int(record.get("patch_rejections")) for record in records
    )
    patch_candidates = sum(
        _as_nonnegative_int(record.get("patch_candidates")) for record in records
    )
    rejection_reasons = Counter(
        str(reason)
        for record in records
        for reason in (
            record.get("patch_rejection_reasons")
            if isinstance(record.get("patch_rejection_reasons"), list)
            else []
        )
    )
    failures = [record for record in records if record.get("e2e_success") is False]
    failure_stages = Counter(str(record.get("failure_stage") or "UNKNOWN") for record in failures)
    failure_reasons = Counter(str(record.get("stop_reason") or "UNKNOWN") for record in failures)
    credits = [
        int(value)
        for record in records
        for value in [record.get("credits_used")]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    tokens = [
        int(value)
        for record in records
        for value in [record.get("tokens_used")]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    model_calls = [
        int(value)
        for record in records
        for value in [record.get("model_calls")]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    wall_times = [
        value
        for record in records
        for value in [_as_finite_float(record.get("wall_time_s"))]
        if value is not None and value >= 0
    ]
    return {
        "runs": runs,
        "e2e_eligible": len(e2e_rows),
        "e2e_unknown": runs - len(e2e_rows),
        "e2e_successes": e2e,
        "e2e_success_rate": e2e / len(e2e_rows) if e2e_rows else None,
        "final_validation_eligible": len(final_rows),
        "final_validation_unknown": runs - len(final_rows),
        "final_validation_successes": final,
        "final_validation_success_rate": (
            final / len(final_rows) if final_rows else None
        ),
        "fresh_final_eligible": len(fresh_rows),
        "fresh_final_unknown": runs - len(fresh_rows),
        "fresh_final_successes": fresh,
        "fresh_final_success_rate": fresh / len(fresh_rows) if fresh_rows else None,
        "router": {
            "eligible": len(router_rows),
            "correct": router_correct,
            "accuracy": router_correct / len(router_rows) if router_rows else None,
            "unknown": runs - len(router_rows),
        },
        "acceleration_vs_baseline": _metric_stats(accelerations),
        "usage": {
            "credits": _metric_stats(credits),
            "tokens": _metric_stats(tokens),
            "model_calls": _metric_stats(model_calls),
            "unknown": {
                "credits": runs - len(credits),
                "tokens": runs - len(tokens),
                "model_calls": runs - len(model_calls),
            },
            "wall_time_s": _metric_stats(wall_times),
            "tool_calls": dict(sorted(tool_calls.items())),
        },
        "patch": {
            "candidates": patch_candidates,
            "rejections": rejection_count,
            "rejection_rate": (
                rejection_count / patch_candidates if patch_candidates else None
            ),
            "reasons": dict(sorted(rejection_reasons.items())),
        },
        "failures": {
            "count": len(failures),
            "by_stage": dict(sorted(failure_stages.items())),
            "by_reason": dict(sorted(failure_reasons.items())),
        },
    }


def _population_views(
    records: Sequence[Mapping[str, object]],
    selected_models: Sequence[str],
) -> dict[str, object]:
    rows = list(records)
    modes = sorted(_MODES)
    evidence = {
        evidence_class.value: _aggregate(
            [
                record
                for record in rows
                if record.get("evidence_class") == evidence_class.value
            ]
        )
        for evidence_class in EvidenceClass
    }
    real_rows = [
        record
        for record in rows
        if record.get("evidence_class") == EvidenceClass.REAL.value
        and record.get("real_evidence_eligible") is True
    ]
    expected_modes = {
        mode: _aggregate(
            [record for record in rows if record.get("expected_mode") == mode]
        )
        for mode in modes
    }
    routed_modes = {
        mode: _aggregate(
            [record for record in rows if record.get("routed_mode") == mode]
        )
        for mode in modes
    }
    by_model = {
        model: _aggregate(
            [record for record in rows if record.get("model") == model]
        )
        for model in selected_models
    }
    expected_mode_by_evidence = {
        evidence_class.value: {
            mode: _aggregate(
                [
                    record
                    for record in rows
                    if record.get("evidence_class") == evidence_class.value
                    and record.get("expected_mode") == mode
                ]
            )
            for mode in modes
        }
        for evidence_class in EvidenceClass
    }
    model_by_evidence = {
        evidence_class.value: {
            model: _aggregate(
                [
                    record
                    for record in rows
                    if record.get("evidence_class") == evidence_class.value
                    and record.get("model") == model
                ]
            )
            for model in selected_models
        }
        for evidence_class in EvidenceClass
    }
    return {
        "records": len(rows),
        "overall_all_evidence": _aggregate(rows),
        "real_evidence_headline": _aggregate(real_rows),
        "by_evidence_class": evidence,
        "by_expected_mode": expected_modes,
        "by_routed_mode": routed_modes,
        "by_expected_mode_by_evidence": expected_mode_by_evidence,
        "by_model": by_model,
        "by_model_by_evidence": model_by_evidence,
        "run_ids": [str(record.get("run_id")) for record in rows],
    }


def build_summary(
    *,
    config: BenchmarkConfig,
    executor_fingerprint: str,
    descriptors_found: int,
    selected_tasks: Sequence[TaskDescriptor],
    selected_models: Sequence[str],
    planned_runs: int,
    records: Sequence[Mapping[str, object]],
    new_runs: int,
    resumed_runs: int,
    stopped_reason: str | None,
    batch_elapsed_s: float,
    all_attempt_records: Sequence[Mapping[str, object]] | None = None,
    latest_slot_records: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    latest_rows = list(
        records if latest_slot_records is None else latest_slot_records
    )
    attempt_rows = list(
        records if all_attempt_records is None else all_attempt_records
    )
    all_views = _population_views(attempt_rows, selected_models)
    latest_views = _population_views(latest_rows, selected_models)
    fingerprint = _sha256_json(
        {
            "schema": SUMMARY_SCHEMA,
            "runner": RUNNER_FINGERPRINT,
            "implementation_fingerprint": _implementation_fingerprint(),
            "executor_fingerprint": executor_fingerprint,
            "execution_policy_fingerprint": _execution_policy_fingerprint(config),
            "backend": config.backend,
            "models": list(selected_models),
            "repeats": config.repeats,
            "tasks": [
                {
                    "task_id": item.task_id,
                    "task_fingerprint": item.task_fingerprint,
                    "split": item.split,
                    "algorithm_family": item.algorithm_family,
                    "expected_mode": item.expected_mode,
                }
                for item in selected_tasks
            ],
        }
    )
    return {
        "schema_version": SUMMARY_SCHEMA,
        "runner_fingerprint": RUNNER_FINGERPRINT,
        "implementation_fingerprint": _implementation_fingerprint(),
        "implementation_facts": _implementation_facts(),
        "execution_policy_fingerprint": _execution_policy_fingerprint(config),
        "benchmark_fingerprint": fingerprint,
        "generated_at": _utc_now(),
        "configuration": config.public_dict(),
        "selection": {
            "descriptors_found": descriptors_found,
            "tasks_selected": len(selected_tasks),
            "task_ids": [item.task_id for item in selected_tasks],
            "algorithm_families": {
                item.task_id: item.algorithm_family for item in selected_tasks
            },
            "models_selected": list(selected_models),
            "planned_runs": planned_runs,
        },
        "execution": {
            # ``records`` remains the number of current plan slots for old
            # consumers.  Attempt history is explicitly separate and is the
            # denominator for scientific headline metrics.
            "records": len(latest_rows),
            "attempt_records": len(attempt_rows),
            "latest_slot_records": len(latest_rows),
            "completed_latest_slots": len(latest_rows),
            "pending_latest_slots": max(0, planned_runs - len(latest_rows)),
            "latest_slot_completion_rate": (
                len(latest_rows) / planned_runs if planned_runs else None
            ),
            "new_runs": new_runs,
            "resumed_runs": resumed_runs,
            "stopped_reason": stopped_reason,
            "batch_elapsed_s": batch_elapsed_s,
            "max_parallel_runs": 1,
            "vitis_serialized": True,
        },
        "evidence_policy": {
            "demo": "orchestration only; excluded from real headline",
            "deterministic": "fixture/test only; excluded from real headline",
            "real": "structured Vitis terminal evidence only",
            "mixed_populations_forbidden": True,
            "headline_population": "all_attempts_for_current_plan_fingerprints",
            "completion_population": "latest_slot",
        },
        "populations": {
            "all_attempts": {
                "definition": (
                    "Every durable attempt whose run_fingerprint belongs to "
                    "the current plan; retries do not erase failures."
                ),
                **all_views,
            },
            "latest_slots": {
                "definition": (
                    "The latest validated record for each completed current-plan "
                    "slot; used only for resume completion."
                ),
                **latest_views,
            },
        },
        "all_attempt_population": all_views["overall_all_evidence"],
        "latest_slot_population": latest_views["overall_all_evidence"],
        "latest_real_evidence": latest_views["real_evidence_headline"],
        # Compatibility headlines deliberately point at all attempts, so a
        # retry cannot make an earlier failed execution disappear.
        "overall_all_evidence": all_views["overall_all_evidence"],
        "real_evidence_headline": all_views["real_evidence_headline"],
        "by_evidence_class": all_views["by_evidence_class"],
        # ``by_mode`` remains as a compatibility alias, but now measures the
        # scheduled/expected task category.  Misrouting is reported separately
        # instead of moving a failure into the wrong category.
        "by_mode": all_views["by_expected_mode"],
        "by_expected_mode": all_views["by_expected_mode"],
        "by_routed_mode": all_views["by_routed_mode"],
        "by_expected_mode_by_evidence": all_views[
            "by_expected_mode_by_evidence"
        ],
        "by_model": all_views["by_model"],
        "by_model_by_evidence": all_views["by_model_by_evidence"],
        "run_ids": all_views["run_ids"],
        "latest_run_ids": latest_views["run_ids"],
    }


_CSV_FIELDS = (
    "schema_version",
    "run_id",
    "run_fingerprint",
    "task_id",
    "split",
    "task_type",
    "difficulty",
    "algorithm_family",
    "expected_mode",
    "routed_mode",
    "router_correct",
    "model",
    "repeat_index",
    "backend",
    "evidence_class",
    "evidence_level",
    "real_evidence_eligible",
    "status",
    "e2e_success",
    "final_validation_success",
    "fresh_final_success",
    "acceleration_vs_baseline",
    "credits_used",
    "tokens_used",
    "model_calls",
    "csim_calls",
    "synth_calls",
    "cosim_calls",
    "wall_time_s",
    "patch_candidates",
    "patch_rejections",
    "failure_stage",
    "stop_reason",
    "resumed",
    "run_dir",
    "result_ref",
    "result_sha256",
    "source_result_ref",
    "source_result_sha256",
    "orchestration_receipt_ref",
)


def write_summary_csv(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            calls = _mapping(record.get("tool_calls"))
            row.update(
                {
                    "csim_calls": _as_nonnegative_int(calls.get("csim")),
                    "synth_calls": _as_nonnegative_int(calls.get("synth")),
                    "cosim_calls": _as_nonnegative_int(calls.get("cosim")),
                }
            )
            writer.writerow(row)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _percent(value: object) -> str:
    parsed = _as_finite_float(value)
    return "-" if parsed is None else f"{100.0 * parsed:.1f}%"


def render_report(summary: Mapping[str, object]) -> str:
    selection = _mapping(summary.get("selection"))
    execution = _mapping(summary.get("execution"))
    real = _mapping(summary.get("real_evidence_headline"))
    latest = _mapping(summary.get("latest_slot_population"))
    latest_real = _mapping(summary.get("latest_real_evidence"))
    overall = _mapping(summary.get("overall_all_evidence"))
    by_evidence = _mapping(summary.get("by_evidence_class"))
    by_mode = _mapping(summary.get("by_expected_mode", summary.get("by_mode")))
    by_mode_by_evidence = _mapping(summary.get("by_expected_mode_by_evidence"))
    real_modes = _mapping(by_mode_by_evidence.get("REAL"))
    lines = [
        "# V3-D Batch Benchmark Report",
        "",
        f"- Benchmark fingerprint: `{summary.get('benchmark_fingerprint')}`",
        f"- Selected tasks / planned runs: `{selection.get('tasks_selected')} / {selection.get('planned_runs')}`",
        f"- Latest slots / all attempts / new / resumed: `"
        f"{execution.get('latest_slot_records')} / {execution.get('attempt_records')} / "
        f"{execution.get('new_runs')} / {execution.get('resumed_runs')}`",
        f"- Stop reason: `{execution.get('stopped_reason') or 'COMPLETED'}`",
        "",
        "> Evidence rule: DEMO and DETERMINISTIC rows are fixture evidence only. "
        "They are never included in the real-evidence headline below.",
        "",
        "## Real Vitis evidence headline (all current-plan attempts)",
        "",
        "> Retries remain in this population, so a later success cannot hide an "
        "earlier failed attempt. Latest-slot rows are used only for completion.",
        "",
        "| Runs | E2E success | Fresh final | Router accuracy |",
        "|---:|---:|---:|---:|",
        f"| {real.get('runs', 0)} | {_percent(real.get('e2e_success_rate'))} | "
        f"{_percent(real.get('fresh_final_success_rate'))} | "
        f"{_percent(_mapping(real.get('router')).get('accuracy'))} |",
        "",
        "## Latest-slot completion population",
        "",
        "| Slots | E2E | Fresh final | Real slots | Real E2E |",
        "|---:|---:|---:|---:|---:|",
        f"| {latest.get('runs', 0)} | {_percent(latest.get('e2e_success_rate'))} | "
        f"{_percent(latest.get('fresh_final_success_rate'))} | "
        f"{latest_real.get('runs', 0)} | "
        f"{_percent(latest_real.get('e2e_success_rate'))} |",
        "",
        "## Evidence populations",
        "",
        "| Evidence | Runs | E2E | Fresh final | Credits | Tokens | Calls C/S/Co/L | Time (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("REAL", "DETERMINISTIC", "DEMO"):
        block = _mapping(by_evidence.get(name))
        usage = _mapping(block.get("usage"))
        credits = _mapping(usage.get("credits"))
        tokens = _mapping(usage.get("tokens"))
        calls = _mapping(usage.get("tool_calls"))
        wall = _mapping(usage.get("wall_time_s"))
        lines.append(
            f"| {name} | {block.get('runs', 0)} | {_percent(block.get('e2e_success_rate'))} | "
            f"{_percent(block.get('fresh_final_success_rate'))} | {credits.get('total', 0)} | "
            f"{tokens.get('total', 0)} | "
            f"{_as_nonnegative_int(calls.get('csim'))}/"
            f"{_as_nonnegative_int(calls.get('synth'))}/"
            f"{_as_nonnegative_int(calls.get('cosim'))}/"
            f"{_as_nonnegative_int(calls.get('llm'))} | "
            f"{wall.get('total', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Real expected-mode results",
            "",
            "| Expected mode | Runs | Success | Success rate | Fresh final |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for mode in sorted(by_mode):
        block = _mapping(real_modes.get(mode))
        lines.append(
            f"| {mode} | {block.get('runs', 0)} | "
            f"{block.get('e2e_successes', 0)} | "
            f"{_percent(block.get('e2e_success_rate'))} | "
            f"{_percent(block.get('fresh_final_success_rate'))} |"
        )
    lines.extend(
        [
            "",
            "## Expected-mode diagnostics (all evidence populations)",
            "",
            "> This table can mix fixture populations. It is diagnostic only; "
            "the REAL table above is the formal result.",
            "",
            "| Mode | Runs | Success | Success rate | Patch rejections | Failure stages |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for mode in sorted(by_mode):
        block = _mapping(by_mode.get(mode))
        patch = _mapping(block.get("patch"))
        failures = _mapping(block.get("failures"))
        lines.append(
            f"| {mode} | {block.get('runs', 0)} | {block.get('e2e_successes', 0)} | "
            f"{_percent(block.get('e2e_success_rate'))} | {patch.get('rejections', 0)} | "
            f"`{_canonical_json(failures.get('by_stage', {}))}` |"
        )
    overall_acceleration = _mapping(overall.get("acceleration_vs_baseline"))
    overall_patch = _mapping(overall.get("patch"))
    overall_failures = _mapping(overall.get("failures"))
    lines.extend(
        [
            "",
            "## Cross-run diagnostics (all evidence, kept separate above)",
            "",
            f"- Acceleration mean / median / count: `{overall_acceleration.get('mean')} / {overall_acceleration.get('median')} / {overall_acceleration.get('count')}`",
            f"- Patch rejection rate: `{_percent(overall_patch.get('rejection_rate'))}`",
            f"- Patch rejection reasons: `{_canonical_json(overall_patch.get('reasons', {}))}`",
            f"- Failure stages: `{_canonical_json(overall_failures.get('by_stage', {}))}`",
            "",
            "## Run IDs",
            "",
            *[f"- `{run_id}`" for run_id in summary.get("run_ids", [])],
            "",
        ]
    )
    return "\n".join(lines)


@dataclass
class BenchmarkOutcome:
    summary: dict[str, object]
    records: list[dict[str, object]]


class BatchBenchmarkRunner:
    """Sequential, failure-isolating and resumable benchmark orchestrator."""

    def __init__(
        self,
        config: BenchmarkConfig,
        executor: BenchmarkExecutor | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] = _utc_now,
    ) -> None:
        self.config = config
        self.config.verify_full_agent_manifest()
        self.executor = executor or default_executor(
            config.backend,
            validation_profile=config.validation_profile,
            final_validation_policy=config.final_validation_policy,
            evidence_memory_mode=config.evidence_memory_mode,
            max_planner_rounds=config.max_planner_rounds,
            max_no_improvement_rounds=config.max_no_improvement_rounds,
            enable_final_fallback=config.enable_final_fallback,
            continuation_policy_mode=config.continuation_policy_mode,
            continuation_policy_version=config.continuation_policy_version,
            continuation_admission_manifest=(
                config.continuation_admission_manifest
            ),
            experience_mode=config.experience_mode,
            experience_store=config.experience_store,
            experience_ranker_version=config.experience_ranker_version,
            experience_admission_manifest=config.experience_admission_manifest,
            experience_task_split=config.experience_task_split,
        )
        if config.full_agent_manifest is not None and (
            type(self.executor) is not V3PrototypeCLIExecutor
            or self.executor.experimental_token_policy
        ):
            raise BenchmarkError(
                "Full Agent manifest requires the formal fixed-token "
                "V3PrototypeCLIExecutor"
            )
        if type(self.executor) is V3PrototypeCLIExecutor:
            executor_policy = (
                self.executor.validation_profile,
                self.executor.final_validation_policy,
                self.executor.evidence_memory_mode,
                self.executor.max_planner_rounds,
                self.executor.max_no_improvement_rounds,
                self.executor.enable_final_fallback,
                self.executor.continuation_policy_mode,
                self.executor.continuation_policy_version,
                self.executor.experience_mode,
                self.executor.experience_ranker_version,
                self.executor.experience_task_split,
            )
            config_policy = (
                config.validation_profile,
                config.final_validation_policy,
                config.evidence_memory_mode,
                config.max_planner_rounds,
                config.max_no_improvement_rounds,
                config.enable_final_fallback,
                config.continuation_policy_mode,
                config.continuation_policy_version,
                config.experience_mode,
                config.experience_ranker_version,
                config.experience_task_split,
            )
            if executor_policy != config_policy:
                raise BenchmarkError(
                    "real executor experience/validation policy conflicts with "
                    "BenchmarkConfig"
                )
            configured_store_snapshot = {
                "role": "READ_ONLY_SEED",
                "present": config.experience_store is not None,
                "size_bytes": config.experience_store_size_bytes,
                "sha256": config.experience_store_sha256,
            }
            if (
                self.executor.experience_store_snapshot
                != configured_store_snapshot
            ):
                raise BenchmarkError(
                    "experience_store changed after the BenchmarkConfig "
                    "snapshot was frozen"
                )
            configured_continuation_admission_snapshot = {
                "role": "CONTINUATION_ENFORCE_ADMISSION",
                "present": config.continuation_admission_manifest is not None,
                "size_bytes": config.continuation_admission_size_bytes,
                "sha256": config.continuation_admission_sha256,
            }
            if (
                self.executor.continuation_admission_snapshot
                != configured_continuation_admission_snapshot
            ):
                raise BenchmarkError(
                    "continuation_admission_manifest changed after the "
                    "BenchmarkConfig snapshot was frozen"
                )
            configured_experience_admission_snapshot = {
                "role": "EXPERIENCE_GUIDED_ADMISSION",
                "present": config.experience_admission_manifest is not None,
                "size_bytes": config.experience_admission_size_bytes,
                "sha256": config.experience_admission_sha256,
            }
            if (
                self.executor.experience_admission_snapshot
                != configured_experience_admission_snapshot
            ):
                raise BenchmarkError(
                    "experience_admission_manifest changed after the "
                    "BenchmarkConfig snapshot was frozen"
                )
        self.monotonic = monotonic
        self.utc_now = utc_now
        self.executor_fingerprint = _executor_fingerprint(self.executor)
        self.execution_policy_fingerprint = _execution_policy_fingerprint(config)
        self.evidence_class = _executor_evidence_class(
            self.executor, self.config.backend
        )
        self.real_evidence_authorized = (
            type(self.executor) is V3PrototypeCLIExecutor
            and self.config.backend == "vitis"
            and self.evidence_class is EvidenceClass.REAL
            and not self.executor.experimental_token_policy
            and getattr(self.executor, "real_evidence_authority", None)
            == REAL_EVIDENCE_AUTHORITY
        )

    def _plan(
        self, descriptors: Sequence[TaskDescriptor], models: Sequence[str]
    ) -> list[tuple[TaskDescriptor, str, int, str, str]]:
        plan: list[tuple[TaskDescriptor, str, int, str, str]] = []
        for descriptor in descriptors:
            for model in models:
                for repeat_index in range(1, self.config.repeats + 1):
                    fingerprint = _run_fingerprint(
                        descriptor,
                        model=model,
                        repeat_index=repeat_index,
                        backend=self.config.backend,
                        executor_fingerprint=self.executor_fingerprint,
                        execution_policy_fingerprint=(
                            self.execution_policy_fingerprint
                        ),
                    )
                    run_id = _make_run_id(
                        descriptor, model, repeat_index, fingerprint
                    )
                    plan.append(
                        (descriptor, model, repeat_index, fingerprint, run_id)
                    )
        return plan

    def _verify_executor_identity(self) -> None:
        self.config.verify_full_agent_manifest()
        if _executor_fingerprint(self.executor) != self.executor_fingerprint:
            raise BenchmarkError(
                "benchmark executor changed after its identity was frozen"
            )
        if (
            self.real_evidence_authorized
            and getattr(self.executor, "experimental_token_policy", False)
        ):
            raise BenchmarkError(
                "experimental token policy cannot use formal REAL authority"
            )

    def run(self) -> BenchmarkOutcome:
        self._verify_executor_identity()
        output_dir = self.config.output_dir
        results_path = output_dir / "benchmark_results.jsonl"
        if results_path.exists() and results_path.stat().st_size and not self.config.resume:
            raise BenchmarkError(
                "output directory already contains benchmark results; use --resume "
                "or choose a new --output-dir"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(output_dir / ".benchmark.lock"):
            if (
                results_path.exists()
                and results_path.stat().st_size
                and not self.config.resume
            ):
                raise BenchmarkError(
                    "output directory acquired by another completed runner; "
                    "use --resume or choose a new --output-dir"
                )
            return self._run_locked(results_path)

    def _run_locked(self, results_path: Path) -> BenchmarkOutcome:
        start = self.monotonic()
        descriptors = discover_tasks(self.config.corpus)
        selected = select_tasks(descriptors, self.config)
        models = select_models(self.config)
        plan = self._plan(selected, models)
        plan_payload = {
            "schema_version": PLAN_SCHEMA,
            "runner_fingerprint": RUNNER_FINGERPRINT,
            "implementation_fingerprint": _implementation_fingerprint(),
            "implementation_facts": _implementation_facts(),
            "executor_fingerprint": self.executor_fingerprint,
            "execution_policy_fingerprint": self.execution_policy_fingerprint,
            "configuration": self.config.public_dict(),
            "runs": [
                {
                    "task_id": descriptor.task_id,
                    "task_fingerprint": descriptor.task_fingerprint,
                    "algorithm_family": descriptor.algorithm_family,
                    "model": model,
                    "repeat_index": repeat_index,
                    "run_fingerprint": fingerprint,
                    "run_id": run_id,
                }
                for descriptor, model, repeat_index, fingerprint, run_id in plan
            ],
        }
        _atomic_json(self.config.output_dir / "benchmark_plan.json", plan_payload)

        previous = _load_previous_records(results_path) if self.config.resume else []
        latest: dict[str, dict[str, object]] = {}
        for record in previous:
            fingerprint = record.get("run_fingerprint")
            if isinstance(fingerprint, str):
                latest[fingerprint] = record

        records: list[dict[str, object]] = []
        new_attempt_records: list[dict[str, object]] = []
        new_runs = 0
        resumed_runs = 0
        invalidated_attempt_run_ids: set[str] = set()
        validated_resume_run_ids: set[str] = set()
        stopped_reason: str | None = None
        for descriptor, model, repeat_index, fingerprint, run_id in plan:
            elapsed = self.monotonic() - start
            if (
                self.config.max_runtime_seconds is not None
                and elapsed >= self.config.max_runtime_seconds
            ):
                stopped_reason = "MAX_RUNTIME_REACHED"
                break
            prior = latest.get(fingerprint)
            invalid_resume_record = False
            retrying_prior_failure = bool(
                prior is not None
                and self.config.retry_failures
                and prior.get("e2e_success") is not True
            )
            if prior is not None:
                resume_spec: BenchmarkRunSpec | None = None
                prior_run_dir = prior.get("run_dir")
                prior_run_id = prior.get("run_id")
                if (
                    descriptor.task is not None
                    and isinstance(prior_run_dir, str)
                    and isinstance(prior_run_id, str)
                ):
                    resume_spec = BenchmarkRunSpec(
                        task=descriptor.task,
                        descriptor=descriptor,
                        model=model,
                        repeat_index=repeat_index,
                        backend=self.config.backend,
                        run_id=prior_run_id,
                        run_fingerprint=fingerprint,
                        run_dir=Path(prior_run_dir).resolve(),
                    )
                resumed = _resume_candidate(
                    self.config.output_dir,
                    prior,
                    spec=resume_spec,
                    executor=self.executor,
                )
                if resumed is not None and isinstance(prior.get("run_id"), str):
                    validated_resume_run_ids.add(str(prior["run_id"]))
                if resumed is not None and not retrying_prior_failure:
                    records.append(resumed)
                    resumed_runs += 1
                    continue
                if resumed is None:
                    # The JSONL row and its complete durable provenance no
                    # longer agree. Never trust or overwrite that directory,
                    # but allow a clean independent recovery run.
                    invalid_resume_record = True
                    if isinstance(prior.get("run_id"), str):
                        invalidated_attempt_run_ids.add(str(prior["run_id"]))

            effective_run_id = run_id
            if (
                prior is not None
                and self.config.retry_failures
                and prior.get("e2e_success") is not True
            ):
                prior_attempts = sum(
                    record.get("run_fingerprint") == fingerprint
                    for record in previous
                )
                effective_run_id = f"{run_id}--retry{prior_attempts:03d}"
            run_dir = self.config.output_dir / "runs" / effective_run_id
            if invalid_resume_record:
                recovery_index = 1
                original_run_id = effective_run_id
                while True:
                    candidate_run_id = (
                        f"{original_run_id}--recovery{recovery_index:03d}"
                    )
                    candidate_dir = self.config.output_dir / "runs" / candidate_run_id
                    if not candidate_dir.exists():
                        effective_run_id = candidate_run_id
                        run_dir = candidate_dir
                        break
                    recovery_index += 1
            preexisting_run_detail: str | None = None
            if run_dir.exists() and any(run_dir.iterdir()):
                preexisting_run_detail = (
                    "scheduled run directory already existed and was non-empty; "
                    "stale artifacts were not read or overwritten"
                )
                blocked_index = 1
                original_run_id = effective_run_id
                while True:
                    candidate_run_id = (
                        f"{original_run_id}--blocked{blocked_index:03d}"
                    )
                    candidate_dir = self.config.output_dir / "runs" / candidate_run_id
                    if not candidate_dir.exists():
                        effective_run_id = candidate_run_id
                        run_dir = candidate_dir
                        break
                    blocked_index += 1
            run_dir.mkdir(parents=True, exist_ok=True)
            started_at = self.utc_now()
            run_start = self.monotonic()
            execution_started = False
            if preexisting_run_detail is not None:
                record = _failure_record(
                    descriptor,
                    model=model,
                    repeat_index=repeat_index,
                    backend=self.config.backend,
                    evidence_class=self.evidence_class,
                    executor_fingerprint=self.executor_fingerprint,
                    run_id=effective_run_id,
                    run_fingerprint=fingerprint,
                    run_dir=run_dir,
                    started_at=started_at,
                    finished_at=self.utc_now(),
                    wall_time_s=max(0.0, self.monotonic() - run_start),
                    error_type="PreexistingRunDirectory",
                    detail=preexisting_run_detail,
                    execution_started=False,
                )
            elif not descriptor.loadable:
                record = _failure_record(
                    descriptor,
                    model=model,
                    repeat_index=repeat_index,
                    backend=self.config.backend,
                    evidence_class=self.evidence_class,
                    executor_fingerprint=self.executor_fingerprint,
                    run_id=effective_run_id,
                    run_fingerprint=fingerprint,
                    run_dir=run_dir,
                    started_at=started_at,
                    finished_at=self.utc_now(),
                    wall_time_s=max(0.0, self.monotonic() - run_start),
                    error_type=descriptor.load_error_type or "TaskPackageError",
                    detail=descriptor.load_error_detail or "task package did not load",
                    execution_started=False,
                )
            else:
                assert descriptor.task is not None
                spec = BenchmarkRunSpec(
                    task=descriptor.task,
                    descriptor=descriptor,
                    model=model,
                    repeat_index=repeat_index,
                    backend=self.config.backend,
                    run_id=effective_run_id,
                    run_fingerprint=fingerprint,
                    run_dir=run_dir,
                )
                def remaining_runtime() -> float | None:
                    return (
                        None
                        if self.config.max_runtime_seconds is None
                        else max(
                            0.0,
                            self.config.max_runtime_seconds
                            - (self.monotonic() - start),
                        )
                    )
                try:
                    self._verify_executor_identity()
                    execution_started = True
                    if (
                        self.config.backend == "vitis"
                        or getattr(self.executor, "requires_vitis_lock", False)
                    ):
                        with _exclusive_lock(
                            Path("/tmp/llm4hls-v3d-vitis-serial.lock")
                        ):
                            raw_result = self.executor.execute(
                                spec,
                                timeout_seconds=remaining_runtime(),
                            )
                    else:
                        raw_result = self.executor.execute(
                            spec,
                            timeout_seconds=remaining_runtime(),
                        )
                    if not isinstance(raw_result, Mapping):
                        raise BenchmarkExecutionError(
                            "executor must return a mapping terminal result"
                        )
                    provenance_receipt: Mapping[str, object] | None = None
                    if self.real_evidence_authorized:
                        assert type(self.executor) is V3PrototypeCLIExecutor
                        provenance_receipt = (
                            self.executor._validate_terminal_provenance(
                                spec, raw_result
                            )
                        )
                    record = _normalise_result(
                        spec,
                        raw_result,
                        evidence_class=self.evidence_class,
                        real_evidence_authorized=self.real_evidence_authorized,
                        executor_fingerprint=self.executor_fingerprint,
                        provenance_receipt=provenance_receipt,
                        started_at=started_at,
                        finished_at=self.utc_now(),
                        wall_time_s=max(0.0, self.monotonic() - run_start),
                    )
                except Exception as exc:
                    partial_report = getattr(exc, "partial_report", None)
                    process_cleanup = getattr(exc, "process_cleanup", None)
                    record = _failure_record(
                        descriptor,
                        model=model,
                        repeat_index=repeat_index,
                        backend=self.config.backend,
                        evidence_class=self.evidence_class,
                        executor_fingerprint=self.executor_fingerprint,
                        run_id=effective_run_id,
                        run_fingerprint=fingerprint,
                        run_dir=run_dir,
                        started_at=started_at,
                        finished_at=self.utc_now(),
                        wall_time_s=max(0.0, self.monotonic() - run_start),
                        error_type=type(exc).__name__,
                        detail=str(exc),
                        execution_started=execution_started,
                        partial_report=(
                            partial_report
                            if isinstance(partial_report, Mapping)
                            else None
                        ),
                        process_cleanup=(
                            process_cleanup
                            if isinstance(process_cleanup, Mapping)
                            else None
                        ),
                    )
            _atomic_json(run_dir / "benchmark_run.json", record)
            if record.get("status") in {"ERROR", "EXECUTOR_TIMEOUT"}:
                _atomic_json(run_dir / "failure.json", record.get("error", {}))
            _append_jsonl(results_path, record)
            records.append(record)
            new_attempt_records.append(record)
            new_runs += 1

        elapsed = max(0.0, self.monotonic() - start)
        current_fingerprints = {fingerprint for _, _, _, fingerprint, _ in plan}
        plan_by_fingerprint = {
            fingerprint: (descriptor, model, repeat_index)
            for descriptor, model, repeat_index, fingerprint, _ in plan
        }
        # The headline retains every attempt, not just the latest slot.  Audit
        # every historical REAL attempt before allowing it back into that
        # population; otherwise tampering with an older retry could survive a
        # resume merely because a newer slot record exists.
        if self.config.resume and self.evidence_class is EvidenceClass.REAL:
            for previous_record in previous:
                fingerprint_value = previous_record.get("run_fingerprint")
                planned = plan_by_fingerprint.get(str(fingerprint_value))
                previous_run_id = previous_record.get("run_id")
                previous_run_dir = previous_record.get("run_dir")
                if (
                    planned is None
                    or not isinstance(previous_run_id, str)
                    or not isinstance(previous_run_dir, str)
                    or previous_run_id in invalidated_attempt_run_ids
                    or previous_run_id in validated_resume_run_ids
                ):
                    continue
                planned_descriptor, planned_model, planned_repeat = planned
                if planned_descriptor.task is None:
                    invalidated_attempt_run_ids.add(previous_run_id)
                    continue
                audit_spec = BenchmarkRunSpec(
                    task=planned_descriptor.task,
                    descriptor=planned_descriptor,
                    model=planned_model,
                    repeat_index=planned_repeat,
                    backend=self.config.backend,
                    run_id=previous_run_id,
                    run_fingerprint=str(fingerprint_value),
                    run_dir=Path(previous_run_dir).resolve(),
                )
                if (
                    _resume_candidate(
                        self.config.output_dir,
                        previous_record,
                        spec=audit_spec,
                        executor=self.executor,
                    )
                    is None
                ):
                    invalidated_attempt_run_ids.add(previous_run_id)
        all_attempt_records: list[dict[str, object]] = []
        for previous_record in previous:
            if previous_record.get("run_fingerprint") not in current_fingerprints:
                continue
            attempt = dict(previous_record)
            if attempt.get("run_id") in invalidated_attempt_run_ids:
                attempt.update(
                    {
                        "status": "ERROR",
                        "e2e_success": False,
                        "final_validation_success": False,
                        "fresh_final_success": False,
                        "acceleration_vs_baseline": None,
                        "failure_stage": "PROVENANCE_RESUME",
                        "stop_reason": "DURABLE_PROVENANCE_INVALIDATED",
                        "resume_provenance_valid": False,
                    }
                )
            all_attempt_records.append(attempt)
        all_attempt_records.extend(new_attempt_records)
        summary = build_summary(
            config=self.config,
            executor_fingerprint=self.executor_fingerprint,
            descriptors_found=len(descriptors),
            selected_tasks=selected,
            selected_models=models,
            planned_runs=len(plan),
            records=records,
            new_runs=new_runs,
            resumed_runs=resumed_runs,
            stopped_reason=stopped_reason,
            batch_elapsed_s=elapsed,
            all_attempt_records=all_attempt_records,
            latest_slot_records=records,
        )
        # Keep the short V3-D prototype names for existing consumers while
        # also publishing the exact filenames promised by the P5 benchmark
        # contract.  Both names are generated from the same in-memory data so
        # an alias can never describe a different population of runs.
        _atomic_json(self.config.output_dir / "summary.json", summary)
        _atomic_json(self.config.output_dir / "benchmark_summary.json", summary)
        write_summary_csv(
            self.config.output_dir / "summary.csv", all_attempt_records
        )
        write_summary_csv(
            self.config.output_dir / "benchmark_summary.csv", all_attempt_records
        )
        report = render_report(summary)
        _atomic_text(self.config.output_dir / "report.md", report)
        _atomic_text(self.config.output_dir / "benchmark_report.md", report)
        return BenchmarkOutcome(summary=summary, records=records)


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
        prog="python -m llm4hls_agent.v3_batch_benchmark",
        description=(
            "Run a resumable V3-D corpus benchmark. Demo and deterministic "
            "backends are fixture evidence and never count as real Vitis E2E."
        ),
    )
    parser.add_argument(
        "--corpus",
        action="append",
        required=True,
        help="Corpus root or direct task.toml; repeat for multiple roots.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--split",
        action="append",
        default=[],
        help="Split name(s), comma-separated or repeated; default is all.",
    )
    parser.add_argument(
        "--models",
        action="extend",
        nargs="+",
        default=[],
        help="Model name(s), space/comma-separated or repeated.",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--backend",
        choices=("demo", "deterministic", "vitis"),
        default="deterministic",
    )
    parser.add_argument(
        "--validation-profile",
        choices=("strict", "fast-experiment"),
        default="strict",
        help="Validation policy forwarded to each V3 single-task run.",
    )
    parser.add_argument(
        "--final-validation-policy",
        choices=("task_contract", "full_internal_audit"),
        default="task_contract",
        help=(
            "Agent-search closeout policy forwarded to each V3 run; independent "
            "certification remains a separate post-search domain."
        ),
    )
    parser.add_argument(
        "--evidence-memory",
        choices=("off", "on"),
        default="on",
        help=(
            "A1 Structured Evidence Memory mode forwarded to each V3 run. "
            "A1 off requires Continuation off."
        ),
    )
    parser.add_argument(
        "--max-planner-rounds",
        type=int,
        help=(
            "Explicit live Planner call limit. Omit to preserve the single-task "
            "CLI profile default."
        ),
    )
    parser.add_argument(
        "--max-no-improvement-rounds",
        type=int,
        default=2,
        help="Stop after this many consecutive non-improving rounds.",
    )
    parser.add_argument(
        "--enable-final-fallback",
        action="store_true",
        help="Allow the bounded Agent-search closeout fallback.",
    )
    parser.add_argument(
        "--continuation-policy",
        choices=("off", "shadow", "enforce"),
        default="shadow",
        help="Continuation control mode forwarded to each V3 single-task run.",
    )
    parser.add_argument(
        "--continuation-policy-version",
        choices=("v2",),
        default="v2",
        help="Continuation implementation forwarded to each run.",
    )
    parser.add_argument(
        "--continuation-admission-manifest",
        help=(
            "Passing, immutable, implementation-version-matched Continuation "
            "admission manifest. Required for enforce and content-bound into "
            "batch/run identity."
        ),
    )
    parser.add_argument(
        "--full-agent-manifest",
        help=(
            "READY Track A Full Agent manifest. It never overrides arguments: "
            "backend=vitis, A1=on, A2=enforce/v2 with matching Admission, and "
            "A3=guided/ranker-v3 with matching Store/Admission must all be "
            "provided explicitly or the batch fails closed."
        ),
    )
    parser.add_argument(
        "--experience-mode",
        choices=("off", "shadow", "guided"),
        default="shadow",
        help="Experience policy forwarded to each V3 single-task run.",
    )
    parser.add_argument(
        "--experience-store",
        help=(
            "Existing frozen experience-store file; its content digest is "
            "bound into batch and run identities."
        ),
    )
    parser.add_argument(
        "--experience-ranker-version",
        choices=("v1", "v3"),
        default="v1",
        help="Experience ranker implementation forwarded to each run.",
    )
    parser.add_argument(
        "--experience-admission-manifest",
        help=(
            "Passing, immutable Ranker V3 admission manifest. Required for "
            "guided and content-bound into batch/run identity."
        ),
    )
    parser.add_argument(
        "--experience-task-split",
        choices=("train", "dev", "hidden_like"),
        help="Optional task split forwarded to each V3 single-task run.",
    )
    parser.add_argument("--mode", dest="mode_filters", action="append", default=[])
    parser.add_argument("--task", dest="task_filters", action="append", default=[])
    parser.add_argument(
        "--difficulty", dest="difficulty_filters", action="append", default=[]
    )
    parser.add_argument(
        "--model",
        "--model-filter",
        dest="model_filters",
        action="append",
        default=[],
        help="Filter the names supplied by --models (glob syntax accepted).",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument(
        "--max-runtime",
        "--max-runtime-seconds",
        dest="max_runtime_seconds",
        type=float,
    )
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stream = stdout or sys.stdout
    args = _parser().parse_args(argv)
    default_model = (
        os.environ.get("LLM4HLS_MODEL", "deepseek-v4-pro")
        if args.backend == "vitis"
        else "deterministic-fixture-v1"
    )
    try:
        config = BenchmarkConfig(
            corpus=tuple(args.corpus),
            output_dir=args.output_dir,
            models=_split_values(args.models) or (default_model,),
            repeats=args.repeats,
            backend=args.backend,
            splits=_split_values(args.split) or ("all",),
            mode_filters=_split_values(args.mode_filters),
            task_filters=_split_values(args.task_filters),
            difficulty_filters=_split_values(args.difficulty_filters),
            model_filters=_split_values(args.model_filters),
            resume=args.resume,
            retry_failures=args.retry_failures,
            max_tasks=args.max_tasks,
            max_runtime_seconds=args.max_runtime_seconds,
            validation_profile=args.validation_profile,
            final_validation_policy=args.final_validation_policy,
            evidence_memory_mode=args.evidence_memory,
            max_planner_rounds=args.max_planner_rounds,
            max_no_improvement_rounds=args.max_no_improvement_rounds,
            enable_final_fallback=args.enable_final_fallback,
            continuation_policy_mode=args.continuation_policy,
            continuation_policy_version=args.continuation_policy_version,
            continuation_admission_manifest=(
                args.continuation_admission_manifest
            ),
            full_agent_manifest=args.full_agent_manifest,
            experience_mode=args.experience_mode,
            experience_store=args.experience_store,
            experience_ranker_version=args.experience_ranker_version,
            experience_admission_manifest=args.experience_admission_manifest,
            experience_task_split=args.experience_task_split,
        )
        outcome = BatchBenchmarkRunner(config).run()
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
    real = _mapping(outcome.summary.get("real_evidence_headline"))
    payload = {
        "status": "DONE",
        "schema_version": SUMMARY_SCHEMA,
        "benchmark_fingerprint": outcome.summary.get("benchmark_fingerprint"),
        "records": len(outcome.records),
        "real_evidence_runs": real.get("runs", 0),
        "real_e2e_success_rate": real.get("e2e_success_rate"),
        "metric_status": (
            "N/A_ORCHESTRATION_ONLY"
            if outcome.records
            and all(
                record.get("status") == "ORCHESTRATION_ONLY"
                for record in outcome.records
            )
            else "MEASURED"
        ),
        "output_dir": str(config.output_dir),
        "results_ref": "benchmark_results.jsonl",
        "summary_ref": "summary.json",
        "csv_ref": "summary.csv",
        "report_ref": "report.md",
        "benchmark_summary_ref": "benchmark_summary.json",
        "benchmark_csv_ref": "benchmark_summary.csv",
        "benchmark_report_ref": "benchmark_report.md",
    }
    print(_canonical_json(payload), file=stream)
    return (
        0
        if all(
            record.get("e2e_success") is True
            or record.get("status") == "ORCHESTRATION_ONLY"
            for record in outcome.records
        )
        else 2
    )


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
