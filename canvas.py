"""分层画布窗口:置顶、可拖动、逐像素透明。

本模块不产生颜色 —— 图形与字形一律先在 `raster` 里光栅化为覆盖度,再由 `compose`
按颜色与各自透明度合成到预乘 BGRA 缓冲,最后经 `UpdateLayeredWindow` 整体提交。
模块负责的是绘制顺序、缓冲生命周期、窗口尺寸,以及把鼠标/定时器消息翻译成状态变化。

两个必须遵守的约束(来自 Task 3 的实测与裁决):
- `compose` 的两个填充函数是 source-over 语义,因此每次重绘都从新缓冲开始
  (或整块拷入缓存背景),在未清空的缓冲上原地重画会累积 alpha。
- 半透明栏底的逐像素合成很贵(460×300 约 130 ms),所以「圆角背景 + 边框」
  整层缓存,常规重绘只重画高亮底与文字行。
"""

from __future__ import annotations

import ctypes
import dataclasses
import json
import time
import traceback
from collections import OrderedDict
from ctypes import wintypes
from typing import Any, Callable, Mapping

import compose
import layout
import raster
import theme as theme_module
import win32

WINDOW_CLASS_NAME = "SakuraLyricBarCanvas"

# 内边距由画布添加(layout 只输出紧贴文字的盒子)。纵向 20 + 20 即 `view_height + 40`。
PADDING_X = 16
PADDING_Y = 20
# 空状态细条高度。原来是 6px —— 但空栏时这条细条是用户**唯一**的拖拽把手
# (刚启动、换角色之后栏里都没有内容),6px 太细抓不住。16px 仍然不占地方,
# 但鼠标能稳稳按住拖动。
COLLAPSED_HEIGHT = 16
BAR_BOTTOM_MARGIN = 80  # 默认位置:工作区底边往上留出的余量(够放下方的输入框)

SHADOW_OFFSETS = ((1, 1), (1, 2))
SHADOW_ALPHA = 128
OUTLINE_DIRECTIONS = (
    (-1, -1), (0, -1), (1, -1),
    (-1, 0), (1, 0),
    (-1, 1), (0, 1), (1, 1),
)

TOPMOST_TIMER_ID = 1
TOPMOST_INTERVAL_MS = 3000
BACKGROUND_CACHE_LIMIT = 4
ERROR_CLASS_ALREADY_EXISTS = 1410

# 窗口过程必须由模块级引用持住:WNDPROC 包装对象一旦被 GC,
# 注册到窗口类里的函数指针就会悬空(点一下窗口进程就崩)。
_wndproc_ref: Any = None
_class_registered = False

_windows: dict[int, "CanvasWindow"] = {}
_shared_raster: raster.Raster | None = None
_background_cache: "OrderedDict[tuple, _BackgroundLayer]" = OrderedDict()
_measure_cache: dict[tuple, int] = {}
MEASURE_CACHE_LIMIT = 512


# --- 公共资源(进程内共享,避免每次重绘都开 DC) ---


def _raster() -> raster.Raster:
    global _shared_raster
    if _shared_raster is None:
        _shared_raster = raster.Raster()
    return _shared_raster


def theme_measure(values: Mapping[str, Any]) -> Callable[[str, int], int]:
    """给布局用的测量函数:按主题字体量出「一行文字折行后的真实高度」。

    布局层本身不碰 GDI,所以由这里注入。结果按 (文字, 最大宽度, 字体, 字号) 缓存 ——
    `measure` 内部要 `CreateFontW`,每帧把每行重量一遍是白烧。
    """

    family = str(values.get("fontFamily") or "")
    size = max(1, int(values.get("fontSize") or 20))
    spacing = float(values.get("lineSpacing") or 1.35)
    line_height = max(1, int(round(size * spacing)))

    def one_line() -> int:
        """字体自己的单行高度(不含用户调的行距)。"""
        key = ("\x00one-line", family, size)
        cached = _measure_cache.get(key)
        if cached is None:
            _width, height = _raster().measure("测Ag", family, size, 4096)
            cached = int(height) if height else line_height
            _measure_cache[key] = cached
        return cached

    def measure(text: str, max_width: int) -> int:
        key = (text, int(max_width), family, size)
        wrapped = _measure_cache.get(key)
        if wrapped is None:
            _width, height = _raster().measure(text, family, size, int(max_width))
            wrapped = int(height)
            if len(_measure_cache) >= MEASURE_CACHE_LIMIT:
                _measure_cache.clear()
            _measure_cache[key] = wrapped

        # 实测高度是「字形本身」占的高度,不含用户调的行距。所以折成几行就补几份
        # 「行距比自然行高高出来的那部分」—— 这样长台词排得开,`行距` 设置也仍然生效
        # (直接把实测高度当行高会把行距吃掉)。
        natural = one_line()
        if natural <= 0:
            return max(wrapped, line_height)
        extra = max(0, line_height - natural)
        visual_lines = max(1, int(round(wrapped / natural)))
        return wrapped + extra * visual_lines

    return measure


