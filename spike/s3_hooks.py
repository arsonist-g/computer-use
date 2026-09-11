"""S3 —— 验证低级键鼠钩子，以及能否区分「物理事件」与「注入事件」。

回答三个问题：
  Q3a ctypes 能否装上 WH_KEYBOARD_LL / WH_MOUSE_LL 并收到事件？（覆盖层的输入封锁前提）
  Q3b 钩子能否识别 LLKHF_INJECTED / LLMHF_INJECTED，从而放行 AI 的 SendInput？
  Q3c 钩子能否真的拦下事件（返回 1 使其不达应用）？

安全设计（与生产逻辑相反）：
  生产要"吞物理、放行注入"；这里测试模式是"吞注入、放行物理"。
  因此这个脚本永远不会影响用户的真实键鼠 —— 你随时可以正常打字、移动鼠标。
  另配 15 秒看门狗，无论发生什么都会卸载钩子。

方法（同一进程内建一个 Entry 做下游探针，直接读它的内容，无跨进程猜测）：
  P0 对照：无钩子，SendInput "abc"  -> Entry 应收到 "abc"
  P1 放行模式：钩子装好，注入事件带 INJECTED 标志被记录，Entry 仍收到 "abc"
  P2 拦截模式：钩子拦下注入事件，Entry 应为空
"""
import ctypes
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
LLKHF_INJECTED = 0x00000010
LLMHF_INJECTED = 0x00000001
WM_QUIT = 0x0012
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WATCHDOG_SECONDS = 15


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


# ---------------------------------------------------------------- 事件发送

def send_unicode_text(text: str):
    """用 SendInput 的 Unicode 路径打一串字符 —— 这也正是生产输入中文要走的路。"""
    inputs = []
    for ch in text:
        for up in (False, True):
            flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=None)
            inputs.append(inp)
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    return sent == len(inputs)


def send_mouse_move(x: int, y: int):
    """绝对移动（虚拟桌面归一化坐标），同样带 INJECTED 标志。"""
    MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    nx, ny = int(x * 65535 / max(sw - 1, 1)), int(y * 65535 / max(sh - 1, 1))
    inp = INPUT(type=0)                     # INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                          dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                          time=0, dwExtraInfo=None)
    arr = (INPUT * 1)(inp)
    return user32.SendInput(1, arr, ctypes.sizeof(INPUT)) == 1


# ---------------------------------------------------------------- 钩子线程

class HookThread(threading.Thread):
    """低级钩子必须在「装有消息循环的线程」上安装，否则收不到回调。"""

    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state
        self.thread_id = None
        self.kb_hook = None
        self.ms_hook = None
        self.ready = threading.Event()
        # 必须持有回调引用，否则被 GC 回收后系统调用野指针 —— ctypes 的经典崩溃点
        self._kb_proc = HOOKPROC(self._on_key)
        self._ms_proc = HOOKPROC(self._on_mouse)

    # 返回值 >0 表示吞掉事件，0/None 表示放行
    def _on_key(self, n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            injected = bool(kb.flags & LLKHF_INJECTED)
            self.state["kb_events"].append((kb.vkCode, injected))
            # 生产逻辑是「吞物理、放行注入」；这里刻意取反，避免影响用户真实输入
            if self.state["mode"] == "swallow_injected" and injected:
                return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_mouse(self, n_code, w_param, l_param):
        if n_code == 0:
            ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            injected = bool(ms.flags & LLMHF_INJECTED)
            self.state["ms_events"].append((ms.pt.x, ms.pt.y, injected))
            if self.state["mode"] == "swallow_injected" and injected:
                return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self.kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0)
        self.ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, None, 0)
        self.state["kb_hook_ok"] = bool(self.kb_hook)
        self.state["ms_hook_ok"] = bool(self.ms_hook)
        self.state["hook_err"] = ctypes.get_last_error()
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        for h in (self.kb_hook, self.ms_hook):
            if h:
                user32.UnhookWindowsHookEx(h)
        self.kb_hook = self.ms_hook = None
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- 主流程

