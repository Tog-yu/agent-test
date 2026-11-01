"""规则抽取器：高确定字段用正则，费用行用「表格列角色推断 → 同行 → 邻行」三档。"""

from __future__ import annotations

import re
from typing import Any

from ..normalize.amounts import parse_amount
from ..normalize.currency import normalize_currency
from ..normalize.payment import normalize_payment_term
from ..schema import (
    Block,
    ChargeCandidate,
    DocumentContext,
    Extraction,
    FieldVal,
    RawDocument,
    TableBlock,
)
from ..utils import clean_text
from .rule_dates import (
    DATE_PATTERNS,
    extract_ref_from_subject,
    normalize_date_match,
    parse_date_string,
)

# 金额：容忍 OCR 把数字认成字母（0→O、1→I 等），交给校验闸门判断而不是在这里丢弃
MONEY = r"(?:(?:[A-Z]{3}|[$¥€£￥])\s*)?[\(\-]?[0-9][0-9OoIlSsBGgZz,\.]*[\)\-]?"

DOC_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("Debit Note", r"debit\s*note|debit\s*memo|D/?N\b|借记通知|借方"),
    ("Credit Note", r"credit\s*note|credit\s*memo|C/?N\b|贷记通知|贷方"),
    ("Invoice", r"\binvoice\b|commercial\s+invoice|发票"),
    ("Arrival Notice", r"arrival\s+notice|到货通知|到港通知"),
]

REF_PATTERNS = [
    # 真实货代账单常用 D/C No.（Debit/Credit Note 编号）
    r"d\s*/\s*c\s*(?:no\.?|number|#)\s*[:：#|]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{3,30})",
    r"(?:debit\s*note|credit\s*note|invoice|document|doc(?:ument)?|ref(?:erence)?|bill)"
    r"\s*(?:no\.?|number|num|#|编号|号码)?\s*[:：#|]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{3,30})",
    r"(?:单号|单据编号|账单号)\s*[:：|]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{3,30})",
]
REF_FALLBACK = r"\b((?:DN|CN|INV|BL|BK|DC)[-/]?\d{4,}[-/]?\d{1,6})\b"

DATE_LABEL_RE = re.compile(r"date|日期|签发日", re.I)

ISSUER_PATTERNS = [
    # 真实账单里「谁开的票」最硬的证据：尾行 balance due to / 收款方
    r"(?:grand\s+)?total\s+balance\s+due\s+to\s+([A-Z][A-Za-z0-9&.,'\- ]{2,60})",
    r"(?:issued?\s*by|issuer|bill\s*from|seller|exporter|签发方|开票方)\s*[:：|]\s*(.+)",
    r"(?:b/?l\s*)?beneficiary\s*[:：|]\s*(.+)",
    r"(?:shipper|exporter)\s*[:：|]\s*(.+)",
]
# 收款/银行行里的 beneficiary 不是单据签发方，排除
BANK_PARTY_RE = re.compile(r"bank|securities|\bbv\b|\bn\.?a\.?\b|hsbc|account|routing|\bach\b", re.I)

RECIPIENT_PATTERNS = [
    r"(?:bill\s*to|billed\s*to|invoice\s*to|买?方|付款方)\s*[:：|]\s*(.+)",
    r"(?:buyer|importer|payer)\s*[:：|]\s*(.+)",
    r"^(?:to|messrs)\s*[:：|]\s*(.+)",
]
BL_PATTERNS = [
    r"(?:master\s+)?(?:b/?l\s*(?:no\.?)?|bill\s*of\s*lading(?:\s*no\.?)?|提单号)\s*[:：|]\s*([A-Za-z0-9\-/]+)",
]
BOOKING_PATTERNS = [r"(?:booking\s*(?:no\.?)?|订舱号)\s*[:：|]\s*([A-Za-z0-9\-/]+)"]
CONTAINER_PATTERNS = [
    r"(?:container|cn?tr\.?)\s*(?:no\.?)?\s*[:：|]\s*([A-Za-z]{4}\d{7})",
]
SUBTOTAL_PATTERNS = [rf"(?:sub\s*-?\s*total|小计)\s*[:：]?\s*({MONEY})"]
TAX_PATTERNS = [rf"(?:tax|vat|gst|税额|税费)\s*[:：]?\s*({MONEY})"]
TOTAL_PATTERNS = [
    # TOTAL AMOUNT 1,410.40 / TOTAL CHARGES TO BE PAID: 245.0000 / GRAND TOTAL
    rf"(?:grand\s*total|total\s*charges?\s*(?:to\s*be\s*paid)?|total\s*amount|total|合计|总计)"
    rf"\s*[:：]?\s*({MONEY})",
]
# "TOTAL QTY 4" / "TOTAL WEIGHT 1,000KGS" 不是金额行
NON_MONEY_TOTAL_RE = re.compile(
    r"total\s*(?:qty|quantity|pcs|pieces?|ctns?|cartons?|pkgs?|packages?|weight|kgs?|"
    r"cbm|volume|containers?|数量|重量|体积|件数)",
    re.I,
)
STRONG_TOTAL_RE = re.compile(r"grand\s*total|total\s*amount|total\s*charges?|合计|总计", re.I)
# 备注/付款说明类句子：出现在这里的主体名不能当作「接收方」声明
# 签发方所在的那一行不能拿来推断「接收方」
ISSUER_LINE_RE = re.compile(
    r"issued?\s*by|issuer|bill\s*from|seller|exporter|beneficiary|签发方|开票方", re.I
)
# 水印 / SaaS 落款：`... Powered by` + 紧接着的公司名，是这套单据系统的供应商，
# 不是「单据开给谁」。真实数据踩过：Debit Note 页脚是
#   "NEPTUNE SHIPPING LIMITED (LAX) Powered by"
#   "湃乐多（上海）航运科技有限公司"
# 把后者当成接收方会让 AR/AP 直接反过来（我方只是 SaaS 提供方，根本不是付款人）。
WATERMARK_RE = re.compile(
    r"powered\s*by|provided\s*by|system\s*by|generated\s*by|supported\s*by|"
    r"技术支持|提供技术支持|系统提供|系统生成|本单据由|"
    r"copyright|all\s+rights\s+reserved|©|\(c\)\s*20\d\d",
    re.I,
)


