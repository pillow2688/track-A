from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from llm4hls_agent.artifacts import (
    build_artifact_manifest,
    verify_artifact_manifest,
)
from llm4hls_agent.budget import BudgetConfig
from llm4hls_agent.optimization import OptimizationConfig, run_v2
from llm4hls_agent.scoring import ScoringConfig
from llm4hls_agent.task import load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig
from llm4hls_agent.v2_team_report import (
    V2TeamReportError,
    analyze_v2_team_report_data,
    collect_v2_team_report_data,
    write_v2_team_report,
)
from llm4hls_agent.workflow import RunConfig
from tests.test_optimization import (
    BaselineCsimFailureBackend,
    InvalidPatchOptimizationProvider,
    PPASequenceBackend,
    RejectOnceThenSucceedProvider,
    SequenceOptimizationProvider,
    make_workflow_task,
)


class FinalClosureMetricDriftBackend(PPASequenceBackend):
    """Return distinct exploration/final Synth evidence for the selected Candidate."""

    def __init__(self) -> None:
        super().__init__()
        self.factor_two_synth_calls = 0

    def run(
        self, kind: str, *, kernel_bytes: bytes, **kwargs: object
    ) -> BackendResult:
        factor = self.factor(kernel_bytes)
        result = super().run(kind, kernel_bytes=kernel_bytes, **kwargs)
        if kind != "synth" or factor != 2:
            return result
        self.factor_two_synth_calls += 1
        if self.factor_two_synth_calls != 2:
            return result
        report = dict(result.report or {})
        report["latency"] = {"best": 90, "average": 90, "worst": 90}
        report["interval"] = {"min": 1, "max": 1}
        return replace(result, report=report)