def main():
    state = {
        "mode": "passthrough",
        "kb_events": [],
        "ms_events": [],
        "kb_hook_ok": False,
        "ms_hook_ok": False,
        "hook_err": 0,
    }
    results = []

    root = tk.Tk()
    root.title("S3-hook-probe")
    root.geometry("520x120+400+300")
    entry = tk.Entry(root, font=("Segoe UI", 16))
    entry.pack(fill="both", expand=True, padx=12, pady=12)

    hook = {"thread": None}

    def read_entry():
        return entry.get()

    def clear_entry():
        entry.delete(0, tk.END)

    def focus_and_send(text):
        root.lift()
        root.attributes("-topmost", True)
        entry.focus_force()
        root.update()
        time.sleep(0.25)
        ok = send_unicode_text(text)
        root.update()
        time.sleep(0.35)
        return ok

    # --- P0 对照：无钩子 ---
    def phase0():
        send_ok = focus_and_send("abc")
        got = read_entry()
        results.append(("P0 对照（无钩子）", "PASS" if got == "abc" else "FAIL",
                        f"SendInput 成功={send_ok} Entry={got!r}"))
        clear_entry()
        # 装钩子
        hook["thread"] = HookThread(state)
        hook["thread"].start()
        hook["thread"].ready.wait(3)
        results.append(("Q3a 低级钩子安装", "PASS" if state["kb_hook_ok"] and state["ms_hook_ok"] else "FAIL",
                        f"键盘钩子={state['kb_hook_ok']} 鼠标钩子={state['ms_hook_ok']} "
                        f"err={state['hook_err']}"))
        root.after(200, phase1)

    # --- P1 放行模式 ---
    def phase1():
        state["mode"] = "passthrough"
        state["kb_events"].clear()
        state["ms_events"].clear()
        send_mouse_move(700, 500)
        send_ok = focus_and_send("abc")
        got = read_entry()
        time.sleep(0.2)
        kb_inj = sum(1 for _, inj in state["kb_events"] if inj)
        kb_phy = sum(1 for _, inj in state["kb_events"] if not inj)
        ms_inj = sum(1 for _, _, inj in state["ms_events"] if inj)
        results.append(("Q3b 注入标志识别", "PASS" if kb_inj >= 6 and ms_inj >= 1 else "FAIL",
                        f"键盘事件 注入={kb_inj} 物理={kb_phy}；鼠标事件 注入={ms_inj}"))
        results.append(("P1 放行模式下应用收到输入", "PASS" if got == "abc" else "FAIL",
                        f"SendInput 成功={send_ok} Entry={got!r}"))
        clear_entry()
        root.after(200, phase2)

    # --- P2 拦截模式（只吞注入，绝不影响物理） ---
    def phase2():
        state["mode"] = "swallow_injected"
        time.sleep(0.1)
        focus_and_send("abc")
        got = read_entry()
        results.append(("Q3c 钩子能拦下注入事件", "PASS" if got == "" else "FAIL",
                        f"Entry={got!r}（应为空 —— 说明拦下了）"))
        state["mode"] = "passthrough"
        finish()

    def finish():
        if hook["thread"]:
            hook["thread"].stop()
        print("\n===== S3 结果 =====")
        for name, verdict, detail in results:
            print(f"[{verdict:4}] {name}\n         {detail}")
        root.destroy()

    def watchdog():
        if root.winfo_exists():
            state["mode"] = "passthrough"
            if hook["thread"]:
                hook["thread"].stop()
            print("\n!! 看门狗触发：已强制卸载钩子并退出")
            root.destroy()

    root.after(WATCHDOG_SECONDS * 1000, watchdog)
    root.after(900, phase0)
    root.mainloop()


if __name__ == "__main__":
    main()
