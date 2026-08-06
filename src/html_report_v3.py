"""
V3 HTML 报告生成器 — 深色主题 · 原文内嵌高亮 · 章节分组

设计参考：前言_校对报告.html（用户提供的参考格式）。
完全独立于 html_report_v2.py，不修改旧报告逻辑。
"""

import html
import re
import time
from typing import List, Dict, Optional, Tuple


# ── 常量 ──────────────────────────────────────────────────────────────────

SEVERITY_MAP = {
    "error":  {"label": "必须修改", "cls": "high",   "badge": "badge-high"},
    "warn":   {"label": "建议修改", "cls": "medium", "badge": "badge-medium"},
    "info":   {"label": "供参考",   "cls": "low",    "badge": "badge-low"},
}

PHASE_SKILL_MAP = {
    "0a_tgscc":      "汉字规范",
    "0b_variant":    "异形词",
    "0c_structure":  "结构检查",
    "1_llm":         "AI审校",
    "2_names":       "专名查词",
}

CONTEXT_RADIUS = 60   # 错误词前后各取 60 字
MAX_CONTEXT = 200     # 上下文总长度上限


# ── 章节解析 ──────────────────────────────────────────────────────────────

def _build_chapter_map(text: str) -> List[Dict]:
    """解析全文 Markdown 标题，返回章节边界列表。

    Returns:
        [{"level": 1, "title": "技术垄断", "start": 0, "end": 500}, ...]
    """
    chapters: List[Dict] = []
    heading_re = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

    matches = list(heading_re.finditer(text))
    if not matches:
        # 全文无标题时作为一个整体
        chapters.append({"level": 0, "title": "全文", "start": 0, "end": len(text)})
        return chapters

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        start = m.start()
        # 结束位置为下一个同级或更高级标题的开始，或文末
        end = len(text)
        for j in range(i + 1, len(matches)):
            next_level = len(matches[j].group(1))
            if next_level <= level + 1:  # 同级或更高级
                end = matches[j].start()
                break
        chapters.append({"level": level, "title": title, "start": start, "end": end})

    return chapters


def _find_chapter_name(position: int, chapters: List[Dict], text: str) -> str:
    """根据字符位置找到所属章节名。"""
    if not chapters:
        return "全文"

    best = None
    for ch in chapters:
        if ch["start"] <= position < ch["end"]:
            # 找最近的低级别标题
            if best is None or ch["start"] > best["start"]:
                best = ch

    if best:
        return best["title"]

    # 在最后一个章节之后
    last = chapters[-1]
    if position >= last["end"]:
        return last["title"]

    return "全文"


# ── 上下文渲染 ────────────────────────────────────────────────────────────

def _render_context(finding: Dict, full_text: str) -> str:
    """从原文提取上下文，内嵌高亮错误区间。

    Returns:
        HTML 字符串，格式如：
        <span>前文...</span><span class="error-word">错误词</span><span>后文...</span>
    """
    loc = finding.get("location")
    error_text = finding.get("char") or finding.get("original") or ""
    if isinstance(error_text, list):
        error_text = "".join(str(x) for x in error_text)

    # 没有 location 的结构类 finding，直接返回消息文本
    if not loc or len(loc) != 2:
        # 结构类 finding 显示 message
        msg = finding.get("message", "") or error_text
        return f'<span class="error-word">{html.escape(str(msg)[:MAX_CONTEXT])}</span>'

    start, end = loc[0], loc[1]
    if start >= end or start >= len(full_text):
        return f'<span class="error-word">{html.escape(error_text)[:MAX_CONTEXT]}</span>'

    # 截取上下文
    ctx_start = max(0, start - CONTEXT_RADIUS)
    ctx_end = min(len(full_text), end + CONTEXT_RADIUS)

    before = full_text[ctx_start:start]
    error = full_text[start:end]
    after = full_text[end:ctx_end]

    # 清理和截断
    before = _clean_context(before, from_start=True)
    after = _clean_context(after, from_start=False)

    # 如果 error 文本包含换行，截断
    error = error.replace('\n', ' ').strip()

    parts = []
    if ctx_start > 0:
        parts.append("…")
    parts.append(f'<span>{html.escape(before)}</span>')
    parts.append(f'<span class="error-word">{html.escape(error)}</span>')
    parts.append(f'<span>{html.escape(after)}</span>')
    if ctx_end < len(full_text):
        parts.append("…")

    return "".join(parts)


