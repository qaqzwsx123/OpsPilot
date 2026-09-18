from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.context import ContextCompressor
from app.database import create_approval, execute_readonly, recent_memory, save_memory, write_audit
from app.models import QueryResult, WorkflowEvent
from app.providers import ResilientSqlWriter
from app.policy import permitted
from app.rag import KnowledgeRag
from app.sql_agent import MetadataRetriever, RiskAssessor, SqlFixer, SqlReviewer


EventHandler = Callable[[WorkflowEvent], None]


class SqlAgentWorkflow:
    def __init__(self) -> None:
        self.retriever = MetadataRetriever()
        self.writer = ResilientSqlWriter()
        self.reviewer = SqlReviewer()
        self.fixer = SqlFixer()
        self.risk_assessor = RiskAssessor()
        self.rag = KnowledgeRag()
        self.compressor = ContextCompressor(Path(__file__).resolve().parent.parent / "data" / "context")

    def run(self, question: str, requester: str, role: str = "operator", on_event: EventHandler | None = None) -> QueryResult:
        events: list[WorkflowEvent] = []

        def emit(stage: str, message: str, **details: object) -> None:
            event = WorkflowEvent(stage, message, dict(details))
            events.append(event)
            if on_event:
                on_event(event)

        def finalize(result: QueryResult) -> QueryResult:
            save_memory(requester, "assistant", result.answer)
            return result

        if not permitted(role, "read"):
            result = QueryResult("blocked", "当前角色无权执行查询。请切换到观察者、运维工程师或值班负责人角色。", events=events)
            write_audit(requester, "access_denied", {"role": role, "operation": "query"})
            return finalize(result)
        memory = recent_memory(requester)
        if memory:
            emit("context", "已加载用户近期会话记忆", message_count=len(memory))
        save_memory(requester, "user", question)
        emit("recall", "开始三路元数据召回")
        candidates = self.retriever.retrieve(question)
        emit("recall", "元数据召回完成", tables=[candidate.name for candidate in candidates])
        generated = self.writer.generate(question, candidates)
        if generated is None:
            emit("rag", "无法生成可靠 SQL，切换到运维知识库")
            answer, sources = self.rag.answer(question)
            write_audit(requester, "rag_fallback", {"question": question, "sources": sources})
            return finalize(QueryResult("answered_by_rag", answer, sources=sources, events=events))

        emit("writer", "已生成候选 SQL", sql=generated.sql, confidence=generated.confidence, provider=self.writer.last_provider)
        allowed_tables = [candidate.name for candidate in candidates]
        review = self.reviewer.review(generated.sql, allowed_tables)
        repair_attempt = 0
        while not review.accepted and repair_attempt < 2:
            repaired_sql = self.fixer.fix(generated.sql, review.issues, allowed_tables)
            if repaired_sql is None or repaired_sql == generated.sql:
                break
            repair_attempt += 1
            emit("fix", "Reviewer 未通过，执行受控 SQL 修复", attempt=repair_attempt, sql=repaired_sql)
            generated.sql = repaired_sql
            review = self.reviewer.review(generated.sql, allowed_tables)
        if not review.accepted:
            emit("reviewer", "SQL 审查未通过，进入修复/拒绝路径", issues=review.issues)
            decision = self.risk_assessor.assess(generated.sql)
            if decision.mode.value == "manual":
                if not permitted(role, "request_change"):
                    emit("risk", "当前角色无权发起变更审批", role=role)
                    write_audit(requester, "access_denied", {"role": role, "operation": "request_change"})
                    return finalize(QueryResult("blocked", "观察者角色不能发起高危变更。请切换到运维工程师角色。", generated.sql, events=events))
                approval_id = create_approval(requester, generated.sql, decision.reason)
                write_audit(requester, "approval_requested", {"sql": generated.sql, "reason": decision.reason, "role": role})
                return finalize(QueryResult("approval_required", "该请求涉及写操作，已创建人工审批单。", generated.sql, approval_id=approval_id, events=events))
            write_audit(requester, "sql_blocked", {"sql": generated.sql, "issues": review.issues})
            return finalize(QueryResult("blocked", "SQL 未通过安全审查：" + "；".join(review.issues), generated.sql, events=events))

        emit("reviewer", "SQL 审查通过", sql=review.normalized_sql)
        decision = self.risk_assessor.assess(review.normalized_sql or generated.sql)
        emit("risk", "风险分级完成", mode=decision.mode.value, reason=decision.reason)
        if decision.mode.value != "auto":
            approval_id = create_approval(requester, review.normalized_sql or generated.sql, decision.reason)
            write_audit(requester, "approval_requested", {"sql": review.normalized_sql, "reason": decision.reason})
            return finalize(QueryResult("approval_required", "需要人工审批后才能执行。", review.normalized_sql, approval_id=approval_id, events=events))

        sql = review.normalized_sql or generated.sql
        try:
            rows = execute_readonly(sql)
        except Exception as exc:  # execution errors must not expose internals to users
            emit("runner", "SQL 执行失败，转知识库兜底", error=type(exc).__name__)
            answer, sources = self.rag.answer(question)
            write_audit(requester, "sql_execution_failed", {"sql": sql, "error": type(exc).__name__})
            return finalize(QueryResult("answered_by_rag", answer, sql=sql, sources=sources, events=events))
        compacted, stats = self.compressor.compact(str(rows))
        emit("runner", "只读 SQL 执行完成", row_count=len(rows), context=stats)
        write_audit(requester, "sql_executed", {"question": question, "sql": sql, "row_count": len(rows)})
        answer = f"查询完成，共返回 {len(rows)} 条记录。"
        return finalize(QueryResult("completed", answer, sql=sql, rows=rows, events=events))
