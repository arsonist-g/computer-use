"""验收补充：**打包后的全新安装**（本轮新增 —— 这一条本项目从来没验过）。

## 为什么要有这个脚本

验收 §8 的 `acceptance_install.py` 验的是**仓库内路径**：它跑 `node bin/computer-use.mjs …`，
而真实用户拿到的是 **npm 装出来的包**：`node_modules/@arsonist-g/computer-use/`，
入口是 npm 生成的 `.cmd` 垫片。两者不是一条路径 —— 例如「`files` 清单漏了一个文件」
这种缺陷只有从 tarball 装出来才会暴露（仓库里那个文件当然在）。

## 它验什么

1. `npm pack` 的产物能被 `npm install` 装进一个干净目录；
2. 装出来的布局对：包目录存在、`.cmd` 垫片存在、`core/pyproject.toml` / `core/src/` / `skill/computer-use/` 都在；
3. 从**装出来的** CLI 跑 `env sync` → 在隔离 home 里建出可用环境（不碰你真实的 `~/.computer-use`）；
4. 再跑一条**不需要 daemon** 的命令（`--version`）；
5. 再跑一条**需要 daemon** 的命令（`windows`：daemon 自动拉起）；
6. `skill install` / `skill uninstall` 在隔离的技能目录里可逆。

## 跑法

    .venv/Scripts/python.exe core/tests/native/acceptance_pack_install.py
    .venv/Scripts/python.exe core/tests/native/acceptance_pack_install.py --from-head
        # 用 `git archive HEAD` 的一份纯净副本打包（工作区正在被别人改时用这个）

## 隔离

全程在临时目录里：包从 tarball 装进 `<tmp>/app`，运行环境用 `USERPROFILE` +
`COMPUTER_USE_HOME` + 独立的 `COMPUTER_USE_PIPE` 指到临时目录 —— **不碰真实的
`~/.computer-use/`**（那里有已装的 omni 环境，数 GB）。

反斜杠一律逐字符构造（`BS = chr(92)`）：写在字面量里会被 bash / heredoc 各层吃掉，
实测因此把管道名弄非法过（`CreateNamedPipeW` err=123）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()
ROOT = CORE.parent
NODE = shutil.which("node")
NPM = shutil.which("npm")

BS = chr(92)
PACKAGE_DIR_NAME = "computer-use"
SCOPE = "@arsonist-g"
COMSPEC = os.environ.get("ComSpec") or "cmd.exe"


def unique_pipe(tag: str) -> str:
    return BS * 2 + "." + BS + "pipe" + BS + f"cu-pack-{tag}-{os.getpid()}"


RESULTS: list[tuple[str, str, str]] = []


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] {item}  {detail}", flush=True)


def run(argv: list[str], *, env: dict | None = None, cwd: Path | None = None,
        timeout: float = 900.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def make_source(tmp: Path, from_head: bool) -> Path:
    """要打包的源码树：工作区，或 `git archive HEAD` 的一份纯净副本。"""
    if not from_head:
        return ROOT
    source = tmp / "source"
    source.mkdir()
    archive = tmp / "head.tar"
    packed = run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"], cwd=ROOT)
    if packed.returncode != 0:
        raise SystemExit(f"git archive 失败：{packed.stderr.strip()[:200]}")
    with tarfile.open(archive) as handle:
        handle.extractall(source)
    return source


def main() -> int:
    from_head = "--from-head" in sys.argv
    if NODE is None or NPM is None:
        print("找不到 node / npm，无法进行")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="cu-pack-"))
    print("=" * 78)
    print("打包后的全新安装" + ("（源码来自 HEAD 的纯净副本）" if from_head else "（源码来自工作区）"))
    print(f"临时目录：{tmp}")
    print("=" * 78)

    try:
        source = make_source(tmp, from_head)

        # ---- 1. npm pack ----
        packed = run([NPM, "pack", "--pack-destination", str(tmp)], cwd=source, timeout=300)
        tarballs = sorted(tmp.glob("*.tgz"))
        if packed.returncode != 0 or not tarballs:
            record("1 pack", "失败",
                   f"`npm pack` exit={packed.returncode} · {(packed.stderr or '').strip()[:200]}")
            return 1
        tarball = tarballs[0]
        record("1 pack", "通过", f"{tarball.name}（{tarball.stat().st_size / 1024:.0f} KiB）")

        # tarball 内容：够不够、多不多（`__pycache__` 不该进包）
        with tarfile.open(tarball) as handle:
            names = [name.removeprefix("package/") for name in handle.getnames()]
        pyc = [name for name in names if name.endswith(".pyc") or "__pycache__" in name]
        py_files = [name for name in names if name.endswith(".py")]
        wanted = ["bin/computer-use.mjs", "core/pyproject.toml", "package.json",
                  "skill/computer-use/SKILL.md", "skill/computer-use/references/errors.md",
                  "skill/computer-use/references/install-and-config.md"]
        missing = [name for name in wanted if name not in names]
        record("2 内容", "通过" if not missing and not pyc else "失败",
               f"条目 {len(names)} 个 · `.py` {len(py_files)} 个 · "
               f"混进包的 `.pyc`/`__pycache__` {len(pyc)} 个 · 缺必需文件 {missing or '无'}")

        # ---- 3. 装进干净目录 ----
        app = tmp / "app"
        app.mkdir()
        (app / "package.json").write_text('{"name":"consumer","private":true}', encoding="utf-8")
        installed = run([NPM, "install", "--no-audit", "--no-fund", str(tarball)], cwd=app)
        package_root = app / "node_modules" / SCOPE / PACKAGE_DIR_NAME
        shim = app / "node_modules" / ".bin" / "computer-use.cmd"
        if installed.returncode != 0 or not package_root.is_dir():
            record("3 安装", "失败",
                   f"`npm install` exit={installed.returncode} · "
                   f"包目录存在={package_root.is_dir()} · {(installed.stderr or '').strip()[:200]}")
            return 1
        record("3 安装", "通过",
               f"包目录 {package_root.is_dir()} · `.cmd` 垫片 {shim.exists()} · "
               f"pyproject { (package_root / 'core' / 'pyproject.toml').exists() } · "
               f"skill { (package_root / 'skill' / 'computer-use' / 'SKILL.md').exists() } · "
               f"references { (package_root / 'skill' / 'computer-use' / 'references').is_dir() }")

        # ---- 4-6. 从装出来的 CLI 跑命令（隔离 home）----
        home = tmp / "home"
        home.mkdir()
        data = home / ".computer-use"
        env = {
            **os.environ,
            "USERPROFILE": str(home),
            "COMPUTER_USE_HOME": str(data),
            "COMPUTER_USE_PIPE": unique_pipe("pack"),
            "COMPUTER_USE_SKILL_DIR": str(tmp / "skills" / PACKAGE_DIR_NAME),
            "PYTHONIOENCODING": "utf-8",
        }

        def cli(*args: str, timeout: float = 900.0) -> subprocess.CompletedProcess:
            """经 npm 生成的 `.cmd` 垫片调用 —— 与真实用户敲的命令是同一条路径。

            **必须经 `cmd.exe /c`**：`.cmd` 不是可执行映像，`CreateProcess` 起不动它，
            只有命令解释器能跑（真实用户也是在一个 shell 里敲这条命令的）。
            """
            return subprocess.run([COMSPEC, "/c", str(shim), *args], env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout)
        sync = cli("env", "sync")
        built = (data / "venv" / "Scripts" / "python.exe").exists()
        record("4 env sync", "通过" if sync.returncode == 0 and built else "失败",
               f"exit={sync.returncode} · 环境建出={built} · "
               f"{(sync.stdout or sync.stderr or '').strip().splitlines()[-1][:110] if (sync.stdout or sync.stderr) else ''}")

        version = cli("--version")
        record("5 不需要 daemon", "通过" if version.returncode == 0 and "computer-use" in (version.stdout or "")
               else "失败",
               f"exit={version.returncode} · 输出={(version.stdout or version.stderr or '').strip()[:60]}")

        windows = cli("windows", timeout=300)
        record("6 需要 daemon", "通过" if windows.returncode == 0 and "hwnd=" in (windows.stdout or "")
               else "失败",
               f"exit={windows.returncode} · "
               f"{(windows.stdout or windows.stderr or '').strip().splitlines()[0][:110] if (windows.stdout or windows.stderr) else ''}")

        # ---- 7. skill 安装 / 卸载可逆 ----
        skill_dir = Path(env["COMPUTER_USE_SKILL_DIR"])
        install = cli("skill", "install")
        same = ((skill_dir / "SKILL.md").exists()
                and (skill_dir / "references" / "errors.md").exists()
                and (skill_dir / "references" / "install-and-config.md").exists()
                and not list(skill_dir.rglob("*-zh.md")))
        uninstall = cli("skill", "uninstall")
        gone = not skill_dir.exists()
        record("7 skill 安装/卸载", "通过" if install.returncode == 0 and same and gone else "失败",
               f"install exit={install.returncode} 落点与 references 齐备={same} · "
               f"uninstall exit={uninstall.returncode} 已移除={gone}")

        # ---- 8. 收尾：不要留下正在跑的 daemon ----
        cli("daemon", "stop", timeout=120)
        leftovers = sorted(name for name in (data.iterdir() if data.exists() else []))
        record("8 隔离目录", "通过" if data.exists() else "失败",
               f"隔离数据目录内容：{[name.name for name in leftovers]}")

        print("\n===== 结果 =====")
        failed = [row for row in RESULTS if row[1] == "失败"]
        for item, verdict, detail in RESULTS:
            print(f"  {item:<18} {verdict:<5} {detail}")
        print(f"\n失败 {len(failed)} 项" + ("" if not failed else " —— 打包/安装链路未通"))
        return 1 if failed else 0
    finally:
        if "--keep" not in sys.argv:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n（--keep：临时目录保留在 {tmp}）")


if __name__ == "__main__":
    raise SystemExit(main())
