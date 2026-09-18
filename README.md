# 安全可控 SQL Agent

一个可直接运行的 AI Agent 工程化 MVP。它把自然语言查询处理为一个受控的五阶段工作流：

```text
Recall → Writer → Reviewer → Fix → Runner
                  │
                  ├─ 结构化数据查询（SQLite）
                  └─ 无合适表时，RAG 知识库兜底
```

项目刻意将 **SQL 生成** 与 **SQL 执行权限** 分开：系统只能自动执行经过审查的只读 `SELECT`；潜在危险 SQL 会被阻断并写入审计日志。该设计可扩展为人工审批后执行写操作，但 Demo 默认不开放写库能力。

## 已实现能力

- 三路元数据召回：表名/字段名、业务别名、语义关键词。
- 可替换的 SQL Writer：内置规则 Provider 保证 Demo 离线可跑；接口已预留给任意 LLM。
- SQL Reviewer：单语句、只读、白名单表/字段、`LIMIT`、危险关键字检查。
- 风险分级与审批单：`AUTO`、`MANUAL`、`BLOCKED`；审批前展示影响行数预估与样本，支持同意/拒绝、审批意见、审批人、30 分钟有效期和完整审计。
- 可验证审计：审计事件以 SHA-256 哈希链串联，控制台可校验链路连续性，帮助发现本地数据被意外修改的情况。
- Agentic RAG：无法生成可执行查询时，以运维知识库生成带来源的回答。
- Context Engineering：超预算上下文先落盘、再保留摘要，减少后续提示词负担。
- Skill 加载：读取 `skills/*/SKILL.md`，将 SOP 作为可复用运行时能力。
- FastAPI、真正逐阶段推送的 SSE 事件流、SQLite 示例数据、持久化会话记忆、审批执行开关、单元测试和离线评测集。

## 快速开始

需要 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m app.seed
uvicorn app.main:app --reload
```

访问 `http://127.0.0.1:8000/` 打开可视化运营控制台；`http://127.0.0.1:8000/docs` 为 Swagger 接口文档。

## 可选：接入真实服务

复制 `.env.example` 中需要的变量到系统环境或 `.env` 加载方案中。

- 配置 `SAFE_SQL_AGENT_LLM_*` 后，SQL Writer 优先调用 OpenAI 兼容接口；调用失败会自动降级为离线规则 Provider。
- 配置 `SAFE_SQL_AGENT_MYSQL_URL` 后，审查通过的只读 SQL 会发往 MySQL，并从 `information_schema` 读取真实表/字段用于召回。安装驱动：`python -m pip install -e ".[mysql]"`。
- 写库默认关闭。安全模式审批后会显示 `approved_safe_mode`，表示“审批已留痕但没有执行 SQL”。仅在本地演示受控审批执行时设置 `SAFE_SQL_AGENT_ALLOW_APPROVED_WRITES=true`；Demo 只白名单允许 `DELETE FROM alerts WHERE status = 'closed'`，并会在真正执行前自动把 SQLite 备份到 `data/backups/`。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 演示请求

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/query -ContentType 'application/json' -Body '{"question":"查询最近的 P1 告警", "requester":"demo-user"}'
```

可尝试：

- `查询华东区离线设备`
- `列出未关闭的高优工单`
- `P1 告警应该如何处理`（RAG 兜底）

## 目录

```text
app/
  main.py          # HTTP API 与 SSE
  workflow.py      # Recall → Writer → Reviewer → Fix → Runner
  database.py      # SQLite、示例业务数据、审计与审批持久化
  sql_agent.py     # Provider、检索、审查、风险分级
  rag.py           # 轻量 Agentic RAG
  context.py       # 上下文压缩与落盘
  skills.py        # Skill.md 加载
skills/            # 可复用运维 SOP
tests/             # 核心安全与流程测试
```

更完整的请求链路、安全边界、评测口径和五分钟演示脚本见 [docs/architecture.md](docs/architecture.md)。

## 生产化替换点

1. 用真实 LLM Provider 替换 `RuleBasedSqlWriter`，要求输出严格 JSON（SQL、涉及表、置信度）。
2. 接入 MySQL/PostgreSQL 只读账号和数据库解析器（如 sqlglot），不要复用本 Demo 的正则检查。
3. 将审批单接入企业 IAM、工单/SSE 通知；执行器使用短期凭证、行数/扫描量/超时硬限制。
4. 接入向量库、Embedding 和离线评测平台，持续监控成功率、拒绝率、P95 延迟与高危拦截率。
5. 审计哈希链是本地 Demo 的篡改检测机制；生产环境应改为基于 KMS/HSM 的 HMAC 或外部不可变审计存储。
