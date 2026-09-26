import os
import struct
import tempfile
import unittest

import _path  # noqa: F401
import compose


def pixel(buffer, x, y):
    index = (y * buffer.width + x) * 4
    return tuple(buffer.data[index : index + 4])  # (b, g, r, a)


class BufferTests(unittest.TestCase):
    def test_new_buffer_is_fully_transparent(self):
        buffer = compose.new_buffer(4, 3)
        self.assertEqual(len(buffer.data), 4 * 3 * 4)
        self.assertEqual(set(buffer.data), {0})

    def test_fill_rect_writes_premultiplied_bgra(self):
        buffer = compose.new_buffer(4, 4)
        compose.fill_rect(buffer, 1, 1, 2, 2, (255, 0, 0, 255))
        self.assertEqual(pixel(buffer, 1, 1), (0, 0, 255, 255))
        self.assertEqual(pixel(buffer, 0, 0), (0, 0, 0, 0))

    def test_fill_rect_is_clipped_to_buffer(self):
        buffer = compose.new_buffer(3, 3)
        compose.fill_rect(buffer, -1, -1, 10, 10, (10, 20, 30, 255))
        self.assertEqual(pixel(buffer, 0, 0), (30, 20, 10, 255))

    def test_half_alpha_is_premultiplied(self):
        buffer = compose.new_buffer(2, 2)
        compose.fill_rect(buffer, 0, 0, 1, 1, (200, 100, 50, 128))
        b, g, r, a = pixel(buffer, 0, 0)
        self.assertEqual(a, 128)
        self.assertEqual(r, 100)  # 200 * 128/255 ≈ 100
        self.assertEqual(g, 50)
        self.assertEqual(b, 25)


class CoverageTests(unittest.TestCase):
    def test_blit_full_coverage_paints_opaque(self):
        buffer = compose.new_buffer(3, 3)
        coverage = compose.Coverage(2, 2, bytes([255] * 4))
        compose.blit_coverage(buffer, 1, 1, coverage, (255, 255, 255, 255))
        self.assertEqual(pixel(buffer, 1, 1), (255, 255, 255, 255))
        self.assertEqual(pixel(buffer, 0, 0), (0, 0, 0, 0))

    def test_half_coverage_halves_alpha(self):
        buffer = compose.new_buffer(2, 2)
        coverage = compose.Coverage(1, 1, bytes([128]))
        compose.blit_coverage(buffer, 0, 0, coverage, (255, 255, 255, 255))
        self.assertEqual(pixel(buffer, 0, 0), (128, 128, 128, 128))

    def test_zero_coverage_leaves_buffer_untouched(self):
        buffer = compose.new_buffer(2, 2)
        coverage = compose.Coverage(1, 1, bytes([0]))
        compose.blit_coverage(buffer, 0, 0, coverage, (255, 0, 0, 255))
        self.assertEqual(pixel(buffer, 0, 0), (0, 0, 0, 0))

    def test_semi_transparent_fill_blends_over_existing_pixel(self):
        # 当前句高亮底色允许配成带 alpha 的颜色(#AARRGGBB)。它必须与栏背景
        # 做 source-over,否则高亮区会比栏本身更透明 —— 视觉上「打洞」。
        buffer = compose.new_buffer(2, 2)
        compose.fill_rect(buffer, 0, 0, 2, 2, (0, 0, 0, 166))
        compose.fill_rect(buffer, 0, 0, 1, 1, (255, 212, 0, 64))
        _b, _g, _r, a = pixel(buffer, 0, 0)
        self.assertGreaterEqual(a, 166, "半透明填充不得让已有像素更透明")

    def test_half_coverage_blends_over_existing_pixel(self):
        # 字形抗锯齿边缘是半覆盖。它必须与已有背景做 source-over 混合,
        # 不能把背景 alpha 覆盖掉 —— 否则半透明栏上文字边缘会「打洞」。
        buffer = compose.new_buffer(2, 2)
        compose.fill_rect(buffer, 0, 0, 1, 1, (0, 0, 0, 166))
        coverage = compose.Coverage(1, 1, bytes([128]))
        compose.blit_coverage(buffer, 0, 0, coverage, (255, 255, 255, 255))
        _b, _g, r, a = pixel(buffer, 0, 0)
        self.assertGreaterEqual(a, 166, "边缘像素 alpha 不得低于背景 alpha")
        self.assertGreater(r, 100, "白色应已部分混入")

    def test_blit_outside_buffer_does_not_raise(self):
        buffer = compose.new_buffer(2, 2)
        coverage = compose.Coverage(4, 4, bytes([255] * 16))
        compose.blit_coverage(buffer, -2, -2, coverage, (255, 255, 255, 255))
        self.assertEqual(pixel(buffer, 0, 0), (255, 255, 255, 255))

    def test_downsample_averages_blocks(self):
        source = compose.Coverage(4, 4, bytes([255, 0, 0, 255] * 4))
        result = compose.downsample(source, 2)
        self.assertEqual((result.width, result.height), (2, 2))
        self.assertEqual(result.data[0], 128)
        self.assertEqual(result.data[1], 128)

    def test_downsample_rejects_non_divisible_factor(self):
        with self.assertRaises(ValueError):
            compose.downsample(compose.Coverage(3, 3, bytes(9)), 2)


class PngTests(unittest.TestCase):
    def test_write_png_produces_valid_header(self):
        buffer = compose.new_buffer(3, 2)
        compose.fill_rect(buffer, 0, 0, 3, 2, (0, 0, 255, 255))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sample.png")
            compose.write_png(path, buffer)
            with open(path, "rb") as handle:
                payload = handle.read()
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", payload[16:24])
        self.assertEqual((width, height), (3, 2))
        self.assertIn(b"IEND", payload[-12:])


if __name__ == "__main__":
    unittest.main()
