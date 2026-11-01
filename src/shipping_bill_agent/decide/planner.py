"""决策层：操作类型 + 字段级 patch + AR/AP + 审计文案。

先拦后判：blocker / 歧义匹配 / 关键字段低置信 → PENDING_REVIEW。
更新语义：只有「有证据」的字段参与 diff；无证据一律继承原值。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schema import FIELD_LABELS, BillEntry, Issue, Provenance
from ..validate.validators import needs_review
from .ar_ap import consistency_check, decide_posting_type

# patch 的应用顺序（先费用属性，后单据属性）
FIELD_ORDER: tuple[str, ...] = (
    "amount",
    "currency",
    "payment_term",
    "charge_item",
    "quantity",
    "unit_price",
    "tax",
    "document_date",
)

# 单据级字段：命中历史账单时不得被新单据静默覆盖（交易主体变了要人来定）
DOC_FIELDS: tuple[str, ...] = (
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "bl_no",
    "booking_no",
    "container_no",
)

CONFIDENCE_FLOOR = {
    "amount": "confidence_amount",
    "currency": "confidence_currency",
    "charge_item": "confidence_name",
}

# 「原值空 → 新值有」只有在这些字段上才算账务意义上的变更；
# 数量/单价/税额属于明细补全，不改变已入账金额，只写进审计说明
MATERIAL_FILL_FIELDS = {"amount", "currency", "payment_term", "charge_item", "document_date"}


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.005
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return str(left).strip().lower() == str(right).strip().lower()


def _threshold(field_name: str, settings: Settings) -> float:
    attr = CONFIDENCE_FLOOR.get(field_name)
    if attr:
        return float(getattr(settings.pipeline, attr))
    return 0.55


def _diff_and_patch(
    entry: BillEntry, original: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """返回 (明确变更字段, 继承字段, 补全字段)。

    三种「空」必须区分开：
    - 无证据（输入材料没提到）→ 继承原值
    - 原值为 None 且新值有证据 → 属于「补全」，不算变更（不覆盖语义）
    - 两边都有值且不同 → 明确变更 → patch
    """
    changed: dict[str, Any] = {}
    inherited: list[str] = []
    filled: list[str] = []
    for field_name in FIELD_ORDER:
        if field_name not in original:
            continue
        prov = entry.provenance.get(field_name)
        new_value = getattr(entry, field_name, None)
        old_value = original.get(field_name)
        if prov is None:
            # 输入材料未提及该字段 → 继承原值
            setattr(entry, field_name, old_value)
            if old_value is not None:
                inherited.append(field_name)
            continue
        if old_value is None and new_value is not None:
            filled.append(field_name)
            continue
        if not _equal(new_value, old_value):
            changed[field_name] = new_value
    return changed, inherited, filled


def _diff_doc_fields(
    entry: BillEntry, original: dict[str, Any]
) -> tuple[list[str], list[Issue]]:
    """单据级字段对齐历史账单。

    规则：无证据 → 继承原值；原值为空 → 用新值补全；两边都有值且不同 →
    **保留原值**，只记 P-DOC_FIELD_DIFF 供人判断（交易主体/单号变更不能自动生效）。
    """
    inherited: list[str] = []
    issues: list[Issue] = []
    for field_name in DOC_FIELDS:
        if field_name not in original:
            continue
        old_value = original.get(field_name)
        prov = entry.provenance.get(field_name)
        new_value = getattr(entry, field_name, None)
        if prov is None or new_value in (None, ""):
            setattr(entry, field_name, old_value)
            if old_value not in (None, ""):
                inherited.append(field_name)
            continue
        if old_value in (None, ""):
            continue  # 补全：原账单没值，用新值
        if not _equal(new_value, old_value):
            issues.append(
                Issue(
                    code="P-DOC_FIELD_DIFF",
                    severity="warn",
                    message=(
                        f"单据级字段「{FIELD_LABELS.get(field_name, field_name)}」与历史账单不一致："
                        f"输入材料为「{new_value}」，账单为「{old_value}」，已保留原值，请人工确认是否变更"
                    ),
                    field=field_name,
                    detail={"input_value": new_value, "bill_value": old_value},
                )
            )
            setattr(entry, field_name, old_value)
    return inherited, issues


def plan(
    entry: BillEntry,
    *,
    original: dict[str, Any] | None,
    settings: Settings,
) -> BillEntry:
    entry.accounting_entity = entry.accounting_entity or settings.pipeline.accounting_entity

    # ---- 单据级字段先对齐历史账单：AR/AP 判定必须基于最终输出的交易主体
    inherited_doc: list[str] = []
    if original:
        inherited_doc, doc_issues = _diff_doc_fields(entry, original)
        entry.issues.extend(doc_issues)

    # ---- AR/AP
    posting, arap_reason = decide_posting_type(
        document_type=entry.document_type,
        issuer=entry.issuer,
        recipient=entry.recipient,
        entity=entry.accounting_entity,
        credit_policy=settings.pipeline.credit_note_policy,
    )
    entry.posting_type = posting

    # 交易方是「推断」出来的（单据没写接收方，只在本单某行出现过主体名）→ 不能据此自动定 AR/AP
    inferred_party = [
        name
        for name in ("issuer", "recipient")
        if (prov := entry.provenance.get(name)) and prov.extractor.startswith("infer")
    ]
    if inferred_party and posting is not None:
        entry.issues.append(
            Issue(
                code="F-PARTY_INFERRED",
                severity="blocker",
                message=(
                    f"{FIELD_LABELS.get(inferred_party[0], inferred_party[0])}"
                    f"「{getattr(entry, inferred_party[0], '')}」是推断值"
                    f"（依据：{(entry.provenance.get(inferred_party[0]).span or '')[:60]}），"
                    "单据未声明该方身份，不能据此自动判定 AR/AP，转人工确认"
                ),
                field=inferred_party[0],
            )
        )

    if posting is None:
        entry.issues.append(
            Issue(code="F-ARAP_UNKNOWN", severity="blocker", message=arap_reason, field="posting_type")
        )
    elif "Credit Note" in (entry.document_type or ""):
        entry.issues.append(
            Issue(
                code="F-CREDIT_POLICY",
                severity="info",
                message=f"Credit Note 按 {settings.pipeline.credit_note_policy} 口径落账",
                field="posting_type",
            )
        )

    contradiction = consistency_check(entry)
    if contradiction:
        entry.issues.append(
            Issue(code="V-AMOUNT_INVALID", severity="warn", message=contradiction, field="amount")
        )

    # ---- 操作类型
    match = entry.match
    filled: list[str] = []
    if match is None or match.bill_id is None:
        entry.intended_operation = "CREATE"
        entry.operation = "CREATE"
        change_note = "未匹配到已有账单，按新增处理"
        changed: dict[str, Any] = {}
        inherited: list[str] = inherited_doc
    else:
        changed, inherited, filled = _diff_and_patch(entry, original or {})
        inherited = inherited_doc + inherited
        if changed:
            entry.intended_operation = "UPDATE"
            entry.operation = "UPDATE"
            change_note = (
                "匹配到已有账单，输入材料明确变更的字段："
                + "、".join(FIELD_LABELS.get(k, k) for k in changed)
            )
        elif [f for f in filled if f in MATERIAL_FILL_FIELDS]:
            # 原账单该字段为空、输入材料有值：这是补全，不能报 NO_CHANGE（否则永远补不上）
            material = [f for f in filled if f in MATERIAL_FILL_FIELDS]
            entry.intended_operation = "UPDATE"
            entry.operation = "UPDATE"
            change_note = (
                "匹配到已有账单；原账单以下字段为空，按输入材料补全："
                + "、".join(FIELD_LABELS.get(f, f) for f in material)
            )
            entry.issues.append(
                Issue(
                    code="P-FILL_ONLY",
                    severity="info",
                    message="本次仅为空字段补全，未修改任何已有值",
                    field=material[0],
                )
            )
        else:
            entry.intended_operation = "NO_CHANGE"
            entry.operation = "NO_CHANGE"
            change_note = "匹配到已有账单，输入材料未提出任何变更"
        entry.inherited_fields = inherited

    # ---- 低置信度拦截（只针对发生变更的字段）
    low_confidence_fields = [
        field_name
        for field_name in changed
        if entry.conf(field_name) < _threshold(field_name, settings)
    ]
    if low_confidence_fields:
        entry.issues.append(
            Issue(
                code="P-LOW_CONFIDENCE",
                severity="warn",
                message=(
                    "以下变更字段置信度低于阈值，需人工确认："
                    + "、".join(
                        f"{FIELD_LABELS.get(f, f)}({entry.conf(f):.2f})"
                        for f in low_confidence_fields
                    )
                ),
                field=low_confidence_fields[0],
                detail={"fields": low_confidence_fields},
            )
        )

    # ---- 拦截优先级：blocker > 匹配歧义 > 金额混淆 > 低置信
    review_reasons: list[str] = []
    if entry.has_blocker():
        review_reasons.append(
            "存在阻断级校验问题：" + "、".join(i.code for i in entry.issues if i.severity == "blocker")
        )
    if match is not None and match.ambiguous:
        entry.issues.append(
            Issue(
                code="M-AMBIGUOUS",
                severity="blocker",
                message=f"匹配到 {len(match.candidates)} 条候选账单，无法唯一确定更新对象，转人工确认",
                field="reference_no",
                detail={"candidates": match.candidates, "tier": match.tier},
            )
        )
        review_reasons.append("账单匹配多命中")
    if needs_review(entry):
        review_reasons.append("金额字段存在 OCR 混淆，禁止自动入账")
    if low_confidence_fields:
        review_reasons.append("变更字段置信度不足")

    if review_reasons:
        entry.operation = "PENDING_REVIEW"
    elif match is not None and match.tier is None:
        entry.issues.append(
            Issue(
                code="M-NO_MATCH",
                severity="info",
                message="四级匹配均未命中已有账单",
                detail={},
            )
        )
    elif match is not None and match.bill_id:
        entry.issues.append(
            Issue(
                code="M-MATCHED",
                severity="info",
                message=f"按 {match.tier} 命中账单 {match.bill_id}",
                detail={"tier": match.tier, "bill_id": match.bill_id},
            )
        )

    entry.ai_reasoning = _build_reasoning(
        entry, changed=changed, inherited=inherited, filled=filled,
        change_note=change_note, arap_reason=arap_reason,
        low_confidence_fields=low_confidence_fields,
    )
    return entry


def _span_text(entry: BillEntry, field_name: str) -> str:
    prov = entry.provenance.get(field_name)
    if prov and prov.span:
        return prov.span.strip()
    return FIELD_LABELS.get(field_name, field_name)


def _fmt_money(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return str(value)


def _build_reasoning(
    entry: BillEntry,
    *,
    changed: dict[str, Any],
    inherited: list[str],
    filled: list[str],
    change_note: str,
    arap_reason: str,
    low_confidence_fields: list[str],
) -> list[str]:
    lines: list[str] = []

    # 1 识别依据
    prov = entry.provenance.get("amount")
    source = prov.source_path.split("/")[-1] if prov and prov.source_path else "输入材料"
    where = f" 第 {prov.page} 页" if prov and prov.page else ""
    lines.append(
        f"从 {source}{where} 识别到费用项「{entry.charge_item_raw or entry.charge_item}」"
        f"金额 {_fmt_money(entry.amount)} {entry.currency or ''}"
        f"，原始文本：{_span_text(entry, 'amount')[:120]}".replace("  ", " ").strip()
    )

    # 2 归一依据
    if entry.charge_item_raw and entry.charge_item and entry.charge_item_raw != entry.charge_item:
        lines.append(
            f"原始费用名称「{entry.charge_item_raw}」按别名表标准化为「{entry.charge_item}」"
        )
    prov_cur = entry.provenance.get("currency")
    if prov_cur and prov_cur.extractor.startswith("normalize") and entry.currency:
        lines.append(f"币种按符号/写法归一为 {entry.currency}（置信度 {prov_cur.confidence:.2f}）")

    # 3 匹配依据
    match = entry.match
    prov_date = entry.provenance.get("document_date")
    if prov_date and "email" in (prov_date.extractor or ""):
        lines.append("单据日期取自邮件发信时间（单据正文未写日期），入账前请核对")
    if match and match.tier and match.bill_id:
        from ..matching.matcher import TIER_LABELS

        lines.append(
            f"通过{TIER_LABELS.get(match.tier, match.tier)}匹配到已有账单 {match.bill_id}"
        )
    elif match and match.ambiguous:
        lines.append(f"按 {match.tier} 命中 {len(match.candidates)} 条候选账单，无法唯一确定，转人工")
    else:
        lines.append("未匹配到已有账单（四级匹配均未命中）")

    # 4 变更依据
    if changed:
        for field_name, new_value in changed.items():
            shown = _fmt_money(new_value) if field_name in {"amount", "quantity", "unit_price", "tax"} else new_value
            lines.append(
                f"输入材料明确要求变更 {FIELD_LABELS.get(field_name, field_name)} → {shown}"
                f"（依据：{_span_text(entry, field_name)[:100]}）"
            )
        lines.append("变更依据均来自输入材料的显式字段，未做推断性修改")
    else:
        lines.append(change_note)

    # 5 未改字段
    if inherited:
        lines.append(
            "以下字段未被输入材料明确修改，保留原账单值："
            + "、".join(FIELD_LABELS.get(f, f) for f in inherited)
        )
    if filled:
        lines.append(
            "以下字段原账单无值、输入材料有值，按补全处理（不视为变更）："
            + "、".join(FIELD_LABELS.get(f, f) for f in filled)
        )

    # 6 应收应付
    lines.append(f"应收应付判定：{arap_reason}")

    # 7 风险
    for issue in entry.issues:
        if issue.code in {
            "V-OCR_CONFUSION",
            "V-CURRENCY_INVALID",
            "V-RECONCILIATION",
            "V-MULTI_CURRENCY",
            "M-AMBIGUOUS",
            "P-DOC_FIELD_DIFF",
            "V-DUP_LINE",
            "F-PARTY_INFERRED",
        }:
            lines.append(f"风险提示：{issue.message}")
        elif issue.code == "P-LOW_CONFIDENCE" and low_confidence_fields:
            continue

    if entry.operation == "PENDING_REVIEW":
        lines.append("综合以上情况，本条记录不自动入账，已转入人工复核（PENDING_REVIEW）")

    return lines
