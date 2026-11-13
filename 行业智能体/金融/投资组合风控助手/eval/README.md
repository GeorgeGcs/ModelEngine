# 冻结阈值评测集（20 个触发点）

> ## 本轮成果的适用范围：**离线回放层验证**
>
> 对应《需求包详情与交付要求》课题 16 验收标准第 2 条：「阈值触发准确性 100%（用历史行情回放构造 20 个触发点，全部命中且无误触发）」。
>
> **本轮只证明 `python -m portfolio_risk` 离线回放层的阈值判定准确，不构成端到端准确率证据。**
> 此刻信号还没有经过 Nexent 主控、MCP 工具调用、知识库研判、微信推送和持久化。接入 Nexent 后，必须用**原封不动**的 `expected_20.json`（含同一份 SHA256 冻结锁）核对编排后的**最终持久化信号**；到那时才有端到端准确率证据。见文末「接入 Nexent 后的复用」及 [Nexent 编排链路状态](nexent/README.md)（当前「待平台实跑」）。

## 一条命令

在项目根目录 `行业智能体/金融/投资组合风控助手` 执行：

```powershell
python eval/run_eval.py
```

它会依次做两件事，且刻意拆成两个独立进程：

1. 校验行情输入和预期标注的 SHA256，从**正常回放入口** `python -m portfolio_risk data/eval_20.json` 运行，以 UTF-8、LF 换行保存输出至 `eval/results/replay_actual.json`；
2. 由 `eval/compare.py` 读取冻结标注和结果文件逐格比对，输出 `eval/results/compare_report.json`。

退出码 `0` = 全部达标，`1` = 存在差异，`2` = 回放或标注校验中止。

反向测试与原有回归：

```powershell
python -m unittest discover -s eval/tests -v
python -m unittest discover -s tests -v
```

## 文件清单

| 文件 | 用途 |
| --- | --- |
| `data/demo.json` | **保留不变**，3 组合 / 7 快照 / 4 信号，仅用于快速演示 |
| `data/eval_20.json` | 独立评测输入：3 组合、8 快照、4 个标的，全为虚构模拟数据 |
| `data/eval_20.json.sha256` | 锁定上述输入的精确字节，覆盖价格、阈值和全部快照 |
| `eval/expected_20.json` | 冻结标注：20 个预期触发事件 + 28 个非触发格 |
| `eval/expected_20.json.sha256` | 标注的 SHA256 冻结锁 |
| `eval/run_eval.py` | 回放 → 结果文件 → 比对的单命令编排 |
| `eval/compare.py` | 独立复算脚本，逐格比对并输出 TP / FP / FN / 差异明细 |
| `eval/results/replay_actual.json` | 本轮实际信号（回放入口原样输出，作为证据留存） |
| `eval/results/compare_report.json` | 本轮复算报告 |
| `eval/tests/test_compare.py` | 基线 + 删/添/重复三条反向测试 + 冻结与独立性测试 |

## 独立性设计

这是本评测集能不能当证据的关键：

- **预期答案独立编写**：20 条预期触发事件由人工依据标注文件里的阈值表逐格推导后写死，未调用 `evaluate_snapshot`、未调用回放 CLI、未用任何被测代码生成。刻意**不写 `event_id`**——它是被测引擎的产物，写进标注就等于让被测代码参与出题。
- **复算脚本不碰被测包**：`eval/compare.py` 只用 Python 标准库，与被测代码之间唯一的接口是结果文件里的 `signals` 数组。`eval/tests/test_compare.py` 用 AST 检查该脚本的 import 列表，断言不含 `portfolio_risk`，并断言标注里没有 `event_id`。
- **冻结**：标注文件的 SHA256 写在 `.sha256` 锁里，比对前强制校验；标注被改动而锁未更新时，比对直接以退出码 2 中止并打印新旧哈希。确需修订标注必须走显式评审，再用 `python eval/compare.py --refresh-freeze` 重新冻结——**不得为了让被测结果通过而改标注**。
- **逐格而非只数条数**：比对的业务键是 `(组合, UTC 时间, 标的, 规则)`，命中后还要核对 `price` 与 `threshold`；`event_id` 必须是 64 位小写十六进制、时间必须是 UTC、不同业务键的 `event_id` 不得冲突。

## 覆盖矩阵

3 组合 × 每组合 2 项持仓 × 8 张快照 = **48 个检查格**，其中 20 个预期触发、28 个明确标注为不触发。非触发格全部逐条列出，不是「剩下的默认不触发」。

