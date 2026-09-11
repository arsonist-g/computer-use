"""截图 —— 降级链、全黑检测、文件命名（DEC-007 / DEC-016 / DEC-012）。

**关于降级层数的一处诚实偏离**：设计里写了四层 `WGC → PrintWindow → DXGI DD 裁剪 → 报错`，
实现里只有三层能落地。原因不是偷懒：

  - 主路径 WGC、末层 DXGI 都由 `windows-capture` 直接提供，文件名与坐标语义可控。
  - **PrintWindow 这一层需要 GDI 位图 → PNG 的编码器**。base 环境禁止 import 重型库
    （PIL / opencv / numpy，DEC-039），而 `windows-capture` 自带的图像保存内部就走 opencv。
    用 ctypes 手写一套 GDI+ PNG 编码器换取一个 spike 从未验证过的兜底层，是把风险换了个地方，
    不是消掉它。

因此实现为 **WGC → DXGI → 显式报错**，并把这条偏离记进 `spike/RESULTS.md` 的遗留项。
降级链失败时抛 `capture_failed` 而不是返回一张黑图 —— 绝不静默失败。

**全黑检测是必需的**，不是防御性代码：spike 实测新建 DXGI 会话后立刻取帧会拿到整帧全黑
（`mean=0.0 max=0`），必须丢弃黑帧或重建会话。WGC 路径同样可能拿到未渲染完的帧。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..errors import CUError, ErrorCode
from ..ids import artifact_name
from . import win32 as w
from .base import CaptureResult, WindowInfo

#: 单层超时。取帧是「等一帧到达」，不是「渲染一帧」——正常在几十毫秒内。
LAYER_TIMEOUT_SECONDS = 4.0
#: 连续拿到多少帧全黑就放弃这一层。
MAX_BLACK_FRAMES = 5


class CaptureLayerError(Exception):
    """某一层失败。只为驱动降级链，不对外暴露。"""


def _frame_is_black(frame, threshold: int = 2) -> bool:
    """全黑判定。用 numpy 的均值而不是逐像素循环 —— 4K 帧有 1400 万像素。"""
    try:
        return float(frame.frame_buffer.mean()) <= threshold
    except Exception:  # noqa: BLE001 —— 拿不到 buffer 时宁可当作「不黑」，交给上层继续
        return False


def _wc_monitor_index(monitor_index: int) -> int:
    """本项目的显示器序号（0 基）→ `windows-capture` 的序号（**1 基**）。

    实测：`WindowsCapture(monitor_index=1)` 拿到主显示器 3440x1440，
    `monitor_index=0` 直接抛「The monitor index must be greater than zero」。
    这个 off-by-one 很容易被误读成「显示器不存在」。
    """
    return monitor_index + 1


def _save(frame, path: Path) -> None:
    """发起保存。**不等落盘** —— 编码是异步的。

    在 `on_frame_arrived` 回调里等文件出现会死锁（保存线程在等捕获线程放行），
    所以「等文件真的出现」这一步由**调用方**在 `control.stop()` 之后做，
    见 `_await_file`。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.save_as_image(str(path))


