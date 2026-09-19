from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.database import add_knowledge_document, approve, audit_integrity, create_approval, data_catalog, delete_knowledge_document, execute_approved, initialize, list_approvals, list_audit, list_knowledge_documents, list_metric_definitions, metric_trend, monitoring_overview, recent_memory, rebuild_knowledge_index, reject_approval, seed_demo_data, seed_metric_demo_data, system_metrics, table_snapshot, write_audit
from app.evaluation import run_evaluation
from app.skills import SkillRegistry, run_skill
from app.tool_registry import catalog, definition, invoke
from app.workflow import SqlAgentWorkflow
from app.rag import KnowledgeRag
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
    role: str = Field(default="operator", min_length=1, max_length=32)
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


class MutationActorRequest(BaseModel):
    role: str = Field(default="operator", min_length=1, max_length=32)
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


class ToolInvokeRequest(MutationActorRequest):
    pass


class SkillRunRequest(MutationActorRequest):
    user_input: str = Field(default="", max_length=500)


class ApprovalActionRequest(BaseModel):
    role: str = Field(default="approver", min_length=1, max_length=32)
    actor: str = Field(default="Lenovo", min_length=1, max_length=64)
    comment: str = Field(default="", max_length=500)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


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
def audit(limit: int = 50) -> list[dict]:
    return list_audit(min(max(limit, 1), 200))


@app.get("/api/v1/audit/integrity")
def verify_audit_integrity() -> dict:
    return audit_integrity()


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
def knowledge(role: str = "viewer") -> list[dict]:
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return list_knowledge_documents()


@app.get("/api/v1/knowledge/search")
def search_knowledge(query: str, limit: int = 3, role: str = "viewer") -> list[dict]:
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索权限。")
    return KnowledgeRag().search(query, min(max(limit, 1), 10))


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
    document = add_knowledge_document(request.title, request.content, request.tags)
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
def approvals(limit: int = 100) -> list[dict]:
    return list_approvals(min(max(limit, 1), 200))


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


@app.get("/api/v1/tools")
def tools_catalog() -> list[dict[str, str]]:
    return catalog()


@app.post("/api/v1/tools/{tool_name}/invoke")
def invoke_tool(tool_name: str, request: ToolInvokeRequest) -> dict:
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


@app.post("/api/v1/evaluations/run")
def evaluate() -> dict:
    return run_evaluation()
