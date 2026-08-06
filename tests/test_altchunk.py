"""Regression tests for altChunk (PDF→Word MHT-embedded) DOCX handling.

Covers the three consumers that must agree on P numbering:
  1. extract_source.extract_docx_units  (DOCX→MD path)
  2. max_pipeline._build_para_text_map  (finding → P resolution)
  3. writeback_engine                   (track changes + comments writeback)

A pure-altChunk DOCX has no standard w:p — all content lives in an embedded
MHT file. This mirrors Tencent Docs / PDF→Word exports.
"""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from src.cli import _docx_to_md
from src.extract_source import (
    W_NS,
    _decode_mht_html,
    docx_uses_altchunk_body,
    extract_altchunk_paragraphs,
    extract_docx_units,
    materialize_altchunk_paragraphs,
    sha256_file,
)
from src.max_pipeline import (
    _build_para_text_map,
    _findings_to_issues,
    _resolve_findings_to_p,
    _run_02_writeback,
)
from src.writeback_engine import audit_docx, build_para_map

W = f"{{{W_NS}}}"

# 顶层命名空间（关系 / 内容类型）
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# quoted-printable 编码的多段 MHT（真实腾讯文档导出的形态）。
# 字节：漩涡 = E6 BC A9 E6 B6 A1；错别字等用 QP 转义，中间含 =3D 转义的引号。
_MHT = """MIME-Version: 1.0
Content-Type: multipart/related;
    type="text/html";
    boundary="----=mhtDocumentPart"

------=mhtDocumentPart
Content-Type: text/html;
    charset="utf-8"
Content-Transfer-Encoding: quoted-printable
Content-Location: file:///C:/fake/document.html

<h1 id=3D"标题">第一章 测试标题</h1>
<p>这是一段测试文本，包含错别字=E9=80=9A=E9=A1=BA。</p>
<p>第二段用了不规范的=E6=BC=A9=E6=B6=A1。</p>
<h3 id=3D"小节">第一节 小节</h3>
<p>第三段正常。</p>
------=mhtDocumentPart--
"""


def _make_altchunk_docx(path: Path) -> None:
    """构建一个正文全部嵌在 altChunk MHT 里的 docx（无 w:p）。"""
    ct = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{CT_NS}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/afchunk.mht" ContentType="message/rfc822"/>
</Types>"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{REL_NS}">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="/word/document.xml" Id="rId1"/>
</Relationships>"""
    doc_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{REL_NS}">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
    Target="/word/afchunk.mht" Id="htmlChunk"/>
</Relationships>"""
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}"
  xmlns:r="{R_NS}">
  <w:body>
    <w:altChunk r:id="htmlChunk"/>
    <w:sectPr/>
  </w:body>
</w:document>"""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/afchunk.mht", _MHT)


def _resolve(docx: Path, findings):
    """把 findings 定位到 P 段落（用 max_pipeline 的定位逻辑）。"""
    return _resolve_findings_to_p(findings, _build_para_text_map(str(docx)))


class AltChunkExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docx = self.root / "alt.docx"
        _make_altchunk_docx(self.docx)

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_extracts_paragraphs_and_headings(self):
        with zipfile.ZipFile(self.docx) as z:
            paras = extract_altchunk_paragraphs(z)
        self.assertEqual(len(paras), 5)
        self.assertEqual(paras[0]["text"], "第一章 测试标题")
        self.assertEqual(paras[0]["heading_level"], 1)
        self.assertIn("通顺", paras[1]["text"])
        self.assertIn("漩涡", paras[2]["text"])
        self.assertEqual(paras[3]["heading_level"], 3)

    def test_docx_units_use_altchunk_paragraphs(self):
        units = extract_docx_units(self.docx)
        self.assertEqual(len(units), 5)
        self.assertEqual(units[0]["location"], "P0")
        self.assertEqual(units[0]["heading_level"], 1)
        self.assertIn("通顺", units[1]["text"])

    def test_docx_to_md_produces_full_text(self):
        md_path, md_text = _docx_to_md(self.docx)
        self.assertEqual(md_path.read_text(encoding="utf-8"), md_text)
        self.assertIn("# 第一章 测试标题", md_text)
        self.assertIn("第一节 小节", md_text)

    def test_p_map_matches_materialized_walk(self):
        """max_pipeline 的 P-map 必须与引擎物化后 build_para_map 逐段一致。"""
        text_map = _build_para_text_map(self.docx)

        with zipfile.ZipFile(self.docx) as z:
            doc_xml = etree.fromstring(z.read("word/document.xml"))
            body = doc_xml.find(f"{W}body")
            paras = extract_altchunk_paragraphs(z)
            materialize_altchunk_paragraphs(body, paras)
            _, engine_text_map = build_para_map(body)

        self.assertEqual(len(text_map), len(engine_text_map))
        for pn in text_map:
            self.assertEqual(text_map[pn], engine_text_map[pn], f"P{pn} 不一致")

    def test_finding_resolves_to_p(self):
        findings = [{
            "phase": "1_llm", "type": "correction",
            "original": "漩涡", "suggestion": "旋涡",
            "real_text": "漩涡", "severity": "warn", "confidence": 0.7,
        }]
        resolved = _resolve(self.docx, findings)
        self.assertEqual(resolved[0]["pn"], 2)

    def test_writeback_produces_tracked_changes_docx(self):
        """端到端：02 引擎在 altChunk docx 上产出可审计的修订+批注 DOCX。"""
        findings = [{
            "phase": "1_llm", "type": "correction",
            "original": "漩涡", "suggestion": "旋涡",
            "real_text": "漩涡", "severity": "warn", "confidence": 0.7,
        }]
        resolved = _resolve(self.docx, findings)
        self.assertEqual(resolved[0]["pn"], 2)
        issues = _findings_to_issues(resolved)

        out = self.root / "review.docx"
        path = _run_02_writeback(
            str(self.docx), "alt", issues, "AI审校", out_path=str(out),
            expected_source_sha256=sha256_file(str(self.docx)))
        self.assertTrue(Path(path).exists())
        audit_docx(str(path))

        # 修订版应含 w:del/w:ins，且原字已被替换（02 引擎做字符级 diff）
        with zipfile.ZipFile(path) as z:
            doc_xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("w:del", doc_xml)
        self.assertIn("w:ins", doc_xml)
        # 被删的"漩"与插入的"旋"都在（diff 后"涡"保留原样）
        self.assertIn(">漩<", doc_xml)
        self.assertIn(">旋<", doc_xml)
        # 原文"漩涡"不再是连续文本
        self.assertNotIn(">漩涡<", doc_xml)


class AltChunkHardeningTests(unittest.TestCase):
    """对抗性审查发现的边界情况回归。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _make_docx_with_body(self, body_xml: str, mht: str = _MHT) -> Path:
        """构造自定义 body 的 altChunk docx。"""
        path = self.root / "custom.docx"
        ct = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{CT_NS}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/afchunk.mht" ContentType="message/rfc822"/>
