#!/usr/bin/env python3
"""Create location-stable review sources for Codex-native proofreading."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Iterator


SOURCE_SCHEMA = "ai-proofread.source.v1"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file without loading it all into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── altChunk (内嵌 MHT/HTML) 支持 ────────────────────────────────────────
#
# PDF→Word 转换工具 / 在线协作编辑器（腾讯文档等）会把正文以 <w:altChunk>
# 形式嵌入，而不是标准 w:p 段落。整个管线（DOCX→MD、P 编号映射、02 引擎
# 回写）都必须从同一份 altChunk 段落列表取数，P 编号才能严格对齐。
# 注意：extract_source / max_pipeline / writeback_engine 三处的段落 walk 逻辑
# 是刻意重复的（见 CLAUDE.md P-numbering consistency），这里只共享 MHT 的
# *解析* 与 *段落物化*，不合并任何 walk 逻辑。


R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _read_altchunk_targets(archive: zipfile.ZipFile) -> list[bytes]:
    """从打开的 docx 里读取全部 <w:altChunk> 引用的内嵌文件原始字节。

    Returns:
        按 altChunk 在 body 中出现的顺序，返回各目标文件（.mht/.html）字节。
    """
    from lxml import etree

    try:
        rels_xml = archive.read("word/_rels/document.xml.rels")
    except KeyError:
        return []
    REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
    rel_root = etree.fromstring(rels_xml)
    rmap: dict[str, str] = {}
    for rel in rel_root.findall(f"{{{REL_NS}}}Relationship"):
        rid = rel.get("Id")
        tgt = rel.get("Target")
        if rid and tgt:
            rmap[rid] = tgt

    doc_xml = archive.read("word/document.xml")
    doc_root = etree.fromstring(doc_xml)
    out: list[bytes] = []
    for element in doc_root.iter():
        if element.tag.rsplit("}", 1)[-1] != "altChunk":
            continue
        rid = element.get(f"{{{R_NS}}}id")
        if not rid:
            continue
        target = rmap.get(rid, "")
        if not target:
            continue
        name = target.lstrip("/")
        if not name.startswith("word/"):
            name = f"word/{name}"
        try:
            out.append(archive.read(name))
        except KeyError:
            continue
    return out


def _decode_mht_html(raw: bytes) -> str:
    """把 altChunk 内嵌文件（MIME multipart MHT / HTML）解码成 HTML 字符串。

    步骤（严格按字节流处理，避免提前 UTF-8 解码损坏）：
      1. latin-1 解码（1:1 字节映射）便于字符串操作；
      2. 按 multipart 实际分隔线（"--"+声明 boundary）切分，取含 text/html 的段；
      3. 以第一个 "<" 标签作为正文起点（MIME 头与分隔线不含 "<"）；
      4. 按 Content-Transfer-Encoding 解码（quoted-printable / base64）→ UTF-8。

    兼容纯 HTML（无 MIME 头）直接返回。
    """
    text = raw.decode("latin-1")
    low = text.lower()
    if "content-type:" not in low and "content-transfer-encoding:" not in low:
        return raw.decode("utf-8", errors="replace")

    body = text
    transfer_encoding = "quoted-printable"  # 默认
    charset = "utf-8"
    cm = re.search(r'Content-Type:\s*text/html[^;]*;\s*charset="?([^"\s;\r\n]+)"?',
                   text, re.IGNORECASE)
    if cm:
        charset = cm.group(1).strip().strip('"')
    em = re.search(r'Content-Transfer-Encoding:\s*([a-zA-Z0-9-]+)', text, re.IGNORECASE)
    if em:
        transfer_encoding = em.group(1).lower()

    bm = re.search(r'boundary="?([^"\r\n]+)"?', text, re.IGNORECASE)
    if bm:
        delim = "--" + bm.group(1).strip('"')
        for part in text.split(delim):
            # 真正的 HTML 段有 Content-Type: text/html 段头（顶层头只写 type="text/html"）
            if re.search(r'Content-Type:\s*text/html', part, re.IGNORECASE):
                body = part
                # 段内可能覆写 charset / transfer-encoding
                c2 = re.search(
                    r'Content-Type:\s*text/html[^;]*;\s*charset="?([^"\s;\r\n]+)"?',
                    part, re.IGNORECASE)
                if c2:
                    charset = c2.group(1).strip().strip('"')
                e2 = re.search(
                    r'Content-Transfer-Encoding:\s*([a-zA-Z0-9-]+)', part, re.IGNORECASE)
                if e2:
                    transfer_encoding = e2.group(1).lower()
                break

    # 先用空行切出 MIME payload（base64 的 payload 无 "<"，此步必须先做）
    header_split = body.find("\n\n")
    if header_split >= 0:
        body = body[header_split + 2:]

    try:
        raw_bytes = body.encode("latin-1")
        if transfer_encoding == "base64":
            import base64
            raw_bytes = base64.b64decode(re.sub(r"\s+", "", body))
        elif transfer_encoding in ("quoted-printable", "qp"):
            import quopri
            raw_bytes = quopri.decodestring(raw_bytes)
        # 按声明的 charset 解码（GBK/GB2312/Big5/UTF-8）
        try:
            return raw_bytes.decode(charset, errors="replace")
        except LookupError:
            return raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        # 回退：取第一个 "<" 之后的原始文本
        tag_pos = body.find("<")
        return body[tag_pos:] if tag_pos >= 0 else body


def _html_to_paragraphs(html_text: str) -> list[dict[str, Any]]:
    """把 HTML 正文解析为有序段落 [{text, heading_level}]。

    标题 h1-h6 → heading_level 1-6（供 Markdown `#` 前缀）；<p>/<div>/<br>
    作为段落分界。跳过纯空段落。
    """
    lines: list[dict[str, Any]] = []
    # 按标题与块级元素切开
    pieces = re.split(
        r"(<h[1-6][^>]*>.*?</h[1-6]>)",
        html_text, flags=re.DOTALL | re.IGNORECASE)
    for piece in pieces:
        m = re.match(r"<h([1-6])[^>]*>(.*?)</h\1>", piece,
                     flags=re.DOTALL | re.IGNORECASE)
        if m:
            level = int(m.group(1))
            body = m.group(2)
        else:
            level = None
            body = piece

        if level is None:
            # 块级分界 → 段落（含未闭合的 <p>/<div> 开标签，增强容错）
            body = re.sub(r"<p[^>]*>", "\n", body, flags=re.IGNORECASE)
            body = re.sub(r"<div[^>]*>", "\n", body, flags=re.IGNORECASE)
            body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
            body = re.sub(r"</p>", "\n", body, flags=re.IGNORECASE)
            body = re.sub(r"</div>", "\n", body, flags=re.IGNORECASE)

        body = re.sub(r"<script[^>]*>.*?</script>", "", body,
                      flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style[^>]*>.*?</style>", "", body,
                      flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", "", body)
        body = html.unescape(body)  # 完整 HTML 实体解码
        body = body.replace("&nbsp;", " ").replace("​", "")

        for para in body.split("\n"):
            text = " ".join(para.split())
            if not text:
                continue
            if level is not None:
                lines.append({"text": text, "heading_level": level})
            else:
                lines.append({"text": text, "heading_level": None})
    return lines


def extract_altchunk_paragraphs(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    """从 altChunk 格式的 docx 提取有序段落 [{text, heading_level}]。

    无 altChunk 或解析失败时返回 []。这是三处消费方（extract_docx_units、
    max_pipeline._build_para_text_map、writeback_engine）共享的唯一事实来源。
    """
    targets = _read_altchunk_targets(archive)
    if not targets:
        return []

    paragraphs: list[dict[str, Any]] = []
    for raw in targets:
        html_text = _decode_mht_html(raw)
        paragraphs.extend(_html_to_paragraphs(html_text))
    return paragraphs


def docx_uses_altchunk_body(archive: zipfile.ZipFile) -> bool:
    """判定 docx 正文是否完全由 altChunk（内嵌 MHT/HTML）承载。

    判定规则（三处消费方共用，保证 P 编号一致）：
      - body 存在至少一个 <w:altChunk>，且
      - 没有**有文本承载**的 w:p 或 w:tbl 段落（空段落 <w:p/> 不算数）。

    这样：
      - 纯 altChunk 文档（含带空占位 <w:p/> 的）→ True，走 altChunk 分支；
      - 混合文档（altChunk + 真实文本段落）→ False，三消费方一致走标准 walk；
      - 纯 Word 文档（无 altChunk）→ False，不受影响。
    """
    from lxml import etree

    doc_xml = archive.read("word/document.xml")
    root = etree.fromstring(doc_xml)
    body = root.find(f"{W}body")
    if body is None:
        return False

    has_altchunk = False
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "altChunk":
            has_altchunk = True
        elif tag == "p":
            if _accepted_paragraph_text(child).strip():
                return False  # 有真实文本段落
        elif tag == "tbl":
            for row_p in child.findall(f".//{W}p"):
                if _accepted_paragraph_text(row_p).strip():
                    return False  # 有真实表格文本
    return has_altchunk


def materialize_altchunk_paragraphs(body: Any, paragraphs: list[dict[str, Any]]) -> int:
    """把 altChunk 段落物化为标准 w:p/w:r/w:t 元素，追加进 body（sectPr 之前）。

    返回写入的段落数。调用方需自行 walk body 生成 P 编号——由于物化的 w:p
    顺序与 extract_altchunk_paragraphs 列表一致，walk 出来的 P 编号三处一致。
    """
    from lxml import etree

    # 移除既有 altChunk 元素（保留 sectPr）
    for ac in body.findall(f"{W}altChunk"):
        body.remove(ac)

    for para in paragraphs:
        p = etree.SubElement(body, f"{W}p")
        r = etree.SubElement(p, f"{W}r")
        t = etree.SubElement(r, f"{W}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = para["text"]

    # 把 sectPr 移到 body 末尾
    sect_prs = body.findall(f"{W}sectPr")
    for sp in sect_prs:
        body.remove(sp)
    if sect_prs:
        body.append(sect_prs[-1])

    return len(paragraphs)


def _accepted_paragraph_text(paragraph: Any) -> str:
    """Return accepted-view text using the writeback engine's exact rules."""
    parts: list[str] = []
    for text_node in paragraph.iter(f"{W}t"):
        if any(ancestor.tag == f"{W}del" for ancestor in text_node.iterancestors()):
            continue
        parts.append(text_node.text or "")
    return "".join(parts)


