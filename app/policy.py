"""RBAC 与工具风险等级策略：权限判断必须在后端完成。"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Role:
    """前端身份对应的后端权限集合。

    角色只描述能力边界，是否允许某次具体操作还要结合工具风险等级、SQL 审查和审批状态。
    """

    # 稳定的机器可读角色 ID，写入请求和审计记录。
    id: str
    # 页面展示名称。
    label: str
    # 给用户解释该角色能做什么。
    description: str
    # 权限字符串集合，例如 read、rag、request_change、approve_change。
    permissions: tuple[str, ...]


# 角色目录是后端 RBAC 的单一事实来源，页面只能展示不能自行放权。
ROLES = (
    Role("viewer", "观察者", "只读查看数据、指标和知识库，不能发起变更。", ("read", "rag")),
    Role("operator", "运维工程师", "可执行只读查询，能够发起需要审批的变更申请。", ("read", "rag", "request_change")),
    Role("approver", "值班负责人", "拥有运维工程师权限，并能够审批受控变更。", ("read", "rag", "request_change", "approve_change")),
)


# 作用：说明函数 role_catalog 的输入、输出与安全边界，避免调用方越过受控流程。
def role_catalog() -> list[dict]:
    return [{**asdict(role), "permissions": list(role.permissions)} for role in ROLES]


# 作用：说明函数 role_by_id 的输入、输出与安全边界，避免调用方越过受控流程。
def role_by_id(role_id: str) -> Role | None:
    return next((role for role in ROLES if role.id == role_id), None)


# 作用：说明函数 permitted 的输入、输出与安全边界，避免调用方越过受控流程。
def permitted(role_id: str, permission: str) -> bool:
    # 页面只负责展示，真正授权以这里的服务端判断为准。
    role = role_by_id(role_id)
    return bool(role and permission in role.permissions)


# 作用：说明函数 policy_summary 的输入、输出与安全边界，避免调用方越过受控流程。
def policy_summary() -> list[dict[str, str]]:
    return [
        {"operation": "只读 SQL / RAG / 自动工具", "tier": "AUTO", "required_permission": "read"},
        {"operation": "创建作业、关闭告警、写库操作", "tier": "MANUAL", "required_permission": "request_change"},
        {"operation": "审批待执行变更", "tier": "MANUAL", "required_permission": "approve_change"},
        {"operation": "删除审计记录（删除动作仍会留痕）", "tier": "MANUAL", "required_permission": "approve_change"},
        {"operation": "数据库维护、DDL、多语句", "tier": "BLOCKED", "required_permission": "无"},
    ]
