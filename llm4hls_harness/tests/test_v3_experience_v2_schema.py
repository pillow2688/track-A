from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_v2 import (
    BOTTLENECK_SUBTYPES_BY_MODE,
    EXPERIENCE_V2_SCHEMA,
    FAILURE_SUBTYPES_BY_MODE,
    STRATEGIES_BY_MODE,
    seal_experience_v2,
    taxonomy_manifest,
    validate_experience_v2,
)


def sample_body() -> dict[str, object]:
    return {
        "schema_version": EXPERIENCE_V2_SCHEMA,
        "record_id": "",
        "taxonomy_versions": {
            "failure": "v3e.failure-taxonomy.v1",
            "bottleneck": "v3e.bottleneck-taxonomy.v1",
            "strategy": "v3e.strategy-taxonomy.v1",
        },
        "source": {
            "run_id": "run-001",
            "candidate_id": "candidate-001",
            "round_index": 1,
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "prompt_version": "v3c.task-aware.v1",
            "toolchain": "Vitis 2025.2",
            "backend_fingerprint": "vitis-backend-v0.7",
            "evidence_level": "REAL_LLM_VITIS",
            "task_split": "train",
            "task_family_hash": "a" * 64,
            "algorithm_family": "DOT_PRODUCT",
            "difficulty": 3,
        },
        "problem": {
            "mode": "OPTIMIZE",
            "failure_stage": "NONE",
            "failure_subtype": "UNKNOWN",
            "primary_bottleneck": "LOOP_LATENCY",
            "bottleneck_subtype": "SERIAL_REDUCTION",
            "numeric_semantics": "FLOAT_TOLERANT",
            "requires_cosim": False,
        },
        "structure_features": {
            "top_signature_hash": "b" * 64,
            "loop_count_bucket": "1",
            "critical_loop_ii": 1,
            "critical_loop_trip_count_bucket": "257-1024",
            "transaction_interval_bucket": "1024+",
            "latency_bucket": "1024+",
            "has_pipeline": True,
            "has_dataflow": False,
            "has_stream": False,
            "has_fifo": False,
            "has_reduction": True,
            "has_dynamic_allocation": False,
            "has_recursion": False,
            "has_unsupported_stl": False,
            "memory_access_pattern": "SEQUENTIAL",
            "resource_pressure": {
                "lut": "LOW",
                "ff": "LOW",
                "dsp": "LOW",
                "bram": "LOW",
            },
        },
        "strategy": {
            "declared_strategy_bundle": ["LOOP_UNROLL"],
            "observed_strategy_atoms": ["LOOP_UNROLL"],
            "strategy_normalization_confidence": 1.0,
            "normalization_reason_codes": ["OBSERVED_PRAGMA_UNROLL"],
            "unclassified_changes": [],
            "patch_lines_added": 1,
            "patch_lines_deleted": 0,
            "patch_complexity": "SMALL",
            "patch_digest": "c" * 64,
        },
        "validation": {
            "patch_valid": True,
            "interface_guard_pass": True,
            "candidate_created": True,
            "csim_status": "PASS",
            "synth_status": "PASS",
            "cosim_status": "PASS",
            "fresh_final_status": "PASS",
            "promoted": True,
            "rejected": False,
            "failure_reason": None,
        },
        "performance": {
            "latency_before": 1027.0,
            "latency_after": 38.0,
            "transaction_interval_before": 1025.0,
            "transaction_interval_after": 38.0,
            "acceleration": 27.026,
            "resource_delta": {"lut": 100.0, "ff": 20.0},
            "strict_improvement": True,
        },
        "cost": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "credits": 30,
            "csim_calls": 2,
            "synth_calls": 2,
            "cosim_calls": 1,
            "wall_time_seconds": 90.0,
        },
        "provenance": {
            "artifact_refs": [
                {"role": "candidate_synth_result", "ref": "actions/a/result.json", "sha256": "d" * 64}
            ],
            "artifact_hashes": ["d" * 64],
            "source_record_hash": "e" * 64,
            "eligible_for_retrieval": True,
            "eligible_for_ranking": True,
            "exclusion_reasons": [],
        },
    }


class ExperienceV2SchemaTests(unittest.TestCase):
    def test_canonical_serialization_and_hash_are_stable(self) -> None:
        first = seal_experience_v2(sample_body())
        reordered = dict(reversed(list(sample_body().items())))
        second = seal_experience_v2(reordered)
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(validate_experience_v2(first), first)

    def test_taxonomies_contain_every_required_mode_and_other_fallback(self) -> None:
        manifest = taxonomy_manifest()
        self.assertEqual(set(manifest["strategy"]["by_mode"]), set(STRATEGIES_BY_MODE))
        self.assertIn("FUNCTIONAL_MISMATCH_OTHER", FAILURE_SUBTYPES_BY_MODE["REPAIR"])
        self.assertIn("OPTIMIZATION_BOTTLENECK_OTHER", BOTTLENECK_SUBTYPES_BY_MODE["OPTIMIZE"])
        for mode, strategies in STRATEGIES_BY_MODE.items():
            self.assertTrue(any(item.startswith("OTHER_") for item in strategies), mode)

    def test_record_rejects_task_identity_secret_and_absolute_path(self) -> None:
        for field, value in (
            ("provider", "api_key=secret"),
            ("backend_fingerprint", "/home/user/vitis"),
        ):
            body = sample_body()
            body["source"][field] = value
            with self.assertRaises(ValueError):
                seal_experience_v2(body)
        body = sample_body()
        body["source"]["task_id"] = "must-not-exist"
        with self.assertRaises(ValueError):
            seal_experience_v2(body)

    def test_content_change_requires_a_new_record_id(self) -> None:
        record = seal_experience_v2(sample_body())
        tampered = copy.deepcopy(record)
        tampered["performance"]["latency_after"] = 39.0
        with self.assertRaisesRegex(ValueError, "record_id"):
            validate_experience_v2(tampered)


if __name__ == "__main__":
    unittest.main()
