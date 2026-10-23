# ADR-0001：策略引擎选 OPA + Rego，而不是自研 DSL 或 Python 函数

- 状态：已接受
- 日期：2026-10-23（W1）
- 相关代码：`policies/authz.rego`、`core/policy/opa_client.py`、`apps/api/main.py`

## 背景

CaseFlow 的写操作平面必须回答一个问题：**这个用户，能不能对这个资源做这个动作。**

它在系统里至少出现在三处：

1. 护栏层：用户说"帮我删掉张三的账号"，当前用户没这个权限 → 在进入规划前就拦掉。
2. 规划层：生成 DAG 时，某些步骤需要判风险级，高风险的必须走审批。
3. 执行层：工具网关在真正调用 MockCorp 之前再校验一次（防御性复核，不信任上游）。

三处必须用同一份授权逻辑，否则三处各自演化，出现"护栏放行、执行拒绝"或者更糟的相反情况。
所以第一件事不是"要不要策略引擎"，而是"授权逻辑放在哪一层、以什么形式存在"。

## 选项

### 选项 A：散落在业务代码里的 if 判断

```python
if user.role == "admin" or (user.role == "team_lead" and action.startswith("code.")):
    ...
```

- 优点：零成本，零新增依赖。
- 缺点：策略与业务耦合；三处复用只能抽函数；改规则要发版；无法单独测；审计时说不清"当时的规则是什么"。
- 致命点：这类项目最常被面试官问的一句是"你怎么保证 Agent 不越权"，回答"我在代码里写了 if"很难证明这条 if 在所有路径上都被执行到。

### 选项 B：自研一个小 DSL（YAML / JSON 规则表）

```yaml
- effect: allow
  role: team_lead
  action: code.*
```

- 优点：可配置、可热更新、比 if 更可审计。
- 缺点：**要自己实现求值语义**（匹配、优先级、deny-overrides、默认拒绝、集合运算）。
  这块一旦开始长，就会变成"写了个半成品 OPA"：表达能力不如 Rego，边界 case 没有测试基准，
  而且这套 DSL 只在本项目存在 —— 对读者来说是完全陌生的东西，学习成本转嫁给了面试官。

### 选项 C：OPA + Rego（sidecar）

- 优点：
  - 语义已定型（默认拒绝、`not`、集合、`with` 覆盖、partial evaluation），不需要发明规则。
  - `opa test` 让策略有 **pytest 等价物**，可以把"unknown role 必须拒绝"这类边界写成用例。
  - 策略与代码解耦：改 `.rego` 不用重新构建 Python 镜像，`--watch` 下改完即生效。
  - 决策可查询：`/v1/data/.../decision` 返回结构化结果，能进审计日志。
  - 是真实企业权限系统的选型（Kubernetes、Terraform、Envoy 生态），面试可对照讲。
- 缺点：
  - 多一个容器、多一个故障点（→ 必须 fail-closed，见下）。
  - Rego 学习曲线（本项目的实际成本见"代价"）。
  - 策略写得过大就很难读，需要纪律约束。

## 决定

选 **C**，并附加三条自我约束：

1. **策略保持小**：每类规则 ≤ 20 行；`authz.rego` 只做"角色 × 动作前缀 × 提权"三件事，
   不做任何业务逻辑（业务判断留在 Python，OPA 只回答 allow/deny）。
2. **先测后接**：任何策略改动先补 `policies/*_test.rego`，`opa test` 绿了才接入服务。
3. **决策契约最小**：对外只暴露 `decision := {allow, reason}`，
   内部规则用 `_` 前缀；调用方只查 `/v1/data/caseflow/authz/decision`。

## 代价与缓解

| 代价 | 实际情况 | 缓解 |
|---|---|---|
| Rego 学习成本 | 方案预估 3–5 天；W1 实际约 2 小时写出第一个可用策略（含 `rego.v1` 语法、`with` 覆盖、`some ... in`） | 保持策略小；遇到语义不确定先写 test 再写规则 |
| 多一个故障点 | OPA 挂了怎么办 | 客户端 **fail-closed**：超时 / 5xx / 连接失败一律拒绝并标记 `degraded=true`，绝不因引擎不可用而放行（`tests/test_policy_client.py` 覆盖 4 种故障） |
| 容器运维成本 | distroless 镜像没有 sh/curl，健康检查写不了 | 用 `opa eval` + `http.send` 打自己的 `/health` 做真实 HTTP 探活（见 `docker-compose.yml`） |
| 决策文档泄露内部结构 | 初版把角色映射表一起返回了 | 改为只查 `decision` 子路径；`opa test` 里加了一条契约用例锁住返回结构 |

## 一个顺手发现的设计点

Rego 里 `not` 的语义是"该表达式不可满足"，而不是"值为 false"。
所以 `not _self_escalation` 在 `input` 缺字段时也成立（因为规则 undefined），
这正好与 fail-closed 一致 —— 缺字段落到 `default allow := false`。
这条在写策略时最容易被误解成"`not` 等于取反"，值得在面试里主动讲。

## 后续（W4/W8 需要复核的假设）

- 策略目前只按 `role + action` 判断。W4 引入护栏层后要加：`tenant` 隔离、资源归属（只能操作自己部门的资源）、
  时间窗口（临时权限到期）。这些仍然写在 Rego 里，但会拆成 `authz.rego` + `tenant.rego` 两个包。
- 如果 Rego 规则超过 ~200 行仍读不下去，考虑 partial evaluation 把"静态可判定的部分"预编译，
  但这是 W11 之后才需要担心的事，现在不提前优化。
