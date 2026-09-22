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
    """Calls an OpenAI-compatible /chat/completions endpoint with strict JSON output."""

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
    """Uses a configured LLM first, then an offline deterministic fallback."""

    def __init__(self) -> None:
        self.llm = OpenAICompatibleSqlWriter() if settings.llm_enabled else None
        self.fallback = RuleBasedSqlWriter()
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
