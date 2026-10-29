"""_resolve 路径解析自检: python3 tests/test_resolve.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tools import _resolve


def main():
    wd = Path(tempfile.mkdtemp())
    (wd / "agent.py").write_text("x")

    # 1. 虚拟根: /agent.py -> workdir/agent.py
    assert _resolve(str(wd), "/agent.py") == (wd / "agent.py").resolve()

    # 2. ../ 越界仍拒绝(即使目标真实存在)
    try:
        _resolve(str(wd), "../whatever")
        raise AssertionError("../ 应被拒绝")
    except ValueError:
        pass

    # 3. 逃生口: 工作目录外既有文件的绝对路径直读
    outside = Path(tempfile.mkdtemp()) / "report.md"
    outside.write_text("hi")
    assert _resolve(str(wd), str(outside)) == outside.resolve()

    # 4. 哪都不存在 -> 带指引的报错
    try:
        _resolve(str(wd), "/no/such/file.md")
        raise AssertionError("不存在的路径应报 FileNotFoundError")
    except FileNotFoundError as e:
        assert "真实绝对路径" in str(e)

    print("ok: 4 checks passed")


if __name__ == "__main__":
    main()
