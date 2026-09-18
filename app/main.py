from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.database import approve, initialize, list_audit, seed_demo_data
from app.skills import SkillRegistry
from app.workflow import SqlAgentWorkflow


app = FastAPI(title="安全可控 SQL Agent", version="0.1.0")
workflow = SqlAgentWorkflow()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    requester: str = Field(default="anonymous", min_length=1, max_length=64)


@app.on_event("startup")
def startup() -> None:
    initialize()
    seed_demo_data()


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
        captured = []
        result = workflow.run(request.question, request.requester, captured.append)
        for event in captured:
            yield f"event: stage\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        yield f"event: result\ndata: {json.dumps(result.to_dict(), ensure_ascii=False)}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/v1/approvals/{approval_id}/approve")
def approve_request(approval_id: str) -> dict:
    approval = approve(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    return {"status": "approved", "approval_id": approval_id, "message": "审批已记录；生产环境应由短期凭证执行器异步执行。"}


@app.get("/api/v1/audit")
def audit(limit: int = 50) -> list[dict]:
    return list_audit(min(max(limit, 1), 200))


@app.get("/api/v1/skills")
def skills() -> list[dict[str, str]]:
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    return [{"name": item.name, "description": item.description} for item in registry.load()]
