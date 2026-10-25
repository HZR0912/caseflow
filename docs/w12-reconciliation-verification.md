# 第二、三项并行整改：异常对账与人工结案验证

日期：2026-10-25。范围：第三项。环境：Windows、Python 3.12、PostgreSQL 16 + pgvector、
真实 ASGI 路由、MockCorp 供应方。设计见 [ADR-0015](design-decisions/0015-background-and-reconciliation.md)。

## 结果与行为

审批台已支持核对供应方执行/补偿收据及实际状态，然后记录有权限的人的处理决定。
原执行日志保留；结案和后台停机在同一事务提交，之后无论点击执行还是旧消息重放均不能重新办事。

| 操作 | 结果 |
|---|---|
| `GET /v1/approval-requests/{id}/reconciliation` | 所有人仍按原审批可读范围查看证据、结案与处理记录 |
| `POST /v1/approval-requests/{id}/reconcile` | 当前 admin 经 OPA 授权后获取新的供应方证据 |
| `POST /v1/approval-requests/{id}/resolve`，`confirm_compensated` | 确认 Saga 所需补偿均有收据且状态匹配，单独记录结案 |
| 同上，`accept_observed_state` | 明确接受当前已观测状态，包括其他系统造成的状态变化 |
| 同上，`keep_open` | 记录继续处理的原因，不写结案记录，不冒充处理完成 |

证据绑定审批、步骤、原始参数指纹、执行日志哈希和证据哈希，默认有效 10 分钟。
结案前重新查询供应方并比较证据；旧版本、过期、供应方失联或状态变化均不能直接结案。
请求中的租户、身份和额外字段不能替代服务端鉴权。

## 专项验证

9 项单元测试、11 项真实 PostgreSQL/ASGI 集成测试通过。

| 检查 | 已验证行为 |
|---|---|
| 未知执行与缺收据 | 未知执行、未知退款及“日志成功但收据缺失”不能关闭 |
| 已退款但响应丢失 | 原退款幂等键查到收据后可确认；核对和重复结案不再退款 |
| 错误供应方证据 | 租户、数量、目标、请求指纹或收据状态组合不符时拒绝 |
| 状态变更与证据时效 | 旧 journal、超过 10 分钟、供应方变化均拒绝；需重新核对 |
| 人工接受现状 | 已变化但可确认的影响可明确接受；理由必填，原 journal 不变 |
| 权限与隔离 | 无 token 401；跨租户 404；普通 owner 可读、核对/结案 403 |
| 并发与终态 | 同请求锁互斥；相同结案幂等；结案后 execute 拒绝，后台凭据清除 |
| 审计 | 处理决定、操作者、时间、脱敏原因、前序哈希单独留存 |
| API 接线 | 主 API 和供应方真实 ASGI 路由，以及 background/execute 入口已覆盖 |

前端另验证未知结果禁用结案、默认继续待处理、必填说明、过期提示、权限按钮、
原失败结果保留和 API 审计事件正确展示。完整共同验证见
[后台恢复报告](w12-background-recovery-verification.md)。

## 复现

```powershell
uv run pytest tests/test_reconciliation.py -q
uv run python -m scripts.verify_integration tests/test_reconciliation_integration.py -q
```

集成脚本创建独立临时数据库，执行完整 upgrade → downgrade → upgrade，然后测试并清理。
迁移 `0010_reconciliation` 的证据、结案、审计表可回滚；结案决议表只接受两种关闭决定，
`keep_open` 仅写审计事件。

## 边界与白话说明

没有收据的未知操作可能仍在路上，当前供应方没有“保证这个请求以后也不会再落地”的
取消屏障，所以这类情况仍需继续待处理。证据表示核对时刻的状态；其他系统随后仍可改变资源。
确认补偿只针对原 Saga 的范围，组外保留成果仍应按原执行摘要核对。

白话说：以前系统只知道“这一步报错了”，人还得自己查究竟扣没扣钱、退没退款。
现在有一个正式入口拿收据核对，并记下谁因为什么理由结案；查不清就保留待处理，
不能把“没看到收据”当成“肯定没发生”。
