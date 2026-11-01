"""金额解析：千分位、欧式小数、括号负数、尾负号、币种前后缀。"""

from __future__ import annotations

import re

_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:[,\s]\d{3})+$")
# 数字 run：整数部分允许「1 234」「1,234」两种千分位，小数部分只允许 . 或 ,
# 注意：空格分组必须整组 3 位（"1 234.56" 命中，而 "40.00 05" 只取 "40.00"）
_NUM_RE = re.compile(r"(?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)(?:[.,]\d+)*")

CURRENCY_TOKENS = (
    "USD", "CNY", "RMB", "EUR", "GBP", "HKD", "SGD", "JPY", "AUD", "CAD", "KRW",
    "TWD", "THB", "MYR", "VND", "PHP", "IDR", "INR", "AED", "SAR", "CHF", "NZD",
    "US$", "HK$", "S$", "NT$", "€", "£", "¥", "￥", "$",
    "美金", "美元", "人民币", "元", "港币", "港元",
)


def _strip_currency(text: str) -> str:
    out = text
    for token in sorted(CURRENCY_TOKENS, key=len, reverse=True):
        out = out.replace(token, " ")
    out = re.sub(r"\b(?:DOLLARS?|YUAN|RENMINBI|CENTS?)\b", " ", out, flags=re.I)
    return out


def _to_float(normalized: str) -> float | None:
    try:
        return float(normalized)
    except (TypeError, ValueError):
        return None


def _normalize_separators(number: str) -> tuple[str, bool]:
    """把 1.234,56 / 1,234.56 / 1 234,56 / 1234 统一成 1234.56；返回 (规范串, 是否可靠)。"""
    # 空格/nbsp 千分位只出现在小数分隔符之前，且每组恰好 3 位；不满足说明这是两个数字
    compact = number.replace("\u00a0", " ").strip()
    if " " in compact:
        cut = [i for i in (compact.find(","), compact.find(".")) if i > 0]
        split_at = min(cut) if cut else len(compact)
        int_part, frac = compact[:split_at], compact[split_at:]
        groups = int_part.split(" ")
        if len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]):
            compact = "".join(groups) + frac
        else:
            return int_part.replace(" ", "") + frac, False

    has_comma = "," in compact
    has_dot = "." in compact

    if has_comma and has_dot:
        if compact.rfind(",") > compact.rfind("."):
            # 欧式：1.234,56
            return compact.replace(".", "").replace(",", "."), True
        return compact.replace(",", ""), True

    if has_comma and not has_dot:
        if _THOUSANDS_RE.match(compact):
            return compact.replace(",", ""), True
        # 单逗号 + 后 1~2 位或 4 位 → 视为小数点
        head, _, tail = compact.rpartition(",")
        if head and tail.isdigit() and len(tail) in (1, 2, 4):
            return f"{head}.{tail}", True
        return compact.replace(",", ""), False

    if compact.count(".") > 1:
        # 1.234.567 → 千分位点
        return compact.replace(".", ""), True

    return compact, True


def parse_amount(raw: object) -> tuple[float | None, float]:
    """返回 (金额, 置信度)。无法解析时 (None, 0.0)。不做任何 OCR 字符修复。"""
    if raw is None:
        return None, 0.0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), 1.0

    text = str(raw).strip()
    if not text:
        return None, 0.0

    confidence = 0.9
    negative = False

    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if text.endswith("-") and any(ch.isdigit() for ch in text):
        negative = True
        text = text[:-1].strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()

    stripped = _strip_currency(text)
    stripped = stripped.replace("\u00a0", " ").strip()

    match = _NUM_RE.search(stripped)
    if not match:
        return None, 0.0
    number = match.group(0)

    # 裸整数紧跟另一个数字（"1 23.45"）：既可能是 123.45，也可能是 1 与 23.45 → 不猜
    runs = _NUM_RE.findall(stripped)
    if len(runs) > 1 and not any(sep in runs[0] for sep in ".,") and len(runs[0]) <= 3:
        after_first = stripped[stripped.find(runs[0]) + len(runs[0]) :].lstrip()
        if after_first.startswith(runs[1]):
            return None, 0.0

    # 残留非数字非分隔符 → 置信度下降（可能是 OCR 噪声）
    residue = _NUM_RE.sub("", stripped).strip()
    if residue:
        confidence = 0.55

    normalized, reliable = _normalize_separators(number)
    value = _to_float(normalized)
    if value is None:
        return None, 0.0
    if not reliable:
        confidence = min(confidence, 0.5)
    if negative:
        value = -value
    return value, confidence


def to_number(value: object) -> float | int | None:
    """对外输出前的数值化：整数值转 int 更干净，否则 float。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 4)
    parsed, _ = parse_amount(value)
    if parsed is None:
        return None
    return int(parsed) if float(parsed).is_integer() else round(parsed, 4)


def sum_matches(total: float, parts: list[float], tolerance: float = 0.01) -> bool:
    return abs(sum(parts) - total) <= tolerance
