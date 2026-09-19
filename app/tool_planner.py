from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tool_registry import definition, invoke


@dataclass(frozen=True, slots=True)
class ToolSelection:
    name: str
    reason: str


class ToolPlanner:
    """Deterministic Function Calling planner for the registered AUTO tool allowlist."""

    rules = (
        ("alert_query", ("告警", "报警", "p1", "p2", "p3"), "问题涉及告警级别、状态或影响范围"),
        ("asset_lookup", ("设备", "资产", "网关", "离线", "在线", "维护", "机器人"), "问题涉及设备资产或运行状态"),
        ("ticket_query", ("工单", "高优", "负责人", "优先级"), "问题涉及故障工单或负责人"),
        ("work_order_query", ("作业", "派单", "维修任务", "待执行"), "问题涉及待执行运维作业"),
        ("knowledge_search", ("sop", "如何", "怎么", "流程", "规范", "排障", "手册"), "问题需要 SOP、操作步骤或规范知识"),
        ("metric_catalog", ("指标", "cpu", "内存", "延迟", "趋势", "成功率"), "问题涉及指标定义或时序观测"),
        ("approval_queue", ("审批", "待批", "变更单"), "问题涉及待处理审批或变更状态"),
        ("audit_recent", ("审计", "留痕", "追溯", "历史操作"), "问题涉及操作追溯或审计记录"),
        ("system_health", ("系统状态", "服务状态", "健康检查", "健康"), "问题涉及 Agent 或数据接入健康状态"),
    )

    def plan(self, question: str, max_tools: int = 3) -> list[ToolSelection]:
        normalized = question.lower()
        selected: list[ToolSelection] = []
        for name, triggers, reason in self.rules:
            tool = definition(name)
            if tool is None or tool.risk != "auto":
                continue
            if any(trigger in normalized for trigger in triggers):
                selected.append(ToolSelection(name, reason))
            if len(selected) >= max_tools:
                break
        return selected

    def execute(self, question: str) -> tuple[list[ToolSelection], list[dict[str, Any]]]:
        selections = self.plan(question)
        evidence: list[dict[str, Any]] = []
        for selection in selections:
            try:
                result = invoke(selection.name, query=question)
                evidence.append(self._compact(selection, result))
            except Exception as exc:  # a read-only tool failure must not stop the SQL/RAG workflow
                evidence.append({"tool": selection.name, "reason": selection.reason, "status": "failed", "result_count": 0, "sample": [], "summary": f"工具执行失败：{type(exc).__name__}"})
        return selections, evidence

    @staticmethod
    def _compact(selection: ToolSelection, result: dict[str, Any]) -> dict[str, Any]:
        rows = result.get("rows") or result.get("documents") or result.get("metrics") or result.get("approvals") or result.get("events") or []
        if isinstance(rows, list):
            sample = rows[:3]
            count = len(rows)
        else:
            sample = rows
            count = 0
        return {
            "tool": selection.name,
            "reason": selection.reason,
            "status": result.get("status", "unknown"),
            "result_count": count,
            "sample": sample,
            "summary": result.get("message") or (f"返回 {count} 条结构化记录" if isinstance(rows, list) else "已返回结构化状态"),
        }
