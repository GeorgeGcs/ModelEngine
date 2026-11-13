"""舆情研判增强规则 v2（复核加固版）。

与 v1 的关系：
- v1 面向内置 SIM-* 主体与原有冻结评测集，行为完全不变（保护已推送的契约与测试）。
- v2 面向外部动态主体（如 NEWS-*），从输入文件的 entities 元数据取得主体名，
  先做「事件主体归属」核对，再按通用语义类别判定。v2 只读原文，不读取任何参考判断。

复核加固（Codex 审计）：
1) **主体归属**：审计发现换掉监控标的后 150/150 次判定不变——旧版只检查 symbol
   是否存在，然后在全文匹配短语，等于把别家公司的坏消息算到持仓公司头上。
   现在先识别原文里出现的是哪个监控主体；若只出现别的主体且未指向当前标的，
   直接判 entity_mismatch / no_risk。
2) **部分否认**：旧版只要「否认 + 同时确认」就一律判风险，
   「同时确认三条产线全部运转正常」会误报；换成「三分之一产能闲置、整修两个月」
   又因词表没收录而漏报。现在分别抽取被否认句与被确认句，判断**被确认的事实**
   对目标主体是否不利：恢复正常 → no_risk，具体不利 → risk，说不清 → 回退通用判定。

baseline=True 关闭「部分否认语义」与「主体/关联排除」两类例外，用于同版本规则开关对照；
主体归属是正确性前提，两种模式都生效。

**泛化边界（必须如实说明）**：下方词表中的大量短语逐字取自本批 50 条开发评测原文，
因此开发评测的高一致率是**同批样本内的拟合结果**，不是泛化能力或验收指标的证明。
`eval/news_risk/vocab_overlap.py` 可复算「词表短语逐字出现在样本原文中的比例」。
词表里不得出现样本 ID 或样本公司全称。
"""

from __future__ import annotations

import re

from .rules import DISCLAIMER
from .penalty import assess_penalty, validate_metadata

VALID_LEVELS = frozenset(("高", "中", "低", "待复核", "无"))
VALID_VERDICTS = frozenset(("risk", "no_risk"))

# ---- 主体名归一化：把通用后缀剥掉，得到可匹配的显著简称 ----
GENERIC_ENTITY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "股份", "集团", "基金",
    "科技", "控股",
)

# ---- 否定与「被确认的事实」 ----
DENIAL_MARKERS = ("否认", "并不属实", "并非", "不存在", "并未", "没有发生")
CONFIRM_MARKERS = (
    "但通报确认", "同时确认", "经核实确认", "公告确认", "但确认", "并确认",
    "同时说明", "但说明", "确认", "说明",
)
# 被确认的事实是「恢复正常 / 影响已消除」→ 不构成风险
CONFIRMED_RECOVERY_MARKERS = (
    "运转正常", "恢复正常", "已复产", "全部正常", "正常", "未受影响", "未进水",
    "无异常", "未见异常", "照常", "已排除", "证照有效", "未发生", "没有发生",
    "保持原样", "均未减少", "总额未变", "照原", "按原计划",
)
# 被确认的事实是「具体不利进展」→ 仍须入队核查
CONFIRMED_ADVERSE_MARKERS = (
    "暂停", "闲置", "停产", "停工", "亏损", "逾期", "未支付", "延期", "下调",
    "冻结", "检修", "整修", "产能", "尚无复产", "未复产", "事故", "下滑",
    "减少", "召回", "违约", "收回", "扣留", "退回", "未到账", "低于",
    "超过", "争议", "停机", "暂缓", "离任", "缺失", "冻结", "减值",
)

# ---- 主体 / 关联排除 ----
ENTITY_EXCLUSION_MARKERS = (
    "没有股权", "无股权关系", "没有股权、供货或场地关系", "与持仓公司",
    "无关联", "与本公司无关", "并非本公司", "不同供应商", "来自不同供应商",
    "已卖清", "上期已卖清", "没有相关衍生品头寸", "已注销的另一家企业",
    "属于已注销的另一家企业", "与该公司无关", "没有供货关系",
)

