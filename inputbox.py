"""输入框窗口:原生 EDIT 子控件 + 圆角/透明跟随主题 + 输入法候选窗定位。

为什么单独一个窗口(设计文档 §5.2):逐像素透明的分层窗口
(`UpdateLayeredWindow`)无法正常渲染子控件,而自绘输入框承载不了输入法
候选窗。输入框因此用 `WS_EX_LAYERED` + `LWA_COLORKEY`(键色 = 纯黑),
子控件照常渲染,输入法也就能把候选窗锚到 EDIT 上。

**命中测试按「该点的合成像素」判定**(早期记录写过"子控件救不了命中测试",
经独立实测与本次复测证伪):键色像素确实会穿透(`WindowFromPoint` 会跳过
整窗键色的窗口),但**子控件自己的不透明像素是算数的** —— 父窗口全区键色、
只让 EDIT 画成主题色时,点 EDIT 能正常聚焦、边距则穿透(本次实测:EDIT 中心
`WindowFromPoint` = `Edit`、真实点击聚焦成功;上边缘穿透到下层画布)。
所以本窗口的形态是:**客户区实心绘制**(主题 `backgroundColor`)+ **圆角区域塑形**
(`SetWindowRgn`)+ **整体 alpha 跟随主题**,`LWA_COLORKEY` 只当兜底护栏
(主题色撞键色时防止那一块被透掉)。整块(含边距)都可点。

按键**不在这里抽队列**:焦点在 EDIT 子控件上,按键消息发给 EDIT,父窗口
过程看不到。改为 `filter_message()` 交给画布的 `pump()` 在 Translate/
Dispatch 之前过滤(等价于对话框的 `IsDialogMessage`,无需子类化 EDIT)。
本模块**没有 pump** —— 线程的消息队列只在画布的 pump 一处抽干,两个
drainer 会互相吞消息。
"""

from __future__ import annotations

import ctypes
import traceback
from ctypes import wintypes
from typing import Any, Callable, Mapping

import theme as theme_module
import win32

WINDOW_CLASS_NAME = "SakuraLyricBarInputBox"

EDIT_CONTROL_ID = 1
MARGIN_X = 8  # EDIT 相对窗口的内缩(左右)
MARGIN_Y = 8
EDIT_HEIGHT_EXTRA = 6  # EDIT 高度 = fontSize + 6
HEIGHT_EXTRA = 16  # 窗口高度 = fontSize + 16
GAP = 6  # 与画布的垂直间距
BAR_BOTTOM_MARGIN = 80  # 未 attach() 时的占位高度:与画布默认位置(工作区底边 +80)配对
MIN_WIDTH = 40

# 键色:LWA_COLORKEY 的键。**只是兜底护栏**,正常情况下不该有像素等于它 ——
# 一旦有(主题背景色或文字色被设成纯黑),那块像素会被透掉且点不中,所以
# `_background_colorref()` / `_text_colorref()` 都会把撞上键色的值错开一位。
KEY_COLOR = 0x000000
BLACK_BRUSH = 4  # GetStockObject 的 stock object 编号(win32.py 未收这个常量)
FALLBACK_EDIT_BACKGROUND = (14, 14, 18, 255)  # 主题给不出背景色时的兜底(同 theme 默认值)
MIN_ALPHA = 1  # alpha 下限:主题把不透明度调到 0 时窗口会整块穿透、点不中,留 1/255 保底

# --- win32.py 里没有的常量与函数:就近在本模块绑定,未扩改 win32.py ---
SWP_NOZORDER = 0x0004
WM_ERASEBKGND = 0x0014
WM_SETFONT = 0x0030
EN_SETFOCUS = 0x0100
EN_KILLFOCUS = 0x0200
GCS_COMPSTR = 0x0008
ERROR_CLASS_ALREADY_EXISTS = 1410

win32.user32.GetFocus.restype = wintypes.HWND
win32.user32.GetFocus.argtypes = []
win32.user32.SetForegroundWindow.restype = wintypes.BOOL
win32.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
# 组合串长度:lpBuf = NULL / dwBufLen = 0 时返回字节数(>0 表示有未提交的组合)。
win32.imm32.ImmGetCompositionStringW.restype = ctypes.c_long
win32.imm32.ImmGetCompositionStringW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]

