# Nexent 编排 → 落库 → 独立复算（课题 16）

## 价格证据修复（2026-11-10，本地验证）

普通调用内嵌数组不再冒充完成返回；同次原生失败会在所有成功认证路径之前阻断。
独立两反例由错误 verified 改为 insufficient，合法调用/返回对照保持通过。
本地价格回归 96 项通过、0 skip；A 已完成固定63d1a74的新 run smoke/full 独立验证，见下节。
冻结哈希、事件契约、命令及未验证项见[本批交付说明](PRICE_EVIDENCE_FIX_20261110.md)。

## 当前实机状态（2026-11-10）

A 验证 run `topic16-live-20261110-025739`：4＋20＝24任务、48格，零命中10；
三方20/20/20，TP20/FP0/FN0、差异0，四核验退出码0。见[实跑报告](PRICE_FULL_20261110.md)。
只覆盖冻结合成行情；不扩展为推送、舆情或整个课题通过。

## 历史实机状态（2026-11-09）

真实 smoke 已通过；同一 `run_id=topic16-live-20261109-133209` 的 full 已执行但未通过。
复用 4 个任务，新增完成 6 个，共 10/24；三方信号条数为 3/3/3，TP/FP/FN=3/0/17。
首个证据缺口为 `P-FUND|2026-11-09T11:00:00+08:00`，原 4 个任务、载荷与预检均保留。
详细边界、四条复算命令和失败后续跑限制见[实跑与复现说明](REAL_PLATFORM_RUN.md)，
脱敏计数与组件哈希见[实跑摘要](../results/nexent_full_real_20261109.json)，
可选平台补丁见[deployment](deployment/README.md)。原始平台数据及凭据不入库。

以下为实机部署前的离线加固记录；其中“本轮”“待平台”描述保留历史验证边界，当前事实以上节为准。

> ## 历史离线状态：待平台实跑
>
> 本机没有可用的 Nexent 实例：无 Docker Desktop / podman / kubectl，常见端口无监听，
> 云端 `nexent.tech` 不可达，也没有任何平台凭据。**因此本轮没有发起真实 Nexent 回放，
> 没有生成 `end-to-end` 通过报告，也没有编造 run_id 或平台调用记录。**
>
> 本轮按复核意见完成两项加固（数据库内容篡改会被对账拦截、任务级原子提交），
> 把真实平台客户端改为 v2.8.3 北向 API，第三轮按官方原样响应修正了健康契约与
> 工具调用配对，第四轮补上「SSE 只给 `execution_key`」时按该键查唯一执行记录
> 的补证通路，并给 `--mode full` 加了 smoke 预检门禁。所有本地产物仍标为
> `scope=local-fixture` / `local-mcp-smoke`，`platform_evidence = false`。
>
> **当前明确暂缓全量 24 任务**：实例可用后先跑 `--mode smoke`，拿到一条命中、
> 一条零命中且各自可追回平台会话 / 实际入参 / 成功返回值 / SQLite 查询结果的
> 记录；两条核实前，`--mode full` 直接拒绝（退出码 4）。
>
> 原始平台 SSE 响应默认只留在运行机器本机（`platform_records/` 已加入 `.gitignore`），
> 仓库只提交脱敏摘要、哈希与对账报告；证据 JSON 中的路径已全部改为仓库相对路径。
>
> 平台成功运行后，才从平台导出智能体配置。

## 需要你提供什么

你只需准备**一台可访问的 Nexent 实例**：在平台发布绑定了 `evaluate_portfolio_snapshot`
的 Agent，在本机环境变量配置 API Key，然后把实例地址和发布的 Agent 名称告诉我们。
**不需要把 Token 发给 WorkBuddy、贴进聊天、写进日志或仓库。**

| 环境变量 | 用途 |
| --- | --- |
| `NEXENT_BASE_URL` | 实例地址（必填） |
| `NEXENT_API_TOKEN` | Bearer API Key（必填，仅本机环境变量读取） |
| `NEXENT_AGENT_NAME` | 已发布的 Agent 名称（必填，不再是 agent_id） |
| `NEXENT_API_PREFIX` | 网关前缀，默认 `/nb/v1`；实际部署不同时才改 |
| `NEXENT_VERSION` / `NEXENT_MODEL` / `NEXENT_MCP_URL` | 可选，写入运行追溯 |
| `NEXENT_HEALTH_STATUS` / `NEXENT_HEALTH_SERVICE` | 可选；默认精确校验 `healthy` / `northbound-api`，仅当该实例官方契约不同才显式覆盖 |
| `NEXENT_TOOL_EVENT_TYPES` | 可选；真实工具事件名不在默认白名单时补充（逗号分隔） |
| `NEXENT_MCP_CALL_LOG` | 可选；SSE 未暴露实际入参时，指向可关联的 MCP 服务端调用记录（JSON 或 JSONL） |
| `NEXENT_EXECUTION_RECORDS` | 可选；SSE 只给 `execution_key` 时，指向平台执行记录导出或 MCP 服务端回执（JSON 或 JSONL），也可传给 `--execution-records` |

