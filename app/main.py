"""FastAPI 应用入口：提供页面 API、SSE 流式接口和权限边界。

本模块是 OpsPilot 的后端 HTTP 入口，负责：
1. 应用生命周期管理：启动时初始化 SQLite、演示数据、知识分块和 Chroma 索引；
2. 提供 Agent 聊天接口：会话 CRUD、消息 CRUD、同步回复和 SSE 流式回复；
3. 提供智能查询接口：同步 SQL Agent 工作流和 SSE 工作流轨迹；
4. 提供审批中心接口：批准、拒绝、删除审批单；
5. 提供审计中心接口：查询、清理、单条删除和哈希链校验；
6. 提供数据浏览器、Skills、知识库、Chroma、指标、工具、记忆和离线评测接口；
7. 在每个写操作入口做 RBAC 权限检查，并写入审计日志。

安全边界：
- role 只是前端声明，最终权限由后端 RBAC、工具风险和 SQL 审查共同决定；
- 所有写操作都必须先通过 permitted(role, permission) 检查；
- 数据浏览器只允许白名单表；
- 审批执行默认走安全模式，不真正写业务数据；
- 审计删除需要显式 confirm，并写入独立的维护日志。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

import json  # 序列化 SSE 事件数据
from collections.abc import AsyncIterator, Iterator  # 标注异步生命周期和流式生成器
from contextlib import asynccontextmanager  # 声明 FastAPI 异步生命周期管理器
from pathlib import Path  # 跨平台路径处理
from queue import Empty, Queue  # 用于跨线程传递工作流事件
from threading import Event, Thread  # 在后台线程运行工作流，避免阻塞事件循环
from typing import Any  # 动态筛选条件的值允许多种 JSON 标量类型

from fastapi import FastAPI, HTTPException, Request  # FastAPI 核心组件
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse  # 各类响应类型
from fastapi.staticfiles import StaticFiles  # 挂载静态文件目录
from pydantic import BaseModel, Field  # 请求体校验和字段约束

from app.config import settings  # 运行配置：模型地址、超时、写开关等
# 下面这一大串是从 database 模块导入的所有持久化函数，覆盖业务、知识、审批、审计、会话、指标等。
from app.database import (
    APPROVAL_STATUSES,           # 合法审批状态集合，用于校验过滤参数
    add_chat_message,            # 追加聊天消息
    add_evaluation_case,         # 新增评测用例
    add_knowledge_document,      # 新增知识文档
    approve,                     # 批准审批单
    approval_count,              # 审批单数量
    approval_status_counts,      # 各状态审批数量
    audit_cleanup_preview,       # 审计清理预览
    audit_count,                 # 审计总数
    audit_integrity,             # 审计哈希链校验
    create_approval,             # 创建审批单
    create_chat_conversation as create_chat_conversation_record,  # 创建会话（重命名避免与 API 函数冲突）
    create_custom_query_tool,     # 创建持久化自定义只读工具
    custom_tool_schema,           # 自定义查询可用表/字段目录
    data_catalog,                # 数据浏览器表目录
    delete_approval,             # 删除审批单
    delete_audit_event,          # 删除单条审计
    delete_audit_events,         # 批量删除勾选的审计记录
    delete_audit_range,          # 按范围删除审计
    delete_evaluation_case,      # 删除评测用例
    delete_chat_conversation,    # 删除会话
    delete_custom_query_tool,    # 删除自定义查询工具
    delete_knowledge_document,   # 删除知识文档
    document_chunks,             # 查询文档分块
    execute_approved,            # 执行已批准变更
    get_chat_messages,           # 查询会话消息
    import_metric_csv,           # 导入指标 CSV
    initialize,                  # 初始化数据库
    knowledge_document_count,    # 知识文档数量
    knowledge_evaluation_feedback_summary,  # 知识评分汇总
    knowledge_tags,              # 知识标签目录
    list_approvals,              # 审批列表
    list_audit,                  # 审计列表
    list_chat_conversations,     # 会话列表
    list_knowledge_documents,    # 知识文档列表
    list_knowledge_versions,     # 知识版本列表
    list_metric_definitions,     # 指标定义列表
    list_metric_imports,         # 指标导入记录
    metric_csv_template,         # 指标 CSV 模板
    metric_trend,                # 指标趋势
    monitoring_overview,         # 监控概览
    recent_memory,               # 最近记忆
    rebuild_knowledge_index,     # 重建知识分块
    reject_approval,             # 拒绝审批单
    rollback_knowledge_document, # 回滚知识文档
    save_knowledge_evaluation_feedback,  # 保存知识评分
    seed_demo_data,              # 填充演示业务数据
    seed_metric_demo_data,       # 填充演示指标
    system_metrics,              # 系统指标
    table_snapshot,              # 表快照
    update_knowledge_document,   # 更新知识文档
    write_audit,                 # 写审计日志
)
from app.chat_service import AgentChatService  # Agent 聊天服务
from app.evaluation import available_evaluation_cases, run_evaluation  # SQL Agent 离线评测
from app.skills import SkillRegistry, run_skill  # Skill 注册表和运行入口
from app.tool_registry import catalog, definition, invoke  # 工具目录、定义、调用
from app.workflow import SqlAgentWorkflow  # SQL Agent 工作流
from app.rag import KnowledgeRag  # RAG 检索
from app.chroma_store import (
    chroma_collection_catalog,   # Chroma 集合目录
    chroma_collection_records,   # Chroma 集合记录
    chroma_stats,                # Chroma 统计
    rebuild_chroma_index,        # 重建 Chroma 索引
)
from app.knowledge_evaluation import run_knowledge_evaluation  # 知识检索评测
from app.policy import permitted, policy_summary, role_catalog  # RBAC 权限检查与目录


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """FastAPI 应用生命周期管理器。

    在服务启动时执行一次初始化：
    1. initialize()：幂等创建表结构和执行轻量迁移；
    2. seed_demo_data()：填充演示业务数据和内置知识库；
    3. seed_metric_demo_data()：填充演示指标；
    4. rebuild_knowledge_index()：重建知识分块，保证 RAG 可用；
    5. rebuild_chroma_index()：重建 Chroma 向量索引；失败时不影响 SQLite/RAG 可用性。

    进入 yield 后进入请求处理阶段；当前未实现 shutdown 逻辑。
    """
    initialize()
    seed_demo_data()
    seed_metric_demo_data()
    rebuild_knowledge_index()
    try:
        rebuild_chroma_index()
    except Exception:
        # SQLite/RAG remains available even if the optional local index needs repair.
        # Chroma 是可选索引；重建失败时不应阻塞服务启动。
        pass
    yield


# 创建 FastAPI 应用实例；lifespan 指定启动/关闭逻辑。
app = FastAPI(title="安全可控 SQL Agent", version="0.1.0", lifespan=lifespan)

# 全局 SQL Agent 工作流实例，供同步和流式查询接口复用。
workflow = SqlAgentWorkflow()   #调用这个类的构造方法 __init__，创建一个对象/实例

# 全局 Agent 聊天服务实例，供同步和流式聊天接口复用。
chat_service = AgentChatService()

# 前端静态文件目录：项目根目录 web/。
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 挂载静态文件目录到 /static，前端通过 /static/xxx 访问。
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------- 请求体模型 ----------

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


class KnowledgeUploadRequest(KnowledgeDocumentRequest):
    """上传文档的请求体；上传正文允许比普通编辑更长。"""
    # 上传文件的原始名称，仅用于显示和审计，不作为本地路径执行。
    content: str = Field(min_length=10, max_length=100000)
    filename: str = Field(min_length=1, max_length=180)


class MutationActorRequest(BaseModel):
    """所有会产生副作用的 API 共用的操作者信息。"""
    # 后端权限检查使用的角色 ID。
    role: str = Field(default="operator", min_length=1, max_length=32)
    # 审计日志中的操作者名称。
    requester: str = Field(default="Lenovo", min_length=1, max_length=64)


class AuditDeleteRequest(MutationActorRequest):
    """删除单条审计记录时的二次确认。"""
    # 必须显式传 true，避免误触发不可逆删除。
    confirm: bool = False


class AuditBulkDeleteRequest(AuditDeleteRequest):
    """删除当前页所选审计记录；限制数量，避免超大请求。"""
    event_ids: list[str] = Field(min_length=1, max_length=100)


class AuditCleanupRequest(MutationActorRequest):
    """按起止时间批量清理审计记录的请求体。"""
    # 清理窗口的起始时间，包含边界。
    start: str = Field(min_length=10, max_length=40)
    # 清理窗口的结束时间，包含边界。
    end: str = Field(min_length=10, max_length=40)
    # 必须显式确认后才会执行预览结果对应的删除。
    confirm: bool = False


class ToolInvokeRequest(MutationActorRequest):
    """工具中心试运行请求；工具名来自 URL，权限和风险仍由后端判断。"""
    query: str = Field(default="", max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CustomToolFilterRequest(BaseModel):
    """一个自定义筛选字段及其允许的比较操作。"""
    column: str = Field(min_length=1, max_length=64)
    operators: list[str] = Field(min_length=1, max_length=7)


class CustomToolCreateRequest(MutationActorRequest):
    """新增受约束的自定义只读查询工具。"""
    name: str = Field(min_length=2, max_length=48)
    description: str = Field(min_length=1, max_length=240)
    category: str = Field(min_length=1, max_length=48)
    table_name: str = Field(min_length=1, max_length=64)
    columns: list[str] = Field(min_length=1, max_length=32)
    filters: list[CustomToolFilterRequest] = Field(default_factory=list, max_length=12)
    max_rows: int = Field(default=20, ge=1, le=100)


class SkillRunRequest(MutationActorRequest):
    """运行一个固定 Skill 时传入的补充上下文。"""
    # 例如 P1、区域或指标编号，交给固定 Skill 解析而非拼接任意 SQL。
    user_input: str = Field(default="", max_length=500)


class ChatConversationRequest(MutationActorRequest):
    """创建 Agent 聊天会话的请求体。"""
    # 左侧历史会话显示名称。
    title: str = Field(default="新对话", max_length=48)


class ChatTurnRequest(MutationActorRequest):
    """向已有会话追加一轮用户消息。"""
    # 当前轮的自然语言内容，聊天服务会结合历史上下文调用模型。
    content: str = Field(min_length=1, max_length=4000)


class ApprovalActionRequest(BaseModel):
    """审批人对一张变更审批单的处理意见。"""
    # 必须拥有 approve_change 权限。
    role: str = Field(default="approver", min_length=1, max_length=32)
    # 审批记录中的实际处理人。
    actor: str = Field(default="Lenovo", min_length=1, max_length=64)
    # 同意或拒绝时附带的业务理由。
    comment: str = Field(default="", max_length=500)


class EvaluationCaseRequest(MutationActorRequest):
    """新增一条可重复运行的 Agent 评测用例。"""
    # 用例展示名称。
    name: str = Field(min_length=2, max_length=80)
    # 送入真实工作流的用户问题。
    question: str = Field(min_length=2, max_length=500)
    # 期望的业务状态，例如 completed、answered_by_rag 或 approval_required。
    expected_status: str = Field(default="completed", min_length=1, max_length=32)


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


# ---------- 健康检查与首页 ----------

@app.get("/health")
def health() -> dict[str, str]:
    """健康探针：返回服务基本状态，用于负载均衡和监控。"""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    """返回前端首页 index.html。"""
    return FileResponse(WEB_DIR / "index.html")


# ---------- Agent 聊天：会话、历史消息和流式回复 ----------

@app.post("/api/v1/chat/conversations")
def create_chat_conversation(request: ChatConversationRequest) -> dict:
    """创建 Agent 聊天会话。

    - 先检查 read 权限；
    - 写入会话记录；
    - 写审计日志记录会话创建动作。
    """
    # 聊天会话创建先走 RBAC，再写入会话审计事件。
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    conversation = create_chat_conversation_record(request.requester, request.title)
    write_audit(request.requester, "chat_conversation_created", {
        "conversation_id": conversation["id"], "role": request.role,
    })
    return conversation


@app.get("/api/v1/chat/conversations")
def chat_conversations(requester: str = "Lenovo", role: str = "viewer") -> list[dict]:
    """列出指定请求人的所有聊天会话，按更新时间倒序。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    return list_chat_conversations(requester)


