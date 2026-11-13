"""Persistent planned slots, attempts and restart-safe execution. Standard library only.

Run with `python -m app.scheduler --help`. Native platform and local rules are
separate modes. Expired in-flight attempts remain unknown, never silently replayed.
"""
import argparse
import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .gateway import build_gateway
from .service import Service
from .store import Store


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def instant(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('时间必须含时区')
    return dt.timestamp()


def validate(plan):
    if plan['timezone'] != 'Asia/Shanghai':
        raise ValueError('时区必须为 Asia/Shanghai')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', plan['window_id']):
        raise ValueError('window_id 仅支持字母数字及下划线/连字符')
    if plan['mode'] not in ('local-rules', 'nexent-price'):
        raise ValueError('须显式选择 local-rules 或 nexent-price')
    if plan['gateway'] not in ('local-mock', 'webhook', 'nexent-wechat'):
        raise ValueError('未知网关')
    if plan['gateway'] == 'nexent-wechat' and plan['mode'] != 'nexent-price':
        raise ValueError('微信 MCP 网关需要 nexent-price 模式')
    start, end, cutoff = (instant(plan[k]) for k in ('start', 'end', 'cutoff'))
    if not start < end <= cutoff:
        raise ValueError('窗口范围不合法')
    for k in ('tolerance_seconds', 'retry_delay_seconds', 'lease_seconds', 'max_attempts',
              'window_seconds', 'max_per_window'):
        value = plan[k]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(k + ' 必须为正数')
    if type(plan['max_attempts']) is not int or type(plan['max_per_window']) is not int:
        raise ValueError('计数上限必须为整数')
    if plan['lease_seconds'] <= 150:
        raise ValueError('lease_seconds 必须大于平台调用超时及写入余量 150 秒')
    seen = set()
    if not plan['slots']:
        raise ValueError('计划不得为空')
    for slot in plan['slots']:
        sid = slot['slot_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', sid) or sid in seen:
            raise ValueError('slot_id 非法或重复')
        seen.add(sid)
        if not start <= instant(slot['scheduled_at']) < end:
            raise ValueError('时隙不在窗口内')
        args = slot['tool_arguments']
        if not args['portfolio']['id'] or not args['portfolio']['holdings'] or not args['snapshot']['prices']:
            raise ValueError('缺组合/持仓/行情')
        instant(args['snapshot']['timestamp'])


class Scheduler:
    def __init__(self, store, plan, *, clock=time.time):
        validate(plan)
        self.store, self.plan, self.clock = store, json.loads(canonical(plan)), clock
        self.window = plan['window_id']
        self.digest = hashlib.sha256(canonical(plan).encode()).hexdigest()
        with store.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS schedule_windows(
                    window_id TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,plan_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule_slots(
                    window_id TEXT,slot_id TEXT,scheduled REAL,state TEXT NOT NULL,
                    owner TEXT,lease_until REAL,next_at REAL NOT NULL,
                    PRIMARY KEY(window_id,slot_id));
                CREATE TABLE IF NOT EXISTS schedule_attempts(
                    attempt_id TEXT PRIMARY KEY,window_id TEXT,slot_id TEXT,source TEXT,
                    started REAL,finished REAL,status TEXT,result_json TEXT,error_type TEXT);
                CREATE TABLE IF NOT EXISTS schedule_heartbeats(window_id TEXT,observed_at REAL);
            ''')
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT plan_sha256 FROM schedule_windows WHERE window_id=?',
                                    (self.window,)).fetchone()
            if existing and existing[0] != self.digest:
                raise ValueError('已有窗口的计划已冻结，不得改配置/时隙')
            conn.execute('INSERT OR IGNORE INTO schedule_windows VALUES(?,?,?)',
                         (self.window, self.digest, canonical(plan)))
            for slot in plan['slots']:
                at = instant(slot['scheduled_at'])
                conn.execute("INSERT OR IGNORE INTO schedule_slots VALUES(?,?,?,'pending',NULL,NULL,?)",
                             (self.window, slot['slot_id'], at, at))
            conn.execute('COMMIT')

    def recover(self):
        now = self.clock()
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT * FROM schedule_slots WHERE window_id=? AND state='running' "
                                "AND lease_until<=?", (self.window, now)).fetchall()
            for row in rows:
                conn.execute("UPDATE schedule_attempts SET status='unknown',finished=?,error_type=? "
                             "WHERE attempt_id=?", (now, 'ExpiredLease', row['owner']))
                conn.execute("UPDATE schedule_slots SET state='unknown',owner=NULL,lease_until=NULL "
                             "WHERE window_id=? AND slot_id=?", (self.window, row['slot_id']))
            conn.execute('COMMIT')
        self.store.recover_pushes()

    def claim(self, slot, source='automatic'):
        now, sid = self.clock(), slot['slot_id']
        if source not in ('automatic', 'manual'):
            raise ValueError('未知尝试来源')
        cutoff = instant(self.plan['cutoff'])
        if source == 'automatic' and now > cutoff:
            return None
        if source == 'manual' and now <= cutoff:
            raise ValueError('人工补跑只允许封窗后执行，不能替代自动时隙')
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM schedule_slots WHERE window_id=? AND slot_id=?',
                               (self.window, sid)).fetchone()
            count = conn.execute('SELECT COUNT(*) FROM schedule_attempts WHERE window_id=? AND slot_id=? AND source=?',
                                 (self.window, sid, source)).fetchone()[0]
            if (row['state'] not in ('pending', 'failed') or row['scheduled'] > now
                    or row['next_at'] > now or count >= self.plan['max_attempts']):
                conn.execute('COMMIT'); return None
            token = uuid.uuid4().hex
            conn.execute("UPDATE schedule_slots SET state='running',owner=?,lease_until=? "
                         'WHERE window_id=? AND slot_id=?',
                         (token, now + self.plan['lease_seconds'], self.window, sid))
            conn.execute("INSERT INTO schedule_attempts VALUES(?,?,?,?,?,NULL,'running',NULL,NULL)",
                         (token, self.window, sid, source, now))
            conn.execute('COMMIT')
            return token

    def finish(self, slot, token, status, result=None, error=None):
        if status not in ('completed', 'failed', 'unknown'):
            raise ValueError('非法完成状态')
        now = self.clock()
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute('UPDATE schedule_slots SET state=?,owner=NULL,lease_until=NULL,next_at=? '
                                   "WHERE window_id=? AND slot_id=? AND state='running' AND owner=?",
                                   (status, now + self.plan['retry_delay_seconds'], self.window,
                                    slot['slot_id'], token)).rowcount
            if changed:
                conn.execute('UPDATE schedule_attempts SET status=?,finished=?,result_json=?,error_type=? '
                             'WHERE attempt_id=?', (status, now, canonical(result), error, token))
            conn.execute('COMMIT')
            return bool(changed)

    def tick(self, executor, *, source='automatic', slot_id=None):
        with self.store.connection() as conn:
            conn.execute('INSERT INTO schedule_heartbeats VALUES(?,?)',(self.window,self.clock()))
        self.recover()
        executed = 0
        for slot in sorted(self.plan['slots'], key=lambda s: (instant(s['scheduled_at']), s['slot_id'])):
            if slot_id is not None and slot['slot_id'] != slot_id:
                continue
            token = self.claim(slot, source)
            if token is None:
                continue
            try:
                result = executor(slot, token)
                status = result['execution_status']
                self.finish(slot, token, status, result)
            except Exception as exc:
                # External side effects may already exist. No automatic replay.
                self.finish(slot, token, 'unknown', error=type(exc).__name__)
            executed += 1
        return executed

    def report(self):
        now, cutoff = self.clock(), instant(self.plan['cutoff'])
        out = dict(window_id=self.window, plan_sha256=self.digest, mode=self.plan['mode'],
                   gateway=self.plan['gateway'], timezone='Asia/Shanghai',
                   observation_closed=now > cutoff, duration_hours=(instant(self.plan['end'])-instant(self.plan['start']))/3600,
                   planned=len(self.plan['slots']), on_time=0, late=0, failed=0, missing=0,
                   pending=0, retries=0, manual=0, unknown=0, slots=[])
        out['completion_scope']='signal persistence and gateway accepted/simulated delivery/suppression decision; not terminal delivery'
        out['push_observations']={}
        out['push_attempts_in_slots']=0
        with self.store.connection() as conn:
            beats=[r[0] for r in conn.execute('SELECT observed_at FROM schedule_heartbeats '
                'WHERE window_id=? ORDER BY observed_at',(self.window,))]
            out['worker_heartbeat']={'count':len(beats),'first':beats[0] if beats else None,
                'last':beats[-1] if beats else None,
                'largest_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=None)}
            env=conn.execute("SELECT name FROM sqlite_master WHERE name='schedule_environments'").fetchone()
            out['environment']=None
            if env:
                row=conn.execute('SELECT config_json FROM schedule_environments WHERE window_id=?',(self.window,)).fetchone()
                if row:out['environment']=json.loads(row[0])
            for slot in self.plan['slots']:
                rows = [dict(r) for r in conn.execute('SELECT * FROM schedule_attempts WHERE window_id=? '
                    'AND slot_id=? ORDER BY started,attempt_id', (self.window, slot['slot_id']))]
                auto = [r for r in rows if r['source']=='automatic' and r['started'] <= cutoff]
                successes = [r for r in auto if r['status']=='completed' and r['finished'] <= cutoff]
                if successes:
                    first = min(r['finished'] for r in successes)
                    outcome = 'on_time' if first <= instant(slot['scheduled_at'])+self.plan['tolerance_seconds'] else 'late'
                elif now <= cutoff:
                    outcome = 'pending'
                else:
                    outcome = 'failed' if auto else 'missing'
                out[outcome] += 1
                out['retries'] += max(0,len(auto)-1)
                out['manual'] += sum(r['source']=='manual' for r in rows)
                out['unknown'] += sum(r['status']=='unknown' for r in rows)
                for row in rows:
                    result=json.loads(row['result_json'] or 'null') or {}
                    for push in result.get('pushes',[]):
                        key=push['status']
                        out['push_observations'][key]=out['push_observations'].get(key,0)+1
                        out['push_attempts_in_slots']+=bool(push.get('attempted'))
                out['slots'].append(dict(slot_id=slot['slot_id'],outcome=outcome,attempts=rows))
        out['counts_balance'] = out['planned'] == sum(out[k] for k in ('on_time','late','failed','missing','pending'))
        out['real_wechat_delivery_proven'] = False
        return out


class Executor:
    def __init__(self, store, plan, evidence_dir):
        self.plan, self.store = plan, store
        self.directory = Path(evidence_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        from integration.wechat import NexentWeChatGateway
        gateway = NexentWeChatGateway() if plan['gateway'] == 'nexent-wechat' else build_gateway(plan['gateway'])
        self.service = Service(store, gateway,
                               window_seconds=plan['window_seconds'], max_per_window=plan['max_per_window'])
        recipient=os.environ.get('TOPIC16_RECIPIENT_ID')
        if recipient:self.service.recipient_id=recipient
        if plan['gateway']=='webhook' and (not recipient or not self.service.gateway.url):
            raise ValueError('Webhook 窗口缺私有网关或接收人配置')
        self.config = None
        if plan['mode'] == 'nexent-price':
            from eval.nexent.nexent_client import PlatformConfig
            self.config = PlatformConfig.from_env()
            if self.config.missing() or not self.config.execution_records:
                raise ValueError('真实模式缺平台配置或独立 NEXENT_EXECUTION_RECORDS')
        wechat_keys = None
        if plan['gateway'] == 'nexent-wechat':
            from integration.dispatch import read_key
            paths = [os.environ.get(name) for name in
                     ('TOPIC16_DISPATCH_KEY_FILE', 'TOPIC16_WECHAT_MCP_KEY_FILE')]
            self.wechat_transport = os.environ.get('TOPIC16_WECHAT_MCP_TRANSPORT')
            if not recipient or not all(paths) or self.wechat_transport not in ('clawbot','local-mock'):
                raise ValueError('微信 MCP 窗口缺少接收人或签名配置')
            wechat_keys = [hashlib.sha256(read_key(path)).hexdigest() for path in paths]
        root=Path(__file__).resolve().parents[1]
        code_files=sorted([*root.glob('app/*.py'),*root.glob('portfolio_risk/*.py'),
                           *root.glob('news_risk/*.py'),root/'eval/nexent/nexent_client.py'])
        fingerprint = {'mode':plan['mode'],'gateway':self.service.gateway.describe(),
                       'code_hashes':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files},
                       'private_config_sha256':hashlib.sha256(canonical([
                           str(self.directory),self.service.recipient_id,
                           getattr(self.service.gateway,'url','')]).encode()).hexdigest(),
                       'endpoint_sha256':hashlib.sha256((self.config.base_url+self.config.agent_name+
                           (self.config.execution_records or '')+str(self.config.model)).encode()).hexdigest() if self.config else None}
        if wechat_keys:
            fingerprint['wechat_key_sha256'] = wechat_keys
            fingerprint['wechat_transport'] = self.wechat_transport
            for name in ('integration/dispatch.py','integration/wechat.py','integration/platform.py'):
                fingerprint['code_hashes'][name] = hashlib.sha256((root/name).read_bytes()).hexdigest()
        with store.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS schedule_environments(window_id TEXT PRIMARY KEY,config_json TEXT)')
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT config_json FROM schedule_environments WHERE window_id=?',(plan['window_id'],)).fetchone()
            if row and row[0] != canonical(fingerprint):
                raise ValueError('窗口运行环境变更，须另开独立窗口')
            conn.execute('INSERT OR IGNORE INTO schedule_environments VALUES(?,?)',(plan['window_id'],canonical(fingerprint)))
            conn.execute('COMMIT')

    def __call__(self, slot, token):
        args = slot['tool_arguments'];portfolio=args['portfolio'];snapshot=args['snapshot']
        run_id = self.plan['window_id']+'-'+slot['slot_id']+'-'+token
        if self.plan['mode']=='local-rules':
            result=self.service.monitor(prices=snapshot['prices'],timestamp=snapshot['timestamp'],
                                        frozen_portfolios=[portfolio],run_id=run_id)
            return {'execution_status':self.push_status(result['pushes']),'run_id':run_id,'mode':'local-rules',
                    'signals':result['signals'],'pushes':result['pushes']}
        from eval.nexent.nexent_client import invoke_task
        task={'task_key':portfolio['id']+'|'+snapshot['timestamp'],'tool_arguments':args}
        result=invoke_task(self.config,run_id=run_id,task=task,timeout=120)
        evidence = self.write_evidence(token+'.json', result)
        if result['evidence_status']!='verified':
            return {'execution_status':'unknown','run_id':run_id,'mode':'nexent-price','evidence':evidence}
        tool=result['tool_evidence']; pushes=[]
        required={'event_id','portfolio_id','timestamp','symbol','rule','price','threshold'}
        symbols={h['symbol'] for h in portfolio['holdings']}
        if self.plan['gateway'] == 'nexent-wechat' and (
                len(result['signals']) > len(symbols)
                or 150 + 40 * len(result['signals']) >= self.plan['lease_seconds']):
            raise ValueError('微信 MCP 发送超出单时隙租约预算')
        for signal in result['signals']:
            if (not isinstance(signal,dict) or not required <= signal.keys()
                    or signal['portfolio_id']!=portfolio['id'] or signal['symbol'] not in symbols
                    or signal['rule'] not in ('stop_loss','take_profit')
                    or instant(signal['timestamp'])!=instant(snapshot['timestamp'])):
                raise ValueError('原生信号结构/任务归属不符，保留原始返回')
            existing=self.store.get_signal(signal['event_id'])
            if existing and existing['detail'].get('raw_signal')!=signal:
                raise ValueError('同 event_id 原始信号冲突')
        self.store.open_run(run_id,{'mode':'nexent-price','evidence':evidence,'tool_evidence':tool})
        for index, signal in enumerate(result['signals']):
            row={'event_id':signal['event_id'],'run_id':run_id,'portfolio_id':signal['portfolio_id'],
                 'portfolio_name':portfolio.get('name',portfolio['id']),'portfolio_type':portfolio.get('type',''),
                 'symbol':signal['symbol'],'risk_type':signal['rule'],'risk_type_name':signal['rule'],
                 'trigger_time':signal['timestamp'],'evidence':canonical(signal),'level':'待复核',
                 'review_action':'人工核对工具返回与行情依据',
                 'detail':{'mode':'nexent-price','raw_signal':signal,'tool_evidence':tool,'evidence':evidence,
                           **({'wechat_portfolio_name':portfolio.get('name',portfolio['id']),
                               'wechat_portfolio_type':portfolio.get('type','')}
                              if self.plan['gateway'] == 'nexent-wechat' else {})}}
            self.store.insert_signal(row)
            if self.plan['gateway'] == 'nexent-wechat':
                persisted = self.store.get_signal(row['event_id'])
                pushes.append(self.push_wechat(persisted, f'{token}-wechat-{index}.json'))
            else:
                pushes.append(self.service._push(row))
        self.store.finish_run(run_id,'completed',len(result['signals']),sum(p['attempted'] for p in pushes))
        return {'execution_status':self.push_status(pushes),'run_id':run_id,'mode':'nexent-price',
                'evidence':evidence,'tool_evidence':tool,'signal_count':len(result['signals']),'pushes':pushes}

    def write_evidence(self, filename, result):
        raw = canonical(result).encode()
        path = self.directory / filename
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw)
        return {'file':path.name,'sha256':hashlib.sha256(raw).hexdigest()}

    def push_wechat(self, signal, filename):
        from integration.platform import invoke
        from integration.wechat import TOOL, authorize
        event_id = signal['event_id']
        bound_run = signal['run_id']
        delivery = [self.wechat_transport, self.service.recipient_id,
                    self.service.window_seconds, self.service.max_per_window]
        args = {'event_id':event_id, 'run_id':bound_run, 'authorization':authorize(event_id, bound_run, delivery)}
        try:
            native = invoke(self.config, tool=TOOL, arguments=args, run_id=bound_run,
                            task_key=event_id, timeout=35)
        except (OSError, ValueError) as exc:
            native = {'evidence_status':'insufficient','error_type':type(exc).__name__}
        evidence = self.write_evidence(filename, native)
        result = native.get('result')
        unknown = {'status':'unknown','event_id':event_id,'attempted':False,'evidence':evidence}
        if (native.get('evidence_status') != 'verified'
                or native.get('dispatch_mode') != 'published-agent-workflow'
                or not isinstance(result, dict)
                or result.get('event_id') != event_id or result.get('run_id') != bound_run
                or type(result.get('attempted')) is not bool):
            return unknown
        with self.store.connection() as conn:
            saved = conn.execute(
                'SELECT id,status,gateway_id,simulated FROM push_records WHERE event_id=? AND recipient_id=?',
                (event_id,self.service.recipient_id)).fetchone()
            attempt = conn.execute(
                'SELECT push_id,status FROM push_attempts WHERE attempt_id=?',
                (result.get('attempt_id'),)).fetchone()
        if (not saved or saved['id'] != result.get('push_id') or saved['status'] != result.get('status')
                or saved['gateway_id'] != result.get('gateway_id') or bool(saved['simulated']) != result.get('simulated')
                or saved['gateway_id'] != self.wechat_transport
                or (saved['status'] == 'delivered' and not saved['simulated'])
                or (result['attempted'] and (not attempt or attempt['push_id'] != saved['id']
                                             or attempt['status'] != saved['status']))):
            return unknown
        return dict(result, evidence=evidence, mcp_call_id=native['tool_evidence']['call_id'])

    def push_status(self, pushes):
        for row in pushes:
            if self.plan['gateway'] != 'nexent-wechat' and row['status']=='failed' and row.get('duplicate'):
                retried=self.service.retry_push(row['event_id'],source='automatic')
                if retried['retried']:
                    row.update(retried,attempted=True)
        if any(p['status'] in ('unknown','sending','pending') for p in pushes):return 'unknown'
        if any(p['status']=='failed' for p in pushes):return 'failed'
        return 'completed'


def make_plan(args):
    portfolios=json.loads(Path(args.portfolios).read_text(encoding='utf-8-sig'))
    if isinstance(portfolios,dict):portfolios=portfolios['portfolios']
    snapshot=json.loads(Path(args.snapshot).read_text(encoding='utf-8-sig'))
    start=datetime.fromtimestamp(instant(args.start), timezone(timedelta(hours=8)))
    if args.hours<=0 or args.interval<=0:raise ValueError('时长和间隔须为正数')
    end=start+timedelta(hours=args.hours);slots=[];at=start;index=0
    while at<end:
        for portfolio in portfolios:
            index+=1
            slots.append({'slot_id':f'slot-{index:06d}','scheduled_at':at.isoformat(),
                          'tool_arguments':{'portfolio':portfolio,'snapshot':snapshot}})
        at+=timedelta(seconds=args.interval)
    plan={'window_id':args.window_id,'timezone':'Asia/Shanghai','mode':args.mode,'gateway':args.gateway,
          'start':start.isoformat(),'end':end.isoformat(),'cutoff':(end+timedelta(minutes=5)).isoformat(),
          'tolerance_seconds':120,'retry_delay_seconds':60,'lease_seconds':300,'max_attempts':2,
          'window_seconds':60,'max_per_window':3,'slots':slots,
          'data_scope':'synthetic-fixed-snapshot','note':'计划时隙与行情快照时间分离；重复固定行情，不代表实时市场。'}
    validate(plan)
    with Path(args.output).open('x',encoding='utf-8') as f:json.dump(plan,f,ensure_ascii=False,indent=2)


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    create=sub.add_parser('make-plan')
    for name in ('window-id','portfolios','snapshot','start','output'):create.add_argument('--'+name,required=True)
    create.add_argument('--hours',type=float,default=24);create.add_argument('--interval',type=int,default=1800)
    create.add_argument('--mode',choices=('local-rules','nexent-price'),required=True)
    create.add_argument('--gateway',choices=('local-mock','webhook','nexent-wechat'),default='local-mock')
    for command in ('run','report','manual'):
        c=sub.add_parser(command)
        for name in ('plan','db'):c.add_argument('--'+name,required=True)
        if command!='report':c.add_argument('--evidence-dir',required=True)
        if command=='report':c.add_argument('--output',required=True)
        if command=='manual':c.add_argument('--slot-id',required=True)
        if command=='run':c.add_argument('--once',action='store_true');c.add_argument('--poll',type=float,default=2)
    args=p.parse_args()
    if args.command=='make-plan':make_plan(args);return 0
    plan=json.loads(Path(args.plan).read_text(encoding='utf-8-sig'));store=Store(args.db)
    scheduler=Scheduler(store,plan)
    if args.command=='report':
        scheduler.recover()
        with Path(args.output).open('x',encoding='utf-8') as f:json.dump(scheduler.report(),f,ensure_ascii=False,indent=2)
        return 0
    executor=Executor(store,plan,args.evidence_dir)
    if args.command=='manual':scheduler.tick(executor,source='manual',slot_id=args.slot_id);return 0
    if args.poll<=0:raise ValueError('poll 必须大于 0')
    while True:
        scheduler.tick(executor)
        if args.once or time.time()>instant(plan['cutoff']):break
        time.sleep(min(args.poll,60))
    return 0


if __name__=='__main__':raise SystemExit(main())
