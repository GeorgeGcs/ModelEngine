# 投资组合风控助手

评审入口见 [交付索引](docs/delivery/README.md)，含脱敏原生 Agent、部署、操作说明、实机视频、截图和逐项自测报告。

2026-11-13：候选 `f742ef1` 按 wpp 人工终版完成正式评分，**43/50（86%）**，`formal=true`、`pass=true`。Windows 项目回归 469/469，0 跳过，高危告警 0。待完成新版实机复验、微信通道、仓库 CI 回执及第二轮观测收尾。[正式评分报告](docs/delivery/舆情复评_20261113.md) · [交付清单](docs/delivery/交付差距与收尾计划_20261112.md)

实测代码 `aeafe7e52193fefb3d5b9e0c25995b825e7de81f` 已在 Nexent v2.8.3 完成三组合三工具联调。干净实例复演 9/9，27 个原生调用回执可核验。10 组业务用例及 1000 条持仓批量导入结果见 [验收报告](docs/delivery/验收报告.md)。

[原生知识库接入](knowledge/README.md)包含三份自编合成资产与可启动 MCP。运行部署、精确 schema、复演与时延证据见 [部署与复演](docs/delivery/部署与复演.md)。

第三批已补行政处罚语义、正式评分v4的代码/时序门禁和可选舆情对象观察适配，见
[第三批交付与迁移](docs/舆情第三批交付_20261110.md)。本轮人工依据已确认，评分结果见上方更新。
首轮 24 小时窗口固定 `dbdf965`，144 个时隙均有尝试，139 个成功回执，5 个未知归入失败。见 [封窗结果](docs/delivery/window-results.json)。

魔擎开发者激励 1 期 · 课题 16。当前版本提供价格阈值规则、离线行情回放、舆情研判、知识检索及 Nexent MCP 接入。早期与 AI 参考答案的开发评测、本轮与 wpp 终版人工复核的评测分别留档；最新正式人工一致率为 86%。示例组合、标的、价格和舆情均为虚构数据。风险提示仅供参考，不构成投资建议。

**2026-11-10：A 已独立验真价格 full（冻结合成行情）。** 固定 `63d1a743cf00e7160862a4e1f014f9c5a82320a9`，
新 run `topic16-live-20261110-025739`：复用4＋新增20＝24任务/48格，零命中10任务；
DB/导出/比对器20/20/20，TP20/FP0/FN0、全部差异0、四项退出码0。
见[新实跑报告](eval/nexent/PRICE_FULL_20261110.md)及[摘要](eval/results/nexent_full_real_20261110.json)。
推送回执/并发及持续调度入口见[第二批交付](docs/调度与推送交付_20261110.md)。首轮 24 小时统计见上方封窗结果；第二轮最终结果待服务器恢复后读取，真实微信尚未验真。

**2026-11-09 实机状态：真实双样本 smoke 已通过；同 run_id 的 full 未通过。**
原 4 个任务复用、本轮新增 6 个，完成 10/24；SQLite / 导出 / 比对器均为 3 条，
TP/FP/FN 为 3/0/17。首个缺口为基金组合 11:00 快照没有可关联的目标工具调用证据。
详见[实跑与复现说明](eval/nexent/REAL_PLATFORM_RUN.md)及[脱敏摘要](eval/results/nexent_full_real_20261109.json)。

## 运行第一个回放

在项目根目录使用 Python 3.11 或更高版本。离线回放和规则测试仅用 Python 标准库。

```powershell
python -m portfolio_risk data/demo.json
python -m unittest discover -s tests -v
```

回放按输入顺序，对每张行情快照和每个组合计算一次。价格小于或等于止损线时产生 `stop_loss`，大于或等于止盈线时产生 `take_profit`。查看输出的 `signal_count` 和 `signals`，可逐条核对事件编号、组合、时间、标的、规则、价格和阈值。缺失持仓行情或阈值配置错误会中止回放，并向标准错误输出原因。

演示数据的预期结果为 3 个组合、7 张快照、4 条信号，其中止损和止盈各 2 条。规则当前判断的是某个时刻是否满足阈值。连续两个时刻处于同一阈值区间可以分别产生信号。后续推送层负责按规则处理重复告警及限频。

