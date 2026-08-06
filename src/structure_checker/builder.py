from typing import Dict, List, Tuple

from .models import Diagnostic, Node, Rule, Token


def _node_real_level(n: Node) -> int:
    """节点真实层级：有 markdown 标题层级（`#` 数量）用它，否则用规则 level。

    使「## 1. 概述」这类 H2 编号小节按作者意图作为二级标题，与「## 第一节」
    同级，不再因规则判定其为"目"(level3) 而误报 hierarchy_gap。
    """
    h = n.token.heading_level
    return h if h is not None else n.token.level


def build_tree(tokens: List[Token], rule_map: Dict[str, Rule]) -> Tuple[List[Node], List[Diagnostic]]:
    nodes: List[Node] = [Node(token=t) for t in tokens]
    diags: List[Diagnostic] = []
    roots: List[Node] = []
    stack: List[Node] = []

    for node in nodes:
        # 清理无效 number 的情况：不致命，后续连续性检查会处理
        while stack and _node_real_level(node) <= _node_real_level(stack[-1]):
            stack.pop()
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
            prev = None
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
                else:
                    curr = n.token.number_value
                    if rule.numbering_continuity == "strict_increase":
                        if curr != prev + 1:
                            diags.append(Diagnostic(
                                kind="continuity_error",
                                message=f"{rule.name} 编号不连续：{prev} → {curr}",
                                position=(n.token.start, n.token.end),
                                extra={"rule": rid, "prev": prev, "curr": curr},
                            ))
                        prev = curr
                    elif rule.numbering_continuity == "increase_or_restart":
                        if not (curr == 1 or curr == (prev + 1)):
                            diags.append(Diagnostic(
                                kind="continuity_error",
                                message=f"{rule.name} 需递增或重启为1：{prev} → {curr}",
                                position=(n.token.start, n.token.end),
                                extra={"rule": rid, "prev": prev, "curr": curr},
                            ))
                        prev = curr

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


