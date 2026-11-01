"""OCR 响应归一：真实网关返回的是「已结构化的单据」，不是 OCR 文本。

真机实测（2026-11-01，用公网 PDF 调通 `gateway.pltplt.com/ai/process`）得到的确切形状：

    {"data": {"createDate": "...", "fileName": "", "orderType": "None",
              "list": [{"fileType": "PAI-COLOADER-NOTICE", "mblNumber": "", "invoiceNumber": "",
                        "shipper": "", "consignee": "", "chargeList": [], "ctrList": [],
                        "hold": {...}, "remark": "", "page_index": [0, 1], ...}],
              "pages_split": [[0, 1]]},
     "success": true, "message": "文档处理成功", "error_code": null, "task_id": null}

也就是说：服务端已经做了业务字段抽取（`chargeList` 就是费用明细），我们不该再从里面
「找 OCR 文本」。同时保留形状无关的兜底解析，以应对网关升级或其它 provider。
"""

from __future__ import annotations

from typing import Any

from ..schema import Block, TableBlock
from ..utils import clean_text, preserve_lines

# 只收「像正文」的键，避免把状态码、错误信息当正文
TEXT_KEYS = {"text", "content", "line", "lines", "words", "value", "words_result", "ocr_text"}
SKIP_KEYS = {"msg", "message", "code", "status", "error", "errmsg", "taskid", "task_id", "requestid"}

# 网关 list[] 里的标量字段 → 单据上可识别的标签（下游规则抽取按标签抓）
GATEWAY_FIELD_LABELS: dict[str, str] = {
    "fileType": "DOCUMENT TYPE",
    "invoiceNumber": "INVOICE NO.",
    "mblNumber": "MBL NO.",
    "shipper": "SHIPPER",
    "consignee": "CONSIGNEE",
    "customsClearance": "CUSTOMS CLEARANCE",
    "customsReleaseStatus": "CUSTOMS RELEASE STATUS",
    "firmsCode": "FIRMS CODE",
    "cyFirmsCode": "CY FIRMS CODE",
    "inlandArrivalDate": "INLAND ARRIVAL DATE",
    "loadedOnRailDate": "LOADED ON RAIL DATE",
    "itNumber": "IT NO.",
    "itDate": "IT DATE",
    "itLocation": "IT LOCATION",
    "pickupLocation": "PICKUP LOCATION",
    "remark": "REMARK",
    "revisedEtaOfDestination": "REVISED ETA OF DESTINATION",
    "usCustomsStatus": "US CUSTOMS STATUS",
    "orderType": "ORDER TYPE",
}
# 这些是「状态/标记」，不是单据内容，别塞进正文污染抽取
GATEWAY_SKIP_FIELDS = {"page_index", "pageIndex", "suspicious", "isDetention", "isDemurrage"}


def _as_table_of_records(records: list[Any], name: str) -> TableBlock | None:
    """list[dict] → 表格。键名未知也能处理（按出现顺序并集做表头）。"""
    if not records:
        return None
    if all(isinstance(r, dict) for r in records):
        header: list[str] = []
        for record in records:
            for key in record:
                if key not in header:
                    header.append(str(key))
        if not header:
            return None
        rows = [
            ["" if record.get(key) in (None, []) else str(record.get(key)) for key in header]
            for record in records
        ]
        return TableBlock(name=name, header=header, rows=rows)
    if all(isinstance(r, list) for r in records):
        rows = [["" if c is None else str(c) for c in r] for r in records]
        if len(rows) >= 2:
            return TableBlock(name=name, header=rows[0], rows=rows[1:])
    return None


def _charge_records_as_lines(records: list[Any], label: str) -> list[str]:
    """费用列表再落一份「一行一条」的文本，供只认文本行的抽取路径兜底。"""
    lines: list[str] = []
    for record in records:
        if isinstance(record, dict):
            cells = [
                str(value).strip()
                for value in record.values()
                if isinstance(value, (str, int, float)) and str(value).strip()
            ]
            if cells:
                lines.append("  ".join(cells))
        elif isinstance(record, list):
            cells = [str(c).strip() for c in record if str(c).strip()]
            if cells:
                lines.append("  ".join(cells))
        elif isinstance(record, str) and record.strip():
            lines.append(record.strip())
    return lines


