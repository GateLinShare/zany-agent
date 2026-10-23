"""skills:扫描目录下 */SKILL.md,把 name/description 清单注入 system prompt。

SKILL.md 正文不进上下文 —— 模型按需用 read_file 读,这是渐进披露:
清单常驻(每条几十 token),全文只有真用到才占窗口。
frontmatter 用行解析,不引 yaml 依赖(只认 name/description 两个键,够用)。
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


def skill_section(workdir: str, sources: tuple = ("/skills", "/.claude/skills")) -> str:
    """返回 system prompt 的 skills 段;没有 skill 目录就返回空串。"""
    items = []
    for src in sources:
        root = Path(workdir) / src.lstrip("/")
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            fm = _frontmatter(skill_md_read(skill_md))
            if fm.get("name"):
                items.append(f"- {fm['name']}: {fm.get('description', '')}(全文: {skill_md_path(src, skill_md)})")
    if not items:
        return ""
    return "\n\n# 可用 skills\n按需用 read_file 读对应 SKILL.md 全文再执行:\n" + "\n".join(items)


def skill_md_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def skill_md_path(src: str, p: Path) -> str:
    """backend 视角的路径(虚拟根), 与 read_file 的约定一致。"""
    return f"{src}/{p.parent.name}/SKILL.md"
