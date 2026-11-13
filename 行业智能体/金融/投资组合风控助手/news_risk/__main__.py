"""舆情风险研判：批量 CLI 入口（python -m news_risk）。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .rules import DISCLAIMER, ENTITIES, assess_batch
from .rules_v2 import assess_batch_v2
from eval.news_risk.provenance import code_identity, judgment_policy, prediction_digest

OUTPUT_SCHEMA = "topic16-news-risk-output-v1"
INPUT_FIELDS = ("symbol", "text")
FORBIDDEN_FIELDS = frozenset((
    "verdict", "label", "answer", "gold", "risk_verdict", "human_verdict",
    "level", "expected", "expect", "case_type", "category", "reason",
    "evidence_quote",
))
# 审计 P1-6：嵌套的参考标签/答案对象同样拒绝，且正常预测与 --validate-only 共用同一套校验。
NESTED_FORBIDDEN = FORBIDDEN_FIELDS | frozenset((
    "references", "label_origin", "human_review_status", "reference_label",
    "reference_reason", "system_verdict",
))


def find_forbidden(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key in NESTED_FORBIDDEN:
                found.append(key)
            found.extend(find_forbidden(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_forbidden(child))
    return found


def load_input(path: Path) -> tuple[dict, list[dict], dict]:
    """只读输入文件；返回原始文档、条目与（可选）动态主体映射，绝不读取参考判断。"""
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(document, list):
        return document, document, {}
    if isinstance(document, dict) and isinstance(document.get("items"), list):
        entities = document.get("entities") or {}
        if not isinstance(entities, dict):
            raise ValueError("输入文件的 entities 须为对象")
        return document, document["items"], entities
    raise ValueError("输入须为舆情条目数组，或含 items 数组的对象")


def load_entities(path: Path) -> tuple[dict, str]:
    """只读独立主体映射文件，返回映射与文件字节的 SHA256。

    审计 P2：冻结样本 samples_50.json 不含 entities，旧版 --profile v2 会直接退出 1。
    该入口允许在不改冻结样本的前提下写定 v2 profile；哈希写入系统输出头，
    由正式评分器与登记清单核对，避免悄悄换主体映射影响判定。
    """
    raw = path.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    bad = sorted(set(find_forbidden(document)))
    if bad:
        raise ValueError(f"主体映射文件含疑似答案/参考判断字段：{bad}")
    mapping = (document.get("entities") if isinstance(document, dict)
               and "entities" in document else document)
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("主体映射须为非空对象，或含非空 entities 对象")
    for symbol, profile in mapping.items():
        if not isinstance(symbol, str) or not isinstance(profile, dict):
            raise ValueError("主体映射格式须为 {标的代码: {canonical: 全称, aliases: [...]}}")
        canonical = profile.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError(f"{symbol} 缺 canonical 主体全称")
        aliases = profile.get("aliases", [])
        if not isinstance(aliases, (list, tuple)) or \
                any(not isinstance(alias, str) for alias in aliases):
            raise ValueError(f"{symbol} 的 aliases 须为字符串数组")
    return mapping, hashlib.sha256(raw).hexdigest()


def validate_input(document: object, items: list[dict], entities: dict,
                   profile: str) -> dict:
    """结构校验：--validate-only 与正常预测都调用它，不允许两条路径松紧不一。"""
    bad = sorted(set(find_forbidden(document)))
    if bad:
        raise ValueError(f"输入含疑似答案/参考判断字段：{bad}")
    if profile == "v2" and not entities:
        raise ValueError("--profile v2 需要输入文件提供 entities 主体映射，或用 --entities 指定独立主体文件")
    known = entities if profile == "v2" else ENTITIES
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {position} 条不是对象")
        for key in INPUT_FIELDS:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"第 {position} 条缺失或为空：{key}")
        if item["symbol"] not in known:
            raise ValueError(f"第 {position} 条标的未知：{item['symbol']}")
    return {
        "mode": "validate-only",
        "profile": profile,
        "count": len(items),
        "note": "仅校验输入结构，未运行研判、未生成任何判定或答案。",
    }


def render(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="批量研判虚构舆情样本，输出结构化结果。")
    parser.add_argument("input", type=Path, help="JSON 文件：条目数组或含 items 数组的对象")
    parser.add_argument("--output", type=Path, default=None, help="结果写入文件（默认标准输出）")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--sample-set-id", default=None, help="实际输入集合 ID；正式运行须与预登记一致")
    parser.add_argument("--validate-only", action="store_true",
                        help="只校验输入结构，不产出判定")
    parser.add_argument("--profile", choices=("v1", "v2"), default="v1",
                        help="v1：内置 SIM-* 主体与原有冻结集；v2：动态主体 + 增强语义规则")
    parser.add_argument("--baseline", action="store_true",
                        help="仅 --profile v2 有效：关闭部分否认与主体排除例外，用于修复前后对照")
    parser.add_argument("--entities", type=Path, default=None,
                        help="v2 独立主体映射 JSON；用于冻结样本不含 entities 时，"
                             "不修改冻结件即可写定 v2。文件 SHA256 会写入系统输出头")
    args = parser.parse_args()

    try:
        document, items, entities = load_input(args.input)
        entities_sha: str | None = None
        if args.entities is not None:
            if args.profile != "v2":
                raise ValueError("--entities 只对 --profile v2 有效")
            entities, entities_sha = load_entities(args.entities)
        validation = validate_input(document, items, entities, args.profile)
        if entities_sha:
            validation["entities_sha256"] = entities_sha
        if args.validate_only:
            payload = {**validation, "schema": OUTPUT_SCHEMA}
        else:
            if args.profile == "v2":
                results = assess_batch_v2(items, entities, baseline=args.baseline)
                profile_label = "v2-baseline" if args.baseline else "v2"
            else:
                if args.baseline:
                    raise ValueError("--baseline 只对 --profile v2 有效")
                results = assess_batch(items)
                profile_label = "v1"
                # 正式评分器的严格模式会逐行核对 profile；v1 规则本身不带该字段，
                # 由 CLI 在系统输出层统一补齐，避免 50 条正确结果被误报为结构错误。
                for row in results:
                    row["profile"] = profile_label
            payload = {
                "schema": OUTPUT_SCHEMA,
                "scope": "system-output",
                "profile": profile_label,
                "count": len(results),
                "note": "系统输出，非人工判定；须由独立评分脚本在预测完成后再与参考判断对齐。",
                "disclaimer": DISCLAIMER,
                "items": results,
                "run_id": args.run_id or 'news-'+uuid.uuid4().hex,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sample_set_id": args.sample_set_id or (document.get('sample_set_id') if isinstance(document,dict) else None),
                "samples_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
                "code_identity": code_identity(),
                "judgment_policy": judgment_policy(),
            }
            for row in results:
                row['code_sha256'] = payload['code_identity']['sha256']
                row['run_id'] = payload['run_id']
                row['sample_set_id'] = payload['sample_set_id']
            if entities_sha:
                payload["entities_sha256"] = entities_sha
            payload['payload_sha256'] = prediction_digest(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"研判失败：{exc}", file=sys.stderr)
        return 1

    raw = render(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(raw)  # 固定 LF，避免 Windows 换行改变证据字节
        print(f"已写出 {len(items)} 条 -> {args.output.as_posix()}")
    else:
        sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
