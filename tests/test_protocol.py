import unittest

import _path  # noqa: F401
import protocol


class EncodeDecodeTests(unittest.TestCase):
    def test_roundtrip(self):
        payload = {"type": "theme", "theme": {"fontSize": 22}}
        self.assertEqual(protocol.decode(protocol.encode(payload)), payload)

    def test_encode_ends_with_newline_and_is_utf8(self):
        encoded = protocol.encode({"type": "user", "text": "中文"})
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertIn("中文", encoded.decode("utf-8"))

    def test_decode_rejects_non_json(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b"{not json")

    def test_decode_rejects_non_object(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b"[1, 2, 3]")

    def test_decode_rejects_missing_type(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'{"text": "hi"}')

    def test_decode_rejects_empty_type(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'{"type": "  "}')

    def test_decode_rejects_oversized_line(self):
        big = b'{"type": "user", "text": "' + b"x" * (protocol.MAX_LINE_BYTES + 1) + b'"}'
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(big)


class BuilderTests(unittest.TestCase):
    def test_current_message_accepts_none(self):
        self.assertEqual(protocol.current_message(None)["index"], None)

    def test_reply_message_keeps_unicode(self):
        message = protocol.reply_message([{"text": "こんにちは", "translation": "你好"}])
        self.assertEqual(message["type"], "reply")
        self.assertEqual(message["segments"][0]["text"], "こんにちは")

    def test_moved_message_fields(self):
        message = protocol.moved_message(10, 20, "PRIMARY")
        self.assertEqual((message["x"], message["y"], message["screenId"]), (10, 20, "PRIMARY"))

    def test_ready_message_fields(self):
        message = protocol.ready_message(1234, [{"id": "PRIMARY", "rect": [0, 0, 1920, 1080]}])
        self.assertEqual(message["type"], "ready")
        self.assertEqual(message["pid"], 1234)

    def test_error_message_fields(self):
        message = protocol.error_message("LAYOUT_FAILED", "boom")
        self.assertEqual((message["code"], message["detail"]), ("LAYOUT_FAILED", "boom"))

    def test_clear_message_has_no_payload(self):
        # 设计文档第 6.4 节:clear 只有 type,没有载荷
        self.assertEqual(protocol.clear_message(), {"type": "clear"})

    def test_bye_message_has_no_payload(self):
        # bye 双向:插件→浮窗是「请求退出」,浮窗→插件是「我退了」;两边都不带载荷
        self.assertEqual(protocol.bye_message(), {"type": "bye"})


if __name__ == "__main__":
    unittest.main()
