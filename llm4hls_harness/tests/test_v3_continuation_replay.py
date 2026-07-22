from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_continuation_replay import evaluate_replay


class ContinuationReplayTests(unittest.TestCase):
    def test_evaluator_reads_only_pre_state_and_reports_descriptive_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dataset.jsonl"
            row = {
                "pre_state": {
                    "mode": "OPTIMIZE", "round_index": 2,
                    "has_correct_candidate": True,
                    "parent_metrics": {
                        "latency": {"worst": 100}, "interval": {"max": 1},
                        "resources": {"LUT": 1, "FF": 1, "DSP": 1, "BRAM_18K": 1, "URAM": 1},
                        "available_resources": {"LUT": 10, "FF": 10, "DSP": 10, "BRAM_18K": 10, "URAM": 10},
                    },
                    "attempted_strategy_atoms": ["LOOP_UNROLL"],
                    "budget": {"tokens_remaining": 1000, "credits_remaining": 50},
                },
                "outcome": {
                    "label": "BENEFICIAL_PERFORMANCE",
                    "future_candidate": {"must_not_be_read": "hidden value"},
                },
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            value = evaluate_replay(path)
            self.assertEqual(value["followup_total"], 1)
            self.assertEqual(value["status"], "DESCRIPTIVE_ONLY")
            self.assertEqual(value["decisions"]["block"], 1)