## 项目入口

| 路径 | 用途 |
| --- | --- |
| `portfolio_risk/rules.py` | 确定性规则，由 CLI 与 MCP 共用 |
| `portfolio_risk/__main__.py` | 读取回放文件并输出 JSON 结果（正常回放入口） |
| `data/demo.json` | 快速演示：股票、基金、混合三个组合及对应的合成行情 |
| `data/eval_20.json` | 冻结评测集输入：3 组合、8 快照 |
| [冻结阈值评测集](eval/README.md) | 20 个触发点标注、独立复算与反向测试 |
| [Nexent 编排 → 落库 → 独立复算](eval/nexent/README.md) | 任务执行证据、信号导出与对账；价格真实 full 已通过；历史失败保留 |
| [规则 MCP 服务](mcps/portfolio_rules/README.md) | 安装可选 SDK、运行服务及配置平台接入 |
| `tests/` | 规则边界与回放验证 |
| [舆情盲标与一致率评测](eval/news_risk/README.md) | 50 条虚构样本、盲标表、研判模块与独立评分器及正式评分结果 |
| [舆情人工标注说明](docs/舆情风险标注说明.md) | 一页判定规则、五类必判非风险与提前固定的评分口径 |
| [舆情研判 MCP 服务](mcps/news_risk/README.md) | 供 Nexent 调用的 `assess_news_risk` 工具 |
| [本地可操作演示](docs/本地演示与验收对照.md) | `app/` 三组合闭环页面、运行命令、风险类别实现与验证情况、微信真实接入步骤 |
| `app/server.py` / `app/demo.py` | 标准库 HTTP 接口与一键三组合演示（全部经公开接口，不插库、不预置成功状态） |
| [CHANGELOG](CHANGELOG.md) | 提前交付阶段各独立增量的范围、验证与复现基线 |
| [技术方案与里程碑](docs/技术方案与里程碑.md) | 平台编排、交付范围、验收证据和排期 |

## 冻结阈值评测集（离线回放层）

离线评测使用合成行情 `data/eval_20.json` 和独立标注 `eval/expected_20.json`。两个文件分别用 SHA256 锁定。20 个预期触发事件和 28 个非触发格覆盖了等于阈值、阈值两侧精度边界，以及同标的在不同组合采用不同阈值的情况。

```powershell
python eval/run_eval.py
python -m unittest discover -s eval/tests -v
```

第一条命令从正常回放入口产出 `eval/results/replay_actual.json`，再由**不导入被测包**的 `eval/compare.py` 逐格比对，输出 TP / FP / FN 和差异明细；第二条包含删一条、添一条、重复一条三个反向测试。

**本轮成果仅证明离线回放层的阈值判定准确性，不是端到端准确率证据。** 接入 Nexent 后须用原封不动的同一份标注核对编排后的最终持久化信号，届时才有端到端证据。详见 [eval/README.md](eval/README.md)。

## Nexent 编排证据链

`eval/nexent/` 已实现两份相互核对的证据：① 24 个逻辑任务是否真的全部执行；② 从数据库重新读出的最终信号是否确实是那 20 条。真实平台状态以本页顶部与实跑报告为准。下文为此前离线机制加固记录；历史 `scope=local-fixture` / `local-mcp-smoke` 产物仍是本地验证，不能代替本轮真实平台证据。

按复核意见完成两项加固：

- **对账不再只数条数**：从 SQLite 独立重读全部原始行与导出文件逐行比对七个业务字段；打开每个任务引用的载荷文件重算 SHA256，核对任务、工具名、实际入参、返回信号；零命中须有 2xx 成功记录与显式空列表。清单自身 SHA256 锁由实跑入口校验，`.gitattributes` 固定清单与锁为 LF 防止 autocrlf 破坏哈希；`--scope end-to-end` 禁止本地替身冒充。
- **任务级原子提交**：`completed` 状态、全部信号、尝试日志在同一事务提交，冲突整批回滚，失败单独写入 `task_failures`。修复前「改库中一条价格仍 PASS」「冲突后行数 1→2」两个反例均已复现并固化成反向测试。

