"""astrbot_plugin_lazy_tools —— 工具按需注入（Lazy Tools）。

核心行为：

- 在 ``on_llm_request`` 的最后阶段，把完整工具集换成
  ``astrbot_find_tools`` / ``astrbot_load_tools`` / ``astrbot_unload_tools``
  三个元工具 + 常驻工具 + 本会话已加载工具；
- 模型需要别的能力时先搜索、再加载，加载后的工具在下一次请求直接可用；
- 已加载组按会话记忆，因此同一会话后续轮次的工具块保持稳定（利于前缀缓存）；
- 管理页（插件详情 → 页面 → 工具组管理）可视化调整：组说明、组显示名、
  工具说明、工具归组、隐藏工具/组、常驻组、上限、自动预加载等。
"""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .lazy_tools.config import load_config, load_overrides, save_config
from .lazy_tools.runtime import LazyToolsRuntime
from .lazy_tools.web_api import register_routes

PLUGIN_NAME = "astrbot_plugin_lazy_tools"
PLUGIN_DESC = "工具分组按需注入：省 token、提缓存命中，并可在插件页管理组说明/工具说明"
PLUGIN_VERSION = "v1.1.1"

_COMMAND_ALIASES = {"状态": "status", "分组": "groups", "组": "groups"}


def _split_command_args(event: AstrMessageEvent) -> list[str]:
    raw = str(getattr(event, "message_str", "") or "").strip()
    tokens = [token for token in re.split(r"\s+", raw) if token]
    tokens = [
        token
        for token in tokens
        if token and not token.startswith("/") and token.lower() != "lazy"
    ]
    if tokens and tokens[0] in _COMMAND_ALIASES:
        tokens[0] = _COMMAND_ALIASES[tokens[0]]
    return tokens


