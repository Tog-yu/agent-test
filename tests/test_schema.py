"""输出契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

from shipping_bill_agent.cli import _check
from shipping_bill_agent.schema import (
    OPERATIONS,
    OutputChargeDetails,
    OutputRecord,
)


def _record(**overrides) -> OutputRecord:
    base = OutputRecord()
    base.operation = "UPDATE"
    base.document_info.document_type = "Debit Note"
    base.document_info.reference_no = "DN-202609-001"
    base.document_info.document_date = "2026-09-20"
    base.transaction_party.issuer = "Destination Agent"
    base.transaction_party.recipient = "Origin Agent"
    base.charge_details = OutputChargeDetails(
        charge_item="Handling Charge",
        charge_item_raw="Handling Fee",
        amount=40.0,
        currency="USD",
        payment_term="PP",
    )
    base.financial_attributes.accounting_entity = "Origin Agent"
    base.financial_attributes.posting_type = "AP"
    base.audit_trail.ai_reasoning = ["依据 A", "依据 B"]
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_contract_keys_exact() -> None:
    payload = _record().to_contract(extension_fields=False)
    assert set(payload) == {
        "operation",
        "document_info",
        "transaction_party",
        "charge_details",
        "financial_attributes",
        "audit_trail",
    }
    assert set(payload["charge_details"]) == {"charge_item", "amount", "currency", "payment_term"}
    assert set(payload["document_info"]) == {"document_type", "reference_no", "document_date"}
    assert set(payload["transaction_party"]) == {"issuer", "recipient"}
    assert set(payload["financial_attributes"]) == {"accounting_entity", "posting_type"}
    assert set(payload["audit_trail"]) == {"ai_reasoning"}


def test_extension_field_toggle() -> None:
    assert "charge_item_raw" in _record().to_contract(extension_fields=True)["charge_details"]
    assert "charge_item_raw" not in _record().to_contract(extension_fields=False)["charge_details"]


def test_null_values_are_kept_as_keys() -> None:
    record = _record()
    record.charge_details.currency = None
    payload = record.to_contract()
    assert payload["charge_details"]["currency"] is None
    assert "currency" in payload["charge_details"]


def test_amount_is_json_number() -> None:
    payload = _record().to_contract()
    dumped = json.loads(json.dumps(payload))
    assert isinstance(dumped["charge_details"]["amount"], (int, float))


def test_checker_accepts_valid_payload() -> None:
    assert _check([_record().to_contract()], allow_extensions=True) == []


def test_checker_rejects_missing_key() -> None:
    payload = _record().to_contract()
    payload.pop("audit_trail")
    problems = _check([payload], allow_extensions=True)
    assert any("缺少键" in p for p in problems)


def test_checker_rejects_string_amount() -> None:
    payload = _record().to_contract()
    payload["charge_details"]["amount"] = "40.00"
    problems = _check([payload], allow_extensions=True)
    assert any("amount 必须是数字" in p for p in problems)


def test_checker_rejects_bad_operation() -> None:
    payload = _record().to_contract()
    payload["operation"] = "MERGE"
    problems = _check([payload], allow_extensions=True)
    assert any("operation 非法" in p for p in problems)


def test_checker_rejects_top_level_object() -> None:
    assert _check({"records": []}, allow_extensions=True)


def test_operations_enum_matches_spec() -> None:
    assert set(OPERATIONS) == {"CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW"}


def test_write_outputs_produces_four_artifacts(tmp_path: Path) -> None:
    from conftest import make_entry

    from shipping_bill_agent.audit import Recorder
    from shipping_bill_agent.config import Settings
    from shipping_bill_agent.pipeline import write_outputs

    entry = make_entry()
    entry.ai_reasoning = ["x"]
    paths = write_outputs(
        [entry], settings=Settings(), recorder=Recorder(), errors=[], out_dir=tmp_path
    )
    for key in ("result", "report", "errors", "trace"):
        assert paths[key].exists(), key
    assert isinstance(json.loads(paths["result"].read_text(encoding="utf-8")), list)
