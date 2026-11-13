"""课题 16 舆情一致率独立评分器（v5，人工与预测证据分离）。

只用 Python 标准库；**不导入 news_risk 或任何被测模块**，不读取、不生成样本答案。
按样本 ID 对齐人工判定与系统输出，检查漏项、重复、无效输出，报告一致条数、
分母、误报、漏报与逐条差异。正式评测固定 50 条，≥43 条一致才达标。

复核加固（Codex 审计 P0）：
  「算出 50 条里一致几条」与「出具正式人工验收结论」是两件事。
  默认 --label-source=unverified：**允许算数，但绝不授予 formal=true**。
  只有同时提供以下证据链，才可能 formal=true：
    1) --label-source human-annotation；
    2) --registration 封存记录（样本哈希、人工表哈希、独立复核者、封存时间、规则 profile）；
    3) 该样本集在 --sample-registry 里**运行前登记**，且未被标记「已公开开发标签」；
    4) 样本锁、样本/人工表实际哈希、系统输出 profile 全部对得上；
    5) 人工表 symbol 逐行与样本一致，无结构错误。≥43 的数值门槛另行核对。
  脚本只能核对记录是否齐全；标签究竟由谁作出，仍须靠真实独立复核过程。

状态与退出码：
  0  正式通过（formal=true，证据链完整且达标）
  1  未完成正式证据链 / 结构错误 / 不达标：可算一致数，但不是验收证据
  2  待人工标注 / 缺系统输出：只输出工程状态，不写正式通过报告
  3  输入文件非法
"""

from __future__ import annotations

import argparse
import csv
import hashlib
try:
    from .provenance import (binding_problems, REGISTRATION_V2,
                             independence_evidence, human_policy_evidence)
except ImportError:
    from provenance import (binding_problems, REGISTRATION_V2,
                            independence_evidence, human_policy_evidence)
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAMPLES = ROOT / "data/news_risk/samples_50.json"
DEFAULT_HUMAN = ROOT / "data/news_risk/human_labels.csv"
DEFAULT_SYSTEM = ROOT / "eval/news_risk/results/system_output.json"
DEFAULT_REPORT = ROOT / "eval/news_risk/results/consistency_report.json"
DEFAULT_REGISTRY = ROOT / "data/news_risk/sample_registry.json"
PREREGISTRATION_PATH = ROOT / "data/news_risk/acceptance_preregistration.json"

DENOMINATOR = 50
THRESHOLD = 43
# 独立预登记摘要的「关键常量」同时内置在评分器里：即使有人改了自供的登记文件，
# 也无法把正式候选换掉或把已公开答案的开发集洗白成正式集。
BUILTIN_PREREGISTRATION = {
    "sample_set_id": "NR-2026-11-09",
    "samples_sha256": "22dfb8652a0d5b6d69bb611525bccc97a591c5c2e4f1b959e65ad86b08635878",
    "entities_sha256": "4a27094d4e428d1395eb635ad34db6a3191f865311eea320f4763e427455ef12",
    "profile": "v2",
    "denominator": 50,
    "threshold": 43,
}
FORBIDDEN_SAMPLE_FINGERPRINTS = frozenset((
    "11f92d6db0294eb6931e8cc55d62ddd61fba9edbdb50e84ae28dbd6214794767",
))
PREREGISTRATION_SCHEMA = "topic16-news-acceptance-preregistration-v1"
VALID_VERDICTS = frozenset(("risk", "no_risk"))
VALID_LEVELS = frozenset(("高", "中", "低", "待复核", "无"))
REQUIRED_SYSTEM_FIELDS = ("sample_id", "symbol", "risk_verdict", "evidence",
                          "level", "review_action")
FORBIDDEN_SAMPLE_FIELDS = frozenset((
    "verdict", "label", "answer", "gold", "risk_verdict", "human_verdict",
    "level", "expected", "expect", "case_type", "category",
))
# 递归扫描：嵌套的参考标签、答案对象同样拒绝（审计 P1-6）。
# 样本侧禁用键包含 level/expected 等；预测侧 level 是合法输出字段，故单独一套。
REFERENCE_KEYS = frozenset((
    "references", "label_origin", "human_review_status", "evidence_quote",
    "reference_label", "reference_reason", "system_verdict", "reason",
))
FORBIDDEN_SAMPLE_KEYS = FORBIDDEN_SAMPLE_FIELDS | REFERENCE_KEYS
# 预测行的 risk_verdict / level 是合法输出字段，不能当答案字段误杀。
FORBIDDEN_NESTED_KEYS = (
    FORBIDDEN_SAMPLE_FIELDS - {"level", "risk_verdict"}) | REFERENCE_KEYS
REGISTRATION_SCHEMA = "topic16-human-annotation-registration-v1"
REGISTRY_SCHEMA = "topic16-news-sample-registry-v1"
DISCLAIMER = "风险提示仅供参考，不构成投资建议；系统输出为辅助建议，重大舆情须人工核实。"

