"""模态 popup 的枚举、合成截图与前台路由。

行为权威是任务契约本身：Windows 用 `GW_ENABLEDPOPUP` 暴露被当前模态窗锁住的 owner；
默认窗口枚举要收下这个 enabled 的 owned popup，截图要裁到 owner + popup 的并集，
写前置要沿模态链把前台交给实际可接收输入的 popup。

本文件不碰真实桌面：`windows` / `capture` 两个模块的 `w` 都换成图替身，截图后端也换成
内存帧。所有 expected 值都手算自独立的 Win32 关系，不取自当前实现输出。
"""

from __future__ import annotations

import ctypes
import sys
import types
from ctypes import wintypes
from pathlib import Path

import pytest

from cu.desktop import capture as capture_mod
from cu.desktop import windows as windows_mod
from cu.desktop.base import WindowInfo
from cu.manifest import DisplayContext, MonitorInfo

# ---------------------------------------------------------------------------
# Win32 常量与手算场景（oracle: specified，来源是 Win32 API 文档与契约）
# ---------------------------------------------------------------------------

GW_OWNER = 4                 # GetWindow(hwnd, GW_OWNER)
GW_ENABLEDPOPUP = 6          # GetWindow(hwnd, GW_ENABLEDPOPUP)
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

OWNER_HWND = 0x00010001
POPUP_HWND = 0x00010002
PLAIN_OWNED_HWND = 0x00010003
PID = 4242

OWNER_FRAME = (100, 100, 300, 300)          # (left, top, right, bottom)
POPUP_FRAME = (360, 80, 560, 260)           # 完全在 owner 右侧之外
UNION = (100, 80, 560, 300)                 # 手算并集
MONITOR_SIZE = (1000, 800)


class _FakeMeanBuffer:
    """`frame_buffer.mean()` 的最小替身；非黑即可。"""

    def mean(self) -> float:
        return 255.0


class _FakeCaptureControl:
    def stop(self) -> None:
        pass


class _FakeFrame:
    """内存帧替身：记下 crop 请求，并把保存动作落成一个非空文件。"""

    def __init__(self, width: int, height: int, log: dict) -> None:
        self.width = int(width)
        self.height = int(height)
        self.frame_buffer = _FakeMeanBuffer()
        self._log = log

    def crop(self, *bounds: int | tuple[int, int, int, int]) -> _FakeFrame:
        if len(bounds) == 1 and isinstance(bounds[0], tuple):
            left, top, right, bottom = bounds[0]
        else:
            left, top, right, bottom = bounds
        rect = (int(left), int(top), int(right), int(bottom))
        self._log["crops"].append(rect)
        return _FakeFrame(rect[2] - rect[0], rect[3] - rect[1], self._log)

    def save_as_image(self, path: str) -> None:
        self._log["saved"].append((self.width, self.height))
        Path(path).write_bytes(b"fake-png")


class _FakeWindowsCapture:
    """`windows_capture.WindowsCapture` 的替身。

    契约要求 owner 被模态 popup 锁住时走**显示器合成图**，所以测试直接记录构造参数：
    `monitor_index` 路径才算合成图；`window_hwnd` 路径仍是旧的单窗口表面。
    """

    def __init__(self, *, log: dict, **kwargs) -> None:
        self._log = log
        self.kwargs = kwargs
        self.frame_handler = None
        self.closed_handler = None
        log["constructors"].append(dict(kwargs))

    def event(self, handler):
        if handler.__name__ == "on_frame_arrived":
            self.frame_handler = handler
        elif handler.__name__ == "on_closed":
            self.closed_handler = handler
        return handler

    def start_free_threaded(self) -> _FakeCaptureControl:
        if "monitor_index" in self.kwargs:
            width, height = MONITOR_SIZE
        else:
            width, height = OWNER_FRAME[2] - OWNER_FRAME[0], OWNER_FRAME[3] - OWNER_FRAME[1]
        self.frame_handler(_FakeFrame(width, height, self._log), _FakeCaptureControl())
        return _FakeCaptureControl()


