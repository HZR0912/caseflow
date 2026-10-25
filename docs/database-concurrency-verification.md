# 数据库并发稳定性修复验证报告

日期：2026-10-25。环境：Windows、Python 3.12、Docker PostgreSQL 16 + pgvector、HTTP OPA 1.4.2。
范围：待办第一项——并发数据库连接异常；没有扩展到后台任务、人工结案或真实模型。

## 结论

本机已复现建连失败并完成连接池修复。相同 120 场景、每例五次，修复后并发 4
连续三轮、并发 8 连续两轮，共 **3000/3000**，全部正式门禁通过。
五轮均无数据库连接错误、无重复副作用、无 OPA 降级；所有借出的池连接归还，池正常关闭。
默认及 CI 回归已恢复为并发 4。此结论限于本报告的负载和环境，不是生产容量保证。

## 复现与原因

1. [原实现并发 4 复现](../eval/results/db-concurrency-before-c4.json)：599/600，
   失败为 `approval_storage_unavailable` / `connection_failed`，门禁退出 1。
2. [直连并发 4 计数](../eval/results/db-concurrency-before-probe-c4.json)：600/600，
   [连接统计](../eval/results/db-concurrency-before-probe-c4.connections.json)为同步 4992、异步 570，合计 **5562**。
   一次通过不能否认间歇性错误。
3. [直连并发 8 诊断](../eval/results/db-direct-probe-c8.json)：597/600；
   [脱敏异常证据](../eval/results/db-direct-probe-c8.connections.json)捕获三次
   `Address already in use (0x00002740/10048)`，无 PostgreSQL SQLSTATE。
   该轮仅加入解锁清理，尚未接入连接池，仍为逐操作直连。

此前只知道有大量 TIME_WAIT；本轮已确认失败发生在客户端 TCP 建连时，
属于 Windows 本机地址/端口分配冲突。代码中供应方每次读写、审批每次会话都重新建连，
使一轮回归产生五千多次短连接；这是本次修复消除的连接频繁创建问题。
没有证据表明 PostgreSQL 的 `max_connections` 已满，也未证明整个动态端口范围耗尽。

## 改动

- API 审批/执行/记忆共享异步连接池，MockCorp 共享同步连接池，评测与服务使用相同配置。
  各池最多 8 条连接、排队最多 32、借用等待最多 5 秒，应用退出时关闭。
- 保留供应方原子事务、真实授权、业务幂等和重试预算。连接失效可由池替换，
  不自动重新执行已进入业务的请求或 SQL。
- 审批 advisory lock 在连接归还前显式释放，覆盖成功、拒绝、异常和取消。
  如果解锁无法完成，关闭连接，避免下一位请求继承残留锁。
- 池满或等待超时按既有接口返回存储不可用；不让请求无限排队或继续写入。
- 增加池统计和安全的连接计数脚本；评测任务发生异常时先取消并等待其他任务，再清理共享资源。
  20 次服务恢复用例仍使用独立新连接，未以复用服务状态代替持久化恢复。

设计及代价见 [ADR-0014](design-decisions/0014-bounded-database-pools.md)。

## 修复后重复评测

| 运行与原始报告 | 通过 | 建连次数 | 池实际连接数：供应方/审批 | P95（毫秒） | 整轮（秒） |
|---|---:|---:|---:|---:|---:|
| [并发 4，第 1 轮](../eval/results/db-pool-c4-r1.json) | 600/600 | 30 | 4/4 | 1390.843 | 65.462 |
| [并发 4，第 2 轮](../eval/results/db-pool-c4-r2.json) | 600/600 | 30 | 4/4 | 1363.843 | 63.846 |
| [并发 4，第 3 轮](../eval/results/db-pool-c4-r3.json) | 600/600 | 30 | 4/4 | 1385.889 | 65.052 |
| [并发 8，第 1 轮](../eval/results/db-pool-c8-r1.json) | 600/600 | 38 | 8/8 | 1985.918 | 44.900 |
| [并发 8，第 2 轮](../eval/results/db-pool-c8-r2.json) | 600/600 | 38 | 8/8 | 1872.277 | 44.096 |

