"""覆盖层上屏取证 —— 它在**屏幕上**到底占了多大、画在了哪里。

## 为什么要这么绕

覆盖层被 `WDA_EXCLUDEFROMCAPTURE` 排除出所有截图管线（DEC-027 的设计意图），
所以**任何常规截屏都看不见它** —— 这正是「覆盖层完全不可见」能连续躲过两轮
测试的原因：所有截图类断言都只会得到「什么都没画」，而那被当成了「光晕本来就该很淡」。

要拿它上屏的证据，只能**临时把 WDA 关掉**再截图。WDA 只影响「能不能被截到」，
不影响「用户能不能看到」，所以这是一个有效对照。

## 它回答什么

1. 光晕的渲染缓冲（480px 宽）比窗口（整屏）小 —— 放大这一步有没有真的发生？
   `UpdateLayeredWindow` **不拉伸**源位图：源比窗口小时它什么都不画。
   判据是四条边带都得有明显像素差；只变左上角 480x200 就说明没放大。
2. 胶囊 / 目标框 / 光标光晕有没有落到它们该在的屏幕位置。

跑法（**会闪过整屏约 8 秒**，不封锁输入）：
    .venv/Scripts/python.exe core/tests/native/overlay_visual_check.py
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

ctypes.WinDLL("kernel32").SetErrorMode(0x0002)   # 压掉 WER 弹窗

from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402

ensure_dpi_awareness()

import cu.desktop.overlay as overlay_mod  # noqa: E402
from cu.desktop.overlay import ControlOverlay, OverlayState  # noqa: E402

TARGET = (900, 600, 400, 300)
CURSOR = (1700, 700)


def grab():
    """取一帧全屏像素（numpy，BGRA）。DXGI 唯一可用的解码路径是它自己的 to_numpy。"""
    from windows_capture import DxgiDuplicationSession

    session = DxgiDuplicationSession(monitor_index=1)   # 1 基，见 capture._wc_monitor_index
    deadline = time.monotonic() + 5.0
    black = 0
    while time.monotonic() < deadline:
        frame = session.acquire_frame(200)
        if frame is None:
            continue
        array = frame.to_numpy()
        if float(array.mean()) <= 2:      # 新建会话后前几帧可能整帧全黑
            black += 1
            if black > 30:
                return None
            continue
        return array
    return None


def region_diff(before, after, x: int, y: int, w: int, h: int) -> float:
    left, top = max(0, x), max(0, y)
    right, bottom = min(after.shape[1], x + w), min(after.shape[0], y + h)
    if right <= left or bottom <= top:
        return -1.0
    patch = (after[top:bottom, left:right].astype("int16")
             - before[top:bottom, left:right].astype("int16"))
    return float(abs(patch[:, :, :3]).mean())


def write_png(path: Path, rgb_rows: bytes, width: int, height: int) -> None:
    """不引图像库写一张 PNG（zlib 是标准库）。"""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # 每行的 filter 字节
        raw += rgb_rows[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                     + chunk(b"IEND", b""))


def main() -> int:
    import numpy as np

    # 关掉捕获排除 —— 只有这样截图才看得见它。
    overlay_mod.capture_exclusion_supported = lambda: False

    print("取基线帧（覆盖层未显示）…")
    before = grab()
    if before is None:
        print("取帧失败 —— 本条**未验证**")
        return 1
    height, width = before.shape[0], before.shape[1]
    print(f"帧尺寸 {width}x{height}")

    overlay = ControlOverlay()
    overlay.set_target(TARGET)
    overlay.set_cursor(CURSOR)
    overlay.start()
    overlay.transition(OverlayState.ARMING)
    overlay.transition(OverlayState.ACTIVE)
    try:
        if not overlay.wait_ready(timeout=8.0):
            print(f"首帧未就绪：{overlay.last_error!r}")
            return 1
        time.sleep(0.8)

        print("取对比帧（覆盖层 ACTIVE）…")
        after = grab()
        if after is None:
            print("取帧失败 —— 本条**未验证**")
            return 1
        if after.shape != before.shape:
            print(f"两帧尺寸不一致 {before.shape} vs {after.shape}")
            return 1

        delta = (after.astype("int16") - before.astype("int16"))[:, :, :3]
        changed = np.abs(delta).mean(axis=2) > 8
        ys, xs = np.nonzero(changed)
        print(f"\n发生变化的像素 {int(changed.sum())} 个")
        if len(xs):
            print(f"变化区域的包围盒 x=[{xs.min()}, {xs.max()}] "
                  f"y=[{ys.min()}, {ys.max()}]  （整屏是 x=[0,{width-1}] y=[0,{height-1}]）")

        band = 96
        print("\n各区域的平均像素差（BGRA 前 3 通道）:")
        for label, box in (
            ("上边带（应当有光晕）", (0, 0, width, band)),
            ("下边带（应当有光晕，较淡）", (0, height - band, width, band)),
            ("左边带（应当有光晕）", (0, 0, band, height)),
            ("右边带（应当有光晕）", (width - band, 0, band, height)),
            ("左上 480x200（没放大就只有这里变）", (0, 0, 480, 200)),
            ("胶囊位置", (1539, 48, 361, 83)),
            ("目标框带（含 24px 外发光）", (TARGET[0] - 30, TARGET[1] - 30,
                                          TARGET[2] + 60, TARGET[3] + 60)),
            ("光标位置", (CURSOR[0] - 26, CURSOR[1] - 26, 52, 52)),
        ):
            print(f"  {label:<38} {region_diff(before, after, *box):8.3f}")

        left_band = region_diff(before, after, 0, 400, band, height - 800)
        right_band = region_diff(before, after, width - band, 400, band, height - 800)
        spread = min(left_band, right_band) > 1.0
        print(f"\n左侧带中下部 {left_band:.3f} · 右侧带中下部 {right_band:.3f}")
        print(f"结论：光晕{'铺满全屏（放大生效）' if spread else '**没有铺满** —— 放大没生效'}")

        out = Path(__file__).resolve().parent / "out" / "overlay-onscreen.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        rgb = after[:, :, [2, 1, 0]].astype("uint8")
        write_png(out, rgb.tobytes(), width, height)
        print(f"\n对比帧已存（WDA 关掉时截到的真实屏幕内容）：{out}")
    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
