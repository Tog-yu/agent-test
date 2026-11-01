"""OCR 网关契约测试：用本地 mock 服务验证「确实按题面给的代码在调」。

题目原文：
    data = {"file": file_url, "task": "orderChange", "callBack": "", "taskType": ""}
    response = requests.post(url, json=data, timeout=(10, 300))
    response.raise_for_status()
    ocr_result = response.json()

真实网关 gateway.pltplt.com 在本机 TCP 不可达（DNS 能解析、连接超时），
所以这里用 127.0.0.1 的 mock 服务验证请求形状与重试语义，
端到端那一条（`test_ocr_text_reaches_extraction`）验证 OCR 文本真的进了抽取链路。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from shipping_bill_agent.config import Settings
from shipping_bill_agent.errors import OcrError
from shipping_bill_agent.ocr.client import build_payload, call_ocr

EXPECTED_KEYS = ["file", "task", "callBack", "taskType"]


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []
    ocr_text: str = ""

    def log_message(self, *args: object) -> None:  # 静音
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode() or "{}")
        type(self).received.append(
            {
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "payload": payload,
                "keys": list(payload.keys()),
            }
        )
        url = str(payload.get("file", ""))

        if "retry-then-ok" in url:
            seen = sum(1 for item in type(self).received if item["payload"].get("file") == url)
            if seen < 3:
                self.send_response(503)
                self.end_headers()
                return
        if "always-500" in url:
            self.send_response(500)
            self.end_headers()
            return
        if "bad-400" in url:
            self.send_response(400)
            self.end_headers()
            return
        if "not-json" in url:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"<html>oops</html>")
            return

        body = {"code": 0, "data": {"pages": [{"page": 1, "text": type(self).ocr_text}]}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


@pytest.fixture()
def mock_gateway(monkeypatch):
    # 本机若设了 HTTP_PROXY，requests 连 127.0.0.1 也会走代理（实测返回 502），
    # 这里在测试内清掉代理变量，保证打的是本地 mock
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    _Handler.received = []
    _Handler.ocr_text = ""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/ai/process"
    try:
        yield url, _Handler
    finally:
        server.shutdown()
        server.server_close()


def _settings(url: str, **overrides) -> Settings:
    return Settings.load(ocr__url=url, ocr__retries=2, **overrides)


def test_payload_matches_spec_keys_and_order() -> None:
    payload = build_payload("https://bucket.example.com/bill.pdf", "orderChange")
    assert list(payload.keys()) == EXPECTED_KEYS
    assert payload == {
        "file": "https://bucket.example.com/bill.pdf",
        "task": "orderChange",
        "callBack": "",
        "taskType": "",
    }


def test_request_shape_and_timeout(mock_gateway) -> None:
    url, handler = mock_gateway
    settings = _settings(url)
    assert settings.ocr.timeout == (10.0, 300.0)
    assert settings.ocr.task == "orderChange"

    result = call_ocr("https://bucket.example.com/bill.pdf", settings=settings)
    assert isinstance(result, dict)

    call = handler.received[-1]
    assert call["path"] == "/ai/process"
    assert call["content_type"] == "application/json"
    assert call["keys"] == EXPECTED_KEYS
    assert call["payload"]["task"] == "orderChange"
    assert call["payload"]["callBack"] == ""
    assert call["payload"]["taskType"] == ""


def test_5xx_is_retried_then_succeeds(mock_gateway) -> None:
    url, handler = mock_gateway
    call_ocr("https://bucket.example.com/retry-then-ok.pdf", settings=_settings(url))
    hits = [i for i in handler.received if "retry-then-ok" in str(i["payload"]["file"])]
    assert len(hits) == 3  # 两次 503 + 一次成功


def test_persistent_5xx_gives_up_after_retries(mock_gateway) -> None:
    url, handler = mock_gateway
    with pytest.raises(OcrError) as excinfo:
        call_ocr("https://bucket.example.com/always-500.pdf", settings=_settings(url))
    hits = [i for i in handler.received if "always-500" in str(i["payload"]["file"])]
    assert len(hits) == 3  # 1 + retries(2)
    assert excinfo.value.code == "E_OCR_FAILED"


def test_4xx_is_not_retried(mock_gateway) -> None:
    """4xx 是契约错误，重试只会浪费 300s 的超时预算。"""
    url, handler = mock_gateway
    with pytest.raises(OcrError):
        call_ocr("https://bucket.example.com/bad-400.pdf", settings=_settings(url))
    hits = [i for i in handler.received if "bad-400" in str(i["payload"]["file"])]
    assert len(hits) == 1


def test_non_json_response_raises(mock_gateway) -> None:
    url, _ = mock_gateway
    with pytest.raises(OcrError) as excinfo:
        call_ocr("https://bucket.example.com/not-json.pdf", settings=_settings(url))
    assert "JSON" in str(excinfo.value)


def _scan_only_pdf(target: Path) -> Path:
    """造一份只有图片、没有文本层的 PDF（模拟扫描件）。"""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = target.with_suffix(".png")
    Image.new("RGB", (1000, 1400), "white").save(image)
    pdf = canvas.Canvas(str(target))
    pdf.drawImage(ImageReader(str(image)), 0, 0, width=595, height=842)
    pdf.save()
    return target


def test_ocr_text_reaches_extraction_for_scan_only_pdf(mock_gateway, tmp_path: Path) -> None:
    """扫描件（无文本层）端到端：网关返回的文本要真被抽成费用记录。

    「扫描件」是 OCR 当**主源**的场景 —— 没有文本层可依赖，只能用 OCR。
    """
    from shipping_bill_agent.pipeline import run

    url, handler = mock_gateway
    handler.ocr_text = (
        "DEBIT NOTE\n"
        "D/C No. : DC-OCR-9001\n"
        "Issued by: GATEWAY OCR LINES LTD\n"
        "D/C DATE 10-24-2026\n"
        "Charge              Amount   Currency\n"
        "Handling Fee         65.00      USD\n"
        "Detention           100.00      USD\n"
        "TOTAL AMOUNT        165.00   CURRENCY USD\n"
    )
    settings = Settings.load(
        ocr__mode="gateway",
        ocr__url=url,
        ocr__public_base="https://bucket.example.com/files",
        pipeline__accounting_entity="PltPlt",
    )
    scan = _scan_only_pdf(tmp_path / "scan.pdf")
    result = run([scan], settings=settings, out_dir=tmp_path / "out")

    assert handler.received, "应该调用过网关"
    assert handler.received[0]["payload"]["file"] == "https://bucket.example.com/files/scan.pdf"
    assert len(result.records) == 2
    refs = {r["document_info"]["reference_no"] for r in result.records}
    assert refs == {"DC-OCR-9001"}
    amounts = sorted(r["charge_details"]["amount"] for r in result.records)
    assert amounts == [65.0, 100.0]
    assert not result.errors  # OCR 成功，不该有回退或失败记录

    trace = json.loads(next((tmp_path / "out").glob("trace-*.json")).read_text("utf-8"))
    assert trace["files"][0]["ocr_call"] is True


def test_text_layer_wins_when_pdf_has_one(mock_gateway, tmp_path: Path, samples: Path) -> None:
    """有文本层的 PDF：OCR 仍被调用（题目要求 PDF 走 OCR），但不覆盖文本层结果。

    实测依据：同一份 Debit Note，文本层的 单号/币种/明细粒度 都比 OCR 完整，
    让 OCR 覆盖会丢单号（T1 匹配的必需键）。详见 docs/REAL_DATA_NOTES.md §9。
    """
    from shipping_bill_agent.pipeline import run

    url, handler = mock_gateway
    handler.ocr_text = (
        "DEBIT NOTE\n"
        "Charge              Amount   Currency\n"
        "Handling Fee         65.00      USD\n"
        "TOTAL AMOUNT         65.00   CURRENCY USD\n"
    )
    settings = Settings.load(
        ocr__mode="gateway",
        ocr__url=url,
        ocr__public_base="https://bucket.example.com/files",
        pipeline__accounting_entity="PltPlt",
    )
    result = run(
        [samples / "DN-202609-001.pdf"], settings=settings, out_dir=tmp_path / "out"
    )

    # 网关确实被调用了（题目要求「PDF 通过 OCR 识别」）
    assert handler.received
    # 但结果来自文本层：3 条明细 + 原单号，而不是 mock 返回的 1 条 DC-OCR-9001
    assert len(result.records) == 3
    assert {r["document_info"]["reference_no"] for r in result.records} == {"DN-202609-001"}
    trace = json.loads(next((tmp_path / "out").glob("trace-*.json")).read_text("utf-8"))
    assert any("以文本层为主源" in note for note in trace["notes"])


def test_missing_public_base_fails_loudly(mock_gateway, samples: Path, tmp_path: Path) -> None:
    """gateway 模式没配 public_base 时必须明确报错，而不是把本地路径当 URL 发出去。"""
    from shipping_bill_agent.pipeline import run

    url, handler = mock_gateway
    settings = Settings.load(
        ocr__mode="gateway", ocr__url=url, ocr__public_base="", pipeline__pdf_strategy="ocr"
    )
    result = run([samples / "DN-202609-001.pdf"], settings=settings, out_dir=tmp_path / "out")
    assert not handler.received, "不该发出请求"
    # 有文本层 → 回退成功，但 trace 必须说明 OCR 没跑成
    trace = json.loads(next((tmp_path / "out").glob("trace-*.json")).read_text("utf-8"))
    assert any("回退文本层" in note for note in trace["files"][0]["notes"])
