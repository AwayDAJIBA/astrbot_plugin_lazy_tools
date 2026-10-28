"""工具目录：把完整工具集分成可读、可管理的工具组。

设计原则：

- **确定性**：同样的工具集 + 同样的覆盖配置，永远得到同样的组 id / 顺序，
  这样工具 schema 块在不同轮次之间保持字节稳定（利于前缀缓存）。
- **可解释**：每个组都有 id、显示名、自动描述与可覆盖的描述；每把工具都有
  自动描述与可覆盖描述。
- **不碰全局工具对象**：工具说明覆盖通过 ``copy.copy(tool)`` 生成仅用于本轮
  注入的副本，不改动其他插件/其他会话共享的工具对象。副本保留原类
  （``_PermissionGuardedTool`` / ``MCPTool`` / ``HandoffTool``），执行路径与
  权限检查不变。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, ToolSet, logger

META_FIND_TOOLS = "astrbot_find_tools"
META_LOAD_TOOLS = "astrbot_load_tools"
META_UNLOAD_TOOLS = "astrbot_unload_tools"
META_TOOL_NAMES = (META_FIND_TOOLS, META_LOAD_TOOLS, META_UNLOAD_TOOLS)
META_NAME_SET = frozenset(META_TOOL_NAMES)

_PLUGIN_PREFIXES = ("astrbot_plugin_", "astrbot_builtin_", "astrbot_")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-.]{3,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

AUTO_LOAD_STOPWORDS = frozenset(
    {
        "tool",
        "tools",
        "plugin",
        "plugins",
        "agent",
        "agents",
        "astrbot",
        "llm",
        "chat",
        "helper",
        "service",
        "module",
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "用法",
        "使用",
        "工具",
        "插件",
        "功能",
        "支持",
        "可以",
        "提供",
        "助手",
        "查询",
        "获取",
        "管理",
        "相关",
        "一个",
    }
)

# 内置能力组：id, 显示名, 自动描述, 优先级, 精确工具名, 名称前缀
_BUILTIN_GROUP_SPECS: tuple[
    tuple[str, str, str, int, frozenset[str], tuple[str, ...]], ...
] = (
    (
        "files",
        "Files",
        "Read, write, edit, grep and transfer local files.",
        10,
        frozenset(
            {
                "astrbot_file_read_tool",
                "astrbot_file_write_tool",
                "astrbot_file_edit_tool",
                "astrbot_grep_tool",
                "astrbot_upload_file",
                "astrbot_download_file",
            }
        ),
        (),
    ),
    (
        "shell",
        "Shell & Python",
        "Run shell commands and Python code in the workspace.",
        11,
        frozenset(
            {
                "astrbot_execute_shell",
                "astrbot_shell_session",
                "astrbot_execute_python",
                "astrbot_execute_ipython",
            }
        ),
        (),
    ),
    (
        "web",
        "Web",
        "Search the web and fetch page contents.",
        12,
        frozenset(),
        (
            "web_search_",
            "image_search_",
            "finance_search_",
            "scholar_search_",
            "tavily_",
            "firecrawl_",
            "exa_",
            "deepseek_fetch_",
        ),
    ),
    (
        "browser",
        "Browser",
        "Automate a browser and run browser skills.",
        13,
        frozenset(
            {
                "astrbot_execute_browser",
                "astrbot_execute_browser_batch",
                "astrbot_run_browser_skill",
            }
        ),
        (),
    ),
    (
        "agent_skills",
        "Agent skills",
        "Inspect and curate reusable agent skills and executions.",
        14,
        frozenset(
            {
                "astrbot_get_execution_history",
                "astrbot_annotate_execution",
                "astrbot_create_skill_payload",
                "astrbot_get_skill_payload",
                "astrbot_create_skill_candidate",
                "astrbot_list_skill_candidates",
                "astrbot_evaluate_skill_candidate",
                "astrbot_promote_skill_candidate",
                "astrbot_list_skill_releases",
                "astrbot_rollback_skill_release",
                "astrbot_sync_skill_release",
            }
        ),
        (),
    ),
    (
        "computer_use",
        "Computer use",
        "Control the desktop: screenshots, mouse and keyboard.",
        15,
        frozenset(
            {
                "astrbot_cua_screenshot",
                "astrbot_cua_mouse_click",
                "astrbot_cua_keyboard_type",
            }
        ),
        ("astrbot_cua_",),
    ),
    (
        "knowledge",
        "Knowledge base",
        "Search the configured knowledge bases.",
        16,
        frozenset({"astr_kb_search"}),
        (),
    ),
    (
        "messaging",
        "Messaging",
        "Send proactive messages or read group message history.",
        17,
        frozenset({"send_message_to_user", "get_group_message_history"}),
        (),
    ),
    (
        "tasks",
        "Scheduled tasks",
        "Create and manage scheduled/background tasks.",
        18,
        frozenset({"future_task"}),
        (),
    ),
    (
        "memory",
        "Memory",
        "Long-term memory, user profiles and the knowledge graph.",
        19,
        frozenset(),
        (),
    ),
    (
        "sessions",
        "Sessions",
        "Read AstrBot / DeepSeek Harness conversation histories.",
        20,
        frozenset(),
        (),
    ),
)

_BUILTIN_GROUP_INDEX = {spec[0]: spec for spec in _BUILTIN_GROUP_SPECS}

_MEMORY_NAME_HINTS = ("memory", "knowledge_graph", "profile")
_SESSION_NAME_HINTS = ("conversation", "harness", "session")


def clean_one_line(text: Any, limit: int = 160) -> str:
    if not isinstance(text, str):
        return ""
    line = " ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    line = re.sub(r"\s+", " ", line)
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


def sanitize_group_id(raw: Any) -> str:
    text = "" if raw is None else str(raw).strip()
    text = re.sub(r"[^0-9A-Za-z_\-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_-")
    if not text:
        return "group"
    if text[0].isdigit():
        text = f"g_{text}"
    return text[:48]


def plugin_stem(name: str) -> str:
    lowered = name.lower()
    for prefix in _PLUGIN_PREFIXES:
        if lowered.startswith(prefix):
            return name[len(prefix) :]
    return name


def unwrap_tool(tool: FunctionTool) -> FunctionTool:
    wrapped = getattr(tool, "_wrapped", None)
    return wrapped if isinstance(wrapped, FunctionTool) else tool


@dataclass(slots=True)
class _Classification:
    kind: str
    key: str
    name: str
    description: str
    priority: int
    stem: str = ""


def builtin_group_for(name: str) -> str:
    for group_id, _name, _desc, _prio, exact, prefixes in _BUILTIN_GROUP_SPECS:
        if name in exact:
            return group_id
        if prefixes and name.startswith(prefixes):
            return group_id
    lowered = name.lower()
    if any(hint in lowered for hint in _MEMORY_NAME_HINTS):
        return "memory"
    if any(hint in lowered for hint in _SESSION_NAME_HINTS):
        return "sessions"
    return "other"


def classify_tool(tool: FunctionTool) -> _Classification:
    """判断工具归属（组 id 的稳定来源）。"""
    try:
        from astrbot.core.agent.handoff import HandoffTool
        from astrbot.core.agent.mcp_client import MCPTool
        from astrbot.core.star import star_map
    except Exception:  # pragma: no cover - 兼容未来版本
        HandoffTool = ()  # type: ignore[assignment]
        MCPTool = ()  # type: ignore[assignment]
        star_map = {}

    original = unwrap_tool(tool)
    name = tool.name

    if MCPTool and isinstance(original, MCPTool):
        server = str(getattr(original, "mcp_server_name", "") or "mcp")
        return _Classification(
            kind="mcp",
            key=f"mcp:{server}",
            name=f"MCP {server}",
            description=f"Tools provided by MCP server '{server}'.",
            priority=200,
        )

    if (HandoffTool and isinstance(original, HandoffTool)) or name.startswith(
        "transfer_to_"
    ):
        return _Classification(
            kind="subagent",
            key="subagent:handoff",
            name="Sub-agents",
            description="Delegate work to the configured specialist sub-agents.",
            priority=30,
        )

    module_path = getattr(tool, "handler_module_path", None) or getattr(
        original, "handler_module_path", None
    )
    if module_path:
        meta = star_map.get(module_path) if isinstance(star_map, dict) else None
        if meta is not None and getattr(meta, "name", None):
            display = (
                getattr(meta, "display_name", None)
                or getattr(meta, "name", None)
                or module_path
            )
            desc = clean_one_line(
                getattr(meta, "short_desc", None)
                or getattr(meta, "desc", None)
                or ""
            )
            return _Classification(
                kind="plugin",
                key=f"plugin:{module_path}",
                name=str(display),
                description=desc,
                priority=100,
                stem=str(getattr(meta, "name", None) or "") or plugin_stem(str(display)),
            )
        if module_path.startswith("data.plugins."):
            parts = module_path.split(".")
            stem = parts[2] if len(parts) > 2 else parts[-1]
            return _Classification(
                kind="plugin",
                key=f"plugin:{module_path}",
                name=plugin_stem(stem),
                description="",
                priority=100,
                stem=plugin_stem(stem),
            )

    group_id = builtin_group_for(name)
    spec = _BUILTIN_GROUP_INDEX.get(group_id)
    if spec:
        return _Classification(
            kind="builtin",
            key=f"builtin:{group_id}",
            name=spec[1],
            description=spec[2],
            priority=spec[3],
        )
    return _Classification(
        kind="builtin",
        key="builtin:other",
        name="Other",
        description="Miscellaneous built-in tools.",
        priority=300,
    )


def make_group_id(classification: _Classification) -> str:
    kind = classification.kind
    if kind == "builtin":
        return classification.key.split(":", 1)[1]
    if kind == "subagent":
        return "subagents"
    if kind == "mcp":
        server = classification.key.split(":", 1)[1]
        return f"mcp_{sanitize_group_id(server)}"
    if kind == "plugin":
        stem = classification.stem or classification.name
        group_id = sanitize_group_id(plugin_stem(stem))
        if group_id in {"group", "other", "files", "shell", "web", "browser"}:
            group_id = f"plugin_{group_id}"
        return group_id
    return "other"


@dataclass(slots=True)
class ToolGroup:
    """目录里的一个分组。"""

    id: str
    name: str
    auto_name: str
    description: str
    auto_description: str
    kind: str
    priority: int
    tools: list[FunctionTool] = field(default_factory=list)
    hidden: bool = False

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]


@dataclass(slots=True)
class Catalog:
    groups: dict[str, ToolGroup]
    tool_to_group: dict[str, str]
    by_name: dict[str, FunctionTool]
    hidden_tools: frozenset[str]
    overridden_tools: dict[str, dict[str, Any]]


def _effective_tool_description(tool: FunctionTool, override: str | None) -> str:
    if override:
        return override
    return clean_one_line(getattr(tool, "description", "") or "", limit=1000)


def _apply_description_override(tool: FunctionTool, description: str) -> FunctionTool:
    """生成一份仅用于本轮注入的工具副本，覆盖 description。"""
    try:
        cloned = copy.copy(tool)
        cloned.description = description
        return cloned
    except Exception:  # pragma: no cover - 理论不可达
        logger.warning(
            "[lazy-tools] failed to clone tool %s for description override",
            getattr(tool, "name", "?"),
        )
        return tool


# 有些插件会自带「插件内部的二级工具检索/列表」工具（例如 qzone_tools 的
# search_wyc_tools / call_wyc_tools）。开启按需注入后，模型容易把它们误当成
# AstrBot 的全局工具目录，查不到东西就直接放弃。这里在注入副本上补一句说明，
# 只改本轮注入的说明文案，不改插件原工具对象。
NESTED_TOOL_MANAGER_NOTE = (
    "（注：这是本插件内部的二级工具检索/列表，不是 AstrBot 全局工具目录；"
    "要查全局工具请调用 astrbot_find_tools）"
)

_TOOL_SEARCH_VERBS_RE = re.compile(r"(search|find|list|call|query|discover)")
_TOOL_WORD_RE = re.compile(r"tools?|toolset")


def looks_like_nested_tool_manager(tool: FunctionTool) -> bool:
    """判断工具是否像「插件内部工具检索/列表」工具。"""
    name = str(getattr(tool, "name", "") or "").lower()
    if not name or not _TOOL_WORD_RE.search(name):
        return False
    if not _TOOL_SEARCH_VERBS_RE.search(name):
        return False
    description = str(getattr(tool, "description", "") or "")
    if not description:
        return True
    lowered = description.lower()
    if "工具" in description:
        return True
    if "tool" in lowered and any(
        keyword in lowered
        for keyword in ("search", "find", "list", "index", "discover", "catalog")
    ):
        return True
    return False


def annotate_nested_tool_manager(tool: FunctionTool) -> FunctionTool:
    """返回带「不是全局工具目录」提示的注入副本。"""
    base = clean_one_line(getattr(tool, "description", "") or "", limit=1000)
    if not base:
        base = "(plugin-local tool index)"
    if NESTED_TOOL_MANAGER_NOTE in base:
        return tool
    limit = 1000 - len(NESTED_TOOL_MANAGER_NOTE)
    if len(base) > limit:
        base = base[:limit].rstrip()
    return _apply_description_override(tool, base + NESTED_TOOL_MANAGER_NOTE)


def build_catalog(
    full_tool_set: ToolSet,
    overrides: dict[str, Any] | None = None,
) -> Catalog:
    """把完整工具集按（覆盖后的）规则分组成目录。"""
    overrides = overrides or {}
    group_overrides: dict[str, Any] = overrides.get("groups") or {}
    tool_overrides: dict[str, Any] = overrides.get("tools") or {}

    buckets: dict[str, tuple[_Classification, list[FunctionTool]]] = {}
    hidden_tools: set[str] = set()
    overridden: dict[str, dict[str, Any]] = {}

    for tool in full_tool_set.tools:
        if not bool(getattr(tool, "active", True)):
            continue
        if tool.name in META_NAME_SET:
            logger.warning(
                "[lazy-tools] tool name %s collides with a meta tool; skipped.",
                tool.name,
            )
            continue
        override = tool_overrides.get(tool.name)
        if isinstance(override, dict):
            if override.get("hidden"):
                hidden_tools.add(tool.name)
                overridden[tool.name] = dict(override)
                continue
            overridden[tool.name] = dict(override)

        classification = classify_tool(tool)
        # 覆盖归组：用户把工具手动放进某个组（可以是不存在的自定义组）
        target_group = ""
        if isinstance(override, dict) and override.get("group"):
            target_group = sanitize_group_id(override["group"])
        if target_group:
            classification = _Classification(
                kind="custom",
                key=f"custom:{target_group}",
                name=target_group,
                description="",
                priority=90,
                stem=target_group,
            )
        entry = buckets.get(classification.key)
        if entry is None:
            entry = (classification, [])
            buckets[classification.key] = entry
        entry[1].append(tool)

    groups: dict[str, ToolGroup] = {}
    used_ids: set[str] = set()
    ordered_keys = sorted(
        buckets,
        key=lambda key: (buckets[key][0].priority, buckets[key][0].key),
    )
    for key in ordered_keys:
        classification, tools = buckets[key]
        if classification.kind == "custom":
            base_id = classification.stem or "custom"
        else:
            base_id = make_group_id(classification)
        group_id = base_id
        suffix = 2
        while group_id in used_ids:
            group_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(group_id)

        auto_description = classification.description or (
            "Custom tool group." if classification.kind == "custom" else "Tools from this plug-in."
        )
        auto_name = classification.name or group_id
        go = group_overrides.get(group_id) if isinstance(group_overrides, dict) else None
        if not isinstance(go, dict):
            go = {}
        display_name = clean_one_line(go.get("name"), 80) or auto_name
        description = clean_one_line(go.get("description"), 500) or auto_description
        hidden = bool(go.get("hidden"))

        effective_tools: list[FunctionTool] = []
        for tool in sorted(tools, key=lambda item: item.name):
            tool_override = tool_overrides.get(tool.name)
            desc_override = ""
            if isinstance(tool_override, dict):
                desc_override = clean_one_line(
                    tool_override.get("description"), 1000
                )
            if desc_override:
                effective_tools.append(_apply_description_override(tool, desc_override))
            else:
                effective_tools.append(tool)

        groups[group_id] = ToolGroup(
            id=group_id,
            name=display_name,
            auto_name=auto_name,
            description=description,
            auto_description=auto_description,
            kind=classification.kind,
            priority=classification.priority,
            tools=effective_tools,
            hidden=hidden,
        )

    # 只保留有效工具的组；被 hidden 的组不出现在目录/injected 中
    visible_groups = {
        group_id: group
        for group_id, group in groups.items()
        if group.tools and not group.hidden
    }

    tool_to_group: dict[str, str] = {}
    by_name: dict[str, FunctionTool] = {}
    for group_id, group in visible_groups.items():
        for tool in group.tools:
            tool_to_group[tool.name] = group_id
            by_name[tool.name] = tool

    return Catalog(
        groups=visible_groups,
        tool_to_group=tool_to_group,
        by_name=by_name,
        hidden_tools=frozenset(hidden_tools),
        overridden_tools=overridden,
    )


# ---------------------------------------------------------------------------
# token 估算 / 关键词索引
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - chinese
    return int(chinese * 0.6 + other * 0.3)


def estimate_tool_set_tokens(tool_set: ToolSet | None) -> int:
    if not tool_set or not tool_set.tools:
        return 0
    try:
        payload = json.dumps(tool_set.openai_schema(), ensure_ascii=False)
    except Exception:
        return 0
    return estimate_tokens(payload)


def estimate_full_tool_set_tokens(full_set: ToolSet | None) -> int:
    if not full_set or not full_set.tools:
        return 0
    try:
        payload = json.dumps(full_set.openai_schema(), ensure_ascii=False)
    except Exception:
        return 0
    return estimate_tokens(payload)


def build_keyword_index(catalog: Catalog) -> dict[str, list[tuple[str, int]]]:
    """自动预加载用的关键词索引（组名/描述/工具名；跨组高频词会被剔除）。"""
    raw: dict[str, dict[str, int]] = {}

    def add_keywords(bucket: dict[str, int], text: str, weight: int) -> None:
        if not text:
            return
        lowered = text.lower()
        for match in _ASCII_TOKEN_RE.findall(lowered):
            token = match.strip("._-")
            if token and token not in AUTO_LOAD_STOPWORDS:
                bucket[token] = max(bucket.get(token, 0), weight)
        for run in _CJK_RUN_RE.findall(text):
            if run not in AUTO_LOAD_STOPWORDS and len(run) <= 8:
                bucket[run] = max(bucket.get(run, 0), weight)
            for index in range(len(run) - 1):
                bigram = run[index : index + 2]
                if bigram not in AUTO_LOAD_STOPWORDS:
                    bucket[bigram] = max(bucket.get(bigram, 0), max(1, weight - 1))

    for group in catalog.groups.values():
        keywords: dict[str, int] = {}
        add_keywords(keywords, group.id, 3)
        add_keywords(keywords, group.name, 3)
        add_keywords(keywords, group.description, 1)
        for tool in group.tools[:12]:
            add_keywords(keywords, tool.name, 2)
        raw[group.id] = keywords

    frequency: dict[str, int] = {}
    for keywords in raw.values():
        for keyword in keywords:
            frequency[keyword] = frequency.get(keyword, 0) + 1
    index: dict[str, list[tuple[str, int]]] = {}
    for group_id, keywords in raw.items():
        for keyword, weight in keywords.items():
            if frequency.get(keyword, 99) > 2:
                continue
            index.setdefault(keyword, []).append((group_id, weight))
    return index
