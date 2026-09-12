"""输入合成 —— SendInput（鼠标 / 键盘）、拟人轨迹、Unicode 输入（DEC-008 / DEC-019）。

**轨迹算法从 参考实现 的 `core/bu_core/humanize.py` 移植**（用户点名的基线），
它本身是 camoufox `HumanizeMouseTrajectory` 的 Python 直译：
三次贝塞尔采样 → y 向扰动 → 弧长幂标度定步数 → easeOutQuad 索引重采样。

**派发层必须重写**：参考实现走 CDP 合成事件，这里走 `SendInput` ——
坐标系（屏幕绝对物理像素，不是视口坐标）、事件结构、节奏控制全部不同。
逐点 `SetCursorPos` 是错的：那会绕过输入队列，某些应用看到的是「光标瞬移」而非鼠标移动。

难度键黑名单在这里执行（DEC-019）。**不提供二次确认**，只提供 `--force` 越过 ——
CLI 无法可靠识别「危险类别」，误判要么形同虚设要么频繁打断。
"""

from __future__ import annotations

import ctypes
import math
import random
import time

from ..errors import CUError, ErrorCode
from . import win32 as w
from .base import InputResult

# ---- 轨迹常量（与参考实现逐项对应，改值即偏离基线）----
_KNOT_MARGIN = 80
_KNOT_COUNT = 2
_DISTORT_MEAN = 1.0
_DISTORT_STD = 1.0
_DISTORT_FREQ = 0.5
_LEN_EXP = 0.25
_LEN_FACTOR = 20
_MIN_POINTS = 2
#: 按下→抬起之间的间隔。参考实现靠 CDP 回执链带出的自然延迟，SendInput 没有回执，
#: 用一个小随机间隔补偿这段差距。
_PRESS_S = (0.02, 0.06)

#: 危险键序列黑名单（DEC-019）。命中即拒绝，需显式 `--force`。
DEFAULT_DANGER_KEYS = frozenset({"win+l", "ctrl+alt+del"})

_BUTTONS = {
    "left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
    "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
    "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP),
}

#: 虚拟屏幕左上角与尺寸，用于把屏幕绝对坐标换算成 SendInput 的归一化坐标。
_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    # F13~F24 是标准功能键（VK_F13..VK_F24 = 0x7C..0x87）。**键盘上没有，
    # 但 API 能用** —— 正因如此它们是理想的「无副作用注入」目标：
    # 任何应用都不会绑定它们，注入时不会在用户屏幕上留下任何痕迹。
    # （验收脚本 §1.3 就靠这个验「注入输入在封锁期间被放行」。）
    "f13": 0x7C, "f14": 0x7D, "f15": 0x7E, "f16": 0x7F, "f17": 0x80, "f18": 0x81,
    "f19": 0x82, "f20": 0x83, "f21": 0x84, "f22": 0x85, "f23": 0x86, "f24": 0x87,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _ch in "abcdefghijklmnopqrstuvwxyz":
    _VK[_ch] = ord(_ch.upper())
for _d in "0123456789":
    _VK[_d] = ord(_d)


def _ease_out_quad(t: float) -> float:
    return -t * (t - 2)


def trajectory(x0: float, y0: float, x1: float, y1: float,
               max_points: int) -> list[tuple[int, int]]:
    """返回含首尾的整数点列（参考实现 `trajectory` 的移植）。

    流程：三次贝塞尔采样（长边每 px 一点）→ y 向扰动 → 弧长幂标度定步数 →
    easeOutQuad 索引重采样（末段步距收敛）。
    """
    left = min(x0, x1) - _KNOT_MARGIN
    right = max(x0, x1) + _KNOT_MARGIN
    down = min(y0, y1) - _KNOT_MARGIN
    up = max(y0, y1) + _KNOT_MARGIN
    knots = [(random.uniform(left, right), random.uniform(down, up))
             for _ in range(_KNOT_COUNT)]
    p0, p3 = (x0, y0), (x1, y1)
    p1, p2 = knots

    n = int(max(abs(x1 - x0), abs(y1 - y0), 2))
    raw = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        u = 1.0 - t
        raw.append((
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ))

    distorted = [raw[0]]
    for point in raw[1:-1]:
        delta = round(random.gauss(_DISTORT_MEAN, _DISTORT_STD)) \
            if random.random() < _DISTORT_FREQ else 0.0
        distorted.append((point[0], point[1] + delta))
    distorted.append(raw[-1])

    total = 0.0
    for a, b in zip(distorted, distorted[1:], strict=False):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    target = min(max_points, max(_MIN_POINTS, int(total ** _LEN_EXP * _LEN_FACTOR)))
    out = []
    for i in range(target):
        t = i / (target - 1)
        index = int(_ease_out_quad(t) * (len(distorted) - 1))
        out.append((round(distorted[index][0]), round(distorted[index][1])))
    return out


# ---------------------------------------------------------------------------
# SendInput 派发
# ---------------------------------------------------------------------------


