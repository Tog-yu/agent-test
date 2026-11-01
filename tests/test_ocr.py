"""OCR 层测试：请求契约、重试策略、磁带回放、形状无关归一。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from shipping_bill_agent.config import OcrSettings, Settings
from shipping_bill_agent.errors import CassetteMissError, OcrError
from shipping_bill_agent.ocr.client import build_payload, call_ocr
from shipping_bill_agent.ocr.normalize import normalize_ocr
from shipping_bill_agent.ocr.providers import (
    CassetteOcrProvider,
    GatewayOcrProvider,
    build_provider,
    cassette_key,
)
from shipping_bill_agent.utils import sha256_file

REQUIRED_KEYS = {"file", "task", "callBack", "taskType"}


def test_payload_matches_contract() -> None:
    payload = build_payload("https://example.com/a.pdf", "orderChange")
    assert set(payload) == REQUIRED_KEYS
    assert payload["callBack"] == ""
    assert payload["taskType"] == ""
    assert payload["task"] == "orderChange"


def test_cassette_hit(samples: Path, settings: Settings) -> None:
    provider = CassetteOcrProvider(settings)
    raw = provider.read(samples / "CN-202609-007.png", task="orderChange")
    assert raw["code"] == 0
    assert "pages" in raw["data"]
    assert provider.hits == 1 and provider.misses == 0


def test_cassette_miss_is_explicit(samples: Path, tmp_path: Path) -> None:
    settings = Settings(
        ocr=OcrSettings(mode="cassette", cassette_dir=tmp_path / "empty")
    )
    provider = CassetteOcrProvider(settings)
    with pytest.raises(CassetteMissError) as excinfo:
        provider.read(samples / "CN-202609-007.png", task="orderChange")
    assert excinfo.value.code == "E_OCR_CASSETTE_MISS"


def test_cassette_key_is_hash_and_task(samples: Path) -> None:
    digest = sha256_file(samples / "CN-202609-007.png")
    assert cassette_key(digest, "orderChange") == f"{digest[:16]}__orderChange.json"


def test_gateway_requires_public_base(samples: Path) -> None:
    settings = Settings(ocr=OcrSettings(mode="gateway", public_base=""))
    provider = GatewayOcrProvider(settings)
    with pytest.raises(OcrError) as excinfo:
        provider.read(samples / "CN-202609-007.png", task="orderChange")
    assert "SBA_OCR_PUBLIC_BASE" in str(excinfo.value)


def test_build_provider_modes() -> None:
    assert build_provider(Settings(ocr=OcrSettings(mode="gateway"))).name == "gateway"
    assert build_provider(Settings(ocr=OcrSettings(mode="cassette"))).name == "cassette"
    assert build_provider(Settings(ocr=OcrSettings(mode="stub"))).name == "stub"


class _FlakyHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    failures: int = 2

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(body)
        if len(type(self).calls) <= type(self).failures:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"busy")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"code": 0, "data": {"pages": [{"page": 1, "text": "OK"}]}}).encode())

    def log_message(self, *args) -> None:  # noqa: D102
        return


@pytest.fixture()
def flaky_server():
    _FlakyHandler.calls = []
    _FlakyHandler.failures = 2
    server = HTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/ai/process"
    server.shutdown()
    thread.join(timeout=2)


def test_retry_on_5xx_then_success(flaky_server: str) -> None:
    settings = Settings(
        ocr=OcrSettings(
            mode="gateway", url=flaky_server, retries=3, connect_timeout=2, read_timeout=5
        )
    )
    result = call_ocr("https://example.com/bill.pdf", settings=settings)
    assert result["code"] == 0
    assert len(_FlakyHandler.calls) == 3
    assert set(_FlakyHandler.calls[0]) == REQUIRED_KEYS


def test_4xx_not_retried(flaky_server: str) -> None:
    class _404Handler(_FlakyHandler):
        def do_POST(self) -> None:  # noqa: N802
            _FlakyHandler.calls.append({"hit": True})
            body = b"bad request"
            self.send_response(400)
            # 必须带 Content-Length：否则 urllib3 会认为响应被截断而重发一次，
            # 让「4xx 不重试」这条断言看起来像实现有问题
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _404Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = Settings(
            ocr=OcrSettings(
                mode="gateway",
                url=f"http://127.0.0.1:{server.server_port}/ai/process",
                retries=3,
                connect_timeout=2,
                read_timeout=3,
            )
        )
        with pytest.raises(OcrError):
            call_ocr("https://example.com/bill.pdf", settings=settings)
        assert len(_FlakyHandler.calls) == 1
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_normalize_handles_nested_and_unknown_shapes() -> None:
    raw = {
        "result": {
            "response": {
                "pages": [
                    {"page": 1, "text": "Line A\nLine B", "tables": [[["Desc", "Amount"], ["THC", "15.00"]]]},
                ]
            }
        }
    }
    text, blocks, tables = normalize_ocr(raw)
    assert "Line A" in text and "Line B" in text
    assert tables and tables[0].header == ["Desc", "Amount"]
    assert tables[0].rows == [["THC", "15.00"]]
    assert any(block.page == 1 for block in blocks)


def test_normalize_falls_back_to_string_collection() -> None:
    raw = {"status": "ok", "data": {"lines": ["Handling Fee 40.00", "Total 40.00"], "code": 0}}
    text, blocks, tables = normalize_ocr(raw)
    assert "Handling Fee 40.00" in text
    assert "status" not in text
    assert blocks and not tables


def test_normalize_preserves_column_spacing() -> None:
    raw = {"data": {"pages": [{"page": 1, "text": "Handling Fee       USD    40.00"}]}}
    text, blocks, _ = normalize_ocr(raw)
    assert blocks[0].text.count("  ") >= 1
