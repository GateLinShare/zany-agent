# mini-agent:设计与实现 —— 无框架版


> 一个零 agent 框架的编码 agent:loop、工具、上下文压缩、子代理、评测闭环
> 全部手写,核心在 `core/` 目录约 700 行纯 Python。第三方依赖只剩
> anthropic(模型 SDK)、mcp(协议客户端)、textual(TUI)、croniter。
> 本文按"面试官会追问什么"组织:每个设计给出**为什么这么选、放弃了什么、
> 什么时候升级**。组件实现细节见 `docs/architecture.md`,本文讲决策与叙事。

## 1. 一句话定位

一个能读、写、跑 shell、改代码的对话式编码 agent。**从 LangGraph/deepagents
生态整体迁出,agent loop 自己写** —— 不是"用框架拼装"的项目,而是"把框架
拆开看清楚之后,只留下值得依赖的部分"的项目。

迁移的动机很实际:loop 的难点不在"循环调模型直到没有 tool_call"(那只有
20 行),在环上挂的上下文工程。框架把这些藏进 middleware,好处是开箱即用,
代价是**排障时隔着三层抽象**——工具入参怎么聚合、thinking 块带签名要原样
回传、tool_result 孤儿会让 API 400,这些坑框架都替我踩了,但也替我藏了。
自己写一遍之后,这些知识变成我的了,代码反而变薄:全项目消息只有一种格式、
循环只有一个、工具只有一种形态。

## 2. 架构设计

### 2.1 分层

```
┌─ 交互层 ────────────────────────────────────┐
│ tui.py  唯一入口: 无参 = Textual TUI;        │
│         -p = 无头模式(供脚本/后台调用)       │
├─ 组装层 ────────────────────────────────────┤
│ agent.py  .env + SYSTEM_PROMPT + build_agent│
├─ 核心层(全部手写) ─────────────────────────┤
│ core/loop.py      agent loop + 流式聚合      │
│ core/tools.py     内置工具 + Tool 统一形态    │
│ core/subagent.py  task 子代理                 │
│ core/summarize.py 上下文压缩(改写式)         │
│ core/model.py     模型薄封装(流式 SSE)       │
│ core/skills.py    skills 渐进披露             │
│ core/mcp.py       MCP 工具加载                │
│ core/messages.py  消息格式小工具              │
├─ 支撑层 ────────────────────────────────────┤
│ core/sessions.py  会话落盘/恢复               │
│ cron_jobs.py      定时任务                    │
└──────────────────────────────────────────────┘
```

关键决策:**Anthropic 原生消息格式是全项目唯一协议**。messages 是
`{"role", "content": str|[blocks]}` 的 dict 列表,模型吃什么格式、会话就存
什么格式、恢复就原样带回 —— 中间零转换层。旧方案里 LangChain 消息对象和
dict 之间每轮转两次,序列化还要走 `message_to_dict`;现在 `json.dumps` 直接落盘。

### 2.2 组装

```python
def build_agent(workdir):
    model = ChatModel(make_client(), os.environ["ANTHROPIC_MODEL"])
    tools = builtin_tools(workdir) + load_mcp_tools()
    tools.append(make_task_tool(model, SYSTEM_PROMPT, workdir, []))
    return Agent(model, SYSTEM_PROMPT + skill_section(workdir), tools, workdir)
```

组装层就这四行 —— 因为没有框架要配,组装自然就薄。三个仍然值得讲的取舍:

**① 路径不做重写。** 文件工具与 execute 同一套文件系统视图(pi/hermes 同款):
绝对路径字面直用,相对路径相对 workdir。曾有 workdir 虚拟根(`/agent.py` 映射到
`workdir/agent.py`),但逃生口、create 特判、外部新建绕道 execute 这些特例都是它
制造的,2026-10-31 移除 —— 两套路径语义的一致性靠"不做"保证,不靠约定补救。

