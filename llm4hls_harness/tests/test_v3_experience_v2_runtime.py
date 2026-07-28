from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_v2 import seal_experience_v2
from llm4hls_agent.v3_experience_v2_runtime import (
    RANKER_ADMISSION_SCHEMA,
    RANKER_GATE_PROTOCOL,
    RANKER_GATE_SCHEMA,
    STRATEGY_CARD_SCHEMA,
    ExperienceV2RuntimeCoordinator,
    compact_strategy_card,
    current_repository_commit,
)
from llm4hls_agent.v3_strategy_ranker_v3 import STRATEGY_RANKER_V3_SCHEMA

from .test_v3_experience_v2_schema import sample_body


class ExperienceV2RuntimeTests(unittest.TestCase):
    def _seed(self, root: Path, count: int = 5) -> Path:
        path = root / "experience-v2.jsonl"
        records = []
        for index in range(1, count + 1):
            body = copy.deepcopy(sample_body())
            body["source"].update(
                {
                    "run_id": f"seed-run-{index}",
                    "candidate_id": f"candidate-{index}",
                    "round_index": index,
                    "task_family_hash": f"{index + 10:064x}",
                }
            )
            body["strategy"]["patch_digest"] = f"{index + 100:064x}"
            body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
            records.append(seal_experience_v2(body))
        path.write_bytes(b"\n".join(canonical_json(item) for item in records) + b"\n")
        return path

    def _gate(self, seed: Path, path: Path) -> Path:
        modes = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
        policy = {
            "overall": {
                "leakage_violations": 0,
                "coverage": 0.40,
                "harmful_recommendation_rate": 0.0,
                "positive_strategy_hit_rate": 0.2727,
            },
            "by_mode": {
                mode: {"harmful_recommendation_rate": 0.0}
                for mode in modes
            },
        }
        gate = {
            "schema_version": RANKER_GATE_SCHEMA,
            "decision": "PASS",
            "authority": "ELIGIBLE_FOR_ADMISSION",
            "ranker_version": STRATEGY_RANKER_V3_SCHEMA,
            "fixed_protocol": RANKER_GATE_PROTOCOL,
            "thresholds": {
                "minimum_coverage": 0.40,
                "maximum_harmful_rate": 0.05,
                "minimum_records_per_mode": 10,
                "minimum_global_positive_hit_rate": 0.2727,
                "maximum_leakage_violations": 0,
            },
            "input": {
                "store_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
                "verified_by_mode": {mode: 10 for mode in modes},
            },
            "checks": {
                "coverage_gate_pass": True,
                "harmful_rate_gate_pass": True,
                "mode_count_gate_pass": True,
                "positive_hit_gate_pass": True,
                "leakage_gate_pass": True,
                "unverified_labels_excluded": True,
                "family_level_sampling": True,
                "query_outcome_fields_present": False,
                "heldout_labels_read_after_decision": True,
                "train_only_support": True,
                "public_only_support": True,
            },
            "policies": {
                "leave_one_task_out": policy,
                "leave_one_task_family_out": policy,
            },
        }
        path.write_bytes(canonical_json(gate) + b"\n")
        return path

    def _admission(
        self, seed: Path, gate: Path
    ) -> dict[str, object]:
        return {
            "schema_version": RANKER_ADMISSION_SCHEMA,
            "decision": "PASS",
            "ranker_schema": STRATEGY_RANKER_V3_SCHEMA,
            "store_path": seed.name,
            "store_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
            "gate_path": gate.name,
            "gate_sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
            "thresholds": {
                "minimum_coverage": 0.40,
                "maximum_harmful_rate": 0.05,
                "minimum_records_per_mode": 10,
                "minimum_global_positive_hit_rate": 0.2727,
                "maximum_leakage_violations": 0,
            },
            "current_commit": current_repository_commit(),
        }

    def _guidance(
        self, coordinator: ExperienceV2RuntimeCoordinator
    ) -> dict[str, object]:
        return coordinator.build_guidance(
            mode="OPTIMIZE",
            source=(
                "void top(float a[16], float *out) { float sum = 0; "
                "for (int i=0;i<16;i++) sum += a[i]; *out=sum; }"
            ),
            task_split="hidden_like",
            current_run_id="current-run",
            current_candidate_id="candidate-current",
            description="public reduction kernel",
            remaining_tokens=4000,
            prompt_token_limit=2000,
        )

    def test_shadow_reads_frozen_v2_and_persists_ranker_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            audit = root / "run" / "experience" / "recommendations.jsonl"
            coordinator = ExperienceV2RuntimeCoordinator(
                seed, recommendation_path=audit
            )
            guidance = self._guidance(coordinator)
            self.assertTrue(guidance["recommended_strategy_bundles"])
            self.assertFalse(coordinator.prompt_injection_authorized)
            first = coordinator.persist_recommendation(1, guidance)
            second = coordinator.persist_recommendation(1, guidance)
            self.assertTrue(first["persisted"])
            self.assertFalse(second["persisted"])
            payload = json.loads(audit.read_text(encoding="utf-8"))
            self.assertFalse(
                payload["control"]["prompt_injection_authorized"]
            )
            self.assertTrue(
                payload["ranking"]["input_contract"][
                    "unverified_labels_excluded"
                ]
            )

    def test_guided_requires_hash_bound_passing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            with self.assertRaisesRegex(ValueError, "admission manifest"):
                ExperienceV2RuntimeCoordinator(
                    seed, injection_requested=True
                )
            gate = self._gate(seed, root / "gate.json")
            manifest = root / "admission.json"
            manifest.write_text(
                json.dumps(self._admission(seed, gate)), encoding="utf-8"
            )
            coordinator = ExperienceV2RuntimeCoordinator(
                seed,
                admission_manifest_path=manifest,
                injection_requested=True,
            )
            self.assertTrue(coordinator.prompt_injection_authorized)

    def test_manifest_cannot_lower_fixed_gate_or_bind_other_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            gate = self._gate(seed, root / "gate.json")
            value = self._admission(seed, gate)
            value["thresholds"]["maximum_harmful_rate"] = 1.0
            path = root / "bad-threshold.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "thresholds"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=path
                )
            value = self._admission(seed, gate)
            value["store_sha256"] = "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Store hash"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=path
                )

    def test_previous_ranker_admission_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            gate = self._gate(seed, root / "gate.json")
            value = self._admission(seed, gate)
            value["schema_version"] = "v3e.strategy-ranker-admission.v1"
            path = root / "old-ranker-version.json"
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unsupported"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=path
                )

    def test_gate_metrics_are_rechecked_from_bound_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            gate = self._gate(seed, root / "gate.json")
            gate_value = json.loads(gate.read_text(encoding="utf-8"))
            gate_value["policies"]["leave_one_task_out"]["overall"][
                "coverage"
            ] = 0.39
            gate.write_text(json.dumps(gate_value), encoding="utf-8")
            manifest = root / "admission.json"
            manifest.write_text(
                json.dumps(self._admission(seed, gate)), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "coverage failed"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=manifest
                )

    def test_store_and_gate_hash_changes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            gate = self._gate(seed, root / "gate.json")
            manifest = root / "admission.json"
            value = self._admission(seed, gate)
            manifest.write_text(json.dumps(value), encoding="utf-8")

            gate.write_bytes(gate.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "Gate hash"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=manifest
                )

            gate = self._gate(seed, gate)
            value = self._admission(seed, gate)
            manifest.write_text(json.dumps(value), encoding="utf-8")
            seed.write_bytes(seed.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "Store hash"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=manifest
                )

    def test_admission_for_other_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = self._seed(root)
            gate = self._gate(seed, root / "gate.json")
            manifest = root / "admission.json"
            value = self._admission(seed, gate)
            value["current_commit"] = "0" * 40
            manifest.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "current_commit mismatch"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=manifest
                )

    def test_compact_card_is_top_one_and_contains_no_record_payload(self) -> None:
        guidance = {
            "schema_version": "v3e.experience-guidance.v1",
            "similar_successes": [],
            "similar_failures": [],
            "recommended_strategy_bundles": [
                {
                    "strategy_bundle": ["ARRAY_PARTITION"],
                    "context": "MODE_SUBTYPE_PROFILE_BACKOFF",
                    "attempts": 4,
                    "successes": 4,
                    "utility": 0.61,
                },
                {
                    "strategy_bundle": ["LOOP_UNROLL"],
                    "context": "SECOND_CHOICE",
                    "attempts": 3,
                    "successes": 3,
                    "utility": 0.55,
                },
            ],
            "discouraged_strategy_bundles": [
                {"strategy_bundle": ["DATAFLOW"], "utility": 0.1}
            ],
            "confidence": 0.61,
            "supporting_record_ids": ["a" * 64],
            "fallback_reason": None,
            "notice": "Historical advice only.",
        }

        card = compact_strategy_card(guidance)

        self.assertEqual(card["schema_version"], STRATEGY_CARD_SCHEMA)
        self.assertEqual(card["strategy_atom"], "ARRAY_PARTITION")
        self.assertEqual(card["avoid_or_high_risk"], ["DATAFLOW"])
        self.assertEqual(card["source_family_count"], 4)
        self.assertNotIn("supporting_record_ids", card)
        self.assertNotIn("LOOP_UNROLL", json.dumps(card))

    def test_abstain_has_no_strategy_card(self) -> None:
        guidance = {
            "schema_version": "v3e.experience-guidance.v1",
            "similar_successes": [],
            "similar_failures": [],
            "recommended_strategy_bundles": [],
            "discouraged_strategy_bundles": [
                {"strategy_bundle": ["DATAFLOW"], "utility": 0.1}
            ],
            "confidence": 0.0,
            "supporting_record_ids": [],
            "fallback_reason": "NO_SAFE_VERIFIED_STRATEGY",
            "notice": "Historical advice only.",
        }

        self.assertIsNone(compact_strategy_card(guidance))


if __name__ == "__main__":
    unittest.main()
