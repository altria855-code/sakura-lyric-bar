"""设置区块描述符与系统字体枚举。

宿主按这里的描述符渲染原生设置界面(插件不写 HTML/JS/CSS),因此字段必须落在宿主的
校验范围内 —— 逐条对齐 `core/app/core_host/plugin_host_services.py`:

- 字段类型只能是 string/password/boolean/integer/number/select/readonly/status/resource;
- `placement: "section_header"` 只能给 status 类型;status 字段不得携带
  options/minimum/maximum/step/maxLength/actionIds;
- **每个字段的 `default` 必须是该字段类型的合法值**(宿主 `_settings_field` 末尾会校验):
  select 的默认值必须落在 `options` 里,status 的默认值必须是
  `{"state", "label", "message"}` 完整结构、`label` 1..120 字符、`message` ≤ 240 字符;
  任一字段不合法 → 该字段被丢掉且整个区块被标成 `SETTINGS_DESCRIPTOR_INVALID`。
  所以本模块的 `_status_value()` 会把调用方给的状态归一化到这些边界内,
  字体选项也会保证默认字体一定在列表里。
- 字段键唯一;select 选项 ≤ 64;整个区块 ≤ 32 字段、≤ 15 Action;
  `status` 的取值只能是 neutral/ready/working/warning/error。

本任务范围内的取舍(与 brief 的差异)见 `.superpowers/sdd/2026-09-25-sakura-lyric-bar/task-12-report.md`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import theme

try:  # 字体枚举只在 Windows 有意义;非 Windows 上导入本模块不应崩。
    import winreg
except ImportError:  # pragma: no cover - 仅非 Windows 环境
    winreg = None  # type: ignore[assignment]


SECTION_ID = "bar"
SECTION_TITLE = "字幕栏"

# 描述符里的 actions 与这里的顺序一一对应;Task 13 必须为每个 id 注册同名回调。
ACTION_IDS: tuple[str, ...] = ("preview", "resetPosition", "restartOverlay")

OPTION_LIMIT = 64
FONT_LIMIT = 64

_STATUS_STATES = ("neutral", "ready", "working", "warning", "error")
_STATUS_LABEL_LIMIT = 120
_STATUS_MESSAGE_LIMIT = 240
_STATUS_LABEL_FALLBACK = "—"

_COLOR_HINT = "支持 #RRGGBB 或 #AARRGGBB"

# 字段顺序即渲染顺序:3 个分组标题,然后显示组、颜色与材质组、行为组。
_SECTION_HEADERS: tuple[tuple[str, str], ...] = (
    ("headerDisplay", "显示"),
    ("headerAppearance", "颜色与材质"),
    ("headerBehavior", "行为"),
)

_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("bilingual", "双语"),
    ("zh", "中文"),
    ("ja", "日文"),
)

_SYNC_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("tts", "跟随语音"),
    ("timer", "按字数估算"),
    ("off", "不高亮"),
)

_ACTION_LABELS: dict[str, str] = {
    "preview": "预览效果",
    "resetPosition": "回到默认位置",
    "restartOverlay": "重新启动浮窗",
}

# 仅浮窗未运行时可用。注意:宿主 `_settings_action` 会把未知属性(含 enabledWhen)过滤掉,
# 详见报告。
_ACTION_ENABLED_WHEN: dict[str, dict[str, str]] = {
    "restartOverlay": {"field": "overlayStatus", "equals": "未运行"},
}


# --------------------------------------------------------------------------- #
# 字体枚举
# --------------------------------------------------------------------------- #

_FONTS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_FONT_SUBSTITUTES_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\FontSubstitutes"

# 注册表值名形如 "Microsoft YaHei UI (TrueType)";去掉尾部的圆括号后缀即字体名。
_FONT_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")

# 注册表常把同一份字体文件的多个家族名并成一条值名(如
# "Microsoft YaHei & Microsoft YaHei UI (TrueType)")。GDI 的 lfFaceName 是**单一**名字
# 且不认 ` & ` 分隔符 —— 传合并名与传乱填名会得到同一个回落字体(本机是宋体),
# 于是「用户选了字体、界面显示已改、画面却不变」。必须拆开分别产出。
_FONT_NAME_SEPARATOR = " & "

# 优先展示的字体,元组顺序即优先级。命中越靠前越优先。
_FONT_PRIORITY: tuple[str, ...] = (
    "YaHei",
    "雅黑",
    "SimSun",
    "宋体",
    "SimHei",
    "黑体",
    "Meiryo",
    "Yu Gothic",
    "MS Gothic",
    "Noto",
    "Source Han",
    "等线",
    "KaiTi",
    "楷体",
    "微软",
)


def _iter_value_names(hive: int, path: str) -> Iterable[str]:
    """注册表某个键下的**值名**;键不存在或不可读时安静地什么都不产出。"""
    if winreg is None:
        return
    try:
        key = winreg.OpenKey(hive, path)
    except OSError:
        return
    with key:
        index = 0
        while True:
            try:
                name, _value, _kind = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            yield name


def _iter_subkey_names(hive: int, path: str) -> Iterable[str]:
    """注册表某个键下的**子键名**;键不存在或不可读时安静地什么都不产出。"""
    if winreg is None:
        return
    try:
        key = winreg.OpenKey(hive, path)
    except OSError:
        return
    with key:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            yield name


def _iter_font_names() -> Iterable[str]:
    """Fonts 键的值名 + FontSubstitutes 的键名(本机与当前用户两个 hive)。"""
    if winreg is None:
        return
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        yield from _iter_value_names(hive, _FONTS_KEY)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        yield from _iter_subkey_names(hive, _FONT_SUBSTITUTES_KEY)


def _font_names(raw: object) -> list[str]:
    """把注册表里的原始名字变成字体名:去尾部后缀,再把合并名按 ` & ` 拆开。

    一条值名可能产出多个名字(含 ` & ` 的合并名),调用方应逐个纳入。
    """
    if not isinstance(raw, str):
        return []
    cleaned = _FONT_SUFFIX.sub("", raw).strip()
    if not cleaned:
        return []
    pieces = (piece.strip() for piece in cleaned.split(_FONT_NAME_SEPARATOR))
    return [piece for piece in pieces if piece]


def _font_sort_key(name: str) -> tuple[int, str, str]:
    lowered = name.lower()
    for index, marker in enumerate(_FONT_PRIORITY):
        if marker.lower() in lowered:
            return (index, lowered, name)
    return (len(_FONT_PRIORITY), lowered, name)


def enumerate_fonts(limit: int = FONT_LIMIT) -> list[str]:
    """系统注册表里的字体名(合并名已拆成单个名字),中/日文字体优先,截断到 `limit`。"""
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = FONT_LIMIT
    if size <= 0:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for raw in _iter_font_names():
        for name in _font_names(raw):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    names.sort(key=_font_sort_key)
    return names[:size]


# --------------------------------------------------------------------------- #
# 字段构造
# --------------------------------------------------------------------------- #


def _status_value(status: object, fallback_label: str = _STATUS_LABEL_FALLBACK) -> dict[str, str]:
    """归一化成一个宿主一定接受的 status 值(恰好三个键,长度也在边界内)。"""
    raw: Mapping[str, Any] = status if isinstance(status, Mapping) else {}
    state = raw.get("state")
    if state not in _STATUS_STATES:
        state = "neutral"
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        label = fallback_label
    message = raw.get("message")
    if not isinstance(message, str):
        message = ""
    return {
        "state": state,
        "label": label.strip()[:_STATUS_LABEL_LIMIT],
        "message": message[:_STATUS_MESSAGE_LIMIT],
    }


def _header_field(key: str, title: str) -> dict[str, Any]:
    """分组标题:`placement: "section_header"` 的 status 字段,值就是该标题。"""
    return {
        "key": key,
        "type": "status",
        "label": title,
        "placement": "section_header",
        # 宿主只认 `default`(它自己会在快照里补出 `value`),所以标题结构挂在这里。
        "default": _status_value({"state": "neutral", "label": title, "message": ""}, title),
    }


def _status_field(key: str, label: str, status: object) -> dict[str, Any]:
    return {
        "key": key,
        "type": "status",
        "label": label,
        "default": _status_value(status, label),
    }


def _boolean_field(key: str, label: str, *, description: str | None = None) -> dict[str, Any]:
    field: dict[str, Any] = {
        "key": key,
        "type": "boolean",
        "label": label,
        "default": theme.DEFAULTS[key],
    }
    if description is not None:
        field["description"] = description
    return field


def _integer_field(
    key: str,
    label: str,
    minimum: int,
    maximum: int,
    *,
    description: str | None = None,
    placement: str | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "key": key,
        "type": "integer",
        "label": label,
        "default": theme.DEFAULTS[key],
        "minimum": minimum,
        "maximum": maximum,
        "step": 1,
    }
    if description is not None:
        field["description"] = description
    if placement is not None:
        field["placement"] = placement
    return field


def _number_field(
    key: str,
    label: str,
    minimum: float,
    maximum: float,
    step: float,
    *,
    description: str | None = None,
    placement: str | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "key": key,
        "type": "number",
        "label": label,
        "default": theme.DEFAULTS[key],
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
    }
    if description is not None:
        field["description"] = description
    if placement is not None:
        field["placement"] = placement
    return field


def _color_field(key: str, label: str, *, description: str | None = None) -> dict[str, Any]:
    """宿主没有取色器 —— 颜色就是手填的字符串,合法写法写进 description。"""
    hint = _COLOR_HINT if description is None else f"{description};{_COLOR_HINT}"
    return {
        "key": key,
        "type": "string",
        "label": label,
        "default": theme.DEFAULTS[key],
        "description": hint,
    }


def _option_items(options: Sequence[tuple[str, str]] | Sequence[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for option in options:
        if isinstance(option, str):
            items.append({"value": option, "label": option})
        else:
            value, label = option
            items.append({"value": value, "label": label})
    return items


def _select_field(
    key: str,
    label: str,
    options: Sequence[dict[str, str]],
    *,
    description: str | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "key": key,
        "type": "select",
        "label": label,
        "default": theme.DEFAULTS[key],
        "options": list(options),
    }
    if description is not None:
        field["description"] = description
    return field


def _font_options(fonts: object, current_font: object) -> list[dict[str, str]]:
    """字体下拉选项:当前值排首位,其次主题默认值,再是枚举结果;去重后截断到 64。

    主题默认值必须留在列表里 —— 宿主要求 select 的 `default` 落在 `options` 内,
    否则该字段会被宿主丢掉,整个区块还会被标成 SETTINGS_DESCRIPTOR_INVALID
    (枚举失败、或系统没装「Microsoft YaHei UI」时就会这样)。

    每个候选都过一遍 `_font_names`:合并名在这里也要拆开。`enumerate_fonts` 已经拆过,
    但 `build_descriptor` 的 `fonts` 由调用方给,这是用户**最后**一道闸 ——
    合并名一旦漏进选项,GDI 会把它当乱填名回落到默认字体(静默失效)。
    """
    names: list[str] = []
    seen: set[str] = set()
    candidates: list[object] = [current_font, theme.DEFAULTS.get("fontFamily")]
    if isinstance(fonts, Iterable) and not isinstance(fonts, (str, bytes)):
        candidates.extend(fonts)
    for candidate in candidates:
        if len(names) >= OPTION_LIMIT:
            break
        for name in _font_names(candidate):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= OPTION_LIMIT:
                break
    return _option_items(names)


def _build_actions() -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for action_id in ACTION_IDS:
        action: dict[str, Any] = {"actionId": action_id, "label": _ACTION_LABELS[action_id]}
        condition = _ACTION_ENABLED_WHEN.get(action_id)
        if condition is not None:
            action["enabledWhen"] = dict(condition)
        actions.append(action)
    return actions


# --------------------------------------------------------------------------- #
# 描述符
# --------------------------------------------------------------------------- #


def build_descriptor(
    fonts: Sequence[str],
    overlay_status: Mapping[str, Any],
    position_status: Mapping[str, Any],
    current_font: str | None = None,
) -> dict[str, Any]:
    """构造设置区块描述符;字段顺序即宿主渲染顺序。"""
    fields: list[dict[str, Any]] = [
        _header_field(key, title) for key, title in _SECTION_HEADERS
    ]

    # 显示
    fields.append(
        _select_field("mode", "显示模式", _option_items(_MODE_OPTIONS))
    )
    fields.append(
        _select_field(
            "fontFamily",
            "字体",
            _font_options(fonts, current_font),
            description="字体名来自系统注册表,中/日文字体排在前面;个别名称可能无法实际渲染",
        )
    )
    fields.append(_integer_field("fontSize", "字号", 10, 72))
    fields.append(_integer_field("width", "栏宽", 200, 1600))
    fields.append(
        _integer_field(
            "maxLines",
            "最多显示行数",
            2,
            40,
            description="按渲染行计,双语模式下每句占两行",
        )
    )
    fields.append(
        _number_field("lineSpacing", "行距", 1.0, 3.0, 0.05, placement="advanced")
    )

    # 颜色与材质
    fields.append(_color_field("textColor", "原文颜色"))
    fields.append(_color_field("translationColor", "译文颜色"))
    fields.append(_color_field("highlightColor", "当前句文字颜色"))
    fields.append(
        _color_field("highlightBackground", "当前句底色", description="留空表示不填底色")
    )
    fields.append(_color_field("backgroundColor", "背景色"))
    fields.append(_integer_field("backgroundOpacity", "背景不透明度", 0, 100))
    fields.append(_integer_field("overallOpacity", "整体不透明度", 0, 100))
    fields.append(_integer_field("cornerRadius", "圆角半径", 0, 40))
    fields.append(_integer_field("borderWidth", "边框粗细", 0, 4))
    fields.append(_color_field("borderColor", "边框颜色"))
    fields.append(_boolean_field("textShadow", "文字阴影"))
    fields.append(_integer_field("textOutlineWidth", "文字描边粗细", 0, 3))
    fields.append(_color_field("textOutlineColor", "文字描边颜色"))

    # 行为
    fields.append(_boolean_field("enabled", "显示浮窗"))
    fields.append(_boolean_field("idleCollapse", "没内容时收起成小条"))
    fields.append(
        _select_field("syncMode", "高亮跟随方式", _option_items(_SYNC_MODE_OPTIONS))
    )
    fields.append(_boolean_field("hideInFullscreen", "全屏应用时自动隐藏"))
    fields.append(
        _integer_field("hoverDelayMs", "鼠标移开后收起延时", 200, 3000, placement="advanced")
    )
    fields.append(_status_field("overlayStatus", "浮窗状态", overlay_status))
    fields.append(_status_field("positionStatus", "当前位置", position_status))

    return {
        "sectionId": SECTION_ID,
        "title": SECTION_TITLE,
        "fields": fields,
        "actions": _build_actions(),
    }
