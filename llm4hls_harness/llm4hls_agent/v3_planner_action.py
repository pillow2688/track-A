"""Durable, budgeted execution boundary for a non-replayable V3-B Planner."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Protocol

from .budget import BudgetExceeded, BudgetLedger
from .repair import PatchProposal, RepairProviderError
from .v3_planner import (
    canonical_json,
    canonical_sha256,
    proposal_payload,
    validate_planner_input,
)
from .workflow import _atomic_json, _write_once_or_verify


LIVE_PROVIDER_REQUEST_SCHEMA = "v3b.planner-provider-request.v1"
LIVE_PLANNER_ACTION_SCHEMA = "v3b.planner-action.v1"
LIVE_PLANNER_OUTCOME_SCHEMA = "v3b.planner-outcome.v1"
LIVE_PLANNER_STARTED_SCHEMA = "v3b.planner-started.v1"
LIVE_PLANNER_COMPLETED_SCHEMA = "v3b.planner-completed.v1"
LIVE_PLANNER_FAILURE_SCHEMA = "v3.token-provider-failure.v1"


class PlannerActionError(RuntimeError):
    """Base error for a durable live-Planner action."""


class PlannerActionAmbiguous(PlannerActionError):
    """A non-replayable Planner may have run without a durable outcome."""


class PlannerActionRejected(PlannerActionError):
    """A durable provider response was charged but cannot create a Candidate."""

    def __init__(
        self,
        message: str,
        *,
        action_id: str,
        request_ref: str,
        failure_ref: str,
        failure_sha256: str,
        reason: str,
        cached: bool,
    ) -> None:
        super().__init__(message)
        self.action_id = action_id
        self.request_ref = request_ref
        self.failure_ref = failure_ref
        self.failure_sha256 = failure_sha256
        self.reason = reason
        self.cached = cached


def completed_llm_action_ids(
    events: Iterable[Mapping[str, object]],
) -> set[str]:
    """Return the shared package/provenance identity set for charged calls."""

    completed: set[str] = set()
    for event in events:
        if event.get("kind") != "llm" or event.get("state") != "COMPLETED":
            continue
        action_id = event.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise PlannerActionError(
                "completed LLM Ledger action has no valid identity"
            )
        completed.add(action_id)
    return completed


def validate_live_planner_action_identity(
    action_request: Mapping[str, object],
    request_audit: Mapping[str, object],
    *,
    expected_action_id: str,
) -> None:
    """Validate the deterministic, non-replayable identity of one live call."""

    expected_fields = {
        "schema_version",
        "logical_operation_id",
        "attempt_index",
        "retry_of",
        "planner_fingerprint",
        "input_ref",
        "input_sha256",
        "request_ref",
        "request_sha256",
        "replay_policy",
    }
    planner_fingerprint = action_request.get("planner_fingerprint")
    input_sha256 = action_request.get("input_sha256")
    attempt_index = action_request.get("attempt_index")
    if (
        set(action_request) != expected_fields
        or action_request.get("schema_version") != LIVE_PLANNER_ACTION_SCHEMA
        or isinstance(attempt_index, bool)
        or not isinstance(attempt_index, int)
        or attempt_index != 0
        or action_request.get("retry_of") is not None
        or action_request.get("replay_policy") != "NON_REPLAYABLE"
        or not isinstance(planner_fingerprint, str)
        or not planner_fingerprint
        or not isinstance(input_sha256, str)
        or len(input_sha256) != 64
        or any(character not in "0123456789abcdef" for character in input_sha256)
    ):
        raise PlannerActionError(
            "live Planner action deterministic identity is invalid"
        )
    logical_operation_id = canonical_sha256(
        {
            "schema_version": "v3b.planner-logical-operation.v1",
            "planner_fingerprint": planner_fingerprint,
            "input_sha256": input_sha256,
        }
    )
    expected_request_ref = f"planner/requests/{logical_operation_id}.json"
    if (
        action_request.get("logical_operation_id") != logical_operation_id
        or action_request.get("request_ref") != expected_request_ref
        or request_audit.get("schema_version") != LIVE_PROVIDER_REQUEST_SCHEMA
        or request_audit.get("logical_operation_id") != logical_operation_id
        or request_audit.get("planner_fingerprint") != planner_fingerprint
        or request_audit.get("input_sha256") != input_sha256
        or canonical_sha256(action_request) != expected_action_id
    ):
        raise PlannerActionError(
            "live Planner action deterministic identity is invalid"
        )


@dataclass(frozen=True)
class PreparedPlannerCall:
    """A deterministic, secret-free audit request prepared before dispatch."""

    request: Mapping[str, object]
    estimated_input_tokens: int
    max_output_tokens: int
    dispatch_context: object | None = None
    # A3 may calculate an advisory while A2 is estimating the next call.  It
    # must not become a durable runtime decision until that call has passed
    # A2/the budget gate.  Keep the validated, secret-free advisory beside the
    # prepared call so the runner can materialize it exactly once at that
    # boundary without asking the ranker to make a second decision.
    experience_guidance: Mapping[str, object] | None = None
    experience_round_index: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("estimated_input_tokens", self.estimated_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        canonical_request = json.loads(
            canonical_json(dict(self.request)).decode("utf-8")
        )
        if not isinstance(canonical_request, dict):
            raise ValueError("Planner request audit must be an object")
        _reject_secret_fields(canonical_request)
        object.__setattr__(self, "request", canonical_request)
        if self.experience_guidance is not None:
            canonical_guidance = json.loads(
                canonical_json(dict(self.experience_guidance)).decode("utf-8")
            )
            if not isinstance(canonical_guidance, dict):
                raise ValueError("experience guidance must be an object")
            _reject_secret_fields(canonical_guidance)
            object.__setattr__(self, "experience_guidance", canonical_guidance)
        if self.experience_round_index is not None and (
            isinstance(self.experience_round_index, bool)
            or not isinstance(self.experience_round_index, int)
            or self.experience_round_index <= 0
        ):
            raise ValueError("experience_round_index must be a positive integer")

    @property
    def estimated_tokens(self) -> int:
        return self.estimated_input_tokens + self.max_output_tokens

    @property
    def effective_max_output_tokens(self) -> int:
        return self.max_output_tokens


class LivePlanner(Protocol):
    replay_policy: str

    def fingerprint(self) -> str: ...

    def prepare(
        self, planner_input: Mapping[str, object]
    ) -> PreparedPlannerCall: ...

    def invoke(self, prepared: PreparedPlannerCall) -> PatchProposal: ...


@dataclass(frozen=True)
class PlannerActionResult:
    proposal: PatchProposal
    action_id: str
    request_ref: str
    request_sha256: str
    output_ref: str
    output_sha256: str
    started_ref: str
    completed_ref: str
    cached: bool


def _after_started(_action_id: str) -> None:
    """Test seam after durable reserve/STARTED and before provider dispatch."""


def _after_reserved_before_started(_action_id: str) -> None:
    """Test seam in the provably pre-dispatch reserve/journal crash window."""


def _after_output_persisted(_action_id: str) -> None:
    """Test seam after outcome persistence and before Ledger completion."""


def _after_ledger_completed(_action_id: str) -> None:
    """Test seam after Ledger completion and before Planner COMPLETED."""


def _reject_secret_fields(value: object) -> None:
    secret_keys = {
        "api_key",
        "api-key",
        "x-api-key",
        "authorization",
        "access_token",
        "client_secret",
        "password",
        "token",
        "secret",
        "cookie",
        "proxy-authorization",
        "proxy_authorization",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in secret_keys:
                raise ValueError("Planner request audit must not contain secrets")
            _reject_secret_fields(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _reject_secret_fields(child)
    elif isinstance(value, str):
        for match in re.finditer(r"https?://[^\s\"'<>]+", value):
            parsed = urllib.parse.urlsplit(match.group(0).rstrip(",.;)]}"))
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Planner request audit URL must not contain userinfo, query, or fragment"
                )
        if re.search(r"\bbearer\s+\S+", value, flags=re.IGNORECASE) or re.search(
            r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|token|secret|cookie|proxy[_-]?authorization)\s*[:=]\s*\S+",
            value,
            flags=re.IGNORECASE,
        ):
            raise ValueError("Planner request audit must not contain secrets")
        without_urls = re.sub(r"https?://[^\s\"'<>]+", "", value)
        if re.search(
            r"(?<![:/A-Za-z0-9_.\-)\]}])/(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+",
            without_urls,
        ) or re.search(
            r"\b[A-Za-z]:\\[^\\\s\"'<>]+", without_urls
        ):
            raise ValueError(
                "Planner request audit must not contain absolute local paths"
            )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlannerActionError(
            f"cannot read Planner action artifact {path.name}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PlannerActionError(
            f"Planner action artifact {path.name} is not an object"
        )
    return value


def _safe_artifact(root: Path, reference: str) -> Path:
    relative = Path(reference)
    if not reference or relative.is_absolute() or ".." in relative.parts:
        raise PlannerActionError("Planner action artifact escapes the run")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PlannerActionError("Planner action artifact uses a symbolic link")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise PlannerActionError("Planner action artifact escapes the run") from exc
    if not path.is_file():
        raise PlannerActionError(f"Planner action artifact is missing: {reference}")
    return path


def _assert_safe_destination(root: Path, path: Path) -> None:
    root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PlannerActionError("Planner action destination escapes the run") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PlannerActionError(
                "Planner action destination uses a symbolic link"
            )
    resolved_parent = path.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise PlannerActionError("Planner action destination escapes the run") from exc


def semantic_proposal_payload(proposal: PatchProposal) -> dict[str, object]:
    """Return model semantics without provider execution/accounting metadata."""

    full = proposal_payload(proposal)
    execution_fields = {
        "provider",
        "model",
        "revision",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "request_id",
        "duration_seconds",
    }
    return {
        key: value for key, value in full.items() if key not in execution_fields
    }


def _validate_complete_live_usage(proposal: PatchProposal) -> None:
    if proposal.input_tokens <= 0 or proposal.output_tokens <= 0:
        raise PlannerActionError(
            "live Planner proposal is missing complete provider token usage"
        )


def build_live_planner_outcome(
    *,
    action_id: str,
    input_sha256: str,
    planner_fingerprint: str,
    proposal: PatchProposal,
) -> dict[str, object]:
    _validate_complete_live_usage(proposal)
    payload = proposal_payload(proposal)
    semantics = semantic_proposal_payload(proposal)
    return {
        "schema_version": LIVE_PLANNER_OUTCOME_SCHEMA,
        "action_id": action_id,
        "input_sha256": input_sha256,
        "outcome": "PROPOSAL",
        "proposal": payload,
        "semantic_proposal_sha256": canonical_sha256(semantics),
        "provider_binding": {
            "planner_fingerprint": planner_fingerprint,
            "provider": proposal.provider,
            "model": proposal.model,
            "revision": proposal.revision,
        },
        "usage": {
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
            "cached_input_tokens": proposal.cached_input_tokens,
            "tokens_used": proposal.tokens_used,
            "duration_seconds": proposal.duration_seconds,
            "request_id": proposal.request_id,
            "usage_complete": True,
        },
    }


def load_live_planner_outcome(
    value: Mapping[str, object],
    *,
    expected_action_id: str,
    expected_input_sha256: str,
    expected_planner_fingerprint: str,
) -> PatchProposal:
    expected_fields = {
        "schema_version",
        "action_id",
        "input_sha256",
        "outcome",
        "proposal",
        "semantic_proposal_sha256",
        "provider_binding",
        "usage",
    }
    if set(value) != expected_fields:
        raise PlannerActionError("live Planner outcome fields are invalid")
    if (
        value.get("schema_version") != LIVE_PLANNER_OUTCOME_SCHEMA
        or value.get("action_id") != expected_action_id
        or value.get("input_sha256") != expected_input_sha256
        or value.get("outcome") != "PROPOSAL"
    ):
        raise PlannerActionError("live Planner outcome identity is invalid")
    payload = value.get("proposal")
    provider_binding = value.get("provider_binding")
    usage = value.get("usage")
    if not isinstance(payload, Mapping):
        raise PlannerActionError("live Planner proposal is invalid")
    try:
        proposal = PatchProposal.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise PlannerActionError(f"live Planner proposal is invalid: {exc}") from exc
    _validate_complete_live_usage(proposal)
    if value.get("semantic_proposal_sha256") != canonical_sha256(
        semantic_proposal_payload(proposal)
    ):
        raise PlannerActionError("live Planner semantic proposal hash mismatch")
    expected_provider_binding = {
        "planner_fingerprint": expected_planner_fingerprint,
        "provider": proposal.provider,
        "model": proposal.model,
        "revision": proposal.revision,
    }
    expected_usage = {
        "input_tokens": proposal.input_tokens,
        "output_tokens": proposal.output_tokens,
        "cached_input_tokens": proposal.cached_input_tokens,
        "tokens_used": proposal.tokens_used,
        "duration_seconds": proposal.duration_seconds,
        "request_id": proposal.request_id,
        "usage_complete": True,
    }
    if provider_binding != expected_provider_binding or usage != expected_usage:
        raise PlannerActionError("live Planner execution metadata mismatch")
    return proposal


class PlannerActionJournal:
    """Execute a live Planner at most once and reconcile durable boundaries."""

    def __init__(self, run_root: str | Path, budget: BudgetLedger) -> None:
        self.run_root = Path(run_root).resolve()
        self.budget = budget

    def execute_or_recover(
        self,
        planner: LivePlanner,
        planner_input: Mapping[str, object],
        *,
        input_ref: str,
        input_sha256: str,
        candidate_id: str,
        code_hash: str,
        timeout_seconds: float | None = None,
    ) -> PlannerActionResult:
        canonical_input = validate_planner_input(planner_input)
        if canonical_sha256(canonical_input) != input_sha256:
            raise PlannerActionError("live Planner input digest mismatch")
        input_path = _safe_artifact(self.run_root, input_ref)
        if canonical_sha256(_read_json_object(input_path)) != input_sha256:
            raise PlannerActionError("live Planner input artifact mismatch")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate_id must not be empty")
        if not isinstance(code_hash, str) or not code_hash:
            raise ValueError("code_hash must not be empty")
        planner_fingerprint = planner.fingerprint()
        if not isinstance(planner_fingerprint, str) or not planner_fingerprint:
            raise ValueError("live Planner fingerprint must not be empty")
        if planner.replay_policy != "NON_REPLAYABLE":
            raise ValueError("live Planner must declare NON_REPLAYABLE")
        prepared = planner.prepare(canonical_input)
        if not isinstance(prepared, PreparedPlannerCall):
            raise TypeError("live Planner prepare() returned an invalid request")
        if prepared.estimated_tokens <= 0:
            raise ValueError("live Planner must reserve a positive token bound")

        # A2/the budget gate has already selected this node before the journal
        # is entered.  Let an A3-capable planner now persist its *one* advisory
        # decision.  This deliberately happens after that authorization and
        # before the LLM ledger reservation, so an A3 persistence error can
        # never leave a charged-but-undispatched Planner action behind.
        record_advisory = getattr(planner, "record_authorized_advisory", None)
        if callable(record_advisory):
            record_advisory(prepared)

        logical_operation_id = canonical_sha256(
            {
                "schema_version": "v3b.planner-logical-operation.v1",
                "planner_fingerprint": planner_fingerprint,
                "input_sha256": input_sha256,
            }
        )
        request_audit = {
            "schema_version": LIVE_PROVIDER_REQUEST_SCHEMA,
            "logical_operation_id": logical_operation_id,
            "planner_fingerprint": planner_fingerprint,
            "input_sha256": input_sha256,
            "estimated_input_tokens": prepared.estimated_input_tokens,
            "configured_max_output_tokens": (
                prepared.request.get("token_envelope", {}).get(
                    "configured_max_output_tokens", prepared.max_output_tokens
                )
                if isinstance(prepared.request.get("token_envelope"), Mapping)
                else prepared.max_output_tokens
            ),
            "effective_max_output_tokens": prepared.max_output_tokens,
            "provider_parameter_name": "max_tokens",
            # Compatibility alias for pre-policy readers.
            "max_output_tokens": prepared.max_output_tokens,
            "request": dict(prepared.request),
        }
        request_ref = f"planner/requests/{logical_operation_id}.json"
        request_path = self.run_root / request_ref
        _assert_safe_destination(self.run_root, request_path)
        _write_once_or_verify(request_path, request_audit)
        request_sha256 = canonical_sha256(request_audit)
        action_request: dict[str, object] = {
            "schema_version": LIVE_PLANNER_ACTION_SCHEMA,
            "logical_operation_id": logical_operation_id,
            "attempt_index": 0,
            "retry_of": None,
            "planner_fingerprint": planner_fingerprint,
            "input_ref": input_ref,
            "input_sha256": input_sha256,
            "request_ref": request_ref,
            "request_sha256": request_sha256,
            "replay_policy": planner.replay_policy,
        }
        action_id = canonical_sha256(action_request)
        action_root = self.run_root / "control" / "live_planner_actions"
        started_ref = (
            f"control/live_planner_actions/{action_id}.started.json"
        )
        completed_ref = (
            f"control/live_planner_actions/{action_id}.completed.json"
        )
        output_ref = f"planner/live_outcomes/{action_id}.json"
        failure_ref = f"planner/provider_failures/{action_id}.json"
        started_path = self.run_root / started_ref
        completed_path = self.run_root / completed_ref
        output_path = self.run_root / output_ref
        failure_path = self.run_root / failure_ref
        for destination in (started_path, completed_path, output_path, failure_path):
            _assert_safe_destination(self.run_root, destination)

        expected_started = {
            "schema_version": LIVE_PLANNER_STARTED_SCHEMA,
            "action_id": action_id,
            "status": "STARTED",
            "request": action_request,
        }
        ledger_events = self.budget.action_events(action_id)
        self._verify_ledger_start(
            action_id=action_id,
            candidate_id=candidate_id,
            code_hash=code_hash,
            request_sha256=request_sha256,
            estimated_tokens=prepared.estimated_tokens,
            allow_missing=not (
                output_path.exists()
                or started_path.exists()
                or completed_path.exists()
            ),
        )
        if output_path.exists() and failure_path.exists():
            raise PlannerActionError(
                "live Planner has both proposal and failure outcomes"
            )
        if failure_path.exists():
            if failure_path.is_symlink():
                raise PlannerActionError("live Planner failure is a symbolic link")
            self._verify_started(started_path, expected_started)
            failure = self._load_failure(
                failure_path,
                action_id=action_id,
                input_sha256=input_sha256,
                planner_fingerprint=planner_fingerprint,
            )
            failure_sha256 = _sha256_file(failure_path)
            usage = failure.get("usage")
            if isinstance(usage, Mapping) and usage.get("usage_complete") is True:
                self._reconcile_failure_ledger(
                    action_id=action_id,
                    failure_ref=failure_ref,
                    failure_sha256=failure_sha256,
                    failure=failure,
                )
                _write_once_or_verify(
                    completed_path,
                    self._failure_completed_record(
                        action_id=action_id,
                        action_request=action_request,
                        failure_ref=failure_ref,
                        failure_sha256=failure_sha256,
                        failure=failure,
                    ),
                )
                raise PlannerActionRejected(
                    "live Planner provider output was durably rejected",
                    action_id=action_id,
                    request_ref=request_ref,
                    failure_ref=failure_ref,
                    failure_sha256=failure_sha256,
                    reason=str(failure.get("truncation_reason") or failure.get("error_type")),
                    cached=True,
                )
            if self.budget.has_pending(action_id):
                self.budget.mark_ambiguous_conservative(
                    action_id,
                    reason="provider failure has UNKNOWN token usage and cannot be replayed",
                )
            raise PlannerActionAmbiguous(
                "live Planner failure has unknown usage and will not be replayed"
            )
        if output_path.exists():
            if output_path.is_symlink():
                raise PlannerActionError("live Planner outcome is a symbolic link")
            self._verify_started(started_path, expected_started)
            proposal = self._load_outcome(
                output_path,
                action_id=action_id,
                input_sha256=input_sha256,
                planner_fingerprint=planner_fingerprint,
            )
            output_sha256 = _sha256_file(output_path)
            self._reconcile_ledger(
                action_id=action_id,
                output_ref=output_ref,
                output_sha256=output_sha256,
                proposal=proposal,
            )
            expected_completed = self._completed_record(
                action_id=action_id,
                action_request=action_request,
                output_ref=output_ref,
                output_sha256=output_sha256,
                proposal=proposal,
            )
            _write_once_or_verify(completed_path, expected_completed)
            return PlannerActionResult(
                proposal=proposal,
                action_id=action_id,
                request_ref=request_ref,
                request_sha256=request_sha256,
                output_ref=output_ref,
                output_sha256=output_sha256,
                started_ref=started_ref,
                completed_ref=completed_ref,
                cached=True,
            )

        if completed_path.exists():
            raise PlannerActionError("live Planner COMPLETED has no durable outcome")
        if started_path.exists():
            self._verify_started(started_path, expected_started)
            if self.budget.has_pending(action_id):
                self.budget.mark_ambiguous_conservative(
                    action_id,
                    reason=(
                        "live Planner STARTED has no durable outcome; provider "
                        "dispatch cannot be replayed"
                    ),
                )
            raise PlannerActionAmbiguous(
                "live Planner action is ambiguous and will not be replayed"
            )
        if ledger_events:
            # This is the only safe missing-journal recovery: the Ledger was
            # reserved, but the durable STARTED artifact was never written.
            # Provider dispatch is sequenced strictly after that artifact.
            if not self.budget.has_pending(action_id):
                raise PlannerActionError(
                    "live Planner Ledger state has no STARTED journal"
                )
            _write_once_or_verify(started_path, expected_started)
        else:
            self.budget.reserve(
                action_id=action_id,
                kind="llm",
                candidate_id=candidate_id,
                code_hash=code_hash,
                tool_config_hash=request_sha256,
                estimated_tokens=prepared.estimated_tokens,
            )
            _after_reserved_before_started(action_id)
            _write_once_or_verify(started_path, expected_started)
        self._verify_ledger_start(
            action_id=action_id,
            candidate_id=candidate_id,
            code_hash=code_hash,
            request_sha256=request_sha256,
            estimated_tokens=prepared.estimated_tokens,
            allow_missing=False,
        )
        _after_started(action_id)
        try:
            bounded_invoke = getattr(planner, "invoke_with_timeout", None)
            if timeout_seconds is not None:
                timeout = float(timeout_seconds)
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError(
                        "timeout_seconds must be finite and positive"
                    )
                proposal = (
                    bounded_invoke(
                        prepared,
                        timeout_seconds=timeout,
                    )
                    if callable(bounded_invoke)
                    else planner.invoke(prepared)
                )
            else:
                proposal = planner.invoke(prepared)
        except RepairProviderError as exc:
            failure = self._provider_failure_record(
                action_id=action_id,
                input_sha256=input_sha256,
                planner_fingerprint=planner_fingerprint,
                prepared=prepared,
                error=exc,
            )
            _atomic_json(failure_path, failure)
            failure_sha256 = _sha256_file(failure_path)
            if exc.usage_complete:
                self.budget.complete(
                    action_id=action_id,
                    result_ref=failure_ref,
                    result_sha256=failure_sha256,
                    elapsed_s=exc.duration_seconds,
                    tokens_used=exc.input_tokens + exc.output_tokens,
                    input_tokens=exc.input_tokens,
                    output_tokens=exc.output_tokens,
                    cached_input_tokens=exc.cached_input_tokens,
                )
                _write_once_or_verify(
                    completed_path,
                    self._failure_completed_record(
                        action_id=action_id,
                        action_request=action_request,
                        failure_ref=failure_ref,
                        failure_sha256=failure_sha256,
                        failure=failure,
                    ),
                )
                raise PlannerActionRejected(
                    "live Planner provider output was durably rejected",
                    action_id=action_id,
                    request_ref=request_ref,
                    failure_ref=failure_ref,
                    failure_sha256=failure_sha256,
                    reason=str(
                        exc.truncation_reason or type(exc).__name__
                    ),
                    cached=False,
                ) from exc
            if self.budget.has_pending(action_id):
                self.budget.mark_ambiguous_conservative(
                    action_id,
                    reason=(
                        "live Planner provider failure has UNKNOWN usage and "
                        "cannot be replayed"
                    ),
                )
            raise PlannerActionAmbiguous(
                "live Planner failed with unknown usage after non-replayable dispatch"
            ) from exc
        except Exception as exc:
            if self.budget.has_pending(action_id):
                self.budget.mark_ambiguous_conservative(
                    action_id,
                    reason=f"live Planner failed without a durable outcome: {type(exc).__name__}",
                )
            raise PlannerActionAmbiguous(
                "live Planner failed after non-replayable dispatch"
            ) from exc
        if not isinstance(proposal, PatchProposal):
            if self.budget.has_pending(action_id):
                self.budget.mark_ambiguous_conservative(
                    action_id,
                    reason="live Planner returned an invalid result type",
                )
            raise PlannerActionAmbiguous("live Planner returned an invalid result")
        try:
            _validate_complete_live_usage(proposal)
        except PlannerActionError as exc:
            if self.budget.has_pending(action_id):
                self.budget.mark_ambiguous_conservative(
                    action_id,
                    reason="live Planner returned incomplete provider token usage",
                )
            raise PlannerActionAmbiguous(
                "live Planner usage is incomplete and cannot be finalized"
            ) from exc
        outcome = build_live_planner_outcome(
            action_id=action_id,
            input_sha256=input_sha256,
            planner_fingerprint=planner_fingerprint,
            proposal=proposal,
        )
        _atomic_json(output_path, outcome)
        output_sha256 = _sha256_file(output_path)
        _after_output_persisted(action_id)
        self.budget.complete(
            action_id=action_id,
            result_ref=output_ref,
            result_sha256=output_sha256,
            elapsed_s=proposal.duration_seconds,
            tokens_used=proposal.tokens_used,
            input_tokens=proposal.input_tokens,
            output_tokens=proposal.output_tokens,
            cached_input_tokens=proposal.cached_input_tokens,
        )
        _after_ledger_completed(action_id)
        _write_once_or_verify(
            completed_path,
            self._completed_record(
                action_id=action_id,
                action_request=action_request,
                output_ref=output_ref,
                output_sha256=output_sha256,
                proposal=proposal,
            ),
        )
        return PlannerActionResult(
            proposal=proposal,
            action_id=action_id,
            request_ref=request_ref,
            request_sha256=request_sha256,
            output_ref=output_ref,
            output_sha256=output_sha256,
            started_ref=started_ref,
            completed_ref=completed_ref,
            cached=False,
        )

    @staticmethod
    def _provider_failure_record(
        *,
        action_id: str,
        input_sha256: str,
        planner_fingerprint: str,
        prepared: PreparedPlannerCall,
        error: RepairProviderError,
    ) -> dict[str, object]:
        envelope = prepared.request.get("token_envelope")
        configured = (
            envelope.get("configured_max_output_tokens")
            if isinstance(envelope, Mapping)
            else prepared.max_output_tokens
        )
        reason = error.truncation_reason
        usage_complete = bool(error.usage_complete)
        actual_input = error.input_tokens if usage_complete else None
        actual_output = error.output_tokens if usage_complete else None
        return {
            "schema_version": LIVE_PLANNER_FAILURE_SCHEMA,
            "action_id": action_id,
            "input_sha256": input_sha256,
            "planner_fingerprint": planner_fingerprint,
            "outcome": "PROVIDER_OUTPUT_REJECTED",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "response_excerpt": error.response_excerpt,
            "requested_max_output_tokens": configured,
            "effective_max_output_tokens": prepared.max_output_tokens,
            "provider_parameter_name": "max_tokens",
            "finish_reason": error.finish_reason,
            "output_truncated": bool(error.output_truncated),
            "truncation_reason": reason,
            "json_incomplete": reason == "JSON_INCOMPLETE",
            "patch_incomplete": reason == "PATCH_INCOMPLETE",
            "usage": {
                "actual_input_tokens": actual_input,
                "actual_output_tokens": actual_output,
                "actual_total_tokens": (
                    error.input_tokens + error.output_tokens
                    if usage_complete
                    else None
                ),
                "cached_input_tokens": (
                    error.cached_input_tokens if usage_complete else None
                ),
                "duration_seconds": error.duration_seconds,
                "request_id": error.request_id,
                "usage_complete": usage_complete,
            },
        }

    @staticmethod
    def _load_failure(
        path: Path,
        *,
        action_id: str,
        input_sha256: str,
        planner_fingerprint: str,
    ) -> dict[str, object]:
        value = _read_json_object(path)
        if (
            value.get("schema_version") != LIVE_PLANNER_FAILURE_SCHEMA
            or value.get("action_id") != action_id
            or value.get("input_sha256") != input_sha256
            or value.get("planner_fingerprint") != planner_fingerprint
            or value.get("outcome") != "PROVIDER_OUTPUT_REJECTED"
            or not isinstance(value.get("usage"), Mapping)
        ):
            raise PlannerActionError("live Planner failure identity is invalid")
        return value

    def _reconcile_failure_ledger(
        self,
        *,
        action_id: str,
        failure_ref: str,
        failure_sha256: str,
        failure: Mapping[str, object],
    ) -> None:
        usage = failure.get("usage")
        if not isinstance(usage, Mapping) or usage.get("usage_complete") is not True:
            raise PlannerActionError("provider failure usage is incomplete")
        input_tokens = int(usage.get("actual_input_tokens") or 0)
        output_tokens = int(usage.get("actual_output_tokens") or 0)
        cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
        completed = self.budget.completed_event(action_id)
        if completed is not None:
            if (
                completed.get("kind") != "llm"
                or completed.get("actual_cost") != self.budget.cost("llm")
                or completed.get("result_ref") != failure_ref
                or completed.get("result_sha256") != failure_sha256
                or completed.get("input_tokens") != input_tokens
                or completed.get("output_tokens") != output_tokens
                or completed.get("cached_input_tokens")
                != cached_input_tokens
                or completed.get("tokens_used") != input_tokens + output_tokens
            ):
                raise PlannerActionError("provider failure Ledger mismatch")
            started = next(
                (
                    event
                    for event in self.budget.action_events(action_id)
                    if event.get("state") == "STARTED"
                ),
                None,
            )
            estimated_tokens = (
                started.get("estimated_tokens")
                if isinstance(started, Mapping)
                else None
            )
            if (
                completed.get("token_reservation_overrun") is True
                or isinstance(estimated_tokens, bool)
                or not isinstance(estimated_tokens, int)
                or input_tokens + output_tokens > estimated_tokens
                or int(self.budget.snapshot()["tokens_remaining"]) < 0
            ):
                raise BudgetExceeded(
                    "live Planner token usage exceeded its durable reservation"
                )
            return
        if self.budget.is_ambiguous(action_id):
            raise PlannerActionError(
                "provider failure appeared after an ambiguous close"
            )
        if not self.budget.has_pending(action_id):
            raise PlannerActionError("provider failure has no pending Ledger action")
        self.budget.complete(
            action_id=action_id,
            result_ref=failure_ref,
            result_sha256=failure_sha256,
            elapsed_s=float(usage.get("duration_seconds") or 0.0),
            tokens_used=input_tokens + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
        )

    @staticmethod
    def _failure_completed_record(
        *,
        action_id: str,
        action_request: Mapping[str, object],
        failure_ref: str,
        failure_sha256: str,
        failure: Mapping[str, object],
    ) -> dict[str, object]:
        usage = failure.get("usage")
        assert isinstance(usage, Mapping)
        return {
            "schema_version": LIVE_PLANNER_COMPLETED_SCHEMA,
            "action_id": action_id,
            "status": "COMPLETED",
            "request": dict(action_request),
            "outcome": "PROVIDER_OUTPUT_REJECTED",
            "result_ref": failure_ref,
            "result_sha256": failure_sha256,
            "truncation_reason": failure.get("truncation_reason"),
            "finish_reason": failure.get("finish_reason"),
            "tokens_used": usage.get("actual_total_tokens"),
            "input_tokens": usage.get("actual_input_tokens"),
            "output_tokens": usage.get("actual_output_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
        }

    @staticmethod
    def _load_outcome(
        path: Path,
        *,
        action_id: str,
        input_sha256: str,
        planner_fingerprint: str,
    ) -> PatchProposal:
        return load_live_planner_outcome(
            _read_json_object(path),
            expected_action_id=action_id,
            expected_input_sha256=input_sha256,
            expected_planner_fingerprint=planner_fingerprint,
        )

    def _reconcile_ledger(
        self,
        *,
        action_id: str,
        output_ref: str,
        output_sha256: str,
        proposal: PatchProposal,
    ) -> None:
        completed = self.budget.completed_event(action_id)
        if completed is not None:
            if (
                completed.get("kind") != "llm"
                or completed.get("actual_cost") != self.budget.cost("llm")
                or completed.get("result_ref") != output_ref
                or completed.get("result_sha256") != output_sha256
                or completed.get("tokens_used") != proposal.tokens_used
                or completed.get("input_tokens") != proposal.input_tokens
                or completed.get("output_tokens") != proposal.output_tokens
                or completed.get("cached_input_tokens")
                != proposal.cached_input_tokens
            ):
                raise PlannerActionError("live Planner Ledger result mismatch")
            started = next(
                (
                    event
                    for event in self.budget.action_events(action_id)
                    if event.get("state") == "STARTED"
                ),
                None,
            )
            estimated_tokens = (
                started.get("estimated_tokens")
                if isinstance(started, Mapping)
                else None
            )
            if (
                completed.get("token_reservation_overrun") is True
                or isinstance(estimated_tokens, bool)
                or not isinstance(estimated_tokens, int)
                or proposal.tokens_used > estimated_tokens
                or int(self.budget.snapshot()["tokens_remaining"]) < 0
            ):
                raise BudgetExceeded(
                    "live Planner token usage exceeded its durable reservation"
                )
            return
        if self.budget.is_ambiguous(action_id):
            raise PlannerActionError(
                "live Planner outcome appeared after an ambiguous close"
            )
        if not self.budget.has_pending(action_id):
            raise PlannerActionError("live Planner outcome has no pending Ledger action")
        self.budget.complete(
            action_id=action_id,
            result_ref=output_ref,
            result_sha256=output_sha256,
            elapsed_s=proposal.duration_seconds,
            tokens_used=proposal.tokens_used,
            input_tokens=proposal.input_tokens,
            output_tokens=proposal.output_tokens,
            cached_input_tokens=proposal.cached_input_tokens,
        )

    @staticmethod
    def _verify_started(path: Path, expected: Mapping[str, object]) -> None:
        if path.is_symlink() or _read_json_object(path) != dict(expected):
            raise PlannerActionError("live Planner STARTED journal mismatch")

    def _verify_ledger_start(
        self,
        *,
        action_id: str,
        candidate_id: str,
        code_hash: str,
        request_sha256: str,
        estimated_tokens: int,
        allow_missing: bool,
    ) -> None:
        events = self.budget.action_events(action_id)
        started = next(
            (event for event in events if event.get("state") == "STARTED"),
            None,
        )
        if started is None:
            if allow_missing and not events:
                return
            raise PlannerActionError("live Planner has no bound Ledger STARTED")
        if (
            started.get("kind") != "llm"
            or started.get("candidate_id") != candidate_id
            or started.get("code_hash") != code_hash
            or started.get("tool_config_hash") != request_sha256
            or started.get("estimated_cost") != self.budget.cost("llm")
            or started.get("estimated_tokens") != estimated_tokens
        ):
            raise PlannerActionError("live Planner Ledger STARTED binding mismatch")

    @staticmethod
    def _completed_record(
        *,
        action_id: str,
        action_request: Mapping[str, object],
        output_ref: str,
        output_sha256: str,
        proposal: PatchProposal,
    ) -> dict[str, object]:
        return {
            "schema_version": LIVE_PLANNER_COMPLETED_SCHEMA,
            "action_id": action_id,
            "status": "COMPLETED",
            "request": dict(action_request),
            "outcome": "PROPOSAL",
            "result_ref": output_ref,
            "result_sha256": output_sha256,
            "semantic_proposal_sha256": canonical_sha256(
                semantic_proposal_payload(proposal)
            ),
            "tokens_used": proposal.tokens_used,
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
            "cached_input_tokens": proposal.cached_input_tokens,
        }
