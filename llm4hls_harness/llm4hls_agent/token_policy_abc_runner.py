"""Controlled, resumable A/B/C runner for the V3-E Token Budget Policy.

Only public task packages listed in a content-bound corpus manifest are
accepted.  Every condition gets a fresh V3 run directory and a fresh
non-replayable Planner request; Candidate and final artifacts are never shared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from .budget import TokenBudgetLimits
from .v3_batch_benchmark import (
    BenchmarkExecutionError,
    BenchmarkRunSpec,
    TaskDescriptor,
    V3PrototypeCLIExecutor,
    discover_tasks,
)


MATRIX_SCHEMA = "v3e.token-policy-abc-matrix.v1"
MANIFEST_SCHEMA = "v3e.token-policy-abc-manifest.v1"
RUN_SCHEMA = "v3e.token-policy-abc-run.v1"
ARTIFACT_SCHEMA = "v3e.token-policy-abc-artifacts.v1"
CONDITIONS = ("A_FIXED", "B_DYNAMIC_HARD", "C_DYNAMIC_VISIBLE")
MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
RESTRICTED_PATH_PARTS = frozenset(
    {"answer", "golden", "hidden", "hidden_like", "reference", "holdout", "test"}
)
_IMPLEMENTATION_FILES = (
    "token_policy_abc_runner.py",
    "token_policy_experiment_cli.py",
    "v3_batch_benchmark.py",
    "v3_prototype_cli.py",
    "v3_openai_planner.py",
    "openai_provider.py",
    "budget.py",
    "v3_prototype.py",
    "vitis.py",
)


class TokenPolicyABCError(RuntimeError):
    """Raised when the controlled matrix or its durable state is invalid."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (_canonical_json(value) + "\n").encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenPolicyABCError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise TokenPolicyABCError(f"JSON artifact is not an object: {path}")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _list_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _git_head(root: Path) -> str | None:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and len(value) == 40 else None


def _implementation_sha256() -> str:
    """Bind run identities to the code that can affect an A/B/C execution."""

    package = Path(__file__).resolve().parent
    files: dict[str, str] = {}
    for name in _IMPLEMENTATION_FILES:
        path = package / name
        if not path.is_file():
            raise TokenPolicyABCError(f"controlled implementation file is missing: {path}")
        files[name] = _sha256_bytes(path.read_bytes())
    return _sha256_json(files)


@dataclass(frozen=True)
class MatrixJob:
    sequence: int
    task_id: str
    mode: str
    family: str
    condition: str
    repeat: int
    run_id: str
    run_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "task_id": self.task_id,
            "mode": self.mode,
            "family": self.family,
            "condition": self.condition,
            "repeat": self.repeat,
            "run_id": self.run_id,
            "run_fingerprint": self.run_fingerprint,
        }


@dataclass(frozen=True)
class LoadedMatrix:
    path: Path
    config: Mapping[str, object]
    config_sha256: str
    corpus_manifest_path: Path
    corpus_manifest: Mapping[str, object]
    corpus_manifest_sha256: str
    corpus_root: Path
    descriptors: Mapping[str, TaskDescriptor]
    implementation_sha256: str
    jobs: tuple[MatrixJob, ...]


