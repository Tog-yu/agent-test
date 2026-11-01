"""真实附件在线验收：公网临时 URL + 在线 OCR，可选 WB OpenAI 兼容反代 LLM。

仅用于验收测试。上传目标是题目附件，执行前需确认允许公网临时托管。
URL 按文件 sha256 映射，避免 GatewayOcrProvider 默认「public_base + 文件名」
无法表达 paste.c-net.org 的随机地址。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import requests

from shipping_bill_agent.config import Settings, load_dotenv
from shipping_bill_agent.ocr.providers import GatewayOcrProvider
from shipping_bill_agent.pipeline import run
from shipping_bill_agent.utils import sha256_file

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "data" / "real"
DETACHED = ROOT / "笔试题AIAGENT" / "附件提取"
UPLOAD_URL = "https://paste.c-net.org/"


def upload(path: Path, session: requests.Session) -> str:
    response = session.post(UPLOAD_URL, data=path.read_bytes(), timeout=60)
    response.raise_for_status()
    url = response.text.strip()
    if not url.startswith("https://"):
        raise RuntimeError(f"临时托管未返回 HTTPS URL：{path.name}: {url[:100]}")
    with session.get(url, timeout=60) as check:
        check.raise_for_status()
        if hashlib.sha256(check.content).hexdigest() != sha256_file(path):
            raise RuntimeError(f"公网下载内容 hash 不一致：{path.name}")
    return url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="启用 WB 反代 LLM")
    parser.add_argument("--out", default=str(ROOT / "out" / "online-full"))
    parser.add_argument(
        "--reuse-image-url",
        default="",
        help="复用已上传的 zim_invoice_scan.png URL，避免重复上传",
    )
    parser.add_argument(
        "--reuse-manifest",
        default="",
        help="复用上一轮 online-manifest.json 的全部公开 URL，避免重复上传",
    )
    args = parser.parse_args()
    load_dotenv()
    reused: dict[str, str] = {}
    if args.reuse_manifest:
        previous = json.loads(Path(args.reuse_manifest).read_text(encoding="utf-8"))
        reused = {item["sha256"]: item["url"] for item in previous.get("uploads", [])}

    sources = [
        REAL / "debit_note_DC-108478.pdf",
        REAL / "arrival_notice_NEPTUNE.pdf",
        REAL / "zim_invoice_scan.png",
        *sorted(DETACHED.glob("*.pdf")),
    ]
    session = requests.Session()
    session.trust_env = False
    urls: dict[str, str] = {}
    url_names: dict[str, str] = {}
    for path in sources:
        digest = sha256_file(path)
        if digest in reused:
            url = reused[digest]
        elif args.reuse_image_url and path.name == "zim_invoice_scan.png":
            url = args.reuse_image_url
        else:
            url = upload(path, session)
        urls[digest] = url
        url_names[digest] = path.name
        print(f"UPLOADED {path.name} -> {url}", flush=True)

    # pipeline 内部仍走标准 GatewayOcrProvider，仅把 URL 解析替换为本次文件映射。
    GatewayOcrProvider.resolve_url = lambda self, path: urls[sha256_file(path)]  # type: ignore[method-assign]

    llm_key = os.environ.get("SBA_LLM_API_KEY", "") if args.llm else ""
    settings = Settings.load(
        ocr__mode="gateway",
        llm__enabled="true" if args.llm else "false",
        llm__base_url=os.environ.get("SBA_LLM_BASE_URL", "http://127.0.0.1:8788/v1"),
        llm__api_key=llm_key,
        llm__model=os.environ.get("SBA_LLM_MODEL", "deepseek-v4.1-flash"),
        pipeline__accounting_entity="PltPlt",
        pipeline__entity_aliases=["湃乐多", "Pilot Aiship", "PLTPLT"],
        pipeline__pdf_strategy="ocr",
    )
    out_dir = Path(args.out)
    result = run([REAL], settings=settings, out_dir=out_dir)
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    counters = trace["counters"]
    manifest = {
        "run_id": trace["run_id"],
        "ocr_mode": "gateway",
        "llm_enabled": args.llm,
        "llm_base_url": settings.llm.base_url if args.llm else None,
        "llm_model": settings.llm.model if args.llm else None,
        "uploads": [{"name": url_names[d], "sha256": d, "url": u} for d, u in urls.items()],
        "counters": counters,
        "records": len(result.records),
        "errors": result.errors,
    }
    (out_dir / "online-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest["counters"], ensure_ascii=False), flush=True)
    if counters.get("ocr_calls") != 5 or counters.get("ocr_errors"):
        raise SystemExit("在线 OCR 验收失败：预期 5 次成功调用")
    if args.llm and (not counters.get("llm_calls") or counters.get("llm_errors")):
        raise SystemExit("在线 LLM 验收失败：未产生成功调用，请检查 WB 反代和模型名")


if __name__ == "__main__":
    main()