实例可用后先跑 `--mode smoke`。**每条记录都要能从平台会话追到工具实际入参、
工具返回和 SQLite 查询结果**：两条核实通过后再跑 24 个任务。第一轮联调交付物是
这两条可追回原始依据的记录，不是又一份「20/20」通过报告。`--mode full` 会校验
同一 `run_id` 下 `nexent_preflight.json` 的 `pass` / `platform_evidence` /
`hit_task_found` / `zero_hit_task_found` 四个字段，任一不满足即退出码 4。仍需拿真实实例确认的只有：
实际网关前缀，以及 `chat/run` 返回的 SSE 工具事件长什么样（尤其是工具调用事件是否
带 `tool_call_id` 与实际入参）。逐条卡点见 `eval/results/platform_probe.json`
与 `platform_status.json`。

## 本轮加固（两项可立即验证的缺陷）

| 问题 | 修复前复现 | 修复后 |
| --- | --- | --- |
| **改库中内容，对账仍 PASS** | 在 DB 副本把一条信号价格改成 `123456.78`，保留原导出和原报告，`verify_tasks.py` 仍 `PASS`、退出码 0 | 退出码 1；`数据库原始行与导出信号逐行七字段一致` 与载荷验真同时 FAIL，报告写明「第 14 行 price: 库='123456.78' 导出='44.99'」 |
| **同一任务只写入一半** | 内存 SQLite 先放 1 行，再提交「新事件＋同 ID 不同价格」，抛 `SignalConflictError` 后行数从 1 变 2 | 行数保持 1，`task_failures` 单独留 1 条；相同内容重投返回 `idempotent_hit=1` 且不增行 |

具体落实：

- **逐行内容比对**：`verify_tasks.py` 用第二个独立只读连接重读全部原始信号，按稳定顺序
  与导出文件的七个业务字段逐行比对，不再只比条数。
- **载荷验真**：打开每个任务实际引用的载荷文件、重算 SHA256，核对 `run_id`、`task_key`、
  工具名 `evaluate_portfolio_snapshot`、实际入参（与冻结清单逐字段一致）、返回信号
  （与库中该任务原始行一致）。零命中任务必须有 2xx 成功调用、`call_success=true`
  和**显式空列表 `[]`**。
- **清单自身锁**：`task_manifest.py --verify` 与实跑入口 `run_nexent_eval.py` 都调用
  `verify_manifest_lock()`；改清单保留旧锁一律拒绝运行。`.gitattributes` 为清单及其
  `.sha256` 固定 `text eol=lf`（仓库原本 `core.autocrlf=true`，锁文件曾出现 CRLF），
  生成锁也改用字节写入固定 LF。
- **原子事务**：`persistence.py` 关闭 sqlite3 隐式事务，改由本模块显式
  `BEGIN IMMEDIATE / COMMIT / ROLLBACK`。一个任务的 `completed` 行、全部信号、全部
  尝试日志同事务提交；中途冲突整批回滚，不留半成品。失败/冲突写进独立的
  `task_failures` 表，不受业务回滚影响。
- **禁止 fixture 冒充端到端**：`--scope end-to-end` 时强制校验载荷
  `platform_evidence=true`、来源属于真实平台白名单、无 `FIXTURE` 标识，且
  `runs.task_manifest_sha256` 等于清单实际哈希、平台版本非空。

反向测试固化在 `eval/nexent/tests/test_hardening.py`：改库中一条价格 / 移走零命中载荷 /
改清单保留旧锁 / fixture 冒充 end-to-end / 冲突整批回滚 / 幂等重投 /
`completed` 状态随信号一起回滚，以及新增的端到端载荷计划/实际入参与配对证据校验，
**十一项全部按预期失败或成立**。

## 第三轮：真实客户端契约修正（修复前后反例）

修复前用本地 mock 网关实测并留证于
`eval/results/northbound_contract_before_fix.json`（`scope=pre-fix-reproduction`），
修复后同一组反例复测结果见 `eval/results/northbound_contract_after_fix.json`
（`scope=post-fix-reproduction`）。两份都是本地 mock，非平台证据。

