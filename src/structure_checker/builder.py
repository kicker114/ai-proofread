import re
from typing import Dict, List, Tuple

from .models import Diagnostic, Node, Rule, Token


def _node_real_level(n: Node) -> int:
    """节点真实层级：有 markdown 标题层级（`#` 数量）用它，否则用规则 level。

    使「## 1. 概述」这类 H2 编号小节按作者意图作为二级标题，与「## 第一节」
    同级，不再因规则判定其为"目"(level3) 而误报 hierarchy_gap。
    """
    h = n.token.heading_level
    return h if h is not None else n.token.level


def _node_structure_level(n: Node) -> int:
    """节点结构层级：用于「同一标题行的多层编号」嵌套判定。

    同一行内（line_id 相同）多个 token 是"一个标题的多层编号"（如合并标题
    「第1章 第1节」的章+节），应按规则层级嵌套，而不是按 heading_level
    拆成独立根。此时用 token.rule 的 level（章1/节2/目3）。
    """
    return n.token.level


def _same_line(a: Node, b: Node) -> bool:
    return (a.token.line_id is not None and b.token.line_id is not None
            and a.token.line_id == b.token.line_id)


def build_tree(tokens: List[Token], rule_map: Dict[str, Rule]) -> Tuple[List[Node], List[Diagnostic]]:
    nodes: List[Node] = [Node(token=t) for t in tokens]
    diags: List[Diagnostic] = []
    roots: List[Node] = []
    stack: List[Node] = []

    # 记录每个标题行的最大规则层级（供跨行同号 / 混合体系判定）
    line_max: Dict[int, int] = {}
    for t in tokens:
        if t.line_id is not None:
            line_max[t.line_id] = max(line_max.get(t.line_id, 0), t.level)
    for node in nodes:
        node.line_max_level = line_max.get(node.token.line_id, 0)

    for node in nodes:
        # 清理无效 number 的情况：不致命，后续连续性检查会处理
        while stack and not _same_line(node, stack[-1]) \
                and _node_real_level(node) <= _node_real_level(stack[-1]):
            stack.pop()
        # 同一行内：后出现的编号若规则层级更深（章→节），直接作为子嵌套
        if stack and _same_line(node, stack[-1]) \
                and _node_structure_level(node) > _node_structure_level(stack[-1]):
            parent = stack[-1]
            parent.children.append(node)
            node.parent = parent
            stack.append(node)
            _check_hierarchy(parent, node, rule_map, diags)
            continue
        if not stack:
            roots.append(node)
            stack.append(node)
            # 孤立层级检查：非 optional 的规则成为根节点（缺少父层级）
            _check_orphan_root(node, rule_map, diags)
        else:
            parent = stack[-1]
            parent.children.append(node)
            node.parent = parent
            stack.append(node)

            # 层级合法性校验（hierarchy_gap / level_mismatch）
            _check_hierarchy(parent, node, rule_map, diags)
    return roots, diags


def _check_orphan_root(
        node: Node,
        rule_map: Dict[str, Rule],
        diags: List[Diagnostic]) -> None:
    """检查成为根节点的层级是否缺少父层级。

    规则 JSON 的 `optional` 字段声明该层级是否可独立存在：
      - optional=True（如 "目"）→ 可作为根，不报错
      - optional=False 且存在更浅层级的规则（如 "节" 无 "章"）→ hierarchy_gap
    """
    rule = rule_map.get(node.rule_id)
    if rule is None or rule.optional:
        return
    has_parent_type = any(r.level < node.level for r in rule_map.values())
    if not has_parent_type:
        return  # 已是顶层规则（如"章"），根节点合法
    diags.append(Diagnostic(
        kind="hierarchy_gap",
        message=f"{rule.name} 缺少父层级（独立出现在文档根部）",
        position=(node.token.start, node.token.end),
        extra={"rule": node.rule_id, "child_level": node.level},
    ))


