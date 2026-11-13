"""Machine provenance checks on synthetic metadata; never real human scoring."""
import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from eval.news_risk import score
from eval.news_risk.provenance import (binding_problems, code_identity, REGISTRATION_V2,
                                      judgment_policy, prediction_digest)
import test_acceptance_scoring as fixtures


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.candidate=score.BUILTIN_PREREGISTRATION
        self.reg={**self.candidate,'schema':REGISTRATION_V2,'code_identity':code_identity(),
                  'preregistered_commit':'a'*40,'registered_at':'2026-11-01T00:00:00Z',
                  'annotation_started_at':'2026-11-02T00:00:00Z','sealed_at':'2026-11-03T00:00:00Z'}
        self.header={**self.candidate,'code_identity':code_identity(),'run_id':'SYNTHETIC-ONLY',
                     'generated_at':'2026-11-04T00:00:00Z', 'judgment_policy':judgment_policy()}
        self.header['payload_sha256']=prediction_digest(self.header)
    def problems(self):
        return binding_problems(self.reg,self.header,samples_sha=self.candidate['samples_sha256'],
                                candidate=self.candidate,now=datetime(2026,11,5,tzinfo=timezone.utc))
    def test_consistent_metadata_and_equivalent_offsets(self):
        self.reg['sealed_at']='2026-11-03T08:00:00+08:00'
        self.assertEqual(self.problems(),[])
    def test_time_order_missing_offset_date_only_and_future(self):
        for value in ('2026-11-03','2026-11-03T00:00:00','2099-01-01T00:00:00Z','2026-11-01T01:00:00Z'):
            with self.subTest(value=value):
                self.reg['sealed_at']=value;self.assertTrue(self.problems())
    def test_code_binding_all_fields_and_profile(self):
        for key in ('code_identity','sample_set_id','samples_sha256','profile','entities_sha256'):
            original=self.header[key];self.header[key]='mismatch'
            self.assertTrue(self.problems(),key);self.header[key]=original
        self.reg['schema']='topic16-human-annotation-registration-v1';self.assertTrue(self.problems())
    def test_template_is_not_a_sealed_record(self):
        self.reg['template_only']=True;self.assertTrue(self.problems())
    def test_numeric_boundary_independent_of_valid_chain(self):
        samples=[{'sample_id':str(i)} for i in range(50)]
        labels={str(i):'no_risk' for i in range(50)}
        for n in (42,43):
            outputs={str(i):{'risk_verdict':'no_risk' if i<n else 'risk'} for i in range(50)}
            with patch.object(score,'verify_provenance',return_value=(True,[],{})):
                report=score.score(samples,labels,outputs,[],label_source='human-annotation',
                    lock_verified=True,samples_path=Path('synthetic'),human_path=Path('synthetic'),
                    system_path=Path('synthetic'),samples_sha='',human_sha='',registration_path=None,
                    registry_path=Path('synthetic'),expected_profile='v2',system_header={},
                    preregistration={},preregistration_problems=[],fixture_mode=False,human_symbol_rows=50)
            self.assertTrue(report['formal']);self.assertEqual(report['pass'],n==43)
            self.assertEqual(report['meets_85_percent'],n==43)


class PublicGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AcceptanceCase();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.full_fixture()
    def test_fixture_never_formal_or_pass(self):
        p,r=self.fixture.run_score(self.fixture.formal_args())
        self.assertEqual(p.returncode,1);self.assertFalse(r['formal']);self.assertFalse(r['pass'])
        self.assertTrue(r['engineering_would_pass']);self.assertTrue(r['not_acceptance_evidence'])
    def test_existing_invalid_human_not_pending_when_system_missing(self):
        self.fixture.human.write_text('sample_id,human_verdict,symbol\n',encoding='utf-8')
        self.fixture.system.unlink()
        p,r=self.fixture.run_score(self.fixture.formal_args())
        self.assertEqual(p.returncode,1);self.assertEqual(r['status'],'invalid-human-labels')
    def test_header_answer_rejected_and_previous_report_retained(self):
        self.fixture.report.write_text('{"pass":true,"synthetic_previous":true}',encoding='utf-8')
        raw=json.loads(self.fixture.system.read_text(encoding='utf-8'));raw['answer']='no_risk'
        self.fixture.system.write_text(json.dumps(raw),encoding='utf-8')
        p,r=self.fixture.run_score(self.fixture.formal_args())
        self.assertEqual(p.returncode,1);self.assertTrue(r['structural_errors'])
        disk=json.loads(self.fixture.report.read_text(encoding='utf-8'))
        self.assertFalse(disk['report_valid']);self.assertTrue(disk['stale_report']['synthetic_previous'])