def _install_capture_backend(monkeypatch: pytest.MonkeyPatch, log: dict) -> None:
    """从 `windows_capture` 边界替换后端，不依赖 capture 模块的私有函数名。"""

    def factory(**kwargs):
        return _FakeWindowsCapture(log=log, **kwargs)

    monkeypatch.setitem(sys.modules, "windows_capture",
                        types.SimpleNamespace(WindowsCapture=factory))


class _FakeUser32:
    def __init__(self, graph: _FakeWin32) -> None:
        self._graph = graph

    def EnumWindows(self, callback, lparam) -> bool:
        for hwnd in self._graph.zorder:
            callback(hwnd, lparam)
        return True

    def IsWindow(self, hwnd: int) -> bool:
        return hwnd in self._graph.visible

    def IsWindowVisible(self, hwnd: int) -> bool:
        return self._graph.visible.get(hwnd, False)

    def IsWindowEnabled(self, hwnd: int) -> bool:
        return self._graph.enabled.get(hwnd, False)

    def IsIconic(self, hwnd: int) -> bool:
        return False

    def GetWindow(self, hwnd: int, command: int) -> int:
        if command == GW_OWNER:
            return self._graph.owners.get(hwnd, 0)
        if command == GW_ENABLEDPOPUP:
            return self._graph.enabled_popups.get(hwnd, 0)
        return 0

    def GetWindowThreadProcessId(self, hwnd: int, out) -> int:
        if out is not None:
            ctypes.cast(out, ctypes.POINTER(wintypes.DWORD)).contents.value = PID
        return 1

    def SetForegroundWindow(self, hwnd: int) -> bool:
        self._graph.foreground_calls.append(hwnd)
        if hwnd in self._graph.foreground_ok:
            self._graph.foreground = hwnd
            return True
        return False

    def AttachThreadInput(self, target: int, mine: int, attach: bool) -> bool:
        self._graph.attach_calls.append((target, mine, attach))
        return True

    def SetFocus(self, hwnd: int) -> int:
        self._graph.focused = hwnd
        return 0


class _FakeKernel32:
    def GetCurrentThreadId(self) -> int:
        return 0x2222


class _FakeWin32:
    """同时承载枚举、捕获边界与前台状态机的最小替身。"""

    GW_OWNER = GW_OWNER
    GW_ENABLEDPOPUP = GW_ENABLEDPOPUP
    WS_EX_APPWINDOW = WS_EX_APPWINDOW
    WS_EX_TOOLWINDOW = WS_EX_TOOLWINDOW
    WS_EX_TOPMOST = WS_EX_TOPMOST

    def __init__(self) -> None:
        self.visible = {OWNER_HWND: True, POPUP_HWND: True, PLAIN_OWNED_HWND: True}
        self.enabled = {OWNER_HWND: False, POPUP_HWND: True, PLAIN_OWNED_HWND: True}
        self.owners = {POPUP_HWND: OWNER_HWND, PLAIN_OWNED_HWND: OWNER_HWND}
        self.enabled_popups = {OWNER_HWND: POPUP_HWND}
        self.zorder = [POPUP_HWND, OWNER_HWND, PLAIN_OWNED_HWND]
        self.foreground_ok = {POPUP_HWND, OWNER_HWND}
        self.foreground = 0
        self.focused: int | None = None
        self.foreground_calls: list[int] = []
        self.attach_calls: list[tuple[int, int, bool]] = []
        self.user32 = _FakeUser32(self)
        self.kernel32 = _FakeKernel32()

    def foreground_hwnd(self) -> int:
        return self.foreground

    def is_window_enabled(self, hwnd: int) -> bool:
        return self.enabled.get(hwnd, False)

    def is_cloaked(self, hwnd: int) -> bool:
        return False

    def window_text(self, hwnd: int) -> str:
        return {OWNER_HWND: "owner", POPUP_HWND: "popup", PLAIN_OWNED_HWND: "plain"}[hwnd]

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return {OWNER_HWND: OWNER_FRAME, POPUP_HWND: POPUP_FRAME,
                PLAIN_OWNED_HWND: OWNER_FRAME}.get(hwnd)

    def extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return {OWNER_HWND: OWNER_FRAME, POPUP_HWND: POPUP_FRAME,
                PLAIN_OWNED_HWND: OWNER_FRAME}.get(hwnd)

    def window_style(self, hwnd: int) -> int:
        return 0

    def window_ex_style(self, hwnd: int) -> int:
        return 0

    def class_name(self, hwnd: int) -> str:
        return "#32770" if hwnd == POPUP_HWND else "OwnerClass"

    def cursor_pos(self) -> tuple[int, int]:
        return 0, 0