def _check_hierarchy(
        parent: Node,
        node: Node,
        rule_map: Dict[str, Rule],
        diags: List[Diagnostic]) -> None:
    """验证 parent→node 的嵌套关系是否符合标题层级。

    规则 JSON 中每个 level 的 `children` 字段声明了合法的直接子规则。
    层级合法性判定优先用 markdown 标题层级（`#` 数量）：
      - 「# 第一章」+「## 1. 概述」→ H1→H2 相邻，合法（作者用 H2 编号小节，
        与「## 第一节」同级，不再因规则判定"目"=level3 而误报 gap）。
      - 「# 第一章」+「### 1.」→ H1→H3 跳级 → hierarchy_gap（真实跳级仍报）。
      - 「## 第一节」+「### 1.」→ H2→H3 相邻，合法。
    双方都有 markdown 层级时按标题层级判定（作者意图优先）；任一方无
    （普通文本行标题、setext）退回按规则 level + children 声明判定。
    """
    parent_rule = rule_map.get(parent.rule_id)
    node_rule = rule_map.get(node.rule_id)
    if parent_rule is None or node_rule is None:
        return  # 虚拟根或未知规则，跳过

    # 同一标题行内的多层编号（如合并标题「第1章 第1节」的章+节）：按规则
    # 层级嵌套，不是层级跳变，直接放行（建树已按结构层级挂好）。
    if _same_line(parent, node):
        if node.token.level > parent.token.level:
            return
        # 同级/反序（如「第1章 第1章」）属异常，仍报 level_mismatch
        parent_rule = rule_map.get(parent.rule_id)
        node_rule = rule_map.get(node.rule_id)
        diags.append(Diagnostic(
            kind="level_mismatch",
            message=f"{node_rule.name if node_rule else node.rule_id} 不应与 "
                    f"{parent_rule.name if parent_rule else parent.rule_id} 同级/反序",
            position=(node.token.start, node.token.end),
            extra={"rule": node.rule_id, "parent": parent.rule_id,
                   "same_line": True},
        ))
        return

    parent_heading = parent.token.heading_level
    node_heading = node.token.heading_level
    p_lvl = parent_heading if parent_heading is not None else parent.level
    n_lvl = node_heading if node_heading is not None else node.level

    if parent_heading is not None and node_heading is not None:
        # 双方都有明确标题层级：按标题层级判定（作者意图优先）
        if n_lvl > p_lvl + 1:
            diags.append(Diagnostic(
                kind="hierarchy_gap",
                message=f"{node_rule.name}（标题层级 H{n_lvl}）直接嵌套在 "
                        f"{parent_rule.name}（H{p_lvl}）下，中间层级缺失",
                position=(node.token.start, node.token.end),
                extra={"rule": node.rule_id, "parent": parent.rule_id,
                       "child_level": n_lvl, "parent_level": p_lvl,
                       "heading": True},
            ))
        elif n_lvl <= p_lvl:
            diags.append(Diagnostic(
                kind="level_mismatch",
                message=f"{node_rule.name}（H{n_lvl}）层级不应低于或等于 "
                        f"{parent_rule.name}（H{p_lvl}）",
                position=(node.token.start, node.token.end),
                extra={"rule": node.rule_id, "parent": parent.rule_id,
                       "child_level": n_lvl, "parent_level": p_lvl,
                       "heading": True},
            ))
        return

    # 任一方无 markdown 层级（普通文本行标题）→ 按规则 level + children 判定
    allowed_children = parent_rule.children
    if allowed_children and node.rule_id in allowed_children:
        return  # 合法直接子规则

    # 不合法嵌套
    if n_lvl > p_lvl + 1:
        # 中间层级缺失（如 章→目，中间缺 节）
        diags.append(Diagnostic(
            kind="hierarchy_gap",
            message=f"{node_rule.name} 直接嵌套在 {parent_rule.name} 下，中间层级缺失",
            position=(node.token.start, node.token.end),
            extra={"rule": node.rule_id, "parent": parent.rule_id,
                   "child_level": n_lvl, "parent_level": p_lvl},
        ))
    elif allowed_children:
        # 同层或反序嵌套，或不在声明范围内
        diags.append(Diagnostic(
            kind="level_mismatch",
            message=f"{node_rule.name} 不应直接嵌套在 {parent_rule.name} 下",
            position=(node.token.start, node.token.end),
            extra={"rule": node.rule_id, "parent": parent.rule_id},
        ))


_CN_NUM_CHARS = "一二三四五六七八九十百千零〇两壹贰叁肆伍陆柒捌玖拾"


def _numbering_system(n: Node) -> str | None:
    """从 token 原文推断编号体系（cn/ar/rm）。"""
    rt = n.token.raw_text
    core = re.sub(r"^第", "", rt)
    core = re.sub(r"[章节]$", "", core)
    if not core:
        return None
    if re.fullmatch(r"[0-9０-９]+", core):
        return "ar"
    if re.fullmatch(f"[{_CN_NUM_CHARS}]+", core):
        return "cn"
    if re.fullmatch(r"[IVXLCDMＩＶＸＬＣＤＭⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹ]+", core):
        return "rm"
    return None


def _is_single_letter_roman(n: Node) -> bool:
    """是否为草稿占位罗马（只认 X / 全角 Ｘ）。

    X 是书稿草稿中最常见的占位符（章号未定时写「第X章」）；I/V/C/M 等
    单字母本身是合法罗马数字（第I章=1、第V章=5、第C章=100），在混合
    体系文档中不应误伤为占位。附录标签「第C章」等按合法罗马处理。
    """
    rt = n.token.raw_text
    core = re.sub(r"^第", "", rt)
    core = re.sub(r"[章节]$", "", core)
    return core in ("X", "Ｘ")


