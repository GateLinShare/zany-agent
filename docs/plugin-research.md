# 插件化调研：master 基座 + develop 工作插件化

> **状态：调研，未实现。** 参照系是 DeepSeek 官方 agent harness（下称 DSH，本地
> `/data01/tsn/code/deepseek-harness`，2026-11 检出）。回答两个问题：DSH 的
> "万物即插件"怎么做的；zany-agent 该抄哪部分——把 master 当基座、develop 的增量做成插件。
> DSH 部分结论经抽查核对（cordis 行数 / workspace 包数 / architecture.md 原文）。

## 0. 结论先行

1. DSH 把"可替换"做成了全局架构原则：内核只剩一个 2811 行的 DI/事件框架（Cordis），
   其余 20.7 万行 TS 全是插件，连 agent loop、模型接入、UI 面板都能从一行 YAML 换掉。
   代价是 381 个包、15+ 核心概念、框架本身要自维护（vendor 里攒了 25 条本地补丁）。
2. zany-agent 不需要那套全量机制。develop 相对 master 的增量（~2647 行，35 个文件）
   实际只有两类：**宿主应用**（feishu gateway + docker，包裹 agent 会话，不在 loop 内）
   和 **loop 内能力**（browser 工具、peers 协作）。
3. 建议的最小方案：**不做通用插件框架**，沿用已有的三条缝（extras 依赖分组 /
   `add_tools` 工具注册 / MCP 外挂），新增一个 ~30 行的 plugins loader 画在 §3.2，
   是否落地等真实需求再定——与 AGENTS.md "不做通用 hook 系统，等第 2 个特判" 同一条纪律。

## 1. DSH 的"万物即插件"机制拆解

### 1.1 内核：Cordis

- 定位：**分层服务容器 + 类型化事件总线 + effect 生命周期**，fork 自
  cordiverse/cordis 4.0.0-rc.11，vendor 进仓库自维护（`vendor/cordis/src/`，7 文件
  2811 行；`vendor/README.md` 记录 25 条 V-编号本地改动——框架自维护成本的真实样本）。
- 插件声明三种形态（`docs/cordis-tutorial/01-first-plugin.md`）：
  `apply(ctx, config)` 函数 / 带 apply 的对象 / `class X extends Service`。
- 依赖：插件导出 `inject: ['llm']` 数组，**装载顺序由服务可用性驱动**，不是列表顺序
  （`docs/cordis-primer.md`）。
- 卸载/热更：所有注册（prompt 段、tool schema、事件监听）都走 `ctx.effect()`/`ctx.on()`
  返回 disposer，卸载逆序回放；`vendor/hmr` 插件 watch 配置与模块，纯配置变更不重挂。

### 1.2 插件物理形态与组装

- 插件 = **npm 包**。workspace 约定有 constraints 强制校验（`docs/cookbook/adding-a-package.md`）：
  `packages/<group>/<pkg>/`、`type: module`、cordis 必须同时出现在 peer+devDeps、
  附 `locale/en.json|zh.json` 展示元数据。
- package.json 的 manifest 字段：`dsh.bundle.patch`（指向 cordis.patch.yml）或
  `dsh.profile`（列出 bundle 栈）；client 插件加 `dsh.client`。
- 组装三层：**profile**（命名组合：web/headless/sdk/acp 模板）→ **bundle**（一个包
  = 一层 patch）→ **patch**（YAML 行按 id 定位、整行替换或 insert）。多层 last-write-wins，
  `dsh --dump-config` 可打印最终组合树（`docs/architecture.md`）。

### 1.3 "万物"的范围

| 类别 | ctx key | 代表包 |
|---|---|---|
| **agent loop 本身** | `ctx.agentLoop` | `packages/core/agent-loop`（2130 行，可整体替换） |
| 模型接入 | `ctx.llm` | llm / llm-deepseek / llm-retry / token-meter |
| 工具 | `ctx.tools` | core/tools、tool-todo、tool-skill、tool-terminal |
| 会话/存储 | `ctx.sessions` | session、session-log-*（JSONL+zstd 分代） |
| skills | skill registry | `packages/skill/*` |
| UI 面板 | client 插件 | `packages/client/ui-*` 约 30 个 |
| 压缩/沙箱/FS/shell/凭证/telemetry/jobs/goals/subagent/MCP/LSP | 各占一个 key | compaction-basic、fs-local、bash-sandbox、credentials-local… |

连"装插件、启停插件"本身都是插件（`packages/boot/plugin-manager`，带 tools.ts，
模型可调）。全仓库 381 个 workspace 包。

