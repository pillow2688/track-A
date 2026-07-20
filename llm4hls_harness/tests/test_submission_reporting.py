import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from submission_tools.reporting import create_benchmark_snapshot, create_oracle_snapshot, generate_submission_docs


class SubmissionReportingTests(unittest.TestCase):
    @staticmethod
    def _canonical_sha(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _write_json(path: Path, value: object) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _make_run(self, repo: Path, *, run_id: str, llm_calls: int, provider: str) -> dict:
        root = repo / "llm4hls_harness" / "runs" / run_id
        (root / "metrics").mkdir(parents=True)
        (root / "planner" / "outputs").mkdir(parents=True)
        (root / "metrics" / "baseline.json").write_text(
            json.dumps({"report": {"latency": {"worst": 100}}}), encoding="utf-8"
        )
        (root / "metrics" / "final.json").write_text(
            json.dumps({"report": {"latency": {"worst": 25}}}), encoding="utf-8"
        )
        live_fields = {}
        candidate_live_fields = {}
        live_artifact_refs = []
        if llm_calls:
            planner_input = {"task": {"task_id": "dotProduct_optimize"}}
            input_sha = self._canonical_sha(planner_input)
            self._write_json(root / "planner" / "inputs" / "round.json", planner_input)
            request = {
                "logical_operation_id": "b" * 64,
                "planner_fingerprint": "planner:fingerprint",
                "input_sha256": input_sha,
                "request": {
                    "provider_request": {
                        "provider": "openai-compatible",
                        "model": "test-model",
                        "http_body": {"model": "test-model"},
                    }
                },
            }
            request_ref = "planner/requests/one.json"
            self._write_json(root / request_ref, request)
            request_sha = self._canonical_sha(request)
            request_meta = {
                "input_ref": "planner/inputs/round.json",
                "input_sha256": input_sha,
                "logical_operation_id": "b" * 64,
                "planner_fingerprint": "planner:fingerprint",
                "request_ref": request_ref,
                "request_sha256": request_sha,
            }
            action_id = self._canonical_sha(request_meta)
            proposal = {
                "provider": "openai-compatible-fast-experiment",
                "model": "test-model",
                "hypothesis": "parallel reduction",
                "request_id": "request-1",
                "input_tokens": 20,
                "output_tokens": 10,
                "cached_input_tokens": 0,
                "patch": "--- kernel.cpp\n+++ kernel.cpp\n@@ -1 +1 @@\n-int dotProduct() { return 0; }\n+int dotProduct() { return 1; }\n",
            }
            outcome = {
                "action_id": action_id,
                "input_sha256": input_sha,
                "proposal": proposal,
                "provider_binding": {
                    "provider": "openai-compatible-fast-experiment",
                    "model": "test-model",
                    "planner_fingerprint": "planner:fingerprint",
                },
                "usage": {
                    "tokens_used": 30,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "cached_input_tokens": 0,
                    "request_id": "request-1",
                    "usage_complete": True,
                },
            }
            output_ref = f"planner/live_outcomes/{action_id}.json"
            output_sha = self._write_json(root / output_ref, outcome)
            self._write_json(
                root / "control" / "live_planner_actions" / f"{action_id}.started.json",
                {"action_id": action_id, "status": "STARTED", "request": request_meta},
            )
            self._write_json(
                root / "control" / "live_planner_actions" / f"{action_id}.completed.json",
                {
                    "action_id": action_id,
                    "status": "COMPLETED",
                    "request": request_meta,
                    "result_ref": output_ref,
                    "result_sha256": output_sha,
                    "tokens_used": 30,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "cached_input_tokens": 0,
                },
            )
            self._write_json(root / "planner" / "outputs" / "one.json", {"proposal": proposal})
            live_fields = {
                "live_planner_action_id": action_id,
                "live_planner_request_ref": request_ref,
                "live_planner_request_sha256": request_sha,
                "live_planner_output_ref": output_ref,
                "live_planner_output_sha256": output_sha,
            }
            patch_ref = "candidates/candidate_001/patch.diff"
            patch_path = root / patch_ref
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(proposal["patch"], encoding="utf-8")
            candidate_live_fields = {
                **live_fields,
                "provider": proposal["provider"],
                "model": proposal["model"],
                "patch_ref": patch_ref,
                "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
            }
            live_artifact_refs = [
                "planner/inputs/round.json",
                request_ref,
                output_ref,
                f"control/live_planner_actions/{action_id}.started.json",
                f"control/live_planner_actions/{action_id}.completed.json",
                patch_ref,
            ]

        source_ref = "candidates/candidate_001/source/kernel.cpp"
        source_path = root / source_ref
        source_path.parent.mkdir(parents=True)
        source_path.write_text("int dotProduct() { return 1; }\n", encoding="utf-8")
        code_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        task_spec = {
            "task_id": "dotProduct_optimize",
            "top": "dotProduct",
            "kernel_file": "kernel.cpp",
            "public_tb": "kernel_tb.cpp",
            "public_file_hashes": {"kernel.cpp": "d" * 64, "kernel_tb.cpp": "e" * 64},
        }
        self._write_json(root / "v3_task_spec.json", task_spec)
        task_fingerprint = self._canonical_sha(
            {
                "task_id": task_spec["task_id"],
                "top": task_spec["top"],
                "kernel_file": task_spec["kernel_file"],
                "public_tb": task_spec["public_tb"],
                "public_file_hashes": task_spec["public_file_hashes"],
            }
        )
        backend_fingerprint = "llm4hls_agent.vitis.VitisBackend:test"
        final = {}
        for tool in ("csim", "synth", "cosim"):
            action_payload = {
                "kind": tool,
                "candidate_id": "candidate_001",
                "code_hash": code_hash,
                "tool_config_hash": hashlib.sha256(tool.encode()).hexdigest(),
                "backend_fingerprint": backend_fingerprint,
                "task_fingerprint": task_fingerprint,
                "validation_scope": "final",
            }
            final_action_id = self._canonical_sha(action_payload)
            result_ref = f"actions/{final_action_id}/result.json"
            action_result = {
                **action_payload,
                "action_id": final_action_id,
                "result_ref": result_ref,
                "status": "PASS",
                "ok": True,
                "phase": "pass",
                "cached": False,
            }
            self._write_json(root / result_ref, action_result)
            final[tool] = dict(action_result)
        registry = {
            "task_id": "dotProduct_optimize",
            "final_candidate_id": "candidate_001",
            "candidates": {
                "candidate_001": {
                    "candidate_id": "candidate_001",
                    "code_hash": code_hash,
                    "source_ref": source_ref,
                    **candidate_live_fields,
                }
            },
        }
        self._write_json(root / "candidate_registry.json", registry)
        result = {
            "task_id": "dotProduct_optimize",
            "mode": "OPTIMIZE",
            "status": "DONE",
            "stop_reason": "FINALIZED",
            "validation_profile": "fast-experiment",
            "backend": {
                "class": "llm4hls_agent.vitis.VitisBackend",
                "evidence_level": "REAL_VITIS_VALIDATED",
                "fingerprint": backend_fingerprint,
            },
            "baseline_metrics_ref": "metrics/baseline.json",
            "final_metrics_ref": "metrics/final.json",
            "final_attempt_candidate_id": "candidate_001",
            "final_candidate_id": "candidate_001",
            "final_validation": final,
            "budget": {
                "tokens_used": 30 if llm_calls else 0,
                "input_tokens_used": 20 if llm_calls else 0,
                "output_tokens_used": 10 if llm_calls else 0,
                "cached_input_tokens_used": 0,
                "token_usage_complete": bool(llm_calls),
                "credits_used": 35,
                "runtime_used_seconds": 12.5,
                "tool_used": {"llm": llm_calls, "csim": 3, "synth": 3, "cosim": 1},
            },
            **live_fields,
        }
        artifact_refs = [
            "v3_task_spec.json",
            "candidate_registry.json",
            source_ref,
            *live_artifact_refs,
            *(final[tool]["result_ref"] for tool in ("csim", "synth", "cosim")),
        ]
        artifacts = []
        for reference in artifact_refs:
            path = root / reference
            artifacts.append(
                {
                    "path": reference,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                }
            )
        package_manifest = {
            "task_id": result["task_id"],
            "status": result["status"],
            "final_candidate_id": "candidate_001",
            "terminal_payload_sha256": self._canonical_sha(result),
            "artifacts": artifacts,
        }
        manifest_ref = "control/package_manifest.json"
        self._write_json(root / manifest_ref, package_manifest)
        result["package"] = {
            "manifest_ref": manifest_ref,
            "manifest_sha256": self._canonical_sha(package_manifest),
        }
        self._write_json(root / "v3_prototype_result.json", result)
        return {"run_id": run_id, "provider_class": provider, "mode": "OPTIMIZE"}

    def test_generates_five_fact_based_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="real-run", llm_calls=1, provider="REAL_LLM")
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec], "blockers": {}}),
                encoding="utf-8",
            )
            output = repo / "docs"
            paths = generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=output)
            self.assertEqual(5, len(paths))
            table = (output / "experiment_tables.md").read_text(encoding="utf-8")
            self.assertIn("100", table)
            self.assertIn("25", table)
            self.assertIn("4", table)
            self.assertIn("REAL_LLM", table)
            self.assertIn("TODO", table)
            demo = (output / "demo_script_5min.md").read_text(encoding="utf-8")
            self.assertIn("100 → 25 cycles", demo)
            self.assertIn("fresh", demo)

    def test_rejects_false_real_llm_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="scripted", llm_calls=0, provider="REAL_LLM")
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "REAL_LLM requires"):
                generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=repo / "docs")

    def test_legacy_non_real_report_does_not_require_live_or_sealed_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="legacy", llm_calls=0, provider="DETERMINISTIC")
            result_path = repo / "llm4hls_harness" / "runs" / "legacy" / "v3_prototype_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result.pop("package")
            result["backend"] = {"evidence_level": "DETERMINISTIC_FIXTURE_NOT_REAL_VITIS"}
            self._write_json(result_path, result)
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec]}),
                encoding="utf-8",
            )

            paths = generate_submission_docs(
                repo_root=repo,
                manifest_path=manifest,
                output_dir=repo / "docs",
            )

            self.assertEqual(5, len(paths))
            self.assertIn("DETERMINISTIC", (repo / "docs" / "experiment_tables.md").read_text(encoding="utf-8"))

    def test_rejects_real_llm_without_live_request_and_output_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="unbound", llm_calls=1, provider="REAL_LLM")
            result_path = repo / "llm4hls_harness" / "runs" / "unbound" / "v3_prototype_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result.pop("live_planner_request_ref")
            result.pop("live_planner_output_ref")
            result_path.write_text(json.dumps(result), encoding="utf-8")
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "PROVENANCE_REF_MISSING"):
                generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=repo / "docs")

    def test_rejects_real_llm_without_positive_complete_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="zero-tokens", llm_calls=1, provider="REAL_LLM")
            result_path = repo / "llm4hls_harness" / "runs" / "zero-tokens" / "v3_prototype_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["budget"]["tokens_used"] = 0
            result_path.write_text(json.dumps(result), encoding="utf-8")
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "REAL_LLM_TOKENS_INVALID"):
                generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=repo / "docs")

    def test_llm_plus_vitis_claim_requires_real_vitis_and_fresh_final(self) -> None:
        for case in ("not-real-vitis", "cached-final"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp)
                spec = self._make_run(repo, run_id=case, llm_calls=1, provider="REAL_LLM")
                result_path = repo / "llm4hls_harness" / "runs" / case / "v3_prototype_result.json"
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if case == "not-real-vitis":
                    result["backend"]["evidence_level"] = "DETERMINISTIC_FIXTURE_NOT_REAL_VITIS"
                else:
                    result["final_validation"]["cosim"]["cached"] = True
                result_path.write_text(json.dumps(result), encoding="utf-8")
                manifest = repo / "evidence.json"
                manifest.write_text(
                    json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec]}),
                    encoding="utf-8",
                )
                output = repo / "docs"

                generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=output)

                table = (output / "experiment_tables.md").read_text(encoding="utf-8")
                self.assertIn("| OPTIMIZE | 仅失败证据/待补 |", table)
                demo = (output / "demo_script_5min.md").read_text(encoding="utf-8")
                self.assertIn("TODO：补录 dotProduct 真实结果", demo)
                checklist = (output / "submission_checklist.md").read_text(encoding="utf-8")
                self.assertIn("- [ ] dotProduct 真实模型 + Vitis", checklist)

    def test_rejects_tampered_package_terminal_and_final_action_evidence(self) -> None:
        cases = {
            "manifest-sha": "PACKAGE_MANIFEST_SHA_MISMATCH",
            "terminal-payload": "TERMINAL_PAYLOAD_SHA_MISMATCH",
            "action-file": "FINAL_ACTION_BINDING_MISMATCH",
            "manifest-action-hash": "PACKAGE_ARTIFACT_HASH_MISMATCH",
        }
        for case, expected in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                repo = Path(temp)
                spec = self._make_run(repo, run_id=case, llm_calls=1, provider="REAL_LLM")
                root = repo / "llm4hls_harness" / "runs" / case
                result_path = root / "v3_prototype_result.json"
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if case == "manifest-sha":
                    result["package"]["manifest_sha256"] = "0" * 64
                    self._write_json(result_path, result)
                elif case == "terminal-payload":
                    result["stop_reason"] = "TAMPERED"
                    self._write_json(result_path, result)
                elif case == "action-file":
                    action_path = root / result["final_validation"]["cosim"]["result_ref"]
                    action = json.loads(action_path.read_text(encoding="utf-8"))
                    action["candidate_id"] = "candidate_999"
                    self._write_json(action_path, action)
                else:
                    manifest_path = root / result["package"]["manifest_ref"]
                    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    synth_ref = result["final_validation"]["synth"]["result_ref"]
                    next(
                        item for item in package_manifest["artifacts"] if item["path"] == synth_ref
                    )["sha256"] = "f" * 64
                    self._write_json(manifest_path, package_manifest)
                    result["package"]["manifest_sha256"] = self._canonical_sha(package_manifest)
                    self._write_json(result_path, result)
                evidence_manifest = repo / "evidence.json"
                evidence_manifest.write_text(
                    json.dumps({"runs_root": "llm4hls_harness/runs", "curated_runs": [spec]}),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, expected):
                    generate_submission_docs(
                        repo_root=repo,
                        manifest_path=evidence_manifest,
                        output_dir=repo / "docs",
                    )

    def test_reads_explicit_benchmark_summary_without_promoting_deterministic_to_real(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="real-run", llm_calls=1, provider="REAL_LLM")
            benchmark = repo / "bench" / "benchmark_summary.json"
            benchmark.parent.mkdir()
            benchmark.write_text(
                json.dumps(
                    {
                        "benchmark_fingerprint": "abcdef0123456789",
                        "selection": {"tasks_selected": 28},
                        "execution": {"records": 28},
                        "real_evidence_headline": {
                            "runs": 0,
                            "e2e_success_rate": None,
                            "fresh_final_success_rate": None,
                        },
                        "by_mode": {
                            "REPAIR": {
                                "runs": 8,
                                "e2e_successes": 8,
                                "e2e_success_rate": 1.0,
                                "failures": {"by_stage": {}},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps(
                    {
                        "runs_root": "llm4hls_harness/runs",
                        "curated_runs": [spec],
                        "benchmark_summaries": ["bench/benchmark_summary.json"],
                    }
                ),
                encoding="utf-8",
            )
            output = repo / "docs"
            generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=output)
            table = (output / "experiment_tables.md").read_text(encoding="utf-8")
            self.assertIn("bench/benchmark_summary.json", table)
            self.assertIn("| 28 | 28 | 0 |", table)

    def test_benchmark_snapshot_drops_local_paths_and_preserves_evidence_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "summary.json"
            source.write_text(
                json.dumps(
                    {
                        "benchmark_fingerprint": "abc",
                        "configuration": {
                            "backend": "deterministic",
                            "models": ["fixture"],
                            "corpus": ["/private/local/corpus"],
                            "output_dir": "/private/local/output",
                        },
                        "selection": {"tasks_selected": 4},
                        "execution": {"records": 4},
                        "all_attempt_population": {"runs": 5, "failures": {"count": 1}},
                        "latest_slot_population": {"runs": 4, "failures": {"count": 0}},
                        "real_evidence_headline": {"runs": 0},
                        "by_evidence_class": {"DETERMINISTIC": {"runs": 4}, "REAL": {"runs": 0}},
                    }
                ),
                encoding="utf-8",
            )
            output = create_benchmark_snapshot(source_path=source, output_path=root / "snapshot.json")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("corpus", payload["configuration"])
            self.assertNotIn("output_dir", payload["configuration"])
            self.assertEqual(4, payload["by_evidence_class"]["DETERMINISTIC"]["runs"])
            self.assertEqual(0, payload["real_evidence_headline"]["runs"])
            self.assertEqual(5, payload["all_attempt_population"]["runs"])
            self.assertEqual(4, payload["latest_slot_population"]["runs"])

    def test_oracle_anchor_and_replay_releases_are_rendered_with_evidence_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            spec = self._make_run(repo, run_id="real-run", llm_calls=1, provider="REAL_LLM")
            oracle = repo / "oracle.json"
            oracle.write_text(
                json.dumps(
                    {
                        "counts": {"accepted": 28, "rejected": 0, "pending": 0, "real_vitis_anchors": 0},
                        "execution": {"resumed_tasks": 28},
                        "backend": {"evidence_class": "deterministic"},
                    }
                ),
                encoding="utf-8",
            )
            anchors = repo / "anchors.json"
            anchors.write_text(
                json.dumps(
                    {
                        "corpus": {"deterministic_checks": 132, "deterministic_accepted": 28, "deterministic_rejected": 0},
                        "valid_real_vitis_anchors": [
                            {
                                "mode": mode,
                                "task_id": f"task-{mode}",
                                "run_id": f"anchor-{mode}",
                                "status": "ACCEPTED",
                                "checks": ["one"],
                            }
                            for mode in ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
                        ],
                        "invalidated_historical_rows": [
                            {"run_id": "old", "task_id": "repair", "reason": "missing golden Synth"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            replay = repo / "replay.json"
            replay.write_text(
                json.dumps(
                    {
                        "evidence_boundary": {
                            "vitis": "REAL_VITIS_VALIDATED",
                            "planner_in_successful_replays": "NOT_REAL_LLM",
                            "atomic_real_llm_acceptance": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            probe_rerun = repo / "probe-rerun.json"
            probe_rerun.write_text(
                json.dumps(
                    {
                        "attempts": [
                            {"accepted": 8, "rejected": 4},
                            {"accepted": 0, "rejected": 4},
                        ],
                        "conclusion": {"overall_status": "PARTIAL_XSIM_BLOCKED"},
                    }
                ),
                encoding="utf-8",
            )
            manifest = repo / "evidence.json"
            manifest.write_text(
                json.dumps(
                    {
                        "runs_root": "llm4hls_harness/runs",
                        "curated_runs": [spec],
                        "oracle_summary": "oracle.json",
                        "real_anchor_release": "anchors.json",
                        "replay_release": "replay.json",
                        "vitis_probe_rerun_release": "probe-rerun.json",
                    }
                ),
                encoding="utf-8",
            )
            output = repo / "docs"
            generate_submission_docs(repo_root=repo, manifest_path=manifest, output_dir=output)
            table = (output / "experiment_tables.md").read_text(encoding="utf-8")
            self.assertIn("deterministic checks=132", table)
            self.assertIn("anchor-STRUCTURAL_FIX", table)
            failure = (output / "failure_analysis.md").read_text(encoding="utf-8")
            self.assertIn("missing golden Synth", failure)
            reproducibility = (output / "reproducibility.md").read_text(encoding="utf-8")
            self.assertIn("successful replay planner=NOT_REAL_LLM", reproducibility)
            self.assertIn("A01=8 accepted/4 rejected", reproducibility)
            self.assertIn("A02 retry=0 accepted/4 rejected", reproducibility)
            self.assertIn("PARTIAL_XSIM_BLOCKED", reproducibility)
            checklist = (output / "submission_checklist.md").read_text(encoding="utf-8")
            self.assertIn("[x] deterministic Oracle 28 accepted", checklist)

    def test_oracle_snapshot_drops_paths_and_keeps_authority_and_resume_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "summary.json"
            source.write_text(
                json.dumps(
                    {
                        "backend": {"evidence_class": "deterministic", "real_anchor_authorized": False},
                        "configuration": {"output_dir": "/private/run", "vitis_root": "/private/vitis"},
                        "corpus": {"root": "/private/corpus", "selected_tasks": 28},
                        "counts": {"accepted": 28, "rejected": 0, "real_vitis_anchors": 0},
                        "execution": {"resumed_tasks": 28, "queue_ref": "/private/queue.json"},
                    }
                ),
                encoding="utf-8",
            )
            output = create_oracle_snapshot(source_path=source, output_path=root / "snapshot.json")
            payload = json.loads(output.read_text(encoding="utf-8"))
            encoded = json.dumps(payload)
            self.assertNotIn("/private", encoded)
            self.assertEqual(28, payload["counts"]["accepted"])
            self.assertEqual(28, payload["execution"]["resumed_tasks"])
            self.assertFalse(payload["backend"]["real_anchor_authorized"])


if __name__ == "__main__":
    unittest.main()
