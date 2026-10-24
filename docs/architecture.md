# CaseFlow 架构（W1–W2 实际进度）

完整目标架构见 `../../CaseFlow-企业办事Agent-项目设计方案.md`。以下只列已有实现。

## 运行入口

```text
用户 / curl → apps/api/main.py :8000
               ├─ Bearer → 服务端身份映射
               ├─ /v1/guardrails/check → core/guardrails/pipeline.py
               │    归一化 → 注入检测 → PII token 化 → 意图路由 → 槽位抽取
               │    产出 disposition ∈ {blocked, human_review, clarify, route}
               ├─ /v1/policy/check → core/policy/opa_client.py → OPA policies/*.rego
               └─ /v1/actions/execute → 策略拒绝 403 / 允许后执行器暂返回 501

scripts/w1_checkpoint_demo.py → core/graph/demo_graph.py
                                └─ PostgresSaver → PostgreSQL checkpoint 表

内部模拟凭据 / HTTP → apps/mockcorp/main.py :8001
                       ├─ Bearer → 服务端 tenant
                       ├─ Identity / Code / Ticket / Money（13 个写操作）
                       └─ service → domain → store → PostgreSQL
                           ├─ dry-run：校验、diff、rollback
                           ├─ 正式执行：状态与收据同事务提交
                           └─ 故障注入：默认关闭，显式开启

Redis 7：容器与持久卷已就绪，尚未接入任务队列
```

主 API、断点演示和 MockCorp 是独立入口。主 API 当前不调用演示图或 MockCorp。
MockCorp 模拟外部系统，凭据授予租户内模拟操作权限；不在内部重复调用 OPA。
后续 Agent 工具网关仍需执行策略校验、审批和重试。

## 数据表

| 表 | 用途 | 关键字段 |
|---|---|---|
| `tenants` | W1 租户业务表 | `slug` |
| `users` | W1 组织成员与角色 | `role`、`external_id` |
| `requests` | W1 办事请求 | `thread_id`、`status`、`plan` |
| `audit_log` | W1 审计链结构 | `prev_hash`、`entry_hash` |
| PostgresSaver 自建表 | W1 演示图的持久化 checkpoint | thread_id 等 |
| `mockcorp_state` | W2 模拟业务，与 W1 业务表隔离 | tenant 主键、state JSONB |
| `mockcorp_receipts` | W2 幂等结果，与业务状态原子提交 | tenant+key 主键、fingerprint、status_code、response |

MockCorp 每租户一行模拟状态；行锁串行化同租户写入，跨租户可并发。
dry-run、提交前拒绝和故障回滚整个事务；成功与部分成功收据跨进程持久化。
这是模拟环境的实现，不承诺高吞吐或无限资源；详见
[ADR-0003](design-decisions/0003-mockcorp-transactions.md) 和 [HTTP 契约](mockcorp-api.md)。

## 已验证的状态机骨架

```text
START → echo → approval(interrupt) → report → END
                    ▲
                    └─ 进程退出后，新进程按 thread_id 从 checkpoint 恢复
```

已验证跨进程恢复，包括 checkpoint 提交后强杀进程。尚未连接真实工具或审批 UI。

## 输入护栏（W4）

```text
文本 → normalize（NFKC / 零宽 / 空格插入 / base64 展开，保留原文索引）
     → injection（规则命中：类别 + 权重 + 原文偏移；≥0.6 拦截）
     → pii（校验位过滤 → 类型化占位符；真实值进请求级 vault）
     → intent（饱和评分 + 置信度三档路由；求确认句式强制只读问答）
     → slots（正则/枚举抽取，缺槽追问，上一轮已确认值合并）
```

护栏不做授权判断：越权与数据外泄只产出 risk 标签和 policy_action，拦截在 OPA。
护栏与主 API 的编排尚未连通——W5 接入。详见 [ADR-0006](design-decisions/0006-input-guardrails.md)。

## 尚未实现

记忆、MCP 工具网关、Agent 执行器与自动重试、Saga 补偿、审批台、Trace 回放和评测 harness，
以及 RAG 的生成与引用标注。护栏与 RAG 目前都是独立入口，尚未接入 Agent 编排。
