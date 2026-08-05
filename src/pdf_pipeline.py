#!/usr/bin/env python3
"""PDF 审校工具链：PDF→Markdown 转换 + 审校发现高亮批注回写。

WorkBuddy/Claude Code 通用入口，打通「图文混排 PDF → 审校 → 原 PDF 高亮批注」全链路：

    python3 src/pdf_pipeline.py pdf2md   <input.pdf> [--out <output.md>]
    python3 src/pdf_pipeline.py annotate <input.pdf> <findings.json>
             [--out <annotated.pdf>] [--author 审校助手] [--dry-run] [--csv <path>]

annotate 读取 max 流水线输出的 findings JSON（`{doc}_max_results.json`，
形如 {"llm": [{original_sentence, corrected_sentence, fix_class, ...}], ...}），
在**原 PDF** 上逐条定位原文并添加高亮注释（Highlight annotation + 弹窗内容），
输出批注版 PDF（默认 `{stem}_审阅版.pdf`）+ 批注清单 CSV。

定位策略（三级降级）：
  1. 字符级全文索引精确匹配（跨行/跨页连续文本一次高亮多块）
  2. 最长连续片段降级（PDF 换行拆句）
  3. rapidfuzz 模糊匹配（len≥4，score≥85，首尾 partial_ratio 校验，清单标注待复核）

依赖：pymupdf（系统 Python 需 /usr/local/bin/python3 或安装 pymupdf）、rapidfuzz、pymupdf4llm。
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

# ── 工具 1：PDF → Markdown ────────────────────────────────────────────


def pdf2md(pdf_path: str, out_path: str | None = None) -> str:
    """用 pymupdf4llm 把图文混排 PDF 转成 Markdown（标题/段落/表格保留）。

    无文字层页面（插图页/章扉页）统计提示并跳过，不影响正文审校。
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

    # 统计无文字层页面（插图页/章扉页/扫描页）
    probe = fitz.open(str(src))
    empty_pages = [i + 1 for i in range(len(probe))
                   if not probe[i].get_text().strip()]
    probe.close()
    if empty_pages:
        shown = ", ".join(str(p) for p in empty_pages[:20])
        more = "..." if len(empty_pages) > 20 else ""
        print(f"⚠️  无文字层页面 {len(empty_pages)} 页（插图/章扉页，跳过）: {shown}{more}")

    md_text = pymupdf4llm.to_markdown(str(src))
    if not md_text.strip():
        sys.exit(f"PDF 无可提取文本（可能全为扫描图，需先 OCR）: {src}")

    if out_path is None:
        out_path = str(src.with_suffix(".md"))
    Path(out_path).write_text(md_text, encoding="utf-8")
    print(f"✅ PDF→MD: {out_path} ({len(md_text)} 字符)")
    return out_path


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


def _flatten_findings(data: dict | list) -> list[dict]:
    """把 max_results.json（按阶段分组的 dict）拍平为 findings 列表。

    兼容两种形态：
      - {"llm": [...], "tgscc": [...], ...}
      - [ {...}, ... ]（已拍平）
    """
    if isinstance(data, list):
        return [f for f in data if isinstance(f, dict)]
    out = []
    for key, batch in data.items():
        if isinstance(batch, list):
            for f in batch:
                if isinstance(f, dict):
                    f.setdefault("phase", key)
                    out.append(f)
    return out


