"""SQLite 事实库：业务数据、知识版本、聊天记忆、审批和审计持久化。

本模块是 OpsPilot 的本地事实库（source of truth），负责：
1. 存放演示业务表：资产、告警、工单、作业单；
2. 存放知识库文档、分块、历史版本和 RAG 人工评分；
3. 存放指标定义、样本和 CSV 导入记录；
4. 存放审批单、审计日志及其哈希链、维护日志；
5. 存放 Agent 会话、聊天消息和记忆；
6. 提供只读 SQL、影响预估、审批执行、审计完整性校验等受控入口。

安全边界：
- 所有本地文件位于项目根目录 data/ 下，便于备份迁移；
- 数据浏览器只允许白名单表，不接受任意表名；
- 审批执行的写操作仅限 allow-list 中的一条 SQL；
- 默认安全模式下审批只记录不落库；
- 审计写入采用链式哈希，保证可校验。
"""

from __future__ import annotations  # 延迟解析类型注解，避免运行时求值

import csv  # 解析 CSV 指标导入文件
import io  # 将字符串包装成文件对象供 csv 使用
import json  # 序列化影响预估、执行结果和审计 payload
import math  # 校验浮点数是否为有限值
import sqlite3  # 本地事实库驱动
from hashlib import sha256  # 审计哈希链使用的摘要算法
from datetime import datetime, timedelta, timezone  # 处理 UTC 时间和审批有效期
from pathlib import Path  # 跨平台路径处理
from typing import Any  # 宽松字典类型标注
from uuid import uuid4  # 生成审批、审计、会话等唯一 ID

from app.config import settings  # 读取配置，用于判断 MySQL 是否启用等

# 所有本地持久化文件都位于项目根目录 data/，便于演示环境备份和迁移。
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# SQLite 事实库：业务表、知识库、会话、审批和审计都从这里读取。
DB_PATH = DATA_DIR / "agent_demo.db"

# 审批执行前生成的 SQLite 备份目录。
BACKUP_DIR = DATA_DIR / "backups"

# 待审批变更的默认有效期，过期后不可继续审批执行。
APPROVAL_TTL = timedelta(minutes=30)

# 数据浏览器允许展示的表白名单；它不是任意 SQL 执行入口。
# key 是实际表名，value 是前端展示用的中文名。
EXPLORER_TABLES = {
    "assets": "设备资产",
    "alerts": "监控告警",
    "tickets": "运维工单",
    "work_orders": "作业任务",
    "metric_definitions": "指标定义",
    "metric_samples": "指标样本",
    "metric_imports": "指标导入记录",
    "evaluation_cases": "自定义评测用例",
    "knowledge_documents": "知识库文档",
    "knowledge_chunks": "知识库分块",
    "knowledge_eval_feedback": "RAG 人工评分",
    "approvals": "审批单",
    "audit_logs": "审计日志",
    "audit_maintenance_logs": "审计维护日志",
    "conversation_memory": "会话记忆",
    "chat_conversations": "Agent 聊天会话",
    "chat_messages": "Agent 聊天消息",
}


# ---------- 连接与初始化 ----------

