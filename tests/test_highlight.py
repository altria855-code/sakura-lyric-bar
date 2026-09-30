import unittest

import _path  # noqa: F401
import highlight

SEGMENTS = [{"text": f"第{i}句", "translation": ""} for i in range(4)]


class TtsModeTests(unittest.TestCase):
    def test_first_playback_highlights_first_segment_and_duplicates_do_not_advance(self):
        tracker = highlight.HighlightTracker("tts")
        self.assertEqual(tracker.begin_turn(4, 0.0, SEGMENTS), -1)
        self.assertEqual(tracker.on_tts_started(0, 1.0), 0)
        self.assertIsNone(tracker.on_tts_started(0, 2.0))
        self.assertEqual(tracker.on_tts_started(1, 3.0), 1)
        self.assertEqual(tracker.on_tts_started(3, 4.0), 3)

    def test_replaying_an_earlier_segment_uses_its_index(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        tracker.on_tts_started(3, 1.0)
        self.assertEqual(tracker.on_tts_started(0, 2.0), 0)
        self.assertEqual(tracker.on_tts_ended(), -1)
        self.assertIsNone(tracker.on_tts_ended())

    def test_no_identity_never_falls_back_to_guessed_timing(self):
        tracker = highlight.HighlightTracker("tts")
        tracker.begin_turn(4, 0.0, SEGMENTS)
        for value in (None, "started", True, -1, 4):
            with self.subTest(value=value):
                self.assertIsNone(tracker.on_tts_started(value, 1.0))
        self.assertIsNone(tracker.poll(100.0))


class TimerModeTests(unittest.TestCase):
    def test_timer_advances_without_tts_and_stops_at_last_segment(self):
        tracker = highlight.HighlightTracker("timer")
        self.assertEqual(tracker.begin_turn(2, 0.0, SEGMENTS), 0)
        self.assertIsNone(tracker.poll(0.1))
        self.assertEqual(tracker.poll(2.0), 1)
        self.assertIsNone(tracker.poll(2.0))
        self.assertIsNone(tracker.poll(100.0))
        self.assertIsNone(tracker.on_tts_started(0, 101.0))


class OffModeTests(unittest.TestCase):
    def test_off_mode_never_highlights(self):
        tracker = highlight.HighlightTracker("off")
        self.assertEqual(tracker.begin_turn(4, 0.0, SEGMENTS), -1)
        self.assertIsNone(tracker.on_tts_started(0, 1.0))
        self.assertIsNone(tracker.poll(99.0))


if __name__ == "__main__":
    unittest.main()
