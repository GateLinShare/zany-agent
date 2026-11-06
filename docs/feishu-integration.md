# miniagent 飞书接入方案（gateway 版，调研 + 设计）

> 2026-10-31 定稿。**只设计未实现**。用户拍板：gateway 目录布局；范围 = 飞书消息 + AI 自建定时任务。
> 参照系：hermes-agent（`/data01/tsn/code/hermes-agent`）。抄它的 gateway 角色划分（常驻连接/会话路由/任务调度/渠道接入），不抄它的多平台抽象（注册表/BaseAdapter/配置体系——为 20+ 平台付的税）。

## 0. 结论（TL;DR）

- 新增 `gateway/` 目录（4 个文件，总 ~450 行），常驻进程 = **miniagent 的 gateway**：
  ws 收飞书消息 + cron 调度，两个入口汇入同一条 per-chat 队列，喂 `Agent.invoke` 后回发。
- **会话与记忆**：每个 chat 一个独立 session 文件（文件名由 chat_id 推导, 无需状态文件），
  盘为中心（读盘→跑→写盘, 重启无损）；跨端上下文靠共用记忆——bot agent 与 TUI 同一份
  `~/.zany/USER.md`（个人偏好/长期记忆）和同一 workdir 的 AGENTS.md, 不做项目/会话切换指令。
- `core/`、`agent.py`、`tui.py` **零改动**；`gateway/channels/feishu.py` 是唯一认识飞书的文件。
- 定时任务由 agent 自己经 `cron_job` 工具创建/删除（`add_tools()` 注入，闭包共享 tick 状态），
  到点结果发回创建任务的会话。三种投递模式（`gateway/cron.py` create 参数）：
  默认 = 续跑创建会话的对话历史；`chat_id` = 改绑指定会话（到点续跑那边的对话，需 bot 在场）；
  `fresh=true` = 每次触发开全新空会话执行（hermes 同款隔离语义，不读不写任何会话文件，结果仍推送到绑定会话）。
- 依赖走 optional extra：`feishu = ["lark-oapi==1.5.3", "websockets>=12"]`（hermes 容器同款版本，已验证）。
- 传输 = lark-oapi **WebSocket 长连接**（出站，内网免公网回调）。

## 1. 调研：hermes 的飞书是怎么跑的

### 1.1 进程模型

```
飞书云 ←—WebSocket长连接(出站)—— gateway 进程(hermes gateway run, 常驻)
                                ├─ feishu adapter(SDK/事件/鉴权, 平台知识全在这)
                                ├─ platform_registry(注册表, 多平台并列: feishu/dingtalk/wecom/slack...)
                                └─ session 路由(chat_id → agent 会话) + cron 投递(FEISHU_HOME_CHANNEL)
```

飞书不是 tui 里的功能，是独立 gateway 进程持有平台连接、收事件、路由会话、发回复；tui 关了 gateway 照跑。cron 也是 gateway 进程内的一个后台线程（`cron/scheduler.py` 头注释："The gateway calls this every 60 seconds from a background thread"），不是独立进程。

### 1.2 接入形态（解耦方式）

- 插件目录 `plugins/platforms/feishu/`（adapter.py 5662 行 + 评论事件 + 会议邀请），bundled 插件由 `gateway/config.py:285` 文件系统扫描自动发现，**零 core 注册代码**。
- adapter 继承 `BasePlatformAdapter`，gateway 主循环只认统一消息事件，不感知"这是飞书"。
- 飞书身份模型（三层 ID，选型用）：`open_id`(ou_, 应用级) / `user_id`(u_, 租户级) / `union_id`(on_, 开发者级)。单 bot 场景 open_id 即够；群 @ 时 `mentions[].id.open_id` 是 bot 自己的 open_id（gating 用）。

### 1.3 能力面（已验证存在，实现时抄语义）

