from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.tool_registry import definition, invoke


@dataclass(frozen=True, slots=True)
class ToolSelection:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    name: str
    arguments: dict[str, Any]
    reason: str


class ToolPlanner:
    """Model-first Function Calling planner with a deterministic safe fallback."""

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

    def execute_agent(self, question: str, max_rounds: int = 3, max_tools: int = 3) -> tuple[list[ToolSelection], list[dict[str, Any]], str, str]:
        """Let the local model choose tools, validate every call, and optionally continue planning."""
        selections: list[ToolSelection] = []
        evidence: list[dict[str, Any]] = []
        used: set[str] = set()
        mode = "model_function_calling"
        for round_number in range(1, max_rounds + 1):
            calls = self._model_choose(question, evidence, used)
            if calls is None:
                mode = "rule_based_allowlist_fallback"
                break
            accepted = 0
            for call in calls:
                tool = definition(call.name)
                if tool is None or tool.risk != "auto" or call.name in used or len(selections) >= max_tools:
                    continue
                used.add(call.name); accepted += 1
                selection = ToolSelection(call.name, call.reason or "模型根据问题和已有证据选择只读工具")
                selections.append(selection)
                query = str(call.arguments.get("query") or question)[:500]
                try:
                    result = invoke(call.name, query=query)
                    compact = self._compact(selection, result)
                    compact.update({"arguments": call.arguments, "round": round_number, "selection_mode": mode})
                    evidence.append(compact)
                except Exception as exc:
                    evidence.append({"tool": call.name, "reason": selection.reason, "arguments": call.arguments, "round": round_number, "selection_mode": mode, "status": "failed", "result_count": 0, "sample": [], "summary": f"工具执行失败：{type(exc).__name__}"})
            if accepted == 0 or len(selections) >= max_tools:
                break
        if not selections:
            selections, evidence = self.execute(question)
            mode = "rule_based_allowlist_fallback"
        summary = self._model_summarize(question, evidence) or self._fallback_summary(evidence)
        return selections, evidence, summary, mode

    def _model_choose(self, question: str, evidence: list[dict[str, Any]], used: set[str]) -> list[ModelToolCall] | None:
        if not settings.chat_enabled:
            return None
        available = [tool for tool in self.rules if definition(tool[0]) and definition(tool[0]).risk == "auto" and tool[0] not in used]
        tool_specs = [{"type": "function", "function": {"name": name, "description": definition(name).description, "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "传给工具的业务问题或筛选条件"}, "reason": {"type": "string", "description": "为什么需要调用这个工具"}}, "required": ["query"]}}} for name, _, _ in available]
        prompt = {"question": question, "already_called": sorted(used), "evidence": evidence[-3:], "instruction": "只从 tools 白名单中选择 0 到 2 个最有帮助的只读工具；如果现有证据已经足够，返回空调用。不要选择 manual 或 blocked 工具。"}
        payload = {"model": settings.chat_model, "messages": [{"role": "system", "content": "你是 OpsPilot 的工具规划器。必须遵守工具白名单，只规划只读调用。"}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)}], "tools": tool_specs, "tool_choice": "auto", "temperature": 0}
        try:
            response = self._request(payload)
            message = response.get("choices", [{}])[0].get("message", {})
            raw_calls = message.get("tool_calls") or []
            if not raw_calls and message.get("content"):
                content = str(message["content"]).replace("```json", "").replace("```", "").strip()
                parsed = json.loads(content); raw_calls = parsed.get("tool_calls", []) if isinstance(parsed, dict) else []
            result = []
            for raw in raw_calls:
                function = raw.get("function", raw) if isinstance(raw, dict) else {}
                name = str(function.get("name", ""))
                arguments = function.get("arguments", {})
                if isinstance(arguments, str): arguments = json.loads(arguments or "{}")
                if name and isinstance(arguments, dict): result.append(ModelToolCall(name, arguments, str(arguments.get("reason", ""))))
            return result
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None

    def _model_summarize(self, question: str, evidence: list[dict[str, Any]]) -> str | None:
        if not settings.chat_enabled or not evidence:
            return None
        payload = {"model": settings.chat_model, "messages": [{"role": "system", "content": "你是 OpsPilot。基于只读工具结果用中文给出简洁、可核对的运维结论。不要声称执行了变更；如果需要变更，明确建议进入审批中心。"}, {"role": "user", "content": json.dumps({"question": question, "tool_results": evidence}, ensure_ascii=False, default=str)}], "temperature": 0.2}
        try:
            content = self._request(payload).get("choices", [{}])[0].get("message", {}).get("content")
            return str(content).strip() if content else None
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _request(payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(f"{settings.chat_base_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=min(settings.chat_timeout_seconds, 12)) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _fallback_summary(evidence: list[dict[str, Any]]) -> str:
        if not evidence:
            return "未匹配到可自动调用的只读工具。"
        return "Agent 已完成只读工具编排：" + "、".join(item["tool"] for item in evidence) + "。详细结果见工作流轨迹；涉及变更的操作仍需进入审批流程。"

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
