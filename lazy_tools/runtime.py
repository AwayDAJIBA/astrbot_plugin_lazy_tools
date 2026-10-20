"""运行期：按会话保存已加载组、生成元工具、改写请求、管理界面快照。"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, ToolSet, logger

from .catalog import (
    META_FIND_TOOLS,
    META_LOAD_TOOLS,
    META_NAME_SET,
    META_TOOL_NAMES,
    META_UNLOAD_TOOLS,
    Catalog,
    ToolGroup,
    build_catalog,
    build_keyword_index,
    clean_one_line,
    estimate_full_tool_set_tokens,
    estimate_tool_set_tokens,
)
from .config import (
    CONFIG_DEFAULTS,
    load_config,
    load_overrides,
    save_config,
    save_overrides,
)

LAZY_TOOL_CALL_PROMPT = (
    "Tool access is two-stage to keep requests compact. "
    "Only a few tool-management tools are available up front; the full catalog is "
    "grouped and hidden until enabled. "
    "When a task needs a capability you do not have yet: call `astrbot_find_tools` "
    "(no arguments lists groups, `query` searches tools, `group` lists one group), "
    "then call `astrbot_load_tools` with the returned group id or exact tool names. "
    "Enabled tools appear in your next step and can then be called directly with "
    "their real schemas; do not call a tool before enabling it and do not invent "
    "tool names or parameters. "
    "`astrbot_unload_tools` disables tools that are no longer needed. "
    "Never return an empty response; briefly explain the purpose when starting a "
    "new type of task, but not before every tool call; keep the conversation "
    "style consistent."
)

_META_NAME_SET = META_NAME_SET

_ACTION_SPLIT_RE = re.compile(r"[\s,，、;/|]+")
_MAX_TRACKED_SESSIONS = 512
_STATS_HISTORY_LIMIT = 30


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in _ACTION_SPLIT_RE.split(value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _session_key_from(umo: str, request: Any) -> str:
    conversation = getattr(request, "conversation", None)
    cid = getattr(conversation, "cid", None) if conversation is not None else None
    if cid:
        return f"{umo}|{cid}"
    return umo or "unknown"


@dataclass(slots=True)
class SessionState:
    groups: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    last_used: float = field(default_factory=time.time)
    umo: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "umo": self.umo,
            "groups": list(self.groups),
            "tools": list(self.tools),
            "last_used": self.last_used,
        }


class LazyToolsController:
    """一次请求内的注入控制器；元工具 handler 闭包持有它。"""

    def __init__(
        self,
        *,
        catalog: Catalog,
        full_tool_set: ToolSet,
        config: dict[str, Any],
        session: SessionState,
        prompt: str | None,
        logger_: Any = None,
        on_change: Any = None,
    ) -> None:
        self.catalog = catalog
        self.full_tool_set = full_tool_set
        self.config = config
        self.session = session
        self.log = logger_ or logger
        self._on_change = on_change
        self.injected = ToolSet()

        self.meta_tools: list[FunctionTool] = []
        self.active_groups: list[str] = []
        self.active_tools: list[str] = []
        self.suppressed_tools: set[str] = set()
        self.auto_loaded_groups: list[str] = []
        self.pinned_groups: list[str] = []
        self.pinned_tools: list[str] = []
        self._keyword_index: dict[str, list[tuple[str, int]]] | None = None

        self._resolve_pins()
        self._restore()
        if self.config.get("auto_load") and prompt:
            self.auto_loaded_groups = self._auto_load_from_text(str(prompt))
            for group_id in self.auto_loaded_groups:
                if group_id not in self.active_groups:
                    self.active_groups.append(group_id)
            if self.auto_loaded_groups:
                self._persist()
        self.meta_tools = self._build_meta_tools()
        self.refresh_injected()

    # -- 状态 ------------------------------------------------------------

    def _resolve_pins(self) -> None:
        for raw in self.config.get("pin_groups") or []:
            group_id = self._resolve_group_id(raw)
            if group_id is None:
                self.log.warning("[lazy-tools] pin_groups entry %r not found", raw)
                continue
            if group_id not in self.pinned_groups:
                self.pinned_groups.append(group_id)
        for raw in self.config.get("pin_tools") or []:
            name = self._resolve_tool_name(raw)
            if name is None:
                self.log.warning("[lazy-tools] pin_tools entry %r not found", raw)
                continue
            if name not in self.pinned_tools:
                self.pinned_tools.append(name)

    def _restore(self) -> None:
        session = self.session
        max_groups = max(0, _coerce_int(self.config.get("max_active_groups"), 8))
        max_tools = max(0, _coerce_int(self.config.get("max_active_tools"), 24))
        restored_groups = [gid for gid in session.groups if gid in self.catalog.groups]
        if max_groups and len(restored_groups) > max_groups:
            restored_groups = restored_groups[-max_groups:]
        self.active_groups = [
            gid for gid in restored_groups if gid not in self.pinned_groups
        ]
        restored_tools = [
            name
            for name in session.tools
            if name in self.catalog.by_name and name not in self.pinned_tools
        ]
        if max_tools and len(restored_tools) > max_tools:
            restored_tools = restored_tools[-max_tools:]
        self.active_tools = restored_tools

    def _persist(self) -> None:
        self.session.groups = list(self.active_groups)
        self.session.tools = list(self.active_tools)
        self.session.last_used = time.time()
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                pass

    def _resolve_group_id(self, raw: Any) -> str | None:
        candidate = str(raw or "").strip()
        if not candidate:
            return None
        if candidate in self.catalog.groups:
            return candidate
        lowered = candidate.lower()
        for group_id, group in self.catalog.groups.items():
            if group_id.lower() == lowered or group.name.lower() == lowered:
                return group_id
        return None

    def _resolve_tool_name(self, raw: Any) -> str | None:
        candidate = str(raw or "").strip()
        if not candidate:
            return None
        if candidate in self.catalog.by_name:
            return candidate
        lowered = candidate.lower()
        for name in self.catalog.by_name:
            if name.lower() == lowered:
                return name
        return None

    def _active_tool_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            if name in seen or name in self.suppressed_tools:
                return
            if name in self.catalog.by_name:
                seen.add(name)
                names.append(name)

        for group_id in sorted(set(self.active_groups) | set(self.pinned_groups)):
            group = self.catalog.groups.get(group_id)
            if group is not None:
                for tool in group.tools:
                    add(tool.name)
        for name in [*self.pinned_tools, *sorted(self.active_tools)]:
            add(name)
        return names

    def refresh_injected(self) -> None:
        """重新渲染注入集合（保持 ToolSet 对象身份，供 runner 下一步直接读取）。"""
        tool_set = ToolSet()
        for meta_tool in self.meta_tools:
            tool_set.add_tool(meta_tool)
        for name in self._active_tool_names():
            tool = self.catalog.by_name.get(name)
            if tool is not None:
                tool_set.add_tool(tool)
        self.injected.tools = tool_set.tools

    def stats(self) -> dict[str, Any]:
        injected_tokens = estimate_tool_set_tokens(self.injected)
        full_tokens = estimate_full_tool_set_tokens(self.full_tool_set)
        saved = max(0, full_tokens - injected_tokens)
        percent = (saved * 100.0 / full_tokens) if full_tokens else 0.0
        return {
            "injected_tools": len(self.injected.tools),
            "full_tools": len(self.full_tool_set.tools),
            "injected_tokens": injected_tokens,
            "full_tokens": full_tokens,
            "saved_tokens": saved,
            "saved_percent": round(percent, 1),
            "active_groups": sorted(set(self.active_groups) | set(self.pinned_groups)),
            "pinned_groups": list(self.pinned_groups),
            "active_tools": sorted(set(self.active_tools) | set(self.pinned_tools)),
            "auto_loaded": list(self.auto_loaded_groups),
        }

    # -- 目录展示 ---------------------------------------------------------

    def _group_marker(self, group_id: str) -> str:
        if group_id in self.pinned_groups:
            return " [pinned]"
        if group_id in self.active_groups:
            return " [loaded]"
        return ""

    def _render_group_line(self, group_id: str) -> str:
        group = self.catalog.groups[group_id]
        return (
            f"- {group.id} ({len(group.tools)} tools){self._group_marker(group_id)}: "
            f"{group.description}"
        )

    def _render_tool_line(
        self, tool: FunctionTool, active_names: set[str] | None = None
    ) -> str:
        group_id = self.catalog.tool_to_group.get(tool.name, "")
        if active_names is None:
            active_names = set(self._active_tool_names())
        marker = " [loaded]" if tool.name in active_names else ""
        description = clean_one_line(getattr(tool, "description", "") or "", 130)
        if not description:
            description = "(no description)"
        return f"- {tool.name} [{group_id}]{marker}: {description}"

    def find_tools(self, *, query: str = "", group: str = "", limit: int = 15) -> str:
        limit = max(1, min(60, _coerce_int(limit, 15)))
        query = (query or "").strip()
        group = (group or "").strip()

        if group:
            group_id = self._resolve_group_id(group)
            if group_id is None:
                return (
                    f"error: unknown group '{group}'. "
                    "Call astrbot_find_tools() without arguments to list group ids."
                )
            tool_group = self.catalog.groups[group_id]
            active_names = set(self._active_tool_names())
            shown = tool_group.tools[:limit]
            header = (
                f"Group '{group_id}' ({len(tool_group.tools)} tools, "
                f"{'active' if group_id in self.active_groups or group_id in self.pinned_groups else 'not loaded'}): "
                f"{tool_group.description}\n"
            )
            body = "\n".join(
                self._render_tool_line(tool, active_names) for tool in shown
            )
            if len(tool_group.tools) > limit:
                body += f"\n… and {len(tool_group.tools) - limit} more tools."
            if (
                group_id not in self.active_groups
                and group_id not in self.pinned_groups
            ):
                footer = f"\nEnable it with astrbot_load_tools(groups=['{group_id}'])."
            else:
                footer = "\nThis group is already available for direct calls."
            return header + body + footer

        if not query:
            group_ids = sorted(
                self.catalog.groups,
                key=lambda gid: (
                    self.catalog.groups[gid].priority,
                    self.catalog.groups[gid].id,
                ),
            )
            lines = [self._render_group_line(gid) for gid in group_ids]
            header = (
                f"Tool groups ({len(group_ids)} groups, {len(self.catalog.by_name)} tools). "
                "Pass query=... to search tools, group=<id> to inspect a group. "
                "Enable what you need with astrbot_load_tools; loaded/pinned groups "
                "are already callable.\n"
            )
            body = "\n".join(lines[:limit])
            if len(lines) > limit:
                body += f"\n… and {len(lines) - limit} more groups."
            return header + body

        terms = [term for term in _ACTION_SPLIT_RE.split(query.lower()) if term]
        scored: list[tuple[float, str, FunctionTool]] = []
        for name, tool in self.catalog.by_name.items():
            group_id = self.catalog.tool_to_group.get(name, "")
            tool_group = self.catalog.groups.get(group_id)
            haystack_name = name.lower()
            haystack_desc = str(getattr(tool, "description", "") or "").lower()
            haystack_group = (
                f"{group_id} {tool_group.name} {tool_group.description}".lower()
                if tool_group is not None
                else group_id
            )
            score = 0.0
            for term in terms:
                if term in haystack_name:
                    score += 10.0
                if term in haystack_desc:
                    score += 4.0
                if term in haystack_group:
                    score += 2.0
            if score > 0:
                scored.append((score, name, tool))
        if not scored:
            for name in difflib.get_close_matches(
                query, list(self.catalog.by_name), n=limit, cutoff=0.55
            ):
                scored.append((0.0, name, self.catalog.by_name[name]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return (
                f"No tools matched '{query}'. Call astrbot_find_tools() without "
                "arguments to list tool groups."
            )
        shown = scored[:limit]
        active_names = set(self._active_tool_names())
        header = (
            f"Found {len(scored)} matching tools (showing {len(shown)}). "
            "Load them with astrbot_load_tools(groups=[...] or tools=[...]):\n"
        )
        body = "\n".join(
            self._render_tool_line(tool, active_names) for _score, _name, tool in shown
        )
        if len(scored) > limit:
            body += "\n… use a more specific query to narrow the results."
        return header + body

    # -- 加载 / 卸载 ------------------------------------------------------

    def load_tools(self, *, groups: Any = None, tools: Any = None) -> str:
        group_inputs = _coerce_name_list(groups)
        tool_inputs = _coerce_name_list(tools)
        if not group_inputs and not tool_inputs:
            return (
                "error: provide groups=[...] and/or tools=[...]. "
                "Use astrbot_find_tools to discover ids first."
            )

        errors: list[str] = []
        enabled_groups: list[str] = []
        enabled_tools: list[str] = []
        evicted_groups: list[str] = []

        for raw in group_inputs:
            group_id = self._resolve_group_id(raw)
            if group_id is None:
                errors.append(f"unknown group '{raw}'")
                continue
            for tool in self.catalog.groups[group_id].tools:
                self.suppressed_tools.discard(tool.name)
            if group_id in self.pinned_groups or group_id in self.active_groups:
                continue
            self.active_groups.append(group_id)
            enabled_groups.append(group_id)
            for tool in self.catalog.groups[group_id].tools:
                if tool.name in self.active_tools:
                    self.active_tools.remove(tool.name)

        for raw in tool_inputs:
            name = self._resolve_tool_name(raw)
            if name is None:
                close = difflib.get_close_matches(
                    raw, list(self.catalog.by_name), n=1, cutoff=0.75
                )
                if close:
                    errors.append(f"unknown tool '{raw}' (did you mean '{close[0]}'?)")
                else:
                    errors.append(f"unknown tool '{raw}'")
                continue
            self.suppressed_tools.discard(name)
            if name in self.pinned_tools:
                continue
            group_id = self.catalog.tool_to_group.get(name, "")
            if group_id and (
                group_id in self.active_groups or group_id in self.pinned_groups
            ):
                continue
            if name not in self.active_tools:
                self.active_tools.append(name)
                enabled_tools.append(name)

        hard_max = _coerce_int(self.config.get("max_active_groups"), 8)
        if hard_max <= 0:
            hard_max = 10**6
        else:
            hard_max = max(hard_max, min(24, hard_max + 8))
        while len(self.active_groups) > hard_max:
            dropped = self.active_groups.pop(0)
            evicted_groups.append(dropped)
            dropped_group = self.catalog.groups.get(dropped)
            if dropped_group is not None:
                for tool in dropped_group.tools:
                    if tool.name in self.active_tools:
                        self.active_tools.remove(tool.name)
        max_tools = _coerce_int(self.config.get("max_active_tools"), 24)
        while max_tools > 0 and len(self.active_tools) > max_tools:
            self.active_tools.pop(0)

        self._persist()
        self.refresh_injected()

        parts: list[str] = []
        if enabled_groups:
            details = []
            for group_id in enabled_groups:
                names = self.catalog.groups[group_id].tool_names()
                preview = ", ".join(names[:10])
                if len(names) > 10:
                    preview += f", … (+{len(names) - 10})"
                details.append(f"group '{group_id}' ({preview})")
            parts.append("Enabled " + "; ".join(details))
        if enabled_tools:
            parts.append("Enabled tool(s): " + ", ".join(enabled_tools))
        if evicted_groups:
            parts.append(
                "Auto-disabled older group(s) to stay within the limit: "
                + ", ".join(evicted_groups)
            )
        if errors:
            parts.append("Ignored: " + "; ".join(errors))
        if not enabled_groups and not enabled_tools:
            body = "Nothing new to enable."
            if errors:
                body += " Ignored: " + "; ".join(errors)
            return body
        body = ".\n".join(parts) + "."
        body += (
            "\nThese tools are injected into the next request — call them directly "
            "in your next step."
        )
        return body

    def unload_tools(self, *, groups: Any = None, tools: Any = None) -> str:
        group_inputs = _coerce_name_list(groups)
        tool_inputs = _coerce_name_list(tools)
        if not group_inputs and not tool_inputs:
            return "error: provide groups=[...] and/or tools=[...] to disable."

        disabled_groups: list[str] = []
        disabled_tools: list[str] = []
        notes: list[str] = []
        for raw in group_inputs:
            group_id = self._resolve_group_id(raw)
            if group_id is None:
                notes.append(f"unknown group '{raw}'")
                continue
            if group_id in self.pinned_groups:
                notes.append(f"group '{group_id}' is pinned by configuration")
                continue
            if group_id in self.active_groups:
                self.active_groups.remove(group_id)
                disabled_groups.append(group_id)
            for tool in self.catalog.groups[group_id].tools:
                self.suppressed_tools.discard(tool.name)
        for raw in tool_inputs:
            name = self._resolve_tool_name(raw)
            if name is None:
                notes.append(f"unknown tool '{raw}'")
                continue
            if name in self.pinned_tools:
                notes.append(f"tool '{name}' is pinned by configuration")
                continue
            group_id = self.catalog.tool_to_group.get(name, "")
            if name in self.active_tools:
                self.active_tools.remove(name)
                disabled_tools.append(name)
            elif group_id in self.active_groups:
                self.suppressed_tools.add(name)
                disabled_tools.append(name)
            else:
                notes.append(f"tool '{name}' is not currently enabled")

        self._persist()
        self.refresh_injected()
        parts: list[str] = []
        if disabled_groups:
            parts.append("Disabled group(s): " + ", ".join(disabled_groups))
        if disabled_tools:
            parts.append("Disabled tool(s): " + ", ".join(disabled_tools))
        if notes:
            parts.append("Notes: " + "; ".join(notes))
        if not disabled_groups and not disabled_tools:
            return "Nothing was disabled. " + (" ".join(notes) if notes else "")
        remaining = sorted(set(self.active_groups) | set(self.pinned_groups))
        parts.append(
            "Active groups now: " + (", ".join(remaining) if remaining else "(none)")
        )
        return ".\n".join(parts) + "."

    def explain_missing_tool(self, name: str) -> str:
        close = difflib.get_close_matches(
            name, list(self.catalog.by_name), n=2, cutoff=0.7
        )
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return (
            f"error: tool '{name}' is not available.{hint} "
            "Call astrbot_find_tools(query=...) to search the catalog, then "
            "astrbot_load_tools(groups=[...] or tools=[...]) to enable it."
        )

    # -- 自动预加载 -------------------------------------------------------

    def _auto_load_from_text(self, text: str) -> list[str]:
        max_groups = _coerce_int(self.config.get("auto_load_max_groups"), 1)
        if max_groups <= 0:
            return []
        if self._keyword_index is None:
            self._keyword_index = build_keyword_index(self.catalog)
        haystack = text.lower()
        if len(haystack) > 4000:
            return []
        scores: dict[str, float] = {}
        best_weights: dict[str, int] = {}
        for keyword, entries in self._keyword_index.items():
            if len(keyword) < 2 or keyword not in haystack:
                continue
            for group_id, weight in entries:
                scores[group_id] = scores.get(group_id, 0.0) + weight
                if weight > best_weights.get(group_id, 0):
                    best_weights[group_id] = weight
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        best_id, best_score = ranked[0]
        # 保守策略：只认“用户明确说到组名/组 id”这种强信号，且要求明显领先，
        # 避免把描述里的泛词误判成要加载整组（错误加载会破坏缓存、浪费 token）。
        if best_score < 4.0:
            return []
        if best_weights.get(best_id, 0) < 3:
            return []
        if len(ranked) > 1 and best_score < ranked[1][1] * 2:
            return []
        if best_id in self.active_groups or best_id in self.pinned_groups:
            return []
        return [best_id][:max_groups]

    # -- 元工具 -----------------------------------------------------------

    def _build_meta_tools(self) -> list[FunctionTool]:
        controller = self

        async def find_tools(
            event,
            query: str = "",
            group: str = "",
            limit: int = 15,
            **_kwargs,
        ) -> str:
            return controller.find_tools(query=query, group=group, limit=limit)

        async def load_tools(
            event,
            groups: Any = None,
            tools: Any = None,
            **_kwargs,
        ) -> str:
            return controller.load_tools(groups=groups, tools=tools)

        async def unload_tools(
            event,
            groups: Any = None,
            tools: Any = None,
            **_kwargs,
        ) -> str:
            return controller.unload_tools(groups=groups, tools=tools)

        find_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords matched against tool names/descriptions. "
                        "Omit to list tool groups."
                    ),
                },
                "group": {
                    "type": "string",
                    "description": "Group id whose tools should be listed.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of result lines (default 15).",
                    "minimum": 1,
                    "maximum": 60,
                },
            },
        }
        load_schema = {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Group ids returned by astrbot_find_tools.",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names to enable individually.",
                },
            },
        }
        return [
            FunctionTool(
                name=META_FIND_TOOLS,
                description=(
                    "Discover tools without loading them. No arguments: list tool "
                    "groups. query=...: search tools. group=...: list one group's "
                    "tools. Load what you need with astrbot_load_tools."
                ),
                parameters=find_schema,
                handler=find_tools,
            ),
            FunctionTool(
                name=META_LOAD_TOOLS,
                description=(
                    "Enable tool groups or individual tools so they become callable "
                    "in the next step. Prefer one whole group over many single tools. "
                    "Loaded tools stay enabled for this conversation."
                ),
                parameters=load_schema,
                handler=load_tools,
            ),
            FunctionTool(
                name=META_UNLOAD_TOOLS,
                description=(
                    "Disable loaded tool groups or tools that are no longer needed, "
                    "to keep requests small."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "groups": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Group ids to disable.",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tool names to disable individually.",
                        },
                    },
                },
                handler=unload_tools,
            ),
        ]


class LazyToolsRuntime:
    """插件级服务：配置/覆盖项缓存、会话状态、请求注入、页面快照。"""

    STATE_FILENAME = "state.json"

    def __init__(self, data_dir: Any, plugin_logger: Any = None) -> None:
        self.data_dir = data_dir
        self.log = plugin_logger or logger
        self.config: dict[str, Any] = load_config(data_dir)
        self.overrides: dict[str, Any] = load_overrides(data_dir)
        self._sessions: dict[str, SessionState] = {}
        self._stats_history: list[dict[str, Any]] = []
        self.last_stats: dict[str, Any] | None = None
        self._warned_skills_like = False
        self._last_full_tool_set: ToolSet | None = None
        self._load_state()

    # -- 状态持久化 -------------------------------------------------------

    def _load_state(self) -> None:
        try:
            path = self.data_dir / self.STATE_FILENAME
            if not path.is_file():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            sessions = payload.get("sessions")
            if isinstance(sessions, dict):
                for key, item in sessions.items():
                    if not isinstance(item, dict):
                        continue
                    groups = [str(g) for g in item.get("groups") or [] if str(g)]
                    tools = [str(t) for t in item.get("tools") or [] if str(t)]
                    last_used = item.get("last_used")
                    self._sessions[str(key)] = SessionState(
                        groups=groups,
                        tools=tools,
                        last_used=float(last_used) if isinstance(last_used, (int, float)) else time.time(),
                        umo=str(item.get("umo") or ""),
                    )
            history = payload.get("stats_history")
            if isinstance(history, list):
                self._stats_history = [
                    item for item in history if isinstance(item, dict)
                ][-_STATS_HISTORY_LIMIT:]
            last_stats = payload.get("last_stats")
            if isinstance(last_stats, dict):
                self.last_stats = last_stats
        except Exception as exc:  # noqa: BLE001
            self.log.warning("[lazy-tools] 读取会话状态失败: %s", exc)

    def _save_state(self) -> None:
        try:
            sessions = {
                key: {
                    "umo": state.umo,
                    "groups": list(state.groups),
                    "tools": list(state.tools),
                    "last_used": state.last_used,
                }
                for key, state in self._sessions.items()
            }
            payload = {
                "sessions": sessions,
                "stats_history": self._stats_history[-_STATS_HISTORY_LIMIT:],
                "last_stats": self.last_stats,
            }
            path = self.data_dir / self.STATE_FILENAME
            text = json.dumps(payload, ensure_ascii=False)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("[lazy-tools] 保存会话状态失败: %s", exc)

    # -- 配置 / 覆盖 ------------------------------------------------------

    def set_context(self, context: Any) -> None:
        """由插件入口注入 Star Context（用于读取全局配置、重建目录）。"""
        self._context = context

    def reload(self) -> None:
        self.config = load_config(self.data_dir)
        self.overrides = load_overrides(self.data_dir)

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        merged = {**self.config, **(patch or {})}
        self.config = save_config(self.data_dir, merged)
        return self.config

    def update_overrides(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按增量方式覆盖组/工具设置（网页保存用）。"""
        overrides = json.loads(json.dumps(self.overrides))  # deep copy
        group_updates = payload.get("group_updates") or payload.get("groups") or {}
        if isinstance(group_updates, dict):
            groups = overrides.setdefault("groups", {})
            for group_id, item in group_updates.items():
                gid = str(group_id).strip()
                if not gid:
                    continue
                if not isinstance(item, dict):
                    continue
                entry = groups.setdefault(gid, {})
                if "name" in item:
                    name = clean_one_line(item.get("name"), 80)
                    if name:
                        entry["name"] = name
                    else:
                        entry.pop("name", None)
                if "description" in item:
                    desc = clean_one_line(item.get("description"), 500)
                    if desc:
                        entry["description"] = desc
                    else:
                        entry.pop("description", None)
                if "hidden" in item:
                    if item.get("hidden"):
                        entry["hidden"] = True
                    else:
                        entry.pop("hidden", None)
                if not entry:
                    groups.pop(gid, None)
        tool_updates = payload.get("tool_updates") or payload.get("tools") or {}
        if isinstance(tool_updates, dict):
            tools = overrides.setdefault("tools", {})
            for tool_name, item in tool_updates.items():
                name = str(tool_name).strip()
                if not name or not isinstance(item, dict):
                    continue
                entry = tools.setdefault(name, {})
                if "group" in item:
                    group = clean_one_line(item.get("group"), 80)
                    if group:
                        entry["group"] = group
                    else:
                        entry.pop("group", None)
                if "description" in item:
                    desc = clean_one_line(item.get("description"), 1000)
                    if desc:
                        entry["description"] = desc
                    else:
                        entry.pop("description", None)
                if "hidden" in item:
                    if item.get("hidden"):
                        entry["hidden"] = True
                    else:
                        entry.pop("hidden", None)
                if not entry:
                    tools.pop(name, None)
        for group_id in payload.get("reset_group_ids") or []:
            overrides.get("groups", {}).pop(str(group_id), None)
        for tool_name in payload.get("reset_tool_names") or []:
            overrides.get("tools", {}).pop(str(tool_name), None)
        if payload.get("reset_all"):
            overrides["groups"] = {}
            overrides["tools"] = {}
        self.overrides = save_overrides(self.data_dir, overrides)
        return self.overrides

    # -- 会话 -------------------------------------------------------------

    def _session(self, key: str, umo: str) -> SessionState:
        state = self._sessions.get(key)
        now = time.time()
        ttl = _coerce_int(self.config.get("session_ttl_seconds"), 0)
        if state is not None and ttl > 0 and now - state.last_used > ttl:
            self._sessions.pop(key, None)
            state = None
        if state is None:
            state = SessionState(umo=umo)
            self._sessions[key] = state
        state.last_used = now
        if len(self._sessions) > _MAX_TRACKED_SESSIONS:
            stale = sorted(self._sessions.items(), key=lambda item: item[1].last_used)
            for stale_key, _ in stale[: len(self._sessions) - _MAX_TRACKED_SESSIONS]:
                self._sessions.pop(stale_key, None)
        return state

    def reset_sessions(self) -> int:
        count = len(self._sessions)
        self._sessions.clear()
        self._save_state()
        return count

    def sessions_snapshot(self) -> list[dict[str, Any]]:
        values = sorted(
            self._sessions.values(), key=lambda item: item.last_used, reverse=True
        )
        return [value.summary() for value in values[:100]]

    # -- 请求注入 ---------------------------------------------------------

    def _effective_enabled(self, plugin_config: dict[str, Any]) -> bool:
        plugin_enabled = plugin_config.get("enabled", True)
        if isinstance(plugin_enabled, str):
            plugin_enabled = plugin_enabled.strip().lower() not in ("false", "0", "off")
        return bool(self.config.get("enabled", True)) and bool(plugin_enabled)

    def transform_request(
        self,
        event: Any,
        request: Any,
        plugin_config: dict[str, Any] | None = None,
    ) -> bool:
        """把 req.func_tool 换成 lazy 集合；成功返回 True。"""
        cfg = self.config
        if not self._effective_enabled(plugin_config or {}):
            return False
        if cfg.get("mode") != "lazy":
            return False
        full_tool_set = getattr(request, "func_tool", None)
        if not isinstance(full_tool_set, ToolSet) or not full_tool_set.tools:
            return False
        if len(full_tool_set.tools) <= _coerce_int(cfg.get("min_tools_for_lazy"), 12):
            return False
        if any(tool.name in _META_NAME_SET for tool in full_tool_set.tools):
            self.log.warning(
                "[lazy-tools] skipped: existing tool name collides with meta tools."
            )
            return False
        self._warn_if_skills_like()

        catalog = build_catalog(full_tool_set, self.overrides)
        if not catalog.groups:
            return False
        self._last_full_tool_set = full_tool_set
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        key = _session_key_from(umo, request)
        session = self._session(key, umo)
        controller = LazyToolsController(
            catalog=catalog,
            full_tool_set=full_tool_set,
            config=cfg,
            session=session,
            prompt=getattr(request, "prompt", None),
            logger_=self.log,
            on_change=self._save_state,
        )
        request.func_tool = controller.injected
        self._rewrite_prompt(request)
        try:
            event.set_extra("_lazy_tools_controller", controller)
        except Exception:
            pass

        stats = controller.stats()
        stats.update(
            {
                "ts": time.time(),
                "umo": umo,
                "session": key,
                "mode": "lazy",
            }
        )
        self.last_stats = stats
        self._stats_history.append(stats)
        if len(self._stats_history) > _STATS_HISTORY_LIMIT:
            self._stats_history = self._stats_history[-_STATS_HISTORY_LIMIT:]
        if cfg.get("debug_log"):
            self.log.info(
                "[lazy-tools] injected %d/%d tools, %d/%d groups active (%s); "
                "schema tokens ≈%d → ≈%d (−%.0f%%)",
                stats["injected_tools"],
                stats["full_tools"],
                len(stats["active_groups"]),
                len(catalog.groups),
                ", ".join(stats["active_groups"]) or "none",
                stats["full_tokens"],
                stats["injected_tokens"],
                stats["saved_percent"],
            )
            if stats["auto_loaded"]:
                self.log.info(
                    "[lazy-tools] auto-loaded group(s): %s",
                    ", ".join(stats["auto_loaded"]),
                )
        self._save_state()
        return True

    def _warn_if_skills_like(self) -> None:
        if self._warned_skills_like:
            return
        self._warned_skills_like = True
        try:
            conf = self._core_misc_tool_schema_mode()
        except Exception:
            conf = "full"
        if conf == "skills_like":
            self.log.warning(
                "[lazy-tools] tool_schema_mode=skills_like 与按需注入不兼容，"
                "请在 AstrBot 配置 → Agent → 工具调用模式中改为 full。"
            )

    def _core_misc_tool_schema_mode(self) -> str:
        try:
            context = getattr(self, "_context", None)
            if context is None:
                return "full"
            cfg = context.get_config()
            return str(
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("misc", {})
                .get("tool_schema_mode", "full")
            )
        except Exception:
            return "full"

    @staticmethod
    def _rewrite_prompt(request: Any) -> None:
        from astrbot.core.astr_main_agent_resources import (
            TOOL_CALL_PROMPT,
            TOOL_CALL_PROMPT_SKILLS_LIKE_MODE,
        )

        current = getattr(request, "system_prompt", "") or ""
        replaced = False
        for original in (TOOL_CALL_PROMPT, TOOL_CALL_PROMPT_SKILLS_LIKE_MODE):
            if original and original in current:
                current = current.replace(original, LAZY_TOOL_CALL_PROMPT, 1)
                replaced = True
                break
        if not replaced:
            current = (
                f"{current}\n{LAZY_TOOL_CALL_PROMPT}\n" if current else LAZY_TOOL_CALL_PROMPT
            )
        request.system_prompt = current

    # -- 页面快照 ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        try:
            full_tool_set = self._last_full_tool_set or self._current_full_tool_set()
            catalog = build_catalog(full_tool_set, self.overrides) if full_tool_set else None
        except Exception:
            catalog = None
        groups: list[dict[str, Any]] = []
        if catalog is not None:
            for group in sorted(
                catalog.groups.values(), key=lambda item: (item.priority, item.id)
            ):
                groups.append(
                    {
                        "id": group.id,
                        "name": group.name,
                        "auto_name": group.auto_name,
                        "description": group.description,
                        "auto_description": group.auto_description,
                        "kind": group.kind,
                        "tool_count": len(group.tools),
                        "hidden": group.hidden,
                        "pinned": group.id in (self.config.get("pin_groups") or []),
                        "tools": [
                            {
                                "name": tool.name,
                                "description": clean_one_line(
                                    getattr(tool, "description", "") or "", 500
                                ),
                                "auto_description": clean_one_line(
                                    getattr(
                                        self._auto_tool(tool.name, full_tool_set),
                                        "description",
                                        "",
                                    )
                                    or "",
                                    500,
                                ),
                                "group": catalog.tool_to_group.get(tool.name, ""),
                                "hidden": False,
                            }
                            for tool in group.tools
                        ],
                    }
                )
        hidden_tools = sorted(catalog.hidden_tools) if catalog else []
        all_tools = sorted(catalog.by_name) if catalog else []
        hidden_tool_info: list[dict[str, Any]] = []
        full_now = self._last_full_tool_set or self._current_full_tool_set()
        auto_by_name: dict[str, str] = {}
        if full_now is not None:
            for tool in full_now.tools:
                auto_by_name[tool.name] = clean_one_line(
                    getattr(tool, "description", "") or "", 500
                )
        pristine_names = set(all_tools) | set(hidden_tools)
        for name in hidden_tools:
            hidden_tool_info.append(
                {"name": name, "auto_description": auto_by_name.get(name, "")}
            )
        for name, item in (self.overrides.get("tools") or {}).items():
            if name in pristine_names or not isinstance(item, dict):
                continue
            hidden_tool_info.append(
                {
                    "name": name,
                    "auto_description": auto_by_name.get(name, ""),
                    "stale": True,
                }
            )
        hidden_groups = sorted(
            gid
            for gid, item in (self.overrides.get("groups") or {}).items()
            if isinstance(item, dict) and item.get("hidden")
        )
        return {
            "config": self.config,
            "defaults": CONFIG_DEFAULTS,
            "overrides": self.overrides,
            "groups": groups,
            "all_tools": all_tools,
            "hidden_tools": hidden_tools,
            "hidden_tool_info": hidden_tool_info,
            "hidden_groups": hidden_groups,
            "pinned_groups": list(self.config.get("pin_groups") or []),
            "pinned_tools": list(self.config.get("pin_tools") or []),
            "stats": self.last_stats,
            "stats_history": list(reversed(self._stats_history[-10:])),
            "sessions": self.sessions_snapshot(),
            "tool_overrides": {
                name: item
                for name, item in (self.overrides.get("tools") or {}).items()
                if isinstance(item, dict)
            },
            "group_overrides": {
                gid: item
                for gid, item in (self.overrides.get("groups") or {}).items()
                if isinstance(item, dict)
            },
        }

    def _auto_tool(self, name: str, full_tool_set: ToolSet) -> Any:
        for tool in full_tool_set.tools:
            if tool.name == name:
                return tool
        return None

    def _current_full_tool_set(self) -> ToolSet | None:
        try:
            context = getattr(self, "_context", None)
            if context is None:
                return None
            manager = context.get_llm_tool_manager()
            return manager.get_full_tool_set()
        except Exception:
            return None
