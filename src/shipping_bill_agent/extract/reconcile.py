"""双轨对账：规则轨 vs LLM 轨，字段级决定裁判与冲突等级。

强字段（单号/日期/金额/币种/总计）信规则——正则命中的是硬证据；
弱字段（签发方/接收方/付款方式/单据类型）信 LLM——语义本是它强项；
不一致本身记成 V-RECONFLICT，把「模型内部犹豫」变成外部可观测信号。
"""

from __future__ import annotations

import re

from ..config import Settings
from ..normalize.amounts import parse_amount
from ..normalize.charge_map import normalize_charge
from ..normalize.currency import normalize_currency
from ..normalize.payment import normalize_payment_term
from ..schema import STRONG_FIELDS, ChargeCandidate, Extraction, FieldVal, Issue


def _compare_values(field_name: str, left: object, right: object) -> bool:
    if field_name in {"amount", "subtotal", "tax", "total"}:
        left_value, _ = parse_amount(left)
        right_value, _ = parse_amount(right)
        if left_value is None or right_value is None:
            return str(left).strip() == str(right).strip()
        return abs(left_value - right_value) <= 0.005
    if field_name == "currency":
        left_code, _ = normalize_currency(left)
        right_code, _ = normalize_currency(right)
        if left_code and right_code:
            return left_code == right_code
    if field_name == "payment_term":
        # 规则读费用行的 P/C 列拿到 "C"，模型可能写 "CC"/"COLLECT"，
        # 归一后再比，避免同义写法被误报成冲突。
        left_term = normalize_payment_term(left)
        right_term = normalize_payment_term(right)
        if left_term and right_term:
            return left_term == right_term
    return str(left).strip().lower() == str(right).strip().lower()


def _charge_name(charge: ChargeCandidate) -> str:
    """标准费用名，用于跨轨合并；归一失败时退回原文名。"""
    canonical, _, _ = normalize_charge(charge.raw_name)
    return (canonical or charge.raw_name or "").strip().lower()


def _charge_key(charge: ChargeCandidate) -> tuple[str, float | None]:
    # 用标准名而不是原文名做并集键：LLM 常把同一行写成
    # 「HANDLING CHARGE」或「SHA pay TAR H/C」等变体，按原文去重会复制费用。
    name = _charge_name(charge)
    amount, _ = parse_amount(charge.amount)
    return name, round(amount, 2) if amount is not None else None


def _span_key(charge: ChargeCandidate) -> str:
    return re.sub(r"\s+", "", (charge.span or "")).lower()


def _same_amount(left: ChargeCandidate, right: ChargeCandidate) -> bool:
    left_value, _ = parse_amount(left.amount)
    right_value, _ = parse_amount(right.amount)
    if left_value is None or right_value is None:
        return False
    return abs(left_value - right_value) <= 0.005


def _same_source_line(left: ChargeCandidate, right: ChargeCandidate) -> bool:
    """两路对同一行给出不同费用名时，用原文证据片段认行。

    真实案例：正文 `SHA pay TAR：H/C $50`，规则轨按别名表归一成
    「Handling Charge」，模型轨照抄成「SHA pay TAR」。名字对不上，
    但两边的 evidence 都是同一行原文 —— 必须按行合并，否则一条费用入两次账。
    """
    left_span, right_span = _span_key(left), _span_key(right)
    if len(left_span) < 8 or len(right_span) < 8:
        return False
    return left_span in right_span or right_span in left_span


