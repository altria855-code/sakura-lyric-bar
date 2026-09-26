"""配置投影:把任意用户配置变成键齐全、值合法的主题字典。两进程共用。"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

_COLOR_PATTERN = re.compile(r"[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8}")

DEFAULTS: dict[str, Any] = {
    # 显示
    "mode": "bilingual",
    "fontFamily": "Microsoft YaHei UI",
    "fontSize": 20,
    "width": 460,
    "maxLines": 10,
    "lineSpacing": 1.35,
    # 颜色与材质
    "textColor": "#FFFFFF",
    "translationColor": "#CFCFCF",
    "highlightColor": "#FFD400",
    "highlightBackground": "",
    "backgroundColor": "#0E0E12",
    "backgroundOpacity": 65,
    "overallOpacity": 100,
    "cornerRadius": 14,
    "borderWidth": 0,
    "borderColor": "#FFFFFF",
    "textShadow": True,
    "textOutlineWidth": 0,
    "textOutlineColor": "#000000",
    # 行为
    "enabled": True,
    "idleCollapse": True,
    "syncMode": "tts",
    "hideInFullscreen": True,
    "hoverDelayMs": 800,
}

_INT_RANGES: dict[str, tuple[int, int]] = {
    "fontSize": (10, 72),
    "width": (200, 1600),
    "maxLines": (2, 40),
    "backgroundOpacity": (0, 100),
    "overallOpacity": (0, 100),
    "cornerRadius": (0, 40),
    "borderWidth": (0, 4),
    "textOutlineWidth": (0, 3),
    "hoverDelayMs": (200, 3000),
}

_FLOAT_RANGES: dict[str, tuple[float, float]] = {
    "lineSpacing": (1.0, 3.0),
}

_ENUMS: dict[str, tuple[str, ...]] = {
    "mode": ("bilingual", "zh", "ja"),
    "syncMode": ("tts", "timer", "off"),
}

_COLOR_KEYS = (
    "textColor",
    "translationColor",
    "highlightColor",
    "highlightBackground",
    "backgroundColor",
    "borderColor",
    "textOutlineColor",
)

_BOOL_KEYS = ("textShadow", "idleCollapse", "enabled", "hideInFullscreen")

_TEXT_KEYS = ("fontFamily",)


def parse_color(value: object) -> tuple[int, int, int, int] | None:
    """把 `#RRGGBB` / `#AARRGGBB` 解析为 (r, g, b, a);非法返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("#"):
        return None
    digits = text[1:]
    if not _COLOR_PATTERN.fullmatch(digits):
        return None
    number = int(digits, 16)
    if len(digits) == 6:
        r, g, b = (number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF
        return (r, g, b, 255)
    a = (number >> 24) & 0xFF
    r, g, b = (number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF
    return (r, g, b, a)


def _is_finite_number(value: object) -> bool:
    # Python 整数天然有限。不能直接对 int 调 math.isfinite:整数超出 double 范围时
    # 它会抛 OverflowError(如 10**400),那会让异常从 project_theme 逃逸出去。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value)


def clamp_int(value: object, low: int, high: int, default: int) -> int:
    if not _is_finite_number(value):
        return default
    number = int(value)
    if number < low:
        return low
    if number > high:
        return high
    return number


def clamp_float(value: object, low: float, high: float, default: float) -> float:
    if not _is_finite_number(value):
        return default
    try:
        number = float(value)
    except OverflowError:
        # 只有超出 double 范围的 int 会走到这里,按符号夹到对应边界。
        return high if value > 0 else low
    if number < low:
        return low
    if number > high:
        return high
    return number


def project_theme(raw: object) -> dict[str, Any]:
    """输出键与 DEFAULTS 完全一致、值全部合法的主题字典。"""
    source: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    result: dict[str, Any] = {}

    for key, default in DEFAULTS.items():
        if key in _INT_RANGES:
            low, high = _INT_RANGES[key]
            result[key] = clamp_int(source.get(key), low, high, int(default))
        elif key in _FLOAT_RANGES:
            low, high = _FLOAT_RANGES[key]
            result[key] = clamp_float(source.get(key), low, high, float(default))
        elif key in _ENUMS:
            candidate = source.get(key)
            result[key] = candidate if candidate in _ENUMS[key] else default
        elif key in _BOOL_KEYS:
            candidate = source.get(key)
            result[key] = candidate if isinstance(candidate, bool) else default
        elif key in _COLOR_KEYS:
            # 空字符串对 highlightBackground 合法(表示不填底色)
            candidate = source.get(key)
            if key == "highlightBackground" and candidate == "":
                result[key] = ""
            elif parse_color(candidate) is None:
                result[key] = default
            else:
                result[key] = str(candidate).strip()
        elif key in _TEXT_KEYS:
            candidate = source.get(key)
            result[key] = candidate.strip() if isinstance(candidate, str) and candidate.strip() else default
        else:
            result[key] = default

    return result


def default_theme() -> dict[str, Any]:
    return project_theme({})


def resolve_color(theme_values: Mapping[str, Any], key: str) -> tuple[int, int, int, int] | None:
    """从已投影的主题里取颜色,`highlightBackground` 为空时返回 None。"""
    value = theme_values.get(key)
    if value == "":
        return None
    return parse_color(value)


def apply_overall_alpha(color: tuple[int, int, int, int], overall_opacity: int) -> tuple[int, int, int, int]:
    """整体不透明度:只缩放 alpha 通道。"""
    r, g, b, a = color
    scaled = int(round(a * (overall_opacity / 100.0)))
    return (r, g, b, max(0, min(255, scaled)))
