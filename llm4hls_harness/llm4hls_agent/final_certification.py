"""Independent Track-A certification for an already frozen search Candidate."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .task import PublicTask
from .tools import BackendResult, ToolBackend, ToolConfig
from .runtime_control import RuntimeDeadline
from .vitis import VitisBackend


FREEZE_SCHEMA = "v3.frozen-search-candidate.v1"
CERTIFICATION_SCHEMA = "v3.final-certification-receipt.v1"
CERTIFICATION_ACTION_SCHEMA = "v3.final-certification-action.v1"
CERTIFIED_RESULT_SCHEMA = "v3.certified-result.v1"
CERTIFICATION_IMPLEMENTATION_VERSION = "v3.final-certification-implementation.v5"
CERTIFICATION_BUDGET_DOMAIN = "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET"
NO_FEEDBACK_POLICY = "NO_SAME_RUN_AGENT_FEEDBACK"
REFERENCE_TOOL_COSTS = {"csim": 1, "synth": 4, "cosim": 20}


class FinalCertificationError(RuntimeError):
    """Raised when a frozen binding or certification receipt is unsafe."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise FinalCertificationError(f"{label} must be a JSON object")
    try:
        decoded = json.loads(_canonical_json(dict(value)).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalCertificationError(f"{label} is not canonical JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise FinalCertificationError(f"{label} must be a JSON object")
    return decoded


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value))


def _atomic_json(path: Path, value: object) -> bytes:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return encoded


def _read_json_with_bytes(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        encoded = path.read_bytes()
        value = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalCertificationError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalCertificationError(f"{path.name} must contain a JSON object")
    return value, encoded


def _read_json(path: Path) -> dict[str, object]:
    value, _encoded = _read_json_with_bytes(path)
    return value


def _safe_ref(root: Path, reference: object) -> Path:
    if not isinstance(reference, str) or not reference:
        raise FinalCertificationError("frozen Candidate source_ref is missing")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise FinalCertificationError("frozen Candidate source_ref escapes run root")
    unresolved = root / relative
    if unresolved.is_symlink():
        raise FinalCertificationError("frozen Candidate source must not be a symlink")
    path = unresolved.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FinalCertificationError(
            "frozen Candidate source_ref resolves outside run root"
        ) from exc
    if not path.is_file():
        raise FinalCertificationError("frozen Candidate source is not a regular file")
    return path


def _backend_fingerprint(backend: object) -> str:
    method = getattr(backend, "fingerprint", None)
    value = (
        str(method())
        if callable(method)
        else f"{type(backend).__module__}.{type(backend).__qualname__}"
    )
    if not value:
        raise FinalCertificationError("certification backend fingerprint is empty")
    return value


def _task_fingerprint(task: PublicTask) -> str:
    return _sha256_json(
        {
            "task_id": task.id,
            "top": task.top,
            "kernel_file": task.kernel_name,
            "public_tb": task.public_tb_name,
            "public_file_hashes": dict(task.public_file_hashes),
            "part": task.part,
            "clock_ns": task.clock_ns,
        }
    )


@dataclass(frozen=True)
class CertificationConfig:
    """Tool identity and the organizer-facing 100 MHz hard gate."""

    tool: ToolConfig
    maximum_clock_period_ns: float = 10.0
    runtime_deadline: RuntimeDeadline | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        period = float(self.maximum_clock_period_ns)
        if not math.isfinite(period) or period <= 0:
            raise ValueError("maximum_clock_period_ns must be finite and positive")
        object.__setattr__(self, "maximum_clock_period_ns", period)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool.to_dict(),
            "maximum_clock_period_ns": self.maximum_clock_period_ns,
            "minimum_frequency_mhz": 1000.0 / self.maximum_clock_period_ns,
        }