def _await_file(path: Path, timeout: float = 5.0) -> None:
    """等异步编码落盘。等不到就报错，让降级链继续。

    宁可走下一层，也不要返回一个「成功但文件是空的」结果 ——
    那会让 AI 拿到一个不存在的截图路径然后去点不存在的坐标。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.stat().st_size > 0:
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise CaptureLayerError(f"保存后文件仍为空：{path.name}")


# ---------------------------------------------------------------------------
# 第 1 层：WGC（主路径，DEC-007 第 1 层）
# ---------------------------------------------------------------------------


def _capture_wgc_window(hwnd: int, path: Path) -> tuple[int, int]:
    from windows_capture import WindowsCapture

    capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=True, draw_border=False)
    done = threading.Event()
    captured: dict = {}
    failures: list[BaseException] = []
    black_frames = 0

    @capture.event
    def on_frame_arrived(frame, control):
        nonlocal black_frames
        if _frame_is_black(frame):
            black_frames += 1
            if black_frames >= MAX_BLACK_FRAMES:
                control.stop()
                failures.append(CaptureLayerError(f"WGC 连续 {black_frames} 帧全黑"))
                done.set()
            return
        try:
            _save(frame, path)
            captured["size"] = (frame.width, frame.height)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            control.stop()
            done.set()

    @capture.event
    def on_closed():
        done.set()

    control = capture.start_free_threaded()
    if not done.wait(LAYER_TIMEOUT_SECONDS):
        try:
            control.stop()
        except Exception:  # noqa: BLE001
            pass
        raise CaptureLayerError(f"WGC 在 {LAYER_TIMEOUT_SECONDS}s 内未交付可用帧")
    if failures:
        raise CaptureLayerError(str(failures[0]))
    if "size" not in captured:
        raise CaptureLayerError("WGC 未产生帧")
    _await_file(path)          # 异步编码在这里才落盘
    return captured["size"]


def _capture_wgc_monitor(monitor_index: int, path: Path) -> tuple[int, int]:
    from windows_capture import WindowsCapture

    capture = WindowsCapture(monitor_index=_wc_monitor_index(monitor_index),
                             cursor_capture=True, draw_border=False)
    done = threading.Event()
    captured: dict = {}
    failures: list[BaseException] = []
    black_frames = 0

    @capture.event
    def on_frame_arrived(frame, control):
        nonlocal black_frames
        if _frame_is_black(frame):
            black_frames += 1
            if black_frames >= MAX_BLACK_FRAMES:
                control.stop()
                failures.append(CaptureLayerError(f"WGC(显示器) 连续 {black_frames} 帧全黑"))
                done.set()
            return
        try:
            _save(frame, path)
            captured["size"] = (frame.width, frame.height)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            control.stop()
            done.set()

    @capture.event
    def on_closed():
        done.set()

    control = capture.start_free_threaded()
    if not done.wait(LAYER_TIMEOUT_SECONDS):
        try:
            control.stop()
        except Exception:  # noqa: BLE001
            pass
        raise CaptureLayerError(f"WGC(显示器) 在 {LAYER_TIMEOUT_SECONDS}s 内未交付可用帧")
    if failures:
        raise CaptureLayerError(str(failures[0]))
    if "size" not in captured:
        raise CaptureLayerError("WGC(显示器) 未产生帧")
    _await_file(path)          # 异步编码在这里才落盘
    return captured["size"]


# ---------------------------------------------------------------------------
# 第 3 层：DXGI Desktop Duplication（DEC-007 末层）
# ---------------------------------------------------------------------------


def _capture_dxgi(monitor_index: int, crop: tuple[int, int, int, int] | None,
                  path: Path) -> tuple[int, int]:
    """DXGI 取帧。`crop` 非空时裁出窗口区域（窗口截图走这条）。

    spike 陷阱 4：新建会话后立刻取帧可能是整帧全黑，必须**丢弃黑帧并循环重试**，
    必要时重建会话。
    """
    from windows_capture import DxgiDuplicationSession

    session = DxgiDuplicationSession(monitor_index=_wc_monitor_index(monitor_index))
    deadline = time.monotonic() + LAYER_TIMEOUT_SECONDS
    black_frames = 0
    recreated = False

    while time.monotonic() < deadline:
        frame = session.acquire_frame(200)
        if frame is None:
            continue
        if _frame_is_black(frame):
            black_frames += 1
            if black_frames >= MAX_BLACK_FRAMES and not recreated:
                # 先重建会话再放弃 —— spike 明确记下了这条恢复路径。
                session.recreate()
                recreated = True
                black_frames = 0
                continue
            if black_frames >= MAX_BLACK_FRAMES * 2:
                raise CaptureLayerError("DXGI 持续返回全黑帧")
            continue
        try:
            _save(frame, path)
        except Exception as exc:  # noqa: BLE001
            raise CaptureLayerError(f"DXGI 保存失败：{exc}") from exc
        _await_file(path)
        # 注：DXGI frame 没有 crop()（spike 陷阱 3：WGC Frame 与 DXGI frame 不是同一类型），
        # 因此窗口截图的这一层存的是**整个显示器**。坐标换算仍以 origin 为准 ——
        # 这也是为什么 WGC 是主路径，DXGI 只作为兜底。
        return (frame.width, frame.height)

    raise CaptureLayerError(f"DXGI 在 {LAYER_TIMEOUT_SECONDS}s 内未取得可用帧")


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def capture_window(hwnd: int, out_dir: Path, seq: int, window: WindowInfo,
                   image_format: str = "png") -> CaptureResult:
    """按 hwnd 截图，走降级链。

    `origin` = 该窗口 `GetWindowRect()` 的左上角。spike 实测 WGC 窗口截图**含标题栏**，
    图像 (0,0) 精确对应 (left, top)，因此点击换算 `screen = (left + x, top + y)` 无偏移
    —— 落实 CONSTRAINT-003。
    """
    from ..ids import format_hwnd

    rect = w.window_rect(hwnd)
    if rect is None:
        raise CUError(ErrorCode.WINDOW_NOT_FOUND,
                      f"窗口不存在：{format_hwnd(hwnd)}", {"hwnd": format_hwnd(hwnd)})
    left, top, right, bottom = rect
    origin = (left, top)
    name = artifact_name("win", seq, hwnd=format_hwnd(hwnd), title=window.title,
                         origin=origin, ext=image_format)
    path = out_dir / name

    failures: list[str] = []
    try:
        width, height = _capture_wgc_window(hwnd, path)
        return CaptureResult(path=str(path), origin=origin, width=width, height=height,
                             layer="wgc", kind="window", window=window)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"WGC: {exc}")

    try:
        width, height = _capture_dxgi(window.monitor, (left, top, right, bottom), path)
        return CaptureResult(path=str(path), origin=origin, width=width, height=height,
                             layer="dxgi", kind="window", window=window)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"DXGI: {exc}")

    raise CUError(
        ErrorCode.CAPTURE_FAILED,
        f"截图降级链全部失败：{'；'.join(failures)}",
        {"hwnd": format_hwnd(hwnd), "attempts": failures},
    )


def capture_full(monitor_index: int, out_dir: Path, seq: int,
                 image_format: str = "png") -> CaptureResult:
    """全屏截图 = 主显示器一张（DEC-009 / DEC-010）。

    全屏截图的 `origin` 恒为 (0,0)：坐标系原点就是主显示器左上角（DEC-001）。
    """
    from .windows import display_context

    context = display_context()
    monitor = next((m for m in context.monitors if m.index == monitor_index), None)
    if monitor is None:
        raise CUError(ErrorCode.INVALID_PARAMS,
                      f"显示器 {monitor_index} 不存在",
                      {"available": [m.index for m in context.monitors]})
    origin = (monitor.rect[0], monitor.rect[1])
    name = artifact_name("full", seq, hwnd="0x00000000", title="全屏",
                         origin=(0, 0), ext=image_format)
    path = out_dir / name

    failures: list[str] = []
    try:
        width, height = _capture_wgc_monitor(monitor_index, path)
        return CaptureResult(path=str(path), origin=origin, width=width, height=height,
                             layer="wgc", kind="full", window=None)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"WGC: {exc}")

    try:
        width, height = _capture_dxgi(monitor_index, None, path)
        return CaptureResult(path=str(path), origin=origin, width=width, height=height,
                             layer="dxgi", kind="full", window=None)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"DXGI: {exc}")

    raise CUError(ErrorCode.CAPTURE_FAILED,
                  f"全屏截图降级链全部失败：{'；'.join(failures)}",
                  {"monitor": monitor_index, "attempts": failures})
