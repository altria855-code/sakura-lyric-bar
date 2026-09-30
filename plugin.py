"""字幕栏插件入口:把前面各部件接到 Sakura 的 `setup(context)` 上。

本模块是**唯一的接线层** —— 注册设置区块、拉起浮窗、订阅宿主事件、推进高亮、持久化位置。
它只用标准库,不导入 `app.*`:宿主能力一律经 `context.get("sakura.host.xxx")` 取。

两个已知的"会卡住调用线程"的地方,这里都做了处置(细节见各自方法):

- `sender.Sender.send()` 最长同步等 120 秒(`SEND_TIMEOUT_SECONDS`)—— **绝不能**跑在浮窗的
  消息读线程上(那会把浮窗的 stdout 憋住、冻住它的 UI),所以发送走独立线程;
- `hostlink.OverlayLink.send()` 在浮窗不读管道时会**一直卡住并握着写锁**(Windows 匿名管道
  约 4KB 就满),于是所有后续发送一起排队 —— 所以本模块自己加了一层**有界写**
  (唯一写线程 + 超时放弃),宿主回调线程最多被拖住 `WRITE_TIMEOUT_SECONDS`。

日志纪律:只写自编短消息 + 稳定码 + 异常类型名;不写台词、不写用户输入、不写 `str(error)`。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import highlight
import hostlink
import protocol
import sender as sender_module
import settings_schema
import theme
import timeline_source

# 插件 ID(plugin.yaml 的 id)。宿主有 `context.plugin_id` 时以它为准。
PLUGIN_ID = "local.sakura.lyric-bar"

STATE_FILE_NAME = "state.json"
TIMELINE_LIMIT = 50  # 一次读取的条数上限(宿主允许 1–500)
TIMELINE_PAGE_LIMIT = 5  # read_since 回报 hasMore 时最多续读几轮(防宿主一直回 True)
POLL_INTERVAL_SECONDS = 1.0  # 高亮兜底推进的周期
POLL_JOIN_TIMEOUT_SECONDS = 1.0  # 收尾时等推进线程的上限(有界清理)
SEND_JOIN_TIMEOUT_SECONDS = 1.0  # 收尾时等发送线程的上限
WRITE_TIMEOUT_SECONDS = 0.5  # 等写线程把一条消息送出去的上限(见 `_send`)
OUTBOX_LIMIT = 32  # 待发消息的队列上限,满了就丢这一条并记日志

# 发送失败时给浮窗的提示文案(设计文档 §6.3):「通道忙」与「等太久」都是"角色还在回复中"
BUSY_NOTICE = "还在回复中"
FAILED_NOTICE = "发送失败"
NO_CHARACTER_NOTICE = "没有可用角色"
BUSY_CODES = ("CHAT_EXECUTION_LIMIT_EXCEEDED", "SEND_TIMEOUT")

# 设置页「预览效果」用的内置示例台词(不改任何持久状态)
DEMO_SEGMENTS = (
    {"text": "こんにちは、いい天気ですね。", "translation": "你好呀,天气真不错呢。"},
    {"text": "今日はどこかへ出かけますか?", "translation": "今天要出门去哪里吗?"},
    {"text": "わたしも一緒に行っていい?", "translation": "我也可以一起去吗?"},
)


class _NullLog:
    """宿主日志服务缺失时的兜底:调用点不必到处判空。"""

    def debug(self, message: object, fields: object = None) -> None:
        pass

    def info(self, message: object, fields: object = None) -> None:
        pass

    def warning(self, message: object, fields: object = None) -> None:
        pass

    def error(self, message: object, fields: object = None) -> None:
        pass


class _Write:
    """一条待写消息:由唯一的写线程送出,调用方只等它的完成信号。"""

    __slots__ = ("message", "done", "ok", "abandoned")

    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.done = threading.Event()
        self.ok = False
        self.abandoned = False


def _as_int(value: object) -> int | None:
    """只认真正的整数(bool 不算):协议里的坐标来自 JSON 整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_entries(result: object) -> list[Any]:
    """服务返回值里的 entries 列表;形状不对就当空批。"""
    if not isinstance(result, dict):
        return []
    entries = result.get("entries")
    return list(entries) if isinstance(entries, list) else []