def _watermark_line_indices(lines: list[str]) -> set[int]:
    """标出水印/落款块所在的行下标（含紧跟其后的 1~2 个非空行）。"""
    blocked: set[int] = set()
    for index, line in enumerate(lines):
        if not WATERMARK_RE.search(line):
            continue
        blocked.add(index)
        taken = 0
        for offset in range(1, 6):
            if index + offset >= len(lines):
                break
            if not lines[index + offset].strip():
                continue
            blocked.add(index + offset)
            taken += 1
            # 落款块可能有多行（中英文公司名各一行），最多连带 3 行
            if taken >= 3:
                break
    return blocked


def _same_party(left: object, right: object) -> bool:
    """交易方比较：忽略大小写、空格与标点（Co., Ltd. 与 Co.,Ltd 视为同一家）。"""
    def norm(value: object) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(value or "").lower())

    a, b = norm(left), norm(right)
    return bool(a) and a == b


REMARK_LINE_RE = re.compile(
    r"^\s*(?:remarks?|notes?|please\s*note|n\.?b\.?|important|attention|提示|备注|注)\b|"
    r"payable\s+by|paid\s+by|charged\s+to|at\s+the\s+expense\s+of|on\s+behalf\s+of|"
    r"to\s+be\s+paid\s+by|由.{0,8}(?:支付|承担)",
    re.I,
)
CURRENCY_LABEL_PATTERNS = [
    rf"(?:currency|curr|币种)\s*[:：]?\s*([A-Z]{{3}})\b",
    rf"\b([A-Z]{{3}})\s+(?:currency|币种)\b",
]

# ---- 费用区块锚点：真实账单的费用行只可能出现在这两个锚点之间
CHARGE_SECTION_START_RE = re.compile(
    r"^(?:charges?|charge\s*desc\w*|description|particulars|details|费用|费用项目|项目|明细|摘要)"
    r"[\s:：|].*"
    r"(?:cur|curr|currency|amount|rate|qty|debit|credit|金额|币种|单价|数量)",
    re.I,
)
CHARGE_SECTION_END_RE = re.compile(
    r"^(?:sub\s*-?\s*total|total|grand\s*total|paid\s*amount|balance|amount\s*due|tax|vat|gst|"
    r"remark|remarks|note|notes|please\s+note|beneficiary|bank|合计|总计|小计|税额|备注)",
    re.I,
)

SKIP_CHARGE_NAMES = {
    "total", "subtotal", "sub total", "grand total", "tax", "vat", "gst", "amount",
    "currency", "qty", "quantity", "unit price", "price", "rate", "remark", "remarks",
    "description", "item", "particulars", "charge", "charges", "no", "no.", "合计", "总计",
    "小计", "税额", "备注", "金额", "币种", "数量", "单价", "费用", "项目",
}
TOTAL_LINE_RE = re.compile(
    r"^(?:sub\s*-?\s*total|total|grand\s*total|tax|vat|gst|balance|合计|总计|小计|税额)", re.I
)

NAME_HINTS = ("charge", "description", "item", "particular", "费用", "项目", "内容", "摘要", "名称")
AMOUNT_HINTS = ("amount", "金额", "总额", "小计", "value")
TOTAL_COL_HINTS = ("total", "合计", "总计")
CURRENCY_HINTS = ("currency", "币种", "curr")
QTY_HINTS = ("qty", "quantity", "数量", "件数")
PRICE_HINTS = ("unit", "price", "单价", "rate")
TERM_HINTS = ("payment", "term", "付款", "pp/cc", "p/c", "prepaid", "collect", "预付", "到付")

PP_PATTERN = re.compile(r"\bPP\b|\bprepaid\b|\bpre-paid\b|预付")
CC_PATTERN = re.compile(r"\bCC\b|\bcollect\b|到付")


# ---------------------------------------------------------------- 工具


def _looks_numeric(text: str) -> bool:
    value, _ = parse_amount(text)
    return value is not None


def _numeric_density(rows: list[list[str]], col: int) -> float:
    total = 0
    hits = 0
    for row in rows:
        if col >= len(row):
            continue
        cell = row[col].strip()
        if not cell:
            continue
        total += 1
        if _looks_numeric(cell):
            hits += 1
    return hits / total if total else 0.0


def _text_density(rows: list[list[str]], col: int) -> float:
    total = 0
    hits = 0
    for row in rows:
        if col >= len(row):
            continue
        cell = row[col].strip()
        if not cell:
            continue
        total += 1
        if re.search(r"[A-Za-z\u4e00-\u9fff]{3,}", cell) and not _looks_numeric(cell):
            hits += 1
    return hits / total if total else 0.0


def _match_header(header: list[str], hints: tuple[str, ...]) -> int | None:
    for idx, cell in enumerate(header):
        lowered = (cell or "").lower()
        if any(hint in lowered for hint in hints):
            return idx
    return None