| 缺陷 | 修复前实测 | 修复后 |
| --- | --- | --- |
| **官方 health 被误拒** | 原样返回 `{"status":"healthy","service":"northbound-api"}`，`probe()` 返回 `health-identity-unconfirmed`（旧版在响应里搜 `nexent` 字样） | 精确校验 `status=healthy` 且 `service=northbound-api`，`probe()` 返回 `reachable`、`contract_matched=true`；`data` 包装的 Agent 列表可枚举 |
| **`tool_error` / 超时 / 计划 A 实际 B 被当成零命中** | `tool_error` 事件带 `tool_result: []` → 返回 `signals=[]`、`signal_count=0`；`error=timeout` 同理；`tool_call` 实际入参为 `P-BOND`、计划为 `P-STOCK`、结果 `[]` → 仍返回 `signals=[]`，完全不检查实际入参 | 分别返回 `evidence_status=failed` / `insufficient`，`signals=None`，缺口写明「工具错误/超时」或「实际入参与冻结清单的计划入参不一致：计划组合='P-STOCK' 实际组合='P-BOND'」 |

具体落实：

- **官方响应契约**：health 精确匹配两个字段；响应头 `X-Request-Id` / `conversation_id`
  单独保存；`conversation_created.content.conversation_id` 支持对象或 JSON 字符串；
  agents 兼容官方 `data` 包装。测试直接使用官方原样响应。
- **同次调用配对**：工具调用事件与结果事件必须共享 `tool_call_id` / `call_id` 等
  调用标识；分别记录 `planned_tool_arguments`（冻结清单）与
  `actual_tool_arguments`（平台工具调用事件，或可关联的 MCP 服务端记录）。
  两者深度比较不一致即证据不足。
- **严格失败判定**：`tool_error`、超时/失败标记、`success=false`、推理与最终输出事件、
  缺少结构化结果、无法核实实际入参、调用标识不匹配，一律不计为成功或零命中。
  只有「工具成功 + 显式 `[]` + 配对成功」才计零命中。
- **MCP 服务端补证**：SSE 不给实际入参时，`NEXENT_MCP_CALL_LOG` 指向的记录必须
  满足「工具名一致 + 返回信号与 SSE 结果逐项相同 + 至少共享一个
  `call_id` / `conversation_id` / `request_id` / `run_id` / `task_key` 标识 + 唯一匹配」。
  不满足则拒绝。MCP 服务可用 `--audit-log` / `--correlation` 产出这类记录（默认关闭）。
- **对账同步收紧**：`--scope end-to-end` 只接受 `topic16-platform-tool-record-v3`，
  强制核对计划/实际入参、实际入参来源、`tool_call_pair` 调用标识与配对方式、
  `tool_success`、`evidence_status=verified`、响应头/会话标识；零命中还要求
  `zero_hit=true`。本地 fixture 仍是 v2，无法冒充端到端。
- **三条契约回归**：官方 health 通过；超时 + 空数组被拒；计划 A / 实际 B 即便返回
  空数组被拒。另有 tool_error、推理事件、调用标识不匹配、MCP 补证正/反例等共 16 项
  北向测试。

## 第四轮：`execution_key` 补证与 smoke 预检门禁（修复前后反例）

复核方验证的联调边界是：**平台 SSE 只返回工具调用和 `execution_key`，即使 MCP 本地
日志里有结果，`0ef85e5` 的客户端仍返回 `insufficient`**。这个保守行为是对的（防止
误报成功），但缺一条「按该键查到该次执行的唯一回执」的补证通路。本轮补齐通路，
并按指令暂缓全量 24 任务。

复现脚本 `eval/nexent/reproduce_execution_key.py` 从历史提交 `0ef85e5` 原样加载旧
客户端，与当前客户端使用**同一组本地构造 SSE 事件与执行回执**，产出：

| 证据 | 结果 |
| --- | --- |
| `eval/results/execution_key_contract_before_fix.json` | 旧客户端：有 `execution_key`、MCP 日志也有结果，仍 `insufficient`、`signals=None`；旧驱动没有 `smoke_preflight_gate`，`--mode full` 可直接跑 24 任务 |
| `eval/results/execution_key_contract_after_fix.json` | 新客户端：唯一命中回执 → `verified`、1 条信号、`pairing=execution-key`；唯一零命中回执 → `verified`、0 条信号；按工具调用标识关联的 MCP 回执 → `verified`；13 种反例全部保持 `insufficient`；门禁缺预检 / 缺零命中 / `run_id` 不一致均拒绝 |

