# Nexent v2.8.3 原生 MCP 回执部署适配

本目录是**平台部署适配包**，与本项目 `mcps/portfolio_rules/server.py` 的业务工具实现分开。它只为 Nexent v2.8.3 增加默认关闭的真实工具回执观察，不计算规则、不替代 Agent、不改写工具参数、返回值、SSE 或评测门禁，也不包含平台完整源码。

已通过的实机边界是本项目的真实双样本 smoke。本轮同 run_id 的 full **未通过**，不能把本目录单元测试通过写成“20/20 端到端通过”。全量覆盖、首个失败任务与三方条数应以独立实跑摘要为准。

## 为什么需要部署适配

该版本 SDK 可以发出目标工具的原生 `tool_call_id` 与调用入参，但北向 SSE 不一定提供可核对的原始 MCP 返回。仅有模型 `final_answer`、会话完成状态、相近时间或同样的结果数量，都不足以证明同次工具成功。

本适配在 SDK 的真实 MCP 返回边界保留原始 `CallToolResult`，然后只在外层工具 future 按时正常返回、未取消时终结回执。原生 UUID 来自**同一个 observer** 的 ContextVar，并沿现有 ThreadManager 的上下文传播进入工具 worker。SDK 不自行读环境变量或决定写盘目录；部署启动层显式注入私有 sink。

成功记录必须同时满足：

- 本次调用有可读出的原生工具调用 ID，并且该 capture 恰好对应一个实际 RPC。
- 实际 RPC 参数包含 `portfolio` 与 `snapshot`。
- 原始响应明确携带 `isError:false`；Pydantic 默认值不算线上明确字段。
- 原始 `structuredContent.result` 明确存在且为数组，允许明确的 `[]`。
- 外层适配器已正常接受结果，且没有超时、异常、取消或捕获失败。

异常、超时和取消为失败；缺字段或关联不足为 insufficient。晚到结果不能把已终结失败升级成功。sink 写盘失败只记录错误类型，不泄露参数或响应，不改变工具原有返回/异常；由于没有有效回执，评测仍应拒绝补证。

回执通过 `source=nexent-execution-record`、`tool_name/call_id/arguments/tool_success/result` 供现有 `--execution-records` 路径读取。消费端必须将 `call_id` 与**本次 SSE 目标工具调用事件**精确匹配，并继续检查实际入参与冻结清单、原始信号与 SQLite。多条匹配仍应拒绝，不得筛掉不利记录以制造唯一匹配。

## 文件及版本约束

| 文件 | 用途 |
|---|---|
| `sdk-observation.patch` | 两个 SDK 模块的最小统一 diff，部署到原生模块位置 |
| `observation-manifest.json` | v2.8.3 官方来源、干净基线、已运行补丁和启动文件的 SHA256 |
| `prepare_observation_overlay.py` | 精确验证并准备独立 overlay；不改 SDK 输入，不启动服务 |
| `startup/topic16_receipt_sink.py` | Linux 私有 JSONL sink 与显式 observer 注入 |
| `startup/sitecustomize.py` | 默认关闭的专用 runtime 启动入口 |
| `runtime-observation.override.example.yaml` | 仅本课题 runtime 的挂载示例，路径由部署者私下配置 |
| `tests/test_receipt_observation.py` | capture、实际 patched wrapper 和私有 sink 的分层回归 |

目标版本为官方 `v2.8.3`，tag 提交 `046d77c8c479d30ccd1ed44cd37f727547798d0f`。官方来源与 SHA256 均写在 manifest；已运行版本的两个候选模块哈希为：

```text
managed_mcp.py d64ffb9157d6407398cd481d7af09ddd56abf0b2adef34ebbabf66706fed3d96
run_agent.py   ff63686b478d484beb8ec45e84a460be7c18fd7d8a76df8f172a3334fadb8e95
```

哈希不符时停止应用并查明版本差异，不做模糊补丁、跳过上下文或用 latest 顶替。已通过 smoke 的实例应保留当前运行代码与挂载，在 full 中途不切回干净版本。

## 新实例的应用步骤

以下变量均由部署者在私有会话设置；本仓库不保存内部地址、私人主机路径或凭据。该过程是独立部署操作，不应在正在运行的评测中执行。

1. 确认这是本课题自己的 v2.8.3 runtime，当前没有活动评测。检查实际使用的 Python；官方镜像的后端虚拟环境可作为候选，但不能仅凭目录名推定 SDK 加载位置。
2. 用 runtime 的实际解释器读取两个模块的 `__file__` 和 SHA256。官方镜像以非 editable 方式安装 SDK；仅修改平台 checkout 下的 `sdk/` 不会改变已安装模块。

```bash
docker exec -i -w /opt/backend "$RUNTIME_CONTAINER" "$RUNTIME_PYTHON" - <<'PY'
import hashlib, importlib, json
from pathlib import Path
for name in ("managed_mcp", "run_agent"):
    path = Path(importlib.import_module("nexent.core.agents." + name).__file__)
    print(json.dumps({"module": name, "path": str(path),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
PY
```

