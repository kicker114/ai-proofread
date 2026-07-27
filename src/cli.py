#!/usr/bin/env python3
"""
ai-proofread CLI — 终端直接调用审校管线，无需 VSCode。

pip install -e . 后全局可用:
    proofread p <file.md|file.docx>             # 单文件校对 (自动转 .docx → .md)
    proofread b <file.md|file.docx>             # 全书分块校对
    proofread d <原稿.md> <校后.md>               # 生成 HTML diff
    proofread s <file.md>                       # 汉字规范专项检查

所有审校模型使用 DeepSeek V4 Flash (deepseek-v4-flash)。

输出:
    <stem>.proofread.md              校对后文件
    <stem>.proofread.json.md         全书校对后文件（book 模式）
    <stem>_diff.html                 浏览器可打开的 HTML 词级 diff
"""

import argparse
import asyncio
import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量（src/.env 中的 DEEPSEEK_API_KEY）
_SRC_DIR = Path(__file__).resolve().parent
_ENV_PATH = _SRC_DIR / ".env"
load_dotenv(str(_ENV_PATH))

DEFAULT_MODEL = "deepseek-v4-flash"
AVAILABLE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v3",
]


# ── DOCX → MD 转换 ────────────────────────────────────────────────────


def _docx_to_md(file_path: Path) -> tuple[Path, str]:
    """Convert a .docx file to .md for processing. Returns (md_path, text)."""
    try:
        import docx
    except ImportError:
        print("❌ 需要 python-docx 库: pip install python-docx")
        sys.exit(1)

    stem = file_path.parent / file_path.stem
    md_path = stem.parent / f"{stem.name}.md"

    doc = docx.Document(str(file_path))
    paragraphs = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            paragraphs.append("")
            continue

        style = para.style.name if para.style else "Normal"

        # 标题转换
        if style.startswith("Heading"):
            level = style.replace("Heading", "").strip()
            if level.isdigit() and 1 <= int(level) <= 9:
                paragraphs.append(f"{'#' * int(level)} {text}")
            else:
                paragraphs.append(text)
        else:
            paragraphs.append(text)

    md_text = "\n".join(paragraphs)

    # 写 .md 文件（与原 docx 同目录）
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    return md_path, md_text


def _resolve_input(file_path: Path) -> Path:
    """自动转换 .docx 为 .md，返回可处理的 .md 路径。"""
    ext = file_path.suffix.lower()
    if ext == ".docx":
        print(f"📄 正在转换 DOCX → MD...")
        md_path, _ = _docx_to_md(file_path)
        stem = file_path.parent / file_path.stem
        print(f"   ✓ {md_path.name}")
        return md_path
    elif ext == ".md":
        return file_path
    else:
        print(f"❌ 不支持的文件格式: {ext}（支持 .md 和 .docx）")
        sys.exit(1)


# ── 子命令: 单文件校对 ────────────────────────────────────────────────


def cmd_proofread(args):
    from .proofreader import deepseek

    file_path = _resolve_input(Path(args.file))
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        target = f.read()
    target_text = f"<target>\n{target}\n</target>"

    context = ""
    if args.context:
        with open(args.context, "r", encoding="utf-8") as f:
            context = f.read()
        context_text = f"<context>\n{context}\n</context>"
    else:
        context_text = ""

    reference = ""
    if args.reference:
        with open(args.reference, "r", encoding="utf-8") as f:
            reference = f.read()
        reference_text = f"<reference>\n{reference}\n</reference>"
    else:
        reference_text = ""

    combined_ref = f"{reference_text}\n\n{context_text}".strip()

    print(f"🔄 正在校对: {file_path.name}  (模型={args.model})")
    result = deepseek(target_text, combined_ref, model=args.model)

    if not result:
        print("❌ 校对失败 (API 返回空)")
        sys.exit(1)

    out_path = file_path.parent / f"{file_path.stem}.proofread.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)
    print(f"✅ 校对完成: {out_path}")

    if not args.no_diff:
        _generate_diff(file_path, str(out_path), args)


