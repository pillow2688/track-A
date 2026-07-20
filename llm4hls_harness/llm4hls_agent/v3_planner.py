"""Versioned Planner contracts and the deterministic V3-A1 adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Mapping, Protocol

from .repair import PatchProposal


PLANNER_INPUT_SCHEMA = "v3a.planner-input.v1"
PLANNER_OUTPUT_SCHEMA = "v3a.planner-output.v1"
PLANNER_ACTION_SCHEMA = "v3a.planner-action.v2"


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _canonical_copy(value: object) -> object:
    return json.loads(canonical_json(value).decode("utf-8"))


def stable_validation(value: Mapping[str, object]) -> dict[str, object]:
    """Drop replay-only metadata while retaining validation identity."""

    def visit(item: object) -> object:
        if isinstance(item, Mapping):
            return {
                str(key): visit(child)
                for key, child in item.items()
                if str(key) not in {"cached", "timestamp"}
            }
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    result = visit(value)
    if not isinstance(result, dict):
        raise TypeError("stable validation must remain an object")
    return result


def build_planner_input(
    *,
    task: Mapping[str, object],
    round_state: Mapping[str, object],
    incumbent: Mapping[str, object],
    baseline: Mapping[str, object],
    history: Sequence[Mapping[str, object]],
    policy: Mapping[str, object],
    budget: Mapping[str, object],
) -> dict[str, object]:
    """Build one canonical input; callers provide only public, stable facts."""

    value = {
        "schema_version": PLANNER_INPUT_SCHEMA,
        "task": dict(task),
        "round": dict(round_state),
        "incumbent": dict(incumbent),
        "baseline": dict(baseline),
        "history": [dict(item) for item in history],
        "policy": dict(policy),
        "budget": dict(budget),
    }
    copied = _canonical_copy(value)
    if not isinstance(copied, dict):
        raise TypeError("Planner input must be an object")
    return copied


def validate_planner_input(value: Mapping[str, object]) -> dict[str, object]:
    expected_fields = {
        "schema_version",
        "task",
        "round",
        "incumbent",
        "baseline",
        "history",
        "policy",
        "budget",
    }
    if set(value) != expected_fields or value.get("schema_version") != PLANNER_INPUT_SCHEMA:
        raise ValueError("Planner input fields do not match v1 contract")
    for name in ("task", "round", "incumbent", "baseline", "policy", "budget"):
        if not isinstance(value.get(name), Mapping):
            raise ValueError(f"Planner input {name} must be an object")
    history = value.get("history")
    if not isinstance(history, list) or any(
        not isinstance(item, Mapping) for item in history
    ):
        raise ValueError("Planner input history must be an object list")
    copied = _canonical_copy(value)
    if not isinstance(copied, dict):
        raise TypeError("Planner input must remain an object")
    return copied


def proposal_payload(proposal: PatchProposal) -> dict[str, object]:
    value = proposal.to_dict()
    value["required_validation"] = list(proposal.required_validation)
    return value


class Planner(Protocol):
    replay_policy: str

    def fingerprint(self) -> str: ...

    def plan(self, planner_input: Mapping[str, object]) -> PatchProposal: ...


class ScriptedPlanner:
    """Deterministic adapter proving the contract before an LLM owns it."""

    replay_policy = "DETERMINISTIC"

    def __init__(self, proposals: Sequence[PatchProposal]) -> None:
        self._proposals = tuple(proposals)
        if not self._proposals:
            raise ValueError("ScriptedPlanner requires at least one proposal")

    def fingerprint(self) -> str:
        return "scripted-planner-v1:" + canonical_sha256(
            [proposal_payload(item) for item in self._proposals]
        )

    def plan(self, planner_input: Mapping[str, object]) -> PatchProposal:
        if planner_input.get("schema_version") != PLANNER_INPUT_SCHEMA:
            raise ValueError("ScriptedPlanner received an unsupported input schema")
        round_state = planner_input.get("round")
        if not isinstance(round_state, Mapping):
            raise ValueError("Planner input round state is missing")
        round_index = round_state.get("round_index")
        if (
            isinstance(round_index, bool)
            or not isinstance(round_index, int)
            or round_index <= 0
            or round_index > len(self._proposals)
        ):
            raise ValueError("ScriptedPlanner round index is out of range")
        return self._proposals[round_index - 1]


def planner_action_request(
    *,
    planner_fingerprint: str,
    planner_input_ref: str,
    planner_input_sha256: str,
    replay_policy: str,
) -> dict[str, object]:
    if not all(
        isinstance(item, str) and item
        for item in (
            planner_fingerprint,
            planner_input_ref,
            planner_input_sha256,
            replay_policy,
        )
    ):
        raise ValueError("Planner action identity fields must be non-empty strings")
    return {
        "schema_version": PLANNER_ACTION_SCHEMA,
        "planner_fingerprint": planner_fingerprint,
        "input_ref": planner_input_ref,
        "input_sha256": planner_input_sha256,
        "replay_policy": replay_policy,
    }


def planner_action_id(request: Mapping[str, object]) -> str:
    if request.get("schema_version") != PLANNER_ACTION_SCHEMA:
        raise ValueError("Planner action request has an unsupported schema")
    return canonical_sha256(request)


def build_planner_output(
    *,
    action_id: str,
    input_sha256: str,
    proposal: PatchProposal,
) -> dict[str, object]:
    payload = proposal_payload(proposal)
    return {
        "schema_version": PLANNER_OUTPUT_SCHEMA,
        "action_id": action_id,
        "input_sha256": input_sha256,
        "outcome": "PROPOSAL",
        "proposal": payload,
        "proposal_sha256": canonical_sha256(payload),
    }


def load_planner_output(
    value: Mapping[str, object],
    *,
    expected_action_id: str | None = None,
    expected_input_sha256: str | None = None,
) -> PatchProposal:
    expected_fields = {
        "schema_version",
        "action_id",
        "input_sha256",
        "outcome",
        "proposal",
        "proposal_sha256",
    }
    if set(value) != expected_fields:
        raise ValueError("Planner output fields do not match v1 contract")
    if (
        value.get("schema_version") != PLANNER_OUTPUT_SCHEMA
        or value.get("outcome") != "PROPOSAL"
    ):
        raise ValueError("Planner output has an unsupported schema or outcome")
    action_id = value.get("action_id")
    input_sha256 = value.get("input_sha256")
    if not isinstance(action_id, str) or not isinstance(input_sha256, str):
        raise ValueError("Planner output identity is invalid")
    if expected_action_id is not None and action_id != expected_action_id:
        raise ValueError("Planner output action binding mismatch")
    if expected_input_sha256 is not None and input_sha256 != expected_input_sha256:
        raise ValueError("Planner output input binding mismatch")
    payload = value.get("proposal")
    if not isinstance(payload, Mapping):
        raise ValueError("Planner output proposal is not an object")
    if value.get("proposal_sha256") != canonical_sha256(payload):
        raise ValueError("Planner output proposal hash mismatch")
    return PatchProposal.from_dict(payload)