### 1.4 插件间通信

- 服务注入：`inject` 声明依赖，运行期 `ctx.llm.xxx()` 直调。
- 事件：TS declaration merging 声明事件名，5 种派发模式——`emit`（观察）、
  **`waterfall`（around 中间件，监听者拿 `(args, next)`，不调 next 即短路，用于拦截）**、
  `parallel`/`serial`/`bail`。三个事件域：`session/*`（持久事实）、`agent/*`
  （live 拦截点，如 `agent/pre-step` 改写/拒绝消息）、能力事件 `tools/*`、`fs/*`、
  `llm/stream`（`docs/architecture.md`）。
- 作用域：子 Context / preset `isolate` realm 把注册限定到单 agent/单会话。

### 1.5 代价账单（为什么不能照搬）

- 381 包 / **20.7 万行 TS**（不含构建产物）/ 53 个分组；一个"可换能力"要设计
  Definition / Provider / Consumer 三角色；核心概念 15+（Service、Context、Fiber、
  inject、effect、isolate、patch、bundle、profile、HMR、5 种 dispatch…）。
- 连 vendor 框架都在持续打补丁（25 条），等于背了一个自研框架的维护成本。
- 收益端确实真实：每层可从配置替换（fs+subprocess 指向远程 sandbox，Bash/PTY/LSP
  一起搬走）；组合可被 agent 自己管理；卸载语义严格可逆。但这些都建立在
  "产品线多、生态开放、快速迭代"的前提上——zany-agent 当前只有 1 个内核 + 2 个宿主。

## 2. master 与 develop 的现状分歧（2026-11-03，git diff 数据）

develop 独有 58 个提交（含大量 merge），文件级增量 35 个文件 +2647/-40 行：

| develop 增量 | 行数 | 性质归类 |
|---|---|---|
| `gateway/`（feishu.py 236 + cron 148 + main 71 + router 120 + run.sh） | ~620 | **宿主应用**：ws 长连接、每会话 agent、cron 三模式，包裹 agent 而非进入 loop |
| `core/browser.py` | 238 | **loop 内工具**：playwright 浏览器（navigate/click/type） |
| `core/peers.py` + `docs/multi-agent.md` | 78+112 | loop 内工具：peer 子代理协作 |
| `core/serve.py` + `test_serve.py` | 128+96 | 宿主：headless serve 入口 |
| `core/tools.py` +42、`agent.py` +7、`tui.py` +14、`core/messages.py` +6 | ~69 | 上述功能的接线（工具注册/消息占位符） |
| `docker/` 7 文件 + `.dockerignore` | ~120 | 打包：网关容器化 |
| `install_browser.sh` + `assets/chrome-headless-shell zip` | 33+120MB | 打包：浏览器内核离线资产 |
| `pyproject.toml` | +4 | playwright 进主依赖；`[project.optional-dependencies] feishu` extra |
| `docs/feishu-integration.md`、`architecture.md→architecture-dev.md`(+174)、`troubleshooting.md` | ~470 | 文档跟随功能 |
| `tests/test_gateway.py` | 216 | 测试 |

**master 已有的"缝"盘点**（关键：不需要新造的）：

- `build_agent(workdir, with_mcp=True)` + `core/mcp_loader.py`：外部工具进程化通道已在
  （MCP 后台补挂），任何"多一个工具"的需求理论上都能走 MCP 外挂而非改内核。
- `[project.optional-dependencies]`：依赖级插件已是 uv extras 标准形态（develop 的
  feishu extra 就是）。
- 风格包 `styles/*.md` + `PROMPT_PACKS`（常驻文本）、`skills/` 渐进披露（按需技能）：
  prompt 层的"插件通道"已在 AGENTS.md 成文，两条通道不许混。
- `core/loop.py`（288 行）**零 hook、零特判**：goal 投影、压缩触发全在 loop 外
  （tui/gateway 侧）。这是最值钱的现状，别破坏。

## 3. 方案：master 基座，develop 增量按两类归位

### 3.1 先校准概念：zany-agent 的"插件"应是两层，不是 DSH 的一层

DSH 的 profile/bundle 两层实际对应的是**宿主形态**（web/headless/sdk/acp）和
**能力组合**。zany-agent 同构映射：

| DSH 概念 | zany-agent 对应 | 现状 |
|---|---|---|
| profile（宿主组合） | tui.py / gateway/main.py / serve.py | 三个宿主已天然分层 |
| bundle/patch（loop 内能力） | 工具注册 + extras 依赖 | 已有 `add_tools` 缝，缺"开关" |
| 插件市场/HMR/Fiber | —— | 不需要（见 §3.4） |

