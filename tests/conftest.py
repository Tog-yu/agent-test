from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shipping_bill_agent.config import Settings  # noqa: E402
from shipping_bill_agent.schema import BillEntry, Provenance  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
BILLS = ROOT / "data" / "existing_bills.json"


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture()
def samples() -> Path:
    return SAMPLES


@pytest.fixture()
def bills_path() -> Path:
    return BILLS


def make_entry(**overrides) -> BillEntry:
    base = {
        "document_type": "Debit Note",
        "reference_no": "DN-202609-001",
        "document_date": "2026-09-20",
        "issuer": "Destination Agent",
        "recipient": "Origin Agent",
        "charge_item": "Handling Charge",
        "charge_item_raw": "Handling Fee",
        "amount": 40.0,
        "currency": "USD",
        "payment_term": "PP",
        "accounting_entity": "Origin Agent",
    }
    base.update(overrides)
    entry = BillEntry(**base)
    for field_name in (
        "document_type", "reference_no", "document_date", "issuer", "recipient",
        "charge_item", "amount", "currency", "payment_term",
    ):
        if getattr(entry, field_name) is None:
            continue
        entry.provenance[field_name] = Provenance(
            field=field_name,
            source_path="data/samples/DN-202609-001.pdf",
            page=1,
            span=f"{entry.charge_item_raw} {entry.amount} {entry.currency}",
            extractor="rule",
            confidence=0.95,
        )
    return entry
