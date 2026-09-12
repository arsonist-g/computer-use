"""UIA 通道探针 —— 用 ctypes 直接走 COM，验证槽位与取值正确。

**为什么独立成一个脚本**：ctypes 调 COM 时 vtable 槽位差一个就会崩进程，
不是抛异常。所以先用最小范围验证槽位，再往里加逻辑。

槽位取自 `UIAutomationClient.h`（winsdk-10）的实际声明顺序，不是凭记忆。
接口是 C++ 虚基类，第 0~2 槽是 IUnknown，之后按声明顺序编号。

跑法：
    .venv/Scripts/python.exe core/tests/native/uia_probe.py [--hwnd 0x...]

不给 `--hwnd` 时自动开一个记事本当目标（原生控件最多，最能看出差异）。
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import utf8_console  # noqa: E402

utf8_console()

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
uia = ctypes.WinDLL("UIAutomationCore", use_last_error=True)

# ---- COM 基础 ----
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
S_OK = 0
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> GUID:
        parts = text.strip("{}").split("-")
        g = cls()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        tail = parts[3] + parts[4]
        for i in range(8):
            g.Data4[i] = int(tail[i * 2:i * 2 + 2], 16)
        return g


#: {ff48dba4-60ef-4201-aa87-54103eef594e}
CLSID_CUIAutomation = GUID.parse("ff48dba4-60ef-4201-aa87-54103eef594e")
#: {30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}
IID_IUIAutomation = GUID.parse("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")

ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
uia.UiaHostProviderFromHwnd.restype = ctypes.c_long
uia.UiaHostProviderFromHwnd.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)]


def _method(interface: int, slot: int, *argtypes):
    """取 vtable[slot] 的函数指针。

    `interface` 是 COM 接口指针（指向 vtable 的指针）。
    """
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.c_void_p))[0]
    func_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(func_ptr)


def vt_release(interface: int) -> None:
    _method(interface, 2)(interface)


def vt_element_from_handle(automation: int, hwnd: int) -> int | None:
    """IUIAutomation::ElementFromHandle，槽 6。"""
    out = ctypes.c_void_p()
    hr = _method(automation, 6, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p))(
        automation, hwnd, ctypes.byref(out))
    if hr != S_OK or not out.value:
        raise OSError(f"ElementFromHandle hr=0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def vt_create_true_condition(automation: int) -> int:
    """IUIAutomation::CreateTrueCondition，槽 21。"""
    out = ctypes.c_void_p()
    hr = _method(automation, 21, ctypes.POINTER(ctypes.c_void_p))(automation, ctypes.byref(out))
    if hr != S_OK or not out.value:
        raise OSError(f"CreateTrueCondition hr=0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def vt_find_all(element: int, scope: int, condition: int) -> int:
    """IUIAutomationElement::FindAll，槽 6。"""
    out = ctypes.c_void_p()
    hr = _method(element, 6, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
        element, scope, condition, ctypes.byref(out))
    if hr != S_OK or not out.value:
        raise OSError(f"FindAll hr=0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def vt_array_length(array: int) -> int:
    n = ctypes.c_int(0)
    hr = _method(array, 3, ctypes.POINTER(ctypes.c_int))(array, ctypes.byref(n))
    if hr != S_OK:
        raise OSError(f"get_Length hr=0x{hr & 0xFFFFFFFF:08X}")
    return n.value


def vt_array_get(array: int, index: int) -> int:
    out = ctypes.c_void_p()
    hr = _method(array, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
        array, index, ctypes.byref(out))
    if hr != S_OK or not out.value:
        raise OSError(f"GetElement({index}) hr=0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def vt_current_name(element: int) -> str:
    """IUIAutomationElement::get_CurrentName，槽 23。返回 BSTR。"""
    out = ctypes.c_void_p()
    hr = _method(element, 23, ctypes.POINTER(ctypes.c_void_p))(element, ctypes.byref(out))
    if hr != S_OK or not out.value:
        return ""
    oleaut32.SysStringLen.restype = ctypes.c_uint
    oleaut32.SysStringLen.argtypes = [ctypes.c_void_p]
    length = oleaut32.SysStringLen(out)
    text = ctypes.wstring_at(out, length) if length else ""
    oleaut32.SysFreeString(out)
    return text


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def vt_current_rect(element: int) -> tuple[int, int, int, int]:
    """IUIAutomationElement::get_CurrentBoundingRectangle，槽 43。"""
    rect = RECT()
    hr = _method(element, 43, ctypes.POINTER(RECT))(element, ctypes.byref(rect))
    if hr != S_OK:
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def vt_current_int(element: int, slot: int) -> int:
    out = ctypes.c_int(0)
    hr = _method(element, slot, ctypes.POINTER(ctypes.c_int))(element, ctypes.byref(out))
    return out.value if hr == S_OK else -1


def vt_current_bool(element: int, slot: int) -> bool:
    out = wintypes.BOOL(0)
    hr = _method(element, slot, ctypes.POINTER(wintypes.BOOL))(element, ctypes.byref(out))
    return bool(out.value) if hr == S_OK else False


#: 控件类型 ID → 名字。只列会用到的；其余以数字形式带出来。
CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}

TREE_SCOPE_DESCENDANTS = 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", default=None)
    args = parser.parse_args()

    # ---- 目标：没有就开一个记事本 ----
    spawned = None
    hwnd = args.hwnd
    if not hwnd:
        spawned = subprocess.Popen(["notepad.exe"])
        time.sleep(4)
        # 用 Win32 找记事本窗口（不依赖 UIA，先把目标定下来）
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        found = user32.FindWindowW("Notepad", None) or user32.FindWindowW(None, None)
        hwnd = int(found) if found else 0
    if not hwnd:
        print("没有可用的目标窗口")
        return 1
    print(f"目标 hwnd=0x{int(hwnd):08X}")

    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr not in (S_OK, RPC_E_CHANGED_MODE):
        print(f"CoInitializeEx hr=0x{hr & 0xFFFFFFFF:08X}")
        return 1
    # CoInitializeEx 的返回值在 ctypes 下是 LONG，负数是 HRESULT 失败
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        print(f"CoInitializeEx 失败 hr={hr}")
        return 1

    automation = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_CUIAutomation), None, CLSCTX_INPROC_SERVER,
                                ctypes.byref(IID_IUIAutomation), ctypes.byref(automation))
    if hr != S_OK or not automation.value:
        print(f"CoCreateInstance hr=0x{hr & 0xFFFFFFFF:08X}")
        return 1
    print("CUIAutomation 创建成功")

    try:
        root = vt_element_from_handle(automation.value, int(hwnd))
        print("ElementFromHandle 成功")

        condition = vt_create_true_condition(automation.value)
        array = vt_find_all(root, TREE_SCOPE_DESCENDANTS, condition)
        count = vt_array_length(array)
        print(f"后代元素数 = {count}")
        if count == 0:
            print("→ 该窗口对 UIA 暴露为空（自绘/Electron 界面的典型表现）")
            return 0

        named = 0
        shown = 0
        for i in range(count):
            element = vt_array_get(array, i)
            try:
                name = vt_current_name(element)
                ctype = CONTROL_TYPES.get(vt_current_int(element, 21), "?")
                rect = vt_current_rect(element)
                offscreen = vt_current_bool(element, 38)
                interactive = vt_current_bool(element, 27)   # IsKeyboardFocusable
                if name.strip():
                    named += 1
                if name.strip() and shown < 15:
                    shown += 1
                    print(f"  [{ctype:<12}] {rect}  focusable={int(interactive)} "
                          f"offscreen={int(offscreen)}  {name[:44]!r}")
            finally:
                vt_release(element)
        print(f"\n有名字的元素 = {named}/{count}")
        vt_release(array)
        vt_release(condition)
        vt_release(root)
    finally:
        vt_release(automation.value)
        if spawned is not None:
            spawned.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