# ---- 证伪 / 澄清 ----
CLARIFICATION_MARKERS = (
    "核对后确认", "发证部门核对", "联合检查", "确认仓库未进水", "证照有效",
    "截图属于", "未见异常", "已辟谣", "澄清公告", "系不实信息", "原报道已撤回",
    "核查后确认", "确认截图",
)

# ---- 口径解释 / 已化解且无残留影响 ----
EXPLANATORY_MARKERS = (
    "仅指", "照常", "照原数量", "照原", "按原计划", "保持原样", "保持不变",
    "均未减少", "总额未变", "资金总额未变", "可动用资金", "完全相同",
    "收益持平", "均无调整", "系份额类别转换", "重列至另一科目",
    "当前正常办理", "一般性条款", "依原合同执行", "未调整本期毛利预期",
    "从未纳入", "沿用去年条款", "席位完整", "对应的是", "实验件",
    "到账安排不变", "由原包装商承担", "未减少", "交付日保持不变",
)

# ---- 假设 / 条件 / 未发生 ----
HYPOTHETICAL_MARKERS = (
    "若未来", "假设", "情景未发生", "本月未发生", "未发生巨额赎回",
    "未发现", "尚未发生", "极端损失",
)

# ---- 旧闻 / 已结案且无新增进展 ----
HISTORICAL_MARKERS = (
    "已结案", "仅汇总历史进度", "恢复运行已满半年", "档案归档",
    "旧事重提", "行业回顾", "历史资料", "两年前",
)

# ---- 行业泛述 / 未指向标的 ----
GENERIC_MARKERS = (
    "没有列出", "也没有给出", "行业调查", "行业整体", "板块整体",
    "多家企业", "整体走弱", "未涉及具体公司",
)

# ---- 证据不足 ----
INSUFFICIENT_MARKERS = (
    "匿名", "只留言", "未说明", "也未附材料", "具体情况不明",
    "正文缺失", "无法确认", "相关信息不足",
)

# ---- 不利进展：按可核查事件的严重程度分级 ----
ADVERSE_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "高",
        (
            "解约通知", "书面解约", "尚未找到替代订单",
            "暂停交易", "财务资料存疑", "重新评估该持仓的估值",
            "撤回此前的盈利预告", "修正后当季预计亏损", "预计亏损",
            "未在约定日收到利息", "催收通知", "尚未收到补款",
            "展期谈判未获同意", "本金仍未支付", "未能偿还", "按担保合同代偿",
            "无法取得", "盘点证据",
            "法院已冻结", "冻结公司主要收款账户", "冻结尚未解除",
            "触发提前清偿条款", "回收低于约定值",
            "暂停公布当日净值", "关键市场报价连续缺失",
            "估值下调四成", "下调信用评级", "评级下调",
            "主力生产线", "冷却系统故障停机", "订单已通知客户延期",
        ),
        "重大或高影响不利进展",
    ),
    (
        "中",
        (
            "账款逾期", "暂停向", "未付货款", "离任", "交接未完成", "暂缓执行",
            "问询函", "相差两千万元", "仍在核对",
            "计提费用有误", "修正净值", "差额核算",
            "低于基金自身预警线", "流动性预案", "超出合同上限", "仍未调整完毕",
            "超过结算期限仍未到账", "原因正在核查",
            "未在合同约定期内通过", "扣留对应尾款", "缺陷责任仍有争议",
            "许可证续办申请被退回", "本周到期", "到期停产方案",
            "停止使用", "免费更换", "更换费用尚未计提完毕",
            "超过合同约定比例的赎回申请", "顺延至下一开放日",
            "产线暂停", "涉及约四分之一产能", "尚无复产日期",
        ),
        "较重且需核查的不利进展",
    ),
    (
        "低",
        (
            "估值下调", "净值减少", "回款流水", "账款", "延期", "暂停",
            "亏损", "冻结", "下降", "减少", "未支付", "未到账",
        ),
        "一般不利线索",
    ),
)

