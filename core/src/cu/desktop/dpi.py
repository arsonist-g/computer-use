"""DPI 感知声明 —— **必须在任何坐标读取之前**（架构 §1.5 第 6 条 / CONSTRAINT-002）。

实测：不声明时 `SM_CXSCREEN` 返回 **2752**，声明后返回 **3440**（本机 125% 缩放）。
差 25%，代价是**每一次点击都偏 1/4 屏**。这条没有折中余地。

因此 `ensure_dpi_awareness()` 是 daemon 构造函数的第一句。它是幂等的：
重复调用无害，但调用得晚了就没用了 —— 上一次坐标读取之后才声明，进程仍按虚拟化坐标算。
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
#: 用伪句柄常量而不是 -3 的裸数字：这个值的含义在文档里有名字，裸数字读代码的人认不出来。
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)

#: SetProcessDpiAwareness 的取值（shcore.dll，Win8.1 的回退路径）。
PROCESS_PER_MONITOR_DPI_AWARE = 2
PROCESS_SYSTEM_DPI_AWARE = 1

user32 = ctypes.WinDLL("user32", use_last_error=True)


@dataclass
class DpiAwareness:
    """声明结果。`mode` 决定坐标是否可信，因此它要能被 `daemon status` 报出来。"""

    mode: str
    method: str
    ok: bool

    def __str__(self) -> str:
        return f"{self.mode} (via {self.method})"


def ensure_dpi_awareness() -> DpiAwareness:
    """声明 PER_MONITOR_AWARE_V2。返回实际生效的模式，**不静默失败**。

    降级链：`SetProcessDpiAwarenessContext`（Win10 1703+）→
    `SetProcessDpiAwareness`（Win8.1+）→ `SetProcessDPIAware`（Vista+）。
    每一层失败都继续尝试下一层，但**最终结果如实返回** —— 如果最终只拿到
    system-aware，坐标在多显示器混合缩放下仍会偏，调用方有权知道。
    """
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    except AttributeError:
        pass
    else:
        err = ctypes.get_last_error()
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return DpiAwareness("per-monitor-v2", "SetProcessDpiAwarenessContext", True)
        # ERROR_ACCESS_DENIED(5) = 已经声明过了。这不是失败：只要最终模式是我们想要的，
        # 谁先声明的无所谓。下面的查询会给出真相。
        if err not in (0, 5):
            pass  # 落到下一层

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return DpiAwareness("per-monitor", "SetProcessDpiAwareness", True)
    except (OSError, AttributeError):
        pass

    try:
        if user32.SetProcessDPIAware():
            return DpiAwareness("system", "SetProcessDPIAware", True)
    except AttributeError:
        pass

    return DpiAwareness("none", "（全部失败）", False)


def awareness_mode() -> str:
    """查询当前生效的模式。只用于诊断与自检，不改变任何状态。"""
    try:
        user32.GetProcessDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetProcessDpiAwarenessContext.argtypes = []
        current = user32.GetProcessDpiAwarenessContext()
    except AttributeError:
        return "unknown"
    if current == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.value:
        return "per-monitor-v2"
    if current == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE.value:
        return "per-monitor"
    if current == DPI_AWARENESS_CONTEXT_SYSTEM_AWARE.value:
        return "system"
    return "unaware"
