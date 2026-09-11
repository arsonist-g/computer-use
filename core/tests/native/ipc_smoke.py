"""IPC 管道的真机冒烟测试。

单元测试无法覆盖真实的 Win32 命名管道 —— DACL、ConnectNamedPipe 的竞态、
PeekNamedPipe 的分块语义都只有真机能验。这个脚本就是那层验证：
把 daemon 跑在本进程的一个线程里（管道本来就是跨进程的，同进程跨线程同样合法），
用真实客户端走一遍往返。

它检验的不是业务逻辑，而是**传输是否可靠**：
  T1 基本往返
  T2 中文 / emoji 往返（`ensure_ascii=False` 的实际效果）
  T3 错误响应能还原成 AppError
  T4 单连接多次调用（分块读的缓冲区复用）
  T5 大消息（> 65536，跨多次 ReadFile）
  T6 多个并发客户端
  T7 优雅停止后端口释放
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# 本脚本会打印中文与 emoji，而 Windows 控制台默认 GBK —— 显式切 UTF-8。
# 产品 CLI 也必须做同样的事（回归：真机跑之前没人发现这个问题）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from cu.errors import CUError, ErrorCode  # noqa: E402
from cu.ipc import IpcClient, IpcServer  # noqa: E402
from cu.protocol import AppError, make_request  # noqa: E402

PIPE = r"\\.\pipe\computer-use-smoke"
RESULTS: list[tuple[str, str, str]] = []


def record(label: str, ok: bool, detail: str) -> None:
    RESULTS.append((label, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL':4}] {label}\n        {detail}")


def handler(request: dict):
    method = request["method"]
    params = request["params"]
    if method == "echo":
        return {"echo": params.get("value")}
    if method == "boom":
        raise CUError(ErrorCode.WINDOW_NOT_FOUND, "窗口不存在", {"hwnd": "0x00000001"})
    if method == "crash":
        raise RuntimeError("handler 内部炸了")
    if method == "slow":
        time.sleep(params.get("seconds", 0.2))
        return {"slept": params.get("seconds")}
    raise CUError(ErrorCode.INVALID_PARAMS, f"未知方法 {method}")


def main() -> int:
    server = IpcServer(handler, pipe_name=PIPE)
    server.start()
    time.sleep(0.05)  # 等 acceptor 建好实例

    # ---- T1 基本往返 ----
    try:
        with IpcClient(PIPE) as client:
            result = client.call(make_request("echo", {"value": "hello"}))
        record("T1 基本往返", result == {"echo": "hello"}, f"result={result}")
    except Exception as exc:
        record("T1 基本往返", False, f"{type(exc).__name__}: {exc}")

    # ---- T2 中文 / emoji ----
    text = "未命名 - 记事本 🎯\ttab\n换行"
    try:
        with IpcClient(PIPE) as client:
            result = client.call(make_request("echo", {"value": text}))
        record("T2 中文/emoji/控制字符往返", result == {"echo": text}, f"result={result!r}")
    except Exception as exc:
        record("T2 中文/emoji/控制字符往返", False, f"{type(exc).__name__}: {exc}")

    # ---- T3 错误响应还原 ----
    try:
        with IpcClient(PIPE) as client:
            client.call(make_request("boom"))
        record("T3 错误响应还原", False, "本应抛 AppError，却成功返回")
    except AppError as exc:
        cu = exc.as_cu_error()
        ok = (cu.code is ErrorCode.WINDOW_NOT_FOUND and cu.exit_code.value == 4
              and cu.detail.get("hwnd") == "0x00000001")
        record("T3 错误响应还原", ok,
               f"code={cu.code} exit={cu.exit_code.value} detail={cu.detail}")
    except Exception as exc:
        record("T3 错误响应还原", False, f"抛了 {type(exc).__name__}: {exc}")

    # ---- T4 单连接多次调用 ----
    try:
        with IpcClient(PIPE) as client:
            values = [client.call(make_request("echo", {"value": f"v{i}"}))["echo"] for i in range(20)]
        record("T4 单连接 20 次调用", values == [f"v{i}" for i in range(20)], f"最后一个={values[-1]}")
    except Exception as exc:
        record("T4 单连接 20 次调用", False, f"{type(exc).__name__}: {exc}")

    # ---- T5 大消息（跨多次 ReadFile） ----
    blob = "中" * 100_000  # UTF-8 后 300KB，远超 65536 的单次读块
    try:
        with IpcClient(PIPE) as client:
            result = client.call(make_request("echo", {"value": blob}), timeout=30)
        record("T5 大消息 300KB", result == {"echo": blob}, f"长度={len(result['echo'])}")
    except Exception as exc:
        record("T5 大消息 300KB", False, f"{type(exc).__name__}: {exc}")

    # ---- T6 并发客户端 ----
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            with IpcClient(PIPE) as client:
                got = client.call(make_request("echo", {"value": f"w{index}"}))
                if got["echo"] != f"w{index}":
                    errors.append(f"w{index} 收到 {got}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"w{index}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    record("T6 6 个并发客户端", not errors, f"errors={errors[:3]}")

    # ---- T7 优雅停止 ----
    started = time.monotonic()
    server.stop()
    elapsed = time.monotonic() - started
    stopped_ok = elapsed < 3.0
    # 端口已释放：再连应失败
    try:
        with IpcClient(PIPE, connect_timeout=1.0) as client:
            client.call(make_request("echo", {"value": "x"}), timeout=1)
        freed = False
    except Exception:  # noqa: BLE001
        freed = True
    record("T7 优雅停止并释放端口", stopped_ok and freed,
           f"stop 耗时={elapsed:.2f}s 端口已释放={freed}")

    print("\n===== IPC 冒烟结果 =====")
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    for label, verdict, detail in RESULTS:
        print(f"[{verdict:4}] {label}  —— {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
