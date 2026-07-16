from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from llm4hls_agent.v2_acceptance import evaluate_v2_acceptance
from llm4hls_agent.v2_review import V2ReviewError, generate_v2_review_reports


def file_state(roots: list[Path]) -> dict[str, tuple[str, int]]:
    state: dict[str, tuple[str, int]] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                state[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    return state


class V2ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_v2_acceptance import V2AcceptanceTests

        self.fixture = V2AcceptanceTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.acceptance_dir = self.root / "acceptance"
        evaluate_v2_acceptance(
            self.fixture.spec,
            self.fixture.optimization_run,
            self.fixture.rejection_run,
            self.acceptance_dir,
        )
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_flat_bilingual_reports_are_complete_and_do_not_mutate_evidence(self) -> None:
        evidence_roots = [
            self.fixture.optimization_run,
            self.fixture.rejection_run,
            self.acceptance_dir,
        ]
        before = file_state(evidence_roots)

        summary = generate_v2_review_reports(
            self.fixture.spec,
            self.fixture.optimization_run,
            self.fixture.rejection_run,
            self.acceptance_dir / "acceptance_result.json",
            self.runs_root,
        )

        self.assertEqual(file_state(evidence_roots), before)
        self.assertEqual(summary["status"], "TEST_PASS")
        english_path = self.runs_root / "V2_ACCEPTANCE_REPORT.md"
        chinese_path = self.runs_root / "V2_ACCEPTANCE_REPORT_CN.md"
        self.assertTrue(english_path.is_file())
        self.assertTrue(chinese_path.is_file())
        english = english_path.read_text(encoding="utf-8")
        chinese = chinese_path.read_text(encoding="utf-8")
        for expected in (
            "Overall status",
            "Core statistics",
            "Candidate tree",
            "Baseline PPA",
            "LOOP_PIPELINE",
            "deepseek-v4-pro",
            "Full unified diff",
            "Final Vitis validation",
            "Safety rejection",
            "Ledger / Trace / action consistency",
            "Raw evidence index",
        ):
            self.assertIn(expected, english)
        for expected in (
            "总体状态",
            "核心统计",
            "候选树",
            "基线 PPA",
            "最终 Vitis 验证",
            "安全拒绝",
            "原始证据索引",
        ):
            self.assertIn(expected, chinese)
        self.assertNotIn("<html", english.casefold())
        self.assertNotIn("<html", chinese.casefold())

    def test_review_rejects_acceptance_result_that_disagrees_with_recompute(self) -> None:
        result_path = self.acceptance_dir / "acceptance_result.json"
        result_path.write_text('{"overall_status":"PASS"}\n', encoding="utf-8")

        with self.assertRaises(V2ReviewError):
            generate_v2_review_reports(
                self.fixture.spec,
                self.fixture.optimization_run,
                self.fixture.rejection_run,
                result_path,
                self.runs_root,
            )


if __name__ == "__main__":
    unittest.main()
