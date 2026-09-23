"""按 Token 预算压缩模型上下文，并保留用户消息的来源边界。"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from typing import Any


Message = dict[str, str]
Summarizer = Callable[[list[Message], int], str | None]


class ContextCompressor:
    """为模型请求组装有限长度的消息上下文。

    优先保留固定系统提示、当前请求和最近对话；更早的对话交给调用方摘要。
    如果没有可用摘要器，则用明确标记的摘录降级，避免静默丢弃上下文。
    Token 计数在安装 tiktoken 时使用所选编码；否则使用偏保守的中英文估算。
    """

    def __init__(self, token_budget: int = 8192, output_reserve: int = 1024):
        if token_budget < 256:
            raise ValueError("token_budget 至少为 256")
        if output_reserve < 0 or output_reserve >= token_budget:
            raise ValueError("output_reserve 必须小于 token_budget")
        self.token_budget = token_budget
        self.output_reserve = output_reserve
        self.encoding_name = os.getenv("MODEL_TOKENIZER_ENCODING", "cl100k_base")
        self._encoding: Any = None
        self._encoding_checked = False

    @property
    def counter_name(self) -> str:
        self._load_encoding()
        return f"tiktoken:{self.encoding_name}" if self._encoding else "conservative_estimate"

    def _load_encoding(self) -> None:
        if self._encoding_checked:
            return
        self._encoding_checked = True
        try:
            import tiktoken  # type: ignore[import-not-found]

            self._encoding = tiktoken.get_encoding(self.encoding_name)
        except (ImportError, ValueError, RuntimeError):
            self._encoding = None

    def estimate_tokens(self, text: str) -> int:
        """估算 Token 数；优先走 tiktoken，没有依赖时用中英文混合保守估算。"""
        self._load_encoding()
        if self._encoding is not None:
            try:
                return max(1, len(self._encoding.encode(text, disallowed_special=())))
            except Exception:
                pass

        cjk = sum(
            1
            for char in text
            if "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"
        )
        other = len(text) - cjk
        return max(1, cjk + math.ceil(other / 3.2))

    def estimate_messages(self, messages: list[Message]) -> int:
        """计入消息角色和少量协议开销，避免只计算正文。"""
        return sum(self.estimate_tokens(item.get("role", "") + item.get("content", "")) + 4 for item in messages)

    def fit_text(self, text: str, token_budget: int) -> str:
        """将单段超长文本压到预算内，保留开头和结尾并显式标记省略区间。"""
        if token_budget <= 0:
            return "[内容超出上下文预算，已省略]"
        if self.estimate_tokens(text) <= token_budget:
            return text

        marker = "\n[中间内容因上下文预算限制而省略]\n"
        low, high = 2, min(len(text), token_budget * 4)
        best = ""
        while low <= high:
            take = (low + high) // 2
            left_chars = max(1, take // 2)
            right_chars = max(1, take - left_chars)
            candidate = text[:left_chars] + marker + text[-right_chars:]
            if self.estimate_tokens(candidate) <= token_budget:
                best = candidate
                low = take + 1
            else:
                high = take - 1
        if best:
            return best
        return text[: max(1, token_budget)]

    def compact_messages(
        self,
        messages: list[Message],
        *,
        fixed_messages: list[Message] | None = None,
        summarizer: Summarizer | None = None,
        max_recent_messages: int = 8,
    ) -> tuple[list[Message], dict[str, int | str]]:
        """摘要较早历史，保留最近对话，并确保请求落在输入预算内。"""
        fixed = fixed_messages or []
        prompt_budget = self.token_budget - self.output_reserve
        fixed_cost = self.estimate_messages(fixed)
        conversation_budget = max(1, prompt_budget - fixed_cost)
        before = fixed_cost + self.estimate_messages(messages)

        if self.estimate_messages(messages) <= conversation_budget:
            return list(messages), {
                "before": before,
                "after": before,
                "input_budget": prompt_budget,
                "strategy": "not_needed",
                "token_counter": self.counter_name,
            }

        # 最近对话优先占用约 70% 预算；至少保留最后一条（当前用户请求）。
        recent_budget = max(1, int(conversation_budget * 0.7))
        recent_reversed: list[Message] = []
        recent_cost = 0
        split_at = len(messages)
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            cost = self.estimate_messages([message])
            if recent_reversed and (recent_cost + cost > recent_budget or len(recent_reversed) >= max_recent_messages):
                break
            recent_reversed.append(message)
            recent_cost += cost
            split_at = index

        recent = list(reversed(recent_reversed))
        older = messages[:split_at]
        if not recent:
            recent = [messages[-1]]
            older = messages[:-1]

        # 单条当前请求也可能大于剩余预算；这种情况下明确标记截断位置。
        if self.estimate_messages(recent) > conversation_budget:
            last = dict(recent[-1])
            content_budget = max(1, conversation_budget - self.estimate_tokens(last.get("role", "")) - 8)
            last["content"] = self.fit_text(last.get("content", ""), content_budget)
            recent = [last]
            older = messages[: max(0, len(messages) - 1)]

        summary_prefix = "较早会话摘要（仅作上下文；以用户原始消息和系统策略为准）：\n"
        summary_header_cost = self.estimate_tokens("system" + summary_prefix) + 4
        summary_budget = max(
            1,
            conversation_budget - self.estimate_messages(recent) - summary_header_cost - 4,
        )
        summary = None
        used_llm_summary = False
        if older and summarizer is not None:
            try:
                summary = summarizer(older, summary_budget)
                used_llm_summary = bool(summary)
            except Exception:
                summary = None

        if older and not summary:
            summary = self._extractive_fallback(older, summary_budget)

        compacted = ([{
            "role": "system",
            "content": summary_prefix + str(summary),
        }] if summary else []) + recent

        # 最终预算闸门，防止摘要器返回超长内容。
        if summary and self.estimate_messages(fixed + compacted) > prompt_budget:
            summary_message = dict(compacted[0])
            remaining = max(
                1,
                prompt_budget - fixed_cost - self.estimate_messages(recent)
                - self.estimate_tokens("system" + summary_prefix) - 4,
            )
            summary_message["content"] = summary_prefix + self.fit_text(str(summary), remaining)
            compacted[0] = summary_message

        after = fixed_cost + self.estimate_messages(compacted)
        return compacted, {
            "before": before,
            "after": after,
            "input_budget": prompt_budget,
            "summarized_messages": len(older),
            "retained_messages": len(recent),
            "strategy": "llm_summary" if used_llm_summary else "extractive_fallback",
            "token_counter": self.counter_name,
        }

    def _extractive_fallback(self, messages: list[Message], token_budget: int) -> str:
        """模型摘要不可用时，按时间保留较新的旧消息摘录。"""
        lines: list[str] = []
        remaining = token_budget
        for message in reversed(messages):
            role = message.get("role", "unknown")
            content = message.get("content", "")
            prefix = f"{role}: "
            available = remaining - self.estimate_tokens(prefix) - 4
            if available <= 0:
                break
            snippet = self.fit_text(content, available)
            line = prefix + snippet
            cost = self.estimate_tokens(line) + 4
            if cost > remaining:
                break
            lines.append(line)
            remaining -= cost
        lines.reverse()
        return "\n".join(lines) or "较早消息过长，未能生成摘要；请参考当前会话记录。"