def utc_now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串，作为所有时间字段的统一格式。"""
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    """建立 SQLite 连接。

    - 确保 data/ 目录存在；
    - 使用 sqlite3.Row 作为 row_factory，使查询结果可按列名访问；
    - 调用方负责关闭连接（通常通过 with 语句）。
    """
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Row 支持 row["column"] 形式访问，比 tuple 更直观。
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    """幂等创建表结构并执行轻量迁移。

    初始化不会删除已有数据；只补建缺少的表、索引和历史版本字段，并为旧审计记录补齐哈希链。
    因此服务每次启动都可以安全调用该函数。

    主要工作：
    1. 用 CREATE TABLE IF NOT EXISTS 补建所有业务表；
    2. 用 PRAGMA table_info 检查缺失列并 ALTER TABLE 补列；
    3. 回填 knowledge_documents 的 updated_at 和初始版本；
    4. 建唯一索引防止指标样本重复；
    5. 回填审计哈希链。
    """
    with connect() as conn:
        # 一次性创建所有表；IF NOT EXISTS 保证幂等。
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
              status TEXT NOT NULL, owner TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
              id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, severity TEXT NOT NULL,
              title TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(asset_id) REFERENCES assets(id)
            );
            CREATE TABLE IF NOT EXISTS tickets (
              id INTEGER PRIMARY KEY, priority TEXT NOT NULL, title TEXT NOT NULL,
              status TEXT NOT NULL, assignee TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_orders (
              id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, action TEXT NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(asset_id) REFERENCES assets(id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_documents (
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1, updated_at TEXT, expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL, version INTEGER NOT NULL,
              title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(document_id) REFERENCES knowledge_documents(id)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              action TEXT NOT NULL, payload TEXT NOT NULL, prev_hash TEXT, entry_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_maintenance_logs (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, actor TEXT NOT NULL,
              operation TEXT NOT NULL, target_event_id TEXT, target_action TEXT,
              cutoff TEXT, deleted_count INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              sql TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
              risk_level TEXT NOT NULL DEFAULT 'high', impact_preview TEXT,
              decision_comment TEXT, decided_by TEXT, decided_at TEXT,
              expires_at TEXT, executed_at TEXT, execution_result TEXT
            );
            CREATE TABLE IF NOT EXISTS conversation_memory (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_conversations (
              id TEXT PRIMARY KEY, requester TEXT NOT NULL, title TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(conversation_id) REFERENCES chat_conversations(id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id);
            CREATE TABLE IF NOT EXISTS metric_definitions (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
              unit TEXT NOT NULL, asset_scope TEXT NOT NULL, description TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'demo'
            );
            CREATE TABLE IF NOT EXISTS metric_samples (
              id INTEGER PRIMARY KEY AUTOINCREMENT, metric_id INTEGER NOT NULL,
              observed_at TEXT NOT NULL, value REAL NOT NULL,
              FOREIGN KEY(metric_id) REFERENCES metric_definitions(id)
            );
            CREATE TABLE IF NOT EXISTS metric_imports (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              filename TEXT NOT NULL, total_rows INTEGER NOT NULL, sample_count INTEGER NOT NULL,
              created_metrics INTEGER NOT NULL, updated_metrics INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluation_cases (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              name TEXT NOT NULL, question TEXT NOT NULL, expected_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
              chunk_index INTEGER NOT NULL, content TEXT NOT NULL, token_count INTEGER NOT NULL,
              FOREIGN KEY(document_id) REFERENCES knowledge_documents(id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_eval_feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL,
              question TEXT NOT NULL, score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
              comment TEXT NOT NULL DEFAULT '', requester TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )

        # Lightweight migrations keep existing local demo databases compatible.
        # 下面这些迁移用于兼容早期演示库；对不存在字段的执行 ALTER TABLE。

        # approvals 表补列：executed_at、execution_result、risk_level、impact_preview 等。
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
        if "executed_at" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN executed_at TEXT")
        if "execution_result" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN execution_result TEXT")
        if "risk_level" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'high'")
        if "impact_preview" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN impact_preview TEXT")
        if "decision_comment" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN decision_comment TEXT")
        if "decided_by" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN decided_by TEXT")
        if "decided_at" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN decided_at TEXT")
        if "expires_at" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN expires_at TEXT")

        # audit_logs 表补列：prev_hash、entry_hash，用于哈希链。
        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_logs)").fetchall()}
        if "prev_hash" not in audit_columns:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN prev_hash TEXT")
        if "entry_hash" not in audit_columns:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN entry_hash TEXT")

        # metric_definitions 表补列：source，用于区分 demo/csv 来源。
        metric_columns = {row["name"] for row in conn.execute("PRAGMA table_info(metric_definitions)").fetchall()}
        if "source" not in metric_columns:
            conn.execute("ALTER TABLE metric_definitions ADD COLUMN source TEXT NOT NULL DEFAULT 'demo'")

        # knowledge_documents 表补列：version、updated_at、expires_at，用于版本和过期管理。
        knowledge_columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()}
        if "version" not in knowledge_columns:
            conn.execute("ALTER TABLE knowledge_documents ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "updated_at" not in knowledge_columns:
            conn.execute("ALTER TABLE knowledge_documents ADD COLUMN updated_at TEXT")
        if "expires_at" not in knowledge_columns:
            conn.execute("ALTER TABLE knowledge_documents ADD COLUMN expires_at TEXT")

        # 为旧文档回填 updated_at；COALESCE 保证已存在值不被覆盖。
        conn.execute("UPDATE knowledge_documents SET updated_at = COALESCE(updated_at, ?)", (utc_now(),))

        # 为还没有历史版本的文档补一条初始版本记录。
        conn.execute(
            "INSERT INTO knowledge_versions (document_id, version, title, content, tags, created_at) "
            "SELECT id, version, title, content, tags, updated_at FROM knowledge_documents d "
            "WHERE NOT EXISTS (SELECT 1 FROM knowledge_versions v WHERE v.document_id = d.id AND v.version = d.version)"
        )

        # 指标样本唯一索引：同一指标同一时间点只保留一条，便于 CSV upsert。
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_samples_metric_time ON metric_samples(metric_id, observed_at)")

        # 最后回填审计哈希链，保证旧数据也有 prev_hash 和 entry_hash。
        _backfill_audit_hashes(conn)


# ---------- 演示数据和指标 CSV ----------

def seed_demo_data() -> None:
    """填充演示业务数据。

    幂等：如果 assets 表已有记录，则只补充知识库内置文档后返回。
    否则写入一组固定的资产、告警、工单、作业单和初始知识文档。
    """
    initialize()
    with connect() as conn:
        # 判断是否已填充过业务数据。
        count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        if count:
            # 已有数据则只补充内置知识库（不会覆盖用户文档）。
            seed_knowledge_library()
            return

        # 演示资产：覆盖华东/华南/华北，状态包含 online/offline/maintenance。
        conn.executemany(
            "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "edge-gateway-sh-01", "华东", "online", "li", "2026-09-18T01:00:00Z"),
                (2, "edge-gateway-sh-02", "华东", "offline", "wang", "2026-09-18T01:20:00Z"),
                (3, "robot-gz-01", "华南", "maintenance", "zhao", "2026-09-17T18:20:00Z"),
                (4, "edge-gateway-bj-01", "华北", "offline", "chen", "2026-09-18T00:50:00Z"),
            ],
        )
        # 演示告警：包含 P1/P2/P3 不同等级和 open/acknowledged/closed 状态。
        conn.executemany(
            "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (101, 2, "P1", "华东网关离线", "open", "2026-09-18T01:15:00Z"),
                (102, 4, "P1", "华北网关离线", "acknowledged", "2026-09-18T00:45:00Z"),
                (103, 3, "P2", "机器人电池低", "open", "2026-09-17T18:10:00Z"),
                (104, 1, "P3", "网关磁盘使用率高", "closed", "2026-09-16T09:10:00Z"),
            ],
        )
        # 演示工单：覆盖高/中优先级和 open/in_progress/closed 状态。
        conn.executemany(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?)",
            [
                (201, "high", "华东网关离线排障", "open", "li", "2026-09-18T01:16:00Z"),
                (202, "medium", "机器人电池更换", "in_progress", "zhao", "2026-09-17T18:30:00Z"),
                (203, "high", "华北网络链路检查", "closed", "chen", "2026-09-17T15:00:00Z"),
            ],
        )
        # 演示作业单：一条 pending 的派单。
        conn.executemany(
            "INSERT INTO work_orders VALUES (?, ?, ?, ?, ?)",
            [(301, 2, "dispatch_engineer", "pending", "2026-09-18T01:17:00Z")],
        )
        # 初始内置知识文档，供 RAG 演示使用。
        conn.executemany(
            "INSERT INTO knowledge_documents (id, title, content, tags) VALUES (?, ?, ?, ?)",
            [
                (1, "P1 告警处置 SOP", "P1 告警需要在 5 分钟内确认。先确认告警范围，再检查设备网络与心跳；无法恢复时创建高优工单并升级值班负责人。", "P1,告警,SOP,升级"),
                (2, "设备离线排障手册", "设备离线时依次检查供电、网络连通性、最近心跳和网关日志。若是单设备故障，优先安排现场巡检；若同区域批量离线，按网络故障升级。", "离线,设备,网络,排障"),
                (3, "工单优先级规范", "高优工单要求在两小时内响应。关闭工单前必须记录根因、处理动作与验证结果。", "工单,优先级,规范"),
            ],
        )
    # 再补充更完整的内置知识库。
    seed_knowledge_library()


def seed_knowledge_library() -> None:
    """补充内置运维知识库，不覆盖用户创建或编辑的文档。

    通过 WHERE NOT EXISTS 按标题去重：已存在同标题文档时跳过，保证幂等。
    """
    # 内置文档：标题、内容、标签。覆盖常见运维主题，便于演示 RAG 检索效果。
    documents = [
        ("告警确认与分级 SOP", "收到告警后先确认告警时间、影响资源、持续时长和是否存在关联告警。P1 需要在 5 分钟内确认并通知值班负责人；P2 在 30 分钟内完成初步定位；P3 纳入工作日排期。告警关闭前必须记录根因、处理动作和验证证据。", "告警,分级,确认,SOP"),
        ("网络链路丢包与延迟排障", "发现链路丢包或延迟升高时，先比对同区域和跨区域指标，再检查网关端口错误、带宽利用率、路由变更和 DNS 解析。单节点异常优先检查设备与接入链路；多节点同时异常升级网络值班，并保留 ping、traceroute 和监控截图。", "网络,丢包,延迟,链路,排障"),
        ("网关磁盘空间清理规范", "网关磁盘使用率超过 80% 时，先确认增长目录、日志保留策略和是否存在异常转储文件。只允许按保留规范清理可再生日志与过期缓存；不得直接删除业务数据、配置文件或未确认的转储。清理后复核磁盘使用率和服务日志。", "网关,磁盘,日志,容量,规范"),
        ("服务 CPU 与内存异常排查", "CPU 或内存持续超过阈值时，确认是瞬时波动还是持续增长，再比对发布记录、请求量、线程池、GC 日志和依赖服务延迟。优先限流、扩容或回滚已确认异常的发布；需要重启前先确认流量切换、会话影响和回滚方案。", "服务,CPU,内存,GC,性能"),
        ("数据库连接池耗尽应急 SOP", "连接池使用率持续高于 90% 时，检查慢 SQL、未释放连接、连接超时配置和应用实例数量。先通过只读查询确认活跃连接来源与持续时间，再评估限流或扩容。禁止在未备份和未审批的情况下执行 KILL、DDL 或清库操作。", "数据库,连接池,慢SQL,应急,SOP"),
        ("消息队列积压处置指南", "消息积压出现后，确认积压主题、生产速率、消费速率、失败重试和下游依赖状态。若消费者异常，先恢复消费能力并观察积压斜率；若下游不可用，按业务优先级限流或暂停生产。处理完成后记录积压峰值、恢复时间和遗留消息数。", "消息队列,积压,消费,重试,排障"),
        ("发布变更前检查清单", "发布前必须确认变更单已审批、影响范围明确、回滚包可用、监控看板已准备，并通知相关值班人员。执行窗口内先进行小流量验证，观察错误率、延迟和资源指标。任何关键指标异常都应停止扩大范围并进入回滚判断。", "发布,变更,检查,审批,灰度"),
        ("发布失败回滚与验证 SOP", "发布失败时先停止继续扩散，记录失败版本、错误日志和影响实例。按已审批的回滚方案恢复上一稳定版本，再验证核心接口、关键任务、告警恢复和数据一致性。回滚完成不代表事件结束，需补充根因分析和预防措施。", "发布,回滚,验证,故障,SOP"),
        ("值班交接与事件升级规范", "交接时应列出未关闭 P1/P2 告警、进行中的工单、风险变更、观察指标和下一检查时间。事件达到升级条件时，说明影响范围、已完成动作、当前证据和需要协助的决策。交接信息必须可追溯，避免仅口头传递。", "值班,交接,升级,事件,规范"),
        ("指标异常波动分析手册", "分析指标异常时，先确认数据来源、采样间隔和基线范围，再查看趋势、同比资源与关联告警。区分单点尖峰、周期性波动和持续劣化；只有在指标、日志和业务影响相互印证后，才将其判定为故障根因。", "指标,趋势,异常,分析,监控"),
    ]
    with connect() as conn:
        for title, content, tags in documents:
            # 按标题去重，已存在则跳过，不覆盖用户修改。
            conn.execute(
                "INSERT INTO knowledge_documents (title, content, tags) SELECT ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM knowledge_documents WHERE title = ?)",
                (title, content, tags, title),
            )


def seed_metric_demo_data() -> None:
    """创建确定性的本地指标目录和 24 小时样本，仅用于无真实数据时的页面演示。

    幂等：如果 metric_definitions 已有记录则直接返回。
    使用固定公式生成 baseline 和 wave，保证每次演示数据一致，便于截图和测试。
    """
    initialize()
    with connect() as conn:
        # 已有指标定义则跳过，避免覆盖真实导入数据。
        if conn.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]:
            return

        # 10 类指标族：主机/网络/应用/消息队列/数据库/作业。
        families = [
            ("CPU 使用率", "主机", "%"), ("内存使用率", "主机", "%"), ("磁盘使用率", "主机", "%"),
            ("网络入站流量", "网络", "MB/s"), ("网络出站流量", "网络", "MB/s"), ("请求延迟 P95", "应用", "ms"),
            ("请求成功率", "应用", "%"), ("消息积压量", "消息队列", "条"), ("数据库连接池使用率", "数据库", "%"),
            ("任务执行耗时", "作业", "s"),
        ]
        definitions = []
        samples = []
        # 起始时间固定，保证演示数据可复现。
        start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)

        # 生成 700 个指标定义，每个指标 24 个样本点。
        for metric_id in range(1, 701):
            label, category, unit = families[(metric_id - 1) % len(families)]
            scope = f"asset-{(metric_id - 1) % 4 + 1}"
            definitions.append((
                metric_id,
                f"{label} · {scope} · #{metric_id:03d}",
                category, unit, scope,
                f"{label} 的本地演示时序指标",
            ))

            # 根据指标 ID 生成稳定的 baseline，避免每次运行结果不同。
            baseline = 35 + (metric_id * 7) % 45
            # 不同单位做量纲调整，使数值分布更自然。
            if unit == "ms":
                baseline *= 3
            elif unit == "MB/s":
                baseline /= 2
            elif unit == "条":
                baseline *= 12
            elif unit == "s":
                baseline /= 4

            # 24 小时样本，使用固定公式生成周期性波动。
            for point in range(24):
                wave = ((point * 11 + metric_id * 3) % 17) - 8
                samples.append((
                    metric_id,
                    (start + timedelta(hours=point)).isoformat(),
                    round(max(0.1, baseline + wave), 2),
                ))

        # 批量写入指标定义和样本。
        conn.executemany(
            "INSERT INTO metric_definitions (id, name, category, unit, asset_scope, description) VALUES (?, ?, ?, ?, ?, ?)",
            definitions,
        )
        conn.executemany(
            "INSERT INTO metric_samples (metric_id, observed_at, value) VALUES (?, ?, ?)",
            samples,
        )


# 真实 CSV 导入允许的中英文列名别名；左侧是内部统一字段名。
CSV_HEADER_ALIASES = {
    "metric_name": ("metric_name", "name", "指标名称", "指标名"),
    "category": ("category", "分类"),
    "unit": ("unit", "单位"),
    "asset_scope": ("asset_scope", "scope", "资源范围", "资产范围", "资源", "资产"),
    "observed_at": ("observed_at", "timestamp", "time", "采集时间", "时间", "时间戳"),
    "value": ("value", "指标值", "数值", "值"),
    "description": ("description", "描述", "说明"),
}

# 单次导入上限，防止本地服务被异常大文件耗尽内存。
MAX_METRIC_IMPORT_ROWS = 50_000


def metric_csv_template() -> str:
    """返回指标 CSV 模板文本，供前端下载或展示。"""
    return (
        "metric_name,category,unit,asset_scope,observed_at,value,description\n"
        "CPU 使用率,主机,%,edge-gateway-sh-01,2026-09-19T09:00:00+08:00,68.5,生产网关 CPU 使用率\n"
        "CPU 使用率,主机,%,edge-gateway-sh-01,2026-09-19T10:00:00+08:00,72.1,生产网关 CPU 使用率\n"
    )


def _resolve_csv_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """把 CSV 表头解析为内部标准字段名。

    参数：
    - fieldnames：csv.DictReader 提供的表头列表。

    返回：
    - dict：内部字段名 -> CSV 中实际列名。

    异常：
    - ValueError：缺少必填列时抛出，提示缺失列名。
    """
    # 归一化表头：小写、去空白，便于匹配别名。
    normalized = {
        str(name).strip().lower(): str(name)
        for name in fieldnames or [] if name and str(name).strip()
    }
    resolved: dict[str, str] = {}
    # 对每个标准字段，按别名顺序找到第一个匹配的 CSV 列。
    for canonical, aliases in CSV_HEADER_ALIASES.items():
        for alias in aliases:
            matched = normalized.get(alias.lower())
            if matched:
                resolved[canonical] = matched
                break

    # 必填列清单，缺任何一个都视为格式错误。
    required = ("metric_name", "category", "unit", "asset_scope", "observed_at", "value")
    missing = [name for name in required if name not in resolved]
    if missing:
        raise ValueError("CSV 缺少必填列：" + "、".join(missing))
    return resolved


def _parse_observed_at(value: str) -> str:
    """解析采集时间字符串为规范 ISO 8601。

    - 支持 Z 结尾的 UTC 时间；
    - 必须是 ISO 8601 格式，否则抛出 ValueError。
    """
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError("采集时间必须是 ISO 8601 格式，例如 2026-09-19T09:00:00+08:00") from exc


def import_metric_csv(content: str, filename: str, requester: str) -> dict[str, int | str]:
    """校验并 upsert 真实指标 CSV 行，不覆盖无关指标。

    流程：
    1. 解析表头，校验必填列；
    2. 逐行校验字段完整性、长度、数值有效性、时间格式；
    3. 限制单次导入行数，防止内存耗尽；
    4. 按 (name, category, unit, asset_scope) 去重，新建或复用指标定义；
    5. 使用 ON CONFLICT 对同指标同时间点做 upsert；
    6. 写入 metric_imports 记录本次导入。

    参数：
    - content：CSV 文本；
    - filename：上传文件名，用于审计记录；
    - requester：导入人标识。

    返回：
    - dict：导入统计信息。
    """
    reader = csv.DictReader(io.StringIO(content))
    headers = _resolve_csv_headers(reader.fieldnames)
    parsed_rows: list[tuple[str, str, str, str, str, float, str]] = []

    # 逐行解析并校验，行号从 2 开始（第 1 行是表头）。
    for line_number, row in enumerate(reader, start=2):
        # 超过最大行数限制直接拒绝，避免内存和数据库压力。
        if len(parsed_rows) >= MAX_METRIC_IMPORT_ROWS:
            raise ValueError(f"CSV 最多允许 {MAX_METRIC_IMPORT_ROWS} 条数据行")

        # 按解析出的列名取值，统一去空白。
        values = {key: str(row.get(column) or "").strip() for key, column in headers.items()}

        # 跳过完全空行。
        if not any(values.values()):
            continue

        # 必填字段不能为空。
        required = ("metric_name", "category", "unit", "asset_scope", "observed_at", "value")
        if any(not values[name] for name in required):
            raise ValueError(f"第 {line_number} 行存在空的必填字段")

        # 元数据字段长度限制，防止超长内容污染数据库。
        if any(len(values[name]) > 160 for name in ("metric_name", "category", "unit", "asset_scope")):
            raise ValueError(f"第 {line_number} 行的指标元数据超过长度限制")

        # value 必须是合法有限浮点数。
        try:
            numeric_value = float(values["value"])
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行的 value 必须是数值") from exc
        if not math.isfinite(numeric_value):
            raise ValueError(f"第 {line_number} 行的 value 必须是有限数值")

        # 解析采集时间。
        observed_at = _parse_observed_at(values["observed_at"])

        parsed_rows.append((
            values["metric_name"], values["category"], values["unit"],
            values["asset_scope"], observed_at, numeric_value,
            values.get("description", ""),
        ))

    if not parsed_rows:
        raise ValueError("CSV 没有可导入的数据行")

    created_metrics = 0
    # 缓存已解析的指标定义 ID，避免重复查询。
    metric_ids: dict[tuple[str, str, str, str], int] = {}

    with connect() as conn:
        # 第一遍：确保所有指标定义存在，记录 ID。
        for name, category, unit, scope, _, _, description in parsed_rows:
            key = (name, category, unit, scope)
            if key in metric_ids:
                continue

            # 查询是否已有同 name/category/unit/scope 的指标定义。
            existing = conn.execute(
                "SELECT id FROM metric_definitions WHERE name = ? AND category = ? AND unit = ? AND asset_scope = ?",
                key,
            ).fetchone()

            if existing is None:
                # 新建指标定义，source 标记为 csv。
                cursor = conn.execute(
                    "INSERT INTO metric_definitions (name, category, unit, asset_scope, description, source) "
                    "VALUES (?, ?, ?, ?, ?, 'csv')",
                    (name, category, unit, scope, description or "从真实 CSV 导入的时序指标"),
                )
                metric_ids[key] = int(cursor.lastrowid)
                created_metrics += 1
            else:
                # 复用已有指标定义。
                metric_ids[key] = int(existing["id"])

        # 第二遍：批量 upsert 样本，同一指标同一时间点覆盖。
        conn.executemany(
            "INSERT INTO metric_samples (metric_id, observed_at, value) VALUES (?, ?, ?) "
            "ON CONFLICT(metric_id, observed_at) DO UPDATE SET value = excluded.value",
            [
                (metric_ids[(name, category, unit, scope)], observed_at, value)
                for name, category, unit, scope, observed_at, value, _ in parsed_rows
            ],
        )

        # 记录本次导入，供页面展示和审计追溯。
        import_id = str(uuid4())
        conn.execute(
            "INSERT INTO metric_imports (id, created_at, requester, filename, total_rows, sample_count, "
            "created_metrics, updated_metrics) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                import_id, utc_now(), requester,
                Path(filename).name[:180] or "metrics.csv",
                len(parsed_rows), len(parsed_rows),
                created_metrics, len(metric_ids) - created_metrics,
            ),
        )

    return {
        "import_id": import_id,
        "total_rows": len(parsed_rows),
        "sample_count": len(parsed_rows),
        "created_metrics": created_metrics,
        "updated_metrics": len(metric_ids) - created_metrics,
    }


# 评测结果只比较业务状态，避免把模型自然语言微小差异误判为失败。
EVALUATION_STATUSES = {"completed", "answered_by_rag", "approval_required", "blocked"}


# ---------- 评测用例与只读 SQL ----------

def list_evaluation_cases() -> list[dict[str, str]]:
    """按创建时间倒序返回所有评测用例。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, requester, name, question, expected_status "
            "FROM evaluation_cases ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def add_evaluation_case(name: str, question: str, expected_status: str, requester: str) -> dict[str, str]:
    """新增评测用例。

    - expected_status 必须在 EVALUATION_STATUSES 中；
    - name 长度 2~80，question 长度 2~500，否则抛 ValueError。
    """
    if expected_status not in EVALUATION_STATUSES:
        raise ValueError("预期结果必须是 completed、answered_by_rag、approval_required 或 blocked")

    case = {
        "id": str(uuid4()),
        "created_at": utc_now(),
        "requester": requester,
        "name": name.strip(),
        "question": question.strip(),
        "expected_status": expected_status,
    }
    # 基础长度校验，防止异常输入写入数据库。
    if not (2 <= len(case["name"]) <= 80 and 2 <= len(case["question"]) <= 500):
        raise ValueError("用例名称需为 2-80 个字符，问题需为 2-500 个字符")

    with connect() as conn:
        conn.execute(
            "INSERT INTO evaluation_cases (id, created_at, requester, name, question, expected_status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (case["id"], case["created_at"], case["requester"], case["name"], case["question"], case["expected_status"]),
        )
    return case


