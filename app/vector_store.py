"""Small, dependency-free local vector store primitives.

The project keeps vectors in SQLite instead of requiring a separate service. The
hashing model is deterministic and replaceable: a future embedding provider can
write the same normalized vector format without changing the RAG API.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

VECTOR_MODEL = "local-hash-bow-v1"
VECTOR_DIMENSIONS = 64


def tokens(text: str) -> list[str]:
    """Tokenize English terms and overlapping Chinese n-grams."""
    english = re.findall(r"[a-z0-9_/-]+", text.lower())
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    grams = [chinese[index:index + width] for width in (1, 2, 3) for index in range(max(0, len(chinese) - width + 1))]
    return english + grams


def embed(text: str, dimensions: int = VECTOR_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    for token, count in Counter(tokens(text)).items():
        bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % dimensions
        vector[bucket] += 1 + math.log(count)
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / length, 8) for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
