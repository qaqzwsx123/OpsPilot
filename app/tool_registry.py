"""Agent 工具白名单：统一定义工具描述、风险等级和受控执行入口。

本模块是 OpsPilot 的工具白名单与执行层，核心设计目标：
1. 内置 TOOLS 与 SQLite 中的自定义只读工具合并驱动工具中心、模型 schema、
   规划器校验和后端执行入口；
2. 每个工具都带明确 risk 等级，作为后端 invoke 的强制安全边界，而非 UI 标签；
3. 所有工具调用只能通过 invoke() 入口，便于统一审计和权限控制；
4. AUTO 工具为只读查询，MANUAL 工具进入审批流程，BLOCKED 工具直接拒绝；
5. 未注册的工具名统一返回 not_found，避免任意函数调用。

安全边界：
- risk 由后端强制校验，前端和模型无法绕过；
- 只读查询使用固定 SQL，不接受用户输入拼接；
- knowledge_search 使用参数化的 RAG 检索，不执行任意 SQL；
- MANUAL 工具不在此处执行写操作，只返回 approval_required；
- BLOCKED 工具无论谁调用都直接拒绝，不进入审批；
- 未注册工具名统一返回 not_found，防止探测内部函数。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

from dataclasses import asdict, dataclass  # asdict 用于把 ToolDefinition 序列化为字典
from typing import Any  # 宽松字典类型标注

# 只读查询和列表接口；所有工具实现都基于这些受控函数，不直接拼接 SQL。
from app.database import (
    custom_query_tool_definition,
    execute_custom_query_tool,
    execute_readonly,             # 执行固定只读 SQL
    list_custom_query_tools,
    list_approvals,               # 审批列表
    list_audit,                   # 审计列表
    list_knowledge_documents,     # 知识文档列表
    list_metric_definitions,      # 指标定义列表
    system_metrics,               # 系统指标
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Agent 可见的工具白名单条目。

    这里的定义同时驱动工具中心卡片、模型 Function Calling schema、规划器校验和后端执行入口。
    risk 不是 UI 标签，而是后端 invoke 的强制安全边界。

    设计说明：
    - frozen=True：定义创建后不可修改，保证白名单稳定；
    - slots=True：减少内存占用并防止动态属性；
    - 与 ToolPlanner、invoke、前端目录共享同一份定义，避免多处维护不一致。

    字段：
    - name：稳定的工具函数名，必须能在 invoke 中找到对应实现；
    - description：给用户和模型看的能力描述；
    - risk：auto 只读、manual 进入审批、blocked 直接拒绝；
    - category：工具中心的分组名称。
    """

    # 稳定的工具函数名，必须能在 invoke 中找到对应实现。
    name: str

    # 给用户和模型看的能力描述。
    description: str

    # auto 只读、manual 进入审批、blocked 直接拒绝。
    risk: str

    # 工具中心的分组名称。
    category: str

    # 自定义工具向 Function Calling 暴露的受限参数结构。
    parameters: dict[str, Any] | None = None

    # 标记是否来自本地数据库配置，而非内置代码目录。
    custom: bool = False


# 当前内置白名单共 12 个工具；用户自定义查询工具保存在 SQLite 中，与内置目录动态合并。
TOOLS = (
    # ---------- AUTO 只读工具（9 个）----------
    # 只读查询，可直接执行，不改变任何业务状态。
    ToolDefinition("asset_lookup", "查询离线与维护中的设备资产", "auto", "资产"),
    ToolDefinition("alert_query", "查询最近的未关闭告警", "auto", "告警"),
    ToolDefinition("ticket_query", "查询当前未关闭运维工单", "auto", "工单"),
    ToolDefinition("work_order_query", "查询待执行运维作业", "auto", "作业"),
    ToolDefinition("knowledge_search", "查看当前知识库文档索引", "auto", "知识库"),
    ToolDefinition("system_health", "读取 Agent 服务与数据接入状态", "auto", "系统"),
    ToolDefinition("approval_queue", "查看待处理和最近审批单状态", "auto", "审批"),
    ToolDefinition("audit_recent", "查看最近关键操作的审计轨迹", "auto", "审计"),
    ToolDefinition("metric_catalog", "检索已接入的指标定义与数据来源", "auto", "可观测性"),

    # ---------- MANUAL 工具（2 个）----------
    # 会改变生产状态，必须走审批流程，不能在 Agent 中直接执行。
    ToolDefinition("create_work_order", "创建运维作业单", "manual", "变更"),
    ToolDefinition("close_alert", "关闭指定告警", "manual", "告警"),

    # ---------- BLOCKED 工具（1 个）----------
    # 高危数据库维护，任何情况下都拒绝 Agent 直接调用。
    ToolDefinition("database_maintenance", "执行数据库维护指令", "blocked", "数据库"),
)


