"""光栅化样张:圆角背景 + 一行日文,导出 PNG 供目视检查。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compose  # noqa: E402
import raster  # noqa: E402

WIDTH, HEIGHT, RADIUS = 460, 120, 14
BACKGROUND = (18, 14, 14, 166)   # #0E0E12 @ 65%
TEXT_COLOR = (255, 255, 255, 255)

def main() -> int:
    surface = raster.Raster()
    try:
        background = surface.rounded_rect(WIDTH, HEIGHT, RADIUS)
        buffer = compose.new_buffer(WIDTH, HEIGHT)
        compose.blit_coverage(buffer, 0, 0, background, BACKGROUND)
        glyphs = surface.text("おはよう、今日もいい天気だね。", "Microsoft YaHei UI", 20, WIDTH - 32)
        compose.blit_coverage(buffer, 16, 12, glyphs, TEXT_COLOR)
    finally:
        surface.close()
    out = Path(__file__).resolve().parents[1] / "render-check.png"
    compose.write_png(str(out), buffer)
    print(f"已写出 {out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