@register(PLUGIN_NAME, "AwayDAJIBA", PLUGIN_DESC, PLUGIN_VERSION)
class LazyToolsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.plugin_config: dict[str, Any] = config if isinstance(config, dict) else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.runtime = LazyToolsRuntime(self.data_dir, self.logger)
        self.runtime.set_context(context)
        self._routes_registered = False

    async def initialize(self) -> None:
        """插件加载时读取磁盘配置并注册管理接口。"""
        try:
            self.runtime.reload()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[lazy-tools] 读取配置失败，使用默认值: %s", exc)
        try:
            if not self._routes_registered:
                register_routes(self.context, self.runtime, PLUGIN_NAME)
                self._routes_registered = True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[lazy-tools] 注册管理接口失败: %s", exc)
        cfg = self.runtime.config
        self.logger.info(
            "[lazy-tools] 已加载 | enabled=%s mode=%s pin_groups=%s | 数据目录=%s",
            cfg.get("enabled"),
            cfg.get("mode"),
            ",".join(cfg.get("pin_groups") or []) or "(none)",
            self.data_dir,
        )

    async def terminate(self) -> None:
        self.logger.info("[lazy-tools] 插件已卸载；按需注入停止。")

    # ------------------------------------------------------------------ 注入
    @filter.on_llm_request(priority=-50000)
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """在其它插件处理完之后，把请求工具集替换为按需集合。"""
        try:
            self.runtime.transform_request(event, req, self.plugin_config)
        except Exception as exc:  # noqa: BLE001
            # 任何异常都不能影响主流程：回退到原始全量工具集。
            self.logger.warning(
                "[lazy-tools] 注入失败，本次回退全量工具集: %s", exc, exc_info=True
            )

    # ------------------------------------------------------------------ 命令
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _status_text(self) -> str:
        cfg = self.runtime.config
        last = self.runtime.last_stats or {}
        lines = [
            "Lazy Tools 状态",
            f"- 开关: {'开' if cfg.get('enabled') else '关'} | 模式: {cfg.get('mode')}",
            f"- 常驻组: {', '.join(cfg.get('pin_groups') or []) or '(无)'}",
            f"- 技能段压缩: {'开' if cfg.get('skills_enabled') else '关'}",
        ]
        if last:
            if last.get("tools_applied", True):
                lines.append(
                    "- 上次请求: {injected}/{full} 工具，schema ≈{before}→{after} tokens（−{pct}%）".format(
                        injected=last.get("injected_tools", "?"),
                        full=last.get("full_tools", "?"),
                        before=last.get("full_tokens", "?"),
                        after=last.get("injected_tokens", "?"),
                        pct=last.get("saved_percent", "?"),
                    )
                )
                lines.append(
                    "- 上次加载组: "
                    + (", ".join(last.get("active_groups") or []) or "(无)")
                )
            if last.get("skills_applied"):
                lines.append(
                    "- 上次技能段: {count} 个技能，{before}→{after} 字符（省 {saved}）".format(
                        count=last.get("skills_count", 0),
                        before=last.get("skills_before_chars", 0),
                        after=last.get("skills_after_chars", 0),
                        saved=last.get("skills_saved_chars", 0),
                    )
                )
        else:
            lines.append("- 本次进程还没有注入记录（发一条会触发 LLM 的消息即可）")
        lines.append(f"- 会话缓存: {len(self.runtime.sessions_snapshot())} 个")
        return "\n".join(lines)

    def _skills_text(self) -> str:
        snapshot = self.runtime.snapshot()
        skills = snapshot.get("skills") or []
        if not skills:
            return "当前没有发现技能（或 AstrBot 技能目录为空）。"
        lines = [f"技能（{len(skills)}）："]
        for skill in skills:
            flags: list[str] = []
            if not skill.get("active", True):
                flags.append("未启用")
            if skill.get("hidden"):
                flags.append("已隐藏")
            if skill.get("overridden"):
                flags.append("自定义简介")
            if skill.get("missing"):
                flags.append("已不存在")
            suffix = (" [" + ", ".join(flags) + "]") if flags else ""
            lines.append(f"- {skill['name']}{suffix}: {skill.get('intro') or ''}")
        stats = snapshot.get("skill_stats") or {}
        if stats.get("applied"):
            lines.append(
                "上次压缩: {before} → {after} 字符（省 {saved}）".format(
                    before=stats.get("before_chars", 0),
                    after=stats.get("after_chars", 0),
                    saved=max(
                        0,
                        int(stats.get("before_chars") or 0)
                        - int(stats.get("after_chars") or 0),
                    ),
                )
            )
        return "\n".join(lines)

    def _groups_text(self) -> str:
        snapshot = self.runtime.snapshot()
        groups = snapshot.get("groups") or []
        if not groups:
            return "当前没有可用的工具组（工具集为空，或全部被隐藏）。"
        lines = [f"工具组（{len(groups)}）："]
        for group in groups:
            marker = " [常驻]" if group.get("pinned") else ""
            lines.append(
                f"- {group['id']} ({group.get('tool_count', 0)} 工具){marker}: "
                f"{group.get('description') or ''}"
            )
        hidden = snapshot.get("hidden_groups") or []
        if hidden:
            lines.append("已隐藏组: " + ", ".join(hidden))
        return "\n".join(lines)

    @filter.command("lazy")
    async def cmd_lazy(self, event: AstrMessageEvent):
        """工具注入管理：/lazy [status|groups|skills|on|off|pin|unpin|desc|tooldesc|skilldesc|skillhide|skillshow|skillreset|reset]"""
        try:
            args = _split_command_args(event)
            sub = args[0] if args else "status"
            if sub == "status":
                yield event.plain_result(self._status_text())
                return
            if sub == "groups":
                yield event.plain_result(self._groups_text())
                return
            if sub == "skills":
                yield event.plain_result(self._skills_text())
                return
            if not self._is_admin(event):
                yield event.plain_result("这个操作需要管理员权限喵。")
                return
            if sub in {"on", "off"}:
                cfg = self.runtime.update_config({"enabled": sub == "on"})
                yield event.plain_result(
                    "已" + ("开启" if cfg.get("enabled") else "关闭") + "按需注入。"
                )
                return
            if sub in {"pin", "unpin"}:
                if len(args) < 2:
                    yield event.plain_result(f"用法: /lazy {sub} <组id>")
                    return
                group_id = args[1]
                pins = [g for g in (self.runtime.config.get("pin_groups") or [])]
                if sub == "pin":
                    if group_id not in pins:
                        pins.append(group_id)
                else:
                    pins = [g for g in pins if g != group_id]
                self.runtime.update_config({"pin_groups": pins})
                yield event.plain_result(
                    ("已常驻组: " if sub == "pin" else "已取消常驻: ") + group_id
                )
                return
            if sub in {"desc", "tooldesc"}:
                if len(args) < 3:
                    yield event.plain_result(
                        f"用法: /lazy {sub} <{'组id' if sub == 'desc' else '工具名'}> <说明文字>"
                    )
                    return
                target, text = args[1], " ".join(args[2:])
                if sub == "desc":
                    self.runtime.update_overrides(
                        {"group_updates": {target: {"description": text}}}
                    )
                else:
                    self.runtime.update_overrides(
                        {"tool_updates": {target: {"description": text}}}
                    )
                yield event.plain_result(f"已更新 {target} 的说明（下一轮生效）。")
                return
            if sub in {"skilldesc", "skillhide", "skillshow", "skillreset"}:
                if len(args) < 2:
                    yield event.plain_result(
                        "用法: /lazy skilldesc <技能名> <说明> | skillhide <技能名> | "
                        "skillshow <技能名> | skillreset <技能名>"
                    )
                    return
                skill_name = args[1]
                if sub == "skilldesc":
                    if len(args) < 3:
                        yield event.plain_result("用法: /lazy skilldesc <技能名> <说明>")
                        return
                    text = " ".join(args[2:])
                    self.runtime.update_overrides(
                        {"skill_updates": {skill_name: {"description": text}}}
                    )
                    yield event.plain_result(
                        f"已更新技能 {skill_name} 的自定义简介（下一轮生效）。"
                    )
                    return
                if sub in {"skillhide", "skillshow"}:
                    self.runtime.update_overrides(
                        {"skill_updates": {skill_name: {"hidden": sub == "skillhide"}}}
                    )
                    yield event.plain_result(
                        ("已隐藏技能: " if sub == "skillhide" else "已恢复技能: ")
                        + skill_name
                    )
                    return
                self.runtime.update_overrides({"reset_skill_names": [skill_name]})
                yield event.plain_result(f"已恢复技能 {skill_name} 的自动简介/显示状态。")
                return
            if sub == "reset":
                count = self.runtime.reset_sessions()
                yield event.plain_result(f"已清空 {count} 个会话的已加载工具组记忆。")
                return
            yield event.plain_result(
                "用法: /lazy [status|groups|skills|on|off|pin <组>|unpin <组>|"
                "desc <组> <说明>|tooldesc <工具> <说明>|skilldesc <技能> <说明>|"
                "skillhide <技能>|skillshow <技能>|skillreset <技能>|reset]"
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[lazy-tools] 命令处理失败: %s", exc, exc_info=True)
            yield event.plain_result(f"处理失败: {exc}")
