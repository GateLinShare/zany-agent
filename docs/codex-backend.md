# 飞书 → codex backend 设计（gateway 插拔层）

> 2026-11-06 设计定稿，未实现。目标：飞书消息改由 codex 执行，一行 env 切回 zany。
> 结论先行：**gateway 加一个 backend 插拔点，codex backend = subprocess 调 `codex exec`**。
> 不引入新协议、不动 router/feishu 渠道、不动 zany core。

## 0. 已验证的事实（2026-11-06 实测）

- codex-cli 0.135.0 已装（`/usr/bin/codex`），`~/.codex/config.toml` 已配 linkapi 中转 + gpt-5.6-sol + auth.json，可直接跑。
- `codex exec --json "..."` 输出 JSONL 事件流：
  ```
  {"type":"thread.started","thread_id":"01a1a96d-…"}     ← 会话 id 从这拿
  {"type":"item.completed","item":{"type":"agent_message","text":"收到"}}  ← 最终回复
  {"type":"turn.completed","usage":{"input_tokens":…,…}}
  ```
- `codex exec resume <thread_id> --json "…"` 续会话：thread_id 不变、上下文接上（input_tokens 13702→27424 实测）。
- codex 原生读 workdir 的 **AGENTS.md**——与 zany 同一份项目记忆，零成本对齐。

## 1. 方案取舍

| 方案 | 判断 |
|---|---|
| A. gateway 加 backend 层，codex 走 `codex exec` 子进程 | **选定**。worker 本来就是"文本进/文本出"，子进程天然满足；切换=改一行 env 重启 |
| B. codex `mcp-server` 挂进 zany 当工具 | 对话主体还是 zany，多一层、多花钱，不是"直接跟 codex 对话"。否 |
| C. 飞书渠道单独对接 codex（绕开 gateway） | 白名单/去重/分段/会话队列/cron 全部重写。否 |

## 1.1 数据流（插拔点唯一）

```
飞书 → router.submit(chat_id, text)          [现有, 不动]
          │ per-chat 串行队列                  [现有, 不动]
          ▼
      backend.run_turn(workdir, chat_id, text, fresh) -> str   ← 唯一插拔点
          │
   ┌──────┴──────────────────────┐
   ▼ AGENT_BACKEND=zany(默认)     ▼ AGENT_BACKEND=codex
   读 feishu-<chat_id>.json       查 ~/.zany/codex_threads.json 取 thread_id
   build_agent().invoke()         有 → codex exec resume <id> --json …
   save_session()                 无 → codex exec --json
   (现有逻辑原样挪入)                  -C GATEWAY_WORKDIR --skip-git-repo-check
   │                                  -s workspace-write  (exec 模式默认不审批, 无人值守)
   │                              thread.started → 记 thread_id
   │                              item.completed(agent_message) → 正文
   └──────────┬───────────────────┘
              ▼
      channel.send(chat_id, reply)     [现有, 分段/⏳提示全复用]
```

## 1.2 关键语义

- **切换 = `.env` 改 `AGENT_BACKEND=codex` + `gateway/run.sh restart`**。两边会话各存各的
  （zany: `~/.zany/sessions/feishu-<chat_id>.json`；codex: `~/.codex/sessions/` rollout 文件），
  来回切互不污染、零迁移、不丢历史。
- **codex 线程映射**：`~/.zany/codex_threads.json` 存 `{chat_id: thread_id}`，per-chat worker
  串行保证同 chat 不会并发 resume 同一 thread。`/new` = 删映射条目（codex 分支下 zany 会话文件本就不存在）。
- **cron 零改动**：`submit()` 不感知 backend，cron prompt 同样喂给 codex；`fresh=true` 在 codex
  分支 = 不带 resume 直接 `codex exec`（天然新会话）。
- **信任模型不变**：白名单用户仍等于能在该机器跑命令（zany/codex 一样），内网自用，不暴露公网。
- **沙箱**：`-s danger-full-access` + `approval_policy=never`（飞书无人值守没法点审批；与
  zany execute 直跑 shell 同一信任模型——白名单=root）。要收紧给进程设 `CODEX_SANDBOX=workspace-write`。
- **CODEX_HOME 默认共享** `~/.codex`（复用 auth/config；会话混存无所谓，resume 按 id）。
  要隔离给 gateway 进程配独立 `CODEX_HOME` 即可，代码不感知。
- **超时**：`CODEX_TIMEOUT`（默认 900s），超时 kill 子进程，回"⏱ codex 超时"。
  gateway 重启时在跑的 codex 子进程变孤儿跑完落 rollout——会话不丢，下次 resume 可接。
