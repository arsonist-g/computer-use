"""控制覆盖层 —— 五态状态机 + 分层窗口渲染（DEC-027 / DEC-030 / DEC-031）。

它同时是两件事：

1. **给用户的视觉信号**：AI 正在操作这台电脑，以及怎么中止。
2. **输入封锁的被控对象**：封锁区间 = 覆盖层可见区间（Arming 起、Off 止）。

窗口必须是 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`（逐像素 alpha、
点击穿透、不抢焦点），并设 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
让它在**所有**截图管线里消失（DEC-027，spike S2b 已实测对 WGC 与 DXGI 都生效）。

视觉实现遵守 DEC-031 的三条硬契约：
  - **没有「边框」这个对象** —— 只有一条从边缘向内长距离羽化到完全透明的渐变。
  - **尺寸按屏幕短边占比** —— 所有几何量由 `_REACH_RATIO` 推出，不写死像素。
  - **非线性衰减，宁透勿浓** —— 线性衰减会在终点留下可感知的内边界，读作色块而不是光。

渲染用 ctypes + GDI 32 位 DIB，**不用 numpy**：覆盖层跑在 daemon 里，但保持一致
（base 环境不引入数值库）能省掉一整类「哪天有人把覆盖层 import 进客户端」的风险。
代价是逐像素计算慢，因此先画到降采样的缓冲再交给 `UpdateLayeredWindow` 放大 ——
光晕本来就该是糊的，放大带来的柔化与设计意图同向。
"""

from __future__ import annotations

import colorsys
import ctypes
import math
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum

from ..errors import CUError, ErrorCode
from . import win32 as w

#: 光晕延伸深度 / 屏幕短边。DEC-031 第 8 轮定稿 24cqmin。
_REACH_RATIO = 0.24
#: 整体不透明度。以**暗色桌面**的观感为准，宁低勿高（曾用 0.82，太浓）。
_GLOW_OPACITY = 0.55
#: 衰减曲线指数：>1 让靠边处最浓、向外迅速溶开，末段极缓归零（非线性，DEC-031 第 4 条）。
_FALLOFF_EXPONENT = 2.6
#: 竖向衰减系数（顶部最浓 → 底部更弱）。
_FALLOFF_VERTICAL = 0.45

#: 降采样后的渲染宽度。光晕是低频信号，降采样 + 放大与设计意图同向。
_RENDER_WIDTH = 480
#: 光谱流速：Arming 快，Active 慢（DEC-030 的 2s / 7s 一圈）。
_SPIN_SECONDS = {"arming": 2.0, "active": 7.0}

#: 目标框的 2px 实线环与 24px 外发光（MASTER §2.5 的 `--shadow-target`）。
#: 这两个**刻意用 px 而不是比例**：它们是元素高亮的描边，与屏幕尺寸无关 ——
#: 写死才能保证 1080p 与 4K 上看起来是同一条线。
_TARGET_RING_PX = 2
_TARGET_GLOW_PX = 24
#: 光标光晕 52px（overlay.md §3.1）。同样刻意用 px，理由见 MASTER §2.5 的例外②：
#: 系统光标的像素尺寸由 OS 与 DPI 决定，不随分辨率放大，否则 4K 上会变成巨大箭头。
_CURSOR_GLOW_PX = 52

#: 胶囊尺寸按字体走，不按屏幕（DEC-031 第 2 条约定的例外）。
_PILL_HEIGHT_RATIO = 0.022
_PILL_MIN_HEIGHT = 22
_PILL_MAX_HEIGHT = 40
_PILL_FONT_RATIO = 0.55


class OverlayState(StrEnum):
    OFF = "off"
    ARMING = "arming"
    ACTIVE = "active"
    STOPPING = "stopping"
    ERROR = "error"


