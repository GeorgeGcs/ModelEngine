"""Synthetic fixtures only. No real human labels or acceptance reports.

The two-candidate test actually executes the CLI in two temporary source trees.
Only the registered sample fingerprint is patched for these synthetic fixtures;
the full evidence checks run unchanged. Temporary results are not delivered.
"""
import contextlib
import csv
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eval.news_risk import provenance as p, score


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class LabelReuseTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.samples = self.dir / 'synthetic.json'
        self.human = self.dir / 'labels.csv'
        self.system = self.dir / 'predictions.json'
        self.registration = self.dir / 'seal.json'
        self.registry = self.dir / 'registry.json'
        self.correction = self.dir / 'correction.json'
        self.policy = self.dir / 'instructions.md'
        self.manifest = self.dir / 'package.json'
        self.items = [dict(sample_id=f'SYN-{i}', symbol='SYN', text='合成测试公司经营正常。')
                      for i in range(50)]
        write_json(self.samples, {'items': self.items})
        self.candidate = {**score.BUILTIN_PREREGISTRATION,
                          'samples_sha256': p.sha256(self.samples.read_bytes())}
        self.write_labels()
        self.reg = {**self.candidate, 'schema': p.REGISTRATION_V2,
                    'annotator': 'synthetic-reviewer', 'independent_review': True,
                    'preregistered_commit': 'a'*40,
                    'registered_at': '2020-01-01T00:00:00Z',
                    'annotation_started_at': '2020-01-02T00:00:00Z',
                    'annotation_completed_at': '2020-01-02T01:00:00Z',
                    'sealed_at': '2020-01-03T00:00:00Z',
                    'human_labels_sha256': p.sha256(self.human.read_bytes()),
                    'judgment_policy': p.judgment_policy(),
                    'code_identity': {'historical': 'different candidate before labeling'},
                    'independence_declaration': {
                        'source': 'synthetic unit-test declaration, not a human statement',
                        'statement': 'Synthetic independence field for mechanism tests only.',
                        'recorded_at': '2020-01-01T00:00:00Z'}}
        self.header = {**self.candidate, 'code_identity': p.code_identity(),
                       'run_id': 'SYNTHETIC-ONLY', 'generated_at': '2020-01-04T00:00:00Z',
                       'judgment_policy': p.judgment_policy(),
                       'items': [dict(sample_id=r['sample_id'], symbol='SYN', risk_verdict='no_risk',
                                      level='无', evidence=r['text'], review_action='合成测试', profile='v2')
                                 for r in self.items]}
        self.save_header()
        write_json(self.registry, {'schema': score.REGISTRY_SCHEMA, 'registered_sets': [{
            'sample_set_id': self.candidate['sample_set_id'], 'file': self.samples.name,
            'sha256': self.candidate['samples_sha256'], 'entities_sha256': self.candidate['entities_sha256']}]})

    def write_labels(self, consistent=50):
        with self.human.open('w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['sample_id', 'human_verdict', 'symbol'])
            writer.writerows((r['sample_id'], 'no_risk' if i < consistent else 'risk', 'SYN')
                             for i, r in enumerate(self.items))

    def save_header(self):
        identity = self.header.get('code_identity')
        for row in self.header['items']:
            row.update(code_sha256=identity.get('sha256') if isinstance(identity, dict) else None,
                       run_id=self.header['run_id'], sample_set_id=self.header['sample_set_id'])
        self.header['payload_sha256'] = p.prediction_digest(self.header)
        write_json(self.system, self.header)

    def make_correction(self):
        write_json(self.registration, self.reg)
        self.corr = {'schema': p.CORRECTION_V1,
                     'original_registration_sha256': p.sha256(self.registration.read_bytes()),
                     'human_labels_sha256': self.reg['human_labels_sha256'],
                     'annotator': self.reg['annotator'], 'sample_set_id': self.reg['sample_set_id'],
                     'profile': self.reg['profile'], 'independent_review': True,
                     'recorded_at': '2020-01-05T00:00:00Z',
                     'source': 'synthetic correction source', 'statement': 'Synthetic correction only.'}
        write_json(self.correction, self.corr)

    def legacy_package(self):
        self.reg.pop('judgment_policy', None)
        self.policy.write_bytes(p.POLICY_PATH.read_bytes())
        self.package = {'reviewer': self.reg['annotator'],
                        'preregistration_commit': self.reg['preregistered_commit'],
                        'prepared_at': '2020-01-01T01:00:00Z',
                        'files': {self.policy.name: p.sha256(self.policy.read_bytes()),
                                  'sim_entities.json': self.reg['entities_sha256']}}
        write_json(self.manifest, self.package)

    def evaluate(self, *, correction=False, legacy=False, label_source='human-annotation'):
        write_json(self.registration, self.reg)
        labels, symbols, errors = score.load_human(self.human)
        outputs, header, output_errors = score.load_system(
            self.system, self.items, strict=True, expected_profile='v2')
        with patch.object(score, 'BUILTIN_PREREGISTRATION', self.candidate):
            return score.score(self.items, labels, outputs, errors + output_errors,
                label_source=label_source, lock_verified=True, samples_path=self.samples,
                human_path=self.human, system_path=self.system,
                samples_sha=p.sha256(self.samples.read_bytes()), human_sha=p.sha256(self.human.read_bytes()),
                registration_path=self.registration, registry_path=self.registry, expected_profile='v2',
                system_header=header, preregistration={'candidate': self.candidate},
                preregistration_problems=[], fixture_mode=False, human_symbol_rows=len(symbols),
                correction_path=self.correction if correction else None,
                annotation_policy_path=self.policy if legacy else None,
                annotation_manifest_path=self.manifest if legacy else None)

    def assert_rejected(self, report, fragment):
        self.assertFalse(report['formal'], report)
        self.assertFalse(report['pass'])
        self.assertIn(fragment, ' '.join(report['formal_requirements_missing']))

    def test_old_annotation_code_is_history(self):
        report = self.evaluate()
        self.assertTrue(report['formal'], report['formal_requirements_missing'])
        self.assertEqual(report['provenance']['historical_annotation_code_identity'], self.reg['code_identity'])

    def test_same_sealed_labels_score_two_actually_executed_candidates(self):
        entities = self.dir / 'entities.json'
        write_json(entities, {'entities': {'SYN': {'canonical': '合成测试公司', 'aliases': []}}})
        self.candidate['entities_sha256'] = p.sha256(entities.read_bytes())
        self.reg['entities_sha256'] = self.candidate['entities_sha256']
        registry = json.loads(self.registry.read_text(encoding='utf-8'))
        registry['registered_sets'][0]['entities_sha256'] = self.candidate['entities_sha256']
        write_json(self.registry, registry)
        write_json(self.registration, self.reg)
        original = self.human.read_bytes(), self.registration.read_bytes()
        identities = []
        for index in (1, 2):
            source = self.dir / f'candidate-{index}'
            for name in (*p.code_identity()['files'], 'news_risk/__init__.py', 'docs/舆情风险标注说明.md'):
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            with (source/'news_risk/rules_v2.py').open('a', encoding='utf-8') as f:
                f.write(f'\n# Synthetic candidate {index} version only.\n')
            run = subprocess.run([sys.executable, '-m', 'news_risk', str(self.samples),
                '--profile', 'v2', '--entities', str(entities), '--run-id', f'SYNTHETIC-{index}',
                '--sample-set-id', self.candidate['sample_set_id'], '--output', str(self.system)],
                cwd=source, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', 'replace'))
            with patch.object(p, 'ROOT', source):
                report = self.evaluate()
            self.assertTrue(report['formal'], report['formal_requirements_missing'])
            self.assertEqual(report['consistent_count'], 50)
            identities.append(report['provenance']['prediction_evidence']['code_identity']['sha256'])
            self.assertEqual((self.human.read_bytes(), self.registration.read_bytes()), original)
        self.assertNotEqual(*identities)

    def test_predictor_mismatch_even_with_recomputed_payload(self):
        self.header['code_identity']['sha256'] = '0'*64
        self.save_header()
        self.assert_rejected(self.evaluate(), '预测代码版本')

    def test_prediction_digest_detects_changed_output(self):
        self.header['items'][0]['risk_verdict'] = 'risk'
        write_json(self.system, self.header)
        self.assert_rejected(self.evaluate(), '输出内容摘要')

    def test_missing_prediction_digest(self):
        self.header.pop('payload_sha256')
        write_json(self.system, self.header)
        self.assert_rejected(self.evaluate(), '输出内容摘要')

    def test_different_samples(self):
        self.samples.write_text('changed samples', encoding='utf-8')
        self.assert_rejected(self.evaluate(), '样本')

    def test_different_entities(self):
        self.header['entities_sha256'] = '0'*64
        self.save_header()
        self.assert_rejected(self.evaluate(), '主体映射')

    def test_different_profile(self):
        self.reg['profile'] = 'v2-other-criteria'
        self.assert_rejected(self.evaluate(), 'profile')

    def test_different_human_policy(self):
        self.reg['judgment_policy']['sha256'] = '0'*64
        self.assert_rejected(self.evaluate(), '人工登记判定口径')

    def test_different_prediction_policy(self):
        self.header['judgment_policy']['sha256'] = '0'*64
        self.save_header()
        self.assert_rejected(self.evaluate(), '预测判定口径')

    def test_changed_labels(self):
        self.write_labels(49)
        self.assert_rejected(self.evaluate(), '人工表 SHA256')

    def test_boolean_without_declaration_does_not_prove_independence(self):
        self.reg.pop('independence_declaration')
        self.assert_rejected(self.evaluate(), '缺有来源与记录时间')

    def test_correction_selects_evidence_without_changing_original(self):
        self.reg['independent_review'] = False
        self.make_correction()
        original = self.registration.read_bytes()
        report = self.evaluate(correction=True)
        self.assertTrue(report['formal'], report['formal_requirements_missing'])
        evidence = report['provenance']['independence_evidence']
        self.assertEqual(evidence['basis'], 'correction')
        self.assertIs(evidence['original_independent_review'], False)
        self.assertEqual(evidence['correction_sha256'], p.sha256(self.correction.read_bytes()))
        self.assertEqual(evidence['source'], self.corr['source'])
        self.assertEqual(self.registration.read_bytes(), original)

    def test_correction_wrong_registration(self):
        self.make_correction()
        self.corr['original_registration_sha256'] = '0'*64
        write_json(self.correction, self.corr)
        self.assert_rejected(self.evaluate(correction=True), 'original_registration_sha256')

    def test_correction_wrong_labels(self):
        self.make_correction()
        self.corr['human_labels_sha256'] = '0'*64
        write_json(self.correction, self.corr)
        self.assert_rejected(self.evaluate(correction=True), 'human_labels_sha256')

    def test_correction_wrong_reviewer_set_or_profile(self):
        self.make_correction()
        for field in ('annotator', 'sample_set_id', 'profile'):
            with self.subTest(field=field):
                value = dict(self.corr, **{field: 'wrong'})
                write_json(self.correction, value)
                self.assert_rejected(self.evaluate(correction=True), field)

    def test_correction_must_have_source_statement_and_positive_declaration(self):
        self.make_correction()
        for field, value in (('source', ''), ('statement', ''), ('independent_review', False)):
            with self.subTest(field=field):
                write_json(self.correction, dict(self.corr, **{field: value}))
                self.assert_rejected(self.evaluate(correction=True), field)

    def test_correction_time_requires_offset_not_future_not_before_seal(self):
        self.make_correction()
        for value in ('2020-01-05', '2020-01-05T00:00:00', '2099-01-01T00:00:00Z',
                      '2020-01-01T00:00:00Z'):
            with self.subTest(value=value):
                write_json(self.correction, dict(self.corr, recorded_at=value))
                self.assertFalse(self.evaluate(correction=True)['formal'])

    def test_correction_after_prediction_is_valid(self):
        self.reg['independent_review'] = False
        self.make_correction()  # Jan 5 correction after Jan 4 prediction.
        self.assertTrue(self.evaluate(correction=True)['formal'])

    def test_invalid_correction_never_falls_back_to_valid_original(self):
        self.correction.write_text('[]', encoding='utf-8')
        self.assert_rejected(self.evaluate(correction=True), '更正声明须为 JSON 对象')

    def test_annotation_and_prediction_time_order(self):
        for field in ('registered_at', 'annotation_started_at', 'annotation_completed_at', 'sealed_at'):
            with self.subTest(field=field):
                original = self.reg[field]
                self.reg[field] = '2099-01-01T00:00:00Z'
                self.assertFalse(self.evaluate()['formal'])
                self.reg[field] = original
        self.header['generated_at'] = '2020-01-01T00:00:00Z'
        self.save_header()
        self.assert_rejected(self.evaluate(), '时序')

    def test_legacy_seal_accepts_unchanged_archived_policy(self):
        self.legacy_package()
        report = self.evaluate(legacy=True)
        self.assertTrue(report['formal'], report['formal_requirements_missing'])
        self.assertEqual(report['provenance']['human_judgment_policy']['basis'], 'archived-annotation-package')

    def test_legacy_missing_policy_evidence_rejected(self):
        self.reg.pop('judgment_policy')
        self.assert_rejected(self.evaluate(), '原标注说明和原材料清单')

    def test_legacy_policy_changed_even_if_manifest_rehashed(self):
        self.legacy_package()
        with self.policy.open('a', encoding='utf-8') as f:
            f.write('\nEngineering-only note outside criteria.\n')
        self.assert_rejected(self.evaluate(legacy=True), '字节摘要')
        text = self.policy.read_text(encoding='utf-8').replace('## 3.', 'CHANGED CRITERIA\n## 3.')
        self.policy.write_text(text, encoding='utf-8')
        self.package['files'][self.policy.name] = p.sha256(self.policy.read_bytes())
        write_json(self.manifest, self.package)
        self.assert_rejected(self.evaluate(legacy=True), '原人工判定口径')

    def test_legacy_package_wrong_reviewer_commit_entities_or_time(self):
        self.legacy_package()
        for field, value in (('reviewer', 'other'), ('preregistration_commit', 'b'*40),
                             ('prepared_at', '2020-01-03T00:00:00Z'), ('files', {})):
            with self.subTest(field=field):
                write_json(self.manifest, dict(self.package, **{field: value}))
                self.assertFalse(self.evaluate(legacy=True)['formal'])

    def test_numeric_threshold_separate_from_evidence(self):
        for count in (42, 43):
            self.write_labels(count)
            self.reg['human_labels_sha256'] = p.sha256(self.human.read_bytes())
            report = self.evaluate()
            self.assertTrue(report['formal'], report['formal_requirements_missing'])
            self.assertEqual(report['consistent_count'], count)
            self.assertEqual(report['pass'], count == 43)
            self.assertEqual(report['meets_85_percent'], count == 43)

    def test_unverified_source_not_promoted_by_correction(self):
        self.make_correction()
        self.assert_rejected(self.evaluate(correction=True, label_source='unverified'), '标签来源')

    def test_report_contains_actual_output_digest(self):
        report = self.evaluate()
        self.assertEqual(report['provenance']['prediction_evidence']['system_output_sha256'],
                         p.sha256(self.system.read_bytes()))

    def run_cli(self):
        report = self.dir/'cli-report.json'
        args = ['score.py', '--samples', str(self.samples), '--human', str(self.human),
                '--system', str(self.system), '--registration', str(self.registration),
                '--sample-registry', str(self.registry), '--report', str(report),
                '--profile', 'v2', '--label-source', 'human-annotation',
                '--review-correction', str(self.correction),
                '--annotation-policy', str(self.policy), '--annotation-manifest', str(self.manifest)]
        (self.samples.with_suffix('.json.sha256')).write_text(self.candidate['samples_sha256'], encoding='utf-8')
        buffer = io.BytesIO()
        stdout = io.TextIOWrapper(buffer, encoding='utf-8')
        with patch.object(sys, 'argv', args), patch.object(score, 'BUILTIN_PREREGISTRATION', self.candidate), \
                patch.object(score, 'load_preregistration', return_value=({'candidate': self.candidate}, [])), \
                contextlib.redirect_stdout(stdout):
            code = score.main()
        stdout.flush()
        return code, json.loads(buffer.getvalue()), json.loads(report.read_text(encoding='utf-8'))

    def test_cli_correction_legacy_policy_and_stale_failure(self):
        self.legacy_package()
        self.reg['independent_review'] = False
        self.make_correction()
        code, report, _ = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertTrue(report['formal'])
        self.header['items'][0]['run_id'] = 'OTHER-RUN'
        self.header['payload_sha256'] = p.prediction_digest(self.header)
        write_json(self.system, self.header)
        code, report, disk = self.run_cli()
        self.assertEqual(code, 1)
        self.assertFalse(report['formal'])
        self.assertTrue(report['structural_errors'])
        self.assertFalse(disk['report_valid'])

    def test_cli_malformed_prediction_identity_fails_without_crash(self):
        self.legacy_package()
        self.make_correction()
        self.header['code_identity'] = ['not an identity object']
        self.save_header()
        code, report, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assert_rejected(report, '预测代码版本')


if __name__ == '__main__':
    unittest.main()
