"""校验闸测试：G1 金额 / G2 币种 / G3 勾稽 / G4 OCR 混淆 / G5 重复。"""

from __future__ import annotations

from conftest import make_entry

from shipping_bill_agent.config import Settings
from shipping_bill_agent.schema import Issue
from shipping_bill_agent.validate.ocr_repair import detect_confusion, repair_currency
from shipping_bill_agent.validate.validators import DocTotals, needs_review, validate_entries


def _codes(entry) -> list[str]:
    return [issue.code for issue in entry.issues]


def test_g1_missing_amount_is_blocker() -> None:
    entry = make_entry(amount=None)
    entry.provenance.pop("amount", None)
    validate_entries([entry], totals=DocTotals(), settings=Settings())
    assert "V-AMOUNT_INVALID" in _codes(entry)
    assert entry.has_blocker()


def test_g1_negative_amount_on_debit_note_is_blocker() -> None:
    entry = make_entry(amount=-40.0)
    validate_entries([entry], totals=DocTotals(), settings=Settings())
    assert entry.has_blocker()


def test_g1_negative_amount_on_credit_note_is_allowed() -> None:
    entry = make_entry(amount=-40.0, document_type="Credit Note")
    validate_entries([entry], totals=DocTotals(), settings=Settings())
    assert "V-AMOUNT_INVALID" not in _codes(entry)


def test_g2_non_iso_currency_is_warn_only() -> None:
    entry = make_entry(currency="XYZ")
    validate_entries([entry], totals=DocTotals(), settings=Settings())
    assert "V-CURRENCY_INVALID" in _codes(entry)
    assert not entry.has_blocker()  # 不阻断，只记风险


def test_g3_reconciliation_mismatch_is_blocker() -> None:
    entries = [
        make_entry(amount=40.0, charge_item="Handling Charge"),
        make_entry(amount=100.0, charge_item="Terminal Handling Charge"),
    ]
    validate_entries(
        entries, totals=DocTotals(subtotal=140.0, tax=0.0, total=195.0), settings=Settings()
    )
    assert all("V-RECONCILIATION" in _codes(entry) for entry in entries)
    assert any(entry.has_blocker() for entry in entries)


def test_g3_reconciliation_ok_within_tolerance() -> None:
    entries = [make_entry(amount=195.004)]
    validate_entries(entries, totals=DocTotals(total=195.0), settings=Settings())
    assert "V-RECONCILIATION" not in _codes(entries[0])


def test_g3_tax_already_included_downgraded_to_info() -> None:
    entries = [make_entry(amount=195.0)]
    validate_entries(entries, totals=DocTotals(tax=10.0, total=195.0), settings=Settings())
    issues = [i for i in entries[0].issues if i.code == "V-RECONCILIATION"]
    assert issues and issues[0].severity == "info"


def test_g4_confusion_detected_but_never_auto_fixed() -> None:
    entry = make_entry(amount=4.0)
    entry.provenance["amount"].span = "Handling Fee  4O.OO  PP"
    validate_entries([entry], totals=DocTotals(), settings=Settings())
    issues = [i for i in entry.issues if i.code == "V-OCR_CONFUSION"]
    assert issues
    assert issues[0].detail["review_required"] is True
    assert entry.amount == 4.0  # 值没被擅自改成 40.00
    assert needs_review(entry) is True


def test_g4_clean_amount_not_flagged() -> None:
    entry = make_entry(amount=40.0)
    validate_entries([entry], totals=DocTotals(), settings=Settings())
    assert "V-OCR_CONFUSION" not in _codes(entry)


def test_g5_duplicate_line_kept_and_flagged() -> None:
    """同单同费用同金额：业务上可能分箱/分批计费，必须保留并标疑，不能静默丢弃。"""
    entries = [
        make_entry(amount=40.0),
        make_entry(amount=40.0),
    ]
    validate_entries(entries, totals=DocTotals(), settings=Settings())
    assert "V-DUP_LINE" not in _codes(entries[0])
    assert "V-DUP_LINE" in _codes(entries[1])
    dup = next(i for i in entries[1].issues if i.code == "V-DUP_LINE")
    assert dup.severity == "warn"
    assert dup.detail["kept"] is True
    assert len(entries) == 2  # 两条都在，钱没有凭空消失


def test_g3_multi_currency_not_summed_across_currencies() -> None:
    """多币种单据不能把不同币种的明细直接相加去和单一总额比。"""
    entries = [make_entry(amount=100.0, currency="USD"), make_entry(amount=50.0, currency="EUR")]
    validate_entries(entries, totals=DocTotals(total=100.0), settings=Settings())
    codes = _codes(entries[0])
    assert "V-MULTI_CURRENCY" in codes
    assert "V-RECONCILIATION" not in codes  # 不该报「明细 150 与总计 100 不一致」


def test_g3_per_currency_totals_reconciled() -> None:
    entries = [make_entry(amount=100.0, currency="USD"), make_entry(amount=50.0, currency="EUR")]
    totals = DocTotals(total=100.0, total_by_currency={"USD": 100.0, "EUR": 50.0})
    validate_entries(entries, totals=totals, settings=Settings())
    assert "V-RECONCILIATION" not in _codes(entries[0])


def test_g3_per_currency_totals_mismatch_is_blocker() -> None:
    entries = [make_entry(amount=100.0, currency="USD"), make_entry(amount=40.0, currency="EUR")]
    totals = DocTotals(total=100.0, total_by_currency={"USD": 100.0, "EUR": 50.0})
    validate_entries(entries, totals=totals, settings=Settings())
    issues = [i for i in entries[0].issues if i.code == "V-RECONCILIATION"]
    assert issues and issues[0].severity == "blocker"
    assert issues[0].detail["currency"] == "EUR"


def test_detect_confusion_positions() -> None:
    findings = detect_confusion("4O.OO", field="amount")
    assert findings and findings[0].char == "O"
    assert findings[0].candidates == ("0",)


def test_detect_confusion_ignores_normal_codes() -> None:
    assert detect_confusion("USD", field="currency") == []
    assert detect_confusion("DN-202609-001", field="reference_no") == []
    assert detect_confusion("COSU6384910020", field="container_no") == []


def test_repair_currency_only_when_verifiable() -> None:
    assert repair_currency("USD") == "USD"
    assert repair_currency("HKD") == "HKD"
    assert repair_currency("US0") == "USD"  # 0→D? 不，是 0→O 之外的唯一白名单解，可验证才修
    assert repair_currency("1SD") is None  # 无唯一解就不猜


def test_issue_severity_contract() -> None:
    issue = Issue(code="X", severity="warn")
    assert issue.severity in {"blocker", "warn", "info"}
