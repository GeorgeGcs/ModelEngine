# 2026-11-09 真实平台阈值评测：full 未通过

本轮只覆盖冻结合成行情的止盈止损链路。真实 smoke 已通过；随后在同一服务器、环境、数据库和 `run_id=topic16-live-20261109-133209` 上接续 full，因缺少目标工具调用证据停止。本报告不构成课题全部功能或正式验收通过。

机器可读结果见 [脱敏实跑摘要](../results/nexent_full_real_20261109.json)。原始 SSE、SQLite、执行回执、审计日志、模型连接、凭据、内部地址和私人主机路径保留在原服务器。

## 实际验收结果

| 核对项 | 本次真实结果 |
| --- | --- |
| 唯一组合 × 快照任务 | 完成 10 / 24；原 smoke 4 个，本轮新增 6 个 |
| 持仓格 | 已完成 20 / 48 |
| 停止时剩余任务 | 1 个已尝试但证据不足，13 个尚未尝试 |
| 已完成任务中的成功零命中 | 7 个，具有成功回执与显式空数组 |
| SQLite 原始行 / 导出 / 比对器读取 | 3 / 3 / 3；全量期望为 20 |
| TP / FP / FN | 3 / 0 / 17 |
| 重复 / 字段不符 / 结构错误 | 0 / 0 / 0 |
| full / query / export / compare / verify 退出码 | 3 / 0 / 0 / 1 / 1 |
| 原 smoke 保护 | 4 个任务行、载荷字节哈希和修改时间均未变，未再次调用 |
| 原预检、冻结锁、原回执 | 原预检和三份冻结锁未改；原回执字节前缀保留 |

首个证据缺口为 `P-FUND|2026-11-09T11:00:00+08:00`。请求返回 HTTP 200，完整采集了 305 个 SSE 事件并出现 `final_answer`，但没有可关联的目标工具调用 ID 或执行键，因此驱动保留 `insufficient`、退出 3，未将该任务写成成功零命中。模型回答不用于补工具证据。

比对器的首个差异是标注 `T04`：`P-FUND | 2026-11-09T03:00:00Z | SIM-C | stop_loss` 缺失，预期价格 `1.8999`、阈值 `1.9000`。17 个缺失包含未完成任务的预期信号，不能据此把差异全部归因为规则计算错误。

## 实跑版本与代码归属

实跑 Git 基线为 `d91169aa767c1f8793f237383b2dbf6181961dab`，运行时保留了以下必要适配。每个实跑组件的 SHA256 记录在脱敏摘要的 `execution_code_sha256`；整理后的提交可通过当前 Git 历史定位。

| 归属 | 改动及目的 |
| --- | --- |
| 本项目 `mcps/portfolio_rules/server.py` | 保留规则计算、校验、审计及输入/输出 schema；把同一真实结果同时作为结构化数组和 JSON 文本块返回。成功空数组也有文本 `[]`，兼容 `mcpadapt 0.1.19` 对空 `content` 的拒绝。 |
| Nexent v2.8.3 部署适配 | [deployment](deployment/README.md) 中的两模块 SDK diff、启动 hook 和私有 sink；在原生 MCP 返回边界记录实际参数、显式成功字段和平台原生调用 ID。 |
| 原评测客户端与门禁 | 本轮没有修改匹配规则、冻结数据、标注、清单、入库、导出或验收判定。 |

运行时观察默认关闭。它只在原始返回明确为 `isError=false`、存在结构化数组、同一调用的外层执行正常接受且未超时/取消时写成功回执；失败、缺字段及迟到返回不会升级为零命中。SDK patch 和启动文件按实跑字节保留，不把整份 Nexent 源码放入本项目。

## 从本提交复现所需的外部配置

1. 提供 Nexent v2.8.3 实例、Linux/ARM64 可用的容器环境及规则 MCP 所需 Python 3.11+ / `mcp==2.3.0`。本项目不附带平台、镜像或模型。
2. 按 [部署适配说明](deployment/README.md) 校验并加载实际安装 SDK 的观察 overlay。仅修改平台源码目录不会改变非 editable 安装的 SDK。保留 overlay 配置，后续重建 runtime 仍需带上它。
3. 让 runtime 能访问规则 MCP 的 `/mcp` 地址，注册、启用并绑定 `evaluate_portfolio_snapshot`，保存并发布 Agent。本次 Agent 名称为 `portfolio_risk_assistant`。MCP 不应向公网开放无鉴权接口。
4. 配置适合平台 CodeAgent 协议的模型。本次使用 Qwen3.8-27B；真实连通检查通过并不足以证明会调用工具。将模型输出上限和预留量显式配置为 4096；本版本旧字段 `max_tokens=0` 会回填为 1 token，不能视为无限。本次采用提供方默认思考模式。
5. 在 northbound、runtime（以及需要内部转发的 config 服务）配置一致、非空的内部 JWT 密钥。v2.8.3 speed 模式也需要此项；`SUPABASE_JWT_SECRET` 优先于 `JWT_SECRET`。实际值仅保存在部署环境。
6. 在评测进程配置 `NEXENT_BASE_URL`、`NEXENT_API_PREFIX`、`NEXENT_AGENT_NAME`、`NEXENT_API_TOKEN`、`NEXENT_VERSION=2.8.3` 和持续追加的 `NEXENT_EXECUTION_RECORDS`。不要将 Token 写入命令参数、日志或仓库。精确校验 health 为 `status=healthy`、`service=northbound-api`，再枚举已发布 Agent 的实际名称。

