# 核心组件设计与实现：loop / 工具 / 上下文压缩

对应代码：`core/loop.py`、`core/tools.py`、`core/summarize.py`、`core/model.py`。
这是把 deepagents 拿回来自己写的那一层，全部是手写的纯 Python，不依赖任何 agent 框架。

## 1. 总体分层

```
tui.py            UI 层:流式渲染 / 会话管理 / cron / 无头模式
agent.py          组装层:.env + SYSTEM_PROMPT + build_agent(workdir)
core/
  model.py        模型薄封装:流式 SSE 事件、非流式补全
  loop.py         ★ agent loop:聚合 → 派发工具 → 回填 → 循环
  tools.py        内置工具 + Tool 统一形态
  subagent.py     task 子代理
  summarize.py    上下文压缩
  sessions.py     会话持久化
  mcp.py / skills.py / messages.py
```

关键决策:**Anthropic 原生消息格式就是全项目的唯一协议**。messages 是
`{"role", "content": str | [blocks]}` 的 dict 列表,blocks 为
text / thinking / tool_use / tool_result。模型吃什么格式,会话就存什么格式,
回放时原样带回 —— 中间不需要任何转换层。旧方案(deepagents+langchain)
光消息格式就要在 LangChain 对象 ↔ dict 之间转两次,这是砍掉框架的最大收益之一。

## 2. loop(core/loop.py,约 110 行)

### 2.1 主循环

```python
def stream(self, messages):          # 生成器,原地改写 messages
    maybe_summarize(self.model, messages)      # 每轮开工前查一次上下文水位
    for _ in range(self.max_turns):            # 默认 40,防模型打转烧钱
        blocks = self._one_model_call(messages)   # 流式调模型,聚合出 content 块
        messages.append({"role": "assistant", "content": blocks})
        calls = tool_uses(messages[-1])
        if not calls:
            return                                # 没有工具调用 = 回答完毕
        results = [每个 call 派发工具, 得到 tool_result 块]
        messages.append({"role": "user", "content": results})
    raise RuntimeError("超过最大轮数")
```

整个 agent 的本质就这么大:**"调模型 → 有工具调用就执行并把结果作为 user
消息回填 → 再调模型"**,直到模型不再要求调用工具。所谓"框架"里神秘的
ReAct / tool-loop,核心就是这 20 行,其余全是工程细节。

### 2.2 两个刻意的设计

**① 工具错误也返回文本,不抛异常。**
工具崩了(文件不存在、命令超时、MCP 掉线)统一转成 `"错误: ..."` 字符串塞回
tool_result。模型看到错误文本会自己修正参数重试 —— 实测 GLM 第一次发错
task 的入参,第二轮就自己纠正了。异常直接炸出循环反而是把"自愈"的机会掐死。

**② UI 和 loop 用事件元组解耦。**
`stream()` 一边跑循环一边 yield:

| 事件 | 用途 |
|---|---|
| `("thinking", delta)` | TUI 洋红色实时渲染思考 |
| `("text", delta)` | 正文流式渲染 |
| `("tool_start", id, name)` | 工具开始执行 |
| `("tool_result", id, name, input_json, output)` | TUI 打一行摘要 |

loop 不关心谁在消费事件(headless 模式直接忽略),UI 不关心循环怎么转。
`invoke()` 是消费事件的便捷封装:不关心过程只要结果时用。

### 2.3 流式聚合(为什么这层必须自己写)

`core/model.py` 只把 anthropic SDK 的 SSE 原始事件
(`content_block_start/delta/stop`)原样 yield 出来,聚合发生在 loop:

- `text_delta` / `thinking_delta` → 按 block index 累加文本;
- `signature_delta` → 存进 thinking 块(GLM 的 thinking 带签名,回传必须原样保留,否则下一轮请求 400);
- `input_json_delta` → tool_use 的入参是分片 JSON 字符串,按 index 拼接,
  `content_block_stop` 时一次性 `json.loads`。

这块没有现成的薄封装可用 —— 要么整套框架,要么自己写,总量 30 行。

### 2.4 记忆与 system prompt

`_system()` 每轮重读 `workdir/AGENTS.md` 拼进 system prompt:模型把用户纠正、
项目约定写进该文件(用 edit_file),下一轮自动生效,用户不用重启。
skills 同理:清单(几十 token/条)常驻 system prompt,SKILL.md 全文按需
read_file —— 渐进披露,不占窗口。

## 3. 工具系统(core/tools.py)

