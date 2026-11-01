"""AR/AP 判定：资金流向在记账主体视角下的符号，不是单据类型的直接映射。"""

from __future__ import annotations

import difflib
import re
import unicodedata

from ..schema import BillEntry

CN_TYPES = {"Credit Note"}
# 正向计费单据：Arrival Notice（到货通知）虽非 DN/CN，但同样是「签发方向接收方开费用」
DN_TYPES = {"Debit Note", "Invoice", "Arrival Notice"}


def _norm(name: str | None) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name)).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def party_matches(entity: str | None, party: str | None) -> bool:
    """主体与交易方是否同一方（容忍公司后缀、缩写差异）。"""
    left, right = _norm(entity), _norm(party)
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 4 and len(right) >= 4 and (left in right or right in left):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.86


def decide_posting_type(
    *,
    document_type: str | None,
    issuer: str | None,
    recipient: str | None,
    entity: str | None,
    credit_policy: str = "mirror",
) -> tuple[str | None, str]:
    """返回 (AR|AP|None, 判定理由)。"""
    if not entity:
        return None, "未提供记账主体，无法判断应收应付方向"
    if not issuer and not recipient:
        return None, "单据未识别到签发方与接收方，无法判断交易方向"

    if party_matches(entity, issuer):
        direction, direction_text = 1, f"记账主体「{entity}」是本单签发方，属于对外收费"
    elif party_matches(entity, recipient):
        direction, direction_text = -1, f"记账主体「{entity}」是本单接收方，属于被收费"
    else:
        return None, (
            f"记账主体「{entity}」既非签发方「{issuer or '未识别'}」"
            f"也非接收方「{recipient or '未识别'}」，不在本单交易链上，不可判方向"
            "（若该主体是代理/代付方，请用 --entity-alias 补充它在单据上的写法，或人工指定收付方向）"
        )

    normalized_type = document_type or "Unknown"

    if normalized_type in DN_TYPES:
        posting = "AR" if direction == 1 else "AP"
        kind_text = "Debit Note/Invoice 正向计费"
    elif normalized_type in CN_TYPES:
        if credit_policy == "same_side":
            posting = "AR" if direction == 1 else "AP"
            kind_text = "Credit Note 按同侧红字口径"
        else:
            posting = "AP" if direction == 1 else "AR"
            kind_text = "Credit Note 按对侧科目（mirror）口径冲销"
    else:
        return None, f"单据类型未知（{normalized_type}），不足以判定应收应付"

    return posting, f"{direction_text}，{kind_text}，判定为 {posting}"


def consistency_check(entry: BillEntry) -> str | None:
    """方向与金额符号的自检，返回提示文案（无问题返回 None）。"""
    if entry.posting_type is None or not isinstance(entry.amount, (int, float)):
        return None
    if entry.amount < 0 and entry.document_type != "Credit Note":
        return f"金额为负（{entry.amount}）但单据类型为 {entry.document_type}，与 {entry.posting_type} 方向需人工确认"
    return None
