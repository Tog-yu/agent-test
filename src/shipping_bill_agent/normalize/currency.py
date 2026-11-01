"""币种归一：符号/单词 → ISO 4217 三位代码，白名单校验。"""

from __future__ import annotations

import re

# ISO 4217 现行代码（含少数历史代码以便容错）
ISO4217: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV
    BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE
    CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD
    HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD
    KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN
    MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
    RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS
    TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST
    XAF XAG XAU XBA XBB XBC XBD XCD XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW
    ZWG ZWL
    """.split()
)

SYMBOL_MAP: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "USD$": "USD",
    "US DOLLAR": "USD",
    "US DOLLARS": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "美金": "USD",
    "美元": "USD",
    "¥": "CNY",
    "￥": "CNY",
    "RMB": "CNY",
    "CN¥": "CNY",
    "人民币": "CNY",
    "元": "CNY",
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "欧元": "EUR",
    "£": "GBP",
    "STERLING": "GBP",
    "英镑": "GBP",
    "HK$": "HKD",
    "HKD$": "HKD",
    "港币": "HKD",
    "港元": "HKD",
    "S$": "SGD",
    "SGD$": "SGD",
    "新加坡元": "SGD",
    "NT$": "TWD",
    "新台币": "TWD",
    "JPY¥": "JPY",
    "日元": "JPY",
    "₩": "KRW",
    "₹": "INR",
    "฿": "THB",
    "₫": "VND",
}

_UPPER_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
# 紧跟/紧跟数字的三位码才算币种：避免 "ALL CHARGES PREPAID" 被读成阿尔巴尼亚列克
_UPPER_CODE_NEAR_AMOUNT_RE = re.compile(r"\b([A-Z]{3})\b(?=\s*[\d(\-])|(?<=[\d])\s+([A-Z]{3})\b")
# 明确的币种标签：CURRENCY USD / CURR: EUR / 币种 CNY
_LABEL_CODE_RE = re.compile(r"(?:CURRENCY|CURR|CUR|币种)\s*[:：]?\s*([A-Z]{3})\b")
_IN_PARENS_RE = re.compile(r"\(([A-Za-z]{3})\)")

# 非货币代码（ISO 4217 里有、但不是计价货币）
NON_MONETARY_CODES = {"XXX", "XTS", "XAU", "XAG", "XDR", "XPT", "XPD", "XBA", "XBB", "XBC"}
# 既是货币代码、又是常见英文词的：单独出现不足以判定，必须有金额相邻
AMBIGUOUS_WORD_CODES = {"ALL", "TRY", "TOP", "PEN", "AMD", "GEL", "MOP", "BAM", "MDL"}


def _clean(raw: str) -> str:
    return raw.replace("\u00a0", " ").strip()


def normalize_currency(raw: object) -> tuple[str | None, float]:
    """返回 (ISO 代码, 置信度)。未知返回 (None, 0.3)。"""
    if raw is None:
        return None, 0.0
    text = _clean(str(raw))
    if not text:
        return None, 0.0

    upper = text.upper()
    stripped = upper.strip(" .:：,")

    if stripped in NON_MONETARY_CODES:
        return None, 0.3

    if stripped in ISO4217 and stripped not in AMBIGUOUS_WORD_CODES:
        return stripped, 1.0
    if stripped in AMBIGUOUS_WORD_CODES:
        # 整串只有一个词（"ALL" / "TRY"），没有金额相邻就不认
        return None, 0.3

    # 括号内代码：Amount (USD)
    paren = _IN_PARENS_RE.search(text)
    if paren and paren.group(1).upper() in ISO4217:
        return paren.group(1).upper(), 0.95

    for symbol, code in sorted(SYMBOL_MAP.items(), key=lambda kv: len(kv[0]), reverse=True):
        if symbol.upper() in upper or symbol in text:
            return code, 0.9

    # 文本中紧跟金额的三位大写代码（"USD 100" / "100 USD"）
    for match in _UPPER_CODE_NEAR_AMOUNT_RE.finditer(upper):
        code = match.group(1) or match.group(2)
        if code and code in ISO4217 and code not in NON_MONETARY_CODES:
            return code, 0.85

    # 显式标签：CURRENCY USD / 币种 CNY
    label = _LABEL_CODE_RE.search(upper)
    if label and label.group(1) in ISO4217:
        return label.group(1), 0.9

    # OCR 混淆修复：只对「含数字的三位码」在白名单内枚举替换（US0 → USD），
    # 纯字母词一律不动，避免把 "ALL" 这类词硬修成货币代码
    if not stripped.isalpha() and len(stripped) == 3:
        from ..validate.ocr_repair import repair_currency

        repaired = repair_currency(stripped)
        if repaired:
            return repaired, 0.5

    return None, 0.3


def is_iso4217(code: object) -> bool:
    if not code:
        return False
    return str(code).strip().upper() in ISO4217
