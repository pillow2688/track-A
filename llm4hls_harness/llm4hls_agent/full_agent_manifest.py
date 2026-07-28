"""Strict loader for the explicit Track A Full Agent runtime manifest.

The safe baseline remains the default product profile.  This module authorizes
no mode by itself: a caller must also supply matching runtime arguments.  The
manifest only makes the complete A1/A2/A3 configuration and its immutable
Gate/Admission/Store inputs content-addressed and fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_POLICY_VERSION,
)
from .v3_continuation_v2 import CONTINUATION_DECISION_SCHEMA_V2
from .v3_experience_v2_runtime import RANKER_ADMISSION_SCHEMA
from .v3_strategy_ranker_v3 import STRATEGY_RANKER_V3_SCHEMA


FULL_AGENT_MANIFEST_SCHEMA = "track-a-full-agent.v1"
FULL_AGENT_PROFILE = "competition-full-agent-v1"
FULL_AGENT_READY = "READY"
FULL_AGENT_PENDING = "PENDING_ARTIFACT_BINDINGS"

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ARTIFACT_ROLES = (
    "a2_gate",
    "a2_admission",
    "a3_store",
    "a3_gate",
    "a3_admission",
)
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "profile",
    "artifact_kind",
    "status",
    "runtime_loader",
    "runtime_authority",
    "artifact_root",
    "token_policy",
    "components",
    "foundations",
    "artifacts",
}


class FullAgentManifestError(ValueError):
    """Raised when a Full Agent manifest is incomplete or inconsistent."""


@dataclass(frozen=True)
class FullAgentArtifact:
    """One immutable artifact referenced by the Full Agent manifest."""

    role: str
    relative_path: str
    path: Path
    sha256: str
    size_bytes: int

    def public_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class FullAgentManifest:
    """Validated, content-addressed Full Agent configuration snapshot."""

    path: Path
    sha256: str
    status: str
    artifacts: Mapping[str, FullAgentArtifact]

    def artifact(self, role: str) -> FullAgentArtifact:
        try:
            return self.artifacts[role]
        except KeyError as exc:
            raise FullAgentManifestError(
                f"Full Agent manifest lacks artifact role {role}"
            ) from exc

    def public_dict(self) -> dict[str, object]:
        return {
            "schema_version": FULL_AGENT_MANIFEST_SCHEMA,
            "profile": FULL_AGENT_PROFILE,
            "status": self.status,
            "path": str(self.path),
            "sha256": self.sha256,
            "artifacts": {
                role: artifact.public_dict()
                for role, artifact in sorted(self.artifacts.items())
            },
        }

    def verify_unchanged(self) -> None:
        current = load_full_agent_manifest(self.path, require_ready=True)
        if current.public_dict() != self.public_dict():
            raise FullAgentManifestError(
                "Full Agent manifest or a bound artifact changed after snapshot"
            )


def _require_exact_mapping(
    value: object,
    expected: Mapping[str, object],
    *,
    label: str,
) -> None:
    if not isinstance(value, Mapping) or dict(value) != dict(expected):
        raise FullAgentManifestError(f"{label} does not match the Full Agent contract")


def _read_manifest(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise FullAgentManifestError(
            f"Full Agent manifest must be a regular non-symlink file: {path}"
        )
    try:
        raw = path.read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullAgentManifestError(
            f"Full Agent manifest is unreadable: {path}"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise FullAgentManifestError("Full Agent manifest must be a JSON object")
    return raw, decoded


def _resolve_artifact_root(manifest_path: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FullAgentManifestError("artifact_root must be a relative directory")
    relative = Path(value)
    if relative.is_absolute():
        raise FullAgentManifestError("artifact_root must be relative")
    root = (manifest_path.parent / relative).resolve()
    if not root.is_dir():
        raise FullAgentManifestError(f"artifact_root is not a directory: {root}")
    return root


def _artifact_binding(
    *,
    role: str,
    value: object,
    artifact_root: Path,
    allow_pending: bool,
) -> FullAgentArtifact | None:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise FullAgentManifestError(
            f"Full Agent artifact {role} fields must be path and sha256"
        )
    raw_path = value.get("path")
    raw_sha256 = value.get("sha256")
    if raw_path is None and raw_sha256 is None and allow_pending:
        return None
    if not isinstance(raw_path, str) or not raw_path:
        raise FullAgentManifestError(f"Full Agent artifact {role} path is missing")
    if not isinstance(raw_sha256, str) or _SHA256.fullmatch(raw_sha256) is None:
        raise FullAgentManifestError(
            f"Full Agent artifact {role} sha256 is invalid"
        )
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise FullAgentManifestError(
            f"Full Agent artifact {role} path must stay below artifact_root"
        )
    unresolved = artifact_root / relative
    if unresolved.is_symlink():
        raise FullAgentManifestError(
            f"Full Agent artifact {role} must not be a symlink"
        )
    path = unresolved.resolve()
    try:
        path.relative_to(artifact_root)
    except ValueError as exc:
        raise FullAgentManifestError(
            f"Full Agent artifact {role} escapes artifact_root"
        ) from exc
    if not path.is_file():
        raise FullAgentManifestError(
            f"Full Agent artifact {role} is missing: {path}"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FullAgentManifestError(
            f"Full Agent artifact {role} is unreadable: {path}"
        ) from exc
    observed = hashlib.sha256(data).hexdigest()
    if observed != raw_sha256:
        raise FullAgentManifestError(
            f"Full Agent artifact {role} sha256 mismatch"
        )
    return FullAgentArtifact(
        role=role,
        relative_path=raw_path,
        path=path,
        sha256=observed,
        size_bytes=len(data),
    )


def _read_json_artifact(
    artifact: FullAgentArtifact,
) -> Mapping[str, object]:
    try:
        value = json.loads(artifact.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullAgentManifestError(
            f"Full Agent artifact {artifact.role} is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise FullAgentManifestError(
            f"Full Agent artifact {artifact.role} must be a JSON object"
        )
    return value


def _require_declared_binding(
    admission: FullAgentArtifact,
    value: Mapping[str, object],
    *,
    path_field: str,
    sha256_field: str,
    target: FullAgentArtifact,
) -> None:
    raw_reference = value.get(path_field)
    if not isinstance(raw_reference, str) or not raw_reference:
        raise FullAgentManifestError(
            f"Full Agent {admission.role} lacks {path_field}"
        )
    relative = Path(raw_reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise FullAgentManifestError(
            f"Full Agent {admission.role} {path_field} is not local"
        )
    resolved = (admission.path.parent / relative).resolve()
    if resolved != target.path:
        raise FullAgentManifestError(
            f"Full Agent {admission.role} does not reference {target.role}"
        )
    if value.get(sha256_field) != target.sha256:
        raise FullAgentManifestError(
            f"Full Agent {admission.role} hash does not bind {target.role}"
        )


def _validate_ready_artifact_graph(
    artifacts: Mapping[str, FullAgentArtifact],
) -> None:
    a2_admission = artifacts["a2_admission"]
    a2_value = _read_json_artifact(a2_admission)
    if (
        a2_value.get("schema_version") != CONTINUATION_ADMISSION_SCHEMA
        or a2_value.get("policy_version") != CONTINUATION_POLICY_VERSION
        or a2_value.get("decision_schema") != CONTINUATION_DECISION_SCHEMA_V2
    ):
        raise FullAgentManifestError(
            "Full Agent A2 Admission contract mismatch"
        )
    _require_declared_binding(
        a2_admission,
        a2_value,
        path_field="gate_json_path",
        sha256_field="gate_json_sha256",
        target=artifacts["a2_gate"],
    )

    a3_admission = artifacts["a3_admission"]
    a3_value = _read_json_artifact(a3_admission)
    if (
        a3_value.get("schema_version") != RANKER_ADMISSION_SCHEMA
        or a3_value.get("decision") != "PASS"
        or a3_value.get("ranker_schema") != STRATEGY_RANKER_V3_SCHEMA
    ):
        raise FullAgentManifestError(
            "Full Agent A3 Admission contract mismatch"
        )
    _require_declared_binding(
        a3_admission,
        a3_value,
        path_field="store_path",
        sha256_field="store_sha256",
        target=artifacts["a3_store"],
    )
    _require_declared_binding(
        a3_admission,
        a3_value,
        path_field="gate_path",
        sha256_field="gate_sha256",
        target=artifacts["a3_gate"],
    )


def load_full_agent_manifest(
    path: str | Path,
    *,
    require_ready: bool = True,
) -> FullAgentManifest:
    """Load one Full Agent manifest without silently weakening its profile."""

    source_path = Path(path).expanduser()
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    raw, value = _read_manifest(source_path)
    resolved = source_path.resolve()
    if set(value) != _TOP_LEVEL_FIELDS:
        raise FullAgentManifestError("Full Agent manifest fields mismatch")
    if value.get("schema_version") != FULL_AGENT_MANIFEST_SCHEMA:
        raise FullAgentManifestError("unsupported Full Agent manifest schema")
    if value.get("profile") != FULL_AGENT_PROFILE:
        raise FullAgentManifestError("unsupported Full Agent profile")
    if value.get("artifact_kind") != "runtime_full_agent_manifest":
        raise FullAgentManifestError("invalid Full Agent artifact kind")
    if value.get("runtime_loader") is not True:
        raise FullAgentManifestError("Full Agent runtime loader must be enabled")
    if (
        value.get("runtime_authority")
        != "EXPLICIT_MANIFEST_PLUS_MATCHING_RUNTIME_ARGUMENTS"
    ):
        raise FullAgentManifestError("Full Agent runtime authority mismatch")
    status = value.get("status")
    if status not in {FULL_AGENT_READY, FULL_AGENT_PENDING}:
        raise FullAgentManifestError("unsupported Full Agent manifest status")
    if require_ready and status != FULL_AGENT_READY:
        raise FullAgentManifestError(
            "Full Agent manifest is not READY; artifact bindings are incomplete"
        )

    _require_exact_mapping(
        value.get("token_policy"),
        {"mode": "fixed", "dynamic_allowed": False},
        label="token_policy",
    )
    components = value.get("components")
    if not isinstance(components, Mapping) or set(components) != {
        "A1 Structured Evidence Memory",
        "A2 Budget-Aware Continuation",
        "A3 Experience Strategy Advisor",
    }:
        raise FullAgentManifestError("Full Agent component fields mismatch")
    _require_exact_mapping(
        components["A1 Structured Evidence Memory"],
        {"mode": "on"},
        label="A1 Structured Evidence Memory",
    )
    _require_exact_mapping(
        components["A2 Budget-Aware Continuation"],
        {
            "mode": "enforce",
            "external_version": "v2",
            "implementation_version": CONTINUATION_POLICY_VERSION,
            "decision_schema": CONTINUATION_DECISION_SCHEMA_V2,
        },
        label="A2 Budget-Aware Continuation",
    )
    _require_exact_mapping(
        components["A3 Experience Strategy Advisor"],
        {
            "mode": "guided",
            "ranker_version": "v3",
            "implementation_schema": STRATEGY_RANKER_V3_SCHEMA,
        },
        label="A3 Experience Strategy Advisor",
    )
    _require_exact_mapping(
        value.get("foundations"),
        {
            "B1 Candidate and Safety Baseline": {
                "enabled": True,
                "ablatable": False,
            },
            "B2 Independent Final Certification": {
                "enabled": True,
                "ablatable": False,
                "budget_domain": "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET",
                "feedback_policy": "NO_SAME_RUN_AGENT_FEEDBACK",
            },
        },
        label="foundations",
    )

    artifact_root = _resolve_artifact_root(resolved, value.get("artifact_root"))
    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, Mapping) or set(raw_artifacts) != set(
        _ARTIFACT_ROLES
    ):
        raise FullAgentManifestError("Full Agent artifact roles mismatch")
    artifacts: dict[str, FullAgentArtifact] = {}
    for role in _ARTIFACT_ROLES:
        artifact = _artifact_binding(
            role=role,
            value=raw_artifacts[role],
            artifact_root=artifact_root,
            allow_pending=status == FULL_AGENT_PENDING,
        )
        if artifact is not None:
            artifacts[role] = artifact
    if status == FULL_AGENT_READY and set(artifacts) != set(_ARTIFACT_ROLES):
        raise FullAgentManifestError(
            "READY Full Agent manifest must bind every required artifact"
        )
    if status == FULL_AGENT_READY:
        _validate_ready_artifact_graph(artifacts)
    if status == FULL_AGENT_PENDING and len(artifacts) == len(_ARTIFACT_ROLES):
        raise FullAgentManifestError(
            "complete Full Agent artifact bindings must use READY status"
        )
    return FullAgentManifest(
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
        status=str(status),
        artifacts=MappingProxyType(artifacts),
    )


__all__ = [
    "FULL_AGENT_MANIFEST_SCHEMA",
    "FULL_AGENT_PENDING",
    "FULL_AGENT_PROFILE",
    "FULL_AGENT_READY",
    "FullAgentArtifact",
    "FullAgentManifest",
    "FullAgentManifestError",
    "load_full_agent_manifest",
]