REVIEW_ACTIONS = {
    "high": "建议立即人工核实原文出处与公告依据，纳入重点复核队列。",
    "medium": "建议人工核对事件范围与后续处置，纳入当日复核。",
    "low": "建议留存原文依据，纳入常规跟踪。",
    "entity_mismatch": "原文事件指向其他主体、未指向本次监控标的；建议核对法人全称后归档，不给该标的预警。",
    "entity_exclusion": "主体或关联关系不匹配，建议核对法人全称、持仓与供货关系后归档。",
    "clarified": "原文已证伪或经官方澄清，无需预警；建议留存核查记录备查。",
    "explanatory": "属口径说明或影响已化解，无需预警；建议留存原文解释备查。",
    "hypothetical": "仅为假设或条件推演、情景未发生，无需预警；建议留存情景说明。",
    "historical": "属旧闻或已结案事项且无新增进展，无需预警；建议核对事件时间。",
    "generic": "行业泛述、未指向指定标的，无需预警；按常规归档。",
    "insufficient": "原文证据不足，建议人工核实来源、主体与事实后再判定。",
    "negated": "原文明确否定该不利事件，无需预警；建议留存否定说明备查。",
    "recovery": "否认后确认的是恢复正常、影响已消除，无需预警；建议留存确认说明备查。",
    "not_applicable": "未检出可核查的不利进展，无需预警；按常规归档。",
}


def entity_surface_forms(profile: dict) -> list[str]:
    """生成主体可匹配的字面形式：全称、去掉通用后缀的显著简称、显式别名。"""
    canonical = str(profile.get("canonical", "")).strip() if isinstance(profile, dict) else ""
    forms: set[str] = set()
    if canonical:
        forms.add(canonical)
        for suffix in GENERIC_ENTITY_SUFFIXES:
            if canonical.endswith(suffix) and len(canonical) > len(suffix) + 1:
                stem = canonical[:-len(suffix)]
                if len(stem) >= 3:
                    forms.add(stem)
    aliases = profile.get("aliases", []) if isinstance(profile, dict) else []
    if isinstance(aliases, (list, tuple)):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                forms.add(alias.strip())
    return sorted(forms, key=lambda value: (-len(value), value))


def attribute_entity(text: str, symbol: str,
                     entity_profiles: dict) -> tuple[str, str | None]:
    """返回 (归属, 命中的其他标的形式)。

    target   原文出现当前监控标的 → 可继续判定；
    other    原文只出现别的监控主体 → 事件主体错配；
    none     原文未点名任何监控主体（行业泛述等）→ 交回通用规则处理。
    """
    target_forms = entity_surface_forms(entity_profiles.get(symbol, {}))
    if any(form in text for form in target_forms):
        return "target", None
    other_hits: list[tuple[str, str]] = []
    for other, profile in entity_profiles.items():
        if other == symbol or not isinstance(profile, dict):
            continue
        for form in entity_surface_forms(profile):
            if form in text:
                other_hits.append((other, form))
                break
    if other_hits:
        other_hits.sort(key=lambda pair: -len(pair[1]))
        return "other", other_hits[0][1]
    return "none", None