HUMAN_ALIASES = {
    "risk": "risk", "风险": "risk", "1": "risk", "是": "risk", "y": "risk",
    "yes": "risk", "true": "risk",
    "no_risk": "no_risk", "非风险": "no_risk", "0": "no_risk", "否": "no_risk",
    "n": "no_risk", "no": "no_risk", "false": "no_risk",
}


def portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<outside-repo>/" + path.name


def normalize_human(value: object) -> str | None:
    if value is None:
        return None
    return HUMAN_ALIASES.get(str(value).strip().lower())


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def find_forbidden_keys(value: object,
                        forbidden: frozenset[str] = FORBIDDEN_NESTED_KEYS) -> list[str]:
    """递归找嵌套答案字段，防止把参考判断藏进子对象/数组里。"""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key in forbidden:
                found.append(key)
            found.extend(find_forbidden_keys(child, forbidden))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_forbidden_keys(child, forbidden))
    return found


def evidence_from_text(evidence: str, text: str) -> bool:
    """证据片段必须来自该条原文；允许研判器在两端加省略号。"""
    core = evidence.strip().strip("…").strip()
    return bool(core) and core in text


def load_samples(path: Path) -> tuple[list[dict], list[str], bool, str]:
    errors: list[str] = []
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    document = json.loads(raw.decode("utf-8-sig"))
    items = document.get("items") if isinstance(document, dict) else document
    if not isinstance(items, list):
        return [], ["样本文件须含 items 数组"], False, digest
    if len(items) != DENOMINATOR:
        errors.append(f"正式评测固定 {DENOMINATOR} 条，样本实际 {len(items)} 条")
    ids: list[str] = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"样本第 {position} 条不是对象")
            continue
        bad = sorted(set(find_forbidden_keys(item, FORBIDDEN_SAMPLE_KEYS)))
        if bad:
            errors.append(f"样本第 {position} 条含疑似答案字段：{bad}")
        if not isinstance(item.get("sample_id"), str) or not item["sample_id"].strip():
            errors.append(f"样本第 {position} 条缺 sample_id")
        else:
            ids.append(item["sample_id"])
        if not isinstance(item.get("symbol"), str) or not item["symbol"].strip():
            errors.append(f"样本第 {position} 条缺 symbol")
    # ID 取自所选冻结样本文件本身；是否被「事先登记」由 registry 核对，
    # 单靠旁边的 .sha256 锁不能证明评测集是运行前选定的。
    if len(ids) != DENOMINATOR or len(set(ids)) != DENOMINATOR:
        errors.append("样本须为 50 个唯一 ID，且不得重复")

    lock_verified = False
    lock = path.with_suffix(path.suffix + ".sha256")
    if not lock.exists():
        errors.append("缺少样本 SHA256 锁文件，无法确认评测集未被改动")
    else:
        recorded = lock.read_text(encoding="utf-8").split()
        if not recorded or recorded[0] != digest:
            errors.append("样本 SHA256 与锁不一致，评测集可能被改动")
        else:
            lock_verified = True
    return items, errors, lock_verified, digest


