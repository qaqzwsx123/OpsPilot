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


class SqlAgentWorkflow:
    def __init__(self) -> None:
        self.retriever = MetadataRetriever()
        self.writer = ResilientSqlWriter()
        self.reviewer = SqlReviewer()
        self.fixer = SqlFixer()
        self.risk_assessor = RiskAssessor()
        self.rag = KnowledgeRag()
        self.tool_planner = ToolPlanner()
        self.compressor = ContextCompressor(Path(__file__).resolve().parent.parent / "data" / "context")
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
