"""Native Nexent v2.8.3 HTTP retrieval plus source verification; no vector store.

Endpoint contract: backend/apps/northbound_knowledge_app.py at 046d77c8.
Native API calls have no Agent tool call ID. Local retrieval IDs are labelled as such.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime,timezone

MANIFEST=Path(__file__).with_name('manifest.json')
SUCCESS=('hit','no_match')
PROJECT_TEXT_HEADER = re.compile(
    r'版本：[^；\n]+；编制日期：(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})'
    r'；来源：[^；\n]+；许可：(?P<license>CC0-1\.0)。'
)


class KnowledgeError(Exception):
    def __init__(self,status,http_status=None):
        super().__init__(status);self.status=status;self.http_status=http_status


def digest(raw):return hashlib.sha256(raw).hexdigest()


def load_assets(manifest=MANIFEST):
    path=Path(manifest).resolve();root=path.parent
    data=json.loads(path.read_text(encoding='utf-8'));documents={}
    for doc in data['documents']:
        file=(root/doc['file']).resolve()
        if not file.is_relative_to(root):raise KnowledgeError('provenance_error')
        raw=file.read_bytes()
        if digest(raw)!=doc['sha256']:raise KnowledgeError('provenance_error')
        key=(doc['doc_id'],doc['version'])
        if key in documents:raise KnowledgeError('provenance_error')
        documents[key]={**doc,'bytes':raw,'text':raw.decode('utf-8')}
    return data,documents


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class NativeAPI:
    scope='nexent-native-http'
    def __init__(self,base_url,token,timeout=15):
        self.base=base_url.rstrip('/');self.token=token;self.timeout=timeout
        parts=urllib.parse.urlsplit(self.base)
        if (not self.base or not token or parts.scheme not in ('http','https')
                or not parts.netloc or parts.username or parts.query or parts.fragment):
            raise KnowledgeError('configuration_error')
        self.opener=urllib.request.build_opener(NoRedirect())
        self.identity=digest(self.base.encode())

    def request(self,method,path,*,payload=None,body=None,content_type=None,binary=False):
        if not path.startswith('/nb/v1/knowledge/'):raise KnowledgeError('configuration_error')
        headers={'Authorization':'Bearer '+self.token}
        if payload is not None:body=json.dumps(payload).encode();content_type='application/json'
        if content_type:headers['Content-Type']=content_type
        request=urllib.request.Request(self.base+path,data=body,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                raw=response.read(8*1024*1024+1)
                if len(raw)>8*1024*1024:raise KnowledgeError('service_error')
                if binary:return raw
                result=json.loads(raw)
                if not isinstance(result,dict):raise KnowledgeError('service_error')
                return result
        except urllib.error.HTTPError as exc:
            raise KnowledgeError('permission_denied' if exc.code in (401,403) else
                                 'configuration_error' if exc.code==409 else 'service_error',exc.code) from None
        except (TimeoutError,):raise KnowledgeError('timeout') from None
        except urllib.error.URLError as exc:
            raise KnowledgeError('timeout' if isinstance(exc.reason,TimeoutError) else 'service_error') from None
        except (OSError,ValueError):raise KnowledgeError('service_error') from None


def query_terms(query):
    words=re.findall(r'[a-z0-9]{2,}|[\u4e00-\u9fff]+',query.lower())
    terms=set()
    for word in words:
        if re.fullmatch(r'[\u4e00-\u9fff]+',word):terms.update(word[i:i+2] for i in range(len(word)-1))
        else:terms.add(word)
    return terms


def align_source_quote(doc, row):
    """Return a unique literal source span or the verified whole-document projection."""
    source = doc['text']
    content = row['content']
    start = source.find(content)
    if start >= 0:
        if source.find(content, start + 1) >= 0:
            raise KnowledgeError('provenance_error')
        return start, content, 'exact'

    if (doc.get('source_type') != 'project-authored' or row.get('process_source') != 'Unstructured'
            or row.get('source_type') != 'minio'):
        raise KnowledgeError('provenance_error')
    lines = source.split('\n', 2)
    header = PROJECT_TEXT_HEADER.fullmatch(lines[1]) if len(lines) == 3 else None
    if header is None:
        raise KnowledgeError('provenance_error')

    # On the pinned Nexent parser, the verified project TXT assets form one
    # chunk. Only its known metadata separators and line breaks are changed.
    for field in ('license', 'date'):
        left, right = header.span(field)
        lines[1] = lines[1][:left] + header[field].replace('-', '\n\n') + lines[1][right:]
    projected = re.sub(r'(?<!\n)\n(?!\n)', '\n\n', '\n'.join(lines)).strip('\n')
    if content != projected:
        raise KnowledgeError('provenance_error')
    return 0, source, 'nexent-v2.8.3-unstructured-project-text-v1'


class KnowledgeClient:
    def __init__(self,store,*,api=None,index_id='',role='',manifest=MANIFEST):
        self.store=store;self.api=api;self.index=index_id;self.role=role;self.manifest=manifest

    def require_config(self):
        if self.api is None or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,254}',self.index):
            raise KnowledgeError('configuration_error')
        self.store.bind_knowledge_endpoint(self.index,self.api.identity)

    @classmethod
    def from_env(cls,store):
        base=os.environ.get('TOPIC16_KB_BASE_URL','');token=os.environ.get('TOPIC16_KB_API_TOKEN','')
        try:api=NativeAPI(base,token) if base and token else None
        except KnowledgeError:api=None
        return cls(store,api=api,index_id=os.environ.get('TOPIC16_KB_INDEX',''),
                   role=os.environ.get('TOPIC16_KB_ROLE',''))

    def retrieve(self,*,run_id,task_key,query):
        if any(not isinstance(v,str) or not v.strip() for v in (run_id,task_key,query)):
            raise ValueError('run_id/task_key/query 须为非空字符串')
        started=time.monotonic()
        record={'retrieval_id':'local-'+uuid.uuid4().hex,'id_origin':'project-local-correlation',
                'native_call_id':None,'run_id':run_id,'task_key':task_key,'query':query,
                'started_at':datetime.now(timezone.utc).isoformat(),
                'scope':self.api.scope if self.api else 'configuration-check',
                'status':'configuration_error','citations':[], 'document_instructions_trusted':False}
        try:
            manifest,assets=load_assets(self.manifest)
            if not self.role:raise KnowledgeError('configuration_error')
            if self.role not in manifest['query_roles']:raise KnowledgeError('permission_denied')
            self.require_config()
            imports=self.store.knowledge_imports(self.index)
            by_path={r['native_path']:r for r in imports if r['status']=='verified' and r['native_path']}
            if not by_path:raise KnowledgeError('configuration_error')
            response=self.api.request('POST','/nb/v1/knowledge/indices/search/hybrid',
                payload={'query':query,'index_names':[self.index],'top_k':10,'weight_accurate':0.7})
            record['response_sha256']=digest(json.dumps(response,sort_keys=True,ensure_ascii=False).encode())
            rows=response.get('results')
            if not isinstance(rows,list) or response.get('total')!=len(rows):raise KnowledgeError('service_error')
            citations=[];terms=query_terms(query);rejected=0;verified_paths=set()
            for row in rows:
                if not isinstance(row,dict):raise KnowledgeError('provenance_error')
                binding=by_path.get(row.get('path_or_url'))
                if binding is None or row.get('index')!=self.index:raise KnowledgeError('provenance_error')
                doc=assets.get((binding['doc_id'],binding['version']))
                if doc is None or doc['sha256']!=binding['sha256']:raise KnowledgeError('provenance_error')
                if self.role not in doc['visible_roles']:raise KnowledgeError('permission_denied')
                if binding['native_path'] not in verified_paths:
                    raw=self.api.request('GET','/nb/v1/knowledge/file/download/'+
                        urllib.parse.quote(binding['native_path'],safe='')+'?download=stream',binary=True)
                    if digest(raw)!=doc['sha256']:raise KnowledgeError('provenance_error')
                    verified_paths.add(binding['native_path'])
                content=row.get('content');chunk=row.get('id')
                if not isinstance(content,str) or not content.strip() or not isinstance(chunk,str) or not chunk:
                    raise KnowledgeError('provenance_error')
                start, quote, alignment = align_source_quote(doc, row)
                score=row.get('score')
                if score is not None and (type(score) not in (int,float) or not math.isfinite(score)):
                    raise KnowledgeError('provenance_error')
                # Explicit conservative relevance filter, separate from native score.
                matched=sum(t in content.lower() for t in terms)
                if not terms or matched/max(1,len(terms))<0.35:
                    rejected+=1;continue
                citations.append({'knowledge_base_id':self.index,'doc_id':doc['doc_id'],'version':doc['version'],
                    'title':doc['title'],'document_sha256':doc['sha256'],'chunk_id':chunk,'quote':quote,
                    'chunk_sha256':digest(content.encode()),'source_alignment':alignment,
                    'quote_sha256':digest(quote.encode()),'start_char':start,'end_char':start+len(quote),
                    'start_line':doc['text'][:start].count('\n')+1,
                    'end_line':doc['text'][:start+len(quote)].count('\n')+1,'score':score,
                    'source':doc['source'],'license':doc['license']})
            record.update(status='hit' if citations else 'no_match',citations=citations,
                          native_result_count=len(rows),relevance_rejected=rejected,
                          relevance_policy='unique Chinese bigrams / English tokens; coverage >= 0.35')
        except KnowledgeError as exc:
            record.update(status=exc.status,citations=[])
            if exc.http_status is not None:record['http_status']=exc.http_status
        except (KeyError,TypeError,ValueError,OSError):
            record.update(status='provenance_error',citations=[])
        record['duration_ms']=round((time.monotonic()-started)*1000,3)
        self.store.save_knowledge(record)
        return record

    def import_asset(self,doc):
        if self.role!='asset_admin':raise KnowledgeError('permission_denied')
        self.require_config()
        filename=doc['doc_id']+'-'+doc['version']+'-'+doc['sha256'][:12]+'.txt'
        claimed,old=self.store.knowledge_claim_import(self.index,doc,filename)
        if not claimed:
            # Never re-upload uncertain attempts. Reconcile only against the recorded native path.
            if old['native_path']:return self.verify_import(doc,old['native_path'])
            return {'doc_id':doc['doc_id'],'status':old['status'],'uploaded':False}
        try:
            boundary='topic16-'+uuid.uuid4().hex
            body=(f'--{boundary}\r\nContent-Disposition: form-data; name="index_name"\r\n\r\n{self.index}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  'Content-Type: text/plain; charset=utf-8\r\n\r\n').encode()+doc['bytes']+f'\r\n--{boundary}--\r\n'.encode()
            response=self.api.request('POST','/nb/v1/knowledge/file/upload',body=body,
                                      content_type='multipart/form-data; boundary='+boundary)
            paths=response.get('uploaded_file_paths')
            if response.get('errors') or not isinstance(paths,list) or len(paths)!=1 or not isinstance(paths[0],str):
                raise KnowledgeError('service_error')
            self.store.knowledge_import_finish(self.index,doc,'uploaded',paths[0])
            result=self.verify_import(doc,paths[0]);result['uploaded']=True;return result
        except KnowledgeError as exc:
            self.store.knowledge_import_finish(self.index,doc,'unknown')
            return {'doc_id':doc['doc_id'],'status':exc.status,'uploaded':None,'retry_allowed':False}

    def verify_import(self,doc,path):
        raw=self.api.request('GET','/nb/v1/knowledge/file/download/'+urllib.parse.quote(path,safe='')+'?download=stream',binary=True)
        if digest(raw)!=doc['sha256']:
            self.store.knowledge_import_finish(self.index,doc,'provenance_error')
            raise KnowledgeError('provenance_error')
        chunks=self.api.request('POST','/nb/v1/knowledge/indices/'+urllib.parse.quote(self.index,safe='')+
            '/chunks?'+urllib.parse.urlencode({'path_or_url':path,'page':1,'page_size':1}))
        if chunks.get('status')!='success' or not isinstance(chunks.get('chunks'),list):
            raise KnowledgeError('service_error')
        status='verified' if chunks['chunks'] else 'indexing_pending'
        self.store.knowledge_import_finish(self.index,doc,status,path)
        return {'doc_id':doc['doc_id'],'version':doc['version'],'status':status,'uploaded':False}