**② system prompt 不入历史。** AGENTS.md 每轮调模型前现读现拼,用完即弃。
否则记忆每轮重复累加,上下文膨胀、缓存全失效。AGENTS.md 由模型自己用
edit_file 维护("用户纠正过的事情记进去"),这是 agent 自演化的最小形态。

**③ 安全模型。** 权限列表永远不是安全边界,OS 级隔离才是。当前定位是开发者在
可信环境用;要无人值守跑不可信目录,升级路径是给 execute 换容器/namespace,
`Tool.run` 的接口不用动。

## 3. Agent Loop 设计(core/loop.py,约 110 行)

### 3.1 基本循环

```python
def stream(self, messages):                     # 生成器,原地改写 messages
    maybe_summarize(self.model, messages)       # 开工前查上下文水位
    for _ in range(self.max_turns):             # 默认 40,防打转烧钱
        blocks = 聚合一次流式模型调用()
        messages.append({"role": "assistant", "content": blocks})
        calls = 提取 tool_use 块()
        if not calls:
            return                              # 纯文本 = 回答完毕
        messages.append({"role": "user",
                         "content": [每个 call 的 tool_result]})
    raise RuntimeError("超过最大轮数")
```

agent 的本质:**调模型 → 有 tool_use 就执行工具、结果作为 user 消息回填 →
再调模型,直到没有 tool_use**。两个刻意的设计:

- **工具错误也返回文本,不抛异常。** 文件不存在、命令超时、MCP 掉线,统一
  转成错误字符串塞回 tool_result,模型看到会自己修正重试 —— 实测 GLM 第一轮
  发错 task 入参、第二轮自己纠正。异常直接炸出循环等于掐死 agent 的自愈机制。
- **轮边界 = 一次用户输入到最终回复。** 轮结束整份 messages 覆盖写 session
  文件,崩溃最多丢当轮;不做并发轮(messages 是线性数组,并发写会互踩),
  上一轮没跑完新输入排队。

### 3.2 流式聚合(loop 里最"脏"但最值钱的 30 行)

`core/model.py` 把 anthropic SDK 的 SSE 原始事件原样 yield,聚合在 loop 做:

- `text_delta` / `thinking_delta` → 按 block index 累加;
- `signature_delta` → **thinking 块的签名原样保留**,下一轮请求不回传就 400
  (GLM 代理实测踩出来的);
- `input_json_delta` → tool_use 入参是分片 JSON,按 index 拼接,
  block stop 时一次性 `json.loads`。

loop 对外只 yield 四种事件元组:`("thinking", d)` / `("text", d)` /
`("tool_start", id, name)` / `("tool_result", id, name, input_json, output)`。
UI 消费事件、不碰循环;headless 模式直接忽略事件 —— **UI 和 loop 用事件流解耦**。

### 3.3 三种 agent 范式,loop 里只有一种(设计判断,面试常问)

开发 agent 的三种推理范式:**ReAct**(推理-行动交替,走一步看一步)、
**Plan-and-Execute**(先出完整计划再逐步执行)、**Reflection**(生成→判分→
按反馈修订的反思回路)。本项目的落位:

| 范式 | 在哪 | 说明 |
|---|---|---|
| ReAct | **就是 loop 本身** | thinking(Reason)→ tool_use(Act)→ tool_result(Observe)每轮交替,不预设计划、不自我评审 |
| Plan-and-Execute | task 子代理的**用法**上,非代码 | 主对话当 Planner 想清楚拆什么,task 派子代理执行;计划是模型**涌现**的,代码不强制 |
| Reflection | loop 外面(eval/evolve 层) | 需要外部判据 + 修订回路:eval 判分 → optimizer 改 AGENTS.md → 不涨分回滚 |

**为什么 loop 里不做另外两种:** ReAct 是另外两者的底座——计划可以先出再走、
反思是外挂判分回路,都不需要改环的形状。刻意不把反思放进对话内,因为
**对话内的自我反思没有外部判据,模型容易"自我感觉良好"**;放评测层,
分数不涨就回滚,可控得多。升级路径:环外加前置规划轮/后置评审轮,ReAct 环不动。

