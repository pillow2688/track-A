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
from typing import Callable, Mapping, Protocol

from .budget import (
    BudgetExceeded,
    TokenBudgetPolicy,
    TokenEnvelope,
    TokenEstimate,
    TokenEstimator,
    validate_token_envelope,
)
from .optimization import (
    ALLOWED_OPTIMIZATIONS,
    OptimizationContext,
    optimization_hls_rules,
    select_optimization,
)
from .openai_provider import FAST_EXPERIMENT_STRATEGIES
from .repair import PatchProposal
from .scoring import estimate_official_score_proxy
from .v3_planner import canonical_json, canonical_sha256, validate_planner_input
from .v3_planner_action import PreparedPlannerCall
from .v3_evidence import SYNTH_EVIDENCE_SCHEMA
from .v3_experience import (
    ExperienceMode,
    estimated_guidance_tokens,
    validate_guidance,
)


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

    def describe_optimization_request_budgeted(
        self,
        context: OptimizationContext,
        token_envelope: Mapping[str, object],
        *,
        experience_guidance: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def propose_optimization_budgeted(
        self,
        context: OptimizationContext,
        token_envelope: Mapping[str, object],
        *,
        experience_guidance: Mapping[str, object] | None = None,
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


@dataclass(frozen=True)
class _BudgetedOptimizationDispatch:
    context: OptimizationContext
    token_envelope: Mapping[str, object]
    guidance: Mapping[str, object] | None = None
    token_budget_visible: bool = True

    def binding(self) -> dict[str, object]:
        return {
            "optimization_context": self.context.to_dict(),
            "experience_guidance": (
                dict(self.guidance) if self.guidance is not None else None
            ),
            "token_envelope": dict(self.token_envelope),
            "token_budget_visible": self.token_budget_visible,
        }


@dataclass(frozen=True)
class _BudgetedPreparedContext:
    context: Mapping[str, object]
    provider_request: Mapping[str, object]
    envelope: TokenEnvelope
    estimate: TokenEstimate
    guidance: Mapping[str, object] | None


@dataclass(frozen=True)
class _BudgetedPreparedOptimization:
    dispatch: _BudgetedOptimizationDispatch
    provider_request: Mapping[str, object]
    envelope: TokenEnvelope
    estimate: TokenEstimate


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
        token_budget_policy: TokenBudgetPolicy | None = None,
        token_estimator: TokenEstimator | None = None,
        token_budget_visible: bool = True,
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
        self.token_budget_policy = token_budget_policy
        self.token_estimator = token_estimator or TokenEstimator()
        self.token_budget_visible = bool(token_budget_visible)
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
        if self.token_budget_policy is not None:
            identity["token_policy"] = {
                "policy_version": (
                    "v3.token-policy.hybrid-v2"
                    if self.token_budget_policy.hybrid_enabled
                    else "v3.token-policy.v1"
                ),
                "limits": {
                    "mode_output_caps": dict(
                        self.token_budget_policy.limits.mode_output_caps
                    ),
                    "mode_minimum_viable_output": dict(
                        self.token_budget_policy.limits.mode_minimum_viable_output
                    ),
                    "configured_max_output_tokens": (
                        self.token_budget_policy.limits.configured_max_output_tokens
                    ),
                    "provider_hard_output_cap": (
                        self.token_budget_policy.limits.provider_hard_output_cap
                    ),
                    "context_window_tokens": (
                        self.token_budget_policy.limits.context_window_tokens
                    ),
                    "future_round_token_reserve": (
                        self.token_budget_policy.limits.future_round_token_reserve
                    ),
                    "final_token_reserve": (
                        self.token_budget_policy.limits.final_token_reserve
                    ),
                },
                "estimator": self.token_estimator.estimator_name,
                "visibility": (
                    "visible" if self.token_budget_visible else "hidden"
                ),
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

    @property
    def dynamic_token_policy_enabled(self) -> bool:
        return self.token_budget_policy is not None

    @staticmethod
    def _rounds_remaining(planner_input: Mapping[str, object]) -> int:
        round_state = _mapping(planner_input.get("round"), "round")
        policy = _mapping(planner_input.get("policy"), "policy")
        maximum = _positive_int(
            policy.get("max_optimization_rounds"),
            "policy.max_optimization_rounds",
        )
        completed = _non_negative_int(
            round_state.get("rounds_completed"), "round.rounds_completed"
        )
        return max(1, maximum - completed)

    def _persist_hybrid_call_gate(
        self, round_index: int, value: Mapping[str, object]
    ) -> dict[str, object]:
        reference = f"planner/call_gates/round_{round_index:03d}.json"
        path = self.run_root / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = canonical_json(value) + b"\n"
        if path.is_file():
            if path.read_bytes() != encoded:
                raise V3OpenAIPlannerError(
                    "Hybrid Planner-call gate artifact changed during replay"
                )
        else:
            path.write_bytes(encoded)
        return {
            "ref": reference,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "decision": value.get("decision"),
            "reason_codes": list(value.get("reason_codes", [])),
        }

    def _hybrid_call_gate(
        self, planner_input: Mapping[str, object], *, mode: str
    ) -> dict[str, object] | None:
        policy = self.token_budget_policy
        if policy is None or not policy.hybrid_enabled:
            return None
        round_state = _mapping(planner_input.get("round"), "round")
        budget = _mapping(planner_input.get("budget"), "budget")
        history = planner_input.get("history")
        if not isinstance(history, list):
            raise V3OpenAIPlannerError("Planner input history must be a list")
        round_index = _positive_int(
            round_state.get("round_index"), "round.round_index"
        )
        estimate = max(1, self.token_estimator.estimate_text(planner_input))
        configured_output = int(policy.limits.mode_output_caps[mode])
        reasons: list[str] = []
        evidence_ids: list[str] = []
        decision = "ALLOW"

        if round_index == 1:
            reasons.append("FIRST_PLANNER_CALL")
        else:
            prior = [
                item
                for item in history
                if isinstance(item, Mapping)
                and int(item.get("round_index") or 0) < round_index
            ]
            latest_round = max(
                (int(item.get("round_index") or 0) for item in prior),
                default=0,
            )
            latest = [
                item
                for item in prior
                if int(item.get("round_index") or 0) == latest_round
            ]
            structured_new = False
            for item in latest:
                for name in ("synth_evidence", "metrics", "score"):
                    binding = item.get(name)
                    if isinstance(binding, Mapping) and isinstance(
                        binding.get("sha256"), str
                    ):
                        structured_new = True
                        evidence_ids.append(str(binding["sha256"]))
            failure_evidence = round_state.get("failure_evidence")
            failure_digest = (
                canonical_sha256(failure_evidence)
                if isinstance(failure_evidence, Mapping) and failure_evidence
                else None
            )
            incumbent = _mapping(planner_input.get("incumbent"), "incumbent")
            baseline = _mapping(planner_input.get("baseline"), "baseline")
            best_changed = (
                incumbent.get("candidate_id") != baseline.get("candidate_id")
            )
            if failure_digest is not None and mode == "OPTIMIZE":
                structured_new = True
                evidence_ids.append(failure_digest)
            if not structured_new:
                reasons.append("NO_NEW_STRUCTURED_EVIDENCE")
            if not best_changed and not structured_new:
                reasons.append("BEST_FAILURE_OR_BOTTLENECK_UNCHANGED")

            attempted = [
                str(item.get("change_class"))
                for item in prior
                if isinstance(item.get("change_class"), str)
                and item.get("change_class")
            ]
            attempted_strategies = {
                strategy
                for bundle in attempted
                for strategy in bundle.split("+")
                if strategy
            }
            if mode == "OPTIMIZE" and not set(
                FAST_EXPERIMENT_STRATEGIES
            ).difference(attempted_strategies):
                reasons.append("NO_UNTRIED_STRATEGY")

            patch_digests = [
                str(item.get("patch_sha256"))
                for item in prior
                if isinstance(item.get("patch_sha256"), str)
                and item.get("patch_sha256")
            ]
            if len(patch_digests) != len(set(patch_digests)):
                reasons.append("DUPLICATE_PATCH_DIGEST")

            failed = [
                (
                    item.get("change_class"),
                    item.get("rejection_reason") or item.get("reason"),
                    item.get("selection_metrics_digest"),
                )
                for item in prior
                if item.get("rejection_reason") or item.get("reason")
            ]
            if len(failed) >= 2 and failed[-1] == failed[-2]:
                reasons.append("EXACT_REPEAT_FAILURE")
            duplicate_bundles = len(attempted) != len(set(attempted))
            if duplicate_bundles:
                reasons.append("DUPLICATE_STRATEGY_BUNDLE")

        credits_remaining = budget.get("credits_remaining")
        if (
            isinstance(credits_remaining, int)
            and not isinstance(credits_remaining, bool)
            and credits_remaining <= self.final_reserve_credits
        ):
            reasons.append("ONLY_FINAL_CREDIT_RESERVE_REMAINS")
        tokens_remaining = budget.get("tokens_remaining")
        minimum = int(policy.limits.mode_minimum_viable_output[mode])
        if (
            not isinstance(tokens_remaining, int)
            or isinstance(tokens_remaining, bool)
            or tokens_remaining
            < estimate
            + minimum
            + policy.limits.final_token_reserve
            + policy.limits.token_budget_safety_margin
        ):
            reasons.append("ESTIMATED_INPUT_EXCEEDS_AVAILABLE_TOKEN_BUDGET")

        blockers = [reason for reason in reasons if reason != "FIRST_PLANNER_CALL"]
        if blockers:
            decision = "BLOCK"
        artifact = {
            "schema_version": "v3e.hybrid-planner-call-gate.v1",
            "mode": mode,
            "round_index": round_index,
            "decision": decision,
            "reason_codes": reasons,
            "estimated_input_tokens": estimate,
            "configured_output_tokens": configured_output,
            "estimated_total_tokens": estimate + configured_output,
            "evidence_digests": sorted(set(evidence_ids)),
            "final_reserve_credits": self.final_reserve_credits,
        }
        binding = self._persist_hybrid_call_gate(round_index, artifact)
        if decision == "BLOCK":
            raise BudgetExceeded(
                "Hybrid Planner-call gate blocked: " + ",".join(blockers)
            )
        return binding

    def _estimate_provider_request(
        self,
        provider_request: Mapping[str, object],
        *,
        context: Mapping[str, object] | OptimizationContext,
        guidance: Mapping[str, object] | None = None,
    ) -> TokenEstimate:
        """Estimate the actual provider messages and expose bounded components."""

        body = provider_request.get("http_body")
        messages = body.get("messages") if isinstance(body, Mapping) else None
        actual_prompt = messages if isinstance(messages, list) else provider_request
        total = max(1, self.token_estimator.estimate_text(actual_prompt))
        if isinstance(context, OptimizationContext):
            raw_components: list[tuple[str, object]] = [
                ("kernel", context.source_excerpt),
                ("headers", {}),
                ("description", ""),
                (
                    "evidence",
                    {
                        "bottleneck": context.bottleneck,
                        "evidence": list(context.evidence),
                        "baseline_metrics": dict(context.baseline_metrics),
                        "current_metrics": dict(context.current_metrics),
                    },
                ),
                ("history", [dict(item) for item in context.failed_actions]),
                ("experience_guidance", guidance or {}),
            ]
        else:
            raw_components = [
                ("kernel", context.get("current_kernel", "")),
                ("headers", context.get("read_only_headers", {})),
                ("description", context.get("description", "")),
                (
                    "evidence",
                    context.get("failure_evidence", context.get("synth_evidence", {})),
                ),
                (
                    "history",
                    {
                        "recent_failures": context.get("recent_failures", []),
                        "attempted_strategies": context.get(
                            "attempted_strategies", []
                        ),
                    },
                ),
                ("experience_guidance", guidance or {}),
                ("token_budget", context.get("token_budget", {})),
            ]
        counts: dict[str, int] = {}
        remaining = total
        for name, value in raw_components:
            count = min(remaining, self.token_estimator.estimate_text(value))
            counts[name] = count
            remaining -= count
        counts["fixed_prompt_template"] = remaining
        return TokenEstimate(
            total_tokens=total,
            component_tokens=counts,
            estimator_name=self.token_estimator.estimator_name,
            estimator_version=self.token_estimator.estimator_version,
            error_sources=self.token_estimator.error_sources,
        )

    def _allocate_token_envelope(
        self,
        planner_input: Mapping[str, object],
        *,
        mode: str,
        base_estimate: TokenEstimate,
        final_estimate: TokenEstimate,
        guidance_tokens: int,
        guidance_allowed: bool,
    ) -> TokenEnvelope:
        if self.token_budget_policy is None:
            raise RuntimeError("dynamic token policy is disabled")
        budget = _mapping(planner_input.get("budget"), "budget")
        envelope = self.token_budget_policy.allocate(
            budget_snapshot=budget,
            mode=mode,
            estimated_base_prompt_tokens=base_estimate.total_tokens,
            estimated_guidance_tokens=guidance_tokens,
            estimated_input_tokens=final_estimate.total_tokens,
            rounds_remaining=self._rounds_remaining(planner_input),
            estimator=final_estimate,
            existing_budget_gate_allowed=True,
            guidance_allowed=guidance_allowed,
        )
        if not envelope.planner_call_allowed:
            reasons = ",".join(envelope.reason_codes) or "TOKEN_POLICY_BLOCKED"
            raise BudgetExceeded(f"Planner call blocked by TokenBudgetPolicy: {reasons}")
        return envelope

    @staticmethod
    def _with_token_budget(
        context: Mapping[str, object], envelope: TokenEnvelope
    ) -> dict[str, object]:
        value = dict(context)
        value["token_budget"] = envelope.to_dict()
        return value

    @staticmethod
    def _with_effective_max_output(
        context: Mapping[str, object], envelope: TokenEnvelope
    ) -> dict[str, object]:
        """Carry the Provider cap without adding TokenEnvelope Prompt text."""

        value = dict(context)
        value["_effective_max_output_tokens"] = (
            envelope.effective_max_output_tokens
        )
        return value

    def _persist_guidance(
        self,
        planner_input: Mapping[str, object],
        guidance: Mapping[str, object] | None,
    ) -> None:
        if guidance is None or self._experience_coordinator is None:
            return
        round_state = _mapping(planner_input.get("round"), "round")
        round_index = _non_negative_int(
            round_state.get("round_index"), "round.round_index"
        )
        self._experience_coordinator.persist_recommendation(round_index, guidance)

    def _build_experience_guidance(
        self,
        planner_input: Mapping[str, object],
        *,
        mode: str,
        source: str,
        failure_evidence: object = None,
        synth_report: object = None,
        synth_evidence: object = None,
        guidance_token_cap: int | None = None,
        persist: bool = True,
    ) -> dict[str, object] | None:
        if self.experience_mode is ExperienceMode.OFF:
            return None
        if self._experience_disabled:
            return None
        if guidance_token_cap is not None and guidance_token_cap <= 0:
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
                prompt_token_limit=guidance_token_cap,
            )
            if not isinstance(guidance, Mapping):
                raise V3OpenAIPlannerError("experience guidance must be an object")
            safe_guidance = validate_guidance(guidance)
            round_index = round_state.get("round_index")
            if isinstance(round_index, bool) or not isinstance(round_index, int):
                raise V3OpenAIPlannerError(
                    "round index is invalid for experience audit"
                )
            if persist:
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

    def _prepare_budgeted_mapping_context(
        self,
        planner_input: Mapping[str, object],
        *,
        mode: str,
        base_context: Mapping[str, object],
        describe: Callable[[Mapping[str, object]], Mapping[str, object]],
        guidance_kwargs: Mapping[str, object],
    ) -> _BudgetedPreparedContext:
        """Run the required base -> guidance -> final two-stage calculation."""

        if self.token_budget_policy is None:
            raise RuntimeError("dynamic token policy is disabled")
        if self.token_budget_policy.hybrid_enabled:
            return self._prepare_hybrid_mapping_context(
                planner_input,
                mode=mode,
                base_context=base_context,
                describe=describe,
            )
        if not self.token_budget_visible:
            return self._prepare_hard_capped_mapping_context(
                planner_input,
                mode=mode,
                base_context=base_context,
                describe=describe,
                guidance_kwargs=guidance_kwargs,
            )
        base_request = describe(base_context)
        if not isinstance(base_request, Mapping):
            raise V3OpenAIPlannerError("provider request audit must be an object")
        base_estimate = self._estimate_provider_request(
            base_request, context=base_context
        )
        provisional = self._allocate_token_envelope(
            planner_input,
            mode=mode,
            base_estimate=base_estimate,
            final_estimate=base_estimate,
            guidance_tokens=0,
            guidance_allowed=(
                self.experience_mode is not ExperienceMode.OFF
                and not self._experience_disabled
            ),
        )
        guidance = self._build_experience_guidance(
            planner_input,
            mode=mode,
            guidance_token_cap=provisional.guidance_token_cap,
            persist=False,
            **dict(guidance_kwargs),
        )
        inject = bool(
            self.experience_mode is ExperienceMode.GUIDED
            and _guidance_actionable(guidance)
        )
        prompt_context = dict(base_context)
        if inject and guidance is not None:
            prompt_context["experience_guidance"] = dict(guidance)
        guidance_tokens = (
            self.token_estimator.estimate_text(guidance) if inject else 0
        )

        envelope = provisional
        final_request: Mapping[str, object] = base_request
        final_estimate = base_estimate
        # TokenEnvelope text itself changes the prompt by a few digits.  Iterate
        # a bounded number of times and always reserve the final observed size.
        for _ in range(4):
            bounded_context = self._with_token_budget(prompt_context, envelope)
            candidate_request = describe(bounded_context)
            candidate_estimate = self._estimate_provider_request(
                candidate_request,
                context=bounded_context,
                guidance=guidance if inject else None,
            )
            candidate_envelope = self._allocate_token_envelope(
                planner_input,
                mode=mode,
                base_estimate=base_estimate,
                final_estimate=candidate_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=(
                    self.experience_mode is not ExperienceMode.OFF
                    and not self._experience_disabled
                ),
            )
            final_request = candidate_request
            final_estimate = candidate_estimate
            stable = (
                candidate_envelope.estimated_input_tokens
                == envelope.estimated_input_tokens
                and candidate_envelope.effective_max_output_tokens
                == envelope.effective_max_output_tokens
                and candidate_envelope.guidance_token_cap
                == envelope.guidance_token_cap
            )
            envelope = candidate_envelope
            if stable:
                break

        # If the final pressure calculation tightened the advice cap, rebuild
        # guidance deterministically before the durable provider request.
        if inject and guidance_tokens > envelope.guidance_token_cap:
            guidance = self._build_experience_guidance(
                planner_input,
                mode=mode,
                guidance_token_cap=envelope.guidance_token_cap,
                persist=False,
                **dict(guidance_kwargs),
            )
            inject = bool(
                self.experience_mode is ExperienceMode.GUIDED
                and _guidance_actionable(guidance)
            )
            prompt_context = dict(base_context)
            if inject and guidance is not None:
                prompt_context["experience_guidance"] = dict(guidance)
            guidance_tokens = (
                self.token_estimator.estimate_text(guidance) if inject else 0
            )
            bounded_context = self._with_token_budget(prompt_context, envelope)
            final_request = describe(bounded_context)
            final_estimate = self._estimate_provider_request(
                final_request,
                context=bounded_context,
                guidance=guidance if inject else None,
            )
            envelope = self._allocate_token_envelope(
                planner_input,
                mode=mode,
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=True,
            )

        # Reach a true fixed point: the Envelope is part of the Prompt, so the
        # request estimated here must be the request governed by that exact
        # Envelope.  Never report a stale estimate after a digit/cap change.
        for _ in range(8):
            final_context = self._with_token_budget(prompt_context, envelope)
            final_request = describe(final_context)
            final_estimate = self._estimate_provider_request(
                final_request,
                context=final_context,
                guidance=guidance if inject else None,
            )
            updated = self._allocate_token_envelope(
                planner_input,
                mode=mode,
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=(
                    self.experience_mode is not ExperienceMode.OFF
                    and not self._experience_disabled
                ),
            )
            if updated.stable_hash == envelope.stable_hash:
                envelope = updated
                break
            envelope = updated
        else:
            raise V3OpenAIPlannerError(
                "TokenEnvelope did not converge with the final provider request"
            )
        body = final_request.get("http_body")
        provider_max = body.get("max_tokens") if isinstance(body, Mapping) else None
        if provider_max != envelope.effective_max_output_tokens:
            raise V3OpenAIPlannerError(
                "Prompt TokenEnvelope and provider max output diverged"
            )
        self._persist_guidance(planner_input, guidance)
        return _BudgetedPreparedContext(
            context=final_context,
            provider_request=final_request,
            envelope=envelope,
            estimate=final_estimate,
            guidance=guidance,
        )

    @staticmethod
    def _compress_hybrid_context(
        context: Mapping[str, object]
    ) -> dict[str, object]:
        """Apply the configured HIGH-pressure compression order.

        Kernel, primary evidence, output schema and interface constraints stay
        untouched.  Only duplicated/old explanatory material is reduced.
        """

        value = _json_copy(context)
        if not isinstance(value, dict):
            raise V3OpenAIPlannerError("Hybrid Planner context is invalid")
        budget = value.get("budget")
        if isinstance(budget, Mapping):
            value["budget"] = {
                name: budget.get(name)
                for name in (
                    "remaining_tokens",
                    "remaining_credits",
                    "round_index",
                    "rounds_completed",
                    "final_reserve_credits",
                )
                if name in budget
            }
        attempts = value.get("attempted_strategies")
        if isinstance(attempts, list):
            value["attempted_strategies"] = attempts[-3:]
        failures = value.get("recent_failures")
        if isinstance(failures, list):
            deduplicated: list[object] = []
            seen: set[str] = set()
            for item in reversed(failures):
                digest = canonical_sha256(item)
                if digest not in seen:
                    deduplicated.append(item)
                    seen.add(digest)
            value["recent_failures"] = list(reversed(deduplicated[:2]))
        synth = value.get("synth_evidence")
        if isinstance(synth, dict):
            scheduling = synth.get("scheduling_or_memory_evidence")
            if isinstance(scheduling, list):
                unique: list[object] = []
                seen_lines: set[str] = set()
                for item in scheduling:
                    rendered = canonical_json(item)
                    if rendered not in seen_lines:
                        unique.append(item)
                        seen_lines.add(rendered)
                synth["scheduling_or_memory_evidence"] = unique[:8]
        failure = value.get("failure_evidence")
        if isinstance(failure, dict):
            for name in ("source_locations", "relevant_tool_log_lines"):
                rows = failure.get(name)
                if isinstance(rows, list):
                    failure[name] = rows[:4]
        description = value.get("description")
        if isinstance(description, str) and len(description) > 1600:
            value["description"] = description[:1600] + "\n[public description compressed]"
        return value

    def _prepare_hybrid_mapping_context(
        self,
        planner_input: Mapping[str, object],
        *,
        mode: str,
        base_context: Mapping[str, object],
        describe: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> _BudgetedPreparedContext:
        if self.token_budget_policy is None:
            raise RuntimeError("Hybrid token policy is disabled")
        base_request = describe(base_context)
        if not isinstance(base_request, Mapping):
            raise V3OpenAIPlannerError("provider request audit must be an object")
        base_estimate = self._estimate_provider_request(
            base_request, context=base_context
        )
        envelope = self._allocate_token_envelope(
            planner_input,
            mode=mode,
            base_estimate=base_estimate,
            final_estimate=base_estimate,
            guidance_tokens=0,
            guidance_allowed=False,
        )
        final_context: dict[str, object] = dict(base_context)
        final_request: Mapping[str, object] = base_request
        final_estimate = base_estimate
        for _ in range(8):
            prompt_context = (
                self._compress_hybrid_context(base_context)
                if envelope.token_pressure == "HIGH"
                else dict(base_context)
            )
            final_context = (
                self._with_effective_max_output(prompt_context, envelope)
                if envelope.token_pressure == "LOW"
                else self._with_token_budget(prompt_context, envelope)
            )
            final_request = describe(final_context)
            final_estimate = self._estimate_provider_request(
                final_request, context=final_context
            )
            updated = self._allocate_token_envelope(
                planner_input,
                mode=mode,
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=0,
                guidance_allowed=False,
            )
            if updated.stable_hash == envelope.stable_hash:
                envelope = updated
                break
            envelope = updated
        else:
            raise V3OpenAIPlannerError("Hybrid TokenEnvelope did not converge")
        body = final_request.get("http_body")
        provider_max = body.get("max_tokens") if isinstance(body, Mapping) else None
        if provider_max != envelope.effective_max_output_tokens:
            raise V3OpenAIPlannerError(
                "Hybrid Provider max output diverged from TokenEnvelope"
            )
        return _BudgetedPreparedContext(
            context=final_context,
            provider_request=final_request,
            envelope=envelope,
            estimate=final_estimate,
            guidance=None,
        )

    def _prepare_hard_capped_mapping_context(
        self,
        planner_input: Mapping[str, object],
        *,
        mode: str,
        base_context: Mapping[str, object],
        describe: Callable[[Mapping[str, object]], Mapping[str, object]],
        guidance_kwargs: Mapping[str, object],
    ) -> _BudgetedPreparedContext:
        """Allocate dynamically while retaining the legacy Planner Prompt."""

        if self.token_budget_policy is None:
            raise RuntimeError("dynamic token policy is disabled")
        base_request = describe(base_context)
        base_estimate = self._estimate_provider_request(
            base_request, context=base_context
        )
        provisional = self._allocate_token_envelope(
            planner_input,
            mode=mode,
            base_estimate=base_estimate,
            final_estimate=base_estimate,
            guidance_tokens=0,
            guidance_allowed=(
                self.experience_mode is not ExperienceMode.OFF
                and not self._experience_disabled
            ),
        )
        guidance = self._build_experience_guidance(
            planner_input,
            mode=mode,
            guidance_token_cap=provisional.guidance_token_cap,
            persist=False,
            **dict(guidance_kwargs),
        )
        inject = bool(
            self.experience_mode is ExperienceMode.GUIDED
            and _guidance_actionable(guidance)
        )
        prompt_context = dict(base_context)
        if inject and guidance is not None:
            prompt_context["experience_guidance"] = dict(guidance)
        guidance_tokens = (
            self.token_estimator.estimate_text(guidance) if inject else 0
        )
        prompt_request = describe(prompt_context)
        final_estimate = self._estimate_provider_request(
            prompt_request,
            context=prompt_context,
            guidance=guidance if inject else None,
        )
        envelope = self._allocate_token_envelope(
            planner_input,
            mode=mode,
            base_estimate=base_estimate,
            final_estimate=final_estimate,
            guidance_tokens=guidance_tokens,
            guidance_allowed=(
                self.experience_mode is not ExperienceMode.OFF
                and not self._experience_disabled
            ),
        )
        if inject and guidance_tokens > envelope.guidance_token_cap:
            guidance = self._build_experience_guidance(
                planner_input,
                mode=mode,
                guidance_token_cap=envelope.guidance_token_cap,
                persist=False,
                **dict(guidance_kwargs),
            )
            inject = bool(
                self.experience_mode is ExperienceMode.GUIDED
                and _guidance_actionable(guidance)
            )
            prompt_context = dict(base_context)
            if inject and guidance is not None:
                prompt_context["experience_guidance"] = dict(guidance)
            guidance_tokens = (
                self.token_estimator.estimate_text(guidance) if inject else 0
            )

        for _ in range(8):
            final_context = self._with_effective_max_output(
                prompt_context, envelope
            )
            final_request = describe(final_context)
            final_estimate = self._estimate_provider_request(
                final_request,
                context=final_context,
                guidance=guidance if inject else None,
            )
            updated = self._allocate_token_envelope(
                planner_input,
                mode=mode,
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=(
                    self.experience_mode is not ExperienceMode.OFF
                    and not self._experience_disabled
                ),
            )
            if updated.stable_hash == envelope.stable_hash:
                envelope = updated
                break
            envelope = updated
        else:
            raise V3OpenAIPlannerError(
                "hard-capped TokenEnvelope did not converge"
            )
        body = final_request.get("http_body")
        provider_max = body.get("max_tokens") if isinstance(body, Mapping) else None
        if provider_max != envelope.effective_max_output_tokens:
            raise V3OpenAIPlannerError(
                "hard-capped Provider max output diverged from TokenEnvelope"
            )
        self._persist_guidance(planner_input, guidance)
        return _BudgetedPreparedContext(
            context=final_context,
            provider_request=final_request,
            envelope=envelope,
            estimate=final_estimate,
            guidance=guidance,
        )

    def _prepare_budgeted_optimization_context(
        self,
        planner_input: Mapping[str, object],
        *,
        context: OptimizationContext,
        source: str,
        synth_report: Mapping[str, object],
        synth_evidence: Mapping[str, object],
    ) -> _BudgetedPreparedOptimization:
        if self.token_budget_policy is None:
            raise RuntimeError("dynamic token policy is disabled")
        if not self.token_budget_visible:
            return self._prepare_hard_capped_optimization_context(
                planner_input,
                context=context,
                source=source,
                synth_report=synth_report,
                synth_evidence=synth_evidence,
            )
        base_request = self.provider.describe_optimization_request(context)
        base_estimate = self._estimate_provider_request(
            base_request, context=context
        )
        provisional = self._allocate_token_envelope(
            planner_input,
            mode="OPTIMIZE",
            base_estimate=base_estimate,
            final_estimate=base_estimate,
            guidance_tokens=0,
            guidance_allowed=(
                self.experience_mode is not ExperienceMode.OFF
                and not self._experience_disabled
            ),
        )
        guidance = self._build_experience_guidance(
            planner_input,
            mode="OPTIMIZE",
            source=source,
            synth_report=synth_report,
            synth_evidence=synth_evidence,
            guidance_token_cap=provisional.guidance_token_cap,
            persist=False,
        )
        inject = bool(
            self.experience_mode is ExperienceMode.GUIDED
            and _guidance_actionable(guidance)
        )
        injected_guidance = guidance if inject else None
        guidance_tokens = (
            self.token_estimator.estimate_text(guidance) if inject else 0
        )
        envelope = provisional
        final_request = base_request
        final_estimate = base_estimate
        for _ in range(4):
            final_request = self.provider.describe_optimization_request_budgeted(
                context,
                envelope.to_dict(),
                experience_guidance=injected_guidance,
            )
            final_estimate = self._estimate_provider_request(
                final_request,
                context=context,
                guidance=injected_guidance,
            )
            updated = self._allocate_token_envelope(
                planner_input,
                mode="OPTIMIZE",
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=(
                    self.experience_mode is not ExperienceMode.OFF
                    and not self._experience_disabled
                ),
            )
            stable = (
                updated.estimated_input_tokens == envelope.estimated_input_tokens
                and updated.effective_max_output_tokens
                == envelope.effective_max_output_tokens
                and updated.guidance_token_cap == envelope.guidance_token_cap
            )
            envelope = updated
            if stable:
                break
        if inject and guidance_tokens > envelope.guidance_token_cap:
            guidance = self._build_experience_guidance(
                planner_input,
                mode="OPTIMIZE",
                source=source,
                synth_report=synth_report,
                synth_evidence=synth_evidence,
                guidance_token_cap=envelope.guidance_token_cap,
                persist=False,
            )
            inject = bool(
                self.experience_mode is ExperienceMode.GUIDED
                and _guidance_actionable(guidance)
            )
            injected_guidance = guidance if inject else None
            guidance_tokens = (
                self.token_estimator.estimate_text(guidance) if inject else 0
            )

        for _ in range(8):
            final_request = self.provider.describe_optimization_request_budgeted(
                context,
                envelope.to_dict(),
                experience_guidance=injected_guidance,
            )
            final_estimate = self._estimate_provider_request(
                final_request,
                context=context,
                guidance=injected_guidance,
            )
            updated = self._allocate_token_envelope(
                planner_input,
                mode="OPTIMIZE",
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=(
                    self.experience_mode is not ExperienceMode.OFF
                    and not self._experience_disabled
                ),
            )
            if updated.stable_hash == envelope.stable_hash:
                envelope = updated
                break
            envelope = updated
        else:
            raise V3OpenAIPlannerError(
                "TokenEnvelope did not converge with the final optimization request"
            )
        body = final_request.get("http_body")
        provider_max = body.get("max_tokens") if isinstance(body, Mapping) else None
        if provider_max != envelope.effective_max_output_tokens:
            raise V3OpenAIPlannerError(
                "Prompt TokenEnvelope and provider max output diverged"
            )
        self._persist_guidance(planner_input, guidance)
        dispatch = _BudgetedOptimizationDispatch(
            context=context,
            token_envelope=envelope.to_dict(),
            guidance=injected_guidance,
            token_budget_visible=True,
        )
        return _BudgetedPreparedOptimization(
            dispatch=dispatch,
            provider_request=final_request,
            envelope=envelope,
            estimate=final_estimate,
        )

    def _prepare_hard_capped_optimization_context(
        self,
        planner_input: Mapping[str, object],
        *,
        context: OptimizationContext,
        source: str,
        synth_report: Mapping[str, object],
        synth_evidence: Mapping[str, object],
    ) -> _BudgetedPreparedOptimization:
        if self.token_budget_policy is None:
            raise RuntimeError("dynamic token policy is disabled")
        base_request = self.provider.describe_optimization_request(context)
        base_estimate = self._estimate_provider_request(
            base_request, context=context
        )
        provisional = self._allocate_token_envelope(
            planner_input,
            mode="OPTIMIZE",
            base_estimate=base_estimate,
            final_estimate=base_estimate,
            guidance_tokens=0,
            guidance_allowed=(
                self.experience_mode is not ExperienceMode.OFF
                and not self._experience_disabled
            ),
        )
        guidance = self._build_experience_guidance(
            planner_input,
            mode="OPTIMIZE",
            source=source,
            synth_report=synth_report,
            synth_evidence=synth_evidence,
            guidance_token_cap=provisional.guidance_token_cap,
            persist=False,
        )
        inject = bool(
            self.experience_mode is ExperienceMode.GUIDED
            and _guidance_actionable(guidance)
        )
        injected_guidance = guidance if inject else None
        guidance_tokens = (
            self.token_estimator.estimate_text(guidance) if inject else 0
        )
        envelope = provisional
        for _ in range(8):
            final_request = self.provider.describe_optimization_request_capped(
                context,
                envelope.effective_max_output_tokens,
                experience_guidance=injected_guidance,
            )
            final_estimate = self._estimate_provider_request(
                final_request,
                context=context,
                guidance=injected_guidance,
            )
            updated = self._allocate_token_envelope(
                planner_input,
                mode="OPTIMIZE",
                base_estimate=base_estimate,
                final_estimate=final_estimate,
                guidance_tokens=guidance_tokens,
                guidance_allowed=(
                    self.experience_mode is not ExperienceMode.OFF
                    and not self._experience_disabled
                ),
            )
            if updated.stable_hash == envelope.stable_hash:
                envelope = updated
                break
            envelope = updated
        else:
            raise V3OpenAIPlannerError(
                "hard-capped optimization TokenEnvelope did not converge"
            )
        body = final_request.get("http_body")
        provider_max = body.get("max_tokens") if isinstance(body, Mapping) else None
        if provider_max != envelope.effective_max_output_tokens:
            raise V3OpenAIPlannerError(
                "hard-capped optimization Provider max diverged"
            )
        self._persist_guidance(planner_input, guidance)
        dispatch = _BudgetedOptimizationDispatch(
            context=context,
            token_envelope=envelope.to_dict(),
            guidance=injected_guidance,
            token_budget_visible=False,
        )
        return _BudgetedPreparedOptimization(
            dispatch=dispatch,
            provider_request=final_request,
            envelope=envelope,
            estimate=final_estimate,
        )

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
        call_gate = self._hybrid_call_gate(value, mode=mode_value)
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
                    "generation_required": task.get("generation_required", False),
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
                    "allow_large_kernel_body_patch": bool(
                        task.get("generation_required", False)
                    ),
                    "planner_cannot_choose_tools_or_final": True,
                },
            }
            token_envelope: TokenEnvelope | None = None
            if self.token_budget_policy is not None:
                budgeted = self._prepare_budgeted_mapping_context(
                    value,
                    mode=mode_value,
                    base_context=context,
                    describe=self.provider.describe_task_aware_request,
                    guidance_kwargs={
                        "source": source,
                        "failure_evidence": failure_evidence,
                    },
                )
                context = dict(budgeted.context)
                provider_request = budgeted.provider_request
                token_envelope = budgeted.envelope
                estimated_input_tokens = budgeted.estimate.total_tokens
                effective_max_output_tokens = (
                    budgeted.envelope.effective_max_output_tokens
                )
            else:
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
                estimated_input_tokens = max(
                    1, len(canonical_json(provider_request))
                )
                effective_max_output_tokens = self.max_output_tokens
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
            if token_envelope is not None:
                request["token_envelope"] = token_envelope.to_dict()
                request["token_envelope_sha256"] = token_envelope.stable_hash
                request["token_budget_visibility"] = (
                    "visible" if self.token_budget_visible else "hidden"
                )
            if call_gate is not None:
                request["planner_call_gate"] = call_gate
            return PreparedPlannerCall(
                request=request,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=effective_max_output_tokens,
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
                    "difficulty_status": task.get("difficulty_status", "UNKNOWN"),
                    "generation_required": task.get("generation_required", False),
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
                    "allow_large_kernel_body_patch": bool(
                        task.get("generation_required", False)
                    ),
                    "planner_cannot_choose_tools_or_final": True,
                },
            }
            token_envelope: TokenEnvelope | None = None
            if self.token_budget_policy is not None:
                budgeted = self._prepare_budgeted_mapping_context(
                    value,
                    mode="OPTIMIZE",
                    base_context=context,
                    describe=self.provider.describe_fast_experiment_request,
                    guidance_kwargs={
                        "source": source,
                        "synth_report": incumbent_report,
                        "synth_evidence": incumbent_evidence,
                    },
                )
                context = dict(budgeted.context)
                provider_request = budgeted.provider_request
                token_envelope = budgeted.envelope
                estimated_input_tokens = budgeted.estimate.total_tokens
                effective_max_output_tokens = (
                    budgeted.envelope.effective_max_output_tokens
                )
            else:
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
                provider_request = self.provider.describe_fast_experiment_request(
                    context
                )
                estimated_input_tokens = max(
                    1, len(canonical_json(provider_request))
                )
                effective_max_output_tokens = self.max_output_tokens
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
            if token_envelope is not None:
                request["token_envelope"] = token_envelope.to_dict()
                request["token_envelope_sha256"] = token_envelope.stable_hash
                request["token_budget_visibility"] = (
                    "visible" if self.token_budget_visible else "hidden"
                )
            if call_gate is not None:
                request["planner_call_gate"] = call_gate
            return PreparedPlannerCall(
                request=request,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=effective_max_output_tokens,
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
        guided_dispatch: _GuidedOptimizationDispatch | None = None
        budgeted_dispatch: _BudgetedOptimizationDispatch | None = None
        token_envelope: TokenEnvelope | None = None
        if self.token_budget_policy is not None:
            budgeted = self._prepare_budgeted_optimization_context(
                value,
                context=context,
                source=source,
                synth_report=incumbent_report,
                synth_evidence=incumbent_evidence,
            )
            provider_request = budgeted.provider_request
            budgeted_dispatch = budgeted.dispatch
            token_envelope = budgeted.envelope
            estimated_input_tokens = budgeted.estimate.total_tokens
            effective_max_output_tokens = budgeted.envelope.effective_max_output_tokens
        else:
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
                guided_dispatch = _GuidedOptimizationDispatch(context, guidance)
                provider_request = self.provider.describe_guided_optimization_request(
                    context, guidance
                )
            else:
                provider_request = self.provider.describe_optimization_request(context)
            estimated_input_tokens = max(1, len(canonical_json(provider_request)))
            effective_max_output_tokens = self.max_output_tokens
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
        if budgeted_dispatch is not None and token_envelope is not None:
            request["context_sha256"] = canonical_sha256(
                budgeted_dispatch.binding()
            )
            request["token_envelope"] = token_envelope.to_dict()
            request["token_envelope_sha256"] = token_envelope.stable_hash
            request["token_budget_visibility"] = (
                "visible" if self.token_budget_visible else "hidden"
            )
        if call_gate is not None:
            request["planner_call_gate"] = call_gate
        return PreparedPlannerCall(
            request=request,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=effective_max_output_tokens,
            dispatch_context=budgeted_dispatch or guided_dispatch or context,
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
        if isinstance(context, _BudgetedOptimizationDispatch):
            request = prepared.request
            if (
                request.get("schema_version") != OPENAI_V3_ADAPTER_REQUEST_SCHEMA
                or request.get("provider_fingerprint")
                != self._provider_fingerprint
                or request.get("context_sha256")
                != canonical_sha256(context.binding())
            ):
                raise V3OpenAIPlannerError(
                    "prepared budgeted optimization request binding is invalid"
                )
            if context.token_budget_visible:
                proposal = self.provider.propose_optimization_budgeted(
                    context.context,
                    context.token_envelope,
                    experience_guidance=context.guidance,
                )
            else:
                envelope = validate_token_envelope(context.token_envelope)
                proposal = self.provider.propose_optimization_capped(
                    context.context,
                    int(envelope["effective_max_output_tokens"]),
                    experience_guidance=context.guidance,
                )
            if not isinstance(proposal, PatchProposal):
                raise V3OpenAIPlannerError(
                    "budgeted optimization provider returned an invalid proposal"
                )
            if proposal.change_class != context.context.allowed_optimization_class:
                raise V3OpenAIPlannerError(
                    "budgeted proposal class diverges from the selected class"
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
