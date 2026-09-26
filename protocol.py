"""插件进程 ↔ 浮窗进程之间的 JSON 行协议(设计文档第 6 节「数据契约」)。

两个进程各持一份本模块:**消息形状只在这里定义**,调用方不要自己拼 dict。
一行一条消息、UTF-8、以 `\\n` 结尾;`decode` 只保证「是一个带非空 `type` 的 JSON
对象」,字段语义由分派方解释(未知 `type` 一律忽略)。
"""

from __future__ import annotations

import json
from typing import Any

# 单行上限:超过即视为协议错误(spec 第 6 节,1 MiB)
MAX_LINE_BYTES = 1_048_576


class ProtocolError(Exception):
    """行不是合法的协议消息(非法 JSON、非对象、缺 type、超长)。"""


def encode(message: dict[str, Any]) -> bytes:
    """消息 → 一行 UTF-8 字节(含结尾换行)。非 ASCII 不转义:两端都按 UTF-8 解码。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes | str) -> dict[str, Any]:
    """一行 → 消息 dict;任何不合规都抛 `ProtocolError`。

    `type` 的校验只要求「非空字符串」—— 具体有哪些类型、各自什么字段,由收方按
    自己的表决定(未知类型忽略,不在这里拒绝)。
    """
    raw = line.encode("utf-8") if isinstance(line, str) else line
    if len(raw) > MAX_LINE_BYTES:
        raise ProtocolError("line too long")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"invalid json: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")
    kind = payload.get("type")
    if not isinstance(kind, str) or not kind.strip():
        raise ProtocolError("missing type")
    return payload


# --- 插件 → 浮窗 ---


def theme_message(theme_values: dict[str, Any]) -> dict[str, Any]:
    return {"type": "theme", "theme": dict(theme_values)}


def reply_message(segments: object) -> dict[str, Any]:
    return {"type": "reply", "segments": segments}


def user_message(text: str) -> dict[str, Any]:
    return {"type": "user", "text": text}


def notice_message(text: str) -> dict[str, Any]:
    return {"type": "notice", "text": text}


def current_message(index: int | None) -> dict[str, Any]:
    return {"type": "current", "index": index}


def demo_message(segments: object) -> dict[str, Any]:
    return {"type": "demo", "segments": segments}


def hello_message(theme_values: dict[str, Any], width: int) -> dict[str, Any]:
    return {"type": "hello", "theme": dict(theme_values), "width": width}


def hide_message() -> dict[str, Any]:
    return {"type": "hide"}


def place_message(x: int, y: int, screen_id: str) -> dict[str, Any]:
    return {"type": "place", "x": int(x), "y": int(y), "screenId": screen_id}


def reset_position_message() -> dict[str, Any]:
    return {"type": "reset-position"}


def clear_message() -> dict[str, Any]:
    return {"type": "clear"}


def bye_message() -> dict[str, Any]:
    """请求浮窗退出。

    `bye` 是**双向**的:插件→浮窗是「请求退出」,浮窗退出前也会回发一条同类型的
    (方向相反,表示「我退了」)。插件侧按「未知消息类型忽略」处理后者。
    """
    return {"type": "bye"}


# --- 浮窗 → 插件 ---


def ready_message(pid: int, screens: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "ready", "pid": pid, "screens": screens}


def submit_message(text: str) -> dict[str, Any]:
    return {"type": "submit", "text": text}


def moved_message(x: int, y: int, screen_id: str) -> dict[str, Any]:
    return {"type": "moved", "x": int(x), "y": int(y), "screenId": screen_id}


def error_message(code: str, detail: str) -> dict[str, Any]:
    return {"type": "error", "code": code, "detail": detail}
