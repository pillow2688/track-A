from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_horizontal_online_pilot import (
    build_pilot_config,
    environment_preflight,
    pilot_plan,
)


class HorizontalOnlinePilotTests(unittest.TestCase):
    def test_preflight_never_serializes_secret_values(self) -> None:
        value = environment_preflight(
            {
                "LLM4HLS_KEY_ROTATED_AFTER_DISCLOSURE": "1",
                "OPENAI_API_KEY": "must-never-be-serialized",
                "OPENAI_BASE_URL": "https://provider.invalid",
                "LLM4HLS_MODEL": "model",
                "LLM4HLS_VITIS_HLS_ROOT": "/definitely/missing",
            }
        )
        encoded = json.dumps(value)
        self.assertNotIn("must-never-be-serialized", encoded)
        self.assertNotIn("https://provider.invalid", encoded)
        self.assertFalse(value["ready"])
        self.assertFalse(value["secret_values_serialized"])

    def test_explicit_user_risk_acceptance_is_recorded_not_mislabeled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vitis_run = Path(directory) / "bin" / "vitis-run"
            vitis_run.parent.mkdir()
            vitis_run.write_text("#!/bin/sh\n", encoding="utf-8")
            vitis_run.chmod(0o755)
            value = environment_preflight(
                {
                    "OPENAI_API_KEY": "current-disclosed-key",
                    "OPENAI_BASE_URL": "https://provider.invalid",
                    "LLM4HLS_MODEL": "model",
                    "LLM4HLS_VITIS_HLS_ROOT": directory,
                },
                allow_disclosed_key=True,
            )
            self.assertTrue(value["ready"])
            self.assertFalse(value["checks"]["key_rotation_marker"])
            self.assertTrue(
                value["checks"]["user_authorized_disclosed_key"]
            )
            self.assertEqual(
                value["credential_authority"],
                "USER_ACCEPTED_DISCLOSED_KEY_RISK",
            )
            encoded = json.dumps(value)
            self.assertNotIn("current-disclosed-key", encoded)

    def test_plan_is_exactly_four_public_shadow_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "experience-v2.jsonl"
            seed.write_text("", encoding="utf-8")
            config = build_pilot_config(
                output_dir=Path(directory) / "output",
                experience_store=seed,
                model="scheduled-model",
            )
            value = pilot_plan(config)
            self.assertEqual(len(config.corpus), 4)
            self.assertEqual(set(value["expected_mode_by_task"].values()), {
                "REPAIR",
                "SYNTH_FIX",
                "STRUCTURAL_FIX",
                "OPTIMIZE",
            })
            self.assertEqual(config.max_tasks, 4)
            self.assertEqual(config.repeats, 1)
            self.assertEqual(config.continuation_policy_mode, "shadow")
            self.assertEqual(config.continuation_policy_version, "v2")
            self.assertEqual(config.experience_mode, "shadow")
            self.assertEqual(config.experience_ranker_version, "v3")
            self.assertEqual(
                config.final_validation_policy,
                "full_internal_audit",
            )
            self.assertEqual(config.max_planner_rounds, 3)
            self.assertTrue(config.enable_final_fallback)
            self.assertTrue(value["does_not_launch_28_tasks"])
            self.assertFalse(value["authority"]["prompt_injection"])
            self.assertFalse(value["authority"]["enforce"])


if __name__ == "__main__":
    unittest.main()
