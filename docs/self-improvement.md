# zany 评测与自进化架构（benchmark + evolution loop）

> 来源：PenguinHarness `agent-tuning` 插件机制（`/data01/tsn/code/github_code/penguin-harness/plugins/agent-tuning/skills/`），
> 按 zany 的形态重设计。与 penguin 的核心差异一条：**评测对象是整个 zany**（一个 git commit：
> 代码 + SYSTEM_PROMPT + skills + styles），不是某个 prompt/state 文件——进化改的就是仓库本身。

## 0. 结论先行

- **零运行时**：整套机制没有一行专属评测服务代码，编排全在 3 个 skill 文本里；平台侧只出两样现成零件——`task` 子代理（并行 Evaluator）和 `-p` 无头模式（被测入口）。
- **git 即快照**：Reference = HEAD、Candidate = 工作区未提交改动、接受 = commit、被拒 = `git checkout -- .`。penguin 的 tar.gz 快照 + 版本号机制整个不需要。
- **三层体系**：Layer1 功能回归（`eval/main.py` 现款，硬断言 0/1）做守门员；Layer2 能力 benchmark（rubric 100 分制 LLM 打分）测"整个 agent 干真实任务的能力"；Layer3 进化循环（严格提升才接受）。进化必须三层同跑：Layer1 不过一票否决。
- **纪律比代码重要**：五条纪律（§7）是分数可比性的全部来源，先于任何实现存在。

## 1. 三层评测体系

| 层 | 回答的问题 | 判分 | 入口 | 现状 |
|---|---|---|---|---|
| L1 功能回归 | 改动有没有弄坏已有功能 | 硬断言，0/1 | `eval/main.py`（进程内 `build_agent`） | **已有**（cases.jsonl） |
| L2 能力 benchmark | 这个版本的 zany 整体多能干 | rubric 100 分制，LLM judge | `-p` 无头（真实入口，保真） | 本文档新增 |
| L3 进化循环 | 谁来改、什么时候留 | 分数严格提升才接受 | Optimizer 会话 | 本文档新增 |

## 2. 角色与拓扑

照抄 penguin 四角色，映射到 zany：

| 角色 | 形态 | 职责 | zany 对应 |
|---|---|---|---|
| Builder | 顶层会话（跑一次） | 设计 benchmark + 校准 + 记基线 | 载入 `benchmark-design` skill 的 zany 会话 |
| Target | 每次 run 一个 | 被测者，在隔离 workspace 干活 | `uv run <repo>/core/tui.py -p "<query>"`，被测版本 = 仓库当前 git 状态 |
| Evaluator | 叶子子代理，每 case×run 一个 | 跑 Target 一次 + 私下打分 | `task` 工具派发（一轮多 tool call 并行，子代理只回传结论的语义正好是协议返回） |
| Optimizer | 另一个独立顶层会话 | 可证伪假设驱动改 zany，严格提升才保留 | 载入 `agent-optimization` skill 的 zany 会话 |

Builder 与 Optimizer 是两个独立会话（不共享上下文），用户手动分两步发起。信息隔离靠 skill 指令 + Trace 事后审计——**zany 无沙箱无路径 jail（AGENTS.md 约定），这条取舍与 penguin 相同且更彻底**，见 §6。

## 2.1 执行适配（被测入口）

已确认的现款事实：

- `-p` 无头：`cwd` 即被测 workspace；stdout = 最终回答；stderr 带 `(session: <id>)` 和 token 用量；会话完整落 `~/.zany/sessions/<workdir>-<hash>/<id>.json` = 审计 Trace 与追溯锚点。`--resume` 可测多轮/会话恢复类 case。
- 进程内入口 `build_agent(workdir)`（eval/main.py 现款）只用于 L1（快、能精确数 tool_calls）。
- 会话隔离：workspace 用临时目录（tmpfile/`eval/benchmarks` 之外的临时目录），会话桶随 cwd 哈希自然隔离。
- 同一时刻整个仓库工作区只能处于一个版本状态 → 矩阵**串行按版本跑**（先把 Reference 矩阵跑完、再切 Candidate）；要并行按版本需 `git worktree` 双目录，属进阶，起步不做。

