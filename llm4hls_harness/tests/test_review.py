from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from llm4hls_agent.review import (
    DASHBOARD,
    REPORT_CN,
    REPORT_EN,
    _patch_display,
    build_review_evidence,
    render_html,
    render_markdown,
)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.languages: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag == "main" and values.get("data-lang"):
            self.languages.add(str(values["data-lang"]))
        if tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))


class ReviewRenderingTests(unittest.TestCase):
    def test_patch_display_is_complete_through_30_lines_and_truncates_31(self) -> None:
        thirty = "\n".join(f"line {index}" for index in range(30))
        thirty_one = "\n".join(f"line {index}" for index in range(31))

        complete = _patch_display(thirty)
        truncated = _patch_display(thirty_one)

        self.assertFalse(complete["truncated"])
        self.assertEqual(complete["display"], thirty)
        self.assertTrue(truncated["truncated"])
        self.assertEqual(len(str(truncated["display"]).splitlines()), 31)
        self.assertIn("showing 30 of 31 lines", str(truncated["display"]))


class RealReviewEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.harness_root = Path(__file__).resolve().parents[1]
        cls.runs_root = cls.harness_root / "runs"
        cls.run_dirs = {
            "compile_error": cls.runs_root / "v1-compile-final",
            "functional_mismatch": cls.runs_root / "v1-functional-final-2",
            "synthesis_error": cls.runs_root / "v1-synthesis-final-2",
            "patch_invalid": cls.runs_root / "v1-patch-invalid",
        }
        required = [
            cls.harness_root / "llm4hls_agent/config/v1_acceptance.json",
            cls.runs_root / "v1-acceptance/acceptance_result.json",
            *(path / "artifact_manifest.json" for path in cls.run_dirs.values()),
        ]
        if not all(path.is_file() for path in required):
            raise unittest.SkipTest("real V1 evidence is not available")
        cls.review = build_review_evidence(
            required[0], cls.run_dirs, required[1], cls.runs_root
        )

    def test_real_summary_is_computed_from_the_four_runs(self) -> None:
        summary = self.review["summary"]
        self.assertEqual(self.review["overall_status"], "PASS")
        self.assertTrue(self.review["acceptance_consistent"])
        self.assertEqual(
            summary,
            {
                "acceptance_cases": 4,
                "hls_repairs_passed": 3,
                "safety_rejections_passed": 1,
                "llm_calls": 3,
                "input_tokens": 2700,
                "output_tokens": 676,
                "cached_input_tokens": 384,
                "total_tokens": 3376,
                "csim_calls": 7,
                "synth_calls": 4,
                "cosim_calls": 3,
                "tool_calls": 14,
                "credits_used": 83,
                "credit_limit": 320,
                "credits_remaining": 237,
                "token_limit": 131072,
                "tokens_remaining": 127696,
                "final_csim_pass": 3,
                "final_synth_pass": 3,
                "final_cosim_pass": 3,
                "clock_pass": 3,
                "hls_case_count": 3,
            },
        )

    def test_every_case_has_direct_human_review_evidence(self) -> None:
        cases = {case["case_id"]: case for case in self.review["cases"]}
        self.assertEqual(set(cases), set(self.run_dirs))
        for case in cases.values():
            self.assertEqual(case["status"], "PASS")
            self.assertTrue(case["error"]["key_log"])
            self.assertTrue(case["error"]["file"])
            self.assertTrue(case["trace_summary"])
            self.assertTrue(case["raw_evidence_links"])
            self.assertTrue(all(check["status"] == "PASS" for check in case["checks"]))
            self.assertNotIn("/home/", str(case))
        self.assertEqual(cases["compile_error"]["error"]["symbol"], "rhs")
        self.assertEqual(
            cases["synthesis_error"]["error"]["subtype"],
            "unsupported_dynamic_allocation",
        )
        self.assertEqual(
            cases["patch_invalid"]["provider"]["real_llm_calls"], 0
        )
        self.assertEqual(cases["patch_invalid"]["patch"]["additions"], 1)
        self.assertEqual(cases["patch_invalid"]["patch"]["deletions"], 1)
        self.assertEqual(cases["patch_invalid"]["patch"]["hunks"], 1)

    def test_bilingual_markdown_and_html_share_the_same_evidence(self) -> None:
        english = render_markdown(self.review, "en")
        chinese = render_markdown(self.review, "zh")
        dashboard = render_html(self.review)
        for output in (english, chinese, dashboard):
            self.assertNotIn("/home/", output)
            self.assertNotIn("file://", output)
            self.assertIn("3376", output)
            self.assertIn("83", output)
            self.assertIn("compile_error", output)
            self.assertIn("patch_invalid", output)
            self.assertIn(self.review["review_data_digest"], output)
        self.assertIn("V1 Human Review Acceptance Report", english)
        self.assertIn("V1 人工审核统一验收报告", chinese)
        self.assertIn("data-lang=\"en\"", dashboard)
        self.assertIn("data-lang=\"zh\"", dashboard)
        self.assertNotRegex(dashboard, r"https?://")
        self.assertEqual(render_markdown(self.review, "en"), english)
        self.assertEqual(render_markdown(self.review, "zh"), chinese)
        self.assertEqual(render_html(self.review), dashboard)
        parser = LinkParser()
        parser.feed(dashboard)
        self.assertEqual(parser.languages, {"en", "zh"})
        for value in parser.links:
            if value.startswith("#"):
                continue
            self.assertFalse(Path(value).is_absolute())
            self.assertTrue((self.runs_root / value).is_file(), value)

    def test_all_raw_links_are_relative_and_exist(self) -> None:
        for case in self.review["cases"]:
            for value in case["raw_evidence_links"]:
                self.assertFalse(Path(value).is_absolute())
                self.assertNotIn("..", Path(value).parts)
                self.assertTrue((self.runs_root / value).is_file(), value)
        english = render_markdown(self.review, "en")
        links = re.findall(r"\[[^]]+\]\(([^)]+)\)", english)
        for value in links:
            if value.startswith("#"):
                continue
            self.assertFalse(Path(value).is_absolute())
            if value not in {REPORT_EN, REPORT_CN, DASHBOARD}:
                self.assertTrue((self.runs_root / value).is_file(), value)


if __name__ == "__main__":
    unittest.main()
