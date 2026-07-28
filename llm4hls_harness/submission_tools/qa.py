from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


TEXT_SUFFIXES = {
    "", ".c", ".cc", ".cpp", ".csv", ".diff", ".env", ".h", ".hpp", ".ini",
    ".json", ".jsonl", ".md", ".py", ".rst", ".sh", ".tcl", ".toml", ".txt",
    ".yaml", ".yml",
}
FORBIDDEN_PARTS = {
    ".cache", ".env", ".git", ".mypy_cache", ".pytest_cache", "__pycache__",
    "actions", "cache", "checkpoints", "golden", "hidden_like", "reference", "runs",
}
TEMP_SUFFIXES = {".bak", ".orig", ".rej", ".swp", ".tmp"}
PLANNER_FORBIDDEN_KEYS = {
    "answer", "golden", "golden_kernel", "hidden", "hidden_grader", "hidden_like",
    "hidden_testbench", "mutation_answer", "reference", "reference_solution",
}


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class _JsonArtifact:
    reference: str
    path: Path
    payload: dict[str, Any]
    raw_sha256: str
    canonical_sha256: str


_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_GIT_OBJECT_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


_CONTENT_RULES = (
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "possible API key"),
    (
        "API_KEY",
        re.compile(
            r"(?im)^\s*(?:export\s+)?(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|LLM4HLS_API_KEY)"
            r"\s*[:=]\s*[\"']?(?!\s*$|\$|\{|<|your|replace|redacted|changeme)[^\s\"'#]{12,}"
        ),
        "literal API key assignment",
    ),
    (
        "AUTHORIZATION",
        re.compile(r"(?i)authorization\s*[:=]\s*[\"']?bearer\s+(?!\$|\{|<|your-|replace-|redacted)[A-Za-z0-9._~+/-]{12,}"),
        "literal Authorization bearer token",
    ),
    ("LOCAL_PATH", re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+/"), "local home path"),
    ("WINDOWS_USER_PATH", re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+\\"), "local Windows user path"),
    (
        "LICENSE_VALUE",
        re.compile(r"(?i)(?:XILINXD_LICENSE_FILE|LM_LICENSE_FILE)\s*=\s*(?:[0-9]{2,6}@[^\s#]+|/(?:home|Users)/[^\s#]+)"),
        "embedded license location",
    ),
)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _path_findings(path: Path, root: Path, max_file_bytes: int) -> list[Finding]:
    rel = _relative(path, root)
    pure = PurePosixPath(rel)
    findings: list[Finding] = []
    lower_parts = {part.casefold() for part in pure.parts}
    forbidden = sorted(lower_parts & FORBIDDEN_PARTS)
    if forbidden:
        findings.append(Finding("FORBIDDEN_PATH", rel, f"forbidden path component: {', '.join(forbidden)}"))
    if pure.name.casefold().startswith(".env") and pure.name != ".env.example":
        findings.append(Finding("ENV_FILE", rel, "runtime environment file is not allowed"))
    if path.suffix.casefold() in TEMP_SUFFIXES or path.name.endswith("~"):
        findings.append(Finding("TEMP_FILE", rel, "temporary/editor file is not allowed"))
    if path.suffix.casefold() in {".lic", ".license"}:
        findings.append(Finding("LICENSE_FILE", rel, "license file is not allowed"))
    try:
        size = path.lstat().st_size
    except OSError:
        size = 0
    if size > max_file_bytes:
        findings.append(Finding("LARGE_FILE", rel, f"file size {size} exceeds {max_file_bytes}"))
    if path.is_symlink():
        findings.append(Finding("SYMLINK", rel, "symlinks are not allowed in staging"))
    return findings


def _walk_json(value: Any, location: str = "$") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{location}.{key}", (key, item)
            yield from _walk_json(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_json(item, f"{location}[{index}]")


def _planner_findings(path: Path, root: Path, text: str, *, force_planner_input: bool = False) -> list[Finding]:
    rel = _relative(path, root)
    normalized = rel.casefold()
    is_input = (
        force_planner_input
        or "/planner/inputs/" in f"/{normalized}"
        or "planner_input" in path.name.casefold()
    )
    if not is_input or path.suffix.casefold() != ".json":
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [Finding("PLANNER_INPUT_INVALID", rel, "Planner input is not valid JSON")]
    findings: list[Finding] = []
    for location, pair in _walk_json(payload):
        key, value = pair
        normalized_key = str(key).casefold().replace("-", "_")
        if normalized_key in PLANNER_FORBIDDEN_KEYS:
            findings.append(Finding("PLANNER_INPUT_LEAK", rel, f"forbidden key at {location}"))
        if isinstance(value, str):
            normalized_value = value.casefold().replace("\\", "/")
            if re.search(r"(?:^|/)(?:golden|hidden_like|reference)(?:/|$)", normalized_value):
                findings.append(Finding("PLANNER_INPUT_LEAK", rel, f"forbidden path value at {location}"))
    return findings


def _safe_run_path(
    *,
    run_root: Path,
    reference: object,
    expected_prefix: tuple[str, ...],
) -> tuple[str, Path] | None:
    if not isinstance(reference, str) or not reference:
        return None
    normalized = PurePosixPath(reference)
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or normalized.parts[: len(expected_prefix)] != expected_prefix
    ):
        return None
    path = run_root.joinpath(*normalized.parts)
    try:
        resolved_root = run_root.resolve()
        path.resolve().relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    current = resolved_root
    for part in normalized.parts:
        current = current / part
        if current.is_symlink():
            return None
    return reference, path


def _json_artifact(
    *,
    run_root: Path,
    reference: object,
    expected_prefix: tuple[str, ...],
    label: str,
    findings: list[Finding],
    code_prefix: str = "PROVENANCE",
) -> _JsonArtifact | None:
    """Load and hash one run-relative JSON object without accepting symlinks."""

    if not isinstance(reference, str) or not reference:
        findings.append(
            Finding(f"{code_prefix}_REF_MISSING", run_root.name, f"missing {label}")
        )
        return None
    safe = _safe_run_path(
        run_root=run_root,
        reference=reference,
        expected_prefix=expected_prefix,
    )
    if safe is None:
        findings.append(
            Finding(f"{code_prefix}_REF_UNSAFE", run_root.name, f"invalid {label}: {reference}")
        )
        return None
    reference, path = safe
    if not path.is_file():
        findings.append(
            Finding(
                f"{code_prefix}_ARTIFACT_MISSING",
                run_root.name,
                f"missing {label} artifact: {reference}",
            )
        )
        return None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        findings.append(
            Finding(
                f"{code_prefix}_ARTIFACT_INVALID",
                run_root.name,
                f"invalid {label} artifact: {exc}",
            )
        )
        return None
    if not isinstance(payload, dict):
        findings.append(
            Finding(
                f"{code_prefix}_ARTIFACT_INVALID",
                run_root.name,
                f"{label} artifact is not an object",
            )
        )
        return None
    return _JsonArtifact(
        reference=reference,
        path=path,
        payload=payload,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        canonical_sha256=_canonical_sha256(payload),
    )


def _provider_compatible(request_provider: str, outcome_provider: str) -> bool:
    """Accept only the provider variants emitted by the current adapters."""

    if request_provider == outcome_provider:
        return True
    return request_provider == "openai-compatible" and outcome_provider in {
        "openai-compatible-fast-experiment",
        "openai-compatible-task-aware",
    }


def _live_planner_chain_findings(
    *,
    run_root: Path,
    action_id: str,
    completed: _JsonArtifact,
) -> tuple[list[Finding], dict[str, int] | None, dict[str, str] | None]:
    findings: list[Finding] = []
    location = run_root.name
    value = completed.payload
    if value.get("action_id") != action_id or value.get("status") != "COMPLETED":
        findings.append(
            Finding("REAL_LLM_ACTION_BINDING_MISMATCH", location, f"invalid completed action {action_id}")
        )
    request_meta = value.get("request")
    request_meta = request_meta if isinstance(request_meta, dict) else {}
    request_artifact = _json_artifact(
        run_root=run_root,
        reference=request_meta.get("request_ref"),
        expected_prefix=("planner", "requests"),
        label=f"live Planner request for {action_id}",
        findings=findings,
    )
    outcome_artifact = _json_artifact(
        run_root=run_root,
        reference=value.get("result_ref"),
        expected_prefix=("planner", "live_outcomes"),
        label=f"live Planner outcome for {action_id}",
        findings=findings,
    )
    started_artifact = _json_artifact(
        run_root=run_root,
        reference=f"control/live_planner_actions/{action_id}.started.json",
        expected_prefix=("control", "live_planner_actions"),
        label=f"live Planner STARTED for {action_id}",
        findings=findings,
    )
    if request_artifact is None or outcome_artifact is None or started_artifact is None:
        return findings, None, None

    if _canonical_sha256(request_meta) != action_id:
        findings.append(
            Finding("REAL_LLM_ACTION_BINDING_MISMATCH", location, f"action ID mismatch for {action_id}")
        )
    if request_meta.get("request_sha256") != request_artifact.canonical_sha256:
        findings.append(
            Finding("REAL_LLM_REQUEST_SHA_MISMATCH", location, f"request SHA-256 mismatch for {action_id}")
        )
    if value.get("result_sha256") != outcome_artifact.raw_sha256:
        findings.append(
            Finding("REAL_LLM_OUTPUT_SHA_MISMATCH", location, f"outcome SHA-256 mismatch for {action_id}")
        )
    started = started_artifact.payload
    if (
        started.get("action_id") != action_id
        or started.get("status") != "STARTED"
        or started.get("request") != request_meta
    ):
        findings.append(
            Finding("REAL_LLM_ACTION_BINDING_MISMATCH", location, f"STARTED/COMPLETED mismatch for {action_id}")
        )

    request = request_artifact.payload
    outcome = outcome_artifact.payload
    if (
        request.get("logical_operation_id") != request_meta.get("logical_operation_id")
        or outcome.get("action_id") != action_id
        or outcome_artifact.reference != f"planner/live_outcomes/{action_id}.json"
    ):
        findings.append(
            Finding("REAL_LLM_ACTION_BINDING_MISMATCH", location, f"request/outcome identity mismatch for {action_id}")
        )
    input_sha = request.get("input_sha256")
    if (
        not isinstance(input_sha, str)
        or _SHA256.fullmatch(input_sha) is None
        or outcome.get("input_sha256") != input_sha
        or request_meta.get("input_sha256") != input_sha
    ):
        findings.append(
            Finding("REAL_LLM_INPUT_SHA_MISMATCH", location, f"request/outcome input SHA mismatch for {action_id}")
        )
    input_artifact = _json_artifact(
        run_root=run_root,
        reference=request_meta.get("input_ref"),
        expected_prefix=("planner", "inputs"),
        label=f"Planner input for {action_id}",
        findings=findings,
    )
    if input_artifact is not None and input_artifact.canonical_sha256 != input_sha:
        findings.append(
            Finding("REAL_LLM_INPUT_SHA_MISMATCH", location, f"Planner input artifact mismatch for {action_id}")
        )

    provider_request = request.get("request")
    provider_request = provider_request if isinstance(provider_request, dict) else {}
    provider_request = provider_request.get("provider_request")
    provider_request = provider_request if isinstance(provider_request, dict) else {}
    request_provider = str(provider_request.get("provider") or "").strip()
    request_model = str(provider_request.get("model") or "").strip()
    http_body = provider_request.get("http_body")
    http_body = http_body if isinstance(http_body, dict) else {}
    provider_binding = outcome.get("provider_binding")
    provider_binding = provider_binding if isinstance(provider_binding, dict) else {}
    outcome_provider = str(provider_binding.get("provider") or "").strip()
    outcome_model = str(provider_binding.get("model") or "").strip()
    proposal = outcome.get("proposal")
    proposal = proposal if isinstance(proposal, dict) else {}
    proposal_provider = str(proposal.get("provider") or "").strip()
    proposal_model = str(proposal.get("model") or "").strip()
    if not all((request_provider, outcome_provider, proposal_provider)):
        findings.append(
            Finding("REAL_LLM_PROVIDER_MISSING", location, f"provider binding missing for {action_id}")
        )
    elif (
        proposal_provider != outcome_provider
        or not _provider_compatible(request_provider, outcome_provider)
    ):
        findings.append(
            Finding("REAL_LLM_PROVIDER_MISMATCH", location, f"provider binding mismatch for {action_id}")
        )
    if not all((request_model, outcome_model, proposal_model)):
        findings.append(
            Finding("REAL_LLM_MODEL_MISSING", location, f"model binding missing for {action_id}")
        )
    elif not (request_model == outcome_model == proposal_model):
        findings.append(
            Finding("REAL_LLM_MODEL_MISMATCH", location, f"model binding mismatch for {action_id}")
        )
    body_model = http_body.get("model")
    if body_model is not None and body_model != request_model:
        findings.append(
            Finding("REAL_LLM_MODEL_MISMATCH", location, f"HTTP model mismatch for {action_id}")
        )
    if (
        request.get("planner_fingerprint") != request_meta.get("planner_fingerprint")
        or provider_binding.get("planner_fingerprint") != request.get("planner_fingerprint")
    ):
        findings.append(
            Finding("REAL_LLM_PROVIDER_MISMATCH", location, f"Planner fingerprint mismatch for {action_id}")
        )

    usage = outcome.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens_used = usage.get("tokens_used")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cached_input_tokens = usage.get("cached_input_tokens", 0)
    request_id = usage.get("request_id")
    if (
        not _positive_integer(tokens_used)
        or not _positive_integer(input_tokens)
        or not _positive_integer(output_tokens)
        or not _nonnegative_integer(cached_input_tokens)
        or usage.get("usage_complete") is not True
        or tokens_used != input_tokens + output_tokens
    ):
        findings.append(
            Finding("REAL_LLM_OUTCOME_USAGE_INVALID", location, f"invalid usage for {action_id}")
        )
        usage_totals = None
    else:
        usage_totals = {
            "tokens_used": int(tokens_used),
            "input_tokens_used": int(input_tokens),
            "output_tokens_used": int(output_tokens),
            "cached_input_tokens_used": int(cached_input_tokens),
        }
    if not isinstance(request_id, str) or not request_id.strip():
        findings.append(
            Finding("REAL_LLM_REQUEST_ID_INVALID", location, f"missing request_id for {action_id}")
        )
    if (
        proposal.get("request_id") != request_id
        or proposal.get("input_tokens") != input_tokens
        or proposal.get("output_tokens") != output_tokens
        or proposal.get("cached_input_tokens", 0) != cached_input_tokens
        or value.get("tokens_used") != tokens_used
        or value.get("input_tokens") != input_tokens
        or value.get("output_tokens") != output_tokens
        or value.get("cached_input_tokens", 0) != cached_input_tokens
    ):
        findings.append(
            Finding("REAL_LLM_USAGE_MISMATCH", location, f"proposal/journal usage mismatch for {action_id}")
        )
    identity = {
        "action_id": action_id,
        "request_ref": request_artifact.reference,
        "request_sha256": request_artifact.canonical_sha256,
        "output_ref": outcome_artifact.reference,
        "output_sha256": outcome_artifact.raw_sha256,
        "request_id": str(request_id or ""),
    }
    return findings, usage_totals, identity


def validate_run_provenance(
    *,
    run_root: Path,
    result: dict[str, Any],
    provider_class: str,
) -> list[Finding]:
    """Fail-closed checks for evidence labels used by submission reports.

    This validates model-call provenance only.  A real model attempt may still
    fail HLS validation, so Vitis closure is deliberately evaluated separately
    by the report generator before it makes a combined LLM+Vitis claim.
    """

    if provider_class != "REAL_LLM":
        return []

    findings: list[Finding] = []
    budget = result.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    tools = budget.get("tool_used")
    tools = tools if isinstance(tools, dict) else {}
    llm_calls = tools.get("llm")
    if not _positive_integer(llm_calls):
        findings.append(
            Finding("REAL_LLM_CALLS_MISSING", run_root.name, "REAL_LLM requires a positive model-call count")
        )
    token_fields = (
        budget.get("tokens_used"),
        budget.get("input_tokens_used"),
        budget.get("output_tokens_used"),
    )
    if (
        not all(_positive_integer(value) for value in token_fields)
        or budget.get("token_usage_complete") is not True
        or budget.get("tokens_used")
        != budget.get("input_tokens_used", 0) + budget.get("output_tokens_used", 0)
    ):
        findings.append(
            Finding(
                "REAL_LLM_TOKENS_INVALID",
                run_root.name,
                "REAL_LLM requires complete, positive input/output/total token usage",
            )
        )

    action_root = run_root / "control" / "live_planner_actions"
    completed_paths = sorted(action_root.glob("*.completed.json"))
    usage_sums = {
        "tokens_used": 0,
        "input_tokens_used": 0,
        "output_tokens_used": 0,
        "cached_input_tokens_used": 0,
    }
    identities: dict[str, dict[str, str]] = {}
    request_ids: set[str] = set()
    for path in completed_paths:
        action_id = path.name.removesuffix(".completed.json")
        completed = _json_artifact(
            run_root=run_root,
            reference=f"control/live_planner_actions/{path.name}",
            expected_prefix=("control", "live_planner_actions"),
            label=f"live Planner COMPLETED for {action_id}",
            findings=findings,
        )
        if completed is None:
            continue
        chain_findings, usage, identity = _live_planner_chain_findings(
            run_root=run_root,
            action_id=action_id,
            completed=completed,
        )
        findings.extend(chain_findings)
        if usage is not None:
            for key, value in usage.items():
                usage_sums[key] += value
        if identity is not None:
            identities[action_id] = identity
            request_id = identity["request_id"]
            if request_id in request_ids:
                findings.append(
                    Finding("REAL_LLM_REQUEST_ID_INVALID", run_root.name, f"duplicate request_id for {action_id}")
                )
            request_ids.add(request_id)

    if not _positive_integer(llm_calls) or len(completed_paths) != llm_calls:
        findings.append(
            Finding(
                "REAL_LLM_CALL_COUNT_MISMATCH",
                run_root.name,
                f"budget records {llm_calls!r} LLM calls but {len(completed_paths)} completed live actions exist",
            )
        )
    for key in ("tokens_used", "input_tokens_used", "output_tokens_used"):
        if budget.get(key) != usage_sums[key]:
            findings.append(
                Finding(
                    "REAL_LLM_BUDGET_USAGE_MISMATCH",
                    run_root.name,
                    f"budget {key} does not match completed live outcomes",
                )
            )
    if "cached_input_tokens_used" in budget and budget.get("cached_input_tokens_used") != usage_sums["cached_input_tokens_used"]:
        findings.append(
            Finding(
                "REAL_LLM_BUDGET_USAGE_MISMATCH",
                run_root.name,
                "budget cached_input_tokens_used does not match completed live outcomes",
            )
        )

    terminal_action_id = result.get("live_planner_action_id")
    terminal_identity = identities.get(terminal_action_id) if isinstance(terminal_action_id, str) else None
    if terminal_identity is None:
        findings.append(
            Finding("REAL_LLM_ACTION_BINDING_MISMATCH", run_root.name, "terminal live Planner action is not completed")
        )
    else:
        terminal_fields = {
            "live_planner_request_ref": "request_ref",
            "live_planner_request_sha256": "request_sha256",
            "live_planner_output_ref": "output_ref",
            "live_planner_output_sha256": "output_sha256",
        }
        for result_key, identity_key in terminal_fields.items():
            if result.get(result_key) != terminal_identity[identity_key]:
                code = (
                    "PROVENANCE_REF_MISSING"
                    if result.get(result_key) in (None, "") and result_key.endswith("_ref")
                    else
                    "REAL_LLM_REQUEST_SHA_MISMATCH"
                    if result_key == "live_planner_request_sha256"
                    else "REAL_LLM_OUTPUT_SHA_MISMATCH"
                    if result_key == "live_planner_output_sha256"
                    else "REAL_LLM_ACTION_BINDING_MISMATCH"
                )
                findings.append(
                    Finding(code, run_root.name, f"terminal {result_key} does not match the completed action")
                )
    return sorted(set(findings), key=lambda item: (item.code, item.detail))


def _manifest_artifact_map(
    *,
    run_root: Path,
    manifest: dict[str, Any],
    findings: list[Finding],
) -> dict[str, tuple[str, int]]:
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        findings.append(
            Finding("PACKAGE_ARTIFACTS_INVALID", run_root.name, "package manifest artifacts is not a list")
        )
        return {}
    output: dict[str, tuple[str, int]] = {}
    for record in records:
        if not isinstance(record, dict):
            findings.append(
                Finding("PACKAGE_ARTIFACTS_INVALID", run_root.name, "package manifest contains a non-object artifact")
            )
            continue
        reference = record.get("path")
        digest = record.get("sha256")
        size = record.get("size_bytes")
        safe = _safe_run_path(run_root=run_root, reference=reference, expected_prefix=())
        if (
            safe is None
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not _nonnegative_integer(size)
            or reference in output
        ):
            findings.append(
                Finding("PACKAGE_ARTIFACTS_INVALID", run_root.name, f"invalid package artifact record: {reference!r}")
            )
            continue
        output[str(reference)] = (digest, int(size))
    return output


def _verify_manifest_file(
    *,
    run_root: Path,
    artifact: _JsonArtifact,
    manifest_artifacts: dict[str, tuple[str, int]],
    findings: list[Finding],
    label: str,
) -> None:
    expected = manifest_artifacts.get(artifact.reference)
    if expected is None:
        findings.append(
            Finding("PACKAGE_ARTIFACT_MISSING", run_root.name, f"{label} is absent from the package manifest")
        )
        return
    digest, size = expected
    if digest != artifact.raw_sha256 or size != artifact.path.stat().st_size:
        findings.append(
            Finding("PACKAGE_ARTIFACT_HASH_MISMATCH", run_root.name, f"{label} does not match its package manifest hash")
        )


def validate_real_vitis_evidence(
    *,
    run_root: Path,
    result: dict[str, Any],
    require_live_planner_candidate: bool = False,
) -> list[Finding]:
    """Validate the sealed evidence needed for a REAL_VITIS final-closure claim.

    The terminal JSON is only a commit marker.  This verifier follows its
    package commit, checks the terminal payload digest, and independently binds
    each final action to the sealed task and final Candidate source.
    """

    findings: list[Finding] = []
    location = run_root.name
    backend = result.get("backend")
    backend = backend if isinstance(backend, dict) else {}
    if (
        backend.get("evidence_level") != "REAL_VITIS_VALIDATED"
        or backend.get("class") != "llm4hls_agent.vitis.VitisBackend"
    ):
        findings.append(
            Finding("REAL_VITIS_BACKEND_INVALID", location, "REAL_VITIS claim is not bound to VitisBackend")
        )

    package = result.get("package")
    package = package if isinstance(package, dict) else {}
    manifest = _json_artifact(
        run_root=run_root,
        reference=package.get("manifest_ref"),
        expected_prefix=("control",),
        label="package manifest",
        findings=findings,
        code_prefix="PACKAGE",
    )
    if manifest is None:
        return sorted(set(findings), key=lambda item: (item.code, item.detail))
    manifest_sha = package.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or manifest_sha != manifest.canonical_sha256:
        findings.append(
            Finding("PACKAGE_MANIFEST_SHA_MISMATCH", location, "package manifest canonical SHA-256 mismatch")
        )
    terminal_payload = dict(result)
    terminal_payload.pop("package", None)
    if manifest.payload.get("terminal_payload_sha256") != _canonical_sha256(terminal_payload):
        findings.append(
            Finding("TERMINAL_PAYLOAD_SHA_MISMATCH", location, "terminal payload SHA-256 mismatch")
        )
    final_candidate_id = result.get("final_candidate_id")
    if (
        manifest.payload.get("task_id") != result.get("task_id")
        or manifest.payload.get("status") != result.get("status")
        or manifest.payload.get("final_candidate_id") != final_candidate_id
    ):
        findings.append(
            Finding("PACKAGE_IDENTITY_MISMATCH", location, "package task/status/final Candidate identity mismatch")
        )
    if result.get("final_attempt_candidate_id") != final_candidate_id:
        findings.append(
            Finding("REAL_VITIS_CANDIDATE_MISMATCH", location, "final attempt and committed Candidate IDs differ")
        )
    manifest_artifacts = _manifest_artifact_map(
        run_root=run_root,
        manifest=manifest.payload,
        findings=findings,
    )

    task_spec = _json_artifact(
        run_root=run_root,
        reference="v3_task_spec.json",
        expected_prefix=(),
        label="task spec",
        findings=findings,
        code_prefix="REAL_VITIS",
    )
    registry = _json_artifact(
        run_root=run_root,
        reference="candidate_registry.json",
        expected_prefix=(),
        label="Candidate registry",
        findings=findings,
        code_prefix="REAL_VITIS",
    )
    if task_spec is None or registry is None:
        return sorted(set(findings), key=lambda item: (item.code, item.detail))
    _verify_manifest_file(
        run_root=run_root,
        artifact=task_spec,
        manifest_artifacts=manifest_artifacts,
        findings=findings,
        label="task spec",
    )
    _verify_manifest_file(
        run_root=run_root,
        artifact=registry,
        manifest_artifacts=manifest_artifacts,
        findings=findings,
        label="Candidate registry",
    )
    if task_spec.payload.get("task_id") != result.get("task_id") or registry.payload.get("task_id") != result.get("task_id"):
        findings.append(
            Finding("REAL_VITIS_TASK_MISMATCH", location, "task spec/Registry/terminal task IDs differ")
        )
    task_fingerprint = _canonical_sha256(
        {
            "task_id": task_spec.payload.get("task_id"),
            "top": task_spec.payload.get("top"),
            "kernel_file": task_spec.payload.get("kernel_file"),
            "public_tb": task_spec.payload.get("public_tb"),
            "public_file_hashes": task_spec.payload.get("public_file_hashes"),
        }
    )

    candidates = registry.payload.get("candidates")
    candidate = candidates.get(final_candidate_id) if isinstance(candidates, dict) else None
    if not isinstance(final_candidate_id, str) or not isinstance(candidate, dict):
        findings.append(
            Finding("REAL_VITIS_CANDIDATE_MISSING", location, "final Candidate is absent from the sealed Registry")
        )
        return sorted(set(findings), key=lambda item: (item.code, item.detail))
    if (
        candidate.get("candidate_id") != final_candidate_id
        or registry.payload.get("final_candidate_id") != final_candidate_id
    ):
        findings.append(
            Finding("REAL_VITIS_CANDIDATE_MISMATCH", location, "terminal and Registry final Candidate IDs differ")
        )
    code_hash = candidate.get("code_hash")
    source_ref = candidate.get("source_ref")
    safe_source = _safe_run_path(
        run_root=run_root,
        reference=source_ref,
        expected_prefix=("candidates", final_candidate_id, "source"),
    )
    if (
        not isinstance(code_hash, str)
        or _SHA256.fullmatch(code_hash) is None
        or safe_source is None
        or not safe_source[1].is_file()
    ):
        findings.append(
            Finding("REAL_VITIS_CODE_BINDING_INVALID", location, "final Candidate source/code binding is incomplete")
        )
    else:
        source_bytes = safe_source[1].read_bytes()
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        manifest_source = manifest_artifacts.get(str(source_ref))
        if source_digest != code_hash:
            findings.append(
                Finding("REAL_VITIS_CODE_HASH_MISMATCH", location, "final Candidate source SHA-256 mismatch")
            )
        if manifest_source != (source_digest, len(source_bytes)):
            findings.append(
                Finding("PACKAGE_ARTIFACT_HASH_MISMATCH", location, "final Candidate source is not sealed by the manifest")
            )

    if require_live_planner_candidate:
        live_action_id = candidate.get("live_planner_action_id")
        if not isinstance(live_action_id, str) or _SHA256.fullmatch(live_action_id) is None:
            findings.append(
                Finding("REAL_LLM_CANDIDATE_PROVENANCE_MISSING", location, "final Candidate has no live Planner action binding")
            )
        else:
            completed = _json_artifact(
                run_root=run_root,
                reference=f"control/live_planner_actions/{live_action_id}.completed.json",
                expected_prefix=("control", "live_planner_actions"),
                label="final Candidate live Planner COMPLETED",
                findings=findings,
            )
            if completed is not None:
                chain_findings, _usage, identity = _live_planner_chain_findings(
                    run_root=run_root,
                    action_id=live_action_id,
                    completed=completed,
                )
                findings.extend(chain_findings)
                _verify_manifest_file(
                    run_root=run_root,
                    artifact=completed,
                    manifest_artifacts=manifest_artifacts,
                    findings=findings,
                    label="final Candidate live Planner COMPLETED",
                )
                if identity is not None:
                    candidate_fields = {
                        "live_planner_request_ref": "request_ref",
                        "live_planner_request_sha256": "request_sha256",
                        "live_planner_output_ref": "output_ref",
                        "live_planner_output_sha256": "output_sha256",
                    }
                    if any(
                        candidate.get(candidate_key) != identity[identity_key]
                        for candidate_key, identity_key in candidate_fields.items()
                    ):
                        findings.append(
                            Finding("REAL_LLM_CANDIDATE_PROVENANCE_MISMATCH", location, "final Candidate live Planner refs/hashes differ")
                        )
                    request_artifact = _json_artifact(
                        run_root=run_root,
                        reference=identity["request_ref"],
                        expected_prefix=("planner", "requests"),
                        label="final Candidate live Planner request",
                        findings=findings,
                    )
                    outcome_artifact = _json_artifact(
                        run_root=run_root,
                        reference=identity["output_ref"],
                        expected_prefix=("planner", "live_outcomes"),
                        label="final Candidate live Planner outcome",
                        findings=findings,
                    )
                    if request_artifact is not None:
                        _verify_manifest_file(
                            run_root=run_root,
                            artifact=request_artifact,
                            manifest_artifacts=manifest_artifacts,
                            findings=findings,
                            label="final Candidate live Planner request",
                        )
                    if outcome_artifact is not None:
                        _verify_manifest_file(
                            run_root=run_root,
                            artifact=outcome_artifact,
                            manifest_artifacts=manifest_artifacts,
                            findings=findings,
                            label="final Candidate live Planner outcome",
                        )
                        proposal = outcome_artifact.payload.get("proposal")
                        proposal = proposal if isinstance(proposal, dict) else {}
                        if (
                            candidate.get("provider") != proposal.get("provider")
                            or candidate.get("model") != proposal.get("model")
                        ):
                            findings.append(
                                Finding("REAL_LLM_CANDIDATE_PROVENANCE_MISMATCH", location, "final Candidate provider/model differs from live outcome")
                            )
                        patch_ref = candidate.get("patch_ref")
                        patch_sha = candidate.get("patch_sha256")
                        safe_patch = _safe_run_path(
                            run_root=run_root,
                            reference=patch_ref,
                            expected_prefix=("candidates", final_candidate_id),
                        )
                        proposal_patch = proposal.get("patch")
                        if (
                            safe_patch is None
                            or not safe_patch[1].is_file()
                            or not isinstance(patch_sha, str)
                            or not isinstance(proposal_patch, str)
                        ):
                            findings.append(
                                Finding("REAL_LLM_CANDIDATE_PATCH_INVALID", location, "final Candidate Patch binding is incomplete")
                            )
                        else:
                            patch_bytes = safe_patch[1].read_bytes()
                            patch_digest = hashlib.sha256(patch_bytes).hexdigest()
                            if (
                                patch_digest != patch_sha
                                or hashlib.sha256(proposal_patch.encode("utf-8")).hexdigest() != patch_sha
                                or manifest_artifacts.get(str(patch_ref))
                                != (patch_digest, len(patch_bytes))
                            ):
                                findings.append(
                                    Finding("REAL_LLM_CANDIDATE_PATCH_INVALID", location, "final Candidate Patch is not sealed to the live outcome")
                                )

    validation = result.get("final_validation")
    validation = validation if isinstance(validation, dict) else {}
    seen_action_ids: set[str] = set()
    for stage in ("csim", "synth", "cosim"):
        record = validation.get(stage)
        record = record if isinstance(record, dict) else {}
        action_id = record.get("action_id")
        result_ref = record.get("result_ref")
        if (
            record.get("status") != "PASS"
            or record.get("ok") is not True
            or record.get("phase") != "pass"
            or record.get("validation_scope") != "search_closeout"
            or record.get("cached") is not False
            or not isinstance(action_id, str)
            or _SHA256.fullmatch(action_id) is None
            or action_id in seen_action_ids
            or result_ref != f"actions/{action_id}/result.json"
        ):
            findings.append(
                Finding("FINAL_ACTION_TERMINAL_INVALID", location, f"terminal final {stage} record is not fresh and complete")
            )
            continue
        seen_action_ids.add(action_id)
        action = _json_artifact(
            run_root=run_root,
            reference=result_ref,
            expected_prefix=("actions", action_id),
            label=f"final {stage} action result",
            findings=findings,
            code_prefix="FINAL_ACTION",
        )
        if action is None:
            continue
        _verify_manifest_file(
            run_root=run_root,
            artifact=action,
            manifest_artifacts=manifest_artifacts,
            findings=findings,
            label=f"final {stage} action result",
        )
        action_value = action.payload
        expected_action_payload = {
            "kind": stage,
            "candidate_id": final_candidate_id,
            "code_hash": code_hash,
            "tool_config_hash": action_value.get("tool_config_hash"),
            "backend_fingerprint": action_value.get("backend_fingerprint"),
            "task_fingerprint": task_fingerprint,
            "validation_scope": "search_closeout",
        }
        terminal_binding_keys = (
            "action_id",
            "code_hash",
            "tool_config_hash",
            "backend_fingerprint",
            "task_fingerprint",
            "validation_scope",
        )
        if (
            action_value.get("action_id") != action_id
            or action_value.get("kind") != stage
            or action_value.get("candidate_id") != final_candidate_id
            or action_value.get("code_hash") != code_hash
            or action_value.get("task_fingerprint") != task_fingerprint
            or action_value.get("backend_fingerprint") != backend.get("fingerprint")
            or action_value.get("result_ref") != result_ref
            or action_value.get("ok") is not True
            or action_value.get("phase") != "pass"
            or action_value.get("validation_scope") != "search_closeout"
            or action_value.get("cached") is not False
            or any(record.get(key) != action_value.get(key) for key in terminal_binding_keys)
            or _canonical_sha256(expected_action_payload) != action_id
        ):
            findings.append(
                Finding("FINAL_ACTION_BINDING_MISMATCH", location, f"final {stage} action is not bound to task/Candidate/code")
            )

    if result.get("status") != "DONE" or not isinstance(final_candidate_id, str):
        return sorted(set(findings), key=lambda item: (item.code, item.detail))

    certification = result.get("final_certification")
    certification = certification if isinstance(certification, dict) else {}
    receipt_ref = certification.get("receipt_ref")
    receipt_artifact = _json_artifact(
        run_root=run_root,
        reference=receipt_ref,
        expected_prefix=("certification",),
        label="independent final certification receipt",
        findings=findings,
        code_prefix="FINAL_CERTIFICATION",
    )
    if receipt_artifact is None:
        findings.append(
            Finding(
                "FINAL_CERTIFICATION_MISSING",
                location,
                "successful run lacks an independent certification receipt",
            )
        )
    else:
        receipt = receipt_artifact.payload
        receipt_payload = {
            key: value
            for key, value in receipt.items()
            if key != "receipt_sha256"
        }
        stages = receipt.get("stages")
        stages = stages if isinstance(stages, dict) else {}
        clock_gate = receipt.get("clock_gate")
        clock_gate = clock_gate if isinstance(clock_gate, dict) else {}
        agent_ledger = receipt.get("agent_ledger")
        agent_ledger = agent_ledger if isinstance(agent_ledger, dict) else {}
        receipt_valid = (
            receipt.get("schema_version")
            == "v3.final-certification-receipt.v1"
            and receipt.get("status") == "PASS"
            and receipt.get("budget_domain")
            == "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET"
            and receipt.get("agent_credits_charged") == 0
            and receipt.get("feedback_policy")
            == "NO_SAME_RUN_AGENT_FEEDBACK"
            and receipt.get("receipt_sha256")
            == _canonical_sha256(receipt_payload)
            and certification.get("receipt_sha256")
            == receipt.get("receipt_sha256")
            and certification.get("status") == "PASS"
            and certification.get("budget_domain")
            == "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET"
            and certification.get("agent_credits_charged") == 0
            and all(
                isinstance(stages.get(stage), dict)
                and stages[stage].get("kind") == stage
                and stages[stage].get("candidate_id") == final_candidate_id
                and stages[stage].get("code_hash") == code_hash
                and stages[stage].get("ok") is True
                for stage in ("csim", "synth", "cosim")
            )
            and clock_gate.get("passed") is True
            and clock_gate.get("maximum_period_ns") == 10.0
            and agent_ledger.get("unchanged") is True
            and agent_ledger.get("before_sha256")
            == agent_ledger.get("after_sha256")
        )
        if not receipt_valid:
            findings.append(
                Finding(
                    "FINAL_CERTIFICATION_INVALID",
                    location,
                    "independent certification receipt is incomplete or unbound",
                )
            )
        freeze_ref = receipt.get("frozen_candidate_ref")
        freeze_artifact = _json_artifact(
            run_root=run_root,
            reference=freeze_ref,
            expected_prefix=("control",),
            label="frozen final Candidate",
            findings=findings,
            code_prefix="FINAL_CERTIFICATION_FREEZE",
        )
        if (
            freeze_artifact is None
            or freeze_artifact.payload.get("schema_version")
            != "v3.frozen-search-candidate.v1"
            or freeze_artifact.payload.get("candidate_id") != final_candidate_id
            or freeze_artifact.payload.get("source_sha256") != code_hash
        ):
            findings.append(
                Finding(
                    "FINAL_CERTIFICATION_FREEZE_INVALID",
                    location,
                    "certification does not bind the frozen final Candidate",
                )
            )
        ledger_path = run_root / "budget_ledger.jsonl"
        if ledger_path.is_file():
            ledger_digest = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
            if (
                agent_ledger.get("before_sha256") != ledger_digest
                or agent_ledger.get("after_sha256") != ledger_digest
            ):
                findings.append(
                    Finding(
                        "FINAL_CERTIFICATION_LEDGER_MUTATED",
                        location,
                        "certification receipt does not preserve Agent Ledger bytes",
                    )
                )
    return sorted(set(findings), key=lambda item: (item.code, item.detail))


def scan_tree(root: Path, *, max_file_bytes: int = 5 * 1024 * 1024) -> list[Finding]:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    findings: list[Finding] = []
    force_planner_input = root.name.casefold() == "inputs" and root.parent.name.casefold() == "planner"
    for path in sorted(root.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        findings.extend(_path_findings(path, root, max_file_bytes))
        if path.is_symlink() or path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(Finding("READ_ERROR", _relative(path, root), str(exc)))
            continue
        for code, pattern, detail in _CONTENT_RULES:
            if pattern.search(text):
                findings.append(Finding(code, _relative(path, root), detail))
        findings.extend(_planner_findings(path, root, text, force_planner_input=force_planner_input))
    return sorted(set(findings), key=lambda item: (item.path, item.code, item.detail))


def _file_findings(path: Path, root: Path, max_file_bytes: int) -> list[Finding]:
    findings = _path_findings(path, root, max_file_bytes)
    if path.is_symlink() or path.suffix.casefold() not in TEXT_SUFFIXES:
        return findings
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return findings + [Finding("READ_ERROR", _relative(path, root), str(exc))]
    for code, pattern, detail in _CONTENT_RULES:
        if pattern.search(text):
            findings.append(Finding(code, _relative(path, root), detail))
    findings.extend(_planner_findings(path, root, text))
    return sorted(set(findings), key=lambda item: (item.path, item.code, item.detail))


def _safe_source_file(source_root: Path, relative: str) -> Path:
    normalized = PurePosixPath(relative)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"unsafe staging include: {relative}")
    path = source_root / Path(*normalized.parts)
    try:
        path.resolve().relative_to(source_root.resolve())
    except ValueError as exc:
        raise ValueError(f"staging include escapes source root: {relative}") from exc
    return path


def _excluded(relative: str, excludes: list[str]) -> bool:
    pure = PurePosixPath(relative)
    return any(pure.match(pattern) for pattern in excludes)


def _git_source_identity(source_root: Path) -> dict[str, Any]:
    """Return conservative Git provenance; an environment cannot forge CLEAN."""

    observed_revision: str | None = None
    observed_state: str | None = None
    try:
        top = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if top.returncode == 0 and top.stdout.strip():
            head = subprocess.run(
                ["git", "-C", str(source_root), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            status = subprocess.run(
                ["git", "-C", str(source_root), "status", "--porcelain=v1", "--untracked-files=all"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if head.returncode == 0 and _GIT_OBJECT_ID.fullmatch(head.stdout.strip().casefold()):
                observed_revision = head.stdout.strip().casefold()
            if status.returncode == 0:
                observed_state = "DIRTY" if status.stdout.strip() else "CLEAN"
    except (OSError, subprocess.SubprocessError):
        pass

    revision_override_raw = os.environ.get("SOURCE_REVISION", "").strip().casefold()
    revision_override = (
        revision_override_raw
        if _GIT_OBJECT_ID.fullmatch(revision_override_raw)
        else None
    )
    state_override_raw = os.environ.get("SOURCE_TREE_STATE", "").strip().upper()
    state_override = (
        state_override_raw
        if state_override_raw
        in {"CLEAN", "CLEAN_AFTER_MANUAL_GIT_CHECK", "DIRTY", "DIRTY_OR_UNVERIFIED"}
        else None
    )
    if observed_revision is not None:
        # A conflicting override is preserved for review, but never replaces a
        # directly observed commit hash in the authoritative field.
        source_revision = observed_revision
        revision_origin = "git"
    elif revision_override is not None:
        source_revision = revision_override
        revision_origin = "environment_unverified"
    else:
        source_revision = "UNRECORDED"
        revision_origin = "unverified"

    if observed_state == "DIRTY":
        source_tree_state = "DIRTY"
        tree_origin = "git"
    elif observed_state == "CLEAN":
        if state_override in {"DIRTY", "DIRTY_OR_UNVERIFIED"}:
            source_tree_state = state_override
            tree_origin = "environment_conservative_override"
        else:
            source_tree_state = "CLEAN"
            tree_origin = "git"
    else:
        # In particular, CLEAN_AFTER_MANUAL_GIT_CHECK is not accepted when
        # Git itself could not verify the source tree.
        if state_override == "DIRTY":
            source_tree_state = "DIRTY"
            tree_origin = "environment_unverified"
        else:
            source_tree_state = "DIRTY_OR_UNVERIFIED"
            tree_origin = "unverified"
    return {
        "source_revision": source_revision,
        "source_revision_origin": revision_origin,
        "source_revision_observed": observed_revision,
        "source_revision_override": revision_override,
        "source_revision_override_valid": not revision_override_raw or revision_override is not None,
        "source_tree_state": source_tree_state,
        "source_tree_state_origin": tree_origin,
        "source_tree_state_observed": observed_state,
        "source_tree_state_override": state_override,
        "source_tree_state_override_valid": not state_override_raw or state_override is not None,
    }


def stage_submission(*, source_root: Path, output_root: Path, spec_path: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    includes = spec.get("include", [])
    excludes = list(spec.get("exclude", []))
    if not isinstance(includes, list) or not includes:
        raise ValueError("staging spec requires a non-empty include list")
    if output_root == source_root or output_root in source_root.parents:
        raise ValueError("staging output must not be the source tree or one of its parents")
    source_identity = _git_source_identity(source_root)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    copied: dict[str, str] = {}
    skipped: list[dict[str, Any]] = []
    source_findings: list[Finding] = []
    fatal_findings: list[Finding] = []
    processed: set[str] = set()
    max_file_bytes = int(spec.get("max_file_bytes", 5 * 1024 * 1024))
    for entry in includes:
        source = _safe_source_file(source_root, str(entry))
        if not source.exists():
            raise FileNotFoundError(f"staging include does not exist: {entry}")
        explicit_file = source.is_file() or source.is_symlink()
        candidates = [source] if explicit_file else [item for item in source.rglob("*") if item.is_file() or item.is_symlink()]
        for path in candidates:
            rel = path.relative_to(source_root).as_posix()
            if rel in processed:
                continue
            processed.add(rel)
            if _excluded(rel, excludes):
                skipped.append({"path": rel, "reason": "EXCLUDED_BY_SPEC", "findings": []})
                continue
            path_findings = _file_findings(path, source_root, max_file_bytes)
            if path_findings:
                rendered_findings = [asdict(item) for item in path_findings]
                skipped.append(
                    {"path": rel, "reason": "SAFETY_FINDING", "findings": rendered_findings}
                )
                source_findings.extend(path_findings)
                if explicit_file:
                    fatal_findings.extend(path_findings)
                continue
            destination = output_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination, follow_symlinks=False)
            copied[rel] = hashlib.sha256(destination.read_bytes()).hexdigest()
    notice = output_root / "STAGING_NOT_FINAL.md"
    notice.write_text(
        "# NOT FINAL SUBMISSION PACKAGE\n\n"
        "This directory is an internal, reviewable staging candidate. Official submission rules must be confirmed before packaging.\n",
        encoding="utf-8",
    )
    copied[notice.name] = hashlib.sha256(notice.read_bytes()).hexdigest()
    findings = scan_tree(output_root, max_file_bytes=max_file_bytes)
    qa_findings = sorted(
        set([*fatal_findings, *findings]),
        key=lambda item: (item.path, item.code, item.detail),
    )
    manifest = {
        "schema_version": "v3d.submission-staging.v1",
        "status": "NOT_FINAL",
        "qa_status": "PASS" if not qa_findings else "FAIL",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        **source_identity,
        "file_count": len(copied),
        "files": copied,
        "skipped_file_count": len(skipped),
        "skipped": skipped,
        "source_findings": [
            asdict(item)
            for item in sorted(
                set(source_findings),
                key=lambda item: (item.path, item.code, item.detail),
            )
        ],
        "qa_findings": [asdict(item) for item in qa_findings],
    }
    manifest_path = output_root / "staging_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if qa_findings:
        raise ValueError(f"staging QA failed with {len(qa_findings)} finding(s); see {manifest_path}")
    return manifest
