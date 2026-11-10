---
name: benchmark-design
description: 为整个 zany（一个 git commit 的完整功能面）设计并校准能力 benchmark，冻结并记录可追溯的 Formal Baseline。Builder 会话载入本文件执行。
---

# Benchmark Design（zany 版）

为一个评测目标（整个 zany 仓库，被测版本 = git commit）设计多 case 能力 benchmark，逐 case 校准难度（每 case 1 run），冻结后把选中的 Pilot 结果记为 Formal Baseline。

本 skill 只改 benchmark，不改被测仓库、不亲自跑被测。所有评测经 `task` 工具派发，把 `eval/skills/agent-evaluation/SKILL.md` 全文连同协议头拼进 prompt。基线记完即停，不开始优化。

## 输入

缺以下任一项先向用户要：目标能力描述、期望基线分、Pilot 迭代上限。齐了就开工，不要复述确认。评测运行时 = 被测仓库 `.env` 的 `ANTHROPIC_MODEL`（及端点），第一次 Pilot 前解析一次并冻结整个流程；每个返回结果必须回报同一模型，不匹配整个矩阵作废。

前置检查（吸收原 agent-initialization 的残余职责）：

- `git -C <repo> status --porcelain` 必须为空（工作区干净，HEAD 即被测版本）；不干净 → 停。
- 记下 `git -C <repo> rev-parse --short HEAD` 作为基线 version。

## 目录契约

```text
REPO = <zany 仓库路径>
BENCHMARK_DIR = <REPO>/eval/benchmarks/<benchmark_id>
SCOREBOARD = <BENCHMARK_DIR>/scoreboard.yaml
```

```text
<benchmark_id>/
├── benchmark_config.toml      # title / description / runs = 1 / status = "draft"
├── scoreboard.yaml            # 初始化 evaluations: []
└── CASE-<nnn>-<slug>/
    ├── statement/README.md    # 公开：给被测 Target 的任务书（可带公开材料）
    └── rubric/
        ├── README.md          # 私有：评分项、分值、Gold
        └── metrics.yaml       # 可选：声明 scorer 与 deepeval 指标
```

rubric/README.md 顶部声明打分器三选一：`scorer: assert | rubric | deepeval:<metric>`（同一 case 恒定；deepeval 的 evaluation_steps 在本阶段生成并冻结进 metrics.yaml——评分标准看答案前定死）。`assert` 打分器意味着该 case 应进 eval/cases.jsonl 而不是本目录。

## 出题规则（纪律，不可裁剪）

- 每个 case 的 rubric **总分恒 100**，必须有有意义的部分分；分值大头压在"预期行为与强 agent 的合理捷径产生不同可打分产物"的点上；格式分/枚举分不得抬高分数地板。
- statement 呈现任务本身：目标、可用材料、输出格式、必要约束。不规定推理顺序、不指认决定性证据、不点破捷径、不泄露私有评分偏好。
- statement **不必唯一确定 Gold**：公开信息可不完整甚至冲突，rubric 编码一个私有判定标准（稳定的可复用 policy/优先级/推理边界），标准在看答案之前定死、之后永不改。禁止与能力无关的随机隐藏映射。
- 每个 case 建前私记：预期行为、强 agent 的合理捷径、难度、两种行为各自产生的不同可打分产物、为什么这个区分度量了目标能力。设计理由不得写进 statement。
- **防作弊（zany 无沙箱，重点）**：statement 与运行指令绝不给仓库路径、不给 eval/ 路径；被测 zany 有文件工具且路径不设 jail，能读仓库里的一切——防的是无意污染，靠泄漏检查 + Evaluator 查 Trace 兜底（见 agent-evaluation）。

每个新发/改动的 case 派发前做两件事（留在 Builder 分析里，不产出单独审计文件）：

1. **一致性评审**：statement 内部自洽（故意的冲突要表现为"来源间的冲突"）；rubric 与 statement 和定死的私有标准一致、自包含；每个评分项只依赖已定义/已提供/明确私有的前提。
2. **泄漏检查**：对照全部公开文件与私有 rubric，确认没有公开文件暴露 Gold、私有评分条件或指向预期解法的提示。

## 校准（Pilot → Freeze → Formal Baseline）

- **Pilot** = 每 case 恰好 1 run 的评测，用于修 benchmark，不改被测仓库。未被选中的 Pilot 结果不进 scoreboard，只保留一份临时的最低分完整有效修订副本（放 benchmarks/ 之外）。
- 第一轮 Pilot 是探索性探测：从分数和 session Trace 重建被测 agent 的实际解法（观察到的策略 → 缺失的行为 → 分离性预测），据此修难度后重跑所有受影响 case。区分**有效性修复**（任务/评分契约不可用，留在当前迭代）与**难度精修**（改变测量对象，受影响 case 重跑后算下一迭代）。
- 精修要制造"可打分的分离决策"：观察到的策略照走仍能拿分的加规则不算加难度。修 rubric 分值只按能力覆盖，不因被测漏了某句话而加分。
- 达到期望基线分且无已知设计缺陷 → 立即 Freeze（不要为留分数余量再修）。到迭代上限没达标 → 恢复并冻结**最低分**有效修订。发布门槛固定 85 分：冻结修订低于 85 也照样发布；只有"没有有效修订可冻结"或"最低有效修订仍 ≥85"（被测太强，benchmark 失去测量意义）才 `status = "failed"` 并报告 `calibration_failed`。没到期望分不是失败。
- **Formal Baseline** = 被选中 Pilot 的原结果直接抄录。禁止重跑、禁止 backfill 到多 run。记录前提：每 case 恰好一个有效 run、评测运行时一致、被测 version 未变（仍是开工时那个 commit）。

## 派发评测

逐格（case×run）调 `task` 工具，prompt = `eval/skills/agent-evaluation/SKILL.md` 全文 + 协议头：

```text
protocol_version: 1
case_id: <case_id>
run: <1_based_run_index>
expected_version: <git short hash>
repo_path: <REPO 绝对路径>
benchmark_dir: <BENCHMARK_DIR 绝对路径>
model: <冻结的 ANTHROPIC_MODEL 值>
```

先验格式再读字段：worker 回的必须是恰好一份纯协议 YAML，混入叙述/围栏/摘要即无效——让**同一个**子代理原样重发干净 YAML，不重跑被测、不自己从坏响应里抠。`evaluation_failed` 绝不记 0 分；重试仅当证据证明被测没启动过且本次带新的具体修复；不设数值重试上限。`version_changed` → 丢弃本轮 Pilot，等版本稳定重来。运行时不匹配（model 不符）→ 整个矩阵作废。

## 记账

时间用 `date -u +"%Y-%m-%dT%H:%M:%SZ"` 取，不从本地时间推。把选中的 Pilot 结果按设计文档 §3 的 scoreboard 结构追加进 `scoreboard.yaml`（version = git short hash，每 run 带 session_id），写完回读解析验证再报告成功。冻结后在 benchmark_config.toml 只改 `status = "published"` 一行并重新解析确认 TOML 有效；`calibration_failed` 同样只改一行成 `failed`，绝不把 failed 留在 draft。所有平均分由你算好写入（score 两位小数、duration 取整、cost 全 null 才写 null），存储值即权威，不建任何重算校验。

收尾删除临时副本等校准脚手架；保留冻结 benchmark、scoreboard、session Trace。不泄露 rubric/Gold/私有评分信息。基线记完即停，不改被测仓库、不开始优化。
