"""插件页 REST API（由 dashboard 的 plugin-extension 通道转发，自动带 JWT）。

路由前缀固定为 ``/<插件名>``，因此插件页里的
``window.AstrBotPluginPage.apiGet("state")`` 会请求到
``/api/v1/plugins/extensions/<插件名>/state``。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import export_bundle, import_bundle
from .runtime import LazyToolsRuntime


def _ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"success": True}
    if data is not None:
        out["data"] = data
    out.update(extra)
    return out


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


async def _read_json(request: Any) -> dict[str, Any]:
    try:
        data = await request.get_json()
    except Exception:
        data = None
    if data is None:
        try:
            data = await request.json()
        except Exception:
            data = None
    return data if isinstance(data, dict) else {}


def register_routes(context: Any, runtime: LazyToolsRuntime, plugin_name: str) -> None:
    """注册插件管理接口；单条失败只记日志，不影响其它接口。"""
    reg = context.register_web_api
    routes: list[tuple[str, Any, list[str], str]] = []

    prefix = f"/{plugin_name}"

    async def api_state(**_kwargs) -> dict[str, Any]:
        try:
            return _ok(runtime.snapshot())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lazy-tools] api_state failed: %s", exc, exc_info=True)
            return _err(f"读取状态失败: {exc}")

    async def api_config(**_kwargs) -> dict[str, Any]:
        try:
            from quart import request

            body = await _read_json(request)
            config = runtime.update_config(body)
            return _ok(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lazy-tools] api_config failed: %s", exc, exc_info=True)
            return _err(f"保存配置失败: {exc}")

    async def api_overrides(**_kwargs) -> dict[str, Any]:
        try:
            from quart import request

            body = await _read_json(request)
            overrides = runtime.update_overrides(body)
            return _ok({"overrides": overrides})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[lazy-tools] api_overrides failed: %s", exc, exc_info=True
            )
            return _err(f"保存覆盖项失败: {exc}")

    async def api_reset_sessions(**_kwargs) -> dict[str, Any]:
        try:
            count = runtime.reset_sessions()
            return _ok({"cleared": count})
        except Exception as exc:  # noqa: BLE001
            return _err(f"清空会话失败: {exc}")

    async def api_reset_overrides(**_kwargs) -> dict[str, Any]:
        try:
            overrides = runtime.update_overrides({"reset_all": True})
            return _ok({"overrides": overrides})
        except Exception as exc:  # noqa: BLE001
            return _err(f"重置覆盖项失败: {exc}")

    async def api_export(**_kwargs) -> dict[str, Any]:
        try:
            return _ok(export_bundle(runtime.data_dir))
        except Exception as exc:  # noqa: BLE001
            return _err(f"导出失败: {exc}")

    async def api_import(**_kwargs) -> dict[str, Any]:
        try:
            from quart import request

            body = await _read_json(request)
            config, overrides = import_bundle(runtime.data_dir, body)
            runtime.reload()
            return _ok({"config": config, "overrides": overrides})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lazy-tools] api_import failed: %s", exc, exc_info=True)
            return _err(f"导入失败: {exc}")

    async def api_reload(**_kwargs) -> dict[str, Any]:
        try:
            runtime.reload()
            return _ok(runtime.snapshot())
        except Exception as exc:  # noqa: BLE001
            return _err(f"重新读取失败: {exc}")

    routes.extend(
        [
            (f"{prefix}/state", api_state, ["GET"], "工具组目录、覆盖项、统计与会话状态"),
            (f"{prefix}/config", api_config, ["POST"], "保存按需注入配置"),
            (f"{prefix}/overrides", api_overrides, ["POST"], "保存组/工具覆盖项"),
            (
                f"{prefix}/actions/reset_sessions",
                api_reset_sessions,
                ["POST"],
                "清空已加载的工具组会话记忆",
            ),
            (
                f"{prefix}/actions/reset_overrides",
                api_reset_overrides,
                ["POST"],
                "重置全部组/工具覆盖项",
            ),
            (f"{prefix}/export", api_export, ["GET"], "导出配置与覆盖项 JSON"),
            (f"{prefix}/import", api_import, ["POST"], "导入配置与覆盖项 JSON"),
            (f"{prefix}/reload", api_reload, ["POST"], "重新读取磁盘配置"),
        ]
    )

    for route, handler, methods, desc in routes:
        try:
            reg(route, handler, methods, desc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[lazy-tools] failed to register route %s: %s", route, exc
            )
