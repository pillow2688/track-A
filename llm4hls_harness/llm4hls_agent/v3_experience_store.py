"""Crash-tolerant append-only storage for V3-E candidate experience.

The store deliberately exposes immutable prefix snapshots.  A recommendation
therefore sees one frozen history even when another process appends experience
while a Planner round is in flight.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .v3_experience import (
    ExperienceSnapshot,
    PutResult,
    canonical_json,
    canonical_sha256,
    validate_experience_record,
)


class ExperienceStoreError(RuntimeError):
    """The JSONL history is corrupt or violates an idempotency invariant."""


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _complete_prefix(data: bytes) -> bytes:
    """Return only newline-committed JSONL bytes.

    A killed writer can leave an incomplete final line.  Readers ignore that
    suffix; the next exclusive writer validates and truncates it before append.
    """

    if not data:
        return b""
    newline = data.rfind(b"\n")
    return b"" if newline < 0 else data[: newline + 1]


def _decode_prefix(prefix: bytes) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line_number, raw in enumerate(prefix.splitlines(), start=1):
        if not raw.strip():
            raise ExperienceStoreError(
                f"experience JSONL contains a blank committed line at {line_number}"
            )
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperienceStoreError(
                f"invalid committed experience JSON at line {line_number}"
            ) from exc
        if not isinstance(value, Mapping):
            raise ExperienceStoreError(
                f"experience JSON at line {line_number} is not an object"
            )
        try:
            records.append(validate_experience_record(value))
        except ValueError as exc:
            raise ExperienceStoreError(
                f"invalid experience record at line {line_number}: {exc}"
            ) from exc
    return tuple(records)


class JsonlExperienceRepository:
    """Append-only repository with exact-id and trajectory-revision deduplication."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _snapshot_from_prefix(self, prefix: bytes) -> ExperienceSnapshot:
        records = _decode_prefix(prefix)
        return ExperienceSnapshot(
            byte_offset=len(prefix),
            prefix_sha256=hashlib.sha256(prefix).hexdigest(),
            record_count=len(records),
        )

    def snapshot(self) -> ExperienceSnapshot:
        if not self._path.exists():
            return ExperienceSnapshot(0, _EMPTY_SHA256, 0)
        with self._path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                prefix = _complete_prefix(handle.read())
                return self._snapshot_from_prefix(prefix)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def records(
        self, snapshot: ExperienceSnapshot | None = None
    ) -> tuple[dict[str, object], ...]:
        frozen = snapshot or self.snapshot()
        if frozen.byte_offset == 0:
            if frozen.prefix_sha256 != _EMPTY_SHA256 or frozen.record_count != 0:
                raise ExperienceStoreError("invalid empty experience snapshot")
            return ()
        if not self._path.exists():
            raise ExperienceStoreError("experience store vanished after snapshot")
        with self._path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                prefix = handle.read(frozen.byte_offset)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if len(prefix) != frozen.byte_offset:
            raise ExperienceStoreError("experience store is shorter than frozen snapshot")
        if hashlib.sha256(prefix).hexdigest() != frozen.prefix_sha256:
            raise ExperienceStoreError("experience snapshot prefix hash changed")
        records = _decode_prefix(prefix)
        if len(records) != frozen.record_count:
            raise ExperienceStoreError("experience snapshot record count changed")
        return records

    def put_if_absent(self, record: Mapping[str, object]) -> PutResult:
        validated = validate_experience_record(record)
        encoded = canonical_json(validated)
        record_sha = hashlib.sha256(encoded).hexdigest()
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # a+b creates atomically enough for flock to serialize all writers.
        with self._path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                data = handle.read()
                prefix = _complete_prefix(data)
                existing = _decode_prefix(prefix)
                if len(prefix) != len(data):
                    handle.seek(len(prefix))
                    handle.truncate()

                for item in existing:
                    if item["record_id"] == validated["record_id"]:
                        if canonical_json(item) != encoded:
                            raise ExperienceStoreError(
                                "record_id collision with different experience content"
                            )
                        return PutResult(
                            inserted=False,
                            record_id=str(validated["record_id"]),
                            record_sha256=record_sha,
                            byte_offset=len(prefix),
                        )
                    if (
                        item["trajectory_id"] == validated["trajectory_id"]
                        and item["revision"] == validated["revision"]
                    ):
                        raise ExperienceStoreError(
                            "trajectory revision already exists under another record_id"
                        )

                handle.seek(0, os.SEEK_END)
                handle.write(encoded + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                byte_offset = handle.tell()
                return PutResult(
                    inserted=True,
                    record_id=str(validated["record_id"]),
                    record_sha256=record_sha,
                    byte_offset=byte_offset,
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def latest_trajectories(
        self,
        snapshot: ExperienceSnapshot | None = None,
        *,
        ranking_only: bool = True,
    ) -> tuple[dict[str, object], ...]:
        latest: dict[str, dict[str, object]] = {}
        for record in self.records(snapshot):
            trajectory = str(record["trajectory_id"])
            previous = latest.get(trajectory)
            if previous is None or int(record["revision"]) > int(previous["revision"]):
                latest[trajectory] = record
            elif int(record["revision"]) == int(previous["revision"]):
                if canonical_json(record) != canonical_json(previous):
                    raise ExperienceStoreError(
                        "conflicting latest records for one trajectory revision"
                    )
        selected = {
            key: record
            for key, record in latest.items()
            if not ranking_only
            or (
                record["eligible_for_ranking"]
                and record["execution_class"] == "REAL_LLM_VITIS"
            )
        }
        return tuple(
            selected[key]
            for key in sorted(
                selected,
                key=lambda item: (
                    str(selected[item]["mode"]),
                    str(selected[item]["record_id"]),
                ),
            )
        )

    def fingerprint(self, snapshot: ExperienceSnapshot | None = None) -> dict[str, object]:
        """Return path-free metadata suitable for checkpoint/Planner audit."""

        frozen = snapshot or self.snapshot()
        # Fail closed if a frozen seed was rewritten after coordinator startup.
        self.records(frozen)
        return {
            "schema_version": frozen.to_dict()["schema_version"],
            "prefix_sha256": frozen.prefix_sha256,
            "record_count": frozen.record_count,
            "byte_offset": frozen.byte_offset,
        }


__all__ = ["ExperienceStoreError", "JsonlExperienceRepository"]
