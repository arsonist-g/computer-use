"""控制覆盖层 —— 五态状态机 + 分层窗口渲染（DEC-027 / DEC-030 / DEC-031）。

它同时是两件事：

1. **给用户的视觉信号**：AI 正在操作这台电脑，以及怎么中止。
2. **输入封锁的被控对象**：封锁区间 = 覆盖层可见区间（Arming 起、Off 止）。

窗口是 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`（逐像素 alpha、
点击穿透、不抢焦点），并设 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
让它在**所有**截图管线里消失（DEC-027，spike S2b 已实测对 WGC 与 DXGI 都生效）。

## 两层表面

规范（MASTER §2.5）把尺寸分成两类，实现必须跟着分：

- **按屏幕短边占比**的只有边缘光晕。它是低频信号，可以先画到降采样缓冲
  （480px 宽）再放大上屏 —— 放大带来的柔化与「无硬边界」的设计意图同向。
- **刻意写死 px** 的是胶囊、目标框的描边与外发光、光标光晕。它们**必须在原生
  分辨率上画**：降采样缓冲要放大 7 倍才上屏，2px 描边在里面只占 0.28 个像素，
  根本表示不出来，只会被放大成一条 14px 的粗线。

所以这里有四个表面：一个全屏的低频光晕表面（降采样，每帧重画以驱动光谱流动），
加三个原生分辨率的小表面（胶囊 / 目标框 / 光标光晕，只在内容变化时重画）。

## 窗口归渲染线程所有

Win32 把窗口消息投递给**创建它的线程**，属主线程必须抽消息队列。不抽的话，
窗口一旦收到任何一条消息，5 秒后就被判定「无响应」（AppHangB1），Windows 会
弹出「程序已停止工作」并结束进程 —— 这不是理论：实测把窗口建在主线程上、
主线程随后阻塞在等输入里，`IsHungAppWindow` 恰好在第 5.0 秒翻成 True。

因此建窗口、显示、改尺寸、上屏、销毁**全部只能在渲染线程上做**；`transition()`
只写状态，其余交给渲染线程。这也意味着 `transition()` 不再同步返回渲染结果 ——
要确认「真的画上去了」，用 `wait_ready()`。
"""

from __future__ import annotations

import colorsys
import ctypes
import math
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum

from ..errors import CUError, ErrorCode
from . import win32 as w


class OverlayState(StrEnum):
    OFF = "off"
    ARMING = "arming"
    ACTIVE = "active"
    STOPPING = "stopping"
    ERROR = "error"


# ---------------------------------------------------------------------------
# 光晕（按屏幕短边占比 —— 唯一一类该用比例的尺寸）
# ---------------------------------------------------------------------------

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
#: 光谱流速（秒/圈）。**武装期与活动期同速** —— 原设计让武装期 2s、活动期 7s，
#: 用流速本身区分阶段；但真机上快流读起来是躁动而不是信息，而阶段已经由
#: 「有没有琥珀目标框」和胶囊文案表达清楚了。多一倍转速换不来等价的收益。
_SPIN_SECONDS = 7.0

# ---------------------------------------------------------------------------
# 原生分辨率元素（一律按**屏幕像素**，不随分辨率放大 —— MASTER §2.5）
# ---------------------------------------------------------------------------

#: 目标框：`0 0 0 2px <目标色>` + `0 0 24px <目标色光晕>`（MASTER §2.5 的 `--shadow-target`）。
_TARGET_RING_PX = 2
_TARGET_GLOW_PX = 24
#: 光标光晕 52px（overlay.md §3.1）。理由见 §2.5 的例外②：系统光标的像素尺寸
#: 由 OS 与 DPI 决定，不随分辨率放大，否则 4K 上会变成巨大箭头。
_CURSOR_GLOW_PX = 52

#: 胶囊。原设计（MASTER §2.5 例外①）让字号与屏幕无关地固定 14px，理由是
#: 「跟随字体尺寸」；但真机上那在 1440p 下明显偏小，改为**按屏幕短边缩放**。
#: 其余尺寸（内边距、圆角、圆点、键帽）一律由字号按 `_PILL_BASE_FONT_PX` 等比推出 ——
#: 整颗胶囊的内部比例与设计稿一致，只是整体放大或缩小。
_PILL_TOP_RATIO = 0.045        # --pill-top: 4.5cqmin
_PILL_FONT_RATIO = 0.0153      # 字号 / 屏幕短边（1440 → 22px）
_PILL_FONT_MIN = 13            # 1080p 及以下别太小
_PILL_FONT_MAX = 40            # 5K 及以上别太大
_PILL_BASE_FONT_PX = 14        # 设计稿字号；下面这些常量都以它为基准
_PILL_KEY_FONT_PX = 13         # --text-key
_PILL_PAD_Y = 8                # --space-2
_PILL_PAD_X = 16               # --space-4
_PILL_GAP = 8                  # --space-2
_PILL_DOT_PX = 7
_PILL_RING_PX = 1              # --shadow-pill 第 1 层：1px 浅色扩散环
_PILL_KEY_PAD_X = 4
_PILL_KEY_PAD_Y = 1
_PILL_KEY_RADIUS = 4

#: `--shadow-pill` 的两层柔影：`(偏移 y, 模糊半径, alpha)`。
#: 它们与那 1px 扩散环同属一个 token，一起实现才叫「按规范画」。
_PILL_SHADOWS = ((8, 24, 0.5), (2, 6, 0.4))
#: 表面缓冲要留出的边距：柔影会溢出胶囊本身，溢出多少由上面的 token 决定。
_PILL_MARGIN = (24, 16, 32)    # 左/右, 上, 下

#: 规范色（oklch → sRGB **精确换算**，不写近似值）。BGRA 顺序。
_PILL_SURFACE = (36, 27, 22)        # oklch(22% 0.02 265)  -> #161B24
_PILL_SURFACE_ALPHA = 0.92          # oklch(... / 0.92)
_PILL_TEXT = (252, 248, 247)        # oklch(98% 0.005 265) -> #F7F8FC
_PILL_DIM = (190, 183, 180)         # oklch(78% 0.01 265)  -> #B4B7BE
_PILL_RING_ALPHA = 0.14             # oklch(100% 0 0 / 0.14)
_PILL_KEY_BORDER_ALPHA = 0.22       # oklch(100% 0 0 / 0.22)
_TARGET_COLOR = (25, 143, 250)      # oklch(75% 0.17 60)   -> #FA8F19
_CURSOR_COLOR = (255, 255, 255)

