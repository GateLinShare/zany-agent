#!/usr/bin/env python3
"""deepeval 评 zany-agent 回答质量: 跑 agent → LLMTestCase → GEval(项目自带 GLM 当 judge) → score+reason。

收编自全局技能 agent-quality-eval 的 deepeval_demo.py(脚本即评测资产, 进项目 git)。
用法(仓库根执行):
    uv run --offline --with deepeval python eval/deepeval/run_eval.py
评其他 checkout: 前面加 ZANY_ROOT=/data01/tsn/code/tsn_files/zany-agent-develop
加用例: 往 CASES 里加 dict(name / seed / query / criteria / context_files / threshold)。
"""

import os
import re
import sys
import tempfile
from pathlib import Path

# 评哪个 checkout: ZANY_ROOT 可覆盖, 默认本脚本所在仓库根
ROOT = Path(os.environ.get("ZANY_ROOT", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
os.environ.setdefault("DEEPEVAL_UPDATE_WARNING_OPT_IN", "0")
os.environ.setdefault("OPENAI_API_KEY", "unused")  # judge 全走自定义模型, 别让 openai client 报缺 key

from core.agent import build_agent, text_of  # noqa: E402
from core.model import ChatModel, make_client  # noqa: E402

import json  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from deepeval.metrics import GEval  # noqa: E402
from deepeval.models import DeepEvalBaseLLM  # noqa: E402
from deepeval.test_case import LLMTestCase, SingleTurnParams  # noqa: E402


# ---------- judge: 复用 agent 自己的模型封装(anthropic 兼容 GLM 代理), 不经 openai ----------
class ZanyJudge(DeepEvalBaseLLM):
    def __init__(self):
        self.chat = ChatModel(os.environ["ANTHROPIC_MODEL"], client=make_client())

    def load_model(self):
        return self.chat

    def get_model_name(self):
        return f"zany-judge:{os.environ['ANTHROPIC_MODEL']}"

    def generate(self, prompt: str, schema: type[BaseModel] | None = None):
        """deepeval 约定(非原生模型): 带 schema 时直接返回 schema 实例, 否则返回 str。"""
        last_err = None
        for attempt in range(3):
            suffix = ""
            if schema is not None:
                js = json.dumps(schema.model_json_schema(), ensure_ascii=False)
                suffix = (f"\n\n只输出一个符合以下 JSON Schema 的 JSON 对象, "
                          f"不要 markdown 代码块, 不要解释:\n{js}")
            text = self.chat.complete("你是评测裁判, 严格按指令输出。", prompt + suffix)
            if schema is None:
                return text
            m = re.search(r"\{.*\}", text, re.S)  # 剥掉可能的围栏/前后杂文本
            try:
                return schema.model_validate_json(m.group(0) if m else text)
            except Exception as e:
                last_err = e
                prompt = f"你上次的输出无法按 schema 解析({e})。重新回答, 只输出 JSON。原指令:\n\n{prompt}"
        raise ValueError(f"judge 连续 3 次未按 schema 输出: {last_err}")

    async def a_generate(self, prompt: str, schema=None):
        return self.generate(prompt, schema)  # 我们全程串行, async 落回同步


# ---------- 跑 agent: 和 eval/main.py 同款流程 ----------
def run_agent(case: dict, workdir: Path) -> str:
    for path, content in case["seed"].items():
        f = workdir / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    agent = build_agent(str(workdir), with_mcp=False)  # 评测不载 MCP, 省探测时间
    msgs = [{"role": "user", "content": case["query"]}]
    agent.invoke(msgs)
    return text_of(msgs[-1])


CASES = [
    {
        "name": "总结质量(开放性, 确定性断言覆盖不了)",
        "seed": {"/notes.md": (
            "周三评审会纪要\n"
            "- 网关超时阈值从 30s 降到 10s, 老王负责, 下周三前完成\n"
            "- 日志采样率提到 100%, 先在网关和订单两个服务灰度\n"
            "- 上季度预算超支 20%, 砍掉实验性 RAG 项目\n"
            "- 新人张倩下周入职, 老王带\n"
            "- 客户 A 工单积压 43 个, 本周五前清零\n")},
        # query 引用文件用相对名: 工作区是临时目录, "/notes.md" 绝对引用有概率让 agent 按字面找而失败
        "query": "读 notes.md(工作目录下), 用不超过三句话总结里面的关键待办, 只输出总结。",
        "criteria": "总结是否忠实覆盖原文的关键待办及其负责人/时限/数字, 是否有捏造原文没有的内容",
        "context_files": ["/notes.md"],
        "threshold": 0.7,
    },
    {
        "name": "诊断质量(答案不能靠关键词断言穷举)",
        "seed": {"/bug.log": (
            "10:00:01 INFO  api started, db pool max=5\n"
            "10:00:05 INFO  GET /orders 200 (12ms)\n"
            "10:00:12 WARN  db pool exhausted, waiting for connection\n"
            "10:00:13 WARN  db pool exhausted, waiting for connection\n"
            "10:00:15 ERROR TimeoutError: connection acquire timed out after 30s\n"
            "10:00:15 ERROR unhandled exception in worker thread -> process exit\n"
            "10:00:16 INFO  supervisor restarting api (restart #7 today)\n")},
        "query": "读 bug.log(工作目录下), 找出服务反复崩溃的根因, 一句话说明。",
        "criteria": "结论是否指出日志中的真实根因(数据库连接池 max=5 耗尽导致获取连接超时、进程退出), "
                    "而不是复述表面现象(如只说'进程崩了'或'超时')",
        "context_files": ["/bug.log"],
        "threshold": 0.7,
    },
]


def main():
    failed = 0
    for case in CASES:
        with tempfile.TemporaryDirectory() as td:
            print(f"\n=== 跑 agent: {case['name']} ===")
            answer = run_agent(case, Path(td))
            print(f"agent 回答: {answer!r}")
            ctx = [(Path(td) / p.lstrip("/")).read_text(encoding="utf-8")
                   for p in case["context_files"]]
        tc = LLMTestCase(input=case["query"], actual_output=answer, context=ctx)
        metric = GEval(
            name=case["name"],
            criteria=case["criteria"],
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT,
                               SingleTurnParams.CONTEXT],
            model=ZanyJudge(),
            threshold=case["threshold"],
            async_mode=False,
        )
        # 指标逐 case 绑定(evaluate() 是 metrics×cases 笛卡尔积, 会错配拉脏平均分)
        metric.measure(tc, _show_indicator=False)
        ok = metric.is_successful()
        failed += not ok
        print(f"\n[{metric.name}] score={metric.score:.2f} "
              f"{'PASS' if ok else 'FAIL'} (threshold={metric.threshold})")
        print(f"  reason: {metric.reason.strip()[:400]}")
    sys.exit(1 if failed else 0)  # 退出码对齐 eval/main.py: 有 FAIL 即非 0


if __name__ == "__main__":
    main()