def _from_gateway_document(data: dict[str, Any]) -> tuple[list[str], list[Block], list[TableBlock]]:
    """把网关的「结构化单据」摊平成文本行 + 表格。"""
    lines: list[str] = []
    blocks: list[Block] = []
    tables: list[TableBlock] = []

    order_type = data.get("orderType")
    if isinstance(order_type, str) and order_type.strip() and order_type != "None":
        lines.append(f"ORDER TYPE: {order_type}")

    docs = data.get("list")
    if not isinstance(docs, list):
        return lines, blocks, tables

    for doc_index, doc in enumerate(docs, start=1):
        if not isinstance(doc, dict):
            continue
        page = _page_no(doc, doc_index)

        for key, value in doc.items():
            if key in GATEWAY_SKIP_FIELDS:
                continue
            if isinstance(value, (list, dict)):
                continue
            text = str(value).strip() if value is not None else ""
            if not text:
                continue
            label = GATEWAY_FIELD_LABELS.get(key, key.replace("_", " ").upper())
            lines.append(f"{label}: {text}")

        # hold 是个 dict，把非空项当备注
        hold = doc.get("hold")
        if isinstance(hold, dict):
            parts = [f"{k}={v}" for k, v in hold.items() if str(v).strip()]
            if parts:
                lines.append("HOLD: " + ", ".join(parts))

        # 费用明细：这是核心，既给表格也给文本行
        charges = doc.get("chargeList")
        if isinstance(charges, list) and charges:
            charge_lines = _charge_records_as_lines(charges, "chargeList")
            lines.extend(charge_lines)
            table = _as_table_of_records(charges, "chargeList")
            if table:
                table.page = page
                tables.append(table)
            for line in charge_lines:
                blocks.append(Block(kind="table_row", text=line, page=page))

        # 箱号列表
        containers = doc.get("ctrList")
        if isinstance(containers, list) and containers:
            container_lines = _charge_records_as_lines(containers, "ctrList")
            lines.extend(container_lines)
            table = _as_table_of_records(containers, "ctrList")
            if table:
                table.page = page
                tables.append(table)

        # other：自由附加信息
        other = doc.get("other")
        if isinstance(other, list) and other:
            lines.extend(_charge_records_as_lines(other, "other"))

    return lines, blocks, tables


def _page_no(item: dict[str, Any], fallback: int) -> int:
    for key in ("page", "page_no", "pageNo", "page_num", "pageNum", "index", "page_index"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list) and value and isinstance(value[0], int):
            return value[0]
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return fallback



def _rows_of(value: Any) -> list[list[str]] | None:
    if not isinstance(value, list) or not value:
        return None
    rows: list[list[str]] = []
    for row in value:
        if isinstance(row, list) and all(not isinstance(cell, (list, dict)) for cell in row):
            rows.append(["" if cell is None else str(cell).strip() for cell in row])
        elif isinstance(row, dict):
            cells = [str(v).strip() for v in row.values() if not isinstance(v, (list, dict))]
            if cells:
                rows.append(cells)
        else:
            return None
    return rows if len(rows) >= 2 else None


def _as_table(value: Any) -> list[list[str]] | None:
    """容忍多层包裹：[[[header],[row]]] 与 [[header],[row]] 都能吃。"""
    if not isinstance(value, list) or not value:
        return None
    direct = _rows_of(value)
    if direct:
        return direct
    if len(value) == 1:
        return _as_table(value[0])
    for item in value:
        parsed = _as_table(item)
        if parsed:
            return parsed
    return None


