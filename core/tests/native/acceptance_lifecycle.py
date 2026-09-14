"""验收清单 §5（会话、锁与存储）的自动化落地 —— 走真实 CLI + 真实 daemon。

对应 `core/tests/acceptance.md` §5 的 5.1 ~ 5.9。这一节本该人眼验，实际上**没有一项
需要人眼**：每一条的判据都是「命令的退出码 / stderr 里的错误码 / 磁盘上文件的存在性 /
daemon 进程还在不在」。把它们留成空白，只是当初没写驱动。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_lifecycle.py

**它会杀掉 daemon 两次**（5.3b/5.4/5.9 要验崩溃恢复）。用的是本机默认管道与默认
data dir，所以运行期间不要有别的会话在操作桌面。全程约 3 分钟，其中 5.3a 单等 121 秒
（陈旧锁的判定阈值是 120s，这是产品里的常量，不为了测试改小 —— 改小就不是在验它了）。
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
    # flush：脚本跑几分钟，输出被重定向到文件时若不 flush，中途看不到任何进展。
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def cu(*args: str, timeout: float = 90.0,
       env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    """跑一次真实 CLI 进程。返回 (退出码, stdout, stderr)。"""
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8",
           **(env_extra or {})}
    proc = subprocess.run(
        [sys.executable, "-m", "cu", *args],
        cwd=str(CORE), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def cu_json(*args: str, timeout: float = 90.0) -> dict:
    # `--json` 写在**最后**（子命令之后）—— 这正是契约 §1.5 的示例写法，
    # 也让本脚本顺带成为 F11 的回归守卫：嵌套子命令挂上 common 父解析器之前，
    # 这个写法在 `session list` / `lock status` / `daemon status` 上一律
    # `unrecognized arguments`（退出码 2）。
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


def write_noop(session_id: str, describe: str, *,
               keep_alive: bool = False) -> tuple[int, str, str]:
    """发一条**不产生任何真实输入**的写命令，用来取 / 保持写锁。

    `scroll 0 0` 是刻意选的：它走完整的写路径（describe 校验 → 取锁 → 覆盖层 →
    输入封锁 → 记日志），但 dx=dy=0 时 `input.scroll` 直接返回，一个事件都不投。

    默认带 `--end`（操作完就退场）；`keep_alive=True` 时带 `--continue`
    —— 两者**互斥**：`--end` 会把保持窗口清零，覆盖层立刻退场。
    """
    tail = ("--continue",) if keep_alive else ("--end",)
    return cu("scroll", "0", "0", "--session", session_id,
              "--describe", describe, *tail)


def daemon_pid() -> int | None:
    result = cu_json("daemon", "status")
    return (result.get("result") or {}).get("pid")


def kill_daemon() -> bool:
    """硬杀 daemon（模拟崩溃）。返回是否杀掉。"""
    pid = daemon_pid()
    if pid is None:
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, text=True, timeout=20)
    time.sleep(1.0)
    return True


def wait_pipe_gone(timeout: float = 10.0) -> bool:
    """等旧管道消失（进程死亡后由 OS 回收）。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 能 WaitNamedPipe 到 0 超时 = 管道还在；报错 = 已经没了。
        if not kernel32.WaitNamedPipeW(PIPE, 0):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# 5.1 / 5.2 锁竞争与强夺
# ---------------------------------------------------------------------------


