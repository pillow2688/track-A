from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from llm4hls_agent.scoring import (
    CandidateScore,
    OFFICIAL_ACCELERATION_CAP,
    OFFICIAL_SCORE_SOURCE,
    OFFICIAL_SCORE_VERSION,
    ScoringConfig,
    compare_scores,
    estimate_official_score_proxy,
    load_scoring_config,
    score_candidate,
)


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "scoring.yaml"
        self.config_value = {
            "schema_version": 1,
            "ppa_weights": {
                "latency": 0.45,
                "ii": 0.35,
                "lut": 0.06,
                "ff": 0.04,
                "dsp": 0.04,
                "bram": 0.04,
                "uram": 0.02,
            },
            "max_resource_percent": {
                "lut": 100.0,
                "ff": 100.0,
                "dsp": 100.0,
                "bram": 100.0,
                "uram": 100.0,
            },
            "required_verification_tier": 4,
            "official_score": {"enabled": False},
        }
        self.config_path.write_text(
            json.dumps(self.config_value), encoding="utf-8"
        )
        self.config = load_scoring_config(self.config_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def metrics(
        *,
        latency: int = 100,
        ii: int = 10,
        lut: int = 100,
        ff: int = 200,
        dsp: int = 0,
        bram: int = 0,
        uram: int = 0,
    ) -> dict[str, object]:
        return {
            "estimated_clock_period_ns": 5.0,
            "latency": {"best": latency, "average": latency, "worst": latency},
            "interval": {"min": ii, "max": ii},
            "resources": {
                "LUT": lut,
                "FF": ff,
                "DSP": dsp,
                "BRAM_18K": bram,
                "URAM": uram,
            },
            "available_resources": {
                "LUT": 1000,
                "FF": 2000,
                "DSP": 100,
                "BRAM_18K": 100,
                "URAM": 50,
            },
        }

    @staticmethod
    def validation(tier: int = 4) -> dict[str, object]:
        values: dict[str, object] = {
            "static": {"status": "PASS"},
            "csim": {"status": "NOT_RUN"},
            "synth": {"status": "NOT_RUN"},
            "cosim": {"status": "NOT_RUN"},
        }
        if tier >= 2:
            values["csim"] = {"status": "PASS"}
        if tier >= 3:
            values["synth"] = {"status": "PASS"}
        if tier >= 4:
            values["cosim"] = {"status": "PASS"}
        return values

    @staticmethod
    def clock(passed: bool = True) -> dict[str, object]:
        return {
            "passed": passed,
            "estimated_period_ns": 5.0,
            "maximum_period_ns": 10.0,
        }

    def score(
        self,
        candidate_id: str,
        *,
        baseline: dict[str, object] | None = None,
        candidate: dict[str, object] | None = None,
        tier: int = 4,
        clock_passed: bool = True,
        tokens: int = 100,
        credits: int = 25,
    ) -> CandidateScore:
        return score_candidate(
            candidate_id=candidate_id,
            baseline=baseline or self.metrics(),
            candidate=candidate or self.metrics(),
            validation=self.validation(tier),
            clock=self.clock(clock_passed),
            config=self.config,
            input_tokens=tokens,
            output_tokens=0,
            cached_input_tokens=0,
            credits_used=credits,
        )

    def test_loads_json_compatible_yaml_and_requires_weights_sum_to_one(self) -> None:
        self.assertEqual(self.config.weights["latency"], 0.45)
        self.assertAlmostEqual(sum(self.config.weights.values()), 1.0)
        self.assertEqual(self.config.official_score_version, OFFICIAL_SCORE_VERSION)
        self.assertEqual(
            self.config.official_acceleration_cap, OFFICIAL_ACCELERATION_CAP
        )
        self.assertEqual(
            self.config.to_dict()["official_score"],
            {
                "enabled": False,
                "version": "public_proxy_v1",
                "acceleration_cap": 8.0,
            },
        )

        invalid = dict(self.config_value)
        invalid["ppa_weights"] = dict(self.config_value["ppa_weights"])
        invalid["ppa_weights"]["latency"] = 0.46
        self.config_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sum"):
            load_scoring_config(self.config_path)

    def test_versioned_official_proxy_config_rejects_formula_drift(self) -> None:
        versioned = dict(self.config_value)
        versioned["official_score"] = {
            "enabled": True,
            "version": OFFICIAL_SCORE_VERSION,
            "acceleration_cap": OFFICIAL_ACCELERATION_CAP,
        }
        self.config_path.write_text(json.dumps(versioned), encoding="utf-8")

        loaded = load_scoring_config(self.config_path)

        self.assertTrue(loaded.official_score_enabled)
        self.assertEqual(loaded.to_dict()["official_score"], versioned["official_score"])

        for field, invalid_value, message in (
            ("version", "public_proxy_v2", "version"),
            ("acceleration_cap", 4.0, "acceleration cap"),
        ):
            with self.subTest(field=field):
                invalid = dict(versioned)
                invalid["official_score"] = dict(versioned["official_score"])
                invalid["official_score"][field] = invalid_value
                self.config_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    load_scoring_config(self.config_path)

    def test_official_proxy_config_requires_complete_version_tuple(self) -> None:
        incomplete = dict(self.config_value)
        incomplete["official_score"] = {
            "enabled": True,
            "version": OFFICIAL_SCORE_VERSION,
        }
        self.config_path.write_text(json.dumps(incomplete), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "configuration"):
            load_scoring_config(self.config_path)

    def test_zero_baseline_resource_is_finite(self) -> None:
        score = self.score(
            "candidate_001",
            baseline=self.metrics(dsp=0, bram=0, uram=0),
            candidate=self.metrics(dsp=1, bram=0, uram=0),
        )

        self.assertTrue(math.isfinite(float(score.ppa_cost)))
        self.assertGreater(score.components["dsp"], 1.0)

    def test_verification_outranks_better_ppa(self) -> None:
        low_tier = self.score(
            "candidate_001",
            candidate=self.metrics(latency=10, ii=1),
            tier=3,
        )
        high_tier = self.score(
            "candidate_002",
            candidate=self.metrics(latency=120, ii=12),
            tier=4,
        )

        comparison = compare_scores(high_tier, low_tier)
        self.assertEqual(comparison.winner, "candidate_002")
        self.assertEqual(comparison.reason, "VERIFICATION_TIER")

    def test_hard_constraint_outranks_better_ppa(self) -> None:
        violating = self.score(
            "candidate_001",
            candidate=self.metrics(latency=10, ii=1),
            clock_passed=False,
        )
        eligible = self.score("candidate_002")

        comparison = compare_scores(eligible, violating)
        self.assertEqual(comparison.winner, "candidate_002")
        self.assertEqual(comparison.reason, "HARD_CONSTRAINTS")

    def test_lower_ppa_outranks_lower_cost(self) -> None:
        faster = self.score(
            "candidate_001",
            candidate=self.metrics(latency=80, ii=8),
            tokens=500,
        )
        cheaper = self.score("candidate_002", tokens=10)

        comparison = compare_scores(faster, cheaper)
        self.assertEqual(comparison.winner, "candidate_001")
        self.assertEqual(comparison.reason, "PPA_COST")

    def test_cost_breaks_only_equal_ppa(self) -> None:
        cheap = self.score("candidate_001", tokens=100, credits=25)
        costly = self.score("candidate_002", tokens=200, credits=25)

        comparison = compare_scores(cheap, costly)
        self.assertEqual(comparison.winner, "candidate_001")
        self.assertEqual(comparison.reason, "TOKEN_COST")

    def test_comparison_is_strict_json_without_non_finite_sentinels(self) -> None:
        candidate = self.score("candidate_001")
        incumbent = self.score("candidate_000")

        encoded = json.dumps(
            compare_scores(candidate, incumbent).to_dict(),
            allow_nan=False,
        )

        self.assertNotIn("Infinity", encoded)

    def test_missing_metric_fails_closed(self) -> None:
        candidate = self.metrics()
        candidate["latency"] = {"worst": None}

        score = self.score("candidate_001", candidate=candidate)

        self.assertFalse(score.hard_constraints_passed)
        self.assertIsNone(score.ppa_cost)
        self.assertIn("INVALID_LATENCY", score.hard_failures)

    def test_official_public_proxy_matches_formula_and_eight_x_cap(self) -> None:
        baseline = self.metrics(latency=800)
        validation = self.validation(4)

        four_x = estimate_official_score_proxy(
            difficulty=2,
            baseline=baseline,
            candidate=self.metrics(latency=200),
            validation=validation,
            requires_cosim=True,
        )
        beyond_cap = estimate_official_score_proxy(
            difficulty=2,
            baseline=baseline,
            candidate=self.metrics(latency=50),
            validation=validation,
            requires_cosim=True,
        )

        self.assertEqual(four_x, 1.7)
        self.assertEqual(beyond_cap, 2.0)

    def test_official_public_proxy_is_zero_until_required_cosim_passes(self) -> None:
        tier_three = self.validation(3)
        kwargs = {
            "difficulty": 3,
            "baseline": self.metrics(latency=800),
            "candidate": self.metrics(latency=400),
            "validation": tier_three,
            "requires_cosim": True,
        }

        self.assertEqual(estimate_official_score_proxy(**kwargs), 0.0)
        self.assertEqual(
            estimate_official_score_proxy(**kwargs, provisional_cosim=True),
            2.325,
        )

    def test_enabled_official_score_is_required_and_outranks_internal_ppa(self) -> None:
        official_config = replace(self.config, official_score_enabled=True)
        missing = score_candidate(
            candidate_id="candidate_missing",
            baseline=self.metrics(),
            candidate=self.metrics(),
            validation=self.validation(4),
            clock=self.clock(),
            config=official_config,
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=0,
            credits_used=0,
        )
        self.assertFalse(missing.hard_constraints_passed)
        self.assertIn("OFFICIAL_SCORE_MISSING", missing.hard_failures)

        faster_but_larger = score_candidate(
            candidate_id="candidate_fast",
            baseline=self.metrics(latency=100, ii=10, lut=100),
            candidate=self.metrics(latency=50, ii=20, lut=900),
            validation=self.validation(4),
            clock=self.clock(),
            config=official_config,
            input_tokens=100,
            output_tokens=0,
            cached_input_tokens=0,
            credits_used=25,
            official_score=0.8,
            official_score_source=OFFICIAL_SCORE_SOURCE,
        )
        smaller_but_slower = score_candidate(
            candidate_id="candidate_small",
            baseline=self.metrics(latency=100, ii=10, lut=100),
            candidate=self.metrics(latency=60, ii=5, lut=100),
            validation=self.validation(4),
            clock=self.clock(),
            config=official_config,
            input_tokens=100,
            output_tokens=0,
            cached_input_tokens=0,
            credits_used=25,
            official_score=0.79,
            official_score_source=OFFICIAL_SCORE_SOURCE,
        )

        comparison = compare_scores(faster_but_larger, smaller_but_slower)
        self.assertEqual(comparison.winner, "candidate_fast")
        self.assertEqual(comparison.reason, "OFFICIAL_SCORE")


if __name__ == "__main__":
    unittest.main()
