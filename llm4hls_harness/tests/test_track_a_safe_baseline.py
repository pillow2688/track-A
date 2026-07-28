from __future__ import annotations

import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent.budget import resolve_reference_tool_cost
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
        self.assertEqual(
            value["artifact_kind"], "auditable_freeze_default_manifest"
        )
        self.assertFalse(value["runtime_loader"])
        self.assertEqual(
            value["runtime_authority"],
            "CLI parser defaults plus contract tests",
        )
        self.assertEqual(value["planner"]["token_policy"], "fixed")
        self.assertFalse(value["planner"]["dynamic_token_policy"]["default"])
        self.assertEqual(
            value["planner"]["dynamic_token_policy"]["formal_runtime_status"],
            "EVALUATION_HARNESS_ONLY_NOT_PRODUCT_CLI",
        )
        self.assertEqual(value["planner"]["evidence_memory"], "on")
        self.assertEqual(value["planner"]["continuation_policy"], "shadow")
        self.assertEqual(
            value["planner"]["continuation_external_version"], "v2"
        )
        self.assertEqual(
            value["planner"]["continuation_implementation_version"],
            "v3.continuation-policy.v3",
        )
        self.assertFalse(value["planner"]["continuation_enforce_default"])
        self.assertIn(value["planner"]["experience_mode"], {"off", "shadow"})
        self.assertEqual(
            value["planner"]["strategy_ranker"],
            "legacy_v1_bayesian_advisory_shadow",
        )
        self.assertFalse(
            value["planner"]["strategy_ranker_v3"]["guided_default"]
        )
        self.assertEqual(
            value["planner"]["strategy_ranker_v3"][
                "current_admission_status"
            ],
            "INVALIDATED_PENDING_TRAIN_ONLY_REEVALUATION",
        )
        self.assertEqual(value["planner"]["comparator"], "latency_first")
        self.assertEqual(
            value["component_profile"]["ablatable_innovations"],
            [
                "Structured Evidence Memory",
                "Budget-Aware Continuation",
                "Experience Strategy Advisor",
            ],
        )
        self.assertEqual(
            value["component_profile"]["always_on_foundations"],
            [
                "Candidate and Safety Baseline",
                "Independent Final Certification",
            ],
        )
        self.assertEqual(
            value["component_profile"]["non_runtime_support"],
            [
                "Offline Experience Pipeline",
                "Evaluation Harness",
            ],
        )
        self.assertIn(
            "dynamic token policy A/B/C",
            value["component_profile"]["evaluation_harness"],
        )
        self.assertIn(
            "strategy ranker v3 fixed evaluation, gate, and shadow reports",
            value["component_profile"]["evaluation_harness"],
        )
        self.assertIn(
            "experience importer",
            value["component_profile"]["offline_experience_pipeline"],
        )
        self.assertEqual(
            value["component_profile"]["retired_product_capabilities"],
            [
                "Continuation V1 decision and replay",
                "Bayesian Strategy Ranker V2",
                "Hybrid Token Policy V2",
                "SATURATED_PARALLEL_REDUCTION_BUNDLE_PENDING_FINAL",
                "final_reserve_credits product semantics",
            ],
        )
        self.assertFalse(
            value["runtime_boundaries"][
                "automatic_experience_postprocessing"
            ]
        )
        self.assertEqual(
            value["runtime_boundaries"]["experience_postprocess_entry"],
            "llm4hls-experience postprocess-run",
        )
        self.assertFalse(
            value["runtime_boundaries"][
                "offline_postprocessing_can_delay_certification"
            ]
        )
        self.assertFalse(
            value["runtime_boundaries"]["dynamic_token_policy_in_product_cli"]
        )
        controls = value["runtime_controls"]["ablatable_innovations"]
        a1 = controls["A1 Structured Evidence Memory"]
        self.assertEqual(a1["cli"], "--evidence-memory")
        self.assertEqual(a1["choices"], ["off", "on"])
        self.assertEqual(a1["safe_default"], "on")
        self.assertIn(
            "B1 evidence ref/hash binding", a1["off_semantics"]
        )
        self.assertIn(
            "Planner/Continuation duplicate-failure", a1["off_semantics"]
        )
        self.assertEqual(
            controls["A2 Budget-Aware Continuation"]["cli"],
            "--continuation-policy",
        )
        self.assertEqual(
            controls["A2 Budget-Aware Continuation"]["safe_default"],
            "shadow",
        )
        self.assertIn(
            "fixed score-aligned 8x stop",
            controls["A2 Budget-Aware Continuation"]["shadow_semantics"],
        )
        self.assertEqual(
            controls["A3 Experience Strategy Advisor"]["cli"],
            "--experience-mode",
        )
        self.assertEqual(
            controls["A3 Experience Strategy Advisor"][
                "runtime_default_ranker"
            ],
            "legacy_v1",
        )
        self.assertEqual(
            controls["A3 Experience Strategy Advisor"]["admission"],
            "fail_closed_no_current_train_only_admission",
        )
        self.assertEqual(
            value["runtime_controls"]["constraints"],
            ["evidence_memory=off requires continuation_policy=off"],
        )
        for foundation in value["runtime_controls"][
            "always_on_foundations"
        ].values():
            self.assertTrue(foundation["enabled"])
            self.assertFalse(foundation["ablatable"])
        expected_profiles = {
            "B0": ("off", "off", "off"),
            "B1": ("on", "off", "off"),
            "B2": ("on", "shadow", "off"),
            "B3": ("on", "shadow", "shadow"),
        }
        for profile_name, expected_modes in expected_profiles.items():
            arguments = value["ablation_profiles"][profile_name][
                "command_arguments"
            ]
            parsed = _parser().parse_args(
                [
                    "--task-dir",
                    "task",
                    "--run-dir",
                    "run",
                    "--patch-file",
                    "patch",
                    *arguments,
                ]
            )
            self.assertEqual(parsed.token_budget_policy, "fixed")
            self.assertEqual(parsed.evidence_memory, expected_modes[0])
            self.assertEqual(parsed.continuation_policy, expected_modes[1])
            self.assertEqual(parsed.experience_mode, expected_modes[2])
        self.assertEqual(
            value["tool_cost_basis"],
            "configurable_development_reference_not_official",
        )
        self.assertEqual(
            value["final_validation"]["budget_domain"], "agent_search"
        )
        self.assertEqual(
            value["final_validation"]["scope"],
            "search_closeout_candidate_confirmation",
        )
        self.assertEqual(value["final_validation"]["fresh_final"], "required")
        self.assertEqual(value["final_validation"]["policy"], "task_contract")
        self.assertEqual(
            value["final_validation"]["task_contract"]["requires_cosim_false"],
            ["csim", "synth"],
        )
        self.assertEqual(
            value["final_certification"]["budget_domain"],
            "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET",
        )
        self.assertEqual(
            value["final_certification"]["agent_credits_charged"], 0
        )
        self.assertEqual(
            value["final_certification"]["stages"],
            ["csim", "synth", "cosim"],
        )
        self.assertFalse(value["access_control"]["hidden_access"])
        self.assertFalse(value["access_control"]["reference_access"])
        self.assertEqual(value["power"]["status"], "UNSUPPORTED")
        # This identifies the exact immutable configuration consumed by a
        # report without claiming that the JSON itself controls all runtime
        # implementation details.
        self.assertEqual(len(hashlib.sha256(raw).hexdigest()), 64)

    def test_tool_costs_resolve_after_parse_with_recorded_provenance(self) -> None:
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
            resolved = tuple(
                resolve_reference_tool_cost(
                    tool=tool,
                    run_override=getattr(env_args, f"cost_{tool}"),
                    environment_variable=f"LLM4HLS_COST_{tool.upper()}",
                    reference_fallback=fallback,
                )
                for tool, fallback in (("csim", 1), ("synth", 4), ("cosim", 20))
            )
        explicit_args = _parser().parse_args(
            [*argv, "--cost-csim", "1", "--cost-synth", "4", "--cost-cosim", "20"]
        )

        self.assertEqual(
            (env_args.cost_csim, env_args.cost_synth, env_args.cost_cosim),
            (None, None, None),
        )
        self.assertEqual(
            tuple(item.value for item in resolved),
            (2, 5, 21),
        )
        self.assertEqual(
            tuple(item.source for item in resolved),
            (
                "env:LLM4HLS_COST_CSIM",
                "env:LLM4HLS_COST_SYNTH",
                "env:LLM4HLS_COST_COSIM",
            ),
        )
        self.assertEqual(
            (explicit_args.cost_csim, explicit_args.cost_synth, explicit_args.cost_cosim),
            (1, 4, 20),
        )
        self.assertEqual(env_args.final_validation_policy, "task_contract")
        self.assertEqual(env_args.token_budget_policy, "fixed")
        self.assertEqual(env_args.evidence_memory, "on")
        self.assertEqual(env_args.continuation_policy, "shadow")
        self.assertEqual(env_args.continuation_policy_version, "v2")
        self.assertEqual(env_args.experience_mode, "shadow")
        self.assertEqual(env_args.experience_ranker_version, "v1")

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args([*argv, "--token-budget-policy", "dynamic"])

    def test_retired_product_symbols_do_not_reenter_runtime_modules(self) -> None:
        package_root = (
            Path(__file__).resolve().parents[1] / "llm4hls_agent"
        )
        for retired_module in (
            "v3_continuation_replay.py",
            "v3_strategy_ranker_v2.py",
            "token_policy_historical_analysis.py",
            "token_policy_hybrid_aggregate.py",
            "token_policy_hybrid_runner.py",
        ):
            self.assertFalse((package_root / retired_module).exists())

        forbidden_runtime_symbols = (
            "v3_continuation_replay",
            "v3_strategy_ranker_v2",
            "token_policy_hybrid",
            "final_reserve_credits",
            "SATURATED_PARALLEL_REDUCTION_BUNDLE_PENDING_FINAL",
        )
        for source in sorted(package_root.glob("*.py")):
            text = source.read_text(encoding="utf-8")
            for symbol in forbidden_runtime_symbols:
                self.assertNotIn(symbol, text, f"{symbol} found in {source}")


if __name__ == "__main__":
    unittest.main()
