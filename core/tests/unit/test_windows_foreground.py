"""Windows 前台抢占的分级重试（`bring_to_foreground`）与其三个私有助手的契约。

契约出处（本文件唯一的行为权威）：`core/src/cu/desktop/windows.py` 里
`bring_to_foreground` / `_attach_to_the_foreground_thread` / `_detach_from` / `_send_alt`
的 docstring（DEC-013 第 2 层、DEC-081、DEC-030）。

**本文件一次都不碰真实桌面**：每个用例都把 `cu.desktop.windows` 的模块级名字 `w` 换成
`_FakeDesktop`，`w.user32` / `w.kernel32` / `w.foreground_hwnd()` / `w.INPUT` 全部由替身应答。
真实的 `SetForegroundWindow` / `AttachThreadInput` / `SendInput` / `ShowWindow` 会抢走用户
正在用的窗口焦点，属于事故，所以替身不留任何回退路径 —— 未声明的名字直接 AttributeError。

覆盖判据：`bring_to_foreground` 与 `_attach_to_the_foreground_thread` 的**分支（decision）
全覆盖** —— 三级重试梯子的每条转移各一条用例（state transition testing 的 0-switch），
外加 `_attach_to_the_foreground_thread` 三个提前返回的分支各一条。项目未配置覆盖率工具
（无 pytest-cov / coverage），所以这是声明值而非实测值。

oracle 类别逐条标注：specified（契约原文或 Win32 外部规范）/ derived（由契约推出）。
"""

from __future__ import annotations

import ctypes
from collections.abc import Sequence

import pytest

from cu.desktop import win32
from cu.desktop import windows as windows_mod
from cu.errors import CUError, ErrorCode

# ---------------------------------------------------------------------------
# 手算的期望值与替身常量：出处是 Win32 文档，与被测实现的取值无关
# ---------------------------------------------------------------------------

WIN_SW_RESTORE = 9            # ShowWindow 的还原命令
WIN_VK_MENU = 0x12            # ALT 的虚拟键码
WIN_INPUT_KEYBOARD = 1        # INPUT.type：键盘事件
WIN_KEYEVENTF_KEYUP = 0x0002  # KEYBDINPUT.dwFlags 的「抬起」位
WIN_ALT_DOWN = 0x0000         # 按住：dwFlags 里没有「抬起」位

TARGET_HWND = 0x0001A2B4      # 要被提到前台的目标窗口
OTHER_HWND = 0x0000C0DE       # 起初占着前台的别的窗口
FOREGROUND_THREAD = 0x1234    # OTHER_HWND 所属线程
TARGET_THREAD = 0x5678        # TARGET_HWND 所属线程（`_give_focus` 要挂的就是它）
MY_THREAD = 0x0ABC            # 本线程


def _step(item: bool | tuple[bool, bool]) -> tuple[bool, bool]:
    """剧本的一项 → `(返回值, 是否真的把前台移过去)`。

    真实系统里这两件事可以不一致：`SetForegroundWindow` 返回 True 却没动前台，正是
    DEC-013 记录过的失效模式，契约因此要求实现**验证**而不是相信返回值 —— 替身必须
    能把这两件事分开摆出来。
    """
    return item if isinstance(item, tuple) else (item, item)


class _FakeUser32:
    """`w.user32` 的替身：只应答与记录，不落到任何真实 DLL 上。"""

    def __init__(self, desktop: _FakeDesktop) -> None:
        self._desktop = desktop

    def ShowWindow(self, hwnd: int, command: int) -> bool:
        self._desktop.events.append(("ShowWindow", hwnd, command))
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        return self._desktop.set_foreground_window(hwnd)

    def AttachThreadInput(self, target: int, mine: int, attach: bool) -> int:
        return self._desktop.attach_thread_input(target, mine, attach)

    def SetFocus(self, hwnd: int) -> int:
        self._desktop.focused = hwnd
        self._desktop.events.append(("SetFocus", hwnd))
        return 0

    def GetWindowThreadProcessId(self, hwnd: int, out: object) -> int:
        self._desktop.thread_queries.append(hwnd)
        if hwnd == TARGET_HWND:
            return self._desktop.target_thread
        return self._desktop.foreground_thread

    def SendInput(self, count: int, items: object, size: int) -> int:
        return self._desktop.send_input(count, items, size)


