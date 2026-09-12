"""覆盖层为什么看不见 —— 逐环节定位。

用户没有看到任何覆盖层（连胶囊都没有）。可能断在三个地方，必须分开查：

  ① `UpdateLayeredWindow` 返回失败（错误码会说明原因）
  ② 缓冲里根本没画出东西
  ③ 缓冲画出来了，但传给 DIB 时 alpha 用错（**预乘 alpha** —— Win32 的要求）

第 ③ 项是本脚本的重点怀疑对象：`AC_SRC_ALPHA` 要求颜色分量**预先乘过 alpha**，
而我之前写的是**非预乘**。后果是「半透明的光晕」被当成「接近纯白的实体」显示 ——
但那应该很**显眼**而不是看不见，所以 ③ 单独不足以解释「完全看不到」。
因此这项要逐条验，不能猜。

跑法（自己会显示覆盖层，**不封锁输入**）：
    .venv/Scripts/python.exe core/tests/native/overlay_diag.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.desktop import win32 as w  # noqa: E402
from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402
from cu.desktop.overlay import (  # noqa: E402
    ControlOverlay,
    OverlayState,
    _read_screen,
)

ensure_dpi_awareness()


def main() -> int:
    screen = _read_screen()
    print(f"屏幕: {screen.width}x{screen.height} @({screen.x},{screen.y})")
    print(f"短边: {screen.short_side}  光晕 reach（比例算出的）: {screen.reach}")

    overlay = ControlOverlay()
    overlay._screen = screen

    # ---- ① 窗口能不能建出来 ----
    print("\n[①] 建窗口 ……")
    try:
        overlay._ensure_window()
    except Exception as exc:  # noqa: BLE001
        print(f"    建窗口失败: {type(exc).__name__}: {exc}")
        return 1
    hwnd = overlay._hwnd
    print(f"    hwnd = {hwnd!r}")
    if not hwnd:
        print("    ✗ 没有 hwnd —— 问题在这一步之后无从谈起")
        return 1

    # 窗口是否真的可见（IsWindowVisible）
    visible = bool(w.user32.IsWindowVisible(hwnd))
    print(f"    IsWindowVisible = {visible}")
    rect = w.window_rect(hwnd)
    print(f"    窗口矩形 = {rect}")
    ex = w.window_ex_style(hwnd)
    print(f"    ex_style = 0x{ex:08X}"
          f"  layered={bool(ex & 0x00080000)} transparent={bool(ex & 0x20)}"
          f" noactivate={bool(ex & 0x08000000)}")

    # ---- ② 缓冲里有没有东西 ----
    print("\n[②] 渲染缓冲 ……")
    overlay._state = OverlayState.ACTIVE
    overlay._state_since = time.monotonic()
    overlay.set_target((900, 600, 400, 300))     # 故意给一个大目标框，好看
    overlay.set_cursor((900, 600))

    width = 480
    height = max(1, int(screen.height * width / screen.width))
    buf = overlay._build_pixels(width, height)
    nonzero = sum(1 for i in range(3, len(buf), 4) if buf[i] > 0)
    opaque = sum(1 for i in range(3, len(buf), 4) if buf[i] > 200)
    print(f"    缓冲 {width}x{height} = {len(buf)} 字节")
    print(f"    alpha>0 的像素: {nonzero} / {width * height}")
    print(f"    alpha>200 的像素: {opaque}")

    # 目标框中心那一行，看有没有 2px 实线环
    tx, ty = 900, 600
    sx, sy = width / screen.width, height / screen.height
    cx, cy = int(tx * sx), int((ty + 150) * sy)
    row = [buf[(cy * width + x) * 4 + 3] for x in range(max(0, cx - 30), min(width, cx + 30))]
    print(f"    目标框左侧那一行 alpha（前 30）: {row[:30]}")

    # ---- ③ UpdateLayeredWindow / SetDIBits 的返回与错误码 ----
    print("\n[③] 实际上屏（UpdateLayeredWindow）……")
    calls: list[tuple[str, bool, int]] = []

    gdi = w.gdi32
    real_setdibits = gdi.SetDIBits
    real_ulw = w.user32.UpdateLayeredWindow
    real_stretch = gdi.SetStretchBltMode

    def spy_setdibits(*a, **k):
        result = real_setdibits(*a, **k)
        calls.append(("SetDIBits", bool(result), ctypes.get_last_error()))
        return result

    def spy_stretch(*a, **k):
        result = real_stretch(*a, **k)
        calls.append(("SetStretchBltMode", bool(result), ctypes.get_last_error()))
        return result

    def spy_ulw(*a, **k):
        ctypes.set_last_error(0)
        result = real_ulw(*a, **k)
        calls.append(("UpdateLayeredWindow", bool(result), ctypes.get_last_error()))
        return result

    gdi.SetDIBits = spy_setdibits
    gdi.SetStretchBltMode = spy_stretch
    w.user32.UpdateLayeredWindow = spy_ulw

    try:
        overlay._blit(buf, width, height)
    except Exception as exc:  # noqa: BLE001
        print(f"    _blit 抛异常: {type(exc).__name__}: {exc}")
        calls.append(("_blit 抛异常", False, 0))
    finally:
        gdi.SetDIBits = real_setdibits
        gdi.SetStretchBltMode = real_stretch
        w.user32.UpdateLayeredWindow = real_ulw

    for name, ok, err in calls:
        mark = "✓" if ok else "✗"
        print(f"    {mark} {name}  返回={'成功' if ok else '失败'}  GetLastError={err}")

    failed = [c for c in calls if not c[1]]
    if failed:
        print("\n    ⚠️ 有调用失败 —— 错误码对应：")
        for code, meaning in ((6, "ERROR_INVALID_HANDLE"), (87, "ERROR_INVALID_PARAMETER"),
                              (5, "ERROR_ACCESS_DENIED")):
            if any(err == code for _, _, err in failed):
                print(f"       {code} = {meaning}")

    # ---- ④ alpha 是否预乘 ----
    print("\n[④] alpha 预乘检查（`AC_SRC_ALPHA` 要求颜色已乘过 alpha）……")
    # 找一个半透明像素看看颜色与 alpha 的关系
    sample = None
    for i in range(0, len(buf), 4):
        if 8 < buf[i + 3] < 200:
            sample = (buf[i + 2], buf[i + 1], buf[i], buf[i + 3])   # R,G,B,A
            break
    if sample:
        r, g, b, a = sample
        print(f"    取样像素 RGBA = {sample}")
        print(f"    若已预乘，颜色应 ≤ alpha({a})；实测 max(RGB)={max(r, g, b)}"
              f" → {'已预乘' if max(r, g, b) <= a else '**未预乘**'}")
    else:
        print("    没找到半透明像素，无法判断")

    # ---- ⑤ 显示 6 秒让你看 ----
    print("\n[⑤] 现在把覆盖层显示 6 秒。**请看你的屏幕**：")
    print("        应当看到：屏幕四边的彩虹光晕 + 顶部胶囊 + 屏幕中央一个琥珀色方框")
    overlay._show()
    for remaining in (6, 5, 4, 3, 2, 1):
        print(f"        … {remaining}", flush=True)
        time.sleep(1.0)
    overlay._hide()
    print("        已撤下。")

    overlay._destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
