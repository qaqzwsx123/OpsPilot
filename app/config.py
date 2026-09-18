from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration. Secrets stay in environment variables, never in code."""

    llm_base_url: str = os.getenv("SAFE_SQL_AGENT_LLM_BASE_URL", "").rstrip("/")
    llm_api_key: str = os.getenv("SAFE_SQL_AGENT_LLM_API_KEY", "")
    llm_model: str = os.getenv("SAFE_SQL_AGENT_LLM_MODEL", "")
    llm_timeout_seconds: int = int(os.getenv("SAFE_SQL_AGENT_LLM_TIMEOUT", "20"))
    mysql_url: str = os.getenv("SAFE_SQL_AGENT_MYSQL_URL", "")
    allow_approved_writes: bool = _flag("SAFE_SQL_AGENT_ALLOW_APPROVED_WRITES")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def mysql_enabled(self) -> bool:
        return bool(self.mysql_url)


settings = Settings()