@app.get("/api/v1/chat/conversations/{conversation_id}/messages")
def chat_messages(conversation_id: str, requester: str = "Lenovo", role: str = "viewer") -> list[dict]:
    """获取指定会话的所有消息。

    - 会话不存在或不属于该 requester 时返回 404；
    - 避免通过 ID 猜测访问他人会话。
    """
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    messages = get_chat_messages(conversation_id, requester)
    if messages is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    return messages


@app.delete("/api/v1/chat/conversations/{conversation_id}")
def delete_chat(conversation_id: str, request: MutationActorRequest) -> dict:
    """删除指定会话及其消息，并写审计日志。"""
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    if not delete_chat_conversation(conversation_id, request.requester):
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    write_audit(request.requester, "chat_conversation_deleted", {
        "conversation_id": conversation_id, "role": request.role,
    })
    return {"deleted": True}


@app.post("/api/v1/chat/conversations/{conversation_id}/messages")
def chat_turn(conversation_id: str, request: ChatTurnRequest) -> dict:
    """同步聊天接口：写入用户消息、调用模型、写入助手消息。

    流程：
    1. 权限检查；
    2. 追加用户消息，若会话不存在返回 404；
    3. 读取完整历史，调用 chat_service.reply；
    4. 追加助手消息并写审计；
    5. 返回会话 ID、助手消息和 provider。

    说明：正式聊天页面使用流式接口；本接口用于简单调用和兼容。
    """
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    # 先写入用户消息；返回 None 表示会话不存在或不属于该 requester。
    if add_chat_message(conversation_id, request.requester, "user", request.content) is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")

    # 重新加载完整历史作为模型上下文。
    history = get_chat_messages(conversation_id, request.requester)
    if history is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")

    # 调用聊天服务；reply 内部会处理流式/非流式和兜底。
    content, provider = chat_service.reply([
        {"role": item["role"], "content": item["content"]} for item in history
    ])

    # 写入助手消息并记录审计。
    message = add_chat_message(conversation_id, request.requester, "assistant", content)
    write_audit(request.requester, "agent_chat_completed", {
        "conversation_id": conversation_id, "provider": provider, "role": request.role,
    })
    return {"conversation_id": conversation_id, "message": message, "provider": provider}


