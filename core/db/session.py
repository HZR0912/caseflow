"""数据库访问层：连接串统一从 settings / 环境变量来。"""

from __future__ import annotations

from apps.api.settings import get_settings
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def get_engine(*, echo: bool = False) -> Engine:
    """同步引擎（Alembic、脚本、W1 的 checkpoint 验证都用它）。

    异步访问（FastAPI 请求路径）在 W2 引入 asyncpg / psycopg 异步池后再加，
    现在不提前抽象。
    """
    return create_engine(get_settings().database_url, pool_pre_ping=True, echo=echo)