def delete_evaluation_case(case_id: str) -> bool:
    """按 ID 删除评测用例，返回是否真的删除了记录。"""
    with connect() as conn:
        return conn.execute("DELETE FROM evaluation_cases WHERE id = ?", (case_id,)).rowcount > 0


def execute_readonly(sql: str) -> list[dict[str, Any]]:
    """执行已通过上层审查的只读 SQL，并把 Row 转成前端可序列化字典。

    说明：
    - 上层负责 SQL 白名单和只读校验，本函数不再重复校验；
    - 如果配置了 MySQL，则转发到 mysql_adapter；
    - 否则使用本地 SQLite 执行。
    """
    if settings.mysql_enabled:
        from app.mysql_adapter import execute_readonly as execute_mysql_readonly
        return execute_mysql_readonly(sql)
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


# ---------- 审计哈希链与按时间清理 ----------

def _canonical_payload(payload: dict[str, Any]) -> str:
    """把 payload 序列化为规范 JSON：键排序、紧凑分隔符、保留非 ASCII。

    规范化的目的是让相同内容的 payload 产生相同的字符串，从而哈希稳定。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit_digest(previous: str, event_id: str, created_at: str, requester: str, action: str, payload: str) -> str:
    """计算审计事件的链式哈希。

    把前一条哈希、事件 ID、时间、请求人、动作和规范 payload 用 | 拼接后做 SHA-256。
    这样任何字段变化都会改变哈希，任何插入/删除都会破坏链的连续性。
    """
    value = "|".join((previous, event_id, created_at, requester, action, payload))
    return sha256(value.encode("utf-8")).hexdigest()


def _backfill_audit_hashes(conn: sqlite3.Connection) -> None:
    """按 rowid 顺序重算并回填审计哈希链。

    - 用于 initialize 时修复旧数据；
    - 也用于删除审计记录后重新串联哈希链；
    - 只更新 entry_hash 不匹配的行，减少写入。
    """
    previous = ""
    rows = conn.execute(
        "SELECT rowid, id, created_at, requester, action, payload, entry_hash FROM audit_logs ORDER BY rowid"
    ).fetchall()
    for row in rows:
        # 重新规范化 payload，防止旧数据格式差异导致哈希不一致。
        payload = _canonical_payload(json.loads(row["payload"]))
        digest = _audit_digest(previous, row["id"], row["created_at"], row["requester"], row["action"], payload)
        if row["entry_hash"] != digest:
            conn.execute(
                "UPDATE audit_logs SET prev_hash = ?, entry_hash = ? WHERE rowid = ?",
                (previous, digest, row["rowid"]),
            )
        previous = digest


def write_audit(requester: str, action: str, payload: dict[str, Any]) -> None:
    """写入带前置哈希和当前哈希的审计事件，形成可校验的链式留痕。

    - 取最后一条非空 entry_hash 作为 previous；
    - 计算当前事件的 digest；
    - 插入时同时写入 prev_hash 和 entry_hash。
    """
    event_id, created_at = str(uuid4()), utc_now()
    canonical_payload = _canonical_payload(payload)
    with connect() as conn:
        # 找到链尾哈希作为前置哈希。
        previous_row = conn.execute(
            "SELECT entry_hash FROM audit_logs WHERE entry_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        previous = previous_row["entry_hash"] if previous_row else ""
        digest = _audit_digest(previous, event_id, created_at, requester, action, canonical_payload)
        conn.execute(
            "INSERT INTO audit_logs (id, created_at, requester, action, payload, prev_hash, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, created_at, requester, action, canonical_payload, previous, digest),
        )


def delete_audit_event(event_id: str, deleted_by: str) -> dict[str, Any] | None:
    """删除指定审计事件，并在独立维护表中记录删除动作。

    说明：
    - 删除后会重新回填哈希链，保证剩余事件链仍然连续；
    - 删除动作本身记录在 audit_maintenance_logs，避免把“删除留痕”混回业务审计。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, created_at, requester, action FROM audit_logs WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None

        # 删除目标审计事件并重建哈希链。
        conn.execute("DELETE FROM audit_logs WHERE id = ?", (event_id,))
        _backfill_audit_hashes(conn)

        # 在维护日志中记录删除动作。
        conn.execute(
            "INSERT INTO audit_maintenance_logs (id, created_at, actor, operation, target_event_id, "
            "target_action, cutoff, deleted_count, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()), utc_now(), deleted_by, "delete_event", event_id, row["action"], None, 1,
                _canonical_payload({"target_created_at": row["created_at"], "target_requester": row["requester"]}),
            ),
        )
        return {
            "deleted": True,
            "deleted_event_id": event_id,
            "deleted_action": row["action"],
            "maintenance_logged": True,
        }


