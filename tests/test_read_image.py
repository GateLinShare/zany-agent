"""read_file 图片分支: 任意路径图片转 image block 回给模型。

跑法: uv run pytest tests/test_read_image.py
"""

import base64

from core.tools import IMAGE_MAX, _Ledger, _read

# 1x1 红色像素 PNG(合法最小图, 硬编码免依赖)
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc00000030101"
    "00cf9ff10a0000000049454e44ae426082"
)


def test_image_returns_block_array(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(PNG_1PX)
    out = _read(str(tmp_path), {"path": str(p)}, _Ledger())
    assert isinstance(out, list)
    img = out[0]
    assert img["type"] == "image"
    assert img["source"]["media_type"] == "image/png"
    assert base64.b64decode(img["source"]["data"]) == PNG_1PX
    assert str(p) in out[1]["text"]  # text 说明块(UI 显示用)


def test_jpg_media_type(tmp_path):
    p = tmp_path / "photo.JPG"  # 大写扩展名也命中
    p.write_bytes(b"\xff\xd8\xff\xe0fake")
    out = _read(str(p), {"path": str(p)}, _Ledger())
    assert out[0]["source"]["media_type"] == "image/jpeg"


def test_missing_image_is_error_text(tmp_path):
    out = _read(str(tmp_path), {"path": str(tmp_path / "no.png")}, _Ledger())
    assert isinstance(out, str) and out.startswith("错误:")


def test_oversize_image_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tools.IMAGE_MAX", 10)
    p = tmp_path / "big.png"
    p.write_bytes(PNG_1PX)  # 76 字节 > 10
    out = _read(str(tmp_path), {"path": str(p)}, _Ledger())
    assert isinstance(out, str) and "超过上限" in out


def test_text_file_unchanged(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    out = _read(str(tmp_path), {"path": str(p)}, _Ledger())
    assert isinstance(out, str) and "hello" in out


def test_unsupported_ext_still_text(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")
    out = _read(str(tmp_path), {"path": str(p)}, _Ledger())
    assert isinstance(out, str)  # 非图片扩展名不走图片分支


def test_image_max_constant_safe():
    # IMAGE_MAX 必须 < API 5MB 上限
    assert 0 < IMAGE_MAX < 5 * 1024 * 1024
