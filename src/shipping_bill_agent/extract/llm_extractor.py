"""LLM 抽取器：LangChain LCEL + 结构化输出 + 结果缓存 + 证据锚定。

定位：**只做提案**。输出交给 reconcile 与校验闸门，不直接决定入账。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..schema import (
    ChargeCandidate,
    DocumentContext,
    Extraction,
    FieldVal,
    LlmExtraction,
    RawDocument,
    TableBlock,
)
from ..normalize.payment import normalize_payment_term
from ..utils import clean_text
from .prompts import HUMAN_EXTRACT, SYSTEM_EXTRACT, render_tables

SCALAR_KEYS = (
    "document_type", "reference_no", "document_date", "issuer", "recipient",
    "bl_no", "booking_no", "container_no", "currency", "payment_term",
    "subtotal", "tax", "total",
)


def _tables_markdown(tables: list[TableBlock], limit: int = 6) -> str:
    chunks: list[str] = []
    for table in tables[:limit]:
        lines = []
        if table.header:
            lines.append(" | ".join(table.header))
        for row in table.rows[:30]:
            lines.append(" | ".join(row))
        if lines:
            chunks.append(f"[表格 {table.name or ''}]\n" + "\n".join(lines))
    return "\n\n".join(chunks)


def _normalize_for_grounding(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


class LlmExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = settings.llm.resolved_enabled()
        self.last_error: str | None = None
        self.calls = 0
        self.cache_hits = 0
        self._chain: Any = None
        if self.enabled:
            self._chain = self._build_chain()

    # ------------------------------------------------------------ 构建

    def _build_chain(self) -> Any:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=self.settings.llm.model,
            temperature=self.settings.llm.temperature,
            api_key=self.settings.llm.api_key,
            base_url=self.settings.llm.base_url,
            timeout=self.settings.llm.timeout,
            max_retries=self.settings.llm.max_retries,
        )
        # WB/部分 OpenAI 兼容网关只实现 tools/function-calling，不支持
        # response_format=json_schema（会 404）。function_calling 更通用，
        # 仍由 Pydantic schema 约束参数，不降低输出契约强度。
        structured = llm.with_structured_output(LlmExtraction, method="function_calling")
        prompt = ChatPromptTemplate.from_messages(
            [("system", SYSTEM_EXTRACT), ("human", HUMAN_EXTRACT)]
        )
        return prompt | structured

    # ------------------------------------------------------------ 缓存

    def _cache_path(self, payload: dict[str, Any]) -> Path:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return Path(self.settings.llm.cache_dir) / f"{digest}.json"

    def _cache_read(self, path: Path) -> LlmExtraction | None:
        if not path.exists():
            return None
        try:
            return LlmExtraction.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏就当没有
            return None

    def _cache_write(self, path: Path, value: LlmExtraction) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ 主流程

    def extract(
        self, doc: RawDocument, *, ctx: DocumentContext | None = None
    ) -> Extraction | None:
        if not self.enabled or self._chain is None:
            return None

        content = clean_text(doc.text)
        if len(content) > self.settings.llm.max_input_chars:
            content = content[: self.settings.llm.max_input_chars] + "\n...(已截断)"

        hints = []
        if ctx:
            for label, value in (
                ("邮件主题", ctx.subject),
                ("发件人", ctx.sender),
                ("收件人", ctx.recipient),
                ("邮件日期", ctx.date),
            ):
                if value:
                    hints.append(f"{label}：{value}")

        payload = {
            "source_name": Path(doc.source.path).name,
            "doc_kind": doc.doc_kind,
            "content": content,
            "tables_block": render_tables(_tables_markdown(doc.tables)),
            "hints": "\n".join(hints) if hints else "（无）",
        }

        cache_file = self._cache_path(
            {"model": self.settings.llm.model, "template": "extract-v2", **payload}
        )
        cached = self._cache_read(cache_file)
        if cached is not None:
            self.cache_hits += 1
            parsed = cached
        else:
            attempts = max(1, self.settings.llm.max_retries + 1)
            parsed = None
            for attempt in range(attempts):
                try:
                    self.calls += 1
                    parsed = self._chain.invoke(payload)
                    if isinstance(parsed, dict):
                        parsed = LlmExtraction.model_validate(parsed)
                    break
                except Exception as exc:  # noqa: BLE001 - LLM 失败必须降级而不是崩
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt == attempts - 1:
                        return None
                    # OpenAI SDK 对 404 不重试；账号池/网关偶发 404 需要应用层兜住。
                    time.sleep(min(0.5 * (2**attempt), 2.0))
            if parsed is None:
                return None
            self._cache_write(cache_file, parsed)

        try:
            return self._to_extraction(parsed, doc=doc, corpus=content)
        except Exception as exc:  # noqa: BLE001 - 提案异常不能击穿规则轨
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    # ------------------------------------------------------------ 归一 + 证据锚定

    def _to_extraction(self, llm: LlmExtraction, *, doc: RawDocument, corpus: str) -> Extraction:
        haystack = _normalize_for_grounding(corpus)
        evidence = {k: v for k, v in (llm.evidence or {}).items() if isinstance(v, str)}
        scalars: dict[str, FieldVal] = {}

        for key in SCALAR_KEYS:
            value = getattr(llm, key, None)
            if value in (None, ""):
                continue
            span = evidence.get(key)
            ok, matched = _ground(span, haystack)
            if not ok:
                # 证据锚定失败 → 丢弃该字段（不虚构的机器保证）
                continue
            scalars[key] = FieldVal(
                value=str(value).strip(),
                span=span,
                source_path=doc.source.path,
                extractor="llm",
                confidence=0.85 if span else 0.75,
            )

        charges: list[ChargeCandidate] = []
        for index, line in enumerate(llm.charges or []):
            name = (line.charge_item or "").strip()
            if not name:
                continue
            ok, _ = _ground(line.evidence, haystack)
            if not ok:
                continue
            charges.append(
                ChargeCandidate(
                    raw_name=name,
                    amount=(line.amount or "").strip() or None,
                    quantity=(line.quantity or "").strip() or None,
                    unit_price=(line.unit_price or "").strip() or None,
                    currency=(line.currency or "").strip() or None,
                    payment_term=normalize_payment_term(line.payment_term),
                    span=line.evidence,
                    source_path=doc.source.path,
                    line_no=index + 1,
                    confidence=0.85 if line.evidence else 0.6,
                    extractor="llm",
                )
            )

        return Extraction(
            scalars=scalars, charges=charges, notes=list(llm.notes or []), extractor="llm"
        )


def _ground(span: str | None, haystack: str) -> tuple[bool, str | None]:
    """证据必须（去空白后）出现在原文里。"""
    if not span:
        return False, None
    needle = _normalize_for_grounding(span)
    if not needle:
        return False, None
    if needle in haystack:
        return True, span
    # 容忍 OCR 里被折行的证据：取首尾片段分别校验
    if len(needle) > 12:
        head, tail = needle[:8], needle[-8:]
        if head in haystack and tail in haystack:
            return True, span
    return False, None
