# 验证报告：三类记忆 + 组织模型（方案 W9）

- 验证日期：**2026-10-25，Asia/Shanghai**。
- 对应本次用户所称“第七周”的记忆任务；文件按原设计方案 W9 编号。
- 环境：Windows、Python 3.12、Node.js、真实 PostgreSQL + pgvector、OPA、Edge。
- 结论：**本地验收通过**，已覆盖 API、数据库、规划/审批/执行链路和浏览器。
- 本轮未提交 Git、未部署线上；CI 配置已增加 W9 检查，未声称远程 CI 已运行。

## 实测汇总

| 验证层 | 结果 | 证据 |
|---|---|---|
| Ruff 静态检查与格式 | 通过 | ruff check / format --check |
| Git 空白检查 | 通过 | git diff --check |
| 全量非集成回归 | **351 passed，66 deselected** | pytest -m 'not integration' -q |
| 全量真实数据库集成 | **66 passed，351 deselected，0 skipped** | scripts.verify_integration |
| 迁移往返 | upgrade → downgrade → upgrade 通过 | 独立临时数据库，包含 0007_memory |
| W9 专项 | **19 个非集成 + 10 个数据库集成通过** | tests/test_memory*.py |
| Vue 交互 | **13 passed**，其中新增记忆交互 4 项 | npm test |
| TypeScript / Vite | 构建通过 | npm run build |
| OPA | **13/13 passed**；格式检查通过 | opa test / opa fmt |
| W9 真实 HTTP + 浏览器 | **16 项通过** | tmp/w9-check.json |
| W8 兼容回归，真实 HTTP + 浏览器 | **17 项通过** | tmp/w8-check.json |

全量回归后，收尾补充同部门历史到规划来源并调整组织表格；
再次跑过 W9 的 19 + 10 项专项、迁移往返、全部 13 项前端测试、构建、
16 项真实 HTTP/浏览器验收及 Ruff 检查。没有把环境缺失导致的跳过计为通过。

## 关键验证与实际结果

### 1. 会话能在进程被杀后恢复

真实 API 保存员工名单、岗位和日期后强杀进程，再启动新进程。
最终一次运行 PID **252416 → 258404**，会话仍为版本 1，公开恢复结果完全相同。
相同 turn_id 与负载重放不增加版本；旧版本新提交返回 409。
原始姓名不出现在 API 会话响应；数据库 checkpoint blob 测试断言姓名与账号不以明文出现。

补充数据库用例把图停在输入 checkpoint 已保存、confirm 尚未运行的位置，
新服务恢复并完成确认。错误密钥、换用户绑定、换会话绑定均拒绝解密。

### 2. 三类记忆真正进入计划，再经过原审批与执行

会话提供员工、岗位、开始/截止日期；个人偏好提供 read 权限；
组织目录提供部门、邮箱域、仓库与项目。三者组合后生成 **单人 4 步入职计划**。

未批准时执行返回 409。另一审批人批准后，先把个人偏好改为 write，
再把组织仓库改为 r_changed；原审批摘要及来源仍保持不变，
最终仍按原仓库、原 read 权限执行成功。

最终一次验收申请 ID：d796f68f-27fe-5a7b-b462-aa54983d7dfe。
摘要：bd78279dbd3e92f840ecdb102391818262deebf127c6f5d1414fd140e9e3d821。
这些 ID 来自已清理的临时演示数据库，供证据对照，不能作为线上申请链接。
机器报告保留来源映射及执行终态，不保存临时凭据。

个人历史能读到自己的 approve / succeeded；另一人的个人历史为空。
同租户同部门历史能找到该申请，且已接入规划的 organization_history；
历史结果不用于自动批准新申请。

### 3. “下周还记得”，但不会越用越不肯忘

在独立数据库中把确认时间向前调整 7 天，再通过实际 pgvector 检索读回偏好。
记录仍可用，分数从 0.9 衰减到约 **0.852764**，再次读取不刷新 confirmed_at。
数据库专项还用可控时钟验证 10 天 TTL 到第 11 天不再召回。

这里明确采用**时间模拟**，没有声称真的等待了一周。
实际年龄衰减、有效期过滤、存储后重新连接均已验证；
哈希向量的词面排序不等于经过语义模型检索质量评测。

