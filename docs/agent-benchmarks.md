# Agent 评测方式与评测集调研

> 2026-11 联网调研（官网/arXiv 实抓 + 交叉确认），三个子任务分别覆盖代码工程、通用助手/工具调用、环境交互三方向。
> 定位：选型参考，不含本仓库实现。

## 1. 总格局

评测分三个家族 + 一个综合基础设施，判分方式是分水岭：
**头部评测清一色可执行/规则验证，不用 LLM judge**（跑回归测试、比数据库终态、精确匹配、校验脚本）；
LLM judge 只用于轨迹质量的附加分析。

| 家族 | 代表 |
|---|---|
| 代码/软件工程 | SWE-bench Verified、Terminal-Bench、SWE-Lancer、Multi-SWE-bench |
| 通用助手/工具调用 | GAIA、BFCL、τ-bench/τ²、BrowseComp |
| 环境交互/长任务 | OSWorld-Verified、TheAgentCompany、WebArena、ARC-AGI-3 |
| 评测基础设施 | HAL（不是评测集，是标准化 harness + 榜单） |

## 2. 代码/软件工程类

### SWE-bench Verified — 第一权威
- 普林斯顿 NLP，Verified 子集与 OpenAI Preparedness 合作人工校验（2024-08）。ICLR 2024 Oral。
- 任务：真 repo + GitHub issue → agent 产出 patch。
- 判分：Docker 容器内跑仓库真实测试，FAIL_TO_PASS 全转绿且 PASS_TO_PASS 无回归才算 resolved。
- 几乎所有前沿实验室 model card 标配引用。入口 https://www.swebench.com/
- 榜单格局：Verified 头部 75-79%（live-SWE-agent + Claude 4.5 Opus ~79%、TRAE ~78.8%、Gemini 3 Pro ~77%）。Lite 已饱和水分多；Pro（2025，大型真实仓库）是抗饱和替代。

### Terminal-Bench — agent 时代事实标准
- Stanford + Laude Institute（Harbor 团队），持续演进（当前 4.1），https://terminal-bench.ai/
- 任务：Docker 容器内纯终端交互的真实系统任务（配置/调试/部署），agent 自由操作，无 patch 格式要求。
- 判分：每任务自带校验脚本/oracle 解，多 trial（k=5）取均值报 95% CI。
- OpenAI Codex、Claude Code、Grok、GLM、Qwen 发布材料都引用。头部：Claude Code + Opus 5.5 ~65.8%。

### SWE-Lancer — 经济价值向
- OpenAI Preparedness（arXiv:2502.12115）。Expensify 真实自由职业市场 198 题（IC SWE 写码 + SWE Manager 选方案），每题标真实悬赏（总池 $1M）。
- 判分：端到端测试通过 = 赚得该题金额，指标是赚到的钱。分量重但更新慢。https://openai.com/index/swe-lancer/

### Multi-SWE-bench — 多语言扩面
- 字节 Seed，NeurIPS 2025 D&B。SWE-bench 范式扩到 7 语言 1632 个真实 issue，判分继承 F2P/P2P。
- 数据/镜像全开源，国产模型常引，西方 model card 引用少。https://github.com/multi-swe-bench/multi-swe-bench

### Aider Polyglot — 裸模型参照系
- 225 道 Exercism 六语言练习题，无 agent 循环、无工具，测"模型按 diff 格式改代码"。
- 只适合看编辑格式遵循，已不适合代表 agent 工程能力。https://aider.chat/docs/leaderboards/

## 3. 通用助手/工具调用类

### GAIA — 综合助手必报口径
- Meta FAIR × HuggingFace（arXiv:2311.12983）。466 题（val 165/test 301，L1-L3 难度），带图文/音频附件的真实问题，需外部工具+浏览。
- 判分：**纯规则精确匹配**（官方 scorer.py 归一化后比对），无 LLM judge。
- OpenAI Deep Research、Anthropic、Magentic-One、Manus 都报过。测试集 gated 防污染。huggingface.co/gaia-benchmark
- 当前头部 ~76%（test），距人类 92% 仍有缺口；已趋饱和、区分度下降。

### BFCL — 工具调用事实标准
- Berkeley Gorilla 团队，https://gorilla.cs.berkeley.edu/leaderboard.html
- 函数 schema + 用户请求 → 调用准确性；v3 加多轮状态跟踪，v4 加 agentic 整体评测。
- 判分：AST 比对 + 可执行/模拟环境状态校验。开源模型技术报告普遍引用。头部 Claude Opus 4.5 (FC) ~77%。

