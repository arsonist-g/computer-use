"""S2b —— 专门回答 Q2c：DXGI Desktop Duplication 会不会绕过 WDA？

S2 里 D 组返回了整帧全黑，那不是结论，是捕获失败。这里补两组对照把它钉死：

  C1 DXGI 无覆盖层          -> 必须非黑（否则 DXGI 在本机这个上下文里根本不可用，Q2c 只能记为"未验证"）
  C2 DXGI 有覆盖层、无 WDA   -> 必须见青色（否则说明 DXGI 根本没在拍实时桌面，Q2c 同样无法判定）
  C3 DXGI 有覆盖层、有 WDA   -> 见青色 = WDA 被绕过（坏消息）；不见 = WDA 对 DXGI 也生效（好消息）

另外：DXGI 的 AcquireNextFrame 在桌面静止时会超时，且会话刚建时首帧可能是黑的。
所以这里循环取若干帧，取最后一张非黑帧。
"""
import ctypes
import time
from pathlib import Path

from s2_overlay import (                          # noqa: E402  复用已验证的覆盖层实现
    OUT, WDA_EXCLUDEFROMCAPTURE, build_overlay, declare_dpi_awareness,
    destroy, edge_samples, is_cyan, pump, user32,
)

import cv2                                        # noqa: E402
import numpy as np                                # noqa: E402


def dxgi_best(path, tries=12, gap=0.15):
    """连续取帧，返回最后一张非黑帧；全黑则返回 None。"""
    from windows_capture import DxgiDuplicationSession
    sess = DxgiDuplicationSession()
    best = None
    for _ in range(tries):
        try:
            f = sess.acquire_frame(timeout_ms=400)
        except Exception:                         # noqa: BLE001  访问丢失时按文档重建
            try:
                sess = DxgiDuplicationSession()
            except Exception:                     # noqa: BLE001
                break
            time.sleep(gap)
            continue
        if f is not None:
            arr = f.to_bgr()
            if arr.max() > 10:
                best = arr
        time.sleep(gap)
    if best is not None:
        cv2.imwrite(str(path), best)
    return best


def main():
    declare_dpi_awareness()
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    results = []

    # --- C1 无覆盖层 ---
    arr = dxgi_best(OUT / "s2b_C1_dxgi_no_overlay.png")
    if arr is None:
        results.append(("C1 DXGI 无覆盖层", "INVALID",
                        "连续取帧全黑 —— DXGI 在本机此上下文不可用，Q2c 无法判定"))
    else:
        b = edge_samples(arr)
        results.append(("C1 DXGI 无覆盖层", "PASS" if not is_cyan(b) else "FAIL",
                        f"非黑帧 {arr.shape[1]}x{arr.shape[0]} 边缘 BGR=({b[0]:.0f},{b[1]:.0f},{b[2]:.0f})"))

    hwnd, res = build_overlay(w, h)
    print("覆盖层已显示（屏幕四边泛青，约 5 秒）")
    pump(0.8)

    # --- C2 有覆盖层、无 WDA ---
    arr = dxgi_best(OUT / "s2b_C2_dxgi_overlay_no_wda.png")
    if arr is None:
        results.append(("C2 DXGI 见覆盖层（控制组）", "INVALID",
                        "连续取帧全黑，无法判定 DXGI 是否在拍实时桌面"))
    else:
        b = edge_samples(arr)
        seen = is_cyan(b)
        results.append(("C2 DXGI 见覆盖层（控制组）", "PASS" if seen else "FAIL",
                        f"边缘 BGR=({b[0]:.0f},{b[1]:.0f},{b[2]:.0f}) 见青色={seen}"
                        + ("" if seen else "  <-- DXGI 根本没拍实时桌面，C3 无意义")))

    # --- C3 有覆盖层、有 WDA ---
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        results.append(("C3 WDA 是否被 DXGI 绕过", "INVALID",
                        f"SetWindowDisplayAffinity 失败 err={ctypes.get_last_error()}"))
    else:
        pump(0.8)
        arr = dxgi_best(OUT / "s2b_C3_dxgi_overlay_wda.png")
        if arr is None:
            results.append(("C3 WDA 是否被 DXGI 绕过", "INVALID", "连续取帧全黑"))
        else:
            b = edge_samples(arr)
            seen = is_cyan(b)
            results.append(("C3 WDA 是否被 DXGI 绕过", "FAIL" if seen else "PASS",
                            f"边缘 BGR=({b[0]:.0f},{b[1]:.0f},{b[2]:.0f}) 见青色={seen}"
                            + ("  <-- DXGI 绕过 WDA：全屏截图会被覆盖层污染" if seen
                               else "  -> WDA 对 DXGI 同样生效")))

    destroy(hwnd, res)
    pump(0.3)

    print("\n===== S2b 结果 =====")
    for name, verdict, detail in results:
        print(f"[{verdict:7}] {name}\n           {detail}")


if __name__ == "__main__":
    main()