def _validate_common_config(value: Mapping[str, object]) -> None:
    if value.get("schema_version") != MATRIX_SCHEMA:
        raise TokenPolicyABCError("unsupported token-policy matrix schema")
    if value.get("scope") != "PUBLIC_TRAIN_DEV_ONLY":
        raise TokenPolicyABCError("matrix scope must be PUBLIC_TRAIN_DEV_ONLY")
    if value.get("allow_hidden_like") is not False:
        raise TokenPolicyABCError("hidden-like tasks must remain disabled")
    if value.get("experience_mode") != "off":
        raise TokenPolicyABCError("Token Policy A/B/C requires experience-mode=off")
    if value.get("baseline_reuse") is not False:
        raise TokenPolicyABCError("this runner does not reuse baseline artifacts")
    if value.get("candidate_reuse") is not False or value.get("final_reuse") is not False:
        raise TokenPolicyABCError("Candidate/final artifact reuse is forbidden")
    conditions = value.get("conditions")
    if not isinstance(conditions, Mapping) or tuple(conditions) != CONDITIONS:
        raise TokenPolicyABCError("matrix must define ordered A/B/C conditions")
    expected = {
        "A_FIXED": ("fixed", "legacy"),
        "B_DYNAMIC_HARD": ("dynamic", "hidden"),
        "C_DYNAMIC_VISIBLE": ("dynamic", "visible"),
    }
    for name, (policy, visibility) in expected.items():
        raw = conditions.get(name)
        if not isinstance(raw, Mapping) or (
            raw.get("token_budget_policy"), raw.get("token_budget_visibility")
        ) != (policy, visibility):
            raise TokenPolicyABCError(f"condition {name} is not controlled")
    for name in (
        "repeats",
        "run_token_limit",
        "credit_limit",
        "max_planner_rounds",
        "max_no_improvement_rounds",
        "search_closeout_reserve_credits",
        "runtime_limit_seconds",
        "provider_hard_output_cap",
    ):
        if _integer(value.get(name)) <= 0:
            raise TokenPolicyABCError(f"matrix {name} must be positive")
    configured_minimums = value.get("mode_minimum_viable_output")
    if not isinstance(configured_minimums, Mapping):
        raise TokenPolicyABCError(
            "matrix mode_minimum_viable_output must be an object"
        )
    normalized_minimums = {
        str(mode): _integer(limit)
        for mode, limit in configured_minimums.items()
    }
    resolved_minimums = dict(
        TokenBudgetLimits().mode_minimum_viable_output
    )
    if normalized_minimums != resolved_minimums:
        raise TokenPolicyABCError(
            "matrix mode_minimum_viable_output does not match the "
            "versioned runtime defaults"
        )
    if value.get("provider_seed_supported") is not False or value.get("seed") is not None:
        raise TokenPolicyABCError(
            "DeepSeek seed is unsupported and must be recorded as null/false"
        )


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
        for task_id in task_ids:
            row = task_rows.get(task_id)
            if row is None or row.get("mode") != mode:
                raise TokenPolicyABCError(
                    f"pilot task {task_id} does not belong to {mode}"
                )
        per_mode[mode] = task_ids

    interleaved = [
        task_id
        for index in range(3)
        for mode in MODES
        for task_id in (per_mode[mode][index],)
    ]
    jobs: list[MatrixJob] = []
    sequence = 0
    repeats = _integer(config["repeats"])
    for repeat in range(1, repeats + 1):
        for task_position, task_id in enumerate(interleaved):
            rotation = (task_position + repeat - 1) % len(CONDITIONS)
            condition_order = CONDITIONS[rotation:] + CONDITIONS[:rotation]
            row = task_rows[task_id]
            for condition in condition_order:
                sequence += 1
                identity = {
                    "schema_version": RUN_SCHEMA,
                    "config_sha256": config_sha256,
                    "implementation_sha256": implementation_sha256,
                    "task_id": task_id,
                    "task_snapshot": {
                        "mutation_manifest_sha256": row.get("mutation_manifest_sha256"),
                        "acceptance_sha256": row.get("acceptance_sha256"),
                    },
                    "mode": row["mode"],
                    "family": row["family"],
                    "condition": condition,
                    "repeat": repeat,
                }
                fingerprint = _sha256_json(identity)
                run_id = (
                    f"{sequence:03d}--{condition.lower()}--{task_id}--"
                    f"r{repeat:02d}--{fingerprint[:10]}"
                )
                jobs.append(
                    MatrixJob(
                        sequence=sequence,
                        task_id=task_id,
                        mode=str(row["mode"]),
                        family=str(row["family"]),
                        condition=condition,
                        repeat=repeat,
                        run_id=run_id,
                        run_fingerprint=fingerprint,
                    )
                )
    return tuple(jobs)


