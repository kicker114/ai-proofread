import re
from typing import List, Tuple

from .models import Diagnostic, Rule, Token
from .numbering import normalize_number


# 行首 BOM（U+FEFF，Windows/记事本/Word 存中文 md 常见）：容忍但不计入偏移
_BOM = "﻿"
# ATX 标题行：'#'（1-9 个，须在列 0）后分隔空白可有可无（ASCII/全角空格）。
# 兼容 '# 第一章'、'#第一章'、'#　第一章'。BOM 容忍。
_ATX_RE = re.compile(
    r"^" + _BOM + r"?((?P<hashes>#{1,9})[ \t　]*)(?P<content>.*)$")
# setext 下划线：只支持 ===（H1）。--- 与水平分隔线歧义，不支持（避免误报）。
_SETEXT_RE = re.compile(r"^[ \t]*={3,}[ \t]*$")
# 代码/围栏块（``` 或 ~~~）：块内"# 标题"是代码不是标题
_FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")

# 标题内容前可剥离的装饰前缀（迭代剥离，保留绝对偏移）：
#   开括号/引号/反引号、空白、列表符、一个"部/编/篇/卷/册"前缀。
# 例如 '# （第一章）'、'# 第一部分 第一章'、'# - 第一章' 都能识别。
_DECOR_PART = re.compile(
    r"^(?:[（(【［「『《〈〔｛<\"'“”‘’`]+"
    r"|[ \t　]+"
    r"|[-+*][ \t　]+"
    r"|第[一二三四五六七八九十百千零〇0-9０-９]+(?:部分|部|编|篇|卷|册)[ \t　]*)")


def _strip_decorators(content: str, content_start: int) -> Tuple[str, int]:
    for _ in range(8):
        m = _DECOR_PART.match(content)
        if not m or m.end() == 0:
            break
        content = content[m.end():]
        content_start += m.end()
    return content, content_start


# 不剥开圆括号的装饰（供 paren_cn 匹配「（一）」等括号序号）。
# 只剥空白/列表符/引号；保留 （ ( 等让 paren_cn 规则有机会锚定。
_DECOR_PART_NO_BRACKET = re.compile(
    r"^(?:[ \t　]+|[-+*][ \t　]+|[「『‘“”\"'`]+)")


def _strip_decorators_no_bracket(
        content: str, content_start: int) -> Tuple[str, int]:
    m = _DECOR_PART_NO_BRACKET.match(content)
    if not m or m.end() == 0:
        return content, content_start
    return content[m.end():], content_start + m.end()


def _match_part_only(
        content: str, content_start: int,
        compiled: List[Tuple[Rule, "re.Pattern"]],
        heading_level: int | None, line_id: int | None) -> List[Token]:
    """用 part 规则锚定内容起点，返回「第X部/编/卷」token（最多一个）。"""
    norm, offmap = _normalize_for_match(content)
    for rule, pat in compiled:
        if rule.id != "part":
            continue
        pm = pat.match(norm)
        if pm is None:
            continue
        num_val = normalize_number(pm.groupdict(), rule.numbering_normalize)
        start = content_start + offmap[pm.start()]
        end = content_start + offmap[pm.end() - 1] + 1
        return [Token(
            start=start,
            end=end,
            rule_id="part",
            level=rule.level,
            priority=rule.priority,
            raw_text=content[offmap[pm.start()]:offmap[pm.end() - 1] + 1],
            number_value=num_val,
            heading_level=heading_level,
            line_id=line_id,
        )]
    return []


