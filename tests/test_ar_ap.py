"""AR/AP 四象限 + 主体缺席 + Credit Note 两种口径。"""

from __future__ import annotations

import pytest

from shipping_bill_agent.decide.ar_ap import decide_posting_type, party_matches

ENTITY = "Origin Agent"
ISSUER = "Destination Agent"


def test_dn_entity_is_recipient_is_ap() -> None:
    posting, reason = decide_posting_type(
        document_type="Debit Note", issuer=ISSUER, recipient=ENTITY, entity=ENTITY
    )
    assert posting == "AP"
    assert "接收方" in reason


def test_dn_entity_is_issuer_is_ar() -> None:
    posting, _ = decide_posting_type(
        document_type="Debit Note", issuer=ENTITY, recipient=ISSUER, entity=ENTITY
    )
    assert posting == "AR"


def test_cn_mirror_flips_sides() -> None:
    posting, _ = decide_posting_type(
        document_type="Credit Note", issuer=ENTITY, recipient=ISSUER, entity=ENTITY,
        credit_policy="mirror",
    )
    assert posting == "AP"

    posting, _ = decide_posting_type(
        document_type="Credit Note", issuer=ISSUER, recipient=ENTITY, entity=ENTITY,
        credit_policy="mirror",
    )
    assert posting == "AR"


def test_cn_same_side_policy() -> None:
    posting, _ = decide_posting_type(
        document_type="Credit Note", issuer=ENTITY, recipient=ISSUER, entity=ENTITY,
        credit_policy="same_side",
    )
    assert posting == "AR"


def test_entity_not_in_parties_is_unknown() -> None:
    posting, reason = decide_posting_type(
        document_type="Debit Note", issuer="A Agent", recipient="B Agent", entity=ENTITY
    )
    assert posting is None
    assert "不在本单交易链" in reason


def test_missing_entity_is_unknown() -> None:
    posting, _ = decide_posting_type(
        document_type="Debit Note", issuer="A", recipient="B", entity=None
    )
    assert posting is None


def test_unknown_document_type_is_unknown() -> None:
    posting, _ = decide_posting_type(
        document_type="Unknown", issuer=ISSUER, recipient=ENTITY, entity=ENTITY
    )
    assert posting is None


def test_invoice_behaves_like_debit_note() -> None:
    posting, _ = decide_posting_type(
        document_type="Invoice", issuer=ISSUER, recipient=ENTITY, entity=ENTITY
    )
    assert posting == "AP"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("Origin Agent", "origin agent", True),
        ("Origin Agent", "Shanghai Origin Agent Co., Ltd", True),
        ("Origin Agent", "Destination Agent", False),
        ("Origin Agent", None, False),
    ],
)
def test_party_matching_tolerates_suffixes(left, right, expected) -> None:
    assert party_matches(left, right) is expected
