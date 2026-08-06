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
    r"|第[一二三四五六七八九十百千零〇0-9]+(?:部分|编|篇|卷|册)[ \t　]*)")


def _strip_decorators(content: str, content_start: int) -> Tuple[str, int]:
    for _ in range(8):
        m = _DECOR_PART.match(content)
        if not m or m.end() == 0:
            break
        content = content[m.end():]
        content_start += m.end()
    return content, content_start


def _match_rules(
        content: str, content_start: int,
        compiled: List[Tuple[Rule, "re.Pattern"]],
        require_space_after: bool,
        plain_only_prefix: bool) -> List[Token]:
    """对标题内容做锚定匹配（re.match 于内容起点，规则间互斥）。

    require_space_after=True：编号后必须是空白/EOL 才接受（用于无 `#` 的
      普通文本行，把"第三章 引言"标题与"第三章书评见附录"正文区分开）。
    plain_only_prefix=True：只允许"第X章/第X节"这类以"第"开头的规则参与
      普通行识别（裸数字"1."太歧义：列表/小数都不该在无 # 时算作小节）。
    """
    content, content_start = _strip_decorators(content, content_start)
    out: List[Token] = []
    for rule, pat in compiled:
        if plain_only_prefix and not re.match(r"\(?第", rule.pattern):
            continue
        pm = pat.match(content)  # 锚定于（剥离装饰后的）内容起点
        if pm is None:
            continue
        if require_space_after:
            after = content[pm.end():]
            if after and after[0] not in " \t　":
                continue
        num_val = normalize_number(pm.groupdict(), rule.numbering_normalize)
        out.append(Token(
            start=content_start + pm.start(),
            end=content_start + pm.end(),
            rule_id=rule.id,
            level=rule.level,
            priority=rule.priority,
            raw_text=pm.group(0),
            number_value=num_val,
        ))
    return out


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

    in_fence = False
    prev_line: Tuple[str, int] | None = None  # 上一条普通文本行（供 setext）
    line_start = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        fm = _FENCE_RE.match(body)
        if fm is not None:
            in_fence = not in_fence  # 开/关围栏
            prev_line = None
            line_start += len(line)
            continue
        if in_fence:
            line_start += len(line)
            continue
        if not body.strip():
            prev_line = None
            line_start += len(line)
            continue

        am = _ATX_RE.match(body)
        if am is not None:
            content = am.group("content")
            content_start = line_start + am.start("content")
            tokens.extend(_match_rules(
                content, content_start, compiled,
                require_space_after=False, plain_only_prefix=False))
            prev_line = None  # ATX 行后接下划线属异常，不参与 setext
        elif _SETEXT_RE.match(body):
            if prev_line is not None:
                pbody, pstart = prev_line
                tokens.extend(_match_rules(
                    pbody.strip(), pstart + (len(pbody) - len(pbody.lstrip())),
                    compiled, require_space_after=False,
                    plain_only_prefix=False))
            prev_line = None
        else:
            # 普通文本行：仅「第X章/第X节」行首锚定 + 编号后空白
            line_tokens = _match_rules(
                body, line_start, compiled,
                require_space_after=True, plain_only_prefix=True)
            tokens.extend(line_tokens)
            prev_line = None if line_tokens else (body, line_start)

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
