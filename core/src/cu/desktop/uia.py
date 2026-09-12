"""UIA 文本通道 —— 用 Windows 辅助功能 API 拿**精确**的元素文本与角色。

它解决的是实测出来的一个问题：检测器（YOLO + OCR + Florence）在原生控件界面上
**描述端基本不可用**。对照实验（`tests/native/uia_compare.py`，记事本）：

    UIA 给出 '加粗(Ctrl+B)' / '斜体(Ctrl+I)' / '链接(Ctrl+K)' / '文本编辑器'
    检测器给出 'unanswerable'（同一个幻觉词重复 15 次）

而 UIA 的局限同样明确：**它只对原生控件界面有效**。同机 14 个窗口的实测 —
`msedge 35 / explorer 28 / VS Code 13 / 微信 2 / QQ 1 / Steam 0 / motrix 0`。
Electron 与自绘界面几乎为空，而那正是当前 AI 操作的主要对象。

所以它是**可选增强**，不是替代：能拿到就用它的文本与角色，拿不到就静默退回检测器
的产出。**拿不到不算失败** —— 那是应用的属性，不是工具的。

实现用 ctypes 直接走 COM（DEC-034：Win32 一律 ctypes，不引入 pywin32）。
槽位取自 `UIAutomationClient.h` 的实际虚函数声明顺序，不是凭记忆 ——
差一个槽会崩进程而不是抛异常。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# COM 基础
# ---------------------------------------------------------------------------

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
S_OK = 0
RPC_E_CHANGED_MODE = -2147417850
TREE_SCOPE_DESCENDANTS = 4

#: 元素上限。UIA 走一趟是跨进程 COM 调用，几百个元素在复杂界面上是秒级成本。
#: 超过这个数就截断 —— 我们要的是「文本增强」，不是完整控件树。
MAX_ELEMENTS = 400

#: 能**直接点的**控件类型。用来填 `interactivity`：UIA 的 pattern 查询更准，
#: 但要为每个元素多做一次跨进程调用；按类型判断在覆盖度与成本之间更划算。
_INTERACTIVE_TYPES = frozenset({
    50000,  # Button
    50002,  # CheckBox
    50003,  # ComboBox
    50004,  # Edit
    50005,  # Hyperlink
    50007,  # ListItem
    50011,  # MenuItem
    50013,  # RadioButton
    50015,  # Slider
    50019,  # TabItem
    50024,  # TreeItem
    50029,  # DataItem
    50031,  # SplitButton
    50037,  # TitleBar
})

CONTROL_TYPES: dict[int, str] = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "listitem",
    50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar",
    50015: "slider", 50016: "spinner", 50017: "statusbar", 50018: "tab",
    50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar", 50038: "separator",
    50039: "semanticzoom", 50040: "appbar",
}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> GUID:
        parts = text.strip("{}").split("-")
        guid = cls()
        guid.Data1 = int(parts[0], 16)
        guid.Data2 = int(parts[1], 16)
        guid.Data3 = int(parts[2], 16)
        tail = parts[3] + parts[4]
        for index in range(8):
            guid.Data4[index] = int(tail[index * 2:index * 2 + 2], 16)
        return guid


CLSID_CUIAutomation = GUID.parse("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation = GUID.parse("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


#: vtable 槽号。**这些数字是契约**（对应 `UIAutomationClient.h` 的声明顺序），
#: 改错一个不是报错而是崩进程。
_SLOT_RELEASE = 2
_SLOT_ELEMENT_FROM_HANDLE = 6          # IUIAutomation
_SLOT_CREATE_TRUE_CONDITION = 21       # IUIAutomation
_SLOT_FIND_ALL = 6                     # IUIAutomationElement
_SLOT_ARRAY_LENGTH = 3                 # IUIAutomationElementArray
_SLOT_ARRAY_GET = 4                    # IUIAutomationElementArray
_SLOT_CURRENT_PROCESS_ID = 20          # IUIAutomationElement
_SLOT_CURRENT_CONTROL_TYPE = 21
_SLOT_CURRENT_NAME = 23
_SLOT_CURRENT_IS_OFFSCREEN = 38
_SLOT_CURRENT_BOUNDING_RECT = 43

_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
_uia = ctypes.WinDLL("UIAutomationCore", use_last_error=True)

_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoCreateInstance.restype = ctypes.c_long
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_oleaut32.SysStringLen.restype = ctypes.c_uint
_oleaut32.SysStringLen.argtypes = [ctypes.c_void_p]
_oleaut32.SysFreeString.restype = None
_oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]


def _call(interface: int, slot: int, *argtypes):
    """取 COM 接口 `vtable[slot]` 的函数指针。

    `interface` 是指向 vtable 的指针，vtable 的第 0 项指向 QueryInterface。
    """
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.c_void_p))[0]
    pointer = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(pointer)


def _release(interface: int) -> None:
    _call(interface, _SLOT_RELEASE)(interface)


@dataclass
class UiaElement:
    """一个 UIA 元素。`rect` 是**屏幕坐标**的 (x, y, w, h)。"""

    name: str
    control_type: str
    rect: tuple[int, int, int, int]
    interactive: bool

    def to_dict(self) -> dict:
        return {"type": self.control_type, "bbox": list(self.rect),
                "interactivity": self.interactive, "content": self.name,
                "source": "uia"}


@dataclass
class UiaResult:
    elements: list[UiaElement] = field(default_factory=list)
    #: 为什么没拿到（拿到时不填）。**不是错误** —— 自绘界面拿不到是正常的。
    reason: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.elements)


def _co_init() -> bool:
    """初始化 COM。已经初始化过（同一线程重复调用）也返回真。"""
    hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr in (S_OK, RPC_E_CHANGED_MODE):
        return True
    return hr >= 0


def read_window(hwnd: int, max_elements: int = MAX_ELEMENTS) -> UiaResult:
    """读一个窗口的 UIA 元素。

    **绝不抛错**：拿不到就返回空的 `UiaResult` 并说明原因。
    UIA 是可选增强 —— 它失败不该让 `parse` 失败，那会违反 CONSTRAINT-005
    （核心功能不依赖可选组件）。
    """
    if sys.platform != "win32":
        return UiaResult(reason="非 Windows")
    if not _co_init():
        return UiaResult(reason="COM 初始化失败")

    automation = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(ctypes.byref(CLSID_CUIAutomation), None, CLSCTX_INPROC_SERVER,
                                 ctypes.byref(IID_IUIAutomation), ctypes.byref(automation))
    if hr != S_OK or not automation.value:
        # 没有 UIA 运行库的系统（极老或精简版）。仍然不算错误。
        return UiaResult(reason=f"CUIAutomation 不可用（hr=0x{hr & 0xFFFFFFFF:08X}）")

    root = condition = array = None
    try:
        root_out = ctypes.c_void_p()
        hr = _call(automation.value, _SLOT_ELEMENT_FROM_HANDLE,
                   wintypes.HWND, ctypes.POINTER(ctypes.c_void_p))(
            automation.value, hwnd, ctypes.byref(root_out))
        if hr != S_OK or not root_out.value:
            return UiaResult(reason=f"窗口对 UIA 不可见（hr=0x{hr & 0xFFFFFFFF:08X}）")
        root = root_out.value

        condition_out = ctypes.c_void_p()
        hr = _call(automation.value, _SLOT_CREATE_TRUE_CONDITION,
                   ctypes.POINTER(ctypes.c_void_p))(automation.value, ctypes.byref(condition_out))
        if hr != S_OK or not condition_out.value:
            return UiaResult(reason="CreateTrueCondition 失败")
        condition = condition_out.value

        array_out = ctypes.c_void_p()
        hr = _call(root, _SLOT_FIND_ALL, ctypes.c_int, ctypes.c_void_p,
                   ctypes.POINTER(ctypes.c_void_p))(
            root, TREE_SCOPE_DESCENDANTS, condition, ctypes.byref(array_out))
        if hr != S_OK or not array_out.value:
            return UiaResult(reason="FindAll 失败")
        array = array_out.value

        count = ctypes.c_int(0)
        if _call(array, _SLOT_ARRAY_LENGTH, ctypes.POINTER(ctypes.c_int))(
                array, ctypes.byref(count)) != S_OK:
            return UiaResult(reason="读取元素数失败")

        elements: list[UiaElement] = []
        for index in range(min(count.value, max_elements)):
            element_out = ctypes.c_void_p()
            if _call(array, _SLOT_ARRAY_GET, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    array, index, ctypes.byref(element_out)) != S_OK or not element_out.value:
                continue
            element = element_out.value
            try:
                name = _current_name(element)
                offscreen = _current_bool(element, _SLOT_CURRENT_IS_OFFSCREEN)
                if not name.strip() or offscreen:
                    continue
                rect = _current_rect(element)
                if rect[2] <= 0 or rect[3] <= 0:
                    continue
                control_id = _current_int(element, _SLOT_CURRENT_CONTROL_TYPE)
                elements.append(UiaElement(
                    name=name,
                    control_type=CONTROL_TYPES.get(control_id, f"type{control_id}"),
                    rect=rect,
                    interactive=control_id in _INTERACTIVE_TYPES,
                ))
            finally:
                _release(element)

        if not elements:
            return UiaResult(reason="该窗口未向 UIA 暴露带文本的元素（自绘/Electron 界面）")
        if count.value > max_elements:
            # 截断要说明，否则「元素比实际少」会被当成检测问题。
            return UiaResult(elements=elements,
                             reason=f"已截断（窗口共 {count.value} 个，取前 {max_elements} 个）")
        return UiaResult(elements=elements)
    except OSError as exc:
        return UiaResult(reason=f"UIA 读取异常：{exc}")
    finally:
        for interface in (array, condition, root):
            if interface:
                _release(interface)
        _release(automation.value)


def _current_name(element: int) -> str:
    out = ctypes.c_void_p()
    if _call(element, _SLOT_CURRENT_NAME, ctypes.POINTER(ctypes.c_void_p))(
            element, ctypes.byref(out)) != S_OK or not out.value:
        return ""
    length = _oleaut32.SysStringLen(out)
    try:
        return ctypes.wstring_at(out, length) if length else ""
    finally:
        _oleaut32.SysFreeString(out)


def _current_rect(element: int) -> tuple[int, int, int, int]:
    rect = RECT()
    if _call(element, _SLOT_CURRENT_BOUNDING_RECT, ctypes.POINTER(RECT))(
            element, ctypes.byref(rect)) != S_OK:
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _current_int(element: int, slot: int) -> int:
    out = ctypes.c_int(0)
    if _call(element, slot, ctypes.POINTER(ctypes.c_int))(element, ctypes.byref(out)) != S_OK:
        return -1
    return out.value


def _current_bool(element: int, slot: int) -> bool:
    out = wintypes.BOOL(0)
    if _call(element, slot, ctypes.POINTER(wintypes.BOOL))(element, ctypes.byref(out)) != S_OK:
        return False
    return bool(out.value)


# ---------------------------------------------------------------------------
# 与检测器产出的合并
# ---------------------------------------------------------------------------

#: 判定「两个框是同一个元素」的重叠阈值。取 0.5 是经验值：
#: 检测器的框会包含一点留白，UIA 的框是控件的精确边界，两者不完全重合但必然大幅相交。
_MATCH_THRESHOLD = 0.5


def overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """交集面积 / 较小者面积。用较小者而不是并集：小框落在大框里时，
    用并集会把「同一元素」误判成低重叠。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh) or 1
    return (ix * iy) / smaller