def _pick_columns(table: TableBlock) -> dict[str, int | None]:
    header = [h or "" for h in table.header]
    rows = table.rows
    width = max([len(header)] + [len(r) for r in rows] + [0])

    def header_at(col: int) -> str:
        return header[col] if 0 <= col < len(header) else ""

    cols: dict[str, int | None] = {
        "name": _match_header(header, NAME_HINTS),
        "amount": _match_header(header, AMOUNT_HINTS),
        "currency": _match_header(header, CURRENCY_HINTS),
        "qty": _match_header(header, QTY_HINTS),
        "price": _match_header(header, PRICE_HINTS),
        "term": _match_header(header, TERM_HINTS),
    }
    if cols["name"] is None:
        best = max(range(width), key=lambda c: _text_density(rows, c), default=None)
        if best is not None and _text_density(rows, best) >= 0.4:
            cols["name"] = best
    if cols["amount"] is None:
        candidates = [
            c for c in range(width) if c != cols["name"] and _numeric_density(rows, c) >= 0.5
        ]
        if candidates:
            # 金额列优先：表头带 total 的列次之，否则取最右的数值列
            non_total = [
                c for c in candidates if not any(h in header_at(c).lower() for h in TOTAL_COL_HINTS)
            ]
            cols["amount"] = (non_total or candidates)[-1]
    return cols


def _clean_name(text: str) -> str:
    name = re.sub(r"\s{2,}", " ", (text or "").strip())
    return name.strip(" :：-—|")


# 这些「名字」其实是字段标签，后面跟的数字是编号/电话/账号，不是金额
LABEL_LIKE_RE = re.compile(
    r"(?:^|\b)(?:no\.?|nos\.?|number|num|#|acct|account|routing|tel|fax|phone|mobile|"
    r"e[_\s-]?mail|date|ref|ref\.|reference|file|invoice\s*date|eta|etd|atd|ata|"
    r"voyage|voy|seal|size|type|weight|measure|package|pkgs|kgs|lbs|cbm|cft|"
    r"booking|b/?l|mbl|hbl|ams|scac|zip|code|days|count|qty)\s*$",
    re.I,
)
ADDRESS_LIKE_RE = re.compile(
    r"\b(?:street|st\.|ave\.?|avenue|blvd|boulevard|road|rd\.?|suite|ste\.?|floor|fl\.?|"
    r"box|drive|dr\.?|lane|ln\.?|way|highway|hwy|plaza|tower|building|bldg)\b"
    r"|^[A-Z][A-Za-z .]{2,30},\s*[A-Z]{2}\s*\d{5}",
    re.I,
)
# 纯编号（单号/账号/箱号）：长得像金额但其实是标识符
ID_LIKE_RE = re.compile(r"^[A-Z]{0,4}[-/ ]?\d{6,}$")


def _name_is_junk(name: str) -> bool:
    cleaned = _clean_name(name)
    if not cleaned:
        return True
    if re.search(r"[:：]\s*$", cleaned):
        return True
    if LABEL_LIKE_RE.search(cleaned):
        return True
    if ADDRESS_LIKE_RE.search(cleaned):
        return True
    letters = re.findall(r"[A-Za-z\u4e00-\u9fff]", cleaned)
    digits = re.findall(r"\d", cleaned)
    if digits and len(digits) >= max(4, len(letters)):
        return True
    # 全大写无空格的长串（表头/免责声明残片）
    if len(cleaned) > 40 and " " not in cleaned:
        return True
    return False


def _strip_name_noise(name: str) -> str:
    """去掉名字里被顺带切进来的数量/单价/付款方式残渣，如 'HANDLING CHARGE - 65.00 1.00 C'。"""
    cleaned = _clean_name(name)
    tokens = cleaned.split()
    while len(tokens) > 1:
        tail = tokens[-1]
        if (
            _MONEY_CELL_RE.match(tail)
            or re.fullmatch(r"[A-Z]{1,2}", tail)
            or re.fullmatch(r"[Pp]/[Cc]", tail)
            or tail in {"-", "—", "|", "@", "X", "x"}
        ):
            tokens.pop()
            continue
        break
    result = " ".join(tokens).strip(" -—|:")
    if re.search(r"[:：]", result):
        tail = re.split(r"[:：]", result)[-1].strip(" -—|:")
        head = re.split(r"[:：]", result)[0].strip()
        # 左边是一串说明性前缀、右边是真正的费用名时，取右边
        if tail and 2 <= len(tail) <= 30 and len(head.split()) >= 2:
            result = tail
    # 行首数量（'4 ADD'L DETENTION...' 里的 4 是「4 天」）
    result = re.sub(r"^\d{1,3}\s+(?=[A-Za-z\u4e00-\u9fff])", "", result)
    return result.strip()


CURRENCY_TOKEN_RE = re.compile(
    r"\b(?:USD|CNY|RMB|EUR|GBP|HKD|SGD|JPY|AUD|CAD|TWD|THB|MYR|VND)\b|[$\u00a5\u20ac\u00a3\uffe5]"
)


SYMBOL_CURRENCY = {"$": "USD", "US$": "USD", "\u00a5": "CNY", "\uffe5": "CNY", "\u20ac": "EUR", "\u00a3": "GBP"}


def _currency_from_amount(amount_raw: str) -> str | None:
    for symbol, code in SYMBOL_CURRENCY.items():
        if symbol in (amount_raw or ""):
            return code
    return None


def _line_has_money_evidence(text: str, amount_raw: str) -> bool:
    """严格模式：没有小数位时，必须同行带币种，才认这是金额（挡掉页码/重量/邮编）。"""
    if "." in amount_raw:
        return True
    return bool(CURRENCY_TOKEN_RE.search(text))


def _amount_plausible(amount: float, raw: str) -> bool:
    """挡掉把账号/单号当金额的情况：无小数点且长得像标识符/年份。"""
    text = (raw or "").strip()
    if "." in text or "," in text and "." in text:
        return True
    if ID_LIKE_RE.match(text):
        return False
    if amount.is_integer() and amount >= 100000:
        return False
    if amount.is_integer() and 1900 <= amount <= 2100 and len(text.replace(",", "")) == 4:
        return False
    return True


