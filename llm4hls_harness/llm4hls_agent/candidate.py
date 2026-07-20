"""Atomic immutable Candidate materialization shared by V1 and V2."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .task import PublicTask
from .workflow import RunArtifactError, _atomic_json, _fsync_directory, _sha256


class PatchApplicationLike(Protocol):
    patched_sha256: str
    patched_bytes: bytes


@dataclass(frozen=True)
class CandidateMaterialization:
    candidate_id: str
    record: dict[str, object]


def _initial_validation() -> dict[str, dict[str, object]]:
    return {
        "csim": {"status": "NOT_RUN"},
        "synth": {"status": "NOT_RUN"},
        "cosim": {"status": "NOT_RUN"},
    }


class CandidateManager:
    """Own Candidate IDs, lineage, staging, and immutable source artifacts."""

    def __init__(self, run_root: str | Path, task: PublicTask) -> None:
        self.run_root = Path(run_root).resolve()
        self.task = task

    def load_registry(self) -> dict[str, object]:
        try:
            value = json.loads(
                (self.run_root / "candidate_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(value, dict) or not isinstance(
                value.get("candidates"), dict
            ):
                raise TypeError("candidate registry is not an object")
            return value
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
        ) as exc:
            raise RunArtifactError(f"cannot load candidate registry: {exc}") from exc

    @staticmethod
    def next_id(registry: Mapping[str, object]) -> str:
        candidates = registry.get("candidates", {})
        numbers = [
            int(match.group(1))
            for key in candidates
            if (match := re.fullmatch(r"candidate_(\d+)", str(key)))
        ] if isinstance(candidates, Mapping) else []
        return f"candidate_{(max(numbers) + 1) if numbers else 0:03d}"

    def save_registry(self, registry: Mapping[str, object]) -> None:
        _atomic_json(self.run_root / "candidate_registry.json", registry)

    def materialize(
        self,
        registry: dict[str, object],
        *,
        parent_id: str,
        patch_text: str,
        application: PatchApplicationLike,
        kind: str,
        metadata: Mapping[str, object],
    ) -> CandidateMaterialization:
        candidates = registry.get("candidates")
        if not isinstance(candidates, dict):
            raise RunArtifactError("candidate registry candidates is not an object")
        if parent_id not in candidates or not isinstance(candidates[parent_id], Mapping):
            raise ValueError(f"parent Candidate does not exist: {parent_id}")
        if not kind:
            raise ValueError("candidate kind must not be empty")

        patch_hash = _sha256(patch_text.encode("utf-8"))
        for candidate_id, value in candidates.items():
            if not isinstance(value, Mapping):
                continue
            if (
                value.get("patch_sha256") != patch_hash
                or value.get("parent_id") != parent_id
            ):
                continue
            source = self.run_root / str(value.get("source_ref", ""))
            if source.is_file() and source.read_bytes() == application.patched_bytes:
                return CandidateMaterialization(str(candidate_id), dict(value))

        candidate_id = self.next_id(registry)
        source_ref = f"candidates/{candidate_id}/source/{self.task.kernel_name}"
        patch_ref = f"candidates/{candidate_id}/patch.diff"
        record: dict[str, object] = {
            "candidate_id": candidate_id,
            "parent_id": parent_id,
            "kind": kind,
            "immutable": True,
            "source_ref": source_ref,
            "patch_ref": patch_ref,
            "patch_sha256": patch_hash,
            "code_hash": application.patched_sha256,
            "status": "MATERIALIZED",
            "validation": _initial_validation(),
            "metrics_ref": None,
            "credits_used": 0,
        }
        protected = set(record)
        overlap = protected.intersection(metadata)
        if overlap:
            raise ValueError(
                "candidate metadata overrides protected fields: "
                + ", ".join(sorted(overlap))
            )
        record.update({str(key): value for key, value in metadata.items()})

        candidate_root = self.run_root / "candidates" / candidate_id
        source_path = candidate_root / "source" / self.task.kernel_name
        patch_path = candidate_root / "patch.diff"
        metadata_path = candidate_root / "candidate.json"
        if candidate_root.exists():
            self._verify_orphan(
                candidate_id,
                record,
                source_path,
                patch_path,
                metadata_path,
                patch_text,
                application,
            )
        else:
            self._stage_candidate(
                candidate_id,
                candidate_root,
                record,
                patch_text,
                application,
            )

        candidates[candidate_id] = record
        registry["active_candidate_id"] = candidate_id
        self.save_registry(registry)
        return CandidateMaterialization(candidate_id, dict(record))

    @staticmethod
    def _verify_orphan(
        candidate_id: str,
        record: Mapping[str, object],
        source_path: Path,
        patch_path: Path,
        metadata_path: Path,
        patch_text: str,
        application: PatchApplicationLike,
    ) -> None:
        try:
            recovered = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunArtifactError(
                f"orphaned candidate {candidate_id} cannot be recovered: {exc}"
            ) from exc
        if (
            not isinstance(recovered, dict)
            or recovered != dict(record)
            or not source_path.is_file()
            or source_path.read_bytes() != application.patched_bytes
            or not patch_path.is_file()
            or patch_path.read_text(encoding="utf-8") != patch_text
        ):
            raise RunArtifactError(
                f"orphaned candidate {candidate_id} is inconsistent"
            )

    def _stage_candidate(
        self,
        candidate_id: str,
        candidate_root: Path,
        record: Mapping[str, object],
        patch_text: str,
        application: PatchApplicationLike,
    ) -> None:
        staging_parent = self.run_root / ".candidate_staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix=f"{candidate_id}.", dir=staging_parent)
        )
        try:
            staged_source = staging_root / "source" / self.task.kernel_name
            staged_patch = staging_root / "patch.diff"
            staged_metadata = staging_root / "candidate.json"
            staged_source.parent.mkdir(parents=True, exist_ok=False)
            with staged_source.open("wb") as stream:
                stream.write(application.patched_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            with staged_patch.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(patch_text)
                stream.flush()
                os.fsync(stream.fileno())
            _atomic_json(staged_metadata, record)
            for path in (staged_source, staged_patch, staged_metadata):
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            candidate_root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_root, candidate_root)
            _fsync_directory(candidate_root.parent)
        except Exception:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        finally:
            try:
                staging_parent.rmdir()
            except OSError:
                pass