### 3.2 能力层插件（browser、peers 这类）：一条新缝就够

master 收编能力代码但**不收编依赖**，即"代码在基座、依赖在 extra、开关在 .env"：

- `core/browser.py`、`core/peers.py` 合入 master；pyproject 把
  `playwright` 挪进 `[project.optional-dependencies] browser` extra。
- `build_agent()` 增加一个参数（或读 `.env` 的 `ZANY_PLUGINS=browser,peers`），
  loader ~30 行：逐个 `importlib.import_module(f"core.plugins.{name}")`，调其
  `register(agent)` → 内部走既有 `add_tools()`；**import 失败按"错误即文本"原则降级
  为该工具缺失，不崩启动**。
- 依赖关系天然由 uv.lock 的 extra 表达，master 默认 `uv sync` 装不上 playwright，
  零重依赖；gateway 容器 `uv sync --all-extras`。
- 目录落点建议 `core/plugins/`（每插件一文件，`register(agent)` 单一约定），
  不建 registry 类、不建 manifest 文件——`.env` 开关 + Python import 就是全部仪式。

```python
# core/plugins/__init__.py 草图（约 30 行，仅示意，未实现）
def load_plugins(agent, names: list[str]):
    for name in names:
        try:
            mod = importlib.import_module(f"core.plugins.{name}")
            mod.register(agent)          # 内部调 add_tools / 注 prompt 段
        except ImportError as e:         # extra 没装 → 工具缺席，不崩
            log(f"插件 {name} 不可用: {e}")
```

### 3.3 宿主层（gateway/serve/docker）：不做成 loop 插件

gateway 包裹着多会话、cron、飞书协议，是 DSH 里 profile 级的东西（`dsh web` 之于
core 的关系），硬塞进 agent loop 的插件缝是类别错误。两个可选落点，等拍板：

- **A. 独立仓** `zany-agent-gateway`：依赖 master（git 依赖或 pip 装 base 包），
  gateway/serve/docker 全部迁出 develop。最彻底，master/develop 双分支矛盾自然消解。
- **单仓双宿主**：master 收 `gateway/` 代码但默认不装 feishu extra（feishu extra
  留 develop 或也上 master——它只是两个纯 py 依赖）。与"gateway 只在 develop"
  的既有决定冲突，需要用户重新拍板。

### 3.4 明确不搬清单（DSH 的教训直接用）

| DSH 机制 | 为什么不搬 |
|---|---|
| Fiber 树 + isolate realm | 单进程单会话场景不存在 |
| HMR/热卸载 | Python 重启即达；gateway 重启本来就是常规操作 |
| profile/bundle/patch 三层组合 | 一层 `.env` 开关 + extras 够；配置行 patch 是为 381 包服务的 |
| plugin 市场 / plugin-manager | YAGNI，连 DSH 自己也是 boot 层插件而非内核义务 |
| Schemastery / manifest 仪式 | 5 个内置工具的规模不配拥有 manifest |
| 5 种事件派发 | 真要用 hook 时先只做 `emit`+`serial` 两种；且按 AGENTS.md 纪律，等 loop 出现第 2 个真实特判再抽 |
| vendor 自研框架 | Cordis 2811 行的教训：框架层一旦自维护就停不下来 |

### 3.5 若采纳，迁移顺序（每步独立可回退，均未实施）

1. master 加 `core/plugins/` loader 草图 + `ZANY_PLUGINS` 开关（不改任何现有行为）。
2. browser/peers/serve 迁入，依赖进 extras；develop 的对应文件删除、改为配置启用。
3. 宿主层二选一（独立仓 or 单仓双宿主），gateway 迁移。
4. 双分支合并策略收尾：develop 若只剩宿主层，考虑降级为 feature 分支甚至归档。

## 4. 开放问题（等拍板）

1. **双分支还要不要**：插件化的终局是 master 一条线 + 可选 extra，develop 的存在
   意义只剩 gateway；若走 3.3 独立仓方案，develop 可归档。
2. `.env` 随仓库走的约定下，`ZANY_PLUGINS` 默认值各 checkout 不同（gateway
   开 browser+feishu，TUI 机不开），.env 分叉管理要不要立规矩。
3. peers 协作（a2a/看板）是能力插件还是宿主能力——它有自己的常驻进程语义，
   倾向先不迁，单独评估。
4. 本调研只覆盖代码结构；**评测/文档（architecture-dev 与 architecture.md 的分叉）
   的归一策略**未展开，跟随 3.3 的拍板。
