"""摄入层测试：格式嗅探、递归解析、旧格式报错、表格结构。"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipping_bill_agent.config import Settings
from shipping_bill_agent.ingest.dispatch import detect_kind, expand_inputs, ingest
from shipping_bill_agent.ingest.eml_parser import parse_eml
from shipping_bill_agent.ingest.excel_parser import parse_excel
from shipping_bill_agent.ingest.word_parser import parse_word


def test_detect_kind_by_extension_and_magic(samples: Path, tmp_path: Path) -> None:
    assert detect_kind(samples / "DN-202609-001.pdf") == "pdf"
    assert detect_kind(samples / "CN-202609-007.png") == "image"
    disguised = tmp_path / "noext"
    disguised.write_bytes(b"%PDF-1.4\n%fake")
    assert detect_kind(disguised) == "pdf"


def test_expand_inputs_skips_hidden_and_dirs(samples: Path) -> None:
    files = expand_inputs([samples])
    names = {f.name for f in files}
    assert "DN-202609-001.pdf" in names
    assert all(f.is_file() for f in files)
    # 子目录（eml/）不应被展开进来：单封信应由调用方显式指定
    assert not any(f.suffix == ".eml" for f in files)


def test_ingest_corrupt_xls_reports_error(samples: Path, tmp_path: Path) -> None:
    """损坏的 .xls 要给出可读错误，而不是让批次崩掉。"""
    broken = tmp_path / "broken.xls"
    broken.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 256)
    doc = ingest(broken, workdir=tmp_path, settings=Settings())
    assert doc.errors
    assert "E_XLS_CORRUPT" in doc.errors[0]


def test_ingest_pdf_text_layer(samples: Path, tmp_path: Path) -> None:
    doc = ingest(samples / "DN-202609-001.pdf", workdir=tmp_path, settings=Settings())
    assert doc.doc_kind == "pdf"
    assert doc.meta["has_text_layer"] is True
    assert "DN-202609-001" in doc.text
    # 块级文本必须保留列间距（否则按列切分会失效）
    charge_lines = [b.text for b in doc.blocks if "Handling Fee" in b.text]
    assert charge_lines and "  " in charge_lines[0]


def test_parse_eml_headers_and_attachments(samples: Path, tmp_path: Path) -> None:
    doc = parse_eml(
        samples / "eml" / "mail_debit_note_001.eml",
        workdir=tmp_path,
        settings=Settings(),
    )
    assert doc.doc_kind == "eml"
    assert "DN-202609-001" in (doc.meta["subject"] or "")
    assert "destination-agent.example" in (doc.meta["from"] or "")
    assert doc.meta["attachments"] == ["DN-202609-001.pdf"]
    assert len(doc.children) == 1
    child = doc.children[0]
    assert child.doc_kind == "pdf"
    assert child.source.origin.endswith("attachment:DN-202609-001.pdf")
    assert "Origin Agent" in doc.text


def test_parse_excel_tables(samples: Path) -> None:
    from shipping_bill_agent.ingest.dispatch import ingest as _ingest

    doc = _ingest(samples / "INV-202609-050.xlsx", workdir=Path("/tmp"), settings=Settings())
    assert doc.doc_kind == "excel"
    assert doc.tables
    table = doc.tables[0]
    assert "Charge Description" in table.header
    assert any("Trucking Fee" in cell for row in table.rows for cell in row)


def test_parse_word_table(samples: Path) -> None:
    from shipping_bill_agent.ingest.dispatch import ingest as _ingest

    doc = _ingest(samples / "DN-202609-002.docx", workdir=Path("/tmp"), settings=Settings())
    assert doc.doc_kind == "word"
    assert doc.tables
    assert "Charge Description" in doc.tables[0].header
    assert "DN-202609-002" in doc.text


def test_missing_file_does_not_raise(tmp_path: Path) -> None:
    doc = ingest(tmp_path / "nope.pdf", workdir=tmp_path, settings=Settings())
    assert doc.errors
