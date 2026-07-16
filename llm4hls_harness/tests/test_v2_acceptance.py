from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.artifacts import build_artifact_manifest
from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.optimization import run_v2, run_v2_rejection
from llm4hls_agent.optimization import OptimizationConfig
from llm4hls_agent.scoring import load_scoring_config
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.v2_acceptance import (
    compute_v2_acceptance,
    evaluate_v2_acceptance,
)
from llm4hls_agent.workflow import RunConfig
from tests.test_optimization import (
    PPASequenceBackend,
    SequenceOptimizationProvider,
    make_workflow_task,
)


class RegressionPPASequenceBackend(PPASequenceBackend):
    def fingerprint(self) -> str:
        return "tests.test_optimization.PPASequenceBackend"

    def run(self, kind: str, *, kernel_bytes: bytes, **kwargs: object) -> BackendResult:
        if kind == "csim" and b"int factor = 9;" in kernel_bytes:
            return BackendResult(
                False, "runtime_fail", 1, 0.1, ["semantic regression"]
            )
        return super().run(kind, kernel_bytes=kernel_bytes, **kwargs)


class V2AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_workflow_task(task_dir)
        self.task = load_public_task(task_dir)
        self.optimization_config = OptimizationConfig(
            scoring=load_scoring_config(
                Path(__file__).parents[1]
                / "llm4hls_agent"
                / "config"
                / "v2_scoring.yaml"
            )
        )
        self.run_config = RunConfig(
            tool=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=self.task.part,
                clock_ns=10.0,
                timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
            ),
            budget=BudgetConfig(
                credit_limit=160,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 6, "synth": 6, "cosim": 6, "llm": 6},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
            minimum_frequency_mhz=100.0,
        )
        self.optimization_run = self.root / "optimization"
        result = run_v2(
            self.task,
            self.optimization_run,
            self.run_config,
            self.optimization_config,
            SequenceOptimizationProvider(),
            backend=PPASequenceBackend(),
        )
        self.assertEqual(result["status"], "DONE")

        patch_path = self.root / "regression.diff"
        patch_path.write_text(
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,5 +1,5 @@\n"
            ' #include "kernel.h"\n'
            " void kernel(int *out) {\n"
            "-    int factor = 0;\n"
            "+    int factor = 9;\n"
            "     *out = factor;\n"
            " }\n",
            encoding="utf-8",
        )
        rejection_config = RunConfig(
            tool=self.run_config.tool,
            budget=BudgetConfig(
                credit_limit=160,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": 2, "synth": 1, "cosim": 1, "llm": 0},
                token_limit=0,
                runtime_limit_seconds=3600.0,
            ),
            minimum_frequency_mhz=100.0,
        )
        self.rejection_run = self.root / "rejection"
        rejected = run_v2_rejection(
            self.task,
            self.rejection_run,
            rejection_config,
            patch_path,
            backend=RegressionPPASequenceBackend(),
        )
        self.assertEqual(rejected["status"], "DONE")

        self.spec = self.root / "v2_acceptance.json"
        self.spec.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acceptance_id": "unit-v2",
                    "evidence_tier": "TEST",
                    "task_id": self.task.id,
                    "required_provider": "openai-compatible",
                    "required_model": "deepseek-v4-pro",
                    "required_toolchain": "Vitis 2025.2",
                    "required_backend_fingerprint": (
                        "tests.test_optimization.PPASequenceBackend"
                    ),
                    "minimum_real_llm_candidates": 2,
                    "require_no_fallback": True,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_acceptance_recomputes_tree_score_and_accounting(self) -> None:
        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "TEST_PASS")
        self.assertTrue(result["checks"]["candidate_tree_valid"])
        self.assertTrue(result["checks"]["two_real_llm_candidates"])
        self.assertTrue(result["checks"]["best_matches_recomputed_comparator"])
        self.assertTrue(result["checks"]["rejected_candidate_did_not_pollute_best"])
        self.assertTrue(result["checks"]["ledger_trace_actions_consistent"])

    def test_tampered_score_fails_closed_at_manifest(self) -> None:
        score_path = self.optimization_run / "scores" / "candidate_002.json"
        score = json.loads(score_path.read_text(encoding="utf-8"))
        score["ppa_cost"] = 0.0001
        score_path.write_text(json.dumps(score), encoding="utf-8")

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertIn("MANIFEST_INVALID", result["reason_codes"])

    def test_rebuilt_manifest_cannot_hide_wrong_final_action_binding(self) -> None:
        result_path = self.optimization_run / "v2_result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        registry = json.loads(
            (self.optimization_run / "candidate_registry.json").read_text(
                encoding="utf-8"
            )
        )
        exploration_ref = registry["candidates"]["candidate_001"]["validation"][
            "csim"
        ]["result_ref"]
        result["final_validation"]["csim"]["result_ref"] = exploration_ref
        result_path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        accepted = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(accepted["overall_status"], "FAIL")
        self.assertIn("FINAL_VALIDATION_INVALID", accepted["reason_codes"])

    def test_writer_emits_deterministic_machine_and_bilingual_reports(self) -> None:
        output = self.root / "acceptance"

        first = evaluate_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run, output
        )
        first_bytes = {
            name: (output / name).read_bytes()
            for name in (
                "acceptance_result.json",
                "acceptance_report.md",
                "acceptance_report_CN.md",
            )
        }
        second = evaluate_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run, output
        )

        self.assertEqual(first, second)
        for name, value in first_bytes.items():
            self.assertEqual((output / name).read_bytes(), value)
        self.assertIn("V2 Machine Acceptance", first_bytes["acceptance_report.md"].decode())
        self.assertIn("V2 机器验收报告", first_bytes["acceptance_report_CN.md"].decode())


if __name__ == "__main__":
    unittest.main()
