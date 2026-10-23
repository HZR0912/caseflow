# CaseFlow（事务官）

[![CI](https://github.com/HZR0912/caseflow/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/HZR0912/caseflow/actions/workflows/ci.yml)

面向企业跨系统办事场景的 Agent：**听懂需求 → 查制度 → 做计划 → 预演 → 等人审批 → 执行 → 失败回滚 → 留痕可审**。

定位不是 RAG 问答机器人，而是 **Agent 的写操作平面**：读操作人人会做，写操作（可逆性、幂等、权限、审批、追溯、部分成功语义）才是工业界的门槛。

完整方案见 `../CaseFlow-企业办事Agent-项目设计方案.md`。

## 当前状态

W1（骨架周）已完成；W2 已实现 MockCorp 四个系统、13 个写操作，支持 dry-run、
PostgreSQL 持久化幂等、故障注入。启动与验收见 [MockCorp API](docs/mockcorp-api.md)。
线上验证结果见顶部 CI 徽章。

| 验收项 | 状态 | 证据 |
|---|---|---|
| `docker compose --profile app up -d --build` 启动四个服务 | ✅ | postgres / redis / opa / api 四个容器全部 `healthy`；不带 profile 时仅启动前三个服务 |
| CI 绿（ruff + pytest + `opa test`） | ✅ | [GitHub Actions 首次成功运行](https://github.com/HZR0912/caseflow/actions/runs/39906307840)：lint-and-test、policy、integration 三个 job 全部通过，含迁移往返和断点恢复集成测试 |
| 能演示"策略拒绝一次请求" | ✅ | intern 请求 `admin.grant` → 403 `self_escalation_forbidden` |
| 跨进程断点恢复 | ✅ | `start` 在审批断点后正常退出，另一个进程 `resume` 完成；另已实测 checkpoint 持久化后强杀进程再恢复 |

W2 本地已通过 95 项非集成测试、6 项真实 PostgreSQL 集成测试；包含迁移往返、并发单次扣款、
余额竞争、207 部分成功持久化、HTTP 超时后强杀服务并重启重放。
Docker 实际启动、容器重启后幂等重放，以及文档中的 PowerShell/curl 命令均已验证。另提供
`uv run python -m scripts.w2_mockcorp_demo` 做真实 HTTP 验收，需按文档配置凭据和开启故障。

已知缺口：Agent 执行器仍返回 501，工具网关 / 自动重试 / 补偿在 W7–W8；
护栏、RAG、记忆、MCP 工具、审批台、Trace 回放、评测 harness 尚未实现。
MockCorp 的供应方幂等已实现，尚未接入 Agent 编排。

## 快速开始

以下命令在仓库根目录的 **PowerShell** 中执行，需要 Python 3.12、uv、Docker。
先安装依赖，再为演示生成一个随机 token；服务端只接收它的 SHA-256 摘要及 intern 身份映射。

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv sync --frozen --extra dev

$token = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
$sha256 = [Security.Cryptography.SHA256]::Create()
$tokenHash = -join ($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($token)) | ForEach-Object { $_.ToString("x2") })
$sha256.Dispose()
$env:AUTH_CREDENTIALS = ConvertTo-Json -Compress -InputObject @(
    @{ token_sha256 = $tokenHash; user_id = "u_intern"; role = "intern"; tenant = "demo" }
)

docker compose --profile app up -d --build
uv run alembic upgrade head
```

健康检查通过后，在同一个 PowerShell 窗口验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
$headers = @{ Authorization = "Bearer $token" }
$body = @{ action = "admin.grant"; resource_type = "account" } | ConvertTo-Json -Compress
Invoke-RestMethod http://127.0.0.1:8000/v1/policy/check -Method Post -Headers $headers -ContentType "application/json" -Body $body
# HTTP 200，allow=false，reason=self_escalation_forbidden

$body | curl.exe --silent --show-error -i http://127.0.0.1:8000/v1/actions/execute -H "Authorization: Bearer $token" -H "Content-Type: application/json" --data-binary "@-"
# HTTP 403，detail.reason=self_escalation_forbidden
```

请求体只接收 `action`、`resource_type`、可选的 `resource_id`。
`user_id`、`role`、`tenant` 从服务端凭据映射获取，放进请求体会返回 **422**。
两个策略接口都要求 Bearer token：缺失、未知或未配置凭据时返回 **401**；健康检查无需认证。
有效身份被策略拒绝返回 **403**（执行接口），策略放行后暂时返回 **501**（执行器尚未实现）。

此演示的 `$token` 和 `$env:AUTH_CREDENTIALS` 只保存在当前 shell 中；新窗口需重新生成并重建 API 容器，
或自行将摘要映射保存到不入库的 `.env`（`AUTH_CREDENTIALS` 为 JSON 数组）并妥善保管原始 token。
撤销或更改角色需要修改服务端配置并重启 API。该机制是开发阶段的静态凭据映射，尚未接入企业 SSO，
也不代表已实现租户资源隔离。设计取舍见 [ADR-0002](docs/design-decisions/0002-trusted-api-identity.md)。

若需要本机热重载，在完成上述凭据配置后使用以下方式，避免同时运行容器 API 占用 8000 端口：

```powershell
docker compose --profile app stop api
docker compose up -d              # 仅 postgres / redis / opa
uv run alembic upgrade head
uv run uvicorn apps.api.main:app --reload --port 8000
```

修改 `policies/*.rego` 后运行 `docker compose restart opa`，再验证策略结果。
Windows Docker 的文件挂载不一定能把变更事件传给 `--watch`；`opa test` 读到新文件通过，
不代表正在运行的 OPA 已加载新策略。

跨进程断点恢复（W1 验收项之一）：

```bash
uv run python -m scripts.w1_checkpoint_demo start    # 触发审批中断后退出进程
uv run python -m scripts.w1_checkpoint_demo resume   # 新进程从 checkpoint 恢复并完成
uv run python -m scripts.w1_checkpoint_demo status   # 查 checkpoint 状态
uv run python -m scripts.w1_checkpoint_demo reset    # 清掉本 thread 的 checkpoint，方便重演
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
