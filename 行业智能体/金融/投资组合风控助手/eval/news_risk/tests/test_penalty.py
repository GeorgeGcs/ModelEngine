"""Independent synthetic cases; no frozen candidates or label files."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from news_risk.rules_v2 import assess_news_v2


class PenaltyTests(unittest.TestCase):
    entities={'X':{'canonical':'林桥设备股份有限公司','aliases':['林桥设备']},
              'Y':{'canonical':'林桥设备（南方）有限公司'}}

    def check(self,text,verdict,**extra):
        result=assess_news_v2(dict(symbol='X',text=text,**extra),self.entities)
        self.assertEqual(result['risk_verdict'],verdict,text)
        self.assertIn(result['evidence'].replace('…',''),text)
        return result

    def test_original_live_counterexample_and_missing_time_boundary(self):
        r=assess_news_v2({'sample_id':'NEWS-LIVE-001','symbol':'GEN-Y',
            'text':'远川精密收到行政处罚决定书。'},{'GEN-Y':{'canonical':'远川精密'}})
        self.assertEqual((r['risk_verdict'],r['level'],r['temporal_evidence']),('risk','中','missing'))

    def test_confirmed_paraphrases_with_source_time(self):
        for text in ('林桥设备收到行政处罚决定书。','监管部门决定给予林桥设备行政罚款。',
                     '林桥设备被处以二十万元罚款。','林桥设备公告确认受到监管处罚。'):
            self.check(text,'risk',published_at='2026-11-10T10:00:00+08:00',source='项目合成公告')

    def test_negation_and_revocation(self):
        for text in ('林桥设备未收到行政处罚决定书。','林桥设备没有收到行政罚款决定。',
                     '林桥设备澄清：不存在行政处罚。','林桥设备的行政处罚决定已撤销。'):
            self.check(text,'no_risk')

    def test_entity_substitution_and_ambiguous_similar_name(self):
        for text in ('林桥设备（南方）有限公司收到行政处罚决定书。',
                     '林桥设备（北方）有限公司收到行政处罚决定书。','多家行业企业受到行政处罚。',
                     '林桥设备经营正常，林桥设备（南方）有限公司受到行政处罚。'):
            self.check(text,'no_risk')

    def test_rumor_hypothesis_and_subsequent_confirmation(self):
        for text in ('网传林桥设备收到行政处罚。','假设林桥设备受到行政处罚。',
                     '林桥设备提示条款：若未来受到行政处罚将公告。'):
            self.check(text,'no_risk')
        self.check('网传林桥设备将受处罚，但公告确认已收到行政处罚决定书。','risk')

    def test_history_and_new_progress(self):
        self.check('林桥设备去年收到行政处罚决定书，现已结案，无新增进展。','no_risk')
        self.check('林桥设备去年受到行政处罚已结案，最新公告确认再度收到行政处罚决定。','risk')

    def test_partial_denial_scope_and_normal_recovery(self):
        self.check('林桥设备否认另一项行政处罚，但确认本次已收到行政处罚决定书。','risk')
        self.check('林桥设备否认行政处罚，同时确认产线恢复正常。','no_risk')
        self.check('林桥设备未收到行政处罚，但同时确认一条产线暂停。','risk')

    def test_contradictions_require_review(self):
        r=self.check('林桥设备收到行政处罚决定书。林桥设备未收到行政处罚决定书。','no_risk')
        self.assertEqual(r['level'],'待复核')

    def test_invalid_inputs_and_time_fail(self):
        for value in ('','not-time','2026-11-10','2026-11-10T10:00:00',42):
            with self.assertRaises(ValueError):self.check('林桥设备收到行政处罚。','risk',published_at=value)
        for item,entities in (({'symbol':'X','text':''},self.entities),
                              ({'symbol':'X','text':12},self.entities),
                              ({'symbol':'X','text':'处罚'},{}),
                              ({'symbol':'X','text':'处罚'},{'X':{}})):
            with self.assertRaises(ValueError):assess_news_v2(item,entities)
