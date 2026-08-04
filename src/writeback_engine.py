#!/usr/bin/env python3
"""Apply proofreading findings back to the original .docx -- must_fix → track changes,
polish/verify → annotations (highlight + comment). 02-side version: pure-Chinese,
P-number located, no source-text evidence.

Modes per fix_class
-------------------
must_fix  → w:del/w:ins track changes (auto-edit) + yellow highlight + styled comment (reason/evidence chain)
polish    → yellow highlight + styled comment (suggestion only, never auto-edit)
verify    → yellow highlight + styled comment (pending verification, never auto-edit)

All comments use 01-aligned multi-run format:
  • Bold title: 【{prefix}】{category}
  • Blue (#1155CC) suggestion: ▶ 建议：{suggested}
  • Gray (#595959) evidence:   ◎ 依据：{reason}

Usage:
    python3 apply_corrections_02.py <output_dir> [--author 审阅助手] [--out <path>]

Reads:   <output_dir>/results/*.json  (issue list with fix_class)
         <output_dir> 所在目录的原始 docx
Writes:  <output_dir>/xxxx_审阅版.docx

The original docx is found by scanning the parent of output_dir for a matching
.docx file (same stem as output_dir name after "output_" prefix).
"""

import argparse
import glob
import json
import os
import re
import sys
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from lxml import etree

# ── OOXML namespaces ──────────────────────────────────────────────────────
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
REL  = 'http://schemas.openxmlformats.org/package/2006/relationships'
W14 = 'http://schemas.microsoft.com/office/word/2010/wordml'
W15 = 'http://schemas.microsoft.com/office/word/2012/wordml'  # WPS required for commentsExtended
NS = {'w': W, 'r': R, 'mc': MC}

# Register namespace prefixes for lxml serialization — ensures comments.xml
# uses standard w: prefix instead of auto-assigned ns0:
etree.register_namespace('w', W)
etree.register_namespace('w14', W14)
etree.register_namespace('w15', W15)
etree.register_namespace('mc', MC)
etree.register_namespace('r', R)


def qn(tag):
    return f'{{{W}}}{tag}'


def rn(tag):
    return f'{{{R}}}{tag}'


def mcn(tag):
    return f'{{{MC}}}{tag}'


# ── 振幅预算：02 无源文证据，保守设闸防 LLM 过度改写 ───────────────
MTF_CHANGED_SPAN_CAP = 30   # 原文被改最大字数
MTF_RETENTION_FLOOR = 0.35   # 字 bigram 保留下限
MTF_SHORT_RETENTION = 0.10   # 短 replacement（≤4字符）的保留下限，防过度阻挡小幅修正
MTF_SHORT_SKIP_GATE = 2      # 单字修改（changed_span ≤2）跳过振幅门禁——1字修正（如「查→茬」）bigram 保留恒为0
MTF_BOUNDARY_CAP = 1         # 最大允许句界变化数
# 以上为本项目标定占位，勿跨语料照搬

# ── 锚点窗口 ──────────────────────────────────────────────────────────────
MIN_ANCHOR = 6
MAX_WINDOW = 30


def anchor_window(text, vpos, vlen):
    """Wrap seg[vpos:vpos+vlen] with ≥MIN_ANCHOR chars, unique in text."""
    s, e = vpos, vpos + vlen
    L = len(text)
    while e - s <= MAX_WINDOW:
        if e - s >= MIN_ANCHOR and text.count(text[s:e]) == 1:
            return s, e
        grew = False
        if e < L:
            e += 1
            grew = True
        if s > 0:
            s -= 1
            grew = True
        if not grew:
            break
    return None


