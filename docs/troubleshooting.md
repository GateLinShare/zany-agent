# 故障处理记录

修复过的故障与经验教训，只记过程，不进 AGENTS.md（那里只放当前行为约定）。

## 2025-XX 工具行之间冒空行（TUI）

**现象**：多个工具调用之间出现空白行，同一条 assistant 消息里的并行工具之间没有，跨消息的工具之间有。

**根因**：两层叠加（`tui.py`）——

1. 模型每条 assistant 消息都先吐 thinking，即使 `SHOW_THINKING=0` 不显示，`think_buf` 里也有内容
2. 工具回填的段定稿条件是 `if think_buf.strip() or text_buf.strip()` → thinking 缓冲非空，每次工具行前都触发 `log_render`；而 `log_render` 无条件先写 `Text()`（块间空行），此时 `_turn_renderable` 返回的是空块 `Text("")`

净效果 = 空行 + 空块。

**修复**：定稿条件改为「有可见内容才定稿」——`(think_buf.strip() and SHOW_THINKING) or text_buf.strip()`，段定稿与轮末定稿两处统一。

**教训**：缓冲区有内容 ≠ 屏上有内容，渲染层判断可见性要带上显示开关（SHOW_THINKING）一起判断。异常路径（出错现场定稿）故意不改，报错前多一行空行无伤。

## 2026-11 输入变首字母 / 回车被隔空吞（TUI ChatPrompt）

**现象**：快速打字（触发输入框拉长时更易出现）后回车提交失灵——第一下回车像被吞，再按才提交；表现为"打了一行出去的只有首字母"之类的观感。

**排查**：先用 Pilot 写复现脚本模拟 slow/fast/grow/after-grow 四场景，app 层不丢字符（含跨折行长文本、批量连打）→ 排除 Textual/终端丢字；根因收敛到 `ChatPrompt` 的 paste-burst 回车抑制窗口。

**根因**：`ChatPrompt`（参照 dcode `PasteBurstTextArea` 搬的最小子集）连击判定（字符间隔 ≤0.03s）后，enter 在 0.12s 抑制窗内判为换行不提交。但存在状态残留缺口：一次被抑制的 enter 只记时间戳不消费，非连击输入时也不清——窗口开着时用户再打一行字、慢悠悠回车，这次 enter 被上一次的"幽灵窗口"隔空吞掉。dcode 原版在非连击输入时会清掉该抑制状态，搬子集时漏了这条。

**修复**（`tui.py` `ChatPrompt`）：非连击输入时清除被抑制的 enter 状态；抑制后把计时锚点续到当前时刻（连续粘贴空行不误提交）；`clear()` 时同步重置连击状态。参数不变（0.03s 连击判定 / 0.12s 抑制窗 / 窗口 0.08s）。

**教训**:参照实现搬"最小子集"时,状态机的清理路径(负分支)和触发路径同样重要--只搬判定不搬清理,状态会跨回合泄漏。测试用确定性时间戳直接驱动内部状态(`tests/test_prompt.py`),比真实 sleep 稳。

## 2026-11 Windows 上 anthropic 导入 5-6s(启动慢)

**现象**:Windows 侧 `zany` 启动慢;`uv run python -c "import anthropic"` 暖缓存也要 5.7-6.4s(py3.12 临时 venv 更慢:冷 26s / 暖 8s)。

**排查**:`-X importtime` 拆解 -- self 时间全部极小(最大 0.1s),但 3175 个模块均摊 ~1.3ms/模块;对照组 stdlib `email` 同样 ~1.2ms/模块,C盘(uv python)与 D盘(venv)无差,排除 .pyc 未编译(`__pycache__` 存在且更新)、排除 Python 版本(3.12 反而更慢)、排除 anthropic 解析本身。

**根因**:整机文件打开开销被安全软件过滤驱动放大(`fltmc filters` 实证):`Estdlock`(亿赛通)+ `UniProtect` + `SinovatioSpy`(中孚)三道钩子叠在每次 open/stat 上,Windows Defender 反而是关的。anthropic SDK 导入面大(3175 个自动生成的 types 模块)只是把这个成本显性化:3175 × 1.5ms ≈ 5s。企业管控机器,驱动层无解,也不能也不该绕。

**缓解**(`core/model.py` + `agent.py`,工作区改动):`make_client()` 内做 anthropic 惰性导入,`ChatModel.client` 属性懒建,`import agent` 从 6s+ 降到 0.4s -- 启动不再付这笔税,成本拖到首条消息。`make_client` 签名去掉了 anthropic 返回注解(`from __future__ import annotations`),缺密钥 fail-fast 前移到 `ChatModel.__init__`。

**教训**:子进程量导入耗时先看 returncode(`python -c "import anthropic"` 从 python 子进程里 spawn 会因 venv trampoline 解析到裸解释器而 ModuleNotFoundError,0.14s 全是假象);慢导入先按每模块均摊成本分层(stdlib vs site-packages vs 跨盘),一眼分清"模块多"还是"每个文件都慢"。
