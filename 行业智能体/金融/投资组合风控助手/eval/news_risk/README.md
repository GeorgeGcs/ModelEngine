# 舆情风险信号：开发评测与正式验收（50 条样本）

2026-11-12：wpp 终版人工复核已确认并封存，本地预测和独立评分完成，31/50（62%），误报 1、漏报 18、结构错误 0，数字未达标。先前参考信息披露保留说明。详见[本轮报告](../../docs/delivery/人工复核结果_20261112.md)，不再将此轮标为等待人工填写。

2026-11-10第三批：当前评分报告为v4、正式封存使用新增登记v2模板。历史v1登记缺代码/时序证据，
不能补造历史后授予正式结论。新步骤以[迁移说明](../../docs/舆情第三批交付_20261110.md)为准；
正式候选固定NR-2026-11-09/profile=v2。CLI增加--sample-set-id和--run-id，逐行绑定代码及运行。
显式合成夹具恒formal=false/pass=false，machine_chain_valid/engineering_would_pass只用于工程测试。
下文开发集数字为历史开发报告，不代表本次真人评测，缺真人文件仍pending。

> ## 当前状态
>
> | 线 | 状态 |
> | --- | --- |
> | **本轮人工复核评测** | 用户确认的 wpp 终版：31/50，未达至少 43/50。预测退出 0、评分退出 1，独立进程复算一致。来源说明单列；原评分器保留 `formal=false / unverified-source / pass=false`，没有改写评分门禁 |
> | **开发评测**（与 AI 参考答案一致率） | **已完成**：v2 规则 50/50、误报 0、漏报 0；同版本开关对照 47/50、误报 2、漏报 1 |
> | **复核整改** | 已修审计 P0（正式评分器默认来源）与主体归属 / 部分否认 / 结构校验 / 词表统计等缺陷，见下 |
>
> 开发评测的参考判断由 **AI 起草、未经独立人工判定**，本轮结果只记作
> **「与 AI 参考答案的一致率」**，**不是**活动要求的「与人工判定一致率」，**不可作为验收证据**。
> AI 自动生成的标签不是人工金标。

对应官方需求包课题 16 验收标准第 3 条（第 705 行），以及交付要求第 3 / 7 条。
判定规则与评分口径见 [docs/舆情风险标注说明.md](../../docs/舆情风险标注说明.md)。

## 两套 50 条样本与「运行前登记」

| | 自造冻结集 | 用户提供的开发评测集 |
| --- | --- | --- |
| ID | `NR-001`…`NR-050` | `N001`…`N050` |
| 输入 | `data/news_risk/samples_50.json`（锁 `22dfb865…`） | `data/news_risk/dev_eval_samples.json`（锁 `11f92d6d…`） |
| 空白盲标表 | `blind_annotation_sheet.csv` | `blind_annotation_sheet_dev_eval.csv` |
| 主体 | `SIM-A`…`SIM-D` | `NEWS-A`…`NEWS-D` |
| 参考判断 | 无（仓库不提供答案） | 用户本机 `舆情参考判断_50条.json`，**AI 起草、未经人工判定，不入库** |
| 登记编号 | `NR-2026-11-09` | `NEWS-DEV-2026-11-09` |
| 是否可作正式盲评 | 可以（未参与 v2 词表归纳、标签未公开） | **不可以**：已用于归纳 v2 词表，且仓库内对照报告 + 三条差异足以还原全部二分类标签 |

`data/news_risk/sample_registry.json` 是**运行前登记**的样本集清单，由 Git 提交历史证明选定时间；
正式评分器只认清单里登记且哈希一致的集合。清单已把开发评测集标为
`not_recommended_for_blind_acceptance=true`，即使有人伪造封存记录也拿不到 `formal=true`。
**单靠样本旁边的 `.sha256` 锁不能证明评测集是事先选定的。**

