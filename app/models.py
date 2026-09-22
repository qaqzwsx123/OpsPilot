"""Agent 工作流共享的数据模型和枚举。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# 作用：说明类 ExecutionMode 的输入、输出与安全边界，避免调用方越过受控流程。
class ExecutionMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    BLOCKED = "blocked"


@dataclass(slots=True)
# 作用：说明类 WorkflowEvent 的输入、输出与安全边界，避免调用方越过受控流程。
class WorkflowEvent:
    stage: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
# 作用：说明类 CandidateTable 的输入、输出与安全边界，避免调用方越过受控流程。
class CandidateTable:
    name: str
    score: int
    matched_by: list[str]
    columns: list[str]


@dataclass(slots=True)
# 作用：说明类 GeneratedSql 的输入、输出与安全边界，避免调用方越过受控流程。
class GeneratedSql:
    sql: str
    intent: str
    confidence: float
    tables: list[str]


@dataclass(slots=True)
# 作用：说明类 ReviewResult 的输入、输出与安全边界，避免调用方越过受控流程。
class ReviewResult:
    accepted: bool
    issues: list[str]
    normalized_sql: str | None = None


@dataclass(slots=True)
# 作用：说明类 QueryResult 的输入、输出与安全边界，避免调用方越过受控流程。
class QueryResult:
    status: str
    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    approval_id: str | None = None
    events: list[WorkflowEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["events"] = [event.to_dict() for event in self.events]
        return result
