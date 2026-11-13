# 课题 16 交付索引

2026-11-12：本地人工评分完成，31/50 未达标。当前状态以[交付差距与收尾计划](交付差距与收尾计划_20261112.md)为准，旧实测版本及失败记录保留。

| 材料 | 入口 |
| --- | --- |
| 应用、工具和依赖 | [项目 README](../../README.md) · [前端](../../app/web/index.html) · [MCP 工具](../../mcps/) · [锁定依赖](../../integration/requirements.txt) |
| 原生 Agent 与部署 | [脱敏原生导出](../../agent.json) · [部署与复演](部署与复演.md) · [Nexent 运行适配](../../eval/nexent/deployment/README.md) |
| 合成数据与知识库 | [演示组合](../../data/demo.json) · [历史价格回放](../../data/eval_20.json) · [50 条舆情样本](../../data/news_risk/samples_50.json) · [知识资产及来源](../../knowledge/README.md) |
| 使用文档及演示 | [操作说明与关键截图](操作说明.md) · [3 分 29 秒实机录像](media/topic16-demo.webm) · [微信接入路径](微信接入说明.md) |
| 测试与复算 | [逐项验收报告](验收报告.md) · [10 组用例](cases.json) · [用例运行脚本](verify_cases.py) · [批量导入测试](benchmark_import.py) |
| 持续观测与人工评分 | [第二轮计划及复现](observation/README.md) · [窗口进度快照](window2-progress.json) · [原人工登记计划](news-review-plan.json) · [终版人工评分](人工复核结果_20261112.md) · [评分摘要](human-review-results-20261112.json) |
| 仓库 CI | [运行与权限说明](CI说明.md) · [Windows 401 项回归](ci-local-results.json) · [Linux 401 项回归](ci-linux-results.json) · [本地静态扫描](security-local-results.json) · [平台接入状态](ci-hosted-status.json) |
| 变更和许可 | [CHANGELOG](../../CHANGELOG.md) · [LICENSE](../../LICENSE) |

实机版本为 `aeafe7e52193fefb3d5b9e0c25995b825e7de81f`。原生导出与实跑配置的哈希可从 [机器可读证据](workflow-evidence.json) 查回。导出中实例相关标识已替换为占位符，按部署说明在验收机重绑定。

[舆情正式评分（86%）](舆情复评_20261113.md) · [候选回归与评分摘要](news-formal-results-20261113.json)
