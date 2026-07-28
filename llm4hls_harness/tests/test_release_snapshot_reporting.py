from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release_tools.full_agent_release_metadata import (
    CLI_POLICY_INTERFACE_VERSION,
    COMBINED_MARKER,
    COMBINED_STATUS,
    DECISION_SCHEMA,
    EFFECTIVE_POLICY_VERSION,
    ReleaseMetadataError,
    TARGETED_RERUN_TASK_IDS,
    absolute_preserving_symlinks,
    aggregate_content_records,
    is_critical_untracked,
    patch_combined_report,
    render_combined_markdown,
    render_status_markdown,
    runtime_critical_paths,
    sanitize_toolchain_receipt,
    sanitized_environment,
    sha256_file,
    test_command,
    validate_report_hashes,
)


def _mode(number: int) -> str:
    if number <= 8:
        return "REPAIR"
    if number <= 14:
        return "SYNTH_FIX"
    if number <= 20:
        return "STRUCTURAL_FIX"
    return "OPTIMIZE"


def _fixture() -> dict[str, object]:
    tasks = []
    total_decisions = 0
    for number in range(1, 29):
        task_id = f"v3d_fast_{number:03d}"
        expected = _mode(number)
        routed = "OPTIMIZE" if number == 18 else expected
        decision_count = 2 if number <= 20 else 1
        total_decisions += decision_count
        first_success = task_id not in TARGETED_RERUN_TASK_IDS
        failure_class = {
            "v3d_fast_012": "PATCH_INVALID",
            "v3d_fast_018": "ARTIFACT_OR_IDENTITY_FAIL",
            "v3d_fast_020": "EXECUTOR_TIMEOUT_AND_INCOMPLETE_ARTIFACT",
        }.get(task_id)
        first = {
            "attempt_kind": "AGENT_RUN",
            "run_id": f"{task_id}--first",
            "run_dir": f"/repo/runs/{task_id}/first",
            "strict_success": first_success,
            "failure_class": failure_class,
        }
        latest = {
            "strict_success": True,
            "source": {
                "campaign": "fixture",
                "run_id": f"{task_id}--latest",
                "run_dir": f"/repo/runs/{task_id}/latest",
                "certified_result_ref": f"/repo/runs/{task_id}/latest/cert.json",
            },
            "agent": {
                "status": "DONE",
                "final_candidate": "candidate_001",
            },
            "routing": {
                "expected_mode": expected,
                "routed_mode": routed,
                "router_correct": expected == routed,
            },
            "usage": {
                "planner_calls": decision_count,
                "tokens": 100,
                "agent_credits": 10,
            },
            "a2": {
                "mode": "enforce",
                "configured_api_version": "v2",
                "effective_policy_version": "v2",
                "decisions": [
                    {
                        "round": index + 1,
                        "decision": "ALLOW",
                        "reason_codes": ["FIXTURE"],
                    }
                    for index in range(decision_count)
                ],
                "false_block_suspected": False,
                "unsafe_finalize_suspected": False,
            },
            "a3": {
                "mode": "guided",
                "decisions": [
                    {
                        "round": index + 1,
                        "decision": "ABSTAIN",
                        "strategy_atom": None,
                    }
                    for index in range(decision_count)
                ],
                "injection_mismatches": 0,
            },
            "b2": {
                "status": "PASS",
                "clock_100mhz": "PASS",
                "observed_period_ns": 1.0,
            },
            "performance": {
                "raw_acceleration": 1.0 if expected == "OPTIMIZE" else None,
            },
            "failure": {"primary_class": None, "reason_codes": []},
        }
        tasks.append(
            {
                "task_id": task_id,
                "expected_mode": expected,
                "first_attempt_result": first,
                "latest_validated_result": latest,
                "targeted_rerun_reason": (
                    "FIXTURE_RERUN" if not first_success else None
                ),
                "attempt_history": [
                    first,
                    *(
                        [
                            {
                                "attempt_kind": "AGENT_RUN",
                                "run_id": f"{task_id}--latest",
                                "strict_success": True,
                            }
                        ]
                        if not first_success
                        else []
                    ),
                ],
                "prior_2026_07_27_coverage_state": "FIXTURE",
                "original_2026_07_27_coverage_record": {
                    "task_id": task_id,
                    "sentinel": "PRESERVE",
                },
            }
        )
    assert total_decisions == 48
    optimize = [
        {
            "task_id": f"v3d_fast_{number:03d}",
            "baseline_latency": 10.0,
            "candidate_latency": 10.0,
            "raw_acceleration": 1.0,
            "capped_acceleration": 1.0,
        }
        for number in range(21, 29)
    ]
    return {
        "schema_version": "old",
        "status": "COMPLETE_STRICT_SUCCESS",
        "markers": [COMBINED_MARKER],
        "configuration": {
            "a2": {
                "configured_api_version": "v2",
                "effective_policy_version": "v2",
            }
        },
        "coverage_identity": {},
        "summary": {
            "coverage": {},
            "usage": {
                "planner_calls_total": 48,
                "planner_calls_mean": 48 / 28,
                "tokens_total": 2800,
                "tokens_mean": 100.0,
                "agent_credits_statistical_sum": 280,
            },
            "by_expected_mode": {
                "REPAIR": {"strict_successes": 8},
                "SYNTH_FIX": {"strict_successes": 6},
                "STRUCTURAL_FIX": {"strict_successes": 6},
                "OPTIMIZE": {"strict_successes": 8},
            },
            "a2": {
                "decision_counts": {"ALLOW": 48},
            },
            "a3": {
                "decision_counts": {"ABSTAIN": 48},
            },
            "performance": {
                "optimize_normalized_acceleration": {"tasks": optimize},
            },
            "official_score_proxy": {"total": 1.0},
        },
        "release_decision": {},
        "outputs": {},
        "tasks": tasks,
    }


