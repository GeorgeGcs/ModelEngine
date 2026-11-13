# 第五批集成候选与干净复演

本批是可运行的集成候选，不是平台验收结论。开发代码复用既有 app、SQLite、价格解析、发送所有权和持续调度。真实舆情/KB 集成、稳定平台导出、干净导入、实机 3×3 复演及时延/视频尚待 A。A 固定 dbdf965 的首轮窗口不升级；新版本另开隔离实例/窗口。

## 完整版本与依赖

从交接单完整 SHA 检出 `topic-16-news-risk`；不只按分支最新值部署。课题目录执行：

```bash
python -m pip install -r integration/requirements.txt
python -m integration.manifest --verify-sdk --output "$PRIVATE_EVIDENCE/release-manifest.json"
```

清单动态封存当前完整 Git SHA、课题工作区是否干净、Python 版本、SDK 实际工具 schema、schema 字节哈希、资产版本/hash、代码 LF 规范化哈希及平台观察补丁清单。清单输出到私有证据目录；提交中的代码不能内嵌自身提交哈希。独立环境 Python 3.11+，B 实测 Python 3.12；mcp==2.3.0、mcp-types==2.3.0。原生 HTTP 适配及 app 只依赖标准库。平台自身 SDK 仍是 Nexent v2.8.3 的依赖与补丁，不在生产 runtime 安装本项目 MCP 环境来替换平台依赖。

`dependencies-tested-windows.json` 另记录B实际测试环境的全部依赖版本，只作为开发环境快照，不能当ARM64/Linux平台依赖锁；尤其pywin32不适用于Linux。A封存新实例实际解析后的依赖版本。

精确工具：`evaluate_portfolio_snapshot`、`assess_news_risk_v2`、`search_portfolio_knowledge`。兼容旧 `assess_news_risk` 服务仍可用，但新 Agent 不同时绑定，避免选择歧义。三个 schema 文件均来自真实 SDK list_tools；`--verify-sdk` 再逐字段比对。角色不能通过 MCP 入参指定。

| 外部依赖 | 新隔离环境需重建/记录 |
|---|---|
| Nexent | v2.8.3，源提交 046d77c8c479d30ccd1ed44cd37f727547798d0f；镜像 ID、实际运行模块 hash 由 A 记录 |
| 模型 | 私有模型接入、模型 ID/版本、thinking 配置；历史 Qwen3.8-27B、输出/预留 4096，只作核对基线 |
| 内部鉴权 | runtime/北向私有 JWT 非空且一致；不进入模板或共享清单 |
| KB | 独立物理索引、实际 embedding 模型/维度、源文件保留、专用资产管理员与测试 Agent 权限；见 knowledge/README.md |
| 项目状态 | 新私有 SQLite、新 SSE/回执流、新预检/证据目录；不拷贝旧历史数据库和会话 |
| Git | manifest及rehearsal需Git可执行程序与完整检出元数据，用于绑定完整提交；缺少时拒绝生成版本证明 |
| 推送 | 本集成演示强制 local-mock；应用/调度的 webhook 为独立显式配置，accepted 不代表送达 |

项目端口可用参数覆盖：app 8765、价格 MCP 8768、舆情 MCP 8770、KB MCP 8771。默认 localhost；容器间地址/网络/映射由 A 的私有部署确定。服务没有公开互联网认证边界，部署在受控网络，不对外发布。

## 模板、绑定与私有配置

`agent.template.json` 明确是 template_not_platform_export，不能直接当 Nexent 导出包或宣称导入成功。A 根据模板在新平台创建/绑定实际模型、三工具、私有 KB 权限；固定 Agent 的原生工具 schema，发布后保存真实配置摘要及原件 SHA。稳定后才从平台实际导出 agent.json/包。

| 配置变量 | 含义 |
|---|---|
| NEXENT_BASE_URL / NEXENT_API_TOKEN / NEXENT_AGENT_NAME | 新隔离北向入口、私有 Bearer、实际发布名称 |
| NEXENT_API_PREFIX | `/nb/v1` |
| NEXENT_VERSION / NEXENT_MODEL | v2.8.3 / 实际模型标识，用于封存 |
| NEXENT_EXECUTION_RECORDS | 新 runtime 唯一原生回执文件，绝不指旧 run |
| TOPIC16_RECEIPTS_DIR / TOPIC16_OBSERVATION_RUN_ID | 新 runtime 私有 sink 目录/观察流标识；后者不是业务 run_id |
| TOPIC16_OBJECT_RECEIPTS | 新隔离 runtime 设 1，启用新闻与知识对象回执；价格补丁不变 |
| TOPIC16_KB_BASE_URL / API_TOKEN / INDEX / ROLE | 实际变量名均有 TOPIC16_KB_ 前缀；见第四批说明，服务固定 risk_reader |
| TOPIC16_KB_DB | app 与知识 MCP **同一私有 SQLite**；不同容器须挂载同一文件，不复制库 |
| TOPIC16_INTEGRATION_EVIDENCE_DIR | HTTP 集成入口的私有新证据根目录 |
| PRIVATE_EVIDENCE | 本次隔离演示证据目录，A 自行创建并限制权限 |

