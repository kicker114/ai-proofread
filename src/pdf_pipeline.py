#!/usr/bin/env python3
"""PDF 审校工具链：PDF→Markdown 转换 + 审校发现高亮批注回写。

WorkBuddy/Claude Code 通用入口，打通「图文混排 PDF → 审校 → 原 PDF 高亮批注」全链路：

    python3 src/pdf_pipeline.py pdf2md   <input.pdf> [--out <output.md>]
    python3 src/pdf_pipeline.py annotate <input.pdf> <findings.json> [--out <annotated.pdf>] [--author 审校助手]

annotate 读取 max 流水线输出的 findings JSON（`{doc}_max_results.json`，
形如 {"llm": [{original_sentence, corrected_sentence, fix_class, ...}], ...}），
在**原 PDF** 上逐条定位原文并添加高亮注释（Highlight annotation + 弹窗内容），
输出批注版 PDF。

依赖：pymupdf（系统 Python 需 /usr/local/bin/python3 或安装 pymupdf）。
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ── 工具 1：PDF → Markdown ────────────────────────────────────────────


def pdf2md(pdf_path: str, out_path: str | None = None) -> str:
    """用 pymupdf4llm 把图文混排 PDF 转成 Markdown（标题/段落/表格保留）。

    无文字层页面（插图页/章扉页）跳过，不影响正文审校。
    """
    try:
        import pymupdf4llm
    except ImportError:
        sys.exit("缺少依赖：pip install pymupdf4llm")

    src = Path(pdf_path)
    if not src.exists():
        sys.exit(f"PDF 不存在: {src}")

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
    import re
    frags = re.split(r"[\s，。！？、；：,.!?;:（）()「」『』《》〈〉\"'“”‘’—…·\-—\d]+", text)
    frags = [f for f in frags if len(f) >= min_len]
    frags.sort(key=len, reverse=True)
    return frags


def annotate_pdf(pdf_path: str, findings_path: str,
                 out_path: str | None = None,
                 author: str = "审校助手") -> str:
    """在原 PDF 上对每条 finding 的原文加高亮 + 弹窗批注，输出批注版 PDF。"""
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
    total_pages = len(doc)
    # 页级文本缓存（去空白），先做包含判断避免全量 search_for
    page_texts = ["".join(doc[i].get_text().split()) for i in range(total_pages)]
    applied = 0
    skipped = []

    for i, f in enumerate(findings, 1):
        original = _extract_original(f)
        if not original:
            skipped.append((i, "无原文字段"))
            continue
        # 太短（<2 字）或太长（>200 字）跳过——短易误匹配，长定位不到
        if len(original) < 2 or len(original) > 200:
            skipped.append((i, f"原文长度异常({len(original)}字)"))
            continue

        norm_orig = "".join(original.split())
        # 候选：整句 → 去空白整句 → 最长连续片段（始终附加——PDF 文本层常因
        # 换行拆句，如'总是道'+'德哲学'分处两行，search_for 不跨行匹配；
        # 而 page_texts 去空白后整句看似存在，故不能以此判断可定位）
        candidates = [original, norm_orig] + _longest_pure_fragment(original)
        # 去重（按去空白形式），保持从长到短/整句优先
        seen, dedup = set(), []
        for c in candidates:
            cn = "".join(c.split())
            if cn and cn not in seen:
                seen.add(cn)
                dedup.append(c)
        candidates = dedup
        # 逐页定位——注意必须保存 page 对象复用，
        # 内联 doc[pno] 多次调用在 PyMuPDF 1.27 下会丢失 annot↔page 绑定
        hit = None
        for pno in range(total_pages):
            pt = page_texts[pno]
            if not any("".join(c.split()) in pt for c in candidates):
                continue
            page = doc[pno]
            for cand in candidates:
                rects = page.search_for(cand)
                if not rects and " " in cand:
                    rects = page.search_for("".join(cand.split()))
                if rects:
                    hit = (page, rects[0])
                    break
            if hit:
                break
        if not hit:
            skipped.append((i, f"未定位: {original[:20]}..."))
            continue

        page, rect = hit
        annot_cls = _annot_class(f)
        color, tag = _FIX_STYLE[annot_cls]
        corrected = _extract_corrected(f)
        content = f"{tag} {original}"
        if corrected and corrected != original:
            content += f"\n→ {corrected}"
        if f.get("message"):
            content += f"\n{f['message']}"

        annot = page.add_highlight_annot(rect)
        annot.set_info(title=author, content=content)
        annot.set_colors(stroke=color)
        annot.update()
        applied += 1

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

    args = parser.parse_args()

    if args.command == "pdf2md":
        pdf2md(args.input, args.out)
    elif args.command == "annotate":
        annotate_pdf(args.input, args.findings, args.out, args.author)


if __name__ == "__main__":
    main()
