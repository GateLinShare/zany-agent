# deepagents-mini-agent 项目架构

基于 PyPI `deepagents>=0.7`（当前 0.7.19）的极简对话式编码 agent，
模型走 GLM（anthropic 兼容代理，`Authorization: Bearer`）。

## 文件职责

| 文件 | 职责 |
|---|---|
| `agent.py` | 构建 agent：模型 / 工具 / skills / MCP / 中间件栈。唯一入口 `build_agent(workdir)` |
| `tui.py` | 唯一入口：无参起 Textual TUI（流式输出、/resume 弹层）；`-p` 无头模式供后台/脚本调用（见下） |
| `mcp_tools.py` | 读同目录 `mcp_servers.json` 加载 MCP 工具；`config_stamp()` 供热重载检测 |
| `sessions.py` | 会话落盘/恢复：`sessions/*.json`，langchain `message_to_dict` 序列化 |
| `cron_jobs.py` | cron 定时任务：调度线程只入队，主循环消费后当普通输入跑一轮 |

## agent 组装（agent.py）

```
init_chat_model("anthropic:GLM模型", default_headers=Bearer)
  ↓
create_deep_agent(
    backend=LocalShellBackend(root_dir=workdir),   # 文件工具虚拟根: /agent.py = workdir/agent.py
    tools=load_mcp_tools(),                        # MCP 工具混入
    skills=SKILL_SOURCES,                          # 工作目录下 /skills、/.claude/skills
    middleware=[
        FilesystemMiddleware(backend, tools=TOOLS),        # ls/read_file/edit_file/write_file/execute
        MemoryMiddleware(backend, sources=["/AGENTS.md"]), # 长期记忆: 每轮 invoke 前把 AGENTS.md 注入 system prompt
        SummarizationMiddleware(model, trigger=80*1024 tokens, keep=20),  # 见下
    ],
)
```

要点：

- **默认子代理开着**：`task` 工具派 general-purpose 子代理独立跑子任务（自带
  文件工具 + skills + 独立压缩），适合量大可并行的活（批量评测、大范围检索）。
  不想要时用 `register_harness_profile` + `HarnessProfile(general_purpose_subagent=enabled=False)` 关掉。
- **压缩换成了 langchain 原版** `SummarizationMiddleware`（触发 80k token，保留最近 20 条）。
  deepagents 默认压缩是"非破坏性"的（原文 offload 到 conversation_history/，状态存私有字段
  `_summarization_event`，跨进程恢复依赖 langgraph checkpointer）；本项目没有 checkpointer，
  持久化靠 sessions/*.json 覆盖写，所以用直接改写 messages 的原版——压缩结果天然落盘，
  恢复不会爆上下文。原始全量记录仍在历史 sessions 文件里。
- **路径约定**：文件工具的路径以 workdir 为虚拟根（`/agent.py`），`execute` 是真实 shell。
  SYSTEM_PROMPT 里写明了，防止模型拼真实绝对路径踩 "File not found"。
- **system prompt 不入历史**：MemoryMiddleware 每次调模型时现拼 system prompt
  （SYSTEM_PROMPT + AGENTS.md 内容），用完即弃，不写进 messages，不会重复累加；
  AGENTS.md 改完下一轮生效。写入由模型用 edit_file 维护。

## 无头模式（tui.py -p）

```
uv run tui.py -p "问题"              # 新会话跑一轮, 落盘, stderr 报 session id
uv run tui.py -p -r <id> "问题"      # 接指定会话继续(原地续写)
uv run tui.py -p -r "问题"           # 接最近一个会话
cat prompt.txt | uv run tui.py -p    # prompt 走 stdin
```

工单 watcher 等后台调用走这条路径；下一轮把 stderr 里的 session id 传回 `-r` 即可多轮续聊。

## 持久化与恢复

- 每轮 invoke 后覆盖写 `sessions/<时间戳>.json`（首轮创建）；崩溃最多丢当轮。
- `/resume`（chat.py）/ `ResumeScreen`（tui.py）列出最近 10 个会话，恢复后**原地续写原文件**。
- 恢复时回显历史：human/ai 全文，工具消息跳过。

## 中间件来源对照

### PyPI deepagents 0.7.19 自带（可直接 import）

`deepagents.middleware/` 下的：

| 中间件 | 作用 |
|---|---|
| `FilesystemMiddleware` | 8 个文件工具 + execute（本项目只用 5 个，且关掉 task 子代理） |
| `SubAgentMiddleware` / `AsyncSubAgentMiddleware` | task 子代理（本项目关闭） |
| `MemoryMiddleware` | AGENTS.md 记忆注入 system prompt（**在用**） |
| skills 支持 | `create_deep_agent(skills=...)` 每轮重扫目录（**在用**） |
| summarization | deepagents 默认压缩 `_DeepAgentsSummarizationMiddleware`（非破坏性，需 checkpointer；本项目换掉了） |
| `permissions.py` | 文件工具权限（FilesystemPermission） |
| `_prompt_caching.py` | Anthropic prompt cache 断点 |
| `_message_eviction.py` / `_overflow_clip.py` / `unsupported_content.py` / `_video.py` | 消息裁剪 / 超限外置 / 不支持内容剥离 / 视频处理 |
| `patch_tool_calls.py` | 工具调用修补 |
| `RubricMiddleware` | rubric 评分 |

### dcode 独有（`libs/code` 的 `deepagents_code` 包，未发版，PyPI 装不到）

| 中间件 | 作用 | mini-agent 等价物 |
|---|---|---|
| `AskUserMiddleware` | agent 中途反问用户（依赖 langgraph interrupt） | 无；模型答完反问、下一条消息回复即可 |
| `ResumeStateMiddleware` | 跨进程恢复会话状态（checkpointer） | `sessions.py` + `/resume` |
| `CostTrackingMiddleware` | 成本统计 | 无 |
| `GoalToolsMiddleware` / `GoalCriteriaMiddleware` / `ReliableRubricMiddleware` | 目标设定 + rubric 评分自动迭代 | 无 |
| `ManagedMemoryGuardMiddleware` | 防 agent 覆写 AGENTS.md 托管块 | 无（无 AGENTS.md 托管块需求） |
| `PluginSkillsMiddleware` | 多来源 skills（plugins/ 等） | deepagents 自带单来源 skills |
| `CodeInterpreterMiddleware` | js_eval / QuickJS 沙箱 | 无 |
| `ShellAllowListMiddleware` | shell 命令白名单 | 无 |
| `LocalContextMiddleware` | git 状态 + 目录树注入 | 无 |
| `AsyncApprovalHITLMiddleware` + hooks | 工具审批 interrupt_on + hooks.json 生命周期钩子 | 无 |
| `compaction_middleware`（dcode 版） | 非破坏性压缩，`_summarization_event` 私有状态 | langchain 原版 SummarizationMiddleware（改写式） |
| `CodeModelRetryMiddleware` / `ToolErrorMiddleware` | 压缩内侧的最终调用重试 | 无 |
| `ExtensionRuntimeMiddleware` | 第三方 tools/middleware 注册表 | 无 |

结论：要用 dcode 独有中间件，只能把 uv 依赖指到本地 checkout
（`/data01/tsn/code/deepagents/libs/code`），或等其发版。