def check_5_1_and_5_2() -> None:
    holder, holder_dir = begin("acceptance-5.1-holder")
    waiter, waiter_dir = begin("acceptance-5.1-waiter")
    if not holder or not waiter:
        record("5.1", "失败", f"begin 失败：holder={holder!r} waiter={waiter!r}")
        return

    code, _out, err = write_noop(holder, "验收 5.1：占住写锁")
    if code != 0:
        record("5.1", "失败", f"占锁的写命令失败：exit={code} {err.splitlines()[:1]}")
        return

    # 第二个会话发写命令：应当等 lock_wait_seconds（默认 10s）后超时。
    started = time.monotonic()
    code2, _out2, err2 = write_noop(waiter, "验收 5.1：应当超时")
    waited = time.monotonic() - started
    has_holder_info = "holder_session" in err2 and "holder_pid" in err2
    record("5.1", "通过" if (code2 == 3 and "lock_timeout" in err2 and has_holder_info) else "失败",
           f"第二个会话 exit={code2}（应 3）等待 {waited:.1f}s · "
           f"错误码={'lock_timeout' if 'lock_timeout' in err2 else '缺失'} · "
           f"持有者信息={'有' if has_holder_info else '无'}")

    # 5.2 强夺：必须记进 waiter 会话的 ops.md，且含 reason。
    #
    # 会话只能用**环境变量**传给它：`lock unlock` 这个嵌套子解析器没挂 common 父解析器，
    # 不认 `--session`（而 `screenshot` / `click` 这些顶层子命令认）。契约里
    # `lock.forceUnlock` 的参数只有 `{reason}`，会话身份本来就靠 `COMPUTER_USE_SESSION`
    # 这条全局约定带过去 —— 走它才是契约预期的用法。
    reason = "验收 5.2：强夺写锁并核对日志"
    code3, _out3, err3 = cu("lock", "unlock", "--force", "--reason", reason,
                            env_extra={"COMPUTER_USE_SESSION": waiter})
    ops = waiter_dir / "ops.md"
    text = ops.read_text(encoding="utf-8") if ops.exists() else ""
    logged = "unlock --force" in text and reason in text
    record("5.2", "通过" if (code3 == 0 and logged) else "失败",
           f"exit={code3} · ops.md 含锚行与 reason={logged}"
           + ("" if code3 == 0 else f" · {err3.splitlines()[:1]}"))

    # 5.2b 无会话身份的强夺 —— 这是**主路径**：契约里 `lock.forceUnlock` 的参数
    # 只有 `{reason}`，按契约调用时必然没有 session_id。所以它必须落进 daemon 日志，
    # 否则「记入操作日志」（api-contract.md §1）这条承诺在最常见的形态下 100% 落空。
    # sessions/ 下**仍然不该有**落点 —— 没有会话身份，就没有那条会话的 ops.md 可写。
    lonely = "验收-5.2b-无会话身份的一次强夺"
    code4, _out4, err4 = cu("lock", "unlock", "--force", "--reason", lonely)
    data_root = holder_dir.parent.parent
    in_log = _reason_in_daemon_log(lonely, data_root)
    in_sessions = _reason_written_anywhere(lonely, data_root, roots=("sessions",))
    record("5.2b", "通过" if (code4 == 0 and in_log and not in_sessions) else "失败",
           f"不带会话身份 exit={code4}（应 0）· 该 reason 在 logs/daemon.log 里={in_log}"
           f"（应 True）· 在 sessions/ 下={in_sessions}（应 False —— 没有会话身份就没有 ops.md）"
           + ("" if code4 == 0 else f" · {err4.splitlines()[:1]}"))

    cu("session", "end", "--session", waiter)


def _reason_written_anywhere(needle: str, data_root: Path,
                             roots: tuple[str, ...] = ("sessions", "logs")) -> bool:
    """这个字符串被写进 data_root 下那几个子目录的任何文本文件里了吗。

    **只扫文本工件**：data_root 下还有 `venv-omni/` 与 `models/`（几万个文件、
    上百 MB 二进制），对它们做 `rglob` + 全文解码会把这一步拖成几分钟。
    """
    for name in roots:
        root = data_root / name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in (".md", ".json", ".log", ".txt"):
                continue
            try:
                if needle in path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
    return False


def _reason_in_daemon_log(needle: str, data_root: Path) -> bool:
    """这个字符串进了 daemon 日志吗 —— §5.2b 的判据（Q-023）。"""
    log = data_root / "logs" / "daemon.log"
    try:
        return log.is_file() and needle in log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 5.3 陈旧锁回收（两条路径）
# ---------------------------------------------------------------------------


