from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llm4hls_agent.v3_experience import canonical_json
from llm4hls_agent.v3_experience_kb import ExperienceKnowledgeBase, build_kb_query
from llm4hls_agent.v3_experience_kb_cli import _parser, main
from llm4hls_agent.v3_experience_v2 import seal_experience_v2

from .test_v3_experience_v2_schema import sample_body


class ExperienceKBCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kb = ExperienceKnowledgeBase(self.root / "kb")
        for index in (1, 2):
            body = copy.deepcopy(sample_body())
            body["source"].update(
                {
                    "run_id": f"run-{index}",
                    "candidate_id": f"candidate-{index}",
                    "round_index": index,
                    "task_family_hash": f"{index + 10:064x}",
                }
            )
            body["strategy"]["patch_digest"] = f"{index + 100:064x}"
            body["provenance"]["source_record_hash"] = f"{index + 200:064x}"
            self.kb.put_if_absent(seal_experience_v2(body))
        self.snapshot = self.kb.snapshot(persist=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *args: str) -> tuple[int, dict[str, object]]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = main(list(args))
        return status, json.loads(stream.getvalue())

    def test_offline_import_commands_default_to_unspecified(self) -> None:
        imported = _parser().parse_args(
            [
                "import",
                "--source",
                "source.json",
                "--store",
                "store.jsonl",
                "--output-dir",
                "output",
            ]
        )
        postprocess = _parser().parse_args(
            ["postprocess-run", "--run-root", "terminal-run"]
        )

        self.assertEqual(imported.task_split, "unspecified")
        self.assertEqual(postprocess.task_split, "unspecified")

    def test_stats_and_snapshot_emit_machine_json(self) -> None:
        status, stats = self.invoke("stats", "--kb-root", str(self.kb.root))
        self.assertEqual(status, 0)
        self.assertEqual(stats["migrated_record_count"], 2)
        status, snapshot = self.invoke("snapshot", "--kb-root", str(self.kb.root))
        self.assertEqual(status, 0)
        self.assertEqual(snapshot["record_count"], 2)

    def test_retrieve_and_rank_accept_frozen_snapshot(self) -> None:
        query = build_kb_query(
            mode="OPTIMIZE",
            task_split="hidden_like",
            bottleneck_subtype="SERIAL_REDUCTION",
            algorithm_family="DOT_PRODUCT",
            task_family_hash="f" * 64,
            toolchain="Vitis 2025.2",
            backend_fingerprint="vitis-backend-v0.7",
            prompt_version="v3c.task-aware.v1",
        )
        query_path = self.root / "query.json"
        query_path.write_bytes(canonical_json(query) + b"\n")
        snapshot_path = next((self.kb.root / "snapshots").glob("*.json"))
        status, retrieval = self.invoke(
            "retrieve",
            "--kb-root",
            str(self.kb.root),
            "--snapshot",
            str(snapshot_path),
            "--query-json",
            str(query_path),
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(retrieval["successes"]), 2)
        status, ranking = self.invoke(
            "rank",
            "--kb-root",
            str(self.kb.root),
            "--snapshot",
            str(snapshot_path),
            "--query-json",
            str(query_path),
        )
        self.assertEqual(status, 0)
        self.assertEqual(ranking["support_count"], 2)

    def test_validate_quarantines_only_digest_of_bad_record(self) -> None:
        bad = self.root / "bad.jsonl"
        bad.write_text('{"not":"v2"}\n', encoding="utf-8")
        status, result = self.invoke(
            "validate",
            "--kb-root",
            str(self.kb.root),
            "--input",
            str(bad),
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["valid"], 0)
        self.assertEqual(result["quarantined"], 1)
        quarantine = (self.kb.root / "quarantine" / "quarantine.jsonl").read_text()
        self.assertNotIn('{"not":"v2"}', quarantine)

    def test_readiness_does_not_run_external_tools(self) -> None:
        records = self.root / "records.jsonl"
        records.write_bytes(
            b"".join(
                canonical_json(item) + b"\n" for item in self.kb.records(self.snapshot)
            )
        )
        status, result = self.invoke("readiness", "--records", str(records))
        self.assertEqual(status, 0)
        self.assertEqual(result["strategy_ranker"]["status"], "NOT_READY")
        self.assertFalse(result["complex_model_training_allowed"])

    def test_postprocess_run_is_an_explicit_offline_command(self) -> None:
        run_root = self.root / "terminal-run"
        run_root.mkdir()
        (run_root / "v3_prototype_result.json").write_text(
            '{"status":"DONE"}\n', encoding="utf-8"
        )
        output_dir = self.root / "offline-output"
        imported = SimpleNamespace(
            records=({"record_id": "r1"},),
            inserted_record_ids=("r1",),
            duplicate_record_ids=(),
        )
        attribution = {
            "record_count": 0,
            "inserted": 0,
            "duplicates": 0,
        }
        with (
            patch(
                "llm4hls_agent.v3_experience_kb_cli.import_historical_runs",
                return_value=imported,
            ) as import_runs,
            patch(
                "llm4hls_agent.v3_experience_kb_cli.write_import_artifacts",
                return_value={"report": output_dir / "report.json"},
            ),
            patch(
                "llm4hls_agent.v3_experience_attribution."
                "persist_recommendation_attributions",
                return_value=attribution,
            ) as persist_attribution,
        ):
            status, result = self.invoke(
                "postprocess-run",
                "--run-root",
                str(run_root),
                "--output-dir",
                str(output_dir),
                "--task-split",
                "train",
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            result["execution_boundary"], "EXPLICIT_OFFLINE_POSTPROCESS"
        )
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(result["vitis_calls"], 0)
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["task_split"], "train")
        self.assertEqual(result["task_split_source"], "explicit_cli")
        imported_sources = import_runs.call_args.args[0]
        self.assertEqual(
            imported_sources,
            [run_root.resolve() / "v3_prototype_result.json"],
        )
        self.assertEqual(
            import_runs.call_args.kwargs["policy"].task_split,
            "train",
        )
        persist_attribution.assert_called_once_with(run_root.resolve())


if __name__ == "__main__":
    unittest.main()