#: 每态的胶囊文案与圆点色相（overlay.md §2.1 的状态表，逐字）。
_PILL_TEXT = {
    OverlayState.ARMING: "AI is using your computer · [Esc] to cancel",
    OverlayState.ACTIVE: "AI is using your computer · [Esc] to cancel",
    OverlayState.STOPPING: "Stopping",
    OverlayState.ERROR: "Something went wrong · [Esc] to dismiss",
}
_PILL_HUE = {
    OverlayState.ARMING: 0.12,
    OverlayState.ACTIVE: 0.12,
    OverlayState.STOPPING: 0.09,
    OverlayState.ERROR: 0.0,
}
#: Stopping / Error 的光谱**冻结**（不流动），且颜色固定。
_FROZEN_HUE = {OverlayState.STOPPING: 0.09, OverlayState.ERROR: 0.0}

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST = 0x00000008
_WS_POPUP = 0x80000000
_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_BI_RGB = 0
_DIB_RGB_COLORS = 0
#: WDA 需要 Win10 2004 (build 19041) 以上（DEC-027 已核实的限制）。
_WDA_MIN_BUILD = 19041


@dataclass
class _Screen:
    x: int
    y: int
    width: int
    height: int
    primary_width: int
    primary_height: int

    @property
    def short_side(self) -> int:
        return min(self.primary_width, self.primary_height) or 1

    @property
    def reach(self) -> int:
        return max(24, int(self.short_side * _REACH_RATIO))


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def win10_build() -> int:
    """当前 Windows 的 build 号。取不到返回 0（视为过旧，不设 WDA）。"""
    try:
        version = w.kernel32.RtlGetVersion
    except AttributeError:
        return 0
    version.restype = ctypes.c_long
    version.argtypes = [ctypes.c_void_p]

    class OSVERSIONINFOW(ctypes.Structure):
        _fields_ = [("dwOSVersionInfoSize", wintypes.DWORD),
                    ("dwMajorVersion", wintypes.DWORD),
                    ("dwMinorVersion", wintypes.DWORD),
                    ("dwBuildNumber", wintypes.DWORD),
                    ("dwPlatformId", wintypes.DWORD),
                    ("szCSDVersion", wintypes.WCHAR * 128)]

    info = OSVERSIONINFOW()
    info.dwOSVersionInfoSize = ctypes.sizeof(OSVERSIONINFOW)
    return int(info.dwBuildNumber) if version(ctypes.byref(info)) == 0 else 0


def capture_exclusion_supported() -> bool:
    """能否安全地设 `WDA_EXCLUDEFROMCAPTURE`。

    **必须做这个检查**（DEC-027）：低于 19041 的版本会退化为 `WDA_MONITOR`，
    那是**黑块** —— 比不排除更糟。不满足时宁可不设，接受截图被污染。
    """
    return win10_build() == 0 or win10_build() >= _WDA_MIN_BUILD


