"""Portable, tamper-evident receipts for the V3-D real-Vitis anchors.

The original Vitis work directories are intentionally ignored because they
contain thousands of generated files, machine-local paths, and large logs.
This module extracts the small provenance subset needed for review:

* the exact source ``oracle_result.json`` hash;
* backend and task fingerprints;
* every gate check and compact result;
* hashes of the raw Vitis artifacts, after verifying them at extraction time.

The checked-in receipt bundle does *not* turn an Oracle baseline/golden replay
into an LLM-Agent result.  A clean clone can verify that the receipt set is
internally hash-bound and still matches the current task tree.  Re-verifying
the raw artifact bytes requires the retained, ignored source run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .v3d_corpus import validate_acceptance, validate_mutation_manifest
from .v3d_oracle_validator import _tree_fingerprint


RECEIPT_SCHEMA_VERSION = "v3d.real-vitis-anchor-receipt.v1"
MANIFEST_SCHEMA_VERSION = "v3d.real-vitis-anchor-receipt-manifest.v1"
RELEASE_SCHEMA_VERSION = "v3d.corpus-oracle-anchor-release.v3"
EXTRACTOR_VERSION = "v3d.anchor-receipt-extractor.v1"
EVIDENCE_CLASS = "REAL_VITIS_2025_2_NO_LLM"
EXPECTED_MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
HEX_DIGITS = frozenset("0123456789abcdef")


class AnchorReceiptError(RuntimeError):
    """Raised when an anchor receipt cannot be trusted."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise AnchorReceiptError(f"cannot hash {path}: {exc}") from exc


def _read_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnchorReceiptError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise AnchorReceiptError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AnchorReceiptError(f"{label} must be an object")
    return dict(value)


