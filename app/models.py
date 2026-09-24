"""Agent 工作流共享的数据模型和枚举。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# 作用：说明类 ExecutionMode 的输入、输出与安全边界，避免调用方越过受控流程。
class ExecutionMode(str, Enum):
    """一次请求最终允许采取的风险等级。

    AUTO 表示只读操作可以在服务端审查通过后自动执行；MANUAL 表示必须先创建审批单，
    BLOCKED 表示即使用户有权限也不允许由 Agent 执行。该枚举同时被 SQL、工具和审批链路复用。
    """

    # 只读查询或知识检索，不会改变业务数据。
    AUTO = "auto"
    # 会改变状态的操作，必须进入人工审批。
    MANUAL = "manual"
    # 数据库维护、DDL 等高危动作直接拒绝。
    BLOCKED = "blocked"


@dataclass(slots=True)
# 作用：说明类 WorkflowEvent 的输入、输出与安全边界，避免调用方越过受控流程。
class WorkflowEvent:
    """工作流轨迹中的一个节点事件，既用于 SSE 推送也用于最终结果回放。"""

    # 节点名称，例如 Recall、Writer、Risk Guard 或 Tool Runner。
    stage: str
    # 面向用户展示的简短进度说明。
    message: str
    # 节点产生的结构化细节，例如 SQL、工具名、返回条数或耗时。
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
# 作用：说明类 CandidateTable 的输入、输出与安全边界，避免调用方越过受控流程。
class CandidateTable:
    """元数据召回得到的候选表，也是 SQL 生成器可见的表白名单。"""

    # 数据库表名。
    name: str
    # 由表名、字段名、业务别名和描述命中情况计算出的召回分数。
    score: int
    # 记录命中的依据，方便在工作流轨迹中解释为什么选择该表。
    matched_by: list[str]
    # 允许交给 SQL Writer 参考的字段列表，避免模型凭空编造列。
    columns: list[str]


@dataclass(slots=True)
# 作用：说明类 GeneratedSql 的输入、输出与安全边界，避免调用方越过受控流程。
class GeneratedSql:
    """SQL 生成节点的结构化输出，后续仍必须经过审查和风险分级。"""

    # 待 Reviewer 审查的原始 SQL 文本。
    sql: str
    # 对用户问题的意图分类，例如 list_alerts 或 metric_trend。
    intent: str
    # Writer 对自身生成结果的置信度，不等于安全审查结论。
    confidence: float
    # Writer 实际引用的表名，用于审查、审计和结果解释。
    tables: list[str]


@dataclass(slots=True)
# 作用：说明类 ReviewResult 的输入、输出与安全边界，避免调用方越过受控流程。
class ReviewResult:
    """SQL 审查结果；accepted 只代表语法/白名单审查通过，不代表可写库。"""

    # 是否通过 SELECT、单语句和表白名单等静态检查。
    accepted: bool
    # 未通过时的可读原因列表，供 Fixer 或用户查看。
    issues: list[str]
    # 经过空白归一化、补 LIMIT 后的 SQL；审查失败时也可能保留规范化文本。
    normalized_sql: str | None = None


@dataclass(slots=True)
# 作用：说明类 QueryResult 的输入、输出与安全边界，避免调用方越过受控流程。
class QueryResult:
    """智能查询或 RAG 请求的最终统一结果。

    该对象把回答、SQL、结构化行、引用来源、风险状态和工作流事件统一打包，
    使同步 API、流式接口、审计中心和前端能够消费同一种结果格式。
    """

    # completed、answered_by_rag、approval_required、blocked 或 failed 等业务状态。
    status: str
    # 面向用户展示的最终摘要或 RAG 答案。
    answer: str
    # 实际执行或审批中的 SQL；纯知识问答时为空。
    sql: str | None = None
    # 查询返回的结构化行数据。
    rows: list[dict[str, Any]] = field(default_factory=list)
    # RAG 引用的文档标题或其它证据来源。
    sources: list[str] = field(default_factory=list)
    # 需要进入审批中心继续处理时关联的审批单编号。
    approval_id: str | None = None
    # 从 Context Memory 到 Runner 的完整可回放轨迹。
    events: list[WorkflowEvent] = field(default_factory=list)
    # SQL 或工具本次实际访问的数据对象。
    data_sources: list[str] = field(default_factory=list)
    # 便于前端直接展示的实际工作流执行步骤。
    execution_steps: list[dict[str, Any]] = field(default_factory=list)
    # 对用户可读的审查、工具或执行失败原因。
    failure_reasons: list[str] = field(default_factory=list)
    # 需要澄清时的追问及建议回答选项。
    clarification: dict[str, Any] | None = None
    # SQL 结果整理统计；包含实际行列数、Token 估算、截断状态和原因。
    result_processing: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["events"] = [event.to_dict() for event in self.events]
        return result
