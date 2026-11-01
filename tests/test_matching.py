"""匹配测试：四级优先级、多命中不猜、tie-break。"""

from __future__ import annotations

from conftest import make_entry

from shipping_bill_agent.config import Settings
from shipping_bill_agent.matching.matcher import match
from shipping_bill_agent.matching.store import BillRecord, ExistingBillStore, normalize_key


def _store(*records: dict) -> ExistingBillStore:
    return ExistingBillStore([BillRecord.from_dict(r, i) for i, r in enumerate(records)])


BASE = {
    "bill_id": "B-1",
    "document_type": "Debit Note",
    "reference_no": "DN-202609-001",
    "document_date": "2026-09-20",
    "issuer": "Destination Agent",
    "recipient": "Origin Agent",
    "charge_item": "Handling Charge",
    "amount": 35.0,
    "currency": "USD",
}


def test_t1_match_by_reference_and_charge() -> None:
    result = match(make_entry(), _store(BASE), settings=Settings())
    assert result.tier == "T1"
    assert result.bill_id == "B-1"
    assert result.ambiguous is False


def test_no_match_when_charge_differs() -> None:
    record = dict(BASE, charge_item="Storage Charge")
    result = match(make_entry(), _store(record), settings=Settings())
    assert result.tier is None
    assert result.bill_id is None


def test_t1_shadows_t4() -> None:
    t4_only = dict(
        BASE,
        reference_no="OTHER-1",
        charge_item="Handling Charge",
        container_no="CSQU3054383",
        amount=40.0,
        document_date="2026-09-20",
    )
    entry = make_entry(container_no="CSQU3054383")
    result = match(entry, _store(t4_only), settings=Settings())
    assert result.tier == "T4"
    assert result.bill_id == "B-1"


def test_ambiguous_when_two_candidates_same_amount() -> None:
    store = _store(
        dict(BASE, bill_id="B-1"),
        dict(BASE, bill_id="B-2"),
    )
    result = match(make_entry(amount=35.0), store, settings=Settings())
    assert result.ambiguous is True
    assert result.bill_id is None
    assert len(result.candidates) == 2


def test_tie_break_by_amount_proximity() -> None:
    store = _store(
        dict(BASE, bill_id="B-1", amount=35.0),
        dict(BASE, bill_id="B-2", amount=40.0),
    )
    result = match(make_entry(amount=40.0), store, settings=Settings())
    assert result.ambiguous is False
    assert result.bill_id == "B-2"


def test_t4_requires_all_keys_in_strict_mode() -> None:
    record = dict(
        BASE, bill_id="B-9", reference_no="OTHER-9", bl_no=None, booking_no=None,
        container_no="CSQU3054383", amount=40.0,
    )
    store = _store(record)

    complete = make_entry(reference_no="ZZZ-1", container_no="CSQU3054383")
    assert match(complete, store, settings=Settings()).tier == "T4"

    missing_container = make_entry(reference_no="ZZZ-1", container_no=None)
    assert match(missing_container, store, settings=Settings()).tier is None


def test_empty_store_returns_no_match() -> None:
    result = match(make_entry(), ExistingBillStore([]), settings=Settings())
    assert result.tier is None


def test_normalize_key_is_case_and_space_insensitive() -> None:
    assert normalize_key(" DN-202609-001 ") == normalize_key("dn-202609-001")
    assert normalize_key(None) is None


def test_has_duplicate_for_idempotency() -> None:
    store = _store(BASE)
    entry = make_entry(amount=35.0)
    assert store.has_duplicate(entry)
    entry.amount = 41.0
    assert not store.has_duplicate(entry)
