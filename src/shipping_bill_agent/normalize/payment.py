"""付款方式归一：把单据上的各种写法统一成 PP / CC。

真实样本（data/real/debit_note_DC-108478.pdf）费用区块的表头是：

    DESCRIPTION  UNIT  RATE  QTY  P/C  DEBIT(+)  CREDIT(-)

明细行 P/C 列只写一个字母 `C`。这是国际货代约定：P = Prepaid（预付），
C = Collect（到付），落到系统契约里是 PP / CC。之前的规则只认字面
`PP`/`CC`，于是 `C` 被当成无效值丢掉，付款方式整列变 null。

这里只做「写法 → 枚举」的确定性归一，不猜：既认不出 Prepaid 也认不出
Collect 的文本一律返回 None，让下游走人工复核。
"""

from __future__ import annotations

import re

PREPAID = "PP"
COLLECT = "CC"

# 去掉空白/分隔符后再比对，避免 "P / C"、"pre-paid" 这类写法漏判
_SEP_RE = re.compile(r"[\s._\-:/\\|]+")
# 表头本身不是取值（"P/C" 归一后是 "PC"），显式排除
_HEADER_TOKENS = {"PC", "PPCC", "PPCCX"}

_PREPAID_TOKENS = {
    "PP",
    "P",
    "PREPAID",
    "PREPAY",
    "PREPAYMENT",
    "PREPAIDFREIGHT",
    "FREIGHTPREPAID",
    "ADVANCE",
}
_COLLECT_TOKENS = {
    "CC",
    "C",
    "COLLECT",
    "COLLECTION",
    "COLLECTFREIGHT",
    "FREIGHTCOLLECT",
    "PAYCOLLECT",
}
_CN_PREPAID = ("预付", "先付", "预付运费")
_CN_COLLECT = ("到付", "收货人付", "对方付", "运费到付")


def normalize_payment_term(raw: object) -> str | None:
    """把任意写法归一为 "PP" / "CC"；无法判断返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    for word in _CN_PREPAID:
        if word in text:
            return PREPAID
    for word in _CN_COLLECT:
        if word in text:
            return COLLECT

    token = _SEP_RE.sub("", text).upper()
    if not token or token in _HEADER_TOKENS:
        return None
    if token in _PREPAID_TOKENS:
        return PREPAID
    if token in _COLLECT_TOKENS:
        return COLLECT

    # 长句里的整词（如 "ALL CHARGES PREPAID" / "FREIGHT COLLECT"）
    if re.search(r"\bPRE-?PAID\b", text.upper()):
        return PREPAID
    if re.search(r"\bCOLLECT\b", text.upper()):
        return COLLECT
    return None
