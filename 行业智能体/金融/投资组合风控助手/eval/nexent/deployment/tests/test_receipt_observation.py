"""Unit regressions for the shipped observation patch, not platform evidence.

The dependency-free tests execute the actual added capture methods from the
hash-pinned patch. SDK integration tests also run the actual patched wrapper
when an observed Nexent v2.8.3 SDK is installed. They use isolated RPC doubles;
they never contact a model, Agent, MCP service, or evaluation database.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from copy import deepcopy
import hashlib
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest


PACKAGE = Path(__file__).resolve().parents[1]
TOOL = "evaluate_portfolio_snapshot"
UNIT_ARGS = {"portfolio": {"id": "UNIT-ONLY"}, "snapshot": {"timestamp": "UNIT-ONLY"}}


def added_block(patch, prefix):
    """Read a complete added definition without keeping platform source copies."""
    lines = patch.splitlines(keepends=True)
    begin = next(i for i, line in enumerate(lines) if line.rstrip("\r\n") == "+" + prefix)
    block = []
    for line in lines[begin:]:
        if not line.startswith("+") or line.startswith("+++"):
            break
        block.append(line[1:])
    return textwrap.dedent("".join(block))


def receipt_namespace():
    manifest = json.loads((PACKAGE / "observation-manifest.json").read_text(encoding="utf-8"))
    raw = (PACKAGE / manifest["patch_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["patch_sha256"]:
        raise AssertionError("Observation patch differs from its manifest")
    patch = raw.decode("utf-8")
    namespace = {
        "ContextVar": ContextVar, "deepcopy": deepcopy, "threading": threading,
        "logger": logging.getLogger("topic16.observation.unit"),
    }
    exec(compile(added_block(patch, "# Optional diagnostic capture. No sink is configured by the SDK itself."),
                 "sdk-observation.patch:capture", "exec"), namespace)
    exec(compile(added_block(patch, "    def _new_receipt_capture(self):"),
                 "sdk-observation.patch:publication", "exec"), namespace)
    return namespace


NS = receipt_namespace()
Capture = NS["_MCPReceiptCapture"]


def capture(raw, call_id="UNIT-ONLY-CALL", accepted=True, error_type=None):
    current = Capture(call_id)
    current.begin(TOOL, UNIT_ARGS)
    current.returned(raw)
    return current.finish(accepted=accepted, error_type=error_type)


class CaptureRegressions(unittest.TestCase):
    def test_hit_and_explicit_empty_array_keep_actual_result(self):
        for result in [[{"event_id": "UNIT-ONLY-EVENT"}], []]:
            with self.subTest(result=result):
                wire = {"isError": False, "structuredContent": {"result": result}}
                record = capture(wire)
                self.assertTrue(record["tool_success"])
                self.assertEqual(record["status"], "success")
                self.assertEqual(record["result"], result)
                self.assertEqual(record["mcp_raw_result"], wire)
                self.assertEqual(record["arguments"], UNIT_ARGS)

    def test_default_false_is_not_an_explicit_wire_success(self):
        class DefaultOnly:
            isError = False

            def model_dump(self, **options):
                self.options = options
                return {"structuredContent": {"result": []}}

        model = DefaultOnly()
        record = capture(model)
        self.assertFalse(record["tool_success"])
        self.assertEqual(record["status"], "insufficient")
        self.assertEqual(model.options, {"mode": "json", "by_alias": True, "exclude_unset": True})

    def test_missing_array_or_call_id_does_not_manufacture_zero_hit(self):
        for wire, call_id in [({"isError": False}, "UNIT-ONLY-CALL"),
                              ({"isError": False, "structuredContent": {"result": None}}, "UNIT-ONLY-CALL"),
                              ({"isError": False, "structuredContent": {"result": []}}, None)]:
            with self.subTest(wire=wire, call_id=call_id):
                record = capture(wire, call_id=call_id)
                self.assertFalse(record["tool_success"])
                self.assertEqual(record["status"], "insufficient")
                if not isinstance(wire.get("structuredContent", {}).get("result"), list):
                    self.assertNotIn("result", record)

    def test_explicit_error_and_outer_failure_reject_empty_array(self):
        for wire, accepted, error in [
            ({"isError": True, "structuredContent": {"result": []}}, True, None),
            ({"isError": False, "structuredContent": {"result": []}}, False, "ValueError"),
            ({"isError": False, "structuredContent": {"result": []}}, False, "TimeoutError"),
            ({"isError": False, "structuredContent": {"result": []}}, False, "RunCancelled"),
        ]:
            with self.subTest(accepted=accepted, error=error, wire=wire):
                record = capture(wire, accepted=accepted, error_type=error)
                self.assertFalse(record["tool_success"])
                self.assertEqual(record["status"], "failed")

    def test_late_result_cannot_upgrade_a_timed_out_receipt(self):
        current = Capture("UNIT-ONLY-LATE")
        current.begin(TOOL, UNIT_ARGS)
        record = current.finish(accepted=False, error_type="TimeoutError")
        current.returned({"isError": False, "structuredContent": {"result": []}})
        self.assertIsNone(current.finish(accepted=True))
        self.assertFalse(record["tool_success"])
        self.assertNotIn("result", record)

    def test_more_than_one_rpc_is_not_a_unique_success(self):
        current = Capture("UNIT-ONLY-MULTIPLE")
        for _ in range(2):
            current.begin(TOOL, UNIT_ARGS)
            current.returned({"isError": False, "structuredContent": {"result": []}})
        record = current.finish(accepted=True)
        self.assertFalse(record["tool_success"])
        self.assertEqual(record["rpc_count"], 2)
        self.assertEqual(record["status"], "insufficient")

    def test_arguments_and_raw_result_are_snapshots(self):
        arguments = deepcopy(UNIT_ARGS)
        raw = {"isError": False, "structuredContent": {"result": []}}
        current = Capture("UNIT-ONLY-SNAPSHOT")
        current.begin(TOOL, arguments)
        current.returned(raw)
        arguments["portfolio"]["id"] = "MODIFIED-AFTER-CALL"
        raw["structuredContent"]["result"].append({"event_id": "MODIFIED-AFTER-CALL"})
        record = current.finish(accepted=True)
        self.assertEqual(record["arguments"], UNIT_ARGS)
        self.assertEqual(record["result"], [])

    def test_reader_uses_current_context_and_default_sink_is_off(self):
        call_id = ContextVar("unit_call_id", default=None)
        owner = SimpleNamespace(_receipt_sink=lambda _: None, _tool_call_id_reader=call_id.get)
        token = call_id.set("UNIT-ONLY-CONTEXT")
        try:
            current = NS["_new_receipt_capture"](owner)
        finally:
            call_id.reset(token)
        current.begin(TOOL, UNIT_ARGS)
        current.returned({"isError": False, "structuredContent": {"result": []}})
        self.assertEqual(current.finish(accepted=True)["call_id"], "UNIT-ONLY-CONTEXT")
        owner._receipt_sink = None
        owner._tool_call_id_reader = lambda: self.fail("Disabled observation must not read the call ID")
        self.assertIsNone(NS["_new_receipt_capture"](owner))

    def test_publication_failure_is_diagnostic_only(self):
        def unavailable(_record):
            raise PermissionError("unit-only private detail must not appear in logs")

        owner = SimpleNamespace(_receipt_sink=unavailable)
        current = Capture("UNIT-ONLY-WRITER")
        current.begin(TOOL, UNIT_ARGS)
        current.returned({"isError": False, "structuredContent": {"result": []}})
        with self.assertLogs("topic16.observation.unit", level="WARNING") as logs:
            self.assertIsNone(NS["_publish_receipt"](owner, current, accepted=True))
        self.assertIn("PermissionError", " ".join(logs.output))
        self.assertNotIn("unit-only private detail", " ".join(logs.output))


try:
    from nexent.core.agents.managed_mcp import ManagedMCPToolCollection, MCPToolTimeoutError
    SDK_AVAILABLE = "receipt_sink" in inspect.signature(ManagedMCPToolCollection).parameters
except ImportError:
    SDK_AVAILABLE = False

if os.environ.get("NEXENT_OBSERVATION_REQUIRED") == "1" and not SDK_AVAILABLE:
    raise RuntimeError("An observed Nexent v2.8.3 SDK is required for the runtime regression command")


class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def close(self):
        async def cancel_pending():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(cancel_pending(), self.loop).result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


class IsolatedManager:
    """Only the SDK manager interface is doubled; the patched wrapper is real."""
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.cancelled = []

    def submit(self, _pool, _spec, function, *args, **kwargs):
        context = copy_context()
        future = self.executor.submit(context.run, function, *args, **kwargs)
        return SimpleNamespace(execution_id="UNIT-ONLY-EXECUTION", future=future)

    def cancel(self, execution_id, **options):
        self.cancelled.append((execution_id, options))


@unittest.skipUnless(SDK_AVAILABLE, "Patched Nexent SDK is not installed; run the required runtime command separately")
class ManagedWrapperRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest = json.loads((PACKAGE / "observation-manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["sdk_files"]:
            name = Path(entry["path"]).stem
            module = __import__("nexent.core.agents." + name, fromlist=[name])
            actual = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            if actual != entry["candidate_sha256"]:
                raise AssertionError("Installed SDK source differs from the shipped observation version")

    def setUp(self):
        self.loop = LoopThread()
        self.manager = IsolatedManager()
        self.scope = SimpleNamespace(cancelled=False)
        self.records = []
        self.context_id = ContextVar("unit_native_tool_call_id", default=None)
        self.collection = ManagedMCPToolCollection(
            manager=self.manager, server_parameters=[], cancellation_scope=self.scope,
            tool_timeout_seconds=0.2, close_timeout_seconds=0.1,
            tool_call_id_reader=self.context_id.get, receipt_sink=self.records.append,
        )
        self.collection._loop = self.loop.loop

    def tearDown(self):
        self.collection.request_close()
        self.manager.executor.shutdown(wait=True)
        self.loop.close()

    def invoke(self, coroutine):
        session = SimpleNamespace(call_tool=coroutine)
        tool = SimpleNamespace(name=TOOL, forward=lambda: self.collection._call_tool(session, TOOL, UNIT_ARGS))
        wrapped = self.collection._wrap_tools([tool])[0]
        token = self.context_id.set("UNIT-ONLY-WRAPPED-CALL")
        try:
            return wrapped.forward()
        finally:
            self.context_id.reset(token)

    def test_original_result_and_context_id_are_preserved(self):
        raw = {"isError": False, "structuredContent": {"result": []}}
        async def rpc(_name, _arguments):
            return raw

        self.assertIs(self.invoke(rpc), raw)
        self.assertEqual(len(self.records), 1)
        self.assertTrue(self.records[0]["tool_success"])
        self.assertEqual(self.records[0]["call_id"], "UNIT-ONLY-WRAPPED-CALL")

    def test_actual_rpc_exception_is_rethrown_and_never_zero_hit(self):
        failure = ValueError("UNIT-ONLY RPC FAILURE")
        async def rpc(_name, _arguments):
            raise failure

        with self.assertRaises(ValueError) as raised:
            self.invoke(rpc)
        self.assertIs(raised.exception, failure)
        self.assertFalse(self.records[0]["tool_success"])
        self.assertEqual(self.records[0]["status"], "failed")
        self.assertNotIn("result", self.records[0])

    def test_timeout_and_late_rpc_never_publish_success(self):
        self.collection.tool_timeout_seconds = 0.02
        async def rpc(_name, _arguments):
            try:
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                await asyncio.sleep(0.03)
            return {"isError": False, "structuredContent": {"result": []}}

        with self.assertRaises(MCPToolTimeoutError):
            self.invoke(rpc)
        time.sleep(0.07)
        self.assertEqual(len(self.records), 1)
        self.assertFalse(self.records[0]["tool_success"])
        self.assertEqual(self.records[0]["error_type"], "TimeoutError")
        self.assertTrue(self.manager.cancelled)

    def test_cancellation_after_raw_result_does_not_claim_success(self):
        raw = {"isError": False, "structuredContent": {"result": []}}
        async def rpc(_name, _arguments):
            self.scope.cancelled = True
            return raw

        self.assertIs(self.invoke(rpc), raw)
        self.assertFalse(self.records[0]["tool_success"])
        self.assertEqual(self.records[0]["error_type"], "RunCancelled")

    def test_sink_failure_keeps_actual_tool_return(self):
        raw = {"isError": False, "structuredContent": {"result": []}}
        async def rpc(_name, _arguments):
            return raw
        def failed_sink(_record):
            raise OSError("UNIT-ONLY DISK FAILURE")

        self.collection._receipt_sink = failed_sink
        with self.assertLogs("managed_mcp", level="WARNING"):
            self.assertIs(self.invoke(rpc), raw)
        self.assertEqual(self.records, [])


@unittest.skipUnless(os.name == "posix", "Private sink uses the Linux runtime file-lock API")
class PrivateSinkRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = PACKAGE / "startup" / "topic16_receipt_sink.py"
        spec = importlib.util.spec_from_file_location("topic16_private_sink_unit", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_private_append_retains_explicit_empty_array(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = self.module.PrivateJSONLReceiptSink(directory, "UNIT-ONLY")
            receipt = capture({"isError": False, "structuredContent": {"result": []}})
            sink(receipt)
            path = Path(directory) / "UNIT-ONLY.execution_records.jsonl"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(stored["tool_success"])
            self.assertEqual(stored["result"], [])

    def test_existing_non_private_file_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "UNIT-ONLY.execution_records.jsonl"
            path.write_text("UNIT-ONLY ORIGINAL", encoding="utf-8")
            path.chmod(0o644)
            sink = self.module.PrivateJSONLReceiptSink(directory, "UNIT-ONLY")
            with self.assertRaises(PermissionError):
                sink(capture({"isError": False, "structuredContent": {"result": []}}))
            self.assertEqual(path.read_text(), "UNIT-ONLY ORIGINAL")

    def test_filename_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unit-target"
            target.write_text("UNIT-ONLY ORIGINAL", encoding="utf-8")
            (Path(directory) / "UNIT-ONLY.execution_records.jsonl").symlink_to(target)
            sink = self.module.PrivateJSONLReceiptSink(directory, "UNIT-ONLY")
            with self.assertRaises(OSError):
                sink(capture({"isError": False, "structuredContent": {"result": []}}))
            self.assertEqual(target.read_text(), "UNIT-ONLY ORIGINAL")


if __name__ == "__main__":
    unittest.main()
