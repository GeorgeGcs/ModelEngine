"""确定性舆情风险研判规则（合成数据专用）。

设计原则：
- 只用 Python 标准库，不调用模型，不联网，不读取任何评测样本的答案文件。
- 判定依据只能来自输入原文；每条输出都带原文证据片段与可解释的 reason_codes。
- 输出定位为辅助建议，不给买卖结论；证据不足时显式降级并要求人工复核。
- 与 data/eval_20.json、eval/expected_20.json、task_manifest.json 等止盈止损冻结件完全隔离。
"""

from __future__ import annotations

DISCLAIMER = "风险提示仅供参考，不构成投资建议；本结果为辅助建议，重大舆情须人工核实。"

# 虚构主体对照表：与 docs/舆情风险标注说明.md 第 2 节保持一致。
ENTITIES: dict[str, dict] = {
    "SIM-A": {
        "canonical": "云岚科技股份有限公司",
        "aliases": ("云岚科技", "云岚股份"),
    },
    "SIM-B": {
        "canonical": "北辰智造集团股份有限公司",
        "aliases": ("北辰智造", "北辰股份"),
    },
    "SIM-C": {
        "canonical": "沣泽消费服务控股有限公司",
        "aliases": ("沣泽消费", "沣泽控股"),
    },
    "SIM-D": {
        "canonical": "星渚生物医药有限公司",
        "aliases": ("星渚生物", "星渚医药"),
    },
}

# 易混淆主体：名称相近但并非监控标的，用于「主体错配」判定。
CONFUSABLES: tuple[tuple[str, str], ...] = (
    ("云岚科技（苏州）有限公司", "SIM-A"),
    ("云岚能源科技有限公司", "SIM-A"),
    ("北辰智造（成都）有限公司", "SIM-B"),
    ("北辰智控技术研究院", "SIM-B"),
    ("沣泽商贸（海口）有限公司", "SIM-C"),
    ("星渚生物科技有限公司", "SIM-D"),
)

NEGATION_MARKERS: tuple[str, ...] = (
    "并未收到", "未收到", "不存在", "并无此事", "否认", "并不属实",
    "没有发生", "并不存在", "未作出", "未发生", "不属实", "没有出现",
)

CLARIFICATION_MARKERS: tuple[str, ...] = (
    "澄清公告", "公司澄清", "已澄清", "已辟谣", "系不实信息", "系误读",
    "原报道已撤回", "原报道已更正", "不实信息", "已撤回", "已更正",
)

HISTORICAL_MARKERS: tuple[str, ...] = (
    "行业回顾", "回顾", "旧事重提", "历史资料", "2024 年", "2023 年",
    "两年前", "旧闻", "档案归档", "无新增进展", "未有最新进展", "早已结案",
)

# 旧闻例外：出现新进展时不能按旧闻免预警。
NEW_DEVELOPMENT_MARKERS: tuple[str, ...] = (
    "新增进展", "最新进展", "再度", "升级", "又有新", "新增处罚",
)

RUMOR_MARKERS: tuple[str, ...] = (
    "网传", "据知情人士", "疑似", "或涉", "或被", "未经证实", "有消息称",
    "传闻", "尚未见正式公告", "仍待核实", "暂无权威来源", "可能涉及",
    "没有官方确认",
)

INSUFFICIENT_MARKERS: tuple[str, ...] = (
    "具体情况不明", "尚无法确认", "正文缺失", "无法确认", "来源不明",
    "具体情况待核", "相关信息不足",
)

GENERIC_MARKERS: tuple[str, ...] = (
    "行业观察", "板块整体", "多家企业", "未涉及具体公司", "行业共性",
)

# 负面事实词库，按严重程度从高到低匹配；同一文本命中多类时取最高等级。
RISK_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "高",
        (
            "立案调查", "财务造假", "虚构收入", "债务违约", "未能按期兑付",
            "安全生产事故", "重大安全事故", "退市风险警示", "采取强制措施",
            "无法表示意见",
        ),
        "重大负面事件",
    ),
    (
        "中",
        (
            "行政处罚", "处罚决定书", "召回", "重大诉讼", "诉讼仲裁",
            "非标准无保留意见", "评级下调", "下调至", "停产", "暂停生产",
        ),
        "较重负面事件",
    ),
    (
        "低",
        (
            "监管问询函", "问询函", "终止续约", "关闭", "进度延后",
            "经营现金流为负", "集中投诉", "客户投诉",
        ),
        "一般负面事件",
    ),
)