def delete_audit_events(event_ids: list[str], deleted_by: str) -> dict[str, Any]:
    """批量删除指定审计事件，重建哈希链并在独立维护日志中留痕。"""
    unique_ids = list(dict.fromkeys(event_id.strip() for event_id in event_ids if event_id.strip()))
    if not unique_ids:
        return {"deleted": False, "deleted_count": 0, "missing_count": 0, "maintenance_logged": False}

    placeholders = ",".join("?" for _ in unique_ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, created_at, requester, action FROM audit_logs WHERE id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        if not rows:
            return {"deleted": False, "deleted_count": 0, "missing_count": len(unique_ids), "maintenance_logged": False}

        found_ids = [row["id"] for row in rows]
        action_counts: dict[str, int] = {}
        for row in rows:
            action_counts[row["action"]] = action_counts.get(row["action"], 0) + 1

        delete_placeholders = ",".join("?" for _ in found_ids)
        conn.execute(f"DELETE FROM audit_logs WHERE id IN ({delete_placeholders})", found_ids)
        _backfill_audit_hashes(conn)
        conn.execute(
            "INSERT INTO audit_maintenance_logs (id, created_at, actor, operation, target_event_id, "
            "target_action, cutoff, deleted_count, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()), utc_now(), deleted_by, "delete_selected", None, None, None, len(rows),
                _canonical_payload({
                    "deleted_event_ids": found_ids,
                    "action_counts": action_counts,
                    "missing_count": len(unique_ids) - len(rows),
                }),
            ),
        )
        return {
            "deleted": True,
            "deleted_count": len(rows),
            "missing_count": len(unique_ids) - len(rows),
            "action_counts": action_counts,
            "maintenance_logged": True,
        }


def _normalize_audit_cutoff(cutoff: str) -> str:
    """把审计清理时间参数规范化为 UTC ISO 8601。

    - 空字符串报错；
    - 支持 Z 结尾；
    - 无时区视为 UTC；
    - 统一转换为 UTC 时区后再序列化。
    """
    value = cutoff.strip()
    if not value:
        raise ValueError("清理时间不能为空")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("清理时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_audit_range(start: str, end: str) -> tuple[str, str]:
    """规范化审计清理时间范围，并校验 start <= end。"""
    normalized_start = _normalize_audit_cutoff(start)
    normalized_end = _normalize_audit_cutoff(end)
    if normalized_start > normalized_end:
        raise ValueError("开始时间不能晚于结束时间")
    return normalized_start, normalized_end


def audit_cleanup_preview(start: str, end: str) -> dict[str, Any]:
    """预览指定时间范围内将匹配多少审计事件。

    - 只读，不删除；
    - 返回匹配数量、最早和最晚时间，供用户确认后再执行删除。
    """
    normalized_start, normalized_end = _normalize_audit_range(start, end)
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count, MIN(created_at) AS oldest, MAX(created_at) AS newest "
            "FROM audit_logs WHERE created_at >= ? AND created_at <= ?",
            (normalized_start, normalized_end),
        ).fetchone()
    return {
        "start": normalized_start,
        "end": normalized_end,
        "matched_count": row["count"],
        "oldest": row["oldest"],
        "newest": row["newest"],
    }