def check_5_3a_stale() -> None:
    """活 daemon + 持有者心跳过期 → 下一次 begin 回收它。

    `STALE_AFTER_SECONDS = 120`，所以这里必须实等 121 秒 —— 把常量改小再验，
    验的就不是产品里那个判定，而是测试自己改出来的一个数。
    """
    holder, _dir = begin("acceptance-5.3a-holder")
    if not holder:
        record("5.3a", "失败", "begin 失败，无法制造持有者")
        return
    _code, _out, err = write_noop(holder, "验收 5.3a：占锁后停止心跳")
    if _code != 0:
        record("5.3a", "失败", f"占锁失败：{err.splitlines()[:1]}")
        return
    print("        … 等 121 秒让持有者心跳过期（阈值 120s，产品常量）")
    for remaining in range(121, 0, -20):
        print(f"        剩余约 {remaining}s")
        time.sleep(min(20, remaining))
    # **只发这一次 begin**：回收就发生在它内部，多试一次就把证据用掉了。
    #
    # 必须用 `--json` 读：人读文本的 `begin` 渲染只有 session 与 dir 两行，
    # `reclaimed_stale_lock` 是**只在 JSON 里**出现的字段（见验收记录的发现）。
    result = cu_json("begin", "--agent-hint", "acceptance-5.3a-fresh")
    payload = result.get("result") or {}
    reclaimed = payload.get("reclaimed_stale_lock")
    # 交叉验证：锁真的不在任何会话手上了。
    holder_after = (cu_json("lock", "status").get("result") or {}).get("holder")
    if reclaimed is None:
        code, out, err = cu("begin", "--agent-hint", "acceptance-5.3a-fresh2")
        text_has = "reclaimed" in out
        record("5.3a", "失败",
               f"JSON 里没有 reclaimed_stale_lock（锁状态 holder={holder_after}）· "
               f"人读文本含该字段={text_has} · 退出码={code} {err.splitlines()[:1]}")
        return
    record("5.3a", "通过",
           f"心跳过期后下一次 begin 回收了陈旧锁："
           f"被回收者={reclaimed.get('session_id')} 空闲了 {reclaimed.get('idle_for_s')}s · "
           f"回收后锁状态 holder={holder_after}")


def check_5_3b_crash_restart() -> None:
    """杀 daemon → 新 begin 不被死锁（锁随 daemon 进程消失，这是崩溃即安全的同一条）。"""
    killed = kill_daemon()
    gone = wait_pipe_gone()
    code, out, err = cu("begin", "--agent-hint", "acceptance-5.3b-after-crash",
                        timeout=60)
    record("5.3b", "通过" if (killed and gone and code == 0) else "失败",
           f"杀进程={killed} 旧管道销毁={gone} · 崩溃后 begin exit={code} "
           f"{out.splitlines()[:1] if out else err.splitlines()[:1]}")


# ---------------------------------------------------------------------------
# 5.4 崩溃后会话变 orphaned
# ---------------------------------------------------------------------------


def check_5_4() -> None:
    session_id, _dir = begin("acceptance-5.4-orphan")
    before = _status_of(session_id)
    kill_daemon()
    # 任何一条命令都会拉起新 daemon，prepare() 里的 mark_orphans 在那一刻执行。
    cu("daemon", "status", timeout=60)
    after = _status_of(session_id)
    record("5.4", "通过" if before == "active" and after == "orphaned" else "失败",
           f"崩溃前 status={before!r} → 重启后 status={after!r}")


def _status_of(session_id: str) -> str:
    result = cu_json("session", "list")
    rows = (result.get("result") or {}).get("sessions") or []
    return next((r["status"] for r in rows if r["session_id"] == session_id), "（不存在）")


# ---------------------------------------------------------------------------
# 5.5 / 5.6 配额清理
# ---------------------------------------------------------------------------


def _quota_gap(root: Path) -> int:
    """复现 `storage.dir_size` 的口径 —— 配额判定的依据必须与产品同源。"""
    from cu.daemon.storage import dir_size

    return dir_size(root)


