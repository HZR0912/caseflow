# W8：补偿 Saga 与部分成功语义

W8 已接通规划、冻结审批、正式执行、供应方补偿和审批台结果展示。新计划启用
saga_version=1，失败后按批准时冻结的 saga_group 选择补偿范围。
实测记录与复验命令见 [验证报告](w8-verification.md)。

## 启动与验收

在项目根目录 PowerShell 中运行：

    $env:PYTHONUTF8 = '1'
    uv sync --frozen --extra dev
    docker compose up -d postgres opa
    Push-Location apps/web
    npm ci
    npm run build
    Pop-Location
    uv run python -m scripts.w8_saga_demo --check --browser-check
    # 或启动人工审批演示
    uv run python -m scripts.w8_saga_demo --serve

演示使用独立随机数据库、API/MockCorp 端口、临时凭据与加密密钥，结束自动清理；
数据库账号需有 CREATE DATABASE 权限。交互模式打开打印的 /approvals 地址，
先用审批人凭据批准，再执行。订单注入余额不足后只撤回仓库成员，账号和邮箱保留。
浏览器检查在 Windows 使用 Edge；Linux 需先在 apps/web 安装 Playwright Chromium。

机器报告为 tmp/w8-check.json，保存步骤、补偿记录、重试事件和新旧进程 PID。
不写入 token、加密密钥或执行参数。tmp/w8-*.png 为浏览器验收截图。

## 补偿范围

| 冻结分组 | 正向步骤 | 失败后的处理 |
|---|---|---|
| p{i}_identity | 创建账号、创建邮箱 | 倒序停用本次邮箱/账号；保留记录 |
| p{i}_access | 仓库邀请、可选订单/席位、验收工单 | 倒序解绑席位、退款、撤回成员；身份组保留 |
| ticket_assignment | 批量分配已有工单 | 只恢复原收据中实际成功的修改 |

付费流程通过 facts 的 paid_item 与 unit_price_cents 同时启用，每人一席，
金额单位为整数分。可选 assignment_ticket_ids 将已有工单分配给第一位员工。
每份计划最多 40 步；超过上限或工单 ID 重复会要求更正。
模板适用已确认入职信息，尚不支持自由生成任意业务计划。

补偿按实际执行顺序倒序进行。已完成的不可逆工单、组外保留步骤会保护它们的依赖；
受保护步骤不能自动撤销。补偿遇到权限拒绝、数据变化或预算耗尽时停止，
剩余未完成补偿列入人工处理。任意正向结果未知时，不启动自动补偿。

## 结果契约

审批状态仍为 approved_ready；实际结果见 execution.status：

| 状态 | 含义 |
|---|---|
| succeeded | 正向步骤全部完成 |
| compensating / compensation_waiting | 补偿进行中 / 持久化等待重试时间 |
| partial_success | 本次补偿结束，仍保留组外成功成果；原任务未全部完成 |
| compensated | 已知成功步骤均已补偿；原任务未完成 |
| failed | 没有成功步骤，也没有补偿成果 |
| compensation_failed | 补偿被阻止或失败，需人工处理 |
| 原 human_required / retry_exhausted 等 | 正向结果未知时保留原分类，Saga 标记 blocked_unknown_outcome |

forward_status 保存触发 Saga 的原始失败分类。steps[].status/items 保留原始执行事实，
不因补偿成功而把失败项改为成功。HTTP 207 必须覆盖全部请求 ID，不能重复、遗漏或夹带
外部 ID；状态和成功/失败计数必须一致。全失败批次不会标记为部分成功。
响应结构不可靠时按未知结果处理。

summary 分别列出 completed、retained、compensated、failed、not_started、
unknown、compensation_unknown、manual。这些不是互斥集合：
一个部分成功批次可同时出现在 completed 和 failed。
retained 表示“尚未确认撤销的成功步骤”，必须结合 compensation_unknown 判断；
它不承诺远端当前状态。compensation_executed 只表示发起过补偿调用。

## 可靠性与安全边界

每次补偿先向 OPA 检查对应撤销动作，再保存意图、次数、当前调用者，然后调用供应方。
补偿键固定为 undo: + SHA256(正向幂等键)，单步初次加最多三次重试，
预算和 next_attempt_at 跨重启保留。同审批使用 PostgreSQL advisory lock，避免并发推进。

供应方 /v1/compensations 仅接收原操作和原键，在租户事务锁内读取原收据；
不接受客户端 before-image。先比较所有受影响对象是否仍等于原收据 after，
再一次性补偿，状态、补偿收据、操作日志原子提交。退款按原订单金额增加当前余额，
不会覆盖其他订单造成的余额变化。成功退款后响应丢失，由原补偿键重放确认。
已补偿的键再次 dry-run 返回 409 compensation_already_applied，避免伪称仍可预演。

补偿是语义撤销，不能抹除历史或收回已发生的邮件/通知。账号、邮箱、席位停用，
订单/退款留档；不可逆工单保留并标记人工处理。对象比较不能识别“改动后完全恢复原值”
的 ABA 历史，真实供应方适配器应使用版本号/ETag 和自己的幂等契约。

没有后台调度器：长等待或进程中断需重新 POST execute / 点击恢复。
人工终态不会因重复执行重置预算；目前提供记录和核对提示，没有人工对账结案接口。
外部真实系统、分布式 exactly-once、定时权限回收不属于本次完成范围。
旧 W7 密文计划缺少 Saga 字段时按原哈希校验、保留旧行为，不追溯改变已批准语义。
