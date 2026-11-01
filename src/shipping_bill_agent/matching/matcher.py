"""四级账单匹配：优先级短路 + 多命中不猜（转人工）。"""

from __future__ import annotations

from ..config import Settings
from ..schema import BillEntry, MatchInfo
from .store import BillRecord, ExistingBillStore

# 可靠性从高到低：单号+费用名 >> 提单号 >> 订舱号 >> 箱号+金额
TIERS: list[tuple[str, tuple[str, ...]]] = [
    ("T1", ("reference_no", "charge_item")),
    ("T2", ("bl_no", "charge_item", "currency")),
    ("T3", ("booking_no", "charge_item", "currency")),
    ("T4", ("container_no", "issuer", "recipient", "document_date", "amount")),
]

TIER_LABELS = {
    "T1": "单据编号 + 费用名称",
    "T2": "提单号 + 费用名称 + 币种",
    "T3": "订舱号 + 费用名称 + 币种",
    "T4": "箱号 + 交易双方 + 日期 + 金额",
}


def _candidate_dict(record: BillRecord) -> dict[str, object]:
    return {
        "bill_id": record.bill_id,
        "reference_no": record.reference_no,
        "charge_item": record.charge_item,
        "amount": record.amount,
        "currency": record.currency,
        "document_date": record.document_date,
        "issuer": record.issuer,
        "recipient": record.recipient,
    }


def _tie_break(candidates: list[BillRecord], entry: BillEntry) -> tuple[list[BillRecord], float | None]:
    """多命中时用金额接近度做 tie-break；仍多于 1 条则保留全部（交人审）。"""
    if len(candidates) <= 1:
        return candidates, None

    if isinstance(entry.amount, (int, float)):
        ranked = sorted(
            candidates,
            key=lambda r: abs(float(r.amount or 0) - float(entry.amount)),
        )
        best, second = ranked[0], ranked[1] if len(ranked) > 1 else None
        best_delta = abs(float(best.amount or 0) - float(entry.amount))
        second_delta = abs(float(second.amount or 0) - float(entry.amount)) if second else None
        if second_delta is None or second_delta > 0.005:
            return [best], best_delta
        return ranked, (second_delta - best_delta)
    return candidates, None


def match(
    entry: BillEntry, store: ExistingBillStore, *, settings: Settings
) -> MatchInfo:
    if len(store) == 0:
        return MatchInfo(tier=None)

    strict_t4 = settings.pipeline.match_t4_strict

    for tier, keys in TIERS:
        if tier == "T4" and strict_t4:
            if any(getattr(entry, key, None) in (None, "") for key in keys):
                continue
        candidates = store.query(keys, entry)
        if not candidates:
            continue

        if len(candidates) == 1:
            return MatchInfo(
                tier=tier,
                bill_id=candidates[0].bill_id,
                candidates=[_candidate_dict(candidates[0])],
            )

        refined, margin = _tie_break(candidates, entry)
        if len(refined) == 1:
            return MatchInfo(
                tier=tier,
                bill_id=refined[0].bill_id,
                margin=margin,
                candidates=[_candidate_dict(refined[0])],
            )

        return MatchInfo(
            tier=tier,
            bill_id=None,
            margin=margin,
            ambiguous=True,
            candidates=[_candidate_dict(record) for record in refined],
        )

    return MatchInfo(tier=None)
