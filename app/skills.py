"""Skills 注册与运行层：把重复运维经验封装为可审计的只读能力。

本模块是 OpsPilot 的 Skill 体系，核心设计目标：
1. 把常见运维经验（如 P1 事件分诊、值班简报、容量巡检）封装为可复用的 SOP；
2. 每个 Skill 定义在 skills/*/SKILL.md，前端可浏览，后端可路由；
3. 只读 Skill 走参数化查询或白名单只读函数，绝不拥有任意写库权限；
4. 高风险 Skill（如变更评审）只输出风险评估结果，实际变更仍需走审批；
5. 每次运行都由上层 API 写审计，保证可追溯、可复现。

安全边界：
- Skill 本身不获得任意 SQL 执行权，运行时按名称路由到固定实现；
- 所有查询均使用参数化或固定 SQL，避免 SQL 注入；
- 写操作不在 Skill 中执行，只通过审批中心走受控流程；
- 规范型 Skill（runnable=False）只作为文档，不直接运行。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

from dataclasses import dataclass  # 声明 Skill 数据结构
from pathlib import Path  # 跨平台路径处理，定位 skills 目录
import re  # 用于从用户输入中提取 P1/P2/P3、区域、指标编号等
from statistics import mean  # 计算指标均值，用于诊断和容量评估
from typing import Any  # 宽松字典类型标注

from app.database import execute_readonly, list_approvals, metric_trend  # 只读数据库入口
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


class SkillRegistry:
    """Skill 文件系统注册表。

    每次读取目录而不是把内容硬编码在 Python 中，因此新增或编辑 SOP 后可以直接刷新；
    注册表只负责发现和解析，真正执行仍由 ``run_skill`` 的固定白名单路由完成。

    使用方式：
        registry = SkillRegistry(Path("skills"))
        skills = registry.load()           # 加载全部 Skill
        skill = registry.get("incident_triage")  # 按名称获取
    """

    def __init__(self, root: Path):
        # skills 根目录，例如项目根目录下的 skills/。
        self.root = root

    def load(self) -> list[Skill]:
        """加载所有 skills/*/SKILL.md，解析为 Skill 列表。

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
                # runnable 字段用字符串 "true"/"false" 表示，需转换。
                metadata.get("runnable", "false").lower() == "true",
            ))
        return skills

    def get(self, name: str) -> Skill | None:
        """按名称查找 Skill，不存在时返回 None。"""
        return next((skill for skill in self.load() if skill.name == name), None)

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
    """Deterministic, auditable skill runtimes. Skills never mutate business data.

    按 Skill 名称路由到固定实现，返回统一结构的结果。

    参数：
    - name：Skill 名称，必须与注册表中的 name 一致；
    - user_input：用户在运行 Skill 时提供的补充上下文。

    返回：
    - dict：统一结构，包含：
      * skill：Skill 名称；
      * status：运行状态（completed / not_found / reviewed / not_runnable）；
      * summary：一句话结论，便于前端展示；
      * data：结构化数据，供前端表格或详情使用；
      * next_steps：推荐的下一步动作；
      * risk：本次运行的风险等级。

    安全边界：
    - 运行时按 Skill 名称路由到固定实现，Skill 不拥有任意 SQL 或写库权限；
    - 所有 SQL 均为固定查询或参数化查询；
    - 变更类 Skill 只输出风险评审，不执行写操作。

    支持的 Skill：
    - incident_triage：P1/P2/P3 事件分诊；
    - metric_diagnosis：指标诊断；
    - ticket_handoff：工单交接；
    - oncall_briefing：值班简报；
    - capacity_review：容量巡检；
    - change_review：变更评审。
    """
    # 运行时按 Skill 名称路由到固定实现，Skill 不拥有任意 SQL 或写库权限。
    text = user_input.strip()

    # ---------- Skill: 事件分诊 ----------
    if name == "incident_triage":
        # 从输入中提取 P1/P2/P3，默认 P1；大小写不敏感。
        severity = (re.search(r"P[123]", text.upper()) or ["P1"])[0]

        # 从输入中提取区域（华东/华南/华北），可选。
        region_match = re.search(r"华东|华南|华北", text)

        # 构造参数化查询条件：固定过滤未关闭告警，可选叠加区域。
        clauses, params = ["a.severity = ?", "a.status != 'closed'"], [severity]
        if region_match:
            clauses.append("s.region = ?")
            params.append(region_match[0])

        # 固定 SQL 模板，使用 ? 占位符防止注入。
        sql = (
            "SELECT a.id, a.severity, a.title, a.status, a.created_at, s.name AS asset_name, s.region "
            "FROM alerts a JOIN assets s ON a.asset_id = s.id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY a.created_at DESC LIMIT 20"
        )

        # 通过参数化辅助函数执行查询。
        rows = execute_readonly_with_params(sql, params)

        return {
            "skill": name,
            "status": "completed",
            "summary": f"识别到 {len(rows)} 条未关闭 {severity} 告警。先确认影响范围，再检查网络、心跳、供电和网关日志。",
            "data": rows,
            "next_steps": [
                "5 分钟内确认告警影响范围",
                "核查设备网络、心跳和最近变更",
                "未恢复则创建高优工单并升级值班负责人",
            ],
            "risk": "auto",
        }

    # ---------- Skill: 指标诊断 ----------
    if name == "metric_diagnosis":
        # 从输入中提取指标编号，支持 "#123" 或 "123" 格式，默认 1。
        metric_id = int((re.search(r"#?(\d+)", text) or ["", "1"])[1])

        # 查询指标趋势（默认最近 24 个点）。
        trend = metric_trend(metric_id)
        if trend is None:
            return {
                "skill": name,
                "status": "not_found",
                "summary": f"未找到指标 #{metric_id}。请在指标中心选择有效指标编号。",
                "data": [],
                "next_steps": [],
                "risk": "auto",
            }

        # 计算统计量：均值、最新值、偏差百分比。
        values = [point["value"] for point in trend["points"]]
        average, latest = mean(values), values[-1]
        deviation = round((latest - average) / average * 100, 1) if average else 0

        # 根据偏差判断当前值相对均值的方向。
        assessment = "高于" if deviation > 15 else "低于" if deviation < -15 else "接近"

        return {
            "skill": name,
            "status": "completed",
            "summary": (
                f"{trend['name']} 当前值 {latest}{trend['unit']}，"
                f"{assessment} 24 小时均值 {average:.2f}{trend['unit']}（偏差 {deviation}%）。"
            ),
            "data": {
                "metric": trend["name"],
                "latest": latest,
                "average": round(average, 2),
                "minimum": min(values),
                "maximum": max(values),
                "points": len(values),
            },
            "next_steps": [
                "确认异常时间点是否对应发布或流量变化",
                "查看关联设备和同类指标",
                "超过阈值时创建告警并记录处理结论",
            ],
            "risk": "auto",
        }

    # ---------- Skill: 工单交接 ----------
    if name == "ticket_handoff":
        # 查询未关闭工单，按优先级高优在前、创建时间早的在前。
        rows = execute_readonly(
            "SELECT id, priority, title, status, assignee, created_at FROM tickets "
            "WHERE status != 'closed' "
            "ORDER BY CASE priority WHEN 'high' THEN 1 ELSE 2 END, created_at ASC LIMIT 20"
        )
        high_count = sum(row["priority"] == "high" for row in rows)
        return {
            "skill": name,
            "status": "completed",
            "summary": f"当前有 {len(rows)} 个未关闭工单，其中高优 {high_count} 个。交接时应优先处理高优和无人认领工单。",
            "data": rows,
            "next_steps": [
                "逐单确认负责人、当前进展和阻塞项",
                "高优工单补齐最近动作与下次更新时间",
                "交接后在工单中记录接手人和验证计划",
            ],
            "risk": "auto",
        }

    # ---------- Skill: 值班简报 ----------
    if name == "oncall_briefing":
        # 查询未关闭告警，按严重级别和创建时间排序。
        alerts = execute_readonly(
            "SELECT severity, title, status, created_at FROM alerts "
            "WHERE status != 'closed' "
            "ORDER BY CASE severity WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, created_at DESC LIMIT 20"
        )

        # 查询未关闭工单，按优先级排序。
        tickets = execute_readonly(
            "SELECT priority, title, status, assignee, created_at FROM tickets "
            "WHERE status != 'closed' "
            "ORDER BY CASE priority WHEN 'high' THEN 1 ELSE 2 END, created_at ASC LIMIT 20"
        )

        # 查询未完成作业单。
        work_orders = execute_readonly(
            "SELECT action, status, created_at FROM work_orders "
            "WHERE status != 'completed' ORDER BY created_at ASC LIMIT 20"
        )

        # 过滤出待审批单。
        approvals = [item for item in list_approvals(limit=20) if item["status"] == "pending"]

        # 统计 P1 数量。
        p1_count = sum(item["severity"] == "P1" for item in alerts)

        return {
            "skill": name,
            "status": "completed",
            "summary": (
                f"已生成值班简报：{len(alerts)} 条未关闭告警（P1 {p1_count} 条）、"
                f"{len(tickets)} 个未关闭工单、{len(work_orders)} 个待执行作业、"
                f"{len(approvals)} 张待审批单。"
            ),
            "data": {
                "alerts": alerts,
                "tickets": tickets,
                "work_orders": work_orders,
                "pending_approvals": approvals,
            },
            "next_steps": [
                "优先确认 P1 告警与其负责人",
                "补齐高优工单的下一次更新时间",
                "明确待审批变更的窗口、影响和审批人",
                "将本简报同步给下一值班人员",
            ],
            "risk": "auto",
        }

    # ---------- Skill: 容量巡检 ----------
    if name == "capacity_review":
        # 查询非在线资产（离线或维护中）。
        rows = execute_readonly(
            "SELECT id, name, region, status, updated_at FROM assets "
            "WHERE status != 'online' ORDER BY updated_at DESC LIMIT 20"
        )

        # 查询指标 #1（演示环境为 CPU 使用率）的趋势。
        trend = metric_trend(1)
        values = [point["value"] for point in (trend or {}).get("points", [])]
        latest = values[-1] if values else None
        average = round(mean(values), 2) if values else None

        return {
            "skill": name,
            "status": "completed",
            "summary": (
                f"容量巡检发现 {len(rows)} 台非在线资产；"
                f"CPU 指标当前值为 {latest if latest is not None else '—'}%，"
                f"24 小时均值为 {average if average is not None else '—'}%。"
            ),
            "data": {
                "non_online_assets": rows,
                "cpu_latest": latest,
                "cpu_average": average,
                "cpu_peak": max(values) if values else None,
            },
            "next_steps": [
                "核对离线或维护资产是否处于计划窗口",
                "观察 CPU 高点是否与流量或发布记录重合",
                "容量持续紧张时创建扩容评估工单",
                "记录本次巡检结论和下一复核时间",
            ],
            "risk": "auto",
        }

    # ---------- Skill: 变更评审 ----------
    if name == "change_review":
        # 优先使用用户输入作为变更提案；为空时用示例 DELETE 语句。
        proposal = text or "DELETE FROM alerts WHERE status = 'closed';"

        # 调用风险分级器判断变更风险等级。
        decision = RiskAssessor().assess(proposal)

        # 非 auto 模式视为高风险，必须走审批。
        high_risk = decision.mode.value != "auto"

        return {
            "skill": name,
            "status": "reviewed",
            "summary": (
                f"变更评审结果：{decision.reason}。"
                f"{'必须发起审批，不能直接执行。' if high_risk else '属于只读操作，仍需通过 SQL 审查。'}"
            ),
            "data": {
                "proposal": proposal,
                "risk_mode": decision.mode.value,
                "reason": decision.reason,
            },
            "next_steps": [
                "确认影响范围和回滚方案",
                "选择维护窗口并指定审批人",
                "通过审批中心创建可审计的变更记录",
            ],
            "risk": decision.mode.value,
        }

    # ---------- 未匹配到运行器 ----------
    # 规范型 Skill 或未注册的 Skill：返回提示，不执行任何操作。
    return {
        "skill": name,
        "status": "not_runnable",
        "summary": "该 Skill 是规范型能力，请在智能查询或对应业务页面中使用。",
        "data": [],
        "next_steps": [],
        "risk": "auto",
    }


def execute_readonly_with_params(sql: str, params: list[str]) -> list[dict[str, Any]]:
    """The demo's parameterized skill query; kept isolated from generated SQL execution.

    参数化只读查询辅助函数，专门用于 Skill 内部的固定 SQL 模板。

    参数：
    - sql：带 ? 占位符的固定 SQL；
    - params：与占位符顺序对应的参数列表。

    返回：
    - list[dict]：查询结果，每行为字典。

    安全边界：
    - 与模型生成的 SQL 执行路径隔离，避免 Skill 查询被注入；
    - 只做查询，不提供写操作入口；
    - 使用 with 管理连接，自动关闭。
    """
    # 延迟导入 connect，避免模块级循环依赖。
    from app.database import connect

    with connect() as conn:
        # 使用参数化执行，SQLite 会正确转义参数，防止注入。
        return [dict(row) for row in conn.execute(sql, params).fetchall()]