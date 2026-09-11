"""CLI 端到端真机测试 —— 走真实 CLI 进程 + 真实 daemon（不是同进程调用）。

为什么要有这一层：单元测试验证的是模块，`ipc_smoke` 验证的是传输，而这条链
**只有把真实进程串起来才会暴露**「客户端等不及 daemon 冷启动」这类竞态 ——
它正是本轮实际踩到的：daemon 明明起来了，CLI 却报管道不存在。

跑法：
    .venv/Scripts/python.exe core/tests/native/cli_e2e.py

脚本自己拉一条**独立管道**（`cu-e2e-cli`），不碰用户默认 daemon；跑完会把它停掉。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

#: 独立管道：默认名留给用户真实的 daemon，测试不该去动它。
PIPE = chr(92) * 2 + ".\\pipe\\cu-e2e-cli"
RESULTS: list[tuple[str, str, str]] = []


def record(label: str, ok: bool, detail: str) -> None:
    RESULTS.append((label, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}\n        {detail}")


def cu(*args: str, expect_ok: bool = True, timeout: float = 90.0) -> tuple[int, str, str]:
    """跑一次真实 CLI 进程。返回 (退出码, stdout, stderr)。"""
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "cu", *args],
        cwd=str(CORE), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def main() -> int:
    # ---- T1 daemon 冷启动 ----
    started = time.monotonic()
    code, out, err = cu("daemon", "status")
    elapsed = time.monotonic() - started
    record("T1 CLI 冷启动自动拉起 daemon", code == 0 and "pid" in out,
           f"exit={code} 耗时={elapsed:.2f}s · {out.splitlines()[0] if out else err}")

    # ---- T2 begin ----
    code, out, err = cu("begin", "--agent-hint", "native-e2e")
    session_id = ""
    directory = ""
    for line in out.splitlines():
        if line.startswith("session "):
            session_id = line.split(" ", 1)[1].strip()
        if line.startswith("dir "):
            directory = line.split(" ", 1)[1].strip()
    record("T2 begin 创建会话", code == 0 and session_id.startswith("s-") and directory,
           f"exit={code} session={session_id} dir={directory}")

    # ---- T3 会话目录真的有文件 ----
    ops = Path(directory) / "ops.md" if directory else None
    manifest = Path(directory) / "session.json" if directory else None
    record("T3 会话目录落盘 ops.md + session.json",
           bool(ops and ops.exists() and manifest and manifest.exists()),
           f"ops.md={ops.exists() if ops else False} session.json={manifest.exists() if manifest else False}")

    # ---- T4 windows ----
    code, out, err = cu("windows")
    lines = [line for line in out.splitlines() if line.startswith("hwnd=")]
    record("T4 windows 列出窗口", code == 0 and len(lines) >= 1,
           f"exit={code} 共 {len(lines)} 行 · 首行：{lines[0][:100] if lines else err[:100]}")

    # ---- T5 windows --json（字段契约） ----
    code, out, err = cu("--json", "windows")
    parsed: dict = {}
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        pass
    rows = (parsed.get("result") or {}).get("windows") or []
    required = {"hwnd", "title", "pid", "process", "rect", "monitor",
                "is_foreground", "is_minimized", "elevated"}
    missing = required - set(rows[0]) if rows else required
    record("T5 --json 字段契约", code == 0 and rows and not missing,
           f"exit={code} {len(rows)} 个窗口 · 缺失字段={sorted(missing)}")

    # ---- T6 screenshot --full ----
    code, out, err = cu("screenshot", "--full", "--session", session_id)
    shot_path = Path(out.split()[0]) if out else None
    exists = bool(shot_path and (Path(directory) / shot_path).exists())
    origin_ok = "origin=0,0" in out
    record("T6 全屏截图 + origin=0,0", code == 0 and exists and origin_ok,
           f"exit={code} {out if out else err[:200]}")

    # ---- T7 describe 强制必填（DEC-019） ----
    code, out, err = cu("click", "100", "100", "--session", session_id)
    record("T7 写命令缺 --describe 报错", code == 2 and "describe_required" in err,
           f"exit={code}（应 2）· {err.splitlines()[0] if err else out}")

    # ---- T8 未知错误码分类：会话不存在 → 退出码 3 ----
    code, out, err = cu("screenshot", "--full", "--session", "s-does-not-exist")
    record("T8 会话不存在 → 退出码 3", code == 3 and "session_not_found" in err,
           f"exit={code}（应 3）· {err.splitlines()[0] if err else out}")

    # ---- T9 危险键黑名单（DEC-019） ----
    code, out, err = cu("key", "win+l", "--session", session_id,
                        "--describe", "测试黑名单拦截")
    record("T9 危险键被拦截", code == 2 and "dangerous_key_blocked" in err,
           f"exit={code}（应 2）· {err.splitlines()[0] if err else out}")

    # ---- T10 config show/set 往返 ----
    code, out, err = cu("config", "set", "overlay_hold_seconds", "7")
    code2, out2, _ = cu("config", "show")
    record("T10 config set/show 往返", code == 0 and '"overlay_hold_seconds": 7' in out2,
           f"set exit={code} · show 含新值={'\"overlay_hold_seconds\": 7' in out2}")
    cu("config", "set", "overlay_hold_seconds", "5")     # 还原

    # ---- T11 session list 与 end ----
    code, out, err = cu("session", "list")
    listed = session_id in out
    code2, out2, err2 = cu("session", "end", "--session", session_id)
    record("T11 session list / end", code == 0 and listed and code2 == 0,
           f"list 含本会话={listed} · end exit={code2} {out2 or err2}")

    # ---- T12 daemon stop ----
    code, out, err = cu("daemon", "stop")
    stopped = code == 0 and "停止" in out
    time.sleep(1.0)
    code2, _out2, _err2 = cu("daemon", "status", timeout=60)   # 会自动重新拉起
    record("T12 daemon stop 后可再次拉起", stopped and code2 == 0,
           f"stop exit={code} · 再次 status exit={code2}")

    cu("daemon", "stop")

    print("\n===== CLI 端到端结果 =====")
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    for label, verdict, detail in RESULTS:
        print(f"[{verdict:4}] {label}  —— {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
