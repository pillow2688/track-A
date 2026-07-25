#!/usr/bin/env python3
"""Build the public, terminal-only Experience increment for online admission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_analysis import write_analysis_artifacts
from llm4hls_agent.v3_experience_importer import (
    ImportPolicy,
    import_historical_runs,
    write_import_artifacts,
)
from llm4hls_agent.v3_experience_store import JsonlExperienceRepository


ROOT = Path(__file__).resolve().parents[4]
OUTPUT = Path(__file__).resolve().parent / "gate-experience-increment"
V1_OUTPUT = OUTPUT / "v1-import"
V2_OUTPUT = OUTPUT / "v2-analysis"

RUN_ROOTS = (
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_repair_guardfix_20260725_a01/"
    "runs/v3d_fast_006--deepseek-v4-pro--r001--39f7041c52f6",
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_repair_multistage_20260725_a02/"
    "runs/horizontal_repair_multistage--deepseek-v4-pro--r001--72ab1c752908",
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_synth_multistage_20260725_a10/"
    "runs/horizontal_synth_multistage--deepseek-v4-pro--r001--allblockers",
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_targeted_20260725_a01/"
    "runs/v3d_fast_013--deepseek-v4-pro--r001--a8c26b239d86",
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_targeted_20260725_a01/"
    "runs/v3d_fast_016--deepseek-v4-pro--r001--3c27ebdd58f8",
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_optimize_heldout_20260725_a01/"
    "runs/dotProduct_optimize--deepseek-v4-pro--r001--newpolicy",
    ROOT
    / "llm4hls_harness/experiments/horizontal_gate_optimize_heldout_20260725_a02/"
    "runs/v3d_fast_025--deepseek-v4-pro--r001--newpolicy",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    forbidden = {"hidden", "hidden_like", "reference", "golden"}
    for run_root in RUN_ROOTS:
        if not (run_root / "v3_prototype_result.json").is_file():
            raise FileNotFoundError(run_root)
        if any(part.casefold() in forbidden for part in run_root.parts):
            raise ValueError(f"forbidden run root: {run_root}")

    V1_OUTPUT.mkdir(parents=True, exist_ok=True)
    store = V1_OUTPUT / "experience_store.jsonl"
    repository = JsonlExperienceRepository(store)
    imported = import_historical_runs(
        RUN_ROOTS,
        repository,
        policy=ImportPolicy(task_split="dev"),
    )
    import_files = write_import_artifacts(imported, V1_OUTPUT)
    analysis = write_analysis_artifacts(
        v1_store=store,
        output_root=V2_OUTPUT,
        public_run_roots=RUN_ROOTS,
        provider_available=True,
        vitis_available=True,
    )
    manifest = {
        "schema_version": "v3e.horizontal-gate-experience-increment.v1",
        "status": "PASS",
        "source_policy": "EXPLICIT_PUBLIC_TERMINAL_REAL_DEEPSEEK_VITIS_RUNS",
        "run_count": len(RUN_ROOTS),
        "v1_record_count": len(imported.records),
        "v1_repository_record_count": imported.repository_record_count,
        "v2_record_count": analysis["stats"]["v2_store_record_count"],
        "ranking_eligible_count": analysis["quality"]["ranking_eligible_count"],
        "scope_guards": {
            "hidden_accessed": False,
            "reference_accessed": False,
            "golden_accessed": False,
            "failed_debug_runs_included": False,
        },
        "files": {
            path.name: _sha256(path)
            for path in (
                store,
                *import_files.values(),
                V2_OUTPUT / "experience_v2_backfill.jsonl",
                V2_OUTPUT / "experience_v2_audit_groups.json",
                V2_OUTPUT / "experience_v2_data_quality.json",
            )
        },
    }
    manifest_path = OUTPUT / "increment-manifest.json"
    manifest_path.write_bytes(canonical_json(manifest) + b"\n")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