class _FakeKernel32:
    """`w.kernel32` 的替身：只回答「本线程是谁」。"""

    def __init__(self, desktop: _FakeDesktop) -> None:
        self._desktop = desktop

    def GetCurrentThreadId(self) -> int:
        return self._desktop.current_thread


class _FakeDesktop:
    """整套 `w` 命名空间的替身 —— 同时是一台**前台状态机**。

    `script` 是 `SetForegroundWindow` 逐次调用的剧本（见 `_step`）。其余成员按契约取值：
    `foreground_hwnd()` 报当前前台，`kernel32.GetCurrentThreadId()` 报本线程，
    `user32.GetWindowThreadProcessId(前台窗口)` 报前台窗口所属线程。

    常量与结构体：`_send_alt` 会做 `ctypes.sizeof(w.INPUT)` 与 `ctypes.byref(item)`，所以
    替身必须给真实的 ctypes 类型。这里借的是 `win32` 那份**纯类型声明**（不含任何调用，
    `windows.py` 拿到的也是同一份类型）。
    """

    INPUT = win32.INPUT
    KEYBDINPUT = win32.KEYBDINPUT
    INPUT_KEYBOARD = WIN_INPUT_KEYBOARD
    VK_MENU = WIN_VK_MENU
    KEYEVENTF_KEYUP = WIN_KEYEVENTF_KEYUP
    SW_RESTORE = WIN_SW_RESTORE

    def __init__(
        self,
        *,
        script: Sequence[bool | tuple[bool, bool]] = (),
        foreground: int = OTHER_HWND,
        current_thread: int = MY_THREAD,
        foreground_thread: int = FOREGROUND_THREAD,
        target_thread: int = TARGET_THREAD,
        attach_ok: bool = True,
    ) -> None:
        self.script = [_step(item) for item in script]
        self.foreground = foreground
        self.current_thread = current_thread
        self.foreground_thread = foreground_thread
        self.target_thread = target_thread
        self.attach_ok = attach_ok
        #: `SetFocus` 拿到焦点的那个窗口（None = 从没设过）。
        self.focused: int | None = None
        #: 动作序列 —— 只记真的发生的事：ShowWindow / SetForegroundWindow /
        #: AttachThreadInput / SendInput。用例靠它钉顺序。
        self.events: list[tuple[object, ...]] = []
        self.set_foreground_calls = 0
        #: `AttachThreadInput` 的全部三元组，按调用顺序（含返回 0 的那次）。
        self.attach_thread_input_calls: list[tuple[int, int, bool]] = []
        #: `SendInput` 注入的 `(wVk, dwFlags)`，按注入顺序。
        self.injected: list[tuple[int, int]] = []
        #: 被问过「属于哪个线程」的窗口。
        self.thread_queries: list[int] = []
        self.user32 = _FakeUser32(self)
        self.kernel32 = _FakeKernel32(self)

    def foreground_hwnd(self) -> int:
        return self.foreground

    def set_foreground_window(self, hwnd: int) -> bool:
        index = self.set_foreground_calls
        self.set_foreground_calls += 1
        self.events.append(("SetForegroundWindow", hwnd))
        returned, moved = self.script[index] if index < len(self.script) else (False, False)
        if moved:
            self.foreground = hwnd
        return returned

    def attach_thread_input(self, target: int, mine: int, attach: bool) -> int:
        self.attach_thread_input_calls.append((target, mine, attach))
        self.events.append(("AttachThreadInput", target, mine, attach))
        if not attach:
            return 1
        return self.foreground_thread if self.attach_ok else 0

    def send_input(self, count: int, items: object, size: int) -> int:
        item = ctypes.cast(items, ctypes.POINTER(win32.INPUT)).contents
        self.injected.append((item.ki.wVk, item.ki.dwFlags))
        self.events.append(("SendInput", item.ki.wVk, item.ki.dwFlags))
        return count


