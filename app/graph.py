# 启用延迟注解求值，允许在类型注解中引用尚未定义的名称。
from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any, Callable, Literal, TypedDict

# LangGraph 的核心组件：END、START 是特殊节点，StateGraph 用于构建状态图。
from langgraph.graph import END, START, StateGraph

# 项目内部依赖：数据库操作、数据模型、权限策略、SQL 生成器、RAG、SQL Agent 组件和工具规划器。
from app.database import create_approval, execute_readonly, recent_memory, save_memory, write_audit
from app.models import CandidateTable, GeneratedSql, QueryResult, ReviewResult, WorkflowEvent
from app.policy import permitted
from app.providers import ResilientSqlWriter
from app.rag import KnowledgeRag
from app.sql_agent import MetadataRetriever, RiskAssessor, SqlFixer, SqlReviewer
from app.skills import SkillRegistry, run_skill
from app.tool_planner import ToolPlanner, ToolSelection, classify_query_intent, prefer_standalone_knowledge


# 事件处理器类型：接收一个 WorkflowEvent，无返回值。
# 用于向前端实时推送工作流进度，例如通过 SSE。
EventHandler = Callable[[WorkflowEvent], None]


def _data_question_from_mixed_intent(question: str) -> str:
    """从“查数据并说明处置办法”中提取 SQL Writer 应处理的数据部分。"""
    markers = (
        "，并按", ",并按", "并按", "，并根据", ",并根据", "并根据",
        "，并说明", ",并说明", "并说明", "，并告诉我", ",并告诉我", "并告诉我",
        "，并解释", ",并解释", "并解释", "并介绍", "并给出", "，并说", ",并说", "并说",
        "，同时说明", ",同时说明", "同时说明", "以及如何", "以及怎么", "；按", ";按",
    )
    split_at = min((position for marker in markers if (position := question.find(marker)) > 0), default=-1)
    return question[:split_at].rstrip(" ，,；;") if split_at > 0 else question


class WorkflowState(TypedDict, total=False):
    """LangGraph 工作流的状态字典，在各节点之间传递。

    total=False 表示所有字段都是可选的，节点可以按需更新部分字段。
    LangGraph 会把每个节点返回的字典合并到全局状态中。
    """
    question: str                       # 用户原始问题
    requester: str                      # 请求者标识（用户 ID 或用户名）
    role: str                           # 当前角色，用于权限校验
    use_model_tools: bool               # 是否启用模型进行工具规划
    on_event: EventHandler | None       # 事件回调，用于实时推送工作流事件
    events: list[WorkflowEvent]         # 已产生的事件列表
    route: str                          # 路由标记，供条件边判断下一步走向
    memory: list[dict[str, Any]]        # 用户近期会话记忆
    selections: list[Any]               # 工具规划器选中的工具列表
    tool_context: list[dict[str, Any]]  # 工具执行返回的上下文证据
    tool_summary: str                   # 工具结果汇总文本
    selection_mode: str                 # 工具选择模式：模型函数调用或规则白名单
    active_skill: str                   # 本次匹配的运维流程名称
    candidates: list[CandidateTable]    # 元数据召回得到的候选表
    allowed_tables: list[str]           # 允许访问的表名白名单
    generated: GeneratedSql             # 生成的 SQL 对象
    review: ReviewResult                # SQL 审查结果
    repair_attempt: int                 # 已尝试修复 SQL 的次数
    risk_reason: str                    # 风险原因说明
    risk_mode: str                      # 风险模式：auto 或 manual
    result: QueryResult                 # 最终返回给用户的查询结果


