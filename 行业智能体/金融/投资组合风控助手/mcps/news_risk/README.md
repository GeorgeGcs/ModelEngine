# 舆情风险研判 MCP 服务

第三批保持两个公开工具名与输入schema，补行政处罚覆盖、输入时间校验及可选对象回执适配。
精确SDK导出见[schema.json](schema.json)，启动与A隔离验证见[第三批说明](../../docs/舆情第三批交付_20261110.md)。
缺发布时间不填造时间；输出temporal_evidence=missing。合法无风险仍返回对象，错误/取消不能补成无风险。

课题 16 验收标准第 3 条配套的研判工具，供 Nexent 编排调用。**本服务与止盈止损链路完全独立**：
不导入、不修改 `mcps/portfolio_rules`、`portfolio_risk`、`eval/nexent/state/` 或任何冻结行情/标注文件。

## 工具契约

`assess_news_risk(news)` / `assess_news_risk_v2(news, entities)` —— 均为只读、幂等
（`read_only_hint=True`, `idempotent_hint=True`）。v1 用内置 `SIM-*` 主体；v2 用调用方传入的
`entities` 主体映射（`{标的代码: {"canonical": 主体全称}}`），适用于外部样本的动态主体。

入参：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `symbol` | 是 | 监控标的代码：`SIM-A` / `SIM-B` / `SIM-C` / `SIM-D` |
| `text` | 是 | 舆情原文（虚构模拟文本） |
| `sample_id` | 否 | 稳定 ID，便于按 ID 对齐评分 |
| `published_at` | 否 | 带时区 ISO 8601 时间 |
| `source` | 否 | 模拟来源名 |

返回值每条至少五项，另附可追溯字段（表内为 `structuredContent.result` 里的单条研判）：

| 字段 | 取值 |
| --- | --- |
| `symbol` | 与入参一致 |
| `risk_verdict` | `risk` / `no_risk`（二分类，供独立评分脚本对齐） |
| `evidence` | 命中词附近的**原文证据片段** |
| `level` | `高` / `中` / `低` / `待复核` / `无` |
| `review_action` | 建议复核动作（不含买卖结论） |
| `evidence_status` | `sufficient` / `negated` / `clarified` / `historical` / `insufficient` / `entity_mismatch` / `denial_confirmed_recovery` / `partial_denial_confirmed` / `not_applicable` |
| `reason_codes` | 判定理由列表 |
| `profile` | 研判版本（`v2` 工具与批量 CLI 的逐行输出携带；`v1` MCP 工具本身不返回此字段，由 CLI 在系统输出层补齐） |
| `disclaimer` | 「风险提示仅供参考，不构成投资建议」 |

**返回体契约（2026-11-09 按真实平台实跑加固）**：两个工具都返回
`CallToolResult`，`isError=false`、`content[0].text` 是**完整 JSON 文本**、
`structuredContent.result` 是**同一份对象**。即使判定为 `no_risk`，文本块也不会是空的
`[]` 或空 content，必须与结构化结果逐字一致。原因是主开发分支 `e25f800` 的
Nexent v2.8.3 实跑证实 `mcpadapt 0.1.19` 会拒绝空 `content`，平台侧依赖
`content[0].text` 取工具结果；此前本工具只返回普通 `dict`，上平台很可能同样无法被消费。
非法输入仍抛 `ToolError`，**不会**伪造一个成功空结果。`Annotated[CallToolResult, dict]`
保证对平台声明的 output schema 与原来的 `dict` 一致（有回归测试锁定）。
CLI 评分链路走 `python -m news_risk`，不经过 MCP，故这次改动不影响任何评测分数。

研判规则见 `news_risk/rules.py` / `news_risk/rules_v2.py` 与
[标注说明](../../docs/舆情风险标注说明.md)：v1 按「主体解析 → 否定反转 → 官方澄清 →
旧闻 → 传闻 → 证据不足 → 负面事实」判定；v2 按「**事件主体归属** → 部分否认（确认的是
不利事实还是恢复正常）→ 主体/关联排除 → 证伪澄清 → 口径解释 → 假设 → 历史 →
泛述 → 证据不足 → 否定 → 不利进展」判定。主体归属是正确性前提，连 `baseline=True` 也生效：
原文点名其他主体时输出 `no_risk` / `entity_mismatch`，不给本次监控标的预警。
两者只用标准库、不调用模型、不联网，因此同输入重复执行结论一致。

