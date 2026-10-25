# CaseFlow（事务官）

[![CI](https://github.com/HZR0912/caseflow/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/HZR0912/caseflow/actions/workflows/ci.yml)

面向企业跨系统办事场景的 Agent：**听懂需求 → 查制度 → 做计划 → 预演 → 等人审批 → 执行 → 失败回滚 → 留痕可审**。

定位不是 RAG 问答机器人，而是 **Agent 的写操作平面**：读操作人人会做，写操作（可逆性、幂等、权限、审批、追溯、部分成功语义）才是工业界的门槛。

完整方案见 `../CaseFlow-企业办事Agent-项目设计方案.md`。

## 当前状态

**W1–W9 已实现：规划、持久审批、正式执行、幂等重放、重试、分组 Saga、三类记忆与组织模型。**

W9 新增加密会话恢复、显式个人偏好、部门/导师/系统归属及历史办理摘要。
记忆可选择、修正、过期和遗忘；来源随审批冻结，不能绕过权限与人工审批。
**使用与白话说明见 [W9 记忆](docs/w9-memory.md)，实测见 [W9 验证报告](docs/w9-verification.md)**。
运行 uv run python -m scripts.w9_memory_demo --serve 可启动独立演示。

W8 失败后倒序补偿本组成功步骤，保留组外成果；支持退款/解绑/撤回成员和批次成功项恢复。
补偿进度、幂等键与重试预算持久化，未知结果或状态冲突转人工处理。
**启动见 [W8 Saga](docs/w8-saga.md)，实测见 [W8 验证报告](docs/w8-verification.md)**。
`uv run python -m scripts.w8_saga_demo --serve` 可启动余额不足补偿演示。

W6 将预演结果冻结为审批单，使用 PostgreSQL checkpoint 等待人工决定；支持
强杀进程后恢复、批准/拒绝、幂等重试与并发保护。审批通过停在 `approved_ready`；
W7 可从审批台显式执行，结果单独保存在 `execution.status`。执行参数加密保存，
调用前重新授权；重启继续沿用原键和尝试预算。
**启动与验收见 [W7 执行器](docs/w7-execution.md)**；
`uv run python -m scripts.w7_execution_demo --serve` 可启动交互演示。

W5 将护栏、制度证据、入职 DAG、逐步 OPA 授权和批量预演串起来，三人入职生成 12 步。
独立 W5 入口成功停在 `awaiting_approval`；审批入口接 W6 持久化审批与 W7 显式执行。
**演示和测试步骤见 [W5 规划与预演](docs/w5-planning.md)**；运行
`uv run python -m scripts.w5_plan_demo` 即可生成可视化页面。

W2 实现 MockCorp 四个系统、13 个写操作，支持 dry-run、PostgreSQL 持久化幂等、
故障注入与供给方独立操作日志（ADR-0004）。启动与验收见 [MockCorp API](docs/mockcorp-api.md)。

W3 实现制度知识库：按条款切块（`char_start/char_end` 可逐字回原文）、
pgvector 向量 + Postgres 全文两路召回、RRF 融合、**跨版本冲突解析**、
引用程序化校验。设计取舍见 [ADR-0005](docs/design-decisions/0005-policy-rag.md)。

W4 实现输入护栏：归一化抗变形（零宽 / 空格插入 / 全角 / base64 展开）→
注入与参数注入检测（逐条给出规则、权重与原文偏移）→ PII 可逆 token 化
（原文进请求级 vault，不落盘）→ 意图置信度路由（route / clarify / human）→
槽位抽取与缺槽追问。越权与数据外泄当前**只输出风险标签与 policy_action，不调用 OPA，
也不在护栏中拦截**；W5 新增的 `/v1/plans/preview` 已在编排层调用 OPA。
设计取舍见 [ADR-0006](docs/design-decisions/0006-input-guardrails.md)。

| 验收项 | 状态 | 证据 |
|---|---|---|
| W1：compose 四服务 healthy | ✅ | 不带 profile 时仅启动 postgres / redis / opa |
| W1：CI 检查配置（ruff + pytest + `opa test` + 迁移往返） | ✅ | 现为六个 job，含审批 UI；本轮远程 CI 尚未运行 |
| W1：策略拒绝一次请求 | ✅ | intern 请求 `admin.grant` → 403 `self_escalation_forbidden` |
| W1：跨进程断点恢复 | ✅ | `scripts.w1_checkpoint_demo start` / `resume` 双进程验证 |
| W2：13 个写操作 × 预演/执行/重放/瞬态失败 | ✅ | `pytest -m "not integration"`，参数化覆盖每个操作 |
| W2：并发与强杀重启 | ✅ | 并发同 key 单次扣款、防透支、HTTP 超时后重启重放 |
| W2：供给方操作日志 | ✅ | `GET /v1/oplog`，含 dry-run / rejected / replayed 四态 |
| W3：检索 recall@5 = 1.000（16 用例） | ✅ | `python -m eval.run_rag_eval`，零外部依赖 |
| W3：版本冲突解析 4/4 | ✅ | 新旧版本并存时只答现行版本，旧版进 `superseded` |
| W3：引用可验证 | ✅ | 真引文返回原文偏移；编造数字与过短片段被拒 |
| W4：注入拦截率 1.000（调参集内应拦截的 24 条） | ✅ | 调参集共 60 条对抗请求，越权/外泄类只检查标签；CI 门禁 ≥ 95% |
| W4：误拦率 0.000（30 条正常集） | ✅ | 同上，门禁 < 5% |
| W4：新留出集 v2 | ⚠️ | 首次拦截 10/13（0.769），正常请求误拦 0/15；保留 3 条漏检，见 [实测记录](docs/benchmark.md) |
| W4：越权授权判断 | 独立护栏仅打标签 | `/v1/guardrails/check` 不调用 OPA；`route`/`clarify` 不表示获得授权 |
| W5：一句话生成 12 步 DAG | ✅ | 显式名单与资源信息齐全后，三人各四步，含依赖、风险、回滚限制 |
| W5：OPA 与批量 dry-run | ✅ | 五场景自检；真实 PostgreSQL 证明业务回滚，OPA 拒绝会停止预演 |
| W5：缺信息与资源冲突追问 | ✅ | `clarify` 返回问题，失败步骤的后继跳过 |
| W6：强杀恢复 | ✅ | 新 API 进程读取相同 checkpoint，从 approval 节点继续；不重跑预演 |
| W6：审批台与授权 | ✅ | Vue3 展示影响、diff、制度证据、回滚限制；禁止自审批与跨租户访问 |
| W6：并发与崩溃窗口 | ✅ | 单一决定；快照/决定/图进度各提交边界均有恢复测试 |
| W7：稳定幂等键与执行恢复 | ✅ | 远端提交后强杀 API，原键恢复；12 步完成、重放无重复 |
| W7：重试与失败分类 | ✅ | 429 退避、Retry-After、累计预算、需人工/待补偿状态；供应方日志核对 30 次实际提交 |
| W7：加密参数与执行台 | ✅ | 密文绑定审批；逐步当前授权；浏览器执行与刷新、桌面/移动端验收 |
| W8：分组补偿与退款恢复 | ✅ | 余额不足精确撤回；逆序解绑/退款/撤回；退款提交后强杀，原键恢复 |
| W8：部分成功与人工处理 | ✅ | 207 逐项校验；只恢复成功项；状态变化、未知结果、预算耗尽均明确展示 |
| W9：会话与个人记忆 | ✅ | API 强杀恢复；显式确认、去重、衰减、版本、遗忘与用户隔离 |
| W9：组织与规划整合 | ✅ | 递归部门/导师/系统归属；三类来源补全计划并冻结审批；组织职位不授予权限 |
| W11：评测 harness 与对抗集 | ✅ | 60 业务 + 60 对抗，每例五次；连接池修复后默认并发 4，三轮各 600/600 |
| W11：基线门禁与报告 | ✅ | 完整性、真实收据、引用和基线比较；JSON/HTML 报告，CI 已配置；并发 8 两轮各 600/600 |

当前测试规模：**396 项非集成 + 78 项集成**，另有 16 项前端交互测试、14 项 Rego 测试，
检索评测 16 用例，护栏调参集 60 + 30、旧回归集 25 + 10、新留出集 25 + 15。

连接池修复后已在本机真实 PostgreSQL + pgvector 的独立临时数据库完成迁移与集成测试：
**78 passed，无跳过**。包括检索、预演、审批、执行、补偿、记忆、组织、并发和旧计划兼容，
以及连接复用、锁释放、取消、池满拒绝、坏连接丢弃和生命周期关闭。
测试后已删除临时数据库。云端 CI 尚待提交后验证；涉及 SQL 的后续改动仍须跑真实数据库测试。

已知缺口：补偿人工终态尚无自动对账/结案接口；长等待仍需显式恢复。
MCP 工具、自由问答生成与 rerank 尚未实现。Trace 回放和规则评测 harness 已实现。
记忆当前是显式结构化实现，使用哈希向量；自由对话自动长期抽取、HR 同步和后台清理尚未实现。
W7 需稳定的 `EXECUTION_ENCRYPTION_KEY`；旧审批没有原始执行参数，必须重新预演审批。
W5 默认本地制度检索并返回经原文校验的引文，规划采用入职模板；可选模型只补充缺失槽位。
护栏是规则实现（语义级改写会漏）；W5 已对检索片段单独扫描间接注入，不能保证全拦截。
MockCorp 的供应方原子幂等已接入执行器；长 Retry-After 或中断后需再次显式恢复。

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
旧 W1 `/v1/actions/execute` 策略探针：拒绝返回 **403**，允许返回 **501** 并提示新入口。
正式执行使用 `POST /v1/approval-requests/{id}/execute`，必须已有冻结且批准的计划。

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

输入护栏（W4 验收项之一，**不需要 Docker、数据库或 API key**）：

```bash
uv run python -m scripts.w4_guardrails_demo          # 八个场景逐层打印
uv run python -m eval.run_guardrails_eval            # 拦截率 / 误拦率，CI 同款门禁
uv run python -m eval.run_guardrails_eval --cases data/guardrails/cases_regression_v1.yaml
uv run python -m eval.run_guardrails_eval --cases data/guardrails/cases_holdout.yaml --report-only
```

HTTP 入口（服务启动后）：

```powershell
$body = @{ text = "下周一来 3 个实习生，做前端的，权限给到 6 月底"; today = "2026-10-24" } | ConvertTo-Json -Compress
Invoke-RestMethod http://127.0.0.1:8000/v1/guardrails/check -Method Post -Headers $headers -ContentType "application/json" -Body $body
# disposition=clarify，slots.missing=["name"]，追问只问姓名
```

`masked_text` 是脱敏后的文本；PII 真实值只在进程内存的 vault 里，不会出现在响应中。

本轮修复的复验命令、预期结果、索引重建要求见 [W4 修复验收](docs/w4-fixes-verification.md)。

## W11：评测与门禁

```powershell
uv run python -m eval.runner --output eval/results/w11-current.json
```

60 业务 + 60 对抗，每例五次，共 600 次。以真实 PostgreSQL 状态、供应方操作日志、
审批与引用原文判分；版本基线比较失败会返回非零退出码，CI 保存 JSON/JSONL/HTML。
这是已知内部规则回归，不代表真实模型效果。用法见 [评测说明](docs/w11-evaluation.md)，
实测数字、修复记录及边界见 [W11 验证报告](docs/w11-verification.md)。
并发数据库连接修复已完成本机复验：并发 4 三轮、并发 8 两轮，共 **3000/3000**。
并发 4 每轮建连由 5562 次降为 30 次；证据与限制见
[数据库并发验证报告](docs/database-concurrency-verification.md)。

## 目录

W10 已增加 OTel / Langfuse 和 `/traces` 只读回放页面。启动方式见
[可观测性说明](docs/w10-observability.md)，实测证据见
[W10 验证报告](docs/w10-verification.md)。

```
apps/api/          FastAPI 主服务
apps/mockcorp/     Mock 企业系统（W2）
apps/web/          Vue3 + TypeScript 审批台（W6）
core/guardrails/   输入护栏（W4）
core/graph/        LangGraph 状态机、状态定义、子图
core/memory/       三类记忆（W9）
core/rag/          检索 / 融合 / 精排 / 版本冲突（W3）
core/tools/        W7 执行器、HTTP 网关、加密计划、幂等重试与 W8 Saga
core/policy/       OPA 客户端与策略调用
core/obs/          OTel、成本计量（W10）
eval/              场景 DSL、runner、确定性断言与基线门禁（W11）
migrations/        Alembic
policies/          Rego 策略（OPA）
scripts/           一次性验证脚本
docs/              架构与 ADR
```

## 开发约定

- **小步提交**：每完成一个可验证的小步骤就 commit 一次，随时可以 `git reset --hard <commit>` 回到任意历史版本。
- **ADR 先行**：关键取舍先写 `docs/design-decisions/` 再写代码。
- **数字不提前编**：所有指标做完实测后填入 `docs/benchmark.md`。