每轮连接统计另有同名 `.connections.json`；报告另有 HTML 和逐例 JSONL。
建连计数包含两次数据库管理连接和 20 次恢复探针，不包含迁移子进程。
所以并发 4 为 `4 + 4 + 20 + 2 = 30`，并发 8 为 `8 + 8 + 20 + 2 = 38`。
并发 4 较修复前的同口径 5562 次减少约 **99.46%**。
耗时包含隔离数据库创建、迁移与清理；P95 包含每例准备及结果核对，不包含排队。
并发 4/8 的 P95 不直接比较优劣；这些是本机样本，不外推生产吞吐量。

五轮均为：业务 300/300、对抗 300/300、业务 pass^5 60/60、引用 90/90，
重放/恢复/补偿各 20/20；每轮实际提交 1065 次、重复副作用 0、授权降级 0。
每个池结束时 `pool_available == pool_size`、`requests_waiting == 0`，
无 `connections_errors`、`requests_errors`、`connections_lost` 或 `returns_bad`。
已从逐例数据独立重算五轮门禁并核对 JSONL 与 JSON 完全一致，
[汇总证据](../eval/results/db-pool-summary.json)。

版本基线保持原样，SHA-256：
`bcdb5d889d303340234aa3d8c10a38e1d5638c452c6013cf5486c1ca4800fc57`。
场景、判分阈值与授权策略未因本次修复放宽。

## 测试与复现

- 非集成：396 passed。
- 真实数据库集成：78 passed，无跳过；在独立数据库完成 upgrade → downgrade → upgrade 后运行并清理。
- 新增 10 项集成测试：复用与等待上限；正常/异常/SQL 错误/取消后的锁释放；
  排队满与超时拒绝；后端断开后丢弃连接且不重放业务；解锁时取消；两个应用的池关闭。
- 原有供应方并发幂等、不能超支、回滚、审批竞争、执行、补偿、记忆测试已改用共享池夹具，
  新服务恢复的部分仍保留独立直连作为交叉验证。
- [W7 真实 HTTP 执行与强杀恢复](../eval/results/db-pool-w7-http.json)：15 组通过，
  30 次业务副作用，无重复；实际杀死 API 进程后用原幂等键恢复。
- [W8 真实 HTTP 补偿与强杀恢复](../eval/results/db-pool-w8-http.json)：16 项通过，
  退款提交后杀死 API，重启后仅有一笔退款；重复提交为 0。
  两次演示的进程及独立数据库均已清理。
- Ruff check、format --check、git diff --check 全部通过。

```powershell
uv run pytest -m "not integration" -q
uv run python -m scripts.verify_integration -m integration -q
uv run python -m scripts.verify_database_connections --concurrency 4 --output eval/results/db-check-c4.json
uv run python -m scripts.verify_database_connections --concurrency 8 --output eval/results/db-check-c8.json
uv run python -m scripts.w7_execution_demo --check --output eval/results/db-check-w7.json
uv run python -m scripts.w8_saga_demo --check --output eval/results/db-check-w8.json
```

## 边界与下一项

本轮是本机源码验证，未运行远程 GitHub CI，未重建已有部署容器。连接上限按进程计算，
观测写入、迁移和其他独立数据库消费者需要另外预算；没有完成长时间压力、
全部数据库故障矩阵或所有 worker 数量下的容量测试。真实模型、后台自动恢复和人工结案仍待实现。

白话说：以前每查一次、写一次数据库，就重新“拨一次电话”，一轮要拨五千多次，
并发时会遇到本机端口冲突。现在只建立少量连接反复使用，用完归还；
同时确认上一位请求留下的锁已经清掉。这样多人同时办事时，不容易因为反复建连而失败。
接下来可以处理第二项：等待和中断后的后台自动恢复。