def _clean_context(text: str, from_start: bool = True) -> str:
    """清理上下文文本：去换行、去多余空格。"""
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'\s+', ' ', text)
    if from_start:
        # 从上下文开头取最后 CONTEXT_RADIUS 字
        if len(text) > CONTEXT_RADIUS:
            text = text[-CONTEXT_RADIUS:]
    else:
        if len(text) > CONTEXT_RADIUS:
            text = text[:CONTEXT_RADIUS]
    return text.strip()


# ── 技能标签推断 ──────────────────────────────────────────────────────────

def _infer_skills(finding: Dict) -> str:
    """根据 finding 的 phase 和 type 推断技能标签。"""
    phase = finding.get("phase", "")
    ftype = finding.get("type", "")

    skills = set()

    # 从 phase 推断
    base = PHASE_SKILL_MAP.get(phase, "")
    if base:
        skills.add(base)

    # 从 type 细粒度推断
    if ftype == "variant_form":
        skills.add("异形词规范")
    elif ftype == "correction":
        skills.add("字词校对")
    elif "continuity" in ftype:
        skills.add("内容逻辑")
    elif "heading" in ftype:
        skills.add("结构诊断")

    # 从 severity 推断
    sev = finding.get("severity", "")
    if sev == "error":
        skills.add("出版规范")

    return "、".join(sorted(skills)) if skills else "审校"


def _infer_issue_type(finding: Dict) -> str:
    """推断显示用的问题类型标签。"""
    phase = finding.get("phase", "")
    ftype = finding.get("type", "")

    type_labels = {
        "variant_form": "异形词",
        "correction": "错别字/用词不当",
        "continuity_error": "编号连续性",
        "heading_skip": "标题层级",
        "heading_continuity": "标题连续性",
        "not_general_standard_kanji": "表外字/生僻字",
        "xingsheng_hanzi": "形声字问题",
    }
    if ftype in type_labels:
        return type_labels[ftype]
    if phase == "0a_tgscc":
        return "汉字规范"
    if phase == "0c_structure":
        return "结构诊断"
    return "审校发现"


# ── HTML 渲染 ─────────────────────────────────────────────────────────────

def _render_stats_cards(all_findings: List[Dict]) -> str:
    """渲染统计卡片。"""
    total = len(all_findings)
    err_count = sum(1 for f in all_findings if f.get("severity") == "error")
    warn_count = sum(1 for f in all_findings if f.get("severity") == "warn")
    info_count = sum(1 for f in all_findings if f.get("severity") == "info")

    return f"""    <div class="stats">
      <div class="stat-card">
        <div class="stat-num">{total}</div>
        <div class="stat-label">问题总数</div>
      </div>
      <div class="stat-card">
        <div class="stat-num sev-high">{err_count}</div>
        <div class="stat-label">必须修改</div>
      </div>
      <div class="stat-card">
        <div class="stat-num sev-medium">{warn_count}</div>
        <div class="stat-label">建议修改</div>
      </div>
      <div class="stat-card">
        <div class="stat-num sev-low">{info_count}</div>
        <div class="stat-label">供参考</div>
      </div>
    </div>"""


def _render_issue_item(finding: Dict, full_text: str, chapters: List[Dict]) -> str:
    """渲染单个问题卡片。"""
    sev_info = SEVERITY_MAP.get(finding.get("severity", "info"), SEVERITY_MAP["info"])
    skills = _infer_skills(finding)
    issue_type = _infer_issue_type(finding)

    # 定位章节
    loc = finding.get("location")
    if loc and len(loc) == 2:
        chapter_name = _find_chapter_name(loc[0], chapters, full_text)
    else:
        chapter_name = "结构诊断"

    # 位置信息
    if loc and len(loc) == 2:
        loc_str = f"字符 {loc[0]}-{loc[1]}"
    else:
        loc_str = "—"

    # 上下文高亮
    context_html = _render_context(finding, full_text)

    # 建议
    suggestion = finding.get("suggestion") or finding.get("suggested") or ""
    if isinstance(suggestion, list):
        suggestion = "".join(str(x) for x in suggestion)

    # 说明
    reason = finding.get("reason") or finding.get("basis") or finding.get("message") or ""

    # 振幅降级注释
    downgrade_note = ""
    if finding.get("p_match_method") == "fuzzy":
        downgrade_note = " ⚠️ 模糊定位，仅供人工确认。"
    if finding.get("ratio") and finding["ratio"] < 80:
        downgrade_note += f" 📐 匹配度 {finding['ratio']:.0f}%，请人工核对。"

    explanation = html.escape(str(reason))[:600] + downgrade_note

    # 如果没有 suggestion 但有 corrected_sentence，显示句子级建议
    if not suggestion:
        corrected = finding.get("corrected_sentence") or ""
        if corrected:
            suggestion = corrected

    return f"""        <div class="issue-item sev-{sev_info['cls']}">
          <div class="issue-header">
            <span class="badge {sev_info['badge']}">{sev_info['label']}</span>
            <span class="badge-skill">{html.escape(skills)}</span>
            <span class="issue-type">{html.escape(issue_type)} · {html.escape(loc_str)}</span>
          </div>
          <div class="error-text">
            {context_html}
          </div>
          <div class="suggestion"><span class="suggestion-label">✦ 建议：</span>{html.escape(suggestion)[:300]}</div>
          <div class="explanation">{html.escape(explanation)[:600]}</div>
        </div>"""


