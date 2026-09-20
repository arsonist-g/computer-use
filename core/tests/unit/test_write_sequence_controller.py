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
from cu.errors import CUError, ErrorCode


class FakeOverlay:
    """替代 ControlOverlay：只实现控制器用到的状态面，不建窗口。"""

    def __init__(self, on_abort=None, on_error=None) -> None:
        # 签名与真实 `ControlOverlay` 对齐：控制器会把这两个回调透传进来。
        self.on_abort = on_abort
        self.on_error = on_error
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
    controller.begin_write(arm_ms=500, continue_seconds=0)
    # oracle: specified —— 前摇期间就封锁输入（overlay.md §2.2：不封锁等于没有前摇）。
    assert controller.blocker.blocking is True
    # oracle: specified —— 覆盖层直接进 Active。前摇是一个**计时**概念（`_armed_at`），
    # 不是一种要画出来的样子：它与 Active 的光谱、胶囊文案、色相完全一致，
    # 单设一个状态只会让「切换」凭空多出来一次（而每次切换都要重建胶囊与目标框）。
    # 前摇本身仍然存在 —— `armed()` 在 500ms 内为假，见下面那条断言。
    assert controller.overlay.state is OverlayState.ACTIVE
    assert controller.armed(arm_ms=500) is False


def test_keep_alive_holds_until_the_continue_window_expires(ctrl) -> None:
    controller, clock = ctrl
    # keep_alive=True（来自 `--continue`）：调用方说了还要继续操作，保持窗口由
    # `continue_seconds` 给（daemon 传的是 config.overlay_continue_seconds）。
    controller.begin_write(arm_ms=500, continue_seconds=30, keep_alive=True)
    controller.end_write()      # 这条写命令跑完了，保持窗口从这里开始算
    clock.advance(29.0)
    controller.tick(0)
    # oracle: specified —— DEC-045：窗口内覆盖层与封锁都保持。
    assert controller.blocker.blocking is True
    assert controller.overlay.visible is True
    clock.advance(2.0)          # 越过 30s 窗口
    controller.tick(0)
    # oracle: derived —— 窗口过期就走退出路径；exit_hold_ms=0 时立即解封。
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False


def test_write_without_keep_alive_ends_the_sequence(ctrl) -> None:
    """DEC-075：不带 `--continue` 的写命令结束序列 —— 不留兜底保持。"""
    controller, clock = ctrl
    # continue_seconds 给得很大也没用：没有 keep_alive 就不设保持窗口。
    controller.begin_write(arm_ms=500, continue_seconds=1000)
    controller.end_write()      # 「这条之后」= 这条**跑完**之后
    clock.advance(0.1)
    controller.tick(0)
    # oracle: specified —— 「没说继续」= 这条跑完就结束了，输入立刻还给人。
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False


def test_tick_does_not_retire_the_overlay_while_a_write_is_in_flight(ctrl) -> None:
    """写命令**执行期间**（前摇 + 派发）覆盖层与封锁都不退场。

    主循环每 0.2s 一次 `tick`，而前摇加上派发可能几秒；不带 `--continue` 时保持窗口
    一上来就是过期，于是 tick 会在**输入还没派发**的时候撤掉覆盖层、解封输入。
    用户看到的就是「覆盖层一闪而过，操作在没有保护的情况下继续」。
    """
    controller, clock = ctrl
    controller.begin_write(arm_ms=1500, continue_seconds=30)
    for _ in range(20):         # 前摇与派发期间，主循环转了 20 轮（共 4 秒）
        clock.advance(0.2)
        controller.tick(exit_hold_ms=500)
    # oracle: derived —— 这条命令还没跑完，保护期就没结束。
    assert controller.overlay.state is OverlayState.ACTIVE
    assert controller.blocker.blocking is True
    # 跑完才轮到保持窗口判定：不带 `--continue`，退场。
    controller.end_write()
    controller.tick(exit_hold_ms=0)
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False


def test_end_write_with_the_end_flag_retires_even_when_kept_alive(ctrl) -> None:
    """`--end` 在 end_write 上生效：这条就是最后一条，保持窗口作废。

    daemon 曾经在**武装之后**立刻结束序列，于是最后一条命令 —— 也是最需要保护的那条 ——
    在覆盖层已经撤下、输入已经解封之后才派发输入。
    """
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, continue_seconds=30, keep_alive=True)
    controller.end_write(end=True)
    clock.advance(0.1)
    controller.tick(0)
    # oracle: specified —— DEC-075：`--end` 之后不再保持。
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False


def test_end_sequence_forces_exit_path_without_immediate_release(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, continue_seconds=1000, keep_alive=True)
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
    controller.begin_write(arm_ms=500, continue_seconds=1000, keep_alive=True)
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
    controller.begin_write(arm_ms=500, continue_seconds=1000, keep_alive=True)
    controller.end_sequence()
    controller.tick(exit_hold_ms=500)     # 第一轮：撤覆盖层
    clock.advance(0.6)                    # 越过 0.5s 保留期
    controller.tick(exit_hold_ms=500)     # 第二轮：应解封
    # oracle: specified —— 过了保留期后 tick 才 set_blocking(False)（DEC-045）。
    assert controller.blocker.blocking is False