另新增 `data/news_risk/acceptance_preregistration.json`：**独立预登记摘要**，把正式候选
（`NR-2026-11-09`）、样本哈希、`sim_entities.json` 主体映射哈希、`profile=v2`、分母 50、
门槛 43 一次性写定，并列出禁用指纹（`NEWS-DEV-2026-11-09` 的开发集）。
`score.py` 内置同一份关键常量并与该文件交叉核对；自供 `--sample-registry` 只能补充记录，
**不能重新登记或洗白已公开答案的开发集**。命中禁用指纹即使把清单里
`not_recommended_for_blind_acceptance` 改成 `false` 也一律拒绝出具 `formal=true`。

盲标表实际为 **10 列：6 列预填**（`sample_id` / `symbol` / `target` / `published_at` / `source` / `text`）
**+ 4 列空白**（`human_verdict` / `human_rationale` / `annotator` / `annotated_at`）。
标注人不要删 `target` 列，也不要往预填列里写判定。

## 交付物

| 路径 | 用途 |
| --- | --- |
| `data/news_risk/prepare_dev_eval.py` | **只读原文文件**做字段映射，生成开发评测输入、SHA256 锁与空白盲标表；不读参考判断 |
| `data/news_risk/dev_eval_samples.json` + `.sha256` | 开发评测输入：50 条原文 + 4 个主体映射，**无任何答案字段** |
| `data/news_risk/sample_registry.json` | 运行前登记的样本集清单（含哈希与盲评资格） |
| `data/news_risk/acceptance_preregistration.json` | **独立预登记摘要**：固定正式候选样本/实体映射哈希、profile、分母、门槛与禁用指纹；评分器内置常量交叉核对，自供清单无法覆盖 |
| `data/news_risk/annotation_registration.template.json` | 人工标注**封存记录模板**：须在标注前复制、填写并提交 |
| `data/news_risk/sim_entities.json` | v2 独立主体映射（只含全称 / 别名，无答案）；冻结样本不含 `entities`，正式评测用 `--entities` 指定，SHA256 由登记清单与系统输出头三方核对 |
| `news_risk/rules.py` | 研判 v1：内置 `SIM-*` 主体；**主体归属先于风险词匹配**，其他监控主体的负面事件判 `entity_mismatch` |
| `news_risk/rules_v2.py` | 研判 v2：先做**事件主体归属**，再做部分否认语义 / 主体关联排除 / 证伪澄清等；恢复与不利事实并存时按子句保留残留不利事实；`baseline=True` 关闭两类语义例外用于同版本开关对照 |
| `news_risk/__main__.py` | 批量 CLI，`--profile v1\|v2`、`--baseline`、`--validate-only`、`--entities`；两条路径共用同一套输入校验 |
| `mcps/news_risk/server.py` | 只读幂等 MCP 工具 `assess_news_risk`（v1）与 `assess_news_risk_v2`（[说明](../../mcps/news_risk/README.md)） |
| `eval/news_risk/score_dev_eval.py` | 开发评分器：预测校验失败直接退出码 3、**不打开参考判断、不写新报告**；**同路径旧报告会就地失效**（`topic16-dev-eval-report-invalidated-v1`）；报告只报 `ai_reference_match_count` 等数字，**不含任何达标布尔值** |
| `eval/news_risk/score.py` | 正式验收评分器 v3：默认 `unverified`，只能算一致数；独立预登记 + 完整证据链才可能 `formal=true`；**有效性 / 达标 / 通过三态分离**；失败运行让旧正式报告失效；`sealed_at` 须带显式时区且不得在未来；`--fixture-mode synthetic-fixture` 仅供工程自测 |
| `eval/news_risk/vocab_overlap.py` | 过拟合诊断：支持 `AnnAssign`，可指定历史规则文件复算词表重合率 |
| `eval/news_risk/repro_p2_audit.py` | 审计 P2 四项问题的修复前 / 修复后可复现对照（只读，可对任意干净检出树复算） |
| `eval/news_risk/results/dev_eval_consistency_{baseline,report}.json` | 开关对照 / 修复后开发评测报告（含误报漏报与对应原文摘录） |
| `eval/news_risk/results/vocab_overlap{,_at_0334111}.json` | 当前 / 上一提交规则版本的词表重合诊断（`diagnostic_only=true`） |
| `eval/news_risk/results/p2_audit_before_after.json` | 四项 P2 缺陷的修复前后实测对照（合成 fixture，非验收证据） |
| `eval/news_risk/tests/` | 119 项测试（v1 规则 15 + v2 规则 28 + 正式评分 35 + 开发评分 28 + **正式评分补强 13**） |
| `eval/news_risk/tests/test_acceptance_scoring.py` | 显式合成夹具：独立预登记、禁洗白开发集、人工表逐行标的、42/50 正式未达标、43/50 通过、旧报告失效；全部标 `synthetic_fixture=true` / `not_acceptance_evidence=true` |

