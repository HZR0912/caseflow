# W9：三类记忆 + 组织模型

本次需求称“第七周”；按项目设计方案，“记忆系统（三类）+ 组织模型”对应 W9，
所以沿用 W9 文件名，不覆盖 W7 执行器和 W8 Saga 的交付记录。

## 这周存在的意义，用白话说

以前系统像一个每次见面都不认识你的办事员。你办到一半退出，下次还得重讲；
每次申请都要重新告诉它常用仓库、邮箱域；它也不知道你属于哪个部门、谁带新人、
哪个系统归谁负责。

现在给它三本不同的笔记：

1. **这件事的草稿本**：记住你已经确认的人、时间和要求。中断后能接着补，不必从头填。
2. **你的常用设置本**：你明确说“以后这样办”，才记住常用部门、仓库和权限偏好。
   下周再来仍能用；这次你说了新要求，就听这次的。
3. **公司的通讯录和分工表**：知道部门上下级、导师、负责人和系统归属，
   也能参考同部门以前办过的申请结果。

这样能少问重复问题，少填重复信息，减少找错仓库、找错负责人的情况。
但笔记可能过时，所以允许修改、过期和遗忘，而且每次申请都能看出资料来自哪里。
“记住你以前申请过管理员权限”只是记住偏好，不能替你获得权限，也不能替审批人点同意。

## 实际交付

| 类型 | 保存内容与使用方式 | 边界 |
|---|---|---|
| 会话记忆 | LangGraph + PostgreSQL checkpoint 保存确认槽位，带版本和最近 32 次提交摘要；重启后按会话 ID 恢复 | 30 天固定有效期；原始员工信息加密，公开响应脱敏；不保存自由文本聊天记录 |
| 个人长期记忆 | 常用部门、邮箱域、代码仓库、工单项目、仓库权限；显式确认、去重、版本控制、向量召回和衰减 | 不把名单、费用、期限自动变成长期偏好；按 tenant + owner 隔离 |
| 组织记忆 | 部门树、成员归属、主管链、导师、系统负责人；递归 CTE 查询和上级默认值继承 | 管理员维护完整组织快照；普通员工可读；组织职位不改变认证角色 |
| 历史决策与类似办理 | 个人最近 20 个已决申请、同部门最近 20 个已决申请，附执行终态及保留/补偿/失败/人工数量 | “类似”当前限定为同部门、已有入职流程；不自动复用旧批准或生成新权限 |

两类历史来自既有审批与执行记录，不另存一套原始工单正文。
规划快照包含个人 history 和 organization_history，便于后续审查。

优先级为 **本次明确输入 > 选中的会话版本 > 个人长期偏好 > 组织默认值**。
显式 null / 空列表也视为修正，不从旧记忆偷偷补回；必要信息因此缺失时重新追问。
默认不使用任何记忆；新申请需勾选相应来源。

最终合并的事实仍经过制度检索、OPA、计划校验、MockCorp 预演和人工审批。
提交时冻结来源、偏好版本、会话版本、组织版本和历史摘要；之后修改或遗忘记忆不会
改写已提交的审批。执行阶段继续按原冻结计划并重新授权。

## 启动与操作

首次安装依赖后，在仓库根目录执行：

    docker compose up -d postgres opa
    docker compose restart opa
    uv run alembic upgrade head
    Push-Location apps/web
    npm ci
    npm run build
    Pop-Location
    uv run python -m scripts.w9_memory_demo --serve

演示启动独立数据库、API 与 MockCorp，打印 /approvals 地址、临时凭据及会话 ID。
进入“记忆与组织”：读取个人偏好；恢复示例会话；查看部门、导师和系统负责人；
勾选要带入新申请的记忆。审批详情的“本次使用的记忆来源”展示提交时的数据。
管理员展开“编辑组织资料”可按版本替换组织快照。Ctrl+C 清理演示服务与数据库。

普通服务须配置稳定的 EXECUTION_ENCRYPTION_KEY 和可信 AUTH_CREDENTIALS；
会话与 W7 加密执行计划共用配置的密钥。未配置密钥时会话写入返回 503，
密钥变化或密文绑定不一致时读取失败，不退回空白状态。
跨设备恢复需要同一身份和会话 ID；浏览器不持久化 token 或会话内容。