def reconcile(
    rule: Extraction, llm: Extraction | None, *, settings: Settings
) -> tuple[Extraction, list[Issue]]:
    if llm is None:
        return rule, []

    issues: list[Issue] = []
    merged = Extraction(scalars={}, charges=[], notes=list(rule.notes), extractor="rule+llm")

    # 固定顺序迭代：set 的顺序受字符串 hash 随机化影响，会让 ai_reasoning 的
    # 数组顺序在不同进程里漂移（同一输入却产不出同一份 result.json）
    for field_name in sorted(set(rule.scalars) | set(llm.scalars)):
        rule_field = rule.scalars.get(field_name)
        llm_field = llm.scalars.get(field_name)
        has_rule = bool(rule_field and rule_field.value not in (None, ""))
        has_llm = bool(llm_field and llm_field.value not in (None, ""))

        if not has_rule and not has_llm:
            # 规则轨可能登记「抽取失败的占位字段」，LLM 轨未返回该键。
            # 两边都没有值时应跳过，不能断言 llm_field 一定存在。
            continue

        if has_rule and has_llm:
            assert rule_field is not None and llm_field is not None
            if _compare_values(field_name, rule_field.value, llm_field.value):
                winner = rule_field.model_copy(update={"confidence": max(rule_field.confidence, 0.95)})
            elif field_name in STRONG_FIELDS:
                winner = rule_field
                issues.append(
                    Issue(
                        code="V-RECONFLICT",
                        severity="blocker",
                        message=(
                            f"{field_name} 规则抽取「{rule_field.value}」与模型抽取"
                            f"「{llm_field.value}」不一致，采用规则值并转人工确认"
                        ),
                        field=field_name,
                        detail={"rule": rule_field.value, "llm": llm_field.value},
                    )
                )
            else:
                winner = llm_field
                issues.append(
                    Issue(
                        code="V-RECONFLICT",
                        severity="warn",
                        message=(
                            f"{field_name} 规则抽取「{rule_field.value}」与模型抽取"
                            f"「{llm_field.value}」不一致，采用模型值（语义字段）"
                        ),
                        field=field_name,
                        detail={"rule": rule_field.value, "llm": llm_field.value},
                    )
                )
        elif has_rule:
            winner = rule_field  # type: ignore[assignment]
        else:
            assert llm_field is not None
            winner = llm_field.model_copy(update={"confidence": llm_field.confidence * 0.8})
        if field_name == "payment_term":
            normalized = normalize_payment_term(winner.value)
            if normalized:
                winner = winner.model_copy(update={"value": normalized})
        merged.scalars[field_name] = winner  # type: ignore[assignment]

    # ---- 费用行：并集，规则优先
    rule_charges = list(rule.charges)
    merged_charges: list[ChargeCandidate] = list(rule_charges)
    seen = {_charge_key(c) for c in rule_charges}

    for charge in llm.charges:
        amount, _ = parse_amount(charge.amount)
        if amount is None:
            # 「FREIGHT PREPAID」这类文本不是带金额的费用行。模型可以提案，
            # 但没有可入账金额的候选不得进入结果。
            issues.append(
                Issue(
                    code="V-LLM_UNPRICED",
                    severity="info",
                    message=(
                        f"模型提出费用「{charge.raw_name}」但没有可解析金额，"
                        "已丢弃该候选"
                    ),
                    field="amount",
                )
            )
            continue
        key = _charge_key(charge)
        if key in seen:
            continue
        # 同一原文行、两路名字不同（H/C → Handling Charge vs SHA pay TAR）
        numbered = next(
            (
                c
                for c in merged_charges
                if _same_amount(c, charge) and _same_source_line(c, charge)
            ),
            None,
        )
        if numbered is not None:
            issues.append(
                Issue(
                    code="V-DUP_LINE",
                    severity="info",
                    message=(
                        f"模型费用「{charge.raw_name}」与「{numbered.raw_name}」出自同一行原文"
                        f"（{charge.span}），按同一行合并，不重复计费"
                    ),
                    field="charge_item",
                )
            )
            continue
        # 同名但金额不同的 LLM 行：记为冲突，仍保留（后续校验会暴露不一致）
        same_name = [c for c in rule_charges if _charge_name(c) == key[0]]
        if same_name:
            issues.append(
                Issue(
                    code="V-RECONFLICT",
                    severity="blocker",
                    message=(
                        f"费用「{charge.raw_name}」金额存在分歧："
                        f"规则 {same_name[0].amount} vs 模型 {charge.amount}"
                    ),
                    field="amount",
                    detail={"rule": same_name[0].amount, "llm": charge.amount},
                )
            )
            continue
        charge.confidence = min(charge.confidence, 0.7)
        merged_charges.append(charge)
        seen.add(key)

    merged.charges = merged_charges

    if not merged_charges:
        issues.append(
            Issue(
                code="E_NO_CHARGE",
                severity="info",
                message="规则与模型均未抽取到费用项",
            )
        )

    return merged, issues
