from __future__ import annotations

import hashlib
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
                    "required_official_acceleration_cap": 8.0,
                    "required_official_score_enabled": True,
                    "required_official_score_version": "public_proxy_v1",
                    "required_exploration_cosim_policy": "official_score_gate",
                    "required_task_difficulty": self.task.difficulty,
                    "required_task_requires_cosim": self.task.requires_cosim,
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
        self.assertTrue(result["checks"]["official_scoring_policy_bound"])
        self.assertTrue(result["checks"]["official_score_metadata_bound"])
        self.assertTrue(result["checks"]["scores_recomputed"])
        self.assertTrue(result["checks"]["exploration_cosim_gated"])
        self.assertTrue(result["checks"]["two_real_llm_candidates"])
        self.assertTrue(result["checks"]["best_matches_recomputed_comparator"])
        self.assertTrue(result["checks"]["rejected_candidate_did_not_pollute_best"])
        self.assertTrue(result["checks"]["ledger_trace_actions_consistent"])

        optimization_config = json.loads(
            (self.optimization_run / "optimization_config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(optimization_config["scoring"]["official_score"]["enabled"])
        self.assertEqual(
            optimization_config["exploration_cosim_policy"],
            "official_score_gate",
        )
        candidate_score = json.loads(
            (self.optimization_run / "scores/candidate_001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIsInstance(candidate_score["official_score"], float)
        self.assertEqual(
            candidate_score["official_score_source"],
            "PUBLIC_VALIDATION_PROXY_V1",
        )

    def test_rebuilt_manifest_cannot_hide_official_metadata_tamper(self) -> None:
        task_spec_path = self.optimization_run / "task_spec.json"
        task_spec = json.loads(task_spec_path.read_text(encoding="utf-8"))
        task_spec["difficulty"] = self.task.difficulty + 1
        task_spec_path.write_text(json.dumps(task_spec, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertFalse(result["checks"]["official_score_metadata_bound"])
        self.assertIn("OFFICIAL_SCORE_METADATA_INVALID", result["reason_codes"])

    def test_rebuilt_manifest_cannot_hide_official_score_source_tamper(self) -> None:
        score_path = self.optimization_run / "scores/candidate_002.json"
        score = json.loads(score_path.read_text(encoding="utf-8"))
        score["official_score_source"] = "FORGED_PROXY"
        score_path.write_text(json.dumps(score, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertFalse(result["checks"]["scores_recomputed"])
        self.assertIn("SCORE_RECOMPUTE_MISMATCH", result["reason_codes"])

    def test_rebuilt_manifest_cannot_change_official_formula_version(self) -> None:
        config_path = self.optimization_run / "optimization_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["scoring"]["official_score"]["version"] = "public_proxy_v2"
        config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertIn("EVIDENCE_INVALID", result["reason_codes"])

    def test_acceptance_rejects_run_that_disables_official_scoring(self) -> None:
        config_path = self.optimization_run / "optimization_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["scoring"]["official_score"]["enabled"] = False
        config["exploration_cosim_policy"] = "ppa_gate"
        config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertFalse(result["checks"]["official_scoring_policy_bound"])
        self.assertIn("OFFICIAL_SCORING_POLICY_INVALID", result["reason_codes"])

    def test_equivalent_bare_kernel_patch_headers_remain_bound(self) -> None:
        registry_path = self.optimization_run / "candidate_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for candidate_id, candidate in registry["candidates"].items():
            if candidate_id == "candidate_000":
                continue
            patch_path = self.optimization_run / candidate["patch_ref"]
            patch_path.chmod(0o644)
            patch = patch_path.read_text(encoding="utf-8")
            patch = patch.replace("--- a/kernel.cpp", "--- kernel.cpp", 1)
            patch = patch.replace("+++ b/kernel.cpp", "+++ kernel.cpp", 1)
            patch_path.write_text(patch, encoding="utf-8")
            patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
            candidate["patch_sha256"] = patch_sha256
            metadata_path = (
                self.optimization_run
                / "candidates"
                / candidate_id
                / "candidate.json"
            )
            metadata_path.chmod(0o644)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["patch_sha256"] = patch_sha256
            metadata_path.write_text(
                json.dumps(metadata, sort_keys=True), encoding="utf-8"
            )
        registry_path.write_text(
            json.dumps(registry, sort_keys=True), encoding="utf-8"
        )
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "TEST_PASS")
        self.assertTrue(
            result["checks"]["public_inputs_and_kernel_only_patches_bound"]
        )

    def test_rebuilt_manifest_cannot_hide_tampered_cosim_gate(self) -> None:
        gate_path = self.optimization_run / "cosim_gates/candidate_001.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["eligible"] = False
        gate_path.write_text(json.dumps(gate, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertFalse(result["checks"]["exploration_cosim_gated"])
        self.assertIn(
            "EXPLORATION_COSIM_GATE_INVALID", result["reason_codes"]
        )

    def test_rebuilt_manifest_cannot_hide_patch_source_lineage_mismatch(self) -> None:
        registry_path = self.optimization_run / "candidate_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        candidate = registry["candidates"]["candidate_001"]
        patch_path = self.optimization_run / candidate["patch_ref"]
        patch_path.chmod(0o644)
        patch = patch_path.read_text(encoding="utf-8").replace(
            "+    int factor = 1;", "+    int factor = 7;", 1
        )
        patch_path.write_text(patch, encoding="utf-8")
        patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        candidate["patch_sha256"] = patch_sha256
        metadata_path = (
            self.optimization_run
            / "candidates"
            / "candidate_001"
            / "candidate.json"
        )
        metadata_path.chmod(0o644)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["patch_sha256"] = patch_sha256
        metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        registry_path.write_text(json.dumps(registry, sort_keys=True), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertFalse(result["checks"]["candidate_tree_valid"])
        self.assertIn("CANDIDATE_TREE_INVALID", result["reason_codes"])

    def test_provider_patch_and_class_must_bind_to_candidate(self) -> None:
        registry = json.loads(
            (self.optimization_run / "candidate_registry.json").read_text(
                encoding="utf-8"
            )
        )
        changed = 0
        for candidate_id, candidate in registry["candidates"].items():
            if candidate_id == "candidate_000" or changed >= 3:
                continue
            action_path = self.optimization_run / candidate["llm_ref"]
            action = json.loads(action_path.read_text(encoding="utf-8"))
            action["change_class"] = "LOOP_RESTRUCTURE"
            action_path.write_text(json.dumps(action, sort_keys=True), encoding="utf-8")
            changed += 1
        self.assertEqual(changed, 3)
        build_artifact_manifest(self.optimization_run)

        result = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(result["overall_status"], "FAIL")
        self.assertFalse(result["checks"]["two_real_llm_candidates"])
        self.assertIn("INSUFFICIENT_REAL_LLM_CANDIDATES", result["reason_codes"])

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

    def test_rebuilt_manifest_cannot_hide_forbidden_provider_context(self) -> None:
        registry = json.loads(
            (self.optimization_run / "candidate_registry.json").read_text(
                encoding="utf-8"
            )
        )
        for candidate_id, candidate in registry["candidates"].items():
            if candidate_id == "candidate_000":
                continue
            request_ref = candidate.get("llm_request_ref")
            if not request_ref:
                continue
            request_path = self.optimization_run / request_ref
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["sent_files"] = ["kernel_tb.cpp"]
            request_path.write_text(json.dumps(request), encoding="utf-8")
        build_artifact_manifest(self.optimization_run)

        accepted = compute_v2_acceptance(
            self.spec, self.optimization_run, self.rejection_run
        )

        self.assertEqual(accepted["overall_status"], "FAIL")
        self.assertIn(
            "INSUFFICIENT_REAL_LLM_CANDIDATES", accepted["reason_codes"]
        )

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
