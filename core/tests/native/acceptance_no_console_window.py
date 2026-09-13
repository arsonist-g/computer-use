"""验收补充：跑 CLI 时**不许出现多余的控制台窗口**（本轮新增，发布阻塞项）。

## 为什么要有这个脚本

Windows 上「控制台子系统程序」被一个**没有控制台**的父进程拉起时，系统会给它
**新开一个控制台**；只有显式传 `CREATE_NO_WINDOW`（Node 侧是 `windowsHide: true`）
才能压住。本项目的 daemon 是**刻意 detached（无控制台）**的（架构 §1.5 第 5 条
「崩溃即安全」的一部分），所以**它拉起的每一个子进程都会弹窗**，除非逐个传标志。

用户 2026-09-13 报告的现象就是这个：跑 `computer-use …` 会弹一个 python 控制台黑窗。
发布版里出现黑窗不可接受 —— 它让「AI 在操作系统」显得可疑，还会污染截图。

## 判据的边界（重要，别把不可修的算进来）

**能由产品修**：产品（Node 启动器 / Python 客户端 / daemon）拉起的每一个子进程。
**不能由产品修**：调用方自己怎么拉起 CLI。若调用方不给自己的直接子进程传
`CREATE_NO_WINDOW`，那 node 本体也会弹窗，而这在包内无法补救（node 是控制台子系统程序）。

因此本脚本**把入口进程按「规范调用方」的方式隐藏**（`CREATE_NO_WINDOW`），
然后断言：**入口之后，产品再没有弹出任何多余控制台**。

## 判定信号：**可见**控制台窗口

`EnumWindows` 取**可见**顶层窗口里控制台类/宿主进程的窗口。采样线程在用例运行期间持续
快照并取并集，闪一下就消失的也能抓到。判据是「入口之后没有**多出来的可见控制台窗口**」
—— 那正是用户看得见的东西，也正是本任务判据里那句「屏幕上一个多余窗口都不该出现」。

**为什么不用「有没有多出控制台宿主进程」当判据（这里踩过坑，记下来）**：
`CREATE_NO_WINDOW` **不是**「不分配控制台」，而是「分配一个**没有窗口**的控制台」。
所以**修好之后**，每一条被压住的子进程**仍然会起一个宿主进程**（`conhost.exe` /
`OpenConsole.exe`），只是用户什么都看不见。用宿主进程数当判据，会把**已经修好的**
判成泄漏（实测：三条用例各报 2~3 个「泄漏宿主」，而可见窗口是 0）。

**辅助信号**：可见控制台窗口的**总数**有没有增长 —— 挡的是「增量集合为空、但总数确实
变多了」这种（句柄被复用的时候，差集看不出来）。

**归因**：窗口按**标题里的路径**归属（控制台窗口的标题默认就是它所运行程序的可执行
路径）。宿主进程按**创建者链**归属，但只用于**打印**。归因不到的一律原样打印、
不计入判定，这样并发活动既不会污染结论、也不会被藏起来。

## 前提

跑之前让这台机器**安静**：别同时开别的 agent / 别的 CLI。归因已经把噪声挡掉大半，
但安静环境下的结论最干净。

## 跑法

    .venv/Scripts/python.exe core/tests/native/acceptance_no_console_window.py
    （可选 --only A,B 只跑指定用例；C 会慢，要加载 omni）

用例：
    A  `windows`（覆盖 Node 启动器 → Python 客户端这一跳，以及启动器的 env sync）
    B  `env sync`（覆盖启动器里的 uv 探测/安装）
    C  `parse --hwnd …`（覆盖 daemon → omni worker 这一跳；要 omni 已安装）
    D  `type "…"`（覆盖 daemon → powershell / clip 的剪贴板回落；**需人工跑**，见文件末）
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()
ROOT = CORE.parent
LAUNCHER = ROOT / "bin" / "computer-use.mjs"
NODE = shutil.which("node")

#: 逐字符构造反斜杠（见 `_bootstrap.py`：写在字面量里会被 shell / heredoc 反复吃掉）
BS = chr(92)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                  wintypes.LPARAM), wintypes.LPARAM]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_PTR = ctypes.c_void_p(-1).value

#: 传统控制台（conhost）与 Windows Terminal（Win11 默认终端）的窗口类
CONSOLE_WINDOW_CLASSES = {
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "PseudoConsoleWindow",
}

#: 控制台宿主进程名（小写）。**不含** `WindowsTerminal.exe` —— 它是 UI 进程，
#: 一个进程承载多个标签页，计进来只会引入噪声；`OpenConsole.exe` 才是「一个控制台一个」。
CONSOLE_HOST_NAMES = ("conhost.exe", "openconsole.exe")


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def unique_pipe(tag: str) -> str:
    return BS * 2 + "." + BS + "pipe" + BS + f"cu-nowin-{tag}-{os.getpid()}"


def _process_path(pid: int) -> str:
    """进程可执行文件的完整路径（拿不到就返回空串）。"""
    if pid <= 0:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(2048)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _process_table() -> dict[int, tuple[str, int]]:
    """全进程表：pid → (可执行文件名, 父进程 pid)。"""
    table: dict[int, tuple[str, int]] = {}
    snapshot_handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot_handle or snapshot_handle == INVALID_HANDLE_PTR:
        return table
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if kernel32.Process32FirstW(snapshot_handle, ctypes.byref(entry)):
            while True:
                table[entry.th32ProcessID] = (entry.szExeFile, entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot_handle, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot_handle)
    return table


def _ancestry(pid: int, table: dict[int, tuple[str, int]],
              depth: int = 3) -> list[tuple[int, str]]:
    """从 `pid` 向上取 depth 层的 `(pid, 可执行路径)`，**当场取好**。

    **必须当场取**：短命进程（uv、node 的子进程）在用例结束时已经没了，事后再去查路径
    只能拿到空串 —— 实测因此把一条真泄漏判成了「未归因」（假通过）。采样线程每 50ms
    跑一次，那时进程还活着，路径取得到。
    """
    chain: list[tuple[int, str]] = []
    current = pid
    for _ in range(depth):
        if not current:
            break
        chain.append((current, _process_path(current).lower().replace("/", BS)))
        entry = table.get(current)
        current = entry[1] if entry else 0
    return chain


def console_hosts() -> dict[int, list[tuple[int, str]]]:
    """控制台宿主：pid → 从创建者向上三层的 `(pid, 路径)` 链。

    控制台宿主必定由「创建那个控制台的那个进程」拉起，所以从创建者往上走就是归因路径。
    走三层而不是一层，是因为 uv 建的 venv 里 `python.exe` 是个**跳板**：daemon 的实际
    镜像是基础解释器（`…\\pyenv-win\\…\\python.exe`，不在数据目录下），创建者链的下一层
    才是数据目录里的 venv python。实测只走一层会漏掉 daemon 自己那个控制台。
    """
    table = _process_table()
    return {pid: _ancestry(parent_pid, table)
            for pid, (name, parent_pid) in table.items()
            if name.lower() in CONSOLE_HOST_NAMES}


def console_windows() -> frozenset[tuple[int, str, str]]:
    """可见顶层窗口里的控制台窗口：`(窗口句柄, 类名, 标题)`。

    刻意不带 pid：Windows Terminal 会把多个控制台放在同一个进程里，pid 不稳定；
    句柄 + 类名 + 标题足以判「多了个窗口」，标题还顺便给了归因依据。
    """
    windows: set[tuple[int, str, str]] = set()

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = Path(_process_path(pid.value)).name.lower()
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        class_name = cls.value
        if class_name in CONSOLE_WINDOW_CLASSES or name in CONSOLE_HOST_NAMES:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            windows.add((hwnd, class_name, title.value))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
    return frozenset(windows)


class _Sampler:
    """运行期间持续采样，取并集 —— 闪一下就消失的窗口也能抓到。"""

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self.windows: set = set()
        self.hosts: dict[int, list[tuple[int, str]]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.windows |= console_windows()
            self.hosts.update(console_hosts())
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 用例执行
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, str, str]] = []


def record(case: str, verdict: str, detail: str) -> None:
    RESULTS.append((case, verdict, detail))
    print(f"[{verdict:4}] 用例 {case}  {detail}", flush=True)


def isolated_env(tag: str) -> tuple[dict, Path]:
    """完全隔离的运行环境：临时 home + 独立管道（不碰真实 ~/.computer-use）。"""
    home = Path(tempfile.mkdtemp(prefix=f"cu-nowin-{tag}-"))
    env = {
        **os.environ,
        "USERPROFILE": str(home),
        "COMPUTER_USE_HOME": str(home / ".computer-use"),
        "COMPUTER_USE_PIPE": unique_pipe(tag),
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("COMPUTER_USE_SESSION", None)
    return env, home


def run_hidden(args: list[str], env: dict, timeout: float, markers: list[str],
               cwd: Path | None = None, state: dict | None = None):
    """按「规范调用方」的方式拉起入口进程：入口隐藏，之后一切都不许弹窗。

    `CREATE_NO_WINDOW` = 0x08000000。`subprocess.CREATE_NO_WINDOW` 在非 Windows 上
    不存在，而本项目只支持 Windows（`package.json` 的 `os: ["win32"]`），故直接写字面量。

    **顺手把入口进程真实的可执行路径追加进 `markers`**：`shutil.which("node")` 拿到的是
    一个 alias/垫片（实测 `…\\node\\aliases\\default\\node.EXE`），与实际运行的
    `…\\node-versions\\v22.23.1\\installation\\node.exe` **不是同一个路径** ——
    用 `which` 的结果去归因会漏掉「node 创建的那个控制台」，而 node 这一跳正是本轮的
    主犯之一（实测漏判过一次）。
    """
    started = time.monotonic()
    proc = subprocess.Popen(args, env=env, cwd=str(cwd or ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=0x08000000)
    # 记下入口 pid：「链路里出现过这个 pid」比路径更硬的证据 —— 同一个 node.exe 路径
    # 可能被并发的另一个 agent 也在用（实测踩到过，会把别人的窗口算成自己的）。
    if state is not None:
        state["entry_pid"] = proc.pid
    real_path = _process_path(proc.pid).lower().replace("/", BS)
    if real_path and real_path not in markers:
        markers.append(real_path)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return proc.returncode, stdout, stderr, time.monotonic() - started


def _markers(env: dict) -> list[str]:
    """本次运行的归因标记：自己的数据目录 + node/uv 可执行路径。

    这几个足以覆盖产品会拉起的每一类子进程（客户端、omni worker、uv、powershell/clip），
    又不会命中别人的临时目录。
    """
    data_dir = env.get("COMPUTER_USE_HOME") or str(
        Path(env.get("USERPROFILE") or Path.home()) / ".computer-use")
    raw = [data_dir, shutil.which("uv") or "", NODE or ""]
    return [item.lower().replace("/", BS) for item in raw if item]


def check_case(case: str, label: str, args: list[str], env: dict, timeout: float,
               cwd: Path | None = None) -> None:
    markers = _markers(env)
    state: dict = {}
    before_windows = console_windows()
    before_hosts = set(console_hosts())

    sampler = _Sampler()
    sampler.start()
    try:
        rc, out, err, elapsed = run_hidden(args, env, timeout, markers, cwd, state)
    except subprocess.TimeoutExpired:
        sampler.stop()
        record(case, "失败", f"{label}：超时（{timeout:.0f}s）—— 结果不可判定")
        return
    sampler.stop()

    new_windows = sorted(sampler.windows - set(before_windows))
    all_hosts = {**console_hosts(), **sampler.hosts}
    new_hosts = {pid: chain for pid, chain in all_hosts.items() if pid not in before_hosts}

    def window_is_ours(title: str) -> bool:
        normalized = title.lower().replace("/", BS)
        return any(marker in normalized for marker in markers)

    def host_is_ours(chain: list[tuple[int, str]]) -> bool:
        """创建者链里任何一层命中归因标记，就算本次运行拉起的。

        链是在**采样当时**取好的（见 `_ancestry`），因此短命进程也认得出。
        """
        entry_pid = state.get("entry_pid")
        return any(pid == entry_pid or any(marker in path for marker in markers)
                   for pid, path in chain)

    mine_windows = [w for w in new_windows if window_is_ours(w[2])]
    rest_windows = [w for w in new_windows if not window_is_ours(w[2])]
    if mine_windows:
        # 空标题的 `PseudoConsoleWindow` 是同一个控制台的另一半（实测每次泄漏都成对
        # 出现），它自己没有标题可供归因 —— 只在「本次运行已经泄漏了」时才跟着计入。
        untitled = [w for w in rest_windows if not w[2]]
        mine_windows += untitled
        rest_windows = [w for w in rest_windows if w not in untitled]
    mine_hosts = {pid: chain for pid, chain in new_hosts.items() if host_is_ours(chain)}
    rest_hosts = {pid: chain for pid, chain in new_hosts.items() if not host_is_ours(chain)}

    # 判定：判据是**多出来的可见控制台窗口** —— 那才是用户看得见的东西。
    # 宿主进程**不参与判决**：`CREATE_NO_WINDOW` 会合法地起一个无窗口控制台（见文件头）。
    count_grew = len(sampler.windows) > len(before_windows)
    ok = not mine_windows and not count_grew
    detail = (f"{label}：exit={rc} 耗时 {elapsed:.1f}s · "
              f"归因本次运行的新可见窗口 {len(mine_windows)} 个 · "
              f"可见窗口总数 {len(before_windows)}→{len(sampler.windows)} · "
              f"新宿主进程 {len(new_hosts)} 个（仅诊断，不参与判决）"
              + ("" if ok else " ⟵ 泄漏！"))
    record(case, "通过" if ok else "失败", detail)
    for hwnd, cls, title in mine_windows:
        print(f"         泄漏窗口 hwnd={hwnd} class={cls!r} title={title!r}")
    if count_grew:
        print(f"         ⚠ 可见窗口总数增长 {len(before_windows)}→{len(sampler.windows)}"
              f"（增量集合为空也算数：句柄复用会让差集看不出来）")
    for hwnd, cls, title in rest_windows:
        print(f"         （未计入）窗口 hwnd={hwnd} class={cls!r} title={title!r}")
    for pid, chain in {**mine_hosts, **rest_hosts}.items():
        print(f"         （诊断：新宿主进程）pid={pid} 创建者链={chain}")
    text = ((out or "") + (err or "")).strip()
    if text:
        print(f"         输出：{text.splitlines()[0][:120] if text.splitlines() else ''}")


def case_a(skip: bool) -> None:
    if skip:
        record("A", "不适用", "--only 未选中")
        return
    env, home = isolated_env("a")
    try:
        check_case("A", "`windows`（Node → Python 客户端）",
                   [NODE, str(LAUNCHER), "windows"], env, timeout=180)
    finally:
        _cleanup(env, home)


def case_b(skip: bool) -> None:
    if skip:
        record("B", "不适用", "--only 未选中")
        return
    env, home = isolated_env("b")
    try:
        check_case("B", "`env sync`（启动器里的 uv 探测/安装）",
                   [NODE, str(LAUNCHER), "env", "sync"], env, timeout=900)
    finally:
        _cleanup(env, home)


def case_c(skip: bool) -> None:
    if skip:
        record("C", "不适用", "--only 未选中")
        return
    # 这一例**必须**用真实数据目录：omni 只装在那儿（数 GB，不可能为测试重下）。
    # 它写入的是产品自己的会话目录（受配额管理），不删改任何既有文件。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("COMPUTER_USE_PIPE", None)
    env.pop("COMPUTER_USE_SESSION", None)
    probe = subprocess.run([NODE, str(LAUNCHER), "windows"], env=env, cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    matched = re.search(r"hwnd=(0x[0-9A-Fa-f]+)", probe.stdout or "")
    if not matched:
        record("C", "不适用", "拿不到窗口句柄（`windows` 没输出 hwnd）—— 跳过")
        return
    hwnd = matched.group(1)

    # `parse` 与 `windows` 不同：它**要求会话身份**（`omni.parse` 要把结构化数据记进
    # 会话目录，而 `windows` 不落盘）。先 `begin` 拿一个会话再带着它 parse ——
    # 否则 daemon 回 `invalid_params`「缺少 session_id」，那一轮等于**根本没跑**，
    # 会伪装成「没有泄漏」（第一次就撞上了这个假绿）。
    begun = subprocess.run([NODE, str(LAUNCHER), "begin", "--json"], env=env, cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    try:
        session = json.loads(begun.stdout or "{}").get("result", {}).get("session_id", "")
    except ValueError:
        session = ""
    if not session:
        record("C", "不适用",
               f"`begin` 没给出 session_id：{(begun.stdout or begun.stderr or '')[:120]}")
        return
    env = {**env, "COMPUTER_USE_SESSION": session}
    check_case("C", f"`parse --hwnd {hwnd}`（daemon → omni worker）",
               [NODE, str(LAUNCHER), "parse", "--hwnd", hwnd], env, timeout=600)


def case_d(skip: bool) -> None:
    if skip:
        record("D", "不适用", "--only 未选中")
        return
    record("D", "需人工", "`type` 的剪贴板回落路径会**读取并覆盖用户的剪贴板** —— "
                          "本轮未获授权，不由脚本执行。人工验法见文件末。")


def _cleanup(env: dict, home: Path) -> None:
    """收掉隔离 daemon（只能经它自己的管道），再删临时目录。"""
    try:
        subprocess.run([NODE, str(LAUNCHER), "daemon", "stop"], env=env, cwd=str(ROOT),
                       capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    only = ""
    for index, arg in enumerate(sys.argv):
        if arg == "--only" and index + 1 < len(sys.argv):
            only = sys.argv[index + 1]
    selected = {part.strip().upper() for part in only.split(",") if part.strip()}

    def skip(case: str) -> bool:
        return bool(selected) and case not in selected

    print("=" * 78)
    print("跑 CLI 不许弹多余控制台（入口进程按规范调用方隐藏；窗口 + 宿主进程双信号）")
    print("=" * 78)
    if NODE is None:
        print("  找不到 node，无法进行")
        return 1

    case_a(skip("A"))
    case_b(skip("B"))
    case_c(skip("C"))
    case_d(skip("D"))

    print("\n===== 结果 =====")
    for case, verdict, detail in RESULTS:
        print(f"  用例 {case}  {verdict:<5} {detail}")
    failed = [r for r in RESULTS if r[1] == "失败"]
    print(f"\n失败 {len(failed)} 例" + ("" if not failed else " —— 发布阻塞项未清"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
