from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.v3_experience import (
    EXPERIENCE_RECORD_FIELDS,
    validate_experience_record,
)
from llm4hls_agent.v3_experience_importer import (
    ImportPolicy,
    _final_pass,
    build_experience_stats,
    build_import_report,
    import_historical_runs,
    render_data_quality_markdown,
)
from llm4hls_agent.v3_experience_store import JsonlExperienceRepository


_MANIFEST = "a" * 64


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _gate(gate: str, *, status: str = "PASS", scope: str = "exploration") -> dict[str, object]:
    return {
        "action_id": f"{gate}-{scope}",
        "status": status,
        "result_ref": f"actions/{gate}-{scope}/result.json",
        "validation_scope": scope,
    }


def _synth_evidence(latency: int, *, candidate_id: str) -> dict[str, object]:
    return {
        "schema_version": "v3a.synth-evidence.v1",
        "candidate_id": candidate_id,
        "loops": [
            {
                "name": "loop",
                "pipeline_ii": 1,
                "trip_count": 100,
                "latency_cycles": latency,
                "issue_type": None,
                "violation_type": None,
            }
        ],
        "observations": [],
        "top_level": {
            "latency": {"best": latency, "average": latency, "worst": latency},
            "transaction_interval": {"min": latency, "max": latency},
            "utilization_percent": {"LUT": 2.0, "FF": 1.0, "DSP": 0.5},
        },
    }


