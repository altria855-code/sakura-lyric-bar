import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401
import plugin


class FakeLogger:
    """四个级别各是一个方法 —— 不能写成 `debug = info = warning = error = _record`:
    那样通过实例调用时 `self` 会被当成 `level`,而 `fields=` 是关键字参数,
    于是 `message` 缺失、直接抛 TypeError(预验证实测)。
    记录 (level, message, fields) 三元组,好让用例能断言级别与字段。"""

    def __init__(self):
        self.records = []

    def _record(self, level, message, fields=None):
        self.records.append((level, message, dict(fields or {})))

    def debug(self, message, fields=None):
        self._record("debug", message, fields)

    def info(self, message, fields=None):
        self._record("info", message, fields)

    def warning(self, message, fields=None):
        self._record("warning", message, fields)

    def error(self, message, fields=None):
        self._record("error", message, fields)


class FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.handlers = []

    def get(self):
        return dict(self.values)

    def update(self, values):
        self.values.update(values)
        return "applied"

    def replace(self, values):
        self.values = dict(values)
        return "applied"

    def on_change(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)


class FakeHost:
    def __init__(self):
        self.timeline = FakeTimeline()
        self.mobile = FakeMobile()
        self.events = []

    def emit(self, name, payload):
        self.events.append((name, payload))


class FakeTimeline:
    def __init__(self):
        self.entries = []
        self.cursor_reads = []

    def read_recent(self, request):
        return {"entries": self.entries, "cursor": "c0"}

    def read_since(self, request):
        self.cursor_reads.append(request)
        return {"entries": self.entries, "nextCursor": "c1", "hasMore": False}


class FakeMobile:
    def characters(self):
        return [{"id": "tian", "name": "天", "current": "true"}]

    def begin(self, plugin_id, character_id, text, artifact):
        return {"jobId": "j1"}

    def poll(self, plugin_id, job_id):
        # 字段名是宿主的真实字段 `status`(mobile_host.py:236-244),不是 `state` ——
        # 写成 `state` 的话,Sender 读不到 completed,发送线程会空转到 120 秒超时。
        return {"status": "completed"}

    def cancel(self, plugin_id, job_id):
        return None


class RuntimeTests(unittest.TestCase):
    def _make(self):
        with tempfile.TemporaryDirectory() as folder:
            host = FakeHost()
            logger = FakeLogger()
            config = FakeConfig({"width": 500})
            runtime = plugin.LyricBarRuntime(
                context=None, plugin_dir=folder, log=logger,
                timeline=host.timeline, mobile=host.mobile, config=config,
                overlay_link=FakeLink(), clock=lambda: 100.0,
            )
            return runtime, host, logger, config, folder

    def test_start_sends_initial_theme(self):
        runtime, _host, _logger, _config, _folder = self._make()
        runtime.start()
        self.assertTrue(runtime.overlay.sent)
        self.assertEqual(runtime.overlay.sent[0]["type"], "hello")

    def test_chat_completed_triggers_timeline_read(self):
        runtime, host, _logger, _config, _folder = self._make()
        runtime.start()
        host.timeline.entries = [
            {"entryId": "e1", "kind": "assistant", "characterId": "tian",
             "payload": {"segments": [{"text": "やあ", "translation": "嗨"}]}},
        ]
        runtime.on_host_event("sakura.host.chat.completed", {"characterId": "tian", "turnId": "t1", "cursor": "c9"})
        self.assertTrue(host.timeline.cursor_reads)
        self.assertEqual(runtime.overlay.sent[-1]["type"], "reply")

    def test_tts_started_advances_highlight(self):
        runtime, host, _logger, _config, _folder = self._make()
        runtime.start()
        host.timeline.entries = [
            {"entryId": "e1", "kind": "assistant", "characterId": "tian",
             "payload": {"segments": [{"text": "一", "translation": ""}, {"text": "二", "translation": ""}]}},
        ]
        runtime.on_host_event("sakura.host.chat.completed", {"characterId": "tian", "turnId": "t1", "cursor": "c9"})
        runtime.on_host_event("sakura.host.tts.started", {"outcome": "started"})
        self.assertEqual(runtime.overlay.sent[-1], {"type": "current", "index": 1})

    def test_overlay_submit_sends_message(self):
        runtime, _host, _logger, _config, _folder = self._make()
        runtime.start()
        runtime.handle_overlay_message({"type": "submit", "text": "早安"})
        self.assertTrue(runtime.mobile_calls)
        self.assertEqual(runtime.mobile_calls[0][2], "早安")

    def test_send_failure_pushes_notice(self):
        runtime, _host, _logger, _config, _folder = self._make()
        runtime.start()
        runtime.mobile_calls.clear()
        runtime.fail_next_send = True
        runtime.handle_overlay_message({"type": "submit", "text": "早安"})
        self.assertEqual(runtime.overlay.sent[-1]["type"], "notice")

    def test_config_change_is_hot_applied(self):
        runtime, _host, _logger, _config, _folder = self._make()
        runtime.start()
        result = runtime.handle_config_change({"fontSize": 26})
        self.assertEqual(result, "applied")
        self.assertEqual(runtime.overlay.sent[-1]["type"], "theme")
        self.assertEqual(runtime.overlay.sent[-1]["theme"]["fontSize"], 26)

    def test_position_is_persisted(self):
        runtime, _host, _logger, _config, folder = self._make()
        runtime.start()
        runtime.handle_overlay_message({"type": "moved", "x": 300, "y": 400, "screenId": "PRIMARY"})
        state = runtime.load_state()
        self.assertEqual(state["x"], 300)
        self.assertEqual(state["screenId"], "PRIMARY")

    def test_preview_action_pushes_demo(self):
        runtime, _host, _logger, _config, _folder = self._make()
        runtime.start()
        result = runtime.handle_action("preview", {})
        self.assertEqual(result, {})
        self.assertEqual(runtime.overlay.sent[-1]["type"], "demo")

    def test_reset_position_action(self):
        runtime, _host, _logger, _config, _folder = self._make()
        runtime.start()
        runtime.handle_action("resetPosition", {})
        self.assertEqual(runtime.overlay.sent[-1]["type"], "reset-position")

    def test_overlay_exit_is_logged_not_restarted(self):
        runtime, _host, logger, _config, _folder = self._make()
        runtime.start()
        runtime.handle_overlay_message({"type": "error", "code": "BOOM", "detail": "x"})
        self.assertTrue(any(level == "error" for level, _m, _f in logger.records))

    def test_logs_never_contain_user_text(self):
        runtime, _host, logger, _config, _folder = self._make()
        runtime.start()
        runtime.handle_overlay_message({"type": "submit", "text": "机密内容"})
        for _level, message, fields in logger.records:
            self.assertNotIn("机密内容", message)
            for value in fields.values():
                self.assertNotIn("机密内容", str(value))


class FakeLink:
    def __init__(self):
        self.sent = []
        self.running = True
        self.started = 0

    def start(self):
        self.started += 1
        self.running = True
        return True

    def send(self, message):
        self.sent.append(message)
        return True

    def stop(self):
        self.running = False


if __name__ == "__main__":
    unittest.main()
