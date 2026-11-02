# agent.md — 项目架构与借鉴来源

> 本项目：`/data01/tsn/code/tsn_files/miniagent`
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
  goal.py         goal 模式:续跑投影 prompt + update_goal 工具(状态真身在 tui)
  subagent.py     task 子代理(递归在构造处掐死)
  summarize.py    上下文压缩(改写式, 80k 触发, 保尾 20 条)
  sessions.py     会话持久化:Anthropic 原生 dict 直接 JSON 落盘
  mcp.py          MCP 工具加载(单次连接即用即走)
  skills.py       skills 清单注入 system prompt(渐进披露; 项目 skills/、.claude/skills + 全局 ~/.miniagent/skills)
  messages.py     消息块辅助(text_of 等)
```

三条贯穿性约定（改代码前先懂这三条）：

| 约定 | 内容 |
|---|---|
| **单协议** | Anthropic 原生消息格式是唯一协议：模型吃它、会话存它、回放原样带回，零转换层 |
| **错误即文本** | 工具崩溃不抛异常，统一转 `"错误: ..."` 塞回 tool_result，让模型自愈 |
| **路径不做重写** | 文件工具与 execute 同一套文件系统视图（pi/hermes 同款）：绝对路径字面直用，相对路径相对 workdir；无虚拟根、无越界 jail，约定写进各文件工具 description（2026-10-31 移除虚拟根） |
| **账本当场接** | 真实 usage 只在流式聚合时能拿到（message_start/message_delta），聚合时顺手记账；展示用真实值，est 粗估只做压缩触发兜底 |
| **压缩是 postflight** | 对话完（每轮响应后）用真实 prompt_tokens 判定触发（hermes 语义）；防抖（降幅<10%连续两次停）和熔断（摘要失败即停）不可拆，防抖必须同口径 est vs est |
| **MCP 快速认输** | pypi 不通的环境里 uvx 会卡几分钟:probe 整体 wait_for 15s、工具调用 60s,超时跳过;stdio 子进程 stderr 收 devnull(MCP_STDERR=1 透传);TUI 界面先起、MCP 后台补(build_agent(with_mcp=False) + add_tools),except 不写 BaseException 防 Ctrl-C 被吞 |
| **工具防爆层** | 截断保头 40%+尾 60%(错误在头、新输出在尾)、execute 超长全量落盘给路径、ls/read 上限提示带精确值、edit BOM/CRLF 透明;行为熔断账本 `_Ledger`(重复读去重/edit 连败升级/同命令警告)挂在 Tool.ledger,压缩后由 `loop.reset_tool_ledger()` 清空 -- 都是提示不是阻断,把"你在循环里"喟给模型 |
| **goal 模式** | `/goal <目标>` 设目标后宿主自动续跑(codex ext/goal 语义, 不要 judge)。状态真身是 tui 侧 dict(工具闭包共享), 每轮末尾 turn_finished 现场注入投影 user 消息(压缩免疫); update_goal(complete/blocked/paused) 申报状态; 预算 GOAL_MAX_TURNS(.env 可配, 默认 500) 耗尽转 paused, 打断/异常轮不续。落盘走会话 JSON 顶层 `goal` 键, /resume 捡回; 优先级: 排队用户文本 > cron > goal。测试 tests/test_goal.py |
| **.env 随仓库走** | 用户明确要求：内网私有仓库，.env(含密钥)直接提交，方便多机部署；不要往 .gitignore 加 .env、不要 git rm --cached .env（曾自作主张被纠正） |
| **TUI 对齐 Claude Code 观感** | 正文/标题 #d4d4d4, markdown 标题白粗体(覆盖点: `rich.themes.DEFAULT.styles` 单例 + `Style.parse()`, 改 DEFAULT_STYLES 字典无效——DEFAULT import 时已 copy；塞 str 会在 get_style 崩)；thinking 折叠 3 行(SHOW_THINKING 开全量)；轮末摘要行 `Thought for Xs, ran N tool calls`(dim, 前置空行) |
| **回合侧账 marks** | Thought/tool 计数是运行时局部变量, Anthropic 消息里没有 → 会话文件顶层 `marks=[{at, thought, tools}]`, at=回合结束时的消息条数做对齐锚；恢复回放按回合分组(真用户消息开组, tool_result 排除), 锚失配(压缩改写后)静默跳过不误导；老会话文件无 marks, 只有新回合才有 |
| **风格包插拔** | 常驻行为(人格/规则)走 `styles/<name>.md` + `.env` 的 `PROMPT_PACKS`(逗号分隔), `agent.prompt_packs()` 全文拼进 SYSTEM_PROMPT(含 task 子代理)——常驻文本不能走 skills 的渐进披露(模型不会每轮自觉读全文)；按需技能仍走 `skills/` 渐进披露, 两条通道别混。不做通用 hook 系统, 等真实需求(skills/MCP 表达不了、loop.py 出现第 2 个特判)再抽 |

git 分支约定（用户定的）：master 的提交要同步到 develop —— master 上提交后，
merge master 进 develop 再 push 两个分支；develop 上自己的提交不反向进 master。
AGENTS.md 同样参与同步，两分支保持一致。
develop 分支的独立工作目录在 `/data01/tsn/code/tsn_files/miniagent-develop`（master 在
`tsn_files/miniagent`）：push 完要去那边 `git pull --ff-only` 同步到最新。
**feishu gateway（gateway/ 目录 + docs/feishu-integration.md）只在 develop 实现，master 不加这些功能**（用户 2026-10-31 定）。

常用命令：

```bash
uv run tui.py              # TUI
uv run tui.py -p "..."     # 无头单次
uv run eval/main.py [name]      # 评测(用例在 evals/cases.jsonl)
bash deploy.sh             # 一键部署(fresh clone → uv/依赖/浏览器内核/全局 miniagent 命令), 见 docs/deploy.md
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
| 反面参照：压缩用非破坏 offload(依赖 checkpointer) | `libs/deepagents/.../summarize` | 我们选**改写式**(`core/summarize.py`)，因 ~/.miniagent/sessions/*.json 直接覆盖写无 checkpointer |

框架本身（LangGraph/middleware/checkpointer）已被**砍掉**——这是本项目的核心决策：依赖协议实现，不依赖编排框架。

### 2.2 dcode = deepagents-code（成品形态参照）

- 位置：`/data01/tsn/code/deepagents/libs/code/deepagents_code/`（deepagents 仓库内的成品编码 Agent，`dcode` 命令）
- 分析文档：`/data01/tsn/code/tsn_files/deepagents-架构说明/dcode-AGENT-能力与配置.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| "一处装配"的成品组装方式(它 `agent.py:3605` create_deep_agent) | `libs/code/deepagents_code/agent.py` | `agent.py` 的 `build_agent()` |
| 成品功能面：TUI + 会话恢复 + 模型配置 + task/skill/mcp | `tui/` `sessions.py` `model_config.py` | `tui.py`、`core/sessions.py`、`.env` |
| markdown 渲染策略：流式期间就用 Textual MarkdownStream 增量写(0.1s 节流)+结束后整段重渲(绕 textual#6518) | `tui/widgets/messages.py:1436` AssistantMessage | 我们取折中：live 粗排纯文本(`_turn_renderable(md=False)`) + 定稿 rich Markdown 渲染(`tui.py:130`) |
| 对照坑：textual 8.x RichLog.write 的 scroll_end(immediate=False) 与 virtual_size reflow 竞态，新内容常沉在视口下 —— ChatLog.write 覆盖后补 scroll_end(immediate=True) 兕底 | textual `_rich_log.py:282` | `tui.py` ChatLog 覆盖 write；.env SHOW_THINKING(默认关)控制 thinking 是否上屏，数据层始终全量记录 |

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
| markdown 渲染：pi-tui 自研 Markdown 组件(marked+highlight.js→ANSI)，每 delta 全量重渲、未闭合 fence 裁剪防闪烁 | `packages/tui/src/components/markdown.ts:236` | 对照参考：若流式也要渲染，先看 dcode 的 MarkdownStream 节流方案 |

### 2.4 PenguinHarness（平台型参照，非代码借鉴）

- 仓库：`/data01/tsn/code/penguin-harness`（TS monorepo，`packages/core` 是引擎）
- 分析文档：`/data01/tsn/code/tsn_files/penguin-harness-架构说明/ARCHITECTURE.md`

| 对照点 | 它 | 我们 |
|---|---|---|
| 单协议贯穿 | OmniMessage 同时是接口/Trace/引擎消息 | Anthropic 原生消息同时是模型输入/落盘格式 |
| 文件即状态 | `~/.penguin/data` 追加式 JSONL Trace | `~/.miniagent/sessions/*.json` 直接覆盖写 |
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

### 2.6 codex（OpenAI codex-rs，Rust）

- 仓库：`/data01/tsn/code/codex`（Rust, OpenAI）
- codex-rs 按 crate 切分（core/exec/tui/protocol/skills/apply-patch...），看 crate 边界怎么划
- `docs/` 的 config/agents_md 写法可参照

## 3. 后续参考入口

- 想改 loop / 压缩 / 工具行为 → 先读 `docs/architecture.md`；工具层差距与优化清单 → `docs/tools-comparison.md`（pi/hermes 逐工具对比 + 按性价比清单，2025-XX 排期用）
- 想看 edit 模糊匹配的完整原版 → `/data01/tsn/code/pi/packages/coding-agent/src/core/tools/edit-diff.ts`
- 想对比"带框架的同一套能力" → `/data01/tsn/code/deepagents/libs/deepagents/deepagents/`
- 架构问答口径（面试/评审）→ `docs/interview-v2.md`
- 插件化调研（master 基座 / develop 增量插件化，未实现）→ `docs/plugin-research.md`
- 故障修复经验只记 `docs/troubleshooting.md`，不进本文件（本文件只放当前行为约定，用户定的）

## 回复风格
- 用户反馈(2026-10-29)：收尾别加"注意/建议"式叮嘱段落，结论给完即止
