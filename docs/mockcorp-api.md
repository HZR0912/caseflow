# W2 MockCorp API

独立 FastAPI 服务，模拟 Identity、Code、Ticket、Money 四个企业系统，共 13 个写操作。
这是 CaseFlow 后续 MCP 和执行器的 HTTP 对接目标。W2 实现供应方的事务与幂等；
Agent 的 OPA 校验、审批、自动重试和 Saga 编排尚未接入此服务。

## 启动（PowerShell）

在仓库根目录执行，需要 Docker、Python 3.12 和 uv。只使用虚构数据。

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv sync --frozen --extra dev
$token = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
$sha256 = [Security.Cryptography.SHA256]::Create()
$tokenHash = -join ($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($token)) | ForEach-Object { $_.ToString("x2") })
$sha256.Dispose()
$env:AUTH_CREDENTIALS = ConvertTo-Json -Compress -InputObject @(
    @{ token_sha256 = $tokenHash; user_id = "mock-operator"; role = "admin"; tenant = "w2-demo-" + [guid]::NewGuid().ToString("N") }
)
$env:MOCKCORP_FAULTS_ENABLED = "true"
docker compose --profile mockcorp up -d --build --wait
Invoke-RestMethod http://127.0.0.1:8001/healthz
```

容器启动时自动执行 `alembic upgrade head`；MockCorp 只依赖 PostgreSQL。
OpenAPI 与交互调试页面：<http://127.0.0.1:8001/docs>。
若同时需要 W1 主 API，使用 `--profile app --profile mockcorp`，主 API 在 8000，MockCorp 在 8001。
切换 shell 凭据后，仅重建目标服务：`docker compose --profile mockcorp up -d --no-deps --wait mockcorp`。

token 仅保存在当前 shell，服务端配置只包含 SHA-256 摘要。新窗口需重新配置 token 和摘要映射，
或自行保存至不入库的 `.env`。每个凭据绑定服务端 tenant；请求不能自报 tenant/role。
MockCorp 将凭据视为该租户的内部系统操作权限，不按角色调用 OPA；Agent 权限仍由主 API 策略层负责。
所有 `/v1/*` 读写都需要 Bearer token，缺失/无效返回 401；健康检查无需认证。

自动运行验收（会在该模拟租户创建演示数据）：

```powershell
$env:MOCKCORP_DEMO_TOKEN = $token
uv run python -m scripts.w2_mockcorp_demo
```

脚本逐一验证 13 操作的预演、执行和重放，另验证 429、key 冲突、207 部分成功补做失败项、
提交后 504 的安全重放。每次完整重演建议使用新 tenant，避免触发累计席位/余额限制。

## curl 验收

继续在同一 PowerShell 窗口运行。JSON 从 stdin 传给 curl.exe，避免 Windows 参数引号差异。

```powershell
$base = "http://127.0.0.1:8001"
$name = "demo_" + [guid]::NewGuid().ToString("N")
$body = @{ name = $name } | ConvertTo-Json -Compress
$key = "repo-" + [guid]::NewGuid().ToString("N")

# 预演：200，dry_run=true，返回 diff，查询不会看到该仓库
$body | curl.exe --silent --show-error -i "$base/v1/code/repos?dry_run=true" -H "Authorization: Bearer $token" -H "Content-Type: application/json" --data-binary "@-"
# 正式执行：201，Idempotency-Replayed: false
$body | curl.exe --silent --show-error -i "$base/v1/code/repos" -H "Authorization: Bearer $token" -H "Idempotency-Key: $key" -H "Content-Type: application/json" --data-binary "@-"
# 重放：201，同一资源 ID 和 JSON，Idempotency-Replayed: true
$body | curl.exe --silent --show-error -i "$base/v1/code/repos" -H "Authorization: Bearer $token" -H "Idempotency-Key: $key" -H "Content-Type: application/json" --data-binary "@-"

$body = @{ name = "rate_" + [guid]::NewGuid().ToString("N") } | ConvertTo-Json -Compress
$key = "rate-" + [guid]::NewGuid().ToString("N")
# 429 + Retry-After: 1，无落地
$body | curl.exe --silent --show-error -i "$base/v1/code/repos?fault=429" -H "Authorization: Bearer $token" -H "Idempotency-Key: $key" -H "Content-Type: application/json" --data-binary "@-"
# 去掉 fault、保留同 key：201，成功一次
$body | curl.exe --silent --show-error -i "$base/v1/code/repos" -H "Authorization: Bearer $token" -H "Idempotency-Key: $key" -H "Content-Type: application/json" --data-binary "@-"
```

## 写接口

下表路径均以 `/v1` 开头。正式写入必须带 `Idempotency-Key`；所有接口支持 `?dry_run=true` 和 `?fault=<name>`。
字段都在请求体顶层，额外字段返回 422。ID 为创建结果中的 `result.id`。

| 系统 | 方法与路径 | JSON 请求体 | 正常状态 |
|---|---|---|---|
| Identity | POST `/identity/users` | `username, display_name` | 201 |
| Identity | POST `/identity/mailboxes` | `user_id, email` | 201 |
| Identity | POST `/identity/users/{user_id}/password` | `new_password`（12–128 字符） | 200 |
| Identity | POST `/identity/users/{user_id}/disable` | `{}`（必须传空对象） | 200 |
| Code | POST `/code/repos` | `name` | 201 |
| Code | POST `/code/repos/{repo_id}/members` | `user_id, permission?`（默认 read） | 201 |
| Code | PATCH `/code/repos/{repo_id}/members/{user_id}` | `permission`（read/write/admin） | 200 |
| Ticket | POST `/ticket/projects` | `key, name`（key：2–16 位大写字母/数字，以字母开头） | 201 |
| Ticket | POST `/ticket/tickets` | `project_id, title, assignee_id?` | 201 |
| Ticket | POST `/ticket/tickets/assign` | `ticket_ids`（1–100 个 ID）, `user_id` | 200 / 207 |
| Money | POST `/money/orders` | `item, quantity, unit_price_cents` | 201 |
| Money | POST `/money/seats` | `order_id, user_id` | 201 |
| Money | POST `/money/refunds` | `order_id, amount_cents` | 201 |

成功响应：`{operation, dry_run, result, diff}`。每条 diff 包含 `collection, before, after`，资源变更另含 `id`。
新增资源 before 为 null。预演成功统一返回 200，执行返回表中状态码。
业务/故障/数据库错误：`{"error":{"code":"...","retryable":false}}`；
认证、参数校验、查询未找到沿用 FastAPI 的 `detail` 格式。数据库不可用为 503。

业务约束：用户名、邮箱、仓库名不区分大小写；用户须启用才能建邮箱、获邀或绑定席位；
Code 邀请要求已有有效邮箱，席位上限按租户内不同成员计数。
金额均为正整数分，数量 1–1000；订单即时扣款，禁止透支/超额退款；
有退款的订单不能新绑定席位，全额退款会停用该订单席位。禁用用户也会停用其邮箱。
密码操作只递增 `password_version`，不存储原文或实现真实密码认证。

## 查询与事务语义

- GET `/v1/state`：当前租户全部模拟状态、余额和规则，不含幂等收据或密码。
- GET `/v1/{system}/{collection}`：`{"items":[...]}`。
- GET `/v1/{system}/{collection}/{resource_id}`：资源对象，不存在返回 404。
- GET `/v1/oplog?after_id=0&limit=100`：本租户**操作日志**，按 `id` 升序。
  返回 `{"items":[...], "next_after_id": <id 或 null>}`；`next_after_id` 非空时表示还有下一页，
  拿它当新的 `after_id` 继续拉即可。设计理由见 [ADR-0004](design-decisions/0004-mockcorp-oplog.md)。
- collection：identity 下 users/mailboxes；code 下 repos/members；ticket 下 projects/tickets；money 下 orders/seats/refunds。
- 数据与幂等收据在 PostgreSQL 同一事务提交，同租户写入串行；跨进程/重启仍可重放。
- key 在租户内全局唯一，允许 1–200 个字母、数字、`.`、`_`、`:`、`-`；正式写入缺 key 返回 400。
  同 key 同操作及参数返回首次已提交状态码与 JSON，头 `Idempotency-Replayed: true`；改变参数或操作返回 409。
  请求摘要包含路径参数及校验后补齐默认值的请求体，不包含 fault。
- dry-run 执行相同业务校验并返回拟变更，不写业务、收据或租户初始化，不占用 key，也不读取旧收据。
  预演资源 ID 不预留，正式调用可能不同；其他请求也可能使预演结果过期。
- 输入错误、业务拒绝和提交前故障不缓存；修正后可用同 key 重试。
- 207 与成功部分一同持久化；同 key 重放仍返回原 207，不会继续执行失败项。
  应仅取 result.items 中 status=failed 的 ID，使用新 key 重新分配。

## 故障注入

默认关闭；关闭时指定 fault 返回 403。未知故障或不适用的操作组合返回 422。
已提交收据的重放优先于新指定的 fault，不会再触发故障。

| fault | 适用操作 | 行为 |
|---|---|---|
| `500` | 全部写操作 | 提交前 500，可重试 |
| `429` | 全部写操作 | 提交前 429，Retry-After: 1 |
| `timeout` | 全部写操作 | 等待后 504，无落地 |
| `slow` | 全部写操作 | 等待后正常成功，重放不等待 |
| `timeout_after_commit` | 全部正式写操作 | 已提交后等待并返回 504；同 key 重试恢复原成功结果；dry-run 不支持 |
| `duplicate_name` | 建用户 | 409 |
| `domain_policy` | 建邮箱 | 403 |
| `seat_limit` | 邀成员 | 409 |
| `invite_rate_limit` | 邀成员 | 429，Retry-After: 1 |
| `insufficient_funds` | 下单 | 409 |
| `approval_timeout` | Money 三个写操作 | 等待后 504，无落地 |
| `partial_success` | 批量分配至少两项 | 前半项成功，其余失败；正式调用 207；预演 200 且不落地 |

timeout 是 HTTP 层的模拟 504；真实客户端等待超时/服务进程强杀后的恢复另外由集成测试覆盖。

## 配置与验证

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MOCKCORP_PORT` | 8001 | Compose 本机绑定端口 |
| `MOCKCORP_FAULTS_ENABLED` | false | 显式启用故障 |
| `MOCKCORP_FAULT_DELAY_S` | 1 | slow/timeout 等等待秒数，0–10 |
| `MOCKCORP_INITIAL_BALANCE_CENTS` | 100000 | 租户初始余额 |
| `MOCKCORP_CODE_SEAT_LIMIT` | 3 | Code 组织席位上限 |
| `MOCKCORP_ALLOWED_EMAIL_DOMAINS` | `["example.com","mockcorp.test"]` | 允许邮箱域的 JSON 数组 |

初始余额、域规则和席位上限仅在新租户首次写入时固化，改环境变量不会重置已有租户。
没有公开 reset 接口；演示使用新 tenant 隔离。不做生产吞吐/收据淘汰承诺，取舍见
[ADR-0003](design-decisions/0003-mockcorp-transactions.md)。结束故障演示后可关闭故障：

```powershell
$env:MOCKCORP_FAULTS_ENABLED = "false"
docker compose --profile mockcorp up -d --no-deps --wait mockcorp
```

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -m "not integration" -q
# 真实 PostgreSQL 集成测试：先迁移，并将 DATABASE_URL 指向专用测试库
uv run alembic upgrade head
uv run pytest -m integration -q
```

集成测试包含并发同 key 单次扣款、不同 key 防透支、事务回滚、部分成功持久化，
以及真实 HTTP 超时后强杀/重启服务再重放；迁移往返在临时数据库及 CI 验证。