| 能力 | 位置 / 配置 |
|---|---|
| ws / webhook 双模式 | `FEISHU_CONNECTION_MODE`（默认 websocket） |
| 私聊 + 群 @mention gating | mentions 匹配 bot open_id |
| 白名单 | `FEISHU_ALLOWED_USERS`（逗号分隔 open_id）/ `FEISHU_ALLOW_ALL_USERS`（dev only） |
| per-chat 串行队列 | adapter 内队列（对齐 openclaw createChatQueue 语义） |
| 事件去重 | 按 message_id 持久化去重（重启不重复处理） |
| 处理状态 | 进行中 reaction / 成功移除 / 失败 CrossMark |
| 超长分段 | `FEISHU_TEXT_BATCH_*`（分段字符数/延迟） |
| cron 调度 | 60s tick 轮询 + 文件锁 `.tick.lock`；任务运行用"不活跃看门狗"（每 5s 查活性，默认 10min 无活动才杀，`HERMES_CRON_TIMEOUT`） |
| cron 投递 | `FEISHU_HOME_CHANNEL`，`deliver=feishu` 任务落点 |
| 图片/文件/语音 | 上传走 `im.v1` media API |

### 1.4 SDK 关键调用（hermes adapter 已验证的姿势）

```python
# REST 客户端(发消息)
lark.Client.builder().app_id(...).app_secret(...).domain(FEISHU_DOMAIN).log_level(...).build()
client.im.v1.message.create(CreateMessageRequest.builder()
    .receive_id_type("chat_id").request_body(
        CreateMessageRequestBody.builder().receive_id(chat_id)
        .msg_type("text").content(json.dumps({"text": ...})).build()).build())
# 回复串
client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(...)...)

# WebSocket 长连接(收事件)
handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(fn).build()
WsClient(app_id=..., app_secret=..., log_level=..., event_handler=handler).start()  # 阻塞
```

参照文件（实现时对照）：
- ws 连接/事件注册：`plugins/platforms/feishu/adapter.py:4715-4739, 1637-1645`
- 发消息/回复/分段：`adapter.py:4630-4990`
- 插件接入教程：`gateway/platforms/ADDING_A_PLATFORM.md`

## 2. 方案取舍

| 方案 | 判断 |
|---|---|
| B. webhook 回调（api-server 式） | 飞书必须主动打进来 → 内网不可行，需公网入口。**否** |
| C. 嵌进 tui.py | TUI 一关 bot 就死；textual 事件循环里塞 ws 线程很脏。**否** |
| **A. 独立常驻 gateway 进程（gateway/ 目录）** | **选定**。TUI / headless / gateway 三个并列前端，互不牵连 |

## 3. gateway 设计

### 3.1 目录结构与职责

```
gateway/
  __init__.py
  main.py            # 常驻入口(~80 行): 校验配置 → 起 cron 调度线程 → 起 ws(阻塞);
                     #   SIGTERM → 停收新消息, 等当前轮(含 cron tick 中的一轮)完成后退出
  router.py          # 会话路由(~120 行): chat_id → session 文件(文件名由 chat_id 推导);
                     #   per-chat worker 线程 + Queue; submit() 是唯一入口
  cron.py            # 定时任务(~100 行): 任务存储 + 20s tick + cron_job 工具工厂
  channels/
    __init__.py
    feishu.py        # 飞书渠道(~150 行): lark-oapi SDK/事件解析/白名单/@gating/
                     #   message_id 去重/发送/回复/分段 — 唯一认识飞书的文件
```

加第二个渠道 = `channels/` 下加文件 + `main.py` 一行接线。**不做**注册表和 BaseAdapter
（一个渠道时抽象没有第二个对象；hermes 的 registry 也是长出来的）。

### 3.2 数据流（两个入口，一条队列）

```
飞书云 ←—ws长连接(出站, lark-oapi)—— gateway/main.py (常驻进程)
                                        │
        ┌───────────────────────────────┤
        ▼                               ▼
channels/feishu.py                 cron.py tick(20s 轮询)
 解析+白名单+@gating+去重            读 gateway/cron_jobs.json
        │  on_message(chat_id,          │ 到期任务: submit(chat_id,
        │              text, msg_id)    │            prompt, source="cron")
        └───────────────┬───────────────┘
                        ▼
        router.submit(chat_id, text)   ← 唯一入口
                        │ per-chat Queue(同会话串行, 异会话并行)
                        ▼
        build_agent(workdir, with_mcp=False).invoke(messages)
        core/ 零改动 —— agent 不知道消息来自飞书还是 cron
                        │
                        ▼
        core/sessions 落盘（会话文件名由 chat_id 推导, 无状态映射）
```

