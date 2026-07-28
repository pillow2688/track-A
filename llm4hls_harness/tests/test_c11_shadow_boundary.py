from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.budget import TokenBudgetLimits, TokenBudgetPolicy, TokenEstimator
from llm4hls_agent.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleOptimizationProvider,
)
from llm4hls_agent.v3_experience_kb import (
    QUERY_SAFE_FIELDS,
    QUERY_STRUCTURE_SAFE_FIELDS,
    build_kb_query,
)
from llm4hls_agent.v3_openai_planner import OpenAICompatibleV3PlannerAdapter
from llm4hls_agent.v3_planner import build_planner_input
from llm4hls_agent.v3_shadow_boundary import (
    COST_SCOPE,
    FORBIDDEN_QUERY_LABELS,
    LABEL_LAYERS,
    ProvenancePool,
    classify_provenance,
    formal_matrix_shadow_decision,
    query_boundary_report,
)


class _Coordinator:
    recommendation_path = None

    def __init__(self) -> None:
        self.persisted: list[int] = []

    def snapshot_metadata(self):
        return {
            "schema_version": "v3e.experience-snapshot.v1",
            "byte_offset": 0,
            "prefix_sha256": "0" * 64,
            "record_count": 0,
        }

    def fingerprint(self):
        return {"prefix_sha256": "0" * 64, "record_count": 0}

    def build_guidance(self, **_facts):
        return {
            "schema_version": "v3e.experience-guidance.v1",
            "similar_successes": [],
            "similar_failures": [],
            "recommended_strategy_bundles": [
                {
                    "strategy_bundle": ["FIX_LOOP_BOUND"],
                    "context": "MODE_SUBTYPE_PROFILE_BACKOFF",
                    "attempts": 4,
                    "successes": 4,
                    "utility": 0.5,
                }
            ],
            "discouraged_strategy_bundles": [
                {"strategy_bundle": ["OTHER_FUNCTIONAL_REPAIR"], "utility": 0.1}
            ],
            "confidence": 0.25,
            "supporting_record_ids": [],
            "fallback_reason": None,
            "notice": "Historical advice only.",
        }

    def persist_recommendation(self, round_index, _guidance):
        self.persisted.append(round_index)
        return {"persisted": True}


def _planner_input(root: Path) -> dict[str, object]:
    source_path = root / "candidates" / "candidate_000" / "kernel.cpp"
    source_path.parent.mkdir(parents=True)
    source = b"void top(int *a) { a[0] = 0; }\n"
    source_path.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    candidate = {
        "candidate_id": "candidate_000",
        "parent_id": None,
        "kind": "baseline",
        "status": "BASELINE",
        "source": {
            "ref": str(source_path.relative_to(root)),
            "sha256": digest,
        },
        "code_hash": digest,
        "metrics": {"ref": None, "sha256": None},
        "synth_evidence": {"ref": None, "sha256": None},
        "validation": {},
    }
    return build_planner_input(
        task={
            "task_id": "c11-public-fixture",
            "task_type": "repair",
            "difficulty": 1,
            "top": "top",
            "part": "xcu55c-fsvh2892-2L-e",
            "clock_ns": 10.0,
            "requires_cosim": False,
            "initial_condition": "repair public behavior",
            "description": "A public C1.1 boundary fixture.",
            "kernel_file": "kernel.cpp",
            "public_tb": "kernel_tb.cpp",
        },
        round_state={
            "round_index": 1,
            "rounds_completed": 0,
            "parent_candidate_id": "candidate_000",
            "mode": "REPAIR",
            "failure_evidence": {
                "schema_version": "v3c.csim-failure-evidence.v1",
                "failure_kind": "runtime_fail",
                "error_summary": "public mismatch",
                "source_locations": ["kernel.cpp:1"],
                "relevant_log_lines": ["expected 1 actual 0"],
            },
        },
        incumbent=candidate,
        baseline=candidate,
        history=[],
        policy={"max_optimization_rounds": 2},
        budget={
            "run_token_limit": 6000,
            "token_limit": 6000,
            "tokens_used": 100,
            "tokens_remaining": 5900,
            "credits_remaining": 80,
        },
    )


