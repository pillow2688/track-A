"""Sanitized, idempotent importer for historical V3 Candidate experience.

Only bounded public features cross the import boundary.  Source text, Patch
text, Planner prompts/responses, logs, task names and local paths are read only
long enough to derive hashes or fixed taxonomies and are never retained in an
experience record or generated report.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .v3_experience import (
    EXPERIENCE_SCHEMA,
    EXECUTION_CLASSES,
    PHASE_MODES,
    TASK_SPLITS,
    ExperienceRepository,
    canonical_json,
    changed_patch_lines,
    normalize_strategy_bundle,
    normalized_patch_hash,
    numeric_bucket,
    opaque_hash,
    validate_experience_record,
)


IMPORT_REPORT_SCHEMA = "v3e.experience-import-report.v1"
EXPERIENCE_STATS_SCHEMA = "v3e.experience-stats.v1"
DATA_QUALITY_REPORT_SCHEMA = "v3e.experience-data-quality.v1"

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ROUND = re.compile(r"proposal_(\d+)\.json\Z")
_FORBIDDEN_ARTIFACT_PARTS = frozenset(
    {"answer", "golden", "hidden", "hidden_like", "reference"}
)
_VITIS_UNKNOWN_SENTINEL = 1 << 28
_TERMINAL_RUN_STATUSES = frozenset({"DONE", "FAILED"})
_FIXED_FAILURE_STAGES = frozenset(
    {
        "PATCH_POLICY_REJECTED",
        "DUPLICATE_STRATEGY_BUNDLE",
        "CSIM",
        "SYNTH",
        "COSIM",
        "FINAL_CSIM",
        "FINAL_SYNTH",
        "FINAL_COSIM",
        "CANDIDATE_REJECTED",
        "UNKNOWN_OR_UNBOUNDED",
    }
)


class HistoricalImportError(ValueError):
    """A historical source cannot safely satisfy the V3-E contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImportPolicy:
    """Explicit metadata and fixture policy for a historical import.

    ``unspecified`` is intentionally the default split.  In particular, the
    importer never guesses that historical data is safe for a hidden-like
    query; callers must explicitly label trusted training history as ``train``.
    """

    task_split: str = "unspecified"
    algorithm_family: str = "unspecified"
    split_overrides: Mapping[str, str] = field(default_factory=dict)
    family_overrides: Mapping[str, str] = field(default_factory=dict)
    include_fixtures: bool = False
    require_terminal: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.task_split, str) or self.task_split not in TASK_SPLITS:
            raise ValueError("task_split is invalid")
        for split in self.split_overrides.values():
            if not isinstance(split, str) or split not in TASK_SPLITS:
                raise ValueError("split override is invalid")
        if (
            not isinstance(self.algorithm_family, str)
            or not self.algorithm_family
            or len(self.algorithm_family) > 320
        ):
            raise ValueError("algorithm_family is invalid")
        for family in self.family_overrides.values():
            if not isinstance(family, str) or not family or len(family) > 320:
                raise ValueError("family override is invalid")


@dataclass(frozen=True)
class HistoricalSource:
    """One discovered result marker; its path never enters exported data."""

    kind: str
    path: Path


@dataclass(frozen=True)
class SourceAudit:
    source_id: str
    source_kind: str
    execution_class: str | None
    disposition: str
    reason: str | None
    record_count: int = 0
    candidate_count: int = 0
    proposal_count: int = 0
    unbounded_metric_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "execution_class": self.execution_class,
            "disposition": self.disposition,
            "reason": self.reason,
            "record_count": self.record_count,
            "candidate_count": self.candidate_count,
            "proposal_count": self.proposal_count,
            "unbounded_metric_count": self.unbounded_metric_count,
        }


@dataclass(frozen=True)
class HistoricalImportResult:
    records: tuple[dict[str, object], ...]
    source_audits: tuple[SourceAudit, ...]
    inserted_record_ids: tuple[str, ...] = ()
    duplicate_record_ids: tuple[str, ...] = ()
    repository_record_count: int | None = None


@dataclass(frozen=True)
class _RunExtraction:
    records: tuple[dict[str, object], ...]
    audit: SourceAudit


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalImportError("MALFORMED_JSON") from exc
    if not isinstance(value, Mapping):
        raise HistoricalImportError("JSON_NOT_OBJECT")
    return dict(value)


