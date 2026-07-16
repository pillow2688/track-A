"""Deterministic, secret-free artifact manifests for audited run evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping


class ArtifactManifestError(RuntimeError):
    """Raised when run evidence cannot be safely indexed or verified."""


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "artifact_manifest.json"
_EXCLUDED_NAMES = {MANIFEST_NAME, "acceptance_result.json", ".run.lock"}
_SENSITIVE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
_V1_REQUIRED_CORE = {
    "task_spec.json",
    "run_config.json",
    "candidate_registry.json",
    "budget_ledger.jsonl",
    "budget_state.json",
    "trace.jsonl",
    "v1_result.json",
    "experimental_report.md",
}

_V2_COMMON_REQUIRED = {
    "task_spec.json",
    "run_config.json",
    "candidate_registry.json",
    "budget_ledger.jsonl",
    "budget_state.json",
    "trace.jsonl",
    "workflow_result.json",
    "experimental_report.md",
}


def _canonical_json(value: object) -> bytes:
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


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactManifestError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactManifestError(f"{path.name} is not a JSON object")
    return value


def _atomic_json(path: Path, value: object) -> None:
    encoded = _canonical_json(value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ArtifactManifestError(f"cannot write {path.name}: {exc}") from exc


def _is_sensitive(relative: Path) -> bool:
    lowered = relative.name.casefold()
    return (
        lowered in _SENSITIVE_NAMES
        or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
        or "api_key" in lowered
        or "apikey" in lowered
        or "secret" in lowered
    )


def _artifact_type(path: str) -> str:
    if path == "task_spec.json":
        return "task_spec"
    if path == "run_config.json":
        return "run_config"
    if path == "candidate_registry.json":
        return "candidate_registry"
    if path.startswith("baseline/source/"):
        return "baseline_source"
    if path.startswith("candidates/") and "/source/" in path:
        return "candidate_source"
    if path.startswith("candidates/") and path.endswith("/patch.diff"):
        return "candidate_patch"
    if path.startswith("candidates/") and path.endswith("/candidate.json"):
        return "candidate_metadata"
    if path.startswith("llm_actions/") and path.endswith("/result.json"):
        return "llm_action_result"
    if path.startswith("llm_actions/") and path.endswith("/request.json"):
        return "llm_action_request"
    if path.startswith("actions/") and path.endswith("/result.json"):
        return "tool_action_result"
    if path.startswith("actions/"):
        return "vitis_artifact"
    if path.startswith("diagnostics/"):
        return "diagnostic"
    if path == "budget_ledger.jsonl":
        return "budget_ledger"
    if path == "budget_state.json":
        return "budget_state"
    if path == "trace.jsonl":
        return "trace"
    if path in {
        "v1_result.json",
        "v2_result.json",
        "v2_rejection_result.json",
        "workflow_result.json",
    }:
        return "workflow_result"
    if path == "optimization_config.json":
        return "optimization_config"
    if path.startswith("optimization_rounds/") and path.endswith(".json"):
        return "optimization_round"
    if path.startswith("scores/") and path.endswith(".json"):
        return "candidate_score"
    if path.startswith("comparisons/") and path.endswith(".json"):
        return "candidate_comparison"
    if path == "experimental_report.md":
        return "experimental_report"
    return "run_artifact"


def _path_identity(
    run_root: Path, relative: str
) -> tuple[str, str | None, str | None]:
    parts = Path(relative).parts
    action_id: str | None = None
    candidate_id: str | None = None
    producer = "workflow"
    if len(parts) >= 2 and parts[0] == "actions":
        action_id = parts[1]
        producer = "toolserver"
    elif len(parts) >= 2 and parts[0] == "llm_actions":
        action_id = parts[1]
        producer = "repair_provider"
    elif len(parts) >= 2 and parts[0] == "candidates":
        candidate_id = parts[1]
        producer = "candidate_manager"
    elif parts and parts[0] == "baseline":
        candidate_id = "candidate_000"
        producer = "baseline_manager"
    if relative.endswith("result.json"):
        try:
            value = _read_json(run_root / relative)
        except ArtifactManifestError:
            value = {}
        raw_candidate = value.get("candidate_id")
        if isinstance(raw_candidate, str):
            candidate_id = raw_candidate
        raw_action = value.get("action_id")
        if isinstance(raw_action, str):
            action_id = raw_action
    return producer, action_id, candidate_id


def _metadata(run_root: Path) -> dict[str, object]:
    result_path = next(
        (
            run_root / name
            for name in (
                "v2_result.json",
                "v2_rejection_result.json",
                "v1_result.json",
                "workflow_result.json",
            )
            if (run_root / name).is_file()
        ),
        run_root / "workflow_result.json",
    )
    result = _read_json(result_path)
    registry = _read_json(run_root / "candidate_registry.json")
    run_config = _read_json(run_root / "run_config.json")
    baseline = result.get("baseline")
    baseline_result = baseline if isinstance(baseline, Mapping) else result
    candidate = result.get("candidate")
    candidate_value = candidate if isinstance(candidate, Mapping) else {}
    patch = result.get("patch")
    patch_value = patch if isinstance(patch, Mapping) else {}
    final_id = registry.get("final_candidate_id")
    baseline_id = registry.get("baseline_candidate_id", "candidate_000")
    candidates = registry.get("candidates")
    candidate_map = candidates if isinstance(candidates, Mapping) else {}
    selected_id = final_id if isinstance(final_id, str) else baseline_id
    selected = candidate_map.get(selected_id, {})
    selected_value = selected if isinstance(selected, Mapping) else {}
    tool = run_config.get("tool")
    tool_value = tool if isinstance(tool, Mapping) else {}
    return {
        "workflow": result.get("workflow"),
        "run_id": str(baseline_result.get("run_id", run_root.name)),
        "task_id": str(baseline_result.get("task_id", registry.get("task_id", ""))),
        "baseline_candidate_id": baseline_id,
        "best_candidate_id": registry.get("best_candidate_id"),
        "final_candidate_id": final_id,
        "provider": candidate_value.get(
            "provider", patch_value.get("provider", selected_value.get("provider"))
        ),
        "model": candidate_value.get(
            "model", patch_value.get("model", selected_value.get("model"))
        ),
        "toolchain_version": tool_value.get("toolchain_id"),
        "code_hash": selected_value.get("code_hash"),
        "tool_config_hash": _digest(
            json.dumps(
                dict(tool_value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ),
    }


def _required_paths(run_root: Path, workflow: object) -> set[str]:
    if workflow == "V2_CANDIDATE_PPA":
        required = _V2_COMMON_REQUIRED | {
            "v2_result.json",
            "optimization_config.json",
        }
        required.update(
            path.relative_to(run_root).as_posix()
            for directory in ("optimization_rounds", "scores", "comparisons")
            for path in sorted((run_root / directory).glob("*.json"))
        )
        return required
    if workflow == "V2_SAFETY_REJECTION":
        return _V2_COMMON_REQUIRED | {"v2_rejection_result.json"}
    if (run_root / "v1_result.json").is_file():
        return set(_V1_REQUIRED_CORE)
    return set()


def build_artifact_manifest(run_dir: str | Path) -> dict[str, object]:
    """Index every durable run artifact using stable relative paths and SHA-256."""

    run_root = Path(run_dir).resolve()
    if not run_root.is_dir():
        raise ArtifactManifestError(f"run directory does not exist: {run_root}")
    staging = run_root / ".candidate_staging"
    if staging.exists() and any(staging.iterdir()):
        raise ArtifactManifestError("candidate staging contains an incomplete materialization")
    metadata = _metadata(run_root)
    required_paths = _required_paths(run_root, metadata.get("workflow"))
    entries: list[dict[str, object]] = []
    for path in sorted(run_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ArtifactManifestError(f"artifact symlink is not allowed: {path}")
        if not path.is_file():
            continue
        relative_path = path.relative_to(run_root)
        relative = relative_path.as_posix()
        if (
            path.name in _EXCLUDED_NAMES
            or path.name.endswith((".lock", ".tmp"))
            or ".candidate_staging" in relative_path.parts
        ):
            continue
        if _is_sensitive(relative_path):
            raise ArtifactManifestError(f"sensitive artifact path is not allowed: {relative}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactManifestError(f"cannot read artifact {relative}: {exc}") from exc
        producer, action_id, candidate_id = _path_identity(run_root, relative)
        entries.append(
            {
                "artifact_type": _artifact_type(relative),
                "path": relative,
                "sha256": _digest(data),
                "size_bytes": len(data),
                "producer": producer,
                "action_id": action_id,
                "candidate_id": candidate_id,
                "required": relative in required_paths,
            }
        )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        **metadata,
        "artifacts": entries,
    }
    _atomic_json(run_root / MANIFEST_NAME, manifest)
    return manifest


def verify_artifact_manifest(run_dir: str | Path) -> dict[str, object]:
    """Verify manifest schema, path boundaries, sizes and content digests."""

    run_root = Path(run_dir).resolve()
    manifest = _read_json(run_root / MANIFEST_NAME)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ArtifactManifestError("unsupported artifact manifest schema")
    raw_entries = manifest.get("artifacts")
    if not isinstance(raw_entries, list):
        raise ArtifactManifestError("manifest artifacts is not an array")
    paths: list[str] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise ArtifactManifestError("manifest artifact entry is not an object")
        relative = raw.get("path")
        if not isinstance(relative, str) or not relative:
            raise ArtifactManifestError("manifest artifact path is invalid")
        path_value = Path(relative)
        if path_value.is_absolute() or ".." in path_value.parts or "\\" in relative:
            raise ArtifactManifestError(f"manifest artifact escapes run directory: {relative}")
        path = (run_root / path_value).resolve()
        try:
            path.relative_to(run_root)
        except ValueError as exc:
            raise ArtifactManifestError(
                f"manifest artifact resolves outside run directory: {relative}"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise ArtifactManifestError(f"manifest artifact is missing: {relative}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactManifestError(f"cannot read artifact {relative}: {exc}") from exc
        if raw.get("size_bytes") != len(data):
            raise ArtifactManifestError(f"artifact size mismatch: {relative}")
        if raw.get("sha256") != _digest(data):
            raise ArtifactManifestError(f"artifact digest mismatch: {relative}")
        paths.append(relative)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ArtifactManifestError("manifest artifact paths are not unique and sorted")
    required_paths = _required_paths(run_root, manifest.get("workflow"))
    missing = sorted(required_paths.difference(paths))
    if missing:
        raise ArtifactManifestError(
            "manifest required artifacts are missing: " + ", ".join(missing)
        )
    required_flags = {
        str(raw["path"])
        for raw in raw_entries
        if isinstance(raw, Mapping) and raw.get("required") is True
    }
    if required_flags != required_paths:
        raise ArtifactManifestError("manifest required flags do not match workflow")
    return manifest


def manifest_digest(run_dir: str | Path) -> str:
    path = Path(run_dir).resolve() / MANIFEST_NAME
    try:
        return _digest(path.read_bytes())
    except OSError as exc:
        raise ArtifactManifestError(f"cannot hash {MANIFEST_NAME}: {exc}") from exc
