# 回滚与版本管理（本项目怎么保证"改坏了能回去"）

写这份文档的原因：**回滚能力不是 git 自带的，是提交习惯带来的。**
MigraAgent 的问题是整仓库只有一个 squash commit —— 那种历史下，git 一点用都没有，
因为唯一能回去的地方就是"什么都没做"。

本仓库从第一个 commit 起就按下面的规矩走。

## 一、日常操作：回到某一版

```powershell
cd "D:\AI item\caseflow"

git log --oneline            # 看提交序列，每个 commit 是一条可回去的时间线
git show <commit> --stat     # 这一版改了什么

# 情况 A：本地改动不要了，彻底退回某个提交（会丢弃之后所有提交）
git reset --hard <commit>

# 情况 B：只是想看看老版本，不动当前分支
git switch --detach <commit>     # 看完 git switch main 回来

# 情况 C：改动还没提交，想临时放下
git stash push -m "试一下新写法"
git stash list
git stash pop                    # 拿回来；不要了就 git stash drop

# 情况 D：已经提交了，但想撤销这次提交、保留代码改动
git reset --soft HEAD~1          # 文件改动留在工作区，commit 撤销
```

**顺序建议**：先 `git switch --detach` 或 `git stash` 看清楚，再决定要不要 `reset --hard`。
`reset --hard` 是唯一会真丢东西的命令，执行前先 `git log --oneline` 确认目标 commit。

## 二、按周打标签（便于"回到 W1 结束时的状态"）

```powershell
git tag -a w1-done -m "W1 验收通过：compose 全绿、策略门可演示、断点恢复可跨进程"
git tag                          # 列标签
git switch --detach w1-done      # 回到 W1 结束状态
```

打标签的意义：每周末演示一旦翻车，可以立刻退回"上次能演示的状态"，而不是在坏掉的分支上硬修。

## 三、实验性改动：分支而不是硬改 main

凡是"这个方向不确定，可能整个推倒"的改动（比如换一套图编排、试另一种幂等键设计）：

```powershell
git switch -c exp/saga-v2        # 开实验分支
# ... 改 ...
git switch main                  # 不满意就直接丢下，main 从未被污染
git branch -D exp/saga-v2
```

## 四、提交粒度：这是关键

本仓库的规矩是 **一个可验证的小步骤 = 一个 commit**，并且 commit message 写"为什么"。
W1 的实际历史就是范例：

```
ci,docs: CI 三 job + api 镜像 + ADR-0001 + 架构现状图
feat(db,graph): Alembic 首批表 + 最小 LangGraph 图，验证跨进程断点恢复
feat(infra): docker compose 起 postgres(pgvector) + redis + opa
feat(policy): 最小 OPA 策略 + Python 客户端 + 受策略门保护的写入口
build(deps): 引入 uv + pyproject 依赖清单
chore: 初始化仓库骨架与开发约定
```

反过来讲，如果一个改动会同时影响 5 个模块、跑不通就想全撤，那说明它本该拆成 5 次提交。

## 五、数据库也要能回滚

代码回滚了、数据库结构没回滚，是最容易忽略的一半：

```powershell
uv run alembic current           # 当前版本
uv run alembic history           # 迁移序列
uv run alembic downgrade -1      # 退一个版本
uv run alembic downgrade base    # 全退
uv run alembic upgrade head      # 回到最新
```

CI 的 integration job 每次都会跑 `downgrade base` → `upgrade head` 往返，
所以在 W1 就已经证明 0001 是可逆的。

**纪律：每个迁移必须写 `downgrade()`，不许留 `pass`。** 留空等于放弃这条回滚路径。

## 六、还没做的（诚实记录）

- 没有配 remote：仓库目前只在本地，`git push` 到 GitHub 后才能谈 CI 徽章变绿。
- 没有 pre-commit hook：靠 CI 兜底，本地是手动跑 `uv run ruff check .`。
- `main` 直推（个人项目、单人开发），没有 PR 流程。如果后面要演示"代码评审"，再开 `dev` 分支。
