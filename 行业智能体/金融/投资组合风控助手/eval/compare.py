"""把回放（或后续 Nexent 编排）的最终信号与冻结标注逐格比对。

独立性约束（这是本文件存在的意义）：
- 只使用 Python 标准库，不读取被测规则实现来推导预期；
- 预期全部来自手工标注文件 expected_20.json，并用 SHA256 锁校验未被改动；
- 本模块与被测代码之间唯一的接口是「结果文件里的 signals 数组」。

因此同一份标注既能核对离线回放层，也能在接入 Nexent 后核对编排后的
最终持久化信号：只要把最终信号导出成相同结构，换 --actual 即可。

判定口径：
- 少一条预期信号 -> FN（漏报）
- 多一条信号、或在非触发格出现信号 -> FP（误报）
- 同一业务键出现两条及以上 -> DUPLICATE，多出的每条同时计 FP
- 命中但 price / threshold 与标注不符 -> MISMATCH，同时计 FN 与 FP
- 结构错误、event_id 冲突、冻结锁不符 -> 直接判失败
"""

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SIGNAL_FIELDS = (
    "event_id", "portfolio_id", "timestamp", "symbol",
    "rule", "price", "threshold",
)
TRIGGER_FIELDS = (
    "annotation_id", "case", "portfolio_id", "timestamp",
    "symbol", "rule", "price", "threshold",
)
NON_TRIGGER_FIELDS = (
    "portfolio_id", "timestamp", "symbol", "price",
    "stop_loss", "take_profit", "case",
)
EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
ROOT = Path(__file__).resolve().parents[1]


class FrozenAnnotationError(Exception):
    """冻结标注缺失、被改动或结构不合法。"""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_lock(expected_path: Path) -> str:
    lock_path = expected_path.with_name(expected_path.name + ".sha256")
    if not lock_path.exists():
        raise FrozenAnnotationError(f"缺少冻结锁文件：{lock_path}")
    token = lock_path.read_text(encoding="utf-8").split()
    if not token or not EVENT_ID_RE.fullmatch(token[0]):
        raise FrozenAnnotationError(f"冻结锁格式不合法：{lock_path}")
    return token[0]


def validate_expected(doc: dict) -> list[str]:
    errors: list[str] = []
    source = doc.get("source", {})
    triggers = doc.get("triggers")
    non_trigger = doc.get("non_trigger_cells")
    if not isinstance(triggers, list) or not isinstance(non_trigger, list):
        return ["标注须含 triggers 与 non_trigger_cells 数组"]

    declared = source.get("cells_total")
    if (len(triggers), len(non_trigger), declared) != (20, 28, 48):
        errors.append("本套评测须覆盖 20 个触发格和 28 个非触发格，共 48 格")
    if len(triggers) != source.get("trigger_count"):
        errors.append("triggers 条数与 source.trigger_count 不符")
    if len(non_trigger) != source.get("non_trigger_count"):
        errors.append("non_trigger_cells 条数与 source.non_trigger_count 不符")
    if len(triggers) + len(non_trigger) != declared:
        errors.append("触发格 + 非触发格之和与 source.cells_total 不符")

    for index, item in enumerate(triggers, start=1):
        missing = [field for field in TRIGGER_FIELDS if field not in item]
        if missing:
            errors.append(f"triggers[{index}] 缺字段 {missing}")
        if item.get("rule") not in ("stop_loss", "take_profit"):
            errors.append(f"triggers[{index}] rule 不合法")
    for index, item in enumerate(non_trigger, start=1):
        missing = [field for field in NON_TRIGGER_FIELDS if field not in item]
        if missing:
            errors.append(f"non_trigger_cells[{index}] 缺字段 {missing}")

    cells = [
        (item.get("portfolio_id"), item.get("timestamp"), item.get("symbol"))
        for item in list(triggers) + list(non_trigger)
    ]
    if len(set(cells)) != len(cells):
        errors.append("存在重复的（组合, 时间, 标的）格子，非触发区间不完整")
    business_keys = [
        (item.get("portfolio_id"), item.get("timestamp"),
         item.get("symbol"), item.get("rule"))
        for item in triggers
    ]
    if len(set(business_keys)) != len(business_keys):
        errors.append("存在重复的预期触发业务键")

    timestamps = set(source.get("timestamps_utc", []))
    for item in list(triggers) + list(non_trigger):
        if item.get("timestamp") not in timestamps:
            errors.append(f"标注时间 {item.get('timestamp')} 不在 source.timestamps_utc 中")
    return errors


