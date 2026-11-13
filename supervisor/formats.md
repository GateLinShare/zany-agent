# 进化流水线格式约定（单一事实源）

任务单、提案、审计三件事的格式都在这。改格式 = 同时改 `evolve.py` 的校验逻辑。

## 1. 任务单 `evolution/tasks/<id>.md`

id 格式 `t-YYYYMMDD-NN`（同日递增）。文件头 YAML，正文自由：

```markdown
---
id: t-20261120-01
status: open          # open | in-progress | gated | canary | done | failed | blocked
layer_hint: a         # 预估层级 a/b/c/d/e, 仅提示; 真身以门禁 diff 分层为准
created: 2026-11-20
origin: "用户说: web 检索能力还是弱"   # 来源, 保持原话以便回溯
---

## 目标（一句话）

## 方向
- …

## 验收
- 新增 case: <名字>, 基线必须 FAIL（先证明现在确实做不到）
- L1 现款全集不回归
```

规则：目标可证伪（能写出验收 case）；不允许在目标里写"改 XX 文件"（实现路径由 worker 探索决定）。

## 2. 部署提案 `evolution/reports/<id>.json`

```json
{
  "task_id": "t-20261120-01",
  "branch": "evolve/t-20261120-01",
  "candidate_sha": "<已推入 stable 的 40 位 sha>",
  "base_sha": "<开工时 stable master sha>",
  "risk_layer": "a|b|c|d|e",
  "motivation": "为什么这个改法能过验收（假设 + 证据一句话）",
  "report": {
    "l1": {"passed": 8, "total": 8, "note": "eval/main.py 现款全集"},
    "l2": {"score": 2, "baseline": 0, "cases": [
      {"case": "<case名>", "baseline_pass": false, "candidate_pass": true}
    ]}
  },
  "sessions": ["<worker 会话 id>", "..."]
}
```

门禁（evolve.py gate）检查：
1. 字段齐全 + L1 全过 + L2 score ≥ baseline + sessions 非空
2. candidate_sha 在 stable 仓、diff 非空、行数 ≤ policy.json `diff_max_lines`(800)
3. 分层按 diff 逐文件判定（未知路径按 c 层），d 层需 `--ack-d`、e 层需 `--ack-e` 且 diff 无密钥明文
4. sessions 里的 Trace 扫描：read_file 不得触碰 eval/benchmarks、rubric、eval/main.py、scoreboard（污染即拒）
5. 对应任务单存在

## 3. 分级与观察期（policy.json）

| 层 | 覆盖 | 观察期 |
|---|---|---|
| a | skills/、styles/、eval/cases.jsonl、eval/benchmarks/ | 6h |
| b | core/plugins/ | 6h |
| c | core/、pyproject.toml、uv.lock、未收录路径 | 24h |
| d | gateway/、docker/、eval/main.py、eval/deepeval/ | 24h + 人工专属验证 |
| e | .env、sessions/ | 6h + 人工过目 + 密钥掩码扫描 |

## 4. 稳定 tag

晋升时 supervisor 在 stable 仓打 `stable/YYYYMMDD-HHMM-<sha7>`，是唯一回滚锚点（hook 保护不可删改）。
