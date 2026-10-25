# W7 执行器：幂等键、重试、失败分类与验收

> 本文保留 W7 阶段语义与当时的测试基线。W8 新计划已启用分组补偿，详见
> [W8 说明](w8-saga.md) 与 [当前验证报告](w8-verification.md)。W7 回归演示的
> 席位不足场景现返回 partial_success：六个身份步骤保留，失败组无已完成操作需要撤销。

W7 将 W6 的 `approved_ready` 接到正式工具调用。三人入职的 12 步会创建真实的
MockCorp 账号、邮箱、仓库成员及工单；审批和执行仍是两个显式动作。

## 1. 一条命令自动验收

在项目根目录 PowerShell 中执行：

```powershell
Set-Location 'D:\AI item\caseflow'
$env:PYTHONUTF8 = '1'
uv sync --frozen --extra dev
docker compose up -d postgres opa
uv run python -m scripts.w7_execution_demo --check
```

脚本使用 `.env` 中的数据库和 OPA 连接配置，创建独立随机数据库、运行迁移、启动
独立端口的 API 与 MockCorp、配置临时凭据和加密密钥。结束时只移除本次数据库与进程，
不修改已有业务数据。需要本地开发数据库账号具备 CREATE DATABASE 权限。

自动验收的关键观察点：

| 场景 | 检查内容 |
|---|---|
| 未批准 / 无执行权限 / 跨租户 | 分别返回 409 / 403 / 404，不执行 |
| 首个开户连续两次 429 | 第三次成功；每次沿用同一个 key；实际调用间隔满足 Retry-After |
| 邮箱提交后响应丢失 | 相同 key 重放得到原收据；只产生一次业务变更 |
| 完成后重复 POST execute | 返回原执行结果，不再调用供应方 |
| 供应方开户已提交，本地尚未记录结果时强杀 API | 新 PID 读到 running 意图，用原 key 恢复，12 步完成 |
| 仓库席位不足 | 6 步已完成、失败步只调用一次、后续步骤未开始，标记待补偿 |
| 供应方独立日志交叉核对 | 三个场景合计 30 次实际提交，无重复副作用 |

输出报告：`tmp/w7-check.json`。其中保存逐步 key、尝试次数、开始时间、退避时长、
重放标志和进程 PID；不保存 Bearer、加密密钥或员工原始参数。

## 2. 审批台交互与浏览器检查

```powershell
Push-Location apps/web
npm ci
npm test
npm run build
Pop-Location
uv run python -m scripts.w7_execution_demo --serve
```

打开终端显示的 `http://127.0.0.1:18000/approvals`，使用本次临时凭据：

1. 以申请人连接，查看冻结计划。
2. 切换审批人，点击“批准计划”。批准本身不执行。
3. 点击“执行已批准计划”。首次开户会经历两次 429，随后完成全部 12 步。
4. 查看执行结果表和“重试与执行记录”；首个开户尝试 3 次，首次邮箱尝试 2 次。
5. 刷新页面、重新连接，确认结果仍在且不再出现新的执行按钮。
6. Ctrl+C 结束并清理本次演示数据库。

可自动检查真实浏览器（本机 Windows 使用 Edge；Linux 需安装 Playwright Chromium）：

```powershell
uv run python -m scripts.w7_execution_demo --check --browser-check
```

检查点击执行、重试记录、刷新后持久化结果、桌面与 390px 移动端布局、JS 异常。
截图为 `tmp/w7-execution-desktop.png`、`tmp/w7-execution-mobile.png`。