class ControlOverlay:
    """覆盖层窗口。**必须与 daemon 同生命周期**（崩溃即安全，架构 §1.5 第 5 条）。"""

    def __init__(self, on_abort=None) -> None:
        self.on_abort = on_abort
        self._hwnd: int | None = None
        self._screen: _Screen | None = None
        self._state = OverlayState.OFF
        self._state_since = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target: tuple[int, int, int, int] | None = None
        self._cursor: tuple[int, int] | None = None
        self._wda_applied = False
        # 缓冲区复用：每帧重建 3MB 的 ctypes buffer 会白白制造 GC 压力。
        self._buffer = None
        self._bitmap = None
        self._memdc = None
        #: 建表面时被顶替掉的那个 DC 自带位图 —— 释放时必须先选回去。
        self._old_bitmap = None
        self._buffer_size = (0, 0)

    # ---- 状态机 ----

    @property
    def state(self) -> OverlayState:
        return self._state

    @property
    def visible(self) -> bool:
        return self._state is not OverlayState.OFF

    def transition(self, state: OverlayState) -> None:
        if state == self._state:
            return
        self._state = state
        self._state_since = time.monotonic()
        if state is OverlayState.OFF:
            self._hide()
        else:
            self._ensure_window()
            self._show()

    def set_target(self, rect: tuple[int, int, int, int] | None) -> None:
        """目标元素高亮框。**只在 Active 显示** —— 出错或中止时 AI 已不在操作任何元素。"""
        self._target = rect

    def set_cursor(self, point: tuple[int, int] | None) -> None:
        self._cursor = point

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._render_loop, name="cu-overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._hide()
        self._destroy()

    def _render_loop(self) -> None:
        while not self._stop.wait(1.0 / 12.0):     # ~12fps：光晕是低频信号，够用
            if not self.visible or self._hwnd is None:
                continue
            try:
                self._render()
            except Exception:  # noqa: BLE001 —— 渲染失败不能影响输入封锁
                continue

    # ---- 窗口 ----

    def _ensure_window(self) -> None:
        if self._hwnd is not None:
            return
        self._screen = _read_screen()
        user32 = w.user32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
            wintypes.HDC, ctypes.POINTER(_POINT), wintypes.DWORD,
            ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.UINT]

        ex_style = (_WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
                    | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST)
        hwnd = user32.CreateWindowExW(
            ex_style, "STATIC", "Computer-Use Overlay", _WS_POPUP,
            self._screen.x, self._screen.y, self._screen.width, self._screen.height,
            None, None, None, None)
        if not hwnd:
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"覆盖层窗口创建失败 err={w.kernel32.GetLastError()}")
        self._hwnd = int(hwnd)

        if capture_exclusion_supported():
            # 对**所有**截图管线不可见（DEC-027）。spike S2b 已实测对 WGC 与 DXGI 都生效。
            self._wda_applied = bool(user32.SetWindowDisplayAffinity(
                self._hwnd, w.WDA_EXCLUDEFROMCAPTURE))
        else:
            # 过旧的系统上 WDA_EXCLUDEFROMCAPTURE 会退化成 WDA_MONITOR（黑块），
            # 比不排除更糟 —— 明确不设，并接受截图被污染。
            self._wda_applied = False

        user32.ShowWindow(self._hwnd, 4)      # SW_SHOWNOACTIVATE
        # 置顶且不激活：TOPMOST + NOACTIVATE 的组合让覆盖层永远在画面最上层，
        # 但绝不抢走目标窗口的焦点。
        user32.SetWindowPos(self._hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def _destroy(self) -> None:
        with self._lock:
            if self._hwnd is not None:
                w.user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            self._release_surface()

    def _show(self) -> None:
        if self._hwnd is not None:
            w.user32.ShowWindow(self._hwnd, 4)

    def _hide(self) -> None:
        if self._hwnd is not None:
            w.user32.ShowWindow(self._hwnd, 0)

    # ---- 渲染 ----

    def _render(self) -> None:
        screen = self._screen
        assert screen is not None
        # 降采样：宽度压到 _RENDER_WIDTH，高度按比例。光晕是低频信号，
        # 放大回来只会更柔 —— 与「无硬边界」的设计意图同向。
        width = min(_RENDER_WIDTH, screen.width)
        height = max(1, int(screen.height * width / screen.width))
        pixels = self._build_pixels(width, height)
        self._blit(pixels, width, height)

    def _build_pixels(self, width: int, height: int) -> bytearray:
        """逐像素生成 BGRA。返回的缓冲是 `height * width * 4` 字节。

        只算**靠近边缘**的像素：距离 >= reach 的地方 alpha 为 0，直接跳过。
        这一步把 4K 全屏的 830 万像素压到实际需要计算的那一圈，
        是在不引入数值库的前提下让纯 Python 渲染可行的关键。
        """
        screen = self._screen
        assert screen is not None
        state = self._state
        frozen = _FROZEN_HUE.get(state)
        spin = _SPIN_SECONDS.get(str(state), 7.0)
        angle0 = (((time.monotonic() - self._state_since) / spin) % 1.0
                  if frozen is None else frozen)

        buffer = bytearray(width * height * 4)
        cx, cy = width / 2.0, height / 2.0
        reach = max(6.0, min(width, height) * _REACH_RATIO)
        two_pi = 2.0 * math.pi
        hsv_to_rgb = colorsys.hsv_to_rgb
        alpha_scale = _GLOW_OPACITY * 255.0

        for y in range(height):
            dy = abs((y + 0.5) - cy)
            # 竖向衰减：顶部最浓 → 底部更弱（外层单层 mask 的等价物）。
            vertical = 1.0 - (y / max(1, height - 1)) * (1.0 - _FALLOFF_VERTICAL)
            row = y * width * 4
            for x in range(width):
                distance = min(abs((x + 0.5) - cx), dy)
                if distance >= reach:
                    continue
                # 非线性衰减：靠边最浓、向外迅速溶开、末段极缓归零。
                # 线性衰减必然在终点留下可感知的内边界（DEC-031 第 4 条）。
                t = 1.0 - distance / reach
                value = int(t ** _FALLOFF_EXPONENT * alpha_scale * vertical)
                if value <= 1:
                    continue
                hue = (math.atan2((y + 0.5) - cy, (x + 0.5) - cx) / two_pi + angle0) % 1.0
                red, green, blue = hsv_to_rgb(hue, 0.85, 1.0)
                index = row + x * 4
                buffer[index] = int(blue * value)
                buffer[index + 1] = int(green * value)
                buffer[index + 2] = int(red * value)
                buffer[index + 3] = value

        # 叠放顺序：边缘光晕（已画） → 目标框 → 光标光晕 → 胶囊。
        # 胶囊在最上，因为它是状态提示，不该被任何东西遮住。
        self._draw_target(buffer, width, height)
        self._draw_cursor(buffer, width, height)
        self._draw_pill(buffer, width, height)
        return buffer

    def _draw_target(self, buffer: bytearray, width: int, height: int) -> None:
        """目标元素高亮框：`0 0 0 2px <target色>` + `0 0 24px <target光晕>`（MASTER §2.5）。

        **只在 Active 态显示** —— 出错或中止时 AI 已不在操作任何元素，
        保留它会让用户以为操作还在进行（overlay.md §2.1）。

        它是**单一琥珀色相**，不取光谱（MASTER §7 的 AVOID）：
        「目标在哪」与「AI 在活动」是两个语义，混用会让用户分不清。
        """
        if self._target is None or self.state is not OverlayState.ACTIVE:
            return
        screen = self._screen
        assert screen is not None
        # 屏幕绝对坐标 → 缓冲坐标（缓冲是降采样的）。
        sx = width / screen.width
        sy = height / screen.height
        tx, ty, tw, th = self._target
        left = int(tx * sx)
        top = int(ty * sy)
        right = int((tx + tw) * sx)
        bottom = int((ty + th) * sy)
        if right <= left or bottom <= top:
            return

        # 琥珀：oklch(75% 0.17 60) 转成 sRGB 约 #F0A24B 一档。
        ring = (75, 162, 240)          # BGRA
        glow = (75, 162, 240)
        glow_reach = max(3, int(_TARGET_GLOW_PX * min(sx, sy)))

        # 外发光：环外一圈按距离衰减。
        for y in range(max(0, top - glow_reach), min(height, bottom + glow_reach)):
            for x in range(max(0, left - glow_reach), min(width, right + glow_reach)):
                # 到矩形环的距离（在框内则为 0）。
                dx = max(left - x, 0, x - right)
                dy = max(top - y, 0, y - bottom)
                distance = (dx * dx + dy * dy) ** 0.5
                if distance > glow_reach:
                    continue
                alpha = int((1.0 - distance / glow_reach) ** 2 * 255 * 0.45)
                if alpha <= 2:
                    continue
                _blend(buffer, width, x, y, glow, alpha)

        # 2px 实线环。MASTER 明确要求它是实线（与边缘光晕的「无硬边界」不冲突：
        # 那是背景光，这是元素高亮，两回事）。
        for offset in range(_TARGET_RING_PX):
            for x in range(max(0, left), min(width, right + 1)):
                for y in (top + offset, bottom - offset):
                    if 0 <= y < height:
                        _blend(buffer, width, x, y, ring, 255)
            for y in range(max(0, top), min(height, bottom + 1)):
                for x in (left + offset, right - offset):
                    if 0 <= x < width:
                        _blend(buffer, width, x, y, ring, 255)

    def _draw_cursor(self, buffer: bytearray, width: int, height: int) -> None:
        """光标光晕：52px 径向渐变（overlay.md §3.1 第 1 层）。

        **只画光晕，不替换系统光标。** 规范里「替换光标」是第 2 层，且带一条
        降级路径：「若隐藏系统光标出现闪烁或不稳定，退回只画光晕 + 保留系统光标」。
        Q-019 至今没验过隐藏光标稳不稳，而这里用的是 `UpdateLayeredWindow` ——
        要画自绘箭头就得把**系统光标**藏掉，那是个全局副作用。
        先走已验证的降级路径，把不确定的那一步留到真机验证之后。

        光晕的意义是让光标在任意背景上都可见：浅色背景靠箭头本身，深色背景靠光晕。
        """
        if self._cursor is None or self.state is not OverlayState.ACTIVE:
            return
        screen = self._screen
        assert screen is not None
        sx = width / screen.width
        sy = height / screen.height
        cx = int(self._cursor[0] * sx)
        cy = int(self._cursor[1] * sy)
        radius = max(4, int(_CURSOR_GLOW_PX / 2 * min(sx, sy)))

        for y in range(max(0, cy - radius), min(height, cy + radius + 1)):
            for x in range(max(0, cx - radius), min(width, cx + radius + 1)):
                distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if distance > radius:
                    continue
                # 径向渐变：中心最亮，边缘归零。非线性，与边缘光晕同一条原则。
                alpha = int((1.0 - distance / radius) ** 2.2 * 255 * 0.85)
                if alpha <= 2:
                    continue
                _blend(buffer, width, x, y, (255, 255, 255), alpha)

    def _draw_pill(self, buffer: bytearray, width: int, height: int) -> None:
        """顶部胶囊：**纯深色实体胶囊 + 文字，无光晕、无描边**（DEC-031 第 3 条）。

        它坐在顶部光晕之上，本身不发光。深色桌面上靠一层 1px 浅色扩散环分离。
        """
        text = _PILL_TEXT.get(self._state)
        if not text:
            return
        pill_height = max(_PILL_MIN_HEIGHT, min(_PILL_MAX_HEIGHT,
                                               int(height * _PILL_HEIGHT_RATIO * 4)))
        # 按中文/英文混合估算宽度：CJK 计 1.0 字宽，其余 0.6。
        units = sum(1.0 if ord(ch) > 0x2E80 else 0.6 for ch in text)
        pill_width = min(width - 8, int(units * pill_height * _PILL_FONT_RATIO))
        left = (width - pill_width) // 2
        top = max(2, int(height * 0.012))
        radius = pill_height // 2

        for y in range(top, min(height, top + pill_height)):
            for x in range(left, min(width, left + pill_width)):
                # 圆角：距左右端不足一个半径时按圆裁。
                corner_x = min(x - left, left + pill_width - 1 - x)
                if corner_x < radius:
                    dy = abs(y - (top + radius))
                    if dy > radius:
                        continue
                    if corner_x * corner_x + (radius - dy) * (radius - dy) > radius * radius:
                        continue
                edge = (corner_x <= 1 or y in (top, top + pill_height - 1))
                index = (y * width + x) * 4
                # 1px 浅色扩散环：深色桌面上把胶囊与背景分开的唯一手段。
                if edge:
                    buffer[index], buffer[index + 1], buffer[index + 2], buffer[index + 3] = (
                        140, 140, 140, 255)
                else:
                    # 深色实体：近似 #1C1C1E，不透明。
                    buffer[index], buffer[index + 1], buffer[index + 2], buffer[index + 3] = (
                        34, 28, 30, 255)

        self._draw_text(buffer, width, height, text, left, top, pill_width, pill_height)

    def _draw_text(self, buffer: bytearray, width: int, height: int, text: str,
                   left: int, top: int, pill_width: int, pill_height: int) -> None:
        """把一个单像素字形表按比例放大画上去。

        用 5×7 点阵而不是 GDI 的 DrawText：`UpdateLayeredWindow` 走的是屏幕外 DC，
        在那上面画字需要再选字体、再 BitBlt 回来，路径长且易错；而我们本来就只是
        在降采样缓冲上作画，点阵放大后的效果与整体风格一致。
        """
        scale = max(1, pill_height // 9)
        cell = 6 * scale
        glyph_width = int(sum(6 * (1.0 if ord(ch) > 0x2E80 else 0.75) for ch in text) * scale)
        x = left + max(2, (pill_width - glyph_width) // 2)
        y = top + max(1, (pill_height - 7 * scale) // 2)
        for ch in text:
            rows = _GLYPH_5X7.get(ch) or _GLYPH_5X7.get(ch.upper()) or _GLYPH_5X7["?"]
            if ch == " ":
                x += int(cell * 0.75)
                continue
            advance = int(cell * (1.0 if ord(ch) > 0x2E80 else 0.75))
            for row_index, row_bits in enumerate(rows):
                for col_index in range(5):
                    if not (row_bits >> (4 - col_index)) & 1:
                        continue
                    for sy in range(scale):
                        py = y + row_index * scale + sy
                        if py >= height:
                            continue
                        for sx in range(scale):
                            px = x + col_index * scale + sx
                            if px >= width:
                                continue
                            index = (py * width + px) * 4
                            buffer[index], buffer[index + 1], buffer[index + 2], buffer[index + 3] = (
                                245, 245, 245, 255)
            x += advance

    def _blit(self, pixels: bytearray, width: int, height: int) -> None:
        screen = self._screen
        assert screen is not None
        if (width, height) != self._buffer_size:
            self._release_surface()
            screen_dc = w.user32.GetDC(None)
            self._memdc = w.gdi32.CreateCompatibleDC(screen_dc)
            self._bitmap = w.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            w.user32.ReleaseDC(None, screen_dc)
            if not self._memdc or not self._bitmap:
                raise CUError(ErrorCode.INTERNAL_ERROR,
                              f"覆盖层离屏表面创建失败 err={w.kernel32.GetLastError()}")
            # **必须把位图选进 DC。** 不选的话，DC 里还是它自带的 1×1 单色位图：
            # `SetDIBits` 写进一个不属于任何 DC 的位图，`UpdateLayeredWindow`
            # 拿到的 DC 里只有那个 1×1 —— 窗口整个是空的，且返回 `ERROR_GEN_FAILURE`。
            # 这一行漏掉时，症状是「覆盖层完全不可见」，排查了很久才落到这里。
            self._old_bitmap = w.gdi32.SelectObject(self._memdc, self._bitmap)
            self._buffer_size = (width, height)
            self._buffer = ctypes.create_string_buffer(width * height * 4)

        ctypes.memmove(self._buffer, bytes(pixels), width * height * 4)
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height        # 负数 = 自上而下，与我们的行序一致
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB

        gdi32 = w.gdi32
        gdi32.SetDIBits.restype = ctypes.c_int
        gdi32.SetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT]

        if not gdi32.SetDIBits(self._memdc, self._bitmap, 0, height, self._buffer,
                               ctypes.byref(info), _DIB_RGB_COLORS):
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"SetDIBits 失败 err={w.kernel32.GetLastError()}")

        # 降采样缓冲放大回全屏。插值交给 ULW 的拉伸 —— HALFTONE 更柔，
        # 与「光晕没有硬边界」的设计一致。设失败不影响正确性，故不检查。
        gdi32.SetStretchBltMode.restype = ctypes.c_int
        gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetStretchBltMode(self._memdc, 4)

        source = _POINT(0, 0)
        size = _SIZE(screen.width, screen.height)
        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        w.kernel32.SetLastError(0)
        ok = w.user32.UpdateLayeredWindow(
            self._hwnd, None, ctypes.byref(_POINT(screen.x, screen.y)), ctypes.byref(size),
            self._memdc, ctypes.byref(source), 0, ctypes.byref(blend), _ULW_ALPHA)
        if not ok:
            # 静默失败在这里的后果是「覆盖层看不见，但代码看不出问题」——
            # 必须抛出来（架构 §2.5：诊断日志要能回答「覆盖层为何不可见」）。
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"UpdateLayeredWindow 失败 err={w.kernel32.GetLastError()}")

    def _release_surface(self) -> None:
        """释放离屏表面。先把旧位图选回去，再删位图 —— 顺序反了删不掉。"""
        if self._memdc and self._old_bitmap:
            w.gdi32.SelectObject(self._memdc, self._old_bitmap)
            self._old_bitmap = None
        if self._bitmap:
            w.gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if self._memdc:
            w.gdi32.DeleteDC(self._memdc)
            self._memdc = None
        self._buffer = None
        self._buffer_size = (0, 0)


def _blend(buffer: bytearray, width: int, x: int, y: int,
           bgr: tuple[int, int, int], alpha: int) -> None:
    """把一个半透明像素**叠加**到缓冲上（alpha 混合，不是覆盖）。

    覆盖会把缓冲里已有的边缘光晕擦掉；目标框与光标都画在光晕之上，
    必须叠加。alpha 越高越接近纯色。
    """
    index = (y * width + x) * 4
    inv = 255 - alpha
    for channel in range(3):
        existing = buffer[index + channel]
        buffer[index + channel] = (bgr[channel] * alpha + existing * inv) // 255
    buffer[index + 3] = max(buffer[index + 3], alpha)


def _read_screen() -> _Screen:
    user32 = w.user32
    return _Screen(
        x=user32.GetSystemMetrics(76),
        y=user32.GetSystemMetrics(77),
        width=user32.GetSystemMetrics(78),
        height=user32.GetSystemMetrics(79),
        primary_width=user32.GetSystemMetrics(0),
        primary_height=user32.GetSystemMetrics(1),
    )


#: 5×7 点阵字形。只收录胶囊文案用得到的字符 —— 这是一个状态提示，不是文本渲染器。
_GLYPH_5X7: dict[str, tuple[int, ...]] = {
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b11110, 0b10001, 0b10001, 0b10001, 0b11110),
    "C": (0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110),
    "D": (0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110),
    "E": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000, 0b11111),
    "G": (0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111),
    "H": (0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "I": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b11111),
    "K": (0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "F": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000, 0b10000),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    "0": (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111),
    "3": (0b11111, 0b00010, 0b00100, 0b00010, 0b00001, 0b10001, 0b01110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),
    "-": (0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000),
    ".": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b01100, 0b01100),
    "·": (0b00000, 0b00000, 0b00000, 0b00100, 0b00000, 0b00000, 0b00000),
    "[": (0b00110, 0b01000, 0b01000, 0b01000, 0b01000, 0b01000, 0b00110),
    "]": (0b01100, 0b00010, 0b00010, 0b00010, 0b00010, 0b00010, 0b01100),
    "?": (0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b00000, 0b00100),
}