def load_human(path: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """返回 (判定, 人工表里的 symbol, 错误)。symbol 列若存在须逐行核对。"""
    errors: list[str] = []
    rows: list[tuple[object, object, object]] = []
    if path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = document.get("items") if isinstance(document, dict) else document
        if not isinstance(entries, list):
            return {}, {}, ["人工判定 JSON 须为数组或含 items 数组"]
        for entry in entries:
            if isinstance(entry, dict):
                rows.append((entry.get("sample_id"), entry.get("human_verdict"),
                             entry.get("symbol")))
            else:
                rows.append((None, None, None))
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "sample_id" not in reader.fieldnames \
                    or "human_verdict" not in reader.fieldnames:
                return {}, {}, ["人工判定 CSV 须含 sample_id 与 human_verdict 两列"]
            for row in reader:
                rows.append((row.get("sample_id"), row.get("human_verdict"),
                             row.get("symbol")))

    labels: dict[str, str] = {}
    symbols: dict[str, str] = {}
    for position, (sample_id, raw_value, symbol) in enumerate(rows, start=1):
        sid = str(sample_id).strip() if sample_id is not None else ""
        if not sid:
            errors.append(f"人工判定第 {position} 行缺 sample_id")
            continue
        verdict = normalize_human(raw_value)
        if verdict is None:
            errors.append(f"{sid} ## 人工判定为空或非法：{raw_value!r}")
            continue
        if sid in labels:
            errors.append(f"{sid} ## 人工判定重复")
            continue
        labels[sid] = verdict
        if isinstance(symbol, str) and symbol.strip():
            symbols[sid] = symbol.strip()
    return labels, symbols, errors


def load_system(path: Path, samples: list[dict], *, strict: bool,
                expected_profile: str | None) -> tuple[dict[str, dict], dict, list[str]]:
    errors: list[str] = []
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    header = document if isinstance(document, dict) else {}
    bad_header = sorted(set(find_forbidden_keys({k:v for k,v in header.items() if k != 'items'})))
    if bad_header:
        errors.append('输出头含疑似答案字段：'+str(bad_header))
    items = document.get("items") if isinstance(document, dict) else document
    if not isinstance(items, list):
        return {}, header, ["系统输出须为数组或含 items 数组"]
    texts = {item.get("sample_id"): item.get("text", "") for item in samples
             if isinstance(item, dict)}
    expected_symbols = {item.get("sample_id"): item.get("symbol") for item in samples
                        if isinstance(item, dict)}
    if strict and expected_profile:
        if header.get("profile") != expected_profile:
            errors.append(f"系统输出 profile 与封存记录不一致：{header.get('profile')!r}")
    outputs: dict[str, dict] = {}
    for position, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            errors.append(f"系统输出第 {position} 条不是对象")
            continue
        sid = row.get("sample_id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"系统输出第 {position} 条缺 sample_id")
            continue
        sid = sid.strip()
        if sid in outputs:
            errors.append(f"{sid} ## 系统输出重复")
            continue
        if sid not in expected_symbols:
            errors.append(f"{sid} ## 系统输出包含评测集以外的 ID")
            continue
        missing = [key for key in REQUIRED_SYSTEM_FIELDS
                   if key not in row or not isinstance(row.get(key), str)
                   or not row[key].strip()]
        if missing:
            errors.append(f"{sid} ## 系统输出缺失或为空字段：{missing}")
        else:
            if row["risk_verdict"] not in VALID_VERDICTS:
                errors.append(f"{sid} ## risk_verdict 非法：{row['risk_verdict']!r}")
            if row["level"] not in VALID_LEVELS:
                errors.append(f"{sid} ## level 非法：{row['level']!r}")
            if row["symbol"] != expected_symbols[sid]:
                errors.append(f"{sid} ## 系统标的与样本不一致：{row['symbol']!r}")
            if strict:
                if not evidence_from_text(row["evidence"], texts.get(sid, "")):
                    errors.append(f"{sid} ## 证据片段不是该条原文的子串")
                if expected_profile and row.get("profile") != expected_profile:
                    errors.append(f"{sid} ## 预测 profile 与封存记录不一致")
        if strict:
            bad = sorted(set(find_forbidden_keys(row)))
            if bad:
                errors.append(f"{sid} ## 预测含疑似答案字段：{bad}")
        outputs[sid] = row
    return outputs, header, errors


def read_registry(path: Path) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    if not path.exists():
        return [], [f"未找到运行前登记的样本集清单：{portable(path)}"]
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], [f"样本集登记文件不可解析：{exc}"]
    if document.get("schema") != REGISTRY_SCHEMA:
        problems.append("样本集登记文件 schema 不符")
    entries = document.get("registered_sets")
    if not isinstance(entries, list):
        problems.append("样本集登记文件缺 registered_sets 数组")
        entries = []
    return [e for e in entries if isinstance(e, dict)], problems


def load_preregistration() -> tuple[dict, list[str]]:
    """读取随仓库提交的独立预登记摘要，并与评分器内置常量交叉核对。

    返回 (摘要, 问题列表)。问题非空时不得授予 formal=true。
    """
    problems: list[str] = []
    try:
        raw = PREREGISTRATION_PATH.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"独立预登记摘要不可读：{exc}"]
    if not isinstance(document, dict) \
            or document.get("schema") != PREREGISTRATION_SCHEMA:
        return {}, ["独立预登记摘要 schema 不符"]
    candidate = document.get("formal_candidate")
    if not isinstance(candidate, dict):
        return {}, ["独立预登记摘要缺 formal_candidate"]
    for key, expected in BUILTIN_PREREGISTRATION.items():
        if candidate.get(key) != expected:
            problems.append(
                f"预登记摘要 formal_candidate.{key} 与评分器内置常量不一致")
    declared_forbidden = {
        item.get("samples_sha256")
        for item in document.get("forbidden_sets", [])
        if isinstance(item, dict)
    }
    if not FORBIDDEN_SAMPLE_FINGERPRINTS.issubset(declared_forbidden):
        problems.append("预登记摘要未登记全部内置禁用样本指纹")
    summary = {
        "file": portable(PREREGISTRATION_PATH),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "candidate": candidate,
        "forbidden": sorted(declared_forbidden),
    }
    return summary, problems


