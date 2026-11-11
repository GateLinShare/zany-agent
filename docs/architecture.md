# zany-agent 架构与核心组件

无框架手写的 mini coding agent(GLM,走 anthropic 兼容代理,`Authorization: Bearer`)。
从 deepagents + langchain 迁移而来(commit `455d92b`),协议只有一套、循环只有一个、
工具只有一种形态。

## 1. 总体分层与文件职责

```
core/
  tui.py          UI 层:claude 式 TUI(2026-11-09 tui2 转正)/ 会话恢复 / cron / 无头模式(-p)
  agent.py        组装层:.env + SYSTEM_PROMPT + 风格包(prompt_packs) + build_agent(workdir)
  cron_jobs.py    cron 定时任务:调度线程只入队,主循环消费后当普通输入跑一轮
  loop.py         ★ agent loop:流式聚合 → 派发工具 → 回填 → 循环(约 320 行)
  model.py        模型薄封装:裸 httpx 直调 /v1/messages + 手解 SSE(不引 anthropic SDK——导入重, Windows 冷启动 6s→0.4s); 流式转发/非流式补全(压缩用)
  tools.py        Tool 统一形态 + 五内置工具(ls/read_file/write_file/edit_file/execute)
  subcore/agent.py  task 子代理(递归在构造处掐死)
  summarize.py    上下文压缩(改写式)
  sessions.py     会话持久化:Anthropic 原生 dict 直接 JSON 落盘
  mcp.py          MCP 工具加载(单次连接即用即走)
  skills.py       skills 清单注入 system prompt(渐进披露)
  goal.py         goal 模式:续跑投影 prompt + update_goal 工具(状态真身在 tui)
  plugins/        能力插件 loader:core/plugins/<name>.py 暴露 register(workdir)->list[Tool]
  frozen.py       frozen(PyInstaller)路径收口:只读资源 app_root() / 可写数据 data_dir()(~/.zany)
  messages.py     消息块辅助(text_of / tool_uses / est_tokens)
  deploy/         源码一键部署:deploy.sh / deploy.cmd(uv + 依赖 + 全局命令)
eval/             评测四层:main.py+cases.jsonl(硬断言回归)、deepeval/(GEval 质量分)、
                  terminal-bench/(Stanford 86 题公开榜)、skill-loop/(进化闭环);总架构 docs/self-improvement.md
release/          构建/发布统一目录:build.sh(构建当前平台)、install.sh(装系统 PATH)、dist 产物、npm 包源(npm/main)
.github/workflows/  build-test.yml(手动触发构建验证)/ release.yml(frozen + npm 发版)
```

四条贯穿性约定:

| 约定 | 内容 |
|---|---|
| **单协议** | Anthropic 原生消息格式是唯一协议:messages 是 `{"role", "content": str \| [blocks]}` 的 dict 列表,blocks 为 text / thinking / tool_use / tool_result。模型吃它、会话存它、回放原样带回,零转换层。旧方案(deepagents+langchain)光消息格式就要在 LangChain 对象 ↔ dict 之间转两次,这是砍掉框架的最大收益之一 |
| **错误即文本** | 工具崩溃不抛异常,统一转 `"错误: ..."` 塞回 tool_result,让模型自己看到并修正重试(实测 GLM 发错 task 入参,第二轮自己纠正) |
| **路径不做重写** | 文件工具与 execute 同一套文件系统视图(pi/hermes 同款):绝对路径字面直用,相对路径相对 workdir;无虚拟根、无越界 jail,约定写进各文件工具 description(2026-10-31 移除虚拟根与 `_resolve` 逃生口) |
| **账本当场接** | 真实 usage 只在流式聚合时能拿到(`message_start` / `message_delta`),聚合时顺手记账;est 粗估只做压缩触发兜底 |

## 2. agent 组装(core/agent.py)

