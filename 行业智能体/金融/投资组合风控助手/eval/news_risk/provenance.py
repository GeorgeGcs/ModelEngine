"""Versioned metadata checks. Declarations do not prove human independence."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION_V2 = 'topic16-human-annotation-registration-v2'
CORRECTION_V1 = 'topic16-review-evidence-correction-v1'
POLICY_PATH = ROOT / 'docs/舆情风险标注说明.md'
POLICY_SHA256 = '38bfb5d5ed7a98394d577bcd7e6312feb4f4c8713f2c9cbe9bd894cec375f311'


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def prediction_digest(header):
    """Canonical digest of all prediction fields except the digest itself."""
    body = {k: v for k, v in header.items() if k != 'payload_sha256'}
    return sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8'))


def judgment_policy(path=None):
    """Business criteria only: subjects, risk, time, uncertainty and scoring.

    Engineering instructions may evolve without changing the human task.
    The actual archived document is additionally hashed without normalization.
    """
    text = (path or POLICY_PATH).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
    start, end = text.index('## 2.'), text.index('## 7.')
    if end <= start:
        raise ValueError('人工判定口径章节缺失或顺序错误')
    return {'schema': 'topic16-news-judgment-policy-v1',
            'id': 'topic16-human-binary-20261109',
            'sha256': sha256(text[start:end].encode('utf-8'))}


def human_policy_evidence(registration, *, policy_path=None, manifest_path=None):
    """Bind labels to criteria, including read-only evidence for legacy seals."""
    problems, details = [], {}
    try:
        current = judgment_policy()
        if current['sha256'] != POLICY_SHA256:
            problems.append('当前人工判定口径与已固定版本不一致')
        declared = registration.get('judgment_policy')
        if declared is not None:
            if declared != current:
                problems.append('人工登记判定口径与当前评测不一致')
            details = {'basis': 'registration', 'judgment_policy': declared}
        if policy_path is not None or manifest_path is not None or declared is None:
            if policy_path is None or manifest_path is None:
                return problems + ['旧登记缺判定口径摘要，须同时提供原标注说明和原材料清单'], details
            raw_policy, raw_manifest = policy_path.read_bytes(), manifest_path.read_bytes()
            manifest = json.loads(raw_manifest.decode('utf-8-sig'))
            if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
                raise ValueError('标注材料清单须含 files 对象')
            if (manifest.get('reviewer') != registration.get('annotator') or
                    manifest.get('preregistration_commit') != registration.get('preregistered_commit')):
                problems.append('标注材料清单的复核者或预登记提交与原登记不一致')
            if manifest['files'].get(policy_path.name) != sha256(raw_policy):
                problems.append('原标注说明字节摘要与标注材料清单不一致')
            if manifest['files'].get('sim_entities.json') != registration.get('entities_sha256'):
                problems.append('标注材料清单的主体映射与原登记不一致')
            prepared = aware(manifest.get('prepared_at'))
            if not aware(registration.get('registered_at')) <= prepared <= aware(registration.get('annotation_started_at')):
                problems.append('标注材料清单须在登记后、实际标注开始前准备')
            policy = judgment_policy(policy_path)
            if policy != current:
                problems.append('原人工判定口径与当前评测不一致')
            details = {'basis': 'archived-annotation-package', 'judgment_policy': policy,
                       'policy_document_sha256': sha256(raw_policy),
                       'manifest_sha256': sha256(raw_manifest), 'prepared_at': manifest.get('prepared_at')}
    except (OSError, ValueError, TypeError) as exc:
        problems.append('人工判定口径证据不可核验：' + str(exc))
    return problems, details


def independence_evidence(registration, *, registration_sha, human_sha,
                          correction_path=None, now=None):
    """Validate and report the selected declaration; never rewrite a seal."""
    problems = []
    details = {'original_independent_review': registration.get('independent_review'),
               'original_registration_sha256': registration_sha,
               'human_labels_sha256': human_sha, 'basis': 'original-declaration'}
    try:
        current = now or datetime.now(timezone.utc)
        if correction_path is not None:
            raw = correction_path.read_bytes()
            declaration = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(declaration, dict):
                raise ValueError('更正声明须为 JSON 对象')
            details.update(basis='correction', correction_sha256=sha256(raw))
            expected = {'schema': CORRECTION_V1,
                        'original_registration_sha256': registration_sha,
                        'human_labels_sha256': human_sha,
                        'annotator': registration.get('annotator'),
                        'sample_set_id': registration.get('sample_set_id'),
                        'profile': registration.get('profile')}
            for field, value in expected.items():
                if declaration.get(field) != value:
                    problems.append('更正声明关联字段不符：' + field)
            if declaration.get('independent_review') is not True:
                problems.append('更正声明未明确 independent_review=true')
            earliest = aware(registration.get('sealed_at'))
        else:
            declaration = registration.get('independence_declaration')
            if not isinstance(declaration, dict):
                raise ValueError('缺有来源与记录时间的独立性声明')
            if registration.get('independent_review') is not True:
                problems.append('封存记录未声明 independent_review=true')
            earliest = aware(registration.get('registered_at'))
        for field in ('source', 'statement'):
            if not isinstance(declaration.get(field), str) or not declaration[field].strip():
                problems.append('独立性声明缺 ' + field)
        recorded = aware(declaration.get('recorded_at'))
        if not earliest <= recorded <= current:
            problems.append('独立性声明记录时间早于关联记录或晚于评分时刻')
        if correction_path is None and recorded > aware(registration.get('annotation_started_at')):
            problems.append('原独立性声明晚于标注开始；应保留原记录并另附有出处的更正')
        details.update({key: declaration.get(key) for key in ('source', 'statement', 'recorded_at')})
    except (OSError, ValueError, TypeError) as exc:
        problems.append('独立性声明不可核验：' + str(exc))
    details['valid'] = not problems
    return problems, details


def code_identity():
    # Hash source text with LF normalization, portable across Windows/Linux checkout.
    files = ['news_risk/__main__.py','news_risk/rules.py','news_risk/rules_v2.py',
             'news_risk/penalty.py','eval/news_risk/provenance.py']
    hashes = {name: hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
              for name in files}
    digest = hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'schema':'topic16-news-code-v1','normalization':'UTF-8 source LF','files':hashes,'sha256':digest}


def aware(value):
    if not isinstance(value,str) or 'T' not in value:
        raise ValueError('timestamp requires date, time and offset')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('timestamp requires offset')
    return result


def binding_problems(registration, header, *, samples_sha, candidate, now=None):
    problems=[]
    if registration.get('template_only') is True:
        problems.append('模板不是实际封存记录')
    if registration.get('schema') != REGISTRATION_V2:
        problems.append('缺 v2 封存记录：须核验样本、主体与完整时序，不能补造历史')
    identity=code_identity()
    # Human labels bind to the task and policy, not the predictor implementation.
    # The old seal's code_identity / commit are retained as historical evidence.
    if header.get('code_identity') != identity:
        problems.append('预测代码版本摘要与当前被测实现不一致')
    for document, label in ((registration,'封存'),(header,'预测')):
        for field, value in (('sample_set_id',candidate['sample_set_id']),
                             ('samples_sha256',samples_sha),('profile',candidate['profile']),
                             ('entities_sha256',candidate['entities_sha256'])):
            if document.get(field) != value:problems.append(label+field+'与预登记/实际输入不一致')
    if samples_sha != candidate['samples_sha256']:
        problems.append('实际样本摘要与当前候选不一致')
    try:
        policy = judgment_policy()
        if policy['sha256'] != POLICY_SHA256 or header.get('judgment_policy') != policy:
            problems.append('预测判定口径与当前固定口径不一致')
        if header.get('payload_sha256') != prediction_digest(header):
            problems.append('预测输出内容摘要不一致或缺失')
    except (OSError, ValueError, TypeError) as exc:
        problems.append('预测摘要不可核验：' + str(exc))
    if not isinstance(header.get('run_id'),str) or not header['run_id'].strip():
        problems.append('预测缺 run_id')
    if not re.fullmatch(r'[0-9a-f]{40}',str(registration.get('preregistered_commit',''))):
        problems.append('缺标注前登记提交的完整 SHA；仍须人工验证真实提交和身份')
    try:
        times=[aware(registration.get(k)) for k in ('registered_at','annotation_started_at')]
        if 'annotation_completed_at' in registration:
            times.append(aware(registration['annotation_completed_at']))
        times.append(aware(registration.get('sealed_at')))
        times.append(aware(header.get('generated_at')))
        current=now or datetime.now(timezone.utc)
        if times != sorted(times) or times[-1] > current:
            problems.append('时序应为登记<=标注开始<=封存<=预测生成<=当前时间')
    except (ValueError,TypeError):
        problems.append('完整时序字段必须是带显式时区的 ISO 时间')
    return problems
