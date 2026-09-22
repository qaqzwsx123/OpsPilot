"""Local Chroma index for searchable knowledge and operational context.

本模块负责在本地维护一个可重建的 Chroma 向量索引，用于知识库文档和运维上下文的语义检索。

设计原则：
1. SQLite 仍然是唯一事实来源（source of truth）；
2. Chroma 只存储“可重建”的本地搜索索引，位于 data/chroma；
3. 删除或重建 Chroma 索引不会修改任何业务表；
4. 所有集合名称固定，避免页面、重建任务和检索器各自创建不同集合；
5. 仅允许访问项目预定义的集合，防止把 Chroma 当成任意数据库入口；
6. 禁用匿名遥测，避免本地数据外泄。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

from pathlib import Path  # 用于跨平台路径拼接和解析
from typing import Any  # 标注较宽松的字典结构，便于适配不同数据来源
import os  # 用于设置环境变量，在导入 chromadb 前禁用遥测

# 在导入 chromadb 之前设置环境变量，确保匿名遥测关闭。
# setdefault 表示如果外部已经设置，则不覆盖外部配置。
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

import chromadb  # 本地 Chroma 向量数据库客户端
from chromadb.config import Settings  # Chroma 客户端配置

from app.vector_store import VECTOR_DIMENSIONS, VECTOR_MODEL, embed  # 向量模型、维度与嵌入函数

# 项目根目录：当前文件位于 app/ 下，parent.parent 即项目根目录。
ROOT = Path(__file__).resolve().parent.parent

# Chroma 持久化目录：项目根目录下的 data/chroma。
CHROMA_DIR = ROOT / "data" / "chroma"

# Chroma 集合名称固定，避免页面、重建任务和检索器各自创建不同集合。
# key 是业务逻辑名称，value 是 Chroma 中实际存储的集合名。
COLLECTION_NAMES = {
    "knowledge": "ops_knowledge",      # 知识库文档
    "assets": "ops_assets",            # 资产
    "alerts": "ops_alerts",            # 告警
    "tickets": "ops_tickets",          # 工单
    "work_orders": "ops_work_orders",  # 作业单
}

# 全局 Chroma 客户端实例，延迟初始化。
# 使用全局变量可以避免每次调用都重新创建 PersistentClient。
_client: chromadb.ClientAPI | None = None


def _get_client() -> chromadb.ClientAPI:
    """获取或创建 Chroma 持久化客户端。

    作用：
    - 延迟创建持久化客户端，避免导入模块时立即创建数据目录；
    - 确保 Chroma 数据目录存在；
    - 关闭匿名遥测和产品遥测，使用项目自定义的 NoopTelemetry。

    返回：
    - chromadb.ClientAPI：Chroma 客户端实例。

    安全边界：
    - 该函数只负责创建客户端，不直接读写业务数据；
    - 所有数据变更都应通过 _replace_collection 或 rebuild_chroma_index 等受控流程完成。
    """
    # 声明使用全局变量，保证整个进程只创建一个客户端。
    global _client

    # 如果尚未初始化，则创建持久化客户端。
    if _client is None:
        # 确保 data/chroma 目录存在，parents=True 允许递归创建。
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)

        # 创建 PersistentClient，数据存储在 CHROMA_DIR。
        _client = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(
                anonymized_telemetry=False,  # 关闭匿名遥测
                # 指定自定义遥测实现，进一步确保不会发送遥测数据。
                chroma_product_telemetry_impl="app.chroma_telemetry.NoopTelemetry",
                chroma_telemetry_impl="app.chroma_telemetry.NoopTelemetry",
            ),
        )

    # 返回全局客户端实例。
    return _client


def _get_collection(name: str):
    """获取或创建指定名称的 Chroma 集合。

    参数：
    - name：Chroma 集合名称，通常来自 COLLECTION_NAMES 的 value。

    返回：
    - Chroma Collection 对象。

    说明：
    - 集合使用余弦距离（cosine）；
    - 元数据中记录嵌入模型和维度，便于后续排查和重建；
    - embedding_function=None 表示不使用 Chroma 内置嵌入函数，
      而是由本模块通过 app.vector_store.embed 显式生成向量。
    """
    return _get_client().get_or_create_collection(
        name=name,
        metadata={
            "hnsw:space": "cosine",              # 使用余弦距离
            "embedding_model": VECTOR_MODEL,     # 记录嵌入模型名称
            "embedding_dimensions": VECTOR_DIMENSIONS,  # 记录向量维度
        },
        embedding_function=None,  # 不使用 Chroma 内置嵌入，改为外部显式传入
    )


def _replace_collection(name: str, rows: list[dict[str, Any]]) -> int:
    """用给定数据完整替换指定 Chroma 集合的内容。

    该函数用于重建索引：集合是可重建索引，因此先清理旧内容，
    再从 SQLite 事实表完整写入最新数据。

    参数：
    - name：Chroma 集合名称；
    - rows：待写入的记录列表，每条记录至少包含：
      * id：记录唯一标识；
      * text：用于生成嵌入的文本；
      * document（可选）：实际存储的文档内容，默认使用 text；
      * metadata：元数据字典。

    返回：
    - int：成功写入的记录数量。

    安全边界：
    - 只操作 Chroma 索引，不修改 SQLite 业务表；
    - 调用方应确保 rows 来自权威数据源。
    """
    # 获取或创建集合。
    collection = _get_collection(name)

    # 获取集合中已有的所有 ID（include=[] 表示不返回文档和元数据，只取 ID）。
    existing = collection.get(include=[]).get("ids", [])

    # 如果存在旧数据，先删除，实现“完整替换”。
    if existing:
        collection.delete(ids=existing)

    # 如果没有新数据，直接返回 0。
    if not rows:
        return 0

    # 使用 upsert 批量写入：
    # - ids：字符串化后的记录 ID；
    # - embeddings：对 row["text"] 调用 embed 生成向量；
    # - documents：存储的文档内容，优先使用 row["document"]，否则使用 row["text"]；
    # - metadatas：元数据字典。
    collection.upsert(
        ids=[str(row["id"]) for row in rows],
        embeddings=[embed(row["text"]) for row in rows],
        documents=[row.get("document", row["text"]) for row in rows],
        metadatas=[row["metadata"] for row in rows],
    )

    # 返回写入的记录数量。
    return len(rows)


def rebuild_chroma_index() -> dict[str, Any]:
    """从权威 SQLite 表重建所有本地 Chroma 集合。

    该函数是索引重建的入口，负责：
    1. 从 SQLite 读取知识库分块、资产、告警、工单、作业单；
    2. 为每条记录构造统一的 text、document 和 metadata；
    3. 调用 _replace_collection 完整替换各个 Chroma 集合；
    4. 返回重建结果统计。

    返回：
    - dict：包含状态、路径、模型、维度、各集合数量及总数。

    安全边界：
    - 只读取 SQLite 事实表，不修改业务数据；
    - Chroma 索引可随时删除并重建，不影响 SQLite 中的事实数据。
    """
    # 延迟导入数据库模块，避免循环导入或导入时建立数据库连接。
    from app.database import connect, knowledge_chunks

    # 构造知识库记录列表。
    knowledge_rows = []
    for row in knowledge_chunks():
        # 将标题、标签和内容拼接成用于嵌入的文本。
        text = f"{row['title']} {row['tags']}\n{row['content']}"

        knowledge_rows.append({
            # 使用 document_id 和 chunk_index 构造稳定且唯一的 ID。
            "id": f"knowledge:{row['document_id']}:{row['chunk_index']}",
            "text": text,                    # 用于生成嵌入
            "document": row["content"],      # 实际存储的文档内容
            "metadata": {
                "source_type": "knowledge",
                "document_id": int(row["document_id"]),
                "chunk_index": int(row["chunk_index"]),
                "title": str(row["title"]),
                "tags": str(row["tags"] or ""),
            },
        })

    # 初始化业务数据行字典，分别对应资产、告警、工单、作业单。
    business_rows: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("assets", "alerts", "tickets", "work_orders")
    }

    # 使用 with 管理数据库连接，确保自动关闭。
    with connect() as conn:
        # 读取资产表，构造资产记录。
        for row in conn.execute(
            "SELECT id, name, region, status, owner, updated_at FROM assets ORDER BY id"
        ).fetchall():
            item = dict(row)
            # 构造用于嵌入的自然语言描述。
            item["text"] = (
                f"资产 {item['name']}，区域 {item['region']}，状态 {item['status']}，"
                f"负责人 {item['owner']}，更新时间 {item['updated_at']}"
            )
            # 构造元数据，保留关键字段便于过滤和展示。
            item["metadata"] = {
                "source_type": "asset",
                "record_id": int(item["id"]),
                "title": str(item["name"]),
                "region": str(item["region"]),
                "status": str(item["status"]),
            }
            business_rows["assets"].append(item)

        # 读取告警表，构造告警记录。
        for row in conn.execute(
            "SELECT id, asset_id, severity, title, status, created_at FROM alerts ORDER BY id"
        ).fetchall():
            item = dict(row)
            item["text"] = (
                f"告警 {item['title']}，等级 {item['severity']}，资产 {item['asset_id']}，"
                f"状态 {item['status']}，创建于 {item['created_at']}"
            )
            item["metadata"] = {
                "source_type": "alert",
                "record_id": int(item["id"]),
                "title": str(item["title"]),
                "severity": str(item["severity"]),
                "status": str(item["status"]),
            }
            business_rows["alerts"].append(item)

        # 读取工单表，构造工单记录。
        for row in conn.execute(
            "SELECT id, priority, title, status, assignee, created_at FROM tickets ORDER BY id"
        ).fetchall():
            item = dict(row)
            # assignee 可能为空，使用“未分配”兜底。
            item["text"] = (
                f"工单 {item['title']}，优先级 {item['priority']}，状态 {item['status']}，"
                f"处理人 {item['assignee'] or '未分配'}，创建于 {item['created_at']}"
            )
            item["metadata"] = {
                "source_type": "ticket",
                "record_id": int(item["id"]),
                "title": str(item["title"]),
                "priority": str(item["priority"]),
                "status": str(item["status"]),
            }
            business_rows["tickets"].append(item)

        # 读取作业单表，构造作业单记录。
        for row in conn.execute(
            "SELECT id, asset_id, action, status, created_at FROM work_orders ORDER BY id"
        ).fetchall():
            item = dict(row)
            item["text"] = (
                f"作业单 {item['action']}，资产 {item['asset_id']}，"
                f"状态 {item['status']}，创建于 {item['created_at']}"
            )
            item["metadata"] = {
                "source_type": "work_order",
                "record_id": int(item["id"]),
                "title": str(item["action"]),
                "status": str(item["status"]),
            }
            business_rows["work_orders"].append(item)

    # 先重建知识库集合，并记录数量。
    counts = {
        "knowledge": _replace_collection(COLLECTION_NAMES["knowledge"], knowledge_rows)
    }

    # 再重建其余业务集合，并合并数量统计。
    counts.update({
        key: _replace_collection(COLLECTION_NAMES[key], rows)
        for key, rows in business_rows.items()
    })

    # 返回重建结果摘要。
    return {
        "status": "ready",
        "path": str(CHROMA_DIR),
        "model": VECTOR_MODEL,
        "dimensions": VECTOR_DIMENSIONS,
        "collections": counts,
        "total": sum(counts.values()),
    }


def chroma_stats() -> dict[str, Any]:
    """获取 Chroma 索引的整体统计信息。

    返回：
    - dict：包含状态、存储类型、路径、模型、维度、各集合计数及总数。

    说明：
    - 兼容不同 Chroma 版本：0.6 返回集合名称字符串，旧版本返回对象。
    """
    client = _get_client()
    collections = {}

    # 遍历所有集合，获取每个集合的记录数。
    for name in client.list_collections():
        # Chroma 0.6 返回集合名称字符串；旧版本返回对象，需要取 .name。
        collection_name = name if isinstance(name, str) else name.name

        # 获取集合对象并统计数量。
        collection = client.get_collection(collection_name)
        collections[collection_name] = collection.count()

    # 返回统计摘要。
    return {
        "status": "ready",
        "storage": "Chroma PersistentClient",
        "path": str(CHROMA_DIR),
        "model": VECTOR_MODEL,
        "dimensions": VECTOR_DIMENSIONS,
        "collections": collections,
        "total": sum(collections.values()),
    }


def chroma_collection_catalog() -> list[dict[str, Any]]:
    """获取 Chroma 集合目录，按名称排序。

    返回：
    - list[dict]：每项包含集合名称和记录数。

    说明：
    - 用于前端展示当前有哪些可浏览的集合。
    """
    client = _get_client()
    result = []

    # 遍历所有集合，构造名称与数量列表。
    for name in client.list_collections():
        # 兼容 Chroma 0.6 字符串和旧版对象。
        collection_name = name if isinstance(name, str) else name.name
        collection = client.get_collection(collection_name)
        result.append({"name": collection_name, "count": collection.count()})

    # 按集合名称排序，保证输出稳定。
    return sorted(result, key=lambda item: item["name"])


def chroma_collection_records(collection_name: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """分页浏览指定 Chroma 集合中的记录。

    参数：
    - collection_name：集合名称，必须是 COLLECTION_NAMES 中定义的集合；
    - limit：返回记录数上限，默认 100，内部限制在 1~200；
    - offset：偏移量，默认 0，内部限制不小于 0。

    返回：
    - list[dict]：每项包含 id、document、metadata。

    安全边界：
    - 只允许浏览项目定义的集合，防止把 Chroma 当成任意数据库入口。
    """
    # 只允许访问预定义集合，否则抛出 ValueError。
    allowed = set(COLLECTION_NAMES.values())
    if collection_name not in allowed:
        raise ValueError("不支持的 Chroma 集合")

    # 获取集合对象。
    collection = _get_client().get_collection(collection_name)

    # 查询记录：include 指定返回文档和元数据。
    result = collection.get(
        limit=max(1, min(limit, 200)),  # 限制 limit 在 1~200 之间
        offset=max(0, offset),          # offset 不能为负数
        include=["documents", "metadatas"],
    )

    # 提取 ID、文档和元数据列表，处理可能为空的情况。
    ids = result.get("ids", [])
    documents = result.get("documents", []) or []
    metadatas = result.get("metadatas", []) or []

    # 将三个列表按索引组合成统一结构。
    return [
        {
            "id": record_id,
            "document": documents[index] if index < len(documents) else "",
            "metadata": metadatas[index] if index < len(metadatas) else {},
        }
        for index, record_id in enumerate(ids)
    ]


def search_knowledge(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """在知识库集合中进行语义检索。

    参数：
    - query：用户查询文本；
    - top_k：返回结果数量，默认 3，内部限制在 1~10。

    返回：
    - list[dict]：每条结果包含 content、title、tags、document_id、
      chunk_index 和 semantic_score。

    说明：
    - 将 Chroma 距离转换为前端可解释的语义得分；
    - 保留来源元数据，便于追溯知识库文档和分块。
    """
    # 获取知识库集合。
    collection = _get_collection(COLLECTION_NAMES["knowledge"])

    # 如果集合为空，直接返回空列表，避免无意义查询。
    if not collection.count():
        return []

    # 调用 Chroma 查询：
    # - query_embeddings：对查询文本生成嵌入；
    # - n_results：限制在 1~10；
    # - include：返回文档、元数据和距离。
    result = collection.query(
        query_embeddings=[embed(query)],
        n_results=max(1, min(top_k, 10)),
        include=["documents", "metadatas", "distances"],
    )

    # 提取第一组结果（单查询）。
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]

    # 将距离转换为语义得分：距离越小，得分越高。
    # 使用 1 - distance，并限制在 0 以上，保留 4 位小数。
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