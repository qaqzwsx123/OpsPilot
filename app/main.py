from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.database import add_knowledge_document, approve, delete_knowledge_document, execute_approved, initialize, list_approvals, list_audit, list_knowledge_documents, list_metric_definitions, metric_trend, monitoring_overview, recent_memory, rebuild_knowledge_index, seed_demo_data, seed_metric_demo_data, system_metrics, write_audit
from app.evaluation import run_evaluation
from app.skills import SkillRegistry
from app.tool_registry import catalog, invoke
from app.workflow import SqlAgentWorkflow
from app.rag import KnowledgeRag


app = FastAPI(title="安全可控 SQL Agent", version="0.1.0")
workflow = SqlAgentWorkflow()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    requester: str = Field(default="anonymous", min_length=1, max_length=64)


class KnowledgeDocumentRequest(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=10, max_length=4000)
    tags: str = Field(default="未分类", max_length=200)


@app.on_event("startup")
def startup() -> None:
    initialize()
    seed_demo_data()
    seed_metric_demo_data()
    rebuild_knowledge_index()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/v1/query")
def query(request: QueryRequest) -> dict:
    return workflow.run(request.question, request.requester).to_dict()


@app.post("/api/v1/query/stream")
def stream_query(request: QueryRequest) -> StreamingResponse:
    def event_stream() -> Iterator[str]:
        queue: Queue[tuple[str, dict]] = Queue()
        completed = Event()

        def worker() -> None:
            try:
                result = workflow.run(request.question, request.requester, lambda event: queue.put(("stage", event.to_dict())))
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
def approve_request(approval_id: str) -> dict:
    approval = approve(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    execution = execute_approved(approval_id, settings.allow_approved_writes)
    if execution is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    outcome = execution.get("outcome")
    write_audit(approval["requester"], "approval_resolved", {"approval_id": approval_id, "outcome": outcome})
    messages = {
        "executed": "审批完成，已执行受控 Demo 操作。",
        "safe_mode": "审批已记录。安全模式关闭了写库执行；设置 SAFE_SQL_AGENT_ALLOW_APPROVED_WRITES=true 后才可执行。",
        "not_allowlisted": "审批已记录，但该 SQL 不在 Demo 执行白名单内。",
        "not_approved": "审批单当前不处于可执行状态。",
    }
    return {"status": execution["status"], "approval_id": approval_id, "outcome": outcome, "message": messages.get(outcome, "审批状态已更新。")}


@app.get("/api/v1/audit")
def audit(limit: int = 50) -> list[dict]:
    return list_audit(min(max(limit, 1), 200))


@app.get("/api/v1/skills")
def skills() -> list[dict[str, str]]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    return [{"name": item.name, "description": item.description} for item in registry.load()]


@app.get("/api/v1/skills/{skill_name}")
def skill_detail(skill_name: str) -> dict[str, str]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    for item in registry.load():
        if item.name == skill_name:
            return {"name": item.name, "description": item.description, "content": item.content}
    raise HTTPException(status_code=404, detail="Skill 不存在")


@app.get("/api/v1/knowledge")
def knowledge() -> list[dict]:
    return list_knowledge_documents()


@app.get("/api/v1/knowledge/search")
def search_knowledge(query: str, limit: int = 3) -> list[dict]:
    return KnowledgeRag().search(query, min(max(limit, 1), 10))


@app.post("/api/v1/knowledge/reindex")
def reindex_knowledge() -> dict:
    chunks = rebuild_knowledge_index()
    write_audit("Lenovo", "knowledge_reindexed", {"chunk_count": chunks})
    return {"status": "completed", "chunk_count": chunks}


@app.post("/api/v1/knowledge", status_code=201)
def create_knowledge(request: KnowledgeDocumentRequest) -> dict:
    document = add_knowledge_document(request.title, request.content, request.tags)
    write_audit("Lenovo", "knowledge_created", {"document_id": document["id"], "title": document["title"]})
    return document


@app.delete("/api/v1/knowledge/{document_id}")
def delete_knowledge(document_id: int) -> None:
    if not delete_knowledge_document(document_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    write_audit("Lenovo", "knowledge_deleted", {"document_id": document_id})


@app.get("/api/v1/metrics")
def metrics() -> dict:
    return {**system_metrics(), "llm_enabled": settings.llm_enabled, "approved_writes_enabled": settings.allow_approved_writes}


@app.get("/api/v1/approvals")
def approvals(limit: int = 100) -> list[dict]:
    return list_approvals(min(max(limit, 1), 200))


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
def invoke_tool(tool_name: str) -> dict:
    result = invoke(tool_name)
    write_audit("Lenovo", "tool_invoked", {"tool": tool_name, "status": result["status"]})
    return result


@app.get("/api/v1/memory/{requester}")
def memory(requester: str, limit: int = 6) -> list[dict[str, str]]:
    return recent_memory(requester, min(max(limit, 1), 30))


@app.post("/api/v1/evaluations/run")
def evaluate() -> dict:
    return run_evaluation()
