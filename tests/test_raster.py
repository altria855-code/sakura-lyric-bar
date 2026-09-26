import os
import unittest

import _path  # noqa: F401
import raster

WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS, "仅 Windows")
class RoundedRectTests(unittest.TestCase):
    def setUp(self):
        self.raster = raster.Raster()
        self.addCleanup(self.raster.close)

    def test_center_is_fully_covered(self):
        coverage = self.raster.rounded_rect(40, 40, 10)
        self.assertEqual((coverage.width, coverage.height), (40, 40))
        center = coverage.data[20 * 40 + 20]
        self.assertEqual(center, 255)

    def test_corners_are_transparent(self):
        coverage = self.raster.rounded_rect(40, 40, 10)
        for index in (0, 39, 39 * 40, 39 * 40 + 39):
            self.assertEqual(coverage.data[index], 0, index)

    def test_edge_midpoints_are_covered(self):
        coverage = self.raster.rounded_rect(40, 40, 10)
        self.assertEqual(coverage.data[0 * 40 + 20], 255)
        self.assertEqual(coverage.data[20 * 40 + 0], 255)

    def test_corner_has_antialiased_pixels(self):
        coverage = self.raster.rounded_rect(60, 60, 20)
        corner_zone = [coverage.data[row * 60 + col] for row in range(0, 20) for col in range(0, 20)]
        partial = [value for value in corner_zone if 0 < value < 255]
        self.assertTrue(partial, "圆角处应存在半覆盖像素,证明做了抗锯齿")

    def test_zero_radius_fills_rectangle(self):
        coverage = self.raster.rounded_rect(20, 20, 0)
        self.assertEqual(set(coverage.data), {255})

    def test_radius_larger_than_half_is_clamped(self):
        coverage = self.raster.rounded_rect(20, 20, 999)
        self.assertEqual((coverage.width, coverage.height), (20, 20))
        self.assertEqual(coverage.data[10 * 20 + 10], 255)
        # 只查中心点的话,即使去掉夹取也能通过;与「半径 = 一半」逐字节比较能咬住
        # 「夹成了别的值」或「退化成直角」。但要注意它咬不住「整行删掉夹取」——
        # GDI 自己会把椭圆夹到矩形范围内,rad=999 不夹取与 rad=10 逐字节相同(已实测)。
        self.assertEqual(bytes(coverage.data), bytes(self.raster.rounded_rect(20, 20, 10).data))


@unittest.skipUnless(WINDOWS, "仅 Windows")
class TextTests(unittest.TestCase):
    def setUp(self):
        self.raster = raster.Raster()
        self.addCleanup(self.raster.close)

    def test_text_produces_ink(self):
        coverage = self.raster.text("测试", "Microsoft YaHei UI", 20, 400)
        self.assertGreater(coverage.width, 0)
        self.assertGreater(coverage.height, 0)
        self.assertTrue(any(value > 0 for value in coverage.data))

    def test_text_height_tracks_font_size(self):
        small = self.raster.measure("测试", "Microsoft YaHei UI", 16, 400)
        large = self.raster.measure("测试", "Microsoft YaHei UI", 32, 400)
        self.assertLess(small[1], large[1])

    def test_long_text_wraps_within_max_width(self):
        coverage = self.raster.text("这是一段很长的中文文本" * 8, "Microsoft YaHei UI", 20, 120)
        self.assertLessEqual(coverage.width, 120)
        # width 恒等于 max_width、height > 20 对「未换行的单行」也成立 —— 两条都太弱。
        # 真正的换行证据是出现多条墨迹带(行与行之间存在全零行)。
        inked = [
            row for row in range(coverage.height)
            if any(coverage.data[row * coverage.width + col] for col in range(coverage.width))
        ]
        gaps = [row for row in range(min(inked), max(inked)) if row not in inked]
        self.assertGreaterEqual(len(gaps) + 1, 2, "长文本应在栏宽内折成多行")

    def test_unknown_font_falls_back_without_raising(self):
        coverage = self.raster.text("abc", "不存在的字体名", 20, 400)
        self.assertTrue(any(value > 0 for value in coverage.data))

    def test_empty_text_yields_empty_coverage(self):
        coverage = self.raster.text("", "Microsoft YaHei UI", 20, 400)
        self.assertEqual(coverage.height, 0)


@unittest.skipUnless(WINDOWS, "仅 Windows")
class LifecycleTests(unittest.TestCase):
    def test_close_is_idempotent(self):
        surface = raster.Raster()
        surface.close()
        surface.close()  # 第二次调用不应抛异常


if __name__ == "__main__":
    unittest.main()
