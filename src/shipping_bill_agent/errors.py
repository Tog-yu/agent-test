"""统一异常类型。"""

from __future__ import annotations


class ShippingBillError(Exception):
    """基础异常，携带机器可判的错误码。"""

    code = "E_UNKNOWN"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class UnsupportedFormatError(ShippingBillError):
    code = "E_UNSUPPORTED_DOC"


class OcrError(ShippingBillError):
    code = "E_OCR_FAILED"


class CassetteMissError(OcrError):
    code = "E_OCR_CASSETTE_MISS"