话术:"我的 loop 是纯 ReAct,一个环没加别的。这是有意的取舍,不是没做。"

### 3.4 双入口

- **TUI**:后台线程跑 `agent.stream()`,thinking 洋红/正文白色实时渲染,
  工具打一行摘要;多行输入(粘贴回放的连击检测)、/resume 弹层、
  MCP 热重载(mcp_servers.json mtime 变更 → 下轮重建 agent)、cron 唤醒。
- **无头 `-p`**:单次 invoke,`-r <id>` 续会话(id 不存在则以该 id 新开);
  stdout 只出最终回复、stderr 出 session id —— **stdout 机器读、stderr 人读**。

### 3.5 上下文管理:三层防线

1. **入口截断**:单工具结果 20000 字符封顶,超长提示分页 —— 别让一个
   `cat 大文件` 撑爆窗口;
2. **80k token 触发改写式压缩**(见 §4);
3. **长期记忆走文件不走 messages**:AGENTS.md 注入 system prompt,
   "短期靠窗口、长期靠文件"分层。

## 4. 工具系统与压缩

### 4.1 Tool 统一形态

```python
@dataclass
class Tool:
    name: str; description: str; input_schema: dict
    run: Callable[[dict], str]     # 错误也返回文本,不抛异常
```

内置工具、MCP 工具、task 子代理全部归一成这一个 dataclass,loop 不区分来源。
框架的 middleware/handler 体系,等价物就是这 10 行。

内置五个工具:ls / read_file(offset-limit 分页、行号、剩余行提示)/
write_file / edit_file(old_string 唯一性校验,多处报错提示 replace_all,
防误替换)/ execute(shell=True、timeout 120s、stdout+stderr+exit code)。

task 子代理 = "把再建一个 Agent 实例包装成工具":子代理拿同一套工具但
**唯独没有 task**(递归在构造处掐死);入参一个 prompt,出参只有结论文本,
中间过程全部留在子代理自己的消息列表里 —— 主对话不污染。

### 4.2 压缩为什么选"改写式"

