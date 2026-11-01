"""文件分派：扩展名 + 魔数嗅探，未知/旧格式给出明确错误。"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..errors import UnsupportedFormatError
from ..schema import RawDocument, SourceRef
from ..utils import sha256_file
from .eml_parser import parse_eml
from .excel_parser import parse_excel
from .pdf_parser import parse_pdf_text
from .word_parser import parse_word

EXT_MAP: dict[str, str] = {
    ".eml": "eml",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
    ".bmp": "image",
    ".webp": "image",
    ".xls": "excel",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".doc": "word",
    ".docx": "word",
}

MAGIC_MAP: list[tuple[bytes, str]] = [
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"II*\x00", "image"),
    (b"MM\x00*", "image"),
    (b"BM", "image"),
]


def detect_kind(path: Path) -> str:
    kind = EXT_MAP.get(path.suffix.lower())
    if kind:
        return kind
    try:
        head = path.open("rb").read(16)
    except OSError:
        return "unknown"
    for magic, magic_kind in MAGIC_MAP:
        if head.startswith(magic):
            return magic_kind
    if head.startswith(b"PK\x03\x04"):
        return "excel"  # xlsx/docx 共用 zip 容器，扩展名已先行判定
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "excel"
    if head.lower().startswith(b"received:") or b"mime-version" in head.lower():
        return "eml"
    return "unknown"


def ingest(
    path: Path,
    *,
    workdir: Path,
    settings: Settings,
    origin: str = "root",
    depth: int = 0,
) -> RawDocument:
    """摄入单个文件。任何异常都收敛为 RawDocument.errors，不向上抛。"""
    path = Path(path)
    kind = detect_kind(path)
    try:
        source = SourceRef(
            path=str(path),
            sha256=sha256_file(path),
            kind=kind,  # type: ignore[arg-type]
            origin=origin,
            size_bytes=path.stat().st_size,
        )
    except OSError as exc:
        source = SourceRef(path=str(path), sha256="", kind="unknown", origin=origin)
        return RawDocument(
            source=source, doc_kind="unknown", errors=[f"E_PARSE_FAILED: 无法读取文件 ({exc})"]
        )

    try:
        if kind == "eml":
            return parse_eml(path, workdir=workdir, settings=settings, origin=origin, depth=depth)
        if kind == "pdf":
            return parse_pdf_text(path, source=source)
        if kind == "excel":
            return parse_excel(path, source=source)
        if kind == "word":
            return parse_word(path, source=source)
        if kind == "image":
            return RawDocument(source=source, doc_kind="image", meta={"needs_ocr": True})
    except UnsupportedFormatError as exc:
        return RawDocument(
            source=source, doc_kind=kind, errors=[f"{exc.code}: {exc}"]  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 - 单文件失败不拖垮批次
        return RawDocument(
            source=source,
            doc_kind=kind,  # type: ignore[arg-type]
            errors=[f"E_PARSE_FAILED: {type(exc).__name__}: {exc}"],
        )

    return RawDocument(
        source=source,
        doc_kind="unknown",
        errors=[f"E_UNSUPPORTED_DOC: 不支持的文件类型 {path.suffix} ({path.name})"],
    )


def expand_inputs(inputs: list[str | Path]) -> list[Path]:
    """目录展开为文件列表（不递归子目录之外的隐藏文件）。"""
    files: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and not child.name.startswith("."):
                    files.append(child)
        elif path.is_file():
            files.append(path)
    return files
