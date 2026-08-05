#!/usr/bin/env python3
"""
max_pipeline.py — ai-proofread 最大化检查模式

打通项目全部环节的完整审校管线。调用方式见 src/cli.py 的 `max` 子命令。

    Phase 0: 确定性检查（无 LLM，秒级）
      0a. TGSCC 汉字规范检查（通用规范汉字表，查繁体/异体/表外字）
      0b. 异形词/不规范词形检查（离线词典数据 xh7_compressed + yixingci）
      0c. 结构检查（标题层级 hierarchy_gap/level_mismatch + 编号连续性）

    Phase 1: LLM 审校 —— JSON 发现模式（核心新增）
      分块 → 异步并发 → 每块返回 [{original_sentence, corrected_sentence}]
      → match_similar_text 模糊定位回写 → 精修版全文（保持原格式，非全文重写）

    Phase 2: 专名查词（可选 --names，需 MDict 词典文件）
      LLM 识别专名 → query_mdx 查现汉/辞海 → 修正

    Phase 3: 句子对齐（原文 vs 精修版）
      锚点算法 + Jaccard n-gram → 句子级 HTML 勘误表（match/delete/insert/move）

    Phase 4: master 综合 HTML 报告
      聚合 0a/0b/0c/1/2/3 全部发现 → 单文件自包含报告

用法（经 CLI）:
    proofread max <file.docx|md> [--concurrent 5] [--model deepseek-v4-flash] [--names]
"""

import asyncio
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 数据路径 ──────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "reliable-proofreading-data"
_RES_DIR = Path(__file__).resolve().parent / "resource"

DICT_PATHS = {
    "xianhan": "/Users/kicker114/Downloads/辞典/常用词典/现代汉语词典第7版/现代汉语词典第7版.mdx",
    "cihai": "/Users/kicker114/Downloads/辞典/常用词典/汉语辞海.mdx",
}


# ── 通用工具 ──────────────────────────────────────────────────────────


