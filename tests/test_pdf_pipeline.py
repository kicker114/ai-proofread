"""PDF 高亮批注的独立回归测试。

直接运行：python3 tests/test_pdf_pipeline.py
"""

import csv
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fitz

import src.pdf_pipeline as pdf_pipeline
from src.pdf_pipeline import annotate_pdf, pdf2md
from src.extract_source import extract_source


class PdfPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="ai-proofread-pdf-")
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source.pdf"

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "alpha beta")
        page.insert_text((72, 92), "gamma delta")
        page.insert_text((72, 130), "repeat phrase")
        page.insert_text((300, 130), "repeat phrase")
        page.insert_text((72, 160), "unique fragment")
        page.insert_text((72, 190), "fuzzy target")
        page = doc.new_page()
        page.insert_text((72, 72), "repeat phrase")
        doc.save(self.source)
        doc.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_findings(self, findings, source_hash=True):
        normalized = []
        for finding in findings:
            item = {
                "fix_class": "verify",
                "current": "",
                "suggested": "",
                "reason": "Review finding.",
                "category": "proofreading",
                "page": 1,
            }
            item.update(finding)
            normalized.append(item)
        data = {
            "schema": "ai-proofread.findings.v1",
            "issues": normalized,
        }
        if source_hash:
            data["source_sha256"] = hashlib.sha256(
                self.source.read_bytes()).hexdigest()
        path = self.root / "findings.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def _read_csv(path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_exact_quads_duplicate_disambiguation_and_evidence(self):
        findings = self._write_findings([
            {
                "fix_class": "must_fix",
                "current": "alpha beta gamma delta",
                "suggested": "fixed text",
                "reason": "A deterministic correction.",
                "category": "fact",
                "evidence": [{
                    "title": "Primary source",
                    "url": "https://example.com/source",
                    "accessed_at": "2026-08-06",
                }],
            },
            {
                "fix_class": "verify",
                "current": "repeat phrase",
                "suggested": "ambiguous",
            },
            {
                "fix_class": "verify",
                "current": "repeat phrase",
                "suggested": "page two",
                "page": 2,
            },
        ])
        output = self.root / "annotated.pdf"
        csv_path = self.root / "annotations.csv"

        annotate_pdf(
            str(self.source), str(findings), str(output), author="Codex",
            csv_path=str(csv_path))

        rows = self._read_csv(csv_path)
        self.assertEqual(rows[0]["状态"], "hit")
        self.assertEqual(rows[0]["quad数"], "2")
        self.assertEqual(rows[1]["状态"], "ambiguous")
        self.assertEqual(rows[1]["候选页"], "1")
        self.assertEqual(rows[2]["状态"], "hit")
        self.assertEqual(rows[2]["页码"], "2")

        doc = fitz.open(output)
        page_one = doc[0]
        page_one_annots = list(page_one.annots() or ())
        self.assertEqual(len(page_one_annots), 1)
        self.assertEqual(len(page_one_annots[0].vertices), 8)
        self.assertEqual(page_one_annots[0].type[1], "Highlight")
        self.assertEqual(page_one_annots[0].info["title"], "Codex")
        self.assertIn("Primary source", page_one_annots[0].info["content"])
        self.assertIn("https://example.com/source",
                      page_one_annots[0].info["content"])
        page_two = doc[1]
        page_two_annots = list(page_two.annots() or ())
        self.assertEqual(len(page_two_annots), 1)
        doc.close()

    def test_fragment_and_fuzzy_require_explicit_opt_in(self):
        findings = self._write_findings([
            {
                "fix_class": "polish",
                "current": "unique fragment -- missing tail",
                "suggested": "fragment suggestion",
            },
            {
                "fix_class": "verify",
                "current": "fuzzy txrget",
                "suggested": "fuzzy target",
            },
        ])
        preview_csv = self.root / "preview.csv"
        preview_pdf = self.root / "preview.pdf"

        annotate_pdf(
            str(self.source), str(findings), str(preview_pdf),
            csv_path=str(preview_csv))
        preview_rows = self._read_csv(preview_csv)
        self.assertEqual([row["状态"] for row in preview_rows],
                         ["preview", "preview"])
        self.assertEqual([row["匹配方式"] for row in preview_rows],
                         ["fragment", "fuzzy"])
        self.assertGreater(float(preview_rows[1]["得分"]), 85.0)
        preview_doc = fitz.open(preview_pdf)
        self.assertEqual(sum(len(list(page.annots() or ()))
                             for page in preview_doc), 0)
        preview_doc.close()

        allowed_csv = self.root / "allowed.csv"
        allowed_pdf = self.root / "allowed.pdf"
        annotate_pdf(
            str(self.source), str(findings), str(allowed_pdf),
            csv_path=str(allowed_csv), allow_fragment=True, allow_fuzzy=True)
        allowed_rows = self._read_csv(allowed_csv)
        self.assertEqual([row["状态"] for row in allowed_rows], ["hit", "hit"])
        allowed_doc = fitz.open(allowed_pdf)
        self.assertEqual(sum(len(list(page.annots() or ()))
                             for page in allowed_doc), 2)
        allowed_doc.close()

    def test_dry_run_does_not_create_pdf(self):
        findings = self._write_findings([{
            "fix_class": "must_fix",
            "current": "alpha beta gamma delta",
            "suggested": "fixed text",
        }])
        output = self.root / "must-not-exist.pdf"
        csv_path = self.root / "dry-run.csv"
        result = annotate_pdf(
            str(self.source), str(findings), str(output),
            csv_path=str(csv_path), dry_run=True)
        self.assertEqual(result, str(csv_path))
        self.assertFalse(output.exists())
        self.assertEqual(self._read_csv(csv_path)[0]["状态"], "hit")

    def test_pdf2md_falls_back_per_page_without_losing_markdown_pages(self):
        chunks = [
            {
                "metadata": {"page": 1},
                "text": "alpha",
            },
            {
                "metadata": {"page": 2},
                "text": "## Preserved Heading\n\nrepeat phrase",
            },
        ]
        output = self.root / "source.md"
        captured = io.StringIO()
        with patch("pymupdf4llm.to_markdown", return_value=chunks):
            with redirect_stdout(captured):
                pdf2md(str(self.source), str(output))

        converted = output.read_text(encoding="utf-8")
        self.assertIn("gamma delta", converted)
        self.assertIn("unique fragment", converted)
        self.assertIn("## Preserved Heading", converted)
        self.assertIn("1(7.4%)", captured.getvalue())
        self.assertNotIn("2(", captured.getvalue())

    def test_source_hash_mismatch_refuses_writeback(self):
        findings = self._write_findings([{
            "fix_class": "must_fix",
            "current": "alpha beta",
            "suggested": "fixed",
        }])
        payload = json.loads(findings.read_text(encoding="utf-8"))
        payload["source_sha256"] = "0" * 64
        findings.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            annotate_pdf(
                str(self.source), str(findings),
                str(self.root / "must-not-exist.pdf"),
                csv_path=str(self.root / "must-not-exist.csv"))
        self.assertIn("SHA-256 不匹配", str(caught.exception))

    def test_findings_v1_requires_source_hash(self):
        findings = self._write_findings([{
            "fix_class": "must_fix",
            "current": "alpha beta",
            "suggested": "fixed",
        }], source_hash=False)
        with self.assertRaises(SystemExit) as caught:
            annotate_pdf(
                str(self.source), str(findings),
                str(self.root / "must-not-exist.pdf"),
                csv_path=str(self.root / "must-not-exist.csv"))
        self.assertIn("缺少 source_sha256", str(caught.exception))

    def test_output_paths_cannot_overwrite_inputs(self):
        original_pdf = self.source.read_bytes()
        findings = self._write_findings([{
            "fix_class": "must_fix",
            "current": "alpha beta",
            "suggested": "fixed",
        }])
        original_findings = findings.read_bytes()

        with self.assertRaisesRegex(SystemExit, "不能覆盖原 PDF"):
            pdf2md(str(self.source), str(self.source))
        with self.assertRaisesRegex(SystemExit, "CSV 输出路径不能覆盖"):
            annotate_pdf(
                str(self.source), str(findings),
                str(self.root / "review.pdf"),
                csv_path=str(self.source), dry_run=True)
        with self.assertRaisesRegex(SystemExit, "PDF 输出路径不能覆盖"):
            annotate_pdf(
                str(self.source), str(findings), str(findings),
                csv_path=str(self.root / "review.csv"))
        manifest = self.root / "review_source.json"
        extract_source(self.source, manifest)
        with self.assertRaisesRegex(SystemExit, "source manifest"):
            annotate_pdf(
                str(self.source), str(findings), str(manifest),
                csv_path=str(self.root / "manifest-review.csv"),
                source_manifest=str(manifest))

        self.assertEqual(self.source.read_bytes(), original_pdf)
        self.assertEqual(findings.read_bytes(), original_findings)

    def test_failed_pdf_audit_preserves_existing_output(self):
        findings = self._write_findings([{
            "fix_class": "must_fix",
            "current": "alpha beta",
            "suggested": "fixed",
        }])
        output = self.root / "existing.pdf"
        output.write_bytes(b"existing review")
        with patch(
                "src.pdf_pipeline._verify_annotations",
                side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                annotate_pdf(
                    str(self.source), str(findings), str(output),
                    csv_path=str(self.root / "existing.csv"))
        self.assertEqual(output.read_bytes(), b"existing review")

    def test_source_change_during_location_rejects_outputs(self):
        findings = self._write_findings([{
            "current": "alpha beta",
            "suggested": "fixed",
        }])
        output = self.root / "changed-source.pdf"
        csv_path = self.root / "changed-source.csv"
        original_builder = pdf_pipeline._build_text_index

        def mutate_source(document):
            result = original_builder(document)
            self.source.write_bytes(self.source.read_bytes() + b"\n% changed")
            return result

        with patch("src.pdf_pipeline._build_text_index",
                   side_effect=mutate_source):
            with self.assertRaisesRegex(SystemExit, "定位期间发生变化"):
                annotate_pdf(
                    str(self.source), str(findings), str(output),
                    csv_path=str(csv_path), dry_run=True)
        self.assertFalse(output.exists())
        self.assertFalse(csv_path.exists())

    def test_findings_v1_validates_schema_and_prefers_issues(self):
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        unsupported = self.root / "unsupported.json"
        unsupported.write_text(json.dumps({
            "schema": "ai-proofread.findings.v2",
            "issues": [],
        }), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "不支持的 findings schema"):
            annotate_pdf(
                str(self.source), str(unsupported),
                str(self.root / "unsupported.pdf"),
                csv_path=str(self.root / "unsupported.csv"))

        invalid = self.root / "invalid.json"
        invalid.write_text(json.dumps({
            "schema": "ai-proofread.findings.v1",
            "source_sha256": source_hash,
            "issues": [{
                "fix_class": "must_fix",
                "current": "alpha beta",
                "suggested": "fixed",
                "reason": "Missing category and page.",
            }],
        }), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "缺少字段: category"):
            annotate_pdf(
                str(self.source), str(invalid),
                str(self.root / "invalid.pdf"),
                csv_path=str(self.root / "invalid.csv"))

        missing_page = self.root / "missing-page.json"
        missing_page.write_text(json.dumps({
            "schema": "ai-proofread.findings.v1",
            "source_sha256": source_hash,
            "issues": [{
                "fix_class": "verify",
                "current": "alpha beta",
                "suggested": "",
                "reason": "Check.",
                "category": "fact",
            }],
        }), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "一基整数 page"):
            annotate_pdf(
                str(self.source), str(missing_page),
                str(self.root / "missing-page.pdf"),
                csv_path=str(self.root / "missing-page.csv"))

        both = self.root / "both.json"
        both.write_text(json.dumps({
            "schema": "ai-proofread.findings.v1",
            "source_sha256": source_hash,
            "issues": [{
                "fix_class": "verify",
                "current": "alpha beta",
                "suggested": "",
                "reason": "Canonical.",
                "category": "fact",
                "page": 1,
            }],
            "findings": [{"current": "repeat phrase"}],
        }), encoding="utf-8")
        csv_path = self.root / "both.csv"
        annotate_pdf(
            str(self.source), str(both), str(self.root / "both.pdf"),
            csv_path=str(csv_path), dry_run=True)
        rows = self._read_csv(csv_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["原文"], "alpha beta")

    def test_findings_v1_rejects_incomplete_evidence(self):
        findings = self._write_findings([{
            "current": "alpha beta",
            "evidence": [{
                "title": "Source",
                "url": "https://example.com/source",
            }],
        }])
        with self.assertRaisesRegex(SystemExit, "accessed_at"):
            annotate_pdf(
                str(self.source), str(findings),
                str(self.root / "evidence.pdf"),
                csv_path=str(self.root / "evidence.csv"))

    def test_source_manifest_binds_legacy_findings(self):
        manifest = self.root / "review_source.json"
        extract_source(self.source, manifest)
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({
            "llm": [{
                "original_sentence": "alpha beta",
                "corrected_sentence": "fixed",
                "fix_class": "must_fix",
            }],
        }), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "请传 --source-manifest"):
            annotate_pdf(
                str(self.source), str(legacy),
                str(self.root / "legacy-unbound.pdf"),
                csv_path=str(self.root / "legacy-unbound.csv"),
                dry_run=True)
        csv_path = self.root / "legacy.csv"
        annotate_pdf(
            str(self.source), str(legacy), str(self.root / "legacy.pdf"),
            csv_path=str(csv_path), dry_run=True,
            source_manifest=str(manifest))
        self.assertEqual(self._read_csv(csv_path)[0]["状态"], "hit")

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["source_sha256"] = "0" * 64
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "SHA-256 不匹配"):
            annotate_pdf(
                str(self.source), str(legacy),
                str(self.root / "legacy-mismatch.pdf"),
                csv_path=str(self.root / "legacy-mismatch.csv"),
                source_manifest=str(manifest))

    def test_tied_fuzzy_matches_stay_ambiguous_when_allowed(self):
        repeated = self.root / "repeated-fuzzy.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "fuzzy target")
        page.insert_text((72, 100), "fuzzy target")
        document.save(repeated)
        document.close()
        findings = self.root / "repeated-fuzzy.json"
        findings.write_text(json.dumps({
            "source_sha256": hashlib.sha256(repeated.read_bytes()).hexdigest(),
            "llm": [{
                "original_sentence": "fuzzy txrget",
                "corrected_sentence": "fuzzy target",
                "fix_class": "verify",
                "page": 1,
            }],
        }), encoding="utf-8")
        output = self.root / "repeated-fuzzy-review.pdf"
        csv_path = self.root / "repeated-fuzzy.csv"
        annotate_pdf(
            str(repeated), str(findings), str(output),
            csv_path=str(csv_path), allow_fuzzy=True)
        row = self._read_csv(csv_path)[0]
        self.assertEqual(row["状态"], "ambiguous")
        self.assertEqual(row["匹配方式"], "fuzzy")
        document = fitz.open(output)
        self.assertEqual(sum(len(list(page.annots() or ()))
                             for page in document), 0)
        document.close()

    def test_cross_page_exact_match_generates_page_local_quads(self):
        source = self.root / "cross-page.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "cross page")
        page = document.new_page()
        page.insert_text((72, 72), "continuation")
        document.save(source)
        document.close()
        findings = self.root / "cross-page.json"
        findings.write_text(json.dumps({
            "schema": "ai-proofread.findings.v1",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "issues": [{
                "fix_class": "verify",
                "current": "cross page continuation",
                "suggested": "",
                "reason": "Cross-page review.",
                "category": "layout",
                "page": 1,
            }],
        }), encoding="utf-8")
        output = self.root / "cross-page-review.pdf"
        csv_path = self.root / "cross-page.csv"
        annotate_pdf(
            str(source), str(findings), str(output),
            csv_path=str(csv_path), author="Codex")

        row = self._read_csv(csv_path)[0]
        self.assertEqual(row["状态"], "hit")
        self.assertEqual(row["匹配方式"], "crosspage")
        self.assertEqual(row["页码"], "1,2")
        self.assertEqual(row["quad数"], "2")
        document = fitz.open(output)
        self.assertEqual(
            [len(list(page.annots() or ())) for page in document], [1, 1])
        document.close()

    def test_pdf2md_falls_back_when_equal_length_text_is_missing(self):
        raw_page_one = "".join(
            page.get_text("text", sort=True) for page in fitz.open(self.source)
        ).split("repeat phrase", 1)[0]
        chunks = [
            {"metadata": {"page": 1}, "text": "x" * len(raw_page_one)},
            {"metadata": {"page": 2}, "text": "repeat phrase"},
        ]
        output = self.root / "equal-length.md"
        with patch("pymupdf4llm.to_markdown", return_value=chunks):
            pdf2md(str(self.source), str(output))
        converted = output.read_text(encoding="utf-8")
        self.assertIn("alpha beta", converted)
        self.assertIn("gamma delta", converted)

    def test_annot_class_maps_llm_findings_to_must_fix(self):
        # LLM JSON 发现不带 fix_class；有实质修改 → must_fix（对齐 DOCX 路径），
        # 无修改 → verify。
        self.assertEqual(
            pdf_pipeline._annot_class({
                "phase": "1_llm",
                "original": "尔茨海默病的安东尼",
                "suggestion": "阿尔茨海默病的安东尼",
            }), "must_fix")
        self.assertEqual(
            pdf_pipeline._annot_class({
                "phase": "1_llm",
                "original": "一样",
                "suggestion": "一样",
            }), "verify")
        # 其余阶段仍按 phase 前缀推断，不受影响。
        self.assertEqual(
            pdf_pipeline._annot_class({
                "phase": "0b_variant", "original": "子细", "suggestion": "仔细",
            }), "variant")
        self.assertEqual(
            pdf_pipeline._annot_class({
                "phase": "0a_tgscc", "char": "藉", "suggestion": "借",
            }), "tgscc")

    def test_normalize_pdf_markdown_joins_line_breaks_and_collapses_spaces(self):
        # 句中折行拼接 + 拉丁两侧空格收敛
        self.assertEqual(
            pdf_pipeline._normalize_pdf_markdown(
                "是帕伊萨\n\n（ paisa ）。这个词在哥伦比\n\n亚被用来指原籍。"),
            "是帕伊萨（paisa）。这个词在哥伦比亚被用来指原籍。")
        # 句末标点结尾 → 保留段落换行；数字两侧空格收敛
        self.assertEqual(
            pdf_pipeline._normalize_pdf_markdown(
                "不出 10 年就会死去。\n\n\n后来的几个月里，洛佩拉重返农场。"),
            "不出10年就会死去。\n\n后来的几个月里，洛佩拉重返农场。")
        # 结构行（分隔线）不参与拼接
        self.assertEqual(
            pdf_pipeline._normalize_pdf_markdown(
                "公派医生。\n\n\n~~~\n\n\n洛佩拉是帕伊萨。"),
            "公派医生。\n~~~\n\n洛佩拉是帕伊萨。")
        # 拉丁词内部空格保留（英文书名/术语）
        self.assertEqual(
            pdf_pipeline._normalize_pdf_markdown(
                "阿尔茨海默病 (Alzheimer disease) 是一种病。"),
            "阿尔茨海默病(Alzheimer disease)是一种病。")

    def test_diff_spans_returns_minimal_char_change(self):
        # 替换（单处 → 单 span）
        cur = "圣文森特医院是麦德林卓越的公立大学。"
        sug = "圣文森特医院是麦德林杰出的公立大学。"
        spans = pdf_pipeline._diff_spans(cur, sug)
        self.assertEqual(len(spans), 1)
        start, end, repl = spans[0]
        self.assertEqual(cur[start:end], "卓越")
        self.assertEqual(repl, "杰出")
        # 插入式改法（人 → 病人会，替换文本包含原文，不拆碎）
        cur, sug = "人在四十七八岁时发病。", "病人会在四十七八岁时发病。"
        spans = pdf_pipeline._diff_spans(cur, sug)
        self.assertEqual(len(spans), 1)
        start, end, repl = spans[0]
        self.assertEqual(cur[start:end], "人")
        self.assertEqual(repl, "病人会")
        # 纯插入锚定前一字符（自己 → 自己的）
        cur, sug = "构成属于自己灵魂的一切。", "构成属于自己的灵魂的一切。"
        spans = pdf_pipeline._diff_spans(cur, sug)
        self.assertEqual(len(spans), 1)
        start, end, repl = spans[0]
        self.assertEqual(cur[start:end], "己")
        self.assertEqual(repl, "己的")
        # 纯删除（repl 空）
        cur, sug = "构成属于自己灵魂的一切。", "构成自己灵魂的一切。"
        spans = pdf_pipeline._diff_spans(cur, sug)
        self.assertEqual(len(spans), 1)
        start, end, repl = spans[0]
        self.assertEqual(cur[start:end], "属于")
        self.assertEqual(repl, "")
        # 句首纯插入 → None（无锚点，回退整句）
        self.assertIsNone(
            pdf_pipeline._diff_spans("中的主人公。", "书中的主人公。"))
        # 整句重排/大改 → None
        self.assertIsNone(
            pdf_pipeline._diff_spans(
                "他的档案写着，病人逃出了医院。",
                "病人逃出了医院，他的档案写着。"))
        # 短句全替换（≥80%）→ None
        self.assertIsNone(pdf_pipeline._diff_spans("卓越", "杰出"))

    def test_diff_spans_splits_multiple_distant_edits(self):
        # 一句内两处分散改动（可以→能够、疾病→疾病的）→ 两个独立 span
        cur = ("到目前为止，我们还没有找到可以完全治愈阿尔茨海默病的方法，"
               "但已经能使用药物延缓整个疾病进程。")
        sug = ("到目前为止，我们还没有找到能够完全治愈阿尔茨海默病的方法，"
               "但已经能使用药物延缓整个疾病的进程。")
        spans = pdf_pipeline._diff_spans(cur, sug)
        self.assertEqual(len(spans), 2)
        self.assertEqual([cur[s:e] for s, e, _ in spans], ["可以", "病"])
        self.assertEqual([r for _, _, r in spans], ["能够", "病的"])
        # 相邻两处改动（载→录、，→、）→ 两个独立 span
        cur = "但是这一百多年，却记载了现代医学最漫长，也最艰难的探索。"
        sug = "但是这一百多年，却记录了现代医学最漫长、也最艰难的探索。"
        spans = pdf_pipeline._diff_spans(cur, sug)
        self.assertEqual(len(spans), 2)
        self.assertEqual([cur[s:e] for s, e, _ in spans], ["载", "，"])
        self.assertEqual([r for _, _, r in spans], ["录", "、"])

    def test_annotation_content_marks_pure_deletion(self):
        finding = {"category": "用词"}
        # 纯删除：corrected 空 → 显式标「（删除）」，不再无箭头
        content = pdf_pipeline._annotation_content(
            finding, "属于", "", "【必改】")
        self.assertEqual(
            content, "【必改】 属于\n→（删除）\n分类：用词")
        # 正常替换：显示 → 替换文本
        content = pdf_pipeline._annotation_content(
            finding, "卓越", "杰出", "【必改】")
        self.assertEqual(
            content, "【必改】 卓越\n→ 杰出\n分类：用词")


if __name__ == "__main__":
    unittest.main()
