from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from llm4hls_agent.cli import main
    from llm4hls_agent.tools import BackendResult
    from llm4hls_agent.v2_team_report import V2TeamReportError
    from llm4hls_agent.v3_prototype_cli import main as v3_prototype_main
except ModuleNotFoundError:
    def main(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("V0 CLI is not implemented")

    def BackendResult(*_args: object, **_kwargs: object):  # type: ignore[misc]
        raise AssertionError("V0 CLI is not implemented")

    class V2TeamReportError(RuntimeError):
        pass

    def v3_prototype_main(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("V3 prototype CLI is not implemented")


def make_task(root: Path) -> None:
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "cli_fixture"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 40",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text('#include "kernel.h"\nvoid kernel() {}\n', encoding="utf-8")
    (root / "kernel.h").write_text("void kernel();\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class PassingBackend:
    def run(self, kind: str, **_kwargs: object):
        if kind == "synth":
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                report={
                    "estimated_clock_period_ns": 4.0,
                    "latency": {"best": 1, "average": 1, "worst": 1},
                    "interval": {"min": 1, "max": 1},
                    "resources": {},
                    "available_resources": {},
                    "utilization_percent": {},
                },
            )
        if kind == "cosim":
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                cosim={"status": "Pass", "latency": {"min": 1, "average": 1, "max": 1}},
            )
        return BackendResult(True, "pass", 0, 0.1)


class RepairingBackend(PassingBackend):
    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object):
        if b"value - 1" in kernel_bytes:
            return BackendResult(False, "runtime_fail", 1, 0.1, ["kernel.cpp:2 mismatch"])
        return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)


