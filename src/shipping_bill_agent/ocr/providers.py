"""OCR provider 三态：gateway（真实网关）/ cassette（录制回放）/ stub（空实现）。

设计动机：网关需要「OCR 服务端可访问的 URL」，本地文件无法直传；且 300s 超时
让 CI 不可行。cassette 把「真实响应体」按 (sha256, task) 存盘，使离线与 CI
拿到的是与线上同形状的响应。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings
from ..errors import CassetteMissError, OcrError
from ..utils import sha256_file
from .client import call_ocr


def cassette_key(digest: str, task: str) -> str:
    return f"{digest[:16]}__{task}.json"


class OcrProvider(Protocol):
    name: str

    def read(self, path: Path, *, task: str) -> dict[str, Any]: ...


class GatewayOcrProvider:
    name = "gateway"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hits = 0
        self.misses = 0

    def resolve_url(self, path: Path) -> str:
        base = self.settings.ocr.public_base.strip()
        if not base:
            raise OcrError(
                "gateway 模式需要 SBA_OCR_PUBLIC_BASE（OCR 服务端可访问的下载前缀），"
                "或改用 --ocr-mode cassette 做离线回放",
                code="E_OCR_FAILED",
            )
        return f"{base.rstrip('/')}/{path.name}"

    def read(self, path: Path, *, task: str) -> dict[str, Any]:
        url = self.resolve_url(path)
        self.hits += 1
        return call_ocr(url, settings=self.settings)


class CassetteOcrProvider:
    name = "cassette"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dir = Path(settings.ocr.cassette_dir)
        self.hits = 0
        self.misses = 0

    def cassette_path(self, path: Path, task: str) -> Path:
        return self.dir / cassette_key(sha256_file(path), task)

    def read(self, path: Path, *, task: str) -> dict[str, Any]:
        target = self.cassette_path(path, task)
        if not target.exists():
            self.misses += 1
            raise CassetteMissError(
                f"OCR 回放未命中：{target.name}。请切 --ocr-mode gateway 真实调用，"
                f"或把网关响应存为该文件（scripts/make_samples.py 可生成）",
                code="E_OCR_CASSETTE_MISS",
            )
        self.hits += 1
        return json.loads(target.read_text(encoding="utf-8"))

    @staticmethod
    def record(path: Path, task: str, payload: dict[str, Any], cassette_dir: Path) -> Path:
        """录制：把网关原始响应体按 key 存盘（供 gateway 首次成功后落库）。"""
        cassette_dir.mkdir(parents=True, exist_ok=True)
        target = cassette_dir / cassette_key(sha256_file(path), task)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target


class StubOcrProvider:
    """返回空文本的占位实现，用于明确「不调 OCR」的场景。"""

    name = "stub"
    hits = 0
    misses = 0

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    def read(self, path: Path, *, task: str) -> dict[str, Any]:
        self.hits += 1
        return {"code": 0, "msg": "stub", "data": {"pages": [{"page": 1, "text": ""}]}}


def build_provider(settings: Settings) -> OcrProvider:
    mode = (settings.ocr.mode or "cassette").lower()
    if mode == "gateway":
        return GatewayOcrProvider(settings)
    if mode == "stub":
        return StubOcrProvider(settings)
    return CassetteOcrProvider(settings)
