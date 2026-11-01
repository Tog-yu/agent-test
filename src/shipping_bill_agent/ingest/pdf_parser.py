"""PDF 文本层解析：pypdf 逐页抽文本；无文本层时标记 needs_ocr。"""

from __future__ import annotations

from pathlib import Path

from ..schema import Block, RawDocument, SourceRef
from ..utils import clean_text, preserve_lines


def parse_pdf_text(path: Path, *, source: SourceRef) -> RawDocument:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    blocks: list[Block] = []
    page_texts: list[str] = []

    for page_no, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - 单页失败不影响其他页
            raw = ""
            blocks.append(Block(kind="note", text=f"第 {page_no} 页提取失败: {exc}", page=page_no))
        page_texts.append(clean_text(raw))
        # 块级文本保留行内空白：列对齐是后续按列切分的前提
        for idx, line in enumerate(preserve_lines(raw), start=1):
            blocks.append(Block(kind="paragraph", text=line, page=page_no, line_no=idx))

    full_text = clean_text("\n".join(page_texts))
    has_text_layer = len(full_text) >= 20
    return RawDocument(
        source=source,
        doc_kind="pdf",
        text=full_text,
        blocks=blocks,
        tables=[],
        meta={
            "pages": len(reader.pages),
            "has_text_layer": has_text_layer,
            "text_chars": len(full_text),
            "needs_ocr": not has_text_layer,
        },
    )