- **错误即文本**：非零退出/解析失败 → `⚠️ codex: <stderr 尾部>` 回飞书，不崩 worker。

## 1.2.1 切到 codex 后与 zany 的差异清单（预期内）

| 项 | zany | codex backend |
|---|---|---|
| AGENTS.md | ✅ | ✅ codex 原生读 |
| USER.md 个人记忆 | ✅ 自动载入 | ❌ 不注入（codex 有自己的 memories 机制；真要共享再加 prompt 前缀） |
| 压缩/token 账本 | ✅ 80k 触发 | codex 自带 auto_compact（config 已配 200k）；usage 只打日志不记账 |
| goal 模式 / restart 插件 / MCP / skills 渐进披露 | ✅ | ❌（codex 有自己的 skills；MCP 在 codex config.toml 里配） |
| cron 任务 | ✅ | ✅ 零改动 |
| 图片收发 | ❌ | 预留：`codex exec -i <file>` 原生支持，Phase 2 |

## 2. 改动清单（预计 ~90 行）

| 位置 | 动作 |
|---|---|
| `gateway/backends.py`（新增 ~70 行） | `make_backend()` 按 `AGENT_BACKEND` 返回 `run_turn`；zany 实现 = router._run_one 的 agent 段原样挪入；codex 实现 = subprocess + JSONL 解析 + threads 映射 + 超时 |
| `gateway/router.py` | `_run_one` 瘦身为调 backend.run_turn（逻辑挪走不重写） |
| `.env` / env.example | `AGENT_BACKEND=zany`（默认不变，不配=zany）；`CODEX_TIMEOUT=900` |
| `tests/test_gateway.py` | 补 codex backend 纯逻辑单测：JSONL 解析（取 agent_message/thread_id）、threads 映射读写、`/new` 清映射（mock subprocess） |

不做：注册表/BaseAdapter 抽象（两个 backend 用 duck typing 的 `run_turn` 函数够了，同 feishu-integration §3.1 的判断——第二个 backend 到位才值一个 if）；per-chat 混跑 backend（要按群分再说）。

## 3. 分期

- **Phase 1（本设计）**：backends 插拔 + codex 文本轮 + /new + 超时。文本进→codex 跑→回复，切换回 zany 改一行 env。
- Phase 2 可选：流式（事件流增量 → 飞书消息编辑）、图片（`-i`）、codex usage 日志、`codex exec resume --last` 按 cwd 兜底。

## 3.1 审批桥调研（2026-11-06 实测，未实现，用户拍板太复杂不建）

让 codex 审批发飞书、用户回 y/n 的链路需换 `codex app-server`（JSON-RPC 长驻进程，experimental），踩坑结论留存：

- 协议链路通：`initialize`(clientInfo) → `thread/start`(cwd/approvalPolicy/sandbox; threadId 在 **response.result.thread.id**) → `turn/start`(threadId + input[{type:"text",text}]) → 事件流(item/completed 的 agentMessage.text) → turn/completed。
- 审批 server→client request：`item/commandExecution/requestApproval`，params 带 command/cwd/threadId；回 `{"id":<同id>,"result":{"decision":"accept"}}`。
- **decision 是 camelCase**：`accept`/`acceptForSession`（老协议 ReviewDecision 的 `approved`/`approved_for_session` 会被静默判拒）。
- workspace-write 沙箱依赖 **bubblewrap**，缺失时降级只读 FS（命令批了也写不进）；已 `apt install bubblewrap` 修复。档位：`untrusted`=所有非白名单命令都批（太吵），`on-request`=沙箱内自由跑、越界才请示。
- 接入难点：审批等待占 worker，飞书回复走同会话串行队列会死锁 —— 需 router 对 pending approval 短路放行。约 150-200 行，故搁置。

## 4. 验证清单（实现后）

1. `.env` 设 `AGENT_BACKEND=codex` → restart → 私聊"你好" → codex 回复（日志见 `codex exec` 子进程命令行）
2. 连发两条 → 第二条带上下文（resume 生效，threads 映射有 id）
3. `/new` → 再发消息 thread_id 变新
4. 切回 `AGENT_BACKEND=zany` → restart → 原会话历史还在（feishu-<chat_id>.json 未被 codex 分支动过）
5. cron 任务到点 → 结果来自 codex 分支
6. 白名单外用户无反应；群聊不 @ 不响应（门禁在 channel 层，backend 不感知）