@app.post("/api/v1/chat/conversations/{conversation_id}/messages/stream")
def chat_turn_stream(conversation_id: str, request: ChatTurnRequest) -> StreamingResponse:
    """Stream planning stages and model deltas, then persist the completed assistant turn.

    流式聊天接口，SSE 事件类型：
    - stage：规划阶段（context、plan）；
    - token：模型增量文本；
    - done：完成，返回会话 ID、provider、助手消息和规划；
    - error：流式过程中出现异常。

    流程：
    1. 权限检查；
    2. 写入用户消息并加载历史；
    3. 调用 plan_turn 生成本轮规划；
    4. 定义 event_stream 生成器，依次发送 stage、token、done 事件；
    5. 拼接所有 token 后写入助手消息并写审计；
    6. 返回 StreamingResponse，禁用缓存和代理缓冲。
    """
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Agent 聊天权限。")
    if add_chat_message(conversation_id, request.requester, "user", request.content) is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")
    history = get_chat_messages(conversation_id, request.requester)
    if history is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问。")

    # 转成 OpenAI 兼容的消息列表。
    messages = [{"role": item["role"], "content": item["content"]} for item in history]

    # 生成本轮任务规划：路线、步骤和使用的上下文条数。
    plan = chat_service.plan_turn(request.content, messages)

    def event_stream() -> Iterator[str]:
        """SSE 事件生成器：把规划、增量 token 和完成事件编码为 SSE 格式。"""
        def emit(name: str, payload: dict) -> str:
            """把事件名和 payload 编码为 SSE 字符串。"""
            return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # 等 SSE 连接建立后再调用摘要模型，避免摘要过程阻塞 HTTP 响应头。
        model_messages, context_stats = chat_service.prepare_context(messages, plan)
        plan["context_messages"] = context_stats.get("retained_messages", len(messages))
        plan["context_stats"] = context_stats

        # 第一阶段：上下文加载。
        yield emit("stage", {
            "stage": "context",
            "message": (
                f"上下文压缩完成：保留 {plan['context_messages']} 条近期消息，"
                f"输入约 {context_stats['after']}/{context_stats['input_budget']} Token"
            ),
            "stats": context_stats,
        })

        # 第二阶段：任务规划。
        yield emit("stage", {
            "stage": "plan",
            "message": plan["route"],
            "steps": plan["steps"],
        })

        chunks: list[str] = []  # 收集所有 token，用于最终写入助手消息
        provider = "fallback"   # 默认兜底 provider
        try:
            # 逐段读取聊天服务的流式输出。
            for item in chat_service.stream_reply(model_messages, plan, prepared=True):
                provider = item.get("provider", provider)
                if item.get("type") == "token":
                    chunks.append(item["content"])
                    yield emit("token", {"content": item["content"], "provider": provider})

            # 拼接完整回复并持久化。
            content = "".join(chunks)
            message = add_chat_message(conversation_id, request.requester, "assistant", content)

            # 写审计：记录 provider、role、上下文条数和路线。
            write_audit(request.requester, "agent_chat_stream_completed", {
                "conversation_id": conversation_id, "provider": provider, "role": request.role,
                "context_messages": plan["context_messages"], "route": plan["route"],
                "context_stats": context_stats,
            })

            # 完成事件。
            yield emit("done", {
                "conversation_id": conversation_id,
                "provider": provider,
                "message": message,
                "plan": plan,
            })
        except Exception as exc:  # keep the browser informed if the stream fails after it starts
            # 流已经开始，无法再返回 HTTP 状态码，通过 error 事件通知前端。
            yield emit("error", {"message": "流式聊天失败：" + str(exc)[:180]})

    # 返回 SSE 响应：禁用缓存和 Nginx 缓冲，保证增量实时到达浏览器。
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 智能查询：同步结果与 SSE 工作流轨迹 ----------

@app.post("/api/v1/query")
def query(request: QueryRequest) -> dict:
    """同步智能查询接口。

    直接调用 SQL Agent 工作流，返回完整结果。
    use_model_tools 由配置决定是否允许模型选择只读工具。
    """
    return workflow.run(
        request.question,
        request.requester,
        request.role,
        use_model_tools=settings.model_tool_planner_enabled,
    ).to_dict()
