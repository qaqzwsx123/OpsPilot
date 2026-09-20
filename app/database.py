from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "agent_demo.db"
BACKUP_DIR = DATA_DIR / "backups"
APPROVAL_TTL = timedelta(minutes=30)
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
    "conversation_memory": "会话记忆",
    "chat_conversations": "Agent 聊天会话",
    "chat_messages": "Agent 聊天消息",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    with connect() as conn:
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
        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_logs)").fetchall()}
        if "prev_hash" not in audit_columns:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN prev_hash TEXT")
        if "entry_hash" not in audit_columns:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN entry_hash TEXT")
        metric_columns = {row["name"] for row in conn.execute("PRAGMA table_info(metric_definitions)").fetchall()}
        if "source" not in metric_columns:
            conn.execute("ALTER TABLE metric_definitions ADD COLUMN source TEXT NOT NULL DEFAULT 'demo'")
        knowledge_columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()}
        if "version" not in knowledge_columns:
            conn.execute("ALTER TABLE knowledge_documents ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "updated_at" not in knowledge_columns:
            conn.execute("ALTER TABLE knowledge_documents ADD COLUMN updated_at TEXT")
        if "expires_at" not in knowledge_columns:
            conn.execute("ALTER TABLE knowledge_documents ADD COLUMN expires_at TEXT")
        conn.execute("UPDATE knowledge_documents SET updated_at = COALESCE(updated_at, ?)", (utc_now(),))
        conn.execute("INSERT INTO knowledge_versions (document_id, version, title, content, tags, created_at) SELECT id, version, title, content, tags, updated_at FROM knowledge_documents d WHERE NOT EXISTS (SELECT 1 FROM knowledge_versions v WHERE v.document_id = d.id AND v.version = d.version)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_samples_metric_time ON metric_samples(metric_id, observed_at)")
        _backfill_audit_hashes(conn)


def seed_demo_data() -> None:
    initialize()
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        if count:
            seed_knowledge_library()
            return
        conn.executemany(
            "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "edge-gateway-sh-01", "华东", "online", "li", "2026-09-18T01:00:00Z"),
                (2, "edge-gateway-sh-02", "华东", "offline", "wang", "2026-09-18T01:20:00Z"),
                (3, "robot-gz-01", "华南", "maintenance", "zhao", "2026-09-17T18:20:00Z"),
                (4, "edge-gateway-bj-01", "华北", "offline", "chen", "2026-09-18T00:50:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (101, 2, "P1", "华东网关离线", "open", "2026-09-18T01:15:00Z"),
                (102, 4, "P1", "华北网关离线", "acknowledged", "2026-09-18T00:45:00Z"),
                (103, 3, "P2", "机器人电池低", "open", "2026-09-17T18:10:00Z"),
                (104, 1, "P3", "网关磁盘使用率高", "closed", "2026-09-16T09:10:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?)",
            [
                (201, "high", "华东网关离线排障", "open", "li", "2026-09-18T01:16:00Z"),
                (202, "medium", "机器人电池更换", "in_progress", "zhao", "2026-09-17T18:30:00Z"),
                (203, "high", "华北网络链路检查", "closed", "chen", "2026-09-17T15:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO work_orders VALUES (?, ?, ?, ?, ?)",
            [(301, 2, "dispatch_engineer", "pending", "2026-09-18T01:17:00Z")],
        )
        conn.executemany(
            "INSERT INTO knowledge_documents (id, title, content, tags) VALUES (?, ?, ?, ?)",
            [
                (1, "P1 告警处置 SOP", "P1 告警需要在 5 分钟内确认。先确认告警范围，再检查设备网络与心跳；无法恢复时创建高优工单并升级值班负责人。", "P1,告警,SOP,升级"),
                (2, "设备离线排障手册", "设备离线时依次检查供电、网络连通性、最近心跳和网关日志。若是单设备故障，优先安排现场巡检；若同区域批量离线，按网络故障升级。", "离线,设备,网络,排障"),
                (3, "工单优先级规范", "高优工单要求在两小时内响应。关闭工单前必须记录根因、处理动作与验证结果。", "工单,优先级,规范"),
            ],
        )
    seed_knowledge_library()


def seed_knowledge_library() -> None:
    """Add the operational baseline library without replacing user-authored documents."""
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
            conn.execute(
                "INSERT INTO knowledge_documents (title, content, tags) SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM knowledge_documents WHERE title = ?)",
                (title, content, tags, title),
            )


def seed_metric_demo_data() -> None:
    """Creates a deterministic local metric catalog and samples for UI demonstrations."""
    initialize()
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]:
            return
        families = [
            ("CPU 使用率", "主机", "%"), ("内存使用率", "主机", "%"), ("磁盘使用率", "主机", "%"),
            ("网络入站流量", "网络", "MB/s"), ("网络出站流量", "网络", "MB/s"), ("请求延迟 P95", "应用", "ms"),
            ("请求成功率", "应用", "%"), ("消息积压量", "消息队列", "条"), ("数据库连接池使用率", "数据库", "%"),
            ("任务执行耗时", "作业", "s"),
        ]
        definitions = []
        samples = []
        start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
        for metric_id in range(1, 701):
            label, category, unit = families[(metric_id - 1) % len(families)]
            scope = f"asset-{(metric_id - 1) % 4 + 1}"
            definitions.append((metric_id, f"{label} · {scope} · #{metric_id:03d}", category, unit, scope, f"{label} 的本地演示时序指标"))
            baseline = 35 + (metric_id * 7) % 45
            if unit == "ms":
                baseline *= 3
            elif unit == "MB/s":
                baseline /= 2
            elif unit == "条":
                baseline *= 12
            elif unit == "s":
                baseline /= 4
            for point in range(24):
                wave = ((point * 11 + metric_id * 3) % 17) - 8
                samples.append((metric_id, (start + timedelta(hours=point)).isoformat(), round(max(0.1, baseline + wave), 2)))
        conn.executemany(
            "INSERT INTO metric_definitions (id, name, category, unit, asset_scope, description) VALUES (?, ?, ?, ?, ?, ?)",
            definitions,
        )
        conn.executemany(
            "INSERT INTO metric_samples (metric_id, observed_at, value) VALUES (?, ?, ?)",
            samples,
        )


CSV_HEADER_ALIASES = {
    "metric_name": ("metric_name", "name", "指标名称", "指标名"),
    "category": ("category", "分类"),
    "unit": ("unit", "单位"),
    "asset_scope": ("asset_scope", "scope", "资源范围", "资产范围", "资源", "资产"),
    "observed_at": ("observed_at", "timestamp", "time", "采集时间", "时间", "时间戳"),
    "value": ("value", "指标值", "数值", "值"),
    "description": ("description", "描述", "说明"),
}
MAX_METRIC_IMPORT_ROWS = 50_000


def metric_csv_template() -> str:
    return (
        "metric_name,category,unit,asset_scope,observed_at,value,description\n"
        "CPU 使用率,主机,%,edge-gateway-sh-01,2026-09-19T09:00:00+08:00,68.5,生产网关 CPU 使用率\n"
        "CPU 使用率,主机,%,edge-gateway-sh-01,2026-09-19T10:00:00+08:00,72.1,生产网关 CPU 使用率\n"
    )


def _resolve_csv_headers(fieldnames: list[str] | None) -> dict[str, str]:
    normalized = {str(name).strip().lower(): str(name) for name in fieldnames or [] if name and str(name).strip()}
    resolved: dict[str, str] = {}
    for canonical, aliases in CSV_HEADER_ALIASES.items():
        for alias in aliases:
            matched = normalized.get(alias.lower())
            if matched:
                resolved[canonical] = matched
                break
    required = ("metric_name", "category", "unit", "asset_scope", "observed_at", "value")
    missing = [name for name in required if name not in resolved]
    if missing:
        raise ValueError("CSV 缺少必填列：" + "、".join(missing))
    return resolved


def _parse_observed_at(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError("采集时间必须是 ISO 8601 格式，例如 2026-09-19T09:00:00+08:00") from exc


def import_metric_csv(content: str, filename: str, requester: str) -> dict[str, int | str]:
    """Validate and upsert real metric CSV rows without overwriting unrelated metrics."""
    reader = csv.DictReader(io.StringIO(content))
    headers = _resolve_csv_headers(reader.fieldnames)
    parsed_rows: list[tuple[str, str, str, str, str, float, str]] = []
    for line_number, row in enumerate(reader, start=2):
        if len(parsed_rows) >= MAX_METRIC_IMPORT_ROWS:
            raise ValueError(f"CSV 最多允许 {MAX_METRIC_IMPORT_ROWS} 条数据行")
        values = {key: str(row.get(column) or "").strip() for key, column in headers.items()}
        if not any(values.values()):
            continue
        required = ("metric_name", "category", "unit", "asset_scope", "observed_at", "value")
        if any(not values[name] for name in required):
            raise ValueError(f"第 {line_number} 行存在空的必填字段")
        if any(len(values[name]) > 160 for name in ("metric_name", "category", "unit", "asset_scope")):
            raise ValueError(f"第 {line_number} 行的指标元数据超过长度限制")
        try:
            numeric_value = float(values["value"])
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行的 value 必须是数值") from exc
        if not math.isfinite(numeric_value):
            raise ValueError(f"第 {line_number} 行的 value 必须是有限数值")
        observed_at = _parse_observed_at(values["observed_at"])
        parsed_rows.append((values["metric_name"], values["category"], values["unit"], values["asset_scope"], observed_at, numeric_value, values.get("description", "")))
    if not parsed_rows:
        raise ValueError("CSV 没有可导入的数据行")

    created_metrics = 0
    metric_ids: dict[tuple[str, str, str, str], int] = {}
    with connect() as conn:
        for name, category, unit, scope, _, _, description in parsed_rows:
            key = (name, category, unit, scope)
            if key in metric_ids:
                continue
            existing = conn.execute(
                "SELECT id FROM metric_definitions WHERE name = ? AND category = ? AND unit = ? AND asset_scope = ?",
                key,
            ).fetchone()
            if existing is None:
                cursor = conn.execute(
                    "INSERT INTO metric_definitions (name, category, unit, asset_scope, description, source) VALUES (?, ?, ?, ?, ?, 'csv')",
                    (name, category, unit, scope, description or "从真实 CSV 导入的时序指标"),
                )
                metric_ids[key] = int(cursor.lastrowid)
                created_metrics += 1
            else:
                metric_ids[key] = int(existing["id"])
        conn.executemany(
            "INSERT INTO metric_samples (metric_id, observed_at, value) VALUES (?, ?, ?) "
            "ON CONFLICT(metric_id, observed_at) DO UPDATE SET value = excluded.value",
            [(metric_ids[(name, category, unit, scope)], observed_at, value) for name, category, unit, scope, observed_at, value, _ in parsed_rows],
        )
        import_id = str(uuid4())
        conn.execute(
            "INSERT INTO metric_imports (id, created_at, requester, filename, total_rows, sample_count, created_metrics, updated_metrics) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (import_id, utc_now(), requester, Path(filename).name[:180] or "metrics.csv", len(parsed_rows), len(parsed_rows), created_metrics, len(metric_ids) - created_metrics),
        )
    return {"import_id": import_id, "total_rows": len(parsed_rows), "sample_count": len(parsed_rows), "created_metrics": created_metrics, "updated_metrics": len(metric_ids) - created_metrics}


EVALUATION_STATUSES = {"completed", "answered_by_rag", "approval_required", "blocked"}


def list_evaluation_cases() -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, requester, name, question, expected_status FROM evaluation_cases ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def add_evaluation_case(name: str, question: str, expected_status: str, requester: str) -> dict[str, str]:
    if expected_status not in EVALUATION_STATUSES:
        raise ValueError("预期结果必须是 completed、answered_by_rag、approval_required 或 blocked")
    case = {"id": str(uuid4()), "created_at": utc_now(), "requester": requester, "name": name.strip(), "question": question.strip(), "expected_status": expected_status}
    if not (2 <= len(case["name"]) <= 80 and 2 <= len(case["question"]) <= 500):
        raise ValueError("用例名称需为 2-80 个字符，问题需为 2-500 个字符")
    with connect() as conn:
        conn.execute(
            "INSERT INTO evaluation_cases (id, created_at, requester, name, question, expected_status) VALUES (?, ?, ?, ?, ?, ?)",
            (case["id"], case["created_at"], case["requester"], case["name"], case["question"], case["expected_status"]),
        )
    return case


def delete_evaluation_case(case_id: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM evaluation_cases WHERE id = ?", (case_id,)).rowcount > 0


def execute_readonly(sql: str) -> list[dict[str, Any]]:
    if settings.mysql_enabled:
        from app.mysql_adapter import execute_readonly as execute_mysql_readonly

        return execute_mysql_readonly(sql)
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit_digest(previous: str, event_id: str, created_at: str, requester: str, action: str, payload: str) -> str:
    value = "|".join((previous, event_id, created_at, requester, action, payload))
    return sha256(value.encode("utf-8")).hexdigest()


def _backfill_audit_hashes(conn: sqlite3.Connection) -> None:
    previous = ""
    rows = conn.execute("SELECT rowid, id, created_at, requester, action, payload, entry_hash FROM audit_logs ORDER BY rowid").fetchall()
    for row in rows:
        payload = _canonical_payload(json.loads(row["payload"]))
        digest = _audit_digest(previous, row["id"], row["created_at"], row["requester"], row["action"], payload)
        if row["entry_hash"] != digest:
            conn.execute("UPDATE audit_logs SET prev_hash = ?, entry_hash = ? WHERE rowid = ?", (previous, digest, row["rowid"]))
        previous = digest


def write_audit(requester: str, action: str, payload: dict[str, Any]) -> None:
    event_id, created_at = str(uuid4()), utc_now()
    canonical_payload = _canonical_payload(payload)
    with connect() as conn:
        previous_row = conn.execute("SELECT entry_hash FROM audit_logs WHERE entry_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        previous = previous_row["entry_hash"] if previous_row else ""
        digest = _audit_digest(previous, event_id, created_at, requester, action, canonical_payload)
        conn.execute(
            "INSERT INTO audit_logs (id, created_at, requester, action, payload, prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, created_at, requester, action, canonical_payload, previous, digest),
        )


def delete_audit_event(event_id: str, deleted_by: str) -> dict[str, Any] | None:
    """Delete one audit row, rebuild the remaining chain, and retain a deletion event."""
    with connect() as conn:
        row = conn.execute("SELECT id, created_at, requester, action FROM audit_logs WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM audit_logs WHERE id = ?", (event_id,))
        _backfill_audit_hashes(conn)

        deletion_id, deletion_time = str(uuid4()), utc_now()
        deletion_payload = {
            "deleted_event_id": event_id,
            "deleted_action": row["action"],
            "deleted_requester": row["requester"],
            "deleted_created_at": row["created_at"],
            "deleted_by": deleted_by,
        }
        canonical_payload = _canonical_payload(deletion_payload)
        previous_row = conn.execute("SELECT entry_hash FROM audit_logs WHERE entry_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        previous = previous_row["entry_hash"] if previous_row else ""
        digest = _audit_digest(previous, deletion_id, deletion_time, deleted_by, "audit_deleted", canonical_payload)
        conn.execute(
            "INSERT INTO audit_logs (id, created_at, requester, action, payload, prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (deletion_id, deletion_time, deleted_by, "audit_deleted", canonical_payload, previous, digest),
        )
        return {"deleted": True, "deleted_event_id": event_id, "deleted_action": row["action"], "audit_event_id": deletion_id}


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.strip().split()).rstrip(";")


def _impact_preview(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    """Return an explainable, read-only impact estimate for supported demo changes."""
    normalized = _normalized_sql(sql).lower()
    if normalized == "delete from alerts where status = 'closed'":
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
    return {
        "operation": "UNKNOWN",
        "table": "unknown",
        "matched_rows": None,
        "sample_rows": [],
        "estimate_mode": "not_allowlisted",
        "destructive": True,
    }


def create_approval(requester: str, sql: str, reason: str) -> str:
    approval_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    with connect() as conn:
        preview = _impact_preview(conn, sql)
        conn.execute(
            "INSERT INTO approvals (id, created_at, requester, sql, reason, status, risk_level, impact_preview, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval_id, created_at.isoformat(), requester, sql, reason, "pending", "high", json.dumps(preview, ensure_ascii=False), (created_at + APPROVAL_TTL).isoformat()),
        )
    return approval_id


def _is_approval_expired(row: sqlite3.Row) -> bool:
    return bool(row["expires_at"]) and datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc) <= datetime.now(timezone.utc)


def _expire_pending_approvals(conn: sqlite3.Connection) -> None:
    pending = conn.execute("SELECT id, expires_at FROM approvals WHERE status = 'pending' AND expires_at IS NOT NULL").fetchall()
    expired = [row["id"] for row in pending if datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc) <= datetime.now(timezone.utc)]
    if expired:
        conn.executemany(
            "UPDATE approvals SET status = 'expired', decision_comment = '系统自动过期：审批有效期为 30 分钟。', decided_at = ? WHERE id = ?",
            [(utc_now(), approval_id) for approval_id in expired],
        )


def approve(approval_id: str, decided_by: str, comment: str = "") -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "pending":
            return dict(row)
        if _is_approval_expired(row):
            conn.execute(
                "UPDATE approvals SET status = 'expired', decision_comment = '系统自动过期：审批有效期为 30 分钟。', decided_at = ? WHERE id = ?",
                (utc_now(), approval_id),
            )
            return dict(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
        conn.execute(
            "UPDATE approvals SET status = 'approved', decision_comment = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (comment.strip(), decided_by, utc_now(), approval_id),
        )
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row)


def reject_approval(approval_id: str, decided_by: str, comment: str = "") -> dict[str, Any] | None:
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
    """Delete a completed approval record while leaving its audit trail intact."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] == "pending":
            return {**dict(row), "deletable": False}
        conn.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))
    return {**dict(row), "deletable": True}


def _create_pre_execution_backup(conn: sqlite3.Connection, approval_id: str) -> str:
    """Make a restorable local SQLite snapshot immediately before an enabled demo write."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"approval-{approval_id[:8]}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    backup_path = BACKUP_DIR / name
    with sqlite3.connect(backup_path) as destination:
        conn.backup(destination)
    return str(backup_path.relative_to(ROOT)).replace("\\", "/")


def execute_approved(approval_id: str, allow_writes: bool) -> dict[str, Any] | None:
    """Executes only the explicitly allow-listed local Demo operation after approval."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "approved":
            return {**dict(row), "outcome": "not_approved"}
        sql = _normalized_sql(row["sql"])
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
        if sql.lower() != "delete from alerts where status = 'closed'":
            return {**dict(row), "outcome": "not_allowlisted"}
        backup_path = _create_pre_execution_backup(conn, approval_id)
        cursor = conn.execute(sql)
        outcome = {"deleted_rows": cursor.rowcount, "backup_path": backup_path, "mode": "executed"}
        conn.execute(
            "UPDATE approvals SET status = 'executed', executed_at = ?, execution_result = ? WHERE id = ?",
            (utc_now(), json.dumps(outcome), approval_id),
        )
        updated = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return {**dict(updated), "outcome": "executed"}


def save_memory(requester: str, role: str, content: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversation_memory VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), utc_now(), requester, role, content),
        )


def recent_memory(requester: str, limit: int = 6) -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM conversation_memory WHERE requester = ? ORDER BY created_at DESC LIMIT ?",
            (requester, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def create_chat_conversation(requester: str, title: str = "新对话") -> dict[str, Any]:
    conversation_id, timestamp = str(uuid4()), utc_now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_conversations (id, requester, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, requester, title.strip()[:48] or "新对话", timestamp, timestamp),
        )
    return {"id": conversation_id, "requester": requester, "title": title.strip()[:48] or "新对话", "created_at": timestamp, "updated_at": timestamp}


def list_chat_conversations(requester: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_conversations WHERE requester = ? ORDER BY updated_at DESC LIMIT ?",
            (requester, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_chat_messages(conversation_id: str, requester: str) -> list[dict[str, str]] | None:
    with connect() as conn:
        conversation = conn.execute("SELECT 1 FROM chat_conversations WHERE id = ? AND requester = ?", (conversation_id, requester)).fetchone()
        if conversation is None:
            return None
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def add_chat_message(conversation_id: str, requester: str, role: str, content: str) -> dict[str, str] | None:
    if role not in {"user", "assistant"}:
        raise ValueError("unsupported chat role")
    timestamp = utc_now()
    with connect() as conn:
        conversation = conn.execute("SELECT title FROM chat_conversations WHERE id = ? AND requester = ?", (conversation_id, requester)).fetchone()
        if conversation is None:
            return None
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, timestamp),
        )
        title = conversation["title"]
        if role == "user" and title == "新对话":
            title = content.replace("\n", " ").strip()[:32] or "新对话"
            conn.execute("UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?", (title, timestamp, conversation_id))
        else:
            conn.execute("UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))
    return {"role": role, "content": content, "created_at": timestamp}


def delete_chat_conversation(conversation_id: str, requester: str) -> bool:
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM chat_conversations WHERE id = ? AND requester = ?", (conversation_id, requester)).fetchone()
        if exists is None:
            return False
        conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conversation_id,))
    return True


def system_metrics() -> dict[str, int | bool]:
    with connect() as conn:
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('assets', 'alerts', 'tickets', 'work_orders')"
        ).fetchone()[0]
        knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        approval_count = conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('approved', 'approved_safe_mode', 'executed')").fetchone()[0]
        metric_count = conn.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]
        imported_metric_count = conn.execute("SELECT COUNT(*) FROM metric_definitions WHERE source = 'csv'").fetchone()[0]
        import_count = conn.execute("SELECT COUNT(*) FROM metric_imports").fetchone()[0]
    return {"demo_tables": table_count, "knowledge_documents": knowledge_count, "metric_definitions": metric_count, "imported_metric_definitions": imported_metric_count, "metric_imports": import_count, "audit_events": audit_count, "approved_actions": approval_count}


def list_metric_definitions(keyword: str = "", category: str = "", limit: int = 60) -> list[dict[str, Any]]:
    clauses, params = [], []
    if keyword:
        clauses.append("(name LIKE ? OR description LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, category, unit, asset_scope, description, source FROM metric_definitions" + where + " ORDER BY CASE WHEN source = 'csv' THEN 0 ELSE 1 END, id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def metric_trend(metric_id: int, points: int = 24) -> dict[str, Any] | None:
    with connect() as conn:
        definition = conn.execute(
            "SELECT id, name, category, unit, asset_scope, description, source FROM metric_definitions WHERE id = ?", (metric_id,)
        ).fetchone()
        if definition is None:
            return None
        rows = conn.execute(
            "SELECT observed_at, value FROM metric_samples WHERE metric_id = ? ORDER BY observed_at DESC LIMIT ?",
            (metric_id, points),
        ).fetchall()
    return {**dict(definition), "points": list(reversed([dict(row) for row in rows]))}


def list_metric_imports(limit: int = 8) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, requester, filename, total_rows, sample_count, created_metrics, updated_metrics FROM metric_imports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def knowledge_document_count(tag: str = "", status: str = "") -> int:
    with connect() as conn:
        where, params = _knowledge_where(tag, status)
        return conn.execute("SELECT COUNT(*) FROM knowledge_documents" + where, params).fetchone()[0]


def _knowledge_where(tag: str = "", status: str = "") -> tuple[str, list[str]]:
    clauses, params = [], []
    if tag:
        clauses.append("tags LIKE ?"); params.append("%" + tag + "%")
    if status == "active": clauses.append("(expires_at IS NULL OR expires_at >= date('now'))")
    if status == "expired": clauses.append("expires_at IS NOT NULL AND expires_at < date('now')")
    if status == "expiring": clauses.append("expires_at IS NOT NULL AND expires_at >= date('now') AND expires_at <= date('now', '+7 day')")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_knowledge_documents(limit: int = 100, offset: int = 0, tag: str = "", status: str = "") -> list[dict[str, Any]]:
    with connect() as conn:
        where, params = _knowledge_where(tag, status)
        rows = conn.execute(
            "SELECT id, title, content, tags, version, updated_at, expires_at, "
            "CASE WHEN expires_at IS NOT NULL AND expires_at < date('now') THEN 'expired' "
            "WHEN expires_at IS NOT NULL AND expires_at <= date('now', '+7 day') THEN 'expiring' ELSE 'active' END AS status, "
            "CASE WHEN expires_at IS NULL THEN NULL ELSE CAST(julianday(expires_at) - julianday('now') AS INTEGER) END AS expires_in_days "
            "FROM knowledge_documents" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        ).fetchall()
    return [dict(row) for row in rows]


def add_knowledge_document(title: str, content: str, tags: str, expires_at: str | None = None) -> dict[str, Any]:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_documents (title, content, tags, version, updated_at, expires_at) VALUES (?, ?, ?, 1, ?, ?)", (title, content, tags, now, expires_at or None)
        )
        row = conn.execute(
            "SELECT id, title, content, tags, version, updated_at, expires_at, 'active' AS status FROM knowledge_documents WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        conn.execute("INSERT INTO knowledge_versions (document_id, version, title, content, tags, created_at) VALUES (?, 1, ?, ?, ?, ?)", (cursor.lastrowid, title, content, tags, now))
    document = dict(row)
    index_knowledge_document(document["id"])
    return document


def update_knowledge_document(document_id: int, title: str, content: str, tags: str, expires_at: str | None = None) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as conn:
        current = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        if current is None:
            return None
        next_version = int(current["version"]) + 1
        conn.execute(
            "UPDATE knowledge_documents SET title = ?, content = ?, tags = ?, version = ?, updated_at = ?, expires_at = ? WHERE id = ?",
            (title.strip(), content, tags.strip() or "未分类", next_version, now, expires_at or None, document_id),
        )
        conn.execute(
            "INSERT INTO knowledge_versions (document_id, version, title, content, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, next_version, title.strip(), content, tags.strip() or "未分类", now),
        )
        row = conn.execute(
            "SELECT id, title, content, tags, version, updated_at, expires_at, 'active' AS status FROM knowledge_documents WHERE id = ?", (document_id,)
        ).fetchone()
    document = dict(row)
    index_knowledge_document(document_id)
    return document


def list_knowledge_versions(document_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, document_id, version, title, content, tags, created_at FROM knowledge_versions WHERE document_id = ? ORDER BY version DESC",
            (document_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def rollback_knowledge_document(document_id: int, version: int) -> dict[str, Any] | None:
    with connect() as conn:
        selected = conn.execute("SELECT title, content, tags FROM knowledge_versions WHERE document_id = ? AND version = ?", (document_id, version)).fetchone()
        current = conn.execute("SELECT expires_at FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
    if selected is None or current is None:
        return None
    return update_knowledge_document(document_id, selected["title"], selected["content"], selected["tags"], current["expires_at"])


def delete_knowledge_document(document_id: int) -> bool:
    with connect() as conn:
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM knowledge_versions WHERE document_id = ?", (document_id,))
        cursor = conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
    return cursor.rowcount == 1


def knowledge_tags() -> list[str]:
    return sorted({tag.strip() for row in list_knowledge_documents(limit=1000) for tag in row["tags"].replace("，", ",").split(",") if tag.strip()})


def document_chunks(document_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT chunk_index, content, token_count FROM knowledge_chunks WHERE document_id = ? ORDER BY chunk_index", (document_id,)).fetchall()
    return [dict(row) for row in rows]


def _split_knowledge(content: str, size: int = 180, overlap: int = 30) -> list[str]:
    normalized = " ".join(content.split())
    if len(normalized) <= size:
        return [normalized] if normalized else []
    chunks, start = [], 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        # Prefer ending at a sentence boundary when practical.
        boundary = max(normalized.rfind(mark, start + size // 2, end) for mark in "。；；.!?")
        if boundary > start:
            end = boundary + 1
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks


def index_knowledge_document(document_id: int) -> int:
    with connect() as conn:
        document = conn.execute("SELECT content FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        if document is None:
            return 0
        chunks = _split_knowledge(document["content"])
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        conn.executemany(
            "INSERT INTO knowledge_chunks (document_id, chunk_index, content, token_count) VALUES (?, ?, ?, ?)",
            [(document_id, index, chunk, max(1, len(chunk) // 3)) for index, chunk in enumerate(chunks)],
        )
    return len(chunks)


def rebuild_knowledge_index() -> int:
    with connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM knowledge_documents").fetchall()]
    return sum(index_knowledge_document(document_id) for document_id in ids)


def knowledge_chunks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.document_id, c.chunk_index, c.content, c.token_count, d.title, d.tags "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id = d.id "
            "WHERE d.expires_at IS NULL OR d.expires_at >= date('now') "
            "ORDER BY c.document_id, c.chunk_index"
        ).fetchall()
    return [dict(row) for row in rows]


def save_knowledge_evaluation_feedback(evaluation_id: str, question: str, score: int, comment: str, requester: str) -> dict[str, Any]:
    created_at = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_eval_feedback (evaluation_id, question, score, comment, requester, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (evaluation_id, question.strip(), score, comment.strip(), requester, created_at),
        )
    return {"id": cursor.lastrowid, "evaluation_id": evaluation_id, "question": question.strip(), "score": score, "comment": comment.strip(), "requester": requester, "created_at": created_at}


def knowledge_evaluation_feedback_summary(evaluation_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count, AVG(score) AS average_score FROM knowledge_eval_feedback WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
    return {"count": row["count"], "average_score": round(row["average_score"], 2) if row["average_score"] is not None else None}


APPROVAL_STATUSES = {"pending", "approved", "approved_safe_mode", "executed", "rejected", "expired"}


def _approval_status_clause(status: str) -> tuple[str, tuple[str, ...]]:
    """Build a fixed, parameterized status predicate for approval list views."""
    if not status:
        return "", ()
    if status not in APPROVAL_STATUSES:
        raise ValueError(f"Unsupported approval status: {status}")
    return " WHERE status = ?", (status,)


def list_approvals(limit: int = 100, offset: int = 0, status: str = "") -> list[dict[str, Any]]:
    with connect() as conn:
        _expire_pending_approvals(conn)
        where_clause, parameters = _approval_status_clause(status)
        rows = conn.execute(
            "SELECT id, created_at, requester, sql, reason, status, risk_level, impact_preview, decision_comment, "
            "decided_by, decided_at, expires_at, executed_at, execution_result "
            f"FROM approvals{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*parameters, limit, offset)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if item["impact_preview"]:
            item["impact_preview"] = json.loads(item["impact_preview"])
        if item["execution_result"]:
            item["execution_result"] = json.loads(item["execution_result"])
        result.append(item)
    return result


def monitoring_overview() -> dict[str, Any]:
    with connect() as conn:
        asset_states = [dict(row) for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM assets GROUP BY status ORDER BY count DESC"
        ).fetchall()]
        alert_severity = [dict(row) for row in conn.execute(
            "SELECT severity, COUNT(*) AS count FROM alerts WHERE status != 'closed' GROUP BY severity ORDER BY severity"
        ).fetchall()]
        latest_alerts = [dict(row) for row in conn.execute(
            "SELECT a.id, a.severity, a.title, a.status, a.created_at, s.name AS asset_name, s.region "
            "FROM alerts a JOIN assets s ON a.asset_id = s.id WHERE a.status != 'closed' "
            "ORDER BY a.created_at DESC LIMIT 10"
        ).fetchall()]
        open_tickets = conn.execute("SELECT COUNT(*) FROM tickets WHERE status != 'closed'").fetchone()[0]
        metric_patterns = [
            ("CPU 使用率", "主机"), ("内存使用率", "主机"), ("磁盘使用率", "主机"),
            ("网络入站流量", "网络"), ("请求延迟 P95", "应用"),
        ]
        metric_series = []
        for name, category in metric_patterns:
            definition = conn.execute(
                "SELECT id, name, unit, asset_scope, source FROM metric_definitions WHERE name LIKE ? AND category = ? ORDER BY CASE WHEN source = 'csv' THEN 0 ELSE 1 END, id LIMIT 1",
                (name + "%", category),
            ).fetchone()
            if definition is None:
                continue
            points = conn.execute(
                "SELECT observed_at, value FROM metric_samples WHERE metric_id = ? ORDER BY observed_at DESC LIMIT 24",
                (definition["id"],),
            ).fetchall()
            metric_series.append({**dict(definition), "points": list(reversed([dict(point) for point in points]))})
        alert_dates = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count FROM alerts GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()
    alert_trend = list(reversed([dict(row) for row in alert_dates]))
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
            knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
            chunk_count = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        health_checks = [
            {"name": "API 服务", "status": "healthy", "detail": "当前进程正常响应"},
            {"name": "SQLite 数据库", "status": "healthy", "detail": "连接与只读探针正常"},
            {"name": "知识库索引", "status": "healthy" if chunk_count else "degraded", "detail": f"{knowledge_count} 份文档 · {chunk_count} 个分块"},
            {"name": "本地 DeepSeek", "status": "healthy" if settings.chat_enabled else "degraded", "detail": "模型配置已加载" if settings.chat_enabled else "未配置，使用离线兜底"},
        ]
    except sqlite3.Error as exc:
        health_checks = [{"name": "SQLite 数据库", "status": "down", "detail": f"探针失败：{exc}"}]
    return {
        "asset_states": asset_states, "alert_severity": alert_severity,
        "latest_alerts": latest_alerts, "open_tickets": open_tickets,
        "metric_series": metric_series, "alert_trend": alert_trend,
        "health_checks": health_checks, "sample_source": "SQLite metric_samples / alerts",
    }


def list_audit(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def audit_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]


def approval_count(status: str = "") -> int:
    with connect() as conn:
        _expire_pending_approvals(conn)
        where_clause, parameters = _approval_status_clause(status)
        return conn.execute(f"SELECT COUNT(*) FROM approvals{where_clause}", parameters).fetchone()[0]


def approval_status_counts() -> dict[str, int]:
    """Return stable zero-filled counts so the approval filter badges are reliable."""
    with connect() as conn:
        _expire_pending_approvals(conn)
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM approvals GROUP BY status").fetchall()
    counts = {status: 0 for status in APPROVAL_STATUSES}
    counts.update({row["status"]: row["count"] for row in rows})
    return counts


def data_catalog() -> list[dict[str, Any]]:
    """Safe local SQLite catalog for the built-in read-only data explorer."""
    with connect() as conn:
        result = []
        for name, label in EXPLORER_TABLES.items():
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            columns = conn.execute(f"PRAGMA table_info({name})").fetchall()
            result.append({"name": name, "label": label, "row_count": count, "column_count": len(columns)})
    return result


def table_snapshot(table_name: str, limit: int = 30, offset: int = 0) -> dict[str, Any] | None:
    """Return schema plus a bounded sample. Table name is fixed to the allow-list above."""
    if table_name not in EXPLORER_TABLES:
        return None
    with connect() as conn:
        schema = [
            {"name": row["name"], "type": row["type"], "required": bool(row["notnull"]), "primary_key": bool(row["pk"])}
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        total = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()]
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
    """Verify all or a bounded tail of the local audit hash chain."""
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        checked = min(max(limit or total, 1), total) if total else 0
        offset = total - checked
        anchor = conn.execute("SELECT entry_hash FROM audit_logs ORDER BY rowid LIMIT 1 OFFSET ?", (offset - 1,)).fetchone() if offset else None
        rows = conn.execute(
            "SELECT id, created_at, requester, action, payload, prev_hash, entry_hash FROM audit_logs ORDER BY rowid LIMIT ? OFFSET ?",
            (checked, offset),
        ).fetchall()
    previous = anchor["entry_hash"] if anchor else ""
    for index, row in enumerate(rows, start=1):
        payload = _canonical_payload(json.loads(row["payload"]))
        expected = _audit_digest(previous, row["id"], row["created_at"], row["requester"], row["action"], payload)
        if row["prev_hash"] != previous or row["entry_hash"] != expected:
            return {"valid": False, "checked_events": index - 1, "total_events": total, "scope": "full" if checked == total else "recent", "broken_at": row["id"]}
        previous = expected
    return {"valid": True, "checked_events": len(rows), "total_events": total, "scope": "full" if checked == total else "recent", "latest_hash": previous or None}