def close_shared_raster() -> None:
    """关闭进程内共享的光栅器(退出路径调用);没建过就什么都不做,可重复调用。

    公开入口是给调用方用的:`overlay` 退出时要收尾,以前靠读 `canvas._shared_raster` ——
    私有字段一旦改名就会**静默跳过收尾**。
    """
    global _shared_raster
    _measure_cache.clear()
    if _shared_raster is not None:
        _shared_raster.close()
        _shared_raster = None


def _theme_key(values: Mapping[str, Any]) -> str:
    return json.dumps(dict(values), ensure_ascii=False, sort_keys=True)


# --- 绘制工具 ---


def _copy_layer(buffer: compose.Buffer, layer: compose.Buffer) -> None:
    """把缓存层拷进目标缓冲(同尺寸走整块切片,否则逐行)。"""
    if buffer.width == layer.width and buffer.height == layer.height:
        buffer.data[:] = layer.data
        return
    rows = min(buffer.height, layer.height)
    columns = min(buffer.width, layer.width) * 4
    source_stride = layer.width * 4
    target_stride = buffer.width * 4
    for row in range(rows):
        target = row * target_stride
        source = row * source_stride
        buffer.data[target : target + columns] = layer.data[source : source + columns]


def _blit_clipped(
    buffer: compose.Buffer,
    x: int,
    y: int,
    coverage: compose.Coverage,
    color: tuple[int, int, int, int],
    clip_top: int,
    clip_bottom: int,
) -> None:
    """按视口裁剪后合成:落在 [clip_top, clip_bottom) 之外的行直接丢弃。

    滚动时半行会露在视口外,不裁剪就会画进栏的内边距、盖住圆角与边框。
    """
    if coverage.width <= 0 or coverage.height <= 0:
        return
    top = max(int(y), int(clip_top))
    bottom = min(int(y) + coverage.height, int(clip_bottom), buffer.height)
    if bottom <= top:
        return
    skip = top - int(y)
    height = bottom - top
    width = coverage.width
    data = coverage.data[skip * width : (skip + height) * width]
    compose.blit_coverage(
        buffer, x, top, compose.Coverage(width=width, height=height, data=data), color
    )


def _ring_coverage(
    outer: compose.Coverage, inner: compose.Coverage, inset: int
) -> compose.Coverage:
    """边框覆盖度 = 外圈圆角减去内缩 `inset` 像素的内圈圆角(逐像素相减,保住抗锯齿)。"""
    width = outer.width
    height = outer.height
    ring = bytearray(width * height)
    outer_data = outer.data
    inner_data = inner.data
    inner_width = inner.width
    left = max(0, inset)
    right = min(width, inset + inner_width)
    for row in range(height):
        start = row * width
        source_row = row - inset
        if source_row < 0 or source_row >= inner.height:
            ring[start : start + width] = outer_data[start : start + width]
            continue
        ring[start : start + left] = outer_data[start : start + left]
        ring[start + right : start + width] = outer_data[start + right : start + width]
        base = source_row * inner_width
        for column in range(left, right):
            value = outer_data[start + column] - inner_data[base + column - inset]
            if value > 0:
                ring[start + column] = value
    return compose.Coverage(width=width, height=height, data=ring)


