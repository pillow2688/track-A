"""Run fresh Vitis final audits for unresolved public Experience Candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .budget import BudgetConfig, BudgetLedger
from .task import load_public_task
from .tools import ToolConfig, ToolResult, ToolServer
from .v3_experience import canonical_json
from .v3_experience_v2 import seal_experience_v2, validate_experience_v2
from .vitis import VitisBackend, detect_vitis_toolchain


CANDIDATE_AUDIT_SCHEMA = "v3e.experience-candidate-final-audit.v1"


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
    for ref in record["provenance"]["artifact_refs"]:
        if ref["role"] == "package_manifest":
            return str(ref["sha256"])
    return None


def _source_path(run_root: Path, candidate_id: str) -> Path:
    registry = _read_json(run_root / "candidate_registry.json")
    candidates = registry.get("candidates")
    if not isinstance(candidates, Mapping):
        raise ValueError("candidate registry is invalid")
    candidate = candidates.get(candidate_id)
    if not isinstance(candidate, Mapping):
        raise ValueError("Candidate is absent from registry")
    source = candidate.get("source")
    if isinstance(source, Mapping):
        reference = source.get("ref")
        digest = source.get("sha256")
    else:
        reference = candidate.get("source_ref")
        digest = candidate.get("code_hash")
    if (
        not isinstance(reference, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("Candidate source binding is invalid")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Candidate source escapes its run")
    path = (run_root / relative).resolve()
    path.relative_to(run_root.resolve())
    if _sha256(path) != digest:
        raise ValueError("Candidate source hash mismatch")
    return path


def _task_dir(run_root: Path) -> Path:
    benchmark = _read_json(run_root / "benchmark_run.json")
    task_dir = benchmark.get("task_dir")
    if not isinstance(task_dir, str):
        raise ValueError("benchmark run has no public task directory")
    path = Path(task_dir).resolve()
    if any(
        part.casefold()
        in {"hidden", "hidden_like", "golden", "reference", "answer"}
        for part in path.parts
    ):
        raise ValueError("audit task directory is forbidden")
    return path


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


def _audit_one(
    record: Mapping[str, object],
    *,
    source_run_root: Path,
    audit_root: Path,
    vitis_root: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    candidate_id = str(record["source"]["candidate_id"])
    source_path = _source_path(source_run_root, candidate_id)
    kernel_bytes = source_path.read_bytes()
    task = load_public_task(_task_dir(source_run_root))
    audit_root.mkdir(parents=True, exist_ok=True)
    config = BudgetConfig(
        credit_limit=25,
        costs={"csim": 1, "synth": 4, "cosim": 20},
        tool_limits={"csim": 1, "synth": 1, "cosim": 1},
        token_limit=0,
        runtime_limit_seconds=3600.0,
    )
    ledger = BudgetLedger(audit_root / "budget_ledger.jsonl", config)
    server = ToolServer(
        task=task,
        budget=ledger,
        run_root=audit_root,
        config=ToolConfig(
            vitis_root=str(vitis_root),
            part=task.part,
            clock_ns=task.clock_ns,
            timeouts={"csim": 300.0, "synth": 900.0, "cosim": 900.0},
            toolchain_id="Vitis 2025.2",
        ),
        backend=VitisBackend(),
    )
    results: dict[str, ToolResult | None] = {}
    results["csim"] = server.csim(
        kernel_bytes,
        candidate_id=candidate_id,
        validation_scope="search_closeout",
    )
    if results["csim"].ok:
        results["synth"] = server.synth(
            kernel_bytes,
            candidate_id=candidate_id,
            validation_scope="search_closeout",
        )
    else:
        results["synth"] = None
    if results["csim"].ok and results["synth"] is not None and results["synth"].ok:
        results["cosim"] = server.cosim(
            kernel_bytes,
            candidate_id=candidate_id,
            validation_scope="search_closeout",
        )
    else:
        results["cosim"] = None
    statuses = {
        kind: (
            "PASS"
            if result is not None and result.ok
            else "FAIL"
            if result is not None
            else "NOT_RUN"
        )
        for kind, result in results.items()
    }
    passed = all(statuses[kind] == "PASS" for kind in ("csim", "synth", "cosim"))
    body = json.loads(canonical_json(record).decode("utf-8"))
    old_record_id = str(body["record_id"])
    body["record_id"] = ""
    body["validation"].update(
        {
            "csim_status": statuses["csim"],
            "synth_status": statuses["synth"],
            "cosim_status": statuses["cosim"],
            "fresh_final_status": "PASS" if passed else "FAIL",
            "promoted": bool(passed and body["validation"]["promoted"]),
            "rejected": not passed or bool(body["validation"]["rejected"]),
            "failure_reason": (
                None if passed else "FRESH_FULL_INTERNAL_AUDIT_FAILED"
            ),
        }
    )
    synth = results["synth"]
    latency_after = _worst_latency(synth.report if synth is not None else None)
    interval_after = _max_interval(synth.report if synth is not None else None)
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
    refs = list(body["provenance"]["artifact_refs"])
    for kind, result in results.items():
        if result is None:
            continue
        cost[f"{kind}_calls"] = int(cost[f"{kind}_calls"]) + 1
        cost["credits"] = int(cost["credits"]) + credits[kind]
        cost["wall_time_seconds"] = float(cost["wall_time_seconds"]) + float(
            result.elapsed_s
        )
        result_path = audit_root / result.result_ref
        refs.append(
            {
                "role": f"fresh_audit_{kind}",
                "ref": f"audits/{audit_root.name}/{result.result_ref}",
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
        "schema_version": CANDIDATE_AUDIT_SCHEMA,
        "old_record_id": old_record_id,
        "new_record_id": sealed["record_id"],
        "source_run": source_run_root.name,
        "candidate_id": candidate_id,
        "mode": sealed["problem"]["mode"],
        "stage_status": statuses,
        "fresh_final_status": sealed["validation"]["fresh_final_status"],
        "strict_improvement": sealed["performance"]["strict_improvement"],
        "budget": ledger.snapshot(),
        "llm_calls": 0,
    }
    (audit_root / "audit_result.json").write_bytes(canonical_json(audit) + b"\n")
    return sealed, audit


def audit_candidates(
    records: Sequence[Mapping[str, object]],
    groups: Mapping[str, Mapping[str, str]],
    *,
    search_roots: Sequence[Path],
    output_root: Path,
    vitis_root: Path,
) -> tuple[list[dict[str, object]], dict[str, dict[str, str]], dict[str, object]]:
    output_root = output_root.resolve()
    toolchain = detect_vitis_toolchain(vitis_root)
    if toolchain.preflight_result != "READY":
        raise ValueError("Vitis toolchain is not READY")
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
            paths = index.get(_manifest_digest(record) or "", [])
            candidates: list[Path] = []
            for manifest in paths:
                run_root = manifest.parents[1]
                result_path = run_root / "v3_prototype_result.json"
                if not result_path.is_file():
                    continue
                result = _read_json(result_path)
                if result.get("final_candidate_id") != record["source"]["candidate_id"]:
                    candidates.append(run_root)
            if not candidates:
                reasons["NO_LOCATED_NONFINAL_CANDIDATE"] += 1
            else:
                unique = {str(path.resolve()): path for path in candidates}
                if len(unique) != 1:
                    raise ValueError("unresolved record maps to multiple source runs")
                source_run = next(iter(unique.values()))
                replacement, audit = _audit_one(
                    record,
                    source_run_root=source_run,
                    audit_root=output_root / "audits" / old_id[:16],
                    vitis_root=vitis_root,
                )
                audits.append(audit)
        output.append(replacement)
        group = groups.get(old_id)
        if not isinstance(group, Mapping):
            raise ValueError(f"audit group missing for {old_id}")
        output_groups[str(replacement["record_id"])] = {
            "source_v1_record_id": str(group["source_v1_record_id"]),
            "task_audit_hash": str(group["task_audit_hash"]),
        }
    output.sort(key=lambda item: str(item["record_id"]))
    verified = Counter(
        str(record["problem"]["mode"])
        for record in output
        if record["provenance"]["eligible_for_ranking"] is True
        and record["validation"]["fresh_final_status"] in {"PASS", "FAIL"}
    )
    report = {
        "schema_version": "v3e.experience-candidate-audit-batch.v1",
        "toolchain": {
            key: value
            for key, value in toolchain.to_dict().items()
            if key != "vitis_root"
        },
        "audit_count": len(audits),
        "audits": audits,
        "unresolved_reasons": dict(sorted(reasons.items())),
        "verified_ranking_records_by_mode": dict(sorted(verified.items())),
        "llm_calls": 0,
    }
    return output, output_groups, report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--audit-groups", type=Path, required=True)
    parser.add_argument("--search-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--vitis-root", type=Path, required=True)
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
    args.output_root = args.output_root.resolve()
    args.output_root.mkdir(parents=True, exist_ok=True)
    output, output_groups, report = audit_candidates(
        records,
        groups,
        search_roots=args.search_root,
        output_root=args.output_root,
        vitis_root=args.vitis_root,
    )
    records_path = args.output_root / "experience-v2-audited.jsonl"
    groups_path = args.output_root / "experience-v2-audited-audit-groups.json"
    report_path = args.output_root / "candidate-audit-batch-report.json"
    records_path.write_bytes(
        b"".join(canonical_json(item) + b"\n" for item in output)
    )
    groups_path.write_bytes(
        canonical_json(
            {
                "schema_version": "v3e.audited-audit-groups.v1",
                "groups": output_groups,
                "retrieval_feature": False,
            }
        )
        + b"\n"
    )
    report["records_sha256"] = _sha256(records_path)
    report["groups_sha256"] = _sha256(groups_path)
    report_path.write_bytes(canonical_json(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CANDIDATE_AUDIT_SCHEMA", "audit_candidates"]
