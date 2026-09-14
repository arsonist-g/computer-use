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
from dataclasses import dataclass
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
    """全黑判定。用 numpy 的均值而不是逐像素循环 —— 4K 帧有 1400 万像素。

    **两种 frame 取像素的方式不一样**，这里必须都认：

      - WGC 的 frame 有 `frame_buffer`（已是 numpy 数组）；
      - `DxgiDuplicationFrame` **没有这个属性**（它内部叫 `_raw_buffer`），
        唯一的解码入口是 `to_numpy()`。

    曾经只读 `frame_buffer`，于是 DXGI 每次取帧都抛 `AttributeError`、
    被下面的兜底吞成「不黑」——**黑帧检测在 DXGI 路径上等于没有**，
    新建会话后的第一帧（spike 陷阱 4 记录过：整帧全黑）被原样存成了文件。
    实测：同一会话连续 5 帧，第 1 帧 `to_numpy().mean()=0.00`、
    第 2 帧起 `86.87 / 110.27…`。所以这条判据不是锦上添花，它是 DXGI 层
    唯一能挡住「静默返回一张黑图」的东西。
    """
    try:
        buffer = getattr(frame, "frame_buffer", None)
        if buffer is None:
            buffer = frame.to_numpy()
        return float(buffer.mean()) <= threshold
    except Exception:  # noqa: BLE001 —— 拿不到 buffer 时宁可当作「不黑」，交给上层继续
        return False


def _wc_monitor_index(monitor_index: int) -> int:
    """本项目的显示器序号（0 基）→ `windows-capture` 的序号（**1 基**）。

    实测：`WindowsCapture(monitor_index=1)` 拿到主显示器 3440x1440，
    `monitor_index=0` 直接抛「The monitor index must be greater than zero」。
    这个 off-by-one 很容易被误读成「显示器不存在」。
    """
    return monitor_index + 1


# ---------------------------------------------------------------------------
# 光标标记（`screenshot --cursor`）
# ---------------------------------------------------------------------------

#: 红框线宽（物理像素）。1px 在深色界面上会糊，2px 才稳。
_MARKER_THICKNESS = 2
#: 红框颜色。帧缓冲是 windows-capture 交付的原始 **BGRA** 布局，不是 RGB。
_MARKER_COLOR = (0, 0, 255, 255)


@dataclass
class _CursorMark:
    """要不要在帧上画光标红框，以及这一层画成了没有。

    帧缓冲只在**取帧回调内部**可写（保存读的就是那块内存），而图像原点是
    对外入口才知道的，所以这两样东西要一起递进回调。
    """

    origin: tuple[int, int]
    size: tuple[int, int]
    result: str = ""


