"""OCR 字符混淆检测。

原则：**检测，但不擅自修复金额类字段**。
原因：`4O.OO` 可能该是 `40.00` 也可能该是 `4.00`，修复方向有歧义；
财务错账成本远高于人工复核成本。只有「候选集封闭」的字段（如币种）才允许
在能通过白名单验证的前提下纠正——那是验证，不是猜测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..normalize.currency import ISO4217

LETTER_TO_DIGIT = {
    "O": "0", "o": "0", "D": "0",
    "I": "1", "l": "1", "i": "1", "|": "1",
    "S": "5", "s": "5",
    "B": "8",
    "G": "6",
    "Z": "2", "z": "2",
    "T": "7",
}

DIGIT_TO_LETTER = {v: k for k, v in LETTER_TO_DIGIT.items()}

AMBIGUOUS_CHARS = set("OoDIl|SsBGZzT")


@dataclass(frozen=True)
class Finding:
    field: str
    value: str
    index: int
    char: str
    candidates: tuple[str, ...]
    snippet: str

    def as_detail(self) -> dict[str, object]:
        return {
            "field": self.field,
            "char": self.char,
            "index": self.index,
            "candidates": list(self.candidates),
            "snippet": self.snippet,
        }


def detect_confusion(value: object, *, field: str) -> list[Finding]:
    """扫描「字母紧邻数字」的可疑字符。字母与数字混排是正常单号形态，
    只有歧义字符夹在数字中间才算混淆。"""
    if value is None:
        return []
    text = str(value)
    if not text:
        return []

    findings: list[Finding] = []
    for index, char in enumerate(text):
        if char not in AMBIGUOUS_CHARS:
            continue
        prev_char = text[index - 1] if index > 0 else ""
        next_char = text[index + 1] if index + 1 < len(text) else ""
        digit_neighbor = any(ch.isdigit() for ch in (prev_char, next_char) if ch)
        if not digit_neighbor:
            continue
        # 纯字母上下文里的 I/O 属于正常单词，不判
        if not (prev_char.isdigit() or next_char.isdigit()):
            continue
        replacement = LETTER_TO_DIGIT.get(char)
        if replacement is None:
            continue
        findings.append(
            Finding(
                field=field,
                value=text,
                index=index,
                char=char,
                candidates=(replacement,),
                snippet=_snippet(text, index),
            )
        )
    return findings


def _snippet(text: str, index: int, radius: int = 6) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius + 1)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


_CURRENCY_SHAPE = re.compile(r"^[A-Za-z|0-9]{3}$")


def repair_currency(candidate: object) -> str | None:
    """币种候选集封闭（ISO 4217），允许「枚举替换 + 白名单验证」。

    `USO` → 尝试 O→0 得 `US0`（非法），无解 → 返回 None，不猜。
    `USD` → 直接命中。
    """
    if candidate is None:
        return None
    text = str(candidate).strip().upper()
    if not text:
        return None
    if text in ISO4217:
        return text
    if not _CURRENCY_SHAPE.match(text):
        return None

    options: list[str] = [text]
    for index, char in enumerate(text):
        replacement = LETTER_TO_DIGIT.get(char)
        if replacement:
            options.append(text[:index] + replacement + text[index + 1 :])
        for letter in DIGIT_TO_LETTER.get(char, ()):
            options.append(text[:index] + letter + text[index + 1 :])
    hits = {option for option in options if option in ISO4217}
    # 多个不同解时不能挑一个（US0 可能变 USD，也可能变 USO）→ 交人工
    return hits.pop() if len(hits) == 1 else None