def _first_section_number(n: Node) -> int | None:
    """该节点 children 里第一个节（rule_id=section）的编号。"""
    for c in n.children:
        if c.rule_id == "section" and c.token.number_value is not None:
            return c.token.number_value
    return None


def validate_continuity(roots: List[Node], rule_map: Dict[str, Rule]) -> List[Diagnostic]:
    diags: List[Diagnostic] = []

    def dfs(parent: Node):
        if not parent.children:
            return
        # 在同一父节点下检查连续性
        # 将同层的子节点按 rule_id 分桶，分别检查
        buckets: Dict[str, List[Node]] = {}
        for c in parent.children:
            buckets.setdefault(c.rule_id, []).append(c)

        for rid, items in buckets.items():
            rule = rule_map[rid]
            # 混合编号体系占位检测：桶内既有阿拉伯/中文编号，又有单字母罗马
            # （草稿占位「第X章」/附录「第C章」）→ 单字母罗马判为占位，
            # number 置 None 触发 number_missing，而非连续性误报。
            systems = {_numbering_system(n) for n in items}
            single_romans = [n for n in items if _is_single_letter_roman(n)]
            if len(systems) > 1 and single_romans:
                for n in single_romans:
                    n.token.number_value = None

            prev = None
            prev_node: Node | None = None
            seq = []
            for n in items:
                seq.append((n, n.token.number_value))
                if rule.numbering_continuity == "none":
                    continue
                if n.token.number_value is None:
                    diags.append(Diagnostic(
                        kind="number_missing",
                        message=f"{rule.name} 缺少可解析编号",
                        position=(n.token.start, n.token.end),
                        extra={"rule": rid, "text": n.token.raw_text},
                    ))
                    continue
                if prev is None:
                    prev = n.token.number_value
                    prev_node = n
                else:
                    curr = n.token.number_value
                    # 编号体系切换（罗马前言 I/II → 阿拉伯正文 1/2，或中文→阿拉伯）：
                    # 数值不可比，不检查连续性（真实书稿常见"前言罗马 + 正文阿拉伯"）
                    if (prev_node is not None
                            and _numbering_system(prev_node) != _numbering_system(n)):
                        prev = curr
                        prev_node = n
                        continue
                    if rule.numbering_continuity == "strict_increase":
                        # 合并标题多行展开：同一章号重复（第1章 第1节 /
                        # 第1章 第2节），仅当**前后两行都含更深层编号**（同章
                        # 多节展开）才合法；同时检查两行的节号连续性
                        # （第1章 第1节 / 第1章 第3节 → 节 1→3 应报）。
                        if (curr == prev and n.line_max_level > n.level
                                and prev_node is not None
                                and prev_node.line_max_level > prev_node.level):
                            ps = _first_section_number(prev_node)
                            cs = _first_section_number(n)
                            if (ps is not None and cs is not None
                                    and cs != ps + 1):
                                diags.append(Diagnostic(
                                    kind="continuity_error",
                                    message=f"{rule.name}（多节展开）节编号不连续："
                                            f"{ps} → {cs}",
                                    position=(n.token.start, n.token.end),
                                    extra={"rule": rid, "prev": ps, "curr": cs,
                                           "expand": True},
                                ))
                            prev = curr
                            prev_node = n
                            continue
                        if curr != prev + 1:
                            diags.append(Diagnostic(
                                kind="continuity_error",
                                message=f"{rule.name} 编号不连续：{prev} → {curr}",
                                position=(n.token.start, n.token.end),
                                extra={"rule": rid, "prev": prev, "curr": curr},
                            ))
                        prev = curr
                        prev_node = n
                    elif rule.numbering_continuity == "increase_or_restart":
                        if not (curr == 1 or curr == (prev + 1)):
                            diags.append(Diagnostic(
                                kind="continuity_error",
                                message=f"{rule.name} 需递增或重启为1：{prev} → {curr}",
                                position=(n.token.start, n.token.end),
                                extra={"rule": rid, "prev": prev, "curr": curr},
                            ))
                        prev = curr
                        prev_node = n

        for c in parent.children:
            dfs(c)

    # 构造一个虚拟根，便于统一处理多根情况
    virtual = Node(token=_virtual_token())
    virtual.children = roots
    for r in roots:
        r.parent = virtual
    dfs(virtual)
    return diags


def _virtual_token():
    # 简易的虚拟 token，用于校验遍历
    from .models import Token as _T
    return _T(start=-1, end=-1, rule_id="__root__", level=0, priority=0, raw_text="__root__")


