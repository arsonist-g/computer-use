"""S2 —— 验证 ctypes 能否实现覆盖层，以及 WDA 是否让它在截图里消失。

回答三个问题：
  Q2a ctypes 能否创建分层窗口 + 逐像素 alpha + 点击穿透 + 置顶？（覆盖层的实现前提）
  Q2b SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) 能否让覆盖层从 WGC 捕获中消失？
  Q2c 同一个 WDA 对 DXGI Desktop Duplication 是否也生效？（Q-017，决定了全屏截图会不会被污染）

方法（自证式对照，不依赖任何外部工具的 z-order）：
  A 基线：无覆盖层             -> 边缘不应有青色
  B 覆盖层在，未设 WDA         -> 边缘必须有青色  ← 若这步没青色，说明测试本身无效，后面结论作废
  C 覆盖层在，已设 WDA         -> 边缘不应有青色
  D 覆盖层在，已设 WDA，走 DXGI -> 检查青色

  B 是控制组：它证明覆盖层确实画在屏幕上了、且捕获确实看得见它。
  没有 B 的青色，C 的"没有青色"就无法区分"WDA 生效"和"覆盖层压根没显示"。
"""
import ctypes
import time
from ctypes import wintypes
from pathlib import Path

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
SW_SHOWNOACTIVATE = 4
DIB_RGB_COLORS = 0
CYAN_BGR = (255, 255, 0)          # B=255 G=255 R=0 -> 青
BAND_REACH = 220                  # 光晕向内羽化深度（物理像素）


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# 签名（不声明 argtypes 时 ctypes 会把 64 位句柄截断成 int，这是常见崩溃源）
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]


def declare_dpi_awareness():
    """必须在任何坐标读取之前调用（CONSTRAINT-002）。实测：不声明则 3440 被虚拟化成 2752。"""
    if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
        raise OSError(f"SetProcessDpiAwarenessContext failed: {ctypes.get_last_error()}")


def build_overlay(w, h):
    """创建覆盖层窗口并绘制一圈青色羽化带，返回 hwnd 与资源句柄（供释放）。"""
    import numpy as np

    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
        "STATIC", "cu-spike-overlay", WS_POPUP,
        0, 0, w, h, None, None, None, None,
    )
    if not hwnd:
        raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负值 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                  ctypes.byref(bits), None, 0)
    if not hbmp:
        raise OSError(f"CreateDIBSection failed: {ctypes.get_last_error()}")
    gdi32.SelectObject(hdc_mem, hbmp)

    # 距最近边缘的距离 -> alpha（边缘 1，向内羽化到 0），预乘后写入
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.minimum(np.minimum(xx, yy), np.minimum(w - 1 - xx, h - 1 - yy)).astype(np.float32)
    a = np.clip(1.0 - dist / BAND_REACH, 0.0, 1.0)
    a = (a * a * 255.0).astype(np.uint8)                       # 平方让边缘更浓、羽化更长
    buf = np.zeros((h, w, 4), dtype=np.uint8)                  # BGRA
    for i, c in enumerate(CYAN_BGR):
        buf[..., i] = (c * (a.astype(np.float32) / 255.0)).astype(np.uint8)   # 预乘
    buf[..., 3] = a
    ctypes.memmove(bits, buf.ctypes.data, buf.nbytes)

    pt_dst, size, pt_src = POINT(0, 0), SIZE(w, h), POINT(0, 0)
    bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    if not user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst),
                                      ctypes.byref(size), hdc_mem, ctypes.byref(pt_src),
                                      0, ctypes.byref(bf), ULW_ALPHA):
        raise OSError(f"UpdateLayeredWindow failed: {ctypes.get_last_error()}")
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    return hwnd, (hdc_screen, hdc_mem, hbmp)


def destroy(hwnd, res):
    hdc_screen, hdc_mem, hbmp = res
    user32.DestroyWindow(hwnd)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)


def pump(seconds):
    """分层窗口不需要消息循环也能显示，但泵一下更稳。"""
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def cap_wgc(path):
    import threading
    from windows_capture import WindowsCapture
    box = {}
    cap = WindowsCapture(cursor_capture=False, draw_border=False, monitor_index=1)

    @cap.event
    def on_frame_arrived(frame, control):
        frame.save_as_image(str(path))
        control.stop()

    @cap.event
    def on_closed():
        pass

    t = threading.Thread(target=cap.start, daemon=True)
    t.start()
    t.join(8.0)
    return path.exists()