#: 胶囊内容：`(主文案, 键名, 尾文案)`。键名为 None 时不画键帽。
#: 完整文案（供对照 overlay.md §2.1 的状态表）：
#:   Arming / Active  `● AI is using your computer · [Esc] to cancel`
#:   Stopping         `● Stopping`
#:   Error            `● Something went wrong · [Esc] to dismiss`
_PILL_CONTENT = {
    OverlayState.ARMING: ("AI is using your computer", "Esc", "to cancel"),
    OverlayState.ACTIVE: ("AI is using your computer", "Esc", "to cancel"),
    OverlayState.STOPPING: ("Stopping", None, None),
    OverlayState.ERROR: ("Something went wrong", "Esc", "to dismiss"),
}
#: 圆点色相（`--state-hue`）。Stopping 用暂停色，Error 用错误色。
_PILL_HUE = {
    OverlayState.ARMING: 0.12,
    OverlayState.ACTIVE: 0.12,
    OverlayState.STOPPING: 0.09,
    OverlayState.ERROR: 0.0,
}
#: Stopping / Error 的光谱**冻结**（不流动），且颜色固定。
_FROZEN_HUE = {OverlayState.STOPPING: 0.09, OverlayState.ERROR: 0.0}

#: WDA 需要 Win10 2004 (build 19041) 以上（DEC-027 已核实的限制）。
_WDA_MIN_BUILD = 19041

#: 渲染帧间隔（~12fps）。光晕是低频信号，够用。
_FRAME_SECONDS = 1.0 / 12.0

_EX_STYLE = (w.WS_EX_LAYERED | w.WS_EX_TRANSPARENT | w.WS_EX_NOACTIVATE
             | w.WS_EX_TOOLWINDOW | w.WS_EX_TOPMOST)


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


@dataclass(frozen=True)
class _TextRun:
    """胶囊里的一段文字：画在哪个像素、占多大、什么颜色。"""

    size: int
    x: int
    y: int
    width: int
    height: int
    text: str
    color: tuple[int, int, int]


@dataclass
class _Detail:
    """一个原生分辨率小表面的内容：像素 + 它该放在屏幕的哪里。"""

    pixels: bytearray
    x: int
    y: int
    width: int
    height: int


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


def _bitmap_info(width: int, height: int) -> _BITMAPINFO:
    """32 位自下而上/自上而下的 DIB 头。负高度 = 自上而下，与我们的行序一致。"""
    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = w.BI_RGB
    return info


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


def _pump_messages() -> None:
    """抽干**本线程**的消息队列。

    **这不是可选项。** 窗口归本线程所有，Win32 把它的消息投到这里；不抽的话，
    窗口收到任何一条消息后 5 秒就被判定「无响应」（AppHangB1），Windows 会弹
    「程序已停止工作」并结束进程 —— 实测确认过这条路径。
    """
    msg = w.MSG()
    while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, w.PM_REMOVE):
        w.user32.TranslateMessage(ctypes.byref(msg))
        w.user32.DispatchMessageW(ctypes.byref(msg))


