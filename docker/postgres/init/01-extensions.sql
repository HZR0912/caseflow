-- W1：pgvector 扩展。业务表结构由 Alembic 管理，这里只做镜像级一次性初始化。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
