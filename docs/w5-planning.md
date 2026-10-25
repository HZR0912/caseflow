# W5：规划、预演与验收

入口：`POST /v1/plans/preview`。实际 LangGraph 路径：

```text
understand → retrieve → plan → authorize → dry_run → END
```

成功返回 `awaiting_approval`：计划已预演，尚未审批和执行。当前支持入职准备，
每人生成账号、邮箱、仓库邀请、入职工单四步；三人为 12 步 DAG。
每步包含依赖、风险、回滚限制和制度证据。缺信息会追问，不猜账号拼音和资源 ID。

## 1. 两分钟演示（无需 Docker 或模型密钥）

在仓库根目录 PowerShell 执行：

```powershell
$env:PYTHONUTF8 = '1'
uv sync --frozen --extra dev
uv run python -m scripts.w5_plan_demo
Start-Process .\tmp\w5-demo.html
Start-Process .\tmp\w5-demo-conflict.html
```

脚本依次演示以下场景，任何断言失败会以非零状态退出。页面展示依赖图、风险和回滚限制，
展开底部可查看制度引文、逐步授权结果和预演 diff。完整输入采用固定日期 2026-10-24，
便于复现“下周一”和“12 月底”。

| 场景 | 预期状态 | 应检查什么 |
|---|---|---|
| success | awaiting_approval | 12 步均 ok，经过全部五个节点 |
| clarify | clarify | 只经过 understand，列出缺失名单、域和资源信息 |
| conflict | clarify | 邮箱重复；该分支后续 skipped，其他员工可继续预演 |
| denied | denied | 实习生身份被拒，不调用预演 |
| injection | blocked | 注入请求被拦，不生成计划 |

每次都会比较预演前后业务快照，必须完全相同。离线模式的 OPA 是明确标注的测试替身，
MockCorp 使用真实业务逻辑和 ASGI HTTP 入口、内存沙箱；这不等同于真实服务连通验收。

单独运行或只做断言：

```powershell
uv run python -m scripts.w5_plan_demo --scenario conflict
uv run python -m scripts.w5_plan_demo --check
```

## 2. 真实 OPA + PostgreSQL 验收

先配置 `.env` 的 DATABASE_URL、OPA_URL（默认 localhost 开发服务），然后：

```powershell
docker compose up -d postgres opa
uv run alembic upgrade head
uv run python -m scripts.w5_plan_demo --live --check
```

仍应得到以上五种结果和“W5 验收通过”。真实 OPA 参与每个动作授权；真实 PostgreSQL
验证整批事务回滚和 `plan.preview` 操作日志。脚本创建独立随机演示租户，准备仓库和项目
等测试数据，结束后清理该租户。MockCorp HTTP 通过进程内 ASGI transport 调用，
该命令不验证网络部署的主 API/MockCorp；下一节提供真实 HTTP 操作。

## 3. 从主 API 发起真实 HTTP 请求

此示例会在独立演示租户中**正式创建一个仓库和一个工单项目作为前置数据**；规划预演
不增加账号、邮箱、成员或工单。使用 admin 凭据，README 快速开始的 intern 凭据会被拒。
两个服务需要使用相同的凭据映射。

