"""五道校验闸：金额 / 币种 / 勾稽 / OCR 混淆 / 重复。

原则：校验层只产出 Issue（带 severity），不决定流转；流转由 decide/planner 消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..normalize.currency import is_iso4217
from ..schema import BillEntry, Issue
from .ocr_repair import detect_confusion

TOLERANCE = 0.01
AMOUNT_FIELDS = ("amount", "unit_price", "tax", "subtotal", "total")


@dataclass
class DocTotals:
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    currency: str | None = None
    # 单据按币种分别给出的总额，例如 TOTAL USD 100.00 / TOTAL EUR 50.00
    total_by_currency: dict[str, float] = field(default_factory=dict)
    # 那个 total 属于哪个币种（从总额行文本里读出来）
    total_currency: str | None = None
    extras: dict[str, object] = field(default_factory=dict)


def _span_of(entry: BillEntry, field_name: str) -> str | None:
    prov = entry.provenance.get(field_name)
    return prov.span if prov else None


def _check_amount(entry: BillEntry) -> list[Issue]:
    issues: list[Issue] = []
    amount = entry.amount
    if amount is None:
        issues.append(
            Issue(
                code="V-AMOUNT_INVALID",
                severity="blocker",
                message="未能从输入材料中提取到有效金额",
                field="amount",
            )
        )
        return issues

    if not isinstance(amount, (int, float)):
        issues.append(
            Issue(
                code="V-AMOUNT_INVALID",
                severity="blocker",
                message=f"金额不是数字类型：{amount!r}",
                field="amount",
            )
        )
        return issues

    if amount < 0 and entry.document_type != "Credit Note":
        issues.append(
            Issue(
                code="V-AMOUNT_INVALID",
                severity="blocker",
                message=f"非 Credit Note 单据出现负金额 {amount}",
                field="amount",
            )
        )

    if abs(amount) >= 1e12:
        issues.append(
            Issue(
                code="V-AMOUNT_INVALID",
                severity="warn",
                message=f"金额数量级异常：{amount}",
                field="amount",
            )
        )
    return issues


def _check_currency(entry: BillEntry) -> list[Issue]:
    if entry.currency is None:
        return [
            Issue(
                code="V-CURRENCY_INVALID",
                severity="warn",
                message="未识别到币种，按题目要求置 null（不臆测）",
                field="currency",
            )
        ]
    if not is_iso4217(entry.currency):
        return [
            Issue(
                code="V-CURRENCY_INVALID",
                severity="warn",
                message=f"币种 {entry.currency} 不在 ISO 4217 白名单内",
                field="currency",
            )
        ]
    return []


def _check_ocr_confusion(entry: BillEntry) -> list[Issue]:
    issues: list[Issue] = []
    for field_name in ("amount", "reference_no", "container_no", "currency"):
        span = _span_of(entry, field_name)
        if not span:
            continue
        findings = detect_confusion(span, field=field_name)
        if not findings:
            continue
        # 金额/单号/箱号类混淆必须人工确认：不做自动修复
        needs_review = field_name in {"amount", "reference_no", "container_no"}
        issues.append(
            Issue(
                code="V-OCR_CONFUSION",
                severity="warn",
                message=(
                    f"{field_name} 字段存在 OCR 字符混淆"
                    f"（{'、'.join(sorted({f.char for f in findings}))}），"
                    "系统不自动修复，需人工核对原文"
                ),
                field=field_name,
                detail={
                    "findings": [f.as_detail() for f in findings],
                    "review_required": needs_review,
                },
            )
        )
    return issues


def _document_issues(entries: list[BillEntry], totals: DocTotals) -> list[Issue]:
    """G3：明细金额 + 税额 与 总计 勾稽。

    多币种单据不做跨币种求和（没有汇率就不能加）；若单据按币种分别给了总额，
    则逐币种比对；只给一个总额且无法确定币种时降级为 info。
    """
    if not entries:
        return []

    grouped = group_amounts(entries)
    currencies = sorted(currency for currency in grouped if currency)
    total = totals.total
    tax = totals.tax or 0.0

    if total is None:
        return [
            Issue(
                code="V-RECONCILIATION",
                severity="info",
                message="单据未提供总计金额，跳过勾稽校验",
                field="total",
            )
        ]

    # 逐币种总额：单据把每个币种的合计都列出来了
    if totals.total_by_currency:
        issues: list[Issue] = []
        for currency, stated in sorted(totals.total_by_currency.items()):
            stated_currency = totals.total_currency or currency
            if stated_currency != currency:
                continue
            actual = grouped.get(currency, 0.0)
            if abs(actual - stated) > TOLERANCE:
                issues.append(
                    Issue(
                        code="V-RECONCILIATION",
                        severity="blocker",
                        message=(
                            f"{currency} 明细合计 {actual:.2f} 与单据该币种总计 {stated:.2f} 不一致"
                            f"（差额 {actual - stated:.2f}）"
                        ),
                        field="total",
                        detail={
                            "currency": currency,
                            "detail_sum": round(actual, 4),
                            "total": stated,
                            "delta": round(actual - stated, 4),
                        },
                    )
                )
        if issues:
            return issues
        return []

    if len(currencies) > 1:
        return [
            Issue(
                code="V-MULTI_CURRENCY",
                severity="info",
                message=(
                    f"单据含多币种费用（{'、'.join(currencies)}），未做汇率换算，"
                    f"跳过总额勾稽；各币种明细合计："
                    + "；".join(f"{c} {grouped[c]:.2f}" for c in currencies)
                ),
                field="currency",
                detail={"totals": {c: round(grouped[c], 4) for c in currencies}},
            )
        ]

    detail_sum = grouped.get(currencies[0], 0.0) if currencies else sum(
        entry.amount for entry in entries if isinstance(entry.amount, (int, float))
    )
    expected = detail_sum + tax

    if abs(expected - float(total)) <= TOLERANCE:
        return []

    with_tax_ok = abs(detail_sum - float(total)) <= TOLERANCE
    if with_tax_ok:
        # 明细已等于总计，说明 tax 可能已含在 total 里
        return [
            Issue(
                code="V-RECONCILIATION",
                severity="info",
                message=(
                    f"明细合计 {detail_sum:.2f} 已等于总计 {float(total):.2f}，"
                    f"判断税额 {tax:.2f} 已包含在总计中"
                ),
                field="total",
            )
        ]

    return [
        Issue(
            code="V-RECONCILIATION",
            severity="blocker",
            message=(
                f"明细金额合计 {detail_sum:.2f} + 税额 {tax:.2f} = {expected:.2f}，"
                f"与单据总计 {float(total):.2f} 不一致（差额 {expected - float(total):.2f}）"
            ),
            field="total",
            detail={
                "detail_sum": round(detail_sum, 4),
                "tax": round(tax, 4),
                "total": float(total),
                "delta": round(expected - float(total), 4),
            },
        )
    ]


def group_amounts(entries: list[BillEntry]) -> dict[str, float]:
    """按币种汇总明细金额；币种缺失归到空串键。"""
    grouped: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry.amount, (int, float)):
            continue
        key = (entry.currency or "").strip().upper()
        grouped[key] = grouped.get(key, 0.0) + float(entry.amount)
    return grouped


def _duplicate_lines(entries: list[BillEntry]) -> list[Issue]:
    """G5-①：同单同费用同金额重复提取。

    业务上一张单可能对同一费用收多次（两个箱子各收一次 THC），所以**不丢弃**，
    只在被重复的行上标 V-DUP_LINE，让人判断是合法多行还是重复录入。
    """
    seen: dict[tuple, int] = {}
    for index, entry in enumerate(entries):
        key = (
            (entry.charge_item or "").lower().strip(),
            round(float(entry.amount), 4) if isinstance(entry.amount, (int, float)) else None,
            entry.currency,
        )
        if key in seen:
            entry.issues.append(
                Issue(
                    code="V-DUP_LINE",
                    severity="warn",
                    message=(
                        f"同一单据内出现相同费用行：{entry.charge_item} {entry.amount} "
                        f"{entry.currency or ''}（与第 {seen[key] + 1} 条相同）。"
                        "若是分批/分箱分别计费属正常，请人工确认是否重复录入"
                    ),
                    field="charge_item",
                    detail={"duplicate_of": seen[key], "kept": True},
                )
            )
        else:
            seen[key] = index
    return []


def validate_entries(
    entries: list[BillEntry],
    *,
    totals: DocTotals,
    settings: Settings,
) -> None:
    """就地写入 issues。不丢弃任何记录：重复行保留并标疑（V-DUP_LINE）。"""
    if not entries:
        return

    for entry in entries:
        entry.issues.extend(_check_amount(entry))
        entry.issues.extend(_check_currency(entry))
        entry.issues.extend(_check_ocr_confusion(entry))

    doc_issues = _document_issues(entries, totals)
    for entry in entries:
        entry.issues.extend(doc_issues)

    # 重复行标记放在最后：先让每条记录带上单据级问题，再标重复
    _duplicate_lines(entries)


def needs_review(entry: BillEntry) -> bool:
    """金额类 OCR 混淆虽为 warn，仍要求人工确认（财务安全优先）。"""
    for issue in entry.issues:
        if issue.code == "V-OCR_CONFUSION" and issue.field == "amount":
            return True
        if issue.code == "V-OCR_CONFUSION" and issue.detail.get("review_required"):
            return True
    return False
