"""决策层测试：CREATE / UPDATE 字段级 patch / NO_CHANGE / PENDING。"""

from __future__ import annotations

from conftest import make_entry

from shipping_bill_agent.config import Settings
from shipping_bill_agent.decide.planner import plan
from shipping_bill_agent.schema import Issue, MatchInfo, Provenance

SETTINGS = Settings()

ORIGINAL = {
    "document_type": "Debit Note",
    "reference_no": "DN-202609-001",
    "document_date": "2026-09-20",
    "issuer": "Destination Agent",
    "recipient": "Origin Agent",
    "charge_item": "Handling Charge",
    "charge_item_raw": "Handling Fee",
    "amount": 35.0,
    "currency": "USD",
    "payment_term": "PP",
    "quantity": None,
    "unit_price": None,
    "tax": None,
}


def _matched(bill_id: str = "B-1001") -> MatchInfo:
    return MatchInfo(tier="T1", bill_id=bill_id)


def test_create_when_no_match() -> None:
    entry = make_entry()
    entry.match = MatchInfo(tier=None)
    plan(entry, original=None, settings=SETTINGS)
    assert entry.operation == "CREATE"
    assert entry.posting_type == "AP"


def test_update_only_when_explicit_change_has_evidence() -> None:
    entry = make_entry(amount=40.0)
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    assert entry.operation == "UPDATE"
    assert entry.amount == 40.0
    assert "amount" in entry.provenance
    assert any("明确要求变更" in line for line in entry.ai_reasoning)


def test_unchanged_fields_are_inherited_not_overwritten() -> None:
    entry = make_entry(amount=40.0)
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    # 币种/付款方式输入材料抽到了但与原文一致；数量/单价原值为空 → 不算变更
    assert entry.operation == "UPDATE"
    assert entry.quantity is None
    assert entry.unit_price is None


def test_missing_evidence_means_inherit() -> None:
    entry = make_entry(amount=35.0, currency="CNY")
    entry.provenance.pop("currency")  # 没有证据 → 必须继承原值
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    assert entry.currency == "USD"
    assert entry.operation == "NO_CHANGE"


def test_no_change_when_values_identical() -> None:
    entry = make_entry(amount=35.0)
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    assert entry.operation == "NO_CHANGE"
    assert entry.intended_operation == "NO_CHANGE"


def test_blocker_forces_pending_review() -> None:
    entry = make_entry(amount=40.0)
    entry.issues.append(Issue(code="V-RECONCILIATION", severity="blocker", message="不平"))
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    assert entry.operation == "PENDING_REVIEW"
    assert entry.intended_operation == "UPDATE"  # 意图仍保留在报告里


def test_ambiguous_match_forces_pending_review() -> None:
    entry = make_entry()
    entry.match = MatchInfo(tier="T1", ambiguous=True, candidates=[{"bill_id": "B-1"}, {"bill_id": "B-2"}])
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    assert entry.operation == "PENDING_REVIEW"
    assert any(issue.code == "M-AMBIGUOUS" for issue in entry.issues)


def test_unknown_posting_type_forces_pending_review() -> None:
    entry = make_entry(issuer="A Agent", recipient="B Agent")
    entry.match = MatchInfo(tier=None)
    plan(entry, original=None, settings=SETTINGS)
    assert entry.posting_type is None
    assert entry.operation == "PENDING_REVIEW"


def test_low_confidence_change_forces_pending_review() -> None:
    entry = make_entry(amount=4.0)
    entry.provenance["amount"] = Provenance(
        field="amount", span="4O.OO", extractor="rule:columns", confidence=0.55
    )
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    assert entry.operation == "PENDING_REVIEW"
    assert any(issue.code == "P-LOW_CONFIDENCE" for issue in entry.issues)


def test_reasoning_covers_required_sections() -> None:
    entry = make_entry(amount=40.0)
    entry.match = _matched()
    plan(entry, original=dict(ORIGINAL), settings=SETTINGS)
    text = "\n".join(entry.ai_reasoning)
    assert "识别到费用项" in text
    assert "标准化" in text
    assert "匹配到已有账单" in text
    assert "应收应付判定" in text
