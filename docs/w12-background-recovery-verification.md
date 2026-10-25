# 第二、三项并行整改：后台恢复验证报告

日期：2026-10-25。环境：Windows、Python 3.12、PostgreSQL 16 + pgvector、Redis 7、
OPA 1.4.2、真实 API/MockCorp/worker 子进程。第二项由后台实现子智能体负责，第三项由
对账实现子智能体负责，另有独立审查与主智能体整合验证。

## 结论

第二项后台自动恢复、第三项异常对账与人工结案已实现并完成本机源码验证。
人在批准计划上点击执行后，授权先持久化；长等待或进程中断后由 worker 自动继续。
没有执行授权的单据不会被自动启动，撤权和人工结案后不会继续产生新的操作。
第三项的证据与限制见 [对账验证报告](w12-reconciliation-verification.md)。

## 真实进程强杀

[原始报告](../eval/results/background-process.json)来自
`scripts.verify_background_process`，使用隔离数据库和随机 Redis 队列：

1. 供应方第一次创建账号已提交、响应仍在延迟时，使用操作系统 `kill` 终止 API。
2. 启动独立 worker，它自行发现 PostgreSQL 中的授权，沿用原幂等键恢复。
3. 供应方第二次创建账号已提交、响应尚未保存时，再 `kill` worker。
4. 新 worker 按原 `due_at` 等待并接续，没有调整时钟、催任务或点击恢复。
5. 最终 12 步成功，供应方只有 **12 笔业务提交、2 笔原键重放、0 笔重复副作用**；
   后台任务 `completed` 且密文凭据已清空。

脚本运行后清理了自己创建的 API/供应方/worker 进程、临时数据库和 Redis 消息，
没有清空共享 Redis。此证据与单元测试中的异常注入、任务取消明确区分。

## 共同验证

| 验证 | 结果 | 证据 |
|---|---:|---|
| Python 非集成测试 | 405 passed | [JUnit](../eval/results/w12-unit.xml) |
| 真实数据库集成 | 108 passed，无跳过 | [JUnit](../eval/results/w12-integration.xml)；独立数据库升→降→升后测试并删除 |
| 后台专项（包含于上述集成） | 19 passed | `tests/test_background_integration.py` |
| 对账专项（包含于上述两类） | 9 单元 + 11 集成通过 | `tests/test_reconciliation*.py` |
| 前端 | 24 passed；生产构建通过 | Vitest、vue-tsc、Vite |
| Rego | 15/15；格式通过 | `opa test policies tests/policies -v` |
| W11 业务与对抗回归 | 600/600；全部正式门禁通过 | [JSON](../eval/results/w12-current.json)、[HTML](../eval/results/w12-current.html) |
| 静态检查 | Ruff、format、diff、Compose 配置通过 | 本轮命令输出 |

后台专项覆盖：未经批准或身份不符不创建任务、授权持久化而 journal 尚未写入时中断、
远程提交后丢响应、新服务恢复、重复 worker 与人工执行争抢同一锁、到期调度、
真实 Redis 消息删除后扫描重建、六种授权失效、新凭据再授权、补偿等待、
旧日志尚未初始化 Saga 的恢复窗口、三阶段 OPA 不可用及有上限的基础设施重试。

## 验证中发现并修复

- OPA 临时不可用曾被当作永久拒绝，可能阻断任务或误触发补偿。后台执行现在暂停重试，
  不消耗业务尝试次数；真正的策略拒绝仍然阻止操作。
- 人工结案增加执行入口冻结，并与后台使用相同请求锁和事务，防止结案后旧消息继续执行。
- 供应方证据增加状态组合校验，缺收据不能宣称已保留/已补偿。
- Windows Arq 关闭路径引用不存在的 `SIGUSR1`；改为显式清理任务与拥有的连接资源。
- 前端审计事件按 API 的 `payload` 展示，避免处理理由丢失；CI 补充真实 Redis 与进程强杀步骤。
- 第一轮 600 次全部通过，但门禁因新增 Rego 测试改变政策文件指纹而拒绝放行。
  [失败证据保留](../eval/results/w12-policy-test-mismatch.json)。新增策略测试已移至
  `tests/policies` 并纳入 CI，实际加载的策略恢复原样；随后完整复跑通过，未调整基线或阈值。

复跑并发为 4：业务 300/300、对抗 300/300、业务 pass^5 为 60/60，引用、重放、恢复、
补偿均通过；供应方 1065 次实际提交、重复副作用 0、OPA 降级 0，P95 为 1364.337 毫秒。

版本基线保持原样，SHA-256：
`bcdb5d889d303340234aa3d8c10a38e1d5638c452c6013cf5486c1ca4800fc57`。

## 运行与复现

配置 `AUTH_CREDENTIALS` 和稳定的 `EXECUTION_ENCRYPTION_KEY`，API、worker、MockCorp
使用同一数据库和身份配置。完整部署：

```powershell
docker compose --profile app up -d --build
```

MockCorp 先完成迁移并健康后，API 和 worker 才就绪。源码方式运行须先
`uv run alembic upgrade head`，再分别启动 API、MockCorp 与
`uv run python -m core.jobs.worker`。详见 [后台运行说明](background-recovery.md)。

```powershell
uv run pytest -m "not integration" -q
uv run python -m scripts.verify_integration -m integration -q
uv run python -m scripts.verify_background_process
uv run python -m eval.runner --concurrency 4 --output eval/results/w12-current.json
```

## 实际边界

本轮验证的是源码与独立临时服务；本机数据库 schema 已升级到 `0010_reconciliation`，
未重建此前运行的 API/MockCorp 容器。使用现有部署需按上面的命令更新镜像并启动 worker。
远程 CI 由 GitHub push 触发，本机报告不能替代对应提交的 Actions 结果。

授权默认 24 小时、基础设施失败预算默认 6 次；耗尽或身份失效会暂停，需要人重新核对授权。
容器环境变量不会热更新，撤权必须同步更新并重建相关实例。当前身份仍是静态 token 映射，
没有接入企业 SSO 或集中撤权平台。

没有完成长期压力测试、任意网络故障组合、生产级 worker 告警和真实供应方部署。
W11 回归仍是确定性规则评测，不能据此声称真实大模型质量提升。

白话说：以前任务卡住，要有人守着再点一次；现在已经答应办理的事情，程序重启后还能
自己接着办，同时不重复扣款或开户。碰到查不清的结果，会留给人核对并记录结论。
这次补的是“出问题后还能把事情负责到底”的能力。