```python
build_agent(workdir, with_mcp=True):
    model = ChatModel(os.environ["ANTHROPIC_MODEL"])
    tools = builtin_tools(workdir)
    tools += load_plugins(workdir)                   # .env 的 ZANY_PLUGINS 选装能力插件
    tools += load_mcp_tools()                        # with_mcp=False 时 TUI 后台补
    tools.append(make_task_tool(model, STYLE_PROMPT, workdir, [],
                                shared_interrupt=interrupt_flag))  # 子代理不载 MCP; 打断标志主/子共享
    return Agent(model, STYLE_PROMPT + skill_section(workdir), tools, workdir, ...)
```

- **system prompt 每轮现拼**:`Agent._system()` 每轮重读 `workdir/AGENTS.md` 拼进
  system prompt,用完即弃、不入 messages —— 用户改记忆不用重启,模型用 edit_file 维护。
- **常驻文本与按需技能是两条通道**:`prompt_packs()` 把 `styles/<name>.md` 全文拼进
  SYSTEM_PROMPT(`.env` 的 `PROMPT_PACKS` 逗号分隔选用,人格/规则类常驻行为);
  skills 走渐进披露——`skill_section()` 扫项目 `skills/`、`.claude/skills`、
  `.agents/skills` + 全局 `~/.zany/skills`,清单(几十 token/条)常驻,SKILL.md
  全文按需 read_file,同名先到先得。按需技能别塞 PROMPT_PACKS(模型不会每轮自觉读全文)。
- **MCP 后台补**:`build_agent(with_mcp=False)` 让 TUI 界面先起,`add_tools()` 在
  probe 完成后补工具(见 §7)。

## 3. loop(core/loop.py)

### 3.1 主循环

```python
def _run(self, messages):                    # stream() 包装:清残留打断标志 + finally 清理
    for _ in range(self.max_turns):          # 默认 40,防模型打转烧钱
        if interrupted: yield ("interrupted", ...); return
        yield from self._compact_step(...)   # preflight 兜底(见 §6)
        blocks = 聚合 self.model.stream(...) # 流式调模型,按 block index 聚合 content 块
        messages.append({"role": "assistant", "content": blocks})
        记账(turn_in, turn_out)              # usage 事件给 UI
        calls = tool_uses(messages[-1])
        if not calls: return                 # 没有工具调用 = 回答完毕
        results = [每个 call 派发工具 → tool_result 块]   # 错误也转文本
        messages.append({"role": "user", "content": results})
        yield from self._compact_step(...)   # postflight(见 §6)
    raise RuntimeError("超过最大轮数")
```

整个 agent 的本质就这么大:**调模型 → 有工具调用就执行并把结果作为 user 消息回填 →
再调模型**,直到模型不再要求调用工具。所谓"框架"里神秘的 ReAct / tool-loop 核心就是
这 20 行,其余全是工程细节(循环本体约 320 行)。

### 3.2 事件解耦

`stream()` 一边跑循环一边 yield 事件元组,UI 和 loop 互不关心对方:

| 事件 | 用途 |
|---|---|
| `("thinking", delta)` | TUI 洋红色实时渲染思考 |
| `("text", delta)` | 正文流式渲染 |
| `("tool_start", id, name)` | 工具开始执行 |
| `("tool_result", id, name, input_json, output)` | TUI 打一行摘要 |
| `("usage", turn_in, turn_out)` | 每次模型响应的真实 token |
| `("compact", before, after)` | 触发了上下文压缩 |
| `("compact_error", err)` | 摘要失败,本会话停用自动压缩 |
| `("interrupted", reason)` | 用户打断,本轮到此为止 |
| `("turn_limit", max_turns)` | 工具轮数用尽(`.env` `MAX_TURNS`,默认 40):不抛异常,追加一条 assistant 说明到 messages 后正常 return,UI 黄字提示 |

`invoke()` 是消费事件的便捷封装:不关心过程只要结果时用(headless / eval)。

### 3.3 流式聚合(为什么这层必须自己写)

`core/model.py` 只把 anthropic SDK 的 SSE 原始事件
(`content_block_start/delta/stop`)原样 yield 出来,聚合发生在 loop:

- `text_delta` / `thinking_delta` → 按 block index 累加文本;
- `signature_delta` → 存进 thinking 块(GLM 的 thinking 带签名,回传必须原样保留,
  否则下一轮请求 400);
