#!/usr/bin/env python3
"""
ai-proofread CLI — 终端直接调用审校管线，无需 VSCode。

pip install -e . 后全局可用:
    proofread p <file.md|file.docx>             # 单文件校对 (自动转 .docx → .md)
    proofread b <file.md|file.docx>             # 全书分块校对
    proofread m <file.md|file.docx>             # ★ 最大化检查（全环节）
    proofread w <file.docx>                     # DOCX 修订+批注回写
    proofread d <原稿.md> <校后.md>               # 生成 HTML diff
    proofread s <file.md>                       # 汉字规范专项检查

所有审校模型使用 DeepSeek V4 Flash (deepseek-v4-flash)。

输出:
    <stem>.proofread.md              校对后文件
    <stem>_refined.md                精修版全文（max 模式）
    <stem>_max_report.html           综合报告（max 模式）
    <stem>_alignment.html            句子级对齐勘误表（max 模式）
    <stem>_审阅版.docx               DOCX 修订+批注回写版
"""

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional
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
            target_text = c.get("target", "").strip()
            first = target_text[:30].splitlines()[0] if target_text else "(空)"
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


# ── 子命令: 最大化检查模式 ───────────────────────────────────────────


def cmd_max(args):
    """最大化检查：确定性检查 + LLM 审校 + 句子对齐 + 综合报告。"""
    from .max_pipeline import run_max

    results = run_max(
        args.file, model=args.model, concurrent=args.concurrent,
        rpm=args.rpm, run_names=args.names, verbose=args.verbose)

    # 自动打开 master 报告
    if not args.no_view and results.get("report_path"):
        url = f"file://{Path(results['report_path']).resolve()}"
        webbrowser.open(url)
        print(f"🌐 报告已打开: {url}")

    # 可选回写
    if args.writeback:
        docx_path = _find_source_docx(args.file)
        if docx_path:
            _do_writeback(docx_path, results, args.author)


# ── 子命令: DOCX 回写 ─────────────────────────────────────────────────


def _find_source_docx(file_arg: str) -> Optional[str]:
    """根据输入参数（.docx 或已转换的 .md）找到源 docx 路径。"""
    fpath = Path(file_arg)
    if fpath.suffix.lower() == ".docx" and fpath.exists():
        return str(fpath)
    # 尝试同目录下同名 .docx
    stem = fpath.parent / fpath.stem
    for ext in (".docx", ".doc"):
        candidate = stem.parent / f"{stem.name}{ext}"
        if candidate.exists():
            return str(candidate)
    return None


def _do_writeback(docx_path: str, findings_data: dict, author: str = "审校助手"):
    """执行 DOCX 回写：生成 Adeu 命令文件。"""
    from .writeback import load_findings, findings_to_adeu_changes, _write_adeu_batch_script

    # 收集所有阶段的发现
    all_findings = []
    for key in ("tgscc", "variants", "structure", "llm", "names", "findings"):
        batch = findings_data.get(key, [])
        if isinstance(batch, list):
            all_findings.extend(batch)
    # 也接受直接的 issues[] 格式
    if isinstance(findings_data, list):
        all_findings = findings_data

    if not all_findings:
        print("⚠️  没有可回写的发现")
        return

    print(f"\n📋 回写 {len(all_findings)} 条发现...")
    changes = findings_to_adeu_changes(all_findings)

    output_path = str(Path(docx_path).parent / f"{Path(docx_path).stem}_审阅版.docx")
    script_path = _write_adeu_batch_script(docx_path, output_path, changes, author)

    print(f"📄 Adeu 批处理命令: {script_path}")
    print(f"📄 目标输出: {output_path}")
    print(f"💡 下一步: proofread w --apply 应用修订到 DOCX")


