"""Regression tests for structure-checker heading-only scanning (fix).

The old `scan_text` ran each rule pattern over the whole document text, so
prose like「在第二章中」「12.9亿」「第6条规则」produced bogus structure
tokens (chapter jumps, subsection numbers of 12/13, etc.). The fix scans
only markdown ATX heading lines (`^#{1,9} `) and anchors each rule match at
the heading content start.
"""

import unittest
from pathlib import Path

from src.max_pipeline import phase0_structure
from src.structure_checker.check_structure import check_text_with_rules
from src.structure_checker.rules import load_rules_from_json
from src.structure_checker.scanner import scan_text

_RULES = str(
    Path(__file__).resolve().parent.parent
    / "src" / "structure_checker" / "rules.example.json")


def _tokens(text: str):
    rules, _ = load_rules_from_json(_RULES)
    tokens, diags = scan_text(text, rules)
    return tokens, diags


def _kinds(tokens):
    return [(t.rule_id, t.raw_text, t.number_value) for t in tokens]


class ScannerUnitTests(unittest.TestCase):
    def test_prose_only_text_produces_no_tokens(self):
        prose = (
            "本书在第二章中讨论了技术扩散，其他章节则聚焦治理。\n"
            "2024年用户规模达12.9亿，同比增长18.6%。\n"
            "第三章书评见附录。作者按1. 背景、2. 现状依次展开，\n"
            "并参考了第五章结论。这里的第6条规则适用于全部场景。\n"
        )
        tokens, diags = _tokens(prose)
        self.assertEqual(tokens, [])
        self.assertEqual(diags, [])

    def test_markdown_headings_detected_with_numbers(self):
        text = (
            "# 第一章 引言\n"
            "## 第一节 背景\n"
            "### 1. 意义\n"
            "### 2. 现状\n"
        )
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("section", "第一节", 1),
            ("subsection", "1.", 1),
            ("subsection", "2.", 2),
        ])

    def test_token_positions_slice_to_raw_text(self):
        text = (
            "# 第一章 引言\n"
            "## 第一节 背景\n"
        )
        tokens, _ = _tokens(text)
        self.assertGreaterEqual(len(tokens), 2)
        for t in tokens:
            self.assertEqual(text[t.start:t.end], t.raw_text)

    def test_crlf_line_endings_keep_positions_correct(self):
        text = "# 第一章 引言\r\n## 第一节 背景\r\n### 3. 内容\r\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("section", "第一节", 1),
            ("subsection", "3.", 3),
        ])
        for t in tokens:
            self.assertEqual(text[t.start:t.end], t.raw_text)

    def test_heading_body_chapter_word_not_matched(self):
        text = "# 第一章 引言\n### 罗杰斯《创新的扩散》第三章书评\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])

    def test_combined_chapter_heading_only_chapter_token(self):
        # 标题正文含"第一节"不应额外产出 section token（锚定匹配）
        text = "### 第一章 第一节 背景\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])

    def test_indented_heading_ignored_consistent_with_splitter(self):
        text = "  ## 第一节 背景\n# 第一章 引言\n"
        tokens, _ = _tokens(text)
        # 行首空格的"  ## "不是标题（splitter 同样不切分），只有列 0 的 # 生效
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])

    def test_plain_text_chapter_heading_detected_when_number_has_space(self):
        # 无 # 前缀的纯文本书稿："第一章 引言"行首锚定 + 编号后空白 → 识别
        text = "第一章 引言\n这是正文。\n第二章 主体\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("chapter", "第二章", 2),
        ])

    def test_plain_text_chapter_reference_without_space_not_detected(self):
        # 正文句首引用"第三章书评见附录"：编号后无空白 → 不是标题，不误报
        text = "第三章书评见附录。\n在第二章中讨论了技术扩散。\n"
        tokens, _ = _tokens(text)
        self.assertEqual(tokens, [])

    def test_no_space_atx_heading_detected(self):
        # 中文书稿最常见的"#第一章"（# 后无空格）
        text = "#第一章 引言\n##第1节 背景\n###第I章 附录\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("section", "第1节", 1),
            ("chapter", "第I章", 1),
        ])

    def test_fullwidth_space_separator_detected(self):
        text = "#　第一章 引言\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])

    def test_utf8_bom_does_not_break_heading_anchor(self):
        text = "﻿# 第一章 引言\n## 第一节 背景\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("section", "第一节", 1),
        ])
        # 偏移须在剥离 BOM 后仍与原文一致
        for t in tokens:
            self.assertEqual(text[t.start:t.end], t.raw_text)

    def test_decorator_prefixes_stripped(self):
        cases = {
            "# （第一章）": [("chapter", "第一章", 1)],
            "# 【第三章】": [("chapter", "第三章", 3)],
            "# 第一部分 第一章": [("chapter", "第一章", 1)],
            "# `第一章`": [("chapter", "第一章", 1)],
            "# - 第一章 序": [("chapter", "第一章", 1)],
        }
        for text, expected in cases.items():
            tokens, _ = _tokens(text)
            self.assertEqual(_kinds(tokens), expected, text)

    def test_subsection_rejects_decimal_and_date_at_heading_start(self):
        # rules.example.json 的 N. 加了 (?=\D|$) 前瞻
        for text in ["# 12.9亿的市场规模", "# 2024.1 一季度回顾",
                     "# 3.14 的近似值"]:
            tokens, _ = _tokens(text)
            self.assertEqual(tokens, [], text)
        # 真小节"12. 背景"（编号后空白）仍识别
        tokens, _ = _tokens("# 12. 背景\n")
        self.assertEqual(_kinds(tokens), [("subsection", "12.", 12)])

    def test_plain_ordered_list_not_subsection(self):
        # 无 # 的"1. 列表项"是列表不是目（plain_only_prefix 只认 第X章/第X节）
        text = "1. 第一项\n2. 第二项\n"
        tokens, _ = _tokens(text)
        self.assertEqual(tokens, [])

    def test_fenced_code_block_headings_skipped(self):
        text = ("# 第一章 真实\n"
                "```\n# 第二章 这是代码不是标题\n1. 代码里的列表\n"
                "```\n"
                "# 第三章 继续\n")
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("chapter", "第三章", 3),
        ])

    def test_setext_heading_detected(self):
        text = "第一章 总论\n===\n\n正文。\n\n第二章 背景\n===\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1),
            ("chapter", "第二章", 2),
        ])

    def test_setext_no_space_number_detected_but_dash_rule_ignored(self):
        # setext 标题编号后无空格也识别（下划线已表明标题意图）
        text = "第一章总论\n===\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])
        # --- 与水平分隔线歧义 → 不支持，正文"第三章书评见附录" + --- 不误报
        text2 = "第三章书评见附录。\n---\n"
        tokens2, _ = _tokens(text2)
        self.assertEqual(tokens2, [])

    def test_multi_digit_subsection(self):
        text = "### 12. 扩展内容\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("subsection", "12.", 12)])

    def test_restart_continuity_allowed_for_subsection(self):
        # increase_or_restart：12 → 1（重启）→ 2 均合法
        text = (
            "# 第一章 引言\n"
            "## 第一节 背景\n"
            "### 12. 上一节延续\n"
            "### 1. 重启编号\n"
            "### 2. 继续\n"
        )
        result = check_text_with_rules(text, _RULES)
        self.assertEqual([d.kind for d in result.diagnostics], [])


