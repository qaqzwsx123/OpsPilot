"""Local Chroma index for searchable knowledge and operational context.

SQLite remains the source of truth. Chroma only stores a rebuildable, local
search index under data/chroma, so deleting or rebuilding it cannot mutate the
business tables.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os

os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")
import chromadb
from chromadb.config import Settings

from app.vector_store import VECTOR_DIMENSIONS, VECTOR_MODEL, embed

ROOT = Path(__file__).resolve().parent.parent
CHROMA_DIR = ROOT / "data" / "chroma"
# Chroma 集合名称固定，避免页面、重建任务和检索器各自创建不同集合。
COLLECTION_NAMES = {
    "knowledge": "ops_knowledge",
    "assets": "ops_assets",
    "alerts": "ops_alerts",
    "tickets": "ops_tickets",
    "work_orders": "ops_work_orders",
}

_client: chromadb.ClientAPI | None = None


# 作用：说明函数 _get_client 的输入、输出与安全边界，避免调用方越过受控流程。
def _get_client() -> chromadb.ClientAPI:
    # 延迟创建持久化客户端，避免导入模块时立即创建数据目录。
    global _client
    if _client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(
                anonymized_telemetry=False,
                chroma_product_telemetry_impl="app.chroma_telemetry.NoopTelemetry",
                chroma_telemetry_impl="app.chroma_telemetry.NoopTelemetry",
            ),
        )
    return _client


# 作用：说明函数 _get_collection 的输入、输出与安全边界，避免调用方越过受控流程。
def _get_collection(name: str):
    return _get_client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine", "embedding_model": VECTOR_MODEL, "embedding_dimensions": VECTOR_DIMENSIONS},
        embedding_function=None,
    )


# 作用：说明函数 _replace_collection 的输入、输出与安全边界，避免调用方越过受控流程。
def _replace_collection(name: str, rows: list[dict[str, Any]]) -> int:
    # 集合是可重建索引：先清理旧内容，再从 SQLite 事实表完整写入。
    collection = _get_collection(name)
    existing = collection.get(include=[]).get("ids", [])
    if existing:
        collection.delete(ids=existing)
    if not rows:
        return 0
    collection.upsert(
        ids=[str(row["id"]) for row in rows],
        embeddings=[embed(row["text"]) for row in rows],
        documents=[row.get("document", row["text"]) for row in rows],
        metadatas=[row["metadata"] for row in rows],
    )
    return len(rows)


def rebuild_chroma_index() -> dict[str, Any]:
    """Rebuild all local collections from the authoritative SQLite tables."""
    from app.database import connect, knowledge_chunks

    knowledge_rows = []
    for row in knowledge_chunks():
        text = f"{row['title']} {row['tags']}\n{row['content']}"
        knowledge_rows.append({
            "id": f"knowledge:{row['document_id']}:{row['chunk_index']}",
            "text": text,
            "document": row["content"],
            "metadata": {
                "source_type": "knowledge",
                "document_id": int(row["document_id"]),
                "chunk_index": int(row["chunk_index"]),
                "title": str(row["title"]),
                "tags": str(row["tags"] or ""),
            },
        })

    business_rows: dict[str, list[dict[str, Any]]] = {key: [] for key in ("assets", "alerts", "tickets", "work_orders")}
    with connect() as conn:
        for row in conn.execute("SELECT id, name, region, status, owner, updated_at FROM assets ORDER BY id").fetchall():
            item = dict(row); item["text"] = f"资产 {item['name']}，区域 {item['region']}，状态 {item['status']}，负责人 {item['owner']}，更新时间 {item['updated_at']}"
            item["metadata"] = {"source_type": "asset", "record_id": int(item["id"]), "title": str(item["name"]), "region": str(item["region"]), "status": str(item["status"])}
            business_rows["assets"].append(item)
        for row in conn.execute("SELECT id, asset_id, severity, title, status, created_at FROM alerts ORDER BY id").fetchall():
            item = dict(row); item["text"] = f"告警 {item['title']}，等级 {item['severity']}，资产 {item['asset_id']}，状态 {item['status']}，创建于 {item['created_at']}"
            item["metadata"] = {"source_type": "alert", "record_id": int(item["id"]), "title": str(item["title"]), "severity": str(item["severity"]), "status": str(item["status"])}
            business_rows["alerts"].append(item)
        for row in conn.execute("SELECT id, priority, title, status, assignee, created_at FROM tickets ORDER BY id").fetchall():
            item = dict(row); item["text"] = f"工单 {item['title']}，优先级 {item['priority']}，状态 {item['status']}，处理人 {item['assignee'] or '未分配'}，创建于 {item['created_at']}"
            item["metadata"] = {"source_type": "ticket", "record_id": int(item["id"]), "title": str(item["title"]), "priority": str(item["priority"]), "status": str(item["status"])}
            business_rows["tickets"].append(item)
        for row in conn.execute("SELECT id, asset_id, action, status, created_at FROM work_orders ORDER BY id").fetchall():
            item = dict(row); item["text"] = f"作业单 {item['action']}，资产 {item['asset_id']}，状态 {item['status']}，创建于 {item['created_at']}"
            item["metadata"] = {"source_type": "work_order", "record_id": int(item["id"]), "title": str(item["action"]), "status": str(item["status"])}
            business_rows["work_orders"].append(item)

    counts = {"knowledge": _replace_collection(COLLECTION_NAMES["knowledge"], knowledge_rows)}
    counts.update({key: _replace_collection(COLLECTION_NAMES[key], rows) for key, rows in business_rows.items()})
    return {"status": "ready", "path": str(CHROMA_DIR), "model": VECTOR_MODEL, "dimensions": VECTOR_DIMENSIONS, "collections": counts, "total": sum(counts.values())}


# 作用：说明函数 chroma_stats 的输入、输出与安全边界，避免调用方越过受控流程。
def chroma_stats() -> dict[str, Any]:
    client = _get_client()
    collections = {}
    for name in client.list_collections():
        # Chroma 0.6 returns collection names; older releases returned objects.
        collection_name = name if isinstance(name, str) else name.name
        collection = client.get_collection(collection_name)
        collections[collection_name] = collection.count()
    return {"status": "ready", "storage": "Chroma PersistentClient", "path": str(CHROMA_DIR), "model": VECTOR_MODEL, "dimensions": VECTOR_DIMENSIONS, "collections": collections, "total": sum(collections.values())}


# 作用：说明函数 chroma_collection_catalog 的输入、输出与安全边界，避免调用方越过受控流程。
def chroma_collection_catalog() -> list[dict[str, Any]]:
    client = _get_client()
    result = []
    for name in client.list_collections():
        collection_name = name if isinstance(name, str) else name.name
        collection = client.get_collection(collection_name)
        result.append({"name": collection_name, "count": collection.count()})
    return sorted(result, key=lambda item: item["name"])


# 作用：说明函数 chroma_collection_records 的输入、输出与安全边界，避免调用方越过受控流程。
def chroma_collection_records(collection_name: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    # 只允许浏览项目定义的集合，防止把 Chroma 当成任意数据库入口。
    allowed = set(COLLECTION_NAMES.values())
    if collection_name not in allowed:
        raise ValueError("不支持的 Chroma 集合")
    collection = _get_client().get_collection(collection_name)
    result = collection.get(
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
        include=["documents", "metadatas"],
    )
    ids = result.get("ids", [])
    documents = result.get("documents", []) or []
    metadatas = result.get("metadatas", []) or []
    return [
        {"id": record_id, "document": documents[index] if index < len(documents) else "", "metadata": metadatas[index] if index < len(metadatas) else {}}
        for index, record_id in enumerate(ids)
    ]


# 作用：说明函数 search_knowledge 的输入、输出与安全边界，避免调用方越过受控流程。
def search_knowledge(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    # 将 Chroma 距离转换为前端可解释的语义得分，并保留来源元数据。
    collection = _get_collection(COLLECTION_NAMES["knowledge"])
    if not collection.count():
        return []
    result = collection.query(query_embeddings=[embed(query)], n_results=max(1, min(top_k, 10)), include=["documents", "metadatas", "distances"])
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]
    return [
        {
            "content": document,
            "title": metadata.get("title", "知识库文档"),
            "tags": metadata.get("tags", ""),
            "document_id": metadata.get("document_id"),
            "chunk_index": metadata.get("chunk_index", 0),
            "semantic_score": round(max(0.0, 1.0 - float(distance)), 4),
        }
        for document, metadata, distance in zip(documents, metadatas, distances)
    ]
