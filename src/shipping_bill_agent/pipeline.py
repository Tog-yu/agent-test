"""主管线：摄入 → OCR → 抽取 → 对账 → 归一 → 校验 → 匹配 → 决策 → 输出。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import FileRecord, Recorder
from .config import Settings
from .decide.planner import plan
from .errors import CassetteMissError, OcrError
from .extract.llm_extractor import LlmExtractor
from .extract.ocr_merge import merge_ocr_supplement
from .extract.reconcile import reconcile
from .extract.rule_extractor import extract_by_rules
from .ingest.dispatch import expand_inputs, ingest
from .matching.matcher import match
from .matching.store import BillRecord, ExistingBillStore
from .normalize.amounts import parse_amount, to_number
from .normalize.charge_map import normalize_charge
from .normalize.currency import normalize_currency
from .normalize.payment import normalize_payment_term
from .ocr.normalize import gateway_error, normalize_ocr
from .ocr.providers import build_provider
from .schema import (
    BillEntry,
    ChargeCandidate,
    DocumentContext,
    Extraction,
    FieldVal,
    Issue,
    OutputAuditTrail,
    OutputChargeDetails,
    OutputDocumentInfo,
    OutputFinancialAttributes,
    OutputRecord,
    OutputTransactionParty,
    Provenance,
    RawDocument,
)
from .validate.validators import DocTotals, validate_entries

BILL_SOURCE_KINDS = {"pdf", "image", "excel", "word"}
MAX_PAGE_IN_TEXT = 120


@dataclass
class RunResult:
    entries: list[BillEntry] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    out_dir: Path | None = None
    result_path: Path | None = None
    report_path: Path | None = None
    errors_path: Path | None = None
    trace_path: Path | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    recorder: Recorder | None = None

    @property
    def degraded(self) -> bool:
        return any(entry.degraded for entry in self.entries)


# ---------------------------------------------------------------- OCR


def _needs_ocr(doc: RawDocument, settings: Settings) -> bool:
    if doc.doc_kind == "image":
        return True
    if doc.doc_kind != "pdf":
        return False
    strategy = settings.pipeline.pdf_strategy
    text_chars = int(doc.meta.get("text_chars") or 0)
    min_chars = settings.pipeline.pdf_text_layer_min_chars
    has_text = bool(doc.meta.get("has_text_layer")) and text_chars >= min_chars
    if strategy == "text":
        return False
    if strategy == "auto":
        return not has_text
    return True


def _looks_like_transport_doc(root: RawDocument) -> bool:
    """整份输入（含附件）都是提单/运单 → 没有费用是预期，不是解析失败。"""
    from .extract.rule_extractor import TRANSPORT_DOC_RE

    nodes = [node for node in root.walk() if (node.text or "").strip()]
    if not nodes:
        return False
    return all(TRANSPORT_DOC_RE.search((node.text or "")[:6000]) for node in nodes)


def _has_usable_text_layer(doc: RawDocument, settings: Settings) -> bool:
    """PDF 的文本层够不够撑起抽取（不够就当扫描件）。"""
    if doc.doc_kind != "pdf" or not doc.meta.get("has_text_layer"):
        return False
    text_chars = int(doc.meta.get("text_chars") or 0)
    return (
        text_chars >= settings.pipeline.pdf_text_layer_min_chars
        and bool((doc.text or "").strip())
    )


def _ocr_failed(doc: RawDocument, message: str, *, settings: Settings, recorder: Recorder) -> None:
    """OCR 失败后的收尾：能回退文本层就回退，回退不了才是真失败。

    默认策略是 ocr（题目要求 PDF 走 OCR），但「网关挂了」不该等于「整批文档都没了」。
    PDF 解析阶段已经把文本层留在 doc.text 里了，只要字符数够就直接沿用，
    并在 meta 里留标记，让 trace / report 能明确区分「OCR 结果」和「回退的文本层结果」。
    """
    path = Path(doc.source.path)
    text_chars = int(doc.meta.get("text_chars") or 0)
    if not _has_usable_text_layer(doc, settings):
        # 扫描件没有文本层可回退 —— 这是真失败，必须进 errors.json
        doc.errors.append(message)
        return
    doc.meta["ocr"] = {
        "provider": "fallback",
        "fallback": "text_layer",
        "text_chars": text_chars,
        "error": message,
    }
    doc.meta["ocr_fallback"] = message
    recorder.note(f"OCR 失败已回退文本层：{path.name}（{message[:90]}）")


def _apply_ocr(
    doc: RawDocument,
    *,
    settings: Settings,
    provider: Any,
    recorder: Recorder,
) -> None:
    path = Path(doc.source.path)
    recorder.bump("ocr_calls")
    try:
        raw = provider.read(path, task=settings.ocr.task)
    except CassetteMissError as exc:
        recorder.bump("ocr_errors")
        _ocr_failed(doc, f"{exc.code}: {exc}", settings=settings, recorder=recorder)
        return
    except (OcrError, OSError) as exc:
        recorder.bump("ocr_errors")
        _ocr_failed(doc, f"E_OCR_FAILED: {exc}", settings=settings, recorder=recorder)
        return

    text, blocks, tables = normalize_ocr(raw)
    error = gateway_error(raw) if isinstance(raw, dict) else None
    if error:
        _ocr_failed(doc, f"E_OCR_FAILED: 网关处理失败：{error}", settings=settings, recorder=recorder)
        return
    if not text and not tables:
        _ocr_failed(doc, "E_OCR_FAILED: OCR 返回内容为空", settings=settings, recorder=recorder)
        return

    # 有文本层的 PDF：文本层做主源，OCR 结果放副源位。
    # 实测依据（docs/REAL_DATA_NOTES.md §9）：同一份 Debit Note，
    #   文本层 → 单号 DC-108478 / 币种 USD / 明细 65 + 1310.40 + 35（3 条）
    #   OCR   → 单号空 / 币种空 / 明细 100 + 1310.40（2 条，65+35 被合并）
    # 让 OCR 覆盖文本层会丢单号（T1 匹配的必需键），所以只把 OCR 当补充源。
    if _has_usable_text_layer(doc, settings):
        doc.ocr_text = text or None
        doc.ocr_blocks = blocks
        doc.ocr_tables = tables
        doc.meta["ocr"] = {
            "provider": provider.name,
            "role": "supplement",
            "blocks": len(blocks),
            "tables": len(tables),
        }
        recorder.note(f"OCR 已执行；该 PDF 有文本层，以文本层为主源，OCR 仅补缺失标识：{path.name}")
        return

    doc.text = text or doc.text
    doc.blocks = blocks or doc.blocks
    doc.tables = tables or doc.tables
    doc.meta["ocr"] = {"provider": provider.name, "role": "primary", "blocks": len(blocks), "tables": len(tables)}


# ---------------------------------------------------------------- 上下文


def _context_from_eml(root: RawDocument) -> DocumentContext | None:
    if root.doc_kind != "eml":
        return None
    meta = root.meta or {}
    return DocumentContext(
        subject=meta.get("subject"),
        sender=meta.get("from"),
        recipient=meta.get("to"),
        date=meta.get("date"),
        body_text=root.text,
        origin=Path(root.source.path).name,
    )


# ---------------------------------------------------------------- 归一 + 组装


def _provenance(field_name: str, field: FieldVal) -> Provenance:
    return Provenance(
        field=field_name,
        source_path=field.source_path,
        page=field.page,
        line_no=field.line_no,
        span=field.span,
        extractor=field.extractor,
        confidence=field.confidence,
    )


def _scalar(extraction: Extraction, name: str, default: Any = None) -> Any:
    field = extraction.scalars.get(name)
    if field and field.value not in (None, ""):
        return field.value
    return default


def _materialize(
    extraction: Extraction,
    *,
    doc: RawDocument,
    ctx: DocumentContext | None,
    recon_issues: list[Issue],
    settings: Settings,
    store: ExistingBillStore,
    degraded: bool,
) -> list[BillEntry]:
    entity = settings.pipeline.accounting_entity
    doc_currency_default, _ = normalize_currency(_scalar(extraction, "currency"))

    subtotal, _ = parse_amount(_scalar(extraction, "subtotal"))
    tax, _ = parse_amount(_scalar(extraction, "tax"))
    total, _ = parse_amount(_scalar(extraction, "total"))
    total_field = extraction.get("total")
    total_currency = None
    if total_field.span:
        total_currency, total_ccy_conf = normalize_currency(total_field.span)
        if total_ccy_conf < 0.5:
            total_currency = None
    totals = DocTotals(
        subtotal=subtotal,
        tax=tax,
        total=total,
        currency=doc_currency_default,
        total_by_currency=dict(extraction.totals_by_currency),
        total_currency=total_currency,
    )

    entries: list[BillEntry] = []
    for charge in extraction.charges:
        entries.append(
            _build_entry(
                extraction,
                charge,
                doc=doc,
                ctx=ctx,
                settings=settings,
                degraded=degraded,
                doc_currency=doc_currency_default,
                entity=entity,
            )
        )

    if not entries:
        return []

    # 校验只标问题、不丢记录：同单重复费用行保留并标 V-DUP_LINE（业务上允许分箱计费）
    validate_entries(entries, totals=totals, settings=settings)
    kept = entries
    for entry in kept:
        entry.issues.extend(recon_issues)

    for entry in kept:
        entry.match = match(entry, store, settings=settings)
        original = store.get(entry.match.bill_id) if entry.match else None
        plan(entry, original=original.as_original() if original else None, settings=settings)
    return kept


def _build_entry(
    extraction: Extraction,
    charge: ChargeCandidate,
    *,
    doc: RawDocument,
    ctx: DocumentContext | None,
    settings: Settings,
    degraded: bool,
    doc_currency: str | None,
    entity: str,
) -> BillEntry:
    entry = BillEntry()
    entry.degraded = degraded
    entry.accounting_entity = entity

    # ---- 单据级
    for name in (
        "document_type", "reference_no", "document_date", "issuer", "recipient",
        "bl_no", "booking_no", "container_no",
    ):
        field = extraction.scalars.get(name)
        if field and field.value not in (None, ""):
            setattr(entry, name, str(field.value).strip())
            entry.provenance[name] = _provenance(name, field)

    # ---- 费用级
    raw_name = (charge.raw_name or "").strip() or None
    canonical, name_conf, method = normalize_charge(raw_name)
    entry.charge_item_raw = raw_name
    entry.charge_item = canonical
    entry.provenance["charge_item"] = Provenance(
        field="charge_item",
        source_path=charge.source_path,
        page=charge.page,
        line_no=charge.line_no,
        span=charge.span,
        extractor=f"normalize:{method}",
        confidence=name_conf,
    )
    if method == "unmapped" and raw_name:
        entry.issues.append(
            Issue(
                code="N-UNMAPPED_CHARGE",
                severity="info",
                message=f"费用名称「{raw_name}」未命中别名表，保留原始名称",
                field="charge_item",
            )
        )
    elif method == "ambiguous" and raw_name:
        entry.issues.append(
            Issue(
                code="N-MULTI_CHARGE",
                severity="warn",
                message=(
                    f"费用名称「{raw_name}」同时命中多个标准名，无法唯一归一，"
                    "已保留原文，请人工拆分后入账"
                ),
                field="charge_item",
            )
        )

    amount, amount_conf = parse_amount(charge.amount)
    entry.amount = amount
    entry.provenance["amount"] = Provenance(
        field="amount",
        source_path=charge.source_path,
        page=charge.page,
        line_no=charge.line_no,
        span=charge.span,
        extractor=charge.extractor,
        confidence=min(charge.confidence, amount_conf if amount_conf else charge.confidence),
    )

    raw_currency = charge.currency or doc_currency
    currency, currency_conf = normalize_currency(raw_currency)
    entry.currency = currency
    # 只有「单据上真有币种」才算有证据；否则不写 provenance，交给 planner 走继承
    if raw_currency:
        entry.provenance["currency"] = Provenance(
            field="currency",
            source_path=charge.source_path or doc.source.path,
            page=charge.page,
            span=raw_currency,
            extractor="normalize:currency",
            confidence=currency_conf,
        )

    # 费用行的 P/C 列取值是 P/C，契约里要 PP/CC；归一不出来的不硬猜
    term = normalize_payment_term(charge.payment_term)
    if not term:
        term = normalize_payment_term(_scalar(extraction, "payment_term"))
    term = term or ""
    entry.payment_term = term or None
    if entry.payment_term:
        source_field = extraction.scalars.get("payment_term")
        entry.provenance["payment_term"] = _provenance(
            "payment_term",
            source_field
            if source_field and source_field.value
            else FieldVal(value=term, span=charge.span, extractor=charge.extractor,
                          confidence=charge.confidence, source_path=charge.source_path),
        )

    quantity, qty_conf = parse_amount(charge.quantity)
    unit_price, price_conf = parse_amount(charge.unit_price)
    entry.quantity = quantity
    entry.unit_price = unit_price
    if quantity is not None:
        entry.provenance["quantity"] = Provenance(
            field="quantity", source_path=charge.source_path, span=charge.span,
            extractor=charge.extractor, confidence=qty_conf,
        )
    if unit_price is not None:
        entry.provenance["unit_price"] = Provenance(
            field="unit_price", source_path=charge.source_path, span=charge.span,
            extractor=charge.extractor, confidence=price_conf,
        )

    entry.source_paths = [doc.source.path]
    return entry


# ---------------------------------------------------------------- 单文件处理


def process_document_tree(
    root: RawDocument,
    *,
    settings: Settings,
    store: ExistingBillStore,
    recorder: Recorder,
    provider: Any,
    llm: LlmExtractor,
    workdir: Path,
) -> list[BillEntry]:
    ctx = _context_from_eml(root)
    all_docs = root.walk()
    leaves = [d for d in all_docs[1:] if d.doc_kind in BILL_SOURCE_KINDS] if ctx else [root]

    entries: list[BillEntry] = []
    for doc in leaves:
        if doc.errors and not doc.text:
            continue
        if _needs_ocr(doc, settings):
            _apply_ocr(doc, settings=settings, provider=provider, recorder=recorder)
        entries.extend(
            _process_single_source(
                doc, ctx=ctx, settings=settings, store=store, recorder=recorder, llm=llm
            )
        )

    # EML 正文自身是否成单：正文费用先抽出来，再按「费用名+金额」与附件结果比对，
    # 只并入附件没覆盖到的（而不是「附件有一条第正文就全丢」）
    if ctx is not None:
        body_doc = root.model_copy(deep=True)
        body_entries = _process_single_source(
            body_doc,
            ctx=ctx,
            settings=settings,
            store=store,
            recorder=recorder,
            llm=llm,
            allow_body=True,
        )
        if body_entries:
            seen = {
                ((entry.charge_item or "").lower().strip(), _amount_key(entry.amount))
                for entry in entries
            }
            added = [
                entry
                for entry in body_entries
                if ((entry.charge_item or "").lower().strip(), _amount_key(entry.amount)) not in seen
            ]
            skipped = len(body_entries) - len(added)
            if added:
                entries.extend(added)
                recorder.note(
                    f"邮件正文另有 {len(added)} 条费用未被附件覆盖，已并入"
                )
            if skipped:
                recorder.note(f"邮件正文有 {skipped} 条费用与附件重复，已跳过")
    return entries


def _amount_key(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    return None


def _process_single_source(
    doc: RawDocument,
    *,
    ctx: DocumentContext | None,
    settings: Settings,
    store: ExistingBillStore,
    recorder: Recorder,
    llm: LlmExtractor,
    allow_body: bool = False,
) -> list[BillEntry]:
    llm_ctx = ctx if (ctx and (allow_body or doc.doc_kind != "eml")) else None
    rule = extract_by_rules(
        doc,
        ctx=llm_ctx,
        entity=settings.pipeline.accounting_entity,
        entity_aliases=settings.pipeline.entity_aliases,
        body_charges=allow_body or doc.doc_kind == "eml",
    )

    # OCR 副源（只可能出现在「有文本层的 PDF」上）：补主源缺失的标识字段。
    # 同一份文件再抽一遍，只是视图换成了 OCR 的文本/表格。
    if doc.ocr_blocks or doc.ocr_tables:
        ocr_view = doc.model_copy(deep=True)
        ocr_view.text = doc.ocr_text or ""
        ocr_view.blocks = doc.ocr_blocks
        ocr_view.tables = doc.ocr_tables
        ocr_rule = extract_by_rules(
            ocr_view,
            ctx=llm_ctx,
            entity=settings.pipeline.accounting_entity,
            entity_aliases=settings.pipeline.entity_aliases,
            body_charges=False,
        )
        rule, filled_by_ocr = merge_ocr_supplement(rule, ocr_rule)
        if filled_by_ocr:
            recorder.note(
                f"OCR 补充字段：{'、'.join(filled_by_ocr)}（{Path(doc.source.path).name}）"
            )

    llm_extraction = llm.extract(doc, ctx=llm_ctx) if llm.enabled else None
    if llm.enabled and llm_extraction is None:
        recorder.bump("llm_errors")
    extraction, recon_issues = reconcile(rule, llm_extraction, settings=settings)

    if not extraction.charges:
        return []

    return _materialize(
        extraction,
        doc=doc,
        ctx=ctx,
        recon_issues=recon_issues,
        settings=settings,
        store=store,
        degraded=not llm.enabled,
    )


# ---------------------------------------------------------------- 跨文件


def _cross_file_checks(
    entries: list[BillEntry],
    *,
    store: ExistingBillStore,
    recorder: Recorder,
) -> None:
    seen: dict[tuple, BillEntry] = {}
    for entry in entries:
        if entry.operation in {"PENDING_REVIEW"}:
            continue
        ref = (entry.reference_no or "").strip().lower()
        # 没有单号就无法证明两份材料是同一张单：不做跨文件判重（否则两份无关文件会互判重复）
        key = (
            ref,
            (entry.charge_item or "").lower().strip(),
            round(float(entry.amount), 2) if isinstance(entry.amount, (int, float)) else None,
            entry.currency,
        )
        if ref and key in seen:
            entry.issues.append(
                Issue(
                    code="V-DUP_BILL",
                    severity="blocker",
                    message=(
                        f"同批输入中重复出现：{entry.reference_no} / {entry.charge_item} / "
                        f"{entry.amount} {entry.currency or ''}，疑似重复投递"
                    ),
                    field="reference_no",
                )
            )
            entry.operation = "PENDING_REVIEW"
            entry.ai_reasoning.append(
                "同批输入中已出现相同单号+费用+金额的记录，判定为重复投递，转人工确认"
            )
            continue
        if ref:
            seen[key] = entry

        if entry.operation == "CREATE":
            duplicates = store.has_duplicate(entry)
            if duplicates:
                entry.issues.append(
                    Issue(
                        code="V-DUP_BILL",
                        severity="blocker",
                        message=(
                            "与历史账单重复：" + "、".join(r.bill_id for r in duplicates)
                            + "（同单号 + 同费用 + 同金额 + 同币种）"
                        ),
                        field="reference_no",
                        detail={"bill_ids": [r.bill_id for r in duplicates]},
                    )
                )
                entry.operation = "PENDING_REVIEW"


# ---------------------------------------------------------------- 输出


def _to_output_record(entry: BillEntry) -> OutputRecord:
    return OutputRecord(
        operation=entry.operation,  # type: ignore[arg-type]
        document_info=OutputDocumentInfo(
            document_type=entry.document_type,
            reference_no=entry.reference_no,
            document_date=entry.document_date,
        ),
        transaction_party=OutputTransactionParty(
            issuer=entry.issuer, recipient=entry.recipient
        ),
        charge_details=OutputChargeDetails(
            charge_item=entry.charge_item,
            charge_item_raw=entry.charge_item_raw,
            amount=to_number(entry.amount),
            currency=entry.currency,
            payment_term=entry.payment_term,
        ),
        financial_attributes=OutputFinancialAttributes(
            accounting_entity=entry.accounting_entity,
            posting_type=entry.posting_type,
        ),
        audit_trail=OutputAuditTrail(ai_reasoning=list(entry.ai_reasoning)),
    )


def _sort_key(record: dict[str, Any]) -> tuple[str, str, str]:
    info = record.get("document_info") or {}
    details = record.get("charge_details") or {}
    return (
        str(info.get("reference_no") or ""),
        str(details.get("charge_item") or ""),
        str(info.get("document_date") or ""),
    )


def _report_dict(entry: BillEntry) -> dict[str, Any]:
    payload = entry.model_dump(mode="json")
    payload["issues"] = [issue.model_dump(mode="json") for issue in entry.issues]
    payload["provenance"] = {
        name: prov.model_dump(mode="json") for name, prov in entry.provenance.items()
    }
    payload["match"] = entry.match.model_dump(mode="json") if entry.match else None
    return payload


def _write_json(path: Path, payload: Any) -> None:
    """先写临时文件再原子替换：中途失败不会留下「看起来正常」的半套产物。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_outputs(
    entries: list[BillEntry],
    *,
    settings: Settings,
    recorder: Recorder,
    errors: list[dict[str, Any]],
    out_dir: Path,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = [
        (
            entry,
            _to_output_record(entry).to_contract(
                extension_fields=settings.pipeline.extension_fields
                and not settings.pipeline.strict_schema
            ),
        )
        for entry in entries
    ]
    # result 与 report 用同一份排序后的顺序，否则两份文件同一条记录的下标对不上
    pairs.sort(key=lambda pair: _sort_key(pair[1]))
    ordered_entries = [entry for entry, _ in pairs]
    records = [record for _, record in pairs]

    result_path = out_dir / "result.json"
    _write_json(result_path, records)

    report_path = out_dir / "report.json"
    _write_json(
        report_path,
        {
            "run_id": recorder.run_id,
            "degraded": any(entry.degraded for entry in ordered_entries),
            "llm_enabled": bool(recorder.counters.get("llm_calls", 0) or settings.llm.enabled),
            "llm_errors": int(recorder.counters.get("llm_errors", 0)),
            "summary": _summary(ordered_entries),
            "records": [_report_dict(entry) for entry in ordered_entries],
        },
    )

    errors_path = out_dir / "errors.json"
    _write_json(errors_path, errors)

    trace_path = recorder.flush(out_dir)
    return {
        "result": result_path,
        "report": report_path,
        "errors": errors_path,
        "trace": trace_path,
    }


def _summary(entries: list[BillEntry]) -> dict[str, Any]:
    operations: dict[str, int] = {}
    severities: dict[str, int] = {}
    for entry in entries:
        operations[entry.operation] = operations.get(entry.operation, 0) + 1
        for issue in entry.issues:
            severities[issue.severity] = severities.get(issue.severity, 0) + 1
    return {
        "total_records": len(entries),
        "operations": operations,
        "issue_severity": severities,
        "posting_types": {
            key: sum(1 for e in entries if (e.posting_type or "null") == key)
            for key in ("AR", "AP", "null")
        },
    }


# ---------------------------------------------------------------- 对外入口


def _write_raw_snapshots(root: RawDocument, *, raw_dir: Path) -> list[str]:
    """把每份文档「实际用于抽取的那份文本」落盘，作为审计原文。

    `provenance.span` 只保留命中的那一小段，回答不了「这句话在原文什么位置、上下文是什么」；
    所以把整份文本另存，文件名前缀用 sha256 前 8 位，避免同名文件互相覆盖。
    返回相对 run 目录的路径列表，写进 trace 的 `files[].raw_snapshots`。
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for node in root.walk():
        text = (node.text or "").strip()
        if not text:
            continue
        digest = (node.source.sha256 or "nodigest")[:8]
        origin = node.source.origin or "root"
        label = origin if origin != "root" else Path(node.source.path).name
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", label)[:60] or "doc"
        target = raw_dir / f"{digest}__{safe}.txt"

        ocr = (node.meta or {}).get("ocr") or {}
        source_kind = ocr.get("role") or "text_layer"
        if ocr.get("provider"):
            source_kind += f" (provider={ocr['provider']})"
        header = "\n".join(
            [
                f"# source    : {node.source.path}",
                f"# sha256    : {node.source.sha256}",
                f"# doc_kind  : {node.doc_kind}",
                f"# origin    : {origin}",
                f"# text_from : {source_kind}",
                f"# chars     : {len(text)}",
                "# 说明：抽取阶段实际使用的文本。report.json 里 provenance.span 的片段都能在此文件内检索到。",
                "",
            ]
        )
        target.write_text(header + text, encoding="utf-8")
        written.append(str(target.relative_to(raw_dir.parent)))
    return written


def run(
    inputs: list[str | Path],
    *,
    settings: Settings | None = None,
    out_dir: str | Path | None = None,
    bills_path: str | Path | None = None,
    recorder: Recorder | None = None,
) -> RunResult:
    settings = settings or Settings.load()
    recorder = recorder or Recorder()
    out = Path(out_dir) if out_dir else Path.cwd() / "out" / f"run-{recorder.run_id}"
    workdir = out / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    store = ExistingBillStore.from_json(bills_path)
    provider = build_provider(settings)
    llm = LlmExtractor(settings)
    result = RunResult(out_dir=out, recorder=recorder)
    if not llm.enabled:
        recorder.note("LLM 未启用（无 API Key 或显式关闭），本次仅规则轨抽取，输出标记 degraded")

    files = expand_inputs(inputs)
    if not files:
        recorder.note("输入为空，未处理任何文件")
    for raw in inputs:
        if not Path(str(raw)).expanduser().exists():
            message = f"E_INPUT_NOT_FOUND: 输入路径不存在 {raw}"
            result.errors.append({"path": str(raw), "error": message})

    all_entries: list[BillEntry] = []
    for path in files:
        file_record = FileRecord(path=str(path))
        with recorder.stage("ingest", path=path.name) as detail:
            root = ingest(path, workdir=workdir, settings=settings)
            file_record.sha256 = root.source.sha256
            file_record.kind = root.doc_kind
            file_record.origin = root.source.origin
            file_record.ingested = not root.errors
            file_record.errors.extend(root.errors)
            detail["kind"] = root.doc_kind
            detail["children"] = len(root.children)

        for error in root.errors:
            result.errors.append({"path": str(path), "error": error})
        # 处理阶段会往 doc.errors 追加 OCR 失败等，先快照解析阶段已有的
        known_errors = set(root.errors)

        with recorder.stage("process", path=path.name) as detail:
            try:
                entries = process_document_tree(
                    root,
                    settings=settings,
                    store=store,
                    recorder=recorder,
                    provider=provider,
                    llm=llm,
                    workdir=workdir,
                )
            except Exception as exc:  # noqa: BLE001 - 单文件失败不拖垮批次
                entries = []
                message = f"E_PARSE_FAILED: {type(exc).__name__}: {exc}"
                file_record.errors.append(message)
                result.errors.append({"path": str(path), "error": message})
                detail["error"] = message
            detail["entries"] = len(entries)
            file_record.entries = len(entries)
            file_record.ocr_call = any(
                "ocr" in (doc.meta or {}) for doc in root.walk()
            )
            file_record.issues = sum(len(entry.issues) for entry in entries)

            # 处理阶段新增的错误（OCR 失败、回放未命中）也必须落到 errors.json，
            # 不能因为「解析阶段没报错」就静默吞掉一个文件
            known = set(known_errors)
            for node in root.walk():
                origin = node.source.origin or ""
                label = f"{origin} " if origin and origin != "root" else ""
                # OCR 失败但已回退文本层：不是解析失败，但要让人知道「这份结果不是 OCR 来的」
                fallback = (node.meta or {}).get("ocr_fallback")
                if fallback:
                    file_record.notes.append(
                        f"{label}OCR 失败已回退文本层（结果来自 PDF 文本层，非 OCR）：{fallback[:120]}"
                    )
                for error in node.errors:
                    if error in known:
                        continue
                    known.add(error)
                    message = f"{label}{error}".strip()
                    file_record.errors.append(message)
                    result.errors.append({"path": str(path), "error": message})
                    detail["error"] = message

            # 一条费用都没有、又没有任何报错，才是「解析成功但没抽到钱」——这时必须显式可见，
            # 否则外部看起来像「这批文件本来就没费用」。放在错误收集之后，避免和 OCR 失败重复报。
            if not entries and not file_record.errors:
                if _looks_like_transport_doc(root):
                    # 提单/运单本来就不计费，报成错误会淹没真正的失败
                    note = "识别为提单/运单类非计费单据，未抽取费用（符合预期）"
                    file_record.notes.append(note)
                    recorder.note(f"{path.name}：{note}")
                else:
                    message = "E_NO_CHARGE: 解析成功但未抽取到任何费用（可能不是账单，或版式未被识别）"
                    file_record.errors.append(message)
                    result.errors.append({"path": str(path), "error": message})

            # 原文快照：审计要求「这条记录从哪段文字来」，只留 span 片段不够
            if settings.pipeline.keep_raw_text:
                file_record.raw_snapshots = _write_raw_snapshots(root, raw_dir=out / "raw")

        all_entries.extend(entries)
        recorder.file(file_record)

    with recorder.stage("cross_check"):
        _cross_file_checks(all_entries, store=store, recorder=recorder)
        if settings.pipeline.idempotent:
            _apply_idempotency(all_entries, store=store, recorder=recorder)

    recorder.counters["llm_calls"] = llm.calls
    recorder.counters["llm_cache_hits"] = llm.cache_hits
    if llm.last_error:
        recorder.note(f"LLM 调用失败并降级：{llm.last_error}")

    with recorder.stage("emit"):
        paths = write_outputs(
            all_entries, settings=settings, recorder=recorder, errors=result.errors, out_dir=out
        )

    result.entries = all_entries
    result.result_path = paths["result"]
    result.report_path = paths["report"]
    result.errors_path = paths["errors"]
    result.trace_path = paths["trace"]
    result.records = json.loads(paths["result"].read_text(encoding="utf-8"))
    return result


def _apply_idempotency(
    entries: list[BillEntry], *, store: ExistingBillStore, recorder: Recorder
) -> None:
    """重复投递保护：同一业务键已在历史账单里存在相同金额 → 降级 NO_CHANGE。"""
    for entry in entries:
        if entry.operation not in {"CREATE", "UPDATE"}:
            continue
        duplicates = store.has_duplicate(entry)
        if duplicates:
            entry.operation = "NO_CHANGE"
            entry.issues.append(
                Issue(
                    code="P-IDEMPOTENT",
                    severity="info",
                    message="该账单已入账（幂等键命中），本次不做变更",
                    detail={"bill_ids": [r.bill_id for r in duplicates]},
                )
            )
            entry.ai_reasoning.append(
                "幂等保护：同一单号 + 费用 + 金额 + 币种已存在历史账单，判定为重复投递，不重复入账"
            )
            recorder.bump("idempotent_skips")
