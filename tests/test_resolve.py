"""_resolve 路径解析自检: python3 tests/test_resolve.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.tools import _Ledger, _resolve, _write  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    wd = Path(d)
    (wd / "a.py").write_text("x")

    # 1. 相对路径挂 workdir
    assert _resolve(str(wd), "a.py") == (wd / "a.py").resolve()
    assert _resolve(str(wd), "./a.py") == (wd / "a.py").resolve()

    # 2. 绝对路径字面直用, 存在与不存在都按真实路径解析(pi/hermes: 无虚拟根)
    assert _resolve(str(wd), str(wd / "a.py")) == (wd / "a.py").resolve()
    outside = wd.parent / "no_such_missing.md"
    assert _resolve(str(wd), str(outside)) == outside.resolve()

    # 3. ~ 展开, 不落进 workdir
    assert not str(_resolve(str(wd), "~")).startswith(str(wd))

    # 4. write_file 可用绝对路径在工作目录外新建文件(旧虚拟根禁止的场景)
    new = wd.parent / "resolve_test_new.md"
    _write(str(wd), {"path": str(new), "content": "hi"}, _Ledger())
    assert new.read_text() == "hi"
    new.unlink()

print("ok")
