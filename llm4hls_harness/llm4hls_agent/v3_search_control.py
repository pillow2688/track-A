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
RTL_LIVENESS_OBLIGATION_SCHEMA = "v3.rtl-liveness-obligation.v1"
STRUCTURAL_GUARD_SCHEMA = "v3.structural-liveness-guard.v1"
ACTION_FAMILY_FRONTIER_SCHEMA = "v3.structural-action-family-frontier.v1"
SEMANTIC_PROGRESS_SCHEMA = "v3.semantic-progress.v1"

# These are the causal experiment families used for a liveness obligation.
# They intentionally describe *how* a candidate changes a topology rather
# than a task or a particular kernel.  The frontier is a planning aid; it does
# not require every family to be attempted.
STRUCTURAL_ACTION_FAMILIES = (
    "capacity_adjustment",
    "producer_normalization",
    "topology_elimination",
    "sequential_pipeline_fallback",
    "protocol_initialization",
    "stream_balance_repair",
)

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

# The Planner's public wire contract permits descriptive action-family labels.
# Keep a small, evidence-based alias map so that a renamed FIFO-depth proposal
# cannot evade the durable experiment-family boundary.  Unknown labels remain
# distinct rather than being guessed into a more restrictive class.
_ACTION_FAMILY_ALIASES = {
    "STREAM_DEPTH_INCREASE": "FIFO_CAPACITY_OR_PROTOCOL",
    "FIFO_DEPTH_INCREASE": "FIFO_CAPACITY_OR_PROTOCOL",
    "FIFO_DEPTH_CHANGE": "FIFO_CAPACITY_OR_PROTOCOL",
    "FIFO_CAPACITY_CHANGE": "FIFO_CAPACITY_OR_PROTOCOL",
    "CAPACITY_ADJUSTMENT": "FIFO_CAPACITY_OR_PROTOCOL",
    "PRODUCER_NORMALIZATION": "PRODUCER_NORMALIZATION",
    "TOPOLOGY_ELIMINATION": "DATAFLOW_TOPOLOGY_OR_ORDER",
    "SEQUENTIAL_PIPELINE_FALLBACK": "SEQUENTIAL_PIPELINE_FALLBACK",
    "PROTOCOL_INITIALIZATION": "PROTOCOL_INITIALIZATION",
    "STREAM_BALANCE_REPAIR": "STREAM_BALANCE_REPAIR",
}


def _text(value: object, *, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_DYNAMIC.sub("<dynamic>", value).split())[:limit]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_action_family(value: object) -> str:
    """Return a stable action-family label without inventing unknown classes."""

    family = _text(value, limit=120).upper()
    return _ACTION_FAMILY_ALIASES.get(family, family)


