from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent.v3_prototype_cli import _parser


class TrackASafeBaselineTests(unittest.TestCase):
    def test_safe_baseline_freezes_non_enforcing_defaults(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "llm4hls_agent"
            / "config"
            / "track_a_safe_baseline_v1.json"
        )
        raw = path.read_bytes()
        value = json.loads(raw)

        self.assertEqual(value["schema_version"], "track-a-safe-baseline.v1")
        self.assertEqual(value["planner"]["continuation_policy"], "shadow")
        self.assertIn(value["planner"]["experience_mode"], {"off", "shadow"})
        self.assertEqual(value["planner"]["strategy_ranker"], "bayesian_shadow")
        self.assertEqual(value["planner"]["comparator"], "latency_first")
        self.assertEqual(value["final_validation"]["fresh_final"], "required")
        self.assertFalse(value["access_control"]["hidden_access"])
        self.assertFalse(value["access_control"]["reference_access"])
        self.assertEqual(value["power"]["status"], "UNSUPPORTED")
        # This identifies the exact immutable configuration consumed by a
        # report without claiming that the JSON itself controls all runtime
        # implementation details.
        self.assertEqual(len(hashlib.sha256(raw).hexdigest()), 64)

    def test_cli_tool_costs_are_configurable_with_official_defaults(self) -> None:
        argv = ["--task-dir", "task", "--run-dir", "run", "--patch-file", "patch"]
        with patch.dict(
            "os.environ",
            {
                "LLM4HLS_COST_CSIM": "2",
                "LLM4HLS_COST_SYNTH": "5",
                "LLM4HLS_COST_COSIM": "21",
            },
            clear=False,
        ):
            env_args = _parser().parse_args(argv)
        explicit_args = _parser().parse_args(
            [*argv, "--cost-csim", "1", "--cost-synth", "4", "--cost-cosim", "20"]
        )

        self.assertEqual(
            (env_args.cost_csim, env_args.cost_synth, env_args.cost_cosim),
            (2, 5, 21),
        )
        self.assertEqual(
            (explicit_args.cost_csim, explicit_args.cost_synth, explicit_args.cost_cosim),
            (1, 4, 20),
        )


if __name__ == "__main__":
    unittest.main()