- `input_json_delta` → tool_use 的入参是分片 JSON 字符串,按 index 拼接,
  `content_block_stop` 时一次性 `json.loads`;解析失败置 `{}` 不炸轮次。

这块没有现成的薄封装可用 —— 要么整套框架,要么自己写,总量 30 行。

### 3.4 打断(hermes interrupt 语义)

`interrupt(reason)` 置标志 + 立即 `abort()` 在途模型流(不等下一个 SSE 事件);
loop 在三处检查点协作式停下:每轮开始、流式循环的 SSE 事件间隙、工具派发间隙(0.1s 轮询)。
收尾规则:

- 丢弃未完成的 thinking 块(没拿到 signature,原样回传会被 API 拒);
- 保留 text / tool_use,有 tool_use 就补 `cancelled` tool_result 保配对 ——
  消息序列始终合法可回放,partial 内容照常落盘、token 照常记账;
- 在途工具立即废弃不等结果:工具跑在 agent 专职 worker 线程(_dispatch 0.1s
  轮询),打断即丢弃结果;有 kill 钩子就强杀(execute 终止整个进程组);
  同批剩余工具请求不再执行。worker 会把废弃调用跑完自己丢弃
  (mcp 60s / execute 已被杀,上限有界)。

Ctrl+C 两段式:第一次置打断标志协作收尾,第二次恢复 SIG_DFL 硬退。

### 3.5 token 账本(hermes sessions 表语义)

- **记账点在聚合循环里**:Anthropic 流式协议的真实 usage 在 `message_start`
  (input_tokens + cache_read + cache_creation)和 `message_delta`(output_tokens)
  两个事件里,聚合时顺手接住,不当场接就永远丢了。
- **只做 session 级累计**:`Agent.usage = {input, output, calls}`,随 save_session
  落盘、resume 时接回;打断轮也记账。不做消息级。
- 子代理消耗不计入主会话账本,保持薄。
- **事件给增量,累计在 Agent 上**:UI 只展示不维护,丢事件不影响账本。

## 4. 工具系统(core/tools.py)

### 4.1 统一形态

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], str]   # 入参 dict → 结果文本;错误也返回文本
    ledger: Any | None = None    # 行为熔断账本(仅内置工具);压缩后由 loop 清空
