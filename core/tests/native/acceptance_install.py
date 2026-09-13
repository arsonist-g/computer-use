"""验收清单 §8（安装与分发）的自动化落地 —— 真跑 Node 启动器，全程隔离。

对应 `core/tests/acceptance.md` §8 的 8.1 ~ 8.6。

隔离手段：启动器认 `COMPUTER_USE_HOME`（环境装哪）与 `COMPUTER_USE_SKILL_DIR`
（SKILL 装哪）两个环境变量，所以整节可以在临时目录里跑完，不碰
`~/.computer-use/venv`，也不碰你真实的技能目录。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_install.py
    （可加 --skip-install 跳过 8.2/8.3 的下载，只验 8.1 / 8.4 / 8.5 / 8.6）
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()
ROOT = CORE.parent
LAUNCHER = ROOT / "bin" / "computer-use.mjs"
NODE = shutil.which("node")
PIPE = local_pipe()

RESULTS: list[tuple[str, str, str]] = []


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def run_node(args: list[str], *, home: Path, env_extra: dict | None = None,
             timeout: float = 600.0) -> subprocess.CompletedProcess:
    env = {**os.environ, "COMPUTER_USE_HOME": str(home),
           "PYTHONIOENCODING": "utf-8", **(env_extra or {})}
    return subprocess.run([NODE, str(LAUNCHER), *args], env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def cu(*args: str, timeout: float = 90.0) -> tuple[int, str, str]:
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "cu", *args], cwd=str(CORE), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


# ---------------------------------------------------------------------------
# 8.1 uv 缺失：明确报错 + 安装指引，且**不回退系统 pip**
# ---------------------------------------------------------------------------


def check_8_1() -> None:
    home = Path(tempfile.mkdtemp(prefix="cu-8.1-"))
    try:
        uv_path = shutil.which("uv")
        if uv_path is None:
            record("8.1", "不适用", "本机没装 uv —— 无法构造出「有 uv」以外的对照")
            return
        # 把 uv 所在目录从 PATH 里摘掉，其余保持不变（node 用绝对路径调，不受影响）。
        # **按「哪个目录里真的有 uv 可执行文件」来摘，不要按 realpath**：
        # 本机的 uv 是 pipx 装的，`which uv` 给的是 shim（`…\bin\uv`），
        # 而 realpath 指向 pipx 的 venv —— 两者不是一个目录，按 realpath 摘会摘不掉。
        def has_uv(directory: str) -> bool:
            return any((Path(directory) / name).exists()
                       for name in ("uv.exe", "uv.cmd", "uv.bat", "uv"))

        kept = [entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                if entry and not has_uv(entry)]
        scrubbed = os.pathsep.join(kept)
        proc = run_node(["env", "sync"], home=home, env_extra={"PATH": scrubbed},
                        timeout=180)
        text = (proc.stdout or "") + (proc.stderr or "")
        guided = "uv 未安装" in text and "astral.sh/uv" in text
        no_pip = "不会退回系统 pip" in text
        created = (home / "venv" / "Scripts" / "python.exe").exists()
        record("8.1", "通过" if (proc.returncode != 0 and guided and no_pip and not created)
               else "失败",
               f"exit={proc.returncode}（应非 0）· 含安装指引={guided} · "
               f"明示不回退系统 pip={no_pip} · 没有偷偷建环境={not created} · "
               f"PATH 中已摘除含 uv 的目录（{uv_path}）")
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8.2 首次安装：干净目录里建出可用环境
# ---------------------------------------------------------------------------


def check_8_2(skip: bool) -> None:
    if skip:
        record("8.2", "不适用", "--skip-install：本轮未验")
        return
    home = Path(tempfile.mkdtemp(prefix="cu-8.2-"))
    try:
        started = time.monotonic()
        proc = run_node(["env", "sync"], home=home, timeout=900)
        elapsed = time.monotonic() - started
        python = home / "venv" / "Scripts" / "python.exe"
        if proc.returncode != 0 or not python.exists():
            record("8.2", "失败",
                   f"exit={proc.returncode} 耗时 {elapsed:.0f}s · "
                   f"环境={python.exists()} · "
                   f"{(proc.stderr or proc.stdout or '').strip().splitlines()[-3:]}")
            return
        probe = subprocess.run([str(python), "-m", "cu", "--version"], cwd=str(CORE),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
        ok = probe.returncode == 0 and "computer-use" in (probe.stdout or "")
        stamp = (home / "venv" / ".computer-use-stamp").exists()
        record("8.2", "通过" if ok else "失败",
               f"从零建环境 exit={proc.returncode} 耗时 {elapsed:.0f}s · "
               f"{python.exists()=} · 戳文件={stamp} · "
               f"`-m cu --version` → {(probe.stdout or probe.stderr or '').strip()[:60]}")
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8.3 镜像 403 → 单次回退官方源，且报错透出索引源与状态码
# ---------------------------------------------------------------------------


class _DenyAll(http.server.BaseHTTPRequestHandler):
    """一个对任何请求都回 403 的索引 —— 「会 403 的镜像」的确定性构造。"""

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定名
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"denied by acceptance stub")

    def log_message(self, *args) -> None:
        pass


def check_8_3(skip: bool) -> None:
    if skip:
        record("8.3", "不适用", "--skip-install：本轮未验")
        return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DenyAll)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = Path(tempfile.mkdtemp(prefix="cu-8.3-"))
    denied = f"http://127.0.0.1:{port}/simple"
    try:
        # 让**两次**都失败：索引指向 403 桩，再让官方源走一个不存在的代理，
        # 这样失败信息里的索引源与状态码才都能被读到（这正是 DEC-042 要的「透出」）。
        proc = run_node(["env", "sync"], home=home, timeout=420, env_extra={
            "UV_INDEX_URL": denied, "UV_DEFAULT_INDEX": denied, "PIP_INDEX_URL": denied,
            "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
            # 403 桩在 localhost：别让它也走那个死代理，否则第一次失败会变成连接错误，
            # 「透出状态码」这一条就验不到了。
            "NO_PROXY": "127.0.0.1,localhost",
        })
        text = (proc.stdout or "") + (proc.stderr or "")
        fallback_count = text.count("默认索引失败，回退")
        shows_index = denied in text or "127.0.0.1" in text
        shows_status = "403" in text
        hint = "COMPUTER_USE_INDEX_URL" in text
        record("8.3", "通过" if (fallback_count == 1 and shows_index and shows_status) else "失败",
               f"回退提示出现 {fallback_count} 次（应恰好 1 次）· 失败信息含索引源={shows_index} · "
               f"含状态码 403={shows_status} · 提到可用索引覆盖={hint} · exit={proc.returncode}")
    finally:
        server.shutdown()
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8.4 / 8.5 SKILL 安装与卸载
# ---------------------------------------------------------------------------


def check_8_4_and_8_5() -> None:
    skill_dir = Path(tempfile.mkdtemp(prefix="cu-skill-")) / "computer-use"
    try:
        proc = run_node(["skill", "install"], home=Path(tempfile.gettempdir()),
                        env_extra={"COMPUTER_USE_SKILL_DIR": str(skill_dir)}, timeout=60)
        source = ROOT / "skill" / "computer-use"
        installed = skill_dir / "SKILL.md"
        refs_dir = skill_dir / "references"
        same = (installed.exists() and (source / "SKILL.md").exists()
                and installed.read_text(encoding="utf-8")
                == (source / "SKILL.md").read_text(encoding="utf-8"))
        refs = sorted(p.name for p in refs_dir.glob("*.md")) if refs_dir.is_dir() else []
        want_refs = sorted(p.name for p in (source / "references").glob("*.md")
                           if not p.name.endswith("-zh.md"))
        translated = sorted(p.name for p in skill_dir.rglob("*-zh.md"))
        ok = proc.returncode == 0 and same and refs == want_refs and not translated
        record("8.4", "通过" if ok else "失败",
               f"exit={proc.returncode} · 落点={installed}（存在={installed.exists()}，"
               f"与包内 SKILL.md 逐字节相同={same}）· references {refs} · "
               f"漏装的 references {sorted(set(want_refs) - set(refs)) or '无'} · 装进来的译本 {translated or '无'}")

        proc2 = run_node(["skill", "uninstall"], home=Path(tempfile.gettempdir()),
                         env_extra={"COMPUTER_USE_SKILL_DIR": str(skill_dir)}, timeout=60)
        gone = not skill_dir.exists()
        record("8.5", "通过" if (proc2.returncode == 0 and gone) else "失败",
               f"exit={proc2.returncode} · 目录已移除={gone}")
    finally:
        shutil.rmtree(skill_dir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8.6 daemon 自动拉起（用户无感）
# ---------------------------------------------------------------------------


def check_8_6() -> None:
    cu("daemon", "stop")
    time.sleep(1.5)
    started = time.monotonic()
    code, out, err = cu("windows", timeout=90)
    elapsed = time.monotonic() - started
    ok = code == 0 and "hwnd=" in out
    record("8.6", "通过" if ok else "失败",
           f"停掉 daemon 后直接跑 `windows`：exit={code} 耗时 {elapsed:.1f}s，"
           f"用户侧没有任何额外动作" + ("" if ok else f" · {err.splitlines()[:1]}"))


# ---------------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    skip = "--skip-install" in sys.argv
    print("=" * 72)
    print("验收 §8 安装与分发（临时目录隔离，不动你的 ~/.computer-use）")
    print("=" * 72)

    if NODE is None:
        print("  找不到 node，§8 无法进行")
        return 1

    # 先跑 8.1：它要求 PATH 里没有 uv，跑完之后的用例不受影响（各自构造 env）。
    check_8_1()
    check_8_2(skip)
    check_8_3(skip)
    check_8_4_and_8_5()
    check_8_6()

    print("\n===== §8 结果 =====")
    failed = [r for r in RESULTS if r[1] == "失败"]
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
