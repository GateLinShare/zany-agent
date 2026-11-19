"""edit 失配报错附"最接近原文行"自检: python3 tests/test_edit_nearest.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.tools import _nearest, builtin_tools  # noqa: E402

# 1. _nearest: 单行近失配 / 多行窗口 / 完全无关
text = "alpha\n| ④ 耗时 | 0.7s/张 | 126 张共 42.6s；摔倒事件 ≈1s |\ngamma\n"
hit = _nearest(text, "| ④ 耗时 | 0.7s/张 | 126 张共 42.6s；摔例事件 ≈1s |")
assert "摔倒" in hit and "相似度" in hit and "行 2" in hit, hit
assert _nearest(text, "| ④ 耗时 | 0.7s/张 | 126 张共 42.6s；摔倒事件 ≠1s\ngamma").startswith("\n最接近的原文[行 2-3"), _nearest(text, "| ④ 耗时 | 0.7s/张 | 126 张共 42.6s；摔倒事件 ≈1s\ngamma")
assert _nearest(text, "完全无关的另一段内容跟文件没有任何关系xyz") == ""

# 2. 端到端: edit_file 失配时错误里带最接近行, 命中时无提示
with tempfile.TemporaryDirectory() as d:
    tools = {t.name: t for t in builtin_tools(d)}
    edit = tools["edit_file"]
    Path(d, "r.md").write_text("# 标题\n\n| ④ 耗时 | 0.7s；摔倒事件 |", encoding="utf-8")
    out = edit.run({"path": "r.md", "old_string": "0.7s；摔例事件", "new_string": "x"})
    assert out.startswith("错误:") and "最接近的原文[行 3" in out and "摔倒" in out, out
    out2 = edit.run({"old_string": "摔倒事件", "new_string": "奔跑事件", "path": "r.md"})
    assert out2.startswith("已替换"), out2

print("ok")