class V2TeamReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task_dir = self.root / "task"
        make_workflow_task(task_dir)
        self.task = load_public_task(task_dir)
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
        self.optimization_config = OptimizationConfig(
            scoring=ScoringConfig(
                weights={
                    "latency": 0.45,
                    "ii": 0.35,
                    "lut": 0.06,
                    "ff": 0.04,
                    "dsp": 0.04,
                    "bram": 0.04,
                    "uram": 0.02,
                },
                max_resource_percent={
                    "lut": 100.0,
                    "ff": 100.0,
                    "dsp": 100.0,
                    "bram": 100.0,
                    "uram": 100.0,
                },
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _tree_digests(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _run(
        self,
        name: str,
        *,
        provider: object | None = None,
        backend: object | None = None,
        optimization_config: OptimizationConfig | None = None,
    ) -> Path:
        run_root = self.root / name
        run_v2(
            self.task,
            run_root,
            self.run_config,
            optimization_config or self.optimization_config,
            provider or SequenceOptimizationProvider(),
            backend=backend or PPASequenceBackend(),
        )
        return run_root

    def test_round_decisions_cosim_gate_and_accounting_are_reconstructed(self) -> None:
        run_root = self._run("full-sequence")

        data = analyze_v2_team_report_data(
            collect_v2_team_report_data(run_root, mode="offline")
        )

        self.assertEqual(
            [round_review.decision for round_review in data.rounds],
            [
                "PROMOTED",
                "PROMOTED",
                "REJECTED_VALIDATION",
                "REJECTED_NOT_BETTER",
            ],
        )
        self.assertEqual(
            [round_review.cosim_gate["state"] for round_review in data.rounds],
            [
                "EXECUTED_PASS",
                "EXECUTED_PASS",
                "NOT_REACHED_CSIM_FAILED",
                "SKIPPED_BY_GATE",
            ],
        )
        self.assertEqual(
            [round_review.best_after for round_review in data.rounds],
            ["candidate_001", "candidate_002", "candidate_002", "candidate_002"],
        )
        ledger = data.accounting["ledger"]
        self.assertEqual(ledger["input_tokens"], 410)
        self.assertEqual(ledger["output_tokens"], 80)
        self.assertEqual(ledger["cached_input_tokens"], 40)
        self.assertEqual(ledger["total_tokens"], 490)
        self.assertEqual(
            ledger["total_tokens"],
            ledger["input_tokens"] + ledger["output_tokens"],
        )
        self.assertLessEqual(ledger["cached_input_tokens"], ledger["input_tokens"])
        self.assertEqual(ledger["credits"], 106)
        self.assertEqual(data.accounting["baseline_credits"], 25)
        self.assertEqual(data.accounting["rounds_credits"], 56)
        self.assertEqual(data.accounting["final_credits"], 25)
        self.assertEqual(data.accounting["decomposed_credits"], 106)
        self.assertTrue(data.accounting["conserved"])
        self.assertEqual(data.accounting["mismatches"], ())
        self.assertEqual(data.cosim_summary["gate_skipped"], 1)
        self.assertEqual(data.cosim_summary["gate_saved_credits"], 20)
        skipped_cosim = data.rounds[-1].tools[-1]
        self.assertIn("未调用", skipped_cosim["invocation_reason"])
        self.assertIn("gate", skipped_cosim["invocation_reason"])
        self.assertEqual(
            data.rounds[-1].rejection_reason, "PPA_NOT_BETTER"
        )
        self.assertIsNone(data.rounds[0].rejection_reason)

    def test_provider_and_patch_rejections_remain_attempt_stubs(self) -> None:
        two_rounds = replace(self.optimization_config, max_rounds=2)
        provider_run = self._run(
            "provider-rejection",
            provider=RejectOnceThenSucceedProvider(),
            optimization_config=two_rounds,
        )
        patch_run = self._run(
            "patch-rejection",
            provider=InvalidPatchOptimizationProvider(),
            optimization_config=two_rounds,
        )

        provider_data = collect_v2_team_report_data(provider_run, mode="offline")
        patch_data = collect_v2_team_report_data(patch_run, mode="offline")

        self.assertIsNone(provider_data.rounds[0].candidate_id)
        self.assertEqual(provider_data.rounds[0].decision, "PROVIDER_REJECTED")
        self.assertEqual(
            provider_data.candidate_tree["attempts"],
            (
                {
                    "id": "attempt_round_001",
                    "parent": "candidate_000",
                    "round": 1,
                    "status": "PROVIDER_REJECTED",
                    "rejection_reason": (
                        "RepairProviderError: injected first-round provider rejection"
                    ),
                },
            ),
        )
        self.assertEqual(
            [round_review.decision for round_review in patch_data.rounds],
            ["PATCH_REJECTED", "PATCH_REJECTED"],
        )
        self.assertTrue(
            all(round_review.candidate_id is None for round_review in patch_data.rounds)
        )
        self.assertEqual(
            [attempt["status"] for attempt in patch_data.candidate_tree["attempts"]],
            ["PATCH_REJECTED", "PATCH_REJECTED"],
        )
        self.assertEqual(patch_data.candidate_tree["nodes"][0]["id"], "candidate_000")

    def test_automatic_report_is_sealed_by_the_final_manifest(self) -> None:
        run_root = self._run("automatic")

        manifest = verify_artifact_manifest(run_root)
        report_bytes = (run_root / "experimental_report.md").read_bytes()
        entry = next(
            artifact
            for artifact in manifest["artifacts"]
            if artifact["path"] == "experimental_report.md"
        )

        self.assertTrue(entry["required"])
        self.assertEqual(entry["artifact_type"], "experimental_report")
        self.assertEqual(entry["size_bytes"], len(report_bytes))
        self.assertEqual(entry["sha256"], hashlib.sha256(report_bytes).hexdigest())
        self.assertIn(b"GENERATED_PRE_MANIFEST", report_bytes)

    def test_offline_report_is_deterministic_and_does_not_modify_the_run(self) -> None:
        run_root = self._run("offline-source")
        before = self._tree_digests(run_root)
        manifest_before = (run_root / "artifact_manifest.json").read_bytes()
        automatic_report_before = (run_root / "experimental_report.md").read_bytes()
        first = self.root / "team-report-a.md"
        second = self.root / "team-report-b.md"

        returned_first = write_v2_team_report(
            run_root, mode="offline", output_path=first
        )
        returned_second = write_v2_team_report(
            run_root, mode="offline", output_path=second
        )

        self.assertEqual(returned_first, first.resolve())
        self.assertEqual(returned_second, second.resolve())
        self.assertEqual(first.read_bytes(), second.read_bytes())
        report = first.read_text(encoding="utf-8")
        self.assertIn("MANIFEST_VERIFIED", report)
        self.assertIn("Provider expected effect", report)
        self.assertIn("Synth worst-case latency", report)
        self.assertIn("CoSim max latency", report)
        self.assertIn("top-level transaction interval", report)
        self.assertIn("run 外部复盘产物", report)
        self.assertNotIn("| Round | LLM expected effect", report)
        self.assertEqual(self._tree_digests(run_root), before)
        self.assertEqual(
            (run_root / "artifact_manifest.json").read_bytes(), manifest_before
        )
        self.assertEqual(
            (run_root / "experimental_report.md").read_bytes(),
            automatic_report_before,
        )
        verify_artifact_manifest(run_root)

    def test_offline_report_rejects_output_inside_the_run(self) -> None:
        run_root = self._run("inside-output")
        destination = run_root / "offline-team-report.md"

        with self.assertRaisesRegex(
            V2TeamReportError, "offline report output must be outside run_dir"
        ):
            write_v2_team_report(
                run_root, mode="offline", output_path=destination
            )

        self.assertFalse(destination.exists())
        verify_artifact_manifest(run_root)

    def test_manifest_and_reference_corruption_fail_without_overwriting_output(
        self,
    ) -> None:
        source = self._run("corruption-source")

        manifest_corrupt = self.root / "manifest-corrupt"
        shutil.copytree(source, manifest_corrupt)
        task_spec = manifest_corrupt / "task_spec.json"
        task_spec.write_bytes(task_spec.read_bytes() + b" ")
        manifest_output = self.root / "manifest-corrupt-report.md"
        manifest_output.write_bytes(b"KEEP-MANIFEST-OUTPUT")
        with self.assertRaisesRegex(
            V2TeamReportError, "MANIFEST_VERIFICATION_FAILED"
        ):
            write_v2_team_report(
                manifest_corrupt,
                mode="offline",
                output_path=manifest_output,
            )
        self.assertEqual(manifest_output.read_bytes(), b"KEEP-MANIFEST-OUTPUT")

        reference_corrupt = self.root / "reference-corrupt"
        shutil.copytree(source, reference_corrupt)
        missing_ref = "llm_actions/missing/result.json"
        result_path = reference_corrupt / "v2_result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["rounds"][0]["provider_ref"] = missing_ref
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        round_path = reference_corrupt / "optimization_rounds/round_001.json"
        round_record = json.loads(round_path.read_text(encoding="utf-8"))
        round_record["provider_ref"] = missing_ref
        round_path.write_text(
            json.dumps(round_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(reference_corrupt)
        reference_output = self.root / "reference-corrupt-report.md"
        reference_output.write_bytes(b"KEEP-REFERENCE-OUTPUT")
        with self.assertRaisesRegex(
            V2TeamReportError, "MANIFEST_REF_CLOSURE_FAILED"
        ):
            write_v2_team_report(
                reference_corrupt,
                mode="offline",
                output_path=reference_output,
            )
        self.assertEqual(reference_output.read_bytes(), b"KEEP-REFERENCE-OUTPUT")

    def test_cross_object_rebinding_fails_even_after_manifest_is_rebuilt(self) -> None:
        source = self._run("binding-source")

        provider_corrupt = self.root / "provider-binding-corrupt"
        shutil.copytree(source, provider_corrupt)
        result_path = provider_corrupt / "v2_result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        request_ref = result["rounds"][0]["request_ref"]
        result["rounds"][0]["provider_ref"] = request_ref
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        round_path = provider_corrupt / "optimization_rounds/round_001.json"
        round_record = json.loads(round_path.read_text(encoding="utf-8"))
        round_record["provider_ref"] = request_ref
        round_path.write_text(
            json.dumps(round_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(provider_corrupt)
        with self.assertRaisesRegex(V2TeamReportError, "Ledger result_ref"):
            write_v2_team_report(
                provider_corrupt,
                mode="offline",
                output_path=self.root / "provider-binding-report.md",
            )

        score_corrupt = self.root / "score-binding-corrupt"
        shutil.copytree(source, score_corrupt)
        wrong_score_ref = "scores/candidate_000.json"
        result_path = score_corrupt / "v2_result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["rounds"][0]["score_ref"] = wrong_score_ref
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        round_path = score_corrupt / "optimization_rounds/round_001.json"
        round_record = json.loads(round_path.read_text(encoding="utf-8"))
        round_record["score_ref"] = wrong_score_ref
        round_path.write_text(
            json.dumps(round_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        registry_path = score_corrupt / "candidate_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["candidates"]["candidate_001"]["score_ref"] = wrong_score_ref
        registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(score_corrupt)
        with self.assertRaisesRegex(V2TeamReportError, "score_ref binds Candidate"):
            write_v2_team_report(
                score_corrupt,
                mode="offline",
                output_path=self.root / "score-binding-report.md",
            )

        tool_corrupt = self.root / "tool-binding-corrupt"
        shutil.copytree(source, tool_corrupt)
        registry_path = tool_corrupt / "candidate_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        first = registry["candidates"]["candidate_001"]["validation"]["csim"]
        second = registry["candidates"]["candidate_002"]["validation"]["csim"]
        first["action_id"] = second["action_id"]
        first["result_ref"] = second["result_ref"]
        registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(tool_corrupt)
        with self.assertRaisesRegex(V2TeamReportError, "action binds Candidate"):
            write_v2_team_report(
                tool_corrupt,
                mode="offline",
                output_path=self.root / "tool-binding-report.md",
            )

        metrics_corrupt = self.root / "metrics-binding-corrupt"
        shutil.copytree(source, metrics_corrupt)
        registry_path = metrics_corrupt / "candidate_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["candidates"]["candidate_001"]["metrics_ref"] = (
            registry["candidates"]["candidate_002"]["metrics_ref"]
        )
        registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(metrics_corrupt)
        with self.assertRaisesRegex(
            V2TeamReportError, "metrics_ref is not its Synth result_ref"
        ):
            write_v2_team_report(
                metrics_corrupt,
                mode="offline",
                output_path=self.root / "metrics-binding-report.md",
            )

        scope_corrupt = self.root / "validation-scope-corrupt"
        shutil.copytree(source, scope_corrupt)
        result_path = scope_corrupt / "v2_result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        registry = json.loads(
            (scope_corrupt / "candidate_registry.json").read_text(encoding="utf-8")
        )
        final_id = result["final_candidate_id"]
        exploration_synth = registry["candidates"][final_id]["validation"][
            "synth"
        ]
        result["final_attempts"][0]["validation"]["synth"] = exploration_synth
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(scope_corrupt)
        with self.assertRaisesRegex(V2TeamReportError, "validation_scope"):
            write_v2_team_report(
                scope_corrupt,
                mode="offline",
                output_path=self.root / "validation-scope-report.md",
            )

        status_corrupt = self.root / "validation-status-corrupt"
        shutil.copytree(source, status_corrupt)
        registry_path = status_corrupt / "candidate_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["candidates"]["candidate_003"]["validation"]["csim"][
            "status"
        ] = "PASS"
        registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(status_corrupt)
        with self.assertRaisesRegex(V2TeamReportError, "validation status=PASS"):
            write_v2_team_report(
                status_corrupt,
                mode="offline",
                output_path=self.root / "validation-status-report.md",
            )

    def test_final_headline_uses_independent_closure_synth_metrics(self) -> None:
        two_rounds = replace(self.optimization_config, max_rounds=2)
        run_root = self._run(
            "final-metric-drift",
            backend=FinalClosureMetricDriftBackend(),
            optimization_config=two_rounds,
        )

        data = collect_v2_team_report_data(run_root, mode="offline")

        selected = data.final_validation["selected_exploration_metrics"]
        closure = data.final_validation["metrics"]
        self.assertEqual(selected["latency"]["worst"], 100)
        self.assertEqual(closure["latency"]["worst"], 90)
        self.assertEqual(data.run_outcome["final_latency_worst"], 90.0)
        self.assertAlmostEqual(data.run_outcome["latency_speedup"], 1000 / 90)

    def test_report_scrubs_paths_bearer_tokens_and_url_queries(self) -> None:
        source = self._run("redaction-source")
        redacted_run = self.root / "redaction-mutated"
        shutil.copytree(source, redacted_run)
        request_path = next((redacted_run / "llm_actions").glob("*/request.json"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["context"]["audit_sensitive_string"] = (
            "/usr/local/private/config Bearer abcdefghijklmnop "
            "https://example.test/hook?token=super-secret&user=alice"
        )
        request_path.write_text(
            json.dumps(request, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_artifact_manifest(redacted_run)
        destination = self.root / "redacted-report.md"

        write_v2_team_report(
            redacted_run, mode="offline", output_path=destination
        )
        report = destination.read_text(encoding="utf-8")

        self.assertNotIn("/usr/local/private", report)
        self.assertNotIn("abcdefghijklmnop", report)
        self.assertNotIn("token=super-secret", report)
        self.assertIn("<ABSOLUTE_PATH>", report)
        self.assertIn("Bearer <REDACTED>", report)
        self.assertIn("?<REDACTED_QUERY>", report)

    def test_baseline_failure_still_produces_a_sealed_truthful_report(self) -> None:
        run_root = self._run(
            "baseline-failure", backend=BaselineCsimFailureBackend()
        )

        data = analyze_v2_team_report_data(
            collect_v2_team_report_data(run_root, mode="offline")
        )
        report = (run_root / "experimental_report.md").read_text(encoding="utf-8")
        manifest = verify_artifact_manifest(run_root)
        covered = {artifact["path"] for artifact in manifest["artifacts"]}

        self.assertEqual(data.run_outcome["status"], "FAILED")
        self.assertEqual(
            data.run_outcome["stop_reason"], "BASELINE_NOT_VERIFIED"
        )
        self.assertEqual(data.rounds, ())
        self.assertEqual(
            [tool["execution_state"] for tool in data.baseline["tools"]],
            [
                "EXECUTED_FAIL",
                "NOT_REACHED_CSIM_FAILED",
                "NOT_REACHED_CSIM_FAILED",
            ],
        )
        self.assertEqual(data.accounting["ledger"]["credits"], 1)
        self.assertEqual(data.accounting["decomposed_credits"], 1)
        self.assertTrue(data.accounting["conserved"])
        self.assertIn("BASELINE_NOT_VERIFIED", report)
        self.assertTrue(
            any("回到 V1" in str(item.get("action")) for item in data.next_actions)
        )
        self.assertFalse(
            any(
                str(item.get("action")).startswith("检查 final/fallback")
                for item in data.next_actions
            )
        )
        self.assertIn("GENERATED_PRE_MANIFEST", report)
        self.assertIn("v2_result.json", covered)
        self.assertIn("experimental_report.md", covered)


if __name__ == "__main__":
    unittest.main()