## 3. 分层复验

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -m 'not integration' -q
uv run python -m scripts.verify_integration
docker compose exec -T opa /opa test /policies -v
```

`verify_integration` 在独立临时数据库完成 upgrade → downgrade → upgrade 和全部
真实数据库测试。没有 DATABASE_URL 时直接运行 pytest 导致集成测试跳过，不算验收。

仅运行 W7 专项：

```powershell
uv run pytest tests/test_execution.py -q
uv run python -m scripts.verify_integration tests/test_execution_integration.py -q
```

## 4. 接口与执行语义

`POST /v1/approval-requests/{id}/execute` 无需客户端传入工具参数或幂等键。
它只执行服务端已冻结并获得批准的计划；返回执行 journal，业务失败也用 HTTP 200
返回带失败状态的 journal。鉴权、未批准、过期、并发等前置拒绝使用 4xx/503。
`GET /v1/approval-requests/{id}` 的 `execution` 返回最新记录，`executable` 表示是否
具有加密执行参数。审批 `status` 保持历史批准结果，执行结果在 `execution.status`。

| 分类 / 状态 | 条件 | 行为 |
|---|---|---|
| retryable | 429、408、5xx、HTTP 传输错误 | 同 key 重试，默认初次 + 最多 3 次重试 |
| retry_waiting | Retry-After 尚未到期，且剩余等待大于 5 秒 | 保存时间并返回，稍后再次调用 execute；不会提前重试 |
| retry_exhausted | 4 次尝试用尽（跨重启累计） | 停止；重复调用只返回终态，不能重置预算 |
| human_required | 参数错误、401/403、幂等冲突、无效成功收据、权限撤销 | 不自动重试，不运行后续步骤 |
| compensation_required | domain_policy、duplicate_name、seat_limit、insufficient_funds 或业务 409 | 停止并列出已完成步骤，W8 才实现补偿 |
| succeeded | 每个步骤取得有效正式收据 | 保存供应方真实 ID，后续依赖引用该 ID，完成后不再调用工具 |

退避为 `min(8s, 0.5s × 2^(attempt-1)) × U(0.75, 1)`，并取 Retry-After 与退避的较大值。
支持 Retry-After 秒数和 HTTP 日期；超过一天转人工处理，不通过截断来提前重试。
演示的前两次 429 都带 Retry-After: 1，因此前两段实际等待均至少一秒。

调用意图和尝试计数在 HTTP 之前提交。进程被杀会消耗这次预算，恢复后复用原键。
最后一次尝试若在收据落库前中断，不会偷偷追加第五次调用，会保留
`outcome_unknown=true` 并转为 retry_exhausted，需人工核对供应方日志。
超时后即使随后遇到 401，也保留之前的未知结果；不能把“没收到响应”当成“没写入”。

幂等键由租户、审批 ID、冻结计划摘要、步骤 ID、操作名、解析后的目标参数确定。
供应方仍检查同 key 的请求指纹，业务状态、收据、提交日志原子保存。
同审批的审批/恢复/执行共用 PostgreSQL 会话 advisory lock；并发请求返回 409，
每次工具调用前重新向 OPA 检查动作与资源，管理员权限另查 `admin.grant`。

## 5. 普通开发环境的加密配置

W6 的脱敏占位符不能用于创建实体。W7 将原始可执行计划用 Fernet 认证加密保存到
审批表独立字段，并把计划摘要放入审批快照。密文还绑定租户和审批 ID。
checkpoint、响应、journal 只包含脱敏计划或真实资源 ID，不保存明文参数与凭据。

演示脚本自动管理本次临时密钥。普通开发环境应生成一次，并安全保存同一个值到本机
未跟踪的 `.env` 的 `EXECUTION_ENCRYPTION_KEY`，后续进程使用同一密钥。临时会话可这样设置：

```powershell
$env:EXECUTION_ENCRYPTION_KEY = uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
uv run alembic upgrade head
uv run python -m scripts.serve_api --port 18000
```

这段生成命令只运行一次；不要每次重启都生成新值。当前未实现多密钥轮换。
空密钥保留 W6 审批演示兼容性，只能创建不可执行的审批。配置密钥前生成的历史审批
需要重新预演并审批，不能从脱敏文本推测原始姓名和账号。密钥缺失/错误/密文篡改会阻止执行。

## 6. 本周边界

- 仅接入 W5 入职计划允许的四个工具；自由工具调用和 MCP server 尚未实现。
- 已完成步骤保留，失败不会自动回滚。`compensation_required` 仅标记需求；
  `compensation_executed=false`，不宣称 W8 的 Saga 已完成。
- 首次执行检查审批期限；已经开始的运行允许跨过审批期限恢复，仍检查当前调用者权限。
- 执行是显式 HTTP 驱动，未接后台队列；长 Retry-After 或进程中断后需点击恢复/再次 POST。
- 未实现计划中的定时开通/权限到期回收；时间字段仍按 W5 的既有能力说明处理。
- 零重复依赖供应方原子幂等契约；不能推广为任意第三方系统的 exactly-once 保证。
- 旧 `/v1/actions/execute` 是无审批参数的 W1 策略探针，仍返回 501 并提示使用新接口。

## 7. 实测记录

实测日期：2026-10-24（Asia/Shanghai）。最终结果见本节和 `tmp/w7-check.json`；
远程 CI 需提交后另行确认。

| 验证层 | 实测结果 |
|---|---|
| Ruff lint / format、git diff --check | 通过 |
| 非集成回归 | 318 passed（其中 W7 新增 28 项） |
| 真实 PostgreSQL 集成 | 43 passed，无跳过（其中 W7 新增 14 项） |
| 迁移往返 | upgrade → downgrade → upgrade 通过 |
| Vue 交互 / TypeScript / Vite | 6 项通过，构建通过 |
| OPA | 12/12 通过 |
| W7 真实 HTTP + 浏览器 | 16 组通过；供应方 30 次实际提交，无重复 |

强杀实例的旧/新 PID、逐步重试与供应方重放标志保存在 JSON 报告，
桌面/移动端截图保存在 `tmp`；验收进程和临时数据库均已清理。