def _make_v3_run(
    root: Path,
    *,
    rejection: str | None = None,
    candidate_latency: int = 40,
    candidate_status: str = "FINAL_VERIFIED",
    demo: bool = False,
) -> Path:
    run = root / "run"
    proposal_ref = "planner/proposal_001.json"
    patch_ref = "candidates/candidate_001/patch.diff"
    patch = "--- kernel.cpp\n+++ kernel.cpp\n@@ -1 +1 @@\n-int x;\n+int x = 1;\n"
    provider = "scripted-v3a0-prototype" if demo else "openai-compatible-task-aware"
    model = "operator-supplied-patch-v1" if demo else "deepseek-v4-pro"
    proposal = {
        "parent_candidate_id": "candidate_000",
        "change_class": "LOOP_UNROLL",
        "patch": patch,
        "provider": provider,
        "model": model,
        "input_tokens": 10,
        "output_tokens": 5,
        "duration_seconds": 1.5,
        "risk": json.dumps(
            {
                "level": "LOW",
                "primary_bottleneck": "serial loop latency",
                "mutation_answer": "must-not-survive",
            }
        ),
    }
    _write_json(run / proposal_ref, proposal)
    (run / "baseline" / "source").mkdir(parents=True, exist_ok=True)
    (run / "baseline" / "source" / "kernel.cpp").write_text(
        "#include <hls_stream.h>\n#pragma HLS DATAFLOW\nhls::stream<int> s;\n",
        encoding="utf-8",
    )
    _write_json(
        run / "evidence" / "synth" / "baseline.json",
        _synth_evidence(100, candidate_id="candidate_000"),
    )
    _write_json(
        run / "control" / "package_manifest.json",
        {"schema_version": "v3a.package-manifest.v1", "status": "DONE"},
    )
    _write_json(
        run / "v3_task_spec.json",
        {
            "task_id": "private_task_name_must_not_survive",
            "task_type": "optimize",
            "difficulty": 2,
        },
    )
    backend = (
        {
            "class": "llm4hls_agent.v3_prototype.DeterministicPrototypeBackend",
            "evidence_level": "ORCHESTRATION_SMOKE_ONLY",
        }
        if demo
        else {
            "class": "llm4hls_agent.vitis.VitisBackend",
            "evidence_level": "REAL_VITIS_VALIDATED",
        }
    )
    result: dict[str, object] = {
        "status": "DONE",
        "mode": "OPTIMIZE",
        "task_id": "private_task_name_must_not_survive",
        "backend": backend,
        "package": {
            "manifest_ref": "control/package_manifest.json",
            "manifest_sha256": _MANIFEST,
            "schema_version": "v3a.package-commit.v1",
        },
        "baseline_synth_evidence_ref": "evidence/synth/baseline.json",
        "candidate_rounds": [],
    }
    config: dict[str, object] = {
        "live_planner_replay_policy": "NON_REPLAYABLE" if not demo else "REPLAYABLE",
        "budget": {"costs": {"csim": 1, "synth": 4, "cosim": 20}},
    }
    if demo:
        result["planner_contract"] = {"mode": "scripted_deterministic_adapter"}
    candidates: dict[str, object] = {
        "candidate_000": {
            "candidate_id": "candidate_000",
            "source_ref": "baseline/source/kernel.cpp",
            "synth_evidence_ref": "evidence/synth/baseline.json",
            "validation": {},
        }
    }
    if rejection is None:
        (run / "candidates" / "candidate_001").mkdir(parents=True, exist_ok=True)
        (run / patch_ref).write_text(patch, encoding="utf-8")
        (run / "candidates" / "candidate_001" / "source").mkdir(
            parents=True, exist_ok=True
        )
        (run / "candidates" / "candidate_001" / "source" / "kernel.cpp").write_text(
            "int x = 1;\n", encoding="utf-8"
        )
        _write_json(
            run / "evidence" / "synth" / "candidate.json",
            _synth_evidence(candidate_latency, candidate_id="candidate_001"),
        )
        exploration = {
            "csim": _gate("csim"),
            "synth": _gate("synth"),
            "cosim": {"status": "NOT_RUN"},
        }
        final = {
            "csim": _gate("csim", scope="final"),
            "synth": _gate("synth", scope="final"),
            "cosim": _gate("cosim", scope="final"),
        }
        for gate, scope in (
            ("csim", "exploration"),
            ("synth", "exploration"),
            ("csim", "final"),
            ("synth", "final"),
            ("cosim", "final"),
        ):
            _write_json(
                run / "actions" / f"{gate}-{scope}" / "result.json",
                {"elapsed_s": 2.0},
            )
        candidate: dict[str, object] = {
            "candidate_id": "candidate_001",
            "planner_ref": proposal_ref,
            "patch_ref": patch_ref,
            "source_ref": "candidates/candidate_001/source/kernel.cpp",
            "synth_evidence_ref": "evidence/synth/candidate.json",
            "status": candidate_status,
            "validation": exploration,
        }
        if candidate_status == "FINAL_VERIFIED":
            candidate["final_validation"] = final
        candidates["candidate_001"] = candidate
        result["candidate_rounds"] = [
            {
                "candidate_id": "candidate_001",
                "strategy_bundle": ["LOOP_UNROLL"],
                "decision": candidate_status,
                "risk_decision": {
                    "level": "LOW",
                    "primary_bottleneck": "serial loop latency",
                },
            }
        ]
    else:
        _write_json(
            run / "control" / "proposal_rejections" / "round_001.json",
            {
                "schema_version": "v3a.proposal-rejection.v1",
                "round_index": 1,
                "planner_ref": proposal_ref,
                "planner_output_sha256": "b" * 64,
                "reason": rejection,
                "change_class": "LOOP_UNROLL",
                "parent_candidate_id": "candidate_000",
            },
        )
    _write_json(run / "candidate_registry.json", {"candidates": candidates})
    _write_json(run / "v3_run_config.json", config)
    _write_json(run / "v3_prototype_result.json", result)
    return run


