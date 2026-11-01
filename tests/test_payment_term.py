"""付款方式归一：P/C 列 → PP / CC。

真实账单（data/real/debit_note_DC-108478.pdf）费用区块的表头是

    DESCRIPTION  UNIT  RATE  QTY  P/C  DEBIT(+)  CREDIT(-)

明细行 P/C 列只写一个 `C`（Collect=到付）；契约里必须是 `CC`。
之前规则只认字面 PP/CC，`C` 被丢掉 → 三行费用付款方式全 null。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipping_bill_agent.config import Settings
from shipping_bill_agent.extract.reconcile import reconcile
from shipping_bill_agent.extract.rule_extractor import extract_by_rules
from shipping_bill_agent.ingest.dispatch import ingest
from shipping_bill_agent.normalize.payment import normalize_payment_term
from shipping_bill_agent.pipeline import run
from shipping_bill_agent.schema import ChargeCandidate, Extraction, FieldVal

ROOT = Path(__file__).resolve().parents[1]
DEBIT_NOTE = ROOT / "data" / "real" / "debit_note_DC-108478.pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C", "CC"),
        ("P", "PP"),
        ("CC", "CC"),
        ("PP", "PP"),
        ("c", "CC"),
        ("Collect", "CC"),
        ("COLLECT", "CC"),
        ("PREPAID", "PP"),
        ("pre-paid", "PP"),
        ("FREIGHT COLLECT", "CC"),
        ("ALL CHARGES PREPAID", "PP"),
        ("到付", "CC"),
        ("预付", "PP"),
        # 表头本身不是取值；认不出的写法必须返回 None，不能猜
        ("P / C", None),
        ("COD", None),
        ("HANDLING CHARGE", None),
        ("", None),
        (None, None),
    ],
)
def test_variants(raw: object, expected: str | None) -> None:
    assert normalize_payment_term(raw) == expected


@pytest.mark.skipif(not DEBIT_NOTE.exists(), reason="缺少真实样本")
def test_real_debit_note_pc_column(tmp_path: Path) -> None:
    """P/C 列被挤进无分隔文本行（`… 1.00 C 65.00`）时仍要认成 CC。"""
    doc = ingest(DEBIT_NOTE, workdir=tmp_path / "work", settings=Settings())
    extraction = extract_by_rules(doc)
    assert [c.payment_term for c in extraction.charges] == ["CC", "CC", "CC"]


@pytest.mark.skipif(not DEBIT_NOTE.exists(), reason="缺少真实样本")
def test_end_to_end_charge_writes_cc(tmp_path: Path) -> None:
    result = run(
        [DEBIT_NOTE],
        settings=Settings().with_overrides(pipeline__accounting_entity="PltPlt"),
        out_dir=tmp_path / "dn",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert payload
    assert {r["charge_details"]["payment_term"] for r in payload} == {"CC"}


def test_other_documents_are_not_forced_to_cc(tmp_path: Path) -> None:
    """没有 P/C 列的账单不能被硬塞 CC（Arrival Notice 就该是 null）。"""
    arrival = ROOT / "data" / "real" / "arrival_notice_NEPTUNE.pdf"
    if not arrival.exists():
        pytest.skip("缺少真实样本")
    doc = ingest(arrival, workdir=tmp_path / "work", settings=Settings())
    extraction = extract_by_rules(doc)
    assert [c.payment_term for c in extraction.charges] == [None]


def test_reconcile_treats_c_and_collect_as_same() -> None:
    rule = Extraction(
        scalars={"payment_term": FieldVal(value="C", confidence=0.85)},
        charges=[ChargeCandidate(raw_name="Handling Fee", amount="65.00")],
    )
    llm = Extraction(
        scalars={"payment_term": FieldVal(value="Collect", confidence=0.85, extractor="llm")},
        charges=[],
        extractor="llm",
    )

    merged, issues = reconcile(rule, llm, settings=Settings())

    assert not [issue for issue in issues if issue.code == "V-RECONFLICT"]
    assert merged.get("payment_term").value == "CC"
