import unittest

import _path  # noqa: F401
import view


SEGMENTS = [
    {"text": "第一句", "translation": "第一句译文"},
    {"text": "第二句", "translation": "第二句译文"},
]


class ViewStateTests(unittest.TestCase):
    def test_initial_state_is_empty(self):
        state = view.ViewState({})
        self.assertEqual(state.segments, [])
        self.assertEqual(state.current_segment, -1)
        self.assertEqual(state.lines(), [])

    def test_set_reply_stores_segments_and_resets_highlight(self):
        state = view.ViewState({})
        state.set_current(3)
        self.assertTrue(state.set_reply(SEGMENTS))
        self.assertEqual(len(state.segments), 2)
        self.assertEqual(state.current_segment, 0)

    def test_set_reply_with_same_payload_is_not_a_change(self):
        state = view.ViewState({})
        state.set_reply(SEGMENTS)
        self.assertFalse(state.set_reply([dict(item) for item in SEGMENTS]))

    def test_malformed_segments_filtered(self):
        state = view.ViewState({})
        state.set_reply(["x", {"text": "ok"}])
        self.assertEqual(len(state.segments), 1)

    def test_set_current_clamps_to_range(self):
        state = view.ViewState({})
        state.set_reply(SEGMENTS)
        self.assertTrue(state.set_current(99))
        self.assertEqual(state.current_segment, 1)
        self.assertFalse(state.set_current(1))

    def test_set_current_none_clears_highlight(self):
        state = view.ViewState({})
        state.set_reply(SEGMENTS)
        self.assertTrue(state.set_current(None))
        self.assertEqual(state.current_segment, -1)

    def test_notice_expires(self):
        state = view.ViewState({})
        state.set_notice("还在回复中", now=100.0)
        self.assertEqual(state.notice, "还在回复中")
        self.assertFalse(state.expire_notice(now=101.0))
        self.assertTrue(state.expire_notice(now=106.0))
        self.assertEqual(state.notice, "")

    def test_clear_removes_everything(self):
        state = view.ViewState({})
        state.set_reply(SEGMENTS)
        state.set_user("你好")
        state.set_notice("提示", now=0.0)
        self.assertTrue(state.clear())
        self.assertEqual(state.lines(), [])

    def test_theme_change_reports_change(self):
        state = view.ViewState({})
        self.assertTrue(state.set_theme({"fontSize": 30}))
        self.assertFalse(state.set_theme({"fontSize": 30}))

    def test_highlight_box_points_at_current_segment(self):
        state = view.ViewState({})
        state.set_reply(SEGMENTS)
        state.set_current(1)
        boxes = state.boxes(460)
        highlight = state.highlight_box(boxes)
        self.assertIsNotNone(highlight)
        self.assertEqual(highlight.segment, 1)

    def test_user_line_precedes_conversation(self):
        state = view.ViewState({"mode": "ja"})
        state.set_user("我发的")
        state.set_reply(SEGMENTS)
        lines = state.lines()
        self.assertEqual(lines[0][1], "user")
        self.assertEqual(lines[1][0], "第一句")


if __name__ == "__main__":
    unittest.main()