class _LayeredSurface:
    """一个 `UpdateLayeredWindow` 分层窗口 + 它的离屏 DIB 表面。

    三条纪律：

    1. **窗口归创建它的线程所有**，所有操作都只能在那个线程上做（见模块文档）。
    2. 逐像素 alpha 一律走 `UpdateLayeredWindow`，且**源位图必须与窗口同尺寸** ——
       `UpdateLayeredWindow` 不会替你拉伸：源比 `psize` 小时它什么都不画
       （实测：整屏光晕窗口配 480x200 的缓冲，屏幕上四条边干干净净，一个像素都没有）。
       所以缓冲比窗口小时，这一步由 `blit()` 用 `StretchBlt` 补上。
    3. 缓冲区尺寸或窗口矩形变了才重建对应的表面。
    """

    def __init__(self, title: str) -> None:
        self._title = title
        self._hwnd: int | None = None
        self._rect: tuple[int, int, int, int] | None = None
        #: 渲染缓冲那一路（可能小于窗口）。
        self._src = None
        self._src_size = (0, 0)
        #: 摊平到窗口尺寸那一路（只在缓冲小于窗口时才建）。
        self._dst = None
        self._dst_size = (0, 0)
        self._shown = False
        self._wda = False

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    @property
    def shown(self) -> bool:
        return self._shown

    @property
    def excl_from_capture(self) -> bool:
        return self._wda

    def ensure(self) -> None:
        """建窗口（只建一次）。必须在属主线程上调用。"""
        if self._hwnd is not None:
            return
        hwnd = w.user32.CreateWindowExW(_EX_STYLE, "STATIC", self._title, w.WS_POPUP,
                                        0, 0, 1, 1, None, None, None, None)
        if not hwnd:
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"覆盖层窗口创建失败[{self._title}] "
                          f"err={w.kernel32.GetLastError()}")
        self._hwnd = int(hwnd)
        if capture_exclusion_supported():
            # 对**所有**截图管线不可见（DEC-027）。spike S2b 已实测对 WGC 与 DXGI 都生效。
            self._wda = bool(w.user32.SetWindowDisplayAffinity(
                self._hwnd, w.WDA_EXCLUDEFROMCAPTURE))
        else:
            # 过旧的系统上 WDA_EXCLUDEFROMCAPTURE 会退化成 WDA_MONITOR（黑块），
            # 比不排除更糟 —— 明确不设，并接受截图被污染。
            self._wda = False

    def place(self, x: int, y: int, width: int, height: int) -> None:
        """定位并改尺寸。置顶（`HWND_TOPMOST`）顺带保证它压在同类窗口之上。"""
        self.ensure()
        if self._rect == (x, y, width, height):
            return
        self._rect = (x, y, width, height)
        if not w.user32.SetWindowPos(self._hwnd, w.HWND_TOPMOST, x, y, width, height,
                                     w.SWP_NOACTIVATE):
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"覆盖层定位失败[{self._title}] err={w.kernel32.GetLastError()}")

    def raise_to_top(self) -> None:
        if self._hwnd is not None:
            w.user32.SetWindowPos(self._hwnd, w.HWND_TOPMOST, 0, 0, 0, 0,
                                  w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)

    def blit(self, pixels: bytearray, width: int, height: int) -> None:
        """把 BGRA 缓冲上屏。`(width, height)` 是**渲染缓冲**尺寸，可以小于窗口。"""
        if len(pixels) != width * height * 4:
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"覆盖层缓冲尺寸不符[{self._title}] "
                          f"{len(pixels)} != {width * height * 4}")
        if self._src_size != (width, height):
            _release_surface(self._src)
            self._src = _make_surface(width, height, self._title)
            self._src_size = (width, height)
        ctypes.memmove(self._src.bits, bytes(pixels), len(pixels))

        window_width, window_height = self._rect[2], self._rect[3]
        if (width, height) == (window_width, window_height):
            source_dc = self._src.dc
        else:
            if self._dst_size != (window_width, window_height):
                _release_surface(self._dst)
                self._dst = _make_surface(window_width, window_height, self._title)
                self._dst_size = (window_width, window_height)
            # **必须是 COLORONCOLOR，不能用 HALFTONE**：HALFTONE 会把 32 位 DIB 的
            # 第 4 个字节（alpha）整条丢掉 —— 实测目的缓冲的 alpha 全变 0。
            # 最近邻放大在这条路径上完全够用：源像素每个覆盖约 7 个屏幕像素，
            # 相邻台阶的 alpha 差约 3/255，在一条本来就该糊的光晕上看不出来。
            w.gdi32.SetStretchBltMode(self._dst.dc, w.COLORONCOLOR)
            if not w.gdi32.StretchBlt(self._dst.dc, 0, 0, window_width, window_height,
                                      self._src.dc, 0, 0, width, height, w.SRCCOPY):
                raise CUError(ErrorCode.INTERNAL_ERROR,
                              f"光晕放大失败[{self._title}] err={w.kernel32.GetLastError()}")
            source_dc = self._dst.dc

        size = _SIZE(window_width, window_height)
        origin = _POINT(self._rect[0], self._rect[1])
        source = _POINT(0, 0)
        blend = _BLENDFUNCTION(w.AC_SRC_OVER, 0, 255, w.AC_SRC_ALPHA)
        ctypes.set_last_error(0)
        ok = w.user32.UpdateLayeredWindow(self._hwnd, None, ctypes.byref(origin),
                                          ctypes.byref(size), source_dc,
                                          ctypes.byref(source), 0, ctypes.byref(blend),
                                          w.ULW_ALPHA)
        if not ok:
            # 静默失败在这里的后果是「覆盖层看不见，但代码看不出问题」——
            # 必须抛出来（架构 §2.5：诊断日志要能回答「覆盖层为何不可见」）。
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"UpdateLayeredWindow 失败[{self._title}] "
                          f"err={w.kernel32.GetLastError()}")

    def show(self) -> None:
        if self._hwnd is None or self._shown:
            return
        w.user32.ShowWindow(self._hwnd, w.SW_SHOWNOACTIVATE)
        self.raise_to_top()
        self._shown = True

    def hide(self) -> None:
        if self._hwnd is None or not self._shown:
            return
        w.user32.ShowWindow(self._hwnd, w.SW_HIDE)
        self._shown = False

    def destroy(self) -> None:
        self._shown = False
        if self._hwnd is not None:
            w.user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        self._rect = None
        _release_surface(self._src)
        self._src = None
        self._src_size = (0, 0)
        _release_surface(self._dst)
        self._dst = None
        self._dst_size = (0, 0)


@dataclass
class _Surface:
    """一个 32 位 DIB 段 + 它自己的 DC。像素内存由 `bits` 直接读写。"""

    dc: int
    bitmap: int
    old_bitmap: int
    bits: ctypes.c_void_p


def _make_surface(width: int, height: int, title: str) -> _Surface:
    """建一个 32 位 DIB 段。

    用 `CreateDIBSection` 而不是 `CreateCompatibleBitmap` + `SetDIBits`：它直接
    交出可写的像素指针，省掉一次整图拷贝，读回文字覆盖率也只有这一条路。
    """
    screen_dc = w.user32.GetDC(None)
    dc = w.gdi32.CreateCompatibleDC(screen_dc)
    w.user32.ReleaseDC(None, screen_dc)
    bits = ctypes.c_void_p()
    info = _bitmap_info(width, height)
    bitmap = w.gdi32.CreateDIBSection(None, ctypes.byref(info), w.DIB_RGB_COLORS,
                                      ctypes.byref(bits), None, 0)
    if not dc or not bitmap or not bits:
        raise CUError(ErrorCode.INTERNAL_ERROR,
                      f"离屏表面创建失败[{title}] err={w.kernel32.GetLastError()}")
    old = w.gdi32.SelectObject(dc, bitmap)
    return _Surface(int(dc), int(bitmap), int(old or 0), bits)


def _release_surface(surface: _Surface | None) -> None:
    """释放一个 DIB 表面。先把旧位图选回去，再删位图 —— 顺序反了删不掉。"""
    if surface is None:
        return
    if surface.dc and surface.old_bitmap:
        w.gdi32.SelectObject(surface.dc, surface.old_bitmap)
    if surface.bitmap:
        w.gdi32.DeleteObject(surface.bitmap)
    if surface.dc:
        w.gdi32.DeleteDC(surface.dc)


