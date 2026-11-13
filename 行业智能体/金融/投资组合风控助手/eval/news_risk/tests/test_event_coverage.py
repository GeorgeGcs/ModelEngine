"""Independent synthetic event cases; no formal sample text or human labels."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from news_risk.rules_v2 import assess_news_v2

PROFILES = {
    'SYN-X': {'canonical': '榆川机电股份有限公司', 'aliases': ['榆川机电']},
    'SYN-Y': {'canonical': '石湾材料有限公司', 'aliases': ['石湾材料']},
}


def assess(text, symbol='SYN-X', **kwargs):
    return assess_news_v2(dict(symbol=symbol, text=text, **kwargs), PROFILES)


class EventCoverageTests(unittest.TestCase):
    def check(self, text, verdict, level=None):
        result = assess(text)
        self.assertEqual(result['risk_verdict'], verdict, result)
        if level:
            self.assertEqual(result['level'], level, result)
        self.assertIn(result['evidence'].strip('…'), text)
        return result

    def test_investigation_not_cancelled_by_business_as_usual(self):
        self.check('榆川机电公告已收到执法部门立案通知，日常业务照常开展。', 'risk', '高')

    def test_exact_legal_name_outranks_contained_short_alias(self):
        self.check('榆川机电股份有限公司公告，已收到立案通知。', 'risk', '高')
        self.check('榆川机电股份有限公司启动产品召回。', 'risk', '中')

    def test_shared_alias_is_ambiguous_but_explicit_name_is_unique(self):
        profiles = {
            'A': {'canonical': '青杉设备有限公司', 'aliases': ['青杉']},
            'B': {'canonical': '青杉材料有限公司', 'aliases': ['青杉']},
        }
        for text, expected in (('青杉启动产品召回。', 'no_risk'),
                               ('青杉设备有限公司启动产品召回。', 'risk'),
                               ('青杉材料有限公司启动产品召回。', 'no_risk')):
            with self.subTest(text=text):
                self.assertEqual(assess_news_v2({'symbol': 'A', 'text': text}, profiles)['risk_verdict'], expected)

    def test_debt_nonpayment_is_not_negation(self):
        self.check('榆川机电未能按约兑付到期本金。', 'risk', '高')

    def test_custody(self):
        self.check('榆川机电总经理被依法采取强制措施。', 'risk', '高')

    def test_delisting(self):
        self.check('榆川机电股票被实施退市风险警示。', 'risk', '高')

    def test_safety(self):
        self.check('榆川机电发生严重生产事故。', 'risk', '高')

    def test_recall(self):
        self.check('榆川机电确认召回存在缺陷的设备。', 'risk', '中')

    def test_litigation(self):
        self.check('榆川机电遭遇重大仲裁索赔。', 'risk', '中')

    def test_rating(self):
        self.check('榆川机电信用评级由AA调降为BBB。', 'risk', '中')

    def test_production(self):
        self.check('榆川机电主要生产线因检修停工。', 'risk', '中')

    def test_customer(self):
        for text in ('榆川机电主要客户终止合作。', '榆川机电用户集中投诉已经核实。'):
            with self.subTest(text=text):
                self.check(text, 'risk', '低')

    def test_penalty_regulator_as_subject(self):
        self.check('执法部门对榆川机电处以行政处罚。', 'risk', '中')

    def test_negative_penalty_action(self):
        for prefix in ('未', '不予', '拟'):
            with self.subTest(prefix=prefix):
                self.check('执法部门对榆川机电' + prefix + '处以行政处罚。', 'no_risk')

    def test_explicit_negations(self):
        for suffix in ('未收到立案通知。', '未被采取强制措施的高管均正常履职。',
                       '高管未被采取强制措施。', '并未召回产品。', '主要工厂没有停产。',
                       '否认股票被实施退市风险警示。'):
            with self.subTest(suffix=suffix):
                self.check('榆川机电' + suffix, 'no_risk')

    def test_official_clarification(self):
        self.check('榆川机电召回传闻经官方澄清为不实信息。', 'no_risk')

    def test_only_rumor(self):
        for prefix in ('网传', '据知情人士称', '未经证实的消息称'):
            with self.subTest(prefix=prefix):
                result = self.check(prefix + '榆川机电可能涉及监管调查。', 'no_risk', '待复核')
                self.assertEqual(result['evidence_status'], 'insufficient')

    def test_rumor_without_confirmation_remains_uncertain(self):
        self.check('网传榆川机电产品召回，尚未获得官方确认。', 'no_risk', '待复核')

    def test_official_new_confirmation_after_rumor(self):
        self.check('网传榆川机电产品召回；最新公告确认该公司开始召回缺陷产品。', 'risk', '中')

    def test_old_news_no_progress(self):
        for text in ('榆川机电去年产品召回，当前无新增进展。',
                     '回顾榆川机电产品召回旧闻，重新转载并未补充新进展。'):
            with self.subTest(text=text):
                self.check(text, 'no_risk')

    def test_old_news_explicit_new_progress(self):
        self.check('榆川机电去年产品召回；最新公告确认再次召回另一批次。', 'risk', '中')

    def test_near_name_separate_legal_entity(self):
        for name in ('榆川机电（北城）有限公司', '榆川机电科技有限公司'):
            with self.subTest(name=name):
                self.check(name + '确认召回一批设备。', 'no_risk')

    def test_other_monitored_entity(self):
        self.check('石湾材料发生重大生产事故。', 'no_risk')
        self.assertEqual(assess('石湾材料发生重大生产事故。', 'SYN-Y')['risk_verdict'], 'risk')

    def test_reporting_another_entity_is_not_ownership(self):
        for name in ('石湾材料', '岚谷设备有限公司'):
            with self.subTest(name=name):
                self.check('榆川机电公告供应商' + name + '召回产品。', 'no_risk')

    def test_explicit_other_subject_resets_context(self):
        self.check('榆川机电日常业务正常；岚谷设备有限公司召回产品。', 'no_risk')

    def test_missing_subject_is_not_assumed(self):
        self.check('近期行业有企业被立案调查。', 'no_risk')

    def test_partial_denial_with_distinct_confirmed_event(self):
        self.check('榆川机电否认产品召回，但公告确认信用评级下调。', 'risk')

    def test_partial_denial_with_normal_operations(self):
        self.check('榆川机电否认产品召回，同时确认生产正常。', 'no_risk')

    def test_partial_denial_does_not_borrow_other_entity_confirmation(self):
        self.check('榆川机电否认产品召回，同时确认石湾材料召回产品。', 'no_risk')

    def test_history_and_rumor_in_prefix_clause(self):
        for text in ('去年，榆川机电召回过产品。', '据未经证实的传闻，榆川机电召回产品。'):
            with self.subTest(text=text):
                self.check(text, 'no_risk')

    def test_normal_operations_do_not_cancel_confirmed_investigation(self):
        self.check('榆川机电已收到立案通知，否认停产，同时确认生产正常。', 'risk', '高')

    def test_contradictory_same_event_requires_review(self):
        result = self.check('榆川机电公告确认召回产品；榆川机电否认召回产品。', 'no_risk', '待复核')
        self.assertEqual(result['evidence_status'], 'insufficient')

    def test_audit_full_opinion_not_substring(self):
        for opinion in ('标准无保留意见', '非标准无保留意见', '带强调事项段的无保留意见'):
            with self.subTest(opinion=opinion):
                self.check('榆川机电年报获出具' + opinion + '。', 'no_risk')

    def test_audit_explicit_adverse_opinions(self):
        for opinion in ('保留意见', '否定意见', '无法表示意见'):
            with self.subTest(opinion=opinion):
                self.check('榆川机电年报获出具' + opinion + '。', 'risk', '中')

    def test_unqualified_opinion_does_not_cancel_other_event(self):
        self.check('榆川机电年报获出具无保留意见，同时收到监管立案通知。', 'risk', '高')

    def test_unqualified_opinion_with_explicit_adverse_basis(self):
        self.check('榆川机电获出具无保留意见，附注指出持续经营存在重大不确定性。', 'risk', '中')

    def test_id_and_entity_name_are_not_lookup_keys(self):
        text = '榆川机电已收到监管立案通知。'
        first = assess(text, sample_id='independent-a')
        second = assess(text, sample_id='independent-b')
        first.pop('sample_id'); second.pop('sample_id')
        self.assertEqual(first, second)
        swapped = assess_news_v2({'symbol': 'NEW', 'text': text.replace('榆川机电', '青杉制冷')},
                                {'NEW': {'canonical': '青杉制冷', 'aliases': []}})
        self.assertEqual(swapped['risk_verdict'], first['risk_verdict'])

    def test_dates_not_invented(self):
        result = assess('榆川机电已收到监管立案通知。')
        self.assertEqual(result['temporal_evidence'], 'missing')
        result = assess('榆川机电已收到监管立案通知。', published_at='2026-10-01T10:00:00+08:00')
        self.assertEqual(result['temporal_evidence'], 'provided_not_window_verified')


if __name__ == '__main__':
    unittest.main()