def catalog() -> list[dict[str, Any]]:
    """返回工具目录，供前端工具中心和模型 Function Calling 使用。

    返回：
    - list[dict]：每项包含 name、description、risk、category。

    说明：内置和自定义工具合并成同一份目录，便于 API 与模型共享。
    """
    # 前端工具中心和模型 Function Calling 都从同一份目录生成。
    builtin = [asdict(tool) for tool in TOOLS]
    custom = [{
        "name": tool["name"], "description": tool["description"], "risk": "auto",
        "category": tool["category"], "custom": True,
        "table_name": tool["table_name"], "columns": tool["columns"],
        "filters": tool["filters"], "max_rows": tool["max_rows"],
        "parameters": _filter_parameters(tool["filters"]),
    } for tool in list_custom_query_tools()]
    return builtin + custom


def _filter_parameters(filters: list[dict[str, Any]]) -> dict[str, Any]:
    """生成仅包含已配置筛选字段和比较符的 JSON Schema。"""
    properties: dict[str, Any] = {}
    for item in filters:
        properties[item["column"]] = {
            "type": "object",
            "properties": {
                "operator": {"type": "string", "enum": item["operators"]},
                "value": {"type": "string", "description": "筛选值；数值字段也以字符串形式传入"},
            },
            "required": ["operator", "value"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {"filters": {"type": "object", "properties": properties, "additionalProperties": False}},
        "additionalProperties": False,
    }


def definition(name: str) -> ToolDefinition | None:
    """按名称查找工具定义。

    参数：
    - name：工具名。

    返回：
    - ToolDefinition：找到时返回定义；
    - None：未注册的工具名。

    用途：
    - ToolPlanner 校验模型提议的工具名；
    - API 层判断工具是否存在；
    - invoke 前置校验工具风险等级。
    """
    builtin = next((tool for tool in TOOLS if tool.name == name), None)
    if builtin is not None:
        return builtin
    custom_tool = custom_query_tool_definition(name)
    if custom_tool is None:
        return None
    return ToolDefinition(
        custom_tool["name"], custom_tool["description"], "auto", custom_tool["category"],
        _filter_parameters(custom_tool["filters"]), True,
    )


def invoke(name: str, query: str = "", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """工具的统一执行入口。

    参数：
    - name：工具名；
    - query：可选查询文本，仅部分内置工具使用（如 knowledge_search）；
    - arguments：模型生成的调用参数，自定义查询只接受配置过的 filters。

    返回：
    - dict：统一结构的结果，至少包含 status 和 tool 字段：
      * status="completed"：只读工具执行成功；
      * status="approval_required"：MANUAL 工具，需走审批；
      * status="blocked"：BLOCKED 工具，拒绝执行；
      * status="not_found"：未注册工具。

    安全边界：
    - 工具调用只允许走这里，AUTO 读操作和 MANUAL/BLOCKED 结果均可审计；
    - 只读查询使用固定 SQL，不接受用户输入拼接；
    - knowledge_search 使用参数化的 RAG 检索；
    - MANUAL 工具不在此处执行写操作，只返回 approval_required；
    - BLOCKED 工具无论谁调用都直接拒绝，不进入审批；
    - 未注册工具名统一返回 not_found，防止探测内部函数。

    实现说明：
    - 前四个工具（asset/alert/ticket/work_order）共享一个 readonly_queries 字典，
      查询逻辑相同，只是 SQL 不同；
    - knowledge_search 支持带 query 的语义检索和无 query 的文档列表两种模式；
    - system_health 直接返回 system_metrics()；
    - approval_queue 额外统计 pending 数量，便于前端展示徽章；
    - metric_catalog 使用 list_metric_definitions()。
    """
    # 工具调用只允许走这里，AUTO 读操作和 MANUAL/BLOCKED 结果均可审计。
    # 前四个只读查询工具共享固定 SQL，使用 ? 占位符不使用，输入不经 SQL 拼接。
    readonly_queries = {
        "asset_lookup": (
            "SELECT id, name, region, status, owner, updated_at FROM assets "
            "WHERE status != 'online' ORDER BY updated_at DESC LIMIT 20;"
        ),
        "alert_query": (
            "SELECT id, severity, title, status, created_at FROM alerts "
            "WHERE status != 'closed' ORDER BY created_at DESC LIMIT 20;"
        ),
        "ticket_query": (
            "SELECT id, priority, title, status, assignee, created_at FROM tickets "
            "WHERE status != 'closed' ORDER BY created_at DESC LIMIT 20;"
        ),
        "work_order_query": (
            "SELECT id, asset_id, action, status, created_at FROM work_orders "
            "WHERE status != 'completed' ORDER BY created_at DESC LIMIT 20;"
        ),
    }

    # 自定义查询工具使用持久化的表/字段白名单和参数化筛选器。
    if name not in readonly_queries and custom_query_tool_definition(name) is not None:
        result = execute_custom_query_tool(name, arguments)
        return result or {"status": "not_found", "tool": name, "message": "工具不存在。"}

    # 只读查询工具：走固定 SQL，统一返回 rows。
    if name in readonly_queries:
        return {
            "status": "completed",
            "tool": name,
            "rows": execute_readonly(readonly_queries[name]),
        }

    # 知识库检索工具：
    # - 有 query 时走 RAG 语义检索，返回 Top-5 证据；
    # - 无 query 时返回最近 20 篇知识文档列表。
    if name == "knowledge_search":
        if query.strip():
            # 延迟导入，避免模块级循环依赖和启动开销。
            from app.rag import KnowledgeRag

            evidence = KnowledgeRag().search(query, top_k=5)
            return {
                "status": "completed",
                "tool": name,
                "documents": evidence,
                "query": query[:160],  # 截断查询文本，避免审计日志过长
            }
        return {
            "status": "completed",
            "tool": name,
            "documents": list_knowledge_documents(20),
        }

    # 系统健康工具：直接返回聚合后的系统指标。
    if name == "system_health":
        return {"status": "completed", "tool": name, "metrics": system_metrics()}

    # 审批队列工具：返回审批列表并统计 pending 数量。
    if name == "approval_queue":
        rows = list_approvals(limit=20)
        return {
            "status": "completed",
            "tool": name,
            "pending_count": sum(row["status"] == "pending" for row in rows),
            "approvals": rows,
        }

    # 审计轨迹工具：返回最近 20 条审计事件。
    if name == "audit_recent":
        return {"status": "completed", "tool": name, "events": list_audit(limit=20)}

    # 指标目录工具：返回最近 20 个指标定义。
    if name == "metric_catalog":
        return {"status": "completed", "tool": name, "metrics": list_metric_definitions(limit=20)}

    # MANUAL 工具：不在此处执行写操作，只返回 approval_required，
    # 由 API 层创建审批单并引导用户到审批中心。
    if name in {"create_work_order", "close_alert"}:
        return {
            "status": "approval_required",
            "tool": name,
            "message": "该工具会改变生产状态，需在审批中心创建并确认操作。",
        }

    # BLOCKED 工具：无论谁调用都直接拒绝，不进入审批流程。
    if name == "database_maintenance":
        return {
            "status": "blocked",
            "tool": name,
            "message": "数据库维护工具不允许被 Agent 直接调用。",
        }

    # 未注册工具名：统一返回 not_found，不抛出异常，避免探测内部实现。
    return {
        "status": "not_found",
        "tool": name,
        "message": "工具不存在。",
    }