**v2 词表由本批 50 条开发评测原文归纳**：`eval/news_risk/vocab_overlap.py` 修正提取器
（补上带类型注解的 `ADVERSE_PATTERNS`）后，对 `0334111` 的规则版本复算为
**143/178 = 80.34%**（此前报的 71/105 = 67.62% 漏统计了这一组），当前规则为
**164/216 = 75.93%**。故 v2 的 `50/50` 只是同批样本上的开发评测结果，
**不是泛化能力证明，也不是与人工判定一致率**。正式验收前须写定使用哪个工具/样本/
profile 并先提交封存记录，不得事后按结果切换；正式盲评推荐未被开发的自造集 `NR-2026-11-09`。

在 Nexent 平台添加服务并刷新工具列表后，应能同时看到 `assess_news_risk` 与
`assess_news_risk_v2`；绑定哪个工具由评测时写定的 profile 决定。

## 安装与启动（项目根目录）

可复用已有 `.venv`；若单独安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r mcps/news_risk/requirements.txt
```

```powershell
# stdio：本地调试 / 平台 Container 方式
.\.venv\Scripts\python.exe -m mcps.news_risk.server --transport stdio

# streamable-http：Nexent Remote URL 推荐（默认 8770，避开规则工具 8768）
.\.venv\Scripts\python.exe -m mcps.news_risk.server --transport streamable-http --host 0.0.0.0 --port 8770

# SSE（曾在本机 SDK 下观察到 404，原因未证实；按目标 Nexent 版本实测后再选）
.\.venv\Scripts\python.exe -m mcps.news_risk.server --transport sse --port 8771

# 返回体 wire 契约回归（须用装了 mcp==2.3 的 .venv）
.\.venv\Scripts\python.exe -m unittest discover -s mcps/news_risk/tests -v
```

`mcps/news_risk/tests/test_transport.py` 的 6 项测试锁定：显式 `isError=false`、
`content[0].text` 为可反序列化的完整 JSON、`structuredContent.result` 与之逐字一致、
`no_risk` 也有非空文本块、非法输入抛 `ToolError` 不伪造成功、输出 schema 未变。
这些是**本地 SDK 层**验证，不等于平台实证。

容器化部署时，容器内 `127.0.0.1` 指向容器自身；应让 MCP 监听 Nexent 后端可达的地址
（Docker Desktop 可试 `host.docker.internal`），不要把无鉴权服务直接暴露公网。
在 Nexent 平台按官方 MCP 接入文档添加 Remote 服务、做连通性测试并刷新工具列表，
确认出现 `assess_news_risk` / `assess_news_risk_v2`，再把它绑定到支持工具调用的模型与
「投资组合风控助手」Agent。真实实例接通后须**另留舆情工具调用记录**，
尤其要确认平台能正确解析返回体里的 `content[0].text`（结构化结果是否完整到达 Agent），
不能只凭「工具列表可见」就认定链路可用。

> **主开发分支实跑给出的边界条件**（`e25f800` / `REAL_PLATFORM_RUN.md`）：
> 真实 smoke 双样本通过，但同 `run_id` 的 full 只完成 10/24、SQLite 3 条、
> `TP/FP/FN=3/0/17`；首个缺口是某任务 HTTP 200、305 个 SSE 事件、有 `final_answer`，
> 却拿不到可关联的目标工具调用 ID / 执行键，驱动保守保留 `insufficient` 并停止。
> **模型回答不能用来补工具证据。** 因此舆情工具上平台后，除 `content[0].text` 可解析外，
> 还须能追到该次调用的实际入参、显式成功标记与返回结果；缺任一证据都应判证据不足，
> 而不是用 Agent 的自然语言总结顶替。

本分支**尚未**在真实实例注册，平台侧绑定状态为「待平台实跑」。

## 评测隔离红线

- 两套 50 条样本（`data/news_risk/samples_50.json`、`data/news_risk/dev_eval_samples.json`）
  及其答案**不得**写入提示词、知识库、工具参数或 Agent 配置；本工具的代码与默认配置里
  没有任何评测样本答案，v2 词表也不得写死样本 ID 或样本公司名。
- 用户本机的 `舆情参考判断_50条.json` 由 **AI 起草、未经独立人工判定**，只留在运行机器，
  不入库；仅 `eval/news_risk/score_dev_eval.py` 在预测校验通过后读取。
- `data/news_risk/dev_samples.json` 是独立开发样例（`DEV-` 前缀），其 `expect` 仅为工程自测期望，
  **不是人工金标**，不参与 50 条一致率评分。
- 系统输出须在人工标注封存后生成，再由 `eval/news_risk/score.py` 独立对齐计算。

## 合规

全部为虚构模拟数据，未使用真实企业、人物或敏感数据。输出定位为辅助建议，
重大舆情须人工核实，不构成投资建议。本模块由 WorkBuddy 辅助编写。
