#!/usr/bin/env python
"""复现 execution_key 联调边界的「修复前 / 修复后」对照证据。

用法（从项目目录执行）：

    python eval/nexent/reproduce_execution_key.py

- 修复前代码从本仓库历史提交 ``0ef85e5`` 的原样源码在内存中加载为独立模块；
- 修复后代码就是工作区当前的 ``eval/nexent/nexent_client.py``；
- 两组输入完全相同：SSE 只给「工具调用 + execution_key」，另有可查到的执行回执；
- 全部为本地构造的 SSE 事件与回执，不接触真实 Nexent、不读取或写入任何凭据；
- 输出 ``eval/results/execution_key_contract_before_fix.json`` 与
  ``eval/results/execution_key_contract_after_fix.json``。

⚠️ 两份输出都是本地 mock 对照，``platform_evidence = false``，不是平台实跑证据。
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
REPO = PROJECT.parents[2]
RESULTS = PROJECT / "eval" / "results"
sys.path.insert(0, str(HERE))

import nexent_client as current  # noqa: E402
import run_nexent_eval as driver  # noqa: E402

OLD_COMMIT = "0ef85e5"
CLIENT_REL = (HERE / "nexent_client.py").resolve().relative_to(REPO).as_posix()
DRIVER_REL = (HERE / "run_nexent_eval.py").resolve().relative_to(REPO).as_posix()

TOOL = current.TOOL_NAME
RUN_ID = "RUN-CONTRACT-LOCAL"
TASK_KEY = "P-STOCK|2026-11-09T10:00:00+08:00"
EXEC_KEY = "EX-7788"
PLATFORM_IDS = {"conversation_id": "C-1", "x_request_id": "XR-1",
                "request_id": "XR-1", "session_id": "C-1"}

PLANNED_ARGS = {
    "portfolio": {
        "id": "P-STOCK",
        "holdings": [{"symbol": "SIM-A", "stop_loss": "95.00",
                      "take_profit": "106.00"}],
    },
    "snapshot": {"timestamp": "2026-11-09T10:00:00+08:00",
                 "prices": {"SIM-A": "95.00"}},
}
OTHER_ARGS = {
    "portfolio": {
        "id": "P-BOND",
        "holdings": [{"symbol": "SIM-C", "stop_loss": "1.80",
                      "take_profit": "2.10"}],
    },
    "snapshot": {"timestamp": "2026-11-09T11:00:00+08:00",
                 "prices": {"SIM-C": "1.90"}},
}
SIGNAL = {
    "event_id": "a" * 64,
    "portfolio_id": "P-STOCK",
    "timestamp": "2026-11-09T02:00:00Z",
    "symbol": "SIM-A",
    "rule": "stop_loss",
    "price": "95.00",
    "threshold": "95.00",
}


def git_show(commit: str, rel: str) -> str:
    """从本仓库历史取原样源码；浅克隆无该提交时明确报错，不做兜底猜测。"""
    done = subprocess.run(
        ["git", "show", f"{commit}:{rel}"], cwd=REPO,
        capture_output=True, check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"无法从 {commit} 读取 {rel}；请在完整克隆的过程仓中运行本脚本。"
            f" git: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8")


def load_old_module() -> types.ModuleType:
    module = types.ModuleType("nexent_client_at_0ef85e5")
    module.__file__ = f"<git:{OLD_COMMIT}:{CLIENT_REL}>"
    # @dataclass 会通过 sys.modules[cls.__module__] 解析注解，必须登记后再 exec。
    sys.modules[module.__name__] = module
    try:
        exec(compile(git_show(OLD_COMMIT, CLIENT_REL), module.__file__, "exec"),
             module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


def sse(*events) -> str:
    chunks = []
    for name, payload in events:
        chunks.append(f"event: {name}\n")
        chunks.append("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n")
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


def events_from(text: str, module=current) -> list[dict]:
    return module.parse_sse_events(text.splitlines(keepends=True))


EXECUTION_KEY_ONLY_SSE = sse(
    ("conversation_created", {
        "type": "conversation_created",
        "content": json.dumps({"conversation_id": "C-1"}),
    }),
    ("tool_call", {"type": "tool_call", "tool_name": TOOL,
                   "tool_call_id": "TC-1", "execution_key": EXEC_KEY,
                   "arguments": PLANNED_ARGS}),
    ("execution_started", {"type": "execution_started",
                           "execution_key": EXEC_KEY}),
)

# SSE 已给出显式工具结果，但没有可与结果配对的同次调用标识。
SSE_WITH_UNPAIRED_RESULT = sse(
    ("conversation_created", {
        "type": "conversation_created",
        "content": json.dumps({"conversation_id": "C-1"}),
    }),
    ("tool_call", {"type": "tool_call", "tool_name": TOOL,
                   "tool_call_id": "TC-1", "execution_key": EXEC_KEY,
                   "arguments": PLANNED_ARGS}),
    ("tool_result", {"type": "tool_result", "tool_name": TOOL,
                     "tool_call_id": "TC-OTHER", "execution_key": EXEC_KEY,
                     "structuredContent": {"result": [SIGNAL]}}),
)


def receipt(**overrides) -> dict:
    entry = {
        "schema": "topic16-execution-record-v1",
        "source": "mcp-server-receipt",
        "tool_name": TOOL,
        "execution_key": EXEC_KEY,
        "arguments": PLANNED_ARGS,
        "result": [SIGNAL],
        "tool_success": True,
        "recorded_at": "2026-11-09T02:00:00Z",
    }
    entry.update(overrides)
    return entry


def mcp_entry(**overrides) -> dict:
    """MCP 服务端审计记录：有结果，但没有任何 execution_key 可关联。"""
    entry = {
        "schema": "topic16-mcp-call-log-v1",
        "source": "mcp-server-log",
        "tool_name": TOOL,
        "call_id": "TC-1",
        "conversation_id": "C-1",
        "arguments": PLANNED_ARGS,
        "result": [SIGNAL],
        "recorded_at": "2026-11-09T02:00:00Z",
    }
    entry.update(overrides)
    return entry


def mcp_receipt(**overrides) -> dict:
    """可唯一关联到该次调用的 MCP 服务端回执：只靠 SSE 工具调用标识。"""
    entry = {
        "schema": "topic16-mcp-call-log-v1",
        "source": "mcp-server-log",
        "tool_name": TOOL,
        "call_id": "TC-1",
        "conversation_id": "C-1",
        "arguments": PLANNED_ARGS,
        "result": [SIGNAL],
        "tool_success": True,
        "recorded_at": "2026-11-09T02:00:00Z",
    }
    entry.update(overrides)
    return entry


def resolve(module, events, *, mcp=None, executions=None, planned=None):
    kwargs = {"platform_ids": PLATFORM_IDS, "run_id": RUN_ID,
              "task_key": TASK_KEY}
    if mcp is not None:
        kwargs["mcp_call_log"] = mcp
    if executions is not None:
        kwargs["execution_records"] = executions
    return module.resolve_tool_evidence(
        events, module.DEFAULT_TOOL_EVENT_TYPES,
        PLANNED_ARGS if planned is None else planned, **kwargs)


def brief(result: dict) -> dict:
    evidence = result.get("evidence") or {}
    signals = evidence.get("signals")
    return {
        "status": result["status"],
        "signal_count": None if signals is None else len(signals),
        "zero_hit": evidence.get("zero_hit"),
        "pairing": evidence.get("pairing"),
        "execution_key": evidence.get("execution_key"),
        "actual_arguments_source": evidence.get("actual_arguments_source"),
        "gaps": result.get("gaps", []),
    }


def write_json(name: str, payload: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    target = RESULTS / name
    target.write_bytes(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        .encode("utf-8"))
    print(f"  -> {target.relative_to(PROJECT).as_posix()}")


def gate_probe() -> dict:
    with TemporaryDirectory() as folder:
        path = Path(folder) / "nexent_preflight.json"
        ok_missing, why_missing = driver.smoke_preflight_gate(path, RUN_ID)
        doc = {
            "schema": "topic16-northbound-preflight-v2",
            "run_id": RUN_ID, "scope": "northbound-preflight",
            "platform_evidence": True, "pass": True,
            "hit_task_found": True, "zero_hit_task_found": True,
        }
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        ok_valid, why_valid = driver.smoke_preflight_gate(path, RUN_ID)
        doc["zero_hit_task_found"] = False
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        ok_no_zero, why_no_zero = driver.smoke_preflight_gate(path, RUN_ID)
        return {
            "missing_preflight": {"allowed": ok_missing, "problems": why_missing},
            "valid_preflight_same_run_id": {"allowed": ok_valid,
                                            "problems": why_valid},
            "preflight_without_zero_hit": {"allowed": ok_no_zero,
                                           "problems": why_no_zero},
            "different_run_id": {
                "allowed": driver.smoke_preflight_gate(path, "RUN-OTHER")[0]},
            "full_mode_exit_code_when_gate_denied": 4,
        }


def main() -> int:
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = load_old_module()
    old_driver_src = git_show(OLD_COMMIT, DRIVER_REL)
    events = events_from(EXECUTION_KEY_ONLY_SSE)
    sse_keys = current.collect_execution_keys(events)
    good_receipt = receipt()

    # ---- 修复前：同一组输入，旧客户端只能保守返回 insufficient ----
    before = resolve(old, events_from(EXECUTION_KEY_ONLY_SSE, old),
                     mcp=[mcp_entry()])
    before_doc = {
        "schema": "topic16-execution-key-pre-fix-reproduction-v1",
        "scope": "pre-fix-reproduction",
        "platform_evidence": False,
        "checked_at": checked_at,
        "baseline_commit": OLD_COMMIT,
        "note": "从 0ef85e5 原样源码加载旧客户端，与当前工作区客户端使用同一组本地构造的 "
                "SSE 事件和执行回执。全部为本地 mock，不是真实 Nexent 平台记录。",
        "input": {
            "sse_shape": "tool_call(execution_key=EX-7788) + execution_started；SSE 无结构化工具结果",
            "execution_key": EXEC_KEY,
            "mcp_server_log_present": True,
            "mcp_log_contains_tool_result": True,
            "execution_receipt_available": True,
        },
        "observed": brief(before),
        "full_mode_gate": {
            "smoke_preflight_gate_in_driver": "smoke_preflight_gate" in old_driver_src,
            "note": "旧驱动没有 smoke 预检门禁：--mode full 可直接跑全量 24 任务，"
                    "不需要先交付一条命中与一条零命中的可追溯记录。",
        },
        "assessment": "旧客户端保持 insufficient 是正确且必要的保守行为（本地日志里有结果"
                      "但无法唯一关联到该次调用，不能误报成功）；缺的是按 SSE execution_key "
                      "查唯一执行记录的补证通路，以及暂缓全量的 smoke 预检门禁。",
    }

    # ---- 修复后：唯一回执可补证；各类不满足条件一律保留 insufficient ----
    def case(label, ev, **kwargs):
        return label, brief(resolve(current, ev, **kwargs))

    positives = dict([
        case("unique_hit_receipt", events, executions=[good_receipt]),
        case("unique_zero_hit_receipt", events,
             executions=[receipt(result=[])]),
        case("mcp_receipt_linked_by_tool_call_id", events,
             executions=[mcp_receipt()]),
    ])
    negatives = dict([
        case("no_execution_records", events, executions=[]),
        case("unrelated_mcp_log_only", events, mcp=[mcp_entry()]),
        case("ambiguous_records", events,
             executions=[good_receipt, receipt(recorded_at="later")]),
        case("missing_success_marker", events,
             executions=[receipt(tool_success=False)]),
        case("error_marker", events,
             executions=[receipt(error="timeout")]),
        case("wrong_actual_arguments", events,
             executions=[receipt(arguments=OTHER_ARGS)]),
        case("execution_key_not_from_sse", events,
             executions=[receipt(execution_key="EX-9999")]),
        case("fixture_source_rejected", events,
             executions=[receipt(source="local-fixture")]),
        case("receipt_conflicts_with_sse_result",
             events_from(SSE_WITH_UNPAIRED_RESULT),
             executions=[receipt(result=[])]),
        case("call_id_receipt_with_unrelated_call_id", events,
             executions=[mcp_receipt(call_id="TC-OTHER")]),
        case("call_id_receipt_without_success_marker", events,
             executions=[mcp_receipt(tool_success=None)]),
        case("call_id_receipt_with_timeout_marker", events,
             executions=[mcp_receipt(timeout="MCP timeout")]),
        case("ambiguous_call_id_receipts", events,
             executions=[mcp_receipt(), mcp_receipt(recorded_at="later")]),
    ])
    after_doc = {
        "schema": "topic16-execution-key-post-fix-verification-v1",
        "scope": "post-fix-reproduction",
        "platform_evidence": False,
        "checked_at": checked_at,
        "note": "当前工作区客户端的本地 mock 实测。SSE 显式 execution_key 可查唯一执行记录"
                "补证，SSE 工具调用标识可查唯一 MCP 服务端回执补证；无法唯一关联、"
                "缺成功标记、错误标记、入参不一致、键不来自 SSE、fixture 来源或与 SSE "
                "结果冲突，一律保留 insufficient。",
        "execution_keys_collected_from_sse": sse_keys,
        "tool_call_ids_collected_from_sse": current.collect_tool_call_ids(events),
        "positive": positives,
        "negative": negatives,
        "preflight_contract": {
            "schema": "topic16-northbound-preflight-v2",
            "required_fields": ["run_id", "platform_evidence", "pass",
                                "hit_task_found", "zero_hit_task_found"],
            "per_record_checks": [
                "平台请求/会话 ID", "实际工具入参与同次调用配对",
                "成功返回值与 SQLite 查询结果一致",
                "执行记录补证键（execution_key 或 tool_call_id/call_id）来自本次 SSE",
                "零命中由工具成功 + 显式 [] + 配对共同证明",
            ],
        },
        "full_mode_gate": gate_probe(),
    }

    write_json("execution_key_contract_before_fix.json", before_doc)
    write_json("execution_key_contract_after_fix.json", after_doc)

    # 轻量自检：正例必须 verified，全部反例必须 insufficient/failed。
    bad = [k for k, v in positives.items() if v["status"] != "verified"]
    bad += [k for k, v in negatives.items() if v["status"] == "verified"]
    if positives["unique_hit_receipt"]["signal_count"] != 1 \
            or positives["unique_zero_hit_receipt"]["signal_count"] != 0:
        bad.append("positive_signal_count")
    if bad:
        print("复现自检失败：", ", ".join(bad))
        return 1
    print(f"复现自检通过：{len(positives)} 个正例 verified，"
          f"{len(negatives)} 个反例均被拒绝；SSE 执行键 {sse_keys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
