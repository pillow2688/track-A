from __future__ import annotations

import json
import stat
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.repair import (
    PatchLimits,
    PatchProposal,
    PatchValidationError,
    RepairProviderError,
    StaticPatchProvider,
    apply_unified_diff,
    diagnose_failure,
    deterministic_fallback_proposal,
    FailureDiagnostic,
    normalize_unified_diff_headers,
    relocate_unified_diff_hunks,
    run_v1,
)
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.workflow import RunConfig


def make_task(root: Path) -> None:
    root.mkdir()
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "repair_fixture"',
                'task_type = "generate"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                'budget = 80',
                'requires_cosim = true',
                '[target]',
                'part = "xcu55c-fsvh2892-2L-e"',
                'clock_ns = 5.0',
            ]
        ),
        encoding="utf-8",
    )
    (root / "description.md").write_text("repair fixture\n", encoding="utf-8")
    (root / "kernel.cpp").write_text(
        '#include "kernel.h"\nvoid kernel(int value, int *out) {\n    *out = value - 1;\n}\n',
        encoding="utf-8",
    )
    (root / "kernel.h").write_text("void kernel(int value, int *out);\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class RepairBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, kind: str, *, kernel_bytes: bytes, **_kwargs: object) -> BackendResult:
        self.calls.append(kind)
        fixed = b"value + 1" in kernel_bytes
        if not fixed:
            return BackendResult(False, "runtime_fail", 1, 0.1, ["kernel.cpp:3 mismatch"])
        if kind == "synth":
            return BackendResult(
                True,
                "pass",
                0,
                0.2,
                report={
                    "estimated_clock_period_ns": 4.25,
                    "latency": {"best": 1, "average": 1, "worst": 1},
                    "interval": {"min": 1, "max": 1},
                    "resources": {"LUT": 1},
                },
            )
        if kind == "cosim":
            return BackendResult(True, "pass", 0, 0.3, cosim={"status": "Pass"})
        return BackendResult(True, "pass", 0, 0.1)


class RepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_task(task_dir)
        self.task = load_public_task(task_dir)
        self.config = RunConfig(
            tool=ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=self.task.part,
                clock_ns=5.0,
                timeouts={"csim": 10.0, "synth": 20.0, "cosim": 30.0},
            ),
            budget=BudgetConfig(
                credit_limit=80,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": None, "synth": None, "cosim": None, "llm": 1},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
            minimum_frequency_mhz=100.0,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def patch(self, replacement: str = "value + 1") -> str:
        return (
            "--- a/kernel.cpp\n"
            "+++ b/kernel.cpp\n"
            "@@ -1,4 +1,4 @@\n"
            ' #include "kernel.h"\n'
            " void kernel(int value, int *out) {\n"
            f"-    *out = value - 1;\n+    *out = {replacement};\n"
            " }\n"
        )

    def provider(self, patch: str | None = None) -> StaticPatchProvider:
        return StaticPatchProvider(
            PatchProposal(
                patch=patch or self.patch(),
                provider="test-provider",
                model="test-model",
                revision="test-revision",
                input_tokens=12,
                output_tokens=8,
            )
        )

    def test_patch_validator_only_allows_kernel_and_applies_hunk(self) -> None:
        application = apply_unified_diff(
            self.task.kernel_bytes,
            self.patch(),
            kernel_name="kernel.cpp",
        )
        self.assertIn(b"value + 1", application.patched_bytes)
        self.assertEqual(application.additions, 1)
        self.assertEqual(application.deletions, 1)
        with self.assertRaises(PatchValidationError):
            apply_unified_diff(
                self.task.kernel_bytes,
                self.patch().replace("kernel.cpp", "kernel.h"),
                kernel_name="kernel.cpp",
            )
        with self.assertRaises(PatchValidationError):
            apply_unified_diff(
                self.task.kernel_bytes,
                "```diff\n" + self.patch() + "```\n",
                kernel_name="kernel.cpp",
            )

    def test_patch_normalizer_repairs_only_hunk_line_counts(self) -> None:
        malformed = self.patch().replace(
            "@@ -1,4 +1,4 @@", "@@ -1,99 +1,98 @@"
        )

        normalized = normalize_unified_diff_headers(malformed)
        application = apply_unified_diff(
            self.task.kernel_bytes,
            normalized,
            kernel_name="kernel.cpp",
        )

        self.assertIn("@@ -1,4 +1,4 @@", normalized)
        self.assertIn(b"value + 1", application.patched_bytes)
        self.assertEqual(
            [line for line in malformed.splitlines() if not line.startswith("@@")],
            [line for line in normalized.splitlines() if not line.startswith("@@")],
        )

    def test_patch_relocator_repairs_unique_off_by_one_hunk_start_only(self) -> None:
        misplaced = self.patch().replace("@@ -1,4 +1,4 @@", "@@ -2,4 +2,4 @@")

        relocated = relocate_unified_diff_hunks(
            self.task.kernel_bytes,
            misplaced,
            kernel_name="kernel.cpp",
        )
        application = apply_unified_diff(
            self.task.kernel_bytes,
            relocated,
            kernel_name="kernel.cpp",
        )

        self.assertIn("@@ -1,4 +1,4 @@", relocated)
        self.assertIn(b"value + 1", application.patched_bytes)
        self.assertEqual(
            [line for line in misplaced.splitlines() if not line.startswith("@@")],
            [line for line in relocated.splitlines() if not line.startswith("@@")],
        )

    def test_patch_relocator_accepts_unique_exact_match_at_large_offset(self) -> None:
        source = "".join(f"padding {index}\n" for index in range(32)) + "target\n"
        misplaced = (
            "--- kernel.cpp\n"
            "+++ kernel.cpp\n"
            "@@ -1,1 +1,1 @@\n"
            "-target\n"
            "+changed\n"
        )

        relocated = relocate_unified_diff_hunks(
            source,
            misplaced,
            kernel_name="kernel.cpp",
        )
        application = apply_unified_diff(
            source,
            relocated,
            kernel_name="kernel.cpp",
        )

        self.assertIn("@@ -33,1 +33,1 @@", relocated)
        self.assertTrue(application.patched_bytes.endswith(b"changed\n"))

    def test_patch_relocator_rejects_ambiguous_old_hunk(self) -> None:
        source = "same\nmiddle\nsame\n"
        misplaced = (
            "--- kernel.cpp\n"
            "+++ kernel.cpp\n"
            "@@ -2,1 +2,1 @@\n"
            "-same\n"
            "+changed\n"
        )

        with self.assertRaisesRegex(
            PatchValidationError, "not a unique source match"
        ):
            relocate_unified_diff_hunks(
                source,
                misplaced,
                kernel_name="kernel.cpp",
            )

    def test_patch_relocator_rejects_missing_old_hunk(self) -> None:
        missing = self.patch().replace("value - 1", "value - 2").replace(
            "@@ -1,4 +1,4 @@", "@@ -2,4 +2,4 @@"
        )

        with self.assertRaisesRegex(
            PatchValidationError, "not a unique source match"
        ):
            relocate_unified_diff_hunks(
                self.task.kernel_bytes,
                missing,
                kernel_name="kernel.cpp",
            )

    def test_patch_relocator_still_rejects_non_kernel_target(self) -> None:
        misplaced = self.patch().replace("kernel.cpp", "kernel.h").replace(
            "@@ -1,4 +1,4 @@", "@@ -2,4 +2,4 @@"
        )

        with self.assertRaisesRegex(
            PatchValidationError, "patch may modify only 'kernel.cpp'"
        ):
            relocate_unified_diff_hunks(
                self.task.kernel_bytes,
                misplaced,
                kernel_name="kernel.cpp",
            )

    def test_diagnosis_localizes_public_failure(self) -> None:
        run_root = self.root / "diagnostic-run"
        action = run_root / "actions" / "a"
        action.mkdir(parents=True)
        (action / "result.json").write_text(
            json.dumps({"evidence": ["kernel.cpp:3 mismatch"]}), encoding="utf-8"
        )
        result = {
            "validation": {
                "csim": {
                    "status": "FAIL",
                    "phase": "runtime_fail",
                    "result_ref": "actions/a/result.json",
                },
                "synth": {"status": "NOT_RUN"},
                "cosim": {"status": "NOT_RUN"},
            }
        }
        diagnostic = diagnose_failure(self.task, result, run_root)
        self.assertEqual(diagnostic.code, "FUNCTIONAL_MISMATCH")
        self.assertEqual(diagnostic.source_lines, (1, 2, 3, 4))
        self.assertTrue(diagnostic.repairable)

    def test_diagnosis_classifies_compile_synth_cosim_and_timeout(self) -> None:
        cases = [
            ("csim", "compile_error", "COMPILE_ERROR", True),
            ("synth", "synth_error", "SYNTHESIS_ERROR", True),
            ("cosim", "cosim_fail", "COSIM_FAILURE", True),
            ("cosim", "timeout", "TOOL_TIMEOUT", False),
        ]
        for index, (stage, phase, code, repairable) in enumerate(cases):
            with self.subTest(stage=stage, phase=phase):
                run_root = self.root / f"classification-{index}"
                action = run_root / "actions" / "a"
                action.mkdir(parents=True)
                (action / "result.json").write_text(
                    json.dumps({"evidence": [f"kernel.cpp:3 {phase}"]}),
                    encoding="utf-8",
                )
                validation = {
                    "csim": {"status": "PASS"},
                    "synth": {"status": "PASS"},
                    "cosim": {"status": "PASS"},
                }
                validation[stage] = {
                    "status": "TIMEOUT" if phase == "timeout" else "FAIL",
                    "phase": phase,
                    "result_ref": "actions/a/result.json",
                }
                diagnostic = diagnose_failure(
                    self.task, {"validation": validation}, run_root
                )
                self.assertEqual(diagnostic.code, code)
                self.assertEqual(diagnostic.repairable, repairable)

    def test_v1_repairs_in_isolated_candidate_and_promotes(self) -> None:
        run_dir = self.root / "v1-run"
        backend = RepairBackend()
        result = run_v1(
            self.task,
            run_dir,
            self.config,
            self.provider(),
            backend=backend,
        )
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["candidate_id"], "candidate_001")
        self.assertFalse(result["v1_acceptance"]["accepted"])
        self.assertIsNone(result["rollback"])
        self.assertEqual(backend.calls, ["csim", "csim", "synth", "cosim"])
        registry = json.loads((run_dir / "candidate_registry.json").read_text())
        self.assertEqual(registry["best_candidate_id"], "candidate_001")
        self.assertEqual(registry["final_candidate_id"], "candidate_001")
        candidate = registry["candidates"]["candidate_001"]
        self.assertEqual(candidate["parent_id"], "candidate_000")
        self.assertEqual(candidate["status"], "VERIFIED")
        self.assertTrue(candidate["immutable"])
        self.assertEqual(candidate["code_hash"], result["patch"]["patched_sha256"])
        self.assertEqual(
            candidate["patch_sha256"],
            sha256(result["patch"]["applied_patch"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(candidate["validation"]["cosim"]["status"], "PASS")
        source = run_dir / candidate["source_ref"]
        self.assertIn("value + 1", source.read_text())
        self.assertEqual(source.stat().st_mode & stat.S_IWUSR, 0)
        baseline = run_dir / "baseline" / "source" / "kernel.cpp"
        self.assertIn("value - 1", baseline.read_text())
        self.assertEqual(baseline.stat().st_mode & stat.S_IWUSR, 0)
        self.assertTrue((run_dir / "diagnostics/candidate_000.json").is_file())
        self.assertTrue((run_dir / "v1_result.json").is_file())
        self.assertEqual(result["budget"]["tokens_used"], 20)
        for stage in ("csim", "synth", "cosim"):
            result_ref = result["validation"][stage]["result_ref"]
            action_result = json.loads((run_dir / result_ref).read_text())
            self.assertEqual(action_result["candidate_id"], "candidate_001")
            self.assertEqual(action_result["code_hash"], candidate["code_hash"])

    def test_invalid_testbench_patch_is_rejected_before_candidate_allocation(self) -> None:
        run_dir = self.root / "invalid-patch"
        malicious = self.patch().replace("kernel.cpp", "kernel_tb.cpp")

        result = run_v1(
            self.task,
            run_dir,
            self.config,
            self.provider(malicious),
            backend=RepairBackend(),
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "PATCH_INVALID")
        self.assertIsNone(result["candidate_id"])
        self.assertFalse((run_dir / "candidates").exists())
        registry = json.loads((run_dir / "candidate_registry.json").read_text())
        self.assertEqual(set(registry["candidates"]), {"candidate_000"})
        self.assertIsNone(registry["best_candidate_id"])
        self.assertIsNone(registry["final_candidate_id"])

    def test_v0_fallback_proposal_is_minimal_for_vector_add(self) -> None:
        task = load_public_task(Path(__file__).parents[1] / "examples" / "u55c_repair_task")
        diagnostic = FailureDiagnostic(
            candidate_id="candidate_000", stage="csim", phase="runtime_fail",
            code="FUNCTIONAL_MISMATCH", summary="mismatch", evidence=(),
            source_excerpt="", source_lines=(), repairable=True,
        )
        proposal = deterministic_fallback_proposal(task, diagnostic)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertIn("+ b[i]", proposal.patch)
        self.assertEqual(proposal.patch.count("+        c[i]"), 1)

    def test_llm_based_acceptance_requires_openai_provider_and_tokens(self) -> None:
        provider = StaticPatchProvider(PatchProposal(
            patch=self.patch(), provider="openai-compatible", model="deepseek-v4-pro",
            input_tokens=30, output_tokens=10,
        ))
        result = run_v1(
            self.task, self.root / "llm-acceptance", self.config,
            provider, backend=RepairBackend(),
        )
        self.assertTrue(result["v1_acceptance"]["accepted"])

    def test_failed_candidate_is_rejected_and_rolled_back(self) -> None:
        run_dir = self.root / "reject-run"
        result = run_v1(
            self.task,
            run_dir,
            self.config,
            self.provider(self.patch("value * 1")),
            backend=RepairBackend(),
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["candidate_id"], "candidate_001")
        self.assertEqual(result["rollback"]["to"], "candidate_000")
        registry = json.loads((run_dir / "candidate_registry.json").read_text())
        self.assertIsNone(registry["best_candidate_id"])
        self.assertEqual(registry["active_candidate_id"], "candidate_000")
        self.assertEqual(registry["candidates"]["candidate_001"]["status"], "REJECTED")

    def test_repeated_v1_run_reuses_provider_and_tool_actions(self) -> None:
        run_dir = self.root / "repeat-run"
        backend = RepairBackend()
        first = run_v1(self.task, run_dir, self.config, self.provider(), backend=backend)
        ledger = (run_dir / "budget_ledger.jsonl").read_bytes()
        second = run_v1(self.task, run_dir, self.config, self.provider(), backend=backend)
        self.assertEqual(first["status"], "DONE")
        self.assertEqual(second["status"], "DONE")
        self.assertEqual(second["candidate_id"], "candidate_001")
        self.assertEqual((run_dir / "budget_ledger.jsonl").read_bytes(), ledger)
        self.assertEqual(backend.calls, ["csim", "csim", "synth", "cosim"])

    def test_repair_closure_is_denied_before_provider_when_budget_is_too_low(self) -> None:
        run_dir = self.root / "budget-denied"
        low_budget = replace(
            self.config,
            budget=BudgetConfig(
                credit_limit=20,
                costs={"csim": 1, "synth": 4, "cosim": 20, "llm": 0},
                tool_limits={"csim": None, "synth": None, "cosim": None, "llm": 1},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )
        result = run_v1(
            self.task, run_dir, low_budget, self.provider(), backend=RepairBackend()
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stop_reason"], "REPAIR_CLOSURE_BUDGET_DENIED")
        self.assertEqual(result["budget"]["tool_used"]["llm"], 0)
        self.assertFalse((run_dir / "llm_actions").exists())

    def test_failed_provider_usage_is_charged_and_no_fallback_runs_by_default(self) -> None:
        class FailingProvider:
            def fingerprint(self) -> str:
                return "failing-provider"

            def propose_patch(self, _context):
                raise RepairProviderError(
                    "invalid response", input_tokens=31, output_tokens=9,
                    duration_seconds=1.25, request_id="req-failed",
                )

        result = run_v1(
            self.task, self.root / "provider-failure", self.config,
            FailingProvider(), backend=RepairBackend(),
        )
        self.assertEqual(result["stop_reason"], "LLM_REPAIR_FAILED")
        self.assertEqual(result["budget"]["tokens_used"], 40)
        self.assertIsNone(result["candidate_id"])

    def test_tampered_cached_provider_result_is_rejected(self) -> None:
        run_dir = self.root / "tampered-provider"
        backend = RepairBackend()
        first = run_v1(self.task, run_dir, self.config, self.provider(), backend=backend)
        self.assertEqual(first["status"], "DONE")
        result_path = next((run_dir / "llm_actions").glob("*/result.json"))
        value = json.loads(result_path.read_text())
        value["patch"] = self.patch("value * 99")
        result_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        result_path.write_text(json.dumps(value), encoding="utf-8")

        second = run_v1(self.task, run_dir, self.config, self.provider(), backend=backend)
        self.assertEqual(second["status"], "FAILED")
        self.assertEqual(second["stop_reason"], "LLM_REPAIR_FAILED")
        self.assertIn("digest", second["provider_error"])
        registry = json.loads((run_dir / "candidate_registry.json").read_text())
        self.assertEqual(registry["best_candidate_id"], "candidate_001")


if __name__ == "__main__":
    unittest.main()
