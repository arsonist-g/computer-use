"""验收清单 §9（设计阶段遗留项）里**能够实测**的那几条。

对应 `core/tests/acceptance.md` §9 的 9.5（架构 §2.1 的全部 `[待测]` 性能目标）
与 9.9（写序列兜底阈值 30s 是否够用）。§9 其余各条的结论写在那份清单里 ——
它们要么需要第二台显示器 / 独占全屏应用 / 提权应用（本机不可构造），
要么是「有了数据之后要做的设计决策」，不是可以跑出来的事实。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_leftovers.py
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _winutil as W  # noqa: E402
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

from cu.desktop import capture as capture_mod  # noqa: E402
from cu.desktop import windows as windows_mod  # noqa: E402

PIPE = local_pipe()
RESULTS: list[tuple[str, str, str]] = []
OUT = Path(__file__).parent / "out" / "acceptance-leftovers"


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def cu(*args: str, timeout: float = 120.0) -> tuple[int, str, str]:
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "cu", *args], cwd=str(CORE), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def cu_json(*args: str, timeout: float = 120.0) -> dict:
    _code, out, _err = cu("--json", *args, timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# 9.5 架构 §2.1 的 6 个 [待测] 性能目标
# ---------------------------------------------------------------------------


def _median_ms(fn, rounds: int = 5) -> float:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def check_9_5() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    session = ((cu_json("begin", "--agent-hint", "acceptance-9.5").get("result") or {})
               .get("session_id") or "")
    hwnd = W.spawn_notepad()
    if hwnd is None:
        record("9.5", "失败", "起不来用于测性能的靶子窗口")
        return
    try:
        W.move(hwnd, 200, 150, 1280, 720, top=True)
        time.sleep(1.0)                     # 1280x720 ≈ 1080p 量级的单窗口
        info = windows_mod.check_hwnd(hwnd).info
        monitors = windows_mod.display_context().monitors
        screen = monitors[0].rect
        screen_size = (screen[2] - screen[0], screen[3] - screen[1])

        # ① 窗口枚举
        enum_ms = _median_ms(
            lambda: windows_mod.enumerate_windows(windows_mod.display_context().monitors, True), 5)
        # ② 单窗口截图（约 1080p 量级）
        shot_ms = _median_ms(
            lambda: capture_mod.capture_window(hwnd, OUT, 901, info, "png"), 3)
        # ③ 全屏截图（本机 3440x1440 —— 比 4K 小一档，结论里写明）
        full_ms = _median_ms(lambda: capture_mod.capture_full(0, OUT, 902), 3)
        # ④ CLI 冷路径（daemon 已在跑）：解释器启动 + import + IPC + 渲染
        cli_ms = _median_ms(lambda: cu("windows"), 5)
        # ⑤ 写操作本体（不含前摇与拟人移动）：daemon 自报的 total_ms
        write_total = None
        code, out, _err = cu("key", "f24", "--session", session, "--describe",
                             "验收 9.5：量写操作本体耗时", "--end")
        if code == 0 and "total=" in out:
            write_total = int(out.split("total=")[1].split("ms")[0])
        # ⑥ daemon 常驻内存
        status = (cu_json("daemon", "status").get("result") or {})
        rss = status.get("resident_bytes")

        rows = [
            ("窗口枚举", f"{enum_ms:.1f}ms", "< 30ms", enum_ms < 30),
            ("截图 单窗口 1280x720", f"{shot_ms:.0f}ms", "< 150ms（目标写的是 1080p 单窗口）",
             shot_ms < 150),
            ("截图 全屏 {}x{}".format(*screen_size), f"{full_ms:.0f}ms",
             "< 400ms（目标写的是 4K；本机是这一档）", full_ms < 400),
            ("CLI 冷路径（daemon 在跑）", f"{cli_ms:.0f}ms", "< 200ms", cli_ms < 200),
            ("写操作本体（不含前摇/拟人移动）",
             f"{write_total}ms" if write_total is not None else "未取到",
             "< 150ms", write_total is not None and write_total < 150),
            ("daemon 常驻内存",
             f"{rss / 1024 / 1024:.0f}MB" if rss else "未取到", "50~100MB",
             bool(rss) and 50 * 1024**2 <= rss <= 100 * 1024**2),
        ]
        for name, got, want, ok in rows:
            record("9.5", "通过" if ok else "失败", f"{name}: 实测 {got} / 目标 {want}")
        if session:
            cu("session", "end", "--session", session)
    except Exception as exc:  # noqa: BLE001
        record("9.5", "失败", f"{type(exc).__name__}: {exc}")
    finally:
        W.close(hwnd)


# ---------------------------------------------------------------------------
# 9.9 兜底阈值 30s：调用方忘了带 --end / --continue 时会发生什么
# ---------------------------------------------------------------------------


def check_9_9() -> None:
    """发一条**不带任何续期标志**的写命令，量覆盖层与输入封锁多久才自己松开。

    DEC-045 把这个阈值定为 30s 并降级为兜底。这条要回答的就是那个数是否照做。
    """
    session = ((cu_json("begin", "--agent-hint", "acceptance-9.9").get("result") or {})
               .get("session_id") or "")
    code, out, err = cu("scroll", "0", "0", "--session", session,
                        "--describe", "验收 9.9：不带 --end/--continue 的写命令")
    if code != 0:
        record("9.9", "失败", f"写命令失败：{err.splitlines()[:1]}")
        return
    started = time.monotonic()
    released_at = None
    overlay_off_at = None
    while time.monotonic() - started < 55:
        payload = cu_json("daemon", "status").get("result") or {}
        if overlay_off_at is None and payload.get("overlay_state") == "off":
            overlay_off_at = time.monotonic() - started
        if not payload.get("input_blocked"):
            released_at = time.monotonic() - started
            break
        time.sleep(1.0)
    cu("session", "end", "--session", session)
    if released_at is None:
        record("9.9", "失败", "55 秒内输入封锁没有自行解除")
        return
    # 兜底阈值 30s + 退出保留期 0.5s；给一点调度余量。
    ok = 28.0 <= released_at <= 35.0
    record("9.9", "通过" if ok else "失败",
           f"覆盖层退场于 {overlay_off_at:.1f}s · 输入解封于 {released_at:.1f}s"
           f"（配置的兜底阈值 30s + 退出保留期 0.5s）")


# ---------------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 72)
    print("验收 §9 遗留项（能实测的两条）")
    print("=" * 72)
    check_9_9()          # 先跑：9.9 结束时会自然把封锁放开
    check_9_5()

    print("\n===== §9 结果 =====")
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
