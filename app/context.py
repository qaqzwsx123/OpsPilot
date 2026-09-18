from __future__ import annotations

import hashlib
from pathlib import Path


class ContextCompressor:
    """Four-step compacting: persist → archive pointer → retain result → concise summary."""

    def __init__(self, directory: Path, token_budget: int = 180):
        self.directory = directory
        self.token_budget = token_budget

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 3)

    def compact(self, text: str) -> tuple[str, dict[str, int | str]]:
        before = self.estimate_tokens(text)
        if before <= self.token_budget:
            return text, {"before": before, "after": before, "strategy": "not_needed"}
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        path = self.directory / f"{digest}.txt"
        path.write_text(text, encoding="utf-8")
        summary = text[: self.token_budget * 2] + f"\n[完整结果已归档: {path.name}]"
        return summary, {"before": before, "after": self.estimate_tokens(summary), "strategy": "persist_then_summarize"}