def _valid_charge_name(name: str) -> bool:
    cleaned = _clean_name(name)
    if len(cleaned) < 2:
        return False
    lowered = cleaned.lower()
    if lowered in SKIP_CHARGE_NAMES:
        return False
    if TOTAL_LINE_RE.match(lowered):
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", cleaned):
        return False
    if _looks_numeric(cleaned) and not re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", cleaned):
        return False
    if _name_is_junk(cleaned):
        return False
    return True


def _charge_regions(lines: list[tuple[str, int | None, int | None]]) -> list[tuple[int, int]]:
    """定位费用区块：从「描述/表头行」到「合计/备注行」。

    真实账单（Arrival Notice、Debit Note）里满页是地址、电话、账号，
    只有表头锚点之间的行才可能是费用行——这一层能把噪声行挡在抽取之外。
    区块不跨页。
    """
    regions: list[tuple[int, int]] = []
    for idx, (text, page, _line_no) in enumerate(lines):
        stripped = text.strip()
        if not CHARGE_SECTION_START_RE.match(stripped):
            continue
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            next_text, next_page, _ = lines[j]
            if page is not None and next_page is not None and next_page != page:
                end = j
                break
            if CHARGE_SECTION_END_RE.match(next_text.strip()):
                end = j
                break
        if end > idx + 1:
            regions.append((idx + 1, end))
    return regions


# ---------------------------------------------------------------- 单字段


def _find_labeled(
    lines: list[str], patterns: list[str], *, confidence: float = 0.9
) -> FieldVal:
    for pattern in patterns:
        regex = re.compile(pattern, re.I)
        for line in lines:
            match = regex.search(line)
            if not match:
                continue
            # 表格被压平成 "key | value" 时会带出前导竖线，去掉它
            value = (match.group(1) or "").strip().strip(".,;:|").strip()
            if value:
                return FieldVal(value=value, span=line.strip(), extractor="rule", confidence=confidence)
    return FieldVal()


def _find_document_type(lines: list[str]) -> FieldVal:
    for line in lines:
        for doc_type, pattern in DOC_TYPE_PATTERNS:
            if re.search(pattern, line, re.I):
                return FieldVal(
                    value=doc_type, span=line.strip(), extractor="rule", confidence=0.9
                )
    return FieldVal()


def _find_reference_no(lines: list[str]) -> FieldVal:
    # 单号必须含数字：否则 EXPORT REFERENCES / HOUSE BILL INFO 这类标签会被当成单号
    for pattern in REF_PATTERNS:
        regex = re.compile(pattern, re.I)
        for line in lines:
            match = regex.search(line)
            if not match:
                continue
            candidate = (match.group(1) or "").strip().strip(".,;:")
            if candidate and len(candidate) >= 4 and re.search(r"\d{3,}", candidate):
                return FieldVal(
                    value=candidate, span=line.strip(), extractor="rule", confidence=0.9
                )
    for line in lines:
        match = re.search(REF_FALLBACK, line)
        if match:
            return FieldVal(
                value=match.group(1), span=line.strip(), extractor="rule", confidence=0.75
            )
    return FieldVal()


def _find_date(lines: list[str]) -> FieldVal:
    labelled = [line for line in lines if DATE_LABEL_RE.search(line)]
    for scope, confidence in ((labelled, 0.95), (lines, 0.8)):
        for line in scope:
            for pattern, kind in DATE_PATTERNS:
                match = re.search(pattern, line, re.I)
                if not match:
                    continue
                normalized = normalize_date_match(match, kind)
                if normalized:
                    return FieldVal(
                        value=normalized,
                        span=line.strip(),
                        extractor="rule",
                        confidence=confidence,
                    )
    return FieldVal()


def _find_total(lines: list[str]) -> FieldVal:
    """取单据总额。跳过数量/重量类 TOTAL 行；强模式（GRAND TOTAL / TOTAL AMOUNT）优先。"""
    regexes = [re.compile(pattern, re.I) for pattern in TOTAL_PATTERNS]
    weak = FieldVal()
    for strong_pass in (True, False):
        for line in lines:
            if NON_MONEY_TOTAL_RE.search(line):
                continue
            if bool(STRONG_TOTAL_RE.search(line)) != strong_pass:
                continue
            for regex in regexes:
                match = regex.search(line)
                if not match:
                    continue
                raw = match.group(1)
                if not _line_has_money_evidence(line, raw):
                    continue
                value, conf = parse_amount(raw)
                if value is None:
                    continue
                found = FieldVal(
                    value=value, span=line.strip(), extractor="rule", confidence=min(conf, 0.9)
                )
                if strong_pass:
                    weak = found  # 同一强度内后出现的覆盖前者（GRAND TOTAL 通常在最后）
                else:
                    weak = weak if weak.value is not None else found
                break
        if weak.value is not None:
            return weak
    return weak


def _currency_of_line(line: str) -> str | None:
    code, conf = normalize_currency(line)
    if code and conf >= 0.5:
        return code
    return None


def _totals_by_currency(lines: list[str]) -> dict[str, float]:
    """单据按币种分别给出的总额：TOTAL USD 100.00 / TOTAL EUR 50.00 → {USD:100, EUR:50}。"""
    regexes = [re.compile(pattern, re.I) for pattern in TOTAL_PATTERNS]
    totals: dict[str, float] = {}
    for line in lines:
        if NON_MONEY_TOTAL_RE.search(line):
            continue
        for regex in regexes:
            match = regex.search(line)
            if not match:
                continue
            code = _currency_of_line(line)
            if not code:
                break
            value, _ = parse_amount(match.group(1))
            if value is not None:
                totals[code] = value
            break
    return totals


