# miniagent 飞书接入方案（调研 + 设计）

> 2026-10-31 调研定稿。**只设计未实现**，实现时按本文件 Phase 1 清单动工。
> 参照系：hermes-agent（`/data01/tsn/code/hermes-agent`）已原生支持飞书，方案是抄它的进程模型、砍掉它的多平台复杂度。

## 0. 结论（TL;DR）

- hermes 的飞书 = **常驻 gateway 进程 + 平台 adapter 插件**，连接走 `lark-oapi` SDK 的 **WebSocket 长连接**（出站，内网免公网回调）。
- miniagent 不需要 hermes 那套 gateway（多平台注册表 / pairing / 看板），**一个常驻脚本 `feishu_bot.py`（预估 ~200 行）就是我们的 gateway**：ws 收消息 → per-chat 串行喂 `Agent.invoke` → 回发。
- 解耦铁律：`core/`、`agent.py`、`tui.py` **零改动**；飞书知识（SDK/事件/鉴权/分段）隔离在 `feishu_bot.py` 单文件；agent 不知道消息从哪来。
- 依赖走 optional extra：`feishu = ["lark-oapi==1.5.3", "websockets>=12"]`（hermes 容器同款版本，已验证）。

## 1. 调研：hermes 的飞书是怎么跑的

### 1.1 进程模型

```
飞书云 ←—WebSocket长连接(出站)—— gateway 进程(hermes gateway run, 常驻)
                                ├─ feishu adapter(SDK/事件/鉴权, 平台知识全在这)
                                ├─ platform_registry(注册表, 多平台并列: feishu/dingtalk/wecom/slack...)
                                └─ session 路由(chat_id → agent 会话) + cron 投递(FEISHU_HOME_CHANNEL)
```

飞书不是 tui 里的功能，是独立进程持有平台连接、收事件、路由会话、发回复。tui 关了 gateway 照跑。

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
| **A. 独立常驻进程 `feishu_bot.py`** | **选它**。TUI / headless / bot 三个并列前端，互不牵连 |

## 3. 方案 A 设计

### 3.1 架构

```
飞书云 ←—ws长连接(出站, lark-oapi)—— feishu_bot.py (常驻进程, miniagent 的 gateway)
                                     │ 唯一认识飞书的地方: SDK/事件/鉴权/gating/分段
                                     │ per-chat 串行队列(同会话排队, 异会话并行)
                                     ▼
                                 build_agent(workdir) + Agent.invoke(messages)
                                 core/ 零改动 —— agent 不知道飞书存在
                                     │
                                     ▼
                           core/sessions 落盘恢复(state: chat_id → session 文件)
```

### 3.2 解耦论证

1. **core 不动**：`Agent.invoke(messages)` 是"文本进、messages 出"的纯内核接口（`tui.py -p` 已在这么用），飞书只是第三个调用方。单协议约定原样成立：飞书文本 ↔ Anthropic messages，无中间格式。
2. **飞书知识单文件隔离**：SDK import、事件解析、allowlist、@gating、去重、分段全在 `feishu_bot.py`；删掉它项目其余部分零感知。
3. **会话复用现成机制**：每个飞书会话（私聊会话 / 群 oc_xxx）= 一个 Agent 实例 + 一条 `sessions.py` 会话文件。压缩 / token 账本 / 恢复全部白嫖；state 文件只存 `chat_id → session path` 做重启续聊（`load_session_data` + `agent.restore(usage)`）。
4. **工作目录 = 能力边界**：`FEISHU_WORKDIR` 显式指定 agent 虚拟根，不配不启动。

### 3.3 运行模型

- **并发**：每会话一个 worker 线程 + `queue.Queue`（同会话严格串行——Agent/messages 非线程安全；异会话并行）。ws 回调只做解析+入队，不跑 agent。
- **授权**：`FEISHU_ALLOWED_USERS` 白名单，**默认拒绝**。群聊只响应 @bot（mentions 命中 `FEISHU_BOT_OPEN_ID`），私聊全响应。⚠️ miniagent 的 execute 是真实 shell——白名单用户等于能在该机器跑命令，内网自用可以，**不要暴露公网**。
- **反馈**：收到消息先回"⏳ 处理中"（40 轮工具循环可能几分钟），完成回正文；超长按 ~4000 字符分段。
- **去重**：飞书事件会重推，按 message_id 近期窗口（~200 条 deque）去重。
- **指令**：`/new` 丢弃当前会话上下文，下条消息开新 session 文件。

### 3.4 配置（.env，随仓库走）

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_ALLOWED_USERS=ou_xxx,ou_yyy    # 必配, 空=全部拒绝
FEISHU_WORKDIR=/data01/tsn/code/tsn_files/miniagent   # agent 虚拟根
FEISHU_BOT_OPEN_ID=ou_xxx             # 群 @ 判断; 未配=群聊不响应(私聊不受影响)
# FEISHU_HOME_CHANNEL=oc_xxx          # Phase 2 cron 落点
```

## 4. 分期

| 期 | 内容 |
|---|---|
| Phase 1 | 常驻 bot：文本进→跑 agent→结果回；/new；白名单+群 gating+去重+分段 |
| Phase 2 | 主动投递：croniter 到点把 prompt 跑完发给 `FEISHU_HOME_CHANNEL`。注意现有 `cron_jobs.py` 是"注入 REPL"语义，bot 进程不适用，挂 bot 进程内实现 |
| Phase 2 可选 | 图片/文件收发（hermes adapter 有现成语义可抄） |
| 将来 | 第二渠道（钉钉/企微/webhook）出现时再抽 `channels/` 最小接口（on_message→run→reply），现在不抽 |

## 5. 改动清单

| 位置 | 动作 |
|---|---|
| `feishu_bot.py`（新增） | 唯一核心文件，~200 行 |
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

```bash
uv sync --extra feishu
# .env 补 FEISHU_* 后:
nohup uv run feishu_bot.py   # 或挂 s6
```

验证：私聊发"你好" → ⏳ + 回复；群里 @bot；`/new` 后上下文清空；重启 bot 进程 → 发"继续刚才的"能接回上一会话。
