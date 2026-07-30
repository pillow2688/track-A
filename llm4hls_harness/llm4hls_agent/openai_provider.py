"""OpenAI-compatible repair provider with a strict, auditable response contract."""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Mapping

from .budget import validate_token_envelope
from .optimization import OptimizationContext
from .repair import PatchProposal, RepairContext, RepairProviderError


DEFAULT_MODEL = "deepseek-v4-pro"
FAST_EXPERIMENT_RESPONSE_SCHEMA = "v3b.fast-planner-response.v1"
TASK_AWARE_RESPONSE_SCHEMA = "v3c.task-aware-planner-response.v1"
FAST_EXPERIMENT_STRATEGIES = (
    "ARRAY_PARTITION",
    "MEMORY_PARTITION",
    "LOOP_UNROLL",
    "PAR_FACTOR_TUNING",
    "MULTI_PARTIAL_SUM",
    "PARALLEL_REDUCTION",
    "MEMORY_BANKING",
    "LOCAL_LOOP_RESTRUCTURE",
    "LOOP_PIPELINE",
    "DATAFLOW",
    "STREAMING",
    "BITWIDTH_OPTIMIZATION",
)
FAST_EXPERIMENT_SYSTEM_PROMPT = (
    "You are an AMD Vitis HLS optimization Planner. Use only the supplied public "
    "kernel, read-only headers, task description, and structured synthesis evidence. "
    "Propose exactly one Candidate that preserves numerical semantics, the top function "
    "signature, and all interfaces. Never use or modify tests, hidden/reference files, "
    "headers, metadata, budgets, tool policy, Candidate promotion, or final selection. "
    "Return strict JSON only. Its patch must be a directly applicable single-file unified "
    "diff for the current kernel, with exact hunk start positions and exact old/new line "
    "counts. Never claim that a tool ran or that a Candidate passed validation."
)
TASK_AWARE_SYSTEM_PROMPT = (
    "You are one task-aware AMD Vitis HLS Planner. The supplied mode is an "
    "authoritative routing decision made by deterministic code. Propose exactly one "
    "minimal kernel-only repair Patch for that mode, using only the supplied public "
    "kernel, read-only headers, task description, and bounded failure evidence. "
    "Preserve numerical semantics except for the diagnosed functional defect, preserve "
    "the top signature and interfaces, and never modify tests, headers, metadata, "
    "budgets, tool policy, Candidate promotion, or final selection. Return strict JSON "
    "only. Never claim that a tool ran or that a Candidate passed validation."
)
TASK_AWARE_CHANGE_CLASS = {
    "REPAIR": "FUNCTIONAL_REPAIR",
    "SYNTH_FIX": "SYNTHESIS_REPAIR",
    "STRUCTURAL_FIX": "STRUCTURAL_REPAIR",
}
_UNIFIED_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,\d+)? "
    r"\+(?P<new_start>\d+)(?:,\d+)? @@(?P<suffix>.*)$"
)
TRUNCATION_REASONS = frozenset(
    {
        "NOT_TRUNCATED",
        "PROVIDER_LENGTH_LIMIT",
        "JSON_INCOMPLETE",
        "PATCH_INCOMPLETE",
        "CONTEXT_LIMIT",
        "UNKNOWN_TRUNCATION",
    }
)