def _find_money_line(lines: list[str], patterns: list[str]) -> FieldVal:
    for pattern in patterns:
        regex = re.compile(pattern, re.I)
        for line in lines:
            match = regex.search(line)
            if not match:
                continue
            value, conf = parse_amount(match.group(1))
            if value is not None:
                return FieldVal(
                    value=value, span=line.strip(), extractor="rule", confidence=min(conf, 0.9)
                )
    return FieldVal()


def _find_payment_term(lines: list[str]) -> FieldVal:
    for line in lines:
        if PP_PATTERN.search(line):
            return FieldVal(value="PP", span=line.strip(), extractor="rule", confidence=0.85)
        if CC_PATTERN.search(line):
            return FieldVal(value="CC", span=line.strip(), extractor="rule", confidence=0.85)
    return FieldVal()


_TERM_CELL_MAX = 16


def _term_cell(cell: str) -> str | None:
    """费用行的付款方式单元格：短单元格才归一，避免把长描述误判成 P/C。"""
    text = (cell or "").strip()
    if not text or len(text) > _TERM_CELL_MAX:
        return None
    return normalize_payment_term(text)


# 无列分隔的 PDF 文本行：P/C 列会挤成「… 1.00 C 65.00」，
# 只认「紧贴金额之前的独立单字母」，避免误伤费用名里的普通字母。
_TERM_BEFORE_AMOUNT_RE = re.compile(r"(?:^|\s)([PC])\s*$")


def _term_before_amount(name_part: str) -> str | None:
    match = _TERM_BEFORE_AMOUNT_RE.search((name_part or "").rstrip())
    return normalize_payment_term(match.group(1)) if match else None


# ---------------------------------------------------------------- 费用行


def _charges_from_table(
    table: TableBlock, *, source_path: str | None, terms_map: dict[str, str] | None = None
) -> list[ChargeCandidate]:
    rows = [r for r in table.rows if any(cell.strip() for cell in r)]
    if not rows:
        return []
    cols = _pick_columns(table)
    if cols["name"] is None or cols["amount"] is None:
        return []

    charges: list[ChargeCandidate] = []
    for row in rows:
        def cell(key: str) -> str:
            idx = cols.get(key)
            if idx is None or idx >= len(row):
                return ""
            return (row[idx] or "").strip()

        name = cell("name")
        if not _valid_charge_name(name):
            continue
        amount, amount_conf = parse_amount(cell("amount"))
        if amount is None:
            continue
        qty, _ = parse_amount(cell("qty"))
        price, _ = parse_amount(cell("price"))
        currency = cell("currency") or None
        # 表头的 P/C 列取值是 P（Prepaid）/ C（Collect），契约里要 PP / CC
        term = normalize_payment_term(cell("term"))
        charges.append(
            ChargeCandidate(
                raw_name=_clean_name(name),
                amount=amount,
                quantity=qty,
                unit_price=price,
                currency=currency,
                payment_term=term,
                span=" | ".join(c for c in row if c),
                source_path=source_path,
                page=table.page,
                confidence=0.9 if table.header else amount_conf * 0.9,
                extractor="rule:table",
            )
        )
    return charges


_COL_SPLIT_RE = re.compile(r"\s{2,}")
_CURRENCY_CELL_RE = re.compile(r"^[A-Za-z$¥€£￥]{1,4}$")
_LETTERS_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]{2,}")
# 允许 OCR 噪声：以数字开头、可含 0/O、1/I 等混淆字符
_MONEY_CELL_RE = re.compile(r"^(?:(?:[A-Z]{3}|[$¥€£￥])\s*)?[\(\-]?[0-9][0-9OoIlSsBGgZz,\.]*[\)\-]?$")


def _money_cell(cell: str) -> tuple[float, float] | None:
    """只把「像金额」的单元格当数字，避免把带字母的词（USD/PP）误判。"""
    text = cell.strip()
    if not text or not _MONEY_CELL_RE.match(text):
        return None
    value, confidence = parse_amount(text)
    if value is None:
        return None
    return value, confidence


def _parse_columnar_row(
    text: str, *, source_path: str | None, page: int | None, line_no: int | None
) -> ChargeCandidate | None:
    """固定宽度/制表位对齐的行：按 2+ 空格切列，再推断列角色。

    这一档专门服务「PDF 文本层」与「表格式邮件正文」——它们没有结构化 table，
    但列之间有稳定的宽间距。
    """
    cells = [cell.strip() for cell in _COL_SPLIT_RE.split(text.strip()) if cell.strip()]
    if len(cells) < 3:
        return None

    name_idx = next(
        (i for i, cell in enumerate(cells) if _LETTERS_RE.search(cell) and not TOTAL_LINE_RE.match(cell)),
        None,
    )
    if name_idx is None:
        return None
    name = _strip_name_noise(cells[name_idx])
    if not _valid_charge_name(name):
        return None

    numeric: list[tuple[int, float, float]] = []
    for index, cell in enumerate(cells):
        if index == name_idx:
            continue
        parsed = _money_cell(cell)
        if parsed is not None:
            numeric.append((index, parsed[0], parsed[1]))
    if not numeric:
        return None

    amount_idx, amount, amount_conf = max(numeric, key=lambda item: item[0])
    if amount_idx <= name_idx:
        return None
    if not _amount_plausible(amount, cells[amount_idx]):
        return None

    currency: str | None = None
    term: str | None = None
    for index, cell in enumerate(cells):
        if index in {name_idx, amount_idx}:
            continue
        term_hit = _term_cell(cell)
        if term_hit:
            term = term_hit
            continue
        if currency is None and _CURRENCY_CELL_RE.match(cell):
            code, _ = normalize_currency(cell)
            if code:
                currency = code

    between = [value for index, value, _ in numeric if name_idx < index < amount_idx]
    quantity = between[0] if len(between) >= 2 else None
    unit_price = between[-1] if between else None

    # 列齐、币种/付款方式都在位时，金额归属是硬证据；否则降档
    base = 0.9 if (currency or term) and len(cells) >= 5 else 0.78
    confidence = min(base, amount_conf) if amount_conf else base * 0.85

    return ChargeCandidate(
        raw_name=name,
        amount=amount,
        quantity=quantity,
        unit_price=unit_price,
        currency=currency,
        payment_term=term,
        span=text.strip(),
        source_path=source_path,
        page=page,
        line_no=line_no,
        confidence=confidence,
        extractor="rule:columns",
    )


