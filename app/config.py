"""应用配置：加载本地环境变量并集中暴露运行参数。

本模块负责：
1. 从项目根目录的 .env 文件加载本地环境变量；
2. 提供无第三方依赖的轻量 .env 解析器；
3. 将运行参数集中封装为不可变的 Settings 数据类；
4. 通过属性方法判断各能力是否启用；
5. 在模块导入时创建全局 settings 单例，供其他模块直接使用。

安全边界：
- 代码中只保存非敏感默认值；
- API Key 等敏感信息仅从环境变量读取，不会硬编码，也不会返回给前端；
- 已审批写操作默认关闭，保证演示环境安全；
- .env 使用 setdefault 加载，不会覆盖进程已有环境变量。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

import os  # 读取环境变量、设置环境变量
from dataclasses import dataclass  # 使用 dataclass 声明配置结构
from pathlib import Path  # 跨平台路径处理


def _load_local_env() -> None:
    """Small dependency-free .env loader for local desktop deployments.

    作用：
    - 从项目根目录读取 .env 文件；
    - 逐行解析 KEY=VALUE；
    - 忽略空行、注释行和不含等号的行；
    - 去除值两侧的引号；
    - 使用 os.environ.setdefault 写入，不覆盖已存在的环境变量。

    这样设计的原因：
    - 本地桌面部署可能没有 docker-compose 或系统环境变量；
    - 不引入 python-dotenv 等额外依赖，保持轻量；
    - setdefault 保证外部显式设置的环境变量优先级更高。
    """
    # 项目根目录 = 当前文件所在目录的父目录的父目录，即 app/config.py -> app -> 项目根。
    path = Path(__file__).resolve().parent.parent / ".env"

    # 如果 .env 文件不存在，直接返回，使用进程环境变量或代码默认值。
    if not path.exists():
        return

    # 按 UTF-8 读取文件内容，并按行分割。
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        # 去掉行首尾空白。
        line = raw_line.strip()

        # 跳过空行、以 # 开头的注释行、以及不包含 = 的行。
        if not line or line.startswith("#") or "=" not in line:
            continue

        # 只按第一个 = 分割，允许值中包含 =。
        key, value = line.split("=", 1)

        # 去除键和值两侧空白，并去掉值两侧的单引号或双引号。
        # setdefault 确保不覆盖已经存在的环境变量。
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'")
        )


# 模块导入时立即加载 .env，使后续 Settings 类定义能读取到环境变量。
_load_local_env()


def _flag(name: str, default: bool = False) -> bool:
    """将 .env 中常见的字符串布尔值统一转换为 Python bool。

    参数：
    - name：环境变量名称；
    - default：环境变量不存在时的默认布尔值。

    返回：
    - bool：解析后的布尔值。

    识别为 True 的字符串：
    - "1"、"true"、"yes"、"on"（不区分大小写，允许两侧空白）。
    其他值均视为 False。
    """
    # 读取环境变量，若不存在则使用默认值的字符串形式；
    # 统一转小写并去除空白后，判断是否属于真值集合。
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """应用运行配置。

    配置值优先从项目根目录的 ``.env`` 和进程环境读取，代码只保存非敏感默认值。
    SQL 生成模型和 Agent 聊天模型可以是两个独立的 OpenAI 兼容服务。

    设计说明：
    - frozen=True：实例创建后不可修改，避免运行期意外篡改配置；
    - slots=True：使用 __slots__ 减少内存占用，并防止动态添加属性；
    - 字段默认值在类定义时通过 os.getenv 求值，因此模块导入时即确定配置；
    - 敏感信息（API Key）只从环境变量读取，不提供默认明文。
    """

    # ---------- SQL Writer / Safe SQL Agent 相关配置 ----------

    # SQL Writer 使用的模型服务地址；为空时使用规则生成器。
    # 来源环境变量：SAFE_SQL_AGENT_LLM_BASE_URL
    # rstrip("/") 去掉末尾斜杠，便于后续拼接 /chat/completions。
    llm_base_url: str = os.getenv("SAFE_SQL_AGENT_LLM_BASE_URL", "").rstrip("/")

    # SQL Writer 的访问令牌，不会返回给前端。
    # 来源环境变量：SAFE_SQL_AGENT_LLM_API_KEY
    llm_api_key: str = os.getenv("SAFE_SQL_AGENT_LLM_API_KEY", "")

    # SQL Writer 使用的模型名称。
    # 来源环境变量：SAFE_SQL_AGENT_LLM_MODEL
    llm_model: str = os.getenv("SAFE_SQL_AGENT_LLM_MODEL", "")

    # SQL Writer 单次 HTTP 请求超时时间，单位秒。
    # 来源环境变量：SAFE_SQL_AGENT_LLM_TIMEOUT，默认 20 秒。
    llm_timeout_seconds: int = int(os.getenv("SAFE_SQL_AGENT_LLM_TIMEOUT", "20"))

    # 可选 MySQL 连接串；为空时使用本地 SQLite 演示库。
    # 来源环境变量：SAFE_SQL_AGENT_MYSQL_URL
    mysql_url: str = os.getenv("SAFE_SQL_AGENT_MYSQL_URL", "")

    # 是否允许已审批写操作真正落库，默认关闭以保证演示安全。
    # 来源环境变量：SAFE_SQL_AGENT_ALLOW_APPROVED_WRITES
    # 使用 _flag 解析字符串布尔值，默认 False。
    allow_approved_writes: bool = _flag("SAFE_SQL_AGENT_ALLOW_APPROVED_WRITES")

    # ---------- Agent 聊天服务相关配置 ----------

    # Agent 聊天服务类型，目前支持 openai-compatible。
    # 来源环境变量：MODEL_PROVIDER，默认 "openai-compatible"。
    # strip().lower() 统一为小写，便于比较。
    chat_provider: str = os.getenv("MODEL_PROVIDER", "openai-compatible").strip().lower()

    # Agent 聊天模型的 OpenAI 兼容服务地址。
    # 来源环境变量：MODEL_BASE_URL。
    # rstrip("/") 去掉末尾斜杠，便于拼接接口路径。
    chat_base_url: str = os.getenv("MODEL_BASE_URL", "").rstrip("/")

    # Agent 聊天服务访问令牌；本地模型服务可按需留空。
    # 来源环境变量：MODEL_API_KEY。
    chat_api_key: str = os.getenv("MODEL_API_KEY", "")

    # Agent 聊天模型名称。
    # 来源环境变量：MODEL_NAME。
    chat_model: str = os.getenv("MODEL_NAME", "")

    # 可选推理强度；Ollama 的思考模型可设为 none，避免回复额度被推理内容耗尽。
    # 留空时不向其他 OpenAI 兼容服务发送此扩展参数。
    chat_reasoning_effort: str = os.getenv("MODEL_REASONING_EFFORT", "").strip().lower()

    # Agent 聊天请求超时时间，单位秒。
    # 来源环境变量：MODEL_TIMEOUT，默认 90 秒，适合本地大模型较慢的响应。
    chat_timeout_seconds: int = int(os.getenv("MODEL_TIMEOUT", "90"))

    # 模型上下文窗口和回复预留，压缩器按二者计算本轮输入预算。
    # 本地模型窗口不同时可在 .env 中覆盖；预算包含 system、历史和当前问题。
    chat_context_window_tokens: int = int(os.getenv("MODEL_CONTEXT_WINDOW_TOKENS", "8192"))
    chat_output_reserve_tokens: int = int(os.getenv("MODEL_OUTPUT_RESERVE_TOKENS", "1024"))
    chat_recent_messages: int = int(os.getenv("MODEL_CONTEXT_RECENT_MESSAGES", "8"))

    # Agent 工具规划是辅助调用，使用独立的小预算，避免过多证据挤占规划提示。
    tool_context_budget_tokens: int = int(os.getenv("AGENT_TOOL_CONTEXT_TOKENS", "2048"))

    # 工具规划模式：model/auto/on 使用模型 Function Calling，否则走规则白名单。
    # 来源环境变量：AGENT_TOOL_PLANNER_MODE，默认 "model"。
    # strip().lower() 统一格式，便于后续属性判断。
    tool_planner_mode: str = os.getenv("AGENT_TOOL_PLANNER_MODE", "model").strip().lower()

    # ---------- 能力开关属性 ----------

    @property
    def llm_enabled(self) -> bool:
        """SQL Writer 是否启用模型服务。

        条件：同时配置了 llm_base_url、llm_api_key、llm_model。
        如果任一为空，则回退到规则生成器。
        """
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def mysql_enabled(self) -> bool:
        """是否配置了 MySQL 连接串。

        如果为空，则使用本地 SQLite 演示库。
        """
        return bool(self.mysql_url)

    @property
    def chat_enabled(self) -> bool:
        """Agent 聊天服务是否可用。

        条件：
        - chat_provider 必须是 "openai-compatible"；
        - 必须配置 chat_base_url 和 chat_model。
        API Key 可为空，因为部分本地模型服务不需要认证。
        """
        return self.chat_provider == "openai-compatible" and bool(self.chat_base_url and self.chat_model)

    @property
    def model_tool_planner_enabled(self) -> bool:
        """查询工作流是否允许请求本地模型选择只读工具。

        当 tool_planner_mode 为以下之一时启用：
        - "model"、"auto"、"on"、"true"、"1"
        否则走规则白名单模式。
        """
        return self.tool_planner_mode in {"model", "auto", "on", "true", "1"}


# 模块导入时创建全局配置单例。
# 其他模块可以通过 ``from app.config import settings`` 直接使用。
settings = Settings()
