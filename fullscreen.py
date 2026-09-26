"""全屏应用检测。两道判据见 spec 第 5.4 节。"""

from __future__ import annotations

import ctypes

import win32

_COVER_TOLERANCE = 2  # 允许 2 像素误差


class FullscreenProbe:
    """真实探针。测试用子类覆盖这四个方法即可脱离桌面。"""

    def notification_state(self) -> int:
        state = ctypes.c_int()
        result = win32.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if result != 0:
            raise OSError(f"SHQueryUserNotificationState 返回 {result}")
        return state.value

    def foreground_rect(self) -> tuple[int, int, int, int] | None:
        hwnd = win32.user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = win32.RECT()
        if not win32.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def monitor_rect(self) -> tuple[int, int, int, int] | None:
        hwnd = win32.user32.GetForegroundWindow()
        if not hwnd:
            return None
        monitor = win32.user32.MonitorFromWindow(hwnd, win32.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not win32.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return (info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom)

    def foreground_class(self) -> str:
        hwnd = win32.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        buffer = ctypes.create_unicode_buffer(256)
        win32.user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", win32.RECT),
        ("rcWork", win32.RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


win32.user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]


def _covers(window: tuple[int, int, int, int], monitor: tuple[int, int, int, int]) -> bool:
    """判据 2 的核心:窗口矩形是否覆盖整块屏幕(四条边各允许 `_COVER_TOLERANCE` 像素误差)。

    **基准必须是 `monitor_rect()` 返回的 `rcMonitor`(整块屏幕),不要改成 `rcWork`(工作区)**:
    任务栏可见时,普通最大化窗口正好比屏幕矮一条任务栏,用 rcMonitor 就不会把它误判成全屏;
    换成 rcWork 则「一最大化就藏浮窗」。

    已知误判面(设计取舍,不是 bug):任务栏设为「自动隐藏」时 `rcWork == rcMonitor`,而 Windows
    的最大化窗口会向四周外扩约 8px(不可见边框),于是**最大化任何普通窗口都会命中本判据、浮窗
    被隐藏**。缓解:任务栏改回常显,或关掉 `hideInFullscreen` 开关。彻底区分需要
    `GWL_STYLE & WS_MAXIMIZE` 配合 `WS_CAPTION`/`WS_THICKFRAME`,但有反向风险 —— 部分无边框全屏
    游戏本身就是最大化窗口,加了判别式反而会让浮窗盖在游戏上(那正是用户最在意要藏起来的场景),
    故本次不加。详见设计文档第 11 节「已知限制」。
    """
    return (
        window[0] <= monitor[0] + _COVER_TOLERANCE
        and window[1] <= monitor[1] + _COVER_TOLERANCE
        and window[2] >= monitor[2] - _COVER_TOLERANCE
        and window[3] >= monitor[3] - _COVER_TOLERANCE
    )


def is_fullscreen_foreground(probe: FullscreenProbe | None = None) -> bool:
    probe = probe or FullscreenProbe()
    try:
        # 判据 1:通知状态(3 = 独占全屏 D3D,4 = 演示模式)
        state = probe.notification_state()
        if state in (win32.QUNS_RUNNING_D3D_FULL_SCREEN, win32.QUNS_PRESENTATION_MODE):
            return True
        if probe.foreground_class() in win32.SHELL_CLASS_NAMES:
            return False
        # 判据 2:几何覆盖(容差与「自动隐藏任务栏」这个已知误判面见 _covers 的说明)
        window = probe.foreground_rect()
        monitor = probe.monitor_rect()
        if window is None or monitor is None:
            return False
        return _covers(window, monitor)
    except Exception:
        return False
