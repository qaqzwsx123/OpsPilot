"""Skills 注册与运行层：把重复运维经验封装为可审计的只读能力。

本模块是 OpsPilot 的 Skill 体系，核心设计目标：
1. 把常见运维任务封装为声明式流程，流程定义在 skills/*/SKILL.md；
2. 每个流程声明触发语、工具白名单、输入、步骤和结构化输出；
3. 流程统一通过工具注册表读取数据，绝不拥有任意 SQL 或写库权限；
4. 高风险 Skill（如变更评审）只输出风险评估结果，实际变更仍需走审批；
5. 每次运行都由上层 API 写审计，保证可追溯、可复现。

安全边界：
- Skill 本身不获得任意 SQL 执行权，只能调用 frontmatter 声明且已注册的 AUTO 工具；
- 工具查询继续由统一工具白名单执行，避免 SQL 注入；
- 写操作不在 Skill 中执行，只通过审批中心走受控流程；
- 非可运行的 SOP 文档由知识库管理，不出现在流程目录。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

from dataclasses import dataclass  # 声明 Skill 数据结构
from pathlib import Path  # 跨平台路径处理，定位 skills 目录
import re  # 用于从用户输入中提取 P1/P2/P3、区域、指标编号等
from typing import Any  # 宽松字典类型标注

from app.tool_registry import definition as tool_definition, invoke as invoke_tool  # 统一受控的工具目录和执行入口
from app.sql_agent import RiskAssessor  # 风险分级器，用于 change_review Skill


@dataclass(slots=True)
class Skill:
    """从 ``skills/*/SKILL.md`` 加载的一条运维 SOP 能力定义。

    设计说明：
    - slots=True：减少内存占用并防止动态属性；
    - 字段与 SKILL.md frontmatter 一一对应，便于解析和展示；
    - runnable 区分“有确定性运行器”和“仅作为规范文档”两类 Skill。

    字段：
    - name：稳定名称，也是运行时路由键；
    - description：列表卡片和 Agent 选择时展示的简要说明；
    - content：完整 SOP 文本，既可供 RAG 检索也可供用户阅读；
    - category：页面筛选用的业务分类；
    - risk：auto 只读、manual 需审批；Skill 本身不获得任意写库权限；
    - suggestions：从 SOP 元数据解析出的推荐提问示例；
    - runnable：是否有对应的确定性运行器。
    """

    # Skill 的稳定名称，也是运行时路由键。
    name: str

    # 列表卡片和 Agent 选择时展示的简要说明。
    description: str

    # 完整 SOP 文本，既可供 RAG 检索也可供用户阅读。
    content: str

    # 页面筛选用的业务分类。
    category: str = "通用"

    # auto 只读、manual 需审批；Skill 本身不获得任意写库权限。
    risk: str = "auto"

    # 从 SOP 元数据解析出的推荐提问示例。
    suggestions: tuple[str, ...] = ()

    # 是否有对应的确定性运行器，而不是只作为规范文档存在。
    runnable: bool = False

    # 自动路由用触发短语、流程可用工具及面向用户的执行说明。
    triggers: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    required_input: str = ""
    steps: tuple[str, ...] = ()
    output: str = ""


class SkillRegistry:
    """Skill 文件系统注册表。

    每次读取目录而不是把流程元数据硬编码在 Python 中，因此修改触发语、依赖工具或步骤后可以直接刷新；
    流程只能调用元数据声明且已注册的 AUTO 工具。

    使用方式：
        registry = SkillRegistry(Path("skills"))
        skills = registry.load()           # 加载可运行的运维流程
        skill = registry.get("incident_triage")  # 按名称获取
    """

    def __init__(self, root: Path):
        # skills 根目录，例如项目根目录下的 skills/。
        self.root = root

    def load(self, runnable_only: bool = True) -> list[Skill]:
        """加载 skills/*/SKILL.md；默认只返回可执行运维流程。

        返回：
        - list[Skill]：按路径排序的 Skill 列表。

        说明：
        - 使用 glob("*/SKILL.md") 遍历一级子目录中的 SKILL.md；
        - sorted 保证加载顺序稳定，便于测试和展示；
        - frontmatter 解析失败时使用安全默认值，不会抛异常影响整体加载。
        """
        # 每次从 skills/*/SKILL.md 读取，便于新增 SOP 后无需改后端注册表。
        skills: list[Skill] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            # 读取 SKILL.md 全文，包含 frontmatter 和正文。
            content = path.read_text(encoding="utf-8")

            # 解析 frontmatter 元数据（--- 之间的 key: value）。
            metadata = self._metadata(content)
            runnable = metadata.get("runnable", "false").lower() == "true"
            if runnable_only and not runnable:
                continue

            # suggestions 用 | 分隔多个示例问题。
            suggestions = tuple(
                item.strip()
                for item in metadata.get("suggestions", "").split("|")
                if item.strip()
            )

            # 构造 Skill 对象；缺失字段使用安全默认值。
            skills.append(Skill(
                metadata.get("name", path.parent.name),   # 缺省用目录名作为 name
                metadata.get("description", ""),
                content,
                metadata.get("category", "通用"),
                metadata.get("risk", "auto"),
                suggestions,
                runnable,
                self._items(metadata.get("triggers", "")),
                self._items(metadata.get("tools", "")),
                metadata.get("input", ""),
                self._items(metadata.get("steps", "")),
                metadata.get("output", ""),
            ))
        return skills

    def get(self, name: str) -> Skill | None:
        """按名称查找 Skill，不存在时返回 None。"""
        return next((skill for skill in self.load() if skill.name == name), None)

    def match(self, text: str) -> Skill | None:
        """按配置的明确触发短语选择最匹配的运维流程。"""
        normalized = "".join(text.casefold().split())
        matches = [
            skill for skill in self.load()
            if any("".join(term.casefold().split()) in normalized for term in skill.triggers)
        ]
        return max(
            matches,
            key=lambda skill: max((len(term) for term in skill.triggers if "".join(term.casefold().split()) in normalized), default=0),
            default=None,
        )

    @staticmethod
    def _items(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split("|") if item.strip())

    @staticmethod
    def _metadata(content: str) -> dict[str, str]:
        """解析 SKILL.md 的 frontmatter。

        参数：
        - content：SKILL.md 全文。

        返回：
        - dict[str, str]：frontmatter 中的 key: value 键值对。
          无 frontmatter 时返回空字典。

        格式约定：
        - 首行必须是 "---"；
        - 遇到下一个 "---" 结束解析；
        - 每行按第一个 ":" 分割 key 和 value，两侧去空白。
        """
        lines = content.splitlines()

        # 首行不是 --- 时视为无 frontmatter。
        if not lines or lines[0].strip() != "---":
            return {}

        metadata: dict[str, str] = {}
        # 从第二行开始解析，直到遇到下一个 ---。
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                # 只按第一个冒号分割，允许 value 中包含冒号。
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
        return metadata


def run_skill(name: str, user_input: str = "") -> dict[str, Any]:
    """执行一个声明式运维流程；所有业务数据均由工具白名单读取。"""
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    skill = registry.get(name)
    if skill is None:
        return {"skill": name, "status": "not_found", "summary": "未找到可运行的运维流程。", "data": [], "next_steps": [], "risk": "auto", "tools_used": [], "tool_evidence": []}

    text = user_input.strip()
    if name == "change_review":
        proposal = text or "DELETE FROM alerts WHERE status = 'closed';"
        decision = RiskAssessor().assess(proposal)
        return {
            "skill": name, "status": "reviewed",
            "summary": f"变更评审结果：{decision.reason}。{'必须进入人工审批，不能直接执行。' if decision.mode.value != 'auto' else '属于只读操作，仍需经过 SQL 审查。'}",
            "data": {"proposal": proposal, "risk_mode": decision.mode.value, "reason": decision.reason},
            "next_steps": list(skill.steps), "risk": decision.mode.value, "tools_used": [], "tool_evidence": [],
        }

    results: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    for tool_name in skill.tools:
        definition = tool_definition(tool_name)
        if definition is None or definition.risk != "auto":
            continue
        tool_query = text
        if tool_name == "metric_catalog" and not re.search(r"(?:#|指标(?:编号)?\s*)\d+", tool_query):
            tool_query = "#1"
        try:
            result = invoke_tool(tool_name, query=tool_query)
        except Exception as exc:
            result = {"status": "failed", "message": f"工具调用失败：{type(exc).__name__}"}
        results[tool_name] = result
        records = result.get("rows") or result.get("metrics") or result.get("approvals") or result.get("documents") or result.get("events") or []
        count = len(records) if isinstance(records, list) else 0
        evidence.append({
            "tool": tool_name,
            "reason": f"流程 {skill.name} 按定义步骤调用该只读工具",
            "status": result.get("status", "unknown"),
            "result_count": count,
            "sample": records[:3] if isinstance(records, list) else [],
            "summary": f"{tool_name} 返回 {count} 条记录",
            "selection_mode": "skill_workflow",
            "round": 1,
        })

    def rows(tool_name: str) -> list[dict[str, Any]]:
        value = results.get(tool_name, {}).get("rows", [])
        return value if isinstance(value, list) else []

    data: dict[str, Any]
    summary: str
    status = "completed"
    next_steps = list(skill.steps)
    risk = skill.risk

    if name == "incident_triage":
        severity = (re.search(r"P[123]", text.upper()) or ["P1"])[0]
        region = re.search(r"华东|华南|华北", text)
        assets = {str(row.get("id")): row for row in rows("asset_lookup")}
        alerts = [dict(row) for row in rows("alert_query") if str(row.get("severity", "")).upper() == severity]
        if region:
            alerts = [row for row in alerts if row.get("region") == region[0]]
        for alert in alerts:
            asset = assets.get(str(alert.get("asset_id", "")), {})
            alert["asset_name"] = alert.get("asset_name") or asset.get("name")
            alert["region"] = alert.get("region") or asset.get("region")
            alert["asset_status"] = alert.get("asset_status") or asset.get("status")
        knowledge_sources = results.get("knowledge_search", {}).get("documents", [])
        data = {
            "alerts": alerts, "severity": severity, "region": region[0] if region else None,
            "knowledge_sources": knowledge_sources[:5] if isinstance(knowledge_sources, list) else [],
        }
        summary = f"识别到 {len(alerts)} 条符合条件的未关闭 {severity} 告警。"
    elif name == "metric_diagnosis":
        trend = results.get("metric_catalog", {}).get("trend")
        knowledge_sources = results.get("knowledge_search", {}).get("documents", [])
        points = (trend or {}).get("points", [])
        values = [float(point["value"]) for point in points if point.get("value") is not None]
        if not trend or not values:
            status, summary, next_steps = "not_found", "未找到该指标或没有可用样本。", []
            data = {
                "knowledge_sources": knowledge_sources[:5] if isinstance(knowledge_sources, list) else [],
            }
        else:
            average, latest = sum(values) / len(values), values[-1]
            deviation = round((latest - average) / average * 100, 1) if average else 0
            direction = "高于" if deviation > 15 else "低于" if deviation < -15 else "接近"
            summary = f"{trend['name']} 当前值 {latest}{trend['unit']}，{direction} 24 小时均值 {average:.2f}{trend['unit']}（偏差 {deviation}%）。"
            data = {
                "metric": trend["name"], "latest": latest, "average": round(average, 2),
                "minimum": min(values), "maximum": max(values), "points": len(values),
                "knowledge_sources": knowledge_sources[:5] if isinstance(knowledge_sources, list) else [],
            }
    elif name == "ticket_handoff":
        tickets, unassigned = rows("ticket_query"), rows("unassigned_tickets")
        high_count = sum(row.get("priority") == "high" for row in tickets)
        knowledge_sources = results.get("knowledge_search", {}).get("documents", [])
        data = {
            "tickets": tickets, "unassigned": unassigned,
            "knowledge_sources": knowledge_sources[:5] if isinstance(knowledge_sources, list) else [],
        }
        summary = f"当前有 {len(tickets)} 个未关闭工单，其中高优 {high_count} 个、未分配 {len(unassigned)} 个。"
    elif name == "oncall_briefing":
        alerts, tickets, work_orders = rows("alert_query"), rows("ticket_query"), rows("work_order_query")
        approvals = [row for row in results.get("approval_queue", {}).get("approvals", []) if row.get("status") == "pending"]
        p1_count = sum(row.get("severity") == "P1" for row in alerts)
        knowledge_sources = results.get("knowledge_search", {}).get("documents", [])
        data = {
            "alerts": alerts, "tickets": tickets, "work_orders": work_orders,
            "pending_approvals": approvals,
            "knowledge_sources": knowledge_sources[:5] if isinstance(knowledge_sources, list) else [],
        }
        summary = f"值班简报：{len(alerts)} 条未关闭告警（P1 {p1_count} 条）、{len(tickets)} 个工单、{len(work_orders)} 个待执行作业、{len(approvals)} 张待审批单。"
    elif name == "capacity_review":
        assets = rows("asset_lookup")
        knowledge_sources = results.get("knowledge_search", {}).get("documents", [])
        trend = results.get("metric_catalog", {}).get("trend")
        points = (trend or {}).get("points", [])
        values = [float(point["value"]) for point in points if point.get("value") is not None]
        latest = values[-1] if values else None
        average = round(sum(values) / len(values), 2) if values else None
        data = {
            "non_online_assets": assets, "metric": trend.get("name") if trend else None,
            "latest": latest, "average": average, "peak": max(values) if values else None,
            "recent_samples": rows("latest_metric_samples"),
            "knowledge_sources": knowledge_sources[:5] if isinstance(knowledge_sources, list) else [],
        }
        summary = f"容量巡检发现 {len(assets)} 台非在线资产；{data['metric'] or '指标'}当前值 {latest if latest is not None else '—'}，24 小时均值 {average if average is not None else '—'}。"
    else:
        data = {tool_name: result for tool_name, result in results.items()}
        summary = f"已完成运维流程 {skill.name}。"

    return {
        "skill": skill.name, "status": status, "summary": summary, "data": data,
        "next_steps": next_steps, "risk": risk, "tools_used": [item["tool"] for item in evidence],
        "tool_evidence": evidence,
    }
