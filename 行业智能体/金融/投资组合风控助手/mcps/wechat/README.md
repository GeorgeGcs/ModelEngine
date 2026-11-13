# Nexent 微信风险推送

定时执行器在 `nexent-price` 模式下验证价格工具回执并保存风险事件。选择 `nexent-wechat` 网关后，它通过 Nexent 已发布工作流调用 `send_wechat_risk_notification`。MCP 服务从同一 SQLite 读取该事件，按现有事务完成去重、限频及重试，通过 ClawBot 发送。

工具参数为事件 ID、原事件所属 run ID 和短时签名。收件人、账号、消息内容和实际发送通道由部署侧配置或从原事件读取。真实发送前，先用 `local-mock` 在隔离环境复演整个调用链。

## 部署和绑定

1. 按 [部署与复演](../../docs/delivery/部署与复演.md) 准备独立 Nexent v2.8.3 runtime。使用本候选的观察补丁清单与 `integration/dispatch.py`，启用签名工作流和对象回执。为测试发布 Agent 绑定原有三个工具及 [微信工具 schema](schema.json)。构建实际工具库存后，在原集成预检命令加 `--wechat` 核对四工具绑定。
2. MCP 服务与 scheduler 使用同一台宿主机的独立 SQLite 文件。用不同容器时，让两者读写同一本地目录及其 WAL 文件。保持测试与既有观测数据库分离。
3. 给 scheduler 和 MCP 服务设置下表共有项。工作流签名密钥另按部署说明配置。微信工具签名密钥使用独立的 32 字节私有文件。

| 进程 | 配置 |
| --- | --- |
| 两者 | `TOPIC16_WECHAT_MCP_KEY_FILE`、`TOPIC16_RECIPIENT_ID`、`TOPIC16_WECHAT_MCP_TRANSPORT` |
| MCP | `TOPIC16_WECHAT_MCP_DB` 指向 scheduler 的 `--db`。限频 `TOPIC16_WECHAT_WINDOW_SECONDS`、`TOPIC16_WECHAT_MAX_PER_WINDOW` 与计划一致，默认 60 秒、3 次 |
| MCP 真通道 | 设 `TOPIC16_WECHAT_MCP_TRANSPORT=clawbot`，配置 `TOPIC16_CLAWBOT_ACCOUNT`、`TOPIC16_CLAWBOT_TARGET` 和可选 `TOPIC16_CLAWBOT_COMMAND_JSON`。默认使用 `["openclaw"]`，Windows 可指定 Node 可执行文件与 OpenClaw CLI 脚本 |
| scheduler | 按部署说明设置真实 Nexent 北向配置、`TOPIC16_DISPATCH_KEY_FILE` 及新窗口对应的 `NEXENT_EXECUTION_RECORDS` |

ClawBot 使用已经扫码绑定、已获接收人授权的测试实例。MCP 只在隔离私网监听。命令、账号及会话配置仅由部署方设置。

```bash
python -m mcps.wechat.server --transport streamable-http --host "$MCP_BIND" --port 8772
python -m app.scheduler make-plan --mode nexent-price --gateway nexent-wechat \
  --window-id topic16-wechat-smoke --portfolios "$PORTFOLIOS" --snapshot "$SNAPSHOT" \
  --start "$START" --hours 0.01 --interval 1800 --output "$PLAN"
python -m app.scheduler run --plan "$PLAN" --db "$DB" --evidence-dir "$EVIDENCE"
```

## 回执与手机确认

定时执行器核对 Nexent 工具调用及独立原生回执，再匹配 SQLite 中的事件、推送记录、状态和发送尝试。CLI 有消息编号且发送结果可识别时，记录为网关受理。超时、无编号或有冲突的 CLI 回执保留为未知，避免重复发送。明确未启动发送的失败可按既有事务重试。

真实微信消息附发送尝试核对码。观察者从手机读取核对码后执行：

```bash
python -m app.clawbot --db "$DB" --phone-code "$CODE_FROM_PHONE" --observer "$OBSERVER"
```

推送查询页面可见手机确认时间，发送尝试保存提供方消息编号。需分别在近期互动和约定闲置时长后的新事件上实测，记录定时任务、MCP 调用与手机确认。模拟链路的报告注明模拟。价格调度负责本节的推送触发，现有三工具综合演示沿用其原来的模拟推送入口。

[OpenClaw 消息命令](https://docs.openclaw.ai/cli/message) · [腾讯微信插件](https://github.com/Tencent/openclaw-weixin)