_TRAILING_MONEY_RE = re.compile(
    rf"^(?P<name>.+?)\s{{1,}}(?P<amount>{MONEY})\s*(?P<currency>[A-Z]{{3}})?\s*$"
)


TRANSPORT_DOC_RE = re.compile(
    r"bill\s+of\s+lading|sea\s*waybill|way\s*bill|ocean\s+bill|air\s*waybill|提单", re.I
)


def _is_transport_document(lines: list[str]) -> bool:
    """提单/运单：没有费用区块时不该硬抽费用（否则重量、地址、箱号都会被当成金额）。"""
    head = [line for line in lines[:120]]
    if not any(TRANSPORT_DOC_RE.search(line) for line in head):
        return False
    # 有明确费用表头的除外（有些提单背面会附费用）
    return not any(
        "charges" in line.lower() and "amount" in line.lower() for line in lines
    )


def _charges_from_lines(
    lines: list[tuple[str, int | None, int | None]],
    *,
    source_path: str | None,
    regions: list[tuple[int, int]] | None = None,
    strict: bool = False,
) -> list[ChargeCandidate]:
    """文本行四档：列切分 → 同行「名称 + 金额」→ 同行带币种 → 下一行是金额。

    regions 非空时只在区块内找费用行（真实账单必备，否则地址/电话/账号会混进来）。
    """
    charges: list[ChargeCandidate] = []
    consumed: set[int] = set()

    scope: list[int] = (
        [i for start, end in regions for i in range(start, end)] if regions else list(range(len(lines)))
    )

    # 档 1：固定宽度列
    for idx in scope:
        line, page, line_no = lines[idx]
        stripped = line.strip()
        if len(stripped) < 6 or TOTAL_LINE_RE.match(stripped):
            continue
        candidate = _parse_columnar_row(
            stripped, source_path=source_path, page=page, line_no=line_no
        )
        if candidate is not None:
            charges.append(candidate)
            consumed.add(idx)

    # 档 2/3：同行「名称 + 金额（+币种）」
    for idx in scope:
        if idx in consumed:
            continue
        line, page, line_no = lines[idx]
        stripped = line.strip()
        if len(stripped) < 4 or TOTAL_LINE_RE.match(stripped):
            continue
        match = _TRAILING_MONEY_RE.match(stripped)
        if not match:
            continue
        raw_name = _strip_name_noise(match.group("name"))
        if not _valid_charge_name(raw_name):
            continue
        amount, conf = parse_amount(match.group("amount"))
        if amount is None or not _amount_plausible(amount, match.group("amount")):
            continue
        if strict and not _line_has_money_evidence(stripped, match.group("amount")):
            continue
        charges.append(
            ChargeCandidate(
                raw_name=raw_name,
                amount=amount,
                currency=match.group("currency") or _currency_from_amount(match.group("amount")),
                payment_term=_term_before_amount(match.group("name")),
                span=stripped,
                source_path=source_path,
                page=page,
                line_no=line_no,
                confidence=0.75 if conf >= 0.5 else 0.55,
                extractor="rule:line",
            )
        )
        consumed.add(idx)

    # 邻行档：名称行 + 下一行只有金额（同样受区块与页边界约束）
    for idx in scope:
        if idx in consumed or idx + 1 >= len(lines):
            continue
        if regions and idx + 1 not in scope:
            continue
        line, page, line_no = lines[idx]
        name = _strip_name_noise(line)
        if not _valid_charge_name(name) or len(name) > 60:
            continue
        next_line, next_page, next_no = lines[idx + 1]
        next_stripped = next_line.strip()
        if not re.fullmatch(rf"{MONEY}\s*(?:[A-Z]{{3}})?", next_stripped):
            continue
        amount, _ = parse_amount(next_stripped)
        if amount is None or not _amount_plausible(amount, next_stripped):
            continue
        if strict and not _line_has_money_evidence(name, next_stripped):
            continue
        charges.append(
            ChargeCandidate(
                raw_name=name,
                amount=amount,
                span=f"{name} / {next_stripped}",
                source_path=source_path,
                page=next_page or page,
                line_no=next_no or line_no,
                confidence=0.6,
                extractor="rule:adjacent",
            )
        )
        consumed.add(idx)
        consumed.add(idx + 1)

    return charges


# ---------------------------------------------------------------- 主入口


def _lines_of(doc: RawDocument) -> list[tuple[str, int | None, int | None]]:
    if doc.blocks:
        return [
            (b.text, b.page, b.line_no)
            for b in doc.blocks
            if b.kind in {"paragraph", "kv", "table_row"} and b.text.strip()
        ]
    return [
        (line, None, idx)
        for idx, line in enumerate(doc.text.split("\n"), start=1)
        if line.strip()
    ]