两份都是本地 mock，`platform_evidence=false`，不是平台实跑证据。反例覆盖：无记录、
MCP 日志对不上执行键或调用标识、多条记录无法唯一关联、缺显式成功标记、带错误标记、
实际入参与计划不一致、执行键不来自本次 SSE、fixture 来源、回执结果与 SSE 已有结果
冲突，以及调用标识关联路径的「标识不匹配 / 缺成功标记 / 超时标记 / 多匹配」。

三条证据路径按强度排序，**任一无法唯一关联即保留 `insufficient`**：

1. **SSE 同次调用标识配对**：工具调用事件与结果事件共享 `tool_call_id` / `call_id`，
   结果事件带显式工具名与结构化结果。
2. **MCP 服务端记录补证**：SSE 不给实际入参时，`NEXENT_MCP_CALL_LOG` 记录须工具名
   一致、结果与 SSE 逐项相同、共享至少一个会话/请求/任务标识且唯一匹配。
3. **执行记录 / MCP 服务端回执补证**：SSE 无结构化结果时，按**本次 SSE 显式关联键**
   查记录，两种关联方式都要求唯一匹配：
   - `execution_key`（`execution_id` / `executionKey` 等，不解析自然语言）；
   - 目标工具**调用事件**里的 `tool_call_id` / `call_id`，用于「可唯一关联到该次调用的
     MCP 服务端回执」。结果事件、非目标工具事件或本地日志里自造的标识一律不采信。

   记录还须带显式成功标记、无错误/超时标记、工具名一致、实际入参可核实且与冻结清单
   一致、返回结果为显式数组。SSE 若已给结果，回执必须与之逐项一致，**不能用回执覆盖
   平台结果**。同时带两种标识时以 `execution_key` 配对为准；`pairing` 分别记为
   `execution-key` / `tool-call-id`，`tool_call_ids` 与 `execution_keys` 都写入 v3
   载荷供对账核实关联键确实来自本次 SSE。

落库与对账同步收紧：`actual_arguments_source=execution-record` 写入 v3 载荷，
`tool_call_pair` 允许 `call_id` 或 `execution_key` 并记下 `pairing`；`verify_tasks.py`
与 `check_traceability()` 都要求补证键出现在本次 SSE 采集的 `execution_keys`（执行键
路径）或 `tool_call_ids`（调用标识路径）里；`check_traceability()` 升级为
`topic16-northbound-preflight-v2`，逐条核对平台请求/会话 ID、实际入参与配对、
成功返回值与 SQLite 行、关联键来自 SSE、零命中由「工具成功 + 显式 `[]` + 配对」
共同证明，并输出 `hit_task_found` / `zero_hit_task_found`。

## 实机 smoke 预检采集清单（拿到实例后执行）

**只跑两条**：一条命中、一条零命中，暂不跑 24 任务。每条都要能串起
「平台请求/会话 ID → 工具实际入参 → 成功返回值 → SQLite 查询结果」。

| # | 采集项 | 落到哪里 | 核实方式 |
| --- | --- | --- | --- |
| 1 | 实例地址、`GET /nb/v1/health` 原样响应、报告的版本 / 模型 | `platform_probe.py`、`platform_status.json` | `status=healthy` 且 `service=northbound-api`；版本非空 |
| 2 | 已发布 Agent 名称与 `GET /nb/v1/agents` 结果 | `platform_status.json` | 名称在官方 `data` 列表里 |
| 3 | 本次 `run_id`（两条与后续全量必须同一个） | 命令 `--run-id`、DB `runs` | 手填、可复制；不自动生成 |
| 4 | 响应头 `X-Request-Id` / `conversation_id`，`conversation_created.content.conversation_id` | `platform_ids`、`response_header_ids` | 每条记录至少一个请求/会话 ID |
| 5 | SSE 原始事件（含工具调用事件的实际入参） | `platform_records/<run_id>/task_XX.json`（本机、已忽略） | 载荷哈希写入 `task_runs.tool_response_sha256` |
| 6 | 若 SSE 只给 `execution_key`：该键对应的平台执行记录导出，或带**同一 `execution_key`**、或带 **SSE 工具调用事件同一 `tool_call_id` / `call_id`** 的 MCP 服务端回执 | `NEXENT_EXECUTION_RECORDS` / `--execution-records`（本机、已忽略） | 关联键来自本次 SSE、唯一匹配、显式成功标记、无错误标记、工具名与实际入参一致、显式结果数组 |
| 7 | 工具返回的原始信号数组（命中 1 条，零命中显式 `[]`） | v3 载荷 `tool_result` → `final_signals` | 与 SQLite 中该 `task_key` 的行逐字段一致 |
| 8 | 两条记录的可追溯性报告 | `eval/results/nexent_preflight.json` | `pass=true`、`platform_evidence=true`、`hit_task_found=true`、`zero_hit_task_found=true`，7 项 checks 全 OK；`execution_keys` / `tool_call_ids` 与配对方式一致 |