# 窗口过程必须由模块级引用持住:WNDPROC 包装对象一旦被 GC,
# 注册到窗口类里的函数指针就会悬空。
_wndproc_ref: Any = None
_class_registered = False

_boxes: dict[int, "InputBox"] = {}
_solid_brushes: dict[int, int] = {}


def _colorref(color: tuple[int, int, int, int]) -> int:
    """(r, g, b, a) → COLORREF(0x00BBGGRR)。"""
    r, g, b, _a = color
    return (b << 16) | (g << 8) | r


def _field(msg: Any, *names: str) -> int:
    """从消息里取整数域,取不到返回 0。

    `ctypes.wintypes.MSG` 的窗口句柄域叫 **`hWnd`**(与 SDK 一致),不是 `hwnd`;
    手搓的替身对象两种写法都有,所以两个名字都认 —— 踩过一次:hwnd 取成 0 时
    过滤器对任何真实消息都不匹配,表现就是「回车没反应」。
    """
    for name in names:
        value = getattr(msg, name, None)
        if value is not None:
            return int(value)
    return 0


def _default_position(width: int, height: int) -> tuple[int, int]:
    """未 `attach()` 时的占位位置:工作区底部居中下方(与画布默认位置配对)。

    画布高度随内容变化,这里只是没有画布信息时的兜底;真正的位置由 attach() 给出。
    """
    work = win32.RECT()
    if not win32.user32.SystemParametersInfoW(
        win32.SPI_GETWORKAREA, 0, ctypes.byref(work), 0
    ):
        return (0, 0)
    left = work.left + max(0, (work.right - work.left - width) // 2)
    top = work.bottom - BAR_BOTTOM_MARGIN + GAP  # 与画布默认位置的下边缘对齐后再往下让 6px
    # 上/下/右三边都夹住:只夹上边界的话,字大或工作区矮时会掉到任务栏之下
    left = max(work.left, min(left, work.right - width))
    return (left, max(work.top, min(top, work.bottom - height)))


def _dispatch(hwnd: Any, msg: int, wparam: Any, lparam: Any) -> int:
    box = _boxes.get(int(hwnd or 0))
    if box is None:
        return win32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return box._handle(hwnd, msg, wparam, lparam)


def _solid_brush(colorref: int) -> int:
    """按颜色取/建实心刷子(EDIT 底色用),同色复用。

    不删:刷子会被 EDIT 在绘制期间持续使用,删早了等于让它画到已释放的
    GDI 对象上。颜色种类 = 进程里出现过的主题背景色数量,就那么几个。
    """
    brush = _solid_brushes.get(colorref)
    if brush is None:
        brush = int(win32.gdi32.CreateSolidBrush(colorref) or 0)
        _solid_brushes[colorref] = brush
    return brush


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
    window_class.hCursor = win32.user32.LoadCursorW(None, win32.IDC_IBEAM)  # 输入框用 I 型光标
    # 类背景刷只作初始兜底:真正的底色由 WM_ERASEBKGND 与创建时的 _paint_background()
    # 填主题背景色。兜底用黑板刷 = 键色,即使漏了一次也是「透明」而不是「一块错色」。
    window_class.hbrBackground = win32.gdi32.GetStockObject(BLACK_BRUSH)
    window_class.lpszMenuName = None
    window_class.lpszClassName = WINDOW_CLASS_NAME
    window_class.hIconSm = None

    if not win32.user32.RegisterClassExW(ctypes.byref(window_class)):
        error = ctypes.get_last_error()
        if error != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(f"RegisterClassExW 失败: Win32 error {error}")
    _class_registered = True


class InputBox:
    """原生 EDIT 输入框窗口:悬停出现、点击聚焦、回车提交、Esc 收起保留草稿。"""

    def __init__(
        self,
        on_submit: Callable[[str], None],
        on_focus_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._on_submit = on_submit
        self._on_focus_change = on_focus_change
        self._values: dict[str, Any] = theme_module.default_theme()

        self._hwnd: int | None = None
        self._edit: int | None = None
        self._font: int | None = None  # HFONT 句柄值(整数,便于当 WPARAM 传)
        self._box_size = (0, 0)
        self._edit_size = (0, 0)
        self._position = (0, 0)
        self._canvas_rect: tuple[int, int, int, int] | None = None
        self._above = False
        self._focused = False

        _register_class()
        self._create_window()
        self._apply_text_style()

    # --- 窗口生命周期 ---

    def _create_window(self) -> None:
        width = max(MIN_WIDTH, int(self._values.get("width", 460)))
        height = int(self._values.get("fontSize", 20)) + HEIGHT_EXTRA
        left, top = _default_position(width, height)
        style = win32.WS_EX_TOPMOST | win32.WS_EX_TOOLWINDOW | win32.WS_EX_LAYERED
        hwnd = win32.user32.CreateWindowExW(
            # WS_CLIPCHILDREN:父窗口刷底色时不许盖住 EDIT(否则要等 EDIT 自己重画)
            style, WINDOW_CLASS_NAME, "", win32.WS_POPUP | win32.WS_CLIPCHILDREN,
            left, top, width, height,
            None, None, win32.kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            raise OSError(f"CreateWindowExW 失败: Win32 error {ctypes.get_last_error()}")
        self._hwnd = int(hwnd)
        _boxes[self._hwnd] = self
        self._box_size = (width, height)

        # 圆角 + 整体 alpha(跟随主题);主题一变由 apply_theme 重新施加
        try:
            self._apply_layered_style()
        except OSError:
            self._destroy()
            raise

        edit = win32.user32.CreateWindowExW(
            0, "EDIT", "",
            win32.WS_CHILD | win32.WS_VISIBLE | win32.ES_LEFT | win32.ES_AUTOHSCROLL,
            MARGIN_X, MARGIN_Y,
            max(1, width - MARGIN_X * 2),
            int(self._values.get("fontSize", 20)) + EDIT_HEIGHT_EXTRA,
            self._hwnd, EDIT_CONTROL_ID, win32.kernel32.GetModuleHandleW(None), None,
        )
        if not edit:
            error = ctypes.get_last_error()
            self._destroy()
            raise OSError(f"创建 EDIT 子控件失败: Win32 error {error}")
        self._edit = int(edit)
        self._position = (left, top)
        self._box_size = (width, height)
        self._edit_size = (max(1, width - MARGIN_X * 2),
                           int(self._values.get("fontSize", 20)) + EDIT_HEIGHT_EXTRA)
        self._paint_background()

    def _background_colorref(self) -> int:
        """窗口底色 = 主题 `backgroundColor`。

        撞上键色(纯黑主题)时错开一位:键色那块像素会被透掉、也点不中。
        """
        background = _colorref(
            theme_module.parse_color(self._values.get("backgroundColor"))
            or FALLBACK_EDIT_BACKGROUND
        )
        return 0x010101 if background == KEY_COLOR else background

    def _text_colorref(self) -> int:
        """EDIT 文字色 = 主题 `textColor`。

        同样要躲开键色:纯黑文字会被 LWA_COLORKEY 透成一个个小洞 —— 既看不见也点不中。
        """
        text = _colorref(
            theme_module.parse_color(self._values.get("textColor")) or (255, 255, 255, 255)
        )
        return 0x010101 if text == KEY_COLOR else text

    def _layered_alpha(self) -> int:
        """整窗 alpha = 背景不透明度 × 整体不透明度(与栏的观感一致)。"""
        background = int(self._values.get("backgroundOpacity", 100))
        overall = int(self._values.get("overallOpacity", 100))
        alpha = int(round(background / 100.0 * overall / 100.0 * 255))
        # 下限 1:主题把不透明度调到 0 时整窗会穿透,输入框就再也点不中了
        return max(MIN_ALPHA, min(255, alpha))

    def _apply_layered_style(self) -> None:
        """圆角塑形 + 整体 alpha 都跟随主题(尺寸或主题变化时必须重新施加)。

        - 窗口区域外的部分既不显示也不参与命中测试,正是圆角想要的效果;
        - `LWA_COLORKEY | LWA_ALPHA` 两个标志可以同时用:键色像素透掉,
          其余像素按整体 alpha 与桌面合成(输入框里的文字也会一起半透明,
          原生 EDIT 做不到「只让背景透明」);
        - 键色只是兜底护栏,见 `_background_colorref()` / `_text_colorref()`。
        """
        if self._hwnd is None:
            return
        width, height = int(self._box_size[0]), int(self._box_size[1])
        radius = max(0, min(int(self._values.get("cornerRadius", 0)), min(width, height) // 2))
        # 注意 x2/y2 要比宽高大 1:区域是右开区间,否则最右/最下一列像素会被切掉
        region = win32.gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius * 2, radius * 2
        )
        if not region:
            raise OSError(f"CreateRoundRectRgn 失败: Win32 error {ctypes.get_last_error()}")
        if not win32.user32.SetWindowRgn(self._hwnd, region, True):
            # 失败时区域句柄没被系统接管,自己删掉,别漏 GDI 对象
            error = ctypes.get_last_error()
            win32.gdi32.DeleteObject(region)
            raise OSError(f"SetWindowRgn 失败: Win32 error {error}")
        # 成功后区域归系统所有,不要自己删
        if not win32.user32.SetLayeredWindowAttributes(
            self._hwnd, KEY_COLOR, self._layered_alpha(),
            win32.LWA_COLORKEY | win32.LWA_ALPHA,
        ):
            raise OSError(f"SetLayeredWindowAttributes 失败: Win32 error {ctypes.get_last_error()}")

    def _paint_background(self) -> None:
        """把窗口客户区刷成主题背景色。

        整块都实心绘制(含 EDIT 之外的边距),所以**整块都可点**;命中测试按该点的
        合成像素算,键色像素才会穿透 —— 底色的作用是不让这块变成键色(见模块开头)。
        """
        if self._hwnd is None:
            return
        hdc = win32.user32.GetDC(self._hwnd)
        if not hdc:
            return
        try:
            rect = win32.RECT(0, 0, int(self._box_size[0]), int(self._box_size[1]))
            win32.user32.FillRect(
                hdc, ctypes.byref(rect), _solid_brush(self._background_colorref())
            )
        finally:
            win32.user32.ReleaseDC(self._hwnd, hdc)

    def _destroy(self) -> None:
        hwnd = self._hwnd
        if hwnd is not None:
            # DestroyWindow 未绑定:WM_CLOSE 走 DefWindowProc 会自己销毁(同 canvas)
            win32.user32.SendMessageW(hwnd, win32.WM_CLOSE, 0, 0)
            _boxes.pop(int(hwnd), None)
            self._hwnd = None
            self._edit = None

    def close(self) -> None:
        self._destroy()
        if self._font is not None:
            # EDIT 已随父窗口销毁,这时删字体不会让控件画到已释放的对象上
            win32.gdi32.DeleteObject(self._font)
            self._font = None

    def show(self) -> None:
        """显示(**不激活**窗口,悬停唤出时不能抢走前台焦点)。重复调用幂等。"""
        hwnd = self._hwnd
        if hwnd is None:
            return
        if win32.user32.IsWindowVisible(hwnd):
            return  # 已经在显示:不重排、不重设 z 序,避免重复悬停事件造成闪烁
        win32.user32.ShowWindow(hwnd, win32.SW_SHOWNOACTIVATE)
        win32.user32.SetWindowPos(
            hwnd, win32.HWND_TOPMOST, 0, 0, 0, 0,
            win32.SWP_NOMOVE | win32.SWP_NOSIZE | win32.SWP_NOACTIVATE,
        )

    def hide(self) -> None:
        """只 `SW_HIDE`:不销毁、不清空草稿。重复调用幂等。"""
        hwnd = self._hwnd
        if hwnd is None:
            return
        if self._edit is not None and int(win32.user32.GetFocus() or 0) == self._edit:
            # 焦点还在框里就先交还:否则隐藏后按键继续进 EDIT,等于隐形打字
            win32.user32.SetFocus(None)
        if not win32.user32.IsWindowVisible(hwnd):
            return
        win32.user32.ShowWindow(hwnd, win32.SW_HIDE)

    def focus(self) -> None:
        """显示 + 激活 + 把焦点交给 EDIT(设计 §5.1:需要键盘时由输入框去激活)。

        `SetForegroundWindow` 在后台进程里可能被系统拒绝(返回 0,Windows 的
        前台窗口限制),这时线程内的焦点仍然到位、真实点击也照常激活;
        返回值不用来判断成败。
        """
        self.show()
        if self._hwnd is None:
            return
        win32.user32.SetForegroundWindow(self._hwnd)
        if self._edit is not None:
            win32.user32.SetFocus(self._edit)
        self._position_ime()

    # --- 主题与几何 ---

    def apply_theme(self, values: Mapping[str, Any] | None) -> None:
        """主题投影后就地更新(字号影响窗口高度、字体与文字色)。"""
        self._values = theme_module.project_theme(values if values is not None else {})
        self._apply_text_style()
        self._apply_layout()

    def attach(self, canvas_rect: tuple[int, int, int, int], above: bool = False) -> None:
        """贴住画布 `(x, y, width, height)`:同宽、左对齐、垂直间距 6px。

        `above=True` 放画布上边缘上方(屏幕底部放不下时由调用方判断),否则放下方。
        位置每次布局都按**当前**尺寸重算,所以之后主题改字号也不会错位。
        """
        x, y, width, height = (int(value) for value in canvas_rect)
        self._canvas_rect = (x, y, width, height)
        self._above = bool(above)
        self._apply_layout()

    def _apply_layout(self) -> None:
        """按当前主题与画布矩形重排窗口与 EDIT(隐藏时也生效,先摆好再显示)。"""
        if self._hwnd is None:
            return
        width = max(
            MIN_WIDTH,
            int(self._canvas_rect[2]) if self._canvas_rect else int(self._values.get("width", 460)),
        )
        font_size = int(self._values.get("fontSize", 20))
        height = font_size + HEIGHT_EXTRA
        edit_width = max(1, width - MARGIN_X * 2)
        edit_height = font_size + EDIT_HEIGHT_EXTRA
        self._box_size = (width, height)
        self._edit_size = (edit_width, edit_height)

        # 位置按**当前**高度现算:attach() 时算死的话,apply_theme 改字号会变高,
        # above=True 就会错位、甚至压住画布。
        if self._canvas_rect is not None:
            canvas_x, canvas_y, _canvas_width, canvas_height = self._canvas_rect
            left = canvas_x
            top = canvas_y - height - GAP if self._above else canvas_y + canvas_height + GAP
        else:
            left, top = _default_position(width, height)
        self._position = (left, top)

        win32.user32.SetWindowPos(
            self._hwnd, win32.HWND_TOPMOST, int(left), int(top), width, height,
            win32.SWP_NOACTIVATE,
        )
        if self._edit is not None:
            win32.user32.SetWindowPos(
                self._edit, 0, MARGIN_X, MARGIN_Y, edit_width, edit_height,
                SWP_NOZORDER | win32.SWP_NOACTIVATE,
            )
        # 尺寸或主题变了:圆角区域与整体 alpha 都要重新施加,底色也要重刷
        self._apply_layered_style()
        self._paint_background()

    def geometry(self) -> tuple[int, int, int, int] | None:
        """窗口矩形 (x, y, 宽, 高);**已销毁时返回 None**(与 canvas 同约定)。"""
        if self._hwnd is None:
            return None
        rect = win32.RECT()
        if not win32.user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
            raise OSError(f"GetWindowRect 失败: Win32 error {ctypes.get_last_error()}")
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def hwnd(self) -> int | None:
        """输入框窗口句柄(已销毁返回 None);供调用方做原生查询。"""
        return self._hwnd

    def visible(self) -> bool:
        """当前是否真的可见(`IsWindowVisible`)。"""
        return self._hwnd is not None and bool(win32.user32.IsWindowVisible(self._hwnd))

    def focused(self) -> bool:
        """键盘焦点是否在本输入框的 EDIT 上(实时查询,不是缓存标志)。"""
        return self._edit is not None and int(win32.user32.GetFocus() or 0) == self._edit

    # --- 文本 ---

    def text(self) -> str:
        if self._edit is None:
            return ""
        length = int(win32.user32.GetWindowTextLengthW(self._edit))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 2)
        win32.user32.GetWindowTextW(self._edit, buffer, length + 1)
        return buffer.value

    def set_text(self, value: Any) -> None:
        if self._edit is None:
            return
        win32.user32.SetWindowTextW(self._edit, "" if value is None else str(value))

    # --- 按键过滤(由画布的 pump 调用;本模块不抽队列) ---

    def filter_message(self, msg: Any) -> bool:
        """接管发给本输入框的回车/Esc:返回 True 表示调用方**不得**再派发它。

        实现里所有异常都吞掉并返回 False:消息过滤在 pump 的主循环上,
        抛出去会把整个消息循环带崩。
        """
        try:
            return self._filter(msg)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False

    def _filter(self, msg: Any) -> bool:
        if self._edit is None:
            return False
        if _field(msg, "message") != win32.WM_KEYDOWN:
            return False
        # 只接管发给本输入框(EDIT 或窗口本体)的按键,别人的回车不碰
        target = _field(msg, "hWnd", "hwnd")
        if target != self._edit and target != self._hwnd:
            return False
        key = _field(msg, "wParam") & 0xFFFF
        if key not in (win32.VK_RETURN, win32.VK_ESCAPE):
            return False
        if self._is_composing():
            # 输入法组合中:按键归输入法 —— 回车是「上屏」不是「发送」,
            # Esc 是「取消组合」不是「收起输入框」。放行给 EDIT/输入法处理。
            return False
        if key == win32.VK_RETURN:
            self._submit()
        else:
            self.hide()  # 草稿保留
        return True

    def _submit(self) -> None:
        value = self.text().strip()
        try:
            if value and self._on_submit is not None:
                self._on_submit(value)
        except Exception:  # noqa: BLE001 — 回调炸了也不能把消息循环带走
            traceback.print_exc()
        finally:
            # 清空放在回调**之后**:Task 8 的提交回调要读 text() 再自行清空,
            # 提前清空会让它读到空串(见报告)。
            self.set_text("")
            self.hide()

    def _is_composing(self) -> bool:
        """输入法是否有未提交的组合串(GCS_COMPSTR 长度 > 0)。"""
        edit = self._edit
        if edit is None:
            return False
        context = win32.imm32.ImmGetContext(edit)
        if not context:
            return False
        try:
            return win32.imm32.ImmGetCompositionStringW(context, GCS_COMPSTR, None, 0) > 0
        finally:
            win32.imm32.ImmReleaseContext(edit, context)

    # --- 窗口过程 ---

    def _handle(self, hwnd: Any, msg: int, wparam: Any, lparam: Any) -> int:
        try:
            if msg == WM_ERASEBKGND:
                rect = win32.RECT(0, 0, int(self._box_size[0]), int(self._box_size[1]))
                win32.user32.FillRect(
                    wparam, ctypes.byref(rect), _solid_brush(self._background_colorref())
                )
                return 1
            if msg == win32.WM_CTLCOLOREDIT:
                # EDIT 的底色与窗口同色,文字色取主题 textColor(两者都躲开键色)
                background = self._background_colorref()
                win32.gdi32.SetTextColor(wparam, self._text_colorref())
                win32.gdi32.SetBkColor(wparam, background)
                return _solid_brush(background)
            if msg == win32.WM_SETFOCUS:
                # 点中窗口本体(EDIT 之外的边距)时把焦点转给 EDIT,并锚一次候选窗。
                # 候选窗定位的真实触发点:这里是「窗口本体拿到焦点」,
                # 更常见的是下面 WM_COMMAND 里的 EN_SETFOCUS(点进 EDIT)。
                # 注意 WM_IME_* 发给持有输入法上下文的窗口(EDIT),父窗口过程收不到,
                # 所以没有 WM_IME_STARTCOMPOSITION 分支 —— 那是不可达的死代码。
                if self._edit is not None:
                    win32.user32.SetFocus(self._edit)
                self._position_ime()
                return 0
            if msg == win32.WM_COMMAND:
                self._on_command(wparam)
                return 0
            if msg == win32.WM_DESTROY:
                _boxes.pop(int(hwnd or 0), None)
                self._hwnd = None
                self._edit = None
                return 0
        except Exception:  # noqa: BLE001 — 从 ctypes 回调里逃逸的异常会穿到 C 栈
            traceback.print_exc()
        return win32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_command(self, wparam: Any) -> None:
        """EDIT 的焦点通知:EN_SETFOCUS / EN_KILLFOCUS(WM_COMMAND 到父窗口)。"""
        if win32.loword(wparam) != EDIT_CONTROL_ID:
            return
        code = win32.hiword(wparam)
        if code == EN_SETFOCUS:
            self._set_focused(True)
            self._position_ime()
        elif code == EN_KILLFOCUS:
            self._set_focused(False)

    def _set_focused(self, value: bool) -> None:
        if value == self._focused:
            return  # 重复的焦点通知不外传,避免下游被同一状态刷两次
        self._focused = value
        if self._on_focus_change is not None:
            try:
                self._on_focus_change(value)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # --- 输入法 ---

    def _position_ime(self) -> None:
        """把输入法组合窗(候选窗)锚到 EDIT 左下角。

        调用点(`WM_SETFOCUS` 与 `EN_SETFOCUS`)就是**真实触发点**:`WM_IME_*`
        发给持有输入法上下文的窗口(EDIT),父窗口过程收不到,所以别指望
        `WM_IME_STARTCOMPOSITION`;窗口过程里没有那个分支。
        若日后候选窗位置真的出问题,再考虑子类化 EDIT,不要提前做。

        `CFS_FORCE_POSITION` 的 `ptCurrentPos` 取「持有输入法上下文的窗口」
        (即 EDIT)的客户区坐标,所以这里是 `(0, EDIT 高度)`。
        """
        edit = self._edit
        if edit is None:
            return
        context = win32.imm32.ImmGetContext(edit)
        if not context:
            return
        try:
            form = win32.COMPOSITIONFORM()
            form.dwStyle = win32.CFS_FORCE_POSITION
            form.ptCurrentPos = win32.POINT(0, int(self._edit_size[1]))
            form.rcArea = win32.RECT(0, 0, 0, 0)
            win32.imm32.ImmSetCompositionWindow(context, ctypes.byref(form))
        finally:
            win32.imm32.ImmReleaseContext(edit, context)

    # --- 字体 ---

    def _apply_text_style(self) -> None:
        """EDIT 的字体与主题一致(设计 §5.2);文字色在 WM_CTLCOLOREDIT 里生效。"""
        if self._edit is None:
            return
        family = str(self._values.get("fontFamily") or "Microsoft YaHei UI")
        size = int(self._values.get("fontSize", 20))
        font = win32.gdi32.CreateFontW(
            -size, 0, 0, 0, 400, 0, 0, 0,
            win32.DEFAULT_CHARSET, win32.OUT_TT_PRECIS, win32.CLIP_DEFAULT_PRECIS,
            win32.ANTIALIASED_QUALITY, 0, family,
        )
        if not font:
            raise OSError(f"CreateFontW 失败: Win32 error {ctypes.get_last_error()}")
        previous = self._font
        self._font = int(font)  # HFONT 句柄值(restype 是 c_void_p,读回来就是 int)
        # 先换字体再删旧字体:EDIT 还握着旧句柄,先删会让它画到已释放的对象上
        win32.user32.SendMessageW(self._edit, WM_SETFONT, self._font, 1)
        if previous is not None:
            win32.gdi32.DeleteObject(previous)
