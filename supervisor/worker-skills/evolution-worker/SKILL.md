---
name: evolution-worker
description: 进化 worker 流程（T2 探索 + T3 变异 + 提案产出）。spawn-worker 拉起的 worker 会话载入本文件执行，直到产出部署提案。
---

# Evolution Worker（evolve worktree 内执行）

你在一个独立 evolve worktree 里。被改被测代码 = 当前工作区；**禁止读写生产部署目录**（primary/standby 的代码）。所有评测经 `task` 工具派发，绝不亲自跑或打分被测。

## 开工校验（任一不满足即停，输出 blocked + 原因）

- 任务单存在且 `status` 非 blocked。
- `git status --porcelain` 干净（脏就停，不代清理）。
- 记下基线：`git rev-parse HEAD`（= stable master）。

## 第一步：探索（对齐任务单的目标）

- 读任务单方向，盘点现状：相关模块、已有能力、为什么现在做不到。
- 撞墙测试：手工构造最小输入走一遍现有链路，确认缺口真实存在（拿 execute 试）。
- 产出验收 case：按 `eval/cases.jsonl` 现有格式写新 case，**先跑一遍证明基线 FAIL**（`python eval/main.py <case>`）。基线就过的新 case 无意义，删掉重想。
- 把探索结论和新 case 写回任务单正文（追加"探索记录"节）。

## 第二步：变异（可证伪假设驱动）

按 `eval/skill-loop/skills/agent-optimization/SKILL.md` 的纪律执行（八步循环、访问边界、禁改 eval/ 全部判分件），差异点：

- Reference = 基线 sha 的代码，Candidate = 工作区改动（commit 在本地分支 `evolve/<任务id>`）。
- 评测口径（M1 简化，benchmark 矩阵未建）：
  - **L1**：`python eval/main.py` 现款全集必须全过（回归线）。
  - **L2**：任务单新增 case，基线 0 个过 → 候选必须全过（`l2.baseline=0, l2.score=通过数`）。新 case 文件本身可以随候选一起提交（属 a 层）。
- 提交纪律：小步 commit、每个 commit 带假设编号（`H1: ...`）；发现假设证伪 → revert 该 commit 换假设，不恋战。

## 第三步：产出部署提案

全部绿了才做：

1. `git push stable evolve/<任务id>`（推到 stable 仓，fast-forward）。
2. 写提案 `evolution/reports/<任务id>.json`，格式严格按 `supervisor/formats.md` §2。`candidate_sha` = 候选分支 HEAD 全 sha；`sessions` 列出本任务所有 zany 会话 id（评测 session + 本会话）。
3. 任务单 `status: in-progress → gated`（等 supervisor gate）。

## 红线

- 禁改：`eval/` 判分件语义（新增 case 允许、改已有 case 不允许）、`.env`、`sessions/`、生产部署目录。
- 禁读：`eval/benchmarks/` 的 rubric/Gold、`eval/main.py` 判分逻辑（评测当黑盒）。
- 分数链不上 Trace 的评测 = 没做过。提案里每个分数都要能被 session Trace 复核。
- 卡住/缺资源 → 任务单 `status: blocked` + 写明缺什么，不硬编。