def marker_box(cursor: tuple[int, int], origin: tuple[int, int], width: int, height: int,
               size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """光标红框在**图像坐标**里的位置；光标不在图内时返回 None。

    全项目只有这一处「屏幕坐标 → 图像坐标」的减法，所以它单独成一个纯函数、单独测。
    减 `origin` **之前**必须先判光标在不在图内：先减再判，一个远在图外的光标会被
    平移进图里，于是在图上画出一个并不存在的光标。

    返回 `(x0, y0, x1, y1)`，右下为开区间，已夹进图像范围。
    """
    cx, cy = cursor
    ox, oy = origin
    if not (ox <= cx < ox + width and oy <= cy < oy + height):
        return None
    half_w = max(1, size[0] // 2)
    half_h = max(1, size[1] // 2)
    x0 = min(max(cx - ox - half_w, 0), max(width - 1, 0))
    y0 = min(max(cy - oy - half_h, 0), max(height - 1, 0))
    x1 = min(max(cx - ox + half_w + 1, x0 + 1), width)
    y1 = min(max(cy - oy + half_h + 1, y0 + 1), height)
    return (x0, y0, x1, y1)


def paint_marker(buffer, box: tuple[int, int, int, int]) -> None:
    """在帧缓冲上画一个空心红框。**就地改写** —— 保存读的就是这块内存。

    只用切片赋值、不 import numpy：缓冲区本来就是 windows-capture 交付的零拷贝
    数组（DEC-039 禁的是在 base 环境 import 重型库，不是「不能用已经拿到的数组」）。
    """
    x0, y0, x1, y1 = box
    buffer[y0:y0 + _MARKER_THICKNESS, x0:x1] = _MARKER_COLOR
    buffer[y1 - _MARKER_THICKNESS:y1, x0:x1] = _MARKER_COLOR
    buffer[y0:y1, x0:x0 + _MARKER_THICKNESS] = _MARKER_COLOR
    buffer[y0:y1, x1 - _MARKER_THICKNESS:x1] = _MARKER_COLOR


def _writable_buffer(frame):
    """这块帧能改写的缓冲。WGC 的 Frame 直接给 `frame_buffer`；DXGI 的帧只有
    `to_numpy()`（它会把结果缓存下来，随后保存读的是同一块内存）。"""
    buffer = getattr(frame, "frame_buffer", None)
    return frame.to_numpy() if buffer is None else buffer


def _mark_cursor(frame, origin: tuple[int, int], size: tuple[int, int]) -> str:
    """按光标当前位置在帧上画红框，返回这次的结果口径。"""
    box = marker_box(w.cursor_pos(), origin, int(frame.width), int(frame.height), size)
    if box is None:
        return "outside"
    try:
        paint_marker(_writable_buffer(frame), box)
    except Exception:  # noqa: BLE001 —— 标记画不上不该毁掉这张截图，但必须如实报出来
        return "unsupported"
    return "drawn"


def _finish(result: CaptureResult, mark: _CursorMark | None, marker: str = "") -> CaptureResult:
    """把「光标在哪、在不在图里、红框画没画上」并进截图结果。

    光标坐标默认就报：调用方据此判断落点，而图里那根工字梁是靠不住的视觉证据。
    """
    cx, cy = w.cursor_pos()
    result.cursor = (int(cx), int(cy))
    result.cursor_inside = (result.origin[0] <= cx < result.origin[0] + result.width
                            and result.origin[1] <= cy < result.origin[1] + result.height)
    if mark is not None:
        result.cursor_marker = marker or mark.result or "unsupported"
    return result


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


def _capture_wgc_window(hwnd: int, path: Path,
                        mark: _CursorMark | None = None) -> tuple[int, int]:
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
            if mark is not None:
                mark.result = _mark_cursor(frame, mark.origin, mark.size)
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


def _capture_wgc_monitor(monitor_index: int, path: Path,
                         mark: _CursorMark | None = None) -> tuple[int, int]:
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
            if mark is not None:
                mark.result = _mark_cursor(frame, mark.origin, mark.size)
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
                  path: Path, mark: _CursorMark | None = None) -> tuple[int, int]:
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
            if mark is not None:
                mark.result = _mark_cursor(frame, mark.origin, mark.size)
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
                   image_format: str = "png",
                   draw_cursor: bool = False) -> CaptureResult:
    """按 hwnd 截图，走降级链。

    `origin` 取 **DWM 扩展框**（`extended_frame_bounds`）的左上角，不是 `GetWindowRect`。

    为什么：`GetWindowRect` 含 Win10/11 那条**不可见的调整边框**（本机 125% 缩放下
    左右各 7px），而 WGC 交付的图像只覆盖可见框 —— 实测同一时刻
    `GetWindowRect` 是 900x560、图像是 886x553，两者恰好差 14x7。若拿
    `GetWindowRect` 当原点，`screen = origin + 图像坐标` 就会横向偏 7px，
    违反 CONSTRAINT-003（截图像素坐标系与 click 坐标系必须同源）。
    （曾经的注释写着「图像 (0,0) 精确对应 GetWindowRect」—— 那是错的，见这条实测。）
    """
    from ..ids import format_hwnd

    rect = w.window_rect(hwnd)
    if rect is None:
        raise CUError(ErrorCode.WINDOW_NOT_FOUND,
                      f"窗口不存在：{format_hwnd(hwnd)}", {"hwnd": format_hwnd(hwnd)})
    # 扩展框取不到时（DWM 关闭、窗口尚未合成）退回 GetWindowRect —— 那是次优解，
    # 但比拒绝截图好，且此时两者的差通常也为 0。
    frame = w.extended_frame_bounds(hwnd) or rect
    left, top = frame[0], frame[1]
    origin = (left, top)
    name = artifact_name("win", seq, hwnd=format_hwnd(hwnd), title=window.title,
                         origin=origin, ext=image_format)
    path = out_dir / name

    failures: list[str] = []
    mark = _CursorMark(origin=origin, size=w.cursor_size()) if draw_cursor else None
    try:
        width, height = _capture_wgc_window(hwnd, path, mark)
        return _finish(CaptureResult(path=str(path), origin=origin, width=width, height=height,
                                     layer="wgc", kind="window", window=window), mark)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"WGC: {exc}")

    try:
        # DXGI 这一层存的是**整个显示器**（它没有 crop），图像原点不是窗口原点 ——
        # 拿窗口原点去画红框会画到别的地方，所以这一层不画（记 `unsupported`），
        # 但光标坐标与「在不在图内」照报。
        width, height = _capture_dxgi(window.monitor, rect, path)
        return _finish(CaptureResult(path=str(path), origin=origin, width=width, height=height,
                                     layer="dxgi", kind="window", window=window),
                       mark, marker="unsupported")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"DXGI: {exc}")

    raise CUError(
        ErrorCode.CAPTURE_FAILED,
        f"截图降级链全部失败：{'；'.join(failures)}",
        {"hwnd": format_hwnd(hwnd), "attempts": failures},
    )


def capture_full(monitor_index: int, out_dir: Path, seq: int,
                 image_format: str = "png",
                 draw_cursor: bool = False) -> CaptureResult:
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
    mark = _CursorMark(origin=origin, size=w.cursor_size()) if draw_cursor else None
    try:
        width, height = _capture_wgc_monitor(monitor_index, path, mark)
        return _finish(CaptureResult(path=str(path), origin=origin, width=width, height=height,
                                     layer="wgc", kind="full", window=None), mark)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"WGC: {exc}")

    try:
        width, height = _capture_dxgi(monitor_index, None, path, mark)
        return _finish(CaptureResult(path=str(path), origin=origin, width=width, height=height,
                                     layer="dxgi", kind="full", window=None), mark)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"DXGI: {exc}")

    raise CUError(ErrorCode.CAPTURE_FAILED,
                  f"全屏截图降级链全部失败：{'；'.join(failures)}",
                  {"monitor": monitor_index, "attempts": failures})