# 全角字符 → ASCII（用于标题内容归一化后再匹配规则，保持偏移不变）：
#   全角数字 ０-９ → 0-9，全角句点 ．(U+FF0E) → .(U+002E)
#   繁體「節」 → 节（港台/繁体书稿常见第X節）
#   全角拉丁字母（第Ｘ章）与小写 ivxlcdm（第iv章）→ ASCII 大写
_FULLWIDTH_TRANS = {
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "．": ".",
    "節": "节",
    "Ｉ": "I", "Ｖ": "V", "Ｘ": "X", "Ｌ": "L", "Ｃ": "C",
    "Ｄ": "D", "Ｍ": "M",
    "i": "I", "v": "V", "x": "X", "l": "L", "c": "C", "d": "D", "m": "M",
}
# Unicode 罗马数字（U+2160-216B）→ ASCII 序列（多字符展开）
_UNICODE_ROMAN = {
    "Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V",
    "Ⅵ": "VI", "Ⅶ": "VII", "Ⅷ": "VIII", "Ⅸ": "IX", "Ⅹ": "X",
    "Ⅺ": "XI", "Ⅻ": "XII",
}


def _normalize_for_match(content: str) -> Tuple[str, List[int]]:
    """把标题内容的全角字符归一化为 ASCII，同时记录原文映射。

    返回 (归一化后的 content, offset_map)，其中 offset_map[i] 是归一化串第
    i 个字符在原文中的绝对偏移（原文 → 原样，全角 → 对应 ASCII 位置）。
    这样 token 起点/终点可精确映射回原文，保持 report 高亮与偏移切片一致性。
    Unicode 罗马数字（Ⅻ → XII）是 1→N 展开：norm 的每个字符都映射回同一个
    原文偏移，token 偏移切片仍指向原文的 Ⅻ 位置。
    """
    out: List[str] = []
    mapping: List[int] = []
    for offset, ch in enumerate(content):
        if ch in _UNICODE_ROMAN:
            out.append(_UNICODE_ROMAN[ch])
            mapping.extend([offset] * len(_UNICODE_ROMAN[ch]))
        else:
            out.append(_FULLWIDTH_TRANS.get(ch, ch))
            mapping.append(offset)
    return "".join(out), mapping