## 路径与命令

先从原 smoke 的私密摘要、命令记录及预检确认实际路径：`DB` 是原数据库文件，`RECORDS_DIR` 是驱动载荷目录，`PREFLIGHT` 是原通过的预检文件，`EXECUTION_RECORDS` 是实时追加的原生回执源，`OUT` 是服务器私密输出目录。证据目录不等于数据库目录。变量赋值中的私人路径只保存在运行环境。

从成功 smoke **首次**接续 full 的命令为：

```bash
.venv/bin/python eval/nexent/run_nexent_eval.py --mode full --run-id "$RUN_ID" \
  --db "$DB" --records-dir "$RECORDS_DIR" --preflight-output "$PREFLIGHT" \
  --execution-records "$EXECUTION_RECORDS"
```

本轮 full 停止后，实际以独立进程执行了以下四条命令，未补算、过滤异常或使用 DISTINCT 去重：

```bash
.venv/bin/python eval/nexent/query_run.py --run-id "$RUN_ID" --db "$DB" \
  --output "$OUT/query_run.json"
.venv/bin/python eval/nexent/export_final_signals.py --run-id "$RUN_ID" --db "$DB" \
  --output "$OUT/final_signals.json" --trace-output "$OUT/export_trace.json"
.venv/bin/python eval/compare.py --expected eval/expected_20.json \
  --actual "$OUT/final_signals.json" --scope end-to-end --report "$OUT/compare_report.json"
.venv/bin/python eval/nexent/verify_tasks.py --run-id "$RUN_ID" --db "$DB" \
  --manifest eval/nexent/task_manifest.json --manifest-lock eval/nexent/task_manifest.json.sha256 \
  --records-root "$TASK_DIR" --exported "$OUT/final_signals.json" \
  --compare-report "$OUT/compare_report.json" --expected-lock eval/expected_20.json.sha256 \
  --scope end-to-end --report "$OUT/verify_tasks.json"
```

`TASK_DIR` 是课题根目录；`--records-root` 用来解析数据库中已有的相对载荷引用，不能直接替换为 `RECORDS_DIR`。原始 query、export trace 和 verify 报告可能包含内部地址或私人路径，只在服务器保留；仓库提交的是白名单脱敏摘要。

## 当前失败运行的后续限制

现有驱动支持从已完成且与预检一一对应的 smoke 首次接续 full，不支持失败 full 再次接续。当前 run 状态为 `failed`，数据库已有 10 个任务，而原预检仍对应 4 个任务，直接再次 full 会被续跑复核拒绝。

后续继续此运行前，需要处理实际工具调用证据缺口，并评审显式的失败运行续跑能力。不要手工把 run 改回 `completed`、改写预检、删除数据库或替换 run_id 拼接结果。本轮已按此边界保留现场。

## 必要改动的回归结果

| 回归 | 实际环境 | 结果 |
| --- | --- | --- |
| MCP 文本/结构化命中、显式空数组、错误、schema | 原 MCP 2.3 虚拟环境中的独立进程 | 4/4，0 skip，退出 0 |
| capture、实际 patched wrapper、超时/取消/晚到返回、私有 sink | 原 Nexent v2.8.3 Linux runtime 中的独立进程，隔离 RPC | 17/17，0 skip，退出 0 |
| overlay 已有输出保护、错误基线及补丁篡改拒绝 | Windows Python 3.12，工作区私有临时目录 | 3/3，退出 0 |
| 原有规则与回放单元测试 | Windows Python 3.12，工作区私有临时目录 | 8/8，退出 0 |

前两组没有访问真实模型、Agent 或评测 SQLite；执行前后评测数据库的字节哈希相同。
上述代码回归通过不改变本轮真实 full 未通过的结论。
