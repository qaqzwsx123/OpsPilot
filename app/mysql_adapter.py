from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from urllib.parse import unquote, urlparse

from app.config import settings


def _connect():
    """Create a short-lived MySQL connection from mysql://user:password@host:3306/database."""
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("已配置 SAFE_SQL_AGENT_MYSQL_URL，但未安装 PyMySQL。请执行 pip install -e '.[mysql]'。") from exc
    parsed = urlparse(settings.mysql_url)
    if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise RuntimeError("SAFE_SQL_AGENT_MYSQL_URL 格式应为 mysql://user:password@host:3306/database")
    return pymysql.connect(
        host=parsed.hostname, port=parsed.port or 3306, user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""), database=parsed.path.strip("/"), charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5, read_timeout=15,
    )


def execute_readonly(sql: str) -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())


@lru_cache(maxsize=1)
def introspect_schema() -> dict[str, dict[str, object]]:
    """Loads table/column metadata once for three-way recall in a real MySQL deployment."""
    query = """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
        ORDER BY table_name, ordinal_position
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            for row in cursor.fetchall():
                grouped[row["table_name"]].append(row["column_name"])
    return {
        name: {"columns": columns, "aliases": [name], "description": f"MySQL table {name}"}
        for name, columns in grouped.items()
    }