def cap_dxgi(path):
    import cv2
    from windows_capture import DxgiDuplicationSession
    s = DxgiDuplicationSession()
    f = s.acquire_frame(timeout_ms=1500)
    if f is None:
        return None
    arr = f.to_bgr()
    cv2.imwrite(str(path), arr)
    return arr


def edge_samples(img):
    """在四条边的内侧 20px 处各取一点，返回这些点的平均 BGR。"""
    h, w = img.shape[:2]
    pts = [(20, h // 2), (w - 21, h // 2), (w // 2, 20), (w // 2, h - 21)]
    vals = [img[y, x] for x, y in pts]
    b = sum(int(v[0]) for v in vals) / len(vals)
    g = sum(int(v[1]) for v in vals) / len(vals)
    r = sum(int(v[2]) for v in vals) / len(vals)
    return b, g, r


def is_cyan(bgr, tol=70):
    b, g, r = bgr
    return abs(b - CYAN_BGR[0]) < tol and abs(g - CYAN_BGR[1]) < tol and abs(r - CYAN_BGR[2]) < tol


def main():
    declare_dpi_awareness()
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    print(f"DPI 感知已声明；主显示器 {w}x{h}")

    results = []

    # --- A 基线 ---
    p = OUT / "s2_A_baseline.png"
    ok = cap_wgc(p)
    if not ok:
        print("A 基线捕获失败，测试中止")
        return
    import cv2
    img = cv2.imread(str(p))
    bA = edge_samples(img)
    results.append(("Q2a' A 基线（无覆盖层）", "PASS" if not is_cyan(bA) else "FAIL",
                    f"边缘均值 BGR=({bA[0]:.0f},{bA[1]:.0f},{bA[2]:.0f}) 见青色={is_cyan(bA)}"))

    # --- 建覆盖层 ---
    hwnd, res = build_overlay(w, h)
    print(f"覆盖层窗口 hwnd=0x{hwnd:X}  (若此刻看到屏幕四边泛青，属预期，2 秒后消失)")
    pump(1.0)

    # --- B 覆盖层在，未设 WDA（控制组） ---
    p = OUT / "s2_B_no_wda.png"
    cap_wgc(p)
    bB = edge_samples(cv2.imread(str(p)))
    results.append(("Q2a 覆盖层可见（控制组）", "PASS" if is_cyan(bB) else "FAIL",
                    f"边缘均值 BGR=({bB[0]:.0f},{bB[1]:.0f},{bB[2]:.0f}) 见青色={is_cyan(bB)}"
                    + ("" if is_cyan(bB) else "  <-- 控制组失败，后续 C/D 结论作废")))

    # --- C 设 WDA，再走 WGC ---
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        results.append(("Q2b WDA 排除（WGC）", "FAIL",
                        f"SetWindowDisplayAffinity 失败 err={ctypes.get_last_error()}"))
    else:
        pump(0.8)
        p = OUT / "s2_C_wda_wgc.png"
        cap_wgc(p)
        bC = edge_samples(cv2.imread(str(p)))
        results.append(("Q2b WDA 排除（WGC）", "PASS" if not is_cyan(bC) else "FAIL",
                        f"边缘均值 BGR=({bC[0]:.0f},{bC[1]:.0f},{bC[2]:.0f}) 见青色={is_cyan(bC)}"))

        # --- D 同一窗口，走 DXGI ---
        p = OUT / "s2_D_wda_dxgi.png"
        arr = cap_dxgi(p)
        if arr is None:
            results.append(("Q2c WDA 排除（DXGI）", "FAIL", "DXGI 无帧"))
        else:
            bD = edge_samples(arr)
            seen = is_cyan(bD)
            results.append(("Q2c WDA 排除（DXGI）", "FAIL" if seen else "PASS",
                            f"边缘均值 BGR=({bD[0]:.0f},{bD[1]:.0f},{bD[2]:.0f}) 见青色={seen}"
                            + ("  <-- DXGI 绕过了 WDA！全屏截图会被污染" if seen else "")))

    destroy(hwnd, res)
    pump(0.3)

    print("\n===== S2 结果 =====")
    for name, verdict, detail in results:
        print(f"[{verdict:4}] {name}\n         {detail}")


if __name__ == "__main__":
    main()