# ── load findings ─────────────────────────────────────────────────────────
def load_findings(results_dir):
    """Load all results/*.json, keep only issues with fix_class + location + current."""
    findings = []
    for path in sorted(glob.glob(os.path.join(results_dir, '*.json'))):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        issues = data.get('issues', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for issue in issues:
            fc = str(issue.get('fix_class', '')).strip()
            loc = str(issue.get('location', '')).strip()
            cur = str(issue.get('current', '')).strip()
            sug = str(issue.get('suggested', '')).strip()
            if not fc or not loc or not cur:
                continue
            pn = _extract_pn(loc)
            if pn is None:
                continue
            findings.append({
                'fix_class': fc,
                'location': loc,
                'pn': pn,
                'current': cur,
                'suggested': sug,
                'reason': str(issue.get('reason', '')).strip(),
                'description': str(issue.get('description', '')).strip(),
                'category': str(issue.get('category', '')).strip(),
            })
    return findings


def _extract_pn(loc):
    m = re.search(r'P(\d+)', loc)
    return int(m.group(1)) if m else None


# ── docx helpers (same P-numbering as extract_text.py) ────────────────────
def build_para_map(body):
    """Walk body children in order, assign P-numbers (same as extract_text.py).

    Returns {pn: w:p_element} -- only paragraphs that contain text.
    Also returns {pn: str} for text lookup.
    """
    para_map = {}
    text_map = {}
    pn = 0
    for child in body.iterchildren():
        tag = child.tag.rsplit('}', 1)[-1]
        if tag == 'p':
            t = _para_raw_text(child)
            if t.strip():
                para_map[pn] = child
                text_map[pn] = t
                pn += 1
        elif tag == 'tbl':
            for row_p in child.findall('.//' + qn('p')):
                t = _para_raw_text(row_p)
                if t.strip():
                    para_map[pn] = row_p
                    text_map[pn] = t
                    pn += 1
    return para_map, text_map


def _para_raw_text(p_elem):
    """Accept-view text of a w:p element — matches extract_text.py accepted_para_text().

    含 w:ins 内文本，排除 w:del 内文本。与 extract_text.py 的 P-number 编号保持一致，
    否则第二轮 findings 的 location:P{n} 会定位错位。
    """
    parts = []
    for t in p_elem.iter(qn('t')):
        # 排除 w:del 祖先下的被删文本
        if any(a.tag == qn('del') for a in t.iterancestors()):
            continue
        parts.append(t.text or '')
    return ''.join(parts)


def normalize_text(s):
    """标记/空白归一化：折叠连续空格，全角字母数字→半角，智能引号→直引号。

    不消除所有空格（原版消除空格后归一化位置映射回原文时偏移错误）。
    """
    # 统一非标准空格（分别替换，不拼字面序列）
    for c in (chr(0xa0), chr(0x200b), chr(0x2009), chr(0x2002), chr(0x2003)):
        s = s.replace(c, ' ')
    s = s.replace(chr(0x2013), '-').replace(chr(0x2014), '--')
    s = s.replace(chr(0x2018), "'").replace(chr(0x2019), "'")
    s = s.replace(chr(0x201c), '"').replace(chr(0x201d), '"')
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def _find_para(para_map, text_map, pn, anchor):
    """Find the paragraph containing `anchor`. Try exact P-number first, then full search."""
    # 1: exact P-number
    p = para_map.get(pn)
    if p is not None:
        txt = text_map.get(pn, '')
        if txt and anchor in txt:
            return p, txt
    # 2: fallback -- search all paragraphs
    for opn, txt in text_map.items():
        if anchor in txt or (normalize_text(anchor) in normalize_text(txt)):
            return para_map[opn], txt
    return None, None


def _locate_paragraph(docx_path, p_num, body_para_map, body_text_map):
    """Locate a paragraph by P-number, checking headers/footers/textboxes if beyond body.

    Returns (lxml.Element, str) or (None, None).
    """
    # 1. Try body paragraphs first
    if p_num in body_para_map:
        return body_para_map[p_num], body_text_map.get(p_num, '')

    # 2. If beyond body, iterate headers/footers/textboxes
    body_count = len(body_para_map)
    offset = p_num - body_count
    if offset < 0:
        return None, None

    try:
        from docx import Document
        doc = Document(docx_path)
    except Exception:
        return None, None

    # Headers then footers
    for i, section in enumerate(doc.sections):
        for header in (section.header, section.first_page_header):
            if header and header.paragraphs:
                for p in header.paragraphs:
                    ht = _para_raw_text(p._element).strip()
                    if ht:
                        if offset == 0:
                            return p._element, ht
                        offset -= 1
        for footer in (section.footer, section.first_page_footer):
            if footer and footer.paragraphs:
                for p in footer.paragraphs:
                    ft = _para_raw_text(p._element).strip()
                    if ft:
                        if offset == 0:
                            return p._element, ft
                        offset -= 1

    # Textboxes
    ns_w = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    for child in doc.element.body.iterchildren():
        for txbx in child.iter(f'{{{ns_w}}}txbxContent'):
            for p in txbx.iter(f'{{{ns_w}}}p'):
                text = _para_raw_text(p).strip()
                if text:
                    if offset == 0:
                        return p, text
                    offset -= 1

    return None, None


def bigrams(s):
    """字 bigram 计数，用于 retention 度量。"""
    s = re.sub(r'\s+', '', s or '')
    from collections import Counter
    return Counter(s[i:i + 2] for i in range(len(s) - 1)) if len(s) >= 2 else Counter()


def measure_mtf(cur, sug):
    """量化 must_fix 改动幅度：原文被改字数、保留率、句界变化。

    简化版 verify_revisions measure（不判 edit_kind，只做三指标）。
    返回值全为零 → 零动作放行。
    """
    cur = cur or ''
    sug = sug or ''
    if cur == sug:
        return {'changed_span': 0, 'added': 0, 'retention': 1.0, 'boundary_delta': 0,
                'len_cur': len(cur), 'len_sug': len(sug)}
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, cur, sug, autojunk=False)
    changed = added = 0
    retained = sum(i2 - i1 for _, i1, i2, _, _ in sm.get_opcodes() if _ == 'equal')
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('delete', 'replace'):
            changed += i2 - i1
        if tag in ('insert', 'replace'):
            added += j2 - j1
    bc = bigrams(cur)
    bs = bigrams(sug)
    inter = sum((bc & bs).values())
    retention = round(inter / sum(bc.values()), 3) if bc else 1.0
    _sent = re.compile(r'[。！？!?]+')
    bd = abs(len(_sent.findall(sug or '')) - len(_sent.findall(cur or '')))
    return {'changed_span': changed, 'added': added, 'retention': retention,
            'boundary_delta': bd, 'len_cur': len(cur), 'len_sug': len(sug)}


def _copy_rpr(src_run, dst_run):
    """Copy <w:rPr> from src_run to dst_run (both <w:r> elements), stripping annotation-only elements."""
    src_rpr = src_run.find(qn('rPr'))
    if src_rpr is None:
        return
    existing = dst_run.find(qn('rPr'))
    if existing is not None:
        dst_run.remove(existing)
    dst_rpr = deepcopy(src_rpr)
    # Remove annotation-only + revision-metadata children (copied into del/ins
    # would create nested OOXML that Word renders incorrectly or silently drops)
    strips = {'highlight', 'commentReference', 'commentRangeStart', 'commentRangeEnd',
              'moveFrom', 'moveTo', 'moveFromRangeStart', 'moveFromRangeEnd',
              'moveToRangeStart', 'moveToRangeEnd', 'rPrChange', 'bookmarkStart',
              'bookmarkEnd'}
    for child in list(dst_rpr):
        tag = child.tag.rsplit('}', 1)[-1]
        if tag in strips:
            dst_rpr.remove(child)
    dst_run.insert(0, dst_rpr)


def _strip_highlight(run_or_elem):
    """Remove <w:highlight> from a run element's rPr (or the first run inside an element)."""
    if run_or_elem is None:
        return
    r = run_or_elem if run_or_elem.find(qn('r')) is None else run_or_elem.find(qn('r'))
    rpr = r.find(qn('rPr')) if r is not None else None
    if rpr is None:
        return
    hl = rpr.find(qn('highlight'))
    if hl is not None:
        rpr.remove(hl)


# ── Track changes helpers ─────────────────────────────────────────────────
def _next_rev_id(root):
    """Maximum existing w:revId + 1 across document.xml."""
    ids = []
    for el in root.iter(qn('ins')):
        ids.append(int(el.get(qn('id'), 0)))
    for el in root.iter(qn('del')):
        ids.append(int(el.get(qn('id'), 0)))
    return max(ids) + 1 if ids else 1


def _next_cmt_id(existing_comments):
    """Maximum comment ID + 1 from existing comments.xml."""
    ids = [0]
    if existing_comments is not None:
        for cmt in existing_comments.iter(qn('comment')):
            try:
                ids.append(int(cmt.get(qn('id'), 0)))
            except (ValueError, TypeError):
                pass
    return max(ids) + 1


def _author_iso(author):
    now = datetime.now(timezone.utc)
    return author, now.strftime('%Y-%m-%dT%H:%M:%SZ')


