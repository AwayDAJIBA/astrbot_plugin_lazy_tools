"""配置与覆盖项（overrides）的读写。

所有可调参数都存在插件自己的数据目录里：

- ``config.json``    —— 注入开关、常驻组、上限、自动预加载等
- ``overrides.json`` —— 组说明 / 工具说明 / 工具归组 / 隐藏 等颗粒度覆盖

AstrBot 的插件配置 schema 只保留一个 ``enabled`` 总开关，其余都在插件页
（``pages/dashboard``）里编辑；这样参数变更不需要重启或重载插件。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"
OVERRIDES_FILENAME = "overrides.json"

CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "lazy",  # lazy | full
    "pin_groups": ["files", "shell", "messaging"],
    "pin_tools": [],
    "max_active_groups": 8,
    "max_active_tools": 24,
    "min_tools_for_lazy": 12,
    "auto_load": False,
    "auto_load_max_groups": 1,
    "session_ttl_seconds": 0,
    "debug_log": True,
}

OVERRIDES_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "groups": {},  # group_id -> {"name": str, "description": str, "hidden": bool}
    "tools": {},  # tool_name -> {"group": str, "description": str, "hidden": bool}
    "group_order": [],  # 可选：页面排序用
}


def _deep_merge(base: Any, override: Any) -> Any:
    """把 override 深度合并进 base 的副本（dict 递归，list/scalar 直接替换）。"""
    if isinstance(base, dict):
        out = copy.deepcopy(base)
        if not isinstance(override, dict):
            return out
        for key, value in override.items():
            if key in out and isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json(path: Path, fallback: Any) -> Any:
    try:
        if not path.is_file():
            return copy.deepcopy(fallback)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return copy.deepcopy(fallback)


def load_config(data_dir: Path) -> dict[str, Any]:
    raw = read_json(data_dir / CONFIG_FILENAME, {})
    if not isinstance(raw, dict):
        raw = {}
    return normalize_config(_deep_merge(CONFIG_DEFAULTS, raw))


def save_config(data_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(config)
    _atomic_write_json(data_dir / CONFIG_FILENAME, normalized)
    return normalized


def normalize_config(raw: Any) -> dict[str, Any]:
    """把任意来源的配置收敛到已知字段与合法范围。"""
    merged = _deep_merge(CONFIG_DEFAULTS, raw if isinstance(raw, dict) else {})

    def as_int(key: str, low: int, high: int) -> int:
        try:
            value = int(merged.get(key, CONFIG_DEFAULTS[key]))
        except (TypeError, ValueError):
            value = int(CONFIG_DEFAULTS[key])
        return max(low, min(high, value))

    def as_bool(key: str) -> bool:
        value = merged.get(key, CONFIG_DEFAULTS[key])
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "off", "no", "disabled"}
        return bool(value)

    def as_str_list(key: str) -> list[str]:
        value = merged.get(key, CONFIG_DEFAULTS[key])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            value = []
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out

    mode = str(merged.get("mode", "lazy") or "lazy").strip().lower()
    if mode not in {"lazy", "full"}:
        mode = "lazy"
    return {
        "enabled": as_bool("enabled"),
        "mode": mode,
        "pin_groups": as_str_list("pin_groups"),
        "pin_tools": as_str_list("pin_tools"),
        "max_active_groups": as_int("max_active_groups", 0, 40),
        "max_active_tools": as_int("max_active_tools", 0, 200),
        "min_tools_for_lazy": as_int("min_tools_for_lazy", 0, 500),
        "auto_load": as_bool("auto_load"),
        "auto_load_max_groups": as_int("auto_load_max_groups", 0, 8),
        "session_ttl_seconds": as_int("session_ttl_seconds", 0, 30 * 24 * 3600),
        "debug_log": as_bool("debug_log"),
    }


def load_overrides(data_dir: Path) -> dict[str, Any]:
    raw = read_json(data_dir / OVERRIDES_FILENAME, {})
    if not isinstance(raw, dict):
        raw = {}
    return normalize_overrides(_deep_merge(OVERRIDES_DEFAULTS, raw))


def save_overrides(data_dir: Path, overrides: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_overrides(overrides)
    normalized["updated_at"] = time.time()
    _atomic_write_json(data_dir / OVERRIDES_FILENAME, normalized)
    return normalized


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(line.strip() for line in value.strip().splitlines() if line.strip())
    return text[:limit]


def normalize_overrides(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    groups: dict[str, dict[str, Any]] = {}
    raw_groups = source.get("groups")
    if isinstance(raw_groups, dict):
        for group_id, item in raw_groups.items():
            gid = str(group_id).strip()
            if not gid or not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {}
            name = _clean_text(item.get("name"), 80)
            description = _clean_text(item.get("description"), 500)
            if name:
                entry["name"] = name
            if description:
                entry["description"] = description
            if item.get("hidden"):
                entry["hidden"] = True
            if entry:
                groups[gid] = entry

    tools: dict[str, dict[str, Any]] = {}
    raw_tools = source.get("tools")
    if isinstance(raw_tools, dict):
        for tool_name, item in raw_tools.items():
            name = str(tool_name).strip()
            if not name or not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {}
            group = _clean_text(item.get("group"), 80)
            description = _clean_text(item.get("description"), 1000)
            if group:
                entry["group"] = group
            if description:
                entry["description"] = description
            if item.get("hidden"):
                entry["hidden"] = True
            if entry:
                tools[name] = entry

    order: list[str] = []
    raw_order = source.get("group_order")
    if isinstance(raw_order, list):
        for item in raw_order:
            text = str(item).strip()
            if text and text not in order:
                order.append(text)

    out: dict[str, Any] = {
        "version": 1,
        "groups": groups,
        "tools": tools,
        "group_order": order,
    }
    updated_at = source.get("updated_at")
    if isinstance(updated_at, (int, float)):
        out["updated_at"] = float(updated_at)
    return out


def export_bundle(data_dir: Path) -> dict[str, Any]:
    return {
        "config": load_config(data_dir),
        "overrides": load_overrides(data_dir),
        "exported_at": time.time(),
    }


def import_bundle(data_dir: Path, payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("导入内容必须是 JSON 对象")
    config = normalize_config(payload.get("config", {}))
    overrides = normalize_overrides(payload.get("overrides", {}))
    save_config(data_dir, config)
    save_overrides(data_dir, overrides)
    return config, overrides
