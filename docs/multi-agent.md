# 多 agent 协作：peer 机制设计文档

> 2026-XX 实现。需求来源：主代理能给"另一个 miniagent"发任务、能优化代码并重启对方；
> 对端必须是**真正的独立进程**（老代码/新代码可并存），且跨机无 SSH（只有网络可达）时也要能通信。
> 自检：`uv run python test_serve.py`

---

## 1. 一图总览

```
主代理(TUI)                         peer miniagent(独立进程)
─────────                          ─────────────────────────
peer send ────POST /message──────▶ 后台线程跑完整 loop(做完不中断)
peer check ───GET /task/<id>─────▶ {"state": "working|completed|failed", "result": ...}
peer restart ─POST /restart──────▶ execv 自我重启(载磁盘上的新代码)
主代理自己 ───restart 工具────────▶ 轮末 execv 自改自重启
```

- **peer = 独立进程 = 完整 miniagent**：可以是另一份 checkout（新旧代码并存），也可以在另一台机器上。
- **通信 = HTTP + JSON**（stdlib，零依赖）。消息语义照 Google A2A（task_id + working/completed/failed），
  但没引协议/SDK —— 哪天要换官方 a2a-sdk，只动 `serve.py` + `peers.py` 的传输层。
- **代码分发不依赖 git**：AI 用文件工具直接改对端的代码文件，重启即生效。

## 2. 功能清单

### 2.1 peer 端常驻服务 —— `core/serve.py`（入口 `uv run tui.py --serve [--port 8787]`）

| 端点 | 功能 |
|---|---|
| `POST /message {"task": "..."}` | 派任务。立即返回 `{"task_id", "state": "working"}`，后台线程跑完整 agent loop —— 任务不被主代理的轮次/打断/压缩影响，跑完才标记 completed |
| `GET /task/<task_id>` | 查进度/收结论：`working / completed / failed` + 结果文本 |
| `POST /restart`（可选 body `{"pull": true}`） | 自我重启。默认直接 execv 重读**磁盘上的当前代码**（AI 改完文件即可，不依赖 git）；`pull:true` 才先 `git pull --ff-only`（代码走 git 分发的场景），pull 失败也照常重启 |

- 每个 task 一个全新 agent 实例（hermes kanban 语义：任务级隔离，无上下文串扰）；**会话落盘**：
  `.sessions/<对端 workdir 分桶>/peer-<task_id>.json`（可追溯/可回放），落盘完成后才对外报 completed
- 任务结果表在内存，重启即丢（会话文件还在，可从盘上找回对话）
- 鉴权：Bearer token（对端 `.env` 的 `PEER_TOKEN`）。**没配 token 只绑 127.0.0.1**，配了才绑 0.0.0.0（hermes 规则）—— `/restart` 等于把机器交给调用方，是信任边界，不能裸奔。

### 2.2 主代理控制面 —— `core/peers.py`（`peer` 工具）

四个动作：

| action | 行为 |
|---|---|
| `send(name, task)` | 派任务，立即返回 task_id（异步，主代理不阻塞） |
| `check(name, task_id)` | 查进度/收结论 |
| `restart(name, pull?)` | 让对端换代码重启 |
| `list` | 列出已注册的 peer |

注册表：`workdir/peers/<name>.json`（主代理可用文件工具直接增删改）：

```json
{"url": "http://10.0.0.7:8787", "token": "与对端 PEER_TOKEN 一致"}
```

错误即文本（不抛异常），所有失败都返回 `"错误: ..."` 让模型自愈。工具只在 build_agent 挂载，
task 子代理拿不到 —— 防递归。

### 2.3 自改自重启 —— `core/tools.py` 的 `restart` 工具

**不依赖外部指令，agent 自己就能触发**：改完自身（框架）的代码后调 `restart`，本轮结束后
进程 execv 重起，新代码生效。

关键设计——**排定制，延迟到轮末**：

- 工具只往 `core.tools._pending_restart` 写一组 argv，不立即执行
- 轮末检查点：TUI 在 `run_turn` 尾（会话已落盘）、serve 在 `run_task` 收尾后 —— `execv`
- mid-turn 重起会把当前轮对话丢掉，排定制保证本轮做完、落盘完才换
- TUI 重起自动接 `-r` 最近会话，对话续上；serve 重起后继续监听同一端口

边界：
- **无头模式（`-p`）拒绝**：一轮即退，下次调用自然用新代码
- **子代理不可见**：子代理在同进程内，execv 会连坐主进程
- serve 端的 agent 也带这个工具 → peer 自己改完自己的代码也能自主重启

## 3. 典型工作流

```bash
# ① 对端起服务(哪台机器、哪份 checkout 都行)
PEER_TOKEN=xxx uv run tui.py --serve

# ② 主代理侧注册 peer
mkdir peers && echo '{"url":"http://10.0.0.7:8787","token":"xxx"}' > peers/worker.json
```

之后主代理（模型自动走工具，无需人工）：

1. `peer send(worker, "重构 parse 函数，跑通测试")` → 继续干自己的事
2. `peer check(worker, <task_id>)` → 收结论
3. 发现对端框架代码有毛病 → 文件工具直接改对端的 `.py` → `peer restart(worker)`（或对端自己调 `restart`）
4. 再 `peer send` 验证新代码行为

老代码 / 新代码并存 = 两个 checkout 各起一个 serve，注册两个 peer。

## 4. 设计决策（为什么是这个形状）

| 决策 | 理由 |
|---|---|
| peer = 独立**进程**，不是 in-process 子代理 | "优化代码并重启"只有进程语义成立：Python 不会重载已改模块；独立进程还能"做完不中断"（隔离主代理的打断/压缩） |
| 通信走 HTTP 文件式信箱，不引 A2A SDK | 同机/自有机 SSH + 文件就够；**无 SSH 只有网络可达**是这个设计的触发条件。语义照 A2A 留升级路径 |
| 重启不依赖 git | AI 直接改文件 → execv 读磁盘当前代码；`pull:true` 只是可选通道（无 SSH 机器上代码走 git 分发时用） |
| 自重启延迟到轮末 | mid-turn execv 丢本轮对话；轮末 = 会话已落盘，无损 |
| 任务表在内存 | peer 任务是短生命周期产物；跨重启要留结果时再落盘 |
| Bearer token 必配才对外 | `/restart` 等于交出机器，安全不省 |

## 5. 已知边界 / 未做

- serve 任务表重启即丢（见 4）
- peer 的多任务之间无共享上下文（每 task 全新实例）；需要续接时在 task 文本里带上下文
- peer 之间互派任务：peer 端 agent 也挂了 `peer` 工具，链路通但没有递归深度护栏（MAX_TURNS 兜底）
- 单协议约定贯穿：会话/通信都是 Anthropic 原生 dict，新旧 checkout 互通无转换层