def structural_action_family(value: object) -> str | None:
    """Map a public proposal label to one structural frontier family.

    Unknown labels deliberately remain unknown.  This prevents a generic
    Planner label from borrowing evidence for one of the six audited families.
    """

    raw = _text(value, limit=120).casefold().replace("-", "_").replace(" ", "_")
    canonical = canonical_action_family(value)
    if raw in STRUCTURAL_ACTION_FAMILIES:
        return raw
    if canonical == "FIFO_CAPACITY_OR_PROTOCOL":
        return "capacity_adjustment"
    if canonical == "DATAFLOW_TOPOLOGY_OR_ORDER":
        return "topology_elimination"
    if canonical == "PRODUCER_NORMALIZATION":
        return "producer_normalization"
    if canonical == "SEQUENTIAL_PIPELINE_FALLBACK":
        return "sequential_pipeline_fallback"
    if canonical == "PROTOCOL_INITIALIZATION":
        return "protocol_initialization"
    if canonical == "STREAM_BALANCE_REPAIR":
        return "stream_balance_repair"
    return None


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
    action_family_frontier: Mapping[str, object] | None = None,
    structural_guard_rejected: bool = False,
) -> str:
    """Choose one bounded continuation action from persisted facts only."""

    if final_reserve_only:
        return "FINALIZE"
    if structural_guard_rejected and isinstance(action_family_frontier, Mapping):
        if action_family_frontier.get("has_high_value_untried_family") is True:
            return "CONTINUE_WITH_LLM"
        if facts.get("terminal_failure") is True:
            return "STOP"
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
    cycle_initialization: list[dict[str, object]] = []
    for cycle in cycles[:8]:
        processes = set(cycle[:-1])
        cycle_streams = [
            str(item["stream"])
            for item in facts
            if isinstance(item, Mapping)
            and set(item["producer_processes"]).intersection(processes)
            and set(item["consumer_processes"]).intersection(processes)
        ]
        initializers: list[str] = []
        # The initializer can be a source process outside the feedback SCC
        # (for example a seed stage).  It is still relevant if it writes into
        # the SCC without first consuming any SCC stream.
        for process in sorted(functions):
            body = functions.get(process, "")
            # A process that can write into the SCC without first consuming an
            # SCC stream is a statically visible initial-token source.  This is
            # deliberately conservative: an unrecognised initialization remains
            # UNKNOWN and is never used to waive the guard.
            writes_cycle_stream = any(
                re.search(rf"\b{re.escape(stream)}\s*\.\s*write\s*\(", body)
                for stream in cycle_streams
            )
            reads_cycle_stream = any(
                re.search(rf"\b{re.escape(stream)}\s*\.\s*read\s*\(", body)
                for stream in cycle_streams
            )
            explicit_same_stream_initialization = any(
                (
                    (first_write := next(
                        iter(re.finditer(
                            rf"\b{re.escape(stream)}\s*\.\s*write\s*\(", body
                        )),
                        None,
                    )) is not None
                    and (first_read := next(
                        iter(re.finditer(
                            rf"\b{re.escape(stream)}\s*\.\s*read\s*\(", body
                        )),
                        None,
                    )) is not None
                    and first_write.start() < first_read.start()
                )
                for stream in cycle_streams
            )
            if writes_cycle_stream and (
                not reads_cycle_stream
                or explicit_same_stream_initialization
            ):
                initializers.append(process)
        cycle_initialization.append(
            {
                "process_cycle": cycle,
                "streams": cycle_streams,
                "initial_token_processes": initializers,
                "initialization": "PRESENT" if initializers else "UNKNOWN",
            }
        )
    fixed_trip_loops = len(re.findall(r"for\s*\([^;]+;[^;]*(?:<|<=)\s*\d+", text))
    explicit_stream_initializers: list[dict[str, str]] = []
    for stream in facts:
        stream_name = str(stream.get("stream", ""))
        if not stream_name:
            continue
        for process, body in functions.items():
            writes = list(re.finditer(rf"\b{re.escape(stream_name)}\s*\.\s*write\s*\(", body))
            reads = list(re.finditer(rf"\b{re.escape(stream_name)}\s*\.\s*read\s*\(", body))
            if writes and reads and writes[0].start() < reads[0].start():
                explicit_stream_initializers.append(
                    {"stream": stream_name, "process": process}
                )
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
        "feedback_initial_token": (
            "PRESENT"
            if (
                any(item["initialization"] == "PRESENT" for item in cycle_initialization)
                or explicit_stream_initializers
            )
            else "UNKNOWN"
        ),
        "cycle_initialization": cycle_initialization,
        "explicit_stream_initializers": explicit_stream_initializers,
        "candidate_changes_stream_topology": "UNKNOWN",
        "topology_confidence": "SOURCE_PATTERN" if streams else "UNKNOWN",
    }


def _rtl_liveness_evidence(evidence: object) -> bool:
    """Recognize bounded public evidence that makes a topology defect urgent."""

    raw = evidence if isinstance(evidence, Mapping) else {}
    facts = failure_facts(raw)
    corpus = " ".join(
        str(raw.get(key, ""))
        for key in ("failure_kind", "phase", "error_summary", "synthesis_error", "cosim_progress")
    ).casefold()
    return (
        facts["last_failure_kind"] in {"DEADLOCK", "RTL_MISMATCH", "TIMEOUT"}
        or any(token in corpus for token in ("deadlock", "no progress", "no_progress", "rtl mismatch"))
    )