REVIEW_ACTIONS: dict[str, str] = {
    "high": "建议立即人工核实原文出处与监管文件，纳入重点复核队列。",
    "medium": "建议人工核对公告原文与影响范围，纳入当日复核。",
    "low": "建议留存原文依据，纳入常规跟踪。",
    "negated": "无需预警；建议留存该否定说明备查。",
    "clarified": "无需预警；建议留存官方澄清或撤回记录备查。",
    "historical": "无需预警；建议核对事件时间与是否确有新增进展。",
    "rumor": "证据不足，建议持续跟踪官方渠道后再复核，暂不生成预警。",
    "entity_mismatch": "主体错配，建议核对法人全称或证券代码后再归档。",
    "insufficient": "原文证据不足，建议人工核实来源、主体与事实后再判定。",
    "not_applicable": "无需预警；按常规归档。",
}

VALID_LEVELS = frozenset(("高", "中", "低", "待复核", "无"))
VALID_VERDICTS = frozenset(("risk", "no_risk"))


def _first_marker(text: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in text:
            return marker
    return None


def _snippet(text: str, marker: str | None, width: int = 16) -> str:
    """截取命中词附近的原文片段，作为可核对的原文证据。"""
    if not marker:
        return text[: width * 2].strip()
    start = text.find(marker)
    if start < 0:
        return text[: width * 2].strip()
    left = max(0, start - width)
    right = min(len(text), start + len(marker) + width)
    piece = text[left:right].strip()
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{piece}{suffix}"


def _entity_state(text: str, symbol: str) -> tuple[str, str | None]:
    """返回 (matched | mismatch | unclear, 命中的易混淆主体名)。

    先把易混淆主体全名从文本中摘掉，再判断监控主体：否则
    「云岚科技（苏州）有限公司」会因为包含别名「云岚科技」而被误判为同一主体。
    """
    scrubbed = text
    confusable_hit: str | None = None
    for name, near in sorted(CONFUSABLES, key=lambda pair: -len(pair[0])):
        if name in scrubbed:
            if near == symbol and confusable_hit is None:
                confusable_hit = name
            scrubbed = scrubbed.replace(name, "〔其他主体〕")
    entity = ENTITIES[symbol]
    names = (entity["canonical"], *entity["aliases"], symbol)
    if any(name in scrubbed for name in names):
        return "matched", confusable_hit
    if confusable_hit:
        return "mismatch", confusable_hit
    return "unclear", None


def _other_monitored_form(text: str, symbol: str) -> str | None:
    """返回原文点名的「其他监控标的」字面形式，用于把负面事件归属到正确主体。

    审计 P2：v1 原先在「主体不明」时仍会先命中风险词，导致
    「北辰智造集团股份有限公司收到行政处罚决定书」在 SIM-A 上被误判为 risk。
    这里先剔除易混淆主体全称（避免把「云岚科技（苏州）有限公司」当成 SIM-A），
    再检查是否点名了别的监控标的。返回 None 表示没有可归属的其他主体。
    """
    scrubbed = text
    for name, _near in CONFUSABLES:
        scrubbed = scrubbed.replace(name, "〔其他主体〕")
    hits: list[str] = []
    for other, profile in ENTITIES.items():
        if other == symbol:
            continue
        for form in (profile["canonical"], *profile["aliases"]):
            if form in scrubbed:
                hits.append(form)
                break
    return max(hits, key=len) if hits else None


def _risk_match(text: str) -> tuple[str, str, str] | None:
    """返回 (等级, 命中词, 事实类别)，取最高严重程度。"""
    for level, markers, category in RISK_PATTERNS:
        marker = _first_marker(text, markers)
        if marker:
            return level, marker, category
    return None


def assess_news(item: dict) -> dict:
    """研判一条舆情，输出五要素 + 可追溯的判定状态。

    必填入参：symbol（监控标的代码）、text（原文）。
    可选入参：sample_id、published_at、source。
    返回结构固定，risk_verdict 只取 risk / no_risk，便于独立评分脚本按 ID 对齐。
    """
    if not isinstance(item, dict):
        raise ValueError("舆情条目须为对象")
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or symbol not in ENTITIES:
        raise ValueError(f"未知监控标的：{symbol!r}")
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("舆情原文 text 须为非空字符串")
    text = text.strip()

    entity_state, confusable = _entity_state(text, symbol)
    # 审计 P2：主体归属是正确性前提，必须早于否定/澄清/风险词匹配。
    # 只有「主体不明」且原文点名了别的监控标的时，才判主体错配；
    # 已匹配当前标的或命中易混淆主体的情形不受影响。
    other_monitored = (_other_monitored_form(text, symbol)
                       if entity_state == "unclear" else None)
    reasons: list[str] = []

    verdict = "no_risk"
    level = "无"
    evidence_status = "not_applicable"
    action_key = "not_applicable"
    evidence_marker: str | None = None

    if entity_state == "mismatch":
        evidence_status = "entity_mismatch"
        action_key = "entity_mismatch"
        evidence_marker = confusable
        reasons.append("同名主体错配")
    elif other_monitored:
        evidence_status = "entity_mismatch"
        action_key = "entity_mismatch"
        evidence_marker = other_monitored
        reasons.append(
            f"原文点名的是其他监控主体（{other_monitored}），未指向本次标的 {symbol}")
    else:
        negation = _first_marker(text, NEGATION_MARKERS)
        clarification = _first_marker(text, CLARIFICATION_MARKERS)
        historical = _first_marker(text, HISTORICAL_MARKERS)
        new_development = _first_marker(text, NEW_DEVELOPMENT_MARKERS)
        rumor = _first_marker(text, RUMOR_MARKERS)
        insufficient = _first_marker(text, INSUFFICIENT_MARKERS)
        generic = _first_marker(text, GENERIC_MARKERS)
        risk = _risk_match(text)

        if negation:
            evidence_status, action_key = "negated", "negated"
            evidence_marker = negation
            reasons.append("原文明确否定负面事件")
        elif clarification:
            evidence_status, action_key = "clarified", "clarified"
            evidence_marker = clarification
            reasons.append("官方澄清或原报道撤回")
        elif historical and not new_development:
            evidence_status, action_key = "historical", "historical"
            evidence_marker = historical
            reasons.append("旧闻重提且无新增进展")
        elif rumor:
            evidence_status, action_key = "insufficient", "rumor"
            level = "待复核"
            evidence_marker = rumor
            reasons.append("仅传闻或未经证实")
        elif insufficient:
            evidence_status, action_key = "insufficient", "insufficient"
            level = "待复核"
            evidence_marker = insufficient
            reasons.append("原文证据不足")
        elif risk:
            verdict = "risk"
            level, evidence_marker, category = risk
            evidence_status = "sufficient"
            action_key = {"高": "high", "中": "medium", "低": "low"}[level]
            reasons.append(f"主体匹配且命中{category}")
        elif entity_state == "unclear" and generic:
            evidence_status, action_key = "not_applicable", "not_applicable"
            evidence_marker = generic
            reasons.append("行业泛述，未指向监控标的")
        elif entity_state == "unclear":
            evidence_status, action_key = "insufficient", "insufficient"
            level = "待复核"
            evidence_marker = None
            reasons.append("主体无法唯一确定")
        else:
            reasons.append("未检出可核实的负面事实")

    result = {
        "symbol": symbol,
        "risk_verdict": verdict,
        "evidence": _snippet(text, evidence_marker),
        "level": level,
        "review_action": REVIEW_ACTIONS[action_key],
        "evidence_status": evidence_status,
        "reason_codes": reasons,
        "disclaimer": DISCLAIMER,
    }
    if isinstance(item.get("sample_id"), str) and item["sample_id"].strip():
        result["sample_id"] = item["sample_id"].strip()
    return result


def assess_batch(items: list[dict]) -> list[dict]:
    """批量研判；任一非法输入整批报错，不产生半份结果。"""
    if not isinstance(items, list):
        raise ValueError("输入须为舆情条目数组，或含 items 数组的对象")
    results = []
    for position, item in enumerate(items, start=1):
        try:
            results.append(assess_news(item))
        except ValueError as exc:
            raise ValueError(f"第 {position} 条：{exc}") from exc
    return results
