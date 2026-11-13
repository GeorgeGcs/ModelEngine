# 第二轮连续观测

本目录是 A 的部署适配，不修改项目已有调度器、规则或 B 的开发分支。它将持久化调度器接到已发布 Agent 的签名分派入口，仍须由原生观察器提供唯一工具回执。

首轮固定 `dbdf965` 的 139 个成功与 5 个未知结果保留。第二轮使用已验真的项目提交 `aeafe7e52193fefb3d5b9e0c25995b825e7de81f`，加本目录 `price_worker.py`，独立 runtime、北向入口、数据库和回执。短测试及正式窗口分别留库。

## 已完成验证

- Windows 和 Linux 适配器各 13/13，0 skip。新增计划生成测试 3/3。
- 新 Linux patched runtime 观察器 28/28，0 skip。
- 实机短测试真实命中与成功空数组各一次，均回查独立 SQLite、实际参数、可信调用事件和唯一原生回执。
- 注入一次模拟推送失败，换新的 Linux 进程重试后完成。2 个原生调用、3 个调度尝试、2 个模拟推送尝试。重试复用原回执，没有再次调用价格工具。
- 结果未知禁止自动重放。短测试采用进程退出后重开，未据此声称真实 RPC 进行中断电恢复通过。

## 本轮计划

`topic16-observe2-20261111-231800`，北京时间 2026-11-11 23:18 至 2026-11-12 23:18，23:23 截止采集。144 个组合时隙、288 个持仓格，每组合每 30 分钟一次，三组相隔 180 秒，准时完成容差 150 秒。

使用原 8 个冻结快照循环 6 次，不改行情时间或标注。预期出现的原始信号会重复返回，数据库按原 event_id 去重，重复通知会抑制；不能用唯一信号数代替时隙成功数。最多 2 次自动尝试，只对明确失败的模拟推送复用已存证据重试；未知调用和未知推送不自动重放。正常窗口不注入故障，不人工补跑。

最终分列准时、迟到、失败、缺失、待执行、重试、人工和未知。144 个时隙须各自有可核验的成功证据，才能报告第二轮业务观测通过。开始记录和进度快照均不是完成证明。

## 在新环境复现

先按项目部署文档完成同版已发布 Agent、`optional_published_workflow` 与私有签名 key、独立原生回执流和观察器严格回归。私有配置至少包含 `NEXENT_BASE_URL`、`NEXENT_API_TOKEN`、`NEXENT_AGENT_NAME`、`NEXENT_VERSION`、`NEXENT_MODEL`、`NEXENT_EXECUTION_RECORDS` 和 `TOPIC16_DISPATCH_KEY_FILE`。通过私有 env 文件注入，不能放到命令行、日志或 Git。

以下从项目目录执行，示例开始时间需换成新的未来时间。所有输出目录必须独立，不覆盖已有窗口。

```bash
export PYTHONPATH="$PWD:$PWD/docs/delivery/observation"
python -B -m unittest discover -s docs/delivery/observation -v
python docs/delivery/observation/make_plan.py \
  --start '2026-11-15T08:00:00+08:00' --window-id topic16-observe-new \
  --output "$NEW_PRIVATE_WINDOW/plan.json"
python -B docs/delivery/observation/price_worker.py \
  --plan "$NEW_PRIVATE_WINDOW/plan.json" \
  --db "$NEW_PRIVATE_WINDOW/schedule.sqlite" \
  --evidence-dir "$NEW_PRIVATE_WINDOW/evidence"
```

用容器或服务管理器托管 worker，并另设每 30 秒存活、重启和 NTP 采样。启动前封存项目 SHA、适配器 SHA256、平台发布配置、镜像 ID、计划及旧证据哈希。时隙执行时必须固定版本与配置。

结束后由独立进程调用 `verify_evidence.verify(window_directory, receipt_file)`，该函数以 `mode=ro` 重开数据库。另核对采样连续性、worker 退出码、计划/版本哈希、旧文件哈希及预检回执前缀。原始记录留部署机，只提交脱敏统计。

实际首次 worker 启动碰到原 Docker 网络地址耗尽，未执行任务，失败容器和计划保留。后续独立窗口 worker 使用 host 网络访问新北向服务的 loopback 端口，没有扩容或重启历史网络及服务。复现者可选择有空余地址的独立网络，但须在开窗前固定。

所有推送均为 `local-mock`，不证明真实微信送达。
