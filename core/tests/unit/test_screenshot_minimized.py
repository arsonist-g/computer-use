"""最小化窗口的截图：先把它从最小化里拉出来，再截。

契约出处：`core/src/cu/desktop/windows.py` 的 `restore_minimized`、
`core/src/cu/desktop/real.py` 的 `RealDesktop.capture`，以及
`core/src/cu/desktop/base.py` 的 `CaptureResult.window_restored`。

为什么要有这条契约：最小化的窗口**没有可截的合成面**。实测（本机「设置」窗口）
降级链两层全败 —— WGC 报「4.0s 内未交付可用帧」；DXGI 按窗口矩形去显示器上裁，
而最小化窗口的矩形在屏幕外（`GetWindowRect` 给 `-32000`），裁出来是空的，
保存时 `cv::imwrite` 抛 `!_img.empty()`。调用方看到的是 `capture_failed`，
而真正的原因（窗口没还原）在报错文本里一个字都没有。

**本文件一次都不碰真实桌面**：`restore_minimized` 的用例把模块级 `w` 换成替身，
`RealDesktop.capture` 的用例把 `display_context` / `enumerate_windows` /
`restore_minimized` / `capture_window` 全换成替身。真实的 `ShowWindow` 会把用户
正在用的窗口拉出来，属于事故，所以替身不留任何回退路径 —— 未声明的名字直接
AttributeError。

oracle 类别逐条标注：specified（契约原文或 Win32 外部规范）/ derived（由契约推出）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cu.config import Config
from cu.desktop import capture as capture_mod
from cu.desktop import real as real_mod
from cu.desktop import windows as windows_mod
from cu.desktop.base import CaptureResult, WindowInfo
from cu.errors import CUError, ErrorCode

# ---------------------------------------------------------------------------
# 手算的期望值：出处是 Win32 文档，与被测实现的取值无关
# ---------------------------------------------------------------------------

WIN_SW_SHOWNOACTIVATE = 4     # ShowWindow：显示但不激活 —— 本模块用的就是它
WIN_SW_RESTORE = 9            # ShowWindow：激活并显示 —— 会抢走前台，本模块**刻意不用**
WIN_NOT_MINIMIZED = 0         # IsIconic：0 = 不在最小化（普通窗口或最大化窗口）
WIN_MINIMIZED = 1             # IsIconic：非 0 = 已最小化
TARGET_HWND = 0x0001A2B4


# ---------------------------------------------------------------------------
# `restore_minimized` 的替身桌面
# ---------------------------------------------------------------------------


class _FakeUser32:
    def __init__(self, desktop: _FakeWindow) -> None:
        self._desktop = desktop

    def IsIconic(self, hwnd: int) -> int:
        return self._desktop.is_iconic(hwnd)

    def ShowWindow(self, hwnd: int, command: int) -> bool:
        self._desktop.show_window(hwnd, command)
        return True


class _FakeWindow:
    """`windows.py` 里 `w` 命名空间的替身，同时是一台**最小化状态机**。

    `restore_delay_polls` 模拟「还原发出去之后还要过几帧才真的解除」——
    真实系统里 `SW_SHOWNOACTIVATE` 到 `IsIconic` 变假不是同一个瞬间，等待循环要能扛住。
    `restore_works=False` 模拟还原发出去了但窗口始终不解除（挂死、或窗口在另一个
    虚拟桌面上），这条路径必须**报错而不是放行去截图**。
    """

    SW_SHOWNOACTIVATE = WIN_SW_SHOWNOACTIVATE
    #: 也声明 `SW_RESTORE`：它存在但**不该被用到**，用例靠「命令字面量是哪一个」来钉这件事。
    SW_RESTORE = WIN_SW_RESTORE

    def __init__(self, *, minimized: bool = True, restore_works: bool = True,
                 restore_delay_polls: int = 0) -> None:
        self.minimized = minimized
        self.restore_works = restore_works
        self.restore_delay_polls = restore_delay_polls
        self.restore_calls = 0
        self.polls_after_restore = 0
        #: 被问过「是否最小化」的窗口，按调用顺序 —— 用例靠它钉「等条件而不是睡死」。
        self.iconic_queries: list[int] = []
        self.events: list[tuple[object, ...]] = []
        self.user32 = _FakeUser32(self)

    def is_iconic(self, hwnd: int) -> int:
        self.iconic_queries.append(hwnd)
        if not self.minimized:
            return WIN_NOT_MINIMIZED
        if self.restore_calls == 0 or not self.restore_works:
            return WIN_MINIMIZED
        if self.polls_after_restore >= self.restore_delay_polls:
            return WIN_NOT_MINIMIZED
        self.polls_after_restore += 1
        return WIN_MINIMIZED

    def show_window(self, hwnd: int, command: int) -> None:
        self.restore_calls += 1
        self.events.append(("ShowWindow", hwnd, command))

    def window_text(self, hwnd: int) -> str:
        return "某个窗口"


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeWindow) -> _FakeWindow:
    monkeypatch.setattr(windows_mod, "w", fake)
    assert windows_mod.w is fake, "替身没装上：再往下走就会碰真实桌面"
    return fake


def _show_window_events(fake: _FakeWindow) -> list[tuple[object, ...]]:
    return [event for event in fake.events if event[0] == "ShowWindow"]


# ---------------------------------------------------------------------------
# 什么时候不动手
# ---------------------------------------------------------------------------


def test_a_visible_window_is_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 「只在 IsIconic 时动手」
    fake = _install(monkeypatch, _FakeWindow(minimized=False))
    assert windows_mod.restore_minimized(TARGET_HWND) is False
    assert fake.events == []


def test_a_maximized_window_is_not_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— Win32 里 IsIconic 对普通与最大化窗口同为 0。
    # 对一个最大化窗口发 SW_SHOWNOACTIVATE 会把它还原到**原始大小与位置**，
    # 等于每次截图都替用户改一次窗口布局（DEC-085 的边界）。
    fake = _install(monkeypatch, _FakeWindow(minimized=False))
    windows_mod.restore_minimized(TARGET_HWND)
    assert _show_window_events(fake) == []


# ---------------------------------------------------------------------------
# 动手的时候：还原一次、等到解除、解除不了就报错
# ---------------------------------------------------------------------------


def test_a_minimized_window_is_restored_without_taking_the_foreground(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 实测（本机记事本，2026-10-15）：`SW_RESTORE` 还原之后
    # `fg` 直接变成目标窗口；`SW_SHOWNOACTIVATE` 不动前台，而 WGC 在两种命令下都拿得到帧
    # （都是 1006x553）。`screenshot` 是读命令，抢走用户的前台会让此刻的按键落进那个窗口，
    # 所以这里必须发**不激活**的那一个。
    assert WIN_SW_SHOWNOACTIVATE != WIN_SW_RESTORE, "两个命令不能写成同一个数，否则这条用例失去意义"
    fake = _install(monkeypatch, _FakeWindow(minimized=True))
    assert windows_mod.restore_minimized(TARGET_HWND) is True
    assert fake.events == [("ShowWindow", TARGET_HWND, WIN_SW_SHOWNOACTIVATE)]


def test_it_returns_as_soon_as_the_window_is_no_longer_minimized(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: derived —— 等条件而不是睡固定时长：解除得早就早退。
    # 预算给 5s，实际解除在第 5 次查询，所以次数远小于预算允许的轮数。
    monkeypatch.setattr(windows_mod, "RESTORE_WAIT_MS", 5000)
    monkeypatch.setattr(windows_mod, "RESTORE_POLL_MS", 5)
    fake = _install(monkeypatch, _FakeWindow(minimized=True, restore_delay_polls=3))
    assert windows_mod.restore_minimized(TARGET_HWND) is True
    # 首次判定 1 次 + 还原后 4 次（3 次仍最小化，第 4 次解除）。
    assert len(fake.iconic_queries) == 5


def test_it_reports_window_minimized_when_the_restore_never_takes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 还等不到就报 window_minimized 而不是放行去截图：
    # 那种情况下降级链必然全败，而 capture_failed 指不到真正的原因。
    monkeypatch.setattr(windows_mod, "RESTORE_WAIT_MS", 40)
    monkeypatch.setattr(windows_mod, "RESTORE_POLL_MS", 1)
    fake = _install(monkeypatch, _FakeWindow(minimized=True, restore_works=False))
    with pytest.raises(CUError) as caught:
        windows_mod.restore_minimized(TARGET_HWND)
    assert caught.value.code is ErrorCode.WINDOW_MINIMIZED
    assert caught.value.detail["hwnd"] == "0x0001A2B4"
    # 报错前确实试过还原，而且只试了一次 —— 重试留给调用方。
    assert fake.restore_calls == 1


# ---------------------------------------------------------------------------
# `RealDesktop.capture` 接上它之后的行为
# ---------------------------------------------------------------------------


class _Display:
    """`display_context()` 的替身。`enumerate_windows` 也换成替身，所以内容无关紧要。"""

    monitors: list[object] = []


def _window_info(hwnd: int, *, minimized: bool, monitor: int) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, title="某个窗口", pid=4242, process="some.exe",
                      rect=(100, 200, 800, 600), monitor=monitor, is_foreground=False,
                      is_minimized=minimized, elevated=False)


def _wire_capture(monkeypatch: pytest.MonkeyPatch, infos: list[WindowInfo],
                  calls: list[str]) -> dict:
    """把 `capture` 会碰到的桌面调用全换成替身；`infos` 是逐次枚举的返回队列。"""
    queue = list(infos)
    seen: dict = {}

    monkeypatch.setattr(windows_mod, "display_context", lambda: _Display())
    monkeypatch.setattr(windows_mod, "enumerate_windows",
                        lambda _monitors, all_windows=False: [queue.pop(0)])
    monkeypatch.setattr(windows_mod, "restore_minimized",
                        lambda hwnd: calls.append(f"restore:0x{hwnd:08X}") or True)

    def fake_capture_window(hwnd, out_dir, seq, info, image_format, draw_cursor=False):
        seen["info"] = info
        return CaptureResult(path=str(Path(out_dir) / "shot.png"), origin=(1, 2),
                             width=3, height=4, layer="wgc", kind="window", window=info)

    monkeypatch.setattr(capture_mod, "capture_window", fake_capture_window)
    return seen


def _capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CaptureResult:
    return real_mod.RealDesktop(Config()).capture(
        hwnd=TARGET_HWND, monitor=None, image_format="png", out_dir=tmp_path, seq=1)


def test_capture_restores_a_minimized_window_then_reenumerates(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # oracle: specified —— 还原之后必须重新枚举：rect 与 monitor 都变了，
    # 而 DXGI 那条降级路径按 monitor 去显示器上裁。
    calls: list[str] = []
    before = _window_info(TARGET_HWND, minimized=True, monitor=0)
    after = _window_info(TARGET_HWND, minimized=False, monitor=1)
    seen = _wire_capture(monkeypatch, [before, after], calls)

    result = _capture(monkeypatch, tmp_path)

    assert calls == ["restore:0x0001A2B4"]
    assert seen["info"] is after, "还原之后交给 capture_window 的必须是新枚举出来的那一份"
    # oracle: specified —— 只读命令动了用户的窗口，必须出现在返回值里。
    assert result.window_restored is True
    assert result.to_dict()["window_restored"] is True


def test_capture_leaves_a_visible_window_alone(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # oracle: specified —— 非最小化时既不还原也不多枚举一次
    # （队列只放一份，多枚举一次就会 IndexError）。
    calls: list[str] = []
    only = _window_info(TARGET_HWND, minimized=False, monitor=0)
    seen = _wire_capture(monkeypatch, [only], calls)

    result = _capture(monkeypatch, tmp_path)

    assert calls == []
    assert seen["info"] is only
    assert result.window_restored is False
    assert "window_restored" not in result.to_dict()
