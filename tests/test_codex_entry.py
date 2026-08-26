import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from src.max_pipeline import (
    _findings_to_issues,
    _save_findings,
    _trim_trailing_garbage,
    _whitespace_only,
    run_max,
)


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "ai-proofread"


class CodexEntryTests(unittest.TestCase):
    def test_project_entry_and_skill_contract_exist(self):
        agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        metadata = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn("$ai-proofread", agents_text)
        self.assertIn("name: ai-proofread", skill_text)
        self.assertIn("ai-proofread.source.v1", skill_text)
        self.assertIn("ai-proofread.findings.v1", skill_text)
        self.assertIn('"issues"', skill_text)
        self.assertIn("pipeline", skill_text)
        self.assertIn("codex-native", skill_text)
        self.assertIn("$ai-proofread", metadata)

    def test_skill_passes_codex_validator(self):
        validator = Path.home() / ".codex" / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"
        completed = subprocess.run(
            [sys.executable, str(validator), str(SKILL)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_max_findings_overwrite_stale_output_and_include_names(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "max_results.json"
            output.write_text('{"llm":[{"current":"stale"}]}', encoding="utf-8")
            _save_findings(str(output), {
                "tgscc": [],
                "variants": [],
                "structure": [],
                "llm": [],
                "names": [{"current": "Moon", "suggestion": "月球"}],
                "source_path": "/tmp/source.docx",
                "source_sha256": "a" * 64,
            })
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["llm"], [])
            self.assertEqual(len(data["names"]), 1)
            self.assertEqual(data["source_sha256"], "a" * 64)

    def test_max_refuses_word_writeback_when_source_changes(self):
        async def no_findings(*_args, **_kwargs):
            return {"findings": [], "refined_chunks": []}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("Source paragraph")
            document.save(source)
            markdown = root / "source.md"
            markdown.write_text("Source paragraph", encoding="utf-8")
            alignment = {
                "stats": {"match": 1, "delete": 0, "insert": 0},
                "html_path": str(root / "alignment.html"),
            }

            with patch("src.cli._resolve_input", return_value=markdown), \
                    patch("src.extract_source.sha256_file",
                          side_effect=["a" * 64, "b" * 64]), \
                    patch("src.max_pipeline.phase0_tgscc", return_value=[]), \
                    patch("src.max_pipeline.phase0_variants", return_value=[]), \
                    patch("src.max_pipeline.phase0_structure", return_value=[]), \
                    patch("src.splitter.split_markdown_by_title_and_length_with_context",
                          return_value=[]), \
                    patch("src.max_pipeline.phase1_json_proofread",
                          side_effect=no_findings), \
                    patch("src.max_pipeline.phase3_align", return_value=alignment), \
                    patch("src.max_pipeline.phase4_report",
                          return_value=str(root / "report.html")):
                with self.assertRaisesRegex(RuntimeError, "审校期间发生变化"):
                    run_max(str(source), writeback=True)

            findings = json.loads(
                (root / "source_max_results.json").read_text(encoding="utf-8"))
            self.assertEqual(findings["source_sha256"], "a" * 64)

    def test_fuzzy_word_location_is_comment_only(self):
        issues = _findings_to_issues([{
            "phase": "0b_variant",
            "location": "P2",
            "current": "short anchor",
            "suggestion": "replacement",
            "reason": "Fuzzy match.",
            "p_match_method": "fuzzy",
        }])
        self.assertEqual(issues[0]["fix_class"], "verify")
        self.assertEqual(issues[0]["category"], "定位待核")

    def test_llm_reason_not_fallback_to_full_original(self):
        # LLM 发现不带 reason（prompt 禁止解释），【依据】不得回落到整句原文，
        # 否则会与【建议】几乎一模一样的重复引述。
        issues = _findings_to_issues([{
            "phase": "1_llm",
            "type": "correction",
            "original": "一个人仍然活着，却渐渐失去了构成属于自己灵魂的一切。",
            "suggestion": "一个人仍然活着，却渐渐失去了构成自己灵魂的一切。",
            "location": "P3",
        }])
        self.assertEqual(issues[0]["fix_class"], "must_fix")
        self.assertEqual(issues[0]["reason"], "")

    def test_variant_reason_uses_basis_not_original(self):
        # variants 的真实依据在 basis（词典来源），不应回落到 original 原词。
        issues = _findings_to_issues([{
            "phase": "0b_variant",
            "type": "variant_form",
            "original": "子细",
            "suggestion": "仔细",
            "location": "P5",
            "basis": "现代汉语词典/异形词表",
        }])
        self.assertEqual(issues[0]["fix_class"], "must_fix")
        self.assertEqual(issues[0]["reason"], "现代汉语词典/异形词表")

    def test_whitespace_only_detects_linebreak_and_space_noise(self):
        # PDF 折行/空格碎片 → 去空白后相同 → 视为纯空白假阳性
        self.assertTrue(_whitespace_only("这样\n的场景", "这样的场景"))
        self.assertTrue(_whitespace_only("（ paisa ）", "（paisa）"))
        self.assertTrue(_whitespace_only("西部， 5另一个", "西部，5另一个"))
        # 真正的内容差异 → 不是纯空白
        self.assertFalse(_whitespace_only("人", "病人会"))
        self.assertFalse(_whitespace_only("卓越", "杰出"))

    def test_trim_trailing_garbage_strips_ascii_tail(self):
        # 句末标点后紧跟孤立 ASCII 垃圾 → 剥离
        self.assertEqual(_trim_trailing_garbage("死去。n\n"), "死去。")
        self.assertEqual(_trim_trailing_garbage("这会发生！x"), "这会发生！")
        # 正常英文/数字结尾 → 不误伤
        self.assertEqual(_trim_trailing_garbage("（paisa）。"), "（paisa）。")
        self.assertEqual(_trim_trailing_garbage("大约 10 年"), "大约 10 年")


if __name__ == "__main__":
    unittest.main()
