"""OpenAI-compatible repair provider with a strict, auditable response contract."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

from .optimization import OptimizationContext
from .repair import PatchProposal, RepairContext, RepairProviderError


DEFAULT_MODEL = "deepseek-v4-pro"


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
    objective = {
        "task_id": context.task_id,
        "parent_candidate_id": context.parent_candidate_id,
        "round_index": context.round_index,
        "allowed_optimization_class": context.allowed_optimization_class,
        "bottleneck": context.bottleneck,
        "evidence": list(context.evidence),
        "baseline_metrics": dict(context.baseline_metrics),
        "current_metrics": dict(context.current_metrics),
        "failed_actions": [dict(item) for item in context.failed_actions],
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
    return "\n".join(
        [
            "ROLE\nYou are a Vitis HLS PPA optimization agent.",
            "OBJECTIVE\nImprove the stated bottleneck with one minimal Patch while preserving correctness and the interface.",
            "OPTIMIZATION\n" + json.dumps(objective, sort_keys=True),
            "RELEVANT SOURCE\n" + context.source_excerpt,
            "CONSTRAINTS\n" + json.dumps(constraints, sort_keys=True),
            "BUDGET\n" + json.dumps(budget, sort_keys=True),
            "OUTPUT\nReturn exactly one JSON object with hypothesis, optimization_class, expected_effect, risk, required_validation, and patch. The optimization_class must equal the allowed class. required_validation must contain csim, synth, and cosim. The patch must be one unified diff. Do not use Markdown fences or output any other text.",
        ]
    )


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


def _request_completion(
    config: OpenAICompatibleConfig,
    transport: Transport,
    *,
    prompt: str,
) -> _Completion:
    body: dict[str, object] = {
        "model": config.model,
        "messages": [
            {
                "role": "system",
                "content": "Follow the response schema exactly. Never modify tests or interfaces.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_output_tokens,
        "response_format": {"type": "json_object"},
    }
    if "api.deepseek.com" in config.base_url and config.model.startswith("deepseek-"):
        body["thinking"] = {"type": "disabled"}
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
                "openai-compatible-optimization-v1",
                self.config.base_url,
                self.config.model,
                str(self.config.max_output_tokens),
                format(self.config.temperature, ".12g"),
            ]
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
