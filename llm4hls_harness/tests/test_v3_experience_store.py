from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience_guidance import ExperienceFeatureExtractor
from llm4hls_agent.v3_experience_store import (
    ExperienceStoreError,
    JsonlExperienceRepository,
)


def _outcome(*, success: bool, latency: int = 50) -> dict[str, object]:
    return {
        "patch_valid": True,
        "candidate_created": True,
        "csim_pass": success,
        "synth_pass": success,
        "cosim_status": "PASS" if success else "FAIL",
        "final_pass": success,
        "promoted": success,
        "failure_stage": None if success else "COSIM",
        "latency_before": 100,
        "latency_after": latency,
        "acceleration": 100 / latency,
        "tokens": 100,
        "credits": 25,
        "wall_time_seconds": 10.0,
    }


class ExperienceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "experience.jsonl"
        self.store = JsonlExperienceRepository(self.path)
        self.extractor = ExperienceFeatureExtractor()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(
        self,
        index: int,
        *,
        revision: int = 1,
        run_id: str | None = None,
        candidate_id: str | None = None,
        execution_class: str = "REAL_LLM_VITIS",
        eligible: bool = True,
        success: bool = True,
    ) -> dict[str, object]:
        return self.extractor.build_record(
            run_id=run_id or f"run-{index}",
            candidate_id=candidate_id or f"candidate-{index}",
            revision=revision,
            task_id=f"task-{index}",
            task_split="train",
            mode="OPTIMIZE",
            source="float acc=0; for(int i=0;i<16;i++) acc += a[i]*b[i];",
            description="dot product reduction",
            patch=f"--- a/kernel.cpp\n+++ b/kernel.cpp\n+// change {index}-{revision}\n",
            execution_class=execution_class,
            eligible_for_ranking=eligible,
            artifact_refs=[
                {
                    "role": "tool-result",
                    "ref": f"artifacts/candidate-{index}/result.json",
                    "sha256": f"{index:064x}",
                }
            ],
            strategy_bundle=["LOOP_UNROLL"],
            outcome=_outcome(success=success, latency=max(1, 60 - revision)),
        )

    def test_missing_store_has_stable_empty_snapshot(self) -> None:
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.byte_offset, 0)
        self.assertEqual(snapshot.record_count, 0)
        self.assertEqual(self.store.records(snapshot), ())
        self.assertFalse(self.path.exists())

    def test_put_is_idempotent_and_rejects_record_id_collision(self) -> None:
        record = self.record(1)
        first = self.store.put_if_absent(record)
        second = self.store.put_if_absent(record)
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(self.store.snapshot().record_count, 1)

        collision = dict(record)
        collision["outcome"] = dict(record["outcome"])
        collision["outcome"]["tokens"] = 101
        with self.assertRaises(ExperienceStoreError):
            self.store.put_if_absent(collision)

    def test_same_trajectory_revision_cannot_be_counted_twice(self) -> None:
        first = self.record(1, run_id="one-run", candidate_id="one-candidate")
        second = dict(first)
        second["record_id"] = "f" * 64
        self.store.put_if_absent(first)
        with self.assertRaises(ExperienceStoreError):
            self.store.put_if_absent(second)

    def test_latest_revision_counts_trajectory_once(self) -> None:
        first = self.record(
            1, revision=1, run_id="same-run", candidate_id="same-candidate"
        )
        second = self.record(
            2, revision=2, run_id="same-run", candidate_id="same-candidate"
        )
        self.store.put_if_absent(first)
        self.store.put_if_absent(second)
        latest = self.store.latest_trajectories()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["revision"], 2)

    def test_latest_ineligible_revision_revokes_older_ranking_revision(self) -> None:
        first = self.record(
            1, revision=1, run_id="same-run", candidate_id="same-candidate"
        )
        second = self.record(
            2,
            revision=2,
            run_id="same-run",
            candidate_id="same-candidate",
            eligible=False,
        )
        self.store.put_if_absent(first)
        self.store.put_if_absent(second)
        self.assertEqual(self.store.latest_trajectories(ranking_only=True), ())
        self.assertEqual(
            self.store.latest_trajectories(ranking_only=False)[0]["revision"], 2
        )

    def test_ranking_filter_excludes_fixture_and_ineligible(self) -> None:
        self.store.put_if_absent(self.record(1))
        self.store.put_if_absent(
            self.record(
                2,
                execution_class="DETERMINISTIC_FIXTURE",
                eligible=False,
            )
        )
        self.store.put_if_absent(self.record(3, eligible=False))
        self.assertEqual(len(self.store.latest_trajectories(ranking_only=False)), 3)
        ranking = self.store.latest_trajectories(ranking_only=True)
        self.assertEqual([item["run_id"] for item in ranking], ["run-1"])

    def test_frozen_snapshot_ignores_later_append(self) -> None:
        self.store.put_if_absent(self.record(1))
        frozen = self.store.snapshot()
        self.store.put_if_absent(self.record(2))
        self.assertEqual(len(self.store.records(frozen)), 1)
        self.assertEqual(len(self.store.records()), 2)

    def test_incomplete_tail_is_ignored_then_recovered_by_writer(self) -> None:
        self.store.put_if_absent(self.record(1))
        committed_size = self.path.stat().st_size
        with self.path.open("ab") as handle:
            handle.write(b'{"killed_writer":')
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.byte_offset, committed_size)
        self.assertEqual(snapshot.record_count, 1)
        self.store.put_if_absent(self.record(2))
        self.assertEqual(len(self.store.records()), 2)
        self.assertNotIn(b"killed_writer", self.path.read_bytes())

    def test_snapshot_detects_prefix_tampering(self) -> None:
        self.store.put_if_absent(self.record(1))
        frozen = self.store.snapshot()
        data = bytearray(self.path.read_bytes())
        location = data.index(b"run-1")
        data[location : location + 5] = b"run-X"
        self.path.write_bytes(data)
        with self.assertRaises(ExperienceStoreError):
            self.store.records(frozen)

    def test_complete_corrupt_line_is_not_silently_skipped(self) -> None:
        self.path.write_bytes(b"not-json\n")
        with self.assertRaises(ExperienceStoreError):
            self.store.snapshot()


if __name__ == "__main__":
    unittest.main()
