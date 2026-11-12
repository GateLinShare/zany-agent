# AGENTS.md — 项目架构与借鉴来源

> 本项目：`/data01/tsn/code/tsn_files/zany-agent`
> 定位：**无框架手写的 mini coding agent**（GLM，走 anthropic 兼容代理）。从
> deepagents + langchain 迁移而来，协议只有一套、循环只有一个、工具只有一种形态。
> 配套文档：`docs/architecture.md`（架构与组件全量文档）。

## 1. 当前架构

```
core/agent.py      组装层:.env + SYSTEM_PROMPT + build_agent(workdir)
core/tui.py        UI 层:claude 式 TUI(烙定区 print 进原生滚动 + 底部活动区整帧重绘)/ 会话恢复 / cron / 无头模式(-p) / 会话标题
eval/main.py       评测:eval/cases.jsonl 用例 → 临时目录跑 agent → 断言判分
core/
  loop.py         ★ agent loop:聚合 → 派发工具 → 回填 → 循环; 空回复重试(3次)/超限强压重试兜底都在这
  model.py        模型薄封装:裸 httpx 直调 /v1/messages + 手解 SSE(不引 anthropic SDK——导入重, Windows 冷启动 6s→0.4s)
  tools.py        Tool 统一形态 + 五内置工具(ls/read_file/write_file/edit_file/execute)
  goal.py         goal 模式:续跑投影 prompt + update_goal 工具(状态真身在 tui)
  subcore/agent.py     task 子代理(递归在构造处掐死)
  summarize.py    上下文压缩(改写式, 窗口×比例触发默认 120k, 保尾 20 条)
  sessions.py     会话持久化:Anthropic 原生 dict 直接 JSON 落盘
  mcp_loader.py   MCP 工具加载(单次连接即用即走; 不能叫 mcp.py——会撞 mcp SDK 包名,
                  python core/tui.py 直启时 core/ 进 sys.path[0] 就把自己 import 成了 SDK)
  skills.py       skills 清单注入 system prompt(渐进披露; 项目 skills/、.claude/skills、.agents/skills + 全局 ~/.zany/skills, 同名先到先得去重)
  plugins/        能力插件 loader(见约定表"插件缝")
  messages.py     消息块辅助(text_of 等)
```

贯穿性约定（改代码前先懂）：