```

内置工具、MCP 工具、task 子代理全部归一成这一个 dataclass,loop 层完全不区分
来源 —— 这是"框架的 middleware 体系"用 10 行 dataclass 替代的等价物。

### 4.2 内置五个工具

| 工具 | 作用 | 实现要点 |
|---|---|---|
| `ls` | 列目录(以工作目录为根) | 类型 + 文件大小;`LS_MAX`(500 条)封顶,超了提示换更具体的 path |
| `read_file` | 读文件,大文件分页 | offset/limit,带行号;截断提示带精确续读参数(`用 offset=100 继续读`);同参数重读返回 stub 指回早前结果(不重复回显) |
| `write_file` | 整文件覆盖写 | 自动建父目录;落盘后失效该文件的读去重/连败记录 |
| `edit_file` | 字符串替换,支持单处或多处(`edits` 数组一次改多处) | 所有 edit 对原始全文定位、按位置从后往前套用 + 重叠检测;精确失配先归一化重试(NFKC/智能引号/行尾空白),回写只动覆盖到的行;BOM 剥离 + CRLF 透明还原(匹配在 LF 空间,回写按原行尾);同路径连败 ≥3 次附升级提示(重读/换 write_file);单处旧格式兼容保留 |
| `execute` | 真实 shell | shell=True + `start_new_session`(POSIX),cwd=workdir,timeout 默认 `EXEC_TIMEOUT`(1200s,可按命令覆盖);超时 kill 整个进程组并回传超时前已产出的输出;stdout+stderr+exit code |

### 4.3 四个防线

1. **路径不做重写**:`_resolve()` 绝对路径字面直用、相对路径相对 workdir
   (pi/hermes 同款);无虚拟根无 jail —— execute 本就是真实 shell,
   文件工具再圈地只会制造两套路径语义,赌模型给对路径、错时报错带锚点自纠。
2. **输出截断防线**:单工具结果 `MAX_OUTPUT`(200000 字符)封顶;截断方向学
   hermes **保头 40% + 保尾 60%**(错误常在前头,最新输出在后头),execute 超长
   时全量落盘 `/tmp/agent-exec-*.log` 并把路径告诉模型,想看全文可再读。
3. **执行超时防线**:execute 默认 1200s;超时用 `os.killpg` 杀整个进程组
   (只杀 shell 会残留孙子进程孤儿),并回传超时前的部分输出(hermes 语义:
   超时不代表一无所获)。
4. **行为熔断防线**(`_Ledger`,hermes file_tools 防循环语义的简化版):每个
   agent 一份账本,记三类循环 —— ①同参数重复 read → 返回 stub 指回早前结果;
   ②同路径 edit 连败 ≥3 次 → 附"停止变体重试,重读原文/换 write_file";
   ③同一条 execute 命令连跑 ≥3 次 → 提醒"结果大概率不变"。都是提示不是
   阻断 —— 把"你在循环里"这个元信息喂给模型,让它自己改行为。**上下文压缩
   后由 `loop.reset_tool_ledger()` 清空**(摘要会改写掉旧 read 结果,去重
   stub 那时会说谎)。

### 4.4 task 子代理(core/subcore/agent.py)

`make_task_tool()` 把"再建一个 Agent 实例"本身包装成工具:

- 子代理拿到和主代理同一套内置工具,**唯独没有 task 和 MCP** —— 递归在构造处掐死;
- 输入只有一个 `prompt`,输出只有结论文本(`text_of(最后一条)`)—— 中间过程全部
  留在子代理的消息列表里,主对话只保留结论,不污染上下文;
- 适合检索、批量评测这类量大或可并行的活。

## 5. 模型层(core/model.py)

不引 anthropic SDK —— 它拖着 pydantic 一大家子,Windows 上(安全软件过滤驱动放大每文件
打开开销)导入 6s+,冷启动 0.4s vs 6s。只用 POST `{base}/v1/messages` 一种调用,
裸 httpx + 手解 SSE,事件直接是协议原生的 dict(单协议:模型吐的 / loop 聚合的 /
会话落盘的是同一种 dict)。

- `make_client()`:`httpx.Client`,端点/密钥/模型名走 `.env`
  (`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`),头同时带
  `x-api-key` + `Authorization: Bearer`(GLM 代理只认 Bearer)。
- `ChatModel.stream()`:SSE 原始事件(`content_block_start/delta/stop`)原样 yield,
  不做聚合(聚合在 loop);另有 `abort()` 供打断。
- `ChatModel.complete()`:非流式补全(压缩摘要用),复用同一 client。
- 不做重试/降级(ponytail:代理断流直接报错给上层,频繁到需要再加 tenacity)。

## 6. 上下文压缩(core/summarize.py)

### 6.1 为什么选"改写式"

- **非破坏式**(deepagents 默认):原文 offload 到私有状态,历史里放占位符,
  依赖 checkpointer 才能恢复完整现场。
- **改写式**(本方案):超阈值直接把较早历史替换成一条摘要消息。

选改写式的根本原因是持久化模型:本项目 ~/.zany/sessions/*.json 直接覆盖写,没有
checkpointer。改写式的结果天然就在 messages 里,落盘/恢复零额外依赖;全量原文
仍留在历史 session 文件里(每轮都存过盘),想翻旧账去文件里找。

### 6.2 触发与切分

```python
def maybe_summarize(model, messages, prompt_tokens=None,
                    trigger_tokens=TRIGGER_TOKENS, keep=20):
    ...
    return (是否压缩, 判定用token, 压缩前est, 压缩后est)
