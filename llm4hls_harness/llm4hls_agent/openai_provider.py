"""OpenAI-compatible repair provider with a strict, auditable response contract."""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

from .optimization import OptimizationContext
from .repair import PatchProposal, RepairContext, RepairProviderError


DEFAULT_MODEL = "deepseek-v4-pro"
FAST_EXPERIMENT_RESPONSE_SCHEMA = "v3b.fast-planner-response.v1"
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
_UNIFIED_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,\d+)? "
    r"\+(?P<new_start>\d+)(?:,\d+)? @@(?P<suffix>.*)$"
)


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
        raise RepairProviderError(f"OpenAI-compatible API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RepairProviderError(
            f"OpenAI-compatible API request failed ({type(exc).__name__})"
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
        "final_reserve_credits": context.final_reserve_credits,
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
    if set(context) != required:
        raise ValueError("fast Planner context has an invalid field set")
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


def _completion_body(
    config: OpenAICompatibleConfig,
    *,
    prompt: str,
    system_prompt: str | None = None,
) -> dict[str, object]:
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
        "max_tokens": config.max_output_tokens,
        "response_format": {"type": "json_object"},
    }
    if "api.deepseek.com" in config.base_url and config.model.startswith("deepseek-"):
        body["thinking"] = {"type": "disabled"}
    return body


def _request_completion(
    config: OpenAICompatibleConfig,
    transport: Transport,
    *,
    prompt: str,
    system_prompt: str | None = None,
) -> _Completion:
    body = _completion_body(
        config, prompt=prompt, system_prompt=system_prompt
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
        raise RepairProviderError(f"OpenAI-compatible API returned HTTP {status}")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairProviderError("OpenAI-compatible API returned invalid JSON") from exc
    usage = envelope.get("usage", {})
    if not isinstance(usage, dict):
        raise RepairProviderError(
            "OpenAI-compatible API usage is invalid", duration_seconds=duration
        )
    try:
        input_tokens = int(usage["prompt_tokens"])
        output_tokens = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RepairProviderError(
            "OpenAI-compatible API did not report token usage",
            duration_seconds=duration,
        ) from exc
    cached_input_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    if input_tokens < 0 or output_tokens < 0 or cached_input_tokens < 0:
        raise RepairProviderError(
            "OpenAI-compatible API reported negative token usage",
            duration_seconds=duration,
        )
    request_id = (
        envelope.get("id")
        or headers.get("x-request-id")
        or headers.get("X-Request-Id")
    )
    try:
        message = envelope["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RepairProviderError(
            "OpenAI-compatible API response has no assistant content",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
        ) from exc
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    if not isinstance(content, str) or not content.strip():
        raise RepairProviderError(
            "provider response content is empty",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            duration_seconds=duration,
            request_id=str(request_id) if request_id else None,
        )
    return _Completion(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        request_id=str(request_id) if request_id else None,
        duration_seconds=duration,
    )


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
            ]
        )

    def propose_patch(self, context: RepairContext) -> PatchProposal:
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=build_repair_prompt(context),
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
            ]
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
            ),
        }

    def propose_fast_experiment(
        self, context: Mapping[str, object]
    ) -> PatchProposal:
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=build_fast_experiment_prompt(context),
            system_prompt=FAST_EXPERIMENT_SYSTEM_PROMPT,
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
        )

    def propose_optimization(self, context: OptimizationContext) -> PatchProposal:
        completion = _request_completion(
            self.config,
            self._transport,
            prompt=build_optimization_prompt(context),
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
        )