## 3. 目录契约

```
eval/benchmarks/<bench_id>/
├── benchmark_config.toml      # title / runs=1 / status: draft|published|failed
├── scoreboard.yaml            # 全部历史分数（进 git，文件即事实来源）
└── CASE-<nnn>-<slug>/
    ├── statement/README.md    # 公开任务书：只给被测 Target 的
    └── rubric/
        ├── README.md          # 私有：评分项+分值+Gold，只有 Evaluator 读
        └── metrics.yaml       # 可选：deepeval 指标声明（metric/steps/threshold/分值权重）
```

scoreboard.yaml 单条（照 penguin，字段含 git 语义）：

```yaml
evaluations:
  - time: <ISO-8601 UTC>
    version: <git short hash；工作区脏则 <hash>+dirty>
    summary_title: >-        # 本轮结论
    summary: >-              # 给下一轮的假设线索
    score: <全 case 平均，两位小数>
    cost: <全 case 平均>
    duration_ms: <全 case 平均>
    cases:
      - case: CASE-001-fix-fuzzy-edit
        scorer: deepeval:GEval       # 打分器标识，同一 case 恒定（换=改题=重新校准）
        score: ...
        runs:
          - score: <0..100>
            duration_ms: ...
            session_id: <~/.zany/sessions 桶内 id>   # 追溯锚点，链回完整 Trace
```

## 3.1 打分器层（scorer）：deepeval 的结合点

deepeval 是**打分器实现**，不是编排层——skill 循环、git 语义、协议 YAML、纪律五条全部不变，
变的只是 Evaluator 最后一步"按 rubric 打分"的实现。三种打分器并存，case 的 rubric 里声明用哪个：

| scorer | 实现 | 适用 |
|---|---|---|
| `assert` | L1 硬断言（eval/main.py） | 功能回归，能穷举关键词的；**不要换 judge**（flaky+烧钱） |
| `rubric` | 自由文本评分项，Evaluator 子代理直读直打（penguin 式） | 要审计完整 session Trace 做过程判断的（"是否重复瞎试""有没有乱来"） |
| `deepeval:<metric>` | 结构化指标，execute 跑公共打分脚本 | 判分点能写成检查输出性质的 |

deepeval 侧用两个指标起步，不贪多：

- **GEval**：rubric 的结构化形态。Builder 设计时就用 judge 把 evaluation_criteria 生成为 evaluation_steps 并**冻结进 `rubric/metrics.yaml`**（"评分标准评测前定死"纪律的 deepeval 语义）；打分时每指标只调 1 次 judge（步骤生成那半省掉）。
- **ToolCorrectness**：直接吃 session JSON 里的 tool_use 序列对比 expected_tools——zany 的 Anthropic 原生消息抽 `tools_called` 零转换，是"评整个 agent 过程行为"最贴的现成指标。

工程形态（复用 agent-quality-eval 技能现款，零新增配置）：

- 公共打分脚本 `eval/deepeval/run_eval.py`（收编自 agent-quality-eval 技能的 deepeval_demo.py，独立评测现款已可用）：skill-loop 的 Evaluator 调它当 scorer 的入口（读 TestCase 字段 + metrics.yaml → 输出分数+reason）属待接线。judge 就是 `core.model.ChatModel`，自定义 generate 带 schema **返回实例不返回元组**（技能硬约定）。
- Evaluator 子代理流程：跑被测 → 从 session JSON 抽字段组 TestCase → execute 调打分脚本 → score×100 按协议 YAML 返回。
- deepeval 侧失败（judge 抽风/schema 崩）= scoring 失败 ≠ 0 分：重试只重跑 judge，不重跑被测（输入固定，比自由 rubric 更省）。
- 遥测保持关闭（`DEEPEVAL_TELEMETRY_OPT_OUT=1`），不推 Confident AI 云。