def _install(monkeypatch: pytest.MonkeyPatch, desktop: _FakeDesktop) -> _FakeDesktop:
    """装上替身 —— 装上之后 `bring_to_foreground` 里不会有任何一个真实 Win32 调用。"""
    monkeypatch.setattr(windows_mod, "w", desktop)
    assert windows_mod.w is desktop, "替身没装上：再往下走就会碰真实桌面"
    return desktop


def _ladder_events(target: int) -> list[tuple[object, ...]]:
    """第 1 级失败之后走满第 2、3 级并解挂的完整动作序列（契约第 3 / 4 / 5 条）。"""
    return [
        ("ShowWindow", target, WIN_SW_RESTORE),
        ("SetForegroundWindow", target),
        ("AttachThreadInput", FOREGROUND_THREAD, MY_THREAD, True),
        ("SetForegroundWindow", target),
        ("SendInput", WIN_VK_MENU, WIN_ALT_DOWN),
        ("SetForegroundWindow", target),
        ("SendInput", WIN_VK_MENU, WIN_KEYEVENTF_KEYUP),
        ("AttachThreadInput", FOREGROUND_THREAD, MY_THREAD, False),
    ]


def _focus_events(target: int) -> list[tuple[object, ...]]:
    """抢到前台之后把焦点交过去的那三个动作（`_give_focus`）。

    `SetFocus` 只能跨线程用在**同一条输入队列**上，所以它两侧是成对的一次 attach / detach。
    """
    return [
        ("AttachThreadInput", TARGET_THREAD, MY_THREAD, True),
        ("SetFocus", target),
        ("AttachThreadInput", TARGET_THREAD, MY_THREAD, False),
    ]


