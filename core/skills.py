"""skills:扫描目录下 */SKILL.md,把 name/description 清单注入 system prompt。

SKILL.md 正文不进上下文 —— 模型按需用 read_file 读,这是渐进披露:
清单常驻(每条几十 token),全文只有真用到才占窗口。
frontmatter 用行解析,不引 yaml 依赖(只认 name/description 两个键,够用)。
默认扫项目 skills/、.claude/skills、.agents/skills 和全局 ~/.zany/skills(跨项目共用);
同名技能先到先得去重(项目 skills/ > .claude > .agents > 全局)。
~ 开头的 source 按真实路径展开,清单里也列绝对路径(read_file 字面直用),
其余 source 沿旧约定挂 workdir。
"""

from pathlib import Path

from core.frozen import app_root


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
    """source → 目录:~ 与绝对路径按字面展开(内置技能/全局共享池), 其余挂 workdir —— 既有约定。"""
    if src.startswith("~"):
        return Path(src).expanduser()
    if Path(src).is_absolute():
        return Path(src)
    return Path(workdir) / src.lstrip("/")


def skill_section(
    workdir: str,
    sources: tuple = ("skills", ".claude/skills", ".agents/skills", "~/.zany/skills"),
) -> str:
    """返回 system prompt 的 skills 段;没有 skill 目录就返回空串。同名技能先到先得。"""
    bundled = app_root() / "skills"
    if bundled.is_dir() and str(bundled) not in sources:
        sources = (*sources, str(bundled))  # 内置技能排最后: 项目/全局同名先到先得
    items, seen = [], set()
    for src in sources:
        root = _root(src, workdir)
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            fm = _frontmatter(skill_md_read(skill_md))
            name = fm.get("name")
            if not name or name in seen:  # 同名去重: 项目技能优先于全局
                continue
            seen.add(name)
            items.append(f"- {name}: {fm.get('description', '')}(全文: {skill_md_path(src, skill_md)})")
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
    """read_file 可用的路径: 非项目内挂载(~、绝对路径)用真实绝对路径, 其余相对 workdir ——
    不能带前导斜杠: 工具层约定绝对路径按真实文件系统字面解析, 带斜杠必 404。"""
    if src.startswith("~") or Path(src).is_absolute():
        return str(p)
    return f"{src.lstrip('/')}/{p.parent.name}/SKILL.md"