真实平台客户端按 Nexent v2.8.3 北向 API 官方契约实现：`GET /nb/v1/health` 精确校验
`{"status":"healthy","service":"northbound-api"}` → `GET /nb/v1/agents`（官方 `data` 包装）→
`POST /nb/v1/chat/run`（`agent_name` + `query`、Bearer、SSE）。保存响应头
`X-Request-Id` / `conversation_id`，并支持从 `conversation_created.content.conversation_id`
关联会话；工具调用与结果必须按**同一次调用标识**配对，分别记录计划入参与实际入参，
只接受已识别的成功工具结果。`tool_error`、超时、推理事件、缺少结果或实际入参
无法核实一律判为证据不足；只有「工具成功 + 显式 `[]`」才计零命中。SSE 未暴露
实际入参时，可用可关联的 MCP 服务端调用记录补证（`NEXENT_MCP_CALL_LOG`）。
证据不足则退出码 3 并写缺口报告。默认 `--mode smoke` 先交付一条命中、一条零命中且
可追回原始依据的真实记录，再考虑全量。

第三轮复核发现并已修复的两个契约缺陷（修复前后反例见
[eval/nexent/README.md](eval/nexent/README.md)）：官方 health 原样响应被旧版
「响应里必须含 nexent 字样」的检查误拒；`tool_error` / 超时 / 计划组合 A 而实际
调用组合 B 的调用，即便返回空数组也被计为合法零命中。原始 SSE 响应只留在运行机器
本机（`platform_records/` 已忽略），仓库证据只保留脱敏摘要、哈希与对账报告，
且证据里的路径全部改为仓库相对路径。

第四轮补上用户验证的联调边界：**SSE 只给「工具调用 + `execution_key`」时**，
即使 MCP 本地日志里有结果也不能推断平台调用成功。`0ef85e5` 保守返回
`insufficient` 是对的，本轮新增按 SSE 显式关联键查唯一执行记录 / MCP 服务端回执的
补证通路（`NEXENT_EXECUTION_RECORDS` 或 `--execution-records`）：关联键可以是
`execution_key`，也可以是目标工具调用事件的 `tool_call_id` / `call_id`（用于
「可唯一关联到该次调用的 MCP 服务端回执」），两者都只能来自本次 SSE；记录须唯一
匹配、带显式成功标记、无错误标记、工具名与计划/实际入参一致、返回显式数组；
与 SSE 已有结果冲突时不得用回执覆盖平台结果。无法唯一关联一律保留 `insufficient`。
修复前后同组输入的对照证据见
`eval/results/execution_key_contract_{before,after}_fix.json`（本地 mock，非平台证据）。

全量 24 任务的首次接续门禁要求同一 `run_id` 已有 `pass` /
`platform_evidence` / `hit_task_found` / `zero_hit_task_found` 全为 true 的
`nexent_preflight.json`，否则退出码 4。smoke 预检每条记录必须串起平台请求/会话 ID、
实际工具入参、成功返回值与 SQLite 查询结果；实机采集清单见
[eval/nexent/README.md](eval/nexent/README.md)。

```powershell
python eval/nexent/task_manifest.py --verify
python eval/nexent/run_local_fixture.py
python eval/nexent/reproduce_execution_key.py
python -m unittest discover -s eval/nexent/tests -v
python -m unittest discover -s eval/tests -v
python -m unittest discover -s tests -v
```

平台可用后需按 [eval/nexent/README.md](eval/nexent/README.md) 在本机环境变量配置 `NEXENT_BASE_URL` / `NEXENT_API_TOKEN` / `NEXENT_AGENT_NAME`（Token 不贴聊天、不进仓库），再运行驱动脚本；从平台导出智能体配置也在实跑成功之后进行。

## 舆情风险信号（终版人工复核已完成，正式评分通过）

独立分支 `topic-16-news-risk` 交付验收标准第 3 条所需工程链路。**两条线必须分开看**：

