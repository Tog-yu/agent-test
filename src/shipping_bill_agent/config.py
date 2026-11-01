"""运行配置：环境变量 > 默认值，CLI 参数再覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载（不引入 python-dotenv）。已存在的环境变量不覆盖。"""
    target = path or (PROJECT_ROOT / ".env")
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class OcrSettings:
    mode: str = "cassette"  # gateway | cassette | stub
    url: str = "https://gateway.pltplt.com/ai/process"
    task: str = "orderChange"
    public_base: str = ""
    cassette_dir: Path = PROJECT_ROOT / "data" / "ocr_cassettes"
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    retries: int = 3

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


@dataclass(frozen=True)
class LlmSettings:
    enabled: str = "auto"  # auto | true | false
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.0
    timeout: int = 90
    max_retries: int = 2
    cache_dir: Path = PROJECT_ROOT / "out" / ".llm_cache"
    max_input_chars: int = 12000

    def resolved_enabled(self) -> bool:
        if self.enabled in {"false", "0", "off", "no"}:
            return False
        if self.enabled in {"true", "1", "on", "yes"}:
            return bool(self.api_key)
        return bool(self.api_key)


@dataclass(frozen=True)
class PipelineSettings:
    accounting_entity: str = "Origin Agent"
    # 记账主体在单据上的其他写法（中英文名、简称），用于「我方是否出现在单据上」的判向
    entity_aliases: list[str] = field(default_factory=list)
    credit_note_policy: str = "mirror"  # mirror | same_side
    # 题目要求「PDF 和图片通过 OCR API 识别」→ 默认 ocr，PDF 一律送网关。
    # 但网关不可达时不能整批报废：OCR 失败会自动回退到文本层（见 pipeline._apply_ocr），
    # 只有连文本层都没有的扫描件才算真失败。auto 留给「有文本层就别浪费 OCR 预算」的场景。
    pdf_strategy: str = "ocr"  # ocr | auto | text
    pdf_text_layer_min_chars: int = 120
    match_t4_strict: bool = True
    idempotent: bool = False
    # 原文快照：把每份文档「实际用来抽取的那份文本」落到 out/<run>/raw/。
    # provenance.span 只留字段级片段，复核时需要能回看完整上下文，所以默认开启。
    keep_raw_text: bool = True
    strict_schema: bool = False
    confidence_amount: float = 0.90
    confidence_currency: float = 0.90
    confidence_name: float = 0.60
    extension_fields: bool = True


@dataclass(frozen=True)
class Settings:
    ocr: OcrSettings = field(default_factory=OcrSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    pipeline: PipelineSettings = field(default_factory=PipelineSettings)

    @classmethod
    def load(cls, **overrides: Any) -> "Settings":
        settings = cls(
            ocr=OcrSettings(
                mode=_env_str("SBA_OCR_MODE", "cassette").lower(),
                url=_env_str("SBA_OCR_URL", "https://gateway.pltplt.com/ai/process"),
                task=_env_str("SBA_OCR_TASK", "orderChange"),
                public_base=_env_str("SBA_OCR_PUBLIC_BASE", ""),
                cassette_dir=Path(_env_str("SBA_OCR_CASSETTE_DIR", "" ) or (PROJECT_ROOT / "data" / "ocr_cassettes")),
                connect_timeout=_env_float("SBA_OCR_CONNECT_TIMEOUT", 10.0),
                read_timeout=_env_float("SBA_OCR_READ_TIMEOUT", 300.0),
                retries=_env_int("SBA_OCR_RETRIES", 3),
            ),
            llm=LlmSettings(
                enabled=_env_str("SBA_LLM_ENABLED", "auto").lower(),
                base_url=_env_str("SBA_LLM_BASE_URL", "https://api.deepseek.com/v1"),
                api_key=_env_str("SBA_LLM_API_KEY", ""),
                model=_env_str("SBA_LLM_MODEL", "deepseek-chat"),
                temperature=_env_float("SBA_LLM_TEMPERATURE", 0.0),
                timeout=_env_int("SBA_LLM_TIMEOUT", 90),
                max_retries=_env_int("SBA_LLM_MAX_RETRIES", 2),
                cache_dir=Path(_env_str("SBA_LLM_CACHE_DIR", "") or (PROJECT_ROOT / "out" / ".llm_cache")),
                max_input_chars=_env_int("SBA_LLM_MAX_INPUT_CHARS", 12000),
            ),
            pipeline=PipelineSettings(
                accounting_entity=_env_str("SBA_ACCOUNTING_ENTITY", "Origin Agent"),
                credit_note_policy=_env_str("SBA_CREDIT_NOTE_POLICY", "mirror").lower(),
                pdf_strategy=_env_str("SBA_PDF_STRATEGY", "ocr").lower(),
                pdf_text_layer_min_chars=_env_int("SBA_PDF_TEXT_MIN_CHARS", 120),
                match_t4_strict=_env_bool("SBA_MATCH_T4_STRICT", True),
                idempotent=_env_bool("SBA_IDEMPOTENT", False),
                keep_raw_text=_env_bool("SBA_KEEP_RAW_TEXT", True),
                strict_schema=_env_bool("SBA_STRICT_SCHEMA", False),
                confidence_amount=_env_float("SBA_CONF_AMOUNT", 0.90),
                confidence_currency=_env_float("SBA_CONF_CURRENCY", 0.90),
                confidence_name=_env_float("SBA_CONF_NAME", 0.60),
                extension_fields=_env_bool("SBA_EXTENSION_FIELDS", True),
            ),
        )
        return settings.with_overrides(**overrides)

    def with_overrides(self, **kwargs: Any) -> "Settings":
        """支持 ocr__mode / llm__model / pipeline__accounting_entity 形式的覆盖。"""
        buckets: dict[str, dict[str, Any]] = {"ocr": {}, "llm": {}, "pipeline": {}}
        for key, value in kwargs.items():
            if value is None:
                continue
            if "__" in key:
                bucket, _, attr = key.partition("__")
                if bucket in buckets:
                    buckets[bucket][attr] = value
            elif key in {"ocr", "llm", "pipeline"} and isinstance(value, dict):
                buckets[key].update(value)
        return Settings(
            ocr=replace(self.ocr, **buckets["ocr"]),
            llm=replace(self.llm, **buckets["llm"]),
            pipeline=replace(self.pipeline, **buckets["pipeline"]),
        )
