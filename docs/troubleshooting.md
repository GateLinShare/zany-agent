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
