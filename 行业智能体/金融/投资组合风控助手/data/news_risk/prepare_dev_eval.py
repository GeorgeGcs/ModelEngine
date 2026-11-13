"""把用户提供的 50 条舆情原文落地为开发评测输入。

**只读原文文件**，绝不读取参考判断文件；只做字段映射与结构校验，
不生成、不推断任何标签或答案。同时产出沿用同一批 ID 的空白盲标表，
供未来正式验收时由复核者按原文作人工判定。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "dev_eval_samples.json"
LOCK = Path(str(SAMPLES) + ".sha256")
BLIND_SHEET = HERE / "blind_annotation_sheet_dev_eval.csv"

FORBIDDEN_FIELDS = frozenset((
    "label", "verdict", "answer", "gold", "risk_verdict", "human_verdict",
    "level", "expected", "expect", "case_type", "category", "reason",
    "evidence_quote",
))
SHEET_HEADER = (
    "sample_id", "symbol", "target", "published_at", "source", "text",
    "human_verdict", "human_rationale", "annotator", "annotated_at",
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build(source: Path) -> dict:
    raw = source.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) != 50:
        raise ValueError("原文文件须含 50 条 cases")
    entities: dict[str, dict] = {}
    items: list[dict] = []
    ids: list[str] = []
    for position, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"第 {position} 条不是对象")
        bad = FORBIDDEN_FIELDS.intersection(case)
        if bad:
            raise ValueError(f"第 {position} 条含疑似答案字段：{sorted(bad)}")
        for key in ("id", "target_id", "target", "published_at", "source_type", "text"):
            value = case.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"第 {position} 条缺失或为空：{key}")
        symbol = case["target_id"]
        entities.setdefault(symbol, {"canonical": case["target"], "aliases": []})
        ids.append(case["id"])
        items.append({
            "sample_id": case["id"],
            "symbol": symbol,
            "target": case["target"],
            "published_at": case["published_at"],
            "source": case["source_type"],
            "text": case["text"],
        })
    if len(set(ids)) != 50:
        raise ValueError("样本 ID 必须唯一")
    return {
        "schema": "topic16-news-risk-dev-eval-input-v1",
        "dataset": document.get("dataset", "unknown"),
        "scope": "development-evaluation-input",
        "input_only_note": "本文件只含原文与主体信息，不含任何参考判断、标签或答案；被测模块的预测阶段只读本文件。",
        "data_note": document.get("source", "虚构模拟数据"),
        "disclaimer": "风险提示仅供参考，不构成投资建议；系统输出为辅助建议，重大舆情须人工核实。",
        "entities": entities,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="用户提供的 50 条舆情原文 JSON（只含输入，不含答案）")
    args = parser.parse_args()
    try:
        document = build(args.source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"输入准备失败：{exc}", file=sys.stderr)
        return 1

    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    SAMPLES.write_bytes(raw)
    LOCK.write_bytes(f"{sha256_bytes(raw)}  {SAMPLES.name}\n".encode("utf-8"))

    with BLIND_SHEET.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_HEADER,
                                lineterminator="\n")
        writer.writeheader()
        for item in document["items"]:
            writer.writerow({
                **{key: item[key] for key in SHEET_HEADER[:6]},
                "human_verdict": "", "human_rationale": "",
                "annotator": "", "annotated_at": "",
            })
    print(f"原文 {len(document['items'])} 条 -> {SAMPLES.name}")
    print(f"主体 {len(document['entities'])} 个：{sorted(document['entities'])}")
    print(f"空白盲标表（沿用同一批 ID）-> {BLIND_SHEET.name}")
    print(f"SHA256 锁 -> {LOCK.name}: {sha256_bytes(raw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