# ── 子命令: 全书校对 ─────────────────────────────────────────────────


def cmd_book(args):
    from .splitter import split_markdown_by_title_and_length_with_context
    from .proofreader import process_paragraphs_async

    file_path = _resolve_input(Path(args.file))
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    stem = file_path.parent / file_path.stem

    # ── Step 1: Split ──
    print(f"📦 [1/3] 正在分块 (levels={args.levels}, chunk_size={args.chunk_size})...")
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    chunks = split_markdown_by_title_and_length_with_context(
        text, levels=args.levels, cut_by=args.chunk_size
    )

    json_path = f"{stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    total_target = sum(len(c.get("target", "")) for c in chunks)
    print(f"   ✓ 分块完成: {len(chunks)} 段, {total_target} 字")
    if args.verbose:
        for i, c in enumerate(chunks):
            first = c.get("target", "").strip()[:30].splitlines()[0]
            print(f"      No.{i+1:>4}  {len(c.get('target','')):>6}字  {first}")

    # ── Step 2: Proofread ──
    print(f"🔄 [2/3] 正在校对 (模型={args.model}, 并发={args.concurrent})...")
    out_json = f"{stem}.proofread.json"
    asyncio.run(
        process_paragraphs_async(
            str(json_path),
            out_json,
            start_count=1,
            model=args.model,
            rpm=args.rpm,
            max_concurrent=args.concurrent,
        )
    )

    # ── Step 3: 合并输出 ──
    print(f"📝 [3/3] 生成校对后文件...")
    with open(out_json, "r", encoding="utf-8") as f:
        proofread_chunks = json.load(f)

    out_md = f"{stem}.proofread.json.md"
    md_text = "\n---\n".join(c for c in proofread_chunks if c)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md_text)

    processed = sum(1 for c in proofread_chunks if c)
    total = len(proofread_chunks)
    print(f"✅ 全书校对完成: {out_md}  ({processed}/{total} 段处理)")

    if not args.no_diff:
        _generate_diff(file_path, out_md, args)


# ── 通用: 生成 diff ──────────────────────────────────────────────────


def _generate_diff(input_path: Path, proofread_path: str, args) -> str | None:
    from .diff_tools import jsdiff_md_text

    try:
        parent = input_path.parent
        stem = input_path.stem
        diff_path = parent / f"{stem}_diff.html"

        jsdiff_md_text(
            str(parent) or ".",
            input_path.name,
            Path(proofread_path).name,
            str(diff_path),
        )
        abs_path = str(diff_path.resolve())
        url = f"file://{abs_path}"
        print(f"📄 Diff 页面: {url}")

        if not getattr(args, "no_view", False):
            webbrowser.open(url)
            print("🌐 浏览器已打开 (若未弹出，手动打开上述链接)")
        return abs_path
    except Exception as e:
        print(f"⚠️  生成 HTML diff 失败: {e}")
        return None


# ── 子命令: 仅生成 diff ──────────────────────────────────────────────


def cmd_diff(args):
    from .diff_tools import jsdiff_md_text

    before, after = Path(args.before), Path(args.after)
    for p in (before, after):
        if not p.exists():
            print(f"❌ 文件不存在: {p}")
            sys.exit(1)

    parent = before.parent
    diff_path = args.output or str(parent / f"{before.stem}_diff.html")

    jsdiff_md_text(str(parent) or ".", before.name, after.name, diff_path)
    abs_path = str(Path(diff_path).resolve())
    print(f"📄 Diff: file://{abs_path}")
    if not args.no_view:
        webbrowser.open(f"file://{abs_path}")
        print("🌐 浏览器已打开")


# ── 子命令: 专项检查 ──────────────────────────────────────────────────


