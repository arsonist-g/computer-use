"""验收脚本共用的小工具 —— 造一个受控的靶子窗口、量它的矩形、读回截图的像素。

为什么需要它：验收 §2 / §3 / §6 都要「在真实窗口上做一件事，再客观地看结果」。
靶子必须是**我们自己起的**窗口（不能拿用户正开着的记事本去点），量结果必须用
像素与矩形，而不是「看起来对了」。

这里刻意不 import 任何产品模块之外的东西，也不给产品代码加绑定 —— 测试需要而
产品不需要的 Win32 调用（`PostMessageW` 等）就在本文件里用 ctypes 直接取。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path  # noqa: E402

add_src_to_path()

from cu.desktop import win32 as w  # noqa: E402
from cu.desktop import windows as windows_mod  # noqa: E402
from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402

#: **必须在 import 时就声明**，而且要早于本模块里任何一次坐标读取。
#:
#: 不声明的后果是整条测试链偏 1/4 屏：进程被 DPI 虚拟化后，`GetWindowRect` /
#: `ClientToScreen` 给你的都是缩放过的逻辑坐标，而产品（daemon）是按物理像素说话的
#: （CONSTRAINT-002）。实测过一次：测试进程读到窗口在 (300,220) 900x560，
#: 而同一时刻 daemon 看到的是 (375,275) 1111x693 —— 比值正好 1.25，
#: 也就是本机 125% 缩放。这类偏差会被误读成「产品坐标映射有 bug」。
ensure_dpi_awareness()

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]

WM_CLOSE = 0x0010
HWND_TOP = 0
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def _top_level_hwnds() -> set[int]:
    return {info.hwnd for info in
            windows_mod.enumerate_windows(windows_mod.display_context().monitors,
                                          all_windows=True)}


def spawn(exe: str, match: str | tuple[str, ...], timeout: float = 20.0,
          new_console: bool = False) -> int | None:
    """起一个进程，返回它**新出现**的顶层窗口句柄。

    只看「新出现的」：用户可能正开着一个记事本，不能把他的当成我们的靶子
    —— 后面要往它里面打字、还要关掉它。

    `match` 允许给多个候选：一个控制台窗口的属主可能是 `cmd.exe`，也可能是
    `conhost.exe` / `WindowsTerminal.exe`，取决于系统怎么托管它。

    `new_console`：控制台程序必须带 `CREATE_NEW_CONSOLE`，否则它会**附着到
    当前这个控制台**上、压根不建窗口 —— 表现是「等了 20 秒也没等到新窗口」。
    """
    keys = (match,) if isinstance(match, str) else match
    before = _top_level_hwnds()
    flags = subprocess.CREATE_NEW_CONSOLE if new_console else 0
    try:
        subprocess.Popen([exe], close_fds=True, creationflags=flags)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        for info in windows_mod.enumerate_windows(windows_mod.display_context().monitors,
                                                  all_windows=True):
            if info.hwnd in before:
                continue
            process = (info.process or "").lower()
            if any(key.lower() in process for key in keys):
                # 窗口刚建出来时可能还没画、也没有标题，等它稳定下来。
                time.sleep(1.0)
                return info.hwnd
    return None


def spawn_notepad() -> int | None:
    """记事本。Windows 11 的记事本进程名可能是 Notepad.exe 或带包名前缀。"""
    return spawn("notepad.exe", "notepad")


def spawn_console() -> int | None:
    """一个黑底控制台窗口 —— 用作 §3.1 的遮挡物，与白底记事本反差最大。"""
    return spawn("cmd.exe", ("cmd.exe", "conhost", "windowsterminal"), new_console=True)


def close(hwnd: int) -> None:
    """请窗口自己关闭（WM_CLOSE）。这才是「点关闭按钮」同一条路径。"""
    _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(30):
        if not w.user32.IsWindow(hwnd):
            return
        time.sleep(0.1)


def kill_of(exe: str) -> None:
    """兜底：按进程名收尸（窗口没关掉时用）。"""
    subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True, timeout=20)


def alive(hwnd: int) -> bool:
    return bool(w.user32.IsWindow(hwnd))


def rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """(left, top, width, height)。**含不可见调整边框**的 GetWindowRect。"""
    raw = w.window_rect(hwnd)
    if raw is None:
        return None
    left, top, right, bottom = raw
    return left, top, right - left, bottom - top


def extended_frame(hwnd: int) -> tuple[int, int, int, int] | None:
    """DWM 真实可见框 (left, top, width, height) —— 截图坐标换算的基准。"""
    raw = w.extended_frame_bounds(hwnd)
    if raw is None:
        return None
    left, top, right, bottom = raw
    return left, top, right - left, bottom - top


def move(hwnd: int, x: int, y: int, width: int, height: int,
         top: bool = False) -> bool:
    """搬动 / 改尺寸。`top=True` 时同时置到 z-order 顶端（做遮挡物用）。

    先 `SW_RESTORE`：Windows 11 的记事本会记住自己上次的窗口状态，重新拉起时
    可能直接是**最小化**的（矩形是 `-32000,-32000`）。不先还原，
    `SetWindowPos` 会作用在一个最小化窗口上，后面读到的一切都是假的。
    """
    if _win32.IsIconic(hwnd):
        _win32.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(0.4)
    flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
    after = HWND_TOP if top else 0
    return bool(_user32.SetWindowPos(hwnd, after, x, y, width, height, flags))


def image(path: str | Path):
    """读回保存的截图（numpy BGR 数组）。cv2 读不了非 ASCII 路径，DEC-043 已保证纯 ASCII。"""
    import cv2

    data = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if data is None:
        raise RuntimeError(f"读不出图片：{path}")
    return data


def frame_mean(path: str | Path) -> float:
    """整图均值。全黑检测用的就是它（capture._frame_is_black 的同一个判据）。"""
    return float(image(path).mean())


def cursor_pos() -> tuple[int, int]:
    return w.cursor_pos()


def settle(hwnd: int, quiet: float = 1.5, timeout: float = 15.0) -> tuple[int, int, int, int] | None:
    """等窗口自己消停下来，返回稳定的矩形。

    为什么必须有这一步：Windows 11 的记事本会在变成可见/被激活之后**再套用它自己记住的
    窗口位置与尺寸**，把我们刚 `SetWindowPos` 设的值覆盖掉。不等它，就会拿到
    「我设的矩形」和「截图里的窗口」两个不同时刻的快照，然后把差异误读成坐标偏移。
    """
    deadline = time.monotonic() + timeout
    last = rect(hwnd)
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.25)
        current = rect(hwnd)
        if current != last:
            last = current
            stable_since = time.monotonic()
            continue
        if time.monotonic() - stable_since >= quiet:
            return current
    return last


# ---------------------------------------------------------------------------
# 把「输入的结果」读回来 —— 验收 §6 的判据不能是「看起来对了」
# ---------------------------------------------------------------------------

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
EM_GETSEL = 0x00B0
EM_GETFIRSTVISIBLELINE = 0x00CE
EM_POSFROMCHAR = 0x00D6

#: 记事本的编辑区在这几个类名下（不同版本 / 不同实现各占一个）。
_EDIT_CLASSES = ("RichEditD2DPT", "RichEdit50W", "Edit", "RICHEDIT60W")

_win32 = ctypes.WinDLL("user32", use_last_error=True)


def _bind() -> None:
    _win32.FindWindowExW.restype = wintypes.HWND
    _win32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR,
                                     wintypes.LPCWSTR]
    _win32.EnumChildWindows.restype = wintypes.BOOL
    _win32.SendMessageW.restype = ctypes.c_ssize_t
    _win32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM]
    _win32.ClientToScreen.restype = wintypes.BOOL
    _win32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _win32.GetClientRect.restype = wintypes.BOOL
    _win32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


_bind()


def child_window(parent: int, classes: tuple[str, ...] = _EDIT_CLASSES) -> int | None:
    """父窗口下的第一个编辑控件（记事本的文本区），**递归**查找。

    `FindWindowExW` 只找**直接子窗口**，而 WinUI 版记事本把编辑控件嵌在
    `Notepad → NotepadTextBox → RichEditD2DPT` 三层里，直接子窗口一个都不是编辑区。
    所以这里用 `EnumChildWindows`：它枚举的是全部后代。

    找不到**不算失败** —— 换一个版本、换一个应用就没有这个子窗口；
    调用方据此把依赖它的验收项判为「本机无法构造」。
    """
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if w.class_name(hwnd) in classes:
            hits.append(int(hwnd))
            return False            # 找到就停，别白扫剩下的几百个
        return True

    _win32.EnumChildWindows.argtypes = [wintypes.HWND, type(callback), wintypes.LPARAM]
    _win32.EnumChildWindows(parent, callback, 0)
    del callback                    # 回调必须活到枚举结束（同钩子回调那个陷阱）
    return hits[0] if hits else None


def send_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
    return int(_win32.SendMessageW(hwnd, message, wparam, lparam))


def window_text(hwnd: int) -> str:
    length = send_message(hwnd, WM_GETTEXTLENGTH)
    buffer = ctypes.create_unicode_buffer(length + 2)
    _win32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.cast(buffer, ctypes.c_void_p))
    return buffer.value


def first_visible_line(edit_hwnd: int) -> int:
    """编辑控件当前最上面那一行的行号 —— 滚轮方向的客观读数。"""
    return send_message(edit_hwnd, EM_GETFIRSTVISIBLELINE)


def char_point(edit_hwnd: int, index: int) -> tuple[int, int]:
    """第 `index` 个字符在编辑控件**客户区**里的坐标（EM_POSFROMCHAR）。"""
    packed = send_message(edit_hwnd, EM_POSFROMCHAR, index)
    x = ctypes.c_short(packed & 0xFFFF).value
    y = ctypes.c_short((packed >> 16) & 0xFFFF).value
    return x, y


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    point = wintypes.POINT(x, y)
    if not _win32.ClientToScreen(hwnd, ctypes.byref(point)):
        raise RuntimeError("ClientToScreen 失败")
    return point.x, point.y


def clipboard_get() -> str | None:
    """读文本剪贴板。读不到返回 None（不抛 —— 它不是被验的对象）。"""
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                              capture_output=True, text=True, timeout=10)
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def clipboard_set(text: str) -> bool:
    try:
        proc = subprocess.run(["clip"], input=text.encode("utf-16-le"),
                              capture_output=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
