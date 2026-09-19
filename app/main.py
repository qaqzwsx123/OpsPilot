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
from app.database import add_chat_message, add_evaluation_case, add_knowledge_document, approve, approval_count, audit_count, audit_integrity, create_approval, create_chat_conversation as create_chat_conversation_record, data_catalog, delete_evaluation_case, delete_chat_conversation, delete_knowledge_document, document_chunks, execute_approved, get_chat_messages, import_metric_csv, initialize, knowledge_document_count, knowledge_tags, list_approvals, list_audit, list_chat_conversations, list_knowledge_documents, list_metric_definitions, list_metric_imports, metric_csv_template, metric_trend, monitoring_overview, recent_memory, rebuild_knowledge_index, reject_approval, seed_demo_data, seed_metric_demo_data, system_metrics, table_snapshot, write_audit
from app.chat_service import AgentChatService
from app.evaluation import available_evaluation_cases, run_evaluation
from app.skills import SkillRegistry, run_skill
from app.tool_registry import catalog, definition, invoke
from app.workflow import SqlAgentWorkflow
from app.rag import KnowledgeRag
from app.knowledge_evaluation import run_knowledge_evaluation
from app.policy import permitted, policy_summary, role_catalog


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize()
    seed_demo_data()
    seed_metric_demo_data()
    rebuild_knowledge_index()
    yield


app = FastAPI(title="安全可控 SQL Agent", version="0.1.0", lifespan=lifespan)
workflow = SqlAgentWorkflow()
chat_service = AgentChatService()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    requester: str = Field(default="anonymous", min_length=1, max_length=64)
    role: str = Field(default="operator", min_length=1, max_length=32)


class KnowledgeDocumentRequest(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=10, max_length=4000)
    tags: str = Field(default="未分类", max_length=200)
    expires_at: str = Field(default="", max_length=10)
    role: str = Field(default="operator", min_length=1, max_length=32)
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


class MutationActorRequest(BaseModel):
    role: str = Field(default="operator", min_length=1, max_length=32)
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


class ToolInvokeRequest(MutationActorRequest):
    pass


class SkillRunRequest(MutationActorRequest):
    user_input: str = Field(default="", max_length=500)


class ChatConversationRequest(MutationActorRequest):
    title: str = Field(default="新对话", max_length=48)


class ChatTurnRequest(MutationActorRequest):
    content: str = Field(min_length=1, max_length=4000)


class ApprovalActionRequest(BaseModel):
    role: str = Field(default="approver", min_length=1, max_length=32)
    actor: str = Field(default="Lenovo", min_length=1, max_length=64)
    comment: str = Field(default="", max_length=500)


class EvaluationCaseRequest(MutationActorRequest):
    name: str = Field(min_length=2, max_length=80)
    question: str = Field(min_length=2, max_length=500)
    expected_status: str = Field(default="completed", min_length=1, max_length=32)


