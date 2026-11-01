"""边界与回归：这一轮 code review 暴露的每一类问题各留一条用例。

对应报告 docs/CODE_REVIEW_v1.1.0.md 的编号：
A1 金额空格千分位 / A2 重复行保留标疑 / A3 空币种不覆盖 / A4 接收方推断 /
A5 匹配键归一 / A6 单据级字段继承 / S2 币种误判 / S3 一格多费用 / S4 总额误判 /
S7 输入路径不存在 / S8 抽不到费用要可见
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_entry

from shipping_bill_agent.config import Settings
from shipping_bill_agent.decide.planner import plan
from shipping_bill_agent.extract.rule_extractor import TOTAL_PATTERNS, _find_money_line, _find_total
from shipping_bill_agent.matching.store import normalize_key
from shipping_bill_agent.normalize.amounts import parse_amount
from shipping_bill_agent.normalize.charge_map import normalize_charge
from shipping_bill_agent.normalize.currency import normalize_currency
from shipping_bill_agent.pipeline import run
from shipping_bill_agent.schema import MatchInfo

SETTINGS = Settings()

BILL = {
    "document_type": "Debit Note",
    "reference_no": "DN-202609-001",
    "document_date": "2026-09-20",
    "issuer": "K Line Logistics Ltd",
    "recipient": "Origin Agent",
    "charge_item": "Handling Charge",
    "charge_item_raw": "Handling Fee",
    "amount": 40.0,
    "currency": "USD",
    "payment_term": "PP",
    "quantity": None,
    "unit_price": None,
    "tax": None,
}


# ---------------------------------------------------------------- A1 金额


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234.56", 1234.56),
        ("12 345.67", 12345.67),
        ("2 400,00", 2400.0),
        ("USD 1 410.40", 1410.4),
        ("20 000", 20000.0),
        ("(1 234.56)", -1234.56),
        ("1,234.56", 1234.56),
    ],
)
def test_a1_space_thousands_are_not_truncated(raw: str, expected: float) -> None:
    value, _ = parse_amount(raw)
    assert value == pytest.approx(expected)


def test_a1_ambiguous_grouping_returns_none_instead_of_guessing() -> None:
    # "1 23.45" 既可能是 123.45 也可能是一个 1 加一个 23.45 → 不猜
    assert parse_amount("1 23.45") == (None, 0.0)


def test_a1_end_to_end_xlsx_cell_with_space_thousands(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Debit Note No.", "DC-202609-777"])
    sheet.append(["Charge", "Amount", "Currency"])
    sheet.append(["Handling Fee", "1 234.56", "USD"])
    source = tmp_path / "space_amount.xlsx"
    book.save(source)

    result = run([source], settings=SETTINGS, out_dir=tmp_path / "out")
    assert result.records[0]["charge_details"]["amount"] == pytest.approx(1234.56)


# ---------------------------------------------------------------- A2 重复行


def test_a2_duplicate_lines_are_kept_and_flagged(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Debit Note No.", "DC-202609-778"])
    sheet.append(["Charge", "Amount", "Currency"])
    sheet.append(["THC", 40.0, "USD"])
    sheet.append(["THC", 40.0, "USD"])  # 两个箱子各收一次
    source = tmp_path / "dup.xlsx"
    book.save(source)

    result = run([source], settings=SETTINGS, out_dir=tmp_path / "out")
    assert len(result.records) == 2, "两行都必须保留，钱不能凭空消失"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    codes = [i["code"] for r in report["records"] for i in r["issues"]]
    assert "V-DUP_LINE" in codes


# ---------------------------------------------------------------- A3 空币种


def test_a3_missing_currency_does_not_null_out_existing_value(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Debit Note No.", "DN-202609-001"])
    sheet.append(["Charge", "Amount"])  # 没有币种列
    sheet.append(["Handling Fee", 40.0])
    source = tmp_path / "no_currency.xlsx"
    book.save(source)
    bills = tmp_path / "bills.json"
    bills.write_text(json.dumps([{**BILL, "bill_id": "B-1"}]), encoding="utf-8")

    result = run([source], settings=SETTINGS, out_dir=tmp_path / "out", bills_path=bills)
    record = result.records[0]
    assert record["charge_details"]["currency"] == "USD", "应继承历史账单币种，而不是被 null 覆盖"


# ---------------------------------------------------------------- A4 接收方推断


def test_a4_remark_line_does_not_make_entity_the_recipient(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Debit Note No.", "DC-202609-780"])
    sheet.append(["Issued by:", "ABC Lines Pte Ltd"])
    sheet.append(["Charge", "Amount", "Currency"])
    sheet.append(["Handling Fee", 40.0, "USD"])
    sheet.append(["Remarks: destination charges payable by PLTPLT PTE LTD on delivery."])
    source = tmp_path / "remark.xlsx"
    book.save(source)

    settings = SETTINGS.with_overrides(pipeline__accounting_entity="PLTPLT PTE LTD")
    result = run([source], settings=settings, out_dir=tmp_path / "out")
    record = result.records[0]
    assert record["transaction_party"]["recipient"] is None, "备注句不能证明我方是接收方"
    assert record["financial_attributes"]["posting_type"] is None
    assert record["operation"] == "PENDING_REVIEW"


def test_a4_inferred_recipient_never_auto_posts(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Debit Note No.", "DC-202609-781"])
    sheet.append(["Issued by:", "ABC Lines Pte Ltd"])
    sheet.append(["Notify party: PLTPLT PTE LTD"])  # 非备注行，会走推断
    sheet.append(["Charge", "Amount", "Currency"])
    sheet.append(["Handling Fee", 40.0, "USD"])
    source = tmp_path / "inferred.xlsx"
    book.save(source)

    settings = SETTINGS.with_overrides(pipeline__accounting_entity="PLTPLT PTE LTD")
    result = run([source], settings=settings, out_dir=tmp_path / "out")
    record = result.records[0]
    assert record["operation"] == "PENDING_REVIEW"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    codes = [i["code"] for r in report["records"] for i in r["issues"]]
    assert "F-PARTY_INFERRED" in codes


# ---------------------------------------------------------------- A5 匹配键


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("DN-202609-001", "DN/202609/001"),
        ("DN-202609-001", "dn 202609 001"),
        ("DC_108478", "DC-108478"),
    ],
)
def test_a5_reference_separators_are_folded(left: str, right: str) -> None:
    assert normalize_key(left) == normalize_key(right)


# ---------------------------------------------------------------- A6 单据级字段继承


def test_a6_issuer_is_not_overwritten_on_matched_bill() -> None:
    entry = make_entry(amount=40.0)
    entry.issuer = "Destination Agent"
    entry.match = MatchInfo(tier="T1", bill_id="B-1")
    plan(entry, original=dict(BILL), settings=SETTINGS)
    assert entry.issuer == "K Line Logistics Ltd", "交易主体不能被子据静默覆盖"
    assert any(i.code == "P-DOC_FIELD_DIFF" for i in entry.issues)


# ---------------------------------------------------------------- 小问题


def test_s2_common_words_are_not_currencies() -> None:
    assert normalize_currency("ALL CHARGES PREPAID")[0] is None
    assert normalize_currency("XXX")[0] is None
    assert normalize_currency("TRY")[0] is None
    assert normalize_currency("TOTAL AMOUNT 1,410.40 CURRENCY USD")[0] == "USD"


def test_s3_merged_charge_names_are_not_guessed() -> None:
    name, conf, method = normalize_charge("THC + DOC FEE")
    assert method == "ambiguous"
    assert name == "THC + DOC FEE"
    assert conf < 0.5


def test_s4_total_quantity_is_not_a_total_amount() -> None:
    assert _find_total(["TOTAL QTY 4"]) .value is None
    assert _find_total(["TOTAL CHARGES USD 40.00"]).value == pytest.approx(40.0)
    # 旧的通用匹配仍会命中 QTY 行——所以 total 必须走 _find_total
    assert _find_money_line(["TOTAL QTY 4"], TOTAL_PATTERNS).value == pytest.approx(4.0)


def test_s7_missing_input_is_reported(tmp_path: Path) -> None:
    result = run([tmp_path / "nope.xlsx"], settings=SETTINGS, out_dir=tmp_path / "out")
    assert any("E_INPUT_NOT_FOUND" in item["error"] for item in result.errors)


def test_s8_file_without_charges_is_visible(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Sheet with nothing billable"])
    source = tmp_path / "empty.xlsx"
    book.save(source)

    result = run([source], settings=SETTINGS, out_dir=tmp_path / "out")
    assert result.records == []
    assert any("E_NO_CHARGE" in item["error"] for item in result.errors)


# ---------------------------------------------------------------------------
# 邮件三件套：正文 / 附件 / 内嵌图。真实数据（PRE-ALERT 邮件）暴露的三条。
# ---------------------------------------------------------------------------

def test_default_strategy_sends_pdf_to_ocr(samples: Path) -> None:
    """默认必须把 PDF 送 OCR —— 题目明确要求「PDF 和图片通过 OCR API 识别」。"""
    from shipping_bill_agent.ingest.dispatch import ingest
    from shipping_bill_agent.pipeline import _needs_ocr

    assert Settings().pipeline.pdf_strategy == "ocr"
    doc = ingest(samples / "DN-202609-001.pdf", workdir=samples / "tmp_ocr", settings=SETTINGS)
    assert doc.meta["has_text_layer"] is True
    assert _needs_ocr(doc, SETTINGS) is True

    # auto 留给「别浪费 OCR 预算」的场景
    auto = Settings.load(pipeline__pdf_strategy="auto")
    assert _needs_ocr(doc, auto) is False

    # text：从不调 OCR
    text_only = Settings.load(pipeline__pdf_strategy="text")
    assert _needs_ocr(doc, text_only) is False


def test_ocr_failure_falls_back_to_text_layer(samples: Path, tmp_path: Path) -> None:
    """默认走 OCR，但网关失败时不能整批报废：有文本层就回退，且必须留下痕迹。"""
    result = run([samples / "DN-202609-001.pdf"], settings=SETTINGS, out_dir=tmp_path / "out")
    # 回退成功 → 照样出记录，而且不写进 errors.json（不是解析失败）
    assert result.records, "回退文本层后应有记录"
    assert not any("E_OCR_CASSETTE_MISS" in item["error"] for item in result.errors)
    trace = json.loads(
        next((tmp_path / "out").glob("trace-*.json")).read_text(encoding="utf-8")
    )
    assert any("回退文本层" in note for note in trace["files"][0]["notes"])


def test_scanned_pdf_without_text_layer_still_fails_loudly(tmp_path: Path) -> None:
    """没有文本层可回退的扫描件，OCR 失败必须进 errors.json，不能装作成功。"""
    from reportlab.pdfgen import canvas

    source = tmp_path / "blank_scan.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.save()  # 空页，无文本层

    result = run([source], settings=SETTINGS, out_dir=tmp_path / "out")
    assert result.records == []
    assert any("OCR" in item["error"] for item in result.errors)


def test_email_subject_carries_container_number(tmp_path: Path, samples: Path) -> None:
    """邮件标题里的 CNTR NO. 要能抽出来——正文常常只写 MBL NO.，箱子号只在标题里。"""
    from shipping_bill_agent.extract.rule_extractor import extract_by_rules
    from shipping_bill_agent.ingest.dispatch import ingest
    from shipping_bill_agent.schema import DocumentContext

    source = samples / "eml" / "mail_debit_note_001.eml"
    doc = ingest(source, workdir=tmp_path, settings=SETTINGS)
    ctx = DocumentContext(
        subject="PRE-ALERT SSZSE26093128  CNTR NO.: TIIU8975975   MBL NO.: OOLU4055331281",
        sender='"luka@pltplt.com" <luka@pltplt.com>',
        recipient=None,
        date="Tue, 20 Oct 2026 15:46:44 +0800",
        body_text="SHA pay TAR：H/C $50",
        origin="root",
    )
    extraction = extract_by_rules(doc, ctx=ctx, body_charges=True)
    assert extraction.get("container_no").value == "TIIU8975975"
    assert extraction.get("container_no").extractor == "rule:email_subject"
    assert extraction.get("bl_no").value == "OOLU4055331281"


def test_subject_date_is_not_used_as_document_date(tmp_path: Path, samples: Path) -> None:
    """标题里的 "Arrived time 2026-10-30" 是到港时间，不是单据日期。"""
    from shipping_bill_agent.extract.rule_extractor import extract_by_rules
    from shipping_bill_agent.ingest.dispatch import ingest
    from shipping_bill_agent.schema import DocumentContext

    doc = ingest(samples / "eml" / "mail_debit_note_001.eml", workdir=tmp_path, settings=SETTINGS)
    ctx = DocumentContext(
        subject="PRE-ALERT ... Arrived time 2026-10-30",
        sender=None, recipient=None, date=None, body_text="", origin="root",
    )
    extraction = extract_by_rules(doc, ctx=ctx, body_charges=False)
    # 正文自身有日期（2026-09-20），标题里的到港日不得覆盖它
    assert extraction.get("document_date").value == "2026-09-20"


def test_bill_of_lading_is_not_reported_as_parse_failure(tmp_path: Path) -> None:
    """提单本来就没有费用，不能报 E_NO_CHARGE 错误——否则会淹没真正的解析失败。"""
    from reportlab.pdfgen import canvas

    source = tmp_path / "bl.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(72, 760, "BILL OF LADING")
    pdf.drawString(72, 740, "SHIPPER: SHENZHEN XUELANG TRADING CO., LTD.")
    pdf.drawString(72, 720, "B/L NO: OOLU4055331281")
    pdf.drawString(72, 700, "FREIGHT PREPAID")
    pdf.drawString(72, 680, "GROSS WEIGHT 10715.000KGS  MEASUREMENT 68.000CBM")
    pdf.save()

    result = run([source], settings=SETTINGS, out_dir=tmp_path / "out")
    assert result.records == []
    assert not any("E_NO_CHARGE" in item["error"] for item in result.errors)
    trace = json.loads(
        next((tmp_path / "out").glob("trace-*.json")).read_text(encoding="utf-8")
    )
    assert any("提单" in note for note in trace["files"][0]["notes"])


def test_saas_watermark_is_not_a_recipient(tmp_path: Path) -> None:
    """`... Powered by <我方>` 这类 SaaS 水印不能当成「单据开给我方」。

    真实数据踩过：Debit Note 页脚是
        NEPTUNE SHIPPING LIMITED (LAX) Powered by
        湃乐多（上海）航运科技有限公司
        Pilot Aiship Tech co., Ltd.
    把这三行当接收方声明，会让 AR/AP 直接反过来（我方只是系统供应商）。
    """
    from reportlab.pdfgen import canvas

    from shipping_bill_agent.extract.rule_extractor import extract_by_rules
    from shipping_bill_agent.ingest.dispatch import ingest

    source = tmp_path / "dn_watermark.pdf"
    pdf = canvas.Canvas(str(source))
    lines = [
        "DEBIT NOTE",
        "D/C No. : DC-900001",
        "Issued by: NEPTUNE SHIPPING LIMITED",
        "Charge              Amount   Currency",
        "Handling Fee        65.00      USD",
        "TOTAL AMOUNT        65.00   CURRENCY USD",
        "NEPTUNE SHIPPING LIMITED (LAX) Powered by",
        "湃乐多（上海）航运科技有限公司",
        "Pilot Aiship Tech co., Ltd.",
    ]
    y = 760
    for line in lines:
        pdf.drawString(60, y, line)
        y -= 22
    pdf.save()

    doc = ingest(source, workdir=tmp_path / "w", settings=SETTINGS)
    extraction = extract_by_rules(
        doc,
        entity="PltPlt",
        entity_aliases=["湃乐多", "Pilot Aiship"],
    )
    # 签发方仍应抽到
    assert extraction.get("issuer").value == "NEPTUNE SHIPPING LIMITED"
    # 但水印里的公司名不能变成接收方
    assert extraction.get("recipient").value is None
