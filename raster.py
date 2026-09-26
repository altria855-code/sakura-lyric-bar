"""用 GDI 把形状与字形光栅化为覆盖度(8 位灰度),不直接产生颜色。

GDI 绘制不会写入 alpha 通道,所以本模块只产出「覆盖度」,
由 compose 模块按颜色与各自透明度合成 —— 这是透明浮窗的关键。
"""

from __future__ import annotations

import ctypes

import compose
import win32


class Raster:
    def __init__(self) -> None:
        self._screen_dc = win32.user32.GetDC(None)
        if not self._screen_dc:
            raise OSError(f"GetDC 失败: Win32 error {ctypes.get_last_error()}")
        self._memory_dc = win32.gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._memory_dc:
            win32.user32.ReleaseDC(None, self._screen_dc)
            raise OSError(f"CreateCompatibleDC 失败: Win32 error {ctypes.get_last_error()}")
        win32.gdi32.SetBkMode(self._memory_dc, win32.TRANSPARENT)

    def close(self) -> None:
        if getattr(self, "_memory_dc", None):
            win32.gdi32.DeleteDC(self._memory_dc)
            self._memory_dc = None
        if getattr(self, "_screen_dc", None):
            win32.user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None

    # --- 内部工具 ---

    def _make_dib(self, width: int, height: int) -> tuple[int, ctypes.c_void_p, ctypes.Array]:
        header = win32.BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 自顶向下
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = win32.BI_RGB

        bits = ctypes.c_void_p()
        bitmap = win32.gdi32.CreateDIBSection(
            self._memory_dc, ctypes.byref(header), win32.DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        if not bitmap:
            raise OSError(f"CreateDIBSection 失败: Win32 error {ctypes.get_last_error()}")
        buffer = (ctypes.c_ubyte * (width * height * 4)).from_address(bits.value)
        return bitmap, bits, buffer

    def _coverage_from_bits(self, bits, width: int, height: int) -> compose.Coverage:
        """从 32bpp DIB 的红色通道提取覆盖度(GDI 会把白色画成 BGR=FF,FF,FF)。"""
        # bits[2::4] 在 C 层一次取完每像素的第 3 字节(红色通道)。
        # 同尺寸同口径实测(460×120 的 DIB):逐像素循环约 3.7 ms,切片约 0.6 ms(约 6 倍)。
        # 注意别把 4 倍超采样后 1840×480 那张 DIB 的数字(约 57 ms)拿来跟这里的比 —— 像素量差 16 倍。
        return compose.Coverage(width=width, height=height, data=bytearray(bits[2::4]))

    # --- 形状 ---

    def rounded_rect(self, width: int, height: int, radius: int, supersample: int = 4) -> compose.Coverage:
        width = max(1, int(width))
        height = max(1, int(height))
        radius = max(0, min(int(radius), min(width, height) // 2))
        factor = max(1, int(supersample))

        big_width = width * factor
        big_height = height * factor
        bitmap, _bits, buffer = self._make_dib(big_width, big_height)
        previous = win32.gdi32.SelectObject(self._memory_dc, bitmap)
        try:
            ctypes.memset(buffer, 0, len(buffer))
            brush = win32.gdi32.CreateSolidBrush(0x00FFFFFF)
            pen = win32.gdi32.CreatePen(0, 1, 0x00FFFFFF)
            try:
                win32.gdi32.SelectObject(self._memory_dc, brush)
                win32.gdi32.SelectObject(self._memory_dc, pen)
                if radius == 0:
                    rect = win32.RECT(0, 0, big_width, big_height)
                    win32.user32.FillRect(self._memory_dc, ctypes.byref(rect), brush)
                else:
                    win32.gdi32.RoundRect(
                        self._memory_dc, 0, 0, big_width, big_height,
                        radius * factor * 2, radius * factor * 2,
                    )
                big = self._coverage_from_bits(buffer, big_width, big_height)
            finally:
                win32.gdi32.DeleteObject(brush)
                win32.gdi32.DeleteObject(pen)
        finally:
            win32.gdi32.SelectObject(self._memory_dc, previous)
            win32.gdi32.DeleteObject(bitmap)

        if factor == 1:
            return big
        return compose.downsample(big, factor)

    # --- 文字 ---

    def _create_font(self, font_family: str, font_size: int):
        return win32.gdi32.CreateFontW(
            -int(font_size), 0, 0, 0, 400, 0, 0, 0,
            win32.DEFAULT_CHARSET, win32.OUT_TT_PRECIS, win32.CLIP_DEFAULT_PRECIS,
            win32.ANTIALIASED_QUALITY, 0, font_family or "Microsoft YaHei UI",
        )

    def measure(self, text: str, font_family: str, font_size: int, max_width: int) -> tuple[int, int]:
        if not text:
            return (0, 0)
        rect = win32.RECT(0, 0, max(1, int(max_width)), 0)
        font = self._create_font(font_family, font_size)
        previous = win32.gdi32.SelectObject(self._memory_dc, font)
        try:
            win32.user32.DrawTextW(
                self._memory_dc, text, -1, ctypes.byref(rect),
                win32.DT_CALCRECT | win32.DT_WORDBREAK | win32.DT_NOPREFIX | win32.DT_EDITCONTROL,
            )  # 必须与 text() 用同一组标志,否则「量出来的高」和「画出来的换行」不一致
        finally:
            win32.gdi32.SelectObject(self._memory_dc, previous)
            win32.gdi32.DeleteObject(font)
        return (rect.right - rect.left, rect.bottom - rect.top)

    def text(
        self,
        text: str,
        font_family: str,
        font_size: int,
        max_width: int,
        align: str = "left",
    ) -> compose.Coverage:
        if not text:
            return compose.Coverage(width=0, height=0, data=b"")

        width = max(1, int(max_width))
        _, height = self.measure(text, font_family, font_size, width)
        height = max(1, height)

        bitmap, _bits, buffer = self._make_dib(width, height)
        previous_bitmap = win32.gdi32.SelectObject(self._memory_dc, bitmap)
        font = self._create_font(font_family, font_size)
        previous_font = win32.gdi32.SelectObject(self._memory_dc, font)
        try:
            ctypes.memset(buffer, 0, len(buffer))
            win32.gdi32.SetTextColor(self._memory_dc, 0x00FFFFFF)
            win32.gdi32.SetBkColor(self._memory_dc, 0x00000000)
            flags = win32.DT_WORDBREAK | win32.DT_NOPREFIX | win32.DT_EDITCONTROL
            if align == "center":
                flags |= win32.DT_CENTER
            elif align == "right":
                flags |= win32.DT_RIGHT
            rect = win32.RECT(0, 0, width, height)
            win32.user32.DrawTextW(self._memory_dc, text, -1, ctypes.byref(rect), flags)
            return self._coverage_from_bits(buffer, width, height)
        finally:
            win32.gdi32.SelectObject(self._memory_dc, previous_font)
            win32.gdi32.SelectObject(self._memory_dc, previous_bitmap)
            win32.gdi32.DeleteObject(font)
            win32.gdi32.DeleteObject(bitmap)
