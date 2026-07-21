"""Append-only v2 knowledge-base storage and explainable deterministic retrieval."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import canonical_json, canonical_sha256
from .v3_experience_v2 import (
    ALGORITHM_FAMILIES,
    BOTTLENECK_SUBTYPES_BY_MODE,
    FAILURE_SUBTYPES_BY_MODE,
    MODES,
    TASK_SPLITS_V2,
    v2_identity_key,
    validate_experience_v2,
)


KB_SNAPSHOT_SCHEMA = "v3e.experience-kb-snapshot.v1"
KB_INDEX_SCHEMA = "v3e.experience-kb-index.v1"
KB_QUERY_SCHEMA = "v3e.experience-query.v2"
KB_RETRIEVAL_SCHEMA = "v3e.explainable-retrieval.v1"
QUARANTINE_SCHEMA = "v3e.experience-quarantine.v1"
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class ExperienceKnowledgeBaseError(RuntimeError):
    """The derived knowledge base violates an immutable storage invariant."""


@dataclass(frozen=True)
class KBSnapshot:
    byte_offset: int
    prefix_sha256: str
    record_count: int
    index_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": KB_SNAPSHOT_SCHEMA,
            "byte_offset": self.byte_offset,
            "prefix_sha256": self.prefix_sha256,
            "record_count": self.record_count,
            "index_sha256": self.index_sha256,
        }


@dataclass(frozen=True)
class KBPutResult:
    inserted: bool
    record_id: str
    byte_offset: int


@dataclass(frozen=True)
class RetrievalCase:
    record: dict[str, object]
    explanation: dict[str, object]


@dataclass(frozen=True)
class ExplainableRetrievalResult:
    successes: tuple[RetrievalCase, ...]
    failures: tuple[RetrievalCase, ...]
    considered: tuple[RetrievalCase, ...]
    filtered_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        def summary(case: RetrievalCase) -> dict[str, object]:
            record = case.record
            return {
                "record_id": record["record_id"],
                **case.explanation,
            }

        return {
            "schema_version": KB_RETRIEVAL_SCHEMA,
            "successes": [summary(item) for item in self.successes],
            "failures": [summary(item) for item in self.failures],
            "considered": [summary(item) for item in self.considered],
            "filtered_counts": dict(sorted(self.filtered_counts.items())),
        }


def _complete_prefix(data: bytes) -> bytes:
    if not data:
        return b""
    newline = data.rfind(b"\n")
    return b"" if newline < 0 else data[: newline + 1]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _decode_v2(prefix: bytes) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line_number, raw in enumerate(prefix.splitlines(), start=1):
        try:
            value = json.loads(raw)
            if not isinstance(value, Mapping):
                raise ValueError("record is not an object")
            records.append(validate_experience_v2(value))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ExperienceKnowledgeBaseError(
                f"invalid v2 record at committed line {line_number}: {type(exc).__name__}"
            ) from exc
    return tuple(records)


def _index_values(record: Mapping[str, object]) -> dict[str, list[str]]:
    source = record["source"]
    problem = record["problem"]
    strategy = record["strategy"]
    assert isinstance(source, Mapping) and isinstance(problem, Mapping) and isinstance(strategy, Mapping)
    subtype = (
        problem["bottleneck_subtype"]
        if problem["mode"] == "OPTIMIZE"
        else problem["failure_subtype"]
    )
    return {
        "mode": [str(problem["mode"])],
        "subtype": [str(subtype)],
        "strategy": [str(item) for item in strategy["observed_strategy_atoms"]],
        "algorithm_family": [str(source["algorithm_family"])],
        "task_family": [str(source["task_family_hash"])],
        "provider": [str(source["provider"])],
        "toolchain": [str(source["toolchain"])],
    }


def build_secondary_index(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    dimensions: dict[str, dict[str, list[str]]] = {
        name: {}
        for name in (
            "mode",
            "subtype",
            "strategy",
            "algorithm_family",
            "task_family",
            "provider",
            "toolchain",
        )
    }
    for raw in sorted(records, key=lambda item: str(item["record_id"])):
        record = validate_experience_v2(raw)
        for dimension, values in _index_values(record).items():
            for value in values:
                dimensions[dimension].setdefault(value, []).append(str(record["record_id"]))
    return {
        "schema_version": KB_INDEX_SCHEMA,
        "record_count": len(records),
        "dimensions": {
            name: {key: sorted(set(ids)) for key, ids in sorted(values.items())}
            for name, values in sorted(dimensions.items())
        },
    }


class ExperienceKnowledgeBase:
    """Directory-backed v2 store; raw v1 artifacts are never rewritten."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.raw_dir = self.root / "raw"
        self.derived_dir = self.root / "derived"
        self.snapshot_dir = self.root / "snapshots"
        self.index_dir = self.root / "indexes"
        self.quarantine_dir = self.root / "quarantine"
        self.report_dir = self.root / "reports"
        self.store_path = self.derived_dir / "experience_v2.jsonl"
        self.index_path = self.index_dir / "secondary_index.json"
        self.quarantine_path = self.quarantine_dir / "quarantine.jsonl"

    def _prefix(self) -> bytes:
        if not self.store_path.is_file():
            return b""
        with self.store_path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _complete_prefix(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def records(self, snapshot: KBSnapshot | None = None) -> tuple[dict[str, object], ...]:
        prefix = self._prefix()
        if snapshot is not None:
            if len(prefix) < snapshot.byte_offset:
                raise ExperienceKnowledgeBaseError("v2 store is shorter than frozen snapshot")
            prefix = prefix[: snapshot.byte_offset]
            if hashlib.sha256(prefix).hexdigest() != snapshot.prefix_sha256:
                raise ExperienceKnowledgeBaseError("v2 snapshot prefix hash changed")
        records = _decode_v2(prefix)
        if snapshot is not None and len(records) != snapshot.record_count:
            raise ExperienceKnowledgeBaseError("v2 snapshot record count changed")
        return records

    def put_if_absent(self, raw: Mapping[str, object]) -> KBPutResult:
        record = validate_experience_v2(raw)
        encoded = canonical_json(record)
        identity = v2_identity_key(record)
        self.derived_dir.mkdir(parents=True, exist_ok=True)
        with self.store_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                data = handle.read()
                prefix = _complete_prefix(data)
                existing = _decode_v2(prefix)
                if len(data) != len(prefix):
                    handle.seek(len(prefix))
                    handle.truncate()
                for item in existing:
                    if item["record_id"] == record["record_id"]:
                        if canonical_json(item) != encoded:
                            raise ExperienceKnowledgeBaseError("v2 record-id collision")
                        return KBPutResult(False, str(record["record_id"]), len(prefix))
                    if v2_identity_key(item) == identity:
                        raise ExperienceKnowledgeBaseError(
                            "run/candidate/round already has different derived content"
                        )
                handle.seek(0, os.SEEK_END)
                handle.write(encoded + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                offset = handle.tell()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return KBPutResult(True, str(record["record_id"]), offset)

    def rebuild_index(self, snapshot: KBSnapshot | None = None) -> dict[str, object]:
        records = self.records(snapshot)
        index = build_secondary_index(records)
        _atomic_write(self.index_path, canonical_json(index) + b"\n")
        return index

    def snapshot(self, *, persist: bool = True) -> KBSnapshot:
        prefix = self._prefix()
        records = _decode_v2(prefix)
        index = build_secondary_index(records)
        index_bytes = canonical_json(index)
        snapshot = KBSnapshot(
            len(prefix),
            hashlib.sha256(prefix).hexdigest(),
            len(records),
            hashlib.sha256(index_bytes).hexdigest(),
        )
        if persist:
            _atomic_write(self.index_path, index_bytes + b"\n")
            path = self.snapshot_dir / f"{snapshot.prefix_sha256}.json"
            _atomic_write(path, canonical_json(snapshot.to_dict()) + b"\n")
        return snapshot

    def eligible_records(
        self, snapshot: KBSnapshot | None = None, *, ranking: bool = False
    ) -> tuple[dict[str, object], ...]:
        field = "eligible_for_ranking" if ranking else "eligible_for_retrieval"
        return tuple(
            item
            for item in self.records(snapshot)
            if item["source"]["evidence_level"] == "REAL_LLM_VITIS"
            and item["provenance"][field] is True
        )

    def quarantine(
        self,
        *,
        source_digest: str,
        reason: str,
        line_number: int | None = None,
    ) -> str:
        entry: dict[str, object] = {
            "schema_version": QUARANTINE_SCHEMA,
            "quarantine_id": canonical_sha256(
                {"source_digest": source_digest, "reason": reason, "line_number": line_number}
            ),
            "source_digest": source_digest,
            "reason": reason,
            "line_number": line_number,
        }
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        encoded = canonical_json(entry)
        with self.quarantine_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = handle.read().splitlines()
                if encoded not in existing:
                    handle.seek(0, os.SEEK_END)
                    handle.write(encoded + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return str(entry["quarantine_id"])

    def validate_external_jsonl(self, path: str | Path) -> dict[str, object]:
        """Validate without modifying the external file; quarantine only digests."""

        source = Path(path)
        valid = 0
        quarantined = 0
        seen_identity: dict[tuple[str, str, int], str] = {}
        for line_number, raw in enumerate(source.read_bytes().splitlines(), start=1):
            digest = hashlib.sha256(raw).hexdigest()
            try:
                value = json.loads(raw)
                if not isinstance(value, Mapping):
                    raise ValueError("JSON_NOT_OBJECT")
                record = validate_experience_v2(value)
                identity = v2_identity_key(record)
                previous = seen_identity.get(identity)
                if previous and previous != record["record_id"]:
                    raise ValueError("DUPLICATE_IDENTITY_CONFLICT")
                seen_identity[identity] = str(record["record_id"])
                valid += 1
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                quarantined += 1
                self.quarantine(
                    source_digest=digest,
                    reason=str(exc)[:160] or type(exc).__name__,
                    line_number=line_number,
                )
        return {
            "valid": valid,
            "quarantined": quarantined,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }


_QUERY_FIELDS = {
    "schema_version",
    "query_id",
    "mode",
    "failure_subtype",
    "bottleneck_subtype",
    "algorithm_family",
    "task_family_hash",
    "structure_features",
    "strategy_context",
    "task_split",
    "current_run_id",
    "current_candidate_id",
    "current_patch_digest",
    "toolchain",
    "exclude_same_task_family",
}


def build_kb_query(
    *,
    mode: str,
    task_split: str,
    failure_subtype: str = "UNKNOWN",
    bottleneck_subtype: str = "UNKNOWN",
    algorithm_family: str = "OTHER",
    task_family_hash: str = "0" * 64,
    structure_features: Mapping[str, object] | None = None,
    strategy_context: Sequence[str] = (),
    current_run_id: str | None = None,
    current_candidate_id: str | None = None,
    current_patch_digest: str | None = None,
    toolchain: str = "Vitis 2025.2",
    exclude_same_task_family: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": KB_QUERY_SCHEMA,
        "query_id": "",
        "mode": mode,
        "failure_subtype": failure_subtype,
        "bottleneck_subtype": bottleneck_subtype,
        "algorithm_family": algorithm_family,
        "task_family_hash": task_family_hash,
        "structure_features": dict(structure_features or {}),
        "strategy_context": list(strategy_context),
        "task_split": task_split,
        "current_run_id": current_run_id,
        "current_candidate_id": current_candidate_id,
        "current_patch_digest": current_patch_digest,
        "toolchain": toolchain,
        "exclude_same_task_family": exclude_same_task_family,
    }
    value["query_id"] = canonical_sha256({**value, "query_id": ""})
    return validate_kb_query(value)


def validate_kb_query(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) != _QUERY_FIELDS or value.get("schema_version") != KB_QUERY_SCHEMA:
        raise ValueError("knowledge-base query fields mismatch")
    mode = value.get("mode")
    if mode not in MODES:
        raise ValueError("query mode is invalid")
    if value.get("task_split") not in TASK_SPLITS_V2:
        raise ValueError("query task split is invalid")
    if value.get("failure_subtype") not in FAILURE_SUBTYPES_BY_MODE[str(mode)]:
        raise ValueError("query failure subtype is invalid")
    if value.get("bottleneck_subtype") not in BOTTLENECK_SUBTYPES_BY_MODE[str(mode)]:
        raise ValueError("query bottleneck subtype is invalid")
    if value.get("algorithm_family") not in ALGORITHM_FAMILIES:
        raise ValueError("query algorithm family is invalid")
    for name in ("task_family_hash", "query_id"):
        item = value.get(name)
        if not isinstance(item, str) or len(item) != 64 or any(c not in "0123456789abcdef" for c in item):
            raise ValueError(f"query {name} is invalid")
    if not isinstance(value.get("structure_features"), Mapping):
        raise ValueError("query structure features must be an object")
    if not isinstance(value.get("strategy_context"), list):
        raise ValueError("query strategy context must be a list")
    if not isinstance(value.get("exclude_same_task_family"), bool):
        raise ValueError("query family exclusion must be boolean")
    expected = canonical_sha256({**dict(value), "query_id": ""})
    if value.get("query_id") != expected:
        raise ValueError("query_id does not bind query content")
    return json.loads(canonical_json(value).decode("utf-8"))


def _is_success(record: Mapping[str, object]) -> bool:
    problem = record["problem"]
    validation = record["validation"]
    performance = record["performance"]
    assert isinstance(problem, Mapping) and isinstance(validation, Mapping) and isinstance(performance, Mapping)
    mode = problem["mode"]
    if mode == "OPTIMIZE":
        return bool(
            validation["csim_status"] == "PASS"
            and validation["synth_status"] == "PASS"
            and performance["strict_improvement"] is True
            and validation["fresh_final_status"] == "PASS"
        )
    return validation["fresh_final_status"] == "PASS"


def _bucket_similarity(left: object, right: object) -> float | None:
    order = ["0", "1", "2-4", "5-16", "17-64", "65-256", "257-1024", "1024+"]
    if left not in order or right not in order:
        return None
    distance = abs(order.index(str(left)) - order.index(str(right)))
    return max(0.0, 1.0 - 0.25 * distance)


class ExplainableSimilarCaseRetriever:
    def __init__(
        self,
        *,
        min_similarity: float = 0.35,
        max_successes: int = 3,
        max_failures: int = 2,
    ) -> None:
        self.min_similarity = float(min_similarity)
        self.max_successes = max_successes
        self.max_failures = max_failures

    def _similarity(
        self, query: Mapping[str, object], record: Mapping[str, object]
    ) -> tuple[float, list[str], list[str]]:
        source = record["source"]
        problem = record["problem"]
        structure = record["structure_features"]
        strategy = record["strategy"]
        assert all(isinstance(item, Mapping) for item in (source, problem, structure, strategy))
        matched: list[str] = []
        mismatched: list[str] = []
        weighted = 0.0
        total = 0.0

        def compare(name: str, left: object, right: object, weight: float) -> None:
            nonlocal weighted, total
            if left in (None, "UNKNOWN") or right in (None, "UNKNOWN"):
                return
            total += weight
            if left == right:
                weighted += weight
                matched.append(name)
            else:
                mismatched.append(name)

        subtype_name = "bottleneck_subtype" if query["mode"] == "OPTIMIZE" else "failure_subtype"
        compare(subtype_name, query[subtype_name], problem[subtype_name], 6.0)
        compare("algorithm_family", query["algorithm_family"], source["algorithm_family"], 2.0)
        compare("requires_cosim", query["structure_features"].get("requires_cosim"), problem["requires_cosim"], 1.5)
        compare("toolchain", query["toolchain"], source["toolchain"], 1.0)
        for name, weight in (
            ("has_dataflow", 1.5),
            ("has_stream", 1.5),
            ("has_fifo", 1.0),
            ("has_reduction", 1.0),
            ("critical_loop_ii", 1.5),
            ("memory_access_pattern", 1.0),
            ("patch_complexity", 0.5),
        ):
            left = query["structure_features"].get(name)
            right = strategy.get(name) if name == "patch_complexity" else structure.get(name)
            compare(name, left, right, weight)
        for name, weight in (
            ("critical_loop_trip_count_bucket", 1.0),
            ("transaction_interval_bucket", 1.0),
            ("latency_bucket", 0.5),
        ):
            score = _bucket_similarity(query["structure_features"].get(name), structure.get(name))
            if score is not None:
                total += weight
                weighted += weight * score
                (matched if score == 1 else mismatched).append(name)
        context = set(query["strategy_context"])
        atoms = set(strategy["observed_strategy_atoms"])
        if context:
            total += 1.0
            if context.intersection(atoms):
                weighted += 1.0
                matched.append("strategy_context")
            else:
                mismatched.append("strategy_context")
        return (round(weighted / total, 8) if total else 0.0), matched, mismatched

    def retrieve(
        self,
        query: Mapping[str, object],
        records: Sequence[Mapping[str, object]],
    ) -> ExplainableRetrievalResult:
        validated_query = validate_kb_query(query)
        filtered: dict[str, int] = {}

        def reject(reason: str) -> None:
            filtered[reason] = filtered.get(reason, 0) + 1

        candidates: list[RetrievalCase] = []
        for raw in sorted(records, key=lambda item: str(item.get("record_id", ""))):
            record = validate_experience_v2(raw)
            source = record["source"]
            problem = record["problem"]
            strategy = record["strategy"]
            provenance = record["provenance"]
            if source["evidence_level"] != "REAL_LLM_VITIS":
                reject("NON_REAL_EVIDENCE")
                continue
            if provenance["eligible_for_retrieval"] is not True:
                reject("INELIGIBLE_FOR_RETRIEVAL")
                continue
            if source["task_split"] not in {"train", "dev"}:
                reject("UNSUPPORTED_EXPERIENCE_SPLIT")
                continue
            if validated_query["task_split"] == "hidden_like" and source["task_split"] != "train":
                reject("HIDDEN_QUERY_TRAIN_ONLY")
                continue
            if problem["mode"] != validated_query["mode"]:
                reject("MODE_MISMATCH")
                continue
            if source["run_id"] == validated_query["current_run_id"]:
                reject("CURRENT_RUN")
                continue
            if (
                validated_query["current_candidate_id"]
                and source["candidate_id"] == validated_query["current_candidate_id"]
            ):
                reject("CURRENT_CANDIDATE")
                continue
            if (
                validated_query["current_patch_digest"]
                and strategy["patch_digest"] == validated_query["current_patch_digest"]
            ):
                reject("DUPLICATE_PATCH")
                continue
            same_family = source["task_family_hash"] == validated_query["task_family_hash"]
            if validated_query["exclude_same_task_family"] and same_family:
                reject("SAME_TASK_FAMILY_EXCLUDED")
                continue
            score, matched, mismatched = self._similarity(validated_query, record)
            if score < self.min_similarity:
                reject("SIMILARITY_BELOW_THRESHOLD")
                continue
            candidates.append(
                RetrievalCase(
                    record,
                    {
                        "similarity": score,
                        "matched_features": matched,
                        "mismatched_features": mismatched,
                        "family_relation": "SAME" if same_family else "DIFFERENT",
                        "eligibility_reasons": ["REAL_LLM_VITIS", "ELIGIBLE", "SPLIT_ALLOWED", "MODE_MATCH"],
                    },
                )
            )
        candidates.sort(
            key=lambda item: (-float(item.explanation["similarity"]), str(item.record["record_id"]))
        )
        successes = tuple(item for item in candidates if _is_success(item.record))[: self.max_successes]
        failures = tuple(item for item in candidates if not _is_success(item.record))[: self.max_failures]
        selected_ids = {item.record["record_id"] for item in (*successes, *failures)}
        considered = tuple(item for item in candidates if item.record["record_id"] in selected_ids)
        return ExplainableRetrievalResult(successes, failures, considered, filtered)


__all__ = [
    "ExperienceKnowledgeBase",
    "ExperienceKnowledgeBaseError",
    "ExplainableRetrievalResult",
    "ExplainableSimilarCaseRetriever",
    "KBSnapshot",
    "build_kb_query",
    "build_secondary_index",
    "validate_kb_query",
]
