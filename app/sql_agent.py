from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass

from app.models import CandidateTable, ExecutionMode, GeneratedSql, ReviewResult


SCHEMA: dict[str, dict[str, object]] = {
    "assets": {
        "columns": ["id", "name", "region", "status", "owner", "updated_at"],
        "aliases": ["设备", "资产", "网关", "机器人", "在线", "离线"],
        "description": "设备资产、区域和在线状态",
    },
    "alerts": {
        "columns": ["id", "asset_id", "severity", "title", "status", "created_at"],
        "aliases": ["告警", "P1", "P2", "P3", "报警", "严重度"],
        "description": "设备告警和处置状态",
    },
    "tickets": {
        "columns": ["id", "priority", "title", "status", "assignee", "created_at"],
        "aliases": ["工单", "高优", "优先级", "负责人", "未关闭"],
        "description": "故障处理工单",
    },
    "work_orders": {
        "columns": ["id", "asset_id", "action", "status", "created_at"],
        "aliases": ["作业", "派单", "维修", "操作"],
        "description": "执行中的运维动作",
    },
    "metric_definitions": {
        "columns": ["id", "name", "category", "unit", "asset_scope", "description"],
        "aliases": ["指标", "指标目录", "CPU", "内存", "延迟", "成功率"],
        "description": "运维指标定义与指标分类",
    },
    "metric_samples": {
        "columns": ["id", "metric_id", "observed_at", "value"],
        "aliases": ["趋势", "时序", "采样", "监控值"],
        "description": "指标时间序列采样数据",
    },
}


@lru_cache(maxsize=1)
def active_schema() -> dict[str, dict[str, object]]:
    """Uses demo metadata by default; fetches actual schema when MySQL is configured."""
    from app.config import settings

    if settings.mysql_enabled:
        from app.mysql_adapter import introspect_schema

        return introspect_schema()
    return SCHEMA


class MetadataRetriever:
    """Three-way recall: schema words, business aliases and description semantics."""

    def retrieve(self, question: str, top_k: int = 3) -> list[CandidateTable]:
        normalized = question.lower()
        candidates: list[CandidateTable] = []
        for table, meta in active_schema().items():
            matched: list[str] = []
            score = 0
            if table in normalized:
                score += 4
                matched.append("table_name")
            if any(column.lower() in normalized for column in meta["columns"]):
                score += 2
                matched.append("column_name")
            aliases = [alias for alias in meta["aliases"] if alias.lower() in normalized]
            if aliases:
                score += len(aliases) * 3
                matched.append("business_alias:" + ",".join(aliases))
            description_tokens = set(str(meta["description"]).lower())
            semantic_hits = sum(1 for char in set(normalized) if char in description_tokens)
            if semantic_hits >= 2:
                score += 1
                matched.append("semantic_hint")
            if score:
                candidates.append(CandidateTable(table, score, matched, list(meta["columns"])))
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k]