def rtl_liveness_obligation(
    *, requires_cosim: bool, evidence: object, source: object
) -> dict[str, object] | None:
    """Create one high-priority, evidence-bound stream liveness obligation.

    It is intentionally independent of task IDs and of A2/A3.  It appears
    only when a task already requires CoSim, the persisted evidence is a
    liveness signal, and the current public source exposes an implicated
    multi-producer stream or dependency cycle.
    """

    facts = structural_facts(source)
    implicated_streams = [
        str(item["stream"])
        for item in facts["streams"]
        if isinstance(item, Mapping) and item.get("possible_multi_producer") is True
    ]
    cycles = facts["stream_dependency_cycles"]
    if not (
        requires_cosim
        and _rtl_liveness_evidence(evidence)
        and (implicated_streams or cycles)
    ):
        return None
    return {
        "schema_version": RTL_LIVENESS_OBLIGATION_SCHEMA,
        "id": "RTL_LIVENESS_STREAM_TOPOLOGY",
        "kind": "rtl_liveness",
        "status": "OPEN",
        "priority": 100,
        "implicated_streams": implicated_streams,
        "dependency_cycles": cycles,
        "planner_requirements": [
            "FIFO depth-only changes do not satisfy this obligation.",
            "Removing one writer while retaining a mutually waiting feedback cycle does not satisfy this obligation.",
            "Eliminate implicated multi-producer streams and uninitialized dependency cycles.",
            "Prefer a unidirectional single-producer/single-consumer stream topology while preserving the top-level interface and functional semantics.",
        ],
        "source_topology": facts,
    }


def _topology_shape(facts: Mapping[str, object]) -> dict[str, object]:
    """Keep only graph structure, excluding FIFO capacity and unrelated code."""

    streams = facts.get("streams")
    return {
        "streams": [
            {
                "stream": item.get("stream"),
                "producers": item.get("producer_processes", []),
                "consumers": item.get("consumer_processes", []),
            }
            for item in streams
            if isinstance(item, Mapping)
        ]
        if isinstance(streams, list)
        else [],
        "dependency_cycles": facts.get("stream_dependency_cycles", []),
    }


def structural_liveness_guard(
    *, parent_source: object, candidate_source: object, obligation: object
) -> dict[str, object]:
    """Reject only candidates that leave an evidence-bound liveness defect open.

    The guard is a static pre-tool admission check, not a general DATAFLOW
    correctness proof.  No obligation means no restriction; ordinary acyclic
    DATAFLOW and cycles with a statically visible initial token remain allowed.
    """

    active = obligation if isinstance(obligation, Mapping) else None
    if not active:
        return {
            "schema_version": STRUCTURAL_GUARD_SCHEMA,
            "eligible": True,
            "reason": "NO_RTL_LIVENESS_OBLIGATION",
        }
    parent = structural_facts(parent_source)
    candidate = structural_facts(candidate_source)
    implicated = {
        str(item) for item in active.get("implicated_streams", [])
        if isinstance(item, str)
    }
    candidate_streams = {
        str(item.get("stream")): item
        for item in candidate["streams"]
        if isinstance(item, Mapping)
    }
    residual_multi_producer = sorted(
        name
        for name in implicated
        if isinstance(candidate_streams.get(name), Mapping)
        and candidate_streams[name].get("possible_multi_producer") is True
    )
    residual_cycle = bool(candidate["has_stream_dependency_cycle"])
    initialized_cycle = any(
        item.get("initialization") == "PRESENT"
        for item in candidate["cycle_initialization"]
        if isinstance(item, Mapping)
    )
    topology_changed = _topology_shape(parent) != _topology_shape(candidate)
    reasons: list[str] = []
    if residual_multi_producer:
        # Keep the former detailed code for existing evidence readers while
        # emitting the short stable reason required by the public frontier.
        reasons.extend(("MULTI_PRODUCER_REMAINS", "IMPLICATED_MULTI_PRODUCER_REMAINS"))
    if residual_cycle and not initialized_cycle:
        reasons.extend((
            "RESIDUAL_UNINITIALIZED_CYCLE",
            "UNINITIALIZED_STREAM_DEPENDENCY_CYCLE_REMAINS",
        ))
    if not topology_changed:
        reasons.extend((
            "DEPTH_ONLY_CHANGE",
            "NO_TOPOLOGY_PROGRESS",
            "DEPTH_ONLY_OR_TOPOLOGY_NEUTRAL_PATCH",
        ))
    return {
        "schema_version": STRUCTURAL_GUARD_SCHEMA,
        "eligible": not reasons,
        "reason": "PASS" if not reasons else "STRUCTURAL_OBLIGATION_UNSATISFIED",
        "violations": reasons,
        "implicated_streams": sorted(implicated),
        "parent_topology": parent,
        "candidate_topology": candidate,
        "topology_changed": topology_changed,
        "candidate_has_explicit_initial_token": (
            candidate.get("feedback_initial_token") == "PRESENT"
        ),
    }


