"""IPC 层 —— 命名管道（DEC-033 / DEC-036）。

**零 pywin32**：管道、DACL、读写全部走 ctypes（DEC-034 / DEC-036）。
DACL 用 SDDL 手写并交给内核执行，spike S4 已实测证伪组（只授权 SYSTEM）确实被拒（err=5）。

线程模型（架构 §2.2，刻意不用重叠 I/O）：acceptor 线程循环「建实例 → 等连接 → 交接」，
每个连接一个处理线程阻塞读写。低并发（个位数）下这套模型完全够用，
且显著降低 ctypes 复杂度 —— 重叠 I/O 要引入事件对象与 IOCP，收益为零。

本模块只负责「把一行 NDJSON 搬进搬出」，不解析语义 —— 那是 protocol.py 的事。
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .errors import CUError, ErrorCode
from .protocol import APP_ERROR, AppError, dump_line, error_response, load_line, ok_response

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080

SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TokenUser = 1

ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231
ERROR_PIPE_LISTENING = 536
ERROR_OPERATION_ABORTED = 995

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: 单条消息的字节上限。客户端可能带 `--inline` 传 base64（一张 4K PNG ≈ 数 MB），
#: 与 protocol.read_line 的 8MiB 保持一致。
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: 连接空闲上限。超过即认为客户端已死（CLI 进程崩溃时不会关闭管道）。
#: 取值远大于一次正常命令的往返，避免误杀正在思考的调用方。
CONNECTION_IDLE_TIMEOUT = 600.0

#: 管道名。DACL 已经限定了当前用户与 SYSTEM，名字本身不需要带 SID
#: —— 带 SID 反而会让「同一用户的另一个进程」看起来像另一个主体。
PIPE_NAME = r"\\.\pipe\computer-use-daemon"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# 陷阱（spike RESULT #2）：返回句柄的 API 不声明 restype，ctypes 按 32 位 int 截断，
# GetCurrentProcess() 的伪句柄 -1 会变成 0xFFFFFFFF。所有句柄返回一律显式声明。
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.GetCurrentProcessId.argtypes = []
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.PeekNamedPipe.restype = wintypes.BOOL
kernel32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.WaitNamedPipeW.restype = wintypes.BOOL
kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GetLastError.argtypes = []
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CancelSynchronousIo.restype = wintypes.BOOL
kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
]
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)
]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_ALL_ACCESS = 0x1F03FF


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def current_user_sid() -> str:
    """当前进程令牌的用户 SID 字符串（形如 `S-1-5-21-...-1001`）。

    管道 DACL 的 SDDL 要把这个 SID 写进去 —— 简写（`CU` = CREATOR OWNER 之类）
    在手写 SDDL 里语义不同；显式写 SID 是 spike 里验证过的那一种。
    """
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise CUError(ErrorCode.INTERNAL_ERROR,
                      f"OpenProcessToken 失败 err={ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        if size.value == 0:
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"GetTokenInformation 未返回所需长度 err={ctypes.get_last_error()}")
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buf, size.value, ctypes.byref(size)):
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"GetTokenInformation 失败 err={ctypes.get_last_error()}")
        user = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
        str_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(str_sid)):
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"ConvertSidToStringSidW 失败 err={ctypes.get_last_error()}")
        try:
            return str_sid.value or ""
        finally:
            kernel32.LocalFree(str_sid)
    finally:
        kernel32.CloseHandle(token)


def make_dacl_sddl(user_sid: str) -> str:
    """当前用户 + SYSTEM 全权，其余无权限（DEC-036）。

    这是本系统唯一的鉴权点：同一用户下的其他进程被视为可信边界内
    （已记录的残余风险，见 DEC-033）。
    """
    return f"D:(A;;GA;;;SY)(A;;GA;;;{user_sid})"


def _sddl_to_pointer(sddl: str) -> ctypes.c_void_p:
    psd = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(psd), None):
        raise CUError(ErrorCode.INTERNAL_ERROR,
                      f"SDDL 转换失败 err={ctypes.get_last_error()} sddl={sddl!r}")
    return psd


def is_process_alive(pid: int) -> bool:
    """进程是否仍在运行。用 `OpenProcess` 而不是 `wait`，因为 daemon 不是子进程。"""
    if pid <= 0:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class IpcServer:
    """命名管道的服务端：接受连接、把请求交给 handler、把返回值写回。

    `handler(request: dict) -> Any` 在**连接线程**里被调用，因此可以阻塞
    （桌面操作本来就是阻塞的）。它抛 `CUError` 会被转成结构化错误响应；
    抛其他异常转成 `internal_error`，绝不把栈暴露给客户端。
    """

    def __init__(
        self,
        handler: Callable[[dict], Any],
        pipe_name: str = PIPE_NAME,
        user_sid: str | None = None,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        idle_timeout: float = CONNECTION_IDLE_TIMEOUT,
    ) -> None:
        self.handler = handler
        self.pipe_name = pipe_name
        self.user_sid = user_sid or current_user_sid()
        self.max_message_bytes = max_message_bytes
        self.idle_timeout = idle_timeout
        self._sddl = make_dacl_sddl(self.user_sid)

        self._lock = threading.Lock()
        self._listening: int | None = None       # 正在等待连接的实例
        self._connections: dict[int, int] = {}   # 已连接句柄 → 处理线程 ident
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._acceptor_thread_id: int | None = None
        self._acceptor: threading.Thread | None = None
        #: 每处理一条消息后回调（daemon 用它做空闲计时）。
        self.on_activity: Callable[[], None] | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._acceptor is not None:
            return
        self._stop.clear()
        self._acceptor = threading.Thread(target=self._accept_loop, name="cu-ipc-accept", daemon=True)
        self._acceptor.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """优雅停止：取消 acceptor 的阻塞 `ConnectNamedPipe`，关掉所有连接，再等线程收敛。

        `CancelSynchronousIo` 是唯一能打断阻塞中 `ConnectNamedPipe` 的手段；
        关掉监听句柄对阻塞在同一调用上的线程无效（Win32 语义如此）。
        """
        self._stop.set()
        thread_id = self._acceptor_thread_id
        if thread_id:
            handle = kernel32.OpenThread(_THREAD_ALL_ACCESS, False, thread_id)
            if handle:
                kernel32.CancelSynchronousIo(handle)
                kernel32.CloseHandle(handle)

        with self._lock:
            listening = self._listening
            self._listening = None
            connections = list(self._connections.keys())
        if listening:
            kernel32.CloseHandle(listening)
        for handle in connections:
            kernel32.CloseHandle(handle)
        if self._acceptor is not None:
            self._acceptor.join(timeout=join_timeout)
        for thread in list(self._threads):
            thread.join(timeout=1.0)
        with self._lock:
            self._threads.clear()
            self._connections.clear()
        self._acceptor = None
        self._acceptor_thread_id = None

    # ---- acceptor ----

    def _accept_loop(self) -> None:
        self._acceptor_thread_id = kernel32.GetCurrentThreadId()
        while not self._stop.is_set():
            handle = self._create_instance()
            if handle is None:  # _create_instance 抛错时才会 None（错误已抛出）
                continue
            self._listening = handle
            connected = self._wait_for_client(handle)
            self._listening = None

            if self._stop.is_set():
                if connected:
                    kernel32.DisconnectNamedPipe(handle)
                kernel32.CloseHandle(handle)
                break
            if not connected:
                kernel32.CloseHandle(handle)
                time.sleep(0.05)
                continue

            thread = threading.Thread(
                target=self._serve_connection, args=(handle,),
                name="cu-ipc-conn", daemon=True,
            )
            with self._lock:
                self._threads.append(thread)
                if len(self._threads) > 64:  # 回收已结束的线程对象，别让列表无界增长
                    self._threads = [t for t in self._threads if t.is_alive()]
            thread.start()
            with self._lock:
                # `ident` 只有 start() 之后才有值，所以登记放在启动之后。
                self._connections[handle] = thread.ident or 0

    def _create_instance(self) -> int:
        psd = _sddl_to_pointer(self._sddl)
        sa = SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=psd,
            bInheritHandle=False,
        )
        try:
            handle = kernel32.CreateNamedPipeW(
                self.pipe_name,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES,
                65536,   # 出缓冲：大消息靠分块写，缓冲够大即可
                65536,   # 入缓冲
                0,       # 默认超时（仅对 WaitNamedPipe 有意义）
                ctypes.byref(sa),
            )
        finally:
            kernel32.LocalFree(psd)

        if not handle or handle == INVALID_HANDLE_VALUE:
            err = kernel32.GetLastError()
            if err == ERROR_ACCESS_DENIED:
                raise CUError(
                    ErrorCode.INTERNAL_ERROR,
                    "创建管道被拒绝（ERROR_ACCESS_DENIED）：同名管道可能由另一个用户的进程持有。",
                    {"err": err, "pipe": self.pipe_name},
                )
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"CreateNamedPipeW 失败 err={err}", {"err": err, "pipe": self.pipe_name})
        return handle

    def _wait_for_client(self, handle: int) -> bool:
        """等客户端连上来。连上返回 True；被取消/对端消失返回 False；其他错误抛出。"""
        kernel32.SetLastError(0)
        if kernel32.ConnectNamedPipe(handle, None):
            return True
        err = kernel32.GetLastError()
        if err == ERROR_PIPE_CONNECTED:
            return True          # 客户端在建实例与调用之间已经连上，属正常竞态
        if err in (ERROR_OPERATION_ABORTED, ERROR_NO_DATA, ERROR_PIPE_LISTENING):
            return False
        raise CUError(ErrorCode.INTERNAL_ERROR, f"ConnectNamedPipe 失败 err={err}", {"err": err})

    def _serve_connection(self, handle: int) -> None:
        try:
            buffer = bytearray()
            while not self._stop.is_set():
                line = _read_line_handle(handle, buffer, self.max_message_bytes, self.idle_timeout)
                if line is None:
                    break
                if self.on_activity is not None:
                    self.on_activity()
                response = self._dispatch(line)
                if response is not None:
                    _write_all(handle, dump_line(response))
        except OSError:
            pass  # 客户端断开是常态，不是错误
        except CUError:
            pass  # 超限等协议级问题：断开这条连接，不影响 daemon
        finally:
            with self._lock:
                self._connections.pop(handle, None)
                current = threading.current_thread()
                self._threads = [t for t in self._threads if t is not current]
            kernel32.DisconnectNamedPipe(handle)
            kernel32.CloseHandle(handle)

    def _dispatch(self, line: bytes) -> dict | None:
        request_id: Any = None
        try:
            message = load_line(line)
            request_id = message.get("id")
            method = message.get("method")
            if not isinstance(method, str):
                raise CUError(ErrorCode.INVALID_PARAMS, "请求缺少 method 字段")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise CUError(ErrorCode.INVALID_PARAMS, "params 必须是对象")
            result = self.handler({"method": method, "params": params, "id": request_id})
            if request_id is None:
                return None  # 通知，不回响应
            return ok_response(request_id, result)
        except CUError as exc:
            return error_response(request_id, APP_ERROR, exc.message, exc.to_rpc_data())
        except AppError as exc:
            return error_response(request_id, APP_ERROR, exc.message,
                                  {"code": exc.code, "hint": exc.hint, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 —— 边界：任何 handler 异常都要变成响应，不能断连
            cu = CUError(ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return error_response(request_id, APP_ERROR, cu.message, cu.to_rpc_data())


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class IpcClient:
    """连接 daemon 的一次性客户端。一次命令 = 一个连接（CLI 前端无状态）。"""

    def __init__(self, pipe_name: str = PIPE_NAME, connect_timeout: float = 10.0) -> None:
        self.pipe_name = pipe_name
        self.connect_timeout = connect_timeout
        self._handle: int | None = None
        self._buffer = bytearray()

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def connect(self) -> None:
        """等管道可用并连上。等不到就报「daemon 没在跑」—— 由上层决定是否拉起。"""
        deadline = time.monotonic() + self.connect_timeout
        delay = 0.02
        while True:
            kernel32.SetLastError(0)
            handle = kernel32.CreateFileW(
                self.pipe_name, GENERIC_READ | GENERIC_WRITE, 0, None,
                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
            )
            if handle and handle != INVALID_HANDLE_VALUE:
                self._handle = handle
                return
            err = kernel32.GetLastError()
            if err not in (ERROR_PIPE_BUSY, ERROR_FILE_NOT_FOUND):
                raise CUError(ErrorCode.INTERNAL_ERROR,
                              f"连接 daemon 管道失败 err={err}", {"pipe": self.pipe_name})
            if time.monotonic() >= deadline:
                raise CUError(ErrorCode.INTERNAL_ERROR, "daemon 未在运行（管道不可用）",
                              {"pipe": self.pipe_name, "err": err})
            # 管道忙 = 实例都在服务别的连接；等一会儿再来。
            kernel32.WaitNamedPipeW(self.pipe_name, 100)
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)

    def call(self, message: dict, timeout: float = 300.0) -> dict:
        """发一条请求，读回一条响应。

        超时抛错，**不自动重试**（DEC-041：写命令重试可能等于再点一次）。
        """
        if self._handle is None:
            raise CUError(ErrorCode.INTERNAL_ERROR, "尚未连接")
        _write_all(self._handle, dump_line(message))
        line = _read_line_handle(self._handle, self._buffer, MAX_MESSAGE_BYTES, timeout)
        if line is None:
            raise CUError(ErrorCode.INTERNAL_ERROR, "daemon 在响应前关闭了连接")
        response = load_line(line)
        if "error" in response:
            error = response["error"] or {}
            data = error.get("data") or {}
            raise AppError(
                code=str(data.get("code", "internal_error")),
                message=str(error.get("message", "")),
                hint=str(data.get("hint", "")),
                detail=dict(data.get("detail") or {}),
            )
        return response.get("result")

    def close(self) -> None:
        if self._handle is not None:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> IpcClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# daemon 单实例锁
# ---------------------------------------------------------------------------


class DaemonLock:
    """`~/.computer-use/daemon.lock` —— 防止两个 daemon 同时跑（data-model §3.1）。

    判据是文件锁（`msvcrt.locking`）：同用户并发启动时，后者拿不到锁即退出。
    文件内容里的 PID 只用于诊断（谁占着），不参与判定 —— 文件锁本身在进程死亡时
    由内核释放，因此不存在「PID 被复用」导致的误判。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self.pid = os.getpid()

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(self.pid).encode("ascii"))
            os.fsync(fd)
        except OSError:
            pass  # 写 PID 只是诊断信息，失败不影响锁的效力
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # 解锁失败无碍：进程退出时内核会释放
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> DaemonLock:
        if not self.acquire():
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          f"已有 daemon 在运行（{self.path}）")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_lock_pid(path: Path) -> int | None:
    """读锁文件里记录的 PID。读不到或内容非法返回 None。仅供诊断。"""
    try:
        raw = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 底层读写