def _json_extract(text: str) -> Optional[List[Dict]]:
    """从模型输出中稳健提取 JSON 数组（容忍 ```json 代码块、前后缀文字）。"""
    if not text:
        return None
    text = text.strip()
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 找到第一个 [ 和最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # 容错：尝试修复未闭合引号等（粗粒度）
    return None


def _save_findings(path: str, results: Dict[str, Any]) -> None:
    """将 max pipeline 各阶段发现序列化到 JSON，供 writeback 使用。"""
    export = {
        key: results.get(key, [])
        for key in ("tgscc", "variants", "structure", "llm", "names")
        if isinstance(results.get(key, []), list)
    }
    for key in ("source_path", "source_sha256"):
        value = results.get(key)
        if value:
            export[key] = value
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    print(f"  发现 JSON: {path}")


# ── P 编号文本池（与 writeback_engine.build_para_map 逐字节一致）──────

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _para_raw_text(p_elem) -> str:
    """Accept-view text of a w:p —— 含 w:ins 内文本，排除 w:del 祖先下文本。

    必须与 apply_corrections_02.build_para_map 的 _para_raw_text 逐字节一致，
    否则 findings 的 location:P{n} 在回写时会定位错位。
    """
    parts = []
    for t in p_elem.iter(f"{{{_W_NS}}}t"):
        if any(a.tag == f"{{{_W_NS}}}del" for a in t.iterancestors()):
            continue
        parts.append(t.text or "")
    return "".join(parts)


def _build_para_text_map(docx_path: str) -> Dict[int, str]:
    """按 02 引擎规则构建 {pn: text} 文本池。

    规则（与 writeback_engine.build_para_map 一致）：
      - body 直接子级 w:p：非空文本 → pn 从 0 递增
      - w:tbl 内每个单元格段落（.//w:p）→ 同样编号（一个单元格段落 = 一个 P）
      - 空段落跳过（不占 P 编号）
    """
    import zipfile
    from lxml import etree

    with zipfile.ZipFile(docx_path, "r") as z:
        doc_xml = etree.fromstring(z.read("word/document.xml"))
    body = doc_xml.find(f"{{{_W_NS}}}body")
    text_map: Dict[int, str] = {}
    pn = 0
    if body is None:
        return text_map
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = _para_raw_text(child)
            if t.strip():
                text_map[pn] = t
                pn += 1
        elif tag == "tbl":
            for row_p in child.findall(f".//{{{_W_NS}}}p"):
                t = _para_raw_text(row_p)
                if t.strip():
                    text_map[pn] = t
                    pn += 1
    return text_map


def _find_source_docx(file_arg: str) -> Optional[str]:
    """根据输入参数（.docx 或已转换的 .md）找到源 docx 路径。"""
    fpath = Path(file_arg)
    if fpath.suffix.lower() == ".docx" and fpath.exists():
        return str(fpath)
    stem = fpath.parent / fpath.stem
    for ext in (".docx",):
        candidate = stem.parent / f"{stem.name}{ext}"
        if candidate.exists():
            return str(candidate)
    return None


def _resolve_findings_to_p(findings: List[Dict], text_map: Dict[int, str]) -> List[Dict]:
    """把 finding 定位到具体 P 段落，补充 pn/current/location。

    策略：
      1. 有效显式 P 只在该段内解析，不跨段静默改址。
      2. 无显式 P 时，精确命中必须全书唯一。
      3. 模糊命中必须有唯一且明显领先的最高分。
      4. 重复、并列或未命中的 finding 跳过写回，仍保留在报告中。
    """
    from .special_checker.match_similar_text import find_best_match

    items = sorted(text_map.items())  # [(pn, text)]
    if not items:
        return findings

    resolved: List[Dict] = []
    for f in findings:
        key_text = (f.get("real_text") or f.get("current")
                    or f.get("original") or f.get("char") or "").strip()
        # 去除 markdown 标记残留（LLM 审校的是 MD，标题行可能带 # 前缀；
        # DOCX 段落文本无此标记，导致定位失败）
        key_text = re.sub(r"^#{1,6}\s*", "", key_text).strip()
        key_text = re.sub(r"^\s*[-*]\s+", "", key_text).strip()
        if not key_text:
            continue

        best = None
        requested_pn = f.get("pn")
        if not isinstance(requested_pn, int):
            location = str(f.get("location", "")).strip()
            match = re.fullmatch(r"P(\d+)", location)
            requested_pn = int(match.group(1)) if match else None

        if requested_pn is not None and requested_pn in text_map:
            ptext = text_map[requested_pn]
            if key_text in ptext:
                best = {"pn": requested_pn, "real_text": key_text,
                        "ratio": 100.0, "match_method": "exact"}
            elif len(key_text) >= 2:
                bm = find_best_match(ptext, key_text)
                if bm.get("real_text") and bm.get("ratio", 0) >= 60:
                    exact = _longest_common_substring(key_text, ptext)
                    anchor = exact if exact and len(exact) >= 2 else bm["real_text"]
                    best = {"pn": requested_pn, "real_text": anchor,
                            "ratio": bm["ratio"], "match_method": "fuzzy"}
            if best is None:
                print(f"  WARN: 显式 P{requested_pn} 未找到锚点，跳过写回: {key_text[:30]}")
                continue
        elif requested_pn is not None:
            print(f"  WARN: P{requested_pn} 超出文档范围，跳过写回: {key_text[:30]}")
            continue
        else:
            exact_candidates = [
                {"pn": pn, "real_text": key_text, "ratio": 100.0}
                for pn, ptext in items if key_text in ptext
            ]
            if len(exact_candidates) == 1:
                best = exact_candidates[0]
            elif len(exact_candidates) > 1:
                pages = ", ".join(f"P{item['pn']}" for item in exact_candidates[:8])
                print(f"  WARN: 锚点跨段重复（{pages}），跳过写回: {key_text[:30]}")
                continue

        # 无精确命中时做模糊定位，并要求最高分唯一领先。
        if best is None and requested_pn is None and len(key_text) >= 2:
            fuzzy_candidates = []
            for pn, ptext in items:
                bm = find_best_match(ptext, key_text)
                if bm.get("real_text") and bm.get("ratio", 0) >= 60:
                    exact = _longest_common_substring(key_text, ptext)
                    anchor = exact if exact and len(exact) >= 2 else bm["real_text"]
                    fuzzy_candidates.append({
                        "pn": pn, "real_text": anchor, "ratio": bm["ratio"],
                        "match_method": "fuzzy"})
            fuzzy_candidates.sort(key=lambda item: item["ratio"], reverse=True)
            if fuzzy_candidates:
                top = fuzzy_candidates[0]
                runner_up = fuzzy_candidates[1] if len(fuzzy_candidates) > 1 else None
                if runner_up and top["ratio"] - runner_up["ratio"] < 5:
                    print(f"  WARN: 模糊锚点并列，跳过写回: {key_text[:30]}")
                    continue
                best = top

        if best:
            f["pn"] = best["pn"]
            f["current"] = best["real_text"]
            f["location"] = f"P{best['pn']}"
            f["p_ratio"] = best["ratio"]
            f["p_match_method"] = best.get("match_method", "exact")
            resolved.append(f)
        else:
            print(f"  WARN: finding 未定位，跳过写回: {key_text[:30]}")
    return resolved


def _longest_common_substring(a: str, b: str) -> str:
    """返回 a 与 b 的最长公共子串（简单实现，用于提取精确锚点）。"""
    if not a or not b:
        return ""
    max_len = 0
    best = ""
    # 简化：以 a 的每个起点尝试 b.find 扩展（a 通常较短）
    for start in range(len(a)):
        lo, hi = start, len(a)
        while lo <= hi:
            mid = (lo + hi) // 2
            if a[start:mid + 1] in b:
                if mid - start + 1 > max_len:
                    max_len = mid - start + 1
                    best = a[start:mid + 1]
                lo = mid + 1
            else:
                hi = mid - 1
        if len(a) - start <= max_len:
            break
    return best


def _findings_to_issues(findings: List[Dict]) -> List[Dict]:
    """把 max pipeline 各阶段发现转成 writeback_engine 的 issues[] 格式。

    fix_class 路由：
      - TGSCC 单字（tgscc）→ polish（批注不改文，防同字误匹配）
      - 异形词/词形（variants）→ must_fix（字符级修订）
      - LLM 有实质修改 → must_fix；无修改 → verify
      - 结构诊断（structure）→ 跳过（在 HTML 报告呈现）
    """
    issues: List[Dict] = []
    for f in findings:
        phase = f.get("phase", "")
        current = (f.get("current") or f.get("char") or f.get("original") or "").strip()
        # 兼容 suggestion / suggested 两个字段名（max 用 suggestion，pub 用 suggested）
        suggested = (f.get("suggested") or f.get("suggestion") or "").strip()
        location = f.get("location") or f"P{f.get('pn', 0)}"
        reason = (f.get("reason") or f.get("original") or f.get("message") or "").strip()

        if phase.startswith("0c_structure"):
            continue  # 结构诊断在 max report 呈现，不写回 DOCX

        if f.get("p_match_method") == "fuzzy":
            issues.append({
                "fix_class": "verify", "location": location,
                "current": current, "suggested": suggested,
                "reason": "模糊定位，仅供人工确认。" + reason,
                "category": "定位待核",
            })
            continue

        if phase.startswith("0a_tgscc"):
            # 单字：剥离注释只取替换字
            import re as _re
            m = _re.match(r"^(\S+)\((.+)\)$", suggested)
            sug = m.group(1) if m else suggested
            issues.append({
                "fix_class": "polish", "location": location,
                "current": current, "suggested": sug,
                "reason": suggested, "category": "tgscc汉字",
            })
        elif phase.startswith("0b_variant"):
            issues.append({
                "fix_class": "must_fix", "location": location,
                "current": current, "suggested": suggested,
                "reason": reason or "异形词规范", "category": "词形规范",
            })
        elif phase.startswith("1_llm") or phase.startswith("2_names"):
            if suggested and suggested != current:
                issues.append({
                    "fix_class": "must_fix", "location": location,
                    "current": current, "suggested": suggested,
                    "reason": reason, "category": "ai审校",
                })
            else:
                issues.append({
                    "fix_class": "verify", "location": location,
                    "current": current, "suggested": "",
                    "reason": reason, "category": "ai审校",
                })
        else:
            issues.append({
                "fix_class": "polish", "location": location,
                "current": current, "suggested": suggested,
                "reason": reason, "category": "审校",
            })
    return issues


def _load_offline_data() -> Dict[str, Any]:
    data: Dict[str, Any] = {"variant_to_standard": {}, "variant_to_preferred": {}}
    # xh7_compressed.json（现汉第7版 压缩版）
    xh7_path = _DATA_DIR / "xh7_compressed.json"
    if xh7_path.exists():
        with open(xh7_path, "r", encoding="utf-8") as f:
            xh7 = json.load(f)
        data["variant_to_standard"].update(xh7.get("variant_to_standard", {}))
        data["variant_to_preferred"].update(xh7.get("variant_to_preferred_single", {}))
        data["variant_to_preferred"].update(xh7.get("variant_to_preferred_multi", {}))
    # yixingci_data.json（国家语委第一批异形词整理表，权威层）
    yx_path = _DATA_DIR / "yixingci_data.json"
    if yx_path.exists():
        with open(yx_path, "r", encoding="utf-8") as f:
            yx = json.load(f)
        data["variant_to_standard"].update(yx.get("variant_to_standard", {}))
    return data


# ── Phase 0a: TGSCC 汉字规范 ──────────────────────────────────────────


def phase0_tgscc(text: str) -> List[Dict]:
    """TGSCC 通用规范汉字表检查（确定性，含繁体/异体/表外字）。"""
    from .special_checker import check_to_tgscc

    results = check_to_tgscc(text)
    out = []
    for r in results:
        # 表外字（not_general_standard_kanji）多为生僻字，降为提示级
        severity = "error" if r.error_type != "not_general_standard_kanji" else "info"
        out.append({
            "phase": "0a_tgscc",
            "type": r.error_type,
            "char": r.original_text,
            "suggestion": r.suggestion,
            "location": list(r.location) if r.location else None,
            "severity": severity,
            "confidence": r.confidence,
        })
    return out


# ── Phase 0b: 异形词/不规范词形 ───────────────────────────────────────


def phase0_variants(text: str) -> List[Dict]:
    """扫描文本中的不规范词形/异形词（离线词典确定性匹配）。"""
    data = _load_offline_data()
    findings: List[Dict] = []
    seen = set()

    for variant, standard in data["variant_to_standard"].items():
        if not variant or len(variant) < 2:
            continue  # 单字变体交给 TGSCC 处理，避免人名/专名误报
        pos = 0
        while True:
            idx = text.find(variant, pos)
            if idx == -1:
                break
            # 避免重复：同一位置已由更长词形覆盖
            key = (idx, variant)
            if key not in seen:
                seen.add(key)
                findings.append({
                    "phase": "0b_variant",
                    "type": "variant_form",
                    "original": variant,
                    "suggestion": standard,
                    "location": [idx, idx + len(variant)],
                    "severity": "error" if len(variant) >= 3 else "warn",
                    "confidence": 1.0,
                    "basis": "现代汉语词典/异形词表",
                })
            pos = idx + 1

    return findings


# ── Phase 0c: 结构检查 ────────────────────────────────────────────────


def phase0_structure(text: str, rules_path: Optional[str] = None) -> List[Dict]:
    """结构检查：标题层级 + 编号连续性（确定性）。"""
    if rules_path is None:
        rules_path = str(_PROJECT_ROOT / "src" / "structure_checker" / "rules.example.json")
    from .structure_checker.check_structure import check_text_with_rules

    result = check_text_with_rules(text, rules_path)
    out = []
    for d in result.diagnostics:
        out.append({
            "phase": "0c_structure",
            "type": d.kind,
            "message": d.message,
            "location": list(d.position),
            "severity": "error" if d.kind == "continuity_error" else "warn",
            "confidence": 1.0,
            "extra": d.extra,
        })
    return out


# ── Phase 1: LLM JSON 发现模式 ────────────────────────────────────────


async def _proofread_one_json(
        chunk: Dict, index: int, total: int, model: str, rate_limiter) -> Optional[Dict]:
    """处理单个 chunk，返回 {index, findings, chunk_text}。"""
    from .proofreader import deepseek, load_system_prompt

    target_text = chunk.get("target", "")
    context_text = chunk.get("context", "")
    reference_text = chunk.get("reference", "")

    pre_text = ""
    if reference_text:
        pre_text += f"<reference>\n{reference_text}\n</reference>"
    if context_text and context_text.strip() != target_text.strip():
        pre_text += f"\n<context>\n{context_text}\n</context>"
    post_text = f"<target>\n{target_text}\n</target>"

    await rate_limiter.wait()
    sys_prompt = load_system_prompt("json")
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: deepseek(post_text, pre_text, model, system_prompt=sys_prompt))

    if not result:
        print(f"  ⚠️  chunk {index}/{total}: 返回空", file=sys.stderr)
        return None

    findings = _json_extract(result)
    print(f"  ✓ chunk {index}/{total}: 发现 {len(findings) if findings else 0} 条")
    # index 用 0-based（供 refined_chunks 回写），显示时已加 1
    return {"index": index - 1, "findings": findings, "chunk_text": target_text}