采集顺序：

```powershell
# 只在运行机器设置，Token 不贴聊天、不进仓库
$env:NEXENT_BASE_URL="https://<实例>"
$env:NEXENT_API_TOKEN="<本机环境变量>"
$env:NEXENT_AGENT_NAME="<已发布的 Agent 名称>"
$RUN_ID = "RUN-20261109T000000Z-abcd1234"
# 若 SSE 只给 execution_key，补一项（也可用 --execution-records 传参）：
$env:NEXENT_EXECUTION_RECORDS="eval/nexent/state/execution_records.jsonl"
python eval/nexent/run_nexent_eval.py --mode smoke --run-id $RUN_ID
python eval/nexent/query_run.py --run-id $RUN_ID
```

两条核实后再用**同一个 `run_id`** 跑 `--mode full`。若 SSE 只有 `execution_key`
且既查不到该键对应的执行记录、也拿不到可唯一关联到该次调用的 MCP 服务端回执，
报告会保留 `insufficient`、退出码 3 并写 `nexent_evidence_gaps.json`；此时不要绕过
门禁，先把第 6 项补齐。MCP 服务端回执可用 `mcps/portfolio_rules/server.py` 的
`--audit-log --correlation` 产出（需含显式成功标记与可关联的调用标识，详见
`mcps/portfolio_rules/README.md`）。

## 两份必须相互核对的证据

只看最终比对报告会漏掉「某个本应零命中的任务根本没执行，报告照样通过」。链路同时留下：

1. **任务执行证据**：`task_manifest.json`（冻结的 24 个逻辑任务 / 48 个持仓格，SHA256 锁
   `79e12464…`）与 `task_runs` 表。每个「组合＋快照时间」单独一行，记录平台返回的
   会话/请求/工具调用标识、原始载荷引用与哈希、返回条数、状态与重试次数。
   **零命中任务也必须 `completed` 且具备平台标识**，且载荷文件必须能打开验真。
2. **最终信号证据**：`final_signals` 表 → 独立进程只读重开 DB →
   `nexent_final_signals.json` → 用原封不动的 `eval/expected_20.json` 比对。

`verify_tasks.py` 同时核对两者及逐行内容一致性，任何一项缺失即判失败。

## 真实平台客户端（v2.8.3 北向 API）

`nexent_client.py` 已按目标版本公开接口重写，**不再发送旧的 `run_id/task_key/input` 字段，
也不再对整个响应做 `json.loads`**：

| 步骤 | 接口 | 校验 |
| --- | --- | --- |
| ① 健康 | `GET /nb/v1/health` | 精确匹配 `status=healthy` / `service=northbound-api`；保存响应头标识；不匹配即停 |
| ② 枚举 | `GET /nb/v1/agents` | 官方 `data` 包装，确认目标 `agent_name` 在已发布列表中 |
| ③ 调用 | `POST /nb/v1/chat/run` | body 只有 `agent_name` + `query`，`Authorization: Bearer`，`Accept: text/event-stream`，解析 SSE；保存 `X-Request-Id` / `conversation_id`，并支持 `conversation_created.content.conversation_id` 关联会话 |

**工具证据原则**：带显式工具名 `evaluate_portfolio_snapshot` 只是起点。工具调用事件与
结果事件必须按同一次调用标识配对，实际入参必须与冻结清单的计划入参深度一致；
只接受已识别的成功工具结果。绝不从模型 `final_answer`、自然语言或任意 `result`
数组推断；`tool_error` / 超时 / 失败标记 / 推理事件 / 缺少结果 / 标识不匹配一律拒绝。
只有「工具成功 + 显式 `[]`」才计零命中。若 SSE 流不暴露实际入参，驱动退出码 3、
写 `eval/results/nexent_evidence_gaps.json`，明确列出缺口（真实工具事件样例，或可关联的
MCP 服务端调用记录：工具名、实际入参、返回信号、时间戳、调用标识），
**不写库、不生成通过报告**。

