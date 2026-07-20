import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from submission_tools.qa import scan_tree, stage_submission, validate_run_provenance


class SubmissionQATests(unittest.TestCase):
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

    def _make_live_provenance(self, run_root: Path) -> dict:
        planner_input = {"task": {"task_id": "task"}}
        input_sha = self._canonical_sha(planner_input)
        self._write_json(run_root / "planner" / "inputs" / "round.json", planner_input)
        request = {
            "logical_operation_id": "b" * 64,
            "planner_fingerprint": "planner:fingerprint",
            "input_sha256": input_sha,
            "request": {
                "provider_request": {
                    "provider": "openai-compatible",
                    "model": "model",
                    "http_body": {"model": "model"},
                }
            },
        }
        request_ref = "planner/requests/one.json"
        self._write_json(run_root / request_ref, request)
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
        outcome = {
            "action_id": action_id,
            "input_sha256": input_sha,
            "proposal": {
                "provider": "openai-compatible-fast-experiment",
                "model": "model",
                "request_id": "req-1",
                "input_tokens": 7,
                "output_tokens": 4,
                "cached_input_tokens": 0,
            },
            "provider_binding": {
                "provider": "openai-compatible-fast-experiment",
                "model": "model",
                "planner_fingerprint": "planner:fingerprint",
            },
            "usage": {
                "tokens_used": 11,
                "input_tokens": 7,
                "output_tokens": 4,
                "cached_input_tokens": 0,
                "request_id": "req-1",
                "usage_complete": True,
            },
        }
        output_ref = f"planner/live_outcomes/{action_id}.json"
        output_sha = self._write_json(run_root / output_ref, outcome)
        self._write_json(
            run_root / "control" / "live_planner_actions" / f"{action_id}.started.json",
            {"action_id": action_id, "status": "STARTED", "request": request_meta},
        )
        self._write_json(
            run_root / "control" / "live_planner_actions" / f"{action_id}.completed.json",
            {
                "action_id": action_id,
                "status": "COMPLETED",
                "request": request_meta,
                "result_ref": output_ref,
                "result_sha256": output_sha,
                "tokens_used": 11,
                "input_tokens": 7,
                "output_tokens": 4,
                "cached_input_tokens": 0,
            },
        )
        return {
            "live_planner_action_id": action_id,
            "live_planner_request_ref": request_ref,
            "live_planner_request_sha256": request_sha,
            "live_planner_output_ref": output_ref,
            "live_planner_output_sha256": output_sha,
            "budget": {
                "tokens_used": 11,
                "input_tokens_used": 7,
                "output_tokens_used": 4,
                "cached_input_tokens_used": 0,
                "token_usage_complete": True,
                "tool_used": {"llm": 1},
            },
        }

    def test_real_llm_provenance_requires_bound_live_artifacts_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_root = Path(temp) / "run"
            result = self._make_live_provenance(run_root)

            self.assertEqual(
                [],
                validate_run_provenance(
                    run_root=run_root,
                    result=result,
                    provider_class="REAL_LLM",
                ),
            )
            result["live_planner_request_ref"] = "../outside.json"
            codes = {
                item.code
                for item in validate_run_provenance(
                    run_root=run_root,
                    result=result,
                    provider_class="REAL_LLM",
                )
            }
            self.assertIn("REAL_LLM_ACTION_BINDING_MISMATCH", codes)

    def test_real_llm_provenance_rejects_hash_model_usage_and_budget_tamper(self) -> None:
        mutations = ("hash", "input", "model", "usage", "budget")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                run_root = Path(temp) / "run"
                result = self._make_live_provenance(run_root)
                outcome_path = run_root / result["live_planner_output_ref"]
                if mutation == "hash":
                    request_path = run_root / result["live_planner_request_ref"]
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    request["request"]["provider_request"]["model"] = "tampered-model"
                    self._write_json(request_path, request)
                    expected = "REAL_LLM_REQUEST_SHA_MISMATCH"
                elif mutation == "input":
                    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                    outcome["input_sha256"] = "0" * 64
                    self._write_json(outcome_path, outcome)
                    expected = "REAL_LLM_INPUT_SHA_MISMATCH"
                elif mutation == "model":
                    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                    outcome["proposal"]["model"] = "other-model"
                    self._write_json(outcome_path, outcome)
                    expected = "REAL_LLM_MODEL_MISMATCH"
                elif mutation == "usage":
                    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                    outcome["proposal"]["request_id"] = "different-request"
                    self._write_json(outcome_path, outcome)
                    expected = "REAL_LLM_USAGE_MISMATCH"
                else:
                    result["budget"]["tokens_used"] = 12
                    expected = "REAL_LLM_BUDGET_USAGE_MISMATCH"
                codes = {
                    item.code
                    for item in validate_run_provenance(
                        run_root=run_root,
                        result=result,
                        provider_class="REAL_LLM",
                    )
                }
                self.assertIn(expected, codes)

    def test_detects_secret_local_path_and_forbidden_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "code.txt").write_text(
                "token=" + "sk-" + "A" * 32
                + "\nOPENAI_API_KEY=" + "opaque-provider-key-123456"
                + "\npath=" + "/ho" + "me/alice/project\n",
                encoding="utf-8",
            )
            (root / "runs").mkdir()
            (root / "runs" / "result.json").write_text("{}", encoding="utf-8")
            codes = {item.code for item in scan_tree(root)}
            self.assertTrue({"API_KEY", "LOCAL_PATH", "FORBIDDEN_PATH"}.issubset(codes))

    def test_detects_planner_golden_and_hidden_like_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            planner = root / "planner" / "inputs"
            planner.mkdir(parents=True)
            (planner / "round.json").write_text(
                json.dumps({"golden_kernel": "answer", "source": "hidden_like/kernel_tb.cpp"}),
                encoding="utf-8",
            )
            findings = [item for item in scan_tree(root) if item.code == "PLANNER_INPUT_LEAK"]
            self.assertGreaterEqual(len(findings), 2)

    def test_direct_planner_inputs_root_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            planner = Path(temp) / "planner" / "inputs"
            planner.mkdir(parents=True)
            (planner / "round.json").write_text(
                json.dumps({"hidden_testbench": "not allowed"}), encoding="utf-8"
            )
            findings = scan_tree(planner)
            self.assertTrue(any(item.code == "PLANNER_INPUT_LEAK" for item in findings))

    def test_staging_excludes_answers_runs_and_marks_not_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            (source / "public").mkdir()
            (source / "public" / "kernel.cpp").write_text("int kernel() { return 1; }\n", encoding="utf-8")
            (source / "public" / "golden").mkdir()
            (source / "public" / "golden" / "kernel.cpp").write_text("answer\n", encoding="utf-8")
            (source / "runs").mkdir()
            (source / "runs" / "result.json").write_text("{}", encoding="utf-8")
            spec = source / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "include": ["public", "runs"],
                        "exclude": ["**/golden/**", "**/runs/**"],
                    }
                ),
                encoding="utf-8",
            )
            output = Path(temp) / "staging"
            manifest = stage_submission(source_root=source, output_root=output, spec_path=spec)
            self.assertEqual("NOT_FINAL", manifest["status"])
            self.assertEqual("DIRTY_OR_UNVERIFIED", manifest["source_tree_state"])
            self.assertTrue((output / "public" / "kernel.cpp").is_file())
            self.assertFalse((output / "public" / "golden").exists())
            self.assertFalse((output / "runs").exists())
            self.assertTrue((output / "STAGING_NOT_FINAL.md").is_file())
            self.assertEqual([], manifest["qa_findings"])
            self.assertEqual(2, manifest["skipped_file_count"])

    def test_staging_explicit_unsafe_file_fails_and_records_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            unsafe = source / "credential.txt"
            unsafe.write_text("OPENAI_API_KEY=literal-provider-secret-123456\n", encoding="utf-8")
            spec = source / "spec.json"
            spec.write_text(json.dumps({"include": ["credential.txt"]}), encoding="utf-8")
            output = Path(temp) / "staging"

            with self.assertRaisesRegex(ValueError, "staging QA failed"):
                stage_submission(source_root=source, output_root=output, spec_path=spec)

            manifest = json.loads((output / "staging_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("FAIL", manifest["qa_status"])
            self.assertEqual("SAFETY_FINDING", manifest["skipped"][0]["reason"])
            self.assertIn("API_KEY", {item["code"] for item in manifest["qa_findings"]})
            self.assertFalse((output / "credential.txt").exists())

    def test_staging_directory_skip_is_explicit_and_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            public = source / "public"
            public.mkdir(parents=True)
            (public / "kernel.cpp").write_text("int kernel() { return 0; }\n", encoding="utf-8")
            (public / "secret.txt").write_text(
                "Authorization: Bearer opaque-secret-value-123456\n", encoding="utf-8"
            )
            spec = source / "spec.json"
            spec.write_text(json.dumps({"include": ["public"]}), encoding="utf-8")
            output = Path(temp) / "staging"

            manifest = stage_submission(source_root=source, output_root=output, spec_path=spec)

            self.assertEqual("PASS", manifest["qa_status"])
            self.assertTrue((output / "public" / "kernel.cpp").is_file())
            self.assertFalse((output / "public" / "secret.txt").exists())
            skipped = {item["path"]: item for item in manifest["skipped"]}
            self.assertEqual("SAFETY_FINDING", skipped["public/secret.txt"]["reason"])
            self.assertIn("AUTHORIZATION", {item["code"] for item in skipped["public/secret.txt"]["findings"]})
            self.assertIn("AUTHORIZATION", {item["code"] for item in manifest["source_findings"]})

    @unittest.skipUnless(shutil.which("git"), "git is required for source provenance test")
    def test_staging_git_dirty_state_cannot_be_overridden_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            (source / "kernel.cpp").write_text("int kernel() { return 0; }\n", encoding="utf-8")
            spec = source / "spec.json"
            spec.write_text(json.dumps({"include": ["kernel.cpp"]}), encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "qa@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Submission QA"], check=True)
            subprocess.run(["git", "-C", str(source), "add", "kernel.cpp", "spec.json"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "fixture"], check=True)
            head = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (source / "kernel.cpp").write_text("int kernel() { return 1; }\n", encoding="utf-8")
            output = Path(temp) / "staging"

            with mock.patch.dict(
                os.environ,
                {"SOURCE_TREE_STATE": "CLEAN_AFTER_MANUAL_GIT_CHECK", "SOURCE_REVISION": "f" * 40},
            ):
                manifest = stage_submission(source_root=source, output_root=output, spec_path=spec)

            self.assertEqual(head, manifest["source_revision"])
            self.assertEqual("git", manifest["source_revision_origin"])
            self.assertEqual("DIRTY", manifest["source_tree_state"])
            self.assertEqual("DIRTY", manifest["source_tree_state_observed"])
            self.assertEqual("CLEAN_AFTER_MANUAL_GIT_CHECK", manifest["source_tree_state_override"])


if __name__ == "__main__":
    unittest.main()
