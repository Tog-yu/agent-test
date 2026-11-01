"""文本层抽取结果 × OCR 副源抽取结果的合并。

为什么需要合并
--------------
同一份 PDF 有两条抽取路径：PDF 自带的文本层，和网关 OCR。实测（`docs/REAL_DATA_NOTES.md` §9）
两者各有强弱，谁也不能无脑覆盖谁：

===========  ======================================  ==========================
字段          文本层                                   OCR
===========  ======================================  ==========================
单号          `DC-108478`（有）                       空（没抽到）
币种          USD                                    空（`chargeList` 无币种字段）
明细粒度      65.00 + 1310.40 + 35.00（3 条）          100.00 + 1310.40（2 条，合并了）
提单号        有（部分版式没有）                       `mblNumber` 稳定给出
箱号          正文常缺                                 `ctrList[].ctrNumber` 稳定给出
===========  ======================================  ==========================

因此：**文本层做主源，OCR 只补它缺的标识字段。**

不合并的字段及原因
------------------
- `document_type`：网关给的是 `PAI-COLOADER-NOTICE` / `PAI-CMA-NOTICE` 这类自家分类，
  不是本系统的标准单据类型，映射不可靠，硬用等于制造分类错误。
- `issuer` / `recipient`：网关的 `shipper` / `consignee` 是**货物关系方**，
  语义上不等于账单签发方 / 接收方（Debit Note 的 consignee 常常既不是出单人也不是付款人）。
- 金额 / 币种 / 日期：文本层可逐字回溯原文，OCR 有识别误差，冲突时文本层优先。
"""

from __future__ import annotations

import re

from ..schema import Extraction, FieldVal

# 允许 OCR 补充的字段：都是纯标识，语义无歧义
SUPPLEMENT_FIELDS: tuple[str, ...] = ("bl_no", "booking_no", "container_no", "reference_no")

# OCR 来源的置信度上限：网关是黑盒，补进来的值不该和原文直读同等可信
OCR_SUPPLEMENT_MAX_CONFIDENCE = 0.7


def _has_value(field: FieldVal | None) -> bool:
    return field is not None and field.value not in (None, "")


def merge_ocr_supplement(
    primary: Extraction, supplement: Extraction
) -> tuple[Extraction, list[str]]:
    """把 OCR 副源里「主源缺失」的标识字段补进主源，就地修改并返回 (primary, 补了哪些)。"""
    filled: list[str] = []

    for name in SUPPLEMENT_FIELDS:
        if _has_value(primary.scalars.get(name)):
            continue
        candidate = supplement.scalars.get(name)
        if not _has_value(candidate):
            continue
        value = str(candidate.value).strip()
        # 单号必须含数字：OCR 有时会把标签文字当成单号值
        if name == "reference_no" and not re.search(r"\d{3,}", value):
            continue
        primary.scalars[name] = FieldVal(
            value=value,
            span=candidate.span,
            extractor="ocr:supplement",
            confidence=min(candidate.confidence, OCR_SUPPLEMENT_MAX_CONFIDENCE),
            source_path=candidate.source_path,
        )
        filled.append(name)

    # 费用只在主源一条都没有时才用 OCR 的：说明这份 PDF 的文本层撑不起抽取
    if not primary.charges and supplement.charges:
        primary.charges = supplement.charges
        filled.append("charges")

    return primary, filled
