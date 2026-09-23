"""模型供应商适配层：优先本地 LLM，失败时回退规则 SQL 生成器。

本模块是 SQL Agent 的 SQL 生成适配层，核心设计目标：
1. 优先调用 OpenAI 兼容的本地模型生成候选 SQL；
2. 模型不可用、超时、返回非法 JSON 或拒答时，自动回退到确定性规则生成器；
3. 通过 last_provider 暴露本次实际使用的提供方，便于调试和工作流轨迹展示；
4. 明确安全边界：模型只负责提出候选 SQL，后续仍必须经过
   表白名单、只读约束、LIMIT、风险分级和审批等本地后端流程；
5. 模型输出强制为结构化 JSON，避免解析歧义，同时降低模型越权空间。

安全边界：
- 不信任模型返回的任何表名、字段名或 SQL，后端会再次校验；
- 提示词明确要求模型“绝不编造表字段”；
- 模型拒答（sql=null）时返回 None，由上层决定是否走 RAG 或提示用户；
- 回退到规则生成器保证本地演示在无模型时仍可运行。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

import json  # 构造请求 payload 和解析模型返回的 JSON
import re  # 用于剥离模型返回中的 Markdown 代码块围栏
from urllib.error import URLError  # 捕获网络层错误
from urllib.request import Request, urlopen  # 使用标准库 HTTP 客户端，避免额外依赖

from app.config import settings  # 读取 llm_base_url、llm_api_key、llm_model、超时等配置
from app.models import CandidateTable, GeneratedSql  # 输入候选表和输出结构化 SQL 的数据模型
from app.sql_agent import RuleBasedSqlWriter  # 确定性规则生成器，作为离线兜底


class OpenAICompatibleSqlWriter:
    """调用 OpenAI 兼容的 Chat Completions 接口生成结构化 SQL。

    模型只负责提出候选 SQL；表白名单、只读约束、LIMIT、风险分级和审批都在本地后端完成，
    因此模型不可用或返回不合法 JSON 时可以安全回退。

    使用方式：
        writer = OpenAICompatibleSqlWriter()
        result = writer.generate(question, tables, tool_context)

    说明：
    - 只处理 /chat/completions 的 OpenAI 兼容协议；
    - 提示词要求模型输出 JSON 对象，并用 response_format 强化；
    - 解析失败或模型返回 sql=null 时统一返回 None，由上层回退或改走 RAG。
    """

    def generate(
        self,
        question: str,
        tables: list[CandidateTable],
        tool_context: list[dict] | None = None,
    ) -> GeneratedSql | None:
        """调用模型生成候选 SQL。

        参数：
        - question：用户自然语言问题；
        - tables：schema 召回阶段给出的候选表及其列；
        - tool_context：已执行的只读工具证据，仅供模型理解问题，
          不能被用来编造字段。

        返回：
        - GeneratedSql：模型返回了合法 SQL 时；
        - None：模型拒答、返回空 sql、JSON 解析失败、网络异常等任何不可用情况。

        安全边界：
        - 只把候选表的 name 和 columns 暴露给模型，不暴露真实数据；
        - 提示词明确禁止编造字段；
        - 模型返回内容仍需经过后端 Reviewer 和 RiskAssessor。
        """
        # 让模型只输出结构化 JSON，后续仍必须经过 Reviewer 和 RiskAssessor。
        # 构造精简 schema：只传表名和列名，减少 prompt 长度和敏感信息暴露。
        schema = [{"table": item.name, "columns": item.columns} for item in tables]

        # 构造提示词：
        # - 明确角色是“受控 SQL 规划器”；
        # - 要求只根据给定表生成单条 SQL；
        # - SOP/解释类或信息不足时返回 null；
        # - 强制 JSON 输出格式，便于稳定解析；
        # - 附上 schema 和只读工具证据，最后是用户问题。
        prompt = (
            "你是受控 SQL 规划器。只根据给定表生成单条 SQL；若问题属于 SOP/解释类或信息不足，返回 null。"
            "绝不编造表字段。输出 JSON：{\"sql\": string|null, \"intent\": string, \"confidence\": 0-1, \"tables\": [string]}。"
            f"\n可用 schema: {json.dumps(schema, ensure_ascii=False)}"
            f"\n已执行的只读工具证据（仅供理解问题，不能据此编造字段）: {json.dumps(tool_context or [], ensure_ascii=False)}"
            f"\n用户问题: {question}"
        )

        # 构造请求 payload：
        # - temperature=0 降低随机性，保证同类问题输出稳定；
        # - response_format=json_object 让服务端尽量只返回 JSON；
        # - ensure_ascii=False 保留中文，便于本地模型理解。
        payload = json.dumps(
            {
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")

        # 构造标准库 POST 请求，指向 OpenAI 兼容的 chat/completions。
        # 使用 Bearer 认证，密钥来自配置，不会记录到审计。
        request = Request(
            f"{settings.llm_base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.llm_api_key}",
            },
            method="POST",
        )

        try:
            # 发起请求并读取完整响应体；超时来自配置。
            with urlopen(request, timeout=settings.llm_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))

            # 按 OpenAI 兼容格式取出 choices[0].message.content。
            raw = body["choices"][0]["message"]["content"]

            # 剥离模型可能包裹的 Markdown 代码块围栏（```json ... ```）。
            # 这是兼容不同模型行为的轻量清洗，避免因格式差异解析失败。
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

            # 解析 JSON；如果模型输出非法 JSON 会抛 ValueError，被下方统一捕获。
            result = json.loads(raw)

            # 模型明确拒答（sql 为空或 null）时返回 None，由上层回退或改走 RAG。
            if not result.get("sql"):
                return None

            # 构造结构化结果；缺失字段使用安全默认值。
            return GeneratedSql(
                sql=str(result["sql"]),
                intent=str(result.get("intent", "llm_sql")),
                confidence=float(result.get("confidence", 0.0)),
                tables=list(result.get("tables", [])),
            )
        except (KeyError, TypeError, ValueError, URLError, TimeoutError):
            # 任意不可用情况统一返回 None，避免上层感知具体异常类型。
            # 包括：响应结构缺失、JSON 解析失败、网络异常、超时等。
            return None


class ResilientSqlWriter:
    """可恢复的 SQL Writer 门面。

    如果配置了可用的 LLM，先尝试模型生成；网络、超时、解析或模型拒答时使用确定性规则生成器，
    保证本地演示仍能运行，同时通过 last_provider 暴露本次实际使用的提供方。

    使用方式：
        writer = ResilientSqlWriter()
        result = writer.generate(question, tables, tool_context)
        provider = writer.last_provider  # "openai_compatible" 或 "rule_based"

    设计说明：
    - 组合模式：LLM 适配器和规则生成器都是可替换的组件；
    - 只读 last_provider 属性，便于工作流轨迹展示和审计；
    - 不在本类中做 SQL 校验，校验由后端 Reviewer 和 RiskAssessor 负责。
    """

    def __init__(self) -> None:
        # 可选模型适配器；未配置完整连接信息时保持 None。
        # settings.llm_enabled 要求 base_url、api_key、model 都非空。
        self.llm = OpenAICompatibleSqlWriter() if settings.llm_enabled else None

        # 离线规则生成器，保证无模型时仍能覆盖核心演示问题。
        self.fallback = RuleBasedSqlWriter()

        # 最近一次 generate 使用的提供方，供调试和工作流轨迹展示。
        # 默认值为 "rule_based"，因为未调用前无法确定实际路径。
        self.last_provider = "rule_based"

    def generate(
        self,
        question: str,
        tables: list[CandidateTable],
        tool_context: list[dict] | None = None,
    ) -> GeneratedSql | None:
        """优先调用模型生成 SQL，失败时回退到规则生成器。

        参数：
        - question：用户自然语言问题；
        - tables：候选表及其列；
        - tool_context：已执行的只读工具证据，透传给模型作为理解上下文。

        返回：
        - GeneratedSql：模型或规则生成器返回的结果；
        - None：两者都无法生成时（例如问题属于 SOP 类）。

        副作用：
        - 更新 self.last_provider，标记本次实际使用的提供方。

        安全边界：
        - 模型不可用时保留可运行的离线演示能力；
        - 不因模型异常而向上抛出，避免影响主流程稳定性。
        """
        # 明确写操作由本地安全规则优先识别，不能让模型将变更请求改写成可自动执行的 SELECT。
        guarded = self.fallback.generate(question, tables, tool_context)
        if guarded and guarded.intent == "write_request":
            self.last_provider = "safety_guard"
            return guarded

        # 模型不可用时保留可运行的离线演示能力，同时记录实际 provider。
        if self.llm:
            generated = self.llm.generate(question, tables, tool_context)
            if generated:
                # 模型成功返回合法 SQL：记录 provider 并直接返回。
                self.last_provider = "openai_compatible"
                return generated

        # 模型未配置、拒答或异常：回退到确定性规则生成器。
        self.last_provider = "rule_based"
        return self.fallback.generate(question, tables, tool_context)
