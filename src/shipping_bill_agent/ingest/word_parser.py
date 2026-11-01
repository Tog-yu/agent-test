"""Word 解析：docx 用 python-docx；doc（Word 97 二进制）用外部转换器提取文本后重建表格。"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import UnsupportedFormatError
from ..schema import Block, RawDocument, SourceRef, TableBlock
from ..utils import clean_text

_KV_RE = re.compile(r"^([A-Za-z\u4e00-\u9fff][^:：]{1,28})\s*[:：]\s*(.+)$")


def parse_word(path: Path, *, source: SourceRef) -> RawDocument:
    if path.suffix.lower() == ".doc":
        return _parse_legacy_doc(path, source=source)
    return _parse_docx(path, source=source)


# ---------------------------------------------------------------- docx


def _parse_docx(path: Path, *, source: SourceRef) -> RawDocument:
    import docx

    document = docx.Document(str(path))
    blocks: list[Block] = []
    line_no = 0
    paragraphs: list[str] = []

    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        line_no += 1
        paragraphs.append(text)
        kind = "kv" if _KV_RE.match(text) else "paragraph"
        blocks.append(Block(kind=kind, text=text, line_no=line_no))  # type: ignore[arg-type]

    tables: list[TableBlock] = []
    for index, table in enumerate(document.tables, start=1):
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        header = rows[0] if len(rows) > 1 else []
        body = rows[1:] if header else rows
        tables.append(TableBlock(name=f"table{index}", header=header, rows=body))
        for row in rows:
            line_no += 1
            line = " | ".join(cell for cell in row if cell)
            if line:
                paragraphs.append(line)
                blocks.append(Block(kind="table_row", text=line, line_no=line_no))

    text = clean_text("\n".join(paragraphs))
    return RawDocument(
        source=source,
        doc_kind="word",
        text=text,
        blocks=blocks,
        tables=tables,
        meta={"paragraphs": len(document.paragraphs), "tables": len(tables), "format": "docx"},
    )


# ---------------------------------------------------------------- .doc（Word 97 二进制）


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _convert_with_textutil(path: Path) -> str | None:
    """macOS 自带，无需安装。"""
    binary = shutil.which("textutil")
    if not binary:
        return None
    proc = _run([binary, "-convert", "txt", "-encoding", "UTF-8", "-stdout", str(path)])
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _convert_with_soffice(path: Path) -> str | None:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(
            [binary, "--headless", "--convert-to", "txt:Text", "--outdir", tmp, str(path)],
            timeout=180,
        )
        if proc.returncode != 0:
            return None
        produced = Path(tmp) / (path.stem + ".txt")
        if not produced.exists():
            return None
        return produced.read_text(encoding="utf-8", errors="replace")


def _convert_with_antiword(path: Path) -> str | None:
    binary = shutil.which("antiword")
    if not binary:
        return None
    proc = _run([binary, str(path)])
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


CONVERTERS = (
    ("textutil", _convert_with_textutil),
    ("soffice", _convert_with_soffice),
    ("antiword", _convert_with_antiword),
)


def _doc_to_text(path: Path) -> tuple[str, str]:
    for name, converter in CONVERTERS:
        text = converter(path)
        if text and text.strip():
            return text, name
    raise UnsupportedFormatError(
        f"无法解析旧版 .doc（{path.name}）：本机没有可用的转换器。"
        "请安装 textutil(macOS 自带) / LibreOffice(soffice) / antiword 之一，"
        "或把文件另存为 .docx",
        code="E_DOC_NO_CONVERTER",
    )


def _rows_from_tabs(lines: list[str]) -> tuple[list[list[str]], list[int]]:
    """把「同一段连续、含制表符」的行还原成表格，返回 (rows, 被合并的行下标)。"""
    rows: list[list[str]] = []
    consumed: list[int] = []
    buffer: list[list[str]] = []
    for index, line in enumerate(lines):
        if "\t" in line:
            cells = [cell.strip() for cell in line.split("\t")]
            cells = [cell for cell in cells if cell != ""] or cells
            buffer.append(cells)
            consumed.append(index)
        elif buffer:
            rows.extend(buffer)
            buffer = []
    rows.extend(buffer)
    return rows, consumed


def _parse_legacy_doc(path: Path, *, source: SourceRef) -> RawDocument:
    raw_text, converter = _doc_to_text(path)
    # 先在**未压空白**的原始文本上切行：制表符是还原表格的唯一线索
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    lines = [line for line in lines if line.strip()]

    tables: list[TableBlock] = []
    rows, table_line_index = _rows_from_tabs(lines)
    if len(rows) >= 2:
        tables.append(TableBlock(name="table1", header=rows[0], rows=rows[1:]))

    blocks: list[Block] = []
    for index, line in enumerate(lines):
        if index in table_line_index:
            cells = [cell for cell in line.split("\t") if cell.strip()]
            blocks.append(Block(kind="table_row", text=" | ".join(cells), line_no=index + 1))
            continue
        kind = "kv" if _KV_RE.match(line) else "paragraph"
        blocks.append(Block(kind=kind, text=line.strip(), line_no=index + 1))  # type: ignore[arg-type]

    text = clean_text("\n".join(lines))
    return RawDocument(
        source=source,
        doc_kind="word",
        text=text,
        blocks=blocks,
        tables=tables,
        meta={"format": "doc", "converter": converter, "tables": len(tables)},
    )
