from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.scoring import (
    CandidateScore,
    ScoringConfig,
    compare_scores,
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

        invalid = dict(self.config_value)
        invalid["ppa_weights"] = dict(self.config_value["ppa_weights"])
        invalid["ppa_weights"]["latency"] = 0.46
        self.config_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sum"):
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


if __name__ == "__main__":
    unittest.main()
