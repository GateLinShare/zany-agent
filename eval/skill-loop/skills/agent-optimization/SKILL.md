---
name: agent-optimization
description: 以冻结 benchmark 的分数账本为反馈，用可证伪假设驱动改整个 zany 仓库（代码/prompt/skills），分数严格提升才 commit。Optimizer 会话载入本文件执行。
---

# Agent Optimization（zany 版）

用"证据 → 假设 → Candidate → 评测 → 接受或回滚"循环改进被测 zany。公开 statement、scoreboard、score 关联的 session Trace 是黑盒反馈；所有评测经 `task` 工具派发（把 `eval/skills/agent-evaluation/SKILL.md` 全文拼进 prompt），绝不亲自跑或打分被测。

## 输入

缺以下任一项先向用户要：目标分、`runs` 数（正整数，冻结整个会话）、轮次上限。齐了就开工。

前置校验，任一不满足就停并说明（不代建缺失物）：

- benchmark 存在且 `status = "published"`（draft 未冻结、failed 未完成校准，都停）。
- scoreboard 里有覆盖全部冻结 case 的完整 Formal Baseline，且其 version 与当前 `git -C <repo> rev-parse --short HEAD` 一致；运行时（model）从该基线记录继承，不要求用户重复给。
- **工作区干净**（`git status --porcelain` 为空）——Candidate = 工作区改动，Reference = HEAD，脏工作区没有对照基线。不干净 → 停。

## 访问边界

可读：公开 statement、scoreboard、score 关联的 session Trace（含被拒 Candidate 的）、被测仓库源码。
**禁读**：rubric/README.md、metrics.yaml、Gold、私有评分条件、Evaluator 的 workspace。
私有评测信息一旦进入你的上下文 → 回滚当前 Candidate 并以"污染"状态停止。

**可改**：被测仓库的代码、SYSTEM_PROMPT、skills/、styles/——评测对象是整个 agent，都可动。
**禁改**：`eval/` 下的一切（benchmark 与判分件，改了就是自己给自己放水）、`.env` 的密钥、scoreboard（唯一例外：追加被接受的评测记录）。

## 每轮八步

1. **确立 Reference**：当前保持为最优的版本（git commit）+ 它在冻结 benchmark 上的完整评测。校验覆盖全部冻结 case、运行时一致。
2. **诊断能力缺口**：对比各 case 的 per-case 分（顶层平均只用于整版本比较），用公开 statement + session_id 读被测 Trace + 历史被拒尝试，找"可通过通用改动改善的可观察行为"；多 run 可区分稳定行为与随机波动。
3. **可证伪假设**：连接缺口与一个有界 Candidate，写明哪些可观察决策/产物应当变化、为什么。只加分析步骤不预测行为变化的改动不算假设。当前诊断穷尽时，用剩余公开证据与历史尝试构造不同的可行 Candidate。
4. **从 Reference 构造一个 Candidate**：直接在工作区改。开工前确认工作区干净；记录本次新建的文件清单（回滚用）。**版本号只增**：Candidate 版本 = Reference commit 的新 hash，被拒改动不留版本、hash 天然不复用。
5. **admissibility 检查**：改动是通用的（不 case 特判硬编码——benchmark 的 statement 原文也出现在 eval/ 目录里，别把答案写进 prompt）、未掺私有评测信息、只动了允许范围。发现私有信息进上下文 → 回滚并停（contaminated）。
6. **先跑 L1 全回归**：`uv run eval/main.py`，任一失败即拒绝 Candidate（守门员一票否决）。过了再逐格派 `task` 跑完整冻结矩阵（prompt = agent-evaluation SKILL 全文 + 协议头，`expected_version` = Candidate 的新 commit 或 `<hash>+dirty`——工作区改动未提交时是 dirty，接受 commit 后才成为干净版本）。任何格子在飞期间不许改 Candidate。矩阵执行是串行的（单 worker 队列），多格可一次 task 多调用发出但会排队，耐心等。
7. **裁决**：全部格子有效**且** Candidate 顶层平均分**严格高于** Reference 才接受——分数提升即接受，假设是否被验证单独记录，不混为一谈。接受 → `git add -A && git commit`（信息写假设与结果摘要）；拒绝 → `git checkout -- .` 恢复 + 按第 4 步的新建文件清单删除 Candidate 产物并验证工作区回到干净。发现别的进程改了工作区 → 停，不覆盖。
8. **持久化**：接受的评测立即追加进 scoreboard（version = 新 commit hash）并回读校验（含 version 与各 session_id）才开下一轮。达标提前停；轮尽保留最高分 Reference。被拒 Candidate 的评测不进 scoreboard，只作下一轮假设的证据。

一轮只有在某个 Candidate 拿到完整有效评测后才计轮；修正类重试/有效性修复不耗轮次，**被拒 Candidate 的完整有效评测算一次**。

## 记账与报告

时间 `date -u` 取。scoreboard 结构与冻结纪律同 benchmark-design（score 两位小数、duration 取整、cost 全 null 才写 null、存储值即权威不重算）。追加后回读验证再继续。

报告：每轮一行小结（分数、诊断的缺口、改动、接受/回滚），最终给出最高分 Reference 的 commit hash 与 scoreboard 路径。不泄露 rubric/Gold/私有评分信息。