def _majority_currency(charges: list[ChargeCandidate], lines: list[str]) -> FieldVal:
    counts: dict[str, int] = {}
    raw_seen: dict[str, str] = {}
    for charge in charges:
        if not charge.currency:
            continue
        code, _ = normalize_currency(charge.currency)
        if code:
            counts[code] = counts.get(code, 0) + 1
            raw_seen.setdefault(code, str(charge.currency))
    if not counts:
        for line in lines:
            for token in re.findall(
                r"\b(USD|CNY|EUR|GBP|HKD|SGD|JPY|AUD|CAD|TWD|THB|MYR|VND)\b", line.upper()
            ):
                counts[token] = counts.get(token, 0) + 1
    if not counts:
        return FieldVal()
    best = max(counts.items(), key=lambda item: item[1])
    return FieldVal(
        value=raw_seen.get(best[0], best[0]),
        extractor="rule",
        confidence=0.85,
        span=f"{best[0]} x{best[1]}",
    )


COMPANY_RE = re.compile(
    r"\b(?:LTD|LIMITED|LLC|INC|INCORPORATED|CORP|CORPORATION|GMBH|SHIPPING|LOGISTICS|"
    r"FORWARDING|FREIGHT|LINES?|AGENC\w*|TRADING|MARITIME|EXPRESS)\b|科技|物流|航运|货运|国际",
    re.I,
)


def _find_issuer_by_letterhead(
    lines_with_pos: list[tuple[str, int | None, int | None]]
) -> FieldVal:
    """跨页反复出现的公司名 = 单据抬头（真实账单的签发方通常就这样出现）。"""
    stats: dict[str, dict[str, Any]] = {}
    for text, page, _ in lines_with_pos:
        stripped = text.strip()
        if not (6 <= len(stripped) <= 70):
            continue
        if not COMPANY_RE.search(stripped):
            continue
        if re.search(r"\d{3,}", stripped):  # 地址/电话/编号行
            continue
        key = re.sub(r"\s+", " ", stripped).lower()
        rec = stats.setdefault(key, {"n": 0, "pages": set(), "span": stripped})
        rec["n"] += 1
        if page is not None:
            rec["pages"].add(page)
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for rec in stats.values():
        pages = len(rec["pages"]) or 1
        if rec["n"] >= 3 or (rec["n"] >= 2 and pages >= 2):
            score = (pages, rec["n"])
            if best is None or score > best[0]:
                best = (score, rec)
    if best is None:
        return FieldVal()
    rec = best[1]
    return FieldVal(
        value=rec["span"],
        span=rec["span"],
        extractor="rule:letterhead",
        confidence=0.7,
    )


def _find_issuer(lines_with_pos: list[tuple[str, int | None, int | None]]) -> FieldVal:
    lines = [item[0] for item in lines_with_pos]
    # 1) 明确的签发/收款方标签（beneficiary 放在抬头之后，避开银行收款行）
    for pattern in ISSUER_PATTERNS[:2]:
        regex = re.compile(pattern, re.I)
        for line in lines:
            match = regex.search(line)
            if not match:
                continue
            value = re.sub(r"\s{2,}", " ", (match.group(1) or "").strip().strip(".,;:"))
            if len(value) < 3 or BANK_PARTY_RE.search(value):
                continue
            return FieldVal(value=value, span=line.strip(), extractor="rule", confidence=0.85)
    # 2) 抬头：跨页重复的公司名
    letterhead = _find_issuer_by_letterhead(lines_with_pos)
    if letterhead.value:
        return letterhead
    # 3) beneficiary（排除银行/证券托管这类收款机构）
    regex = re.compile(ISSUER_PATTERNS[2], re.I)
    for line in lines:
        match = regex.search(line)
        if not match:
            continue
        value = re.sub(r"\s{2,}", " ", (match.group(1) or "").strip().strip(".,;:"))
        if len(value) < 3 or BANK_PARTY_RE.search(value) or BANK_PARTY_RE.search(line):
            continue
        return FieldVal(value=value, span=line.strip(), extractor="rule", confidence=0.75)
    return FieldVal()


def _find_recipient(
    lines_with_pos: list[tuple[str, int | None, int | None]],
    *,
    entity: str | None,
    entity_aliases: list[str] | None,
    issuer: FieldVal,
) -> FieldVal:
    labeled = _find_labeled([item[0] for item in lines_with_pos], RECIPIENT_PATTERNS, confidence=0.9)
    if labeled.value:
        return labeled
    if not entity:
        return FieldVal()
    # 主体名字确实出现在单据上、且不是签发方 → 只能作为「推断」，不等于单据声明的接收方
    if issuer.value and _same_party(issuer.value, entity):
        return FieldVal()
    needles = [entity, *(entity_aliases or [])]
    watermark = _watermark_line_indices([item[0] for item in lines_with_pos])
    for index, (text, _page, _line_no) in enumerate(lines_with_pos):
        if index in watermark:
            # 水印/落款块里出现的公司名是 SaaS 供应商，不是交易方
            continue
        if REMARK_LINE_RE.search(text) or ISSUER_LINE_RE.search(text):
            # 备注句、签发方那一行都不构成「接收方声明」
            continue
        haystack = text.lower()
        for needle in needles:
            token = (needle or "").strip().lower()
            if len(token) >= 3 and token in haystack:
                return FieldVal(
                    value=entity,
                    span=text.strip(),
                    extractor="infer:entity_in_doc",
                    confidence=0.6,
                )
    return FieldVal()


