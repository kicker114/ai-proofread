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

    def test_combined_chapter_heading_extracts_both_numbers(self):
        # 合并标题「第1章 第1节」/「第一章 第一节」：章+节编号都取（各层并列）
        for text in ["### 第1章 第1节 背景", "### 第一章 第一节 背景"]:
            tokens, _ = _tokens(text)
            self.assertEqual(len(tokens), 2, text)
            self.assertEqual(tokens[0].rule_id, "chapter", text)
            self.assertEqual(tokens[1].rule_id, "section", text)
            self.assertEqual(tokens[0].number_value, 1, text)
            self.assertEqual(tokens[1].number_value, 1, text)

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
            "# 第一部分 第一章": [("part", "第一部分", 1),
                                  ("chapter", "第一章", 1)],
            "# `第一章`": [("chapter", "第一章", 1)],
            "# - 第一章 序": [("chapter", "第一章", 1)],
        }
        for text, expected in cases.items():
            tokens, _ = _tokens(text)
            self.assertEqual(_kinds(tokens), expected, text)

    def test_subsection_rejects_decimal_and_date_at_heading_start(self):
        # rules.example.json 的 N. 加了 (?=\D|$) 前瞻；num_dot 排除量词/年份
        for text in ["# 12.9亿的市场规模", "# 2024.1 一季度回顾",
                     "# 12. 5亿", "# １２． ５億"]:
            tokens, _ = _tokens(text)
            self.assertEqual(tokens, [], text)
        # 真小节"12. 背景"（编号后空白）仍识别
        tokens, _ = _tokens("# 12. 背景\n")
        self.assertEqual(_kinds(tokens), [("subsection", "12.", 12)])
        # "3.14 的近似值"：语法上无法区分圆周率与第3章14节，num_dot 识别为
        # 多级编号（新规则合理行为）
        tokens, _ = _tokens("# 3.14 的近似值\n")
        self.assertEqual(_kinds(tokens), [("num_dot", "3.14", None)])

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

    def test_invalid_roman_rejected_as_missing_number(self):
        # 非法罗马（IIX、VX、IIII）不再解析为真实章节编号
        text = "# 第IIX章 附录\n# 第VX章 附录\n"
        tokens, _ = _tokens(text)
        for t in tokens:
            self.assertEqual(t.rule_id, "chapter")
            self.assertIsNone(t.number_value)
        # 合法罗马仍解析（X=10、XII=12）
        text2 = "# 第X章 附录\n# 第XII章 附录\n"
        tokens2, _ = _tokens(text2)
        nums = [t.number_value for t in tokens2 if t.rule_id == "chapter"]
        self.assertEqual(nums, [10, 12])

    def test_fullwidth_digits_and_period_normalized(self):
        # 全角数字/句点（第１章、1．、１２．）归一化为 ASCII 后命中规则，
        # token 偏移/raw_text 回退到原文
        cases = {
            "# 第１章 引言": [("chapter", "第１章", 1)],
            "# 第１节 背景": [("section", "第１节", 1)],
            "# 1．概述": [("subsection", "1．", 1)],
            "### １２．细节": [("subsection", "１２．", 12)],
        }
        for text, expected in cases.items():
            tokens, _ = _tokens(text)
            self.assertEqual(_kinds(tokens), expected, text)
            for t in tokens:
                self.assertEqual(text[t.start:t.end], t.raw_text, text)

    def test_setext_heading_level_1_so_h2_numbered_subsection_is_legal(self):
        # setext 章（=== 作 H1）+ ## 1.（H2）编号节 → 无 level_mismatch
        # （回归：普通行先产出 heading_level=None 的章 token 曾使此处误报）
        text = "第一章 总论\n===\n## 1. 概述\n## 2. 详述\n"
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])
        # setext 章 token 应带 heading_level=1
        tokens, _ = _tokens(text)
        chapter = next(t for t in tokens if t.rule_id == "chapter")
        self.assertEqual(chapter.heading_level, 1)

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

    def test_h2_numbered_subsection_under_chapter_is_legal(self):
        # 修复核心：## 1. 编号小节按作者意图作为 H2（与 ## 第一节 同级），
        # 不再因规则判定"目"=level3 而误报 hierarchy_gap
        text = "# 第一章 引言\n## 1. 概述\n## 2. 详述\n"
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_h1_to_h3_real_skip_still_reports_gap(self):
        # 真跳级：# 第一章 直接 ### 1.（缺 H2）→ hierarchy_gap 仍报
        text = "# 第一章 引言\n### 1. 直接目\n"
        result = check_text_with_rules(text, _RULES)
        self.assertIn("hierarchy_gap", [d.kind for d in result.diagnostics])

    def test_section_and_numbered_subsection_same_h2_level(self):
        # 章下混用 ## 第一节 与 ## 1.（同为 H2）→ 无诊断
        text = ("# 第一章 引言\n"
                "## 第一节 背景\n"
                "## 1. 概述\n"
                "## 2. 详述\n")
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_numbered_subsection_jump_still_checked(self):
        # 编号节 1→3 跳号：连续性检查不因修复而丢失
        text = "# 第一章 引言\n## 1. 概述\n## 3. 详述\n"
        result = check_text_with_rules(text, _RULES)
        kinds = [d.kind for d in result.diagnostics]
        self.assertIn("continuity_error", kinds)

    def test_merged_heading_same_line_nested_no_gap(self):
        # 合并标题「第1章 第1节」/「第一章 第一节」：章+节同层嵌套，不误报
        # hierarchy_gap（同一行内是"一个标题的多层编号"，不是层级跳变）
        for text in ["# 第1章 第1节", "# 第一章 第一节",
                     "# 第1章 第1节 第3目"]:
            result = check_text_with_rules(text, _RULES)
            self.assertEqual(result.diagnostics, [], text)

    def test_merged_heading_tokens_share_line_id(self):
        # 同一标题行的章+节 token 共享 line_id（供 builder 同层嵌套）
        text = "# 第1章 第1节\n"
        tokens, _ = _tokens(text)
        chapter = next(t for t in tokens if t.rule_id == "chapter")
        section = next(t for t in tokens if t.rule_id == "section")
        self.assertIsNotNone(chapter.line_id)
        self.assertEqual(chapter.line_id, section.line_id)

    def test_traditional_jie_and_fullwidth_roman_normalized(self):
        # 全角/繁體節（第１節）、全角罗马（第Ⅻ章/第Ｘ章）、Unicode 罗马、
        # 小写 iv：归一化后命中，偏移映射回原文
        cases = {
            "# 第１節 背景": [("section", "第１節", 1)],
            "# 第Ⅻ章 结束": [("chapter", "第Ⅻ章", 12)],
            "# 第Ｘ章 附录": [("chapter", "第Ｘ章", 10)],
            "# 第iv章": [("chapter", "第iv章", 4)],
        }
        for text, expected in cases.items():
            tokens, _ = _tokens(text)
            self.assertEqual(_kinds(tokens), expected, text)
            for t in tokens:
                self.assertEqual(text[t.start:t.end], t.raw_text, text)

    def test_merged_heading_narratives_excluded(self):
        # 叙述性提及（前有正文词 / 引号 / 空格）不产出第二个真实编号
        cases = [
            "# 第一章 第二章的比较",      # 空格+同层 → 排除
            "# 第一章 参考文献「第2章」",   # 引号 → 排除
            "# 第一章 与第二章的比较",     # 与 → 排除
        ]
        for text in cases:
            tokens, _ = _tokens(text)
            self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)], text)
        # 起点非编号的叙述标题 → 无 token
        tokens, _ = _tokens("### 罗杰斯《创新的扩散》第三章书评")
        self.assertEqual(tokens, [])

    def test_merged_heading_compact_and_pure_text(self):
        # 无空格紧凑（第1章第1节）、纯文本行（第1章 第1节）、第字前缀
        cases = {
            "# 第1章第1节": [("chapter", "第1章", 1), ("section", "第1节", 1)],
            "第1章 第1节": [("chapter", "第1章", 1), ("section", "第1节", 1)],
            "# 第1章 第1.小节": [("chapter", "第1章", 1),
                                 ("subsection", "1.", 1)],
        }
        for text, expected in cases.items():
            tokens, _ = _tokens(text)
            self.assertEqual(_kinds(tokens), expected, text)

    def test_fullwidth_decimal_with_space_not_subsection(self):
        # 全角小数带空格（１２． ５亿）不再被当目编号
        for text in ["# １２． ５亿", "# 12. 5亿", "# １２．５ 亿元"]:
            tokens, _ = _tokens(text)
            self.assertEqual(tokens, [], text)
        # 标题内真编号（12. 背景 / 1. 2024年的回顾）仍识别
        tokens, _ = _tokens("# 12. 背景\n")
        self.assertEqual(_kinds(tokens), [("subsection", "12.", 12)])
        tokens, _ = _tokens("# 1. 2024年的回顾\n")
        self.assertEqual(_kinds(tokens), [("subsection", "1.", 1)])

    def test_merged_heading_cross_line_same_chapter_ok(self):
        # 合并标题多行展开（第1章 第1节 / 第1章 第2节）→ 同章多节，不误报
        text = "# 第1章 第1节\n# 第1章 第2节\n"
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])
        # 独立的真重复章（无更深层）仍报
        text2 = "# 第1章\n# 第1章\n"
        result2 = check_text_with_rules(text2, _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result2.diagnostics])

    def test_placeholder_roman_in_mixed_system_is_missing(self):
        # 草稿占位「第X章」混排（第1章→第X章→第2章）：判占位 → number_missing，
        # 而非连续性误报 1→10→2
        text = "# 第1章\n# 第X章\n# 第2章\n"
        result = check_text_with_rules(text, _RULES)
        kinds = [d.kind for d in result.diagnostics]
        self.assertIn("number_missing", kinds)
        self.assertNotIn("continuity_error", kinds)

    def test_section_roman_numbering_supported(self):
        # 节级罗马（第IV节）正常嵌套，无诊断
        text = "# 第一章\n## 第IV节\n"
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_legit_single_letter_roman_not_placeholder(self):
        # 第I章=1 是合法罗马，混合体系中不被误判为占位 number_missing
        text = "# 第I章 前言\n# 第II章 绪论\n# 第1章 正文\n# 第2章 结论\n"
        result = check_text_with_rules(text, _RULES)
        # 罗马→阿拉伯体系切换：数值不可比，不报连续性
        self.assertEqual(result.diagnostics, [])
        # 仅 X 是占位
        text2 = "# 第1章\n# 第X章\n# 第2章\n"
        result2 = check_text_with_rules(text2, _RULES)
        self.assertIn("number_missing", [d.kind for d in result2.diagnostics])

    def test_cross_line_same_chapter_requires_both_with_sections(self):
        # 独立章 + 带节行重复（第2章 / 第2章 第1节）→ 真实重复章，仍报
        text = "# 第1章\n# 第2章\n# 第2章 第1节\n# 第3章\n"
        result = check_text_with_rules(text, _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result.diagnostics])

    def test_cross_line_section_continuity_checked(self):
        # 合并标题多行展开的节号连续性：1→3 跳节、1→1 重复都报
        for text in ["# 第1章 第1节\n# 第1章 第3节\n",
                     "# 第1章 第1节\n# 第1章 第1节\n"]:
            result = check_text_with_rules(text, _RULES)
            self.assertIn("continuity_error",
                          [d.kind for d in result.diagnostics], text)
        # 连续 1→2 合法
        text2 = "# 第1章 第1节\n# 第1章 第2节\n"
        result2 = check_text_with_rules(text2, _RULES)
        self.assertEqual(result2.diagnostics, [])

    def test_subsection_with_amount_title_not_dropped(self):
        # 真编号小节标题以数量词开头（1. 2亿用户）不被误删
        text = "# 1. 2亿用户\n# 2. 500万元投资\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("subsection", "1.", 1), ("subsection", "2.", 2)])
        # 纯数量小数（12. 5亿）仍排除，含繁体億
        for t in ["# 12. 5亿", "# １２． ５億"]:
            tokens, _ = _tokens(t)
            self.assertEqual(tokens, [], t)

    def test_plain_line_merged_section_requires_space_after(self):
        # 普通行叙述「第一章 第三节内容是重点。」：第X节后紧跟正文 → 不合并
        text = "第一章 第三节内容是重点。\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])
        # 真合并仍取全
        tokens, _ = _tokens("第1章 第1节\n")
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第1章", 1), ("section", "第1节", 1)])

    def test_ghost_section_narrative_excluded(self):
        # 「第一章 第一节的比较」的"第一节"是叙述（编号后紧跟正文词），
        # 不合并成幽灵节
        text = "# 第一章 第一节的比较\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])
        # 有空格的真合并保留
        tokens, _ = _tokens("# 第一章 第一节 背景\n")
        self.assertEqual(_kinds(tokens), [
            ("chapter", "第一章", 1), ("section", "第一节", 1)])

    def test_fullwidth_decor_prefix_normalized(self):
        # 全角数字装饰前缀（第１部分 第一章）识别 part + 章
        text = "# 第１部分 第一章 标题\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("part", "第１部分", 1), ("chapter", "第一章", 1)])

    def test_chinese_ordered_item_recognized(self):
        # 中文序号一、二、三 识别，跳号报错
        tokens, _ = _tokens("# 一、研究背景\n")
        self.assertEqual(_kinds(tokens), [("cn_item", "一、", 1)])
        tokens, _ = _tokens("# 二、研究意义\n")
        self.assertEqual(_kinds(tokens), [("cn_item", "二、", 2)])
        # 一、二、三 递增合法
        result = check_text_with_rules(
            "# 一、\n# 二、\n# 三、\n", _RULES)
        self.assertEqual(result.diagnostics, [])
        # 一、三、跳号报错
        result = check_text_with_rules("# 一、\n# 三、\n", _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result.diagnostics])

    def test_parenthesized_chinese_item_recognized(self):
        # 括号序号（一）（二）识别（不被装饰剥离吞掉）
        text = "# （一）概念界定\n# （二）历史沿革\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("paren_cn", "（一）", 1), ("paren_cn", "（二）", 2)])

    def test_ar_punct_item_recognized_and_jump_checked(self):
        # 数字顿号 1、2、 识别，跳号报错
        tokens, _ = _tokens("# 1、研究方法\n# 2、数据来源\n")
        self.assertEqual(_kinds(tokens), [
            ("ar_punct", "1、", 1), ("ar_punct", "2、", 2)])
        result = check_text_with_rules("# 1、\n# 3、\n", _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result.diagnostics])

    def test_num_dot_level_by_dot_count(self):
        # 多级数字 1.1→节(2)、1.1.1→目(3)，量词/年份排除
        tokens, _ = _tokens("# 1.1 研究背景\n")
        self.assertEqual(_kinds(tokens), [("num_dot", "1.1", None)])
        self.assertEqual(tokens[0].level, 2)
        tokens, _ = _tokens("# 1.1.1 问题提出\n")
        self.assertEqual(tokens[0].level, 3)
        # 量词/年份小数排除
        for t in ["# 12.9亿的市场规模", "# 2024.1 一季度回顾"]:
            tokens, _ = _tokens(t)
            self.assertEqual(tokens, [], t)

    def test_part_heading_recognized(self):
        # 部/编/卷/篇/册 独立标题识别
        expected = {
            "# 第一部 思想的形成": "第一部",
            "# 第一卷 思想": "第一卷",
            "# 第一编 总则": "第一编",
        }
        for t, raw in expected.items():
            tokens, _ = _tokens(t)
            self.assertEqual(_kinds(tokens), [("part", raw, 1)], t)
        # 第1部分 第一章 → part + 章（part 层级保留）
        tokens, _ = _tokens("# 第1部分 第一章 引言\n")
        self.assertEqual(_kinds(tokens), [
            ("part", "第1部分", 1), ("chapter", "第一章", 1)])

    def test_special_sections_recognized_not_numbered(self):
        # 无编号特殊部分（前言/附录/后记/参考文献）识别，不参与连续性
        for t in ["# 前言", "# 绪论", "# 附录", "# 后记", "# 参考文献",
                  "# 引言", "# 结语"]:
            tokens, _ = _tokens(t)
            self.assertEqual(len(tokens), 1, t)
            self.assertEqual(tokens[0].rule_id, "special", t)

    def test_chapter_direct_cn_item_no_gap(self):
        # # 第一章 直接 # 一、：同级 H1 标题，不报 hierarchy_gap
        text = "# 第一章 引言\n# 一、研究背景\n# 二、研究意义\n"
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_part_resets_chapter_continuity(self):
        # 部内章节重新起号（第一部 第1-2章 → 第二部 第1章）→ 不误报 2→1
        text = ("# 第一部 总则\n# 第一章 适用范围\n# 第二章 基本原则\n"
                "# 第二部 分则\n# 第一章 细则\n")
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])
        # 独立章跳号仍报
        result2 = check_text_with_rules("# 第一章\n# 第四章\n", _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result2.diagnostics])

    def test_part_prefix_merged_section_no_gap(self):
        # 第一部 第1节：part + section 同层嵌套，不报 hierarchy_gap
        text = "# 第一部 第1节 概述\n"
        tokens, _ = _tokens(text)
        self.assertEqual(_kinds(tokens), [
            ("part", "第一部", 1), ("section", "第1节", 1)])
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])
        # 第一部 第一章 → part + 章（part 层级保留）
        tokens, _ = _tokens("# 第一部 第一章 引言\n")
        self.assertEqual(_kinds(tokens), [
            ("part", "第一部", 1), ("chapter", "第一章", 1)])

    def test_paren_cn_with_decor_prefix_recognized(self):
        # 括号序号前有装饰前缀（- / 「」）也能识别
        for t in ["# - （一）概念", "# 「（一）」概念", "# （一）概念"]:
            tokens, _ = _tokens(t)
            self.assertEqual(_kinds(tokens), [("paren_cn", "（一）", 1)], t)
        # 括号装饰标题仍识别
        tokens, _ = _tokens("# （第一章） 引言\n")
        self.assertEqual(_kinds(tokens), [("chapter", "第一章", 1)])

    def test_halfwidth_paren_and_volume_no_prefix(self):
        # 半角括号 (一)、无第前缀 卷一/卷X 识别
        tokens, _ = _tokens("# (一) 概念\n")
        self.assertEqual(_kinds(tokens), [("paren_cn", "(一)", 1)])
        for t, raw, num in [("# 卷一", "卷一", 1), ("# 卷X", "卷X", 10),
                            ("# 第一部", "第一部", 1)]:
            tokens, _ = _tokens(t)
            self.assertEqual(_kinds(tokens), [("part", raw, num)], t)

    def test_part_absorbs_deeper_cross_line_sections(self):
        # 卷→节（跨行，同为 H1）：节挂到卷下，不报 hierarchy_gap
        text = ("# 第一卷 基础\n# 第1节 引言\n# 第2节 综述\n"
                "# 第二卷 进阶\n# 第1节 方法\n")
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_same_part_section_jump_checked(self):
        # 同卷内节跳号（第1卷 第1节 / 第1卷 第3节）→ 报 1→3
        text = "# 第1卷 第1节\n# 第1卷 第3节\n"
        result = check_text_with_rules(text, _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result.diagnostics])
        # 连续 1→2 合法
        text2 = "# 第1卷 第1节\n# 第1卷 第2节\n"
        result2 = check_text_with_rules(text2, _RULES)
        self.assertEqual(result2.diagnostics, [])

    def test_merged_part_chapter_keeps_part_and_restart(self):
        # 第一部 第一章 合并标题：part + 章都保留
        tokens, _ = _tokens("# 第一部 第一章 总则\n")
        self.assertEqual(_kinds(tokens), [
            ("part", "第一部", 1), ("chapter", "第一章", 1)])
        # 跨部章重启不误报
        text = ("# 第一部 第一章\n# 第一部 第二章\n"
                "# 第二部 第一章\n")
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_appendix_with_number_suffix_recognized(self):
        # 附录A / 附录一 识别为 special（词边界 + 编号后缀）
        for t in ["# 附录A", "# 附录一", "# 附录1"]:
            tokens, _ = _tokens(t)
            self.assertEqual(_kinds(tokens), [("special", t[2:], None)], t)

    def test_volume_no_prefix_with_chapter_keeps_chapter(self):
        # 卷一 第一章：无第前缀 part + 章都保留，章跳号检出
        tokens, _ = _tokens("# 卷一 第一章 总则\n")
        self.assertEqual(_kinds(tokens), [
            ("part", "卷一", 1), ("chapter", "第一章", 1)])
        result = check_text_with_rules(
            "# 卷一 第一章\n# 卷一 第三章\n", _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result.diagnostics])

    def test_plain_text_chapter_direct_cn_item_no_gap(self):
        # 纯文本书稿 第一章 直接 一、/（一）（无 #）：柔性小节不报 gap
        text = ("第一章 引言\n（一）背景\n（二）问题\n"
                "第二章 方法\n一、样本\n二、指标\n")
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])
        # 序号跳号仍检出
        text2 = "第一章 引言\n（一）背景\n（三）问题\n"
        result2 = check_text_with_rules(text2, _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result2.diagnostics])

    def test_same_number_part_different_unit_not_merged(self):
        # 第一部(部) 与 卷一(卷) 编号同为 1：不同单位不合并，不跨单位误报
        text = ("# 第一部 第1节\n# 第二部 第1节\n"
                "# 卷一 第3节\n")
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

    def test_plain_text_ordered_headings_recognized(self):
        # 无 # 纯文本书稿的（一）/一、/1、 标题识别，跳号检出
        tokens, _ = _tokens("（一）研究背景\n")
        self.assertEqual(_kinds(tokens), [("paren_cn", "（一）", 1)])
        tokens, _ = _tokens("一、研究背景\n")
        self.assertEqual(_kinds(tokens), [("cn_item", "一、", 1)])
        tokens, _ = _tokens("1、方法\n")
        self.assertEqual(_kinds(tokens), [("ar_punct", "1、", 1)])
        # 列表项（1. 数字句点）仍不识别
        tokens, _ = _tokens("1. 第一项\n")
        self.assertEqual(tokens, [])
        # 跳号检出
        result = check_text_with_rules(
            "（一）研究背景\n（三）研究意义\n", _RULES)
        self.assertIn("continuity_error",
                      [d.kind for d in result.diagnostics])

    def test_special_word_boundary_not_overmatched(self):
        # special 后界：完整词匹配，避免跋山涉水/结论性思考误报
        for t in ["# 跋山涉水", "# 结论性思考", "# 参考文献格式规范",
                  "# 注释方法"]:
            tokens, _ = _tokens(t)
            self.assertEqual(tokens, [], t)
        # 完整特殊词仍识别
        for t in ["# 参考文献", "# 结论", "# 附录"]:
            tokens, _ = _tokens(t)
            self.assertEqual(len(tokens), 1, t)
            self.assertEqual(tokens[0].rule_id, "special", t)

    def test_h2_numbered_subsection_with_h3_child_is_legal(self):
        # 章 + ## 1.（H2）+ ### 2.（H3）：H2→H3 相邻 → 无诊断
        text = "# 第一章 引言\n## 1. 概述\n### 2. 细目\n"
        result = check_text_with_rules(text, _RULES)
        self.assertEqual(result.diagnostics, [])

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
