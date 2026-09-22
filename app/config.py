"""应用配置：加载本地环境变量并集中暴露运行参数。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env() -> None:
    """Small dependency-free .env loader for local desktop deployments."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


# 作用：说明函数 _flag 的输入、输出与安全边界，避免调用方越过受控流程。
def _flag(name: str, default: bool = False) -> bool:
    # 将 .env 中常见的字符串布尔值统一转换为 Python bool。
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """应用运行配置。

    配置值优先从项目根目录的 ``.env`` 和进程环境读取，代码只保存非敏感默认值。
    SQL 生成模型和 Agent 聊天模型可以是两个独立的 OpenAI 兼容服务，便于本地部署 DeepSeek。
    """

    # SQL Writer 使用的模型服务地址；为空时使用规则生成器。
    llm_base_url: str = os.getenv("SAFE_SQL_AGENT_LLM_BASE_URL", "").rstrip("/")
    # SQL Writer 的访问令牌，不会返回给前端。
    llm_api_key: str = os.getenv("SAFE_SQL_AGENT_LLM_API_KEY", "")
    # SQL Writer 使用的模型名称。
    llm_model: str = os.getenv("SAFE_SQL_AGENT_LLM_MODEL", "")
    # SQL Writer 单次 HTTP 请求超时时间。
    llm_timeout_seconds: int = int(os.getenv("SAFE_SQL_AGENT_LLM_TIMEOUT", "20"))
    # 可选 MySQL 连接串；为空时使用本地 SQLite 演示库。
    mysql_url: str = os.getenv("SAFE_SQL_AGENT_MYSQL_URL", "")
    # 是否允许已审批写操作真正落库，默认关闭以保证演示安全。
    allow_approved_writes: bool = _flag("SAFE_SQL_AGENT_ALLOW_APPROVED_WRITES")
    # Agent 聊天服务类型，目前支持 openai-compatible。
    chat_provider: str = os.getenv("MODEL_PROVIDER", "openai-compatible").strip().lower()
    # Agent 聊天模型的 OpenAI 兼容服务地址。
    chat_base_url: str = os.getenv("MODEL_BASE_URL", "").rstrip("/")
    # Agent 聊天服务访问令牌；本地 DeepSeek 可为空。
    chat_api_key: str = os.getenv("MODEL_API_KEY", "")
    # Agent 聊天模型名称。
    chat_model: str = os.getenv("MODEL_NAME", "")
    # Agent 聊天请求超时时间。
    chat_timeout_seconds: int = int(os.getenv("MODEL_TIMEOUT", "90"))
    # 工具规划模式：model/auto/on 使用模型 Function Calling，否则走规则白名单。
    tool_planner_mode: str = os.getenv("AGENT_TOOL_PLANNER_MODE", "model").strip().lower()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def mysql_enabled(self) -> bool:
        return bool(self.mysql_url)

    @property
    def chat_enabled(self) -> bool:
        return self.chat_provider == "openai-compatible" and bool(self.chat_base_url and self.chat_model)

    @property
    def model_tool_planner_enabled(self) -> bool:
        """Whether query workflows may ask the local model to select read-only tools."""
        return self.tool_planner_mode in {"model", "auto", "on", "true", "1"}


settings = Settings()