# 1. 前端发送请求
#    {
#      "question": "查询华东区离线设备",
#      "requester": "Lenovo",
#      "role": "operator"
#    }
# 2. FastAPI 先用 QueryRequest 校验请求参数
#    - 校验通过：进入 query 函数
#    - 校验失败：直接返回 422
#    - query 函数不会执行
# 3. 进入 app/main.py 的 query 函数
# 4. 调用 workflow.run(...)
# 5. workflow.run 组装 LangGraph 初始 state
# 6. 调用 self.graph.invoke(state)
# 7. LangGraph 执行权限、记忆、工具、召回、SQL、
#    审查、修复、风险、执行/审批/RAG 等节点
# 8. LangGraph 返回 final_state
# 9. workflow.run 从 final_state 中取出 result
# 10. workflow.run 检查 result 是否为 QueryResult
# 11. workflow.run 返回 QueryResult
# 12. main.py 调用 result.to_dict()
# 13. FastAPI 把字典序列化成 JSON 返回前端

@app.post("/api/v1/query/stream")
# 前端向 /api/v1/query/stream 发送问题、用户和角色。
# FastAPI 通过 QueryRequest 校验请求，并创建 SSE 事件流。
# SSE 路由启动后台线程，在后台线程中调用 workflow.run(...)。
# workflow.run 组装包含问题、角色、事件回调的 LangGraph state。
# workflow.run 调用 self.graph.invoke(state) 执行 LangGraph。
# LangGraph 依次执行权限检查、记忆加载、工具规划、元数据召回、SQL 生成、SQL 审查、修复、风险判断、执行或审批/RAG 等节点。
# 节点产生的阶段事件通过 on_event 放入队列，再由 SSE 实时推送给前端。
# LangGraph 执行完成后返回 final_state，workflow.run 从中取出 result 并校验它是否为 QueryResult。
# 后台线程调用 result.to_dict()，把最终结果放入队列。
# SSE 路由把最终结果包装成 event: result 发给前端。
# FastAPI 始终返回 StreamingResponse，前端边接收工作流轨迹，边更新右侧流程图，最后展示查询结果。
def stream_query(request: QueryRequest) -> StreamingResponse:
    """SSE 流式智能查询接口。

    在后台线程运行工作流，通过队列把事件传给 SSE 生成器：
    - stage：工作流阶段事件；
    - result：最终结果；
    - error：执行异常。
    使用 keepalive 空注释防止代理断开空闲连接。
    """
    def event_stream() -> Iterator[str]:
        # 跨线程队列：工作流线程 put，SSE 生成器 get。
        queue: Queue[tuple[str, dict]] = Queue()
        # 完成标志：通知 SSE 生成器工作流已结束。
        completed = Event()
        def worker() -> None:
            """后台工作流线程：运行工作流并把事件和结果放入队列。"""
            try:
                # 通过回调把每个阶段事件放入队列。
                result = workflow.run(
                    request.question,
                    request.requester,
                    request.role,
                    lambda event: queue.put(("stage", event.to_dict())),
                    use_model_tools=settings.model_tool_planner_enabled,
                )
                # 最终结果放入队列。
                queue.put(("result", result.to_dict()))
            except Exception as exc:  # errors remain structured for the browser client
                # 异常也作为结构化事件返回，而不是直接抛出。
                queue.put(("error", {"message": "工作流执行失败", "type": type(exc).__name__}))
            finally:
                # 无论成功失败都设置完成标志，避免 SSE 生成器挂起。
                completed.set()

        # 启动后台线程，daemon=True 保证进程退出时线程不阻塞。
        Thread(target=worker, daemon=True).start()

        # 循环读取队列，直到工作流完成且队列为空。
        while not completed.is_set() or not queue.empty():
            try:
                # 超时 0.5 秒，避免阻塞太久无法发送 keepalive。
                event_name, payload = queue.get(timeout=0.5)
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Empty:
                # 队列暂时为空时发送 SSE 注释行作为心跳。
                yield ": keepalive\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- 审批中心 ----------

@app.post("/api/v1/approvals/{approval_id}/approve")
def approve_request(approval_id: str, request: ApprovalActionRequest = ApprovalActionRequest()) -> dict:
    """批准审批单，并按配置决定是否真正执行写操作。

    流程：
    1. 检查 approve_change 权限；
    2. 调用 approve 更新状态；
    3. 若已过期，返回 expired 并写审计；
    4. 调用 execute_approved 执行：
       - allow_writes=False 时进入 safe_mode，只记录不落库；
       - allow_writes=True 时只允许白名单 SQL，并先备份；
    5. 写审计并返回结果和提示信息。
    """
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="当前角色无审批权限。请切换到值班负责人。")
    approval = approve(approval_id, request.actor, request.comment)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")

    # 过期分支：不执行任何 SQL，提示重新发起。
    if approval["status"] == "expired":
        write_audit(approval["requester"], "approval_expired", {
            "approval_id": approval_id, "approver": request.actor,
        })
        return {
            "status": "expired", "approval_id": approval_id,
            "message": "审批单已过期（有效期 30 分钟），没有执行任何 SQL。请重新发起变更。",
        }

    # 执行：safe_mode 或 executed。
    execution = execute_approved(approval_id, settings.allow_approved_writes)
    if execution is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    outcome = execution.get("outcome")

    # 记录审批结果审计。
    write_audit(approval["requester"], "approval_resolved", {
        "approval_id": approval_id, "outcome": outcome,
        "approver_role": request.role, "approver": request.actor, "comment": request.comment,
    })

    # 根据 outcome 返回不同的用户提示。
    messages = {
        "executed": "审批完成，已执行受控 Demo 操作。",
        "safe_mode": "审批已记录为“安全模式已批准”。未执行写库；影响范围仅作预估并已留痕。",
        "not_allowlisted": "审批已记录，但该 SQL 不在 Demo 执行白名单内。",
        "not_approved": "审批单当前不处于可执行状态。",
    }
    return {
        "status": execution["status"], "approval_id": approval_id,
        "outcome": outcome, "message": messages.get(outcome, "审批状态已更新。"),
    }


