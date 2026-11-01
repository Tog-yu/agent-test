"""OCR 归一：适配真实网关的「结构化单据」响应。

背景（2026-11-01 真机实测）：`gateway.pltplt.com/ai/process` 返回的不是 OCR 文本，
而是已经抽好的业务结构：

    {"data": {"list": [{"fileType": "...", "shipper": "...", "chargeList": [...],
                        "ctrList": [...], "hold": {...}}], "orderType": "None"},
     "success": true, "message": "文档处理成功", "error_code": null}

`tests/fixtures/real_gateway_response.json` 是对一份公网 PDF 的真实响应（字段为空，
但结构是一手的）。原来的归一层只在里面找 `pages/text`，结果只抠出 44 个字符。
"""

from __future__ import annotations

import json
from pathlib import Path

from shipping_bill_agent.ocr.normalize import gateway_error, normalize_ocr

FIXTURES = Path(__file__).parent / "fixtures"


def _real() -> dict:
    return json.loads((FIXTURES / "real_gateway_response.json").read_text(encoding="utf-8"))


def _structured() -> dict:
    """按真实结构造一份带内容的响应（键名取真实响应里出现过的）。"""
    return {
        "data": {
            "createDate": "2026-11-01 12:20:00",
            "fileName": "DC-108478.pdf",
            "orderType": "None",
            "list": [
                {
                    "fileType": "DEBIT-NOTE",
                    "invoiceNumber": "DC-108478",
                    "mblNumber": "OOLU4055331281",
                    "shipper": "NEPTUNE SHIPPING LIMITED",
                    "consignee": "PLTPLT PTE LTD",
                    "remark": "TOTAL AMOUNT 1,410.40 CURRENCY USD",
                    "chargeList": [
                        {"chargeName": "HANDLING CHARGE", "amount": "65.00", "currency": "USD"},
                        {"chargeName": "DETENTION", "amount": "1,310.40", "currency": "USD"},
                    ],
                    "ctrList": [{"containerNo": "TIIU8975975", "size": "40HQ"}],
                    "hold": {"holdType": "", "holdDate": "", "holdDesc": ""},
                    "suspicious": "N",
                    "isDetention": "N",
                    "isDemurrage": "N",
                    "page_index": [0, 1],
                }
            ],
            "pages_split": [[0, 1]],
        },
        "success": True,
        "message": "文档处理成功",
        "error_code": None,
        "task_id": None,
    }


def test_real_fixture_shape_is_recognized() -> None:
    """真实夹具能被识别成网关结构化格式，且不把状态字段当正文。"""
    text, blocks, _ = normalize_ocr(_real())
    assert "PAI-COLOADER-NOTICE" in text
    # createDate / success / message 这些不是单据内容
    assert "2026-11-01" not in text
    assert "文档处理成功" not in text
    assert blocks


def test_structured_fields_become_labeled_lines() -> None:
    text, _, _ = normalize_ocr(_structured())
    for expected in (
        "DOCUMENT TYPE: DEBIT-NOTE",
        "INVOICE NO.: DC-108478",
        "MBL NO.: OOLU4055331281",
        "SHIPPER: NEPTUNE SHIPPING LIMITED",
        "CONSIGNEE: PLTPLT PTE LTD",
        "REMARK: TOTAL AMOUNT 1,410.40 CURRENCY USD",
    ):
        assert expected in text


def test_charge_list_becomes_table() -> None:
    _, _, tables = normalize_ocr(_structured())
    charge = next((t for t in tables if t.name == "chargeList"), None)
    assert charge is not None
    assert "chargeName" in charge.header
    assert len(charge.rows) == 2


def test_suspicious_flags_do_not_pollute_text() -> None:
    """suspicious / isDetention 是状态位，不该变成单据正文。"""
    text, _, _ = normalize_ocr(_structured())
    assert "suspicious" not in text.lower()
    assert "isDetention" not in text


def test_empty_gateway_result_is_not_an_error() -> None:
    """字段全空但 success=true：不是失败，只是这份 PDF 不是账单。"""
    assert gateway_error(_real()) is None


def test_success_false_is_reported() -> None:
    raw = {"success": False, "message": "文件下载失败", "error_code": "E4001"}
    assert "文件下载失败" in gateway_error(raw)
    assert "E4001" in gateway_error(raw)
    assert gateway_error({"success": True, "error_code": None}) is None
