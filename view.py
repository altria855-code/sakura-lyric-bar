"""浮窗视图状态:台词、高亮、提示、主题。所有 setter 返回「是否需要重绘」。"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import layout
import theme as theme_module

NOTICE_TTL_SECONDS = 5.0


def _normalize_segments(segments: object) -> list[dict[str, str]]:
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        return []
    cleaned: list[dict[str, str]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        text = segment.get("text")
        translation = segment.get("translation")
        cleaned.append(
            {
                "text": text.strip() if isinstance(text, str) else "",
                "translation": translation.strip() if isinstance(translation, str) else "",
            }
        )
    return [item for item in cleaned if item["text"] or item["translation"]]


def _fingerprint(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ViewState:
    def __init__(self, theme_values: Mapping[str, Any] | None = None) -> None:
        self.theme_values = theme_module.project_theme(theme_values or {})
        self.segments: list[dict[str, str]] = []
        self.user_text = ""
        self.notice = ""
        self.notice_deadline = 0.0
        self.current_segment = -1
        self._theme_key = _fingerprint(self.theme_values)
        self._reply_key = ""

    # --- setter,返回 True 表示需要重绘 ---

    def set_theme(self, values: Mapping[str, Any] | None) -> bool:
        projected = theme_module.project_theme(values or {})
        key = _fingerprint(projected)
        if key == self._theme_key:
            return False
        self.theme_values = projected
        self._theme_key = key
        return True

    def set_reply(self, segments: object) -> bool:
        cleaned = _normalize_segments(segments)
        key = _fingerprint(cleaned)
        changed = key != self._reply_key
        self._reply_key = key
        self.segments = cleaned
        self.current_segment = 0 if cleaned else -1
        return changed

    def set_user(self, text: object) -> bool:
        value = text.strip() if isinstance(text, str) else ""
        if value == self.user_text:
            return False
        self.user_text = value
        return True

    def set_notice(self, text: object, now: float) -> bool:
        value = text.strip() if isinstance(text, str) else ""
        self.notice_deadline = now + NOTICE_TTL_SECONDS if value else 0.0
        if value == self.notice:
            return False
        self.notice = value
        return True

    def expire_notice(self, now: float) -> bool:
        if self.notice and now >= self.notice_deadline:
            self.notice = ""
            self.notice_deadline = 0.0
            return True
        return False

    def set_current(self, index: object) -> bool:
        if index is None:
            target = -1
        elif isinstance(index, bool) or not isinstance(index, int):
            return False
        elif not self.segments:
            target = -1
        else:
            target = max(0, min(index, len(self.segments) - 1))
        if target == self.current_segment:
            return False
        self.current_segment = target
        return True

    def clear(self) -> bool:
        if not self.segments and not self.user_text and not self.notice:
            return False
        self.segments = []
        self._reply_key = ""
        self.user_text = ""
        self.notice = ""
        self.notice_deadline = 0.0
        self.current_segment = -1
        return True

    # --- 派生数据 ---

    def lines(self) -> list[layout.Line]:
        return layout.build_lines(
            self.segments,
            str(self.theme_values.get("mode", "bilingual")),
            self.user_text,
            self.notice,
        )

    def boxes(self, width: int, measure: Any = None) -> layout.LayoutBoxes:
        """算布局。`measure(text, max_width) -> 高度` 由渲染方注入,用来算折行后的真实行高。"""
        return layout.compute_layout(self.lines(), self.theme_values, width, measure)

    def highlight_box(self, boxes: layout.LayoutBoxes) -> layout.LineBox | None:
        if self.current_segment < 0:
            return None
        for box in boxes.boxes:
            if box.segment == self.current_segment and box.role == "source":
                return box
        for box in boxes.boxes:
            if box.segment == self.current_segment:
                return box
        return None

    def has_content(self) -> bool:
        return bool(self.segments or self.user_text or self.notice)
