"""从冻结样本输入生成盲标 CSV 与 SHA256 锁。

本脚本只做字段映射与格式校验，**不含任何判定逻辑、标签或答案**。
50 条样本的答案只存在于人工填写的 human_labels.csv 中。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "samples_50.json"
SHEET = HERE / "blind_annotation_sheet.csv"
LOCK = HERE / "samples_50.json.sha256"

EXPECTED_IDS = tuple(f"NR-{i:03d}" for i in range(1, 51))
INPUT_FIELDS = ("sample_id", "symbol", "published_at", "source", "text")
# 任何可能泄漏答案的字段名都禁止出现在评测输入里。
FORBIDDEN_FIELDS = frozenset((
    "verdict", "label", "answer", "gold", "risk_verdict", "human_verdict",
    "level", "case_type", "category", "expected", "expect", "risk", "no_risk",
))
SHEET_HEADER = (
    "sample_id", "symbol", "published_at", "source", "text",
    "human_verdict", "human_rationale", "annotator", "annotated_at",
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_items() -> list[dict]:
    raw = SAMPLES.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    items = document.get("items")
    if not isinstance(items, list):
        raise ValueError("samples_50.json 须包含 items 数组")
    if len(items) != 50:
        raise ValueError(f"正式评测固定 50 条，实际 {len(items)} 条")
    ids = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {position} 条不是对象")
        bad = FORBIDDEN_FIELDS.intersection(item)
        if bad:
            raise ValueError(f"第 {position} 条含疑似答案字段：{sorted(bad)}")
        missing = [key for key in INPUT_FIELDS
                   if not isinstance(item.get(key), str) or not item[key].strip()]
        if missing:
            raise ValueError(f"第 {position} 条缺失或为空：{missing}")
        ids.append(item["sample_id"])
    if tuple(ids) != EXPECTED_IDS:
        raise ValueError("样本 ID 必须为 NR-001..NR-050 顺序且唯一")
    return items


def main() -> int:
    try:
        items = load_items()
    except (OSError, ValueError) as exc:
        print(f"样本校验失败：{exc}", file=sys.stderr)
        return 1

    # 盲标表只有 5 列输入 + 4 列空白人工列；绝不写入模型答案。
    with SHEET.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_HEADER,
                                lineterminator="\n")
        writer.writeheader()
        for item in items:
            writer.writerow({**{key: item[key] for key in INPUT_FIELDS},
                             "human_verdict": "", "human_rationale": "",
                             "annotator": "", "annotated_at": ""})

    digest = sha256_bytes(SAMPLES.read_bytes())
    LOCK.write_bytes(f"{digest}  {SAMPLES.name}\n".encode("utf-8"))
    print(f"样本 {len(items)} 条；盲标表 -> {SHEET.name}（{len(SHEET_HEADER)} 列，人工列为空）")
    print(f"SHA256 锁 -> {LOCK.name}: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
