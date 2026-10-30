"""skills:扫描目录下 */SKILL.md,把 name/description 清单注入 system prompt。

SKILL.md 正文不进上下文 —— 模型按需用 read_file 读,这是渐进披露:
清单常驻(每条几十 token),全文只有真用到才占窗口。
frontmatter 用行解析,不引 yaml 依赖(只认 name/description 两个键,够用)。
默认扫项目 skills/、.claude/skills 和全局 ~/.miniagent/skills(跨项目共用);
~ 开头的 source 按真实路径展开,清单里也列绝对路径(read_file 字面直用),
其余 source 沿旧约定挂 workdir。
"""

from pathlib import Path


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        key, _, val = line.partition(":")
        if key.strip() in ("name", "description"):
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _root(src: str, workdir: str) -> Path:
    """source → 目录:~ 开头按绝对路径展开;其余(含 "/skills" 这种写法)挂 workdir —— 既有约定。"""
    if src.startswith("~"):
        return Path(src).expanduser()
    return Path(workdir) / src.lstrip("/")


def skill_section(workdir: str, sources: tuple = ("/skills", "/.claude/skills", "~/.miniagent/skills")) -> str:
    """返回 system prompt 的 skills 段;没有 skill 目录就返回空串。"""
    items = []
    for src in sources:
        root = _root(src, workdir)
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            fm = _frontmatter(skill_md_read(skill_md))
            if fm.get("name"):
                items.append(f"- {fm['name']}: {fm.get('description', '')}(全文: {skill_md_path(src, skill_md)})")
    if not items:
        return ""
    return (
        "\n\n# 可用 skills\n"
        "按需用 read_file 读对应 SKILL.md 全文再执行。\n"
        "使用中发现技能过时/有错: 直接用 edit_file/write_file 修对应 SKILL.md, "
        "不要等用户开口(项目技能在 git 里可回滚)。\n"
        + "\n".join(items)
    )


def skill_md_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def skill_md_path(src: str, p: Path) -> str:
    """read_file 可用的路径:~ 开头的 source 用真实绝对路径, 其余相对 workdir。"""
    if src.startswith("~"):
        return str(p)
    return f"{src}/{p.parent.name}/SKILL.md"