def test_an_already_foreground_window_is_not_re_foregrounded_but_gets_the_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 1 条「前台已经是目标 → 不再抢」。

    derived —— 但**焦点仍然要交过去**：前台是「谁在最前」，焦点是「按键进谁」，
    两者在 WinUI 应用上会脱节（真机实测：抢完前台后焦点停在该应用的输入站点窗口上，
    随后那串 `ctrl+a` 一个字符都没选中）。
    """
    desktop = _install(monkeypatch, _FakeDesktop(foreground=TARGET_HWND, script=[True]))

    windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.events == _focus_events(TARGET_HWND), "不再重新抢前台，但要交出焦点"
    assert desktop.set_foreground_calls == 0
    assert desktop.injected == []
    assert desktop.focused == TARGET_HWND


def test_the_first_level_succeeds_without_attaching_or_injecting_alt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 2 条：`ShowWindow(hwnd, SW_RESTORE)` → 第 1 级
    `SetForegroundWindow`；命令字面量 9 取自 Win32 文档的 `SW_RESTORE`。
    derived —— 第 1 级已成，就不该再挂输入队列，也不该注入 ALT。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[True]))

    windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.events == [
        ("ShowWindow", TARGET_HWND, WIN_SW_RESTORE),
        ("SetForegroundWindow", TARGET_HWND),
    ] + _focus_events(TARGET_HWND)
    assert desktop.set_foreground_calls == 1
    # 第 1 级就抢到了：这次挂输入队列**只为**把焦点交过去，不是为了抢前台。
    assert desktop.attach_thread_input_calls == [
        (TARGET_THREAD, MY_THREAD, True),
        (TARGET_THREAD, MY_THREAD, False),
    ]
    assert desktop.injected == []
    assert desktop.focused == TARGET_HWND


def test_the_second_level_attaches_before_the_retry_and_detaches_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 3 / 5 条：第 2 级先 `AttachThreadInput` 挂到当前前台线程、
    再 `SetForegroundWindow`；`_detach_from(attached)` 在 `finally` 里（挂上就必须解挂）。
    derived —— attach 在前、detach 在后，且解挂是这条路径的最后一个动作。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[False, True]))

    windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.events == [
        ("ShowWindow", TARGET_HWND, WIN_SW_RESTORE),
        ("SetForegroundWindow", TARGET_HWND),
        ("AttachThreadInput", FOREGROUND_THREAD, MY_THREAD, True),
        ("SetForegroundWindow", TARGET_HWND),
        ("AttachThreadInput", FOREGROUND_THREAD, MY_THREAD, False),
    ] + _focus_events(TARGET_HWND)
    assert desktop.attach_thread_input_calls == [
        (FOREGROUND_THREAD, MY_THREAD, True),
        (FOREGROUND_THREAD, MY_THREAD, False),
        (TARGET_THREAD, MY_THREAD, True),
        (TARGET_THREAD, MY_THREAD, False),
    ]
    assert desktop.injected == [], "第 2 级就成功了，不该走到注入 ALT 的第 3 级"


def test_the_third_level_holds_alt_down_across_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— 契约第 4 条：`_send_alt(False)` 按住 ALT → 再 `SetForegroundWindow`
    → `finally: _send_alt(True)` 抬起。`VK_MENU` = 0x12 与 `KEYEVENTF_KEYUP` = 0x0002 取自
    Win32 虚拟键码表与 KEYBDINPUT 定义。
    derived —— 「按住 ALT 去抢」要求按下的注入落在这次重试之前、抬起落在它之后。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[False, False, True]))

    windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.injected == [
        (WIN_VK_MENU, WIN_ALT_DOWN),
        (WIN_VK_MENU, WIN_KEYEVENTF_KEYUP),
    ]
    assert desktop.events == _ladder_events(TARGET_HWND) + _focus_events(TARGET_HWND)
    alt_down = desktop.events.index(("SendInput", WIN_VK_MENU, WIN_ALT_DOWN))
    alt_up = desktop.events.index(("SendInput", WIN_VK_MENU, WIN_KEYEVENTF_KEYUP))
    retry = desktop.events.index(("SetForegroundWindow", TARGET_HWND), alt_down)
    assert alt_down < retry < alt_up, "ALT 必须按住越过这次重试：按下 → 重试 → 抬起"
    assert desktop.set_foreground_calls == 3


def test_all_three_levels_failing_raises_foreground_failed_with_the_hwnd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 6 条：三级都不成抛 `CUError(ErrorCode.FOREGROUND_FAILED)`，
    `detail` 里带 hwnd（docstring：绝不退化成「就当它在前台了」）。
    detail 的取值形态按 data-model §3.6（hwnd 一律 `0x%08X` 字符串）接受原值与字符串两种。
    """
    _install(monkeypatch, _FakeDesktop(script=[False, False, False]))

    with pytest.raises(CUError) as excinfo:
        windows_mod.bring_to_foreground(TARGET_HWND)

    assert excinfo.value.code is ErrorCode.FOREGROUND_FAILED
    assert "hwnd" in excinfo.value.detail
    assert excinfo.value.detail["hwnd"] in (TARGET_HWND, f"0x{TARGET_HWND:08X}")


def test_the_input_queue_is_detached_even_when_every_level_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 5 条：`_detach_from(attached)` 一定在 `finally` 里
    （docstring：「不解挂等于我们的输入队列和别人的永久共享」）。
    derived —— 三级全败这条路径同样必须解挂，且解挂是最后一个动作。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[False, False, False]))

    with pytest.raises(CUError):
        windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.attach_thread_input_calls == [
        (FOREGROUND_THREAD, MY_THREAD, True),
        (FOREGROUND_THREAD, MY_THREAD, False),
    ]
    assert desktop.events == _ladder_events(TARGET_HWND)