class CliTests(unittest.TestCase):
    def test_report_v2_run_prints_external_offline_report_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            output = root / "review.md"
            stdout = io.StringIO()
            with patch(
                "llm4hls_agent.cli.write_v2_team_report",
                return_value=output.resolve(),
            ) as write, patch(
                "llm4hls_agent.cli.manifest_digest", return_value="abc123"
            ), redirect_stdout(stdout):
                return_code = main(
                    [
                        "report-v2-run",
                        "--run-dir",
                        str(run_dir),
                        "--output",
                        str(output),
                    ]
                )

        summary = json.loads(stdout.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(summary["status"], "DONE")
        self.assertEqual(summary["report"], str(output.resolve()))
        self.assertEqual(summary["manifest_sha256"], "abc123")
        self.assertEqual(write.call_args.kwargs["mode"], "offline")

    def test_report_v2_run_error_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            output = root / "review.md"
            output.write_text("keep-me", encoding="utf-8")
            stderr = io.StringIO()
            with patch(
                "llm4hls_agent.cli.write_v2_team_report",
                side_effect=V2TeamReportError("MANIFEST_VERIFICATION_FAILED"),
            ), redirect_stderr(stderr):
                return_code = main(
                    [
                        "report-v2-run",
                        "--run-dir",
                        str(run_dir),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "keep-me")
        error = json.loads(stderr.getvalue())
        self.assertEqual(return_code, 3)
        self.assertEqual(error["error_type"], "V2TeamReportError")

    def test_review_v2_prints_flat_markdown_report_references(self) -> None:
        expected = {
            "status": "PASS",
            "report_ref": "V2_ACCEPTANCE_REPORT.md",
            "report_cn_ref": "V2_ACCEPTANCE_REPORT_CN.md",
            "review_data_digest": "abc",
            "summary": {"credits_used": 176},
        }
        stdout = io.StringIO()
        with patch(
            "llm4hls_agent.cli.generate_v2_review_reports", return_value=expected
        ) as generate, redirect_stdout(stdout):
            return_code = main(["review-v2", "--runs-root", "runs"])

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        arguments = generate.call_args.args
        self.assertEqual(arguments[1], Path("runs/v2-optimize-final"))
        self.assertEqual(arguments[2], Path("runs/v2-safety-rejection-final"))
        self.assertEqual(arguments[4], Path("runs"))

    def test_accept_v2_command_prints_machine_and_bilingual_report_refs(self) -> None:
        expected = {
            "overall_status": "PASS",
            "evidence_tier": "REAL",
            "summary": {"credits_used": 152, "tokens_used": 490},
        }
        stdout = io.StringIO()
        with patch(
            "llm4hls_agent.cli.evaluate_v2_acceptance", return_value=expected
        ) as evaluate, redirect_stdout(stdout):
            return_code = main(
                [
                    "accept-v2",
                    "--optimization-run",
                    "runs/v2-opt",
                    "--rejection-run",
                    "runs/v2-reject",
                    "--output-dir",
                    "runs/v2-acceptance",
                ]
            )

        summary = json.loads(stdout.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["result_ref"], "acceptance_result.json")
        self.assertEqual(summary["report_ref"], "acceptance_report.md")
        self.assertEqual(summary["report_cn_ref"], "acceptance_report_CN.md")
        self.assertEqual(evaluate.call_args.args[1], Path("runs/v2-opt"))

    def test_reject_v2_command_prints_audited_safety_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            patch_file = root / "regression.diff"
            patch_file.write_text("fixture", encoding="utf-8")
            expected = {
                "task_id": "cli_fixture",
                "status": "DONE",
                "stop_reason": "SAFETY_REGRESSION_REJECTED",
                "rejected_candidate_id": "candidate_001",
                "best_candidate_id": "candidate_000",
                "final_candidate_id": "candidate_000",
                "budget": {"credits_used": 26, "tokens_used": 0},
            }
            stdout = io.StringIO()
            with patch(
                "llm4hls_agent.cli.run_v2_rejection", return_value=expected
            ) as run, redirect_stdout(stdout):
                return_code = main(
                    [
                        "reject-v2",
                        str(task_dir),
                        "--run-dir",
                        str(root / "run"),
                        "--patch-file",
                        str(patch_file),
                        "--credit-limit",
                        "160",
                    ]
                )

            summary = json.loads(stdout.getvalue())
            self.assertEqual(return_code, 0)
            self.assertEqual(summary["status"], "DONE")
            self.assertEqual(summary["rejected_candidate_id"], "candidate_001")
            self.assertEqual(summary["credits_used"], 26)
            self.assertEqual(summary["tokens_used"], 0)
            self.assertEqual(run.call_args.args[2].budget.tool_limits["llm"], 0)

    def test_optimize_command_builds_v2_config_and_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            expected = {
                "schema_version": 1,
                "workflow": "V2_CANDIDATE_PPA",
                "task_id": "cli_fixture",
                "status": "DONE",
                "stop_reason": "NO_IMPROVEMENT_LIMIT",
                "best_candidate_id": "candidate_002",
                "final_candidate_id": "candidate_002",
                "rounds": [{}, {}, {}, {}],
                "budget": {"credits_used": 126, "tokens_used": 700},
            }
            stdout = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_BASE_URL": "https://api.deepseek.com",
                    "OPENAI_API_KEY": "secret-test-key",
                    "LLM4HLS_MODEL": "deepseek-v4-pro",
                },
                clear=False,
            ), patch(
                "llm4hls_agent.cli.run_v2", return_value=expected
            ) as run, redirect_stdout(stdout):
                return_code = main(
                    [
                        "optimize",
                        str(task_dir),
                        "--run-dir",
                        str(root / "run"),
                        "--credit-limit",
                        "160",
                        "--max-optimization-rounds",
                        "4",
                        "--max-no-improvement-rounds",
                        "2",
                    ]
                )

            summary = json.loads(stdout.getvalue())
            self.assertEqual(return_code, 0)
            self.assertEqual(summary["status"], "DONE")
            self.assertEqual(summary["rounds_completed"], 4)
            self.assertEqual(summary["credits_used"], 126)
            self.assertEqual(summary["tokens_used"], 700)
            run_config = run.call_args.args[2]
            optimization_config = run.call_args.args[3]
            provider = run.call_args.args[4]
            self.assertEqual(run_config.budget.credit_limit, 160)
            self.assertEqual(run_config.budget.tool_limits["llm"], 6)
            self.assertEqual(run_config.minimum_frequency_mhz, 200.0)
            self.assertEqual(optimization_config.max_rounds, 4)
            self.assertEqual(optimization_config.max_no_improvement_rounds, 2)
            self.assertEqual(optimization_config.max_final_attempts, 2)
            self.assertTrue(optimization_config.scoring.official_score_enabled)
            self.assertEqual(
                optimization_config.exploration_cosim_policy,
                "official_score_gate",
            )
            self.assertEqual(provider.config.model, "deepseek-v4-pro")

    def test_review_v1_prints_flat_offline_report_references(self) -> None:
        expected = {
            "status": "PASS",
            "acceptance_result_ref": "v1-acceptance/acceptance_result.json",
            "report_ref": "V1_ACCEPTANCE_REPORT.md",
            "report_cn_ref": "V1_ACCEPTANCE_REPORT_CN.md",
            "review_data_digest": "abc",
            "summary": {"acceptance_cases": 4},
        }
        stdout = io.StringIO()
        with patch(
            "llm4hls_agent.cli.generate_review_reports", return_value=expected
        ) as generate, redirect_stdout(stdout):
            return_code = main(["review-v1", "--runs-root", "runs"])

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        arguments = generate.call_args.args
        self.assertEqual(arguments[3], Path("runs"))
        self.assertEqual(
            arguments[1]["functional_mismatch"],
            Path("runs/v1-functional-final-2"),
        )

    def test_run_command_writes_result_and_prints_machine_readable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            run_dir = root / "run"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                return_code = main(
                    [
                        "run",
                        str(task_dir),
                        "--run-dir",
                        str(run_dir),
                        "--credit-limit",
                        "40",
                        "--cosim-timeout",
                        "30",
                    ],
                    backend=PassingBackend(),
                )

            summary = json.loads(stdout.getvalue())
            self.assertEqual(return_code, 0)
            self.assertEqual(summary["task_id"], "cli_fixture")
            self.assertEqual(summary["status"], "DONE")
            self.assertEqual(summary["credits_used"], 25)
            self.assertTrue((run_dir / "workflow_result.json").is_file())
            self.assertTrue((run_dir / "artifact_manifest.json").is_file())

    def test_invalid_numeric_environment_is_a_machine_readable_config_error(self) -> None:
        stderr = io.StringIO()
        with patch.dict(
            "os.environ", {"LLM4HLS_CSIM_TIMEOUT_S": "not-a-number"}
        ), redirect_stderr(stderr):
            return_code = main(["run", "unused", "--run-dir", "unused-run"])

        error = json.loads(stderr.getvalue())
        self.assertEqual(return_code, 3)
        self.assertEqual(error["status"], "ERROR")
        self.assertEqual(error["error_type"], "ValueError")

    def test_static_v1_repair_command_is_an_explicit_offline_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            (task_dir / "kernel.cpp").write_text(
                '#include "kernel.h"\nvoid kernel() { int value = 1; value = value - 1; }\n',
                encoding="utf-8",
            )
            patch_file = root / "repair.diff"
            patch_file.write_text(
                "--- a/kernel.cpp\n"
                "+++ b/kernel.cpp\n"
                "@@ -1,2 +1,2 @@\n"
                ' #include "kernel.h"\n'
                "-void kernel() { int value = 1; value = value - 1; }\n"
                "+void kernel() { int value = 1; value = value + 1; }\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = main(
                    [
                        "repair",
                        str(task_dir),
                        "--run-dir",
                        str(root / "run"),
                        "--provider",
                        "static",
                        "--patch-file",
                        str(patch_file),
                        "--credit-limit",
                        "80",
                    ],
                    backend=RepairingBackend(),
                )
            summary = json.loads(stdout.getvalue())
            self.assertEqual(return_code, 0)
            self.assertEqual(summary["status"], "DONE")
            self.assertEqual(summary["candidate_id"], "candidate_001")
            self.assertEqual(summary["manifest_ref"], "artifact_manifest.json")
            self.assertTrue((root / "run" / "experimental_report.md").is_file())
            self.assertTrue((root / "run" / "artifact_manifest.json").is_file())