class LyricBarRuntime:
    """插件的运行时:事件、高亮、发送、位置持久化都在这里(便于单测:依赖全部可注入)。"""

    def __init__(
        self,
        context: Any,
        plugin_dir: str,
        log: Any,
        timeline: Any = None,
        conversation: Any = None,
        config: Any = None,
        overlay_link: Any = None,
        clock: Any = None,
        character: Any = None,
        sender: Any = None,
        plugin_id: str | None = None,
    ) -> None:
        self.context = context
        self.plugin_dir = str(plugin_dir)
        self.log = log if log is not None else _NullLog()
        self.clock = clock if callable(clock) else time.monotonic

        host_plugin_id = getattr(context, "plugin_id", None)
        self.plugin_id = (
            host_plugin_id if isinstance(host_plugin_id, str) and host_plugin_id
            else (plugin_id if isinstance(plugin_id, str) and plugin_id else PLUGIN_ID)
        )

        self.timeline = timeline if timeline is not None else self._service("sakura.host.timeline")
        self.conversation = conversation if conversation is not None else self._service("sakura.host.conversation")
        self.character = character if character is not None else self._service("sakura.host.character")
        self.config = config if config is not None else getattr(context, "config", None)
        # 名字必须是 `overlay`:用例按 `runtime.overlay.sent` 访问它(brief Step 3 点名)。
        self.overlay = overlay_link if overlay_link is not None else hostlink.OverlayLink(
            self.plugin_dir, self.log, on_message=self.handle_overlay_message
        )
        # 发送器用它自己的单调钟算 120 秒超时:注入的 clock 在测试里可能是冻结的,
        # 拿它当超时基准会让等待永远不过期。
        self.sender = sender if sender is not None else (
            sender_module.Sender(self.conversation, self.log)
            if self.conversation is not None else None
        )

        self.theme: dict[str, Any] = theme.default_theme()
        self.highlight = highlight.HighlightTracker(self.theme["syncMode"])
        self.state: dict[str, Any] = {}
        self.last_cursor = ""
        # 上一轮的台词轴。空列表(不是 None):首批若没有 assistant 条目时 `[] != None`
        # 会成立,于是推一条空 `reply` + `current: None` **把屏幕清掉** —— 而契约 2 说得很清楚,
        # 这种批次「根本无内容可推」。
        self.segments: list[Any] = []
        self.user_text = ""  # 上一轮的用户轴
        self.character_id = ""
        self.entry_id = ""
        self.segment_indices: dict[int, int] = {}
        self._playback_id = ""
        self._retired_playbacks: set[str] = set()
        self._event_lock = threading.RLock()
        self.sent_texts: list[tuple[str, float]] = []

        # 供测试使用的两个成员(正式路径上只在下面这些地方被读/追加,不改变行为)
        self.conversation_calls: list[tuple[str, str]] = []
        self.fail_next_send = False

        self._closed = False
        self._outbox: queue.Queue = queue.Queue(OUTBOX_LIMIT)
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()
        self._send_threads: list[threading.Thread] = []
        self._send_threads_lock = threading.Lock()

    # --- 生命周期 ---

    def start(self) -> None:
        """拉起浮窗并同步初始状态(首次 `hello` 由本方法发,`OverlayLink` 只做传输)。"""
        self.state = self.load_state()
        self.theme = theme.project_theme(self._config_values())
        self._replace_tracker(self.theme.get("syncMode"))
        self._refresh_character()
        self._init_cursor()
        if not self._start_overlay():
            # 浮窗起不来插件仍然 active:用户可以在设置页点「重新启动浮窗」
            self.log.error(
                "浮窗启动失败",
                fields={"operation": "overlay_start", "reason_code": "OVERLAY_SPAWN_FAILED"},
            )
        self._send(protocol.hello_message(self.theme, self.theme["width"]))
        self._send_position()
        self.log.info("字幕栏已启动")

    def stop(self) -> None:
        """收尾(由 `context.effect` 登记):幂等、有界 —— 关浮窗、停发送器、等线程。"""
        if self._closed:
            return
        self._closed = True
        self._cancel_send()
        self._join_send_threads()
        self._stop_writer()
        try:
            self.overlay.stop()  # 幂等:没 start 过 / 重复调用都安全
        except Exception as error:  # noqa: BLE001 — 收尾路径不能抛
            self.log.warning(
                "停止浮窗失败",
                fields={"operation": "overlay_stop", "reason_code": "OVERLAY_STOP_FAILED",
                        "error_type": type(error).__name__},
            )

    def tick(self) -> None:
        """后台检查角色变化，并推进显式选择的 timer 模式。"""
        with self._event_lock:
            if self._closed:
                return
            self._refresh_character()
            self._advance(self.highlight.poll(self.clock()))

    # --- 宿主事件 ---

    def on_host_event(self, name: str, payload: dict) -> None:
        """宿主回调和定时推进共享同一份显示状态。"""
        with self._event_lock:
            if self._closed:
                return
            data = payload if isinstance(payload, dict) else {}
            try:
                self._refresh_character()
                if name == "sakura.host.chat.completed":
                    self._on_chat_completed(data)
                elif name in ("sakura.host.tts.started", "sakura.host.tts.ended"):
                    self._on_playback(data)
            except Exception as error:
                self.log.error(
                    "处理宿主事件失败",
                    fields={"operation": "host_event", "reason_code": "HOST_EVENT_FAILED",
                            "error_type": type(error).__name__},
                )

    def _show_reply(self, entry_id: str, segments: list, indices: dict[int, int]) -> None:
        self.entry_id = entry_id
        self.segments = segments
        self.segment_indices = indices
        self._send(protocol.reply_message(segments))
        self._advance(self.highlight.begin_turn(len(segments), self.clock(), segments))

    def _retire_playback(self) -> None:
        if self._playback_id:
            self._retired_playbacks.add(self._playback_id)
            self._playback_id = ""
        self._advance(self.highlight.on_tts_ended())

    def _on_playback(self, data: dict) -> None:
        playback_id = _as_text(data.get("playbackId"))
        if not playback_id:
            return
        outcome = data.get("outcome")
        if outcome in ("finished", "stopped", "failed"):
            self._retired_playbacks.add(playback_id)
            if playback_id == self._playback_id:
                self._retire_playback()
            return
        if outcome != "started" or playback_id in self._retired_playbacks or playback_id == self._playback_id:
            return
        character_id = _as_text(data.get("characterId"))
        entry_id = _as_text(data.get("historyEntryId"))
        index = _as_int(data.get("segmentIndex"))
        if not character_id or character_id != self.character_id or not entry_id or index is None or index < 0:
            return
        self._retire_playback()
        if entry_id != self.entry_id:
            result = self._invoke_timeline("get_entry", {"entryId": entry_id})
            entry = result.get("entry") if isinstance(result, dict) else None
            # 查询绑定当前角色；查询期间发生切换时也不能把旧内容送到浮窗。
            self._refresh_character()
            if self._closed or character_id != self.character_id or not isinstance(entry, dict):
                return
            if (entry.get("entryId") != entry_id or entry.get("characterId") != character_id
                    or entry.get("kind") != "assistant"):
                return
            segments, indices = timeline_source.assistant_display(entry)
            if index not in indices:
                return
            self._show_reply(entry_id, segments, indices)
        visible_index = self.segment_indices.get(index)
        if visible_index is None:
            return
        self._playback_id = playback_id
        self._advance(self.highlight.on_tts_started(visible_index, self.clock()))

    def _on_chat_completed(self, payload: dict) -> None:
        if _as_text(payload.get("characterId")) != self.character_id:
            return
        entries = self._read_timeline(payload.get("cursor"))
        self._refresh_character()
        if self._closed or _as_text(payload.get("characterId")) != self.character_id:
            return
        display = timeline_source.display_from_entries(
            entries, self.character_id, self.sent_texts, self.clock(),
            previous_segments=self.segments, previous_user_text=self.user_text,
        )
        entry_id = display["entry_id"]
        if entry_id and (entry_id != self.entry_id or display["segments"] != self.segments):
            self._retire_playback()
            self._show_reply(entry_id, display["segments"], display["segment_indices"])
        user_text = display["user_text"]
        if user_text and user_text != self.user_text:
            self._send(protocol.user_message(user_text))
        self.user_text = user_text

    def _advance(self, index: object) -> None:
        """推 `current`。`None` 一律不推(那是"没变化",不是"清零")。

        `-1` 必须转成 `None` 再推:`view.set_current(-1)` 在 `segments` 非空时会被**夹到 0**
        (显示成"第 1 句高亮"而不是"不高亮"),只有 `None` 才是真正的清零。

        历史段落可以重新朗读，索引允许回到先前的位置。
        """
        if not isinstance(index, int) or isinstance(index, bool):
            return
        self._send(protocol.current_message(index if index >= 0 else None))

    # --- 浮窗消息 ---

    def handle_overlay_message(self, message: dict) -> None:
        """浮窗消息入口(**在浮窗的消息读线程上**):这里不许有任何阻塞等待。"""
        if self._closed or not isinstance(message, dict):
            return
        kind = message.get("type")
        try:
            if kind == "submit":
                self._handle_submit(message.get("text"))
            elif kind == "moved":
                self._handle_moved(message)
            elif kind == "error":
                code = _as_text(message.get("code"))
                self.log.error(
                    "浮窗报告错误",
                    fields={"operation": "overlay", "reason_code": code or "OVERLAY_ERROR"},
                )
            elif kind == "ready":
                self.log.info("浮窗已就绪")
                self._send(protocol.theme_message(self.theme))
                self._send_position()
            # bye(浮窗退出前的道别)与未知类型一样:忽略
        except Exception as error:  # noqa: BLE001 — 读线程不能被处理器的异常带走
            self.log.error(
                "处理浮窗消息失败",
                fields={"operation": "overlay", "reason_code": "OVERLAY_HANDLER_FAILED",
                        "error_type": type(error).__name__},
            )

    def _handle_submit(self, text: object) -> None:
        value = _as_text(text)
        if not value:
            return  # 空文本忽略
        character_id = self.character_id
        if not character_id:
            self.log.warning(
                "没有可用角色",
                fields={"operation": "send_message", "reason_code": "CHARACTER_UNAVAILABLE"},
            )
            self._send(protocol.notice_message(NO_CHARACTER_NOTICE))
            return
        # 测试台账:在**决定发送**时记下(不等发送线程真跑起来),它记的是"这批发送请求"
        self.conversation_calls.append((character_id, value))
        if self.fail_next_send:
            # 测试钩子:直接当作发送失败并复位。同步处理,免得用例要等线程。
            self.fail_next_send = False
            self._finish_send({"ok": False, "code": "SEND_FAILED"}, value)
            return
        if self.sender is None:
            self.log.error(
                "发送消息失败",
                fields={"operation": "send_message", "reason_code": "SEND_FAILED"},
            )
            self._finish_send({"ok": False, "code": "SEND_FAILED"}, value)
            return
        self._spawn_send(character_id, value)

    def _spawn_send(self, character_id: str, text: str) -> None:
        """把 `Sender.send()` 放到独立线程:它最长同步等 120 秒,而本方法跑在浮窗的消息读
        线程上 —— 同步跑会把浮窗发来的消息全排在那 120 秒后面,积压超过约 4KB 的管道缓冲
        就把浮窗 UI 冻住(它卡在写 stdout 上)。发送结果拿到后再推 `user`/`notice`。"""
        def run() -> None:
            try:
                result = self.sender.send(character_id, text)
            except Exception as error:  # noqa: BLE001 — 发送线程只负责收尾,不许漏异常
                result = {"ok": False, "code": "SEND_FAILED"}
                self.log.error(
                    "发送消息失败",
                    fields={"operation": "send_message", "reason_code": "SEND_FAILED",
                            "error_type": type(error).__name__},
                )
            self._finish_send(result, text, character_id)

        thread = threading.Thread(target=run, name="lyric-bar-send", daemon=True)
        with self._send_threads_lock:
            self._send_threads = [item for item in self._send_threads if item.is_alive()]
            self._send_threads.append(thread)
        thread.start()

    def _finish_send(self, result: object, text: str, character_id: str | None = None) -> None:
        if self._closed:
            return
        self._refresh_character()
        if self._closed or (character_id is not None and character_id != self.character_id):
            return
        if isinstance(result, dict) and result.get("ok"):
            now = self.clock()
            self.sent_texts.append((text, now))
            # 只留去重窗口内的记录:timeline_source 只认最近 5 秒,老记录只会越攒越多
            self.sent_texts[:] = [
                item for item in self.sent_texts if 0 <= now - item[1] <= timeline_source.SENT_DEDUPE_SECONDS
            ]
            self._send(protocol.user_message(text))
            return
        code = result.get("code") if isinstance(result, dict) else ""
        code = code if isinstance(code, str) and code else "SEND_FAILED"
        # 失败由 `sender` 自己记日志(同一任务只记一次),这里只管给浮窗的文案
        self._send(protocol.notice_message(BUSY_NOTICE if code in BUSY_CODES else FAILED_NOTICE))

    def _handle_moved(self, message: dict) -> None:
        x = _as_int(message.get("x"))
        y = _as_int(message.get("y"))
        if x is None or y is None:
            return
        self.state["x"] = x
        self.state["y"] = y
        screen_id = _as_text(message.get("screenId"))
        if screen_id:
            self.state["screenId"] = screen_id
        self.save_state(self.state)

    # --- 配置 ---

    def handle_config_change(self, values: dict) -> str:
        """应用一份配置(`settings.register(save=...)` 的持久化回调会**回调**到这里,
        `context.config.on_change` 也接着这里)。

        注意本方法**不写配置**:宿主 `PluginConfig.update()` 是把 `_applied_config` 置 None
        之后才调 on_change 处理器的,处理器里再调一次 `update()` 就是无限递归
        (最终 RecursionError → 保存被标成 error)。持久化由 `save_config` 负责。
        """
        try:
            self._apply_theme(values)
        except Exception as error:  # noqa: BLE001 — 设置回调不许把异常抛给宿主
            self.log.error(
                "应用配置失败",
                fields={"operation": "config_apply", "reason_code": "CONFIG_APPLY_FAILED",
                        "error_type": type(error).__name__},
            )
            return "error"
        return "applied"

    def save_config(self, values: dict) -> str:
        """设置页的保存回调:`config.update()` 负责写盘**并回调 on_change**(= 本类的
        `handle_config_change`),所以主题是在那条回调里、带着合并后的**完整配置**应用的
        (而不是这份表单草稿的子集 —— 用它直接 project 会把没提交的字段打回默认值)。
        """
        update = getattr(self.config, "update", None)
        if not callable(update):
            # 没有配置服务(非宿主环境):至少让界面上的改动生效
            self.log.warning(
                "配置服务不可用",
                fields={"operation": "config_save", "reason_code": "CONFIG_UNAVAILABLE"},
            )
            return self.handle_config_change(values)
        try:
            state = update(dict(values) if isinstance(values, dict) else {})
        except Exception as error:  # noqa: BLE001
            self.log.error(
                "保存配置失败",
                fields={"operation": "config_save", "reason_code": "CONFIG_SAVE_FAILED",
                        "error_type": type(error).__name__},
            )
            return "error"
        return state if state in ("applied", "restart_required", "error") else "applied"

    def _apply_theme(self, values: object) -> None:
        self.theme = theme.project_theme(values)
        if self._replace_tracker(self.theme.get("syncMode")):
            self._advance(self.highlight.begin_turn(len(self.segments), self.clock(), self.segments))
        self._send(protocol.theme_message(self.theme))  # 主题永远最后推(设置页按它热应用)
        if not self.theme.get("enabled", True):
            self._send(protocol.hide_message())

    def _replace_tracker(self, sync_mode: object) -> bool:
        """高亮跟随方式可以热改(`HighlightTracker.sync_mode` 是只读的,换一个实例)。"""
        mode = sync_mode if sync_mode in ("tts", "timer", "off") else "tts"
        if self.highlight.sync_mode == mode:
            return False
        self.highlight = highlight.HighlightTracker(mode)
        return True

    # --- 设置区块 ---

    def load_settings(self) -> dict:
        """设置页的读取回调:`config.get()` + 两个只读状态字段(load 不该改业务状态)。"""
        values = self._config_values()
        values["overlayStatus"] = self.overlay_status()
        values["positionStatus"] = self.position_status()
        return values

    def overlay_status(self) -> dict:
        """浮窗状态(宿主校验:`{state,label,message}` 三键齐全、label 1..120、message ≤240)。"""
        running = bool(getattr(self.overlay, "running", False))
        message = "" if running else _as_text(getattr(self.overlay, "last_error", ""))[:200]
        return {
            "state": "ready" if running else "neutral",
            "label": "运行中" if running else "未运行",
            "message": message,
        }

    def position_status(self) -> dict:
        x = _as_int(self.state.get("x"))
        y = _as_int(self.state.get("y"))
        if x is None or y is None:
            return {"state": "neutral", "label": "默认位置", "message": ""}
        screen_id = _as_text(self.state.get("screenId")) or "未知屏幕"
        return {"state": "ready", "label": f"{screen_id} ({x}, {y})", "message": ""}

    # --- 设置页动作 ---

    def handle_action(self, action_id: str, values: dict) -> dict:
        try:
            if action_id == "preview":
                # 与真实回复走同一条装载路径(浮窗侧 demo/reply 共用)
                self._send(protocol.demo_message(DEMO_SEGMENTS))
                return {}
            if action_id == "resetPosition":
                for key in ("x", "y", "screenId"):
                    self.state.pop(key, None)
                self.save_state(self.state)
                self._send(protocol.reset_position_message())
                return {}
            if action_id == "restartOverlay":
                return self._restart_overlay()
        except Exception as error:  # noqa: BLE001 — Action 回调不许把异常抛给宿主
            self.log.error(
                "设置动作失败",
                fields={"operation": "settings_action", "reason_code": "SETTINGS_ACTION_FAILED",
                        "error_type": type(error).__name__},
            )
            return {"message": "操作失败"}
        return {}  # 未知动作

    def _restart_overlay(self) -> dict:
        """描述符里的 `enabledWhen: overlayStatus == 未运行` 被宿主静默丢弃(Task 12 评审),
        所以「未运行才可点」只能自己判:在运行就是重启,没在运行就是启动。

        重启出来的是**新进程**:必须重新握手(hello + 位置),否则它会停在无主题的初始画面。

        两点注记(评审已知,行为不改):
        - **最坏情形会超过宿主的 action 预算**:这条 Action 恰恰是给「浮窗卡死」准备的,而那时
          `overlay.stop()` 自己是收尾路径(0.2 秒取写锁 + 0.5 秒关流 + 2 秒等待 + 2 秒进程树),
          单独就可能吃掉 3.0 秒的预算。后果只是状态回来得晚(宿主那边表现成一次迟到的
          action 结果),不是泄漏:新起的浮窗靠 stdin EOF 跟着插件进程一起退。
        - **与并发的 `stop()` 相撞会误报**:插件正在停用时 `stop()` 因为已经置位会立刻返回,
          于是这里可能把浮窗又拉起来一次并回报「浮窗已重启」—— 同上是"迟到的状态":插件进程
          一退,新浮窗的 stdin 就 EOF,它自己会退,宿主的进程树清理也会收掉后代。
        """
        was_running = bool(getattr(self.overlay, "running", False))
        try:
            self.overlay.stop()
        except Exception as error:  # noqa: BLE001
            self.log.warning(
                "停止浮窗失败",
                fields={"operation": "overlay_stop", "reason_code": "OVERLAY_STOP_FAILED",
                        "error_type": type(error).__name__},
            )
        if not self._start_overlay():
            return {"message": "浮窗启动失败"}
        self._send(protocol.hello_message(self.theme, self.theme["width"]))
        self._send_position()
        return {"message": "浮窗已重启" if was_running else "浮窗已启动"}

    # --- 状态持久化 ---

    def load_state(self) -> dict:
        """位置状态:先读盘;读不到(还没有数据目录 / 测试环境 / 文件坏了)就用内存里那份。

        内存回退不是凑数:`save_state` 写不进去时也不能把这次移动丢掉,调用方该看到这些坐标。
        """
        stored = self._read_state_file()
        if stored is None:
            return dict(self.state)
        self.state = dict(stored)
        return dict(stored)

    def save_state(self, state: dict) -> None:
        data = dict(state) if isinstance(state, dict) else {}
        self.state = data
        path = self._state_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as error:
            self.log.warning(
                "保存位置失败",
                fields={"operation": "save_state", "reason_code": "STATE_WRITE_FAILED",
                        "error_type": type(error).__name__},
            )

    def _state_path(self) -> Path | None:
        data_path = getattr(self.context, "data_path", None)
        if callable(data_path):
            try:
                return Path(data_path(STATE_FILE_NAME))
            except Exception as error:  # noqa: BLE001 — 服务缺失/拒绝,退回插件目录
                self.log.warning(
                    "取插件数据目录失败",
                    fields={"operation": "state_path", "reason_code": "DATA_PATH_UNAVAILABLE",
                            "error_type": type(error).__name__},
                )
        return Path(self.plugin_dir) / STATE_FILE_NAME if self.plugin_dir else None

    def _read_state_file(self) -> dict | None:
        path = self._state_path()
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None  # 还没有这个文件是最常见的情形,不算异常
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if not isinstance(value, dict):
            self.log.warning(
                "位置记录不可用",
                fields={"operation": "load_state", "reason_code": "STATE_INVALID"},
            )
            return None
        return value

    # --- 时间线 ---

    def _init_cursor(self) -> None:
        """启动时把 cursor 定到"当前最新",不重放历史。

        为什么必须有这一步:`chat.completed` 带的 cursor 是 `timeline.latest_cursor()`
        (=这一轮自己最后一条),而 `read_since` 是 `seq >` 的**排他**读取 —— 直接拿事件
        cursor 去读会读不到这一轮,首条回复就不上屏了。先读一次 recent 拿到"此刻最新",
        下一轮事件的 read_since 才正好覆盖到它。
        """
        if self.last_cursor:
            return
        result = self._invoke_timeline("read_recent", {"limit": 1})
        if isinstance(result, dict):
            self.last_cursor = _as_text(result.get("cursor"))

    def _read_timeline(self, cursor: object) -> list[Any]:
        """读一段新条目:优先 `read_since`(从上次的 cursor 之后),失败就退回 `read_recent`。

        `hasMore` 必须处理:宿主取满 limit 条就置 True,而 SQL 是正序 `ORDER BY seq LIMIT
        limit+1` ⇒ **超上限时被丢的是最新条目**,这一页的 entries[-1] 不是真正的最新;
        不续读就会把**较旧**的文本当最新推上屏。按 nextCursor 接着往后读(最多
        `TIMELINE_PAGE_LIMIT` 轮,cursor 不前进就停,防死循环)。
        """
        start = self.last_cursor or _as_text(cursor)
        result = self._invoke_timeline("read_since", {"cursor": start, "limit": TIMELINE_LIMIT}) if start else None
        if not isinstance(result, dict):
            return self._read_recent()
        entries = _as_entries(result)
        request_cursor = start
        next_cursor = _as_text(result.get("nextCursor")) or start
        pages = 1
        while result.get("hasMore") and pages < TIMELINE_PAGE_LIMIT and next_cursor != request_cursor:
            request_cursor = next_cursor
            result = self._invoke_timeline("read_since", {"cursor": request_cursor, "limit": TIMELINE_LIMIT})
            if not isinstance(result, dict):
                break  # 续读失败:留着已经读到的,别再放大问题
            entries.extend(_as_entries(result))
            next_cursor = _as_text(result.get("nextCursor")) or request_cursor
            pages += 1
        self.last_cursor = next_cursor
        return entries

    def _read_recent(self) -> list[Any]:
        result = self._invoke_timeline("read_recent", {"limit": TIMELINE_LIMIT})
        if not isinstance(result, dict):
            return []
        self.last_cursor = _as_text(result.get("cursor")) or self.last_cursor
        return _as_entries(result)

    def _invoke_timeline(self, method: str, request: dict) -> object:
        """调一次时间线服务;任何失败只记日志(宿主回调有时间预算,这里不能抛)。"""
        call = getattr(self.timeline, method, None) if self.timeline is not None else None
        if not callable(call):
            return None
        try:
            return call(dict(request))
        except Exception as error:  # noqa: BLE001
            code = getattr(error, "code", None)
            self.log.warning(
                "读取时间线失败",
                fields={"operation": "timeline_read",
                        "reason_code": code if isinstance(code, str) and code else "TIMELINE_READ_FAILED",
                        "error_type": type(error).__name__},
            )
            return None

    # --- 角色 ---

    def _refresh_character(self) -> None:
        with self._event_lock:
            self._refresh_character_locked()

    def _refresh_character_locked(self) -> None:
        """角色身份只从宿主读取，事件不能把当前角色改回旧角色。"""
        current_call = getattr(self.character, "current", None)
        try:
            current = current_call() if callable(current_call) else None
        except Exception as error:
            self.log.warning(
                "读取当前角色失败",
                fields={"operation": "character_lookup", "reason_code": "CHARACTER_READ_FAILED",
                        "error_type": type(error).__name__},
            )
            current = None
        character_id = _as_text(current.get("id")) if isinstance(current, dict) else ""
        if character_id == self.character_id:
            return
        previous = self.character_id
        self.character_id = character_id
        self.last_cursor = ""
        self._retire_playback()
        self.entry_id = ""
        self.segment_indices = {}
        self.segments = []
        self.user_text = ""
        self.sent_texts.clear()
        self.highlight.begin_turn(0, self.clock())
        if previous:
            self._cancel_send()
            self._send(protocol.reply_message([]))
            self._send(protocol.user_message(""))

    # --- 宿主服务 ---

    def _service(self, key: str) -> object:
        getter = getattr(self.context, "get", None)
        if not callable(getter):
            return None
        try:
            return getter(key)
        except Exception as error:  # noqa: BLE001 — 服务缺失不该让插件装不起来
            self.log.warning(
                "宿主服务不可用",
                fields={"operation": "service_lookup", "reason_code": "HOST_SERVICE_UNAVAILABLE",
                        "error_type": type(error).__name__},
            )
            return None

    def _config_values(self) -> dict:
        getter = getattr(self.config, "get", None)
        if not callable(getter):
            return {}
        try:
            values = getter()
        except Exception as error:  # noqa: BLE001
            self.log.warning(
                "读取配置失败",
                fields={"operation": "config_read", "reason_code": "CONFIG_READ_FAILED",
                        "error_type": type(error).__name__},
            )
            return {}
        return dict(values) if isinstance(values, dict) else {}

    # --- 写浮窗(有界) ---

    def _send(self, message: dict) -> bool:
        """有界地把一条消息交给浮窗。

        为什么不直接 `self.overlay.send(message)`:写管道时若浮窗不读(Windows 匿名管道约
        4KB 就满),那次写会**一直卡住并握着写锁**,于是后续每条消息都排队 —— 整条通道静默
        停摆。而本方法的调用方是宿主回调线程(事件回调、设置回调、Action)与浮窗的读线程,
        都不能无界等待。做法:交给唯一的写线程,自己最多等 `WRITE_TIMEOUT_SECONDS`;
        超时就把这条作废并返回 False。**返回 True 的含义仍是「已经写出去」**(不是"已入队")。
        """
        if self._closed:
            return False
        self._ensure_writer()
        request = _Write(message)
        try:
            self._outbox.put_nowait(request)
        except queue.Full:
            self.log.warning(
                "浮窗消息积压,丢弃一条",
                fields={"operation": "overlay_send", "reason_code": "OVERLAY_SEND_BACKLOG"},
            )
            return False
        if request.done.wait(WRITE_TIMEOUT_SECONDS):
            return bool(request.ok)
        request.abandoned = True
        self.log.warning(
            "写浮窗超时",
            fields={"operation": "overlay_send", "reason_code": "OVERLAY_WRITE_TIMEOUT"},
        )
        return False

    def _ensure_writer(self) -> None:
        if self._writer is not None:
            return
        with self._writer_lock:
            if self._writer is None:
                thread = threading.Thread(target=self._write_loop, name="lyric-bar-write", daemon=True)
                self._writer = thread
                thread.start()

    def _write_loop(self) -> None:
        """唯一的写线程:保证消息按序成行地送出去(与 `OverlayLink` 的写锁配合)。"""
        while True:
            request = self._outbox.get()
            if request is None:
                return  # 收工哨兵
            if not request.abandoned:
                try:
                    request.ok = bool(self.overlay.send(request.message))
                except Exception as error:  # noqa: BLE001 — 长驻线程,什么都别带走它
                    request.ok = False
                    self.log.warning(
                        "写给浮窗失败",
                        fields={"operation": "overlay_send", "reason_code": "OVERLAY_WRITE_FAILED",
                                "error_type": type(error).__name__},
                    )
            request.done.set()

    def _stop_writer(self) -> None:
        writer = self._writer
        if writer is None:
            return
        self._writer = None
        try:
            self._outbox.put_nowait(None)  # 排在已入队的消息之后:先写完再收工
        except queue.Full:
            return  # 写线程正卡在那次写里(daemon,不拦进程退出)
        writer.join(SEND_JOIN_TIMEOUT_SECONDS)

    def _cancel_send(self) -> None:
        if self.sender is None:
            return
        try:
            self.sender.cancel_current()
        except Exception as error:  # noqa: BLE001
            self.log.debug(
                "取消发送任务失败",
                fields={"operation": "cancel_send", "reason_code": "SEND_CANCEL_FAILED",
                        "error_type": type(error).__name__},
            )

    def _join_send_threads(self) -> None:
        with self._send_threads_lock:
            threads = list(self._send_threads)
            self._send_threads.clear()
        for thread in threads:
            thread.join(SEND_JOIN_TIMEOUT_SECONDS)  # 有界:等不到就交给 daemon 属性

    def _start_overlay(self) -> bool:
        """拉起浮窗。`start()` **不带参数** —— 传输层不主动发消息,首条 hello 由 `start()` 发。"""
        try:
            started = bool(self.overlay.start())
        except Exception as error:  # noqa: BLE001
            self.log.error(
                "浮窗启动失败",
                fields={"operation": "overlay_start", "reason_code": "OVERLAY_SPAWN_FAILED",
                        "error_type": type(error).__name__},
            )
            return False
        # 已经在运行也会返回 False:那不是失败,别记到日志里
        return started or bool(getattr(self.overlay, "running", False))

    def _send_position(self) -> None:
        x = _as_int(self.state.get("x"))
        y = _as_int(self.state.get("y"))
        if x is None or y is None:
            return  # 没记过位置:浮窗按默认位置摆放(主屏下方居中)
        self._send(protocol.place_message(x, y, _as_text(self.state.get("screenId"))))


