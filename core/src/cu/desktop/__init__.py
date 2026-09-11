"""桌面层 —— 全部 Win32 调用集中在这里（DEC-034 / DEC-036）。

对外的形状定义在 `base.py`；daemon 只依赖那个形状，不直接碰 Win32。

分层（依赖方向单向）：

    base.py     接口与数据形状，无 Win32
    dpi.py      DPI 感知声明 —— 必须在任何坐标读取之前
    win32.py    ctypes 绑定与通用结构体（所有 restype 显式声明）
    windows.py  窗口枚举、过滤、提权检测、hwnd 校验
    capture.py  四层降级截图栈
    input.py    SendInput + 拟人轨迹 + Unicode 输入
    overlay.py  控制覆盖层（分层窗口 + 逐像素 alpha + WDA）
    hooks.py    低级键鼠钩子（吞物理、放行注入、物理 Esc 中止）

`RealDesktop` 是把它们接起来的实现；接线完成前 daemon 用 `UnavailableDesktop`，
每个未接的方法都**显式报错**而不是返回空结果。
"""

from __future__ import annotations

from .base import (
    CaptureResult,
    Desktop,
    InputResult,
    ParseResult,
    UnavailableDesktop,
    WindowInfo,
)

__all__ = [
    "CaptureResult",
    "Desktop",
    "InputResult",
    "ParseResult",
    "UnavailableDesktop",
    "WindowInfo",
    "build_desktop",
]


def build_desktop(config, controller=None) -> Desktop:
    """按环境选一个桌面实现。

    真实实现只有在 Win32 模块全部就位时才可用；部分就位时**宁可整体报错**，
    也不要一半能用一半静默失败 —— 后者会让 AI 把「功能没接」误判成「桌面上没有」。
    """
    try:
        from .real import RealDesktop
    except ImportError as exc:
        return UnavailableDesktop(f"桌面层尚未接入（{exc}）")
    try:
        return RealDesktop(config, controller)
    except Exception as exc:  # noqa: BLE001 —— 接线失败也要显式，不能留个半死对象
        return UnavailableDesktop(f"桌面层初始化失败（{type(exc).__name__}: {exc}）")