def extract_by_rules(
    doc: RawDocument,
    *,
    ctx: DocumentContext | None = None,
    source_path: str | None = None,
    entity: str | None = None,
    entity_aliases: list[str] | None = None,
    body_charges: bool = False,
) -> Extraction:
    path = source_path or doc.source.path
    lines_with_pos = _lines_of(doc)
    lines = [item[0] for item in lines_with_pos]

    scalars: dict[str, FieldVal] = {}
    scalars["document_type"] = _find_document_type(lines)
    scalars["reference_no"] = _find_reference_no(lines)
    scalars["document_date"] = _find_date(lines)
    scalars["issuer"] = _find_issuer(lines_with_pos)
    scalars["recipient"] = _find_recipient(
        lines_with_pos,
        entity=entity,
        entity_aliases=entity_aliases,
        issuer=scalars["issuer"],
    )
    scalars["bl_no"] = _find_labeled(lines, BL_PATTERNS)
    scalars["booking_no"] = _find_labeled(lines, BOOKING_PATTERNS)
    scalars["container_no"] = _find_labeled(lines, CONTAINER_PATTERNS)
    scalars["subtotal"] = _find_money_line(lines, SUBTOTAL_PATTERNS)
    scalars["tax"] = _find_money_line(lines, TAX_PATTERNS)
    scalars["total"] = _find_total(lines)
    scalars["payment_term"] = _find_payment_term(lines)

    charges: list[ChargeCandidate] = []
    for table in doc.tables:
        charges.extend(_charges_from_table(table, source_path=path))
    if not charges:
        regions = _charge_regions(lines_with_pos)
        if regions:
            charges = _charges_from_lines(lines_with_pos, source_path=path, regions=regions)
        elif _is_transport_document(lines):
            # 提单/运单是运输凭证，本身不含费用明细；硬抽只会把重量、地址当金额
            charges = []
        else:
            charges = _charges_from_lines(lines_with_pos, source_path=path, strict=True)

    currency_label = FieldVal()
    for pattern in CURRENCY_LABEL_PATTERNS:
        regex = re.compile(pattern)
        for line in lines:
            match = regex.search(line.upper())
            if match:
                code, _ = normalize_currency(match.group(1))
                if code:
                    currency_label = FieldVal(
                        value=code, span=line.strip(), extractor="rule", confidence=0.9
                    )
                    break
        if currency_label.value:
            break
    scalars["currency"] = currency_label if currency_label.value else _majority_currency(charges, lines)

    # 用 ctx（EML 头/正文）补缺，置信度降档并标注来源
    if ctx is not None:
        _fill_from_context(scalars, charges, ctx, include_charges=body_charges)

    for key, field in scalars.items():
        if field.value is not None and not field.source_path:
            field.source_path = path

    for charge in charges:
        charge.source_path = charge.source_path or path

    return Extraction(
        scalars=scalars,
        charges=charges,
        extractor="rule",
        totals_by_currency=_totals_by_currency(lines),
    )


def _clean_party(value: str | None) -> str | None:
    """把 `"Name" <mail@x>` 这类邮件头清洗成可用的交易方名称。

    只有显示名能用才返回；显示名本身是邮箱地址时返回 None（邮箱不是主体名称）。
    """
    text = (value or "").strip()
    if not text:
        return None
    angled = re.match(r"^(.*?)<[^>]*>\s*$", text)
    if angled:
        name = angled.group(1).strip().strip('"').strip("'").strip()
        if name and "@" not in name:
            return name
        return None
    if "@" in text and " " not in text:
        return None
    return text


def _fill_from_context(
    scalars: dict[str, FieldVal],
    charges: list[ChargeCandidate],
    ctx: DocumentContext,
    *,
    include_charges: bool = False,
) -> None:
    """邮件上下文只做「补缺」，不覆盖文档自身的抽取结果。"""
    origin_label = f"{ctx.origin}:email_header"

    sender = _clean_party(ctx.sender)
    if scalars.get("issuer") and not scalars["issuer"].value and sender:
        scalars["issuer"] = FieldVal(
            value=sender, span=f"From: {ctx.sender}", extractor="rule:email_header",
            confidence=0.7, source_path=origin_label,
        )
    to_party = _clean_party(ctx.recipient)
    if scalars.get("recipient") and not scalars["recipient"].value and to_party:
        scalars["recipient"] = FieldVal(
            value=to_party, span=f"To: {ctx.recipient}", extractor="rule:email_header",
            confidence=0.7, source_path=origin_label,
        )
    if scalars.get("document_date") and not scalars["document_date"].value and ctx.date:
        normalized = parse_date_string(ctx.date)
        if normalized:
            scalars["document_date"] = FieldVal(
                value=normalized, span=f"Date: {ctx.date}", extractor="rule:email_date",
                confidence=0.6, source_path=origin_label,
            )
    if scalars.get("reference_no") and not scalars["reference_no"].value and ctx.subject:
        ref = extract_ref_from_subject(ctx.subject)
        if ref:
            scalars["reference_no"] = FieldVal(
                value=ref, span=f"Subject: {ctx.subject}", extractor="rule:email_header",
                confidence=0.65, source_path=origin_label,
            )
    # 邮件标题常承载关单三要素（CNTR NO. / MBL NO. / BOOKING NO.），正文反而不一定有。
    # 只补这三个标识字段：标题里的 "Arrived time" 不该被当单据日期，收件人也不该参与判向。
    if ctx.subject:
        for field_name, patterns in (
            ("bl_no", BL_PATTERNS),
            ("booking_no", BOOKING_PATTERNS),
            ("container_no", CONTAINER_PATTERNS),
        ):
            existing = scalars.get(field_name)
            if existing is not None and existing.value:
                continue
            found = _find_labeled([ctx.subject], patterns)
            if not found.value:
                continue
            found.extractor = "rule:email_subject"
            found.confidence = min(found.confidence or 0.7, 0.75)
            found.span = f"Subject: {ctx.subject[:120]}"
            found.source_path = origin_label
            scalars[field_name] = found
    if include_charges and not charges and ctx.body_text:
        body_lines = [
            (line, None, idx)
            for idx, line in enumerate(ctx.body_text.split("\n"), start=1)
            if line.strip()
        ]
        charges.extend(_charges_from_lines(body_lines, source_path=origin_label))
