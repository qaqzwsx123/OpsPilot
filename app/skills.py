"""Skills 注册与运行层：把重复运维经验封装为可审计的只读能力。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from statistics import mean
from typing import Any

from app.database import execute_readonly, list_approvals, metric_trend
from app.sql_agent import RiskAssessor


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    content: str
    category: str = "通用"
    risk: str = "auto"
    suggestions: tuple[str, ...] = ()
    runnable: bool = False


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root

    def load(self) -> list[Skill]:
        # 每次从 skills/*/SKILL.md 读取，便于新增 SOP 后无需改后端注册表。
        skills: list[Skill] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            content = path.read_text(encoding="utf-8")
            metadata = self._metadata(content)
            suggestions = tuple(item.strip() for item in metadata.get("suggestions", "").split("|") if item.strip())
            skills.append(Skill(
                metadata.get("name", path.parent.name), metadata.get("description", ""), content,
                metadata.get("category", "通用"), metadata.get("risk", "auto"), suggestions,
                metadata.get("runnable", "false").lower() == "true",
            ))
        return skills

    def get(self, name: str) -> Skill | None:
        return next((skill for skill in self.load() if skill.name == name), None)

    @staticmethod
    def _metadata(content: str) -> dict[str, str]:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        metadata: dict[str, str] = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
        return metadata


def run_skill(name: str, user_input: str = "") -> dict[str, Any]:
    """Deterministic, auditable skill runtimes. Skills never mutate business data."""
    # 运行时按 Skill 名称路由到固定实现，Skill 不拥有任意 SQL 或写库权限。
    text = user_input.strip()
    if name == "incident_triage":
        severity = (re.search(r"P[123]", text.upper()) or ["P1"])[0]
        region_match = re.search(r"华东|华南|华北", text)
        clauses, params = ["a.severity = ?", "a.status != 'closed'"], [severity]
        if region_match:
            clauses.append("s.region = ?")
            params.append(region_match[0])
        sql = (
            "SELECT a.id, a.severity, a.title, a.status, a.created_at, s.name AS asset_name, s.region "
            "FROM alerts a JOIN assets s ON a.asset_id = s.id WHERE " + " AND ".join(clauses) + " ORDER BY a.created_at DESC LIMIT 20"
        )
        rows = execute_readonly_with_params(sql, params)
        return {
            "skill": name, "status": "completed", "summary": f"识别到 {len(rows)} 条未关闭 {severity} 告警。先确认影响范围，再检查网络、心跳、供电和网关日志。",
            "data": rows,
            "next_steps": ["5 分钟内确认告警影响范围", "核查设备网络、心跳和最近变更", "未恢复则创建高优工单并升级值班负责人"],
            "risk": "auto",
        }
    if name == "metric_diagnosis":
        metric_id = int((re.search(r"#?(\d+)", text) or ["", "1"])[1])
        trend = metric_trend(metric_id)
        if trend is None:
            return {"skill": name, "status": "not_found", "summary": f"未找到指标 #{metric_id}。请在指标中心选择有效指标编号。", "data": [], "next_steps": [], "risk": "auto"}
        values = [point["value"] for point in trend["points"]]
        average, latest = mean(values), values[-1]
        deviation = round((latest - average) / average * 100, 1) if average else 0
        assessment = "高于" if deviation > 15 else "低于" if deviation < -15 else "接近"
        return {
            "skill": name, "status": "completed", "summary": f"{trend['name']} 当前值 {latest}{trend['unit']}，{assessment} 24 小时均值 {average:.2f}{trend['unit']}（偏差 {deviation}%）。",
            "data": {"metric": trend["name"], "latest": latest, "average": round(average, 2), "minimum": min(values), "maximum": max(values), "points": len(values)},
            "next_steps": ["确认异常时间点是否对应发布或流量变化", "查看关联设备和同类指标", "超过阈值时创建告警并记录处理结论"],
            "risk": "auto",
        }
    if name == "ticket_handoff":
        rows = execute_readonly("SELECT id, priority, title, status, assignee, created_at FROM tickets WHERE status != 'closed' ORDER BY CASE priority WHEN 'high' THEN 1 ELSE 2 END, created_at ASC LIMIT 20")
        high_count = sum(row["priority"] == "high" for row in rows)
        return {
            "skill": name, "status": "completed", "summary": f"当前有 {len(rows)} 个未关闭工单，其中高优 {high_count} 个。交接时应优先处理高优和无人认领工单。",
            "data": rows,
            "next_steps": ["逐单确认负责人、当前进展和阻塞项", "高优工单补齐最近动作与下次更新时间", "交接后在工单中记录接手人和验证计划"],
            "risk": "auto",
        }
    if name == "oncall_briefing":
        alerts = execute_readonly("SELECT severity, title, status, created_at FROM alerts WHERE status != 'closed' ORDER BY CASE severity WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, created_at DESC LIMIT 20")
        tickets = execute_readonly("SELECT priority, title, status, assignee, created_at FROM tickets WHERE status != 'closed' ORDER BY CASE priority WHEN 'high' THEN 1 ELSE 2 END, created_at ASC LIMIT 20")
        work_orders = execute_readonly("SELECT action, status, created_at FROM work_orders WHERE status != 'completed' ORDER BY created_at ASC LIMIT 20")
        approvals = [item for item in list_approvals(limit=20) if item["status"] == "pending"]
        p1_count = sum(item["severity"] == "P1" for item in alerts)
        return {
            "skill": name, "status": "completed",
            "summary": f"已生成值班简报：{len(alerts)} 条未关闭告警（P1 {p1_count} 条）、{len(tickets)} 个未关闭工单、{len(work_orders)} 个待执行作业、{len(approvals)} 张待审批单。",
            "data": {"alerts": alerts, "tickets": tickets, "work_orders": work_orders, "pending_approvals": approvals},
            "next_steps": ["优先确认 P1 告警与其负责人", "补齐高优工单的下一次更新时间", "明确待审批变更的窗口、影响和审批人", "将本简报同步给下一值班人员"],
            "risk": "auto",
        }
    if name == "capacity_review":
        rows = execute_readonly("SELECT id, name, region, status, updated_at FROM assets WHERE status != 'online' ORDER BY updated_at DESC LIMIT 20")
        trend = metric_trend(1)
        values = [point["value"] for point in (trend or {}).get("points", [])]
        latest = values[-1] if values else None
        average = round(mean(values), 2) if values else None
        return {
            "skill": name, "status": "completed",
            "summary": f"容量巡检发现 {len(rows)} 台非在线资产；CPU 指标当前值为 {latest if latest is not None else '—'}%，24 小时均值为 {average if average is not None else '—'}%。",
            "data": {"non_online_assets": rows, "cpu_latest": latest, "cpu_average": average, "cpu_peak": max(values) if values else None},
            "next_steps": ["核对离线或维护资产是否处于计划窗口", "观察 CPU 高点是否与流量或发布记录重合", "容量持续紧张时创建扩容评估工单", "记录本次巡检结论和下一复核时间"],
            "risk": "auto",
        }
    if name == "change_review":
        proposal = text or "DELETE FROM alerts WHERE status = 'closed';"
        decision = RiskAssessor().assess(proposal)
        high_risk = decision.mode.value != "auto"
        return {
            "skill": name, "status": "reviewed", "summary": f"变更评审结果：{decision.reason}。{'必须发起审批，不能直接执行。' if high_risk else '属于只读操作，仍需通过 SQL 审查。'}",
            "data": {"proposal": proposal, "risk_mode": decision.mode.value, "reason": decision.reason},
            "next_steps": ["确认影响范围和回滚方案", "选择维护窗口并指定审批人", "通过审批中心创建可审计的变更记录"],
            "risk": decision.mode.value,
        }
    return {"skill": name, "status": "not_runnable", "summary": "该 Skill 是规范型能力，请在智能查询或对应业务页面中使用。", "data": [], "next_steps": [], "risk": "auto"}


def execute_readonly_with_params(sql: str, params: list[str]) -> list[dict[str, Any]]:
    """The demo's parameterized skill query; kept isolated from generated SQL execution."""
    from app.database import connect

    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