def _render_chapter_group(
    chapter_name: str, findings: List[Dict],
    full_text: str, chapters: List[Dict]
) -> str:
    """渲染一个章节组（标题 + 问题列表）。"""
    if not findings:
        return ""

    items = "\n".join(
        _render_issue_item(f, full_text, chapters) for f in findings
    )

    return f"""      <div class="chapter">
        <div class="chapter-name">
          {html.escape(chapter_name)}
          <span class="chapter-count">({len(findings)} 个问题)</span>
        </div>
        <div class="issue-list">
{items}
        </div>
      </div>"""


# ── 主函数 ────────────────────────────────────────────────────────────────

def generate_report(
    source_text: str,
    tgscc: List[Dict],
    variants: List[Dict],
    structure: List[Dict],
    llm: List[Dict],
    names: Optional[List[Dict]] = None,
    align_stats: Optional[Dict] = None,
    output_path: str = "",
    docname: str = "",
    model: str = "",
    extra: str = "",
) -> str:
    """生成 V3 版深色主题审校报告。

    Args:
        source_text: 完整原文（用于内嵌高亮上下文定位）
        tgscc: TGSCC 汉字规范 findings
        variants: 异形词 findings
        structure: 结构检查 findings
        llm: LLM 审校 findings
        names: 专名查词 findings（可选）
        align_stats: 对齐统计（可选，保留兼容）
        output_path: 输出文件路径
        docname: 文档名
        model: 使用的模型名
        extra: 额外信息字符串

    Returns:
        输出文件路径
    """
    names = names or []
    all_findings = tgscc + variants + structure + llm + names
    chapters = _build_chapter_map(source_text)

    # ── 按章节分组 ──
    chapter_groups: Dict[str, List[Dict]] = {}
    unassigned: List[Dict] = []

    for f in all_findings:
        loc = f.get("location")
        if loc and len(loc) == 2 and loc[0] < loc[1]:
            ch = _find_chapter_name(loc[0], chapters, source_text)
        else:
            ch = "结构诊断"
        if ch:
            chapter_groups.setdefault(ch, []).append(f)
        else:
            unassigned.append(f)

    if unassigned:
        chapter_groups["其他"] = unassigned

    # ── 渲染各部分 ──
    stats_html = _render_stats_cards(all_findings)

    chapter_sections = []
    # 保持章节顺序
    seen_chapters = set()
    for ch in chapters:
        name = ch["title"]
        if name in chapter_groups and name not in seen_chapters:
            seen_chapters.add(name)
            chapter_sections.append(
                _render_chapter_group(name, chapter_groups[name], source_text, chapters))

    # 未匹配的章节（如结构诊断）
    for name, items in chapter_groups.items():
        if name not in seen_chapters:
            seen_chapters.add(name)
            chapter_sections.append(
                _render_chapter_group(name, items, source_text, chapters))

    chapters_html = "\n".join(chapter_sections)

    # ── 完整 HTML ──
    now_str = time.strftime("%Y/%m/%d %H:%M:%S")

    report = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(docname)} · 审校报告</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: #0f1219; color: #e2e6f3; line-height: 1.6; padding: 20px;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; }}

    /* 头部 */
    .header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
    .title {{ font-size: 20px; font-weight: 700; flex: 1; }}
    .export-info {{ font-size: 13px; color: #8b91a8; }}

    /* 统计卡片 */
    .stats {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
    .stat-card {{
      flex: 1; min-width: 150px; text-align: center; padding: 16px;
      background: #1a1e2e; border-radius: 8px; border: 1px solid #2e3348;
    }}
    .stat-num {{ font-size: 32px; font-weight: 700; }}
    .stat-label {{ font-size: 13px; color: #8b91a8; margin-top: 4px; }}
    .sev-high {{ color: #ef4444; }}
    .sev-medium {{ color: #f59e0b; }}
    .sev-low {{ color: #3b82f6; }}

    /* 章节 */
    .chapter {{ margin-bottom: 24px; }}
    .chapter-name {{
      font-size: 16px; font-weight: 600; padding: 12px 16px;
      background: #1a1e2e; border-radius: 8px; margin-bottom: 12px;
      border-left: 4px solid #5b7cff;
    }}
    .chapter-count {{ font-size: 13px; color: #8b91a8; margin-left: 8px; }}

    /* 问题项 */
    .issue-list {{ display: flex; flex-direction: column; gap: 12px; }}
    .issue-item {{
      border: 1px solid #2e3348; border-left: 4px solid #2e3348;
      border-radius: 8px; padding: 16px; background: #22263a;
    }}
    .issue-item.sev-high {{ border-left-color: #ef4444; }}
    .issue-item.sev-medium {{ border-left-color: #f59e0b; }}
    .issue-item.sev-low {{ border-left-color: #3b82f6; }}

    .issue-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }}
    .badge {{
      font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 5px;
    }}
    .badge-high {{ background: rgba(239,68,68,.15); color: #ef4444; }}
    .badge-medium {{ background: rgba(245,158,11,.15); color: #f59e0b; }}
    .badge-low {{ background: rgba(59,130,246,.15); color: #3b82f6; }}
    .badge-skill {{
      background: rgba(91,124,255,.12); border: 1px solid rgba(91,124,255,.3);
      color: #7b9fff; font-size: 12px; padding: 2px 8px; border-radius: 4px;
    }}
    .issue-type {{ font-size: 13px; color: #8b91a8; margin-left: auto; }}

    .error-text {{
      font-size: 15px; background: #1a1d27; border-radius: 6px;
      padding: 10px 14px; margin-bottom: 10px;
    }}
    .error-word {{
      background: rgba(239,68,68,.25); color: #fca5a5;
      border-radius: 3px; padding: 1px 5px; font-weight: 600;
    }}
    .suggestion {{ font-size: 14px; color: #86efac; margin-bottom: 8px; }}
    .suggestion-label {{ color: #8b91a8; }}
    .explanation {{ font-size: 13px; color: #8b91a8; }}

    /* 空状态 */
    .empty {{ text-align: center; padding: 40px; color: #8b91a8; }}

    /* 元信息 */
    .meta {{ font-size: 13px; color: #5c6278; margin-bottom: 20px; }}

    /* 响应式 */
    @media (max-width: 768px) {{
      .stats {{ flex-direction: column; }}
      .stat-card {{ min-width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1 class="title">{html.escape(docname)} · 审校报告</h1>
      <span class="export-info">导出时间：{now_str}</span>
    </div>

    <div class="meta">
      模型：{html.escape(model or '—')} · 各阶段发现：TGSCC {len(tgscc)} + 异形词 {len(variants)} + 结构 {len(structure)} + AI审校 {len(llm)} + 专名 {len(names)} = 总计 {len(all_findings)} 条
    </div>

{stats_html}

{chapters_html if chapters_html else '<div class="empty">未发现问题 🎉</div>'}

  </div>
</body>
</html>"""

    if output_path:
        import os
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)

    return output_path


# ── 兼容旧接口 ────────────────────────────────────────────────────────────

def phase4_report_v3(
        out_dir: str, docname: str, source_text: str,
        tgscc: List[Dict], variants: List[Dict], structure: List[Dict],
        llm: List[Dict], align: Optional[Dict] = None,
        names: Optional[List[Dict]] = None,
        model: str = "", extra: str = "") -> str:
    """兼容 max_pipeline.phase4_report() 签名的新版报告生成器。"""
    import os
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{docname}_max_report.html")
    return generate_report(
        source_text=source_text,
        tgscc=tgscc, variants=variants, structure=structure,
        llm=llm, names=names, align_stats=align.get("stats") if align else None,
        output_path=out_path, docname=docname, model=model, extra=extra,
    )