## 开发评测（与 AI 参考答案）

**时序纪律（代码强制）**：`python -m news_risk` 只读 `dev_eval_samples.json` 产出预测；
`score_dev_eval.py` 阶段 1 只校验预测（漏项、重复、额外 ID、五字段、非法枚举、标的错配、
**嵌套答案字段**、**证据必须是原文子串**、可选 `--expect-profile` 核对研判版本），
任一不通过即退出码 3、**不打开参考判断文件、不写报告**（用运行时 `Path.read_text` 监听直接验证）。
只有阶段 1 全过，阶段 2 才读用户本机的 AI 参考判断。
每次运行写 `<report>.status.json`，带 `run_id` / 退出码 / 输入摘要 / `report_valid` /
`previous_report_invalidated`。**失败运行不写新报告，但会就地覆写同路径的旧报告**
为 `topic16-dev-eval-report-invalidated-v1`：顶层 `report_valid=false`，不再暴露
`ai_reference_match_count`，旧内容整体挪进 `stale_report` 供审计。
结构错误导致退出码 1 时同样处理。这样即使消费方只读报告文件、忽略 sidecar，
也不会把上一次的 50/50 当成本次结果；修好输入重跑成功即可写回有效报告。

```powershell
$SRC = $env:TOPIC16_DEV_INPUT_DIR # 本机私有原文/开发参考材料目录，不提交路径和值
$REF = Join-Path $SRC '舆情参考判断_50条.json'
$P   = '.\.venv\Scripts\python.exe'

# 0) 只从原文重建开发评测输入与空白盲标表（可选）
& $P data/news_risk/prepare_dev_eval.py --source (Join-Path $SRC '舆情输入_50条.json')

# 1) 预测：只读原文
& $P -m news_risk data/news_risk/dev_eval_samples.json --profile v2 --validate-only
& $P -m news_risk data/news_risk/dev_eval_samples.json --profile v2 `
     --output eval/news_risk/results/dev_eval_predictions.json

# 2) 评分：预测校验通过后才读参考判断
& $P eval/news_risk/score_dev_eval.py `
     --predictions eval/news_risk/results/dev_eval_predictions.json `
     --reference $REF --report eval/news_risk/results/dev_eval_consistency_report.json `
     --expect-profile v2 --run-id DEV-EVAL-V2 --profile v2 --variant final
```

同版本开关对照（`--baseline` 关闭「部分否认语义」与「主体/关联排除」两类例外，
主体归属是正确性前提，两种模式都生效）：

```powershell
& $P -m news_risk data/news_risk/dev_eval_samples.json --profile v2 --baseline `
     --output eval/news_risk/results/dev_eval_predictions_baseline.json
& $P eval/news_risk/score_dev_eval.py `
     --predictions eval/news_risk/results/dev_eval_predictions_baseline.json `
     --reference $REF --report eval/news_risk/results/dev_eval_consistency_baseline.json `
     --expect-profile v2-baseline --run-id DEV-EVAL-BASELINE `
     --profile v2-baseline --variant baseline
