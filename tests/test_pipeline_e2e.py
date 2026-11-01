"""端到端：跑真实样本目录，比对金标，校验契约与审计产物。"""

from __future__ import annotations

import json
from pathlib import Path

from shipping_bill_agent.cli import _check
from shipping_bill_agent.config import Settings
from shipping_bill_agent.pipeline import run

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "data" / "golden" / "expected.json"


def _key(record: dict) -> tuple[str, str]:
    return (
        str(record["document_info"]["reference_no"]),
        str(record["charge_details"]["charge_item"]),
    )


def _run(tmp_path: Path, **overrides):
    settings = Settings().with_overrides(
        pipeline__pdf_strategy="auto", pipeline__accounting_entity="Origin Agent", **overrides
    )
    return run(
        [ROOT / "data" / "samples"],
        settings=settings,
        out_dir=tmp_path / "out",
        bills_path=ROOT / "data" / "existing_bills.json",
    )


def test_end_to_end_matches_golden(tmp_path: Path) -> None:
    result = _run(tmp_path)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["records"]

    actual = {_key(r): r for r in result.records}
    assert set(actual) == {(g["reference_no"], g["charge_item"]) for g in golden}

    for expected in golden:
        got = actual[(expected["reference_no"], expected["charge_item"])]
        details = got["charge_details"]
        assert got["operation"] == expected["operation"], (expected, got["operation"])
        assert details["amount"] == expected["amount"]
        assert details["currency"] == expected["currency"]
        assert details["payment_term"] == expected["payment_term"]
        assert got["financial_attributes"]["posting_type"] == expected["posting_type"]
        assert got["document_info"]["document_type"] == expected["document_type"]
        assert got["document_info"]["document_date"] == expected["document_date"]
        assert got["transaction_party"]["issuer"] == expected["issuer"]
        assert got["transaction_party"]["recipient"] == expected["recipient"]


def test_contract_check_passes(tmp_path: Path) -> None:
    result = _run(tmp_path)
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert _check(payload, allow_extensions=True) == []


def test_artifacts_exist_and_are_json(tmp_path: Path) -> None:
    result = _run(tmp_path)
    for path in (result.result_path, result.report_path, result.errors_path, result.trace_path):
        assert path is not None and path.exists()
        json.loads(path.read_text(encoding="utf-8"))


def test_unsupported_file_does_not_break_batch(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert any("E_UNSUPPORTED_DOC" in item["error"] for item in result.errors)
    assert len(result.records) == 10  # 其他文件照常产出


def test_audit_reasoning_is_non_empty_for_every_record(tmp_path: Path) -> None:
    result = _run(tmp_path)
    for record in result.records:
        lines = record["audit_trail"]["ai_reasoning"]
        assert lines and all(isinstance(line, str) and line.strip() for line in lines)


def test_pending_review_record_has_blocker(tmp_path: Path) -> None:
    result = _run(tmp_path)
    pending = [e for e in result.entries if e.operation == "PENDING_REVIEW"]
    assert pending
    for entry in pending:
        assert entry.has_blocker() or any(i.code == "P-IDENTITY" for i in entry.issues)


def test_strict_schema_strips_extension(tmp_path: Path) -> None:
    settings = Settings().with_overrides(
        pipeline__pdf_strategy="auto", pipeline__strict_schema=True
    )
    result = run(
        [ROOT / "data" / "samples" / "CN-202609-007.png"],
        settings=settings,
        out_dir=tmp_path / "strict",
        bills_path=ROOT / "data" / "existing_bills.json",
    )
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert "charge_item_raw" not in payload[0]["charge_details"]
    assert _check(payload, allow_extensions=False) == []


def test_email_attachment_pipeline(tmp_path: Path) -> None:
    settings = Settings().with_overrides(pipeline__pdf_strategy="auto")
    result = run(
        [ROOT / "data" / "samples" / "eml"],
        settings=settings,
        out_dir=tmp_path / "eml",
        bills_path=ROOT / "data" / "existing_bills.json",
    )
    refs = {r["document_info"]["reference_no"] for r in result.records}
    assert refs == {"DN-202609-001"}
    operations = sorted(r["operation"] for r in result.records)
    assert operations == ["NO_CHANGE", "NO_CHANGE", "UPDATE"]


def test_degraded_flag_recorded_when_no_llm(tmp_path: Path) -> None:
    result = _run(tmp_path)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    # 无 API Key 时必须显式标记降级，而不是静默只用规则
    assert report["degraded"] is True
    assert any("LLM 未启用" in note for note in json.loads(
        result.trace_path.read_text(encoding="utf-8")
    )["notes"])
