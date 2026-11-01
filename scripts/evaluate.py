"""评测脚本：字段级 P/R/F1 + 操作类型混淆矩阵 + 风险召回 + 回归门禁。

用法：
    python scripts/evaluate.py            # 打印报告
    python scripts/evaluate.py --gate     # 触发门禁（不达标退出码 1）
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipping_bill_agent.audit import Recorder  # noqa: E402
from shipping_bill_agent.config import Settings  # noqa: E402
from shipping_bill_agent.pipeline import run  # noqa: E402

FIELDS = (
    "document_type",
    "reference_no",
    "document_date",
    "issuer",
    "recipient",
    "charge_item",
    "amount",
    "currency",
    "payment_term",
    "posting_type",
    "operation",
)
OPERATIONS = ("CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW")


def _flatten(record: dict) -> dict:
    return {
        "document_type": record["document_info"]["document_type"],
        "reference_no": record["document_info"]["reference_no"],
        "document_date": record["document_info"]["document_date"],
        "issuer": record["transaction_party"]["issuer"],
        "recipient": record["transaction_party"]["recipient"],
        "charge_item": record["charge_details"]["charge_item"],
        "amount": record["charge_details"]["amount"],
        "currency": record["charge_details"]["currency"],
        "payment_term": record["charge_details"]["payment_term"],
        "posting_type": record["financial_attributes"]["posting_type"],
        "operation": record["operation"],
    }


def _key(flat: dict) -> tuple[str, str]:
    return (str(flat["reference_no"]), str(flat["charge_item"]))


def _equal(field_name: str, left, right) -> bool:
    if field_name == "amount":
        if left is None or right is None:
            return left == right
        return abs(float(left) - float(right)) <= 0.005
    return left == right


def evaluate(*, gate: bool) -> int:
    settings = Settings().with_overrides(pipeline__pdf_strategy="auto")
    result = run(
        [ROOT / "data" / "samples"],
        settings=settings,
        out_dir=ROOT / "out" / "eval",
        bills_path=ROOT / "data" / "existing_bills.json",
        recorder=Recorder(),
    )
    golden_payload = json.loads(
        (ROOT / "data" / "golden" / "expected.json").read_text(encoding="utf-8")
    )
    golden = golden_payload["records"]

    actual_list = [_flatten(record) for record in result.records]
    actual = {_key(item): item for item in actual_list}
    gold_map = {_key(item): item for item in golden}

    missing = sorted(set(gold_map) - set(actual))
    extra = sorted(set(actual) - set(gold_map))

    # ---- 字段级指标（金标记录为准，缺失记录视为全错）
    field_stats: dict[str, dict[str, float | int]] = {}
    tp_total = fp_total = fn_total = 0
    for field_name in FIELDS:
        tp = fp = fn = 0
        for key, expected in gold_map.items():
            got = actual.get(key)
            if got is None:
                fn += 1
                continue
            if _equal(field_name, got[field_name], expected[field_name]):
                tp += 1
            else:
                fn += 1
        for key in set(actual) - set(gold_map):
            fp += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        field_stats[field_name] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        }
        tp_total += tp
        fp_total += fp
        fn_total += fn

    micro_p = tp_total / (tp_total + fp_total) if (tp_total + fp_total) else 0.0
    micro_r = tp_total / (tp_total + fn_total) if (tp_total + fn_total) else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0

    # ---- 操作类型混淆矩阵
    matrix: dict[str, Counter] = {op: Counter() for op in OPERATIONS}
    for key, expected in gold_map.items():
        got = actual.get(key)
        predicted = got["operation"] if got else "MISSING"
        matrix.setdefault(expected["operation"], Counter())[predicted] += 1

    # ---- 风险召回：金标中标记为需人审的，实际是否拦住
    risk_expected = golden_payload.get("risk_expected", [])
    caught = 0
    for item in risk_expected:
        key = (item["reference_no"], item["charge_item"])
        got = actual.get(key)
        if got and got["operation"] == "PENDING_REVIEW":
            caught += 1
    risk_recall = caught / len(risk_expected) if risk_expected else 1.0

    # ---- 记录级完全正确率
    full_correct = 0
    for key, expected in gold_map.items():
        got = actual.get(key)
        if got and all(_equal(f, got[f], expected[f]) for f in FIELDS):
            full_correct += 1
    record_accuracy = full_correct / len(gold_map) if gold_map else 0.0

    report = {
        "records": {"golden": len(gold_map), "actual": len(actual), "missing": missing, "extra": extra},
        "micro": {"precision": round(micro_p, 4), "recall": round(micro_r, 4), "f1": round(micro_f1, 4)},
        "fields": field_stats,
        "operation_matrix": {op: dict(counter) for op, counter in matrix.items()},
        "risk_recall": round(risk_recall, 4),
        "record_accuracy": round(record_accuracy, 4),
        "degraded": result.degraded,
    }

    out_path = ROOT / "out" / "eval" / "evaluation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_report(report)

    if gate:
        failures = []
        if micro_f1 < 0.98:
            failures.append(f"字段级 micro-F1 {micro_f1:.4f} < 0.98")
        if risk_recall < 0.95:
            failures.append(f"风险召回 {risk_recall:.4f} < 0.95")
        if record_accuracy < 0.95:
            failures.append(f"记录级完全正确率 {record_accuracy:.4f} < 0.95")
        if failures:
            print("\n[门禁未通过]")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("\n[门禁通过]")
    return 0


def _print_report(report: dict) -> None:
    records = report["records"]
    print("=" * 68)
    print(f"金标记录 {records['golden']} 条 / 实际输出 {records['actual']} 条")
    if records["missing"]:
        print(f"  缺失：{records['missing']}")
    if records["extra"]:
        print(f"  多余：{records['extra']}")
    print(f"降级模式：{'是（LLM 未启用）' if report['degraded'] else '否'}")
    print("-" * 68)
    print(f"{'字段':<26}{'P':>8}{'R':>8}{'F1':>8}")
    for name, stats in report["fields"].items():
        print(f"{name:<26}{stats['precision']:>8.3f}{stats['recall']:>8.3f}{stats['f1']:>8.3f}")
    print("-" * 68)
    micro = report["micro"]
    print(f"micro  P={micro['precision']:.3f}  R={micro['recall']:.3f}  F1={micro['f1']:.3f}")
    print(f"记录级完全正确率：{report['record_accuracy']:.3f}")
    print(f"风险召回：{report['risk_recall']:.3f}")
    print("-" * 68)
    print("操作类型混淆矩阵（行=金标，列=预测）")
    header = "".join(f"{op:>15}" for op in OPERATIONS)
    print(f"{'':<16}{header}")
    for op in OPERATIONS:
        row = report["operation_matrix"].get(op, {})
        cells = "".join(f"{row.get(col, 0):>15}" for col in OPERATIONS)
        print(f"{op:<16}{cells}")
    print("=" * 68)


def main() -> int:
    parser = argparse.ArgumentParser(description="账单助手评测")
    parser.add_argument("--gate", action="store_true", help="启用回归门禁")
    args = parser.parse_args()
    return evaluate(gate=args.gate)


if __name__ == "__main__":
    raise SystemExit(main())
