"""Opt-in object receipts on the hash-pinned v2.8.3 price observation patch.

No RPCs are made here. Existing price capture and exception propagation stay intact.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

SDK_SHA256 = 'd64ffb9157d6407398cd481d7af09ddd56abf0b2adef34ebbabf66706fed3d96'
OBJECT_TOOLS = frozenset(('assess_news_risk', 'assess_news_risk_v2', 'search_portfolio_knowledge',
                          'send_wechat_risk_notification'))


def capture_type(base):
    class ObjectCapture(base):
        def finish(self, *, accepted, error_type=None):
            if self._name not in OBJECT_TOOLS:
                return super().finish(accepted=accepted,error_type=error_type)
            with self._lock:
                if self._closed:return None
                self._closed=True
                raw=self._raw
                result=raw.get('structuredContent',{}).get('result') if isinstance(raw,dict) and isinstance(raw.get('structuredContent'),dict) else None
                explicit_ok=isinstance(raw,dict) and raw.get('isError') is False
                explicit_error=isinstance(raw,dict) and raw.get('isError') is True
                text_matches=False
                try:
                    content=raw['content']
                    text_matches=(isinstance(result,dict) and len(content)==1 and content[0]['type']=='text'
                                  and json.loads(content[0]['text'])==result)
                except (KeyError,TypeError,ValueError):pass
                args_ok=isinstance(self._arguments,dict) and isinstance(self._arguments.get('news'),dict)
                if self._name=='assess_news_risk_v2':
                    args_ok=args_ok and isinstance(self._arguments.get('entities'),dict)
                if self._name=='search_portfolio_knowledge':
                    args_ok=isinstance(self._arguments,dict) and all(
                        isinstance(self._arguments.get(k),str) and self._arguments[k].strip()
                        for k in ('query','run_id','task_key'))
                if self._name=='send_wechat_risk_notification':
                    args_ok=isinstance(self._arguments,dict) and all(
                        isinstance(self._arguments.get(k),str) and self._arguments[k].strip()
                        for k in ('event_id','run_id','authorization'))
                success=bool(accepted and not error_type and explicit_ok and text_matches and args_ok
                             and self._call_id and self._requests==1 and not self._capture_error)
                record={'schema':'topic16-platform-mcp-object-receipt-v1','source':'nexent-execution-record',
                    'tool_name':self._name,'call_id':self._call_id,'arguments':deepcopy(self._arguments),
                    'tool_success':success,'status':'success' if success else ('failed' if not accepted or error_type or explicit_error else 'insufficient'),
                    'outer_result_accepted':bool(accepted),'rpc_count':self._requests,
                    'explicit_is_error_false':explicit_ok,'explicit_result_object':isinstance(result,dict),
                    'text_matches_structured':text_matches,'mcp_raw_result':deepcopy(raw)}
                if isinstance(result,dict):record['result']=deepcopy(result)
                if error_type:record['error_type']=error_type
                if self._capture_error:record['capture_error_type']=self._capture_error
                return record
    return ObjectCapture


def install():
    from nexent.core.agents import managed_mcp
    if hashlib.sha256(Path(managed_mcp.__file__).read_bytes()).hexdigest()!=SDK_SHA256:
        raise RuntimeError('Object observation requires the exact patched Nexent v2.8.3 SDK')
    if getattr(managed_mcp,'_topic16_object_capture',False):raise RuntimeError('Object observation already installed')
    managed_mcp._MCPReceiptCapture=capture_type(managed_mcp._MCPReceiptCapture)
    managed_mcp._topic16_object_capture=True