def verify_provenance(*, label_source: str, registration_path: Path | None,
                      registry_path: Path, samples_path: Path, human_path: Path,
                      system_path: Path, samples_sha: str, human_sha: str,
                      lock_verified: bool, expected_profile: str | None,
                      system_header: dict, structural_errors: list[str],
                      consistent: int, preregistration: dict,
                      preregistration_problems: list[str],
                      fixture_mode: bool,
                      human_symbol_rows: int, correction_path: Path | None = None,
                      annotation_policy_path: Path | None = None,
                      annotation_manifest_path: Path | None = None) -> tuple[bool, list[str], dict]:
    """逐项核对「正式评测是否有效」；分数是否达标不在这里判定。"""
    missing: list[str] = []
    details: dict = {
        "label_source": label_source,
        "registration": portable(registration_path) if registration_path else None,
        "sample_registry": portable(registry_path),
        "samples_sha256": samples_sha,
        "human_labels_sha256": human_sha,
        "fixture_mode": fixture_mode,
        "preregistration_file": preregistration.get("file"),
        "preregistration_sha256": preregistration.get("sha256"),
    }
    missing.extend(preregistration_problems)

    if label_source != "human-annotation":
        missing.append(f"标签来源未申明为独立人工判定（当前 {label_source}）")
    if label_source == "synthetic-test-fixture":
        missing.append("合成测试标签明确不可作为验收证据")

    # 独立预登记：正式候选只用 NR 集；禁用指纹对任何自供清单都生效。
    candidate = preregistration.get("candidate") or {}
    forbidden = set(preregistration.get("forbidden") or []) \
        | set(FORBIDDEN_SAMPLE_FINGERPRINTS)
    if samples_sha in forbidden:
        missing.append(
            "样本 SHA256 命中独立预登记摘要的禁用指纹：该开发集答案已公开，"
            "自供登记清单不能把它转为正式盲评集")
    if not fixture_mode:
        expected_sha = BUILTIN_PREREGISTRATION["samples_sha256"]
        if samples_sha != expected_sha:
            missing.append(
                "实际样本 SHA256 与独立预登记的正式候选 NR-2026-11-09 不一致；"
                "正式评测不接受自供清单指定的其他样本集")
        if human_symbol_rows != DENOMINATOR:
            missing.append(
                "人工判定表须带 symbol 列并逐行与样本一致："
                f"应有 {DENOMINATOR} 行，实际 {human_symbol_rows} 行")

    registration: dict = {}
    if registration_path is None or not registration_path.exists():
        missing.append("缺 --registration 封存记录（须在人工标注前生成并提交）")
    else:
        try:
            registration_raw = registration_path.read_bytes()
            registration = json.loads(registration_raw.decode('utf-8-sig'))
            if not isinstance(registration, dict):
                raise ValueError('封存记录须为 JSON 对象')
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            missing.append(f"封存记录不可解析：{exc}")
            registration = {}
        if registration:
            registration_sha = sha256_bytes(registration_raw)
            details['registration_sha256'] = registration_sha
            details['historical_annotation_code_identity'] = registration.get('code_identity')
            if registration.get("schema") not in (REGISTRATION_SCHEMA, REGISTRATION_V2):
                missing.append("封存记录 schema 不符")
            if registration.get("samples_sha256") != samples_sha:
                missing.append("封存记录里的样本 SHA256 与实际样本不一致")
            if registration.get("human_labels_sha256") != human_sha:
                missing.append("封存记录里的人工表 SHA256 与实际人工表不一致")
            annotator = registration.get("annotator")
            if not isinstance(annotator, str) or not annotator.strip():
                missing.append("封存记录缺独立复核者 annotator")
            else:
                details["annotator"] = annotator.strip()
            if fixture_mode and correction_path is None:
                if registration.get('independent_review') is not True:
                    missing.append('封存记录未声明 independent_review=true')
            else:
                problems, declaration = independence_evidence(
                    registration, registration_sha=registration_sha, human_sha=human_sha,
                    correction_path=correction_path)
                missing.extend(problems)
                if correction_path is not None:
                    declaration['correction_path'] = portable(correction_path)
                details['independence_evidence'] = declaration
            if not fixture_mode:
                problems, policy = human_policy_evidence(
                    registration, policy_path=annotation_policy_path,
                    manifest_path=annotation_manifest_path)
                missing.extend(problems)
                details['human_judgment_policy'] = policy
            sealed_at = registration.get("sealed_at")
            try:
                sealed = datetime.fromisoformat(str(sealed_at))
                # 审计 P2：无时区的未来时间（如 2099-01-01T00:00:00）旧版会
                # 直接跳过「晚于当前时间」检查而拿到 formal=true。
                # 必须要求显式时区，再按该时区与当前时刻比较。
                if sealed.tzinfo is None or sealed.utcoffset() is None:
                    missing.append("封存记录 sealed_at 须带显式时区（ISO 8601，例如 +08:00）")
                elif sealed > datetime.now(sealed.tzinfo):
                    missing.append("封存时间晚于当前时间")
                details["sealed_at"] = str(sealed_at)
            except (TypeError, ValueError):
                missing.append("封存记录缺可解析的 sealed_at（带时区的 ISO 8601）")
            reg_profile = registration.get("profile")
            if not reg_profile:
                missing.append("封存记录未写定规则 profile")
            elif expected_profile and reg_profile != expected_profile:
                missing.append("封存记录的 profile 与本次 --profile 不一致")
            if system_header.get("profile") and reg_profile \
                    and system_header["profile"] != reg_profile:
                missing.append("系统输出 profile 与封存记录不一致")
            details["profile"] = reg_profile
            details["system_profile"] = system_header.get("profile")

    entries, registry_problems = read_registry(registry_path)
    missing.extend(registry_problems)
    set_id = registration.get("sample_set_id")
    matched = [e for e in entries if e.get("sample_set_id") == set_id]
    if not set_id:
        missing.append("封存记录缺 sample_set_id（运行前选定的样本集编号）")
    elif not matched:
        missing.append(f"sample_set_id {set_id!r} 未在样本集清单里登记")
    else:
        entry = matched[0]
        if entry.get("sha256") != samples_sha:
            missing.append("登记清单里的样本 SHA256 与实际样本不一致")
        if not fixture_mode:
            if set_id != BUILTIN_PREREGISTRATION["sample_set_id"]:
                missing.append(
                    "正式候选样本集只能是独立预登记的 NR-2026-11-09；"
                    "自供登记清单指定的其他集合不被接受")
            if entry.get("sha256") != BUILTIN_PREREGISTRATION["samples_sha256"]:
                missing.append(
                    "自供登记清单里的样本 SHA256 与独立预登记摘要不一致")
            if entry.get("entities_sha256") not in (
                    None, BUILTIN_PREREGISTRATION["entities_sha256"]):
                missing.append(
                    "自供登记清单里的主体映射 SHA256 与独立预登记摘要不一致")
        if entry.get("file") and Path(entry["file"]).name != samples_path.name:
            missing.append("登记清单里的样本文件名与实际不一致")
        if entry.get("not_recommended_for_blind_acceptance"):
            missing.append(
                f"样本集 {set_id} 已用于规则开发 / 标签可从公开材料还原，"
                "登记清单明确禁止用它作正式盲评")
        # 登记清单若声明了主体映射（冻结样本不含 entities、由 --entities 提供），
        # 系统输出头必须带同一份文件的哈希，避免悄悄换主体影响判定。
        # v1 用内置主体表、不需要外部映射：登记清单为 v2 预留的哈希不能阻塞 v1。
        v2_profile = str(expected_profile or "").startswith("v2") or \
            str(system_header.get("profile") or "").startswith("v2")
        expected_entities_sha = entry.get("entities_sha256") if v2_profile else None
        if expected_entities_sha:
            actual_entities_sha = system_header.get("entities_sha256")
            if actual_entities_sha != expected_entities_sha:
                missing.append(
                    f"主体映射 SHA256 与登记清单不一致：{actual_entities_sha!r}")
            details["entities_sha256"] = actual_entities_sha
        reg_entities_sha = registration.get("entities_sha256")
        if reg_entities_sha and reg_entities_sha != system_header.get("entities_sha256"):
            missing.append("封存记录里的主体映射 SHA256 与系统输出不一致")
        details["entities_profile_gate"] = "v2" if v2_profile else "not-required"
        details["sample_set_id"] = set_id
        details["sample_set_registered"] = True
    if not matched:
        details["sample_set_registered"] = False

    if not expected_profile:
        missing.append("未用 --profile 写定本次交付的研判版本")
    if not fixture_mode:
        missing.extend(binding_problems(registration, system_header, samples_sha=samples_sha,
                                        candidate=BUILTIN_PREREGISTRATION))
    if not lock_verified:
        missing.append("样本 SHA256 锁未通过")
    if structural_errors:
        missing.append(f"存在 {len(structural_errors)} 项结构错误")
    # 「是否达到 43/50」不是证据链问题：真实 42/50 也必须是 formal=true 的
    # 有效正式评测，只是 meets_85_percent=false / status=formal-not-met。
    details["consistent_count_at_validation"] = consistent
    details["threshold_checked_separately"] = True
    details["registration_path"] = portable(registration_path) if registration_path else None
    details["evidence_paths"] = {
        "samples": portable(samples_path),
        "human_labels": portable(human_path),
        "system_output": portable(system_path),
    }
    try:
        system_raw = system_path.read_bytes()
        reread = json.loads(system_raw.decode('utf-8-sig'))
        # The recorded file hash must describe the document actually scored.
        if isinstance(reread, dict) and reread != system_header:
            missing.append('预测输出在读取与摘要核对之间发生变化')
        details['prediction_evidence'] = {
            'system_output_sha256': sha256_bytes(system_raw),
            'payload_sha256': system_header.get('payload_sha256'),
            'code_identity': system_header.get('code_identity'),
            'run_id': system_header.get('run_id'),
            'generated_at': system_header.get('generated_at'),
            'judgment_policy': system_header.get('judgment_policy'),
        }
    except (OSError, ValueError):
        missing.append('预测输出文件无法重读核对摘要')
    return not missing, missing, details


