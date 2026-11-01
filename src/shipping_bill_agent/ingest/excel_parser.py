"""Excel 解析：xlsx/xlsm 走 openpyxl，xls 走 xlrd；多 sheet → TableBlock，表头按关键词打分识别。"""

from __future__ import annotations

from pathlib import Path

from ..errors import UnsupportedFormatError
from ..schema import Block, RawDocument, SourceRef, TableBlock

HEADER_HINTS = (
    "charge", "description", "item", "particular", "费用", "项目", "内容", "摘要",
    "amount", "金额", "currency", "币种", "curr", "qty", "quantity", "数量",
    "unit", "单价", "price", "rate", "total", "合计", "总计",
)

LEGACY_SUFFIXES = {".xls"}


def _cell_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header_score(row: list[str]) -> int:
    score = 0
    for cell in row:
        lowered = cell.lower()
        if any(hint in lowered for hint in HEADER_HINTS):
            score += 1
    return score


def _read_xlsx(path: Path) -> list[tuple[str, list[list[str]]]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets: list[tuple[str, list[list[str]]]] = []
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell_to_text(cell) for cell in row]
                if any(cells):
                    rows.append(cells)
            sheets.append((sheet.title, rows))
        return sheets
    finally:
        workbook.close()


def _read_xls(path: Path) -> list[tuple[str, list[list[str]]]]:
    """旧版 BIFF（.xls）：xlrd 2.x 只支持 xls，正好对上。"""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
        raise UnsupportedFormatError(
            "读取 .xls 需要安装 xlrd（pip install xlrd>=2.0）",
            code="E_XLS_READER_MISSING",
        ) from exc

    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:  # noqa: BLE001 - 损坏文件要给出可读错误
        raise UnsupportedFormatError(
            f".xls 文件无法解析（{path.name}）：{type(exc).__name__}: {exc}",
            code="E_XLS_CORRUPT",
        ) from exc

    sheets: list[tuple[str, list[list[str]]]] = []
    try:
        for sheet in book.sheets():
            rows: list[list[str]] = []
            for row_index in range(sheet.nrows):
                cells: list[str] = []
                for col_index in range(sheet.ncols):
                    cell = sheet.cell(row_index, col_index)
                    cells.append(_cell_to_text(_xls_value(book, cell)))
                if any(cells):
                    rows.append(cells)
            sheets.append((sheet.name, rows))
    finally:
        book.release_resources()
    return sheets


def _xls_value(book, cell):  # noqa: ANN001 - xlrd 类型不暴露
    if cell.ctype == 3:  # XL_CELL_DATE
        try:
            import xlrd

            return xlrd.xldate_as_datetime(cell.value, book.datemode).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001 - 日期换算失败就退回原值
            return cell.value
    if cell.ctype in (0, 6):  # EMPTY / BLANK
        return None
    if cell.ctype == 4:  # BOOLEAN
        return "TRUE" if cell.value else "FALSE"
    return cell.value


def _build_document(
    sheets: list[tuple[str, list[list[str]]]], *, source: SourceRef, fmt: str
) -> RawDocument:
    tables: list[TableBlock] = []
    text_chunks: list[str] = []

    for name, raw_rows in sheets:
        if not raw_rows:
            continue

        header: list[str] = []
        data_rows = raw_rows
        first = raw_rows[0]
        if _header_score(first) >= 2 or (_header_score(first) >= 1 and len(first) >= 3):
            header = first
            data_rows = raw_rows[1:]
        else:
            # 表头不一定在第一行（前面常有单据头信息）：在前 15 行里找得分最高的
            best_idx, best_score = -1, 0
            for idx, candidate in enumerate(raw_rows[:15]):
                score = _header_score(candidate)
                if score > best_score:
                    best_idx, best_score = idx, score
            if best_score >= 2:
                header = raw_rows[best_idx]
                data_rows = raw_rows[best_idx + 1 :]

        tables.append(
            TableBlock(name=name, header=header, rows=[r for r in data_rows if any(r)])
        )

        # 文本表示保留**全部**行：表头之上的单据头信息（单号/交易方/日期）也要能被抽到
        for row in raw_rows:
            line = " | ".join(cell for cell in row if cell)
            if line:
                text_chunks.append(line)

    text = "\n".join(text_chunks)
    blocks = [
        Block(kind="paragraph", text=line, line_no=idx + 1)
        for idx, line in enumerate(text.split("\n"))
        if line.strip()
    ]

    return RawDocument(
        source=source,
        doc_kind="excel",
        text=text,
        blocks=blocks,
        tables=tables,
        meta={"sheets": [t.name for t in tables], "format": fmt},
    )


def parse_excel(path: Path, *, source: SourceRef) -> RawDocument:
    if path.suffix.lower() in LEGACY_SUFFIXES:
        return _build_document(_read_xls(path), source=source, fmt="xls")
    return _build_document(_read_xlsx(path), source=source, fmt="xlsx")
