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

# 当前演示使用的确定性哈希向量模型名称，写入 Chroma 元数据便于识别索引版本。
VECTOR_MODEL = "local-hash-bow-v1"
# 向量维度；变更后需要重建 SQLite/Chroma 中的全部索引。
VECTOR_DIMENSIONS = 64


def tokens(text: str) -> list[str]:
    # 中文按字符/词片段切分，英文和数字按连续片段切分，保证无额外依赖。
    """Tokenize English terms and overlapping Chinese n-grams."""
    english = re.findall(r"[a-z0-9_/-]+", text.lower())
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    grams = [chinese[index:index + width] for width in (1, 2, 3) for index in range(max(0, len(chinese) - width + 1))]
    return english + grams


# 作用：说明函数 embed 的输入、输出与安全边界，避免调用方越过受控流程。
def embed(text: str, dimensions: int = VECTOR_DIMENSIONS) -> list[float]:
    # 通过稳定哈希生成稀疏向量；生产环境可替换为真实 embedding。
    vector = [0.0] * dimensions
    for token, count in Counter(tokens(text)).items():
        bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % dimensions
        vector[bucket] += 1 + math.log(count)
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / length, 8) for value in vector]


# 作用：说明函数 cosine 的输入、输出与安全边界，避免调用方越过受控流程。
def cosine(left: list[float], right: list[float]) -> float:
    # 余弦相似度用于比较查询向量与文档向量的方向。
    return sum(a * b for a, b in zip(left, right))


# 作用：说明函数 content_hash 的输入、输出与安全边界，避免调用方越过受控流程。
def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