def score(samples: list[dict], labels: dict[str, str],
          outputs: dict[str, dict], structural_errors: list[str], *,
          label_source: str, lock_verified: bool,
          samples_path: Path, human_path: Path, system_path: Path,
          samples_sha: str, human_sha: str, registration_path: Path | None,
          registry_path: Path, expected_profile: str | None,
          system_header: dict, preregistration: dict,
          preregistration_problems: list[str], fixture_mode: bool,
          human_symbol_rows: int, correction_path: Path | None = None,
          annotation_policy_path: Path | None = None,
          annotation_manifest_path: Path | None = None) -> dict:
    ids = [item["sample_id"] for item in samples if isinstance(item, dict)]
    consistent = 0
    false_positives: list[str] = []
    false_negatives: list[str] = []
    differences: list[dict] = []
    for sid in ids:
        human = labels.get(sid)
        system_row = outputs.get(sid)
        system = system_row.get("risk_verdict") if system_row else None
        if human is None or system not in VALID_VERDICTS:
            differences.append({
                "sample_id": sid, "human_verdict": human,
                "system_verdict": system, "kind": "unscorable",
            })
            continue
        if human == system:
            consistent += 1
        elif human == "no_risk" and system == "risk":
            false_positives.append(sid)
            differences.append({"sample_id": sid, "human_verdict": human,
                                "system_verdict": system, "kind": "false_positive"})
        else:
            false_negatives.append(sid)
            differences.append({"sample_id": sid, "human_verdict": human,
                                "system_verdict": system, "kind": "false_negative"})

    formal, missing, provenance = verify_provenance(
        label_source=label_source, registration_path=registration_path,
        registry_path=registry_path, samples_path=samples_path,
        human_path=human_path, system_path=system_path, samples_sha=samples_sha,
        human_sha=human_sha, lock_verified=lock_verified,
        expected_profile=expected_profile, system_header=system_header,
        structural_errors=structural_errors, consistent=consistent,
        preregistration=preregistration,
        preregistration_problems=preregistration_problems,
        fixture_mode=fixture_mode,
        human_symbol_rows=human_symbol_rows, correction_path=correction_path,
        annotation_policy_path=annotation_policy_path,
        annotation_manifest_path=annotation_manifest_path)
    if len(ids) != DENOMINATOR:
        formal = False
        missing.append(f"样本数不是 {DENOMINATOR}")

    # 两个独立结论：formal = 正式评测是否有效；meets_85_percent = 是否达标。
    meets_85_percent = consistent >= THRESHOLD and len(ids) == DENOMINATOR
    machine_chain_valid = formal
    engineering_would_pass = formal and meets_85_percent
    if fixture_mode:
        formal = False
        missing.append('合成夹具不能成为正式人工验收证据')
    passed = formal and meets_85_percent
    if passed:
        status = "pass"
    elif formal and not meets_85_percent:
        status = "formal-not-met"
    elif structural_errors:
        status = "fail"
    else:
        status = "unverified-source"
    scope = "formal-evaluation" if formal else "engineering-selftest"
    return {
        "schema": "topic16-news-risk-consistency-v5",
        "scorer_sha256": sha256_bytes(Path(__file__).read_bytes().replace(b'\r\n', b'\n')),
        "scope": scope,
        "status": status,
        "formal": formal,
        "machine_chain_valid": machine_chain_valid,
        "engineering_would_pass": engineering_would_pass if fixture_mode else None,
        "formal_evaluation_valid": formal,
        "meets_85_percent": meets_85_percent,
        "pass": passed,
        "synthetic_fixture": fixture_mode,
        "not_acceptance_evidence": True if fixture_mode else not formal,
        "denominator": DENOMINATOR,
        "threshold": THRESHOLD,
        "consistent_count": consistent,
        "consistency_rate": round(consistent / DENOMINATOR, 4),
        "false_positive_count": len(false_positives),
        "false_negative_count": len(false_negatives),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "differences": differences,
        "structural_errors": structural_errors,
        "human_labels_source": label_source,
        "lock_verified": lock_verified,
        "formal_requirements_missing": missing,
        "preregistration": {
            "file": preregistration.get("file"),
            "sha256": preregistration.get("sha256"),
            "formal_candidate": preregistration.get("candidate"),
            "forbidden_fingerprints": preregistration.get("forbidden"),
        },
        "formal_annotation_requirements": {
            "human_symbol_rows": human_symbol_rows,
            "required_rows": DENOMINATOR,
        },
        "provenance": provenance,
        "evidence": {
            "samples": portable(samples_path),
            "human_labels": portable(human_path),
            "system_output": portable(system_path),
        },
        "note": "formal 只表示正式评测的证据链有效；meets_85_percent 才表示达到 43/50；"
                "真实 42/50 会记为 formal=true / meets_85_percent=false / "
                "status=formal-not-met，而不是「无效」。一致率只比 risk/no_risk 二分类；"
                "等级、证据与复核动作仅作可追溯字段，不参与计分。",
        "fixture_warning": (
            "本次显式使用合成夹具：人工表为合成数据，不是独立人工判定，"
            "报告标 not_acceptance_evidence=true 且 pass 恒为 false，不得当作验收证据。"
            if fixture_mode else None),
        "process_note": "脚本只能核对记录是否齐全；标签究竟由谁独立作出，须靠真实复核过程、"
                        "标注前提交的封存记录与独立预登记摘要共同佐证。",
        "disclaimer": DISCLAIMER,
    }


