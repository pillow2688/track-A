from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    from llm4hls_agent.cli import main
    from llm4hls_agent.tools import BackendResult
except ModuleNotFoundError:
    def main(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("V0 CLI is not implemented")

    def BackendResult(*_args: object, **_kwargs: object):  # type: ignore[misc]
        raise AssertionError("V0 CLI is not implemented")


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


if __name__ == "__main__":
    unittest.main()
