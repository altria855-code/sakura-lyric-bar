import unittest

import _path  # noqa: F401
import highlight

SEGMENTS = [{"text": f"第{i}句", "translation": ""} for i in range(4)]


class TtsModeTests(unittest.TestCase):
    def test_begins_at_first_segment(self):
        tracker = highlight.HighlightTracker("tts")
        self.assertEqual(tracker.begin_turn(4, 0.0, SEGMENTS), 0)

    def test_each_started_advances_one(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        self.assertEqual(tracker.on_tts_started("started", 1.0), 1)
        self.assertEqual(tracker.on_tts_started("started", 2.0), 2)

    def test_advance_stops_at_last_segment(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(2, 0.0, SEGMENTS)
        tracker.on_tts_started("started", 1.0)
        self.assertIsNone(tracker.on_tts_started("started", 2.0))
        self.assertIsNone(tracker.on_tts_started("started", 3.0))

    def test_stopped_does_not_advance(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        self.assertIsNone(tracker.on_tts_started("stopped", 1.0))

    def test_failed_does_not_advance(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        self.assertIsNone(tracker.on_tts_started("failed", 1.0))

    def test_empty_turn_has_no_highlight(self):
        tracker = highlight.HighlightTracker("tts")
        self.assertEqual(tracker.begin_turn(0, 0.0, []), -1)

    def test_begin_turn_resets_progress(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        tracker.on_tts_started("started", 1.0)
        self.assertEqual(tracker.begin_turn(4, 5.0, SEGMENTS), 0)


class TimerModeTests(unittest.TestCase):
    def test_poll_advances_after_estimated_duration(self):
        tracker = highlight.HighlightTracker("timer")
        tracker.begin_turn(2, 0.0, [{"text": "一二三", "translation": ""}, {"text": "四五六", "translation": ""}])
        self.assertIsNone(tracker.poll(0.1))
        advanced = tracker.poll(2.0)
        self.assertEqual(advanced, 1)

    def test_poll_is_idempotent_without_new_time(self):
        tracker = highlight.HighlightTracker("timer")
        tracker.begin_turn(2, 0.0, [{"text": "一二三", "translation": ""}, {"text": "四五六", "translation": ""}])
        tracker.poll(2.0)
        self.assertIsNone(tracker.poll(2.0))


class TurnResetTests(unittest.TestCase):
    def test_second_turn_starts_from_the_first_segment(self):
        # `begin_turn` 必须重置累计计数 —— 缺这条重置时,第 2 回合的**第一个**
        # started 会把高亮直接跳到末句(实测 4 句场景得 3)。这是承重逻辑,不是清理。
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        for now in (0.5, 1.5, 2.5):
            tracker.on_tts_started("started", now)
        self.assertEqual(tracker.begin_turn(4, 10.0, SEGMENTS), 0)
        self.assertEqual(tracker.on_tts_started("started", 10.5), 1)


class FallbackTests(unittest.TestCase):
    def test_fallback_to_timer_when_no_tts_events(self):
        # tts 模式下 >5s 没收到任何 started(用户可能关了 TTS)⇒ 改用字数估算推进
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(3, 0.0, [{"text": "三字句", "translation": ""}] * 3)
        self.assertIsNone(tracker.poll(4.9))
        self.assertEqual(tracker.poll(5.1), 1)
        self.assertIsNone(tracker.poll(5.1))  # 同一 now 幂等

    def test_no_fallback_after_real_started(self):
        # 收到过一次真 started ⇒ 本回合不再退化(否则两套时钟会打架)
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        tracker.on_tts_started("started", 1.0)
        self.assertIsNone(tracker.poll(100.0))

    def test_late_first_started_does_not_double_advance(self):
        # 宿主首个 started 迟到(本地 TTS 首次合成可能慢)时,退化路径已经推进过;
        # 迟到的 started 不能让它再跳一句 —— 否则高亮**永久超前**、前几句从未被高亮,
        # 并提前收尾(实测修复前:4 句 + 4 个 started 只得 [2, 3, None, None])。
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        self.assertEqual(tracker.poll(5.1), 1)
        self.assertIsNone(tracker.on_tts_started("started", 5.3))
        self.assertEqual(tracker.on_tts_started("started", 6.0), 2)
        self.assertEqual(tracker.on_tts_started("started", 7.0), 3)
        self.assertIsNone(tracker.on_tts_started("started", 8.0))


class OffModeTests(unittest.TestCase):
    def test_off_mode_never_highlights(self):
        tracker = highlight.HighlightTracker("off")
        self.assertEqual(tracker.begin_turn(4, 0.0, SEGMENTS), -1)
        self.assertIsNone(tracker.on_tts_started("started", 1.0))
        self.assertIsNone(tracker.poll(99.0))


if __name__ == "__main__":
    unittest.main()
