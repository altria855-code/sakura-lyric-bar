"""按宿主段落身份高亮；仅 timer 模式按字数估算。"""

from __future__ import annotations

from typing import Any, Sequence

MILLISECONDS_PER_CHARACTER = 130.0
SEGMENT_PAUSE_MILLISECONDS = 350.0


def _text_length(segments: object, index: int) -> int:
    if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes)):
        if 0 <= index < len(segments):
            segment = segments[index]
            if isinstance(segment, dict):
                text = segment.get("text")
                if isinstance(text, str) and text:
                    return len(text)
    return 1


def _duration_seconds(segments: object, index: int) -> float:
    length = _text_length(segments, index)
    return (length * MILLISECONDS_PER_CHARACTER + SEGMENT_PAUSE_MILLISECONDS) / 1000.0


class HighlightTracker:
    def __init__(self, sync_mode: str) -> None:
        self.sync_mode = sync_mode if sync_mode in ("tts", "timer", "off") else "tts"
        self._count = 0
        self._index = -1
        self._segments: object = []
        self._next_advance = 0.0
        self._last_activity = 0.0

    def last_activity(self) -> float:
        return self._last_activity

    def begin_turn(self, segment_count: int, now: float, segments: object = None) -> int:
        self._segments = segments if segments is not None else []
        self._count = max(0, int(segment_count))
        self._last_activity = now
        if self.sync_mode != "timer" or self._count == 0:
            self._index = -1
        else:
            self._index = 0
        self._next_advance = now + _duration_seconds(self._segments, 0)
        return self._index

    def on_tts_started(self, index: int, now: float) -> int | None:
        if self.sync_mode != "tts" or type(index) is not int or not 0 <= index < self._count:
            return None
        self._last_activity = now
        if index == self._index:
            return None
        self._index = index
        return index

    def on_tts_ended(self) -> int | None:
        if self.sync_mode != "tts" or self._index == -1:
            return None
        self._index = -1
        return -1

    def poll(self, now: float) -> int | None:
        if self.sync_mode != "timer" or self._count == 0:
            return None
        if self.sync_mode == "timer":
            if self._index + 1 >= self._count:
                return None
            if now < self._next_advance:
                return None
            self._index += 1
            self._next_advance = now + _duration_seconds(self._segments, self._index)
            return self._index
        return None