def _token_budget_prompt(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    envelope = validate_token_envelope(value)
    pressure = str(envelope["token_pressure"])
    focus = {
        "LOW": "Use the strongest evidence-backed strategy bundle permitted by the task.",
        "MEDIUM": "Focus on one or two strongly supported strategies and keep explanations concise.",
        "HIGH": "Use at most one high-confidence local strategy; do not perform an unsupported large rewrite.",
        "CRITICAL": "Do not expand scope; the Harness normally blocks this call unless a viable output remains.",
    }[pressure]
    return "\n".join(
        [
            "TOKEN BUDGET",
            f"- Remaining run tokens: {envelope['tokens_remaining']}",
            "- Estimated input tokens for this request: "
            + str(envelope["estimated_input_tokens"]),
            "- Maximum output for this request: "
            + str(envelope["effective_max_output_tokens"]),
            f"- Remaining Planner rounds: {envelope['rounds_remaining']}",
            "- Reserved tokens for later rounds: "
            + str(envelope["future_round_token_reserve"]),
            f"- Token pressure: {pressure}",
            "OUTPUT GUIDANCE",
            "- Return one valid JSON object.",
            "- Keep hypothesis and expected_effect concise.",
            "- Prefer one focused and locally applicable patch.",
            "- Do not rewrite the full kernel when a local diff is sufficient.",
            "- The output may be rejected if JSON or unified diff is incomplete.",
            "- " + focus,
        ]
    )


def _diff_looks_complete(patch: object) -> bool:
    if not isinstance(patch, str) or not patch.strip():
        return False
    lines = patch.splitlines()
    if not any(line.startswith("--- ") for line in lines) or not any(
        line.startswith("+++ ") for line in lines
    ):
        return False
    index = 0
    found_hunk = False
    while index < len(lines):
        match = _UNIFIED_HUNK_HEADER.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        found_hunk = True
        header = re.fullmatch(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*",
            lines[index],
        )
        if header is None:
            return False
        expected_old = int(header.group(2) or 1)
        expected_new = int(header.group(4) or 1)
        old_count = 0
        new_count = 0
        changed_count = 0
        index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith((" ", "-")) and not line.startswith("--- "):
                old_count += 1
            if line.startswith((" ", "+")) and not line.startswith("+++ "):
                new_count += 1
            if (
                line.startswith(("-", "+"))
                and not line.startswith(("--- ", "+++ "))
            ):
                changed_count += 1
            index += 1
        # Wrong hunk counts are a common, fully recoverable model formatting
        # error handled by _normalize_unified_diff_hunk_counts.  Treat only an
        # empty/abrupt hunk as truncation here; Patch policy remains the final
        # authority for all other malformed diffs.
        if (old_count == 0 and new_count == 0) or changed_count == 0:
            return False
    return found_hunk


def classify_output_truncation(
    content: str,
    finish_reason: str | None,
    *,
    required_fields: tuple[str, ...] = ("patch",),
) -> str:
    """Classify incomplete model output before Candidate materialization."""

    if finish_reason == "length":
        return "PROVIDER_LENGTH_LIMIT"
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        stripped = content.rstrip()
        if stripped.startswith("{") and not stripped.endswith("}"):
            return "JSON_INCOMPLETE"
        return "UNKNOWN_TRUNCATION"
    if not isinstance(value, Mapping):
        return "UNKNOWN_TRUNCATION"
    if any(field not in value for field in required_fields):
        return "UNKNOWN_TRUNCATION"
    if "patch" in value and not _diff_looks_complete(value.get("patch")):
        return "PATCH_INCOMPLETE"
    return "NOT_TRUNCATED"


def _normalize_unified_diff_hunk_counts(patch: str) -> str:
    """Repair only hunk counts; the existing Patch Validator remains authoritative."""

    lines = patch.splitlines()
    index = 0
    changed = False
    while index < len(lines):
        match = _UNIFIED_HUNK_HEADER.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        end = index + 1
        old_count = 0
        new_count = 0
        while end < len(lines) and not lines[end].startswith("@@ "):
            line = lines[end]
            if line.startswith((" ", "-")):
                old_count += 1
            if line.startswith((" ", "+")):
                new_count += 1
            end += 1
        normalized = (
            f"@@ -{match.group('old_start')},{old_count} "
            f"+{match.group('new_start')},{new_count} @@{match.group('suffix')}"
        )
        changed = changed or normalized != lines[index]
        lines[index] = normalized
        index = end
    if not changed:
        return patch
    return "\n".join(lines) + ("\n" if patch.endswith("\n") else "")


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0
    max_output_tokens: int = 1000
    temperature: float = 0.0
    top_p: float | None = None

    def __post_init__(self) -> None:
        base = self.base_url.rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise ValueError("OpenAI-compatible base URL must use http or https")
        parsed = urllib.parse.urlsplit(base)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "OpenAI-compatible base URL must not contain userinfo, query, or fragment"
            )
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for the API repair provider")
        if not self.model:
            raise ValueError("LLM4HLS_MODEL must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("LLM timeout must be finite and positive")
        if self.max_output_tokens <= 0:
            raise ValueError("LLM max output tokens must be positive")
        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ValueError("LLM temperature must be finite and non-negative")
        if self.top_p is not None and (
            not math.isfinite(self.top_p) or not 0 < self.top_p <= 1
        ):
            raise ValueError("LLM top_p must be finite and in (0,1]")
        object.__setattr__(self, "base_url", base)

    @property
    def chat_completions_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"


Transport = Callable[[urllib.request.Request, float], tuple[int, Mapping[str, str], bytes]]


def _default_transport(
    request: urllib.request.Request, timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        # An HTTP failure does not provide a trustworthy provider-side usage
        # receipt.  It must stay non-replayable and conservatively accounted,
        # rather than being misclassified as a zero-token completed action.
        raise RepairProviderError(
            f"OpenAI-compatible API returned HTTP {exc.code}",
            usage_complete=False,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RepairProviderError(
            f"OpenAI-compatible API request failed ({type(exc).__name__})",
            usage_complete=False,
        ) from exc


def build_repair_prompt(context: RepairContext) -> str:
    failure = {
        "stage": context.stage,
        "phase": context.phase,
        "code": context.diagnostic_code,
        "summary": context.summary,
        "evidence": list(context.evidence),
    }
    constraints = {
        "top": context.top,
        "kernel_file": context.kernel_name,
        "part": context.part,
        "clock_ns": context.clock_ns,
        "initial_condition": context.initial_condition,
        "rules": [
            "preserve the top function name, signature, argument order, types, and semantics",
            "modify only the named kernel .cpp file",
            "do not modify headers, tests, metadata, assertions, hidden files, or reference files",
        ],
    }
    budget = {
        "remaining_tokens": context.remaining_tokens,
        "remaining_credits": context.remaining_credits,
    }
    return "\n".join(
        [
            "ROLE\nYou are a Vitis HLS C/C++ repair agent.",
            "OBJECTIVE\nRepair the current failure with one minimal patch while preserving the interface and numerical semantics.",
            "STRUCTURED FAILURE\n" + json.dumps(failure, sort_keys=True),
            "RELEVANT SOURCE\n" + context.source_excerpt,
            "CONSTRAINTS\n" + json.dumps(constraints, sort_keys=True),
            "BUDGET\n" + json.dumps(budget, sort_keys=True),
            "OUTPUT\nReturn exactly one JSON object with hypothesis, change_class, expected_effect, risk, required_validation, and patch. The patch must be one unified diff. Do not use Markdown fences or output any other text.",
        ]
    )


def build_optimization_prompt(context: OptimizationContext) -> str:
    score_policy = (
        {
            "kind": "PUBLIC_OFFICIAL_SCORE_PROXY",
            "difficulty": context.difficulty,
            "primary_metric": "synthesis latency.worst",
            "acceleration": "baseline_latency / candidate_latency",
            "acceleration_cap": context.official_acceleration_cap,
            "formula": (
                "if functional == 0: 0; else: difficulty * (0.5 + "
                "0.2*synthesizable + 0.3*min(acceleration, acceleration_cap)"
                "/acceleration_cap)"
            ),
            "current_proxy_score": context.current_official_score,
            "note": (
                "The local value is a public-validation proxy; hidden correctness "
                "is unknown. Reduce worst-case synthesis latency without breaking "
                "CSim, Synth, CoSim, clock, or resource constraints. II and resources "
                "do not directly increase the official score."
            ),
        }
        if context.official_score_enabled
        else {
            "kind": "INTERNAL_PPA",
            "note": "Minimize the configured internal latency, II, and resource cost.",
        }
    )
    objective = {
        "task_id": context.task_id,
        "parent_candidate_id": context.parent_candidate_id,
        "round_index": context.round_index,
        "allowed_optimization_class": context.allowed_optimization_class,
        "bottleneck": context.bottleneck,
        "evidence": list(context.evidence),
        "baseline_metrics": dict(context.baseline_metrics),
        "current_metrics": dict(context.current_metrics),
        "current_validation": dict(context.current_validation),
        "current_clock_constraint": dict(context.current_clock_constraint),
        "failed_actions": [dict(item) for item in context.failed_actions],
        "hls_rules": list(context.hls_rules),
        "score_policy": score_policy,
    }
    constraints = {
        "top": context.top,
        "kernel_file": context.kernel_name,
        "part": context.part,
        "clock_ns": context.clock_ns,
        "rules": [
            "preserve the top function name, signature, argument order, types, interfaces, and numerical semantics",
            "modify only the named kernel .cpp file",
            "implement exactly the one allowed optimization class",
            "do not modify headers, tests, metadata, assertions, hidden files, or reference files",
        ],
    }
    budget = {
        "remaining_tokens": context.remaining_tokens,
        "remaining_credits": context.remaining_credits,
        "search_closeout_reserve_credits": context.search_closeout_reserve_credits,
    }
    role = (
        "You are a Vitis HLS official-score optimization agent."
        if context.official_score_enabled
        else "You are a Vitis HLS internal-PPA optimization agent."
    )
    objective_text = (
        "Improve the official public proxy score with one minimal Patch while "
        "preserving correctness and the interface."
        if context.official_score_enabled
        else "Improve the internal PPA score with one minimal Patch while preserving "
        "correctness and the interface."
    )
    return "\n".join(
        [
            "ROLE\n" + role,
            "OBJECTIVE\n" + objective_text,
            "OPTIMIZATION\n" + json.dumps(objective, sort_keys=True),
            "RELEVANT SOURCE\n" + context.source_excerpt,
            "CONSTRAINTS\n" + json.dumps(constraints, sort_keys=True),
            "BUDGET\n" + json.dumps(budget, sort_keys=True),
            "OUTPUT\nReturn exactly one JSON object with hypothesis, optimization_class, expected_effect, risk, required_validation, and patch. The optimization_class must equal the allowed class. required_validation must contain csim, synth, and cosim. The patch must be one unified diff. Do not use Markdown fences or output any other text.",
        ]
    )


def build_fast_experiment_prompt(context: Mapping[str, object]) -> str:
    """Build one bounded autonomous optimization request for V3-B experiments."""

    required = {
        "objective",
        "task",
        "current_kernel",
        "description",
        "read_only_headers",
        "synth_evidence",
        "recent_failures",
        "attempted_strategies",
        "budget",
        "constraints",
    }
    optional = {
        "experience_guidance",
        "token_budget",
        "_effective_max_output_tokens",
    }
    if not required.issubset(context) or set(context).difference(required | optional):
        raise ValueError("fast Planner context has an invalid field set")
    experience_guidance = context.get("experience_guidance")
    if experience_guidance is not None:
        if not isinstance(experience_guidance, Mapping):
            raise ValueError("experience_guidance must be an object")
        if len(
            json.dumps(
                experience_guidance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ) > 4_800:
            raise ValueError("experience_guidance exceeds the bounded context limit")
    recent_failures = context.get("recent_failures", [])
    if not isinstance(recent_failures, list):
        raise ValueError("recent_failures must be an array")
    if len(
        json.dumps(
            recent_failures,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > 8_000:
        raise ValueError("recent_failures exceeds the bounded context limit")
    objective = {
        "goal": context["objective"],
        "task": context["task"],
        "synth_evidence": context["synth_evidence"],
        "recent_failures": context["recent_failures"],
        "attempted_strategies": context["attempted_strategies"],
        "budget": context["budget"],
    }
    response_contract = {
        "hypothesis": "non-empty string",
        "primary_bottleneck": "non-empty string",
        "evidence_used": ["one or more concise evidence facts"],
        "strategy_bundle": ["one to three allowed strategies"],
        "expected_effect": "non-empty string",
        "risk": {
            "level": "LOW|MEDIUM|HIGH",
            "dimensions": ["zero or more concise risk dimensions"],
        },
        "patch": "one unified diff",
    }
    return "\n".join(
        [
            "ROLE\nYou are an autonomous AMD Vitis HLS optimization Planner.",
            (
                "OBJECTIVE\nPropose one evidence-backed strategy bundle and one "
                "minimal unified diff that strictly reduces worst-case synthesis "
                "latency while preserving numerical semantics and the public interface."
            ),
            "STRUCTURED STATE\n"
            + json.dumps(objective, ensure_ascii=False, sort_keys=True),
            "CURRENT BEST KERNEL\n" + str(context["current_kernel"]),
            "PUBLIC TASK DESCRIPTION\n" + str(context["description"]),
            "READ-ONLY HEADERS\n"
            + json.dumps(
                context["read_only_headers"], ensure_ascii=False, sort_keys=True
            ),
            "CONSTRAINTS\n"
            + json.dumps(context["constraints"], ensure_ascii=False, sort_keys=True),
            *(
                [
                    "EXPERIENCE GUIDANCE (ADVISORY ONLY)\n"
                    + json.dumps(
                        experience_guidance,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ]
                if experience_guidance is not None
                else []
            ),
            "ALLOWED STRATEGIES\n" + json.dumps(FAST_EXPERIMENT_STRATEGIES),
            (
                "HLS EVIDENCE RULES\nTop-level transaction interval is the interval "
                "between complete top-function transactions; it is not loop achieved "
                "II. Use loop.pipeline_ii only for loop II. When the critical loop is "
                "already pipelined with achieved II=1, do not return a PIPELINE-only "
                "patch. For dotProduct, prioritize memory banking/ARRAY_PARTITION, "
                "bounded LOOP_UNROLL or PAR_FACTOR parallelism, multiple partial sums, "
                "and tree/parallel reduction to remove serial accumulation."
            ),
            (
                "AUTHORITY\nYou may propose strategies and a Patch only. You may not "
                "approve tools, change budgets, promote a Candidate, select final, or "
                "modify headers/tests/metadata/interfaces."
            ),
            *(
                [_token_budget_prompt(context["token_budget"])]
                if "token_budget" in context
                else []
            ),
            (
                "PATCH VALIDITY\nThe unified diff must apply directly to the supplied "
                "current kernel. Before returning, recount every hunk: context and '-' "
                "lines equal the old count; context and '+' lines equal the new count. "
                "Use only the kernel filename in ---/+++ headers."
            ),
            "OUTPUT SCHEMA\n"
            + json.dumps(response_contract, ensure_ascii=False, sort_keys=True)
            + "\nReturn exactly this JSON object and no Markdown fences or commentary.",
        ]
    )


def build_guided_optimization_prompt(
    context: OptimizationContext,
    experience_guidance: Mapping[str, object],
) -> str:
    """Add bounded advisory history to the legacy optimization prompt."""

    if not isinstance(experience_guidance, Mapping):
        raise ValueError("experience_guidance must be an object")
    rendered = json.dumps(
        experience_guidance,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered.encode("utf-8")) > 4_800:
        raise ValueError("experience_guidance exceeds the bounded context limit")
    prompt = build_optimization_prompt(context)
    marker = "\nOUTPUT\n"
    if marker not in prompt:
        raise ValueError("optimization prompt has no output boundary")
    advisory = (
        "\nEXPERIENCE GUIDANCE (ADVISORY ONLY)\n"
        + rendered
        + "\nUse this only as historical evidence. Current synth evidence and all "
        "Harness gates remain authoritative."
    )
    return prompt.replace(marker, advisory + marker, 1)


def build_budgeted_optimization_prompt(
    context: OptimizationContext,
    token_envelope: Mapping[str, object],
    *,
    experience_guidance: Mapping[str, object] | None = None,
) -> str:
    prompt = (
        build_guided_optimization_prompt(context, experience_guidance)
        if experience_guidance is not None
        else build_optimization_prompt(context)
    )
    marker = "\nOUTPUT\n"
    if marker not in prompt:
        raise ValueError("optimization prompt has no output boundary")
    return prompt.replace(marker, "\n" + _token_budget_prompt(token_envelope) + marker, 1)


def build_task_aware_prompt(context: Mapping[str, object]) -> str:
    """Build one bounded repair request for the deterministic PhaseRouter mode."""

    required = {
        "mode",
        "task",
        "current_kernel",
        "description",
        "read_only_headers",
        "failure_evidence",
        "budget",
        "constraints",
    }
    optional = {
        "experience_guidance",
        "recent_failures",
        "search_control",
        "code_slice",
        "token_budget",
        "_effective_max_output_tokens",
    }
    if not required.issubset(context) or set(context).difference(required | optional):
        raise ValueError("task-aware Planner context has an invalid field set")
    experience_guidance = context.get("experience_guidance")
    if experience_guidance is not None:
        if not isinstance(experience_guidance, Mapping):
            raise ValueError("experience_guidance must be an object")
        if len(
            json.dumps(
                experience_guidance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ) > 4_800:
            raise ValueError("experience_guidance exceeds the bounded context limit")
    recent_failures = context.get("recent_failures", [])
    if not isinstance(recent_failures, list):
        raise ValueError("recent_failures must be an array")
    if len(
        json.dumps(
            recent_failures,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > 8_000:
        raise ValueError("recent_failures exceeds the bounded context limit")
    search_control = context.get("search_control")
    if search_control is not None and not isinstance(search_control, Mapping):
        raise ValueError("search_control must be an object")
    code_slice = context.get("code_slice")
    if code_slice is not None and not isinstance(code_slice, Mapping):
        raise ValueError("code_slice must be an object")
    mode = str(context["mode"])
    if mode not in TASK_AWARE_CHANGE_CLASS:
        raise ValueError("task-aware Planner mode is unsupported")
    objectives = {
        "REPAIR": (
            "Repair the observed CSim compile/runtime/functional failure while "
            "preserving every unrelated behavior."
        ),
        "SYNTH_FIX": (
            "Make the kernel synthesizable and satisfy the reported clock/resource "
            "constraint without changing its C-level behavior or interface."
        ),
        "STRUCTURAL_FIX": (
            "Repair the observed C/RTL structural failure, focusing on DATAFLOW, "
            "hls::stream, FIFO ordering/depth, and interface behavior."
        ),
    }
    response_contract = {
        "target_obligation": "must equal SEARCH CONTROL primary_obligation",
        "hypothesis": "non-empty string",
        "action_family": "non-empty concise transformation family",
        "action_parameters": {"zero or more compact scalar parameters": "value"},
        "validation_plan": ["ordered stages selected from csim, synth, cosim"],
        "primary_failure": "non-empty string",
        "evidence_used": ["one or more concise supplied evidence facts"],
        "change_class": TASK_AWARE_CHANGE_CLASS[mode],
        "expected_effect": "non-empty string",
        "failure_criteria": "non-empty statement of what falsifies the hypothesis",
        "fallback": "non-empty safe fallback strategy or ABSTAIN",
        "risk": {
            "level": "LOW|MEDIUM|HIGH",
            "dimensions": ["zero or more concise risk dimensions"],
        },
        "patch": "one directly applicable unified diff",
    }
    return "\n".join(
        [
            "ROLE\nYou are the repair-capable mode of one task-aware AMD Vitis HLS Planner.",
            "MODE\n" + mode,
            "OBJECTIVE\n" + objectives[mode],
            "PUBLIC TASK\n"
            + json.dumps(context["task"], ensure_ascii=False, sort_keys=True),
            "CURRENT KERNEL RELEVANT SLICE\n"
            + str(context["current_kernel"])
            + (
                "\nSLICE COORDINATES\n"
                + json.dumps(code_slice, ensure_ascii=False, sort_keys=True)
                + "\nThe bracketed ORIGINAL SOURCE LINES / OMITTED markers are "
                "context metadata, not code. Diff hunk coordinates must refer to "
                "the original kernel; do not copy markers into the patch."
                if code_slice is not None
                else ""
            ),
            "PUBLIC TASK DESCRIPTION\n" + str(context["description"]),
            "READ-ONLY HEADERS\n"
            + json.dumps(
                context["read_only_headers"], ensure_ascii=False, sort_keys=True
            ),
            "BOUNDED FAILURE EVIDENCE\n"
            + json.dumps(
                context["failure_evidence"], ensure_ascii=False, sort_keys=True
            ),
            *(
                [
                    "RECENT REJECTED CANDIDATE FAILURES\n"
                    + json.dumps(
                        recent_failures,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ]
                if recent_failures
                else []
            ),
            *(
                [
                    "SEARCH CONTROL (MANDATORY)\n"
                    + json.dumps(search_control, ensure_ascii=False, sort_keys=True)
                    + "\nYour hypothesis/action must satisfy required_next_change. "
                    "Do not repeat an attempted action_family after a terminal "
                    "failure; use the verified fallback parent when the control "
                    "contract requires it."
                ]
                if search_control
                else []
            ),
            "BUDGET SUMMARY\n"
            + json.dumps(context["budget"], ensure_ascii=False, sort_keys=True),
            "CONSTRAINTS\n"
            + json.dumps(context["constraints"], ensure_ascii=False, sort_keys=True),
            *(
                [
                    "EXPERIENCE GUIDANCE (ADVISORY ONLY)\n"
                    + json.dumps(
                        experience_guidance,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ]
                if experience_guidance is not None
                else []
            ),
            *(
                [_token_budget_prompt(context["token_budget"])]
                if "token_budget" in context
                else []
            ),
            (
                "PATCH VALIDITY\nThe unified diff must apply directly to the supplied "
                "CURRENT KERNEL and target only its filename. Rejected Candidate "
                "patches are not inherited. You MUST address every independent, "
                "evidence-backed blocker from both BOUNDED FAILURE EVIDENCE and "
                "RECENT REJECTED CANDIDATE FAILURES that is still present in CURRENT "
                "KERNEL; do not fix only the newest blocker. If the blockers cannot "
                "be repaired together without changing semantics, explain that risk "
                "in the structured risk fields, but still return one coherent patch. "
                "Keep the change minimal. Before returning, recount every hunk "
                "old/new line count exactly."
            ),
            "OUTPUT SCHEMA\n"
            + json.dumps(response_contract, ensure_ascii=False, sort_keys=True)
            + "\nReturn exactly this JSON object and no Markdown fences or commentary.",
        ]
    )


def _strict_task_aware_response(
    content: object, *, mode: str
) -> dict[str, object]:
    if mode not in TASK_AWARE_CHANGE_CLASS:
        raise RepairProviderError("task-aware Planner mode is unsupported")
    if not isinstance(content, str) or not content.strip():
        raise RepairProviderError("task-aware Planner response content is empty")
    try:
        value = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise RepairProviderError(
            "task-aware Planner response is not strict JSON"
        ) from exc
    required = {
        "target_obligation",
        "hypothesis",
        "action_family",
        "action_parameters",
        "validation_plan",
        "primary_failure",
        "evidence_used",
        "change_class",
        "expected_effect",
        "failure_criteria",
        "fallback",
        "risk",
        "patch",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RepairProviderError(
            "task-aware Planner response has an invalid field set"
        )
    for name in (
        "hypothesis",
        "target_obligation",
        "action_family",
        "primary_failure",
        "expected_effect",
        "failure_criteria",
        "fallback",
        "patch",
    ):
        if not isinstance(value[name], str) or not str(value[name]).strip():
            raise RepairProviderError(f"task-aware Planner field {name} is empty")
    if value["change_class"] != TASK_AWARE_CHANGE_CLASS[mode]:
        raise RepairProviderError(
            "task-aware Planner change_class does not match mode"
        )
    if (
        not isinstance(value["action_parameters"], dict)
        or len(value["action_parameters"]) > 12
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(item, (str, int, float, bool, type(None)))
            or isinstance(item, str) and len(item) > 320
            for key, item in value["action_parameters"].items()
        )
    ):
        raise RepairProviderError("task-aware Planner action_parameters is invalid")
    validation_plan = value["validation_plan"]
    if (
        not isinstance(validation_plan, list)
        or not validation_plan
        or any(item not in {"csim", "synth", "cosim"} for item in validation_plan)
        or len(validation_plan) != len(set(validation_plan))
        or validation_plan[:2] != ["csim", "synth"]
    ):
        raise RepairProviderError(
            "task-aware Planner validation_plan must begin with csim,synth"
        )
    evidence = value["evidence_used"]
    if (
        not isinstance(evidence, list)
        or not 1 <= len(evidence) <= 8
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
    ):
        raise RepairProviderError(
            "task-aware Planner evidence_used must contain 1-8 strings"
        )
    risk = value["risk"]
    if not isinstance(risk, dict) or set(risk) != {"level", "dimensions"}:
        raise RepairProviderError("task-aware Planner risk has an invalid field set")
    dimensions = risk.get("dimensions")
    if risk.get("level") not in {"LOW", "MEDIUM", "HIGH"} or (
        not isinstance(dimensions, list)
        or len(dimensions) > 8
        or any(not isinstance(item, str) or not item.strip() for item in dimensions)
    ):
        raise RepairProviderError("task-aware Planner risk is invalid")
    patch = str(value["patch"])
    if "```" in patch or "--- " not in patch or "+++ " not in patch:
        raise RepairProviderError(
            "task-aware Planner patch must be one unified diff"
        )
    return value


def _strict_fast_experiment_response(content: object) -> dict[str, object]:
    if not isinstance(content, str) or not content.strip():
        raise RepairProviderError("fast Planner response content is empty")
    try:
        value = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise RepairProviderError("fast Planner response is not strict JSON") from exc
    required = {
        "hypothesis",
        "primary_bottleneck",
        "evidence_used",
        "strategy_bundle",
        "expected_effect",
        "risk",
        "patch",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RepairProviderError("fast Planner response has an invalid field set")
    for name in (
        "hypothesis",
        "primary_bottleneck",
        "expected_effect",
        "patch",
    ):
        if not isinstance(value[name], str) or not str(value[name]).strip():
            raise RepairProviderError(f"fast Planner field {name} is empty")
    evidence = value["evidence_used"]
    if (
        not isinstance(evidence, list)
        or not 1 <= len(evidence) <= 12
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
    ):
        raise RepairProviderError("fast Planner evidence_used must contain 1-12 strings")
    bundle = value["strategy_bundle"]
    if (
        not isinstance(bundle, list)
        or not 1 <= len(bundle) <= 3
        or any(item not in FAST_EXPERIMENT_STRATEGIES for item in bundle)
        or len(bundle) != len(set(bundle))
    ):
        raise RepairProviderError(
            "fast Planner strategy_bundle must contain 1-3 unique allowed strategies"
        )
    risk = value["risk"]
    if not isinstance(risk, dict) or set(risk) != {"level", "dimensions"}:
        raise RepairProviderError("fast Planner risk has an invalid field set")
    dimensions = risk.get("dimensions")
    if risk.get("level") not in {"LOW", "MEDIUM", "HIGH"} or (
        not isinstance(dimensions, list)
        or len(dimensions) > 8
        or any(not isinstance(item, str) or not item.strip() for item in dimensions)
    ):
        raise RepairProviderError("fast Planner risk is invalid")
    patch = str(value["patch"])
    if "```" in patch or "--- " not in patch or "+++ " not in patch:
        raise RepairProviderError("fast Planner patch must be one unified diff")
    return value


def _strict_response(
    content: object, *, class_field: str = "change_class"
) -> dict[str, object]:
    if not isinstance(content, str) or not content.strip():
        raise RepairProviderError("provider response content is empty")
    text = content.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Providers sometimes add a short explanation or Markdown fences even
        # when JSON mode is requested. Extract only the outer JSON object; all
        # schema and patch checks below remain strict.
        if "```" in text:
            text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RepairProviderError("provider response is not strict JSON")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RepairProviderError("provider response is not strict JSON") from exc
    if not isinstance(value, dict):
        raise RepairProviderError("provider response must be a JSON object")
    required = {
        "hypothesis": str,
        class_field: str,
        "expected_effect": str,
        "risk": str,
        "required_validation": list,
        "patch": str,
    }
    if isinstance(value.get("required_validation"), str):
        value = dict(value)
        value["required_validation"] = [
            item.strip()
            for item in str(value["required_validation"]).split(",")
            if item.strip()
        ]
    elif isinstance(value.get("required_validation"), Mapping):
        raw_validation = value["required_validation"]
        if all(
            isinstance(name, str) and isinstance(enabled, bool)
            for name, enabled in raw_validation.items()
        ):
            value = dict(value)
            value["required_validation"] = [
                str(name) for name, enabled in raw_validation.items() if enabled
            ]
    aliases = {
        "simulation": "csim", "c-sim": "csim", "c_sim": "csim",
        "synthesis": "synth", "hls-synthesis": "synth",
        "co-simulation": "cosim", "co_sim": "cosim", "co-sim": "cosim",
    }
    if isinstance(value.get("required_validation"), list):
        value = dict(value)
        normalized = []
        for item in value["required_validation"]:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            token = item.strip().lower()
            token = aliases.get(token, token)
            if "co-sim" in token or "cosim" in token:
                token = "cosim"
            elif "synth" in token:
                token = "synth"
            elif "csim" in token or "c-sim" in token or "simulation" in token:
                token = "csim"
            normalized.append(token)
        value["required_validation"] = normalized
    if set(value) != set(required):
        raise RepairProviderError("provider response has missing or unexpected fields")
    for name, expected in required.items():
        if not isinstance(value[name], expected):
            raise RepairProviderError(f"provider response field {name} has the wrong type")
    validations = value["required_validation"]
    if not validations or any(
        not isinstance(item, str) or item not in {"csim", "synth", "cosim"}
        for item in validations
    ):
        raise RepairProviderError("required_validation contains an unsupported stage")
    if len(validations) != len(set(validations)):
        raise RepairProviderError("required_validation contains duplicates")
    for name in ("hypothesis", class_field, "expected_effect", "risk", "patch"):
        if not str(value[name]).strip():
            raise RepairProviderError(f"provider response field {name} is empty")
    if "```" in str(value["patch"]):
        raise RepairProviderError("provider patch must not contain Markdown fences")
    return value


@dataclass(frozen=True)
class _Completion:
    content: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    request_id: str | None
    duration_seconds: float
    finish_reason: str | None
    requested_max_output_tokens: int
    effective_max_output_tokens: int
    revision: str | None


def _completion_body(
    config: OpenAICompatibleConfig,
    *,
    prompt: str,
    system_prompt: str | None = None,
    effective_max_output_tokens: int | None = None,
) -> dict[str, object]:
    selected_max = (
        config.max_output_tokens
        if effective_max_output_tokens is None
        else int(effective_max_output_tokens)
    )
    if selected_max <= 0 or selected_max > config.max_output_tokens:
        raise ValueError(
            "effective_max_output_tokens must be positive and no greater than the provider cap"
        )
    body: dict[str, object] = {
        "model": config.model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
                or "Follow the response schema exactly. Never modify tests or interfaces.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": config.temperature,
        "max_tokens": selected_max,
        "response_format": {"type": "json_object"},
    }
    if config.top_p is not None:
        body["top_p"] = config.top_p
    if "api.deepseek.com" in config.base_url and config.model.startswith("deepseek-"):
        body["thinking"] = {"type": "disabled"}
    return body


def _request_completion(
    config: OpenAICompatibleConfig,
    transport: Transport,
    *,
    prompt: str,
    system_prompt: str | None = None,
    effective_max_output_tokens: int | None = None,
) -> _Completion:
    body = _completion_body(
        config,
        prompt=prompt,
        system_prompt=system_prompt,
        effective_max_output_tokens=effective_max_output_tokens,
    )
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        config.chat_completions_url,
        data=encoded,
        headers={
            "Authorization": "Bearer " + config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    status, headers, raw = transport(request, config.timeout_seconds)
    duration = time.monotonic() - started
    if status < 200 or status >= 300:
        response_excerpt = raw.decode("utf-8", errors="replace")[:2000]
        context_limit = bool(
            re.search(
                r"(?:context(?: length| window)?|maximum context).{0,80}(?:exceed|limit|too long)",
                response_excerpt,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )
        raise RepairProviderError(
            f"OpenAI-compatible API returned HTTP {status}",
            response_excerpt=response_excerpt,
            output_truncated=context_limit,
            truncation_reason="CONTEXT_LIMIT" if context_limit else None,
            usage_complete=False,
        )
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairProviderError("OpenAI-compatible API returned invalid JSON") from exc
    request_id = (
        envelope.get("id")
        or headers.get("x-request-id")
        or headers.get("X-Request-Id")
    )
    response_model = envelope.get("model")
    system_fingerprint = envelope.get("system_fingerprint")
    revision_parts = [
        str(item)
        for item in (response_model, system_fingerprint)
        if isinstance(item, str) and item.strip()
    ]
    try:
        choice = envelope["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RepairProviderError(
            "OpenAI-compatible API response has no assistant content",
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
            usage_complete=False,
        ) from exc
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    if finish_reason is not None and not isinstance(finish_reason, str):
        finish_reason = str(finish_reason)
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    if not isinstance(content, str) or not content.strip():
        raise RepairProviderError(
            "provider response content is empty",
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
            finish_reason=finish_reason,
            response_excerpt=content[:2000] if isinstance(content, str) else None,
            usage_complete=False,
        )
    usage = envelope.get("usage", {})
    if not isinstance(usage, dict):
        raise RepairProviderError(
            "OpenAI-compatible API usage is invalid",
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
            response_excerpt=content[:2000],
            finish_reason=finish_reason,
            usage_complete=False,
        )
    try:
        input_tokens = int(usage["prompt_tokens"])
        output_tokens = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RepairProviderError(
            "OpenAI-compatible API did not report token usage",
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
            response_excerpt=content[:2000],
            finish_reason=finish_reason,
            usage_complete=False,
        ) from exc
    cached_input_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    if input_tokens < 0 or output_tokens < 0 or cached_input_tokens < 0:
        raise RepairProviderError(
            "OpenAI-compatible API reported negative token usage",
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
            response_excerpt=content[:2000],
            finish_reason=finish_reason,
            usage_complete=False,
        )
    return _Completion(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        request_id=str(request_id) if request_id else None,
        duration_seconds=duration,
        finish_reason=finish_reason,
        requested_max_output_tokens=config.max_output_tokens,
        effective_max_output_tokens=int(body["max_tokens"]),
        revision="|".join(revision_parts) if revision_parts else None,
    )


def _raise_if_truncated(
    completion: _Completion,
    *,
    required_fields: tuple[str, ...] = ("patch",),
) -> None:
    reason = classify_output_truncation(
        completion.content,
        completion.finish_reason,
        required_fields=required_fields,
    )
    if reason == "NOT_TRUNCATED":
        return
    raise RepairProviderError(
        "provider output is incomplete and cannot create a Candidate",
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        cached_input_tokens=completion.cached_input_tokens,
        duration_seconds=completion.duration_seconds,
        request_id=completion.request_id,
        response_excerpt=completion.content[:2000],
        finish_reason=completion.finish_reason,
        output_truncated=True,
        truncation_reason=reason,
    )


def _completion_proposal_metadata(completion: _Completion) -> dict[str, object]:
    return {
        "revision": completion.revision,
        "finish_reason": completion.finish_reason,
        "output_truncated": False,
        "truncation_reason": None,
        "provider_parameter_name": "max_tokens",
        "requested_max_output_tokens": completion.requested_max_output_tokens,
        "effective_max_output_tokens": completion.effective_max_output_tokens,
    }


def _context_effective_max_output(context: Mapping[str, object]) -> int | None:
    raw = context.get("token_budget")
    if raw is None:
        hidden = context.get("_effective_max_output_tokens")
        if hidden is None:
            return None
        if isinstance(hidden, bool) or not isinstance(hidden, int) or hidden <= 0:
            raise ValueError("_effective_max_output_tokens must be positive")
        return hidden
    if not isinstance(raw, Mapping):
        raise ValueError("token_budget must be a TokenEnvelope object")
    envelope = validate_token_envelope(raw)
    if envelope["planner_call_allowed"] is not True:
        raise ValueError("TokenEnvelope does not allow a Planner call")
    return int(envelope["effective_max_output_tokens"])


class OpenAICompatibleRepairProvider:
    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    def fingerprint(self) -> str:
        return ":".join(
            [
                "openai-compatible-v2",
                self.config.base_url,
                self.config.model,
                str(self.config.max_output_tokens),
                format(self.config.temperature, ".12g"),
                "default" if self.config.top_p is None else format(self.config.top_p, ".12g"),
            ]
        )

    def with_timeout(
        self, timeout_seconds: float
    ) -> "OpenAICompatibleRepairProvider":
        return type(self)(
            replace(self.config, timeout_seconds=float(timeout_seconds)),
            transport=self._transport,
        )

    def propose_patch(self, context: RepairContext) -> PatchProposal:
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=build_repair_prompt(context),
        )
        _raise_if_truncated(
            completion,
            required_fields=(
                "hypothesis",
                "change_class",
                "expected_effect",
                "risk",
                "required_validation",
                "patch",
            ),
        )
        try:
            parsed = _strict_response(completion.content)
        except RepairProviderError as exc:
            raise RepairProviderError(
                str(exc),
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_input_tokens=completion.cached_input_tokens,
                duration_seconds=completion.duration_seconds,
                request_id=completion.request_id,
                response_excerpt=completion.content[:2000],
                finish_reason=completion.finish_reason,
            ) from exc
        return PatchProposal(
            patch=str(parsed["patch"]),
            provider="openai-compatible",
            model=self.config.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_input_tokens=completion.cached_input_tokens,
            request_id=completion.request_id,
            duration_seconds=completion.duration_seconds,
            hypothesis=str(parsed["hypothesis"]),
            change_class=str(parsed["change_class"]),
            expected_effect=str(parsed["expected_effect"]),
            risk=str(parsed["risk"]),
            required_validation=tuple(str(item) for item in parsed["required_validation"]),
            **_completion_proposal_metadata(completion),
        )


class OpenAICompatibleOptimizationProvider:
    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    def fingerprint(self) -> str:
        return ":".join(
            [
                "openai-compatible-optimization-v2-official-score",
                self.config.base_url,
                self.config.model,
                str(self.config.max_output_tokens),
                format(self.config.temperature, ".12g"),
                "default" if self.config.top_p is None else format(self.config.top_p, ".12g"),
            ]
        )

    def with_timeout(
        self, timeout_seconds: float
    ) -> "OpenAICompatibleOptimizationProvider":
        """Return a request-equivalent provider with a tighter transport timeout."""

        return type(self)(
            replace(self.config, timeout_seconds=float(timeout_seconds)),
            transport=self._transport,
        )

    def describe_optimization_request(
        self, context: OptimizationContext
    ) -> dict[str, object]:
        prompt = build_optimization_prompt(context)
        return {
            "provider": "openai-compatible",
            "model": self.config.model,
            "endpoint": self.config.chat_completions_url,
            "http_body": _completion_body(self.config, prompt=prompt),
        }

    def describe_guided_optimization_request(
        self,
        context: OptimizationContext,
        experience_guidance: Mapping[str, object],
    ) -> dict[str, object]:
        prompt = build_guided_optimization_prompt(context, experience_guidance)
        return {
            "provider": "openai-compatible",
            "model": self.config.model,
            "endpoint": self.config.chat_completions_url,
            "http_body": _completion_body(self.config, prompt=prompt),
        }

    def describe_optimization_request_budgeted(
        self,
        context: OptimizationContext,
        token_envelope: Mapping[str, object],
        *,
        experience_guidance: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        envelope = validate_token_envelope(token_envelope)
        if envelope["planner_call_allowed"] is not True:
            raise ValueError("TokenEnvelope does not allow a Planner call")
        prompt = build_budgeted_optimization_prompt(
            context,
            envelope,
            experience_guidance=experience_guidance,
        )
        return {
            "provider": "openai-compatible",
            "model": self.config.model,
            "endpoint": self.config.chat_completions_url,
            "http_body": _completion_body(
                self.config,
                prompt=prompt,
                effective_max_output_tokens=int(
                    envelope["effective_max_output_tokens"]
                ),
            ),
        }

    def describe_optimization_request_capped(
        self,
        context: OptimizationContext,
        effective_max_output_tokens: int,
        *,
        experience_guidance: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Apply a dynamic hard cap without exposing TokenEnvelope text."""

        prompt = (
            build_guided_optimization_prompt(context, experience_guidance)
            if experience_guidance is not None
            else build_optimization_prompt(context)
        )
        return {
            "provider": "openai-compatible",
            "model": self.config.model,
            "endpoint": self.config.chat_completions_url,
            "http_body": _completion_body(
                self.config,
                prompt=prompt,
                effective_max_output_tokens=effective_max_output_tokens,
            ),
        }

    def describe_fast_experiment_request(
        self, context: Mapping[str, object]
    ) -> dict[str, object]:
        prompt = build_fast_experiment_prompt(context)
        return {
            "schema_version": FAST_EXPERIMENT_RESPONSE_SCHEMA,
            "provider": "openai-compatible",
            "model": self.config.model,
            "endpoint": self.config.chat_completions_url,
            "http_body": _completion_body(
                self.config,
                prompt=prompt,
                system_prompt=FAST_EXPERIMENT_SYSTEM_PROMPT,
                effective_max_output_tokens=_context_effective_max_output(context),
            ),
        }

    def describe_task_aware_request(
        self, context: Mapping[str, object]
    ) -> dict[str, object]:
        prompt = build_task_aware_prompt(context)
        return {
            "schema_version": TASK_AWARE_RESPONSE_SCHEMA,
            "provider": "openai-compatible",
            "model": self.config.model,
            "endpoint": self.config.chat_completions_url,
            "http_body": _completion_body(
                self.config,
                prompt=prompt,
                system_prompt=TASK_AWARE_SYSTEM_PROMPT,
                effective_max_output_tokens=_context_effective_max_output(context),
            ),
        }

    def propose_task_aware(
        self, context: Mapping[str, object]
    ) -> PatchProposal:
        mode = str(context.get("mode"))
        task = context.get("task")
        requires_cosim = (
            task.get("requires_cosim") if isinstance(task, Mapping) else None
        )
        if not isinstance(requires_cosim, bool):
            raise RepairProviderError(
                "task-aware Planner task.requires_cosim must be boolean"
            )
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=build_task_aware_prompt(context),
            system_prompt=TASK_AWARE_SYSTEM_PROMPT,
            effective_max_output_tokens=_context_effective_max_output(context),
        )
        _raise_if_truncated(
            completion,
            required_fields=(
                "target_obligation",
                "hypothesis",
                "action_family",
                "action_parameters",
                "validation_plan",
                "primary_failure",
                "evidence_used",
                "change_class",
                "expected_effect",
                "failure_criteria",
                "fallback",
                "risk",
                "patch",
            ),
        )
        try:
            parsed = _strict_task_aware_response(completion.content, mode=mode)
        except RepairProviderError as exc:
            raise RepairProviderError(
                str(exc),
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_input_tokens=completion.cached_input_tokens,
                duration_seconds=completion.duration_seconds,
                request_id=completion.request_id,
                response_excerpt=completion.content[:2000],
                finish_reason=completion.finish_reason,
            ) from exc
        search_control = context.get("search_control")
        if isinstance(search_control, Mapping):
            required_obligation = search_control.get("primary_obligation")
            if (
                isinstance(required_obligation, str)
                and required_obligation
                and parsed["target_obligation"] != required_obligation
            ):
                raise RepairProviderError(
                    "task-aware Planner target_obligation conflicts with search control",
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    cached_input_tokens=completion.cached_input_tokens,
                    duration_seconds=completion.duration_seconds,
                    request_id=completion.request_id,
                    response_excerpt=completion.content[:2000],
                    finish_reason=completion.finish_reason,
                )
        if mode == "REPAIR":
            required_validation = (
                ("csim", "synth", "cosim")
                if requires_cosim
                else ("csim", "synth")
            )
        elif mode == "SYNTH_FIX":
            required_validation = ("csim", "synth")
        elif mode == "STRUCTURAL_FIX":
            required_validation = ("csim", "synth", "cosim")
        else:  # The strict parser already rejects unsupported modes.
            raise RepairProviderError("task-aware Planner mode is unsupported")
        risk = dict(parsed["risk"])
        risk.update(
            {
                "mode": mode,
                "primary_failure": str(parsed["primary_failure"]),
                "evidence_used": [
                    str(item) for item in parsed["evidence_used"]
                ],
            }
        )
        if tuple(parsed["validation_plan"]) != required_validation:
            raise RepairProviderError(
                "task-aware Planner validation_plan conflicts with deterministic mode policy",
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_input_tokens=completion.cached_input_tokens,
                duration_seconds=completion.duration_seconds,
                request_id=completion.request_id,
                response_excerpt=completion.content[:2000],
                finish_reason=completion.finish_reason,
            )
        return PatchProposal(
            patch=_normalize_unified_diff_hunk_counts(str(parsed["patch"])),
            provider="openai-compatible-task-aware",
            model=self.config.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_input_tokens=completion.cached_input_tokens,
            request_id=completion.request_id,
            duration_seconds=completion.duration_seconds,
            hypothesis=str(parsed["hypothesis"]),
            target_obligation=str(parsed["target_obligation"]),
            action_family=str(parsed["action_family"]),
            action_parameters=dict(parsed["action_parameters"]),
            validation_plan=tuple(str(item) for item in parsed["validation_plan"]),
            change_class=str(parsed["change_class"]),
            expected_effect=str(parsed["expected_effect"]),
            failure_criteria=str(parsed["failure_criteria"]),
            fallback=str(parsed["fallback"]),
            risk=json.dumps(risk, ensure_ascii=False, sort_keys=True),
            required_validation=required_validation,
            **_completion_proposal_metadata(completion),
        )

    def propose_fast_experiment(
        self, context: Mapping[str, object]
    ) -> PatchProposal:
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=build_fast_experiment_prompt(context),
            system_prompt=FAST_EXPERIMENT_SYSTEM_PROMPT,
            effective_max_output_tokens=_context_effective_max_output(context),
        )
        _raise_if_truncated(
            completion,
            required_fields=(
                "hypothesis",
                "primary_bottleneck",
                "evidence_used",
                "strategy_bundle",
                "expected_effect",
                "risk",
                "patch",
            ),
        )
        try:
            parsed = _strict_fast_experiment_response(completion.content)
        except RepairProviderError as exc:
            raise RepairProviderError(
                str(exc),
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_input_tokens=completion.cached_input_tokens,
                duration_seconds=completion.duration_seconds,
                request_id=completion.request_id,
                response_excerpt=completion.content[:2000],
                finish_reason=completion.finish_reason,
            ) from exc
        strategy_bundle = tuple(str(item) for item in parsed["strategy_bundle"])
        risk = dict(parsed["risk"])
        risk["primary_bottleneck"] = str(parsed["primary_bottleneck"])
        risk["evidence_used"] = [str(item) for item in parsed["evidence_used"]]
        return PatchProposal(
            patch=_normalize_unified_diff_hunk_counts(str(parsed["patch"])),
            provider="openai-compatible-fast-experiment",
            model=self.config.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_input_tokens=completion.cached_input_tokens,
            request_id=completion.request_id,
            duration_seconds=completion.duration_seconds,
            hypothesis=str(parsed["hypothesis"]),
            change_class="+".join(strategy_bundle),
            expected_effect=str(parsed["expected_effect"]),
            risk=json.dumps(risk, ensure_ascii=False, sort_keys=True),
            required_validation=("csim", "synth"),
            **_completion_proposal_metadata(completion),
        )

    def _propose_optimization_with_prompt(
        self,
        context: OptimizationContext,
        prompt: str,
        *,
        effective_max_output_tokens: int | None = None,
    ) -> PatchProposal:
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=prompt,
            effective_max_output_tokens=effective_max_output_tokens,
        )
        _raise_if_truncated(
            completion,
            required_fields=(
                "hypothesis",
                "optimization_class",
                "expected_effect",
                "risk",
                "required_validation",
                "patch",
            ),
        )
        try:
            parsed = _strict_response(
                completion.content,
                class_field="optimization_class",
            )
            optimization_class = str(parsed["optimization_class"])
            if optimization_class != context.allowed_optimization_class:
                raise RepairProviderError(
                    "provider optimization class does not match the allowed class"
                )
            validations = tuple(str(item) for item in parsed["required_validation"])
            if set(validations) != {"csim", "synth", "cosim"}:
                raise RepairProviderError(
                    "optimization required_validation must contain csim, synth, and cosim"
                )
        except RepairProviderError as exc:
            raise RepairProviderError(
                str(exc),
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_input_tokens=completion.cached_input_tokens,
                duration_seconds=completion.duration_seconds,
                request_id=completion.request_id,
                response_excerpt=completion.content[:2000],
                finish_reason=completion.finish_reason,
            ) from exc
        return PatchProposal(
            patch=str(parsed["patch"]),
            provider="openai-compatible",
            model=self.config.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_input_tokens=completion.cached_input_tokens,
            request_id=completion.request_id,
            duration_seconds=completion.duration_seconds,
            hypothesis=str(parsed["hypothesis"]),
            change_class=optimization_class,
            expected_effect=str(parsed["expected_effect"]),
            risk=str(parsed["risk"]),
            required_validation=validations,
            **_completion_proposal_metadata(completion),
        )

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal:
        return self._propose_optimization_with_prompt(
            context, build_optimization_prompt(context)
        )

    def propose_optimization_budgeted(
        self,
        context: OptimizationContext,
        token_envelope: Mapping[str, object],
        *,
        experience_guidance: Mapping[str, object] | None = None,
    ) -> PatchProposal:
        envelope = validate_token_envelope(token_envelope)
        return self._propose_optimization_with_prompt(
            context,
            build_budgeted_optimization_prompt(
                context,
                envelope,
                experience_guidance=experience_guidance,
            ),
            effective_max_output_tokens=int(
                envelope["effective_max_output_tokens"]
            ),
        )

    def propose_optimization_capped(
        self,
        context: OptimizationContext,
        effective_max_output_tokens: int,
        *,
        experience_guidance: Mapping[str, object] | None = None,
    ) -> PatchProposal:
        """Use the legacy Prompt while enforcing a dynamic Provider cap."""

        prompt = (
            build_guided_optimization_prompt(context, experience_guidance)
            if experience_guidance is not None
            else build_optimization_prompt(context)
        )
        return self._propose_optimization_with_prompt(
            context,
            prompt,
            effective_max_output_tokens=effective_max_output_tokens,
        )

    def propose_guided_optimization(
        self,
        context: OptimizationContext,
        experience_guidance: Mapping[str, object],
    ) -> PatchProposal:
        return self._propose_optimization_with_prompt(
            context,
            build_guided_optimization_prompt(context, experience_guidance),
        )
