"""Controlled A_FIXED versus D_HYBRID real-provider/Vitis runner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from .token_policy_abc_runner import (
    LoadedMatrix,
    MatrixJob,
    TokenPolicyABCError,
    _append_jsonl,
    _artifact_manifest,
    _atomic_json,
    _canonical_json,
    _extract_record,
    _git_head,
    _implementation_sha256 as _base_implementation_sha256,
    _integer,
    _mapping,
    _preflight,
    _read_json,
    _sha256_bytes,
    _sha256_json,
    _utc_now,
)
from .v3_batch_benchmark import (
    BenchmarkRunSpec,
    V3PrototypeCLIExecutor,
    discover_tasks,
)


MATRIX_SCHEMA = "v3e.token-policy-hybrid-matrix.v1"
MANIFEST_SCHEMA = "v3e.token-policy-hybrid-manifest.v1"
RUN_SCHEMA = "v3e.token-policy-hybrid-run.v1"
CONDITIONS = ("A_FIXED", "D_HYBRID")
MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")


def _implementation_sha256() -> str:
    package = Path(__file__).resolve().parent
    files = {
        "base": _base_implementation_sha256(),
        "runner": _sha256_bytes(Path(__file__).read_bytes()),
        "hybrid_config": _sha256_bytes(
            (package / "config" / "token_policy_hybrid_v2.json").read_bytes()
        ),
        "historical_analysis": _sha256_bytes(
            (package / "token_policy_historical_analysis.py").read_bytes()
        ),
    }
    return _sha256_json(files)


def _validate_config(value: Mapping[str, object]) -> None:
    if value.get("schema_version") != MATRIX_SCHEMA:
        raise TokenPolicyABCError("unsupported Hybrid matrix schema")
    if value.get("scope") != "PUBLIC_TRAIN_DEV_ONLY" or value.get(
        "allow_hidden_like"
    ) is not False:
        raise TokenPolicyABCError("Hybrid matrix must be public train/dev only")
    if value.get("experience_mode") != "off":
        raise TokenPolicyABCError("Hybrid evaluation requires experience-mode=off")
    if any(value.get(name) is not False for name in (
        "baseline_reuse", "candidate_reuse", "final_reuse"
    )):
        raise TokenPolicyABCError("A/D must not reuse run artifacts")
    conditions = value.get("conditions")
    expected = {
        "A_FIXED": ("fixed", "legacy"),
        "D_HYBRID": ("hybrid", "minimal"),
    }
    if not isinstance(conditions, Mapping) or tuple(conditions) != CONDITIONS:
        raise TokenPolicyABCError("matrix must define ordered A_FIXED/D_HYBRID")
    for name, binding in expected.items():
        row = conditions.get(name)
        if not isinstance(row, Mapping) or (
            row.get("token_budget_policy"), row.get("token_budget_visibility")
        ) != binding:
            raise TokenPolicyABCError(f"condition {name} is not controlled")
    for name in (
        "repeats", "run_token_limit", "credit_limit", "max_planner_rounds",
        "max_no_improvement_rounds", "final_reserve_credits",
        "runtime_limit_seconds", "provider_hard_output_cap",
    ):
        if _integer(value.get(name)) <= 0:
            raise TokenPolicyABCError(f"matrix {name} must be positive")
    if _integer(value.get("repeats")) != 2:
        raise TokenPolicyABCError("Hybrid Pilot requires exactly two repeats")


def _balanced_jobs(
    config: Mapping[str, object],
    task_rows: Mapping[str, Mapping[str, object]],
    config_sha256: str,
    implementation_sha256: str,
) -> tuple[MatrixJob, ...]:
    raw_pilot = config.get("pilot_tasks")
    if not isinstance(raw_pilot, Mapping):
        raise TokenPolicyABCError("pilot_tasks must be an object")
    per_mode: dict[str, list[str]] = {}
    for mode in MODES:
        raw = raw_pilot.get(mode)
        if not isinstance(raw, list) or len(raw) != 3 or len(set(raw)) != 3:
            raise TokenPolicyABCError(f"pilot mode {mode} must contain 3 tasks")
        task_ids = [str(item) for item in raw]
        if any(task_rows.get(task_id, {}).get("mode") != mode for task_id in task_ids):
            raise TokenPolicyABCError(f"pilot mode binding mismatch: {mode}")
        per_mode[mode] = task_ids
    interleaved = [
        per_mode[mode][index] for index in range(3) for mode in MODES
    ]
    jobs: list[MatrixJob] = []
    sequence = 0
    for repeat in range(1, _integer(config["repeats"]) + 1):
        for position, task_id in enumerate(interleaved):
            order = CONDITIONS if (position + repeat) % 2 else CONDITIONS[::-1]
            row = task_rows[task_id]
            for condition in order:
                sequence += 1
                identity = {
                    "schema_version": RUN_SCHEMA,
                    "config_sha256": config_sha256,
                    "implementation_sha256": implementation_sha256,
                    "task_id": task_id,
                    "mode": row["mode"],
                    "family": row["family"],
                    "condition": condition,
                    "repeat": repeat,
                    "task_snapshot": {
                        "mutation_manifest_sha256": row.get(
                            "mutation_manifest_sha256"
                        ),
                        "acceptance_sha256": row.get("acceptance_sha256"),
                    },
                }
                fingerprint = _sha256_json(identity)
                jobs.append(
                    MatrixJob(
                        sequence=sequence,
                        task_id=task_id,
                        mode=str(row["mode"]),
                        family=str(row["family"]),
                        condition=condition,
                        repeat=repeat,
                        run_id=(
                            f"{sequence:03d}--{condition.lower()}--{task_id}--"
                            f"r{repeat:02d}--{fingerprint[:10]}"
                        ),
                        run_fingerprint=fingerprint,
                    )
                )
    return tuple(jobs)


def load_matrix(path: Path | str) -> LoadedMatrix:
    matrix_path = Path(path).expanduser().resolve()
    config = _read_json(matrix_path)
    _validate_config(config)
    config_sha256 = _sha256_bytes(matrix_path.read_bytes())
    relative_manifest = Path(str(config.get("corpus_manifest") or ""))
    if relative_manifest.is_absolute():
        raise TokenPolicyABCError("corpus_manifest must be relative")
    manifest_path = (matrix_path.parent / relative_manifest).resolve()
    corpus_boundary = Path(__file__).resolve().parents[1] / "task_corpus"
    try:
        manifest_path.relative_to(corpus_boundary)
    except ValueError as exc:
        raise TokenPolicyABCError(
            "corpus_manifest escapes the public task corpus"
        ) from exc
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != "v3d.fast-corpus.v1":
        raise TokenPolicyABCError("Hybrid matrix requires the public V3-D corpus")
    raw_tasks = manifest.get("tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) != 28:
        raise TokenPolicyABCError("public corpus manifest must contain 28 tasks")
    task_rows = {
        str(row["task_id"]): row
        for row in raw_tasks
        if isinstance(row, Mapping) and isinstance(row.get("task_id"), str)
    }
    corpus_root = manifest_path.parent
    descriptors = {item.task_id: item for item in discover_tasks(corpus_root)}
    if set(task_rows).difference(descriptors):
        raise TokenPolicyABCError("some public task packages are missing")
    implementation = _implementation_sha256()
    return LoadedMatrix(
        path=matrix_path,
        config=config,
        config_sha256=config_sha256,
        corpus_manifest_path=manifest_path,
        corpus_manifest=manifest,
        corpus_manifest_sha256=_sha256_bytes(manifest_path.read_bytes()),
        corpus_root=corpus_root,
        descriptors=descriptors,
        implementation_sha256=implementation,
        jobs=_balanced_jobs(config, task_rows, config_sha256, implementation),
    )


def _condition_args(matrix: LoadedMatrix, condition: str) -> tuple[str, ...]:
    config = matrix.config
    selected = _mapping(_mapping(config["conditions"])[condition])
    args = [
        "--credit-limit", str(config["credit_limit"]),
        "--run-token-limit", str(config["run_token_limit"]),
        "--runtime-limit", str(config["runtime_limit_seconds"]),
        "--csim-timeout", str(config["csim_timeout_seconds"]),
        "--synth-timeout", str(config["synth_timeout_seconds"]),
        "--cosim-timeout", str(config["cosim_timeout_seconds"]),
        "--llm-timeout", str(config["llm_timeout_seconds"]),
        "--llm-max-output-tokens", str(config["provider_hard_output_cap"]),
        "--llm-temperature", str(config["temperature"]),
        "--llm-top-p", str(config["top_p"]),
        "--max-planner-rounds", str(config["max_planner_rounds"]),
        "--max-no-improvement-rounds", str(config["max_no_improvement_rounds"]),
        "--final-reserve-credits", str(config["final_reserve_credits"]),
        "--token-budget-policy", str(selected["token_budget_policy"]),
        "--context-window-tokens", str(config["context_window_tokens"]),
        "--context-safety-margin-tokens", str(
            config["context_safety_margin_tokens"]
        ),
        "--token-safety-margin", str(config["token_safety_margin"]),
        "--future-round-token-reserve", str(
            config["future_round_token_reserve"]
        ),
        "--final-token-reserve", str(config["final_token_reserve"]),
        "--guidance-ratio", str(config["guidance_ratio"]),
    ]
    if condition == "D_HYBRID":
        policy_ref = Path(str(config["hybrid_policy_config"]))
        policy_path = (matrix.path.parent / policy_ref).resolve()
        args.extend(("--token-policy-config", str(policy_path)))
    return tuple(args)


def _augment_record(
    record: dict[str, object], result: Mapping[str, object] | None
) -> dict[str, object]:
    result = result or {}
    gates = result.get("planner_call_gates")
    gates = gates if isinstance(gates, list) else []
    candidates = result.get("candidate_rounds")
    candidates = candidates if isinstance(candidates, list) else []
    second_candidates = [
        row
        for row in candidates
        if isinstance(row, Mapping) and int(row.get("round") or 0) >= 2
    ]
    record.update(
        {
            "schema_version": RUN_SCHEMA,
            "planner_call_gate_decisions": [
                row.get("decision") for row in gates if isinstance(row, Mapping)
            ],
            "planner_call_gate_reason_codes": [
                list(row.get("reason_codes", []))
                for row in gates
                if isinstance(row, Mapping)
            ],
            "second_call_attempted": int(record.get("planner_calls") or 0) >= 2,
            "second_call_blocked": any(
                isinstance(row, Mapping)
                and int(row.get("round") or 0) >= 2
                and row.get("decision") == "BLOCK"
                for row in gates
            ),
            "second_call_improved": any(
                row.get("decision") in {"PROMOTED", "FINAL_VERIFIED"}
                for row in second_candidates
                if isinstance(row, Mapping)
            ),
        }
    )
    return record


def _manifest(
    matrix: LoadedMatrix,
    output: Path,
    *,
    status: str,
    jobs: Sequence[MatrixJob],
) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "status": status,
        "created_at": _utc_now(),
        "experiment_id": matrix.config["experiment_id"],
        "matrix_config_ref": str(matrix.path),
        "matrix_config_sha256": matrix.config_sha256,
        "implementation_sha256": matrix.implementation_sha256,
        "corpus_manifest_ref": str(matrix.corpus_manifest_path),
        "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
        "scope": "PUBLIC_TRAIN_DEV_ONLY",
        "hidden_like_runs": 0,
        "provider": matrix.config["provider"],
        "model": os.environ.get(str(matrix.config["model_env"]), "UNSET"),
        "temperature": matrix.config["temperature"],
        "top_p": matrix.config["top_p"],
        "seed": None,
        "provider_seed_supported": False,
        "vitis_version": os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"),
        "validation_profile": matrix.config["validation_profile"],
        "experience_mode": "off",
        "execution_order": matrix.config["execution_order"],
        "baseline_reuse": False,
        "candidate_reuse": False,
        "final_reuse": False,
        "planned_runs": len(jobs),
        "full_pilot_runs": len(matrix.jobs),
        "conditions": matrix.config["conditions"],
        "git_commit": _git_head(Path(__file__).resolve().parents[2]),
        "output_dir": str(output),
        "schedule_ref": "schedule.jsonl",
        "results_ref": "token_policy_hybrid_results.jsonl",
    }


def run_matrix(
    matrix: LoadedMatrix,
    output_dir: Path | str,
    *,
    dry_run: bool = False,
    resume: bool = False,
    max_runs: int | None = None,
) -> dict[str, object]:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    jobs = matrix.jobs[:max_runs] if max_runs is not None else matrix.jobs
    manifest_path = output / "manifest.json"
    schedule_path = output / "schedule.jsonl"
    results_path = output / "token_policy_hybrid_results.jsonl"
    if not resume and any(path.exists() for path in (manifest_path, results_path)):
        raise TokenPolicyABCError("output directory already contains a matrix run")
    if not schedule_path.exists():
        for job in jobs:
            _append_jsonl(schedule_path, job.to_dict())
    if dry_run:
        value = _manifest(matrix, output, status="DRY_RUN", jobs=jobs)
        _atomic_json(manifest_path, value)
        return value
    model, _vitis_root = _preflight(matrix)
    completed: dict[str, Mapping[str, object]] = {}
    if resume and results_path.is_file():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, Mapping) and isinstance(value.get("run_id"), str):
                completed[str(value["run_id"])] = value
    manifest = _manifest(matrix, output, status="RUNNING", jobs=jobs)
    _atomic_json(manifest_path, manifest)
    records: list[Mapping[str, object]] = []
    executors = {
        condition: V3PrototypeCLIExecutor(
            _condition_args(matrix, condition),
            validation_profile=str(matrix.config["validation_profile"]),
            experience_mode="off",
            experience_task_split="train",
        )
        for condition in CONDITIONS
    }
    for job in jobs:
        if job.run_id in completed:
            records.append(completed[job.run_id])
            continue
        descriptor = matrix.descriptors[job.task_id]
        if descriptor.task is None:
            raise TokenPolicyABCError(f"task became unloadable: {job.task_id}")
        run_dir = output / "runs" / job.run_id
        if run_dir.exists() and any(run_dir.iterdir()):
            raise TokenPolicyABCError(f"fresh run directory is not empty: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)
        spec = BenchmarkRunSpec(
            task=descriptor.task,
            descriptor=descriptor,
            model=model,
            repeat_index=job.repeat,
            backend="vitis",
            run_id=job.run_id,
            run_fingerprint=job.run_fingerprint,
            run_dir=run_dir,
        )
        started = time.monotonic()
        result: Mapping[str, object] | None = None
        error: Exception | None = None
        try:
            result = executors[job.condition].execute(
                spec,
                timeout_seconds=float(matrix.config["runtime_limit_seconds"])
                + 120.0,
            )
        except Exception as exc:
            error = exc
        record = _extract_record(
            matrix,
            job,
            run_dir,
            result,
            wall_time_seconds=max(0.0, time.monotonic() - started),
            error=error,
        )
        _augment_record(record, result)
        artifacts = _artifact_manifest(run_dir)
        artifact_path = run_dir / "hybrid_artifact_hashes.json"
        _atomic_json(artifact_path, artifacts)
        record.update(
            {
                "artifact_manifest_ref": artifact_path.name,
                "artifact_manifest_sha256": _sha256_bytes(
                    artifact_path.read_bytes()
                ),
                "artifact_file_count": artifacts["file_count"],
            }
        )
        _atomic_json(run_dir / "hybrid_run_record.json", record)
        _append_jsonl(results_path, record)
        records.append(record)
    finished = dict(manifest)
    finished.update(
        {
            "status": "COMPLETE" if len(records) == len(jobs) else "INCOMPLETE",
            "finished_at": _utc_now(),
            "actual_runs": len(records),
            "successful_runs": sum(row.get("final_success") is True for row in records),
            "failed_runs": sum(row.get("final_success") is not True for row in records),
            "real_run_blocked": False,
        }
    )
    _atomic_json(manifest_path, finished)
    return finished


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-runs", type=int)
    args = parser.parse_args(argv)
    try:
        if args.max_runs is not None and args.max_runs <= 0:
            raise TokenPolicyABCError("max-runs must be positive")
        value = run_matrix(
            load_matrix(args.config),
            args.output_dir,
            dry_run=args.dry_run,
            resume=args.resume,
            max_runs=args.max_runs,
        )
    except Exception as exc:
        print(
            _canonical_json(
                {
                    "status": "REAL_RUN_BLOCKED",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 3
    print(_canonical_json(value))
    return 0 if value.get("status") in {"DRY_RUN", "COMPLETE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
