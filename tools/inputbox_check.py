"""输入框窗口(实现计划 Task 6)的开发期检查脚本:不进插件包,也不替代 tests/。

跑法(在仓库根目录):
    "D:/GitHub/Sakura/python/python.exe" tools/inputbox_check.py
    "D:/GitHub/Sakura/python/python.exe" tools/inputbox_check.py --real-click

`--real-click` 会**真的**移动光标并按下/松开左键一次(用来回答「点击聚焦」在
`LWA_COLORKEY` 分层窗口上到底能不能命中),点击前后还原光标位置与前台窗口。
它只把点击投在自己的两个窗口重叠处,不会点到别人的窗口上。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import canvas  # noqa: E402
import inputbox  # noqa: E402
import theme  # noqa: E402
import view  # noqa: E402
import win32  # noqa: E402

SEGMENTS = [{"text": "检查用台词", "translation": "检查用译文"}]

FAILURES: list[str] = []

# --- 本脚本自用的绑定(win32.py 没有,且不允许改 win32.py) ---
win32.user32.IsWindow.restype = wintypes.BOOL
win32.user32.IsWindow.argtypes = [wintypes.HWND]
win32.user32.SetCursorPos.restype = wintypes.BOOL
win32.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
win32.user32.mouse_event.restype = None
win32.user32.mouse_event.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.WPARAM
]
win32.gdi32.BitBlt.restype = wintypes.BOOL
win32.gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
win32.gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
win32.gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
win32.gdi32.GetDIBits.restype = ctypes.c_int
win32.gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SRCCOPY = 0x00CC0020
CONTROL_NOISE_LIMIT = 8  # 对照组允许的桌面自身变化像素数(实测 0.4s 内 0~1 个)
BACKDROP_GREEN = (0, 255, 0)  # B 段用的不透明栏底色(纯绿):洞与没上 alpha 都认得出
OPAQUE_FILL = (14, 14, 18)  # 主题 backgroundColor(#0E0E12)不上 alpha 时应有的样子

win32.user32.GetLayeredWindowAttributes.restype = wintypes.BOOL
win32.user32.GetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.COLORREF), ctypes.POINTER(ctypes.c_ubyte),
    ctypes.POINTER(wintypes.DWORD),
]
win32.user32.GetWindowRgn.restype = ctypes.c_int
win32.user32.GetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN]


def read_layered(hwnd: int) -> tuple[int, int, int] | None:
    """读回输入框的分层属性 (键色, alpha, flags);读不到返回 None。"""
    key = wintypes.COLORREF()
    alpha = ctypes.c_ubyte()
    flags = wintypes.DWORD()
    if not win32.user32.GetLayeredWindowAttributes(
        hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)
    ):
        return None
    return (int(key.value), int(alpha.value), int(flags.value))


def region_kind(hwnd: int) -> int:
    """窗口区域类型:0=失败,1=NULLREGION,2=SIMPLEREGION,3=COMPLEXREGION。"""
    probe = win32.gdi32.CreateRoundRectRgn(0, 0, 1, 1, 0, 0)
    try:
        return int(win32.user32.GetWindowRgn(hwnd, probe))
    finally:
        win32.gdi32.DeleteObject(probe)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(name)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def pump_for(window, box, seconds: float = 0.3) -> None:
    """抽一会儿队列:消息只有进了 pump 才会派发、也才会经过过滤器。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        window.pump(message_filter=box.filter_message)
        time.sleep(0.01)


def post_key(hwnd: int, key: int) -> None:
    """把一条 WM_KEYDOWN 投到队列(真实按键也是这个形状,只是目标是 EDIT)。"""
    win32.user32.PostMessageW(hwnd, win32.WM_KEYDOWN, key, 0)


def make_key_message(hwnd: int, key: int) -> wintypes.MSG:
    """手搓一条 MSG(域名叫 hWnd,与 ctypes.wintypes 一致)。"""
    message = wintypes.MSG()
    message.hWnd = hwnd
    message.message = win32.WM_KEYDOWN
    message.wParam = key
    message.lParam = 0
    return message


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(64)
    win32.user32.GetClassNameW(hwnd, buffer, 64)
    return buffer.value