def structural_action_family_frontier(
    *, facts: Mapping[str, object], history: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Return the applicable, evidence-bound frontier for STRUCTURAL_FIX.

    A family is marked attempted when it has a durable proposal experiment,
    including a guard-rejected candidate.  It is *not* treated as validated
    merely because an immutable candidate directory was allocated.
    """

    structure = facts.get("structural_facts")
    structure = structure if isinstance(structure, Mapping) else {}
    streams = structure.get("streams")
    streams = streams if isinstance(streams, list) else []
    has_multi_producer = any(
        isinstance(item, Mapping) and item.get("possible_multi_producer") is True
        for item in streams
    )
    has_balance_issue = any(
        isinstance(item, Mapping)
        and (
            item.get("balance") == "UNBALANCED"
            or item.get("possible_multi_consumer") is True
        )
        for item in streams
    )
    has_cycle = structure.get("has_stream_dependency_cycle") is True
    dataflow = structure.get("dataflow_present") is True
    applicability = {
        "capacity_adjustment": bool(streams),
        "producer_normalization": has_multi_producer,
        "topology_elimination": has_cycle or has_multi_producer,
        "sequential_pipeline_fallback": has_cycle and dataflow,
        "protocol_initialization": has_cycle,
        "stream_balance_repair": has_balance_issue or has_multi_producer,
    }
    attempted: set[str] = set()
    rejected: set[str] = set()
    signatures: dict[str, list[str]] = {name: [] for name in STRUCTURAL_ACTION_FAMILIES}
    for row in history:
        experiment = row.get("proposal_experiment")
        if not isinstance(experiment, Mapping):
            continue
        family = structural_action_family(experiment.get("selected_action_family") or experiment.get("action_family"))
        if family is None:
            continue
        attempted.add(family)
        if str(row.get("status") or row.get("reason") or row.get("rejection_reason") or "").upper() not in {"", "PROMOTED", "PASS"}:
            rejected.add(family)
        observed = row.get("failure_evidence_detail")
        observed_facts = failure_facts(observed) if isinstance(observed, Mapping) else {}
        signature = (
            experiment.get("observed_failure_signature")
            or observed_facts.get("last_failure_signature")
            or experiment.get("input_failure_signature")
        )
        if isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature):
            signatures[family].append(signature)
    untried = [
        family for family in STRUCTURAL_ACTION_FAMILIES
        if applicability[family] and family not in attempted
    ]
    # Prefer a structural mechanism that removes the observed defect, then a
    # conservative sequential fallback.  This is a recommendation, never an
    # instruction to apply a task-specific answer.
    preferred = (
        "topology_elimination" if has_cycle else
        "producer_normalization" if has_multi_producer else
        "stream_balance_repair" if has_balance_issue else
        (untried[0] if untried else None)
    )
    if preferred not in untried:
        preferred = untried[0] if untried else None
    return {
        "schema_version": ACTION_FAMILY_FRONTIER_SCHEMA,
        "attempted_action_families": sorted(attempted),
        "rejected_action_families": sorted(rejected),
        "untried_action_families": untried,
        "family_failure_signatures": {
            family: sorted(set(values)) for family, values in signatures.items() if values
        },
        "recommended_fallback_family": preferred,
        "has_high_value_untried_family": preferred is not None,
        "applicability": applicability,
    }


def semantic_progress_assessment(
    *,
    mode: object,
    facts: Mapping[str, object],
    experiment: object,
    structural_guard: object = None,
    frontier: object = None,
) -> dict[str, object]:
    """Classify one completed/rejected round without equating failure to stasis."""

    if str(mode).upper() != "STRUCTURAL_FIX":
        return {
            "schema_version": SEMANTIC_PROGRESS_SCHEMA,
            "is_semantic_progress": False,
            "reasons": [],
            "mode_scope": "NON_STRUCTURAL_FIX",
        }
    record = experiment if isinstance(experiment, Mapping) else {}
    guard = structural_guard if isinstance(structural_guard, Mapping) else {}
    action = structural_action_family(
        record.get("selected_action_family") or record.get("action_family")
    )
    input_signature = record.get("input_failure_signature")
    current_signature = facts.get("last_failure_signature")
    reasons: list[str] = []
    if isinstance(input_signature, str) and isinstance(current_signature, str) and input_signature != current_signature:
        reasons.append("NEW_FAILURE_SIGNATURE")
    if record.get("frontier_was_untried") is True:
        reasons.append("ACTION_FAMILY_EXCLUDED_OR_TESTED")
    if (
        guard.get("eligible") is False
        and guard.get("violations")
        and record.get("frontier_was_untried") is True
    ):
        reasons.append("STRUCTURAL_GUARD_NEW_UNMET_CONDITION")
    if record.get("expected_topology_delta") and record.get("topology_progress") is True:
        reasons.append("TOPOLOGY_PROGRESS")
    if record.get("hypothesis_falsified") is True:
        reasons.append("HYPOTHESIS_FALSIFIED")
    if record.get("obligation_precision_increased") is True:
        reasons.append("OBLIGATION_ROOT_CAUSE_MORE_PRECISE")
    if isinstance(frontier, Mapping) and frontier.get("has_high_value_untried_family") is True:
        reasons.append("UNTRIED_FALLBACK_REMAINS")
    return {
        "schema_version": SEMANTIC_PROGRESS_SCHEMA,
        "is_semantic_progress": bool(reasons),
        "reasons": sorted(set(reasons)),
        "action_family": action,
        "failure_signature": current_signature if isinstance(current_signature, str) else None,
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
    action_family_frontier: object = None,
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
    declared_family = canonical_action_family(
        getattr(proposal, "selected_action_family", "")
        or getattr(proposal, "action_family", "")
    )
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
        "selected_action_family": _text(
            getattr(proposal, "selected_action_family", ""), limit=120
        ) or family,
        "selected_hypothesis": _text(
            getattr(proposal, "selected_hypothesis", ""), limit=400
        ) or hypothesis,
        "hypotheses_considered": [
            _text(item, limit=240)
            for item in getattr(proposal, "hypotheses_considered", ())
            if _text(item, limit=240)
        ][:6],
        "complete_obligation_requirements": [
            _text(item, limit=240)
            for item in getattr(proposal, "complete_obligation_requirements", ())
            if _text(item, limit=240)
        ][:8],
        "expected_topology_delta": _text(
            getattr(proposal, "expected_topology_delta", ""), limit=320
        ) or "NOT_DECLARED",
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
    frontier = action_family_frontier if isinstance(action_family_frontier, Mapping) else {}
    structural_family = structural_action_family(record["selected_action_family"])
    record["structural_action_family"] = structural_family
    record["frontier_was_untried"] = bool(
        structural_family
        and structural_family in frontier.get("untried_action_families", [])
    )
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
    requires_cosim: bool = False,
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
        family = canonical_action_family(
            experiment.get("selected_action_family") or experiment.get("action_family")
        )
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
        family = canonical_action_family(
            experiment.get("selected_action_family") or experiment.get("action_family")
        )
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
    raw_evidence = evidence if isinstance(evidence, Mapping) else {}
    structure = structural_facts(source)
    liveness_obligation = rtl_liveness_obligation(
        requires_cosim=requires_cosim,
        evidence=raw_evidence,
        source=source,
    )
    topology_attempts = [
        item.get("proposal_experiment", {}).get("changes_stream_topology")
        for item in history
        if isinstance(item.get("proposal_experiment"), Mapping)
    ]
    if topology_attempts:
        structure["candidate_changes_stream_topology"] = bool(topology_attempts[-1])
    structural_frontier = structural_action_family_frontier(
        facts={"structural_facts": structure}, history=history
    ) if str(mode).upper() == "STRUCTURAL_FIX" else None
    chosen_action = continuation_action(
        facts=facts,
        attempted=attempted,
        baseline_id=baseline_id,
        incumbent_id=incumbent_id,
        active_probe_id=active_probe_id,
        final_reserve_only=final_reserve_only,
        action_family_frontier=structural_frontier,
        structural_guard_rejected=(
            str(facts.get("last_failure_kind")) == "DEADLOCK"
            and isinstance(raw_evidence.get("structural_guard"), Mapping)
            and raw_evidence["structural_guard"].get("eligible") is False
        ),
    )
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
    if liveness_obligation is not None:
        obligations.append(
            {
                "schema_version": OBLIGATION_STATE_SCHEMA,
                "id": liveness_obligation["id"],
                "kind": liveness_obligation["kind"],
                "status": liveness_obligation["status"],
                "priority": liveness_obligation["priority"],
                "evidence_refs": list(obligation_record["evidence_refs"]),
                "affected_regions": list(liveness_obligation["implicated_streams"]),
            }
        )
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
    # A terminal result with the same input signature falsifies the previously
    # attempted transformation family.  The next live Planner request is
    # therefore explicitly constrained to a different family.  For a visible
    # stream dependency cycle after a FIFO-capacity attempt, the only useful
    # next experiment is a topology/order change; another FIFO depth tweak is
    # neither new evidence nor a new repair mechanism.
    attempted_families = sorted(
        {
            family
            for item in attempted
            if isinstance((family := item.get("action_family")), str) and family
        }
    )
    require_distinct_family = bool(
        facts["terminal_failure"]
        and attempted_families
        and not new_evidence_since_last_planner
    )
    required_action_family: str | None = None
    if (
        require_distinct_family
        and structure["has_stream_dependency_cycle"]
        and "FIFO_CAPACITY_OR_PROTOCOL" in attempted_families
    ):
        required_action_family = "DATAFLOW_TOPOLOGY_OR_ORDER"
        required_change = "REQUIRE_STREAM_TOPOLOGY_OR_VERIFIED_FALLBACK"
    elif require_distinct_family:
        required_change = "REQUIRE_DISTINCT_ACTION_FAMILY_OR_VERIFIED_FALLBACK"
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
        "forbidden_action_families": attempted_families if require_distinct_family else [],
        "required_action_family": required_action_family,
        "recommended_continuation_action": chosen_action,
        "structural_facts": structure,
        "rtl_liveness_obligation": liveness_obligation,
        "action_family_frontier": structural_frontier,
    }


def is_repeated_terminal_experiment(
    experiment: Mapping[str, object], history: Sequence[Mapping[str, object]], *,
    terminal_failure: bool,
    forbidden_action_families: Sequence[object] = (),
    required_action_family: object = None,
) -> bool:
    """Reject a terminal-repeat or a controller-forbidden action family.

    A different transformation family can test the same suspected cause, and
    a different hypothesis can use the same safe family.  Treating either
    dimension alone as a duplicate would block useful, falsifiable follow-up
    experiments.  Repeating both is the non-informative retry this guard
    prevents.
    """

    if not terminal_failure:
        return False
    family = canonical_action_family(experiment.get("action_family"))
    if not family:
        return False
    forbidden = {
        canonical_action_family(item)
        for item in forbidden_action_families
        if canonical_action_family(item)
    }
    required = canonical_action_family(required_action_family)
    if family in forbidden or (required and family != required):
        return True
    hypothesis = _text(experiment.get("hypothesis"), limit=240).casefold()
    if not hypothesis:
        return False
    for row in history:
        previous = row.get("proposal_experiment")
        if (
            isinstance(previous, Mapping)
            and canonical_action_family(previous.get("action_family")) == family
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
    "canonical_action_family",
    "continuation_action",
    "failure_facts",
    "is_repeated_terminal_experiment",
    "obligation_state",
    "primary_obligation",
    "proposal_experiment",
    "rtl_liveness_obligation",
    "search_control_state",
    "structural_liveness_guard",
    "structural_facts",
]
