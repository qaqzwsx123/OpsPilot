"""SQL Agent 工作流门面：组装依赖并调用 LangGraph。

本模块是 SQL Agent 的应用层门面（Facade），核心职责：
1. 在初始化时组装 LangGraph 所需的节点依赖（召回、生成、审查、修复、风险判断、RAG 和工具规划）；
2. 编译 LangGraph 图对象，供同步和流式入口复用同一套节点；
3. 对外只暴露一个 run 方法，把问题、请求者、角色、事件回调和工具开关放入图状态；
4. 调用图并校验最终结果类型，返回统一的 QueryResult。

设计原则：
- 门面只负责“组装”和“转发”，不实现具体业务判断；
- 具体决策逻辑分布在各个节点组件中，便于单独测试和替换；
- 图和依赖在实例化时构建一次，多次 run 复用，避免重复初始化开销；
- 使用显式类型校验，防止图返回错误结构导致上层难以定位问题。

安全边界：
- 所有安全判断（元数据召回白名单、SQL 审查、风险分级）都由被组装的组件负责；
- 门面本身不做安全判断，但确保这些组件被正确注入图中；
- 不在门面中执行 SQL，执行由图中 Runner 节点完成；
- 聊天上下文压缩在模型请求组装处执行，不改变数据库中的原始会话记录。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

from app.graph import EventHandler, SqlAgentGraph  # LangGraph 图对象和事件回调类型
from app.models import QueryResult, WorkflowEvent  # 统一结果和可回放轨迹
from app.query_clarification import clarification_for_query  # 执行前确认缺失的查询条件
from app.providers import ResilientSqlWriter  # 可恢复的 SQL Writer：模型优先，规则兜底
from app.rag import KnowledgeRag  # 知识检索：优先 Chroma，失败时回退本地轻量检索
from app.sql_agent import (
    MetadataRetriever,   # 元数据召回：决定模型可见表范围
    RiskAssessor,        # 风险分级：AUTO / MANUAL / BLOCKED
    SqlFixer,            # 保守 SQL 修复器
    SqlReviewer,         # 只读 SQL 静态审查器
)
from app.tool_planner import ToolPlanner  # 只读工具编排器：模型优先，规则兜底


class SqlAgentWorkflow:
    """应用层工作流门面。
    该类负责组装 LangGraph 所需的节点依赖，不在这里实现具体业务判断。一次请求会把问题、
    请求者、角色、上下文记忆和事件回调放入图状态，由图中的 Recall、Writer、Reviewer、
    Risk Guard、Runner 等节点按条件边推进，并最终返回统一的 QueryResult。
    使用方式：
        workflow = SqlAgentWorkflow()
        result = workflow.run("查询最近的 P1 告警", "Lenovo", "operator")
    设计说明：
    - 所有依赖在 __init__ 中一次性构建，run 时复用；
    - 图对象编译后只读使用，同步和流式入口共享同一套节点；
    - run 方法只做状态组装和结果校验，不介入业务逻辑。

    安全边界：
    - 安全判断全部由被组装的组件负责，门面不做安全决策；
    - 通过显式依赖注入，保证图中节点使用的是受控组件而非任意实现。
    """

    def __init__(self) -> None:
        # 元数据召回：决定模型和 SQL Writer 可以看到哪些表。
        # 召回结果同时作为 Reviewer 的白名单来源，限制 SQL 可见表范围。
        self.retriever = MetadataRetriever()
        # SQL 生成：模型优先，规则实现兜底。
        # 内部会检查 settings.llm_enabled，未配置时只用规则生成器。
        self.writer = ResilientSqlWriter()
        # 静态审查：拒绝写入、多语句和未授权表。
        # 通过审查只说明可以进入风险判断，不代表可以写库。
        self.reviewer = SqlReviewer()
        # 保守修复：只收窄只读 SQL，不修复写操作。
        # 遇到未授权表或非 SELECT 时返回 None，交由工作流进入其他分支。
        self.fixer = SqlFixer()
        # 风险判断：AUTO/MANUAL/BLOCKED。
        # BLOCKED 直接阻断，MANUAL 进入审批，AUTO 可自动执行。
        self.risk_assessor = RiskAssessor()
        # 知识检索：优先 Chroma，失败时回退 SQLite 轻量检索。
        # 用于 SOP、解释类问题的 RAG 路径。
        self.rag = KnowledgeRag()
        # 自动工具编排：模型 Function Calling 失败时走安全规则白名单。
        # 只允许 risk="auto" 的只读工具，manual/blocked 不在规划范围。
        self.tool_planner = ToolPlanner()
        # LangGraph 图对象；编译后只读使用，便于同步和流式入口复用同一套节点。
        # 构造参数顺序与 SqlAgentGraph 的签名保持一致。
        self.graph = SqlAgentGraph(
            self.retriever,
            self.writer,
            self.reviewer,
            self.fixer,
            self.risk_assessor,
            self.rag,
            self.tool_planner,
        ).graph
    def run(
        self,
        question: str,
        requester: str,
        role: str = "operator",
        on_event: EventHandler | None = None,
        use_model_tools: bool = False,
    ) -> QueryResult: # 返回类型标注，表示这个方法最终返回一个 QueryResult 对象。包含状态、SQL、结果行、证据等。
        clarification = clarification_for_query(question)
        if clarification:
            event = WorkflowEvent(
                "clarification", "查询条件不完整，等待用户补充",
                {"domain": clarification["domain"], "prompt": clarification["prompt"]},
            )
            if on_event:
                on_event(event)
            return QueryResult(
                "needs_clarification", clarification["prompt"],
                events=[event], clarification=clarification,
            )
        # 构造了一个字典 state，它是传给 LangGraph 图的初始状态。
        state = {
            "question": question,
            "requester": requester,
            "role": role,
            "use_model_tools": use_model_tools,
            "on_event": on_event,
            "events": [],
        }

        # self.graph 是在 SqlAgentWorkflow.__init__ 里创建并编译好的 LangGraph 图对象。
       # .invoke(state) 表示同步执行这个图，把 state 作为输入。
        final_state = self.graph.invoke(state)

        # 从最终状态中取出结果。
        result = final_state.get("result")

        # 类型校验：确保图返回了合法的 QueryResult，避免上层拿到错误结构。
        if not isinstance(result, QueryResult):
            raise RuntimeError("LangGraph workflow completed without a QueryResult")
        return result
