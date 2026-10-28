# deepagents-mini-agent:设计与实现 —— 介绍文档

> 一个基于 deepagents 框架的极简编码 agent:从模型接入、agent loop、TUI/无头双入口,
> 到评测闭环的完整设计。本文按"面试官会追问什么"组织:每个设计都给出**为什么这么选、
> 放弃了什么、什么时候升级**。

## 1. 一句话定位

一个能读、写、跑 shell、改代码的对话式编码 agent,约 800 行 Python。
不造轮子:agent 循环和工具层基于 PyPI `deepagents`(LangGraph 生态),自己写的部分是
**装配层(选哪些中间件、怎么拼)、交互层(TUI/无头)、持久化(会话恢复)和评测闭环**。

选 deepagents 而不是从零写 loop 的原因:agent loop 的难点不在"循环调用模型直到没有
tool_call",而在**上下文工程**——压缩、记忆注入、工具结果裁剪、prompt cache 断点。
这些 deepagents 以中间件形式内置,自己写等于重新踩一遍坑;而框架留的装配点
(middleware / backend / harness profile)足够我做产品级定制。

## 2. 架构设计

### 2.1 分层

```
┌─ 交互层 ────────────────────────────────┐
│ tui.py  唯一入口: 无参 = Textual TUI;    │
│         -p = 无头模式(供脚本/后台调用)    │
├─ 编排层 ────────────────────────────────┤
│ agent.py  build_agent(): 模型+工具+      │
│           中间件栈的唯一装配点            │
├─ 框架层 ────────────────────────────────┤
│ deepagents 0.7 (LangGraph): 文件工具、   │
│ 子代理、记忆、压缩、skills                │
├─ 支撑层 ────────────────────────────────┤
│ sessions.py 会话落盘/恢复                │
│ mcp_tools.py  MCP 工具热加载             │
│ cron_jobs.py  定时任务                   │
└─────────────────────────────────────────┘
```

关键决策:**单文件入口、双模式**。交互和无人值守共用同一个 `build_agent` 和同一套
会话落盘,避免"demo 版"和"生产版"行为漂移——这也是 Claude Code `claude -p` 的做法。

### 2.2 agent 装配

```
init_chat_model("anthropic:GLM模型", Bearer 代理头)
  ↓
create_deep_agent(
    backend=LocalShellBackend(workdir),   # 文件工具以 workdir 为虚拟根: /agent.py = 真实的 workdir/agent.py
    tools=load_mcp_tools(),               # MCP 工具混入
    skills=["/skills", "/.claude/skills"],
    middleware=[
        FilesystemMiddleware,    # ls/read_file/edit_file/write_file/execute
        MemoryMiddleware,        # 每轮把 AGENTS.md 注入 system prompt(长期记忆)
        SubAgentMiddleware,      # task 子代理(默认开)
        SummarizationMiddleware, # 上下文压缩
    ],
)
```

三个值得展开的取舍:

**① 路径虚拟根。** 文件工具的路径以 workdir 为根(`/agent.py`),`execute` 却是真实
shell。在 system prompt 里写死这条约定,防止模型拼绝对路径踩 "File not found"。
—— 这本质是"给模型的接口设计":把最容易犯的错变成 prompt 里的一句规则,而不是
在工具层做路径重写(重写会掩盖 execute 和文件工具的不一致)。

**② system prompt 不入历史。** MemoryMiddleware 每次调模型时现拼
SYSTEM_PROMPT + AGENTS.md,用完即弃,不写进 messages。否则记忆每轮重复累加,
上下文膨胀 + prompt cache 全部失效。AGENTS.md 由模型自己用 edit_file 维护,
"用户纠正过的事情记进去"——这是 agent 自演化的最小形态。