def _extract_tables(node: dict[str, Any]) -> list[TableBlock]:
    tables: list[TableBlock] = []
    for key in ("tables", "table", "cells", "table_data", "tableData"):
        candidate = node.get(key)
        if isinstance(candidate, list):
            rows = _as_table(candidate)
            if rows:
                tables.append(TableBlock(header=rows[0], rows=rows[1:]))
            for item in candidate:
                if isinstance(item, dict):
                    inner = _as_table(item.get("rows") or item.get("cells") or item.get("data"))
                    if inner:
                        tables.append(TableBlock(header=inner[0], rows=inner[1:]))
    return tables


def _page_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "content", "ocr_text"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if not parts:
        for key in ("lines", "line", "words", "words_result"):
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        for text_key in ("text", "words", "content", "word"):
                            if isinstance(item.get(text_key), str):
                                parts.append(item[text_key])
                                break
    return "\n".join(parts)


def _find_pages(raw: Any) -> list[dict[str, Any]]:
    """在嵌套结构里找 pages 列表。"""
    if isinstance(raw, dict):
        pages = raw.get("pages") or raw.get("page_list") or raw.get("pageList")
        if isinstance(pages, list) and pages and all(isinstance(p, dict) for p in pages):
            return pages
        for value in raw.values():
            found = _find_pages(value)
            if found:
                return found
    elif isinstance(raw, list):
        for item in raw:
            found = _find_pages(item)
            if found:
                return found
    return []


def _collect_strings(node: Any, out: list[str]) -> None:
    if isinstance(node, str):
        text = node.strip()
        if text and len(text) >= 2:
            out.append(text)
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in SKIP_KEYS:
                continue
            _collect_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_strings(item, out)


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def normalize_ocr(raw: dict[str, Any]) -> tuple[str, list[Block], list[TableBlock]]:
    """返回 (文本, blocks, tables)。"""
    blocks: list[Block] = []
    tables: list[TableBlock] = []
    chunks: list[str] = []

    # ① 真机网关格式：data.list[] 已经是抽好的结构化单据
    data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        lines, blocks, tables = _from_gateway_document(data)
        text = clean_text("\n".join(lines))
        for line_no, line in enumerate(lines, start=1):
            blocks.append(Block(kind="paragraph", text=line, line_no=line_no))
        chunks.append(text)

    pages = _find_pages(raw) if not chunks else []
    if pages:
        for index, page in enumerate(pages, start=1):
            page_no = _page_no(page, index)
            raw_text = _page_text(page)
            text = clean_text(raw_text)
            if text:
                chunks.append(text)
                # 保留行内空白：OCR 的列对齐全靠空格
                for line_no, line in enumerate(preserve_lines(raw_text), start=1):
                    blocks.append(
                        Block(kind="paragraph", text=line, page=page_no, line_no=line_no)
                    )
            for table in _extract_tables(page):
                table.page = page_no
                tables.append(table)
                for row in [table.header, *table.rows]:
                    if row and any(row):
                        blocks.append(
                            Block(kind="table_row", text=" | ".join(row), page=page_no)
                        )
    elif not chunks:
        strings: list[str] = []
        _collect_strings(raw, strings)
        lines = _dedupe(strings)
        text = clean_text("\n".join(lines))
        for line_no, line in enumerate(lines, start=1):
            blocks.append(Block(kind="paragraph", text=line, line_no=line_no))
        chunks.append(text)

    text = clean_text("\n".join(chunks))
    return text, blocks, tables


def gateway_error(raw: dict[str, Any]) -> str | None:
    """网关明确失败（success=false / error_code 非空）时返回原因，供调用方报错。"""
    if not isinstance(raw, dict):
        return None
    if raw.get("success") is False:
        message = raw.get("message") or "网关返回 success=false"
        code = raw.get("error_code")
        return f"{message}（error_code={code}）" if code else str(message)
    code = raw.get("error_code")
    if code not in (None, "", 0):
        return f"网关返回 error_code={code}: {raw.get('message') or ''}".strip()
    return None