def _match_rules(
        content: str, content_start: int,
        compiled: List[Tuple[Rule, "re.Pattern"]],
        require_space_after: bool,
        plain_only_prefix: bool,
        heading_level: int | None = None,
    line_id: int | None = None) -> List[Token]:
    """对标题内容做锚定匹配（re.match 于内容起点，规则间互斥）。

    require_space_after=True：编号后必须是空白/EOL 才接受（用于无 `#` 的
      普通文本行，把"第三章 引言"标题与"第三章书评见附录"正文区分开）。
    plain_only_prefix=True：只允许"第X章/第X节"这类以"第"开头的规则参与
      普通行识别（裸数字"1."太歧义：列表/小数都不该在无 # 时算作小节）。
    heading_level：markdown ATX 标题的 `#` 数量（setext 为 1，普通行为 None）。
      有值时写入 Token，供 builder 建树时优先用它决定层级，避免规则层级
      （如"目"=3）与作者标题层级（H2）冲突导致的误报。
    line_id：标题行唯一标识。同一行内多个 token（合并标题的章+节）共享，
      供 builder 把同一行的多层编号按规则层级嵌套，而不是拆成独立根。

    全角归一化：规则 pattern 的字符类仅 ASCII（`[0-9]+` / `[.]`），中文排版
    常见的全角数字「第１章」「1．」会先归一化为 ASCII 再匹配；token 偏移映射
    回原文，raw_text 用原文子串，report 高亮与偏移切片不受影响。
    """
    def _match_content_once(c: str, cs: int) -> List[Token]:
        """对给定标题内容做「锚定 + 合并」匹配，返回 token 列表。"""
        norm, offmap = _normalize_for_match(c)
        out: List[Token] = []

        def _token_level(rule: Rule, m: "re.Match") -> int:
            # 多级数字（1.1 / 1.1.1）按点数映射层级：1.1→节(2)、1.1.1→目(3)
            if rule.id == "num_dot":
                return m.group(0).count(".") + 1
            return rule.level

        def _make_token(rule: Rule, m: "re.Match") -> Token | None:
            num_val = normalize_number(m.groupdict(), rule.numbering_normalize)
            start = cs + offmap[m.start()]
            end = cs + offmap[m.end() - 1] + 1
            return Token(
                start=start,
                end=end,
                rule_id=rule.id,
                level=_token_level(rule, m),
                priority=rule.priority,
                raw_text=c[offmap[m.start()]:offmap[m.end() - 1] + 1],
                number_value=num_val,
                heading_level=heading_level,
                line_id=line_id,
            )

        # 目编号（N.）歧义检查：编号后（含空白后）若紧跟"数字+量词单位"且
        # **单位之后无其他词**（12. 5亿 → "5亿"后即结束），是纯数量/小数；
        # 若单位后还有名词（1. 2亿用户 → "2亿"后接"用户"），是真编号小节标题，
        # 保留。规则正则的 (?=\D|$) 只挡「点后紧贴数字」。
        _UNIT_AFTER_NUM = re.compile(r"^([0-9]+[亿萬億万元元％%])([^，。；;]*)")

        def _is_subsection_decimal(rule: Rule, m: "re.Match") -> bool:
            if rule.id not in ("subsection", "num_dot"):
                return False
            rest = norm[m.end():].lstrip(" \t　")
            if rule.id == "subsection":
                um = _UNIT_AFTER_NUM.match(rest)
                if not um:
                    return False
                return not um.group(2).strip(" \t　")  # 单位后无其他词 → 纯数量
            # num_dot（1.1 / 12.9 / 2024.1）：
            #   - 后跟量词单位（12.9亿 / 12. 5亿）→ 纯数量/小数
            #   - 点前是 4 位年份（2024.1）→ 日期
            # 否则是真多级编号（1.1 研究背景）保留
            if re.match(r"^[0-9]*[亿萬億万元元％%]", rest):
                return True
            head = m.group(0).split(".")[0]
            if head.isdigit() and len(head) == 4 and 1900 <= int(head) <= 2099:
                return True
            return False

        # 起点匹配（保留 require_space_after 语义：普通行章节号后须空白）
        seed_norm_end = 0
        for rule, pat in compiled:
            if plain_only_prefix and rule.id in ("special", "subsection", "num_dot"):
                continue  # 普通行排除易与列表/正文混淆的规则（数字句点/特殊词）
            pm = pat.match(norm)
            if pm is None:
                continue
            if require_space_after and rule.id in ("chapter", "section"):
                after = norm[pm.end():]
                if after and after[0] not in " \t　":
                    continue
            if _is_subsection_decimal(rule, pm):
                continue
            out.append(_make_token(rule, pm))
            seed_norm_end = max(seed_norm_end, pm.end())

        # 合并：标题行内后续编号（合并标题「第1章 第1节」「第一章 第一节」）。
        # 只接受「规则层级严格递增 + 编号间仅空白/空」的连续编号——这是标题自身
        # 层级结构的信号；叙述性提及（罗杰斯第三章书评 / 第二章的比较 /
        # 参考文献「第2章」）不满足「层递」或「紧邻前一编号」，被排除。
        if out:
            last_end = seed_norm_end
            max_level = max(t.level for t in out)
            part_child_extended = False  # 是否已合并 part 的（同 level）子级
            candidates = []
            for rule, pat in compiled:
                if plain_only_prefix and rule.id in ("special", "subsection", "num_dot"):
                    continue
                for m in pat.finditer(norm, seed_norm_end):
                    candidates.append((m.start(), m.end(), rule, m))
            candidates.sort(key=lambda x: (x[0], x[1]))
            for start, end, rule, m in candidates:
                if start < last_end:
                    continue
                # 编号间只能空白/空，或至多一个「第」字（「第1.小节」的 1. 前缀）。
                # 叙述性提及（第二章的比较 / 参考文献「第2章」）含正文词 → 排除；
                # 同层重复（第二章 第二章）被下面的 level 递增挡住。
                if norm[last_end:start].strip(" \t　") not in ("", "第"):
                    continue
                if _token_level(rule, m) <= max_level:
                    # part 允许同级的声明子级（卷一 第一章：part=1, chapter=1）。
                    # 只放行第一个扩展；之后仍同级（卷一 第一章 第二章）排除。
                    if (not part_child_extended
                            and any(t.rule_id == "part" for t in out)
                            and rule.id in ("chapter", "section", "subsection")):
                        part_child_extended = True
                    else:
                        continue
                if _is_subsection_decimal(rule, m):
                    continue
                # 编号后检查（防幽灵节 / 防普通行叙述行）。只对「第X章/第X节」
                # 生效：它们后紧跟无空白的正文词（第一章 第一节的比较）是叙述；
                # 「N.」目编号后紧跟正文词（第1.小节 / 1. 2亿用户）是正常标题，
                # 不检查。
                if rule.id in ("chapter", "section"):
                    if require_space_after:
                        # 普通文本行：合并编号后也须空白（与起点一致），
                        # 「第1章 第1节内容见第三章」的第1节是叙述 → 排除
                        after = norm[end:]
                        if after and after[0] not in " \t　":
                            continue
                    else:
                        # ATX 标题：合并编号后若紧跟正文词（非空白、非"第"、
                        # 非数字），是叙述性提及（第一章 第一节的比较）→ 排除
                        after = norm[end:]
                        if (after and after[0] not in " \t　"
                                and not after.startswith("第") and not after[0].isdigit()):
                            continue
                out.append(_make_token(rule, m))
                last_end = end
                max_level = _token_level(rule, m)
        return out

    # 第一遍：剥离装饰前缀后锚定——捕获「（第一章）」「第一章」等。
    stripped, stripped_start = _strip_decorators(content, content_start)
    out = _match_content_once(stripped, stripped_start)
    if out:
        # 剥离掉 part 前缀（第一部/第1卷/第1部分）时，总是补充 part token：
        # 「第一部 第1节」「第一部 第一章」都保留部级。out 已含 part
        # （卷一 第1节 seed 命中）时不重复补。
        if not any(t.rule_id == "part" for t in out):
            part_toks = _match_part_only(
                content, content_start, compiled, heading_level, line_id)
            if part_toks:
                return part_toks + out
        return out
    # 第二遍：剥离非括号装饰后锚定——捕获「（一）」「- （一）」「第一部」等。
    # 括号序号的开括号不是装饰，须保留给 paren_cn 规则（_strip_decorators
    # 会把 （ 当装饰剥掉，导致括号序号漏检）。
    second, second_start = _strip_decorators_no_bracket(
        content, content_start)
    return _match_content_once(second, second_start)


