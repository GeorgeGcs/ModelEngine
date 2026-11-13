"""过拟合诊断：统计 v2 词表短语「逐字出现在 50 条开发评测原文」中的比例。

这不是一致率指标，也不是验收证据。它只回答一个工程问题：
本轮 50/50 的结果有多少来自同批原文的短语拟合，因此能否外推到未见样本。
比例越高，越说明需要在正式验收时另作独立人工判定并增加留出集。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "news_risk/rules_v2.py"
SAMPLES = ROOT / "data/news_risk/dev_eval_samples.json"
DEFAULT_REPORT = ROOT / "eval/news_risk/results/vocab_overlap.json"


def portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<outside-repo>/" + path.name


def collect_marker_groups(source: str) -> dict[str, list[str]]:
    """从 AST 取各 *_MARKERS 元组与 ADVERSE_PATTERNS 的词表，不执行被测模块。

    审计 P1-9：旧版只处理 ast.Assign，遗漏带类型注解的
    `ADVERSE_PATTERNS: tuple[...] = ...`（ast.AnnAssign），少算了一整组词表。
    """
    tree = ast.parse(source)
    groups: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets
                     if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        else:
            continue
        if value is None:
            continue
        for name in names:
            if name == "ADVERSE_PATTERNS":
                markers: list[str] = []
                if isinstance(value, (ast.Tuple, ast.List)):
                    for entry in value.elts:
                        if (isinstance(entry, (ast.Tuple, ast.List))
                                and len(entry.elts) >= 2
                                and isinstance(entry.elts[1], (ast.Tuple, ast.List))):
                            # 只取每组的第 2 项（短语元组），不把第 3 项「原因说明」当短语。
                            markers.extend(
                                item.value for item in entry.elts[1].elts
                                if isinstance(item, ast.Constant)
                                and isinstance(item.value, str)
                            )
                groups[name] = markers
            elif name.endswith("_MARKERS"):
                groups[name] = [
                    item.value for item in ast.walk(value)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
    return groups


def diagnose(rules_path: Path | None = None, note: str | None = None) -> dict:
    rules_path = rules_path or RULES
    groups = collect_marker_groups(rules_path.read_text(encoding="utf-8"))
    document = json.loads(SAMPLES.read_text(encoding="utf-8-sig"))
    texts = [item["text"] for item in document["items"]]
    corpus = "\n".join(texts)
    breakdown = {}
    all_markers: list[str] = []
    for name, markers in sorted(groups.items()):
        unique = list(dict.fromkeys(markers))
        all_markers.extend(unique)
        verbatim = [marker for marker in unique if marker in corpus]
        breakdown[name] = {
            "markers": len(unique),
            "verbatim_in_50_samples": len(verbatim),
            "rate": round(len(verbatim) / len(unique), 4) if unique else None,
        }
    unique_all = list(dict.fromkeys(all_markers))
    verbatim_all = [marker for marker in unique_all if marker in corpus]
    return {
        "schema": "topic16-news-risk-vocab-overlap-v1",
        "scope": "overfit-diagnostic",
        "diagnostic_only": True,
        "not_acceptance_evidence": True,
        "not_an_agreement_metric": True,
        "samples": "data/news_risk/dev_eval_samples.json",
        "rules": portable(rules_path),
        "sample_count": len(texts),
        "total_markers": len(unique_all),
        "verbatim_in_samples": len(verbatim_all),
        "verbatim_rate": round(len(verbatim_all) / len(unique_all), 4)
        if unique_all else None,
        "breakdown": breakdown,
        "not_verbatim_examples": [m for m in unique_all if m not in corpus][:20],
        "adverse_patterns_included": "ADVERSE_PATTERNS" in breakdown
        and breakdown.get("ADVERSE_PATTERNS", {}).get("markers", 0) > 0,
        "audit_note": note or "",
        "note": "比例越高，说明词表越贴近本批原文，开发评测的高一致率越可能是同批拟合；"
                "该数字不能证明或否定泛化能力，只提示正式验收须用独立人工判定，"
                "并建议增加留出集或新增样本。",
        "disclaimer": "风险提示仅供参考，不构成投资建议；系统输出为辅助建议，重大舆情须人工核实。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--rules", type=Path, default=None,
                        help="可指定历史版本的 rules_v2.py 复核词表统计")
    parser.add_argument("--note", default=None, help="写入报告的审计说明")
    args = parser.parse_args()
    payload = diagnose(args.rules, args.note)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(raw)
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
