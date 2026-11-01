"""OCR 网关客户端：严格按题目给出的契约调用，含重试与超时。"""

from __future__ import annotations

import time
from typing import Any

import requests

from ..config import Settings
from ..errors import OcrError

RETRY_STATUS = {429, 500, 502, 503, 504}


def build_payload(file_url: str, task: str) -> dict[str, str]:
    """题目给定请求体，键名与顺序保持原样。"""
    return {"file": file_url, "task": task, "callBack": "", "taskType": ""}


def call_ocr(
    file_url: str,
    *,
    settings: Settings,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """调用 OCR 网关。返回原始 JSON（不做形状假设）。"""
    payload = build_payload(file_url, settings.ocr.task)
    timeout = settings.ocr.timeout
    attempts = max(0, settings.ocr.retries) + 1
    owns_session = session is None
    http = session or requests.Session()
    last_error: str = ""

    try:
        for attempt in range(attempts):
            try:
                response = http.post(settings.ocr.url, json=payload, timeout=timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == attempts - 1:
                    break
                time.sleep(min(2 ** attempt, 8))
                continue

            # 题面用 response.raise_for_status()：这里保留同样的语义，
            # 只是把 5xx/429 让给重试、4xx 直接判契约错误（重试无意义）
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                status = response.status_code
                if status in RETRY_STATUS and attempt < attempts - 1:
                    last_error = f"HTTP {status}: {response.text[:200]}"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise OcrError(
                    f"OCR 网关返回 {status}: {response.text[:300]}",
                    code="E_OCR_FAILED",
                ) from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise OcrError(f"OCR 响应不是合法 JSON: {exc}", code="E_OCR_FAILED") from exc

            if not isinstance(data, dict):
                raise OcrError("OCR 响应顶层不是对象", code="E_OCR_FAILED")
            return data
    finally:
        if owns_session:
            http.close()

    raise OcrError(f"OCR 网关调用失败（已重试 {attempts} 次）: {last_error}", code="E_OCR_FAILED")
