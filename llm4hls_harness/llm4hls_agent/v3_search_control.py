"""Compact, deterministic search-control facts for routed V3 repairs.

This module deliberately has no tool, graph, Candidate, Ledger, or model
authority.  It turns already persisted failure and proposal facts into a small
Planner contract: what must be repaired, what has already been tried, and what
kind of next experiment is required.  Dynamic run paths, action IDs and time
stamps are never used as semantic evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence


SEARCH_CONTROL_SCHEMA = "v3.search-control.v1"
PROPOSAL_EXPERIMENT_SCHEMA = "v3.proposal-experiment.v1"
OBLIGATION_STATE_SCHEMA = "v3.obligation-state.v1"
CANDIDATE_STATE_SCHEMA = "v3.candidate-state.v1"

_OBLIGATION_KIND = {
    "FUNCTIONAL_CORRECTNESS": "functional_correctness",
    "SYNTHESIS_LEGALITY": "synthesis_legality",
    "RTL_LIVENESS": "rtl_liveness",
    "INTERFACE_PROTOCOL": "interface_safety",
    "PATCH_APPLICABILITY": "interface_safety",
    "TIMING_LEGALITY": "timing",
    "RESOURCE_LEGALITY": "resource",
    "STREAM_TOPOLOGY": "stream_topology",
    "HIDDEN_REGRESSION_RISK": "hidden_regression_risk",
    "PERFORMANCE_OR_CORRECTNESS": "performance",
}

_DYNAMIC = re.compile(
    r"(?:[A-Za-z]:)?(?:/[^\s:]+)+|\b(?:20\d{2}|1\d{9,})\b|"
    r"(?:candidate|run|action)[_-]?[0-9a-f]{6,}",
    re.IGNORECASE,
)


def _text(value: object, *, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_DYNAMIC.sub("<dynamic>", value).split())[:limit]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _failure_stage(raw: Mapping[str, object], phase: str, patch_error: str) -> str:
    """Normalize tool-specific phase strings into the public stage vocabulary."""

    schema = _text(raw.get("schema_version"), limit=120).casefold()
    explicit = _text(raw.get("stage"), limit=48).upper()
    corpus = " ".join((explicit, phase, schema)).upper()
    if patch_error:
        return "PATCH_VALIDATION"
    if "COSIM" in corpus or "CO_SIM" in corpus:
        return "COSIM"
    if "SYNTH" in corpus:
        return "SYNTH"
    if any(token in corpus for token in ("CSIM", "COMPILE", "RUNTIME")):
        return "CSIM"
    return "UNKNOWN"


def failure_facts(
    evidence: object, *, patch_failure: object = None
) -> dict[str, object]:
    """Return a stable failure projection, including terminal/mechanical split."""

    raw = evidence if isinstance(evidence, Mapping) else {}
    patch = patch_failure if isinstance(patch_failure, Mapping) else {}
    patch_error = _text(patch.get("error_type"), limit=96)
    phase = _text(raw.get("phase"), limit=48).upper()
    stage = _failure_stage(raw, phase, patch_error)
    kind = _text(raw.get("failure_kind"), limit=96).upper()
    summary = _text(
        raw.get("synthesis_error") or raw.get("error_summary"), limit=240
    )
    is_mechanical = bool(patch_error) or kind in {
        "PATCH_VALIDATION_ERROR",
        "PROVIDER_OUTPUT_REJECTED",
    }
    terminal = bool(
        kind in {"DEADLOCK", "RTL_MISMATCH", "TIMEOUT", "SYNTH_ERROR", "COMPILE_ERROR", "RUNTIME_FAIL", "CONSTRAINT_VIOLATION"}
        or phase in {"COSIM_FAIL", "SYNTH_ERROR", "COMPILE_ERROR", "TIMEOUT"}
    ) and not is_mechanical
    signature_payload = {
        "phase": phase,
        "failure_kind": kind,
        "summary": summary,
        "patch_error": patch_error,
        "deadlock": raw.get("deadlock") is True,
        "timeout": raw.get("timeout") is True,
        "rtl_mismatch": raw.get("rtl_mismatch") is True,
        "unsupported": list(raw.get("unsupported_constructs", [])[:4])
        if isinstance(raw.get("unsupported_constructs"), list)
        else [],
    }
    return {
        "last_failure_stage": stage,
        "last_failure_kind": kind or patch_error or "UNKNOWN",
        "patch_error_type": patch_error or None,
        "last_failure_signature": _digest(signature_payload),
        "failure_summary": summary or patch_error or "UNKNOWN",
        "last_failed_candidate_id": _text(raw.get("candidate_id"), limit=96) or None,
        # This is a run-relative Artifact binding, not Planner evidence text.
        # Keep it exact so package sealing can verify it; reject absolute or
        # traversal paths rather than replacing the action id with a token.
        "tool_report_ref": (
            str(raw.get("result_ref"))
            if isinstance(raw.get("result_ref"), str)
            and raw.get("result_ref")
            and not str(raw.get("result_ref")).startswith(("/", "\\"))
            and ".." not in str(raw.get("result_ref")).replace("\\", "/").split("/")
            else None
        ),
        "cosim_progress": _text(raw.get("cosim_progress"), limit=96) or None,
        "no_progress_seconds": raw.get("no_progress_seconds")
        if isinstance(raw.get("no_progress_seconds"), (int, float))
        and not isinstance(raw.get("no_progress_seconds"), bool)
        else None,
        "cosim_runtime": {
            key: raw.get(key)
            for key in (
                "xsim_started",
                "runtime_stage",
                "transaction_progress",
                "log_growth",
                "output_growth",
            )
            if raw.get(key) is not None
        },
        "terminal_failure": terminal,
        "mechanical_failure": is_mechanical,
        "safe_local_recovery_available": bool(
            patch.get("safe_local_recovery_available") is True
        ),
    }


def primary_obligation(mode: object, evidence: object, *, patch_failure: object = None) -> str:
    """Map only public failure facts to one actionable obligation."""

    facts = failure_facts(evidence, patch_failure=patch_failure)
    kind = str(facts["last_failure_kind"])
    summary = str(facts["failure_summary"]).casefold()
    normalized_mode = str(mode).upper()
    if facts["mechanical_failure"]:
        return "PATCH_APPLICABILITY"
    if normalized_mode == "SYNTH_FIX" or kind in {"SYNTH_ERROR", "CONSTRAINT_VIOLATION"}:
        return "SYNTHESIS_LEGALITY"
    if normalized_mode == "STRUCTURAL_FIX" or kind in {"DEADLOCK", "RTL_MISMATCH"}:
        return "RTL_LIVENESS" if "deadlock" in summary or kind == "DEADLOCK" else "INTERFACE_PROTOCOL"
    if normalized_mode == "REPAIR":
        return "FUNCTIONAL_CORRECTNESS"
    if "resource" in summary:
        return "RESOURCE_LEGALITY"
    if "clock" in summary or "timing" in summary:
        return "TIMING_LEGALITY"
    return "PERFORMANCE_OR_CORRECTNESS"


def obligation_state(
    mode: object, evidence: object, *, patch_failure: object = None
) -> dict[str, object]:
    """Represent exactly one open, evidence-bound repair obligation.

    The uppercase ``id`` remains compatible with the existing Planner wire
    contract.  ``kind`` is the stable lower-case vocabulary used for reports,
    comparisons and future extensions; it prevents a mode name from being
    mistaken for the specific unresolved constraint.
    """

    facts = failure_facts(evidence, patch_failure=patch_failure)
    obligation_id = primary_obligation(mode, evidence, patch_failure=patch_failure)
    raw = evidence if isinstance(evidence, Mapping) else {}
    raw_regions = raw.get("source_locations") or raw.get("affected_regions")
    regions = (
        [_text(item, limit=160) for item in raw_regions[:8] if _text(item, limit=160)]
        if isinstance(raw_regions, list)
        else []
    )
    file_name = _text(raw.get("file"), limit=160)
    if file_name and file_name not in regions:
        regions.append(file_name)
    refs = [
        item
        for item in (facts.get("tool_report_ref"), raw.get("evidence_ref"))
        if isinstance(item, str) and item and not item.startswith(("/", "\\"))
    ]
    priority = 100 if facts["terminal_failure"] else 80 if facts["mechanical_failure"] else 60
    return {
        "schema_version": OBLIGATION_STATE_SCHEMA,
        "id": obligation_id,
        "kind": _OBLIGATION_KIND.get(obligation_id, "hidden_regression_risk"),
        "status": "OPEN",
        "priority": priority,
        "evidence_refs": refs[:2],
        "affected_regions": regions[:8],
    }


def candidate_state(candidate_id: object, candidate: object) -> dict[str, object]:
    """Project a Registry record into the compact CandidateState contract."""

    record = candidate if isinstance(candidate, Mapping) else {}
    validation = record.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}

    def stage(name: str) -> str:
        item = validation.get(name)
        if isinstance(item, Mapping):
            value = item.get("ok")
            if value is True:
                return "PASS"
            if value is False:
                return "FAIL"
        if item is True:
            return "PASS"
        if item is False:
            return "FAIL"
        return "NOT_RUN"

    return {
        "schema_version": CANDIDATE_STATE_SCHEMA,
        "candidate_id": str(candidate_id),
        "parent_id": record.get("parent_id") if isinstance(record.get("parent_id"), str) else None,
        "code_hash": record.get("code_hash") if isinstance(record.get("code_hash"), str) else None,
        "validation_level": str(record.get("status") or "UNKNOWN"),
        "csim_status": stage("csim"),
        "synth_status": stage("synth"),
        "cosim_status": stage("cosim"),
        "clock_status": stage("clock"),
        "resource_status": stage("resource"),
        "change_risk": str(record.get("change_risk") or "UNKNOWN"),
    }


def continuation_action(
    *,
    facts: Mapping[str, object],
    attempted: Sequence[Mapping[str, object]],
    baseline_id: str,
    incumbent_id: str,
    active_probe_id: str | None = None,
    final_reserve_only: bool = False,
) -> str:
    """Choose one bounded continuation action from persisted facts only."""

    if final_reserve_only:
        return "FINALIZE"
    if facts.get("mechanical_failure") is True:
        # Header count/location normalization is deterministic and already
        # constrained by the complete Unified Diff dry-run contract.  If the
        # normalized form remains invalid, the caller records the precise
        # evidence and must not silently materialize a Candidate.
        return "CONTINUE_WITHOUT_LLM"
    if facts.get("terminal_failure") is True and attempted and incumbent_id != baseline_id:
        return "SWITCH_PARENT"
    if facts.get("terminal_failure") is True:
        return "CONTINUE_WITH_LLM"
    if active_probe_id and active_probe_id != incumbent_id:
        return "STOP"
    return "CONTINUE_WITH_LLM"


def structural_facts(source: object) -> dict[str, object]:
    """Extract conservative DATAFLOW/stream facts from public kernel source.

    This is an explanatory and planning aid, never a correctness proof.  It
    intentionally reports ``UNKNOWN`` instead of inventing a graph whenever a
    source pattern is not statically visible.
    """

    text = source if isinstance(source, str) else ""
    streams: set[str] = set()
    for declaration in re.finditer(
        r"hls::stream\s*<[^>]+>\s+([^;]+);", text
    ):
        # Public HLS code commonly declares several FIFO variables in one
        # statement.  Keep just identifier tokens; this is evidence for a
        # conservative topology aid, not a full C++ parser.
        streams.update(
            re.findall(r"\b([A-Za-z_]\w*)\b", declaration.group(1))
        )
    streams = sorted(streams)
    # A deliberately small brace matcher: enough for compact HLS kernels, and
    # returns unknown rather than pretending to parse C++ when it cannot.
    functions: dict[str, str] = {}
    for match in re.finditer(r"(?:^|\n)\s*(?:void|[A-Za-z_]\w*[\w:<>,\s*&]*)\s+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", text):
        depth = 1
        cursor = match.end()
        while cursor < len(text) and depth:
            depth += (text[cursor] == "{") - (text[cursor] == "}")
            cursor += 1
        if depth == 0:
            functions[match.group(1)] = text[match.end():cursor - 1]
    facts: list[dict[str, object]] = []
    for stream_name in streams[:12]:
        writes = len(re.findall(rf"\b{re.escape(stream_name)}\s*\.\s*write\s*\(", text))
        reads = len(re.findall(rf"\b{re.escape(stream_name)}\s*\.\s*read\s*\(", text))
        depth = re.search(
            rf"#pragma\s+HLS\s+STREAM[^\n]*\bvariable\s*=\s*{re.escape(stream_name)}[^\n]*\bdepth\s*=\s*(\d+)",
            text,
            re.IGNORECASE,
        )
        producers = sorted(
            function_name for function_name, body in functions.items()
            if re.search(rf"\b{re.escape(stream_name)}\s*\.\s*write\s*\(", body)
        )
        consumers = sorted(
            function_name for function_name, body in functions.items()
            if re.search(rf"\b{re.escape(stream_name)}\s*\.\s*read\s*\(", body)
        )
        facts.append(
            {
                "stream": stream_name,
                "write_count": writes,
                "read_count": reads,
                "declared_depth": int(depth.group(1)) if depth else None,
                "balance": "BALANCED" if writes == reads else "UNBALANCED",
                "producer_processes": producers,
                "consumer_processes": consumers,
                "producer_count": len(producers),
                "consumer_count": len(consumers),
                "possible_multi_producer": len(producers) > 1,
                "possible_multi_consumer": len(consumers) > 1,
            }
        )
    process_edges: dict[str, set[str]] = {name: set() for name in functions}
    for stream in facts:
        for producer in stream["producer_processes"]:
            for consumer in stream["consumer_processes"]:
                if producer != consumer:
                    process_edges[str(producer)].add(str(consumer))
    call_edges: dict[str, set[str]] = {name: set() for name in functions}
    for caller, body in functions.items():
        for callee in functions:
            if callee != caller and re.search(
                rf"\b{re.escape(callee)}\s*\(", body
            ):
                call_edges[caller].add(callee)
    cycles: list[list[str]] = []
    for start in sorted(process_edges):
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for child in process_edges.get(node, ()):
                if child == start and len(path) > 1:
                    cycle = path + [start]
                    if cycle not in cycles:
                        cycles.append(cycle)
                elif child not in path and len(path) < 8:
                    stack.append((child, path + [child]))
    fixed_trip_loops = len(re.findall(r"for\s*\([^;]+;[^;]*(?:<|<=)\s*\d+", text))
    return {
        "dataflow_present": bool(re.search(r"#pragma\s+HLS\s+DATAFLOW", text, re.IGNORECASE)),
        "stream_count": len(streams),
        "streams": facts,
        "process_call_graph": {
            name: sorted(process_edges[name] | call_edges[name])
            for name in sorted(process_edges)
        },
        "stream_dependency_graph": {
            name: sorted(edges) for name, edges in sorted(process_edges.items())
        },
        "stream_dependency_cycles": cycles[:8],
        "has_stream_dependency_cycle": bool(cycles),
        "fixed_trip_loop_count": fixed_trip_loops,
        "feedback_initial_token": "UNKNOWN",
        "candidate_changes_stream_topology": "UNKNOWN",
        "topology_confidence": "SOURCE_PATTERN" if streams else "UNKNOWN",
    }


def action_family_for_proposal(
    proposal: object, *, obligation: str, patch_failure: object = None
) -> str:
    """Classify an already returned patch without relying on a task identifier."""

    patch = _text(getattr(proposal, "patch", ""), limit=6000).casefold()
    hypothesis = _text(getattr(proposal, "hypothesis", ""), limit=400).casefold()
    corpus = patch + " " + hypothesis
    if isinstance(patch_failure, Mapping):
        return "UNIFIED_DIFF_COORDINATE_RECOVERY"
    if obligation == "SYNTHESIS_LEGALITY":
        if any(token in corpus for token in ("vector", "new ", "delete", "malloc", "std::")):
            return "SYNTH_LANGUAGE_COMPATIBILITY"
        return "SYNTHESIS_LEGALITY_LOCAL_REWRITE"
    if obligation in {"RTL_LIVENESS", "INTERFACE_PROTOCOL"}:
        if any(token in corpus for token in ("#pragma hls stream", "depth=", " fifo", "fifo_")):
            return "FIFO_CAPACITY_OR_PROTOCOL"
        if any(token in corpus for token in ("dataflow", ".write(", ".read(", "stream")):
            return "DATAFLOW_TOPOLOGY_OR_ORDER"
        return "STRUCTURAL_SCHEDULING"
    if obligation == "FUNCTIONAL_CORRECTNESS":
        return "LOCAL_FUNCTIONAL_REPAIR"
    if obligation == "TIMING_LEGALITY":
        return "TIMING_OR_SCHEDULING"
    if obligation == "RESOURCE_LEGALITY":
        return "RESOURCE_REDUCTION"
    return "LOCAL_SEMANTIC_CHANGE"


def proposal_experiment(
    proposal: object,
    *,
    obligation: str,
    patch_failure: object = None,
    input_failure_signature: str | None = None,
) -> dict[str, object]:
    """Create the durable proposal-experiment record before materialization."""

    proposal_obligation = _text(
        getattr(proposal, "target_obligation", ""), limit=96
    )
    if proposal_obligation and proposal_obligation != obligation:
        raise ValueError("proposal target_obligation conflicts with current obligation")
    hypothesis = _text(getattr(proposal, "hypothesis", ""), limit=400) or "UNSPECIFIED_HYPOTHESIS"
    expected = _text(getattr(proposal, "expected_effect", ""), limit=320)
    validations = getattr(proposal, "validation_plan", ()) or getattr(
        proposal, "required_validation", ()
    )
    validation_plan = [str(item) for item in validations if str(item) in {"csim", "synth", "cosim"}]
    declared_family = _text(getattr(proposal, "action_family", ""), limit=120)
    family = declared_family or action_family_for_proposal(
        proposal, obligation=obligation, patch_failure=patch_failure
    )
    parameters = getattr(proposal, "action_parameters", {})
    if not isinstance(parameters, Mapping):
        raise ValueError("proposal action_parameters must be an object")
    normalized_parameters = {
        str(key): value for key, value in parameters.items()
        if isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))
    }
    record = {
        "schema_version": PROPOSAL_EXPERIMENT_SCHEMA,
        "target_obligation": obligation,
        "hypothesis": hypothesis,
        "action_family": family,
        "action_parameters": normalized_parameters,
        "expected_effect": expected or "VERIFY_ROUTED_FAILURE_IS_REMOVED",
        "validation_plan": validation_plan,
        "failure_criteria": _text(
            getattr(proposal, "failure_criteria", ""), limit=240
        ) or "ROUTED_VALIDATION_NOT_PASS",
        "fallback": _text(getattr(proposal, "fallback", ""), limit=240)
        or "VERIFIED_INCUMBENT_OR_BASELINE",
        "changes_stream_topology": bool(
            re.search(
                r"(?:hls::stream|#pragma\s+HLS\s+(?:STREAM|DATAFLOW)|\.read\s*\(|\.write\s*\()",
                str(getattr(proposal, "patch", "")),
                re.IGNORECASE,
            )
        ),
        "input_failure_signature": (
            input_failure_signature
            if isinstance(input_failure_signature, str)
            and re.fullmatch(r"[0-9a-f]{64}", input_failure_signature)
            else None
        ),
    }
    record["experiment_sha256"] = _digest(record)
    return record


def search_control_state(
    *, mode: object,
    evidence: object,
    patch_failure: object,
    history: Sequence[Mapping[str, object]],
    semantic_no_improvement: int,
    baseline_id: str,
    incumbent_id: str,
    source: object = None,
    candidate_records: Mapping[str, object] | None = None,
    active_probe_id: str | None = None,
    final_reserve_only: bool = False,
) -> dict[str, object]:
    """Build a bounded planner-visible control contract from durable history."""

    facts = failure_facts(evidence, patch_failure=patch_failure)
    obligation_record = obligation_state(
        mode, evidence, patch_failure=patch_failure
    )
    obligation = str(obligation_record["id"])
    attempted: list[dict[str, object]] = []
    for row in history:
        experiment = row.get("proposal_experiment")
        if not isinstance(experiment, Mapping):
            continue
        family = _text(experiment.get("action_family"), limit=96)
        hypothesis = _text(experiment.get("hypothesis"), limit=240)
        if family:
            attempted.append({"action_family": family, "hypothesis": hypothesis})
    attempted = attempted[-4:]
    prior_input_signatures = [
        item.get("proposal_experiment", {}).get("input_failure_signature")
        for item in history
        if isinstance(item.get("proposal_experiment"), Mapping)
        and isinstance(
            item.get("proposal_experiment", {}).get("input_failure_signature"),
            str,
        )
    ]
    new_evidence_since_last_planner = (
        not prior_input_signatures
        or facts["last_failure_signature"] != prior_input_signatures[-1]
    )
    required_change = (
        "REGENERATE_APPLICABLE_DIFF"
        if facts["mechanical_failure"]
        else "CHANGE_ACTION_FAMILY_OR_USE_VERIFIED_FALLBACK"
        if attempted and facts["terminal_failure"]
        else "ADDRESS_PRIMARY_OBLIGATION"
    )
    records = candidate_records if isinstance(candidate_records, Mapping) else {}
    portfolio_ids = [baseline_id, incumbent_id]
    if active_probe_id:
        portfolio_ids.append(active_probe_id)
    candidate_states = {
        candidate_id: candidate_state(candidate_id, records.get(candidate_id))
        for candidate_id in dict.fromkeys(portfolio_ids)
        if candidate_id
    }
    fallback_parent_id = incumbent_id or baseline_id
    hypotheses: list[dict[str, object]] = []
    for row in history[-4:]:
        experiment = row.get("proposal_experiment")
        if not isinstance(experiment, Mapping):
            continue
        family = _text(experiment.get("action_family"), limit=96)
        hypothesis = _text(experiment.get("hypothesis"), limit=240)
        if not family or not hypothesis:
            continue
        hypotheses.append(
            {
                "id": str(experiment.get("experiment_sha256") or _digest(experiment)),
                "target_obligation": str(experiment.get("target_obligation") or obligation),
                "cause": hypothesis,
                "confidence": "EVIDENCE_BOUND",
                "action_family": family,
                "tried": True,
                "result": str(row.get("status") or row.get("reason") or "UNKNOWN"),
                "failure_signature": facts["last_failure_signature"],
            }
        )
    chosen_action = continuation_action(
        facts=facts,
        attempted=attempted,
        baseline_id=baseline_id,
        incumbent_id=incumbent_id,
        active_probe_id=active_probe_id,
        final_reserve_only=final_reserve_only,
    )
    raw_evidence = evidence if isinstance(evidence, Mapping) else {}
    structure = structural_facts(source)
    topology_attempts = [
        item.get("proposal_experiment", {}).get("changes_stream_topology")
        for item in history
        if isinstance(item.get("proposal_experiment"), Mapping)
    ]
    if topology_attempts:
        structure["candidate_changes_stream_topology"] = bool(topology_attempts[-1])
    structure["cosim_runtime"] = {
        key: raw_value
        for key, raw_value in (
            ("xsim_started", raw_evidence.get("xsim_started")),
            ("runtime_stage", raw_evidence.get("runtime_stage")),
            ("transaction_progress", raw_evidence.get("transaction_progress")),
            ("log_growth", raw_evidence.get("log_growth")),
            ("output_growth", raw_evidence.get("output_growth")),
            ("cosim_progress", facts.get("cosim_progress")),
            ("no_progress_seconds", facts.get("no_progress_seconds")),
        )
        if raw_value is not None
    }
    obligations = [obligation_record]
    if structure["has_stream_dependency_cycle"] or any(
        item.get("possible_multi_producer") is True
        or item.get("possible_multi_consumer") is True
        for item in structure["streams"]
        if isinstance(item, Mapping)
    ):
        obligations.append(
            {
                "schema_version": OBLIGATION_STATE_SCHEMA,
                "id": "STREAM_TOPOLOGY",
                "kind": "stream_topology",
                "status": "OPEN",
                "priority": 90,
                "evidence_refs": list(obligation_record["evidence_refs"]),
                "affected_regions": [
                    str(item.get("stream"))
                    for item in structure["streams"][:8]
                    if isinstance(item, Mapping)
                ],
            }
        )
    obligations.append(
        {
            "schema_version": OBLIGATION_STATE_SCHEMA,
            "id": "HIDDEN_REGRESSION_RISK",
            "kind": "hidden_regression_risk",
            "status": "MONITORED",
            "priority": 1,
            "evidence_refs": [],
            "affected_regions": [],
        }
    )
    return {
        "schema_version": SEARCH_CONTROL_SCHEMA,
        "failure": facts,
        "primary_obligation": obligation,
        "obligations": obligations,
        "hypotheses": hypotheses,
        "semantic_no_improvement_rounds": max(0, int(semantic_no_improvement)),
        "candidate_portfolio": {
            "baseline_candidate_id": baseline_id,
            "verified_incumbent_id": incumbent_id,
            "active_probe_id": active_probe_id,
            "fallback_parent_id": fallback_parent_id,
            "parent_selection_reason": (
                "VERIFIED_INCUMBENT"
                if fallback_parent_id != baseline_id
                else "IMMUTABLE_BASELINE"
            ),
            "candidate_states": candidate_states,
        },
        "attempted_experiments": attempted,
        "new_evidence_since_last_planner": new_evidence_since_last_planner,
        "required_next_change": required_change,
        "recommended_continuation_action": chosen_action,
        "structural_facts": structure,
    }


def is_repeated_terminal_experiment(
    experiment: Mapping[str, object], history: Sequence[Mapping[str, object]], *, terminal_failure: bool) -> bool:
    """Reject the same hypothesis *and* action family after a terminal fact.

    A different transformation family can test the same suspected cause, and
    a different hypothesis can use the same safe family.  Treating either
    dimension alone as a duplicate would block useful, falsifiable follow-up
    experiments.  Repeating both is the non-informative retry this guard
    prevents.
    """

    if not terminal_failure:
        return False
    family = experiment.get("action_family")
    if not isinstance(family, str) or not family:
        return False
    hypothesis = _text(experiment.get("hypothesis"), limit=240).casefold()
    if not hypothesis:
        return False
    for row in history:
        previous = row.get("proposal_experiment")
        if (
            isinstance(previous, Mapping)
            and previous.get("action_family") == family
            and _text(previous.get("hypothesis"), limit=240).casefold()
            == hypothesis
        ):
            return True
    return False


__all__ = [
    "PROPOSAL_EXPERIMENT_SCHEMA",
    "SEARCH_CONTROL_SCHEMA",
    "CANDIDATE_STATE_SCHEMA",
    "OBLIGATION_STATE_SCHEMA",
    "candidate_state",
    "continuation_action",
    "failure_facts",
    "is_repeated_terminal_experiment",
    "obligation_state",
    "primary_obligation",
    "proposal_experiment",
    "search_control_state",
    "structural_facts",
]