class V3PrototypeCliTests(unittest.TestCase):
    @staticmethod
    def _done_result() -> dict[str, object]:
        return {
            "workflow": "V3_A1_VERTICAL_PROTOTYPE",
            "task_id": "cli_fixture",
            "status": "DONE",
            "stop_reason": "NO_IMPROVEMENT_LIMIT",
            "exploration_stop_reason": "NO_IMPROVEMENT_LIMIT",
            "rounds_completed": 1,
            "best_candidate_id": "candidate_001",
            "final_candidate_id": "candidate_001",
            "budget": {"credits_used": 30},
        }

    def test_scripted_mode_preserves_legacy_budget_and_call_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            patch_file = root / "candidate.diff"
            patch_file.write_text("fixture patch", encoding="utf-8")
            run_dir = root / "run"
            stdout = io.StringIO()
            backend = object()
            run = Mock(return_value=self._done_result())
            backend_class = Mock(return_value=backend)
            prototype_module = types.ModuleType("llm4hls_agent.v3_prototype")
            prototype_module.DeterministicPrototypeBackend = backend_class
            prototype_module.run_v3_prototype = run

            with patch.dict(
                sys.modules,
                {"llm4hls_agent.v3_prototype": prototype_module},
            ), redirect_stdout(stdout):
                return_code = v3_prototype_main(
                    [
                        "--task-dir",
                        str(task_dir),
                        "--patch-file",
                        str(patch_file),
                        "--run-dir",
                        str(run_dir),
                    ]
                )

        self.assertEqual(return_code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "DONE")
        task, called_run_dir, config, proposals = run.call_args.args
        self.assertEqual(task.id, "cli_fixture")
        self.assertEqual(called_run_dir, str(run_dir))
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].patch, "fixture patch")
        self.assertEqual(proposals[0].provider, "scripted-v3a0-prototype")
        self.assertEqual(config.budget.token_limit, 4096)
        self.assertEqual(config.budget.tool_limits["llm"], 1)
        self.assertEqual(config.budget.tool_limits["csim"], 3)
        self.assertIs(run.call_args.kwargs["backend"], backend)
        self.assertNotIn("planner", run.call_args.kwargs)
        self.assertNotIn("max_planner_rounds", run.call_args.kwargs)

    def test_live_openai_mode_builds_provider_adapter_and_planner_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            run_dir = root / "run"
            stdout = io.StringIO()
            provider = object()
            planner = object()
            backend = object()
            run = Mock(return_value=self._done_result())
            backend_class = Mock(return_value=backend)
            prototype_module = types.ModuleType("llm4hls_agent.v3_prototype")
            prototype_module.DeterministicPrototypeBackend = backend_class
            prototype_module.run_v3_prototype = run

            with patch.dict(
                "os.environ",
                {
                    "OPENAI_BASE_URL": "https://llm.example/v1",
                    "OPENAI_API_KEY": "secret-live-test-key",
                },
                clear=False,
            ), patch(
                "llm4hls_agent.openai_provider.OpenAICompatibleOptimizationProvider",
                return_value=provider,
            ) as provider_class, patch(
                "llm4hls_agent.v3_openai_planner.OpenAICompatibleV3PlannerAdapter",
                return_value=planner,
            ) as adapter_class, patch.dict(
                sys.modules,
                {"llm4hls_agent.v3_prototype": prototype_module},
            ), redirect_stdout(stdout):
                return_code = v3_prototype_main(
                    [
                        "--task-dir",
                        str(task_dir),
                        "--live-openai",
                        "--run-dir",
                        str(run_dir),
                        "--model",
                        "fixture-live-model",
                        "--token-budget",
                        "12000",
                        "--llm-max-output-tokens",
                        "512",
                        "--llm-timeout",
                        "15",
                        "--llm-temperature",
                        "0.2",
                        "--max-planner-rounds",
                        "3",
                    ]
                )

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "DONE")
        provider_config = provider_class.call_args.args[0]
        self.assertEqual(provider_config.base_url, "https://llm.example/v1")
        self.assertEqual(provider_config.api_key, "secret-live-test-key")
        self.assertEqual(provider_config.model, "fixture-live-model")
        self.assertEqual(provider_config.max_output_tokens, 512)
        self.assertEqual(provider_config.timeout_seconds, 15.0)
        self.assertEqual(provider_config.temperature, 0.2)
        adapter_class.assert_called_once_with(
            run_dir.resolve(),
            provider,
            final_reserve_credits=25,
            max_output_tokens=512,
        )
        task, called_run_dir, config = run.call_args.args
        self.assertEqual(task.id, "cli_fixture")
        self.assertEqual(called_run_dir, str(run_dir))
        self.assertEqual(config.budget.token_limit, 12000)
        self.assertEqual(config.budget.tool_limits["llm"], 3)
        self.assertEqual(config.budget.tool_limits["csim"], 5)
        self.assertIs(run.call_args.kwargs["backend"], backend)
        self.assertIs(run.call_args.kwargs["planner"], planner)
        self.assertEqual(run.call_args.kwargs["max_planner_rounds"], 3)

    def test_live_openai_mode_requires_endpoint_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            make_task(task_dir)
            stderr = io.StringIO()
            run = Mock()
            prototype_module = types.ModuleType("llm4hls_agent.v3_prototype")
            prototype_module.DeterministicPrototypeBackend = Mock()
            prototype_module.run_v3_prototype = run

            with patch.dict("os.environ", {}, clear=True), patch.dict(
                sys.modules,
                {"llm4hls_agent.v3_prototype": prototype_module},
            ), redirect_stderr(stderr):
                return_code = v3_prototype_main(
                    [
                        "--task-dir",
                        str(task_dir),
                        "--live-openai",
                        "--run-dir",
                        str(root / "run"),
                    ]
                )

        error = json.loads(stderr.getvalue())
        self.assertEqual(return_code, 3)
        self.assertEqual(error["error_type"], "ValueError")
        self.assertIn("OPENAI_BASE_URL", error["detail"])
        run.assert_not_called()

    def test_scripted_and_live_modes_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stderr(stderr):
            v3_prototype_main(
                [
                    "--task-dir",
                    "task",
                    "--patch-file",
                    "candidate.diff",
                    "--live-openai",
                    "--run-dir",
                    "run",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
