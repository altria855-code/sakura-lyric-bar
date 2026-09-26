"""把时间线条目投影为浮窗需要的显示数据。纯逻辑,便于单测。"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

SENT_DEDUPE_SECONDS = 5.0


def _fingerprint(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _segments_of(entry: Mapping[str, Any]) -> list[dict[str, str]] | None:
    payload = entry.get("payload")
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("segments")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    cleaned: list[dict[str, str]] = []
    for segment in raw:
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


def _dedupe(text: str, sent_texts: Sequence[tuple[str, float]], now: float) -> bool:
    for previous, stamp in sent_texts:
        if previous == text and 0 <= now - stamp <= SENT_DEDUPE_SECONDS:
            return True
    return False


def display_from_entries(
    entries: object,
    character_id: str,
    sent_texts: Sequence[tuple[str, float]] = (),
    now: float = 0.0,
    previous_segments: object = None,
    previous_user_text: str = "",
) -> dict[str, Any]:
    """返回 {"segments", "user_text", "changed"}。"""
    segments: list[dict[str, str]] = []
    user_text = ""

    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            owner = entry.get("characterId")
            if isinstance(owner, str) and owner and owner != character_id:
                continue
            kind = entry.get("kind")
            if kind == "assistant":
                found = _segments_of(entry)
                if found is not None:
                    segments = found
            elif kind == "human":
                payload = entry.get("payload")
                if isinstance(payload, Mapping):
                    raw = payload.get("text")
                    text = raw.strip() if isinstance(raw, str) else ""
                    if text:
                        if _dedupe(text, sent_texts, now):
                            # 本地刚发过、屏幕上已经显示了 —— 本轮显式清空该轴,
                            # 而不是留着上一批的旧值(那会把陈旧文本顶回去)。
                            user_text = ""
                        else:
                            user_text = text

    # 注意:**不要**在这里回填 previous_user_text。回填看起来像"保住用户那行",
    # 实际会在去重命中时把陈旧文本顶回来:多轮轨迹 R1[H(OLD),A(R1)] → 本地发 NEW
    # → R2[H(OLD),H(NEW),A(R1)] → 若回填则 user_text=OLD,而 R3 的 changed=True
    # 会把「你:OLD」推上屏、覆盖刚发的 NEW。正确语义是:**去重命中 ⇒ 显式清空**
    # (表示"本轮不推这一轴"),调用方空文本就不推 user 消息,浮窗自然保持原样。

    changed = (
        _fingerprint(segments) != _fingerprint(previous_segments)
        or user_text != previous_user_text
    )
    return {"segments": segments, "user_text": user_text, "changed": changed}
