from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Rule:
    id: str
    name: str
    level: int
    enabled: bool
    optional: bool
    priority: int
    pattern: str
    numbering_normalize: List[str]
    numbering_continuity: str  # "strict_increase" | "increase_or_restart" | "none"
    children: List[str]


@dataclass
class Token:
    start: int
    end: int
    rule_id: str
    level: int
    priority: int
    raw_text: str
    number_value: Optional[int] = None
    # markdown ATX 标题层级（`#` 数量）：setext === 为 1，普通文本行标题为 None。
    # 有值时不替代 rule level 做"类型识别"，但在建树/层级校验时优先用它，
    # 使「## 1. 概述」这类 H2 编号小节按作者意图作为"节"层级的二级标题，
    # 不再因 rule 是"目"(level3) 而误报 hierarchy_gap。
    heading_level: Optional[int] = None
    # 标题行唯一标识：同一行内多个 token（如合并标题「第1章 第1节」的章+节）
    # 共享同一 line_id，建树时视为"一个标题的多层编号"，按规则层级嵌套，
    # 而不是把后出现的节当作独立根节点。
    line_id: Optional[int] = None


@dataclass
class Node:
    token: Token
    children: List["Node"] = field(default_factory=list)
    parent: Optional["Node"] = None
    # 该 token 所在标题行的最大规则层级（用于判断"行含更深层编号"，
    # 如合并标题「第1章 第1节」的章行含节 → 跨行同号时视同章多节展开）
    line_max_level: int = 0

    @property
    def level(self) -> int:
        return self.token.level

    @property
    def rule_id(self) -> str:
        return self.token.rule_id


@dataclass
class Diagnostic:
    kind: str  # e.g., "overlap_discard", "hierarchy_gap", "continuity_error", "level_mismatch"
    message: str
    position: Tuple[int, int]
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    root_nodes: List[Node]
    diagnostics: List[Diagnostic]



