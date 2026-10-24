# CaseFlow（事务官）

[![CI](https://github.com/HZR0912/caseflow/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/HZR0912/caseflow/actions/workflows/ci.yml)

面向企业跨系统办事场景的 Agent：**听懂需求 → 查制度 → 做计划 → 预演 → 等人审批 → 执行 → 失败回滚 → 留痕可审**。

定位不是 RAG 问答机器人，而是 **Agent 的写操作平面**：读操作人人会做，写操作（可逆性、幂等、权限、审批、追溯、部分成功语义）才是工业界的门槛。

完整方案见 `../CaseFlow-企业办事Agent-项目设计方案.md`。

## 当前状态

**W1（骨架周）已完成；W2（MockCorp）已完成；W3（制度知识库）已完成；W4（输入护栏）已完成。**

W2 实现 MockCorp 四个系统、13 个写操作，支持 dry-run、PostgreSQL 持久化幂等、
故障注入与供给方独立操作日志（ADR-0004）。启动与验收见 [MockCorp API](docs/mockcorp-api.md)。

W3 实现制度知识库：按条款切块（`char_start/char_end` 可逐字回原文）、
pgvector 向量 + Postgres 全文两路召回、RRF 融合、**跨版本冲突解析**、
引用程序化校验。设计取舍见 [ADR-0005](docs/design-decisions/0005-policy-rag.md)。

W4 实现输入护栏：归一化抗变形（零宽 / 空格插入 / 全角 / base64 展开）→
注入与参数注入检测（逐条给出规则、权重与原文偏移）→ PII 可逆 token 化
（原文进请求级 vault，不落盘）→ 意图置信度路由（route / clarify / human）→
槽位抽取与缺槽追问。越权与数据外泄**只打风险标签交 OPA**，护栏不做授权判断。
设计取舍见 [ADR-0006](docs/design-decisions/0006-input-guardrails.md)。

| 验收项 | 状态 | 证据 |
|---|---|---|
| W1：compose 四服务 healthy | ✅ | 不带 profile 时仅启动 postgres / redis / opa |
| W1：CI 绿（ruff + pytest + `opa test` + 迁移往返） | ✅ | 现为五个 job，含 `guardrails-eval` |
| W1：策略拒绝一次请求 | ✅ | intern 请求 `admin.grant` → 403 `self_escalation_forbidden` |
| W1：跨进程断点恢复 | ✅ | `scripts.w1_checkpoint_demo start` / `resume` 双进程验证 |
| W2：13 个写操作 × 预演/执行/重放/瞬态失败 | ✅ | `pytest -m "not integration"`，参数化覆盖每个操作 |
| W2：并发与强杀重启 | ✅ | 并发同 key 单次扣款、防透支、HTTP 超时后重启重放 |
| W2：供给方操作日志 | ✅ | `GET /v1/oplog`，含 dry-run / rejected / replayed 四态 |
| W3：检索 recall@5 = 1.000（16 用例） | ✅ | `python -m eval.run_rag_eval`，零外部依赖 |
| W3：版本冲突解析 4/4 | ✅ | 新旧版本并存时只答现行版本，旧版进 `superseded` |
| W3：引用可验证 | ✅ | 真引文返回原文偏移；编造数字与过短片段被拒 |
| W4：危险操作拦截率 1.000（60 条对抗集） | ✅ | `python -m eval.run_guardrails_eval`，CI 门禁 ≥ 95% |
| W4：误拦率 0.000（30 条正常集） | ✅ | 同上，门禁 < 5% |
| W4：留出集泛化 | ✅ | 首次 0.385 → 补规则后 1.000，数字与流程记在 ADR-0006 |

当前测试规模：**215 项非集成 + 14 项集成**（MockCorp 7 + RAG 7），
`opa test` 11 项，检索评测 16 用例，护栏评测 60 + 30 用例（另有留出集 25 + 10）。

> ⚠ **集成测试本地未验证**：本仓库的集成测试（`pytest -m integration`）需要一个真实
> Postgres。W2 交付时曾出现"README 写 13 项通过、实际 2 项失败"的情况——原因是
> 修改只跑了离线测试就更新了文档。现在**凡是改动涉及 SQL 的提交，必须在
> `DATABASE_URL` 指向真库的环境下跑一遍集成测试再更新这里的数字**；
> 没跑过就写"待 CI 验证"。CI 的 integration job 每次都会跑。

已知缺口：Agent 执行器仍返回 501，工具网关 / 自动重试 / 补偿在 W7–W8；
记忆、MCP 工具、审批台、Trace 回放、评测 harness、LLM 生成与 rerank 尚未实现。
RAG 目前只有"检索"，还没有"生成答案"——生成与引用标注在 W5 接入编排时一起做。
护栏是规则实现（语义级改写会漏），LLM 分类器作为降级链的替换实现留到 W5；
检索结果里的**间接注入**尚未覆盖，W5 接 RAG 时需要独立的扫描路径。
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

输入护栏（W4 验收项之一，**不需要 Docker、数据库或 API key**）：

```bash
uv run python -m scripts.w4_guardrails_demo          # 八个场景逐层打印
uv run python -m eval.run_guardrails_eval            # 拦截率 / 误拦率，CI 同款门禁
uv run python -m eval.run_guardrails_eval --cases data/guardrails/cases_holdout.yaml
```

HTTP 入口（服务启动后）：

```powershell
$body = @{ text = "下周一来 3 个实习生，做前端的，权限给到 6 月底"; today = "2026-10-24" } | ConvertTo-Json -Compress
Invoke-RestMethod http://127.0.0.1:8000/v1/guardrails/check -Method Post -Headers $headers -ContentType "application/json" -Body $body
# disposition=clarify，slots.missing=["name"]，追问只问姓名
```

`masked_text` 是脱敏后的文本；PII 真实值只在进程内存的 vault 里，不会出现在响应中。

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