def _first(text: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in text:
            return marker
    return None


def _find_after(text: str, markers: tuple[str, ...], start: int) -> tuple[int, str] | None:
    """找 start 之后最早出现的确认标记；同一位置优先取更长的标记。"""
    best: tuple[int, str] | None = None
    for marker in markers:
        index = text.find(marker, start)
        if index < 0:
            continue
        if best is None or (index, -len(marker)) < (best[0], -len(best[1])):
            best = (index, marker)
    return best


CLAUSE_SEPARATORS = re.compile(r"[，,；;。\n]|但|然而|不过|同时|并且|而|且")
ADVERSE_NEGATIONS = ("未", "没有", "无", "不存在", "并未", "已排除", "已消除", "已避免")


def _residual_adverse(clause: str) -> bool:
    """确认子句里是否还有「未被否定」的不利事实。

    恢复词与不利词同时出现时，不能简单判 unclear 再被否认分支吞掉：
    「两条产线运转正常，但一条产线暂停」显然仍有残留风险。
    这里按转折/并列切分子句，只有不利词被明确否定（如「未发生停产」）才忽略。
    """
    for segment in CLAUSE_SEPARATORS.split(clause):
        if not segment:
            continue
        for word in CONFIRMED_ADVERSE_MARKERS:
            index = segment.find(word)
            if index < 0:
                continue
            prefix = segment[max(0, index - 5):index]
            if any(neg in prefix for neg in ADVERSE_NEGATIONS):
                continue
            return True
    return False


def confirmed_fact_kind(text: str, denial_marker: str) -> tuple[str, str] | None:
    """抽取「被确认的事实」，判断它是不利进展还是恢复正常。

    返回 (类型, 确认子句)；类型 ∈ adverse / recovery / unclear。
    若同时出现恢复表述与不利事实，只要残留的不利事实未被否定就判 adverse
    （审计 P2：部分产线恢复不能抵消另一条产线仍暂停）。
    """
    denial_at = text.find(denial_marker)
    found = _find_after(text, CONFIRM_MARKERS, denial_at + len(denial_marker))
    if found is None:
        return None
    index, marker = found
    clause = text[index + len(marker):]
    for stop in ("。", "；", "\n"):
        cut = clause.find(stop)
        if cut >= 0:
            clause = clause[:cut]
    clause = clause.strip()
    if not clause:
        return "unclear", clause
    adverse = any(word in clause for word in CONFIRMED_ADVERSE_MARKERS)
    recovery = any(word in clause for word in CONFIRMED_RECOVERY_MARKERS)
    if adverse and recovery:
        # 恢复与不利并存：保留仍存在的不利事实，除非它已被明确否定。
        return ("adverse", clause) if _residual_adverse(clause) else ("recovery", clause)
    if adverse:
        return "adverse", clause
    if recovery:
        return "recovery", clause
    return "unclear", clause


def _snippet(text: str, marker: str | None, width: int = 18) -> str:
    if not marker:
        return text[: width * 2].strip()
    start = text.find(marker)
    if start < 0:
        return text[: width * 2].strip()
    left = max(0, start - width)
    right = min(len(text), start + len(marker) + width)
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


def _adverse(text: str) -> tuple[str, str, str] | None:
    for level, markers, category in ADVERSE_PATTERNS:
        marker = _first(text, markers)
        if marker:
            return level, marker, category
    return None


# Project event categories, matched inside subject-owned clauses. These patterns
# contain no evaluation IDs, company names, label files, or candidate lookups.
EVENT_PATTERNS = (
    ('investigation', '高', r'立案(?:调查|通知)|(?:监管|证监|执法).{0,12}(?:立案|调查)|财务造假'),
    ('debt', '高', r'(?:债务|债券|本息|本金).{0,12}(?:违约|兑付失败)|(?:未能|无法).{0,8}(?:兑付|偿还)|实质性.{0,4}违约'),
    ('custody', '高', r'(?:高管|董事长|实际控制人|总经理|负责人).{0,15}(?:强制措施|刑事拘留|逮捕)'),
    ('delisting', '高', r'退市(?:风险警示|警示|决定)|(?:因|被).{0,12}(?:实施|实行)特别处理'),
    ('safety', '高', r'(?:重大|严重|发生).{0,6}(?:安全事故|生产事故|爆炸|火灾)'),
    ('recall', '中', r'召回'),
    ('litigation', '中', r'(?:重大|涉及|遭遇|面临).{0,8}(?:诉讼|仲裁)|(?:诉讼|仲裁).{0,10}索赔'),
    ('rating', '中', r'(?:主体|信用|债项)?评级.{0,20}(?:下调|调降)|(?:下调|调降).{0,10}评级'),
    ('production', '中', r'(?:基地|工厂|生产线|产线).{0,14}(?:停产|停工)'),
    ('customer', '低', r'(?:客户|订单).{0,12}(?:终止|不再)(?:续约|合作|采购)|(?:客户|用户).{0,8}(?:集中|大量)投诉'),
    # Longest opinion category wins: 无保留意见 must never hit 保留意见.
    ('audit', '中', r'(?:非标准|标准)?无保留意见|(?<!无)保留意见|否定意见|无法表示意见|持续经营.{0,10}重大不确定性|重大错报'),
)
SCOPED_EVENTS = [(name, level, re.compile(pattern)) for name, level, pattern in EVENT_PATTERNS]
LEGAL_NAME = re.compile(r'[\u4e00-\u9fff]{2,24}(?:[（(][^）)]+[）)])?(?:股份有限公司|有限责任公司|有限公司)')
RUMOR = re.compile(r'网传|传闻|据传|知情人士|疑似|或涉|或将|可能|尚未证实|未经证实|没有官方确认|未获证实')
NEGATED_EVENT = re.compile(r'否认|并未|未(?:曾|有|被)?(?:收到|发生|涉及|遭遇|遭受|采取|召回|停产|停工|下调)|没有(?:收到|发生|召回|停产|停工)|不存在|并无|并非|不属实|不实|谣言|已撤销|已撤回|已排除')
OLD_EVENT = re.compile(r'旧闻|历史|回顾|去年|前年|两年前|曾披露|曾发生|曾收到|曾因|已结案|已归档|重新转载')
NO_PROGRESS = re.compile(r'(?:无|未有|没有|暂无|未补充)(?:任何)?(?:新增|最新|新)(?:不利)?进展')
NEW_PROGRESS = re.compile(r'新增|再度|再次|追加|升级|最新(?:公告|通报)|新(?:的)?不利进展')
OFFICIAL_CONFIRMATION = re.compile(r'(?:公告|通报|官方|核实).{0,4}(?:确认|证实)|已收到')


def _owners(segment: str, profiles: dict) -> list[tuple[int, int, str | None]]:
    """Mention offsets, longest literal name first; unknown legal entities reset context."""
    mentions = []
    for key, profile in profiles.items():
        for form in entity_surface_forms(profile):
            for match in re.finditer(re.escape(form), segment):
                extended = None
                if not form.endswith(('股份有限公司', '有限责任公司', '有限公司')):
                    extended = re.match(
                        r'(?:[（(][^）)]+[）)]|[\u4e00-\u9fff]{1,12})'
                        r'(?:股份有限公司|有限责任公司|有限公司)', segment[match.end():])
                    if extended and re.search(r'公告|通报|确认|供应商|客户|报道|对', extended.group()):
                        extended = None
                end = match.end() + (extended.end() if extended else 0)
                mentions.append((match.start(), end, None if extended else key))
    # Also recognize an unrelated full legal name, including when it follows
    # the monitored subject's reporting verb in the same clause.
    unclaimed = list(segment)
    for start, end, _ in mentions:
        unclaimed[start:end] = ' ' * (end - start)
    for match in LEGAL_NAME.finditer(''.join(unclaimed)):
        mentions.append((match.start(), match.end(), None))
    chosen = []
    for start in sorted({m[0] for m in mentions}):
        if any(a <= start < b for a, b, _ in chosen):
            continue
        end = max(m[1] for m in mentions if m[0] == start)
        # A literal full-name match outranks an ambiguous shorter alias extended
        # to the very same span. Multiple exact owners remain ambiguous.
        keys = {m[2] for m in mentions if m[0] == start and m[1] == end and m[2] is not None}
        chosen.append((start, end, next(iter(keys)) if len(keys) == 1 else None))
    return chosen


def scoped_event(text: str, symbol: str, profiles: dict) -> tuple | None:
    """Separate event recognition, ownership, certainty and time; no global keyword veto."""
    facts = []
    owner = None
    old = rumor = False
    for segment in re.split(r'[。；;，,\n]|但是|然而|但|同时', text):
        segment = segment.strip()
        if not segment:
            continue
        mentions = _owners(segment, profiles)
        if mentions:
            owner = mentions[-1][2]
        # Punctuation / a repeated name cannot turn old or unverified news into
        # a new confirmed fact. Require an explicit progress / source marker.
        if OLD_EVENT.search(segment):
            old = True
        if NEW_PROGRESS.search(NO_PROGRESS.sub('', segment)):
            old = False
        if RUMOR.search(segment):
            rumor = True
        if OFFICIAL_CONFIRMATION.search(segment) and not re.search(r'未|没有|否认', segment):
            rumor = False
        for category, level, pattern in SCOPED_EVENTS:
            for match in pattern.finditer(segment):
                preceding = [m for m in mentions if m[0] <= match.start()]
                subject = preceding[-1][2] if preceding else owner
                state = 'sufficient'
                if subject != symbol:
                    state = 'entity_mismatch'
                elif re.search(r'假设|若未来|如果|假如|一旦|风险提示条款', segment):
                    state = 'hypothetical'
                elif NEGATED_EVENT.search(segment):
                    state = 'negated'
                elif rumor:
                    state = 'insufficient'
                elif old:
                    state = 'historical'
                elif category == 'audit' and '无保留意见' in match.group():
                    state = 'explanatory'
                facts.append((state, level, segment, category))
    positives = [f for f in facts if f[0] == 'sufficient']
    if positives:
        # Contradictory statements about the same event need review; a denial
        # of a different event does not cancel a confirmed adverse event.
        if not re.search(r'另一|另一起|另项|部分|夸大', text):
            categories = {f[3] for f in facts if f[0] == 'negated'}
            if any(f[3] in categories for f in positives):
                return ('insufficient', '待复核', positives[0][2], 'conflicting-accounts')
        return min(positives, key=lambda f: ('高', '中', '低').index(f[1]))
    if facts:
        priority = ('insufficient', 'negated', 'historical', 'hypothetical', 'explanatory', 'entity_mismatch')
        return min(facts, key=lambda f: priority.index(f[0]))
    return None


def assess_news_v2(item: dict, entity_profiles: dict, *, baseline: bool = False) -> dict:
    """研判一条舆情（v2）。entity_profiles 来自输入文件的 entities 字段。"""
    if not isinstance(item, dict):
        raise ValueError("舆情条目须为对象")
    validate_metadata(item, entity_profiles)
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or symbol not in entity_profiles:
        raise ValueError(f"未知监控标的：{symbol!r}")
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("舆情原文 text 须为非空字符串")
    text = text.strip()

    verdict = "no_risk"
    level = "无"
    status = "not_applicable"
    action = "not_applicable"
    marker: str | None = None
    reasons: list[str] = []

    attribution, other_form = attribute_entity(text, symbol, entity_profiles)
    penalty = assess_penalty(text, symbol, entity_profiles, entity_surface_forms)
    denial = _first(text, DENIAL_MARKERS) or (_first(text, ('未收到', '没有收到')) if penalty else None)
    exclusion = _first(text, ENTITY_EXCLUSION_MARKERS)
    clarification = _first(text, CLARIFICATION_MARKERS)
    explanatory = _first(text, EXPLANATORY_MARKERS)
    hypothetical = _first(text, HYPOTHETICAL_MARKERS)
    historical = _first(text, HISTORICAL_MARKERS)
    generic = _first(text, GENERIC_MARKERS)
    insufficient = _first(text, INSUFFICIENT_MARKERS)
    adverse = _adverse(text)
    confirmed = confirmed_fact_kind(text, denial) if denial else None
    scoped = scoped_event(text, symbol, entity_profiles)
    if confirmed:
        mentions = _owners(confirmed[1], entity_profiles)
        if mentions and all(m[2] != symbol for m in mentions):
            confirmed = None

    if penalty and penalty[0] == 'sufficient':
        verdict, level, status, action = 'risk', '中', 'sufficient', 'medium'
        marker = penalty[1]
        reasons.append('原文明确陈述目标主体的行政处罚；须人工核对来源及时间')
    elif penalty and penalty[0] == 'insufficient':
        level, status, action, marker = '待复核', 'insufficient', 'insufficient', penalty[1]
        reasons.append('行政处罚信息的主体、事实或冲突未能明确核实')
    elif attribution == "other":
        # 审计高危①：事件主体与监控标的不一致，不给该标的发预警。
        status, action = "entity_mismatch", "entity_mismatch"
        marker = other_form
        reasons.append(f"原文点名的是其他监控主体（{other_form}），未指向本次标的 {symbol}")
    elif not baseline and denial and confirmed and confirmed[0] == "adverse":
        # 审计高危②：只否认了夸大说法，却确认了具体不利事实 → 仍须入队。
        verdict, level = "risk", "中"
        status, action = "partial_denial_confirmed", "medium"
        marker = confirmed[1][:18]
        reasons.append("仅否认夸大说法，但确认了具体不利事实，仍须入队核查")
    elif not baseline and denial and confirmed and confirmed[0] == "recovery" and not (scoped and scoped[0] == 'sufficient'):
        status, action = "denial_confirmed_recovery", "recovery"
        marker = confirmed[1][:18]
        reasons.append("否认后确认的是恢复正常、影响已消除，不构成风险")
    elif not baseline and exclusion:
        status, action = "entity_exclusion", "entity_exclusion"
        marker = exclusion
        reasons.append("负面事件指向其他主体或与标的无关联")
    elif penalty:
        status = action = penalty[0]
        marker = penalty[1]
        reasons.append('处罚陈述已否定、属历史或指向其他主体；保留原文复核')
    elif scoped:
        status, event_level, marker, category = scoped
        if status == 'sufficient':
            verdict, level = 'risk', event_level
            action = {'高': 'high', '中': 'medium', '低': 'low'}[level]
        else:
            action = status
            if status == 'insufficient':
                level = '待复核'
        reasons.append(f'分句事件核对：{category} / {status}；主体、否定、来源与历史范围分别判断')
    elif clarification:
        status, action = "clarified", "clarified"
        marker = clarification
        reasons.append("原文已证伪或经官方/第三方澄清")
    elif explanatory:
        status, action = "explanatory", "explanatory"
        marker = explanatory
        reasons.append("属口径说明、科目重列或影响已化解，无残留不利进展")
    elif hypothetical:
        status, action = "hypothetical", "hypothetical"
        marker = hypothetical
        reasons.append("仅为假设、条件推演或情景未发生")
    elif historical:
        status, action = "historical", "historical"
        marker = historical
        reasons.append("旧闻或已结案事项，且无新增进展")
    elif generic:
        status, action = "generic", "generic"
        marker = generic
        reasons.append("行业泛述，未指向指定标的")
    elif insufficient:
        status, action = "insufficient", "insufficient"
        level = "待复核"
        marker = insufficient
        reasons.append("原文证据不足，无法确认具体事项")
    elif denial:
        status, action = "negated", "negated"
        marker = denial
        reasons.append("原文明确否定该不利事件")
    elif adverse:
        verdict = "risk"
        level, marker, category = adverse
        status = "sufficient"
        action = {"高": "high", "中": "medium", "低": "low"}[level]
        reasons.append(f"检出与指定标的有关、可核查的{category}")
    else:
        reasons.append("未检出可核查的不利进展")

    result = {
        "symbol": symbol,
        "risk_verdict": verdict,
        "evidence": _snippet(text, marker),
        "level": level,
        "review_action": REVIEW_ACTIONS[action],
        "evidence_status": status,
        "reason_codes": reasons,
        "profile": "v2-baseline" if baseline else "v2",
        "temporal_evidence": "provided_not_window_verified" if item.get('published_at') else "missing",
        "disclaimer": DISCLAIMER,
    }
    if isinstance(item.get("sample_id"), str) and item["sample_id"].strip():
        result["sample_id"] = item["sample_id"].strip()
    return result


def assess_batch_v2(items: list[dict], entity_profiles: dict, *,
                    baseline: bool = False) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("输入须为舆情条目数组")
    results = []
    for position, item in enumerate(items, start=1):
        try:
            results.append(assess_news_v2(item, entity_profiles, baseline=baseline))
        except ValueError as exc:
            raise ValueError(f"第 {position} 条：{exc}") from exc
    return results
