# eval/skill-loop — skill 编排的闭环评测（penguin 式）

评测对象是**整个 zany**（一个 git commit：代码 + SYSTEM_PROMPT + skills + styles 的完整功能面）。
机制零运行时代码：编排全在 3 个 SKILL.md 里，平台只出两个现成零件——`task` 子代理（Evaluator）和
`-p` 无头模式（被测入口）。完整架构见 `docs/self-improvement.md`。

## 三个 skill 的分工

| skill | 角色 | 职责 |
|---|---|---|
| `skills/benchmark-design/` | Builder | 出考卷：statement/rubric 分离、泄漏检查、Pilot 校准难度、冻结基线进 scoreboard |
| `skills/agent-evaluation/` | Evaluator | 改卷子（作为 task 子代理跑）：建隔离 workspace → 跑 `uv run core/tui.py -p` → 按 scorer 打分 → 纯协议 YAML 返回 |
| `skills/agent-optimization/` | Optimizer | 提分循环：可证伪假设 → 工作区改动 = Candidate → L1 回归 + L2 矩阵 → 分数严格提升才 `git commit`，否则 `git checkout -- .` |

## 怎么跑

- **Builder**：开一个 zany 会话，read_file 本目录 `benchmark-design/SKILL.md`，按它执行（需要目标能力、期望基线分、Pilot 迭代上限）。
- **Optimizer**：另开一个独立会话（不共享上下文），read_file `agent-optimization/SKILL.md`，按它执行（需要目标分、runs、轮次上限；要求 benchmark 已 published）。
- **Evaluator** 不靠 skill 发现（task 子代理不载 skills）：由 Builder/Optimizer 把 `agent-evaluation/SKILL.md` 全文拼进 task prompt 发格子。
- 执行是串行的（loop 单 worker 队列）：一个 case ≈ 一次完整 agent 会话，8 case 一轮矩阵按 20-40 分钟挂机预期。

## 产物

`eval/benchmarks/<benchmark_id>/`（进 git）：`benchmark_config.toml`（status: draft/published/failed）、
`scoreboard.yaml`（全部分数，每 run 带 session_id 链回 `~/.zany/sessions/` 的完整 Trace）、
`CASE-*/statement/`（公开任务书）与 `CASE-*/rubric/`（私有评分项，头部声明 scorer：assert | rubric | deepeval:<metric>）。

## 纪律（分数可比性的全部来源）

rubric 评测前定死；statement/rubric 物理分离且 workspace 只拷 statement；失败 ≠ 0 分；
分数严格提升才接受、被拒改动不留版本；每 run 记 session_id 可追溯。zany 无沙箱无路径 jail——
信息隔离是指令级屏障（statement 不给仓库/eval 路径 + Evaluator 查 Trace 里有无 rubric 读取），不是安全边界。
