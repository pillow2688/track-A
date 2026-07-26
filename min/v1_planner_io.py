"""Durable, response-first I/O and strict JSON adaptation for the V1 planner.

This module deliberately does not call a model a second time.  Its boundary is:

    HTTP response -> durable sanitized evidence -> caller accounts the call
                  -> strict local JSON adaptation
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


ERROR_EMPTY_RESPONSE = "EMPTY_RESPONSE"
ERROR_TRUNCATED_JSON = "TRUNCATED_JSON"
ERROR_INVALID_JSON = "INVALID_JSON"
ERROR_INVALID_SCHEMA = "INVALID_SCHEMA"
ERROR_HTTP_RESPONSE = "HTTP_RESPONSE_ERROR"
ERROR_TRANSPORT = "TRANSPORT_ERROR"
ERROR_INVALID_ENVELOPE = "INVALID_ENVELOPE"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "client_secret",
    "password",
    "cookie",
    "set-cookie",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'authorization|client[_-]?secret|password)"\s*:\s*")([^"]*)(")'
)
_FENCE = re.compile(
    r"\A\s*```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\s*\Z",
    re.IGNORECASE,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _redact_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = _BEARER.sub("Bearer <REDACTED>", value)
    redacted = _JSON_SECRET.sub(r'\1<REDACTED>\3', redacted)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<REDACTED_SECRET>")
    return redacted


def sanitize_response_value(
    value: object,
    *,
    secrets: tuple[str, ...] = (),
) -> object:
    """Recursively redact secret-shaped response keys and Bearer values."""

    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            folded = key.casefold().replace("-", "_")
            if any(part.replace("-", "_") in folded for part in _SENSITIVE_KEY_PARTS):
                sanitized[key] = "<REDACTED>"
            else:
                sanitized[key] = sanitize_response_value(
                    raw_value,
                    secrets=secrets,
                )
        return sanitized
    if isinstance(value, list):
        return [
            sanitize_response_value(item, secrets=secrets) for item in value
        ]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), secrets)


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or int(value) != value or int(value) < 0:
        return None
    return int(value)


@dataclass(frozen=True)
class NullableUsage:
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    tokens_used: int | None
    usage_complete: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "tokens_used": self.tokens_used,
            "usage_complete": self.usage_complete,
        }


@dataclass(frozen=True)
class HttpExchange:
    response_received: bool
    status_code: int | None
    request_id_header: str | None
    raw_body: bytes
    elapsed_s: float
    transport_error_type: str | None = None
    transport_error_detail: str | None = None


@dataclass(frozen=True)
class PlannerResponseReceipt:
    planner_dir: Path
    response_received: bool
    request_id: str | None
    finish_reason: str | None
    raw_content: str
    usage: NullableUsage
    elapsed_s: float
    status_code: int | None
    envelope_ref: str | None
    raw_response_ref: str | None
    raw_content_ref: str
    receipt_ref: str
    receipt_sha256: str
    envelope_error: str | None
    envelope_error_type: str | None
    content_error: str | None
    transport_error_type: str | None
    transport_error_detail: str | None


@dataclass(frozen=True)
class ParsedPlannerPayload:
    hypothesis: str
    patch: str


class PlannerAdaptationError(ValueError):
    """A stable, machine-readable local response adaptation error."""

    def __init__(self, category: str, detail: str):
        if category not in {
            ERROR_EMPTY_RESPONSE,
            ERROR_TRUNCATED_JSON,
            ERROR_INVALID_JSON,
            ERROR_INVALID_SCHEMA,
            ERROR_HTTP_RESPONSE,
            ERROR_TRANSPORT,
            ERROR_INVALID_ENVELOPE,
        }:
            raise ValueError(f"unknown planner adaptation category: {category}")
        self.category = category
        self.detail = detail
        super().__init__(f"{category}: {detail}")


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for index, item in enumerate(content):
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            raise PlannerAdaptationError(
                ERROR_INVALID_SCHEMA,
                f"message content item {index} is not text",
            )
        return "".join(parts)
    if content is None:
        return ""
    raise PlannerAdaptationError(
        ERROR_INVALID_SCHEMA,
        "message content is neither a string nor a text array",
    )


def _extract_usage(envelope: Mapping[str, object]) -> NullableUsage:
    raw_usage = envelope.get("usage")
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    input_tokens = _optional_nonnegative_int(usage.get("prompt_tokens"))
    output_tokens = _optional_nonnegative_int(usage.get("completion_tokens"))
    provider_total = _optional_nonnegative_int(usage.get("total_tokens"))
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    cached = _optional_nonnegative_int(details.get("cached_tokens"))
    if cached is None:
        cached = _optional_nonnegative_int(usage.get("prompt_cache_hit_tokens"))
    complete = input_tokens is not None and output_tokens is not None
    computed_total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    total = provider_total if provider_total is not None else computed_total
    if complete and total != computed_total:
        complete = False
        total = None
    if not complete:
        total = None
    return NullableUsage(
        input_tokens=input_tokens if complete else None,
        output_tokens=output_tokens if complete else None,
        cached_input_tokens=cached if complete else None,
        tokens_used=total,
        usage_complete=complete,
    )


def perform_chat_completion(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    timeout_s: float,
) -> HttpExchange:
    """Perform exactly one OpenAI-compatible request without parsing its content."""

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_output_tokens,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw_body = response.read()
            headers = response.headers
            request_id = (
                headers.get("x-request-id")
                or headers.get("request-id")
                or headers.get("x-amzn-requestid")
            )
            return HttpExchange(
                response_received=True,
                status_code=getattr(response, "status", None),
                request_id_header=request_id,
                raw_body=raw_body,
                elapsed_s=time.monotonic() - started,
            )
    except urllib.error.HTTPError as exc:
        raw_body = exc.read()
        request_id = (
            exc.headers.get("x-request-id")
            or exc.headers.get("request-id")
            or exc.headers.get("x-amzn-requestid")
        )
        return HttpExchange(
            response_received=True,
            status_code=exc.code,
            request_id_header=request_id,
            raw_body=raw_body,
            elapsed_s=time.monotonic() - started,
            transport_error_type=type(exc).__name__,
            transport_error_detail=_redact_text(str(exc), (api_key,)),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HttpExchange(
            response_received=False,
            status_code=None,
            request_id_header=None,
            raw_body=b"",
            elapsed_s=time.monotonic() - started,
            transport_error_type=type(exc).__name__,
            transport_error_detail=_redact_text(str(exc), (api_key,)),
        )


def persist_response(
    *,
    planner_dir: Path,
    exchange: HttpExchange,
    model: str,
    api_key: str = "",
    action_id: str | None = None,
    prompt_sha256: str | None = None,
    endpoint: str | None = None,
) -> PlannerResponseReceipt:
    """Persist the complete sanitized response evidence before content parsing."""

    planner_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = planner_dir / "response_receipt.json"
    if receipt_path.exists():
        raise FileExistsError(
            f"refusing to overwrite existing planner response: {receipt_path}"
        )
    secrets = tuple(secret for secret in (api_key,) if secret)
    envelope: Mapping[str, object] | None = None
    envelope_error: str | None = None
    envelope_error_type: str | None = None
    raw_response_ref: str | None = None
    decoded = ""
    if exchange.raw_body:
        try:
            decoded = exchange.raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            decoded = exchange.raw_body.decode("utf-8", errors="replace")
            envelope_error = f"UnicodeDecodeError: {exc}"
            envelope_error_type = "INVALID_ENVELOPE_ENCODING"
        if envelope_error_type is None:
            try:
                parsed = json.loads(decoded)
                if isinstance(parsed, Mapping):
                    envelope = parsed
                else:
                    envelope_error = "response envelope is not a JSON object"
                    envelope_error_type = "INVALID_ENVELOPE_SCHEMA"
            except json.JSONDecodeError as exc:
                envelope_error = f"JSONDecodeError: {exc}"
                envelope_error_type = "INVALID_ENVELOPE_JSON"
        if envelope is None:
            raw_response_path = planner_dir / "raw_response.txt"
            _atomic_write_text(
                raw_response_path,
                _redact_text(decoded, secrets),
            )
            raw_response_ref = raw_response_path.name

    sanitized_envelope = (
        sanitize_response_value(envelope, secrets=secrets)
        if envelope is not None
        else None
    )
    envelope_ref: str | None = None
    if sanitized_envelope is not None:
        envelope_path = planner_dir / "response_envelope.json"
        _atomic_write_json(envelope_path, sanitized_envelope)
        envelope_ref = envelope_path.name

    request_id = (
        _redact_text(exchange.request_id_header, secrets)
        if exchange.request_id_header is not None
        else None
    )
    finish_reason: str | None = None
    raw_content = ""
    content_error: str | None = None
    usage = NullableUsage(None, None, None, None, False)
    if envelope is not None:
        envelope_id = envelope.get("id")
        if isinstance(envelope_id, str) and envelope_id.strip():
            request_id = _redact_text(envelope_id, secrets)
        usage = _extract_usage(envelope)
        choices = envelope.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            first = choices[0]
            finish = first.get("finish_reason")
            finish_reason = finish if isinstance(finish, str) else None
            message = first.get("message")
            message = message if isinstance(message, Mapping) else {}
            try:
                raw_content = _redact_text(
                    _extract_text(message.get("content")),
                    secrets,
                )
            except PlannerAdaptationError as exc:
                content_error = str(exc)
        else:
            content_error = "INVALID_SCHEMA: choices[0] is unavailable"

    raw_content_path = planner_dir / "raw_content.txt"
    _atomic_write_text(raw_content_path, raw_content)
    persisted_paths = [raw_content_path]
    if envelope_ref is not None:
        persisted_paths.append(planner_dir / envelope_ref)
    if raw_response_ref is not None:
        persisted_paths.append(planner_dir / raw_response_ref)
    secret_material_persisted = any(
        secret.encode("utf-8") in path.read_bytes()
        for secret in secrets
        for path in persisted_paths
    )
    if secret_material_persisted:
        raise RuntimeError("planner response redaction failed closed")
    receipt_value = {
        "schema_version": "track-a.v1-planner-response-receipt.v1",
        "model": model,
        "action_id": action_id,
        "prompt_sha256": prompt_sha256,
        "endpoint_host": (
            urllib.parse.urlsplit(endpoint).hostname if endpoint else None
        ),
        "response_received": exchange.response_received,
        "http_status": exchange.status_code,
        "request_id": request_id,
        "finish_reason": finish_reason,
        "elapsed_s": exchange.elapsed_s,
        "usage": usage.to_dict(),
        "wire_body_sha256": _sha256(exchange.raw_body),
        "envelope_ref": envelope_ref,
        "envelope_sha256": (
            _sha256((planner_dir / envelope_ref).read_bytes())
            if envelope_ref is not None
            else None
        ),
        "raw_response_ref": raw_response_ref,
        "raw_response_sha256": (
            _sha256((planner_dir / raw_response_ref).read_bytes())
            if raw_response_ref is not None
            else None
        ),
        "raw_content_ref": raw_content_path.name,
        "raw_content_sha256": _sha256(raw_content_path.read_bytes()),
        "envelope_error": envelope_error,
        "envelope_error_type": envelope_error_type,
        "content_extraction_error": content_error,
        "transport_error_type": exchange.transport_error_type,
        "transport_error_detail": (
            _redact_text(exchange.transport_error_detail, secrets)
            if exchange.transport_error_detail is not None
            else None
        ),
        "secret_redaction_applied": bool(
            any(secret in decoded for secret in secrets)
        ),
        "secret_material_persisted": secret_material_persisted,
    }
    _atomic_write_json(receipt_path, receipt_value)
    return PlannerResponseReceipt(
        planner_dir=planner_dir,
        response_received=exchange.response_received,
        request_id=request_id,
        finish_reason=finish_reason,
        raw_content=raw_content,
        usage=usage,
        elapsed_s=exchange.elapsed_s,
        status_code=exchange.status_code,
        envelope_ref=envelope_ref,
        raw_response_ref=raw_response_ref,
        raw_content_ref=raw_content_path.name,
        receipt_ref=receipt_path.name,
        receipt_sha256=_sha256(receipt_path.read_bytes()),
        envelope_error=envelope_error,
        envelope_error_type=envelope_error_type,
        content_error=content_error,
        transport_error_type=exchange.transport_error_type,
        transport_error_detail=(
            _redact_text(exchange.transport_error_detail, secrets)
            if exchange.transport_error_detail is not None
            else None
        ),
    )


def _looks_truncated(content: str, error: json.JSONDecodeError) -> bool:
    stripped = content.rstrip()
    if not stripped:
        return False
    if error.msg.startswith("Unterminated string"):
        return True
    if stripped.endswith(("}", "]")):
        return False
    if error.pos >= max(0, len(content) - 2):
        return True
    return False


def parse_planner_response(
    receipt: PlannerResponseReceipt,
) -> ParsedPlannerPayload:
    """Strictly adapt locally stored content; never dispatch another request."""

    content = receipt.raw_content
    if not receipt.response_received:
        raise PlannerAdaptationError(
            ERROR_TRANSPORT,
            receipt.transport_error_detail or "planner request produced no response",
        )
    if (
        receipt.status_code is None
        or receipt.status_code < 200
        or receipt.status_code >= 300
    ):
        raise PlannerAdaptationError(
            ERROR_HTTP_RESPONSE,
            f"planner HTTP status is {receipt.status_code}",
        )
    if receipt.envelope_error_type is not None:
        raise PlannerAdaptationError(
            ERROR_INVALID_ENVELOPE,
            f"{receipt.envelope_error_type}: {receipt.envelope_error}",
        )
    if receipt.content_error is not None:
        raise PlannerAdaptationError(
            ERROR_INVALID_SCHEMA,
            receipt.content_error.partition(":")[2].strip()
            or receipt.content_error,
        )
    if receipt.finish_reason == "length":
        raise PlannerAdaptationError(
            ERROR_TRUNCATED_JSON,
            "provider finish_reason indicates output truncation",
        )
    if not content.strip():
        raise PlannerAdaptationError(
            ERROR_EMPTY_RESPONSE,
            "planner content is empty",
        )
    fence = _FENCE.fullmatch(content)
    candidate = fence.group("body") if fence is not None else content
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        category = (
            ERROR_TRUNCATED_JSON
            if receipt.finish_reason == "length" or _looks_truncated(candidate, exc)
            else ERROR_INVALID_JSON
        )
        raise PlannerAdaptationError(category, str(exc)) from exc
    if not isinstance(parsed, Mapping):
        raise PlannerAdaptationError(
            ERROR_INVALID_SCHEMA,
            "planner JSON root must be an object",
        )
    if set(parsed) != {"hypothesis", "patch"}:
        raise PlannerAdaptationError(
            ERROR_INVALID_SCHEMA,
            "planner JSON must contain exactly hypothesis and patch",
        )
    hypothesis = parsed.get("hypothesis")
    patch = parsed.get("patch")
    if (
        not isinstance(hypothesis, str)
        or not hypothesis.strip()
        or not isinstance(patch, str)
        or not patch.strip()
    ):
        raise PlannerAdaptationError(
            ERROR_INVALID_SCHEMA,
            "hypothesis and patch must be non-empty strings",
        )
    return ParsedPlannerPayload(hypothesis=hypothesis, patch=patch)


def persist_parse_result(
    *,
    planner_dir: Path,
    payload: ParsedPlannerPayload | None,
    error: PlannerAdaptationError | None,
) -> Path:
    if (payload is None) == (error is None):
        raise ValueError("exactly one of payload or error must be provided")
    path = planner_dir / "parse_result.json"
    if payload is not None:
        value: dict[str, object] = {
            "schema_version": "track-a.v1-planner-parse.v1",
            "ok": True,
            "error_type": None,
            "hypothesis_sha256": _sha256(payload.hypothesis.encode("utf-8")),
            "patch_sha256": _sha256(payload.patch.encode("utf-8")),
        }
    else:
        assert error is not None
        value = {
            "schema_version": "track-a.v1-planner-parse.v1",
            "ok": False,
            "error_type": error.category,
            "detail": error.detail,
        }
    _atomic_write_json(path, value)
    return path
