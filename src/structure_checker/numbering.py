import re
from typing import Optional


_CN_DIGITS = {
    "零": 0, "〇": 0, "○": 0, "O": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "万": 10000, "亿": 100000000}


def chinese_numeral_to_int(text: str) -> Optional[int]:
    if not text:
        return None
    # 纯阿拉伯数字直接返回
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None

    total = 0
    section = 0
    number = 0

    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                # 万/亿：将当前累积的 section + number 作为整体乘以单位。
                # 原实现 `(section + (number if number else 1))` 在 "十亿" 时
                # number=0（十已被消费进 section）却兜底成 1，导致 10亿+1亿=11亿。
                # 正确语义：数字已消费则补 0，仅在完全没有前置数字时（如 "万一"）补 1。
                section += number
                if section == 0:
                    section = 1
                total += section * unit
                section = 0
                number = 0
            else:
                # 十/百/千
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            # 非法字符，放弃解析
            return None

    return total + section + number


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

# 合法罗马数字的严格判定（标准减法规则正则）。
#   I/X/C/M 各自最多连续 3 次；V/L/D 不重复；减法对仅限 IX/IV/XL/XC/CD/CM，
#   且不能出现「小值在大值之后又做减法」（IIX、VX、IC 等）。
# 用途：把真正的非法序列（IIX、VX、IIII）与合法罗马章节号（X=10、XII=12、
# MMXXIV=2024）区分开——非法序列不解析为真实章节编号。
_ROMAN_RE = re.compile(
    r"^M{0,3}(CM|CD|D?C{0,3})"
    r"(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def is_valid_roman(text: str) -> bool:
    """text 是否为合法罗马数字（标准减法规则）。"""
    if not text:
        return False
    text = text.upper()
    return bool(_ROMAN_RE.fullmatch(text))


def roman_to_int(text: str) -> Optional[int]:
    if not text:
        return None
    text = text.upper()
    if not is_valid_roman(text):
        return None  # 非法序列（IIX、VX、IIII）不算真实罗马
    total = 0
    prev = 0
    for ch in reversed(text):
        val = _ROMAN_VALUES.get(ch, 0)
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total if total > 0 else None


def normalize_number(groups: dict, order: list) -> Optional[int]:
    for tag in order:
        if tag == "arabic":
            val = groups.get("num_ar") or groups.get("arabic")
            if val and val.isdigit():
                return int(val)
        elif tag == "cn":
            val = groups.get("num_cn") or groups.get("cn")
            n = chinese_numeral_to_int(val or "")
            if n is not None:
                return n
        elif tag == "roman":
            val = groups.get("num_rm") or groups.get("roman")
            n = roman_to_int(val or "")
            if n is not None:
                return n
    return None