`run_nexent_eval.py` 默认 `--mode smoke`：按清单顺序调用，直到拿到一条命中和一条零命中，
把两条真实平台记录分别追到原始工具结果与数据库行，输出
`eval/results/nexent_preflight.json` 后停下。`--mode full` 才跑全量 24 个任务，
且必须先通过同一 `run_id` 的预检门禁，否则退出码 4。通过报告仍由后续导出 / 比对 /
对账命令链生成。凭据只从本机环境变量读取，所有落盘证据经 `_redact` 去除凭据类字段。

若 SSE 只暴露 `execution_key` 而没有结构化工具结果，`resolve_tool_evidence()` 会按该键
查 `NEXENT_EXECUTION_RECORDS` / `--execution-records` 指向的执行记录或 MCP 服务端回执；
只有唯一匹配且满足全部条件才计 `verified`，否则 `insufficient`。缺口报告
`nexent_evidence_gaps.json` 会列出该键与缺失项，便于实机按上表补齐。

## 本轮实测（全部为本地自检，不是平台证据）

| 项 | 结果 |
| --- | --- |
| 离线基线 `python eval/run_eval.py` | `TP=20 FP=0 FN=0 duplicates=0 mismatches=0`，退出 0；三把锁 `d33871cd…` / `386fb579…` / `79e12464…` 原样未改 |
| 任务清单 | 24 任务 / 48 格，清单自身锁校验 PASS |
| 本地 fixture 全链路 | 24/24 完成、10 个零命中任务均有可核验载荷、48 格、库 20 行 = 导出 20 条 = 比对器 20 条，`TP=20 FP=0 FN=0`；新版对账 22 项检查全 OK |
| 幂等重试 | 20 → 20 行，事件内容哈希不变，attempt_count 1 → 2，原始载荷哈希未覆盖；T04/T18 同价不同时刻均保留 |
| 测试回归 | `tests` 8 项、`eval/tests` 8 项、`eval/nexent/tests` 57 项，共 73 项，含同库续跑及拒绝错误数据库、篡改载荷的测试 |
| 平台驱动 | 无配置时退出码 **2**，只写 `platform_status.json`；三个端到端文件确认未生成；`--mode full` 无预检时退出码 **4** |
| 北向客户端离线测试 | 官方 health 原样响应、`data` 包装、响应头与会话关联、配对与计划/实际入参、三条契约回归、MCP 补证正反例、`execution_key` / 工具调用标识补证正反例、Token 不泄露 |
| `execution_key` 修复前后反例 | 旧客户端 `insufficient` 且无 full 门禁；新客户端 3 个正例 `verified`（命中 1 条 / 零命中 0 条 / MCP 回执按调用标识关联）、13 个反例全部拒绝、门禁四种情形符合预期 |
| 证据路径 | `eval/results/*.json` 中已无本机绝对路径；导出/对账/比对/探测报告统一写仓库相对路径 |
| 原始平台响应 | `platform_records/` 与 `*.mcp_calls.jsonl` 已加入 `.gitignore`，默认只留本机 |

### SSE 自检（措辞已更正）

本机自检**曾观察到** `mcp==2.3.0` 的 `sse_client` 在 POST `/messages/?session_id=…`
时返回 `404 Not Found`，`tools/list` 未完成；手工 HTTP 请求 `GET /sse` 能拿到 endpoint
事件、POST 返回 202。**原因未证实**，不能归因于 SDK 会话注册时序：复核方用相同 SDK
与相同自检脚本，在默认端口及另一对端口都跑通了 SSE 的工具发现、止损命中和零命中，
另测 15 个并发会话均通过。旧报告只保留外层 `ExceptionGroup` 文本，不足以判定机理。

现在的 `mcp_smoke.py` 会展开异常组到最内层错误、单独摘出 HTTP 错误行，并用
「启动前端口空闲 + netstat 记录监听 PID + 终止本次启动进程后端口释放」确认端口归属
本次启动链（venv `python.exe` 是启动器，实际监听在子进程，不能按 PID 相等硬判）。
本机两组端口的当前结果：`streamable-http` PASS（工具发现 / 命中 / 零命中），
`sse` 仍 FAIL 且如实留证。详见 `eval/results/mcp_smoke.json` 与
`mcp_smoke_alt_port.json` 的 `transports` 数组。平台实际采用哪种 transport
须在拿到的实例上验证。

## 从项目目录可复制的命令

