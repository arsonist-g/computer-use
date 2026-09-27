"""真机验收：禁用 owner + enabled 模态 popup 的枚举、并集截图、前台路由。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_modal_popup.py

本脚本只创建自己的 owner 与 MessageBox popup；所有窗口都在 finally 中自动关闭。
它不触碰用户已有窗口。popup 被主动摆到 owner 矩形之外，随后用全屏图作差分 oracle：
窗口截图对应区域必须与全屏图同一区域一致，才能证明它真的包含 owner 之外的 popup。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _winutil as W  # noqa: E402
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

from cu.desktop import capture as capture_mod  # noqa: E402
from cu.desktop import windows as windows_mod  # noqa: E402
from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402

ensure_dpi_awareness()

OUT = Path(__file__).parent / "out" / "acceptance-modal-popup"

user32 = ctypes.WinDLL("user32", use_last_error=True)

GW_OWNER = 4
GW_ENABLEDPOPUP = 6
WM_CLOSE = 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
SM_CXSCREEN = 0
SM_CYSCREEN = 1

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.MessageBoxW.restype = ctypes.c_int
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetLastActivePopup.restype = wintypes.HWND
user32.GetLastActivePopup.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []
user32.IsChild.restype = wintypes.BOOL
user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]


def _create_owner() -> int:
    width = min(320, max(180, user32.GetSystemMetrics(SM_CXSCREEN) // 4))
    height = min(240, max(140, user32.GetSystemMetrics(SM_CYSCREEN) // 4))
    hwnd = user32.CreateWindowExW(0, "STATIC", "cu-modal-owner",
                                  WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  40, 40, width, height,
                                  None, None, None, None)
    if not hwnd:
        raise RuntimeError(f"CreateWindowExW(owner) 失败 err={ctypes.get_last_error()}")
    user32.ShowWindow(hwnd, 5)      # SW_SHOW
    user32.UpdateWindow(hwnd)
    return int(hwnd)


def _wait_for(predicate, timeout: float, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def _frame(hwnd: int) -> tuple[int, int, int, int]:
    return windows_mod.w.extended_frame_bounds(hwnd) or windows_mod.w.window_rect(hwnd)


def _union(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _post_close(hwnd: int | None) -> None:
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _probe(owner: int, finished: threading.Event, result: dict) -> None:
    popup = 0
    try:
        popup = int(_wait_for(
            lambda: int(user32.GetWindow(owner, GW_ENABLEDPOPUP) or 0),
            timeout=8.0,
        ) or 0)
        if not popup or popup == owner:
            raise RuntimeError("未观察到 GW_ENABLEDPOPUP 指向 owned popup")

        def relation_ready() -> bool:
            return (
                int(user32.GetWindow(popup, GW_OWNER) or 0) == owner
                and not bool(user32.IsWindowEnabled(owner))
                and bool(user32.IsWindowEnabled(popup))
                and int(user32.GetWindow(owner, GW_ENABLEDPOPUP) or 0) == popup
            )

        if not _wait_for(relation_ready, timeout=4.0):
            raise RuntimeError("popup/owner 的 enabled 或 owner 关系未稳定")

        owner_rect = windows_mod.w.window_rect(owner)
        if owner_rect is None:
            raise RuntimeError("owner 在探测期间消失")
        popup_w = min(380, max(260, user32.GetSystemMetrics(SM_CXSCREEN) // 4))
        popup_h = min(260, max(180, user32.GetSystemMetrics(SM_CYSCREEN) // 4))
        popup_x = owner_rect[2] + 60
        popup_y = owner_rect[1] + 20
        if popup_x + popup_w > user32.GetSystemMetrics(SM_CXSCREEN) - 20:
            popup_x = owner_rect[0]
            popup_y = owner_rect[3] + 60
        if popup_y + popup_h > user32.GetSystemMetrics(SM_CYSCREEN) - 20:
            popup_y = max(20, owner_rect[1] - popup_h - 60)

        if not user32.SetWindowPos(popup, 0, popup_x, popup_y, popup_w, popup_h,
                                   SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW):
            raise RuntimeError("SetWindowPos(popup) 失败")

        def moved() -> bool:
            frame = _frame(popup)
            return frame is not None and frame[2] > frame[0] and frame[3] > frame[1]

        if not _wait_for(moved, timeout=3.0):
            raise RuntimeError("弹窗移动后仍取不到有效矩形")

        owner_frame = _frame(owner)
        popup_frame = _frame(popup)
        if owner_frame is None or popup_frame is None:
            raise RuntimeError("owner/popup 矩形不可读")
        separated = (popup_frame[0] >= owner_frame[2]
                     or popup_frame[1] >= owner_frame[3]
                     or popup_frame[2] <= owner_frame[0]
                     or popup_frame[3] <= owner_frame[1])
        if not separated:
            raise RuntimeError(f"popup 未放到 owner 之外 owner={owner_frame} popup={popup_frame}")

        rows = windows_mod.enumerate_windows(windows_mod.display_context().monitors)
        order = [row.hwnd for row in rows]
        if popup not in order or owner not in order or order.index(popup) >= order.index(owner):
            raise RuntimeError(
                f"默认枚举未按 z-order 收下 modal popup owner={owner:#x} popup={popup:#x} "
                f"owner_text={windows_mod.w.window_text(owner)!r} "
                f"popup_text={windows_mod.w.window_text(popup)!r} "
                f"owner_visible={bool(user32.IsWindowVisible(owner))} "
                f"popup_visible={bool(user32.IsWindowVisible(popup))} "
                f"owner_style={windows_mod.w.window_style(owner):#x} "
                f"owner_ex={windows_mod.w.window_ex_style(owner):#x} "
                f"popup_style={windows_mod.w.window_style(popup):#x} "
                f"popup_ex={windows_mod.w.window_ex_style(popup):#x} "
                f"popup_cloaked={windows_mod.w.is_cloaked(popup)} "
                f"order={[hex(h) for h in order]}"
            )

        info = windows_mod.check_hwnd(owner).info
        shot = capture_mod.capture_window(owner, OUT, 1, info, "png")
        union = _union(owner_frame, popup_frame)
        expected_origin = (union[0], union[1])
        expected_size = (union[2] - union[0], union[3] - union[1])
        if shot.origin != expected_origin or (shot.width, shot.height) != expected_size:
            raise RuntimeError(
                f"截图矩形不是并集 expected={expected_origin}/{expected_size} "
                f"actual={shot.origin}/{shot.width}x{shot.height}"
            )

        shot_image = W.image(shot.path)
        full = capture_mod.capture_full(0, OUT, 2, "png")
        full_image = W.image(full.path)
        monitor = windows_mod.display_context().monitors[0]
        # 取 popup 内部一小块，并要求窗口截图与独立全屏图的同一屏幕区域一致；
        # 仅当图片真的包含 owner 矩形之外的 popup 像素时，这个关系才成立。
        margin_x = max(8, (popup_frame[2] - popup_frame[0]) // 5)
        margin_y = max(8, (popup_frame[3] - popup_frame[1]) // 5)
        x0 = popup_frame[0] + margin_x
        y0 = popup_frame[1] + margin_y
        x1 = popup_frame[2] - margin_x
        y1 = popup_frame[3] - margin_y
        sx, sy = x0 - union[0], y0 - union[1]
        fx, fy = x0 - monitor.rect[0], y0 - monitor.rect[1]
        actual = shot_image[sy:sy + (y1 - y0), sx:sx + (x1 - x0)]
        expected = full_image[fy:fy + (y1 - y0), fx:fx + (x1 - x0)]
        if actual.shape != expected.shape or actual.size == 0:
            raise RuntimeError(f"popup 差分区域形状不一致 actual={actual.shape} expected={expected.shape}")
        diff = float(abs(actual.astype("int16") - expected.astype("int16")).mean())
        if diff > 12.0:
            raise RuntimeError(f"窗口截图中的 popup 区域与全屏图不一致 mean_abs_diff={diff:.2f}")

        windows_mod.bring_to_foreground(owner)
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground != popup:
            raise RuntimeError(f"前台未路由到 popup foreground={foreground:#x} popup={popup:#x}")
        tid = int(user32.GetWindowThreadProcessId(popup, None))
        gui = GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(gui)
        focused = 0
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gui)):
            focused = int(gui.hwndFocus or 0)
        # oracle: specified/derived —— 契约要求焦点进入模态 popup 的输入子树。
        # Win32 的 MessageBox 通常把焦点给内部按钮等子控件，因此顶层 HWND 相等不是
        # 必要条件；`focused == popup` 或 `popup` 是 focused 的祖先是它的正确判据。
        focus_in_popup = focused == popup or bool(user32.IsChild(popup, focused))
        if not focus_in_popup:
            raise RuntimeError(
                f"焦点未落到 popup 子树 focused={focused:#x} popup={popup:#x}"
            )

        result.update(
            ok=True,
            popup=popup,
            owner_frame=owner_frame,
            popup_frame=popup_frame,
            offset_diff=diff,
            capture=(shot.origin, shot.width, shot.height, shot.layer),
        )
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}", popup=popup)
    finally:
        _post_close(popup or int(user32.GetLastActivePopup(owner) or 0))
        finished.set()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    owner = _create_owner()
    finished = threading.Event()
    result: dict = {}
    worker = threading.Thread(target=_probe, args=(owner, finished, result), daemon=True)
    worker.start()
    try:
        user32.MessageBoxW(owner, "modal popup acceptance probe", "cu-modal-popup",
                           MB_OK | MB_ICONINFORMATION)
        if not finished.wait(timeout=8.0):
            raise RuntimeError("探测线程未在超时内结束")
        worker.join(timeout=2.0)
    finally:
        _post_close(int(user32.GetLastActivePopup(owner) or 0))
        user32.DestroyWindow(owner)

    if not result.get("ok"):
        print(f"[FAIL] modal popup acceptance: {result.get('error', '探测线程未留下结果')}")
        return 1
    print(
        "[PASS] modal popup acceptance: "
        f"owner={result['owner_frame']} popup={result['popup_frame']} "
        f"capture={result['capture']} popup_region_diff={result['offset_diff']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
