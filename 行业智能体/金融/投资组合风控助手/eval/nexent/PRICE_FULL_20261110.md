# 课题 16：真实 Nexent 价格 full 通过及复现说明

北京时间 2026-11-10，服务器会话 A 独立执行与核验。本结论仅覆盖**冻结合成行情的止盈止损链路**。

## 结果

| 项目 | 实测 |
| --- | --- |
| 新 run_id | `topic16-live-20261110-025739` |
| 项目完整 SHA | `63d1a743cf00e7160862a4e1f014f9c5a82320a9` |
| 平台 | openEuler 24.03 LTS-SP2 / ARM64；Docker 28.5.1 / Compose 2.20.0；Nexent v2.8.3 |
| Agent / 模型 | `portfolio_risk_assistant`，发布版本2；Qwen3.8-27B，输出及预留4096 |
| 工具 | `evaluate_portfolio_snapshot`，实际 schema 与发布配置已核对 |
| smoke / full | smoke独立验证4任务（1命中、3零命中），full复用4、新增20，总计24唯一任务、48持仓格 |
| 平台成功零命中 | 10任务；真实成功回执、显式 `[]` |
| SQLite / 导出 / 比对器读取条数 | 20 / 20 / 20 |
| TP / FP / FN | 20 / 0 / 0 |
| 重复 / 字段偏差 / 结构错误 | 0 / 0 / 0 |
| full 退出码 | 0 |
| query / export / compare / verify 退出码 | 0 / 0 / 0 / 0 |
| 逐任务证据 | 24条唯一原生回执；实际入参、目标事件原生ID、文本/结构化返回、载荷哈希、DB逐项一致 |
| smoke未重放/覆盖 | 各原任务参数只有1次原生调用；原任务行、载荷SHA256与mtime、原回执前缀一致 |
| 冻结件 | 五份文件与固定哈希一致，未修改标注 |

四条命令均以独立进程执行。导出本 run 的全部原始信号，仅稳定排序和同名字段映射，没有补算、异常过滤、DISTINCT 或去重。`verify_tasks` 显式指向同一数据库、run、载荷引用根、导出和比对报告。

P-FUND 11:00 本次原生调用摘要 `87c7a2880f5d`，唯一回执、实际参数、显式 `isError=false`、外层正常接受、双通道结果及独立DB重读均一致，返回1条。它证明本次请求成功，不改变历史失败，也不证明已定位历史重分类根因。

## 实际加载版本与测试

- 项目运行源码、客户端和 MCP：固定63d1a743cf00e7160862a4e1f014f9c5a82320a9的隔离检出。服务器通过四反例与两条扩展冲突检查，Nexent回归96项、0skip、exit0；价格MCP SDK回归4项通过。
- runtime 镜像：`sha256:fdc85b4a554be5e42843f8c505f0abf0a20b3c0e44cfecd1a774eb86ac64b681`。
- SDK `managed_mcp.py`：`d64ffb9157d6407398cd481d7af09ddd56abf0b2adef34ebbabf66706fed3d96`。
- SDK `run_agent.py`：`ff63686b478d484beb8ec45e84a460be7c18fd7d8a76df8f172a3334fadb8e95`。
- 本实例 sandbox 命名隔离副本：`4357fee96e6199d152b2e7606d97cdef654de887cb210c385a65a0bc72dd6919`。
- receipt sink：`64fb1f22ce9545910278c6811a715b73dca72b771061425e56382d64ab840815`。
- 本次新 runtime 内执行 `NEXENT_OBSERVATION_REQUIRED=1` 严格回归：17执行/17通过/0skip/exit0。仅测试进程关闭生产sink，隔离RPC测试与24条真实调用证据分别留存。

平台观察适配以项目 `eval/nexent/deployment/` 的清单、补丁和启动组件为基础；sandbox 名称/网络/标签/工作卷的隔离属于部署适配。整份平台源码未复制进项目。真实回执另外证明ID从目标工具事件关联至RPC与原生返回，不能仅用模块文件哈希或单测代替。

## 可复现步骤与外部配置

