# W10：可观测性、Langfuse 与 Trace 回放

## 这周解决什么问题

给办事过程装一个行车记录仪。之前知道最后办成或失败，现在可以知道哪一步慢、为什么被拒绝、重试了几次、失败后撤销了什么。打开回放只是看记录，不会重新开户、发邀请或扣钱。

## 已实现

- 显式 OTel SDK Span：规划五节点、审批三节点、策略检查、模型 HTTP 调用、工具每次尝试、Saga 补偿。
- 同一申请的提交、决定、执行、恢复使用稳定 Trace ID；父子 Span 保留调用关系。独立预览使用新的 Trace ID。
- PostgreSQL `observation_traces` / `observation_spans` 保存开始与结束帧。强杀后未结束的帧保留为 `running`，新进程追加记录。
- 可选通用 OTLP HTTP 导出与 Langfuse 原生 OTLP 导出；批处理队列上限 2048，网络超时 2 秒。导出失败不回滚业务。
- `/traces` 页面：列表、播放/暂停、上一步/下一步、进度条、节点输入输出摘要、耗时、错误、tokens 和 USD 估算。审批台顶部提供入口。
- 读取接口：`GET /v1/traces?request_id=<UUID>`、`GET /v1/traces/<32位trace_id>`。最多 100 条 Trace、每条最多 2000 帧，截断明确提示。

## 启用与复验

在仓库根目录运行（已有 PostgreSQL / OPA 环境）：

```powershell
uv sync --frozen --extra dev
uv run alembic upgrade head
npm --prefix apps/web ci
npm --prefix apps/web run build
$env:OBSERVATION_ENABLED = 'true'
uv run python -m scripts.serve_api --port 18000
```

访问 `http://127.0.0.1:18000/trace`s，输入现有 `AUTH_CREDENTIALS` 对应的凭据。默认采集关闭；打开开关后只会采集新操作，旧业务不会补造记录。API 镜像也需要重新构建才能加载新代码。

可选本地 Langfuse（独立 Compose 项目，不替换业务数据库）：

```powershell
uv run python -m scripts.setup_observability
docker compose --env-file tmp/observability.env -f docker-compose.observability.yml up -d
uv run python -m scripts.w10_observability_demo --check --browser-check --langfuse-check
```

Langfuse 地址 `http://127.0.0.1:13000`，本地账号 `local@example.com`；随机密码及项目密钥在被 Git 忽略的 `tmp/observability.env` 中。演示脚本自动读入这些值，不打印密钥。普通 API 需将文件中的 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 配入启动环境。容器访问宿主机服务时 BASE_URL 使用 `http://host.docker.internal:13000`（Docker Desktop）。该 Compose 的内部服务密码仅用于本地开发。

无 Langfuse 也能运行 `uv run python -m scripts.w10_observability_demo --check`。普通 CI 运行这一版本；本地完整验证另外覆盖浏览器和真实 Langfuse。

## 数据、权限与统计口径

原始提示词、模型正文、员工资料、工具参数、Bearer、异常文本和堆栈不进入观测记录。节点只保存白名单结构摘要：字段名、人数、检索 chunk ID 与偏移、步骤 ID、动作、决策、状态及记忆来源。回放无法还原完整敏感输入，这是有意的取舍。

OPA 决定 `trace.read` / `trace.read_all`；员工及负责人只能看自己的 Trace，admin 可看本租户，跨租户始终不可见。API 返回 `Cache-Control: no-store`。浏览器凭据仅留在内存；退出或切换身份清空旧数据，并丢弃迟到的旧请求响应。Langfuse 是独立运维系统，访问权限由其项目权限管理，不等同于 CaseFlow 的租户接口权限。

实际处理耗时仅累加最外层阶段，避免把嵌套 Span 重复相加；全程跨度包含阶段间审批等待。未结束的阶段不推算完成耗时，页面明确展示未结束数量。存在截断或未完成时统计为部分数据。

模型 tokens 读取兼容接口返回的 `usage.prompt_tokens` / `completion_tokens`。可在每个 `PLANNING_PROVIDERS` 配置 `input_usd_per_million`、`output_usd_per_million`。缺 usage 或缺价格显示未知；纯规则零模型调用单独标注。费用是 USD 估算，不是供应商账单。

## 已知边界

- 当前 Span 覆盖 API 内的业务节点和出站调用；向 OPA / MockCorp 传播 W3C `traceparent`，没有宣称对方内部每个数据库操作均有 Span。同步直接调用图的兼容入口保留原行为，持久追踪针对 API 使用的异步路径。
- 观测写入独立连接，故障后冷却 30 秒；冷却期间可能缺帧。开始已写、结束未写会显示未知。整个开始写入失败可能无法从回放识别缺失；这不是审计级无损日志。
- Langfuse 使用内存批处理，强杀、队列满或远端故障可能丢导出数据；本地已落库帧可回放，但没有自动补传队列。
- 尚未加入生产保留期清理、采样、全局告警或吞吐容量基准；列表与单 Trace 读取已有硬上限。

实测结果见 [W10 验证报告](w10-verification.md)，决策见 [ADR-0012](design-decisions/0012-observability.md)。