def load_matrix(path: Path | str) -> LoadedMatrix:
    matrix_path = Path(path).expanduser().resolve()
    config = _read_json(matrix_path)
    _validate_common_config(config)
    config_sha256 = _sha256_bytes(matrix_path.read_bytes())
    raw_manifest = config.get("corpus_manifest")
    if not isinstance(raw_manifest, str) or not raw_manifest:
        raise TokenPolicyABCError("corpus_manifest must be a relative path")
    relative = Path(raw_manifest)
    if relative.is_absolute():
        raise TokenPolicyABCError("corpus_manifest must be relative to matrix config")
    corpus_manifest_path = (matrix_path.parent / relative).resolve()
    corpus_manifest = _read_json(corpus_manifest_path)
    if corpus_manifest.get("schema_version") != "v3d.fast-corpus.v1":
        raise TokenPolicyABCError("matrix requires the public v3d-fast corpus")
    task_values = corpus_manifest.get("tasks")
    if not isinstance(task_values, list) or len(task_values) != 28:
        raise TokenPolicyABCError("public corpus manifest must contain 28 tasks")
    task_rows: dict[str, Mapping[str, object]] = {}
    for raw in task_values:
        if not isinstance(raw, Mapping):
            raise TokenPolicyABCError("corpus task row is invalid")
        task_id = str(raw.get("task_id") or "")
        relative_task = Path(str(raw.get("path") or ""))
        lowered = {part.casefold() for part in relative_task.parts}
        if not task_id or lowered.intersection(RESTRICTED_PATH_PARTS):
            raise TokenPolicyABCError("restricted task path found in public matrix")
        task_rows[task_id] = raw
    corpus_root = corpus_manifest_path.parent
    descriptors = {item.task_id: item for item in discover_tasks(corpus_root)}
    if set(task_rows).difference(descriptors):
        raise TokenPolicyABCError("some public task packages are missing")
    for task_id, descriptor in descriptors.items():
        if task_id not in task_rows:
            continue
        if not descriptor.loadable:
            raise TokenPolicyABCError(f"task package does not load: {task_id}")
        if descriptor.expected_mode != task_rows[task_id].get("mode"):
            raise TokenPolicyABCError(f"task mode binding mismatch: {task_id}")
    implementation_sha256 = _implementation_sha256()
    jobs = _balanced_jobs(
        config, task_rows, config_sha256, implementation_sha256
    )
    return LoadedMatrix(
        path=matrix_path,
        config=config,
        config_sha256=config_sha256,
        corpus_manifest_path=corpus_manifest_path,
        corpus_manifest=corpus_manifest,
        corpus_manifest_sha256=_sha256_bytes(corpus_manifest_path.read_bytes()),
        corpus_root=corpus_root,
        descriptors=descriptors,
        implementation_sha256=implementation_sha256,
        jobs=jobs,
    )


def _condition_args(matrix: LoadedMatrix, condition: str) -> tuple[str, ...]:
    config = matrix.config
    conditions = _mapping(config["conditions"])
    selected = _mapping(conditions[condition])
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
        "--search-closeout-reserve-credits", str(config["search_closeout_reserve_credits"]),
        "--token-budget-policy", str(selected["token_budget_policy"]),
        "--context-window-tokens", str(config["context_window_tokens"]),
        "--context-safety-margin-tokens", str(config["context_safety_margin_tokens"]),
        "--token-safety-margin", str(config["token_safety_margin"]),
        "--future-round-token-reserve", str(config["future_round_token_reserve"]),
        "--search-closeout-token-reserve", str(config["search_closeout_token_reserve"]),
        "--guidance-token-cap", str(config["guidance_token_cap"]),
        "--guidance-ratio", str(config["guidance_ratio"]),
    ]
    caps = _mapping(config["mode_output_caps"])
    args.extend(
        (
            "--repair-max-output-tokens", str(caps["REPAIR"]),
            "--synth-fix-max-output-tokens", str(caps["SYNTH_FIX"]),
            "--structural-fix-max-output-tokens", str(caps["STRUCTURAL_FIX"]),
            "--optimize-max-output-tokens", str(caps["OPTIMIZE"]),
        )
    )
    visibility = selected.get("token_budget_visibility")
    if visibility in {"hidden", "visible"}:
        args.extend(("--token-budget-visibility", str(visibility)))
    return tuple(args)


def _condition_executor(
    matrix: LoadedMatrix, condition: str
) -> V3PrototypeCLIExecutor:
    """Build one experimental executor without bypassing owned CLI controls."""

    return V3PrototypeCLIExecutor(
        _condition_args(matrix, condition),
        validation_profile=str(matrix.config["validation_profile"]),
        max_planner_rounds=int(matrix.config["max_planner_rounds"]),
        max_no_improvement_rounds=int(
            matrix.config["max_no_improvement_rounds"]
        ),
        experience_mode="off",
        experience_task_split="train",
        experimental_token_policy=True,
    )


def _execution_identity(
    matrix: LoadedMatrix,
    *,
    model: str,
    vitis_root: Path,
    executors: Mapping[str, V3PrototypeCLIExecutor],
) -> dict[str, object]:
    payload = {
        "schema_version": "v3e.token-policy-abc-execution-identity.v1",
        "matrix_config_sha256": matrix.config_sha256,
        "implementation_sha256": matrix.implementation_sha256,
        "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
        "model": model,
        "vitis_root_sha256": _sha256_bytes(
            str(vitis_root).encode("utf-8")
        ),
        "toolchain_id": os.environ.get(
            "LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"
        ),
        "executor_fingerprints": {
            condition: executors[condition].fingerprint()
            for condition in CONDITIONS
        },
    }
    return {
        **payload,
        "execution_identity_sha256": _sha256_json(payload),
    }