## 4. Evaluator 协议

每个格子 = 一个 task 子代理，指令：建临时 workspace → 拷 statement → `cd` 进去跑被测命令 → 按 rubric 打分 → 返回**纯协议 YAML**：

```
status: ok | evaluation_failed
case_id / score(两位小数) / cost / duration_ms / session_id
```

- **失败绝不记 0 分**（`evaluation_failed` = 启动失败/评分器自身故障，重试需新证据，不设数值上限）；答案错/产物缺/格式坏都是正常打分。
- 返回格式坏（混入叙述）：让**同一个**子代理重发，不重跑被测 agent（重跑引入分数扰动）。
- cost/duration 取 stderr 的 token 行 + 实测墙钟；取不到写 null，不影响分数有效性。

## 5. 进化循环（Optimizer 每轮八步）

1. 确立 Reference：HEAD + 它在冻结 benchmark 上的基线评测（scoreboard 最新一条）。
2. 诊断缺口：per-case 分数对比 + 按 session_id 读 Target 的 session JSON，找可观察的行为缺口（多次 run 区分稳定缺陷与随机波动）。
3. 可证伪假设：必须写明"改完后哪几个 case 的哪类可观察行为会变、为什么"。只加分析不预测行为变化的改动不算假设。
4. 造 Candidate：工作区直接改（代码、prompt、skills、styles 都合法——评测对象是整个 zany）。开工前确认工作区干净，否则停。
5. admissibility 检查：改动通用（不 case 特判硬编码）、未掺 rubric 私有信息；发现私有信息进入 Optimizer 上下文 → 回滚并停。
6. **先跑 L1 全回归**（`eval/main.py`，任一失败即拒绝 Candidate），再并行派 task 跑 L2 完整矩阵。
7. 裁决：矩阵全有效且平均分**严格**高于 Reference 才 `git commit`（假设是否被验证单独记录，分数说话）；否则 `git checkout -- .` 恢复。被拒的改动不留版本。
8. 接受的评测立即追加进 scoreboard（带新 commit hash）再开下一轮。达标提前停，轮尽保留最高分版本。

## 6. 防作弊：zany 的一个真实弱点，如实记录

penguin 有沙箱插件，rubric 至少物理上不进被测 workspace；zany **路径不做重写、无 jail**，被测 agent 的 read_file 绝对路径直用——workspace 在 tmp，但它理论上能读仓库里 `eval/benchmarks/**/rubric/`。zany 无沙箱可加（约定使然），所以：

- **主防线**：statement 与运行指令绝不给仓库路径；rubric 虽在仓库（可审计必须进 git），但 skill 指令明令 Target 不得读 `eval/`；
- **审计兜底**：Evaluator 打分前查该 run 的 session Trace 里有无 eval/rubric 路径的读操作，命中即该 run 作废记 `contaminated`；
- **泄漏检查**：Builder 每次改题后对照公开文件与 rubric（"公开文件不暴露 Gold、私有评分条件或解题提示"）；
- 与 penguin 相同的诚实声明：这是指令级屏障，不是安全边界；被评测的是我们自己的 agent，作弊动机为零，屏障防的是**无意的**分数污染。

## 6.1 case 清单草案（覆盖整个 zany 功能面）

