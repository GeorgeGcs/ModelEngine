# 原生知识库适配：第四批

开发范围：3 份项目自编 CC0-1.0 资产、Nexent 原生上传/检索适配、SQLite 追踪、真实 MCP SDK 入口。尚未由 A 在真实知识库上验证；本地测试使用契约替身，不能证明平台索引、权限或 Agent 编排成功。

## 版本与接口依据

目标 Nexent v2.8.3，官方提交 `046d77c8c479d30ccd1ed44cd37f727547798d0f`。
依据 [northbound_knowledge_app.py](https://github.com/ModelEngine-Group/nexent/blob/046d77c8c479d30ccd1ed44cd37f727547798d0f/backend/apps/northbound_knowledge_app.py)
（SHA256 `15b3757ab8031c15d99045790533a5d65924ab0fa0c709a64175837764384c38`），
[HybridSearchRequest](https://github.com/ModelEngine-Group/nexent/blob/046d77c8c479d30ccd1ed44cd37f727547798d0f/backend/consts/model.py)
与 [文件实现](https://github.com/ModelEngine-Group/nexent/blob/046d77c8c479d30ccd1ed44cd37f727547798d0f/backend/management/services/file/service.py)。未复制平台源码。

| 操作 | 原生请求 | 核验 |
|---|---|---|
| 上传 | POST `/nb/v1/knowledge/file/upload`；multipart `file` 与 `index_name` | `uploaded_file_paths` 一个对象名且无 errors；201 仅上传受理 |
| 原文 | GET `/nb/v1/knowledge/file/download/{编码的对象名}?download=stream` | 字节 SHA256 必须等于资产清单；不跟随重定向 |
| 索引 | POST `/nb/v1/knowledge/indices/{物理索引名}/chunks?path_or_url=...&page=1&page_size=1` | `status=success` 且有 chunk 才记 verified；空数组记 indexing_pending |
| 检索 | POST `/nb/v1/knowledge/indices/search/hybrid`；`query,index_names,top_k=10,weight_accurate=0.7` | results/total、index、path_or_url、id、content、score；每篇命中文档再次下载并核对全文 SHA |

原生北向路由要求资产管理员上下文。`risk_reader` 是本适配器的固定服务角色，不能取代原生管理员鉴权，也不能给调用人提权。用独立测试租户/专用索引，将管理员凭据封存在受限适配服务。不要把此 MCP 无鉴权端口暴露公网。调用者不能选角色、索引、文件路径或 URL。

检索中的原生分数仅原样记录。适配器另用明确的保守词项覆盖规则（中文双字/英文词，覆盖至少 0.35）筛除无关最近邻，记录原始条数与筛除数；它不是语义准确率承诺。所有返回行先经过来源/引用校验，坏行使整次检索失败，不能用相关度过滤掩盖伪造来源。分块内容必须是指定 UTF-8 文档中的连续原文，否则 provenance_error；不静默规范化引文。

## A 的独立导入步骤

1. 在新隔离环境检出本批完整 SHA；沿用 A 当前窗口会改变证据，故不要升级当前 dbdf965 实例。
2. 在 Nexent 知识库管理界面创建**专用测试知识库**，选择该实例实际可用的 embedding 模型及维度，保留源文件。设置资产管理员拥有权限、测试 Agent 使用权限；禁止公共可见。记录实际物理索引名、模型版本与权限映射，不猜 ID。原生也提供创建 API，但本适配器不自动创建/删除库或调整权限。
3. 项目根目录 Python 3.11+；导入/HTTP 检索只需标准库。MCP 环境另安装 `mcps/knowledge/requirements.txt`（mcp 与 mcp-types 都固定 2.3.0）。已有平台 SDK 环境不被此要求覆盖。
4. 私有环境文件提供下列值，权限只给部署账户；不得把值写进命令行、代码或共享日志。

| 环境变量 | 用途 |
|---|---|
| TOPIC16_KB_BASE_URL | 真实北向 origin，不带 `/nb/v1`、查询串或用户密码 |
| TOPIC16_KB_API_TOKEN | 专用资产管理员 Bearer key |
| TOPIC16_KB_INDEX | 专用物理索引名，小写字母/数字/下划线/点/连字符 |
| TOPIC16_KB_ROLE | 导入时 asset_admin；检索服务固定 risk_reader |
| TOPIC16_KB_DB | 私有 SQLite 路径；MCP 必填，与业务应用共用同一新库 |
| PRIVATE_EVIDENCE | 本次新隔离证据目录，由 A 预先创建 |

```bash
python -m knowledge assets --db "$TOPIC16_KB_DB" --output "$PRIVATE_EVIDENCE/assets.json"
python -m knowledge import --db "$TOPIC16_KB_DB" --output "$PRIVATE_EVIDENCE/import-1.json"
# indexing_pending 时等待平台处理完后复核，输出使用新文件名：
python -m knowledge import --db "$TOPIC16_KB_DB" --output "$PRIVATE_EVIDENCE/import-2.json"
```

重复导入验证原有 native path，不再次上传。同 doc_id/version 内容变化拒绝，须增加版本并保留旧资产。上传超时/进程中断留下 unknown/uploading，无 native path 时禁止自动重发：A 在独立平台核查原上传；确需补录应另交带原生对象证据的迁移，不能手工改成 verified。同库不复用另一 endpoint 的导入记录。旧引用、原文版本及私有源文件需持续保留。

## 检索与业务记录

切换私有配置中的固定服务角色为 risk_reader 后，启动：

```bash
python -m mcps.knowledge.server --transport sse --host 127.0.0.1 --port 8771
# CLI 直调对照，不能代替 Agent 调用证据
python -m knowledge query --db "$TOPIC16_KB_DB" --run-id "$RUN_ID" --task-key "kb-policy" --query "止损阈值如何触发" --output "$PRIVATE_EVIDENCE/query-supported.json"
python -m knowledge query --db "$TOPIC16_KB_DB" --run-id "$RUN_ID" --task-key "kb-absent" --query "火星引力公式" --output "$PRIVATE_EVIDENCE/query-absent.json"
python -m knowledge records --db "$TOPIC16_KB_DB" --run-id "$RUN_ID" --output "$PRIVATE_EVIDENCE/retrievals.json"
```

工具精确名 `search_portfolio_knowledge(query,run_id,task_key)`，SDK 导出的 schema 见 `../mcps/knowledge/schema.json`。成功 hit/no_match 都为对象；双通道 JSON 内容一致，显式 isError=false。permission_denied、configuration_error、service_error、timeout、provenance_error 为 isError=true，不是合法无依据。

A 的真实 Agent 验证只提供上述查询和本次业务标识，不把期望结论放进 prompt。第一条应能查回 policy 的原文及哈希；第二条应 no_match 且无编造引用。再以无权限配置验证拒绝、不返回正文。发布时固定工具 schema，启用本项目可选对象观察器，验证原生调用 ID、实际 query/run/task、唯一回执、显式成功和外层接受。观察流 ID 与业务 run_id 不是同一概念。

直接 HTTP 的 retrieval_id 明确是 project-local-correlation；native_call_id=null，不伪造生产 ID。平台 MCP 原生 ID 应由原生回执关联到实际参数及返回的 retrieval_id，保留私有原始回执。缺这些证据不能声明 Agent 链路通过。

现有 `POST /api/monitor` 增加 `knowledge_queries:[{portfolio_id,query}]`，每个查询使用该业务 run_id 和组合/快照 task_key。SQLite `knowledge_records` 保存完整检索记录，信号 detail 存 retrieval_id 和状态；`GET /api/knowledge?run_id=...` 可重读。无依据/错误令 knowledge_review_required=true，仍保留原始行情/新闻独立产生的风险。知识库在本版是辅助项目规则解释，不替代阈值或事实判断。外部文档永远是非可信数据。

迁移只新增 knowledge_imports、knowledge_endpoints、knowledge_records 三表和查询索引；不改变旧 run 或信号。私有库及引用正文不提交仓库。

## 开发验证与尚缺证据

```bash
python -m unittest discover -s knowledge/tests -v
python -m unittest discover -s mcps/knowledge/tests -v
python -m unittest discover -s app/tests -v
```

原生接口替身、真实 SDK handler、临时 SQLite 是开发验证；真实 embedding/索引、平台权限、Agent 调用 ID 跨层传播和正式业务验收仍由 A 独立执行。项目资料不含正式候选/开发评测集、参考答案、人工标签或真实公司事实。
