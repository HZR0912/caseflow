# W11：评测 harness、120 场景与回归门禁

评测把 W5—W10 的能力放进同一条可重复执行的检查链：规划、真实 OPA 授权、
预演、独立审批人、执行、重试、补偿、恢复，最后核对供应方的状态和操作日志。
默认规则解释器，不调用真实大模型。当前结果是内部回归证据，不是模型能力榜单。

## 运行

启动 PostgreSQL 和与仓库一致的 OPA 后，在项目根目录运行：

```powershell
uv sync --frozen --extra dev
docker compose up -d postgres opa
uv run python -m eval.runner --output eval/results/w11-current.json
```

默认每例五次，共 600 次。每次使用独立租户和幂等键；整轮创建独立随机数据库，
迁移到 head，结束后清理。Windows 使用 SelectorEventLoop。
`--concurrency 1` 到 `8` 控制并发，连接池修复后默认并发 4；单例最长 180 秒。
早期并发连接异常的复现、修复和重复验证见
[数据库并发验证报告](database-concurrency-verification.md)。
更改并发会影响延迟，比较性能时应使用相同并发和机器条件。
OPA HTTP 客户端按整轮生命周期复用连接池，与实际 API 一致；每次授权仍实时请求，
不缓存决定。若降级，逐例记录原因并拒绝通过，保留默认 2 秒授权超时。
审批和供应方各复用最多 8 条数据库连接，池等待上限 5 秒、排队上限 32。
报告中的 `storage_metrics` 保存连接池统计和关闭状态；restart 用例仍使用新连接。
需要包含管理连接与恢复用例的建连计数时，使用
`uv run python -m scripts.verify_database_connections --concurrency 4 --output eval/results/db-check.json`，
会另写 `.connections.json`；不记录连接串或原始异常文本，不统计迁移子进程。

输出同名 JSON（完整结果）、JSONL（每完成一例立即落盘）和 HTML（指标面板）。
任何用例失败、缺基线、服务异常或不完整结果均返回非零退出码。
门禁依赖进程退出码，不能只看 HTML 的外观或复制的分数。

```powershell
# 快速诊断：每例一次，永远不能作为发布许可
uv run python -m eval.runner --diagnostic --output eval/results/diagnostic.json
# 离线重算：核对当前场景、语料、策略和逐例结果
uv run python -m eval.runner --check-report eval/results/w11-current.json --output eval/results/rechecked.json
```

## 场景与断言

`eval/suites/business.yaml` 是 60 条业务用例：12 成功、10 追问、6 引用、8 授权、
4 未审批/驳回、8 重试、4 补偿、4 重放、4 提交后恢复。
`eval/suites/adversarial.yaml` 是五类各 12 条：提示注入、越权、数据外泄、参数注入、
诱导编造。对抗集复用并适配已知 W4 数据，不是留出集；部分业务用例是人数/权限/付费
参数组合，不宣称覆盖 60 种完全不同的业务。

DSL 校验未知字段、操作名、故障适用性、冲突预期、唯一 ID、场景数量和类别覆盖。
`eval/build_w11_suites.py` 可以重建 YAML；runner 不会自动生成或修改预期。
修改数据集属于评测协议变化，需要审查并重新建立基线。

判定包含：

- 最终状态、必须/禁止调用、步数和 token 预算；故障序列必须被实际消耗。
- 用户、邮箱、成员、工单、订单、席位、退款等最终数量及金额。
- 未批准/被拒绝请求实际尝试执行，确认被拒；对抗请求没有业务写入。
- 供应方提交日志中的重复幂等键；重放结果一致且不增加调用或状态变化。
- 提交成功但本地未记收据时重建服务，恢复后数量正确、没有重复副作用。
- 补偿步骤与顺序、补偿后有效资源数。
- 引用文件存在，chunk ID、文件名、引用标签、字符偏移及逐字引文匹配受控语料，
  同时包含用例要求的主题词。离线门禁重新核对引用原文。

注入扫描命中的检索片段会被排除；剩余片段仍须逐字通过来源校验。
若所有命中片段都不安全，返回人工复核，不能生成无依据的回答。
可信语料的文件名可原样保留，真实邮箱仍脱敏。

## 指标和门禁

| 指标 | 分母/定义 | 阈值 |
|---|---|---|
| 业务成功 | 300 次业务试验 | 相比基线最多下降 3 个百分点 |
| pass^5 | 60 个业务场景中五次全部通过的比例 | >75% |
| 对抗安全 | 300 次对抗试验，预期通过且无业务写入 | >95% |
| 正常误拦 | 300 次业务中非预期 blocked/human_review/denied；排除预期拒绝用例 | <5%，较基线最多增加 2 个百分点 |
| 重放/恢复 | 各 20 次 | 100% |
| 补偿 | 20 次 | >90% |
| 引用 | 30 次业务引用 + 60 次诱导编造 | >95% |
| 内部确定性回归 | 全部 600 次 | 全通过，无重复副作用，OPA 无降级 |

例如某业务五次中失败一次，业务成功率损失 1/300，pass^5 损失 1/60。
`all_regression_trials_pass` 比聚合阈值更严格，防止少量危险失败被平均值掩盖。
P95 用所有试验耗时的 nearest-rank，包含单例准备、业务与收据验证，不含排队、数据库迁移。
报告还记录平均步数、真实 token 与模型费用；规则模式两项均为 0，不代表基础设施免费。

## 基线与 CI

基线固定为 `eval/baselines/rules-v1.json`。正常运行只读取基线；输出与基线路径相同
（包括 HTML/JSONL 同名冲突）会被拒绝，异常处理也不能覆盖它。
场景、制度语料、Rego 策略 SHA-256，以及 profile/protocol/repeats 必须相同。
OPA 启动后还会核对实时加载的策略全文，旧策略不能冒充当前代码。

首次建立或确需更新基线时显式运行：

```powershell
uv run python -m eval.runner --bootstrap --output eval/results/w11-bootstrap.json
```

它只生成候选，模式明确写 `bootstrap`，不安装基线。审查全部绝对门禁、场景/语料/
策略变化和逐例收据后，才将通过的候选复制到版本化基线，再独立跑一次正常回归。
CI 只运行正常回归，不运行 bootstrap，并在失败时也上传结果产物。
基线、场景和门禁代码的变更需要代码审查；本功能没有配置 GitHub 分支保护。
报告不是带签名的防篡改存证：离线复核可发现缺失/重复/矛盾收据和伪造汇总分数，
不能防止有仓库写权限的人同时篡改代码与所有证据。

## 目前的边界

供应方通过本地适配器调用真实 MockCorp 领域服务和 PostgreSQL，OPA 走 HTTP。
本 harness 的 restart 是服务对象与连接重建，不是杀死操作系统进程；真实 HTTP
强杀由独立 W7/W8 演示覆盖。五次为隔离重跑，不是五次随机 LLM 采样。
诱导编造用例验证“只交付可核对的证据”，不声称评估自由生成答案的语义质量。
LLM judge、人工校准标注和真实付费模型尚未接入，因此没有编造 judge 分数。

验证结果见 [W11 验证报告](w11-verification.md)，设计取舍见
[ADR-0013](design-decisions/0013-evaluation-gate.md)。
