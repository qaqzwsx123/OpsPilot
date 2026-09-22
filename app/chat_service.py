"""Agent 聊天服务：连接本地 OpenAI 兼容模型并提供离线兜底。

该模块是 OpsPilot 的聊天适配层，负责：
1. 将多轮聊天消息转换为 OpenAI 兼容的 /chat/completions 请求；
2. 支持普通响应和 SSE 流式响应；
3. 在本地模型不可用时返回离线兜底文案；
4. 通过系统提示词和任务规划约束模型，确保聊天不越权执行写操作。
"""
from __future__ import annotations  # 延迟解析类型注解，避免运行时求值，提升兼容性
import json  # 构造和解析 OpenAI 兼容接口的 JSON 请求与响应
from collections.abc import Iterator  # 标注流式生成器的返回类型
from typing import Any  # 标注较宽松的字典结构，便于适配不同模型返回
from urllib.error import HTTPError, URLError  # 捕获 HTTP 错误和网络错误
from urllib.request import Request, urlopen  # 使用标准库 HTTP 客户端，避免额外依赖
from app.config import settings  # 项目配置：模型地址、密钥、超时、开关等


# 聊天模型的安全边界：聊天只负责解释和规划，不直接执行业务变更。
# 该提示词会作为第一条 system 消息发送给模型，用于约束模型行为。
SYSTEM_PROMPT = """你是 OpsPilot 的 Agent 聊天助手，服务于运维团队。请使用中文自然、简洁地对话。
你可以解释系统能力、帮助梳理排障思路、总结用户提供的信息，并建议何时使用智能查询、Skills、审批和审计页面。
严禁声称已经执行 SQL、调用工具、修改数据库、关闭告警或创建工单；这些动作必须由对应的受控业务流程完成。
当用户询问业务数据时，建议其使用“智能查询”或相应 Skill，并给出可直接使用的提问方式。"""