class EvaluationRunRequest(BaseModel):
    scope: str = Field(default="baseline", regex="^(baseline|all|selected)$")
    case_ids: list[str] = Field(default_factory=list, max_length=100)
    role: str = Field(default="viewer", min_length=1, max_length=32)
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/v1/chat/conversations")
def create_chat_conversation(request: ChatConversationRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    conversation = create_chat_conversation_record(request.requester, request.title)
    write_audit(request.requester, "chat_conversation_created", {"conversation_id": conversation["id"], "role": request.role})
    return conversation


@app.get("/api/v1/chat/conversations")
def chat_conversations(requester: str = "Lenovo", role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    return list_chat_conversations(requester)


@app.get("/api/v1/chat/conversations/{conversation_id}/messages")
def chat_messages(conversation_id: str, requester: str = "Lenovo", role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    messages = get_chat_messages(conversation_id, requester)
    if messages is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    return messages


@app.delete("/api/v1/chat/conversations/{conversation_id}")
def delete_chat(conversation_id: str, request: MutationActorRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    if not delete_chat_conversation(conversation_id, request.requester):
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    write_audit(request.requester, "chat_conversation_deleted", {"conversation_id": conversation_id, "role": request.role})
    return {"deleted": True}


@app.post("/api/v1/chat/conversations/{conversation_id}/messages")
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


@app.post("/api/v1/query")
def query(request: QueryRequest) -> dict:
    return workflow.run(request.question, request.requester, request.role).to_dict()


@app.post("/api/v1/query/stream")
def stream_query(request: QueryRequest) -> StreamingResponse:
    def event_stream() -> Iterator[str]:
        queue: Queue[tuple[str, dict]] = Queue()
        completed = Event()

        def worker() -> None:
            try:
                result = workflow.run(request.question, request.requester, request.role, lambda event: queue.put(("stage", event.to_dict())))
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


@app.get("/api/v1/audit")
def audit(limit: int = 50, offset: int = 0) -> list[dict]:
    return list_audit(min(max(limit, 1), 200), max(offset, 0))


@app.get("/api/v1/audit/integrity")
def verify_audit_integrity(scope: str = "full") -> dict:
    if scope not in {"full", "recent_100"}:
        raise HTTPException(status_code=400, detail="校验范围必须是 full 或 recent_100")
    return audit_integrity(None if scope == "full" else 100)


@app.get("/api/v1/audit/summary")
def audit_summary() -> dict:
    return {"total": audit_count()}


@app.get("/api/v1/data/tables")
def explorer_catalog(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无数据浏览权限。")
    return data_catalog()


@app.get("/api/v1/data/tables/{table_name}")
def explorer_table(table_name: str, role: str = "viewer", limit: int = 30, offset: int = 0) -> dict:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无数据浏览权限。")
    snapshot = table_snapshot(table_name, min(max(limit, 1), 100), max(offset, 0))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="该表不在数据浏览器授权范围内。")
    write_audit("Lenovo", "data_explorer_viewed", {"table": table_name, "limit": snapshot["limit"], "offset": snapshot["offset"], "role": role})
    return snapshot


@app.get("/api/v1/skills")
def skills() -> list[dict]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    return [{"name": item.name, "description": item.description, "category": item.category, "risk": item.risk, "suggestions": list(item.suggestions), "runnable": item.runnable} for item in registry.load()]


@app.get("/api/v1/skills/history")
def skills_history(role: str = "viewer", limit: int = 8) -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Skill 运行记录查看权限。")
    return [event for event in list_audit(limit=200) if event["action"] == "skill_run"][:min(max(limit, 1), 30)]


@app.get("/api/v1/skills/{skill_name}")
def skill_detail(skill_name: str) -> dict[str, str]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    for item in registry.load():
        if item.name == skill_name:
            return {"name": item.name, "description": item.description, "content": item.content}
    raise HTTPException(status_code=404, detail="Skill 不存在")


@app.post("/api/v1/skills/{skill_name}/run")
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
def knowledge_tag_catalog(role: str = "viewer") -> list[str]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return knowledge_tags()


@app.get("/api/v1/knowledge/{document_id}/chunks")
def knowledge_document_chunks(document_id: int, role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return document_chunks(document_id)


@app.get("/api/v1/knowledge/search")
def search_knowledge(query: str, limit: int = 3, role: str = "viewer") -> list[dict]:
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索权限。")
    return KnowledgeRag().search(query, min(max(limit, 1), 10))


@app.post("/api/v1/knowledge/evaluation")
def evaluate_knowledge(request: MutationActorRequest) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索评测权限。")
    report = run_knowledge_evaluation()
    write_audit(request.requester, "knowledge_evaluation_completed", {"total": report["total"], "passed": report["passed"], "hit_at_3": report["hit_at_3"], "role": request.role})
    return report


@app.post("/api/v1/knowledge/reindex")
def reindex_knowledge(request: MutationActorRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    chunks = rebuild_knowledge_index()
    write_audit(request.requester, "knowledge_reindexed", {"chunk_count": chunks, "role": request.role})
    return {"status": "completed", "chunk_count": chunks}


@app.post("/api/v1/knowledge", status_code=201)
def create_knowledge(request: KnowledgeDocumentRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = add_knowledge_document(request.title, request.content, request.tags, request.expires_at)
    write_audit(request.requester, "knowledge_created", {"document_id": document["id"], "title": document["title"], "role": request.role})
    return document


@app.delete("/api/v1/knowledge/{document_id}")
def delete_knowledge(document_id: int, request: MutationActorRequest) -> None:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    if not delete_knowledge_document(document_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    write_audit(request.requester, "knowledge_deleted", {"document_id": document_id, "role": request.role})


@app.get("/api/v1/metrics")
def metrics() -> dict:
    return {**system_metrics(), "llm_enabled": settings.llm_enabled, "approved_writes_enabled": settings.allow_approved_writes}


@app.get("/api/v1/approvals")
def approvals(limit: int = 100, offset: int = 0) -> list[dict]:
    return list_approvals(min(max(limit, 1), 200), max(offset, 0))


@app.get("/api/v1/approvals/summary")
def approval_summary() -> dict:
    return {"total": approval_count()}


@app.get("/api/v1/policies")
def policies() -> dict:
    return {"roles": role_catalog(), "policies": policy_summary()}


@app.get("/api/v1/monitoring/overview")
def monitoring() -> dict:
    return monitoring_overview()


@app.get("/api/v1/metric-definitions")
def metric_definitions(keyword: str = "", category: str = "", limit: int = 60) -> list[dict]:
    return list_metric_definitions(keyword.strip(), category.strip(), min(max(limit, 1), 100))


@app.get("/api/v1/metric-definitions/{metric_id}/trend")
def metric_definition_trend(metric_id: int, points: int = 24) -> dict:
    trend = metric_trend(metric_id, min(max(points, 2), 48))
    if trend is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    return trend


@app.get("/api/v1/metrics/import-template")
def metric_import_template() -> PlainTextResponse:
    return PlainTextResponse(
        metric_csv_template(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="metric-import-template.csv"'},
    )


@app.get("/api/v1/metrics/imports")
def metric_imports(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无指标导入记录查看权限。")
    return list_metric_imports()


@app.post("/api/v1/metrics/import", status_code=201)
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
def tools_catalog() -> list[dict[str, str]]:
    return catalog()


@app.get("/api/v1/tools/history")
def tools_history(role: str = "viewer", limit: int = 8) -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无工具调用记录查看权限。")
    actions = {"tool_invoked", "tool_approval_requested", "tool_access_denied"}
    return [event for event in list_audit(limit=200) if event["action"] in actions][:min(max(limit, 1), 30)]


@app.post("/api/v1/tools/{tool_name}/invoke")
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
def memory(requester: str, limit: int = 6) -> list[dict[str, str]]:
    return recent_memory(requester, min(max(limit, 1), 30))


@app.get("/api/v1/evaluations/cases")
def evaluation_cases(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无评测用例查看权限。")
    return available_evaluation_cases()


@app.post("/api/v1/evaluations/cases", status_code=201)
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
def remove_evaluation_case(case_id: str, request: MutationActorRequest) -> dict:
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="观察者角色不能删除评测用例。")
    if not delete_evaluation_case(case_id):
        raise HTTPException(status_code=404, detail="自定义评测用例不存在。")
    write_audit(request.requester, "evaluation_case_deleted", {"case_id": case_id, "role": request.role})
    return {"deleted": True}


@app.post("/api/v1/evaluations/run")
def evaluate(request: EvaluationRunRequest = EvaluationRunRequest()) -> dict:
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无运行评测权限。")
    try:
        report = run_evaluation(request.scope, request.case_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "evaluation_completed", {"scope": report["scope"], "dataset_size": report["dataset_size"], "passed": report["passed"], "role": request.role})
    return report