class ReleaseReportPatchTests(unittest.TestCase):
    def test_coverage_status_and_attempt_history_are_preserved(self) -> None:
        source = _fixture()
        history_before = json.dumps(
            [
                (
                    task["first_attempt_result"],
                    task["attempt_history"],
                    task["original_2026_07_27_coverage_record"],
                )
                for task in source["tasks"]
            ],
            sort_keys=True,
        )
        report = patch_combined_report(source)
        history_after = json.dumps(
            [
                (
                    task["first_attempt_result"],
                    task["attempt_history"],
                    task["original_2026_07_27_coverage_record"],
                )
                for task in report["tasks"]
            ],
            sort_keys=True,
        )
        self.assertEqual(report["status"], COMBINED_STATUS)
        self.assertEqual(history_after, history_before)
        coverage = report["summary"]["coverage"]
        self.assertEqual(
            coverage["first_attempt"]["strict_successes"], 25
        )
        self.assertEqual(
            coverage["latest_validated_combined"]["strict_successes"], 28
        )
        self.assertEqual(
            coverage["targeted_rerun_task_ids"],
            list(TARGETED_RERUN_TASK_IDS),
        )
        self.assertFalse(coverage["single_batch_28x1"])
        self.assertEqual(
            report["coverage_metrics"]["FIRST_ATTEMPT_STRICT_SUCCESS"][
                "successes"
            ],
            25,
        )
        self.assertEqual(
            report["coverage_metrics"][
                "LATEST_VALIDATED_COMBINED_COVERAGE"
            ]["successes"],
            28,
        )

    def test_a2_version_contract_is_uniform(self) -> None:
        report = patch_combined_report(_fixture())
        normalized = [report["configuration"]["a2"], report["summary"]["a2"]]
        for task in report["tasks"]:
            normalized.append(task["a2_version_contract"])
            normalized.append(task["latest_validated_result"]["a2"])
            normalized.extend(
                task["latest_validated_result"]["a2"]["decisions"]
            )
        for value in normalized:
            self.assertEqual(
                value["cli_policy_interface_version"],
                CLI_POLICY_INTERFACE_VERSION,
            )
            self.assertEqual(
                value["effective_policy_version"],
                EFFECTIVE_POLICY_VERSION,
            )
            self.assertEqual(value["decision_schema"], DECISION_SCHEMA)
            self.assertNotEqual(value["effective_policy_version"], "v2")

    def test_018_route_difference_remains_visible(self) -> None:
        report = patch_combined_report(_fixture())
        task = next(
            item
            for item in report["tasks"]
            if item["task_id"] == "v3d_fast_018"
        )
        routing = task["latest_validated_result"]["routing"]
        self.assertEqual(routing["expected_mode"], "STRUCTURAL_FIX")
        self.assertEqual(routing["routed_mode"], "OPTIMIZE")
        self.assertFalse(routing["route_match"])
        self.assertTrue(routing["runtime_gate_consistent"])
        self.assertEqual(
            report["summary"]["routing"]["expected_mode_matches"], 27
        )

    def test_evidence_boundaries_do_not_claim_causal_benefit(self) -> None:
        report = patch_combined_report(_fixture())
        boundaries = report["evidence_boundaries"]
        self.assertEqual(
            boundaries["no_harm_evidence"][
                "latest_validated_combined_coverage"
            ]["strict_successes"],
            28,
        )
        self.assertEqual(boundaries["no_harm_evidence"]["a2_false_block"], 0)
        self.assertFalse(
            boundaries["causal_benefit_evidence"][
                "same_implementation_and_executor_fingerprint_paired_ablation"
            ]
        )
        self.assertEqual(
            boundaries["causal_benefit_evidence"]["supported_claims"], []
        )

    def test_rendered_reports_show_required_homepage_and_route_columns(self) -> None:
        report = patch_combined_report(_fixture())
        markdown = render_combined_markdown(report)
        status = render_status_markdown(report)
        for text in (markdown, status):
            self.assertIn("First attempt: 25/28", text)
            self.assertIn(
                "Latest validated combined coverage: 28/28", text
            )
            self.assertIn("Targeted reruns: 012, 018, 020", text)
            self.assertIn("Single batch 28×1: No", text)
            self.assertIn("Single-fingerprint causal ablation: No", text)
        self.assertIn("Expected Mode | Routed Mode | Route Match", markdown)
        self.assertIn("不能声称 A2/A3 导致成功率提升", markdown)


