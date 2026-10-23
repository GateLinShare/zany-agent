#!/usr/bin/env python3
"""评测 loop: JSONL 用例 → 临时目录隔离跑 agent → 断言判分 → 汇总。

参考 pi(packages/evals)和 deepagents(libs/evals)的最小子集:
用例格式见 evals/cases.jsonl, 每行:
    {"name": "...", "seed": {"/foo.md": "内容"}, "query": "...",
     "checks": {"answer_contains": ["三"], "file_contains": {"/out.txt": ["ok"]},
                "file_exists": ["/out.txt"], "max_steps": 6}}

    uv run eval.py                    # 跑全部
    uv run eval.py smoke              # 只跑 name 含 "smoke" 的
"""

import json
import re
import sys
import tempfile
from pathlib import Path

from agent import build_agent, text_of

EVALS_FILE = Path(__file__).resolve().parent / "evals" / "cases.jsonl"


def load_cases(pattern: str) -> list[dict]:
    cases = [json.loads(l) for l in EVALS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [c for c in cases if pattern in c.get("name", "")] if pattern else cases


def run_checks(case: dict, workdir: Path, answer: str, n_tool_calls: int) -> list[str]:
    """返回失败项列表, 空 = 全过。"""
    fails = []
    for s in case.get("checks", {}).get("answer_contains", []):
        if s.lower() not in answer.lower():
            fails.append(f"answer_contains:{s!r}")
    for path, needles in case.get("checks", {}).get("file_contains", {}).items():
        f = workdir / path.lstrip("/")
        if not f.exists():
            fails.append(f"file_contains:{path} (文件不存在)")
        elif not all(n in f.read_text(encoding="utf-8") for n in needles):
            fails.append(f"file_contains:{path} {needles!r}")
    for path in case.get("checks", {}).get("file_exists", []):
        if not (workdir / path.lstrip("/")).exists():
            fails.append(f"file_exists:{path}")
    limit = case.get("checks", {}).get("max_steps")
    if limit and n_tool_calls > limit:
        fails.append(f"max_steps:{n_tool_calls}>{limit}")
    return fails


def main() -> None:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = load_cases(pattern)
    if not cases:
        raise SystemExit(f"没有匹配的用例: {pattern!r}")
    passed = 0
    for c in cases:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            for path, content in c.get("seed", {}).items():
                f = workdir / path.lstrip("/")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content, encoding="utf-8")
            agent = build_agent(str(workdir))
            try:
                msgs = [{"role": "user", "content": c["query"]}]
                agent.invoke(msgs)
                answer = text_of(msgs[-1])
                n_tools = sum(
                    1 for m in msgs if m["role"] == "assistant" and isinstance(m["content"], list)
                    for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"
                )
                fails = run_checks(c, workdir, answer, n_tools)
            except Exception as e:
                answer, fails = "", [f"exception:{e}"]
        ok = not fails
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']}")
        if not ok:
            for f_ in fails:
                print(f"      ✗ {f_}")
            print(f"      answer: {answer[:200]!r}")
    print(f"\n{passed}/{len(cases)} 通过")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
