"""生成合成样本、历史账单与 OCR 回放磁带。

说明（重要）：
- 题目提供的百度网盘数据本机无法下载，因此样本为**按题目描述的字段形态合成的数据**。
- OCR 网关（gateway.pltplt.com）本机不可达，因此 ``data/ocr_cassettes`` 里的响应是
  **按题目契约形状构造的离线替身**，用于让管线与测试确定性可跑；切到
  ``--ocr-mode gateway`` 时走真实网关。
"""

from __future__ import annotations

import json
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipping_bill_agent.ocr.providers import cassette_key  # noqa: E402
from shipping_bill_agent.utils import sha256_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
CASSETTES = ROOT / "data" / "ocr_cassettes"
TASK = "orderChange"

COLUMNS = (36, 6, 6, 10, 8, 12)


def row(cells: list[str]) -> str:
    parts = []
    for index, cell in enumerate(cells):
        width = COLUMNS[index] if index < len(COLUMNS) else 12
        if index == 0:
            parts.append(f"{cell:<{width}}")
        elif index in {1, 4}:
            parts.append(f"{cell:<{width}}")
        else:
            parts.append(f"{cell:>{width}}")
    return "  ".join(part.rstrip() if index == len(cells) - 1 else part
                      for index, part in enumerate(parts))


HEADER = row(["Charge Description", "Curr", "Qty", "Unit Price", "Terms", "Amount"])


# ---------------------------------------------------------------- PDF


