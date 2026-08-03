#!/usr/bin/env python3
"""
writeback.py — ai-proofread DOCX 修订+批注回写引擎

将 max 管线（或任何发现 JSON）回写到 Word 文档，生成带修订标记和批注的 .docx。
默认使用 Adeu MCP 引擎（process_document_batch），批量一次调用完成。
遵循 proofreading-publish/HANDOFF.md 的工具链定论。

用法:
    # 方式 A：独立回写（已有 findings JSON）
    python3 src/writeback.py <docx_path> <findings.json> [--out 输出.docx] [--engine adeu|officecli]

    # 方式 B：通过 CLI
    proofread w <docx_path> [--findings findings.json] [--out 输出.docx]

    # 方式 C：max 管线自动回写
    proofread max <docx_path> --writeback

findings JSON 格式（由 max_pipeline.py run_max() 产出）:
{
    "tgscc": [{phase, type, char, suggestion, location, severity, confidence}, ...],
    "variants": [{phase, type, original, suggestion, location, severity, basis}, ...],
    "structure": [{phase, type, message, location, severity, extra}, ...],
    "llm": [{phase, type, original, suggestion, location, ratio, severity}, ...],
    "names": [{phase, type, original, suggestion, severity}, ...],  // optional
}
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── fix_class 映射规则 ────────────────────────────────────────────────
# 将 max pipeline 的 phase+severity 转换为 proofread-pub 的 fix_class 体系

ROUTING = {
    # phase (prefix match) → {severity → (fix_class, comment_prefix)}
    "0a_tgscc": {
        "error":   ("must_fix", "【必改·汉字】"),
        "warn":    ("polish",   "💬润色【汉字】"),
        "info":    ("verify",   "💬待核【汉字】"),
    },
    "0b_variant": {
        "error":   ("must_fix", "【必改·词形】"),
        "warn":    ("polish",   "💬润色【词形】"),
    },
    "0c_structure": {
        "error":   ("verify",   "💬待核【结构】"),
        "warn":    ("verify",   "💬待核【结构】"),
    },
    "1_llm": {
        "warn":    ("must_fix", "【必改·AI审校】"),
        "error":   ("must_fix", "【必改·AI审校】"),
        "info":    ("polish",   "💬润色【AI审校】"),
    },
    "2_names": {
        "info":    ("verify",   "💬待核【专名】"),
    },
}

_DEFAULT_ROUTE = ("verify", "💬待核【审校】")


def _route(phase: str, severity: str) -> tuple:
    """查路由表获取 (fix_class, comment_prefix)。"""
    for prefix, table in ROUTING.items():
        if phase.startswith(prefix):
            return table.get(severity, ("verify", "💬待核"))
    return _DEFAULT_ROUTE


# ── 发现 → Adeu changes 转换 ──────────────────────────────────────────


def _safe_text(finding: Dict) -> str:
    """从 finding 中提取原文（兼容各种字段名）。"""
    return (finding.get("original") or
            finding.get("char") or
            finding.get("message") or "").strip()


def _safe_suggestion(finding: Dict) -> str:
    """从 finding 中提取建议文本。

    特殊处理：TGSCC 的 suggestion 格式为 "借(38藉：读jí...)"——
    提取括号前的替换字（仅借），注释部分（括号内容）剥离到批注。
    """
    sug = (finding.get("suggestion") or "").strip()
    if not sug:
        return ""
    # 检测 TGSCC 格式："字(注释)"
    import re
    m = re.match(r'^(\S+)\((.+)\)$', sug)
    if m and finding.get("phase", "").startswith("0a_tgscc"):
        return m.group(1)  # 只取替换字
    return sug


def _tgscc_note(finding: Dict) -> str:
    """提取 TGSCC 注释（括号内容）。"""
    sug = (finding.get("suggestion") or "").strip()
    import re
    m = re.match(r'^(\S+)\((.+)\)$', sug)
    if m and finding.get("phase", "").startswith("0a_tgscc"):
        return m.group(2)
    return ""


def _is_single_char(finding: Dict) -> bool:
    """判断是否为单字符修正（TGSCC 汉字替换等，需上下文锚定）。"""
    text = _safe_text(finding)
    return len(text) <= 1


def findings_to_adeu_changes(findings: List[Dict]) -> List[Dict]:
    """将 max pipeline findings 转为 Adeu process_document_batch changes 列表。

    每条 change: {type: "modify", target_text, new_text, comment, match_mode}
    """
    changes: List[Dict] = []
    stats = {"must_fix": 0, "polish": 0, "verify": 0, "skipped": 0}

    for f in findings:
        phase = f.get("phase", "")
        text = _safe_text(f)
        suggestion = _safe_suggestion(f)
        severity = f.get("severity", "info")
        fix_class, prefix = _route(phase, severity)

        if not text:
            stats["skipped"] += 1
            continue

        # 结构类发现：跳过（逻辑诊断不适合文本查找）
        if phase.startswith("0c_structure"):
            stats["verify"] += 1  # 在 max report 中呈现
            continue

        # 单字符修正（TGSCC）：必须加前后各 1-2 字的上下文锚定
        tgscc_note = _tgscc_note(f)

        # 构建批注内容
        comment_lines = [f"{prefix}【{f.get('type','')}】"]
        if suggestion and suggestion != text:
            # 聚焦建议（"旧"→"新"）
            comment_lines.append(f"▶ 建议：{_focus_pair(text, suggestion)}")
        else:
            comment_lines.append(f"▶ 原文：{text}")
        if tgscc_note:
            comment_lines.append(f"◎ 用法说明：{tgscc_note}")
        if f.get("basis"):
            comment_lines.append(f"◎ 依据：{f['basis']}")
        if f.get("ratio"):
            # ratio 是 0-100 的整数或 0-1 的浮点数
            r = f['ratio']
            if r > 1:
                r = r / 100.0  # 标准化为 0-1
            comment_lines.append(f"◎ 置信度：{r:.0%}")

        change: Dict[str, Any] = {
            "type": "modify",
            "target_text": text,
            "comment": "\n".join(comment_lines),
            "match_mode": "first",
        }

        # 单字符修正（TGSCC 等）：降为 polish（仅批注不改文），
        # 因为 Adeu 通过 target_text 匹配会在全文范围命中所有同字位置，
        # 无法精确定位到具体的那一处。
        if _is_single_char(f) and fix_class == "must_fix":
            fix_class = "polish"
            change["comment"] = change["comment"].replace(
                "【必改·", "💬润色【", 1)
            stats["polish"] += 1
            stats["must_fix"] -= 1

        # 修订文本（must_fix 才改文）
        if fix_class == "must_fix" and suggestion and suggestion != text:
            change["new_text"] = suggestion
        else:
            change["new_text"] = text  # polish/verify：不改文

        changes.append(change)

    return changes


def _focus_pair(original: str, suggested: str) -> str:
    """生成聚焦建议对（"旧词"→"新词"），最多 40 字符。"""
    orig_short = original[:20] + ("…" if len(original) > 20 else "")
    sugg_short = suggested[:20] + ("…" if len(suggested) > 20 else "")
    return f"\"{orig_short}\"→\"{sugg_short}\""


# ── 发现加载（兼容多种格式）───────────────────────────────────────────


def load_findings(source: str) -> List[Dict]:
    """从多种格式中加载发现列表。

    支持的格式：
    1. 单一 findings JSON 文件（按 phase 分别存）
    2. 标准 issues[] 数组
    3. max pipeline run_max() 的完整结果 JSON
    """
    path = Path(source)
    if not path.exists():
        print(f"❌ 文件不存在: {source}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 格式 1：标准 issues[]
    if isinstance(data, list):
        return [{"phase": "imported", "severity": "warn",
                 "original": item.get("current", ""),
                 "suggestion": item.get("suggested", ""),
                 "type": item.get("issue_type", item.get("category", "未知")),
                 "fix_class": item.get("fix_class", "polish"),
                 "reason": item.get("reason", ""),
                 **item}
                for item in data]

    # 格式 2：dict 顶层（max pipeline 结果 / 含 phase 键的 findings）
    findings = []
    # 收集所有已知 phase
    for key in ("tgscc", "variants", "structure", "llm", "names"):
        batch = data.get(key, [])
        if isinstance(batch, list):
            findings.extend(batch)
    # 也检查顶级 issues
    if "issues" in data and isinstance(data["issues"], list):
        for item in data["issues"]:
            if not item.get("phase"):
                item["phase"] = "imported"
            findings.append(item)

    if not findings:
        print(f"⚠️  未找到可识别的发现数据")
    return findings


# ── Adeu 引擎回写（主力） ──────────────────────────────────────────────


def writeback_adeu(source_docx: str, findings: List[Dict],
                   output_path: Optional[str] = None,
                   author: str = "审校助手") -> str:
    """使用 Adeu MCP process_document_batch 批量回写修订+批注。

    Args:
        source_docx: 原始 docx 路径
        findings: 发现列表
        output_path: 输出路径（默认 <stem>_审阅版.docx）
        author: 修订作者名

    Returns:
        输出 docx 路径
    """
    if output_path is None:
        p = Path(source_docx)
        output_path = str(p.parent / f"{p.stem}_审阅版.docx")

    # 生成 changes
    changes = findings_to_adeu_changes(findings)
    if not changes:
        print("⚠️  没有可回写的发现")
        return output_path

    stats = {"must_fix": 0, "polish": 0, "verify": 0, "total": len(changes)}
    for c in changes:
        fmt = c.get("comment", "")
        if "【必改" in fmt:
            stats["must_fix"] += 1
        elif "润色" in fmt:
            stats["polish"] += 1
        else:
            stats["verify"] += 1

    print(f"📋 回写统计: {stats['total']} 条"
          f"（必改 {stats['must_fix']} + 润色 {stats['polish']} + 待核 {stats['verify']}）")

    # 调用 Adeu MCP process_document_batch
    # 通过 mcp__adeu__process_document_batch 工具（在本会话中可用)
    # 由于在脚本中无法直接调用 Claude Code 的 MCP 工具，
    # 改为输出 Adeu 命令文件供 Claude Code agent 执行
    batch_script = _write_adeu_batch_script(source_docx, output_path, changes, author)
    print(f"📄 Adeu 批处理脚本: {batch_script}")
    print(f"📄 输出路径: {output_path}")
    return output_path


def _write_adeu_batch_script(source_docx: str, output_path: str,
                              changes: List[Dict], author: str) -> str:
    """将 Adeu changes 序列化到 JSON 命令文件，供 Claude Code agent 消费。"""
    script_path = str(Path(source_docx).parent / "_writeback_commands.json")
    payload = {
        "source_docx": source_docx,
        "output_path": output_path,
        "author": author,
        "changes": changes,
        "engine": "adeu",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return script_path


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="ai-proofread DOCX 修订+批注回写引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
引擎说明:
  adeu        批量字符级修订+批注，1 次 MCP 调用完成（主力，推荐）
  officecli   保留既有修订叠加，适合多轮审校叠加场景

示例:
  python3 src/writeback.py 稿件.docx findings.json
  python3 src/writeback.py 稿件.docx max_results.json --out 稿件_审阅版.docx
  proofread w 稿件.docx                          # 自动定位 findings
        """,
    )
    ap.add_argument("docx", help="原始 Word 文档 (.docx)")
    ap.add_argument("findings", nargs="?", help="发现 JSON 文件路径")
    ap.add_argument("--out", help="输出路径（默认 <stem>_审阅版.docx）")
    ap.add_argument("--author", default="审校助手", help="修订作者名")
    ap.add_argument("--engine", default="adeu", choices=["adeu", "officecli"],
                    help="回写引擎 (默认 adeu)")
    ap.add_argument("--export", action="store_true",
                    help="仅导出 Adeu 命令 JSON，不实际执行")
    args = ap.parse_args()

    # 自动推断 findings 路径
    if args.findings is None:
        docx_path = Path(args.docx)
        candidates = [
            str(docx_path.parent / f"{docx_path.stem}_max_results.json"),
            str(docx_path.parent / f"findings.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                args.findings = c
                break
        if args.findings is None:
            print("❌ 未指定 findings JSON，自动搜索也找不到。"
                  f"尝试了: {', '.join(candidates)}")
            sys.exit(1)

    # 加载 findings
    findings = load_findings(args.findings)
    print(f"📥 加载 {len(findings)} 条发现"
          f"（来自 {Path(args.findings).name}）")

    if not findings:
        print("❌ 没有可用的发现数据")
        sys.exit(1)

    # 回写
    out = writeback_adeu(
        args.docx, findings,
        output_path=args.out,
        author=args.author)

    print(f"\n✅ 回写命令已生成")
    print(f"   源文档: {args.docx}")
    print(f"   输出: {out}")
    print(f"   下一步: 执行 proofread w --apply 应用修订")


if __name__ == "__main__":
    main()
