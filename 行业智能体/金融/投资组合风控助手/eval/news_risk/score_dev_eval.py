"""开发评测评分器：「与 AI 参考答案的一致率」（复核加固版）。

严格时序：
  阶段 1 只读并校验**系统预测**（由 python -m news_risk 从原文文件产出）；
  阶段 1 校验不通过时直接退出（退出码 3），**不打开参考判断文件、不写报告**；
  阶段 2 仅在阶段 1 完全通过后，才读取用户本机的 AI 参考判断文件，按 ID 对齐。

复核加固（Codex 审计）：
- 递归扫描样本/预测里的嵌套答案字段；证据片段必须是该条原文的子串；
  可用 --expect-profile 强制核对预测文件与每行的研判版本。
- 报告字段改为 ai_reference_match_count / ai_reference_sample_count /
  ai_reference_match_rate，**删除任何验收门槛布尔值**：本脚本不判断达标与否，
  退出码 0 只表示「结构合法、已完成比对」，1 表示结构错误。
- 每次运行写 <report>.status.json，带 run_id、退出码、输入摘要与 report_valid；
  失败运行**不写新报告**，但会就地覆写同路径的旧报告为
  topic16-dev-eval-report-invalidated-v1（顶层 report_valid=false、
  不再暴露 ai_reference_match_count，旧内容整体挪进 stale_report），
  使只读报告文件的消费方也不会把上一次的一致条数当成本次结果。

本脚本的结果**不是**「与人工判定一致率」，也不是验收证据：
参考判断由 AI 起草、未经独立人工判定。正式验收须由复核者按
blind_annotation_sheet_dev_eval.csv 的同一批 ID 作人工判定，并改用 score.py。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAMPLES = ROOT / "data/news_risk/dev_eval_samples.json"
DEFAULT_PREDICTIONS = ROOT / "eval/news_risk/results/dev_eval_predictions.json"
DEFAULT_REPORT = ROOT / "eval/news_risk/results/dev_eval_consistency_report.json"

DENOMINATOR = 50
VALID_VERDICTS = frozenset(("risk", "no_risk"))
VALID_LEVELS = frozenset(("高", "中", "低", "待复核", "无"))
REQUIRED_FIELDS = ("sample_id", "symbol", "risk_verdict", "evidence",
                   "level", "review_action")
REFERENCE_KEYS = frozenset((
    "references", "label_origin", "human_review_status", "evidence_quote",
    "reference_label", "reference_reason", "system_verdict", "reason",
    "answer", "gold", "label", "verdict", "expected", "expect",
    "case_type", "category", "human_verdict",
))
FORBIDDEN_SAMPLE_FIELDS = REFERENCE_KEYS | {"risk_verdict", "level"}
FORBIDDEN_PREDICTION_FIELDS = REFERENCE_KEYS  # risk_verdict / level 是合法输出字段
LABEL_ALIASES = {
    "有风险信号": "risk", "risk": "risk", "1": "risk", "是": "risk",
    "无风险信号": "no_risk", "no_risk": "no_risk", "0": "no_risk", "否": "no_risk",
}
AI_LABEL_ORIGIN = "AI 起草的参考判断"
UNREVIEWED = "未经独立人工判定"
DISCLAIMER = "风险提示仅供参考，不构成投资建议；系统输出为辅助建议，重大舆情须人工核实。"


def portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<outside-repo>/" + path.name


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def find_forbidden(value: object, forbidden: frozenset[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key in forbidden:
                found.append(key)
            found.extend(find_forbidden(child, forbidden))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_forbidden(child, forbidden))
    return found


def evidence_from_text(evidence: str, text: str) -> bool:
    core = evidence.strip().strip("…").strip()
    return bool(core) and core in text


def write_status(report: Path, run_id: str, state: str, exit_code: int, *,
                 report_valid: bool, reference_read: bool,
                 samples: Path, predictions: Path, reference: Path | None,
                 previous_report_invalidated: bool = False) -> None:
    """每次运行都写状态文件；消费方必须核对 run_id / exit_code / report_valid。"""
    payload = {
        "schema": "topic16-dev-eval-run-status-v1",
        "run_id": run_id,
        "state": state,
        "exit_code": exit_code,
        "report_valid": report_valid,
        "reference_read": reference_read,
        "previous_report_invalidated": previous_report_invalidated,
        "inputs": {
            "samples": portable(samples),
            "samples_sha256": file_digest(samples),
            "predictions": portable(predictions),
            "predictions_sha256": file_digest(predictions),
            "ai_reference": portable(reference) if reference else None,
        },
        "note": "失败运行不写新报告；若同路径留有旧报告，本次已就地覆写为失效状态。"
                "消费方仍须按本次 run_id、exit_code 与 report_valid 判断，"
                "不得引用 stale_report 里的旧数字。",
    }
    sidecar = Path(str(report) + ".status.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def invalidate_report(report: Path, *, run_id: str, state: str,
                      exit_code: int, problems: list[str]) -> bool:
    """失败运行时就地标记旧报告失效，不依赖可能被忽略/丢失的 sidecar。

    旧报告内容整体挪进 stale_report：任何只读顶层字段的消费方都会看到
    report_valid=false，且拿不到 ai_reference_match_count，不会被旧数字误导。
    返回是否确实覆写过一份旧报告。
    """
    if not report.is_file():
        return False
    try:
        stale = json.loads(report.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        stale = {"unparseable_previous_report": True}
    if isinstance(stale, dict) and stale.get("schema") == \
            "topic16-dev-eval-report-invalidated-v1":
        stale = stale.get("stale_report", {})  # 幂等，不层层嵌套
    payload = {
        "schema": "topic16-dev-eval-report-invalidated-v1",
        "scope": "ai-reference-development-evaluation",
        "report_valid": False,
        "invalidated_by_run_id": run_id,
        "invalidation_state": state,
        "invalidation_exit_code": exit_code,
        "invalidation_problems": problems,
        "stale_report": stale,
        "metric_name": "与 AI 参考答案的一致率",
        "not_human_judgment_agreement": True,
        "not_acceptance_evidence": True,
        "note": "上一次失败运行已使同路径的旧报告失效；旧数字仅保留在 stale_report 供审计，"
                "不得作为本次结果引用。请按本次 run_id 的状态文件与退出码重跑。",
        "disclaimer": DISCLAIMER,
    }
    report.write_bytes(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return True


def load_samples(path: Path) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    raw = path.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    items = document.get("items")
    if not isinstance(items, list) or len(items) != DENOMINATOR:
        return [], [f"开发评测输入固定 {DENOMINATOR} 条"]
    ids = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"样本第 {position} 条不是对象")
            continue
        bad = sorted(set(find_forbidden(item, FORBIDDEN_SAMPLE_FIELDS)))
        if bad:
            errors.append(f"样本第 {position} 条含疑似答案字段：{bad}")
        sid = item.get("sample_id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"样本第 {position} 条缺 sample_id")
        else:
            ids.append(sid)
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            errors.append(f"样本第 {position} 条缺原文")
    if len(set(ids)) != DENOMINATOR:
        errors.append("样本 ID 必须 50 个且唯一")
    lock = Path(str(path) + ".sha256")
    if not lock.exists():
        errors.append("缺少输入 SHA256 锁")
    elif not lock.read_text(encoding="utf-8").split() \
            or lock.read_text(encoding="utf-8").split()[0] != sha256_bytes(raw):
        errors.append("输入 SHA256 与锁不一致")
    return items, errors


def load_predictions(path: Path, samples: list[dict], *,
                     expect_profile: str | None) -> tuple[dict[str, dict], list[str]]:
    """阶段 1：只读系统预测，绝不接触参考判断文件。"""
    errors: list[str] = []
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    header = document if isinstance(document, dict) else {}
    items = document.get("items") if isinstance(document, dict) else document
    if not isinstance(items, list):
        return {}, ["预测文件须为数组或含 items 数组"]
    if expect_profile and header.get("profile") != expect_profile:
        errors.append(f"预测文件 profile 与期望不一致：{header.get('profile')!r}")
    expected = {item["sample_id"]: item for item in samples
                if isinstance(item, dict) and "sample_id" in item}
    outputs: dict[str, dict] = {}
    for position, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            errors.append(f"预测第 {position} 条不是对象")
            continue
        bad = sorted(set(find_forbidden(row, FORBIDDEN_PREDICTION_FIELDS)))
        if bad:
            errors.append(f"预测第 {position} 条含疑似答案字段：{bad}")
        sid = row.get("sample_id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"预测第 {position} 条缺 sample_id")
            continue
        sid = sid.strip()
        if sid in outputs:
            errors.append(f"{sid} ## 预测重复")
            continue
        if sid not in expected:
            errors.append(f"{sid} ## 预测含评测集以外 ID")
            continue
        missing = [key for key in REQUIRED_FIELDS
                   if not isinstance(row.get(key), str) or not row[key].strip()]
        if missing:
            errors.append(f"{sid} ## 预测缺失或为空字段：{missing}")
        else:
            if row["risk_verdict"] not in VALID_VERDICTS:
                errors.append(f"{sid} ## risk_verdict 非法：{row['risk_verdict']!r}")
            if row["level"] not in VALID_LEVELS:
                errors.append(f"{sid} ## level 非法：{row['level']!r}")
            if row["symbol"] != expected[sid].get("symbol"):
                errors.append(f"{sid} ## 预测标的与样本不一致")
            if not evidence_from_text(row["evidence"], expected[sid].get("text", "")):
                errors.append(f"{sid} ## 证据片段不是该条原文的子串")
            if expect_profile and row.get("profile") != expect_profile:
                errors.append(f"{sid} ## 预测 profile 与期望不一致")
        outputs[sid] = row
    return outputs, errors


def load_reference(path: Path) -> tuple[dict[str, dict], list[str]]:
    """阶段 2：预测校验完成后才调用；只在本机读取 AI 参考判断。"""
    errors: list[str] = []
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if document.get("label_origin") != AI_LABEL_ORIGIN:
        errors.append("参考文件 label_origin 不是「AI 起草的参考判断」；"
                      "人工判定请改用 score.py 走正式验收流程")
    if document.get("human_review_status") != UNREVIEWED:
        errors.append("参考文件 human_review_status 不是「未经独立人工判定」；"
                      "本脚本只用于 AI 参考答案开发评测")
    refs = document.get("references")
    if not isinstance(refs, list) or len(refs) != DENOMINATOR:
        return {}, errors + [f"参考判断须为 {DENOMINATOR} 条"]
    labels: dict[str, dict] = {}
    for position, entry in enumerate(refs, start=1):
        if not isinstance(entry, dict):
            errors.append(f"参考第 {position} 条不是对象")
            continue
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"参考第 {position} 条缺 id")
            continue
        label = LABEL_ALIASES.get(str(entry.get("label", "")).strip())
        if label is None:
            errors.append(f"{sid} ## 参考标签非法：{entry.get('label')!r}")
            continue
        if sid in labels:
            errors.append(f"{sid} ## 参考判断重复")
            continue
        labels[sid] = {
            "verdict": label,
            "label": entry.get("label"),
            "evidence_quote": entry.get("evidence_quote", ""),
            "reason": entry.get("reason", ""),
        }
    return labels, errors


def score(samples: list[dict], predictions: dict[str, dict],
          references: dict[str, dict], structural_errors: list[str], *,
          samples_path: Path, predictions_path: Path, reference_path: Path,
          profile: str, variant: str, run_id: str) -> dict:
    matched = 0
    false_positives: list[dict] = []
    false_negatives: list[dict] = []
    differences: list[dict] = []
    for item in samples:
        sid = item["sample_id"]
        ref = references.get(sid)
        pred = predictions.get(sid)
        if not ref or not pred or pred.get("risk_verdict") not in VALID_VERDICTS:
            differences.append({"sample_id": sid, "kind": "unscorable"})
            continue
        if ref["verdict"] == pred["risk_verdict"]:
            matched += 1
            continue
        detail = {
            "sample_id": sid,
            "symbol": item.get("symbol"),
            "text": item.get("text"),
            "reference_label": ref["label"],
            "reference_evidence_quote": ref["evidence_quote"],
            "reference_reason": ref["reason"],
            "predicted_verdict": pred["risk_verdict"],
            "predicted_level": pred.get("level"),
            "predicted_evidence": pred.get("evidence"),
            "predicted_evidence_status": pred.get("evidence_status"),
            "predicted_reason_codes": pred.get("reason_codes", []),
        }
        if ref["verdict"] == "no_risk":
            false_positives.append(detail)
            differences.append({"sample_id": sid, "kind": "false_positive",
                                "reference": "no_risk", "predicted": "risk"})
        else:
            false_negatives.append(detail)
            differences.append({"sample_id": sid, "kind": "false_negative",
                                "reference": "risk", "predicted": "no_risk"})

    return {
        "schema": "topic16-ai-reference-agreement-v2",
        "scope": "ai-reference-development-evaluation",
        "metric_name": "与 AI 参考答案的一致率",
        "not_human_judgment_agreement": True,
        "not_acceptance_evidence": True,
        "not_an_acceptance_threshold": True,
        "label_origin": AI_LABEL_ORIGIN,
        "human_review_status": UNREVIEWED,
        "reference_read_after_prediction_validation": True,
        "ai_reference_sample_count": DENOMINATOR,
        "ai_reference_match_count": matched,
        "ai_reference_match_rate": round(matched / DENOMINATOR, 4),
        "false_positive_count": len(false_positives),
        "false_negative_count": len(false_negatives),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "differences": differences,
        "structural_errors": structural_errors,
        "profile": profile,
        "variant": variant,
        "run_id": run_id,
        "evidence": {
            "samples": portable(samples_path),
            "predictions": portable(predictions_path),
            "ai_reference": portable(reference_path),
        },
        "note": "本结果仅与 AI 起草的参考答案比对，不是与人工判定一致率，不可作为活动验收证据。"
                "报告不含任何达标布尔值：达到 43/50 才达标的门槛只属于正式人工评测。"
                "报告只含差异条目的参考摘录，不含完整答案集。",
        "generalization_caveat": "研判词表由本批原文归纳，本结果属于同批样本上的开发评测，"
                                 "不能外推到未见样本；正式验收应另用独立人工判定，"
                                 "并建议增加留出集或新增样本检验泛化能力。",
        "disclaimer": DISCLAIMER,
    }


def emit(payload: dict, report: Path | None) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(raw)
    sys.stdout.buffer.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="开发评测：计算系统预测与 AI 参考答案的一致率（非人工判定、非验收证据）。")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--reference", type=Path, required=True,
                        help="用户本机的 AI 参考判断 JSON（评分阶段才读取，不入库）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--profile", default="from-predictions",
                        help="仅作报告标注：v2 / v2-baseline / v1")
    parser.add_argument("--variant", default="final",
                        help="仅作报告标注：baseline（修复前）或 final（修复后）")
    parser.add_argument("--expect-profile", default=None,
                        help="写定研判版本；核对预测文件头与每条预测，不一致即结构错误")
    parser.add_argument("--run-id", default="dev-eval",
                        help="本次运行编号；状态文件按此编号留痕")
    args = parser.parse_args()

    def finish(state: str, code: int, payload: dict, *, report_valid: bool,
               reference_read: bool, write_report: bool = False) -> int:
        emit(payload, args.report if write_report else None)
        invalidated = False
        if not write_report and not report_valid:
            raw_problems = payload.get("problems") or []
            problems = [str(p) for p in raw_problems][:10] \
                if isinstance(raw_problems, list) else [str(raw_problems)]
            invalidated = invalidate_report(args.report, run_id=args.run_id,
                                            state=state, exit_code=code,
                                            problems=problems)
        write_status(args.report, args.run_id, state, code,
                     report_valid=report_valid, reference_read=reference_read,
                     samples=args.samples, predictions=args.predictions,
                     reference=args.reference if reference_read else None,
                     previous_report_invalidated=invalidated)
        return code

    try:
        samples, sample_errors = load_samples(args.samples)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return finish("invalid-samples", 3,
                      {"status": "invalid-samples", "problems": [str(exc)]},
                      report_valid=False, reference_read=False)
    if sample_errors:
        return finish("invalid-samples", 3,
                      {"status": "invalid-samples", "problems": sample_errors},
                      report_valid=False, reference_read=False)

    # ---- 阶段 1：只读预测 ----
    if not args.predictions.exists():
        return finish("missing-predictions", 2, {
            "status": "missing-predictions",
            "problems": [f"系统输出不存在：{portable(args.predictions)}；"
                         "请先运行 python -m news_risk ... --output ..."],
        }, report_valid=False, reference_read=False)
    try:
        predictions, prediction_errors = load_predictions(
            args.predictions, samples, expect_profile=args.expect_profile)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return finish("invalid-predictions", 3,
                      {"status": "invalid-predictions", "problems": [str(exc)]},
                      report_valid=False, reference_read=False)
    if len(predictions) != DENOMINATOR:
        prediction_errors.append(
            f"预测须覆盖 {DENOMINATOR} 个 ID，实际 {len(predictions)} 个")
    if prediction_errors:
        # 严格时序门禁：预测结构校验未通过，绝不打开参考判断文件、绝不写一致率报告。
        return finish("invalid-predictions", 3, {
            "status": "invalid-predictions",
            "scope": "ai-reference-development-evaluation",
            "metric_name": "与 AI 参考答案的一致率",
            "not_acceptance_evidence": True,
            "reference_read": False,
            "structural_errors": prediction_errors,
            "problems": prediction_errors,
            "note": "预测结构校验未通过：本阶段不读取 AI 参考判断，也不生成一致率报告。",
        }, report_valid=False, reference_read=False)

    # ---- 阶段 2：阶段 1 全部通过后，才读取参考判断 ----
    if not args.reference.exists():
        return finish("missing-ai-reference", 2, {
            "status": "missing-ai-reference",
            "problems": [f"AI 参考判断文件不存在：{portable(args.reference)}"],
        }, report_valid=False, reference_read=False)
    try:
        references, reference_errors = load_reference(args.reference)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return finish("invalid-ai-reference", 3, {
            "status": "invalid-ai-reference", "problems": [str(exc)],
        }, report_valid=False, reference_read=True)

    errors = sample_errors + prediction_errors + reference_errors
    if set(references) != {item["sample_id"] for item in samples}:
        errors.append("参考判断 ID 与评测输入未完全对齐")
    report = score(samples, predictions, references, errors,
                   samples_path=args.samples, predictions_path=args.predictions,
                   reference_path=args.reference, profile=args.profile,
                   variant=args.variant, run_id=args.run_id)
    code = 0 if not errors else 1
    emit(report, args.report)
    invalidated = False
    if errors:
        # 结构错误的本次报告同样不能在磁盘上留成「有效一致率」。
        invalidated = invalidate_report(
            args.report, run_id=args.run_id,
            state="scored-with-structural-errors", exit_code=code,
            problems=[str(e) for e in errors][:10])
    write_status(args.report, args.run_id,
                 "scored" if not errors else "scored-with-structural-errors", code,
                 report_valid=not errors, reference_read=True,
                 samples=args.samples, predictions=args.predictions,
                 reference=args.reference,
                 previous_report_invalidated=invalidated)
    if args.report:
        sys.stderr.write(
            f"报告已写出：{portable(args.report)}；"
            f"与 AI 参考答案一致 {report['ai_reference_match_count']}/{DENOMINATOR}，"
            f"误报 {report['false_positive_count']}，漏报 {report['false_negative_count']}。"
            "注意：这不是与人工判定一致率，不是验收证据，也不判断达标。\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