def _owner_info() -> WindowInfo:
    left, top, right, bottom = OWNER_FRAME
    return WindowInfo(hwnd=OWNER_HWND, title="owner", pid=PID, process="probe.exe",
                      rect=(left, top, right - left, bottom - top), monitor=0,
                      is_foreground=False, is_minimized=False, elevated=False)


def _install_windows_graph(monkeypatch: pytest.MonkeyPatch, graph: _FakeWin32) -> _FakeWin32:
    monkeypatch.setattr(windows_mod, "w", graph)
    monkeypatch.setattr(windows_mod, "process_name", lambda _pid: "probe.exe")
    monkeypatch.setattr(windows_mod, "is_elevated", lambda _pid: False)
    monkeypatch.setattr(windows_mod, "monitor_index_for", lambda _hwnd, _monitors: 0,
                        raising=False)
    if hasattr(windows_mod, "_monitor_index_for"):
        monkeypatch.setattr(windows_mod, "_monitor_index_for", lambda _hwnd, _monitors: 0)
    return graph


def _install_capture_graph(monkeypatch: pytest.MonkeyPatch, graph: _FakeWin32) -> _FakeWin32:
    monkeypatch.setattr(capture_mod, "w", graph)
    monkeypatch.setattr(windows_mod, "w", graph)
    monitor = MonitorInfo(index=0, rect=[0, 0, *MONITOR_SIZE], primary=True, dpi=96, scale=1.0)
    monkeypatch.setattr(windows_mod, "display_context",
                        lambda: DisplayContext(primary_index=0, monitors=[monitor]))
    monkeypatch.setattr(windows_mod, "monitor_index_for", lambda _hwnd, _monitors: 0,
                        raising=False)
    if hasattr(windows_mod, "_monitor_index_for"):
        monkeypatch.setattr(windows_mod, "_monitor_index_for", lambda _hwnd, _monitors: 0)
    return graph