1. 在新隔离目录按完整 SHA 检出课题路径。无关 LFS 对象可跳过 smudge；验证五份冻结件及锁，不能改变评测输入或标注。
2. 提供可运行的 ARM64 Nexent v2.8.3 镜像、对应 SDK 补丁和启动观察组件。为 runtime/sandbox、网络、卷、回执文件创建独立命名；核对实际挂载与DNS路由，不只依赖 Compose project 名称。
3. 在该实例发布指定 Agent、核对精确工具名/schema，连接本提交的价格 MCP。模型/平台凭据只在服务器私有配置中注入；内部 JWT 必须非空并在相关服务间一致。
4. 驱动所需配置为 `NEXENT_BASE_URL`、`NEXENT_API_PREFIX=/nb/v1`、`NEXENT_AGENT_NAME`、`NEXENT_API_TOKEN`、`NEXENT_VERSION`、`NEXENT_MODEL`、`NEXENT_MCP_URL`、`NEXENT_EXECUTION_RECORDS`。复用有授权的模型服务；这些外部配置和凭据不包含在仓库提交中。
5. 选择新的唯一 run，使用独立 SQLite、预检、日志与回执。当前驱动要求载荷路径位于课题 ROOT 下才能写相对引用，使用已忽略的 `eval/nexent/platform_records/<新run>/`；不要把课题外 state 目录直接用作 `--records-dir`。所有原始载荷仍只留在服务器。
6. 显式传入上述路径运行 smoke。独立重读验证命中和零命中各至少一条，实际运行可能需要遍历多个任务，本次为4个。四项预检布尔值成立后，同 run、同库、同配置继续 full，先封存 smoke 任务/载荷/回执基线。
7. full结束后独立执行以下命令，路径变量应指本次实际位置；`RECORDS_ROOT` 是解析数据库相对载荷引用的课题根目录，不是归档state目录。

```bash
"$PYTHON" eval/nexent/query_run.py --run-id "$RUN_ID" --db "$DB" --output "$QUERY_REPORT"
"$PYTHON" eval/nexent/export_final_signals.py --run-id "$RUN_ID" --db "$DB" --output "$EXPORTED" --trace-output "$EXPORT_TRACE"
"$PYTHON" eval/compare.py --actual "$EXPORTED" --scope end-to-end --report "$COMPARE_REPORT"
"$PYTHON" eval/nexent/verify_tasks.py --run-id "$RUN_ID" --db "$DB" --records-root "$RECORDS_ROOT" --exported "$EXPORTED" --compare-report "$COMPARE_REPORT" --report "$TASK_REPORT"
```

8. 除四条命令之外，用原生回执检查每个任务实际参数与唯一调用、显式错误字段、外层接受、文本JSON与结构化结果，再验证 smoke 从未被重发或覆盖。任一失败均保留首个差异，不把部分结果拼成20/20。

## 证据与历史保护

脱敏摘要见 [nexent_full_real_20261110.json](../results/nexent_full_real_20261110.json)。部署清单 `deployment.json` 与新 runtime 测试 `price-runtime-observation.json` 保留在 A 的交付证据包中。源文件及导出/比对/核验报告的SHA256记录在摘要中。

服务器任务根内相对位置：本次 `state/topic16-live-20261110-025739/`；运行环境 `isolated/topic16-price-b-20261110/`；候选源码 `isolated/releases/63d1a743cf00e7160862a4e1f014f9c5a82320a9/`。原始SQLite、SSE/载荷、原生回执、导出信号和完整日志留服务器，不放进仓库。

历史 `topic16-live-20261109-133209` 仍为 failed、10/24任务、3条信号，16项保护检查全部通过；没有更新其旧结论或拼接结果。原服务继续保留 d91169aa767c1f8793f237383b2dbf6181961dab 加原适配，不被本次隔离源码替换。

本次最初另有 `topic16-live-20261110-025348` 因载荷根外路径配置错误，在真实工具返回后、任务落库前退出1。其0任务/0信号、原始返回、日志和数据库原状态均保留；后续使用完全独立run和回执流，没有复用该返回。

本通过结果不覆盖真实市场数据、舆情人工一致率、知识库业务闭环、真实微信送达、24小时调度或整个集成产品的干净复演。上述工作按后续增量继续；PR #83 保持原状态，不发正式验收。
