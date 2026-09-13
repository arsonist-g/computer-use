"""验收补充：**并发首次运行**（本轮新增）。

## 为什么要有这个脚本

任务清单 T3 里的一条：**两个 agent 同时第一次调 CLI** 会怎样？这一条在本轮之前没人验过，
而它恰恰是「装到别人机器上会出事」的典型场景 —— 用户可能同时开着两个 agent 会话，
它们会在同一瞬间各自触发：建 base 环境（uv）、拉起 daemon、建数据目录。

要验的是**并发下不出事**，不是「更快」：
1. 两个进程都**不该**报「管道不存在」之类的假失败；
2. 只应有**一个** daemon 活下来（单实例语义，DEC-035）；
3. 数据目录不该留下半成品（半建的 venv、两个 daemon.lock 之类）。

## 跑法

    .venv/Scripts/python.exe core/tests/native/acceptance_concurrent_start.py

## 隔离

两个进程指向**同一个全新的**临时 home（这才是「并发首次运行」），但用同一个独立管道名 ——
不碰真实的 `~/.computer-use/`。反斜杠逐字符构造（`BS = chr(92)`）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()
ROOT = CORE.parent
LAUNCHER = ROOT / "bin" / "computer-use.mjs"
NODE = shutil.which("node")
BS = chr(92)
CONCURRENCY = 2


def main() -> int:
    if NODE is None:
        print("找不到 node，无法进行")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="cu-conc-"))
    home = tmp / "home"
    home.mkdir()
    data = home / ".computer-use"
    pipe = BS * 2 + "." + BS + "pipe" + BS + f"cu-conc-{os.getpid()}"
    env = {
        **os.environ,
        "USERPROFILE": str(home),
        "COMPUTER_USE_HOME": str(data),
        "COMPUTER_USE_PIPE": pipe,
        "PYTHONIOENCODING": "utf-8",
    }

    print("=" * 78)
    print(f"并发首次运行：{CONCURRENCY} 个进程同时跑 `windows`（同一个全新 home）")
    print(f"home={home}")
    print("=" * 78)

    try:
        started = time.monotonic()
        procs = [subprocess.Popen([NODE, str(LAUNCHER), "windows"], env=env, cwd=str(ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8", errors="replace")
                 for _ in range(CONCURRENCY)]
        outs = [proc.communicate(timeout=600) for proc in procs]
        elapsed = time.monotonic() - started

        results = []
        for index, (proc, (out, err)) in enumerate(zip(procs, outs, strict=True), start=1):
            ok = proc.returncode == 0 and "hwnd=" in (out or "")
            results.append((index, proc.returncode, ok, (out or err or "").strip().splitlines()[:1]))
            print(f"  #{index} exit={proc.returncode} 成功={ok} · {results[-1][3]}")

        # 数一数有几个 daemon 活着（按命令行里的管道名认）
        listing = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
                                  f"Where-Object {{ $_.CommandLine -like '*cu.daemon*' -and $_.CommandLine -like '*{pipe}*' }} | "
                                  "Select-Object -ExpandProperty ProcessId"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        daemons = [line.strip() for line in (listing.stdout or "").splitlines() if line.strip()]
        print(f"  daemon 进程数 = {len(daemons)}（{daemons}）")
        print(f"  数据目录内容 = {sorted(item.name for item in data.iterdir()) if data.exists() else '不存在'}")

        all_ok = all(row[2] for row in results)
        one_daemon = len(daemons) == 1
        # 收尾：不要留下正在跑的 daemon
        subprocess.run([NODE, str(LAUNCHER), "daemon", "stop"], env=env, cwd=str(ROOT),
                       capture_output=True, timeout=120)

        verdict = "通过" if (all_ok and one_daemon) else "失败"
        print(f"\n[{verdict}] 总耗时 {elapsed:.1f}s · "
              f"{CONCURRENCY} 个并发进程全部成功={all_ok} · 只有一个 daemon={one_daemon}"
              + ("" if verdict == "通过" else " ⟵ 并发首次运行有问题"))
        return 0 if verdict == "通过" else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
