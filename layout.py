"""台词 → 渲染行 → 栏内布局。纯计算,不触碰窗口。

对外只有纯函数:输入台词片段 / 主题 / 栏宽,输出渲染行与盒模型。
与 `theme.project_theme` 同样的姿态:运行期遇到坏数据(超范围的数值、NaN、
超大整数、非 Mapping 的主题)一律回落到默认值,绝不抛异常。
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Line = tuple[str, str, int]

VALID_MODES = ("bilingual", "zh", "ja")

_PADDING_X = 16
_NOTICE_HEIGHT_RATIO = 0.8

# 主题字段坏掉时的回落值(与 theme.DEFAULTS 保持一致)
_FALLBACK_FONT_SIZE = 20
_FALLBACK_LINE_SPACING = 1.35
_FALLBACK_MAX_LINES = 10
_FALLBACK_WIDTH = 460

# 合法区间:坏值可以很离谱,但不能让布局算出非物理尺寸(顺带挡住浮点溢出)
_FONT_SIZE_RANGE = (1, 512)
_LINE_SPACING_RANGE = (0.0, 100.0)
_MAX_LINES_RANGE = (1, 1000)
_WIDTH_RANGE = (1, 100_000)


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finite_number(value: object) -> float | None:
    """把 int/float 规整为有限 float;其它(NaN、±inf、超大整数、非数字)返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # 只有超出 double 范围的 int 会走到这里(如 10**400),不能让它逃逸
        return None
    return number if math.isfinite(number) else None


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    number = _finite_number(value)
    if number is None:
        return default
    return max(low, min(int(number), high))


def _clamp_float(value: object, default: float, low: float, high: float) -> float:
    number = _finite_number(value)
    if number is None:
        return default
    return max(low, min(number, high))


def build_lines(
    segments: object,
    mode: str,
    user_text: object = "",
    notice: object = "",
) -> list[Line]:
    """把台词片段与提示文本投影为渲染行。"""
    if mode not in VALID_MODES:
        mode = "bilingual"

    lines: list[Line] = []

    spoken = _clean(user_text)
    if spoken:
        lines.append((spoken, "user", -1))

    if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes)):
        for index, segment in enumerate(segments):
            if not isinstance(segment, Mapping):
                continue
            source = _clean(segment.get("text"))
            translation = _clean(segment.get("translation"))
            if mode in ("bilingual", "ja") and source:
                lines.append((source, "source", index))
            if mode in ("bilingual", "zh") and translation:
                lines.append((translation, "translation", index))

    hint = _clean(notice)
    if hint:
        lines.append((hint, "notice", -1))

    return lines


def _iter_lines(lines: object) -> Iterator[Line]:
    """只吐出形状合法的行(三元组、文本为 str);坏行跳过,不把非法值传下去。"""
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
        return
    for entry in lines:
        if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes)) or len(entry) != 3:
            continue
        text, role, segment = entry
        if not isinstance(text, str):
            continue
        if not isinstance(role, str):
            role = "source"
        if isinstance(segment, bool) or not isinstance(segment, int):
            segment = -1
        yield text, role, segment


@dataclass(frozen=True)
class LineBox:
    text: str
    role: str
    segment: int
    top: int
    height: int


@dataclass(frozen=True)
class LayoutBoxes:
    boxes: list[LineBox]
    line_height: int
    content_height: int
    view_height: int
    max_scroll: int
    wrap_width: int


def compute_layout(lines: list[Line], theme_values: Mapping[str, Any], width: int) -> LayoutBoxes:
    """把渲染行堆叠成盒模型,并算出视口高度与最大滚动量。

    `top` 从 0 起算、不含任何纵向留白 —— 视口高度就是纯文本高度,
    栏外留白由窗口层自己加(见 Task 5 的 `view_height + 40`)。
    """
    values = theme_values if isinstance(theme_values, Mapping) else {}

    font_size = _clamp_int(values.get("fontSize"), _FALLBACK_FONT_SIZE, *_FONT_SIZE_RANGE)
    spacing = _clamp_float(values.get("lineSpacing"), _FALLBACK_LINE_SPACING, *_LINE_SPACING_RANGE)
    max_lines = _clamp_int(values.get("maxLines"), _FALLBACK_MAX_LINES, *_MAX_LINES_RANGE)
    bar_width = _clamp_int(width, _FALLBACK_WIDTH, *_WIDTH_RANGE)

    # font_size ≤ 512 且 spacing ≤ 100,乘积不会溢出
    line_height = max(1, int(round(font_size * spacing)))
    wrap_width = max(1, bar_width - _PADDING_X * 2)

    boxes: list[LineBox] = []
    top = 0
    for text, role, segment in _iter_lines(lines):
        height = line_height
        if role == "notice":
            height = max(1, int(round(line_height * _NOTICE_HEIGHT_RATIO)))
        boxes.append(LineBox(text=text, role=role, segment=segment, top=top, height=height))
        top += height

    content_height = top if boxes else line_height
    view_height = max(min(content_height, line_height * max_lines), line_height)
    max_scroll = max(0, content_height - view_height)

    return LayoutBoxes(
        boxes=boxes,
        line_height=line_height,
        content_height=content_height,
        view_height=view_height,
        max_scroll=max_scroll,
        wrap_width=wrap_width,
    )


def follow_offset(layout: LayoutBoxes, segment_index: int, previous_offset: int) -> int:
    """返回让指定句完整可见的滚动偏移;内容放得下时恒为 0。"""
    if layout.max_scroll <= 0 or not layout.boxes:
        return 0

    offset = _clamp_int(previous_offset, 0, 0, layout.max_scroll)

    targets = [box for box in layout.boxes if box.segment == segment_index]
    if not targets:
        return offset

    first = min(box.top for box in targets)
    last = max(box.top + box.height for box in targets)

    if first - offset < 0:
        offset = first
    elif last - offset > layout.view_height:
        offset = last - layout.view_height
    return max(0, min(offset, layout.max_scroll))


def text_top_in_view(layout: LayoutBoxes, box: LineBox, offset: int) -> int:
    return box.top - offset
