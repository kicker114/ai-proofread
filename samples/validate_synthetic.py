#!/usr/bin/env python3
"""审校合成稿确定性阶段回归校验（无需 API，秒级）。

用法：python3 samples/validate_synthetic.py
断言：
  - TGSCC：关键繁体字全部命中
  - 异形词：按纳/案语/笔划/录象/份量 全部检出
  - 结构：hierarchy_gap + 章 2→4 continuity_error 恰好各 1 条
任一缺失 → 打印差异并以退出码 1 结束。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SAMPLES = _ROOT / "samples"
_MANUSCRIPT = _SAMPLES / "审校合成稿.md"

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.max_pipeline import (  # noqa: E402
    phase0_structure,
    phase0_tgscc,
    phase0_variants,
)

# 关键繁体字（从中抽取代表性子集；全文共 64 条命中）
REQUIRED_TGSCC_CHARS = {
    "國", "開", "據", "網", "萬", "億", "產", "術", "獻", "為",
    "類", "環", "種", "認", "親", "進", "壟", "後", "續", "們",
    "評", "與", "線", "終", "則", "謹", "態", "資", "斷",
}

REQUIRED_VARIANTS = {"按纳", "案语", "笔划", "录象", "份量"}


def main() -> int:
    if not _MANUSCRIPT.is_file():
        print(f"❌ 找不到合成稿: {_MANUSCRIPT}")
        return 1
    text = _MANUSCRIPT.read_text(encoding="utf-8")

    problems: list[str] = []

    tgscc = phase0_tgscc(text)
    got_chars = {r["char"] for r in tgscc}
    missing_tgscc = REQUIRED_TGSCC_CHARS - got_chars
    if missing_tgscc:
        problems.append(f"TGSCC 缺 {len(missing_tgscc)} 字: "
                        + " ".join(sorted(missing_tgscc)))
    if len(tgscc) < 50:
        problems.append(f"TGSCC 仅 {len(tgscc)} 条（期望 ≥50）")
    print(f"  TGSCC: {len(tgscc)} 条"
          + ("" if not missing_tgscc else " ⚠️ 缺字"))

    variants = phase0_variants(text)
    got_variants = {v["original"] for v in variants}
    missing_variants = REQUIRED_VARIANTS - got_variants
    if missing_variants:
        problems.append("异形词缺: " + "、".join(sorted(missing_variants)))
    if len(variants) != len(REQUIRED_VARIANTS):
        problems.append(f"异形词 {len(variants)} 条（期望 {len(REQUIRED_VARIANTS)}）")
    print(f"  异形词: {len(variants)} 条"
          + ("" if not missing_variants else " ⚠️ 缺词"))

    struct = phase0_structure(text)
    types = [r["type"] for r in struct]
    has_gap = types.count("hierarchy_gap") == 1
    has_continuity = any(
        r["type"] == "continuity_error" and "2 → 4" in (r.get("message") or "")
        for r in struct
    )
    if not has_gap:
        problems.append(
            f"hierarchy_gap 应为 1 条，实际 {types.count('hierarchy_gap')}")
    if not has_continuity:
        problems.append("缺少章 2→4 continuity_error（或消息不含 '2 → 4'）")
    if len(struct) != 2:
        problems.append(f"结构诊断 {len(struct)} 条（期望恰好 2 条设计内错误）")
    print(f"  结构: {len(struct)} 条"
          + ("" if has_gap and has_continuity else " ⚠️ 与期望不符"))

    if problems:
        print("\n❌ 确定性阶段回归失败：")
        for p in problems:
            print("  -", p)
        return 1

    print("\n✅ 确定性阶段全部命中（TGSCC/异形词/结构 与错误清单一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
