"""低级键盘钩子对物理 Esc 的处置（DEC-088）。

为什么在单测里直接调钩子过程：钩子的**载体**（`SetWindowsHookEx` + 消息泵）装不得
—— 它会坐在全系统的输入链路上，装一次就影响每一次按键。而「收到一个 Esc 事件时
决定做什么」是纯判断，手工构造一条 `KBDLLHOOKSTRUCT` 就能覆盖，不必真按键。

oracle 标注：
- ``specified`` —— `hooks.py` 模块文档里的安全底线（先解封再回调、注入事件放行）。
- ``derived`` —— 由「只有封锁期间才有可中止的东西」推导出的边界。
"""

from __future__ import annotations

import ctypes

from cu.desktop import win32 as w
from cu.desktop.hooks import InputBlocker

#: 钩子过程只在 `code >= 0` 时处理事件。HC_ACTION = 0 就是「事件已到达钩子」。
HC_ACTION = 0


def esc_event(*, injected: bool = False) -> w.KBDLLHOOKSTRUCT:
    """一条 Esc 的原始事件。`flags` 决定它看起来是物理的还是注入的。"""
    return w.KBDLLHOOKSTRUCT(
        vkCode=w.VK_ESCAPE,
        scanCode=0,
        flags=w.LLKHF_INJECTED if injected else 0,
        time=0,
        dwExtraInfo=None,
    )


def wparam_for(*, down: bool) -> int:
    return w.WM_KEYDOWN if down else w.WM_KEYUP


def send(blocker: InputBlocker, event: w.KBDLLHOOKSTRUCT, *, down: bool = True) -> int:
    """把事件喂给钩子过程，返回它给的 `LRESULT`：`1` = 吞掉，其余 = 放行。"""
    return blocker._keyboard_hook(HC_ACTION, wparam_for(down=down), ctypes.addressof(event))


def passthrough(event: w.KBDLLHOOKSTRUCT, *, down: bool = True) -> int:
    """对照组：不吞事件时应有的返回值。直连 `CallNextHookEx`，与钩子里的调用同参。"""
    return w.user32.CallNextHookEx(None, HC_ACTION, wparam_for(down=down), ctypes.addressof(event))


def test_esc_while_not_blocking_is_left_alone() -> None:
    """空闲时钩子**不碰**这枚键 —— 这正是 DEC-088 修掉的那条。

    钩子随 daemon 常驻。空闲时的 Esc 若被当成中止，会有三个后果：覆盖层闪一下
    （已删掉的 Stopping 态）、闩住下一条写命令、这一下按键到不了用户当时的程序。
    """
    aborts: list[bool] = []
    blocker = InputBlocker(on_abort=lambda: aborts.append(True))
    event = esc_event()

    # oracle: specified —— 未封锁时事件原样交给下一个钩子。
    assert send(blocker, event) == passthrough(event)
    assert aborts == []


def test_esc_while_blocking_is_swallowed_and_aborts() -> None:
    aborts: list[bool] = []
    blocker = InputBlocker(on_abort=lambda: aborts.append(True))
    blocker.set_blocking(True)

    # oracle: specified —— 封锁期间的物理 Esc 被吞掉，不落到 AI 正在操作的窗口上。
    assert send(blocker, esc_event()) == 1
    assert aborts == [True]
    # oracle: specified —— 安全底线：先解封再通知，解封不依赖回调。
    assert blocker.blocking is False


def test_esc_releases_input_even_when_the_callback_raises() -> None:
    def boom() -> None:
        raise RuntimeError("回调炸了")

    blocker = InputBlocker(on_abort=boom)
    blocker.set_blocking(True)

    # oracle: specified —— 「输入被永久封锁」在机制上不成立。
    assert send(blocker, esc_event()) == 1
    assert blocker.blocking is False


def test_injected_esc_is_passed_through_while_blocking() -> None:
    """AI 自己的 SendInput Esc 不能被自己吞掉（否则 `key esc` 静默失效）。"""
    aborts: list[bool] = []
    blocker = InputBlocker(on_abort=lambda: aborts.append(True))
    blocker.set_blocking(True)
    event = esc_event(injected=True)

    # oracle: specified —— 注入事件一律放行，且不触发中止。
    assert send(blocker, event) == passthrough(event)
    assert aborts == []
    assert blocker.blocking is True


def test_idle_esc_does_not_use_up_the_next_abort() -> None:
    """空闲时按过的 Esc 不能把「一次机会」提前吃掉：封锁里的下一枚仍要中止。"""
    aborts: list[bool] = []
    blocker = InputBlocker(on_abort=lambda: aborts.append(True))
    send(blocker, esc_event())              # 空闲：什么都不该发生
    assert aborts == []
    blocker.set_blocking(True)

    # oracle: derived —— `_abort_latched` 是「同一次按键只触发一次」的去重；
    # 空闲那次不该把它置位，否则封锁开始后的第一枚 Esc 会被静默吃掉。
    assert send(blocker, esc_event()) == 1
    assert aborts == [True]
