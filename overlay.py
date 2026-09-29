"""浮窗进程入口。"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path
from typing import Any, Mapping

# 宿主内置解释器带 python312._pth,脚本目录不会自动进入 sys.path。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import canvas  # noqa: E402
import compose  # noqa: E402
import fullscreen  # noqa: E402
import inputbox  # noqa: E402
import protocol  # noqa: E402
import theme  # noqa: E402
import view  # noqa: E402
import win32  # noqa: E402

# 指针离开后多久收起输入框(主题里是 hoverDelayMs,演示模式先按默认值写死)
HIDE_DELAY_SECONDS = 0.8

# win32.py 未绑定:演示模式要用它判断光标是否还在输入框上(不扩改 win32.py)
win32.user32.GetCursorPos.restype = wintypes.BOOL
win32.user32.GetCursorPos.argtypes = [ctypes.POINTER(win32.POINT)]

DEMO_SEGMENTS = [
    {"text": "おはよう、今日もいい天気だね。", "translation": "早上好,今天天气也不错呢。"},
    {"text": "ちょっと待ってて、今行くから。", "translation": "稍等一下,我这就过去。"},
    {"text": "ねえ、聞いてる?", "translation": "喂,你在听吗?"},
]


def _say(text: str, stream: object = None) -> None:
    """打印到控制台;pythonw.exe 下流可能是 None,打印不能变成崩溃点。"""
    stream = sys.stdout if stream is None else stream
    if stream is None:
        return
    try:
        print(text, file=stream, flush=True)  # type: ignore[arg-type]
    except OSError:
        pass


def _selftest() -> int:
    # 主题投影:任意输入都要落成键齐全、值合法的主题
    projected = theme.project_theme({"fontSize": 999, "backgroundColor": "不是颜色"})
    assert set(projected) == set(theme.DEFAULTS), "投影结果的键集合必须与 DEFAULTS 一致"
    assert projected["fontSize"] == 72, "超范围数值夹到边界(theme 的 fontSize 上限)"
    assert projected["backgroundColor"] == theme.DEFAULTS["backgroundColor"], "非法颜色回落默认值"
    # 颜色解析:合法十六进制;缺 # 号不算合法
    assert theme.parse_color("#FF8800") == (255, 136, 0, 255)
    assert theme.parse_color("FF8800") is None

    state = view.ViewState({})
    state.set_reply(DEMO_SEGMENTS)
    boxes = state.boxes(460, canvas.theme_measure(state.theme_values))
    assert boxes.boxes, "演示台词应产生布局行"
    assert state.highlight_box(boxes) is not None or state.current_segment < 0
    # 栏高上限 = 最多显示行数 × 行高
    assert boxes.view_height <= boxes.line_height * theme.DEFAULTS["maxLines"]
    # 合成:不建窗口也能把视图画进缓冲,且不是一张全透明图
    buffer = compose.new_buffer(460, boxes.view_height + 40)
    canvas.CanvasWindow.paint_into(buffer, state, boxes, 0)
    assert any(buffer.data), "合成结果不应全透明"
    _say("selftest ok")
    return 0


def _render_sample(path: str) -> int:
    state = view.ViewState(theme.default_theme())
    state.set_reply(DEMO_SEGMENTS)
    state.set_current(1)
    boxes = state.boxes(
        int(state.theme_values["width"]), canvas.theme_measure(state.theme_values)
    )
    buffer = compose.new_buffer(int(state.theme_values["width"]), boxes.view_height + 40)
    painter = canvas.CanvasWindow.paint_into  # 与窗口 render() 共用同一条绘制路径
    painter(buffer, state, boxes, 0)
    compose.write_png(path, buffer)
    _say(f"已写出 {path}")
    return 0


def _cursor_inside(rect: tuple[int, int, int, int]) -> bool:
    """光标是否落在窗口矩形内(收起输入框前要问一句,见 `_demo` 的收起条件)。"""
    point = win32.POINT()
    if not win32.user32.GetCursorPos(ctypes.byref(point)):
        return False
    x, y, width, height = rect
    return x <= point.x < x + width and y <= point.y < y + height


def _needs_above(canvas_rect: tuple[int, int, int, int], box_height: int) -> bool:
    """画布贴到工作区底边、下方放不下输入框时改放画布上方(越界判断由调用方做)。"""
    work = win32.RECT()
    if not win32.user32.SystemParametersInfoW(
        win32.SPI_GETWORKAREA, 0, ctypes.byref(work), 0
    ):
        return False
    _x, y, _width, height = canvas_rect
    return y + height + inputbox.GAP + box_height > work.bottom


def _demo(duration: float | None = None) -> int:
    state = view.ViewState(theme.default_theme())
    state.set_reply(DEMO_SEGMENTS)

    hover_deadline: float | None = None  # 指针离开后的收起到期时间;None = 不打算收起

    def box_height() -> int:
        return int(state.theme_values["fontSize"]) + inputbox.HEIGHT_EXTRA

    def sync_box() -> None:
        """让输入框贴住画布当前几何(拖动、变高、换主题后都要重贴)。"""
        rect = window.geometry()
        if rect is None:
            return
        box.attach(rect, above=_needs_above(rect, box_height()))

    def report_drag_end(x: int, y: int) -> None:
        _say(f"拖拽结束:({x}, {y})")
        sync_box()

    def report_submit(text: str) -> None:
        _say(f"提交: {text}")
        # 演示模式没有宿主,把「你:…」直接上屏,回车是否真的生效一眼可见
        state.set_user(text)
        window.render(state)
        sync_box()

    def report_focus(entered: bool) -> None:
        _say(f"输入框{'获得' if entered else '失去'}焦点")

    box = inputbox.InputBox(report_submit, on_focus_change=report_focus)
    box.apply_theme(state.theme_values)

    def on_hover(entered: bool) -> None:
        nonlocal hover_deadline
        if entered:
            # 重复的进入/离开不得让输入框闪烁或提前收起:进入即撤销待收起
            hover_deadline = None
            sync_box()
            box.show()
        else:
            hover_deadline = time.monotonic() + HIDE_DELAY_SECONDS

    window = canvas.CanvasWindow(
        state.theme_values, on_hover_change=on_hover, on_drag_end=report_drag_end
    )
    window.show()
    window.render(state)  # 立刻出画面,不必等第一个 2 秒
    _say(f"浮窗位置 {window.geometry()}")
    _say("演示模式:鼠标移到栏上出输入框,点击聚焦后可打字;回车提交、Esc 收起保留草稿")
    deadline = None if duration is None else time.monotonic() + float(duration)
    try:
        index = 0
        last = time.monotonic()
        while window.pump(message_filter=box.filter_message):
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            if hover_deadline is not None and now >= hover_deadline:
                # 收起条件按设计 §5.2:「离开画布**与输入框**超过收起延时」。
                # 光标还在输入框上、或正在框里打字时不收起 —— 否则手一动到框上
                # 就看着它消失。
                rect = box.geometry()
                if not box.focused() and rect is not None and not _cursor_inside(rect):
                    box.hide()
                    hover_deadline = None
            if now - last > 2.0:
                state.set_current(index % len(DEMO_SEGMENTS))
                last = now
                index += 1
                window.render(state)
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        box.close()
        window.close()
    _say("演示结束")
    return 0


# --- --run:协议驱动的主循环 ---

LOOP_INTERVAL_SECONDS = 0.02  # 主循环目标间隔(设计文档第 5 节:20ms)
FULLSCREEN_CHECK_SECONDS = 1.0  # 全屏检测周期(spec §5.4)
PARSE_FAILURE_LIMIT = 3  # 连续解析失败上限(spec 第 6 节:断开并重启浮窗)
PROTOCOL_EXIT_CODE = 3  # 协议致命错误时的退出码(brief Step 5)
START_FAILURE_EXIT_CODE = 1  # 建窗失败(brief 只规定 0/3,这里取通用失败码)
READ_CHUNK_BYTES = 65536  # stdin 裸读的块大小
MONITORINFOF_PRIMARY = 0x0001

# win32.py 未绑定 EnumDisplayMonitors:就近在本模块绑定(未扩改 win32.py)。
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(win32.RECT), wintypes.LPARAM
)
win32.user32.EnumDisplayMonitors.restype = wintypes.BOOL
win32.user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM
]


def _warn(text: str, stream: object = None) -> None:
    """warning/debug 走 stderr:插件侧把子进程 stderr 的非空行转成 log.warning(Task 9)。"""
    stream = sys.stderr if stream is None else stream
    if stream is None:
        return
    try:
        print(f"overlay: {text}", file=stream, flush=True)  # type: ignore[arg-type]
    except OSError:
        pass


def _as_int(value: object, default: int = 0) -> int:
    """坏数据不进 Win32:非数字、NaN、±inf、超大整数一律回落默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (OverflowError, ValueError):
        return default


