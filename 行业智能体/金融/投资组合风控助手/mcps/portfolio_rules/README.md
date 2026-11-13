# 组合价格阈值 MCP 服务

为兼容 Nexent v2.8.3 中的旧 `mcpadapt`，成功返回同时包含真实结构化 `result` 数组
和其 JSON 文本块，包含明确的 `isError=false`。零命中也返回文本 `[]`，避免合法的
结构化空数组因文本内容块为空而被适配器拒绝。规则计算、入参、审计及输入/输出 schema 保持原样。

在固定 MCP 2.3 项目环境中执行传输回归：

```bash
.venv/bin/python -m unittest discover -s mcps/portfolio_rules/tests -v
```

命中、成功空数组、错误不冒充零命中及 schema 保持由专用回归覆盖；异常仍抛 `ToolError`。
实机 smoke / full 的实际结论见[实跑说明](../../eval/nexent/REAL_PLATFORM_RUN.md)。

`evaluate_portfolio_snapshot` 调用项目内的 `portfolio_risk.rules.evaluate_snapshot`，只读计算一张带时区的行情快照，返回零条或多条价格信号。等于止损线或止盈线也会触发。相同输入可重复调用，不产生推送。

## 安装

在项目根目录用独立 Python 环境安装官方 MCP SDK。以下为 Windows PowerShell 命令。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r mcps/portfolio_rules/requirements.txt
```

## 通过 Remote URL 配置 Nexent

优先使用 `streamable-http`（已在 `mcp==2.3.0` 本地自检中通过工具发现、命中与零命中三项验证）：

```powershell
.\.venv\Scripts\python.exe -m mcps.portfolio_rules.server --transport streamable-http --port 8769
```

URL 为 `http://<平台可达地址>:8769/mcp`。若该 Nexent 版本只支持 SSE：

```powershell
.\.venv\Scripts\python.exe -m mcps.portfolio_rules.server --transport sse --port 8768
```

SSE URL 为 `http://<平台可达地址>:8768/sse`。服务默认只监听 `127.0.0.1`；当 Nexent 后端与服务同机运行并共享网络命名空间时可直接用回环地址。刷新工具列表后，将 `evaluate_portfolio_snapshot` 绑定到行情监控智能体。实际平台注册和原生配置导出属于后续集成步骤。[Nexent MCP 接入说明](https://modelengine-group.github.io/nexent/en/integration/integration-in/mcp.html)

**SSE 观察记录（原因未证实）**：本机自检**曾观察到** `mcp==2.3.0` 自带的
`mcp.client.sse.sse_client` 在 POST `/messages/?session_id=…` 时返回 `404 Not Found`，
`tools/list` 未能完成；手工 HTTP 请求 `GET /sse` 能拿到 endpoint 事件、POST 返回 202。
这只能说明本机这组端口与启动时序下曾出现 404，**原因未证实**，不能归因于 SDK 会话注册
时序：复核方用相同 SDK 与相同自检脚本，在默认端口及另一对端口都跑通了 SSE 的工具发现、
止损命中和零命中，另测 15 个并发会话均通过。旧报告只保留外层 `ExceptionGroup` 文本，
不足以判定机理。

现在的自检脚本会展开异常组到**最内层错误**、单独摘出 HTTP 错误行，并用
「启动前端口空闲 + netstat 记录监听 PID + 终止本次启动进程后端口释放」确认端口归属
本次启动链（venv 的 `python.exe` 是启动器，实际监听在子进程，不能按 PID 相等硬判）。
两组端口的结果见 `eval/results/mcp_smoke.json` 与 `mcp_smoke_alt_port.json`，
脚本见 `eval/nexent/mcp_smoke.py`。Nexent Remote URL 实际选用哪种 transport、平台侧
客户端能否连上 SSE，仍须在拿到的实例上验证。该观察只涉及传输层，
`evaluate_portfolio_snapshot` 的只读逻辑与返回结构未改。

如果 Nexent 后端在 Docker 内，容器中的回环地址指向容器自身。可用 `--host <接口地址> --port 8768` 监听后端可达的接口，然后使用该地址拼出 SSE URL。在 Docker Desktop 上也可使用 `host.docker.internal` 访问宿主机上对容器开放的服务。此开发服务无鉴权，请把监听接口和访问权限限制在可信的开发网络内。

## 可选调用审计（默认关闭）

当 `chat/run` 的 SSE 只给出工具结果、不给工具调用事件的实际入参时，可以用服务端审计记录补证。
启动服务时加两个参数：

```powershell
.\.venv\Scripts\python.exe -m mcps.portfolio_rules.server --transport streamable-http --port 8769 `
  --audit-log eval/nexent/state/mcp_calls.jsonl `
  --correlation '{"run_id":"RUN-20261109T000000Z-abcd1234"}'
```

每次 `evaluate_portfolio_snapshot` **成功返回**后会在本机追加一行 JSONL，记录
`tool_name`、`tool_success=True`、实际 `arguments`（portfolio + snapshot）、`result`、
`recorded_at` 与 `correlation`。客户端用 `NEXENT_MCP_CALL_LOG`（或驱动
`--mcp-call-log`）读取，并要求记录与本次平台会话/请求/任务标识至少共享一个标识、
返回信号与 SSE 结果一致且唯一匹配，才接受补证。`--correlation` 可填 `call_id` /
`conversation_id` / `request_id` / `run_id` / `task_key` / `execution_key` 等。

若平台 SSE **只给「工具调用 + `execution_key`」而没有结构化工具结果**，这份审计记录
还可以作为「可唯一关联到该次调用的 MCP 服务端回执」，改由
`NEXENT_EXECUTION_RECORDS`（或驱动 `--execution-records`）读取。此时必须满足：
回执里的 `call_id` 与 SSE 工具调用事件的 `tool_call_id` / `call_id` 相同（或通过
`--correlation` 带上同一个 `execution_key`）、只有一条记录匹配、带
`tool_success=True`、无错误/超时标记、`arguments` 与冻结清单的计划入参一致、
`result` 是显式数组。仅凭「本地日志里也有结果」不能补证；关联不上客户端会保留
`insufficient`。`mcps/portfolio_rules/server.py` 的审计行只在规则函数成功返回后写出，
因此 `tool_success=True` 不是推测值。

审计开关默认关闭；开启时只写服务端本机日志，不改动任何行情或持仓数据，也不改变工具的
`read_only_hint=True` 语义。`*.mcp_calls.jsonl`、`*audit_log.jsonl` 与 `*execution_records*.json*` 已加入
`.gitignore`，原始调用记录默认不提交仓库。

## 通过 stdio 本地调试

在支持 stdio 的 MCP 客户端中选择项目根目录为工作目录，命令设置为虚拟环境中的 Python 绝对路径，参数设置为 `-m`、`mcps.portfolio_rules.server`。客户端会启动子进程并通过标准输入输出通信。

## 调用样例

调用 `evaluate_portfolio_snapshot`，传入以下参数。价格用十进制字符串可保留价格精度。

```json
{
  "portfolio": {
    "id": "stock-demo",
    "holdings": [{"symbol": "SIM-STOCK-A", "stop_loss": "95.00", "take_profit": "106.00"}]
  },
  "snapshot": {
    "timestamp": "2026-11-07T09:30:00+08:00",
    "prices": {"SIM-STOCK-A": "95.00"}
  }
}
```

MCP 响应的 `structuredContent.result` 是信号列表。样例会返回一条 `stop_loss`，含 `event_id`、组合、标的、UTC 时间、价格和阈值。将价格改为 `100.00` 时信号列表为空。缺少持仓行情或输入无效时，客户端收到错误。
