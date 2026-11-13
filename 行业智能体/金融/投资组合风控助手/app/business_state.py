"""Append-only business components and links to deduplicated signals."""
import json


class BusinessState:
    def initialize_business_state(self,conn):
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS business_components(
            run_id TEXT,component_key TEXT,status TEXT,payload_json TEXT,
            PRIMARY KEY(run_id,component_key));
          CREATE TABLE IF NOT EXISTS business_signals(
            run_id TEXT,event_id TEXT,created INTEGER,PRIMARY KEY(run_id,event_id));
          INSERT OR IGNORE INTO business_signals SELECT run_id,event_id,1 FROM signals;
          CREATE TABLE IF NOT EXISTS topic16_schema_versions(version INTEGER PRIMARY KEY);
          INSERT OR IGNORE INTO topic16_schema_versions VALUES(4);
        ''')

    def save_component(self,run_id,key,status,payload):
        with self.connection() as conn:
            conn.execute('INSERT INTO business_components VALUES(?,?,?,?)',
                         (run_id,key,status,json.dumps(payload,ensure_ascii=False,sort_keys=True)))

    def business_record(self,run_id):
        with self.connection() as conn:
            run=conn.execute('SELECT * FROM monitor_runs WHERE run_id=?',(run_id,)).fetchone()
            if not run:raise ValueError('run_id 不存在')
            components=[dict(key=r[0],status=r[1],payload=json.loads(r[2])) for r in conn.execute(
                'SELECT component_key,status,payload_json FROM business_components WHERE run_id=? ORDER BY rowid',(run_id,))]
            links=[dict(r) for r in conn.execute('SELECT event_id,created FROM business_signals WHERE run_id=?',(run_id,))]
        signals=[self.get_signal(r['event_id']) for r in links]
        pushes=[p for r in links for p in self.list_pushes(event_id=r['event_id'])]
        result=dict(run);result['detail']=json.loads(result.pop('detail_json'))
        return dict(run=result,components=components,signal_links=links,signals=signals,
                    knowledge=self.knowledge_records(run_id),pushes=pushes,
                    real_wechat_delivery_proven=False)
