"""低级键鼠钩子 —— 输入封锁与物理 Esc 中止（DEC-028 / DEC-029）。

**安全底线**（架构 §1.5 第 5 条，不可违反）：

  - 钩子装在 **daemon 自己的消息泵线程**上，不装进任何长寿的旁路进程。
    daemon 死亡 ⟹ OS 自动摘除钩子 ⟹ 用户输入立即恢复。
    「输入被永久封锁」在机制上不成立，**靠的就是这条**。
  - 只吞**物理**事件（`LLKHF_INJECTED` / `LLMHF_INJECTED` 未置位），
    放行注入事件 —— 否则 AI 自己的 SendInput 会被自己吞掉。spike Q3b 已实测标志可靠。
  - 物理 Esc 永远可用，且被吞掉后触发中止。
  - 钩子处理函数必须极快返回：超时会被 Windows 静默摘除，输入恢复但**封锁静默失效**。
    因此这里只做标志判断与回调投递，不做任何阻塞或耗时计算。

系统保底：**Ctrl+Alt+Del 无法被任何用户态钩子拦截**，永远可用。这是安全底线，不是缺陷。

spike 陷阱 5：钩子回调是 `WINFUNCTYPE` 对象，被 GC 回收后系统会调用野指针 ——
因此回调**必须作为实例属性持有引用**。
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections.abc import Callable

from ..errors import CUError, ErrorCode
from . import win32 as w

#: 消息泵只在钩子活动期间运行。空闲时让出 CPU，同时保证消息延迟在人可感知阈值以下。
_MESSAGE_PUMP_IDLE_MS = 10
#: 钩子被系统摘除的判定阈值。Windows 对低级钩子的超时约 300ms（LowLevelHooksTimeout），
#: 超过这个数还没收到任何事件不足以判定，但连续失败会被记录。
_HOOK_TIMEOUT_MS = 1000


class HookInstallError(CUError):
    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, f"低级钩子安装失败：{detail}")
        CUError.__post_init__(self)


class InputBlocker:
    """在**当前线程**上安装键盘 / 鼠标低级钩子，并跑消息泵。

    用法：在一个专用线程里 `run()`；调用 `stop()` 让消息泵退出；
    `set_blocking(True/False)` 切换「吞物理 / 全放行」。

    **必须与 daemon 同生命周期** —— 不要把这个类放进独立的常驻进程。
    """

    def __init__(self, on_abort: Callable[[], None] | None = None,
                 on_input_seen: Callable[[bool], None] | None = None) -> None:
        self.on_abort = on_abort
        #: 回调参数是 `is_physical`。用于诊断与「用户是否在试图动电脑」的判断。
        self.on_input_seen = on_input_seen

        self._thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._blocking = threading.Event()
        self._ready = threading.Event()
        self._failure: str | None = None

        # spike 陷阱 5：回调对象必须持有引用，否则被 GC 回收后系统调用野指针。
        self._keyboard_proc = w.HOOKPROC(self._keyboard_hook)
        self._mouse_proc = w.HOOKPROC(self._mouse_hook)
        self._keyboard_handle: int | None = None
        self._mouse_handle: int | None = None
        #: 物理 Esc 是否在封锁中触发过中止。用于避免同一次按键反复触发。
        self._abort_latched = False

    # ---- 生命周期 ----

    def start(self, timeout: float = 5.0) -> None:
        """在专用线程上安装钩子并开始跑消息泵。安装失败会抛出。"""
        if self._thread is not None:
            return
        self._ready.clear()
        self._failure = None
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="cu-input-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise HookInstallError(f"消息泵线程在 {timeout}s 内未就绪")
        if self._failure:
            raise HookInstallError(self._failure)

    def stop(self, timeout: float = 3.0) -> None:
        """卸载钩子并结束消息泵。返回后输入**一定**已经放行。"""
        self._running.clear()
        if self._thread_id is not None:
            # 用 PostThreadMessage 让 GetMessage 立刻返回 —— 比轮询快，也比
            # 等空闲超时可靠。
            w.user32.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._uninstall()
        self._thread_id = None

    def set_blocking(self, blocking: bool) -> None:
        """切换封锁。封锁期间吞物理事件；不封锁时全放行。"""
        if blocking:
            self._blocking.set()
        else:
            self._blocking.clear()
            self._abort_latched = False

    @property
    def blocking(self) -> bool:
        return self._blocking.is_set()

    @property
    def installed(self) -> bool:
        return self._keyboard_handle is not None and self._mouse_handle is not None

    # ---- 消息泵线程 ----

    def _run(self) -> None:
        self._thread_id = w.kernel32.GetCurrentThreadId()
        try:
            self._install()
        except HookInstallError as exc:
            self._failure = str(exc)
            self._ready.set()
            return
        self._ready.set()
        self._pump()

    def _install(self) -> None:
        module = w.kernel32.GetModuleHandleW(None)
        self._keyboard_handle = w.user32.SetWindowsHookExW(
            w.WH_KEYBOARD_LL, self._keyboard_proc, module, 0)
        if not self._keyboard_handle:
            raise HookInstallError(f"WH_KEYBOARD_LL err={w.kernel32.GetLastError()}")
        self._mouse_handle = w.user32.SetWindowsHookExW(
            w.WH_MOUSE_LL, self._mouse_proc, module, 0)
        if not self._mouse_handle:
            w.user32.UnhookWindowsHookEx(self._keyboard_handle)
            self._keyboard_handle = None
            raise HookInstallError(f"WH_MOUSE_LL err={w.kernel32.GetLastError()}")

    def _pump(self) -> None:
        message = w.MSG()
        while self._running.is_set():
            # PeekMessage + 空闲等待，而不是阻塞的 GetMessage：
            # 阻塞会让 stop() 依赖 PostThreadMessage，而多一个消息源就多一条能卡住的路径。
            result = w.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1)  # PM_REMOVE
            if result:
                if message.message == w.WM_QUIT:
                    break
                w.user32.TranslateMessage(ctypes.byref(message))
                w.user32.DispatchMessageW(ctypes.byref(message))
            else:
                time.sleep(_MESSAGE_PUMP_IDLE_MS / 1000.0)

    def _uninstall(self) -> None:
        if self._keyboard_handle:
            w.user32.UnhookWindowsHookEx(self._keyboard_handle)
            self._keyboard_handle = None
        if self._mouse_handle:
            w.user32.UnhookWindowsHookEx(self._mouse_handle)
            self._mouse_handle = None

    # ---- 钩子过程（必须极快返回）----

    def _keyboard_hook(self, code: int, wparam: int, lparam: int):
        if code < 0:
            return w.user32.CallNextHookEx(None, code, wparam, lparam)
        try:
            info = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
            injected = bool(info.flags & w.LLKHF_INJECTED)
            if self.on_input_seen is not None:
                self.on_input_seen(not injected)
            # 注入事件一律放行 —— AI 自己的 SendInput 不能被自己吞掉。
            if injected:
                return w.user32.CallNextHookEx(None, code, wparam, lparam)

            is_down = wparam in (w.WM_KEYDOWN, w.WM_SYSKEYDOWN)
            if is_down and info.vkCode == w.VK_ESCAPE and not self._abort_latched:
                # 物理 Esc：**先把封锁解除，再通知回调**。
                #
                # 顺序与「无条件解封」都是安全底线，不是选择：
                # 物理 Esc 是用户拿回控制的唯一保底手段，它**不能依赖回调是否存在、
                # 是否被正确挂上、是否抛异常**。这里直接清 `_blocking`，
                # 所以哪怕 `on_abort` 是 None 或抛了错，输入也已经放行了。
                #
                # （这条曾经写错：`return 1` 是无条件的，而 `on_abort()` 只在不为 None
                # 时调用 —— 于是「没挂回调」就等于「Esc 被吞掉、什么都不发生」，
                # 用户会觉得按 Esc 没用。它把安全底线交给了调用方的正确性。）
                self._abort_latched = True
                self._blocking.clear()
                if self.on_abort is not None:
                    try:
                        self.on_abort()
                    except Exception:  # noqa: BLE001 —— 回调炸了不能影响解封
                        pass
                return 1
            if not is_down and info.vkCode == w.VK_ESCAPE:
                # 抬起事件：封锁已解除就放行，让应用能收到完整的按键对。
                return 1 if self._blocking.is_set() else w.user32.CallNextHookEx(
                    None, code, wparam, lparam)

            if self._blocking.is_set():
                return 1                    # 吞掉物理事件
            return w.user32.CallNextHookEx(None, code, wparam, lparam)
        except Exception:  # noqa: BLE001 —— 钩子过程绝不能抛出，抛出会让输入卡住
            return w.user32.CallNextHookEx(None, code, wparam, lparam)

    def _mouse_hook(self, code: int, wparam: int, lparam: int):
        if code < 0:
            return w.user32.CallNextHookEx(None, code, wparam, lparam)
        try:
            info = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
            injected = bool(info.flags & w.LLMHF_INJECTED)
            if self.on_input_seen is not None:
                self.on_input_seen(not injected)
            if injected:
                return w.user32.CallNextHookEx(None, code, wparam, lparam)
            if self._blocking.is_set():
                return 1                    # 吞掉物理事件
            return w.user32.CallNextHookEx(None, code, wparam, lparam)
        except Exception:  # noqa: BLE001
            return w.user32.CallNextHookEx(None, code, wparam, lparam)