class ScannerIntegrationTests(unittest.TestCase):
    def test_synthetic_sample_still_two_diagnostics(self):
        sample = (Path(__file__).resolve().parent.parent
                  / "samples" / "审校合成稿.md")
        text = sample.read_text(encoding="utf-8")
        struct = phase0_structure(text)
        self.assertEqual(len(struct), 2)
        types = {r["type"] for r in struct}
        self.assertIn("hierarchy_gap", types)
        self.assertIn("continuity_error", types)

    def test_chapter_skip_number_detected(self):
        text = (
            "# 第一章 引言\n"
            "## 第一节 背景\n"
            "# 第四章 跳章\n"
            "## 第二节 内容\n"
        )
        result = check_text_with_rules(text, _RULES)
        kinds = [d.kind for d in result.diagnostics]
        self.assertIn("continuity_error", kinds)
        msgs = [d.message for d in result.diagnostics
                if d.kind == "continuity_error"]
        self.assertTrue(any("1 → 4" in m for m in msgs))

    def test_orphan_section_reports_hierarchy_gap(self):
        # 无章直接出现的"第一节"（optional=False 且存在更浅层级）→ hierarchy_gap
        text = (
            "## 第一节 无章\n"
            "### 1. 内容\n"
            "### 2. 内容\n"
        )
        result = check_text_with_rules(text, _RULES)
        self.assertIn("hierarchy_gap", [d.kind for d in result.diagnostics])

    def test_full_valid_tree_no_diagnostics(self):
        text = (
            "# 第一章 引言\n"
            "## 第一节 背景\n"
            "### 1. 意义\n"
            "### 2. 现状\n"
            "## 第二节 方法\n"
            "### 1. 步骤\n"
            "### 2. 指标\n"
            "# 第二章 主体\n"
            "## 第一节 内容\n"
            "### 1. 呈现\n"
            "### 2. 讨论\n"
        )
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])


if __name__ == "__main__":
    unittest.main()
