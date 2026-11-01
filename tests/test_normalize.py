"""归一化测试：金额、币种、费用名别名（含题目 6 组映射）。"""

from __future__ import annotations

import pytest

from shipping_bill_agent.normalize.amounts import parse_amount, to_number
from shipping_bill_agent.normalize.charge_map import normalize_charge
from shipping_bill_agent.normalize.currency import is_iso4217, normalize_currency


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("40.00", 40.0),
        ("1,234.56", 1234.56),
        ("USD 40.00", 40.0),
        ("(1,234.00)", -1234.0),
        ("123.45-", -123.45),
        ("1.234,56", 1234.56),
        ("USD 1,000", 1000.0),
    ],
)
def test_parse_amount_formats(raw: str, expected: float) -> None:
    value, confidence = parse_amount(raw)
    assert value == pytest.approx(expected)
    assert confidence >= 0.5


def test_parse_amount_rejects_non_numeric() -> None:
    assert parse_amount("N/A") == (None, 0.0)
    assert parse_amount(None) == (None, 0.0)


def test_parse_amount_lowers_confidence_on_noise() -> None:
    value, confidence = parse_amount("4O.OO")
    assert value is not None
    assert confidence < 0.7  # 有字母残留 → 不可信，交给校验闸门


def test_to_number_returns_numeric_types() -> None:
    assert to_number(40.0) == 40
    assert isinstance(to_number(40.5), float)
    assert to_number("1,234.56") == 1234.56
    assert to_number(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USD", "USD"),
        ("US$", "USD"),
        ("美金", "USD"),
        ("RMB", "CNY"),
        ("¥", "CNY"),
        ("(EUR)", "EUR"),
        ("HKD", "HKD"),
    ],
)
def test_normalize_currency(raw: str, expected: str) -> None:
    code, confidence = normalize_currency(raw)
    assert code == expected
    assert confidence >= 0.85


def test_normalize_currency_unknown() -> None:
    code, confidence = normalize_currency("XYZ123")
    assert code is None
    assert confidence < 0.5
    assert is_iso4217("USD") and not is_iso4217("XYZ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Handling Fee", "Handling Charge"),
        ("H/D Fee", "Handling Charge"),
        ("Doc Fee", "Documentation Fee"),
        ("THC", "Terminal Handling Charge"),
        ("Trucking Fee", "Trucking Charge"),
        ("Customs Fee", "Customs Clearance Fee"),
        ("Storage Fee", "Storage Charge"),
        # 模糊 / 关键词
        ("Handing Fee", "Handling Charge"),
        ("Terminal Handling", "Terminal Handling Charge"),
        ("Ocean Freight", "Ocean Freight"),
        ("BAF", "Bunker Adjustment Factor"),
        ("报关费", "Customs Clearance Fee"),
    ],
)
def test_charge_alias_mapping(raw: str, expected: str) -> None:
    canonical, confidence, _method = normalize_charge(raw)
    assert canonical == expected
    assert confidence >= 0.6


def test_charge_unmapped_keeps_raw_name() -> None:
    canonical, confidence, method = normalize_charge("Special Weather Surcharge")
    assert method == "unmapped"
    assert canonical == "Special Weather Surcharge"
    assert confidence < 0.5
