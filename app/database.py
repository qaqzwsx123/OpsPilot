from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "agent_demo.db"


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
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              action TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              sql TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
              executed_at TEXT, execution_result TEXT
            );
            CREATE TABLE IF NOT EXISTS conversation_memory (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requester TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metric_definitions (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
              unit TEXT NOT NULL, asset_scope TEXT NOT NULL, description TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metric_samples (
              id INTEGER PRIMARY KEY AUTOINCREMENT, metric_id INTEGER NOT NULL,
              observed_at TEXT NOT NULL, value REAL NOT NULL,
              FOREIGN KEY(metric_id) REFERENCES metric_definitions(id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
              chunk_index INTEGER NOT NULL, content TEXT NOT NULL, token_count INTEGER NOT NULL,
              FOREIGN KEY(document_id) REFERENCES knowledge_documents(id)
            );
            """
        )
        # Lightweight migrations keep existing local demo databases compatible.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
        if "executed_at" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN executed_at TEXT")
        if "execution_result" not in columns:
            conn.execute("ALTER TABLE approvals ADD COLUMN execution_result TEXT")


def seed_demo_data() -> None:
    initialize()
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        if count:
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
            "INSERT INTO knowledge_documents VALUES (?, ?, ?, ?)",
            [
                (1, "P1 告警处置 SOP", "P1 告警需要在 5 分钟内确认。先确认告警范围，再检查设备网络与心跳；无法恢复时创建高优工单并升级值班负责人。", "P1,告警,SOP,升级"),
                (2, "设备离线排障手册", "设备离线时依次检查供电、网络连通性、最近心跳和网关日志。若是单设备故障，优先安排现场巡检；若同区域批量离线，按网络故障升级。", "离线,设备,网络,排障"),
                (3, "工单优先级规范", "高优工单要求在两小时内响应。关闭工单前必须记录根因、处理动作与验证结果。", "工单,优先级,规范"),
            ],
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


def execute_readonly(sql: str) -> list[dict[str, Any]]:
    if settings.mysql_enabled:
        from app.mysql_adapter import execute_readonly as execute_mysql_readonly

        return execute_mysql_readonly(sql)
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def write_audit(requester: str, action: str, payload: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_logs VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), utc_now(), requester, action, json.dumps(payload, ensure_ascii=False)),
        )


def create_approval(requester: str, sql: str, reason: str) -> str:
    approval_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO approvals (id, created_at, requester, sql, reason, status) VALUES (?, ?, ?, ?, ?, ?)",
            (approval_id, utc_now(), requester, sql, reason, "pending"),
        )
    return approval_id


def approve(approval_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "pending":
            return dict(row)
        conn.execute("UPDATE approvals SET status = 'approved' WHERE id = ?", (approval_id,))
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row)


def execute_approved(approval_id: str, allow_writes: bool) -> dict[str, Any] | None:
    """Executes only the explicitly allow-listed local Demo operation after approval."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "approved":
            return {**dict(row), "outcome": "not_approved"}
        sql = " ".join(row["sql"].strip().split()).rstrip(";")
        if not allow_writes:
            return {**dict(row), "outcome": "safe_mode"}
        # The Demo deliberately permits exactly one auditable destructive action.
        # Production must replace this with an AST policy and short-lived credentials.
        if sql.lower() != "delete from alerts where status = 'closed'":
            return {**dict(row), "outcome": "not_allowlisted"}
        cursor = conn.execute(sql)
        outcome = {"deleted_rows": cursor.rowcount}
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


def system_metrics() -> dict[str, int | bool]:
    with connect() as conn:
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('assets', 'alerts', 'tickets', 'work_orders')"
        ).fetchone()[0]
        knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        approval_count = conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('approved', 'executed')").fetchone()[0]
        metric_count = conn.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]
    return {"demo_tables": table_count, "knowledge_documents": knowledge_count, "metric_definitions": metric_count, "audit_events": audit_count, "approved_actions": approval_count}


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
            "SELECT id, name, category, unit, asset_scope, description FROM metric_definitions" + where + " ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def metric_trend(metric_id: int, points: int = 24) -> dict[str, Any] | None:
    with connect() as conn:
        definition = conn.execute(
            "SELECT id, name, category, unit, asset_scope, description FROM metric_definitions WHERE id = ?", (metric_id,)
        ).fetchone()
        if definition is None:
            return None
        rows = conn.execute(
            "SELECT observed_at, value FROM metric_samples WHERE metric_id = ? ORDER BY observed_at DESC LIMIT ?",
            (metric_id, points),
        ).fetchall()
    return {**dict(definition), "points": list(reversed([dict(row) for row in rows]))}


def list_knowledge_documents(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, content, tags FROM knowledge_documents ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def add_knowledge_document(title: str, content: str, tags: str) -> dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_documents (title, content, tags) VALUES (?, ?, ?)", (title, content, tags)
        )
        row = conn.execute(
            "SELECT id, title, content, tags FROM knowledge_documents WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    document = dict(row)
    index_knowledge_document(document["id"])
    return document


def delete_knowledge_document(document_id: int) -> bool:
    with connect() as conn:
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        cursor = conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
    return cursor.rowcount == 1


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
            "ORDER BY c.document_id, c.chunk_index"
        ).fetchall()
    return [dict(row) for row in rows]


def list_approvals(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, requester, sql, reason, status, executed_at, execution_result "
            "FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
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
    return {
        "asset_states": asset_states, "alert_severity": alert_severity,
        "latest_alerts": latest_alerts, "open_tickets": open_tickets,
    }


def list_audit(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