def _paragraph_styles(archive: zipfile.ZipFile) -> dict[str, dict[str, Any]]:
    """Read paragraph style names and outline levels from styles.xml."""
    from lxml import etree

    try:
        root = etree.fromstring(archive.read("word/styles.xml"))
    except KeyError:
        return {}

    styles: dict[str, dict[str, Any]] = {}
    for style in root.findall(f"{W}style"):
        if style.get(f"{W}type") != "paragraph":
            continue
        style_id = style.get(f"{W}styleId")
        if not style_id:
            continue
        name_node = style.find(f"{W}name")
        outline_node = style.find(f"{W}pPr/{W}outlineLvl")
        outline = outline_node.get(f"{W}val") if outline_node is not None else None
        styles[style_id] = {
            "name": name_node.get(f"{W}val") if name_node is not None else style_id,
            "outline_level": int(outline) if outline and outline.isdigit() else None,
        }
    return styles


def _heading_level(paragraph: Any, styles: dict[str, dict[str, Any]]) -> tuple[str | None, int | None]:
    p_style = paragraph.find(f"{W}pPr/{W}pStyle")
    if p_style is None:
        return None, None
    style_id = p_style.get(f"{W}val")
    style = styles.get(style_id or "", {})
    style_name = style.get("name") or style_id
    match = re.search(r"(?:heading|标题)\s*([1-9])", style_name or "", re.IGNORECASE)
    if match:
        return style_name, int(match.group(1))
    outline = style.get("outline_level")
    if isinstance(outline, int) and 0 <= outline <= 8:
        return style_name, outline + 1
    return style_name, None


