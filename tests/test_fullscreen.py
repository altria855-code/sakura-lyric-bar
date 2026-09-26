import unittest

import _path  # noqa: F401
import fullscreen


class Probe(fullscreen.FullscreenProbe):
    def __init__(self, state=1, rect=None, monitor=(0, 0, 1920, 1080), klass="Chrome_WidgetWin_1"):
        self._state = state
        self._rect = rect
        self._monitor = monitor
        self._klass = klass

    def notification_state(self):
        return self._state

    def foreground_rect(self):
        return self._rect

    def monitor_rect(self):
        return self._monitor

    def foreground_class(self):
        return self._klass


class FullscreenTests(unittest.TestCase):
    def test_d3d_fullscreen_state_wins(self):
        self.assertTrue(fullscreen.is_fullscreen_foreground(Probe(state=3, rect=None)))

    def test_presentation_mode_counts(self):
        self.assertTrue(fullscreen.is_fullscreen_foreground(Probe(state=4, rect=None)))

    def test_accepts_notifications_state_is_not_fullscreen(self):
        # state 5 is QUNS_ACCEPTS_NOTIFICATIONS (quiet time is 6) -- and it is not fullscreen either.
        self.assertFalse(fullscreen.is_fullscreen_foreground(Probe(state=5, rect=None)))

    def test_geometry_covering_monitor_counts(self):
        probe = Probe(state=1, rect=(0, 0, 1920, 1080), monitor=(0, 0, 1920, 1080))
        self.assertTrue(fullscreen.is_fullscreen_foreground(probe))

    def test_geometry_covering_monitor_on_second_screen(self):
        probe = Probe(state=1, rect=(1920, 0, 3840, 1080), monitor=(1920, 0, 3840, 1080))
        self.assertTrue(fullscreen.is_fullscreen_foreground(probe))

    def test_windowed_maximized_is_not_fullscreen(self):
        probe = Probe(state=1, rect=(0, 0, 1920, 1040), monitor=(0, 0, 1920, 1080))
        self.assertFalse(fullscreen.is_fullscreen_foreground(probe))

    def test_tolerance_allows_two_pixel_gap(self):
        # 钉住 `_COVER_TOLERANCE == 2`:四条边各内缩 2px 仍算命中(容差改成 0 或 1 本用例会红)
        monitor = (0, 0, 1920, 1080)
        for edge, rect in (
            ("left", (2, 0, 1920, 1080)),
            ("top", (0, 2, 1920, 1080)),
            ("right", (0, 0, 1918, 1080)),
            ("bottom", (0, 0, 1920, 1078)),
        ):
            with self.subTest(edge=edge):
                self.assertTrue(fullscreen.is_fullscreen_foreground(Probe(state=1, rect=rect, monitor=monitor)))

    def test_tolerance_rejects_three_pixel_gap(self):
        # 反向钉住:内缩 3px 就不算命中(容差改成 3 或更大本用例会红)
        monitor = (0, 0, 1920, 1080)
        for edge, rect in (
            ("left", (3, 0, 1920, 1080)),
            ("top", (0, 3, 1920, 1080)),
            ("right", (0, 0, 1917, 1080)),
            ("bottom", (0, 0, 1920, 1077)),
        ):
            with self.subTest(edge=edge):
                self.assertFalse(fullscreen.is_fullscreen_foreground(Probe(state=1, rect=rect, monitor=monitor)))

    def test_shell_classes_are_ignored(self):
        probe = Probe(state=1, rect=(0, 0, 1920, 1080), klass="Progman")
        self.assertFalse(fullscreen.is_fullscreen_foreground(probe))

    def test_remaining_shell_classes_are_ignored(self):
        # 名单在用例里另抄一份:win32.SHELL_CLASS_NAMES 里删掉任何一个名字,本用例都会变红
        for klass in ("WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"):
            with self.subTest(klass=klass):
                probe = Probe(state=1, rect=(0, 0, 1920, 1080), klass=klass)
                self.assertFalse(fullscreen.is_fullscreen_foreground(probe))

    def test_missing_geometry_is_not_fullscreen(self):
        self.assertFalse(fullscreen.is_fullscreen_foreground(Probe(state=1, rect=None)))

    def test_probe_failure_counts_as_not_fullscreen(self):
        class Broken(fullscreen.FullscreenProbe):
            def notification_state(self):
                raise OSError("boom")

        self.assertFalse(fullscreen.is_fullscreen_foreground(Broken()))


if __name__ == "__main__":
    unittest.main()