@app.post("/api/v1/approvals/{approval_id}/reject")
def reject_request(approval_id: str, request: ApprovalActionRequest = ApprovalActionRequest()) -> dict:
    """拒绝审批单。

    - 检查 approve_change 权限；
    - 调用 reject_approval 更新状态；
    - 只对处于 pending 的单子写审计，避免重复记录。
    """
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="当前角色无审批权限。请切换到值班负责人。")
    approval = reject_approval(approval_id, request.actor, request.comment)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")

    # 若状态不是 rejected，说明已经处理过，不再写审计。
    if approval["status"] != "rejected":
        return {
            "status": approval["status"], "approval_id": approval_id,
            "message": "审批单当前已不是待处理状态。",
        }

    write_audit(approval["requester"], "approval_rejected", {
        "approval_id": approval_id, "approver_role": request.role,
        "approver": request.actor, "comment": approval["decision_comment"],
    })
    return {
        "status": "rejected", "approval_id": approval_id,
        "message": "审批已拒绝；没有执行任何 SQL，也没有修改业务数据。",
    }


@app.delete("/api/v1/approvals/{approval_id}")
def remove_approval(approval_id: str, request: ApprovalActionRequest = ApprovalActionRequest()) -> dict:
    """删除已完成的审批记录。

    - 只有 approve_change 角色可删除；
    - pending 状态不可删除，需要先批准或拒绝；
    - 删除操作写审计，保留可追溯性。
    """
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


# ---------- 审计中心 ----------

@app.get("/api/v1/audit")
def audit(limit: int = 50, offset: int = 0) -> list[dict]:
    """分页查询审计日志，limit 限制在 1~200，offset 不小于 0。"""
    return list_audit(min(max(limit, 1), 200), max(offset, 0))