| 线 | 参考 | 结果 | 能否作为验收证据 |
| --- | --- | --- | --- |
| 开发评测 | 用户本机**AI 起草、未经人工判定**的参考判断 | v2 `50/50`；修复前 `47/50`（误报 2、漏报 1） | **不能**，只记「与 AI 参考答案的一致率」 |
| 修复候选复评 | 用户确认的 wpp 终版判定 | **43/50（86%）**，误报 2、漏报 5、结构错误 0 | 正式评分通过，证据校验无缺项 |

- `data/news_risk/blind_annotation_sheet.csv`（自造集 `NR-2026-11-09`，**正式盲评推荐**）与
  `blind_annotation_sheet_dev_eval.csv`（外部 50 条，沿用 `N001`…`N050`，**只作开发评测**）：
  均为空白盲标表；填好的 `human_labels.csv` 留本机、不提交。
  仓库**不提供**自造集 50 条标准答案，AI 生成的标签不是人工金标。
  外部开发评测集的原文已用于归纳词表、标签可从公开开发报告还原，
  `sample_registry.json` 已标为「不推荐盲评」，正式评分器会拒绝为它出具 `formal=true`。
- `news_risk/` + `mcps/news_risk/`：确定性研判规则（v1 / v2）与只读 MCP 工具
  `assess_news_risk` / `assess_news_risk_v2`，每条输出标的、风险判定、原文证据、等级、建议复核动作；
  v2 先做**事件主体归属**再做部分否认语义，覆盖否定反转、主体错配、证据不足。
  两个 MCP 工具按主开发分支真实实跑经验改为 `content[0].text`（完整 JSON）+
  `structuredContent.result` 双通道返回，本地 SDK 6/6 wire 回归通过；平台实证仍待实例。
- `eval/news_risk/score_dev_eval.py`：开发评分器，预测结构校验通过后才读 AI 参考答案；
  报告只给 `ai_reference_match_count` / `ai_reference_match_rate`，不给达标布尔值。
- `eval/news_risk/score.py`：**默认 `--label-source unverified`**，任意两列 CSV 都只能算出一致条数，
  `formal=false`。要 `formal=true` 必须显式 `human-annotation`，并提供标注前提交的封存记录、
  运行前登记的样本清单（`--sample-registry`）、写定的 profile、哈希与标注人证据链。
  脚本只能核对记录是否齐全，标签由谁独立作出仍须靠真实复核过程。
- `data/news_risk/acceptance_preregistration.json`：**独立预登记摘要**固定正式候选
  `NR-2026-11-09`、样本与 `sim_entities.json` 主体映射哈希、`profile=v2`、分母 50、门槛 43，
  并列出已公开答案开发集的禁用指纹；评分器内置常量交叉核对，自供清单不能洗白开发集。
  正式评分 v3 把 **`formal`（证据链有效）/ `meets_85_percent`（≥43）/ `pass`（两者同时成立）**
  拆开：真实 42/50 记为 `formal-not-met`，如实记录「正式未达标」而非「评测无效」。
  人工表须 50 行逐行带 `symbol`；`--fixture-mode synthetic-fixture` 只供工程自测，
  报告强制标 `not_acceptance_evidence=true`，正式验收不得使用。
  两个评分器在失败运行中都让同路径旧报告就地失效，避免「异常后磁盘上仍是通过报告」。
- `eval/news_risk/vocab_overlap.py`：过拟合诊断。修正提取器（补 `ADVERSE_PATTERNS` 的带注解赋值）后，
  对 `0334111` 的规则复算为 **143/178 = 80.34%**（旧值 71/105 = 67.62% 漏统计了这一组）；
  当前规则为 **164/216 = 75.93%**。两个数字都说明 `50/50` 属**同批拟合**，
  不能外推到未见样本，正式验收须另作独立人工判定并建议增加留出集。

```powershell
python data/news_risk/build_sheet.py
python -m news_risk data/news_risk/samples_50.json --validate-only
python -m unittest discover -s eval/news_risk/tests -v
python eval/news_risk/score.py
```

最后一条在人工表不存在时只输出 `pending-human-annotation`（退出码 2）；
人工表存在但缺条目 / 有非法值 → 退出码 1 `fail`；证据链不全 → `unverified-source`；
证据链齐备但不足 43 条 → 退出码 1 `formal-not-met`。四种情况都**不生成正式通过报告**。
完整命令见 [eval/news_risk/README.md](eval/news_risk/README.md)。
本轮实测：舆情测试 119/119、全套回归 215/215；该记录属于当时的开发评测，最新正式成绩见上方复评报告。