```powershell
$token = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
$sha256 = [Security.Cryptography.SHA256]::Create()
$tokenHash = -join ($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($token)) | ForEach-Object { $_.ToString('x2') })
$sha256.Dispose()
$env:AUTH_CREDENTIALS = ConvertTo-Json -Compress -InputObject @(
    @{ token_sha256 = $tokenHash; user_id = 'w5-operator'; role = 'admin'; tenant = 'w5-http-' + [guid]::NewGuid().ToString('N') }
)
$env:PLANNING_PROVIDERS = '[]'
docker compose --profile app --profile mockcorp up -d --build --wait
$headers = @{ Authorization = "Bearer $token" }

function Send-Json($url, $payload, $requestHeaders) {
    $json = $payload | ConvertTo-Json -Depth 20 -Compress
    Invoke-RestMethod $url -Method Post -Headers $requestHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}
$seedHeaders = @{ Authorization = "Bearer $token"; 'Idempotency-Key' = 'w5-repo' }
$repo = Send-Json 'http://127.0.0.1:8001/v1/code/repos' @{ name = 'frontend' } $seedHeaders
$seedHeaders['Idempotency-Key'] = 'w5-project'
$project = Send-Json 'http://127.0.0.1:8001/v1/ticket/projects' @{ key = 'ONBOARD'; name = '入职准备' } $seedHeaders
$request = @{
    text = '下周一来3个实习生，做前端的，权限给到12月底，请准备入职环境'
    today = '2026-10-24'
    facts = @{
        employees = @(
            @{ name = '张三'; username = 'zhangsan' }
            @{ name = '李四'; username = 'lisi' }
            @{ name = '王五'; username = 'wangwu' }
        )
        role = '前端'; start_date = '2026-10-26'; end_date = '2026-12-31'
        email_domain = 'example.com'; permission = 'read'
        repo_id = $repo.result.id; project_id = $project.result.id
    }
}
$result = Send-Json 'http://127.0.0.1:8000/v1/plans/preview' $request $headers
$result.status                       # awaiting_approval
$result.trace                        # 五个节点
$result.plan.steps.Count             # 12
$result.preview.steps | Format-Table step_id,status
$result | ConvertTo-Json -Depth 30    # 证据、授权、依赖、diff
```

API 用 HTTP 200 承载工作流结果，需要读取 `status`：`clarify` 时补齐 `facts` 后重新提交；
`denied` 表示 OPA 拒绝；`preview_unavailable` 表示服务不可用或响应不完整；
`human_review` 表示不支持或无法可靠处理；`evidence_only` 为制度原文检索。
未认证为 401，非法请求为 422。不要把响应中的脱敏占位符当作真实员工字段提交。

## 4. 自动化测试

```powershell
uv run pytest tests/test_planning.py tests/test_planning_api.py -q
uv run pytest -m 'not integration' -q
uv run ruff check .
uv run ruff format --check .
```

PostgreSQL 专项测试需显式设置环境变量（pytest 不自动读取 `.env` 中的 DATABASE_URL）：

```powershell
# 读取本地配置到进程环境，不打印数据库凭据
$env:DATABASE_URL = uv run python -c 'from apps.api.settings import get_settings; print(get_settings().database_url)'
uv run alembic upgrade head
uv run pytest tests/test_planning_integration.py -q
```

预期两项均通过：3 个席位成功、2 个席位冲突。两种情况都不留下用户、邮箱、成员、工单
或预演幂等收据，只保留 dry-run 操作日志；测试自行清理其随机租户。
CI 已加入五场景自检，真实 PostgreSQL 测试由现有 integration job 收集。

## 范围与下一周衔接

- 默认规则提取和模板规划，无模型调用。API 可通过 `PLANNING_PROVIDERS` 按序配置
  Qwen/DeepSeek 的 chat/completions 兼容接口，示例见 `.env.example`。模型仅补充有原文
  依据的缺失槽位，不改已确认字段，不选择任意工具。失败依次降级，最后返回规则结果。
  供应商调用、脱敏和降级已用模拟响应测试，未验证真实付费模型效果。
- 默认读取本地制度库，检索片段经过注入扫描和原文偏移校验；返回证据和模板计划，
  尚未实现自由生成的制度问答或语义 rerank。规则检测仍有 W4 已记录的漏检。
- 日期写入计划约束；MockCorp 尚无到期自动回收。仓库移除成员、删除工单也未实现，
  计划明确列出回滚限制。
- HTTP 每次重新构图；checkpoint 测试验证状态脱敏，不承诺 W5 会话跨进程恢复。
  `awaiting_approval` 是终态标记；审批 interrupt/恢复是 W6，正式执行是 W7。

设计依据：[ADR-0007](design-decisions/0007-understand-plan-preview.md)。
