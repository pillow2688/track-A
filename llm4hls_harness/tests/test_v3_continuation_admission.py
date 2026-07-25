from __future__ import annotations

import copy
import unittest

from llm4hls_agent.v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_FIXED_THRESHOLDS,
    CONTINUATION_POLICY_VERSION,
    validate_continuation_admission,
)


def passing_manifest() -> dict[str, object]:
    return {
        "schema_version": CONTINUATION_ADMISSION_SCHEMA,
        "decision": "PASS",
        "policy_version": CONTINUATION_POLICY_VERSION,
        "protocol": "MODE_SPECIFIC_PRE_STATE_ONLINE_SHADOW",
        "thresholds": CONTINUATION_FIXED_THRESHOLDS,
        "metrics": {
            "mode_counts": {
                "REPAIR": 2,
                "SYNTH_FIX": 2,
                "STRUCTURAL_FIX": 2,
                "OPTIMIZE": 2,
            },
            "beneficial_retention": 1.0,
            "v1_beneficial_retention": 0.5,
            "waste_block_rate": 0.5,
            "v1_waste_block_rate": 0.4,
            "false_blocks": 0,
            "structural_essential_samples": 1,
            "structural_essential_retention": 1.0,
            "leakage_violations": 0,
        },
        "evidence_sha256": "e" * 64,
    }


class ContinuationAdmissionTests(unittest.TestCase):
    def test_passing_fixed_protocol_manifest_is_accepted(self) -> None:
        self.assertEqual(
            validate_continuation_admission(passing_manifest()),
            passing_manifest(),
        )

    def test_thresholds_cannot_be_lowered(self) -> None:
        value = passing_manifest()
        value["thresholds"] = dict(CONTINUATION_FIXED_THRESHOLDS)
        value["thresholds"]["maximum_false_blocks"] = 10
        with self.assertRaisesRegex(ValueError, "thresholds"):
            validate_continuation_admission(value)

    def test_regression_and_missing_modes_fail_closed(self) -> None:
        value = passing_manifest()
        value["metrics"]["beneficial_retention"] = 0.4
        value["metrics"]["v1_beneficial_retention"] = 0.5
        with self.assertRaisesRegex(ValueError, "retention regressed"):
            validate_continuation_admission(value)
        value = passing_manifest()
        del value["metrics"]["mode_counts"]["SYNTH_FIX"]
        with self.assertRaisesRegex(ValueError, "four modes"):
            validate_continuation_admission(value)

    def test_structural_denominator_must_be_nonzero(self) -> None:
        value = copy.deepcopy(passing_manifest())
        value["metrics"]["structural_essential_samples"] = 0
        with self.assertRaisesRegex(ValueError, "structural sample"):
            validate_continuation_admission(value)


if __name__ == "__main__":
    unittest.main()
