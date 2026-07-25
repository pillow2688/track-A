from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_v2 import MODES, validate_experience_v2


ROOT = Path(__file__).resolve().parents[2]
STORE = (
    ROOT
    / "llm4hls_harness"
    / "experiments"
    / "v3e"
    / "kb_v2"
    / "experience"
    / "derived"
    / "experience_v2.jsonl"
)


def _records() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in STORE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class C1ExperienceRecordV2AuditTests(unittest.TestCase):
    def test_frozen_store_is_canonical_valid_and_unique(self) -> None:
        records = _records()
        self.assertEqual(len(records), 103)
        validated = [validate_experience_v2(record) for record in records]
        self.assertEqual(
            len({str(record["record_id"]) for record in validated}),
            len(validated),
        )
        self.assertEqual(
            len(
                {
                    (
                        str(record["source"]["run_id"]),
                        str(record["source"]["candidate_id"]),
                        int(record["source"]["round_index"]),
                    )
                    for record in validated
                }
            ),
            len(validated),
        )
        self.assertEqual(
            STORE.read_bytes(),
            b"".join(canonical_json(record) + b"\n" for record in validated),
        )

    def test_frozen_store_has_public_real_evidence_for_all_modes(self) -> None:
        records = [validate_experience_v2(record) for record in _records()]
        self.assertEqual(
            {str(record["problem"]["mode"]) for record in records},
            set(MODES),
        )
        self.assertTrue(
            all(
                record["source"]["evidence_level"] == "REAL_LLM_VITIS"
                for record in records
            )
        )
        self.assertEqual(
            sum(
                record["provenance"]["eligible_for_ranking"] is True
                for record in records
            ),
            76,
        )

        serialized = canonical_json(records).decode("utf-8").lower()
        for forbidden in (
            "/home/",
            "api_key",
            "api-key",
            "authorization",
            "bearer ",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIsNone(
            re.search(r"(?<![a-z0-9])sk-[a-z0-9]{8,}", serialized)
        )
        for record in records:
            for artifact in record["provenance"]["artifact_refs"]:
                segments = set(str(artifact["ref"]).lower().split("/"))
                self.assertTrue(
                    segments.isdisjoint({"hidden", "reference", "golden"})
                )


if __name__ == "__main__":
    unittest.main()
