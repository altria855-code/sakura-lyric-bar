"""预乘 BGRA 像素缓冲、覆盖度合成与 PNG 导出。纯字节运算。"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


@dataclass
class Buffer:
    width: int
    height: int
    data: bytearray


@dataclass
class Coverage:
    width: int
    height: int
    data: bytes | bytearray


def new_buffer(width: int, height: int) -> Buffer:
    width = max(1, int(width))
    height = max(1, int(height))
    return Buffer(width=width, height=height, data=bytearray(width * height * 4))


def _premultiply(color: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    r, g, b, a = color
    a = max(0, min(255, int(a)))
    scale = a / 255.0
    return (
        max(0, min(255, int(round(r * scale)))),
        max(0, min(255, int(round(g * scale)))),
        max(0, min(255, int(round(b * scale)))),
        a,
    )


def fill_rect(
    buffer: Buffer,
    x: int,
    y: int,
    width: int,
    height: int,
    color: tuple[int, int, int, int],
) -> None:
    left = max(0, int(x))
    top = max(0, int(y))
    right = min(buffer.width, int(x) + int(width))
    bottom = min(buffer.height, int(y) + int(height))
    if left >= right or top >= bottom:
        return

    r, g, b, a = _premultiply(color)
    stride = buffer.width * 4

    if a == 255:
        # 不透明填充与 source-over 结果恒等,走整行切片快路径。
        row = bytes((b, g, r, 255)) * (right - left)
        for row_index in range(top, bottom):
            start = row_index * stride + left * 4
            buffer.data[start : start + len(row)] = row
        return

    # 半透明填充必须 source-over。高亮底色允许写成 #AARRGGBB,若直接覆盖会把
    # 栏背景的 alpha 一起抹掉,高亮区比栏本身更透明 —— 视觉上「打洞」。
    keep = 1.0 - a / 255.0
    for row_index in range(top, bottom):
        base = row_index * stride + left * 4
        for column in range(right - left):
            index = base + column * 4
            buffer.data[index] = int(b + buffer.data[index] * keep)
            buffer.data[index + 1] = int(g + buffer.data[index + 1] * keep)
            buffer.data[index + 2] = int(r + buffer.data[index + 2] * keep)
            buffer.data[index + 3] = int(a + buffer.data[index + 3] * keep)


def blit_coverage(
    buffer: Buffer,
    x: int,
    y: int,
    coverage: Coverage,
    color: tuple[int, int, int, int],
) -> None:
    src_left = max(0, -int(x))
    src_top = max(0, -int(y))
    dst_left = max(0, int(x))
    dst_top = max(0, int(y))
    copy_width = min(coverage.width - src_left, buffer.width - dst_left)
    copy_height = min(coverage.height - src_top, buffer.height - dst_top)
    if copy_width <= 0 or copy_height <= 0:
        return

    r, g, b, a = _premultiply(color)
    stride = buffer.width * 4
    source = coverage.data

    for row in range(copy_height):
        source_row = (src_top + row) * coverage.width + src_left
        target_row = (dst_top + row) * stride + dst_left * 4
        for column in range(copy_width):
            cover = source[source_row + column]
            if not cover:
                continue
            index = target_row + column * 4
            # source-over:覆盖度只调制「源」,目标按 (1 - 源alpha) 保留。
            # 若写成直接覆盖,半覆盖的字形边缘会连目标的 alpha 一起抹掉,
            # 在半透明栏上表现为文字边缘「打洞」(边缘比背景更透明)。
            factor = cover / 255.0
            source_alpha = a * factor
            keep = 1.0 - source_alpha / 255.0
            buffer.data[index] = int(b * factor + buffer.data[index] * keep)
            buffer.data[index + 1] = int(g * factor + buffer.data[index + 1] * keep)
            buffer.data[index + 2] = int(r * factor + buffer.data[index + 2] * keep)
            buffer.data[index + 3] = int(source_alpha + buffer.data[index + 3] * keep)


def downsample(coverage: Coverage, factor: int) -> Coverage:
    factor = int(factor)
    if factor < 2:
        raise ValueError("factor must be >= 2")
    if coverage.width % factor or coverage.height % factor:
        raise ValueError("coverage size must be divisible by factor")

    width = coverage.width // factor
    height = coverage.height // factor
    area = factor * factor
    out = bytearray(width * height)
    source = coverage.data
    source_stride = coverage.width

    for row in range(height):
        for column in range(width):
            total = 0
            base_y = row * factor
            base_x = column * factor
            for inner_y in range(factor):
                start = (base_y + inner_y) * source_stride + base_x
                for inner_x in range(factor):
                    total += source[start + inner_x]
            out[row * width + column] = (total + area // 2) // area  # 四舍五入,避免整体偏暗
    return Coverage(width=width, height=height, data=out)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def write_png(path: str, buffer: Buffer) -> None:
    """把预乘 BGRA 缓冲写成 8 位 RGBA PNG(非预乘)。"""
    raw = bytearray()
    stride = buffer.width * 4
    for row in range(buffer.height):
        raw.append(0)  # filter type 0
        start = row * stride
        for column in range(buffer.width):
            index = start + column * 4
            b, g, r, a = buffer.data[index : index + 4]
            if a and a != 255:
                r = min(255, r * 255 // a)
                g = min(255, g * 255 // a)
                b = min(255, b * 255 // a)
            raw.extend((r, g, b, a))

    header = struct.pack(">IIBBBBB", buffer.width, buffer.height, 8, 6, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(payload)
