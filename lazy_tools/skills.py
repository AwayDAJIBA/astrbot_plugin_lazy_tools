"""AstrBot ``## Skills`` 段的按需压缩 + 自定义技能简介。

AstrBot 每一轮都会把当前可用技能（名字 + 完整 description + ``SKILL.md``
绝对路径 + 七条英文规则）拼进 system prompt。内置技能多的时候，单这一段就
可能上千到几千字符，闲聊时全是白烧的 token。

这里把这段压成：

- ``技能名 + 一句简介 + SKILL.md 的相对路径``（同一技能根目录只写一次）；
- 原来的七条英文规则换成一段中文精简规则；
- 支持按技能名**自定义简介**（也可以隐藏某个技能），覆盖项存在
  ``overrides.json`` 的 ``skills`` 段里；
- 技能全文始终留在各自的 ``SKILL.md``，模型真正要用时才去读文件，
  即 progressive disclosure。

任何解析失败 / 结构不认识时都原样放行，绝不会把技能弄丢。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

SKILLS_HEADER = "## Skills"

# AstrBot 的 build_skills_prompt 会生成：
#   - **<name>**: <description>
#     File: `<abs path>`
_ITEM_RE = re.compile(
    r"-\s+\*\*(?P<name>[^*\n]+?)\*\*:\s*(?P<desc>.*?)\n\s*File:\s*`(?P<path>[^`]+)`",
    re.S,
)

_DEFAULT_RULES = (
    "使用规则：任务与简介匹配、或用户点名时才用；用前必须先读完该技能的 SKILL.md"
    "（用 astrbot_file_read_tool 或 shell 读上面给的绝对路径），不许照记忆瞎来；"
    "只打开 SKILL.md 直接引用的脚本/模板，别批量加载整个目录；"
    "多个技能都沾边时挑最小集合，并说一句用了哪个；技能确实用不了就说清原因、给替代方案。"
)

_HEADER_INTRO = "可用技能（只给名字和一句简介）。要真用哪一个，先读它的 SKILL.md 全文再照做。"


def clean_text(value: Any, limit: int = 500) -> str:
    """压成单行并截断，用于简介 / 覆盖项。"""
    if not isinstance(value, str):
        return ""
    text = " ".join(line.strip() for line in value.strip().splitlines() if line.strip())
    return text[:limit].strip()


def first_sentence(text: str, max_chars: int = 50) -> str:
    """取第一句并截断到 max_chars，保留可读性。"""
    desc = " ".join((text or "").split())
    if not desc:
        return "（无简介，读 SKILL.md 看详情）"
    match = re.search(r"[。；!?！？]|\.(?=\s)", desc)
    if match and match.start() >= 12:
        desc = desc[: match.start()]
    if max_chars and len(desc) > max_chars:
        desc = desc[:max_chars].rstrip(" ,，、;；:：") + "…"
    return desc


def split_root(path: str) -> tuple[str, str]:
    """把 ``<root>/<name>/SKILL.md`` 拆成 ``(root, "<name>/SKILL.md")``。

    拆不动就返回 ``("", 原路径)``。
    """
    raw = (path or "").replace("\\", "/").strip()
    parts = [segment for segment in raw.split("/") if segment]
    if len(parts) >= 3 and parts[-1].lower() == "skill.md":
        root = raw[: len(raw) - len("/".join(parts[-2:])) - 1]
        if root:
            return root, "/".join(parts[-2:])
    return "", raw


def locate_skills_block(system_prompt: str) -> tuple[int, int, str] | None:
    """返回 ``(start, end, block)``；没有 ``## Skills`` 段时返回 None。"""
    if not system_prompt:
        return None
    start = system_prompt.find(SKILLS_HEADER)
    if start < 0:
        return None
    tail = system_prompt[start + len(SKILLS_HEADER) :]
    nxt = re.search(r"\n## (?!Skills)", tail)
    end = start + len(SKILLS_HEADER) + (nxt.start() if nxt else len(tail))
    return start, end, system_prompt[start:end]