### 3.3 解耦论证

1. **core 不动**：`Agent.invoke(messages)` 是"文本进、messages 出"的纯内核接口（`tui.py -p` 已在这么用），gateway 是第三个调用方。单协议约定原样成立：飞书文本 ↔ Anthropic messages，无中间格式。
2. **飞书知识单文件隔离**：SDK import、事件解析、白名单、@gating、去重、分段全在 `channels/feishu.py`；删掉它 gateway 其余部分零感知。
3. **cron 与消息同构**：cron 到点不是"另起 agent"，而是把 prompt 当一条消息 `submit()` 进对应会话的队列——复用同一条串行 worker，无新并发模型；任务与用户消息天然排队不竞争。
4. **会话复用现成机制（盘为中心）**：每个 chat 一条 `sessions.py` 会话文件，文件名
   `feishu-<chat_id>.json` 由 chat_id 直接推导（无 state.json，重启零状态）。每条消息
   读盘 → invoke → 写盘，Agent/usage/压缩状态用 `load_session_data` + `agent.restore()`
   现场重建。压缩 / token 账本 / 恢复全部白嫖。
5. **工作目录 = 能力边界**：`FEISHU_WORKDIR` 单值指定 agent 虚拟根，不配不启动；
   记忆跨端共用同源于此——bot agent 与 TUI 用同一 HOME 的 USER.md、同一 workdir 的 AGENTS.md。

### 3.4 运行模型

- **并发**：router 每会话一个 worker 线程 + `queue.Queue`（同会话严格串行——Agent/messages 非线程安全；异会话并行）。ws 回调和 tick 都只做解析+submit，不跑 agent。
- **授权**：`FEISHU_ALLOWED_USERS` 白名单，**默认拒绝**；群聊只响应 @bot（mentions 命中 `FEISHU_BOT_OPEN_ID`），私聊全响应。⚠️ miniagent 的 execute 是真实 shell——白名单用户等于能在该机器跑命令，内网自用可以，**不要暴露公网**。
- **反馈**：入队后先回"⏳ 处理中"（cron 来源回"📋 定时任务 xx 触发"），完成回正文；超长按 ~4000 字符分段。
- **去重**：飞书事件会重推，feishu.py 按 message_id 近期窗口（~200 条 deque）去重。
- **指令**：仅 `/new`（丢弃当前会话上下文, 下条消息开新文件）。不做项目/会话切换。
- **会话模型（盘为中心）**：gateway 不在内存里养会话——每条消息处理时从盘上重读
  会话文件 → append → invoke → 写回；文件名由 chat_id 推导, bot 重启零状态。跨会话的
  偏好/知识延续靠记忆层（USER.md / AGENTS.md, 见 §3.7），不靠接会话历史。
- **记忆共用**：bot agent 的 system prompt 载入 `~/.zany/USER.md` + workdir 的
  AGENTS.md, 与 TUI 同一份（miniagent 既有机制, gateway 零额外代码）——TUI 里教过的
  偏好/纠正, 飞书新会话开场就记得。
- **MCP**：`with_mcp=False`（per-chat 构建 agent 时 probe 15s 不合理；需要时再后台补，同 TUI 的 add_tools 模式）。

### 3.5 定时任务（AI 自建自删）

- **存储**：`gateway/cron_jobs.json`，格式 `{name, cron, prompt, chat_id}`
  （对齐项目现有 cron_jobs.json 加一个归属字段：结果发回哪个 chat）。
  **不合并进根目录 cron_jobs.json**——那个文件是 TUI"注入 REPL"语义，TUI 起来就会读，混用会串。
