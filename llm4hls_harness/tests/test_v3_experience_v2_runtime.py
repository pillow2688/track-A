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
    ExperienceV2RuntimeCoordinator,
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

    def _admission(self, seed: Path) -> dict[str, object]:
        by_mode = {
            mode: {
                "records": 10,
                "coverage": 0.5,
                "harmful_rate": 0.0,
                "positive_hit_rate": 0.5,
            }
            for mode in ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
        }
        return {
            "schema_version": RANKER_ADMISSION_SCHEMA,
            "decision": "PASS",
            "ranker_version": STRATEGY_RANKER_V3_SCHEMA,
            "seed_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
            "protocol": "LOTO_AND_LEAVE_ONE_TASK_FAMILY_OUT",
            "thresholds": {
                "minimum_coverage": 0.40,
                "maximum_harmful_rate": 0.05,
                "minimum_records_per_mode": 10,
                "minimum_global_positive_hit_rate": 0.2727,
                "maximum_leakage_violations": 0,
            },
            "metrics": {
                "coverage": 0.5,
                "harmful_rate": 0.0,
                "global_positive_hit_rate": 0.5,
                "leakage_violations": 0,
                "by_mode": by_mode,
            },
            "evidence_sha256": "e" * 64,
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
            manifest = root / "admission.json"
            manifest.write_text(
                json.dumps(self._admission(seed)), encoding="utf-8"
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
            value = self._admission(seed)
            value["thresholds"]["maximum_harmful_rate"] = 1.0
            path = root / "bad-threshold.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "thresholds"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=path
                )
            value = self._admission(seed)
            value["seed_sha256"] = "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seed hash"):
                ExperienceV2RuntimeCoordinator(
                    seed, admission_manifest_path=path
                )


if __name__ == "__main__":
    unittest.main()
