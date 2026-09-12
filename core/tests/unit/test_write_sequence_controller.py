"""写序列控制器时序契约（DEC-045）：前摇、keep-alive、退出保留期、中止。

被测类 ``WriteSequenceController`` 会构造 ``ControlOverlay`` 与 ``InputBlocker``，
两者都碰真实 Win32（建分层窗口、装低级键鼠钩子）。单测不得在 CI 里建窗口或装钩子，
所以这里把这两个依赖整体替换为可观测的 fake，并用一个可推进的假时钟替换模块内的
``time`` —— 时序断言因此完全确定，不依赖真实耗时、不受机器快慢影响。

oracle 标注：
- ``specified`` —— 来自 ``controller.py`` 的 docstring / DEC-045。
- ``derived`` —— 由状态机与保留期语义推导出的边界。
"""

from __future__ import annotations

import types

import pytest

import cu.desktop.controller as controller_mod
from cu.desktop.controller import WriteSequenceController
from cu.desktop.overlay import OverlayState


class FakeOverlay:
    """替代 ControlOverlay：只实现控制器用到的状态面，不建窗口。"""

    def __init__(self) -> None:
        self._state = OverlayState.OFF
        self.transitions: list[str] = []

    @property
    def state(self) -> OverlayState:
        return self._state

    @property
    def visible(self) -> bool:
        return self._state is not OverlayState.OFF

    def transition(self, state: OverlayState) -> None:
        self._state = state
        self.transitions.append(str(state))

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_target(self, rect) -> None:  # noqa: ANN001 - 仅占位
        pass

    def set_cursor(self, point) -> None:  # noqa: ANN001 - 仅占位
        pass


class FakeBlocker:
    """替代 InputBlocker：记录每次封锁切换，不装钩子。"""

    def __init__(self, on_abort=None) -> None:
        self.on_abort = on_abort
        self._blocking = False
        self.calls: list[bool] = []

    @property
    def blocking(self) -> bool:
        return self._blocking

    def set_blocking(self, blocking: bool) -> None:
        self._blocking = blocking
        self.calls.append(blocking)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def ctrl(monkeypatch):
    """构造依赖全部被替换的控制器，连同假时钟一起返回。"""
    monkeypatch.setattr(controller_mod, "ControlOverlay", FakeOverlay)
    monkeypatch.setattr(controller_mod, "InputBlocker", FakeBlocker)
    clock = FakeClock()
    monkeypatch.setattr(controller_mod, "time",
                        types.SimpleNamespace(monotonic=clock.monotonic))
    controller = WriteSequenceController()
    return controller, clock


def test_begin_write_arms_and_blocks_from_first_frame(ctrl) -> None:
    controller, _clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=5)
    # oracle: specified —— 前摇期间就封锁输入（overlay.md §2.2：不封锁等于没有前摇）。
    assert controller.blocker.blocking is True
    # oracle: specified —— 进入 Arming 态。
    assert controller.overlay.state is OverlayState.ARMING


def test_keep_alive_ignores_hold_threshold(ctrl) -> None:
    controller, clock = ctrl
    # hold_seconds 只有 1s，但 keep_alive=True（来自 --continue）表示调用方还要继续操作。
    controller.begin_write(arm_ms=500, hold_seconds=1, keep_alive=True)
    clock.advance(2.0)          # 已超过 hold_seconds，但仍远小于 keep-alive 窗口
    controller.tick(0)
    # oracle: specified —— DEC-045：keep_alive=True 时不看阈值，覆盖层与封锁都保持。
    assert controller.blocker.blocking is True
    # oracle: derived —— 覆盖层未被撤下（仍在保持窗口内）。
    assert controller.overlay.visible is True


def test_expired_hold_without_keep_alive_exits(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=1)
    clock.advance(2.0)
    controller.tick(0)
    # oracle: specified —— 保持窗口过期且无 keep-alive ⇒ 走退出路径；exit_hold_ms=0 立即解封。
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False


def test_end_sequence_forces_exit_path_without_immediate_release(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=1000, keep_alive=True)
    controller.end_sequence()
    # oracle: specified —— end_sequence 只把保持窗口置为过期，不在这里立刻解封。
    assert controller.blocker.blocking is True
    clock.advance(0.1)
    controller.tick(0)
    # oracle: specified —— 下一轮 tick 走退出路径；exit_hold_ms=0 时立即解封。
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False


def test_exit_hold_first_tick_removes_overlay_but_keeps_blocking(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=1000, keep_alive=True)
    controller.end_sequence()
    controller.tick(exit_hold_ms=500)
    # oracle: specified —— 退出保留期：先撤覆盖层。
    assert controller.overlay.state is OverlayState.OFF
    # oracle: specified —— 保留期内输入仍被封锁（这正是保留期存在的意义）。
    assert controller.blocker.blocking is True
    # oracle: derived —— 保留期截止时刻被记下，供后续 tick 判断何时解封。
    assert controller._exit_release_at is not None


def test_exit_hold_releases_after_deadline(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=1000, keep_alive=True)
    controller.end_sequence()
    controller.tick(exit_hold_ms=500)     # 第一轮：撤覆盖层
    clock.advance(0.6)                    # 越过 0.5s 保留期
    controller.tick(exit_hold_ms=500)     # 第二轮：应解封
    # oracle: specified —— 过了保留期后 tick 才 set_blocking(False)（DEC-045）。
    assert controller.blocker.blocking is False


def test_exit_hold_zero_releases_on_first_tick(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=1000, keep_alive=True)
    controller.end_sequence()
    controller.tick(exit_hold_ms=0)
    # oracle: specified —— exit_hold_ms=0 时一次 tick 就解封。
    assert controller.blocker.blocking is False
    assert controller.overlay.state is OverlayState.OFF
    # oracle: derived —— 解封后保留期状态被清空，写序列归零。
    assert controller._exit_release_at is None


def test_abort_releases_immediately_without_hold(ctrl) -> None:
    controller, _clock = ctrl
    controller.begin_write(arm_ms=500, hold_seconds=1000, keep_alive=True)
    assert controller.blocker.blocking is True
    controller.abort("用户按下物理 Esc")
    # oracle: specified —— 中止的第一件事就是解除封锁，不等退出保留期。
    assert controller.blocker.blocking is False
    assert controller.aborted is True
    # oracle: specified —— 中止进入 Stopping 态。
    assert controller.overlay.state is OverlayState.STOPPING
