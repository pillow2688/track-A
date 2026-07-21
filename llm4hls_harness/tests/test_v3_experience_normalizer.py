from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_experience_guidance import ExperienceFeatureExtractor
from llm4hls_agent.v3_experience_normalizer import (
    MigrationContext,
    StrategyNormalizer,
    classify_algorithm_family,
    infer_failure_subtype_from_observed,
    migrate_v1_to_v2,
    task_family_hash,
)


class StrategyNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = StrategyNormalizer()

    def test_repair_detects_loop_index_branch_accumulation_and_cast(self) -> None:
        parent = """void k(int a[16], int o[16]) { int acc=1; for(int i=0;i<15;i++){ if(a[i]>0) acc-=a[i]; o[i]=acc; } }"""
        candidate = """void k(int a[16], int o[16]) { int acc=0; for(int i=0;i<16;i++){ if(a[i]>=0) acc+=static_cast<int>(a[i]); o[i]=acc; } }"""
        patch = """--- a/kernel.cpp\n+++ b/kernel.cpp\n-int acc=1; for(int i=0;i<15;i++){ if(a[i]>0) acc-=a[i];\n+int acc=0; for(int i=0;i<16;i++){ if(a[i]>=0) acc+=static_cast<int>(a[i]);\n"""
        result = self.normalizer.normalize(mode="REPAIR", parent_source=parent, candidate_source=candidate, patch=patch)
        self.assertIn("FIX_LOOP_BOUND", result.observed_strategy_atoms)
        self.assertIn("FIX_BRANCH_CONDITION", result.observed_strategy_atoms)
        self.assertIn("FIX_ACCUMULATION", result.observed_strategy_atoms)

    def test_synth_detects_dynamic_stl_recursion_and_static_bound(self) -> None:
        cases = (
            ("int *p=new int[n]; delete[] p;", "int p[16];", "REMOVE_DYNAMIC_ALLOCATION"),
            ("std::vector<int> v;", "int v[16];", "REPLACE_UNSUPPORTED_STL"),
            ("int k(int n){return k(n-1);}", "int k(int n){return n;}", "REMOVE_RECURSION"),
            ("for(int i=0;i<n;i++){}", "constexpr int N=16; for(int i=0;i<N;i++){}", "STATICIZE_LOOP_BOUND"),
        )
        for old, new, expected in cases:
            patch = f"--- a/kernel.cpp\n+++ b/kernel.cpp\n-{old}\n+{new}\n"
            result = self.normalizer.normalize(mode="SYNTH_FIX", parent_source=old, candidate_source=new, patch=patch)
            self.assertIn(expected, result.observed_strategy_atoms, expected)

    def test_structural_detects_fifo_dataflow_order_and_interface(self) -> None:
        cases = (
            ("", "#pragma HLS STREAM variable=s depth=32", "INCREASE_FIFO_DEPTH"),
            ("#pragma HLS DATAFLOW", "", "REMOVE_UNSAFE_DATAFLOW"),
            ("producer(); consumer();", "consumer(); producer();", "REORDER_STREAM_OPERATIONS"),
            ("#pragma HLS INTERFACE ap_memory port=a", "#pragma HLS INTERFACE m_axi port=a", "FIX_INTERFACE_PROTOCOL"),
        )
        for old, new, expected in cases:
            patch = f"--- a/kernel.cpp\n+++ b/kernel.cpp\n-{old}\n+{new}\n"
            result = self.normalizer.normalize(mode="STRUCTURAL_FIX", parent_source=old, candidate_source=new, patch=patch)
            self.assertIn(expected, result.observed_strategy_atoms, expected)

    def test_optimize_detects_all_major_pragma_and_reduction_atoms(self) -> None:
        parent = "void k(float a[32], float *o){float acc=0; for(int i=0;i<32;i++) acc+=a[i];}"
        candidate = """void k(float a[32], float *o){
#pragma HLS ARRAY_PARTITION variable=a cyclic factor=4
#pragma HLS PIPELINE II=1
#pragma HLS UNROLL factor=4
float partial_sum[4]; for(int lane=0;lane<4;lane++) partial_sum[lane]=0;
}"""
        patch = "\n".join(["--- a/kernel.cpp", "+++ b/kernel.cpp"] + [f"+{line}" for line in candidate.splitlines()])
        result = self.normalizer.normalize(mode="OPTIMIZE", declared_strategy=["DATAFLOW"], parent_source=parent, candidate_source=candidate, patch=patch)
        self.assertIn("ARRAY_PARTITION", result.observed_strategy_atoms)
        self.assertIn("LOOP_PIPELINE", result.observed_strategy_atoms)
        self.assertIn("LOOP_UNROLL", result.observed_strategy_atoms)
        self.assertIn("DECLARED_OBSERVED_CONFLICT", result.reason_codes)

    def test_other_fallback_does_not_use_task_id(self) -> None:
        left = self.normalizer.normalize(mode="REPAIR", declared_strategy=None, patch="")
        right = self.normalizer.normalize(mode="REPAIR", declared_strategy=None, patch="")
        self.assertEqual(left, right)
        self.assertEqual(left.observed_strategy_atoms, ("OTHER_FUNCTIONAL_REPAIR",))

    def test_unambiguous_observed_fix_refines_only_coarse_failure(self) -> None:
        self.assertEqual(
            infer_failure_subtype_from_observed(
                "FUNCTIONAL_MISMATCH_OTHER", ("FIX_ARRAY_INDEX",)
            ),
            "WRONG_ARRAY_INDEX",
        )
        self.assertEqual(
            infer_failure_subtype_from_observed(
                "SYNTHESIS_ERROR_OTHER",
                ("REPLACE_UNSUPPORTED_STL", "REMOVE_RECURSION"),
            ),
            "SYNTHESIS_ERROR_OTHER",
        )
        self.assertEqual(
            infer_failure_subtype_from_observed("OFF_BY_ONE", ("FIX_LOOP_BOUND",)),
            "OFF_BY_ONE",
        )

    def test_algorithm_and_task_family_ignore_mutation_constants(self) -> None:
        first = "void kernel(int a[16], int o[16]) { for(int i=0;i<15;i++) o[i]=a[i]*3+7; }"
        second = "void kernel(int a[16], int o[16]) { for(int i=0;i<16;i++) o[i]=a[i]*2+1; }"
        structure = {
            "loop_count_bucket": "1",
            "has_dataflow": False,
            "has_stream": False,
            "has_fifo": False,
            "has_reduction": False,
            "memory_access_pattern": "SEQUENTIAL",
        }
        self.assertEqual(classify_algorithm_family(first), "VECTOR_ELEMENTWISE")
        self.assertEqual(
            task_family_hash(first, "VECTOR_ELEMENTWISE", structure),
            task_family_hash(second, "VECTOR_ELEMENTWISE", structure),
        )


