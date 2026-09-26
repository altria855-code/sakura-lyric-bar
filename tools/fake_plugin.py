"""假插件:启动浮窗子进程、按协议喂消息,验证浮窗主循环(实现计划 Task 8 Step 6)。

跑法(仓库根目录):
    "D:/GitHub/Sakura/python/python.exe" -B tools/fake_plugin.py                    # 全流程
    "D:/GitHub/Sakura/python/python.exe" -B tools/fake_plugin.py --eof-check         # stdin EOF 生命线
    "D:/GitHub/Sakura/python/python.exe" -B tools/fake_plugin.py --protocol-error-check
    "D:/GitHub/Sakura/python/python.exe" -B tools/fake_plugin.py --geometry-check    # 两窗几何 + 拖动
    "D:/GitHub/Sakura/python/python.exe" -B tools/fake_plugin.py --fullscreen-check  # 会短暂盖住主屏

比 brief 里的骨架多做的几件事 —— 否则「验证」只剩一个退出码:

1. 后台读浮窗的 stdout:握手(`ready`)与回传事件(`submit`/`moved`/`error`)要看得见才算验证过;
2. 跨进程给 EDIT 投「设置文本 + 回车」,走通提交回传链路 —— 真实按键走的就是这条路:
   按键发给 EDIT 子控件,由画布的 pump 先过输入框的过滤器(不传过滤器就永远看不到);
3. 抓屏读「高亮色在第几行」:`current` 推进时黄字必须往下走,清空后栏要收起成小条 ——
   屏幕证据优先于「我以为状态机变了」;
4. 投一串鼠标消息走完一次拖动,验证 `moved` 回传与「拖动结束后重新贴输入框」;
5. 造一个覆盖整屏的前台窗口,验证全屏时隐藏、关掉后恢复。

子进程的 stderr 直接继承本进程(不过滤、不吞):浮窗的 warning 与 traceback 会原样
打在控制台上,便于人工确认。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import canvas  # noqa: E402
import inputbox  # noqa: E402
import protocol  # noqa: E402
import win32  # noqa: E402

PYTHONW = Path(sys.executable).with_name("pythonw.exe")
if not PYTHONW.exists():
    PYTHONW = Path(sys.executable)

SEGMENTS = [
    {"text": "第一句日文", "translation": "第一句中文"},
    {"text": "第二句日文", "translation": "第二句中文"},
]

SUBMIT_TEXT = "我打的一句话"
READY_TIMEOUT = 5.0
SUBMIT_TIMEOUT = 3.0

# --- 本脚本自用的绑定(win32.py 没有,且不允许改 win32.py) ---
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
win32.user32.EnumWindows.restype = wintypes.BOOL
win32.user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
win32.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
win32.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
win32.user32.GetWindow.restype = wintypes.HWND
win32.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
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
win32.user32.GetSystemMetrics.restype = ctypes.c_int
win32.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
win32.user32.AttachThreadInput.restype = wintypes.BOOL
win32.user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
win32.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
win32.kernel32.GetCurrentThreadId.argtypes = []

WM_SETTEXT = 0x000C  # 跨进程也由系统编组(字符串),SetWindowTextW 则改不了别的进程的控件
GW_CHILD = 5
SRCCOPY = 0x00CC0020
SM_CXSCREEN = 0
SM_CYSCREEN = 1
YELLOW_ROW_MIN_PIXELS = 3  # 一行至少有这么多「高亮色」像素才算高亮行

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(name)
    return bool(ok)


class Reader(threading.Thread):
    """后台读浮窗 stdout:逐行 protocol.decode,把收到的消息记下来。"""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True, name="fake-plugin-stdout")
        self._stream = stream
        self._messages: list[dict] = []
        self._bad_lines: list[str] = []
        self._lock = threading.Lock()

    def run(self) -> None:
        # 裸 fd 读,不用 `for raw in self._stream`:守护线程持有 BufferedReader 的锁阻塞时,
        # 本进程收尾会撞上 `_enter_buffered_busy` 并以 0xC0000005 退出
        # (与浮窗侧修掉的那个崩溃同源;这里同样有「工具先退出、子进程还活着」的窗口)。
        try:
            handle = self._stream.fileno()
        except (AttributeError, ValueError, OSError):
            return
        pending = b""
        while True:
            try:
                chunk = os.read(handle, 65536)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._deliver(line)

    def _deliver(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            message = protocol.decode(text)
        except protocol.ProtocolError:
            with self._lock:
                self._bad_lines.append(text)
            print(f"  ← (非法行) {text[:120]}", flush=True)
            return
        with self._lock:
            self._messages.append(message)
        print(f"  ← {message.get('type')} {_brief(message)}", flush=True)

    def messages(self) -> list[dict]:
        with self._lock:
            return list(self._messages)

    def bad_lines(self) -> list[str]:
        with self._lock:
            return list(self._bad_lines)

    def wait_for(self, kind: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self.messages():
                if message.get("type") == kind:
                    return message
            time.sleep(0.05)
        return None


def _brief(message: dict) -> str:
    """摘要一行:形状 + 关键字段(正文截断,控制台里够看就行)。"""
    kind = message.get("type")
    if kind == "ready":
        return f"pid={message.get('pid')} screens={len(message.get('screens') or [])}"
    if kind == "submit":
        return f"text={str(message.get('text'))[:40]!r}"
    if kind == "error":
        return f"code={message.get('code')} detail={str(message.get('detail'))[:80]}"
    return ""


def spawn() -> tuple[subprocess.Popen, Reader]:
    """启动浮窗并开始读它的 stdout(stderr 继承本进程,便于看 warning)。"""
    process = subprocess.Popen(
        [str(PYTHONW), str(PLUGIN_DIR / "overlay.py"), "--run"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=str(PLUGIN_DIR),
    )
    reader = Reader(process.stdout)
    reader.start()
    return process, reader


def send(process: subprocess.Popen, message: dict) -> bool:
    try:
        process.stdin.write(protocol.encode(message))
        process.stdin.flush()
    except (BrokenPipeError, OSError) as error:
        # 浮窗先死了:别让工具自己崩在写管道上,后面的检查会如实报出来
        print(f"  ! 写给浮窗失败({type(error).__name__}): {message.get('type')}", flush=True)
        return False
    print(f"  → {message.get('type')}", flush=True)
    return True


def send_raw(process: subprocess.Popen, data: bytes) -> bool:
    """直接写原始字节:制造协议错误(残缺 JSON / 超长行)用。"""
    try:
        process.stdin.write(data)
        process.stdin.flush()
    except (BrokenPipeError, OSError) as error:
        print(f"  ! 写给浮窗失败({type(error).__name__})", flush=True)
        return False
    print(f"  → (raw {len(data)} bytes)", flush=True)
    return True


# --- 找浮窗的窗口 / 抓屏 ---


def find_window(pid: int, class_name: str) -> int | None:
    """在浮窗进程里按窗口类名找顶层窗口。"""
    match: list[int] = []

    def collect(hwnd, _param):
        try:
            owner = wintypes.DWORD()
            win32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                buffer = ctypes.create_unicode_buffer(64)
                win32.user32.GetClassNameW(hwnd, buffer, 64)
                if buffer.value == class_name:
                    match.append(int(hwnd))
        except Exception:  # noqa: BLE001 — 从 ctypes 回调里逃逸的异常会穿到 C 栈
            pass
        return True

    win32.user32.EnumWindows(WNDENUMPROC(collect), 0)
    return match[0] if match else None


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = win32.RECT()
    win32.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def capture(rect: tuple[int, int, int, int]) -> tuple[bytes, int]:
    """BitBlt 一块屏幕成自上而下的 BGRA 字节(与 tools/inputbox_check.py 同法)。"""
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


def screen_reading(rect: tuple[int, int, int, int]) -> tuple[int | None, int]:
    """抓这一块屏幕 → (高亮色最靠上的一行, 非高亮文字的像素数)。

    判据只用色相:高亮是暖色(红比蓝大一大截),白字/灰译文/深色底都不满足,
    所以「黄字在第几行」可以直接当「高亮是否推进」的屏幕证据。
    第二个数用来判断这份读数可不可信 —— 抓屏抓到被别的窗口盖住的区域时,
    黄字行号是别人的内容,得先证明「栏的文字确实画在屏幕上」。
    """
    raw, width = capture(rect)
    row_hits: dict[int, int] = {}
    text_pixels = 0
    for index in range(0, len(raw), 4):
        blue, green, red = raw[index], raw[index + 1], raw[index + 2]
        if red >= 120 and green >= 90 and (red - blue) >= 60 and (red - green) <= 90:
            row = index // 4 // width
            row_hits[row] = row_hits.get(row, 0) + 1
        elif red >= 150 and green >= 150 and blue >= 150:
            text_pixels += 1
    # 一行要有若干个黄像素才算「高亮行」:圆角外透出的桌面上偶尔也有暖色像素,
    # 单个像素就把行号算进来会把读数带偏(实测踩过:读数莫名其妙变成第 0 行)。
    rows = sorted(row for row, hits in row_hits.items() if hits >= YELLOW_ROW_MIN_PIXELS)
    return (rows[0] if rows else None), text_pixels


# --- 提交链路:跨进程给 EDIT 投「文本 + 回车」 ---


def find_edit(pid: int) -> int | None:
    """找输入框窗口,再取它的 EDIT 子控件(输入框只有一个子窗口)。"""
    hwnd = find_window(pid, inputbox.WINDOW_CLASS_NAME)
    if hwnd is None:
        return None
    child = win32.user32.GetWindow(hwnd, GW_CHILD)
    return int(child) if child else None


def inject_submit(pid: int, text: str) -> str:
    """向 EDIT 投「设置文本 + 回车」,返回说明。

    两处坑(都实测过):
    - `SetWindowTextW` **改不了别的进程里控件的文本**(MSDN 明说),用它注入会静默失败,
      表现为「回车发出去了但浮窗没回 submit」(输入框提交空串时本来就不回传);
    - `SendMessageW(edit, WM_SETTEXT, 0, cast(c_wchar_p(text), c_void_p).value)` 会让
      临时缓冲在调用前就被回收 —— 文本乱码(实测收到过 `'贠柼Ȗ'`)。
      所以这里显式建缓冲并**在调用期间持住引用**;WM_SETTEXT 是系统消息,跨进程由系统编组。
    回车用 `PostMessage`(与真实按键一样是投进队列的,过滤器正是按队列消息设计的)。
    """
    edit = find_edit(pid)
    if edit is None:
        return "没找到输入框的 EDIT,跳过注入"
    buffer = ctypes.create_unicode_buffer(text)  # 必须活到 SendMessageW 返回
    win32.user32.SendMessageW(edit, WM_SETTEXT, 0, ctypes.cast(buffer, ctypes.c_void_p).value)
    win32.user32.PostMessageW(edit, win32.WM_KEYDOWN, win32.VK_RETURN, 0)
    return f"已向 EDIT(0x{edit:X})投递回车"


def stop(process: subprocess.Popen) -> tuple[int, float]:
    """等浮窗退出,返回 (退出码, 耗时);超时就杀掉并给 -1。"""
    started = time.monotonic()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return -1, time.monotonic() - started
    return process.returncode, time.monotonic() - started


# --- 各验证模式 ---


def demo() -> int:
    """brief Step 6 的消息序列:hello → 两轮(reply/current/user/notice/clear)→ bye。

    除了「喂消息 + 看退出码」,还做两件能自证的事:
    - 跨进程给 EDIT 投回车,验证提交回传链路(过滤器真的接住了按键);
    - 抓屏看高亮色的位置:高亮句推进时黄字应该往下走,清空后栏应收起成 6px 小条。
    """
    process, reader = spawn()
    send(process, {"type": "hello", "theme": {}, "width": 460})
    ready = reader.wait_for("ready", READY_TIMEOUT)
    check("浮窗回 ready 握手", ready is not None, _brief(ready or {}))
    if ready is not None:
        check("ready 带 pid 与屏幕列表", bool(ready.get("pid")) and bool(ready.get("screens")))

    submit_seen: dict | None = None
    readings: list[dict] = []
    for round_index in range(2):
        send(process, {"type": "reply", "segments": SEGMENTS})
        time.sleep(0.5)
        for index in range(2):
            send(process, {"type": "current", "index": index})
            time.sleep(1.2)
            if round_index == 0:
                readings.append(_highlight_row(process.pid))
        if round_index == 0:
            print(f"  · {inject_submit(process.pid, SUBMIT_TEXT)}", flush=True)
            submit_seen = reader.wait_for("submit", SUBMIT_TIMEOUT)
        send(process, {"type": "user", "text": SUBMIT_TEXT})
        time.sleep(1.0)
        send(process, {"type": "notice", "text": "还在回复中"})
        time.sleep(4.0)
        send(process, protocol.clear_message())
        time.sleep(0.5)
    cleared = _highlight_row(process.pid)

    send(process, protocol.bye_message())
    time.sleep(0.3)
    process.stdin.close()
    code, elapsed = stop(process)

    check("回车经过滤器回传 submit", submit_seen is not None, _brief(submit_seen or {}))
    if submit_seen is not None:
        check("submit 文本与输入一致", submit_seen.get("text") == SUBMIT_TEXT,
              repr(submit_seen.get("text")))
    if len(readings) == 2:
        first, second = readings
        trustworthy = all(item["text"] > 0 for item in (first, second))
        check("抓屏读得到栏的文字(读数可信)", trustworthy,
              f"文字像素 {first['text']}/{second['text']}")
        check("current 推进时高亮黄字在屏幕上往下走",
              trustworthy and second["row"] > first["row"],
              f"第 {first['row']} 行 → 第 {second['row']} 行")
    else:
        check("current 推进时高亮黄字在屏幕上往下走", False, f"只抓到 {len(readings)} 次屏")
    # clear 之后判定「栏空了」用尺寸 + 无文字:小条是半透明的,底色会透出桌面,
    # 拿它去判颜色会误报(实测踩过),而「6px 细条 + 屏上没有文字」是确定性的。
    cleared_ok = (
        cleared["visible"]
        and cleared["rect"] is not None
        and cleared["rect"][3] == canvas.COLLAPSED_HEIGHT
        and cleared["text"] == 0
    )
    check("clear 后栏收起成小条且屏幕上没有文字", cleared_ok,
          f"rect={cleared['rect']} visible={cleared['visible']} 文字像素={cleared['text']}")
    errors = [m for m in reader.messages() if m.get("type") == "error"]
    check("浮窗没报协议错误", not errors, _brief(errors[0]) if errors else "")
    check("浮窗的 stdout 都是合法协议行", not reader.bad_lines())
    check("bye 后浮窗自行退出且退出码为 0", code == 0, f"退出码 {code},耗时 {elapsed:.2f}s")
    return 0 if not FAILURES else 1


def _highlight_row(pid: int) -> dict:
    """抓一次画布窗口所在的屏幕区域 → 读数 dict(带「这份读数可不可信」的线索)。"""
    hwnd = find_window(pid, canvas.WINDOW_CLASS_NAME)
    if hwnd is None:
        print("  · 抓屏:没找到画布窗口", flush=True)
        return {"rect": None, "visible": False, "row": None, "text": 0}
    rect = window_rect(hwnd)
    visible = bool(win32.user32.IsWindowVisible(hwnd))
    row, text_pixels = screen_reading(rect)
    print(f"  · 抓屏 rect={rect} visible={visible} 高亮最上行={row} 文字像素={text_pixels}",
          flush=True)
    return {"rect": rect, "visible": visible, "row": row, "text": text_pixels}


def geometry_check() -> int:
    """两窗几何:**画布几何一变就必须重新 attach 输入框**,否则矩形重叠时点击会落到画布上。

    重点是「拖动过之后」:那时画布以左上角为锚、内容变高会朝**下**长,旧位置的输入框
    正好被压在里面 —— 这是 Task 6 复审留下的残余条件。
    """
    process, reader = spawn()
    send(process, {"type": "hello", "theme": {}, "width": 460})
    reader.wait_for("ready", READY_TIMEOUT)
    send(process, {"type": "reply", "segments": SEGMENTS})
    time.sleep(0.8)
    check("默认位置下两窗不重叠", *_box_clear_of_canvas(process.pid))

    send(process, {"type": "place", "x": 300, "y": 200, "screenId": "PRIMARY"})
    time.sleep(0.5)
    _check_drag(process, reader, canvas_rect_before=_rects(process.pid)[0])
    # 拖动(place)过之后锚点变成左上角:内容变高是向下长的,不重贴就会压住输入框
    send(process, {"type": "reply", "segments": SEGMENTS * 3})
    time.sleep(0.8)
    check("place 之后内容变高,两窗仍不重叠", *_box_clear_of_canvas(process.pid))

    send(process, {"type": "reset-position"})
    time.sleep(0.6)
    check("reset-position 后两窗仍不重叠", *_box_clear_of_canvas(process.pid))
    check("reset-position 回到主屏下方居中", *_at_default_position(process.pid))

    _kill(process)
    return 0 if not FAILURES else 1


def _check_drag(process: subprocess.Popen, reader: Reader,
                canvas_rect_before: tuple[int, int, int, int] | None) -> None:
    """投一串真实的鼠标消息走完一次拖动(与真实拖拽同一条消息路径)。"""
    delta = (100, 80)
    if not _drag(process.pid, (20, 20), delta):
        check("拖动结束回传 moved", False, "没找到画布窗口")
        return
    moved = reader.wait_for("moved", 3.0)
    check("拖动结束回传 moved", moved is not None,
          "" if moved is None else
          f"x={moved.get('x')} y={moved.get('y')} screenId={moved.get('screenId')}")
    after = _rects(process.pid)[0]
    if canvas_rect_before and after:
        moved_ok = (after[0] - canvas_rect_before[0], after[1] - canvas_rect_before[1]) == delta
        check("画布跟着鼠标走了 (100, 80)", moved_ok,
              f"{canvas_rect_before[:2]} → {after[:2]}")
    else:
        check("画布跟着鼠标走了 (100, 80)", False, "没找到画布窗口")
    check("拖动结束后两窗仍不重叠", *_box_clear_of_canvas(process.pid))


def _drag(pid: int, origin: tuple[int, int], delta: tuple[int, int]) -> bool:
    """把「按下 → 移动 → 松开」投给画布窗口(lParam 的坐标是客户区坐标)。"""
    hwnd = find_window(pid, canvas.WINDOW_CLASS_NAME)
    if hwnd is None:
        return False
    print(f"  → 拖动 客户端({origin[0]},{origin[1]}) 位移 {delta}", flush=True)
    win32.user32.PostMessageW(hwnd, win32.WM_LBUTTONDOWN, 0, _pack(origin))
    time.sleep(0.1)
    win32.user32.PostMessageW(
        hwnd, win32.WM_MOUSEMOVE, 0, _pack((origin[0] + delta[0], origin[1] + delta[1]))
    )
    time.sleep(0.1)
    win32.user32.PostMessageW(hwnd, win32.WM_LBUTTONUP, 0, 0)
    return True


def _pack(point: tuple[int, int]) -> int:
    """客户区坐标 → lParam(低 16 位 x、高 16 位 y)。"""
    x, y = point
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _rects(pid: int) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    canvas_hwnd = find_window(pid, canvas.WINDOW_CLASS_NAME)
    box_hwnd = find_window(pid, inputbox.WINDOW_CLASS_NAME)
    return (
        window_rect(canvas_hwnd) if canvas_hwnd else None,
        window_rect(box_hwnd) if box_hwnd else None,
    )


def _box_clear_of_canvas(pid: int) -> tuple[bool, str]:
    """两个矩形是否互不重叠(重叠时画布的置顶会在 topmost 带上抢走点击)。"""
    canvas_rect, box_rect = _rects(pid)
    if canvas_rect is None or box_rect is None:
        return False, "没找到画布或输入框窗口"
    cx, cy, cw, ch = canvas_rect
    bx, by, bw, bh = box_rect
    overlap = not (cx + cw <= bx or bx + bw <= cx or cy + ch <= by or by + bh <= cy)
    position = "下" if by >= cy else "上"
    gap = by - (cy + ch) if by >= cy else cy - (by + bh)
    return (
        not overlap,
        f"画布={canvas_rect} 输入框={box_rect}({position}方,缝 {gap}px)",
    )


def _at_default_position(pid: int) -> tuple[bool, str]:
    """画布是否回到「主屏工作区底部居中」(reset-position 的目标位置)。"""
    canvas_rect, _ = _rects(pid)
    if canvas_rect is None:
        return False, "没找到画布窗口"
    work = win32.RECT()
    if not win32.user32.SystemParametersInfoW(win32.SPI_GETWORKAREA, 0, ctypes.byref(work), 0):
        return False, "读不到工作区"
    width, height = canvas_rect[2], canvas_rect[3]
    expect_left = work.left + max(0, (work.right - work.left - width) // 2)
    expect_top = max(work.top, work.bottom - height - canvas.BAR_BOTTOM_MARGIN)
    return (
        (canvas_rect[0], canvas_rect[1]) == (expect_left, expect_top),
        f"实际=({canvas_rect[0]},{canvas_rect[1]}) 期望=({expect_left},{expect_top})",
    )


def fullscreen_check() -> int:
    """全屏隐藏/恢复的接线(含 `hideInFullscreen` 开关):造一个覆盖整屏的前台窗口观察。

    四步:全屏 → 隐藏(开关默认开);**全屏中把开关关掉 → 不该再隐藏**(验收清单第 11 条);
    重新打开开关再全屏 → 又隐藏(证明是开关在起作用,不是检测被永久关掉);取消全屏 → 恢复。

    **会短暂盖住主屏**(用的是系统 STATIC 类的置顶窗口),别在正用电脑时跑。
    """
    process, reader = spawn()
    send(process, {"type": "hello", "theme": {}, "width": 460})
    reader.wait_for("ready", READY_TIMEOUT)
    send(process, {"type": "reply", "segments": SEGMENTS})
    time.sleep(1.0)
    hwnd = find_window(process.pid, canvas.WINDOW_CLASS_NAME)
    shown = hwnd is not None and bool(win32.user32.IsWindowVisible(hwnd))
    check("栏初始可见", shown)
    if not shown or hwnd is None:
        _kill(process)
        return 1

    cover = _make_fullscreen_window()
    check("造出覆盖整屏的前台窗口", cover is not None, _foreground_dump())
    check("前台全屏时栏被隐藏(主题里 hideInFullscreen 默认开)",
          _wait_visible(hwnd, False, 4.0), _foreground_dump())

    # 验收清单第 11 条:关掉「全屏应用时自动隐藏」,全屏期间也不能藏
    send(process, {"type": "theme", "theme": {"hideInFullscreen": False}})
    check("关掉 hideInFullscreen 后全屏不再隐藏", _wait_visible(hwnd, True, 4.0), _foreground_dump())

    if cover is not None:
        win32.user32.SendMessageW(cover, win32.WM_CLOSE, 0, 0)
    check("全屏窗口关掉后栏仍可见", _wait_visible(hwnd, True, 4.0), _foreground_dump())

    # 开关打开 + 再次全屏:应当又隐藏 —— 否则无法区分「开关生效」与「检测彻底不跑了」
    send(process, {"type": "theme", "theme": {"hideInFullscreen": True}})
    time.sleep(0.3)
    cover_again = _make_fullscreen_window()
    check("重新打开开关后全屏又隐藏", _wait_visible(hwnd, False, 4.0), _foreground_dump())
    if cover_again is not None:
        win32.user32.SendMessageW(cover_again, win32.WM_CLOSE, 0, 0)
    check("取消全屏后栏恢复显示", _wait_visible(hwnd, True, 4.0), _foreground_dump())

    _kill(process)
    return 0 if not FAILURES else 1


def _make_fullscreen_window() -> int | None:
    """造一个覆盖主屏整块区域、并试图抢到前台的窗口(用系统 STATIC 类,不注册窗口类)。"""
    width = int(win32.user32.GetSystemMetrics(SM_CXSCREEN))
    height = int(win32.user32.GetSystemMetrics(SM_CYSCREEN))
    hwnd = win32.user32.CreateWindowExW(
        win32.WS_EX_TOPMOST, "STATIC", "", win32.WS_POPUP,
        0, 0, width, height, None, None, win32.kernel32.GetModuleHandleW(None), None,
    )
    if not hwnd:
        return None
    _steal_foreground(int(hwnd))
    _pump(0.3)
    return int(hwnd)


def _steal_foreground(hwnd: int) -> None:
    """把窗口抢到前台。

    直接 `SetForegroundWindow` 会被系统拒绝(实测:前台仍是终端)。惯例做法是先把本线程的
    输入接到当前前台线程上,设完再断开。
    """
    foreground = win32.user32.GetForegroundWindow()
    current = int(win32.kernel32.GetCurrentThreadId())
    owner = int(win32.user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    attached = False
    if owner and owner != current:
        attached = bool(win32.user32.AttachThreadInput(current, owner, True))
    try:
        win32.user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            win32.user32.AttachThreadInput(current, owner, False)


def _wait_visible(hwnd: int, want: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(0.1)
        if bool(win32.user32.IsWindowVisible(hwnd)) == want:
            return True
    return False


def _pump(seconds: float) -> None:
    """抽一会儿本进程的消息队列(造出来的窗口要有人处理消息才正常)。"""
    message = wintypes.MSG()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while win32.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, win32.PM_REMOVE):
            win32.user32.TranslateMessage(ctypes.byref(message))
            win32.user32.DispatchMessageW(ctypes.byref(message))
        time.sleep(0.01)


def _foreground_dump() -> str:
    """失败时最要紧的一条信息:此刻的前台窗口是谁、多大。"""
    hwnd = win32.user32.GetForegroundWindow()
    if not hwnd:
        return "没有前台窗口"
    buffer = ctypes.create_unicode_buffer(64)
    win32.user32.GetClassNameW(hwnd, buffer, 64)
    rect = win32.RECT()
    win32.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return f"前台={buffer.value} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})"


def _kill(process: subprocess.Popen) -> None:
    try:
        send(process, protocol.bye_message())
        time.sleep(0.3)
        process.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass
    stop(process)


def protocol_error_check() -> int:
    """协议错误路径:连续 3 次解析失败 → 退出码 3;单行超长 → 一条就退出码 3。"""
    process, reader = spawn()
    send(process, {"type": "hello", "theme": {}, "width": 460})
    check("浮窗回 ready 握手", reader.wait_for("ready", READY_TIMEOUT) is not None)
    for _ in range(3):
        send_raw(process, b'{"type": "user"\n')  # 残缺 JSON
        time.sleep(0.2)
    code, elapsed = stop(process)
    errors = [m for m in reader.messages() if m.get("type") == "error"]
    check("连续 3 次解析失败后以退出码 3 结束", code == 3, f"退出码 {code},耗时 {elapsed:.2f}s")
    check(
        "每次失败都回 OVERLAY_PROTOCOL_INVALID",
        len(errors) >= 3 and all(m.get("code") == "OVERLAY_PROTOCOL_INVALID" for m in errors),
        f"收到 {len(errors)} 条 error",
    )

    oversized, oversized_reader = spawn()
    send(oversized, {"type": "hello", "theme": {}, "width": 460})
    oversized_reader.wait_for("ready", READY_TIMEOUT)
    # 一行就超过 1 MiB:不等到换行,读到超长即判致命
    send_raw(oversized, b'{"type": "user", "text": "' + b"x" * (protocol.MAX_LINE_BYTES + 10) + b'"}\n')
    code, elapsed = stop(oversized)
    errors = [m for m in oversized_reader.messages() if m.get("type") == "error"]
    check("单行超长一条就判致命(退出码 3)", code == 3, f"退出码 {code},耗时 {elapsed:.2f}s")
    check(
        "超长行回 OVERLAY_PROTOCOL_INVALID",
        bool(errors) and errors[0].get("code") == "OVERLAY_PROTOCOL_INVALID",
        _brief(errors[0]) if errors else "没有 error",
    )
    return 0 if not FAILURES else 1


def eof_check() -> int:
    """生命线:不发 bye,直接关 stdin —— 浮窗必须在数秒内自己退出。"""
    process, reader = spawn()
    send(process, {"type": "hello", "theme": {}, "width": 460})
    ready = reader.wait_for("ready", READY_TIMEOUT)
    check("浮窗回 ready 握手", ready is not None, _brief(ready or {}))

    started = time.monotonic()
    process.stdin.close()  # 等价于插件进程退出:管道写端关闭
    code, _ = stop(process)
    elapsed = time.monotonic() - started
    check("stdin EOF 后浮窗自行退出", code == 0, f"退出码 {code}")
    check("退出耗时在数秒内", 0 <= elapsed < 5.0, f"{elapsed:.2f}s")
    return 0 if not FAILURES else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_plugin")
    parser.add_argument("--eof-check", action="store_true", help="只验证 stdin EOF 生命线")
    parser.add_argument("--protocol-error-check", action="store_true",
                        help="只验证协议错误路径(退出码 3)")
    parser.add_argument("--fullscreen-check", action="store_true",
                        help="只验证全屏隐藏/恢复(会短暂盖住主屏)")
    parser.add_argument("--geometry-check", action="store_true",
                        help="只验证两窗几何(place / 变高 / reset-position 后不重叠)")
    args = parser.parse_args(argv)

    if args.eof_check:
        print("=== stdin EOF 生命线 ===", flush=True)
        code = eof_check()
    elif args.protocol_error_check:
        print("=== 协议错误路径 ===", flush=True)
        code = protocol_error_check()
    elif args.fullscreen_check:
        print("=== 全屏隐藏/恢复 ===", flush=True)
        code = fullscreen_check()
    elif args.geometry_check:
        print("=== 两窗几何 ===", flush=True)
        code = geometry_check()
    else:
        print("=== 协议驱动全流程 ===", flush=True)
        code = demo()

    print(f"\n{'全部通过' if code == 0 else '有失败项: ' + ', '.join(FAILURES)}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