class AgentChatService:
    """Agent 聊天适配器。

    负责会话消息到本地 OpenAI 兼容 DeepSeek 的请求转换、流式 SSE 增量解析和离线兜底。
    聊天服务只解释问题、维护多轮上下文和给出流程建议，不直接调用写操作工具。

    主要方法：
    - reply：非流式响应，兼容简单调用；
    - plan_turn：在每轮对话前生成可解释的任务规划；
    - stream_reply：流式响应，正式聊天页面使用；
    - _split_stream：把兜底文案切分成小块，模拟流式输出；
    - _fallback：生成统一的离线/异常兜底文案。
    """

    def reply(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        """非流式聊天接口。

        参数：
        - messages：OpenAI 兼容格式的多轮消息列表，通常每项包含 role 和 content。

        返回：
        - tuple[str, str]：第一个元素是回复内容，第二个元素是提供方标识。
          provider 可能为：
          * offline：未启用本地模型；
          * local_deepseek：本地模型正常返回；
          * fallback：本地模型异常，返回兜底文案。
        """
        # 非流式接口主要用于兼容简单调用；正式聊天页面使用 stream_reply。
        # 如果配置中关闭了聊天能力，则直接返回离线兜底，避免发起网络请求。
        if not settings.chat_enabled:
            return self._fallback("未检测到本地模型配置。请检查 .env 中的 MODEL_BASE_URL 和 MODEL_NAME。"), "offline"

        # 构造 OpenAI 兼容的 /chat/completions 请求体。
        payload = {
            "model": settings.chat_model,  # 本地模型名称，例如 deepseek-chat
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},  # 安全边界与角色约束
                *messages[-16:],  # 只保留最近 16 条历史，防止上下文过长
            ],
            "temperature": 0.3,  # 降低随机性，适合运维解释、规划和排障建议
        }

        # 请求头默认是 JSON；如果配置了 API Key，则追加 Bearer 认证。
        headers = {"Content-Type": "application/json"}
        if settings.chat_api_key:
            headers["Authorization"] = f"Bearer {settings.chat_api_key}"

        # 构造标准库 POST 请求，指向本地 OpenAI 兼容服务的 chat/completions。
        request = Request(
            f"{settings.chat_base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            # 发起请求并读取完整响应体；timeout 来自配置，避免长时间阻塞。
            with urlopen(request, timeout=settings.chat_timeout_seconds) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))

            # 按 OpenAI 兼容格式读取 choices[0].message.content。
            content = data.get("choices", [{}])[0].get("message", {}).get("content")

            # 如果模型返回空内容，视为异常，交由下方统一兜底。
            if not content:
                raise RuntimeError("模型返回中没有 message.content")

            return str(content), "local_deepseek"

        except HTTPError as exc:
            # HTTP 错误：例如 401、404、500 等。读取错误正文并截断，避免返回过长内容。
            detail = exc.read().decode("utf-8", errors="replace")[:180]
            return self._fallback(f"本地模型返回 HTTP {exc.code}：{detail}"), "fallback"

        except (URLError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
            # 其他异常：网络不可达、超时、JSON 解析失败、结构缺失、空内容等。
            return self._fallback(f"本地模型暂时不可用：{exc}"), "fallback"

    @staticmethod
    def plan_turn(content: str, history: list[dict[str, str]]) -> dict[str, Any]:
        """Create an explicit, explainable plan before each conversational turn.

        在每轮对话前生成一个显式、可解释的任务规划，用于：
        - 告诉模型本轮应该走哪条路线；
        - 把规划步骤注入 system 消息，约束模型回答方向；
        - 统计实际使用的上下文消息数量，便于审计和调试。

        参数：
        - content：本轮用户输入文本；
        - history：当前会话的历史消息列表。

        返回：
        - dict：包含 route、steps、context_messages 三个字段。
        """
        # 将本轮输入转成小写，便于进行简单、稳定的关键词匹配。
        text = content.lower()

        # 根据关键词做粗粒度路由。这里不是意图识别的最终方案，而是可解释的规则兜底。
        if any(word in text for word in ("删除", "修改", "关闭告警", "创建工单", "执行sql", "写库")):
            # 涉及写操作、变更或受控动作时，必须引导到审批和审计流程。
            route = "受控变更建议"
            steps = [
                "理解目标与对象",
                "检查风险和影响范围",
                "需要变更时转审批中心",
                "由受控流程执行并写入审计",
            ]
        elif any(word in text for word in ("告警", "设备", "资产", "工单", "查询", "指标")):
            # 涉及运维数据查询时，建议使用只读工具或智能查询，不直接写库。
            route = "智能查询建议"
            steps = [
                "理解问题与筛选条件",
                "建议调用只读工具或智能查询",
                "返回结果并解释关键字段",
                "保留查询轨迹供审计",
            ]
        elif any(word in text for word in ("sop", "排障", "规范", "怎么处理", "故障")):
            # 涉及排障、SOP、规范时，优先走知识库检索和人工确认。
            route = "知识库检索建议"
            steps = [
                "识别故障主题",
                "检索有效 SOP 和历史证据",
                "整理处置步骤与升级条件",
                "标注需要人工确认的动作",
            ]
        else:
            # 其他情况作为普通多轮对话处理，重点是上下文补全和下一步建议。
            route = "多轮对话规划"
            steps = [
                "加载当前会话上下文",
                "理解本轮目标",
                "结合历史消息补全指代",
                "给出下一步可执行建议",
            ]
        # context_messages 与 reply/stream_reply 中 messages[-16:] 的窗口保持一致。
        return {
            "route": route,
            "steps": steps,
            "context_messages": min(len(history), 16),
        }

    def stream_reply(self, messages: list[dict[str, str]], plan: dict[str, Any]) -> Iterator[dict[str, str]]:
        """Stream OpenAI-compatible deltas and keep an honest fallback path.
        流式聊天接口。通过 SSE 逐段转发模型增量，网络或模型异常时逐段发送可解释的兜底消息。
        参数：
        - messages：OpenAI 兼容格式的多轮消息列表；
        - plan：由 plan_turn 生成的任务规划。
        生成：
        - dict：每个事件形如 {"type": "token", "content": "...", "provider": "..."}。
          provider 可能为 offline、local_deepseek、fallback。
        """
        # 通过 SSE 逐段转发模型增量，网络或模型异常时逐段发送可解释的兜底消息。
        # 将本轮任务规划注入第二条 system 消息，让模型知道路线和步骤。
        context_instruction = (
            "当前任务规划：" + str(plan["route"]) + "；步骤：" + " → ".join(plan["steps"]) +
            "。请遵守安全边界，不要声称已经执行受控操作。"
        )
        # 如果聊天能力未启用，则走离线兜底：按小块输出，保持流式体验。
        if not settings.chat_enabled:
            fallback = self._fallback("未检测到本地模型配置。请检查 .env 中的 MODEL_BASE_URL 和 MODEL_NAME。")
            for part in self._split_stream(fallback):
                yield {"type": "token", "content": part, "provider": "offline"}
            return
        # 构造流式请求体。stream=True 表示要求服务端返回 SSE 增量。
        payload = {
            "model": settings.chat_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},  # 安全边界
                {"role": "system", "content": context_instruction},  # 本轮规划
                *messages[-16:],  # 最近 16 条上下文，与 plan_turn 统计一致
            ],
            "temperature": 0.3,  # 保持较低随机性
            "stream": True,  # 开启流式返回
        }
        # 请求头默认 JSON；有 API Key 时追加认证头。
        headers = {"Content-Type": "application/json"}
        if settings.chat_api_key:
            headers["Authorization"] = f"Bearer {settings.chat_api_key}"
        # 构造标准库 POST 请求。
        request = Request(
            f"{settings.chat_base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            # 打开流式响应，逐行读取 SSE 数据。
            with urlopen(request, timeout=settings.chat_timeout_seconds) as response:
                for raw_line in response:
                    # SSE 每行通常是 bytes，需要解码；strip 去掉首尾空白和换行。
                    line = raw_line.decode("utf-8", errors="replace").strip()

                    # 只处理以 "data:" 开头的 SSE 数据行；忽略空行、注释行等。
                    if not line.startswith("data:"):
                        continue

                    # 去掉前缀 "data:"，得到真正的数据内容。
                    data_line = line[5:].strip()

                    # OpenAI 兼容流式协议用 [DONE] 表示结束。
                    if data_line == "[DONE]":
                        break

                    # 尝试解析 JSON；某些服务可能发送心跳或非 JSON 数据，解析失败则跳过。
                    try:
                        data = json.loads(data_line)
                    except json.JSONDecodeError:
                        continue

                    # 解析增量内容：choices[0].delta.content。
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content")

                    # 只有存在实际增量文本时才向前端发送 token。
                    if delta:
                        yield {"type": "token", "content": str(delta), "provider": "local_deepseek"}

        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
            # 流式过程中出现异常：生成兜底文案，并按小块继续以 token 形式输出。
            message = self._fallback(f"本地模型暂时不可用：{exc}")
            for part in self._split_stream(message):
                yield {"type": "token", "content": part, "provider": "fallback"}

    @staticmethod
    def _split_stream(text: str) -> Iterator[str]:
        """把兜底文本切分成固定大小的小块，用于模拟流式输出。
        参数：
        - text：需要输出的完整兜底文本。
        生成：
        - str：每次生成 18 个字符的小块。
        """
        # 按固定长度切分，避免一次性返回大段文本。
        # 这里不保证按语义切分，仅用于离线/异常时的流式体验兜底。
        for index in range(0, len(text), 18):
            yield text[index:index + 18]

    @staticmethod
    def _fallback(reason: str) -> str:
        """生成统一的离线/异常兜底文案。
        参数：
        - reason：模型不可用或配置缺失的原因说明。
        返回：
        - str：面向用户的兜底提示，说明当前状态和仍可使用的功能。
        """
        return (
            f"{reason}\n\n"
            "聊天历史已保存。你仍可以使用智能查询、Skills、审批和审计功能；"
            "模型恢复后可继续在该会话中对话。"
        )