审计 P2 四项已修（修复前后实测对照见
[eval/news_risk/results/p2_audit_before_after.json](eval/news_risk/results/p2_audit_before_after.json)，
可用 `eval/news_risk/repro_p2_audit.py` 对 `git archive 2786c70` 的干净树复算）：
v1 其他主体负面事件误报、v2 部分产线恢复吞掉残留不利事实、冻结样本跑 `--profile v2` 示例命令退出 1、
无时区的未来 `sealed_at` 拿到 `formal=true`。正式评测若写定 v2，须同时传
`--entities data/news_risk/sim_entities.json`（只含主体全称 / 别名，冻结样本不改动，
SHA256 由登记清单与系统输出头三方核对）；封存记录的 `sealed_at` 必须带显式时区且不得晚于评分时刻。
这些改动只提高工程正确性，**不产生任何正式验收结论**。

## 本地可操作演示（三组合闭环）

`app/` 用 Python 标准库提供可点击页面与公开 HTTP 接口，不引入 Web 框架：

```powershell
$P = '.\.venv\Scripts\python.exe'
& $P -m app.server --host 127.0.0.1 --port 8765   # 浏览器打开 http://127.0.0.1:8765/
& $P -m app.demo --port 8766 --no-serve            # 一键跑完三组合闭环并打印三条可查回记录
& $P -m unittest discover -s app/tests -p "test_*.py" -v
```

页面支持股票 / 基金 / 混合组合的手工录入、批量导入、修改持仓、重启后查询与「立即监控」；
信号展示组合、风险类型、触发时间、数据或舆情原文、等级与建议复核动作，
顶部与每条推送消息均显著标注「风险提示仅供参考，不构成投资建议」。
风险类别逐项实现与验证情况见 [本地演示与验收对照](docs/本地演示与验收对照.md)：
止盈止损复用冻结评测集同一规则；单日异动、集中度、流动性为确定性判定；
舆情复用 v2 研判规则。**流动性的「模拟成交量 < 持仓数量」是演示口径，不是真实盘口。**

推送侧提供可替换的 MCP 适配器契约：默认 `LocalMockGateway` 明确标 `SIMULATED`，
不发起真实外部请求；`WebhookGateway` 给出真实接入模板，凭据只从部署机环境变量读取。
去重（`event_id + 接收人` 唯一键）、限频（窗口上限，抑制也落库）、
故障恢复（只原地重试 `failed/pending`，已送达不重发）均由 SQLite 事务实现并留有回归。
实测一次演示：三组合各有一条可查回记录，重复触发 7 条全部去重，
限频抑制 6 条，故障恢复重试 3 条送达且再次重试被拦截。

**边界**：本地模拟网关不证明真实微信送达。真实 Nexent 三工具编排、MCP 注册、原生导出和干净复演已有独立实测；首轮 24 小时观测及本轮人工评分也已有结果，具体失败与待补项见交付差距，不将局部通过写成整体验收通过。

## 接入与交付

价格全量、知识库检索、三工具集成、原生导出和干净复演已取得实测证据。当前优先将已达到一致率数值门槛的舆情候选纳入交付版本，复验受影响链路，再推进微信通道。仓库 CI 和第二轮观测结果按可用条件收口。本仓库当前用于过程开发，正式成果按活动规范通过 PR 合入 AgentsHub 的 `main`。

采用 MIT 许可。基础代码、演示样例和测试由 Codex 辅助编写，冻结评测集与复算脚本由 WorkBuddy 辅助编写，Codex 参与了逐格复核、冻结校验和重新检出验证；舆情样本、标注说明、研判模块与独立评分器由 WorkBuddy 辅助编写，50 条人工判定由标注者本人填写，不由 AI 代填。详见[评测说明](eval/README.md)。

[活动与提交规则](https://atomgit.com/org/ModelEngine/discussions/26) · [官方需求包](https://atomgit.com/ModelEngine/AgentsHub/blob/main/需求包详情与交付要求.md)
