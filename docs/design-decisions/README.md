# 架构决策记录（ADR）

每个关键取舍一页：背景、选项、决定、代价。面试时可以直接翻给对方看。

| 编号 | 决策 | 状态 |
|---|---|---|
| [0001](0001-opa-vs-dsl.md) | 策略引擎选 OPA + Rego，而非自研 DSL / Python if | 已接受 |
| [0002](0002-trusted-api-identity.md) | 用服务端 token 映射建立最小可信身份来源 | 已接受 |
| [0003](0003-mockcorp-transactions.md) | MockCorp 原子幂等、预演和故障语义 | 已接受 |
| [0004](0004-mockcorp-oplog.md) | MockCorp 独立操作日志，失败路径也写 | 已接受 |
| [0005](0005-policy-rag.md) | 制度知识库：按条款切块、版本是块的属性、引用必须可验证 | 已接受 |
| [0006](0006-input-guardrails.md) | 输入护栏：规则先行、归一化只用于匹配、越权只打标签不拦 | 已接受 |
| [0007](0007-understand-plan-preview.md) | 入职规划、DAG 与事务预演 | 已接受 |
| [0008](0008-durable-approval.md) | 持久审批与崩溃窗口恢复 | 已接受 |
| [0009](0009-durable-execution.md) | 加密参数、审批绑定与持久执行日志 | 已接受 |
| [0010](0010-scoped-saga.md) | 冻结分组、收据绑定补偿与部分成功 | 已接受 |
| [0011](0011-scoped-memory.md) | 显式分层记忆、加密会话与关系型组织目录 | 已接受 |
| [0012](0012-observability.md) | 显式 OTel、独立持久回放与可选 Langfuse | 已接受 |
| [0013](0013-evaluation-gate.md) | 真实收据评测、五次全通过与显式版本基线 | 已接受 |
| [0014](0014-bounded-database-pools.md) | 有界数据库连接池、显式解锁与并发回归 | 已接受 |
| [0015](0015-background-and-reconciliation.md) | 持久执行授权、后台接续与收据对账结案 | 已接受 |

## 写 ADR 的规矩

1. **必须写代价**：只写优点的 ADR 是宣传稿，不是决策记录。
2. **必须写被否掉的选项**：否则读者无法判断你是否真的做过选择。
3. **决定之后要有复核点**：如果前提变了（比如 Rego 规则长到读不懂），要能指出在哪里重新评估。
