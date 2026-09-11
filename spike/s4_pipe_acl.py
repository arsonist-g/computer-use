"""S4 —— 验证 ctypes 手写 SDDL + 命名管道 DACL 是否真的被内核执行。

回答一个问题：
  Q4 不引入 pywin32，能否用 ctypes 建一条「只有当前用户和 SYSTEM 能连」的命名管道？
     并且这个限制究竟是"写了个 DACL 但内核没管"，还是真的生效？

难点：证明"其他用户连不上"本需要第二个账户。这里用**自我证伪**绕过：
  建一条 DACL 只授权 SYSTEM、**不授权当前用户**的管道。
  如果当前用户连它也被拒绝 -> DACL 确实被内核执行，那么"只授权当前用户"的那条自然也对其他用户生效。
  如果当前用户照样能连 -> DACL 没生效，方案作废。

三组：
  T1 授权当前用户 + SYSTEM  -> 当前用户应能连上（正例）
  T2 只授权 SYSTEM          -> 当前用户应被拒绝（证伪组）
  T3 授予 Everyone(WD)      -> 当前用户应能连上（对照组，证明 T2 的失败不是因为别的原因）
"""
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TokenUser = 1
ERROR_ACCESS_DENIED = 5
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
# GetCurrentProcess 返回伪句柄 (HANDLE)-1。不声明 restype，ctypes 默认按 32 位 int 返回，
# 会把 -1 截断成 0xFFFFFFFF，随后 OpenProcessToken 报 ERROR_INVALID_HANDLE(6)。
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]


def current_user_sid_string() -> str:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(f"OpenProcessToken err={ctypes.get_last_error()}")

    size = wintypes.DWORD(0)
    advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if not advapi32.GetTokenInformation(token, TokenUser, buf, size.value, ctypes.byref(size)):
        raise OSError(f"GetTokenInformation err={ctypes.get_last_error()}")
    tu = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents

    str_sid = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(tu.User.Sid, ctypes.byref(str_sid)):
        raise OSError(f"ConvertSidToStringSidW err={ctypes.get_last_error()}")
    sid = str_sid.value
    kernel32.LocalFree(str_sid)
    kernel32.CloseHandle(token)
    return sid


def make_security_attributes(sddl: str):
    psd = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(psd), None):
        raise OSError(f"ConvertStringSecurityDescriptorToSecurityDescriptorW err="
                      f"{ctypes.get_last_error()} sddl={sddl!r}")
    sa = SECURITY_ATTRIBUTES(nLength=ctypes.sizeof(SECURITY_ATTRIBUTES),
                             lpSecurityDescriptor=psd, bInheritHandle=False)
    return sa, psd


def try_roundtrip(pipe_name: str, sddl: str):
    """建管道并用当前用户尝试连接。返回 (可连接?, 错误码)。"""
    sa, psd = make_security_attributes(sddl)
    h_pipe = kernel32.CreateNamedPipeW(
        pipe_name, PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1, 4096, 4096, 0, ctypes.byref(sa))
    if h_pipe == INVALID_HANDLE_VALUE or not h_pipe:
        err = ctypes.get_last_error()
        kernel32.LocalFree(psd)
        return None, f"CreateNamedPipeW 失败 err={err}"

    h_client = kernel32.CreateFileW(
        pipe_name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    err = ctypes.get_last_error()
    connected = h_client not in (INVALID_HANDLE_VALUE, None)

    if connected:
        kernel32.CloseHandle(h_client)
    kernel32.CloseHandle(h_pipe)
    kernel32.LocalFree(psd)
    return connected, (0 if connected else err)


def main():
    sid = current_user_sid_string()
    print(f"当前用户 SID = {sid}")

    base = r"\\.\pipe\cu-spike"
    cases = [
        ("T1 授权 当前用户+SYSTEM", f"D:(A;;GA;;;SY)(A;;GA;;;{sid})", True),
        ("T2 只授权 SYSTEM（证伪组）", "D:(A;;GA;;;SY)", False),
        ("T3 授权 Everyone（对照组）", "D:(A;;GA;;;WD)", True),
    ]

    results = []
    for i, (label, sddl, expect_allowed) in enumerate(cases):
        name = f"{base}-{i}"
        ok, info = try_roundtrip(name, sddl)
        if ok is None:
            results.append((label, "ERROR", info))
            continue
        if expect_allowed:
            verdict = "PASS" if ok else "FAIL"
            detail = f"当前用户可连接={ok} err={info}"
        else:
            # 证伪组：被拒绝才是我们要的结果
            denied = (not ok) and info == ERROR_ACCESS_DENIED
            verdict = "PASS" if denied else "FAIL"
            detail = (f"当前用户可连接={ok} err={info}"
                      + ("  -> DACL 被内核执行" if denied
                         else "  -> DACL 没生效，ctypes 方案作废"))
        results.append((label, verdict, detail))
        print(f"  sddl={sddl}")

    print("\n===== S4 结果 =====")
    for label, verdict, detail in results:
        print(f"[{verdict:5}] {label}\n          {detail}")


if __name__ == "__main__":
    main()
