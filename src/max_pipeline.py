#!/usr/bin/env python3
"""
max_pipeline.py — ai-proofread 最大化检查模式

打通项目全部环节的完整审校管线。调用方式见 src/cli.py 的 `max` 子命令。

    Phase 0: 确定性检查（无 LLM，秒级）
      0a. TGSCC 汉字规范检查（通用规范汉字表，查繁体/异体/表外字）
      0b. 异形词/不规范词形检查（离线词典数据 xh7_compressed + yixingci）
      0c. 结构检查（标题层级 hierarchy_gap/level_mismatch + 编号连续性）

    Phase 1: LLM 审校 —— JSON 发现模式（核心新增）
      分块 → 异步并发 → 每块返回 {"findings": [{original_sentence, corrected_sentence}]}
      （对象包装，兼容多 provider 的 json_object 模式）
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
import hashlib
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
MAX_CHECKPOINT_SCHEMA = "ai-proofread.max-checkpoint.v1"

# Phase 1 自动续跑：失败块记 checkpoint 后自动重跑（只补失败块），最多轮数。
# 服务端劣化（DeepSeek 高峰空响应/慢响应）常是时点性抖动，轮间退避等负载
# 回落即自动收敛，避免一次命令中途退出需手动重跑。轮次耗尽仍 fast-fail。
MAX_PHASE1_ROUNDS = 3
PHASE1_RETRY_DELAY_BASE = 60.0  # 轮间基础退避秒数，第 n 轮失败后等 n * BASE

DICT_PATHS = {
    "xianhan": "/Users/kicker114/Downloads/辞典/常用词典/现代汉语词典第7版/现代汉语词典第7版.mdx",
    "cihai": "/Users/kicker114/Downloads/辞典/常用词典/汉语辞海.mdx",
}


# ── 通用工具 ──────────────────────────────────────────────────────────


def _json_extract(text: str) -> Optional[List[Dict]]:
    """从模型输出中稳健提取 JSON 发现数组。

    规范输出是对象 {"findings": [...]}（prompt 要求，见
    prompt-proofreader-system-outputJSON.xml；对象格式让各 provider 的
    json_object/json_schema 模式能 pin 结构）：
    - {"findings": [...]} → 提取数组；{"findings": []} 表示"审完无发现"（干净块）。
    - 兼容漂移：{"issues"/"changes"/"corrections": [...]} 非空 → 救回有效发现；
      但错误键名的空对象数组（如 {"issues": []}）不可信 → 返回 None（判 invalid →
      failed，避免模型用错键名时被当成"0 发现"静默漏审）。
    - 裸数组 [...]（旧格式）→ 仍兼容；空数组 [] 仍是干净块。
    - 无法解析 → None。
    """
    if not text:
        return None
    text = text.strip()
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    if not text:
        return None
    if text.startswith("{"):
        # 规范对象格式
        end = text.rfind("}")
        if end <= 0:
            return None
        try:
            obj = json.loads(text[:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        # findings 是 canonical 键：空=干净块（合法）
        if isinstance(obj.get("findings"), list):
            return obj["findings"]
        # 其余兼容键：非空救回，空不可信 → failed
        for key in ("issues", "changes", "corrections"):
            val = obj.get(key)
            if isinstance(val, list):
                return val if val else None
        return None
    # 数组格式（旧格式兼容）
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
    for key in ("source_path", "source_sha256", "stats"):
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


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _checkpoint_run_dir(
        checkpoint_root: str | Path,
        identity: Dict[str, Any]) -> tuple[Path, str]:
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    run_key = hashlib.sha256(encoded).hexdigest()[:20]
    return Path(checkpoint_root) / run_key, run_key


def _checkpoint_record_path(run_dir: Path, index: int) -> Path:
    return run_dir / f"chunk-{index:06d}.json"


def _load_chunk_checkpoint(
        path: Path, identity: Dict[str, Any], index: int,
        chunk_sha256: str) -> Optional[List[Dict]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != MAX_CHECKPOINT_SCHEMA:
        return None
    if payload.get("identity") != identity:
        return None
    if payload.get("index") != index:
        return None
    if payload.get("chunk_sha256") != chunk_sha256:
        return None
    if payload.get("status") != "complete":
        return None
    findings = payload.get("findings")
    return findings if isinstance(findings, list) else None


def _write_chunk_checkpoint(
        run_dir: Path, identity: Dict[str, Any], index: int,
        chunk_sha256: str, status: str,
        findings: Optional[List[Dict]] = None,
        error: str = "") -> None:
    payload: Dict[str, Any] = {
        "schema": MAX_CHECKPOINT_SCHEMA,
        "identity": identity,
        "index": index,
        "chunk_sha256": chunk_sha256,
        "status": status,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if status == "complete":
        payload["findings"] = findings if findings is not None else []
    elif error:
        payload["error"] = error
    _atomic_write_json(_checkpoint_record_path(run_dir, index), payload)


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

    altChunk 格式（PDF→Word 导出，正文嵌 MHT、无 w:p）：用共享解析器
    extract_altchunk_paragraphs 取段落，P 编号与引擎物化后的 build_para_map
    逐段一致（已用同一列表校验）。
    """
    import zipfile
    from lxml import etree

    with zipfile.ZipFile(docx_path, "r") as z:
        # 纯 altChunk 文档（正文完全由 MHT 承载）→ 共享解析器建 text_map，
        # P 编号与引擎物化后的 build_para_map 逐段一致。
        # 兼容两种运行方式：作为模块（相对导入）或作为脚本（顶层绝对导入）。
        try:
            from .extract_source import docx_uses_altchunk_body, extract_altchunk_paragraphs
        except ImportError:
            from extract_source import docx_uses_altchunk_body, extract_altchunk_paragraphs
        if docx_uses_altchunk_body(z):
            altchunk_paras = extract_altchunk_paragraphs(z)
            if altchunk_paras:
                return {pn: para["text"] for pn, para in enumerate(altchunk_paras)}

        doc_xml = etree.fromstring(z.read("word/document.xml"))

    text_map: Dict[int, str] = {}
    body = doc_xml.find(f"{{{_W_NS}}}body")
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


