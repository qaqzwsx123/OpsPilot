from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings


SYSTEM_PROMPT = """你是 OpsPilot 的 Agent 聊天助手，服务于运维团队。请使用中文自然、简洁地对话。
你可以解释系统能力、帮助梳理排障思路、总结用户提供的信息，并建议何时使用智能查询、Skills、审批和审计页面。
严禁声称已经执行 SQL、调用工具、修改数据库、关闭告警或创建工单；这些动作必须由对应的受控业务流程完成。
当用户询问业务数据时，建议其使用“智能查询”或相应 Skill，并给出可直接使用的提问方式。"""


class AgentChatService:
    """OpenAI-compatible local chat adapter with an honest offline fallback."""

    def reply(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        if not settings.chat_enabled:
            return self._fallback("未检测到本地模型配置。请检查 .env 中的 MODEL_BASE_URL 和 MODEL_NAME。"), "offline"
        payload = {
            "model": settings.chat_model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages[-16:]],
            "temperature": 0.3,
        }
        headers = {"Content-Type": "application/json"}
        if settings.chat_api_key:
            headers["Authorization"] = f"Bearer {settings.chat_api_key}"
        request = Request(
            f"{settings.chat_base_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urlopen(request, timeout=settings.chat_timeout_seconds) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not content:
                raise RuntimeError("模型返回中没有 message.content")
            return str(content), "local_deepseek"
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:180]
            return self._fallback(f"本地模型返回 HTTP {exc.code}：{detail}"), "fallback"
        except (URLError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
            return self._fallback(f"本地模型暂时不可用：{exc}"), "fallback"

    @staticmethod
    def _fallback(reason: str) -> str:
        return f"{reason}\n\n聊天历史已保存。你仍可以使用智能查询、Skills、审批和审计功能；模型恢复后可继续在该会话中对话。"