async def phase1_json_proofread(
        chunks: List[Dict], model: str = "deepseek-v4-flash",
        concurrent: int = 3, rpm: int = 15) -> Dict:
    """异步并发 JSON 发现模式审校。返回 {findings, refined_chunks}。"""
    from .proofreader import RateLimiter

    rate_limiter = RateLimiter(rpm)
    semaphore = asyncio.Semaphore(concurrent)

    async def worker(idx: int, chunk: Dict) -> Optional[Dict]:
        async with semaphore:
            return await _proofread_one_json(chunk, idx + 1, len(chunks), model, rate_limiter)

    tasks = [worker(i, c) for i, c in enumerate(chunks)]
    results = await asyncio.gather(*tasks)

    all_findings: List[Dict] = []
    refined_chunks = [c.get("target", "") for c in chunks]
    for r in results:
        if r is None:
            continue
        # 回写：对每个 {original, corrected} 用模糊匹配定位
        chunk_text = r["chunk_text"]
        new_text = chunk_text
        for item in (r["findings"] or []):
            original = (item or {}).get("original_sentence") or ""
            corrected = (item or {}).get("corrected_sentence") or ""
            if not original or not corrected or original == corrected:
                continue
            from .special_checker.match_similar_text import find_best_match
            bm = find_best_match(chunk_text, original, modified=corrected)
            if bm["location"] and bm["ratio"] >= 60:
                start, end = bm["location"]
                new_text = new_text[:start] + corrected + new_text[end:]
                all_findings.append({
                    "phase": "1_llm",
                    "type": "correction",
                    "original": original,
                    "suggestion": corrected,
                    "real_text": bm["real_text"],  # 实际匹配到的原文（供 P 映射）
                    "location": [start, end],
                    "ratio": bm["ratio"],
                    "severity": "warn",
                    "confidence": 0.7,
                })
        refined_chunks[r["index"]] = new_text

    return {"findings": all_findings, "refined_chunks": refined_chunks}