```

**结果**（本轮修完主体归属与部分否认后重新复跑，数字未变，因为这 50 条原文都点名了各自标的）：

| 版本 | 与 AI 参考答案一致 | 误报 | 漏报 | 结构错误 |
| --- | --- | --- | --- | --- |
| `v2-baseline`（开关对照） | 47 / 50 = 94% | 2（N003、N024） | 1（N038） | 0 |
| `v2`（修复后） | **50 / 50 = 100%** | 0 | 0 | 0 |

三条差异：N003 同名公司受罚且明确无关联、N024 已卖清且无衍生品头寸（均应不触发），
N038 否认「全厂停产」但确认四分之一产能暂停（仍应触发）。完整原文与依据在两份报告里。

> **泛化局限（必须如实说明）**：词表高度贴近本批原文。修正提取器后，
> 对上一提交 `0334111` 的规则版本复算为 **143/178 = 80.34%**（与复核方结论一致；
> 旧提取器漏了带类型注解的 `ADVERSE_PATTERNS`）；本轮修复后的规则版本为
> **164/216 = 75.93%**（分母变大是因为新增了主体归属与部分否认词表）。
> 两个数字都说明 50/50 是**同批拟合**，不能外推到未见样本。
> 复核方曾用 20 条定向反例测得旧版仅 9/20，但反馈里只给出两句，已纳入本轮回归；
> **未拿到全部 20 条、也未做独立留出集验证，因此不能声称整体泛化已改善**。
> 正式验收须另作独立人工判定，并强烈建议增加留出集。
>
> ```powershell
> & $P eval/news_risk/vocab_overlap.py                      # 当前规则版本
> & $P eval/news_risk/vocab_overlap.py --rules <历史版本文件> # 复核任意提交
> ```

## 正式验收（待独立人工标注）

**默认不授予正式结论**：`--label-source` 默认 `unverified`，任意两列 CSV（包括把 AI 参考判断
转换而来、不填标注人/时间/复核记录的表）都只能算出一致条数，`formal=false`、
`not_acceptance_evidence=true`。要 `formal=true` 必须**同时**具备：

1. `--label-source human-annotation`；
2. `--registration` 指向**标注前生成并提交**的封存记录（样本哈希、人工表哈希、独立复核者、
   `independent_review=true`、封存时间、写定的规则 profile）；
3. 样本集在 `--sample-registry` 里登记、哈希一致，且**未被标记「已公开开发标签」**；
4. `--profile`、封存记录、系统输出头与每行预测的 profile 一致；
5. 样本锁通过、人工表若提供 `symbol` 则逐行一致、证据是原文子串、无结构错误；
6. 上述证据链有效才可能formal=true；一致条数≥43/50另决定meets_85_percent，二者共同决定pass。

报告会列出 `formal_requirements_missing`，逐项说明为什么没给正式结论。
脚本只能核对记录是否齐全；**标签究竟由谁独立作出，仍须靠真实复核过程**。

顺序：

1. 选定样本集（推荐 `NR-2026-11-09`）与 profile，**先写并提交封存记录**，再请一位
   没看过 AI 参考答案、系统输出或开发报告的复核者**只看原文**填写 CSV，另存
   `data/news_risk/human_labels.csv`（留本机、不提交），封存后补记人工表哈希。
2. 用写定的 profile 生成系统输出。
3. 独立评分：

```powershell
& $P -m news_risk data/news_risk/samples_50.json --profile v2 `
     --entities data/news_risk/sim_entities.json `
     --output eval/news_risk/results/system_output.json
& $P eval/news_risk/score.py --samples data/news_risk/samples_50.json `
     --human data/news_risk/human_labels.csv `
     --system eval/news_risk/results/system_output.json `
     --report eval/news_risk/results/consistency_report.json `
     --label-source human-annotation --profile v2 `
     --registration data/news_risk/annotation_registration.json `
     --sample-registry data/news_risk/sample_registry.json
