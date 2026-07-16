"""Deterministic V2 optimization selection and workflow boundaries."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping


ALLOWED_OPTIMIZATIONS = (
    "LOOP_PIPELINE",
    "LOOP_UNROLL",
    "MEMORY_LAYOUT",
    "LOOP_RESTRUCTURE",
)


@dataclass(frozen=True)
class OptimizationDecision:
    optimization_class: str | None
    bottleneck: str
    evidence: tuple[str, ...]
    metrics_digest: str
    stop_reason: str | None = None


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_metric(metrics: Mapping[str, object], group: str, name: str) -> float:
    values = metrics.get(group)
    value = values.get(name) if isinstance(values, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"optimization metric {group}.{name} is missing")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"optimization metric {group}.{name} is invalid")
    return parsed


def select_optimization(
    metrics: Mapping[str, object],
    *,
    source: str,
    attempted: tuple[str, ...],
    failures: tuple[tuple[str, str], ...],
) -> OptimizationDecision:
    """Select one untried class from structured evidence without side effects."""

    if any(item not in ALLOWED_OPTIMIZATIONS for item in attempted):
        raise ValueError("attempted optimization contains an unsupported class")
    digest = _canonical_digest(metrics)
    failed_same = {
        optimization_class
        for optimization_class, metrics_digest in failures
        if metrics_digest == digest
    }
    available = [
        item
        for item in ALLOWED_OPTIMIZATIONS
        if item not in attempted and item not in failed_same
    ]
    if not available:
        return OptimizationDecision(
            optimization_class=None,
            bottleneck="no distinct optimization remains",
            evidence=(),
            metrics_digest=digest,
            stop_reason="NO_DISTINCT_OPTIMIZATION",
        )

    interval = _positive_metric(metrics, "interval", "max")
    latency = _positive_metric(metrics, "latency", "worst")
    raw_evidence = metrics.get("evidence", [])
    evidence = tuple(
        str(item) for item in raw_evidence
    ) if isinstance(raw_evidence, list) else ()
    joined = " ".join(evidence).lower()

    if (
        "MEMORY_LAYOUT" in available
        and any(token in joined for token in ("memory port", "limited memory", "load operation"))
    ):
        selected = "MEMORY_LAYOUT"
        bottleneck = "memory port or array access bottleneck"
    elif interval > 1 and "LOOP_PIPELINE" in available:
        selected = "LOOP_PIPELINE"
        bottleneck = f"maximum interval is {interval:g}"
    elif "LOOP_UNROLL" in available and latency > max(4.0, 4.0 * interval):
        selected = "LOOP_UNROLL"
        bottleneck = f"worst latency is {latency:g} with interval {interval:g}"
    elif "MEMORY_LAYOUT" in available:
        selected = "MEMORY_LAYOUT"
        bottleneck = "pipeline and unroll opportunities were exhausted"
    else:
        selected = available[0]
        bottleneck = "remaining loop structure bottleneck"

    return OptimizationDecision(
        optimization_class=selected,
        bottleneck=bottleneck,
        evidence=evidence,
        metrics_digest=digest,
    )