def load_expected(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    locked = read_lock(path)
    if digest != locked:
        raise FrozenAnnotationError(
            "冻结标注已被改动，拒绝比对。\n"
            f"  锁文件记录：{locked}\n"
            f"  当前文件值：{digest}\n"
            "  标注一旦冻结不得随被测结果调整；确需修订请走显式评审并重新冻结。"
        )
    doc = json.loads(raw.decode("utf-8-sig"))
    errors = validate_expected(doc)
    if errors:
        raise FrozenAnnotationError("冻结标注结构不合法：\n  - " + "\n  - ".join(errors))
    return doc, digest


def load_actual(raw: bytes) -> list[dict]:
    doc = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(doc, dict) or not isinstance(doc.get("signals"), list):
        raise ValueError("结果文件须为含 signals 数组的对象")
    signals = doc["signals"]
    if "signal_count" in doc and doc["signal_count"] != len(signals):
        raise ValueError(
            f"signal_count={doc['signal_count']} 与 signals 长度 {len(signals)} 不符，结果文件可能被改动"
        )
    return signals


def business_key(signal: dict) -> tuple:
    return (
        signal.get("portfolio_id"), signal.get("timestamp"),
        signal.get("symbol"), signal.get("rule"),
    )


def cell_key(signal: dict) -> tuple:
    return (signal.get("portfolio_id"), signal.get("timestamp"), signal.get("symbol"))


def describe_key(key: tuple) -> str:
    return " | ".join(str(part) for part in key)


def portable(path: Path) -> str:
    """报告里写相对路径，避免证据文件带本机绝对路径。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<outside-repo>/" + path.name


def compare(expected: dict, signals: list[dict], expected_digest: str,
            actual_digest: str, expected_path: Path, actual_path: Path,
            scope: str) -> dict:
    triggers = expected["triggers"]
    non_trigger = expected["non_trigger_cells"]
    expected_by_key = {business_key(t): t for t in triggers}
    expected_cells = {
        cell_key(t): ("trigger", t) for t in triggers
    }
    expected_cells.update({cell_key(c): ("non_trigger", c) for c in non_trigger})

    structural_errors: list[str] = []
    for index, signal in enumerate(signals, start=1):
        if not isinstance(signal, dict):
            structural_errors.append(f"signals[{index}] 不是对象")
            continue
        missing = [f for f in SIGNAL_FIELDS if f not in signal]
        if missing:
            structural_errors.append(f"signals[{index}] 缺字段 {missing}")
            continue
        if not EVENT_ID_RE.fullmatch(str(signal["event_id"])):
            structural_errors.append(f"signals[{index}] event_id 不是 64 位小写十六进制")
        if not UTC_RE.fullmatch(str(signal["timestamp"])):
            structural_errors.append(f"signals[{index}] timestamp 不是 UTC（...Z）格式：{signal['timestamp']}")
        if signal["rule"] not in ("stop_loss", "take_profit"):
            structural_errors.append(f"signals[{index}] rule 不合法：{signal['rule']}")

    groups: dict[tuple, list[tuple[int, dict]]] = defaultdict(list)
    for index, signal in enumerate(signals, start=1):
        if isinstance(signal, dict):
            groups[business_key(signal)].append((index, signal))

    # event_id 在不同业务键之间必须唯一，否则去重会误伤。
    id_owner: dict[str, tuple] = {}
    for key, items in groups.items():
        for index, signal in items:
            event_id = str(signal.get("event_id"))
            if event_id in id_owner and id_owner[event_id] != key:
                structural_errors.append(
                    f"signals[{index}] event_id 与另一业务键冲突：{event_id}"
                )
            id_owner[event_id] = key

    TP = FP = FN = duplicates = mismatches = 0
    differences: list[dict] = []

    for key, items in groups.items():
        if len(items) > 1:
            duplicates += len(items) - 1
            differences.append({
                "type": "DUPLICATE",
                "key": describe_key(key),
                "count": len(items),
                "indices": [index for index, _ in items],
                "event_ids": [str(s.get("event_id")) for _, s in items],
                "detail": "同一业务键出现多条信号，多出的每条计为 FP",
            })

    for key, items in groups.items():
        annotation = expected_by_key.get(key)
        if annotation is None:
            for index, signal in items:
                FP += 1
                cell = cell_key(signal)
                kind, cell_annotation = expected_cells.get(cell, ("undefined", None))
                if kind == "non_trigger":
                    reason = "非触发格出现信号（误触发）"
                else:
                    reason = "未定义格出现信号（组合/时间/标的不在冻结评测集内）"
                differences.append({
                    "type": "UNEXPECTED",
                    "key": describe_key(key),
                    "index": index,
                    "reason": reason,
                    "cell_case": cell_annotation.get("case") if cell_annotation else None,
                    "actual": signal,
                })
            continue

        first_index, first = items[0]
        wrong = {
            field: {"expected": annotation[field], "actual": first.get(field)}
            for field in ("price", "threshold")
            if str(first.get(field)) != str(annotation[field])
        }
        if wrong:
            mismatches += 1
            FN += 1
            FP += 1
            differences.append({
                "type": "MISMATCH",
                "key": describe_key(key),
                "annotation_id": annotation["annotation_id"],
                "index": first_index,
                "fields": wrong,
                "actual": first,
            })
        else:
            TP += 1
        for index, extra in items[1:]:
            FP += 1
            differences.append({
                "type": "EXTRA_DUPLICATE",
                "key": describe_key(key),
                "index": index,
                "reason": "重复信号计为 FP",
                "actual": extra,
            })

    for key, annotation in expected_by_key.items():
        if key not in groups:
            FN += 1
            differences.append({
                "type": "MISSING",
                "key": describe_key(key),
                "annotation_id": annotation["annotation_id"],
                "case": annotation["case"],
                "reason": "预期触发信号缺失（漏报）",
                "expected": {
                    field: annotation[field]
                    for field in ("rule", "price", "threshold")
                },
            })

    passed = (
        TP == expected["source"]["trigger_count"]
        and FP == 0 and FN == 0
        and duplicates == 0 and mismatches == 0
        and not structural_errors
    )
    return {
        "schema": "topic16-compare-report-v1",
        "scope": scope,
        "pass": passed,
        "metrics": {
            "TP": TP,
            "FP": FP,
            "FN": FN,
            "duplicates": duplicates,
            "mismatches": mismatches,
            "expected_triggers": expected["source"]["trigger_count"],
            "expected_cells": expected["source"]["cells_total"],
            "actual_signals": len(signals),
        },
        "expected_file": portable(expected_path),
        "expected_sha256": expected_digest,
        "actual_file": portable(actual_path),
        "actual_sha256": actual_digest,
        "differences": differences,
        "structural_errors": structural_errors,
    }


def print_summary(report: dict) -> None:
    m = report["metrics"]
    print(f"比对范围：{report['scope']}")
    print(f"冻结标注 SHA256：{report['expected_sha256']}（校验通过）")
    print(
        "TP={TP} FP={FP} FN={FN} duplicates={duplicates} mismatches={mismatches} "
        "actual_signals={actual_signals} expected_triggers={expected_triggers} "
        "expected_cells={expected_cells}".format(**m)
    )
    for error in report["structural_errors"]:
        print(f"  [结构错误] {error}")
    for diff in report["differences"]:
        print(f"  [{diff['type']}] {diff.get('key', '')} {diff.get('reason', '')}")
        if diff["type"] == "MISMATCH":
            print(f"      字段差异：{diff['fields']}")
        if diff["type"] == "MISSING":
            print(f"      标注：{diff['annotation_id']} {diff['case']}")
    print("结论：" + (f"PASS —— {m['TP']} 个触发点全部命中，无误触发、无重复、无字段偏差"
                     if report["pass"] else "FAIL —— 见上列差异明细"))


def main(argv: list[str] | None = None) -> int:
    eval_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, default=eval_dir / "expected_20.json")
    parser.add_argument("--actual", type=Path,
                        default=eval_dir / "results" / "replay_actual.json")
    parser.add_argument("--report", type=Path,
                        default=eval_dir / "results" / "compare_report.json")
    parser.add_argument("--scope", choices=("offline-replay-layer", "end-to-end", "local-fixture"), default="offline-replay-layer",
                        help="证据范围标签；接入 Nexent 核对最终持久化信号时改为 end-to-end")
    parser.add_argument("--refresh-freeze", action="store_true",
                        help="显式重算冻结锁并退出；仅用于标注经评审后的合法修订")
    args = parser.parse_args(argv)

    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")

    if args.refresh_freeze:
        raw = args.expected.read_bytes()
        errors = validate_expected(json.loads(raw.decode("utf-8-sig")))
        if errors:
            print("拒绝重新冻结：" + "；".join(errors), file=sys.stderr)
            return 2
        lock = args.expected.with_name(args.expected.name + ".sha256")
        lock.write_text(f"{sha256_bytes(raw)}  {args.expected.name}\n", encoding="utf-8", newline="\n")
        print(f"已重新冻结：{lock} -> {sha256_bytes(raw)}")
        print("注意：修订标注必须留下评审记录，不得为了让被测结果通过而改标注。")
        return 0

    try:
        expected, expected_digest = load_expected(args.expected)
        actual_raw = args.actual.read_bytes()
        signals = load_actual(actual_raw)
    except (OSError, ValueError, FrozenAnnotationError, json.JSONDecodeError) as exc:
        print(f"比对中止：{exc}", file=sys.stderr)
        return 2

    report = compare(
        expected, signals, expected_digest, sha256_bytes(actual_raw),
        args.expected, args.actual, args.scope,
    )
    if args.scope == "end-to-end":
        report["scope_note"] = "来源由调用者声明；需凭 Nexent 运行记录与持久化导出核实。"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print_summary(report)
    print(f"报告：{portable(args.report)}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