def test_default_enumeration_includes_active_modal_popup_in_z_order(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— 契约第 2 条：默认枚举收下 enabled 的 owned modal popup，
    普通 owned 窗仍过滤；返回顺序原样保留 EnumWindows 的 z-order。

    expected `[POPUP_HWND, OWNER_HWND]` 是手算结果：popup 在 owner 上方，plain owned 被过滤。
    """
    graph = _install_windows_graph(monkeypatch, _FakeWin32())

    rows = windows_mod.enumerate_windows([MonitorInfo(index=0, rect=[0, 0, 1000, 800])])

    assert [row.hwnd for row in rows] == [POPUP_HWND, OWNER_HWND]
    assert [row.zorder for row in rows] == [1, 2]
    assert PLAIN_OWNED_HWND not in {row.hwnd for row in rows}
    assert graph.enabled[OWNER_HWND] is False


def test_capture_of_disabled_owner_uses_monitor_composition_and_unions_popup_bounds(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """oracle: specified + derived —— 契约第 3 条。

    `UNION` 是手算的 owner/popup 扩展框并集；`(monitor_index=1, crop=(100,80,560,300))`
    是契约「先取合成显示器图，再裁并集」的直接读数。
    """
    graph = _install_capture_graph(monkeypatch, _FakeWin32())
    log = {"constructors": [], "crops": [], "saved": []}
    _install_capture_backend(monkeypatch, log)

    result = capture_mod.capture_window(OWNER_HWND, tmp_path, 1, _owner_info(), "png")

    assert log["constructors"] == [
        {"cursor_capture": True, "draw_border": False, "monitor_index": 1}
    ], "模态 owner 不能只截 HWND 表面"
    assert log["crops"] == [UNION], "crop 应覆盖 owner 与 popup 的并集"
    assert result.origin == (UNION[0], UNION[1])
    assert (result.width, result.height) == (UNION[2] - UNION[0], UNION[3] - UNION[1])
    assert result.layer == "wgc"
    assert result.window is not None
    assert graph.foreground == 0


def test_capture_keeps_single_window_behavior_when_owner_is_enabled(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """oracle: specified —— 契约第 5 条：owner 未禁用时保持原来的单窗口表面捕获。"""
    graph = _install_capture_graph(monkeypatch, _FakeWin32())
    graph.enabled[OWNER_HWND] = True
    log = {"constructors": [], "crops": [], "saved": []}
    _install_capture_backend(monkeypatch, log)

    result = capture_mod.capture_window(OWNER_HWND, tmp_path, 2, _owner_info(), "png")

    assert log["constructors"] == [
        {"cursor_capture": True, "draw_border": False, "window_hwnd": OWNER_HWND}
    ]
    assert log["crops"] == []
    assert result.origin == (OWNER_FRAME[0], OWNER_FRAME[1])
    assert (result.width, result.height) == (
        OWNER_FRAME[2] - OWNER_FRAME[0], OWNER_FRAME[3] - OWNER_FRAME[1]
    )


def test_capture_keeps_single_window_behavior_when_enabled_popup_is_owner_itself(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """oracle: specified —— 契约第 5 条第二支：`GW_ENABLEDPOPUP` 返回 owner 本身，
    owner 的禁用状态不足以触发并集捕获。"""
    graph = _install_capture_graph(monkeypatch, _FakeWin32())
    graph.enabled_popups[OWNER_HWND] = OWNER_HWND
    log = {"constructors": [], "crops": [], "saved": []}
    _install_capture_backend(monkeypatch, log)

    result = capture_mod.capture_window(OWNER_HWND, tmp_path, 3, _owner_info(), "png")

    assert log["constructors"] == [
        {"cursor_capture": True, "draw_border": False, "window_hwnd": OWNER_HWND}
    ]
    assert log["crops"] == []
    assert result.origin == (OWNER_FRAME[0], OWNER_FRAME[1])


def test_bring_to_foreground_routes_to_active_modal_popup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— 契约第 4 条：owner 被禁用且 popup 是 GW_ENABLEDPOPUP 时，
    不能把 owner 当目标；前台与焦点都要落到 popup。"""
    graph = _install_windows_graph(monkeypatch, _FakeWin32())

    windows_mod.bring_to_foreground(OWNER_HWND)

    assert graph.foreground_calls == [POPUP_HWND]
    assert graph.foreground == POPUP_HWND
    assert graph.focused == POPUP_HWND


def test_bring_to_foreground_keeps_owner_when_owner_is_enabled(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— 契约第 5 条：owner 未禁用时不沿模态关系重定向。"""
    graph = _install_windows_graph(monkeypatch, _FakeWin32())
    graph.enabled[OWNER_HWND] = True

    windows_mod.bring_to_foreground(OWNER_HWND)

    assert graph.foreground_calls == [OWNER_HWND]
    assert graph.foreground == OWNER_HWND
    assert graph.focused == OWNER_HWND


def test_bring_to_foreground_keeps_owner_when_enabled_popup_is_owner_itself(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— 契约第 5 条第二支：返回 owner 本身时不重定向到 Unowned 窗口。"""
    graph = _install_windows_graph(monkeypatch, _FakeWin32())
    graph.enabled_popups[OWNER_HWND] = OWNER_HWND

    windows_mod.bring_to_foreground(OWNER_HWND)

    assert graph.foreground_calls == [OWNER_HWND]
    assert graph.foreground == OWNER_HWND
    assert graph.focused == OWNER_HWND