def parse_skills_block(system_prompt: str) -> list[dict[str, str]]:
    """从 system prompt 里解析出技能条目（原始 description）。"""
    located = locate_skills_block(system_prompt)
    if located is None:
        return []
    items: list[dict[str, str]] = []
    for name, desc, path in _ITEM_RE.findall(located[2]):
        name = clean_text(name, 200)
        if not name:
            continue
        root, rel = split_root(path)
        items.append(
            {
                "name": name,
                "description": clean_text(desc, 4000),
                "path": path.strip(),
                "root": root,
                "rel": rel,
            }
        )
    return items


def skill_overrides(overrides: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = (overrides or {}).get("skills")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, item in raw.items():
        key = clean_text(name, 200)
        if not key or not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        description = clean_text(item.get("description"), 1000)
        if description:
            entry["description"] = description
        if item.get("hidden"):
            entry["hidden"] = True
        if entry:
            result[key] = entry
    return result


def build_skills_block(
    items: list[dict[str, str]],
    *,
    overrides: dict[str, Any] | None = None,
    desc_max_chars: int = 50,
    group_root: bool = True,
    compress_rules: bool = True,
    original_block: str = "",
) -> tuple[str, dict[str, Any]]:
    """把解析出的技能条目渲染成精简版 Skills 段。"""
    stats: dict[str, Any] = {
        "skills": len(items),
        "hidden": 0,
        "overridden": 0,
    }
    overrides_map = skill_overrides(overrides)

    groups: "OrderedDict[str, list[tuple[str, str, str]]]" = OrderedDict()
    for item in items:
        name = item["name"]
        override = overrides_map.get(name, {})
        if override.get("hidden"):
            stats["hidden"] += 1
            continue
        description = override.get("description") or ""
        if description:
            stats["overridden"] += 1
        else:
            description = first_sentence(item.get("description", ""), desc_max_chars)
        root = item.get("root", "")
        if not group_root:
            root, rel = "", item.get("path") or item.get("rel") or ""
        else:
            rel = item.get("rel") or item.get("path") or ""
        groups.setdefault(root, []).append((name, description, rel))

    lines: list[str] = [SKILLS_HEADER, "", _HEADER_INTRO, ""]
    for root, entries in groups.items():
        if root:
            lines.append(f"技能根目录：`{root}`")
        for name, description, rel in entries:
            lines.append(f"- {name}: {description} → `{rel}`")
        lines.append("")

    if compress_rules:
        rules = _DEFAULT_RULES.strip()
    else:
        match = re.search(r"### Skill rules\n(.*)\Z", original_block, re.S)
        rules = ("### Skill rules\n" + match.group(1).strip()) if match else ""
    if rules:
        lines.append(rules)
    lines.append("")
    return "\n".join(lines), stats


def apply_skills_block(
    system_prompt: str,
    *,
    overrides: dict[str, Any] | None = None,
    desc_max_chars: int = 50,
    group_root: bool = True,
    compress_rules: bool = True,
) -> tuple[str, dict[str, Any]]:
    """压缩 system prompt 里的 Skills 段。

    Returns:
        ``(new_system_prompt, stats)``；找不到可压缩的段时原样返回，
        ``stats["applied"]`` 为 False。
    """
    stats: dict[str, Any] = {
        "applied": False,
        "before_chars": 0,
        "after_chars": 0,
        "skills": 0,
        "hidden": 0,
        "overridden": 0,
    }
    located = locate_skills_block(system_prompt)
    if located is None:
        return system_prompt, stats
    start, end, block = located
    items = parse_skills_block(system_prompt)
    if not items:
        return system_prompt, stats

    new_block, extra = build_skills_block(
        items,
        overrides=overrides,
        desc_max_chars=desc_max_chars,
        group_root=group_root,
        compress_rules=compress_rules,
        original_block=block,
    )
    if len(new_block) >= len(block):
        # 技能很少 / 原文本来就短时压缩反而更长，那就别动
        return system_prompt, stats

    new_prompt = system_prompt[:start] + new_block + system_prompt[end:]
    stats.update(extra)
    stats.update(
        applied=True,
        before_chars=len(block),
        after_chars=len(new_block),
    )
    return new_prompt, stats