### 4. 修正、并发、遗忘和清理

- 本次显式 read 覆盖旧 admin 偏好；显式 null 保持为空并追问，旧记忆不补回。
- 未明确确认返回 not_saved；相同有效值去重，不刷新期限。
- 两个同版本会话提交、两个同版本偏好更新，均只有一个成功，另一请求 409。
- 删除偏好后再创建使用更高修订号，旧删除请求返回 409，不能删掉新值。
- 过期会话读取返回 410，过期偏好不参与搜索。
- purge-expired 清理本人 1 条偏好和 1 个会话；
  checkpoints、checkpoint_blobs、checkpoint_writes 中对应会话数据清零。
  同租户另一用户的过期偏好仍在，未被越界删除。

遗忘只清理当前可用记忆，不删除已冻结审批与执行审计；防 ABA 的修订元数据无旧值、
无向量。备份清除策略不在本次本地验证范围内。

### 5. 组织模型与权限是两回事

实际递归查询 frontend → eng，邮箱域由上级继承，仓库/项目由当前部门覆盖；
汇报链和明确导师均返回 reviewer。循环关系、悬空引用、重复负责人和无效系统归属被拒。
组织版本过时返回 409；未知/停用部门在查询时返回冲突，不能继续使用。

普通 employee 可以保存个人偏好与读取组织，写组织返回 403。
将 employee 设为部门负责人，再保存 admin 权限偏好，入职规划仍被 OPA 拒绝。
intern 读记忆返回 403；没有凭据返回 401；伪造 tenant 字段返回 422。
另一用户和跨租户管理员都不能读取或删除本人的会话（404），也读不到个人偏好。
跨租户组织目录为空。

### 6. 浏览器与既有功能

Edge 真实连接临时 API，验证读取/确认保存偏好、恢复会话、显式勾选来源、
部门/导师/系统表格、冻结来源版本、退出清空界面、无浏览器存储残留。
检查 **1440px 桌面 / 390px 手机**，无页面水平溢出、无 JavaScript 异常，
宽表在容器内滚动；最终截图已视觉复核：

- [桌面](../tmp/w9-memory-desktop.png)
- [手机](../tmp/w9-memory-mobile.png)

W8 全流程再次通过：余额不足精确撤回、退款提交后强杀恢复、207 成功子集补偿、
补偿失败转人工和浏览器展示，**17 项通过**。本次没有把旧 W7 报告冒充新跑结果；
W7 相关单测和数据库测试包含于全量回归。

## 复验命令

仓库根目录 PowerShell：

    $env:PYTHONUTF8 = '1'
    docker compose up -d postgres opa
    docker compose restart opa
    uv run ruff check .
    uv run ruff format --check .
    git diff --check
    uv run pytest -m 'not integration' -q
    uv run python -m scripts.verify_integration
    docker compose exec -T opa /opa fmt --diff --fail /policies
    docker compose exec -T opa /opa test /policies -v
    Push-Location apps/web
    npm test
    npm run build
    Pop-Location
    uv run python -m scripts.w9_memory_demo --check --browser-check
    uv run python -m scripts.w8_saga_demo --check --browser-check

首次需要 uv sync --frozen --extra dev 与 apps/web 下 npm ci。
Linux 浏览器复验需安装 Playwright Chromium；Windows 使用已安装的 Edge。
CI 新增 W9 HTTP 检查，浏览器截图检查当前在本地完成。
脚本 finally 清理独立数据库和子进程；tmp 内机器报告、日志和截图被 Git 忽略。
本报告、验收脚本与测试代码可随仓库管理。

## 这周的意义与范围

白话说：以前每次办事都像第一次见面，你得重复介绍人、部门和常用资源。
现在系统有“这件事的草稿本”“你的常用设置本”“公司的通讯录和分工表”，
能接着上次办、少填重复信息，也更容易找到正确的资源和负责人。
记错了能改，过时了会停用，不想保留可以遗忘；真正办事前仍要检查权限、预演和审批。

当前完成的是现有入职流程中的结构化记忆闭环。
自由对话自动长期记忆、HR/SSO 同步、语义向量质量评测、后台过期清理等尚未完成；
后续 MCP、观测与全流程评测也不属于本次交付。
完整使用说明见 [三类记忆与组织模型](w9-memory.md)。