def merge(detector: list[dict], uia: list[UiaElement],
          *, origin: tuple[int, int] = (0, 0)) -> list[dict]:
    """把 UIA 的文本与角色并入检测器产出。

    两条规则：

    1. **UIA 更准，所以它覆盖**。实测 15/15 全部是 UIA 对、检测器错。
       检测器的 bbox 保留（它对「这是个可点的东西」的定位更全），只换文本与角色。
    2. **UIA 没对上的元素原样保留**。它有检测器漏掉的东西（菜单项、状态栏文字），
       而那些恰恰是可点的。

    `origin` 是截图原点的屏幕坐标：UIA 给的是屏幕坐标，检测器给的是图像坐标，
    两者必须先对齐才能比框。这是本模块最容易错的一处 —— 少了这步，
    合并会因为坐标错位而匹配不上，而表现是「UIA 好像没生效」。
    """
    merged = [dict(item) for item in detector]
    used: set[int] = set()

    for candidate in uia:
        screen_box = candidate.rect
        image_box = (screen_box[0] - origin[0], screen_box[1] - origin[1],
                     screen_box[2], screen_box[3])
        best_index, best_ratio = -1, 0.0
        for index, item in enumerate(merged):
            if index in used:
                continue
            box = item.get("bbox")
            if not (isinstance(box, (list, tuple)) and len(box) == 4):
                continue
            width = int(box[2]) - int(box[0])
            height = int(box[3]) - int(box[1])
            if width <= 0 or height <= 0:
                continue
            ratio = overlap_ratio(image_box, (int(box[0]), int(box[1]), width, height))
            if ratio > best_ratio:
                best_index, best_ratio = index, ratio

        if best_index >= 0 and best_ratio >= _MATCH_THRESHOLD:
            item = merged[best_index]
            item["content"] = candidate.name
            item["type"] = candidate.control_type
            item["interactivity"] = candidate.interactive
            item["source"] = "uia"
            used.add(best_index)
        else:
            # 检测器漏掉的 —— UIA 独有，补进去。
            merged.append(candidate.to_dict())

    return merged


def available() -> tuple[bool, str]:
    """UIA 是否可用。做一次真实的小查询代价太高，这里只探运行库。"""
    try:
        _uia.UiaHostProviderFromHwnd  # noqa: B018 —— 只探导出符号存在
    except AttributeError:
        return False, "UIAutomationCore 不可用"
    return True, ""


__all__ = ["MAX_ELEMENTS", "UiaElement", "UiaResult", "available", "merge",
           "overlap_ratio", "read_window"]