def _ensure_run_rpr(para):
    """Return a default rPr element (<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr>)"""
    rpr = etree.SubElement(para, qn('rPr')) if para.find(qn('rPr')) is None else para.find(qn('rPr'))
    return rpr


def _make_run(para, text, rpr_attrs=None):
    """Create <w:r><w:rPr>…</w:rPr><w:t>text</w:t></w:r> with optional rPr.

    rpr_attrs: dict with keys 'sz', 'color', 'b', 'i', etc. for w:rPr children.
    """
    r = etree.SubElement(para, qn('r'))
    if rpr_attrs:
        rpr = etree.SubElement(r, qn('rPr'))
        for k, v in rpr_attrs.items():
            etree.SubElement(rpr, qn(k), attrib=v if isinstance(v, dict) else {qn(k): v})
    t = etree.SubElement(r, qn('t'))
    t.text = text
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return r


def apply_track_change(para, current, suggested, rid, author, date_iso):
    """Apply character-precise tracked change using difflib minimal diff.

    v3: preserves text outside the error span by truncating boundary runs
    instead of removing them. Never creates after_run clones (diff output
    covers the entire current span).
    """
    runs = para.findall(qn('r'))
    if not runs:
        return False
    parent = para
    joined = ''
    intervals = []
    for r in runs:
        t_el = r.find(qn('t'))
        txt = t_el.text or '' if t_el is not None else ''
        intervals.append((len(joined), len(joined) + len(txt), r, t_el))
        joined += txt
    cpos = joined.find(current)
    if cpos < 0:
        jn = normalize_text(joined)
        cn = normalize_text(current)
        cpos = jn.find(cn)
        if cpos >= 0:
            ri = 0; ni = 0
            while ni < cpos and ri < len(joined):
                if not joined[ri].isspace():
                    ni += 1
                ri += 1
            cpos = ri
        else:
            print('  WARN: anchor not found: "' + current[:40] + '"')
            return False
    c_end = cpos + len(current)

    # Find overlapping runs
    ai = [i for i, (s, e, r, t) in enumerate(intervals) if s < c_end and e > cpos]
    if not ai:
        print('  WARN: anchor span empty: "' + current[:40] + '"')
        return False

    first_idx, last_idx = ai[0], ai[-1]

    # ── Split first run: truncate text before cpos, keep it ──
    first_s, first_e, first_run, first_t = intervals[first_idx]
    if first_s < cpos and first_t is not None and first_t.text:
        before_len = cpos - first_s
        first_t.text = first_t.text[:before_len]

    # ── Split last run: keep text after c_end, truncate original ──
    last_s, last_e, last_run, last_t = intervals[last_idx]
    needs_after_tail = False
    after_tail_text = ''
    if last_e > c_end and last_t is not None and last_t.text:
        after_start = c_end - last_s
        if 0 < after_start < len(last_t.text):
            after_tail_text = last_t.text[after_start:]
            last_t.text = last_t.text[:after_start]
            needs_after_tail = True

    # ── Build diff elements ──
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, current, suggested, autojunk=False)
    new_elements = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        cp = current[i1:i2]
        sp = suggested[j1:j2]

        if tag == 'equal' and cp:
            clone = deepcopy(first_run)
            ct = clone.find(qn('t'))
            if ct is not None:
                ct.text = cp
            _strip_highlight(clone)
            new_elements.append(clone)

        elif tag == 'delete' and cp:
            del_el = etree.Element(qn('del'))
            del_el.set(qn('id'), str(rid))
            del_el.set(qn('author'), author)
            del_el.set(qn('date'), date_iso)
            dr = etree.SubElement(del_el, qn('r'))
            _copy_rpr(first_run, dr)
            dt = etree.SubElement(dr, qn('delText'))
            dt.text = cp
            dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            new_elements.append(del_el)

        elif tag == 'insert' and sp:
            ins_el = etree.Element(qn('ins'))
            ins_el.set(qn('id'), str(rid))
            ins_el.set(qn('author'), author)
            ins_el.set(qn('date'), date_iso)
            ir = etree.SubElement(ins_el, qn('r'))
            _copy_rpr(first_run, ir)
            irp = ir.find(qn('rPr'))
            if irp is None:
                irp = etree.SubElement(ir, qn('rPr'))
            if irp.find(qn('highlight')) is None:
                etree.SubElement(irp, qn('highlight')).set(qn('val'), 'yellow')
            it = etree.SubElement(ir, qn('t'))
            it.text = sp
            it.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            new_elements.append(ins_el)

        elif tag == 'replace':
            if cp:
                del_el = etree.Element(qn('del'))
                del_el.set(qn('id'), str(rid))
                del_el.set(qn('author'), author)
                del_el.set(qn('date'), date_iso)
                dr = etree.SubElement(del_el, qn('r'))
                _copy_rpr(first_run, dr)
                dt = etree.SubElement(dr, qn('delText'))
                dt.text = cp
                dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_elements.append(del_el)
            if sp:
                ins_el = etree.Element(qn('ins'))
                ins_el.set(qn('id'), str(rid))
                ins_el.set(qn('author'), author)
                ins_el.set(qn('date'), date_iso)
                ir = etree.SubElement(ins_el, qn('r'))
                _copy_rpr(first_run, ir)
                irp = ir.find(qn('rPr'))
                if irp is None:
                    irp = etree.SubElement(ir, qn('rPr'))
                if irp.find(qn('highlight')) is None:
                    etree.SubElement(irp, qn('highlight')).set(qn('val'), 'yellow')
                it = etree.SubElement(ir, qn('t'))
                it.text = sp
                it.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_elements.append(ins_el)

    # Remove runs fully inside the error span
    runs_to_remove = [i for i in ai
                      if intervals[i][0] >= cpos and intervals[i][1] <= c_end]
    for i in reversed(sorted(set(runs_to_remove))):
        try:
            parent.remove(runs[i])
        except (ValueError, IndexError):
            pass

    # Find a valid insertion point: use the first non-removed overlapping run,
    # or fall back to runs outside the span, or append to parent end.
    insert_ref = None
    # Try remaining runs in order: first non-removed from ai, then first run before ai, then append
    for candidate_idx in (first_idx, 0):
        if candidate_idx < len(runs) and runs[candidate_idx].getparent() is parent:
            insert_ref = runs[candidate_idx]
            break
    if insert_ref is not None:
        insert_idx = list(parent).index(insert_ref)
        for el in new_elements:
            parent.insert(insert_idx, el)
            insert_idx += 1
    else:
        for el in new_elements:
            parent.append(el)

    # Append the after-tail text as a plain run (if any)
    if needs_after_tail and after_tail_text:
        tail_run = deepcopy(last_run)
        tail_t = tail_run.find(qn('t'))
        if tail_t is not None:
            tail_t.text = after_tail_text
        parent.append(tail_run)

    return True

