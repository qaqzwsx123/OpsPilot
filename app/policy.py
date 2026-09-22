"""RBAC 与工具风险等级策略：权限判断必须在后端完成。"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Role:
    id: str
    label: str
    description: str
    permissions: tuple[str, ...]


ROLES = (
    Role("viewer", "观察者", "只读查看数据、指标和知识库，不能发起变更。", ("read", "rag")),
    Role("operator", "运维工程师", "可执行只读查询，能够发起需要审批的变更申请。", ("read", "rag", "request_change")),
    Role("approver", "值班负责人", "拥有运维工程师权限，并能够审批受控变更。", ("read", "rag", "request_change", "approve_change")),
)


def role_catalog() -> list[dict]:
    return [{**asdict(role), "permissions": list(role.permissions)} for role in ROLES]


def role_by_id(role_id: str) -> Role | None:
    return next((role for role in ROLES if role.id == role_id), None)


def permitted(role_id: str, permission: str) -> bool:
    # 页面只负责展示，真正授权以这里的服务端判断为准。
    role = role_by_id(role_id)
    return bool(role and permission in role.permissions)


def policy_summary() -> list[dict[str, str]]:
    return [
        {"operation": "只读 SQL / RAG / 自动工具", "tier": "AUTO", "required_permission": "read"},
        {"operation": "创建作业、关闭告警、写库操作", "tier": "MANUAL", "required_permission": "request_change"},
        {"operation": "审批待执行变更", "tier": "MANUAL", "required_permission": "approve_change"},
        {"operation": "删除审计记录（删除动作仍会留痕）", "tier": "MANUAL", "required_permission": "approve_change"},
        {"operation": "数据库维护、DDL、多语句", "tier": "BLOCKED", "required_permission": "无"},
    ]
