"""Private import ledger and retrieval-to-business-run links. Additive migration."""
import json


class KnowledgeState:
    def initialize_knowledge_state(self,conn):
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS knowledge_imports(
            index_id TEXT,doc_id TEXT,version TEXT,sha256 TEXT,filename TEXT,
            status TEXT,native_path TEXT,PRIMARY KEY(index_id,doc_id,version));
          CREATE TABLE IF NOT EXISTS knowledge_records(
            retrieval_id TEXT PRIMARY KEY,run_id TEXT,task_key TEXT,status TEXT,payload_json TEXT);
          CREATE INDEX IF NOT EXISTS idx_knowledge_run ON knowledge_records(run_id);
          CREATE TABLE IF NOT EXISTS knowledge_endpoints(index_id TEXT PRIMARY KEY,endpoint_sha256 TEXT);
        ''')

    def bind_knowledge_endpoint(self,index,identity):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT endpoint_sha256 FROM knowledge_endpoints WHERE index_id=?',(index,)).fetchone()
            if row and row[0]!=identity:raise ValueError('该索引绑定另一实例；请使用独立数据库')
            conn.execute('INSERT OR IGNORE INTO knowledge_endpoints VALUES(?,?)',(index,identity))
            conn.execute('COMMIT')

    def knowledge_claim_import(self,index,doc,filename):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM knowledge_imports WHERE index_id=? AND doc_id=? AND version=?',
                             (index,doc['doc_id'],doc['version'])).fetchone()
            if row:
                if row['sha256']!=doc['sha256']:raise ValueError('同文档版本内容变化，须新版本')
                conn.execute('COMMIT');return False,dict(row)
            conn.execute('INSERT INTO knowledge_imports VALUES(?,?,?,?,?,?,NULL)',
                         (index,doc['doc_id'],doc['version'],doc['sha256'],filename,'uploading'))
            conn.execute('COMMIT');return True,None

    def knowledge_import_finish(self,index,doc,status,native_path=None):
        with self.connection() as conn:
            conn.execute('UPDATE knowledge_imports SET status=?,native_path=COALESCE(?,native_path) '
                         'WHERE index_id=? AND doc_id=? AND version=?',
                         (status,native_path,index,doc['doc_id'],doc['version']))

    def knowledge_imports(self,index):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM knowledge_imports WHERE index_id=?',(index,))]

    def save_knowledge(self,record):
        with self.connection() as conn:
            conn.execute('INSERT INTO knowledge_records VALUES(?,?,?,?,?)',
                         (record['retrieval_id'],record['run_id'],record['task_key'],record['status'],
                          json.dumps(record,ensure_ascii=False,sort_keys=True)))

    def knowledge_records(self,run_id):
        with self.connection() as conn:
            return [json.loads(r[0]) for r in conn.execute(
                'SELECT payload_json FROM knowledge_records WHERE run_id=? ORDER BY rowid',(run_id,))]
