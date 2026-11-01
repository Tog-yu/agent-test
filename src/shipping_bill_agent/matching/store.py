"""历史账单仓：加载、键归一、按键查询。"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..normalize.amounts import parse_amount
from ..normalize.charge_map import normalize_charge

# 分隔符在比较时一律抹掉：DN-202609-001 / DN/202609/001 / DN 202609 001 视为同一个单号
_SEP_RE = re.compile(r"[\s/\-_.\\]+")


def normalize_key(value: Any) -> str | None:
    """匹配键归一：全角转半角、小写、抹掉空白与 -/_. 等分隔符、去首尾标点。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    if not text:
        return None
    text = _SEP_RE.sub("", text)
    return text.strip(".,;:，。；：|")


def charge_keys(*values: Any) -> set[str]:
    """费用名比较键：标准名、原始名、原始名再归一后的结果都算同一项。"""
    keys: set[str] = set()
    for value in values:
        if not value:
            continue
        key = normalize_key(value)
        if key:
            keys.add(key)
        canonical, _, _ = normalize_charge(str(value))
        canonical_key = normalize_key(canonical)
        if canonical_key:
            keys.add(canonical_key)
    return keys


@dataclass
class BillRecord:
    bill_id: str
    reference_no: str | None = None
    document_type: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    bl_no: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    charge_item: str | None = None
    charge_item_raw: str | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    tax: float | None = None
    accounting_entity: str | None = None
    posting_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> "BillRecord":
        amount, _ = parse_amount(data.get("amount"))
        quantity, _ = parse_amount(data.get("quantity"))
        unit_price, _ = parse_amount(data.get("unit_price"))
        tax, _ = parse_amount(data.get("tax"))
        return cls(
            bill_id=str(data.get("bill_id") or f"BILL-{index:04d}"),
            reference_no=data.get("reference_no"),
            document_type=data.get("document_type"),
            document_date=data.get("document_date"),
            issuer=data.get("issuer"),
            recipient=data.get("recipient"),
            bl_no=data.get("bl_no"),
            booking_no=data.get("booking_no"),
            container_no=data.get("container_no"),
            charge_item=data.get("charge_item"),
            charge_item_raw=data.get("charge_item_raw"),
            amount=amount,
            currency=data.get("currency"),
            payment_term=data.get("payment_term"),
            quantity=quantity,
            unit_price=unit_price,
            tax=tax,
            accounting_entity=data.get("accounting_entity"),
            posting_type=data.get("posting_type"),
            raw=dict(data),
        )

    def value_of(self, key: str) -> Any:
        return getattr(self, key, None)

    def as_original(self) -> dict[str, Any]:
        """返回可直接用于字段级 patch 的原始字段快照。"""
        return {
            "document_type": self.document_type,
            "reference_no": self.reference_no,
            "document_date": self.document_date,
            "issuer": self.issuer,
            "recipient": self.recipient,
            "bl_no": self.bl_no,
            "booking_no": self.booking_no,
            "container_no": self.container_no,
            "charge_item": self.charge_item,
            "charge_item_raw": self.charge_item_raw,
            "amount": self.amount,
            "currency": self.currency,
            "payment_term": self.payment_term,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "tax": self.tax,
        }


class ExistingBillStore:
    def __init__(self, records: list[BillRecord] | None = None) -> None:
        self.records: list[BillRecord] = records or []
        self._index: dict[str, list[BillRecord]] = {}
        for record in self.records:
            key = normalize_key(record.reference_no)
            if key:
                self._index.setdefault(key, []).append(record)

    @classmethod
    def from_json(cls, path: str | Path | None) -> "ExistingBillStore":
        if not path:
            return cls([])
        target = Path(path)
        if not target.exists():
            return cls([])
        raw = json.loads(target.read_text(encoding="utf-8"))
        rows: list[dict[str, Any]] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key in ("bills", "records", "data", "items"):
                if isinstance(raw.get(key), list):
                    rows = [r for r in raw[key] if isinstance(r, dict)]
                    break
        return cls([BillRecord.from_dict(row, index) for index, row in enumerate(rows)])

    def query(self, keys: tuple[str, ...], entry: Any) -> list[BillRecord]:
        """按匹配键组合查询，值为空则视为不命中。"""
        entry_values: dict[str, str] = {}
        for key in keys:
            value = getattr(entry, key, None)
            normalized = normalize_key(value)
            if normalized is None:
                return []
            entry_values[key] = normalized

        candidates: list[BillRecord] = []
        # 有 reference_no 时先走索引缩小范围
        pool = self.records
        ref_key = entry_values.get("reference_no")
        if ref_key and ref_key in self._index:
            pool = self._index[ref_key]

        for record in pool:
            if _record_matches(record, entry_values, entry):
                candidates.append(record)
        return _unique(candidates)

    def find_by_reference(self, reference_no: Any) -> list[BillRecord]:
        key = normalize_key(reference_no)
        if not key:
            return []
        return list(self._index.get(key, []))

    def get(self, bill_id: str | None) -> BillRecord | None:
        if not bill_id:
            return None
        for record in self.records:
            if record.bill_id == bill_id:
                return record
        return None

    def has_duplicate(self, entry: Any) -> list[BillRecord]:
        """跨历史账单判重：同单号 + 同费用 + 同金额 + 同币种。"""
        ref = normalize_key(getattr(entry, "reference_no", None))
        if not ref:
            return []
        wanted = charge_keys(getattr(entry, "charge_item", None), getattr(entry, "charge_item_raw", None))
        return [
            record
            for record in self._index.get(ref, [])
            if wanted & charge_keys(record.charge_item, record.charge_item_raw)
            and _amount_equal(record.amount, getattr(entry, "amount", None))
            and normalize_key(record.currency) == normalize_key(getattr(entry, "currency", None))
        ]

    def __len__(self) -> int:
        return len(self.records)


def _record_matches(record: BillRecord, entry_values: dict[str, str], entry: Any) -> bool:
    """逐键比较。charge_item 用「标准名/原始名/再归一」三路键；amount 走数值比较。"""
    for key, value in entry_values.items():
        if key == "charge_item":
            if value not in charge_keys(record.charge_item, record.charge_item_raw):
                return False
        elif key == "amount":
            if not _amount_equal(record.amount, getattr(entry, "amount", None)):
                return False
        elif normalize_key(record.value_of(key)) != value:
            return False
    return True


def _amount_equal(left: Any, right: Any, tolerance: float = 0.005) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _unique(records: list[BillRecord]) -> list[BillRecord]:
    seen: set[str] = set()
    out: list[BillRecord] = []
    for record in records:
        if record.bill_id in seen:
            continue
        seen.add(record.bill_id)
        out.append(record)
    return out
