"""审计 P2 四项问题的「修复前 / 修复后」可复现对照脚本。

只读运行，不修改传入的任何仓库文件；结果打印为 JSON。
本脚本刻意不 import 本仓库的 news_risk 包，而是按 --root 动态加载，
因此可以对任意一棵干净检出树（例如 git archive 2786c70）复算。

用法（在课题目录下）：

    # 修复前：另建目录导出合并提交当时的树
    git archive 2786c70 "行业智能体/金融/投资组合风控助手" | tar -x -C /tmp/p2-before
    python eval/news_risk/repro_p2_audit.py \
        --root /tmp/p2-before/行业智能体/金融/投资组合风控助手 --mode before

    # 修复后：指向当前工作树
    python eval/news_risk/repro_p2_audit.py --root . --mode after

四项检查与另一台电脑 Codex 的反馈逐条对应：
  P2-1 v1 把其他监控主体的负面事件算到当前标的；
  P2-2 v2 把「部分产线恢复、另一条仍暂停」判成无风险；
  P2-3 冻结 samples_50.json 不含 entities，正式评测示例命令退出 1；
  P2-4 无时区的未来 sealed_at 能拿到 formal=true。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SYMBOLS = ("SIM-A", "SIM-B", "SIM-C", "SIM-D")


def import_from(root: Path, module: str):
    """按指定根目录加载模块，避免被当前工作目录的同名包抢先命中。"""
    for name in [n for n in sys.modules if n == "news_risk"
                 or n.startswith("news_risk.")]:
        del sys.modules[name]
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module(module)
    finally:
        sys.path.remove(str(root))


def p2_1(root: Path) -> dict:
    assess_news = import_from(root, "news_risk.rules").assess_news
    r = assess_news({"symbol": "SIM-A",
                     "text": "北辰智造集团股份有限公司收到行政处罚决定书。"})
    return {"risk_verdict": r["risk_verdict"],
            "evidence_status": r["evidence_status"]}


def p2_2(root: Path) -> dict:
    assess_news_v2 = import_from(root, "news_risk.rules_v2").assess_news_v2
    entities = json.loads(
        (root / "data/news_risk/dev_eval_samples.json").read_text(encoding="utf-8")
    )["entities"]
    r = assess_news_v2(
        {"symbol": "NEWS-A",
         "text": "公司否认全厂停产，同时确认两条产线运转正常，"
                 "但一条产线暂停，尚无复产日期。"}, entities)
    return {"risk_verdict": r["risk_verdict"],
            "evidence_status": r["evidence_status"]}


def run_cli(root: Path, *extra: str) -> dict:
    p = subprocess.run(
        [sys.executable, "-m", "news_risk",
         str(root / "data/news_risk/samples_50.json"),
         "--profile", "v2", *extra, "--validate-only"],
        cwd=str(root), capture_output=True)
    return {"exit": p.returncode,
            "stderr": p.stderr.decode("utf-8", "replace").strip()[:160]}


def p2_3(root: Path) -> dict:
    """旧文档命令 vs 新文档命令；两者都不修改冻结样本。"""
    out = {"documented_command_before": run_cli(root)}
    entity_file = root / "data/news_risk/sim_entities.json"
    if entity_file.exists():
        out["documented_command_after"] = run_cli(
            root, "--entities", str(entity_file))
    return out


def p2_4(root: Path) -> dict:
    """无时区 + 远在未来的封存时间：修复前能拿到 formal=true。"""
    score = root / "eval/news_risk/score.py"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        samples, lock = d / "samples_50.json", d / "samples_50.json.sha256"
        human, system = d / "human_labels.csv", d / "system_output.json"
        report = d / "consistency_report.json"
        registration, registry = (d / "annotation_registration.json",
                                  d / "sample_registry.json")
        items = [{
            "sample_id": f"NR-{i:03d}",
            "symbol": SYMBOLS[(i - 1) % 4],
            "published_at": "2026-11-05T10:00:00+08:00",
            "source": "工程自测模拟来源",
            "text": f"模拟舆情原文第 {i} 条，用于评分脚本机制验证。",
        } for i in range(1, 51)]
        raw = (json.dumps({"items": items}, ensure_ascii=False, indent=2)
               + "\n").encode("utf-8")
        samples.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        lock.write_bytes(f"{sha}  {samples.name}\n".encode("utf-8"))
        with human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"])
            for i in range(1, 51):
                writer.writerow([f"NR-{i:03d}", "no_risk"])
        rows = [{
            "sample_id": f"NR-{i:03d}",
            "symbol": SYMBOLS[(i - 1) % 4],
            "risk_verdict": "no_risk",
            "evidence": items[i - 1]["text"][:12],
            "level": "无",
            "review_action": "无需预警；按常规归档。",
            "profile": "v2",
        } for i in range(1, 51)]
        system.write_bytes((json.dumps({
            "schema": "topic16-news-risk-output-v1",
            "scope": "system-output", "profile": "v2",
            "count": len(rows), "items": rows,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        sealed_at = "2099-01-01T00:00:00"
        registration.write_bytes((json.dumps({
            "schema": "topic16-human-annotation-registration-v1",
            "sample_set_id": "TEST-SET",
            "samples_sha256": sha,
            "human_labels_sha256": hashlib.sha256(human.read_bytes()).hexdigest(),
            "annotator": "独立复核者（合成测试固定值）",
            "independent_review": True,
            "sealed_at": sealed_at,
            "profile": "v2",
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        registry.write_bytes((json.dumps({
            "schema": "topic16-news-sample-registry-v1",
            "registered_sets": [{
                "sample_set_id": "TEST-SET", "file": samples.name,
                "sha256": sha,
                "not_recommended_for_blind_acceptance": False,
            }],
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        p = subprocess.run([
            sys.executable, str(score),
            "--samples", str(samples), "--human", str(human),
            "--system", str(system), "--report", str(report),
            "--label-source", "human-annotation",
            "--registration", str(registration),
            "--sample-registry", str(registry), "--profile", "v2",
        ], cwd=str(root), capture_output=True)
        doc = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return {"sealed_at": sealed_at, "exit": p.returncode,
                "formal": doc.get("formal"), "status": doc.get("status"),
                "missing": [m for m in doc.get("formal_requirements_missing", [])
                            if "时区" in m or "晚于" in m]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="待复现的课题目录（可为干净导出的旧树）")
    parser.add_argument("--mode", choices=("before", "after"), default="after")
    args = parser.parse_args()
    root = args.root.resolve()
    result = {
        "schema": "topic16-news-risk-p2-audit-v1",
        "mode": args.mode,
        "scope": "p2-regression-reproduction",
        "diagnostic_only": True,
        "not_acceptance_evidence": True,
        "note": "合成 fixture，不是人工金标；仅复现工程缺陷的修复前后行为。",
        "p2_1_other_entity_bad_news": p2_1(root),
        "p2_2_residual_adverse": p2_2(root),
        "p2_3_frozen_samples_v2": p2_3(root),
        "p2_4_sealed_at_no_tz_future": p2_4(root),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
