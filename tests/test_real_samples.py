"""真实样本回归测试（data/real，来自笔试题网盘附件）。

这三份是真实货代单据，版式和合成样本完全不同：
- debit_note_DC-108478.pdf  → NEPTUNE 出的 Debit Note（原名 PDF 示例账单2.pdf），表头 `DESCRIPTION UNIT RATE QTY P/C DEBIT(+) CREDIT(-)`
- arrival_notice_NEPTUNE.pdf → CMA CGM / NEPTUNE 的 Arrival Notice（原名 PDF示例账单1.pdf），表头 `CHARGES CUR AMOUNT`
- prealert_SSZSE26093128.eml → PRE-ALERT 邮件，正文一行 `SHA pay TAR：H/C $50`，带 2 个 PDF 附件 + 1 张内嵌签名图

这几个断言就是「真实版式不能被改坏」的护栏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipping_bill_agent.config import Settings
from shipping_bill_agent.extract.rule_extractor import extract_by_rules
from shipping_bill_agent.ingest.dispatch import ingest
from shipping_bill_agent.pipeline import run

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "data" / "real"
DEBIT_NOTE = REAL / "debit_note_DC-108478.pdf"
ARRIVAL = REAL / "arrival_notice_NEPTUNE.pdf"
EML = REAL / "prealert_SSZSE26093128.eml"

pytestmark = pytest.mark.skipif(not REAL.exists() or not DEBIT_NOTE.exists(), reason="缺少真实样本")

ENTITY = "PltPlt"
ALIASES = ["湃乐多", "Pilot Aiship", "PLTPLT"]


def _doc(path: Path, tmp_path: Path):
    return ingest(path, workdir=tmp_path / "work", settings=Settings())


def test_debit_note_three_charges(tmp_path: Path) -> None:
    extraction = extract_by_rules(_doc(DEBIT_NOTE, tmp_path))
    assert extraction.get("document_type").value == "Debit Note"
    assert extraction.get("reference_no").value == "DC-108478"
    assert extraction.get("document_date").value == "2026-10-24"
    assert extraction.get("total").value == pytest.approx(1410.40)
    assert [c.amount for c in extraction.charges] == [65.0, 1310.40, 35.0]
    assert [c.raw_name for c in extraction.charges][:1] == ["HANDLING CHARGE"]


def test_debit_note_issuer_is_letterhead_not_shipper(tmp_path: Path) -> None:
    """签发方是出单人 NEPTUNE，不是 SHIPPER/CONSIGNEE 这两个货物关系方。"""
    extraction = extract_by_rules(_doc(DEBIT_NOTE, tmp_path))
    assert extraction.get("issuer").value == "NEPTUNE SHIPPING LIMITED"
    assert "YIWU" not in str(extraction.get("issuer").value)


def test_debit_note_charges_reconcile_with_total(tmp_path: Path) -> None:
    """地址、电话、账号、路由号都不能被当成费用行——否则明细合计对不上总计。"""
    extraction = extract_by_rules(_doc(DEBIT_NOTE, tmp_path))
    assert sum(c.amount for c in extraction.charges) == pytest.approx(1410.40)


def test_arrival_notice_uses_charges_block(tmp_path: Path) -> None:
    extraction = extract_by_rules(_doc(ARRIVAL, tmp_path))
    assert extraction.get("document_type").value == "Arrival Notice"
    assert extraction.get("total").value == pytest.approx(245.0)
    amounts = [c.amount for c in extraction.charges]
    assert amounts == [245.0]  # 只有 CHARGES 区块里那一行，页面正文的 $25/$1100/$150 不算


def test_eml_parses_attachments_and_skips_inline_signature(tmp_path: Path) -> None:
    assert EML is not None
    doc = ingest(EML, workdir=tmp_path / "work", settings=Settings())
    attachments = doc.meta["attachments"]
    assert len(doc.children) == 2, attachments  # MBL + HBL 两个 PDF 附件
    assert all(child.doc_kind == "pdf" for child in doc.children)
    assert "InsertPic_C25B.jpg" in doc.meta.get("inline_images", [])


def test_eml_body_charge_not_duplicated(tmp_path: Path) -> None:
    """正文那一条费用只能出一次，不能因为有两个附件就成两条。"""
    assert EML is not None
    result = run(
        [EML],
        settings=Settings().with_overrides(
            pipeline__pdf_strategy="auto",
            pipeline__accounting_entity=ENTITY,
            pipeline__entity_aliases=ALIASES,
        ),
        out_dir=tmp_path / "out",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["charge_details"]["amount"] == 50.0
    assert payload[0]["charge_details"]["currency"] == "USD"
    assert payload[0]["charge_details"]["charge_item"] == "Handling Charge"


def test_real_batch_posting_types(tmp_path: Path) -> None:
    """真实批次里 PltPlt 只是 SaaS 供应商，不在任何一张单的交易链上 → 一律不判向。

    这条早先断言「3 条 AP」，依据是 Debit Note 页脚出现的
    `NEPTUNE SHIPPING LIMITED (LAX) Powered by / 湃乐多（上海）航运科技有限公司`。
    但那是**单据系统的水印**，不是「单据开给湃乐多」。水印修复后
    `recipient` 归 null，AR/AP 按规则不再判——这是把「猜对」改成「不猜」。
    """
    result = run(
        [REAL],
        settings=Settings().with_overrides(
            pipeline__pdf_strategy="auto",
            pipeline__accounting_entity=ENTITY,
            pipeline__entity_aliases=ALIASES,
        ),
        out_dir=tmp_path / "batch",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))

    assert len(payload) == 7
    assert all(r["financial_attributes"]["posting_type"] is None for r in payload)
    assert all(r["operation"] == "PENDING_REVIEW" for r in payload)

    # 判不出的理由必须写清「不在本单交易链上」，而不是一句笼统的「无法判断」
    report = json.loads((tmp_path / "batch" / "report.json").read_text(encoding="utf-8"))
    reasons = [
        i["message"]
        for r in report["records"]
        for i in r["issues"]
        if i["code"] == "F-ARAP_UNKNOWN"
    ]
    assert reasons
    assert any("不在本单交易链上" in m or "未识别到签发方与接收方" in m or "单据类型未知" in m
               for m in reasons)

    errors = json.loads(result.errors_path.read_text(encoding="utf-8"))
    assert not errors, f"不该再有解析错误：{errors}"


def test_ar_ap_decides_when_entity_is_in_the_chain(tmp_path: Path) -> None:
    """同一份 Debit Note：换成签发方自己记账 → 判 AR + CREATE，自动入账。

    这条用来证明「AR/AP 判向」本身是工作的。真实批次全 null 不是能力缺失，
    而是那批文件里没有一张是开给 PltPlt 的（它只以 `Powered by` 水印出现）。
    """
    result = run(
        [REAL / "debit_note_DC-108478.pdf"],
        settings=Settings().with_overrides(
            pipeline__pdf_strategy="auto",
            pipeline__accounting_entity="NEPTUNE SHIPPING LIMITED",
        ),
        out_dir=tmp_path / "self",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert payload, "应有记录"
    assert all(r["financial_attributes"]["posting_type"] == "AR" for r in payload)
    assert all(r["operation"] == "CREATE" for r in payload)

    # 换回 PltPlt：同一份文件立刻变成「不在交易链上」
    other = run(
        [REAL / "debit_note_DC-108478.pdf"],
        settings=Settings().with_overrides(
            pipeline__pdf_strategy="auto", pipeline__accounting_entity="PltPlt"
        ),
        out_dir=tmp_path / "other",
    )
    other_payload = json.loads(other.result_path.read_text(encoding="utf-8"))
    assert all(r["financial_attributes"]["posting_type"] is None for r in other_payload)


def test_scan_uses_real_gateway_cassette(tmp_path: Path) -> None:
    """扫描件走真实网关响应（data/ocr_cassettes 里的 f3ca6432…），抽出 2 条 wharfage。

    这份响应是 2026-11-01 把 zim_invoice_scan.png 传公网后真机调网关拿到的，
    不是伪造的：chargeList = [{"chargeItem": "WHARFAGE ADMINISTRATION FEE", "amount": "5.00"},
                              {"chargeItem": "WHARFAGE", "amount": "21.90"}]
    合计 26.90，与单据 TOTAL 一致。
    """
    result = run(
        [REAL / "zim_invoice_scan.png"],
        settings=Settings().with_overrides(pipeline__accounting_entity=ENTITY),
        out_dir=tmp_path / "scan",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert {r["document_info"]["reference_no"] for r in payload} == {"QRF1618731"}
    assert sorted(r["charge_details"]["amount"] for r in payload) == [5.0, 21.9]
    assert sum(r["charge_details"]["amount"] for r in payload) == pytest.approx(26.90)
    # 网关的 chargeList 里没有币种字段 → 必须置 null，不能凭空补 USD
    assert all(r["charge_details"]["currency"] is None for r in payload)
    assert not json.loads(result.errors_path.read_text(encoding="utf-8"))


def test_bill_of_lading_attachment_produces_no_charge(tmp_path: Path) -> None:
    """MBL/HBL 是提单，没有费用明细；不能把重量、地址、邮编当金额抽出来。"""
    assert EML is not None
    doc = ingest(EML, workdir=tmp_path / "work", settings=Settings())
    for child in doc.children:
        extraction = extract_by_rules(child)
        assert extraction.charges == []


def test_pdf_ocr_does_not_override_text_layer(tmp_path: Path) -> None:
    """PDF 有文本层时，OCR 结果里「更差」的部分不得覆盖文本层。

    真实对照（同一份 debit_note_DC-108478.pdf，2026-11-01 真机 OCR）：

        文本层 → 单号 DC-108478 / 币种 USD / 明细 65 + 1310.40 + 35（3 条）
        OCR   → 单号空     / 币种空  / 明细 100 + 1310.40（2 条，65+35 被合并）

    OCR 覆盖文本层会直接丢单号（T1 匹配的必需键）并合并明细，
    所以策略是「文本层为主源，OCR 只补标识字段」。
    """
    result = run(
        [REAL / "debit_note_DC-108478.pdf"],
        settings=Settings().with_overrides(pipeline__accounting_entity=ENTITY),
        out_dir=tmp_path / "dn",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))

    # 1) 明细粒度来自文本层：3 条，不是 OCR 合并后的 2 条
    assert len(payload) == 3
    assert sorted(r["charge_details"]["amount"] for r in payload) == [35.0, 65.0, 1310.4]
    # 2) 单号和币种来自文本层（OCR 这两项都是空的）
    assert {r["document_info"]["reference_no"] for r in payload} == {"DC-108478"}
    assert all(r["charge_details"]["currency"] == "USD" for r in payload)
    # 3) 总额勾稽
    assert sum(r["charge_details"]["amount"] for r in payload) == pytest.approx(1410.40)

    # 4) 网关确实被调用过（题目要求 PDF 走 OCR），且没有报错
    assert not json.loads(result.errors_path.read_text(encoding="utf-8"))
    trace = json.loads(next((tmp_path / "dn").glob("trace-*.json")).read_text(encoding="utf-8"))
    assert trace["files"][0]["ocr_call"] is True
    assert trace["counters"]["ocr_calls"] >= 1
    assert any("以文本层为主源" in note for note in trace["notes"])


def test_raw_snapshot_contains_every_provenance_span(tmp_path: Path) -> None:
    """审计闭环：report.json 里每个 provenance.span 都必须能在原文快照里检索到。

    这是「这条记录凭什么入账」的最后一环 —— 没有完整原文，span 只是孤立的半句话，
    复核的人无法确认它是不是被断章取义。
    """
    result = run(
        [REAL / "debit_note_DC-108478.pdf"],
        settings=Settings().with_overrides(pipeline__accounting_entity=ENTITY),
        out_dir=tmp_path / "dn",
    )
    report = json.loads((tmp_path / "dn" / "report.json").read_text(encoding="utf-8"))
    trace = json.loads(next((tmp_path / "dn").glob("trace-*.json")).read_text(encoding="utf-8"))

    snapshots = trace["files"][0]["raw_snapshots"]
    assert snapshots, "应落盘原文快照"
    raw_text = (tmp_path / "dn" / snapshots[0]).read_text(encoding="utf-8")

    # 头部元信息齐备
    assert "data/real/debit_note_DC-108478.pdf" in raw_text
    assert "# sha256" in raw_text and "# text_from" in raw_text

    for record in report["records"]:
        for field, prov in (record.get("provenance") or {}).items():
            span = (prov or {}).get("span") or ""
            if not span.strip():
                continue
            assert span.strip() in raw_text, f"{field} 的 span 在原文里找不到：{span[:60]}"


def test_keep_raw_text_can_be_disabled(tmp_path: Path) -> None:
    """敏感场景可以关掉原文落盘（只留 span）。"""
    result = run(
        [REAL / "debit_note_DC-108478.pdf"],
        settings=Settings().with_overrides(
            pipeline__accounting_entity=ENTITY, pipeline__keep_raw_text=False
        ),
        out_dir=tmp_path / "off",
    )
    trace = json.loads(next((tmp_path / "off").glob("trace-*.json")).read_text(encoding="utf-8"))
    assert trace["files"][0]["raw_snapshots"] == []
    assert not (tmp_path / "off" / "raw").exists()
    # 关掉原文快照不影响抽取结果
    assert len(json.loads((tmp_path / "off" / "result.json").read_text(encoding="utf-8"))) == 3
