"""数据库访问层：连接串统一从 settings / 环境变量来。"""

from __future__ import annotations

from apps.api.settings import get_settings
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def get_engine(*, echo: bool = False) -> Engine:
    """同步引擎（Alembic、脚本、W1 的 checkpoint 验证都用它）。

    长驻审批与供应方服务使用 core.db.pools 的 psycopg 池；此引擎用于独立同步脚本。
    """
    return create_engine(get_settings().database_url, pool_pre_ping=True, echo=echo)