| 约定 | 内容 |
|---|---|
| **单协议** | Anthropic 原生消息格式是唯一协议：模型吃它、会话存它、回放原样带回，零转换层 |
| **错误即文本** | 工具崩溃不抛异常，统一转 `"错误: ..."` 塞回 tool_result，让模型自愈 |
| **路径不做重写** | 文件工具与 execute 同一套文件系统视图（pi/hermes 同款）：绝对路径字面直用，相对路径相对 workdir；无虚拟根、无越界 jail，约定写进各文件工具 description |
| **账本当场接** | 真实 usage 只在流式聚合时能拿到（message_start/message_delta），聚合时顺手记账；展示用真实值，est 粗估只做压缩触发兜底 |
| **压缩是 postflight** | 每轮响应后用真实 prompt_tokens 判定触发（hermes 语义）；防抖（降幅<10%连续两次停）和熔断（摘要失败即停）不可拆，防抖必须同口径 est vs est。另有超限兜底：413/超长 400 → 强压一次重试（run 限一次），preflight 用上次真实 tokens 判定覆盖不了单轮暴涨 |
| **空回复重试** | 无 tool_use/无非空 text 即空，不 append 直接重试 3 次；耗尽落哨兵文本收尾。GLM/vLLM 网关偶发吐空 content assistant，不兜底=静默中断 |
| **MCP 快速认输** | pypi 不通环境 uvx 会卡几分钟：probe 15s、工具调用 60s 超时跳过；TUI 先起、MCP 后台补挂（失败服务器每 60s 重试，5 分钟窗口，失败落盘 ~/.zany/mcp-errors.log）；工具名 `mcp__<server>__<tool>` 前缀防跨服务器重名；配置在 `~/.zany/mcp_servers.json`（不入 git，各 checkout 共享）。rea-dispatcher 代码在 develop 仓 `mcp-servers/rea-dispatcher/`（克隆即用，见 docs/mcp-setup.md；develop 独有，master 不收） |
| **模块名避让** | core/ 下文件名禁止叫 mcp.py、json.py 这类与三方包同名——`python core/tui.py` 直启时 core/ 进 sys.path[0]，会把自己 import 成 SDK（mcp.py 改名 mcp_loader.py 的教训，2026-11-12） |
| **工具防爆层** | 输出截断 200k 字符保头 40%+尾 60%（错误在头、新输出在尾）、execute 超长全量落盘给路径、ls/read 上限提示带精确值、edit BOM/CRLF 透明；行为熔断账本 `_Ledger` 挂 Tool.ledger，压缩后 `loop.reset_tool_ledger()` 清空——都是提示不是阻断，把"你在循环里"喂给模型 |
| **goal 模式** | `/goal <目标>` 设目标后宿主自动续跑(codex ext/goal 语义, 不要 judge)。状态真身是 tui 侧 dict, 每轮末尾注入投影 user 消息(压缩免疫); update_goal(complete/blocked/paused) 申报; 预算 GOAL_MAX_TURNS 耗尽转 paused, 打断/异常轮不续。落盘会话 JSON 顶层 `goal` 键; 优先级: 排队用户文本 > cron > goal |
| **回合侧账 marks** | Thought/tool 计数是运行时局部变量 → 会话文件顶层 `marks=[{at, thought, tools}]`, at=回合结束时消息条数做对齐锚；锚失配(压缩改写后)静默跳过；老会话文件无 marks |
| **密钥不入库** | `.env` 不进 git，键清单在 `env.example`（新机 `cp env.example .env` 后填密钥） |
| **TUI 对齐 Claude Code 观感** | 正文/标题 #d4d4d4, markdown 标题白粗体(覆盖点: `rich.themes.DEFAULT.styles` 单例 + `Style.parse()`，改 DEFAULT_STYLES 字典无效)；thinking 折叠 3 行(SHOW_THINKING 开全量)；轮末摘要行 dim。Win 读键踩坑(getwch 两段扩展键/ctypes restype/ConPTY 喂键诊断)见 docs/troubleshooting.md，`ZANY_TUI_TRACE=路径` 开读键落盘探针 |
| **插件缝** | `core/plugins/<name>.py` 暴露 `register(workdir)->list[Tool]`，`.env` 的 `ZANY_PLUGINS` 选用、失败降级不崩启动；`ZANY_PLUGINS_RELOAD=1` 每会话重建(先清 pyc)。插件文件只在 develop(master 是纯基座)，develop 配 `ZANY_PLUGINS=browser,peers,restart` |
| **npm 分发** | PyInstaller onefile 经 npm 裸包 `zany-agent` 分发(平台包 optionalDependencies, codex 同款)；版本单源 pyproject.toml；frozen 路径收口 core/frozen.py(只读 app_root()、可写 data_dir()=~/.zany)；发布走 Actions workflow_dispatch，见 docs/npm-release.md |
| **风格包** | 常驻行为走 `styles/<name>.md` + `.env` 的 `PROMPT_PACKS` 拼进 SYSTEM_PROMPT(含 task 子代理)——常驻文本不能走 skills 渐进披露，两条通道别混。不做通用 hook 系统，等真实需求再抽 |

git 分支约定（用户定的）：
- master 提交后 merge 进 develop 再 push 两个分支；develop 自己的提交不反向进 master；
  develop 独立工作目录 `/data01/tsn/code/tsn_files/zany-agent-develop`，push 完去那边 `git pull --ff-only`。
