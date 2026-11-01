"""运行期审计：阶段耗时、文件结果、OCR/LLM 调用记录。"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class StageRecord:
    stage: str
    ms: float
    ok: bool = True
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileRecord:
    path: str
    sha256: str = ""
    kind: str = ""
    origin: str = "root"
    ingested: bool = False
    ocr_call: bool = False
    ocr_error: str | None = None
    entries: int = 0
    issues: int = 0
    errors: list[str] = field(default_factory=list)
    # 非错误但需要解释的情况（例如「这是提单，本来就没有费用」）
    notes: list[str] = field(default_factory=list)
    # 原文快照的相对路径（run 目录下）。provenance.span 只存字段级片段，
    # 要复核「这段钱是从哪句话抠出来的」需要完整文本，所以单独落盘。
    raw_snapshots: list[str] = field(default_factory=list)


class Recorder:
    def __init__(self) -> None:
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.started_at = time.time()
        self.stages: list[StageRecord] = []
        self.files: list[FileRecord] = []
        self.counters: dict[str, Any] = {
            "ocr_calls": 0,
            "ocr_cache_hits": 0,
            "ocr_errors": 0,
            "llm_calls": 0,
            "llm_cache_hits": 0,
            "llm_errors": 0,
        }
        self.notes: list[str] = []

    @contextmanager
    def stage(self, name: str, **detail: Any) -> Iterator[dict[str, Any]]:
        payload = dict(detail)
        started = time.perf_counter()
        ok = True
        try:
            yield payload
        except Exception:
            ok = False
            raise
        finally:
            self.stages.append(
                StageRecord(
                    stage=name,
                    ms=round((time.perf_counter() - started) * 1000, 2),
                    ok=ok,
                    detail=payload,
                )
            )

    def file(self, record: FileRecord) -> FileRecord:
        self.files.append(record)
        return record

    def bump(self, key: str, delta: int = 1) -> None:
        self.counters[key] = int(self.counters.get(key, 0)) + delta

    def note(self, text: str) -> None:
        self.notes.append(text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
            "duration_ms": round((time.time() - self.started_at) * 1000, 2),
            "counters": self.counters,
            "files": [asdict(record) for record in self.files],
            "stages": [asdict(record) for record in self.stages],
            "notes": self.notes,
        }

    def flush(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"trace-{self.run_id}.json"
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target
