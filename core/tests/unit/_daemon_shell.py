"""单测用的 `Daemon` 壳 —— 不跑 `Daemon.__init__`，只装被测方法会用到的部件。

为什么不直接构造：`Daemon.__init__` 的第一句就是 DPI 声明，随后 import
`windows-capture` 去建真实桌面层 —— 那是真机行为，单测里既不该跑也跑不起来。
这里显式装配需要的成员（配置 / 会话 / 写锁 / 日志 / 假桌面），比给生产代码开一条
「测试专用构造分支」干净：被测的每个方法用什么，这里就装什么。

文件名以 `_` 开头，pytest 不会把它当成测试模块收集。
"""

from __future__ import annotations

import threading
from pathlib import Path

from cu.config import Config
from cu.daemon.core import Daemon
from cu.daemon.log import DaemonLog
from cu.daemon.sessions import Sessions
from cu.daemon.writelock import WriteLock
from cu.desktop.base import InputResult, WindowIdentity
from cu.manifest import ScreenshotRecord, WindowRef


class FakeOverlay:
    """只实现 `_write` 会碰的那两个方法。"""

    visible = False

    def set_cursor(self, point) -> None:  # noqa: ANN001 - 仅占位
        pass

    def set_target(self, rect) -> None:  # noqa: ANN001 - 仅占位
        pass


class FakeController:
    """写序列控制器的空壳：只实现 `_write` 会碰到的成员。"""

    def __init__(self) -> None:
        self.overlay = FakeOverlay()
        self.state = "off"

    def begin_write(self, arm_ms: int, hold_seconds: float, *, keep_alive: bool = False) -> None:
        pass

    def wait_for_arm(self, arm_ms: int) -> None:
        pass

    def end_sequence(self) -> None:
        pass

    def note_error(self) -> None:
        pass


class FakeDesktop:
    """记录「桌面层收到了什么」，不碰 Win32。"""

    def __init__(self) -> None:
        self.seen_expect: list[WindowIdentity | None] = []

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1,
              hwnd: int | None = None, expect: WindowIdentity | None = None) -> InputResult:
        self.seen_expect.append(expect)
        return InputResult(ok=True, moved_ms=1, total_ms=2)


def shell(tmp_path: Path, *, desktop: FakeDesktop | None = None) -> Daemon:
    """装一个够用的 daemon。`config.data_dir` 指向 tmp_path，不碰真实数据目录。"""
    daemon = object.__new__(Daemon)
    config = Config(data_dir=str(tmp_path))
    daemon.config = config
    daemon.log = DaemonLog(config.daemon_log, config.daemon_log_limit_bytes,
                           secrets=[config.vlm.api_key])
    daemon.sessions = Sessions(config.sessions_dir,
                               storage_limit_bytes=config.storage_limit_bytes)
    daemon.lock = WriteLock()
    daemon.desktop = desktop or FakeDesktop()
    daemon.controller = FakeController()
    daemon._write_lock = threading.Lock()
    daemon._stop = threading.Event()
    daemon._stop_event = daemon._stop
    return daemon


def record_window_screenshot(daemon: Daemon, session_id: str, *, hwnd: int, pid: int,
                             klass: str) -> ScreenshotRecord:
    """登记一次「窗口截图」—— 写操作前置的期望身份就是从这条记录来的。"""
    seq = daemon.sessions.next_seq(session_id)
    record = ScreenshotRecord(
        seq=seq, kind="window", file=f"win-{seq:04d}.png", width=10, height=10,
        window=WindowRef.of(hwnd, pid, "notepad.exe", "未命名 - 记事本", klass, [0, 0, 10, 10]),
    )
    daemon.sessions.add_screenshot(session_id, record)
    return record


def daemon_log_text(daemon: Daemon) -> str:
    return daemon.log.path.read_text(encoding="utf-8")