```

- **触发点 = 窗口上限 × 比例**:`.env` 可配,默认 `200000 × 0.6 = 120k`
  (`ANTHROPIC_CONTEXT_TOKENS` / `COMPACT_TRIGGER_RATIO`)。不引 tokenizer,
  阈值本身就是粗粒度的。
- **token 判定(hermes 语义, postflight)**:优先用上一次响应的真实 prompt_tokens
  (loop 从 `message_start`/`message_delta` 抓的 usage);None 时(会话恢复首轮/
  GLM 代理不回 usage)回落 字符数/3 粗估。字符数天然高估,往"提前压缩"的方向错,
  保守方向正确。循环开头的 preflight 判一次,是给恢复的超长会话兜底。
- **切分点保护**:截断点不能落在"工具调用对"中间 —— 如果切分点把 tool_result
  切到摘要区、tool_use 留在保留区,API 直接报孤儿结果错误。所以切分点向后滑动
  到非 tool_result 开头的消息为止。
- **保留尾部**:最近 20 条原样保留(正在进行的任务现场),更早的压成一条 user
  消息。
- **一维化**:喂给摘要模型前把 content blocks 拍平 —— thinking 块丢弃、image 标记
  占位、tool_use 只留名字、tool_result 留前 500 字,否则摘要请求本身可能超上下文。

### 6.2.1 摘要 prompt:结构化 + 迭代更新(学 pi/ hermes)

摘要消息结构固定:`{"role": "user", "content": SUMMARY_TAG + "\n\n" + 摘要}`,
前缀常量 `SUMMARY_TAG`(落盘识别/迭代更新都依赖这个约定)。

- **固定章节模板**(pi checkpoint 格式):`## Goal / Constraints & Preferences /
  Progress(Done/In Progress/Blocked) / Key Decisions / Next Steps / Critical Context`,
  并要求保留精确的文件路径/命令/报错原文 —— 泛泛的"讨论了X"没用,精确值才是
  后续会话最依赖的东西。
- **迭代更新**:stale 区里找旧摘要(`_prev_summary_idx`),找到就走 UPDATE 模式:
  `<previous>` = 上次摘要正文,`<conversation>` = 其后全部未摘要的消息(**含上次
  保尾** —— 它们上次没被摘要过,不给就丢);指示模型 PRESERVE 仍然成立的信息、
  ADD/UPDATE 新进展。避免全量重写把旧摘要细节流失(改写式的固有风险,靠迭代
  对冲)。首次压缩走全量提炼。

### 6.3 防抖与熔断(loop._compact_step)

- **防抖**(hermes #14690):压缩后 est 降幅 <10% 连续两次 → 放弃,防无限压缩循环;
  对比必须同口径(est vs est),真实值和 est 差一个 system+tools 的量,不能混比。
- **熔断**(hermes 600s 冷却的简化):摘要 LLM 失败一次就停用本会话自动压缩,
  防每轮重试拖垮对话;失败不炸轮次(错误即文本),发 `compact_error` 事件。

## 7. MCP(core/mcp.py)

读 `~/.zany/mcp_servers.json`(运行时数据根,不进 git,各 checkout 共享一份;
2026-11-03 从项目根挪入),stdio / http 双支持,单次连接即用即走,每台服务器
握手+list_tools 后归一成 Tool。两层超时快速认输(pypi 不通的环境里 uvx 会卡几分钟):

- `PROBE_TIMEOUT = 15s`:握手+list_tools 超时跳过该服务器;
- `CALL_TIMEOUT = 60s`:单次工具调用超时。

stdio 子进程 stderr 默认收 devnull(`MCP_STDERR=1` 透传排障);except 不接
BaseException(防吞 Ctrl-C);工具名带 `mcp__<server>__<tool>` 前缀(pi/hermes 同款)
防跨服务器重名覆盖;`config_stamp()` 供热重载检测。TUI 场景 `build_agent(with_mcp=False)`
先起界面,probe 完成后 `agent.add_tools()` 补入;probe 失败的服务器进 `failed_servers()`
名单,TUI worker 每 60s 全量重试补挂(5 分钟窗口)—— 界面不因 MCP 卡启动,网络恢复自动补挂。

## 8. 持久化与恢复(core/sessions.py)

- 每轮 invoke 后覆盖写 `~/.zany/sessions/<时间戳>.json`(首轮创建),Anthropic 原生 dict
  直接 JSON 落盘(含 usage);崩溃最多丢当轮。顶层扩展键(都不进 messages,回放忽略):
  - `title`:会话标题(首轮后异步生成 + `/title` 手动),`/resume` 列表优先展示;
  - `marks`:`[{at, thought, tools}]` 回合侧账本 —— Thought/tool 计数是运行时局部
    变量,Anthropic 消息里没有;`at` = 回合结束时的消息条数做对齐锚,恢复回放按回合
    分组对齐,锚失配(压缩改写后)静默跳过;老会话文件无 marks;
  - `goal`:goal 模式状态(见 §9.4),`/resume` 捡回。
- `/resume`(TUI 内嵌高亮行弹层:↑↓ 选、回车恢复、esc 取消)/ `core/tui.py -r [id]`
  列出最近 10 个会话(按 mtime 排序),恢复后**原地续写原文件**;`-r` 不带 id =
  最近活跃会话。
- 恢复时回显历史:human/ai 全文,工具消息跳过。

## 9. 交互层(core/tui.py)

### 9.1 claude 式 TUI(2026-11-09 tui2 转正)

不用全屏 UI 框架,渲染分两块:

- **烙定区**:完成的轮次用普通 `print` 进终端原生滚动缓冲,永不重绘;
- **活动区**(底部):输入行 + 状态行,整帧重绘,只用 CUU/ED/CHA 三个光标序列。

markdown 策略:流式期间粗排纯文本(`_turn_renderable(md=False)`),轮次定稿后
rich Markdown 整段渲染进烙定区 —— 绕开流式渲染的闪烁/竞态一整类问题。
thinking 默认折叠 3 行(`.env` SHOW_THINKING 开全量),数据层始终全量记录;
轮末摘要行 `Thought for Xs, ran N tool calls`。

### 9.2 读键(Term)

POSIX 走 termios + select;Windows 走 getwch。Windows 侧踩过的坑已固化成约定:

- **扩展键两段**:ConPTY/PyCharm 方向键 = `\x00` 前缀 + 键码两段 getwch。前缀必须
  当场补取第二段(第二段已在 getwch 内部缓存,不取永远无人取,全键冻结——包括
  Ctrl-C;2026-11-09 修,`/resume` 弹层死锁的根因)。老 console 是 `\xe0` 前缀同款。
- **ESC 分支只认两样**:bracketed paste 结束标记、孤按 ESC(picker 取消键)。
  不解析 VT 序列(`\x1b[A` 等)—— 那是当初的错误归因产物,2026-11-09 删;
  ConPTY 下方向键实际走两段扩展键。
- **conhost ctrl+v**:关 PROCESSED_INPUT 后无系统粘贴,键层映射 `PASTE_CLIP`
  走 ctypes 读剪贴板(HANDLE 必须设 64 位 restype,截断即 access violation)。
  另有无标记粘贴启发式(PyCharm/ConPTY/conhost 不透传 `?2004h` 粘贴标记,靠
  PASTE_WINDOW 内连发判定)。
- **诊断**:`ZANY_TUI_TRACE=路径` 开读键落盘探针,穿透 GUI 终端看冻结现场;
  ConPTY harness(tmp/pty_drive.py,不进 git)可程序化喂键全链路复现。

### 9.3 goal 模式(core/goal.py)

`/goal <目标>` 设目标后宿主自动续跑(codex ext/goal 语义,不做 judge):

- 状态真身是 tui 侧 dict,`update_goal` 工具闭包共享同一引用,零同步;
- 每轮末尾 `turn_finished()` 现场注入续跑投影 user 消息(压缩免疫);
- `update_goal(complete/blocked/paused)` 申报状态;预算 `GOAL_MAX_TURNS`
  (`.env` 可配,默认 500)耗尽转 paused 可 resume;打断/异常轮不续;
- 同一阻塞条件连续 3 轮才许申报 blocked,不许自作主张 paused;
- 优先级:排队用户文本 > cron > goal。测试 tests/test_goal.py。

### 9.4 入口与无头模式

```
uv run core/tui.py              # interactive TUI
uv run core/tui.py -r [id]      # TUI 直接恢复指定/最近活跃会话
uv run core/tui.py -p "问题"              # 无头:新会话跑一轮, 落盘, stderr 报 session id
uv run core/tui.py -p -r <id> "问题"      # 接指定会话继续(原地续写); id 不存在则以该 id 新开
uv run core/tui.py -p -r "问题"           # 接最近一个会话
cat prompt.txt | uv run core/tui.py -p    # prompt 走 stdin
```

无头模式单次 invoke、不起 UI、不挂 cron,支持 Ctrl+C 两段式打断;工单 watcher 等
后台调用走这条路径,下一轮把 stderr 里的 session id 传回 `-r` 即可多轮续聊。
评测同走此路径:`eval/main.py` 读 `eval/cases.jsonl` 在临时目录跑 agent 断言判分。

### 9.1 评测体系(eval/ 四层)

| 层 | 判分 | 入口 |
|---|---|---|
| L1 功能回归 | JSONL 用例(seed+query+checks)→ 临时目录跑 agent → 硬断言 0/1(answer_contains/file_exists/file_contains/max_steps) | `eval/main.py` |
| L2 回答质量 | deepeval GEval 0~1 分+理由,judge 用项目自带 ChatModel(零新增配置);只接硬断言够不着的开放性任务 | `eval/deepeval/`(README 有硬约定 4 条) |
| L2' 能力榜对标 | Stanford Terminal-Bench 86 题,容器内真实终端任务可执行脚本判分;setup.sh(数据集+harness+base 镜像本地重建)/run.sh 分离,oracle 先验判分链路 | `eval/terminal-bench/` |
| L3 进化闭环 | 评测对象=整个 zany(一个 git commit);git 即快照:接受=commit、被拒=checkout -- .,分数严格提升才保留 | `eval/skill-loop/`(编排全在 3 个 SKILL.md,零运行时代码) |

判分方式的选型依据(三家族+分水岭结论)见 `docs/agent-benchmarks.md`;
进化循环完整架构见 `docs/self-improvement.md`;Terminal-Bench 环境配方与
已知限制见 `eval/terminal-bench/README.md`。task 子代理是 L2/L3 评测矩阵的执行器。

## 10. 能力插件(core/plugins/,master 基座)

插件 = 一个 .py 暴露 `register(workdir) -> list[Tool]`,没有基类 / manifest /
registry —— `.env` 的 `ZANY_PLUGINS` 开关 + Python import 就是全部仪式:

- 降级:import 或 register 失败 → 打印跳过,不崩启动(与 core/mcp.py 同款);
- 重依赖走 pyproject extras(master 默认不装,import 失败即该工具缺席);
- 热更:`ZANY_PLUGINS_RELOAD=1` 时每次 load_plugins 先清 pyc 再 reload(防同秒
  同大小改动拿旧字节码),供 gateway 长驻宿主"改插件文件 → 新会话即用新代码";
- 重依赖插件文件只在 develop(browser/peers/restart),master 只留 loader 与约定。

## 11. 分发(frozen + npm)

本地构建/部署:

```bash
bash release/build.sh      # 无参=当前平台 → release/dist/zany-<target>; 可传 target 同款命名
bash release/install.sh    # 二进制装进系统 PATH(zany 命令, 覆盖源码版), 装后 --help 自检
```

正式发版:GitHub Actions(`release.yml`, workflow_dispatch)打三平台 onefile 上
Release 并发 npm 包, 包源在 `release/npm/main`(平台包
`zany-agent-{linux-x64,darwin-arm64,win32-x64}` 走 optionalDependencies,
codex 同款), 版本单源 pyproject.toml。frozen 路径约定收口 `core/frozen.py`:
只读资源 `app_root()`(_MEIPASS)、可写数据 `data_dir()`(~/.zany);内置
skills 全局/用户级目录照常生效(styles 随包分发)。源码里所有 `read_text/open` 必须 `encoding="utf-8"`
(Windows 默认 GBK 必崩)。发布流程 docs/npm-release.md。

## 12. 一句话总结

协议只有一套(Anthropic 原生消息),循环只有一个(280 行的 `stream()`),
工具只有一种形态(Tool dataclass)。框架消失后,agent 的全部复杂度 —— 聚合、
派发、回填、压缩、打断、记账 —— 都是看得见摸得着的普通 Python。