def _body_paragraphs(body: Any) -> Iterator[tuple[Any, str]]:
    """Yield non-empty body and table paragraphs in writeback P-number order."""
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            if _accepted_paragraph_text(child).strip():
                yield child, "paragraph"
        elif tag == "tbl":
            for paragraph in child.findall(f".//{W}p"):
                if _accepted_paragraph_text(paragraph).strip():
                    yield paragraph, "table_cell"


def extract_docx_units(path: str | Path) -> list[dict[str, Any]]:
    """Extract DOCX units with P numbers identical to the 02 writeback engine.

    altChunk 格式（PDF→Word 导出等）正文嵌在 MHT 里、无标准 w:p，此时
    用 extract_altchunk_paragraphs 提取段落，P 编号与引擎物化后一致。
    """
    from lxml import etree

    with zipfile.ZipFile(path, "r") as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
        styles = _paragraph_styles(archive)
        body = root.find(f"{W}body")
        if body is None:
            return []
        # 纯 altChunk 文档（正文完全由内嵌 MHT 承载）→ 共享解析器，
        # 保留 heading_level 供 MD 标题。P 编号与引擎物化后一致。
        if docx_uses_altchunk_body(archive):
            altchunk_paras = extract_altchunk_paragraphs(archive)
            if altchunk_paras:
                return [
                    {
                        "location": f"P{index}",
                        "kind": "paragraph",
                        "text": para["text"],
                        **({"heading_level": para["heading_level"]}
                           if para.get("heading_level") else {}),
                    }
                    for index, para in enumerate(altchunk_paras)
                ]

    units: list[dict[str, Any]] = []
    for index, (paragraph, kind) in enumerate(_body_paragraphs(body)):
        style_name, heading_level = _heading_level(paragraph, styles)
        unit: dict[str, Any] = {
            "location": f"P{index}",
            "kind": kind,
            "text": _accepted_paragraph_text(paragraph),
        }
        if style_name:
            unit["style"] = style_name
        if heading_level:
            unit["heading_level"] = heading_level
        units.append(unit)
    return units