def check_5_5() -> None:
    """阶段 1：删最旧会话的图片与结构化数据，**保留 ops.md**。

    在临时目录里跑**真实的** `Sessions.end()` → `storage.cleanup()` 路径。
    为什么不通过 CLI：那要把用户的 `storage_limit_bytes` 真改成几百 KB，而配置是
    全局的（daemon 的 data dir 无法从命令行覆盖）。用同一套代码换一个 sessions 根，
    验的是同一条清理逻辑 —— 差别只在「配额是从哪来的」。
    """
    import shutil
    import tempfile
    from datetime import datetime

    from cu.daemon.sessions import Sessions
    from cu.ids import artifact_name

    root = Path(tempfile.mkdtemp(prefix="cu-quota-"))
    try:
        sessions = Sessions(root, storage_limit_bytes=0)      # 先不清理
        old = sessions.begin(moment=datetime(2026, 1, 1, 10, 0, 0), agent_hint="验收-旧")
        new = sessions.begin(moment=datetime(2026, 1, 2, 10, 0, 0), agent_hint="验收-新")

        # 文件名一律走 `ids.artifact_name` —— 清理靠 `is_parsed_name` 认名字，
        # 手写一个「看起来像」的名字会让这条验收变成在验我编的名字。
        shot = artifact_name("win", 1, hwnd="0x0000ABCD", title="验收靶子",
                             origin=(100, 100), ext="png")
        parsed = artifact_name("win", 1, hwnd="0x0000ABCD", title="验收靶子",
                               origin=(100, 100), suffix="omni", ext="md")
        (old.directory / shot).write_bytes(b"x" * 200_000)
        (old.directory / parsed).write_text("# 结构化数据\n" + "y" * 50_000, encoding="utf-8")
        (new.directory / shot).write_bytes(b"x" * 200_000)

        # 上限要卡在「**两张都删掉才够、只删一张不够**」的位置：
        # 清理是一边走一边回头看的（每删一个就检查是否已经够了），所以上限必须
        # 严格小于「只删掉较大的那张图之后」的用量，才能逼出「结构化数据也跟着走」。
        # 名字排序里 png 排在 md 前面，先删的是图。
        png_size = (old.directory / shot).stat().st_size
        md_size = (old.directory / parsed).stat().st_size
        sessions.storage_limit_bytes = _quota_gap(root) - png_size - md_size + 1000

        result = sessions.end(old.session_id)
        png_gone = not (old.directory / shot).exists()
        md_gone = not (old.directory / parsed).exists()
        ops_kept = (old.directory / "ops.md").exists()
        # 新会话是活动会话，必须毫发无损。
        new_kept = (new.directory / shot).exists()
        record("5.5", "通过" if (png_gone and md_gone and ops_kept and new_kept) else "失败",
               f"阶段1 删图={png_gone} 删结构化数据={md_gone} 留 ops.md={ops_kept} · "
               f"活动会话未动={new_kept} · 释放 {result['freed_bytes']} 字节 "
               f"（文件名取自 artifact_name：{parsed}）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_5_6() -> None:
    """阶段 2：图片删光仍超限 → 整目录删；**活动会话绝不删**。"""
    import shutil
    import tempfile
    from datetime import datetime

    from cu.daemon.sessions import Sessions
    from cu.ids import artifact_name

    root = Path(tempfile.mkdtemp(prefix="cu-quota2-"))
    try:
        sessions = Sessions(root, storage_limit_bytes=0)
        old = sessions.begin(moment=datetime(2024, 1, 1, 1, 0, 0), agent_hint="验收-最旧")
        live = sessions.begin(moment=datetime(2024, 1, 2, 1, 0, 0), agent_hint="验收-活动")
        shot = artifact_name("win", 1, hwnd="0x0000AAAA", title="验收靶子",
                             origin=(10, 10), ext="png")
        (old.directory / shot).write_bytes(b"z" * 100_000)
        (live.directory / shot).write_bytes(b"z" * 100_000)

        # 上限 1 字节：阶段 1 之后仍超限，必须进阶段 2。
        sessions.storage_limit_bytes = 1
        result = sessions.end(old.session_id)

        live_kept = ((live.directory / "ops.md").exists()
                     and (live.directory / shot).exists())
        untouched = live.session_id not in result["deleted_sessions"]
        old_gone = not old.directory.exists()
        record("5.6", "通过" if (live_kept and untouched and old_gone) else "失败",
               f"活动会话文件完好={live_kept} 未被删={untouched} · 最旧会话已整删={old_gone} · "
               f"deleted_sessions={result['deleted_sessions']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5.7 / 5.8 空闲退出的两条豁免
# ---------------------------------------------------------------------------


def check_5_7() -> None:
    """覆盖层在场 → 空闲退出绝不发生。

    **不能在等待期间查询 daemon**：`handle_request` 的第一步就是 `touch()`，
    一问就把空闲计时清零，那验的就不是「它自己不会退」而是「我一直在戳它」。
    所以等待期间只看进程是否还活着。
    """
    _code, _out, _err = cu("config", "set", "daemon_idle_exit_seconds", "5")
    try:
        session_id, _dir = begin("acceptance-5.7-overlay")
        # `--continue`：保持窗口内（默认 30s），覆盖层与输入封锁都留在场上。
        write_noop(session_id, "验收 5.7：让覆盖层留在场上", keep_alive=True)
        pid = daemon_pid()
        # 结束会话 → 活动会话与写锁都没了，场上只剩覆盖层这一个理由。
        cu("session", "end", "--session", session_id)
        print("        … 12 秒不打扰 daemon（空闲阈值 5s）")
        time.sleep(12.0)
        alive = _process_alive(pid) if pid else False
        record("5.7", "通过" if alive else "失败",
               f"daemon pid={pid} 在覆盖层仍可见时熬过 12s 空闲阈值={alive}")
    finally:
        cu("config", "set", "daemon_idle_exit_seconds", "600")
        cu("daemon", "stop")


def _process_alive(pid: int) -> bool:
    import ctypes

    handle = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.WinDLL("kernel32").CloseHandle(handle)
    return True


def check_5_8() -> None:
    """写锁被持有 → 空闲退出绝不发生。

    直接测 `_idle_watch` 的守卫表达式，而不是跑一遍等待：**锁的持有者必然是
    一个活动会话**（锁只由 `_write` 按 session_id 取，`session end` 先释放锁再结束
    会话），所以「只有锁、没有活动会话」这个局面在产品里不可达 —— 想制造它，
    只能绕过产品代码直接调 `lock.acquire`。这正是这里做的事：不伪造成一次真实
    操作，而是把那一条守卫拿出来单独钉住。
    """
    import tempfile

    from cu.config import Config
    from cu.daemon.core import Daemon

    config = Config(data_dir=tempfile.mkdtemp(prefix="cu-idle-"))
    daemon = Daemon(config)
    guard = lambda: bool(daemon.sessions.active_ids() or daemon.lock.held   # noqa: E731
                         or daemon._overlay_present())
    before = guard()
    daemon.lock.acquire("s-manual-holder", 4242, timeout=0.0)
    held = guard()
    daemon.lock.release("s-manual-holder")
    after = guard()
    record("5.8", "通过" if (not before and held and not after) else "失败",
           f"空闲时为假={not before} · 仅持锁时为真={held} · 释放后为假={not after}")


# ---------------------------------------------------------------------------
# 5.9 崩溃后无残留管道
# ---------------------------------------------------------------------------


def check_5_9() -> None:
    kill_daemon()
    gone = wait_pipe_gone()
    code, out, err = cu("daemon", "status", timeout=60)
    record("5.9", "通过" if (gone and code == 0 and "pid" in out) else "失败",
           f"崩溃后旧管道已销毁={gone} · 立刻再起 exit={code} "
           f"{out.splitlines()[:1] if out else err.splitlines()[:1]}")


def report_findings() -> None:
    """把当初在这套流程里撞见的几个缺口逐条复验一遍。

    它们**不属于任何一条验收项**（是 2026-09-13 全量验收时顺带抓出来的），
    所以单独列在这里：每条都给一个「通过 / 失败」的判据，而不是只打印事实。
    """
    print("\n----- 当初撞见的缺口，逐条复验 -----", flush=True)

    # F1 / Q-022：INTERNAL_ERROR 的 hint 让人「详见 daemon 日志」——
    # 那个文件以前从未被创建过；现在 daemon 起停就会写它。
    data_root = Path.home() / ".computer-use"
    log = data_root / "logs" / "daemon.log"
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    record("F1", "通过" if "daemon 启动" in log_text else "失败",
           f"daemon 日志 {log} 存在={log.is_file()}（hint 承诺的那个文件）· "
           f"含启动记录={'daemon 启动' in log_text}")

    # F4 / Q-023：无会话身份的强夺必须留痕 —— 判据在 §5.2b 里（要真的夺一次锁）。
    print("  [F4] 无会话身份的强夺是否留痕 → 见 §5.2b 的实测", flush=True)

    # F11：嵌套子命令没挂 common 父解析器，选项写在子命令之后会 unrecognized。
    # 契约 §1.5 的示例写法正是「选项跟在子命令之后」，所以这四条现在都该是 0。
    nested = [("daemon", "status", "--json"), ("config", "show", "--json"),
              ("session", "list", "--json"), ("lock", "status", "--json"),
              ("session", "end", "--session", "s-nonexistent-probe", "--json")]
    codes = {"/".join(item): cu(*item)[0] for item in nested}
    after_ok = all(code in (0, 3) for code in codes.values())        # 3 = 会话不存在，也算「认了参数」
    before_code = cu("--json", "daemon", "status")[0]                # 旧写法仍要能走
    record("F11", "通过" if after_ok and before_code == 0 else "失败",
           f"选项写在子命令之后：{codes}（应无 2 —— 2 就是 unrecognized arguments）· "
           f"写在最前面仍是 exit={before_code}")


def cleanup_sessions() -> int:
    """结束本次验收造出来的会话，别把测试痕迹留在用户真实的 data dir 里。"""
    result = cu_json("session", "list")
    rows = (result.get("result") or {}).get("sessions") or []
    ended = 0
    for row in rows:
        hint = row.get("agent_hint") or ""
        if not hint.startswith("acceptance-") or row.get("status") == "ended":
            continue
        code, _out, _err = cu("session", "end", "--session", row["session_id"])
        ended += 1 if code == 0 else 0
    return ended


# ---------------------------------------------------------------------------


CHECKS = {
    "5.1": None,      # 5.1 与 5.2 在同一个函数里，见 main()
    "5.3a": None,
    "5.3b": None,
    "5.4": None,
    "5.5": None,
    "5.6": None,
    "5.7": None,
    "5.8": None,
    "5.9": None,
}


def main() -> int:
    # 行缓冲：脚本要跑几分钟，输出重定向到文件时默认的块缓冲会让中途完全看不到进展。
    sys.stdout.reconfigure(line_buffering=True)

    only = None
    for index, arg in enumerate(sys.argv):
        if arg == "--only" and index + 1 < len(sys.argv):
            only = {part.strip() for part in sys.argv[index + 1].split(",") if part.strip()}
    if not only:
        only = set(CHECKS)

    def want(*items: str) -> bool:
        return any(item in only for item in items)

    print("=" * 72)
    print("验收 §5 会话、锁与存储（全自动，不需人眼）")
    print("=" * 72)
    print("""
  [!] 顺序有讲究：5.1 的锁超时会让覆盖层停在 Error 态（红色，等用户按 Esc 才关），
      而 5.3b 会杀掉 daemon —— 覆盖层随进程消失。所以先做 5.3a 的长等待，
      再做 5.1/5.2，紧接着 5.3b 把那个红色覆盖层一并清掉。
""")

    if want("5.1", "5.2"):
        check_5_3a_stale()
        check_5_1_and_5_2()
    if want("5.3b"):
        check_5_3b_crash_restart()
    if want("5.4"):
        check_5_4()
    if want("5.5"):
        check_5_5()
    if want("5.6"):
        check_5_6()
    if want("5.7"):
        check_5_7()
    if want("5.8"):
        check_5_8()
    if want("5.9"):
        check_5_9()

    report_findings()
    ended = cleanup_sessions()
    print(f"\n  已结束 {ended} 个验收会话")

    print("\n===== §5 结果 =====")
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    failed = [r for r in RESULTS if r[1] == "失败"]
    print(f"\n  通过 {len(RESULTS) - len(failed)} · 失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
