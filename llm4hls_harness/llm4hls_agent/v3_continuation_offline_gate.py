"""Offline replay Gate generator for the current Continuation V3 policy.

The generator reads only existing public run artifacts.  It never calls a
model, a tool backend, or Vitis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from .v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_DECISION_SCHEMA,
    CONTINUATION_FIXED_THRESHOLDS,
    CONTINUATION_GATE_SCHEMA,
    CONTINUATION_MODES,
    CONTINUATION_POLICY_VERSION,
    CONTINUATION_PROTOCOL_VERSION,
    current_repository_commit,
    validate_continuation_admission,
    validate_continuation_gate,
)
from .v3_continuation_v2 import canonical_sha256, continuation_decision_v2


REPLAY_MANIFEST_SCHEMA = "v3.continuation-offline-replay-manifest.v1"
_FUTURE_FIELDS = {
    "actual_followup",
    "final_result",
    "future_candidate",
    "outcome",
}
_POSITIVE_OUTCOMES = {
    "ESSENTIAL_FOR_CORRECTNESS",
    "BENEFICIAL_PERFORMANCE",
}


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _safe_source_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("offline replay source path is invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("offline replay source path must be repository-relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("offline replay source path escapes the harness") from exc
    if not path.is_file():
        raise ValueError(f"offline replay source is missing: {relative}")
    return path


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _migrate_historical_pre_state(
    value: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    """Apply only the documented search-reserve field rename."""

    migrated = dict(value)
    migrations: list[str] = []
    if (
        "search_closeout_reserve_available" not in migrated
        and "final_reserve_available" in migrated
    ):
        migrated["search_closeout_reserve_available"] = bool(
            migrated.pop("final_reserve_available")
        )
        migrations.append(
            "final_reserve_available->search_closeout_reserve_available"
        )
    return migrated, migrations


def _outcome(
    *,
    pre_state: Mapping[str, object],
    candidate: Mapping[str, object],
    previous_candidate: Mapping[str, object] | None,
) -> str:
    decision = str(candidate.get("decision") or "")
    if (
        decision in {"FINAL_VERIFIED", "CORRECTNESS_VERIFIED", "PROMOTED"}
        and not bool(pre_state.get("has_verified_incumbent"))
    ):
        return "ESSENTIAL_FOR_CORRECTNESS"
    latency = _positive_number(candidate.get("latency_worst"))
    previous_latency = _positive_number(
        previous_candidate.get("latency_worst")
        if previous_candidate is not None
        else None
    )
    if (
        latency is not None
        and previous_latency is not None
        and latency < previous_latency
    ):
        return "BENEFICIAL_PERFORMANCE"
    if decision.startswith("REJECTED"):
        return "HARMFUL"
    return "WASTEFUL"


def _source_gate(
    *,
    run_root: Path,
    result: Mapping[str, object],
    round_index: int,
) -> tuple[Path, str]:
    for item in result.get("planner_call_gates", ()):
        if not isinstance(item, Mapping) or item.get("round") != round_index:
            continue
        ref = item.get("ref")
        digest = item.get("sha256")
        if not isinstance(ref, str) or not isinstance(digest, str):
            break
        path = (run_root / ref).resolve()
        try:
            path.relative_to(run_root)
        except ValueError as exc:
            raise ValueError("historical Planner Gate path is unsafe") from exc
        if not path.is_file():
            raise ValueError("historical Planner Gate is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("historical Planner Gate hash mismatch")
        return path, digest
    raise ValueError(f"round {round_index} has no hash-bound Planner Gate")


def _replay_sample(
    *,
    harness_root: Path,
    source: Mapping[str, object],
) -> dict[str, object]:
    if set(source) != {"run_result_path", "round_index"}:
        raise ValueError("offline replay sample fields mismatch")
    result_path = _safe_source_path(harness_root, source.get("run_result_path"))
    round_index = source.get("round_index")
    if (
        isinstance(round_index, bool)
        or not isinstance(round_index, int)
        or round_index < 1
    ):
        raise ValueError("offline replay round index is invalid")
    result = _object(result_path)
    backend = result.get("backend")
    if (
        not isinstance(backend, Mapping)
        or backend.get("evidence_level") != "REAL_VITIS_VALIDATED"
        or not str(backend.get("class", "")).endswith(".VitisBackend")
    ):
        raise ValueError("offline replay requires a real validated Vitis artifact")
    task_id = result.get("task_id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or any(
            term in task_id.lower()
            for term in ("hidden", "reference", "golden")
        )
    ):
        raise ValueError("offline replay task provenance is not public")
    mode = result.get("mode")
    if mode not in CONTINUATION_MODES:
        raise ValueError("offline replay mode is invalid")

    run_root = result_path.parent
    source_gate_path, source_gate_sha256 = _source_gate(
        run_root=run_root,
        result=result,
        round_index=round_index,
    )
    source_gate = _object(source_gate_path)
    raw_pre_state = source_gate.get("pre_state")
    if not isinstance(raw_pre_state, Mapping):
        raise ValueError("historical Planner Gate has no pre-state")
    leakage = sum(name in raw_pre_state for name in _FUTURE_FIELDS)
    pre_state, migrations = _migrate_historical_pre_state(raw_pre_state)
    current_decision = continuation_decision_v2(
        mode=str(mode),
        pre_state=pre_state,
    )
    current_decision["policy_version"] = CONTINUATION_POLICY_VERSION

    candidates = {
        item.get("round"): item
        for item in result.get("candidate_rounds", ())
        if isinstance(item, Mapping) and isinstance(item.get("round"), int)
    }
    candidate = candidates.get(round_index)
    if not isinstance(candidate, Mapping):
        raise ValueError("offline replay has no same-round Candidate outcome")
    previous_candidate = candidates.get(round_index - 1)
    previous_candidate = (
        previous_candidate
        if isinstance(previous_candidate, Mapping)
        else None
    )
    outcome = _outcome(
        pre_state=pre_state,
        candidate=candidate,
        previous_candidate=previous_candidate,
    )
    relative_result = result_path.relative_to(harness_root)
    relative_gate = source_gate_path.relative_to(harness_root)
    return {
        "sample_id": canonical_sha256(
            {
                "run_result_path": str(relative_result),
                "round_index": round_index,
                "source_gate_sha256": source_gate_sha256,
            }
        ),
        "task_id": task_id,
        "mode": mode,
        "round_index": round_index,
        "source": {
            "kind": "REAL_VITIS_HISTORICAL_ARTIFACT",
            "run_result_path": str(relative_result),
            "run_result_sha256": hashlib.sha256(
                result_path.read_bytes()
            ).hexdigest(),
            "planner_gate_path": str(relative_gate),
            "planner_gate_sha256": source_gate_sha256,
            "stored_policy_version": source_gate.get("policy_version"),
        },
        "pre_state_sha256": canonical_sha256(pre_state),
        "pre_state_migrations": migrations,
        "current_decision": current_decision,
        "outcome": outcome,
        "candidate_outcome": {
            "candidate_id": candidate.get("candidate_id"),
            "decision": candidate.get("decision"),
            "latency_worst": candidate.get("latency_worst"),
        },
        "leakage_violations": leakage,
    }


def build_offline_gate(
    manifest: Mapping[str, object],
    *,
    harness_root: str | Path,
    current_commit: str | None = None,
) -> dict[str, object]:
    """Replay all configured real artifacts through the current V3 policy."""

    if set(manifest) != {"schema_version", "protocol_version", "samples"}:
        raise ValueError("offline replay manifest fields mismatch")
    if manifest.get("schema_version") != REPLAY_MANIFEST_SCHEMA:
        raise ValueError("unsupported offline replay manifest schema")
    if manifest.get("protocol_version") != CONTINUATION_PROTOCOL_VERSION:
        raise ValueError("offline replay protocol version mismatch")
    sources = manifest.get("samples")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise ValueError("offline replay samples are invalid")
    root = Path(harness_root).expanduser().resolve()
    audited = [
        _replay_sample(harness_root=root, source=source)
        for source in sources
        if isinstance(source, Mapping)
    ]
    if len(audited) != len(sources):
        raise ValueError("offline replay sample is not an object")

    mode_coverage = Counter(str(item["mode"]) for item in audited)
    false_blocks = sum(
        item["outcome"] in _POSITIVE_OUTCOMES
        and item["current_decision"]["decision"] != "ALLOW"
        for item in audited
    )
    leakage = sum(int(item["leakage_violations"]) for item in audited)
    failures: list[str] = []
    if any(
        mode_coverage[mode]
        < CONTINUATION_FIXED_THRESHOLDS["minimum_samples_per_mode"]
        for mode in CONTINUATION_MODES
    ):
        failures.append("PER_MODE_SAMPLE_GATE")
    if (
        false_blocks
        > CONTINUATION_FIXED_THRESHOLDS["maximum_false_blocks"]
    ):
        failures.append("FALSE_BLOCK_GATE")
    if (
        leakage
        > CONTINUATION_FIXED_THRESHOLDS["maximum_leakage_violations"]
    ):
        failures.append("LEAKAGE_GATE")
    commit = current_commit or current_repository_commit()
    gate = {
        "schema_version": CONTINUATION_GATE_SCHEMA,
        "gate_status": "PASS" if not failures else "FAIL",
        "policy_version": CONTINUATION_POLICY_VERSION,
        "decision_schema": CONTINUATION_DECISION_SCHEMA,
        "current_commit": commit,
        "protocol_version": CONTINUATION_PROTOCOL_VERSION,
        "thresholds": CONTINUATION_FIXED_THRESHOLDS,
        "mode_coverage": {
            mode: mode_coverage[mode] for mode in CONTINUATION_MODES
        },
        "metrics": {
            "sample_count": len(audited),
            "false_blocks": false_blocks,
            "leakage_violations": leakage,
            "essential_samples": sum(
                item["outcome"] == "ESSENTIAL_FOR_CORRECTNESS"
                for item in audited
            ),
            "beneficial_samples": sum(
                item["outcome"] == "BENEFICIAL_PERFORMANCE"
                for item in audited
            ),
        },
        "source_manifest_sha256": canonical_sha256(manifest),
        "audited_samples": audited,
    }
    if not failures:
        validate_continuation_gate(gate, expected_commit=commit)
    return gate


def write_gate_and_admission(
    *,
    replay_manifest_path: str | Path,
    gate_path: str | Path,
    admission_path: str | Path,
    harness_root: str | Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Generate the Gate and its minimal content-bound Admission."""

    replay_path = Path(replay_manifest_path).expanduser().resolve()
    manifest = _object(replay_path)
    commit = current_repository_commit()
    gate = build_offline_gate(
        manifest,
        harness_root=harness_root,
        current_commit=commit,
    )
    if gate["gate_status"] != "PASS":
        raise ValueError("Continuation Admission is forbidden: Gate is not PASS")

    gate_output = Path(gate_path).expanduser().resolve()
    admission_output = Path(admission_path).expanduser().resolve()
    if gate_output.parent != admission_output.parent:
        raise ValueError("Continuation Gate and Admission must share a directory")
    gate_output.parent.mkdir(parents=True, exist_ok=True)
    gate_output.write_text(
        json.dumps(gate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    admission = {
        "schema_version": CONTINUATION_ADMISSION_SCHEMA,
        "policy_version": CONTINUATION_POLICY_VERSION,
        "decision_schema": CONTINUATION_DECISION_SCHEMA,
        "current_commit": commit,
        "gate_json_path": gate_output.name,
        "gate_json_sha256": hashlib.sha256(
            gate_output.read_bytes()
        ).hexdigest(),
        "gate_status": gate["gate_status"],
        "protocol_version": CONTINUATION_PROTOCOL_VERSION,
        "mode_coverage": gate["mode_coverage"],
    }
    validate_continuation_admission(
        admission,
        admission_path=admission_output,
        expected_commit=commit,
    )
    admission_output.write_text(
        json.dumps(admission, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return gate, admission


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay current Continuation V3 on existing real artifacts"
    )
    parser.add_argument("--replay-manifest", required=True)
    parser.add_argument("--gate-output", required=True)
    parser.add_argument("--admission-output", required=True)
    parser.add_argument(
        "--harness-root",
        default=str(Path(__file__).resolve().parents[1]),
    )
    args = parser.parse_args(argv)
    gate, admission = write_gate_and_admission(
        replay_manifest_path=args.replay_manifest,
        gate_path=args.gate_output,
        admission_path=args.admission_output,
        harness_root=args.harness_root,
    )
    print(
        json.dumps(
            {
                "gate_status": gate["gate_status"],
                "metrics": gate["metrics"],
                "mode_coverage": gate["mode_coverage"],
                "gate_json_sha256": admission["gate_json_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REPLAY_MANIFEST_SCHEMA",
    "build_offline_gate",
    "write_gate_and_admission",
]