def extract_pdf_units(path: str | Path) -> list[dict[str, Any]]:
    """Extract one review unit per PDF page, including pages without a text layer."""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("缺少 PyMuPDF：python3 -m pip install 'PyMuPDF>=1.27'") from exc

    units: list[dict[str, Any]] = []
    with fitz.open(str(path)) as document:
        for page_index, page in enumerate(document):
            text = page.get_text("text", sort=True).strip()
            units.append({
                "page": page_index + 1,
                "kind": "page",
                "has_text": bool(text),
                "text": text,
            })
    return units


def build_review_source(path: str | Path) -> dict[str, Any]:
    """Build an ``ai-proofread.source.v1`` payload for DOCX or PDF."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"文件不存在: {source}")

    digest_before = sha256_file(source)
    source_type = source.suffix.lower().lstrip(".")
    if source_type == "docx":
        units = extract_docx_units(source)
    elif source_type == "pdf":
        units = extract_pdf_units(source)
        if units and not any(unit["has_text"] for unit in units):
            raise RuntimeError(f"PDF 无文字层，需先 OCR: {source}")
    else:
        raise ValueError(f"不支持的格式: .{source_type}（仅支持 .docx 和 .pdf）")

    digest_after = sha256_file(source)
    if digest_before != digest_after:
        raise RuntimeError("提取期间源文件发生变化，请重新运行 extract")
    return {
        "schema": SOURCE_SCHEMA,
        "source_path": str(source.resolve()),
        "source_name": source.name,
        "source_type": source_type,
        "source_sha256": digest_after,
        "size_bytes": source.stat().st_size,
        "unit_count": len(units),
        "units": units,
    }


def extract_source(path: str | Path, out_path: str | Path) -> dict[str, Any]:
    """Build and write a review-source JSON file."""
    source = Path(path).expanduser().resolve()
    destination = Path(out_path).expanduser()
    if destination.resolve() == source:
        raise ValueError("输出路径不能覆盖源文件")
    payload = build_review_source(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return payload