```powershell
# 0) 环境与 MCP 自检（需要 mcp SDK 时用 .venv 的 python）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r mcps/portfolio_rules/requirements.txt
.\.venv\Scripts\python.exe eval/nexent/mcp_smoke.py --startup-wait 3.0

# 1) 固定基线、清单锁、离线回放
python eval/run_eval.py
python eval/nexent/task_manifest.py --verify
python -m unittest discover -s tests -v
python -m unittest discover -s eval/tests -v
python -m unittest discover -s eval/nexent/tests -v

# 2) 本地 fixture 机制自检与修复前后反例（均非平台证据）
python eval/nexent/run_local_fixture.py
python eval/nexent/platform_probe.py
python eval/nexent/reproduce_execution_key.py

# 3) 平台实跑：凭据只从本机环境变量读取
$env:NEXENT_BASE_URL="https://<你的实例>"
$env:NEXENT_API_TOKEN="<本机环境变量，不贴聊天>"
$env:NEXENT_AGENT_NAME="<已发布的 Agent 名称>"
# 可选：$env:NEXENT_API_PREFIX="/nb/v1"; $env:NEXENT_VERSION="2.8.3"
# 两条预检与后续全量必须共用同一个 run_id，这里显式固定：
$RUN_ID = "RUN-20261109T000000Z-abcd1234"
# 若 SSE 不暴露实际入参，另起 MCP 服务时带审计记录，并把路径告诉驱动：
#   .\.venv\Scripts\python.exe -m mcps.portfolio_rules.server --transport streamable-http --port 8769 `
#     --audit-log eval/nexent/state/mcp_calls.jsonl --correlation "{`"run_id`":`"$RUN_ID`"}"
#   $env:NEXENT_MCP_CALL_LOG="eval/nexent/state/mcp_calls.jsonl"
# 若 SSE 只给 execution_key，把该键唯一对应的执行记录 / MCP 回执交给驱动：
#   $env:NEXENT_EXECUTION_RECORDS="eval/nexent/state/execution_records.jsonl"
python eval/nexent/run_nexent_eval.py --mode smoke --run-id $RUN_ID `
  --execution-records "eval/nexent/state/execution_records.jsonl"
# 核对 nexent_preflight.json：pass / platform_evidence / hit_task_found /
# zero_hit_task_found 全部为 true，且两条都能追到
# 「平台会话 -> 实际入参 -> 工具返回 -> SQLite 行」。
# 使用同一个数据库和 run_id。full 会重新读取 smoke 载荷并核对库行，
# 保留已完成任务的证据，只调用余下任务；门禁不通过时返回 4。
# 若 smoke 用了执行回执，full 继续使用更新后的同一回执源。
python eval/nexent/run_nexent_eval.py --mode full --run-id $RUN_ID `
  --execution-records "eval/nexent/state/execution_records.jsonl"

