from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from app.database import knowledge_chunks, rebuild_knowledge_index


def _tokens(text: str) -> list[str]:
    """Mixed tokenizer: English terms plus overlapping Chinese n-grams."""
    english = re.findall(r"[a-z0-9_/-]+", text.lower())
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    grams = [chinese[index:index + width] for width in (1, 2, 3) for index in range(max(0, len(chinese) - width + 1))]
    return english + grams


def _embedding(tokens: list[str], dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token, count in Counter(tokens).items():
        bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % dimensions
        vector[bucket] += 1 + math.log(count)
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class KnowledgeRag:
    """Dependency-free hybrid RAG: chunking, lexical recall, hash-vector recall and reranking."""

    def search(self, question: str, top_k: int = 3) -> list[dict[str, Any]]:
        chunks = knowledge_chunks()
        if not chunks:
            rebuild_knowledge_index()
            chunks = knowledge_chunks()
        query_tokens = _tokens(question)
        if not query_tokens:
            return []
        query_set = set(query_tokens)
        query_vector = _embedding(query_tokens)
        scored = []
        for chunk in chunks:
            document_tokens = _tokens(chunk["title"] + " " + chunk["tags"] + " " + chunk["content"])
            if not document_tokens:
                continue
            lexical = len(query_set & set(document_tokens)) / max(1, len(query_set))
            semantic = _cosine(query_vector, _embedding(document_tokens))
            title_boost = 0.12 if query_set & set(_tokens(chunk["title"] + " " + chunk["tags"])) else 0.0
            score = round(lexical * 0.62 + semantic * 0.30 + title_boost, 4)
            if score > 0.06:
                scored.append({**chunk, "lexical_score": round(lexical, 4), "semantic_score": round(semantic, 4), "score": score})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:top_k]

    def answer(self, question: str, top_k: int = 2) -> tuple[str, list[str]]:
        evidence = self.search(question, top_k)
        if not evidence:
            return "没有检索到足够可靠的运维知识。请补充设备、告警等级或故障现象。", []
        answer = "\n".join(
            f"- {item['content']}\n  [证据：{item['title']} · 片段 {item['chunk_index'] + 1} · 混合得分 {item['score']}]"
            for item in evidence
        )
        return answer, list(dict.fromkeys(item["title"] for item in evidence))
