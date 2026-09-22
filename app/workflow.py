"""SQL Agent 工作流门面：组装依赖并调用 LangGraph。"""

from __future__ import annotations

from pathlib import Path

from app.context import ContextCompressor
from app.graph import EventHandler, SqlAgentGraph
from app.models import QueryResult
from app.providers import ResilientSqlWriter
from app.rag import KnowledgeRag
from app.sql_agent import MetadataRetriever, RiskAssessor, SqlFixer, SqlReviewer
from app.tool_planner import ToolPlanner


# 作用：说明类 SqlAgentWorkflow 的输入、输出与安全边界，避免调用方越过受控流程。
class SqlAgentWorkflow:
    """应用层工作流门面。

    该类负责组装 LangGraph 所需的节点依赖，不在这里实现具体业务判断。一次请求会把问题、
    请求者、角色、上下文记忆和事件回调放入图状态，由图中的 Recall、Writer、Reviewer、
    Risk Guard、Runner 等节点按条件边推进，并最终返回统一的 QueryResult。
    """

    def __init__(self) -> None:
        # 元数据召回：决定模型和 SQL Writer 可以看到哪些表。
        self.retriever = MetadataRetriever()
        # SQL 生成：模型优先，规则实现兜底。
        self.writer = ResilientSqlWriter()
        # 静态审查：拒绝写入、多语句和未授权表。
        self.reviewer = SqlReviewer()
        # 保守修复：只收窄只读 SQL，不修复写操作。
        self.fixer = SqlFixer()
        # 风险判断：AUTO/MANUAL/BLOCKED。
        self.risk_assessor = RiskAssessor()
        # 知识检索：优先 Chroma，失败时回退 SQLite 轻量检索。
        self.rag = KnowledgeRag()
        # 自动工具编排：模型 Function Calling 失败时走安全规则白名单。
        self.tool_planner = ToolPlanner()
        # 上下文压缩器：将历史事件和大段结果压缩后再交给模型。
        self.compressor = ContextCompressor(Path(__file__).resolve().parent.parent / "data" / "context")
        # LangGraph 图对象；编译后只读使用，便于同步和流式入口复用同一套节点。
        self.graph = SqlAgentGraph(
            self.retriever,
            self.writer,
            self.reviewer,
            self.fixer,
            self.risk_assessor,
            self.rag,
            self.tool_planner,
            self.compressor,
        ).graph

    def run(self, question: str, requester: str, role: str = "operator", on_event: EventHandler | None = None, use_model_tools: bool = False) -> QueryResult:
        # 外部接口只面对一个 run 方法，复杂的节点流转由 SqlAgentGraph 负责。
        state = {
            "question": question,
            "requester": requester,
            "role": role,
            "use_model_tools": use_model_tools,
            "on_event": on_event,
            "events": [],
        }
        final_state = self.graph.invoke(state)
        result = final_state.get("result")
        if not isinstance(result, QueryResult):
            raise RuntimeError("LangGraph workflow completed without a QueryResult")
        return result
