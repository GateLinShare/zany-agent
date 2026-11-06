"""skills 自检: python3 tests/test_skills.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.skills import _frontmatter, skill_section


def mk_skill(root: Path, name: str, desc: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: \"{desc}\"\n---\n正文", encoding="utf-8")
    return d / "SKILL.md"


def main():
    # 1. frontmatter: 认 name/description, 剥引号; 无 frontmatter 返回空
    fm = _frontmatter('---\nname: foo\ndescription: "bar baz"\n---\nbody')
    assert fm == {"name": "foo", "description": "bar baz"}, fm
    assert _frontmatter("no frontmatter") == {}

    tmp = Path(tempfile.mkdtemp())

    # 2. 项目目录: 清单列出, 路径相对 workdir(与 read_file 约定一致)
    md = mk_skill(tmp / "skills", "alpha", "项目技能")
    sec = skill_section(str(tmp), sources=("/skills",))
    assert "- alpha: 项目技能(全文: skills/alpha/SKILL.md)" in sec, sec

    # 3. 全局目录(~ 展开): HOME 指到沙箱, 清单列真实绝对路径
    home = tmp / "home"
    mk_skill(home / ".zany" / "skills", "beta", "全局技能")
    old = os.environ["HOME"]
    os.environ["HOME"] = str(home)
    try:
        sec = skill_section(str(tmp), sources=("~/.zany/skills",))
    finally:
        os.environ["HOME"] = old
    assert f"- beta: 全局技能(全文: {home}/.zany/skills/beta/SKILL.md)" in sec, sec

    # 4. 默认 sources 含全局目录; 两处同扫不串位
    assert skill_section.__defaults__[0][-1] == "~/.zany/skills"
    md.write_text("---\nname: alpha2\ndescription: x\n---\n", encoding="utf-8")
    sec = skill_section(str(tmp))  # 默认 sources: 项目 skills/ 命中, 全局 HOME 未配静默跳过
    assert "alpha2" in sec and "beta" not in sec, sec

    # 5. 段头带"发现问题→直接改"的自主引导(用户定的, git 兑底可回滚)
    assert "直接用 edit_file/write_file" in sec and "不要等用户开口" in sec, sec

    # 6. 一个目录都没有 → 空串(不产生空标题)
    assert skill_section(str(tmp / "nowhere"), sources=("/skills",)) == ""

    print("test_skills: all passed")


if __name__ == "__main__":
    main()