def _resolve_findings_to_p(
        findings: List[Dict], text_map: Dict[int, str],
        skip_log: Optional[List[Dict]] = None) -> List[Dict]:
    """把 finding 定位到具体 P 段落，补充 pn/current/location。

    策略：
      1. 有效显式 P 只在该段内解析，不跨段静默改址。
      2. 无显式 P 时，精确命中必须全书唯一。
      3. 模糊命中必须有唯一且明显领先的最高分。
      4. 重复、并列或未命中的 finding 跳过写回，仍保留在报告中。

    skip_log 非 None 时，被跳过的 finding 会带原因（duplicate_anchor /
    fuzzy_tie / p_out_of_range / explicit_p_not_found / not_found /
    empty_key_text）追加进去，供 *_skipped.json 落盘与统计。
    """
    from .special_checker.match_similar_text import find_best_match

    def log_skip(reason: str, detail: str, f: Dict) -> None:
        if skip_log is not None:
            skip_log.append({
                "reason": reason,
                "detail": detail,
                "finding": dict(f),
            })

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
            log_skip("empty_key_text", "finding 缺少可用于定位的文本字段", f)
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
                log_skip("explicit_p_not_found",
                         f"显式 P{requested_pn} 段内未找到锚点", f)
                continue
        elif requested_pn is not None:
            print(f"  WARN: P{requested_pn} 超出文档范围，跳过写回: {key_text[:30]}")
            log_skip("p_out_of_range", f"显式 P{requested_pn} 超出文档范围", f)
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
                log_skip("duplicate_anchor", f"锚点跨段重复（{pages}）", f)
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
                    log_skip("fuzzy_tie",
                             f"模糊锚点并列（{top['ratio']:.1f} vs "
                             f"{runner_up['ratio']:.1f}，分差<5）", f)
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
            log_skip("not_found", "精确/模糊均未能唯一定位", f)
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
        chunk: Dict, index: int, total: int, model: str, rate_limiter,
        system_prompt: str,
        api_stats: Dict[str, int | float],
        models: Optional[List[str]] = None) -> Optional[Dict]:
    """处理单个 chunk，返回 {index, findings, chunk_text}。

    models: 多 provider failover 顺序（默认 [model]，单腿）。单 provider 耗尽重试
    后由 deepseek_async 自动推进到下一个 model。
    """
    from .proofreader import deepseek_async

    target_text = chunk.get("target", "")
    context_text = chunk.get("context", "")
    reference_text = chunk.get("reference", "")

    pre_text = ""
    if reference_text:
        pre_text += f"<reference>\n{reference_text}\n</reference>"
    if context_text and context_text.strip() != target_text.strip():
        pre_text += f"\n<context>\n{context_text}\n</context>"
    post_text = f"<target>\n{target_text}\n</target>"

    # JSON 发现模式：限制输出上限，避免模型生成超长 JSON 拖慢审校（见 CLAUDE.md）
    result = await deepseek_async(
        post_text, pre_text, model, rate_limiter,
        system_prompt=system_prompt, max_tokens=4096,
        stats=api_stats, request_label=f"chunk {index}/{total}",
        models=models,
    )

    if not result:
        print(f"  ⚠️  chunk {index}/{total}: API 失败", file=sys.stderr)
        return None

    findings = _json_extract(result)
    if findings is None:
        api_stats["invalid_json"] += 1
        print(f"  ⚠️  chunk {index}/{total}: JSON 无效", file=sys.stderr)
        return None
    if findings:
        print(f"  ✓ chunk {index}/{total}: 发现 {len(findings)} 条")
    else:
        print(f"  ✓ chunk {index}/{total}: 审完 0 发现（干净块）")
    # index 用 0-based（供 refined_chunks 回写），显示时已加 1
    return {"index": index - 1, "findings": findings, "chunk_text": target_text}


