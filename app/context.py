"""上下文压缩：归档长结果，并把短摘要保留给后续 Agent 步骤。

本模块提供一个轻量的本地上下文压缩器，用于 Agent 工作流中控制模型上下文长度。

设计目标：
1. 当工具返回结果或工作流上下文过长时，避免直接塞进模型导致超上下文；
2. 把完整原文落盘归档，保留故障复盘、审计所需的原始证据；
3. 只把短摘要和归档文件名交给后续节点，减少 Token 消耗；
4. 不删除任何业务数据，仅对“传给模型的上下文”做压缩；
5. 不依赖具体模型的 tokenizer，使用字符数粗略估算 Token，适合本地演示。

安全边界：
- 归档目录由调用方指定，本模块只写入该目录；
- 不修改数据库、不删除业务数据；
- 归档文件名使用内容哈希，避免文件名冲突和路径注入。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

import hashlib  # 用于对完整文本生成稳定摘要，作为归档文件名
from pathlib import Path  # 跨平台路径处理，便于指定归档目录和写入文件

class ContextCompressor:
    """面向 Agent 的本地上下文压缩器。
    先估算输入 Token，超预算时把完整文本落盘，再把短摘要和归档文件名交给后续节点。
    这样既减少模型上下文，也保留了故障复盘所需的原始证据；它不删除业务数据。
    使用方式：
        compressor = ContextCompressor(Path("data/context_archive"), token_budget=180)
        summary, stats = compressor.compact(long_text)
    参数：
    - directory：归档目录，超长文本会写入该目录；
    - token_budget：后续模型步骤允许携带的估算 Token 上限，默认 180。
    注意：
    - token_budget 是估算值，不是精确的模型 Token 数；
    - 压缩策略为“先持久化，再截断摘要”，不会丢失原始数据。
    """
    def __init__(self, directory: Path, token_budget: int = 180):
        # 归档目录，保存超长工具结果或工作流上下文的完整副本。
        # 调用方应确保该目录可写，且不包含敏感信息的越权访问风险。
        self.directory = directory
        # 后续模型步骤允许携带的估算 Token 上限。
        # 超过该上限时，compact 会触发归档和摘要逻辑。
        self.token_budget = token_budget
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算文本的 Token 数量。
        参数：
        - text：待估算的文本。
        返回：
        - int：估算的 Token 数，至少为 1。
        说明：
        - 这是轻量估算，不替代具体模型 tokenizer；目的是控制本地演示预算。
        - 采用 len(text) // 3 的粗略比例，适合中英文混合的运维文本。
        - 使用 max(1, ...) 保证空字符串也返回 1，避免后续除零或零预算异常。
        """
        # 这是轻量估算，不替代具体模型 tokenizer；目的是控制本地演示预算。
        # 对于中文、英文、符号混合文本，约 3 个字符对应 1 个 Token 是一种保守估计。
        return max(1, len(text) // 3)
    def compact(self, text: str) -> tuple[str, dict[str, int | str]]:
        """压缩文本，必要时归档完整原文并返回摘要。
        参数：
        - text：待压缩的完整文本，通常来自工具结果或工作流上下文。
        返回：
        - tuple[str, dict]：
          * 第一个元素是可直接放入模型上下文的文本：
            - 如果未超预算，返回原文；
            - 如果超预算，返回截断摘要 + 归档文件名提示；
          * 第二个元素是压缩统计信息，包含：
            - before：压缩前的估算 Token 数；
            - after：压缩后的估算 Token 数；
            - strategy：压缩策略，可能为 "not_needed" 或 "persist_then_summarize"。
        安全边界：
        - 只在超预算时写入归档目录；
        - 归档文件名使用内容 SHA-256 前 12 位，避免冲突；
        - 不删除、不修改任何业务数据。
        """
        # 原文写入归档目录，返回可放进上下文的摘要和压缩统计。
        # 先估算原始文本的 Token 数。
        before = self.estimate_tokens(text)
        # 如果未超过预算，无需压缩，直接返回原文和 "not_needed" 策略。
        if before <= self.token_budget:
            return text, {
                "before": before,
                "after": before,
                "strategy": "not_needed",
            }
        # 超过预算：确保归档目录存在，parents=True 允许递归创建。
        self.directory.mkdir(parents=True, exist_ok=True)
        # 对完整文本计算 SHA-256，取前 12 位作为归档文件名。
        # 这样相同内容会得到相同文件名，不同内容几乎不会冲突。
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        # 归档文件路径，例如 data/context_archive/ab12cd34ef56.txt。
        path = self.directory / f"{digest}.txt"
        # 将完整原文写入归档文件，编码为 UTF-8，保留原始证据。
        path.write_text(text, encoding="utf-8")
        # 构造摘要：
        # - 取原文前 token_budget * 2 个字符作为摘要主体；
        # - 追加归档文件名提示，方便后续节点或人工追溯完整内容。
        summary = text[: self.token_budget * 2] + f"\n[完整结果已归档: {path.name}]"
        # 返回摘要和压缩统计：
        # - after：摘要的估算 Token 数；
        # - strategy：标记为 "persist_then_summarize"，表示先持久化再摘要。
        return summary, {
            "before": before,
            "after": self.estimate_tokens(summary),
            "strategy": "persist_then_summarize",
        }