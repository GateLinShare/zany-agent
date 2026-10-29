# 上下文压缩对比：我们 vs hermes / deepagents(dcode) / pi

> 对比对象：本项目 `core/summarize.py`（约 80 行）+ `core/loop.py::_compact_step`（约 18 行）。
> 参照：hermes-agent（`agent/context_*.py`）、deepagents SDK（`libs/deepagents/.../middleware/summarization.py`）+ 成品 dcode（`libs/code/deepagents_code/offload_middleware.py`）、pi（`packages/coding-agent/src/core/compaction/`）。
> 所有对方条目均带源码位置；我方机制见 `core/summarize.py`、`core/loop.py:252-269`。

## 一、总览定位

| | 我们 | hermes | deepagents / dcode | pi |
|---|---|---|---|---|
| 一句话 | 改写式极简压缩：判定准 + 双保护，其余从简 | 压缩即数据安全工程：失败分类学 + 并发锁 + 协议级打磨 | 非破坏事件视图 + 三级 offload 降级链，深绑 langchain/langgraph | 树状 session 非破坏投影 + split-turn 双摘要 |
| 代码量 | ~100 行 | ~3000 行（context_compressor）+ 编排 | 2314 行 + 依赖 langchain 1.4.6 私有 API | ~1100 行 + session 树 |
| 核心依赖 | 无（协议原生 dict） | SQLite(state.db) + 会话轮转 | langgraph checkpointer + backend 文件 | JSONL session 树 |

## 二、逐维度对比

| 维度 | 我们 | hermes | deepagents / dcode | pi |
|---|---|---|---|---|
| **触发时机** | postflight（每轮响应后 `_compact_step`） | 三保险：turn 头 preflight + 工具循环中 pre-API + postflight 真实 usage（`conversation_loop.py:4746`） | preflight（`wrap_model_call` 每次调用前判定，`summarization.py:1487`）+ provider overflow 兜底重试 | postflight 为主 + preflight 兜底（`prepareNextTurn`，`agent-session.ts:906`），手动独立入口 |
| **token 口径** | 真实 `prompt_tokens` 优先，None 时 est(chars/3) 兜底（`maybe_summarize`） | postflight 只信 API 真实 `prompt_tokens`（显式排除 completion 防思考模型虚高）；preflight 字符粗估 len/4 | 双轨：近似计数 OR 最后一条 AI 消息的真实 `usage_metadata`（需 provider 匹配） | 真实 `usage.totalTokens` 优先，缺失时 chars/4 兜底；上下文被编辑过则全量重估不信旧值 |
| **阈值** | `窗口(131072) × 0.6 ≈ 78.6k`，.env 两参数可配 | `(窗口 − max_tokens) × 0.5`，给输出预留；config.yaml 全套可配 | 模型 profile 有 `max_input_tokens` → 85% fraction；否则固定 170k | `窗口 − reserveTokens(16384)`；settings 可配、**支持按模型覆盖** |
| **防抖/防误触** | 压缩后 est 降幅 <10% 连续 2 次 → 放弃（同口径 est-vs-est，`loop.py:268`） | 同款 <10%×2 防抖 + 每轮压缩硬上限 3 次 + 粗估噪声 defer 到真实值 | 无防抖，靠"压完自然低于阈值"幂等；compact 工具有 50% 资格门 | 无百分比防抖；消息时间戳早于最近压缩边界则跳过（防旧 usage 误触） |
| **压缩方式** | **改写式**：messages 原地替换为 `[user(摘要)] + 尾部`，旧对话消失（全文留在旧 session 文件） | 改写式替换 + **无 LLM 前置 pass**（旧 tool_result 换单行摘要）；原文随会话轮转留父 session | **非破坏事件视图**：`_summarization_event` 是 lens，raw messages 全程不动；原文 XML offload 到 `/conversation_history/*.md` | **树上插入 CompactionEntry**，原文 entry 全留盘，投影时省略；切分支可拿回完整历史 |
| **保留策略** | 保尾 20 条；切点开头是 tool_result 则后移（防孤儿） | 头部保护 `protect_first_n=3`（**跨轮衰减为 0** 防化石）+ 尾按 token 预算；锚定最后真实 user/assistant 消息；双侧边界对齐 + `_sanitize_tool_pairs` 兜底 | `keep`（默认 20 条或 10% token）；cutoff 落 ToolMessage 时**回退找配对 AIMessage**；system 不参与 | 保尾 20000 token；**永不切 toolResult**，切 turn 中间回溯 user 起点；system 快照随 entry 存 |
| **摘要 prompt** | 一句话指令：保留目标/约束/关键操作与结论/未完成/文件路径 | 结构化 11 节模板（Active Task 逐字、Blocked 带原始报错、Pending 标 STALE），要求时间锚定、同语言、密钥 REDACTED；有旧摘要走**迭代更新式** | langchain 默认：SESSION INTENT / SUMMARY / ARTIFACTS；deepagents 注入媒体引用说明 | 固定 checkpoint 结构（Goal/Constraints/Progress/Decisions/Next Steps）；有旧摘要走 PRESERVE+ADD+UPDATE 合并式；尾部追加文件操作清单 |
| **大工具结果** | `_flat` 一维化时截前 500 字（仅摘要输入瘦身），无独立 offload | 压缩窗口外旧 tool_result 换信息性单行摘要（无 LLM）；图片：最新含图消息之前的图全换占位符 | **三级**：老消息 args>2000 字符截断 → 结果>20k token offload 换 stub → overflow 尾部 clip；图片 data-URL 上传换引用 | 摘要输入内截 2000 字符；超长由 read 工具自身截断负责；不做落盘 offload |
| **thinking 块** | `_flat` 直接丢弃 | 计入尾部预算但**从不修改**、不进摘要输入（加密 reasoning 块） | 未确认特殊处理 | 显式剔除，注释写明理由：回放 thinking 常不在实时上下文，会撑爆摘要请求 |
| **失败处理** | 熔断：摘要失败 1 次 → `_compact_dead` 停（`loop.py:263`），错误转 `compact_error` 事件，不炸轮次 | **失败分类学**：auth/网络错→无条件中止保原样；瞬时错→回退主模型重试；无 provider→600s 冷却（持久化到 state.db）；其余→本地静态兜底摘要 | offload 失败降级继续；摘要重试耗尽上抛；overflow 两次仍超→终止；`_check_reduction` 保证重试严格变小否则熔断 | 截断的摘要（length-stop）不许当 checkpoint；失败发事件后停止，无无限重试；overflow 只 compact-and-retry 一次 |
| **持久化/恢复** | 压缩结果天然落在 messages → `.sessions/*.json` 覆盖写；账本 `last_in` 随存随恢复 | 原文留父 session（轮转）或同 id 重写；失败冷却按 session 持久化；resume 靠消息里的旧摘要走迭代更新 | 事件随 checkpointer 存 + offload 落 backend 大文件；resume 后用 raw+event 重建视图 | JSONL append 一条 entry，原文不删；resume 即重算投影；防旧 usage 重触发 |
| **手动命令** | 无 | `/compress [focus]`、`/compress here [N]`、`--preview`，可清冷却强制 | dcode：`/offload`（强压绕资格门）+ `/summarization-model` 换摘要模型 + `/context-doctor` | `/compact [自定义指令]`，focus 拼进摘要 prompt，支持扩展钩子取消/接管 |
| **特色亮点** | 全场最小实现；防抖口径纪律（est-vs-est 不混比）；错误即文本不炸轮次 | ① 失败分类学（数据安全优先）② 并发原子锁+TTL 租约 ③ 每个边角机制对应真实 issue 号 | ① 事件视图保 replay/eval ② PreCompact hook + 专用摘要模型降级链 ③ `_check_reduction` 熔断 | ① 树状投影非破坏 ② split-turn 双摘要 ③ 20+ 家 provider overflow 正则库 |

