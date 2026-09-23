"""模型优先、规则兜底的只读工具编排器。

本模块是 OpsPilot 的工具规划层，负责在 SQL Agent 工作流中决定“调用哪些只读工具”。

核心设计目标：
1. 优先让本地模型通过 OpenAI Function Calling 选择只读工具；
2. 模型不可用、返回非法调用或未选中任何工具时，回退到规则白名单；
3. 无论模型如何选择，最终都必须经过工具存在性、风险等级、重复调用和最大次数校验；
4. 只允许只读工具（risk="auto"），manual/blocked 工具不在规划范围内；
5. 第一轮默认只允许一次工具调用；只有工具返回 needs_followup=true 时才进入第二轮；
6. 所有选择理由和证据都保留在工作流轨迹中，便于审计和调试。

安全边界：
- 模型返回的工具名可能非法，执行前必须经过 definition() 白名单校验；
- 模型不能选择 manual 或 blocked 风险的工具；
- 重复调用同一工具会被拒绝（used 集合）；
- 总调用次数受 max_tools 限制；
- 只读工具执行失败不阻塞 SQL/RAG 工作流，只记录失败证据。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

import json  # 构造请求 payload 和解析模型返回的 JSON
from urllib.error import HTTPError, URLError  # 捕获网络层错误
from urllib.request import Request, urlopen  # 使用标准库 HTTP 客户端，避免额外依赖
from dataclasses import dataclass  # 声明不可变的数据结构
from typing import Any  # 宽松字典类型标注

from app.config import settings  # 读取 chat_base_url、chat_model、超时等配置
from app.context import ContextCompressor  # 为工具规划请求控制证据 Token 预算
from app.tool_registry import catalog, definition, invoke  # 工具定义查询和调用入口


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """规划器最终接受的一次工具选择。

    设计说明：
    - frozen=True：选择创建后不可修改，保证工作流轨迹稳定；
    - slots=True：减少内存占用并防止动态属性；
    - 与 ModelToolCall 区分：ToolSelection 是已通过白名单校验的选择，
      ModelToolCall 是模型提出的候选，可能非法。

    字段：
    - name：被白名单接受的工具名；
    - reason：模型或规则解释为什么需要这个工具，展示在工作流轨迹中。
    """

    # 被白名单接受的工具名。
    name: str

    # 模型或规则解释为什么需要这个工具，展示在工作流轨迹中。
    reason: str


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """OpenAI Function Calling 返回的候选调用，在执行前还要过白名单校验。

    设计说明：
    - 与 ToolSelection 区分：本类是模型原始提议，未经过白名单校验；
    - 工具名可能不存在、风险等级不合法或参数缺失，不能直接执行；
    - 保留 arguments 和 reason 便于审计和调试。

    字段：
    - name：模型请求调用的工具名，可能是不合法值，不能直接执行；
    - arguments：模型生成的 JSON 参数，如 query 或筛选条件；
    - reason：模型给出的调用理由。
    """

    # 模型请求调用的工具名，可能是不合法值，不能直接执行。
    name: str

    # 模型生成的 JSON 参数，如 query 或筛选条件。
    arguments: dict[str, Any]

    # 模型给出的调用理由。
    reason: str


class ToolPlanner:
    """模型优先、规则兜底的只读工具编排器。

    第一轮默认只允许一次工具调用；只有工具返回 ``needs_followup`` 时才进入第二轮。
    无论模型如何选择，最终都必须经过工具存在性、风险等级、重复调用和最大次数校验。

    使用方式：
        planner = ToolPlanner()
        selections, evidence, summary, mode = planner.execute_agent(question)
        # 或仅用规则：
        selections, evidence = planner.execute(question)

    两种执行路径：
    - execute：仅规则匹配，同步、确定性；
    - execute_agent：模型优先，规则兜底，支持多轮 followup。

    安全边界：
    - 只允许 risk="auto" 的只读工具；
    - 模型提议的工具名必须通过 definition() 校验；
    - 重复工具和超限调用会被拒绝；
    - 工具执行失败不影响主流程。
    """

    # 规则兜底目录：工具名、触发词、审计轨迹中的选择理由。
    # 每条规则： (工具名, 触发词元组, 选择理由)
    # 触发词匹配问题小写文本中的子串；命中任意一个即选中该工具。
    rules = (
        ("alert_query", ("告警", "报警", "p1", "p2", "p3"), "问题涉及告警级别、状态或影响范围"),
        ("asset_lookup", ("设备", "资产", "网关", "离线", "在线", "维护", "机器人"), "问题涉及设备资产或运行状态"),
        ("ticket_query", ("工单", "高优", "负责人", "优先级"), "问题涉及故障工单或负责人"),
        ("work_order_query", ("作业", "派单", "维修任务", "待执行"), "问题涉及待执行运维作业"),
        ("knowledge_search", ("sop", "如何", "怎么", "流程", "规范", "排障", "手册"), "问题需要 SOP、操作步骤或规范知识"),
        ("metric_catalog", ("指标", "cpu", "内存", "延迟", "趋势", "成功率"), "问题涉及指标定义或时序观测"),
        ("approval_queue", ("审批", "待批", "变更单"), "问题涉及待处理审批或变更状态"),
        ("audit_recent", ("审计", "留痕", "追溯", "历史操作"), "问题涉及操作追溯或审计记录"),
        ("system_health", ("系统状态", "服务状态", "健康检查", "健康"), "问题涉及 Agent 或数据接入健康状态"),
        ("asset_status_summary", ("设备状态统计", "资产状态分布", "按区域统计设备", "资产分布"), "问题需要汇总设备状态或区域分布"),
        ("asset_owner_summary", ("负责人资产统计", "各负责人设备", "每个负责人", "资产负责人分布"), "问题需要按负责人汇总设备资产"),
        ("alert_severity_summary", ("告警统计", "告警分布", "告警汇总", "各级告警数量"), "问题需要汇总告警级别和状态"),
        ("recent_p1_alerts", ("最近的p1", "最近p1告警", "最新p1告警", "一级告警"), "问题需要查看近期最高级别告警"),
        ("ticket_priority_summary", ("工单统计", "工单优先级分布", "工单数量汇总", "各级工单"), "问题需要汇总工单优先级和状态"),
        ("unassigned_tickets", ("未分配工单", "无人负责工单", "没有负责人的工单", "未指派工单"), "问题需要定位未分配的开放工单"),
        ("work_order_status_summary", ("作业统计", "作业状态分布", "任务状态统计", "各类作业数量"), "问题需要汇总运维作业状态和类型"),
        ("latest_metric_samples", ("最新指标值", "最新监控指标", "最新指标样本", "最近采集的指标"), "问题需要查看最近采集的时序指标值"),
    )

    def plan(self, question: str, max_tools: int = 3) -> list[ToolSelection]:
        """基于规则匹配问题，返回选中的工具列表。

        参数：
        - question：用户问题；
        - max_tools：最多选中的工具数量，默认 3。

        返回：
        - list[ToolSelection]：按 rules 顺序匹配到的工具选择。

        规则：
        - 问题转小写后进行子串匹配；
        - 只选择 risk="auto" 且存在的工具；
        - 达到 max_tools 时提前退出；
        - 不做模型调用，完全确定性。
        """
        # 问题转小写，统一匹配。
        normalized = question.lower()
        selected: list[ToolSelection] = []

        # 按 rules 顺序遍历，命中触发词即选中。
        for name, triggers, reason in self.rules:
            # 校验工具存在且为只读工具。
            tool = definition(name)
            if tool is None or tool.risk != "auto":
                continue

            # 命中任意触发词即选中。
            if any(trigger in normalized for trigger in triggers):
                selected.append(ToolSelection(name, reason))

            # 达到上限时提前退出。
            if len(selected) >= max_tools:
                break
        return selected

    def execute(self, question: str) -> tuple[list[ToolSelection], list[dict[str, Any]]]:
        """仅用规则路径执行工具，返回选择列表和证据列表。

        参数：
        - question：用户问题。

        返回：
        - tuple：
          * selections：选中的工具列表；
          * evidence：每个工具的执行证据（压缩后的结果）。

        安全边界：
        - 工具执行失败时记录失败证据，不向上抛出，避免影响主流程。
        """
        selections = self.plan(question)
        evidence: list[dict[str, Any]] = []

        for selection in selections:
            try:
                # 调用工具，传入问题作为 query。
                result = invoke(selection.name, query=question)
                evidence.append(self._compact(selection, result))
            except Exception as exc:  # a read-only tool failure must not stop the SQL/RAG workflow
                # 只读工具失败不阻塞主流程，只记录失败证据。
                evidence.append({
                    "tool": selection.name,
                    "reason": selection.reason,
                    "status": "failed",
                    "result_count": 0,
                    "sample": [],
                    "summary": f"工具执行失败：{type(exc).__name__}",
                })
        return selections, evidence

    def execute_agent(
        self,
        question: str,
        max_rounds: int = 2,
        max_tools: int = 2,
    ) -> tuple[list[ToolSelection], list[dict[str, Any]], str, str]:
        """Let the local model choose one tool first, then continue only on explicit follow-up evidence.

        模型优先、规则兜底的多轮工具编排入口。

        参数：
        - question：用户问题；
        - max_rounds：最大模型轮数，默认 2；
        - max_tools：最多工具调用总数，默认 2。

        返回：
        - tuple：
          * selections：已通过白名单校验的工具选择列表；
          * evidence：每个工具的执行证据（含 arguments、round、selection_mode）；
          * summary：证据摘要文本，供前端或工作流展示；
          * mode：本次实际使用的模式，可能为：
            - "model_function_calling"：模型成功选择；
            - "rule_based_allowlist_fallback"：模型失败或未选中任何工具，回退到规则。

        流程：
        1. 逐轮调用 _model_choose 让模型选择工具；
        2. 对每个模型提议做白名单校验（存在性、只读、未重复、未超限）；
        3. 执行选中工具并记录证据；
        4. 只有当工具返回 needs_followup=true 时才进入下一轮；
        5. 若最终没有任何选择，回退到规则路径；
        6. 生成摘要并返回。

        安全边界：
        - 模型返回的工具名必须通过 definition() 校验；
        - 只允许 risk="auto" 工具；
        - 重复工具和超限调用会被拒绝；
        - 模型返回 None（不可用或解析失败）时回退规则路径。
        """
        selections: list[ToolSelection] = []
        evidence: list[dict[str, Any]] = []
        used: set[str] = set()  # 已调用过的工具名，用于去重
        mode = "model_function_calling"

        # 逐轮尝试模型选择。
        for round_number in range(1, max_rounds + 1):
            # 让模型选择工具；返回 None 表示模型不可用或解析失败。
            calls = self._model_choose(question, evidence, used, max_calls=1)
            if calls is None:
                # 模型不可用：切换到规则兜底模式，跳出模型轮次。
                mode = "rule_based_allowlist_fallback"
                break

            accepted = 0  # 本轮接受的有效调用数
            for call in calls:
                tool = definition(call.name)

                # 白名单校验：工具存在、只读、未重复、未超限。
                if (
                    tool is None
                    or tool.risk != "auto"
                    or call.name in used
                    or len(selections) >= max_tools
                ):
                    continue

                # 标记已使用并计数。
                used.add(call.name)
                accepted += 1

                # 构造已校验的工具选择。
                selection = ToolSelection(
                    call.name,
                    call.reason or "模型根据问题和已有证据选择只读工具",
                )
                selections.append(selection)

                # 从 arguments 中取 query，缺省用原问题，截断到 500 字符。
                query = str(call.arguments.get("query") or question)[:500]

                try:
                    result = invoke(call.name, query=query, arguments=call.arguments)
                    compact = self._compact(selection, result)
                    # 附加模型调用的元信息，便于审计和前端展示。
                    compact.update({
                        "arguments": call.arguments,
                        "round": round_number,
                        "selection_mode": mode,
                    })
                    evidence.append(compact)
                except Exception as exc:
                    # 只读工具失败不阻塞主流程，记录失败证据。
                    evidence.append({
                        "tool": call.name,
                        "reason": selection.reason,
                        "arguments": call.arguments,
                        "round": round_number,
                        "selection_mode": mode,
                        "status": "failed",
                        "result_count": 0,
                        "sample": [],
                        "summary": f"工具执行失败：{type(exc).__name__}",
                    })

                # 每轮最多接受一个工具，接受后跳出内层循环。
                if accepted >= 1:
                    break

            # A second model round is intentionally opt-in. Read-only tools may
            # explicitly return needs_followup=true when the first result is not
            # sufficient to answer the question.
            # 第二轮是显式选择加入的：只有工具明确返回 needs_followup=true 才继续。
            if (
                accepted == 0
                or len(selections) >= max_tools
                or not any(item.get("needs_followup") for item in evidence[-accepted:])
            ):
                break

        # 模型路径完全没选中任何工具时，回退到规则路径。
        if not selections:
            selections, evidence = self.execute(question)
            mode = "rule_based_allowlist_fallback"

        # 生成摘要文本。
        summary = self._fallback_summary(evidence)
        return selections, evidence, summary, mode

    def _model_choose(
        self,
        question: str,
        evidence: list[dict[str, Any]],
        used: set[str],
        max_calls: int = 1,
    ) -> list[ModelToolCall] | None:
        """调用模型 Function Calling 让模型选择工具。

        参数：
        - question：用户问题；
        - evidence：已执行的工具证据，供模型判断是否还需要调用；
        - used：已调用过的工具名集合，避免重复；
        - max_calls：本轮最多允许的调用数，默认 1。

        返回：
        - list[ModelToolCall]：模型返回的候选调用列表（可能为空）；
        - None：模型不可用、请求失败或解析失败，触发规则兜底。

        安全边界：
        - 只把 risk="auto" 且未使用过的工具作为候选传给模型；
        - 工具描述来自 definition()，不暴露内部实现；
        - 模型返回的调用仍需在 execute_agent 中做白名单校验。
        """
        # 聊天未启用时直接返回 None，触发规则兜底。
        if not settings.chat_enabled:
            return None

        # 构造可用工具列表：存在、只读、未使用过。
        available = [
            tool for tool in catalog()
            if tool["risk"] == "auto" and tool["name"] not in used
        ]

        # 构造 OpenAI Function Calling 的 tools 规范。
        # 内置工具使用 query 参数；动态只读工具使用注册时生成的筛选字段 schema。
        tool_specs = []
        for tool in available:
            parameters = json.loads(json.dumps(tool.get("parameters") or {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "传给工具的业务问题或筛选条件"},
                },
                "required": ["query"],
                "additionalProperties": False,
            }))
            parameters.setdefault("properties", {})["reason"] = {
                "type": "string", "description": "为什么需要调用这个工具",
            }
            tool_specs.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": parameters,
                },
            })

        # 工具结果先做结构化样本裁剪，再按工具请求预算逐步减少旧证据。
        tool_input_budget = max(
            256,
            min(
                settings.tool_context_budget_tokens,
                settings.chat_context_window_tokens - settings.chat_output_reserve_tokens - 512,
            ),
        )
        context_compressor = ContextCompressor(
            token_budget=max(512, tool_input_budget),
            output_reserve=0,
        )
        prompt_evidence = json.loads(json.dumps(evidence[-3:], ensure_ascii=False, default=str))

        # 构造用户消息：包含问题、已调用工具、最近证据和指令。
        prompt = {
            "question": question,
            "already_called": sorted(used),
            "evidence": prompt_evidence,
            "instruction": (
                f"只从 tools 白名单中选择最多 {max_calls} 个最有帮助的只读工具；"
                "如果现有证据已经足够，返回空调用。不要选择 manual 或 blocked 工具。"
                "第一轮优先只调用一个工具。"
            ),
        }

        system_message = {
            "role": "system",
            "content": "你是 OpsPilot 的工具规划器。必须遵守工具白名单，只规划只读调用。",
        }

        def request_tokens() -> int:
            serialized = json.dumps(prompt, ensure_ascii=False, default=str)
            return (
                context_compressor.estimate_messages([system_message, {"role": "user", "content": serialized}])
                + context_compressor.estimate_tokens(json.dumps(tool_specs, ensure_ascii=False, default=str))
            )

        # 优先保留问题、调用状态和每条证据的摘要；逐条移除样本，再裁短说明文字。
        while request_tokens() > tool_input_budget and prompt["evidence"]:
            sample_entry = next(
                (item for item in prompt["evidence"] if isinstance(item.get("sample"), list) and item["sample"]),
                None,
            )
            if sample_entry is not None:
                sample_entry["sample"].pop()
                sample_entry["samples_omitted"] = True
                continue
            if len(prompt["evidence"]) > 1:
                prompt["evidence"].pop(0)
                continue
            entry = prompt["evidence"][0]
            entry["summary"] = context_compressor.fit_text(str(entry.get("summary", "")), 80)
            entry["followup_hint"] = context_compressor.fit_text(str(entry.get("followup_hint", "")), 40)
            # 所有样本和长字段都已裁减后，极长问题仍需保留明确的省略标记。
            if request_tokens() > tool_input_budget:
                fixed_cost = context_compressor.estimate_messages([system_message])
                tool_cost = context_compressor.estimate_tokens(json.dumps(tool_specs, ensure_ascii=False, default=str))
                prompt["question"] = context_compressor.fit_text(
                    question,
                    max(32, tool_input_budget - fixed_cost - tool_cost - 128),
                )
            break

        # 构造请求 payload。
        payload = {
            "model": settings.chat_model,
            "messages": [
                system_message,
                {
                    "role": "user",
                    # default=str 兜底序列化非 JSON 类型。
                    "content": json.dumps(prompt, ensure_ascii=False, default=str),
                },
            ],
            "tools": tool_specs,
            "tool_choice": "auto",  # 让模型自行决定是否调用工具
            "temperature": 0,       # 降低随机性，保证规划稳定
            "max_tokens": 512,
        }
        if settings.chat_reasoning_effort:
            payload["reasoning_effort"] = settings.chat_reasoning_effort

        try:
            response = self._request(payload)
            message = response.get("choices", [{}])[0].get("message", {})
            raw_calls = message.get("tool_calls") or []

            # 兼容某些模型不返回 tool_calls 而是在 content 中返回 JSON 的情况。
            if not raw_calls and message.get("content"):
                content = str(message["content"]).replace("```json", "").replace("```", "").strip()
                parsed = json.loads(content)
                raw_calls = parsed.get("tool_calls", []) if isinstance(parsed, dict) else []

            # 规范化每个 tool_call 为 ModelToolCall。
            result = []
            for raw in raw_calls:
                # 兼容两种结构：{"function": {...}} 和 {"name": ..., "arguments": ...}
                function = raw.get("function", raw) if isinstance(raw, dict) else {}
                name = str(function.get("name", ""))
                arguments = function.get("arguments", {})

                # arguments 可能是 JSON 字符串，需要解析。
                if isinstance(arguments, str):
                    arguments = json.loads(arguments or "{}")

                # 只接受 name 存在且 arguments 是字典的调用。
                if name and isinstance(arguments, dict):
                    result.append(ModelToolCall(
                        name,
                        arguments,
                        str(arguments.get("reason", "")),
                    ))
            return result
        except (
            HTTPError,       # HTTP 错误
            URLError,        # 网络错误
            TimeoutError,    # 超时
            ValueError,      # 值错误
            KeyError,        # 结构缺失
            IndexError,      # 数组越界
            TypeError,       # 类型错误
            json.JSONDecodeError,  # JSON 解析失败
        ):
            # 任意不可用情况统一返回 None，触发规则兜底。
            return None

    @staticmethod
    def _request(payload: dict[str, Any]) -> dict[str, Any]:
        """向聊天模型发送 POST 请求并返回 JSON 响应。

        参数：
        - payload：请求体字典。

        返回：
        - dict：解析后的 JSON 响应。

        说明：
        - 超时使用 min(chat_timeout_seconds, 12)：
          工具规划属于辅助步骤，不应占用过长时间；
        - 使用标准库 urlopen，避免额外依赖；
        - 不在此处捕获异常，由调用方统一处理。
        """
        request = Request(
            f"{settings.chat_base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=min(settings.chat_timeout_seconds, 12)) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _fallback_summary(evidence: list[dict[str, Any]]) -> str:
        """生成工具编排的证据摘要文本。

        参数：
        - evidence：工具执行证据列表。

        返回：
        - str：面向用户的一句话摘要。

        说明：
        - 无证据时提示未匹配到工具；
        - 有证据时列出每个工具返回的条数；
        - 明确提示“涉及变更仍需审批”，避免用户误以为已经执行写操作。
        """
        if not evidence:
            return "未匹配到可自动调用的只读工具。"

        # 拼接每个工具的名称和返回条数。
        details = "、".join(
            f"{item['tool']} 返回 {item.get('result_count', 0)} 条"
            for item in evidence
        )
        return (
            "Agent 已完成只读工具编排：" + details +
            "。详细结果见工作流轨迹；涉及变更的操作仍需进入审批流程。"
        )

    @staticmethod
    def _compact(selection: ToolSelection, result: dict[str, Any]) -> dict[str, Any]:
        """把工具原始结果压缩为统一结构的证据。

        参数：
        - selection：已校验的工具选择；
        - result：工具原始返回的字典。

        返回：
        - dict：统一结构的证据，包含：
          * tool：工具名；
          * reason：选择理由；
          * status：工具状态；
          * result_count：结果条数；
          * sample：最多 3 条样本；
          * summary：摘要文本；
          * needs_followup：是否需要下一轮；
          * followup_hint：下一轮提示。

        设计说明：
        - 工具返回结构可能不同，统一从 rows/documents/metrics/approvals/events 中取列表；
        - 结果为空列表时自动标记 needs_followup=true，提示模型可能需要换工具；
        - 只保留前 3 条样本，控制后续 prompt 长度。
        """
        # 从多种可能的字段中取结果列表。
        rows = (
            result.get("rows")
            or result.get("documents")
            or result.get("metrics")
            or result.get("approvals")
            or result.get("events")
            or []
        )

        # 列表结构：取前 3 条样本并统计条数；非列表：直接作为样本。
        if isinstance(rows, list):
            sample = rows[:3]
            count = len(rows)
        else:
            sample = rows
            count = 0

        # 判断是否需要下一轮：
        # 1. 工具显式返回 needs_followup=true；
        # 2. 工具未显式声明且返回空列表时，视为需要 followup。
        followup = bool(result.get("needs_followup"))
        if "needs_followup" not in result and isinstance(rows, list) and not rows:
            followup = True

        return {
            "tool": selection.name,
            "reason": selection.reason,
            "status": result.get("status", "unknown"),
            "result_count": count,
            "sample": sample,
            "summary": (
                result.get("message")
                or (f"返回 {count} 条结构化记录" if isinstance(rows, list) else "已返回结构化状态")
            ),
            "needs_followup": followup,
            "followup_hint": result.get("followup_hint", "") if followup else "",
        }