@app.get("/api/v1/audit/cleanup-preview")
def audit_cleanup_check(start: str, end: str, role: str = "viewer") -> dict:
    """预览指定时间范围内的审计清理影响，只读不删除。

    - 只有 approve_change 角色可预览；
    - 时间格式错误时返回 400。
    """
    if not permitted(role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以预览审计清理范围。")
    try:
        return audit_cleanup_preview(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/v1/audit/range")
def cleanup_audit(request: AuditCleanupRequest) -> dict:
    """按时间范围批量删除审计记录。

    - 只有 approve_change 角色可执行；
    - 必须显式 confirm=True，避免误删；
    - 删除动作记录在独立的审计维护日志中。
    """
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以清理审计记录。")
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请确认按时间范围清理审计记录。")
    try:
        return delete_audit_range(request.start, request.end, request.requester)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/v1/audit/bulk")
def delete_selected_audit(request: AuditBulkDeleteRequest) -> dict:
    """批量删除所选审计事件，只有值班负责人可执行且必须显式确认。"""
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以删除审计记录。")
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请确认删除所选审计记录。")
    if any(not event_id.strip() or len(event_id) > 64 for event_id in request.event_ids):
        raise HTTPException(status_code=400, detail="审计记录 ID 格式无效。")
    return delete_audit_events(request.event_ids, request.requester)


@app.delete("/api/v1/audit/{event_id}")
def delete_audit(event_id: str, request: AuditDeleteRequest) -> dict:
    """删除单条审计记录。

    - 只有 approve_change 角色可执行；
    - 必须显式 confirm=True；
    - 删除后重建哈希链，并在维护日志中留痕。
    """
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以删除审计记录。")
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请确认删除该审计记录。")
    result = delete_audit_event(event_id, request.requester)
    if result is None:
        raise HTTPException(status_code=404, detail="审计记录不存在或已被删除。")
    return result


@app.get("/api/v1/audit/integrity")
def verify_audit_integrity(scope: str = "full") -> dict:
    """校验审计哈希链完整性。

    - scope="full"：校验全部事件；
    - scope="recent_100"：只校验最近 100 条。
    """
    if scope not in {"full", "recent_100"}:
        raise HTTPException(status_code=400, detail="校验范围必须是 full 或 recent_100")
    return audit_integrity(None if scope == "full" else 100)


@app.get("/api/v1/audit/summary")
def audit_summary() -> dict:
    """返回审计事件总数。"""
    return {"total": audit_count()}


# ---------- 数据浏览器、Skills 和知识库 ----------

@app.get("/api/v1/data/tables")
def explorer_catalog(role: str = "viewer") -> list[dict]:
    """返回数据浏览器可访问的表目录。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无数据浏览权限。")
    return data_catalog()


@app.get("/api/v1/data/tables/{table_name}")
def explorer_table(table_name: str, role: str = "viewer", limit: int = 30, offset: int = 0) -> dict:
    """返回指定表的 schema 和样本数据。

    - 表名必须在白名单中，否则 404；
    - 每次查看写审计，便于追踪数据浏览行为。
    """
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无数据浏览权限。")
    snapshot = table_snapshot(table_name, min(max(limit, 1), 100), max(offset, 0))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="该表不在数据浏览器授权范围内。")
    write_audit("Lenovo", "data_explorer_viewed", {
        "table": table_name, "limit": snapshot["limit"],
        "offset": snapshot["offset"], "role": role,
    })
    return snapshot


@app.get("/api/v1/skills")
def skills() -> list[dict]:
    """返回所有 Skill 的目录信息。"""
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    return [
        {
            "name": item.name, "description": item.description,
            "category": item.category, "risk": item.risk,
            "suggestions": list(item.suggestions), "runnable": item.runnable,
        }
        for item in registry.load()
    ]


@app.get("/api/v1/skills/history")
def skills_history(role: str = "viewer", limit: int = 8) -> list[dict]:
    """返回最近的 Skill 运行记录（从审计日志中过滤）。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Skill 运行记录查看权限。")
    return [
        event for event in list_audit(limit=200) if event["action"] == "skill_run"
    ][:min(max(limit, 1), 30)]


@app.get("/api/v1/skills/{skill_name}")
def skill_detail(skill_name: str) -> dict[str, str]:
    """返回指定 Skill 的名称、描述和完整内容。"""
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    for item in registry.load():
        if item.name == skill_name:
            return {"name": item.name, "description": item.description, "content": item.content}
    raise HTTPException(status_code=404, detail="Skill 不存在")


@app.post("/api/v1/skills/{skill_name}/run")
def execute_skill(skill_name: str, request: SkillRunRequest) -> dict:
    """运行一个可执行的 Skill。

    - 检查 read 权限；
    - 规范型 Skill（runnable=False）不允许直接运行；
    - 运行结果写审计。
    """
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无 Skill 运行权限。")
    registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    if not skill.runnable:
        raise HTTPException(status_code=400, detail="该 Skill 是规范型能力，请在智能查询或对应页面中使用。")
    result = run_skill(skill_name, request.user_input)
    write_audit(request.requester, "skill_run", {
        "skill": skill_name, "input": request.user_input[:160],
        "status": result["status"], "role": request.role,
    })
    return result


@app.get("/api/v1/knowledge")
def knowledge(role: str = "viewer", limit: int = 5, offset: int = 0, tag: str = "", status: str = "") -> dict:
    """分页查询知识文档，支持 tag 和 status 过滤。"""
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
    """返回所有知识标签。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return knowledge_tags()


@app.get("/api/v1/knowledge/{document_id}/chunks")
def knowledge_document_chunks(document_id: int, role: str = "viewer") -> list[dict]:
    """返回指定文档的所有分块。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库查看权限。")
    return document_chunks(document_id)


@app.get("/api/v1/knowledge/{document_id}/versions")
def knowledge_document_versions(document_id: int, role: str = "viewer") -> list[dict]:
    """返回指定文档的所有历史版本。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识库版本查看权限。")
    return list_knowledge_versions(document_id)


@app.get("/api/v1/knowledge/search")
def search_knowledge(query: str, limit: int = 3, role: str = "viewer") -> list[dict]:
    """知识库语义检索。

    - 需要 rag 权限；
    - limit 限制在 1~10；
    - 复用 KnowledgeRag，与线上检索行为一致。
    """
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索权限。")
    return KnowledgeRag().search(query, min(max(limit, 1), 10))


@app.get("/api/v1/knowledge/chroma")
def knowledge_chroma(role: str = "viewer") -> dict:
    """返回 Chroma 向量库的统计信息。

    - 需要 rag 权限；
    - Chroma 未就绪时返回 503。
    """
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无知识向量索引查看权限。")
    try:
        return chroma_stats()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chroma 尚未就绪：{exc}") from exc


@app.get("/api/v1/chroma/collections")
def chroma_collections(role: str = "viewer") -> list[dict]:
    """返回所有 Chroma 集合的名称和数量。"""
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无 Chroma 集合查看权限。")
    try:
        return chroma_collection_catalog()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chroma 尚未就绪：{exc}") from exc


@app.get("/api/v1/chroma/collections/{collection_name}")
def chroma_collection(collection_name: str, limit: int = 100, offset: int = 0, role: str = "viewer") -> list[dict]:
    """分页浏览指定 Chroma 集合的记录。

    - 只允许访问白名单集合；
    - 集合不存在或名字非法时返回 400；
    - Chroma 异常时返回 503。
    """
    if not permitted(role, "rag"):
        raise HTTPException(status_code=403, detail="当前角色无 Chroma 数据查看权限。")
    try:
        return chroma_collection_records(collection_name, limit, offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Chroma 记录读取失败：{exc}") from exc


@app.post("/api/v1/knowledge/evaluation")
def evaluate_knowledge(request: MutationActorRequest) -> dict:
    """运行知识检索评测并写审计。"""
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无知识检索评测权限。")
    report = run_knowledge_evaluation()
    write_audit(request.requester, "knowledge_evaluation_completed", {
        "total": report["total"], "passed": report["passed"],
        "hit_at_3": report["hit_at_3"], "role": request.role,
    })
    return report


@app.post("/api/v1/knowledge/evaluation/feedback")
def evaluate_knowledge_feedback(request: KnowledgeEvaluationFeedbackRequest) -> dict:
    """保存人工知识评分，并返回评分汇总。

    - 需要 request_change 权限；
    - 评分和汇总一并返回，便于前端即时刷新。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无提交知识库人工评分权限。")
    feedback = save_knowledge_evaluation_feedback(
        request.evaluation_id, request.question,
        request.score, request.comment, request.requester,
    )
    summary = knowledge_evaluation_feedback_summary(request.evaluation_id)
    write_audit(request.requester, "knowledge_evaluation_feedback", {
        "evaluation_id": request.evaluation_id, "question": request.question,
        "score": request.score, "role": request.role,
    })
    return {"feedback": feedback, "summary": summary}


@app.post("/api/v1/knowledge/reindex")
def reindex_knowledge(request: MutationActorRequest) -> dict:
    """重建知识分块和 Chroma 索引。

    - 需要 request_change 权限；
    - 先重建 SQLite 分块，再重建 Chroma；
    - 写审计记录分块数和 Chroma 总数。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    chunks = rebuild_knowledge_index()
    chroma = rebuild_chroma_index()
    write_audit(request.requester, "knowledge_reindexed", {
        "chunk_count": chunks, "chroma_total": chroma["total"], "role": request.role,
    })
    return {"status": "completed", "chunk_count": chunks, "chroma": chroma}


@app.post("/api/v1/knowledge", status_code=201)
def create_knowledge(request: KnowledgeDocumentRequest) -> dict:
    """新增知识文档。

    - 需要 request_change 权限；
    - 新增后重建 Chroma 索引，保证检索立即可用；
    - 写审计。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = add_knowledge_document(request.title, request.content, request.tags, request.expires_at)
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_created", {
        "document_id": document["id"], "title": document["title"], "role": request.role,
    })
    return document