async def phase1_json_proofread(
        chunks: List[Dict], model: str = "deepseek-v4-flash",
        concurrent: int = 3, rpm: int = 15,
        checkpoint_root: str | Path | None = None,
        checkpoint_identity: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        request_timeout: float = 180.0,
        models: Optional[List[str]] = None) -> Dict:
    """异步并发 JSON 发现模式审校。返回 {findings, refined_chunks}。

    request_timeout: 单块墙钟看门狗（秒）。DeepSeek 在高负载下可能「收下请求
    却不吐响应 / 极慢 trickle」，此时 SDK 的 read timeout（按距上次收字节计时）
    会被持续重置而不触发；这里改用基于事件循环时钟的 asyncio.wait_for，无论
    服务端行为如何都会在 request_timeout 后放弃该块并记为 failed，避免整段
    Phase 1 永久挂起（见 月球之书 试点：单请求 300s 超时未触发、进程挂死 44min）。
    """
    from .proofreader import RateLimiter, load_system_prompt

    started = time.perf_counter()
    system_prompt = system_prompt or load_system_prompt("json")
    rate_limiter = RateLimiter(rpm)
    semaphore = asyncio.Semaphore(concurrent)
    api_stats: Dict[str, int | float] = {
        "logical_calls": 0,
        "attempts": 0,
        "retries": 0,
        "failures": 0,
        "empty_responses": 0,
        "invalid_json": 0,
        "provider_failovers": 0,
        "rate_wait_seconds": 0.0,
        "request_seconds": 0.0,
    }
    chunk_hashes = [
        _sha256_text(json.dumps(
            chunk, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ))
        for chunk in chunks
    ]
    run_dir: Optional[Path] = None
    if checkpoint_root is not None:
        if not checkpoint_identity:
            raise ValueError("启用 max checkpoint 时必须提供 checkpoint_identity")
        run_dir, run_key = _checkpoint_run_dir(
            checkpoint_root, checkpoint_identity)
        _atomic_write_json(run_dir / "manifest.json", {
            "schema": MAX_CHECKPOINT_SCHEMA,
            "run_key": run_key,
            "identity": checkpoint_identity,
            "chunk_count": len(chunks),
            "chunk_sha256": chunk_hashes,
        })

    results: List[Optional[Dict]] = [None] * len(chunks)
    checkpoint_hits = 0
    pending: list[tuple[int, Dict]] = []
    for idx, chunk in enumerate(chunks):
        cached: Optional[List[Dict]] = None
        if run_dir is not None and checkpoint_identity is not None:
            cached = _load_chunk_checkpoint(
                _checkpoint_record_path(run_dir, idx),
                checkpoint_identity, idx, chunk_hashes[idx],
            )
        if cached is not None:
            checkpoint_hits += 1
            results[idx] = {
                "index": idx,
                "findings": cached,
                "chunk_text": chunk.get("target", ""),
            }
        else:
            pending.append((idx, chunk))

    async def worker(idx: int, chunk: Dict) -> Optional[Dict]:
        async with semaphore:
            timed_out = False
            try:
                result = await asyncio.wait_for(
                    _proofread_one_json(
                        chunk, idx + 1, len(chunks), model, rate_limiter,
                        system_prompt, api_stats, models=models,
                    ),
                    timeout=request_timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                api_stats["failures"] += 1
                result = None
                print(
                    f"  ⏱  chunk {idx + 1}/{len(chunks)}: 墙钟超时 "
                    f"{request_timeout:.0f}s，记为失败（可续跑）",
                    file=sys.stderr,
                )
            if run_dir is not None and checkpoint_identity is not None:
                if result is None:
                    _write_chunk_checkpoint(
                        run_dir, checkpoint_identity, idx,
                        chunk_hashes[idx], "failed",
                        error=(
                            f"墙钟超时 {request_timeout:.0f}s" if timed_out
                            else "API 返回空、异常耗尽或 JSON 无效"
                        ),
                    )
                else:
                    _write_chunk_checkpoint(
                        run_dir, checkpoint_identity, idx,
                        chunk_hashes[idx], "complete",
                        findings=result["findings"],
                    )
            return result

    if pending:
        completed = await asyncio.gather(
            *(worker(idx, chunk) for idx, chunk in pending)
        )
        for (idx, _), result in zip(pending, completed):
            results[idx] = result

    all_findings: List[Dict] = []
    refined_chunks = [c.get("target", "") for c in chunks]
    # Layer A 丢弃可见性：LLM 返回的原始发现里，有多少因「无实际修改 /
    # 匹配不到原文 / 匹配率过低」被静默丢弃 —— 全部带原因记入 skipped，
    # 由调用方落盘 *_skipped.json 供人工复核。
    skipped: List[Dict] = []
    findings_from_llm = 0
    for r in results:
        if r is None:
            continue
        # 回写：对每个 {original, corrected} 用模糊匹配定位
        chunk_text = r["chunk_text"]
        new_text = chunk_text
        for item in (r["findings"] or []):
            findings_from_llm += 1
            original = (item or {}).get("original_sentence") or ""
            corrected = (item or {}).get("corrected_sentence") or ""
            if not original or not corrected or original == corrected:
                skipped.append({
                    "reason": "no_op_change",
                    "detail": "原句为空、改句为空或原句等于改句",
                    "original": original,
                    "corrected": corrected,
                })
                continue
            from .special_checker.match_similar_text import find_best_match
            bm = find_best_match(chunk_text, original, modified=corrected)
            if not bm["location"]:
                skipped.append({
                    "reason": "match_no_location",
                    "detail": "模糊匹配未能定位到原文",
                    "original": original,
                    "corrected": corrected,
                })
                continue
            if bm["ratio"] < 60:
                skipped.append({
                    "reason": "match_below_threshold",
                    "detail": f"匹配率 {bm['ratio']:.1f} < 60，疑似 LLM 改写而非原文",
                    "original": original,
                    "corrected": corrected,
                    "ratio": round(bm["ratio"], 2),
                })
                continue
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

    failed_chunks = sum(1 for result in results if result is None)
    stats: Dict[str, Any] = {
        "total_chunks": len(chunks),
        "checkpoint_hits": checkpoint_hits,
        "attempted_chunks": len(pending),
        "completed_chunks": len(chunks) - failed_chunks,
        "failed_chunks": failed_chunks,
        "findings_from_llm": findings_from_llm,
        "dropped_match": len(skipped),
        **api_stats,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    if run_dir is not None:
        stats["checkpoint_dir"] = str(run_dir.resolve())
    print(
        "  阶段统计: "
        f"块={stats['total_chunks']} "
        f"续跑命中={stats['checkpoint_hits']} "
        f"本次调用={stats['attempted_chunks']} "
        f"失败={stats['failed_chunks']} | "
        f"API尝试={stats['attempts']} 重试={stats['retries']} "
        f"切换={stats['provider_failovers']} "
        f"空响应={stats['empty_responses']} JSON无效={stats['invalid_json']} | "
        f"限速等待={stats['rate_wait_seconds']:.2f}s "
        f"请求累计={stats['request_seconds']:.2f}s "
        f"墙钟={stats['elapsed_seconds']:.2f}s"
    )
    return {
        "findings": all_findings,
        "refined_chunks": refined_chunks,
        "stats": stats,
        "skipped": skipped,
    }


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
        llm: List[Dict], align: Dict,
        source_text: str = "", names: Optional[List[Dict]] = None,
        model: str = "", extra: Optional[str] = "",
        skipped: Optional[List[Dict]] = None) -> str:
    """生成自包含 master HTML 报告 — V3 深色主题 · 原文内嵌高亮。

    委托给 html_report_v3.phase4_report_v3()。
    """
    from .html_report_v3 import phase4_report_v3

    return phase4_report_v3(
        out_dir=out_dir, docname=docname, source_text=source_text,
        tgscc=tgscc, variants=variants, structure=structure,
        llm=llm, names=names, align=align, model=model,
        extra=extra or "", skipped=skipped)


# ── 主流程 ────────────────────────────────────────────────────────────


def run_max(
        file_path: str, model: str = "deepseek-v4-flash",
        concurrent: int = 3, rpm: int = 15,
        run_names: bool = False, verbose: bool = False,
        writeback: bool = False, author: str = "审校助手",
        chunk_size: int = 200,
        request_timeout: float = 180.0,
        failover_models: Optional[List[str]] = None) -> Dict:
    """执行完整 max 管线。返回各阶段结果与产物路径。

    writeback=True 时，审校完成后直接把发现回写到 DOCX（02 引擎，字符级修订+批注）。
    request_timeout: Phase 1 单块墙钟看门狗（秒），透传给 phase1_json_proofread。
    failover_models: 多 provider 备选模型列表（如 ["qwen3.8-max"]）。Phase 1 单块在
    model 重试耗尽后按此顺序自动推进；跨 provider 的失败块共享同一 checkpoint
    （identity 不含 model），续跑只补失败块。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须是正整数")
    from .cli import _resolve_input  # 复用 DOCX→MD 转换
    from .extract_source import sha256_file
    from .proofreader import load_system_prompt
    from .splitter import split_markdown_by_title_and_length_with_context

    run_started = time.perf_counter()
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
    stage_stats: Dict[str, Any] = {}
    if src_docx and src_docx_sha256:
        results["source_path"] = str(Path(src_docx).resolve())
        results["source_sha256"] = src_docx_sha256
    review_source_sha256 = src_docx_sha256 or sha256_file(md_path)
    system_prompt = load_system_prompt("json")
    prompt_sha256 = _sha256_text(system_prompt)
    # identity 不含 model：多 provider failover 时失败块可在不同模型/provider 间
    # 续跑共享 checkpoint（DeepSeek 跑的块不被 qwen 重审）。副作用：旧 checkpoint
    # 含 model 键、与当前 identity 不等 → 自动 cache miss（无害，run 重来）。
    checkpoint_identity = {
        "source_sha256": review_source_sha256,
        "prompt_sha256": prompt_sha256,
        "chunk_size": chunk_size,
    }
    checkpoint_root = Path(out_dir) / f".{docname}_max_checkpoint"

    print("╔════════════════════════════════════════════╗")
    print("║  ai-proofread · 最大化检查模式             ║")
    print("╚════════════════════════════════════════════╝")
    print(f"输入: {md_path.name} ({len(text)} 字)")

    # ── Phase 0: 确定性检查 ──
    print("\n[Phase 0] 确定性检查...")
    phase0_started = time.perf_counter()
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
    stage_stats["phase0"] = {
        "seconds": round(time.perf_counter() - phase0_started, 3),
        "findings": len(tgscc) + len(variants) + len(structure),
    }

    # ── Phase 1: LLM JSON 发现模式 ──
    print(f"\n[Phase 1] LLM 审校（JSON 发现模式，模型={model}）...")
    print("  分块...")
    chunks = split_markdown_by_title_and_length_with_context(
        text, levels=[1, 2], cut_by=chunk_size)
    print(f"      ✓ {len(chunks)} 块")

    print("  异步并发审校...")
    t0 = time.time()
    # 自动续跑：失败块已记 checkpoint，重跑同一调用只补失败块（identity 相同）。
    # 服务端劣化多为时点性抖动，轮间退避等负载回落即收敛；轮次耗尽仍 fast-fail。
    phase1_round = 0
    llm_result = None
    while True:
        phase1_round += 1
        if phase1_round > 1:
            delay = PHASE1_RETRY_DELAY_BASE * (phase1_round - 1)
            print(
                f"  ⏳ 第 {phase1_round - 1} 轮有失败块，{delay:.0f}s 后自动续跑"
                "（checkpoint 只补失败块）..."
            )
            time.sleep(delay)
        llm_result = asyncio.run(phase1_json_proofread(
            chunks, model=model, concurrent=concurrent, rpm=rpm,
            checkpoint_root=checkpoint_root,
            checkpoint_identity=checkpoint_identity,
            system_prompt=system_prompt,
            request_timeout=request_timeout,
            models=([model] + list(failover_models)
                    if failover_models else None)))
        failed_now = llm_result.get("stats", {}).get("failed_chunks", 0)
        if failed_now == 0:
            break
        if phase1_round >= MAX_PHASE1_ROUNDS:
            print(
                f"  ❌ 第 {phase1_round} 轮后仍有 {failed_now} 块失败"
                f"（达自动续跑上限 {MAX_PHASE1_ROUNDS} 轮），已保留 checkpoint"
            )
            break
    llm = llm_result["findings"]
    refined_text = "\n".join(llm_result["refined_chunks"])
    # Layer A 丢弃（chunk 内定位失败）在 phase1 内收集；Layer B 丢弃（P 段
    # 解析失败）在 Phase 5 回写前由 _resolve_findings_to_p 追加，统一落盘。
    llm_skipped = llm_result.get("skipped", []) or []
    skipped_all: List[Dict] = list(llm_skipped)
    print(f"      ✓ {len(llm)} 条修正 ({(time.time()-t0):.0f}s)"
          f"{f'；{len(llm_skipped)} 条被跳过/未定位' if llm_skipped else ''}")
    results["llm"] = llm
    phase1_stats = llm_result.get("stats", {
        "total_chunks": len(chunks),
        "checkpoint_hits": 0,
        "attempted_chunks": len(chunks),
        "completed_chunks": len(chunks),
        "failed_chunks": 0,
        "elapsed_seconds": round(time.time() - t0, 3),
    })
    stage_stats["phase1"] = phase1_stats
    results["stats"] = stage_stats
    if phase1_stats.get("failed_chunks", 0):
        # 释放 API 线程池：不再接受新任务。超时放弃的请求线程无法被取消，
        # 会继续阻塞到 SDK 超时或更久；若在此处正常退出，concurrent.futures
        # 的 _python_exit 会在进程退出时无条件 join 这些线程导致退出挂起，
        # 由 CLI 层 flush 后 os._exit 兜底。
        try:
            from .proofreader import _API_EXECUTOR
            _API_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        raise RuntimeError(
            f"Phase 1 存在失败分块（{phase1_stats.get('failed_chunks')} 块），"
            "已保留 checkpoint；重新运行同一命令将只补失败块"
        )

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
    phase2_started = time.perf_counter()
    if run_names:
        print("\n[Phase 2] 专名查词...")
        names = phase2_names(refined_text, model=model)
    results["names"] = names
    stage_stats["phase2"] = {
        "seconds": round(time.perf_counter() - phase2_started, 3),
        "enabled": run_names,
        "findings": len(names),
    }

    # ── Phase 3: 句子对齐 ──
    print("\n[Phase 3] 句子对齐...")
    t0 = time.time()
    align_base = os.path.join(out_dir, docname)
    align = phase3_align(text, refined_text, align_base)
    phase3_seconds = time.time() - t0
    print(f"      ✓ 匹配={align['stats'].get('match')} "
          f"删除={align['stats'].get('delete')} 新增={align['stats'].get('insert')} "
          f"({phase3_seconds:.0f}s)")
    results["align"] = align
    stage_stats["phase3"] = {"seconds": round(phase3_seconds, 3)}

    # ── Phase 4: master 报告 ──
    print("\n[Phase 4] 综合报告...")
    phase4_started = time.perf_counter()
    extra = f"· 模型 {model} · 并发 {concurrent}"
    report_path = phase4_report(
        out_dir, docname, tgscc, variants, structure,
        llm, align, source_text=text, names=names,
        model=model, extra=extra, skipped=skipped_all)
    results["report_path"] = report_path
    print(f"  ✓ 报告: {report_path}")
    stage_stats["phase4"] = {
        "seconds": round(time.perf_counter() - phase4_started, 3),
    }
    results["stats"] = stage_stats
    if writeback and src_docx:
        # Preserve findings even when the source changes before Word writeback.
        _save_findings(findings_path, results)

    # ── Phase 5: DOCX 回写（02 引擎，直接 OOXML） ──
    if writeback and src_docx:
        print("\n[Phase 5] DOCX 回写（02 引擎，直接 OOXML）...")
        phase5_started = time.perf_counter()
        current_sha256 = sha256_file(src_docx)
        if current_sha256 != src_docx_sha256:
            raise RuntimeError("源 DOCX 在审校期间发生变化，拒绝写回")
        all_findings = llm + tgscc + variants + structure + names
        resolved = _resolve_findings_to_p(all_findings, text_map,
                                          skip_log=skipped_all)
        issues = _findings_to_issues(resolved)
        n_must = sum(1 for i in issues if i["fix_class"] == "must_fix")
        n_polish = sum(1 for i in issues if i["fix_class"] == "polish")
        n_verify = sum(1 for i in issues if i["fix_class"] == "verify")
        layer_b_drops = len(skipped_all) - len(llm_skipped)
        print(f"  ✓ 定位 {len(resolved)} 条发现 → {len(issues)} 条 issues"
              f"（必改 {n_must} + 润色 {n_polish} + 待核 {n_verify}"
              f"{f'；{layer_b_drops} 条未定位跳过' if layer_b_drops else ''}）")

        review_path = _run_02_writeback(
            src_docx, docname, issues, author,
            expected_source_sha256=src_docx_sha256)
        results["review_path"] = review_path
        print(f"  ✓ 审阅版: {review_path}")
        stage_stats["phase5"] = {
            "seconds": round(time.perf_counter() - phase5_started, 3),
            "issues": len(issues),
            "dropped_resolution": len(skipped_all) - len(llm_skipped),
        }

    # Layer A + B 丢弃可见性：带原因原子落盘 *_skipped.json，供人工复核
    # 覆盖率损失（LLM 改写太离谱 / 锚点跨段重复 / 模糊并列 / 完全未定位等）。
    skipped_path = ""
    if skipped_all:
        skipped_path = os.path.join(out_dir, f"{docname}_skipped.json")
        _atomic_write_json(Path(skipped_path), {
            "schema": "ai-proofread.skipped.v1",
            "source_sha256": review_source_sha256,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(skipped_all),
            "dropped": skipped_all,
        })
        results["skipped_path"] = skipped_path
        print(f"  ⚠️  {len(skipped_all)} 条发现被跳过/未定位，"
              f"未进入修订与批注（详见 {skipped_path}）")

    stage_stats["total_seconds"] = round(
        time.perf_counter() - run_started, 3)
    results["stats"] = stage_stats
    _save_findings(findings_path, results)
    print("\n阶段耗时:")
    for phase in ("phase0", "phase1", "phase2", "phase3", "phase4", "phase5"):
        data = stage_stats.get(phase)
        if isinstance(data, dict):
            print(f"  {phase}: {data.get('seconds', data.get('elapsed_seconds', 0)):.2f}s")
    print(f"  total: {stage_stats['total_seconds']:.2f}s")
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
    ap.add_argument("--chunk-size", type=int, default=200)
    ap.add_argument("--names", action="store_true", help="启用专名查词")
    ap.add_argument("--writeback", action="store_true", help="审校后回写 DOCX")
    ap.add_argument("--author", default="审校助手")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run_max(args.file, model=args.model, concurrent=args.concurrent,
            rpm=args.rpm, run_names=args.names, verbose=args.verbose,
            writeback=args.writeback, author=args.author,
            chunk_size=args.chunk_size)