# ---------------------------------------------------------------------------


def _read_line_handle(handle: int, buffer: bytearray, limit: int, timeout: float) -> bytes | None:
    """从管道句柄读到换行为止。返回**不含换行**的一行；对端关闭返回 None。

    `buffer` 由调用方持有并在多次调用间复用 —— 一次 `ReadFile` 可能读回半条或
    一条半消息，残留必须留给下一次调用。
    """
    while True:
        index = buffer.find(b"\n")
        if index >= 0:
            line = bytes(buffer[:index])
            del buffer[: index + 1]
            return line
        if len(buffer) > limit:
            raise CUError(ErrorCode.INVALID_PARAMS, f"单条消息超过 {limit} 字节上限")

        chunk = _read_chunk(handle, timeout)
        if chunk is None:
            if buffer:
                line = bytes(buffer)
                buffer.clear()
                return line
            return None
        buffer += chunk


def _read_chunk(handle: int, timeout: float) -> bytes | None:
    """读一块。对端关闭返回 None。

    **必须先用 `PeekNamedPipe` 探可用字节**：管道是阻塞模式，直接 `ReadFile`
    在没有数据时会一直挂住，超时形同虚设。`PeekNamedPipe` 在任何模式下都不阻塞，
    所以「先探再读」是这套不引入重叠 I/O 的方案里唯一能实现超时的路径。
    """
    deadline = time.monotonic() + timeout
    available = wintypes.DWORD(0)
    while True:
        kernel32.SetLastError(0)
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            err = kernel32.GetLastError()
            if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED,
                       ERROR_PIPE_LISTENING):
                return None
            raise OSError(f"PeekNamedPipe err={err}")
        if available.value:
            break
        if time.monotonic() >= deadline:
            raise OSError(f"读取超时（{timeout}s）")
        time.sleep(0.005)

    size = min(available.value, 65536)
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD(0)
    kernel32.SetLastError(0)
    if not kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None):
        err = kernel32.GetLastError()
        if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
            return None
        raise OSError(f"ReadFile err={err}")
    if read.value == 0:
        return None
    return buf.raw[: read.value]


def _write_all(handle: int, data: bytes) -> None:
    """写完整块。管道缓冲满时 `WriteFile` 会阻塞到对端读走部分数据。"""
    offset = 0
    written = wintypes.DWORD(0)
    while offset < len(data):
        chunk = data[offset : offset + 65536]
        kernel32.SetLastError(0)
        ok = kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None)
        if not ok:
            err = kernel32.GetLastError()
            if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
                raise OSError(f"写入失败：对端已关闭 err={err}")
            raise OSError(f"WriteFile err={err}")
        if written.value == 0:
            raise OSError("WriteFile 写入 0 字节")
        offset += written.value
