# agent.md — 项目架构与借鉴来源

> 本项目：`/data01/tsn/code/tsn_files/deepagents-mini-agent`
> 定位：**无框架手写的 mini coding agent**（GLM，走 anthropic 兼容代理）。从
> deepagents + langchain 迁移而来，协议只有一套、循环只有一个、工具只有一种形态。
> 配套文档：`docs/architecture.md`（架构与组件全量文档）、`docs/interview-v2.md`（架构叙事与迁移复盘）。

## 1. 当前架构

```
agent.py          组装层:.env + SYSTEM_PROMPT + build_agent(workdir)
tui.py            UI 层:rich Live 流式渲染 / 会话恢复 / cron / 无头模式(-p)
eval/main.py      评测:evals/cases.jsonl 用例 → 临时目录跑 agent → 断言判分
core/
  loop.py         ★ agent loop:聚合 → 派发工具 → 回填 → 循环(约110行)
  model.py        模型薄封装:流式 SSE 转发 / 非流式补全(压缩用)
  tools.py        Tool 统一形态 + 五内置工具(ls/read_file/write_file/edit_file/execute)
  subagent.py     task 子代理(递归在构造处掐死)
  summarize.py    上下文压缩(改写式, 80k 触发, 保尾 20 条)
  sessions.py     会话持久化:Anthropic 原生 dict 直接 JSON 落盘
  mcp.py          MCP 工具加载(单次连接即用即走)
  skills.py       skills 清单注入 system prompt(渐进披露)
  messages.py     消息块辅助(text_of 等)
```

三条贯穿性约定（改代码前先懂这三条）：

| 约定 | 内容 |
|---|---|
| **单协议** | Anthropic 原生消息格式是唯一协议：模型吃它、会话存它、回放原样带回，零转换层 |
| **错误即文本** | 工具崩溃不抛异常，统一转 `"错误: ..."` 塞回 tool_result，让模型自愈 |
| **虚拟根** | 文件工具以 workdir 为虚拟根（`/agent.py` = `workdir/agent.py`），execute 是真实 shell；规则写进 SYSTEM_PROMPT 不做路径重写 |
| **账本当场接** | 真实 usage 只在流式聚合时能拿到（message_start/message_delta），聚合时顺手记账；展示用真实值，est 粗估只做压缩触发兜底 |
| **压缩是 postflight** | 对话完（每轮响应后）用真实 prompt_tokens 判定触发（hermes 语义）；防抖（降幅<10%连续两次停）和熔断（摘要失败即停）不可拆，防抖必须同口径 est vs est |
| **MCP 快速认输** | pypi 不通的环境里 uvx 会卡几分钟：probe 整体 wait_for 15s、工具调用 60s，超时跳过；stdio 子进程 stderr 收 devnull（MCP_STDERR=1 透传）；TUI 界面先起、MCP 后台补（build_agent(with_mcp=False) + add_tools），except 不写 BaseException 防 Ctrl-C 被吞 |
| **.env 随仓库走** | 用户明确要求：内网私有仓库，.env(含密钥)直接提交，方便多机部署；不要往 .gitignore 加 .env、不要 git rm --cached .env（曾自作主张被纠正） |

常用命令：

```bash
uv run tui.py              # TUI
uv run tui.py -p "..."     # 无头单次
uv run eval/main.py [name]      # 评测(用例在 evals/cases.jsonl)
```

## 2. 借鉴来源（对照表）

### 2.1 deepagents（迁移起点 + SDK 参考）