def _role_color(
    values: Mapping[str, Any], box: layout.LineBox, current_segment: int, overall: int
) -> tuple[int, int, int, int]:
    """按行角色取色;当前朗读句的**所有显示行**用高亮色。

    不能只对 `role == "source"` 生效:只中文模式下画面上根本没有原文行,
    那样高亮色就永远用不上 —— 而「当前句底色」允许留空,此时文字色是唯一的高亮提示。
    """
    if box.segment >= 0 and box.segment == current_segment:
        key = "highlightColor"
    elif box.role in ("translation", "notice"):
        key = "translationColor"
    else:  # source / user
        key = "textColor"
    color = theme_module.parse_color(values.get(key)) or (255, 255, 255, 255)
    return theme_module.apply_overall_alpha(color, overall)


@dataclasses.dataclass
class _BackgroundLayer:
    """背景层的两份产物,同键缓存:合成好的位图 + 高亮底要复用的圆角覆盖度。"""

    buffer: compose.Buffer  # 圆角背景 + 边框
    mask: compose.Coverage  # 外层圆角覆盖度(未合成),高亮底的掩膜


def _build_background(width: int, height: int, values: Mapping[str, Any]) -> _BackgroundLayer:
    surface = _raster()
    layer = compose.new_buffer(width, height)
    radius = int(values.get("cornerRadius", 0))

    background = theme_module.parse_color(values.get("backgroundColor")) or (0, 0, 0, 0)
    background = theme_module.apply_overall_alpha(
        background, int(values.get("backgroundOpacity", 100))
    )
    background = theme_module.apply_overall_alpha(
        background, int(values.get("overallOpacity", 100))
    )
    mask = surface.rounded_rect(width, height, radius)
    compose.blit_coverage(layer, 0, 0, mask, background)

    border_width = int(values.get("borderWidth", 0))
    if border_width > 0 and width > border_width * 2 and height > border_width * 2:
        inner = surface.rounded_rect(
            width - border_width * 2, height - border_width * 2, max(0, radius - border_width)
        )
        border = theme_module.parse_color(values.get("borderColor")) or (255, 255, 255, 255)
        border = theme_module.apply_overall_alpha(border, int(values.get("overallOpacity", 100)))
        compose.blit_coverage(layer, 0, 0, _ring_coverage(mask, inner, border_width), border)

    return _BackgroundLayer(buffer=layer, mask=mask)


def _background_layer(width: int, height: int, values: Mapping[str, Any]) -> _BackgroundLayer:
    """「圆角背景 + 边框」合成层,只在尺寸或主题变化时重建。"""
    key = (_theme_key(values), int(width), int(height))
    cached = _background_cache.get(key)
    if cached is not None:
        _background_cache.move_to_end(key)
        return cached

    layer = _build_background(width, height, values)
    _background_cache[key] = layer
    while len(_background_cache) > BACKGROUND_CACHE_LIMIT:
        _background_cache.popitem(last=False)
    return layer


def _band_mask(layer: _BackgroundLayer, inset: int, top: int, bottom: int) -> compose.Coverage:
    """从缓存的圆角覆盖度里切出高亮底块的掩膜:纵向 [top, bottom)、横向内缩 `inset`。

    `blit_coverage` 的源是按自身宽度连续排布的,所以横向内缩要逐行重排;
    只处理高亮块那几行(最多 maxLines 行),代价可忽略。
    """
    mask = layer.mask
    top = max(0, min(int(top), mask.height))
    bottom = max(top, min(int(bottom), mask.height))
    left = max(0, min(int(inset), mask.width))
    right = max(left, mask.width - left)
    row_bytes = right - left
    data = bytearray()
    for row in range(top, bottom):
        start = row * mask.width
        data += mask.data[start + left : start + right]
    return compose.Coverage(width=row_bytes, height=bottom - top, data=data)