def scan_text(text: str, rules: List[Rule]) -> Tuple[List[Token], List[Diagnostic]]:
    """扫描文档结构：识别 markdown 标题（ATX `#` / setext `===` / 无 `#` 的
    "第X章 标题"普通行），不再全文正则扫描。

    原实现对全文做 `rule.pattern.finditer`，正文里的「在第二章中」「12.9亿」
    等表述被误判成结构 token，产生大量误报。现在：

      - ATX 标题：`#{1,9}` 后可有可无分隔（含全角空格/BOM 容忍），
        标题内容起点锚定各规则 pattern；内容前可剥离括号/部卷前缀等装饰。
      - setext 标题：`===` 下划线前的文本行（`---` 与水平分隔线歧义，不支持）。
      - 普通文本行：仅当行首是「第X章/第X节」且编号后为空白时视为标题
        （覆盖无 `#` 的纯文本书稿；"在第二章中"这类行首不匹配、"
        第三章书评见附录"编号后无空白均不误报）。裸数字（列表/小数）不在此识别。
      - 代码围栏（```/~~~）内的假标题被跳过。

    subsection 规则 `N.` 必须后跟非数字（rules.example.json 已加
    `(?=\\D|$)` 前瞻），避免把标题起点的「12.9亿」「2024.1」当目编号。
    """
    tokens: List[Token] = []
    diags: List[Diagnostic] = []
    compiled: List[Tuple[Rule, re.Pattern]] = [
        (rule, re.compile(rule.pattern)) for rule in rules
    ]

    lines = text.splitlines(keepends=True)

    # 第一遍：标记 setext 标题文本行（其下一行是 ===；--- 与水平线歧义不支持）。
    # 这样的行必须以 heading_level=1 参与建树——若让它在普通行分支以
    # heading_level=None 先产出，后续「## 1.」嵌其下会因"任一方无标题层级"
    # 退回规则检查而误报 level_mismatch。
    setext_lines: set[int] = set()
    for i in range(len(lines) - 1):
        body = lines[i].rstrip("\r\n")
        nxt = lines[i + 1].rstrip("\r\n")
        if (body.strip() and _SETEXT_RE.match(nxt)
                and _ATX_RE.match(body) is None):
            setext_lines.add(i)

    in_fence = False
    line_start = 0
    for idx, line in enumerate(lines):
        body = line.rstrip("\r\n")
        fm = _FENCE_RE.match(body)
        if fm is not None:
            in_fence = not in_fence  # 开/关围栏
            line_start += len(line)
            continue
        if in_fence:
            line_start += len(line)
            continue
        if not body.strip():
            line_start += len(line)
            continue

        am = _ATX_RE.match(body)
        if am is not None:
            content = am.group("content")
            content_start = line_start + am.start("content")
            hashes = len(am.group("hashes").replace(" ", ""))  # 标题层级 = # 数量
            tokens.extend(_match_rules(
                content, content_start, compiled,
                require_space_after=False, plain_only_prefix=False,
                heading_level=hashes, line_id=line_start))
        elif idx in setext_lines:
            # setext 标题文本行：作为 H1 参与建树
            indent = len(body) - len(body.lstrip(" \t"))
            tokens.extend(_match_rules(
                body.strip(), line_start + indent, compiled,
                require_space_after=False, plain_only_prefix=False,
                heading_level=1, line_id=line_start))
        elif _SETEXT_RE.match(body):
            pass  # === 下划线行本身，其标题文本行已在上一行处理
        else:
            # 普通文本行：仅「第X章/第X节」行首锚定 + 编号后空白
            tokens.extend(_match_rules(
                body, line_start, compiled,
                require_space_after=True, plain_only_prefix=True,
                heading_level=None, line_id=line_start))

        line_start += len(line)

    tokens.sort(key=lambda t: (t.start, -t.priority, -(t.end - t.start)))
    tokens, overlap_diags = _resolve_overlaps(tokens)
    diags.extend(overlap_diags)
    return tokens, diags


