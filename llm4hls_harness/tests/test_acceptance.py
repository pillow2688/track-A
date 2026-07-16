from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.acceptance import evaluate_acceptance
from llm4hls_agent.artifacts import build_artifact_manifest
from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.repair import PatchProposal, StaticPatchProvider, run_v1
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.workflow import RunConfig


def make_task(root: Path) -> None:
    root.mkdir()
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "acceptance_fixture"',
                'task_type = "generate"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                'budget = 80',
                'requires_cosim = true',
                '[target]',
                'part = "xcu55c-fsvh2892-2L-e"',
                'clock_ns = 10.0',
            ]
        ),
        encoding="utf-8",
    )
    (root / "description.md").write_text("fixture\n", encoding="utf-8")
    (root / "kernel.cpp").write_text(
        '#include "kernel.h"\nvoid kernel(int x, int *out) { *out = x - 1; }\n',
        encoding="utf-8",
    )
    (root / "kernel.h").write_text(
        "void kernel(int x, int *out);\n", encoding="utf-8"
    )
    (root / "kernel_tb.cpp").write_text(
        "int main() { return 0; }\n", encoding="utf-8"
    )


class AcceptanceBackend:
    def fingerprint(self) -> str:
        return "tests.AcceptanceBackend:v1"

    def run(self, kind: str, *, kernel_bytes: bytes, **_kwargs: object) -> BackendResult:
        if b"x - 1" in kernel_bytes:
            return BackendResult(
                False, "runtime_fail", 1, 0.1, ["kernel.cpp:2 mismatch"]
            )
        if kind == "synth":
            return BackendResult(
                True,
                "pass",
                0,
                0.1,
                report={
                    "estimated_clock_period_ns": 2.0,
                    "latency": {"best": 1, "average": 1, "worst": 1},
                    "interval": {"min": 1, "max": 1},
                    "resources": {},
                },
            )
        if kind == "cosim":
            return BackendResult(True, "pass", 0, 0.1, cosim={"status": "Pass"})
        return BackendResult(True, "pass", 0, 0.1)


class AcceptanceEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_task(task_dir)
        task = load_public_task(task_dir)
        config = RunConfig(
            tool=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=task.part,
                clock_ns=10.0,
                timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
            ),
            budget=BudgetConfig(
                credit_limit=80,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": None, "synth": None, "cosim": None, "llm": 1},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )
        patch = (
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,2 +1,2 @@\n"
            ' #include "kernel.h"\n'
            "-void kernel(int x, int *out) { *out = x - 1; }\n"
            "+void kernel(int x, int *out) { *out = x + 1; }\n"
        )
        provider = StaticPatchProvider(
            PatchProposal(
                patch=patch,
                provider="openai-compatible",
                model="deepseek-v4-pro",
                input_tokens=20,
                output_tokens=10,
            )
        )
        self.run_dir = self.root / "functional-run"
        self.task = task
        self.config = config
        result = run_v1(
            task, self.run_dir, config, provider, backend=AcceptanceBackend()
        )
        self.assertEqual(result["status"], "DONE")
        (self.run_dir / "experimental_report.md").write_text(
            "# unit evidence only\n", encoding="utf-8"
        )
        build_artifact_manifest(self.run_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_spec(self, *, tier: str, backend: str) -> Path:
        path = self.root / f"spec-{tier}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acceptance_id": "unit-v1",
                    "evidence_tier": tier,
                    "required_provider": "openai-compatible",
                    "required_model": "deepseek-v4-pro",
                    "required_toolchain": "Vitis 2025.2",
                    "required_backend_fingerprint": backend,
                    "cases": [
                        {
                            "case_id": "functional_mismatch",
                            "kind": "hls",
                            "error_class": "FUNCTIONAL_MISMATCH",
                            "task_id": "acceptance_fixture",
                        }
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_test_evidence_is_deterministic_but_never_reports_real_pass(self) -> None:
        spec = self.write_spec(tier="TEST", backend="tests.AcceptanceBackend:v1")
        output = self.root / "acceptance"
        first = evaluate_acceptance(
            spec,
            {"functional_mismatch": self.run_dir},
            output,
        )
        first_bytes = (output / "acceptance_result.json").read_bytes()
        first_report = (output / "acceptance_report.md").read_bytes()
        first_cn_report = (output / "acceptance_report_CN.md").read_bytes()
        second = evaluate_acceptance(
            spec,
            {"functional_mismatch": self.run_dir},
            output,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["overall_status"], "TEST_PASS")
        self.assertEqual(
            (output / "acceptance_result.json").read_bytes(), first_bytes
        )
        self.assertEqual((output / "acceptance_report.md").read_bytes(), first_report)
        self.assertEqual(
            (output / "acceptance_report_CN.md").read_bytes(), first_cn_report
        )
        report = first_report.decode("utf-8")
        cn_report = first_cn_report.decode("utf-8")
        self.assertIn("Acceptance matrix", report)
        self.assertIn("experimental_report.md", report)
        self.assertIn("artifact_manifest.json", report)
        self.assertIn("python3 \\", report)
        self.assertNotIn("\n+  ", report)
        self.assertIn("V1 统一验收报告", cn_report)
        self.assertIn("验收矩阵", cn_report)
        self.assertIn("实验报告", cn_report)
        self.assertIn("产物清单", cn_report)
        self.assertIn("python3 \\", cn_report)
        self.assertNotIn("\n+  ", cn_report)

    def test_real_acceptance_rejects_non_vitis_backend(self) -> None:
        spec = self.write_spec(
            tier="REAL", backend="llm4hls_agent.vitis.VitisBackend:v0.4"
        )
        result = evaluate_acceptance(
            spec,
            {"functional_mismatch": self.run_dir},
            self.root / "real-acceptance",
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertIn("BACKEND_NOT_ALLOWED", result["cases"][0]["reason_codes"])

    def test_tampered_manifest_evidence_fails_closed(self) -> None:
        spec = self.write_spec(tier="TEST", backend="tests.AcceptanceBackend:v1")
        (self.run_dir / "trace.jsonl").write_text("tampered\n", encoding="utf-8")
        result = evaluate_acceptance(
            spec,
            {"functional_mismatch": self.run_dir},
            self.root / "tampered-acceptance",
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertIn("MANIFEST_INVALID", result["cases"][0]["reason_codes"])

    def test_patch_invalid_is_a_separate_safety_pass_without_candidate(self) -> None:
        malicious = (
            "--- a/kernel_tb.cpp\n"
            "+++ b/kernel_tb.cpp\n"
            "@@ -1,1 +1,1 @@\n"
            "-int main() { return 0; }\n"
            "+int main() { return 1; }\n"
        )
        run_dir = self.root / "patch-invalid"
        result = run_v1(
            self.task,
            run_dir,
            self.config,
            StaticPatchProvider(
                PatchProposal(
                    patch=malicious,
                    provider="static-patch-file",
                    model="offline",
                )
            ),
            backend=AcceptanceBackend(),
        )
        self.assertEqual(result["stop_reason"], "PATCH_INVALID")
        (run_dir / "experimental_report.md").write_text(
            "# safety evidence\n", encoding="utf-8"
        )
        build_artifact_manifest(run_dir)
        spec = self.root / "safety-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acceptance_id": "unit-safety",
                    "evidence_tier": "TEST",
                    "required_provider": "openai-compatible",
                    "required_model": "deepseek-v4-pro",
                    "required_toolchain": "Vitis 2025.2",
                    "required_backend_fingerprint": "tests.AcceptanceBackend:v1",
                    "cases": [
                        {
                            "case_id": "patch_invalid",
                            "kind": "safety",
                            "error_class": "PATCH_INVALID",
                            "task_id": "acceptance_fixture",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        accepted = evaluate_acceptance(
            spec, {"patch_invalid": run_dir}, self.root / "safety-acceptance"
        )

        self.assertEqual(accepted["overall_status"], "TEST_PASS")
        self.assertEqual(accepted["cases"][0]["status"], "PASS")
        self.assertFalse((run_dir / "candidates").exists())


if __name__ == "__main__":
    unittest.main()
