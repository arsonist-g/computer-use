"""验收 §2.3 写操作前置的「期望身份」（Q-024 / DEC-013 第 1 层）—— 真实 CLI + 真实 daemon。

这条检查此前**不可达**：`windows.check_hwnd()` 的 `expect_pid` / `expect_class`
全仓没有任何调用点传过（`real.py` 是唯一调用点，只传了 hwnd），所以「hwnd 被系统
复用给了别的进程」从未被拦住，`window_stale` 这个错误码也就一直发不出来。

实测四种形态（**同一个会话内按顺序做**，理由见下）：

  | # | 现场 | 判据 |
  |---|---|---|
  | ① | 会话里**还没有**该 hwnd 的截图 | 放行（约束 4：无从比对就别拦） |
  | ② | 会话里那张截图的 pid/class 与现场一致 | 放行（exit 0） |
  | ③ | 记录里的 pid 与现场不符（hwnd 被复用） | `window_stale`（exit 4） |
  | ④ | 记录里的窗口类与现场不符 | `window_stale`（exit 4） |

三条取舍，都写在脚本里而不是靠口头约定：

1. **四种形态共用一个会话**：写锁是**会话持有、跨命令保持**的（DEC-004），
   第二个会话在第一个结束前拿不到锁，只会得到一个 `lock_timeout` —— 那不是被测的东西。
   所以「没有记录」这一条必须在**截图之前**做，天然落在同一个会话里。
2. **用 `move` 而不是 `click`**：`click` 会真的按下去。这条检查挡的是「点到**另一个**
   窗口」，而 `move` 与 `click` 走的是同一条 `_preflight`。失败的那两条在派发任何输入
   **之前**就中止了 —— 全程只可能移动光标，不会按下任何键。
3. **会重启 daemon 一次**：会话清单在活动会话的**内存**里（`Sessions._active`），
   改盘上的 `session.json` 对活着的会话不起作用。重启之后该会话不在内存里，
   `get()` 会从盘上重读 —— 这也正是真机上的真实情形（旧 daemon 崩过一次）。

`--json` 一律写在**子命令之后**（契约 §1.5 的写法），顺带让失败那两条也断言
「错误信封确实在 **stdout** 上」（Q-025）。

用的是本机默认管道与默认 data dir，运行期间不要有别的会话在操作桌面。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

PIPE = local_pipe()
RESULTS: list[tuple[str, str, str]] = []


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def cu(*args: str, timeout: float = 90.0) -> tuple[int, str, str]:
    """跑一次真实 CLI 进程。返回 (退出码, stdout, stderr)。"""
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "cu", *args],
        cwd=str(CORE), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def cu_json(*args: str, timeout: float = 90.0) -> dict:
    _code, out, _err = cu(*args, "--json", timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def begin(hint: str) -> tuple[str, Path]:
    _code, out, _err = cu("begin", "--agent-hint", hint)
    session_id, directory = "", Path()
    for line in out.splitlines():
        if line.startswith("session "):
            session_id = line.split(" ", 1)[1].strip()
        if line.startswith("dir "):
            directory = Path(line.split(" ", 1)[1].strip())
    return session_id, directory


def daemon_pid() -> int | None:
    return (cu_json("daemon", "status").get("result") or {}).get("pid")


def restart_daemon(timeout: float = 40.0) -> int | None:
    """让 daemon 重来一轮：`daemon stop` 之后，下一次调用会自动拉起一个新的。

    `daemon status` 在无 daemon 时会**自动拉起**一个 —— 那正是我们要的，
    所以这里用「pid 变了」当新实例就绪的判据（而不是等管道消失）。
    """
    before = daemon_pid()
    cu("daemon", "stop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        after = daemon_pid()
        if after is not None and after != before:
            return after
    return None


def pick_target() -> dict | None:
    """挑一个稳定、非提权、未最小化的窗口当靶子。

    优先 `explorer.exe`（长期存在的桌面进程），它不会在脚本跑的中途消失。
    """
    rows = ((cu_json("windows").get("result") or {}).get("windows")) or []
    usable = [row for row in rows
              if not row.get("elevated") and not row.get("is_minimized")
              and (row.get("title") or "").strip()]
    if not usable:
        return None
    for row in usable:
        if (row.get("process") or "").lower() == "explorer.exe":
            return row
    return usable[0]


def tamper_manifest(session_dir: Path, *, pid: int | None = None,
                    klass: str | None = None) -> str:
    """改掉会话清单里最后一次窗口截图记下的 pid / 窗口类。

    **这就是「hwnd 被系统复用给了别的进程」的等价物**：现场变了，记录还是旧的。
    真实的复用没法按需构造（要等系统回收并重发同一个 hwnd），所以直接改记录。
    """
    path = session_dir / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = data.get("screenshots") or []
    window = next((shot.get("window") for shot in reversed(shots) if shot.get("window")), None)
    if window is None:
        raise RuntimeError(f"清单里没有带 window 的截图记录：{path}")
    if pid is not None:
        window["pid"] = pid
    if klass is not None:
        window["class"] = klass
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"记录已改为 pid={window['pid']} class={window['class']!r}"


def move_to(hwnd: str, point: tuple[int, int], session: str, note: str) -> tuple[int, str, str]:
    """一次写操作。`--json` 与 `--end` 都写在子命令之后（契约 §1.5 的写法）。"""
    x, y = point
    return cu("move", str(x), str(y), "--hwnd", hwnd, "--session", session,
              "--describe", note, "--end", "--json")


def error_envelope(out: str) -> dict:
    """从 **stdout** 取错误信封 —— `--json` 时它必须在这条流上（Q-025）。"""
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload.get("error") or {}


def show_rejection(name: str, code: int, out: str, err: str, note: str) -> None:
    """失败那两条的共同判据：exit 4 + `window_stale` + 信封在 stdout。"""
    error = error_envelope(out)
    verdict = "通过" if (code == 4 and error.get("code") == "window_stale" and not err) else "失败"
    record(name, verdict,
           f"{note} → exit={code}（应 4）· stdout 的 error_code="
           f"{error.get('code')!r}（应 window_stale）· stderr 是否为空={not err}"
           + ("" if verdict == "通过" else f" · stdout={out[:160]!r} · stderr={err[:160]!r}"))


# ---------------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 72)
    print("验收 §2.3 写操作前置的期望身份（Q-024）—— 真实 CLI + 真实 daemon")
    print("=" * 72)

    target = pick_target()
    if target is None:
        record("2.3", "失败", "找不到可用的靶窗口（需要非提权、未最小化、有标题的顶层窗口）")
        return 1

    hwnd = target["hwnd"]
    rect = target["rect"]
    point = (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
    print(f"  靶窗口：{hwnd}  {target['process']}  pid={target['pid']}  "
          f"\"{target['title']}\"  rect={rect}", flush=True)

    session, session_dir = begin("acceptance-2.3-preflight")
    if not session or not session_dir:
        record("2.3", "失败", "begin 没拿到 session_id / dir")
        return 1

    # ① 还没有该 hwnd 的截图 → 无可比对，保守放行（必须在截图之前做，见模块文档）。
    code1, out1, err1 = move_to(hwnd, point, session, "没有历史记录，应当放行")
    record("2.3a", "通过" if code1 == 0 else "失败",
           f"会话里还没有该窗口的截图 → exit={code1}（应 0）"
           + ("" if code1 == 0 else f" · stdout={out1[:120]!r} · {err1.splitlines()[:2]}"))

    # 登记身份：这一次截图就是「期望身份」的来源。
    code_shot, _out_shot, err_shot = cu("screenshot", "--hwnd", hwnd, "--session", session,
                                        "--describe", "登记靶窗口的身份")
    if code_shot != 0:
        record("2.3", "失败", f"截图失败：exit={code_shot} {err_shot.splitlines()[:2]}")
        return 1

    # ② 记录与现场一致 → 放行。
    code2, out2, err2 = move_to(hwnd, point, session, "期望身份一致，应当放行")
    record("2.3b", "通过" if code2 == 0 else "失败",
           f"记录与现场一致 → exit={code2}（应 0）"
           + ("" if code2 == 0 else f" · stdout={out2[:120]!r} · {err2.splitlines()[:2]}"))

    # 让 daemon 换一轮，使那个会话从盘上重新读（活动会话走的是内存里的清单）。
    if restart_daemon() is None:
        record("2.3", "失败", "daemon 重启失败，后面两条无法验证")
        return 1

    # ③ pid 不符 = hwnd 被复用给了别的进程。
    try:
        note3 = tamper_manifest(session_dir, pid=int(target["pid"]) + 1)
    except (OSError, RuntimeError, ValueError) as exc:
        record("2.3c", "失败", f"改不动会话清单：{exc}")
        return 1
    code3, out3, err3 = move_to(hwnd, point, session, "期望身份不符，应当拒绝")
    show_rejection("2.3c", code3, out3, err3, note3)

    # ④ 窗口类不符（同一个 hwnd，换了个窗口类）。
    try:
        note4 = tamper_manifest(session_dir, pid=int(target["pid"]), klass="NotARealWindowClass")
    except (OSError, RuntimeError, ValueError) as exc:
        record("2.3d", "失败", f"改不动会话清单：{exc}")
        return 1
    code4, out4, err4 = move_to(hwnd, point, session, "窗口类不符，应当拒绝")
    show_rejection("2.3d", code4, out4, err4, note4)

    # 收尾：写命令失败会让覆盖层冻结成红色并**留在屏幕上**（DEC-030：等用户按 Esc），
    # 而它在场时 daemon 的空闲退出不会发生。让 daemon 退一次把它一起带走。
    cu("daemon", "stop")
    cu("session", "end", "--session", session)
    print("\n  已停止 daemon（顺带清掉失败那两次留下的红色覆盖层）并结束会话", flush=True)

    print("\n===== §2.3 结果 =====")
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<6} {verdict:<5} {detail}")
    failed = [row for row in RESULTS if row[1] == "失败"]
    print(f"\n  通过 {len(RESULTS) - len(failed)} · 失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