def _source_id(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError:
        payload = path.name.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identity_hash(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_digest(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _infer_algorithm_family(source: str) -> str:
    """Fixed public-source classifier; task identity never participates."""

    text = source.casefold()
    rules = (
        ("CONVOLUTION_STENCIL", ("convolution", "stencil", "kernel window")),
        ("MATRIX_ALGEBRA", ("matrix", "matmul", "gemm")),
        ("DOT_REDUCTION", ("dot product", "dotproduct", "accumulate", "reduction")),
        ("STREAM_PIPELINE", ("hls::stream", "dataflow", "fifo")),
        ("SORT_SEARCH", ("sort", "search", "compare-and-swap")),
        ("CRYPTO_HASH", ("aes", "sha", "cipher", "encrypt")),
        ("VECTOR_ELEMENTWISE", ("vector", "elementwise", "element-wise")),
    )
    for family, tokens in rules:
        if any(token in text for token in tokens):
            return family
    if re.search(r"\b(?:sum|acc|total)\s*\+=", text):
        return "DOT_REDUCTION"
    return "GENERIC_HLS"


def _task_split_for(
    policy: ImportPolicy,
    task_id: str,
    task: Mapping[str, object],
    manifest: Mapping[str, object],
) -> str:
    if task_id in policy.split_overrides:
        return policy.split_overrides[task_id]
    if policy.task_split != "unspecified":
        return policy.task_split
    for value in (task.get("task_split"), manifest.get("task_split")):
        if isinstance(value, str) and value in TASK_SPLITS:
            return value
    return "unspecified"


def _family_for(
    policy: ImportPolicy,
    task_id: str,
    task: Mapping[str, object],
    manifest: Mapping[str, object],
    *,
    source: str = "",
) -> str:
    if task_id in policy.family_overrides:
        return policy.family_overrides[task_id]
    if policy.algorithm_family != "unspecified":
        return policy.algorithm_family
    for value in (
        task.get("algorithm_family"),
        manifest.get("algorithm_family"),
    ):
        normalized = _text(value)
        if normalized:
            return normalized
    return _infer_algorithm_family(source)


def discover_historical_sources(
    roots: Sequence[str | Path],
) -> tuple[HistoricalSource, ...]:
    """Discover supported V3 results and explicitly excluded fixture results."""

    markers = (
        ("V3_TERMINAL", "v3_prototype_result.json"),
        ("ORACLE_RESULT", "oracle_result.json"),
        ("DETERMINISTIC_EXECUTOR", "executor_result.json"),
    )
    discovered: dict[tuple[str, str], HistoricalSource] = {}
    for raw_root in roots:
        root = Path(raw_root)
        if root.is_file():
            for kind, filename in markers:
                if root.name == filename:
                    discovered[(kind, str(root.resolve()))] = HistoricalSource(kind, root)
                    break
            continue
        if not root.is_dir():
            continue
        for kind, filename in markers:
            for path in root.rglob(filename):
                key = (kind, str(path.resolve()))
                discovered[key] = HistoricalSource(kind, path)
    return tuple(discovered[key] for key in sorted(discovered))


def _fixture_audit(source: HistoricalSource) -> SourceAudit:
    execution = (
        "ORACLE_FIXTURE"
        if source.kind == "ORACLE_RESULT"
        else "DETERMINISTIC_FIXTURE"
    )
    reason = (
        "ORACLE_GOLDEN_NOT_AGENT_EXPERIENCE"
        if source.kind == "ORACLE_RESULT"
        else "DETERMINISTIC_EXECUTOR_NOT_AGENT_EXPERIENCE"
    )
    return SourceAudit(
        source_id=_source_id(source.path),
        source_kind=source.kind,
        execution_class=execution,
        disposition="EXCLUDED",
        reason=reason,
    )


def _load_optional_object(path: Path) -> dict[str, object]:
    return _read_json_object(path) if path.is_file() else {}


def _proposal_paths(run_dir: Path) -> tuple[Path, ...]:
    planner = run_dir / "planner"
    if not planner.is_dir():
        return ()

    def key(path: Path) -> tuple[int, str]:
        match = _ROUND.fullmatch(path.name)
        return (int(match.group(1)) if match else 10**9, path.name)

    return tuple(sorted(planner.glob("proposal_*.json"), key=key))


def _round_index(path: Path, value: Mapping[str, object]) -> int:
    raw = value.get("round_index")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    match = _ROUND.fullmatch(path.name)
    if match:
        return int(match.group(1))
    raise HistoricalImportError("PROPOSAL_ROUND_MISSING")


def _has_oracle_marker(value: object) -> bool:
    """Recognize oracle/golden structure without relying on directory names."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key == "golden":
                return True
            if normalized_key == "schema_version" and str(item).casefold().startswith(
                "v3d.oracle"
            ):
                return True
            if _has_oracle_marker(item):
                return True
    elif isinstance(value, list):
        return any(_has_oracle_marker(item) for item in value)
    return False


def _execution_class(
    result: Mapping[str, object],
    config: Mapping[str, object],
    proposals: Sequence[Mapping[str, object]],
) -> str | None:
    backend = result.get("backend") if isinstance(result.get("backend"), Mapping) else {}
    planner_contract = (
        result.get("planner_contract")
        if isinstance(result.get("planner_contract"), Mapping)
        else {}
    )
    backend_class = str(backend.get("class", "")).casefold()
    evidence_level = str(backend.get("evidence_level", "")).upper()
    contract_mode = str(planner_contract.get("mode", "")).casefold()
    replay = str(config.get("live_planner_replay_policy", "")).upper()
    providers = {
        provider
        for item in proposals
        if (provider := _text(item.get("provider")).casefold())
    }
    models = {
        model
        for item in proposals
        if (model := _text(item.get("model")).casefold())
    }

    schema_markers = {
        str(result.get("result_schema", "")).casefold(),
        str(result.get("schema_version", "")).casefold(),
    }
    if (
        any("oracle" in marker for marker in schema_markers)
        or _has_oracle_marker(result)
        or any("oracle" in provider or "golden" in provider for provider in providers)
        or any("oracle" in model or "golden" in model for model in models)
    ):
        return "ORACLE_FIXTURE"
    if (
        "ORCHESTRATION_SMOKE_ONLY" in evidence_level
        or "demo" in backend_class
    ):
        return "DEMO_FIXTURE"
    if (
        "deterministicprototypebackend" in backend_class
        or contract_mode == "scripted_deterministic_adapter"
        or "DETERMINISTIC_TEST_ONLY" in evidence_level
        or any("deterministic" in provider for provider in providers)
        or any("fixture" in model for model in models)
    ):
        return "DETERMINISTIC_FIXTURE"
    if (
        "scripted-v3a0-prototype" in providers
        or "operator-supplied-patch-v1" in models
    ):
        return "DEMO_FIXTURE"
    if (
        backend_class.endswith(".vitisbackend")
        and evidence_level
        in {"REAL_VITIS_VALIDATED", "REAL_VITIS_ATTEMPT_FAILED"}
        and replay == "NON_REPLAYABLE"
        and providers
        and not any("scripted" in provider or "replay" in provider for provider in providers)
    ):
        return "REAL_LLM_VITIS"
    return None


def _mode(
    result: Mapping[str, object],
    manifest: Mapping[str, object],
    task: Mapping[str, object],
) -> str:
    phase = result.get("phase_decision")
    candidates = [
        result.get("mode"),
        phase.get("mode") if isinstance(phase, Mapping) else None,
        manifest.get("mode"),
    ]
    task_type = str(task.get("task_type", "")).strip().casefold()
    candidates.append(
        {
            "optimize": "OPTIMIZE",
            "repair": "REPAIR",
            "synth_fix": "SYNTH_FIX",
            "synthesis_fix": "SYNTH_FIX",
            "structural_fix": "STRUCTURAL_FIX",
        }.get(task_type)
    )
    for value in candidates:
        normalized = str(value or "").strip().upper()
        if normalized in PHASE_MODES:
            return normalized
    raise HistoricalImportError("MODE_MISSING")


def _manifest_fingerprint(
    run_dir: Path,
    result: Mapping[str, object],
    manifest: Mapping[str, object],
) -> str:
    package = result.get("package")
    if not isinstance(package, Mapping):
        raise HistoricalImportError("PACKAGE_FINGERPRINT_MISSING")
    digest = _valid_digest(package.get("manifest_sha256"))
    if digest is None:
        raise HistoricalImportError("PACKAGE_FINGERPRINT_MISSING")
    if package.get("schema_version") != "v3a.package-commit.v1":
        raise HistoricalImportError("PACKAGE_COMMIT_SCHEMA_INVALID")
    manifest_ref = package.get("manifest_ref")
    if _artifact(run_dir, "package_manifest", manifest_ref) is None:
        raise HistoricalImportError("PACKAGE_MANIFEST_MISSING")
    if manifest.get("schema_version") != "v3a.package-manifest.v1":
        raise HistoricalImportError("PACKAGE_MANIFEST_SCHEMA_INVALID")
    if str(manifest.get("status", "")).upper() not in _TERMINAL_RUN_STATUSES:
        raise HistoricalImportError("PACKAGE_MANIFEST_NOT_TERMINAL")
    return digest


def _relative_ref(run_dir: Path, path: Path) -> str | None:
    try:
        resolved_run = run_dir.resolve()
        relative = path.resolve().relative_to(resolved_run)
    except (OSError, ValueError):
        return None
    if {part.casefold() for part in relative.parts}.intersection(
        _FORBIDDEN_ARTIFACT_PARTS
    ):
        return None
    rendered = relative.as_posix()
    return rendered if len(rendered) <= 320 else None


def _artifact(
    run_dir: Path,
    role: str,
    ref: object,
) -> dict[str, object] | None:
    if not isinstance(ref, str) or not ref:
        return None
    path = run_dir / ref
    relative = _relative_ref(run_dir, path)
    if relative is None or not path.is_file():
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return {"role": role, "ref": relative, "sha256": digest}


def _artifacts(
    run_dir: Path,
    items: Iterable[tuple[str, object]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for role, ref in items:
        item = _artifact(run_dir, role, ref)
        if item is None:
            continue
        identity = (str(item["role"]), str(item["ref"]))
        if identity not in seen:
            result.append(item)
            seen.add(identity)
    return result


def _read_safe_text(run_dir: Path, ref: object) -> str:
    if not isinstance(ref, str) or not ref:
        return ""
    path = run_dir / ref
    if _relative_ref(run_dir, path) is None or not path.is_file():
        return ""
    try:
        payload = path.read_bytes()
    except OSError:
        return ""
    if len(payload) > 1_000_000:
        return ""
    return payload.decode("utf-8", errors="ignore")


def _source_flags(run_dir: Path, candidate: Mapping[str, object]) -> dict[str, bool]:
    source = _read_safe_text(run_dir, candidate.get("source_ref"))
    upper = source.upper()
    return {
        "has_dataflow": bool(re.search(r"#\s*PRAGMA\s+HLS\s+DATAFLOW\b", upper)),
        "has_stream": "HLS::STREAM" in upper,
        "has_fifo": bool(
            re.search(r"#\s*PRAGMA\s+HLS\s+STREAM\b", upper)
            or re.search(r"\bDEPTH\s*=", upper)
        ),
        "has_interface": bool(
            re.search(r"#\s*PRAGMA\s+HLS\s+INTERFACE\b", upper)
        ),
        "has_bitwidth": bool(
            re.search(r"\bAP_(?:U?INT|U?FIXED)\s*<", upper)
        ),
    }


def _load_ref_object(run_dir: Path, ref: object) -> dict[str, object]:
    if not isinstance(ref, str) or not ref:
        return {}
    path = run_dir / ref
    if _relative_ref(run_dir, path) is None or not path.is_file():
        return {}
    try:
        return _read_json_object(path)
    except HistoricalImportError:
        return {}


def _synth_evidence(
    run_dir: Path,
    candidate: Mapping[str, object],
    *,
    baseline_ref: object = None,
) -> dict[str, object]:
    for ref in (
        candidate.get("synth_evidence_ref"),
        candidate.get("final_synth_evidence_ref"),
        baseline_ref,
    ):
        value = _load_ref_object(run_dir, ref)
        if value:
            return value
    return {}


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _bounded_metric(value: object) -> tuple[float | None, bool]:
    number = _number(value)
    if number is None:
        return None, False
    if number >= _VITIS_UNKNOWN_SENTINEL:
        return None, True
    return number, False


def _top_metrics(
    evidence: Mapping[str, object],
) -> tuple[float | None, float | None, bool, bool]:
    top = evidence.get("top_level")
    if not isinstance(top, Mapping):
        return None, None, False, False
    latency = top.get("latency")
    interval = top.get("transaction_interval")
    latency_raw = latency.get("worst") if isinstance(latency, Mapping) else None
    interval_raw = interval.get("max") if isinstance(interval, Mapping) else None
    latency_value, latency_unbounded = _bounded_metric(latency_raw)
    interval_value, interval_unbounded = _bounded_metric(interval_raw)
    return latency_value, interval_value, latency_unbounded, interval_unbounded


def _loops(evidence: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = evidence.get("loops")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _bottleneck_loop(evidence: Mapping[str, object]) -> Mapping[str, object] | None:
    loops = _loops(evidence)
    if not loops:
        return None

    def key(loop: Mapping[str, object]) -> tuple[float, float, float, str]:
        ii = _number(loop.get("pipeline_ii"))
        latency = _number(loop.get("latency_cycles"))
        trip = _number(loop.get("trip_count"))
        return (
            -1 if ii is None else ii,
            -1 if latency is None else latency,
            -1 if trip is None else trip,
            str(loop.get("name", "")),
        )

    return max(loops, key=key)


def _resource_pressure(evidence: Mapping[str, object]) -> str:
    top = evidence.get("top_level")
    utilization = top.get("utilization_percent") if isinstance(top, Mapping) else None
    if not isinstance(utilization, Mapping):
        return "UNKNOWN"
    values = [_number(value) for value in utilization.values()]
    observed = [value for value in values if value is not None]
    if not observed:
        return "UNKNOWN"
    maximum = max(observed)
    if maximum >= 85:
        return "HIGH"
    if maximum >= 50:
        return "MEDIUM"
    return "LOW"


def _fixed_taxonomy(*values: object) -> str | None:
    text = " ".join(str(value) for value in values if value is not None).casefold()
    text = re.sub(r"[_-]+", " ", text)
    if not text:
        return None
    rules = (
        ("SYNTHESIS_UNSUPPORTED", ("operator new", "delete[]", "dynamic allocation", "unsupported")),
        ("DATAFLOW_OR_FIFO", ("deadlock", "fifo", "stream", "dataflow")),
        ("MEMORY", ("memory", "bandwidth", "access bottleneck")),
        ("INTERFACE", ("interface", "top signature")),
        ("RESOURCE", ("resource", " lut", " dsp", "bram", "uram")),
        ("TIMING", ("timing", "clock violation", "slack")),
        ("PIPELINE_II", ("pipeline ii", "ii violation", " ii=")),
        ("LOOP_LATENCY", ("serial", "latency", "loop", "reduction")),
        ("FUNCTIONAL", ("runtime", "functional", "mismatch", "incorrect", "missing term")),
        ("SYNTHESIS", ("synth", "constraint violation")),
    )
    for taxonomy, needles in rules:
        if any(needle in text for needle in needles):
            return taxonomy
    return "UNKNOWN"


def _risk(value: object) -> tuple[str, str | None]:
    parsed: Mapping[str, object] = {}
    if isinstance(value, Mapping):
        parsed = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = {}
        if isinstance(decoded, Mapping):
            parsed = decoded
    level = str(parsed.get("level", "UNKNOWN")).strip().upper()
    if level not in {"LOW", "MEDIUM", "HIGH"}:
        level = "UNKNOWN"
    taxonomy = _fixed_taxonomy(
        parsed.get("primary_bottleneck"), parsed.get("primary_failure")
    )
    return level, taxonomy


def _failure_type(result: Mapping[str, object]) -> str | None:
    evidence = result.get("failure_evidence")
    value = evidence.get("failure_kind") if isinstance(evidence, Mapping) else None
    if not value:
        return None
    normalized = re.sub(r"[^A-Z0-9_]+", "_", str(value).strip().upper()).strip("_")
    return normalized[:80] or None


def _evidence_features(
    run_dir: Path,
    result: Mapping[str, object],
    parent: Mapping[str, object],
    risk_taxonomy: str | None,
) -> tuple[dict[str, object], float | None, int]:
    synth = _synth_evidence(
        run_dir,
        parent,
        baseline_ref=result.get("baseline_synth_evidence_ref"),
    )
    latency, interval, latency_unbounded, interval_unbounded = _top_metrics(synth)
    unbounded = int(latency_unbounded) + int(interval_unbounded)
    loop = _bottleneck_loop(synth)
    loop_ii = _number(loop.get("pipeline_ii")) if loop else None
    trip_count = _number(loop.get("trip_count")) if loop else None
    observations = synth.get("observations")
    observation_kinds = []
    if isinstance(observations, list):
        observation_kinds = [
            str(item.get("kind", ""))
            for item in observations
            if isinstance(item, Mapping)
        ]
    violation = loop.get("violation_type") if loop else None
    issue = loop.get("issue_type") if loop else None
    failure = _failure_type(result)
    bottleneck = _fixed_taxonomy(failure, *observation_kinds, violation, issue)
    if risk_taxonomy not in {None, "UNKNOWN"} and bottleneck in {
        None,
        "UNKNOWN",
        "PIPELINE_II",
        "LOOP_LATENCY",
        "SYNTHESIS",
    }:
        bottleneck = risk_taxonomy
    flags = _source_flags(run_dir, parent)
    memory_bottleneck = bottleneck == "MEMORY" or _fixed_taxonomy(
        *observation_kinds, violation, issue
    ) == "MEMORY"
    if not synth:
        pipeline_status = "UNKNOWN"
    elif loop_ii is None:
        pipeline_status = "NO_PIPELINE_II"
    elif loop_ii > 1:
        pipeline_status = "II_GT_1"
    else:
        pipeline_status = "II_1"
    return (
        {
            "failure_type": failure,
            "primary_bottleneck": bottleneck,
            "loop_ii": None if loop_ii is None else int(loop_ii),
            "trip_count_bucket": numeric_bucket(trip_count),
            "transaction_interval_bucket": (
                "UNKNOWN_OR_UNBOUNDED"
                if interval_unbounded
                else numeric_bucket(interval)
            ),
            "latency_bucket": (
                "UNKNOWN_OR_UNBOUNDED"
                if latency_unbounded
                else numeric_bucket(latency)
            ),
            "pipeline_status": pipeline_status,
            **flags,
            "memory_bottleneck": memory_bottleneck,
            "resource_pressure": _resource_pressure(synth),
        },
        latency,
        unbounded,
    )


def _strategy_bundle(
    proposal: Mapping[str, object],
    round_value: Mapping[str, object],
) -> list[str]:
    raw = round_value.get("strategy_bundle")
    if raw is None:
        raw = proposal.get("strategy_bundle", proposal.get("change_class", ""))
    try:
        return normalize_strategy_bundle(raw)
    except ValueError:
        if isinstance(raw, str):
            parts: Sequence[object] = raw.split("+")
        elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            parts = raw
        else:
            return []
        normalized: list[str] = []
        for item in parts:
            value = re.sub(r"[^A-Z0-9_]+", "_", str(item).upper()).strip("_")
            if value and value not in normalized:
                normalized.append(value)
        return normalized[:3]


def _validation_map(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, Mapping)
    }


def _status(gate: Mapping[str, object] | None) -> str:
    if not gate:
        return "NOT_RUN"
    raw = str(gate.get("status", "NOT_RUN")).strip().upper()
    if raw == "PASS":
        return "PASS"
    if raw == "FAIL":
        return "FAIL"
    if raw.startswith("SKIP"):
        return "SKIPPED"
    if raw in {"NOT_RUN", "NOT_ATTEMPTED", ""}:
        return "NOT_RUN"
    return "UNKNOWN"


def _optional_pass(status: str) -> bool | None:
    if status == "PASS":
        return True
    if status == "FAIL":
        return False
    return None


def _final_validation(
    result: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, Mapping[str, object]]:
    direct = _validation_map(candidate.get("final_validation"))
    if direct:
        return direct
    candidate_id = _text(candidate.get("candidate_id"))
    attempted = result.get("final_attempted_candidate_ids")
    attempted_ids = {str(item) for item in attempted} if isinstance(attempted, list) else set()
    if candidate_id and (
        candidate_id in attempted_ids
        or candidate_id == str(result.get("final_attempt_candidate_id", ""))
    ):
        return _validation_map(result.get("final_validation"))
    return {}


def _effective_validation(
    exploration: Mapping[str, Mapping[str, object]],
    final: Mapping[str, Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    effective = dict(exploration)
    for gate, value in final.items():
        if _status(value) not in {"NOT_RUN", "UNKNOWN"}:
            effective[gate] = value
    return effective


def _final_pass(
    candidate: Mapping[str, object], final: Mapping[str, Mapping[str, object]]
) -> bool | None:
    statuses = [_status(final.get(gate)) for gate in ("csim", "synth", "cosim")]
    if any(value == "FAIL" for value in statuses):
        return False
    if all(value == "PASS" for value in statuses):
        return True
    return None


def _tool_refs(
    exploration: Mapping[str, Mapping[str, object]],
    final: Mapping[str, Mapping[str, object]],
) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    seen: set[str] = set()
    for scope, values in (("candidate", exploration), ("final", final)):
        for gate in ("csim", "synth", "cosim"):
            value = values.get(gate)
            ref = value.get("result_ref") if isinstance(value, Mapping) else None
            if isinstance(ref, str) and ref not in seen:
                items.append((f"{scope}_{gate}_result", ref))
                seen.add(ref)
    return items


def _candidate_credits(
    config: Mapping[str, object],
    exploration: Mapping[str, Mapping[str, object]],
    final: Mapping[str, Mapping[str, object]],
) -> int:
    budget = config.get("budget")
    costs = budget.get("costs") if isinstance(budget, Mapping) else None
    if not isinstance(costs, Mapping):
        return 0
    seen: set[str] = set()
    total = 0
    for values in (exploration, final):
        for gate in ("csim", "synth", "cosim"):
            value = values.get(gate)
            if not isinstance(value, Mapping) or _status(value) not in {"PASS", "FAIL"}:
                continue
            action_id = str(value.get("action_id", value.get("result_ref", "")))
            if not action_id or action_id in seen:
                continue
            raw_cost = costs.get(gate)
            if isinstance(raw_cost, int) and not isinstance(raw_cost, bool) and raw_cost >= 0:
                total += raw_cost
            seen.add(action_id)
    return total


def _wall_time(
    run_dir: Path,
    proposal: Mapping[str, object],
    exploration: Mapping[str, Mapping[str, object]],
    final: Mapping[str, Mapping[str, object]],
) -> float | None:
    values: list[float] = []
    proposal_duration = _number(proposal.get("duration_seconds"))
    if proposal_duration is not None:
        values.append(proposal_duration)
    seen: set[str] = set()
    for _, ref in _tool_refs(exploration, final):
        if not isinstance(ref, str) or ref in seen:
            continue
        action = _load_ref_object(run_dir, ref)
        elapsed = _number(action.get("elapsed_s"))
        if elapsed is not None:
            values.append(elapsed)
        seen.add(ref)
    return sum(values) if values else None


def _failure_stage(
    candidate: Mapping[str, object],
    exploration: Mapping[str, Mapping[str, object]],
    final: Mapping[str, Mapping[str, object]],
    *,
    unbounded_after: bool,
) -> str | None:
    for prefix, values in (("", exploration), ("FINAL_", final)):
        for gate in ("csim", "synth", "cosim"):
            if _status(values.get(gate)) == "FAIL":
                stage = f"{prefix}{gate.upper()}"
                return stage if stage in _FIXED_FAILURE_STAGES else "CANDIDATE_REJECTED"
    if unbounded_after:
        return "UNKNOWN_OR_UNBOUNDED"
    if str(candidate.get("status", "")).upper() == "REJECTED":
        return "CANDIDATE_REJECTED"
    return None


def _proposal_tokens(proposal: Mapping[str, object]) -> int:
    total = 0
    for key in ("input_tokens", "output_tokens"):
        value = proposal.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def _round_values(result: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw = result.get("candidate_rounds")
    if not isinstance(raw, list):
        return {}
    return {
        str(item.get("candidate_id")): item
        for item in raw
        if isinstance(item, Mapping) and item.get("candidate_id")
    }


def _candidate_record(
    *,
    run_dir: Path,
    result: Mapping[str, object],
    config: Mapping[str, object],
    task: Mapping[str, object],
    manifest: Mapping[str, object],
    manifest_fingerprint: str,
    mode: str,
    execution_class: str,
    proposal_path: Path,
    proposal: Mapping[str, object],
    candidate: Mapping[str, object],
    parent: Mapping[str, object],
    round_value: Mapping[str, object],
    policy: ImportPolicy,
) -> tuple[dict[str, object], int]:
    candidate_id = _text(candidate.get("candidate_id"))
    if not candidate_id:
        raise HistoricalImportError("CANDIDATE_ID_MISSING")
    patch_ref = candidate.get("patch_ref")
    patch_text = _read_safe_text(run_dir, patch_ref)
    if not patch_text:
        raw_patch = proposal.get("patch")
        patch_text = raw_patch if isinstance(raw_patch, str) else ""
    if not patch_text:
        raise HistoricalImportError("PATCH_MISSING")
    patch_artifact = _artifact(run_dir, "candidate_patch", patch_ref)
    applied_patch_sha = (
        str(patch_artifact["sha256"])
        if patch_artifact is not None
        else hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    )
    risk_value = round_value.get("risk_decision", proposal.get("risk"))
    risk_level, risk_taxonomy = _risk(risk_value)
    evidence, latency_before, before_unbounded = _evidence_features(
        run_dir, result, parent, risk_taxonomy
    )
    candidate_synth = _synth_evidence(run_dir, candidate)
    (
        latency_after,
        _,
        after_latency_unbounded,
        after_interval_unbounded,
    ) = _top_metrics(candidate_synth)
    after_unbounded_count = int(after_latency_unbounded) + int(
        after_interval_unbounded
    )
    exploration = _validation_map(candidate.get("validation"))
    final = _final_validation(result, candidate)
    effective = _effective_validation(exploration, final)
    cosim = _status(effective.get("cosim"))
    final_pass = _final_pass(candidate, final)
    acceleration = None
    if (
        mode == "OPTIMIZE"
        and latency_before is not None
        and latency_before > 0
        and latency_after is not None
        and latency_after > 0
    ):
        acceleration = latency_before / latency_after
    status = str(candidate.get("status", "")).upper()
    promoted = status in {"PROMOTED", "FINAL_VERIFIED"} or str(
        round_value.get("decision", "")
    ).upper() in {"PROMOTED", "FINAL_VERIFIED"}
    task_id = _text(task.get("task_id")) or _text(result.get("task_id"))
    if not task_id:
        raise HistoricalImportError("TASK_ID_MISSING")
    task_split = _task_split_for(policy, task_id, task, manifest)
    family = _family_for(
        policy,
        task_id,
        task,
        manifest,
        source=_read_safe_text(run_dir, candidate.get("source_ref")),
    )
    difficulty = task.get("difficulty", 1)
    if not isinstance(difficulty, int) or isinstance(difficulty, bool) or difficulty <= 0:
        difficulty = 1
    package_ref = (
        result.get("package", {}).get("manifest_ref")
        if isinstance(result.get("package"), Mapping)
        else None
    )
    artifact_items: list[tuple[str, object]] = [
        ("package_manifest", package_ref),
        ("planner_proposal", proposal_path.relative_to(run_dir).as_posix()),
        ("candidate_patch", patch_ref),
        ("candidate_synth_evidence", candidate.get("synth_evidence_ref")),
        ("final_synth_evidence", candidate.get("final_synth_evidence_ref")),
    ]
    artifact_items.extend(_tool_refs(exploration, final))
    record = {
        "schema_version": EXPERIENCE_SCHEMA,
        "record_id": _identity_hash(
            EXPERIENCE_SCHEMA, manifest_fingerprint, candidate_id, applied_patch_sha
        ),
        "trajectory_id": _identity_hash(
            "trajectory", manifest_fingerprint, candidate_id, applied_patch_sha
        ),
        "revision": 1,
        "run_id": f"hist_{manifest_fingerprint}",
        "candidate_id": candidate_id,
        "task_id_hash": opaque_hash(task_id),
        "task_split": task_split,
        "mode": mode,
        "difficulty": difficulty,
        "algorithm_family": family,
        "execution_class": execution_class,
        "eligible_for_ranking": execution_class == "REAL_LLM_VITIS",
        "evidence_features": evidence,
        "proposal_features": {
            "strategy_bundle": _strategy_bundle(proposal, round_value),
            "patch_lines": changed_patch_lines(patch_text),
            "planner_risk": risk_level,
            "normalized_patch_hash": normalized_patch_hash(patch_text),
        },
        "outcome": {
            "patch_valid": True,
            "candidate_created": True,
            "csim_pass": _optional_pass(_status(effective.get("csim"))),
            "synth_pass": _optional_pass(_status(effective.get("synth"))),
            "cosim_status": cosim,
            "final_pass": final_pass,
            "promoted": promoted,
            "failure_stage": _failure_stage(
                candidate,
                exploration,
                final,
                unbounded_after=bool(after_unbounded_count),
            ),
            "latency_before": latency_before,
            "latency_after": latency_after,
            "acceleration": acceleration,
            "tokens": _proposal_tokens(proposal),
            "credits": _candidate_credits(config, exploration, final),
            "wall_time_seconds": _wall_time(
                run_dir, proposal, exploration, final
            ),
        },
        "artifact_refs": _artifacts(run_dir, artifact_items),
    }
    return validate_experience_record(record), before_unbounded + after_unbounded_count


def _unmaterialized_record(
    *,
    run_dir: Path,
    result: Mapping[str, object],
    task: Mapping[str, object],
    manifest: Mapping[str, object],
    manifest_fingerprint: str,
    mode: str,
    execution_class: str,
    proposal_path: Path,
    proposal: Mapping[str, object],
    rejection_path: Path,
    rejection: Mapping[str, object],
    parent: Mapping[str, object],
    round_index: int,
    policy: ImportPolicy,
) -> tuple[dict[str, object], int]:
    raw_patch = proposal.get("patch")
    patch_text = raw_patch if isinstance(raw_patch, str) else ""
    if not patch_text:
        raise HistoricalImportError("PATCH_MISSING")
    risk_level, risk_taxonomy = _risk(proposal.get("risk"))
    evidence, latency_before, unbounded = _evidence_features(
        run_dir, result, parent, risk_taxonomy
    )
    reason = str(rejection.get("reason", "PATCH_POLICY_REJECTED")).upper()
    duplicate = reason == "DUPLICATE_STRATEGY_BUNDLE"
    failure_stage = (
        "DUPLICATE_STRATEGY_BUNDLE" if duplicate else "PATCH_POLICY_REJECTED"
    )
    planner_output_sha = _valid_digest(rejection.get("planner_output_sha256"))
    if planner_output_sha is None:
        try:
            planner_output_sha = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise HistoricalImportError("PROPOSAL_ARTIFACT_READ_FAILED") from exc
    candidate_id = f"proposal_{round_index:03d}"
    task_id = _text(task.get("task_id")) or _text(result.get("task_id"))
    if not task_id:
        raise HistoricalImportError("TASK_ID_MISSING")
    task_split = _task_split_for(policy, task_id, task, manifest)
    family = _family_for(
        policy,
        task_id,
        task,
        manifest,
        source=_read_safe_text(run_dir, parent.get("source_ref")),
    )
    difficulty = task.get("difficulty", 1)
    if not isinstance(difficulty, int) or isinstance(difficulty, bool) or difficulty <= 0:
        difficulty = 1
    package_ref = (
        result.get("package", {}).get("manifest_ref")
        if isinstance(result.get("package"), Mapping)
        else None
    )
    record = {
        "schema_version": EXPERIENCE_SCHEMA,
        "record_id": _identity_hash(
            EXPERIENCE_SCHEMA, manifest_fingerprint, round_index, planner_output_sha
        ),
        "trajectory_id": _identity_hash(
            "trajectory", manifest_fingerprint, round_index, planner_output_sha
        ),
        "revision": 1,
        "run_id": f"hist_{manifest_fingerprint}",
        "candidate_id": candidate_id,
        "task_id_hash": opaque_hash(task_id),
        "task_split": task_split,
        "mode": mode,
        "difficulty": difficulty,
        "algorithm_family": family,
        "execution_class": execution_class,
        # A rejected proposal is useful for Patch-policy diagnostics, but it
        # never became a Candidate and therefore must not influence strategy
        # retrieval/ranking statistics.
        "eligible_for_ranking": False,
        "evidence_features": evidence,
        "proposal_features": {
            "strategy_bundle": _strategy_bundle(proposal, rejection),
            "patch_lines": changed_patch_lines(patch_text),
            "planner_risk": risk_level,
            "normalized_patch_hash": normalized_patch_hash(patch_text),
        },
        "outcome": {
            "patch_valid": duplicate,
            "candidate_created": False,
            "csim_pass": None,
            "synth_pass": None,
            "cosim_status": "NOT_RUN",
            "final_pass": None,
            "promoted": False,
            "failure_stage": failure_stage,
            "latency_before": latency_before,
            "latency_after": None,
            "acceleration": None,
            "tokens": _proposal_tokens(proposal),
            "credits": 0,
            "wall_time_seconds": _number(proposal.get("duration_seconds")),
        },
        "artifact_refs": _artifacts(
            run_dir,
            (
                ("package_manifest", package_ref),
                ("planner_proposal", proposal_path.relative_to(run_dir).as_posix()),
                ("proposal_rejection", rejection_path.relative_to(run_dir).as_posix()),
            ),
        ),
    }
    return validate_experience_record(record), unbounded


def _extract_v3_run(source: HistoricalSource, policy: ImportPolicy) -> _RunExtraction:
    result_path = source.path
    run_dir = result_path.parent
    result = _read_json_object(result_path)
    if (
        policy.require_terminal
        and str(result.get("status", "")).upper() not in _TERMINAL_RUN_STATUSES
    ):
        raise HistoricalImportError("NON_TERMINAL_RUN")
    config = _load_optional_object(run_dir / "v3_run_config.json")
    task = _load_optional_object(run_dir / "v3_task_spec.json")
    manifest = _load_optional_object(run_dir / "control" / "package_manifest.json")
    proposal_paths = _proposal_paths(run_dir)
    proposals = [_read_json_object(path) for path in proposal_paths]
    execution_class = _execution_class(result, config, proposals)
    source_id = _source_id(result_path)
    if execution_class is None:
        raise HistoricalImportError("UNVERIFIED_EXECUTION_PROVENANCE")
    if execution_class not in EXECUTION_CLASSES:
        raise HistoricalImportError("INVALID_EXECUTION_CLASS")
    if execution_class != "REAL_LLM_VITIS" and not policy.include_fixtures:
        return _RunExtraction(
            (),
            SourceAudit(
                source_id=source_id,
                source_kind=source.kind,
                execution_class=execution_class,
                disposition="EXCLUDED",
                reason="FIXTURE_EXCLUDED_BY_POLICY",
                proposal_count=len(proposals),
            ),
        )
    manifest_fingerprint = _manifest_fingerprint(run_dir, result, manifest)
    mode = _mode(result, manifest, task)
    registry = _load_optional_object(run_dir / "candidate_registry.json")
    raw_candidates = registry.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, Mapping) else {}
    candidate_by_ref: dict[str, Mapping[str, object]] = {}
    for candidate_id, raw_candidate in candidates.items():
        if not isinstance(raw_candidate, Mapping):
            continue
        candidate = dict(raw_candidate)
        candidate.setdefault("candidate_id", str(candidate_id))
        ref = candidate.get("planner_ref")
        if isinstance(ref, str) and ref:
            candidate_by_ref[ref] = candidate
    rejection_by_ref: dict[str, tuple[Path, Mapping[str, object]]] = {}
    rejection_dir = run_dir / "control" / "proposal_rejections"
    if rejection_dir.is_dir():
        for rejection_path in sorted(rejection_dir.glob("round_*.json")):
            rejection = _read_json_object(rejection_path)
            ref = rejection.get("planner_ref")
            if isinstance(ref, str) and ref:
                rejection_by_ref[ref] = (rejection_path, rejection)
    round_by_candidate = _round_values(result)
    records: list[dict[str, object]] = []
    unbounded_count = 0
    for proposal_path, proposal in zip(proposal_paths, proposals, strict=True):
        proposal_ref = proposal_path.relative_to(run_dir).as_posix()
        round_index = _round_index(proposal_path, proposal)
        parent_id = str(proposal.get("parent_candidate_id", ""))
        parent = candidates.get(parent_id)
        if not isinstance(parent, Mapping):
            parent = {}
        candidate = candidate_by_ref.get(proposal_ref)
        if candidate is not None:
            round_value = round_by_candidate.get(str(candidate.get("candidate_id")), {})
            record, unbounded = _candidate_record(
                run_dir=run_dir,
                result=result,
                config=config,
                task=task,
                manifest=manifest,
                manifest_fingerprint=manifest_fingerprint,
                mode=mode,
                execution_class=execution_class,
                proposal_path=proposal_path,
                proposal=proposal,
                candidate=candidate,
                parent=parent,
                round_value=round_value,
                policy=policy,
            )
        elif proposal_ref in rejection_by_ref:
            rejection_path, rejection = rejection_by_ref[proposal_ref]
            record, unbounded = _unmaterialized_record(
                run_dir=run_dir,
                result=result,
                task=task,
                manifest=manifest,
                manifest_fingerprint=manifest_fingerprint,
                mode=mode,
                execution_class=execution_class,
                proposal_path=proposal_path,
                proposal=proposal,
                rejection_path=rejection_path,
                rejection=rejection,
                parent=parent,
                round_index=round_index,
                policy=policy,
            )
        else:
            continue
        records.append(record)
        unbounded_count += unbounded
    disposition = "IMPORTED" if records else "EXCLUDED"
    reason = None if records else "NO_BOUND_PROPOSALS"
    return _RunExtraction(
        tuple(records),
        SourceAudit(
            source_id=source_id,
            source_kind=source.kind,
            execution_class=execution_class,
            disposition=disposition,
            reason=reason,
            record_count=len(records),
            candidate_count=sum(
                bool(record["outcome"]["candidate_created"]) for record in records
            ),
            proposal_count=len(proposals),
            unbounded_metric_count=unbounded_count,
        ),
    )


def import_historical_runs(
    roots: Sequence[str | Path],
    repository: ExperienceRepository | None = None,
    *,
    policy: ImportPolicy | None = None,
) -> HistoricalImportResult:
    """Import all discoverable sources, continuing past malformed/excluded data."""

    active_policy = policy or ImportPolicy()
    records: list[dict[str, object]] = []
    audits: list[SourceAudit] = []
    inserted: list[str] = []
    duplicates: list[str] = []
    for source in discover_historical_sources(roots):
        if source.kind != "V3_TERMINAL":
            audits.append(_fixture_audit(source))
            continue
        try:
            extraction = _extract_v3_run(source, active_policy)
        except HistoricalImportError as exc:
            audits.append(
                SourceAudit(
                    source_id=_source_id(source.path),
                    source_kind=source.kind,
                    execution_class=None,
                    disposition="EXCLUDED",
                    reason=exc.code,
                )
            )
            continue
        except ValueError:
            audits.append(
                SourceAudit(
                    source_id=_source_id(source.path),
                    source_kind=source.kind,
                    execution_class=None,
                    disposition="EXCLUDED",
                    reason="INVALID_EXTRACTED_RECORD",
                )
            )
            continue
        audits.append(extraction.audit)
        for record in extraction.records:
            records.append(record)
            if repository is not None:
                result = repository.put_if_absent(record)
                target = inserted if result.inserted else duplicates
                target.append(result.record_id)
    records.sort(key=lambda item: str(item["record_id"]))
    audits.sort(key=lambda item: (item.source_id, item.source_kind))
    return HistoricalImportResult(
        records=tuple(records),
        source_audits=tuple(audits),
        inserted_record_ids=tuple(sorted(inserted)),
        duplicate_record_ids=tuple(sorted(duplicates)),
        repository_record_count=(
            repository.snapshot().record_count if repository is not None else None
        ),
    )


def _outcome_label(record: Mapping[str, object]) -> str:
    outcome = record["outcome"]
    assert isinstance(outcome, Mapping)
    stage = outcome.get("failure_stage")
    if stage in {"PATCH_POLICY_REJECTED", "DUPLICATE_STRATEGY_BUNDLE"}:
        return str(stage)
    if outcome.get("final_pass") is True:
        return "FINAL_PASS"
    if outcome.get("promoted") is True:
        return "PROMOTED"
    if outcome.get("candidate_created") is True:
        return "CANDIDATE_REJECTED"
    return "UNOBSERVED"


def _missing_fields(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    missing: Counter[str] = Counter()
    for record in records:
        evidence = record["evidence_features"]
        outcome = record["outcome"]
        assert isinstance(evidence, Mapping) and isinstance(outcome, Mapping)
        if record.get("task_split") == "unspecified":
            missing["task_split"] += 1
        if record.get("algorithm_family") == "unspecified":
            missing["algorithm_family"] += 1
        for name in ("failure_type", "primary_bottleneck", "loop_ii"):
            if evidence.get(name) is None:
                missing[f"evidence_features.{name}"] += 1
        for name in ("latency_before", "latency_after", "wall_time_seconds"):
            if outcome.get(name) is None:
                missing[f"outcome.{name}"] += 1
    return dict(sorted(missing.items()))


def build_import_report(result: HistoricalImportResult) -> dict[str, object]:
    records = result.records
    real_records = [
        record for record in records if record["execution_class"] == "REAL_LLM_VITIS"
    ]
    fixture_records = [
        record for record in records if record["execution_class"] != "REAL_LLM_VITIS"
    ]
    mode_counts = Counter(str(record["mode"]) for record in records)
    outcome_counts = Counter(_outcome_label(record) for record in records)
    mode_outcomes: dict[str, Counter[str]] = {
        mode: Counter() for mode in sorted(PHASE_MODES)
    }
    for record in records:
        mode_outcomes[str(record["mode"])][_outcome_label(record)] += 1
    execution_counts = Counter(str(record["execution_class"]) for record in records)
    exclusion_counts = Counter(
        str(audit.reason)
        for audit in result.source_audits
        if audit.disposition == "EXCLUDED" and audit.reason
    )
    return {
        "schema_version": IMPORT_REPORT_SCHEMA,
        "discovered_source_count": len(result.source_audits),
        "imported_run_count": len(
            {str(record["run_id"]) for record in records}
        ),
        "real_run_count": len(
            {str(record["run_id"]) for record in real_records}
        ),
        "fixture_run_count": len(
            {str(record["run_id"]) for record in fixture_records}
        ),
        "record_count": len(records),
        "parsed_record_count": len(records),
        "repository_record_count": result.repository_record_count,
        "fixture_record_count": len(fixture_records),
        "candidate_count": sum(
            bool(record["outcome"]["candidate_created"]) for record in records
        ),
        "real_candidate_count": sum(
            bool(record["outcome"]["candidate_created"])
            for record in real_records
        ),
        "ranking_eligible_count": sum(
            bool(record["eligible_for_ranking"]) for record in records
        ),
        "inserted_count": len(result.inserted_record_ids),
        "duplicate_count": len(result.duplicate_record_ids),
        "mode_distribution": {
            mode: mode_counts.get(mode, 0) for mode in sorted(PHASE_MODES)
        },
        "outcome_distribution": dict(sorted(outcome_counts.items())),
        "mode_outcome_distribution": {
            mode: dict(sorted(counts.items()))
            for mode, counts in mode_outcomes.items()
        },
        "execution_class_distribution": dict(sorted(execution_counts.items())),
        "missing_fields": _missing_fields(records),
        "unbounded_metric_count": sum(
            audit.unbounded_metric_count for audit in result.source_audits
        ),
        "excluded_source_count": sum(
            audit.disposition == "EXCLUDED" for audit in result.source_audits
        ),
        "excluded_by_reason": dict(sorted(exclusion_counts.items())),
        "sources": [audit.to_dict() for audit in result.source_audits],
    }


def _stage_counts(
    records: Sequence[Mapping[str, object]],
    field: str,
) -> dict[str, int]:
    values = [record["outcome"][field] for record in records]
    attempted = [value for value in values if isinstance(value, bool)]
    return {
        "attempted": len(attempted),
        "passed": sum(value is True for value in attempted),
        "failed": sum(value is False for value in attempted),
    }


def _average(values: Iterable[object]) -> float | None:
    observed: list[float] = []
    for value in values:
        number = _number(value)
        if number is not None and number < _VITIS_UNKNOWN_SENTINEL:
            observed.append(number)
    return sum(observed) / len(observed) if observed else None


def _strict_improvement(record: Mapping[str, object]) -> bool:
    if record.get("mode") != "OPTIMIZE":
        return False
    outcome = record["outcome"]
    assert isinstance(outcome, Mapping)
    before = _number(outcome.get("latency_before"))
    after = _number(outcome.get("latency_after"))
    correctness = (
        outcome.get("csim_pass") is True
        and outcome.get("synth_pass") is True
        and outcome.get("cosim_status") != "FAIL"
    )
    return bool(
        correctness
        and before is not None
        and after is not None
        and before < _VITIS_UNKNOWN_SENTINEL
        and after < _VITIS_UNKNOWN_SENTINEL
        and after < before
    )


def _strategy_group(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    outcomes = [record["outcome"] for record in records]
    csim_attempts = sum(isinstance(outcome["csim_pass"], bool) for outcome in outcomes)
    synth_attempts = sum(
        isinstance(outcome["synth_pass"], bool) for outcome in outcomes
    )
    cosim_attempts = sum(
        outcome["cosim_status"] in {"PASS", "FAIL"} for outcome in outcomes
    )
    final_attempts = sum(
        isinstance(outcome["final_pass"], bool) for outcome in outcomes
    )
    improvement_attempts = sum(
        record["mode"] == "OPTIMIZE"
        and outcome["candidate_created"] is True
        and outcome["csim_pass"] is True
        and outcome["synth_pass"] is True
        and outcome.get("latency_before") is not None
        and outcome.get("latency_after") is not None
        for record, outcome in zip(records, outcomes, strict=True)
    )
    return {
        "attempts": len(records),
        "patch_valid": sum(bool(outcome["patch_valid"]) for outcome in outcomes),
        "candidate_attempts": sum(
            bool(outcome["patch_valid"]) for outcome in outcomes
        ),
        "candidate_created": sum(
            bool(outcome["candidate_created"]) for outcome in outcomes
        ),
        "csim_attempts": csim_attempts,
        "csim_pass": sum(outcome["csim_pass"] is True for outcome in outcomes),
        "synth_attempts": synth_attempts,
        "synth_pass": sum(outcome["synth_pass"] is True for outcome in outcomes),
        "strict_improvement_attempts": improvement_attempts,
        "strict_improvement": sum(_strict_improvement(record) for record in records),
        "cosim_attempts": cosim_attempts,
        "cosim_pass": sum(outcome["cosim_status"] == "PASS" for outcome in outcomes),
        "final_attempts": final_attempts,
        "final_pass": sum(outcome["final_pass"] is True for outcome in outcomes),
        "average_acceleration": _average(
            outcome.get("acceleration") for outcome in outcomes
        ),
        "average_tokens": _average(outcome.get("tokens") for outcome in outcomes),
        "average_credits": _average(outcome.get("credits") for outcome in outcomes),
        "average_wall_time_seconds": _average(
            outcome.get("wall_time_seconds") for outcome in outcomes
        ),
    }


def build_experience_stats(
    records: Sequence[Mapping[str, object]],
    *,
    ranking_only: bool = True,
) -> dict[str, object]:
    """Build stage-conditioned statistics without treating NOT_RUN as failure."""

    validated = [validate_experience_record(record) for record in records]
    selected = [
        record
        for record in validated
        if not ranking_only
        or (
            record["eligible_for_ranking"]
            and record["execution_class"] == "REAL_LLM_VITIS"
        )
    ]
    patch_attempted = len(selected)
    patch_passed = sum(record["outcome"]["patch_valid"] for record in selected)
    cosim_attempted = [
        record
        for record in selected
        if record["outcome"]["cosim_status"] in {"PASS", "FAIL"}
    ]
    groups: dict[tuple[str, str, tuple[str, ...]], list[dict[str, object]]] = defaultdict(list)
    for record in selected:
        evidence = record["evidence_features"]
        proposal = record["proposal_features"]
        context = str(
            evidence.get("primary_bottleneck")
            or evidence.get("failure_type")
            or "UNKNOWN"
        )
        key = (
            str(record["mode"]),
            context,
            tuple(str(item) for item in proposal["strategy_bundle"]),
        )
        groups[key].append(record)
    strategy_groups = []
    for (mode, context, bundle), group_records in sorted(groups.items()):
        strategy_groups.append(
            {
                "mode": mode,
                "context": context,
                "strategy_bundle": list(bundle),
                **_strategy_group(group_records),
            }
        )
    outcomes = [record["outcome"] for record in selected]
    return {
        "schema_version": EXPERIENCE_STATS_SCHEMA,
        "ranking_only": ranking_only,
        "record_count": len(selected),
        "mode_distribution": dict(
            sorted(Counter(str(record["mode"]) for record in selected).items())
        ),
        "stages": {
            "patch": {
                "attempted": patch_attempted,
                "passed": patch_passed,
                "failed": patch_attempted - patch_passed,
            },
            "candidate": {
                "attempted": sum(
                    bool(record["outcome"]["patch_valid"]) for record in selected
                ),
                "created": sum(
                    bool(record["outcome"]["candidate_created"]) for record in selected
                ),
            },
            "csim": _stage_counts(selected, "csim_pass"),
            "synth": _stage_counts(selected, "synth_pass"),
            "cosim": {
                "attempted": len(cosim_attempted),
                "passed": sum(
                    record["outcome"]["cosim_status"] == "PASS"
                    for record in cosim_attempted
                ),
                "failed": sum(
                    record["outcome"]["cosim_status"] == "FAIL"
                    for record in cosim_attempted
                ),
            },
            "final": _stage_counts(selected, "final_pass"),
        },
        "strict_improvement_count": sum(
            _strict_improvement(record) for record in selected
        ),
        "averages": {
            "acceleration": _average(
                outcome.get("acceleration") for outcome in outcomes
            ),
            "tokens": _average(outcome.get("tokens") for outcome in outcomes),
            "credits": _average(outcome.get("credits") for outcome in outcomes),
            "wall_time_seconds": _average(
                outcome.get("wall_time_seconds") for outcome in outcomes
            ),
        },
        "strategy_groups": strategy_groups,
    }


def render_data_quality_markdown(result: HistoricalImportResult) -> str:
    report = build_import_report(result)
    missing = report["missing_fields"]
    exclusions = report["excluded_by_reason"]
    lines = [
        "# V3-E Experience Data Quality",
        "",
        f"Schema: `{DATA_QUALITY_REPORT_SCHEMA}`",
        "",
        "Only fixed, public, bounded features and relative artifact hashes are exported. "
        "Source, Patch, Prompt, log, credential, local-path, golden and hidden-like "
        "contents are not retained.",
        "",
        "## Coverage",
        "",
        f"- Discovered sources: {report['discovered_source_count']}",
        f"- Imported terminal runs: {report['imported_run_count']}",
        f"- Real LLM/Vitis runs: {report['real_run_count']}",
        f"- Fixture runs retained: {report['fixture_run_count']}",
        f"- Experience records: {report['record_count']}",
        f"- Materialized Candidates: {report['candidate_count']}",
        f"- Ranking-eligible records: {report['ranking_eligible_count']}",
        f"- UNKNOWN_OR_UNBOUNDED metrics: {report['unbounded_metric_count']}",
        "",
        "## Missing fields",
        "",
    ]
    if missing:
        lines.extend(f"- `{name}`: {count}" for name, count in missing.items())
    else:
        lines.append("- None")
    lines.extend(["", "## Excluded sources", ""])
    if exclusions:
        lines.extend(f"- `{name}`: {count}" for name, count in exclusions.items())
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Semantics",
            "",
            "- Patch-policy rejection has `patch_valid=false`, `candidate_created=false`, "
            "and all downstream gates unattempted.",
            "- Patch-policy rejection is retained only in the all-proposals audit; it is "
            "excluded from default Candidate statistics and strategy ranking.",
            "- Duplicate-strategy rejection has `patch_valid=true`, "
            "`candidate_created=false`, and is excluded from ranking.",
            "- Final validation overlays exploration `NOT_RUN` gates only for the same "
            "Candidate.",
            "- Vitis unknown/unbounded latency sentinels become null numeric outcomes and "
            "never enter averages; a legitimate zero-cycle combinational latency is kept.",
            "- Fixture provenance is explicit and fixtures are never ranking-eligible.",
            "- Terminal `REAL_VITIS_ATTEMPT_FAILED` runs are retained as genuine negative "
            "Agent evidence; `FAILED` is not treated as non-terminal.",
            "",
        ]
    )
    return "\n".join(lines)


def write_import_artifacts(
    result: HistoricalImportResult,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Write the three derived reports; the repository owns its JSONL file."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    report_path = target / "experience_import_report.json"
    stats_path = target / "experience_stats.json"
    quality_path = target / "experience_data_quality_report.md"
    report_path.write_bytes(canonical_json(build_import_report(result)) + b"\n")
    stats_path.write_bytes(
        canonical_json(build_experience_stats(result.records)) + b"\n"
    )
    quality_path.write_text(render_data_quality_markdown(result), encoding="utf-8")
    return {
        "import_report": report_path,
        "stats": stats_path,
        "data_quality_report": quality_path,
    }


__all__ = [
    "DATA_QUALITY_REPORT_SCHEMA",
    "EXPERIENCE_STATS_SCHEMA",
    "HistoricalImportError",
    "HistoricalImportResult",
    "HistoricalSource",
    "IMPORT_REPORT_SCHEMA",
    "ImportPolicy",
    "SourceAudit",
    "build_experience_stats",
    "build_import_report",
    "discover_historical_sources",
    "import_historical_runs",
    "render_data_quality_markdown",
    "write_import_artifacts",
]
