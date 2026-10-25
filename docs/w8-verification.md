# W8 验证报告：补偿 Saga + 部分成功语义

- 验证日期：2026-10-24，Asia/Shanghai。
- 环境：Windows / Python 3.12 / Node.js 24 / Docker PostgreSQL + pgvector / OPA / Edge。
- 范围：规划 → 预演 → 冻结审批 → 执行 → 分组补偿 → 持久化查询 → 浏览器展示。
- 结论：本地自动化验收通过。远程 CI 已增加 W8 验收步骤，本轮未提交、未运行远程 CI。

## 汇总

| 验证层 | 结果 | 证据 / 命令 |
|---|---|---|
| Ruff 静态检查 | 通过 | uv run ruff check . |
| Ruff 格式检查 | 通过 | uv run ruff format --check . |
| Diff 空白检查 | 通过 | git diff --check |
| 非集成回归 | **332 passed，56 deselected** | uv run pytest -m 'not integration' -q |
| 真实 PostgreSQL 集成 | **56 passed，331 deselected，0 skipped** | uv run python -m scripts.verify_integration |
| 迁移往返 | upgrade → downgrade → upgrade 通过 | 同上，独立临时数据库 |
| Vue 交互测试 | **9 passed** | apps/web 下 npm test |
| TypeScript / Vite | 构建通过 | apps/web 下 npm run build |
| OPA 策略 | **12/12 passed** | docker compose exec -T opa /opa test /policies -v |
| W7 兼容回归，真实 HTTP + 浏览器 | **16 项通过，30 次业务提交，无重复** | tmp/w7-check.json |
| W8 真实 HTTP + 浏览器 | **17 项通过，重复提交键 0 个** | tmp/w8-check.json |

集成运行时共有 331 个非集成用例；随后新增“付费人数超限/重复工单要求澄清”的纯规划
回归用例，因此最终非集成总数为 332。该新增校验经全量非集成及最终 HTTP 演示验证。
集成测试使用真实数据库，未把缺少 DATABASE_URL 导致的跳过算作成功。

## 关键验收结果

### 1. 余额不足：精确撤回仓库成员

单员工付费计划在创建账号、邮箱、仓库成员后，订单返回 insufficient_funds。
只补偿 p1_invite；p1_user 与 p1_mail 保持 active。未创建订单、未绑定席位，
后续验收工单未执行；返回 partial_success，摘要明确列出保留与撤销步骤。
再次执行返回相同终态。

### 2. 退款提交后强杀 API：恢复且只退款一次

验收工单连续 4 次 429 后停止，按 p1_seat → p1_order → p1_invite 倒序补偿。
通过独立 SQL 确认退款补偿收据已经提交后，强杀 API，此时本地 journal 尚无成功回执。
新 PID 启动后原补偿键不变，订单补偿第 2 次调用取得 replayed=true。
供应方余额恢复为 **100000 分**，退款记录 **恰好 1 笔**，席位停用、成员撤回。
新旧 PID、原键及事件保存在 JSON 报告。

### 3. 批次部分成功：2 成功 / 2 失败，只恢复成功项

真实 HTTP 207 响应包含 4 个工单的逐项结果。执行器停止后续动作，
仅补偿 assignment 组；四个工单最终恢复到本次分配前状态。
原始 2 成功 / 2 失败记录保留，身份、仓库成员与入职工单等组外成果保留。
补偿依次注入 429、提交后响应丢失；第 3 次按原键重放成功，Retry-After 至少 1 秒。

### 4. 补偿失败：人工处理，预算不重置

成员撤回连续 4 次 500 后返回 compensation_failed。
manual 和 compensation_unknown 明确列出 p1_invite，不猜测远端状态。
重复 execute 不新增供应方日志、不重置尝试次数。浏览器隐藏自动恢复按钮，
显示核对供应方状态的提示，刷新后仍保留原记录。

### 5. 边界与兼容回归

新增 W8 专项覆盖 **14 个非集成用例 + 13 个数据库集成用例**：

- 原收据绑定、跨租户/操作替换拒绝、固定补偿键、退款增量保留无关采购。
- 对象变化拒绝撤销；多项批次先全部比较，任一成功项变化则整次补偿不落地。
- 已有外部依赖保护账号；不可逆保留步骤保护其祖先，避免先拆除仍被使用的资源。
- 批次失败项后续被其他操作修改时不碰它；畸形、重复、遗漏、外部 ID 收据转未知。
- 全失败批次不标部分成功；付费人数超限或重复工单进入澄清。
- 补偿调用提交后崩溃、正向失败已保存但 Saga 尚未创建的崩溃窗口均可恢复。
- 补偿并发单一推进；Retry-After 等待时间和调用预算跨新服务实例保留。
- 权限撤销、补偿预算耗尽、外部退款导致状态变化时停止后续依赖补偿。
- 正向结果未知时不自动补偿；补偿结果未知单独暴露。
- 旧 W7 加密计划缺少 Saga 字段仍能通过原摘要校验，不自动改变旧审批的补偿语义。

## 浏览器检查

真实服务与临时凭据下检查桌面 1440px、移动端 390px：
审批冻结分组、保留/撤销摘要、批次逐项结果、未知补偿提示、终态按钮和刷新持久化。
检查页面无水平溢出、无 JavaScript 异常；宽表在容器内横向滚动。
截图经视觉复核：

- tmp/w8-scope-desktop.png
- tmp/w8-batch-desktop.png
- tmp/w8-batch-mobile.png
- tmp/w8-manual-mobile.png

浏览器测试明确等待目标申请详情加载，避免读取上一份申请的数据。
原始步骤的“批次部分成功”与整体“保留组外成果”使用不同文案。

## 复验

在仓库根目录 PowerShell 运行：

    $env:PYTHONUTF8 = '1'
    docker compose up -d postgres opa
    uv run ruff check .
    uv run ruff format --check .
    uv run pytest -m 'not integration' -q
    uv run python -m scripts.verify_integration
    docker compose exec -T opa /opa test /policies -v
    Push-Location apps/web
    npm test
    npm run build
    Pop-Location
    uv run python -m scripts.w7_execution_demo --check --browser-check
    uv run python -m scripts.w8_saga_demo --check --browser-check

首次运行需 uv sync --frozen --extra dev、apps/web 下 npm ci。
完整使用说明见 [W8 Saga](w8-saga.md)，实现取舍见
[ADR-0010](design-decisions/0010-scoped-saga.md)。
演示/集成数据库和子进程在 finally 中清理；机器报告和截图留在被 git 忽略的 tmp，
本报告与复验脚本可随代码版本管理。

## 已知边界

补偿是语义撤销并保留审计历史，不能抹除已发生通知或删除不可逆工单。
结果未知时必须对账，人工处理尚无结案接口；长等待需要显式恢复，无后台自动调度。
供应方为 MockCorp；真实系统仍需各自实现幂等、版本控制和退款契约。
当前对象值比较不能检测修改后恢复原值的 ABA 历史。
本轮未部署线上、未提交 Git，未宣称整个项目所有周次功能已完成。
