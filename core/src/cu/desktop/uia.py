"""UIA 通道 —— 用 Windows 辅助功能 API 拿**精确**的元素文本、角色与状态。

它解决的是实测出来的一个问题：检测器（YOLO + OCR + Florence）在原生控件界面上
**描述端基本不可用**。对照实验（`tests/native/uia_compare.py`，记事本）：

    UIA 给出 '加粗(Ctrl+B)' / '斜体(Ctrl+I)' / '链接(Ctrl+K)' / '文本编辑器'
    检测器给出 'unanswerable'（同一个幻觉词重复 15 次）

**状态也是它独有的**：一个灰掉的按钮在像素上和一个亮的按钮长得一样，检测器与多模态
端点都只会报「这是个按钮」；而 UIA 的 `IsEnabled` 直接给出此刻能不能点。实测资源管理器
的工具栏：画面里 `新建`/`排序`/`后退`/`前进` 是灰的、`查看` 是亮的，UIA 报的正是
`0/0/0/0/1`，五个全对。

而 UIA 的局限同样明确：**它只对原生控件界面有效**。同机 14 个窗口的实测里，元素文本
只从浏览器、资源管理器、编辑器这类原生控件应用里拿得到；Electron 与自绘界面几乎为空，
而那正是当前 AI 操作的主要对象。

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

#: **返回**多少个可用元素。超过就截断 —— 我们要的是「文本与状态增强」，不是完整控件树。
MAX_ELEMENTS = 400

#: **翻看**树里多少个元素。与 `MAX_ELEMENTS` 是两件事，早先被合成一个数，代价是：
#: 一个 Electron 窗口的树有 550 个元素、其中 217 个可用，而「取前 400 个」把可用元素
#: 也一起卡在 400 个下标之内 —— 实测返回 114 个，**丢掉 103 个有名字的控件**。
#: 两者的代价差得很远：翻看是每个元素两次跨进程属性读（实测 550 个元素约 145ms，
#: 比只翻 400 个只多 22ms），而丢掉的是真的能用来说明「这是什么」的名字。
#:
#: 上限仍然要有：树极大时逐元素读属性是线性成本（实测约 0.2ms/个）。
MAX_SCAN = 4000

#: 能**直接点的**控件类型。只回答「它是不是个能操作的东西」这一半；
#: 另一半是「此刻能不能操作」，来自 `IsEnabled` —— 两者一起由 `_is_actionable` 合并。
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
#: `get_CurrentIsEnabled`。这个槽号是**插值出来的**，两点独立验证过：
#: ① 扫 24~31 槽，24/25/29/30/31 返回 BSTR 指针、26/27/28 返回布尔 —— 与
#:    `AcceleratorKey / AccessKey / HasKeyboardFocus / IsKeyboardFocusable /
#:    IsEnabled / AutomationId / ClassName / HelpText` 的顺序逐位吻合，
#:    且与已知锚点 23(Name) / 38(IsOffscreen) / 43(BoundingRectangle) 自洽；
#: ② 取值与画面一致（见模块 docstring 的资源管理器实测）。
_SLOT_CURRENT_IS_ENABLED = 28
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
    """一个 UIA 元素。`rect` 是**屏幕坐标**的 `(left, top, width, height)`。

    这是 UIA 自己的形状，**不对外**：`to_dict()` 才转成契约形状。
    """

    name: str
    control_type: str
    rect: tuple[int, int, int, int]
    #: 类型上可交互**且**此刻启用 —— 即「现在能不能点」。
    interactive: bool
    #: `IsEnabled`。与 `interactive` 分开留着：一个禁用的按钮和一个纯文本标签
    #: 在 `interactive` 上都是假，但前者是「等条件满足就能点」，后者是「别管它」。
    enabled: bool = True

    def to_dict(self) -> dict:
        """契约形态：`bbox` 一律 `[x1, y1, x2, y2]`，与检测器产出同一套语义。

        **转换发生在这里，不在消费侧。** `bbox` 在整个契约里只有一个含义；
        早先这里原样输出 `(left, top, width, height)`，而合并侧按 `(x1,y1,x2,y2)`
        解包（要求 `width > left` 且 `height > top`），于是「左边距大于自身宽度」的元素
        被静默丢掉 —— 实测一个资源管理器窗口 UIA 读到 162 个、只有 1 个过关，
        而侥幸过关的那些框在 md 里又是另一套语义，读的人按框算中心会偏几百像素。
        """
        left, top, width, height = self.rect
        return {"type": self.control_type,
                "bbox": [left, top, left + width, top + height],
                "interactivity": self.interactive,
                "enabled": self.enabled,
                "content": self.name,
                "source": "uia"}


@dataclass
class UiaResult:
    elements: list[UiaElement] = field(default_factory=list)
    #: 为什么没拿到（拿到时不填）。**不是错误** —— 自绘界面拿不到是正常的。
    reason: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.elements)


def _is_actionable(control_id: int, enabled: bool) -> bool:
    """这个控件**现在**能不能点。

    两件事分开问、合起来答：类型决定「它是不是个能操作的东西」，`IsEnabled` 决定
    「此刻能不能操作」。早先只看类型，于是一个灰掉的按钮照样报可交互 —— 调用方据此
    点下去什么都不会发生（实测：资源管理器没有历史时的「后退」）。
    """
    return control_id in _INTERACTIVE_TYPES and enabled


def _scan_budget(total: int) -> int:
    """这次最多翻看树里多少个元素。

    **与 `MAX_ELEMENTS` 不是一回事**：那个是「返回几个可用元素」，这个是「允许翻多少个下标」。
    合成一个数就会出现「前 400 个下标」被当成「前 400 个可用元素」——
    而可用元素在树里的分布是不均匀的，Electron 窗口的大块无名子树会把后面的名字全顶出去。
    """
    return min(total, MAX_SCAN)


def _truncation_note(total: int, scanned: int, returned: int) -> str:
    """截断说明；没截断返回空串。

    两种截断要分开说：树里还有没翻到的（扫描上限），和可用元素已经取满（返回上限）。
    合成一句会让「为什么少了」无从判断 —— 前者要调 `MAX_SCAN`，后者才要调 `MAX_ELEMENTS`。
    """
    notes = []
    if total > scanned:
        notes.append(f"树里还有 {total - scanned} 个没翻（扫描上限 {MAX_SCAN}）")
    if returned >= MAX_ELEMENTS:
        notes.append(f"可用元素已取满 {MAX_ELEMENTS} 个")
    return "已截断：" + "；".join(notes) if notes else ""


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
        scanned = _scan_budget(count.value)
        for index in range(scanned):
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
                enabled = _current_bool(element, _SLOT_CURRENT_IS_ENABLED)
                elements.append(UiaElement(
                    name=name,
                    control_type=CONTROL_TYPES.get(control_id, f"type{control_id}"),
                    rect=rect,
                    interactive=_is_actionable(control_id, enabled),
                    enabled=enabled,
                ))
            finally:
                _release(element)
            # 取满就停 —— 但**不**因此少翻树：翻到的下标由 `_scan_budget` 决定，
            # 这里管的是「返回几个」。
            if len(elements) >= max_elements:
                break

        if not elements:
            return UiaResult(reason="该窗口未向 UIA 暴露带文本的元素（自绘/Electron 界面）")
        return UiaResult(elements=elements,
                         reason=_truncation_note(count.value, scanned, len(elements)))
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


def available() -> tuple[bool, str]:
    """UIA 是否可用。做一次真实的小查询代价太高，这里只探运行库。"""
    try:
        _uia.UiaHostProviderFromHwnd  # noqa: B018 —— 只探导出符号存在
    except AttributeError:
        return False, "UIAutomationCore 不可用"
    return True, ""


__all__ = ["MAX_ELEMENTS", "MAX_SCAN", "UiaElement", "UiaResult", "available", "read_window"]
