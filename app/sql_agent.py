"""SQL Agent 核心组件：元数据召回、SQL 生成、审查、修复和风险判断。

本模块是 SQL Agent 的核心链路组件集合，按执行顺序依次为：
1. MetadataRetriever：元数据召回，限制后续模型可见表范围；
2. RuleBasedSqlWriter：离线确定性 SQL 生成器，模型不可用时兜底；
3. SqlReviewer：只读 SQL 静态审查器，阻断写入、DDL、多语句和未授权表；
4. SqlFixer：保守 SQL 修复器，只处理可证明安全的只读问题；
5. RiskAssessor：SQL 风险分级器，决定自动执行、审批或阻断。

设计原则：
- 任何组件都不信任上游输出，全部在后端做二次校验；
- 模型和规则生成器只能“提出候选 SQL”，最终是否执行由 Reviewer 和 RiskAssessor 决定；
- 所有判定都输出可解释原因，便于审计和用户理解；
- MySQL 配置后以实时 introspection 结果替代静态演示 schema。

安全边界：
- 只读：Reviewer 拒绝一切写入、DDL 和 SQLite 维护关键字；
- 白名单：只允许召回阶段给出的表白名单，其他表一律拒绝；
- 单语句：只允许单条 SQL，阻断多语句拼接；
- 自动 LIMIT：对没有 LIMIT 的只读查询追加默认 LIMIT；
- 风险分级：写操作进入审批，高危指令直接阻断。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

import re  # 正则匹配 SQL 关键字、表名和指标编号
from functools import lru_cache  # 缓存 active_schema 结果，避免重复 introspection
from dataclasses import dataclass  # 声明 RiskDecision 数据结构

from app.models import CandidateTable, ExecutionMode, GeneratedSql, ReviewResult  # 各阶段使用的数据模型


# 本地演示库的可召回元数据。每张表都明确列、业务别名和用途，避免模型看到未知字段。
# 配置 MySQL 后 active_schema 会以实时 introspection 结果替代这份静态目录。
SCHEMA: dict[str, dict[str, object]] = {
    "assets": {
        "columns": ["id", "name", "region", "status", "owner", "updated_at"],
        # 业务别名用于中文问题召回，例如“设备”“离线”等用户口语。
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
    """Uses demo metadata by default; fetches actual schema when MySQL is configured.

    返回当前生效的 schema 元数据。

    - 未配置 MySQL：返回内置演示 SCHEMA；
    - 配置 MySQL：调用 introspect_schema 读取真实表结构，替代静态目录。

    使用 lru_cache(maxsize=1) 缓存结果，避免每次召回都查询 information_schema。
    注意：MySQL schema 变更后需要重启服务或显式清缓存才能生效。
    """
    from app.config import settings

    if settings.mysql_enabled:
        # 延迟导入，避免未启用 MySQL 时引入 pymysql 依赖。
        from app.mysql_adapter import introspect_schema
        return introspect_schema()
    return SCHEMA


class MetadataRetriever:
    """元数据召回器。

    通过表名/字段名、中文业务别名和表描述三类信号计算轻量分数，输出候选表及命中依据。
    候选结果既用于限制 SQL Writer 的上下文，也用于向用户解释查询为什么命中了这些表。

    使用方式：
        retriever = MetadataRetriever()
        candidates = retriever.retrieve("查询华东区离线设备", top_k=3)

    安全边界：
    - 只从 active_schema() 中召回表，模型无法看到未注册的表；
    - 召回结果作为后续 Reviewer 的白名单来源；
    - 打分和命中依据全部可解释，便于审计和用户理解。
    """

    def retrieve(self, question: str, top_k: int = 3) -> list[CandidateTable]:
        """根据问题召回候选表。

        参数：
        - question：用户自然语言问题；
        - top_k：返回候选表数量上限，默认 3。

        返回：
        - list[CandidateTable]：按得分倒序排列的候选表，每项包含：
          * name：表名；
          * score：召回得分；
          * matched：命中依据列表（table_name / column_name / business_alias / semantic_hint）；
          * columns：该表的列清单。

        打分规则：
        - 表名命中：+4（最直接信号）；
        - 字段名命中：+2（可能暗示用户关注某列）；
        - 业务别名命中：每个 +3（中文口语强信号）；
        - 描述字符重合：≥2 个字符重合时 +1（弱语义信号）。

        设计理由：
        - 表名和业务别名权重高，字段名和描述权重低，避免噪声召回；
        - 只返回 score > 0 的表，避免无意义候选；
        - 得分可解释，便于前端展示“为什么命中这些表”。
        """
        # 使用表名、字段名、业务别名和描述做轻量召回，限制后续模型可见表范围。
        normalized = question.lower()
        candidates: list[CandidateTable] = []

        for table, meta in active_schema().items():
            # 累计得分和命中依据。
            matched: list[str] = []
            score = 0

            # 表名直接命中：最强信号。
            if table in normalized:
                score += 4
                matched.append("table_name")

            # 字段名命中：用户可能直接提到列名。
            if any(column.lower() in normalized for column in meta["columns"]):
                score += 2
                matched.append("column_name")

            # 业务别名命中：中文口语强信号，每个别名累加。
            aliases = [alias for alias in meta["aliases"] if alias.lower() in normalized]
            if aliases:
                score += len(aliases) * 3
                matched.append("business_alias:" + ",".join(aliases))

            # 描述字符重合：弱语义提示，命中 ≥2 个字符才计分，避免偶发命中。
            description_tokens = set(str(meta["description"]).lower())
            semantic_hits = sum(1 for char in set(normalized) if char in description_tokens)
            if semantic_hits >= 2:
                score += 1
                matched.append("semantic_hint")

            # 有得分才作为候选，避免空召回。
            if score:
                candidates.append(CandidateTable(table, score, matched, list(meta["columns"])))

        # 按得分倒序，最多返回 top_k 个。
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k]


class RuleBasedSqlWriter:
    """离线确定性 SQL Writer。

    它只覆盖演示项目中已知的查询意图，并且输出仍必须经过 SqlReviewer 和 RiskAssessor；
    它的作用是模型服务不可用时保持系统可演示，而不是绕过安全链路。

    支持的意图：
    - 指标趋势查询（"指标 #123"）；
    - 指标目录查询；
    - 各区域离线设备统计；
    - P1 告警关联设备；
    - 告警列表；
    - 离线设备列表；
    - 工单列表；
    - 写操作（删除/更新/修改）——返回示例 DELETE，交由后续流程拦截。

    安全边界：
    - 生成的 SQL 仍走 Reviewer 和 RiskAssessor，不绕过安全链路；
    - 只使用白名单表和固定列；
    - 对 SOP/解释类问题返回 None，交由 RAG 路径处理。
    """

    def generate(
        self,
        question: str,
        tables: list[CandidateTable],
        tool_context: list[dict] | None = None,
    ) -> GeneratedSql | None:
        """根据问题生成候选 SQL 或返回 None。

        参数：
        - question：用户问题；
        - tables：召回阶段给出的候选表（本实现未使用，保留接口一致性）；
        - tool_context：已执行的只读工具证据（本实现未使用）。

        返回：
        - GeneratedSql：命中了已知意图时；
        - None：问题属于 SOP/解释类，或无法匹配任何意图。

        安全边界：
        - 离线规则只生成项目白名单表上的示例 SQL，不能替代 Reviewer；
        - 写操作意图返回示例 DELETE，交由后续审批流程处理。
        """
        # 离线规则只生成项目白名单表上的示例 SQL，不能替代 Reviewer。
        q = question.lower()

        # Knowledge-seeking questions should not be forced into a database query merely
        # because they mention a severity such as P1. They are handled by the RAG route.
        # 知识类问题（“如何处理”“SOP”“排障步骤”等）走 RAG，不生成 SQL。
        if any(phrase in q for phrase in ["如何处理", "怎么处理", "处理流程", "sop", "排障步骤", "规范"]):
            return None

        # 明确写操作意图优先于 SQL 生成；避免把“最近更新的工单”等只读问题误判为写入。
        explicit_assignment = re.search(
            r"(?:将|把).{0,35}(?:标记为|设为|设成|设置为|改为|改成|更新为|设置成)", q,
        )
        direct_status_change = re.search(
            r"(?:更新|修改|更改).{0,18}(?:状态|字段|数据|为|成)|"
            r"^(?:请|帮我|帮忙)?(?:关闭|完成|启用|禁用)(?:工单|告警|设备|资产)|"
            r"(?:将|把).{0,35}(?:关闭|完成|启用|禁用)", q,
        )
        if "删除" in q or "清空" in q or explicit_assignment or direct_status_change:
            if "工单" in q:
                ticket_id = re.search(r"工单\s*#?\s*(\d+)", question)
                where = f"id = {ticket_id.group(1)}" if ticket_id else "status != 'closed'"
                if "删除" in q or "清空" in q:
                    return GeneratedSql(
                        f"DELETE FROM tickets WHERE {where};",
                        "write_request", 0.9, ["tickets"],
                    )
                status = "closed" if any(word in q for word in ["关闭", "完成"]) else "in_progress"
                return GeneratedSql(
                    f"UPDATE tickets SET status = '{status}' WHERE {where};",
                    "write_request", 0.9, ["tickets"],
                )
            if "设备" in q or "资产" in q:
                if "删除" in q or "清空" in q:
                    return GeneratedSql(
                        "DELETE FROM assets WHERE status != 'online';",
                        "write_request", 0.85, ["assets"],
                    )
                status = "offline" if any(word in q for word in ["离线", "下线"]) else "maintenance"
                return GeneratedSql(
                    f"UPDATE assets SET status = '{status}' WHERE status != '{status}';",
                    "write_request", 0.85, ["assets"],
                )
            if "告警" in q or "报警" in q:
                if "删除" in q or "清空" in q:
                    return GeneratedSql(
                        "DELETE FROM alerts WHERE status = 'closed';",
                        "write_request", 0.85, ["alerts"],
                    )
                return GeneratedSql(
                    "UPDATE alerts SET status = 'closed' WHERE status != 'closed';",
                    "write_request", 0.85, ["alerts"],
                )
            return GeneratedSql(
                "DELETE FROM alerts WHERE status = 'closed';",
                "write_request", 0.75, ["alerts"],
            )

        # 指标趋势查询：用户输入 "指标 #123" 时生成趋势 SQL。
        metric_id_match = re.search(r"#\s*(\d{1,3})", question)
        if "指标" in q and metric_id_match:
            metric_id = int(metric_id_match.group(1))
            return GeneratedSql(
                "SELECT d.id, d.name, d.unit, s.observed_at, s.value FROM metric_definitions d "
                "JOIN metric_samples s ON d.id = s.metric_id WHERE d.id = " + str(metric_id) +
                " ORDER BY s.observed_at DESC LIMIT 24;",
                "metric_trend", 0.9, ["metric_definitions", "metric_samples"],
            )

        # 最近指标样本查询，先于通用指标目录意图识别。
        if "指标" in q and any(word in q for word in ["最近", "最新", "样本", "采集", "趋势"]):
            return GeneratedSql(
                "SELECT md.id AS metric_id, md.name, md.category, md.unit, ms.observed_at, ms.value "
                "FROM metric_samples AS ms JOIN metric_definitions AS md ON md.id = ms.metric_id "
                "ORDER BY ms.observed_at DESC LIMIT 20;",
                "latest_metric_samples", 0.88, ["metric_definitions", "metric_samples"],
            )

        # 指标目录查询。
        if "指标目录" in q or ("指标" in q and "查询" in q):
            return GeneratedSql(
                "SELECT id, name, category, unit, asset_scope FROM metric_definitions ORDER BY id LIMIT 30;",
                "metric_catalog", 0.84, ["metric_definitions"],
            )

        # 设备状态/区域汇总。
        if any(word in q for word in ["设备", "资产"]) and any(word in q for word in ["统计", "汇总", "分布"]) and any(word in q for word in ["状态", "区域", "在线", "离线"]):
            return GeneratedSql(
                "SELECT region, status, COUNT(*) AS asset_count FROM assets "
                "GROUP BY region, status ORDER BY region, status LIMIT 100;",
                "asset_status_summary", 0.88, ["assets"],
            )

        # 各区域离线设备统计：必须在通用离线查询之前判断，避免被后者覆盖。
        if "各区域" in q and "离线" in q:
            return GeneratedSql(
                "SELECT region, COUNT(*) AS offline_count FROM assets WHERE status = 'offline' "
                "GROUP BY region ORDER BY offline_count DESC LIMIT 20;",
                "offline_assets_by_region", 0.9, ["assets"],
            )

        # P1 告警关联设备查询：需要在通用告警查询之前判断。
        if ("p1" in q or "告警" in q) and any(word in q for word in ["设备", "关联", "影响"]):
            where = " WHERE a.severity = 'P1'" if "p1" in q else ""
            return GeneratedSql(
                "SELECT a.id AS alert_id, a.severity, a.title AS alert_title, a.status AS alert_status, "
                "s.name AS asset_name, s.region, s.status AS asset_status "
                "FROM alerts a JOIN assets s ON a.asset_id = s.id" + where +
                " ORDER BY a.created_at DESC LIMIT 20;",
                "alerts_with_assets", 0.89, ["alerts", "assets"],
            )

        # 通用告警列表查询。
        if "告警" in q or "p1" in q or "报警" in q:
            where = []
            if "p1" in q:
                where.append("severity = 'P1'")
            if "未关闭" in q or "未处理" in q or "open" in q:
                where.append("status != 'closed'")
            clause = " WHERE " + " AND ".join(where) if where else ""
            return GeneratedSql(
                f"SELECT id, asset_id, severity, title, status, created_at FROM alerts{clause} "
                "ORDER BY created_at DESC LIMIT 20;",
                "list_alerts", 0.91, ["alerts"],
            )

        # 离线设备列表：支持按华东/华北/华南过滤。
        if "离线" in q and any(word in q for word in ["设备", "网关", "资产", "华东", "华北", "华南"]):
            where = ["status = 'offline'"]
            for region in ["华东", "华北", "华南"]:
                if region in question:
                    where.append(f"region = '{region}'")
            return GeneratedSql(
                "SELECT id, name, region, status, owner, updated_at FROM assets WHERE " +
                " AND ".join(where) + " ORDER BY updated_at DESC LIMIT 20;",
                "list_offline_assets", 0.92, ["assets"],
            )

        # 工单列表查询：支持未关闭和高优过滤。
        if "工单" in q:
            where = []
            if "未关闭" in q or "未完成" in q:
                where.append("status != 'closed'")
            if "高优" in q or "高优先级" in q:
                where.append("priority = 'high'")
            clause = " WHERE " + " AND ".join(where) if where else ""
            return GeneratedSql(
                f"SELECT id, priority, title, status, assignee, created_at FROM tickets{clause} "
                "ORDER BY created_at DESC LIMIT 20;",
                "list_tickets", 0.88, ["tickets"],
            )

        # 运维作业列表，结构与 work_order_query 工具保持一致。
        if "作业" in q or "待执行" in q:
            where = " WHERE status != 'completed'" if any(word in q for word in ["待执行", "未完成", "未关闭"]) else ""
            return GeneratedSql(
                "SELECT id, asset_id, action, status, created_at FROM work_orders" + where +
                " ORDER BY created_at DESC LIMIT 20;",
                "list_work_orders", 0.86, ["work_orders"],
            )

        # 未匹配任何已知意图。
        return None


class SqlReviewer:
    """只读 SQL 静态审查器。

    forbidden 检查写入和 DDL 关键字，table_re 提取 FROM/JOIN 表名；审查还会拒绝多语句、
    未授权表并补充默认 LIMIT。通过审查只说明“可以进入风险判断”，不代表可以直接写库。

    审查顺序：
    1. 必须以 SELECT 开头；
    2. 不得包含写入/DDL/维护关键字；
    3. 必须是单条 SQL；
    4. 必须能识别出 FROM/JOIN 表；
    5. 所有表必须在 allowed_tables 白名单内；
    6. 无 LIMIT 时自动追加默认 LIMIT 100。

    安全边界：
    - 任何一步失败都返回 ReviewResult(False, [原因], sql)；
    - 只补充 LIMIT，不改写其他 SQL 内容；
    - 不使用正则做语义分析，只做关键字和表名级别的静态检查。
    """

    # 写入、DDL、外部挂载和 SQLite 维护关键字全部进入阻断或审批链路。
    # 注意：此处只用于阻断，不是风险分级；风险分级由 RiskAssessor 负责。
    forbidden = re.compile(
        r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|replace)\b",
        re.I,
    )

    # 只提取 FROM/JOIN 后的简单表名，用于和 allowed_tables 做白名单比较。
    # 不处理子查询、CTE 等复杂结构，复杂 SQL 会在白名单检查时被拒绝。
    table_re = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)

    def review(self, sql: str, allowed_tables: list[str]) -> ReviewResult:
        """审查 SQL 并返回审查结果。

        参数：
        - sql：待审查的 SQL；
        - allowed_tables：允许访问的表白名单。

        返回：
        - ReviewResult：包含 passed、issues、normalized_sql 三个字段。
          * passed=True 时，normalized_sql 可能已被追加 LIMIT；
          * passed=False 时，issues 列出所有阻断原因。

        安全边界：
        - 通过审查不代表可以写库，仍需经过 RiskAssessor；
        - 只做静态检查，不执行 SQL。
        """
        # 先拒绝写入/DDL/多语句，再核对表白名单，最后补充 LIMIT。
        # 把 SQL 规范化为单空格分隔，便于后续匹配。
        normalized = " ".join(sql.strip().split())

        # 1. 必须以 SELECT 开头。
        if not normalized.lower().startswith("select"):
            return ReviewResult(False, ["只允许 SELECT 查询"], normalized)

        # 2. 不得包含写入/DDL/维护关键字。
        if self.forbidden.search(normalized):
            return ReviewResult(False, ["检测到写入或 DDL 关键字"], normalized)

        # 3. 必须是单条 SQL：不允许出现多个分号，也不允许分号出现在中间。
        if normalized.count(";") > 1 or (";" in normalized and not normalized.endswith(";")):
            return ReviewResult(False, ["只允许单条 SQL"], normalized)

        # 4. 提取 FROM/JOIN 表名。
        used_tables = self.table_re.findall(normalized)
        if not used_tables:
            return ReviewResult(False, ["未识别到 FROM/JOIN 表"], normalized)

        # 5. 所有表必须在白名单内。
        unknown = sorted(set(used_tables) - set(allowed_tables))
        if unknown:
            return ReviewResult(False, [f"存在未授权表: {', '.join(unknown)}"], normalized)

        # 6. 无 LIMIT 时追加默认 LIMIT 100；先去掉末尾分号再追加，避免语法错误。
        if " limit " not in normalized.lower():
            normalized = normalized.rstrip(";") + " LIMIT 100;"

        # 通过审查。
        return ReviewResult(True, [], normalized)


class SqlFixer:
    """保守 SQL 修复器。

    只处理多余分号和缺失 LIMIT 等可证明安全的只读问题；涉及未知表、未识别表或非 SELECT 时
    返回 None，让工作流进入修复失败或风险处理分支，而不是擅自改写用户意图。

    修复范围（仅限可证明安全的只读问题）：
    - 多条语句：保留第一条；
    - 缺少 LIMIT：追加默认 LIMIT 100。

    不修复：
    - 非 SELECT：返回 None；
    - 未授权表或未识别表：返回 None，交由 Reviewer 重新判断；
    - 其他语义问题：返回 None，不擅自改写。
    """

    def fix(self, sql: str, issues: list[str], allowed_tables: list[str]) -> str | None:
        """尝试修复 SQL 并返回修复后的 SQL。

        参数：
        - sql：待修复的 SQL；
        - issues：Reviewer 给出的问题列表；
        - allowed_tables：允许访问的表白名单（本实现未使用，保留接口一致性）。

        返回：
        - str：修复后的 SQL，末尾保证有分号；
        - None：无法安全修复时，交由工作流进入其他分支。

        安全边界：
        - 只做保守修复，不改写 SQL 语义；
        - 未授权表或未识别表的问题不修复，避免绕过白名单。
        """
        normalized = " ".join(sql.strip().split())

        # 非 SELECT 不修复。
        if not normalized.lower().startswith("select"):
            return None

        # 未授权表或未识别表的问题不修复，避免绕过白名单。
        if any("未授权表" in issue or "未识别到" in issue for issue in issues):
            return None

        # A model can accidentally emit multiple statements. Keep only the first
        # read statement and let the Reviewer revalidate it in the next iteration.
        # 多语句时只保留第一条，交由 Reviewer 重新审查。
        if normalized.count(";") > 1:
            normalized = normalized.split(";", 1)[0].strip()

        # 缺少 LIMIT 时追加默认 LIMIT 100。
        if " limit " not in normalized.lower():
            normalized = normalized.rstrip(";") + " LIMIT 100"

        # 保证末尾有分号。
        return normalized + ("" if normalized.endswith(";") else ";")


@dataclass(slots=True)
class RiskDecision:
    """风险评估节点的结果。

    字段：
    - mode：最终执行模式（AUTO / MANUAL / BLOCKED）；
    - reason：面向用户和审计中心的风险解释。

    设计说明：
    - slots=True：减少内存占用并防止动态属性；
    - 与 ExecutionMode 枚举配合，便于工作流按模式分支。
    """

    # 最终执行模式，决定自动运行、创建审批或直接阻断。
    mode: ExecutionMode

    # 面向用户和审计中心的风险解释。
    reason: str


class RiskAssessor:
    """SQL 风险分级器，始终在后端执行。

    分级规则：
    - 包含 DROP/ALTER/ATTACH/PRAGMA/VACUUM：BLOCKED，直接阻断；
    - 包含 INSERT/UPDATE/DELETE/REPLACE：MANUAL，需要人工审批；
    - 其他（只读 SELECT）：AUTO，可自动执行。

    安全边界：
    - 分级始终在后端执行，不信任前端或模型给出的风险等级；
    - BLOCKED 表示不可在 Agent 中执行，比 MANUAL 更严格；
    - 该分级在 SqlReviewer 之后执行，此时 SQL 已通过只读和白名单检查。
    """

    def assess(self, sql: str) -> RiskDecision:
        """评估 SQL 的风险等级。

        参数：
        - sql：已通过 SqlReviewer 审查的 SQL。

        返回：
        - RiskDecision：包含 mode 和 reason。

        说明：
        - 此处仍会重新匹配写关键字，因为 Reviewer 通过不代表没有写操作
          （例如 Reviewer 允许 DELETE 进入审批流程，而不是直接阻断）。
        """
        # 风险分级决定自动执行、进入审批，还是直接阻断。
        lowered = sql.lower()

        # 高危数据库指令：直接阻断，不允许进入审批流程。
        if re.search(r"\b(drop|alter|attach|pragma|vacuum)\b", lowered):
            return RiskDecision(ExecutionMode.BLOCKED, "包含不可在 Agent 中执行的高危数据库指令")

        # 数据写操作：进入人工审批流程。
        if re.search(r"\b(insert|update|delete|replace)\b", lowered):
            return RiskDecision(ExecutionMode.MANUAL, "数据写操作需要人工审批")

        # 只读 SELECT：自动执行。
        return RiskDecision(ExecutionMode.AUTO, "只读 SELECT，可自动执行")
