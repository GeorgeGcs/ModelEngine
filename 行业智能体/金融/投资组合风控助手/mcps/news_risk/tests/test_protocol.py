"""Real SDK request handler; mocked transport timeout is not a live-platform test."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from mcp_types import CallToolRequestParams
from mcps.news_risk.server import server


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def params(self,text):
        return CallToolRequestParams(name='assess_news_risk_v2',arguments={
            'news':{'symbol':'X','text':text},'entities':{'X':{'canonical':'松野材料'}}})
    async def test_live_sdk_handler_success_and_no_risk_are_objects(self):
        for text,verdict in (('松野材料收到行政处罚决定书。','risk'),('松野材料未收到行政处罚。','no_risk')):
            wire=(await server._handle_call_tool(None,self.params(text))).model_dump(mode='json',by_alias=True)
            self.assertIs(wire['isError'],False)
            self.assertEqual(json.loads(wire['content'][0]['text']),wire['structuredContent']['result'])
            self.assertEqual(wire['structuredContent']['result']['risk_verdict'],verdict)
    async def test_outer_acceptance_can_carry_explicit_error(self):
        wire=(await server._handle_call_tool(None,self.params(''))).model_dump(mode='json',by_alias=True)
        self.assertIs(wire['isError'],True);self.assertFalse(wire.get('structuredContent'))
    async def test_timeout_at_call_boundary_is_error_not_no_risk(self):
        with patch.object(server,'call_tool',new=AsyncMock(side_effect=TimeoutError('synthetic timeout'))):
            wire=(await server._handle_call_tool(None,self.params('任意原文'))).model_dump(mode='json',by_alias=True)
        self.assertIs(wire['isError'],True);self.assertNotIn('risk_verdict',wire['content'][0]['text'])
    async def test_cancellation_is_not_converted_to_success(self):
        with patch.object(server,'call_tool',new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await server._handle_call_tool(None,self.params('任意原文'))