凭据仅由私有环境文件加载，不放 CLI 参数、截图或录像。这里的地址、ID 和文件值全部由 A 在隔离环境确定。

## 平台适配与回退

项目 MCP/业务代码在本仓库；平台 SDK 两模块观察补丁、hash、验证与回退步骤在 `eval/nexent/deployment/README.md` 和 observation-manifest.json。对象观察器是 opt-in startup 组件，要求 managed_mcp 的精确已补丁哈希；不接受未知 SDK。启动文件 hash 随清单封存，生产调用 ID 贯穿调度/RPC/sink 仍须原生实测。

严格回归在新 patched Linux runtime 执行 `NEXENT_OBSERVATION_REQUIRED=1 python -m unittest discover -s <完整部署包>/tests -p test_receipt_observation.py -v`，另执行 `test_object_observation.py`；本机 skip 不能当严格通过。回退停止新隔离实例，保留证据后恢复上一完整清单/启动包及新库备份；不覆盖 A 当前窗口或旧失败现场。追加表可留存，不要求破坏性删库。

## 启动与独立核验

先做离线准备检查，再启动 MCP 与 app。检查输出仅证明配置状态，不证明平台导入。

```bash
python -m integration.check --mode local-rules --db "$TOPIC16_KB_DB" --output "$PRIVATE_EVIDENCE/prepare-check.json"
python -m mcps.portfolio_rules.server --transport sse --port 8768
python -m mcps.news_risk.server --transport sse --port 8770
python -m mcps.knowledge.server --transport sse --port 8771
python -m app.server --db "$TOPIC16_KB_DB" --port 8765
```

分别托管上述进程。KB 导入见第四批说明，所有资产 verified 后再切换服务角色。缺源文件/模型、索引未就绪、权限或 schema 不符均是阻断项，不用本地检索兜底。

A 从实际已发布配置或平台导出生成私有 inventory，字段如下；声明需附私有原件 SHA，不能从本模板生成后自称平台结果：

```json
{"platform_version":"v2.8.3","published":true,
 "tools":[{"name":"<ACTUAL_NAME>","inputSchema":{"<ACTUAL_SCHEMA>":"..."}}],
 "knowledge":{"permission_verified":true},
 "provenance":{"kind":"platform-published-config","sha256":"<ACTUAL_PRIVATE_SOURCE_SHA256>"}}
```

```bash
python -m integration.check --mode nexent-integrated --db "$TOPIC16_KB_DB" --ports '' --inventory "$PRIVATE_EVIDENCE/inventory.json" --probe-platform --output "$PRIVATE_EVIDENCE/native-check.json"
```

`--ports ''` 用于服务已启动后的核验；启动前默认端口探测会明确报告占用。真实检查复核 health/已发布名称及 KB chunks 权限/非空索引，检查精确三工具 schema、配置、回执文件和干净业务库。inventory 仍是声明，需 A 保存其原生来源；结果中的 platform_import_proven 始终 false。权限声明不能代替真实拒绝/允许对照。首次业务运行后 clean_business_state=false 是正常保护，不能清库来绕过复演要求。

## 同一次业务运行

本版原生入口将一个业务输入分成价格、新闻和知识查询，各向**同一已发布 Agent**发送一次北向请求，绑定同一 run_id 及各自 task_key；不是一个聊天里自动规划所有工具。每个组合的默认演示为 3 个北向请求。回执出现多次目标调用、冲突、错误、无实际参数或作用域不符均拒绝；不连续重试追求成功。价格使用已有严格解析器；对象必须具有独立原生 raw 封套、显式成功、text/structuredContent 一致和唯一调用 ID。只有工具调用、模型正文键、SSE 成功遮盖原生失败、其他 run 回执均拒绝。