**③ 压缩选了"改写式"而非"非破坏式"。** deepagents 默认压缩把原文 offload 到
conversation_history/、状态存私有字段,跨进程恢复依赖 langgraph checkpointer;
本项目没有 checkpointer,持久化靠 sessions/*.json 覆盖写。用 langchain 原版
SummarizationMiddleware 直接改写 messages 为 summary + 尾部 20 条,压缩结果
天然落盘,恢复不会爆上下文。**取舍:丢了原文外置(全量记录仍在历史 session 文件里),
换来恢复路径零依赖。** 这是"持久化模型决定中间件选型"的一个具体例子。

### 2.2 安全模型

文件工具可配 FilesystemPermission,但 `execute` 绕过一切文件权限——和 Claude Code、
deepagents、PenguinHarness 的共识一致:**权限列表永远不是安全边界,OS 级隔离才是**。
当前定位是开发者在可信环境用,所以没做沙箱;若要无人值守跑在不可信目录,升级路径是
给 backend 换容器/namespace 隔离(LocalShellBackend 是接口的实现,不是接口本身)。

## 3. Agent Loop 设计

### 3.1 基本循环

LangGraph 的标准 ReAct 环:模型节点 →(有 tool_call?)→ 工具节点 → 回模型,
直到没有 tool_call。自己做的部分在环外:

- **每轮 = 一次用户输入到最终回复**,轮内模型可多次调用工具;
- 轮结束整份 state 覆盖写 session 文件(首轮创建),崩溃最多丢当轮;
- resume = 把 messages 原样带回去接着跑,上下文无缝。

### 3.2 流式与并发模型

TUI 里 agent 跑在后台线程,用 `stream_mode=["messages", "values"]` 双流:

- `messages` 流逐 token 出思考/正文/工具入参,驱动在屏流式区;
- `values` 流拿最终 state,作为落盘和定稿依据。

三个流式处理的细节(都是踩坑后修的):

1. **工具入参跨 chunk 重组**:tool_call_chunks 的续片没有 id,要归到"最近见过的
   call id"下累积,否则入参 JSON 拼不完整。
2. **思考/正文分色**:GLM 的 reasoning 以 content block 数组到达,按
   `type == thinking/text` 分流,思考洋红、正文白色,轮结束正文定稿进聊天区。
3. **同轮排队**:上一轮没跑完时新输入提示等待,不做并发轮——agent state 是线性的
   messages,并发轮会互相踩。

### 3.3 双入口

- **TUI**:多行输入框(参考 pi/dcode 的"连击检测"——无 bracketed paste 的终端会把
  粘贴回放成一串按键,短时间窗内的 enter 当作粘贴换行而不是提交)、/resume 弹层、
  MCP 配置热重载(mcp_servers.json 变更 → 下轮重建 agent)、cron 唤醒入队。
- **无头 `-p`**:单次 invoke,`-r <id>` 接会话续聊,id 不存在则以该 id 新开(供
  watcher/工单系统多轮调用),stdout 只出最终回复、stderr 出 session id——
  **stdout 机器读、stderr 人读**,方便管道集成。

### 3.4 上下文管理策略

三层防线:工具结果超限自动截断/外置(框架自带)→ 80k token 触发摘要压缩(保留
最近 20 条)→ 恢复时靠压缩后的落盘。长期记忆走 AGENTS.md 而不是塞进 messages,
配合"改完下一轮生效",形成"短期靠窗口、长期靠文件"的分层。

## 4. 评测设计

### 4.1 设计原则(从三个参考实现里提炼)

调研了三套参考:

| 实现 | 评测形态 | 我取什么 |
|---|---|---|
| pi (packages/evals) | vitest 用例文件,断言写在代码里,docker 隔离,LLM judge | 每例独立隔离环境;最终回复 + 产物双检查 |
| deepagents (libs/evals) | pytest + scorer,check 即函数:file_contains / final_text_contains / tool_call 次数 | check 类型学:状态断言 + 过程约束 |
| PenguinHarness | 零框架代码,纯 Skill 编排:Builder/Evaluator/Optimizer 四角色,Rubric 与 Statement 隔离,**分数严格提升才接受改动** | 进化闭环的纪律,而非机制 |

提炼出的两条原则:

1. **用例即数据,判分即代码。** 用例(JSONL 一行一例:seed 文件 + query + checks)
   和判分逻辑分离,加用例不改代码,模型换版/prompt 调整后重跑即得对比分。
2. **检查产物,不检查过程话术。** 让 agent 把结果落到文件、然后断言文件内容,
   比断言回答字符串鲁棒得多;过程指标(max_steps/工具次数)只作为效率红线。

### 4.2 评测 loop 实现

```
eval/main.py: JSONL 用例 → tempfile.TemporaryDirectory 隔离 → build_agent(workdir).invoke
          → 四类 check 判分 → PASS/FAIL + N/M 汇总
```

- `seed`:每例预置初始文件,构造"读-改-写"场景;
- checks:`answer_contains`(最终回复)/ `file_contains`、`file_exists`(工作区产物)
  / `max_steps`(防绕圈,效率红线);
- 隔离用 `tempfile.TemporaryDirectory`:每例全新目录 + 全新 agent,互不污染,
  天然并行安全——比 pi 的 docker 隔离轻三个数量级,对纯文件/shell 任务够用;
- `eval/main.py <pattern>` 按名字过滤,先跑 smoke 子集再跑全量。

样例四条覆盖四类能力:读后答(检验分页读取)、写文件(检验产物)、精确编辑
(检验 edit 而非整文件重写)、真实 shell 跨虚拟根(检验路径约定是否被模型遵守)。

### 4.3 进化闭环(设计,迭代中)

评测回答"现在几分的",进化回答"怎么涨分"。参考 PenguinHarness 的**纯编排进化**
(它没有任何框架代码,全靠角色化 Skill + 协议约束),映射到本项目:

```
evolve.py(规划中)
  1. 快照当前 SYSTEM_PROMPT + AGENTS.md → snapshots/v<N>.tar.gz
  2. optimizer 轮: tui.py -p "读 scoreboard 和失败 case, 提出可证伪假设, 改 AGENTS.md"
  3. 跑 eval/main.py, 分数没有严格提升 → 从快照回滚
  4. 追加 scoreboard.yaml(每版本每 case 每次运行的分数), 回到 2
```

三条从 Penguin 抄的纪律:

- **Rubric 与 Statement 隔离**:优化者只看分数和公开陈述,看不到判分细节,
  防"围绕评分规则作弊";
- **严格提升才保留**:每次改动先快照,分数不涨即回滚,杜绝"改了感觉良好";
- **分数可追溯**:scoreboard 记录 版本×case×run 三级,任何分数能回放来源。

这里 `task` 子代理正好是执行器:评测矩阵的每格(case × run)可以派一个子代理
独立跑,主对话只收结果——Penguin 用 `run_subagent` 做的事,同构。

### 4.4 评测的升级路径(按触发条件)

| 现状 | 触发条件 | 升级 |
|---|---|---|
| 字符串断言 | 需要评"回答质量"而非"文件对不对" | cases 加 `rubric` 字段 + LLM judge 打 0-100 分 |
| 每例跑 1 次 | 分数开始抖(模型随机性) | `runs` 字段,多 run 求均值报方差 |
| 单机串行 | 用例多了慢 | 子代理矩阵并行 / 进程池 |
| 单轮任务 | 需要评多轮连续任务(改 bug → 跑测试 → 修到过) | case 带 `steps` 数组,逐轮断言 |
| tempfile | 需要真实仓库任务 | SWE-bench 式:git 固定提交 + 测试套件当判据 |
| 无对比 | 调 prompt / 换模型 | 记录基线,报 lift(pi 的 docs-lift 模式:同一用例开关某能力跑对照) |

## 5. 如果继续做(路线图)

1. **评测侧**:rubric LLM judge → 多 run 稳定性 → evolve.py 自动进化闭环;
2. **能力侧**:权限/审批中间件(工具调用白名单 + interrupt 确认),对接真实
   工单系统做无人值守多轮(无头 -r 已就绪);
3. **可观测**:每轮 token/成本/时长进 session 文件,评测报告带成本维度——
   agent 的分数要在成本约束下看才有意义。

## 6. 一分钟版本(电梯陈述)

> 我基于 deepagents(LangGraph 生态)做了一个极简编码 agent,自己写的部分是装配层、
> TUI/无头双入口、会话持久化和评测闭环。loop 本身站在框架肩上,我的工作在三个
> 判断:① 持久化模型决定中间件选型——没有 checkpointer 就选改写式压缩,让压缩
> 结果天然落盘;② 评测用"用例即数据、判分即代码",检查工作区产物而不是过程话术,
> tempfile 隔离做到零依赖并行;③ 进化闭环抄 Penguin 的纪律——Rubric 隔离、
> 快照回滚、分数严格提升才接受,机制可以简陋,纪律不能没有。

## 7. 话术版:追问怎么答

### 7.1 "你的 agent loop 是怎么设计的?"(主答,约 2 分钟)

"Loop 本身是 LangGraph 的标准 ReAct 环:模型节点发消息,有 tool_call 就进工具节点,
工具结果回填再回模型,直到没有 tool_call 轮结束。我刻意没有自己写这个环,因为
工程难点不在环,在环上挂的上下文工程——压缩、记忆注入、工具结果裁剪,这些我用
deepagents 的中间件栈解决,我的工作是把环**包成一轮一轮的会话语义**和踩平流式的坑。

具体到我自己写的部分,三个决策:

第一,**轮的边界**。我把'一轮'定义为一次用户输入到最终回复,轮内模型可以自由调
任意多次工具。轮结束把整个 state——就是那份线性 messages 数组——覆盖写进 session
文件,首轮创建、之后覆盖,崩溃最多丢当轮。恢复就是把 messages 原样带回 agent 再跑,
上下文无缝。我不做并发轮,因为 state 是线性数组,两个轮同时写会互相踩;上一轮没跑
完,新输入就排队。

第二,**双流式**。TUI 里 agent 跑在后台线程,我用 LangGraph 的
`stream_mode=["messages", "values"]` 同时订阅两条流:messages 流逐 token 吐思考、
正文、工具入参,驱动屏幕上的流式渲染;values 流给最终 state,作为落盘和定稿依据。
这里踩过三个坑,都修了:一是工具入参是分片到达的,tool_call_chunks 的续片不带 id,
要归到'最近见过的 call id'下面累积,否则入参 JSON 拼不完整;二是 GLM 的思考内容
以 content block 数组到达,要按 block 的 type 分流,思考染成洋红、正文白色,轮结束
正文才定稿进聊天记录;三是最终回复必须从 values 流的 state 里取最后一条 AI 消息,
不能从流里攒——流里混着工具调用前的中间叙述。

第三,**上下文管理是三层防线**。第一层框架自带:单个工具结果超限自动截断或外置成
文件;第二层 80k token 触发摘要压缩,保留最近 20 条消息;第三层长期记忆不进
messages,走工作目录的 AGENTS.md,每轮调模型前注入 system prompt,用完即弃——
这样记忆不占上下文、不破坏 prompt cache,而且由模型自己用编辑工具维护,算是
agent 自演化的最小形态。压缩有个选型:deepagents 默认压缩是非破坏性的,原文
外置、状态存私有字段,跨进程恢复依赖 checkpointer;我没有 checkpointer,持久化
就是 session 文件覆盖写,所以换成 langchain 原版的改写式压缩——直接把 messages
改写成摘要加尾部,压缩结果天然落盘。这个决策的逻辑是:**持久化模型决定中间件
选型**。"

**可能追问:**

- *"为什么不自己写 loop?"* —— "自研环大概两百行就跑通,但之后每个上下文问题都
  要自己补:压缩时机、记忆注入点、工具结果超限、prompt cache 断点。deepagents
  把这些做成中间件,我挂在栈上就行。我的价值在选型和拼装,不在重写。"
- *"为什么改写式压缩,不怕丢信息?"* ——"非破坏式的好处是原文可回放,但它的状态
  跨进程恢复要 checkpointer,我为了零依赖选了 JSON 覆盖写,两者冲突。改写式丢的
  原文在历史 session 文件里还有全量,等于用磁盘换依赖。如果以后要审计回放,再把
  offload 加回来,接口不用动。"
- *"无头模式和 TUI 行为一致吗?"* ——"同一个 build_agent、同一套落盘,唯一区别是
  无头模式单次 invoke 不挂 cron、stdout 只出最终回复、stderr 出 session id,
  stdout 机器读 stderr 人读,方便工单系统管道集成。"

### 7.2 "评测怎么做的?"(主答,约 2 分钟)

"评测的核心是两条原则,都是调研了 pi、deepagents 官方 evals、PenguinHarness 三家
之后提炼的:

第一,**用例即数据,判分即代码**。用例是 JSONL,一行一例:seed 文件、query、checks。
判分逻辑独立,加用例不改一行代码。这样换模型或者调 prompt 之后重跑一遍就得到
对比分——评测的价值在'可对比',不在'跑一次'。

第二,**检查产物,不检查过程话术**。我让 agent 把结果落到文件,断言文件内容,
而不是断言回答字符串。比如'把配置里 mode=dev 改成 prod'这条用例,判分看
config.ini 的内容,模型嘴上怎么说无所谓。过程指标只保留一条效率红线:max_steps,
防止 agent 绕圈烧 token。

实现上很薄:每条用例开一个 tempfile.TemporaryDirectory,把 seed 文件铺进去,
build_agent(临时目录) 起一个全新 agent 跑一轮,然后过四类 check——
answer_contains 查最终回复,file_exists / file_contains 查工作区产物,max_steps
查步数。输出 PASS/FAIL 和 N/M 汇总,支持按名字过滤,先跑 smoke 子集再跑全量。

隔离的选择我对比过:pi 用 docker,每例一个容器;我用临时目录,轻三个数量级,
而且天然并行安全——每例独立目录加独立 agent,互相看不见。对纯文件和 shell 任务,
这个隔离强度是够的。"

**可能追问:**

- *"字符串断言会不会太弱?"* ——"会,这是我有意的分层。现在四类 check 回答的
  问题是'能力有没有发生':会不会读、会不会精确编辑而不是整文件重写、路径约定
  有没有遵守。评'回答质量'是另一个问题,升级路径已经留了:用例加 rubric 字段,
  接 LLM judge 打 0-100 分,判分框架不用动。先让便宜断言覆盖面铺开,再上贵的。"
- *"模型有随机性,跑一次准吗?"* ——"单 run 分数会抖,这在设计里是已知项。办法是
  用例加 runs 字段,每例跑 N 次报均值和方差。我现在的策略是先单 run 把用例集
  铺起来,等分数真的开始抖——说明用例难度卡在模型能力的噪声区了——再上多 run,
  因为多 run 的成本是线性乘 N,不该过早付。"
- *"怎么让 agent 变得更好,不只是打分?"* —— 这里接进化闭环:"评测回答'现在几分',
  进化回答'怎么涨分'。我参考 PenguinHarness 的做法——它整个进化系统零框架代码,
  全靠角色编排。我的版本:每轮先快照当前 SYSTEM_PROMPT 和 AGENTS.md,然后派一个
  optimizer 子代理读 scoreboard 和失败用例,提出一个**可证伪的假设**、改 AGENTS.md,
  重跑 eval,分数没有严格提升就从快照回滚,分数全部进 scoreboard.yaml,按
  版本×用例×run 三级可追溯。三条纪律:优化者看不到判分细节,防止围绕评分规则
  作弊;严格提升才保留,杜绝'改了感觉良好';每个分数能回放来源。这里的执行器
  正好是 deepagents 的 task 子代理,评测矩阵每格派一个子代理独立跑,主对话收
  结果就行。"
- *"为什么不用现成评测框架?"* ——"看过 pi 的 vitest-evals 和 deepagents 的
  pytest-harbor,前者绑定 TS 生态,后者绑定 LangSmith。我的判分需求就是几个
  纯函数,一个百行脚本足够;框架我借的是它们的 check 类型学和隔离模型,不是
  代码。框架等用例规模或并行需求真正需要时再引入。"
