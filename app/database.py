from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


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
              sql TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL
            );
            """
        )


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


def execute_readonly(sql: str) -> list[dict[str, Any]]:
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
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
            (approval_id, utc_now(), requester, sql, reason, "pending"),
        )
    return approval_id


def approve(approval_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE approvals SET status = 'approved' WHERE id = ?", (approval_id,))
    return dict(row)


def list_audit(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

