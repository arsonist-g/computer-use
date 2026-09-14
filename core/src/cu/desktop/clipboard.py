"""剪贴板文本读写 —— ctypes 直取 `CF_UNICODETEXT`（DEC-034：不引 pywin32）。

**不走子进程**。2026-09-14 实测，两条子进程路子都会动坏内容：

- `clip.exe` 即使收到一段 UTF-16LE 字节流，也按控制台代码页去解码 —— `中文A` 落进去
  变成 `-N锟斤拷eA`，非 ASCII 直接坏掉；
- `Get-Clipboard -Raw` 反向读会多带一个行尾 `\\r\\n`。

`type` 的换行路径既要把用户的文本原样送进去，也要把用户原来的剪贴板原样还回去
（DEC-077），这两条都做不到，所以改走 Win32 剪贴板 API。
"""

from __future__ import annotations

import ctypes

from ..errors import CUError, ErrorCode
from . import win32 as w


def get_text() -> str | None:
    """读文本剪贴板。里面没有文本（或打不开剪贴板）返回 None —— 它本身不是被验的对象。"""
    if not w.user32.OpenClipboard(None):
        return None
    try:
        handle = w.user32.GetClipboardData(w.CF_UNICODETEXT)
        if not handle:
            return None
        pointer = w.kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            w.kernel32.GlobalUnlock(handle)
    finally:
        w.user32.CloseClipboard()


def set_text(text: str) -> None:
    """写文本剪贴板。失败抛 `CUError` —— 静默失败会让调用方以为内容已经粘进去了。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not w.user32.OpenClipboard(None):
        raise CUError(ErrorCode.INTERNAL_ERROR, "打不开剪贴板",
                      {"err": ctypes.get_last_error()})
    try:
        handle = w.kernel32.GlobalAlloc(w.GMEM_MOVEABLE, len(data))
        if not handle:
            raise CUError(ErrorCode.INTERNAL_ERROR, "分配剪贴板内存失败",
                          {"err": ctypes.get_last_error()})
        pointer = w.kernel32.GlobalLock(handle)
        if not pointer:
            w.kernel32.GlobalFree(handle)
            raise CUError(ErrorCode.INTERNAL_ERROR, "锁定剪贴板内存失败",
                          {"err": ctypes.get_last_error()})
        ctypes.memmove(pointer, data, len(data))
        w.kernel32.GlobalUnlock(handle)
        if not w.user32.EmptyClipboard():
            w.kernel32.GlobalFree(handle)
            raise CUError(ErrorCode.INTERNAL_ERROR, "清空剪贴板失败",
                          {"err": ctypes.get_last_error()})
        if not w.user32.SetClipboardData(w.CF_UNICODETEXT, handle):
            w.kernel32.GlobalFree(handle)
            raise CUError(ErrorCode.INTERNAL_ERROR, "写入剪贴板失败",
                          {"err": ctypes.get_last_error()})
        # 成功后这块内存归系统所有，**不能**再 GlobalFree。
    finally:
        w.user32.CloseClipboard()