- **调度**：20s 轮询（沿用自家 cron_jobs.py 节奏；hermes 是 60s tick，分钟级 cron 下间隔只影响延迟上限不影响正确性），每 tick 重读任务文件（即时生效），分钟槽去重（`(name, 分钟槽)` 集合，与 cron_jobs.py / hermes tick 同构）。
- **投递目标**：优先创建任务时的会话（群里建的发群里，私聊建的发私聊）；`FEISHU_HOME_CHANNEL` 仅作无归属任务的兜底。
- **AI 工具**：bot 侧经 `Agent.add_tools()` 给每个会话注入一个 `cron_job` 工具：
  - `action: create|delete|list`，`name`（唯一键）、`cron`（标准 5 段表达式，croniter 校验）、`prompt`
  - 工具闭包绑定当前 chat_id（per-chat agent，闭包捕获）——create 时写入该 chat_id，list/delete 按 name
  - 工具描述写明："任务到点自动把 prompt 交给本会话的 agent 执行，结果发回本会话"
  - 与 goal 模式 `update_goal`（状态真身在外层、工具闭包共享）同一已验证模式，core 零改动
- **权限**：cron prompt 也是喂 agent 的，allowlist 已覆盖，无新增信任边界。
- **看门狗（可选，hermes 参照）**：cron 任务跑 agent 按"不活跃"而非硬超时杀——hermes 每 5s 查一次活性、默认 10min 无活动才终止（`HERMES_CRON_TIMEOUT` 语义）。miniagent 暂无活动追踪器，Phase 2 可用简单轮询 agent 状态替代。
- **不硬复用根目录 `cron_jobs.py`**：只同款形状（croniter + 20s 轮询 + 分钟槽），它 inject-to-REPL 的调度循环形态与 gateway 的 run-and-deliver 不同，不值得为此抽象。

### 3.6 配置（.env，随仓库走）

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_ALLOWED_USERS=ou_xxx,ou_yyy    # 必配, 空=全部拒绝
FEISHU_WORKDIR=/data01/tsn/code/tsn_files/miniagent   # agent 虚拟根(决定 AGENTS.md/sessions 桶)
FEISHU_BOT_OPEN_ID=ou_xxx             # 群 @ 判断; 未配=群聊不响应(私聊不受影响)
# FEISHU_HOME_CHANNEL=oc_xxx          # 无归属 cron 任务的兜底投递
```

### 3.7 与 TUI 的关系

- **会话各玩各的**：bot 每 chat 独立 session 文件（`feishu-<chat_id>.json`），不接续
  TUI 会话；TUI 的 /resume 会看到 bot 的会话文件（同桶），忽略即可。
- **记忆共用（跨端上下文的唯一通道, 够用）**：两边读写同一份 `~/.zany/USER.md`
  （个人偏好/长期记忆，两边都随 system prompt 载入）和同一 workdir 的 AGENTS.md。
  TUI 里教过的偏好/纠正/项目约定，飞书新会话开场就带着——接续靠记忆不靠历史。
- **记忆文件并发**：USER.md / AGENTS.md 整文件覆盖写，两端同时改有交错风险，
  低概率接受；真出问题再加文件锁。
- **进程隔离零影响**：独立进程，内存/Agent 实例/账本全隔离；TUI 不开 gateway 照跑。
- **并发改代码**：FEISHU_WORKDIR 若与 TUI 正在改的仓库相同，飞书 agent 会抢文件——
  与开两个 TUI 窗口改同一仓库同性质，靠约定不靠锁。
- **运维**：gateway 依赖 lark-oapi，裸 `uv sync` 会把它从共享 venv 里删掉 ——
- 部署统一用 `uv sync --extra feishu`；启停用 `gateway/run.sh {start|stop|status|restart}`（§6）。

## 4. 分期

| 期 | 内容 |
|---|---|
| Phase 1 | gateway 骨架（main/router/channels/feishu）：文本进→跑 agent→结果回；/new；白名单+群 gating+去重+分段+重启续聊 |
| Phase 2 | 定时任务：gateway/cron.py（存储+20s tick+投递）+ `cron_job` 工具注入，见 §3.5 |
| Phase 2 可选 | 图片/文件收发（hermes adapter 有现成语义可抄）；cron 看门狗 |
| 将来 | 项目/会话切换指令（若真需要接续具体会话再加）；第二渠道：channels/ 下加文件 + main 一行接线 |

## 5. 改动清单

| 位置 | 动作 |
|---|---|
| `gateway/`（新增目录，4 文件 + run.sh，~460 行） | main.py / router.py / cron.py / channels/feishu.py / run.sh |
| `pyproject.toml` | `[project.optional-dependencies] feishu = ["lark-oapi==1.5.3", "websockets>=12"]` |
| `.env` | 上表 FEISHU_* |
| `core/` `agent.py` `tui.py` | 零改动 |

## 6. 启用 checklist

飞书侧（运维操作）：
1. 开放平台 → 企业自建应用 → 记 App ID / App Secret
2. 应用能力 → 开启机器人
3. 权限：`im:message`、`im:message.group_at_msg`、`im:chat`、`im:resource`（按需 `contact:user.employee_id:readonly`）
4. 事件与回调 → **使用长连接接收事件**；订阅 `im.message.receive_v1`
5. 创建版本并发布 → 管理员审核
6. 拉机器人进目标群 / 私聊

部署侧：

**启停脚本 `gateway/run.sh`**（设计，实现时随 gateway 一起落地）：

```bash
#!/usr/bin/env bash
# miniagent 飞书 gateway 启停; 进程痕迹都在 ~/.zany/ (pid/log), 项目仓库里不留运行文件
set -euo pipefail
cd "$(dirname "$0")/.."                      # 项目根, 让 uv run 找得到 pyproject
PID_FILE=~/.zany/gateway.pid
LOG_FILE=~/.zany/gateway.log

alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-status}" in
  start)
    if alive; then echo "已在运行 (pid $(cat "$PID_FILE"))"; exit 0; fi
    rm -f "$PID_FILE"
    nohup uv run gateway/main.py >>"$LOG_FILE" 2>&1 &   # uv run 是 exec 语义, $! 即 python pid
    echo $! > "$PID_FILE"; sleep 2
    if alive; then echo "已启动 (pid $(cat "$PID_FILE"), 日志 $LOG_FILE)"
    else echo "启动失败, 看日志: tail -50 $LOG_FILE"; exit 1; fi
    ;;
  stop)
    alive || { echo "未运行"; rm -f "$PID_FILE"; exit 0; }
    kill "$(cat "$PID_FILE")"                            # SIGTERM → main.py 优雅退出
    for _ in $(seq 1 15); do alive || break; sleep 1; done
    alive && kill -9 "$(cat "$PID_FILE")"                 # 趣时强杀(默认 15s 等待)
    rm -f "$PID_FILE"; echo "已停止"
    ;;
  status)  alive && echo "运行中 (pid $(cat "$PID_FILE"))" || echo "未运行" ;;
  restart) "$0" stop; "$0" start ;;
  *) echo "用法: $0 {start|stop|status|restart}"; exit 1 ;;
esac
```

```bash
uv sync --extra feishu   # 裸 uv sync 会把 lark-oapi 从共享 venv 删掉, gateway 部署固定带 --extra
# .env 补 FEISHU_* 后:
gateway/run.sh start
```

验证清单：
- 私聊发"你好" → ⏳ + 回复；群里 @bot 响应、不 @ 不响应；白名单外用户无反应
- TUI 里教 bot 一个偏好（写进 USER.md）→ 飞书新会话问相关问题时记得（记忆共用生效）
- 群里 @bot 说"每天 9 点提醒我站会" → agent 调 cron_job create → 到点发回该群（重启 gateway 后仍在）
- 机器人对 bot 说"把我 10 分钟前建的任务删了" → cron_job delete 生效
- `/new` 后上下文清空；重启 gateway 进程 → 发"继续刚才的"能接回上一会话（会话文件由 chat_id 推导）

## 实现落点（2026-10-31）

按本文档实现完毕：`gateway/main.py`（入口/配置防呆/优雅停）、`gateway/router.py`（每会话队列+worker，
盘为中心：读盘→建 Agent→跑→写盘）、`gateway/cron.py`（CronStore + 20s tick + cron_job 工具）、
`gateway/channels/feishu.py`（lark-oapi ws 封装/解析/门禁/去重/分段发送）。启动停机 `gateway/run.sh`。
依赖走 `uv sync --extra feishu`（lark-oapi + websockets）。纯逻辑单测：`uv run python tests/test_gateway.py`
（解析/门禁/分段/会话路径/cron 往返与分钟槽去重/router 端到端 mock 冒烟，8 项）。
真实联调需在 .env 填 FEISHU_APP_ID/SECRET/ALLOWED_USERS 后按上方验证清单走一遍。
