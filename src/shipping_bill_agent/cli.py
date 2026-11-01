"""命令行入口：run（跑账单）/ check（校验输出契约）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings, load_dotenv
from .pipeline import run
from .schema import OPERATIONS, POSTING_TYPES

REQUIRED_KEYS = {
    "operation",
    "document_info",
    "transaction_party",
    "charge_details",
    "financial_attributes",
    "audit_trail",
}
DETAIL_KEYS = {"charge_item", "amount", "currency", "payment_term"}
ALLOWED_EXTENSIONS = {"charge_item_raw"}
DOC_INFO_KEYS = {"document_type", "reference_no", "document_date"}
PARTY_KEYS = {"issuer", "recipient"}
FIN_KEYS = {"accounting_entity", "posting_type"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shipping_bill_agent",
        description="航运智能账单助手：多格式文档 → 标准账单 JSON",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="处理文件/目录并输出标准 JSON")
    run_parser.add_argument("--input", nargs="+", required=True, help="文件或目录，可多个")
    run_parser.add_argument("--entity", default=None, help="记账主体（决定 AR/AP）")
    run_parser.add_argument(
        "--entity-alias",
        action="append",
        default=None,
        help="记账主体在单据上的其他写法，可重复；用于识别我方是否为单据接收方",
    )
    run_parser.add_argument("--bills", default=None, help="历史账单 JSON 路径")
    run_parser.add_argument("--out", default=None, help="输出目录")
    run_parser.add_argument(
        "--ocr-mode", choices=["gateway", "cassette", "stub"], default=None, help="OCR 提供方"
    )
    run_parser.add_argument(
        "--pdf-strategy", choices=["ocr", "text", "auto"], default=None, help="PDF 处理策略"
    )
    run_parser.add_argument("--llm", choices=["auto", "on", "off"], default=None, help="LLM 开关")
    run_parser.add_argument("--strict-schema", action="store_true", help="剔除扩展字段")
    run_parser.add_argument("--idempotent", action="store_true", help="重复投递保护")
    run_parser.add_argument("--json-only", action="store_true", help="只打印 result.json")
    run_parser.add_argument("--quiet", action="store_true", help="精简输出")

    check_parser = sub.add_parser("check", help="校验输出 JSON 是否符合契约")
    check_parser.add_argument("--input", required=True, help="result.json 路径")
    # 与 run 的默认口径保持一致：run 默认带 charge_item_raw 扩展字段，check 默认也放行，
    # 否则「run 完直接 check」必然失败。要严格按题目契约校验时加 --no-allow-extensions
    check_parser.add_argument(
        "--allow-extensions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="允许扩展字段（默认允许；--no-allow-extensions 严格按题目契约）",
    )
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if args.entity:
        overrides["pipeline__accounting_entity"] = args.entity
    if getattr(args, "entity_alias", None):
        overrides["pipeline__entity_aliases"] = list(args.entity_alias)
    if args.ocr_mode:
        overrides["ocr__mode"] = args.ocr_mode
    if args.pdf_strategy:
        overrides["pipeline__pdf_strategy"] = args.pdf_strategy
    if args.llm:
        overrides["llm__enabled"] = {"on": "true", "off": "false", "auto": "auto"}[args.llm]
    if args.strict_schema:
        overrides["pipeline__strict_schema"] = True
        overrides["pipeline__extension_fields"] = False
    if args.idempotent:
        overrides["pipeline__idempotent"] = True
    return Settings.load(**overrides)


def _print_summary(result: Any) -> None:
    records = result.records
    print(f"运行 ID：{result.recorder.run_id}")
    print(f"输出目录：{result.out_dir}")
    print(f"记录数：{len(records)}")
    counts: dict[str, int] = {}
    for record in records:
        counts[record["operation"]] = counts.get(record["operation"], 0) + 1
    if counts:
        print("操作分布：" + "，".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for path_name, path in (
        ("result.json", result.result_path),
        ("report.json", result.report_path),
        ("errors.json", result.errors_path),
    ):
        if path:
            print(f"  - {path_name}: {path}")
    if result.errors:
        print(f"解析错误：{len(result.errors)} 个（见 errors.json）")
    if result.degraded:
        print("提示：本次运行处于 degraded 模式（LLM 未启用），语义字段仅靠规则抽取")


def _check(payload: Any, *, allow_extensions: bool) -> list[str]:
    problems: list[str] = []
    if not isinstance(payload, list):
        return ["顶层必须是 JSON 数组"]

    for index, record in enumerate(payload):
        where = f"[{index}]"
        if not isinstance(record, dict):
            problems.append(f"{where} 不是对象")
            continue
        missing = REQUIRED_KEYS - set(record)
        if missing:
            problems.append(f"{where} 缺少键：{sorted(missing)}")
            continue

        if record["operation"] not in OPERATIONS:
            problems.append(f"{where}.operation 非法：{record['operation']!r}")

        for key, expected in (
            ("document_info", DOC_INFO_KEYS),
            ("transaction_party", PARTY_KEYS),
            ("financial_attributes", FIN_KEYS),
        ):
            block = record.get(key)
            if not isinstance(block, dict):
                problems.append(f"{where}.{key} 不是对象")
                continue
            lost = expected - set(block)
            if lost:
                problems.append(f"{where}.{key} 缺少键：{sorted(lost)}")

        details = record.get("charge_details")
        if not isinstance(details, dict):
            problems.append(f"{where}.charge_details 不是对象")
        else:
            allowed = DETAIL_KEYS | (ALLOWED_EXTENSIONS if allow_extensions else set())
            unexpected = set(details) - allowed
            if unexpected:
                problems.append(f"{where}.charge_details 存在多余键：{sorted(unexpected)}")
            lost = DETAIL_KEYS - set(details)
            if lost:
                problems.append(f"{where}.charge_details 缺少键：{sorted(lost)}")
            amount = details.get("amount")
            if amount is not None and not isinstance(amount, (int, float)):
                problems.append(f"{where}.charge_details.amount 必须是数字，实际 {type(amount).__name__}")
            currency = details.get("currency")
            if currency is not None and (not isinstance(currency, str) or len(currency) != 3):
                problems.append(f"{where}.charge_details.currency 必须是三位代码")

        posting = record["financial_attributes"].get("posting_type") if isinstance(
            record["financial_attributes"], dict
        ) else None
        if posting is not None and posting not in POSTING_TYPES:
            problems.append(f"{where}.financial_attributes.posting_type 非法：{posting!r}")

        audit = record.get("audit_trail")
        if not isinstance(audit, dict) or not isinstance(audit.get("ai_reasoning"), list):
            problems.append(f"{where}.audit_trail.ai_reasoning 必须是字符串数组")
        elif not all(isinstance(item, str) for item in audit["ai_reasoning"]):
            problems.append(f"{where}.audit_trail.ai_reasoning 含非字符串元素")

    return problems


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        path = Path(args.input).expanduser()
        if not path.exists():
            print(f"文件不存在：{path}", file=sys.stderr)
            return 2
        payload = json.loads(path.read_text(encoding="utf-8"))
        problems = _check(payload, allow_extensions=args.allow_extensions)
        if problems:
            print(f"契约校验失败，共 {len(problems)} 处：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"契约校验通过：{len(payload)} 条记录符合标准输出格式")
        return 0

    if args.command == "run":
        settings = _settings_from_args(args)
        out_dir = Path(args.out).expanduser() if args.out else None
        result = run(
            args.input,
            settings=settings,
            out_dir=out_dir,
            bills_path=args.bills,
        )
        if args.json_only:
            print(json.dumps(result.records, ensure_ascii=False, indent=2))
        elif args.quiet:
            print(json.dumps({"records": len(result.records), "out": str(result.out_dir)},
                             ensure_ascii=False))
        else:
            _print_summary(result)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