class _TextRenderer:
    """GDI 文字栅格化 —— 只产出**覆盖率**，颜色与 alpha 交给 Python 合成。

    为什么不直接用 GDI 画的像素：GDI 在 32 位 DIB 上作画**不写 alpha**（恒为 0），
    拿去做 `UpdateLayeredWindow` 会得到全透明。所以让它在黑底上画白字，
    读回来的灰度就是覆盖率 —— 字体的抗锯齿由 GDI 负责（那是它的专长），
    颜色与 alpha 由我们掌握。这也让「胶囊面是 92% 不透明、文字是 100%」能同时成立。

    只在渲染线程上使用；`close()` 必须和它同线程。
    """

    #: 规范首选字体，回退见 `_pick_face`。
    _PREFERRED = ("Segoe UI Variable Text", "Segoe UI")

    def __init__(self) -> None:
        self._dc = None
        self._fonts: dict[int, int] = {}
        self._face = None

    def _measure_dc(self):
        if self._dc is None:
            screen_dc = w.user32.GetDC(None)
            self._dc = w.gdi32.CreateCompatibleDC(screen_dc)
            w.user32.ReleaseDC(None, screen_dc)
            if not self._dc:
                raise CUError(ErrorCode.INTERNAL_ERROR,
                              f"文字测量 DC 创建失败 err={w.kernel32.GetLastError()}")
        return self._dc

    def _font(self, px: int) -> int:
        font = self._fonts.get(px)
        if font is not None:
            return font
        dc = self._measure_dc()
        for face in (self._face,) if self._face else self._PREFERRED:
            logfont = w.LOGFONTW()
            logfont.lfHeight = -px
            logfont.lfWeight = 400
            logfont.lfCharSet = w.DEFAULT_CHARSET
            logfont.lfQuality = w.ANTIALIASED_QUALITY   # 灰度抗锯齿；CLEARTYPE 会污染 RGB
            logfont.lfFaceName = face
            candidate = w.gdi32.CreateFontIndirectW(ctypes.byref(logfont))
            if not candidate:
                continue
            # 系统找不到face时会静默替换成别的字体 —— 确认一下真的用上了想要的。
            buffer = ctypes.create_unicode_buffer(64)
            w.gdi32.SelectObject(dc, candidate)
            w.gdi32.GetTextFaceW(dc, 64, buffer)
            if self._face is None and not buffer.value.startswith(face):
                w.gdi32.DeleteObject(candidate)
                continue
            self._face = face
            font = int(candidate)
            break
        else:
            raise CUError(ErrorCode.INTERNAL_ERROR, "找不到可用的界面字体")
        self._fonts[px] = font
        return font

    def extent(self, px: int, text: str) -> tuple[int, int]:
        """`(宽, 行高)`。行高取自实际字体，用于竖直居中。"""
        dc = self._measure_dc()
        w.gdi32.SelectObject(dc, self._font(px))
        size = w.SIZE()
        if not w.gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size)):
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"文字测量失败 err={w.kernel32.GetLastError()}")
        return int(size.cx), int(size.cy)

    def mask(self, runs, width: int, height: int) -> bytes:
        """把若干段文字画到一张 DIB 上，返回**覆盖率**（每像素 1 字节，行优先）。

        `runs` 是 `(字号 px, x, y, 文字)`。
        """
        surface = _make_surface(width, height, "text-mask")
        try:
            w.gdi32.PatBlt(surface.dc, 0, 0, width, height, w.BLACKNESS)
            w.gdi32.SetBkMode(surface.dc, w.TRANSPARENT_BK)
            w.gdi32.SetTextColor(surface.dc, 0x00FFFFFF)
            for px, x, y, text in runs:
                w.gdi32.SelectObject(surface.dc, self._font(px))
                w.gdi32.TextOutW(surface.dc, x, y, text, len(text))
            raw = ctypes.string_at(surface.bits, width * height * 4)
        finally:
            _release_surface(surface)
        # BGRA 四通道都等于覆盖率（黑底白字），取一个就够。
        return raw[0::4]

    def close(self) -> None:
        for font in self._fonts.values():
            w.gdi32.DeleteObject(font)
        self._fonts.clear()
        if self._dc:
            w.gdi32.DeleteDC(self._dc)
            self._dc = None


