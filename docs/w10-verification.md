# W10 验证报告：OTel + Langfuse + Trace 回放

验证日期：2026-10-25。结论：本周功能已实现，以下本地验证全部通过。保留仓库已有 W4–W9 未提交工作，未执行提交或重置。

## 验证结果

| 验证 | 实际结果 | 命令 / 证据 |
| --- | --- | --- |
| Python 单元测试 | 362 passed，67 integration deselected | `uv run pytest -m 'not integration' -q` |
| PostgreSQL 集成与迁移往返 | 67 passed；head → base → head 成功 | `uv run python -m scripts.verify_integration` |
| Vue 前端测试 | 16 passed，3 files | `npm --prefix apps/web test` |
| TypeScript 与生产构建 | 通过 | `npm --prefix apps/web run build` |
| OPA 策略 | 14/14 passed，格式检查通过 | `docker exec caseflow-opa opa test /policies -v` |
| Ruff 静态与格式 | 全部通过 | `uv run ruff check .` / `uv run ruff format --check .` |
| W10 完整端到端 | 16 组检查通过 | `uv run python -m scripts.w10_observability_demo --check --browser-check --langfuse-check` |
| W9 真实 HTTP + 浏览器回归 | 16 组通过 | `uv run python -m scripts.w9_memory_demo --check --browser-check` |
| W8 真实 HTTP 补偿回归 | 16 组通过 | `uv run python -m scripts.w8_saga_demo --check` |

完整 W10 运行于 11:46:10（北京时间）完成。机器证据：[w10-check.json](../tmp/w10-check.json)。
各脚本创建隔离数据库及 API / MockCorp 进程，结束后清理。Langfuse 独立本地 Compose 服务保留。

## 16 组端到端断言

1. 规划五节点、审批三节点、策略与执行使用同一 Trace，并保留节点结构。
2. 两次 429 重试、提交后响应丢失及幂等重放在工具帧中可见。
3. LangGraph 审批中断标为 waiting，正常等待不标成失败。
4. 检索结果中保留 chunk ID 与字符偏移，原文不进入观测记录。
5. 本地 HTTP 模型测试服务返回 input=100 / output=20 tokens；按 2 / 4 USD 每百万 tokens 估算为 0.00028 USD。
6. 本地回放记录不包含测试员工姓名、账号、邮箱域和认证令牌。
7. 未认证 401；实习生 403；跨租户和非所有者员工 404；同租户管理员可读。
8. Trace 端点无写接口，POST 返回 405。
9. 连续回放三次，供应方操作日志数量保持不变。
10. API 重启后帧记录保持一致，重复执行已完成申请没有新增业务副作用。
11. 余额不足产生 partial_success，补偿 Span 可见，已保留与已撤回业务结果符合 W8 语义。
12. 供应方提交后强杀 API，持久开始帧保留，未结束数量至少为 2。
13. 新进程追加到同一 Trace，通过原幂等键恢复；旧的未结束帧不伪造为成功。
14. 真实 Edge 浏览器验证播放/暂停、逐步切换、模型详情、390px 手机无横向溢出、退出清理和 GET-only 网络访问；无页面异常。
15. 真实 Langfuse 收到 OTLP 后，通过公开查询 API 确认 Trace 和原本地 Span ID 均已落库，非仅检查接收 HTTP 200。
16. Langfuse generation 实际返回 input=100 / output=20 / total=120；再次检查敏感字段未泄漏。

## 实际 Trace 证据

| 场景 | Trace ID | 记录结果 |
| --- | --- | --- |
| 重试后成功 | `4ecf82024d40f9ab544c64d076f2b9f8` | 初次回放 35 帧、3 个失败尝试、0 未结束；业务最终成功 |
| 部分成功与补偿 | `ac93f4a76f3e0eede329a05b3b790204` | 33 帧、0 未结束；订单余额不足后撤回成员关系 |
| 强杀与恢复 | `5439ed8b41066fba2ccc00b31f16c4d6` | 恢复后 33 帧、2 条原始未结束；业务恢复成功，追踪统计明确 partial |

Langfuse 查询第一条 Trace 返回 **37 observations**。比初次回放的 35 帧多 2 帧，来自重启后重复执行已完成申请时的入口与策略记录；工具没有重复调用。
初次成功场景阶段累计 14438.723 ms，全程跨度 14572.206 ms，仅作本次运行证据，不作为性能基准。

桌面与手机截图已实际查看，文字、摘要和控制按钮可读，窄屏布局正常：

![桌面回放](../tmp/w10-trace-desktop.png)

[手机截图](../tmp/w10-trace-mobile.png)

## 单元与集成补充覆盖

- OTel 父子 Span、上下文退出清理、租户不同 Trace ID 不同、重启身份稳定。
- 异常只记录类别，不导出异常正文或堆栈。
- 缺失/非法 usage、缺单价显示未知；无模型调用单独计数；嵌套耗时不重复累计。
- 观测数据库故障触发 30 秒冷却；业务照常返回，回放读取明确报 503。
- OTLP 端点不可达与本地写失败同时发生时，业务逻辑仍完成。
- W3C traceparent 到达策略 HTTP transport；真实数据库开始帧在结束前可见。
- 浏览器退出后迟到的响应不会重新填入旧身份数据。

## 验证边界

模型使用本地兼容 HTTP 测试服务，没有调用付费模型；100/20 tokens 是受控返回值，用于核对采集与计费计算，不是大模型效果评测。Langfuse 使用实际 3.131.0 本地服务及 PostgreSQL、ClickHouse、Redis、MinIO。

观测是尽力记录：数据库不可用可能缺帧，Langfuse 内存导出队列在强杀或断网时可能丢数据，没有持久补传。未结束帧代表未知，不能据此认定业务失败。生产保留期、采样、全局告警、并发吞吐和供应方内部 Span 尚未纳入本周交付。

W10 演示数据库已清理，JSON 与截图保留在本机忽略目录 tmp；要重新获得可操作的回放记录，请按 [功能说明](w10-observability.md) 启用采集并提交新申请。

## 这周存在的意义（白话）

前几周让系统学会办事、审批、重试和收拾失败后的残局。这周给这些过程装上行车记录仪。以后有人说“怎么还没办好”，你可以看到是模型慢、审批还没过、外部接口限流，还是余额不够；有人问“刚才失败后到底撤回了什么”，也能沿着记录查清楚。

它的价值是把“靠猜哪里出了问题”变成“拿记录定位问题”，还能知道模型用了多少 tokens、估算花多少钱。回放只是看录像，不会再执行一遍，所以不会因为排查问题又开一个账号或再扣一次钱。