# ── Phase 2: 专名查词（可选） ─────────────────────────────────────────


def phase2_names(text: str, model: str = "deepseek-v4-flash") -> List[Dict]:
    """LLM 识别专名 + MDict 词典查证。"""
    from .lookup_mdict import deepseek as names_deepseek

    print("  📖 专名查词（LLM 识别 → 词典核验）...")
    result = names_deepseek(text)
    if not result:
        return []
    return [{
        "phase": "2_names",
        "type": "name_check",
        "original": text,
        "suggestion": result,
        "severity": "info",
        "confidence": 0.5,
    }]


# ── Phase 3: 句子对齐 ─────────────────────────────────────────────────


def phase3_align(original_text: str, refined_text: str, output_base: str) -> Dict:
    """原文 vs 精修版 句子级对齐，生成 HTML 勘误表。"""
    from .splitter import split_chinese_sentences_with_line_numbers
    from .sentence_aligner import align_sentences_anchor, get_alignment_statistics
    from .html_report_v2 import save_html_report_stage1

    s_a = split_chinese_sentences_with_line_numbers(original_text)
    s_b = split_chinese_sentences_with_line_numbers(refined_text)
    sentences_a = [s for s, _, _ in s_a if s]
    sentences_b = [s for s, _, _ in s_b if s]
    line_a = [sl for _, sl, _ in s_a]
    line_b = [sl for _, sl, _ in s_b]

    alignment = align_sentences_anchor(
        sentences_a, sentences_b,
        window_size=10, similarity_threshold=0.6, ngram_size=2)

    # 添加行号
    for item in alignment:
        if item.get("a_indices"):
            idx = item["a_indices"][0]
            if idx < len(line_a):
                item["a_line_number"] = line_a[idx]
                item["a_line_numbers"] = [line_a[i] for i in item["a_indices"] if i < len(line_a)]
        if item.get("b_indices"):
            idx = item["b_indices"][0]
            if idx < len(line_b):
                item["b_line_number"] = line_b[idx]
                item["b_line_numbers"] = [line_b[i] for i in item["b_indices"] if i < len(line_b)]

    stats = get_alignment_statistics(alignment)
    html_path = f"{output_base}_alignment.html"
    save_html_report_stage1(
        alignment, html_path,
        title_a="原文", title_b="精修版",
        runtime=0.0, stats=stats,
        algorithm_name="锚点算法", threshold=0.6, ngram_size=2)

    return {"alignment": alignment, "stats": stats, "html": html_path}


