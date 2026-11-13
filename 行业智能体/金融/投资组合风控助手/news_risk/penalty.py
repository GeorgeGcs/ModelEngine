"""Scoped administrative-penalty statements. Project rules, not legal advice.

Only operates on supplied text and entity metadata. No evaluation IDs or labels.
Ambiguous attribution and contradictory statements require review.
"""
import re
from datetime import datetime

PENALTY = re.compile(r'行政处罚|行政罚款|处以[^。；，]{0,16}罚款|罚款[^。；，]{0,12}决定|监管处罚')
NEGATIVE = re.compile(r'未(?:曾|有|被)?收到|没有收到|未处以|并无|不存在|未受|未被|不予|免予|撤销|撤回|不实|谣言|否认')
UNCERTAIN = re.compile(r'网传|传闻|据传|疑似|或将|可能|拟(?:对|处以|给予)|未经证实|假设|如果|若(?:未来)?|风险提示|条款')
CONFIRMED = re.compile(r'已收到|收到|已被|被处以|处以行政处罚|决定给予|决定处以|确认|公告|通报')
HISTORY = re.compile(r'历史|旧闻|去年|前年|两年前|已结案|已归档|回顾|曾于|曾收到')
NEW = re.compile(r'最新|新增|再度|再次|新进展|新(?:的)?(?:处罚|罚款)|升级|追加')


def validate_metadata(item, profiles):
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError('entities 须为非空主体映射')
    for symbol, profile in profiles.items():
        if not isinstance(symbol, str) or not isinstance(profile, dict):
            raise ValueError('主体映射格式非法')
        if not isinstance(profile.get('canonical'), str) or not profile['canonical'].strip():
            raise ValueError('主体缺 canonical 全称')
        aliases = profile.get('aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise ValueError('aliases 须为非空字符串数组')
    if 'published_at' in item:
        value = item['published_at']
        if not isinstance(value, str) or 'T' not in value:
            raise ValueError('published_at 须为带时区 ISO 时间')
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('published_at 非法') from exc
        if dt.tzinfo is None:
            raise ValueError('published_at 须含时区')


def assess_penalty(text, symbol, profiles, forms):
    if not PENALTY.search(text):
        return None
    segments = [s.strip() for s in re.split(r'[。；;\n]|[，,]|但|然而|同时', text) if s.strip()]
    facts = []
    context = None
    for segment in segments:
        mentions = []
        for key, profile in profiles.items():
            for form in forms(profile):
                at = segment.find(form)
                if at >= 0:
                    # A short name contained in another legal person's full name is ambiguous.
                    suffix = segment[at+len(form):]
                    extended = re.match(r'[（(][^）)]+[）)](?:有限|股份)', suffix)
                    mentions.append((at, len(form), None if extended else key))
        if mentions:
            # Prefer the longest name at a position; don't attribute a suffix alias to its parent.
            first = min(m[0] for m in mentions)
            owners = {m[2] for m in mentions if m[0] == first and m[1] == max(x[1] for x in mentions if x[0] == first)}
            context = next(iter(owners)) if len(owners) == 1 else None
        match = PENALTY.search(segment)
        if not match:
            continue
        if context != symbol:
            facts.append(('entity_mismatch', segment)); continue
        if NEGATIVE.search(segment):
            facts.append(('negated', segment)); continue
        if UNCERTAIN.search(segment) and not re.search(r'(?:公告|通报|核实)确认|已收到', segment):
            facts.append(('insufficient', segment)); continue
        if HISTORY.search(segment) and not NEW.search(segment):
            facts.append(('historical', segment)); continue
        if re.search(r'已结案|已撤销|无新增', text) and not NEW.search(text.replace('无新增','')):
            facts.append(('historical', segment)); continue
        if CONFIRMED.search(segment) or re.search(r'受到|遭到|作出', segment):
            facts.append(('sufficient', segment))
        else:
            facts.append(('insufficient', segment))
    confirmed = [f for f in facts if f[0] == 'sufficient']
    negated = [f for f in facts if f[0] == 'negated']
    if confirmed and negated and not re.search(r'另一|另一起|另项|另案|部分|夸大', text):
        # Conflicting accounts without a clear different event cannot certify either claim.
        return 'insufficient', confirmed[0][1]
    if confirmed:
        return confirmed[0]
    for kind in ('insufficient', 'negated', 'historical', 'entity_mismatch'):
        for fact in facts:
            if fact[0] == kind:
                return fact
    return 'insufficient', text