def cmd_writeback(args):
    """DOCX 修订+批注回写。"""
    from .writeback import load_findings, writeback_adeu

    # 查找源 docx
    docx_path = args.docx if Path(args.docx).exists() else _find_source_docx(args.docx)
    if not docx_path:
        print(f"❌ 找不到源文档: {args.docx}")
        sys.exit(1)

    # 自动推断 findings
    findings_path = args.findings
    if findings_path is None:
        d = Path(docx_path).parent
        stem = Path(docx_path).stem
        candidates = [
            str(d / f"{stem}_max_results.json"),
            str(d / f"{stem}_findings.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                findings_path = c
                break
        if findings_path is None:
            print("❌ 未指定 --findings，自动搜索也找不到")
            sys.exit(1)

    findings = load_findings(findings_path)
    print(f"📥 加载 {len(findings)} 条发现")

    if not findings:
        print("❌ 没有发现数据")
        sys.exit(1)

    if args.apply:
        _apply_adeu_writeback(findings_path)
    else:
        changes = writeback_adeu(docx_path, findings, output_path=args.out, author=args.author)
        print(f"\n✅ 回写命令已生成，执行 proofread w --apply 应用修订")


def _apply_adeu_writeback(commands_path: str):
    """读取 Adeu 命令文件并执行 MCP process_document_batch（需在 agent 上下文中）。"""
    with open(commands_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    print(f"📋 准备执行 Adeu 批处理 ({len(payload['changes'])} 条变更)...")
    print(f"   源文档: {payload['source_docx']}")
    print(f"   输出: {payload['output_path']}")
    print()
    print("💡 Adeu MCP 调用需在 Claude Code agent 上下文中完成。")
    print("   请将以下命令文件交给 Claude Code agent:")
    print(f"   cat {commands_path} | proofread w --apply")
    # 实际执行需 mcp__adeu__process_document_batch 工具（仅在 agent 中可用）


# ── 主入口 ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="proofread",
        description="ai-proofread 命令行工具 — 终端直接调用审校，无需 VSCode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
子命令简写:  p=proofread  b=book  m=max  w=writeback  d=diff  s=special

示例:
  proofread p 我的稿件.docx
  proofread b 我的稿件.docx --concurrent 5
  proofread m 我的稿件.docx              # ★ 最大化检查（全环节）
  proofread m 我的稿件.docx --writeback  #   审校后自动回写 DOCX
  proofread w 我的稿件.docx              #   独立回写修订+批注
  proofread d 原稿.md 校后.md
  proofread s 我的稿件.md

默认模型: {DEFAULT_MODEL}（DeepSeek V4 Flash）
        """,
    )
    # 全局参数
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    parser.set_defaults(verbose=False)
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

    # max
    m = sub.add_parser("max", aliases=["m"], help="最大化检查（全环节打通）")
    m.add_argument("file", help="要检查的文件 (.md / .docx)")
    m.add_argument("--model", default=DEFAULT_MODEL, choices=AVAILABLE_MODELS)
    m.add_argument("--concurrent", type=int, default=3, help="LLM 并发数 (默认 3)")
    m.add_argument("--rpm", type=int, default=15, help="API 速率限制 (默认 15)")
    m.add_argument("--names", action="store_true", help="启用专名查词（MDict 词典）")
    m.add_argument("--writeback", action="store_true",
                    help="审校完成后自动回写 DOCX（修订+批注）")
    m.add_argument("--author", default="审校助手", help="修订作者名")
    m.add_argument("--no-view", action="store_true", help="不自动打开报告")

    # writeback
    w = sub.add_parser("writeback", aliases=["w"], help="DOCX 修订+批注回写")
    w.add_argument("docx", help="原始 Word 文档 (.docx)")
    w.add_argument("--findings", help="发现 JSON（自动搜索同目录 _max_results.json）")
    w.add_argument("--out", help="输出路径（默认 <stem>_审阅版.docx）")
    w.add_argument("--author", default="审校助手", help="修订作者名")
    w.add_argument("--apply", action="store_true",
                    help="实际执行 Adeu MCP 回写（需 Claude Code agent）")
    w.add_argument("--dry-run", action="store_true", help="仅导出命令，不执行")

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
        "max": cmd_max,
        "m": cmd_max,
        "writeback": cmd_writeback,
        "w": cmd_writeback,
    }
    fn = cmd_map.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
