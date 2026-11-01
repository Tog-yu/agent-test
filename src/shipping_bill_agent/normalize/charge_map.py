"""费用名称标准化：精确别名 → 包含匹配 → 编辑距离 → 关键词 → 保留原文。"""

from __future__ import annotations

import difflib
import functools
import re
from pathlib import Path
from typing import Any

import yaml

from ..config import PROJECT_ROOT

ALIAS_FILE = PROJECT_ROOT / "data" / "charge_aliases.yaml"

# 兜底内置（YAML 缺失时仍能覆盖题目要求的 6 组）
_BUILTIN_CANONICAL: dict[str, list[str]] = {
    "Handling Charge": ["Handling Fee", "H/D Fee", "HD Fee", "Handling", "操作费"],
    "Documentation Fee": ["Doc Fee", "Documentary Fee", "Documentation Charge", "单证费"],
    "Terminal Handling Charge": ["THC", "Terminal Handling", "码头操作费"],
    "Trucking Charge": ["Trucking Fee", "Haulage Fee", "拖车费"],
    "Customs Clearance Fee": ["Customs Fee", "Clearance Fee", "报关费", "清关费"],
    "Storage Charge": ["Storage Fee", "Storage", "仓储费", "堆存费"],
}
_BUILTIN_KEYWORDS: dict[str, list[str]] = {
    "Handling Charge": ["handling"],
    "Documentation Fee": ["doc fee", "documentation"],
    "Terminal Handling Charge": ["terminal handling", "thc"],
    "Trucking Charge": ["trucking", "haulage"],
    "Customs Clearance Fee": ["customs", "clearance", "报关"],
    "Storage Charge": ["storage", "仓储"],
}

_NOISE_RE = re.compile(r"[\s\u00a0\.\,\:\;\-_/\\\(\)\[\]\"'`*#￥$]+")
FUZZY_THRESHOLD = 0.86


def denoise(text: str) -> str:
    """归一用于比对的键：小写、去标点、压空白、去尾复数 s。"""
    lowered = (text or "").lower().strip()
    collapsed = _NOISE_RE.sub(" ", lowered).strip()
    return re.sub(r"\s+", " ", collapsed)


@functools.lru_cache(maxsize=1)
def _load_tables() -> tuple[dict[str, str], dict[str, list[str]], int]:
    """返回 (alias_key -> canonical, keyword -> [canonical], alias 总数)。"""
    canonical: dict[str, list[str]] = dict(_BUILTIN_CANONICAL)
    keywords: dict[str, list[str]] = dict(_BUILTIN_KEYWORDS)

    path: Path = ALIAS_FILE
    if path.exists():
        try:
            data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            loaded = data.get("canonical") or {}
            if isinstance(loaded, dict):
                for name, aliases in loaded.items():
                    merged = list(canonical.get(name, []))
                    for alias in aliases or []:
                        if alias not in merged:
                            merged.append(alias)
                    canonical[name] = merged
            loaded_kw = data.get("keywords") or {}
            if isinstance(loaded_kw, dict):
                for name, kws in loaded_kw.items():
                    merged_kw = list(keywords.get(name, []))
                    for kw in kws or []:
                        if kw not in merged_kw:
                            merged_kw.append(kw)
                    keywords[name] = merged_kw
        except (yaml.YAMLError, OSError):
            pass

    alias_index: dict[str, str] = {}
    for name, aliases in canonical.items():
        alias_index[denoise(name)] = name
        for alias in aliases:
            alias_index.setdefault(denoise(alias), name)

    return alias_index, keywords, len(alias_index)


def canonical_names() -> list[str]:
    alias_index, _, _ = _load_tables()
    return sorted(set(alias_index.values()))


def _keyword_hit(keyword: str, key: str) -> bool:
    """关键词按「词序列」命中：h c 只匹配独立的 h c，不匹配 each charge。"""
    if not keyword:
        return False
    tokens = key.split()
    parts = keyword.split()
    if len(parts) == 1:
        return parts[0] in tokens or (len(parts[0]) >= 6 and parts[0] in key)
    span = len(parts)
    return any(tokens[i : i + span] == parts for i in range(len(tokens) - span + 1))


def normalize_charge(raw: object) -> tuple[str | None, float, str]:
    """返回 (标准名, 置信度, 命中方式)。

    命中方式：exact / contains / fuzzy / keyword / unmapped
    """
    if raw is None:
        return None, 0.0, "empty"
    text = str(raw).strip()
    if not text:
        return None, 0.0, "empty"

    alias_index, keywords, _ = _load_tables()
    key = denoise(text)
    if not key:
        return None, 0.0, "empty"

    # ① 精确
    if key in alias_index:
        return alias_index[key], 1.0, "exact"

    # ② 包含（别名整词出现在原文里，或原文出现在别名里）
    # 单性别名按「词」匹配（避免 'thc' 命中 'each'），多词别名按子串匹配
    tokens = set(key.split())
    candidates = []
    for alias, name in alias_index.items():
        if len(alias) < 3:
            continue
        if " " in alias:
            if alias in key:
                candidates.append((alias, name))
        elif alias in tokens:
            candidates.append((alias, name))
    if candidates:
        names = {name for _, name in candidates}
        if len(names) > 1:
            # "THC + DOC FEE" 这类一格多费用：不猜其中任一个，保留原文交人拆
            return text.strip(), 0.3, "ambiguous"
        alias, name = max(candidates, key=lambda item: len(item[0]))
        return name, 0.9, "contains"

    # ③ 编辑距离
    close = difflib.get_close_matches(key, list(alias_index.keys()), n=1, cutoff=FUZZY_THRESHOLD)
    if close:
        return alias_index[close[0]], 0.8, "fuzzy"

    # ④ 关键词（按词序列命中，避免 'h c' 命中 'each charge' 这类噪声）
    for name, kws in keywords.items():
        for kw in kws:
            if _keyword_hit(denoise(kw), key):
                return name, 0.7, "keyword"

    # ⑤ 保留原文
    return text, 0.4, "unmapped"
