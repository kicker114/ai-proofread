#!/usr/bin/env python3
"""PDF 审校工具链：PDF→Markdown 转换 + 审校发现高亮批注回写。

WorkBuddy/Claude Code 通用入口，打通「图文混排 PDF → 审校 → 原 PDF 高亮批注」全链路：

    python3 src/pdf_pipeline.py pdf2md   <input.pdf> [--out <output.md>]
    python3 src/pdf_pipeline.py annotate <input.pdf> <findings.json>
             [--source-manifest <review_source.json>] [--out <annotated.pdf>]
             [--author 审校助手] [--dry-run] [--csv <path>]

annotate 读取 max 流水线输出的 findings JSON（`{doc}_max_results.json`，
形如 {"llm": [{original_sentence, corrected_sentence, fix_class, ...}], ...}），
在**原 PDF** 上逐条定位原文并添加高亮注释（Highlight annotation + 弹窗内容），
输出批注版 PDF（默认 `{stem}_审阅版.pdf`）+ 批注清单 CSV。

定位策略（安全优先的三级降级）：
  1. rawdict 字符坐标精确匹配（跨行/跨页生成逐行 quads）
  2. 最长连续片段降级（默认只预览，需 --allow-fragment 才写入）
  3. rapidfuzz 模糊匹配（默认只预览，需 --allow-fuzzy 才写入）

重复精确命中必须用 finding 的一基页码 `page` 唯一消歧，否则标记 ambiguous，
不写入批注。严格校验 `ai-proofread.findings.v1`；legacy max findings 可用
`--source-manifest` 绑定源 PDF 哈希。

依赖：pymupdf（系统 Python 需 /usr/local/bin/python3 或安装 pymupdf）、rapidfuzz、pymupdf4llm。
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, OrderedDict
from difflib import SequenceMatcher
from pathlib import Path

# ── 工具 1：PDF → Markdown ────────────────────────────────────────────


def _normalized_char_count(text: str) -> int:
    """用于页级覆盖率比较的非空白字符数。"""
    return len(re.sub(r"\s+", "", text))


def _raw_text_coverage(raw_text: str, markdown_text: str) -> float:
    """Estimate how much raw page text survives in Markdown, ignoring spaces."""
    raw = re.sub(r"\s+", "", raw_text)
    markdown = re.sub(r"\s+", "", markdown_text)
    if not raw:
        return 1.0
    matcher = SequenceMatcher(None, raw, markdown, autojunk=False)
    retained = sum(block.size for block in matcher.get_matching_blocks())
    return retained / len(raw)


# 句末标点：行尾若是这些标点，视为自然段落边界，不参与折行拼接。
_SENTENCE_END = set("。！？…；：」』】\"'”’")
# 零宽字符：PDF 文字层常夹带，既不可见又会在去空白匹配时造成「索引有、提取无」
# 的不一致，故索引与提取两侧统一剔除。
_ZERO_WIDTH = set("​‌‍﻿")
# 结构性行前缀：标题 / 分隔线 / 表格 / 引用 / 列表，不参与折行拼接。
_STRUCT_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}|\*{3,}|-{3,}|_{3,}|~{3,}|\|+|>|[-\*+]\s|\d+[.、．])"
)


def _is_structural_line(line: str) -> bool:
    """判断是否结构性行（标题/分隔/表格/引用/列表）。"""
    return bool(_STRUCT_LINE_RE.match(line))


def _normalize_pdf_markdown(text: str) -> str:
    """把 PDF 提取的 markdown 折行/空格碎片归一化，供 LLM 审校前喂入。

    PDF 文字层把正文逐行切块（`病\\n人`），pymupdf4llm 又把每条视觉行变成
    markdown ``\\n\\n`` 段落、并在拉丁文两侧塞空格（`（ paisa ）`）。LLM 会把
    这些排版碎片误当错误去「改」，产生假阳性。这里**只删空白/换行、不改任何
    非空白字符**，因此 annotate 的「去空白精确匹配」不受影响。

    规则：
      - 折行拼接：忽略空行，把相邻非空行「上一行非句末标点结尾、且两行皆非
        结构行」时直接接成一句；句末标点结尾或结构行则保留为段落换行。
      - 空格收敛：去掉与 CJK 字符/CJK 标点相邻的空格；拉丁词内部空格保留。
    """
    # 1) 折行拼接：空行不视为段落边界（pymupdf4llm 每行之间都塞空行）。
    lines = [ln.strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if _is_structural_line(ln):
            out.append(ln)
            out.append("")
            continue
        if out and out[-1] != "" and out[-1][-1] not in _SENTENCE_END:
            out[-1] += ln  # 直接拼接，不补空格
            continue
        if out and out[-1] != "":
            out.append("")
        out.append(ln)
    joined = "\n".join(out)
    # 2) 空格收敛：去掉与 CJK 字符/CJK 标点相邻的横向空格（保留换行）。
    cjk = r"一-鿿　-〿＀-￯"
    joined = re.sub(rf"(?<=[{cjk}])[ \t　]+|[ \t　]+(?=[{cjk}])", "", joined)
    # 收尾：折叠连续空行，去首尾空白。
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()


def pdf2md(pdf_path: str, out_path: str | None = None,
           coverage_threshold: float = 0.75) -> str:
    """用 pymupdf4llm 把图文混排 PDF 转成 Markdown（标题/段落/表格保留）。

    逐页比较 pymupdf4llm 与 PyMuPDF raw text 的非空白字符覆盖率。
    raw 有正文但 Markdown 覆盖率低于阈值时，该页自动降级为 raw text，
    避免复杂图文版式静默漏掉正文。无文字层页面仍统计提示并跳过。
    """
    try:
        import pymupdf4llm
    except ImportError:
        sys.exit("缺少依赖：pip install pymupdf4llm")
    try:
        import fitz
    except ImportError:
        sys.exit("缺少依赖：pip install pymupdf")

    src = Path(pdf_path)
    if not src.exists():
        sys.exit(f"PDF 不存在: {src}")
    destination = Path(out_path) if out_path is not None else src.with_suffix(".md")
    if destination.resolve() == src.resolve():
        sys.exit("输出路径不能覆盖原 PDF")

    # 统计无文字层页面，并保留逐页 raw text 作为覆盖率基准与降级来源。
    probe = fitz.open(str(src))
    raw_pages = [page.get_text("text", sort=True) for page in probe]
    empty_pages = [i + 1 for i, text in enumerate(raw_pages)
                   if not text.strip()]
    probe.close()
    if empty_pages:
        shown = ", ".join(str(p) for p in empty_pages[:20])
        more = "..." if len(empty_pages) > 20 else ""
        print(f"⚠️  无文字层页面 {len(empty_pages)} 页（插图/章扉页，跳过）: {shown}{more}")

    chunks = pymupdf4llm.to_markdown(
        str(src), page_chunks=True, show_progress=False)
    markdown_by_page: dict[int, str] = {}
    if isinstance(chunks, list):
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            metadata = chunk.get("metadata")
            page_number = metadata.get("page") if isinstance(metadata, dict) else None
            if not isinstance(page_number, int) or not 1 <= page_number <= len(raw_pages):
                page_number = index + 1
            markdown_by_page[page_number] = str(chunk.get("text", ""))

    selected_pages = []
    fallback_pages: list[tuple[int, float]] = []
    for page_number, raw_text in enumerate(raw_pages, 1):
        markdown_text = markdown_by_page.get(page_number, "")
        raw_count = _normalized_char_count(raw_text)
        coverage = _raw_text_coverage(raw_text, markdown_text)
        if raw_count and coverage < coverage_threshold:
            selected_pages.append(raw_text.strip())
            fallback_pages.append((page_number, coverage))
        else:
            selected_pages.append(markdown_text.strip())

    md_text = "\n\n".join(text for text in selected_pages if text).strip() + "\n"
    if not md_text.strip():
        sys.exit(f"PDF 无可提取文本（可能全为扫描图，需先 OCR）: {src}")

    # 归一化折行/空格碎片（只删空白，不改非空白字符），避免 LLM 把排版
    # 换行误当校对错误。须在写盘前执行，使下游 max 管线读到连续正文。
    md_text = _normalize_pdf_markdown(md_text)

    if fallback_pages:
        shown = ", ".join(
            f"{page}({coverage:.1%})"
            for page, coverage in fallback_pages[:20]
        )
        more = "..." if len(fallback_pages) > 20 else ""
        print(
            f"⚠️  Markdown 页级覆盖不足，{len(fallback_pages)} 页改用 raw text "
            f"（阈值 {coverage_threshold:.0%}）: {shown}{more}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(md_text, encoding="utf-8")
    print(f"✅ PDF→MD: {destination} ({len(md_text)} 字符)")
    return str(destination)


# ── 工具 2：findings → 原 PDF 高亮批注 ────────────────────────────────


# 各阶段 fix_class → 高亮颜色（RGB 0-1）与批注标签
_FIX_STYLE = {
    "must_fix": ((1.0, 0.75, 0.25), "【必改】"),   # 橙黄
    "polish":   ((1.0, 0.92, 0.5), "【润色】"),   # 淡黄
    "verify":   ((0.7, 0.85, 1.0), "【待核】"),   # 淡蓝
    "tgscc":    ((1.0, 0.7, 0.7), "【汉字规范】"), # 淡红
    "variant":  ((1.0, 0.85, 0.4), "【词形】"),
    "structure":((0.8, 0.9, 1.0), "【结构】"),
    "names":    ((0.85, 0.95, 0.8), "【专名】"),
}

# 定位状态枚举
HIT = "hit"
SKIP = "skip"
PREVIEW = "preview"
AMBIGUOUS = "ambiguous"


def _flatten_findings(data: dict | list) -> list[dict]:
    """把 max_results.json（按阶段分组的 dict）拍平为 findings 列表。

    兼容两种形态：
      - {"llm": [...], "tgscc": [...], ...}
      - [ {...}, ... ]（已拍平）
    """
    if isinstance(data, list):
        return [dict(f) for f in data if isinstance(f, dict)]
    if not isinstance(data, dict):
        return []
    if data.get("schema") == "ai-proofread.findings.v1":
        canonical = data.get("issues") if "issues" in data else data.get("findings")
        return [dict(f) for f in canonical if isinstance(f, dict)] \
            if isinstance(canonical, list) else []
    if isinstance(data.get("findings"), list):
        return [dict(f) for f in data["findings"] if isinstance(f, dict)]
    if isinstance(data.get("issues"), list):
        return [dict(f) for f in data["issues"] if isinstance(f, dict)]
    out = []
    for key, batch in data.items():
        if isinstance(batch, list):
            for f in batch:
                if isinstance(f, dict):
                    item = dict(f)
                    item.setdefault("phase", key)
                    out.append(item)
    return out


def _extract_original(f: dict) -> str:
    """从一条 finding 提取原文片段（用于 PDF 定位）。"""
    for k in ("current", "original_sentence", "original", "char", "wrong", "text"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _extract_corrected(f: dict) -> str:
    """从一条 finding 提取修正建议（用于批注弹窗）。"""
    for k in ("suggested", "corrected_sentence", "suggestion", "correct", "replace", "message"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _annot_class(f: dict) -> str:
    """确定批注样式类：must_fix / polish / verify / tgscc / variant / structure / names。"""
    fc = f.get("fix_class", "")
    if fc in _FIX_STYLE:
        return fc
    phase = str(f.get("phase", ""))
    if "tgscc" in phase:
        return "tgscc"
    if "variant" in phase:
        return "variant"
    if "structure" in phase:
        return "structure"
    if "names" in phase:
        return "names"
    if "1_llm" in phase:
        # LLM JSON 发现不带 fix_class；原文≠建议即有实质修改 → must_fix，
        # 与 DOCX 路径 _findings_to_issues 的 must_fix 路由保持一致。
        return "must_fix" if _extract_original(f) != _extract_corrected(f) else "verify"
    return "verify"


def _longest_pure_fragment(text: str, min_len: int = 6) -> list[str]:
    """把句子切成不含标点/空白的连续片段，从长到短返回（用于跨行定位降级）。

    例：'它总是道\n德哲学的分支' → ['德哲学的分支', '它总是道']（去空白后）。
    """
    frags = re.split(r"[\s，。！？、；：,.!?;:（）()「」『』《》〈〉\"'“”‘’—…·\-—\d]+", text)
    frags = [f for f in frags if len(f) >= min_len]
    frags.sort(key=len, reverse=True)
    return frags


# ── 文本索引（跨页 + 跨行定位的核心）────────────────────────────────


def _build_text_index(doc) -> tuple[str, list[tuple[int, int, "fitz.Quad"]]]:
    """构建字符级全文索引。

    Returns:
        full_text: 全书去空白后的拼接文本（保持字符顺序）
        full_locs: 与 full_text 逐字符对应的 (page_index, line_id, char_quad)。
                   char_quad 来自 get_text("rawdict")，可把任意子串精确
                   映射为逐行 quads，避免多行/双栏被合成一个大矩形。

    注意：PyMuPDF 1.27 下必须复用 page 对象（内联 doc[pno] 多次调用
    会丢失 annot↔page 绑定），本函数只读不写，无此风险。
    """
    import fitz  # PyMuPDF（类型标注用）
    full_chars: list[str] = []
    full_locs: list[tuple[int, int, "fitz.Quad"]] = []
    line_id = 0
    for pno in range(len(doc)):
        page = doc[pno]
        d = page.get_text("rawdict", sort=False)
        for block in d.get("blocks", []):
            if block.get("type") != 0:  # 非文本块（图片等）跳过
                continue
            for line in block.get("lines", []):
                current_line_id = line_id
                line_id += 1
                line_dir = tuple(line.get("dir", (1.0, 0.0)))
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        value = char.get("c", "")
                        if not value or not value.strip() or value in _ZERO_WIDTH:
                            continue
                        try:
                            quad = fitz.recover_char_quad(line_dir, span, char)
                        except (KeyError, TypeError, ValueError):
                            quad = fitz.Rect(char["bbox"]).quad
                        full_chars.append(value)
                        full_locs.append((pno, current_line_id, quad))
    return "".join(full_chars), full_locs


def _quads_from_span(full_locs, start: int,
                     length: int) -> list[tuple[int, list["fitz.Quad"]]]:
    """把索引区间映射为按页分组的逐行 quads。

    Returns:
        [(page_index, [line_quad, ...]), ...]
    """
    import fitz  # PyMuPDF（类型标注用）
    seg = full_locs[start:start + length]
    by_line: OrderedDict[tuple[int, int], "fitz.Rect"] = OrderedDict()
    for pno, line_id, quad in seg:
        key = (pno, line_id)
        if key not in by_line:
            by_line[key] = fitz.Rect(quad.rect)
        else:
            by_line[key] |= quad.rect
    by_page: OrderedDict[int, list["fitz.Quad"]] = OrderedDict()
    for (pno, _), rect in by_line.items():
        by_page.setdefault(pno, []).append(rect.quad)
    out = list(by_page.items())
    return out


def _find_all(text: str, needle: str) -> list[int]:
    """返回 needle 的全部（含重叠）起点。"""
    starts = []
    offset = 0
    while needle and offset <= len(text) - len(needle):
        idx = text.find(needle, offset)
        if idx < 0:
            break
        starts.append(idx)
        offset = idx + 1
    return starts


def _candidate_pages(page_quads) -> list[int]:
    """返回去重后的一基候选页码。"""
    return sorted({pno + 1 for pno, _ in page_quads})


def _locate_exact(full_text: str, full_locs, norm_s: str,
                  page_hint: int | None = None) -> dict | None:
    """枚举全部精确命中，并用一基 page_hint 唯一消歧。

    返回匹配字典；重复或页码提示冲突时返回 status=ambiguous，绝不
    静默选择第一处。
    """
    starts = _find_all(full_text, norm_s)
    if not starts:
        return None
    candidates = [
        (start, _quads_from_span(full_locs, start, len(norm_s)))
        for start in starts
    ]
    all_pages = sorted({
        page
        for _, page_quads in candidates
        for page in _candidate_pages(page_quads)
    })
    if page_hint is not None:
        candidates = [
            item for item in candidates
            if page_hint in _candidate_pages(item[1])
        ]
        if not candidates:
            return {
                "status": AMBIGUOUS,
                "method": "exact",
                "page_quads": [],
                "candidate_pages": all_pages,
                "score": 100.0,
                "reason": f"page={page_hint} 与精确命中页不一致",
            }
    if len(candidates) != 1:
        pages = sorted({
            page
            for _, page_quads in candidates
            for page in _candidate_pages(page_quads)
        })
        return {
            "status": AMBIGUOUS,
            "method": "exact",
            "page_quads": [],
            "candidate_pages": pages or all_pages,
            "score": 100.0,
            "reason": f"精确命中 {len(candidates)} 处，需用 page 唯一消歧",
        }
    start, page_quads = candidates[0]
    method = "crosspage" if len(page_quads) > 1 else "exact"
    return {
        "status": HIT,
        "method": method,
        "start": start,
        "length": len(norm_s),
        "page_quads": page_quads,
        "candidate_pages": _candidate_pages(page_quads),
        "score": 100.0,
        "reason": "",
    }


def _locate_fuzzy(full_text: str, full_locs, norm_s: str,
                  page_hint: int | None = None,
                  score_cutoff: float = 85.0) -> dict | None:
    """rapidfuzz 模糊定位（回退层）。

    仅在 len(norm_s) >= 4 时使用；partial_ratio_alignment 返回真实分数，
    并对候选首尾做 partial_ratio 校验，降低误匹配。命中标注 'fuzzy'。
    """
    if len(norm_s) < 4 or len(full_text) < len(norm_s):
        return None
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None

    search_start = 0
    search_end = len(full_text)
    if page_hint is not None:
        indices = [
            idx for idx, (pno, _, _) in enumerate(full_locs)
            if pno == page_hint - 1
        ]
        if not indices:
            return None
        search_start = indices[0]
        search_end = indices[-1] + 1
    haystack = full_text[search_start:search_end]

    def candidate(segment: str, offset: int) -> dict | None:
        alignment = fuzz.partial_ratio_alignment(
            norm_s, segment, score_cutoff=score_cutoff)
        if alignment is None:
            return None
        length = alignment.dest_end - alignment.dest_start
        if length < 2:
            return None
        start = search_start + offset + alignment.dest_start
        matched = full_text[start:start + length]
        head_n = min(4, len(norm_s))
        tail_n = min(4, len(norm_s))
        if fuzz.partial_ratio(norm_s[:head_n], matched[:head_n]) < 80:
            return None
        if fuzz.partial_ratio(norm_s[-tail_n:], matched[-tail_n:]) < 80:
            return None
        return {
            "start": start,
            "length": length,
            "score": float(alignment.score),
            "local_start": alignment.dest_start,
            "local_end": alignment.dest_end,
        }

    best = candidate(haystack, 0)
    if best is None:
        return None
    alternatives = []
    left = haystack[:best["local_start"]]
    right = haystack[best["local_end"]:]
    if left:
        alternatives.append(candidate(left, 0))
    if right:
        alternatives.append(candidate(right, best["local_end"]))
    tied = [item for item in alternatives
            if item is not None and item["score"] >= best["score"] - 0.5]
    if tied:
        pages = set()
        for item in [best, *tied]:
            quads = _quads_from_span(
                full_locs, item["start"], item["length"])
            pages.update(_candidate_pages(quads))
        return {
            "status": AMBIGUOUS,
            "method": "fuzzy",
            "page_quads": [],
            "candidate_pages": sorted(pages),
            "score": best["score"],
            "reason": "模糊匹配存在并列候选，拒绝自动写入",
        }

    page_quads = _quads_from_span(
        full_locs, best["start"], best["length"])
    return {
        "status": HIT,
        "method": "fuzzy",
        "page_quads": page_quads,
        "candidate_pages": _candidate_pages(page_quads),
        "score": best["score"],
        "reason": "",
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _declared_source_sha256(data: dict | list,
                            findings: list[dict]) -> str | None:
    """读取 findings.v1 顶层或逐条 finding 的源文件哈希。"""
    values = []
    is_v1 = False
    if isinstance(data, dict):
        is_v1 = data.get("schema") == "ai-proofread.findings.v1"
        if is_v1 and not (
                isinstance(data.get("source_sha256"), str)
                and data["source_sha256"].strip()):
            sys.exit("ai-proofread.findings.v1 缺少 source_sha256，拒绝写回")
        if isinstance(data.get("source_sha256"), str):
            values.append(data["source_sha256"])
        source = data.get("source")
        if isinstance(source, dict) and isinstance(source.get("sha256"), str):
            values.append(source["sha256"])
    values.extend(
        f["source_sha256"] for f in findings
        if isinstance(f.get("source_sha256"), str)
    )
    normalized = {value.strip().lower() for value in values if value.strip()}
    if not normalized:
        return None
    if len(normalized) != 1:
        sys.exit("findings 中存在互相冲突的 source_sha256")
    expected = normalized.pop()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        sys.exit("findings 的 source_sha256 不是有效 SHA-256")
    return expected


def _validate_findings_v1(data: dict, page_count: int) -> list[dict]:
    """Validate the Codex-native PDF findings contract."""
    findings = data.get("issues") if "issues" in data else data.get("findings")
    if not isinstance(findings, list) or not findings:
        sys.exit("ai-proofread.findings.v1 的 issues 必须是非空数组")
    required = ("fix_class", "current", "suggested", "reason", "category")
    validated = []
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            sys.exit(f"finding #{index} 必须是对象")
        missing = [key for key in required if key not in finding]
        if missing:
            sys.exit(f"finding #{index} 缺少字段: {', '.join(missing)}")
        if finding.get("fix_class") not in ("must_fix", "polish", "verify"):
            sys.exit(f"finding #{index} 的 fix_class 无效: {finding.get('fix_class')}")
        for key in ("current", "suggested", "reason", "category"):
            if not isinstance(finding.get(key), str):
                sys.exit(f"finding #{index} 的 {key} 必须是字符串")
        for key in ("current", "reason", "category"):
            if not finding[key].strip():
                sys.exit(f"finding #{index} 的 {key} 不能为空")
        page = finding.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            sys.exit(f"finding #{index} 缺少有效 PDF 页码（应为一基整数 page）")
        if page < 1 or page > page_count:
            sys.exit(f"finding #{index} 的 page 超出范围: {page}（共 {page_count} 页）")
        evidence = finding.get("evidence", [])
        if not isinstance(evidence, list):
            sys.exit(f"finding #{index} 的 evidence 必须是对象数组")
        for evidence_index, item in enumerate(evidence, 1):
            if not isinstance(item, dict):
                sys.exit(f"finding #{index} 的 evidence #{evidence_index} 必须是对象")
            for key in ("title", "url", "accessed_at"):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    sys.exit(
                        f"finding #{index} 的 evidence #{evidence_index} "
                        f"缺少 {key}")
        validated.append(dict(finding))
    return validated


def _source_manifest_sha256(manifest_path: str, source_pdf: Path) -> str:
    """Validate an extract manifest and return its bound PDF digest."""
    manifest = Path(manifest_path)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"无法读取 source manifest: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ai-proofread.source.v1":
        sys.exit("source manifest 不是 ai-proofread.source.v1")
    if data.get("source_type") != "pdf":
        sys.exit("source manifest 的 source_type 必须是 pdf")
    declared_path = data.get("source_path")
    if not isinstance(declared_path, str) or Path(declared_path).resolve() != source_pdf.resolve():
        sys.exit("source manifest 与输入 PDF 路径不一致")
    digest = str(data.get("source_sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        sys.exit("source manifest 缺少有效 source_sha256")
    return digest


def _parse_page_hint(finding: dict, page_count: int) -> tuple[int | None, str]:
    raw = finding.get("page")
    if raw in (None, ""):
        return None, ""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None, f"无效页码提示: {raw!r}"
    if page < 1 or page > page_count:
        return None, f"页码提示超出范围: {page}（共 {page_count} 页）"
    return page, ""


_MERGE_GAP = 2  # 相邻改动间隔 ≤2 个相等字符时合并为一个改动（`人`→`病人会` 不拆碎）


def _diff_spans(cur: str, sug: str) -> list[tuple[int, int, str]] | None:
    """求 cur↔sug 的全部字符级差异区间，返回 [(start, end, replacement), ...]。

    用于把 PDF 批注从「整句引用」缩到「逐处字符级差异」：一句内多处分散改动
    逐处独立高亮，弹窗只显示 `原「X」→「Y」`。用 SequenceMatcher opcode 逐处
    切分，并把被 ≤_MERGE_GAP 个相等字符隔开的相邻改动合并（`人`→`病人会` 这类
    「替换文本包含原文」的插入式改法不会被拆碎成多段）。replacement 为空 =
    纯删除；start==end = 纯插入（锚定前一字符）。无法字符级定位（无 sug /
    差异 ≥80% / 句首纯插入无锚点）时返回 None，调用方回退整句。
    """
    if not sug or cur == sug:
        return None
    n = len(cur)
    opcodes = SequenceMatcher(None, cur, sug, autojunk=False).get_opcodes()

    def _finalize(fi1, li2, fj1, lj2):
        start, end = fi1, li2
        repl = sug[fj1:lj2]
        if end == start:
            # 纯插入：锚定前一字符定位；句首插入无锚点 → 无法字符级。
            if fi1 <= 0:
                return None
            start, end = fi1 - 1, fi1
            repl = cur[start:end] + repl
        return (start, end, repl)

    spans: list[tuple[int, int, str]] = []
    total_changed = 0
    pending = None  # 当前改动组 (fi1, li2, fj1, lj2)
    gap = 0         # 距上一改动的相等字符数
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            if pending is not None:
                gap += i2 - i1
            continue
        if pending is not None and gap <= _MERGE_GAP:
            fi1, _li2, fj1, _lj2 = pending
            pending = (fi1, i2, fj1, j2)
        else:
            if pending is not None:
                span = _finalize(*pending)
                if span is None:
                    return None
                spans.append(span)
            pending = (i1, i2, j1, j2)
        gap = 0
        total_changed += max(i2 - i1, j2 - j1)
    if pending is not None:
        span = _finalize(*pending)
        if span is None:
            return None
        spans.append(span)
    if not spans:
        return None
    if total_changed * 10 >= n * 8:
        return None  # 差异覆盖 ≥80% cur，本就是大改，缩小无意义
    return spans


def _annotation_content(finding: dict, original: str,
                        corrected: str, tag: str) -> str:
    """把 findings.v1 的建议、理由、分类和联网证据写入弹窗。"""
    lines = [f"{tag} {original}"]
    if corrected and corrected != original:
        lines.append(f"→ {corrected}")
    elif original:
        lines.append("→（删除）")
    category = finding.get("category")
    if isinstance(category, str) and category.strip():
        lines.append(f"分类：{category.strip()}")
    reasons = []
    for key in ("reason", "message"):
        value = finding.get(key)
        if (isinstance(value, str) and value.strip()
                and value.strip() not in reasons
                and value.strip() != corrected):
            reasons.append(value.strip())
    for reason in reasons:
        lines.append(f"理由：{reason}")
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            accessed = str(item.get("accessed_at", "")).strip()
            label = title or url
            if not label:
                continue
            if accessed:
                label += f"（访问于 {accessed}）"
            lines.append(f"依据：{label}")
            if url and url != title:
                lines.append(url)
    return "\n".join(lines)


def _verify_annotations(out_path: str, expected: list[tuple]) -> int:
    """重开输出，核验新批注的页码、类型、作者、内容和 quad 数。"""
    import fitz
    doc = fitz.open(out_path)
    actual = Counter()
    for pno, page in enumerate(doc):
        for annot in page.annots() or ():
            info = annot.info or {}
            vertices = annot.vertices or []
            actual[(
                pno,
                annot.type[1],
                info.get("title", ""),
                info.get("content", ""),
                len(vertices) // 4,
            )] += 1
    doc.close()
    missing = Counter(expected) - actual
    if missing:
        details = "; ".join(
            f"page={item[0] + 1}, type={item[1]}, author={item[2]!r}, "
            f"quads={item[4]} x{count}"
            for item, count in missing.items()
        )
        raise RuntimeError(f"PDF 批注写后校验失败: {details}")
    return sum(actual.values())


def annotate_pdf(pdf_path: str, findings_path: str,
                 out_path: str | None = None,
                 author: str = "审校助手",
                 csv_path: str | None = None,
                 dry_run: bool = False,
                 allow_fragment: bool = False,
                 allow_fuzzy: bool = False,
                 source_manifest: str | None = None) -> str:
    """在原 PDF 上对每条 finding 的原文加高亮 + 弹窗批注，输出批注版 PDF。

    dry_run=True 时只做定位统计 + 写 CSV，不生成批注 PDF（预览命中率）。
    fragment / fuzzy 默认 status=preview；必须分别显式允许才会写入。
    source_manifest 为 legacy findings 提供源 PDF 路径与 SHA-256 绑定。
    csv_path 默认 {stem}_批注清单.csv（UTF-8 BOM，Excel 可直接打开）。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("缺少依赖：pip install pymupdf")

    src = Path(pdf_path)
    if not src.exists():
        sys.exit(f"PDF 不存在: {src}")
    findings_src = Path(findings_path)
    if not findings_src.exists():
        sys.exit(f"findings 不存在: {findings_src}")

    csv_destination = Path(csv_path) if csv_path else src.with_name(
        f"{src.stem}_批注清单.csv")
    pdf_destination = Path(out_path) if out_path else src.with_name(
        f"{src.stem}_审阅版.pdf")
    resolved = {
        "source": src.resolve(),
        "findings": findings_src.resolve(),
        "csv": csv_destination.resolve(),
        "output": pdf_destination.resolve(),
    }
    protected_inputs = {resolved["source"], resolved["findings"]}
    if source_manifest:
        protected_inputs.add(Path(source_manifest).resolve())
    if resolved["csv"] in protected_inputs:
        sys.exit("CSV 输出路径不能覆盖原 PDF、findings 或 source manifest")
    if resolved["output"] in protected_inputs:
        sys.exit("PDF 输出路径不能覆盖原 PDF、findings 或 source manifest")
    if resolved["csv"] == resolved["output"]:
        sys.exit("PDF 与 CSV 输出路径不能相同")

    with open(findings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if (isinstance(data, dict) and data.get("schema")
            and data.get("schema") != "ai-proofread.findings.v1"):
        sys.exit(f"不支持的 findings schema: {data.get('schema')}")

    is_v1 = (isinstance(data, dict)
             and data.get("schema") == "ai-proofread.findings.v1")
    findings = _flatten_findings(data)
    if not is_v1 and not findings:
        sys.exit(f"findings 为空: {findings_path}")

    expected_hash = _declared_source_sha256(data, findings)
    manifest_hash = (_source_manifest_sha256(source_manifest, src)
                     if source_manifest else None)
    if expected_hash and manifest_hash and expected_hash != manifest_hash:
        sys.exit("findings 与 source manifest 的 SHA-256 不一致")
    bound_hash = expected_hash or manifest_hash
    if not bound_hash:
        sys.exit("旧版 findings 缺少源哈希；请传 --source-manifest")

    snapshot_dir = tempfile.TemporaryDirectory(prefix="ai-proofread-pdf-source-")
    snapshot = Path(snapshot_dir.name) / f"source{src.suffix}"
    shutil.copy2(src, snapshot)
    snapshot_hash = _file_sha256(snapshot)
    current_path_hash = _file_sha256(src)
    if snapshot_hash != bound_hash or current_path_hash != bound_hash:
        snapshot_dir.cleanup()
        sys.exit(
            "源文件 SHA-256 不匹配，拒绝写回："
            f"expected={bound_hash}, snapshot={snapshot_hash}, "
            f"PDF={current_path_hash}"
        )

    try:
        doc = fitz.open(str(snapshot))
    except Exception:
        snapshot_dir.cleanup()
        raise
    try:
        if is_v1:
            findings = _validate_findings_v1(data, len(doc))
    except SystemExit:
        doc.close()
        snapshot_dir.cleanup()
        raise
    if not findings:
        doc.close()
        snapshot_dir.cleanup()
        sys.exit(f"findings 为空: {findings_path}")

    try:
        full_text, full_locs = _build_text_index(doc)
    except Exception:
        doc.close()
        snapshot_dir.cleanup()
        raise

    csv_path = str(csv_destination)
    out_path = str(pdf_destination)
    csv_destination.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        pdf_destination.parent.mkdir(parents=True, exist_ok=True)

    records: list[list] = []
    applied = 0
    skipped: list[tuple[int, str]] = []
    previewed: list[tuple[int, str]] = []
    expected_annots: list[tuple] = []

    for i, f in enumerate(findings, 1):
        original = _extract_original(f)
        phase = str(f.get("phase", ""))
        fix_class = str(f.get("fix_class", ""))
        corrected = _extract_corrected(f)
        row_base = [i, phase, fix_class, original, corrected]

        if not original:
            records.append(row_base + [
                SKIP, "-", "-", "-", 0, "无原文字段", "-"
            ])
            skipped.append((i, "无原文字段"))
            continue
        # 太短（<2 字）或太长（>200 字）跳过——短易误匹配，长定位不到
        if len(original) < 2 or len(original) > 200:
            reason = f"原文长度异常({len(original)}字)"
            records.append(row_base + [
                SKIP, "-", "-", "-", 0, reason, "-"
            ])
            skipped.append((i, reason))
            continue

        norm_orig = "".join(original.split())
        page_hint, page_error = _parse_page_hint(f, len(doc))
        if page_error:
            records.append(row_base + [
                SKIP, "-", "-", "-", 0, page_error, "-"
            ])
            skipped.append((i, page_error))
            continue
        match = None

        # 1) 索引精确匹配（跨行/跨页连续文本）
        match = _locate_exact(full_text, full_locs, norm_orig, page_hint)
        if match is None:
            # 2) 最长连续片段降级（PDF 换行拆句导致整句拼接后仍找不到时）
            for frag in _longest_pure_fragment(original):
                frag_norm = "".join(frag.split())
                frag_hit = _locate_exact(
                    full_text, full_locs, frag_norm, page_hint)
                if frag_hit:
                    match = frag_hit
                    match["method"] = "fragment"
                    match["score"] = round(
                        len(frag_norm) / len(norm_orig) * 100, 1)
                    if match["status"] == AMBIGUOUS:
                        match["reason"] = (
                            "片段" + match["reason"].removeprefix("精确")
                        )
                    break
            # 3) rapidfuzz 模糊定位（仅精确失败后）
            if match is None:
                match = _locate_fuzzy(
                    full_text, full_locs, norm_orig, page_hint)

        if match is None:
            reason = f"未定位: {original[:20]}..."
            records.append(row_base + [
                SKIP, "-", "-", "-", 0, reason, "-"
            ])
            skipped.append((i, reason))
            continue

        match_method = match["method"]
        page_quads = match["page_quads"]
        status = match["status"]
        reason = match["reason"]
        if status == HIT and match_method == "fragment" and not allow_fragment:
            status = PREVIEW
            reason = "fragment 默认仅预览；正式写入需 --allow-fragment"
        if status == HIT and match_method == "fuzzy" and not allow_fuzzy:
            status = PREVIEW
            reason = "fuzzy 默认仅预览；正式写入需 --allow-fuzzy"

        pages = ",".join(str(p + 1) for p, _ in page_quads) or "-"
        candidate_pages = ",".join(
            str(page) for page in match["candidate_pages"]
        ) or "-"
        quad_count = sum(len(quads) for _, quads in page_quads)
        records.append(row_base + [
            status,
            match_method,
            pages,
            candidate_pages,
            quad_count,
            reason or "-",
            f"{match['score']:.1f}",
        ])

        if status == AMBIGUOUS:
            skipped.append((i, reason))
            continue
        if status == PREVIEW:
            previewed.append((i, reason))
            continue

        if dry_run:
            continue  # 只统计不写批注

        # 生成高亮批注（复用已保存的 page 对象，规避 1.27 绑定坑）
        annot_cls = _annot_class(f)
        color, tag = _FIX_STYLE[annot_cls]

        # 字符级差异：exact/crosspage 命中时只高亮真正变化的字，弹窗只显
        # 差异片段（原「X」→「Y」）；一句内多处分散改动逐处独立高亮
        # （multi-span），避免把一字之改埋进长句。
        pieces = [(page_quads, original, corrected)]
        if match_method in ("exact", "crosspage") and "start" in match:
            spans = _diff_spans(norm_orig, "".join(corrected.split()))
            if spans is not None:
                pieces = []
                for c_start, c_end, repl in spans:
                    full_start = match["start"] + c_start
                    full_end = match["start"] + c_end
                    if full_end <= full_start:
                        continue
                    sub_quads = _quads_from_span(
                        full_locs, full_start, full_end - full_start)
                    if sub_quads:
                        pieces.append((
                            sub_quads, norm_orig[c_start:c_end], repl,
                        ))
                if not pieces:
                    pieces = [(page_quads, original, corrected)]

        for highlight_quads, content_original, content_corrected in pieces:
            content = _annotation_content(
                f, content_original, content_corrected, tag)
            for pno, quads in highlight_quads:
                page = doc[pno]
                annot = page.add_highlight_annot(quads)
                annot.set_info(title=author, content=content)
                annot.set_colors(stroke=color)
                annot.update()
                expected_annots.append((
                    pno, "Highlight", author, content, len(quads)
                ))
        applied += 1

    if _file_sha256(src) != bound_hash:
        doc.close()
        snapshot_dir.cleanup()
        sys.exit("源 PDF 在定位期间发生变化，拒绝生成审校产物")

    def write_csv(destination: Path) -> None:
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "序号", "phase", "fix_class", "原文", "建议", "状态",
                "匹配方式", "页码", "候选页", "quad数", "原因", "得分",
            ])
            writer.writerows(records)

    # dry-run：只预览不生成批注 PDF
    if dry_run:
        write_csv(csv_destination)
        print(f"📋 批注清单: {csv_path}")
        doc.close()
        snapshot_dir.cleanup()
        hit_n = sum(1 for row in records if row[5] == HIT)
        print(
            f"🔍 dry-run 预览: 共 {len(findings)} 条发现 → "
            f"可安全定位 {hit_n} 条, 仅预览 {len(previewed)} 条, "
            f"跳过/歧义 {len(skipped)} 条"
        )
        if skipped:
            print("   跳过明细(前10):")
            for idx, reason in skipped[:10]:
                print(f"     #{idx}: {reason}")
        return str(csv_path)

    temp_output = pdf_destination.with_name(
        f".{pdf_destination.stem}.tmp-{os.getpid()}.pdf")
    temp_csv = csv_destination.with_name(
        f".{csv_destination.name}.tmp-{os.getpid()}")
    try:
        write_csv(temp_csv)
        doc.save(str(temp_output), garbage=4, deflate=True)
        doc.close()
        total_annots = _verify_annotations(str(temp_output), expected_annots)
        if _file_sha256(src) != bound_hash:
            raise RuntimeError("源 PDF 在写回期间发生变化，拒绝交付")
        os.replace(temp_output, pdf_destination)
        os.replace(temp_csv, csv_destination)
    finally:
        if not doc.is_closed:
            doc.close()
        if temp_output.exists():
            temp_output.unlink()
        if temp_csv.exists():
            temp_csv.unlink()
        snapshot_dir.cleanup()

    print(f"📋 批注清单: {csv_path}")
    print(f"✅ 高亮批注完成: {out_path}")
    print(
        f"   共 {len(findings)} 条发现 → 高亮 {applied} 条, "
        f"仅预览 {len(previewed)} 条, 跳过/歧义 {len(skipped)} 条"
    )
    if skipped:
        print("   跳过明细(前10):")
        for idx, reason in skipped[:10]:
            print(f"     #{idx}: {reason}")

    print(
        f"   校验: 新增批注 {len(expected_annots)} 条，"
        f"批注版注释总数 = {total_annots}"
    )
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF 审校工具链")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pdf2md = sub.add_parser("pdf2md", help="PDF → Markdown（供 proofread max 消费）")
    p_pdf2md.add_argument("input", help="输入 PDF 路径")
    p_pdf2md.add_argument("--out", help="输出 Markdown 路径（默认同目录同名 .md）")

    p_annot = sub.add_parser("annotate", help="审校发现 → 原 PDF 高亮批注")
    p_annot.add_argument("input", help="输入 PDF 路径（原稿）")
    p_annot.add_argument("findings", help="findings JSON 路径（{doc}_max_results.json）")
    p_annot.add_argument("--out", help="输出批注版 PDF 路径（默认 {stem}_审阅版.pdf）")
    p_annot.add_argument("--author", default="审校助手", help="批注作者名")
    p_annot.add_argument("--dry-run", action="store_true",
                         help="仅预览定位命中率 + 写 CSV，不生成批注 PDF")
    p_annot.add_argument("--csv", help="批注清单 CSV 路径（默认 {stem}_批注清单.csv）")
    p_annot.add_argument(
        "--allow-fragment", action="store_true",
        help="允许把 fragment 降级命中正式写入（默认仅在 CSV 预览）")
    p_annot.add_argument(
        "--allow-fuzzy", action="store_true",
        help="允许把 fuzzy 降级命中正式写入（默认仅在 CSV 预览）")
    p_annot.add_argument(
        "--source-manifest",
        help="proofread extract 生成的 ai-proofread.source.v1（绑定 legacy findings）")

    args = parser.parse_args()

    if args.command == "pdf2md":
        pdf2md(args.input, args.out)
    elif args.command == "annotate":
        annotate_pdf(
            args.input,
            args.findings,
            args.out,
            args.author,
            csv_path=args.csv,
            dry_run=args.dry_run,
            allow_fragment=args.allow_fragment,
            allow_fuzzy=args.allow_fuzzy,
            source_manifest=args.source_manifest,
        )


if __name__ == "__main__":
    main()