def freeze_search_candidate(
    task: PublicTask,
    search_run_dir: str | Path,
    search_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write-once binding used by certification after Agent search terminates."""

    root = Path(search_run_dir).resolve()
    result_path = root / "v3_prototype_result.json"
    result, search_result_bytes = _read_json_with_bytes(result_path)
    if search_result is not None:
        supplied_result = _canonical_mapping(
            search_result,
            label="supplied search result",
        )
        if _canonical_json(supplied_result) != _canonical_json(result):
            raise FinalCertificationError(
                "supplied search result differs from v3_prototype_result.json"
            )
    if result.get("task_id") != task.id:
        raise FinalCertificationError(
            "search result task_id does not match PublicTask"
        )
    if result.get("status") != "DONE":
        raise FinalCertificationError(
            "only a completed Agent search can freeze a certification Candidate"
        )
    binding = result.get("terminal_candidate_binding")
    if not isinstance(binding, Mapping):
        raise FinalCertificationError("search result lacks terminal Candidate binding")
    candidate_id = binding.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise FinalCertificationError("search result has no legal terminal Candidate")
    source_path = _safe_ref(root, binding.get("source_ref"))
    source_bytes = source_path.read_bytes()
    source_sha256 = _sha256_bytes(source_bytes)
    if binding.get("source_sha256") != source_sha256:
        raise FinalCertificationError("terminal Candidate source hash is stale")
    ledger_path = root / "budget_ledger.jsonl"
    if not ledger_path.is_file():
        raise FinalCertificationError("Agent search Ledger is missing")
    ledger_bytes = ledger_path.read_bytes()
    run_config_path = root / "v3_run_config.json"
    task_spec_path = root / "v3_task_spec.json"
    if not run_config_path.is_file() or not task_spec_path.is_file():
        raise FinalCertificationError("Agent search identity artifacts are missing")
    run_config, run_config_bytes = _read_json_with_bytes(run_config_path)
    task_spec, task_spec_bytes = _read_json_with_bytes(task_spec_path)
    expected_task_spec = {
        "task_id": task.id,
        "top": task.top,
        "kernel_file": task.kernel_name,
        "public_tb": task.public_tb_name,
        "public_file_hashes": dict(task.public_file_hashes),
        "part": task.part,
        "clock_ns": task.clock_ns,
    }
    for name, expected in expected_task_spec.items():
        if task_spec.get(name) != expected:
            raise FinalCertificationError(
                f"v3_task_spec.json {name} does not match PublicTask"
            )
    search_tool_config = _canonical_mapping(
        run_config.get("tool"),
        label="v3_run_config.json tool",
    )
    freeze: dict[str, object] = {
        "schema_version": FREEZE_SCHEMA,
        "freeze_status": "FROZEN",
        "budget_domain": "agent_search",
        "feedback_policy": NO_FEEDBACK_POLICY,
        "task_id": task.id,
        "task_fingerprint": _task_fingerprint(task),
        "candidate_id": candidate_id,
        "source_ref": str(binding["source_ref"]),
        "source_sha256": source_sha256,
        "terminal_binding_sha256": _sha256_json(dict(binding)),
        "registry_revision": binding.get("registry_revision"),
        "candidate_decision_ref": binding.get("candidate_decision_ref"),
        "search_result_ref": "v3_prototype_result.json",
        "search_result_sha256": _sha256_bytes(search_result_bytes),
        "agent_ledger_ref": "budget_ledger.jsonl",
        "agent_ledger_sha256": _sha256_bytes(ledger_bytes),
        "run_config_sha256": _sha256_bytes(run_config_bytes),
        "task_spec_sha256": _sha256_bytes(task_spec_bytes),
        "search_tool_config": search_tool_config,
        "search_tool_config_sha256": _sha256_json(search_tool_config),
    }
    freeze["freeze_sha256"] = _sha256_json(freeze)
    freeze_path = root / "control" / "frozen_candidate.json"
    if freeze_path.is_file():
        existing = _read_json(freeze_path)
        if existing != freeze:
            raise FinalCertificationError("frozen Candidate binding is immutable")
    else:
        _atomic_json(freeze_path, freeze)
    return freeze


def _artifact_hashes(
    action_root: Path, artifacts: Mapping[str, str]
) -> dict[str, str]:
    values: dict[str, str] = {}
    root = action_root.resolve()
    for name, reference in artifacts.items():
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise FinalCertificationError(f"{name} artifact escapes action root")
        path = (action_root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FinalCertificationError(
                f"{name} artifact resolves outside action root"
            ) from exc
        if not path.is_file():
            raise FinalCertificationError(f"{name} certification artifact is missing")
        values[str(name)] = _sha256_bytes(path.read_bytes())
    return values


def _validate_cached_stage_artifacts(
    action_root: Path, stage_result: Mapping[str, object]
) -> None:
    raw_artifacts = stage_result.get("artifacts")
    raw_hashes = stage_result.get("artifact_hashes")
    if not isinstance(raw_artifacts, Mapping) or not isinstance(
        raw_hashes, Mapping
    ):
        raise FinalCertificationError(
            "cached certification stage lacks artifact bindings"
        )
    artifacts: dict[str, str] = {}
    expected_hashes: dict[str, str] = {}
    for name, reference in raw_artifacts.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(reference, str)
            or not reference
        ):
            raise FinalCertificationError(
                "cached certification artifact binding is invalid"
            )
        artifacts[name] = reference
    for name, digest in raw_hashes.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise FinalCertificationError(
                "cached certification artifact hash is invalid"
            )
        expected_hashes[name] = digest
    if set(artifacts) != set(expected_hashes):
        raise FinalCertificationError(
            "cached certification artifact bindings are incomplete"
        )
    if _artifact_hashes(action_root, artifacts) != expected_hashes:
        raise FinalCertificationError(
            "cached certification artifact content changed"
        )


def _stage_result(
    *,
    task: PublicTask,
    kernel_bytes: bytes,
    candidate_id: str,
    stage: str,
    action_root: Path,
    config: CertificationConfig,
    backend: ToolBackend,
    backend_fingerprint: str,
) -> dict[str, object]:
    result_path = action_root / "result.json"
    code_hash = _sha256_bytes(kernel_bytes)
    tool_config_hash = config.tool.hash_for(
        stage, backend_fingerprint=backend_fingerprint
    )
    action_identity = {
        "budget_domain": CERTIFICATION_BUDGET_DOMAIN,
        "certification_implementation_version": (
            CERTIFICATION_IMPLEMENTATION_VERSION
        ),
        "kind": stage,
        "candidate_id": candidate_id,
        "code_hash": code_hash,
        "tool_config_hash": tool_config_hash,
        "backend_fingerprint": backend_fingerprint,
        "task_fingerprint": _task_fingerprint(task),
    }
    action_id = _sha256_json(action_identity)
    if result_path.is_file():
        existing = _read_json(result_path)
        for name, expected in (
            ("schema_version", CERTIFICATION_ACTION_SCHEMA),
            ("action_id", action_id),
            ("budget_domain", CERTIFICATION_BUDGET_DOMAIN),
            (
                "certification_implementation_version",
                CERTIFICATION_IMPLEMENTATION_VERSION,
            ),
            ("kind", stage),
            ("candidate_id", candidate_id),
            ("code_hash", code_hash),
            ("tool_config_hash", tool_config_hash),
            ("backend_fingerprint", backend_fingerprint),
            ("task_fingerprint", action_identity["task_fingerprint"]),
        ):
            if existing.get(name) != expected:
                raise FinalCertificationError(
                    f"cached certification {stage} result has mismatched {name}"
                )
        _validate_cached_stage_artifacts(action_root, existing)
        return existing
    work_dir = action_root / "work"
    if work_dir.exists():
        raise FinalCertificationError(
            f"certification {stage} action is ambiguous and will not auto-rerun"
        )
    action_root.mkdir(parents=True, exist_ok=True)
    effective_timeout = config.tool.timeout_for(stage)
    runtime_permit = None
    if config.runtime_deadline is not None:
        runtime_permit = config.runtime_deadline.permit(
            effective_timeout,
            operation=stage,
            final_closure=True,
        )
        effective_timeout = runtime_permit.effective_timeout_seconds
    if runtime_permit is not None and not runtime_permit.allowed:
        backend_result = BackendResult(
            ok=False,
            phase="not_started",
            return_code=-1,
            elapsed_s=0.0,
            evidence=[str(runtime_permit.reason_code)],
        )
    else:
        try:
            backend_result = backend.run(
                stage,
                task=task,
                kernel_bytes=kernel_bytes,
                work_dir=work_dir,
                config=config.tool.with_timeout(stage, effective_timeout),
            )
        except Exception as exc:
            backend_result = BackendResult(
                ok=False,
                phase="tool_error",
                return_code=-1,
                elapsed_s=0.0,
                evidence=[f"{type(exc).__name__}: {exc}"],
            )
    result: dict[str, object] = {
        "schema_version": CERTIFICATION_ACTION_SCHEMA,
        **action_identity,
        "action_id": action_id,
        "ok": bool(backend_result.ok),
        "phase": str(backend_result.phase),
        "return_code": int(backend_result.return_code),
        "elapsed_s": float(backend_result.elapsed_s),
        "started": runtime_permit is None or runtime_permit.allowed,
        "reason_code": (
            None
            if runtime_permit is None or runtime_permit.allowed
            else runtime_permit.reason_code
        ),
        "effective_timeout_seconds": effective_timeout,
        "evidence": list(backend_result.evidence),
        "artifacts": dict(backend_result.artifacts),
        "artifact_hashes": _artifact_hashes(
            action_root, backend_result.artifacts
        ),
        "report": backend_result.report,
        "cosim": backend_result.cosim,
    }
    _atomic_json(result_path, result)
    return result


def _clock_gate(
    synth_result: Mapping[str, object],
    *,
    maximum_period_ns: float,
) -> dict[str, object]:
    report = synth_result.get("report")
    raw_period = (
        report.get("estimated_clock_period_ns")
        if isinstance(report, Mapping)
        else None
    )
    try:
        period = float(raw_period)
    except (TypeError, ValueError, OverflowError):
        period = math.nan
    valid = math.isfinite(period) and period > 0
    passed = valid and period <= maximum_period_ns
    return {
        "schema_version": "v3.final-certification-clock-gate.v1",
        "maximum_period_ns": maximum_period_ns,
        "minimum_frequency_mhz": 1000.0 / maximum_period_ns,
        "observed_period_ns": period if valid else None,
        "passed": passed,
        "reason": (
            "CLOCK_AT_OR_ABOVE_100_MHZ"
            if passed
            else "CLOCK_PERIOD_MISSING_OR_INVALID"
            if not valid
            else "CLOCK_BELOW_100_MHZ"
        ),
    }


def _certification_outcome(
    stages: Mapping[str, object],
    clock_gate: Mapping[str, object],
    *,
    ledger_unchanged: bool,
) -> tuple[str, list[str]]:
    failed_stages = [
        stage
        for stage in ("csim", "synth", "cosim")
        if not isinstance(stages.get(stage), Mapping)
        or stages[stage].get("ok") is not True  # type: ignore[union-attr]
    ]
    failure_reasons = [f"{stage.upper()}_FAILED" for stage in failed_stages]
    if clock_gate.get("passed") is not True:
        failure_reasons.append(str(clock_gate.get("reason") or "CLOCK_GATE_INVALID"))
    if not ledger_unchanged:
        failure_reasons.append("AGENT_LEDGER_MUTATED_DURING_CERTIFICATION")
    return ("PASS" if not failure_reasons else "FAIL"), failure_reasons


@contextmanager
def _exclusive_certification_lock(root: Path):
    """Fail closed instead of letting two processes execute one certification."""

    lock_path = root / "control" / "final_certification.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FinalCertificationError(
                "independent final certification is already in progress"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _certify_frozen_candidate_locked(
    task: PublicTask,
    root: Path,
    config: CertificationConfig,
    *,
    backend: ToolBackend | None = None,
    search_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Execute one certification while the run-level claim is held."""

    freeze = freeze_search_candidate(task, root, search_result=search_result)
    current_task_fingerprint = _task_fingerprint(task)
    if freeze.get("task_fingerprint") != current_task_fingerprint:
        raise FinalCertificationError(
            "current task fingerprint does not match frozen Candidate"
        )
    frozen_search_tool_config = _canonical_mapping(
        freeze.get("search_tool_config"),
        label="frozen search_tool_config",
    )
    if freeze.get("search_tool_config_sha256") != _sha256_json(
        frozen_search_tool_config
    ):
        raise FinalCertificationError("frozen search_tool_config hash is corrupt")
    certification_tool_config = _canonical_mapping(
        config.tool.to_dict(),
        label="certification tool configuration",
    )
    if _canonical_json(certification_tool_config) != _canonical_json(
        frozen_search_tool_config
    ):
        raise FinalCertificationError(
            "certification tool configuration does not match frozen search config"
        )
    source_path = _safe_ref(root, freeze.get("source_ref"))
    kernel_bytes = source_path.read_bytes()
    if _sha256_bytes(kernel_bytes) != freeze.get("source_sha256"):
        raise FinalCertificationError("frozen Candidate changed before certification")
    ledger_path = root / str(freeze["agent_ledger_ref"])
    ledger_before = ledger_path.read_bytes()
    ledger_before_sha256 = _sha256_bytes(ledger_before)
    if ledger_before_sha256 != freeze.get("agent_ledger_sha256"):
        raise FinalCertificationError("Agent Ledger changed after Candidate freeze")
    selected_backend = backend or VitisBackend()
    backend_fingerprint = _backend_fingerprint(selected_backend)
    certification_identity = {
        "schema_version": CERTIFICATION_SCHEMA,
        "certification_implementation_version": (
            CERTIFICATION_IMPLEMENTATION_VERSION
        ),
        "freeze_sha256": freeze["freeze_sha256"],
        "task_fingerprint": freeze["task_fingerprint"],
        "candidate_id": freeze["candidate_id"],
        "source_sha256": freeze["source_sha256"],
        "config": config.to_dict(),
        "backend_fingerprint": backend_fingerprint,
        "budget_domain": CERTIFICATION_BUDGET_DOMAIN,
    }
    certification_id = _sha256_json(certification_identity)
    certification_root = root / "certification" / certification_id
    receipt_path = certification_root / "receipt.json"
    if receipt_path.is_file():
        receipt = _read_json(receipt_path)
        if (
            receipt.get("schema_version") != CERTIFICATION_SCHEMA
            or receipt.get("certification_id") != certification_id
            or receipt.get("receipt_sha256")
            != _sha256_json(
                {
                    key: value
                    for key, value in receipt.items()
                    if key != "receipt_sha256"
                }
            )
        ):
            raise FinalCertificationError("certification receipt is corrupt")
        for name, expected in certification_identity.items():
            if receipt.get(name) != expected:
                raise FinalCertificationError(
                    f"certification receipt has mismatched {name}"
                )
        cached_stages = receipt.get("stages")
        if (
            not isinstance(cached_stages, Mapping)
            or set(cached_stages) != {"csim", "synth", "cosim"}
        ):
            raise FinalCertificationError(
                "certification receipt lacks stage bindings"
            )
        validated_stages: dict[str, object] = {}
        for stage in ("csim", "synth", "cosim"):
            cached_stage = cached_stages.get(stage)
            if not isinstance(cached_stage, Mapping):
                raise FinalCertificationError(
                    f"certification receipt lacks {stage} stage"
                )
            action_root = certification_root / "actions" / stage
            if not (action_root / "result.json").is_file():
                raise FinalCertificationError(
                    f"cached certification {stage} result is missing"
                )
            validated_stage = _stage_result(
                task=task,
                kernel_bytes=kernel_bytes,
                candidate_id=str(freeze["candidate_id"]),
                stage=stage,
                action_root=action_root,
                config=config,
                backend=selected_backend,
                backend_fingerprint=backend_fingerprint,
            )
            if _canonical_json(cached_stage) != _canonical_json(validated_stage):
                raise FinalCertificationError(
                    f"certification receipt {stage} stage differs from result.json"
                )
            validated_stages[stage] = validated_stage
        expected_clock_gate = _clock_gate(
            validated_stages["synth"],  # type: ignore[arg-type]
            maximum_period_ns=config.maximum_clock_period_ns,
        )
        if receipt.get("clock_gate") != expected_clock_gate:
            raise FinalCertificationError(
                "certification receipt clock gate is inconsistent"
            )
        ledger_after = ledger_path.read_bytes()
        ledger_after_sha256 = _sha256_bytes(ledger_after)
        ledger_unchanged = ledger_after == ledger_before
        expected_status, expected_failure_reasons = _certification_outcome(
            validated_stages,
            expected_clock_gate,
            ledger_unchanged=ledger_unchanged,
        )
        if (
            receipt.get("status") != expected_status
            or receipt.get("failure_reasons") != expected_failure_reasons
            or receipt.get("feedback_policy") != NO_FEEDBACK_POLICY
            or receipt.get("next_action_on_failure")
            != "START_NEW_AGENT_SEARCH_RUN"
            or receipt.get("agent_credits_charged") != 0
            or receipt.get("reference_tool_costs") != REFERENCE_TOOL_COSTS
            or receipt.get("reference_tool_costs_are_official") is not False
            or receipt.get("frozen_candidate_ref")
            != "control/frozen_candidate.json"
            or receipt.get("agent_ledger")
            != {
                "ref": str(freeze["agent_ledger_ref"]),
                "before_sha256": ledger_before_sha256,
                "after_sha256": ledger_after_sha256,
                "unchanged": ledger_unchanged,
            }
        ):
            raise FinalCertificationError(
                "certification receipt outcome contract is inconsistent"
            )
        if not ledger_unchanged:
            raise FinalCertificationError("Agent Ledger changed after certification")
        return receipt

    stages: dict[str, object] = {}
    for stage in ("csim", "synth", "cosim"):
        stages[stage] = _stage_result(
            task=task,
            kernel_bytes=kernel_bytes,
            candidate_id=str(freeze["candidate_id"]),
            stage=stage,
            action_root=certification_root / "actions" / stage,
            config=config,
            backend=selected_backend,
            backend_fingerprint=backend_fingerprint,
        )
    clock_gate = _clock_gate(
        stages["synth"],  # type: ignore[arg-type]
        maximum_period_ns=config.maximum_clock_period_ns,
    )
    ledger_after = ledger_path.read_bytes()
    ledger_after_sha256 = _sha256_bytes(ledger_after)
    ledger_unchanged = ledger_after == ledger_before
    status, failure_reasons = _certification_outcome(
        stages,
        clock_gate,
        ledger_unchanged=ledger_unchanged,
    )
    receipt: dict[str, object] = {
        **certification_identity,
        "certification_id": certification_id,
        "status": status,
        "failure_reasons": failure_reasons,
        "feedback_policy": NO_FEEDBACK_POLICY,
        "next_action_on_failure": "START_NEW_AGENT_SEARCH_RUN",
        "agent_credits_charged": 0,
        "reference_tool_costs": dict(REFERENCE_TOOL_COSTS),
        "reference_tool_costs_are_official": False,
        "frozen_candidate_ref": "control/frozen_candidate.json",
        "stages": stages,
        "clock_gate": clock_gate,
        "agent_ledger": {
            "ref": str(freeze["agent_ledger_ref"]),
            "before_sha256": ledger_before_sha256,
            "after_sha256": ledger_after_sha256,
            "unchanged": ledger_unchanged,
        },
    }
    receipt["receipt_sha256"] = _sha256_json(receipt)
    _atomic_json(receipt_path, receipt)
    return receipt


def certify_frozen_candidate(
    task: PublicTask,
    search_run_dir: str | Path,
    config: CertificationConfig,
    *,
    backend: ToolBackend | None = None,
    search_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Execute CSim, Synth, CoSim and the 100 MHz gate outside Agent Ledger."""

    root = Path(search_run_dir).resolve()
    with _exclusive_certification_lock(root):
        return _certify_frozen_candidate_locked(
            task,
            root,
            config,
            backend=backend,
            search_result=search_result,
        )


def certify_v3_search_result(
    task: PublicTask,
    search_run_dir: str | Path,
    config: CertificationConfig,
    *,
    backend: ToolBackend | None = None,
) -> dict[str, object]:
    """Produce the post-search combined result consumed by CLI/batch reporting."""

    root = Path(search_run_dir).resolve()
    with _exclusive_certification_lock(root):
        search_result = _read_json(root / "v3_prototype_result.json")
        receipt = _certify_frozen_candidate_locked(
            task,
            root,
            config,
            backend=backend,
            search_result=search_result,
        )
        combined = dict(search_result)
        combined["result_schema"] = CERTIFIED_RESULT_SCHEMA
        combined["agent_search_status"] = search_result.get("status")
        combined["agent_search_stop_reason"] = search_result.get("stop_reason")
        combined["final_certification"] = {
            "status": receipt["status"],
            "certification_id": receipt["certification_id"],
            "budget_domain": receipt["budget_domain"],
            "agent_credits_charged": 0,
            "feedback_policy": receipt["feedback_policy"],
            "receipt_ref": (
                f"certification/{receipt['certification_id']}/receipt.json"
            ),
            "receipt_sha256": receipt["receipt_sha256"],
        }
        if receipt["status"] == "PASS":
            combined["status"] = "DONE"
        else:
            combined["status"] = "FAILED"
            combined["stop_reason"] = (
                "FINAL_CERTIFICATION_FAILED_NEW_AGENT_RUN_REQUIRED"
            )
        _atomic_json(root / "v3_certified_result.json", combined)
        return combined