def _validate_schedule(
    schedule_path: Path, jobs: Sequence[MatrixJob]
) -> None:
    expected = [job.to_dict() for job in jobs]
    if not schedule_path.exists():
        for row in expected:
            _append_jsonl(schedule_path, row)
        return
    actual: list[Mapping[str, object]] = []
    for line in schedule_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise TokenPolicyABCError("schedule row is not an object")
        actual.append(value)
    if actual != expected:
        raise TokenPolicyABCError(
            "existing schedule does not match the selected matrix jobs"
        )


def _load_resumed_records(
    *,
    matrix: LoadedMatrix,
    jobs: Sequence[MatrixJob],
    output: Path,
    manifest_path: Path,
    results_path: Path,
    execution_identity: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    if not manifest_path.is_file():
        raise TokenPolicyABCError("resume requires an existing manifest")
    prior_manifest = _read_json(manifest_path)
    expected_manifest_fields = {
        "schema_version": MANIFEST_SCHEMA,
        "matrix_config_sha256": matrix.config_sha256,
        "implementation_sha256": matrix.implementation_sha256,
        "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
        "execution_identity_sha256": execution_identity[
            "execution_identity_sha256"
        ],
        "planned_runs": len(jobs),
    }
    for field, expected in expected_manifest_fields.items():
        if prior_manifest.get(field) != expected:
            raise TokenPolicyABCError(
                f"resume manifest identity mismatch: {field}"
            )

    by_id = {job.run_id: job for job in jobs}
    completed: dict[str, Mapping[str, object]] = {}
    if not results_path.is_file():
        return completed
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise TokenPolicyABCError("resume result row is not an object")
        run_id = value.get("run_id")
        if not isinstance(run_id, str) or run_id not in by_id:
            raise TokenPolicyABCError("resume result has an unknown run_id")
        if run_id in completed:
            raise TokenPolicyABCError("resume result has a duplicate run_id")
        job = by_id[run_id]
        condition_config = _mapping(
            _mapping(matrix.config["conditions"])[job.condition]
        )
        expected_fields = {
            **job.to_dict(),
            "schema_version": RUN_SCHEMA,
            "matrix_config_sha256": matrix.config_sha256,
            "implementation_sha256": matrix.implementation_sha256,
            "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
            "execution_identity_sha256": execution_identity[
                "execution_identity_sha256"
            ],
            "run_ref": f"runs/{run_id}",
            "provider": matrix.config["provider"],
            "model": execution_identity["model"],
            "token_budget_policy": condition_config[
                "token_budget_policy"
            ],
            "token_budget_visibility": condition_config[
                "token_budget_visibility"
            ],
        }
        for field, expected in expected_fields.items():
            if value.get(field) != expected:
                raise TokenPolicyABCError(
                    f"resume result identity mismatch for {run_id}: {field}"
                )
        run_dir = output / "runs" / run_id
        record_path = run_dir / "abc_run_record.json"
        if value.get("artifact_manifest_ref") != "abc_artifact_hashes.json":
            raise TokenPolicyABCError(
                f"resume artifact manifest reference mismatch for {run_id}"
            )
        artifact_path = run_dir / "abc_artifact_hashes.json"
        if not record_path.is_file() or not artifact_path.is_file():
            raise TokenPolicyABCError(
                f"resume receipts are missing for {run_id}"
            )
        if _read_json(record_path) != dict(value):
            raise TokenPolicyABCError(
                f"resume record receipt mismatch for {run_id}"
            )
        if value.get("artifact_manifest_sha256") != _sha256_bytes(
            artifact_path.read_bytes()
        ):
            raise TokenPolicyABCError(
                f"resume artifact manifest hash mismatch for {run_id}"
            )
        if _read_json(artifact_path) != _artifact_manifest(run_dir):
            raise TokenPolicyABCError(
                f"resume run artifacts changed for {run_id}"
            )
        completed[run_id] = value
    return completed


def _read_optional_run_json(run_dir: Path, reference: object) -> Mapping[str, object]:
    if not isinstance(reference, str) or not reference:
        return {}
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        return {}
    path = run_dir / relative
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _latency_worst(run_dir: Path, result: Mapping[str, object], key: str) -> float | None:
    value = _read_optional_run_json(run_dir, result.get(key))
    report = _mapping(value.get("report"))
    latency = _mapping(report.get("latency"))
    return _number(latency.get("worst"))


def _score_proxy(run_dir: Path, result: Mapping[str, object]) -> float | None:
    value = _read_optional_run_json(run_dir, result.get("final_score_ref"))
    for key in ("score", "official_score_proxy", "local_score_proxy"):
        number = _number(value.get(key))
        if number is not None:
            return number
    return None


def _request_rows(run_dir: Path) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for path in sorted((run_dir / "planner" / "requests").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            rows.append(value)
    return rows


def _provider_failure_rows(run_dir: Path) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    root = run_dir / "planner" / "provider_failures"
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            rows.append(value)
    return rows


def _provider_model_revisions(run_dir: Path) -> list[str]:
    revisions: set[str] = set()
    for path in sorted((run_dir / "planner" / "live_outcomes").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        proposal = _mapping(value.get("proposal")) if isinstance(value, Mapping) else {}
        revision = proposal.get("revision")
        if isinstance(revision, str) and revision.strip():
            revisions.add(revision.strip())
    return sorted(revisions)


def _prompt_versions(run_dir: Path) -> list[str]:
    versions: set[str] = set()
    for row in _request_rows(run_dir):
        request = _mapping(row.get("request"))
        for field in ("adapter_version", "schema_version"):
            value = request.get(field)
            if isinstance(value, str) and value.strip():
                versions.add(value.strip())
    return sorted(versions)


def _proposal_rejections(run_dir: Path) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for path in sorted((run_dir / "control" / "proposal_rejections").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            rows.append(value)
    return rows


def _artifact_manifest(run_dir: Path) -> dict[str, object]:
    files: dict[str, dict[str, object]] = {}
    excluded = {"abc_artifact_hashes.json", "abc_run_record.json"}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative in excluded:
            continue
        data = path.read_bytes()
        files[relative] = {"size_bytes": len(data), "sha256": _sha256_bytes(data)}
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "scope": "ALL_V3_RUN_FILES_BEFORE_ABC_RECEIPT",
        "excluded_self_referential_files": sorted(excluded),
        "file_count": len(files),
        "files": files,
    }


def reconcile_record_from_artifacts(
    record: Mapping[str, object],
    run_dir: Path,
    config: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    """Recover durable metrics when the CLI failed after persisting its work.

    This never changes model, Candidate, or validation artifacts.  The budget
    state and Candidate registry are authoritative durable receipts, whereas a
    subprocess exception can prevent the terminal summary from reaching the
    matrix runner.
    """

    reconciled = dict(record)
    changed: list[str] = []
    budget_path = run_dir / "budget_state.json"
    if budget_path.is_file():
        budget = _read_json(budget_path)
        recovered = {
            "actual_input_tokens": _integer(budget.get("input_tokens_used")),
            "actual_output_tokens": _integer(budget.get("output_tokens_used")),
            "actual_total_tokens": _integer(budget.get("tokens_used")),
            "credits_used": _integer(budget.get("credits_used")),
            "tool_calls": dict(_mapping(budget.get("tool_used"))),
        }
        recovered["budget_compliant"] = bool(
            recovered["actual_total_tokens"]
            <= _integer(config.get("run_token_limit"))
            and recovered["credits_used"] <= _integer(config.get("credit_limit"))
        )
        for name, value in recovered.items():
            if reconciled.get(name) != value:
                reconciled[name] = value
                changed.append(name)

    registry_path = run_dir / "candidate_registry.json"
    if registry_path.is_file():
        registry = _read_json(registry_path)
        candidates = _mapping(registry.get("candidates"))
        non_baseline = [
            _mapping(value)
            for value in candidates.values()
            if isinstance(value, Mapping) and value.get("kind") != "baseline"
        ]
        promoted = [
            value
            for value in non_baseline
            if value.get("status") in {"PROMOTED", "FINAL_VERIFIED"}
        ]
        candidate_values = {
            "candidate_count": len(non_baseline),
            "candidate_promoted_count": len(promoted),
            "candidate_promotion_rate": (
                len(promoted) / len(non_baseline) if non_baseline else 0.0
            ),
            "patch_valid_count": len(non_baseline),
        }
        for name, value in candidate_values.items():
            if reconciled.get(name) != value:
                reconciled[name] = value
                changed.append(name)

        final_id = registry.get("final_candidate_id")
        final_candidate = (
            _mapping(candidates.get(final_id))
            if isinstance(final_id, str)
            else {}
        )
        final_rows = _mapping(final_candidate.get("final_validation"))
        final_validation: dict[str, object] = {}
        all_fresh_pass = final_candidate.get("status") == "FINAL_VERIFIED"
        for stage in ("csim", "synth", "cosim"):
            stage_row = _mapping(final_rows.get(stage))
            result_ref = stage_row.get("result_ref")
            action_result: Mapping[str, object] = {}
            if isinstance(result_ref, str) and result_ref:
                result_path = run_dir / result_ref
                if result_path.is_file():
                    action_result = _read_json(result_path)
            fresh = bool(
                action_result.get("cached") is False
                and action_result.get("validation_scope") == "search_closeout"
            )
            passed = bool(
                stage_row.get("status") == "PASS"
                and stage_row.get("ok") is True
                and fresh
            )
            final_validation[stage] = {
                "status": stage_row.get("status", "NOT_RUN"),
                "fresh": fresh,
            }
            all_fresh_pass = all_fresh_pass and passed
        if reconciled.get("final_validation") != final_validation:
            reconciled["final_validation"] = final_validation
            changed.append("final_validation")
        if reconciled.get("final_success") is not all_fresh_pass:
            reconciled["final_success"] = all_fresh_pass
            changed.append("final_success")

    action_results = sorted(run_dir.glob("actions/*/result.json"))
    if reconciled.get("evidence_level") is None and action_results:
        fingerprints = []
        for path in action_results:
            value = _read_json(path)
            fingerprint = value.get("backend_fingerprint")
            if isinstance(fingerprint, str):
                fingerprints.append(fingerprint)
        if fingerprints and all(
            value.startswith("llm4hls_agent.vitis.VitisBackend:")
            for value in fingerprints
        ):
            reconciled["evidence_level"] = "REAL_VITIS_VALIDATED"
            changed.append("evidence_level")

    if changed:
        reconciled["reconciled_from_durable_artifacts"] = True
        reconciled["reconciled_fields"] = sorted(set(changed))
        reconciled["original_status"] = record.get("status")
        reconciled["original_error_type"] = record.get("error_type")
    return reconciled, sorted(set(changed))


def _extract_record(
    matrix: LoadedMatrix,
    job: MatrixJob,
    run_dir: Path,
    result: Mapping[str, object] | None,
    *,
    wall_time_seconds: float,
    error: Exception | None,
) -> dict[str, object]:
    result = result or {}
    budget = _mapping(result.get("budget"))
    tool_used = _mapping(budget.get("tool_used"))
    requests = _request_rows(run_dir)
    failures = _provider_failure_rows(run_dir)
    rejections = _proposal_rejections(run_dir)
    token_rounds = _list_of_mappings(result.get("token_policy_rounds"))
    configured_outputs = [
        _integer(row.get("configured_max_output_tokens"))
        for row in requests
        if _integer(row.get("configured_max_output_tokens")) > 0
    ]
    effective_outputs = [
        _integer(row.get("effective_max_output_tokens"))
        for row in requests
        if _integer(row.get("effective_max_output_tokens")) > 0
    ]
    finish_reasons = [
        str(row.get("finish_reason"))
        for row in token_rounds
        if row.get("finish_reason") is not None
    ]
    finish_reasons.extend(
        str(row.get("finish_reason"))
        for row in failures
        if row.get("finish_reason") is not None
    )
    truncation_reasons = [
        str(row.get("truncation_reason"))
        for row in token_rounds
        if row.get("output_truncated") is True
    ]
    truncation_reasons.extend(
        str(row.get("truncation_reason"))
        for row in failures
        if row.get("output_truncated") is True
    )
    final = _mapping(result.get("final_validation"))
    final_success = bool(
        result.get("status") == "DONE"
        and all(
            _mapping(final.get(stage)).get("status") == "PASS"
            and _mapping(final.get(stage)).get("cached") is False
            for stage in ("csim", "synth", "cosim")
        )
    )
    candidate_rounds = _list_of_mappings(result.get("candidate_rounds"))
    promoted = [
        row
        for row in candidate_rounds
        if str(row.get("decision") or "") in {"PROMOTED", "FINAL_VERIFIED"}
    ]
    baseline_latency = _latency_worst(
        run_dir, result, "baseline_metrics_ref"
    )
    final_latency = _latency_worst(run_dir, result, "final_metrics_ref")
    acceleration = (
        baseline_latency / final_latency
        if baseline_latency is not None
        and final_latency is not None
        and final_latency > 0
        else None
    )
    input_tokens = _integer(budget.get("input_tokens_used"))
    output_tokens = _integer(budget.get("output_tokens_used"))
    total_tokens = _integer(budget.get("tokens_used"), input_tokens + output_tokens)
    max_output_sum = sum(effective_outputs)
    model_revisions = _provider_model_revisions(run_dir)
    condition_config = _mapping(_mapping(matrix.config["conditions"])[job.condition])
    backend = _mapping(result.get("backend"))
    return {
        "schema_version": RUN_SCHEMA,
        **job.to_dict(),
        "matrix_config_sha256": matrix.config_sha256,
        "implementation_sha256": matrix.implementation_sha256,
        "corpus_manifest_sha256": matrix.corpus_manifest_sha256,
        "public_scope": "PUBLIC_TRAIN_DEV_ONLY",
        "hidden_like": False,
        "run_ref": f"runs/{job.run_id}",
        "evidence_level": backend.get("evidence_level"),
        "provider": matrix.config["provider"],
        "model": os.environ.get(str(matrix.config["model_env"]), ""),
        "model_versions": model_revisions,
        "prompt_versions": _prompt_versions(run_dir),
        "temperature": matrix.config["temperature"],
        "top_p": matrix.config["top_p"],
        "seed": None,
        "provider_seed_supported": False,
        "token_budget_policy": condition_config["token_budget_policy"],
        "token_budget_visibility": condition_config["token_budget_visibility"],
        "experience_mode": "off",
        "validation_profile": matrix.config["validation_profile"],
        "baseline_reused": False,
        "candidate_reused": False,
        "final_reused": False,
        "status": result.get("status", "ERROR"),
        "stop_reason": result.get("stop_reason") or (
            type(error).__name__ if error is not None else "NO_TERMINAL_RESULT"
        ),
        "error_type": type(error).__name__ if error is not None else None,
        "error_detail": str(error) if error is not None else None,
        "final_success": final_success,
        "final_validation": {
            stage: {
                "status": _mapping(final.get(stage)).get("status"),
                "fresh": _mapping(final.get(stage)).get("cached") is False,
            }
            for stage in ("csim", "synth", "cosim")
        },
        "planner_calls": _integer(tool_used.get("llm"), len(requests)),
        "configured_max_outputs": configured_outputs,
        "effective_max_outputs": effective_outputs,
        "estimated_input_tokens": [
            _integer(row.get("estimated_input_tokens")) for row in requests
        ],
        "actual_input_tokens": input_tokens,
        "actual_output_tokens": output_tokens,
        "actual_total_tokens": total_tokens,
        "guidance_tokens": sum(
            _integer(row.get("guidance_actual_tokens")) for row in token_rounds
        ),
        "token_pressures": [
            str(row.get("token_pressure"))
            for row in token_rounds
            if row.get("token_pressure") is not None
        ],
        "max_output_utilization": (
            output_tokens / max_output_sum if max_output_sum > 0 else None
        ),
        "finish_reasons": finish_reasons,
        "output_truncated": bool(truncation_reasons),
        "truncation_reasons": truncation_reasons,
        "json_incomplete_count": sum(
            row.get("truncation_reason") == "JSON_INCOMPLETE" for row in failures
        ),
        "patch_incomplete_count": sum(
            row.get("truncation_reason") == "PATCH_INCOMPLETE" for row in failures
        ),
        "patch_invalid_count": sum(
            "PATCH" in str(row.get("reason") or "") for row in rejections
        ),
        "patch_valid_count": len(candidate_rounds),
        "candidate_count": len(candidate_rounds),
        "candidate_promoted_count": len(promoted),
        "candidate_promotion_rate": (
            len(promoted) / len(candidate_rounds) if candidate_rounds else 0.0
        ),
        "baseline_latency": baseline_latency,
        "final_latency": final_latency,
        "latency_improved": bool(
            baseline_latency is not None
            and final_latency is not None
            and final_latency < baseline_latency
        ),
        "acceleration": acceleration,
        "local_score_proxy": _score_proxy(run_dir, result),
        "credits_used": _integer(budget.get("credits_used")),
        "wall_time_seconds": wall_time_seconds,
        "budget_compliant": bool(
            total_tokens <= _integer(matrix.config["run_token_limit"])
            and _integer(budget.get("credits_used"))
            <= _integer(matrix.config["credit_limit"])
        ),
        "tool_calls": dict(tool_used),
    }


def _preflight(matrix: LoadedMatrix) -> tuple[str, Path]:
    missing = [
        name
        for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", str(matrix.config["model_env"]))
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise TokenPolicyABCError("missing external Provider variables: " + ",".join(missing))
    vitis_root = Path(
        os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "/opt/xilinx/2025.2/Vitis")
    ).expanduser().resolve()
    if not (vitis_root / "settings64.sh").is_file():
        raise TokenPolicyABCError(f"Vitis settings64.sh is missing under {vitis_root}")
    model = os.environ[str(matrix.config["model_env"])].strip()
    return model, vitis_root


def _manifest(
    matrix: LoadedMatrix,
    output_dir: Path,
    *,
    status: str,
    selected_jobs: Sequence[MatrixJob],
    execution_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    repository_root = Path(__file__).resolve().parents[2]
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
        "execution_identity": (
            dict(execution_identity)
            if execution_identity is not None
            else None
        ),
        "execution_identity_sha256": (
            execution_identity.get("execution_identity_sha256")
            if execution_identity is not None
            else None
        ),
        "scope": "PUBLIC_TRAIN_DEV_ONLY",
        "hidden_like_runs": 0,
        "provider": matrix.config["provider"],
        "model": os.environ.get(str(matrix.config["model_env"]), "UNSET"),
        "provider_seed_supported": False,
        "temperature": matrix.config["temperature"],
        "top_p": matrix.config["top_p"],
        "seed": None,
        "vitis_version": os.environ.get("LLM4HLS_TOOLCHAIN_ID", "Vitis 2025.2"),
        "validation_profile": matrix.config["validation_profile"],
        "experience_mode": "off",
        "execution_order": matrix.config["execution_order"],
        "baseline_reuse": False,
        "candidate_reuse": False,
        "final_reuse": False,
        "planned_runs": len(selected_jobs),
        "full_pilot_runs": len(matrix.jobs),
        "conditions": matrix.config["conditions"],
        "git_commit": _git_head(repository_root),
        "output_dir": str(output_dir),
        "schedule_ref": "schedule.jsonl",
        "results_ref": "token_policy_abc_results.jsonl",
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
    results_path = output / "token_policy_abc_results.jsonl"
    if not resume and any(path.exists() for path in (manifest_path, results_path)):
        raise TokenPolicyABCError("output directory already contains a matrix run")
    _validate_schedule(schedule_path, jobs)
    if dry_run:
        manifest = _manifest(
            matrix, output, status="DRY_RUN", selected_jobs=jobs
        )
        _atomic_json(manifest_path, manifest)
        return manifest

    model, vitis_root = _preflight(matrix)
    executors = {
        condition: _condition_executor(matrix, condition)
        for condition in CONDITIONS
    }
    execution_identity = _execution_identity(
        matrix,
        model=model,
        vitis_root=vitis_root,
        executors=executors,
    )
    completed: dict[str, Mapping[str, object]] = {}
    if resume:
        completed = _load_resumed_records(
            matrix=matrix,
            jobs=jobs,
            output=output,
            manifest_path=manifest_path,
            results_path=results_path,
            execution_identity=execution_identity,
        )
    manifest = _manifest(
        matrix,
        output,
        status="RUNNING",
        selected_jobs=jobs,
        execution_identity=execution_identity,
    )
    _atomic_json(manifest_path, manifest)
    records: list[Mapping[str, object]] = []
    for job in jobs:
        prior = completed.get(job.run_id)
        if prior is not None:
            records.append(prior)
            continue
        descriptor = matrix.descriptors[job.task_id]
        if descriptor.task is None:
            raise TokenPolicyABCError(f"task became unloadable: {job.task_id}")
        run_dir = output / "runs" / job.run_id
        if run_dir.exists() and any(run_dir.iterdir()):
            raise TokenPolicyABCError(
                f"fresh run directory is not empty: {run_dir}"
            )
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
                timeout_seconds=float(matrix.config["runtime_limit_seconds"]) + 120.0,
            )
        except Exception as exc:
            error = exc
        wall = max(0.0, time.monotonic() - started)
        record = _extract_record(
            matrix,
            job,
            run_dir,
            result,
            wall_time_seconds=wall,
            error=error,
        )
        artifacts = _artifact_manifest(run_dir)
        artifact_path = run_dir / "abc_artifact_hashes.json"
        _atomic_json(artifact_path, artifacts)
        record.update(
            {
                "execution_identity_sha256": execution_identity[
                    "execution_identity_sha256"
                ],
                "artifact_manifest_ref": "abc_artifact_hashes.json",
                "artifact_manifest_sha256": _sha256_bytes(artifact_path.read_bytes()),
                "artifact_file_count": artifacts["file_count"],
            }
        )
        _atomic_json(run_dir / "abc_run_record.json", record)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m llm4hls_agent.token_policy_abc_runner",
        description="Run the public DeepSeek + Vitis Token Policy A/B/C matrix.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-runs", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.max_runs is not None and args.max_runs <= 0:
            raise TokenPolicyABCError("max-runs must be positive")
        matrix = load_matrix(args.config)
        result = run_matrix(
            matrix,
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
    print(_canonical_json(result))
    return 0 if result.get("status") in {"DRY_RUN", "COMPLETE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