def delete_audit_range(start: str, end: str, deleted_by: str) -> dict[str, Any]:
    """按包含起止边界的时间窗口批量删除审计事件。

    删除范围只作用于 audit_logs；维护日志保留在 audit_maintenance_logs，避免把“删除留痕”
    混回普通业务审计列表。调用方应先执行 audit_cleanup_preview 再确认。

    返回：
    - dict：包含删除数量、时间范围、是否记录维护日志以及各 action 的删除计数。
    """
    normalized_start, normalized_end = _normalize_audit_range(start, end)
    with connect() as conn:
        # 查出待删除的事件，用于统计 action 分布。
        rows = conn.execute(
            "SELECT id, action FROM audit_logs WHERE created_at >= ? AND created_at <= ? ORDER BY rowid",
            (normalized_start, normalized_end),
        ).fetchall()
        if not rows:
            return {
                "deleted": False, "deleted_count": 0,
                "start": normalized_start, "end": normalized_end,
                "maintenance_logged": False,
            }

        # 统计各 action 的删除数量，写入维护日志便于审计。
        action_counts: dict[str, int] = {}
        for row in rows:
            action_counts[row["action"]] = action_counts.get(row["action"], 0) + 1

        # 删除范围内事件并重建哈希链。
        conn.execute(
            "DELETE FROM audit_logs WHERE created_at >= ? AND created_at <= ?",
            (normalized_start, normalized_end),
        )
        _backfill_audit_hashes(conn)

        # 记录维护日志。
        conn.execute(
            "INSERT INTO audit_maintenance_logs (id, created_at, actor, operation, target_event_id, "
            "target_action, cutoff, deleted_count, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()), utc_now(), deleted_by, "delete_range", None, None,
                normalized_start + " / " + normalized_end, len(rows),
                _canonical_payload({"start": normalized_start, "end": normalized_end, "action_counts": action_counts}),
            ),
        )
    return {
        "deleted": True,
        "deleted_count": len(rows),
        "start": normalized_start,
        "end": normalized_end,
        "maintenance_logged": True,
        "action_counts": action_counts,
    }


# ---------- 审批、影响预估与受控执行 ----------

def _normalized_sql(sql: str) -> str:
    """把 SQL 规范化为单空格分隔、去掉末尾分号的形式。

    - 折叠连续空白，便于比较和哈希；
    - 去掉末尾分号，保证白名单比较稳定。
    """
    return " ".join(sql.strip().split()).rstrip(";")


def _impact_preview(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    """对受支持的演示变更返回只读、可解释的影响预估。

    - 只有 allow-list 中的 SQL 才能给出精确匹配行数；
    - 其他 SQL 返回 not_allowlisted，提示不可预估；
    - 只读，不执行任何写操作。
    """
    normalized = _normalized_sql(sql).lower()

    # 演示允许的删除操作：删除已关闭告警。
    if normalized == "delete from alerts where status = 'closed'":
        # 最多取 10 条样本，供审批页面展示将要影响哪些行。
        sample = [dict(row) for row in conn.execute(
            "SELECT id, severity, title, status, created_at FROM alerts WHERE status = 'closed' "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]
        return {
            "operation": "DELETE",
            "table": "alerts",
            "matched_rows": conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'closed'").fetchone()[0],
            "sample_rows": sample,
            "estimate_mode": "exact_count",
            "destructive": True,
        }

    # 未在白名单中的 SQL：不可预估，标记为 destructive 以提醒谨慎。
    return {
        "operation": "UNKNOWN",
        "table": "unknown",
        "matched_rows": None,
        "sample_rows": [],
        "estimate_mode": "not_allowlisted",
        "destructive": True,
    }


def create_approval(requester: str, sql: str, reason: str) -> str:
    """创建只读影响预估已完成、但尚未执行的人工审批单。

    - 记录创建时间、请求人、SQL、原因；
    - 计算 impact_preview 并存储；
    - 状态为 pending，risk_level 默认为 high；
    - 过期时间 = 创建时间 + APPROVAL_TTL。

    返回：
    - str：新建审批单的 ID。
    """
    approval_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    with connect() as conn:
        # 先做只读影响预估，供审批人参考。
        preview = _impact_preview(conn, sql)
        conn.execute(
            "INSERT INTO approvals (id, created_at, requester, sql, reason, status, risk_level, impact_preview, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id, created_at.isoformat(), requester, sql, reason,
                "pending", "high", json.dumps(preview, ensure_ascii=False),
                (created_at + APPROVAL_TTL).isoformat(),
            ),
        )
    return approval_id


def _is_approval_expired(row: sqlite3.Row) -> bool:
    """判断审批单是否已过期。

    - 没有 expires_at 视为不过期；
    - 否则比较当前 UTC 时间与 expires_at。
    """
    return bool(row["expires_at"]) and datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc) <= datetime.now(timezone.utc)


def _expire_pending_approvals(conn: sqlite3.Connection) -> None:
    """把已过期的 pending 审批单标记为 expired。

    通常在列表、计数等读取操作前调用，保证状态一致。
    """
    pending = conn.execute(
        "SELECT id, expires_at FROM approvals WHERE status = 'pending' AND expires_at IS NOT NULL"
    ).fetchall()
    expired = [
        row["id"] for row in pending
        if datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc) <= datetime.now(timezone.utc)
    ]
    if expired:
        # 批量更新状态和决定时间，附带自动过期说明。
        conn.executemany(
            "UPDATE approvals SET status = 'expired', decision_comment = '系统自动过期：审批有效期为 30 分钟。', "
            "decided_at = ? WHERE id = ?",
            [(utc_now(), approval_id) for approval_id in expired],
        )


def approve(approval_id: str, decided_by: str, comment: str = "") -> dict[str, Any] | None:
    """批准待审审批单。

    - 不存在返回 None；
    - 非 pending 状态直接返回当前记录；
    - 已过期则标记 expired 并返回；
    - 否则置为 approved，记录决定人和决定时间。
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "pending":
            return dict(row)

        # 过期检查：已过期则改为 expired，不执行批准。
        if _is_approval_expired(row):
            conn.execute(
                "UPDATE approvals SET status = 'expired', decision_comment = '系统自动过期：审批有效期为 30 分钟。', "
                "decided_at = ? WHERE id = ?",
                (utc_now(), approval_id),
            )
            return dict(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())

        # 正常批准。
        conn.execute(
            "UPDATE approvals SET status = 'approved', decision_comment = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (comment.strip(), decided_by, utc_now(), approval_id),
        )
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row)


def reject_approval(approval_id: str, decided_by: str, comment: str = "") -> dict[str, Any] | None:
    """拒绝待审审批单。

    - 不存在返回 None；
    - 非 pending 状态直接返回当前记录；
    - 否则置为 rejected，记录决定人、时间和拒绝理由。
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "pending":
            return dict(row)
        conn.execute(
            "UPDATE approvals SET status = 'rejected', decision_comment = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (comment.strip() or "审批人拒绝执行该变更。", decided_by, utc_now(), approval_id),
        )
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row)


def delete_approval(approval_id: str) -> dict[str, Any] | None:
    """删除已完成的审批记录，保留审计轨迹不受影响。

    - 不存在返回 None；
    - pending 状态不可删除，返回 deletable=False；
    - 其他状态删除并返回 deletable=True。
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] == "pending":
            return {**dict(row), "deletable": False}
        conn.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))
    return {**dict(row), "deletable": True}


def _create_pre_execution_backup(conn: sqlite3.Connection, approval_id: str) -> str:
    """在执行启用的演示写操作前创建可恢复的本地 SQLite 快照。

    - 备份目录 data/backups；
    - 文件名包含审批 ID 前缀和 UTC 时间戳，便于识别；
    - 使用 conn.backup 保证一致性快照。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"approval-{approval_id[:8]}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    backup_path = BACKUP_DIR / name
    with sqlite3.connect(backup_path) as destination:
        conn.backup(destination)
    # 返回相对项目根目录的路径，避免暴露绝对路径。
    return str(backup_path.relative_to(ROOT)).replace("\\", "/")