def test_the_attach_helper_returns_zero_when_there_is_no_foreground_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 7 条第一支：没有前台窗口 → 返回 0（等于没挂上），
    于是也就没有任何 `AttachThreadInput`。
    """
    desktop = _install(monkeypatch, _FakeDesktop(foreground=0))

    assert windows_mod._attach_to_the_foreground_thread() == 0
    assert desktop.attach_thread_input_calls == []
    assert desktop.thread_queries == [], "没有前台窗口，连它属于哪个线程都不必问"


def test_the_attach_helper_returns_zero_when_the_foreground_belongs_to_this_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 7 条第二支：前台线程就是本线程 → 返回 0，
    既不问线程归属之外的任何事，也不去挂自己（挂自己没有意义）。
    """
    desktop = _install(monkeypatch, _FakeDesktop(foreground=OTHER_HWND, foreground_thread=MY_THREAD))

    assert windows_mod._attach_to_the_foreground_thread() == 0
    assert desktop.thread_queries == [OTHER_HWND], "先问过前台窗口属于哪个线程"
    assert desktop.attach_thread_input_calls == []


def test_the_attach_helper_returns_zero_when_attach_thread_input_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oracle: specified —— 契约第 7 条第三支：`AttachThreadInput` 返回 0 → 返回 0
    （试过但没挂上，因此调用方也不该解挂）。
    """
    desktop = _install(monkeypatch, _FakeDesktop(attach_ok=False))

    assert windows_mod._attach_to_the_foreground_thread() == 0
    assert desktop.attach_thread_input_calls == [(FOREGROUND_THREAD, MY_THREAD, True)]


def test_a_failed_attach_is_not_followed_by_a_detach(monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: derived —— `_detach_from` 只对「真的挂上的线程 id」解挂（0 = 没挂上）：
    第 2 级 attach 失败、第 3 级成功这条路径上不该出现任何解挂调用。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[False, False, True], attach_ok=False))

    windows_mod.bring_to_foreground(TARGET_HWND)

    # 两次都是「试过但没挂上」：第 2 级抢前台那次，以及 `_give_focus` 那次。
    assert desktop.attach_thread_input_calls == [
        (FOREGROUND_THREAD, MY_THREAD, True),
        (TARGET_THREAD, MY_THREAD, True),
    ]
    assert desktop.focused is None, "没挂上输入队列就不该设焦点"
    assert desktop.injected == [
        (WIN_VK_MENU, WIN_ALT_DOWN),
        (WIN_VK_MENU, WIN_KEYEVENTF_KEYUP),
    ]


def test_the_first_level_does_not_trust_the_return_value_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— docstring：「把目标窗口提到前台并**确认成功**」「不确认的话，
    点击会落到当时真正的前台窗口上」（DEC-013 记录的第 1 个失效模式）。
    derived —— `SetForegroundWindow` 返回 True 但前台并没有移过去时，必须继续升级。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[(True, False), (True, True)]))

    windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.set_foreground_calls == 2, "只报了成功、前台没动，必须接着升级"
    assert desktop.attach_thread_input_calls == [
        (FOREGROUND_THREAD, MY_THREAD, True),
        (FOREGROUND_THREAD, MY_THREAD, False),
        (TARGET_THREAD, MY_THREAD, True),
        (TARGET_THREAD, MY_THREAD, False),
    ]


def test_the_first_level_needs_the_call_to_succeed_as_well(monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle: specified —— 契约第 2 条把第 1 级的成功写成两个条件同时成立：
    `SetForegroundWindow` 成功 **且** `foreground_hwnd()` 等于目标。
    derived —— 只满足后半（前台碰巧已经走到目标、调用自身却报了失败）不能在第 1 级收工。
    """
    desktop = _install(monkeypatch, _FakeDesktop(script=[(False, True), (True, True)]))

    windows_mod.bring_to_foreground(TARGET_HWND)

    assert desktop.set_foreground_calls == 2
    # 这条路径上第 1 级已经把前台挪到目标了，所以第 2 级问「前台属于哪个线程」
    # 问到的是**目标自己的**线程 —— 挂它、解挂它，随后 `_give_focus` 再挂一次。
    assert desktop.attach_thread_input_calls == [
        (TARGET_THREAD, MY_THREAD, True),
        (TARGET_THREAD, MY_THREAD, False),
        (TARGET_THREAD, MY_THREAD, True),
        (TARGET_THREAD, MY_THREAD, False),
    ]
