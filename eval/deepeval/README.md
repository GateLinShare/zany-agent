# eval/deepeval — LLM-as-judge 回答质量评测

用 deepeval(GEval) 给 zany-agent 的**回答质量**打分：开放性任务（总结全不全、诊断对不对、解释清不清楚）
没法穷举关键词走 `eval/main.py` 的确定性断言，用 LLM-as-judge 出 0~1 分 + 理由。
能用确定性断言的继续走 `eval/main.py`，别为省事全换 judge（judge 抽风引入 flaky，还多烧模型调用）。

## 怎么跑

```bash
cd /data01/tsn/code/tsn_files/zany-agent && \
uv run --offline --with deepeval python eval/deepeval/run_eval.py
```

- 评其他 checkout：前面加 `ZANY_ROOT=/data01/tsn/code/tsn_files/zany-agent-develop`
- deepeval 不进项目依赖：4.3.3 走 uv `--with` 临时叠加（66 个包，首次装 ~1 分钟，之后 `--offline` 走缓存）
- 全程串行，2 用例约 80 秒 = 2 次 agent 运行 + 4 次 judge 调用（GEval 每指标 2 次调模型：生成评测步骤 + 打分）
- judge 就是项目自己的 GLM 封装（`core.model.ChatModel`），零新增 API 配置
- 退出码对齐 eval/main.py：有 FAIL 即非 0

## 加用例

往 `run_eval.py` 的 `CASES` 里加 dict（name / seed / query / criteria / context_files / threshold）。
criteria 是判分核心：写清"什么算好"（覆盖哪些要点、是否允许推断、什么算捏造），judge 按它生成评测步骤。

## 硬约定（都是踩过的坑）

1. deepeval 不执行被测系统，agent 循环自己搭：`build_agent(workdir, with_mcp=False)` → `invoke` → `text_of`，再装 `LLMTestCase(input, actual_output, context=[工作区文件内容])`
2. 自定义 judge 带 schema 时**直接返回 schema 实例**（deepeval 按 isinstance 走实例分支）；别抄 OllamaModel 的 `(实例, cost)` 元组约定，会炸
3. 指标逐 case 绑定：`metric.measure(test_case)`；`evaluate()` 是 metrics×cases 笛卡尔积，不同类型 case 共评会错配拉脏平均分
4. query 里引用文件用相对名："读 notes.md"，别写 "/notes.md"（临时工作区下绝对引用会让 agent 按字面找文件而失败）
5. GEval score 由 judge 1~10 归一化到 0~1，threshold=0.7 起步；`strict_mode` 时 threshold 恒为 1
6. 遥测已关（`DEEPEVAL_TELEMETRY_OPT_OUT=1`）；评测记录落 `~/.deepeval` 本地，不推 Confident AI 云

## 和 eval/skill-loop 的关系

本目录的 judge 接法（ZanyJudge + schema 约定）是 skill-loop 闭环评测里 `deepeval` 打分器的公共依赖：
skill-loop 的 Evaluator 按 case rubric/metrics.yaml 声明调用本目录脚本给单 case 打分（scorer 入口接线见
`docs/self-improvement.md` §3.1，待实施）。经验与踩坑记录的全量版本在全局技能
`~/.zany/skills/agent-quality-eval/SKILL.md`（含 SWE-bench harness 现款）。