@app.post("/api/v1/knowledge/upload", status_code=201)
def upload_knowledge(request: KnowledgeUploadRequest) -> dict:
    """上传知识文档。

    - 仅支持 .txt 和 .md 后缀；
    - 正文长度上限更高（100000）；
    - 保存后重建 Chroma 索引并写审计。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    suffix = Path(request.filename).suffix.lower()
    if suffix not in {".txt", ".md"}:
        raise HTTPException(status_code=400, detail="目前仅支持上传 .txt 或 .md 文档。")
    document = add_knowledge_document(request.title, request.content, request.tags, request.expires_at)
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_uploaded", {
        "document_id": document["id"], "filename": request.filename, "role": request.role,
    })
    return document


@app.put("/api/v1/knowledge/{document_id}")
def edit_knowledge(document_id: int, request: KnowledgeDocumentRequest) -> dict:
    """编辑知识文档，自动递增版本并重建索引。"""
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = update_knowledge_document(
        document_id, request.title, request.content, request.tags, request.expires_at,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_updated", {
        "document_id": document_id, "version": document["version"], "role": request.role,
    })
    return document


@app.post("/api/v1/knowledge/{document_id}/rollback/{version}")
def rollback_knowledge(document_id: int, version: int, request: MutationActorRequest) -> dict:
    """将知识文档回滚到指定历史版本。

    - 回滚本身以新版本记录，不会丢失当前版本；
    - 回滚后重建 Chroma 索引。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    document = rollback_knowledge_document(document_id, version)
    if document is None:
        raise HTTPException(status_code=404, detail="目标版本或知识文档不存在")
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_rolled_back", {
        "document_id": document_id, "source_version": version,
        "new_version": document["version"], "role": request.role,
    })
    return document


@app.delete("/api/v1/knowledge/{document_id}")
def delete_knowledge(document_id: int, request: MutationActorRequest) -> None:
    """删除知识文档及其分块和版本，并重建 Chroma 索引。

    - 返回 204（FastAPI 默认 None 返回空 body）；
    - 删除后写审计。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="当前角色无知识库维护权限。")
    if not delete_knowledge_document(document_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    rebuild_chroma_index()
    write_audit(request.requester, "knowledge_deleted", {
        "document_id": document_id, "role": request.role,
    })


# ---------- 指标、工具、记忆和离线评测 ----------

@app.get("/api/v1/metrics")
def metrics() -> dict:
    """返回系统指标、LLM 开关和已批准写操作开关。"""
    return {
        **system_metrics(),
        "llm_enabled": settings.llm_enabled,
        "approved_writes_enabled": settings.allow_approved_writes,
    }


@app.get("/api/v1/approvals")
def approvals(limit: int = 100, offset: int = 0, status: str = "") -> list[dict]:
    """分页查询审批单，可按状态过滤。"""
    if status and status not in APPROVAL_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的审批状态筛选")
    return list_approvals(min(max(limit, 1), 200), max(offset, 0), status)


@app.get("/api/v1/approvals/summary")
def approval_summary(status: str = "") -> dict:
    """返回审批总数和各状态计数。"""
    if status and status not in APPROVAL_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的审批状态筛选")
    return {"total": approval_count(status), "status_counts": approval_status_counts()}


@app.get("/api/v1/policies")
def policies() -> dict:
    """返回角色目录和策略摘要，供前端展示权限边界。"""
    return {"roles": role_catalog(), "policies": policy_summary()}


@app.get("/api/v1/monitoring/overview")
def monitoring() -> dict:
    """返回监控概览，并在健康检查中追加 Chroma 状态。

    - monitoring_overview() 提供基础健康项；
    - 如果 Chroma 可用，追加 healthy 项；
    - Chroma 异常时追加 degraded 项，不影响其他健康项。
    """
    overview = monitoring_overview()
    try:
        status = chroma_stats()
        overview.setdefault("health_checks", []).append({
            "name": "Chroma 向量库", "status": "healthy",
            "detail": f"{len(status['collections'])} 个集合 · {status['total']} 条索引",
        })
    except Exception as exc:
        overview.setdefault("health_checks", []).append({
            "name": "Chroma 向量库", "status": "degraded",
            "detail": f"索引不可用：{exc}",
        })
    return overview


@app.get("/api/v1/metric-definitions")
def metric_definitions(keyword: str = "", category: str = "", limit: int = 60) -> list[dict]:
    """按关键字和分类查询指标定义。"""
    return list_metric_definitions(keyword.strip(), category.strip(), min(max(limit, 1), 100))


@app.get("/api/v1/metric-definitions/{metric_id}/trend")
def metric_definition_trend(metric_id: int, points: int = 24) -> dict:
    """返回指定指标的趋势数据点，points 限制在 2~48。"""
    trend = metric_trend(metric_id, min(max(points, 2), 48))
    if trend is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    return trend


@app.get("/api/v1/metrics/import-template")
def metric_import_template() -> PlainTextResponse:
    """下载指标 CSV 导入模板。"""
    return PlainTextResponse(
        metric_csv_template(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="metric-import-template.csv"'},
    )


@app.get("/api/v1/metrics/imports")
def metric_imports(role: str = "viewer") -> list[dict]:
    """返回最近的指标导入记录。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无指标导入记录查看权限。")
    return list_metric_imports()


@app.post("/api/v1/metrics/import", status_code=201)
async def import_metrics_csv(
    request: Request, filename: str = "metrics.csv",
    role: str = "operator", requester: str = "Lenovo",
) -> dict:
    """导入真实指标 CSV。

    流程：
    1. 权限检查；无权限时写拒绝审计并返回 403；
    2. 读取请求体，校验非空且不超过 5 MB；
    3. 使用 UTF-8 或 UTF-8 BOM 解码；
    4. 调用 import_metric_csv 解析并 upsert；
    5. 写审计记录导入结果。
    """
    if not permitted(role, "request_change"):
        write_audit(requester, "metric_import_denied", {
            "filename": filename[:180], "role": role,
        })
        raise HTTPException(status_code=403, detail="观察者角色不能导入真实指标 CSV。请切换到运维工程师或值班负责人。")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="请选择包含指标数据的 CSV 文件。")
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV 文件不能超过 5 MB。")

    try:
        # utf-8-sig 兼容带 BOM 的 CSV。
        content = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV 必须使用 UTF-8 或 UTF-8 BOM 编码保存。") from exc

    try:
        result = import_metric_csv(content, filename, requester)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    write_audit(requester, "metric_csv_imported", {
        "filename": filename[:180], "role": role, **result,
    })
    return result


@app.get("/api/v1/tools")
def tools_catalog() -> list[dict[str, Any]]:
    """返回所有工具目录。"""
    return catalog()