def _provider() -> OpenAICompatibleOptimizationProvider:
    return OpenAICompatibleOptimizationProvider(
        OpenAICompatibleConfig(
            base_url="https://llm.example/v1",
            api_key="fixture-secret",
            model="fixture-c11",
            max_output_tokens=512,
        ),
        transport=lambda _request, _timeout: (500, {}, b"{}"),
    )


class C11ShadowBoundaryTests(unittest.TestCase):
    def test_query_uses_exact_public_whitelists(self) -> None:
        report = query_boundary_report(
            top_level_fields=set(QUERY_SAFE_FIELDS),
            structure_fields=set(QUERY_STRUCTURE_SAFE_FIELDS),
        )
        self.assertTrue(report["safe"])
        self.assertFalse(
            set(QUERY_SAFE_FIELDS).intersection(FORBIDDEN_QUERY_LABELS)
        )
        self.assertEqual(COST_SCOPE["QUERY_ALLOWED"], ())
        self.assertEqual(set(LABEL_LAYERS), {
            "L0_PRE_DECISION_PUBLIC_FEATURES",
            "L1_HISTORICAL_SUPPORT_LABELS",
            "L2_HELD_OUT_EVALUATION_LABELS",
            "L3_POST_TERMINAL_ATTRIBUTION",
        })

    def test_nested_query_features_are_fail_closed(self) -> None:
        base = {
            "mode": "OPTIMIZE",
            "task_split": "train",
            "bottleneck_subtype": "SERIAL_REDUCTION",
            "algorithm_family": "DOT_PRODUCT",
            "task_family_hash": "f" * 64,
        }
        with self.assertRaisesRegex(ValueError, "non-whitelisted"):
            build_kb_query(**base, structure_features={"cost": 1})
        with self.assertRaisesRegex(ValueError, "unsafe text"):
            build_kb_query(
                **base,
                structure_features={"latency_bucket": "hidden answer"},
            )

    def test_provenance_pools_are_disjoint_and_matrix_is_delayed(self) -> None:
        self.assertEqual(
            classify_provenance(
                evidence_level="REAL_LLM_VITIS",
                task_split="train",
                eligible_for_ranking=True,
            ),
            ProvenancePool.TRAIN_SUPPORT,
        )
        self.assertEqual(
            classify_provenance(
                evidence_level="REAL_LLM_VITIS",
                task_split="dev",
                eligible_for_ranking=True,
            ),
            ProvenancePool.DEV_EVALUATION_ONLY,
        )
        self.assertEqual(
            classify_provenance(
                evidence_level="DETERMINISTIC_FIXTURE",
                task_split="train",
                eligible_for_ranking=False,
            ),
            ProvenancePool.FIXTURE_EXCLUDED,
        )
        self.assertEqual(
            classify_provenance(
                evidence_level="REAL_LLM_VITIS",
                task_split="train",
                eligible_for_ranking=False,
            ),
            ProvenancePool.TRAIN_OBSERVATION_ONLY,
        )
        self.assertEqual(
            classify_provenance(
                evidence_level="REAL_LLM_VITIS",
                task_split="train",
                eligible_for_ranking=True,
                current_matrix_record=True,
            ),
            ProvenancePool.CURRENT_MATRIX_POST_TERMINAL_ONLY,
        )

    def test_off_shadow_fixed_request_and_execution_identity_are_equal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner_input = _planner_input(root)
            coordinator = _Coordinator()
            off = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="off",
            )
            shadow = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="shadow",
                experience_coordinator=coordinator,
                experience_task_split="train",
            )
            off_call = off.prepare(planner_input)
            shadow_call = shadow.prepare(planner_input)
            self.assertEqual(
                off_call.request["provider_request"],
                shadow_call.request["provider_request"],
            )
            self.assertEqual(off_call.dispatch_context, shadow_call.dispatch_context)
            self.assertEqual(off.fingerprint(), shadow.fingerprint())
            self.assertEqual(coordinator.persisted, [1])

    def test_dynamic_policy_shadow_is_a_fully_equivalent_sidecar(self) -> None:
        policy = TokenBudgetPolicy(
            TokenBudgetLimits(
                configured_max_output_tokens=400,
                minimum_viable_output_tokens=128,
                provider_hard_output_cap=512,
                context_window_tokens=8192,
                future_round_token_reserve=300,
                configured_guidance_cap=600,
            )
        )
        estimator = TokenEstimator(
            tokenizer=lambda text: text.split(),
            estimator_name="c11-fixture-tokenizer",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner_input = _planner_input(root)
            off = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="off",
                token_budget_policy=policy,
                token_estimator=estimator,
            )
            shadow = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="shadow",
                experience_coordinator=_Coordinator(),
                experience_task_split="train",
                token_budget_policy=policy,
                token_estimator=estimator,
            )
            off_call = off.prepare(planner_input)
            shadow_call = shadow.prepare(planner_input)
            self.assertEqual(
                off_call.request["token_envelope"],
                shadow_call.request["token_envelope"],
            )
            self.assertEqual(
                off_call.request["provider_request"],
                shadow_call.request["provider_request"],
            )
            self.assertEqual(off_call.dispatch_context, shadow_call.dispatch_context)
            self.assertEqual(off.fingerprint(), shadow.fingerprint())
            decision = formal_matrix_shadow_decision(
                fixed_provider_request_equal=True,
                dynamic_provider_request_equal=True,
                dispatch_context_equal=True,
                planner_fingerprint_equal=True,
                graph_route_equal=True,
                ranker_runtime_imported=False,
            )
            self.assertTrue(decision["runtime_shadow_admitted"])
            self.assertEqual(decision["formal_matrix"]["experience"], "shadow")
            self.assertEqual(decision["formal_matrix"]["ranker"], "shadow")

    def test_guided_recommend_injects_only_one_short_strategy_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            call = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="guided",
                experience_coordinator=_Coordinator(),
                experience_task_split="train",
            ).prepare(_planner_input(root))

        prompt = call.request["provider_request"]["http_body"]["messages"][1][
            "content"
        ]
        self.assertIn("v3e.top1-strategy-card.v1", prompt)
        self.assertIn('"strategy_atom": "FIX_LOOP_BOUND"', prompt)
        self.assertIn('"source_family_count": 4', prompt)
        self.assertIn('"avoid_or_high_risk": ["OTHER_FUNCTIONAL_REPAIR"]', prompt)
        self.assertNotIn("supporting_record_ids", prompt)
        self.assertNotIn("similar_successes", prompt)

    def test_guided_abstain_does_not_change_provider_request(self) -> None:
        class AbstainingCoordinator(_Coordinator):
            def build_guidance(self, **facts):
                guidance = super().build_guidance(**facts)
                guidance["recommended_strategy_bundles"] = []
                guidance["fallback_reason"] = "NO_SAFE_VERIFIED_STRATEGY"
                return guidance

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner_input = _planner_input(root)
            off = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="off",
            ).prepare(planner_input)
            guided = OpenAICompatibleV3PlannerAdapter(
                root,
                _provider(),
                fast_experiment=True,
                experience_mode="guided",
                experience_coordinator=AbstainingCoordinator(),
                experience_task_split="train",
            ).prepare(planner_input)

        self.assertEqual(
            off.request["provider_request"],
            guided.request["provider_request"],
        )

    def test_ranker_v3_is_reachable_only_through_explicit_cli_adapter(self) -> None:
        package = Path(__file__).resolve().parents[1] / "llm4hls_agent"
        entrypoints = (
            "v3_prototype.py",
            "v3_prototype_cli.py",
            "v3_openai_planner.py",
            "v3_phase_router.py",
            "v3_batch_benchmark.py",
        )
        imported: list[str] = []
        for name in entrypoints:
            tree = ast.parse((package / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
        self.assertFalse(
            any(item.endswith("v3_strategy_ranker_v2") for item in imported)
        )
        self.assertFalse(
            any(item.endswith("v3_strategy_ranker_v3") for item in imported)
        )
        cli = ast.parse(
            (package / "v3_prototype_cli.py").read_text(encoding="utf-8")
        )
        cli_imports = [
            node.module
            for node in ast.walk(cli)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        self.assertTrue(
            any(
                item.endswith("v3_experience_v2_runtime")
                for item in cli_imports
            )
        )


if __name__ == "__main__":
    unittest.main()
