"""Nexent v2.8.3 北向 API 适配器（真实实例路径）。

接口以目标版本公开的北向 API 为准：
- GET  {prefix}/health     精确验证 {"status":"healthy","service":"northbound-api"}
- GET  {prefix}/agents     官方 data 包装，确认目标 agent_name 已发布
- POST {prefix}/chat/run   agent_name + query，返回 SSE 事件流
鉴权：Authorization: Bearer <NEXENT_API_TOKEN>，凭据只从环境变量读取，
绝不写入任何证据、日志或仓库文件。

平台标识：
- 保存响应头里的 X-Request-Id / conversation_id 等标识；
- 支持从 conversation_created.content.conversation_id 关联会话
  （content 可以是对象，也可以是 JSON 字符串）。

工具证据原则（比「显式工具名」更严格）：
1. 工具调用事件与工具结果事件必须能用**同一次调用标识**配对；
2. 分别记录 planned_tool_arguments（来自冻结任务清单）与
   actual_tool_arguments（来自平台工具调用事件，或可关联的 MCP 服务端记录）；
3. 只接受已识别的成功工具结果。tool_error、超时、失败标记、推理/最终输出事件、
   缺少结构化结果、无法核实实际入参，一律判为失败或证据不足；
4. 只有「工具成功 + 显式返回 []」才计零命中；
5. 绝不从 final_answer、自然语言或任意 result 数组推断工具已执行。

若 SSE 流不暴露实际入参，允许用可关联的 MCP 服务端调用记录补证；
记录必须能与本次平台会话/请求/任务标识对齐，且工具名与返回信号一致。

若平台 SSE 只返回工具调用与 execution_key、不返回结构化结果，则按该键去查
对应的执行记录（平台执行记录导出，或 MCP 服务端回执）。补证必须满足：
关联键来自 SSE 显式字段（execution_key，或目标工具调用事件的 tool_call_id/
call_id）、记录中同一键唯一匹配、带显式成功标记、无错误/超时标记、工具名一致、
实际入参可核实且与计划一致、返回结果为显式数组。
任何一项不成立（尤其是无法唯一关联）一律保留 insufficient，绝不误报成功。

本模块不提供任何「假平台」实现；本地替身只存在于 tests/fixtures。
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_NAME = "evaluate_portfolio_snapshot"
DEFAULT_PREFIX = "/nb/v1"
HEALTH_PATH = "/health"
AGENTS_PATH = "/agents"
CHAT_RUN_PATH = "/chat/run"
HEALTH_STATUS = "healthy"
HEALTH_SERVICE = "northbound-api"
REQUIRED_ENV = ("NEXENT_BASE_URL", "NEXENT_API_TOKEN", "NEXENT_AGENT_NAME")
OPTIONAL_ENV = (
    "NEXENT_API_PREFIX", "NEXENT_VERSION", "NEXENT_MODEL", "NEXENT_MCP_URL",
    "NEXENT_HEALTH_STATUS", "NEXENT_HEALTH_SERVICE",
    "NEXENT_TOOL_EVENT_TYPES", "NEXENT_MCP_CALL_LOG",
    "NEXENT_EXECUTION_RECORDS",
)

DEFAULT_TOOL_EVENT_TYPES = (
    "tool_call", "tool_result", "tool_response", "tool_output", "tool_use",
    "tool_success", "tool_execution_result", "mcp_call", "mcp_tool", "tool",
)
CALL_EVENT_TYPES = frozenset((
    "tool_call", "tool_use", "tool_request", "tool_invocation",
    "mcp_call", "tool",
))
SUCCESS_RESULT_TYPES = frozenset((
    "tool_result", "tool_response", "tool_output", "tool_success",
    "tool_execution_result", "tool_completed", "mcp_tool", "tool",
))
ERROR_EVENT_TYPES = frozenset((
    "tool_error", "tool_failed", "tool_timeout", "tool_cancelled",
    "timeout", "error", "exception", "failed", "cancelled",
))
NON_EVIDENCE_TYPES = frozenset((
    "final_answer", "answer", "message", "text", "delta", "done", "stop",
    "reasoning", "thought", "thinking", "plan", "llm", "content",
))
CALL_ID_KEYS = ("tool_call_id", "call_id", "toolCallId", "tool_use_id",
                "invocation_id")
ARGUMENT_KEYS = ("arguments", "tool_arguments", "tool_input", "parameters",
                 "args", "input")
RESULT_KEYS = ("tool_result", "result", "output", "data")
SENSITIVE_KEY_PARTS = ("token", "secret", "password", "authorization",
                       "api_key", "apikey", "credential")
HEADER_ID_MAP = {
    "x-request-id": "x_request_id",
    "request-id": "request_id",
    "conversation-id": "conversation_id",
    "conversation_id": "conversation_id",
    "x-conversation-id": "conversation_id",
    "session-id": "session_id",
    "x-session-id": "session_id",
    "trace-id": "trace_id",
    "x-trace-id": "trace_id",
}
EVENT_ID_MAP = {
    "session_id": "session_id", "chat_id": "chat_id",
    "conversation_id": "conversation_id", "request_id": "request_id",
    "x_request_id": "x_request_id", "trace_id": "trace_id",
    "tool_call_id": "tool_call_id", "run_id": "run_id",
    "task_key": "task_key",
}
CORRELATION_KEYS = ("call_id", "tool_call_id", "conversation_id",
                    "request_id", "x_request_id", "session_id", "trace_id",
                    "run_id", "task_key")
MCP_LOG_SOURCES = ("mcp-server-log", "mcp-server-audit")
EXECUTION_KEY_NAMES = frozenset((
    "execution_key", "execution_id", "exec_key", "executionkey",
    "executionid", "tool_execution_key", "toolexecutionkey",
))
# 自然语言 / 文本承载字段：execution_key 绝不能从模型正文里推断。
# 模型可以复述、臆造或注入执行键，只有平台事件的结构化字段才是可信来源。
CONTENT_BEARING_KEYS = frozenset((
    "content", "text", "message", "messages", "answer", "final_answer",
    "delta", "reasoning", "thought", "thinking", "output_text", "body",
    "response", "completion", "narrative", "output_text_delta",
))
# 平台显式执行事件：本身不是工具调用/结果，但会在结构化字段里给出执行键。
EXECUTION_EVENT_MARKERS = ("execution", "tool_execution")
# 回执里「明确失败」的状态词；区别于「缺成功标记」（后者单独处理）。
RECORD_FAILURE_STATES = frozenset((
    "error", "failed", "failure", "timeout", "timed_out", "cancelled",
    "canceled", "exception", "aborted", "rejected",
))
# 回执的作用域标识：显式写了但与本次运行不一致，说明它属于别的 run / 任务。
SCOPE_KEYS = ("run_id", "task_key", "conversation_id", "request_id",
              "x_request_id", "session_id", "trace_id")
EXECUTION_RECORD_SOURCES = frozenset((
    "mcp-server-log", "mcp-server-audit", "mcp-server-receipt",
    "platform-execution-record", "nexent-execution-record",
))
EXECUTION_SUCCESS_STATUSES = frozenset((
    "success", "succeeded", "completed", "ok", "done",
))


class PlatformUnavailable(Exception):
    """平台配置不全、契约未确认或实例不可达。"""


class PlatformResponseError(Exception):
    """平台响应无法解析，或工具证据不足以确认本次调用。"""


@dataclass
class PlatformConfig:
    base_url: str
    token: str
    agent_name: str
    api_prefix: str = DEFAULT_PREFIX
    version: str | None = None
    model: str | None = None
    mcp_url: str | None = None
    health_status: str = HEALTH_STATUS
    health_service: str = HEALTH_SERVICE
    tool_event_types: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_TOOL_EVENT_TYPES)
    mcp_call_log: str | None = None
    execution_records: str | None = None

    @classmethod
    def from_env(cls) -> "PlatformConfig":
        raw_types = os.environ.get("NEXENT_TOOL_EVENT_TYPES", "")
        types = tuple(
            item.strip().lower() for item in raw_types.split(",") if item.strip()
        ) or DEFAULT_TOOL_EVENT_TYPES
        prefix = os.environ.get("NEXENT_API_PREFIX", DEFAULT_PREFIX).strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        return cls(
            base_url=os.environ.get("NEXENT_BASE_URL", "").rstrip("/"),
            token=os.environ.get("NEXENT_API_TOKEN", ""),
            agent_name=os.environ.get("NEXENT_AGENT_NAME", ""),
            api_prefix=prefix.rstrip("/"),
            version=os.environ.get("NEXENT_VERSION"),
            model=os.environ.get("NEXENT_MODEL"),
            mcp_url=os.environ.get("NEXENT_MCP_URL"),
            health_status=os.environ.get("NEXENT_HEALTH_STATUS", HEALTH_STATUS),
            health_service=os.environ.get("NEXENT_HEALTH_SERVICE", HEALTH_SERVICE),
            tool_event_types=types,
            mcp_call_log=os.environ.get("NEXENT_MCP_CALL_LOG"),
            execution_records=os.environ.get("NEXENT_EXECUTION_RECORDS"),
        )

    def missing(self) -> list[str]:
        values = {
            "NEXENT_BASE_URL": self.base_url,
            "NEXENT_API_TOKEN": self.token,
            "NEXENT_AGENT_NAME": self.agent_name,
        }
        return sorted(name for name, value in values.items() if not value)

    def url(self, path: str) -> str:
        return f"{self.base_url}{self.api_prefix}{path}"

    def label(self) -> str:
        """脱敏后的实例标识：不含 token、不含用户信息。"""
        return self.base_url.replace("https://", "").replace("http://", "").split("@")[-1]

    def public_tool_event_types(self) -> list[str]:
        return list(self.tool_event_types)


def _redact(value: Any) -> Any:
    """递归去掉凭据类键，确保任何落盘证据都不含 Token。"""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if any(part in k.lower() for part in SENSITIVE_KEY_PARTS)
                else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "...<truncated>"
    return value


def _request(config: PlatformConfig, path: str, *, method: str = "GET",
             body: dict | None = None, timeout: float = 30.0,
             accept: str = "application/json") -> tuple[int, dict, str]:
    """发起一次北向 API 请求，返回 (http_status, headers, text)。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    request = urllib.request.Request(
        config.url(path), data=data, method=method,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read().decode(
            "utf-8", "replace")


def _json_body(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlatformResponseError(
            f"北向 API 响应不是 JSON：{text[:300]!r}") from exc


def _maybe_json(value: Any) -> Any:
    """content 可能是 JSON 字符串；解析失败就原样返回。"""
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _agent_names(payload: Any, depth: int = 0) -> list[str]:
    """兼容官方 data 包装与若干常见嵌套形式，不做版本猜测。"""
    if depth > 4:
        return []
    candidates: list = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("agents", "data", "result", "items", "list", "records", "rows",
                    "content"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
            if isinstance(value, dict):
                nested = _agent_names(value, depth + 1)
                if nested:
                    return nested
    names = []
    for item in candidates:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("agent_name", "name", "agentName", "id"):
                if isinstance(item.get(key), str):
                    names.append(item[key])
                    break
    return names


def extract_header_ids(headers: dict | None) -> dict:
    """保存响应头里的 X-Request-Id / conversation_id 等可核对标识。"""
    ids: dict = {}
    for key, value in (headers or {}).items():
        canonical = HEADER_ID_MAP.get(str(key).lower())
        if canonical and value and canonical not in ids:
            ids[canonical] = str(value)
    return ids


def _collect_ids(obj: Any, out: dict, depth: int = 0) -> None:
    """递归收取显式标识字段，覆盖 conversation_created.content.conversation_id。"""
    if depth > 5:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            canonical = EVENT_ID_MAP.get(str(key).lower())
            if (canonical and isinstance(value, (str, int))
                    and value and canonical not in out):
                out[canonical] = str(value)
            child = _maybe_json(value)
            if isinstance(child, (dict, list)):
                _collect_ids(child, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_ids(item, out, depth + 1)


def extract_platform_ids(events: list[dict], headers: dict | None = None) -> dict:
    """响应头 + SSE 显式标识；绝不解析自然语言。"""
    ids = extract_header_ids(headers)
    for event in events:
        payload = event.get("data")
        if isinstance(payload, dict):
            _collect_ids(payload, ids)
    return {
        "session_id": (ids.get("session_id") or ids.get("conversation_id")
                       or ids.get("chat_id")),
        "conversation_id": ids.get("conversation_id"),
        "request_id": ids.get("request_id") or ids.get("x_request_id"),
        "x_request_id": ids.get("x_request_id"),
        "trace_id": ids.get("trace_id"),
        "tool_call_id": ids.get("tool_call_id"),
        "run_id": ids.get("run_id"),
        "task_key": ids.get("task_key"),
    }


def _walk_execution_keys(obj: Any, out: set, depth: int = 0) -> None:
    """只走结构化字段；content / text / message 等文本块一律不进入。

    审计反例：模型在 final_answer 或 message.content 里复述／臆造一个
    execution_key，若顺着文本里的 JSON 收键，就能拿一条本地回执伪造成功。
    """
    if depth > 5:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in CONTENT_BEARING_KEYS:
                continue
            if (lowered in EXECUTION_KEY_NAMES
                    and isinstance(value, (str, int)) and value):
                out.add(str(value))
            child = _maybe_json(value)
            if isinstance(child, (dict, list)):
                _walk_execution_keys(child, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_execution_keys(item, out, depth + 1)


def collect_execution_keys(events: list[dict]) -> list[str]:
    """只从 SSE 平台事件的结构化字段收 execution_key，绝不解析模型正文。

    采信两类事件：① 带目标工具名的调用／结果事件；② 平台显式执行事件
    （execution_started 等）。final_answer / message / reasoning 等自然语言
    事件整条跳过，即使正文里嵌了 JSON 或显式 execution_key 字段。
    """
    keys: set = set()
    for event in events:
        etype = _event_type(event)
        if etype in NON_EVIDENCE_TYPES or etype in ERROR_EVENT_TYPES:
            continue
        payload = event.get("data")
        if not isinstance(payload, dict):
            continue
        tool = _tool_name(payload)
        if tool is not None and tool != TOOL_NAME:
            continue
        if tool != TOOL_NAME and not any(
                marker in etype for marker in EXECUTION_EVENT_MARKERS):
            continue
        _walk_execution_keys(payload, keys)
    return sorted(keys)


def collect_tool_call_ids(events: list[dict]) -> list[str]:
    """只从目标工具的调用事件收显式 tool_call_id / call_id。

    用于「SSE 只给 execution_key、但 MCP 服务端回执用调用标识关联」的补证路径；
    结果事件或非目标工具事件里的标识一律不采信。
    """
    ids: set = set()
    for event in events:
        payload = event.get("data")
        if not isinstance(payload, dict) or _tool_name(payload) != TOOL_NAME:
            continue
        etype = _event_type(event)
        if etype not in CALL_EVENT_TYPES or _error_reason(payload, etype):
            continue
        value = _call_id(payload)
        if value:
            ids.add(str(value))
    return sorted(ids)


def _entry_call_ids(entry: dict) -> set:
    out: set = set()
    ids = _log_ids(entry)
    for key in ("call_id", "tool_call_id"):
        value = ids.get(key) or entry.get(key)
        if isinstance(value, (str, int)) and value:
            out.add(str(value))
    return out


def _execution_keys_in(entry: dict) -> list[str]:
    keys: set = set()
    _walk_execution_keys(entry, keys)
    return sorted(keys)


def _execution_success(entry: dict) -> bool:
    """执行记录必须带显式成功标记；缺省不算成功。"""
    for key in ("tool_success", "call_success", "success", "ok"):
        if entry.get(key) is True:
            return True
    status = str(entry.get("status") or entry.get("state") or "").lower()
    return status in EXECUTION_SUCCESS_STATUSES


def _execution_result(entry: dict) -> list | None:
    for key in ("result", "tool_result", "signals", "output_signals"):
        value = _maybe_json(entry.get(key))
        if isinstance(value, list):
            return value
    return None


def _record_failure_reason(entry: dict) -> str | None:
    """回执是否带「明确失败」标记（区别于仅仅缺成功标记）。"""
    reason = (_error_reason(entry, "")
              or _error_reason(entry.get("mcp_raw_result"), ""))
    if reason:
        return reason
    for key in ("tool_success", "call_success", "success", "ok",
                "outer_result_accepted"):
        if entry.get(key) is False:
            return f"回执显式标记 {key}=false"
    state = str(entry.get("status") or entry.get("state") or "").lower()
    if state in RECORD_FAILURE_STATES:
        return f"回执状态为 {state}"
    return None


def _record_scope_conflict(entry: dict, platform_ids: dict | None,
                           run_id: str | None,
                           task_key: str | None) -> str | None:
    """回执显式声明的作用域与本次运行冲突时，认定它属于别的 run / 任务。

    只比较「本次确实掌握」的标识；回执没写该字段不算冲突（允许最小回执）。
    """
    expected = dict(platform_ids or {})
    expected["run_id"] = run_id or expected.get("run_id")
    expected["task_key"] = task_key or expected.get("task_key")
    ids = _log_ids(entry)
    for key in SCOPE_KEYS:
        declared = ids.get(key) or entry.get(key)
        mine = expected.get(key)
        if declared in (None, "") or mine in (None, ""):
            continue
        if str(declared) != str(mine):
            return (f"回执显式标记 {key}={declared!r}，与本次运行的 "
                    f"{mine!r} 不一致，判定属于其他 run / 任务")
    return None


def _record_link_tokens(entry: dict, execution_keys: list[str],
                        call_ids: list[str]) -> set[str]:
    """回执与本次 SSE 调用之间的关联 token（执行键或工具调用标识）。"""
    shared_keys = set(_execution_keys_in(entry)) & set(execution_keys)
    shared_calls = _entry_call_ids(entry) & set(call_ids)
    return {f"key:{v}" for v in shared_keys} | {f"call:{v}" for v in shared_calls}


def screen_call_records(records: list[dict], events: list[dict],
                        platform_ids: dict | None, run_id: str | None,
                        task_key: str | None, *,
                        sse_call_ids: list[str] | None = None
                        ) -> tuple[list[dict], list[str], bool]:
    """返回 (可用记录, 缺口说明, 是否有同次失败)，在任何成功路径之前执行。

    ① 明确属于其他 run / 任务（作用域标识冲突）；
    ② 同一执行键 / 调用标识下存在明确失败 —— 阻断所有成功路径，
       包括 SSE 直接配对，不能只丢弃失败回执后继续认证；
    ③ 其余记录继续交给下游的成功标记 / 入参 / 结果校验。
    """
    gaps: list[str] = []
    blocked: set[int] = set()
    for position, entry in enumerate(records):
        conflict = _record_scope_conflict(entry, platform_ids, run_id, task_key)
        if conflict:
            blocked.add(position)
            gaps.append(conflict)

    execution_keys = collect_execution_keys(events)
    # 已识别的独立返回/完成合并事件也可能携带唯一调用 ID。
    call_ids = sorted(set(collect_tool_call_ids(events)) | set(sse_call_ids or []))
    groups: dict[str, list[int]] = {}
    linked_failure = False
    for position, entry in enumerate(records):
        if position in blocked:
            continue
        for token in _record_link_tokens(entry, execution_keys, call_ids):
            groups.setdefault(token, []).append(position)
    for token, positions in groups.items():
        failures = [p for p in positions if _record_failure_reason(records[p])]
        if not failures:
            continue
        linked_failure = True
        gaps.append(
            f"关联 {token} 下存在 {len(failures)} 条明确失败回执；"
            "同键冲突：本次 SSE 或其他回执的成功信息均不得覆盖该失败")
        blocked.update(positions)

    usable = [entry for i, entry in enumerate(records) if i not in blocked]
    return usable, gaps, linked_failure


def probe(config: PlatformConfig, timeout: float = 6.0) -> dict:
    """按 health -> agents 顺序实测平台；任何凭据都不写入返回值。"""
    checks: list[dict] = []
    result: dict = {
        "status": "unavailable",
        "platform_label": config.label() or None,
        "platform_version": config.version,
        "model": config.model,
        "mcp_url": config.mcp_url,
        "agent_name": config.agent_name or None,
        "api_prefix": config.api_prefix,
        "api_paths": {
            "health": config.url(HEALTH_PATH).replace(config.base_url, "<base>"),
            "agents": config.url(AGENTS_PATH).replace(config.base_url, "<base>"),
            "chat_run": config.url(CHAT_RUN_PATH).replace(config.base_url, "<base>"),
        },
        "health_contract": {"status": config.health_status,
                            "service": config.health_service},
        "tool_event_types": config.public_tool_event_types(),
        "checks": checks,
        "required_environment": list(REQUIRED_ENV),
        "optional_environment": list(OPTIONAL_ENV),
        "credential_handling": "API Token 仅经 NEXENT_API_TOKEN 环境变量读取；"
                               "探测只记录是否设置，绝不写出取值或写入证据文件。",
    }
    missing = config.missing()
    checks.append({"check": "required_environment", "ok": not missing,
                   "detail": "齐全" if not missing else f"缺少 {missing}"})
    if missing:
        result["blocked_at"] = "平台配置不全，未发起任何请求"
        return result

    header_ids: dict = {}
    # ① 健康响应必须精确匹配官方契约，而不是任意返回 200 的网关。
    try:
        status, headers, text = _request(config, HEALTH_PATH, timeout=timeout)
        health = _json_body(text)
        health_ids = extract_header_ids(headers)
        header_ids.update(health_ids)
        contract = {"status": config.health_status,
                    "service": config.health_service}
        confirmed = isinstance(health, dict) and all(
            health.get(key) == value for key, value in contract.items()
        )
        keys = sorted(health.keys())[:20] if isinstance(health, dict) else []
        reported_version = None
        if isinstance(health, dict):
            for version_key in ("version", "app_version", "service_version"):
                if isinstance(health.get(version_key), str):
                    reported_version = health[version_key]
                    break
        checks.append({
            "check": f"GET {config.api_prefix}{HEALTH_PATH}",
            "ok": 200 <= status < 300,
            "detail": f"HTTP {status}",
        })
        checks.append({
            "check": f"健康响应契约精确匹配 status={config.health_status}/"
                     f"service={config.health_service}",
            "ok": 200 <= status < 300 and confirmed,
            "detail": f"响应键={keys}；契约匹配={confirmed}",
        })
        result["health"] = {
            "http_status": status,
            "identity_confirmed": confirmed,
            "contract_matched": confirmed,
            "response_keys": keys,
            "reported_version": reported_version,
            "response_header_ids": health_ids,
        }
        if not (200 <= status < 300 and confirmed):
            result["status"] = "health-contract-mismatch"
            result["blocked_at"] = (
                "健康响应未精确匹配官方契约；请确认网关前缀 /nb/v1 与该实例版本，"
                "或提供一次真实 health 响应样例。不在此处硬闯后续接口。")
            return result
    except urllib.error.HTTPError as exc:
        checks.append({"check": f"GET {config.api_prefix}{HEALTH_PATH}",
                       "ok": False, "detail": f"HTTP {exc.code}"})
        result["blocked_at"] = f"健康检查 HTTP {exc.code}"
        return result
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": f"GET {config.api_prefix}{HEALTH_PATH}",
                       "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        result["blocked_at"] = f"健康检查失败：{type(exc).__name__}"
        return result

    # ② 枚举已发布 Agent，确认目标名称在列（官方 data 包装）。
    try:
        status, headers, text = _request(config, AGENTS_PATH, timeout=timeout)
        payload = _json_body(text)
        agents_ids = extract_header_ids(headers)
        header_ids.update(agents_ids)
        names = _agent_names(payload)
        found = config.agent_name in names
        checks.append({
            "check": f"GET {config.api_prefix}{AGENTS_PATH}",
            "ok": 200 <= status < 300,
            "detail": f"HTTP {status}",
        })
        checks.append({
            "check": "目标 Agent 已发布且名称可枚举",
            "ok": 200 <= status < 300 and found,
            "detail": f"已发布 Agent 数={len(names)}；目标匹配={found}",
        })
        result["agents"] = {
            "http_status": status,
            "published_count": len(names),
            "target_found": found,
            "response_header_ids": agents_ids,
        }
        if not (200 <= status < 300 and found):
            result["status"] = "agent-not-published"
            result["blocked_at"] = (
                "已发布 Agent 列表中找不到 NEXENT_AGENT_NAME；请先在平台发布"
                "绑定了规则 MCP 的 Agent，并核对名称大小写。")
            return result
    except urllib.error.HTTPError as exc:
        checks.append({"check": f"GET {config.api_prefix}{AGENTS_PATH}",
                       "ok": False, "detail": f"HTTP {exc.code}"})
        result["blocked_at"] = f"Agent 列表 HTTP {exc.code}"
        return result
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": f"GET {config.api_prefix}{AGENTS_PATH}",
                       "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        result["blocked_at"] = f"Agent 列表读取失败：{type(exc).__name__}"
        return result

    result["platform_ids"] = header_ids
    result["status"] = "reachable"
    return result


def parse_sse_events(lines, max_lines: int = 5000) -> list[dict]:
    """把 SSE 行流解析成事件列表；保留原始结构但不含任何凭据。"""
    events: list[dict] = []
    current: dict = {"event": None, "data": []}

    def flush():
        if not current["data"] and not current["event"]:
            return
        raw = "\n".join(current["data"]).strip()
        parsed: Any = None
        if raw and raw != "[DONE]":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"text": raw}
        events.append({
            "index": len(events),
            "event": current["event"],
            "done": raw == "[DONE]",
            "data": _redact(parsed),
        })

    for count, raw_line in enumerate(lines):
        if count >= max_lines:
            break
        line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) \
            else str(raw_line)
        line = line.rstrip("\r\n")
        if line == "":
            flush()
            current = {"event": None, "data": []}
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"].append(line[len("data:"):].strip())
        elif line.startswith("id:") or line.startswith("retry:"):
            continue
    flush()
    return events


def _event_type(event: dict) -> str:
    payload = event.get("data")
    if isinstance(payload, dict):
        for key in ("type", "event_type", "event"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value.lower()
    return str(event.get("event") or "").lower()


def _tool_name(payload: Any, depth: int = 0) -> str | None:
    if depth > 3 or not isinstance(payload, dict):
        return None
    for key in ("tool_name", "name", "tool"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    function = payload.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    for key in ("data", "content", "input"):
        nested = _tool_name(_maybe_json(payload.get(key)), depth + 1)
        if nested:
            return nested
    return None


def _call_id(payload: Any, depth: int = 0) -> str | None:
    if depth > 3 or not isinstance(payload, dict):
        return None
    for key in CALL_ID_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("data", "content", "input", "function"):
        nested = _call_id(_maybe_json(payload.get(key)), depth + 1)
        if nested:
            return nested
    return None


def _arguments(payload: Any, depth: int = 0) -> dict | None:
    """只接受确实是本工具入参的字典（含 portfolio 与 snapshot）。"""
    if depth > 3 or not isinstance(payload, dict):
        return None
    for key in ARGUMENT_KEYS:
        value = _maybe_json(payload.get(key))
        if (isinstance(value, dict)
                and "portfolio" in value and "snapshot" in value):
            return value
    for key in ("data", "content", "function", "input"):
        nested = _arguments(_maybe_json(payload.get(key)), depth + 1)
        if nested:
            return nested
    return None


def _result_list(payload: Any, depth: int = 0) -> tuple[list, str] | None:
    """只认结构化的工具结果字段，不认 content 文本块。"""
    if depth > 3 or not isinstance(payload, dict):
        return None
    structured = payload.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), list):
        return structured["result"], "structuredContent.result"
    for key in RESULT_KEYS:
        value = _maybe_json(payload.get(key))
        if isinstance(value, list) and key in ("tool_result", "result"):
            return value, key
        if isinstance(value, dict):
            nested = _result_list(value, depth + 1)
            if nested:
                return nested
    return None


def _is_completed_result(payload: dict, etype: str) -> bool:
    """结果字段本身不是返回：普通调用不能用内嵌数组给自己补证。

    明确的结果事件类型提供返回语义；通用 tool/mcp_tool 或自定义白名单
    事件还需显式成功标记。普通 call/request 类型仍须独立返回或原生回执。
    本函数只在事件白名单和错误检查之后使用，不从默认 isError=false 补成功。
    """
    if etype in CALL_EVENT_TYPES - {"tool"}:
        return False
    if etype in SUCCESS_RESULT_TYPES - {"tool", "mcp_tool"}:
        return True
    return _execution_success(payload)


def _error_reason(payload: Any, etype: str, depth: int = 0) -> str | None:
    if etype in ERROR_EVENT_TYPES:
        return f"工具事件类型为 {etype}"
    if depth > 3 or not isinstance(payload, dict):
        return None
    for key in ("error", "errors", "is_error", "isError", "exception",
                "timeout", "timed_out", "failed"):
        value = payload.get(key)
        if value not in (None, "", False, 0, []):
            return f"事件带 {key} 错误/超时标记"
    for key in ("success", "ok"):
        if payload.get(key) is False:
            return f"事件显式标记 {key}=false"
    state = payload.get("status") or payload.get("state")
    if isinstance(state, str) and state.lower() in (
            "error", "failed", "failure", "timeout", "timed_out",
            "cancelled", "canceled"):
        return f"事件状态为 {state}"
    for key in ("data", "output", "content"):
        nested = _error_reason(_maybe_json(payload.get(key)), "", depth + 1)
        if nested:
            return nested
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def load_mcp_call_log(path: str | None) -> list[dict]:
    """读取 MCP 服务端调用记录：JSON 文档（calls 数组）或 JSONL。"""
    if not path:
        return []
    file = Path(path)
    if not file.is_file():
        return []
    text = file.read_text(encoding="utf-8-sig")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        entries = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    if isinstance(doc, list):
        return [item for item in doc if isinstance(item, dict)]
    if isinstance(doc, dict):
        # 单行 JSONL 也是合法 JSON 对象；按单条调用记录处理。
        if "tool_name" in doc:
            return [doc]
        calls = doc.get("calls")
        if isinstance(calls, list):
            return [item for item in calls if isinstance(item, dict)]
    return []


# 兼容旧名；执行记录与 MCP 回执都是「可关联的调用记录」。
load_call_records = load_mcp_call_log


def _log_ids(entry: dict) -> dict:
    ids: dict = {}
    _collect_ids(entry, ids)
    for key in ("call_id", "tool_call_id"):
        value = entry.get(key)
        if value and "call_id" not in ids:
            ids["call_id"] = str(value)
    return ids


def _shared_correlation(entry: dict, result_event: dict,
                        platform_ids: dict | None, run_id: str | None,
                        task_key: str | None) -> str | None:
    """MCP 记录必须至少与一个平台/任务标识对得上，不能只靠结果相同。"""
    entry_ids = _log_ids(entry)
    for key in CORRELATION_KEYS:
        left = entry_ids.get(key) or entry.get(key)
        if key == "call_id":
            right = result_event.get("call_id") or (platform_ids or {}).get(
                "tool_call_id")
        elif key == "run_id":
            right = run_id
        elif key == "task_key":
            right = task_key
        else:
            right = (platform_ids or {}).get(key)
        if left and right and str(left) == str(right):
            return key
    return None


def _mcp_candidates(result_event: dict, logs: list[dict],
                    platform_ids: dict | None, run_id: str | None,
                    task_key: str | None) -> list[tuple[dict, str]]:
    """MCP 记录补证：除关联标识外，还须显式成功、无失败标记、作用域一致。"""
    candidates = []
    for entry in logs:
        if entry.get("tool_name") != TOOL_NAME:
            continue
        if entry.get("source") not in (None, *MCP_LOG_SOURCES):
            continue
        if _record_failure_reason(entry):
            continue
        if _record_scope_conflict(entry, platform_ids, run_id, task_key):
            continue
        args = _maybe_json(entry.get("arguments"))
        if not (isinstance(args, dict) and "portfolio" in args
                and "snapshot" in args):
            continue
        if entry.get("result") != result_event["result"]:
            continue
        shared = _shared_correlation(entry, result_event, platform_ids,
                                    run_id, task_key)
        if shared:
            candidates.append((entry, shared))
    return candidates


def _execution_candidates(events: list[dict], records: list[dict],
                          sse_results: list[dict], *,
                          platform_ids: dict | None = None,
                          run_id: str | None = None,
                          task_key: str | None = None
                          ) -> tuple[list[tuple], list[str]]:
    """按 SSE 的执行键或工具调用标识查执行记录；无法唯一关联即返回空候选。

    两条唯一关联方式：
    ① 回执带与 SSE 相同的 ``execution_key``；
    ② 回执是 MCP 服务端记录，带与 SSE 工具调用事件相同的 ``tool_call_id``/``call_id``。
    除此之外（仅结果相同、仅 conversation_id 相同等）一律不算可唯一关联。
    """
    keys = collect_execution_keys(events)
    call_ids = collect_tool_call_ids(events)
    if not keys and not call_ids:
        return [], [
            "SSE 流没有可核对的工具结果，也没有暴露 execution_key 或工具调用标识；"
            "无法关联该次调用的执行记录 / MCP 服务端回执。"
        ]
    # 同一执行键 / 调用标识下只要出现一条「明确失败」回执，整组关联作废：
    # 不能把失败那条过滤掉之后，让同键的成功回执继续被采信。
    conflicted_tokens: set[str] = set()
    for entry in records:
        if not _record_failure_reason(entry):
            continue
        for value in _execution_keys_in(entry):
            conflicted_tokens.add("key:" + value)
        for value in _entry_call_ids(entry):
            conflicted_tokens.add("call:" + value)

    candidates = []
    for entry in records:
        if entry.get("tool_name") != TOOL_NAME:
            continue
        source = entry.get("source")
        if source is not None and source not in EXECUTION_RECORD_SOURCES:
            continue
        shared_keys = sorted(set(_execution_keys_in(entry)) & set(keys))
        shared_calls = sorted(_entry_call_ids(entry) & set(call_ids))
        if not shared_keys and not shared_calls:
            continue
        token_kind = "key" if shared_keys else "call"
        token_value = shared_keys[0] if shared_keys else shared_calls[0]
        if f"{token_kind}:{token_value}" in conflicted_tokens:
            continue
        if _record_scope_conflict(entry, platform_ids, run_id, task_key):
            continue
        if not _execution_success(entry):
            continue
        if _error_reason(entry, ""):
            continue
        arguments = _maybe_json(entry.get("arguments")
                                or entry.get("tool_arguments"))
        if not (isinstance(arguments, dict)
                and "portfolio" in arguments and "snapshot" in arguments):
            continue
        result = _execution_result(entry)
        if result is None:
            continue
        # SSE 若已给出显式结果，执行记录必须与之逐项一致，不能用记录覆盖平台结果。
        if sse_results and not any(
                item["result"] == result for item in sse_results):
            continue
        if shared_keys:
            association, token, exec_key = "execution-key", token_value, token_value
        else:
            association, token, exec_key = "tool-call-id", token_value, None
        candidates.append((entry, token, arguments, result, association, exec_key))
    if not candidates:
        return [], [
            f"SSE 暴露 execution_key={keys} / tool_call_id={call_ids}，但在执行记录 / "
            "MCP 服务端回执中找不到同时满足『同一执行键或同一工具调用标识 + 显式成功标记 + "
            "无错误标记 + 工具名一致 + 实际入参可核实 + 显式结果数组』的记录；"
            "本次保留 insufficient。",
        ]
    if len(candidates) > 1:
        return [], [
            f"execution_key={keys} / tool_call_id={call_ids} 对应 {len(candidates)} 条"
            "执行记录，无法唯一关联到这一次调用；按指令保留 insufficient，不误报成功。",
        ]
    return candidates, []


def resolve_tool_evidence(events: list[dict], allowed_types: tuple[str, ...],
                          planned_arguments: dict | None, *,
                          mcp_call_log: list[dict] | None = None,
                          execution_records: list[dict] | None = None,
                          platform_ids: dict | None = None,
                          run_id: str | None = None,
                          task_key: str | None = None) -> dict:
    """配对工具调用与结果，并核实计划/实际入参；返回 status/evidence/gaps。

    依次尝试 SSE 同次调用标识配对、MCP 服务端回执补证、SSE execution_key
    对应的执行记录补证。同次明确失败在所有成功路径之前阻断；任何路径都
    无法唯一关联时保留 insufficient，不因本地日志里有结果就误报成功。
    """
    allowed = {t.lower() for t in allowed_types}
    gaps: list[str] = []
    fatal: list[str] = []
    calls: list[dict] = []
    results: list[dict] = []

    for event in events:
        payload = event.get("data")
        if not isinstance(payload, dict) or _tool_name(payload) != TOOL_NAME:
            continue
        etype = _event_type(event)
        index = event.get("index")
        error = _error_reason(payload, etype) or _record_failure_reason(payload)
        if error:
            fatal.append(f"事件 #{index}（{etype or '未标注'}）：{error}")
            continue
        if etype in NON_EVIDENCE_TYPES:
            continue
        if (etype not in allowed and etype not in SUCCESS_RESULT_TYPES
                and etype not in CALL_EVENT_TYPES):
            gaps.append(f"事件 #{index} 类型 {etype!r} 不在工具事件白名单，未作为证据")
            continue
        found = _result_list(payload)
        entry = {
            "index": index,
            "event_type": etype,
            "call_id": _call_id(payload),
            "arguments": _arguments(payload),
            "result": found[0] if found else None,
            "result_field": found[1] if found else None,
        }
        if found and _is_completed_result(payload, etype):
            results.append(entry)
        if entry["arguments"] is not None or etype in CALL_EVENT_TYPES:
            calls.append(entry)

    if fatal:
        return {
            "status": "failed",
            "evidence": None,
            "gaps": fatal + [
                "检测到工具错误/超时/失败标记：本次调用不得计为成功命中，"
                "也不得计为零命中。",
            ],
        }
    # 合并两类可关联记录，按内容去重，避免同一条记录被数成两次「多匹配」。
    records, seen = [], set()
    for entry in list(mcp_call_log or []) + list(execution_records or []):
        marker = _canonical(entry)
        if marker not in seen:
            seen.add(marker)
            records.append(entry)
    # 体检：剔除「明确属于其他 run / 任务」与「同键成功+失败冲突」的回执。
    # 不做这一步的话，失败回执会被下游过滤掉，同键的成功回执照样能伪造成功。
    records, screen_gaps, linked_failure = screen_call_records(
        records, events, platform_ids, run_id, task_key,
        sse_call_ids=[entry["call_id"] for entry in calls + results
                      if entry["call_id"]])
    gaps.extend(screen_gaps)
    if linked_failure:
        return {"status": "insufficient", "evidence": None, "gaps": gaps}

    if not results:
        gaps.append(
            "SSE 流中没有带显式工具名 " + TOOL_NAME + " 的已识别成功工具结果；"
            "不能从 final_answer 或任意 result 数组推断工具已执行。")

    chosen: tuple[dict, dict | None, str] | None = None
    if results:
        for result_event in reversed(results):
            if not result_event["call_id"]:
                continue
            paired = next(
                (c for c in calls if c["call_id"] == result_event["call_id"]
                 and c["arguments"] is not None), None
            )
            if paired:
                chosen = (result_event, paired, "sse-tool-call")
                break
    if chosen is None and results:
        candidates: list[tuple[dict, dict, str, str]] = []
        for result_event in results:
            for entry, shared in _mcp_candidates(
                    result_event, records, platform_ids, run_id, task_key):
                candidates.append((result_event, entry, shared, "mcp-server-log"))
        if len(candidates) > 1:
            gaps.append("多条 MCP 服务端记录与同一次调用匹配，无法唯一关联")
        elif len(candidates) == 1:
            result_event, entry, _shared, source = candidates[0]
            chosen = (result_event, {"arguments": _maybe_json(entry["arguments"]),
                                     "call_id": _log_ids(entry).get("call_id")
                                     or result_event["call_id"],
                                     "index": None}, source)
        else:
            gaps.append(
                "工具调用与结果未能按同一次调用标识配对；SSE 流未暴露实际入参，"
                "且没有可与本次平台会话/请求/任务标识关联的 MCP 服务端调用记录。")

    # ③ 平台只给「工具调用 + execution_key」：按该键查唯一执行记录。
    if chosen is None:
        executions, exec_gaps = _execution_candidates(
            events, records, results, platform_ids=platform_ids,
            run_id=run_id, task_key=task_key)
        if len(executions) == 1:
            _entry, key, arguments, signals, association, exec_key = executions[0]
            chosen = (
                {"index": None, "call_id": key, "result": signals,
                 "result_field": "execution_record.result",
                 "event_type": "execution_record", "execution_key": exec_key,
                 "pairing": association},
                {"arguments": arguments, "index": None, "call_id": key,
                 "execution_key": exec_key},
                "execution-record",
            )
        else:
            gaps.extend(exec_gaps)

    if chosen is None:
        return {"status": "insufficient", "evidence": None,
                "gaps": gaps + _mcp_hint()}

    result_event, call_event, source = chosen
    actual = call_event["arguments"]
    if not isinstance(actual, dict) or "portfolio" not in actual \
            or "snapshot" not in actual:
        return {"status": "insufficient", "evidence": None,
                "gaps": ["无法核实工具实际入参（缺少 portfolio/snapshot）"]
                + _mcp_hint()}
    if planned_arguments is None or _canonical(actual) != _canonical(
            planned_arguments):
        planned_id = (planned_arguments or {}).get("portfolio", {}).get("id")
        actual_id = actual.get("portfolio", {}).get("id")
        return {
            "status": "insufficient",
            "evidence": None,
            "gaps": [
                f"工具实际入参与冻结清单的计划入参不一致：计划组合={planned_id!r} "
                f"实际组合={actual_id!r}；即便返回空数组也不得计为零命中。",
            ],
        }

    pairing = result_event.get("pairing") or {
        "sse-tool-call": "sse-call-id",
        "mcp-server-log": "mcp-server-log",
        "execution-record": "execution-key",
    }[source]
    evidence = {
        "status": "verified",
        "tool_name": TOOL_NAME,
        "call_id": result_event["call_id"] or call_event.get("call_id"),
        "execution_key": result_event.get("execution_key")
        or call_event.get("execution_key"),
        "call_event_index": call_event["index"],
        "result_event_index": result_event["index"],
        "result_field": result_event["result_field"],
        "pairing": pairing,
        "planned_tool_arguments": planned_arguments,
        "actual_tool_arguments": actual,
        "actual_arguments_source": source,
        "tool_success": True,
        "signal_count": len(result_event["result"]),
        "signals": result_event["result"],   # 原样，不做任何数值处理
        "zero_hit": len(result_event["result"]) == 0,
    }
    return {"status": "verified", "evidence": evidence, "gaps": []}


def _mcp_hint() -> list[str]:
    return [
        "补证要求：提供可与本次平台会话/请求/任务标识关联的 MCP 服务端调用记录"
        "（工具名、实际入参、返回信号、时间戳、call_id 或 conversation_id/"
        "request_id/run_id/task_key），配置到 NEXENT_MCP_CALL_LOG。",
        "若平台 SSE 只给 execution_key：把该键对应的平台执行记录导出或 MCP 服务端"
        "回执配置到 NEXENT_EXECUTION_RECORDS，记录须带同一执行键（或与 SSE 工具调用"
        "事件相同的 tool_call_id/call_id）、显式成功标记、实际入参与返回信号；"
        "无法唯一关联时保持 insufficient。",
    ]


def extract_tool_evidence(events: list[dict], allowed_types: tuple[str, ...],
                          planned_arguments: dict | None = None, **kwargs) -> dict | None:
    """兼容入口：只有完全核实通过才返回证据，否则返回 None。"""
    resolved = resolve_tool_evidence(events, allowed_types,
                                     planned_arguments, **kwargs)
    return resolved["evidence"]


def build_query(task: dict) -> str:
    """构造给 Agent 的指令：只有输入，没有预期结果，不暗示任何阈值答案。"""
    arguments = json.dumps(task["tool_arguments"], ensure_ascii=False,
                           sort_keys=True)
    return (
        "请调用已绑定的 MCP 工具 " + TOOL_NAME + " 处理下面的输入，"
        "并把该工具的结构化结果（信号数组）原样返回；"
        "不要重新计算或改写价格、阈值、时间、标的与规则，"
        "也不要用自然语言复述代替结构化结果。\n"
        "输入 JSON：\n" + arguments
    )


def evidence_gaps(events: list[dict], ids: dict, gaps: list[str] | None = None) -> list[str]:
    """把无法核实的原因汇总成可交付给用户的缺口清单。"""
    out = list(gaps or [])
    if not any(value for key, value in (ids or {}).items()
               if key in ("session_id", "conversation_id", "request_id",
                          "x_request_id", "tool_call_id")):
        out.append("响应头与 SSE 事件流均未暴露可核对的会话/请求/工具调用标识")
    out.append(
        f"已解析 {len(events)} 个 SSE 事件；请核对事件类型白名单 "
        "NEXENT_TOOL_EVENT_TYPES 是否覆盖实际部署的事件名。")
    return out


def invoke_task(config: PlatformConfig, *, run_id: str, task: dict,
                timeout: float = 90.0) -> dict:
    """用 agent_name + query 发起一次真实对话，解析 SSE 并保留工具依据。"""
    missing = config.missing()
    if missing:
        raise PlatformUnavailable(f"平台配置缺失：{missing}")
    body = {
        "agent_name": config.agent_name,
        "query": build_query(task),
    }
    request = urllib.request.Request(
        config.url(CHAT_RUN_PATH),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            http_status = response.status
            content_type = response.headers.get("Content-Type", "")
            headers = dict(response.headers)
            events = parse_sse_events(response)
    except urllib.error.HTTPError as exc:
        raise PlatformResponseError(
            f"chat/run HTTP {exc.code}：{exc.read().decode('utf-8', 'replace')[:400]}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise PlatformUnavailable(
            f"chat/run 调用失败：{type(exc).__name__}: {exc}") from exc

    header_ids = extract_header_ids(headers)
    ids = extract_platform_ids(events, headers)
    execution_keys = collect_execution_keys(events)
    tool_call_ids = collect_tool_call_ids(events)
    resolved = resolve_tool_evidence(
        events, config.tool_event_types, task.get("tool_arguments"),
        mcp_call_log=load_call_records(config.mcp_call_log),
        execution_records=load_call_records(config.execution_records),
        platform_ids=ids, run_id=run_id, task_key=task.get("task_key"),
    )
    tool = resolved["evidence"]
    gaps = [] if tool else evidence_gaps(events, ids, resolved["gaps"])
    return {
        "http_status": http_status,
        "content_type": content_type,
        "run_id": run_id,
        "task_key": task["task_key"],
        "platform_ids": ids,
        "response_header_ids": header_ids,
        "execution_keys": execution_keys,
        "tool_call_ids": tool_call_ids,
        "events": events,                    # 已脱敏；原始响应只留在本机
        "tool_evidence": (None if tool is None else {
            k: v for k, v in tool.items() if k != "signals"
        }),
        "evidence_status": resolved["status"],
        "signals": None if tool is None else tool["signals"],
        "evidence_gaps": gaps,
        "raw_response": {
            "schema": "topic16-northbound-sse-v2",
            "api_path": config.api_prefix + CHAT_RUN_PATH,
            "agent_name": config.agent_name,
            "event_count": len(events),
            "response_header_ids": header_ids,
            "execution_keys": execution_keys,
            "tool_call_ids": tool_call_ids,
            "events": events,
        },
    }
