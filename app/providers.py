"""模型供应商适配层：优先本地 DeepSeek，失败时回退规则 SQL 生成器。"""

from __future__ import annotations

import json
import re
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.config import settings
from app.models import CandidateTable, GeneratedSql
from app.sql_agent import RuleBasedSqlWriter


class OpenAICompatibleSqlWriter:
    """调用 OpenAI 兼容的 Chat Completions 接口生成结构化 SQL。

    模型只负责提出候选 SQL；表白名单、只读约束、LIMIT、风险分级和审批都在本地后端完成，
    因此模型不可用或返回不合法 JSON 时可以安全回退。
    """

    def generate(self, question: str, tables: list[CandidateTable], tool_context: list[dict] | None = None) -> GeneratedSql | None:
        # 让模型只输出结构化 JSON，后续仍必须经过 Reviewer 和 RiskAssessor。
        schema = [{"table": item.name, "columns": item.columns} for item in tables]
        prompt = (
            "你是受控 SQL 规划器。只根据给定表生成单条 SQL；若问题属于 SOP/解释类或信息不足，返回 null。"
            "绝不编造表字段。输出 JSON：{\"sql\": string|null, \"intent\": string, \"confidence\": 0-1, \"tables\": [string]}。"
            f"\n可用 schema: {json.dumps(schema, ensure_ascii=False)}"
            f"\n已执行的只读工具证据（仅供理解问题，不能据此编造字段）: {json.dumps(tool_context or [], ensure_ascii=False)}"
            f"\n用户问题: {question}"
        )
        payload = json.dumps(
            {
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{settings.llm_base_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.llm_api_key}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=settings.llm_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            raw = body["choices"][0]["message"]["content"]
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            result = json.loads(raw)
            if not result.get("sql"):
                return None
            return GeneratedSql(
                sql=str(result["sql"]), intent=str(result.get("intent", "llm_sql")),
                confidence=float(result.get("confidence", 0.0)), tables=list(result.get("tables", [])),
            )
        except (KeyError, TypeError, ValueError, URLError, TimeoutError):
            return None


class ResilientSqlWriter:
    """可恢复的 SQL Writer 门面。

    如果配置了可用的 LLM，先尝试模型生成；网络、超时、解析或模型拒答时使用确定性规则生成器，
    保证本地演示仍能运行，同时通过 last_provider 暴露本次实际使用的提供方。
    """

    def __init__(self) -> None:
        # 可选模型适配器；未配置完整连接信息时保持 None。
        self.llm = OpenAICompatibleSqlWriter() if settings.llm_enabled else None
        # 离线规则生成器，保证无模型时仍能覆盖核心演示问题。
        self.fallback = RuleBasedSqlWriter()
        # 最近一次 generate 使用的提供方，供调试和工作流轨迹展示。
        self.last_provider = "rule_based"

    def generate(self, question: str, tables: list[CandidateTable], tool_context: list[dict] | None = None) -> GeneratedSql | None:
        # 模型不可用时保留可运行的离线演示能力，同时记录实际 provider。
        if self.llm:
            generated = self.llm.generate(question, tables, tool_context)
            if generated:
                self.last_provider = "openai_compatible"
                return generated
        self.last_provider = "rule_based"
        return self.fallback.generate(question, tables, tool_context)
