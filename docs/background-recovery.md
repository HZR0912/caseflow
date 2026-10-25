# 后台自动恢复

`POST /v1/approval-requests/{id}/execute` 仍然是唯一的执行授权入口。入口在同一把
审批请求锁内先写入 `background_jobs`，再调用供应方；因此 API 在第一次写入前被杀，
worker 也能从 PostgreSQL 恢复。没有点击执行的已审批计划不会被后台扫描。

这里的“第一次写入”指第一次供应方副作用；如果授权尚未成功写入 PostgreSQL，API
不会开始执行，后台也不会推断用户已经同意。审批单过期且尚未开始执行时仍然拒绝启动。

PostgreSQL 是任务的持久事实来源，Redis/Arq 只保存 request ID 的短期唤醒消息。
worker 每 2 秒扫描到期任务并补发消息，Redis 被清空或消息丢失也能恢复。多个 worker
由 `ApprovalService.session(request_id)` 的同一把 PostgreSQL advisory lock 互斥，供应方
幂等键和已有 journal 继续承担重放保护。

## 授权与失败

委托凭据使用现有执行加密密钥加密后存入 PostgreSQL；Redis、日志和状态 API 都不含
Bearer。worker 每次副作用前按当前 `AUTH_CREDENTIALS` 重新验证 token 摘要、用户、租户和
角色，并检查委托有效期。撤权、角色/租户变化、密文损坏和到期都会把任务标为
`blocked`，不会消耗业务步骤的重试预算；用户必须再次点击执行，用当前凭据重新授权。

供应方返回的 `Retry-After`、执行失败后的 `retry_waiting` 和补偿等待会被转成持久
`waiting` 任务；worker 在到期后继续，原步骤的最多四次业务尝试不会重置。终态、未知结果、
人工处理状态不会被后台无限循环。

OPA 临时不可用会按基础设施故障等待重试，不会被误当成业务失败或触发补偿；真正的
策略拒绝仍然阻止执行。基础设施重试最多 `BACKGROUND_MAX_FAILURES` 次（默认 6 次），
采用有上限的指数退避；委托有效期由 `BACKGROUND_AUTHORIZATION_HOURS` 控制（默认 24 小时）。
这些计数独立于每个业务步骤的四次尝试预算。任务终结或被阻断时清除密文凭据。

后台与手动执行、审批恢复、人工结案共用请求锁。人工结案会同时关闭后台任务，后续
执行入口也检查结案记录，不能因旧 Redis 消息而重新启动已结案流程。

## API 与运行

查询 `GET /v1/approval-requests/{id}/background` 返回 `job: null` 或状态、到期时间、
失败次数和无敏感信息的错误码。部署完整 app profile 时，API、MockCorp、Redis 和独立
worker 一起启动：

```powershell
uv run alembic upgrade head
docker compose --profile app up -d --build
```

手动运行 worker：

```powershell
uv run python -m core.jobs.worker
```

没有 `EXECUTION_ENCRYPTION_KEY` 时执行会明确返回 `execution_key_not_configured`，不会
创建可执行后台任务。

worker 每次验证使用新 `Settings()`，不复用 API 的配置缓存；直接修改本机 `.env` 的撤权
可被下一次后台动作观察到。容器环境变量属于进程配置，修改后必须重新创建 API、worker
和 MockCorp 实例；不会声称旧容器能自动看见外部环境变化。

## 验证范围

`uv run python -m scripts.verify_integration tests/test_background_integration.py -q` 在隔离
数据库执行升级→降级→升级并跑 19 项定向测试：授权前拒绝、等待期限、租户隔离、首条
journal 前中断、供应方提交后取消并重建服务、多个 worker/手动执行互斥、凭据撤销/角色及
身份变化/密文损坏/到期、新凭据重新授权、补偿等待、旧版补偿初始化窗口、三阶段 OPA
故障恢复、有界基础设施退避和真实 Redis 丢消息后重建。全部通过；这些测试中的取消与
重建服务不等同于操作系统级强杀，进程验证由独立脚本记录。

worker 在任务开始时设置 30 秒再扫描时间。进程被强杀后最迟会在该时间到期并经过下一轮
扫描时重新尝试；正在运行的任务仍受 advisory lock 保护，不会因为扫描时间到期而并行
执行同一个请求。队列不是精确到毫秒的定时器。