def apply_annotation(para, current, fix_class, rid, author, date_iso, suggested='', reason='', description='', category=''):
    """Apply yellow highlight + comment to `current` in the paragraph.

    Adds highlight to runs that contain the error text, then places
    commentRangeStart before the first affected run and
    commentRangeEnd + commentReference after the last affected run.
    """
    runs = para.findall(qn('r'))
    if not runs:
        return False

    joined = ''
    run_info = []  # (start, end, run, t_element)
    for i, r in enumerate(runs):
        t_el = r.find(qn('t'))
        txt = t_el.text or '' if t_el is not None else ''
        run_info.append((len(joined), len(joined) + len(txt), r, t_el))
        joined += txt

    cpos = joined.find(current)
    if cpos < 0:
        joined_norm = normalize_text(joined)
        cur_norm = normalize_text(current)
        cpos = joined_norm.find(cur_norm)
        if cpos >= 0:
            raw_idx = 0
            norm_idx = 0
            while norm_idx < cpos and raw_idx < len(joined):
                if not joined[raw_idx].isspace():
                    norm_idx += 1
                raw_idx += 1
            cpos = raw_idx
        else:
            print(f'  WARN (annotate): "{current[:40]}" not found')
            return False

    c_end = cpos + len(current)
    affected = [i for i, (s, e, r, t) in enumerate(run_info) if s < c_end and e > cpos]
    if not affected:
        return False

    first_idx, last_idx = affected[0], affected[-1]
    first_run = run_info[first_idx][2]
    last_run = run_info[last_idx][2]
    parent = first_run.getparent()
    cmt_id = rid

    # Add highlight to affected runs
    for idx in affected:
        s, e, r, t_el = run_info[idx]
        rpr = r.find(qn('rPr'))
        if rpr is None:
            rpr = etree.SubElement(r, qn('rPr'))
        # Only add highlight if not already there
        if rpr.find(qn('highlight')) is None:
            etree.SubElement(rpr, qn('highlight')).set(qn('val'), 'yellow')

    # commentRangeStart — before first affected run
    crs = etree.Element(qn('commentRangeStart'))
    crs.set(qn('id'), str(cmt_id))
    parent.insert(list(parent).index(first_run), crs)

    # commentRangeEnd — after last affected run
    cre = etree.Element(qn('commentRangeEnd'))
    cre.set(qn('id'), str(cmt_id))
    last_idx_in_parent = list(parent).index(last_run)
    parent.insert(last_idx_in_parent + 1, cre)

    # commentReference — after commentRangeEnd
    cref = etree.Element(qn('commentReference'))
    cref.set(qn('id'), str(cmt_id))
    parent.insert(last_idx_in_parent + 2, cref)

    return cmt_id, category, fix_class, current, suggested, reason, description


# ── comment.xml ───────────────────────────────────────────────────────────
def build_comment_xml(comments_list):
    """Build <w:comments> from structured entries.

    Each entry: (cmt_id, author, date_iso, title, suggestion, evidence)
    Creates styled multi-run comment matching 01 pipeline format:
      - Bold title: 【prefix】category
      - Blue (#1155CC) suggestion: ▶ 建议：...
      - Gray (#595959) evidence:   ◎ 依据：...

    The root element includes mc:Ignorable="w14" and full namespace
    declarations so Word correctly renders the comments pane.
    """
    comments = etree.fromstring(f'<w:comments xmlns:w="{W}"/>')

    for entry in comments_list:
        cmt_id, author, date_iso, title, suggestion, evidence = entry
        c = etree.SubElement(comments, qn('comment'))
        c.set(qn('id'), str(cmt_id))
        c.set(qn('author'), author)
        c.set(qn('date'), date_iso)
        c.set(qn('initials'), 'AI')
        p = etree.SubElement(c, qn('p'))

        # Paragraph style required for Word comment rendering
        ppr = etree.SubElement(p, qn('pPr'))
        pstyle = etree.SubElement(ppr, qn('pStyle'))
        pstyle.set(qn('val'), 'CommentText')

        # Run 0: annotationRef (standard Word comment appearance)
        r0 = etree.SubElement(p, qn('r'))
        rpr0 = etree.SubElement(r0, qn('rPr'))
        rs0 = etree.SubElement(rpr0, qn('rStyle'))
        rs0.set(qn('val'), 'CommentReference')
        etree.SubElement(r0, qn('annotationRef'))

        # Run 1: title in bold
        r1 = etree.SubElement(p, qn('r'))
        rpr1 = etree.SubElement(r1, qn('rPr'))
        etree.SubElement(rpr1, qn('b'))
        t1 = etree.SubElement(r1, qn('t'))
        t1.text = title
        t1.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')

        # Line break before suggestion/evidence section
        if suggestion or evidence:
            br1 = etree.SubElement(p, qn('r'))
            etree.SubElement(br1, qn('br'))

        if suggestion:
            r2 = etree.SubElement(p, qn('r'))
            rpr2 = etree.SubElement(r2, qn('rPr'))
            c2 = etree.SubElement(rpr2, qn('color'))
            c2.set(qn('val'), '1155CC')
            t2 = etree.SubElement(r2, qn('t'))
            t2.text = f'▶ 建议：{suggestion}'
            t2.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')

        if evidence:
            if suggestion:
                br2 = etree.SubElement(p, qn('r'))
                etree.SubElement(br2, qn('br'))
            r3 = etree.SubElement(p, qn('r'))
            rpr3 = etree.SubElement(r3, qn('rPr'))
            c3 = etree.SubElement(rpr3, qn('color'))
            c3.set(qn('val'), '595959')
            t3 = etree.SubElement(r3, qn('t'))
            t3.text = f'◎ 依据：{evidence}'
            t3.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')

    return comments


# ── WPS 兼容：批注段落样式 + commentsExtended ────────────────────────────────

