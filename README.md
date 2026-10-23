# CaseFlow（事务官）

面向企业跨系统办事场景的 Agent：**听懂需求 → 查制度 → 做计划 → 预演 → 等人审批 → 执行 → 失败回滚 → 留痕可审**。

定位不是 RAG 问答机器人，而是 **Agent 的写操作平面**：读操作人人会做，写操作（可逆性、幂等、权限、审批、追溯、部分成功语义）才是工业界的门槛。

完整方案见 `../CaseFlow-企业办事Agent-项目设计方案.md`。

## 当前状态

W1（骨架周）：仓库 / Docker Compose / Alembic / 最小 LangGraph 断点恢复 / 最小 OPA 策略 / CI。

| 目标 | 状态 |
|---|---|
| `docker compose up` 一条命令起 postgres + redis + opa | 进行中 |
| CI 绿（ruff + pytest + `opa test`） | 进行中 |
| 能演示"策略拒绝一次请求" | 进行中 |

## 快速开始

```bash
cp .env.example .env
docker compose up -d              # postgres:5432 / redis:6379 / opa:8181
uv sync --extra dev               # 安装依赖（含 dev 组）
uv run alembic upgrade head       # 建表
uv run uvicorn apps.api.main:app --reload --port 8000
```

验证：

```bash
curl http://127.0.0.1:8000/healthz
curl -X POST http://127.0.0.1:8000/v1/policy/check \
  -H 'content-type: application/json' \
  -d '{"user":{"id":"u_1","role":"intern"},"action":"admin.grant","resource":{"type":"account"}}'
# -> {"allow": false, "reason": "..."}
```

跨进程断点恢复（W1 验收项之一）：

```bash
uv run python -m scripts.w1_checkpoint_demo start    # 触发审批中断后退出进程
uv run python -m scripts.w1_checkpoint_demo resume   # 新进程从 checkpoint 恢复并完成
```

## 目录

```
apps/api/          FastAPI 主服务
apps/mockcorp/     Mock 企业系统（W2）
apps/web/          前端（W5+）
core/guardrails/   输入护栏（W4）
core/graph/        LangGraph 状态机、状态定义、子图
core/memory/       三类记忆（W9）
core/rag/          检索 / 融合 / 精排 / 版本冲突（W3）
core/tools/        MCP server、网关、幂等、saga（W7-W8）
core/policy/       OPA 客户端与策略调用
core/obs/          OTel、成本计量（W10）
eval/              场景 DSL、runner、judge（W11）
migrations/        Alembic
policies/          Rego 策略（OPA）
scripts/           一次性验证脚本
docs/              架构与 ADR
```

## 开发约定

- **小步提交**：每完成一个可验证的小步骤就 commit 一次，随时可以 `git reset --hard <commit>` 回到任意历史版本。
- **ADR 先行**：关键取舍先写 `docs/design-decisions/` 再写代码。
- **数字不提前编**：所有指标做完实测后填入 `docs/benchmark.md`。