def _sequence(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise AnchorReceiptError(f"{label} must be an array")
    return list(value)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= HEX_DIGITS
    )


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AnchorReceiptError(f"{label} must be a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AnchorReceiptError(f"{label} is not a safe relative path")
    return candidate


def _inside(root: Path, relative: Path, *, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise AnchorReceiptError(f"{label} escapes its root") from exc
    return resolved


def _receipt_content_hash(receipt: Mapping[str, object]) -> str:
    unsigned = dict(receipt)
    unsigned.pop("content_sha256", None)
    return _sha256_bytes(_canonical_json(unsigned))


def _compact_report(report: object) -> dict[str, object] | None:
    if not isinstance(report, dict):
        return None
    compact: dict[str, object] = {}
    for key in (
        "estimated_clock_period_ns",
        "latency",
        "interval",
        "resources",
        "utilization_percent",
    ):
        value = report.get(key)
        if isinstance(value, (dict, int, float)) and not isinstance(value, bool):
            compact[key] = value
    return compact or None


def _compact_observation(
    observation: Mapping[str, object],
    *,
    attempt_root: Path,
    subject: str,
    gate: str,
) -> dict[str, object]:
    status = observation.get("status")
    if status not in {"PASS", "FAIL", "NOT_RUN", "ERROR"}:
        raise AnchorReceiptError(f"{subject}.{gate} has invalid status {status!r}")

    raw_hashes = observation.get("artifact_hashes", {})
    raw_paths = observation.get("artifacts", {})
    artifact_hashes = _mapping(raw_hashes, label=f"{subject}.{gate}.artifact_hashes")
    artifact_paths = _mapping(raw_paths, label=f"{subject}.{gate}.artifacts")
    if set(artifact_hashes) != set(artifact_paths):
        raise AnchorReceiptError(f"{subject}.{gate} artifact names do not match")
    if status != "NOT_RUN" and gate in {"csim", "synth", "cosim"} and not artifact_hashes:
        raise AnchorReceiptError(f"{subject}.{gate} lacks raw artifact hashes")

    verified_hashes: dict[str, str] = {}
    for name in sorted(artifact_hashes):
        digest = artifact_hashes[name]
        if not _is_sha256(digest):
            raise AnchorReceiptError(f"{subject}.{gate}.{name} has invalid SHA-256")
        relative = _safe_relative(
            artifact_paths[name], label=f"{subject}.{gate}.{name}.path"
        )
        artifact = _inside(attempt_root, relative, label=f"{subject}.{gate}.{name}")
        if _sha256_file(artifact) != digest:
            raise AnchorReceiptError(f"{subject}.{gate}.{name} raw artifact drift")
        verified_hashes[str(name)] = str(digest)

    result: dict[str, object] = {
        "status": status,
        "phase": observation.get("phase"),
        "return_code": observation.get("return_code"),
        "elapsed_seconds": observation.get("elapsed_seconds"),
        "artifact_hashes": verified_hashes,
    }
    report = _compact_report(observation.get("report"))
    if report is not None:
        result["metrics"] = report
    cosim = observation.get("cosim")
    if isinstance(cosim, dict):
        result["cosim_metrics"] = cosim
    if gate == "ppa":
        for key in ("comparison_policy", "baseline_metrics", "golden_metrics"):
            if key in observation:
                result[key] = observation[key]
    return result


def _artifact_binding(observations: Mapping[str, object]) -> dict[str, object]:
    rows: list[dict[str, str]] = []
    for subject in sorted(observations):
        gates = _mapping(observations[subject], label=f"observations.{subject}")
        for gate in sorted(gates):
            result = _mapping(gates[gate], label=f"observations.{subject}.{gate}")
            hashes = _mapping(
                result.get("artifact_hashes", {}),
                label=f"observations.{subject}.{gate}.artifact_hashes",
            )
            for name in sorted(hashes):
                digest = hashes[name]
                if not _is_sha256(digest):
                    raise AnchorReceiptError("receipt contains an invalid artifact hash")
                rows.append(
                    {
                        "subject": str(subject),
                        "gate": str(gate),
                        "name": str(name),
                        "sha256": str(digest),
                    }
                )
    return {
        "count": len(rows),
        "set_sha256": _sha256_bytes(_canonical_json(rows)),
    }


def _assert_portable(value: object, *, path: str = "receipt") -> None:
    """Reject path/log/secret material before a receipt can be written."""

    if isinstance(value, dict):
        forbidden_keys = {"evidence", "artifacts", "log", "raw_log", "api_key"}
        overlap = forbidden_keys & {str(key).casefold() for key in value}
        if overlap:
            raise AnchorReceiptError(f"{path} contains forbidden raw field(s): {sorted(overlap)}")
        for key, child in value.items():
            _assert_portable(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_portable(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.casefold()
        if value.startswith("/") or ":\\" in value:
            raise AnchorReceiptError(f"{path} contains an absolute path")
        if any(
            marker in lowered
            for marker in ("openai_api_key", "api-key", "bearer ", "/home/")
        ):
            raise AnchorReceiptError(f"{path} contains secret or machine-local material")


def _source_record_for_anchor(
    *,
    runs_root: Path,
    anchor: Mapping[str, object],
) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    task_id = str(anchor.get("task_id", ""))
    run_id = str(anchor.get("run_id", ""))
    if not task_id or not run_id or Path(run_id).name != run_id:
        raise AnchorReceiptError(f"invalid source binding for {task_id or '<unknown>'}")
    run_root = _inside(runs_root, Path(run_id), label=f"{task_id}.run")
    summary_path = run_root / "summary.json"
    summary = _read_object(summary_path, label=f"{run_id} summary")
    accepted = _sequence(summary.get("accepted"), label=f"{run_id}.accepted")
    matching = [
        _mapping(item, label=f"{run_id}.accepted[]")
        for item in accepted
        if isinstance(item, dict)
        and item.get("task_id") == task_id
        and item.get("task_fingerprint") == anchor.get("task_fingerprint")
        and item.get("real_anchor") is True
    ]
    if len(matching) != 1:
        raise AnchorReceiptError(f"{task_id} does not resolve to one accepted source record")
    record_ref = _safe_relative(matching[0].get("record_ref"), label=f"{task_id}.record_ref")
    record_path = _inside(run_root, record_ref, label=f"{task_id}.record")
    record = _read_object(record_path, label=f"{task_id} oracle result")
    return summary_path, record_path, summary, record


def _build_receipt(
    *,
    corpus_root: Path,
    runs_root: Path,
    anchor: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    task_id = str(anchor.get("task_id", ""))
    task_dir = corpus_root / "tasks" / task_id
    if not task_dir.is_dir():
        raise AnchorReceiptError(f"missing current task package: {task_id}")
    task_fingerprint = _tree_fingerprint(task_dir)
    if task_fingerprint != anchor.get("task_fingerprint"):
        raise AnchorReceiptError(f"{task_id} release/task fingerprint mismatch")

    summary_path, record_path, summary, record = _source_record_for_anchor(
        runs_root=runs_root,
        anchor=anchor,
    )
    backend = _mapping(summary.get("backend"), label="source backend")
    backend_fingerprint = backend.get("fingerprint")
    required_record_values = {
        "schema_version": "v3d.oracle-task.v1",
        "status": "ACCEPTED",
        "real_anchor": True,
        "evidence_class": "real_vitis",
        "backend": "vitis",
        "task_id": task_id,
        "task_fingerprint": task_fingerprint,
        "mode": anchor.get("mode"),
        "operator": anchor.get("operator"),
    }
    for key, expected in required_record_values.items():
        if record.get(key) != expected:
            raise AnchorReceiptError(
                f"{task_id} source record {key} mismatch: {record.get(key)!r} != {expected!r}"
            )
    if (
        backend.get("name") != "vitis"
        or backend.get("evidence_class") != "real_vitis"
        or backend.get("real_anchor_authorized") is not True
        or not _is_sha256(backend_fingerprint)
        or record.get("backend_fingerprint") != backend_fingerprint
    ):
        raise AnchorReceiptError(f"{task_id} source backend is not authorized real Vitis")

    checks = _sequence(record.get("checks"), label=f"{task_id}.checks")
    if len(checks) != anchor.get("checks"):
        raise AnchorReceiptError(f"{task_id} check count differs from release")
    for index, raw_check in enumerate(checks):
        check = _mapping(raw_check, label=f"{task_id}.checks[{index}]")
        if check.get("matches") is not True or check.get("actual") != check.get("expected"):
            raise AnchorReceiptError(f"{task_id} contains a non-matching gate check")

    raw_observations = _mapping(record.get("observations"), label=f"{task_id}.observations")
    compact_observations: dict[str, object] = {}
    attempt_root = record_path.parent
    for subject in ("baseline", "golden"):
        gates = _mapping(raw_observations.get(subject), label=f"{task_id}.{subject}")
        compact_observations[subject] = {
            gate: _compact_observation(
                _mapping(gates.get(gate), label=f"{task_id}.{subject}.{gate}"),
                attempt_root=attempt_root,
                subject=subject,
                gate=gate,
            )
            for gate in ("csim", "synth", "cosim", "ppa")
        }

    mutation = validate_mutation_manifest(
        json.loads((task_dir / "mutation_manifest.json").read_text(encoding="utf-8"))
    )
    acceptance = validate_acceptance(
        json.loads((task_dir / "acceptance.json").read_text(encoding="utf-8"))
    )
    if (
        mutation["operator"] != record.get("operator")
        or mutation["seed"] != record.get("seed")
        or mutation["expected_mode"] != record.get("mode")
        or acceptance["expected_mode"] != record.get("mode")
    ):
        raise AnchorReceiptError(f"{task_id} evaluator metadata differs from source record")

    source_run_id = str(anchor["run_id"])
    source_record_ref = str(record_path.relative_to(runs_root / source_run_id))
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "evidence_boundary": {
            "evidence_class": EVIDENCE_CLASS,
            "agent_or_llm_evaluation": False,
            "raw_artifacts_committed": False,
            "claim": (
                "Real Vitis baseline/golden Oracle gate evidence only; "
                "not an LLM or Agent repair/optimization result."
            ),
        },
        "source": {
            "run_id": source_run_id,
            "record_ref": source_record_ref,
            "source_record_sha256": _sha256_file(record_path),
            "source_summary_sha256": _sha256_file(summary_path),
            "record_schema_version": record.get("schema_version"),
        },
        "backend": {
            "name": "vitis",
            "toolchain": summary.get("configuration", {}).get("toolchain_id")
            if isinstance(summary.get("configuration"), dict)
            else None,
            "fingerprint": backend_fingerprint,
            "real_anchor_authorized": True,
            "serial": summary.get("configuration", {}).get("backend") == "vitis"
            and summary.get("execution", {}).get("serial") is True
            if isinstance(summary.get("configuration"), dict)
            and isinstance(summary.get("execution"), dict)
            else False,
        },
        "task": {
            "task_id": task_id,
            "mode": record.get("mode"),
            "operator": record.get("operator"),
            "seed": record.get("seed"),
            "task_fingerprint": task_fingerprint,
        },
        "result": {
            "status": "ACCEPTED",
            "real_anchor": True,
            "gate_scope": record.get("gate_scope"),
            "expected": record.get("expected"),
            "checks": checks,
            "observations": compact_observations,
            "wall_time_seconds": record.get("wall_time_seconds"),
        },
    }
    receipt["artifact_binding"] = _artifact_binding(compact_observations)
    receipt["content_sha256"] = _receipt_content_hash(receipt)
    _assert_portable(receipt)
    return receipt, {
        "run_id": source_run_id,
        "summary_sha256": _sha256_file(summary_path),
        "historical_manifest_sha256": _mapping(
            summary.get("corpus"), label=f"{source_run_id}.corpus"
        ).get("manifest_sha256"),
        "accepted": _mapping(summary.get("counts"), label=f"{source_run_id}.counts").get(
            "accepted"
        ),
        "rejected": _mapping(summary.get("counts"), label=f"{source_run_id}.counts").get(
            "rejected"
        ),
        "attempted": _mapping(
            summary.get("execution"), label=f"{source_run_id}.execution"
        ).get("attempted_this_run"),
    }


def generate_anchor_receipts(
    *,
    corpus_root: Path | str,
    runs_root: Path | str,
    release_path: Path | str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Extract and write a deterministic receipt bundle from retained runs."""

    corpus = Path(corpus_root).resolve()
    runs = Path(runs_root).resolve()
    release_file = Path(release_path).resolve()
    output = Path(output_dir).resolve()
    release = _read_object(release_file, label="anchor release")
    anchors = [
        _mapping(item, label="valid_real_vitis_anchors[]")
        for item in _sequence(
            release.get("valid_real_vitis_anchors"),
            label="valid_real_vitis_anchors",
        )
    ]
    if len(anchors) != 12 or len({item.get("task_id") for item in anchors}) != 12:
        raise AnchorReceiptError("release must select exactly 12 unique anchors")

    output.mkdir(parents=True, exist_ok=True)
    receipts_dir = output / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    source_runs: dict[str, dict[str, object]] = {}
    backend_fingerprints: set[str] = set()
    cut_times: list[str] = []
    for anchor in anchors:
        receipt, source_run = _build_receipt(
            corpus_root=corpus,
            runs_root=runs,
            anchor=anchor,
        )
        task = _mapping(receipt["task"], label="receipt.task")
        source = _mapping(receipt["source"], label="receipt.source")
        backend = _mapping(receipt["backend"], label="receipt.backend")
        result = _mapping(receipt["result"], label="receipt.result")
        artifact_binding = _mapping(
            receipt["artifact_binding"], label="receipt.artifact_binding"
        )
        task_id = str(task["task_id"])
        receipt_path = receipts_dir / f"{task_id}.json"
        _write_json(receipt_path, receipt)
        entries.append(
            {
                "task_id": task_id,
                "mode": task["mode"],
                "run_id": source["run_id"],
                "path": f"receipts/{task_id}.json",
                "receipt_sha256": _sha256_file(receipt_path),
                "content_sha256": receipt["content_sha256"],
                "task_fingerprint": task["task_fingerprint"],
                "source_record_sha256": source["source_record_sha256"],
                "checks": len(_sequence(result["checks"], label="receipt.checks")),
                "artifact_hash_count": artifact_binding["count"],
                "artifact_hash_set_sha256": artifact_binding["set_sha256"],
            }
        )
        source_runs[str(source_run["run_id"])] = source_run
        backend_fingerprints.add(str(backend["fingerprint"]))
        source_record = _read_object(
            _inside(
                runs / str(source["run_id"]),
                _safe_relative(source["record_ref"], label="record_ref"),
                label="source record",
            ),
            label="source record",
        )
        finished_at = source_record.get("finished_at")
        if isinstance(finished_at, str):
            cut_times.append(finished_at)

    if len(backend_fingerprints) != 1:
        raise AnchorReceiptError("selected anchors use more than one backend fingerprint")
    entries.sort(key=lambda item: str(item["task_id"]))
    expected_receipt_names = {f"{item['task_id']}.json" for item in entries}
    for stale in receipts_dir.glob("*.json"):
        if stale.name not in expected_receipt_names:
            stale.unlink()
    mode_counts = Counter(str(item["mode"]) for item in entries)
    if mode_counts != Counter({mode: 3 for mode in EXPECTED_MODES}):
        raise AnchorReceiptError(f"unexpected mode distribution: {dict(mode_counts)}")
    receipt_set_rows = [
        {
            "task_id": item["task_id"],
            "receipt_sha256": item["receipt_sha256"],
            "task_fingerprint": item["task_fingerprint"],
            "source_record_sha256": item["source_record_sha256"],
        }
        for item in entries
    ]
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "receipt_cut_at": max(cut_times) if cut_times else None,
        "evidence_boundary": {
            "evidence_class": EVIDENCE_CLASS,
            "agent_or_llm_evaluation": False,
            "raw_artifacts_committed": False,
            "clean_clone_scope": (
                "Verifies receipt integrity and current task-tree binding; "
                "raw artifact re-hashing requires the retained ignored runs."
            ),
        },
        "backend": {
            "name": "vitis",
            "toolchain": "Vitis 2025.2",
            "fingerprint": next(iter(backend_fingerprints)),
        },
        "corpus": {
            "manifest_ref": "corpus_manifest.json",
            "manifest_sha256": _sha256_file(corpus / "corpus_manifest.json"),
        },
        "counts": {
            "receipts": len(entries),
            "checks": sum(int(item["checks"]) for item in entries),
            "artifact_hashes": sum(int(item["artifact_hash_count"]) for item in entries),
            "by_mode": dict(sorted(mode_counts.items())),
        },
        "source_runs": [source_runs[key] for key in sorted(source_runs)],
        "receipt_set_sha256": _sha256_bytes(_canonical_json(receipt_set_rows)),
        "receipts": entries,
    }
    _assert_portable(manifest)
    _write_json(output / "manifest.json", manifest)
    _bind_release(release_file, output, manifest)
    return manifest


def _bind_release(
    release_path: Path,
    receipt_dir: Path,
    manifest: Mapping[str, object],
) -> None:
    release = _read_object(release_path, label="anchor release")
    entries = {
        str(item["task_id"]): _mapping(item, label="receipt manifest entry")
        for item in _sequence(manifest.get("receipts"), label="receipt manifest receipts")
        if isinstance(item, dict)
    }
    anchors = _sequence(
        release.get("valid_real_vitis_anchors"), label="valid_real_vitis_anchors"
    )
    bound: list[dict[str, object]] = []
    for raw_anchor in anchors:
        anchor = _mapping(raw_anchor, label="valid_real_vitis_anchors[]")
        entry = entries.get(str(anchor.get("task_id")))
        if entry is None:
            raise AnchorReceiptError("release anchor has no receipt")
        anchor.update(
            {
                "backend_fingerprint": _mapping(
                    manifest.get("backend"), label="manifest.backend"
                )["fingerprint"],
                "source_record_sha256": entry["source_record_sha256"],
                "receipt_ref": f"{receipt_dir.name}/{entry['path']}",
                "receipt_sha256": entry["receipt_sha256"],
                "artifact_hash_count": entry["artifact_hash_count"],
                "artifact_hash_set_sha256": entry["artifact_hash_set_sha256"],
            }
        )
        bound.append(anchor)
    release["schema_version"] = RELEASE_SCHEMA_VERSION
    release["receipt_bundle"] = {
        "manifest_ref": f"{receipt_dir.name}/manifest.json",
        "manifest_sha256": _sha256_file(receipt_dir / "manifest.json"),
        "backend_fingerprint": _mapping(
            manifest.get("backend"), label="manifest.backend"
        )["fingerprint"],
        "receipts": len(entries),
        "source_records_hash_bound": len(entries),
        "artifact_hashes": _mapping(
            manifest.get("counts"), label="manifest.counts"
        )["artifact_hashes"],
        "clean_clone_verification": (
            "python -m llm4hls_agent.v3d_anchor_receipts verify "
            "--corpus task_corpus/v3d-fast "
            f"--receipts releases/{receipt_dir.name} "
            f"--release releases/{release_path.name}"
        ),
    }
    release["valid_real_vitis_anchors"] = bound
    _write_json(release_path, release)


def verify_anchor_receipts(
    *,
    corpus_root: Path | str,
    receipts_dir: Path | str,
    release_path: Path | str | None = None,
) -> dict[str, object]:
    """Fail-closed verification requiring only a clean checkout."""

    corpus = Path(corpus_root).resolve()
    receipts = Path(receipts_dir).resolve()
    manifest_path = receipts / "manifest.json"
    manifest = _read_object(manifest_path, label="receipt manifest")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise AnchorReceiptError("unsupported receipt manifest schema")
    if manifest.get("extractor_version") != EXTRACTOR_VERSION:
        raise AnchorReceiptError("unsupported receipt extractor version")
    boundary = _mapping(manifest.get("evidence_boundary"), label="manifest boundary")
    if (
        boundary.get("evidence_class") != EVIDENCE_CLASS
        or boundary.get("agent_or_llm_evaluation") is not False
        or boundary.get("raw_artifacts_committed") is not False
    ):
        raise AnchorReceiptError("receipt manifest crosses its evidence boundary")
    backend = _mapping(manifest.get("backend"), label="manifest backend")
    if (
        backend.get("name") != "vitis"
        or backend.get("toolchain") != "Vitis 2025.2"
        or not _is_sha256(backend.get("fingerprint"))
    ):
        raise AnchorReceiptError("manifest backend provenance is incomplete")
    corpus_binding = _mapping(manifest.get("corpus"), label="manifest corpus")
    if corpus_binding.get("manifest_sha256") != _sha256_file(
        corpus / "corpus_manifest.json"
    ):
        raise AnchorReceiptError("current corpus manifest differs from receipt bundle")

    entries = [
        _mapping(item, label="manifest.receipts[]")
        for item in _sequence(manifest.get("receipts"), label="manifest.receipts")
    ]
    if len(entries) != 12 or len({item.get("task_id") for item in entries}) != 12:
        raise AnchorReceiptError("receipt manifest must contain 12 unique tasks")
    expected_receipt_files = {
        _safe_relative(item.get("path"), label="manifest receipt path").as_posix()
        for item in entries
    }
    actual_receipt_files = {
        path.relative_to(receipts).as_posix()
        for path in receipts.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_receipt_files != expected_receipt_files:
        raise AnchorReceiptError("receipt directory contains missing or unbound files")
    mode_counts: Counter[str] = Counter()
    check_count = 0
    artifact_count = 0
    receipt_set_rows: list[dict[str, object]] = []
    verified_entries: dict[str, dict[str, object]] = {}
    source_run_rows = [
        _mapping(item, label="manifest.source_runs[]")
        for item in _sequence(manifest.get("source_runs"), label="manifest.source_runs")
    ]
    source_runs: dict[str, dict[str, object]] = {}
    for row in source_run_rows:
        run_id = str(row.get("run_id", ""))
        if not run_id or Path(run_id).name != run_id or run_id in source_runs:
            raise AnchorReceiptError("manifest contains an invalid source run")
        if not _is_sha256(row.get("summary_sha256")) or not _is_sha256(
            row.get("historical_manifest_sha256")
        ):
            raise AnchorReceiptError(f"{run_id} source run lacks hash provenance")
        source_runs[run_id] = row
    for entry in entries:
        task_id = str(entry.get("task_id", ""))
        relative = _safe_relative(entry.get("path"), label=f"{task_id}.receipt path")
        receipt_path = _inside(receipts, relative, label=f"{task_id}.receipt")
        if entry.get("receipt_sha256") != _sha256_file(receipt_path):
            raise AnchorReceiptError(f"{task_id} receipt file hash mismatch")
        receipt = _read_object(receipt_path, label=f"{task_id} receipt")
        _assert_portable(receipt)
        if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            raise AnchorReceiptError(f"{task_id} receipt schema mismatch")
        if receipt.get("content_sha256") != _receipt_content_hash(receipt):
            raise AnchorReceiptError(f"{task_id} receipt content hash mismatch")
        if receipt.get("content_sha256") != entry.get("content_sha256"):
            raise AnchorReceiptError(f"{task_id} manifest content binding mismatch")
        receipt_boundary = _mapping(
            receipt.get("evidence_boundary"), label=f"{task_id}.boundary"
        )
        if (
            receipt_boundary.get("evidence_class") != EVIDENCE_CLASS
            or receipt_boundary.get("agent_or_llm_evaluation") is not False
            or receipt_boundary.get("raw_artifacts_committed") is not False
        ):
            raise AnchorReceiptError(f"{task_id} receipt crosses evidence boundary")
        receipt_backend = _mapping(receipt.get("backend"), label=f"{task_id}.backend")
        if (
            receipt_backend.get("name") != "vitis"
            or receipt_backend.get("toolchain") != "Vitis 2025.2"
            or receipt_backend.get("fingerprint") != backend.get("fingerprint")
            or receipt_backend.get("real_anchor_authorized") is not True
            or receipt_backend.get("serial") is not True
        ):
            raise AnchorReceiptError(f"{task_id} backend binding mismatch")
        source = _mapping(receipt.get("source"), label=f"{task_id}.source")
        source_run = source_runs.get(str(source.get("run_id", "")))
        if (
            source_run is None
            or not _is_sha256(source.get("source_record_sha256"))
            or not _is_sha256(source.get("source_summary_sha256"))
            or source.get("source_record_sha256") != entry.get("source_record_sha256")
            or source.get("source_summary_sha256") != source_run.get("summary_sha256")
            or source.get("run_id") != entry.get("run_id")
        ):
            raise AnchorReceiptError(f"{task_id} source record is not hash-bound")
        _safe_relative(source.get("record_ref"), label=f"{task_id}.source.record_ref")
        task = _mapping(receipt.get("task"), label=f"{task_id}.task")
        if task.get("task_id") != task_id or task.get("mode") != entry.get("mode"):
            raise AnchorReceiptError(f"{task_id} task identity mismatch")
        task_dir = corpus / "tasks" / task_id
        if task.get("task_fingerprint") != _tree_fingerprint(task_dir):
            raise AnchorReceiptError(f"{task_id} current task tree has drifted")
        if task.get("task_fingerprint") != entry.get("task_fingerprint"):
            raise AnchorReceiptError(f"{task_id} manifest task binding mismatch")
        mutation = validate_mutation_manifest(
            json.loads((task_dir / "mutation_manifest.json").read_text(encoding="utf-8"))
        )
        acceptance = validate_acceptance(
            json.loads((task_dir / "acceptance.json").read_text(encoding="utf-8"))
        )
        if (
            task.get("operator") != mutation["operator"]
            or task.get("seed") != mutation["seed"]
            or task.get("mode") != mutation["expected_mode"]
            or task.get("mode") != acceptance["expected_mode"]
        ):
            raise AnchorReceiptError(f"{task_id} evaluator metadata binding mismatch")

        result = _mapping(receipt.get("result"), label=f"{task_id}.result")
        if result.get("status") != "ACCEPTED" or result.get("real_anchor") is not True:
            raise AnchorReceiptError(f"{task_id} is not an accepted real anchor")
        checks = _sequence(result.get("checks"), label=f"{task_id}.checks")
        if len(checks) != entry.get("checks"):
            raise AnchorReceiptError(f"{task_id} check count mismatch")
        expected_results = _mapping(
            result.get("expected"), label=f"{task_id}.expected"
        )
        gate_scope = set(
            str(gate)
            for gate in _sequence(result.get("gate_scope"), label=f"{task_id}.gate_scope")
        )
        for subject in ("baseline", "golden"):
            declared = _mapping(
                expected_results.get(subject), label=f"{task_id}.expected.{subject}"
            )
            acceptance_key = f"{subject}_validation"
            for gate, status in _mapping(
                acceptance.get(acceptance_key), label=f"acceptance.{acceptance_key}"
            ).items():
                if gate in gate_scope and declared.get(gate) != status:
                    raise AnchorReceiptError(
                        f"{task_id} expected {subject}.{gate} differs from task acceptance"
                    )
        observations = _mapping(
            result.get("observations"), label=f"{task_id}.observations"
        )
        observed_check_keys: set[tuple[str, str]] = set()
        for raw_check in checks:
            check = _mapping(raw_check, label=f"{task_id}.check")
            if check.get("matches") is not True or check.get("actual") != check.get(
                "expected"
            ):
                raise AnchorReceiptError(f"{task_id} has a failing receipt check")
            subject = str(check.get("subject", ""))
            gate = str(check.get("gate", ""))
            key = (subject, gate)
            if key in observed_check_keys or subject not in {"baseline", "golden"}:
                raise AnchorReceiptError(f"{task_id} has an invalid duplicate check")
            observed_check_keys.add(key)
            subject_observations = _mapping(
                observations.get(subject), label=f"{task_id}.{subject}"
            )
            gate_observation = _mapping(
                subject_observations.get(gate), label=f"{task_id}.{subject}.{gate}"
            )
            declared_expected = _mapping(
                expected_results.get(subject), label=f"{task_id}.expected.{subject}"
            )
            if (
                gate_observation.get("status") != check.get("actual")
                or declared_expected.get(gate) != check.get("expected")
            ):
                raise AnchorReceiptError(f"{task_id} check/result binding mismatch")
        artifact_binding = _artifact_binding(observations)
        declared_binding = _mapping(
            receipt.get("artifact_binding"), label=f"{task_id}.artifact_binding"
        )
        if artifact_binding != declared_binding:
            raise AnchorReceiptError(f"{task_id} artifact-set binding mismatch")
        if (
            artifact_binding.get("count") != entry.get("artifact_hash_count")
            or artifact_binding.get("set_sha256")
            != entry.get("artifact_hash_set_sha256")
        ):
            raise AnchorReceiptError(f"{task_id} manifest artifact binding mismatch")
        for subject in ("baseline", "golden"):
            gates = _mapping(observations.get(subject), label=f"{task_id}.{subject}")
            for gate in ("csim", "synth", "cosim"):
                observation = _mapping(
                    gates.get(gate), label=f"{task_id}.{subject}.{gate}"
                )
                hashes = _mapping(
                    observation.get("artifact_hashes", {}),
                    label=f"{task_id}.{subject}.{gate}.artifact_hashes",
                )
                if observation.get("status") != "NOT_RUN" and not hashes:
                    raise AnchorReceiptError(f"{task_id}.{subject}.{gate} lacks hashes")
                if any(not _is_sha256(value) for value in hashes.values()):
                    raise AnchorReceiptError(f"{task_id}.{subject}.{gate} has bad hashes")

        mode_counts[str(task["mode"])] += 1
        check_count += len(checks)
        artifact_count += int(artifact_binding["count"])
        receipt_set_rows.append(
            {
                "task_id": task_id,
                "receipt_sha256": entry["receipt_sha256"],
                "task_fingerprint": entry["task_fingerprint"],
                "source_record_sha256": entry["source_record_sha256"],
            }
        )
        verified_entries[task_id] = entry

    if mode_counts != Counter({mode: 3 for mode in EXPECTED_MODES}):
        raise AnchorReceiptError("receipt mode coverage is incomplete")
    receipt_set_rows.sort(key=lambda item: str(item["task_id"]))
    if manifest.get("receipt_set_sha256") != _sha256_bytes(
        _canonical_json(receipt_set_rows)
    ):
        raise AnchorReceiptError("receipt-set hash mismatch")
    counts = _mapping(manifest.get("counts"), label="manifest counts")
    if counts != {
        "receipts": 12,
        "checks": check_count,
        "artifact_hashes": artifact_count,
        "by_mode": dict(sorted(mode_counts.items())),
    }:
        raise AnchorReceiptError("receipt manifest counts mismatch")

    if release_path is not None:
        release_file = Path(release_path).resolve()
        release = _read_object(release_file, label="anchor release")
        if release.get("schema_version") != RELEASE_SCHEMA_VERSION:
            raise AnchorReceiptError("anchor release is not receipt-bound v3")
        bundle = _mapping(release.get("receipt_bundle"), label="release receipt bundle")
        if (
            bundle.get("manifest_ref") != f"{receipts.name}/manifest.json"
            or bundle.get("manifest_sha256") != _sha256_file(manifest_path)
            or bundle.get("backend_fingerprint") != backend.get("fingerprint")
            or bundle.get("receipts") != 12
            or bundle.get("source_records_hash_bound") != 12
            or bundle.get("artifact_hashes") != artifact_count
        ):
            raise AnchorReceiptError("release/receipt manifest binding mismatch")
        release_anchors = [
            _mapping(item, label="release anchor")
            for item in _sequence(
                release.get("valid_real_vitis_anchors"),
                label="release valid anchors",
            )
        ]
        if {str(item.get("task_id")) for item in release_anchors} != set(
            verified_entries
        ):
            raise AnchorReceiptError("release and receipt task sets differ")
        for anchor in release_anchors:
            entry = verified_entries[str(anchor["task_id"])]
            expected = {
                "backend_fingerprint": backend["fingerprint"],
                "source_record_sha256": entry["source_record_sha256"],
                "receipt_ref": f"{receipts.name}/{entry['path']}",
                "receipt_sha256": entry["receipt_sha256"],
                "artifact_hash_count": entry["artifact_hash_count"],
                "artifact_hash_set_sha256": entry["artifact_hash_set_sha256"],
            }
            if any(anchor.get(key) != value for key, value in expected.items()):
                raise AnchorReceiptError(f"release binding mismatch for {anchor['task_id']}")

    return {
        "status": "PASS",
        "evidence_class": EVIDENCE_CLASS,
        "receipts": 12,
        "checks": check_count,
        "artifact_hashes": artifact_count,
        "task_tree_matches": 12,
        "backend_fingerprint": backend["fingerprint"],
        "release_bound": release_path is not None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="extract receipts from retained runs")
    generate.add_argument("--corpus", type=Path, required=True)
    generate.add_argument("--runs-root", type=Path, required=True)
    generate.add_argument("--release", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="verify receipts in a clean checkout")
    verify.add_argument("--corpus", type=Path, required=True)
    verify.add_argument("--receipts", type=Path, required=True)
    verify.add_argument("--release", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            result = generate_anchor_receipts(
                corpus_root=arguments.corpus,
                runs_root=arguments.runs_root,
                release_path=arguments.release,
                output_dir=arguments.output,
            )
        else:
            result = verify_anchor_receipts(
                corpus_root=arguments.corpus,
                receipts_dir=arguments.receipts,
                release_path=arguments.release,
            )
    except AnchorReceiptError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    main_entry()
