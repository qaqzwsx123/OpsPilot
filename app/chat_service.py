"""Agent 聊天服务：连接本地 OpenAI 兼容模型并提供离线兜底。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings


# 聊天模型的安全边界：聊天只负责解释和规划，不直接执行业务变更。
SYSTEM_PROMPT = """你是 OpsPilot 的 Agent 聊天助手，服务于运维团队。请使用中文自然、简洁地对话。
你可以解释系统能力、帮助梳理排障思路、总结用户提供的信息，并建议何时使用智能查询、Skills、审批和审计页面。
严禁声称已经执行 SQL、调用工具、修改数据库、关闭告警或创建工单；这些动作必须由对应的受控业务流程完成。
当用户询问业务数据时，建议其使用“智能查询”或相应 Skill，并给出可直接使用的提问方式。"""


class AgentChatService:
    """Agent 聊天适配器。

    负责会话消息到本地 OpenAI 兼容 DeepSeek 的请求转换、流式 SSE 增量解析和离线兜底。
    聊天服务只解释问题、维护多轮上下文和给出流程建议，不直接调用写操作工具。
    """

    def reply(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        # 非流式接口主要用于兼容简单调用；正式聊天页面使用 stream_reply。
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
    def plan_turn(content: str, history: list[dict[str, str]]) -> dict[str, Any]:
        """Create an explicit, explainable plan before each conversational turn."""
        text = content.lower()
        if any(word in text for word in ("删除", "修改", "关闭告警", "创建工单", "执行sql", "写库")):
            route = "受控变更建议"
            steps = ["理解目标与对象", "检查风险和影响范围", "需要变更时转审批中心", "由受控流程执行并写入审计"]
        elif any(word in text for word in ("告警", "设备", "资产", "工单", "查询", "指标")):
            route = "智能查询建议"
            steps = ["理解问题与筛选条件", "建议调用只读工具或智能查询", "返回结果并解释关键字段", "保留查询轨迹供审计"]
        elif any(word in text for word in ("sop", "排障", "规范", "怎么处理", "故障")):
            route = "知识库检索建议"
            steps = ["识别故障主题", "检索有效 SOP 和历史证据", "整理处置步骤与升级条件", "标注需要人工确认的动作"]
        else:
            route = "多轮对话规划"
            steps = ["加载当前会话上下文", "理解本轮目标", "结合历史消息补全指代", "给出下一步可执行建议"]
        return {"route": route, "steps": steps, "context_messages": min(len(history), 16)}

    def stream_reply(self, messages: list[dict[str, str]], plan: dict[str, Any]) -> Iterator[dict[str, str]]:
        """Stream OpenAI-compatible deltas and keep an honest fallback path."""
        # 通过 SSE 逐段转发模型增量，网络或模型异常时逐段发送可解释的兜底消息。
        context_instruction = (
            "当前任务规划：" + str(plan["route"]) + "；步骤：" + " → ".join(plan["steps"]) +
            "。请遵守安全边界，不要声称已经执行受控操作。"
        )
        if not settings.chat_enabled:
            fallback = self._fallback("未检测到本地模型配置。请检查 .env 中的 MODEL_BASE_URL 和 MODEL_NAME。")
            for part in self._split_stream(fallback):
                yield {"type": "token", "content": part, "provider": "offline"}
            return
        payload = {
            "model": settings.chat_model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "system", "content": context_instruction}, *messages[-16:]],
            "temperature": 0.3,
            "stream": True,
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
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_line = line[5:].strip()
                    if data_line == "[DONE]":
                        break
                    try:
                        data = json.loads(data_line)
                    except json.JSONDecodeError:
                        continue
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield {"type": "token", "content": str(delta), "provider": "local_deepseek"}
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
            message = self._fallback(f"本地模型暂时不可用：{exc}")
            for part in self._split_stream(message):
                yield {"type": "token", "content": part, "provider": "fallback"}

    @staticmethod
    def _split_stream(text: str) -> Iterator[str]:
        for index in range(0, len(text), 18):
            yield text[index:index + 18]

    @staticmethod
    def _fallback(reason: str) -> str:
        return f"{reason}\n\n聊天历史已保存。你仍可以使用智能查询、Skills、审批和审计功能；模型恢复后可继续在该会话中对话。"