class RuleBasedSqlWriter:
    """Offline provider. Replace it with an LLM provider in production."""

    def generate(self, question: str, tables: list[CandidateTable]) -> GeneratedSql | None:
        q = question.lower()
        # Knowledge-seeking questions should not be forced into a database query merely
        # because they mention a severity such as P1. They are handled by the RAG route.
        if any(phrase in q for phrase in ["如何处理", "怎么处理", "处理流程", "sop", "排障步骤", "规范"]):
            return None
        if any(word in q for word in ["删除", "清空", "更新", "修改", "写入"]):
            return GeneratedSql("DELETE FROM alerts WHERE status = 'closed';", "write_request", 0.75, ["alerts"])
        metric_id_match = re.search(r"#\s*(\d{1,3})", question)
        if "指标" in q and metric_id_match:
            metric_id = int(metric_id_match.group(1))
            return GeneratedSql(
                "SELECT d.id, d.name, d.unit, s.observed_at, s.value FROM metric_definitions d "
                "JOIN metric_samples s ON d.id = s.metric_id WHERE d.id = " + str(metric_id) +
                " ORDER BY s.observed_at DESC LIMIT 24;",
                "metric_trend", 0.9, ["metric_definitions", "metric_samples"],
            )
        if "指标目录" in q or ("指标" in q and "查询" in q):
            return GeneratedSql(
                "SELECT id, name, category, unit, asset_scope FROM metric_definitions ORDER BY id LIMIT 30;",
                "metric_catalog", 0.84, ["metric_definitions"],
            )
        if "各区域" in q and "离线" in q:
            return GeneratedSql(
                "SELECT region, COUNT(*) AS offline_count FROM assets WHERE status = 'offline' GROUP BY region ORDER BY offline_count DESC LIMIT 20;",
                "offline_assets_by_region", 0.9, ["assets"],
            )
        if ("p1" in q or "告警" in q) and any(word in q for word in ["设备", "关联", "影响"]):
            where = " WHERE a.severity = 'P1'" if "p1" in q else ""
            return GeneratedSql(
                "SELECT a.id AS alert_id, a.severity, a.title AS alert_title, a.status AS alert_status, "
                "s.name AS asset_name, s.region, s.status AS asset_status "
                "FROM alerts a JOIN assets s ON a.asset_id = s.id" + where + " ORDER BY a.created_at DESC LIMIT 20;",
                "alerts_with_assets", 0.89, ["alerts", "assets"],
            )
        if "告警" in q or "p1" in q or "报警" in q:
            where = []
            if "p1" in q:
                where.append("severity = 'P1'")
            if "未关闭" in q or "未处理" in q or "open" in q:
                where.append("status != 'closed'")
            clause = " WHERE " + " AND ".join(where) if where else ""
            return GeneratedSql(
                f"SELECT id, asset_id, severity, title, status, created_at FROM alerts{clause} ORDER BY created_at DESC LIMIT 20;",
                "list_alerts", 0.91, ["alerts"],
            )
        if "离线" in q and any(word in q for word in ["设备", "网关", "资产", "华东", "华北", "华南"]):
            where = ["status = 'offline'"]
            for region in ["华东", "华北", "华南"]:
                if region in question:
                    where.append(f"region = '{region}'")
            return GeneratedSql(
                "SELECT id, name, region, status, owner, updated_at FROM assets WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC LIMIT 20;",
                "list_offline_assets", 0.92, ["assets"],
            )
        if "工单" in q:
            where = []
            if "未关闭" in q or "未完成" in q:
                where.append("status != 'closed'")
            if "高优" in q or "高优先级" in q:
                where.append("priority = 'high'")
            clause = " WHERE " + " AND ".join(where) if where else ""
            return GeneratedSql(
                f"SELECT id, priority, title, status, assignee, created_at FROM tickets{clause} ORDER BY created_at DESC LIMIT 20;",
                "list_tickets", 0.88, ["tickets"],
            )
        return None


class SqlReviewer:
    forbidden = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|replace)\b", re.I)
    table_re = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)

    def review(self, sql: str, allowed_tables: list[str]) -> ReviewResult:
        normalized = " ".join(sql.strip().split())
        if not normalized.lower().startswith("select"):
            return ReviewResult(False, ["只允许 SELECT 查询"], normalized)
        if self.forbidden.search(normalized):
            return ReviewResult(False, ["检测到写入或 DDL 关键字"], normalized)
        if normalized.count(";") > 1 or (";" in normalized and not normalized.endswith(";")):
            return ReviewResult(False, ["只允许单条 SQL"], normalized)
        used_tables = self.table_re.findall(normalized)
        if not used_tables:
            return ReviewResult(False, ["未识别到 FROM/JOIN 表"], normalized)
        unknown = sorted(set(used_tables) - set(allowed_tables))
        if unknown:
            return ReviewResult(False, [f"存在未授权表: {', '.join(unknown)}"], normalized)
        if " limit " not in normalized.lower():
            normalized = normalized.rstrip(";") + " LIMIT 100;"
        return ReviewResult(True, [], normalized)


class SqlFixer:
    """Conservative deterministic repair. It only narrows read queries; it never repairs writes."""

    def fix(self, sql: str, issues: list[str], allowed_tables: list[str]) -> str | None:
        normalized = " ".join(sql.strip().split())
        if not normalized.lower().startswith("select"):
            return None
        if any("未授权表" in issue or "未识别到" in issue for issue in issues):
            return None
        # A model can accidentally emit multiple statements. Keep only the first
        # read statement and let the Reviewer revalidate it in the next iteration.
        if normalized.count(";") > 1:
            normalized = normalized.split(";", 1)[0].strip()
        if " limit " not in normalized.lower():
            normalized = normalized.rstrip(";") + " LIMIT 100"
        return normalized + ("" if normalized.endswith(";") else ";")


@dataclass(slots=True)
class RiskDecision:
    mode: ExecutionMode
    reason: str


class RiskAssessor:
    def assess(self, sql: str) -> RiskDecision:
        lowered = sql.lower()
        if re.search(r"\b(drop|alter|attach|pragma|vacuum)\b", lowered):
            return RiskDecision(ExecutionMode.BLOCKED, "包含不可在 Agent 中执行的高危数据库指令")
        if re.search(r"\b(insert|update|delete|replace)\b", lowered):
            return RiskDecision(ExecutionMode.MANUAL, "数据写操作需要人工审批")
        return RiskDecision(ExecutionMode.AUTO, "只读 SELECT，可自动执行")