def test_exit_hold_zero_releases_on_first_tick(ctrl) -> None:
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, continue_seconds=1000, keep_alive=True)
    controller.end_sequence()
    controller.tick(exit_hold_ms=0)
    # oracle: specified —— exit_hold_ms=0 时一次 tick 就解封。
    assert controller.blocker.blocking is False
    assert controller.overlay.state is OverlayState.OFF
    # oracle: derived —— 解封后保留期状态被清空，写序列归零。
    assert controller._exit_release_at is None


def test_abort_releases_immediately_without_hold(ctrl) -> None:
    controller, _clock = ctrl
    controller.begin_write(arm_ms=500, continue_seconds=1000, keep_alive=True)
    assert controller.blocker.blocking is True
    controller.abort("用户按下物理 Esc")
    # oracle: specified —— 中止的第一件事就是解除封锁，不等退出保留期。
    assert controller.blocker.blocking is False
    assert controller.aborted is True
    # oracle: specified —— 中止**直接撤下覆盖层**（DEC-088：没有 Stopping 态）。
    assert controller.overlay.state is OverlayState.OFF

def test_abort_refuses_exactly_one_write_command_and_does_not_latch(ctrl) -> None:
    """DEC-068：中止是**一次性**闸门 —— 挡住下一条写命令，然后自己清掉。"""
    controller, _clock = ctrl
    controller.abort("用户按下物理 Esc")

    # oracle: specified —— 中止之后的第一条写命令不执行：它就是「告诉 AI 停下」的那次机会。
    with pytest.raises(CUError) as raised:
        controller.begin_write(arm_ms=500, continue_seconds=0)
    assert raised.value.code is ErrorCode.ABORTED_BY_USER
    # oracle: derived —— 被拒的命令不能把输入封回去（那正是用户按 Esc 想拿回的东西），
    # 闩也不能留着：留着就等于把写通路闩到 daemon 重启（DEC-068 的旧行为）。
    assert controller.blocker.blocking is False
    assert controller.overlay.state is OverlayState.OFF
    assert controller.aborted is False

    # oracle: specified —— 用户说了「可以继续」之后直接再调一次即可，不需要重启 daemon。
    controller.begin_write(arm_ms=500, continue_seconds=0)
    assert controller.overlay.state is OverlayState.ACTIVE
    assert controller.blocker.blocking is True


def test_abort_during_the_arming_pause_consumes_the_latch(ctrl) -> None:
    """Esc 落在这条命令的前摇期间：这条已经挨过了，别让**下一条**再白挨一次。"""
    controller, _clock = ctrl
    controller.begin_write(arm_ms=500, continue_seconds=0)
    controller.abort("用户按下物理 Esc")

    # oracle: specified —— 前摇期间的 Esc 让这条命令立刻失败。
    with pytest.raises(CUError) as raised:
        controller.wait_for_arm(arm_ms=500)
    assert raised.value.code is ErrorCode.ABORTED_BY_USER
    # oracle: derived —— 机会已经用在这一条上了，闩随之消费掉。
    assert controller.aborted is False

    controller.begin_write(arm_ms=500, continue_seconds=0)
    assert controller.overlay.state is OverlayState.ACTIVE


def test_physical_esc_outside_a_write_sequence_is_not_an_abort(ctrl) -> None:
    """空闲时按物理 Esc 不算中止（DEC-088）。

    钩子随 daemon 常驻，空闲时也挂在输入链路上。若把那时按的 Esc 记成中止，
    后果之一正是「下一条写命令被 DEC-068 的一次性闸门平白拒掉」——
    用户看到的是「AI 说我按过 Esc，可我没按」。
    """
    controller, _clock = ctrl
    controller.start()
    controller._handle_physical_esc()

    # oracle: derived —— 覆盖层不在场 ⟹ 没有可中止的东西。
    assert controller.aborted is False
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is False

    # oracle: specified —— 覆盖层在场时，它才是中止。
    controller.begin_write(arm_ms=500, continue_seconds=0)
    controller._handle_physical_esc()
    assert controller.aborted is True
    assert controller.overlay.state is OverlayState.OFF


def test_physical_esc_in_the_exit_hold_is_not_an_abort(ctrl) -> None:
    """退场保留期里按 Esc 也不算中止 —— 那时覆盖层已经撤下、输入还没放行。"""
    controller, clock = ctrl
    controller.begin_write(arm_ms=500, continue_seconds=1000, keep_alive=True)
    controller.end_sequence()
    controller.tick(exit_hold_ms=500)
    assert controller.overlay.state is OverlayState.OFF
    assert controller.blocker.blocking is True

    controller._handle_physical_esc()

    # oracle: derived —— 判据是「覆盖层在场」，不是「输入被封锁」。
    assert controller.aborted is False
    # oracle: specified —— 保留期本身不受影响：过了截止时刻照旧解封。
    clock.advance(0.6)
    controller.tick(exit_hold_ms=500)
    assert controller.blocker.blocking is False
