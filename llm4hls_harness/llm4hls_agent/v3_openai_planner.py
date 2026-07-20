"""OpenAI-compatible V2 provider adapter for the durable V3-B Planner boundary.

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


OPENAI_V3_ADAPTER_REQUEST_SCHEMA = "v3b.openai-v2-adapter-request.v1"
OPENAI_V3_ADAPTER_VERSION = "openai-v2-compat-planner-adapter-v1"


class V3OpenAIPlannerError(RuntimeError):
    """The V3 input cannot be projected safely into the V2 provider contract."""


class AuditableOptimizationProvider(Protocol):
    """Existing provider surface required by the compatibility adapter."""

    def fingerprint(self) -> str: ...

    def describe_optimization_request(
        self, context: OptimizationContext
    ) -> Mapping[str, object]: ...

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal: ...


@dataclass(frozen=True)
class _BoundArtifact:
    reference: str
    digest: str
    data: bytes


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
    if relative.is_absolute() or ".." in relative.parts:
        raise V3OpenAIPlannerError(f"Planner input {name} escapes the run")
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
    """Project one V3 round into the existing single-class OpenAI provider."""

    replay_policy = "NON_REPLAYABLE"

    def __init__(
        self,
        run_root: str | Path,
        provider: AuditableOptimizationProvider,
        *,
        final_reserve_credits: int = 25,
        max_output_tokens: int | None = None,
    ) -> None:
        self.run_root = Path(run_root).resolve()
        self.provider = provider
        if final_reserve_credits < 0:
            raise ValueError("final_reserve_credits must be non-negative")
        self.final_reserve_credits = int(final_reserve_credits)
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

    def fingerprint(self) -> str:
        identity = {
            "adapter": OPENAI_V3_ADAPTER_VERSION,
            "request_schema": OPENAI_V3_ADAPTER_REQUEST_SCHEMA,
            "provider_fingerprint": self._provider_fingerprint,
            "final_reserve_credits": self.final_reserve_credits,
            "max_output_tokens": self.max_output_tokens,
            "selector": "v2-deterministic-selection-v1",
        }
        return f"{OPENAI_V3_ADAPTER_VERSION}:{canonical_sha256(identity)}"

    def prepare(
        self, planner_input: Mapping[str, object]
    ) -> PreparedPlannerCall:
        value = validate_planner_input(planner_input)
        task = _mapping(value.get("task"), "task")
        round_state = _mapping(value.get("round"), "round")
        incumbent = _mapping(value.get("incumbent"), "incumbent")
        baseline = _mapping(value.get("baseline"), "baseline")
        policy = _mapping(value.get("policy"), "policy")
        budget = _mapping(value.get("budget"), "budget")

        source, incumbent_report, incumbent_evidence = _source_and_report(
            self.run_root, incumbent, name="incumbent"
        )
        _baseline_source, baseline_report, _baseline_evidence = _source_and_report(
            self.run_root, baseline, name="baseline"
        )
        current_metrics = _metrics_with_evidence(
            incumbent_report, incumbent_evidence
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
        # A byte-for-token reservation is intentionally conservative for
        # OpenAI-compatible tokenizers and prevents actual usage from silently
        # exceeding the durable reservation.
        estimated_input_tokens = max(1, len(encoded_provider_request))
        return PreparedPlannerCall(
            request=request,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=self.max_output_tokens,
            dispatch_context=context,
        )

    def invoke(self, prepared: PreparedPlannerCall) -> PatchProposal:
        context = prepared.dispatch_context
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
