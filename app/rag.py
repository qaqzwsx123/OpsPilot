"""知识检索层：优先查询 Chroma，失败时使用本地轻量向量检索。

本模块是知识库 RAG 的统一检索入口，核心设计目标：
1. SQLite 中的 knowledge_chunks 是不可丢失的事实源；
2. Chroma 是可重建的向量索引，作为首选召回路径；
3. Chroma 不可用时，自动回退到本地分词 + 哈希向量 + 余弦相似度；
4. 最终分数由词法命中、语义相似度和标题标签加权组成，兼顾精确和模糊匹配；
5. 每条证据保留标题、分块编号和评分，便于前端引用、评测和人工复核。
安全边界：
- 只读检索，不修改任何业务数据或知识分块；
- 不依赖外部网络，Chroma 失败时完全在本进程内完成回退检索；
- 分词与向量化使用固定算法，可复现、可解释，避免黑盒。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值
import hashlib  # 用于对 token 做稳定哈希，构造本地哈希向量
import math  # 用于 log 加权和向量归一化
import re  # 用于英文词和中文汉字提取
from collections import Counter  # 统计 token 频次，用于哈希向量加权
from typing import Any  # 宽松字典类型标注
from app.chroma_store import search_knowledge as chroma_search_knowledge  # 首选检索路径：Chroma 向量检索
from app.database import knowledge_chunks, rebuild_knowledge_index  # 事实源：SQLite 分块和分块重建
def _tokens(text: str) -> list[str]:
    """混合分词：英文术语 + 重叠中文 n-gram。
    参数：
    - text：待分词文本。
    返回：
    - list[str]：token 列表，包含英文词和中文 1/2/3 元组。
    说明：
    - 英文部分：提取小写字母、数字、下划线、斜杠、连字符组成的词；
      这样 "CPU"、"P95"、"mysql+pymysql" 等术语能保留为一个 token。
    - 中文部分：只保留汉字，再按 1、2、3 元组切分并重叠，
      例如 "设备离线" 会得到 设、备、离、线、设备、备离、离线、设备离、备离线。
      重叠 n-gram 能在无分词器的情况下近似中文分词效果。
    - 该分词器无第三方依赖、可复现，适合本地演示和离线回退。
    """
    # 英文/数字/符号词：小写后提取连续片段。
    english = re.findall(r"[a-z0-9_/-]+", text.lower())
    # 中文：只保留汉字，去掉标点和空白。
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    # 中文重叠 n-gram：宽度 1、2、3 分别切分，重叠以保留边界语义。
    # range(max(0, len(chinese) - width + 1)) 保证短文本也能正确切分且不越界。
    grams = [
        chinese[index:index + width]
        for width in (1, 2, 3)
        for index in range(max(0, len(chinese) - width + 1))
    ]
    # 合并英文和中文 token。
    return english + grams
def _embedding(tokens: list[str], dimensions: int = 64) -> list[float]:
    """把 token 列表映射为固定维度的归一化哈希向量。
    参数：
    - tokens：已分词 token 列表；
    - dimensions：向量维度，默认 64。
    返回：
    - list[float]：L2 归一化后的向量，便于用余弦相似度比较。
    算法（哈希技巧 / hashing trick）：
    1. 用 Counter 统计每个 token 的频次；
    2. 对每个 token 做 SHA-256，取前 8 个十六进制字符转整数，模 dimensions 得到桶；
    3. 桶内累加 1 + log(count)，对高频 token 做对数加权，避免频次过度放大；
    4. 最后做 L2 归一化，使不同长度文本的向量可比。
    说明：
    - 哈希向量无需预训练，完全本地、确定性；
    - dimensions=64 是演示用的小维度，兼顾速度和区分度；
    - 不同 token 可能哈希到同一桶（碰撞），用对数加权部分缓解。
    """
    vector = [0.0] * dimensions
    # 对每个 token，按其频次的对数加权累加到哈希桶。
    for token, count in Counter(tokens).items():
        # 取 SHA-256 前 8 位作为整数，映射到 [0, dimensions) 的桶。
        bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % dimensions
        # 1 + log(count)：保证单次出现也为正，多次出现有更高但边际递减的权重。
        vector[bucket] += 1 + math.log(count)
    # L2 归一化；长度为 0 时用 1.0 避免除零。
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]
def _cosine(left: list[float], right: list[float]) -> float:
    """计算两个等长向量的余弦相似度。
    由于 _embedding 已做 L2 归一化，点积即余弦相似度，无需再除以模长。
    """
    return sum(a * b for a, b in zip(left, right))

class KnowledgeRag:
    """轻量混合 RAG 检索器。
    SQLite 保存文档和分块这一事实源，Chroma 作为可重建的向量索引优先参与召回；Chroma 不可用时
    自动回退到本地分词、哈希向量和余弦相似度。最终分数由词法命中、语义相似度和标题标签加权组成，
    每条证据保留文档标题、分块编号和评分，便于前端引用与评测。
    使用方式：
        rag = KnowledgeRag()
        hits = rag.search("设备离线怎么办", 3)
        answer, citations = rag.answer("设备离线怎么办", 2)
    """
    def search(self, question: str, top_k: int = 3) -> list[dict[str, Any]]:
        """检索与问题最相关的知识分块。
        参数：
        - question：用户自然语言问题；
        - top_k：返回条数上限，默认 3。
        返回：
        - list[dict]：按相关度倒序排列的证据列表，每项包含：
          * title / tags / content / chunk_index / document_id；
          * lexical_score：词法命中率；
          * semantic_score：语义相似度（来自 Chroma 距离或本地余弦）；
          * score：最终加权分数。
        流程：
        1. 先尝试 Chroma 检索；
        2. Chroma 异常或无结果时，回退到 SQLite 分块 + 本地检索；
        3. 统一走 _rank 做加权排序。
        安全边界：
        - 不修改知识库；
        - Chroma 失败不影响检索可用性；
        - 分块为空时自动重建一次，保证演示环境可直接用。
        """
        # Chroma 是可重建索引；SQLite 中的知识分块是不可丢失的事实来源。
        try:
            chroma_hits = chroma_search_knowledge(question, top_k)
        except Exception:
            # Chroma 未就绪、集合缺失或索引损坏时静默回退，避免影响问答。
            chroma_hits = []
        # Chroma 返回结果时，再经过 _rank 做混合打分；
        # 这样即使 Chroma 给出候选，仍会叠加词法和标题加成，提升可解释性。
        if chroma_hits:
            return self._rank(question, chroma_hits, top_k)
        # 回退路径：读取 SQLite 分块作为候选集。
        chunks = knowledge_chunks()
        # 分块为空时重建一次；典型场景是首次部署或数据被清空。
        if not chunks:
            rebuild_knowledge_index()
            chunks = knowledge_chunks()
        # 对问题分词，若无法提取任何 token，则直接返回空。
        query_tokens = _tokens(question)
        if not query_tokens:
            return []
        # 下面这两行在 _rank 中会重新计算，此处保留是为了保持语义清晰，
        # 也便于后续如需在此处做提前过滤。
        query_set = set(query_tokens)
        query_vector = _embedding(query_tokens)
        return self._rank(question, chunks, top_k)
    def _rank(self, question: str, chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        """对候选分块进行混合加权排序。
        参数：
        - question：用户问题；
        - chunks：候选分块列表（来自 Chroma 或 SQLite）；
        - top_k：返回条数上限。
        返回：
        - list[dict]：按最终得分倒序的前 top_k 条证据。
        打分公式：
        - lexical：query token 集合与文档 token 集合的 Jaccard 式命中率；
        - semantic：若 chunk 已有 semantic_score（来自 Chroma），直接使用；
          否则用本地哈希向量计算余弦相似度；
        - title_boost：问题 token 命中标题或标签时加 0.12；
        - 最终 score = lexical * 0.62 + semantic * 0.30 + title_boost。
        设计理由：
        - 词法权重最高，保证术语精确命中优先；
        - 语义权重次之，弥补同义表达；
        - 标题加成突出 SOP 名称匹配，便于“XX 怎么处理”类问题。
        - score > 0.06 的阈值过滤明显无关分块，减少噪声。
        """
        # 问题分词；无 token 时直接返回空，避免无意义打分。
        query_tokens = _tokens(question)
        if not query_tokens:
            return []
        query_set = set(query_tokens)
        query_vector = _embedding(query_tokens)
        scored = []
        for chunk in chunks:
            # 把标题、标签、正文一起分词，标题和标签的术语也会进入文档 token 集合。
            document_tokens = _tokens(
                chunk["title"] + " " + chunk["tags"] + " " + chunk["content"]
            )
            if not document_tokens:
                continue
            # 词法得分：query token 集合被文档覆盖的比例，范围 [0, 1]。
            lexical = len(query_set & set(document_tokens)) / max(1, len(query_set))
            # 语义得分：优先使用 Chroma 给的 semantic_score；
            # 否则用本地哈希向量计算余弦相似度。
            semantic = chunk.get(
                "semantic_score",
                _cosine(query_vector, _embedding(document_tokens)),
            )
            # 标题/标签加成：问题 token 命中标题或标签时加分，突出 SOP 名称匹配。
            title_boost = (
                0.12
                if query_set & set(_tokens(chunk["title"] + " " + chunk["tags"]))
                else 0.0
            )
            # 加权求和，保留 4 位小数便于展示和比较。
            score = round(lexical * 0.62 + semantic * 0.30 + title_boost, 4)
            # 过滤明显无关分块，减少噪声证据。
            if score > 0.06:
                scored.append({
                    **chunk,
                    "lexical_score": round(lexical, 4),
                    "semantic_score": round(semantic, 4),
                    "score": score,
                })
        # 按最终得分倒序排序，只返回前 top_k 条。
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:top_k]

    def answer(self, question: str, top_k: int = 2) -> tuple[str, list[str]]:
        """生成基于检索证据的答案文本和引用标题列表。
        参数：
        - question：用户问题；
        - top_k：引用证据条数，默认 2。
        返回
        - tuple：
          * answer：拼接后的答案文本，每条证据附来源、片段编号和得分；
          * citations：去重后的文档标题列表，便于前端展示引用来源。
        说明：
        - 这里不做生成式总结，只做“证据陈列”，保证答案可溯源、可审计；
        - 若无可靠证据，返回明确提示，引导用户补充信息；
        - 引用标题用 dict.fromkeys 去重并保持出现顺序。
        """
        # 检索 Top-K 证据。
        evidence = self.search(question, top_k)
        # 无证据时给出可操作的提示，而不是编造答案。
        if not evidence:
            return "没有检索到足够可靠的运维知识。请补充设备、告警等级或故障现象。", []
    # 把每条证据格式化为带来源、片段编号和混合得分的条目。
        answer = "\n".join(
            f"- {item['content']}\n"
            f"  [证据：{item['title']} · 片段 {item['chunk_index'] + 1} · 混合得分 {item['score']}]"
            for item in evidence
        )
        # 引用标题去重并保持首次出现顺序。
        return answer, list(dict.fromkeys(item["title"] for item in evidence))