def _inject_comment_styles(styles_root):
    """注入 CommentText(段落) 和 CommentReference(字符) 样式定义。

    WPS 不似 Word 会将缺失的批注样式 fallback 到 Normal，
    缺少这两条样式会导致侧边批注完全不显示。
    """
    existing = set()
    for s in styles_root.iter(qn('style')):
        sid = s.get(qn('styleId'))
        if sid:
            existing.add(sid)

    if 'CommentText' not in existing:
        st = etree.SubElement(styles_root, qn('style'))
        st.set(qn('type'), 'paragraph')
        st.set(qn('styleId'), 'CommentText')
        n = etree.SubElement(st, qn('name'))
        n.set(qn('val'), 'Comment Text')
        b = etree.SubElement(st, qn('basedOn'))
        b.set(qn('val'), 'Normal')
        ppr = etree.SubElement(st, qn('pPr'))
        sp = etree.SubElement(ppr, qn('spacing'))
        sp.set(qn('after'), '200')

    if 'CommentReference' not in existing:
        st = etree.SubElement(styles_root, qn('style'))
        st.set(qn('type'), 'character')
        st.set(qn('styleId'), 'CommentReference')
        n = etree.SubElement(st, qn('name'))
        n.set(qn('val'), 'Comment Reference')

    return styles_root


def _map_comment_para_ids(doc_xml):
    """扫描 doc_xml 中 w:commentRangeStart 建立 cmt_id → 段落 w14:paraId 映射。"""
    mapping = {}
    for crs in doc_xml.iter(qn('commentRangeStart')):
        cmt_id = crs.get(qn('id'))
        if cmt_id is None:
            continue
        p = crs.getparent()
        while p is not None and p.tag != qn('p'):
            p = p.getparent()
        if p is not None:
            para_id = p.get(f'{{{W14}}}paraId')
            if para_id:
                mapping[cmt_id] = para_id
    return mapping


def _build_comments_extended(cmt_id_para_map):
    """构建 <w15:commentsEx> — WPS 通过此文件将批注锚定到段落。

    标准批注文件使用 w15: namespace (Word 2012/2013+) 而非 w14:。
    每个 <w15:commentEx> 关联批注所在段落的 w14:paraId。
    不含 paraIdCommentId（标准版文件无此属性）。
    """
    root = etree.Element(f'{{{W15}}}commentsEx')
    for cmt_id in sorted(cmt_id_para_map.keys(), key=int):
        ex = etree.SubElement(root, f'{{{W15}}}commentEx')
        ex.set(f'{{{W15}}}paraId', cmt_id_para_map[cmt_id])
        ex.set(f'{{{W15}}}done', '0')
    return root


# ── altChunk 转换 ────────────────────────────────────────────────────────