### 3.1 统一形态

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], str]   # 入参 dict → 结果文本;错误也返回文本
```

内置工具、MCP 工具、task 子代理全部归一成这一个 dataclass,loop 层
完全不区分来源 —— 这是"框架的 middleware 体系"用 10 行 dataclass 替代的等价物。

### 3.2 内置五个工具

| 工具 | 作用 | 实现要点 |
|---|---|---|
| `ls` | 列目录(以工作目录为根) | 类型 + 文件大小 |
| `read_file` | 读文件,大文件分页 | offset/limit,带行号;截断时提示剩余行数,引导模型翻页 |
| `write_file` | 整文件覆盖写 | 自动建父目录 |
| `edit_file` | 字符串替换,支持单处或多处(`edits` 数组一次改多处) | 所有 edit 对原始全文定位、按位置从后往前套用 + 重叠检测(学 pi);精确失配先归一化重试(NFKC/智能引号/行尾空白),回写只动覆盖到的行(学 pi);单处旧格式 `old_string/new_string` 兼容保留 |
| `execute` | 真实 shell | shell=True,cwd=workdir,timeout 120s,stdout+stderr+exit code |

### 3.3 三个防线

1. **路径越界防线**:`_resolve()` 把 `/agent.py` 解析成 `workdir/agent.py`
   (虚拟根),用 `Path.is_relative_to` 拒绝 `../` 越出工作目录。
   虚拟根约定写进 SYSTEM_PROMPT 而不是在工具层做路径重写 —— 重写会掩盖
   "文件工具虚拟根 / execute 真实 shell"两套路径的不一致,不如让规则显式。
2. **输出截断防线**:单工具结果 20000 字符封顶(`MAX_OUTPUT`),超长截断并
   告知全长、提示分页。这是上下文管理的第一层:别让一个 `cat 大文件` 撑爆窗口。
3. **执行超时防线**:execute 默认 120s,可按命令覆盖,防死循环命令挂死整个轮次。

### 3.4 task 子代理(core/subagent.py)

`make_task_tool()` 把"再建一个 Agent 实例"本身包装成工具:

- 子代理拿到和主代理同一套工具(builtin),**唯独没有 task** —— 子代理不再
  派子代理,递归在构造处掐死;
- 输入只有一个 `prompt`,输出只有结论文本(`text_of(最后一条)`)——
  中间过程全部留在子代理的消息列表里,主对话只保留结论,不污染上下文;
- 适合检索、批量评测这类量大或可并行的活。

## 4. 上下文压缩(core/summarize.py)

### 4.1 为什么选"改写式"

两种流派:
- **非破坏式**(deepagents 默认):原文 offload 到私有状态,历史里放占位符;
  依赖 checkpointer 才能恢复完整现场。
- **改写式**(本方案):超阈值直接把较早历史替换成一条摘要消息。

选改写式的根本原因是持久化模型:本项目 sessions/*.json 是直接覆盖写,
没有 checkpointer。改写式的结果天然就在 messages 里,落盘/恢复零额外依赖;
全量原文仍留在历史 session 文件里(每轮都存过盘),想翻旧账去文件里找。

### 4.2 触发与切分

```python
def maybe_summarize(model, messages, prompt_tokens=None, trigger_tokens=80*1024, keep=20):
    ...
    return (是否压缩, 判定用token, 压缩前est, 压缩后est)
```

- **token 判定(hermes 语义, postflight)**:优先用上一次响应的真实 prompt_tokens
  (loop 从 `message_start`/`message_delta` 抓的 usage),拿到真实值才准;
  None 时(会话恢复首轮/GLM 代理不回 usage)回落 字符数/3 粗估。
  字符数天然高估, 往"提前压缩"的方向错, 保守方向正确。
- **切分点保护**:截断点不能落在"工具调用对"中间。messages 的结构是
  `assistant(tool_use) → user(tool_result)` 成对出现,如果切分点把
  tool_result 切到摘要区、tool_use 留在保留区,API 直接报孤儿结果错误。
  所以切分点向后滑动到非 tool_result 开头的消息为止。
- **保留尾部**:最近 20 条原样保留(正在进行的任务现场),更早的压成一条
  user 消息。摘要 prompt 明确要求保留:目标与约束、关键操作与结论、
  未完成事项、文件路径。
- **一维化**:喂给摘要模型前,把 content blocks 拍平 —— thinking 块丢弃
  (中间推理不需要进摘要),tool_use 只留名字,tool_result 留前 500 字。
  否则摘要请求本身就可能超上下文。

触发时机学 hermes context_engine:每轮响应+工具回填后、下一轮模型调用前
(loop._compact_step, postflight —— "对话完校验");循环开头也判一次,
是给恢复的超长会话的 preflight 兜底。带两层保护(都从 hermes 学的):
- **防抖**(#14690):压缩后 est 降幅 <10% 连续两次 → 放弃,防无限压缩循环;
  对比必须同口径(est vs est),真实值和 est 差一个 system+tools 的量,不能混比
- **熔断**(600s 冷却的简化):摘要 LLM 失败一次就停用本会话自动压缩,防每轮重试拖垮对话

## 4.3 token 账本(hermes sessions 表语义)

- **记账点在聚合循环里**:Anthropic 流式协议的真实 usage 在 `message_start`
  (input_tokens + cache_read + cache_creation)和 `message_delta`(output_tokens)
  两个事件里,聚合时顺手接住,不当场接就永远丢了(此前就是这个 bug)。
- **只做 session 级累计**:`Agent.usage = {input, output, calls}`,随
  save_session 落盘、resume 时接回。不做消息级 —— hermes 的
  messages.token_count 就是没在写入时顺手记,一直烂尾成 null。
- 子代理消耗不计入主会话账本(dcode 有子代理转移机制,我们不搞,保持薄)。
- **事件给绝对量/增量分开**:usage 事件给的是本轮增量,累计值在 Agent 上;
  UI(tui)只展示不维护,丢事件也不影响账本。

## 5. 一句话总结

协议只有一套(Anthropic 原生消息),循环只有一个(约 110 行的 stream()),
工具只有一种形态(Tool dataclass)。框架消失后,agent 的全部复杂度
——聚合、派发、回填、压缩 —— 都是看得见摸得着的普通 Python。