- **feishu gateway（gateway/ + docs/feishu-integration.md）只在 develop，master 不加**。
- 文件归属防漂移：共享核心（core/ eval/ tests/ pyproject.toml docs/architecture.md）**只在 master 改**，
  develop 靠 merge 拿；develop 独有（gateway/ core/plugins/ docker/ 等）master 不收。
- pyproject.toml 两分支不同（develop 侧有 feishu extra）：master 动过 pyproject 后 merge 进 develop，
  必须 `grep feishu pyproject.toml` 确认 extra 还在（被静默吃过一次，网关全起不来）。
- develop 开工前：`git fetch origin && git log HEAD..origin/master --oneline` 非空先 merge 再干活。
- filter-branch/历史重写必须 master+develop 同做同推，否则禁止。

常用命令：

```bash
uv run core/tui.py              # TUI
uv run core/tui.py -p "..."     # 无头单次
uv run eval/main.py [name]      # 评测(用例在 eval/cases.jsonl)
bash core/deploy/deploy.sh      # 一键部署, 见 docs/deploy.md; Windows 用 deploy.cmd
bash release/build.sh           # 构建当前平台 frozen 二进制 → release/dist/zany-<target>
bash release/install.sh         # 装进系统 PATH(zany 命令, 覆盖源码版)
```

## 2. 借鉴来源（对照表）

### 2.1 deepagents（迁移起点 + SDK 参考）

- 仓库：`/data01/tsn/code/deepagents`，SDK 本体 `libs/deepagents/deepagents/`
- 分析文档：`/data01/tsn/code/tsn_files/deepagents-架构说明/ARCHITECTURE-CAPABILITIES.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| 能力面：五文件工具 + execute + task + skills + 压缩 | `middleware/filesystem.py` 等 | `core/tools.py`、`core/subcore/agent.py`、`core/skills.py` |
| edit_file 核心语义：count + 唯一性校验 + replace | `backends/filesystem.py:521` | `core/tools.py` `_edit` 精确分支 |
| task 子代理语义：子代理无 task、只回传结论 | `middleware/subagents.py` | `core/subcore/agent.py` |
| skills 渐进披露：清单常驻、SKILL.md 按需读 | `middleware/` skills 部分 | `core/skills.py` |
| 反面参照：压缩用非破坏 offload(依赖 checkpointer) | `libs/deepagents/.../summarize` | 我们选**改写式**(`core/summarize.py`)，因 sessions 直接覆盖写无 checkpointer |

框架本身（LangGraph/middleware/checkpointer）已被**砍掉**——核心决策：依赖协议实现，不依赖编排框架。

### 2.2 dcode = deepagents-code（成品形态参照）

- 位置：`/data01/tsn/code/deepagents/libs/code/deepagents_code/`（`dcode` 命令）
- 分析文档：`/data01/tsn/code/tsn_files/deepagents-架构说明/dcode-AGENT-能力与配置.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| "一处装配"的成品组装方式 | `libs/code/deepagents_code/core/agent.py` | `core/agent.py` 的 `build_agent()` |
| 成品功能面：TUI + 会话恢复 + 模型配置 + task/skill/mcp | `tui/` `sessions.py` `model_config.py` | `core/tui.py`、`core/sessions.py`、`.env` |
| markdown 渲染：live 粗排纯文本 + 定稿整段重渲 | `tui/widgets/messages.py:1436` | `core/tui.py`（未用 Textual，取其折中思路） |

### 2.3 pi（工具与评测的直接借鉴源）

- 仓库：`/data01/tsn/code/pi`（`@earendil-works/pi-coding-agent`，TS monorepo）
- 分析文档：`/data01/tsn/code/tsn_files/pi-架构说明/PI-AGENT-工具架构分析.md`

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| **edit 批量编辑**：edits[] 对原文定位、排序、重叠检测、逆序套用 | `coding-agent/src/core/tools/edit-diff.ts` | `core/tools.py` `_edit` 精确分支 |
| **edit 模糊匹配兜底**：NFKC/智能引号/破折号/行尾空白归一化重试；命中只回写覆盖行 | 同上 | `core/tools.py` `_fuzzy` / `_overlay` |
| 极简内核哲学：内置工具极少，其余外置 | `packages/coding-agent/src/core/tools/` | 整体设计取向 |
| 评测风格：JSONL 用例 → 判分 | `packages/evals/` | `eval/main.py` + `eval/cases.jsonl` |