def execute_approved(approval_id: str, allow_writes: bool) -> dict[str, Any] | None:
    """执行已批准变更；默认安全模式只记录审批结果，不真正写业务数据。

    - 不存在返回 None；
    - 非 approved 状态返回 not_approved；
    - allow_writes=False 时进入 safe_mode，仅记录预估影响；
    - allow_writes=True 时只允许白名单 SQL，执行前先备份；
    - 执行后更新状态为 executed 并记录结果。
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None

        # 只有 approved 状态才允许执行。
        if row["status"] != "approved":
            return {**dict(row), "outcome": "not_approved"}

        sql = _normalized_sql(row["sql"])

        # 安全模式：不写库，仅记录将要影响的估算行数。
        if not allow_writes:
            preview = _impact_preview(conn, sql)
            outcome = {
                "mode": "safe_mode",
                "would_affect_rows": preview["matched_rows"],
                "message": "默认安全模式：审批已记录，未执行任何写库操作。",
            }
            conn.execute(
                "UPDATE approvals SET status = 'approved_safe_mode', executed_at = ?, execution_result = ? WHERE id = ?",
                (utc_now(), json.dumps(outcome, ensure_ascii=False), approval_id),
            )
            updated = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return {**dict(updated), "outcome": "safe_mode"}

        # The Demo deliberately permits exactly one auditable destructive action.
        # Production must replace this with an AST policy and short-lived credentials.
        # 演示只允许一条受控删除 SQL；生产应替换为 AST 策略和短期凭证。
        if sql.lower() != "delete from alerts where status = 'closed'":
            return {**dict(row), "outcome": "not_allowlisted"}

        # 执行前先备份，保证可恢复。
        backup_path = _create_pre_execution_backup(conn, approval_id)
        cursor = conn.execute(sql)
        outcome = {"deleted_rows": cursor.rowcount, "backup_path": backup_path, "mode": "executed"}
        conn.execute(
            "UPDATE approvals SET status = 'executed', executed_at = ?, execution_result = ? WHERE id = ?",
            (utc_now(), json.dumps(outcome), approval_id),
        )
        updated = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return {**dict(updated), "outcome": "executed"}


# ---------- Agent 聊天会话与上下文记忆 ----------

def save_memory(requester: str, role: str, content: str) -> None:
    """保存一条会话记忆，供下一轮 Agent 上下文加载。"""
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversation_memory VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), utc_now(), requester, role, content),
        )


def recent_memory(requester: str, limit: int = 6) -> list[dict[str, str]]:
    """返回指定请求人最近的会话记忆，按时间正序（旧->新）。

    实现：先按时间倒序取 limit 条，再反转，方便按时间顺序拼接上下文。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM conversation_memory WHERE requester = ? ORDER BY created_at DESC LIMIT ?",
            (requester, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def create_chat_conversation(requester: str, title: str = "新对话") -> dict[str, Any]:
    """创建一个新的 Agent 聊天会话。

    - title 去空白并截断到 48 字符，空标题回退为“新对话”；
    - 同时写入 created_at 和 updated_at。
    """
    conversation_id, timestamp = str(uuid4()), utc_now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_conversations (id, requester, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, requester, title.strip()[:48] or "新对话", timestamp, timestamp),
        )
    return {
        "id": conversation_id,
        "requester": requester,
        "title": title.strip()[:48] or "新对话",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def list_chat_conversations(requester: str, limit: int = 100) -> list[dict[str, Any]]:
    """按 updated_at 倒序返回指定请求人的会话列表。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_conversations "
            "WHERE requester = ? ORDER BY updated_at DESC LIMIT ?",
            (requester, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_chat_messages(conversation_id: str, requester: str) -> list[dict[str, str]] | None:
    """返回指定会话的所有消息，按 id 正序。

    - 会话不属于该 requester 时返回 None，避免越权访问；
    - 会话存在时返回消息列表（可能为空）。
    """
    with connect() as conn:
        conversation = conn.execute(
            "SELECT 1 FROM chat_conversations WHERE id = ? AND requester = ?",
            (conversation_id, requester),
        ).fetchone()
        if conversation is None:
            return None
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_chat_message(conversation_id: str, requester: str, role: str, content: str) -> dict[str, str] | None:
    """向属于 requester 的会话追加 user/assistant 消息并更新时间。

    - role 只允许 user 或 assistant；
    - 会话不属于该 requester 时返回 None；
    - 如果会话标题仍是“新对话”且首条是用户消息，用消息内容自动生成标题。
    """
    if role not in {"user", "assistant"}:
        raise ValueError("unsupported chat role")

    timestamp = utc_now()
    with connect() as conn:
        conversation = conn.execute(
            "SELECT title FROM chat_conversations WHERE id = ? AND requester = ?",
            (conversation_id, requester),
        ).fetchone()
        if conversation is None:
            return None

        # 写入消息。
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, timestamp),
        )

        # 首次用户消息时自动生成会话标题。
        title = conversation["title"]
        if role == "user" and title == "新对话":
            title = content.replace("\n", " ").strip()[:32] or "新对话"
            conn.execute(
                "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, timestamp, conversation_id),
            )
        else:
            # 其他情况只更新 updated_at。
            conn.execute(
                "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
                (timestamp, conversation_id),
            )
    return {"role": role, "content": content, "created_at": timestamp}


def delete_chat_conversation(conversation_id: str, requester: str) -> bool:
    """删除指定会话及其所有消息。

    - 会话不属于该 requester 时返回 False；
    - 先删消息再删会话，避免外键残留。
    """
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM chat_conversations WHERE id = ? AND requester = ?",
            (conversation_id, requester),
        ).fetchone()
        if exists is None:
            return False
        conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conversation_id,))
    return True


# ---------- 监控、指标和知识库查询 ----------

def system_metrics() -> dict[str, int | bool]:
    """聚合监控页面所需的系统级统计。

    返回：
    - demo_tables：核心演示表数量（正常应为 4）；
    - knowledge_documents：知识文档数；
    - metric_definitions：指标定义数；
    - imported_metric_definitions：CSV 导入的指标数；
    - metric_imports：导入批次数量；
    - audit_events：审计事件数量；
    - approved_actions：已批准/执行/安全模式审批数量。
    """
    with connect() as conn:
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('assets', 'alerts', 'tickets', 'work_orders')"
        ).fetchone()[0]
        knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        approval_count = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status IN ('approved', 'approved_safe_mode', 'executed')"
        ).fetchone()[0]
        metric_count = conn.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]
        imported_metric_count = conn.execute("SELECT COUNT(*) FROM metric_definitions WHERE source = 'csv'").fetchone()[0]
        import_count = conn.execute("SELECT COUNT(*) FROM metric_imports").fetchone()[0]
    return {
        "demo_tables": table_count,
        "knowledge_documents": knowledge_count,
        "metric_definitions": metric_count,
        "imported_metric_definitions": imported_metric_count,
        "metric_imports": import_count,
        "audit_events": audit_count,
        "approved_actions": approval_count,
    }


def list_metric_definitions(keyword: str = "", category: str = "", limit: int = 60) -> list[dict[str, Any]]:
    """按关键字和分类查询指标定义。

    排序规则：先展示 CSV 导入的真实指标，再展示演示指标，最后按 id 升序。
    """
    clauses, params = [], []
    # 关键字同时匹配 name 和 description。
    if keyword:
        clauses.append("(name LIKE ? OR description LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if category:
        clauses.append("category = ?")
        params.append(category)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, category, unit, asset_scope, description, source FROM metric_definitions"
            + where
            + " ORDER BY CASE WHEN source = 'csv' THEN 0 ELSE 1 END, id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def metric_trend(metric_id: int, points: int = 24) -> dict[str, Any] | None:
    """返回指定指标的最近 points 个样本点。

    - 指标不存在返回 None；
    - 样本按时间倒序取，再反转为正序，便于前端画图。
    """
    with connect() as conn:
        definition = conn.execute(
            "SELECT id, name, category, unit, asset_scope, description, source FROM metric_definitions WHERE id = ?",
            (metric_id,),
        ).fetchone()
        if definition is None:
            return None
        rows = conn.execute(
            "SELECT observed_at, value FROM metric_samples WHERE metric_id = ? ORDER BY observed_at DESC LIMIT ?",
            (metric_id, points),
        ).fetchall()
    return {**dict(definition), "points": list(reversed([dict(row) for row in rows]))}


def list_metric_imports(limit: int = 8) -> list[dict[str, Any]]:
    """按创建时间倒序返回最近的指标导入记录。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, requester, filename, total_rows, sample_count, "
            "created_metrics, updated_metrics FROM metric_imports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def knowledge_document_count(tag: str = "", status: str = "") -> int:
    """返回符合 tag/status 过滤条件的知识文档数量。"""
    with connect() as conn:
        where, params = _knowledge_where(tag, status)
        return conn.execute("SELECT COUNT(*) FROM knowledge_documents" + where, params).fetchone()[0]


def _knowledge_where(tag: str = "", status: str = "") -> tuple[str, list[str]]:
    """构造知识文档查询的 WHERE 子句和参数。

    - tag：模糊匹配 tags 字段；
    - status 可选值：
      * active：未过期或过期时间为空；
      * expired：已过期；
      * expiring：7 天内即将过期。
    使用参数化查询，避免 SQL 注入。
    """
    clauses, params = [], []
    if tag:
        clauses.append("tags LIKE ?"); params.append("%" + tag + "%")
    if status == "active":
        clauses.append("(expires_at IS NULL OR expires_at >= date('now'))")
    if status == "expired":
        clauses.append("expires_at IS NOT NULL AND expires_at < date('now')")
    if status == "expiring":
        clauses.append("expires_at IS NOT NULL AND expires_at >= date('now') AND expires_at <= date('now', '+7 day')")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_knowledge_documents(limit: int = 100, offset: int = 0, tag: str = "", status: str = "") -> list[dict[str, Any]]:
    """分页查询知识文档，并附带过期状态和剩余天数。

    返回字段：
    - status：active / expiring / expired；
    - expires_in_days：过期剩余天数，未设置过期时间为 None。
    """
    with connect() as conn:
        where, params = _knowledge_where(tag, status)
        rows = conn.execute(
            "SELECT id, title, content, tags, version, updated_at, expires_at, "
            "CASE WHEN expires_at IS NOT NULL AND expires_at < date('now') THEN 'expired' "
            "WHEN expires_at IS NOT NULL AND expires_at <= date('now', '+7 day') THEN 'expiring' ELSE 'active' END AS status, "
            "CASE WHEN expires_at IS NULL THEN NULL ELSE CAST(julianday(expires_at) - julianday('now') AS INTEGER) END AS expires_in_days "
            "FROM knowledge_documents" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def add_knowledge_document(title: str, content: str, tags: str, expires_at: str | None = None) -> dict[str, Any]:
    """新增知识文档、初始版本和分块，并返回可用于前端刷新的记录。

    - 版本号从 1 开始；
    - 同时写入 knowledge_versions 作为版本 1；
    - 调用 index_knowledge_document 生成分块。
    """
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_documents (title, content, tags, version, updated_at, expires_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (title, content, tags, now, expires_at or None),
        )
        row = conn.execute(
            "SELECT id, title, content, tags, version, updated_at, expires_at, 'active' AS status "
            "FROM knowledge_documents WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        conn.execute(
            "INSERT INTO knowledge_versions (document_id, version, title, content, tags, created_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (cursor.lastrowid, title, content, tags, now),
        )
    document = dict(row)
    # 生成知识分块，便于后续检索和向量索引。
    index_knowledge_document(document["id"])
    return document


def update_knowledge_document(document_id: int, title: str, content: str, tags: str, expires_at: str | None = None) -> dict[str, Any] | None:
    """更新知识文档并递增版本；旧版本保存在 knowledge_versions 供对比和回滚。

    - 文档不存在返回 None；
    - 新版本号 = 当前版本 + 1；
    - tags 为空时回退为“未分类”；
    - 更新后重新生成分块。
    """
    now = utc_now()
    with connect() as conn:
        current = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        if current is None:
            return None
        next_version = int(current["version"]) + 1

        # 更新主表。
        conn.execute(
            "UPDATE knowledge_documents SET title = ?, content = ?, tags = ?, version = ?, updated_at = ?, expires_at = ? WHERE id = ?",
            (title.strip(), content, tags.strip() or "未分类", next_version, now, expires_at or None, document_id),
        )
        # 追加历史版本。
        conn.execute(
            "INSERT INTO knowledge_versions (document_id, version, title, content, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, next_version, title.strip(), content, tags.strip() or "未分类", now),
        )
        row = conn.execute(
            "SELECT id, title, content, tags, version, updated_at, expires_at, 'active' AS status "
            "FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    document = dict(row)
    # 重新生成分块，保持检索与文档内容一致。
    index_knowledge_document(document_id)
    return document


def list_knowledge_versions(document_id: int) -> list[dict[str, Any]]:
    """返回指定文档的所有历史版本，按版本号倒序。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, document_id, version, title, content, tags, created_at FROM knowledge_versions "
            "WHERE document_id = ? ORDER BY version DESC",
            (document_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def rollback_knowledge_document(document_id: int, version: int) -> dict[str, Any] | None:
    """将文档恢复到指定历史版本，并以新版本记录这次回滚操作。

    - 指定版本不存在返回 None；
    - 保留当前 expires_at；
    - 通过 update_knowledge_document 递增版本，形成新的历史记录。
    """
    with connect() as conn:
        selected = conn.execute(
            "SELECT title, content, tags FROM knowledge_versions WHERE document_id = ? AND version = ?",
            (document_id, version),
        ).fetchone()
        current = conn.execute("SELECT expires_at FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
    if selected is None or current is None:
        return None
    return update_knowledge_document(document_id, selected["title"], selected["content"], selected["tags"], current["expires_at"])


def delete_knowledge_document(document_id: int) -> bool:
    """删除知识文档及其分块和历史版本，返回是否真的删除了主记录。

    - 先删分块和版本，避免外键残留；
    - 返回主表删除是否成功。
    """
    with connect() as conn:
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM knowledge_versions WHERE document_id = ?", (document_id,))
        cursor = conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
    return cursor.rowcount == 1


def knowledge_tags() -> list[str]:
    """收集所有知识文档中出现过的标签，去重并排序。

    - 同时兼容英文逗号和中文逗号；
    - 返回排序后的标签列表。
    """
    return sorted({
        tag.strip()
        for row in list_knowledge_documents(limit=1000)
        for tag in row["tags"].replace("，", ",").split(",")
        if tag.strip()
    })


def document_chunks(document_id: int) -> list[dict[str, Any]]:
    """返回指定文档的全部分块，按 chunk_index 正序。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT chunk_index, content, token_count FROM knowledge_chunks WHERE document_id = ? ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _split_knowledge(content: str, size: int = 180, overlap: int = 30) -> list[str]:
    """将知识文档内容切分为带重叠的分块。

    参数：
    - content：原始文本；
    - size：目标分块字符长度，默认 180；
    - overlap：相邻分块重叠字符数，默认 30，避免语义在边界处丢失。

    策略：
    - 先做空白归一化；
    - 优先在句号、分号、问号、感叹号等句子边界处断开；
    - 无法找到边界时按 size 直接切分。
    """
    normalized = " ".join(content.split())
    if len(normalized) <= size:
        return [normalized] if normalized else []

    chunks, start = [], 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        # Prefer ending at a sentence boundary when practical.
        # 在分块中后段寻找最近的句子边界，让切分更自然。
        boundary = max(normalized.rfind(mark, start + size // 2, end) for mark in "。；；.!?")
        if boundary > start:
            end = boundary + 1
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        # 下一块从 end - overlap 开始，形成重叠。
        start = max(start + 1, end - overlap)
    return chunks


def index_knowledge_document(document_id: int) -> int:
    """为指定文档生成知识分块，替换旧分块。

    - 文档不存在返回 0；
    - 先删除旧分块，再写入新分块；
    - 返回新分块数量。
    """
    with connect() as conn:
        document = conn.execute("SELECT content FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        if document is None:
            return 0
        chunks = _split_knowledge(document["content"])
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        conn.executemany(
            "INSERT INTO knowledge_chunks (document_id, chunk_index, content, token_count) VALUES (?, ?, ?, ?)",
            # 知识分块的 token_count 仅用于排序/诊断，采用轻量字符估算，不作为模型请求预算。
            [(document_id, index, chunk, max(1, len(chunk) // 3)) for index, chunk in enumerate(chunks)],
        )
    return len(chunks)


def rebuild_knowledge_index() -> int:
    """从 SQLite 文档事实表重新生成全部知识分块，保证索引可丢失、可重建。

    返回：
    - int：重建的分块总数。
    """
    with connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM knowledge_documents").fetchall()]
    return sum(index_knowledge_document(document_id) for document_id in ids)


def knowledge_chunks() -> list[dict[str, Any]]:
    """返回所有未过期知识文档的分块，供向量索引重建使用。

    - 过滤过期文档：expires_at 为空或晚于今天；
    - 关联文档标题和标签，便于构造嵌入文本。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.document_id, c.chunk_index, c.content, c.token_count, d.title, d.tags "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id = d.id "
            "WHERE d.expires_at IS NULL OR d.expires_at >= date('now') "
            "ORDER BY c.document_id, c.chunk_index"
        ).fetchall()
    return [dict(row) for row in rows]


def save_knowledge_evaluation_feedback(evaluation_id: str, question: str, score: int, comment: str, requester: str) -> dict[str, Any]:
    """保存一条 RAG 人工评分反馈。

    - score 由表约束为 1~5；
    - 返回保存后的记录，便于前端展示。
    """
    created_at = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_eval_feedback (evaluation_id, question, score, comment, requester, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (evaluation_id, question.strip(), score, comment.strip(), requester, created_at),
        )
    return {
        "id": cursor.lastrowid,
        "evaluation_id": evaluation_id,
        "question": question.strip(),
        "score": score,
        "comment": comment.strip(),
        "requester": requester,
        "created_at": created_at,
    }


def knowledge_evaluation_feedback_summary(evaluation_id: str) -> dict[str, Any]:
    """汇总某次评测的反馈：评分数量与平均分。

    - 无反馈时 average_score 返回 None；
    - 有反馈时保留两位小数。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count, AVG(score) AS average_score FROM knowledge_eval_feedback WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
    return {
        "count": row["count"],
        "average_score": round(row["average_score"], 2) if row["average_score"] is not None else None,
    }


# 审批状态机：pending 可批准/拒绝；approved 可执行；过期后只读展示。
APPROVAL_STATUSES = {"pending", "approved", "approved_safe_mode", "executed", "rejected", "expired"}


# ---------- 页面列表、分页统计和数据浏览器 ----------

def _approval_status_clause(status: str) -> tuple[str, tuple[str, ...]]:
    """为审批列表构造固定、参数化的状态过滤子句。

    - 空字符串表示不过滤；
    - 状态必须在 APPROVAL_STATUSES 中，否则抛错；
    - 使用参数化占位符，避免 SQL 注入。
    """
    if not status:
        return "", ()
    if status not in APPROVAL_STATUSES:
        raise ValueError(f"Unsupported approval status: {status}")
    return " WHERE status = ?", (status,)


def list_approvals(limit: int = 100, offset: int = 0, status: str = "") -> list[dict[str, Any]]:
    """分页查询审批单，并解析 JSON 字段。

    - 调用前先过期处理，保证 pending 状态准确；
    - impact_preview 和 execution_result 从 JSON 字符串解析为字典。
    """
    with connect() as conn:
        _expire_pending_approvals(conn)
        where_clause, parameters = _approval_status_clause(status)
        rows = conn.execute(
            "SELECT id, created_at, requester, sql, reason, status, risk_level, impact_preview, decision_comment, "
            "decided_by, decided_at, expires_at, executed_at, execution_result "
            f"FROM approvals{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        # 解析 JSON 字段，方便前端直接使用。
        if item["impact_preview"]:
            item["impact_preview"] = json.loads(item["impact_preview"])
        if item["execution_result"]:
            item["execution_result"] = json.loads(item["execution_result"])
        result.append(item)
    return result


def monitoring_overview() -> dict[str, Any]:
    """聚合监控首页所需的健康探针、告警趋势和关键指标摘要。

    返回：
    - asset_states：按状态分组的资产数量；
    - alert_severity：未关闭告警按等级分组；
    - latest_alerts：最近 10 条未关闭告警，关联资产名称和区域；
    - open_tickets：未关闭工单数量；
    - metric_series：5 类关键指标的最近 24 个样本点；
    - alert_trend：最近 7 天的告警数量趋势；
    - health_checks：API、SQLite、知识库索引、本地模型的健康状态；
    - sample_source：数据来源说明。
    """
    with connect() as conn:
        # 资产状态分布。
        asset_states = [dict(row) for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM assets GROUP BY status ORDER BY count DESC"
        ).fetchall()]

        # 未关闭告警按等级分布。
        alert_severity = [dict(row) for row in conn.execute(
            "SELECT severity, COUNT(*) AS count FROM alerts WHERE status != 'closed' GROUP BY severity ORDER BY severity"
        ).fetchall()]

        # 最近 10 条未关闭告警，关联资产名称和区域。
        latest_alerts = [dict(row) for row in conn.execute(
            "SELECT a.id, a.severity, a.title, a.status, a.created_at, s.name AS asset_name, s.region "
            "FROM alerts a JOIN assets s ON a.asset_id = s.id WHERE a.status != 'closed' "
            "ORDER BY a.created_at DESC LIMIT 10"
        ).fetchall()]

        # 未关闭工单数量。
        open_tickets = conn.execute("SELECT COUNT(*) FROM tickets WHERE status != 'closed'").fetchone()[0]

        # 关键指标：优先选择 CSV 导入的真实指标，否则使用演示指标。
        metric_patterns = [
            ("CPU 使用率", "主机"), ("内存使用率", "主机"), ("磁盘使用率", "主机"),
            ("网络入站流量", "网络"), ("请求延迟 P95", "应用"),
        ]
        metric_series = []
        for name, category in metric_patterns:
            definition = conn.execute(
                "SELECT id, name, unit, asset_scope, source FROM metric_definitions "
                "WHERE name LIKE ? AND category = ? ORDER BY CASE WHEN source = 'csv' THEN 0 ELSE 1 END, id LIMIT 1",
                (name + "%", category),
            ).fetchone()
            if definition is None:
                continue
            points = conn.execute(
                "SELECT observed_at, value FROM metric_samples WHERE metric_id = ? ORDER BY observed_at DESC LIMIT 24",
                (definition["id"],),
            ).fetchall()
            metric_series.append({**dict(definition), "points": list(reversed([dict(point) for point in points]))})

        # 最近 7 天告警趋势。
        alert_dates = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count FROM alerts GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()

    # 反转为时间正序，便于前端画图。
    alert_trend = list(reversed([dict(row) for row in alert_dates]))

    # 健康探针：API、SQLite、知识库索引、本地模型。
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
            knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
            chunk_count = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        health_checks = [
            {"name": "API 服务", "status": "healthy", "detail": "当前进程正常响应"},
            {"name": "SQLite 数据库", "status": "healthy", "detail": "连接与只读探针正常"},
            {"name": "知识库索引", "status": "healthy" if chunk_count else "degraded", "detail": f"{knowledge_count} 份文档 · {chunk_count} 个分块"},
            {"name": "本地 LLM", "status": "healthy" if settings.chat_enabled else "degraded", "detail": "模型配置已加载" if settings.chat_enabled else "未配置，使用离线兜底"},
        ]
    except sqlite3.Error as exc:
        # SQLite 探针失败时，只报告数据库 down，避免其他健康项误导。
        health_checks = [{"name": "SQLite 数据库", "status": "down", "detail": f"探针失败：{exc}"}]

    return {
        "asset_states": asset_states,
        "alert_severity": alert_severity,
        "latest_alerts": latest_alerts,
        "open_tickets": open_tickets,
        "metric_series": metric_series,
        "alert_trend": alert_trend,
        "health_checks": health_checks,
        "sample_source": "SQLite metric_samples / alerts",
    }


def list_audit(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """按创建时间倒序返回审计日志，并解析 payload。

    - payload 存储为规范 JSON 字符串，返回前解析为字典；
    - 返回结果包含 prev_hash 和 entry_hash，便于前端展示哈希链。
    """
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def audit_count() -> int:
    """返回审计日志总数。"""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]


def approval_count(status: str = "") -> int:
    """返回审批单数量，可按状态过滤。

    - 调用前先过期处理，保证 pending 计数准确。
    """
    with connect() as conn:
        _expire_pending_approvals(conn)
        where_clause, parameters = _approval_status_clause(status)
        return conn.execute(f"SELECT COUNT(*) FROM approvals{where_clause}", parameters).fetchone()[0]


def approval_status_counts() -> dict[str, int]:
    """返回各审批状态的计数，缺失状态补 0。

    保证前端状态徽章不会因为某状态无记录而缺失。
    """
    with connect() as conn:
        _expire_pending_approvals(conn)
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM approvals GROUP BY status").fetchall()
    counts = {status: 0 for status in APPROVAL_STATUSES}
    counts.update({row["status"]: row["count"] for row in rows})
    return counts


def data_catalog() -> list[dict[str, Any]]:
    """返回数据浏览器可访问的表目录。

    只允许 EXPLORER_TABLES 白名单中的表；每项包含表名、中文名、行数和列数。
    """
    with connect() as conn:
        result = []
        for name, label in EXPLORER_TABLES.items():
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            columns = conn.execute(f"PRAGMA table_info({name})").fetchall()
            result.append({
                "name": name,
                "label": label,
                "row_count": count,
                "column_count": len(columns),
            })
    return result


def table_snapshot(table_name: str, limit: int = 30, offset: int = 0) -> dict[str, Any] | None:
    """返回指定表的 schema 和有界样本数据。

    - 表名必须来自白名单，否则返回 None；
    - schema 包含列名、类型、是否必填、是否主键；
    - rows 按 rowid 倒序分页，便于查看最新数据。
    """
    if table_name not in EXPLORER_TABLES:
        return None
    with connect() as conn:
        schema = [
            {
                "name": row["name"],
                "type": row["type"],
                "required": bool(row["notnull"]),
                "primary_key": bool(row["pk"]),
            }
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        total = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        rows = [dict(row) for row in conn.execute(
            f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()]
    return {
        "name": table_name,
        "label": EXPLORER_TABLES[table_name],
        "schema": schema,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }


def audit_integrity(limit: int | None = None) -> dict[str, Any]:
    """校验审计哈希链的连续性，并返回断点、检查数量和整体状态。

    - limit 为空时校验全部；
    - 校验时会从锚点（前一条的 entry_hash）开始，保证局部校验也正确；
    - 发现断点立即返回 valid=False，并给出断点事件 ID；
    - 全部通过则返回 valid=True 和最新哈希。
    """
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        # checked 限制在 [1, total]；total 为 0 时 checked 为 0。
        checked = min(max(limit or total, 1), total) if total else 0
        offset = total - checked

        # 取锚点：待校验窗口前一条的 entry_hash，作为起始 previous。
        anchor = conn.execute(
            "SELECT entry_hash FROM audit_logs ORDER BY rowid LIMIT 1 OFFSET ?", (offset - 1,)
        ).fetchone() if offset else None

        # 取出待校验窗口的所有事件。
        rows = conn.execute(
            "SELECT id, created_at, requester, action, payload, prev_hash, entry_hash "
            "FROM audit_logs ORDER BY rowid LIMIT ? OFFSET ?",
            (checked, offset),
        ).fetchall()

    previous = anchor["entry_hash"] if anchor else ""
    for index, row in enumerate(rows, start=1):
        # 重算期望哈希，与存储值比较。
        payload = _canonical_payload(json.loads(row["payload"]))
        expected = _audit_digest(previous, row["id"], row["created_at"], row["requester"], row["action"], payload)
        # prev_hash 或 entry_hash 不一致，说明链被破坏。
        if row["prev_hash"] != previous or row["entry_hash"] != expected:
            return {
                "valid": False,
                "checked_events": index - 1,
                "total_events": total,
                "scope": "full" if checked == total else "recent",
                "broken_at": row["id"],
            }
        previous = expected

    return {
        "valid": True,
        "checked_events": len(rows),
        "total_events": total,
        "scope": "full" if checked == total else "recent",
        "latest_hash": previous or None,
    }