- 仓库：`/data01/tsn/code/deepagents`
- SDK 本体：`libs/deepagents/deepagents/`（`middleware/`、`backends/`）
- 分析文档：`/data01/tsn/code/tsn_files/deepagents-架构说明/ARCHITECTURE-CAPABILITIES.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| 能力面：五文件工具 + execute + task + skills + 压缩 | `middleware/filesystem.py` 等 | `core/tools.py`、`core/subagent.py`、`core/skills.py` |
| edit_file 核心语义：count + 唯一性校验 + replace | `backends/filesystem.py:521` | `core/tools.py` `_edit` 精确分支 |
| task 子代理语义：子代理无 task、只回传结论 | `middleware/subagents.py` | `core/subagent.py` |
| skills 渐进披露：清单常驻、SKILL.md 按需读 | `middleware/` skills 部分 | `core/skills.py` |
| 反面参照：压缩用非破坏 offload(依赖 checkpointer) | `libs/deepagents/.../summarize` | 我们选**改写式**(`core/summarize.py`)，因 sessions/*.json 直接覆盖写无 checkpointer |

框架本身（LangGraph/middleware/checkpointer）已被**砍掉**——这是本项目的核心决策：依赖协议实现，不依赖编排框架。

### 2.2 dcode = deepagents-code（成品形态参照）

- 位置：`/data01/tsn/code/deepagents/libs/code/deepagents_code/`（deepagents 仓库内的成品编码 Agent，`dcode` 命令）
- 分析文档：`/data01/tsn/code/tsn_files/deepagents-架构说明/dcode-AGENT-能力与配置.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| "一处装配"的成品组装方式(它 `agent.py:3605` create_deep_agent) | `libs/code/deepagents_code/agent.py` | `agent.py` 的 `build_agent()` |
| 成品功能面：TUI + 会话恢复 + 模型配置 + task/skill/mcp | `tui/` `sessions.py` `model_config.py` | `tui.py`、`core/sessions.py`、`.env` |

### 2.3 pi（工具与评测的直接借鉴源）

- 仓库：`/data01/tsn/code/pi`（`@earendil-works/pi-coding-agent`，TS monorepo）
- 工具代码：`packages/coding-agent/src/core/tools/`；引擎：`packages/agent/`
- 分析文档：`/data01/tsn/code/tsn_files/pi-架构说明/PI-AGENT-工具架构分析.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| **edit 批量编辑**：edits[] 全部对原文定位、按位置排序、重叠检测、逆序套用 | `coding-agent/src/core/tools/edit-diff.ts` | `core/tools.py` `_edit` 精确分支 |
| **edit 模糊匹配兜底**：NFKC/智能引号/破折号/行尾空白归一化重试；命中后只回写覆盖到的行，未动行保留原字节 | 同上 `normalizeForFuzzyMatch` / `applyReplacementsPreservingUnchangedLines` | `core/tools.py` `_fuzzy` / `_overlay` |
| 极简内核哲学：内置工具极少，其余外置(它 8 个内置+extension，我们 5 个内置+MCP/skills/task) | `packages/coding-agent/src/core/tools/` | 整体设计取向 |
| 评测风格：JSONL 用例(seed/query/checks) → 判分 | `packages/evals/` | `eval/main.py` + `evals/cases.jsonl` |

### 2.4 PenguinHarness（平台型参照，非代码借鉴）

- 仓库：`/data01/tsn/code/penguin-harness`（TS monorepo，`packages/core` 是引擎）
- 分析文档：`/data01/tsn/code/tsn_files/penguin-harness-架构说明/ARCHITECTURE.md`

| 对照点 | 它 | 我们 |
|---|---|---|
| 单协议贯穿 | OmniMessage 同时是接口/Trace/引擎消息 | Anthropic 原生消息同时是模型输入/落盘格式 |
| 文件即状态 | `~/.penguin/data` 追加式 JSONL Trace | `sessions/*.json` 直接覆盖写 |
| 引擎零业务 | core 不认识厂商 SDK 和 UI | `core/loop.py` 不区分工具来源、UI 用事件元组解耦 |

### 2.5 hermes-agent（进阶功能参照）

- 仓库：`/data01/tsn/code/hermes-agent`（Python，个人 AI agent：CLI/消息网关/TUI/桌面同核）
- 分析文档：`/data01/tsn/code/tsn_files/hermes-agent-analysis/`

| 参照点 | 它在哪(约) |
|---|---|
| 技能自动更新(skills 生命周期/更新机制) | 见 `hermes-agent-analysis/09-skill-update-mechanisms.md` |
| 看板/子代理协作(subagent + a2a + kanban) | 见 `hermes-agent-analysis/10-subagent-a2a-kanban-botmode.md` |
| 定时任务(cron/scheduled jobs) | `cron/`、`gateway/` 相关模块 |
| 窄腰设计:核心不加工具,能力走 CLI+skill/插件 | 根 `AGENTS.md` "The Footprint Ladder" |

## 3. 后续参考入口

- 想改 loop / 压缩 / 工具行为 → 先读 `docs/architecture.md`
- 想看 edit 模糊匹配的完整原版 → `/data01/tsn/code/pi/packages/coding-agent/src/core/tools/edit-diff.ts`
- 想对比"带框架的同一套能力" → `/data01/tsn/code/deepagents/libs/deepagents/deepagents/`
- 架构问答口径（面试/评审）→ `docs/interview-v2.md`