## API 契约

全部 /v1/memory 入口需要 Bearer token。tenant、owner 和认证角色均来自服务端身份，
请求体不接受客户端伪造这些字段。

| 方法与路径（前缀 /v1/memory） | 用途 |
|---|---|
| GET /preferences | 我的偏好，包括已过期条目，便于管理 |
| PUT /preferences | key、value、confirmed、expected_version、ttl_days；未确认返回 not_saved |
| DELETE /preferences/{key}?expected_version=N | 按版本遗忘值和向量；不影响别人 |
| GET /search?q=文本 | 可用偏好的向量相关度 × 时间衰减排序 |
| GET /history | 本人的已决申请与执行结果摘要 |
| GET /history?department_id=frontend | 同租户、同部门办理摘要，需要 organization.read |
| POST /sessions | 新建 30 天会话，返回 id 和 version=0 |
| GET /sessions/{id} | 恢复脱敏槽位、版本、缺失问题、最近提交摘要 |
| POST /sessions/{id}/turns | turn_id、expected_version、request、可选 clear_fields |
| DELETE /sessions/{id} | 删除会话所有权行及对应三类 checkpoint 数据 |
| GET /organization | 当前租户完整组织目录和版本 |
| PUT /organization | expected_version、departments、members、systems，原子替换 |
| GET /organization/context?department_id=frontend | 部门祖先、默认资源、负责人、建议导师与主管链 |
| POST /purge-expired | 物理清理本人过期偏好、会话及 checkpoint |

偏好 key 白名单：department_id / email_domain / repo_id / project_id / permission。
TTL 默认 180 天，可选 1–365 天。确认的重要性为 0.9，未确认 0.2，
写入阈值 0.6；读取分数 = importance × 0.5^(已过去天数 / 90)，分数低于 0.2
或超过 TTL 不参与召回。读操作和相同值重放不刷新确认时间。
重新确认已过期值需当前版本。对仍有效的相同值提交只去重，不用于延长保留期。

偏好更新与会话提交都带乐观版本；并发仅一个成功，另一个返回 409，
同一 turn_id、同一负载重试可重放。会话最多保留最近 32 个提交 ID；
更早的重复提交会因旧 expected_version 拒绝，不会再应用一次。
偏好遗忘后保留只有身份、字段名和修订号的版本元数据，防止旧删除请求误删重建的新值；
不保留旧值或向量。遗忘不删除已有审批、执行审计或备份中的历史副本。

规划/审批请求增加可选 memory：

~~~json
{
  "text": "请准备入职环境",
  "today": "2026-10-25",
  "facts": { "permission": "read" },
  "memory": {
    "session_id": "由创建会话接口返回的 UUID",
    "session_version": 1,
    "use_preferences": true,
    "use_organization": true
  }
}
~~~

会话版本变化后旧选择返回 409，重新读取并确认新版本后再提交。
不存在/非本人会话均返回 404；会话过期返回 410；非法字段、组织环、未知引用、
重复部门负责人或重复系统默认值返回 422。

## 当前边界

- 向量实现为 hashing-v1-128，实际使用 pgvector 余弦排序。它是可离线复现的词面向量，
  不宣称具有专业语义模型的近义理解能力或已验证语义召回率。
- 组织采用关系表和递归 CTE；没有 Neo4j，也尚未接入企业 HR / SSO 自动同步。
  部门最多 100 个、成员最多 1000 个，递归展示深度上限 100 层。
- 保存的是明确确认的结构化事实与摘要，尚无自由对话的全自动长期记忆抽取。
- 过期立即停止使用；物理清理由本人显式调用，没有后台清理调度器。
- 记忆与组织不会扩展现有流程模板，也不表示 MCP、观测、全流程评测等后续周次已完成。

验证证据见 [W9 验证报告](w9-verification.md)，取舍见
[ADR-0011](design-decisions/0011-scoped-memory.md)。
