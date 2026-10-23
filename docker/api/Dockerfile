# 主服务镜像（compose 里 profile: app 才会用到）
# 依赖 uv.lock 做可复现安装：镜像里的版本必须和本地一致
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /srv

# 先只拷依赖清单，让依赖层可缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY apps ./apps
COPY core ./core
COPY migrations ./migrations
COPY policies ./policies
COPY scripts ./scripts

# 非 root 运行
RUN useradd --create-home --uid 10001 caseflow && chown -R caseflow:caseflow /srv
USER caseflow

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