def _resolve_overlaps(tokens: List[Token]) -> Tuple[List[Token], List[Diagnostic]]:
    if not tokens:
        return tokens, []
    result: List[Token] = []
    diags: List[Diagnostic] = []
    current_group: List[Token] = []

    def commit_group(group: List[Token]):
        if not group:
            return
        # 选择保留：优先级高 -> 跨度长 -> 起点早
        best = sorted(group, key=lambda t: (-t.priority, -(t.end - t.start), t.start))[0]
        result.append(best)
        for t in group:
            if t is not best:
                diags.append(Diagnostic(
                    kind="overlap_discard",
                    message=f"重叠冲突：丢弃 {t.rule_id} 于 [{t.start},{t.end})，保留 {best.rule_id}",
                    position=(t.start, t.end),
                    extra={"kept": best.rule_id, "discarded": t.rule_id},
                ))

    # 扫描分组：有重叠的归为一组
    # group_start 保留用于将来更复杂的分组策略（当前未使用）
    _group_start = tokens[0].start
    group_end = tokens[0].end
    current_group = [tokens[0]]
    for t in tokens[1:]:
        if t.start < group_end:  # overlap
            current_group.append(t)
            if t.end > group_end:
                group_end = t.end
        else:
            commit_group(current_group)
            current_group = [t]
            _group_start = t.start
            group_end = t.end
    commit_group(current_group)

    result.sort(key=lambda t: t.start)
    return result, diags