def cmd_special(args):
    from .special_checker import check_to_tgscc

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    print(f"🔍 正在检查汉字规范...")
    results = check_to_tgscc(text)

    if not results:
        print("✅ 未发现问题")
        return

    print(f"\n{'─' * 60}")
    print(f"发现 {len(results)} 处可能问题:")
    print(f"{'─' * 60}")
    for r in results:
        print(f"  类型: {r.error_type}")
        print(f"  原文: {r.original_text}")
        print(f"  建议: {r.suggestion}")
        if r.location:
            print(f"  位置: {r.location}")
        print()

    stem = file_path.parent / file_path.stem
    csv_path = f"{stem}_special_check.csv"
    with open(csv_path, "w", encoding="utf-8-sig") as f:
        f.write("类型,原文,建议,位置,置信度\n")
        for r in results:
            f.write(
                f"{r.error_type},{r.original_text},{r.suggestion},{r.location},{r.confidence}\n"
            )
    print(f"📄 详细结果已保存: {csv_path}")


# ── 主入口 ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="proofread",
        description="ai-proofread 命令行工具 — 终端直接调用审校，无需 VSCode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
子命令简写:  p=proofread  b=book  d=diff  s=special

示例:
  proofread p 我的稿件.docx
  proofread p 我的稿件.md --context 上下文.md --ref 参考资料.md
  proofread b 我的稿件.docx --concurrent 5
  proofread d 原稿.md 校后.md
  proofread s 我的稿件.md

默认模型: {DEFAULT_MODEL}（DeepSeek V4 Flash）
        """,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    sub = parser.add_subparsers(dest="command")

    # proofread
    p = sub.add_parser("proofread", aliases=["p"], help="单文件校对")
    p.add_argument("file", help="要校对的文件 (.md / .docx)")
    p.add_argument("--context", help="上下文文件")
    p.add_argument("--ref", "--reference", dest="reference", help="参考资料文件")
    p.add_argument(
        "--model", default=DEFAULT_MODEL, choices=AVAILABLE_MODELS,
        help=f"模型 (默认 {DEFAULT_MODEL})",
    )
    p.add_argument("--no-diff", action="store_true", help="不生成 diff HTML")
    p.add_argument("--no-view", action="store_true", help="不自动打开浏览器")

    # book
    b = sub.add_parser("book", aliases=["b"], help="全书校对（split + pipeline）")
    b.add_argument("file", help="要校对的文件 (.md / .docx)")
    b.add_argument("--chunk-size", type=int, default=200, help="每块目标字数 (默认 200)")
    b.add_argument(
        "--levels", type=int, nargs="+", default=[1, 2],
        help="标题切分级别 (默认 1 2)",
    )
    b.add_argument("--model", default=DEFAULT_MODEL, choices=AVAILABLE_MODELS)
    b.add_argument("--rpm", type=int, default=15, help="API 速率限制 (默认 15)")
    b.add_argument("--concurrent", type=int, default=3, help="并发数 (默认 3)")
    b.add_argument("--no-diff", action="store_true")
    b.add_argument("--no-view", action="store_true")

    # diff
    d = sub.add_parser("diff", aliases=["d"], help="生成 HTML 差异对比")
    d.add_argument("before", help="原稿文件")
    d.add_argument("after", help="校后文件")
    d.add_argument("--output", "-o", help="输出路径 (默认 <原稿_stem>_diff.html)")
    d.add_argument("--no-view", action="store_true")

    # special
    s = sub.add_parser("special", aliases=["s"], help="汉字规范等专项检查")
    s.add_argument("file", help="要检查的文件")
    s.add_argument(
        "--check", default="tgscc", choices=["tgscc"],
        help="检查类型 (默认 tgscc)",
    )

    args = parser.parse_args()

    cmd_map = {
        "proofread": cmd_proofread,
        "p": cmd_proofread,
        "book": cmd_book,
        "b": cmd_book,
        "diff": cmd_diff,
        "d": cmd_diff,
        "special": cmd_special,
        "s": cmd_special,
    }
    fn = cmd_map.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
