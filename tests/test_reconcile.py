"""双轨对账回归：空字段与币种别名不能制造假冲突/异常。"""

from __future__ import annotations

from shipping_bill_agent.config import Settings
from shipping_bill_agent.extract.reconcile import reconcile
from shipping_bill_agent.schema import ChargeCandidate, Extraction, FieldVal


def test_empty_rule_field_without_llm_field_is_skipped() -> None:
    rule = Extraction(
        scalars={"bl_no": FieldVal(value=None)},
        charges=[ChargeCandidate(raw_name="Handling Fee", amount="40.00")],
    )
    llm = Extraction(scalars={}, charges=[], extractor="llm")

    merged, issues = reconcile(rule, llm, settings=Settings())

    assert "bl_no" not in merged.scalars
    assert len(merged.charges) == 1
    assert issues == []


def test_currency_symbol_and_iso_code_are_not_a_conflict() -> None:
    rule = Extraction(
        scalars={"currency": FieldVal(value="US$", confidence=1.0)},
        charges=[ChargeCandidate(raw_name="Handling Fee", amount="40.00", currency="US$")],
    )
    llm = Extraction(
        scalars={"currency": FieldVal(value="USD", confidence=0.85, extractor="llm")},
        charges=[],
        extractor="llm",
    )

    merged, issues = reconcile(rule, llm, settings=Settings())

    assert merged.get("currency").value == "US$"
    assert not [issue for issue in issues if issue.code == "V-RECONFLICT"]


def test_llm_charge_name_variant_is_not_duplicated() -> None:
    rule = Extraction(
        charges=[
            ChargeCandidate(
                raw_name="HANDLING CHARGE", amount="50.00", currency="USD"
            )
        ]
    )
    llm = Extraction(
        charges=[
            ChargeCandidate(
                raw_name="SHA pay TAR H/C",
                amount="50.00",
                currency="USD",
                extractor="llm",
            )
        ],
        extractor="llm",
    )

    merged, _ = reconcile(rule, llm, settings=Settings())

    assert len(merged.charges) == 1
    assert merged.charges[0].raw_name == "HANDLING CHARGE"


def test_llm_amount_disagreement_does_not_add_a_second_charge() -> None:
    rule = Extraction(
        charges=[ChargeCandidate(raw_name="Handling Fee", amount="4.00", currency="USD")]
    )
    llm = Extraction(
        charges=[
            ChargeCandidate(
                raw_name="Handling Charge",
                amount="40.00",
                currency="USD",
                extractor="llm",
            )
        ],
        extractor="llm",
    )

    merged, issues = reconcile(rule, llm, settings=Settings())

    assert len(merged.charges) == 1
    assert merged.charges[0].amount == "4.00"
    assert any(issue.code == "V-RECONFLICT" for issue in issues)


def test_llm_unpriced_charge_is_rejected() -> None:
    rule = Extraction(charges=[])
    llm = Extraction(
        charges=[
            ChargeCandidate(
                raw_name="FREIGHT",
                amount=None,
                currency=None,
                extractor="llm",
            )
        ],
        extractor="llm",
    )

    merged, issues = reconcile(rule, llm, settings=Settings())

    assert merged.charges == []
    assert any(issue.code == "V-LLM_UNPRICED" for issue in issues)


def test_same_source_line_with_different_names_is_merged() -> None:
    """真实案例：正文「SHA pay TAR：H/C $50」被两路起了不同名字，不能重复计费。"""
    span = "SHA pay TAR：H/C $50"
    rule = Extraction(
        charges=[ChargeCandidate(raw_name="H/C", amount="50.00", span=span)],
    )
    llm = Extraction(
        charges=[
            ChargeCandidate(
                raw_name="SHA pay TAR", amount="50.00", span=span, extractor="llm"
            )
        ],
        extractor="llm",
    )

    merged, issues = reconcile(rule, llm, settings=Settings())

    assert len(merged.charges) == 1
    assert merged.charges[0].raw_name == "H/C"
    assert any(issue.code == "V-DUP_LINE" for issue in issues)


def test_different_lines_with_same_amount_are_kept() -> None:
    """同一单据里两条同额不同行的费用必须都保留（不能按金额一刀切）。"""
    rule = Extraction(
        charges=[ChargeCandidate(raw_name="Handling Charge", amount="50.00", span="HANDLING CHARGE 50.00")],
    )
    llm = Extraction(
        charges=[
            ChargeCandidate(
                raw_name="Trucking Charge",
                amount="50.00",
                span="TRUCKING CHARGE 50.00",
                extractor="llm",
            )
        ],
        extractor="llm",
    )

    merged, _ = reconcile(rule, llm, settings=Settings())

    assert len(merged.charges) == 2
