"""Agent 工具白名单：统一定义工具描述、风险等级和受控执行入口。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.database import execute_readonly, list_approvals, list_audit, list_knowledge_documents, list_metric_definitions, system_metrics


@dataclass(frozen=True, slots=True)
# 作用：说明类 ToolDefinition 的输入、输出与安全边界，避免调用方越过受控流程。
class ToolDefinition:
    name: str
    description: str
    risk: str
    category: str


TOOLS = (
    ToolDefinition("asset_lookup", "查询离线与维护中的设备资产", "auto", "资产"),
    ToolDefinition("alert_query", "查询最近的未关闭告警", "auto", "告警"),
    ToolDefinition("ticket_query", "查询当前未关闭运维工单", "auto", "工单"),
    ToolDefinition("work_order_query", "查询待执行运维作业", "auto", "作业"),
    ToolDefinition("knowledge_search", "查看当前知识库文档索引", "auto", "知识库"),
    ToolDefinition("system_health", "读取 Agent 服务与数据接入状态", "auto", "系统"),
    ToolDefinition("approval_queue", "查看待处理和最近审批单状态", "auto", "审批"),
    ToolDefinition("audit_recent", "查看最近关键操作的审计轨迹", "auto", "审计"),
    ToolDefinition("metric_catalog", "检索已接入的指标定义与数据来源", "auto", "可观测性"),
    ToolDefinition("create_work_order", "创建运维作业单", "manual", "变更"),
    ToolDefinition("close_alert", "关闭指定告警", "manual", "告警"),
    ToolDefinition("database_maintenance", "执行数据库维护指令", "blocked", "数据库"),
)


# 作用：说明函数 catalog 的输入、输出与安全边界，避免调用方越过受控流程。
def catalog() -> list[dict[str, str]]:
    # 前端工具中心和模型 Function Calling 都从同一份目录生成。
    return [asdict(tool) for tool in TOOLS]


# 作用：说明函数 definition 的输入、输出与安全边界，避免调用方越过受控流程。
def definition(name: str) -> ToolDefinition | None:
    return next((tool for tool in TOOLS if tool.name == name), None)


# 作用：说明函数 invoke 的输入、输出与安全边界，避免调用方越过受控流程。
def invoke(name: str, query: str = "") -> dict[str, Any]:
    # 工具调用只允许走这里，AUTO 读操作和 MANUAL/BLOCKED 结果均可审计。
    readonly_queries = {
        "asset_lookup": "SELECT id, name, region, status, owner, updated_at FROM assets WHERE status != 'online' ORDER BY updated_at DESC LIMIT 20;",
        "alert_query": "SELECT id, severity, title, status, created_at FROM alerts WHERE status != 'closed' ORDER BY created_at DESC LIMIT 20;",
        "ticket_query": "SELECT id, priority, title, status, assignee, created_at FROM tickets WHERE status != 'closed' ORDER BY created_at DESC LIMIT 20;",
        "work_order_query": "SELECT id, asset_id, action, status, created_at FROM work_orders WHERE status != 'completed' ORDER BY created_at DESC LIMIT 20;",
    }
    if name in readonly_queries:
        return {"status": "completed", "tool": name, "rows": execute_readonly(readonly_queries[name])}
    if name == "knowledge_search":
        if query.strip():
            from app.rag import KnowledgeRag

            evidence = KnowledgeRag().search(query, top_k=5)
            return {"status": "completed", "tool": name, "documents": evidence, "query": query[:160]}
        return {"status": "completed", "tool": name, "documents": list_knowledge_documents(20)}
    if name == "system_health":
        return {"status": "completed", "tool": name, "metrics": system_metrics()}
    if name == "approval_queue":
        rows = list_approvals(limit=20)
        return {"status": "completed", "tool": name, "pending_count": sum(row["status"] == "pending" for row in rows), "approvals": rows}
    if name == "audit_recent":
        return {"status": "completed", "tool": name, "events": list_audit(limit=20)}
    if name == "metric_catalog":
        return {"status": "completed", "tool": name, "metrics": list_metric_definitions(limit=20)}
    if name in {"create_work_order", "close_alert"}:
        return {"status": "approval_required", "tool": name, "message": "该工具会改变生产状态，需在审批中心创建并确认操作。"}
    if name == "database_maintenance":
        return {"status": "blocked", "tool": name, "message": "数据库维护工具不允许被 Agent 直接调用。"}
    return {"status": "not_found", "tool": name, "message": "工具不存在。"}
