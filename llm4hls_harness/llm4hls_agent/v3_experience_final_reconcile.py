"""Reconcile missing Experience V2 final labels from already-sealed run evidence.

This utility never mutates the frozen input store.  When a record's Candidate
is exactly the run's final Candidate and the package manifest binds the fresh
final tool results, it emits a new immutable record and replaces the old record
only in a new derived store.  No LLM or Vitis call is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .v3_experience import canonical_json
from .v3_experience_v2 import seal_experience_v2, validate_experience_v2


FINAL_RECONCILE_SCHEMA = "v3e.experience-final-reconcile.v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON object required: {path}")
    return dict(value)


def _manifest_index(search_roots: Sequence[Path]) -> dict[str, list[Path]]:
    output: dict[str, list[Path]] = defaultdict(list)
    for root in search_roots:
        for path in root.resolve().rglob("control/package_manifest.json"):
            output[_sha256(path)].append(path)
    return dict(output)


def _manifest_digest(record: Mapping[str, object]) -> str | None:
    refs = record["provenance"]["artifact_refs"]
    for ref in refs:
        if ref["role"] == "package_manifest":
            return str(ref["sha256"])
    return None


def _status(value: object) -> str:
    if not isinstance(value, Mapping):
        return "NOT_RUN"
    status = value.get("status")
    return str(status) if status in {"PASS", "FAIL", "NOT_RUN"} else "NOT_RUN"


def _worst_latency(report: object) -> float | None:
    if not isinstance(report, Mapping):
        return None
    latency = report.get("latency")
    if not isinstance(latency, Mapping):
        return None
    for name in ("worst", "max", "average", "best", "min"):
        value = latency.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _max_interval(report: object) -> float | None:
    if not isinstance(report, Mapping):
        return None
    interval = report.get("interval")
    if not isinstance(interval, Mapping):
        return None
    for name in ("max", "worst", "average", "min", "best"):
        value = interval.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _candidate_code_hash(run_root: Path, candidate_id: str) -> str:
    registry = _read_json(run_root / "candidate_registry.json")
    candidates = registry.get("candidates")
    if not isinstance(candidates, Mapping):
        raise ValueError("candidate registry has no candidates")
    candidate = candidates.get(candidate_id)
    if not isinstance(candidate, Mapping):
        raise ValueError("final Candidate is absent from registry")
    code_hash = candidate.get("code_hash")
    if not isinstance(code_hash, str) or len(code_hash) != 64:
        raise ValueError("final Candidate code hash is invalid")
    return code_hash


def _bound_final_results(
    run_root: Path,
    package_manifest: Mapping[str, object],
    result: Mapping[str, object],
    *,
    candidate_id: str,
) -> dict[str, dict[str, object]]:
    artifacts = package_manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("package manifest artifacts are invalid")
    bound = {
        str(item["path"]): str(item["sha256"])
        for item in artifacts
        if isinstance(item, Mapping)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha256"), str)
    }
    final = result.get("final_validation")
    if not isinstance(final, Mapping):
        raise ValueError("run result has no final validation")
    code_hash = _candidate_code_hash(run_root, candidate_id)
    output: dict[str, dict[str, object]] = {}
    for kind in ("csim", "synth", "cosim"):
        item = final.get(kind)
        if not isinstance(item, Mapping) or item.get("status") == "NOT_RUN":
            output[kind] = {"status": "NOT_RUN"}
            continue
        ref = item.get("result_ref")
        if not isinstance(ref, str) or ref not in bound:
            raise ValueError(f"final {kind} result is not package-bound")
        path = run_root / ref
        digest = _sha256(path)
        if digest != bound[ref]:
            raise ValueError(f"final {kind} result hash mismatch")
        value = _read_json(path)
        if value.get("candidate_id") != candidate_id:
            raise ValueError(f"final {kind} Candidate mismatch")
        if value.get("code_hash") != code_hash:
            raise ValueError(f"final {kind} code hash mismatch")
        if value.get("validation_scope") != "final":
            raise ValueError(f"final {kind} scope mismatch")
        output[kind] = {
            "status": "PASS" if value.get("ok") is True else "FAIL",
            "ref": ref,
            "sha256": digest,
            "elapsed_s": float(value.get("elapsed_s") or 0.0),
            "report": value.get("report"),
        }
    return output


def _reconciled_record(
    record: Mapping[str, object],
    *,
    run_root: Path,
    package_manifest_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    result_path = run_root / "v3_prototype_result.json"
    result = _read_json(result_path)
    candidate_id = str(record["source"]["candidate_id"])
    if result.get("final_candidate_id") != candidate_id:
        raise ValueError("record Candidate is not the run's final Candidate")
    package = _read_json(package_manifest_path)
    stages = _bound_final_results(
        run_root, package, result, candidate_id=candidate_id
    )
    csim = str(stages["csim"]["status"])
    synth = str(stages["synth"]["status"])
    cosim = str(stages["cosim"]["status"])
    requires_cosim = bool(record["problem"]["requires_cosim"])
    complete = csim in {"PASS", "FAIL"} and synth in {"PASS", "FAIL"}
    if requires_cosim:
        complete = complete and cosim in {"PASS", "FAIL"}
    if not complete:
        raise ValueError("run final validation is not terminal for this contract")
    passed = bool(
        csim == "PASS"
        and synth == "PASS"
        and (cosim == "PASS" if requires_cosim else cosim != "FAIL")
    )
    body = json.loads(canonical_json(record).decode("utf-8"))
    old_record_id = str(body["record_id"])
    body["record_id"] = ""
    validation = body["validation"]
    validation.update(
        {
            "csim_status": csim,
            "synth_status": synth,
            "cosim_status": cosim,
            "fresh_final_status": "PASS" if passed else "FAIL",
            "promoted": passed,
            "rejected": not passed,
            "failure_reason": None if passed else "RECONCILED_FINAL_FAILED",
        }
    )
    synth_report = stages["synth"].get("report")
    latency_after = _worst_latency(synth_report)
    interval_after = _max_interval(synth_report)
    performance = body["performance"]
    if latency_after is not None:
        performance["latency_after"] = latency_after
    if interval_after is not None:
        performance["transaction_interval_after"] = interval_after
    before = performance.get("latency_before")
    if (
        body["problem"]["mode"] == "OPTIMIZE"
        and isinstance(before, (int, float))
        and not isinstance(before, bool)
        and latency_after is not None
        and latency_after > 0
    ):
        performance["acceleration"] = float(before) / latency_after
        performance["strict_improvement"] = bool(
            passed and latency_after < float(before)
        )
    cost = body["cost"]
    credits = {"csim": 1, "synth": 4, "cosim": 20}
    for kind in ("csim", "synth", "cosim"):
        if stages[kind]["status"] != "NOT_RUN":
            field = f"{kind}_calls"
            cost[field] = int(cost[field]) + 1
            cost["credits"] = int(cost["credits"]) + credits[kind]
            cost["wall_time_seconds"] = float(
                cost["wall_time_seconds"]
            ) + float(stages[kind].get("elapsed_s") or 0.0)
    refs = list(body["provenance"]["artifact_refs"])
    logical_prefix = f"source-run/{run_root.name}"
    for kind in ("csim", "synth", "cosim"):
        stage = stages[kind]
        if stage["status"] == "NOT_RUN":
            continue
        refs.append(
            {
                "role": f"reconciled_final_{kind}",
                "ref": f"{logical_prefix}/{stage['ref']}",
                "sha256": stage["sha256"],
            }
        )
    refs.append(
        {
            "role": "reconciled_source_result",
            "ref": f"{logical_prefix}/v3_prototype_result.json",
            "sha256": _sha256(result_path),
        }
    )
    body["provenance"]["artifact_refs"] = refs[:32]
    body["provenance"]["artifact_hashes"] = [
        str(item["sha256"]) for item in refs[:32]
    ]
    body["provenance"]["source_record_hash"] = old_record_id
    sealed = seal_experience_v2(body)
    audit = {
        "old_record_id": old_record_id,
        "new_record_id": sealed["record_id"],
        "mode": sealed["problem"]["mode"],
        "source_run": run_root.name,
        "candidate_id": candidate_id,
        "final_status": sealed["validation"]["fresh_final_status"],
        "stage_status": {
            kind: stages[kind]["status"]
            for kind in ("csim", "synth", "cosim")
        },
    }
    return sealed, audit


def reconcile(
    records: Sequence[Mapping[str, object]],
    groups: Mapping[str, Mapping[str, str]],
    *,
    search_roots: Sequence[Path],
) -> tuple[list[dict[str, object]], dict[str, dict[str, str]], dict[str, object]]:
    index = _manifest_index(search_roots)
    output: list[dict[str, object]] = []
    output_groups: dict[str, dict[str, str]] = {}
    audits: list[dict[str, object]] = []
    reasons: Counter[str] = Counter()
    for raw in records:
        record = validate_experience_v2(raw)
        old_id = str(record["record_id"])
        replacement = record
        if (
            record["provenance"]["eligible_for_ranking"] is True
            and record["validation"]["fresh_final_status"] == "NOT_RUN"
        ):
            digest = _manifest_digest(record)
            paths = index.get(digest or "", [])
            if not paths:
                reasons["PACKAGE_MANIFEST_NOT_FOUND"] += 1
            else:
                successes = []
                for manifest_path in paths:
                    run_root = manifest_path.parents[1]
                    try:
                        successes.append(
                            _reconciled_record(
                                record,
                                run_root=run_root,
                                package_manifest_path=manifest_path,
                            )
                        )
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                if not successes:
                    reasons["NO_BOUND_FINAL_FOR_CANDIDATE"] += 1
                else:
                    unique = {
                        str(item[0]["record_id"]): item for item in successes
                    }
                    if len(unique) != 1:
                        raise ValueError(
                            "multiple source runs disagree on reconciliation"
                        )
                    replacement, audit = next(iter(unique.values()))
                    audits.append(audit)
        output.append(replacement)
        source_group = groups.get(old_id)
        if not isinstance(source_group, Mapping):
            raise ValueError(f"audit group missing for {old_id}")
        output_groups[str(replacement["record_id"])] = {
            "source_v1_record_id": str(
                source_group["source_v1_record_id"]
            ),
            "task_audit_hash": str(source_group["task_audit_hash"]),
        }
    output.sort(key=lambda item: str(item["record_id"]))
    if len({str(item["record_id"]) for item in output}) != len(output):
        raise ValueError("reconciled store contains duplicate record IDs")
    mode_counts = Counter(
        str(item["problem"]["mode"])
        for item in output
        if item["provenance"]["eligible_for_ranking"] is True
        and item["validation"]["fresh_final_status"] in {"PASS", "FAIL"}
    )
    report = {
        "schema_version": FINAL_RECONCILE_SCHEMA,
        "input_records": len(records),
        "output_records": len(output),
        "replacement_count": len(audits),
        "replacements": sorted(
            audits, key=lambda item: str(item["old_record_id"])
        ),
        "unreconciled_reasons": dict(sorted(reasons.items())),
        "verified_ranking_records_by_mode": dict(sorted(mode_counts.items())),
        "llm_calls": 0,
        "vitis_calls": 0,
        "frozen_input_mutated": False,
    }
    return output, output_groups, report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--audit-groups", type=Path, required=True)
    parser.add_argument("--search-root", type=Path, action="append", required=True)
    parser.add_argument("--output-records", type=Path, required=True)
    parser.add_argument("--output-groups", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    args = parser.parse_args(argv)
    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    groups_value = _read_json(args.audit_groups)
    groups = groups_value.get("groups")
    if not isinstance(groups, Mapping):
        raise ValueError("audit groups are invalid")
    output, output_groups, report = reconcile(
        records, groups, search_roots=args.search_root
    )
    args.output_records.parent.mkdir(parents=True, exist_ok=True)
    args.output_records.write_bytes(
        b"".join(canonical_json(item) + b"\n" for item in output)
    )
    args.output_groups.write_bytes(
        canonical_json(
            {
                "schema_version": "v3e.reconciled-audit-groups.v1",
                "groups": output_groups,
                "retrieval_feature": False,
            }
        )
        + b"\n"
    )
    report["output_records_sha256"] = _sha256(args.output_records)
    report["output_groups_sha256"] = _sha256(args.output_groups)
    args.output_report.write_bytes(canonical_json(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FINAL_RECONCILE_SCHEMA", "reconcile"]