def frame(rect: tuple[int, int, int, int]) -> tuple[bytes, int]:
    """BitBlt 一块屏幕到兼容位图,再 GetDIBits 读成自上而下的 BGRA 字节。

    不用逐点 `GetPixel`:本机实测它在桌面 DC 上约 10ms/点(460×36 要 165 秒),
    而且读回的通道值还会上下抖;BitBlt 一次 16ms,紧邻两次抓屏逐像素相同。
    """
    x, y, width, height = rect
    screen = win32.user32.GetDC(None)
    if not screen:
        raise OSError("GetDC(None) 失败")
    memory = win32.gdi32.CreateCompatibleDC(screen)
    bitmap = win32.gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = win32.gdi32.SelectObject(memory, bitmap)
    buffer = (ctypes.c_ubyte * (width * height * 4))()
    header = win32.BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # 自顶向下,行序与 rect 一致
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = win32.BI_RGB
    try:
        win32.gdi32.BitBlt(memory, 0, 0, width, height, screen, x, y, SRCCOPY)
        win32.gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(header), 0)
        return bytes(buffer), width
    finally:
        win32.gdi32.SelectObject(memory, previous)
        win32.gdi32.DeleteObject(bitmap)
        win32.gdi32.DeleteDC(memory)
        win32.user32.ReleaseDC(None, screen)


def pixels(raw: bytes, width: int) -> list[tuple[int, int, int]]:
    """BGRA 字节 → 逐像素 (r, g, b)。"""
    return [(raw[i + 2], raw[i + 1], raw[i]) for i in range(0, len(raw), 4)]


def differs(one: tuple[int, int, int], two: tuple[int, int, int]) -> bool:
    """两个像素是否有意义的差别:留 16 的余量,滤掉桌面自身/取整的抖动。"""
    return any(abs(a - b) > 16 for a, b in zip(one, two))


def changed_indices(one: list[tuple[int, int, int]], two: list[tuple[int, int, int]]) -> list[int]:
    return [index for index, (a, b) in enumerate(zip(one, two)) if differs(a, b)]