def _work_area() -> tuple[int, int, int, int] | None:
    work = win32.RECT()
    if not win32.user32.SystemParametersInfoW(
        win32.SPI_GETWORKAREA, 0, ctypes.byref(work), 0
    ):
        return None
    return (work.left, work.top, work.right, work.bottom)


def _screens() -> list[dict[str, Any]]:
    """枚举显示器 → `[{"id", "rect", "primary"}]`(`ready` 的载荷与 `moved` 的 screenId)。

    id 方案是**本模块自造的**:主屏 → `"PRIMARY"`,其余按左上角排序后依次
    `"DISPLAY2"`、`"DISPLAY3"`…。它**不代表** Windows 自己的显示器名字 —— 那边的
    `EnumDisplayDevices`/`\\\\.\\DISPLAYn` 按适配器枚举,与「按左上角排序」并不一致
    (主屏也未必是 `DISPLAY1`)。所以:**将来若要按 id 去定位某块屏幕(例如恢复记住的位置),
    必须重做这套 id(改用 `MONITORINFOEXW.szDevice` 之类的稳定标识),不能拿这里的编号
    去反查系统**。

    枚举不到时回落成「整个工作区一块主屏」,这样 `ready` 永远给得出合法的屏幕列表。
    """
    found: list[dict[str, Any]] = []

    def collect(handle: Any, _hdc: Any, _rect: Any, _data: Any) -> bool:
        try:
            info = fullscreen.MONITORINFO()
            info.cbSize = ctypes.sizeof(fullscreen.MONITORINFO)
            if win32.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                found.append(
                    {
                        "rect": [
                            info.rcMonitor.left, info.rcMonitor.top,
                            info.rcMonitor.right, info.rcMonitor.bottom,
                        ],
                        "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    }
                )
        except Exception:  # noqa: BLE001 — 从 ctypes 回调里逃逸的异常会穿到 C 栈
            traceback.print_exc()
        return True

    try:
        win32.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(collect), 0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        found = []

    if not found:
        work = _work_area()
        return [{"id": "PRIMARY", "rect": list(work or (0, 0, 0, 0)), "primary": True}]

    # 主屏在前,其余按 (top, left):多次调用之间 id 保持稳定
    found.sort(key=lambda item: (not item["primary"], item["rect"][1], item["rect"][0]))
    number = 1
    for item in found:
        if item["primary"]:
            item["id"] = "PRIMARY"
        else:
            number += 1
            item["id"] = f"DISPLAY{number}"
    return found


def _screen_id_at(x: int, y: int, screens: list[dict[str, Any]] | None = None) -> str:
    """坐标落在哪块显示器(拖动上报用);都不命中(屏幕间缝隙、越界)时给主屏。"""
    screens = _screens() if screens is None else screens
    for screen in screens:
        left, top, right, bottom = screen["rect"]
        if left <= x < right and top <= y < bottom:
            return str(screen["id"])
    for screen in screens:
        if screen["primary"]:
            return str(screen["id"])
    return "PRIMARY"




class _Channel:
    """与插件进程的传输:stdout 加锁写、stdin 后台线程逐行读。

    stdin 是浮窗的生命线 —— 插件进程一死,管道就 EOF,浮窗必须跟着退出(否则桌面上
    会留下一个没人管的置顶窗口)。
    """

    def __init__(self) -> None:
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stopped = threading.Event()  # EOF 或协议致命错误:主循环该退了
        self.exit_code = 0
        self._write_lock = threading.Lock()
        self._failures = 0

    def send(self, message: Mapping[str, Any]) -> bool:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is None:  # pythonw 下没有重定向时 stdout 可能是 None
            return False
        data = protocol.encode(dict(message))
        try:
            with self._write_lock:
                stream.write(data)
                stream.flush()
        except (OSError, ValueError):
            return False
        return True

    def start(self) -> None:
        # 守护线程:主循环退出时不必等它从阻塞的 readline 里醒过来
        threading.Thread(target=self._read_stdin, name="overlay-stdin", daemon=True).start()

    def drain(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        while True:
            try:
                pending.append(self.messages.get_nowait())
            except queue.Empty:
                return pending

    # --- 后台线程 ---

    def _read_stdin(self) -> None:
        """**裸 fd** 逐行读(不走 `sys.stdin` 的缓冲对象)。

        为什么不用 `for line in sys.stdin.buffer`:`BufferedReader.readline()` 会持有 io
        缓冲锁阻塞在那里,而这是守护线程 —— 主循环正常返回后,解释器收尾要拿同一把锁去关
        stdin,撞上就是 `Fatal Python error: _enter_buffered_busy`,进程以 0xC0000005 退出
        (实测:bye 路径的退出码是 3221225477,而 EOF 路径恰好因为线程已结束才干净)。
        裸 `os.read` 不持任何锁,两条路径都是干净的 0。
        """
        try:
            handle = sys.stdin.fileno()
        except (AttributeError, ValueError, OSError):
            # 没有 stdin(未接管道 / 插件进程已退出):浮窗没有存在的理由
            self.stopped.set()
            return
        pending = b""
        while True:
            try:
                chunk = os.read(handle, READ_CHUNK_BYTES)
            except OSError:
                break  # 管道断了(ERROR_BROKEN_PIPE 之类),按 EOF 处理
            if not chunk:
                break  # EOF:插件进程已退出
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not self._deliver(line):
                    return
            if len(pending) > protocol.MAX_LINE_BYTES:
                # 还没读到换行就已超长:一条就判致命(spec:单行超过 1 MiB 视为协议错误)
                self._protocol_error("line too long", fatal=True)
                return
        self.stopped.set()

    def _deliver(self, line: bytes) -> bool:
        """处理一行;返回 False 表示读线程该收工了(协议致命错误)。"""
        line = line.rstrip(b"\r")
        if not line.strip():
            return True
        if len(line) > protocol.MAX_LINE_BYTES:
            self._protocol_error("line too long", fatal=True)
            return False
        try:
            message = protocol.decode(line)
        except protocol.ProtocolError as error:
            self._failures += 1
            fatal = self._failures >= PARSE_FAILURE_LIMIT
            self._protocol_error(str(error), fatal=fatal)
            return not fatal
        self._failures = 0
        self.messages.put(message)
        return True

    def _protocol_error(self, detail: str, fatal: bool) -> None:
        self.send(protocol.error_message("OVERLAY_PROTOCOL_INVALID", detail))
        if fatal:
            self.exit_code = PROTOCOL_EXIT_CODE
            self.stopped.set()


class _RunSession:
    """`--run` 的会话:窗口、状态与主循环。"""

    def __init__(self, channel: _Channel) -> None:
        self.channel = channel
        self.view = view.ViewState(theme.default_theme())
        self.probe = fullscreen.FullscreenProbe()

        # 显示策略:主题里的 enabled 是权威,`hide` 只是「立刻藏起来」的一次性开关
        self.hide_by_policy = not bool(self.view.theme_values.get("enabled", True))
        self.fullscreen_active = False
        self.recorded_position: tuple[int, int, str] | None = None

        self._dirty = True  # 首帧要画
        self._hover_active = False
        self._hover_deadline: float | None = None
        self._attached_key: tuple[Any, ...] | None = None
        self._warned: dict[str, str] = {}
        self.last_probe_at = 0.0

        # 拖动会话(见 _on_drag_start):拖动期间悬停与输入框都冻住
        self._drag_active = False
        self._drag_pressed_rect: tuple[int, int, int, int] | None = None
        self._drag_hid_box = False

        # 可用性检查放在建窗之后、主循环之前:失败只记一次 warning,本轮生命周期内跳过检测
        self.probe_available = self._probe_is_available()

        self.inputbox = inputbox.InputBox(self._on_submit)
        try:
            self.canvas = canvas.CanvasWindow(
                self.view.theme_values,
                on_hover_change=self._on_hover,
                on_drag_end=self._on_drag_end,
                on_drag_start=self._on_drag_start,
            )
        except OSError:
            self.inputbox.close()  # 别把已经建好的输入框漏在那
            raise

    # --- 启动与退出 ---

    def _probe_is_available(self) -> bool:
        """spec §5.4:检测 API 不可用时按「不隐藏」处理并记 warning 一次。"""
        try:
            self.probe.notification_state()
        except Exception as error:  # noqa: BLE001 — 探针是外部 API,什么都可能抛
            _warn(f"FULLSCREEN_PROBE_UNAVAILABLE {type(error).__name__}: {error}")
            return False
        return True

    def run(self) -> int:
        try:
            self._startup()
            while True:
                try:
                    # 队列只在画布这一处抽干,过滤器必须传:输入框的回车/Esc 靠它拦截
                    if not self.canvas.pump(message_filter=self.inputbox.filter_message):
                        break  # 窗口没了 / WM_QUIT
                    if self.channel.stopped.is_set():
                        break  # stdin EOF(插件进程已退出)或协议致命错误
                    if not self._dispatch_all():
                        break  # bye
                    self._tick()
                except Exception as error:  # noqa: BLE001 — 主循环是生命线,同 canvas._handle 的兜法
                    # 意外异常不能让浮窗悄悄死掉:它还要守着 stdin EOF(插件进程一死就得跟着退)
                    self._warn_once("loop", f"OVERLAY_LOOP_ERROR {type(error).__name__}: {error}")
                time.sleep(LOOP_INTERVAL_SECONDS)  # 兜住异常也要照常节流,别空转
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return self.channel.exit_code

    def _startup(self) -> None:
        self.canvas.show()
        self.canvas.render(self.view)  # 立刻出画面(空状态:收起成小条)
        self._sync_box()
        self._apply_policy()
        self.last_probe_at = time.monotonic()
        # 就绪握手:插件靠它确认子进程活着,之后的 hello 才会把主题带过来
        self.channel.send(protocol.ready_message(os.getpid(), _screens()))

    def _shutdown(self) -> None:
        self.channel.send(protocol.bye_message())
        try:
            self.inputbox.close()
        finally:
            self.canvas.close()
            canvas.close_shared_raster()  # 公开入口:缺失/改名会当场炸,不会静默漏掉收尾

    # --- 消息分派 ---

    def _dispatch_all(self) -> bool:
        """排空队列并分派;返回 False 表示收到 bye,主循环该退了。

        单条消息处理炸了只记日志并跳过它 —— 一条坏消息不该带走主循环,也不该把它后面
        已经排队的消息一起吞掉(兜的宽度与 `canvas._handle`/`inputbox.filter_message` 一致:
        接 `Exception`,不放 `KeyboardInterrupt` 这类 `BaseException`)。

        日志只写一行(稳定码 + 异常类型 + 异常消息,`_warn_once` 去重):宿主侧只保留每条
        stderr 行的前 200 字符,多行 traceback 到那边会碎成好几条没信息量的 warning。
        """
        for message in self.channel.drain():
            try:
                if not self._dispatch(message):
                    return False
            except Exception as error:  # noqa: BLE001
                self._warn_once(
                    "dispatch", f"OVERLAY_DISPATCH_FAILED {type(error).__name__}: {error}"
                )
        return True

    def _dispatch(self, message: Mapping[str, Any]) -> bool:
        kind = message.get("type")
        if kind in ("hello", "theme"):
            self._apply_theme(message.get("theme"))
        elif kind in ("reply", "demo"):
            # demo(设置页「预览效果」)与真实回复走同一条装载路径
            self.view.set_reply(message.get("segments"))
            self._reset_scroll()
            self._dirty = True
        elif kind == "user":
            if self.view.set_user(message.get("text")):
                self._dirty = True
        elif kind == "notice":
            if self.view.set_notice(message.get("text"), time.monotonic()):
                self._dirty = True
        elif kind == "current":
            if self.view.set_current(message.get("index")):
                self._dirty = True
        elif kind == "clear":
            if self.view.clear():
                self._dirty = True
        elif kind == "hide":
            self.hide_by_policy = True
            self._apply_policy()
        elif kind == "place":
            self._place(message)
        elif kind == "reset-position":
            self._reset_position()
        elif kind == "bye":
            return False
        else:
            # spec 第 6 节:未知消息类型一律忽略并记 debug 日志
            _warn(f"debug unknown message type: {kind!r}")
        return True

    def _apply_theme(self, values: object) -> None:
        self.view.set_theme(values if isinstance(values, Mapping) else None)
        self.canvas.set_theme(self.view.theme_values)
        self._apply_input_theme()
        # 主题里的 enabled 是权威:插件关掉「显示浮窗」时推 hide,再打开时只会推主题,
        # 所以这里按 enabled 重算策略位 —— 否则「关一次就再也回不来」。
        self.hide_by_policy = not bool(self.view.theme_values.get("enabled", True))
        self._dirty = True
        self._apply_policy()
        self._sync_box()

    def _apply_input_theme(self) -> None:
        # apply_theme 经 _apply_layered_style 施加圆角与 alpha,可能抛 OSError;
        # 它不能让主题变化把主循环带走,这里直接兜住(不假设前置条件都成立)。
        try:
            self.inputbox.apply_theme(self.view.theme_values)
        except OSError as error:
            self._warn_once("theme", f"OVERLAY_THEME_FAILED {type(error).__name__}: {error}")

    def _place(self, message: Mapping[str, Any]) -> None:
        x = _as_int(message.get("x"))
        y = _as_int(message.get("y"))
        self.canvas.move_to(x, y)
        self.recorded_position = (x, y, str(message.get("screenId") or ""))
        self._sync_box()

    def _reset_position(self) -> None:
        # 默认位置 + 底边锚点一起恢复(都在 canvas 的公开入口里,见 restore_default_position)
        self.canvas.restore_default_position()
        self.recorded_position = None
        self._sync_box()

    def _reset_scroll(self) -> None:
        """新回复从第一句起看(偏移由 canvas 自己维护,走它的公开入口)。"""
        self.canvas.reset_scroll()

    # --- 显示策略 ---

    def _policy_visible(self) -> bool:
        values = self.view.theme_values
        # 全屏隐藏要**真的看开关**(验收清单第 11 条:关掉「全屏应用时自动隐藏」就不能再藏)
        fullscreen_hidden = self.fullscreen_active and bool(values.get("hideInFullscreen", True))
        return (
            bool(values.get("enabled", True))
            and not self.hide_by_policy
            and not fullscreen_hidden
        )

    def _apply_policy(self) -> None:
        """全屏 / `enabled` / `hide` 只影响显示,窗口与位置都保留(设计文档 §5.4)。"""
        visible = self._policy_visible()
        self.canvas.set_visible_by_policy(visible)
        if not visible:
            self.inputbox.hide()
        elif self._hover_active and not self._drag_active:
            # 从全屏恢复时,指针若还停在栏上就把输入框放回来(状态一直保留着)。
            # 拖动中不放:见 _on_drag_start(拖完由 _on_drag_end 决定放不放)。
            self._sync_box()
            self.inputbox.show()

    def _sync_box(self) -> None:
        """让输入框贴住画布当前几何。

        画布几何一变(拖动结束、换主题、内容变高、place/reset)就必须重贴:画布被拖动过
        之后 `_anchor_bottom=False`,`_resize` 会以左上角为锚**向下**生长,输入框还停在
        原位时两者矩形可能重叠 —— 而画布的 3 秒置顶会让它在 topmost 带上抢到点击。
        """
        if self._drag_active:
            # 拖动中**不贴**:画布正跟着光标跑,每 20ms 重贴一次只会让输入框追着栏晃。
            # `_attached_key` 置空:松手后几何已变,必须重贴一次(否则 key 相同会被跳过)。
            self._attached_key = None
            self._hide_box_during_drag()
            return
        try:
            rect = self.canvas.geometry()
            if rect is None:
                return
            height = int(self.view.theme_values.get("fontSize", 20)) + inputbox.HEIGHT_EXTRA
            key: tuple[Any, ...] = (rect, height)
            if key == self._attached_key:
                return  # 几何没变:不重排,免得每 20ms 空转一次 SetWindowPos
            self._attached_key = key
            self.inputbox.attach(rect, above=_needs_above(rect, height))
        except OSError as error:
            self._warn_once("attach", f"OVERLAY_ATTACH_FAILED {type(error).__name__}: {error}")

    def _warn_once(self, key: str, text: str) -> None:
        """同一类失败只报一次:主循环 20ms 一圈,重复刷日志会把宿主日志淹掉。"""
        if self._warned.get(key) == text:
            return
        self._warned[key] = text
        _warn(text)

    # --- 周期性工作 ---

    def _tick(self) -> None:
        try:
            self._expire_hover()
            if self.view.expire_notice(time.monotonic()):
                self._dirty = True
            self._check_fullscreen()
            if self._dirty:
                self._dirty = False
                self.canvas.render(self.view)
            self._sync_box()
        except Exception as error:  # noqa: BLE001 — 一次周期任务失败不能带走主循环
            self._warn_once("tick", f"OVERLAY_TICK_FAILED {type(error).__name__}: {error}")

    def _expire_hover(self) -> None:
        if self._hover_deadline is None or time.monotonic() < self._hover_deadline:
            return
        rect = self.inputbox.geometry()
        # 收起条件按设计 §5.2:「离开画布**与输入框**超过收起延时」—— 光标还在框上、
        # 或正在框里打字时不收,否则手一动到框上就看着它消失。
        if not self.inputbox.focused() and (rect is None or not _cursor_inside(rect)):
            self.inputbox.hide()  # 草稿保留
            self._hover_deadline = None

    def _check_fullscreen(self) -> None:
        if not self.probe_available:
            return
        now = time.monotonic()
        if now - self.last_probe_at < FULLSCREEN_CHECK_SECONDS:
            return
        self.last_probe_at = now
        active = fullscreen.is_fullscreen_foreground(self.probe)
        if active != self.fullscreen_active:
            self.fullscreen_active = active
            self._apply_policy()

    # --- 回调 ---

    def _on_hover(self, entered: bool) -> None:
        if self._drag_active:
            # 拖动期间**冻结悬停**:窗口在动,系统在光标扫过别的窗口时会补进出消息,
            # 放行就是「显示 → 起收起计时 → 再显示」反复横跳(真机实测:输入框闪个不停)。
            return
        if entered:
            # 重复的进入/离开(栏自己移动或变高时系统会补一条真实的 WM_MOUSELEAVE)
            # 不得让输入框闪烁或提前收起:进入即撤销待收起,show()/attach() 都幂等
            self._hover_active = True
            self._hover_deadline = None
            if self._policy_visible():
                self._sync_box()
                self.inputbox.show()
        else:
            self._hover_active = False
            self._hover_deadline = time.monotonic() + self._hide_delay()

    def _hide_delay(self) -> float:
        default = int(theme.DEFAULTS["hoverDelayMs"])
        return max(0.0, _as_int(self.view.theme_values.get("hoverDelayMs"), default) / 1000.0)

    def _on_submit(self, text: str) -> None:
        """回车提交:直接用回调收到的文本。

        输入框在回调**之后**才清空并 hide(),所以这里再读一次 `inputbox.text()` 只会
        拿到空串;清空与 hide() 也由输入框自己负责(Task 6),调用方不做第二遍。
        """
        value = text.strip() if isinstance(text, str) else ""
        if not value:
            return
        self.channel.send(protocol.submit_message(value))
        self.view.set_user(value)
        self.canvas.render(self.view)  # 立即上屏,不等下一次 tick
        self._sync_box()

    def _on_drag_start(self) -> None:
        """按下左键:拖动会话开始(**不在这里 hide()**,见下)。

        单点一下栏(按下-抬起、窗口没动)不该让输入框闪一下 —— 那正是要消灭的「闪」,
        所以藏框推迟到「几何真的变了」的那一刻(`_sync_box` → `_hide_box_during_drag`)。
        """
        self._drag_active = True
        self._drag_pressed_rect = self.canvas.geometry()

    def _hide_box_during_drag(self) -> None:
        """栏真的被拖动了才藏输入框 —— 一次点击(没位移)不算拖动。"""
        if self._drag_hid_box:
            return
        rect = self.canvas.geometry()
        if rect is None or self._drag_pressed_rect is None:
            return
        if rect[:2] == self._drag_pressed_rect[:2]:
            return  # 位置没变:还在"按下"那一帧
        self._drag_hid_box = True
        self.inputbox.hide()  # 草稿保留

    def _on_drag_end(self, x: int, y: int) -> None:
        self._drag_active = False
        was_dragging = self._drag_hid_box
        self._drag_hid_box = False
        self._drag_pressed_rect = None
        self._sync_box()  # 拖动结束后几何已定,立刻重贴(不等下一次 tick)
        # 报位先发:它要写进插件的位置记忆,不能被下面这段恢复逻辑的任何意外挡掉
        self.channel.send(protocol.moved_message(x, y, _screen_id_at(x, y)))
        if not was_dragging:
            return  # 没藏过框(点击/没位移):悬停状态不动,别把框显示出来
        # 拖动期间悬停是冻住的,这里按「光标现在还在不在栏上」把状态补回来:
        # 还在 → 与真实悬停进入同一条路径(它自己会检查显示策略);已经离开 → 起收起计时。
        # 不看 `_hover_active`:那是拖动开始前的快照,松手时可能已经不准了。
        try:
            rect = self.canvas.geometry()
        except OSError:
            rect = None  # 读不到几何就当"不在栏上"(下次真实悬停会自然把框放回来)
        self._on_hover(rect is not None and _cursor_inside(rect))


def _run() -> int:
    channel = _Channel()
    try:
        session = _RunSession(channel)
    except OSError as error:
        # 原因码用设计文档错误表里的 OVERLAY_SPAWN_FAILED(它经 reason_code 进宿主日志)
        detail = f"{type(error).__name__}: {error}"
        channel.send(protocol.error_message("OVERLAY_SPAWN_FAILED", detail))
        _warn(f"OVERLAY_SPAWN_FAILED {detail}")
        return START_FAILURE_EXIT_CODE
    channel.start()  # stdin 线程:必须在窗口都建好之后,消息才不会漏在门外
    return session.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="overlay")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--demo", action="store_true")
    # 自动化用:跑满 N 秒自行退出(--demo 本身一直跑到 Ctrl+C)
    parser.add_argument("--demo-seconds", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--render-sample", metavar="PNG")
    parser.add_argument("--run", action="store_true", help="插件进程驱动的主循环")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.render_sample:
        return _render_sample(args.render_sample)
    if args.demo:
        return _demo(args.demo_seconds)
    if args.run:
        return _run()
    # 没有子命令时不打印 help(argparse 会直接写 sys.stdout,pythonw 下可能是 None)
    _say("用法: overlay.py [--selftest | --demo | --render-sample PNG | --run]", sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