def _convert_altchunk_to_paras(z, docx_path, doc_xml, body):
    """Convert altChunk (MHT/HTML embedded content) to standard w:p paragraphs.

    某些 PDF 转换工具/协作编辑器会产出 altChunk 格式的 docx，其正文不是
    标准的 w:p 而是嵌入的 MHT 文件。此函数读取 MHT，解析为纯文本段落，
    创建对应的 w:p/w:r/w:t XML 元素替换 body 中的 altChunk。
    """
    # Get relationships to map rId → target path
    rels_xml = z.read('word/_rels/document.xml.rels').decode('utf-8')
    rmap = {}
    for m in re.finditer(r'<Relationship[^>]*>', rels_xml):
        rid_m = re.search(r'Id="([^"]+)"', m.group())
        tgt_m = re.search(r'Target="([^"]+)"', m.group())
        if rid_m and tgt_m:
            rmap[rid_m.group(1)] = tgt_m.group(1)

    # Collect altChunk ids and their references
    ac_refs = []
    for ac in body.findall(qn('altChunk')):
        rid = ac.get(rn('id')) or ac.get(f'{{{R}}}id')
        if rid:
            target = rmap.get(rid, '')
            if target.startswith('/'):
                target = target.lstrip('/')
            elif not target.startswith('word/'):
                target = os.path.join('word', target)
            ac_refs.append((ac, rid, target))

    if not ac_refs:
        return

    # Collect all paragraph content from all altChunks
    all_lines = []
    for _, _, target in ac_refs:
        try:
            raw = z.read(target).decode('utf-8', errors='replace')
        except KeyError:
            continue

        # Strip MIME headers before first <html
        idx = raw.find('<html')
        if idx >= 0:
            raw = raw[idx:]

        # Strip HTML tags
        html = re.sub(r'<script[^>]*>.*?</script>', '', raw,
                       flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html,
                       flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</p>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</div>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'<[^>]+>', '', html)
        html = re.sub(r'&nbsp;', ' ', html)
        html = re.sub(r'&amp;', '&', html)
        html = re.sub(r'&lt;', '<', html)
        html = re.sub(r'&gt;', '>', html)
        html = re.sub(r'&quot;', '"', html)

        for line in html.split('\n'):
            line = line.strip().replace('\r', '')
            if line:
                all_lines.append(line)

    if not all_lines:
        print('WARN: altChunk 提取为空', file=sys.stderr)
        return

    # Remove altChunk elements from body, keeping only sectPr
    for ac, _, _ in ac_refs:
        body.remove(ac)

    # Insert w:p elements for each text line
    for line in all_lines:
        p = etree.SubElement(body, qn('p'))  # inserted at end, before sectPr
        r = etree.SubElement(p, qn('r'))
        t = etree.SubElement(r, qn('t'))
        t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        t.text = line

    # Move sectPr to end of body (it was likely after the altChunks)
    sectPrs = body.findall(qn('sectPr'))
    for sp in sectPrs:
        body.remove(sp)
    if sectPrs:
        body.append(sectPrs[-1])

    print(f'  altChunk → {len(all_lines)} w:p paragraphs')


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='02: 审校 findings → docx 回写')
    ap.add_argument('output_dir', help='output_{docname} 目录')
    ap.add_argument('--author', default='审阅助手')
    ap.add_argument('--out', help='输出路径（默认 output_dir/xxx_审阅版.docx）')
    args = ap.parse_args()

    d = args.output_dir
    if not os.path.isdir(d):
        print(f'ERROR: output_dir not found: {d}')
        sys.exit(1)

    # Find original docx
    parent = os.path.dirname(d) or '.'
    stem = os.path.basename(d).removeprefix('output_')
    candidates = [os.path.join(parent, f) for f in os.listdir(parent)
                  if f.startswith(stem) and f.lower().endswith('.docx')
                  and '审阅版' not in f and '修订' not in f
                  and '批注' not in f and '注释' not in f
                  and '参考' not in f and '原始备份' not in f and 'flatten' not in f
                  and not any(t in f for t in ('-批注', '-参考', '标准版', '修改版'))]
    # 选长度最短且排序最前的原始文件（排除附加词汇后的变体）
    candidates.sort(key=lambda x: (len(os.path.basename(x)), x))
    docx_path = candidates[0]
    # 记录选中文件用于诊断
    print(f'源文档: {os.path.basename(docx_path)}')

    # Load findings
    results_dir = os.path.join(d, 'results')
    if not os.path.isdir(results_dir):
        print(f'ERROR: results/ not found: {results_dir}')
        sys.exit(1)
    findings = load_findings(results_dir)
    must_fix = [f for f in findings if f['fix_class'] == 'must_fix']
    annotations = [f for f in findings if f['fix_class'] in ('polish', 'verify')]
    print(f'Load: {len(findings)} findings: {len(must_fix)} must_fix + {len(annotations)} polish/verify')

    if not must_fix and not annotations:
        print('No actionable findings. Skip.')
        return

    # Open docx
    try:
        z = zipfile.ZipFile(docx_path, 'r')
    except Exception as e:
        print(f'ERROR: cannot open {docx_path}: {e}')
        sys.exit(1)

    # Read document.xml
    doc_xml = etree.fromstring(z.read('word/document.xml'))
    body = doc_xml.find(qn('body'))
    para_map, text_map = build_para_map(body)
    print(f'Doc: {docx_path} → {len(para_map)} paragraphs')

    # ── Handle altChunk (embedded MHT/HTML) ──
    if not para_map:
        ac = body.findall(qn('altChunk'))
        if ac:
            print('WARN: 文档为 altChunk 格式，正在转换为标准 w:p 段落...')
            _convert_altchunk_to_paras(z, docx_path, doc_xml, body)
            para_map, text_map = build_para_map(body)
            print(f'  转换后: {len(para_map)} paragraphs')

    # Read settings.xml for trackChanges injection
    settings_xml = None
    if must_fix:
        try:
            settings_xml = etree.fromstring(z.read('word/settings.xml'))
        except KeyError:
            print('WARN: no word/settings.xml, creating skeleton')
            settings_xml = etree.Element(qn('settings'))

    # Read existing comments.xml for comment ID counter
    existing_comments_xml = None
    try:
        existing_comments_xml = etree.fromstring(z.read('word/comments.xml'))
    except (KeyError, etree.XMLSyntaxError):
        pass

    # Start separate ID counters for revisions and comments (avoid collision)
    next_rid = _next_rev_id(doc_xml)
    next_cmt_id = _next_cmt_id(existing_comments_xml)
    author, date_iso = _author_iso(args.author)

    # ── shared comment entries pool (both must_fix and annotations append here) ──
    COMMENT_ENTRIES = []  # (cmt_id, author, date_iso, title, suggestion, evidence)

    # ── apply must_fix: annotation first, then track change (with amplitude gate) ──
    # 批次处理：先定位所有 findings 的段落和位置，按段落后→前排序分批
    # 避免前一批的 run 删除破坏后一批的定位
    track_done = 0
    track_fail = 0
    downgraded = 0       # 超预算降为批注的 must_fix
    track_skip = 0       # budget check 跳过的
    must_ann_done = 0
    must_ann_fail = 0

    # 先为每条 finding 预计算 text position（不修改段落）
    # 同一段落的多条 finding 做位置预排
    pre_located = []  # [(p_el, cpos, current, suggested, fix_class, ...)]
    for fd in must_fix:
        p, txt = _find_para(para_map, text_map, fd['pn'], fd['current'])
        if p is None:
            p, txt = _locate_paragraph(docx_path, fd['pn'], para_map, text_map)
        if p is None:
            track_fail += 1
            print(f'  WARN: anchor "{fd["current"][:30]}" not found (even after full search)')
            continue

        # 在原始文本中找位置
        joined = ''
        for r in p.findall(qn('r')):
            t_el = r.find(qn('t'))
            txt = t_el.text or '' if t_el is not None else ''
            joined += txt
        cpos = joined.find(fd['current'])
        if cpos < 0:
            jn = normalize_text(joined)
            cn = normalize_text(fd['current'])
            cpos2 = jn.find(cn)
            if cpos2 >= 0:
                ri = 0; ni = 0
                while ni < cpos2 and ri < len(joined):
                    if not joined[ri].isspace():
                        ni += 1
                    ri += 1
                cpos = ri
            else:
                track_fail += 1
                print(f'  WARN: cannot pre-locate anchor "{fd["current"][:30]}"')
                continue

        pre_located.append((p, cpos, fd['current'], fd['suggested'],
                           fd.get('fix_class', 'must_fix'),
                           fd.get('reason', ''), fd.get('category', ''),
                           fd.get('description', ''), fd.get('pn', '')))

    # 按段落分组，段内从后往前排
    para_groups = {}
    for pl in pre_located:
        pid = id(pl[0])  # 用段落元素 id 分组
        para_groups.setdefault(pid, {'para': pl[0], 'findings': []})
        para_groups[pid]['findings'].append(pl)

    # 遍历段落组，每个段落做 annotations + 合并 tracked changes
    for gid, g in para_groups.items():
        # 段内从后往前排序（位置大的先处理）
        g['findings'].sort(key=lambda x: x[1], reverse=True)
        p = g['para']
        findings_list = g['findings']

        # Step 1: Apply all annotations (don't affect text structure)
        for pl in findings_list:
            p_el, cpos, cur, sug, fc, reason, cat, desc, pn = pl
            ann_result = apply_annotation(
                p_el, cur, fc,
                next_cmt_id, author, date_iso,
                suggested=sug, reason=reason,
                description=desc, category=cat)
            if ann_result is not False:
                cmt_id, ann_cat, ann_fc, ann_cur, ann_sug, ann_reason, ann_desc = ann_result
                title = f'【必改】{ann_cat}'
                COMMENT_ENTRIES.append((cmt_id, author, date_iso, title, ann_sug, ann_reason))
                next_cmt_id += 1
                must_ann_done += 1
            else:
                must_ann_fail += 1

        # Step 2: Build combined tracked change (single diff per paragraph)
        # Get full paragraph text
        joined = ''
        for r in p.findall(qn('r')):
            t_el = r.find(qn('t'))
            txt = t_el.text or '' if t_el is not None else ''
            joined += txt
        if not joined:
            continue

        # Filter findings that pass the amplitude gate
        passed = []
        for pl in findings_list:
            p_el, cpos, cur, sug, fc, reason, cat, desc, pn = pl
            met = measure_mtf(cur, sug)
            if met['changed_span'] <= MTF_SHORT_SKIP_GATE:
                budget_ok = True
            else:
                min_ret = MTF_SHORT_RETENTION if met['changed_span'] <= 4 else MTF_RETENTION_FLOOR
                budget_ok = (
                    met['changed_span'] <= MTF_CHANGED_SPAN_CAP
                    and met['retention'] >= min_ret
                    and met['boundary_delta'] <= MTF_BOUNDARY_CAP
                )
            if not budget_ok:
                downgraded += 1
                track_skip += 1
                print(f'  ⛔超幅: P{pn} 改{met["changed_span"]}字/保留{met["retention"]}(阀{min_ret})'
                      f'/句界{met["boundary_delta"]} → 仅批注不自动改')
                continue
            passed.append((cpos, cur, sug, pn))

        if not passed:
            continue

        # Apply all changes to text (back to front so positions don't shift)
        modified = list(joined)
        for cpos, cur, sug, pn in sorted(passed, key=lambda x: -x[0]):
            modified[cpos:cpos + len(cur)] = list(sug)
        corrected = ''.join(modified)

        # Single difflib diff against the ORIGINAL paragraph text
        from difflib import SequenceMatcher
        sm = SequenceMatcher(None, joined, corrected, autojunk=False)

        # Build new elements
        template_run = None
        for r in p.findall(qn('r')):
            template_run = r
            break

        new_children = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            orig_seg = joined[i1:i2]
            new_seg = corrected[j1:j2]

            if tag == 'equal' and orig_seg:
                clone = deepcopy(template_run) if template_run else None
                if clone is not None:
                    ct = clone.find(qn('t'))
                    if ct is not None:
                        ct.text = orig_seg
                    _strip_highlight(clone)
                    new_children.append(clone)

            elif tag == 'delete' and orig_seg:
                del_el = etree.Element(qn('del'))
                del_el.set(qn('id'), str(next_rid))
                del_el.set(qn('author'), author)
                del_el.set(qn('date'), date_iso)
                dr = etree.SubElement(del_el, qn('r'))
                _copy_rpr(template_run, dr) if template_run is not None else None
                dt = etree.SubElement(dr, qn('delText'))
                dt.text = orig_seg
                dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_children.append(del_el)

            elif tag == 'insert' and new_seg:
                ins_el = etree.Element(qn('ins'))
                ins_el.set(qn('id'), str(next_rid))
                ins_el.set(qn('author'), author)
                ins_el.set(qn('date'), date_iso)
                ir = etree.SubElement(ins_el, qn('r'))
                if template_run is not None:
                    _copy_rpr(template_run, ir)
                irp = ir.find(qn('rPr'))
                if irp is None:
                    irp = etree.SubElement(ir, qn('rPr'))
                if irp.find(qn('highlight')) is None:
                    etree.SubElement(irp, qn('highlight')).set(qn('val'), 'yellow')
                it = etree.SubElement(ir, qn('t'))
                it.text = new_seg
                it.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                new_children.append(ins_el)

            elif tag == 'replace':
                if orig_seg:
                    del_el = etree.Element(qn('del'))
                    del_el.set(qn('id'), str(next_rid))
                    del_el.set(qn('author'), author)
                    del_el.set(qn('date'), date_iso)
                    dr = etree.SubElement(del_el, qn('r'))
                    if template_run is not None:
                        _copy_rpr(template_run, dr)
                    dt = etree.SubElement(dr, qn('delText'))
                    dt.text = orig_seg
                    dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    new_children.append(del_el)
                if new_seg:
                    ins_el = etree.Element(qn('ins'))
                    ins_el.set(qn('id'), str(next_rid))
                    ins_el.set(qn('author'), author)
                    ins_el.set(qn('date'), date_iso)
                    ir = etree.SubElement(ins_el, qn('r'))
                    if template_run is not None:
                        _copy_rpr(template_run, ir)
                    irp = ir.find(qn('rPr'))
                    if irp is None:
                        irp = etree.SubElement(ir, qn('rPr'))
                    if irp.find(qn('highlight')) is None:
                        etree.SubElement(irp, qn('highlight')).set(qn('val'), 'yellow')
                    it = etree.SubElement(ir, qn('t'))
                    it.text = new_seg
                    it.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    new_children.append(ins_el)

        if new_children:
            next_rid += 1

        # Replace all w:r elements, preserve non-run children (comment markers)
        for r in p.findall(qn('r')):
            p.remove(r)
        for child in new_children:
            p.append(child)
        track_done += 1

    # ── inject trackRevisions in settings.xml ──
    if track_done and settings_xml is not None:
        tr = settings_xml.find(qn('trackRevisions'))
        if tr is None:
            settings_xml.append(etree.Element(qn('trackRevisions')))
        settings_xml.set(mcn('Ignorable'), 'w14')

    # ── apply annotations (polish/verify) ──
    annotate_done = 0
    annotate_fail = 0
    # COMMENT_ENTRIES already initialized in must_fix section
    fc_prefix = {'polish': '💬润色', 'verify': '💬待核'}
    for fd in annotations:
        p, txt = _find_para(para_map, text_map, fd['pn'], fd['current'])
        if p is None:
            p, txt = _locate_paragraph(docx_path, fd['pn'], para_map, text_map)
        if p is None:
            annotate_fail += 1
            print(f'  WARN (annotate): anchor "{fd["current"][:30]}" not found')
            continue
        result = apply_annotation(p, fd['current'], fd['fix_class'],
                                  next_cmt_id, author, date_iso,
                                  suggested=fd.get('suggested', ''),
                                  reason=fd.get('reason', ''),
                                  description=fd.get('description', ''),
                                  category=fd.get('category', ''))
        if result is False:
            annotate_fail += 1
            continue
        cmt_id, cat, fc, cur, sug, reason, desc = result
        prefix = fc_prefix.get(fc, '待核')
        title = f'{prefix}【{cat}】'
        COMMENT_ENTRIES.append((cmt_id, author, date_iso, title, sug, reason))
        annotate_done += 1
        next_cmt_id += 1

    # ── update comments.xml ──
    if COMMENT_ENTRIES:
        try:
            old_root = etree.fromstring(z.read('word/comments.xml'))
        except (KeyError, etree.XMLSyntaxError):
            old_root = etree.fromstring(f'<w:comments xmlns:w="{W}"/>')
        new_comments = build_comment_xml(COMMENT_ENTRIES)
        for child in new_comments:
            old_root.append(child)

    # ── write back ──
    tmp_path = docx_path + '.tmp.docx'
    with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as out:
        for name in z.namelist():
            if name == 'word/document.xml':
                out.writestr(name, etree.tostring(doc_xml, xml_declaration=True, encoding='UTF-8', standalone=True))
            elif name == 'word/comments.xml' and COMMENT_ENTRIES:
                out.writestr(name, etree.tostring(old_root, xml_declaration=True, encoding='UTF-8', standalone=True))
            elif name == 'word/settings.xml' and track_done and settings_xml is not None:
                out.writestr(name, etree.tostring(settings_xml, xml_declaration=True, encoding='UTF-8', standalone=True))
            elif name == 'word/styles.xml' and COMMENT_ENTRIES:
                styles_el = etree.fromstring(z.read(name))
                _inject_comment_styles(styles_el)
                out.writestr(name, etree.tostring(styles_el, xml_declaration=True, encoding='UTF-8', standalone=True))
            else:
                out.writestr(name, z.read(name))

        # Create comments.xml if didn't exist
        if COMMENT_ENTRIES and 'word/comments.xml' not in z.namelist():
            out.writestr('word/comments.xml',
                         etree.tostring(old_root, xml_declaration=True, encoding='UTF-8', standalone=True))
            rebuild_ct = True
            rebuild_rels = True

        # Write commentsExtended.xml for WPS comment-to-paragraph anchoring
        if COMMENT_ENTRIES and 'word/commentsExtended.xml' not in z.namelist():
            cmt_para_map = _map_comment_para_ids(doc_xml)
            if cmt_para_map:
                cmt_ext = _build_comments_extended(cmt_para_map)
                out.writestr('word/commentsExtended.xml',
                             etree.tostring(cmt_ext, xml_declaration=True, encoding='UTF-8', standalone=True))
                rebuild_ct = True
                rebuild_rels = True

    z.close()

    # ── handle rels and content types if comments were added ──
    if COMMENT_ENTRIES:
        # Ensure rebuild flags exist (may have been set by altChunk create path)
        try:
            rebuild_ct
        except NameError:
            rebuild_ct = False
        try:
            rebuild_rels
        except NameError:
            rebuild_rels = False
        comments_ct = 'application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml'
        rebuild_ct = False
        rebuild_rels = False

        # Read existing [Content_Types].xml; create skeleton if missing
        try:
            with zipfile.ZipFile(tmp_path, 'r') as ztmp:
                ct_data = ztmp.read('[Content_Types].xml')
            ct_xml = etree.fromstring(ct_data)
        except (KeyError, etree.XMLSyntaxError):
            ct_xml = etree.Element(f'{{http://schemas.openxmlformats.org/package/2006/content-types}}Types')
        ct_ns = ct_xml.tag.split('}')[0].strip('{') if '}' in ct_xml.tag else ''
        # Check if comments override exists
        has_comment_ct = any(
            child.tag.endswith('Override') and 'comments.xml' in (child.get('PartName', '') or '')
            for child in ct_xml
        )
        if not has_comment_ct:
            ov = etree.SubElement(ct_xml, f'{{{ct_ns}}}Override')
            ov.set('PartName', '/word/comments.xml')
            ov.set('ContentType', comments_ct)
            rebuild_ct = True

        # Register commentsExtended.xml Content Type (WPS requirement)
        has_cmt_ext_ct = any(
            child.tag.endswith('Override') and 'commentsExtended.xml' in (child.get('PartName', '') or '')
            for child in ct_xml
        )
        if not has_cmt_ext_ct:
            ov = etree.SubElement(ct_xml, f'{{{ct_ns}}}Override')
            ov.set('PartName', '/word/commentsExtended.xml')
            ov.set('ContentType', 'application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml')
            rebuild_ct = True

        # Read existing rels; create skeleton if missing
        try:
            with zipfile.ZipFile(tmp_path, 'r') as ztmp:
                rels_data = ztmp.read('word/_rels/document.xml.rels')
            rels_xml = etree.fromstring(rels_data)
        except (KeyError, etree.XMLSyntaxError):
            rels_xml = etree.Element('{http://schemas.openxmlformats.org/package/2006/relationships}Relationships')
        has_comment_rel = any(
            rel.get('Target') and 'comments.xml' in rel.get('Target', '')
            for rel in rels_xml
        )
        if not has_comment_rel:
            r = etree.SubElement(rels_xml, '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')
            r.set('Id', 'rComments1')
            r.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments')
            r.set('Target', 'comments.xml')
            rebuild_rels = True

        # Register commentsExtended.xml relationship (WPS requirement)
        has_cmt_ext_rel = any(
            rel.get('Target') and 'commentsExtended.xml' in (rel.get('Target', '') or '')
            for rel in rels_xml
        )
        if not has_cmt_ext_rel:
            r = etree.SubElement(rels_xml, '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')
            r.set('Id', 'rCommentsExt1')
            r.set('Type', 'http://schemas.microsoft.com/office/2011/relationships/commentsExtended')
            r.set('Target', 'commentsExtended.xml')
            rebuild_rels = True

        if rebuild_ct or rebuild_rels:
            tmp2 = tmp_path + '.2'
            with zipfile.ZipFile(tmp_path, 'r') as zin:
                with zipfile.ZipFile(tmp2, 'w', zipfile.ZIP_DEFLATED) as zout:
                    for name in zin.namelist():
                        if name == '[Content_Types].xml' and rebuild_ct:
                            zout.writestr(name, etree.tostring(ct_xml, xml_declaration=True, encoding='UTF-8', standalone=True))
                        elif name == 'word/_rels/document.xml.rels' and rebuild_rels:
                            zout.writestr(name, etree.tostring(rels_xml, xml_declaration=True, encoding='UTF-8', standalone=True))
                        else:
                            zout.writestr(name, zin.read(name))
            os.replace(tmp2, tmp_path)

    # Rename to _审阅版.docx
    out_path = args.out or os.path.join(d, f'{stem}_审阅版.docx')
    if os.path.exists(out_path):
        os.remove(out_path)
    os.rename(tmp_path, out_path)

    print(f'\nDone: {track_done} track, {track_fail} fail, {must_ann_done} must-ann, {must_ann_fail} must-ann-fail, {annotate_done} polish/verify-ann, {annotate_fail} polish/verify-ann-fail'
          + (f' | ⛔超幅降级批注 {downgraded}' if downgraded else ''))
    print(f'→ {out_path}')


if __name__ == '__main__':
    main()
