"""写序列控制器 —— 把锁、覆盖层、输入封锁绑成一个整体（DEC-030 / DEC-032）。

架构 §1.5 第 2 条是这里的全部理由：**写锁、覆盖层、输入封锁三者同属 daemon，
「释放锁」与「解除封锁」必须是一次本地操作**，不经过任何 IPC。
只要它们分处两个进程，「锁已释放但输入还被封锁」这个窗口就存在，
而那个窗口的后果是把用户锁在电脑外。

状态机（overlay.md §2）：

    Off → Arming（1.5s 前摇，**从第一帧起就封锁输入**）→ Active → Stopping/Error → Off

关键取舍：**前摇的粒度是「每次写序列一次」，不是每条写命令一次**（DEC-030）。
否则一次 20 步的任务要多等 30~40 秒。连续写操作之间若间隔小于保持阈值，
覆盖层与封锁都保持 —— 这正是写操作之间那次 LLM 思考不该被切断的原因。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..errors import CUError, ErrorCode
from .hooks import InputBlocker
from .overlay import ControlOverlay, OverlayState


def _aborted_error(message: str) -> CUError:
    """「用户已中止」的统一错误。

    它不是故障，是用户意图 —— 调用方**不应**自动重来（DEC-028：
    尊重中止，先与用户确认）。
    """
    return CUError(ErrorCode.ABORTED_BY_USER, message)


#: `--continue` 的保持时长。取得足够长，让「调用方说还要继续」在它下一条命令
#: 到达之前不会被阈值提前掐断；又不会长到调用方其实已经结束时还扣着用户的键鼠 ——
#: 真结束时会显式发 `--end`，这一条只是「调用方崩了」的兜底。
_KEEP_ALIVE_SECONDS = 120.0


class WriteSequenceController:
    """一次「写序列」的视觉与封锁生命周期。

    它**不持有写锁** —— 锁的持有者是会话（DEC-004），跨多条写命令保持。
    这里只管「覆盖层现在该显示什么、输入现在该不该封锁」。
    """

    def __init__(self, *, on_abort: Callable[[], None] | None = None,
                 on_state_change: Callable[[str], None] | None = None) -> None:
        self.on_abort = on_abort
        self.on_state_change = on_state_change
        self.overlay = ControlOverlay()
        self.blocker = InputBlocker(on_abort=self._handle_physical_esc)
        self._armed_at: float | None = None
        self._hold_until = 0.0
        #: 退出保留期的截止时刻。见 `tick()` 的说明。
        self._exit_release_at: float | None = None
        self._lock = threading.Lock()
        self._aborted = threading.Event()
        self._started = False

    # ---- 生命周期 ----

    def start(self) -> None:
        """安装钩子并启动覆盖层渲染线程。**输入此前不被封锁**（OFF 态）。"""
        if self._started:
            return
        self.blocker.start()          # 装不上就抛错 —— 不静默降级成「没有封锁」
        self.overlay.start()
        self._started = True

    def shutdown(self) -> None:
        """解除封锁 → 关覆盖层 → 卸钩子。顺序不能反。"""
        if not self._started:
            return
        self.blocker.set_blocking(False)
        self.overlay.transition(OverlayState.OFF)
        self.overlay.stop()
        self.blocker.stop()
        self._started = False

    # ---- 写序列 ----

    def begin_write(self, arm_ms: int, hold_seconds: float, *,
                    keep_alive: bool = False) -> None:
        """一条写命令开始。首次（或上一条没说要继续）才走前摇。

        **前摇期间就封锁输入**（overlay.md §2.2）：前摇的全部意义是给用户反应时间，
        此时不封锁等于没有前摇。

        `keep_alive=True`（来自 `--continue`）表示调用方明确要接着操作：
        覆盖层与封锁都保持，不再重新武装。这是 DEC-045 的核心 ——
        阈值是个**猜**（下一条命令什么时候来，事先不知道），而调用方知道，
        所以把这个判断交给它。
        """
        with self._lock:
            now = time.monotonic()
            if self._aborted.is_set():
                # 用户已经按过 Esc —— 在用户重新明确继续之前不再自动重新武装。
                # 否则中止之后下一条命令立刻又把输入封回去，用户会觉得按 Esc 没用。
                raise _aborted_error("上一轮操作已被用户中止；请与用户确认后再继续")
            # 显式续期优先：调用方说了要连续操作，就不看阈值。
            self._hold_until = now + (hold_seconds if not keep_alive else _KEEP_ALIVE_SECONDS)
            if self.overlay.visible:
                return                # 仍在保持窗口内：不重新武装，输入保持封锁
            self._armed_at = now
            self.overlay.transition(OverlayState.ARMING)
            # 从 Arming 的第一帧起封锁。
            self.blocker.set_blocking(True)

    def finish_arming(self) -> None:
        """前摇时间到 —— 进入 Active（如果还在武装中）。"""
        with self._lock:
            if self.overlay.state is OverlayState.ARMING:
                self.overlay.transition(OverlayState.ACTIVE)

    def armed(self, arm_ms: int) -> bool:
        """前摇是否已经走完。写命令在前摇结束前不应该真的派发输入。"""
        if self._armed_at is None:
            return True
        return (time.monotonic() - self._armed_at) * 1000.0 >= arm_ms

    def wait_for_arm(self, arm_ms: int) -> None:
        """等到前摇结束。用户在此期间按 Esc 会立刻抛错中断等待。"""
        deadline = time.monotonic() + arm_ms / 1000.0
        while time.monotonic() < deadline:
            if self._aborted.is_set():
                raise _aborted_error("用户在前摇期间按下了 Esc")
            time.sleep(0.02)
        self.finish_arming()

    def note_error(self) -> None:
        """写操作失败 —— 光谱冻结为红，等用户按 Esc 关闭（DEC-030）。"""
        with self._lock:
            self.overlay.transition(OverlayState.ERROR)
            # Error 态**不封锁输入**：AI 已不在操作任何元素，没有理由扣着用户的键鼠。
            self.blocker.set_blocking(False)

    def abort(self, reason: str = "") -> None:
        """中止当前写序列。Stopping → Off（≤300ms）。"""
        with self._lock:
            self._aborted.set()
            self.overlay.transition(OverlayState.STOPPING)
            # 中止的第一件事就是解除封锁 —— 用户按 Esc 就是为了拿回控制权。
            self.blocker.set_blocking(False)
        self.notify()

    def resume(self) -> None:
        """用户确认后允许新的写序列（清掉中止状态）。"""
        with self._lock:
            self._aborted.clear()
            self.overlay.transition(OverlayState.OFF)
            self.blocker.set_blocking(False)

    def end_sequence(self) -> None:
        """调用方显式结束写序列（`--end`）。

        立刻把保持窗口置为已过期，让 `tick()` 在下一轮走正常的退出路径 ——
        退出保留期（DEC-045）仍然生效，不在这里立刻解封。
        """
        with self._lock:
            self._hold_until = 0.0

    @property
    def aborted(self) -> bool:
        return self._aborted.is_set()

    def tick(self, exit_hold_ms: int = 0) -> None:
        """空闲时的推进：保持窗口过期就退场。

        退出**不再立刻解封**（DEC-045）：先进入一个短暂保留期（默认 0.5s），
        期间覆盖层已经不在，但输入**仍被封锁**，然后才真正放行。

        为什么加这一段：解除封锁的瞬间，如果用户正好在按物理键或移动鼠标，
        那一下会落在他没预期的地方。概率极低，但代价是「工具在我手里乱动」，
        不值得赌。0.5s 的等待，人感觉不出来，却把那个窗口关掉了。

        **顺序在这里很要紧**：保留期的判定必须在「覆盖层是否可见」**之前**。
        退场的第一件事就是把覆盖层转 OFF，于是 `visible` 变成假 —— 若先判 `visible`
        就直接返回，解封那一步**永远不可达**，输入会被一直封着。
        （这个次序错误被 `test_exit_hold_releases_after_deadline` 钉住过。）
        """
        with self._lock:
            if self._aborted.is_set():
                self.overlay.transition(OverlayState.OFF)
                self.blocker.set_blocking(False)
                self._hold_until = 0.0
                self._exit_release_at = None
                return

            # ① 已在保留期内：只看时间，不看可见性（见 docstring 的次序说明）。
            if self._exit_release_at is not None:
                if time.monotonic() >= self._exit_release_at:
                    self._release_after_exit()
                return

            if not self.overlay.visible or self.overlay.state is OverlayState.ERROR:
                return

            # ② 保持窗口未过：什么都不做。
            if time.monotonic() < self._hold_until:
                return

            # ③ 保持窗口已过：撤覆盖层，进入保留期。
            self.overlay.transition(OverlayState.OFF)
            self._exit_release_at = time.monotonic() + exit_hold_ms / 1000.0
            if exit_hold_ms <= 0:
                self._release_after_exit()

    def _release_after_exit(self) -> None:
        """保留期结束：真正解除输入封锁，写序列归零。"""
        self.blocker.set_blocking(False)
        self._armed_at = None
        self._exit_release_at = None

    def _handle_physical_esc(self) -> None:
        """物理 Esc（低级钩子识别，注入的 Esc 不会走到这里）。"""
        self.abort("用户按下物理 Esc")
        if self.on_abort is not None:
            self.on_abort()

    def notify(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change(str(self.overlay.state))

    @property
    def state(self) -> str:
        return str(self.overlay.state)