```

`data/news_risk/samples_50.json` 是冻结件，**不含 `entities`**。旧版文档直接写
`--profile v2` 会在校验阶段退出 1。现通过 `--entities data/news_risk/sim_entities.json`
提供仅含主体全称/别名的独立映射（不含任何答案），其 SHA256 会写进系统输出头，
由 `sample_registry.json` 的 `entities_sha256` 与封存记录三方核对，防止悄悄换主体。
若写定 `v1` 则不需要 `--entities`，但须同步把封存记录的 `profile` 改为 `v1`。

**状态说明**：人工判定文件不存在或系统输出未生成 → 退出码 2 `pending-human-annotation`；
人工表已存在但缺条目 / 有非法值 → 退出码 1 `fail` 并列出 `structural_errors`（**不是** pending）；
证据链不全 → 退出码 1 `unverified-source`；全部齐备且达标 → 退出码 0 `pass`。

**v3：正式评测是否有效 ≠ 是否达标**（提前交付增量）。报告顶层拆成三个字段：

| 字段 | 含义 |
| --- | --- |
| `formal` / `formal_evaluation_valid` | 证据链有效：样本来自独立预登记、`--label-source human-annotation`、封存记录 / 登记清单 / profile / 哈希 / 标注人齐备、人工表 50 行逐行带 `symbol`、无结构错误 |
| `meets_85_percent` | 一致条数 ≥43 且分母恰好 50 |
| `pass` | `formal and meets_85_percent`，只有它决定退出码 0 |

真实 **42/50** 记为 `formal=true` / `meets_85_percent=false` / `pass=false` /
`status=formal-not-met`（退出码 1）：证据链有效但如实未达标，**不得**被写成「评测无效」。
`formal_requirements_missing` 只列证据链缺口，不再混入「未达到 43 条」。
人工表缺 `symbol` 列或某行标的写错，`formal=false` 并计入结构错误。

**显式合成夹具**：`--fixture-mode synthetic-fixture` 只用于工程自测（见
`test_acceptance_scoring.py`），报告强制标 `synthetic_fixture=true` /
`not_acceptance_evidence=true` 并附 `fixture_warning`。**正式验收绝不能带此参数**。
失败运行（人工表丢失、系统输出缺失、输入非法）不写新报告，但若同路径留着上一次的
`pass=true` 报告，会就地覆写为 `topic16-formal-report-invalidated-v1`，旧结论挪进
`stale_report`；旧报告本来不存在时仍不创建文件。修好输入重跑即可恢复。

## 研判模块输出契约与 profile

每条输出至少含 `symbol` / `risk_verdict`（`risk`｜`no_risk`）/ `evidence`（原文证据片段）/
`level`（高｜中｜低｜待复核｜无）/ `review_action`（不含买卖结论），
另附 `evidence_status`、`reason_codes`、`disclaimer`、`profile`。
v2 新增 `entity_mismatch`（事件指向别的主体）与 `denial_confirmed_recovery`（否认后确认的是恢复正常）。

- `--profile v1`：内置 `SIM-*` 主体；本轮按审计 P2-1 把「其他监控主体的负面事件」判为
  `no_risk / entity_mismatch`（此前会先命中风险词误报），其余规则与判定顺序未改。
- `--profile v2`：**主体归属 → 部分否认 → 主体/关联排除 → 证伪澄清 → 口径解释 → 假设 → 历史 → 泛述 → 证据不足 → 否定 → 不利进展**。
- 正式验收前须**写定使用哪个 profile 与哪一套样本**，不得事后按结果切换。

## 审计 P2 四项修复（修复前后可复现）

另一台电脑的复核方对 `topic-16-news-risk@2786c70` 复现了 4 个标准回归未覆盖的问题。
四项都在本轮修复，并用 `eval/news_risk/repro_p2_audit.py` 对「`git archive 2786c70`
导出的干净树」与「当前工作树」分别复算，实测对照存
`eval/news_risk/results/p2_audit_before_after.json`（合成 fixture，不是验收证据）：

| 问题 | 修复前（`2786c70`） | 修复后 | 修复方式 |
| --- | --- | --- | --- |
| P2-1 v1 把别的主体的负面事件算给当前标的 | `SIM-A` +「北辰智造集团股份有限公司收到行政处罚决定书」→ `risk` / `sufficient` | `no_risk` / `entity_mismatch` | `news_risk/rules.py` 新增 `_other_monitored_form()`：主体归属检查前移到否定、澄清、风险词匹配之前；只在「主体不明」且原文点名其他监控标的时判错配，易混淆主体全称先剔除，不干扰既有边界 |
| P2-2 v2 漏掉仍暂停的产线 | 「否认全厂停产，确认两条产线运转正常，但一条产线暂停，尚无复产日期」→ `no_risk` / `negated` | `risk` / `partial_denial_confirmed` | `news_risk/rules_v2.py` 新增 `_residual_adverse()`：恢复词与不利词并存时按转折 / 并列切分子句，只有不利词被明确否定（`未发生停产` 等）才忽略；纯恢复、明确否定的反例仍判 `denial_confirmed_recovery` |
| P2-3 正式评测示例命令跑不通 | 冻结 `samples_50.json` 不含 `entities`，`--profile v2` 退出 1 | 加 `--entities data/news_risk/sim_entities.json` 后退出 0 | 新增只含主体全称 / 别名的独立映射文件，不修改冻结件；CLI 的 `--entities` 校验禁止答案字段、禁止配 `v1`，文件 SHA256 写入系统输出头 |
| P2-4 无时区的未来封存时间可正式通过 | `sealed_at=2099-01-01T00:00:00` → `formal=true` / 退出 0 | `formal=false` / 退出 1 `unverified-source` | `eval/news_risk/score.py` 要求 `sealed_at` 带显式时区，再按该时区与当前时刻比较；无时区、纯日期、未来时间一律拒绝 |

复现命令（PowerShell，`$P = '.\.venv\Scripts\python.exe'`）：

```powershell
# 修复前：把合并提交当时的树导出到本机临时目录
git archive 2786c70 "行业智能体/金融/投资组合风控助手" | tar -x -C .\p2-before
& $P eval/news_risk/repro_p2_audit.py `
    --root .\p2-before\行业智能体\金融\投资组合风控助手 --mode before
# 修复后：指向当前工作树
& $P eval/news_risk/repro_p2_audit.py --root . --mode after
```