class SqlAgentGraph:
    """基于 LangGraph 的 SQL Agent 编排类。

    该类把原有的 SQL Agent 业务逻辑（元数据召回、SQL 生成、审查、修复、风险评估、
    人工审批、只读执行、RAG 兜底）组织成一张状态图。
    每个节点是一个处理步骤，边决定流转方向。

    注意：这是第一版迁移，故意没有使用 checkpoint 或 interrupt。
    审批的持久化和执行仍然由现有的审批 API 负责，避免状态图与外部审批系统冲突。
    """

    def __init__(
        self,
        retriever: MetadataRetriever,
        writer: ResilientSqlWriter,
        reviewer: SqlReviewer,
        fixer: SqlFixer,
        risk_assessor: RiskAssessor,
        rag: KnowledgeRag,
        tool_planner: ToolPlanner,
    ) -> None:
        """注入所有依赖组件，并构建状态图。"""
        self.retriever = retriever          # 元数据召回器：三路召回候选表
        self.writer = writer                # SQL 生成器：调用 LLM 生成候选 SQL
        self.reviewer = reviewer            # SQL 审查器：检查 SQL 安全性与规范性
        self.fixer = fixer                  # SQL 修复器：根据审查问题修复 SQL
        self.risk_assessor = risk_assessor  # 风险评估器：判断 SQL 是自动执行还是人工审批
        self.rag = rag                      # 知识库 RAG：SQL 无法生成或执行失败时兜底回答
        self.tool_planner = tool_planner    # 工具规划器：决定调用哪些只读工具
        self.skill_registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
        self.graph = self._build_graph()    # 编译后的 LangGraph 状态图

    def _build_graph(self):
        """构建并编译 LangGraph 状态图。

        返回一个可执行对象，调用 .invoke(state) 即可运行整个工作流。
        """
        builder = StateGraph(WorkflowState)

        # 注册所有节点。每个节点对应一个方法，接收 state，返回要更新的 state 字段。
        builder.add_node("access_check", self._access_check)    # 权限检查
        builder.add_node("prepare", self._prepare)              # 准备：加载记忆、工具规划
        builder.add_node("recall", self._recall)                # 元数据召回
        builder.add_node("generate", self._generate)            # 生成 SQL
        builder.add_node("review_node", self._review)           # 审查 SQL
        builder.add_node("fix", self._fix)                      # 修复 SQL
        builder.add_node("review_failed", self._review_failed)  # 审查失败处理
        builder.add_node("risk", self._risk)                    # 风险评估
        builder.add_node("approval", self._approval)            # 创建审批单
        builder.add_node("execute", self._execute)              # 执行只读 SQL
        builder.add_node("rag", self._rag)                      # RAG 兜底
        builder.add_node("finalize", self._finalize)            # 收尾：保存记忆

        # 定义图的流转关系。
        builder.add_edge(START, "access_check")  # 从 START 进入权限检查

        # 权限检查后：有权则 prepare，无权则 finalize
        builder.add_conditional_edges(
            "access_check",
            self._route_access,
            {"prepare": "prepare", "finalize": "finalize"},
        )

        builder.add_conditional_edges(
            "prepare",
            self._route_prepared,
            {"recall": "recall", "finalize": "finalize"},
        )
        builder.add_edge("recall", "generate")   # 召回完成后生成 SQL

        # 生成后：如果生成失败则走 rag，否则进入 review_node
        builder.add_conditional_edges(
            "generate",
            self._route_generated,
            {"review": "review_node", "rag": "rag"},
        )

        # 审查后：根据审查结果决定修复、风险评估或审查失败
        builder.add_conditional_edges(
            "review_node",
            self._route_review,
            {"fix": "fix", "risk": "risk", "review_failed": "review_failed"},
        )

        # 修复后：要么重新审查，要么审查失败
        builder.add_conditional_edges(
            "fix",
            self._route_fix,
            {"review": "review_node", "review_failed": "review_failed"},
        )

        builder.add_edge("review_failed", "finalize")  # 审查失败直接收尾

        # 风险评估后：自动执行或走人工审批
        builder.add_conditional_edges(
            "risk",
            self._route_risk,
            {"execute": "execute", "approval": "approval"},
        )

        builder.add_edge("approval", "finalize")  # 审批单创建后收尾
        builder.add_edge("execute", "finalize")   # 执行完成后收尾
        builder.add_edge("rag", "finalize")       # RAG 兜底后收尾
        builder.add_edge("finalize", END)         # 收尾后结束

        return builder.compile()  # 编译图，返回可调用对象

    @staticmethod
    def _emit(state: WorkflowState, stage: str, message: str, **details: object) -> list[WorkflowEvent]:
        """创建一个工作流事件，追加到事件列表，并通过回调推送。

        参数：
            state: 当前状态，可能包含已有事件和 on_event 回调。
            stage: 事件阶段，如 "recall"、"writer"、"risk"。
            message: 人类可读的事件描述。
            **details: 附加到事件中的键值对详情。

        返回：
            更新后的事件列表。
        """
        event = WorkflowEvent(stage, message, dict(details))
        events = [*state.get("events", []), event]  # 复制旧事件并追加新事件
        on_event = state.get("on_event")
        if on_event:
            on_event(event)  # 实时推送给前端（例如 SSE）
        return events

    def _access_check(self, state: WorkflowState) -> dict[str, Any]:
        """节点：权限检查。

        检查当前角色是否有 "read" 权限。如果没有，记录审计日志，
        构造被阻止的 QueryResult，并设置路由为 finalize。
        """
        role = state["role"]
        if permitted(role, "read"):
            return {"route": "prepare"}  # 有权限，进入准备阶段

        # 无权限：记录审计，构造阻止结果
        events = state.get("events", [])
        requester = state["requester"]
        write_audit(requester, "access_denied", {"role": role, "operation": "query"})
        result = QueryResult(
            "blocked",
            "当前角色无权执行查询。请切换到观察者、运维工程师或值班负责人角色。",
            events=events,
        )
        return {"route": "finalize", "events": events, "result": result}

    def _prepare(self, state: WorkflowState) -> dict[str, Any]:
        """节点：准备阶段。

        1. 加载用户近期会话记忆。
        2. 保存用户问题到记忆。
        3. 根据 use_model_tools 决定使用模型工具规划还是规则白名单。
        4. 执行工具规划，获取选中的工具和工具执行上下文。
        5. 记录工具规划审计日志。
        6. 发送工具规划事件和工具执行事件。
        7. 返回更新后的状态。
        """
        requester = state["requester"]
        question = state["question"]
        events = state.get("events", [])
        memory = recent_memory(requester)  # 加载近期记忆
        if memory:
            # 如果存在记忆，发送 context 事件
            state = {**state, "events": events}
            events = self._emit(state, "context", "已加载用户近期会话记忆", message_count=len(memory))
        save_memory(requester, "user", question)  # 保存用户问题

        planner_started = time.perf_counter()  # 开始计时
        skill = None if prefer_standalone_knowledge(question) else self.skill_registry.match(question)
        active_skill = skill.name if skill else ""
        if skill:
            # 明确匹配到复合流程时，流程负责按元数据顺序调用它声明的只读工具。
            skill_result = run_skill(skill.name, question)
            tool_context = skill_result.get("tool_evidence", [])
            selections = [ToolSelection(item["tool"], item["reason"]) for item in tool_context]
            tool_summary = skill_result.get("summary", "")
            if any(item.get("tool") == "knowledge_search" for item in tool_context):
                source_summary = self.tool_planner._fallback_summary(tool_context)
                if source_summary:
                    tool_summary += "\n" + source_summary
            selection_mode = "skill_workflow"
            write_audit(requester, "skill_run", {
                "skill": skill.name, "input": question[:160], "status": skill_result.get("status"),
                "tools": skill_result.get("tools_used", []), "source": "intelligent_query", "role": state["role"],
            })
        else:
            needs_knowledge, needs_data = classify_query_intent(question)
            if needs_knowledge and not needs_data:
                events = self._emit(state, "knowledge", "知识问题优先检索知识库")
                answer, sources = self.rag.answer(question)
                events = self._emit(
                    {**state, "events": events}, "knowledge",
                    "知识库检索完成并附带文档来源", sources=sources,
                )
                write_audit(requester, "knowledge_searched", {
                    "question": question[:160], "sources": sources,
                    "surface": "intelligent_query", "role": state["role"],
                })
                return {
                    "route": "knowledge_only",
                    "result": QueryResult("answered_by_rag", answer, sources=sources, events=events),
                    "events": events,
                    "memory": memory,
                }
            if needs_knowledge and needs_data:
                selections, tool_context, tool_summary, selection_mode, _ = self.tool_planner.execute_for_intent(question)
            elif state.get("use_model_tools", False):
                # 纯实时数据问题不向规划模型暴露知识检索工具，避免无关 SOP 混入结果。
                selections, tool_context, tool_summary, selection_mode = self.tool_planner.execute_agent(
                    question, allow_knowledge=False,
                )
            else:
                selections, tool_context = self.tool_planner.execute(question, include_knowledge=False)
                tool_summary, selection_mode = self.tool_planner._fallback_summary(tool_context), "rule_based_allowlist"
        planner_latency_ms = round((time.perf_counter() - planner_started) * 1000)  # 计算耗时
        planner_name = "运维流程" if active_skill else ("本地 LLM Function Calling" if selection_mode == "model_function_calling" else "规则白名单兜底")
        role = state["role"]
        if state.get("use_model_tools", False):
            # 如果是模型规划，记录审计日志
            write_audit(requester, "agent_tool_plan", {
                "tools": [{"name": item.name, "reason": item.reason} for item in selections],
                "selection_mode": selection_mode,
                "planner": planner_name,
                "latency_ms": planner_latency_ms,
                "role": role,
            })
        # 构造要返回的状态更新
        next_state: dict[str, Any] = {
            "memory": memory,
            "selections": selections,
            "tool_context": tool_context,
            "tool_summary": tool_summary,
            "selection_mode": selection_mode,
            "active_skill": active_skill,
            "events": events,
        }
        if selections or active_skill:
            # 如果有选中的工具，发送 tool_plan 事件
            next_state["events"] = self._emit(
                {**state, **next_state},
                "tool_plan",
                f"Agent 已选择运维流程：{active_skill}" if active_skill else "Agent 已选择只读白名单工具",
                tools=[{"name": item.name, "reason": item.reason} for item in selections],
                skill=active_skill or None,
                selection_mode=selection_mode,
                planner=planner_name,
                latency_ms=planner_latency_ms,
            )
            # 逐个发送工具执行完成事件，并写审计日志
            for evidence in tool_context:
                next_state["events"] = self._emit(
                    {**state, **next_state},
                    "tool",
                    f"工具 {evidence['tool']} 执行完成",
                    **evidence,
                )
                write_audit(requester, "agent_tool_invoked", {
                    "tool": evidence["tool"],
                    "status": evidence["status"],
                    "result_count": evidence["result_count"],
                    "reason": evidence["reason"],
                    "arguments": evidence.get("arguments", {}),
                    "round": evidence.get("round", 1),
                    "selection_mode": evidence.get("selection_mode", selection_mode),
                    "needs_followup": evidence.get("needs_followup", False),
                    "role": role,
                })
            # 发送工具结果汇总事件
            next_state["events"] = self._emit({**state, **next_state}, "tool_summary", "Agent 已汇总工具结果", answer=tool_summary)
        else:
            # 没有命中工具，发送 tool_plan 事件说明继续结构化召回
            next_state["events"] = self._emit(
                {**state, **next_state},
                "tool_plan",
                "未命中适用的自动工具，继续结构化召回",
                tools=[],
                selection_mode=selection_mode,
                planner=planner_name,
                latency_ms=planner_latency_ms,
            )
        return next_state

    def _recall(self, state: WorkflowState) -> dict[str, Any]:
        """节点：元数据召回。

        调用 retriever.retrieve 进行三路元数据召回，得到候选表。
        发送 recall 事件，返回候选表和允许的表名。
        """
        events = self._emit(state, "recall", "开始三路元数据召回")
        candidates = self.retriever.retrieve(state["question"])  # 召回候选表
        events = self._emit({**state, "events": events}, "recall", "元数据召回完成", tables=[candidate.name for candidate in candidates])
        return {"candidates": candidates, "allowed_tables": [candidate.name for candidate in candidates], "events": events}

    def _generate(self, state: WorkflowState) -> dict[str, Any]:
        """节点：生成 SQL。

        使用 writer.generate 根据问题、候选表和工具上下文生成候选 SQL。
        如果生成失败（返回 None），则走 RAG 兜底，直接构造 answered_by_rag 结果。
        如果成功，发送 writer 事件，返回生成的 SQL 对象，并设置路由为 review。
        """
        # 知识库原文只用于检索证据与最终解释，不送入 SQL 生成器，避免文档指令影响 SQL 规划。
        writer_context = [
            item for item in state.get("tool_context", [])
            if item.get("tool") != "knowledge_search"
        ]
        writer_question = state["question"]
        needs_knowledge, needs_data = classify_query_intent(writer_question)
        if needs_knowledge and needs_data:
            # 已有知识工具负责 SOP 证据；SQL Writer 只解析数据子问题，避免混合句被误判为纯知识问答。
            writer_question = _data_question_from_mixed_intent(writer_question)
        generated = self.writer.generate(writer_question, state["candidates"], writer_context)
        if generated is None:
            # 无法生成可靠 SQL，切换到运维知识库
            events = self._emit(state, "rag", "无法生成可靠 SQL，切换到运维知识库")
            answer, sources = self.rag.answer(state["question"])
            write_audit(state["requester"], "rag_fallback", {"question": state["question"], "sources": sources})
            result = QueryResult("answered_by_rag", answer, sources=sources, events=events)
            return {"route": "rag", "events": events, "result": result}
        # 生成成功
        events = self._emit(
            state,
            "writer",
            "已生成候选 SQL",
            sql=generated.sql,
            confidence=generated.confidence,
            provider=self.writer.last_provider,
        )
        return {"generated": generated, "repair_attempt": 0, "events": events, "route": "review"}

    def _review(self, state: WorkflowState) -> dict[str, Any]:
        """节点：审查 SQL。

        调用 reviewer.review 检查生成的 SQL 是否符合安全规则和表名白名单。
        返回审查结果。
        """
        review = self.reviewer.review(state["generated"].sql, state["allowed_tables"])
        return {"review": review}

    def _fix(self, state: WorkflowState) -> dict[str, Any]:
        """节点：修复 SQL。

        当审查未通过时，调用 fixer.fix 根据审查问题修复 SQL。
        如果修复失败（返回 None 或与原始 SQL 相同），设置路由为 review_failed。
        如果修复成功，增加修复次数，更新 generated.sql，发送 fix 事件，设置路由为 review 重新审查。
        """
        generated = state["generated"]
        repaired_sql = self.fixer.fix(generated.sql, state["review"].issues, state["allowed_tables"])
        if repaired_sql is None or repaired_sql == generated.sql:
            # 修复无效，进入审查失败路径
            return {"route": "review_failed"}
        repair_attempt = state.get("repair_attempt", 0) + 1
        generated.sql = repaired_sql  # 注意：这里直接修改了状态中的对象
        events = self._emit(
            {**state, "events": state.get("events", [])},
            "fix",
            "Reviewer 未通过，执行受控 SQL 修复",
            attempt=repair_attempt,
            sql=repaired_sql,
        )
        return {"generated": generated, "repair_attempt": repair_attempt, "events": events, "route": "review"}

    def _review_failed(self, state: WorkflowState) -> dict[str, Any]:
        """节点：审查失败处理。

        当 SQL 审查最终未通过（修复次数超限或无法修复）时进入此节点。
        1. 发送 reviewer 事件说明未通过。
        2. 使用 risk_assessor.assess 评估风险。
        3. 如果风险模式为 manual，且当前角色有 request_change 权限，则创建审批单；
           否则返回 blocked 结果。
        4. 如果风险模式不是 manual，则记录审计并返回 blocked 结果。
        """
        generated = state["generated"]
        review = state["review"]
        events = self._emit(state, "reviewer", "SQL 审查未通过，进入修复/拒绝路径", issues=review.issues)
        decision = self.risk_assessor.assess(generated.sql)
        if decision.mode.value == "manual":
            # 需要人工审批
            if not permitted(state["role"], "request_change"):
                # 无权限发起变更审批
                events = self._emit({**state, "events": events}, "risk", "当前角色无权发起变更审批", role=state["role"])
                write_audit(state["requester"], "access_denied", {"role": state["role"], "operation": "request_change"})
                result = QueryResult("blocked", "观察者角色不能发起高危变更。请切换到运维工程师角色。", generated.sql, events=events)
                return {"events": events, "result": result}
            # 有权限，创建审批单
            approval_id = create_approval(state["requester"], generated.sql, decision.reason)
            write_audit(state["requester"], "approval_requested", {"sql": generated.sql, "reason": decision.reason, "role": state["role"]})
            result = QueryResult("approval_required", "该请求涉及写操作，已创建人工审批单。", generated.sql, approval_id=approval_id, events=events)
            return {"events": events, "result": result}
        # 非 manual 模式，直接阻止
        write_audit(state["requester"], "sql_blocked", {"sql": generated.sql, "issues": review.issues})
        result = QueryResult("blocked", "SQL 未通过安全审查：" + "；".join(review.issues), generated.sql, events=events)
        return {"events": events, "result": result}

    def _risk(self, state: WorkflowState) -> dict[str, Any]:
        """节点：风险评估。

        审查通过后，使用 risk_assessor.assess 判断 SQL 是自动执行还是人工审批。
        发送 reviewer 通过事件和 risk 事件。
        返回风险模式、原因，并设置路由为 execute 或 approval。
        """
        sql = state["review"].normalized_sql or state["generated"].sql
        decision = self.risk_assessor.assess(sql)
        events = self._emit(state, "reviewer", "SQL 审查通过", sql=sql)
        events = self._emit({**state, "events": events}, "risk", "风险分级完成", mode=decision.mode.value, reason=decision.reason)
        return {
            "events": events,
            "route": "execute" if decision.mode.value == "auto" else "approval",
            "risk_reason": decision.reason,
            "risk_mode": decision.mode.value,
        }

    def _approval(self, state: WorkflowState) -> dict[str, Any]:
        """节点：创建审批单。

        当风险评估为 manual 时进入此节点。
        使用 create_approval 创建审批单，记录审计日志，返回 approval_required 结果。
        """
        sql = state["review"].normalized_sql or state["generated"].sql
        reason = state.get("risk_reason", "需要人工审批")
        approval_id = create_approval(state["requester"], sql, reason)
        write_audit(state["requester"], "approval_requested", {"sql": sql, "reason": reason})
        result = QueryResult("approval_required", "需要人工审批后才能执行。", sql, approval_id=approval_id, events=state.get("events", []))
        return {"result": result}

    def _execute(self, state: WorkflowState) -> dict[str, Any]:
        """节点：执行只读 SQL。

        当风险评估为 auto 时进入此节点。
        调用 execute_readonly 执行 SQL。如果执行失败，则走 RAG 兜底。
        如果成功，完整结果保留给用户界面；只有后续模型请求才会按上下文预算压缩，
        然后发送 runner 事件、写审计日志并构造 completed 结果。
        """
        sql = state["review"].normalized_sql or state["generated"].sql
        try:
            rows = execute_readonly(sql)
        except Exception as exc:
            # 执行错误不向用户暴露内部细节，转知识库兜底
            events = self._emit(state, "runner", "SQL 执行失败，转知识库兜底", error=type(exc).__name__)
            answer, sources = self.rag.answer(state["question"])
            write_audit(state["requester"], "sql_execution_failed", {"sql": sql, "error": type(exc).__name__})
            result = QueryResult("answered_by_rag", answer, sql=sql, sources=sources, events=events)
            return {"events": events, "result": result}
        # 执行成功；查询行是用户可见结果，不在此处做无效压缩或另存一份原始副本。
        events = self._emit(state, "runner", "只读 SQL 执行完成", row_count=len(rows))
        write_audit(state["requester"], "sql_executed", {"question": state["question"], "sql": sql, "row_count": len(rows)})
        tool_summary = state.get("tool_summary", "")
        answer = (tool_summary + "\n\n" if tool_summary else "") + f"查询完成，共返回 {len(rows)} 条记录。"
        sources = []
        for evidence in state.get("tool_context", []):
            if evidence.get("tool") != "knowledge_search":
                continue
            for document in evidence.get("sample", [])[:3]:
                if not isinstance(document, dict) or not document.get("title"):
                    continue
                title = str(document["title"])
                chunk_index = document.get("chunk_index")
                source = f"{title} · 片段 {chunk_index + 1}" if isinstance(chunk_index, int) else title
                if source not in sources:
                    sources.append(source)
        result = QueryResult("completed", answer, sql=sql, rows=rows, sources=sources, events=events)
        return {"events": events, "result": result}

    def _rag(self, state: WorkflowState) -> dict[str, Any]:
        """节点：RAG 兜底。

        实际上结果已经在 _generate 中准备好（当生成失败时），
        这里只是把 state["result"] 原样返回，保持图的流转。
        """
        # 结果在 _generate 中准备，以保持相同的兜底时机
        return {"result": state["result"]}

    @staticmethod
    def _finalize(state: WorkflowState) -> dict[str, Any]:
        """节点：收尾。

        保存助手回答到记忆，返回最终结果和事件。
        """
        result = state["result"]
        save_memory(state["requester"], "assistant", result.answer)
        result.execution_steps = [
            {"stage": event.stage, "message": event.message, "details": event.details}
            for event in result.events
        ]
        data_sources: list[str] = []
        for event in result.events:
            if event.stage == "tool" and event.details.get("tool"):
                label = f"只读工具：{event.details['tool']}"
                if label not in data_sources:
                    data_sources.append(label)
        executed_sql = result.sql or ""
        sql_tables = list(dict.fromkeys(
            match.group(1) for match in re.finditer(r"\b(?:FROM|JOIN)\s+[\"`]?([A-Za-z_][A-Za-z0-9_]*)", executed_sql, re.IGNORECASE)
        ))
        runner_failed = any(event.stage == "runner" and event.details.get("error") for event in result.events)
        if result.status == "completed" or runner_failed:
            prefix = "SQLite 业务表：" if result.status == "completed" else "尝试查询 SQLite 业务表："
            data_sources.extend(prefix + table for table in sql_tables)
        for source in result.sources:
            label = f"知识库：{source}"
            if label not in data_sources:
                data_sources.append(label)
        result.data_sources = data_sources

        failures: list[str] = []
        for event in result.events:
            details = event.details
            if details.get("status") in {"failed", "blocked", "not_found"}:
                failures.append(f"工具 {details.get('tool', '未知工具')}：{details.get('summary') or event.message}")
            if details.get("error"):
                failures.append(f"{event.message}（{details['error']}）")
            if result.status in {"blocked", "failed"} and isinstance(details.get("issues"), list):
                failures.extend(str(issue) for issue in details["issues"])
        if result.status == "blocked" and not failures:
            failures.append(result.answer)
        result.failure_reasons = list(dict.fromkeys(failures))
        return {"result": result, "events": result.events}

    # 以下为条件边的路由函数，根据 state 中的 route 或 review 结果决定下一步节点。

    @staticmethod
    def _route_access(state: WorkflowState) -> Literal["prepare", "finalize"]:
        """权限检查后的路由：如果 route 是 finalize 则去 finalize，否则去 prepare。"""
        return "finalize" if state.get("route") == "finalize" else "prepare"

    @staticmethod
    def _route_prepared(state: WorkflowState) -> Literal["recall", "finalize"]:
        """纯知识问题已经完成 RAG 回答，跳过无关的 SQL 召回与生成步骤。"""
        return "finalize" if state.get("route") == "knowledge_only" else "recall"

    @staticmethod
    def _route_generated(state: WorkflowState) -> Literal["review", "rag"]:
        """生成 SQL 后的路由：如果 route 是 rag 则去 rag，否则去 review。"""
        return "rag" if state.get("route") == "rag" else "review"

    @staticmethod
    def _route_review(state: WorkflowState) -> Literal["fix", "risk", "review_failed"]:
        """审查后的路由：
        - 如果审查通过（review.accepted 为 True），去 risk。
        - 如果 route 是 review_failed 或修复次数已达 2 次，去 review_failed。
        - 否则去 fix。
        """
        review = state["review"]
        if review.accepted:
            return "risk"
        if state.get("route") == "review_failed" or state.get("repair_attempt", 0) >= 2:
            return "review_failed"
        return "fix"

    @staticmethod
    def _route_fix(state: WorkflowState) -> Literal["review", "review_failed"]:
        """修复后的路由：如果 route 是 review_failed 则去 review_failed，否则去 review 重新审查。"""
        return "review_failed" if state.get("route") == "review_failed" else "review"

    @staticmethod
    def _route_risk(state: WorkflowState) -> Literal["execute", "approval"]:
        """风险评估后的路由：如果 route 是 execute 则去 execute，否则去 approval。"""
        return "execute" if state.get("route") == "execute" else "approval"