3. 仅将这两个模块复制到私有基线目录，按 `nexent/core/agents/` 组织；设置 `CLEAN_NEXENT_PACKAGE` 指向该副本的 `nexent` 目录。不要提交副本。验证它们属于 manifest 中的干净基线后，在课题目录执行：

```bash
.venv/bin/python eval/nexent/deployment/prepare_observation_overlay.py \
  --sdk-package "$CLEAN_NEXENT_PACKAGE" --output "$TOPIC16_OVERLAY_DIR"
```

准备器拒绝已存在的输出目录、非基线 SDK、补丁上下文偏差或任何候选哈希不符。它不覆盖输入模块，产出仅两个候选 SDK 模块、两个启动文件及相对路径清单。输出应放在 Git 之外。

4. 由运行评测的用户预先创建私有回执目录（0700）及 `topic16.execution_records.jsonl`（0600）。**已有文件必须保留，不能清空或换源拼接结果**。预创建保证容器以 root 追加时仍保留评测用户的文件所有权；确认该用户能只读消费它。sink 会拒绝文件符号链接和已有非私有文件。
5. 依据步骤 2 的实测模块位置，私下设置 `NEXENT_MANAGED_MCP_MODULE`、`NEXENT_RUN_AGENT_MODULE`。只读取原 runtime 的 `PYTHONPATH` 值并保留到 `NEXENT_ORIGINAL_PYTHONPATH`，不要打印整个容器环境或 Compose 配置。把示例 override 复制为私有部署配置，并设置其中需要的目录变量。
6. 先对官方生产 Compose 加此 override 执行 `config --quiet`。确认目标只有本课题 runtime 后，才用同一 Compose 组合 `up -d --no-deps --force-recreate nexent-runtime`。数据库、网络、北向 Token 及其他项目无需重建；不要重启 Docker daemon。
7. 用同一 runtime 解释器核对两个模块的候选哈希，并确认 `MessageObserver._topic16_receipt_sink_installed` 为 true。新进程和主进程必须加载相同挂载及环境。重建 runtime 时要继续带此 override，否则观察通路会丢失。

`TOPIC16_OBSERVATION_RUN_ID=topic16` 只决定观察流文件名，不是评测数据库的 `RUN_ID`，也不参与“同次调用”推断。真实关联仍只使用 SSE 原生 UUID。运行驱动的服务器会话需要显式指向宿主机上的同一私有文件：

```bash
export NEXENT_EXECUTION_RECORDS="$TOPIC16_PRIVATE_RECEIPTS_DIR/topic16.execution_records.jsonl"
```

## 评测所需外部配置

准备一个实际可访问的 Nexent、支持平台执行协议的模型、已启用并发布的规则 Agent，以及从 runtime 容器可达的规则 MCP。API Token、模型 Key、内部 JWT 和部署路径只留在私有环境。SDK 不会因本补丁获得任何账户或权限。

驱动仍需要 `NEXENT_BASE_URL`、`NEXENT_AGENT_NAME`、`NEXENT_API_TOKEN`，以及实测确定的 `NEXENT_API_PREFIX`、版本追溯和执行回执文件。数据库位置以 smoke 实际配置为准，不能把证据归档目录当成默认数据库目录。

先真实 smoke，通过四项预检后才使用**同一 RUN_ID、数据库、载荷目录、预检和回执源**接续 full。例如以下参数必须来自原 smoke 配置，而不是新建或猜测：

```bash
.venv/bin/python eval/nexent/run_nexent_eval.py --mode full --run-id "$RUN_ID" \
  --db "$EVAL_DB" --records-dir "$EVAL_RECORDS_DIR" \
  --preflight-output "$EVAL_PREFLIGHT" --execution-records "$NEXENT_EXECUTION_RECORDS"
```

门禁拒绝或部分失败时保留数据库、原载荷、哈希和回执，报告首个 task_key/错误；不得删库、改标注、换 run_id 或用 final_answer 补结果。

## 回归验证与证明边界

普通项目环境可运行依赖隔离的 capture 回归：

```bash
python -m unittest discover -s eval/nexent/deployment/tests -v
```

没有 patched SDK 或 Linux 文件锁的环境会明确跳过对应项；不能把这些跳过写成通过。要完整验证 wrapper/超时/写盘行为，在已装载本适配的 v2.8.3 Linux runtime 内，将本目录测试与打包文件复制到私有临时目录，使用该 runtime 的解释器执行：

```bash
NEXENT_OBSERVATION_REQUIRED=1 "$RUNTIME_PYTHON" -m unittest discover \
  -s "$OBSERVATION_PACKAGE/tests" -p test_receipt_observation.py -v
```

该严格模式要求 patched SDK 存在。测试使用实际 capture/wrapper 代码与隔离 RPC，覆盖命中、显式空数组、缺线上成功字段、显式错误、异常、取消、超时、晚到结果、原生调用 ID、sink 写盘失败及 Linux 私有文件保护。它们不联系真实模型、Agent、MCP 或 SQLite，也不生成平台证据；真实 smoke/full 必须单独运行并独立读库复算。