另新增证据链约束：登记清单声明 `entities_sha256` 时，v2 的系统输出头与封存记录必须带
同一哈希，否则 `formal=false`（`test_34`）；但 v1 使用内置主体表，登记清单为 v2 预留的
哈希不会反过来卡死 v1 正式评测（`test_35`）。封存记录模板与两份正式评测说明统一补上
`--sample-registry`、`--entities` 和「`sealed_at` 必须带时区且不得晚于评分时刻」。

**边界**：本轮没有让任何 AI 参考答案变成人工金标，`score.py` 默认仍是 `unverified`，
无人工表时仍 `pending-human-annotation`（退出码 2）；`consistency_report.json` 未生成。
开发评测仍只记「与 AI 参考答案的一致率」。

## 测试结果

| 项 | 结果 |
| --- | --- |
| 舆情测试 | `eval/news_risk/tests` **119/119**（v1 规则 15 + v2 规则 28 + 正式评分 35 + 开发评分 28 + 正式评分补强 13） |
| 正式评分补强 | 独立预登记常量交叉核对 2、有效性/达标分离 3、**42/50 记为 formal-not-met**、自供清单洗白开发集被拒、正式模式拒绝非预登记样本、人工表逐行 `symbol`、旧报告失效与幂等恢复，共 13 项显式合成夹具 |
| 开发评测 | baseline 47/50（误报 2、漏报 1）→ v2 50/50；报告均标 `not_human_judgment_agreement=true`、`not_acceptance_evidence=true`，且无达标布尔值 |
| P0 门禁 | 还原「AI 参考转两列 CSV」反例，默认参数下 `formal=false` / 退出码 1；无封存记录、哈希不符、symbol 整列写错、缺 1 条均被拒 |
| P2 回归 | 四项修复前后对照见上表；新增 10 项回归（主体归属 2、残留不利事实 2、`--entities` 与登记哈希 3、封存时间 1、主体映射哈希 1、v1 profile 门禁 1），证据文件 `p2_audit_before_after.json` |
| 主体归属 | 25 条风险样本 × 换 3 个标的 = **75/75** 由 `risk` 变为 `no_risk / entity_mismatch` |
| 部分否认 | 「确认三条产线全部运转正常」→ `no_risk`；「三分之一产能闲置、整修两个月」→ `risk`；两句均已固化为回归 |
| 结构校验 | 正常预测与 `--validate-only` 共用校验；嵌套答案字段、伪造证据、profile 不符均被拒 |
| v1 profile | CLI 逐行补齐 `profile=v1`；严格评分不再把 50 条正确输出误判为结构错误（新增回归） |
| MCP 返回体 | `mcps/news_risk/tests` **6/6**（venv / mcp 2.3）：`isError=false` + `content[0].text` 完整 JSON + `structuredContent.result` 逐字一致 + 输出 schema 未变 |
| 过拟合诊断 | 修正 `AnnAssign` 后：上一提交 143/178（80.34%）、当前 164/216（75.93%）；报告标 `diagnostic_only=true` |
| 正式评测 | `score.py` 无人工表时 `pending-human-annotation`（退出码 2）；有表但证据链不全时 `unverified-source`（退出码 1）；未生成 `consistency_report.json` |
| 旧报告失效 | 开发评分器 28/28（含成功→失败、连续失败幂等、结构错误、恢复 4 项新增）；正式评分器 13/13 中含 3 项同口径回归 |
| 全套回归 | **215/215**（8 + 8 + 80 + 119）+ `mcps/portfolio_rules` 4/4 + `mcps/news_risk` 6/6（均用项目 `.venv`，系统 Python 缺 `mcp` 包）+ deployment 20 项（12 通过 / 8 skip）；离线阈值基线仍 `TP=20 FP=0 FN=0`；五份冻结哈希原样 |

