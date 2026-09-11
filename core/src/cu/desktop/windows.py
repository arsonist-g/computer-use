"""窗口枚举、过滤、提权检测、hwnd 校验（api-contract.md §1.2 / DEC-013 / DEC-021 / DEC-024）。

`elevated` 是**必需字段而非装饰**：本工具无法操作提权窗口（UIPI 拦截输入），
AI 需要在**枚举阶段**就绕开它们，而不是在点击时才失败（DEC-024）。

hwnd 会被系统复用，所以「hwnd 存在」不足以保证「这就是那个窗口」——
写操作前置的身份校验要比对 pid 与窗口类（DEC-013）。
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass

from ..errors import CUError, ErrorCode
from ..manifest import DisplayContext, MonitorInfo
from . import win32 as w
from .base import WindowInfo

#: 提权查询的缓存：一次 `windows` 枚举里几十个窗口往往属于十来个进程，
#: 逐个 OpenProcess 是纯浪费。缓存键是 pid，值是一次枚举的生命周期。
_ELEVATION_CACHE: dict[int, bool] = {}
_PROCESS_NAME_CACHE: dict[int, str] = {}


def clear_caches() -> None:
    """每次枚举前清空 —— 进程名与提权状态都会随进程启动/退出变化。"""
    _ELEVATION_CACHE.clear()
    _PROCESS_NAME_CACHE.clear()


def process_name(pid: int) -> str:
    if pid in _PROCESS_NAME_CACHE:
        return _PROCESS_NAME_CACHE[pid]
    name = ""
    handle = w.kernel32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(size.value)
            if w.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1]
        finally:
            w.kernel32.CloseHandle(handle)
    _PROCESS_NAME_CACHE[pid] = name
    return name


def is_elevated(pid: int) -> bool:
    """目标进程是否以更高完整性级别运行。

    判据是令牌的 elevation 状态，不是「能不能 OpenProcess」—— 后者受权限影响，
    在开了调试权限的进程里会给出错误答案。
    """
    if pid in _ELEVATION_CACHE:
        return _ELEVATION_CACHE[pid]
    elevated = _probe_elevated(pid)
    _ELEVATION_CACHE[pid] = elevated
    return elevated


def _probe_elevated(pid: int) -> bool:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    handle = w.kernel32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 打不开往往正是因为对方提权（或已被保护）。保守判为提权：
        # 猜错的代价是不去操作一个能操作的窗口（可恢复），
        # 反过来的代价是 UIPI 静默吞掉输入（不可恢复、且会误导 AI）。
        return True
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, w.TOKEN_QUERY, ctypes.byref(token)):
            return True
        try:
            info = TOKEN_ELEVATION()
            size = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(info),
                                              ctypes.sizeof(info), ctypes.byref(size))
            return bool(info.TokenIsElevated) if ok else True
        finally:
            w.kernel32.CloseHandle(token)
    finally:
        w.kernel32.CloseHandle(handle)


def _is_top_level(hwnd: int) -> bool:
    """默认过滤：可见、有标题、非工具窗、未被 DWM 隐藏。

    刻意**不**判断「是否被遮挡」——「置顶」与「被遮挡」是两个概念，
    把后者当过滤条件会把大量正常窗口滤掉（DEC-021 修正了 sketch 的这处概念混用）。
    """
    if not w.user32.IsWindowVisible(hwnd):
        return False
    if w.is_cloaked(hwnd):
        return False
    owner = w.user32.GetWindow(hwnd, w.GW_OWNER)
    ex_style = w.window_ex_style(hwnd)
    if owner and not (ex_style & w.WS_EX_APPWINDOW):
        return False            # 有主窗口的附属窗，不是独立应用
    if ex_style & w.WS_EX_TOOLWINDOW and not (ex_style & w.WS_EX_APPWINDOW):
        return False
    style = w.window_style(hwnd)
    if style & w.WS_EX_TOOLWINDOW:
        return False
    return True


def _monitor_index_for(hwnd: int, monitors: list[MonitorInfo]) -> int:
    handle = w.user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = w.MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(w.MONITORINFOEXW)
    if not handle or not w.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return 0
    rect = [info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom]
    for monitor in monitors:
        if monitor.rect == rect:
            return monitor.index
    return 0


def enumerate_windows(monitors: list[MonitorInfo],
                      all_windows: bool = False) -> list[WindowInfo]:
    """按 **z-order 从上到下**返回窗口（api-contract.md §1.2）。

    `EnumWindows` 的枚举顺序本身就是 z-order，所以不排序 —— 排序反而会毁掉这个顺序。
    """
    clear_caches()
    foreground = w.foreground_hwnd()
    collected: list[tuple[int, WindowInfo]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if all_windows:
            if not w.user32.IsWindowVisible(hwnd):
                return True
        elif not _is_top_level(hwnd) or not w.window_text(hwnd).strip():
            return True

        rect = w.window_rect(hwnd)
        if rect is None:
            return True
        pid = wintypes.DWORD(0)
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        x, y, right, bottom = rect
        info = WindowInfo(
            hwnd=int(hwnd),
            title=w.window_text(hwnd),
            pid=pid.value,
            process=process_name(pid.value),
            rect=(x, y, right - x, bottom - y),
            monitor=_monitor_index_for(hwnd, monitors),
            is_foreground=(int(hwnd) == foreground),
            is_minimized=bool(w.user32.IsIconic(hwnd)),
            elevated=is_elevated(pid.value),
            klass=w.class_name(hwnd),
            is_topmost=bool(w.window_ex_style(hwnd) & w.WS_EX_TOPMOST),
            zorder=len(collected) + 1,
        )
        collected.append((int(hwnd), info))
        return True

    w.user32.EnumWindows(callback, 0)
    # 回调对象必须活到 EnumWindows 返回之后 —— 这里显式持有引用，
    # 否则 CPython 可能在枚举期间回收它（与钩子回调同一个陷阱）。
    del callback
    return [info for _hwnd, info in collected]


def display_context() -> DisplayContext:
    """会话开始时的显示器配置快照（data-model.md §3.3 的 `display`）。"""
    monitors: list[MonitorInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                        ctypes.POINTER(w.RECT), wintypes.LPARAM)
    def callback(handle, _hdc, _rect, _lparam):
        info = w.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(w.MONITORINFOEXW)
        if not w.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return True
        rect = [info.rcMonitor.left, info.rcMonitor.top,
                info.rcMonitor.right, info.rcMonitor.bottom]
        dpi = _dpi_of_monitor(handle)
        monitors.append(MonitorInfo(
            index=len(monitors),
            rect=rect,
            primary=bool(info.dwFlags & 1),     # MONITORINFOF_PRIMARY
            dpi=dpi,
            scale=dpi / 96.0,
        ))
        return True

    w.user32.EnumDisplayMonitors(None, None, callback, 0)
    del callback

    primary = next((m.index for m in monitors if m.primary), 0)
    return DisplayContext(primary_index=primary, monitors=monitors)


def _dpi_of_monitor(handle) -> int:
    try:
        w.shcore.GetDpiForMonitor.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.POINTER(wintypes.UINT),
                                              ctypes.POINTER(wintypes.UINT)]
        x = wintypes.UINT(0)
        y = wintypes.UINT(0)
        if w.shcore.GetDpiForMonitor(handle, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
            return int(x.value)
    except (AttributeError, OSError):
        pass
    return 96


# ---------------------------------------------------------------------------
# hwnd 校验（DEC-013 的身份校验）
# ---------------------------------------------------------------------------


@dataclass
class WindowCheck:
    hwnd: int
    info: WindowInfo


def check_hwnd(hwnd: int, expect_pid: int | None = None,
               expect_class: str | None = None) -> WindowCheck:
    """写操作前置的身份校验。任一不满足即抛错，绝不「尽力而为地点击」。

    三种失败各有各的码，因为 AI 的反应不同：
      - `window_not_found` —— 重新枚举；
      - `window_stale`     —— hwnd 被系统复用给了别的进程，**必须**重新枚举；
      - `window_minimized` —— 请用户还原窗口。
    """
    from ..ids import format_hwnd

    if not w.user32.IsWindow(hwnd):
        raise CUError(ErrorCode.WINDOW_NOT_FOUND,
                      f"窗口不存在：{format_hwnd(hwnd)}", {"hwnd": format_hwnd(hwnd)})

    pid = wintypes.DWORD(0)
    w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    klass = w.class_name(hwnd)

    if expect_pid is not None and pid.value != expect_pid:
        raise CUError(
            ErrorCode.WINDOW_STALE,
            f"hwnd {format_hwnd(hwnd)} 现属 pid={pid.value}({process_name(pid.value)})，"
            f"与记录的 pid={expect_pid} 不符",
            {"hwnd": format_hwnd(hwnd), "actual_pid": pid.value, "expected_pid": expect_pid},
        )
    if expect_class is not None and klass != expect_class:
        raise CUError(
            ErrorCode.WINDOW_STALE,
            f"hwnd {format_hwnd(hwnd)} 的窗口类为 {klass!r}，与记录的 {expect_class!r} 不符",
            {"hwnd": format_hwnd(hwnd), "actual_class": klass, "expected_class": expect_class},
        )

    if w.user32.IsIconic(hwnd):
        raise CUError(ErrorCode.WINDOW_MINIMIZED,
                      f"窗口已最小化：{format_hwnd(hwnd)}（{w.window_text(hwnd)}）",
                      {"hwnd": format_hwnd(hwnd)})

    elevated = is_elevated(pid.value)
    if elevated:
        # 显式报错而不是让 UIPI 静默吞掉输入（DEC-024 / 约束 4）。
        raise CUError(
            ErrorCode.ELEVATED_WINDOW,
            f"目标窗口属提权进程（pid={pid.value} {process_name(pid.value)}），"
            "UIPI 会拦截本工具的输入",
            {"hwnd": format_hwnd(hwnd), "pid": pid.value},
        )

    rect = w.window_rect(hwnd) or (0, 0, 0, 0)
    x, y, right, bottom = rect
    info = WindowInfo(
        hwnd=hwnd, title=w.window_text(hwnd), pid=pid.value,
        process=process_name(pid.value), rect=(x, y, right - x, bottom - y),
        monitor=0, is_foreground=(hwnd == w.foreground_hwnd()),
        is_minimized=False, elevated=False, klass=klass,
    )
    return WindowCheck(hwnd=hwnd, info=info)


def bring_to_foreground(hwnd: int) -> None:
    """把目标窗口提到前台并**确认成功**（DEC-013 第 2 层）。

    不确认的话，点击会落到当时真正的前台窗口上 —— Windows 的输入是发给前台窗口的，
    不是发给「你心里想的那个窗口」的。这正是 DEC-013 记录的第 1 个失效模式。
    """
    if w.foreground_hwnd() == hwnd:
        return
    w.user32.ShowWindow(hwnd, w.SW_RESTORE)
    w.user32.SetForegroundWindow(hwnd)
    if w.foreground_hwnd() != hwnd:
        from ..ids import format_hwnd

        raise CUError(ErrorCode.FOREGROUND_FAILED,
                      f"无法把窗口提到前台：{format_hwnd(hwnd)}",
                      {"hwnd": format_hwnd(hwnd)})


def all_monitors() -> Iterable[MonitorInfo]:
    return display_context().monitors