</Types>"""
        rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{REL_NS}">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="/word/document.xml" Id="rId1"/>
</Relationships>"""
        doc_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{REL_NS}">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
    Target="/word/afchunk.mht" Id="htmlChunk"/>
</Relationships>"""
        document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}">
  <w:body>{body_xml}<w:sectPr/></w:body>
</w:document>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", ct)
            z.writestr("_rels/.rels", rels)
            z.writestr("word/_rels/document.xml.rels", doc_rels)
            z.writestr("word/document.xml", document)
            z.writestr("word/afchunk.mht", mht)
        return path

    def test_empty_w_p_plus_altchunk_uses_shared_predicate(self):
        """空 <w:p/> 占位 + altChunk：extract 与 P-map 必须一致（不因空段退路）。"""
        path = self._make_docx_with_body('<w:p/>' + '<w:altChunk r:id="htmlChunk"/>')
        units = extract_docx_units(str(path))
        text_map = _build_para_text_map(str(path))
        self.assertEqual(len(units), 5)
        self.assertEqual(len(text_map), 5)
        for i in range(5):
            self.assertEqual(units[i]["text"], text_map[i], f"P{i} 不一致")
        # 共享判定应识别为 altChunk 文档
        with zipfile.ZipFile(path) as z:
            self.assertTrue(docx_uses_altchunk_body(z))

    def test_mixed_doc_falls_back_to_native_walk(self):
        """混合文档（真实 w:p + altChunk）：三消费方一致走标准 walk，altChunk 不物化。"""
        path = self._make_docx_with_body(
            '<w:p><w:r><w:t>原生段落甲</w:t></w:r></w:p>'
            '<w:altChunk r:id="htmlChunk"/>')
        units = extract_docx_units(str(path))
        text_map = _build_para_text_map(str(path))
        self.assertEqual([u["text"] for u in units], ["原生段落甲"])
        self.assertEqual(list(text_map.values()), ["原生段落甲"])
        with zipfile.ZipFile(path) as z:
            self.assertFalse(docx_uses_altchunk_body(z))  # 有真实文本 → 非 altChunk 文档

    def test_decode_base64_and_gbk(self):
        import base64
        html_body = '<p>这是base64编码内容</p>'
        b64 = base64.b64encode(html_body.encode("utf-8")).decode()
        mht_b64 = (f'MIME-Version: 1.0\nContent-Type: text/html; charset="utf-8"\n'
                   f'Content-Transfer-Encoding: base64\n\n{b64}\n')
        self.assertIn("base64编码", _decode_mht_html(mht_b64.encode("latin-1")))

        mht_gbk = ('Content-Type: text/html; charset="gbk"\n'
                   'Content-Transfer-Encoding: quoted-printable\n\n'
                   '<p>=D6=D0=CE=C4</p>\n')
        self.assertIn("中文", _decode_mht_html(mht_gbk.encode("latin-1")))

    def test_top_level_import_builds_text_map(self):
        """max_pipeline 顶层 import（脚本模式）也能建 P-map。"""
        import importlib
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        try:
            mp = importlib.import_module("max_pipeline")
            importlib.reload(mp)
            tm = mp._build_para_text_map(str(self._make_docx_with_body(
                '<w:altChunk r:id="htmlChunk"/>')))
            self.assertEqual(len(tm), 5)
        finally:
            sys.path.pop(0)

    def test_docx_without_rels_part_does_not_crash(self):
        """纯 Word docx 缺 word/_rels/document.xml.rels 不崩溃（BUG 修复）。"""
        path = self.root / "norels.docx"
        document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>正常段落</w:t></w:r></w:p><w:sectPr/></w:body></w:document>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", document)
        units = extract_docx_units(str(path))
        self.assertEqual([u["text"] for u in units], ["正常段落"])


if __name__ == "__main__":
    unittest.main()