```bash
# 真实平台；私有目录必须全新
python -m integration.runner --case integration/cases/stock-demo-down.json --mode nexent-integrated --db "$TOPIC16_KB_DB" --run-id "$RUN_ID" --evidence-dir "$PRIVATE_EVIDENCE/native-single" --output "$PRIVATE_EVIDENCE/native-single-summary.json"
# 本地规则；若请求 KB 但未配置，将明确记录 configuration_error，不自动回退
python -m integration.runner --case integration/cases/stock-demo-down.json --mode local-rules --db "$LOCAL_DEMO_DB" --run-id "$LOCAL_RUN_ID" --evidence-dir "$LOCAL_EVIDENCE" --output "$LOCAL_OUTPUT"
```

本地模式想单独演示规则时，可把独立演示输入复制到私有目录并显式设 knowledge_queries=[]，记录此范围；不把它称为 KB 闭环。新原生入口不计算本地价格/新闻结果作兜底。其他已实现类别（日异动、集中度、流动性）只在 local-rules app 计算，不能合并声称原生已验证。

HTTP 对应 `POST /api/integrated-monitor`，body 为 `{case,run_id,mode}`；现有 `/api/monitor` 仍是本地规则入口。查询 `GET /api/business-run?run_id=...` 可重读输入、逐组件计划参数/原生回执、新闻对象（含合法无风险）、检索原文位置、最终信号和模拟推送。页面新增新闻时间/来源、知识查询及业务证据查看。

数据库版本 4：在已有追加的推送/调度/知识表之上新增 business_components、business_signals、topic16_schema_versions；旧 signals 按原 run_id 补建关联，不改信号内容。相同 event_id 在新运行仍可追踪，原信号/推送不覆盖或重发；signal_links.created=false 表示复用，原信号 detail 保留第一次证据，本次输入与检索看本次 components/knowledge。已有 run_id 拒绝覆盖。无依据与错误均可令 review_required；只有明确错误/缺证据令 evidence_complete=false，合法 no_match 不等于无风险。

## 随机复演、计时与录像

候选池为 6 个独立合成演示输入，不含冻结正式集或答案。先用固定种子按股票/基金/混合各随机选一例，将输入及 SHA 封存，再每例执行三次；不看结果后挑样本。

```bash
python -m integration.rehearsal plan --seed 161118 --output "$PRIVATE_EVIDENCE/rehearsal-plan.json"
python -m integration.rehearsal run --plan "$PRIVATE_EVIDENCE/rehearsal-plan.json" --mode nexent-integrated --db "$TOPIC16_KB_DB" --evidence-dir "$PRIVATE_EVIDENCE/rehearsal-runs" --output "$PRIVATE_EVIDENCE/rehearsal-result.json"
```

3 例×3次=9业务运行，默认27次北向请求；各自新 run_id，不拼价格 full/旧运行。计划被修改则拒绝；失败保留，脚本仍执行预定其余案例，不额外重试。方向比较含风险类型/等级、新闻判定和知识状态；同事件重复运行不会重复模拟发送。端到端时延包含业务执行和持久化/模拟推送。首字时延仅计收到首个非空 `final_answer.content` 字符串事件；心跳、思考、工具输出不算。它是平台可见回答事件口径，若当前 UI 提前显示其他已证实类型，A 应另行记录可见性映射；缺支持类型就保留 null，不伪造首字。逐工具请求时延、业务首个可见事件和整个业务耗时分别记录。

干净复演顺序：全新依赖与权限→导入已验证平台实际导出→重新绑定脱敏占位符→核对真实工具/KB/权限→封存新完整清单→运行上述随机9次→独立重读业务记录。不能携带旧 SQLite、会话或预置成功数据。导出前后私有原件与脱敏文件分别算 SHA；两者不是逐字相同。B 收到 A 脱敏导出后再做结构/敏感信息检查，目前不提供假 agent.json。

录像建议 3–8 分钟：展示版本与合成/模拟边界；导入三组合；选择一条实际执行；展示实际参数、工具回执、引用原文及信号；查询模拟推送和去重；说明另两组合复演记录及未验收项。只展示脱敏摘要，不展示原始 SSE、凭据、内部地址、私人路径或人工作业。视频由 A 实机录制，本批没有视频成品。

持续调度继续使用 `python -m app.scheduler` 及第二批说明，保持已有计划/时隙/尝试统计。`nexent-price` 窗口只证明价格链路，不能因为新集成入口存在就升级为新闻/KB窗口；本批未改它的执行模式。下一窗口如需包含对象工具，应先独立验证并明确新计划范围。