### τ-bench / τ²-bench — 可靠性口径，上升最快
- Sierra Research（Shunyu Yao 等），github.com/sierra-research/tau2-bench + taubench.com。
- 工具-Agent-用户三方交互：agent 带领域 API 和 policy 文档，与 LLM 模拟用户多轮对话完成客服任务；τ² 加 telecom 域和"双控"（引导用户自己操作）。
- 判分：跑完比对**模拟数据库终态** vs 目标态 + 动作序列检查；指标 pass^k（k 次重复全部成功）。GPT-4.1/GPT-5、xAI/Anthropic 发布均引用。

### BrowseComp — 深度研究/浏览口径
- OpenAI（Jason Wei 等，arXiv:2504.12516）。1266 道难检索"纠缠信息"问题，短答案字符串比对，无 judge。
- Deep Research 类系统标配对照；无官方常驻榜，各实验室自报。代码在 github.com/openai/simple-evals。

### AgentBench — 历史权威
- 清华 THUDM，ICLR 2024，学术引用量最高的早期综合评测（8 个交互环境）。
- 环境基线停在 2023，前沿实验室新发布已少引用。

### AssistantBench — 中等采用度补充
- AI2 等（arXiv:2407.15711）。214 个真实 web 任务，规则化类型匹配判分（DROP 式 EM/F1）。
- 天然"谁都考不好"的难度定位（头部 <30%）。

## 4. 环境交互/长任务类

### OSWorld-Verified — computer use 事实标准
- HKU Tao Yu 组 + Salesforce。真实 Ubuntu 虚拟机 369+ 任务（浏览器/办公/系统设置），截图或 a11y 树输入。
- 判分：结果导向自动验证器（比 VM 最终状态/文件/设置）。OpenAI/Anthropic/Google 模型发布必引。
- 官方 Verified 榜头部 ~84-90%，已越过人类基线 ~72%。2026-06 出 2.0。https://os-world.github.io/

### TheAgentCompany — 长时程办公代表作
- CMU（WebArena 同源团队），ICML 2025，https://the-agent-company.com/
- 自托管"模拟公司"全套内部服务（GitLab/Plane/ownCloud/RocketChat），269 项长时程数字员工任务，平均几十步跨多服务。
- 判分：确定性验证器 + LLM evaluator 混合，结果导向为主。

### WebArena — 类别奠基，区分度已降
- CMU，NeurIPS 2024 Oral。自托管仿真站 + 程序化状态验证器。GPT-4 时代 ~14%，当前 CUA ~60%，团队主力已转向后续项目。

### ARC-AGI-3 — 泛化最硬指标
- ARC Prize Foundation（非营利，向 NIST CAISI 提供基准），https://arcprize.org/leaderboard
- ARC-AGI-1/2 已被头部基本攻破；ARC-AGI-3（2025-11 开放）是未知规则交互游戏，须在线探索学习，全自动精确判分，半私有测试集防污染，目前基本未被攻破（多数前沿模型 <5%）。

### METR — 长任务能力指标
- 前沿模型风险评估非营利机构。RE-Bench：8 小时真实 ML 研发任务，程序化评分 + 人工核查，与人类专家同题对比。
- time-horizon 指标（"50% 任务时长"，每约 7 个月翻倍）已成行业最常引用的长任务口径。不做公开榜，以研究报告发布。https://metr.org/

## 5. HAL — 评测基础设施
- Princeton NLP，arXiv 2510.11977，https://hal.princeton.edu/
- 不是单一评测集：标准化 harness 在数百台 VM 并行跑 agent，横跨 model × scaffold × benchmark（9×9），单次评测 $40k / 21,730 rollouts。
- 把**成本和方差**当一等公民指标；开放任务用 LLM judge，其余原生自动验证。
- 价值在横向发现：同一模型换 scaffold 分数差很大、成本差一个数量级。

## 6. 对本项目的启示

1. **权威口径 = 能机检就机检**：zany-agent 的 eval 体系若要对齐行业做法，开放任务方向是配可执行验证器（文件终态/脚本断言），LLM judge（现有 deepeval/GEval 路线，见全局技能 agent-quality-eval）只做质量抽检和轨迹分析兜底。
2. 可落地性最好的两个：**Terminal-Bench**（Docker + 终端任务正好对上 zany 的工具面，判分现成）和 **GAIA validation**（165 题、纯规则判分、公开榜可提交）。
3. 本仓库 SWE-bench harness 已通（见 ~/.zany/skills/agent-quality-eval/SKILL.md，astropy gold 4/4），差真实 predictions 跑分。