### 2.4 PenguinHarness（平台型参照，非代码借鉴）

- 仓库：`/data01/tsn/code/penguin-harness`
- 分析文档：`/data01/tsn/code/tsn_files/penguin-harness-架构说明/ARCHITECTURE.md`

| 对照点 | 它 | 我们 |
|---|---|---|
| 单协议贯穿 | OmniMessage 三用 | Anthropic 原生消息同时是模型输入/落盘格式 |
| 文件即状态 | 追加式 JSONL Trace | `~/.zany/sessions/*.json` 直接覆盖写 |
| 引擎零业务 | core 不认识厂商 SDK 和 UI | `core/loop.py` 不区分工具来源、UI 用事件元组解耦 |

### 2.5 hermes-agent（进阶功能参照）

- 仓库：`/data01/tsn/code/hermes-agent`（Python），分析文档：`/data01/tsn/code/tsn_files/hermes-agent-analysis/`

| 参照点 | 它在哪(约) |
|---|---|
| 兜底语义(空回复重试/overflow 强压重试/打断收尾) | `agent/conversation_loop.py` |
| 技能自动更新 | `hermes-agent-analysis/09-skill-update-mechanisms.md` |
| 看板/子代理协作 | `hermes-agent-analysis/10-subagent-a2a-kanban-botmode.md` |
| 定时任务 | `cron/`、`gateway/` 相关模块 |
| 窄腰设计 | 根 `AGENTS.md` "The Footprint Ladder" |

### 2.6 codex（OpenAI codex-rs，Rust）

- 仓库：`/data01/tsn/code/codex`——看 crate 边界怎么划；`docs/` 的 config/agents_md 写法可参照

### 2.7 claude-code（逆向产物，行为语义对照源）

- 位置：`/data01/tsn/code/tsn_files/claude-code-逆向/rea-claude/js/$bunfs/root/`（bun 打包的 minified chunk，没有源码目录，行为靠 grep 字符串定位）
- 已对照过的行为：粘贴折叠、输入框键盘语义（行尾反斜杠软换行）

| 借了什么 | 它在哪 | 我们在哪 |
|---|---|---|
| 粘贴折叠：换行 >2 或 >800 字符 → 输入框留 `[Pasted text #N +X lines]` 占位符，原文存映射，提交时还原；0 换行无行数后缀 | `chunk-18hwfzwa.js`（onPaste 分支：`$9=800` 长度阈值、`hne=min(columns-10,2)` 行阈值，来自 `chunk-n770a3wm.js`）；`chunk-s61vbvea.js`（`$W` 计行 / `sie` 造占位符） | `core/tui.py` `Editor.insert_paste()/expand()`（ctrl+v 与 bracketed paste 同一入口） |
| 超长粘贴截断：>10k 字符 → `[...Truncated text #N +X lines...]`，保头尾各 500 字符 | `chunk-18hwfzwa.js` `Xno()`（`n6e=1e4, IN=1000`） | 未借（提交展开后模型自己看全量，无需 UI 截断） |

## 3. 后续参考入口

- 想改 loop / 压缩 / 工具行为 → 先读 `docs/architecture.md`；工具层差距清单 → `docs/tools-comparison.md`
- 想看 edit 模糊匹配的完整原版 → `/data01/tsn/code/pi/packages/coding-agent/src/core/tools/edit-diff.ts`
- 故障修复经验只记 `docs/troubleshooting.md`，不进本文件（本文件只放当前行为约定）

## 回复风格
- 收尾别加"注意/建议"式叮嘱段落，结论给完即止