def _default_position(width: int, height: int) -> tuple[int, int]:
    """默认位置:主屏工作区底部居中(见验收清单第 1 条)。"""
    work = win32.RECT()
    if not win32.user32.SystemParametersInfoW(
        win32.SPI_GETWORKAREA, 0, ctypes.byref(work), 0
    ):
        return (0, 0)
    left = work.left + max(0, (work.right - work.left - width) // 2)
    top = work.bottom - height - BAR_BOTTOM_MARGIN
    return (left, max(work.top, top))


# --- 窗口过程 ---


def _dispatch(hwnd: Any, msg: int, wparam: Any, lparam: Any) -> int:
    window = _windows.get(int(hwnd or 0))
    if window is None:
        return win32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return window._handle(hwnd, msg, wparam, lparam)


def _register_class() -> None:
    global _wndproc_ref, _class_registered
    if _class_registered:
        return

    _wndproc_ref = win32.WNDPROC(_dispatch)
    instance = win32.kernel32.GetModuleHandleW(None)
    window_class = win32.WNDCLASSEXW()
    window_class.cbSize = ctypes.sizeof(win32.WNDCLASSEXW)
    window_class.style = 0
    window_class.lpfnWndProc = _wndproc_ref
    window_class.cbClsExtra = 0
    window_class.cbWndExtra = 0
    window_class.hInstance = instance
    window_class.hIcon = None
    # 类光标必须是箭头:否则栏上沿用系统当前光标(从别的程序上拖过来会显示成那里的光标)。
    # 类光标只认注册时的值,所以在这里设,而不是在 __init__ 里每次创建窗口时设。
    window_class.hCursor = win32.user32.LoadCursorW(None, win32.IDC_ARROW)
    window_class.hbrBackground = None  # 分层窗口的背景由 ULW 提交,不能有类背景刷
    window_class.lpszMenuName = None
    window_class.lpszClassName = WINDOW_CLASS_NAME
    window_class.hIconSm = None

    if not win32.user32.RegisterClassExW(ctypes.byref(window_class)):
        error = ctypes.get_last_error()
        if error != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(f"RegisterClassExW 失败: Win32 error {error}")
    _class_registered = True


class CanvasWindow:
    """置顶、可拖动、逐像素透明的画布窗口。"""

    def __init__(
        self,
        theme_values: Mapping[str, Any],
        on_hover_change: Callable[[bool], None] | None = None,
        on_drag_end: Callable[[int, int], None] | None = None,
        on_drag_start: Callable[[], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._theme_values: dict[str, Any] = dict(theme_values)
        self._on_hover_change = on_hover_change
        self._on_drag_end = on_drag_end
        self._on_drag_start = on_drag_start
        # Task 5 还没有依赖时间的逻辑(提示过期、悬停延时都在后续任务),先按接口收下。
        self.clock = clock or time.monotonic

        self._hwnd: int | None = None
        self._size = (int(self._theme_values.get("width", 460)), COLLAPSED_HEIGHT)
        self._offset = 0
        self._running = True
        self._shown = False
        self._policy_visible = True
        self._content_visible = True
        self._tracking_leave = False
        self._dragging = False
        self._drag_origin = (0, 0)
        self._drag_window = (0, 0)
        self._anchor_bottom = True
        self._timer_id: Any = None

        self._screen_dc = win32.user32.GetDC(None)
        if not self._screen_dc:
            raise OSError(f"GetDC 失败: Win32 error {ctypes.get_last_error()}")
        self._memory_dc = win32.gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._memory_dc:
            win32.user32.ReleaseDC(None, self._screen_dc)
            raise OSError(f"CreateCompatibleDC 失败: Win32 error {ctypes.get_last_error()}")

        self._dib_bitmap: Any = None
        self._dib_array: Any = None
        self._dib_previous: Any = None
        self._dib_size: tuple[int, int] | None = None

        try:
            _register_class()
            self._create_window()
        except OSError:
            self._release_dc()  # 与 CreateWindowExW 失败路径一致,别把两个 DC 漏在那
            raise

    # --- 窗口生命周期 ---

    def _create_window(self) -> None:
        width, height = self._size
        left, top = _default_position(width, height)
        style = (
            win32.WS_EX_TOPMOST
            | win32.WS_EX_TOOLWINDOW
            | win32.WS_EX_LAYERED
            | win32.WS_EX_NOACTIVATE
        )
        hwnd = win32.user32.CreateWindowExW(
            style, WINDOW_CLASS_NAME, "", win32.WS_POPUP,
            left, top, width, height,
            None, None, win32.kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            self._release_dc()
            raise OSError(f"CreateWindowExW 失败: Win32 error {ctypes.get_last_error()}")

        self._hwnd = int(hwnd)
        _windows[self._hwnd] = self
        self._timer_id = win32.user32.SetTimer(
            self._hwnd, TOPMOST_TIMER_ID, TOPMOST_INTERVAL_MS, None
        )

    def show(self) -> None:
        self._shown = True
        self._apply_visibility()

    def hide(self) -> None:
        self._shown = False
        self._apply_visibility()

    def close(self) -> None:
        hwnd = self._hwnd
        if hwnd is not None:
            if self._timer_id:
                win32.user32.KillTimer(hwnd, self._timer_id)
                self._timer_id = None
            if self._dragging:
                self._dragging = False
                win32.user32.ReleaseCapture()
            # DestroyWindow 未绑定:WM_CLOSE 走 DefWindowProc 会自己销毁窗口,
            # 且是同步的(SendMessage 同线程直调窗口过程)。
            win32.user32.SendMessageW(hwnd, win32.WM_CLOSE, 0, 0)
            _windows.pop(int(hwnd), None)
            self._hwnd = None
        self._running = False
        self._release_dib()
        self._release_dc()

    def _release_dib(self) -> None:
        if self._dib_bitmap:
            if self._memory_dc:
                win32.gdi32.SelectObject(self._memory_dc, self._dib_previous)
            win32.gdi32.DeleteObject(self._dib_bitmap)
        self._dib_bitmap = None
        self._dib_array = None
        self._dib_previous = None
        self._dib_size = None

    def _release_dc(self) -> None:
        if getattr(self, "_memory_dc", None):
            win32.gdi32.DeleteDC(self._memory_dc)
            self._memory_dc = None
        if getattr(self, "_screen_dc", None):
            win32.user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None

    def set_visible_by_policy(self, visible: bool) -> None:
        """全屏隐藏 / `enabled` 开关:只影响显示,不销毁窗口,位置与状态保留。"""
        self._policy_visible = bool(visible)
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        if self._hwnd is None:
            return
        if self._shown and self._policy_visible and self._content_visible:
            win32.user32.ShowWindow(self._hwnd, win32.SW_SHOWNOACTIVATE)
            self._raise_topmost()
        else:
            win32.user32.ShowWindow(self._hwnd, win32.SW_HIDE)

    # --- 位置与几何 ---

    def geometry(self) -> tuple[int, int, int, int] | None:
        """窗口矩形 (x, y, 宽, 高);**已销毁时返回 None**(退出路径会读它)。"""
        if self._hwnd is None:
            return None
        return self._window_rect()

    def _window_rect(self) -> tuple[int, int, int, int]:
        rect = win32.RECT()
        if not win32.user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
            raise OSError(f"GetWindowRect 失败: Win32 error {ctypes.get_last_error()}")
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def move_to(self, x: int, y: int) -> None:
        if self._hwnd is None:
            return
        self._anchor_bottom = False
        win32.user32.SetWindowPos(
            self._hwnd, win32.HWND_TOPMOST, int(x), int(y), 0, 0,
            win32.SWP_NOSIZE | win32.SWP_NOACTIVATE,
        )

    def restore_default_position(self) -> None:
        """回到默认位置(主屏工作区底部居中),并把锚点恢复成「贴底、向上生长」。

        `move_to` 会把锚点改成左上角 —— 那是拖动需要的;但回到默认位置后必须改回来,
        否则内容一变高,栏就以左上角为锚**朝屏幕外**长。`reset-position`(设置页的
        「重置位置」)要的就是这个组合,所以收成一个公开入口,别让调用方去改私有字段。
        """
        if self._hwnd is None:
            return
        self.move_to(*_default_position(*self._size))
        self._anchor_bottom = True

    def _raise_topmost(self) -> None:
        if self._hwnd is None:
            return
        win32.user32.SetWindowPos(
            self._hwnd, win32.HWND_TOPMOST, 0, 0, 0, 0,
            win32.SWP_NOMOVE | win32.SWP_NOSIZE | win32.SWP_NOACTIVATE,
        )

    def _resize(self, width: int, height: int) -> None:
        if (width, height) == self._size or self._hwnd is None:
            return
        left, top = self._window_rect()[:2]
        flags = win32.SWP_NOACTIVATE
        if self._anchor_bottom:
            # 默认位置是贴着屏幕底部的,内容变高时向上生长,底边保持不动。
            top = top + self._size[1] - height
        else:
            flags |= win32.SWP_NOMOVE
        self._size = (width, height)
        win32.user32.SetWindowPos(
            self._hwnd, win32.HWND_TOPMOST, left, top, int(width), int(height), flags
        )

    # --- 消息 ---

    def pump(self, message_filter: Callable[[Any], bool] | None = None) -> bool:
        """抽干本线程消息队列(不阻塞),返回是否继续运行。

        队列必须只在**这一处**抽干(Task 6 的输入框也走这里),否则两个 drainer
        会互相吞消息。`message_filter` 是输入框的 `filter_message`:焦点在 EDIT
        子控件上,回车/Esc 发给 EDIT、父窗口过程看不到,只能在这里拦。
        """
        if self._hwnd is None:
            return False
        message = wintypes.MSG()
        while win32.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, win32.PM_REMOVE):
            if message.message == win32.WM_QUIT:
                self._running = False
                continue
            # 被过滤器接管的按键不得再 Translate/Dispatch:TranslateMessage 会为它
            # 生成 WM_CHAR,派发下去仍会送到 EDIT(滤波器等于白拦)。
            if message_filter is not None and message_filter(message):
                continue
            win32.user32.TranslateMessage(ctypes.byref(message))
            win32.user32.DispatchMessageW(ctypes.byref(message))
        return self._running

    def _handle(self, hwnd: Any, msg: int, wparam: Any, lparam: Any) -> int:
        try:
            if msg == win32.WM_MOUSEMOVE:
                self._on_mouse_move(lparam)
                return 0
            if msg == win32.WM_LBUTTONDOWN:
                self._begin_drag(lparam)
                return 0
            if msg == win32.WM_LBUTTONUP:
                self._end_drag()
                return 0
            if msg == win32.WM_CAPTURECHANGED:
                # 拖动中别人把捕获抢走(或窗口被 SW_HIDE:隐藏持有捕获的窗口会释放捕获):
                # 之后的 WM_LBUTTONUP 不会再送到我们这里,不在这里收尾的话 `_dragging`
                # 会**永久**留在 True —— 栏从此黏在光标上、`on_drag_end` 再也不上报位置。
                self._end_drag()
                return 0
            if msg == win32.WM_MOUSELEAVE:
                # 只在「跟踪中」才上报离开:窗口自己移动时光标可能正好离开,
                # 系统会补一条 WM_MOUSELEAVE;重复上报会让下游的收起延时被取消两次。
                if self._tracking_leave:
                    self._tracking_leave = False
                    self._notify_hover(False)
                return 0
            if msg == win32.WM_TIMER:
                if int(wparam) == TOPMOST_TIMER_ID:
                    self._raise_topmost()
                return 0
            if msg == win32.WM_DESTROY:
                _windows.pop(int(hwnd or 0), None)
                self._hwnd = None
                self._running = False
                win32.user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 — 从 ctypes 回调里逃逸的异常会穿到 C 栈
            traceback.print_exc()
        return win32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_mouse_move(self, lparam: Any) -> None:
        if not self._tracking_leave and self._hwnd is not None:
            track = win32.TRACKMOUSEEVENT()
            track.cbSize = ctypes.sizeof(win32.TRACKMOUSEEVENT)
            track.dwFlags = win32.TME_LEAVE
            track.hwndTrack = self._hwnd
            track.dwHoverTime = 0
            if win32.user32.TrackMouseEvent(ctypes.byref(track)):
                self._tracking_leave = True
                self._notify_hover(True)

        if self._dragging:
            # 窗口跟着光标走:位移按**屏幕**坐标算,每移动 1px 窗口就补 1px。
            point = self._screen_point(lparam)
            self.move_to(
                self._drag_window[0] + point[0] - self._drag_origin[0],
                self._drag_window[1] + point[1] - self._drag_origin[1],
            )

    def _screen_point(self, lparam: Any) -> tuple[int, int]:
        """把鼠标消息的**客户端**坐标换成屏幕坐标。

        拖动必须用屏幕坐标:窗口自己在跟着光标走,而客户端坐标 = 鼠标屏幕坐标 −
        窗口当前位置,拿它做增量等于每帧都把自己刚走过的位移再扣一次
        (`新位置 = 按下位置 + 鼠标屏幕 − 窗口当前位置 − 按下时客户端坐标`)——
        真机实测:栏只走到鼠标位移的一半、跟不上手,并且来回抽搐。
        屏幕坐标是绝对的,窗口跟着动不会改变它,所以每帧算出的增量都基于同一起点。
        """
        point = win32.POINT(win32.loword(lparam), win32.hiword(lparam))
        if not win32.user32.ClientToScreen(self._hwnd, ctypes.byref(point)):
            raise OSError(f"ClientToScreen 失败: Win32 error {ctypes.get_last_error()}")
        return int(point.x), int(point.y)

    def _begin_drag(self, lparam: Any) -> None:
        if self._hwnd is None:
            return
        self._drag_origin = self._screen_point(lparam)
        self._drag_window = self._window_rect()[:2]
        # SetCapture 返回「上一个捕获窗口」,可能是 NULL,不能用返回值判断成败。
        win32.user32.SetCapture(self._hwnd)
        self._dragging = True
        self._notify_drag_start()

    def _end_drag(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        win32.user32.ReleaseCapture()
        if self._on_drag_end is not None and self._hwnd is not None:
            left, top = self._window_rect()[:2]
            self._on_drag_end(left, top)

    def _notify_hover(self, entered: bool) -> None:
        if self._on_hover_change is not None:
            self._on_hover_change(entered)

    def _notify_drag_start(self) -> None:
        if self._on_drag_start is not None:
            self._on_drag_start()

    # --- 主题与绘制 ---

    def set_theme(self, values: Mapping[str, Any]) -> None:
        self._theme_values = dict(values)

    def reset_scroll(self) -> None:
        """滚动偏移复位(新回复从第一句起看);不重绘,下一次 `render` 生效。

        偏移只在 `render` 内部维护,把这个动作收成公开入口,调用方就不必去写
        `canvas._offset` —— 私有字段一旦改名,那种写法会静默新建一个属性、真实状态不变。
        """
        self._offset = 0

    @staticmethod
    def next_offset(boxes: layout.LayoutBoxes, current_segment: int, previous_offset: int) -> int:
        """滚动偏移的下一次取值(纯计算,便于不建窗口地测)。

        有当前句时跟随它;没有当前句(< 0)时**保持**上次偏移 —— 用 -1 去调
        `follow_offset` 会命中 segment == -1 的 user/notice 行,把视图滚到底部。
        """
        if current_segment >= 0:
            return layout.follow_offset(boxes, current_segment, previous_offset)
        return min(previous_offset, boxes.max_scroll)

    def render(self, view: Any) -> None:
        if self._hwnd is None:
            return
        values = self._theme_values
        width = int(values.get("width", 460))
        boxes = view.boxes(width, theme_measure(values))

        # 滚动偏移只在 render 内维护,不写进 view
        self._offset = self.next_offset(boxes, int(view.current_segment), self._offset)

        collapsed = (not view.has_content()) and bool(values.get("idleCollapse", True))
        height = COLLAPSED_HEIGHT if collapsed else boxes.view_height + PADDING_Y * 2
        # 空状态 + 关闭「收起成小条」= 整窗隐藏(设计文档 §5.3),不能留一个透明空窗。
        self._content_visible = view.has_content() or bool(values.get("idleCollapse", True))

        self._resize(width, height)
        if view.has_content() or collapsed:
            buffer = compose.new_buffer(width, height)
            self.paint_into(buffer, view, boxes, self._offset)
            self._upload(buffer)
        self._apply_visibility()

    def _upload(self, buffer: compose.Buffer) -> None:
        self._ensure_dib(buffer.width, buffer.height)
        self._dib_array[:] = buffer.data

        left, top = self._window_rect()[:2]
        destination = win32.POINT(left, top)
        size = win32.SIZE(buffer.width, buffer.height)
        source = win32.POINT(0, 0)
        blend = win32.BLENDFUNCTION(win32.AC_SRC_OVER, 0, 255, win32.AC_SRC_ALPHA)
        if not win32.user32.UpdateLayeredWindow(
            self._hwnd, self._screen_dc,
            ctypes.byref(destination), ctypes.byref(size),
            self._memory_dc, ctypes.byref(source), 0,
            ctypes.byref(blend), win32.ULW_ALPHA,
        ):
            raise OSError(f"UpdateLayeredWindow 失败: Win32 error {ctypes.get_last_error()}")

    def _ensure_dib(self, width: int, height: int) -> None:
        if self._dib_bitmap and self._dib_size == (width, height):
            return
        self._release_dib()

        header = win32.BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
        header.biWidth = int(width)
        header.biHeight = -int(height)  # 自顶向下,与 compose.Buffer 的行序一致
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
        self._dib_bitmap = bitmap
        # 保持引用存活:UpdateLayeredWindow 之前这块内存不能被回收。
        self._dib_array = (ctypes.c_ubyte * (width * height * 4)).from_address(bits.value)
        self._dib_previous = win32.gdi32.SelectObject(self._memory_dc, bitmap)
        self._dib_size = (width, height)

    # --- 绘制入口(窗口与样张共用) ---

    @staticmethod
    def paint_into(
        buffer: compose.Buffer, view: Any, boxes: layout.LayoutBoxes, offset: int
    ) -> None:
        """把视图画进缓冲:背景(缓存)→ 高亮底 → 文字。

        调用方负责给出**新缓冲**(或已整块覆盖过的缓冲):两个填充函数是 source-over
        语义,在旧内容上重画会累积 alpha。
        """
        values = view.theme_values
        width = min(buffer.width, max(1, int(values.get("width", buffer.width))))
        collapsed = (not view.has_content()) and bool(values.get("idleCollapse", True))

        # 1. + 2. 背景与边框:整层拷贝缓存,常规重绘不付逐像素合成的钱
        layer = None
        if view.has_content() or collapsed:
            bar_height = min(
                COLLAPSED_HEIGHT if collapsed else boxes.view_height + PADDING_Y * 2,
                buffer.height,
            )
            layer = _background_layer(width, bar_height, values)
            _copy_layer(buffer, layer.buffer)
        if collapsed:
            return  # 空状态只有那条细条

        clip_top = PADDING_Y
        clip_bottom = min(buffer.height, PADDING_Y + boxes.view_height)
        overall = int(values.get("overallOpacity", 100))

        # 3. 高亮底色:当前句所在区块的整块矩形(先乘整体不透明度)。
        # 不能直接用矩形填:圆角处会画到轮廓之外、有边框时会盖住边框线。
        # 用背景层已算好的圆角覆盖度按行切片作掩膜,有边框时矩形再内缩 borderWidth。
        highlight = theme_module.resolve_color(values, "highlightBackground")
        current = int(getattr(view, "current_segment", -1))
        if highlight is not None and current >= 0 and layer is not None:
            block = [box for box in boxes.boxes if box.segment == current]
            if block:
                top = max(min(box.top for box in block) - offset + PADDING_Y, clip_top)
                bottom = min(
                    max(box.top + box.height for box in block) - offset + PADDING_Y, clip_bottom
                )
                if bottom > top:
                    inset = int(values.get("borderWidth", 0))
                    compose.blit_coverage(
                        buffer, inset, top, _band_mask(layer, inset, top, bottom),
                        theme_module.apply_overall_alpha(highlight, overall),
                    )

        # 4. 文字:每行光栅化一次,按 描边 → 阴影 → 本色 顺序合成
        surface = _raster()
        font_family = str(values.get("fontFamily", "Microsoft YaHei UI"))
        font_size = int(values.get("fontSize", 20))
        outline_width = int(values.get("textOutlineWidth", 0))
        outline_color = theme_module.apply_overall_alpha(
            theme_module.parse_color(values.get("textOutlineColor")) or (0, 0, 0, 255), overall
        )
        shadow = bool(values.get("textShadow", True))
        shadow_color = theme_module.apply_overall_alpha((0, 0, 0, SHADOW_ALPHA), overall)

        for box in boxes.boxes:
            top = PADDING_Y + box.top - offset  # 一律用 box.top/box.height,notice 行高不是整数倍
            if top + boxes.line_height <= clip_top or top >= clip_bottom:
                continue  # 整行在视口外:不必光栅化
            coverage = surface.text(box.text, font_family, font_size, boxes.wrap_width)
            if coverage.width <= 0 or coverage.height <= 0:
                continue
            if outline_width > 0:
                for delta_x, delta_y in OUTLINE_DIRECTIONS:
                    _blit_clipped(
                        buffer, PADDING_X + delta_x * outline_width,
                        top + delta_y * outline_width, coverage, outline_color,
                        clip_top, clip_bottom,
                    )
            if shadow:
                for delta_x, delta_y in SHADOW_OFFSETS:
                    _blit_clipped(
                        buffer, PADDING_X + delta_x, top + delta_y,
                        coverage, shadow_color, clip_top, clip_bottom,
                    )
            _blit_clipped(
                buffer, PADDING_X, top, coverage,
                _role_color(values, box, current, overall), clip_top, clip_bottom,
            )
