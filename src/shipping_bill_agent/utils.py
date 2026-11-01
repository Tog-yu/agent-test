"""通用小工具：哈希、HTML 转文本、文本清洗。"""

from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from pathlib import Path

_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def preserve_lines(text: str) -> list[str]:
    """按行切分但**保留行内空白**。

    PDF 文本层与 OCR 输出常靠多空格对齐列；一旦压成单空格，
    「按列切分」就失效，费用名会把币种/数量一起吞进去。所以块级文本必须保留原间距。
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return [line for line in lines if line.strip()]


class _HtmlToText(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    CELL_TAGS = {"td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in self.CELL_TAGS:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data.strip():
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = html.unescape(raw)
        lines = []
        for line in raw.split("\n"):
            cleaned = line.strip().strip("|").strip()
            cleaned = re.sub(r"\s*\|\s*", " | ", cleaned).strip()
            if cleaned:
                lines.append(cleaned)
        return clean_text("\n".join(lines))


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    parser = _HtmlToText()
    try:
        parser.feed(raw)
        return parser.get_text()
    except Exception:  # noqa: BLE001 - HTML 千奇百怪，兜底正则
        stripped = re.sub(r"<[^>]+>", " ", raw)
        return clean_text(html.unescape(stripped))


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name or "attachment").strip(" .")
    return cleaned[:120] or "attachment"
