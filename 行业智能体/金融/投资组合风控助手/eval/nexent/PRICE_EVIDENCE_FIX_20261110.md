# 价格证据认证修复：普通调用与跨来源冲突

基线：`712da82c895dcf35b88c03578879777f55566fc4`；过程分支 `topic-16-news-risk`。
本批只修 `nexent_client.py` 的证据认证及增加项目内回归，不改变业务规则、冻结件或历史实跑报告。
所有本批测试均为本地合成 SSE/回执与本地 HTTP 替身；没有执行真实 Nexent、模型或推送。

## 最小输入与前后结果

实际参数取冻结 `task_manifest.json` 中
`P-FUND|2026-11-09T11:00:00+08:00` 的 `tool_arguments`。
工具名 `evaluate_portfolio_snapshot`，调用 ID 为合成标识 `AUDIT-CALL`。
反例里的 `[]` 是证据认证输入，不是该任务预期业务答案。

| 证据输入 | 712 修复前 | 本批修复后 |
| --- | --- | --- |
| 单个普通 `tool_call` 有实际参数、调用 ID 和 `result=[]`，无独立返回 | verified / zero_hit=true | insufficient / evidence=null |
| SSE 含独立 call/result，原生同 ID 回执为 `tool_success=false,status=failed,error=TimeoutError` | verified / zero_hit=true | insufficient / evidence=null |
| 合法独立 call/result，明确返回 `[]` | verified / zero_hit=true | verified / zero_hit=true |

A 提供的外部独立复现脚本：修复前退出码 1，修复后退出码 0；三项结果如上。
两条缺陷也已固化到仓库 `tests/test_price_completion.py`，不依赖外部脚本才能复测。

## 认证规则

- 普通 call/request 事件中的内嵌数组不进入返回候选，须有独立返回或唯一原生回执。
- 明确结果类型（如 `tool_result`、`tool_completed`）可表示完成合并事件；通用
  `tool` / `mcp_tool` 及自定义白名单事件还须显式成功标记。`isError` 缺省值不补成成功。
- 在 SSE 配对、MCP 补证、执行键补证三条成功路径之前检查同次失败，按可信调用 ID
  或执行键关联。已关联的失败不能因来源类型未知或缺失而被筛掉，也不能被 SSE 成功覆盖。
- 完成合并事件和结果事件的调用 ID 同样参与失败关联，防止通过省掉独立 call 绕过检查。
- 除失败状态/超时标记外，原始 MCP `isError=true`、`outer_result_accepted=false` 也阻断成功。
  外层正常接受错误封套并不等于语义成功；`isError=true` 仍失败。
- 无关调用的失败、明确属于其他运行的回执不否定独立有效 SSE；其他运行的回执自身不能补证。
  `observation_run_id` 不作为评测 `run_id`。

## 已执行验证

Windows / 系统 Python 3.12.7，无新增依赖。在课题根目录执行：

```bash
python -B -m unittest discover -s eval/nexent/tests -p test_price_completion.py -v
python -B -m unittest discover -s eval/nexent/tests -v
```

| 验证 | 实际数 | skip | 退出码 |
| --- | ---: | ---: | ---: |
| 新增回归对未修改 712 源码 | 16 方法，26 个失败子用例 | 0 | 1 |
| 新增回归对修复源码 | 16 通过 | 0 | 0 |
| 完整价格回归（包含上述 16 项） | 96 通过 | 0 | 0 |
| A 的独立反例脚本，修复后 | 2 反例拒绝 + 1 合法对照通过 | 不适用 | 0 |

完整套件首次在受限沙箱中出现临时目录/本机 HTTP 权限失败，未算通过；随后将 TEMP/TMP
固定到 B 的独立验证目录，在允许回环 HTTP 的进程中完成上表 96 项验证。
临时库、载荷和完整日志只留本机验证目录，不入库。

已有回归继续覆盖正文伪执行键、仅调用无返回、原生成败冲突、其他 run/task 回执、
缺显式成功标记、合法命中/零命中、错误/超时、持久化原子性及 smoke/full 接续门禁。
新增回归包含同 ID/执行键冲突、MCP/执行记录交叉冲突、错误封套、完成合并事件及无关失败对照。

## 冻结件核验

五份文件的工作区字节哈希与 712 Git 对象、各自锁文件均一致；锁文件自身字节也未变。

| 文件 | SHA256 |
| --- | --- |
| data/eval_20.json | d33871cd2dcc2ba18964cdb28f10543e5a392e4329c6bf1353aa6c7c3e983b71 |
| eval/expected_20.json | 386fb579b50f34a5841700c2e92efb3fcaec7b3c9be289d563ee642ded65418e |
| eval/nexent/task_manifest.json | 79e12464adbfbb9696fdbb9a41b482de241aea0a8488eaadccb4b629e3c1c338 |
| data/news_risk/samples_50.json | 22dfb8652a0d5b6d69bb611525bccc97a591c5c2e4f1b959e65ad86b08635878 |
| data/news_risk/dev_eval_samples.json | 11f92d6db0294eb6931e8cc55d62ddd61fba9edbdb50e84ae28dbd6214794767 |

## 交给 A 的启动配置与未验证项

没有新增依赖、CLI 参数或环境变量。A 按完整提交 SHA 隔离检出，先执行上述回归及独立反例。
真实运行继续使用其私有 `NEXENT_BASE_URL`、`NEXENT_API_TOKEN`、`NEXENT_AGENT_NAME`
以及独立 `NEXENT_EXECUTION_RECORDS` / `--execution-records`。本批未变更 MCP 或平台观察补丁。

A 确认门禁、实际工具 schema、容器加载版本、唯一回执来源及五份冻结件后，另开新 run、
数据库、载荷及预检，真实命中和零命中 smoke 成立后再同 run full。不得向旧失败 run 拼结果。

本批未验证 Linux patched runtime、真实 Nexent smoke/full、24 小时调度、真实微信送达、
人工评分、知识库或干净实例复演；均不能由 96 项本地回归推定通过。
WebhookGateway、调度持久化、舆情行政处罚及知识库接入列为后续独立批次。

## A 后续独立接收（2026-11-10）

以上为 B 第一批交付时的验证边界。A 已在固定63d1a74上完成新 run 的真实 smoke/full，
24任务、48格、三方20/20/20、TP20/FP0/FN0；见 [实跑报告](PRICE_FULL_20261110.md)。
不重复运行或覆盖旧失败现场，其他未验证项继续保留。
