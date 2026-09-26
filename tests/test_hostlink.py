import json
import sys
import time
import unittest
from pathlib import Path

import _path  # noqa: F401
import hostlink

FAKE = Path(__file__).resolve().parent / "_fake_overlay_runner.py"


class FakeLogger:
    """记 `(级别, message, fields)` 三元组:级别与三个日志字段都要能断言。

    (不能像原来那样把 `info/warning/error` 全都别名到 `debug`、并且丢掉 `fields` ——
    那样「EOF 记 `OVERLAY_EXITED` warning」「字段只用 operation/reason_code/error_type」
    这两条验收点就完全测不到了。与 Task 13 的假 logger 保持同一种形状。)
    """

    def __init__(self):
        self.records = []

    def _record(self, level, message, fields):
        self.records.append((level, message, dict(fields or {})))

    def debug(self, message, fields=None):
        self._record("debug", message, fields)

    def info(self, message, fields=None):
        self._record("info", message, fields)

    def warning(self, message, fields=None):
        self._record("warning", message, fields)

    def error(self, message, fields=None):
        self._record("error", message, fields)


class OverlayLinkTests(unittest.TestCase):
    def _make(self):
        received = []
        logger = FakeLogger()
        link = hostlink.OverlayLink(
            plugin_dir=str(FAKE.parent),
            log=logger,
            on_message=received.append,
            script=str(FAKE),
        )
        return link, received, logger

    def test_start_reports_running(self):
        link, received, _ = self._make()
        self.addCleanup(link.stop)
        self.assertTrue(link.start())
        self.assertTrue(link.running)

    def test_ready_message_is_delivered(self):
        link, received, _ = self._make()
        self.addCleanup(link.stop)
        link.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not received:
            time.sleep(0.05)
        self.assertEqual(received[0]["type"], "ready")

    def test_send_reaches_child(self):
        link, received, _ = self._make()
        self.addCleanup(link.stop)
        link.start()
        link.send({"type": "reply", "segments": []})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(received) < 2:
            time.sleep(0.05)
        self.assertEqual(received[1], {"type": "seen", "kind": "reply"})

    def test_stop_is_idempotent(self):
        link, _received, _ = self._make()
        link.start()
        link.stop()
        link.stop()
        self.assertFalse(link.running)

    def _exits(self, logger):
        return [record for record in logger.records
                if record[2].get("reason_code") == "OVERLAY_EXITED"]

    def test_child_exit_marks_not_running(self):
        link, _received, logger = self._make()
        self.addCleanup(link.stop)
        link.start()
        link.send({"type": "bye"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and link.running:
            time.sleep(0.05)
        self.assertFalse(link.running)
        # EOF 必须留下**一条** warning 级、reason_code=OVERLAY_EXITED 的记录:
        # 这是「浮窗死了插件要知道」的唯一信号,降级成 debug 或删掉都算回归。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self._exits(logger):
            time.sleep(0.05)
        exits = self._exits(logger)
        self.assertEqual(len(exits), 1, logger.records)
        level, _message, fields = exits[0]
        self.assertEqual(level, "warning", logger.records)
        self.assertEqual(fields.get("operation"), "overlay", logger.records)

    def test_start_twice_does_not_duplicate(self):
        link, _received, _ = self._make()
        self.addCleanup(link.stop)
        self.assertTrue(link.start())
        self.assertFalse(link.start())

    def test_send_without_child_returns_false(self):
        link, _received, _ = self._make()
        self.assertFalse(link.send({"type": "reply", "segments": []}))

    def test_script_missing_reports_error(self):
        link = hostlink.OverlayLink(
            plugin_dir=str(FAKE.parent), log=FakeLogger(), on_message=lambda _m: None,
            script=str(FAKE.parent / "不存在.py"),
        )
        self.assertFalse(link.start())
        self.assertTrue(link.last_error)


if __name__ == "__main__":
    unittest.main()
