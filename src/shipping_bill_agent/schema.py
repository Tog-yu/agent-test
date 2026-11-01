"""全系统契约模型。L0 层：只定义结构与常量，不含业务判断。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- 常量

DOC_KINDS = ("eml", "pdf", "image", "excel", "word", "unknown")
DOC_TYPES = ("Debit Note", "Credit Note", "Invoice", "Arrival Notice", "Unknown")
OPERATIONS = ("CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW")
SEVERITIES = ("blocker", "warn", "info")
PAYMENT_TERMS = ("PP", "CC")
POSTING_TYPES = ("AR", "AP")

SCALAR_FIELDS = (
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "bl_no",
    "booking_no",
    "container_no",
    "currency",
    "payment_term",
    "subtotal",
    "tax",
    "total",
)

STRONG_FIELDS = {"reference_no", "document_date", "amount", "currency", "total", "subtotal", "tax"}

FIELD_LABELS = {
    "document_type": "单据类型",
    "reference_no": "单据编号",
    "document_date": "单据日期",
    "issuer": "签发方",
    "recipient": "接收方",
    "bl_no": "提单号",
    "booking_no": "订舱号",
    "container_no": "箱号",
    "currency": "币种",
    "payment_term": "付款方式",
    "subtotal": "小计",
    "tax": "税额",
    "total": "总计",
    "charge_item": "费用名称",
    "amount": "金额",
    "quantity": "数量",
    "unit_price": "单价",
}

DocKind = Literal["eml", "pdf", "image", "excel", "word", "unknown"]
Operation = Literal["CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW"]
Severity = Literal["blocker", "warn", "info"]


# ---------------------------------------------------------------- 摄入


class SourceRef(BaseModel):
    path: str
    sha256: str
    kind: DocKind = "unknown"
    origin: str = "root"
    size_bytes: int = 0


class Block(BaseModel):
    kind: Literal["paragraph", "kv", "header", "table_row", "note"] = "paragraph"
    text: str
    page: int | None = None
    line_no: int | None = None


class TableBlock(BaseModel):
    name: str | None = None
    page: int | None = None
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class RawDocument(BaseModel):
    source: SourceRef
    doc_kind: DocKind = "unknown"
    text: str = ""
    blocks: list[Block] = Field(default_factory=list)
    tables: list[TableBlock] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    children: list["RawDocument"] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # OCR 作为「副源」时的结果。有文本层的 PDF 用文本层做主源（单号/币种/明细粒度都在），
    # OCR 只用来补它缺的标识字段；扫描件的 OCR 结果直接写进 text/blocks/tables，不落这里。
    ocr_text: str | None = None
    ocr_blocks: list[Block] = Field(default_factory=list)
    ocr_tables: list[TableBlock] = Field(default_factory=list)

    def walk(self) -> list["RawDocument"]:
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


# ---------------------------------------------------------------- 抽取


class FieldVal(BaseModel):
    """单字段抽取结果：值 + 原文证据 + 来源 + 置信度。"""

    value: Any = None
    span: str | None = None
    source_path: str | None = None
    page: int | None = None
    line_no: int | None = None
    extractor: str = "rule"
    confidence: float = 0.0

    @property
    def grounded(self) -> bool:
        return self.span is not None and str(self.span).strip() != ""


class ChargeCandidate(BaseModel):
    raw_name: str | None = None
    amount: Any = None
    quantity: Any = None
    unit_price: Any = None
    currency: str | None = None
    payment_term: str | None = None
    span: str | None = None
    source_path: str | None = None
    page: int | None = None
    line_no: int | None = None
    confidence: float = 0.5
    extractor: str = "rule"


class Extraction(BaseModel):
    """规则轨与 LLM 轨共用的抽取产物，便于字段级对账。"""

    scalars: dict[str, FieldVal] = Field(default_factory=dict)
    charges: list[ChargeCandidate] = Field(default_factory=list)
    # 单据按币种分别给出的总额（多币种单据用），例如 {"USD": 100.0, "EUR": 50.0}
    totals_by_currency: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    extractor: str = "rule"

    def get(self, name: str) -> FieldVal:
        return self.scalars.get(name) or FieldVal()


# ---------------------------------------------------------------- 校验与匹配


class Issue(BaseModel):
    code: str
    severity: Severity = "info"
    message: str = ""
    field: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    field: str
    source_path: str | None = None
    page: int | None = None
    line_no: int | None = None
    span: str | None = None
    extractor: str = "rule"
    confidence: float = 0.0


class MatchInfo(BaseModel):
    tier: str | None = None
    bill_id: str | None = None
    margin: float | None = None
    ambiguous: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------- 决策主对象


class BillEntry(BaseModel):
    # 单据级
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    bl_no: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    # 费用级
    charge_item: str | None = None
    charge_item_raw: str | None = None
    amount: Any = None
    currency: str | None = None
    payment_term: str | None = None
    quantity: Any = None
    unit_price: Any = None
    tax: Any = None
    # 财务
    accounting_entity: str | None = None
    posting_type: str | None = None
    # 决策
    operation: Operation = "PENDING_REVIEW"
    intended_operation: str | None = None
    match: MatchInfo | None = None
    issues: list[Issue] = Field(default_factory=list)
    provenance: dict[str, Provenance] = Field(default_factory=dict)
    source_paths: list[str] = Field(default_factory=list)
    ai_reasoning: list[str] = Field(default_factory=list)
    degraded: bool = False
    inherited_fields: list[str] = Field(default_factory=list)

    def has_blocker(self) -> bool:
        return any(i.severity == "blocker" for i in self.issues)

    def conf(self, field_name: str) -> float:
        prov = self.provenance.get(field_name)
        return prov.confidence if prov else 0.0


# ---------------------------------------------------------------- LLM 结构化输出


class LlmChargeLine(BaseModel):
    """LLM 单条费用行输出（键名固定，禁增字段）。"""

    model_config = ConfigDict(extra="forbid")

    charge_item: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    currency: str | None = None
    payment_term: str | None = None
    evidence: str | None = None


class LlmExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    bl_no: str | None = None
    booking_no: str | None = None
    container_no: str | None = None
    currency: str | None = None
    payment_term: str | None = None
    subtotal: str | None = None
    tax: str | None = None
    total: str | None = None
    charges: list[LlmChargeLine] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 对外契约


class OutputDocumentInfo(BaseModel):
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None


class OutputTransactionParty(BaseModel):
    issuer: str | None = None
    recipient: str | None = None


class OutputChargeDetails(BaseModel):
    charge_item: str | None = None
    charge_item_raw: str | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None


class OutputFinancialAttributes(BaseModel):
    accounting_entity: str | None = None
    posting_type: str | None = None


class OutputAuditTrail(BaseModel):
    ai_reasoning: list[str] = Field(default_factory=list)


class OutputRecord(BaseModel):
    operation: Operation = "PENDING_REVIEW"
    document_info: OutputDocumentInfo = Field(default_factory=OutputDocumentInfo)
    transaction_party: OutputTransactionParty = Field(default_factory=OutputTransactionParty)
    charge_details: OutputChargeDetails = Field(default_factory=OutputChargeDetails)
    financial_attributes: OutputFinancialAttributes = Field(
        default_factory=OutputFinancialAttributes
    )
    audit_trail: OutputAuditTrail = Field(default_factory=OutputAuditTrail)

    def to_contract(self, *, extension_fields: bool = True) -> dict[str, Any]:
        """转成题目要求的键结构；缺值为 null，键不省略。"""
        details: dict[str, Any] = {
            "charge_item": self.charge_details.charge_item,
        }
        if extension_fields:
            details["charge_item_raw"] = self.charge_details.charge_item_raw
        details.update(
            {
                "amount": self.charge_details.amount,
                "currency": self.charge_details.currency,
                "payment_term": self.charge_details.payment_term,
            }
        )
        return {
            "operation": self.operation,
            "document_info": {
                "document_type": self.document_info.document_type,
                "reference_no": self.document_info.reference_no,
                "document_date": self.document_info.document_date,
            },
            "transaction_party": {
                "issuer": self.transaction_party.issuer,
                "recipient": self.transaction_party.recipient,
            },
            "charge_details": details,
            "financial_attributes": {
                "accounting_entity": self.financial_attributes.accounting_entity,
                "posting_type": self.financial_attributes.posting_type,
            },
            "audit_trail": {"ai_reasoning": list(self.audit_trail.ai_reasoning)},
        }


class DocumentContext(BaseModel):
    """EML 头/正文提供的兜底上下文，供解析器补缺用。"""

    subject: str | None = None
    sender: str | None = None
    recipient: str | None = None
    date: str | None = None
    body_text: str = ""
    origin: str = "root"


__all__ = [name for name in dir() if not name.startswith("_")]
