#!/usr/bin/env python3
"""Create location-stable review sources for Codex-native proofreading."""

from __future__ import annotations

import hashlib
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
    """Extract DOCX units with P numbers identical to the 02 writeback engine."""
    from lxml import etree

    with zipfile.ZipFile(path, "r") as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
        styles = _paragraph_styles(archive)

    body = root.find(f"{W}body")
    if body is None:
        return []

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
