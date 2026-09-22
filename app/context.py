"""上下文压缩：归档长结果，并把短摘要保留给后续 Agent 步骤。"""

from __future__ import annotations

import hashlib
from pathlib import Path


class ContextCompressor:
    """面向 Agent 的本地上下文压缩器。

    先估算输入 Token，超预算时把完整文本落盘，再把短摘要和归档文件名交给后续节点。
    这样既减少模型上下文，也保留了故障复盘所需的原始证据；它不删除业务数据。
    """

    def __init__(self, directory: Path, token_budget: int = 180):
        # 归档目录，保存超长工具结果或工作流上下文的完整副本。
        self.directory = directory
        # 后续模型步骤允许携带的估算 Token 上限。
        self.token_budget = token_budget

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # 这是轻量估算，不替代具体模型 tokenizer；目的是控制本地演示预算。
        return max(1, len(text) // 3)

    def compact(self, text: str) -> tuple[str, dict[str, int | str]]:
        # 原文写入归档目录，返回可放进上下文的摘要和压缩统计。
        before = self.estimate_tokens(text)
        if before <= self.token_budget:
            return text, {"before": before, "after": before, "strategy": "not_needed"}
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        path = self.directory / f"{digest}.txt"
        path.write_text(text, encoding="utf-8")
        summary = text[: self.token_budget * 2] + f"\n[完整结果已归档: {path.name}]"
        return summary, {"before": before, "after": self.estimate_tokens(summary), "strategy": "persist_then_summarize"}
