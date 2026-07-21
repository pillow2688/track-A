"""OpenAI-compatible provider adapter for the durable V3-B Planner boundary.

The adapter is deliberately narrow.  It resolves only hash-bound run artifacts,
uses the deterministic V2 selector to choose one optimization class, and then
delegates one proposal to the existing OpenAI-compatible optimization provider.
It never gives the provider tool access and never persists credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .optimization import (
    ALLOWED_OPTIMIZATIONS,
    OptimizationContext,
    optimization_hls_rules,
    select_optimization,
)
from .repair import PatchProposal
from .scoring import estimate_official_score_proxy
from .v3_planner import canonical_json, canonical_sha256, validate_planner_input
from .v3_planner_action import PreparedPlannerCall
from .v3_evidence import SYNTH_EVIDENCE_SCHEMA
from .v3_experience import ExperienceMode, validate_guidance


OPENAI_V3_ADAPTER_REQUEST_SCHEMA = "v3b.openai-v2-adapter-request.v1"
OPENAI_V3_FAST_REQUEST_SCHEMA = "v3b.openai-fast-experiment-request.v1"
OPENAI_V3_TASK_AWARE_REQUEST_SCHEMA = "v3c.openai-task-aware-request.v1"
OPENAI_V3_ADAPTER_VERSION = "openai-v2-compat-planner-adapter-v1"
TASK_AWARE_MODES = frozenset({"REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX"})
_FORBIDDEN_PLANNER_PATH_COMPONENTS = frozenset(
    {"answer", "golden", "hidden", "hidden_like", "reference"}
)


class V3OpenAIPlannerError(RuntimeError):
    """The V3 input cannot be projected safely into the V2 provider contract."""


def _guidance_actionable(value: Mapping[str, object] | None) -> bool:
    if value is None:
        return False
    return any(
        isinstance(value.get(name), list) and bool(value.get(name))
        for name in (
            "similar_successes",
            "similar_failures",
            "recommended_strategy_bundles",
            "discouraged_strategy_bundles",
        )
    )


class AuditableOptimizationProvider(Protocol):
    """Existing provider surface required by the compatibility adapter."""

    def fingerprint(self) -> str: ...

    def describe_optimization_request(
        self, context: OptimizationContext
    ) -> Mapping[str, object]: ...

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal: ...

    def describe_guided_optimization_request(
        self,
        context: OptimizationContext,
        experience_guidance: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def propose_guided_optimization(
        self,
        context: OptimizationContext,
        experience_guidance: Mapping[str, object],
    ) -> PatchProposal: ...

    def describe_fast_experiment_request(
        self, context: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def propose_fast_experiment(
        self, context: Mapping[str, object]
    ) -> PatchProposal: ...

    def describe_task_aware_request(
        self, context: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def propose_task_aware(
        self, context: Mapping[str, object]
    ) -> PatchProposal: ...


@dataclass(frozen=True)
class _BoundArtifact:
    reference: str
    digest: str
    data: bytes


@dataclass(frozen=True)
class _GuidedOptimizationDispatch:
    """Bind legacy OptimizationContext to bounded V3-E guidance."""

    context: OptimizationContext
    guidance: Mapping[str, object]

    def binding(self) -> dict[str, object]:
        return {
            "optimization_context": self.context.to_dict(),
            "experience_guidance": dict(self.guidance),
        }


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise V3OpenAIPlannerError(f"Planner input {name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise V3OpenAIPlannerError(f"Planner input {name} must be non-empty text")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise V3OpenAIPlannerError(f"Planner input {name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise V3OpenAIPlannerError(
            f"Planner input {name} must be a non-negative integer"
        )
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise V3OpenAIPlannerError(f"Planner input {name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise V3OpenAIPlannerError(f"Planner input {name} must be positive")
    return number


def _json_copy(value: object) -> object:
    return json.loads(canonical_json(value).decode("utf-8"))


def _public_planner_path(value: object, name: str) -> str:
    """Validate a public-only path before it can influence a Planner request."""

    path_text = _text(value, name)
    relative = Path(path_text)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or any(
            part.casefold() in _FORBIDDEN_PLANNER_PATH_COMPONENTS
            for part in relative.parts
        )
    ):
        raise V3OpenAIPlannerError(
            f"Planner input {name} enters a forbidden path"
        )
    return path_text


def _resolve_binding(
    run_root: Path,
    value: object,
    *,
    name: str,
    optional: bool = False,
) -> _BoundArtifact | None:
    binding = _mapping(value, name)
    if set(binding) != {"ref", "sha256"}:
        raise V3OpenAIPlannerError(f"Planner input {name} binding is invalid")
    reference = binding.get("ref")
    digest = binding.get("sha256")
    if optional and reference is None and digest is None:
        return None
    reference = _text(reference, f"{name}.ref")
    digest = _text(digest, f"{name}.sha256")
    relative = Path(reference)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or any(
            part.casefold() in _FORBIDDEN_PLANNER_PATH_COMPONENTS
            for part in relative.parts
        )
    ):
        raise V3OpenAIPlannerError(
            f"Planner input {name} escapes the run or enters a forbidden path"
        )
    cursor = run_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise V3OpenAIPlannerError(
                f"Planner input {name} uses a symbolic link"
            )
    path = (run_root / relative).resolve()
    try:
        path.relative_to(run_root)
    except ValueError as exc:
        raise V3OpenAIPlannerError(
            f"Planner input {name} escapes the run"
        ) from exc
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise V3OpenAIPlannerError(
            f"Planner input {name} artifact cannot be read"
        ) from exc
    if not path.is_file() or hashlib.sha256(data).hexdigest() != digest:
        raise V3OpenAIPlannerError(
            f"Planner input {name} artifact is missing or has the wrong hash"
        )
    return _BoundArtifact(reference=reference, digest=digest, data=data)


def _read_json(data: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V3OpenAIPlannerError(
            f"Planner input {name} artifact is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise V3OpenAIPlannerError(f"Planner input {name} artifact is not an object")
    return value


def _source_and_report(
    run_root: Path,
    candidate: Mapping[str, object],
    *,
    name: str,
) -> tuple[str, dict[str, object], dict[str, object]]:
    candidate_id = _text(candidate.get("candidate_id"), f"{name}.candidate_id")
    source_artifact = _resolve_binding(
        run_root, candidate.get("source"), name=f"{name}.source"
    )
    assert source_artifact is not None
    source_digest = source_artifact.digest
    if candidate.get("code_hash") != source_digest:
        raise V3OpenAIPlannerError(f"Planner input {name} code hash is invalid")
    try:
        source = source_artifact.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V3OpenAIPlannerError(
            f"Planner input {name} source is not UTF-8"
        ) from exc

    metrics_binding = _mapping(candidate.get("metrics"), f"{name}.metrics")
    metrics_ref = _text(metrics_binding.get("ref"), f"{name}.metrics.ref")
    metrics_artifact = _resolve_binding(
        run_root, metrics_binding, name=f"{name}.metrics"
    )
    assert metrics_artifact is not None
    tool_result = _read_json(metrics_artifact.data, f"{name}.metrics")
    report = tool_result.get("report")
    if (
        tool_result.get("kind") != "synth"
        or tool_result.get("candidate_id") != candidate_id
        or tool_result.get("code_hash") != source_digest
        or tool_result.get("result_ref") != metrics_ref
        or tool_result.get("validation_scope", "exploration") != "exploration"
        or tool_result.get("ok") is not True
        or not isinstance(report, Mapping)
    ):
        raise V3OpenAIPlannerError(
            f"Planner input {name} metrics are not a successful bound Synth result"
        )
    report_copy = _json_copy(report)
    if not isinstance(report_copy, dict):
        raise V3OpenAIPlannerError(f"Planner input {name} Synth report is invalid")

    evidence: dict[str, object] = {}
    evidence_artifact = _resolve_binding(
        run_root,
        candidate.get("synth_evidence"),
        name=f"{name}.synth_evidence",
        optional=True,
    )
    if evidence_artifact is not None:
        evidence = _read_json(
            evidence_artifact.data, f"{name}.synth_evidence"
        )
        if (
            evidence.get("schema_version") != SYNTH_EVIDENCE_SCHEMA
            or evidence.get("candidate_id") != candidate_id
            or evidence.get("action_id") != tool_result.get("action_id")
            or evidence.get("result_ref") != metrics_ref
        ):
            raise V3OpenAIPlannerError(
                f"Planner input {name} Synth evidence binding is invalid"
            )
    return source, report_copy, evidence


def _source_only(
    run_root: Path,
    candidate: Mapping[str, object],
    *,
    name: str,
) -> str:
    """Resolve a hash-bound kernel without requiring successful Synth metrics."""

    _text(candidate.get("candidate_id"), f"{name}.candidate_id")
    source_artifact = _resolve_binding(
        run_root, candidate.get("source"), name=f"{name}.source"
    )
    assert source_artifact is not None
    if candidate.get("code_hash") != source_artifact.digest:
        raise V3OpenAIPlannerError(f"Planner input {name} code hash is invalid")
    try:
        return source_artifact.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V3OpenAIPlannerError(
            f"Planner input {name} source is not UTF-8"
        ) from exc


def _task_aware_failure_evidence(
    round_state: Mapping[str, object], *, mode: str
) -> dict[str, object]:
    """Accept only the bounded extractor output expected for the routed mode."""

    raw = _mapping(round_state.get("failure_evidence"), "round.failure_evidence")
    expected_schema = {
        "REPAIR": "v3c.csim-failure-evidence.v1",
        "SYNTH_FIX": "v3c.synth-failure-evidence.v1",
        "STRUCTURAL_FIX": "v3c.cosim-failure-evidence.v1",
    }[mode]
    if raw.get("schema_version") != expected_schema:
        raise V3OpenAIPlannerError(
            "Planner input failure evidence does not match the routed mode"
        )
    evidence = _sanitize_evidence_value(_json_copy(raw))
    if not isinstance(evidence, dict):
        raise V3OpenAIPlannerError("Planner input failure evidence is invalid")
    # The extractor is intentionally small. Refuse accidental complete logs at
    # the final Planner boundary instead of silently sending them to the model.
    if len(canonical_json(evidence)) > 24_000:
        raise V3OpenAIPlannerError("Planner input failure evidence is not bounded")
    return evidence


_POSIX_LOCAL_PATH = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/(?:[A-Za-z0-9_.+-]+/)+[A-Za-z0-9_.+-]+"
)
_WINDOWS_LOCAL_PATH = re.compile(
    r"\b[A-Za-z]:\\(?:[^\\\s]+\\)+[^\\\s]+"
)


def _sanitize_evidence_text(value: object) -> str:
    text = str(value)
    text = re.sub(r"\bbearer\s+\S+", "<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S+",
        "<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = _POSIX_LOCAL_PATH.sub("<local-path>", text)
    return _WINDOWS_LOCAL_PATH.sub("<local-path>", text)


def _sanitize_evidence_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_evidence_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_evidence_value(child) for child in value]
    if isinstance(value, str):
        return _sanitize_evidence_text(value)
    return value


def _metrics_with_evidence(
    report: Mapping[str, object], evidence: Mapping[str, object]
) -> dict[str, object]:
    value = _json_copy(report)
    if not isinstance(value, dict):
        raise V3OpenAIPlannerError("Synth metrics must remain an object")
    loops = evidence.get("loops")
    report_loop_evidence = value.get("loop_evidence")
    report_loops = (
        report_loop_evidence.get("loops")
        if isinstance(report_loop_evidence, Mapping)
        else None
    )
    selected_loops = (
        loops
        if isinstance(loops, list) and loops
        else (report_loops if isinstance(report_loops, list) else [])
    )
    value["loop_evidence"] = {
        "loops": _sanitize_evidence_value(_json_copy(selected_loops))
    }
    lines: list[str] = []
    observations = evidence.get("observations")
    if isinstance(observations, list):
        for observation in observations[:12]:
            if isinstance(observation, Mapping):
                lines.append(
                    json.dumps(
                        _sanitize_evidence_value(dict(observation)),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:640]
                )
    relevant = evidence.get("relevant_tool_log_lines")
    if isinstance(relevant, list):
        lines.extend(_sanitize_evidence_text(item)[:320] for item in relevant[:12])
    existing = value.get("evidence")
    if isinstance(existing, list):
        lines = [
            _sanitize_evidence_text(item)[:320] for item in existing[:12]
        ] + lines
    value["evidence"] = lines[:24]
    return value


def _history_state(
    history: object,
) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[Mapping[str, object], ...],
]:
    if not isinstance(history, list):
        raise V3OpenAIPlannerError("Planner input history must be a list")
    attempted: list[tuple[str, str]] = []
    failed: list[Mapping[str, object]] = []
    for item in history:
        if not isinstance(item, Mapping):
            raise V3OpenAIPlannerError("Planner input history item is invalid")
        change_class = item.get("change_class")
        if isinstance(change_class, str) and change_class in ALLOWED_OPTIMIZATIONS:
            metrics_digest = item.get("selection_metrics_digest")
            attempt = (
                (change_class, metrics_digest)
                if isinstance(metrics_digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", metrics_digest)
                else None
            )
            if attempt is not None and attempt not in attempted:
                attempted.append(attempt)
            status = str(item.get("status") or "")
            reason = item.get("rejection_reason") or item.get("reason")
            if status == "REJECTED" or reason:
                failed.append(
                    {
                        "kind": item.get("kind"),
                        "candidate_id": item.get("candidate_id"),
                        "round_index": item.get("round_index"),
                        "change_class": change_class,
                        "status": item.get("status"),
                        "reason": reason,
                    }
                )
    return tuple(attempted), tuple(failed)


def _fast_history_state(
    history: object,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(history, list):
        raise V3OpenAIPlannerError("Planner input history must be a list")
    attempts: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for item in history:
        if not isinstance(item, Mapping):
            raise V3OpenAIPlannerError("Planner input history item is invalid")
        strategy = item.get("change_class")
        patch_digest = item.get("patch_sha256")
        if isinstance(strategy, str) and strategy:
            attempt = {
                "round_index": item.get("round_index"),
                "strategy_bundle": strategy.split("+"),
                "patch_sha256": patch_digest if isinstance(patch_digest, str) else None,
                "metrics_digest": item.get("selection_metrics_digest"),
                "status": item.get("status"),
            }
            attempts.append(attempt)
            reason = item.get("rejection_reason") or item.get("reason")
            if reason or item.get("status") == "REJECTED":
                failures.append(attempt | {"reason": reason})
    return attempts[-8:], failures[-3:]


def _official_policy(policy: Mapping[str, object]) -> tuple[bool, float]:
    scoring = _mapping(policy.get("scoring"), "policy.scoring")
    official = _mapping(scoring.get("official_score"), "policy.scoring.official_score")
    enabled = official.get("enabled")
    if not isinstance(enabled, bool):
        raise V3OpenAIPlannerError(
            "Planner input policy.scoring.official_score.enabled must be boolean"
        )
    cap = _positive_number(
        official.get("acceleration_cap"),
        "policy.scoring.official_score.acceleration_cap",
    )
    return enabled, cap


class OpenAICompatibleV3PlannerAdapter:
    """Project one V3 round into strict or autonomous fast Planner prompts."""

    replay_policy = "NON_REPLAYABLE"

    def __init__(
        self,
        run_root: str | Path,
        provider: AuditableOptimizationProvider,
        *,
        final_reserve_credits: int = 25,
        max_output_tokens: int | None = None,
        fast_experiment: bool = False,
        read_only_headers: Mapping[str, str] | None = None,
        experience_mode: str = "off",
        experience_coordinator: object | None = None,
        experience_task_split: str = "unknown",
        experience_algorithm_family: str | None = None,
    ) -> None:
        self.run_root = Path(run_root).resolve()
        self.provider = provider
        if final_reserve_credits < 0:
            raise ValueError("final_reserve_credits must be non-negative")
        self.final_reserve_credits = int(final_reserve_credits)
        self.fast_experiment = bool(fast_experiment)
        self.read_only_headers = {}
        for name, content in (read_only_headers or {}).items():
            public_name = _public_planner_path(name, "read_only_headers")
            self.read_only_headers[public_name] = str(content)
        inferred = getattr(getattr(provider, "config", None), "max_output_tokens", None)
        selected_max = inferred if max_output_tokens is None else max_output_tokens
        if (
            isinstance(selected_max, bool)
            or not isinstance(selected_max, int)
            or selected_max <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        self.max_output_tokens = selected_max
        provider_fingerprint = provider.fingerprint()
        if not isinstance(provider_fingerprint, str) or not provider_fingerprint:
            raise ValueError("provider fingerprint must not be empty")
        configured_secret = getattr(
            getattr(provider, "config", None), "api_key", None
        )
        if (
            isinstance(configured_secret, str)
            and configured_secret
            and configured_secret in provider_fingerprint
        ):
            raise ValueError("provider fingerprint must not contain its API key")
        self._provider_fingerprint = provider_fingerprint
        self._configured_secret = (
            configured_secret
            if isinstance(configured_secret, str) and configured_secret
            else None
        )
        self.experience_mode = ExperienceMode.parse(experience_mode)
        self._experience_error_type: str | None = None
        self._experience_disabled = False
        if self.experience_mode is not ExperienceMode.OFF:
            if experience_coordinator is None:
                if self.experience_mode is ExperienceMode.GUIDED:
                    raise ValueError(
                        "guided experience mode requires an ExperienceCoordinator"
                    )
                self._experience_disabled = True
                self._experience_error_type = "ExperienceCoordinatorUnavailable"
            for method_name in (
                "build_guidance",
                "persist_recommendation",
                "snapshot_metadata",
                "fingerprint",
            ):
                if experience_coordinator is not None and not callable(
                    getattr(experience_coordinator, method_name, None)
                ):
                    if self.experience_mode is ExperienceMode.GUIDED:
                        raise ValueError(
                            f"experience coordinator is missing {method_name}()"
                        )
                    self._experience_disabled = True
                    self._experience_error_type = "ExperienceCoordinatorContractError"
        self._experience_coordinator = experience_coordinator
        self._experience_task_split = str(experience_task_split).strip().casefold()
        if self._experience_task_split not in {
            "train",
            "dev",
            "validation",
            "hidden_like",
            "test",
            "holdout",
            "unknown",
            "unspecified",
        }:
            raise ValueError("unsupported experience task split")
        self._experience_algorithm_family = (
            str(experience_algorithm_family).strip()
            if experience_algorithm_family
            else None
        )
        self._experience_run_id = canonical_sha256(
            {
                "adapter": OPENAI_V3_ADAPTER_VERSION,
                "provider": self._provider_fingerprint,
                "run_label": self.run_root.name,
            }
        )
        self._experience_snapshot = None
        self._experience_fingerprint = None
        if (
            self.experience_mode is not ExperienceMode.OFF
            and not self._experience_disabled
            and experience_coordinator is not None
        ):
            try:
                self._experience_snapshot = dict(
                    experience_coordinator.snapshot_metadata()
                )
                self._experience_fingerprint = dict(
                    experience_coordinator.fingerprint()
                )
            except Exception as exc:
                if self.experience_mode is ExperienceMode.GUIDED:
                    raise
                self._experience_disabled = True
                self._experience_error_type = type(exc).__name__

    def fingerprint(self) -> str:
        identity = {
            "adapter": OPENAI_V3_ADAPTER_VERSION,
            "request_schema": OPENAI_V3_ADAPTER_REQUEST_SCHEMA,
            "provider_fingerprint": self._provider_fingerprint,
            "final_reserve_credits": self.final_reserve_credits,
            "max_output_tokens": self.max_output_tokens,
            "selector": (
                "autonomous-strategy-bundle-v1"
                if self.fast_experiment
                else "v2-deterministic-selection-v1"
            ),
            "fast_experiment": self.fast_experiment,
            "read_only_headers_sha256": canonical_sha256(self.read_only_headers),
        }
        # `off` deliberately hashes the exact pre-V3-E identity.  This is the
        # compatibility contract used to resume old V3-D checkpoints.
        if self.experience_mode is not ExperienceMode.OFF:
            identity["experience"] = {
                "mode": self.experience_mode.value,
                "task_split": self._experience_task_split,
                "algorithm_family": self._experience_algorithm_family,
                "seed": self._experience_fingerprint,
            }
        return f"{OPENAI_V3_ADAPTER_VERSION}:{canonical_sha256(identity)}"

    def experience_summary(self) -> dict[str, object] | None:
        """Return path-free audit metadata for terminal reports."""

        if self.experience_mode is ExperienceMode.OFF:
            return None
        summary: dict[str, object] = {
            "schema_version": "v3e.planner-experience-binding.v1",
            "mode": self.experience_mode.value,
            "task_split": self._experience_task_split,
            "seed_snapshot": self._experience_snapshot,
            "seed_fingerprint": self._experience_fingerprint,
            "authority": "ADVISORY_ONLY",
            "status": "DISABLED" if self._experience_disabled else "ACTIVE",
        }
        if self._experience_error_type is not None:
            summary["error_type"] = self._experience_error_type
        recommendation_path = getattr(
            self._experience_coordinator, "recommendation_path", None
        )
        if recommendation_path is not None:
            try:
                path = Path(recommendation_path).resolve()
                relative = path.relative_to(self.run_root).as_posix()
                if relative is not None and path.is_file():
                    summary["recommendations_ref"] = relative
                    summary["recommendations_sha256"] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
            except (OSError, ValueError) as exc:
                relative = None
                summary["recommendation_audit_status"] = "UNAVAILABLE"
                summary["recommendation_audit_error_type"] = type(exc).__name__
        return summary

    def _build_experience_guidance(
        self,
        planner_input: Mapping[str, object],
        *,
        mode: str,
        source: str,
        failure_evidence: object = None,
        synth_report: object = None,
        synth_evidence: object = None,
    ) -> dict[str, object] | None:
        if self.experience_mode is ExperienceMode.OFF:
            return None
        if self._experience_disabled:
            return None
        coordinator = self._experience_coordinator
        assert coordinator is not None
        task = _mapping(planner_input.get("task"), "task")
        round_state = _mapping(planner_input.get("round"), "round")
        budget = _mapping(planner_input.get("budget"), "budget")
        history = planner_input.get("history")
        if not isinstance(history, list):
            raise V3OpenAIPlannerError("Planner input history must be a list")
        attempted_bundles: list[list[str]] = []
        attempted_hashes: list[str] = []
        for raw in history[-8:]:
            if not isinstance(raw, Mapping):
                continue
            change_class = raw.get("change_class")
            if isinstance(change_class, str) and change_class:
                bundle = [item for item in change_class.split("+") if item]
                if bundle and bundle not in attempted_bundles:
                    attempted_bundles.append(bundle[:3])
            digest = raw.get("patch_sha256")
            if (
                isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest)
                and digest not in attempted_hashes
            ):
                attempted_hashes.append(digest)
        parent_candidate_id = round_state.get("parent_candidate_id")
        try:
            guidance = coordinator.build_guidance(
                mode=mode,
                source=source,
                failure_evidence=failure_evidence,
                synth_report=synth_report,
                synth_evidence=synth_evidence,
                task_split=self._experience_task_split,
                algorithm_family=self._experience_algorithm_family,
                current_run_id=self._experience_run_id,
                current_candidate_id=(
                    str(parent_candidate_id) if parent_candidate_id else None
                ),
                description=str(task.get("description") or ""),
                task_id=str(task.get("task_id") or "unknown-task"),
                difficulty=int(task.get("difficulty") or 1),
                attempted_strategy_bundles=attempted_bundles,
                attempted_patch_hashes=attempted_hashes,
                remaining_credits=(
                    int(budget["credits_remaining"])
                    if isinstance(budget.get("credits_remaining"), int)
                    and not isinstance(budget.get("credits_remaining"), bool)
                    else None
                ),
                remaining_tokens=int(budget.get("tokens_remaining") or 0),
                no_improvement_rounds=int(
                    round_state.get("no_improvement_rounds") or 0
                ),
            )
            if not isinstance(guidance, Mapping):
                raise V3OpenAIPlannerError("experience guidance must be an object")
            safe_guidance = validate_guidance(guidance)
            round_index = round_state.get("round_index")
            if isinstance(round_index, bool) or not isinstance(round_index, int):
                raise V3OpenAIPlannerError(
                    "round index is invalid for experience audit"
                )
            coordinator.persist_recommendation(round_index, safe_guidance)
            return safe_guidance
        except Exception as exc:
            if self.experience_mode is ExperienceMode.GUIDED:
                raise
            # Shadow advice is observational.  Any schema/repository/I/O error
            # disables it for the rest of this run without changing the old
            # Planner request or graph route.
            self._experience_disabled = True
            self._experience_error_type = type(exc).__name__
            return None

    def prepare(
        self, planner_input: Mapping[str, object]
    ) -> PreparedPlannerCall:
        value = validate_planner_input(planner_input)
        task = _mapping(value.get("task"), "task")
        for path_field in ("kernel_file", "public_tb"):
            path_value = task.get(path_field)
            if path_value is not None:
                _public_planner_path(path_value, f"task.{path_field}")
        round_state = _mapping(value.get("round"), "round")
        incumbent = _mapping(value.get("incumbent"), "incumbent")
        baseline = _mapping(value.get("baseline"), "baseline")
        policy = _mapping(value.get("policy"), "policy")
        budget = _mapping(value.get("budget"), "budget")

        mode_value = round_state.get("mode", "OPTIMIZE")
        # V3-A1 inputs and in-flight checkpoints predate PhaseRouter. Preserve
        # their exact optimization path while new routed runs use explicit modes.
        if mode_value == "UNROUTED":
            mode_value = "OPTIMIZE"
        if not isinstance(mode_value, str) or mode_value not in (
            TASK_AWARE_MODES | {"OPTIMIZE"}
        ):
            raise V3OpenAIPlannerError("Planner input round.mode is unsupported")
        if mode_value in TASK_AWARE_MODES:
            source = _source_only(self.run_root, incumbent, name="incumbent")
            failure_evidence = _task_aware_failure_evidence(
                round_state, mode=mode_value
            )
            requires_cosim = task.get("requires_cosim")
            if not isinstance(requires_cosim, bool):
                raise V3OpenAIPlannerError(
                    "Planner input task.requires_cosim must be boolean"
                )
            read_only_files = [
                name
                for name in (
                    *sorted(self.read_only_headers),
                    task.get("public_tb"),
                    "task.toml",
                    "description.md",
                )
                if isinstance(name, str) and name
            ]
            context: dict[str, object] = {
                "mode": mode_value,
                "task": {
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                    "top": task.get("top"),
                    "kernel_file": task.get("kernel_file"),
                    "initial_condition": task.get("initial_condition"),
                    "requires_cosim": requires_cosim,
                    "part": task.get("part"),
                    "clock_ns": task.get("clock_ns"),
                },
                "current_kernel": source,
                "description": str(task.get("description") or ""),
                "read_only_headers": dict(self.read_only_headers),
                "failure_evidence": failure_evidence,
                "budget": {
                    "remaining_tokens": budget.get("tokens_remaining"),
                    "remaining_credits": budget.get("credits_remaining"),
                    "round_index": round_state.get("round_index"),
                    "rounds_completed": round_state.get("rounds_completed"),
                    "final_reserve_credits": self.final_reserve_credits,
                },
                "constraints": {
                    "allowed_files": [task.get("kernel_file")],
                    "read_only_files": read_only_files,
                    "preserve_top": task.get("top"),
                    "preserve_interface": True,
                    "planner_cannot_choose_tools_or_final": True,
                },
            }
            guidance = self._build_experience_guidance(
                value,
                mode=mode_value,
                source=source,
                failure_evidence=failure_evidence,
            )
            if (
                _guidance_actionable(guidance)
                and self.experience_mode is ExperienceMode.GUIDED
            ):
                context["experience_guidance"] = guidance
            provider_request = self.provider.describe_task_aware_request(context)
            if not isinstance(provider_request, Mapping):
                raise V3OpenAIPlannerError(
                    "task-aware provider request audit must be an object"
                )
            encoded_provider_request = canonical_json(provider_request)
            if (
                self._configured_secret is not None
                and self._configured_secret.encode("utf-8")
                in encoded_provider_request
            ):
                raise V3OpenAIPlannerError(
                    "task-aware provider request audit contains its API key"
                )
            request = {
                "schema_version": OPENAI_V3_TASK_AWARE_REQUEST_SCHEMA,
                "adapter_version": OPENAI_V3_ADAPTER_VERSION,
                "provider_fingerprint": self._provider_fingerprint,
                "selection": {
                    "mode": mode_value,
                    "failure_evidence_schema": failure_evidence.get(
                        "schema_version"
                    ),
                },
                "context_sha256": canonical_sha256(context),
                "provider_request": dict(provider_request),
            }
            return PreparedPlannerCall(
                request=request,
                estimated_input_tokens=max(1, len(encoded_provider_request)),
                max_output_tokens=self.max_output_tokens,
                dispatch_context=context,
            )

        source, incumbent_report, incumbent_evidence = _source_and_report(
            self.run_root, incumbent, name="incumbent"
        )
        _baseline_source, baseline_report, _baseline_evidence = _source_and_report(
            self.run_root, baseline, name="baseline"
        )
        current_metrics = _metrics_with_evidence(
            incumbent_report, incumbent_evidence
        )
        if self.fast_experiment:
            attempts, recent_failures = _fast_history_state(value.get("history"))
            context: dict[str, object] = {
                "objective": (
                    "Minimize worst-case synthesis latency under the configured "
                    "Token, Credit, time, interface and correctness constraints."
                ),
                "task": {
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                    "top": task.get("top"),
                    "kernel_file": task.get("kernel_file"),
                    "difficulty": task.get("difficulty"),
                    "initial_condition": task.get("initial_condition"),
                    "requires_cosim": task.get("requires_cosim"),
                    "part": task.get("part"),
                    "clock_ns": task.get("clock_ns"),
                },
                "current_kernel": source,
                "description": str(task.get("description") or ""),
                "read_only_headers": dict(self.read_only_headers),
                "synth_evidence": {
                    "top_latency": incumbent_report.get("latency"),
                    "top_transaction_interval": incumbent_report.get("interval"),
                    "estimated_clock_period_ns": incumbent_report.get(
                        "estimated_clock_period_ns"
                    ),
                    "loops": current_metrics.get("loop_evidence"),
                    "scheduling_or_memory_evidence": current_metrics.get("evidence"),
                    "resources": incumbent_report.get("resources"),
                    "available_resources": incumbent_report.get(
                        "available_resources"
                    ),
                },
                "recent_failures": recent_failures,
                "attempted_strategies": attempts,
                "budget": {
                    "remaining_tokens": budget.get("tokens_remaining"),
                    "remaining_credits": budget.get("credits_remaining"),
                    "round_index": round_state.get("round_index"),
                    "rounds_completed": round_state.get("rounds_completed"),
                    "max_optimization_rounds": policy.get(
                        "max_optimization_rounds"
                    ),
                    "max_no_improvement_rounds": policy.get(
                        "max_no_improvement_rounds"
                    ),
                    "final_reserve_credits": self.final_reserve_credits,
                },
                "constraints": {
                    "allowed_files": [task.get("kernel_file")],
                    "read_only_files": [
                        *sorted(self.read_only_headers),
                        task.get("public_tb"),
                        "task.toml",
                        "description.md",
                    ],
                    "preserve_top": task.get("top"),
                    "preserve_interface": True,
                    "preserve_numerical_semantics": True,
                    "planner_cannot_choose_tools_or_final": True,
                },
            }
            guidance = self._build_experience_guidance(
                value,
                mode="OPTIMIZE",
                source=source,
                synth_report=incumbent_report,
                synth_evidence=incumbent_evidence,
            )
            if (
                _guidance_actionable(guidance)
                and self.experience_mode is ExperienceMode.GUIDED
            ):
                context["experience_guidance"] = guidance
            provider_request = self.provider.describe_fast_experiment_request(context)
            if not isinstance(provider_request, Mapping):
                raise V3OpenAIPlannerError(
                    "fast optimization provider request audit must be an object"
                )
            encoded_provider_request = canonical_json(provider_request)
            if (
                self._configured_secret is not None
                and self._configured_secret.encode("utf-8") in encoded_provider_request
            ):
                raise V3OpenAIPlannerError(
                    "fast optimization provider request audit contains its API key"
                )
            metrics_digest = canonical_sha256(current_metrics)
            request = {
                "schema_version": OPENAI_V3_FAST_REQUEST_SCHEMA,
                "adapter_version": OPENAI_V3_ADAPTER_VERSION,
                "provider_fingerprint": self._provider_fingerprint,
                "selection": {
                    "mode": "autonomous_strategy_bundle",
                    "metrics_digest": metrics_digest,
                    "attempted": attempts,
                },
                "context_sha256": canonical_sha256(context),
                "provider_request": dict(provider_request),
            }
            return PreparedPlannerCall(
                request=request,
                estimated_input_tokens=max(1, len(encoded_provider_request)),
                max_output_tokens=self.max_output_tokens,
                dispatch_context=context,
            )
        attempted, failed_actions = _history_state(value.get("history"))
        decision = select_optimization(
            current_metrics,
            source=source,
            attempted=attempted,
            failures=(),
        )
        if decision.optimization_class is None:
            raise V3OpenAIPlannerError(
                decision.stop_reason or "no distinct optimization remains"
            )

        minimum_frequency = _positive_number(
            policy.get("minimum_frequency_mhz"), "policy.minimum_frequency_mhz"
        )
        estimated_period = incumbent_report.get("estimated_clock_period_ns")
        estimated_valid = (
            not isinstance(estimated_period, bool)
            and isinstance(estimated_period, (int, float))
            and math.isfinite(float(estimated_period))
            and float(estimated_period) > 0
        )
        maximum_period = 1000.0 / minimum_frequency
        current_clock = {
            "minimum_frequency_mhz": minimum_frequency,
            "maximum_period_ns": maximum_period,
            "estimated_period_ns": estimated_period,
            "passed": bool(
                estimated_valid and float(estimated_period) <= maximum_period
            ),
        }
        official_enabled, official_cap = _official_policy(policy)
        validation = _mapping(incumbent.get("validation"), "incumbent.validation")
        requires_cosim = task.get("requires_cosim")
        if not isinstance(requires_cosim, bool):
            raise V3OpenAIPlannerError(
                "Planner input task.requires_cosim must be boolean"
            )
        difficulty = _positive_int(task.get("difficulty"), "task.difficulty")
        current_official_score: float | None = None
        if official_enabled:
            current_official_score = estimate_official_score_proxy(
                difficulty=difficulty,
                baseline=baseline_report,
                candidate=incumbent_report,
                validation=validation,
                requires_cosim=requires_cosim,
            )
        credits_remaining = budget.get("credits_remaining")
        if credits_remaining is not None:
            credits_remaining = _non_negative_int(
                credits_remaining, "budget.credits_remaining"
            )
        context = OptimizationContext(
            task_id=_text(task.get("task_id"), "task.task_id"),
            parent_candidate_id=_text(
                round_state.get("parent_candidate_id"),
                "round.parent_candidate_id",
            ),
            round_index=_positive_int(
                round_state.get("round_index"), "round.round_index"
            ),
            allowed_optimization_class=decision.optimization_class,
            bottleneck=decision.bottleneck,
            evidence=decision.evidence,
            baseline_metrics=baseline_report,
            current_metrics=current_metrics,
            current_validation=validation,
            current_clock_constraint=current_clock,
            source_excerpt=source,
            hls_rules=optimization_hls_rules(decision.optimization_class),
            failed_actions=failed_actions,
            remaining_tokens=_non_negative_int(
                budget.get("tokens_remaining"), "budget.tokens_remaining"
            ),
            remaining_credits=credits_remaining,
            final_reserve_credits=self.final_reserve_credits,
            top=_text(task.get("top"), "task.top"),
            kernel_name=_text(task.get("kernel_file"), "task.kernel_file"),
            part=_text(task.get("part"), "task.part"),
            clock_ns=_positive_number(task.get("clock_ns"), "task.clock_ns"),
            difficulty=difficulty,
            official_score_enabled=official_enabled,
            current_official_score=current_official_score,
            official_acceleration_cap=official_cap,
        )
        guidance = self._build_experience_guidance(
            value,
            mode="OPTIMIZE",
            source=source,
            synth_report=incumbent_report,
            synth_evidence=incumbent_evidence,
        )
        guided_dispatch: _GuidedOptimizationDispatch | None = None
        if _guidance_actionable(guidance) and self.experience_mode is ExperienceMode.GUIDED:
            guided_dispatch = _GuidedOptimizationDispatch(context, guidance)
            provider_request = self.provider.describe_guided_optimization_request(
                context, guidance
            )
        else:
            provider_request = self.provider.describe_optimization_request(context)
        if not isinstance(provider_request, Mapping):
            raise V3OpenAIPlannerError(
                "optimization provider request audit must be an object"
            )
        encoded_provider_request = canonical_json(provider_request)
        if (
            self._configured_secret is not None
            and self._configured_secret.encode("utf-8") in encoded_provider_request
        ):
            raise V3OpenAIPlannerError(
                "optimization provider request audit contains its API key"
            )
        request = {
            "schema_version": OPENAI_V3_ADAPTER_REQUEST_SCHEMA,
            "adapter_version": OPENAI_V3_ADAPTER_VERSION,
            "provider_fingerprint": self._provider_fingerprint,
            "selection": {
                "optimization_class": decision.optimization_class,
                "bottleneck": decision.bottleneck,
                "evidence": list(decision.evidence),
                "metrics_digest": decision.metrics_digest,
                "attempted": [
                    {
                        "optimization_class": optimization_class,
                        "metrics_digest": metrics_digest,
                    }
                    for optimization_class, metrics_digest in attempted
                ],
            },
            "context_sha256": canonical_sha256(context.to_dict()),
            "provider_request": dict(provider_request),
        }
        if guided_dispatch is not None:
            request["context_sha256"] = canonical_sha256(
                guided_dispatch.binding()
            )
        # A byte-for-token reservation is intentionally conservative for
        # OpenAI-compatible tokenizers and prevents actual usage from silently
        # exceeding the durable reservation.
        estimated_input_tokens = max(1, len(encoded_provider_request))
        return PreparedPlannerCall(
            request=request,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=self.max_output_tokens,
            dispatch_context=guided_dispatch or context,
        )

    def invoke(self, prepared: PreparedPlannerCall) -> PatchProposal:
        context = prepared.dispatch_context
        if (
            isinstance(context, Mapping)
            and context.get("mode") in TASK_AWARE_MODES
        ):
            request = prepared.request
            if (
                request.get("schema_version")
                != OPENAI_V3_TASK_AWARE_REQUEST_SCHEMA
                or request.get("provider_fingerprint")
                != self._provider_fingerprint
                or request.get("context_sha256") != canonical_sha256(context)
            ):
                raise V3OpenAIPlannerError(
                    "prepared task-aware Planner request binding is invalid"
                )
            proposal = self.provider.propose_task_aware(context)
            if not isinstance(proposal, PatchProposal):
                raise V3OpenAIPlannerError(
                    "task-aware provider returned an invalid proposal"
                )
            expected_change_class = {
                "REPAIR": "FUNCTIONAL_REPAIR",
                "SYNTH_FIX": "SYNTHESIS_REPAIR",
                "STRUCTURAL_FIX": "STRUCTURAL_REPAIR",
            }[str(context["mode"])]
            if proposal.change_class != expected_change_class:
                raise V3OpenAIPlannerError(
                    "task-aware proposal class diverges from the routed mode"
                )
            return proposal
        if self.fast_experiment:
            if not isinstance(context, Mapping):
                raise V3OpenAIPlannerError(
                    "prepared fast Planner request has no bound context"
                )
            request = prepared.request
            if (
                request.get("schema_version") != OPENAI_V3_FAST_REQUEST_SCHEMA
                or request.get("provider_fingerprint") != self._provider_fingerprint
                or request.get("context_sha256") != canonical_sha256(context)
            ):
                raise V3OpenAIPlannerError(
                    "prepared fast Planner request binding is invalid"
                )
            proposal = self.provider.propose_fast_experiment(context)
            if not isinstance(proposal, PatchProposal):
                raise V3OpenAIPlannerError(
                    "fast optimization provider returned an invalid proposal"
                )
            return proposal
        if isinstance(context, _GuidedOptimizationDispatch):
            request = prepared.request
            if (
                request.get("schema_version") != OPENAI_V3_ADAPTER_REQUEST_SCHEMA
                or request.get("provider_fingerprint")
                != self._provider_fingerprint
                or request.get("context_sha256")
                != canonical_sha256(context.binding())
            ):
                raise V3OpenAIPlannerError(
                    "prepared guided optimization request binding is invalid"
                )
            proposal = self.provider.propose_guided_optimization(
                context.context, context.guidance
            )
            if not isinstance(proposal, PatchProposal):
                raise V3OpenAIPlannerError(
                    "guided optimization provider returned an invalid proposal"
                )
            if (
                proposal.change_class
                != context.context.allowed_optimization_class
            ):
                raise V3OpenAIPlannerError(
                    "guided optimization proposal class diverges from the selected class"
                )
            return proposal
        if not isinstance(context, OptimizationContext):
            raise V3OpenAIPlannerError(
                "prepared Planner request has no bound OptimizationContext"
            )
        request = prepared.request
        if (
            request.get("schema_version") != OPENAI_V3_ADAPTER_REQUEST_SCHEMA
            or request.get("provider_fingerprint") != self._provider_fingerprint
            or request.get("context_sha256") != canonical_sha256(context.to_dict())
        ):
            raise V3OpenAIPlannerError("prepared Planner request binding is invalid")
        proposal = self.provider.propose_optimization(context)
        if not isinstance(proposal, PatchProposal):
            raise V3OpenAIPlannerError(
                "optimization provider returned an invalid proposal"
            )
        if proposal.change_class != context.allowed_optimization_class:
            raise V3OpenAIPlannerError(
                "optimization proposal class diverges from the selected class"
            )
        return proposal