class ControlOverlay:
    """覆盖层。**必须与 daemon 同生命周期**（崩溃即安全，架构 §1.5 第 5 条）。"""

    def __init__(self, on_abort=None) -> None:
        self.on_abort = on_abort
        self._glow = _LayeredSurface("Computer-Use Overlay Glow")
        self._pill = _LayeredSurface("Computer-Use Overlay Pill")
        self._target = _LayeredSurface("Computer-Use Overlay Target")
        self._cursor = _LayeredSurface("Computer-Use Overlay Cursor")
        self._surfaces = (self._glow, self._target, self._cursor, self._pill)
        self._screen: _Screen | None = None
        self._text: _TextRenderer | None = None
        self._state = OverlayState.OFF
        self._state_since = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target_rect: tuple[int, int, int, int] | None = None
        self._cursor_point: tuple[int, int] | None = None
        self._ready = threading.Event()
        self._last_error: BaseException | None = None
        self._error_logged = False
        #: 细节表面的内容签名 —— 内容没变就不重画（胶囊要跑 GDI 量字，不便宜）。
        self._details: dict[str, tuple[object, _Detail]] = {}

    # ---- 状态机（任意线程可调用，只写状态） ----

    @property
    def state(self) -> OverlayState:
        return self._state

    @property
    def visible(self) -> bool:
        return self._state is not OverlayState.OFF

    @property
    def last_error(self) -> BaseException | None:
        """最近一次渲染失败。渲染异常**不再被静默吞掉** —— 那正是覆盖层曾经
        完全不可见却没人发现的原因。"""
        return self._last_error

    def transition(self, state: OverlayState) -> None:
        if state == self._state:
            return
        self._state = state
        self._state_since = time.monotonic()

    def set_target(self, rect: tuple[int, int, int, int] | None) -> None:
        """目标元素高亮框。**只在 Active 显示** —— 出错或中止时 AI 已不在操作任何元素。"""
        self._target_rect = rect

    def set_cursor(self, point: tuple[int, int] | None) -> None:
        self._cursor_point = point

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._ready.clear()
        self._last_error = None
        self._error_logged = False
        self._thread = threading.Thread(target=self._run, name="cu-overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
            self._thread = None

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等**第一帧真的上屏**。返回 False 时看 `last_error` 拿原因。

        存在的理由：`transition()` 之后渲染是异步的，「设了状态」与「画上去了」
        是两件事 —— 验收脚本必须等后者，否则会把「还没画」当成「画得不对」。
        """
        return self._ready.wait(timeout)

    def _run(self) -> None:
        """渲染线程 = 所有窗口的属主线程。抽消息是这里的头等职责。"""
        try:
            while not self._stop.is_set():
                _pump_messages()
                self._sync()
                if self._stop.wait(_FRAME_SECONDS):
                    break
        finally:
            self._destroy_all()

    # ---- 一帧 ----

    def _sync(self) -> None:
        """画一帧。**状态只在此处读一次**，整帧用同一个值 ——

        `transition()` 可能在任何时刻从别的线程改状态，逐处读 `self._state`
        会画出「半帧旧态半帧新态」的东西（实测症状：切到 Off 的瞬间 `_PILL_CONTENT`
        查不到键，整帧被丢弃）。
        """
        try:
            state = self._state
            if state is OverlayState.OFF:
                self._hide_all()
                return
            screen = self._screen_now()
            self._sync_glow(screen, state)
            # 顺序即 z 序：胶囊最后显示，压在最上面（它是状态提示，不该被遮住）。
            target_blitted = self._sync_target(screen, state)
            cursor_blitted = self._sync_cursor(screen, state)
            self._sync_pill(screen, state)
            if (target_blitted or cursor_blitted) and self._pill.shown:
                self._pill.raise_to_top()
            self._ready.set()
            self._last_error = None
            self._error_logged = False
        except Exception as exc:  # noqa: BLE001 —— 渲染失败不能影响输入封锁
            self._note_error(exc)

    def _note_error(self, exc: BaseException) -> None:
        """记下渲染失败，并在**首次**出现时打到 stderr。

        不静默、也不因为一次失败就停掉渲染：瞬时的表面重建失败下一帧可能就好了。
        但「一直失败」必须留下痕迹，否则症状就是「屏幕上看不见，代码上看不出问题」。
        """
        self._last_error = exc
        if not self._error_logged:
            self._error_logged = True
            print(f"[overlay] 渲染失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    def _hide_all(self) -> None:
        for surface in self._surfaces:
            surface.hide()
        self._details.clear()

    def _destroy_all(self) -> None:
        for surface in self._surfaces:
            try:
                surface.destroy()
            except Exception:  # noqa: BLE001 —— 退出路径上不再抛
                pass
        if self._text is not None:
            try:
                self._text.close()
            except Exception:  # noqa: BLE001
                pass
            self._text = None
        self._details.clear()

    def _screen_now(self) -> _Screen:
        if self._screen is None:
            self._screen = _read_screen()
        return self._screen

    # ---- 光晕表面（全屏，缓冲降采样，每帧重画驱动光谱流动） ----

    def _sync_glow(self, screen: _Screen, state: OverlayState) -> None:
        width = min(_RENDER_WIDTH, screen.width)
        height = max(1, int(screen.height * width / screen.width))
        self._glow.place(screen.x, screen.y, screen.width, screen.height)
        self._glow.blit(self._build_glow(screen, width, height, state), width, height)
        self._glow.show()

    def _build_glow(self, screen: _Screen, width: int, height: int,
                    state: OverlayState) -> bytearray:
        """逐像素生成 BGRA 的边缘光晕。

        几何是**「到最近一条屏幕边的距离」**，不是「到屏幕中心的距离」。
        这两个写法只差一个 min，画出来的东西却完全相反：用中心距得到的是一块
        屏幕正中的十字形色块，四边一个像素都没有 —— 而这块色块因为亮在中间、
        又在暗处淡出，肉眼很容易读成「好像有点光」，非常难发现。

        只算靠近边缘的那一圈：距离 >= reach 的地方 alpha 为 0，直接跳过。
        这一步把 4K 全屏的 830 万像素压到实际需要计算的那一圈，
        是在不引入数值库的前提下让纯 Python 渲染可行的关键。

        Stopping / Error 态**整条光晕是一个色相**（冻结琥珀 / 冻结红），不是
        「一条不转的彩虹」。这两件事只差一行：冻结的是**色相本身**，不是
        光谱的旋转相位 —— 只冻相位的话，屏幕上仍是一片彩色，只是不流动了。
        """
        frozen = _FROZEN_HUE.get(state)
        angle0 = 0.0 if frozen is not None else (
            (time.monotonic() - self._state_since) / _SPIN_SECONDS % 1.0)
        frozen_rgb = None
        if frozen is not None:
            red, green, blue = colorsys.hsv_to_rgb(frozen, 0.85, 1.0)
            frozen_rgb = (red, green, blue)

        buffer = bytearray(width * height * 4)
        cx, cy = width / 2.0, height / 2.0
        reach = max(6.0, min(width, height) * _REACH_RATIO)
        edge_columns = int(reach) + 1
        left_band = range(0, min(width, edge_columns))
        right_band = range(max(0, width - edge_columns), width)
        two_pi = 2.0 * math.pi
        hsv_to_rgb = colorsys.hsv_to_rgb
        alpha_scale = _GLOW_OPACITY * 255.0

        for y in range(height):
            # 到上/下边中较近一条的距离。它 >= reach 时，这一行只有左右两条带
            # 还可能落在光晕里，中间整段可以直接跳过。
            edge_y = min(y + 0.5, height - y - 0.5)
            # 竖向衰减：顶部最浓 → 底部更弱（外层单层 mask 的等价物）。
            vertical = 1.0 - (y / max(1, height - 1)) * (1.0 - _FALLOFF_VERTICAL)
            row = y * width * 4
            bands = (left_band, right_band) if edge_y >= reach else (range(width),)
            for band in bands:
                for x in band:
                    distance = min(edge_y, x + 0.5, width - x - 0.5)
                    if distance >= reach:
                        continue
                    # 非线性衰减：靠边最浓、向外迅速溶开、末段极缓归零。
                    # 线性衰减必然在终点留下可感知的内边界（DEC-031 第 4 条）。
                    t = 1.0 - distance / reach
                    value = int(t ** _FALLOFF_EXPONENT * alpha_scale * vertical)
                    if value <= 1:
                        continue
                    if frozen_rgb is None:
                        # 色相沿**屏幕中心的方向角**走 —— 光谱是绕一圈的环，不是沿边平移。
                        hue = (math.atan2((y + 0.5) - cy, (x + 0.5) - cx)
                               / two_pi + angle0) % 1.0
                        red, green, blue = hsv_to_rgb(hue, 0.85, 1.0)
                    else:
                        red, green, blue = frozen_rgb
                    index = row + x * 4
                    buffer[index] = int(blue * value)
                    buffer[index + 1] = int(green * value)
                    buffer[index + 2] = int(red * value)
                    buffer[index + 3] = value
        return buffer

    # ---- 细节表面（原生分辨率，只在内容变化时重画） ----

    def _sync_detail(self, surface: _LayeredSurface, name: str, signature,
                     build) -> bool:
        """同步一个小表面。返回本帧是否重新上屏了（用于维护 z 序）。"""
        cached = self._details.get(name)
        if cached is not None and cached[0] == signature and surface.shown:
            return False
        detail = build()
        if detail is None:
            surface.hide()
            self._details.pop(name, None)
            return False
        self._details[name] = (signature, detail)
        surface.place(detail.x, detail.y, detail.width, detail.height)
        surface.blit(detail.pixels, detail.width, detail.height)
        surface.show()
        return True

    def _sync_pill(self, screen: _Screen, state: OverlayState) -> None:
        self._sync_detail(self._pill, "pill", (state, _screen_key(screen)),
                          lambda: self._build_pill(screen, state))

    def _sync_target(self, screen: _Screen, state: OverlayState) -> bool:
        active = state is OverlayState.ACTIVE
        return self._sync_detail(
            self._target, "target",
            (self._target_rect, _screen_key(screen)) if active else None,
            lambda: self._build_target(screen) if active else None)

    def _sync_cursor(self, screen: _Screen, state: OverlayState) -> bool:
        active = state is OverlayState.ACTIVE
        return self._sync_detail(
            self._cursor, "cursor",
            (self._cursor_point, _screen_key(screen)) if active else None,
            lambda: self._build_cursor(screen) if active else None)

    def _renderer(self) -> _TextRenderer:
        if self._text is None:
            self._text = _TextRenderer()
        return self._text

    def _build_pill(self, screen: _Screen, state: OverlayState) -> _Detail:
        """顶部胶囊：**纯深色实体胶囊 + 文字，无光晕、无描边**（DEC-031 第 3 条）。

        整颗胶囊随屏幕短边缩放（见 `_PILL_FONT_RATIO`），内部比例保持不变。
        深色桌面上靠 `--shadow-pill` 里那圈浅色扩散环分离 —— 那不是装饰，
        是它在深色背景上唯一的边界（D5b）。
        """
        text = self._renderer()
        lead, key, tail = _PILL_CONTENT[state]

        font_px = _pill_font_px(screen)
        k = font_px / _PILL_BASE_FONT_PX
        key_font_px = max(9, round(_PILL_KEY_FONT_PX * k))
        pad_x = round(_PILL_PAD_X * k)
        pad_y = round(_PILL_PAD_Y * k)
        gap = round(_PILL_GAP * k)
        dot = round(_PILL_DOT_PX * k)
        ring = max(1, round(_PILL_RING_PX * k))
        key_pad_x = round(_PILL_KEY_PAD_X * k)
        key_pad_y = max(1, round(_PILL_KEY_PAD_Y * k))
        key_radius = max(2, round(_PILL_KEY_RADIUS * k))

        lead_w, line_h = text.extent(font_px, lead)
        sep_w = text.extent(font_px, "·")[0]
        key_w = key_h = key_text_w = 0
        if key is not None:
            key_text_w, cell_h = text.extent(key_font_px, key)
            key_w = key_text_w + 2 * key_pad_x
            key_h = cell_h + 2 * key_pad_y
        tail_w = text.extent(font_px, tail)[0] if tail else 0

        inner = dot + gap + lead_w
        if key is not None:
            inner += gap + sep_w + gap + key_w + gap + tail_w
        pill_w = pad_x * 2 + inner
        pill_h = pad_y * 2 + max(line_h, dot, key_h)

        side = round(_PILL_MARGIN[0] * k)
        above = round(_PILL_MARGIN[1] * k)
        below = round(_PILL_MARGIN[2] * k)
        width, height = pill_w + side * 2, pill_h + above + below
        buffer = bytearray(width * height * 4)
        left, top = side, above
        right, bottom = left + pill_w, top + pill_h

        self._paint_pill_shadows(buffer, width, height, left, top, right, bottom, k)
        # 浅色扩散环：先铺满整块（含外扩），再用胶囊面盖掉内部，剩下的就是环。
        _fill_round_rect(buffer, width, height,
                         left - ring, top - ring, right + ring, bottom + ring,
                         pill_h / 2.0 + ring,
                         (255, 255, 255), int(_PILL_RING_ALPHA * 255))
        _fill_round_rect(buffer, width, height, left, top, right, bottom,
                         pill_h / 2.0, _PILL_SURFACE,
                         int(_PILL_SURFACE_ALPHA * 255))

        # 逐段排布。
        runs: list[_TextRun] = []
        x = left + pad_x + dot + gap
        line_top = top + max(0, (pill_h - line_h) // 2)
        runs.append(_TextRun(font_px, x, line_top, lead_w, line_h, lead, _PILL_TEXT))
        x += lead_w
        if key is not None:
            x += gap
            runs.append(_TextRun(font_px, x, line_top, sep_w, line_h, "·", _PILL_DIM))
            x += sep_w + gap
            key_top = top + max(0, (pill_h - key_h) // 2)
            # 键帽：1px 描边 + 圆角，非按钮（overlay.md §2.3）。
            _stroke_round_rect(buffer, width, height, x, key_top, x + key_w,
                               key_top + key_h, key_radius,
                               (255, 255, 255), int(_PILL_KEY_BORDER_ALPHA * 255))
            runs.append(_TextRun(key_font_px, x + key_pad_x, key_top + key_pad_y,
                                 key_text_w, key_h, key, _PILL_DIM))
            x += key_w + gap
            runs.append(_TextRun(font_px, x, line_top, tail_w, line_h, tail,
                                 _PILL_TEXT))

        # 文字：GDI 出覆盖率（黑底白字的灰度），这里按各段自己的颜色与 alpha 合成。
        coverage = text.mask([(run.size, run.x, run.y, run.text) for run in runs],
                             width, height)
        for run in runs:
            _compose_text(buffer, coverage, width, height, run.x, run.y,
                          run.width, run.height, run.color)

        # 圆点：色相来自当前状态，不取光谱（圆点表示「哪个阶段」，不是「在流动」）。
        red, green, blue = colorsys.hsv_to_rgb(_PILL_HUE[state], 0.85, 1.0)
        _fill_disc(buffer, width, height, left + pad_x + dot / 2.0,
                   top + pill_h / 2.0, dot / 2.0,
                   (int(blue * 255), int(green * 255), int(red * 255)), 255)

        pill_top = int(screen.short_side * _PILL_TOP_RATIO)
        return _Detail(buffer, screen.x + (screen.width - width) // 2,
                       screen.y + pill_top - above, width, height)

    def _paint_pill_shadows(self, buffer: bytearray, width: int, height: int,
                            left: int, top: int, right: int, bottom: int,
                            scale: float) -> None:
        """`--shadow-pill` 的两层柔影：偏移 + 模糊的深色圆角矩形。

        用可分离盒式模糊近似高斯（跑两趟）。区域只有几十像素见方、而且只在状态
        切换时重画一次，代价可以忽略；偷懒不做的话，胶囊在浅色内容上会显得「贴」
        在屏幕上，缺一层浮起来的重量。
        """
        for offset_y, blur, alpha in _PILL_SHADOWS:
            dy = round(offset_y * scale)
            radius = max(1, round(blur * scale) // 2)
            mask = bytearray(width * height)
            _fill_round_rect(mask, width, height, left, top + dy, right,
                             bottom + dy, (bottom - top) / 2.0,
                             (255, 255, 255), 255)
            _box_blur(mask, width, height, radius)
            _overlay_alpha(buffer, mask, width, height, int(alpha * 255))

    def _build_target(self, screen: _Screen) -> _Detail | None:
        """目标元素高亮框：`0 0 0 2px` 实线环 + `0 0 24px` 外发光（MASTER §2.5）。

        它是**单一琥珀色相**，不取光谱（MASTER §7 的 AVOID）：「目标在哪」与
        「AI 在活动」是两个语义，混用会让用户分不清。
        """
        if self._target_rect is None:
            return None
        x, y, target_w, target_h = self._target_rect
        pad = _TARGET_GLOW_PX + _TARGET_RING_PX
        screen_right, screen_bottom = screen.x + screen.width, screen.y + screen.height
        left = max(screen.x, x - pad)
        top = max(screen.y, y - pad)
        right = min(screen_right, x + target_w + pad)
        bottom = min(screen_bottom, y + target_h + pad)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None

        buffer = bytearray(width * height * 4)
        # 元素矩形在缓冲坐标系里的位置。
        ex, ey = x - left, y - top
        ex2, ey2 = ex + target_w, ey + target_h
        radius = min(8.0, target_w / 2.0, target_h / 2.0)

        # 只扫四条边带：环与外发光都在元素外侧 26px 以内，中间一大片不必碰。
        band = pad + 2
        for x0, x1, y0, y1 in (
            (0, width, 0, min(height, band)),
            (0, width, max(0, height - band), height),
            (0, min(width, band), band, max(band, height - band)),
            (max(0, width - band), width, band, max(band, height - band)),
        ):
            for py in range(y0, y1):
                for px in range(x0, x1):
                    distance = _round_rect_distance(px + 0.5, py + 0.5, ex, ey, ex2, ey2,
                                                    radius)
                    if distance < 0 or distance > pad:
                        continue
                    if distance <= _TARGET_RING_PX:
                        _blend(buffer, width, px, py, _TARGET_COLOR, 255)
                    else:
                        # 外发光：从环外缘起按平方衰减。
                        ratio = 1.0 - (distance - _TARGET_RING_PX) / _TARGET_GLOW_PX
                        _blend(buffer, width, px, py, _TARGET_COLOR,
                               int(ratio * ratio * 255 * 0.45))
        return _Detail(buffer, left, top, width, height)

    def _build_cursor(self, screen: _Screen) -> _Detail | None:
        """光标光晕：52px 径向渐变（overlay.md §3.1 第 1 层）。

        **只画光晕，不替换系统光标。** 规范里「替换光标」是第 2 层，且带一条降级
        路径：「若隐藏系统光标出现闪烁或不稳定，退回只画光晕 + 保留系统光标」。
        隐藏光标是个全局副作用，其稳定性未经真机验证 —— 先走降级路径。

        光晕的意义是让光标在任意背景上都可见：浅色背景靠箭头本身，深色背景靠光晕。
        """
        if self._cursor_point is None:
            return None
        radius = _CURSOR_GLOW_PX / 2.0
        size = _CURSOR_GLOW_PX
        buffer = bytearray(size * size * 4)
        center = radius - 0.5
        for py in range(size):
            for px in range(size):
                distance = math.hypot(px - center, py - center)
                if distance > radius:
                    continue
                # 径向渐变：中心最亮，边缘归零。非线性，与边缘光晕同一条原则。
                alpha = int((1.0 - distance / radius) ** 2.2 * 255 * 0.85)
                if alpha <= 2:
                    continue
                _blend(buffer, width=size, x=px, y=py, bgr=_CURSOR_COLOR, alpha=alpha)
        return _Detail(buffer, self._cursor_point[0] - int(radius),
                       self._cursor_point[1] - int(radius), size, size)


def _screen_key(screen: _Screen) -> tuple[int, int, int, int]:
    return (screen.x, screen.y, screen.width, screen.height)


def _pill_font_px(screen: _Screen) -> int:
    """胶囊字号：按屏幕短边缩放，夹在上下限内。整颗胶囊的尺寸都由它推出。"""
    return max(_PILL_FONT_MIN,
               min(_PILL_FONT_MAX, round(screen.short_side * _PILL_FONT_RATIO)))


def _round_rect_distance(px: float, py: float, left: float, top: float,
                         right: float, bottom: float, radius: float) -> float:
    """点到圆角矩形的有符号距离：<0 内部、0 边界、>0 外部，单位是像素。

    用距离场而不是逐边判断，是为了让「2px 描边」「24px 外发光」「1px 扩散环」
    在四个圆角处也成立 —— 逐边判断会在角上露出直角。
    """
    cx, cy = (left + right) / 2.0, (top + bottom) / 2.0
    hw = max(0.0, (right - left) / 2.0 - radius)
    hh = max(0.0, (bottom - top) / 2.0 - radius)
    qx, qy = abs(px - cx) - hw, abs(py - cy) - hh
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def _fill_round_rect(target, width: int, height: int, left: float, top: float,
                     right: float, bottom: float, radius: float,
                     bgr: tuple[int, int, int], alpha: int) -> None:
    """把圆角矩形按 `alpha` 叠加到 RGBA 缓冲或单通道掩膜上（用 `_blend` 或直接写）。"""
    single = len(target) == width * height
    x0 = max(0, int(left))
    x1 = min(width, int(math.ceil(right)))
    y0 = max(0, int(top))
    y1 = min(height, int(math.ceil(bottom)))
    for py in range(y0, y1):
        for px in range(x0, x1):
            if _round_rect_distance(px + 0.5, py + 0.5, left, top, right, bottom,
                                    radius) > 0:
                continue
            if single:
                target[py * width + px] = alpha
            else:
                _blend(target, width, px, py, bgr, alpha)


def _stroke_round_rect(target, width: int, height: int, left: float, top: float,
                       right: float, bottom: float, radius: float,
                       bgr: tuple[int, int, int], alpha: int) -> None:
    """只画圆角矩形的 1px 轮廓（键帽的描边）。"""
    x0 = max(0, int(left) - 1)
    x1 = min(width, int(math.ceil(right)) + 1)
    y0 = max(0, int(top) - 1)
    y1 = min(height, int(math.ceil(bottom)) + 1)
    for py in range(y0, y1):
        for px in range(x0, x1):
            outside = _round_rect_distance(px + 0.5, py + 0.5, left, top, right,
                                           bottom, radius)
            if not 0 <= outside <= 1.0:
                continue
            _blend(target, width, px, py, bgr, alpha)


def _fill_disc(target, width: int, height: int, cx: float, cy: float, radius: float,
               bgr: tuple[int, int, int], alpha: int) -> None:
    x0 = max(0, int(cx - radius))
    x1 = min(width, int(math.ceil(cx + radius)) + 1)
    y0 = max(0, int(cy - radius))
    y1 = min(height, int(math.ceil(cy + radius)) + 1)
    for py in range(y0, y1):
        for px in range(x0, x1):
            if math.hypot(px + 0.5 - cx, py + 0.5 - cy) <= radius:
                _blend(target, width, px, py, bgr, alpha)


def _compose_text(buffer: bytearray, coverage: bytes, width: int, height: int,
                  x: int, y: int, run_width: int, run_height: int,
                  color: tuple[int, int, int]) -> None:
    """把一段文字的覆盖率按 `color` 合成到缓冲上。

    覆盖率是按**整块缓冲**给的，所以这里必须知道这一段的矩形 —— 只按矩形裁着走，
    不去猜文字实际占了哪些像素。
    """
    for py in range(max(0, y), min(height, y + run_height)):
        row = py * width
        for px in range(max(0, x), min(width, x + run_width)):
            value = coverage[row + px]
            if value:
                _blend(buffer, width, px, py, color, value)


def _box_blur(mask: bytearray, width: int, height: int, radius: int) -> None:
    """就地可分离盒式模糊（横一遍、竖一遍），近似高斯。只用于胶囊投影。"""
    if radius < 1:
        return
    window = 2 * radius + 1
    scratch = bytearray(len(mask))
    for y in range(height):
        row = y * width
        total = 0
        for x in range(-radius, radius + 1):
            total += mask[row + min(max(x, 0), width - 1)]
        for x in range(width):
            scratch[row + x] = total // window
            total -= mask[row + min(max(x - radius, 0), width - 1)]
            total += mask[row + min(max(x + radius + 1, 0), width - 1)]
    for x in range(width):
        total = 0
        for y in range(-radius, radius + 1):
            total += scratch[min(max(y, 0), height - 1) * width + x]
        for y in range(height):
            mask[y * width + x] = total // window
            total -= scratch[min(max(y - radius, 0), height - 1) * width + x]
            total += scratch[min(max(y + radius + 1, 0), height - 1) * width + x]


def _overlay_alpha(buffer: bytearray, mask: bytearray, width: int, height: int,
                   alpha: int) -> None:
    """把单通道掩膜当作黑影叠上去（投影的最后一笔）。"""
    for index in range(width * height):
        weight = mask[index]
        if not weight:
            continue
        _blend(buffer, width, index % width, index // width, (0, 0, 0),
               weight * alpha // 255)


def _blend(buffer: bytearray, width: int, x: int, y: int,
           bgr: tuple[int, int, int], alpha: int) -> None:
    """把一个半透明像素按预乘 alpha **叠加**到缓冲上（不是覆盖）。

    覆盖会把缓冲里已有的边缘光晕擦掉；目标框与光标都画在光晕之上，必须叠加。
    颜色与 alpha 都按 `C_out = C_src + C_dst * (1 - a_src)`、`a_out = a_src + a_dst * (1 - a_src)`
    走 —— 缓冲里存的**是预乘颜色**（`UpdateLayeredWindow` 要的就是预乘），
    所以这里不能写成普通的「两色插值」。
    """
    if alpha <= 0:
        return
    index = (y * width + x) * 4
    inv = 255 - alpha
    for channel in range(3):
        buffer[index + channel] = (bgr[channel] * alpha
                                   + buffer[index + channel] * inv) // 255
    buffer[index + 3] = min(255, alpha + buffer[index + 3] * inv // 255)


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
