"""Regression tests for skipped-finding visibility (Fix 1).

LLM findings that get dropped during chunk-level matching (Layer A) or
chunk→P resolution (Layer B) must be counted, returned with reasons, and
written to *_skipped.json — never silently discarded.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.max_pipeline import _resolve_findings_to_p, phase1_json_proofread, run_max


class LayerASkipVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase1_dropped_findings_are_counted_and_returned(self):
        chunk = {"target": "第一句话完全正常的内容需要修改。第二句也是正文。",
                 "context": ""}
        # 1 条有效修正 + 1 条原句=改句 + 1 条匹配率过低（26 < 60）
        fake_json = json.dumps({"findings": [
            {"original_sentence": "需要修改",
             "corrected_sentence": "需要修正"},
            {"original_sentence": "需要修改",
             "corrected_sentence": "需要修改"},
            {"original_sentence": "完全不相关的别处文字zzz",
             "corrected_sentence": "改成别的"},
        ]}, ensure_ascii=False)

        async def fake_deepseek(*_args, **_kwargs):
            return fake_json

        with patch("src.proofreader.deepseek_async", new=fake_deepseek):
            result = await phase1_json_proofread(
                [chunk], concurrent=1, rpm=100000,
                system_prompt="test prompt")

        self.assertEqual(result["stats"]["findings_from_llm"], 3)
        self.assertEqual(result["stats"]["dropped_match"], 2)
        self.assertEqual(len(result["findings"]), 1)   # 只有有效那条
        self.assertEqual(result["findings"][0]["suggestion"], "需要修正")
        self.assertEqual(len(result["skipped"]), 2)

        reasons = {s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons, {"no_op_change", "match_below_threshold"})
        no_op = next(s for s in result["skipped"]
                     if s["reason"] == "no_op_change")
        self.assertEqual(no_op["original"], "需要修改")


class LayerBResolveSkipTests(unittest.TestCase):
    def setUp(self):
        self.text_map = {
            0: "第一段正常文字。",
            1: "第二段包含重复锚点。",
            2: "第二段包含重复锚点。",   # 跨段重复
            3: "第三段内容。",
        }

    def test_duplicate_anchor_and_not_found_are_logged(self):
        findings = [
            {"phase": "1_llm", "original": "重复锚点", "current": "重复锚点",
             "suggestion": "改"},
            {"phase": "1_llm", "original": "不存在xyz", "current": "不存在xyz",
             "suggestion": "改"},
        ]
        skip_log = []
        resolved = _resolve_findings_to_p(
            findings, self.text_map, skip_log=skip_log)
        self.assertEqual(len(resolved), 0)
        reasons = {s["reason"] for s in skip_log}
        self.assertIn("duplicate_anchor", reasons)
        self.assertIn("not_found", reasons)

    def test_explicit_p_out_of_range_and_missing_key(self):
        findings = [
            {"phase": "1_llm", "pn": 99, "original": "重复锚点",
             "current": "重复锚点", "suggestion": "改"},
            {"phase": "tgscc", "char": "", "current": ""},
        ]
        skip_log = []
        _resolve_findings_to_p(findings, self.text_map, skip_log=skip_log)
        reasons = {s["reason"] for s in skip_log}
        self.assertIn("p_out_of_range", reasons)
        self.assertIn("empty_key_text", reasons)

    def test_explicit_p_keeps_resolution_inside_that_paragraph(self):
        findings = [{"phase": "1_llm", "pn": 3, "original": "第三段内容",
                     "current": "第三段内容", "suggestion": "第三段内容改"}]
        skip_log = []
        resolved = _resolve_findings_to_p(
            findings, self.text_map, skip_log=skip_log)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["pn"], 3)
        self.assertEqual(skip_log, [])


class SkippedJsonEndToEndTests(unittest.TestCase):
    async def _fake_phase1(*_args, **_kwargs):
        return {
            "findings": [],
            "refined_chunks": ["# 第一章\n测试正文。"],
            "stats": {
                "total_chunks": 1,
                "checkpoint_hits": 0,
                "attempted_chunks": 1,
                "completed_chunks": 1,
                "failed_chunks": 0,
                "findings_from_llm": 1,
                "dropped_match": 1,
                "logical_calls": 1,
                "attempts": 1,
                "elapsed_seconds": 0.0,
            },
            "skipped": [{
                "reason": "no_op_change",
                "detail": "原句为空、改句为空或原句等于改句",
                "original": "原句",
                "corrected": "原句",
            }],
        }

    def test_run_max_writes_skipped_json_and_stats(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            md = root / "sample.md"
            md.write_text("# 第一章\n测试正文。", encoding="utf-8")
            alignment = {
                "stats": {"match": 1, "delete": 0, "insert": 0},
                "html_path": str(root / "alignment.html"),
            }

            with patch("src.max_pipeline.phase0_tgscc", return_value=[]), \
                    patch("src.max_pipeline.phase0_variants", return_value=[]), \
                    patch("src.max_pipeline.phase0_structure", return_value=[]), \
                    patch("src.max_pipeline.phase1_json_proofread",
                          side_effect=self._fake_phase1), \
                    patch("src.max_pipeline.phase3_align",
                          return_value=alignment), \
                    patch("src.max_pipeline.phase4_report",
                          return_value=str(root / "sample_max_report.html")):
                results = run_max(str(md))

            self.assertEqual(results["skipped_path"],
                             str(root / "sample_skipped.json"))
            skipped = json.loads(
                (root / "sample_skipped.json").read_text(encoding="utf-8"))
            self.assertEqual(skipped["schema"], "ai-proofread.skipped.v1")
            self.assertEqual(skipped["count"], 1)
            self.assertEqual(skipped["dropped"][0]["reason"], "no_op_change")
            self.assertTrue(skipped["source_sha256"])

            max_results = json.loads(
                (root / "sample_max_results.json").read_text(encoding="utf-8"))
            self.assertEqual(max_results["stats"]["phase1"]["dropped_match"], 1)
            self.assertEqual(max_results["stats"]["phase1"]["findings_from_llm"], 1)


if __name__ == "__main__":
    unittest.main()
