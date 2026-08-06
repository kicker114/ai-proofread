"""Regression tests for splitter context trimming (token efficiency).

The splitter previously attached the ENTIRE paragraph as each chunk's
context, causing ~18x input-token redundancy on book-length files.
build_local_context now keeps only the chapter title + local neighbors.
"""

import unittest

from src.splitter import (
    build_local_context,
    cut_text_by_length,
    split_markdown_by_title_and_length_with_context,
)


class BuildLocalContextTests(unittest.TestCase):
    def test_short_paragraph_keeps_title_and_neighbors(self):
        paragraph = "## 第一章\n这是一段不太长的文本。" * 5 + "目标句在这里。"
        target = "目标句在这里。"
        ctx = build_local_context(paragraph, target, context_pad=200, max_context=3000)
        # 标题保留
        self.assertIn("## 第一章", ctx)
        # target 自身不应出现在 context 里（避免 <context>/<target> 重复）
        self.assertNotIn(target, ctx)
        # 邻居文本应保留（前后文参照）
        self.assertIn("不太长的文本", ctx)

    def test_long_paragraph_trimmed_to_max(self):
        paragraph = "## 第十一章\n" + ("长句子内容。" * 600)
        target = paragraph[1000:1400]  # 某片段
        ctx = build_local_context(paragraph, target, context_pad=400, max_context=1500)
        self.assertLessEqual(len(ctx), 1500)
        self.assertIn("## 第十一章", ctx)  # 标题仍保留

    def test_target_not_found_falls_back(self):
        paragraph = "## 标题\n正文内容" * 50
        ctx = build_local_context(paragraph, "不存在的target", context_pad=100, max_context=500)
        self.assertLessEqual(len(ctx), 500)

    def test_splitter_trims_context_on_long_chapter(self):
        # 构造 3000 字章节，切成多块，每块 context 应被裁剪
        text = "## 第一章\n" + ("这是章节正文，包含各种待审校内容。" * 250)
        chunks = split_markdown_by_title_and_length_with_context(
            text, levels=[2], cut_by=300, context_pad=200, max_context=800)
        self.assertGreater(len(chunks), 1)  # 切成多块
        for c in chunks:
            self.assertLessEqual(len(c["context"]), 800 + 20)  # max_context + 标题余量
            self.assertIn("## 第一章", c["context"])  # 标题保留
            self.assertIn(c["target"], text)  # target 是原文子串


class CutTextByLengthTests(unittest.TestCase):
    def test_blank_line_splits(self):
        text = "第一段。" * 100 + "\n\n" + "第二段。" * 100
        pieces = cut_text_by_length(text, cut_by=300)
        self.assertGreater(len(pieces), 1)
        self.assertIn("第一段。", pieces[0])
        self.assertIn("第二段。", pieces[-1])

    def test_no_blank_line_hard_splits(self):
        # 无空行的连续文本：应被硬切，不能退化成单个大块
        text = "这是没有空行的连续长文本。" * 300
        pieces = cut_text_by_length(text, cut_by=300)
        self.assertGreater(len(pieces), 2)
        # 每块应接近 cut_by（硬切后略长，因句子边界）
        for p in pieces:
            self.assertLess(len(p), 600)

    def test_short_text_unchanged(self):
        text = "短文"
        pieces = cut_text_by_length(text, cut_by=300)
        self.assertEqual(pieces, ["短文"])


if __name__ == "__main__":
    unittest.main()
