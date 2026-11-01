"""抽取测试：单号/日期/交易方/费用行三档 + 证据 span。"""

from __future__ import annotations

from pathlib import Path

from shipping_bill_agent.config import Settings
from shipping_bill_agent.extract.rule_extractor import extract_by_rules
from shipping_bill_agent.ingest.dispatch import ingest
from shipping_bill_agent.ocr.normalize import normalize_ocr
from shipping_bill_agent.ocr.providers import CassetteOcrProvider


def _doc(path: Path, tmp_path: Path):
    return ingest(path, workdir=tmp_path, settings=Settings())


def test_pdf_extraction_full(samples: Path, tmp_path: Path) -> None:
    doc = _doc(samples / "DN-202609-001.pdf", tmp_path)
    extraction = extract_by_rules(doc)

    assert extraction.get("document_type").value == "Debit Note"
    assert extraction.get("reference_no").value == "DN-202609-001"
    assert extraction.get("document_date").value == "2026-09-20"
    assert extraction.get("issuer").value == "Destination Agent"
    assert extraction.get("recipient").value == "Origin Agent"
    assert extraction.get("bl_no").value == "COSU6384910020"
    assert extraction.get("booking_no").value == "BK20260915001"
    assert extraction.get("container_no").value == "CSQU3054383"
    assert extraction.get("payment_term").value == "PP"
    assert extraction.get("total").value == 195.0
    assert extraction.get("currency").value == "USD"

    names = [charge.raw_name for charge in extraction.charges]
    assert names == ["Handling Fee", "Terminal Handling Charge", "Doc Fee"]
    amounts = [charge.amount for charge in extraction.charges]
    assert amounts == [40.0, 120.0, 35.0]
    assert all(charge.currency == "USD" for charge in extraction.charges)
    assert all(charge.payment_term == "PP" for charge in extraction.charges)


def test_every_field_has_evidence_span(samples: Path, tmp_path: Path) -> None:
    doc = _doc(samples / "DN-202609-001.pdf", tmp_path)
    extraction = extract_by_rules(doc)
    for name in ("reference_no", "document_date", "issuer", "recipient", "total"):
        field = extraction.get(name)
        assert field.value is not None, name
        assert field.span, f"{name} 缺少原文证据"
    for charge in extraction.charges:
        assert charge.span and charge.source_path
        assert charge.amount is not None


def test_excel_extraction_uses_table_columns(samples: Path, tmp_path: Path) -> None:
    doc = _doc(samples / "INV-202609-050.xlsx", tmp_path)
    extraction = extract_by_rules(doc)
    assert extraction.get("reference_no").value == "INV-202609-050"
    assert extraction.get("issuer").value == "Destination Agent"
    assert extraction.get("recipient").value == "Origin Agent"
    assert extraction.get("document_date").value == "2026-09-25"
    assert [c.raw_name for c in extraction.charges] == ["Trucking Fee", "Customs Fee", "Storage Fee"]
    assert [c.amount for c in extraction.charges] == [300.0, 80.0, 25.0]
    assert all(c.payment_term == "CC" for c in extraction.charges)


def test_word_extraction_with_symbol_currency(samples: Path, tmp_path: Path) -> None:
    doc = _doc(samples / "DN-202609-002.docx", tmp_path)
    extraction = extract_by_rules(doc)
    assert extraction.get("document_type").value == "Debit Note"
    assert extraction.get("issuer").value == "Origin Agent"
    assert extraction.get("recipient").value == "Destination Agent"
    assert [c.amount for c in extraction.charges] == [50.0, 280.0]
    # 抽取层保留原始写法（US$），归一到 ISO 代码由归一化层负责
    from shipping_bill_agent.normalize.currency import normalize_currency

    assert normalize_currency(extraction.get("currency").value)[0] == "USD"


def test_ocr_image_extraction(samples: Path, tmp_path: Path, settings: Settings) -> None:
    doc = _doc(samples / "CN-202609-007.png", tmp_path)
    raw = CassetteOcrProvider(settings).read(samples / "CN-202609-007.png", task="orderChange")
    text, blocks, tables = normalize_ocr(raw)
    doc.text, doc.blocks, doc.tables = text, blocks, tables

    extraction = extract_by_rules(doc)
    assert extraction.get("document_type").value == "Credit Note"
    assert extraction.get("reference_no").value == "CN-202609-007"
    assert [c.raw_name for c in extraction.charges] == ["THC"]
    assert extraction.charges[0].amount == 15.0
    assert extraction.charges[0].payment_term == "PP"


def test_ocr_confused_amount_is_extracted_but_low_confidence(
    samples: Path, tmp_path: Path, settings: Settings
) -> None:
    """4O.OO 不能导致整条费用消失，也不能被当成高可信金额。"""
    doc = _doc(samples / "DN-202609-003.png", tmp_path)
    raw = CassetteOcrProvider(settings).read(samples / "DN-202609-003.png", task="orderChange")
    text, blocks, tables = normalize_ocr(raw)
    doc.text, doc.blocks, doc.tables = text, blocks, tables

    extraction = extract_by_rules(doc)
    assert len(extraction.charges) == 1
    charge = extraction.charges[0]
    assert charge.raw_name == "Handling Fee"
    assert charge.confidence < 0.9
    assert "4O.OO" in (charge.span or "")


def test_email_context_fills_missing_parties(samples: Path, tmp_path: Path) -> None:
    from shipping_bill_agent.schema import DocumentContext

    doc = _doc(samples / "INV-202609-050.xlsx", tmp_path)
    # 造一个"缺签发方"的文档：删掉相关行
    doc.text = "\n".join(
        line for line in doc.text.split("\n") if "Issued by" not in line
    )
    doc.blocks = [b for b in doc.blocks if "Issued by" not in b.text]

    ctx = DocumentContext(
        subject="Invoice INV-202609-050 update",
        sender="Destination Agent <a@b.c>",
        recipient="Origin Agent <d@e.f>",
        date="Tue, 22 Sep 2026 10:00:00 +0800",
        body_text="",
        origin="mail.eml",
    )
    extraction = extract_by_rules(doc, ctx=ctx)
    # 邮件头只取显示名，不能把 "Name <mail>" 原样塞进交易方
    assert extraction.get("issuer").value == "Destination Agent"
    assert extraction.get("issuer").span.startswith("From:")
    assert extraction.get("reference_no").value == "INV-202609-050"


def test_email_header_without_display_name_is_not_used_as_party(samples: Path, tmp_path: Path) -> None:
    """发件人只有邮箱地址时，不能把它当签发方（邮箱不是主体名称）。"""
    from shipping_bill_agent.schema import DocumentContext

    doc = _doc(samples / "INV-202609-050.xlsx", tmp_path)
    doc.text = "\n".join(line for line in doc.text.split("\n") if "Issued by" not in line)
    doc.blocks = [b for b in doc.blocks if "Issued by" not in b.text]
    ctx = DocumentContext(
        subject="Invoice INV-202609-050",
        sender='"luka@pltplt.com" <luka@pltplt.com>',
        recipient="Origin Agent <d@e.f>",
        date="Tue, 22 Sep 2026 10:00:00 +0800",
        body_text="",
        origin="mail.eml",
    )
    extraction = extract_by_rules(doc, ctx=ctx)
    assert extraction.get("issuer").value != '"luka@pltplt.com" <luka@pltplt.com>'
    assert "@" not in str(extraction.get("issuer").value or "")
