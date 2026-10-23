# CaseFlow 架构（当前进度版）

> 完整目标架构见 `../../CaseFlow-企业办事Agent-项目设计方案.md` §4。
> 这里只画**已经存在**的东西，不做未来规划 —— 图里出现的东西必须是能跑通的代码。

## W1 实际落地的部分

```
                     ┌─────────────────────────────────────────┐
  用户 / curl ──────▶│ apps/api/main.py (FastAPI)              │
                     │  GET  /healthz                          │
                     │  POST /v1/policy/check    只问策略      │
                     │  POST /v1/actions/execute 策略门 → 403  │
                     └───────┬──────────────────────┬──────────┘
                             │                      │
                    ┌────────▼─────────┐   ┌────────▼─────────────────┐
                    │ core/policy/     │   │ core/graph/              │
                    │ opa_client.py    │   │ demo_graph.py            │
                    │ fail-closed 决策 │   │ echo→approval→report     │
                    └────────┬─────────┘   └────────┬─────────────────┘
                             │ HTTP /v1/data/...    │ checkpoint 读写
                    ┌────────▼─────────┐   ┌────────▼─────────────────┐
                    │ OPA 1.4.2        │   │ PostgreSQL 16 + pgvector │
                    │ policies/*.rego  │   │ 业务表 + checkpoint 表   │
                    │ --watch 热更新   │   │ (PostgresSaver)          │
                    └──────────────────┘   └──────────────────────────┘
                                                   ▲
                                          Redis 7（已起，W7 接 Arq）
```

## 数据流：一次写动作为什么要过两道门

1. **护栏层（W4）** 判断"这句话是不是恶意/越权"，越权在进入规划前拦掉。
2. **策略层（W1 已实现）** 判断"这个角色能不能做这个动作"，唯一事实来源是 `policies/`。
3. **执行层（W7）** 工具网关在真正调用前再校验一次 —— 不信任上游，因为上游是模型。

W1 只实现了第 2 道门，并且在 API 层证明"拒绝会返回 403 + 可读原因"。
执行器故意返回 501，而不是假装成功：宁可暴露缺口，也不要让 W7 之前的演示给人错觉。

## 表结构（W1）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `tenants` | 租户隔离的根 | `slug` |
| `users` | 组织成员 + 角色 | `role`、`external_id`（AD/SSO） |
| `requests` | 一次办事请求 | `thread_id`（对 LangGraph checkpoint）、`status`、`plan`(JSONB) |
| `audit_log` | 审计流水 | `prev_hash`/`entry_hash`（链式哈希，唯一索引防分叉） |

## 状态机骨架（W1 验证的最小闭环）

```
START → echo → approval(interrupt) → report → END
                    ▲
                    └── 进程退出后，新进程按 thread_id 从 checkpoint 恢复
```

这条链是后面所有价值的前提：**没有跨进程恢复，"审批等 3 小时"就是假的。**

## 尚未实现（写在明处，别自我暗示已完成）

护栏、RAG、记忆、MCP 工具、MockCorp、幂等键、Saga 补偿、审批台、Trace 回放、评测 harness ——
全部在 W2–W12。当前仓库只有骨架 + 策略门 + 断点恢复。
