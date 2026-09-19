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
    chat_provider: str = os.getenv("MODEL_PROVIDER", "openai-compatible").strip().lower()
    chat_base_url: str = os.getenv("MODEL_BASE_URL", "").rstrip("/")
    chat_api_key: str = os.getenv("MODEL_API_KEY", "")
    chat_model: str = os.getenv("MODEL_NAME", "")
    chat_timeout_seconds: int = int(os.getenv("MODEL_TIMEOUT", "90"))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def mysql_enabled(self) -> bool:
        return bool(self.mysql_url)

    @property
    def chat_enabled(self) -> bool:
        return self.chat_provider == "openai-compatible" and bool(self.chat_base_url and self.chat_model)


settings = Settings()
