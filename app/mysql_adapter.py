"""可选 MySQL 只读适配器；默认演示环境仍使用本地 SQLite。

本模块提供一个可选的 MySQL 只读适配器，用于真实部署时替换本地 SQLite 演示库。

设计目标：
1. 只在配置了 SAFE_SQL_AGENT_MYSQL_URL 时启用；
2. 只用于只读查询和元数据读取，绝不执行写操作；
3. 使用短生命周期连接，不复用到跨请求状态中；
4. 不把密码等敏感信息写入审计日志或暴露给前端；
5. 连接失败时由上层切回 SQLite/规则路径，不影响演示可用性。

安全边界：
- 只暴露只读查询入口 execute_readonly 和只读元数据入口 introspect_schema；
- 不做建表、写入、DDL 等操作；
- URL 解析严格校验 scheme、host 和 database；
- PyMySQL 未安装时给出明确安装提示，而不是静默失败。
"""

from __future__ import annotations  # 延迟解析类型注解，提升兼容性并避免运行时求值

from collections import defaultdict  # 用于按表名分组列信息
from functools import lru_cache  # 缓存 schema 元数据，避免每次请求都查 information_schema
from urllib.parse import unquote, urlparse  # 解析 MySQL URL，支持百分号编码的账号密码

from app.config import settings  # 读取 SAFE_SQL_AGENT_MYSQL_URL 等配置


def _connect():
    """创建短生命周期的 MySQL 连接。

    该适配器只用于真实库的元数据读取和只读查询，连接不复用到跨请求状态中，
    也不会把密码写入审计日志。

    返回：
    - pymysql.Connection：一个开启 DictCursor 的 MySQL 连接。

    异常：
    - RuntimeError：
      * 未安装 PyMySQL 时提示安装命令；
      * URL 格式非法时提示期望格式。

    设计说明：
    - 延迟导入 pymysql，避免未启用 MySQL 时强制安装依赖；
    - 使用 urlparse 解析 URL，unquote 解码用户名和密码中的特殊字符；
    - 连接超时 5 秒、读超时 15 秒，避免长时间阻塞；
    - charset 固定 utf8mb4，兼容中文和 emoji；
    - cursorclass 使用 DictCursor，使查询结果可按列名访问。
    """
    try:
        import pymysql
    except ImportError as exc:
        # 未安装 PyMySQL 时给出明确的安装指引，避免用户排查困难。
        raise RuntimeError(
            "已配置 SAFE_SQL_AGENT_MYSQL_URL，但未安装 PyMySQL。"
            "请执行 pip install -e '.[mysql]'。"
        ) from exc

    # 解析 MySQL URL，期望格式：mysql://user:password@host:3306/database
    parsed = urlparse(settings.mysql_url)

    # 校验 scheme、hostname 和 database 是否齐全且合法。
    if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise RuntimeError(
            "SAFE_SQL_AGENT_MYSQL_URL 格式应为 mysql://user:password@host:3306/database"
        )

    # 返回连接对象；密码通过 unquote 解码，兼容 URL 编码的特殊字符。
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,                     # 默认端口 3306
        user=unquote(parsed.username or ""),          # 解码用户名
        password=unquote(parsed.password or ""),      # 解码密码
        database=parsed.path.strip("/"),              # 去掉路径前的 /
        charset="utf8mb4",                            # 支持中文和 emoji
        cursorclass=pymysql.cursors.DictCursor,       # 结果按列名访问
        connect_timeout=5,                            # 连接超时 5 秒
        read_timeout=15,                              # 读超时 15 秒
    )


def execute_readonly(sql: str) -> list[dict]:
    """执行只读 SQL 并返回字典列表。

    参数：
    - sql：已通过上层审查的只读 SQL。

    返回：
    - list[dict]：查询结果，每行是一个字典。

    安全边界：
    - 只暴露只读查询，连接失败时由上层切回 SQLite/规则路径；
    - 本函数不做 SQL 校验，调用方必须确保 SQL 只读；
    - 使用 with 管理连接和游标，确保自动关闭。
    """
    # 只暴露只读查询，连接失败时由上层切回 SQLite/规则路径。
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            # fetchall 返回 DictCursor 解析后的字典列表。
            return list(cursor.fetchall())


@lru_cache(maxsize=1)
def introspect_schema() -> dict[str, dict[str, object]]:
    """读取当前数据库表结构，转换为元数据召回器统一使用的 schema 形态。

    返回：
    - dict：键为表名，值为 {"columns": [...], "aliases": [...], "description": "..."}。

    说明：
    - 使用 information_schema.columns 查询当前数据库的所有表和列；
    - 按 ordinal_position 排序，保证列顺序与表定义一致；
    - 使用 lru_cache 缓存结果，避免每次请求都查询 information_schema；
    - 该函数只读，不修改任何数据库结构。

    安全边界：
    - 只查询当前 DATABASE() 的元数据，不跨库读取；
    - 返回值不含敏感信息，可直接用于 SQL Agent 的 schema 召回。
    """
    # 将 MySQL 表结构转换成 SQL Agent 使用的统一 schema 形态。
    """Loads table/column metadata once for three-way recall in a real MySQL deployment."""

    # 查询当前数据库的所有表和列，按表名和列顺序排序。
    query = """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
        ORDER BY table_name, ordinal_position
    """

    # 用 defaultdict(list) 按表名收集列名。
    grouped: dict[str, list[str]] = defaultdict(list)

    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            for row in cursor.fetchall():
                grouped[row["table_name"]].append(row["column_name"])

    # 转换为统一 schema 形态：columns、aliases、description。
    # aliases 目前只用表名本身，后续可扩展为同义词或中文别名。
    return {
        name: {
            "columns": columns,
            "aliases": [name],
            "description": f"MySQL table {name}",
        }
        for name, columns in grouped.items()
    }