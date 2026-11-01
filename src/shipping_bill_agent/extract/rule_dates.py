"""日期与单号的正则小工具（供规则抽取器与邮件上下文补缺共用）。"""

from __future__ import annotations

import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

DATE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", "ymd"),
    (r"\b(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ](20\d{2})\b", "dmy_word"),
    (r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", "ymd_cn"),
    (r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b", "mdy_word"),
    # 美式纯数字 10-24-2026 / 10/24/2026（货代单据极常见）
    (r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", "mdy_num"),
]

REF_IN_SUBJECT = re.compile(
    r"\b((?:DN|CN|INV|D\/N|C\/N)[-/]?\d{4,}[-/]?\d{1,6})\b", re.I
)


def normalize_date_match(match: re.Match[str], kind: str) -> str | None:
    try:
        if kind in {"ymd", "ymd_cn"}:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif kind == "dmy_word":
            day = int(match.group(1))
            month = MONTHS.get(match.group(2)[:3].lower())
            year = int(match.group(3))
            if month is None:
                return None
        elif kind == "mdy_word":
            month = MONTHS.get(match.group(1)[:3].lower())
            day = int(match.group(2))
            year = int(match.group(3))
            if month is None:
                return None
        else:  # mdy_num：首段 >12 时按 DMY 处理，其余按美式 MDY
            first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if first > 12 and second <= 12:
                month, day = second, first
            else:
                month, day = first, second
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 2000 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_date_string(text: str | None) -> str | None:
    """从任意字符串里抽第一个合法日期，归一为 YYYY-MM-DD。"""
    if not text:
        return None
    for pattern, kind in DATE_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            normalized = normalize_date_match(match, kind)
            if normalized:
                return normalized
    return None


def extract_ref_from_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    match = REF_IN_SUBJECT.search(subject)
    if match:
        return match.group(1).upper()
    return None
