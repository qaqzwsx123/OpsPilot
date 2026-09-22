"""FastAPI 应用入口：提供页面 API、SSE 流式接口和权限边界。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.database import APPROVAL_STATUSES, add_chat_message, add_evaluation_case, add_knowledge_document, approve, approval_count, approval_status_counts, audit_cleanup_preview, audit_count, audit_integrity, create_approval, create_chat_conversation as create_chat_conversation_record, data_catalog, delete_approval, delete_audit_event, delete_audit_range, delete_evaluation_case, delete_chat_conversation, delete_knowledge_document, document_chunks, execute_approved, get_chat_messages, import_metric_csv, initialize, knowledge_document_count, knowledge_evaluation_feedback_summary, knowledge_tags, list_approvals, list_audit, list_chat_conversations, list_knowledge_documents, list_knowledge_versions, list_metric_definitions, list_metric_imports, metric_csv_template, metric_trend, monitoring_overview, recent_memory, rebuild_knowledge_index, reject_approval, rollback_knowledge_document, save_knowledge_evaluation_feedback, seed_demo_data, seed_metric_demo_data, system_metrics, table_snapshot, update_knowledge_document, write_audit
from app.chat_service import AgentChatService
from app.evaluation import available_evaluation_cases, run_evaluation
from app.skills import SkillRegistry, run_skill
from app.tool_registry import catalog, definition, invoke
from app.workflow import SqlAgentWorkflow
from app.rag import KnowledgeRag
from app.chroma_store import chroma_collection_catalog, chroma_collection_records, chroma_stats, rebuild_chroma_index
from app.knowledge_evaluation import run_knowledge_evaluation
from app.policy import permitted, policy_summary, role_catalog


@asynccontextmanager
# 作用：说明函数 lifespan 的输入、输出与安全边界，避免调用方越过受控流程。
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 服务启动时确保 SQLite、演示数据和可重建的知识索引处于可用状态。
    initialize()
    seed_demo_data()
    seed_metric_demo_data()
    rebuild_knowledge_index()
    try:
        rebuild_chroma_index()
    except Exception:
        # SQLite/RAG remains available even if the optional local index needs repair.
        pass
    yield


app = FastAPI(title="安全可控 SQL Agent", version="0.1.0", lifespan=lifespan)
workflow = SqlAgentWorkflow()
chat_service = AgentChatService()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class QueryRequest(BaseModel):
    """智能查询入口的请求体。

    前端传入的 role 只是一项请求声明，最终权限仍由后端 RBAC、工具风险和 SQL 审查共同决定。
    """
    # 用户自然语言问题，进入 LangGraph 的 question 状态。
    question: str = Field(min_length=2, max_length=500)
    # 审计记录和会话记忆使用的请求者标识。
    requester: str = Field(default="anonymous", min_length=1, max_length=64)
    # 当前身份，用于决定是否可发起变更或审批。
    role: str = Field(default="operator", min_length=1, max_length=32)


class KnowledgeDocumentRequest(BaseModel):
    """知识库文档新增或编辑请求。"""
    # 文档标题，也是检索结果和引用证据的主要显示名称。
    title: str = Field(min_length=2, max_length=100)
    # 文档正文，保存后会重新分块并写入 SQLite/Chroma 索引。
    content: str = Field(min_length=10, max_length=4000)
    # 逗号分隔标签，用于筛选、检索增强和过期治理。
    tags: str = Field(default="未分类", max_length=200)
    # 可选的过期日期，格式由前端约束为 YYYY-MM-DD。
    expires_at: str = Field(default="", max_length=10)
    # 发起新增或编辑的角色，服务端会校验写权限。
    role: str = Field(default="operator", min_length=1, max_length=32)
    # 审计中记录的操作者。
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


# 作用：说明类 KnowledgeUploadRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class KnowledgeUploadRequest(KnowledgeDocumentRequest):
    """上传文档的请求体；上传正文允许比普通编辑更长。"""
    # 上传文件的原始名称，仅用于显示和审计，不作为本地路径执行。
    content: str = Field(min_length=10, max_length=100000)
    filename: str = Field(min_length=1, max_length=180)


# 作用：说明类 MutationActorRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class MutationActorRequest(BaseModel):
    """所有会产生副作用的 API 共用的操作者信息。"""
    # 后端权限检查使用的角色 ID。
    role: str = Field(default="operator", min_length=1, max_length=32)
    # 审计日志中的操作者名称。
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


# 作用：说明类 AuditDeleteRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class AuditDeleteRequest(MutationActorRequest):
    """删除单条审计记录时的二次确认。"""
    # 必须显式传 true，避免误触发不可逆删除。
    confirm: bool = False


# 作用：说明类 AuditCleanupRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class AuditCleanupRequest(MutationActorRequest):
    """按起止时间批量清理审计记录的请求体。"""
    # 清理窗口的起始时间，包含边界。
    start: str = Field(min_length=10, max_length=40)
    # 清理窗口的结束时间，包含边界。
    end: str = Field(min_length=10, max_length=40)
    # 必须显式确认后才会执行预览结果对应的删除。
    confirm: bool = False


# 作用：说明类 ToolInvokeRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class ToolInvokeRequest(MutationActorRequest):
    """工具中心试运行请求；工具名来自 URL，权限和风险仍由后端判断。"""
    pass


# 作用：说明类 SkillRunRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class SkillRunRequest(MutationActorRequest):
    """运行一个固定 Skill 时传入的补充上下文。"""
    # 例如 P1、区域或指标编号，交给固定 Skill 解析而非拼接任意 SQL。
    user_input: str = Field(default="", max_length=500)


# 作用：说明类 ChatConversationRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class ChatConversationRequest(MutationActorRequest):
    """创建 Agent 聊天会话的请求体。"""
    # 左侧历史会话显示名称。
    title: str = Field(default="新对话", max_length=48)


# 作用：说明类 ChatTurnRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class ChatTurnRequest(MutationActorRequest):
    """向已有会话追加一轮用户消息。"""
    # 当前轮的自然语言内容，聊天服务会结合历史上下文调用模型。
    content: str = Field(min_length=1, max_length=4000)


# 作用：说明类 ApprovalActionRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class ApprovalActionRequest(BaseModel):
    """审批人对一张变更审批单的处理意见。"""
    # 必须拥有 approve_change 权限。
    role: str = Field(default="approver", min_length=1, max_length=32)
    # 审批记录中的实际处理人。
    actor: str = Field(default="Lenovo", min_length=1, max_length=64)
    # 同意或拒绝时附带的业务理由。
    comment: str = Field(default="", max_length=500)


# 作用：说明类 EvaluationCaseRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class EvaluationCaseRequest(MutationActorRequest):
    """新增一条可重复运行的 Agent 评测用例。"""
    # 用例展示名称。
    name: str = Field(min_length=2, max_length=80)
    # 送入真实工作流的用户问题。
    question: str = Field(min_length=2, max_length=500)
    # 期望的业务状态，例如 completed、answered_by_rag 或 approval_required。
    expected_status: str = Field(default="completed", min_length=1, max_length=32)


# 作用：说明类 EvaluationRunRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class EvaluationRunRequest(BaseModel):
    """评测运行范围选择。"""
    # baseline 只跑内置用例，all 包含用户自定义用例，selected 只跑 case_ids。
    scope: str = Field(default="baseline", pattern="^(baseline|all|selected)$")
    # scope=selected 时要执行的用例 ID 列表。
    case_ids: list[str] = Field(default_factory=list, max_length=100)
    # 评测请求使用的角色，默认采用只读观察者。
    role: str = Field(default="viewer", min_length=1, max_length=32)
    # 评测审计记录中的请求者。
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


# 作用：说明类 KnowledgeEvaluationFeedbackRequest 的输入、输出与安全边界，避免调用方越过受控流程。
class KnowledgeEvaluationFeedbackRequest(MutationActorRequest):
    """人工对一次知识检索结果进行评分的请求。"""
    # 评测运行或单条结果的关联 ID。
    evaluation_id: str = Field(min_length=1, max_length=64)
    # 被评审的原始问题。
    question: str = Field(min_length=2, max_length=500)
    # 1 到 5 分的人工相关性评分。
    score: int = Field(ge=1, le=5)
    # 可选的具体改进建议。
    comment: str = Field(default="", max_length=1000)


# ---------- Agent 聊天：会话、历史消息和流式回复 ----------

@app.get("/health")
# 作用：说明函数 health 的输入、输出与安全边界，避免调用方越过受控流程。
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
# 作用：说明函数 console 的输入、输出与安全边界，避免调用方越过受控流程。
def console() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/v1/chat/conversations")
# 作用：说明函数 create_chat_conversation 的输入、输出与安全边界，避免调用方越过受控流程。
def create_chat_conversation(request: ChatConversationRequest) -> dict:
    # 聊天会话创建先走 RBAC，再写入会话审计事件。
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    conversation = create_chat_conversation_record(request.requester, request.title)
    write_audit(request.requester, "chat_conversation_created", {"conversation_id": conversation["id"], "role": request.role})
    return conversation


@app.get("/api/v1/chat/conversations")
# 作用：说明函数 chat_conversations 的输入、输出与安全边界，避免调用方越过受控流程。
def chat_conversations(requester: str = "Lenovo", role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    return list_chat_conversations(requester)


@app.get("/api/v1/chat/conversations/{conversation_id}/messages")
# 作用：说明函数 chat_messages 的输入、输出与安全边界，避免调用方越过受控流程。
def chat_messages(conversation_id: str, requester: str = "Lenovo", role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    messages = get_chat_messages(conversation_id, requester)
    if messages is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    return messages


@app.delete("/api/v1/chat/conversations/{conversation_id}")
# 作用：说明函数 delete_chat 的输入、输出与安全边界，避免调用方越过受控流程。
def delete_chat(conversation_id: str, request: MutationActorRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    if not delete_chat_conversation(conversation_id, request.requester):
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    write_audit(request.requester, "chat_conversation_deleted", {"conversation_id": conversation_id, "role": request.role})
    return {"deleted": True}


@app.post("/api/v1/chat/conversations/{conversation_id}/messages")
# 作用：说明函数 chat_turn 的输入、输出与安全边界，避免调用方越过受控流程。
def chat_turn(conversation_id: str, request: ChatTurnRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    if add_chat_message(conversation_id, request.requester, "user", request.content) is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    history = get_chat_messages(conversation_id, request.requester)
    if history is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    content, provider = chat_service.reply([{"role": item["role"], "content": item["content"]} for item in history])
    message = add_chat_message(conversation_id, request.requester, "assistant", content)
    write_audit(request.requester, "agent_chat_completed", {"conversation_id": conversation_id, "provider": provider, "role": request.role})
    return {"conversation_id": conversation_id, "message": message, "provider": provider}


@app.post("/api/v1/chat/conversations/{conversation_id}/messages/stream")
def chat_turn_stream(conversation_id: str, request: ChatTurnRequest) -> StreamingResponse:
    """Stream planning stages and model deltas, then persist the completed assistant turn."""
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    if add_chat_message(conversation_id, request.requester, "user", request.content) is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    history = get_chat_messages(conversation_id, request.requester)
    if history is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    messages = [{"role": item["role"], "content": item["content"]} for item in history]
    plan = chat_service.plan_turn(request.content, messages)

    def event_stream() -> Iterator[str]:
        def emit(name: str, payload: dict) -> str:
            return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield emit("stage", {"stage": "context", "message": f"已加载最近 {plan['context_messages']} 条会话消息"})
        yield emit("stage", {"stage": "plan", "message": plan["route"], "steps": plan["steps"]})
        chunks: list[str] = []
        provider = "fallback"
        try:
            for item in chat_service.stream_reply(messages, plan):
                provider = item.get("provider", provider)
                if item.get("type") == "token":
                    chunks.append(item["content"])
                    yield emit("token", {"content": item["content"], "provider": provider})
            content = "".join(chunks)
            message = add_chat_message(conversation_id, request.requester, "assistant", content)
            write_audit(request.requester, "agent_chat_stream_completed", {
                "conversation_id": conversation_id, "provider": provider, "role": request.role,
                "context_messages": plan["context_messages"], "route": plan["route"],
            })
            yield emit("done", {"conversation_id": conversation_id, "provider": provider, "message": message, "plan": plan})
        except Exception as exc:  # keep the browser informed if the stream fails after it starts
            yield emit("error", {"message": "流式聊天失败：" + str(exc)[:180]})

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 智能查询：同步结果与 SSE 工作流轨迹 ----------

@app.post("/api/v1/query")
# 作用：说明函数 query 的输入、输出与安全边界，避免调用方越过受控流程。
def query(request: QueryRequest) -> dict:
    return workflow.run(request.question, request.requester, request.role, use_model_tools=settings.model_tool_planner_enabled).to_dict()


@app.post("/api/v1/query/stream")
# 作用：说明函数 stream_query 的输入、输出与安全边界，避免调用方越过受控流程。
def stream_query(request: QueryRequest) -> StreamingResponse:
    def event_stream() -> Iterator[str]:
        queue: Queue[tuple[str, dict]] = Queue()
        completed = Event()

        def worker() -> None:
            try:
                result = workflow.run(request.question, request.requester, request.role, lambda event: queue.put(("stage", event.to_dict())), use_model_tools=settings.model_tool_planner_enabled)
                queue.put(("result", result.to_dict()))
            except Exception as exc:  # errors remain structured for the browser client
                queue.put(("error", {"message": "工作流执行失败", "type": type(exc).__name__}))
            finally:
                completed.set()

        Thread(target=worker, daemon=True).start()
        while not completed.is_set() or not queue.empty():
            try:
                event_name, payload = queue.get(timeout=0.5)
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Empty:
                yield ": keepalive\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/v1/approvals/{approval_id}/approve")
# 作用：说明函数 approve_request 的输入、输出与安全边界，避免调用方越过受控流程。
def approve_request(approval_id: str, request: ApprovalActionRequest = ApprovalActionRequest()) -> dict:
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="当前角色无审批权限。请切换到值班负责人。")
    approval = approve(approval_id, request.actor, request.comment)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if approval["status"] == "expired":
        write_audit(approval["requester"], "approval_expired", {"approval_id": approval_id, "approver": request.actor})
        return {"status": "expired", "approval_id": approval_id, "message": "审批单已过期（有效期 30 分钟），没有执行任何 SQL。请重新发起变更。"}
    execution = execute_approved(approval_id, settings.allow_approved_writes)
    if execution is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    outcome = execution.get("outcome")
    write_audit(approval["requester"], "approval_resolved", {"approval_id": approval_id, "outcome": outcome, "approver_role": request.role, "approver": request.actor, "comment": request.comment})
    messages = {
        "executed": "审批完成，已执行受控 Demo 操作。",
        "safe_mode": "审批已记录为“安全模式已批准”。未执行写库；影响范围仅作预估并已留痕。",
        "not_allowlisted": "审批已记录，但该 SQL 不在 Demo 执行白名单内。",
        "not_approved": "审批单当前不处于可执行状态。",
    }
    return {"status": execution["status"], "approval_id": approval_id, "outcome": outcome, "message": messages.get(outcome, "审批状态已更新。")}


@app.post("/api/v1/approvals/{approval_id}/reject")
# 作用：说明函数 reject_request 的输入、输出与安全边界，避免调用方越过受控流程。
def reject_request(approval_id: str, request: ApprovalActionRequest = ApprovalActionRequest()) -> dict:
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="当前角色无审批权限。请切换到值班负责人。")
    approval = reject_approval(approval_id, request.actor, request.comment)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if approval["status"] != "rejected":
        return {"status": approval["status"], "approval_id": approval_id, "message": "审批单当前已不是待处理状态。"}
    write_audit(approval["requester"], "approval_rejected", {"approval_id": approval_id, "approver_role": request.role, "approver": request.actor, "comment": approval["decision_comment"]})
    return {"status": "rejected", "approval_id": approval_id, "message": "审批已拒绝；没有执行任何 SQL，也没有修改业务数据。"}


@app.delete("/api/v1/approvals/{approval_id}")
# 作用：说明函数 remove_approval 的输入、输出与安全边界，避免调用方越过受控流程。
def remove_approval(approval_id: str, request: ApprovalActionRequest = ApprovalActionRequest()) -> dict:
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="当前角色无删除审批记录权限。请切换到值班负责人。")
    approval = delete_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if not approval["deletable"]:
        raise HTTPException(status_code=409, detail="待审批单不能直接删除，请先拒绝或完成审批。")
    write_audit(approval["requester"], "approval_deleted", {
        "approval_id": approval_id, "status": approval["status"], "reason": approval["reason"],
        "deleted_by": request.actor, "approver_role": request.role,
    })
    return {"deleted": True, "message": "审批记录已删除；删除操作已保留在审计中心。"}


@app.get("/api/v1/audit")
# 作用：说明函数 audit 的输入、输出与安全边界，避免调用方越过受控流程。
def audit(limit: int = 50, offset: int = 0) -> list[dict]:
    return list_audit(min(max(limit, 1), 200), max(offset, 0))


@app.get("/api/v1/audit/cleanup-preview")
# 作用：说明函数 audit_cleanup_check 的输入、输出与安全边界，避免调用方越过受控流程。
def audit_cleanup_check(start: str, end: str, role: str = "viewer") -> dict:
    if not permitted(role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以预览审计清理范围。")
    try:
        return audit_cleanup_preview(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/v1/audit/range")
# 作用：说明函数 cleanup_audit 的输入、输出与安全边界，避免调用方越过受控流程。
def cleanup_audit(request: AuditCleanupRequest) -> dict:
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以清理审计记录。")
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请确认按时间范围清理审计记录。")
    try:
        return delete_audit_range(request.start, request.end, request.requester)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/v1/audit/{event_id}")
# 作用：说明函数 delete_audit 的输入、输出与安全边界，避免调用方越过受控流程。
def delete_audit(event_id: str, request: AuditDeleteRequest) -> dict:
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以删除审计记录。")
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请确认删除该审计记录。")
    result = delete_audit_event(event_id, request.requester)
    if result is None:
        raise HTTPException(status_code=404, detail="审计记录不存在或已被删除。")
    return result


@app.get("/api/v1/audit/integrity")
# 作用：说明函数 verify_audit_integrity 的输入、输出与安全边界，避免调用方越过受控流程。
def verify_audit_integrity(scope: str = "full") -> dict:
    if scope not in {"full", "recent_100"}:
        raise HTTPException(status_code=400, detail="校验范围必须是 full 或 recent_100")
    return audit_integrity(None if scope == "full" else 100)


@app.get("/api/v1/audit/summary")
# 作用：说明函数 audit_summary 的输入、输出与安全边界，避免调用方越过受控流程。
def audit_summary() -> dict:
    return {"total": audit_count()}


# ---------- 数据浏览器、Skills 和知识库 ----------

@app.get("/api/v1/data/tables")
# 作用：说明函数 explorer_catalog 的输入、输出与安全边界，避免调用方越过受控流程。
def explorer_catalog(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无数据浏览权限。")
    return data_catalog()


@app.get("/api/v1/data/tables/{table_name}")
# 作用：说明函数 explorer_table 的输入、输出与安全边界，避免调用方越过受控流程。
def explorer_table(table_name: str, role: str = "viewer", limit: int = 30, offset: int = 0) -> dict:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无数据浏览权限。")
    snapshot = table_snapshot(table_name, min(max(limit, 1), 100), max(offset, 0))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="该表不在数据浏览器授权范围内。")
    write_audit("Lenovo", "data_explorer_viewed", {"table": table_name, "limit": snapshot["limit"], "offset": snapshot["offset"], "role": role})
    return snapshot


@app.get("/api/v1/skills")
# 作用：说明函数 skills 的输入、输出与安全边界，避免调用方越过受控流程。
def skills() -> list[dict]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    return [{"name": item.name, "description": item.description, "category": item.category, "risk": item.risk, "suggestions": list(item.suggestions), "runnable": item.runnable} for item in registry.load()]


@app.get("/api/v1/skills/history")
# 作用：说明函数 skills_history 的输入、输出与安全边界，避免调用方越过受控流程。
def skills_history(role: str = "viewer", limit: int = 8) -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Skill 运行记录查看权限。")
    return [event for event in list_audit(limit=200) if event["action"] == "skill_run"][:min(max(limit, 1), 30)]


@app.get("/api/v1/skills/{skill_name}")
# 作用：说明函数 skill_detail 的输入、输出与安全边界，避免调用方越过受控流程。
def skill_detail(skill_name: str) -> dict[str, str]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    for item in registry.load():
        if item.name == skill_name:
            return {"name": item.name, "description": item.description, "content": item.content}
    raise HTTPException(status_code=404, detail="Skill 不存在")


@app.post("/api/v1/skills/{skill_name}/run")
# 作用：说明函数 execute_skill 的输入、输出与安全边界，避免调用方越过受控流程。
def execute_skill(skill_name: str, request: SkillRunRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Skill 运行权限。")
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    if not skill.runnable:
        raise HTTPException(status_code=400, detail="该 Skill 是规范型能力，请在智能查询或对应页面中使用。")
    result = run_skill(skill_name, request.user_input)
    write_audit(request.requester, "skill_run", {"skill": skill_name, "input": request.user_input[:160], "status": result["status"], "role": request.role})
    return result


@app.get("/api/v1/knowledge")
# 作用：说明函数 knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def knowledge(role: str = "viewer", limit: int = 5, offset: int = 0, tag: str = "", status: str = "") -> dict:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    bounded_limit = min(max(limit, 1), 20)
    bounded_offset = max(offset, 0)
    return {
        "items": list_knowledge_documents(bounded_limit, bounded_offset, tag, status),
        "total": knowledge_document_count(tag, status),
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@app.get("/api/v1/knowledge/tags")
# 作用：说明函数 knowledge_tag_catalog 的输入、输出与安全边界，避免调用方越过受控流程。
def knowledge_tag_catalog(role: str = "viewer") -> list[str]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return knowledge_tags()


@app.get("/api/v1/knowledge/{document_id}/chunks")
# 作用：说明函数 knowledge_document_chunks 的输入、输出与安全边界，避免调用方越过受控流程。
def knowledge_document_chunks(document_id: int, role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return document_chunks(document_id)


@app.get("/api/v1/knowledge/{document_id}/versions")
# 作用：说明函数 knowledge_document_versions 的输入、输出与安全边界，避免调用方越过受控流程。
def knowledge_document_versions(document_id: int, role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库版本查看权限。")
    return list_knowledge_versions(document_id)


@app.get("/api/v1/knowledge/search")
# 作用：说明函数 search_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def search_knowledge(query: str, limit: int = 3, role: str = "viewer") -> list[dict]:
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索权限。")
    return KnowledgeRag().search(query, min(max(limit, 1), 10))


@app.get("/api/v1/knowledge/chroma")
# 作用：说明函数 knowledge_chroma 的输入、输出与安全边界，避免调用方越过受控流程。
def knowledge_chroma(role: str = "viewer") -> dict:
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无知识向量索引查看权限。")
    try:
        return chroma_stats()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chroma 尚未就绪：{exc}") from exc


@app.get("/api/v1/chroma/collections")
# 作用：说明函数 chroma_collections 的输入、输出与安全边界，避免调用方越过受控流程。
def chroma_collections(role: str = "viewer") -> list[dict]:
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无 Chroma 集合查看权限。")
    try:
        return chroma_collection_catalog()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chroma 尚未就绪：{exc}") from exc


@app.get("/api/v1/chroma/collections/{collection_name}")
# 作用：说明函数 chroma_collection 的输入、输出与安全边界，避免调用方越过受控流程。
def chroma_collection(collection_name: str, limit: int = 100, offset: int = 0, role: str = "viewer") -> list[dict]:
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无 Chroma 数据查看权限。")
    try:
        return chroma_collection_records(collection_name, limit, offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chroma 记录读取失败：{exc}") from exc


@app.post("/api/v1/knowledge/evaluation")
# 作用：说明函数 evaluate_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def evaluate_knowledge(request: MutationActorRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索评测权限。")
    report = run_knowledge_evaluation()
    write_audit(request.requester, "knowledge_evaluation_completed", {"total": report["total"], "passed": report["passed"], "hit_at_3": report["hit_at_3"], "role": request.role})
    return report


@app.post("/api/v1/knowledge/evaluation/feedback")
# 作用：说明函数 evaluate_knowledge_feedback 的输入、输出与安全边界，避免调用方越过受控流程。
def evaluate_knowledge_feedback(request: KnowledgeEvaluationFeedbackRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无提交知识库人工评分权限。")
    feedback = save_knowledge_evaluation_feedback(request.evaluation_id, request.question, request.score, request.comment, request.requester)
    summary = knowledge_evaluation_feedback_summary(request.evaluation_id)
    write_audit(request.requester, "knowledge_evaluation_feedback", {"evaluation_id": request.evaluation_id, "question": request.question, "score": request.score, "role": request.role})
    return {"feedback": feedback, "summary": summary}


@app.post("/api/v1/knowledge/reindex")
# 作用：说明函数 reindex_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def reindex_knowledge(request: MutationActorRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    chunks = rebuild_knowledge_index()
    chroma = rebuild_chroma_index()
    write_audit(request.requester, "knowledge_reindexed", {"chunk_count": chunks, "chroma_total": chroma["total"], "role": request.role})
    return {"status": "completed", "chunk_count": chunks, "chroma": chroma}


@app.post("/api/v1/knowledge", status_code=201)
# 作用：说明函数 create_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def create_knowledge(request: KnowledgeDocumentRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = add_knowledge_document(request.title, request.content, request.tags, request.expires_at)
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_created", {"document_id": document["id"], "title": document["title"], "role": request.role})
    return document


@app.post("/api/v1/knowledge/upload", status_code=201)
# 作用：说明函数 upload_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def upload_knowledge(request: KnowledgeUploadRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    suffix = Path(request.filename).suffix.lower()
    if suffix not in {".txt", ".md"}:
        raise HTTPException(status_code=400, detail="目前仅支持上传 .txt 或 .md 文档。")
    document = add_knowledge_document(request.title, request.content, request.tags, request.expires_at)
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_uploaded", {"document_id": document["id"], "filename": request.filename, "role": request.role})
    return document


@app.put("/api/v1/knowledge/{document_id}")
# 作用：说明函数 edit_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def edit_knowledge(document_id: int, request: KnowledgeDocumentRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = update_knowledge_document(document_id, request.title, request.content, request.tags, request.expires_at)
    if document is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_updated", {"document_id": document_id, "version": document["version"], "role": request.role})
    return document


@app.post("/api/v1/knowledge/{document_id}/rollback/{version}")
# 作用：说明函数 rollback_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def rollback_knowledge(document_id: int, version: int, request: MutationActorRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = rollback_knowledge_document(document_id, version)
    if document is None:
        raise HTTPException(status_code=404, detail="目标版本或知识文档不存在")
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_rolled_back", {"document_id": document_id, "source_version": version, "new_version": document["version"], "role": request.role})
    return document


@app.delete("/api/v1/knowledge/{document_id}")
# 作用：说明函数 delete_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def delete_knowledge(document_id: int, request: MutationActorRequest) -> None:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    if not delete_knowledge_document(document_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_deleted", {"document_id": document_id, "role": request.role})


# ---------- 指标、工具、记忆和离线评测 ----------

@app.get("/api/v1/metrics")
# 作用：说明函数 metrics 的输入、输出与安全边界，避免调用方越过受控流程。
def metrics() -> dict:
    return {**system_metrics(), "llm_enabled": settings.llm_enabled, "approved_writes_enabled": settings.allow_approved_writes}


@app.get("/api/v1/approvals")
# 作用：说明函数 approvals 的输入、输出与安全边界，避免调用方越过受控流程。
def approvals(limit: int = 100, offset: int = 0, status: str = "") -> list[dict]:
    if status and status not in APPROVAL_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的审批状态筛选")
    return list_approvals(min(max(limit, 1), 200), max(offset, 0), status)


@app.get("/api/v1/approvals/summary")
# 作用：说明函数 approval_summary 的输入、输出与安全边界，避免调用方越过受控流程。
def approval_summary(status: str = "") -> dict:
    if status and status not in APPROVAL_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的审批状态筛选")
    return {"total": approval_count(status), "status_counts": approval_status_counts()}


@app.get("/api/v1/policies")
# 作用：说明函数 policies 的输入、输出与安全边界，避免调用方越过受控流程。
def policies() -> dict:
    return {"roles": role_catalog(), "policies": policy_summary()}


@app.get("/api/v1/monitoring/overview")
# 作用：说明函数 monitoring 的输入、输出与安全边界，避免调用方越过受控流程。
def monitoring() -> dict:
    overview = monitoring_overview()
    try:
        status = chroma_stats()
        overview.setdefault("health_checks", []).append({"name": "Chroma 向量库", "status": "healthy", "detail": f"{len(status['collections'])} 个集合 · {status['total']} 条索引"})
    except Exception as exc:
        overview.setdefault("health_checks", []).append({"name": "Chroma 向量库", "status": "degraded", "detail": f"索引不可用：{exc}"})
    return overview


@app.get("/api/v1/metric-definitions")
# 作用：说明函数 metric_definitions 的输入、输出与安全边界，避免调用方越过受控流程。
def metric_definitions(keyword: str = "", category: str = "", limit: int = 60) -> list[dict]:
    return list_metric_definitions(keyword.strip(), category.strip(), min(max(limit, 1), 100))


@app.get("/api/v1/metric-definitions/{metric_id}/trend")
# 作用：说明函数 metric_definition_trend 的输入、输出与安全边界，避免调用方越过受控流程。
def metric_definition_trend(metric_id: int, points: int = 24) -> dict:
    trend = metric_trend(metric_id, min(max(points, 2), 48))
    if trend is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    return trend


@app.get("/api/v1/metrics/import-template")
# 作用：说明函数 metric_import_template 的输入、输出与安全边界，避免调用方越过受控流程。
def metric_import_template() -> PlainTextResponse:
    return PlainTextResponse(
        metric_csv_template(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="metric-import-template.csv"'},
    )


@app.get("/api/v1/metrics/imports")
# 作用：说明函数 metric_imports 的输入、输出与安全边界，避免调用方越过受控流程。
def metric_imports(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无指标导入记录查看权限。")
    return list_metric_imports()


@app.post("/api/v1/metrics/import", status_code=201)
# 作用：说明函数 import_metrics_csv 的输入、输出与安全边界，避免调用方越过受控流程。
async def import_metrics_csv(request: Request, filename: str = "metrics.csv", role: str = "operator", requester: str = "Lenovo") -> dict:
    if not permitted(role, "request_change"):
        write_audit(requester, "metric_import_denied", {"filename": filename[:180], "role": role})
        raise HTTPException(status_code=403, detail="观察者角色不能导入真实指标 CSV。请切换到运维工程师或值班负责人。")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="请选择包含指标数据的 CSV 文件。")
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV 文件不能超过 5 MB。")
    try:
        content = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV 必须使用 UTF-8 或 UTF-8 BOM 编码保存。") from exc
    try:
        result = import_metric_csv(content, filename, requester)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(requester, "metric_csv_imported", {"filename": filename[:180], "role": role, **result})
    return result


@app.get("/api/v1/tools")
# 作用：说明函数 tools_catalog 的输入、输出与安全边界，避免调用方越过受控流程。
def tools_catalog() -> list[dict[str, str]]:
    return catalog()


@app.get("/api/v1/tools/history")
# 作用：说明函数 tools_history 的输入、输出与安全边界，避免调用方越过受控流程。
def tools_history(role: str = "viewer", limit: int = 8) -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无工具调用记录查看权限。")
    actions = {"tool_invoked", "tool_approval_requested", "tool_access_denied"}
    return [event for event in list_audit(limit=200) if event["action"] in actions][:min(max(limit, 1), 30)]


@app.post("/api/v1/tools/{tool_name}/invoke")
# 作用：说明函数 invoke_tool 的输入、输出与安全边界，避免调用方越过受控流程。
def invoke_tool(tool_name: str, request: ToolInvokeRequest = ToolInvokeRequest(role="viewer", requester="Lenovo")) -> dict:
    tool = definition(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail="工具不存在")
    required_permission = "request_change" if tool.risk == "manual" else "read"
    if not permitted(request.role, required_permission):
        write_audit(request.requester, "tool_access_denied", {"tool": tool_name, "role": request.role, "required_permission": required_permission})
        raise HTTPException(status_code=403, detail="当前角色无该工具调用权限。")
    if tool.risk == "manual":
        approval_id = create_approval(request.requester, f"TOOL {tool_name}", f"工具 {tool.name} 会改变运维状态，需要人工审批。")
        write_audit(request.requester, "tool_approval_requested", {"tool": tool_name, "approval_id": approval_id, "role": request.role})
        return {"status": "approval_required", "tool": tool_name, "approval_id": approval_id, "message": "已创建工具变更审批单，请前往审批中心确认。"}
    result = invoke(tool_name)
    write_audit(request.requester, "tool_invoked", {"tool": tool_name, "status": result["status"], "role": request.role})
    return result


@app.get("/api/v1/memory/{requester}")
# 作用：说明函数 memory 的输入、输出与安全边界，避免调用方越过受控流程。
def memory(requester: str, limit: int = 6) -> list[dict[str, str]]:
    return recent_memory(requester, min(max(limit, 1), 30))


@app.get("/api/v1/evaluations/cases")
# 作用：说明函数 evaluation_cases 的输入、输出与安全边界，避免调用方越过受控流程。
def evaluation_cases(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无评测用例查看权限。")
    return available_evaluation_cases()


@app.post("/api/v1/evaluations/cases", status_code=201)
# 作用：说明函数 create_evaluation_case 的输入、输出与安全边界，避免调用方越过受控流程。
def create_evaluation_case(request: EvaluationCaseRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="观察者角色不能新增评测用例。")
    try:
        case = add_evaluation_case(request.name, request.question, request.expected_status, request.requester)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "evaluation_case_created", {"case_id": case["id"], "name": case["name"], "expected_status": case["expected_status"], "role": request.role})
    return {**case, "source": "custom"}


@app.delete("/api/v1/evaluations/cases/{case_id}")
# 作用：说明函数 remove_evaluation_case 的输入、输出与安全边界，避免调用方越过受控流程。
def remove_evaluation_case(case_id: str, request: MutationActorRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="观察者角色不能删除评测用例。")
    if not delete_evaluation_case(case_id):
        raise HTTPException(status_code=404, detail="自定义评测用例不存在。")
    write_audit(request.requester, "evaluation_case_deleted", {"case_id": case_id, "role": request.role})
    return {"deleted": True}


@app.post("/api/v1/evaluations/run")
# 作用：说明函数 evaluate 的输入、输出与安全边界，避免调用方越过受控流程。
def evaluate(request: EvaluationRunRequest = EvaluationRunRequest()) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无运行评测权限。")
    try:
        report = run_evaluation(request.scope, request.case_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "evaluation_completed", {"scope": report["scope"], "dataset_size": report["dataset_size"], "passed": report["passed"], "role": request.role})
    return report
