# W6：checkpoint、interrupt 与审批台

已接入 Vue 3 审批台和真实 PostgreSQL checkpoint。成功预演后冻结审批快照，进入
`review → approval(interrupt) → report`。批准后为 `approved_ready`，拒绝后为 `rejected`。
两种结果都不会执行正式工具写入。

W5 的理解、检索、规划、授权与预演仍使用原图；W6 持久化的是预演完成后的审批流程。
等待期间不保存原始员工信息或 Bearer token，恢复不重跑 W5。

## 1. 打开可以操作的审批台

在仓库根目录的 PowerShell 执行（需要 Docker、Python 3.12、uv、Node.js 22）：

```powershell
$env:PYTHONUTF8 = '1'
uv sync --frozen --extra dev
docker compose up -d postgres opa
docker compose restart opa
uv run alembic upgrade head
npm --prefix apps/web ci
npm --prefix apps/web run build
uv run python -m scripts.w6_approval_demo --serve
```

脚本会创建隔离的随机演示租户，准备仓库与工单项目，提交两份三人入职申请，
然后**强杀 API 进程并重启，确认原 checkpoint 仍在**。终端会显示：

- 审批台地址：`http://127.0.0.1:18000/approvals`。
- 本次临时申请人凭据和审批人凭据，两者属于同一租户、不同账号。

打开页面后按以下顺序操作：

1. 粘贴申请人凭据并连接，点选申请，检查 12 步、依赖、风险、回滚限制、预演 diff 和制度引文。
2. 确认页面提示“需要另一位有权限的审批人”，没有批准按钮。
3. 使用审批人凭据切换身份，选一份申请，点击“批准计划”。预期变为“审批通过 · 待执行”。
4. 选择另一份申请，先不填理由，确认拒绝按钮不可用；填写理由后拒绝，预期变为“已拒绝”。
5. 刷新页面、重新输入凭据，确认决定和审批人仍能查到。凭据不会放入浏览器持久存储。

终端 Ctrl+C 结束演示，自动停止本次服务并清理该随机租户的测试数据和 checkpoint。
脚本不会停止已有 8000/8001 端口的服务；18000 被占用时可加 `--port 18010`。

## 2. 一键验证强杀恢复

```powershell
uv run python -m scripts.w6_approval_demo --check
Get-Content .\tmp\w6-check.json
```

预期输出包含：

```text
[interrupt] ... next=['approval'] ...
[restart] API PID <旧进程> → <新进程> · checkpoint 未丢失
[approve] approved_ready · trace=['review', 'approval:approve', 'report']
[reject] rejected · 未执行
W6 验收通过
```

它会验证：同一 checkpoint 跨进程存在、批准/拒绝恢复、重复创建不重规划、重复决定幂等、
相反决定 409、自审批 403、无权限 403、跨租户 404、旧计划摘要 409、空拒绝理由 422。
两份申请只有两次预演日志；审批前后业务快照完全相同。正常结束时清理自己的随机租户。
若前端已构建，再检查审批台 HTTP 200。

真实浏览器验收（Windows 默认使用已安装的 Edge）：

```powershell
uv run python -m scripts.w6_approval_demo --check --browser-check
```

它会用浏览器连接真实服务，检查 12 行计划、展开引文与 diff、拒绝按钮约束、
桌面与手机宽度、JavaScript 错误，并生成：
`tmp/w6-approval-desktop.png`、`tmp/w6-approval-mobile.png`。
Linux/macOS 先在 apps/web 运行 `npx playwright install chromium`；需要时使用
环境变量 `W6_BROWSER_CHANNEL` 指定已有浏览器，例如 `msedge`。

## 3. 自动化回归与数据库迁移

```powershell
uv run pytest tests/test_approval.py -q
uv run pytest -m 'not integration' -q
npm --prefix apps/web test
npm --prefix apps/web run build
uv run ruff check .
uv run ruff format --check .
docker compose exec -T opa /opa test /policies -v
```

推荐在自动创建的独立临时数据库中跑集成测试，不影响本机业务库：

```powershell
# 全量集成：upgrade → downgrade → upgrade → pytest → 删除刚创建的数据库
uv run python -m scripts.verify_integration
# 只运行审批测试
uv run python -m scripts.verify_integration tests/test_approval_integration.py -q
```

