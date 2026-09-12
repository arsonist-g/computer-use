"""验收清单 §3（截图降级链）的自动化落地 —— 真机、真窗口、真像素判据。

对应 `core/tests/acceptance.md` §3 的 3.1 ~ 3.4。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_capture.py

**会自己起两个窗口**（记事本 + 黑底控制台）并在结束时关掉它们。不会动用户已有的窗口：
`_winutil.spawn` 只认「启动前后新出现的」那个句柄。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _winutil as W  # noqa: E402
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

import numpy as np  # noqa: E402

from cu.desktop import capture as capture_mod  # noqa: E402
from cu.desktop import windows as windows_mod  # noqa: E402
from cu.errors import CUError, ErrorCode  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
OUT = Path(__file__).parent / "out" / "acceptance-capture"


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def _info(hwnd: int):
    return windows_mod.check_hwnd(hwnd).info


def _screen_crop_mean(x: int, y: int, width: int, height: int) -> float | None:
    """DXGI 抓一帧全屏，裁出指定矩形求均值 —— **对照组**：证明遮挡真的挡住了屏幕。"""
    import numpy as np
    from windows_capture import DxgiDuplicationSession

    try:
        session = DxgiDuplicationSession(monitor_index=1)      # 1 基，见 capture._wc_monitor_index
    except Exception:  # noqa: BLE001
        return None
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        frame = session.acquire_frame(200)
        if frame is None:
            continue
        data = frame.to_numpy()
        if float(data.mean()) <= 2:
            continue                    # spike 陷阱 4：新建会话后前几帧可能整帧全黑
        height_px, width_px = data.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width_px, x + width), min(height_px, y + height)
        if x1 <= x0 or y1 <= y0:
            return None
        return float(np.asarray(data[y0:y1, x0:x1]).mean())
    return None


# ---------------------------------------------------------------------------
# 3.1 WGC 能截到被遮挡的窗口
# ---------------------------------------------------------------------------


def check_3_1() -> None:
    target = W.spawn_notepad()
    if target is None:
        record("3.1", "失败", "起不来记事本窗口")
        return
    occluder = None
    try:
        W.move(target, 200, 150, 900, 600, top=True)
        # 记事本会把自己记住的位置/尺寸再套一遍 —— 等它停了再量，否则后面的遮挡
        # 是按一个过期的矩形摆的，对照组会「挡住了一半」，读出的均值自然不黑。
        W.settle(target)
        frame = W.extended_frame(target)
        if frame is None:
            record("3.1", "失败", "取不到靶子窗口的扩展框")
            return
        baseline = capture_mod.capture_window(target, OUT, 1, _info(target), "png")
        # 对照组基准：遮挡**之前**同一块屏幕区域长什么样。
        screen_before = _screen_crop_mean(*frame)

        occluder = W.spawn_console()
        if occluder is None:
            record("3.1", "失败", "起不来遮挡用的控制台窗口")
            return
        # 盖得比靶子大一圈，确保完全遮住。
        W.move(occluder, frame[0] - 20, frame[1] - 20, frame[2] + 40, frame[3] + 40, top=True)
        W.settle(occluder)
        time.sleep(1.0)

        # 遮挡是否**真的**成立：屏幕那一块必须肉眼可见地变了。
        # 判据用「变了多少」而不是「变黑了」—— 遮挡物的底色是用户的终端主题决定的，
        # 钉一个绝对亮度阈值会把「浅色终端挡住了」误判成「没挡住」。
        screen_after = _screen_crop_mean(*frame)
        # 顺带确认 z-order：遮挡物必须排在被遮窗口之前（枚举顺序就是前后顺序）。
        order = [info.hwnd for info in W.windows_mod.enumerate_windows(
            W.windows_mod.display_context().monitors, all_windows=True)]
        above = (occluder in order and target in order
                 and order.index(occluder) < order.index(target))

        occluded = capture_mod.capture_window(target, OUT, 2, _info(target), "png")

        left, right = W.image(baseline.path), W.image(occluded.path)
        same_shape = left.shape == right.shape
        diff = (float(np.abs(left.astype("int16") - right.astype("int16")).mean())
                if same_shape else float("nan"))
        # 窗口截图走的是窗口自己的表面：遮挡前后应当几乎一样（差异只有噪声级别）。
        unchanged = same_shape and diff < 3.0
        # 对照组必须成立，否则「没变化」可能只是因为屏幕那处本来就没变。
        control_ok = (screen_before is not None and screen_after is not None
                      and abs(screen_after - screen_before) >= 10.0)
        record("3.1", "通过" if (unchanged and control_ok and above) else "失败",
               f"遮挡前后窗口截图像素差={diff:.3f}（应≈0）· 同尺寸={same_shape} · "
               f"对照组：屏幕同区域均值 {screen_before if screen_before is None else round(screen_before, 1)}"
               f" → {screen_after if screen_after is None else round(screen_after, 1)}"
               f"（必须明显变化，才算遮挡真的挡住了屏幕）· "
               f"遮挡物 z-order 在靶子之前={above} · layer={occluded.layer} "
               f"{occluded.width}x{occluded.height}")
    except Exception as exc:  # noqa: BLE001
        record("3.1", "失败", f"{type(exc).__name__}: {exc}")
    finally:
        if occluder is not None:
            W.close(occluder)
        W.close(target)


# ---------------------------------------------------------------------------
# 3.2 全黑帧被丢弃，而不是被保存成一张黑图
# ---------------------------------------------------------------------------


def _create_unpainted_window(x: int, y: int, width: int, height: int) -> int:
    """造一个「刚创建、永远不会渲染」的窗口。

    STATIC 是系统类，不需要注册；关键在**不抽消息** —— 没有消息泵就不会响应
    `WM_PAINT`，它的表面永远是未渲染的。这正是验收要的那个场景，而且可复现
    （靠抢时机去截一个「正在启动」的窗口是不可复现的）。
    """
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    WS_POPUP = 0x80000000
    hwnd = user32.CreateWindowExW(0, "STATIC", "cu-acceptance-unpainted", WS_POPUP,
                                  x, y, width, height, None, None, None, None)
    if not hwnd:
        raise RuntimeError(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
    user32.ShowWindow(hwnd, 4)                                     # SW_SHOWNOACTIVATE
    return int(hwnd)


def check_3_2() -> None:
    hwnd = _create_unpainted_window(320, 240, 640, 400)
    try:
        time.sleep(0.15)          # 「刚创建、尚未渲染」——越短越贴近那个窗口
        try:
            result = capture_mod.capture_window(hwnd, OUT, 3, _info(hwnd), "png")
            mean = W.frame_mean(result.path)
            # 成功路径的唯一合法形态：拿到的**不是**黑图。
            record("3.2", "通过" if mean > 2.0 else "失败",
                   f"成功返回，图均值={mean:.2f}（>2 才不是黑图）· layer={result.layer} "
                   f"{result.width}x{result.height}")
        except CUError as exc:
            allowed = exc.code in (ErrorCode.CAPTURE_FAILED, ErrorCode.CAPTURE_BLACK)
            record("3.2", "通过" if allowed else "失败",
                   f"显式报错 {exc.code.value}（允许 capture_failed / capture_black，"
                   f"不允许多出一张黑图）")
    except Exception as exc:  # noqa: BLE001
        record("3.2", "失败", f"{type(exc).__name__}: {exc}")
    finally:
        W.close(hwnd)


# ---------------------------------------------------------------------------
# 3.3 独占全屏的失败路径
# ---------------------------------------------------------------------------


def check_3_3() -> None:
    """**无法构造场景**：本机没有独占全屏应用（写一个 DXGI 独占全屏程序来验一条
    错误提示，成本远大于收益）。能客观验的只有「错误码存在且提示指向无边框全屏」，
    那是静态事实，不是这条要验的行为。"""
    from cu.errors import HINTS

    hint = HINTS[ErrorCode.CAPTURE_FAILED]
    record("3.3", "不适用",
           f"本机没有独占全屏应用可构造该场景 · 静态旁证：capture_failed 的提示是"
           f"「{hint}」，其中已包含「独占全屏请改为无边框全屏」")


# ---------------------------------------------------------------------------
# 3.4 降级链确实会降级
# ---------------------------------------------------------------------------


def check_3_4() -> None:
    target = W.spawn_notepad()
    if target is None:
        record("3.4", "失败", "起不来记事本窗口")
        return
    try:
        W.move(target, 240, 180, 800, 520, top=True)
        time.sleep(0.8)
        info = _info(target)

        normal = capture_mod.capture_window(target, OUT, 4, info, "png")

        # 让主路径**真的**失败，看它会不会降级 —— 而不是看它报错就算了。
        original = capture_mod._capture_wgc_window

        def broken_wgc(hwnd, path):
            raise capture_mod.CaptureLayerError("验收注入：WGC 不可用")

        capture_mod._capture_wgc_window = broken_wgc
        try:
            degraded = capture_mod.capture_window(target, OUT, 5, info, "png")
        finally:
            capture_mod._capture_wgc_window = original

        degraded_ok = degraded.layer == "dxgi" and W.frame_mean(degraded.path) > 2.0

        # 末层也失败 → 必须是 capture_failed 且列出每一层的失败原因，不能静默返回空。
        original_dxgi = capture_mod._capture_dxgi

        def broken_dxgi(monitor_index, crop, path):
            raise capture_mod.CaptureLayerError("验收注入：DXGI 不可用")

        capture_mod._capture_dxgi = broken_dxgi
        capture_mod._capture_wgc_window = broken_wgc
        try:
            capture_mod.capture_window(target, OUT, 6, info, "png")
            exhausted = "没有报错"
        except CUError as exc:
            attempts = (exc.detail or {}).get("attempts") or []
            exhausted = (f"{exc.code.value}，逐层原因 {len(attempts)} 条"
                         if exc.code is ErrorCode.CAPTURE_FAILED and len(attempts) == 2
                         else f"{exc.code.value}，attempts={attempts}")
        finally:
            capture_mod._capture_wgc_window = original
            capture_mod._capture_dxgi = original_dxgi

        ok = normal.layer == "wgc" and degraded_ok and exhausted.startswith("capture_failed")
        record("3.4", "通过" if ok else "失败",
               f"正常层={normal.layer}（应 wgc）· 注入 WGC 失败后层={degraded.layer}"
               f"（应 dxgi，图非黑={W.frame_mean(degraded.path) > 2.0}）· "
               f"两层都失败 → {exhausted}")
    except Exception as exc:  # noqa: BLE001
        record("3.4", "失败", f"{type(exc).__name__}: {exc}")
    finally:
        W.close(target)


def check_9_4() -> None:
    """WGC 的黄框有没有被画进图里（`draw_border=False` 是否真的生效）。

    判据：窗口截图的最外圈像素里有多少「明显偏黄」的像素（R、G 高而 B 低）。
    Win11 的记事本自身是中性的浅色/深色，不会给出成片的黄；
    黄框一旦进来，最外圈会是连续一整圈的高饱和黄。
    """
    target = W.spawn_notepad()
    if target is None:
        record("9.4", "失败", "起不来记事本窗口")
        return
    try:
        W.move(target, 300, 220, 900, 560, top=True)
        W.settle(target)
        result = capture_mod.capture_window(target, OUT, 7, _info(target), "png")
        image = W.image(result.path)
        band = 3
        outer = np.concatenate([
            image[:band].reshape(-1, image.shape[2]),
            image[-band:].reshape(-1, image.shape[2]),
            image[:, :band].reshape(-1, image.shape[2]),
            image[:, -band:].reshape(-1, image.shape[2]),
        ])
        blue = outer[:, 0]
        green = outer[:, 1]
        red = outer[:, 2]
        yellowish = (red > 180) & (green > 150) & (blue < 120)
        ratio = float(yellowish.mean())
        record("9.4", "通过" if ratio < 0.02 else "失败",
               f"图像最外 {band}px 共 {len(outer)} 像素，其中偏黄 {int(yellowish.sum())} 个"
               f"（{ratio:.2%}）· 偏黄占比 <2% 即认为黄框没有进入画面 · "
               f"layer={result.layer} {result.width}x{result.height}")
    except Exception as exc:  # noqa: BLE001
        record("9.4", "失败", f"{type(exc).__name__}: {exc}")
    finally:
        W.close(target)


# ---------------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print("=" * 72)
    print("验收 §3 截图降级链")
    print("=" * 72)

    check_3_1()
    check_3_2()
    check_3_3()
    check_3_4()
    check_9_4()

    print("\n===== §3 结果 =====")
    failed = [r for r in RESULTS if r[1] == "失败"]
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    print(f"\n  通过 {len(RESULTS) - len(failed) - sum(1 for r in RESULTS if r[1] == '不适用')}"
          f" · 失败 {len(failed)} · 不适用 {sum(1 for r in RESULTS if r[1] == '不适用')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
