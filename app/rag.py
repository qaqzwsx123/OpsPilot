from __future__ import annotations

import re

from app.database import connect


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{1,3}", text.lower()))


class KnowledgeRag:
    """A dependency-free lexical retriever for the demo; replace with embeddings in production."""

    def answer(self, question: str, top_k: int = 2) -> tuple[str, list[str]]:
        query_tokens = _tokens(question)
        with connect() as conn:
            docs = [dict(row) for row in conn.execute("SELECT title, content, tags FROM knowledge_documents").fetchall()]
        ranked = sorted(
            docs,
            key=lambda doc: len(query_tokens & _tokens(doc["title"] + " " + doc["content"] + " " + doc["tags"])),
            reverse=True,
        )[:top_k]
        useful = [doc for doc in ranked if query_tokens & _tokens(doc["title"] + " " + doc["content"] + " " + doc["tags"])]
        if not useful:
            return "没有检索到足够可靠的运维知识。请补充设备、告警等级或故障现象。", []
        answer = "\n".join(f"- {doc['content']}" for doc in useful)
        return answer, [doc["title"] for doc in useful]

