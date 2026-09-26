"""本插件用到的 Win32 绑定。只做绑定,不含业务逻辑。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)

# --- 窗口样式 ---
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSELEAVE = 0x02A3
WM_CAPTURECHANGED = 0x0215
WM_TIMER = 0x0113
WM_SETFOCUS = 0x0007
WM_KEYDOWN = 0x0100
WM_CTLCOLOREDIT = 0x0133
WM_IME_STARTCOMPOSITION = 0x010D
WM_CHAR = 0x0102
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_QUIT = 0x0012

VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

PM_REMOVE = 0x0001
CFS_FORCE_POSITION = 0x0020  # 注意:不是 CFORCEPOSITION

TME_LEAVE = 0x00000002

# 光标:MAKEINTRESOURCE(32512)/MAKEINTRESOURCE(32513),即资源 id 而非资源名字符串
# (实测传 0x0007 这类普通序号会返回 NULL 且 GetLastError = 1814)。
IDC_ARROW = 0x7F00
IDC_IBEAM = 0x7F01

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

DT_LEFT = 0x0000
DT_CENTER = 0x0001
DT_RIGHT = 0x0002
DT_TOP = 0x0000
DT_WORDBREAK = 0x0010
DT_CALCRECT = 0x0400
DT_NOPREFIX = 0x0800
DT_EDITCONTROL = 0x2000

TRANSPARENT = 1
ANTIALIASED_QUALITY = 4
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4
CLIP_DEFAULT_PRECIS = 0
CLEARTYPE_QUALITY = 5

ES_LEFT = 0x0000
ES_AUTOHSCROLL = 0x0080
ES_MULTILINE = 0x0004
ES_NOHIDESEL = 0x0100

DIB_RGB_COLORS = 0
BI_RGB = 0

LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002

QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5   # 5 is NOT quiet time
QUNS_QUIET_TIME = 6
QUNS_APP = 7

SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 2

SHELL_CLASS_NAMES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow")


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    # MSDN 定义这四个字段都是 BYTE(无符号)。用 c_byte 虽然字节布局相同、
    # 传给 UpdateLayeredWindow 行为一致,但 255 读回来会变成 -1,是个留给
    # 调用方的坑(任何 Python 侧断言都会拿到 -1)。
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class COMPOSITIONFORM(ctypes.Structure):
    _fields_ = [
        ("dwStyle", wintypes.DWORD),
        ("ptCurrentPos", POINT),
        ("rcArea", RECT),
    ]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("hwndTrack", wintypes.HWND),
        ("dwHoverTime", wintypes.DWORD),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassExW.restype = wintypes.WORD
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
# GetModuleHandleW 只由 kernel32 导出(user32 没有此函数),绑定见文件末尾 kernel32 段。
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
# 拖动用:WM_MOUSEMOVE 的 lparam 是客户端坐标,而窗口自己在跟着光标走 ——
# 拿客户端坐标算增量会把窗口刚走过的位移又扣掉一次(栏只走一半并来回抖),
# 必须先换成屏幕坐标(见 canvas 的 _screen_point)。
user32.ClientToScreen.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,  # BYTE 无符号,与 BLENDFUNCTION 一致
]
user32.SetTimer.restype = ctypes.c_void_p  # UINT_PTR:64 位下默认 c_int 会截断
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT, wintypes.LPVOID]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]  # UINT_PTR
user32.SetCapture.restype = wintypes.HWND
user32.SetCapture.argtypes = [wintypes.HWND]
user32.ReleaseCapture.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.LoadCursorW.restype = wintypes.HANDLE  # HCURSOR
# 第二形参是 MAKEINTRESOURCE 资源 id(整数);声明成 LPCWSTR 时 ctypes 会
# 拒绝整数(TypeError: wrong type),故按指针宽度收 c_void_p。
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SendMessageW.restype = ctypes.c_ssize_t  # LRESULT 是 LONG_PTR,64 位下默认 c_int 会截断
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]

# FillRect / DrawTextW 由 user32 导出(不是 gdi32),调用点见 raster。
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
user32.DrawTextW.restype = ctypes.c_int
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), wintypes.UINT]

gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.CreateFontW.restype = wintypes.HFONT
# CreateFontW 共 14 个参数(cHeight, cWidth, cEscapement, cOrientation, cWeight,
# bItalic, bUnderline, bStrikeOut, iCharSet, iOutPrecision, iClipPrecision,
# iQuality, iPitchAndFamily, pszFaceName)。
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.LPCWSTR,
]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.RoundRect.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int,
]
gdi32.CreatePen.restype = wintypes.HPEN
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
# 输入框圆角:区域句柄交给 SetWindowRgn 后由系统接管,不要再自己删。
gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
gdi32.CreateRoundRectRgn.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
]

kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# 输入法:输入框窗口用它们把候选窗定位到 EDIT 下方。
imm32.ImmGetContext.restype = wintypes.HANDLE
imm32.ImmGetContext.argtypes = [wintypes.HWND]
imm32.ImmReleaseContext.restype = wintypes.BOOL
imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
imm32.ImmSetCompositionWindow.restype = wintypes.BOOL
imm32.ImmSetCompositionWindow.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMPOSITIONFORM)]

shell32.SHQueryUserNotificationState.restype = ctypes.c_long
shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]

CLR_INVALID = 0xFFFFFFFF


def loword(value: int) -> int:
    """取 WPARAM/LPARAM 的低 16 位,按**有符号**解释(拖动时鼠标坐标可以为负)。"""
    return ctypes.c_short(value & 0xFFFF).value


def hiword(value: int) -> int:
    """取 WPARAM/LPARAM 的高 16 位,按**有符号**解释。"""
    return ctypes.c_short((value >> 16) & 0xFFFF).value
