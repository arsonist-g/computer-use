"""桌面层的真实实现 —— 把 win32 / windows / capture / input / controller 接起来。

daemon 只依赖 `base.Desktop` 的形状；这个类是实现那个形状的那一个。
每个方法要么返回真实结果，要么抛带 `error_code` 的 `CUError` —— 没有中间态。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..errors import CUError, ErrorCode
from ..ids import artifact_name
from . import capture as capture_mod
from . import input as input_mod
from . import windows as windows_mod
from .base import CaptureResult, InputResult, ParseResult, WindowInfo
from .controller import WriteSequenceController

#: 写操作前置的自动前台开关。DEC-013 记录了「前置是否可关闭」这一未决项 ——
#: 默认开启（安全优先），需要「就在当前前台窗口上点击」的场景可以关掉。
BRING_TO_FOREGROUND = True


class RealDesktop:
    def __init__(self, config: Config, controller: WriteSequenceController | None = None) -> None:
        self.config = config
        self.controller = controller or WriteSequenceController()

    # ---- 只读 ----

    def list_windows(self, all_windows: bool = False) -> list[WindowInfo]:
        monitors = windows_mod.display_context().monitors
        return windows_mod.enumerate_windows(monitors, all_windows=all_windows)

    def display_context(self):
        return windows_mod.display_context()

    def capture(self, *, hwnd: int | None, monitor: int | None, image_format: str,
                out_dir: Path, seq: int) -> CaptureResult:
        if hwnd is None:
            return capture_mod.capture_full(
                monitor if monitor is not None else 0, out_dir, seq, image_format)
        # 窗口信息要写进清单（`Screenshot.window` 是复盘时的现场证据，DEC-006）。
        monitors = windows_mod.display_context().monitors
        info = next((w for w in windows_mod.enumerate_windows(monitors) if w.hwnd == hwnd), None)
        if info is None:
            from ..ids import format_hwnd

            raise CUError(ErrorCode.WINDOW_NOT_FOUND,
                          f"窗口不存在：{format_hwnd(hwnd)}", {"hwnd": format_hwnd(hwnd)})
        return capture_mod.capture_window(hwnd, out_dir, seq, info, image_format)

    def parse(self, *, hwnd: int | None, image_path: str | None, ai: bool,
              out_dir: Path, seq: int) -> ParseResult:
        # OmniParser 跑在独立环境（DEC-037），与 base 零代码共享。
        # 那条通道由 omni worker 提供；未安装时**显式报错**，不静默降级（DEC-002）。
        raise CUError(
            ErrorCode.OMNI_NOT_INSTALLED,
            "OmniParser 未安装或未接入",
            {"hwnd": hwnd, "image": image_path, "setup": "computer-use setup omni"},
        )

    # ---- 写 ----

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1,
              hwnd: int | None = None) -> InputResult:
        self._preflight(hwnd)
        return input_mod.click(x, y, button=button, count=count,
                               step_ms=self.config.mouse_step_ms,
                               max_points=self.config.mouse_max_points)

    def move(self, x: int, y: int, *, hwnd: int | None = None) -> InputResult:
        self._preflight(hwnd, foreground=False)
        moved = input_mod.move_cursor(x, y, step_ms=self.config.mouse_step_ms,
                                      max_points=self.config.mouse_max_points)
        return InputResult(ok=True, moved_ms=moved, total_ms=moved)

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
             hwnd: int | None = None) -> InputResult:
        self._preflight(hwnd)
        return input_mod.drag(x1, y1, x2, y2, button=button,
                              step_ms=self.config.mouse_step_ms,
                              max_points=self.config.mouse_max_points)

    def scroll(self, dx: int, dy: int, *, at: tuple[int, int] | None = None) -> InputResult:
        return input_mod.scroll(dx, dy, at=at)

    def type_text(self, text: str, *, hwnd: int | None = None) -> InputResult:
        self._preflight(hwnd)
        return input_mod.type_text(text)

    def key(self, combo: str, *, hwnd: int | None = None, force: bool = False) -> InputResult:
        self._preflight(hwnd, foreground=False)
        return input_mod.key(combo, force=force,
                             danger_keys=frozenset(self.config.danger_keys))

    # ---- 前置校验（DEC-013）----

    def _preflight(self, hwnd: int | None, foreground: bool = True) -> None:
        """写操作的三层前置：身份校验 → 自动前台 → （漂移检查在 daemon 侧）。

        没有 hwnd 时跳过 —— 契约允许「就在当前前台窗口上操作」（api-contract.md
        §1.3：`--hwnd` 用于前置校验，**不改变坐标语义**）。
        """
        if hwnd is None:
            return
        windows_mod.check_hwnd(hwnd)
        if foreground and BRING_TO_FOREGROUND:
            windows_mod.bring_to_foreground(hwnd)


def captured_name(kind: str, seq: int, **kwargs) -> str:
    """留给调用方复用的命名入口（与 capture 内部保持同一个函数）。"""
    return artifact_name(kind, seq, **kwargs)