def pending(status: str, problems: list[str], *, human_path: Path,
            system_path: Path) -> dict:
    return {
        "schema": "topic16-news-risk-consistency-v2",
        "scope": "engineering-progress",
        "status": status,
        "formal": False,
        "formal_evaluation_valid": False,
        "meets_85_percent": False,
        "pass": False,
        "synthetic_fixture": False,
        "not_acceptance_evidence": True,
        "denominator": DENOMINATOR,
        "threshold": THRESHOLD,
        "note": "人工标注未完成或系统输出未生成：只报告工程进展，不产出正式通过报告。"
                "人工表已存在但缺条目/有非法值时，返回码 1 并在 structural_errors 列出，"
                "不会报 pending-human-annotation。",
        "problems": problems,
        "expected_human_labels": portable(human_path),
        "expected_system_output": portable(system_path),
        "disclaimer": DISCLAIMER,
    }


STALE_SCHEMA = "topic16-formal-report-invalidated-v1"


def invalidate_stale_report(report: Path, *, status: str,
                            problems: list[str]) -> bool:
    """失败运行不写新报告，但同路径若留着上一次的正式结论，就地标记失效。

    只在旧报告确实存在时覆写；旧内容整体挪进 stale_report，顶层把
    formal / meets_85_percent / pass / consistent_count 等一律置为不可用，
    避免「本次评分异常、磁盘上却仍是一份 pass=true 的旧报告」。
    """
    if not report.is_file():
        return False
    try:
        stale = json.loads(report.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        stale = {"unparseable_previous_report": True}
    if isinstance(stale, dict) and stale.get("schema") == STALE_SCHEMA:
        stale = stale.get("stale_report", {})  # 幂等，不层层嵌套
    payload = {
        "schema": STALE_SCHEMA,
        "scope": "engineering-progress",
        "status": "stale-report-invalidated",
        "report_valid": False,
        "formal": False,
        "formal_evaluation_valid": False,
        "meets_85_percent": False,
        "pass": False,
        "synthetic_fixture": False,
        "not_acceptance_evidence": True,
        "invalidated_by_status": status,
        "invalidation_problems": [str(p) for p in problems][:10],
        "stale_report": stale,
        "note": "上一次失败运行已使同路径的旧正式报告失效；旧结论仅保留在 "
                "stale_report 供审计，不得作为本次验收结果引用。",
        "disclaimer": DISCLAIMER,
    }
    report.write_bytes(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return True


def emit(payload: dict, report: Path | None) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(raw)
    sys.stdout.buffer.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="独立计算舆情研判与人工判定一致率。")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--human", type=Path, default=DEFAULT_HUMAN,
                        help="人工判定 CSV/JSON（填写后的盲标表，默认路径留本机）")
    parser.add_argument("--system", type=Path, default=DEFAULT_SYSTEM)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--label-source",
                        choices=("unverified", "human-annotation",
                                 "synthetic-test-fixture"),
                        default="unverified",
                        help="默认 unverified：只算一致数、不授予正式结论。"
                             "正式验收须显式 human-annotation 并提供 --registration")
    parser.add_argument("--registration", type=Path, default=None,
                        help="人工标注封存记录 JSON（须在标注前生成并提交）")
    parser.add_argument('--review-correction', type=Path, default=None,
                        help='可选：关联原封存记录与标签哈希的独立性更正说明；不修改原记录')
    parser.add_argument('--annotation-policy', type=Path, default=None,
                        help='旧登记迁移：实际交给标注人的原版标注说明，须与原材料清单配对')
    parser.add_argument('--annotation-manifest', type=Path, default=None,
                        help='旧登记迁移：标注开始前准备的原材料清单，含说明文件摘要与预登记提交')
    parser.add_argument("--sample-registry", type=Path, default=DEFAULT_REGISTRY,
                        help="运行前登记的冻结样本集清单")
    parser.add_argument("--profile", default=None,
                        help="本次交付写定的研判版本（v1 / v2 / v2-baseline）")
    parser.add_argument("--fixture-mode", choices=("synthetic-fixture",),
                        default=None,
                        help="显式声明本次使用合成夹具（仅工程测试）：跳过真实样本指纹校验，"
                             "报告强制标 synthetic_fixture=true / not_acceptance_evidence=true，"
                             "pass 恒为 false。正式验收绝不能带此参数。")
    args = parser.parse_args()
    fixture_mode = args.fixture_mode == "synthetic-fixture"
    preregistration, preregistration_problems = load_preregistration()

    def fail(status: str, problems: list[str], code: int) -> int:
        """未就绪/输入非法：只输出工程进展，并让同路径的旧正式报告失效。"""
        emit(pending(status, problems, human_path=args.human,
                     system_path=args.system), None)
        invalidate_stale_report(args.report, status=status, problems=problems)
        return code

    try:
        samples, sample_errors, lock_verified, samples_sha = load_samples(args.samples)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid-samples", [str(exc)], 3)
    if sample_errors:
        return fail("invalid-samples", sample_errors, 3)

    if not args.human.exists():
        return fail("pending-human-annotation", [
            "人工判定文件不存在；请填写空白盲标表并另存为 human_labels.csv",
        ], 2)
    try:
        labels, human_symbols, human_errors = load_human(args.human)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid-human-labels", [str(exc)], 3)

    # 人工侧漏项 / 重复 / 非法值 / 额外 ID 都算结构错误；不因「未完成」提前退出。
    ids = {item["sample_id"] for item in samples}
    if len(labels) != DENOMINATOR or set(labels) != ids:
        missing_ids = sorted(ids - set(labels))
        extra = sorted(set(labels) - ids)
        human_errors.append(
            f"人工判定须覆盖全部 {DENOMINATOR} 个 ID，缺 {len(missing_ids)} 条：{missing_ids[:8]}")
        if extra:
            human_errors.append(f"人工判定含评测集以外的 ID：{extra[:8]}")
    # 审计 P1-19：人工表提供了 symbol 就逐行核对，不允许整列写错还出正式结论。
    expected_symbols = {item["sample_id"]: item["symbol"] for item in samples}
    for sid, symbol in sorted(human_symbols.items()):
        if sid in expected_symbols and symbol != expected_symbols[sid]:
            human_errors.append(
                f"{sid} ## 人工表标的与样本不一致：{symbol!r} != {expected_symbols[sid]!r}")

    if human_errors and not args.system.exists():
        return fail('invalid-human-labels', human_errors, 1)
    if not args.system.exists():
        return fail("missing-system-output", [
            "系统输出尚未生成；人工标注封存后再运行 python -m news_risk",
        ] + human_errors, 2)
    strict = True  # Structural validity does not depend on a claimed label source.
    try:
        outputs, system_header, system_errors = load_system(
            args.system, samples, strict=strict, expected_profile=args.profile)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid-system-output", [str(exc)] + human_errors, 3)

    missing_outputs = sorted(ids - set(outputs))
    if not fixture_mode:
        identity = system_header.get('code_identity')
        if not isinstance(identity, dict):
            identity = {}
        for sid, row in outputs.items():
            if (row.get('code_sha256') != identity.get('sha256')
                    or not row.get('code_sha256') or row.get('run_id') != system_header.get('run_id')
                    or row.get('sample_set_id') != system_header.get('sample_set_id')):
                system_errors.append(sid+' ## 逐行代码/run/集合身份与输出头不一致')
    if missing_outputs:
        system_errors.append(
            f"系统输出缺 {len(missing_outputs)} 条：{missing_outputs[:8]}")
    if len(outputs) != DENOMINATOR:
        system_errors.append(f"系统输出须为 {DENOMINATOR} 条，实际 {len(outputs)} 条")

    try:
        human_sha = sha256_bytes(args.human.read_bytes())
    except OSError as exc:
        return fail("invalid-human-labels", [str(exc)], 3)

    structural_errors = sample_errors + human_errors + system_errors
    report = score(samples, labels, outputs, structural_errors,
                   label_source=args.label_source, lock_verified=lock_verified,
                   samples_path=args.samples, human_path=args.human,
                   system_path=args.system, samples_sha=samples_sha,
                   human_sha=human_sha, registration_path=args.registration,
                   registry_path=args.sample_registry,
                   expected_profile=args.profile, system_header=system_header,
                   preregistration=preregistration,
                   preregistration_problems=preregistration_problems,
                   fixture_mode=fixture_mode,
                   human_symbol_rows=len(human_symbols), correction_path=args.review_correction,
                   annotation_policy_path=args.annotation_policy,
                   annotation_manifest_path=args.annotation_manifest)
    if report['structural_errors'] and args.report.exists():
        invalidate_stale_report(args.report, status=report['status'], problems=report['structural_errors'])
        invalidated=json.loads(args.report.read_text(encoding='utf-8'))
        invalidated['current_attempt']=report
        args.report.write_text(json.dumps(invalidated,ensure_ascii=False,indent=2),encoding='utf-8')
        emit(report,None)
    else:
        emit(report, args.report)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
