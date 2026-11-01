"""旧格式（.xls / .doc）与不支持格式的回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipping_bill_agent.config import Settings
from shipping_bill_agent.extract.rule_extractor import extract_by_rules
from shipping_bill_agent.ingest.dispatch import ingest
from shipping_bill_agent.pipeline import run

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "data" / "samples" / "legacy"
XLS = LEGACY / "legacy_rates.xls"
DOC = LEGACY / "legacy_debit_note.doc"


@pytest.mark.skipif(not XLS.exists(), reason="先跑 python scripts/make_samples.py")
def test_xls_parsed_into_tables(tmp_path: Path) -> None:
    doc = ingest(XLS, workdir=tmp_path, settings=Settings())
    assert doc.errors == []
    assert doc.doc_kind == "excel"
    assert doc.meta["format"] == "xls"
    assert doc.tables and doc.tables[0].header == ["Charge", "Amount", "Currency"]

    extraction = extract_by_rules(doc)
    assert extraction.get("reference_no").value == "DC-202609-900"
    payload = {(c.raw_name, c.amount, c.currency) for c in extraction.charges}
    assert ("THC", 120.0, "USD") in payload
    assert ("Doc Fee", 35.0, "USD") in payload


@pytest.mark.skipif(not DOC.exists(), reason="需要 textutil/soffice/antiword 才能生成 .doc 样本")
def test_doc_parsed_into_tables(tmp_path: Path) -> None:
    doc = ingest(DOC, workdir=tmp_path, settings=Settings())
    assert doc.errors == []
    assert doc.doc_kind == "word"
    assert doc.meta["format"] == "doc"
    assert doc.tables and doc.tables[0].header == ["Charge", "Amount", "Currency"]

    extraction = extract_by_rules(doc)
    assert extraction.get("reference_no").value == "DC-202609-901"
    assert {c.raw_name for c in extraction.charges} == {"THC", "Doc Fee"}


@pytest.mark.skipif(not XLS.exists(), reason="先跑 python scripts/make_samples.py")
def test_legacy_formats_end_to_end(tmp_path: Path) -> None:
    settings = Settings().with_overrides(
        pipeline__pdf_strategy="auto", pipeline__accounting_entity="Legacy Feeder Co., Ltd."
    )
    result = run([LEGACY], settings=settings, out_dir=tmp_path / "out")
    assert result.errors == []
    assert len(result.records) == 4  # 2 个文件 × 2 条费用
    amounts = sorted(
        (r["document_info"]["reference_no"], r["charge_details"]["amount"]) for r in result.records
    )
    assert amounts == [
        ("DC-202609-900", 35.0),
        ("DC-202609-900", 120.0),
        ("DC-202609-901", 35.0),
        ("DC-202609-901", 120.0),
    ]


def test_unsupported_format_is_isolated(tmp_path: Path) -> None:
    settings = Settings().with_overrides(pipeline__pdf_strategy="auto")
    result = run(
        [ROOT / "data" / "samples"], settings=settings, out_dir=tmp_path / "out"
    )
    assert any("E_UNSUPPORTED_DOC" in item["error"] for item in result.errors)
    # 其他文件照常产出：一个坏文件不能拖垮批次
    assert len(result.records) == 10