class ReleaseSnapshotHelperTests(unittest.TestCase):
    def test_aggregate_is_path_order_independent(self) -> None:
        records = [
            {"path": "b", "size_bytes": 2, "sha256": "b" * 64},
            {"path": "a", "size_bytes": 1, "sha256": "a" * 64},
        ]
        self.assertEqual(
            aggregate_content_records(records),
            aggregate_content_records(reversed(records)),
        )

    def test_critical_untracked_selection_is_narrow(self) -> None:
        self.assertTrue(
            is_critical_untracked(
                "llm4hls_harness/llm4hls_agent/runtime_control.py"
            )
        )
        self.assertTrue(
            is_critical_untracked(
                "llm4hls_harness/release_tools/example.py"
            )
        )
        self.assertFalse(
            is_critical_untracked("llm4hls_harness/runs/run/result.json")
        )
        self.assertFalse(is_critical_untracked("min/minimal_flow.py"))
        self.assertFalse(is_critical_untracked(".env"))

    def test_environment_redacts_secret_and_sanitizes_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "OPENAI_API_KEY=super-secret\n"
                "OPENAI_BASE_URL=https://user:password@example.test/v1?token=x\n"
                "LLM4HLS_TOKEN_BUDGET=32768\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                values = sanitized_environment(root)
            self.assertEqual(
                values["OPENAI_API_KEY"]["value"], "<REDACTED>"
            )
            self.assertEqual(
                values["OPENAI_BASE_URL"]["value"],
                "https://example.test/v1",
            )
            self.assertEqual(
                values["LLM4HLS_TOKEN_BUDGET"]["value"], "32768"
            )

    def test_toolchain_receipt_sanitizes_real_schema_and_keeps_hash(self) -> None:
        raw = {
            "vitis_root": "/home/example/Vitis",
            "selected_executable": "/home/example/Vitis/bin/vitis-run",
            "selected_executable_sha256": "a" * 64,
            "version": "2025.2",
        }
        sanitized = sanitize_toolchain_receipt(raw)
        self.assertEqual(sanitized["vitis_root"], "<VITIS_ROOT>")
        self.assertEqual(
            sanitized["selected_executable"],
            "<VITIS_ROOT>/bin/vitis-run",
        )
        self.assertEqual(
            sanitized["selected_executable_sha256"], "a" * 64
        )
        self.assertNotIn("/home/", json.dumps(sanitized))

    def test_unittest_output_parser_records_count_and_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "test_smoke.py").write_text(
                "import unittest\n"
                "class Smoke(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            result = test_command(
                [sys.executable, "-m", "unittest", "test_smoke.py"],
                cwd=root,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["test_count"], 1)

    def test_absolute_interpreter_path_preserves_virtualenv_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "base-python"
            target.write_text("", encoding="utf-8")
            link = root / "venv-python"
            link.symlink_to(target)
            rendered = absolute_preserving_symlinks(link)
        self.assertEqual(rendered, link)
        self.assertNotEqual(rendered, target.resolve())

    def test_runtime_fingerprint_excludes_report_only_tools(self) -> None:
        root = Path(__file__).resolve().parents[2]
        relative = {
            path.relative_to(root).as_posix()
            for path in runtime_critical_paths(root)
        }
        self.assertFalse(
            any(path.startswith("llm4hls_harness/release_tools/") for path in relative)
        )
        self.assertIn(
            "llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py",
            relative,
        )

    def test_report_hash_index_validates_without_self_hash_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            snapshot = root / "snapshot"
            snapshot.mkdir(parents=True)
            combined = root / "combined.json"
            detail = root / "combined.md"
            status = root / "status.md"
            release = snapshot / "release_snapshot_report.md"
            for path, content in (
                (combined, "{}\n"),
                (detail, "detail\n"),
                (status, "status\n"),
                (release, "release\n"),
            ):
                path.write_text(content, encoding="utf-8")
            index = {
                "schema_version": "v3d.report-hashes.v1",
                "hash_graph": "ACYCLIC_EXTERNAL_INDEX",
                "reports": {
                    "combined_json": {
                        "ref": "combined.json",
                        "size_bytes": combined.stat().st_size,
                        "sha256": sha256_file(combined),
                    },
                    "combined_markdown": {
                        "ref": "combined.md",
                        "size_bytes": detail.stat().st_size,
                        "sha256": sha256_file(detail),
                    },
                    "status_markdown": {
                        "ref": "status.md",
                        "size_bytes": status.stat().st_size,
                        "sha256": sha256_file(status),
                    },
                    "release_snapshot_report": {
                        "ref": "release_snapshot_report.md",
                        "size_bytes": release.stat().st_size,
                        "sha256": sha256_file(release),
                    },
                },
            }
            (snapshot / "report_hashes.json").write_text(
                json.dumps(index), encoding="utf-8"
            )
            validated = validate_report_hashes(
                snapshot_dir=snapshot, root=root
            )
            self.assertEqual(validated["hash_graph"], "ACYCLIC_EXTERNAL_INDEX")

    def test_report_hash_index_rejects_empty_report_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            snapshot = root / "snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "report_hashes.json").write_text(
                '{"reports": {}}\n', encoding="utf-8"
            )
            with self.assertRaises(ReleaseMetadataError):
                validate_report_hashes(snapshot_dir=snapshot, root=root)


if __name__ == "__main__":
    unittest.main()