# 4) 全量后的查库 / 导出 / 比对 / 对账 / 重试
python eval/nexent/query_run.py --run-id $RUN_ID
python eval/nexent/export_final_signals.py --run-id $RUN_ID --output eval/results/nexent_final_signals.json
python eval/compare.py --actual eval/results/nexent_final_signals.json --scope end-to-end --report eval/results/compare_report_end_to_end.json
python eval/nexent/verify_tasks.py --run-id $RUN_ID --report eval/results/nexent_task_reconciliation.json
python eval/nexent/verify_retry.py --run-id $RUN_ID --task-key "P-STOCK|2026-11-09T10:00:00+08:00"
```

第 4 步各脚本默认读取平台运行库 `eval/nexent/state/nexent_eval.db`。**核对本地 fixture
必须给每个脚本显式 `--db eval/nexent/state/fixture_demo.db`**，并给 `verify_tasks.py`
传 `--records-root eval/nexent/state --manifest-lock eval/nexent/task_manifest.json.sha256`，
否则会报「数据库不存在」或找不到载荷。

## 文件清单

| 文件 | 用途 |
| --- | --- |
| `task_manifest.py` / `task_manifest.json`(+`.sha256`) | 生成并冻结 24 任务清单；`verify_manifest_lock()` 供实跑入口校验 |
| `schema.sql` | `runs` / `task_runs` / `final_signals` / `attempt_log` / **`task_failures`** |
| `persistence.py` | 显式事务、任务级原子提交、整批回滚、失败单独留证 |
| `nexent_client.py` | v2.8.3 北向 API：官方 health 契约、响应头/会话标识、工具调用配对、计划/实际入参、MCP 补证、`execution_key` 执行记录补证、脱敏 |
| `run_nexent_eval.py` | smoke / full 两种模式、清单锁校验、v3 平台记录、缺口报告、可追溯性预检 v2、full 门禁 |
| `verify_tasks.py` | 清单锁、逐行内容比对、载荷验真、v3 配对证据（含 `execution_key` 必须来自 SSE）、零命中证据、fixture 门禁、相对路径 |
| `export_final_signals.py` / `query_run.py` / `verify_retry.py` | 只读导出、查库、幂等重试验证 |
| `platform_probe.py` / `mcp_smoke.py` / `run_local_fixture.py` / `reproduce_execution_key.py` | 环境探测、MCP 自检、本地 fixture 演示、修复前后反例复现 |
| `tests/test_pipeline.py` / `test_hardening.py` / `test_northbound.py` | 6 + 16 + 33 项测试与明确标注的替身数据 |

`.gitattributes` 为冻结输入、标注、任务清单及其锁固定 LF，避免 Windows autocrlf
改变字节、破坏 SHA256 证据链；`.gitignore` 忽略 `eval/nexent/state/`（二进制 DB 与
替身载荷可一键重建），证据以 `eval/results/*.json` 为准。

## 关键约束如何落实

- **预期标注隔离**：清单、持久化、驱动、导出、重试、客户端源码里不出现冻结标注、
  离线回放结果或被测规则包（由 `test_06` 用 AST + 子串校验）；对账只读 `.sha256` 锁的哈希值。
- **保留 MCP 只读**：`ToolAnnotations(read_only_hint=True, idempotent_hint=True)` 未改；持久化是独立步骤。
- **不抄写数字**：驱动把平台原始响应完整保存为载荷文件，写库时按引用重新读取
  `tool_result`，哈希留证；模型不重算规则、价格、时间、标的、阈值。
- **导出不修饰**：只允许字段同名映射与稳定排序，不用 `DISTINCT`、不去重、不过滤、不补信号、不重算阈值。
- **凭据安全**：Token 只从环境变量读取；`_redact` 去除凭据类键；探测只记录「是否设置」。
- **原始响应留本机**：`platform_records/<run_id>/task_XX.json` 含完整 SSE 原始事件，
  已加入 `.gitignore`；执行记录导出 / MCP 回执（`*execution_records*.json*`）同样默认
  只留本机。仓库只提交 `eval/results/*.json` 里的脱敏摘要、载荷哈希与对账结论。
  证据文件的路径字段统一为仓库相对路径，不含机器绝对路径。
- **不误报成功**：补证关联键只能取自本次 SSE 的显式 `execution_key` 或目标工具
  调用事件的 `tool_call_id` / `call_id`；查不到唯一且合格的执行记录 / MCP 回执时
  保持 `insufficient`，绝不因为本地日志里有结果就推断平台调用成功。缺预检时
  `--mode full` 拒绝运行，避免把「两条未核实的记录」放大成 24 任务通过报告。
- **工具只读不变**：MCP 工具仍标注 `read_only_hint=True`；`--audit-log` 默认关闭，
  开启时只在服务端本机追加调用审计 JSONL，不改动任何行情/持仓数据。

## 范围与后续进度

本轮结论只覆盖「冻结合成行情 → Nexent 编排 → 最终信号落库」止盈止损链路的
**设计与本地机制验证**；在真实平台跑通之前，不构成端到端准确率证据。

| 事项 | 状态 |
| --- | --- |
| 真实实例 smoke 预检：一条命中 + 一条零命中，各自可追回平台会话 / 实际入参 / 成功返回值 / SQLite 行 | **下一步（待平台实跑，暂缓全量 24 任务）** |
| 若 SSE 只给 `execution_key`：该键唯一对应的执行记录或 MCP 服务端回执 | 采集通路已实现，实机证据待补 |
| 全量 24 任务 + 独立复算报告 | 被 smoke 门禁挡住，预检通过后才运行 |
| 微信送达、去重与限频 | 未开始 |
| 舆情 50 条一致率 ≥85% | 未开始 |
| 连续 24 小时漏跑率 0 / 失败重试 | 未开始 |
| 干净实例导入导出、平台导出智能体配置 | 待平台实跑 |
| 正式验收 / PR 合入 main | 未开始 |

数据全部为人工构造的模拟数据，输出仅为风险提示，不构成投资建议。本轮加固与
北向 API 客户端由 WorkBuddy 辅助编写；此前基础代码由 Codex 辅助编写。按活动合规要求，
正式提交须如实记录实际使用的 AI 工具、用途、环节、产物与人工复核。
