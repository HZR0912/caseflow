"""Alembic 环境。

与 docker-compose / .env 的约定：
- 连接串只从环境变量 DATABASE_URL（或 apps.api.settings）来，不在 alembic.ini 里写死。
- target_metadata 留空到 W2：W1 的第一批表用手写迁移表达，比先铺一套 ORM 更快也更清楚。
  等 W2 引入 SQLAlchemy 模型后改为 autogenerate。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 `alembic` 命令能 import 到仓库根的 apps/ 与 core/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def get_url() -> str:
    """环境变量优先；否则回落到 settings 的默认值，保证本地零配置可跑。"""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    from apps.api.settings import get_settings

    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