两种流派:非破坏式(原文 offload 到私有状态,依赖 checkpointer 恢复)vs
改写式(直接把较早历史替换成一条摘要消息)。**持久化模型决定选型**:本项目
~/.miniagent/sessions/*.json 覆盖写、没有 checkpointer,改写式的结果天然就在 messages 里,
落盘/恢复零额外依赖;全量原文仍留在历史 session 文件里。

三个实现细节:

- **token 估算用字符数/3**,不引 tokenizer —— 阈值本身粗粒度,精确计数是伪需求;
- **切分点保护**:截断点不能落在 `assistant(tool_use) → user(tool_result)`
  对中间,否则保留区出现孤儿 tool_result,API 直接 400;切分点向后滑到
  非 tool_result 开头为止;
- **一维化**:喂摘要模型前 thinking 块丢弃、tool_use 只留名字、tool_result
  留前 500 字 —— 否则摘要请求自己先超上下文。

## 5. 评测设计

(与 v1 相同,不变的部分只留结论)

两条原则:**用例即数据、判分即代码**(JSONL 一行一例:seed + query + checks,
加用例不改代码);**检查产物、不检查话术**(断言文件内容,过程指标只留
max_steps 效率红线)。每例 tempfile.TemporaryDirectory 隔离 + 全新 agent,
比 docker 轻三个数量级,天然并行安全。四类 check:answer_contains /
file_exists / file_contains / max_steps。

无框架之后评测多了一层意义:**eval/main.py 直接测的就是手写 loop 的行为**,
旧方案里 eval 隔着 deepagents 测的是"框架+我的装配"的复合行为 —— 现在
分数回退可以精确归因到自己的代码。

进化闭环(规划中)沿用 Penguin 的三条纪律:Rubric 与 Statement 隔离、
快照回滚、分数严格提升才保留。task 子代理就是评测矩阵的执行器。

## 6. 从框架到无框架:迁移决策复盘(这段是面试故事)

| 维度 | deepagents 版 | 手写版 | 迁移换来 |
|---|---|---|---|
| 消息协议 | LangChain 对象 ↔ dict 双向转换 | Anthropic 原生 dict 直通 | 转换层归零,落盘即请求体 |
| loop | LangGraph 节点图,黑盒执行 | 约 110 行生成器,全透明 | 排障不用读框架源码 |
| 流式 | stream_mode 双流 + chunk 续片修补 | SDK 原始事件 + 30 行聚合 | 事件语义自己定义 |
| 压缩 | SummarizationMiddleware 配置项 | 50 行,含孤儿结果保护 | 行为可审计 |
| 依赖 | langchain+langchain-anthropic+deepagents+mcp+… | anthropic+mcp+croniter+textual | 安装体积、升级风险大降 |
| 代码量 | 核心 ~800 行(多为装配) | 核心 ~700 行(全是机制) | 行数近似,内容全换 |

迁移验证了一个判断:**框架的价值密度随项目成熟而衰减**。起步时中间件开箱即用
是净收益;到调优排障阶段,隔着抽象层的每个坑都比自己写的代码贵。值得从框架
保留的只有两样:anthropic SDK(协议实现,手写不值)和 mcp SDK(协议客户端,
手写不值)—— **依赖协议实现,不依赖编排框架**。

## 7. 一分钟版本(电梯陈述)

> 我做了一个编码 agent,第一版基于 deepagents,后来把框架整个拿掉,loop、
> 工具、压缩全部手写,核心七百行。三个决策值得说:① 消息协议用 Anthropic
> 原生格式直通全链路,模型、会话、回放看同一份数据,零转换层;② agent loop
> 就是"调模型、有 tool_use 就回填 tool_result、直到没有"这 20 行,真正的
> 工程量在流式聚合和错误即文本的自愈设计;③ 上下文管理三层防线——工具结果
> 截断、80k token 改写式压缩(选型由持久化模型决定:没有 checkpointer 就选
> 天然落盘的方案)、长期记忆走 AGENTS.md 文件。评测用例即数据、检查产物不
> 检查话术,进化闭环抄 Penguin 的纪律:快照回滚、严格提升才保留。

## 8. 话术版:追问怎么答

### 8.1 "你的 agent loop 是怎么设计的?"(主答,约 2 分钟)

"Loop 是我自己写的,总共一百来行,核心就一句话:**调模型,消息里有 tool_use
就执行工具、把 tool_result 作为 user 消息回填,再调模型,直到模型不再要求
调用工具**。这个环本身只有二十行,我反而觉得这是这次重写最大的认知收获——
框架把 agent 讲得很神秘,拆开看核心就是这一段,工程量全在环上的三件事:

第一,**流式聚合**。模型的回复是 SSE 事件流,content 块是分片到达的:正文和
思考按块累加,工具入参是分片的 JSON 字符串要按块拼接、块结束时一次 parse。
还有个隐蔽的坑:GLM 的 thinking 块带签名,签名必须原样保留、下一轮请求
原样回传,否则 API 直接 400 —— 这个是实测踩出来的,框架里不会看到这一层。

第二,**工具错误的处理哲学**。工具崩了我一律转成错误文本塞回 tool_result,
不抛异常。模型看到错误文本会自己修正参数重试,实测过子代理入参发错、下一轮
自己纠正。直接抛异常等于把 agent 的自愈机制掐死。

第三,**UI 和 loop 的解耦**。stream 是个生成器,一边跑循环一边 yield 四种
事件:思考增量、正文增量、工具开始、工具结果。TUI 消费事件做流式渲染,
无头模式直接忽略事件拿最终结果,loop 对两边无感知。

上下文管理是三层防线:单工具结果 20k 字符截断、80k token 触发摘要压缩、
长期记忆走 AGENTS.md 文件不占窗口。压缩我选了改写式——直接把老历史换成一条
摘要——原因是持久化模型:我的会话就是 JSON 文件覆盖写,没有 checkpointer,
改写式的结果天然就在 messages 里,落盘恢复零依赖。"

**可能追问:**

- *"为什么放弃了 deepagents?迭代好好的为什么重写?"* —— "三个原因。一是
  排障成本:工具入参聚合出问题时,要跨我的代码、middleware、LangGraph 三层
  去查,而 bug 往往在我想不到的层;二是叙事和可控:这是个人项目,价值在
  证明我理解机制本身,框架把机制藏起来,代码薄了但知识也薄了;三是依赖面:
  langchain 全家桶的升级风险和安装体积,换成只剩 anthropic + mcp 两个协议
  依赖。我的判断是:框架的价值密度随项目成熟而衰减,起步时中间件开箱即用
  是净收益,到调优阶段抽象层就成了负债。"
- *"自己写会不会重新踩一遍框架踩过的坑?"* —— "会,而且我就是要踩一遍,
  每个坑现在都是我代码里一行注释:thinking 签名回传、孤儿 tool_result 的
  切分点保护、分片 JSON 聚合。区别是框架替我踩坑但把知识也藏了,现在坑在
  我自己的代码里,带着注释。总量没变多少——核心七百行——但每一行我都
  知道为什么存在。"
- *"哪些部分你坚决不自己写?"* —— "协议实现。anthropic SDK 管流式和重试,
  mcp SDK 管 JSON-RPC 握手,手写不值也不该值。我画的线是:**依赖协议实现,
  不依赖编排框架**。编排是我这个项目的核心知识,恰恰要自己握着。"
- *"没有框架,功能有缺吗?"* —— "对齐清单我逐项核过:流式 thinking/正文、
  工具调用、子代理、MCP、skills、记忆、压缩、会话恢复、无头模式,全量保留。
  真正丢的只有两样:子代理默认不载 MCP 工具(防递归,要加是传个参数的事),
  和 deepagents 的历史 offload(我落盘更简单,全量在 session 文件里)。"

### 8.2 "评测怎么做的?"(主答,约 90 秒)

"两条原则:用例即数据、判分即代码 —— 用例是 JSONL,seed 文件加 query 加
checks,加用例不改代码,换模型重跑即得对比分;检查产物不检查话术 —— 让
agent 把结果写进文件,断言文件内容,过程只留 max_steps 一条效率红线。
每例开临时目录、起全新 agent,四类 check 跑完报 PASS/FAIL 和通过率。

无框架后这层还有个额外意义:eval 直接测的就是我手写的 loop,分数回退可以
精确归因到自己的代码;旧方案隔着一层框架,回归了都不知道是我的装配问题还是
框架行为。"

**可能追问:**

- *"字符串断言太弱?"* —— "分层策略:便宜断言先铺覆盖面,回答'能力有没有
  发生';评回答质量是另一个问题,升级路径留了 rubric 字段 + LLM judge,
  判分框架不用动。"
- *"怎么让 agent 涨分?"* —— 进化闭环:"评测回答现在几分,进化回答怎么涨。
  抄 PenguinHarness 的纪律:每轮先快照 prompt 和记忆文件,派 optimizer
  子代理读失败用例提可证伪假设、改 AGENTS.md,重跑 eval 分数不严格提升就
  回滚;优化者看不到判分细节防作弊;scoreboard 记 版本×用例×run 三级可追溯。
  task 子代理就是执行器,评测矩阵每格派一个,主对话收结果。"
- *"为什么不用现成评测框架?"* —— "看过 pi 的 vitest-evals 和 deepagents 的
  pytest-harbor,前者绑 TS 生态,后者绑 LangSmith。我的判分就是几个纯函数,
  百行脚本足够;框架等用例规模和并行需求真到了再引入。既然 loop 都手写了,
  评测更没有理由背框架。"

## 9. 路线图

1. **评测侧**:rubric LLM judge → 多 run 稳定性 → evolve.py 进化闭环;
2. **能力侧**:工具调用白名单 + interrupt 审批(Tool 形态统一后是纯增量);
3. **可观测**:每轮 token/成本/时长进 session 文件,评测报告带成本维度。
