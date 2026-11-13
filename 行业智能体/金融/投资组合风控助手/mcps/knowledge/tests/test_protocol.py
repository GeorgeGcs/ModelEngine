import json
import unittest
from unittest.mock import patch,AsyncMock
import asyncio
from mcp_types import CallToolRequestParams
from mcps.knowledge.server import server


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def params(self,**args):
        return CallToolRequestParams(name='search_portfolio_knowledge',arguments=
            dict(query='止损阈值',run_id='unit-run',task_key='task',**args))
    async def call(self):
        return (await server._handle_call_tool(None,self.params())).model_dump(mode='json',by_alias=True)
    async def test_success_and_no_match_objects(self):
        for status in ('hit','no_match'):
            with patch.dict('os.environ',{'TOPIC16_KB_DB':'unused'}),patch('mcps.knowledge.server.Store'),patch('mcps.knowledge.server.KnowledgeClient.from_env') as create:
                create.return_value.retrieve.return_value={'status':status,'citations':[]}
                wire=await self.call()
                self.assertIs(wire['isError'],False);self.assertEqual(json.loads(wire['content'][0]['text']),wire['structuredContent']['result'])
    async def test_errors_are_explicit_even_when_outer_accepts(self):
        for status in ('permission_denied','timeout','service_error','provenance_error','configuration_error'):
            with patch.dict('os.environ',{'TOPIC16_KB_DB':'unused'}),patch('mcps.knowledge.server.Store'),patch('mcps.knowledge.server.KnowledgeClient.from_env') as create:
                create.return_value.retrieve.return_value={'status':status,'citations':[]}
                wire=await self.call();self.assertIs(wire['isError'],True)
                self.assertEqual(json.loads(wire['content'][0]['text']),wire['structuredContent']['result'])
    async def test_missing_private_db_is_error(self):
        with patch.dict('os.environ',{},clear=True):self.assertTrue((await self.call())['isError'])
    async def test_invalid_input_sdk_rejects(self):
        p=CallToolRequestParams(name='search_portfolio_knowledge',arguments={'query':5,'run_id':'r','task_key':'t'})
        wire=(await server._handle_call_tool(None,p)).model_dump(mode='json',by_alias=True)
        self.assertTrue(wire['isError'])
    async def test_cancel_propagates(self):
        with patch.object(server,'call_tool',new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):await self.call()
