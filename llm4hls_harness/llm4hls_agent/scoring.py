"""Deterministic V2 PPA scoring and lexicographic Candidate comparison."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_WEIGHT_KEYS = ("latency", "ii", "lut", "ff", "dsp", "bram", "uram")
_RESOURCE_NAMES = {
    "lut": "LUT",
    "ff": "FF",
    "dsp": "DSP",
    "bram": "BRAM_18K",
    "uram": "URAM",
}


def _number(value: object, *, name: str, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if positive and number <= 0:
        raise ValueError(f"{name} must be positive")
    if not positive and number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


@dataclass(frozen=True)
class ScoringConfig:
    weights: Mapping[str, float]
    max_resource_percent: Mapping[str, float]
    required_verification_tier: int = 4
    official_score_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))
        object.__setattr__(
            self,
            "max_resource_percent",
            MappingProxyType(dict(self.max_resource_percent)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ppa_weights": dict(self.weights),
            "max_resource_percent": dict(self.max_resource_percent),
            "required_verification_tier": self.required_verification_tier,
            "official_score": {"enabled": self.official_score_enabled},
        }


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    verification_tier: int
    hard_constraints_passed: bool
    hard_failures: tuple[str, ...]
    ppa_cost: float | None
    components: Mapping[str, float]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    credits_used: int
    official_score: float | None = None

    @property
    def tokens_used(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "verification_tier": self.verification_tier,
            "hard_constraints_passed": self.hard_constraints_passed,
            "hard_failures": list(self.hard_failures),
            "official_score": self.official_score,
            "ppa_cost": self.ppa_cost,
            "components": dict(self.components),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "tokens_used": self.tokens_used,
            "credits_used": self.credits_used,
        }


@dataclass(frozen=True)
class Comparison:
    winner: str
    strictly_better: bool
    reason: str
    candidate_key: tuple[object, ...]
    incumbent_key: tuple[object, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "winner": self.winner,
            "strictly_better": self.strictly_better,
            "reason": self.reason,
            "candidate_key": list(self.candidate_key),
            "incumbent_key": list(self.incumbent_key),
        }


def load_scoring_config(path: str | Path) -> ScoringConfig:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scoring config: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("scoring config must be an object")
    expected = {
        "schema_version",
        "ppa_weights",
        "max_resource_percent",
        "required_verification_tier",
        "official_score",
    }
    if set(value) != expected or value.get("schema_version") != 1:
        raise ValueError("scoring config schema or fields are invalid")

    raw_weights = value.get("ppa_weights")
    if not isinstance(raw_weights, dict) or set(raw_weights) != set(_WEIGHT_KEYS):
        raise ValueError("scoring weights must configure all PPA metrics")
    weights = {
        key: _number(raw_weights[key], name=f"weight {key}", positive=False)
        for key in _WEIGHT_KEYS
    }
    if not math.isclose(
        sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9
    ):
        raise ValueError("scoring weights must sum to one")

    raw_caps = value.get("max_resource_percent")
    if not isinstance(raw_caps, dict) or set(raw_caps) != set(_RESOURCE_NAMES):
        raise ValueError("resource caps must configure every resource")
    caps = {
        key: _number(raw_caps[key], name=f"resource cap {key}", positive=True)
        for key in _RESOURCE_NAMES
    }
    if any(cap > 100.0 for cap in caps.values()):
        raise ValueError("resource caps cannot exceed 100 percent")

    tier = value.get("required_verification_tier")
    if isinstance(tier, bool) or not isinstance(tier, int) or tier not in range(1, 5):
        raise ValueError("required verification tier must be between 1 and 4")
    official = value.get("official_score")
    if (
        not isinstance(official, dict)
        or set(official) != {"enabled"}
        or not isinstance(official.get("enabled"), bool)
    ):
        raise ValueError("official score configuration is invalid")
    return ScoringConfig(
        weights=weights,
        max_resource_percent=caps,
        required_verification_tier=tier,
        official_score_enabled=official["enabled"],
    )


def verification_tier(validation: Mapping[str, object]) -> int:
    def passed(stage: str) -> bool:
        item = validation.get(stage)
        return isinstance(item, Mapping) and item.get("status") == "PASS"

    if passed("csim") and passed("synth") and passed("cosim"):
        return 4
    if passed("csim") and passed("synth"):
        return 3
    if passed("csim"):
        return 2
    if passed("static"):
        return 1
    return 0


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _metric_values(metrics: Mapping[str, object]) -> dict[str, float]:
    latency = _mapping(metrics.get("latency"), "latency")
    interval = _mapping(metrics.get("interval"), "interval")
    resources = _mapping(metrics.get("resources"), "resources")
    available = _mapping(metrics.get("available_resources"), "available resources")
    values = {
        "latency": _number(latency.get("worst"), name="latency", positive=True),
        "ii": _number(interval.get("max"), name="II", positive=True),
    }
    for key, report_name in _RESOURCE_NAMES.items():
        values[key] = _number(
            resources.get(report_name), name=f"resource {report_name}", positive=False
        )
        values[f"available_{key}"] = _number(
            available.get(report_name),
            name=f"available resource {report_name}",
            positive=True,
        )
    return values


def score_candidate(
    *,
    candidate_id: str,
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    validation: Mapping[str, object],
    clock: Mapping[str, object],
    config: ScoringConfig,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    credits_used: int,
    official_score: float | None = None,
) -> CandidateScore:
    for name, value in (
        ("input tokens", input_tokens),
        ("output tokens", output_tokens),
        ("cached input tokens", cached_input_tokens),
        ("credits", credits_used),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    tier = verification_tier(validation)
    failures: list[str] = []
    if tier < config.required_verification_tier:
        failures.append("VERIFICATION_TIER")

    try:
        estimated = _number(
            clock.get("estimated_period_ns"),
            name="estimated clock period",
            positive=True,
        )
        maximum = _number(
            clock.get("maximum_period_ns"),
            name="maximum clock period",
            positive=True,
        )
        if clock.get("passed") is not True or estimated > maximum:
            failures.append("CLOCK_CONSTRAINT")
    except ValueError:
        failures.append("CLOCK_CONSTRAINT")

    baseline_values: dict[str, float] | None = None
    candidate_values: dict[str, float] | None = None
    try:
        baseline_values = _metric_values(baseline)
    except ValueError as exc:
        text = str(exc).upper()
        failures.append("INVALID_LATENCY" if "LATENCY" in text else "INVALID_BASELINE_METRICS")
    try:
        candidate_values = _metric_values(candidate)
    except ValueError as exc:
        text = str(exc).upper()
        if "LATENCY" in text:
            failures.append("INVALID_LATENCY")
        elif "II" in text:
            failures.append("INVALID_II")
        else:
            failures.append("INVALID_CANDIDATE_METRICS")

    components: dict[str, float] = {}
    if baseline_values is not None and candidate_values is not None:
        components["latency"] = (
            candidate_values["latency"] / baseline_values["latency"]
        )
        components["ii"] = candidate_values["ii"] / baseline_values["ii"]
        for key in _RESOURCE_NAMES:
            available = baseline_values[f"available_{key}"]
            if not math.isclose(
                candidate_values[f"available_{key}"],
                available,
                rel_tol=0.0,
                abs_tol=0.0,
            ):
                failures.append(f"AVAILABLE_{key.upper()}_CHANGED")
            utilization = 100.0 * candidate_values[key] / available
            if utilization > config.max_resource_percent[key]:
                failures.append(f"{key.upper()}_LIMIT")
            components[key] = (
                1.0
                + candidate_values[key] / available
                - baseline_values[key] / available
            )

    failures = list(dict.fromkeys(failures))
    hard_passed = not failures
    ppa_cost = None
    if hard_passed:
        ppa_cost = sum(
            config.weights[key] * components[key] for key in _WEIGHT_KEYS
        )
        if not math.isfinite(ppa_cost):
            failures.append("INVALID_PPA_COST")
            hard_passed = False
            ppa_cost = None

    parsed_official = None
    if official_score is not None:
        parsed_official = _number(
            official_score, name="official score", positive=False
        )
    if config.official_score_enabled and parsed_official is None:
        failures.append("OFFICIAL_SCORE_MISSING")
        hard_passed = False
        ppa_cost = None

    return CandidateScore(
        candidate_id=candidate_id,
        verification_tier=tier,
        hard_constraints_passed=hard_passed,
        hard_failures=tuple(failures),
        ppa_cost=ppa_cost,
        components=MappingProxyType(components),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        credits_used=credits_used,
        official_score=parsed_official,
    )


def _comparison_key(score: CandidateScore) -> tuple[object, ...]:
    official = (
        -score.official_score
        if score.official_score is not None
        else math.inf
    )
    return (
        -score.verification_tier,
        0 if score.hard_constraints_passed else 1,
        official,
        score.ppa_cost if score.ppa_cost is not None else math.inf,
        score.tokens_used,
        score.credits_used,
        score.candidate_id,
    )


def compare_scores(candidate: CandidateScore, incumbent: CandidateScore) -> Comparison:
    candidate_key = _comparison_key(candidate)
    incumbent_key = _comparison_key(incumbent)
    winner = candidate if candidate_key < incumbent_key else incumbent
    labels = (
        "VERIFICATION_TIER",
        "HARD_CONSTRAINTS",
        "OFFICIAL_SCORE",
        "PPA_COST",
        "TOKEN_COST",
        "CREDIT_COST",
        "STABLE_ID",
    )
    reason = "IDENTICAL"
    for label, left, right in zip(labels, candidate_key, incumbent_key):
        if left != right:
            reason = label
            break
    return Comparison(
        winner=winner.candidate_id,
        strictly_better=winner is candidate,
        reason=reason,
        candidate_key=candidate_key,
        incumbent_key=incumbent_key,
    )