def make_pdf(path: Path, lines: list[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    _, height = A4
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(path.stem)
    y = height - 60
    for line in lines:
        font = "Courier" if line.startswith(("Charge", "Handling", "Terminal", "Doc", "THC",
                                             "Trucking", "Customs", "Storage", "Subtotal",
                                             "Total")) else "Helvetica"
        pdf.setFont(font, 10)
        pdf.drawString(40, y, line)
        y -= 18
    pdf.showPage()
    pdf.save()


PDF_001_LINES = [
    "DEBIT NOTE",
    "",
    "Issued by: Destination Agent",
    "To: Origin Agent",
    "Debit Note No: DN-202609-001",
    "Date: 2026-09-20",
    "B/L No: COSU6384910020",
    "Booking No: BK20260915001",
    "Container No: CSQU3054383",
    "",
    HEADER,
    " " + row(["Handling Fee", "USD", "1", "40.00", "PP", "40.00"]),
    " " + row(["Terminal Handling Charge", "USD", "1", "120.00", "PP", "120.00"]),
    " " + row(["Doc Fee", "USD", "1", "35.00", "PP", "35.00"]),
    "",
    "Subtotal  195.00",
    "Total  195.00 USD",
    "Payment Terms: PP",
    "",
    "Remarks: destination charges payable by origin agent.",
]


# ---------------------------------------------------------------- Excel


def make_excel(path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Charges"
    sheet.append(["Invoice No", "INV-202609-050"])
    sheet.append(["Issued by", "Destination Agent"])
    sheet.append(["To", "Origin Agent"])
    sheet.append(["Invoice Date", "2026-09-25"])
    sheet.append(["B/L No", "COSU6384910021"])
    sheet.append([])
    sheet.append(["Charge Description", "Currency", "Qty", "Unit Price", "Amount", "Payment Term"])
    sheet.append(["Trucking Fee", "USD", 1, 300.00, 300.00, "CC"])
    sheet.append(["Customs Fee", "USD", 1, 80.00, 80.00, "CC"])
    sheet.append(["Storage Fee", "USD", 1, 25.00, 25.00, "CC"])
    sheet.append([])
    sheet.append(["Total", "", "", "", 405.00, ""])
    workbook.save(path)


# ---------------------------------------------------------------- Word


def make_word(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("DEBIT NOTE", level=1)
    for line in (
        "Issued by: Origin Agent",
        "To: Destination Agent",
        "Debit Note No: DN-202609-002",
        "Date: 2026-10-06",
        "B/L No: COSU6384910022",
    ):
        document.add_paragraph(line)

    table = document.add_table(rows=1, cols=6)
    header = table.rows[0].cells
    for index, text in enumerate(
        ["Charge Description", "Currency", "Qty", "Unit Price", "Amount", "Payment Term"]
    ):
        header[index].text = text

    for item in (
        ("Handling Fee", "US$", "1", "50.00", "50.00", "PP"),
        ("Trucking Fee", "US$", "1", "280.00", "280.00", "PP"),
    ):
        cells = table.add_row().cells
        for index, text in enumerate(item):
            cells[index].text = text

    document.add_paragraph("Total  330.00 US$")
    document.add_paragraph("Payment Terms: PP")
    document.save(str(path))


# ---------------------------------------------------------------- 图片


def make_image(path: Path, lines: list[str]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1000, 120 + 26 * len(lines)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 15)
    except OSError:  # pragma: no cover
        font = ImageFont.load_default()
    y = 28
    for line in lines:
        draw.text((36, y), line, fill="black", font=font)
        y += 26
    image.save(path)


CN_007_LINES = [
    "CREDIT NOTE",
    "Issued by: Origin Agent",
    "To: Destination Agent",
    "Credit Note No: CN-202609-007",
    "Date: 2026-10-02",
    "B/L No: COSU6384910031",
    HEADER,
    " " + row(["THC", "USD", "1", "15.00", "PP", "15.00"]),
    "Subtotal  15.00",
    "Total  15.00 USD",
]

DN_003_LINES = [
    "DEBIT NOTE",
    "Issued by: Destination Agent",
    "To: Origin Agent",
    "Debit Note No: DN-202609-003",
    "Date: 2026-10-08",
    HEADER,
    " " + row(["Handling Fee", "USD", "1", "4O.OO", "PP", "4O.OO"]),
    "Total  40.00 USD",
    "Payment Terms: PP",
]


# ---------------------------------------------------------------- EML


def make_eml(path: Path, attachments: list[Path]) -> None:
    message = EmailMessage()
    message["Subject"] = "Debit Note DN-202609-001 - Handling Fee correction"
    message["From"] = "Destination Agent <ops@destination-agent.example>"
    message["To"] = "Origin Agent <billing@origin-agent.example>"
    message["Date"] = "Mon, 21 Sep 2026 09:12:00 +0800"
    message.set_content(
        "Dear Origin Agent,\n"
        "\n"
        "Please find attached our debit note DN-202609-001 dated 2026-09-20 for the\n"
        "destination charges of container CSQU3054383.\n"
        "\n"
        "Note that the handling fee has been corrected from USD 35.00 to USD 40.00.\n"
        "Amounts are payable PP. Kindly update your records and confirm.\n"
        "\n"
        "Best regards,\n"
        "Destination Agent - Documentation Team\n"
    )
    for attachment in attachments:
        subtype = "pdf" if attachment.suffix.lower() == ".pdf" else "octet-stream"
        maintype = "application"
        message.add_attachment(
            attachment.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )
    path.write_bytes(message.as_bytes())


# ---------------------------------------------------------------- 历史账单


EXISTING_BILLS = [
    {
        "bill_id": "B-1001",
        "document_type": "Debit Note",
        "reference_no": "DN-202609-001",
        "document_date": "2026-09-20",
        "issuer": "Destination Agent",
        "recipient": "Origin Agent",
        "bl_no": "COSU6384910020",
        "booking_no": "BK20260915001",
        "container_no": "CSQU3054383",
        "charge_item": "Handling Charge",
        "charge_item_raw": "Handling Fee",
        "amount": 35.00,
        "currency": "USD",
        "payment_term": "PP",
        "accounting_entity": "Origin Agent",
        "posting_type": "AP",
    },
    {
        "bill_id": "B-1002",
        "document_type": "Debit Note",
        "reference_no": "DN-202609-001",
        "document_date": "2026-09-20",
        "issuer": "Destination Agent",
        "recipient": "Origin Agent",
        "bl_no": "COSU6384910020",
        "booking_no": "BK20260915001",
        "charge_item": "Terminal Handling Charge",
        "charge_item_raw": "THC",
        "amount": 120.00,
        "currency": "USD",
        "payment_term": "PP",
        "accounting_entity": "Origin Agent",
        "posting_type": "AP",
    },
    {
        "bill_id": "B-1003",
        "document_type": "Debit Note",
        "reference_no": "DN-202609-001",
        "document_date": "2026-09-20",
        "issuer": "Destination Agent",
        "recipient": "Origin Agent",
        "bl_no": "COSU6384910020",
        "booking_no": "BK20260915001",
        "charge_item": "Documentation Fee",
        "charge_item_raw": "Doc Fee",
        "amount": 35.00,
        "currency": "USD",
        "payment_term": "PP",
        "accounting_entity": "Origin Agent",
        "posting_type": "AP",
    },
    {
        "bill_id": "B-1004",
        "document_type": "Invoice",
        "reference_no": "INV-202609-050",
        "document_date": "2026-09-25",
        "issuer": "Destination Agent",
        "recipient": "Origin Agent",
        "bl_no": "COSU6384910021",
        "charge_item": "Trucking Charge",
        "charge_item_raw": "Trucking Fee",
        "amount": 300.00,
        "currency": "USD",
        "payment_term": "CC",
        "accounting_entity": "Origin Agent",
        "posting_type": "AP",
    },
    {
        "bill_id": "B-1005",
        "document_type": "Debit Note",
        "reference_no": "DN-202609-002",
        "document_date": "2026-10-06",
        "issuer": "Origin Agent",
        "recipient": "Destination Agent",
        "bl_no": "COSU6384910022",
        "charge_item": "Handling Charge",
        "charge_item_raw": "Handling Fee",
        "amount": 50.00,
        "currency": "USD",
        "payment_term": "PP",
        "accounting_entity": "Origin Agent",
        "posting_type": "AR",
    },
]


# ---------------------------------------------------------------- 磁带


def write_cassette(image_path: Path, text: str) -> Path:
    CASSETTES.mkdir(parents=True, exist_ok=True)
    payload = {
        "code": 0,
        "msg": "success",
        "_fixture_note": (
            "离线替身：按题目 OCR 契约响应的形状构造，非真实网关返回。"
            "真实链路请用 --ocr-mode gateway + SBA_OCR_PUBLIC_BASE。"
        ),
        "data": {
            "task": TASK,
            "file": image_path.name,
            "pages": [{"page": 1, "text": text}],
        },
    }
    target = CASSETTES / cassette_key(sha256_file(image_path), TASK)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


LEGACY_LINES = [
    "Debit Note No. : DC-202609-901",
    "Issued by : Legacy Feeder Co., Ltd.",
    "Date : 2026-09-18",
    "",
    "Charge\tAmount\tCurrency",
    "THC\t120.00\tUSD",
    "Doc Fee\t35.00\tUSD",
]


def legacy_xls(path: Path) -> None:
    """真实 BIFF .xls（xlwt 生成），验证旧版 Excel 能被解析而不是被拒绝。"""
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("Charges")
    rows = [
        ["Debit Note No.", "DC-202609-900"],
        ["Issued by", "Legacy Feeder Co., Ltd."],
        ["Date", "2026-09-18"],
        [],
        ["Charge", "Amount", "Currency"],
        ["THC", 120.0, "USD"],
        ["Doc Fee", 35.0, "USD"],
    ]
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            sheet.write(row_index, col_index, value)
    book.save(str(path))


def legacy_doc(path: Path) -> bool:
    """真实 Word 97 .doc。用 macOS 自带的 textutil 生成；没有则跳过（返回 False）。"""
    import shutil
    import subprocess

    textutil = shutil.which("textutil")
    if not textutil:
        return False
    plain = path.with_suffix(".txt")
    plain.write_text("\n".join(LEGACY_LINES), encoding="utf-8")
    subprocess.run(
        [textutil, "-convert", "doc", str(plain), "-output", str(path)], check=False
    )
    plain.unlink(missing_ok=True)
    return path.exists()


def unsupported_file(path: Path) -> None:
    """一个真正的非支持格式（Outlook .msg 的 CFB 头），用于验证批次容错。"""
    path.write_bytes(b"\xcf\xfb\x1a\xe4" + b"Outlook Message\x00" + b"\x00" * 256)


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    (SAMPLES / "eml").mkdir(parents=True, exist_ok=True)
    (SAMPLES / "legacy").mkdir(parents=True, exist_ok=True)

    pdf_path = SAMPLES / "DN-202609-001.pdf"
    make_pdf(pdf_path, PDF_001_LINES)

    make_excel(SAMPLES / "INV-202609-050.xlsx")
    make_word(SAMPLES / "DN-202609-002.docx")

    make_image(SAMPLES / "CN-202609-007.png", CN_007_LINES)
    make_image(SAMPLES / "DN-202609-003.png", DN_003_LINES)

    write_cassette(SAMPLES / "CN-202609-007.png", "\n".join(CN_007_LINES))
    write_cassette(SAMPLES / "DN-202609-003.png", "\n".join(DN_003_LINES))

    make_eml(SAMPLES / "eml" / "mail_debit_note_001.eml", [pdf_path])

    # 旧格式样本单独放在 legacy/ 子目录：不参与默认批次（金标按原样本集固定），
    # 由 tests/test_legacy_formats.py 显式跑。
    legacy_xls(SAMPLES / "legacy" / "legacy_rates.xls")
    made_doc = legacy_doc(SAMPLES / "legacy" / "legacy_debit_note.doc")
    unsupported_file(SAMPLES / "unsupported.msg")

    bills_path = ROOT / "data" / "existing_bills.json"
    bills_path.write_text(json.dumps(EXISTING_BILLS, ensure_ascii=False, indent=2), encoding="utf-8")

    print("已生成样本：")
    for path in sorted(SAMPLES.rglob("*")):
        if path.is_file():
            print(f"  - {path.relative_to(ROOT)}")
    print(f"已生成历史账单：{bills_path.relative_to(ROOT)}")
    print(f"已生成 OCR 磁带：{len(list(CASSETTES.glob('*.json')))} 个")
    if not made_doc:
        print("提示：本机没有 textutil，跳过 .doc 样本（.doc 解析仍可用 soffice/antiword）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
