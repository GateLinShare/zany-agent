"""frozen 路径层: 源码运行锚仓库根, 冻结运行资源在 _MEIPASS、数据在 ~/.zany。"""
import sys
from pathlib import Path

import core.frozen as frozen

ROOT = Path(__file__).resolve().parent.parent


def test_source_mode_anchors_repo_root():
    assert frozen.app_root() == ROOT
    assert frozen.data_dir() == ROOT


def test_frozen_mode(monkeypatch):
    monkeypatch.setattr(frozen, "IS_FROZEN", True)
    monkeypatch.setattr(sys, "_MEIPASS", "/fake/meipass", raising=False)
    assert frozen.app_root() == Path("/fake/meipass")
    assert frozen.data_dir() == Path.home() / ".zany"