使用 `.env` 中的 PostgreSQL 连接，需要开发数据库用户有 CREATE DATABASE 权限。
脚本只删除自己生成随机名字的数据库。CI 使用专属 PostgreSQL service。

审批集成测试覆盖：跨连接恢复、真实子进程强杀后新进程恢复、并发相反决定只有一个成功、
决策已提交但图未恢复、快照已提交但 checkpoint 尚未创建、过期、计划摘要不匹配、
角色撤销、租户隔离和自审批。前端测试验证展示、拒绝理由、重复网络提交与退出清理。

## 4. 常规服务与 API 使用

审批台与 API 同源，构建后由 `/approvals` 提供页面。
Windows 本地运行使用支持 psycopg 异步数据库连接的事件循环：

```powershell
uv run python -m scripts.serve_api --port 8000
```

不要在 Windows 为 W6 使用默认 Proactor 事件循环启动；专用启动器会选择 Selector。
Docker/Linux 可继续使用 uvicorn。Docker 镜像自动构建 Vue 页面：

```powershell
docker compose --profile app --profile mockcorp up -d --build --wait
uv run alembic upgrade head
```

沿用 W5 的 AUTH_CREDENTIALS 配置，但需要**同租户两个不同 user_id 的 admin**：一个申请、
一个审批。两个服务共享映射。生产身份系统尚未接入，这仍是开发静态凭据。

| 接口 | 用途 |
|---|---|
| POST `/v1/approval-requests` | 接收 W5 PlanningRequest；服务端预演后提交；要求 UUID `Idempotency-Key` |
| GET `/v1/approval-requests` | 当前租户最近 100 条；普通用户只能查自己的记录 |
| GET `/v1/approval-requests/identity` | 当前可信身份与审批能力 |
| GET `/v1/approval-requests/{id}` | 快照、摘要、审批记录、trace、next、interrupted、checkpoint_id |
| POST `/v1/approval-requests/{id}/decision` | 决定并恢复；不得由申请人调用 |
| POST `/v1/approval-requests/{id}/recover` | 从已保存的快照/决定修复进度，不接受任意 Command 或新决定 |

提交体与 [W5 HTTP 示例](w5-planning.md) 一样，用 `text/today/facts`；信息不足仍返回
`clarify`，不会创建审批单。每次申请发送一个 UUID 创建键，网络重试必须复用，修改输入
时必须换键。同键不同内容返回 409。

决定示例（`$requestId` 和 `$planHash` 取提交响应，`$reviewerToken` 是另一位审批人）：

```powershell
$headers = @{ Authorization = "Bearer $reviewerToken" }
$decision = @{
    decision = 'approve' # 或 reject；reject 要填写 reason
    reason = '已核对预演结果与回滚限制'
    plan_hash = $planHash
    operation_id = [guid]::NewGuid().ToString()
} | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8000/v1/approval-requests/$requestId/decision" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($decision))
```

网络重试复用整个 `$decision`，包括 operation_id。已记录的相同决定返回原结果；不同决定
返回 409。`approval_busy_retry` 表示竞争锁，稍后复用同一请求再试。拒绝后要改计划时，
重新预演并创建新申请。默认审批有效期 24 小时，可配置 APPROVAL_TTL_HOURS（1–168）。

## 5. 恢复机制与边界

审批单先保存脱敏快照，再建立 checkpoint；审批决定先持久化，再发送 `Command(resume)`。
二者不假装是同一事务。`recovery_needed=true` 表示需要恢复：相同决定重试或 recover
会补完剩余步骤。interrupt 节点恢复时从节点开头重入，因此 interrupt 前没有业务副作用。
每份审批用数据库会话锁串行处理，进程退出会释放锁。

approved_ready 只证明该快照获得批准。W7 仍需安全恢复真实实体，重新核对权限、日期和
资源状态；不能执行这里的脱敏占位符。当前只有普通审批记录，尚未接 W1 审计链表，
不声称已经实现防篡改审计。自由文本意见只做既有 PII 规则脱敏，不保证识别所有姓名。
W5 预演完成之前的崩溃需重新发起请求；本周恢复承诺覆盖的是预演后的审批阶段。

详细取舍：[ADR-0008](design-decisions/0008-durable-approval.md)。