## 隔离与合规

- 本分支**未改动**止盈止损链路的冻结行情、标注、任务清单、SQLite 运行态、任何 `run_id`
  或服务器环境，也不修改 `mcps/portfolio_rules`。
- 50 条样本及其答案不得写入提示词、知识库、工具参数或 Agent 配置；
  参考判断文件只留在用户本机、不入库；`dev_eval_predictions*.json`、运行状态文件、
  任意换名的人工表与外部 AI 参考文件均已忽略。
- 开发评测集**不得**用于正式盲评：登记清单与独立预登记摘要双重标记，评分器按内置
  指纹硬性拒绝，自供清单改标也无效；复核者只发空白盲标表。
- `test_acceptance_scoring.py` 全部为**显式合成夹具**，只验证评分脚本机制；
  夹具报告一律带 `synthetic_fixture=true` / `not_acceptance_evidence=true`，
  **不得**当作 50 条人工判定结果或验收证据，正式验收不得使用 `--fixture-mode`。
- 原来 `50/50` 只保留为「与 AI 参考答案的一致率」开发结果；本轮没有新增任何
  人工判定标签，正式候选仍是 `NR-2026-11-09`。
- 全部主体、来源、事件、时间均为虚构模拟数据；未使用真实企业、人物或敏感数据。
  输出定位为辅助建议，重大舆情须人工核实，不构成投资建议。
- 平台侧 MCP 注册与 Agent 发布状态为「待平台实跑」，尚未从平台导出 `agent.json`。
  舆情 MCP 已按主开发分支 `e25f800` 的真实平台经验改为「文本块 + 结构化结果 +
  显式成功」双通道返回，本地 SDK 6/6 wire 测试通过；**但仍未实证真实 Nexent
  能解析 `content[0].text`，也未取得可关联到该次调用的实际入参与成功回执**。
  实跑还表明模型 `final_answer` 不能替代工具调用证据，缺证据必须保留 `insufficient`。
- 舆情样本、标注说明、研判模块与评分脚本由 **WorkBuddy** 辅助编写；
  用户提供的 50 条原文与 AI 参考判断由 **Codex** 侧独立构造，本轮 AI 参考答案未经人工判定。