class MigrationTests(unittest.TestCase):
    def v1_record(self) -> dict[str, object]:
        extractor = ExperienceFeatureExtractor()
        return extractor.build_record(
            run_id="migration-run",
            candidate_id="candidate_001",
            task_id="public-task-name",
            task_split="train",
            mode="OPTIMIZE",
            source="void kernel(float a[32], float *o){float acc=0; for(int i=0;i<32;i++)acc+=a[i];}",
            patch="--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS UNROLL factor=4\n",
            execution_class="REAL_LLM_VITIS",
            eligible_for_ranking=True,
            artifact_refs=[
                {
                    "role": "candidate_patch",
                    "ref": "candidates/candidate_001/patch.diff",
                    "sha256": "f" * 64,
                }
            ],
            strategy_bundle=["LOOP_UNROLL"],
            outcome={
                "patch_valid": True,
                "candidate_created": True,
                "csim_pass": True,
                "synth_pass": True,
                "cosim_status": "PASS",
                "final_pass": True,
                "promoted": True,
                "failure_stage": None,
                "latency_before": 100,
                "latency_after": 25,
                "acceleration": 4.0,
                "tokens": 1200,
                "credits": 30,
                "wall_time_seconds": 10.0,
            },
        )

    def test_v1_migration_is_immutable_stable_and_observed(self) -> None:
        v1 = self.v1_record()
        before = copy.deepcopy(v1)
        context = MigrationContext(
            parent_source="void kernel(float a[32], float *o){for(int i=0;i<32;i++){}}",
            candidate_source="void kernel(float a[32], float *o){#pragma HLS UNROLL factor=4\nfor(int i=0;i<32;i++){}}",
            patch="--- a/kernel.cpp\n+++ b/kernel.cpp\n+#pragma HLS UNROLL factor=4\n",
            provider="deepseek",
            model="deepseek-v4-pro",
            token_policy={
                "run_token_limit": 12000,
                "tokens_remaining_before_call": 8800,
                "estimated_base_prompt_tokens": 1700,
                "estimated_guidance_tokens": 180,
                "estimated_input_tokens": 1880,
                "configured_max_output_tokens": 2400,
                "effective_max_output_tokens": 1500,
                "actual_input_tokens": 1000,
                "actual_output_tokens": 200,
                "actual_total_tokens": 1200,
                "context_window_tokens": 32768,
                "future_round_token_reserve": 1800,
                "guidance_token_cap": 240,
                "guidance_actual_tokens": 180,
                "rounds_remaining": 2,
                "token_pressure": "MEDIUM",
                "finish_reason": "stop",
                "output_truncated": False,
                "truncation_reason": None,
                "estimator_name": "fixture-tokenizer",
                "estimator_version": "test-v1",
                "token_policy_version": "v3.token-policy.v1",
            },
        )
        first = migrate_v1_to_v2(v1, context)
        second = migrate_v1_to_v2(v1, context)
        self.assertEqual(v1, before)
        self.assertEqual(first, second)
        self.assertEqual(first["strategy"]["observed_strategy_atoms"], ["LOOP_UNROLL"])
        self.assertNotIn("task_id", str(first))
        self.assertNotIn("public-task-name", str(first))
        self.assertEqual(first["token_policy"]["effective_max_output_tokens"], 1500)
        self.assertEqual(first["token_policy"]["guidance_actual_tokens"], 180)


if __name__ == "__main__":
    unittest.main()