| 覆盖点 | 标注编号 / 位置 |
| --- | --- |
| 等于止损线 | T01、T06、T11、T20 |
| 等于止盈线 | T05、T08、T17、T19 |
| 止损线下方 0.01 / 0.0001 | T02、T03、T04、T13、T18 |
| 明显跌破止损线 | T09（低 3.00）、T10（低 3.01） |
| 止盈线上方 0.01 / 明显突破 | T07、T12、T14、T15、T16 |
| 阈值另一侧「不应触发」的精度边界 | S1 全部 6 格（95.01 / 54.99 / 1.9001 / 2.4999）、S6 的 105.99 / 2.1199 |
| 同标的在不同组合采用不同阈值 | `SIM-A`：P-STOCK `95.00/106.00` vs P-FUND `92.00/110.00`；`SIM-B`：P-STOCK `48.00/55.00` vs P-MIXED `45.00/58.00`。S2/S3/S4/S5/S6 只触发窄阈值组合，S7/S8 两组合同刻同价都触发但 `threshold` 不同（T09 vs T11、T10 vs T13、T15 vs T17、T16 vs T19） |
| 时区归一化 | 7 张快照输入 `+08:00`，S5 输入 `Z`；标注统一写 UTC，验证 `+08:00 → Z` 一致 |
| 同价不同时刻 | T04 与 T18 同为 `1.8999`，分处 S4 / S8，应分别产生信号；重复告警抑制属推送层职责 |

## 判定口径

| 情形 | 判定 |
| --- | --- |
| 少一条预期信号 | `FN += 1`，差异类型 `MISSING`（漏报） |
| 非触发格或未定义格出现信号 | `FP += 1`，差异类型 `UNEXPECTED`（误触发） |
| 同一业务键出现两条及以上 | `DUPLICATE`，多出的每条同时 `FP += 1`（`EXTRA_DUPLICATE`） |
| 命中但 `price` / `threshold` 与标注不符 | `MISMATCH`，同时 `FN += 1` 且 `FP += 1` |
| 结构错误、时间非 UTC、event_id 冲突、锁不符 | 直接判失败或中止 |

达标条件：`TP = 20` 且 `FP = 0` 且 `FN = 0` 且重复、字段偏差、结构错误均为 0。

三个反向测试（人为篡改结果文件，标注不动）：

| 篡改 | 期望被抓到 |
| --- | --- |
| 删一条 | 退出码 ≠ 0，`FN = 1`，差异含 `MISSING` |
| 添一条（塞进非触发格） | 退出码 ≠ 0，`FP = 1`，差异含 `UNEXPECTED` 且理由是「非触发格误触发」 |
| 重复一条 | 退出码 ≠ 0，`duplicates = 1`、`FP = 1`，差异含 `DUPLICATE` 与 `EXTRA_DUPLICATE` |

另有基线测试（必须 PASS）、冻结锁防篡改测试（改标注不重冻则拒绝运行）、独立性测试（AST 检查不导入被测包、标注不含 `event_id`）。

## 接入 Nexent 后的复用

> **当前进度：链路已实现并加固，平台实跑未完成。** 冻结任务清单、SQLite 落库、独立进程只读导出、任务对账与幂等重试验证见 [eval/nexent/README.md](nexent/README.md)。对账已升级为从库逐行比对七字段、逐个打开载荷验真、校验清单自身锁，并在 `--scope end-to-end` 下强制核对工具调用/结果的配对证据与计划/实际入参；本地替身（v2 载荷）无法冒充端到端（v3 载荷）。本机无可用 Nexent 实例，因此 `eval/results/nexent_final_signals.json` 与 `compare_report_end_to_end.json` **尚未生成**；下面的命令要等平台实跑成功后才有输入。本地替身产物一律标 `scope=local-fixture`、`platform_evidence=false`，不得当作端到端证据。

标注文件与冻结锁**一个字都不改**。把 Nexent 编排完成后落库的最终信号导出成同样的结构：

```json
{
  "signal_count": 20,
  "signals": [
    {
      "event_id": "...",
      "portfolio_id": "P-STOCK",
      "timestamp": "2026-11-09T02:00:00Z",
      "symbol": "SIM-A",
      "rule": "stop_loss",
      "price": "95.00",
      "threshold": "95.00"
    }
  ]
}
```

然后只换 `--actual` 与 `--scope`：

```powershell
python eval/compare.py --actual eval/results/nexent_final_signals.json --scope end-to-end --report eval/results/compare_report_end_to_end.json
```

比对会标出漏掉、多出、重复或改写的信号。`--scope` 记录调用者声明的数据来源。提交端到端证据时，一并保留 Nexent 运行 ID、平台调用记录以及该次运行的持久化导出，使评审能核对报告来源。若平台字段名不同，用只读适配器映射名称，并记录映射规则，保持信号数值、时间和规则不变。

## 数据与工具披露

- 全部组合、标的、行情价格均为人工构造的模拟数据，不含真实行情、个人信息或任何敏感数据；输出仅为风险提示，不构成投资建议。
- 本轮新增的 `data/eval_20.json`、`eval/` 下脚本、标注与文档由 WorkBuddy 辅助编写；此前的基础代码与样例由 Codex 辅助编写（见主 README）。按活动合规要求，正式提交材料须如实记录实际使用的 AI 工具名称、用途、使用环节、产物及人工复核情况，并补齐第三方依赖与许可证记录。
- 冻结标注中的 20 条预期由人工按阈值表独立推导，已与一次回放结果核对；核对过程不改变标注内容。