# ── Phase 4: master 综合报告 ──────────────────────────────────────────


def _render_finding_rows(findings: List[Dict]) -> str:
    if not findings:
        return "<tr><td colspan='5' class='empty'>无发现</td></tr>"
    rows = []
    for f in findings:
        loc = f.get("location") or []
        loc_str = f"{loc[0]}-{loc[1]}" if len(loc) == 2 else "-"
        sev_class = {"error": "sev-err", "warn": "sev-warn", "info": "sev-info"}.get(
            f.get("severity", "info"), "sev-info")
        orig = html.escape(str(f.get("original", "") or f.get("char", "") or f.get("message", ""))[:80])
        sugg = html.escape(str(f.get("suggestion", "") or "")[:120])
        rows.append(
            f"<tr class='{sev_class}'><td>{html.escape(str(f.get('type', '')))}</td>"
            f"<td>{loc_str}</td><td>{orig}</td><td>{sugg}</td>"
            f"<td>{html.escape(str(f.get('basis', '') or ''))}</td></tr>")
    return "\n".join(rows)


def phase4_report(
        out_dir: str, docname: str,
        tgscc: List[Dict], variants: List[Dict], structure: List[Dict],
        llm: List[Dict], align: Dict, extra: Optional[str] = "") -> str:
    """生成自包含 master HTML 报告，聚合全部阶段发现。"""
    def count(items): return len(items)
    def err_count(items): return sum(1 for i in items if i.get("severity") == "error")

    stats_cards = [
        ("TGSCC 汉字规范", count(tgscc), err_count(tgscc)),
        ("异形词/词形", count(variants), err_count(variants)),
        ("结构诊断", count(structure), err_count(structure)),
        ("LLM 修正", count(llm), 0),
        ("对齐差异", align.get("stats", {}).get("total", 0),
         align.get("stats", {}).get("delete", 0) + align.get("stats", {}).get("insert", 0)),
    ]

    cards = "".join(
        f"<div class='card'><div class='num'>{n}</div>"
        f"<div class='label'>{t}</div><div class='err'>{e} 处错误</div></div>"
        for t, n, e in stats_cards)

    align_stats = align.get("stats", {})
    align_html = os.path.basename(align.get("html", ""))
    stats_line = " · ".join(f"{k}: {v}" for k, v in align_stats.items()) or "-"

    report = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>最大化审校报告 — {html.escape(docname)}</title>
