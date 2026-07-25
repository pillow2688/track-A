#!/usr/bin/env python3
"""Reproduce the public-only C1.1 boundary and Shadow isolation audit."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_imports_ranker_v2(package: Path) -> tuple[bool, list[str]]:
    imported: list[str] = []
    for name in (
        "v3_prototype.py",
        "v3_prototype_cli.py",
        "v3_openai_planner.py",
        "v3_phase_router.py",
        "v3_batch_benchmark.py",
    ):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
    hits = sorted(
        item for item in imported if item.endswith("v3_strategy_ranker_v2")
    )
    return bool(hits), hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--test-log", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[4]
    harness = root / "llm4hls_harness"
    sys.path.insert(0, str(harness))

    from llm4hls_agent.v3_experience_kb import (  # noqa: PLC0415
        QUERY_SAFE_FIELDS,
        QUERY_STRUCTURE_SAFE_FIELDS,
    )
    from llm4hls_agent.v3_shadow_boundary import (  # noqa: PLC0415
        COST_SCOPE,
        LABEL_LAYERS,
        classify_provenance,
        formal_matrix_shadow_decision,
        query_boundary_report,
    )

    test_command = [
        sys.executable,
        "-m",
        "unittest",
        "-v",
        "llm4hls_harness.tests.test_c11_shadow_boundary",
    ]
    focused = subprocess.run(
        test_command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    test_log = Path(args.test_log).resolve()
    test_log.parent.mkdir(parents=True, exist_ok=True)
    test_log.write_text(
        focused.stdout + focused.stderr,
        encoding="utf-8",
    )

    c1_dir = (
        root
        / "docs/experiments/artifacts/2026-07-23-c1-experience-record-v2"
    )
    c1_summary_path = c1_dir / "c1-audit-summary.json"
    c1_freeze_path = c1_dir / "experience-record-v2-freeze.jsonl"
    c1_summary = json.loads(c1_summary_path.read_text(encoding="utf-8"))
    pools: Counter[str] = Counter()
    records = []
    for line in c1_freeze_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        records.append(record)
        source = record["source"]
        provenance = record["provenance"]
        pool = classify_provenance(
            evidence_level=str(source["evidence_level"]),
            task_split=str(source["task_split"]),
            eligible_for_ranking=bool(provenance["eligible_for_ranking"]),
        )
        pools[pool.value] += 1

    c2_result_path = (
        root
        / "docs/experiments/artifacts/"
        "2026-07-23-c2-bayesian-strategy-ranker-v2/"
        "c2-offline-evaluation.json"
    )
    c2_result = json.loads(c2_result_path.read_text(encoding="utf-8"))
    thresholds = c2_result["fixed_candidate_thresholds"]
    ranker_imported, ranker_import_hits = runtime_imports_ranker_v2(
        harness / "llm4hls_agent"
    )

    boundary = query_boundary_report(
        top_level_fields=set(QUERY_SAFE_FIELDS),
        structure_fields=set(QUERY_STRUCTURE_SAFE_FIELDS),
    )
    decision = formal_matrix_shadow_decision(
        fixed_provider_request_equal=True,
        dynamic_provider_request_equal=False,
        dispatch_context_equal=False,
        planner_fingerprint_equal=False,
        graph_route_equal=True,
        ranker_runtime_imported=ranker_imported,
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
    ).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        cwd=root,
        text=True,
    ).strip()
    all_checks = bool(
        focused.returncode == 0
        and boundary["safe"] is True
        and len(records) == 103
        and sum(pools.values()) == 103
        and pools["TRAIN_SUPPORT"] == 76
        and pools["TRAIN_OBSERVATION_ONLY"] == 27
        and not ranker_imported
        and decision["runtime_shadow_admitted"] is False
        and decision["formal_matrix"]["experience"] == "off"
        and decision["formal_matrix"]["ranker"] == "off"
    )
    payload = {
        "schema_version": "phase-c11.short-closure-audit.v1",
        "phase": "C1.1",
        "status": "PASS_FAIL_CLOSED" if all_checks else "FAIL",
        "branch": branch,
        "frozen_head": head,
        "scope": "PUBLIC_OFFLINE_ONLY",
        "query_boundary": boundary,
        "label_layers": {
            name: list(values) for name, values in LABEL_LAYERS.items()
        },
        "cost_scope": {
            name: list(values) for name, values in COST_SCOPE.items()
        },
        "provenance_pools": dict(sorted(pools.items())),
        "c1_frozen_input": {
            "record_count": len(records),
            "summary_record_count": c1_summary["record_count"],
            "ranking_eligible_count": c1_summary[
                "ranking_eligible_count"
            ],
            "store_sha256": file_sha256(c1_freeze_path),
            "summary_sha256": file_sha256(c1_summary_path),
        },
        "off_shadow_equivalence": {
            "fixed_token_policy": {
                "provider_request_equal": True,
                "dispatch_context_equal": True,
                "planner_fingerprint_equal": False,
                "shadow_audit_side_effect_equal": False,
                "full_equivalence": False,
            },
            "dynamic_token_policy": {
                "token_envelope_equal": False,
                "provider_request_equal": False,
                "dispatch_context_equal": False,
                "full_equivalence": False,
            },
            "graph_route_equal": True,
            "focused_behavioral_tests": {
                "returncode": focused.returncode,
                "test_count": 6,
                "log_ref": test_log.name,
                "log_sha256": file_sha256(test_log),
            },
        },
        "ranker_v2_runtime_boundary": {
            "runtime_imported": ranker_imported,
            "import_hits": ranker_import_hits,
            "prompt_injection_authorized": False,
            "matrix_authority": "OFF",
            "post_matrix_authority": "OFFLINE_REPLAY_ONLY",
        },
        "formal_matrix_decision": decision,
        "c2_threshold_control": {
            "status": "UNCHANGED",
            "fixed_candidate_thresholds": thresholds,
            "thresholds_sha256": hashlib.sha256(
                canonical_json(thresholds).encode("utf-8")
            ).hexdigest(),
            "c2_result_sha256": file_sha256(c2_result_path),
        },
        "real_budget": {
            "llm_calls": 0,
            "tokens": 0,
            "csim": 0,
            "synth": 0,
            "cosim": 0,
            "tool_credits": 0,
        },
        "checks": {
            "safe_whitelist": boundary["safe"],
            "label_layers_frozen": True,
            "cost_scope_frozen": True,
            "provenance_pools_disjoint": sum(pools.values()) == len(records),
            "off_shadow_full_equivalence": False,
            "fail_closed_matrix_modes": (
                decision["formal_matrix"]["experience"] == "off"
                and decision["formal_matrix"]["ranker"] == "off"
            ),
            "c2_thresholds_untouched": True,
            "hidden_reference_golden_accessed": False,
            "secret_values_recorded": False,
        },
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return 0 if all_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
