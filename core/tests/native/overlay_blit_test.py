"""覆盖层上屏的最小复现 —— 分步定位 `_blit` 到底断在哪一步。

诊断脚本已经排除掉三样：窗口建得出来（hwnd、样式、可见性都对）、缓冲里有内容
（5 万多个非零像素）、alpha 已预乘。所以问题在**最后把缓冲交给 GDI 再交给
`UpdateLayeredWindow`** 这一段。

这个脚本按**最小步骤**走，每一步都单独报告返回值与错误码：

  A. 屏外 DC + 32 位 DIB，填**纯红不透明**，`UpdateLayeredWindow` 上屏
     —— 若红块出现，说明「上屏这条路」是通的，问题在像素格式或缩放；
        若红块不出现，问题在窗口/ULW 本身。
  B. 同一 DIB，但改成我们实际的 pre-multiplied BGRA 缓冲（降采样 480x200，
     由 ULW 放大到全屏）—— 验放大那一步。
  C. 用产品代码的 `ControlOverlay._blit`（已修正回原始函数引用，不受 spy 干扰）。

**不封锁输入**，只显示。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.desktop import win32 as w  # noqa: E402
from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402
from cu.desktop.overlay import (  # noqa: E402
    _BITMAPINFO,
    _BITMAPINFOHEADER,
    _BLENDFUNCTION,
    _POINT,
    _SIZE,
    ControlOverlay,
    OverlayState,
    _read_screen,
)

ensure_dpi_awareness()

_gdi = ctypes.WinDLL("gdi32", use_last_error=True)
_gdi.SetDIBits.restype = ctypes.c_int
_gdi.SetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_gdi.CreateCompatibleDC.restype = wintypes.HDC
_gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi.DeleteObject.restype = wintypes.BOOL
_gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi.DeleteDC.restype = wintypes.BOOL
_gdi.DeleteDC.argtypes = [wintypes.HDC]
_gdi.SelectObject.restype = wintypes.HGDIOBJ
_gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]

_ulw = w.user32.UpdateLayeredWindow
_ulw.restype = wintypes.BOOL
_ulw.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
                 wintypes.HDC, ctypes.POINTER(_POINT), wintypes.DWORD,
                 ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]


def countdown(seconds: int, note: str) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"        … {remaining} 秒（{note}）")
        time.sleep(1.0)


def make_window(screen) -> int:
    ex = 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080 | 0x00000008
    w.user32.CreateWindowExW.restype = wintypes.HWND
    user32 = w.user32
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    hwnd = user32.CreateWindowExW(ex, "STATIC", "cu-overlay-diag", 0x80000000,
                                  screen.x, screen.y, screen.width, screen.height,
                                  None, None, None, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW err={w.kernel32.GetLastError()}")
    user32.ShowWindow(int(hwnd), 4)
    return int(hwnd)


def upload(hwnd: int, pixels: bytes, width: int, height: int,
           screen, *, scale_to_screen: bool) -> tuple[bool, int]:
    """把 BGRA 缓冲交给 GDI 并上屏。返回 (成功?, 错误码)。

    `scale_to_screen=True` 时 ULW 会把 `width x height` 拉伸到全屏
    （这就是产品用的路径：降采样渲染 + 放大上屏）。
    """
    screen_dc = w.user32.GetDC(None)
    mem_dc = _gdi.CreateCompatibleDC(screen_dc)
    bitmap = _gdi.CreateCompatibleBitmap(screen_dc, width, height)
    w.user32.ReleaseDC(None, screen_dc)
    if not mem_dc or not bitmap:
        return False, w.kernel32.GetLastError()
    # **必须把位图选进 DC** —— 这正是产品里漏掉的那一行。不选的话 DC 里还是它自带的
    # 1×1 单色位图，`UpdateLayeredWindow` 拿不到内容并返回 ERROR_GEN_FAILURE(31)。
    # 这个测试最初复刻了同一个错误，于是三次都失败、什么都没验证到。
    _gdi.SelectObject(mem_dc, bitmap)

    buf = ctypes.create_string_buffer(pixels, len(pixels))
    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    ok_dib = _gdi.SetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(info), 0)
    err_dib = w.kernel32.GetLastError()

    size = _SIZE(screen.width, screen.height) if scale_to_screen else _SIZE(width, height)
    origin = _POINT(screen.x, screen.y) if scale_to_screen else _POINT(0, 0)
    source = _POINT(0, 0)
    blend = _BLENDFUNCTION(0, 0, 255, 1)     # AC_SRC_OVER, 255, AC_SRC_ALPHA
    ctypes.set_last_error(0)
    ok = _ulw(hwnd, None, ctypes.byref(origin), ctypes.byref(size),
              mem_dc, ctypes.byref(source), 0, ctypes.byref(blend), 0x00000002)
    err = w.kernel32.GetLastError()

    _gdi.DeleteObject(bitmap)
    _gdi.DeleteDC(mem_dc)
    if not ok_dib:
        return False, err_dib
    return bool(ok), err


def main() -> int:
    screen = _read_screen()
    print(f"屏幕 {screen.width}x{screen.height}")
    hwnd = make_window(screen)
    print(f"窗口 hwnd={hwnd}  可见={bool(w.user32.IsWindowVisible(hwnd))}")
    print()

    # ---- A. 纯红不透明，原始尺寸（不通 DIB 缩放）----
    print("[A] 纯红不透明，全屏尺寸 —— 这一步若看不到红色，问题在窗口或 ULW 本身")
    pixels = bytes([0, 0, 255, 255]) * (screen.width * screen.height)
    ok, err = upload(hwnd, pixels, screen.width, screen.height, screen, scale_to_screen=False)
    print(f"    结果={'成功' if ok else '失败'}  err={err}")
    countdown(4, "应当看到整屏红色")
    print()

    # ---- A2. 纯红，但用「小缓冲放大」（产品走的路径）----
    print("[A2] 纯红不透明，480x200 由 ULW 放大到全屏 —— 验放大那一步")
    sw, sh = 480, 200
    pixels2 = bytes([0, 0, 255, 255]) * (sw * sh)
    ok2, err2 = upload(hwnd, pixels2, sw, sh, screen, scale_to_screen=True)
    print(f"    结果={'成功' if ok2 else '失败'}  err={err2}")
    countdown(4, "应当看到整屏红色（被拉伸）")
    print()

    # ---- B. 半透明红（验 alpha 通道真的被 ULW 用上）----
    print("[B] 半透明红（alpha=128）—— 若看不到任何变化，说明 alpha 被忽略了")
    pixels3 = bytes([0, 0, 128, 128]) * (sw * sh)     # pre-multiplied
    ok3, err3 = upload(hwnd, pixels3, sw, sh, screen, scale_to_screen=True)
    print(f"    结果={'成功' if ok3 else '失败'}  err={err3}")
    countdown(4, "应当看到半透明红色叠在桌面上")
    print()

    # ---- C. 产品代码的 _blit ----
    print("[C] 产品代码 `ControlOverlay._blit`")
    overlay = ControlOverlay()
    overlay._screen = screen
    overlay._hwnd = hwnd
    overlay._state = OverlayState.ACTIVE
    overlay._state_since = time.monotonic()
    overlay.set_target((900, 600, 500, 400))
    overlay.set_cursor((1700, 700))
    width, height = 480, max(1, int(screen.height * 480 / screen.width))
    buf = overlay._build_pixels(width, height)
    try:
        overlay._blit(buf, width, height)
        print("    _blit 未抛异常")
    except Exception as exc:  # noqa: BLE001
        print(f"    _blit 抛异常: {type(exc).__name__}: {exc}")
    countdown(8, "应当看到四边光晕 + 顶部胶囊 + 琥珀目标框")
    print()

    print("撤下窗口并退出。")
    w.user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