| case 主题 | 考的功能 | rubric 区分点 |
|---|---|---|
| 修一个带坑的 bug（seed 含智能引号/BOM 文件） | edit 精确+模糊匹配、read/write | 是否无植入式修改、CRLF/BOM 保留 |
| 多步构建任务（写脚本→跑→修到过） | execute、错误自愈 | 靠报错文本自愈而非瞎试 |
| 按需用 skill（seed 放一个 SKILL.md，任务要用它） | skills 渐进披露 | 是否读全文并按规范产出 |
| 大量重复子任务（如 20 个文件同构处理） | task 子代理派发 | 派发时机与结论汇总质量 |
| 前一步必报错的任务链 | 错误即文本、防熔断 | 报错后换路而非重复同一命令 |
| 超长工具输出后续任务 | 截断保头尾防爆层 | 从截断输出里捞对关键信息 |
| 两轮对话任务（第二轮改需求） | 会话恢复 /resume | 上下文保持 |
| 端到端小项目（README 需求 → 可跑代码） | 综合 | 结构合理性、实际可运行、步骤效率 |

起步 6-8 个 case，runs=1；rubric 总分恒 100、必须有部分分、分值压在"强 agent 与合理捷径产生不同可打分产物"的点上。评分标准看答案前定死，之后不改（防背题）。

## 7. 纪律（照抄 penguin，五条）

1. rubric 评测前定死，之后永不改；
2. statement/rubric 物理分目录，workspace 只拷 statement；
3. 失败 ≠ 0 分；
4. 分数严格提升才接受，被拒改动不留版本号（git checkout 语义下自动成立）；
5. 每 run 记 session_id，任何分数可链回完整 Trace。

裁剪掉的 penguin 部件：tar.gz 快照（git 覆盖）、Pilot→Freeze 完整状态机（简化为"校准跑几轮、分数合理即冻结 status=published"）、多 run 平均矩阵（成本，runs=1 起步）、server 端渲染（直接看 scoreboard.yaml）。

## 7.1 打分器选型与 agent-quality-eval 技能的关系

agent-quality-eval 技能（全局 ~/.zany/skills/）是 deepeval 接法的经验库：judge=项目 ChatModel、
uv --with 临时叠加、schema 返回约定、SWE-bench harness 现款——`eval/deepeval/` 沿用其约定，
不另立 judge 体系。选型标准一句话：**能用 assert 的用 assert，能步骤化的用 deepeval，
要读完整 Trace 做过程判断的才用自由 rubric**——judge 每多一分自由度就多一分 flaky。
SWE-bench（技能内已通全链路）是第四种独立口径：从 Verified 抽实例当 statement、
官方 harness 判定，属 L2 的特例来源，起步不碰。

## 8. 实施切分

1. 三个 skill 已移植至 `eval/skill-loop/skills/{benchmark-design,agent-evaluation,agent-optimization}/SKILL.md`，方式说明 `eval/skill-loop/README.md`（用户定：按评测方式分目录，不入项目根 skills/ 发现路径）。使用方式：Builder/Optimizer 顶层会话开场 read_file 对应 SKILL.md 照单执行；Evaluator 不靠 skill 发现——调用方把 agent-evaluation SKILL 全文拼进 task prompt（子代理不载 skills）。原文改写点：run_subagent→task、penguin run→`uv run core/tui.py -p`、快照→git、app_data_dir→eval/benchmarks/、禁改清单按 zany 重划（禁改 eval/ 下的 benchmark 与判分件，其余全可改）。
2. deepeval 评测方式已落 `eval/deepeval/`（README + run_eval.py，收编自全局技能 deepeval_demo.py，独立评测现款可用）；方式说明 README 已配。剩余接线：skill-loop Evaluator 调 run_eval.py 当 scorer 的入口（metrics.yaml → TestCase 组装 → 分数+reason 输出）。
2. 首轮 Builder 会话：建 `eval/benchmarks/zany-core-v1/`（6-8 case）+ 校准 + 冻结基线。
3. 首轮 Optimizer 会话：一个真实改进假设走完整循环，验证闭环可跑通。
4. 进阶（跑顺再说）：git worktree 双版本并行矩阵、runs>1、continual-learning 式经验回流（goal 模式 turn_finished 注入点是现成缝）。