@app.get("/api/v1/tools/query-schema")
def custom_tools_query_schema(role: str = "viewer") -> list[dict[str, Any]]:
    """返回可配置自定义查询工具的业务表和字段白名单。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无权读取工具配置目录。")
    return custom_tool_schema()


@app.post("/api/v1/tools/custom")
def create_custom_tool(request: CustomToolCreateRequest) -> dict[str, Any]:
    """创建动态注册的只读工具；仅值班负责人可修改工具目录。"""
    if not permitted(request.role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以新增工具。")
    if definition(request.name):
        raise HTTPException(status_code=409, detail="工具名称已被使用。")
    try:
        result = create_custom_query_tool(
            request.name, request.description, request.category, request.table_name,
            request.columns, [item.model_dump() for item in request.filters], request.max_rows,
            request.requester,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "custom_tool_created", {
        "tool": result["name"], "table": result["table_name"],
        "columns": result["columns"], "role": request.role,
    })
    return result


@app.delete("/api/v1/tools/custom/{tool_name}")
def remove_custom_tool(tool_name: str, role: str = "viewer", requester: str = "Lenovo") -> dict[str, Any]:
    """删除自定义工具定义，不影响内置工具。"""
    if not permitted(role, "approve_change"):
        raise HTTPException(status_code=403, detail="只有值班负责人可以删除自定义工具。")
    if not delete_custom_query_tool(tool_name):
        raise HTTPException(status_code=404, detail="自定义工具不存在或已删除。")
    write_audit(requester, "custom_tool_deleted", {"tool": tool_name, "role": role})
    return {"deleted": True, "tool": tool_name}


@app.get("/api/v1/tools/history")
def tools_history(role: str = "viewer", limit: int = 8) -> list[dict]:
    """返回最近的工具调用相关审计事件。

    过滤工具调用及自定义工具目录变更事件。
    """
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无工具调用记录查看权限。")
    actions = {"tool_invoked", "tool_approval_requested", "tool_access_denied", "custom_tool_created", "custom_tool_deleted"}
    return [
        event for event in list_audit(limit=200) if event["action"] in actions
    ][:min(max(limit, 1), 30)]


@app.post("/api/v1/tools/{tool_name}/invoke")
def invoke_tool(
    tool_name: str,
    request: ToolInvokeRequest = ToolInvokeRequest(role="viewer", requester="Lenovo"),
) -> dict:
    """调用一个工具。

    - 工具不存在返回 404；
    - 风险等级为 manual 的工具需要 request_change 权限；
    - manual 工具不直接执行，而是创建审批单；
    - 只读工具直接调用并写审计。
    """
    tool = definition(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail="工具不存在")

    # manual 风险工具需要 request_change 权限，其他只需 read。
    required_permission = "request_change" if tool.risk == "manual" else "read"
    if not permitted(request.role, required_permission):
        write_audit(request.requester, "tool_access_denied", {
            "tool": tool_name, "role": request.role,
            "required_permission": required_permission,
        })
        raise HTTPException(status_code=403, detail="当前角色无该工具调用权限。")

    # manual 风险工具不直接执行，创建审批单。
    if tool.risk == "manual":
        approval_id = create_approval(
            request.requester, f"TOOL {tool_name}",
            f"工具 {tool.name} 会改变运维状态，需要人工审批。",
        )
        write_audit(request.requester, "tool_approval_requested", {
            "tool": tool_name, "approval_id": approval_id, "role": request.role,
        })
        return {
            "status": "approval_required", "tool": tool_name,
            "approval_id": approval_id,
            "message": "已创建工具变更审批单，请前往审批中心确认。",
        }

    # 只读工具直接执行。
    try:
        result = invoke(tool_name, query=request.query, arguments=request.arguments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "tool_invoked", {
        "tool": tool_name, "status": result["status"], "role": request.role,
        "filter_fields": sorted((request.arguments.get("filters") or {}).keys())
        if isinstance(request.arguments.get("filters", {}), dict) else [],
    })
    return result


@app.get("/api/v1/memory/{requester}")
def memory(requester: str, limit: int = 6) -> list[dict[str, str]]:
    """返回指定请求人的最近会话记忆，limit 限制在 1~30。"""
    return recent_memory(requester, min(max(limit, 1), 30))


@app.get("/api/v1/evaluations/cases")
def evaluation_cases(role: str = "viewer") -> list[dict]:
    """返回所有可用评测用例（基线 + 自定义）。"""
    if not permitted(role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无评测用例查看权限。")
    return available_evaluation_cases()


@app.post("/api/v1/evaluations/cases", status_code=201)
def create_evaluation_case(request: EvaluationCaseRequest) -> dict:
    """新增自定义评测用例。

    - 需要 request_change 权限；
    - 校验失败返回 400；
    - 写审计并返回 source="custom"。
    """
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="观察者角色不能新增评测用例。")
    try:
        case = add_evaluation_case(request.name, request.question, request.expected_status, request.requester)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "evaluation_case_created", {
        "case_id": case["id"], "name": case["name"],
        "expected_status": case["expected_status"], "role": request.role,
    })
    return {**case, "source": "custom"}


@app.delete("/api/v1/evaluations/cases/{case_id}")
def remove_evaluation_case(case_id: str, request: MutationActorRequest) -> dict:
    """删除自定义评测用例，并写审计。"""
    if not permitted(request.role, "request_change"):
        raise HTTPException(status_code=403, detail="观察者角色不能删除评测用例。")
    if not delete_evaluation_case(case_id):
        raise HTTPException(status_code=404, detail="自定义评测用例不存在。")
    write_audit(request.requester, "evaluation_case_deleted", {
        "case_id": case_id, "role": request.role,
    })
    return {"deleted": True}


@app.post("/api/v1/evaluations/run")
def evaluate(request: EvaluationRunRequest = EvaluationRunRequest()) -> dict:
    """运行 SQL Agent 离线评测。

    - 需要 read 权限；
    - 复用真实工作流，结果反映当前权限、RAG 和 SQL 策略；
    - 运行结束后写审计。
    """
    if not permitted(request.role, "read"):
        raise HTTPException(status_code=403, detail="当前角色无运行评测权限。")
    try:
        report = run_evaluation(request.scope, request.case_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_audit(request.requester, "evaluation_completed", {
        "scope": report["scope"], "dataset_size": report["dataset_size"],
        "passed": report["passed"], "role": request.role,
    })
    return report
