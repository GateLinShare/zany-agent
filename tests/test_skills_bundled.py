"""内置 skills(app_root()/skills) 应进清单且全文路径为绝对路径(read_file 可直用)。"""
from pathlib import Path

import core.skills as skills


def test_bundled_skills_listed_with_abs_path(tmp_path, monkeypatch):
    bundled = tmp_path / "skills" / "demo"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("---\nname: demo\ndescription: 测试技能\n---\n正文")
    monkeypatch.setattr(skills, "app_root", lambda: tmp_path)
    section = skills.skill_section(str(tmp_path / "nowhere"), sources=())
    assert "- demo: 测试技能" in section
    assert str(bundled / "SKILL.md") in section  # 全文路径必须 read_file 可直读(绝对)


def test_bundled_not_found_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "app_root", lambda: tmp_path / "nope")
    assert skills.skill_section(str(tmp_path), sources=()) == ""