class V3ExperienceImporterTests(unittest.TestCase):
    def test_real_candidate_is_whitelisted_and_zero_latency_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_v3_run(Path(tmp), candidate_latency=0)
            result = import_historical_runs(
                [run],
                policy=ImportPolicy(task_split="train", algorithm_family="vector"),
            )

        self.assertEqual(len(result.records), 1)
        record = validate_experience_record(result.records[0])
        self.assertEqual(set(record), EXPERIENCE_RECORD_FIELDS)
        self.assertEqual(record["execution_class"], "REAL_LLM_VITIS")
        self.assertTrue(record["eligible_for_ranking"])
        self.assertEqual(record["outcome"]["latency_after"], 0.0)
        self.assertEqual(record["outcome"]["final_pass"], True)
        self.assertEqual(record["outcome"]["cosim_status"], "PASS")
        rendered = json.dumps(record, sort_keys=True)
        self.assertNotIn("private_task_name_must_not_survive", rendered)
        self.assertNotIn("must-not-survive", rendered)
        self.assertNotIn(str(run), rendered)
        self.assertFalse(any(Path(ref["ref"]).is_absolute() for ref in record["artifact_refs"]))

    def test_only_train_real_candidates_are_ranking_eligible(self) -> None:
        for task_split, expected in (
            ("train", True),
            ("dev", False),
            ("unspecified", False),
        ):
            with self.subTest(task_split=task_split):
                with tempfile.TemporaryDirectory() as tmp:
                    run = _make_v3_run(Path(tmp))
                    result = import_historical_runs(
                        [run],
                        policy=ImportPolicy(task_split=task_split),
                    )

                self.assertEqual(len(result.records), 1)
                self.assertIs(
                    result.records[0]["eligible_for_ranking"], expected
                )

    def test_repository_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_v3_run(root)
            repository = JsonlExperienceRepository(root / "experience_store.jsonl")
            first = import_historical_runs([run], repository)
            second = import_historical_runs([run], repository)

            self.assertEqual(len(first.inserted_record_ids), 1)
            self.assertEqual(len(first.duplicate_record_ids), 0)
            self.assertEqual(len(second.inserted_record_ids), 0)
            self.assertEqual(len(second.duplicate_record_ids), 1)
            self.assertEqual(repository.snapshot().record_count, 1)

    def test_patch_policy_rejection_is_stage_conditioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_v3_run(Path(tmp), rejection="PATCH_POLICY_REJECTED")
            result = import_historical_runs([run])

        outcome = result.records[0]["outcome"]
        self.assertFalse(outcome["patch_valid"])
        self.assertFalse(outcome["candidate_created"])
        self.assertIsNone(outcome["csim_pass"])
        self.assertIsNone(outcome["synth_pass"])
        self.assertEqual(outcome["cosim_status"], "NOT_RUN")
        self.assertIsNone(outcome["final_pass"])
        self.assertEqual(outcome["failure_stage"], "PATCH_POLICY_REJECTED")
        stats = build_experience_stats(result.records)
        self.assertEqual(stats["stages"]["patch"], {"attempted": 0, "passed": 0, "failed": 0})
        all_proposals = build_experience_stats(result.records, ranking_only=False)
        self.assertEqual(
            all_proposals["stages"]["patch"],
            {"attempted": 1, "passed": 0, "failed": 1},
        )
        self.assertEqual(stats["stages"]["csim"]["attempted"], 0)
        self.assertEqual(stats["stages"]["synth"]["attempted"], 0)

    def test_real_vitis_failed_terminal_attempt_is_kept_as_negative_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_v3_run(Path(tmp), rejection="PATCH_POLICY_REJECTED")
            result_path = run / "v3_prototype_result.json"
            result_value = json.loads(result_path.read_text(encoding="utf-8"))
            result_value["status"] = "FAILED"
            result_value["backend"]["evidence_level"] = "REAL_VITIS_ATTEMPT_FAILED"
            _write_json(result_path, result_value)
            manifest_path = run / "control" / "package_manifest.json"
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_value["status"] = "FAILED"
            _write_json(manifest_path, manifest_value)

            imported = import_historical_runs([run])

        self.assertEqual(len(imported.records), 1)
        self.assertEqual(imported.records[0]["execution_class"], "REAL_LLM_VITIS")
        self.assertFalse(imported.records[0]["eligible_for_ranking"])
        self.assertEqual(
            imported.records[0]["outcome"]["failure_stage"],
            "PATCH_POLICY_REJECTED",
        )

    def test_incomplete_final_closure_is_not_marked_passed(self) -> None:
        candidate = {"status": "FINAL_VERIFIED"}
        self.assertIsNone(
            _final_pass(
                candidate,
                {
                    "csim": {"status": "PASS"},
                    "synth": {"status": "NOT_RUN"},
                    "cosim": {"status": "NOT_RUN"},
                },
            )
        )

    def test_duplicate_bundle_is_auditable_but_not_ranking_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_v3_run(Path(tmp), rejection="DUPLICATE_STRATEGY_BUNDLE")
            result = import_historical_runs([run])

        record = result.records[0]
        self.assertTrue(record["outcome"]["patch_valid"])
        self.assertFalse(record["outcome"]["candidate_created"])
        self.assertFalse(record["eligible_for_ranking"])
        self.assertEqual(
            record["outcome"]["failure_stage"], "DUPLICATE_STRATEGY_BUNDLE"
        )
        self.assertEqual(build_experience_stats(result.records)["record_count"], 0)
        self.assertEqual(
            build_experience_stats(result.records, ranking_only=False)["record_count"],
            1,
        )

    def test_vitis_unknown_latency_is_never_averaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_v3_run(
                Path(tmp),
                candidate_latency=2_684_355_097,
                candidate_status="REJECTED",
            )
            result = import_historical_runs([run])

        record = result.records[0]
        self.assertIsNone(record["outcome"]["latency_after"])
        self.assertIsNone(record["outcome"]["acceleration"])
        self.assertEqual(record["outcome"]["failure_stage"], "UNKNOWN_OR_UNBOUNDED")
        report = build_import_report(result)
        self.assertGreater(report["unbounded_metric_count"], 0)
        stats = build_experience_stats(result.records)
        self.assertIsNone(stats["averages"]["acceleration"])

    def test_demo_oracle_and_executor_never_enter_default_ranking_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            demo = _make_v3_run(root / "demo", demo=True)
            _write_json(root / "oracle" / "oracle_result.json", {"schema_version": "v3d.oracle-task.v1"})
            _write_json(root / "executor" / "executor_result.json", {"execution_mode": "deterministic-fixture"})
            default = import_historical_runs([root])
            fixtures = import_historical_runs(
                [demo], policy=ImportPolicy(include_fixtures=True)
            )

        self.assertEqual(default.records, ())
        reasons = {audit.reason for audit in default.source_audits}
        self.assertIn("FIXTURE_EXCLUDED_BY_POLICY", reasons)
        self.assertIn("ORACLE_GOLDEN_NOT_AGENT_EXPERIENCE", reasons)
        self.assertIn("DETERMINISTIC_EXECUTOR_NOT_AGENT_EXPERIENCE", reasons)
        self.assertEqual(len(fixtures.records), 1)
        self.assertEqual(fixtures.records[0]["execution_class"], "DEMO_FIXTURE")
        self.assertFalse(fixtures.records[0]["eligible_for_ranking"])

    def test_reports_expose_only_opaque_sources_and_fixed_quality_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_v3_run(Path(tmp), rejection="PATCH_POLICY_REJECTED")
            result = import_historical_runs([run])

        report = build_import_report(result)
        quality = render_data_quality_markdown(result)
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn(str(run), rendered)
        self.assertNotIn("private_task_name_must_not_survive", rendered)
        self.assertEqual(report["record_count"], 1)
        self.assertIn("PATCH_POLICY_REJECTED", rendered)
        self.assertIn("UNKNOWN_OR_UNBOUNDED", quality)
        self.assertIn("Fixture provenance", quality)


if __name__ == "__main__":
    unittest.main()