def _extract_original(f: dict) -> str:
    """从一条 finding 提取原文片段（用于 PDF 定位）。"""
    for k in ("original_sentence", "original", "char", "wrong", "text"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _extract_corrected(f: dict) -> str:
    """从一条 finding 提取修正建议（用于批注弹窗）。"""
    for k in ("corrected_sentence", "suggestion", "correct", "replace", "message"):
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


def _build_text_index(doc) -> tuple[str, list[tuple[int, "fitz.Rect"]]]:
    """构建字符级全文索引。

    Returns:
        full_text: 全书去空白后的拼接文本（保持字符顺序）
        full_locs: 与 full_text 逐字符对应的 (page_index, line_bbox)
                   —— 由 get_text("dict") 的行级 bbox 生成，可精确映射
                   任意子串到其所在页的行矩形（支持跨行/跨页连续文本）。

    注意：PyMuPDF 1.27 下必须复用 page 对象（内联 doc[pno] 多次调用
    会丢失 annot↔page 绑定），本函数只读不写，无此风险。
    """
    import fitz  # PyMuPDF（类型标注用）
    raw_chars: list[str] = []
    raw_locs: list[tuple[int, "fitz.Rect"]] = []
    for pno in range(len(doc)):
        page = doc[pno]
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            if block.get("type") != 0:  # 非文本块（图片等）跳过
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(s.get("text", "") for s in spans)
                if not line_text.strip():
                    continue
                line_bbox = fitz.Rect(line["bbox"])
                for ch in line_text:
                    raw_chars.append(ch)
                    raw_locs.append((pno, line_bbox))
    # 去空白对齐（候选也是去空白的 norm_s）
    full_chars: list[str] = []
    full_locs: list[tuple[int, "fitz.Rect"]] = []
    for ch, loc in zip(raw_chars, raw_locs):
        if ch.strip():
            full_chars.append(ch)
            full_locs.append(loc)
    return "".join(full_chars), full_locs


def _rects_from_span(full_locs, start: int, length: int) -> list[tuple[int, "fitz.Rect"]]:
    """把索引区间 [start, start+length) 映射为 按页分组的行 bbox 列表。

    Returns:
        [(page_index, merged_line_rect), ...]（同页多行合并为一个 rect）
    """
    import fitz  # PyMuPDF（类型标注用）
    seg = full_locs[start:start + length]
    by_page: OrderedDict[int, list] = OrderedDict()
    for pno, bbox in seg:
        by_page.setdefault(pno, []).append(bbox)
    out = []
    for pno, bboxes in by_page.items():
        merged = fitz.Rect()
        for b in bboxes:
            merged |= b
        out.append((pno, merged))
    return out


def _locate_exact(full_text: str, full_locs, norm_s: str):
    """索引精确匹配：整句去空白后在全文 find，返回 (method, rects)。

    method: 'exact'（单页）/ 'crosspage'（跨页连续文本）
    """
    idx = full_text.find(norm_s)
    if idx == -1:
        return None
    rects = _rects_from_span(full_locs, idx, len(norm_s))
    method = "crosspage" if len({p for p, _ in rects}) > 1 else "exact"
    return method, rects


def _locate_fuzzy(full_text: str, full_locs, norm_s: str,
                  score_cutoff: float = 85.0):
    """rapidfuzz 模糊定位（回退层）。

    仅在 len(norm_s) >= 4 时使用；滑窗评分取唯一最高分，且窗口首尾
    对候选首尾做 partial_ratio 校验，降低误匹配。命中标注 'fuzzy'。
    """
    if len(norm_s) < 4 or len(full_text) < len(norm_s):
        return None
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None

    n = len(norm_s)
    window = max(int(n * 1.2), n + 8)
    step = max(1, n // 2)
    best_score = 0.0
    best_start = -1
    for start in range(0, len(full_text) - n + 1, step):
        chunk = full_text[start:start + window]
        if len(chunk) < n:
            break
        score = fuzz.ratio(norm_s, chunk)
        if score > best_score:
            best_score = score
            best_start = start
        if score >= 100:
            break
    if best_score < score_cutoff or best_start < 0:
        return None
    # 首尾 partial_ratio 校验
    head_n = min(4, n)
    tail_n = min(4, n)
    head_ok = fuzz.partial_ratio(
        norm_s[:head_n], full_text[best_start:best_start + head_n]) >= 80
    tail_ok = fuzz.partial_ratio(
        norm_s[-tail_n:],
        full_text[best_start + n - tail_n:best_start + n]) >= 80
    if not (head_ok and tail_ok):
        return None
    rects = _rects_from_span(full_locs, best_start, n)
    return "fuzzy", rects


def annotate_pdf(pdf_path: str, findings_path: str,
                 out_path: str | None = None,
                 author: str = "审校助手",
                 csv_path: str | None = None,
                 dry_run: bool = False) -> str:
    """在原 PDF 上对每条 finding 的原文加高亮 + 弹窗批注，输出批注版 PDF。

    dry_run=True 时只做定位统计 + 写 CSV，不生成批注 PDF（预览命中率）。
    csv_path 默认 {stem}_批注清单.csv（UTF-8 BOM，Excel 可直接打开）。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("缺少依赖：pip install pymupdf")

    src = Path(pdf_path)
    if not src.exists():
        sys.exit(f"PDF 不存在: {src}")
    with open(findings_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    findings = _flatten_findings(data)
    if not findings:
        sys.exit(f"findings 为空: {findings_path}")

    doc = fitz.open(str(src))
    full_text, full_locs = _build_text_index(doc)

    if csv_path is None:
        csv_path = str(src.with_name(f"{src.stem}_批注清单.csv"))

    records: list[list] = []
    applied = 0
    skipped = []

    for i, f in enumerate(findings, 1):
        original = _extract_original(f)
        phase = str(f.get("phase", ""))
        fix_class = str(f.get("fix_class", ""))
        corrected = _extract_corrected(f)
        row_base = [i, phase, fix_class, original, corrected]

        if not original:
            records.append(row_base + [SKIP, "-", "-", "无原文字段", "-"])
            skipped.append((i, "无原文字段"))
            continue
        # 太短（<2 字）或太长（>200 字）跳过——短易误匹配，长定位不到
        if len(original) < 2 or len(original) > 200:
            reason = f"原文长度异常({len(original)}字)"
            records.append(row_base + [SKIP, "-", "-", reason, "-"])
            skipped.append((i, reason))
            continue

        norm_orig = "".join(original.split())
        match_method = None
        rects = None

        # 1) 索引精确匹配（跨行/跨页连续文本）
        hit = _locate_exact(full_text, full_locs, norm_orig)
        if hit:
            match_method, rects = hit
        else:
            # 2) 最长连续片段降级（PDF 换行拆句导致整句拼接后仍找不到时）
            for frag in _longest_pure_fragment(original):
                frag_norm = "".join(frag.split())
                frag_hit = _locate_exact(full_text, full_locs, frag_norm)
                if frag_hit:
                    match_method, rects = frag_hit
                    match_method = "fragment"
                    break
            # 3) rapidfuzz 模糊定位（仅精确失败后）
            if match_method is None:
                fuzzy = _locate_fuzzy(full_text, full_locs, norm_orig)
                if fuzzy:
                    match_method, rects = fuzzy

        if match_method is None or not rects:
            reason = f"未定位: {original[:20]}..."
            records.append(row_base + [SKIP, "-", "-", reason, "-"])
            skipped.append((i, reason))
            continue

        # 命中 → 记录
        pages = ",".join(str(p + 1) for p, _ in rects)
        records.append(row_base + [HIT, match_method, pages, "-", "-"])

        if dry_run:
            continue  # 只统计不写批注

        # 生成高亮批注（复用已保存的 page 对象，规避 1.27 绑定坑）
        annot_cls = _annot_class(f)
        color, tag = _FIX_STYLE[annot_cls]
        content = f"{tag} {original}"
        if corrected and corrected != original:
            content += f"\n→ {corrected}"
        if f.get("message"):
            content += f"\n{f['message']}"

        for pno, rect in rects:
            page = doc[pno]
            annot = page.add_highlight_annot(rect)
            annot.set_info(title=author, content=content)
            annot.set_colors(stroke=color)
            annot.update()
        applied += 1

    # 写 CSV 清单（UTF-8 BOM）
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "phase", "fix_class", "原文", "建议",
                    "状态", "匹配方式", "页码", "跳过原因", "得分"])
        w.writerows(records)
    print(f"📋 批注清单: {csv_path}")

    # dry-run：只预览不生成批注 PDF
    if dry_run:
        doc.close()
        hit_n = sum(1 for r in records if r[5] == HIT)
        print(f"🔍 dry-run 预览: 共 {len(findings)} 条发现 → 可定位 {hit_n} 条, 跳过 {len(skipped)} 条")
        if skipped:
            print("   跳过明细(前10):")
            for idx, reason in skipped[:10]:
                print(f"     #{idx}: {reason}")
        return str(csv_path)

    if out_path is None:
        out_path = str(src.with_name(f"{src.stem}_审阅版.pdf"))
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()

    print(f"✅ 高亮批注完成: {out_path}")
    print(f"   共 {len(findings)} 条发现 → 高亮 {applied} 条, 跳过 {len(skipped)} 条")
    if skipped:
        print("   跳过明细(前10):")
        for idx, reason in skipped[:10]:
            print(f"     #{idx}: {reason}")

    # 校验：重新打开确认注释数
    verify = fitz.open(out_path)
    total_annots = sum(len(list(p.annots())) for p in verify)
    verify.close()
    print(f"   校验: 批注版注释总数 = {total_annots}")
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

    args = parser.parse_args()

    if args.command == "pdf2md":
        pdf2md(args.input, args.out)
    elif args.command == "annotate":
        annotate_pdf(args.input, args.findings, args.out, args.author,
                     csv_path=args.csv, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
