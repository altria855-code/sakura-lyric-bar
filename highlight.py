"""当前句推进:跟随 TTS 事件计数,或按字数估算。"""

from __future__ import annotations

from typing import Any, Sequence

MILLISECONDS_PER_CHARACTER = 130.0
SEGMENT_PAUSE_MILLISECONDS = 350.0
TTS_FALLBACK_SECONDS = 5.0


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
        self._started_count = 0
        self._segments: object = []
        self._next_advance = 0.0
        self._last_activity = 0.0
        self._saw_tts = False

    def last_activity(self) -> float:
        return self._last_activity

    def begin_turn(self, segment_count: int, now: float, segments: object = None) -> int:
        self._segments = segments if segments is not None else []
        self._count = max(0, int(segment_count))
        self._last_activity = now
        self._saw_tts = False
        self._started_count = 0
        if self.sync_mode == "off" or self._count == 0:
            self._index = -1
        else:
            self._index = 0
        self._next_advance = now + _duration_seconds(self._segments, 0)
        return self._index

    def on_tts_started(self, outcome: str, now: float) -> int | None:
        if self.sync_mode != "tts" or self._count == 0:
            return None
        if outcome != "started":
            return None
        self._last_activity = now
        self._saw_tts = True
        # 以**累计 started 次数**为准定位,而不是"在当前值上 +1"。
        # 原因:如果宿主首个 started 迟到超过 TTS_FALLBACK_SECONDS(本地 TTS 首次合成
        # 可能慢),退化路径已经推进过若干句;此时再 +1 会让高亮**永久超前**,
        # 表现为前几句从未高亮、且提前收尾(实测 4 句 + 4 个 started 只得 [2,3,None,None])。
        self._started_count += 1
        target = min(self._started_count, self._count - 1)
        if target == self._index:
            return None
        self._index = target
        return self._index

    def poll(self, now: float) -> int | None:
        if self.sync_mode == "off" or self._count == 0:
            return None
        if self.sync_mode == "timer" or (now - self._last_activity > TTS_FALLBACK_SECONDS and not self._saw_tts):
            if self._index + 1 >= self._count:
                return None
            if now < self._next_advance:
                return None
            self._index += 1
            self._next_advance = now + _duration_seconds(self._segments, self._index)
            return self._index
        return None