## 三、我们对标后的可选优化（按性价比排序）

| 优化 | 学谁 | 理由 | 代价 |
|---|---|---|---|
| 1. 手动 `/compact [focus]` | pi / hermes | 压缩判定再准也不如用户自己知道"该压了"；focus 参数还能引导摘要重点 | ~30 行（复用 `maybe_summarize`，force 绕判定） |
| 2. ~~摘要 prompt 结构化 + 迭代更新式~~ **已实现**(学 pi / hermes):固定章节模板(Goal/Constraints/Progress/Decisions/Next Steps/Critical Context,要求保留精确值) + 有旧摘要时走 PRESERVE+ADD+UPDATE 合并(REAL 模型验证:旧摘要的 In Progress 条目被正确 UPDATE 成 Done 并合入新 commit 号);二次压缩不再全量重写丢细节 | pi(`UPDATE prompt`) / hermes | ~~理由~~ 已落地 `core/summarize.py` | 已完成 |
| 3. preflight 兜底（大 tool_result 追加后） | hermes（工具循环中 pre-API 检查） | execute 输出很大时 postflight 滞后一轮，下一轮请求可能直接超窗吃 API 错误；我们目前靠"错误即文本"自愈，但浪费一轮 | `stream()` 里工具回填后加一次判定，~10 行 |
| 4. overflow 感知重试 | pi / deepagents | provider 报 context overflow 时自动压缩后重试一次，把"错误即文本"升级为"错误即恢复" | 需识别 overflow 错误模式，中等 |
| 5. 旧 tool_result 无 LLM 瘦身 | hermes Phase 1 | 不调 LLM 就能省大量 token（旧 tool_result 换单行）；我们 `_flat` 只在摘要输入时用，线上 messages 不动 | 中等，需改动 messages 结构 |
| 不建议抄 | — | hermes 的会话轮转/并发锁（单进程 TUI 无此问题）、deepagents 的 offload 大文件（违背"文件即状态、覆盖写"约定）、pi 的树状 session（我们的平铺 JSON 是刻意选择） | — |

## 四、结论

我们的压缩是"**判定学 hermes、摘要学 pi、结构走自己路**":触发/防抖/熔断这套纪律完整对标 hermes
(且防抖同口径比它更严);摘要 prompt 已升级为 pi 的 checkpoint 结构化格式 + 迭代更新式
(2026-10-28);但压缩结构选了与三家都不同的**改写式原地替换**--这是"无 checkpointer、
sessions 覆盖写"约束下的正确取舍。剩余差距集中在**入口完备性**(无手动命令、无 preflight
兜底)和 overflow 感知重试,都属于小改动;而 hermes/dcode 的重型机制(会话轮转、事件视图、
offload 体系)依赖各自的持久化基建,与我们"协议原生、零框架"的定位冲突,不该抄。