<style>
:root {{ --bg:#f7f8fa; --card:#fff; --line:#e4e7ec; --err:#d03050; --warn:#b58900; --info:#6b7280; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; background:var(--bg); color:#1f2329; line-height:1.6; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:24px; }}
h1 {{ font-size:22px; margin-bottom:4px; }}
.sub {{ color:#6b7280; font-size:13px; margin-bottom:20px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:24px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px; }}
.card .num {{ font-size:26px; font-weight:700; }}
.card .label {{ font-size:13px; color:#4b5563; }}
.card .err {{ font-size:12px; color:var(--err); }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px 20px; margin-bottom:16px; }}
h2 {{ font-size:16px; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--line); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #f0f1f3; vertical-align:top; }}
th {{ background:#fafafa; font-weight:600; }}
.sev-err td:first-child {{ color:var(--err); font-weight:600; }}
.sev-warn td:first-child {{ color:var(--warn); }}
.empty {{ color:#9ca3af; text-align:center; }}
a {{ color:#1f5eff; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.badge {{ display:inline-block; background:#eef2ff; color:#4f46e5; border-radius:4px; padding:1px 8px; font-size:12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📋 最大化审校报告</h1>
  <div class="sub">文件：{html.escape(docname)} · 生成时间：{time.strftime("%Y-%m-%d %H:%M")} {extra}</div>

  <div class="cards">{cards}</div>

  <section>
    <h2>0a · TGSCC 汉字规范检查 <span class="badge">确定性</span></h2>
    <table><tr><th>类型</th><th>位置</th><th>原文</th><th>建议</th><th>依据</th></tr>
    {_render_finding_rows(tgscc)}</table>
  </section>

  <section>
    <h2>0b · 异形词 / 不规范词形 <span class="badge">确定性</span></h2>
    <table><tr><th>类型</th><th>位置</th><th>原文</th><th>建议</th><th>依据</th></tr>
    {_render_finding_rows(variants)}</table>
  </section>

  <section>
    <h2>0c · 结构检查 <span class="badge">确定性</span></h2>
    <table><tr><th>类型</th><th>位置</th><th>原文</th><th>建议</th><th>依据</th></tr>
    {_render_finding_rows(structure)}</table>
  </section>

  <section>
    <h2>1 · LLM 审校（JSON 发现模式） <span class="badge">模型</span></h2>
    <table><tr><th>类型</th><th>位置</th><th>原句</th><th>修正后</th><th>相似度</th></tr>
    {_render_finding_rows(llm)}</table>
  </section>

  <section>
    <h2>3 · 句子对齐 <span class="badge">锚点算法</span></h2>
    <p>统计：{html.escape(stats_line)}</p>
    <p><a href="{html.escape(align_html)}" target="_blank">📄 打开句子级对齐勘误表 →</a></p>
  </section>

</div>
</body>
</html>"""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{docname}_max_report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    return out_path


# ── 主流程 ────────────────────────────────────────────────────────────


def run_max(
        file_path: str, model: str = "deepseek-v4-flash",
        concurrent: int = 3, rpm: int = 15,
        run_names: bool = False, verbose: bool = False,
        writeback: bool = False, author: str = "审校助手") -> Dict:
    """执行完整 max 管线。返回各阶段结果与产物路径。

    writeback=True 时，审校完成后直接把发现回写到 DOCX（02 引擎，字符级修订+批注）。
    """
    from .cli import _resolve_input  # 复用 DOCX→MD 转换
    from .extract_source import sha256_file
    from .splitter import split_markdown_by_title_and_length_with_context

    fpath = Path(file_path)
    src_docx = _find_source_docx(file_path)
    src_docx_sha256 = sha256_file(src_docx) if src_docx else None
    md_path = _resolve_input(fpath)
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    # 源 docx 路径（用于 P 编号映射和回写）
    text_map: Dict[int, str] = {}
    if src_docx:
        text_map = _build_para_text_map(src_docx)
        print(f"源 DOCX: {Path(src_docx).name} ({len(text_map)} 段)")

    out_dir = str(md_path.parent)
    docname = md_path.stem
    results: Dict[str, Any] = {}
    if src_docx and src_docx_sha256:
        results["source_path"] = str(Path(src_docx).resolve())
        results["source_sha256"] = src_docx_sha256

    print("╔════════════════════════════════════════════╗")
    print("║  ai-proofread · 最大化检查模式             ║")
    print("╚════════════════════════════════════════════╝")
    print(f"输入: {md_path.name} ({len(text)} 字)")

    # ── Phase 0: 确定性检查 ──
    print("\n[Phase 0] 确定性检查...")
    print("  0a. TGSCC 汉字规范...")
    t0 = time.time()
    tgscc = phase0_tgscc(text)
    print(f"      ✓ {len(tgscc)} 条 ({(time.time()-t0)*1000:.0f}ms)")
    results["tgscc"] = tgscc

    print("  0b. 异形词/词形...")
    t0 = time.time()
    variants = phase0_variants(text)
    print(f"      ✓ {len(variants)} 条 ({(time.time()-t0)*1000:.0f}ms)")
    results["variants"] = variants

    print("  0c. 结构检查...")
    t0 = time.time()
    structure = phase0_structure(text)
    print(f"      ✓ {len(structure)} 条 ({(time.time()-t0)*1000:.0f}ms)")
    results["structure"] = structure

    # ── Phase 1: LLM JSON 发现模式 ──
    print(f"\n[Phase 1] LLM 审校（JSON 发现模式，模型={model}）...")
    print("  分块...")
    chunks = split_markdown_by_title_and_length_with_context(
        text, levels=[1, 2], cut_by=200)
    print(f"      ✓ {len(chunks)} 块")

    print("  异步并发审校...")
    t0 = time.time()
    llm_result = asyncio.run(phase1_json_proofread(
        chunks, model=model, concurrent=concurrent, rpm=rpm))
    llm = llm_result["findings"]
    refined_text = "\n".join(llm_result["refined_chunks"])
    print(f"      ✓ {len(llm)} 条修正 ({(time.time()-t0):.0f}s)")
    results["llm"] = llm

    # 精修版落盘
    refined_path = os.path.join(out_dir, f"{docname}_refined.md")
    with open(refined_path, "w", encoding="utf-8") as f:
        f.write(refined_text)
    print(f"  精修版: {refined_path}")
    results["refined_path"] = refined_path

    # findings 文件在 Phase 2 后统一保存，确保 names 和源文件哈希不遗漏。
    findings_path = os.path.join(out_dir, f"{docname}_max_results.json")
    results["findings_path"] = findings_path

    # ── Phase 2: 专名查词（可选） ──
    names = []
    if run_names:
        print("\n[Phase 2] 专名查词...")
        names = phase2_names(refined_text, model=model)
    results["names"] = names
    _save_findings(findings_path, results)

    # ── Phase 3: 句子对齐 ──
    print("\n[Phase 3] 句子对齐...")
    t0 = time.time()
    align_base = os.path.join(out_dir, docname)
    align = phase3_align(text, refined_text, align_base)
    print(f"      ✓ 匹配={align['stats'].get('match')} "
          f"删除={align['stats'].get('delete')} 新增={align['stats'].get('insert')} "
          f"({(time.time()-t0):.0f}s)")
    results["align"] = align

    # ── Phase 4: master 报告 ──
    print("\n[Phase 4] 综合报告...")
    extra = f"· 模型 {model} · 并发 {concurrent}"
    report_path = phase4_report(
        out_dir, docname, tgscc, variants, structure, llm, align, extra)
    results["report_path"] = report_path
    print(f"  ✓ 报告: {report_path}")

    # ── Phase 5: DOCX 回写（02 引擎，直接 OOXML） ──
    if writeback and src_docx:
        print("\n[Phase 5] DOCX 回写（02 引擎，直接 OOXML）...")
        current_sha256 = sha256_file(src_docx)
        if current_sha256 != src_docx_sha256:
            raise RuntimeError("源 DOCX 在审校期间发生变化，拒绝写回")
        all_findings = llm + tgscc + variants + structure + names
        resolved = _resolve_findings_to_p(all_findings, text_map)
        issues = _findings_to_issues(resolved)
        n_must = sum(1 for i in issues if i["fix_class"] == "must_fix")
        n_polish = sum(1 for i in issues if i["fix_class"] == "polish")
        n_verify = sum(1 for i in issues if i["fix_class"] == "verify")
        print(f"  ✓ 定位 {len(resolved)} 条发现 → {len(issues)} 条 issues"
              f"（必改 {n_must} + 润色 {n_polish} + 待核 {n_verify}）")

        review_path = _run_02_writeback(
            src_docx, docname, issues, author,
            expected_source_sha256=src_docx_sha256)
        results["review_path"] = review_path
        print(f"  ✓ 审阅版: {review_path}")

    print("\n✅ 最大化检查完成")
    return results


def _run_02_writeback(src_docx: str, docname: str,
                      issues: List[Dict], author: str,
                      out_path: Optional[str] = None,
                      expected_source_sha256: Optional[str] = None) -> str:
    """调用 02 引擎把 issues[] 回写到 DOCX。

    建 output_<docname>/results/ 目录结构（02 引擎约定），subprocess 调用
    writeback_engine.py，输出 <docname>_审阅版.docx。
    """
    import shutil
    import subprocess
    import tempfile
    from .extract_source import sha256_file
    from .writeback_engine import __file__ as ENGINE_PATH, audit_docx

    actionable = [
        issue for issue in issues
        if (isinstance(issue, dict)
            and issue.get("fix_class") in ("must_fix", "polish", "verify")
            and str(issue.get("location", "")).strip()
            and str(issue.get("current", "")).strip())
    ]
    if not actionable:
        raise ValueError("没有可写回的 Word findings")

    # 输出路径（显式指定，避免 02 引擎默认路径歧义）
    parent = os.path.abspath(os.path.dirname(src_docx) or ".")
    delivery_dir = os.path.join(parent, f"output_{docname}")
    out_docx = os.path.abspath(
        out_path or os.path.join(delivery_dir, f"{docname}_审阅版.docx"))
    if Path(src_docx).resolve() == Path(out_docx).resolve():
        raise ValueError("输出路径不能覆盖源 DOCX")
    os.makedirs(os.path.dirname(out_docx), exist_ok=True)
    bound_source_sha256 = (
        expected_source_sha256.lower()
        if expected_source_sha256 else sha256_file(src_docx))

    # 02 引擎仍使用 output_<stem>/results 约定。为每次调用建立隔离目录，
    # 避免真实 output_<stem>/results 中的历史 JSON 被再次加载。
    with tempfile.TemporaryDirectory(
            prefix=".ai-proofread-writeback-", dir=parent) as stage_parent:
        staged_source = os.path.join(stage_parent, os.path.basename(src_docx))
        shutil.copy2(src_docx, staged_source)
        if sha256_file(staged_source) != bound_source_sha256:
            raise RuntimeError("源 DOCX 在校验与快照之间发生变化，拒绝写回")
        engine_stem = Path(src_docx).stem
        engine_out_dir = os.path.join(stage_parent, f"output_{engine_stem}")
        results_dir = os.path.join(engine_out_dir, "results")
        os.makedirs(results_dir, exist_ok=True)
        issues_path = os.path.join(results_dir, "max_findings.json")
        with open(issues_path, "w", encoding="utf-8") as f:
            json.dump({"issues": actionable}, f, ensure_ascii=False, indent=2)

        staged_output = os.path.join(stage_parent, "review-output.docx")
        cmd = [sys.executable, ENGINE_PATH, engine_out_dir,
               "--author", author, "--out", staged_output]
        print(f"  ⚙️  02 引擎: {' '.join(os.path.basename(c) if i in (0, 1) else c for i, c in enumerate(cmd))}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or '').strip()
            raise RuntimeError(
                f"02 引擎退出码 {proc.returncode}"
                + (f": {detail}" if detail else ""))
        if not os.path.isfile(staged_output):
            raise RuntimeError(f"02 引擎未生成输出: {staged_output}")
        try:
            audit_docx(staged_output, expected_author=author)
        except Exception as exc:
            raise RuntimeError(f"02 引擎输出未通过 OOXML 审计: {exc}") from exc
        if sha256_file(src_docx) != bound_source_sha256:
            raise RuntimeError("源 DOCX 在写回期间发生变化，拒绝交付")
        os.replace(staged_output, out_docx)
    return out_docx


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="最大化检查模式")
    ap.add_argument("file", help="要检查的文件 (.md / .docx)")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--concurrent", type=int, default=3)
    ap.add_argument("--rpm", type=int, default=15)
    ap.add_argument("--names", action="store_true", help="启用专名查词")
    ap.add_argument("--writeback", action="store_true", help="审校后回写 DOCX")
    ap.add_argument("--author", default="审校助手")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run_max(args.file, model=args.model, concurrent=args.concurrent,
            rpm=args.rpm, run_names=args.names, verbose=args.verbose,
            writeback=args.writeback, author=args.author)