def run(real_click: bool) -> int:
    theme_values = theme.default_theme()
    font_size = int(theme_values["fontSize"])
    state = view.ViewState(theme_values)
    state.set_reply(SEGMENTS)

    submitted: list[str] = []
    focus_events: list[bool] = []
    drag_events: list[tuple[int, int]] = []

    window = canvas.CanvasWindow(
        theme_values,
        on_drag_end=lambda x, y: drag_events.append((x, y)),
    )
    window.show()
    window.render(state)

    box = inputbox.InputBox(
        submitted.append, on_focus_change=lambda value: focus_events.append(value)
    )
    box.apply_theme(theme_values)
    box.attach(window.geometry(), above=False)

    try:
        # --- 验证 1:窗口存在与可见性(Win32 查询) ---
        section("验证 1:窗口存在 / show() 可见 / hide() 不可见")
        hwnd = box.hwnd()
        check("1a 窗口已创建(IsWindow 为真)", bool(win32.user32.IsWindow(hwnd)), f"hwnd={hwnd}")
        check(
            "1b 窗口类名正确",
            class_name(hwnd) == inputbox.WINDOW_CLASS_NAME,
            class_name(hwnd),
        )
        check("1c 初始不可见(创建后未 show)", not box.visible())
        check(
            "1d 初始 IsWindowVisible 原始查询为假",
            not win32.user32.IsWindowVisible(hwnd),
        )
        box.show()
        check("1e show() 后可见", box.visible())
        check("1f show() 后 IsWindowVisible 原始查询为真", bool(win32.user32.IsWindowVisible(hwnd)))
        box.hide()
        check("1g hide() 后不可见", not box.visible())
        check("1h hide() 后 IsWindowVisible 原始查询为假", not win32.user32.IsWindowVisible(hwnd))

        # --- 验证 2:回车提交 ---
        section("验证 2:回车提交")
        box.show()
        box.set_text("测试文本")
        # 读私有属性只为把 WM_KEYDOWN 投给真实目标(EDIT);真实按键走的就是它
        post_key(box._edit, win32.VK_RETURN)
        pump_for(window, box)
        check("2a on_submit 收到 '测试文本'", submitted == ["测试文本"], repr(submitted))
        check("2b 提交后输入框已隐藏", not box.visible())
        check("2c 提交后草稿被清空", box.text() == "", repr(box.text()))

        # --- 验证 3:Esc 收起并保留草稿 ---
        section("验证 3:Esc 收起保留草稿")
        box.show()
        box.set_text("保留我")
        post_key(box._edit, win32.VK_ESCAPE)
        pump_for(window, box)
        check("3a on_submit 未再被调用", len(submitted) == 1, repr(submitted))
        check("3b 输入框已隐藏", not box.visible())
        check("3c 草稿仍是 '保留我'", box.text() == "保留我", repr(box.text()))

        # --- 验证 4:attach 几何 ---
        section("验证 4:attach 几何")
        box.attach((100, 200, 460, 120), above=False)
        rect = box.geometry()
        check("4a 下方:x == 100", rect[0] == 100, f"rect={rect}")
        check("4b 下方:y == 200 + 120 + 6", rect[1] == 200 + 120 + 6, f"rect={rect}")
        check("4c 宽度与画布一致(460)", rect[2] == 460, f"rect={rect}")
        check("4d 窗口高 == fontSize + 16", rect[3] == font_size + inputbox.HEIGHT_EXTRA, f"rect={rect}")
        box.attach((100, 200, 460, 120), above=True)
        above_rect = box.geometry()
        check(
            "4e 上方:y == 200 - 高 - 6",
            above_rect[1] == 200 - above_rect[3] - 6,
            f"rect={above_rect}",
        )
        check("4f 上方:x 与宽度不变", above_rect[0] == 100 and above_rect[2] == 460, f"rect={above_rect}")

        # --- 验证 5:空文本回车不提交 ---
        section("验证 5:空文本回车不提交")
        box.attach(window.geometry(), above=False)
        box.show()
        box.set_text("")
        check("5a 前置:此刻确实可见(否则这条会因隐藏而假通过)", box.visible())
        post_key(box._edit, win32.VK_RETURN)
        pump_for(window, box)
        check("5b 空文本:on_submit 未被调用", len(submitted) == 1, repr(submitted))
        box.show()
        box.set_text("   ")
        post_key(box._edit, win32.VK_RETURN)
        pump_for(window, box)
        check("5c 纯空白:on_submit 仍未被调用", len(submitted) == 1, repr(submitted))

        # --- 加分项:过滤器边界 / 负对照 / 幂等 ---
        section("加分项:过滤器边界 / 负对照 / 幂等")
        foreign = make_key_message(int(window._hwnd), win32.VK_RETURN)  # 同线程的另一个窗口
        check(
            "A1 发给别的窗口的回车不被接管(filter 返回 False)",
            box.filter_message(foreign) is False,
        )
        other_key = make_key_message(box._edit, 0x25)  # VK_LEFT
        check("A2 非回车/Esc 的按键不被接管", box.filter_message(other_key) is False)
        ours = make_key_message(box._edit, win32.VK_RETURN)
        box.set_text("边界")
        check("A3 发给本输入框 EDIT 的回车被接管", box.filter_message(ours) is True)
        check("A4 接管即提交", submitted == ["测试文本", "边界"], repr(submitted))

        # 负对照:不传过滤器时 pump 只 Translate/Dispatch,回车不会变成提交
        box.show()
        box.set_text("负对照")
        post_key(box._edit, win32.VK_RETURN)
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            window.pump()  # 故意不传 message_filter
            time.sleep(0.01)
        check("A5 负对照:不传过滤器时回车不提交(说明过滤器才是关键)", len(submitted) == 2,
              repr(submitted))

        box.set_text("幂等草稿")
        box.show()
        box.show()
        first = box.geometry()
        box.show()
        check("A6 重复 show() 幂等(几何不变)", box.geometry() == first and box.visible())
        check("A7 重复 show() 不清草稿", box.text() == "幂等草稿", repr(box.text()))
        box.hide()
        box.hide()
        check("A8 重复 hide() 幂等(不可见)", not box.visible())
        check("A9 hide() 不清草稿", box.text() == "幂等草稿", repr(box.text()))
        box.show()
        check("A10 hide() 后 show() 草稿仍在", box.text() == "幂等草稿")
        box.set_text("")

        # --- 加分项:真的上屏了(BitBlt 抓屏比较) ---
        section("加分项:上屏与外观(BitBlt 抓屏比较)")
        # 背景用**自己画的不透明栏**当底:被抓的区域于是完全由本进程绘制,
        # 不受壁纸/别的窗口变化影响 —— 否则抓屏比较会随机失败(实测过)。
        # 底色选纯绿:既能认出「洞」(= 纯背景色),也能认出「没上 alpha」的实心色。
        backdrop = dict(
            theme_values, backgroundOpacity=100, backgroundColor="#00FF00", cornerRadius=0
        )
        window.set_theme(backdrop)
        state.set_theme(backdrop)
        window.render(state)
        canvas_rect = window.geometry()
        box_height = int(box.geometry()[3])
        # attach(above=False) 的落点 = y + height + GAP:喂错位矩形就能让输入框压在栏上
        overlap = (
            canvas_rect[0],
            canvas_rect[1] - box_height - inputbox.GAP,
            canvas_rect[2],
            box_height,
        )
        box.attach(overlap, above=False)
        box.hide()
        rect = box.geometry()
        raw_one, width = frame(rect)
        raw_two, _width = frame(rect)
        base = pixels(raw_one, width)
        control = changed_indices(base, pixels(raw_two, width))
        check(
            "B0 对照组:未显示时两次抓屏零差异",
            len(control) <= CONTROL_NOISE_LIMIT,
            f"{len(control)}/{len(base)}",
        )

        box.show()
        box.set_text("字幕栏Abc")
        pump_for(window, box, 0.3)
        box.attach(overlap, above=False)  # 再顶一次:画布每 3 秒会重设置顶
        shown = pixels(*frame(box.geometry()))
        changed = changed_indices(base, shown)
        if len(changed) < 20:
            print("    重试一次:输入框可能被画布临时盖住(画布每 3 秒重设置顶)", flush=True)
            box.attach(overlap, above=False)
            pump_for(window, box, 0.2)
            shown = pixels(*frame(box.geometry()))
            changed = changed_indices(base, shown)

        # 注:B1/B2 只能证明「这块被画成了非键色 / 带了 alpha」,
        # 分不清是「父窗口填充」还是「EDIT 自绘底色」—— 两者都会让这块区域变样。
        check(
            "B1 显示后整块区域都被画过(= 底色/输入区不是键色,鼠标点得中)",
            len(changed) * 10 >= len(base) * 9,
            f"{len(changed)}/{len(base)}",
        )
        holes = [index for index in changed if shown[index] == BACKDROP_GREEN]
        check("B2 没有键色洞(没有像素等于纯背景色)", len(holes) == 0, f"{len(holes)}")
        opaque = [index for index in changed if shown[index] == OPAQUE_FILL]
        check(
            "B3 底色带了整体 alpha(不是不透明的主题色块)",
            len(opaque) == 0,
            f"不透明底色像素 {len(opaque)}",
        )
        bright = [
            index for index in changed if min(shown[index]) > 140 and max(shown[index]) > 220
        ]
        check("B4 有近白的字形像素(= 主题文字色真的画上去了)", len(bright) >= 10, f"{len(bright)}")
        corner_indices = [
            width + 1,
            width + width - 2,
            (box_height - 2) * width + 1,
            (box_height - 2) * width + width - 2,
        ]
        corner_same = sum(1 for index in corner_indices if not differs(base[index], shown[index]))
        check(
            "B5 圆角之外仍是背景(区域塑形把四角裁掉了)",
            corner_same == len(corner_indices),
            f"{corner_same}/{len(corner_indices)}",
        )
        box.set_text("")
        box.hide()
        window.set_theme(theme_values)
        state.set_theme(theme_values)
        window.render(state)
        box.attach(window.geometry(), above=False)

        # --- 加分项:外观读回 / 主题护栏 / 几何回归(本轮评审要求) ---
        section("加分项:外观读回 / 主题护栏 / 几何回归")
        layer = read_layered(hwnd)
        check(
            "E1 分层属性读回:键色 + LWA_COLORKEY|LWA_ALPHA 两个标志都在",
            layer is not None and layer[0] == 0 and (layer[2] & 0x3) == 0x3,
            f"(键色, alpha, flags)={layer}",
        )
        expected_alpha = round(
            int(theme_values["backgroundOpacity"]) / 100
            * int(theme_values["overallOpacity"]) / 100
            * 255
        )
        check(
            "E2 alpha 读回 = backgroundOpacity × overallOpacity × 255",
            layer is not None and layer[1] == expected_alpha,
            f"alpha={layer[1] if layer else None} 期望={expected_alpha}",
        )
        box.apply_theme(dict(theme_values, backgroundOpacity=30, overallOpacity=50))
        layer = read_layered(hwnd)
        check(
            "E3 apply_theme 后 alpha 立刻重新施加(30% × 50%)",
            layer is not None and layer[1] == round(0.30 * 0.50 * 255),
            f"alpha={layer[1] if layer else None}",
        )
        box.apply_theme(theme_values)
        check(
            "E4 窗口区域已塑形(GetWindowRgn 非空)",
            region_kind(hwnd) in (2, 3),
            f"region kind={region_kind(hwnd)}",
        )

        # 改字号:above=True 的 top 必须按新高度现算,不能压住画布
        box.attach((100, 200, 460, 120), above=True)
        box.apply_theme(dict(theme_values, fontSize=32))
        bigger = box.geometry()
        check(
            "E5 改字号后 above=True 的位置按新高度重算",
            bigger[1] == 200 - bigger[3] - inputbox.GAP
            and bigger[3] == 32 + inputbox.HEIGHT_EXTRA,
            f"rect={bigger} 期望 y={200 - bigger[3] - inputbox.GAP}",
        )
        check(
            "E6 放大后没有压住画布(底边仍在画布顶边之上)",
            bigger[1] + bigger[3] <= 200 - inputbox.GAP,
            f"底边={bigger[1] + bigger[3]} 画布顶边=200",
        )
        box.apply_theme(theme_values)

        # 未 attach 就 show:位置必须整块落在工作区内(别掉到任务栏之下)
        loose = inputbox.InputBox(lambda _text: None)
        loose.apply_theme(dict(theme_values, fontSize=72))
        loose.show()
        loose_rect = loose.geometry()
        work = win32.RECT()
        win32.user32.SystemParametersInfoW(win32.SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
        check(
            "E7 未 attach 的大字号输入框整块在工作区内(下边界也夹住了)",
            loose_rect[1] >= work.top and loose_rect[1] + loose_rect[3] <= work.bottom,
            f"rect={loose_rect} 工作区=({work.top}, {work.bottom})",
        )
        loose.close()

        # 纯黑 textColor:护栏要不让字形被键色透成洞
        box.apply_theme(
            dict(
                theme_values, backgroundColor="#FFFFFF", backgroundOpacity=100,
                overallOpacity=100, textColor="#000000",
            )
        )
        box.attach(overlap, above=False)
        box.set_text("黑字")
        box.show()
        pump_for(window, box, 0.3)
        box.attach(overlap, above=False)
        black_rect = box.geometry()
        black_frame = pixels(*frame(black_rect))
        ink = sum(
            1
            for row in range(inputbox.MARGIN_Y, inputbox.MARGIN_Y + 26)
            for column in range(inputbox.MARGIN_X, inputbox.MARGIN_X + 60)
            if max(black_frame[row * black_rect[2] + column]) <= 100
        )
        # 只数 EDIT 区域:窗口矩形里还包含圆角之外露出的背景,那不是输入框画的
        keyed = sum(
            1
            for row in range(inputbox.MARGIN_Y, inputbox.MARGIN_Y + 26)
            for column in range(inputbox.MARGIN_X, black_rect[2] - inputbox.MARGIN_X)
            if black_frame[row * black_rect[2] + column] == (0, 0, 0)
        )
        check(
            "E8 纯黑 textColor 不打洞:白底上仍有深色字形像素",
            ink >= 5,
            f"字形像素={ink}(被键色透掉的话这里会是 0)",
        )
        check("E9 EDIT 区域内没有键色像素(= 文字没被打洞)", keyed == 0, f"键色像素={keyed}")
        box.set_text("")
        box.apply_theme(theme_values)
        box.hide()

        # --- 加分项:focus() 与焦点回调(EN_SETFOCUS / EN_KILLFOCUS 链路) ---
        section("加分项:focus() 与焦点回调")
        box.show()
        box.focus()
        # 立刻读:SetFocus/WM_SETFOCUS 是同步的;放到抽队列之后再读会被
        # 「别的程序抢走前台」这种环境噪声打成偶发失败(本机实测到过)。
        focus_now = int(win32.user32.GetFocus() or 0)
        check(
            "D1 focus() 后 EDIT 立刻拿到键盘焦点",
            box.focused(),
            f"GetFocus={focus_now}({class_name(focus_now) if focus_now else None}) edit={box._edit}",
        )
        check("D2 on_focus_change(True) 已上报", focus_events[-1:] == [True], repr(focus_events))
        box.set_text("焦点草稿")
        # 立刻收起再断言:输入框拿着焦点期间,机器上真实按下的回车会落进它并提交/清稿
        # (本机实测到过),中间插一段抽队列就会把 D5 变成偶发失败。
        box.hide()
        check("D3 hide() 后焦点已交还(不在 EDIT 上)", not box.focused())
        check("D4 on_focus_change(False) 已上报", focus_events[-1:] == [False], repr(focus_events))
        check("D5 收起不影响草稿", box.text() == "焦点草稿", repr(box.text()))
        pump_for(window, box)
        print(
            f"    观察:抽队列 0.3s 后 focused={box.focused()} "
            f"前台={int(win32.user32.GetForegroundWindow() or 0)}"
            "(别的窗口抢前台会让 EDIT 正常失焦,不作判据)",
            flush=True,
        )
        box.set_text("")

        # --- 可选:真实鼠标点击 ---
        click_result = None
        if real_click:
            section("加分项:真实鼠标点击(移动光标 + 按左键)")
            click_result = try_real_click(window, box, drag_events, theme_values)

        # --- 收尾:close() ---
        section("收尾:close()")
        hwnd_before = box.hwnd()
        box.close()
        box.close()
        check("C1 close() 幂等且窗口已销毁", not bool(win32.user32.IsWindow(hwnd_before)))
        check("C2 close() 后 geometry() 返回 None", box.geometry() is None)
        check("C3 close() 后 visible() 为假", not box.visible())
        box.set_text("x")  # 不该抛
        check("C4 close() 后 set_text/text() 不抛且为空", box.text() == "", repr(box.text()))
    finally:
        box.close()
        window.close()

    print(f"\n{'全部通过' if not FAILURES else '失败项: ' + ', '.join(FAILURES)}", flush=True)
    if click_result is not None:
        print(f"真实点击结论:{click_result}", flush=True)
    return 1 if FAILURES else 0


def try_real_click(window, box, drag_events: list[tuple[int, int]], theme_values: dict) -> str:
    """把输入框挪到画布正上方,注入一次真实左键点击,看谁拿到了它。

    点击只会落在自己的两个窗口重叠处:命中输入框 → EDIT 拿到焦点;
    穿透输入框 → 落到画布上(画布是 NOACTIVATE,不会改动前台窗口)。
    动手之前先用「红色标记文字」证明输入框确实压在栏之上,否则穿透结论不成立。
    """
    box_height = int(box.geometry()[3])
    canvas_rect = window.geometry()
    overlap = (
        canvas_rect[0],
        canvas_rect[1] - box_height - inputbox.GAP,
        canvas_rect[2],
        box_height,
    )
    box.attach(overlap, above=False)
    box.set_text("点我")
    box.show()
    # 标记色:栏里的文字是白的,红字只可能来自输入框 → 用它确认 z 序。
    # 顺带把不透明度调到 100,免得整体 alpha 把红字冲淡、判据变钝。
    # 整个点击阶段都保持这个主题(每次点击前都要靠红字确认置顶),最后再还原。
    box.apply_theme(dict(theme_values, textColor="#FF0000", backgroundOpacity=100))
    box.attach(overlap, above=False)  # attach 会按 HWND_TOPMOST 重新置顶
    pump_for(window, box, 0.2)
    box.attach(overlap, above=False)
    marker = pixels(*frame(box.geometry()))
    red = sum(1 for value in marker if value[0] > 200 and value[1] < 90 and value[2] < 90)
    print(f"    z 序校验:输入框红色标记像素 {red} 个(>0 说明输入框在栏之上)", flush=True)
    if red == 0:
        box.apply_theme(theme_values)
        return "未做:没能确认输入框压在栏之上(画布每 3 秒重设置顶),跳过点击"

    rect = box.geometry()
    point = win32.POINT()
    win32.user32.GetCursorPos(ctypes.byref(point))
    saved_cursor = (point.x, point.y)
    saved_foreground = int(win32.user32.GetForegroundWindow() or 0)
    drag_events.clear()
    print(f"    输入框 {rect} 画布 {canvas_rect} 画布每 3 秒会重设置顶,所以每次点击前重贴", flush=True)

    def ensure_on_top() -> bool:
        """置顶后确认输入框真的画在最上层(红色标记可见再点)。

        画布每 3 秒会把自己重设置顶,盖住输入框的那一瞬间点击会打到画布上,
        那是测量装置的假阴性,不是输入框的问题 —— 所以每次点击前都验一次。
        """
        for _ in range(4):
            box.attach(overlap, above=False)  # attach 会按 HWND_TOPMOST 重新置顶
            strip = pixels(*frame((rect[0] + inputbox.MARGIN_X, rect[1] + inputbox.MARGIN_Y, 80, 20)))
            red = sum(1 for value in strip if value[0] > 200 and value[1] < 90 and value[2] < 90)
            if red > 0:
                return True
            time.sleep(0.05)
        return False

    def click(point_x: int, point_y: int, tag: str) -> tuple[bool, int, list]:
        """点一下并返回 (EDIT 是否拿到焦点, 前台窗口, 画布收到的拖动)。

        环境上有两种会污染结论的干扰,都做重试(判据只看「输入框在最上层且点中了它」):
        ① 画布每 3 秒重设置顶,可能在这一瞬盖住输入框 → 那一次点击会打到画布;
        ② 输入框刚拿到前台时,机器上真实按下的回车/Esc 会落进它(空文本回车只是收起,
           不会提交)—— 表现是抽队列后输入框已经收起。
        两种都不是输入框的问题,所以重试;重试用尽仍失败才如实上报。
        """
        for attempt in range(1, 7):
            on_top = ensure_on_top()
            from_point = class_name(int(win32.user32.WindowFromPoint(win32.POINT(point_x, point_y)) or 0))
            drag_events.clear()
            steps = [f"置顶后可见={box.visible()}"]
            win32.user32.SetCursorPos(point_x, point_y)
            time.sleep(0.02)
            steps.append(f"移光标后可见={box.visible()}")
            win32.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            steps.append(f"按下后可见={box.visible()}")
            win32.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            steps.append(f"抬起后可见={box.visible()}")
            end = time.monotonic() + 0.4
            while time.monotonic() < end:
                window.pump(message_filter=box.filter_message)
                time.sleep(0.01)
            got = box.focused()
            focus_now = int(win32.user32.GetFocus() or 0)
            print(
                f"    [{tag}] 第{attempt}次 置顶确认={on_top} 点 ({point_x}, {point_y}) "
                f"WindowFromPoint={from_point} → focused={got} "
                f"GetFocus={focus_now}({class_name(focus_now) if focus_now else None}) "
                f"前台={class_name(int(win32.user32.GetForegroundWindow() or 0))} "
                f"{' '.join(steps)} 画布收到拖动={list(drag_events)}",
                flush=True,
            )
            if not box.visible():
                print("      输入框在点击期间被收起(外部按键/前台切换),重试", flush=True)
                box.show()
                continue
            if drag_events:
                print("      画布临时抢顶吃掉了这次点击,重试", flush=True)
                continue
            return got, int(win32.user32.GetForegroundWindow() or 0), list(drag_events)
        return got, int(win32.user32.GetForegroundWindow() or 0), list(drag_events)

    try:
        center = (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
        focus_center, foreground, center_drags = click(*center, tag="EDIT 中心")
        # 边距:上边缘中点,在窗口内但在 EDIT 之外
        margin = (rect[0] + rect[2] // 2, rect[1] + 3)
        box.hide()  # 先把焦点交还,免得上一轮的焦点让这轮变成假通过
        pump_for(window, box, 0.1)
        box.show()
        focus_margin, _foreground2, margin_drags = click(*margin, tag="上边缘(EDIT 之外)")
    finally:
        box.apply_theme(theme_values)  # 还原标记主题
        win32.user32.SetCursorPos(saved_cursor[0], saved_cursor[1])
        if saved_foreground and int(win32.user32.GetForegroundWindow() or 0) != saved_foreground:
            win32.user32.SetForegroundWindow(saved_foreground)

    print(f"    前台={foreground} 输入框={box.hwnd()} 画布={window._hwnd}", flush=True)
    if focus_center and focus_margin:
        return "命中:真实鼠标点在 EDIT 上与边距上都让 EDIT 拿到了键盘焦点"
    if focus_center and center_drags:
        return "半命中:EDIT 上可聚焦,但边距穿透到画布"
    if not focus_center and center_drags:
        return "未命中:点击穿透到画布(LWA_COLORKEY 的键色像素不参与命中测试)"
    return "未命中且画布也没收到:需要人工确认(可能是注入被系统拦截)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inputbox_check")
    parser.add_argument("--real-click", action="store_true", help="注入一次真实鼠标点击")
    args = parser.parse_args(argv)
    return run(args.real_click)


if __name__ == "__main__":
    raise SystemExit(main())