class LyricBarPlugin:
    """插件入口(plugin.yaml 的 `entry: plugin:LyricBarPlugin`)。"""

    def setup(self, context: Any) -> None:
        plugin_dir = str(Path(__file__).resolve().parent)
        log = context.get("sakura.host.logging")

        runtime = LyricBarRuntime(context, plugin_dir, log)
        runtime.start()
        context.effect(runtime.stop)

        settings = context.get("sakura.host.settings")
        if settings is not None:
            descriptor = settings_schema.build_descriptor(
                settings_schema.enumerate_fonts(),
                runtime.overlay_status(),
                runtime.position_status(),
                # 必须传拆分后的真实字体名:合并名(带 ` & `)不在 select 的选项里,
                # 宿主会判 SETTINGS_VALUE_INVALID 并把该字段重置为默认字体(Task 12 评审)
                current_font=runtime.theme.get("fontFamily"),
            )
            actions = {
                action_id: (lambda values, _id=action_id: runtime.handle_action(_id, values))
                for action_id in settings_schema.ACTION_IDS
            }
            settings.register(
                descriptor,
                load=runtime.load_settings,
                save=runtime.save_config,
                actions=actions,
            )

        for name in ("sakura.host.chat.completed", "sakura.host.tts.started", "sakura.host.tts.ended"):
            context.on(name, _event_handler(runtime, name))
        context.config.on_change(runtime.handle_config_change)

        # 检查角色变化并推进 timer 模式。
        stop_event = threading.Event()

        def poll_loop() -> None:
            while not stop_event.wait(POLL_INTERVAL_SECONDS):
                runtime.tick()

        poll_thread = threading.Thread(target=poll_loop, name="lyric-bar-poll", daemon=True)
        poll_thread.start()

        def stop_polling() -> None:
            stop_event.set()
            poll_thread.join(POLL_JOIN_TIMEOUT_SECONDS)  # 有界

        context.effect(stop_polling)


def _event_handler(runtime: LyricBarRuntime, name: str):
    """宿主事件处理器:SDK 只传一个 payload 参数,事件名用闭包带进来。"""

    def handle(payload: object = None) -> None:
        runtime.on_host_event(name, payload)

    return handle