def _virtual_screen() -> tuple[int, int, int, int]:
    """虚拟屏幕（全部显示器）的 (left, top, width, height)。

    SendInput 的绝对坐标是**归一化到虚拟屏幕**的 0..65535，不是像素。
    用主显示器尺寸换算会在多显示器下算错。
    """
    left = w.user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
    top = w.user32.GetSystemMetrics(77)       # SM_YVIRTUALSCREEN
    width = w.user32.GetSystemMetrics(78)     # SM_CXVIRTUALSCREEN
    height = w.user32.GetSystemMetrics(79)    # SM_CYVIRTUALSCREEN
    return left, top, width, height


def _normalize(x: int, y: int) -> tuple[int, int]:
    left, top, width, height = _virtual_screen()
    # 65535 而不是 65536：微软文档的 Absolute 坐标满量程是 65535。
    nx = int(round((x - left) * 65535 / max(1, width - 1)))
    ny = int(round((y - top) * 65535 / max(1, height - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _send(*inputs: w.INPUT) -> None:
    count = len(inputs)
    array = (w.INPUT * count)(*inputs)
    sent = w.user32.SendInput(count, array, ctypes.sizeof(w.INPUT))
    if sent != count:
        raise CUError(ErrorCode.INTERNAL_ERROR,
                      f"SendInput 只投递了 {sent}/{count} 个事件",
                      {"err": w.kernel32.GetLastError()})


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> w.INPUT:
    return w.INPUT(type=w.INPUT_MOUSE,
                   mi=w.MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags,
                                   time=0, dwExtraInfo=None))


def _key_input(vk: int, flags: int = 0, scan: int = 0) -> w.INPUT:
    return w.INPUT(type=w.INPUT_KEYBOARD,
                   ki=w.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None))


# 轨迹起点：与参考实现的 `_last_tracked_pos` 同款，初始 (0,0)。
_last_tracked: tuple[float, float] = (0.0, 0.0)


def move_cursor(x: int, y: int, step_ms: int = 10, max_points: int = 30) -> int:
    """从上次落点拟人移动到 (x, y)。返回耗时毫秒。

    中间点固定间隔，跳过轨迹首尾（起点已经在那里了，终点随后精确派发）。
    """
    global _last_tracked
    start = time.monotonic()
    if round(x) == round(_last_tracked[0]) and round(y) == round(_last_tracked[1]):
        return 0
    points = trajectory(_last_tracked[0], _last_tracked[1], x, y, max_points)
    for px, py in points[1:-1]:
        nx, ny = _normalize(px, py)
        _send(_mouse_input(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK,
                           nx, ny))
        time.sleep(step_ms / 1000.0)
    # 终点精确派发 —— 轨迹的最后一个点是浮点插值的结果，不能让落点差几个像素。
    nx, ny = _normalize(x, y)
    _send(_mouse_input(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK,
                       nx, ny))
    _last_tracked = (float(x), float(y))
    return int((time.monotonic() - start) * 1000)


def click(x: int, y: int, *, button: str = "left", count: int = 1,
          step_ms: int = 10, max_points: int = 30) -> InputResult:
    started = time.monotonic()
    if button not in _BUTTONS:
        raise CUError(ErrorCode.INVALID_PARAMS,
                      f"不支持的鼠标键：{button}", {"allowed": sorted(_BUTTONS)})
    down, up = _BUTTONS[button]
    moved_ms = move_cursor(x, y, step_ms=step_ms, max_points=max_points)
    for index in range(max(1, count)):
        if index:
            # 双击间隔落在系统双击时限内即可；上限取系统设置更稳妥，
            # 这里用一个常见值，间隔太小会被部分应用合并成单击。
            time.sleep(0.08)
        _send(_mouse_input(down))
        time.sleep(random.uniform(*_PRESS_S))
        _send(_mouse_input(up))
    return InputResult(ok=True, moved_ms=moved_ms,
                       total_ms=int((time.monotonic() - started) * 1000))


def drag(x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
         step_ms: int = 10, max_points: int = 30) -> InputResult:
    started = time.monotonic()
    if button not in _BUTTONS:
        raise CUError(ErrorCode.INVALID_PARAMS, f"不支持的鼠标键：{button}")
    down, up = _BUTTONS[button]
    moved_ms = move_cursor(x1, y1, step_ms=step_ms, max_points=max_points)
    _send(_mouse_input(down))
    time.sleep(random.uniform(*_PRESS_S))
    move_cursor(x2, y2, step_ms=step_ms, max_points=max_points)
    time.sleep(random.uniform(*_PRESS_S))
    _send(_mouse_input(up))
    return InputResult(ok=True, moved_ms=moved_ms,
                       total_ms=int((time.monotonic() - started) * 1000))


def scroll(dx: int, dy: int, *, at: tuple[int, int] | None = None) -> InputResult:
    """滚动。**正 dy = 向上**（api-contract.md §1.3）。

    Windows 的 `WHEEL_DELTA` 正值表示向前滚（内容上移），与契约同向，
    因此不翻转符号 —— 但这条必须写下来，符号翻转是这类接口最常见的静默错误。
    """
    started = time.monotonic()
    moved_ms = 0
    if at is not None:
        moved_ms = move_cursor(at[0], at[1])
    ticks = int(dy)
    horizontal = int(dx)
    if ticks == 0 and horizontal == 0:
        return InputResult(ok=True, moved_ms=moved_ms, total_ms=0)
    # 每 tick 一次事件：一次投递整段滚动量，应用收到的是一步到底，不像人。
    step = 1 if ticks > 0 else -1
    for _ in range(abs(ticks)):
        _send(_mouse_input(w.MOUSEEVENTF_WHEEL, 0, 0, step * w.WHEEL_DELTA))
        time.sleep(0.01)
    hstep = 1 if horizontal > 0 else -1
    for _ in range(abs(horizontal)):
        _send(_mouse_input(w.MOUSEEVENTF_HWHEEL, 0, 0, hstep * w.WHEEL_DELTA))
        time.sleep(0.01)
    return InputResult(ok=True, moved_ms=moved_ms,
                       total_ms=int((time.monotonic() - started) * 1000))


def type_text(text: str) -> InputResult:
    """Unicode 输入。优先 `KEYEVENTF_UNICODE`，逐字符投递。

    spike 已实测这条路径可用（测试字符正是走它送达的），因此中文无需剪贴板中转。
    剪贴板降级留给 `type` 在 Unicode 路径**确实失败**时使用 —— 不是默认路径，
    因为剪贴板会破坏用户原有的剪贴板内容。
    """
    started = time.monotonic()
    if not text:
        return InputResult(ok=True, total_ms=0)
    failures: list[str] = []
    try:
        for ch in text:
            if ch == "\n":
                # 换行走 VK_RETURN 而不是 Unicode 码点：多数控件只认按键。
                _send(_key_input(0x0D), _key_input(0x0D, w.KEYEVENTF_KEYUP))
                continue
            _send(_key_input(0, w.KEYEVENTF_UNICODE, ord(ch)),
                  _key_input(0, w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP, ord(ch)))
            time.sleep(0.002)
    except CUError as exc:
        failures.append(str(exc))
    if not failures:
        return InputResult(ok=True, total_ms=int((time.monotonic() - started) * 1000))
    # 降级：整体走剪贴板 + Ctrl+V。只在 Unicode 路径失败时才用。
    detail = _type_via_clipboard(text)
    return InputResult(ok=True, total_ms=int((time.monotonic() - started) * 1000),
                       detail={"fallback": "clipboard", "unicode_error": failures[0], **detail})


def _type_via_clipboard(text: str) -> dict:
    """剪贴板降级：写入 → Ctrl+V → 尽量恢复原剪贴板。

    恢复失败不报错，但如实记进 detail —— 用户有权知道自己的剪贴板被动过。
    """
    import subprocess

    previous: str | None = None
    try:
        previous = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        previous = None

    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True, timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise CUError(ErrorCode.INTERNAL_ERROR,
                      f"Unicode 输入失败，且剪贴板降级同样失败：{exc}") from exc

    key("ctrl+v")

    restored = False
    if previous:
        try:
            subprocess.run(["clip"], input=previous.encode("utf-16-le"), check=True, timeout=5)
            restored = True
        except Exception:  # noqa: BLE001
            restored = False
    return {"clipboard_restored": restored}


def _parse_combo(combo: str) -> list[int]:
    """`ctrl+shift+f10` → vk 序列。支持 `ctrl+c` / `alt+tab` / `win+r` / `esc`。"""
    tokens = [t.strip().lower() for t in combo.replace(" ", "").split("+") if t.strip()]
    if not tokens:
        raise CUError(ErrorCode.INVALID_PARAMS, f"空按键组合：{combo!r}")
    keys: list[int] = []
    for token in tokens:
        if token in _VK:
            keys.append(_VK[token])
        elif len(token) == 1:
            vk = w.user32.VkKeyScanW(token)
            if vk == -1:
                raise CUError(ErrorCode.INVALID_PARAMS, f"无法映射字符：{token!r}")
            keys.append(vk & 0xFF)
        else:
            raise CUError(ErrorCode.INVALID_PARAMS, f"未知按键名：{token!r}", {"combo": combo})
    return keys


def key(combo: str, *, force: bool = False, danger_keys: frozenset[str] | None = None) -> InputResult:
    """按组合键。命中黑名单则拒绝，`force=True` 越过（DEC-019）。"""
    started = time.monotonic()
    normalized = "+".join(t.strip().lower() for t in combo.split("+") if t.strip())
    blocked = danger_keys if danger_keys is not None else DEFAULT_DANGER_KEYS
    if normalized in blocked and not force:
        raise CUError(
            ErrorCode.DANGEROUS_KEY_BLOCKED,
            f"按键序列 {normalized} 在黑名单中",
            {"combo": normalized, "danger_keys": sorted(blocked), "bypass": "--force"},
        )
    keys = _parse_combo(combo)
    for vk in keys:
        _send(_key_input(vk))
    time.sleep(0.01)
    for vk in reversed(keys):
        _send(_key_input(vk, w.KEYEVENTF_KEYUP))
    return InputResult(ok=True, total_ms=int((time.monotonic() - started) * 1000))
