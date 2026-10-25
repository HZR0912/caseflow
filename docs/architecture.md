# CaseFlow 架构（W1–W11 实际进度）

完整目标架构见 `../../CaseFlow-企业办事Agent-项目设计方案.md`。以下只列已有实现。

审批、执行与记忆在 API lifespan 中共享异步 PostgreSQL 连接池；MockCorp lifespan
共享同步池。两个池各最多 8 条连接、最多 32 个等待请求、借用等待最多 5 秒。
审批会话在归还连接前显式释放 advisory lock，清理失败则丢弃连接。
每个 worker 各有自己的池；观测、迁移与独立脚本需另外计入连接预算。
详见 [ADR-0014](design-decisions/0014-bounded-database-pools.md) 和
[并发验证报告](database-concurrency-verification.md)。

## 运行入口

```text
用户 / curl → apps/api/main.py :8000
               ├─ Bearer → 服务端身份映射
               ├─ /v1/guardrails/check → core/guardrails/pipeline.py
               │    归一化 → 注入检测 → PII token 化 → 意图路由 → 槽位抽取
               │    产出 disposition ∈ {blocked, human_review, clarify, route}
               ├─ /v1/policy/check → core/policy/opa_client.py → OPA policies/*.rego
               ├─ /v1/plans/preview → LangGraph understand → retrieve → plan → authorize → dry_run
               │    OPA 逐步授权 → MockCorp /v1/preview → awaiting_approval / clarify
               ├─ /approvals → Vue3 审批台
               ├─ /v1/approval-requests → W5 预演 → 冻结审批快照
               │    review → approval(interrupt) → report；AsyncPostgresSaver
               │    /decision → 可信审批人 + OPA → 持久化决定 → Command(resume)
               │    /execute → 解密冻结参数 → 逐次 OPA → 稳定幂等键 → MockCorp
               │        execution_runs：调用意图、真实 ID、重试预算、失败分类
               └─ /v1/actions/execute → W1 策略探针（403 / 501，提示新执行入口）

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

主 API 调用 W5 请求作用域图并通过 HTTP 调用 MockCorp 批量预演；W1 断点演示仍为独立图。
MockCorp 模拟外部系统，凭据授予租户内模拟操作权限；不在内部重复调用 OPA。
W7 Agent 工具网关已接入审批、逐次策略校验与持久重试；MCP server 尚待实现。

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
| `approval_requests` | W6 脱敏快照、创建幂等、期限与不可变决定 | tenant/requester/creation_key、plan_hash、decision、status |

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
W5 `/v1/plans/preview` 已接入编排和逐步 OPA 授权，独立护栏 API 仍只打标签。
详见 [ADR-0006](design-decisions/0006-input-guardrails.md)。

## 规划与预演（W5）

`core/graph/workflow.py` 为实际 LangGraph，计划契约校验白名单操作、依赖引用和无环。
默认规则提取槽位，入职模板生成每人四步；可选供应商降级链只补充有依据的缺失槽位。
制度证据默认本地检索，扫描间接注入并逐字核对原文。OPA 在意图预检和逐步动作检查中生效。
MockCorp 在同一私有事务状态中预演依赖链，结束后仅保留 dry-run 日志。
返回与 checkpoint 状态脱敏，原文和 vault 在请求闭包；W5 API 尚无持久化会话恢复能力。
详见 [演示验收](w5-planning.md) 和 [ADR-0007](design-decisions/0007-understand-plan-preview.md)。

## 尚未实现

MCP server、自由生成的制度问答与 LLM judge。
W8 已接通分组 Saga；人工对账结案与后台重试调度尚未实现。

## 评测与发布门禁（W11）

`eval/scenarios.py` 校验版本化场景，`harness.py` 驱动真实规划/审批/执行服务，
`runner.py` 管理隔离数据库和逐例产物，`gate.py` 重新核对完整性、收据和基线。
60 业务与 60 对抗各跑五次。pass^5 要求同一业务的五次全部通过，内部确定性回归
额外要求 600 次全通过。引用逐字核对受控文件，重放与恢复核对实际副作用。
规则 profile、内部已知对抗集、本地供应方适配器和服务重建的范围详见
[W11 说明](w11-evaluation.md) 与 [ADR-0013](design-decisions/0013-evaluation-gate.md)。

## 持久化审批（W6）

W5 闭包不承诺跨进程恢复；W6 将成功结果冻结到审批表，并以其作为持久图输入。
审批单与 checkpoint 通过可重试的流程衔接：保存快照→建立图，保存决定→恢复图，
checkpoint 完成→更新查询状态。每个崩溃边界均可恢复，审批接口用会话 advisory lock
避免并发推进同一 thread。所有权/租户检查先于 checkpoint 查询；同租户其他记录由 OPA
控制，决定额外禁止自审批。默认 24 小时过期，决定绑定整个快照的摘要。
审批图的 interrupt 前无外部副作用。批准终态 approved_ready，不调用正式工具。
详细演示见 [W6 审批台](w6-approval.md)，设计见 [ADR-0008](design-decisions/0008-durable-approval.md)。

## 持久执行（W7）

`POST /v1/approval-requests/{id}/execute` 读取已批准快照，加密计划解封后验证摘要，
按依赖顺序调用四个允许的入职工具。每次调用前重新问 OPA，使用稳定幂等键；
HTTP 前保存 intent，HTTP 后保存资源 ID。执行记录存入 `execution_runs.journal`，
与审批共享同一会话 advisory lock。审批 checkpoint 不写入明文执行参数。
审批状态与执行状态分开，查询接口组合返回。供应方提交后崩溃由原键重放收据恢复。
重试预算、next_attempt_at 均持久化；新计划业务拒绝后进入 W8 分组 Saga。
详见 [W7 执行验收](w7-execution.md) 与 [ADR-0009](design-decisions/0009-durable-execution.md)。

## 分组补偿与部分成功（W8）

规划新增可选订单、席位、批量工单分配，冻结 saga_version 与 saga_group。
core/tools/saga.py 按失败分组倒序补偿，组外成果及不可逆消费者保护其依赖。
补偿 journal 仍位于 execution_runs，同审批会话锁内持久化意图、重试、固定撤销键。
MockCorp /v1/compensations 在租户行锁内读取原收据并比较对象，原子提交语义撤销。
退款使用增量，批次只恢复实际变更；未知结果或冲突显式进入人工处理。
审批台展示补偿分组、最终影响、原始批次逐项结果和人工提示。
详见 [W8 说明](w8-saga.md)、[实测报告](w8-verification.md) 与
[ADR-0010](design-decisions/0010-scoped-saga.md)。

## 三类记忆与组织模型（W9）

规划解释器按显式选择读取记忆；本次输入 > 会话槽位 > 个人偏好 > 组织默认值。
memory_sessions 先检查 tenant / owner，LangGraph checkpoint 中槽位以绑定身份与会话的
密文保存；memory_preferences 按 tenant / owner / key 去重、版本控制、向量召回和衰减。
org_departments / org_members / org_systems 提供部门、主管、导师和系统负责人，
递归 CTE 得到祖先和最近默认值；组织版本整体事务更新，不改变 OPA 的认证角色。
个人与同部门已决申请提供历史结果摘要。审批快照冻结记忆来源及版本，
之后修改/遗忘不改变已批准的执行计划。说明及证据见
[W9 记忆](w9-memory.md)、[W9 验证](w9-verification.md)、
[ADR-0011](design-decisions/0011-scoped-memory.md)。

## 可观测性（W10）

API 生命周期创建独立 OTel Runtime；审批申请的 Trace ID 稳定跨进程，阶段及节点
使用父子 Span。安全结构投影写入 observation_traces / observation_spans，并可选
批量 OTLP 导出 Langfuse。开始帧先写，结束帧随后补全，强杀留下未结束记录。
观测存储与业务事务独立，导出失败不改变业务结论。`/traces` 页面仅通过经
OPA 与租户/所有者约束的 GET 接口读取；费用按真实返回用量及配置单价估算。
说明及限制见 [W10](w10-observability.md)、[验证](w10-verification.md) 与
[ADR-0012](design-decisions/0012-observability.md)。
