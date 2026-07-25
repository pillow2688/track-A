"""Targeted four-mode online Shadow pilot for horizontal components.

This is intentionally not a 28-task launcher.  It prepares one public anchor
per routed mode, binds Continuation V2 and Experience/Ranker V3 in Shadow, and
uses the resumable real Vitis batch executor.  Execution is fail-closed until
the previously disclosed provider key has been rotated explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping

from .v3_batch_benchmark import BatchBenchmarkRunner, BenchmarkConfig
from .v3_continuation_shadow_evaluation import evaluate_online_shadow_runs


PILOT_SCHEMA = "v3.horizontal-online-shadow-pilot.v1"
PILOT_TASKS = {
    "REPAIR": "task_corpus/official/fpt26-harness-public/projection_bugfix",
    "SYNTH_FIX": "examples/u55c_synthesis_repair_task",
    "STRUCTURAL_FIX": (
        "task_corpus/official/fpt26-harness-public/residual_stream_deadlock"
    ),
    "OPTIMIZE": "task_corpus/official/fpt26-harness-public/dotProduct_optimize",
}


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def environment_preflight(
    environ: Mapping[str, str] | None = None,
    *,
    allow_disclosed_key: bool = False,
) -> dict[str, object]:
    values = environ if environ is not None else os.environ
    vitis_root = Path(
        values.get(
            "LLM4HLS_VITIS_HLS_ROOT",
            "/opt/xilinx/2025.2/Vitis",
        )
    ).expanduser()
    vitis_run = vitis_root / "bin" / "vitis-run"
    key_rotated = (
        values.get("LLM4HLS_KEY_ROTATED_AFTER_DISCLOSURE") == "1"
    )
    credential_authorized = key_rotated or bool(allow_disclosed_key)
    checks = {
        "credential_authorized": credential_authorized,
        "key_rotation_marker": key_rotated,
        "user_authorized_disclosed_key": bool(allow_disclosed_key),
        "api_key_present": bool(values.get("OPENAI_API_KEY", "").strip()),
        "api_endpoint_present": bool(
            values.get("OPENAI_BASE_URL", "").strip()
        ),
        "model_present": bool(values.get("LLM4HLS_MODEL", "").strip()),
        "vitis_run_executable": vitis_run.is_file()
        and os.access(vitis_run, os.X_OK),
    }
    return {
        "schema_version": PILOT_SCHEMA,
        "ready": bool(
            credential_authorized
            and checks["api_key_present"]
            and checks["api_endpoint_present"]
            and checks["model_present"]
            and checks["vitis_run_executable"]
        ),
        "checks": checks,
        "credential_authority": (
            "ROTATED_AFTER_DISCLOSURE"
            if key_rotated
            else "USER_ACCEPTED_DISCLOSED_KEY_RISK"
            if allow_disclosed_key
            else "NOT_AUTHORIZED"
        ),
        "vitis_run": str(vitis_run.resolve()),
        "secret_values_serialized": False,
    }


def build_pilot_config(
    *,
    output_dir: str | Path,
    experience_store: str | Path,
    model: str,
) -> BenchmarkConfig:
    root = package_root()
    corpora = tuple((root / relative).resolve() for relative in PILOT_TASKS.values())
    missing = [str(path) for path in corpora if not (path / "task.toml").is_file()]
    if missing:
        raise ValueError(f"pilot public task packages are missing: {missing}")
    return BenchmarkConfig(
        corpus=corpora,
        output_dir=output_dir,
        models=(model,),
        repeats=1,
        backend="vitis",
        splits=("all",),
        resume=True,
        retry_failures=False,
        max_tasks=4,
        max_runtime_seconds=4 * 7200.0,
        validation_profile="fast-experiment",
        final_validation_policy="full_internal_audit",
        max_planner_rounds=3,
        max_no_improvement_rounds=2,
        enable_final_fallback=True,
        continuation_policy_mode="shadow",
        continuation_policy_version="v2",
        experience_mode="shadow",
        experience_store=experience_store,
        experience_ranker_version="v3",
        experience_task_split="dev",
    )


def pilot_plan(config: BenchmarkConfig) -> dict[str, object]:
    return {
        "schema_version": PILOT_SCHEMA,
        "scope": "FOUR_PUBLIC_MODE_ANCHORS_ONLY",
        "expected_mode_by_task": {
            Path(relative).name: mode
            for mode, relative in sorted(PILOT_TASKS.items())
        },
        "configuration": config.public_dict(),
        "authority": {
            "continuation": "SHADOW",
            "experience": "SHADOW",
            "ranker": "SHADOW",
            "prompt_injection": False,
            "enforce": False,
        },
        "post_run_gate": "MODE_SPECIFIC_PRE_STATE_ONLINE_SHADOW",
        "does_not_launch_28_tasks": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--experience-store", required=True)
    parser.add_argument(
        "--model",
        default=os.environ.get("LLM4HLS_MODEL", "deepseek-v4-pro"),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the four real slots after the fail-closed preflight.",
    )
    parser.add_argument(
        "--allow-disclosed-key",
        action="store_true",
        help=(
            "Record explicit user risk acceptance for this run when the "
            "rotation marker is absent. This never serializes the key."
        ),
    )
    args = parser.parse_args(argv)

    output = Path(args.output_dir).expanduser().resolve()
    config = build_pilot_config(
        output_dir=output,
        experience_store=args.experience_store,
        model=args.model,
    )
    preflight = environment_preflight(
        allow_disclosed_key=args.allow_disclosed_key
    )
    payload = {
        "preflight": preflight,
        "plan": pilot_plan(config),
    }
    print(json.dumps(payload, sort_keys=True))
    if not args.execute:
        return 0
    if preflight["ready"] is not True:
        raise SystemExit("horizontal online pilot preflight is not ready")

    outcome = BatchBenchmarkRunner(config).run()
    run_roots = [
        str(record["run_dir"])
        for record in outcome.records
        if isinstance(record.get("run_dir"), str)
    ]
    gate_dir = output / "horizontal_component_gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    continuation = evaluate_online_shadow_runs(
        run_roots,
        output_path=gate_dir / "continuation-v2-online-shadow-gate.json",
    )
    print(
        json.dumps(
            {
                "status": "DONE",
                "runs": len(run_roots),
                "continuation_gate": continuation["decision"],
                "continuation_samples": continuation["sample_count"],
                "ranker_authority": "SHADOW",
                "guided_authority": False,
                "enforce_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0 if len(run_roots) == 4 else 2


if __name__ == "__main__":
    raise SystemExit(main())
